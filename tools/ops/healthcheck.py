"""Read-only 24/7 health check for the fresh-start paper desk.

Opens every database ``mode=ro`` and never writes to any of them. It prints one JSON line
and exits 2 when any check is CRITICAL (0 otherwise), so a timer unit shows as failed.
Optional ``--out`` atomically writes the same report for ``tools.ops.notify``. No network,
no provider I/O, no signing.

Layout read (matches ``tools.ops.fresh_start``): <root>/research.sqlite, evidence.sqlite,
paper-ledger.sqlite, entry-dispatch/dispatch.sqlite; shared pacing and discovery databases
are passed explicitly.
"""
import argparse
import datetime
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

OK, WARN, CRITICAL = 'OK', 'WARN', 'CRITICAL'
RANK = {OK: 0, WARN: 1, CRITICAL: 2}

# kind: service = long running (must stay active); oneshot = run by its timer; timer.
# manual: documented as enabled by the coordinator only (never required at boot).
UNITS = {
    'desk-continuous-discovery.service': {'kind': 'service'},
    'desk-dashboard.service': {'kind': 'service'},
    'desk-paper-entry-dispatcher.service': {'kind': 'oneshot', 'critical_failed': True},
    'desk-paper-held-cycle.service': {'kind': 'oneshot', 'critical_failed': True},
    'desk-paper-monitor.service': {'kind': 'oneshot'},
    'desk-decisions.service': {'kind': 'oneshot'},
    'desk-backup.service': {'kind': 'oneshot'},
    'desk-paper-entry-dispatcher.timer': {'kind': 'timer', 'manual': True, 'tick': ('entry_tick', 'desk-paper-entry-dispatcher.service')},
    'desk-paper-held-cycle.timer': {'kind': 'timer', 'tick': ('held_tick', 'desk-paper-held-cycle.service')},
    'desk-paper-monitor.timer': {'kind': 'timer', 'manual': True},
    'desk-decisions.timer': {'kind': 'timer', 'tick': ('decisions_tick', 'desk-decisions.service')},
    'desk-backup.timer': {'kind': 'timer'},
}
# (warn, critical) seconds / counts / fractions; override with --threshold name=value.
DEFAULTS = {
    'restarts_warn': 3, 'restarts_critical': 10,
    'discovery_frame_warn': 120, 'discovery_frame_critical': 300,
    'discovery_event_warn': 1800, 'discovery_event_critical': 7200,
    'entry_tick_warn': 300, 'entry_tick_critical': 900,
    'held_tick_warn': 600, 'held_tick_critical': 1800,
    'decisions_tick_warn': 180, 'decisions_tick_critical': 600,
    'mark_age_warn': 360, 'mark_age_critical': 900,
    'monitoring_headroom_warn': 0.20, 'monitoring_headroom_critical': 0.05,
    'monitoring_cap': 3600,
    'pacing_pending_stale': 300,
    'disk_free_fraction_warn': 0.20, 'disk_free_fraction_critical': 0.10,
    'disk_free_bytes_critical': 1 << 30,
    'backup_age_warn': 30 * 3600, 'backup_age_critical': 54 * 3600,
    'max_hold_seconds': 21600,
}
SYSTEMCTL_PROPS = ('ActiveState', 'SubState', 'Result', 'ExecMainStatus', 'NRestarts', 'UnitFileState', 'LastTriggerUSec', 'ActiveEnterTimestamp')


def check(name, severity, detail, **extra):
    return dict({'check': name, 'severity': severity, 'detail': detail}, **extra)


