"""Alert de-duplication, rate limiting and daily summary for the 24/7 desk.

    python -m tools.ops.notify alert --report health.json --state notify-state.json
    python -m tools.ops.notify daily --root <FRESH_ROOT> --report health.json --state notify-state.json

Default notifier: one JSON line to stdout (the journal) plus a bounded ring of recent
messages in the state file. Telegram is used in addition only when a credential file
``telegram.json`` ({"token": ..., "chat_id": ...}) exists in ``$CREDENTIALS_DIRECTORY``
(systemd LoadCredential) or ``--credentials-dir``. The token is never written to the
state, stdout, stderr or any message, and send failures log only the exception type.
Notification only: this tool cannot trade, sign, or touch any store but its own state file.
"""
import argparse
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path

log = logging.getLogger(__name__)
DEFAULT_INTERVAL = 300
REPEAT_SECONDS = {'CRITICAL': 3600, 'WARN': 6 * 3600}
MAX_MESSAGES_PER_HOUR = 12
RING = 20
MAX_TEXT = 3500
SECRET_PATTERNS = (
    re.compile(r'(?i)\bbearer\s+\S+'),
    re.compile(r'\b\d{6,}:[A-Za-z0-9_-]{30,}\b'),                     # telegram bot token shape
    re.compile(r'(?i)\b[\w-]*(key|token|secret|password|passwd|authorization)[\w-]*\b\s*[=:]\s*\S+'),
    re.compile(r'(?i)[?&](api[-_]?key|key|token)=[^&\s]+'),
)


def redact(text, extra=()):
    text = str(text)
    for secret in extra:
        if secret:
            text = text.replace(secret, '[REDACTED]')
    for pattern in SECRET_PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    return text[:MAX_TEXT]


class JournalNotifier:
    name = 'journal'

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout

    def send(self, severity, text, now):
        self.stream.write(json.dumps({'notify': severity, 'ts': int(now), 'text': text}, sort_keys=True) + '\n')
        self.stream.flush()
        return True


class TelegramNotifier:
    name = 'telegram'

    def __init__(self, token, chat_id, opener=urllib.request.urlopen, log=None):
        self.token, self.chat_id, self.opener = token, str(chat_id), opener
        self.log = log or (lambda line: sys.stderr.write(line + '\n'))

    def send(self, severity, text, now):
        url = 'https://api.telegram.org/bot%s/sendMessage' % self.token
        body = urllib.parse.urlencode({'chat_id': self.chat_id, 'text': '[%s] %s' % (severity, text)}).encode()
        try:
            request = urllib.request.Request(url, data=body, method='POST')
            with self.opener(request, timeout=10) as response:
                return 200 <= getattr(response, 'status', 200) < 300
        except Exception as exc:  # noqa: BLE001 - never include str(exc): it can carry the URL
            code = getattr(exc, 'code', None)
            self.log('telegram send failed: %s%s' % (type(exc).__name__, ' %s' % code if code else ''))
            return False


def load_telegram(directory, opener=urllib.request.urlopen):
    if not directory:
        return None
    path = Path(directory) / 'telegram.json'
    if path.is_symlink():
        log.warning('telegram.json ignored: symlink refused; using default notifier')
        return None
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        token, chat = data['token'], data['chat_id']
        if type(token) is not str or not re.fullmatch(r'\d{6,}:[A-Za-z0-9_-]{30,}', token) or not str(chat).lstrip('-').isdigit():
            log.warning('telegram.json ignored: token or chat_id has an invalid format; using default notifier')
            return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # only the exception type: messages from json/KeyError can quote file contents
        log.warning('telegram.json ignored: unreadable or malformed (%s); using default notifier', type(exc).__name__)
        return None
    return TelegramNotifier(token, chat, opener)


def check_freshness(report, now, interval):
    """Never trust old OK data: a report older than 2x the healthcheck interval means the check is not running."""
    ts = report.get('ts')
    if type(ts) in (int, float) and math.isfinite(ts) and now - ts <= 2 * interval:
        return report
    age = 'has no usable timestamp' if type(ts) not in (int, float) or not math.isfinite(ts) else 'is %ds old' % (now - ts)
    return {'kind': 'desk_healthcheck_v1', 'status': 'CRITICAL', 'checks': [
        {'check': 'healthcheck', 'severity': 'CRITICAL',
         'detail': 'healthcheck not running: report %s (limit %ds = 2x interval)' % (age, 2 * interval)}]}