def ro(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError('missing or non-regular database: %s' % path)
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def graded(name, value, warn, critical, detail, **extra):
    severity = CRITICAL if value >= critical else WARN if value >= warn else OK
    return check(name, severity, detail, value=value, **extra)


def guarded(name, function, *args):
    """A failing probe is itself a finding, never a silent pass."""
    try:
        return function(*args)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return [check(name, CRITICAL, 'probe failed: %s: %s' % (type(exc).__name__, str(exc)[:160]))]


def parse_systemd_time(text):
    text = (text or '').strip()
    if not text or text == 'n/a' or text.startswith('0'):
        return None
    if text.startswith('@'):
        return float(text[1:])
    parsed = datetime.datetime.strptime(text, '%a %Y-%m-%d %H:%M:%S UTC')
    return parsed.replace(tzinfo=datetime.timezone.utc).timestamp()


def default_runner(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)


def show(runner, unit):
    result = runner(['systemctl', '--timestamp=utc', 'show', unit] + ['-p' + p for p in SYSTEMCTL_PROPS])
    if result.returncode != 0:
        raise OSError('systemctl show %s failed' % unit)
    return dict(line.partition('=')[::2] for line in result.stdout.splitlines() if '=' in line)


def check_units(runner, now, t, units=None):
    out = []
    for unit, spec in (units or UNITS).items():
        try:
            s = show(runner, unit)
        except (OSError, subprocess.SubprocessError) as exc:
            out.append(check('unit:' + unit, CRITICAL, str(exc)))
            continue
        active, result = s.get('ActiveState', ''), s.get('Result', '')
        enabled = s.get('UnitFileState', '')
        name = 'unit:' + unit
        if spec['kind'] == 'timer':
            if active != 'active':
                severity = OK if spec.get('manual') and enabled != 'enabled' else CRITICAL
                out.append(check(name, severity, 'timer %s (%s)%s' % (active, enabled, ' manual, intentionally off' if severity == OK else '')))
                continue
            out.append(check(name, OK, 'timer active'))
            if 'tick' in spec:
                label = spec['tick'][0]
                out.append(tick_recency(label, s, now, t))
            continue
        if spec['kind'] == 'service':
            severity = OK if active in ('active', 'activating') else CRITICAL
            out.append(check(name, severity, 'ActiveState=%s SubState=%s Result=%s' % (active, s.get('SubState'), result)))
        else:
            failed = active == 'failed' or (result not in ('', 'success') and active != 'active')
            severity = (CRITICAL if spec.get('critical_failed') else WARN) if failed else OK
            out.append(check(name, severity, 'ActiveState=%s Result=%s' % (active, result), failed=failed))
        restarts = int(s.get('NRestarts') or 0)
        if restarts:
            if clean_exit(s):
                out.append(check('restarts:' + unit, OK, 'NRestarts=%d, last run exited cleanly (exit 0): not counted' % restarts, value=restarts))
            else:
                out.append(graded('restarts:' + unit, restarts, t['restarts_warn'], t['restarts_critical'],
                                  'NRestarts=%d after non-zero exit (Result=%s ExecMainStatus=%s)' % (restarts, result, s.get('ExecMainStatus'))))
    return out


def clean_exit(s):
    """True only when systemd positively reports the last run ended with exit 0.

    Restart=always units (discovery's daily --seconds 86400 exit) accumulate NRestarts
    with exit 0; those are expected. Anything unknown or non-zero counts as a failure.
    """
    status = (s.get('ExecMainStatus') or '').strip()
    return (status == '0' and (s.get('Result') or '') in ('', 'success')
            and s.get('SubState') not in ('auto-restart', 'failed'))


def tick_recency(label, show_result, now, t):
    try:
        last = parse_systemd_time(show_result.get('LastTriggerUSec'))
        entered = parse_systemd_time(show_result.get('ActiveEnterTimestamp'))
    except ValueError as exc:
        return check(label, WARN, 'unparseable systemd timestamp: %s' % exc)
    reference = last if last is not None else entered
    if reference is None:
        return check(label, WARN, 'timer has no trigger or activation time')
    age = max(0.0, now - reference)
    return graded(label, int(age), t[label + '_warn'], t[label + '_critical'],
                  'last trigger %ds ago%s' % (age, '' if last is not None else ' (never triggered; since activation)'))


def check_discovery(path, now, t):
    with closing(ro(path)) as c:
        frame = c.execute('SELECT max(at) FROM completions').fetchone()[0]
        event = c.execute('SELECT max(received_at) FROM raw_events').fetchone()[0]
    out = []
    if frame is None:
        out.append(check('discovery_frames', CRITICAL, 'no discovery completions recorded'))
    else:
        out.append(graded('discovery_frames', int(now - frame), t['discovery_frame_warn'], t['discovery_frame_critical'],
                          'latest listener completion %ds ago' % (now - frame)))
    if event is None:
        out.append(check('discovery_events', WARN, 'no discovery events recorded yet'))
    else:
        out.append(graded('discovery_events', int(now - event), t['discovery_event_warn'], t['discovery_event_critical'],
                          'latest migration event %ds ago' % (now - event)))
    return out


def read_state(ledger):
    with closing(ro(ledger)) as c:
        row = c.execute('SELECT payload FROM state WHERE id=1').fetchone()
        events = c.execute('SELECT count(*) FROM events').fetchone()[0]
        fills = []
        for (payload,) in c.execute("SELECT payload FROM outcomes ORDER BY seq"):
            o = json.loads(payload)
            if o.get('type') == 'fill':
                fills.append(o)
    return (json.loads(row[0]) if row else None), events, fills


def check_ledger(ledger, now, t, cfg):
    state, events, fills = read_state(ledger)
    if state is None:
        if events:
            return [check('ledger', CRITICAL, 'checkpoint missing but events exist; recovery required')]
        return [check('ledger', OK, 'empty ledger, no checkpoint yet')]
    out = []
    positions = state.get('positions', {})
    n = len(positions)
    blocked = {m: p.get('exit_blocked') for m, p in positions.items() if p.get('exit_blocked')}
    mode = state.get('mode')
    if mode == 'EXIT_ONLY' or blocked:
        out.append(check('exits', CRITICAL if blocked else WARN,
                         'mode=%s; %d position(s) with unresolved exit: %s' % (mode, len(blocked), sorted(set(blocked.values()))),
                         positions=sorted(blocked), open_positions=n))
    elif mode == 'ENTRY_PAUSED':
        out.append(check('mode', WARN, 'ENTRY_PAUSED; no new entries until resumed', open_positions=n))
    elif mode != 'RUNNING':
        out.append(check('mode', CRITICAL, 'unexpected mode %r' % (mode,), open_positions=n))
    else:
        out.append(check('mode', OK, 'RUNNING, %d open position(s)' % n, open_positions=n))
    for mint, p in positions.items():
        age = now - float(p.get('mark_at', 0))
        out.append(graded('mark_age', int(age), t['mark_age_warn'], t['mark_age_critical'],
                          'position %s last mark %ds ago (price TTL %ss; marks are only refreshed by held passes)'
                          % (mint[:8], age, cfg.get('price_ttl_seconds', 'unknown')), mint=mint))
        held = now - float(p.get('opened_at', now))
        if held > t['max_hold_seconds']:
            out.append(check('max_hold', CRITICAL, 'position %s held %ds, over max hold %ds' % (mint[:8], held, t['max_hold_seconds']), mint=mint))
    return out


def check_evidence(evidence, now, t):
    out = []
    with closing(ro(evidence)) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'paper_observation_passes' in tables:
            null_ids = [r[0] for r in c.execute('SELECT id FROM paper_observation_passes WHERE outcome_hash IS NULL LIMIT 20')]
            out.append(check('observation_passes', CRITICAL if null_ids else OK,
                             'NULL-outcome passes latch the global terminal gate: %s' % null_ids if null_ids else 'no unresolved passes',
                             null_pass_ids=null_ids))
        if 'paper_monitoring_budget' not in tables:
            out.append(check('monitoring_budget', WARN, 'monitoring budget not provisioned'))
            return out
        cap, window, blocked = c.execute('SELECT cap,window_seconds,blocked FROM paper_monitoring_budget WHERE id=1').fetchone()
        used = c.execute('SELECT count(*) FROM paper_monitoring_reservations WHERE at>?', (now - window,)).fetchone()[0]
        pending = c.execute('SELECT count(*) FROM paper_monitoring_reservations r LEFT JOIN paper_monitoring_outcomes o '
                            'ON o.reservation_id=r.id WHERE o.reservation_id IS NULL').fetchone()[0]
    effective = max(cap, t['monitoring_cap'])
    headroom = 1 - used / effective
    severity = CRITICAL if headroom < t['monitoring_headroom_critical'] else WARN if headroom < t['monitoring_headroom_warn'] else OK
    out.append(check('monitoring_budget', severity, '%d/%d requests in last %ds (table cap %d)' % (used, effective, window, cap),
                     used=used, cap=effective, headroom=round(headroom, 4)))
    if blocked:
        out.append(check('monitoring_latch', CRITICAL, 'monitoring blocked: %s' % blocked))
    if pending:
        out.append(check('monitoring_pending', WARN, '%d monitoring reservation(s) without an outcome' % pending, pending=pending))
    return out


def check_pacing(path, now, t):
    out = []
    with closing(ro(path)) as c:
        rows = c.execute('SELECT provider,next_at,blocked_until,high_water,pending FROM state').fetchall()
        waiters = c.execute('SELECT count(*) FROM waiters WHERE expires>?', (now,)).fetchone()[0]
    for provider, next_at, blocked_until, high_water, pending in rows:
        if blocked_until > now:
            out.append(check('pacing:' + provider, CRITICAL, 'blocked for another %ds' % (blocked_until - now)))
        elif pending and now - next_at > t['pacing_pending_stale']:
            out.append(check('pacing:' + provider, CRITICAL, 'slot pending %ds, likely orphaned by a killed process' % (now - next_at)))
        else:
            out.append(check('pacing:' + provider, OK, 'clear (waiters=%d)' % waiters))
    return out or [check('pacing', CRITICAL, 'no provider pacing state')]


def check_disk(path, t, usage=shutil.disk_usage):
    u = usage(path)
    fraction = u.free / u.total
    severity = CRITICAL if fraction < t['disk_free_fraction_critical'] or u.free < t['disk_free_bytes_critical'] \
        else WARN if fraction < t['disk_free_fraction_warn'] else OK
    return [check('disk', severity, '%.1f%% free (%d MiB)' % (fraction * 100, u.free >> 20), free_bytes=u.free)]


def check_backup(root, now, t):
    root = Path(root)
    newest = None
    for child in root.iterdir() if root.is_dir() else ():
        if child.is_symlink() or not child.is_dir():
            continue
        stamp = child.stat().st_mtime
        manifest = child / 'manifest.json'
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text())
                created = datetime.datetime.fromisoformat(data.get('created_utc') or data['created_at'])
                stamp = (created if created.tzinfo else created.replace(tzinfo=datetime.timezone.utc)).timestamp()
            except (ValueError, TypeError, KeyError, OSError):
                pass
        newest = stamp if newest is None else max(newest, stamp)
    if newest is None:
        return [check('backup', CRITICAL, 'no backup found under %s' % root)]
    return [graded('backup', int(now - newest), t['backup_age_warn'], t['backup_age_critical'], 'newest backup %dh old' % ((now - newest) // 3600))]


def build_report(args, runner=default_runner, clock=time.time, usage=shutil.disk_usage):
    try:
        return _build_report(args, runner, clock, usage)
    except sqlite3.Error as exc:
        return {'kind': 'desk_healthcheck_v1', 'ts': int(clock()), 'status': CRITICAL, 'read_only': True,
                'execution_status': 'EXECUTION_UNVERIFIED',
                'checks': [check('healthcheck', CRITICAL, 'database error: %s: %s' % (type(exc).__name__, str(exc)[:160]))]}


def _build_report(args, runner, clock, usage):
    now = clock()
    t = dict(DEFAULTS)
    for item in args.threshold:
        key, _, value = item.partition('=')
        if key not in t:
            raise ValueError('unknown threshold %r' % key)
        t[key] = float(value) if '.' in value else int(value)
    root = Path(args.root)
    cfg = json.loads(Path(args.config).read_text()) if args.config else {}
    if 'max_hold_seconds' in cfg and 'max_hold_seconds' not in {i.partition('=')[0] for i in args.threshold}:
        t['max_hold_seconds'] = int(cfg['max_hold_seconds'])
    checks = []
    if not args.no_systemd:
        checks += guarded('units', check_units, runner, now, t)
    checks += guarded('discovery', check_discovery, args.discovery_db, now, t)
    checks += guarded('ledger', check_ledger, root / 'paper-ledger.sqlite', now, t, cfg)
    checks += guarded('evidence', check_evidence, root / 'evidence.sqlite', now, t)
    checks += guarded('pacing', check_pacing, args.pacing_db, now, t)
    checks += guarded('disk', check_disk, root, t, usage)
    if args.backup_root:
        checks += guarded('backup', check_backup, args.backup_root, now, t)
    escalate_held_failure(checks)
    status = max((c['severity'] for c in checks), key=RANK.get, default=OK)
    return {'kind': 'desk_healthcheck_v1', 'ts': int(now), 'status': status, 'read_only': True,
            'execution_status': 'EXECUTION_UNVERIFIED', 'checks': checks}


def escalate_held_failure(checks):
    """A failing held pass is only urgent when a position is open (no sell can happen)."""
    if any(c.get('open_positions') for c in checks):
        return
    for c in checks:
        if c['check'] == 'unit:desk-paper-held-cycle.service' and c.get('failed'):
            c['severity'] = WARN


def main(argv=None, runner=default_runner, clock=time.time, usage=shutil.disk_usage):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True, help='fresh-start store root')
    p.add_argument('--discovery-db', required=True)
    p.add_argument('--pacing-db', required=True)
    p.add_argument('--backup-root')
    p.add_argument('--config', help='experiment config (price TTL, max hold)')
    p.add_argument('--out', help='also write the report here (atomic, 0600)')
    p.add_argument('--no-systemd', action='store_true')
    p.add_argument('--threshold', action='append', default=[], metavar='NAME=VALUE')
    args = p.parse_args(argv)
    try:
        report = build_report(args, runner, clock, usage)
    except (OSError, ValueError, sqlite3.Error) as exc:
        report = {'kind': 'desk_healthcheck_v1', 'ts': int(clock()), 'status': CRITICAL,
                  'checks': [check('healthcheck', CRITICAL, 'could not run: %s' % str(exc)[:200])]}
    line = json.dumps(report, sort_keys=True, separators=(',', ':'))
    print(line)
    if args.out:
        out = Path(args.out)
        tmp = out.with_name('.' + out.name + '.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(line + '\n')
        os.replace(tmp, out)
    return 2 if report['status'] == CRITICAL else 0


if __name__ == '__main__':
    sys.exit(main())