def load_state(path):
    try:
        data = json.loads(Path(path).read_text())
        if isinstance(data, dict) and isinstance(data.get('alerts'), dict):
            return data
    except (OSError, ValueError):
        pass
    return {'version': 1, 'alerts': {}, 'sent_at': [], 'ring': [], 'daily': None}


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name('.' + path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(state, stream, sort_keys=True)
    os.replace(tmp, path)


def finding_key(c):
    return c['check'] if not c.get('mint') else '%s:%s' % (c['check'], c['mint'])


def deliver(notifiers, severity, text, now, state):
    text = redact(text, [getattr(n, 'token', None) for n in notifiers])
    ok = all([n.send(severity, text, now) for n in notifiers])
    state['ring'] = (state['ring'] + [{'ts': int(now), 'severity': severity, 'text': text, 'delivered': ok}])[-RING:]
    if ok:
        state['sent_at'] = [t for t in state['sent_at'] if now - t < 3600] + [now]
    return ok


def process_report(report, state, now, notifiers, max_per_hour=MAX_MESSAGES_PER_HOUR):
    """One alert message per run covering new/escalated/due-reminder findings plus resolutions."""
    findings = {finding_key(c): c for c in report.get('checks', []) if c['severity'] != 'OK'}
    lines, keys, resolved = [], [], []
    for key, c in sorted(findings.items()):
        known = state['alerts'].get(key)
        due = (known is None or known['severity'] != c['severity'] and c['severity'] == 'CRITICAL'
               or now - known['last_sent'] >= REPEAT_SECONDS[c['severity']])
        if due:
            tag = 'NEW' if known is None else 'ESCALATED' if known['severity'] != c['severity'] else 'STILL'
            lines.append('%s %s %s: %s' % (c['severity'], tag, key, c['detail']))
            keys.append(key)
    for key in sorted(set(state['alerts']) - set(findings)):
        resolved.append(key)
        lines.append('RESOLVED %s' % key)
    if not lines:
        return {'sent': False, 'reason': 'nothing new'}
    recent = [t for t in state['sent_at'] if now - t < 3600]
    if len(recent) >= max_per_hour:
        return {'sent': False, 'reason': 'rate limited', 'suppressed': len(lines)}
    severity = 'CRITICAL' if any(findings[k]['severity'] == 'CRITICAL' for k in keys) else 'WARN' if keys else 'OK'
    if not deliver(notifiers, severity, '\n'.join(lines), now, state):
        return {'sent': False, 'reason': 'delivery failed'}
    for key in keys:
        known = state['alerts'].get(key)
        state['alerts'][key] = {'severity': findings[key]['severity'], 'last_sent': now,
                                'first_seen': known['first_seen'] if known else now,
                                'count': (known['count'] if known else 0) + 1}
    for key in resolved:
        del state['alerts'][key]
    return {'sent': True, 'alerts': len(keys), 'resolved': len(resolved)}


def summary(root, report, now):
    root = Path(root)
    with closing(sqlite3.connect((root / 'paper-ledger.sqlite').resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as c:
        row = c.execute('SELECT payload FROM state WHERE id=1').fetchone()
        state = json.loads(row[0]) if row else {'positions': {}, 'realized_pnl': '0', 'cash': '?', 'mode': 'NO_CHECKPOINT'}
        since = now - 86400
        buys = sells = 0
        for (payload,) in c.execute('SELECT o.payload FROM outcomes o JOIN events e ON e.event_id=o.event_id WHERE e.ts>?', (since,)):
            o = json.loads(payload)
            if o.get('type') == 'fill':
                buys += o.get('side') == 'buy'
                sells += o.get('side') == 'sell'
    blockers = ['%s: %s' % (finding_key(x), x['detail']) for x in report.get('checks', []) if x['severity'] != 'OK']
    lines = ['Daily paper desk summary (EXECUTION_UNVERIFIED, paper only)',
             'mode=%s cash=%s SOL realized_pnl=%s SOL' % (state.get('mode'), state.get('cash'), state.get('realized_pnl')),
             'open positions: %s' % (', '.join(m[:8] for m in state.get('positions', {})) or 'none'),
             'last 24h fills: %d buy, %d sell' % (buys, sells),
             'blockers: %s' % ('; '.join(blockers) if blockers else 'none')]
    return '\n'.join(lines)


def daily(root, report, state, now, notifiers, force=False):
    day = time.strftime('%Y-%m-%d', time.gmtime(now))
    if state.get('daily') == day and not force:
        return {'sent': False, 'reason': 'already sent today'}
    text = summary(root, report, now)
    ok = deliver(notifiers, 'WARN' if report.get('status') != 'OK' else 'OK', text, now, state)
    if ok:
        state['daily'] = day
    return {'sent': ok}


def main(argv=None, clock=time.time, opener=urllib.request.urlopen, stream=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('alert', 'daily'):
        s = sub.add_parser(name)
        s.add_argument('--report', required=True)
        s.add_argument('--state', required=True)
        s.add_argument('--credentials-dir', default=os.environ.get('CREDENTIALS_DIRECTORY'))
        s.add_argument('--interval', type=int, default=DEFAULT_INTERVAL, help='healthcheck interval seconds; reports older than 2x are stale')
        s.add_argument('--max-per-hour', type=int, default=MAX_MESSAGES_PER_HOUR)
    sub.choices['daily'].add_argument('--root', required=True)
    sub.choices['daily'].add_argument('--force', action='store_true')
    args = p.parse_args(argv)
    now = clock()
    notifiers = [JournalNotifier(stream)]
    telegram = load_telegram(args.credentials_dir, opener)
    if telegram:
        notifiers.append(telegram)
    try:
        report = json.loads(Path(args.report).read_text())
        if not isinstance(report, dict):
            raise ValueError('not an object')
        if report.get('kind') != 'desk_healthcheck_v1':
            raise ValueError('not a healthcheck report')
        report = check_freshness(report, now, args.interval)
    except (OSError, ValueError) as exc:
        report = {'kind': 'desk_healthcheck_v1', 'status': 'CRITICAL', 'checks': [
            {'check': 'notify_input', 'severity': 'CRITICAL', 'detail': 'healthcheck report unreadable: %s' % type(exc).__name__}]}
    state = load_state(args.state)
    result = (process_report(report, state, now, notifiers, args.max_per_hour) if args.command == 'alert'
              else daily(args.root, report, state, now, notifiers, args.force))
    save_state(args.state, state)
    (stream or sys.stdout).write(json.dumps({'notify_result': result, 'telegram': bool(telegram)}, sort_keys=True) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
