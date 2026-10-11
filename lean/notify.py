"""Telegram trade notifier for the lean paper desk: a SEPARATE process that only READS the trading store.

    python -m lean.notify --db /var/lib/solana-desk-lean/lean.sqlite --config /etc/solana-paper/lean-notify.json \
        --credentials $CREDENTIALS_DIRECTORY --state-dir /var/lib/solana-desk-lean-notify
    python -m lean.notify --discover-chat --credentials DIR      # setup: print the chat ids that messaged the bot

Separation from trading:
  * The lean store is opened READ-ONLY on every poll (``mode=ro``, or ``immutable=1`` while no ``-wal`` exists, plus
    ``PRAGMA query_only``); nothing here can write it. The notifier keeps its own small state file in ``--state-dir``
    (cursors, outbox, quiet-hours digest, name cache), written atomically (mkstemp + fsync + rename).
  * A Telegram failure only ever delays a message: sends are retried with exponential backoff (a 429 waits exactly its
    ``retry_after``) from a bounded outbox; on overflow the oldest messages are dropped and a "N messages dropped" notice
    is sent when Telegram is reachable again.

Tailing: new ``fills`` rows (by id) and ``events`` rows of kind ``halt`` / ``halt_cleared`` (by id). The FIRST start sets
both cursors to the current maximum, so history is never replayed. The cursors and the messages they produced are
persisted in ONE atomic write before anything is sent, and the state is saved again after every successful send, so a
restart never loses a message and a crash can repeat at most the one message that was in flight.

Secrets: the bot token lives only in the request URL. It never reaches a log line, an exception message or a chained
exception (errors carry a stable code; any URL in error text is replaced by ``[URL]``; the logger has a redacting
filter as a second line of defence). The Helius key (token names) stays inside ``lean.providers``.

Units: the store holds integer lamports and raw token units (lean/store.py, lean/paper.py); conversion happens here only
for display. PnL figures come straight from the fills (``realized_lamports``, fees included) and the ``closed``
position_state row (``trade_pnl_lamports``), exactly what ``Store.realized()`` sums.
"""
import argparse
import datetime
import html
import json
import logging
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request
from zoneinfo import ZoneInfo

from lean import paper
from lean.store import redact as _redact_credentials

log = logging.getLogger('lean.notify')

LAMPORTS = Decimal(paper.LAMPORTS)
STATE_FILE = 'notify-state.json'
STATE_VERSION = 1
MAX_TEXT = 3800                     # Telegram allows 4096 characters per message; keep a margin
MAX_NAMES = 5000
HALT_BATCH = 100
MAX_RESPONSE = 1024 * 1024
BACKOFF_BASE, BACKOFF_CAP, MAX_RETRY_AFTER = 1.0, 300.0, 3600.0

CONFIG_KEYS = {
    'mode': 'PAPER',                # the tag every message starts with: PAPER or LIVE
    'timezone': 'Asia/Seoul',
    'daily_summary_at': '09:00',    # local time; null disables the daily summary
    'quiet_hours': None,            # e.g. ["00:00", "08:00"] local: BUY/SELL batched into one digest at the end
    'allowed_chat_ids': None,       # who may use /status /today /pnl (default: the telegram.json chat_id)
    'commands': True,
    'poll_interval_s': 5,           # store poll period == getUpdates long-poll timeout
    'max_outbox': 200,
    'max_sends_per_cycle': 10,
    'pool_fee_bps': 25,             # same as lean.json: used to turn the stored vault reads into net marks
    'name_lookup': True,            # one Helius DAS getAsset per mint (cached) when the store has no name
    'sol_usd_max_age_s': 3600,
}

REASONS = {'STOP': '손절 (STOP)', 'TRAILING_STOP': '트레일링 스탑', 'TIME_STOP': '시간 손절 (TIME)',
           'MAX_HOLD': '최대 보유시간 (MAX HOLD)', 'LIQUIDATE': '일일 손실 한도 청산', 'DANGER': '위험 감지 청산 (rug write-off)'}
_BASE58 = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')
_TOKEN_SHAPE = re.compile(r'^[0-9]{3,20}:[A-Za-z0-9_-]{20,100}$')
_TOKEN_IN_TEXT = re.compile(r'(?i)bot[0-9]{3,20}:[A-Za-z0-9_-]+')
_BARE_TOKEN = re.compile(r'\b[0-9]{6,20}:[A-Za-z0-9_-]{30,}')
_URL_IN_TEXT = re.compile(r'(?i)\b(?:https?|ftp)://\S+')
_CHAT_ID = re.compile(r'^-?[0-9]{1,20}$|^@[A-Za-z0-9_]{5,64}$')
_HHMM = re.compile(r'^([01][0-9]|2[0-3]):([0-5][0-9])$')


class NotifyError(ValueError):
    """Configuration / credential problem. The message is a stable code, never file contents."""


class StoreUnavailable(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------ redaction
def redact(text, token=None):
    """No bot token, no URL, no credential-shaped value in anything we print or log."""
    text = str(text)
    if token:
        text = text.replace(token, '[REDACTED]')
    text = _TOKEN_IN_TEXT.sub('bot[REDACTED]', text)
    text = _BARE_TOKEN.sub('[REDACTED]', text)
    text = _URL_IN_TEXT.sub('[URL]', text)
    return _redact_credentials(text)[:500]


class _RedactingFilter(logging.Filter):
    def __init__(self, token):
        super().__init__()
        self.token = token

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg, record.args = redact(message, self.token), None
        return True


# ------------------------------------------------------------------------------------------------ config / creds
def load_config(path):
    """Strict notifier config: unknown keys refused; values validated."""
    raw = {} if path is None else json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise NotifyError('CONFIG_NOT_OBJECT')
    unknown = set(raw) - set(CONFIG_KEYS) - {'_comment'}
    if unknown:
        raise NotifyError('CONFIG_UNKNOWN_KEYS: %s' % sorted(unknown))
    cfg = {k: raw.get(k, v) for k, v in CONFIG_KEYS.items()}
    if cfg['mode'] not in ('PAPER', 'LIVE'):
        raise NotifyError('CONFIG_MODE')
    try:
        ZoneInfo(cfg['timezone'])
    except Exception:
        raise NotifyError('CONFIG_TIMEZONE') from None
    if cfg['daily_summary_at'] is not None:
        _hhmm(cfg['daily_summary_at'])
    if cfg['quiet_hours'] is not None:
        if not isinstance(cfg['quiet_hours'], list) or len(cfg['quiet_hours']) != 2:
            raise NotifyError('CONFIG_QUIET_HOURS')
        for value in cfg['quiet_hours']:
            _hhmm(value)
    if cfg['allowed_chat_ids'] is not None:
        if not isinstance(cfg['allowed_chat_ids'], list) or not all(_valid_chat(c) for c in cfg['allowed_chat_ids']):
            raise NotifyError('CONFIG_ALLOWED_CHAT_IDS')
    for key, low, high in (('poll_interval_s', 0, 60), ('max_outbox', 1, 10000), ('max_sends_per_cycle', 1, 100),
                           ('pool_fee_bps', 0, 1000), ('sol_usd_max_age_s', 1, 86400)):
        if isinstance(cfg[key], bool) or not isinstance(cfg[key], (int, float)) or not low <= cfg[key] <= high:
            raise NotifyError('CONFIG_%s' % key.upper())
    for key in ('commands', 'name_lookup'):
        if not isinstance(cfg[key], bool):
            raise NotifyError('CONFIG_%s' % key.upper())
    return cfg


def _hhmm(value):
    match = _HHMM.match(value) if isinstance(value, str) else None
    if match is None:
        raise NotifyError('CONFIG_TIME_FORMAT')
    return int(match.group(1)), int(match.group(2))


def _valid_chat(value):
    return (type(value) is int) or (isinstance(value, str) and _CHAT_ID.match(value) is not None)


def _private_file(path, limit=16384):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o037 or info.st_size > limit:
        raise ValueError
    return Path(path).read_text()


def load_telegram(path, *, need_chat=True):
    """``telegram.json`` = ``{"bot_token": "...", "chat_id": "..."}``: a private regular file (0400/0440/0600)."""
    try:
        value = json.loads(_private_file(path, 4096))
        if not isinstance(value, dict) or set(value) - {'bot_token', 'chat_id', '_comment'}:
            raise ValueError
        token = value.get('bot_token')
        token = token.strip() if isinstance(token, str) else token
        if not isinstance(token, str) or _TOKEN_SHAPE.match(token) is None:
            raise ValueError
        chat = value.get('chat_id')
        if chat is not None and not _valid_chat(chat):
            raise ValueError
        if need_chat and chat is None:
            raise ValueError
        return {'bot_token': token, 'chat_id': None if chat is None else str(chat)}
    except (OSError, ValueError, TypeError):
        raise NotifyError('TELEGRAM_CREDENTIAL_INVALID') from None


# ------------------------------------------------------------------------------------------------ Telegram
class TelegramError(Exception):
    """A failed Bot API call. Only a code (and a redacted description): never the URL or the token."""

    def __init__(self, code, *, retry_after=None, permanent=False, description=None):
        super().__init__(code)
        self.code, self.retry_after, self.permanent = code, retry_after, permanent
        self.description = description


class Telegram:
    """Plain HTTPS Bot API over urllib. ``opener(request, timeout)`` is injectable (tests never touch the network)."""
    API = 'https://api.telegram.org'

    def __init__(self, token, *, opener=None, timeout=15.0):
        from lean.providers import default_opener
        self._token = token
        self.opener = opener or default_opener
        self.timeout = timeout

    def __repr__(self):
        return 'Telegram(token=<redacted>)'

    def call(self, method, params, *, timeout=None):
        request = Request('%s/bot%s/%s' % (self.API, self._token, method), data=json.dumps(params).encode(), method='POST',
                          headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
        status, raw, header_retry, failure = None, b'', None, None
        response = None
        try:
            response = self.opener(request, timeout or self.timeout)
            status = getattr(response, 'status', None)
            raw = response.read(MAX_RESPONSE)
            headers = getattr(response, 'headers', None) or {}
            header_retry = headers.get('Retry-After') if hasattr(headers, 'get') else None
        except HTTPError as error:
            status = error.code
            try:
                raw = error.read(MAX_RESPONSE)
            except Exception:
                raw = b''
            header_retry = error.headers.get('Retry-After') if error.headers is not None else None
        except Exception as error:                  # URLError, timeout, reset...: the text may hold the URL: code only
            failure = TelegramError('NETWORK_%s' % type(error).__name__)
        finally:
            close = getattr(response, 'close', None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        if failure is not None:                     # raised outside the except block: no chained exception at all
            raise failure
        try:
            data = json.loads(raw.decode('utf-8')) if isinstance(raw, (bytes, bytearray)) and raw else {}
        except (ValueError, UnicodeError):
            data = {}
        data = data if isinstance(data, dict) else {}
        if status == 200 and data.get('ok') is True:
            return data.get('result')
        description = redact(data.get('description', ''), self._token) if data.get('description') else None
        code = data.get('error_code') if type(data.get('error_code')) is int else status
        if code == 429 or status == 429:
            params = data.get('parameters') if isinstance(data.get('parameters'), dict) else {}
            retry = params.get('retry_after')
            if type(retry) is not int and isinstance(header_retry, str) and header_retry.isdecimal():
                retry = int(header_retry)
            retry = float(retry) if type(retry) in (int, float) and retry >= 0 else 5.0
            raise TelegramError('HTTP_429', retry_after=min(retry, MAX_RETRY_AFTER), description=description)
        if status == 200:
            raise TelegramError('API_NOT_OK', description=description)
        return_code = 'HTTP_%s' % (status if type(status) is int else 'NONE')
        raise TelegramError(return_code, permanent=status == 400, description=description)

    def send(self, chat_id, text, *, html_mode=True):
        params = {'chat_id': chat_id, 'text': text, 'disable_web_page_preview': True}
        if html_mode:
            params['parse_mode'] = 'HTML'
        return self.call('sendMessage', params)

    def get_updates(self, offset=None, timeout=0):
        params = {'timeout': int(timeout), 'allowed_updates': ['message']}
        if offset is not None:
            params['offset'] = int(offset)
        result = self.call('getUpdates', params, timeout=float(timeout) + 15.0)
        return result if isinstance(result, list) else []


# ------------------------------------------------------------------------------------------------ the store (read-only)
def open_store(path):
    """A READ-ONLY connection to the lean store. ``mode=ro`` while the trader's ``-wal`` exists (reading through its
    -shm), ``immutable=1`` when the store is quiet (no -wal: a plain ro open would create -wal/-shm beside it).
    ``query_only`` refuses every write even if the open mode were wrong."""
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            raise StoreUnavailable('STORE_MISSING')
        resolved = path.resolve()
        flag = 'mode=ro' if Path(str(resolved) + '-wal').exists() else 'immutable=1'
        connection = sqlite3.connect(resolved.as_uri() + '?' + flag, uri=True, timeout=5)
        connection.execute('PRAGMA query_only=1')
        connection.execute('SELECT 1 FROM fills LIMIT 1').fetchall()
        return connection
    except sqlite3.Error as error:
        raise StoreUnavailable('STORE_UNREADABLE:%s' % type(error).__name__) from None
    except OSError as error:
        raise StoreUnavailable('STORE_UNREADABLE:%s' % type(error).__name__) from None


FILL_COLUMNS = ('id', 'ts', 'mint', 'side', 'qty_raw', 'sol_lamports', 'fee_lamports', 'slippage_bps', 'decimals',
                'cost_sold_lamports', 'realized_lamports', 'quote_ref', 'label', 'candidate_id', 'cash_after', 'qty_after',
                'cost_after', 'code_version', 'strategy_version')


class StoreView:
    """Every read the notifier makes, on the real lean.sqlite schema (lean/store.py)."""

    def __init__(self, connection):
        self.c = connection
        self._replay = None

    def close(self):
        self.c.close()

    def initial_cash(self):
        return int(self.c.execute("SELECT value FROM meta WHERE key='initial_cash_lamports'").fetchone()[0])

    def max_ids(self):
        fill = self.c.execute('SELECT COALESCE(MAX(id),0) FROM fills').fetchone()[0]
        event = self.c.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
        return fill, event

    def _fills(self, where='', args=(), limit=None):
        sql = 'SELECT %s FROM fills %s ORDER BY id' % (','.join(FILL_COLUMNS), where)
        if limit is not None:
            sql += ' LIMIT %d' % int(limit)
        return [dict(zip(FILL_COLUMNS, r)) for r in self.c.execute(sql, args).fetchall()]

    def fills_after(self, fill_id, limit=500):
        return self._fills('WHERE id>?', (fill_id,), limit)

    def fills_between(self, start_ts, end_ts):
        return self._fills('WHERE ts>=? AND ts<?', (start_ts, end_ts))

    def fill(self, fill_id):
        rows = self._fills('WHERE id=?', (fill_id,))
        return rows[0] if rows else None

    def mint_fills(self, mint, first_id, last_id):
        return self._fills('WHERE mint=? AND id>=? AND id<=?', (mint, first_id, last_id))

    def halt_events_after(self, event_id, limit=None):
        limit = HALT_BATCH if limit is None else limit
        rows = self.c.execute("SELECT id,ts,kind,payload FROM events WHERE id>? AND kind IN ('halt','halt_cleared') "
                              'ORDER BY id LIMIT ?', (event_id, limit)).fetchall()
        return [{'id': i, 'ts': ts, 'kind': k, 'payload': _loads(p)} for i, ts, k, p in rows]

    def state_for_fill(self, fill_id):
        row = self.c.execute('SELECT mint,open_fill_id,event,state,strategy_version FROM position_state WHERE fill_id=? '
                             'ORDER BY id DESC LIMIT 1', (fill_id,)).fetchone()
        if row is None:
            return None
        return {'mint': row[0], 'open_fill_id': row[1], 'event': row[2], 'state': _loads(row[3]), 'strategy_version': row[4]}

    def open_states(self):
        rows = self.c.execute('SELECT mint,open_fill_id,event,state FROM position_state WHERE id IN '
                              '(SELECT MAX(id) FROM position_state GROUP BY mint)').fetchall()
        return {m: {'open_fill_id': o, 'state': _loads(s)} for m, o, e, s in rows if e != 'closed'}

    def closed(self, start_ts=None, end_ts=None):
        """Closed trades: [{'mint', 'open_fill_id', 'fill_id', 'ts', 'pnl', 'cost', 'reason', 'strategy_version'}]."""
        where, args = "WHERE event='closed'", []
        if start_ts is not None:
            where += ' AND ts>=? AND ts<?'
            args += [start_ts, end_ts]
        rows = self.c.execute('SELECT mint,open_fill_id,fill_id,ts,state,strategy_version FROM position_state %s ORDER BY id'
                              % where, args).fetchall()
        out = []
        for mint, open_id, fill_id, ts, text, version in rows:
            state = _loads(text)
            fills = self.mint_fills(mint, open_id, fill_id)
            cost = sum(f['sol_lamports'] + f['fee_lamports'] for f in fills if f['side'] == 'buy')
            pnl = state.get('trade_pnl_lamports')
            pnl = int(pnl) if type(pnl) is int else sum(f['realized_lamports'] for f in fills if f['side'] == 'sell')
            out.append({'mint': mint, 'open_fill_id': open_id, 'fill_id': fill_id, 'ts': ts, 'pnl': pnl, 'cost': cost,
                        'reason': state.get('reason'), 'strategy_version': version})
        return out

    def replay(self):
        """(positions, cash, realized) by the ONE accounting implementation (lean.paper.apply_fill), like Store does."""
        if self._replay is None:
            positions, cash, realized = {}, self.initial_cash(), 0
            for row in self._fills():
                fill = paper.Fill(ts=row['ts'], mint=row['mint'], side=row['side'], qty_raw=row['qty_raw'],
                                  sol_lamports=row['sol_lamports'], fee_lamports=row['fee_lamports'],
                                  slippage_bps=row['slippage_bps'], decimals=row['decimals'],
                                  cost_sold_lamports=row['cost_sold_lamports'], realized_lamports=row['realized_lamports'],
                                  quote_ref=row['quote_ref'], label=row['label'])
                positions, cash = paper.apply_fill(positions, cash, fill)
                realized += fill.realized_lamports
            self._replay = (positions, cash, realized)
        return self._replay

    def screen_features(self, mint):
        """Features of the mint's latest PASS screen (sol_usd, supply_raw, decimals, market cap inputs)."""
        row = self.c.execute("SELECT features FROM decisions WHERE mint=? AND kind='screen' AND action='PASS' "
                             'ORDER BY id DESC LIMIT 1', (mint,)).fetchone()
        return _loads(row[0]) if row else {}

    def sol_usd_near(self, ts, max_age):
        """The SOL/USD the trader used most recently at or before ``ts`` (screen features), if not older than max_age."""
        for stamp, text in self.c.execute("SELECT ts,features FROM decisions WHERE kind='screen' AND ts<=? "
                                          'ORDER BY id DESC LIMIT 200', (ts + 1,)):
            value = _dec(_loads(text).get('sol_usd'))
            if value is not None and value > 0:
                return value if ts - stamp <= max_age else None
        return None

    def name_from_store(self, mint):
        """A name/symbol from any stored payload (candidate meta, screen features), when the trader recorded one."""
        sources = [r[0] for r in self.c.execute('SELECT meta FROM candidates WHERE mint=?', (mint,))]
        sources += [r[0] for r in self.c.execute("SELECT features FROM decisions WHERE mint=? AND kind='screen' "
                                                 'ORDER BY id DESC LIMIT 3', (mint,))]
        for text in sources:
            data = _loads(text)
            symbol, name = data.get('symbol') or data.get('token_symbol'), data.get('name') or data.get('token_name')
            if isinstance(symbol, str) and symbol.strip() or isinstance(name, str) and name.strip():
                return {'symbol': symbol.strip()[:64] if isinstance(symbol, str) and symbol.strip() else None,
                        'name': name.strip()[:64] if isinstance(name, str) and name.strip() else None}
        return None

    def latest_marks(self, positions, states, *, pool_fee_bps, scan=500):
        """{mint: (net mark lamports, read ts)} from the newest stored ``marks`` observation covering each mint: the
        vault balances the trader read, valued like lean.adapters.mark (constant product, pool fee, fixed fee)."""
        from lean import adapters as A
        wanted, out = {m for m in positions if m in states}, {}
        if not wanted:
            return out
        rows = self.c.execute("SELECT id,ts,meta FROM observations WHERE kind='marks' ORDER BY id DESC LIMIT ?", (scan,))
        for obs_id, ts, meta_text in rows.fetchall():
            mints = _loads(meta_text).get('mints')
            if not isinstance(mints, list) or not wanted & set(mints):
                continue
            raw = self.c.execute('SELECT raw FROM observations WHERE id=?', (obs_id,)).fetchone()[0]
            try:
                values = json.loads(bytes(raw).decode())['result']['value']
            except (ValueError, KeyError, TypeError, UnicodeError):
                continue
            for i, mint in enumerate(mints):
                if mint not in wanted:
                    continue
                position = positions[mint]
                fee = self._last_fee(mint)
                try:
                    base, quote = A.vault_amount(values[2 * i]), A.vault_amount(values[2 * i + 1])
                    net = A.mark(position.qty_raw, base, quote, pool_fee_bps=pool_fee_bps,
                                 pcfg=paper.PaperConfig(fee_lamports=fee, slippage_bps=0))
                except (A.AdapterError, IndexError, ValueError):
                    continue
                out[mint] = (int(net * LAMPORTS), ts)
                wanted.discard(mint)
            if not wanted:
                break
        return out

    def _last_fee(self, mint):
        row = self.c.execute('SELECT fee_lamports FROM fills WHERE mint=? ORDER BY id DESC LIMIT 1', (mint,)).fetchone()
        return int(row[0]) if row else 0


def _loads(text):
    try:
        value = json.loads(text) if isinstance(text, (str, bytes)) else text
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _dec(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    return number if number.is_finite() else None


# ------------------------------------------------------------------------------------------------ formatting
def esc(value):
    return html.escape(str(value), quote=True)


def short_ca(mint):
    mint = str(mint)
    return mint if len(mint) <= 10 else '%s…%s' % (mint[:4], mint[-4:])


def sol_of(lamports):
    return Decimal(int(lamports)) / LAMPORTS


def fmt_amount(value, places=6, *, sign=False):
    """Fixed decimals, trailing zeros trimmed to at least 2 places: 0.2 -> '0.20', 0.0123456 -> '0.012346'."""
    value = Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = format(abs(value), 'f')
    if '.' in text:
        whole, frac = text.split('.')
        frac = frac.rstrip('0').ljust(2, '0')
        text = whole + '.' + frac
    prefix = '-' if value < 0 else ('+' if sign else '')
    return prefix + text


def fmt_sol(value, *, sign=False):
    return fmt_amount(value, 6, sign=sign) + ' SOL'


def fmt_price(value, digits=4):
    """``digits`` significant digits, never scientific notation: 4.1234e-7 -> '0.0000004123'."""
    value = Decimal(value)
    if value == 0:
        return '0'
    places = max(0, digits - 1 - value.adjusted())
    return format(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), 'f')


def fmt_usd_big(value):
    value = Decimal(value)
    for limit, suffix in ((Decimal(10) ** 9, 'B'), (Decimal(10) ** 6, 'M'), (Decimal(10) ** 3, 'K')):
        if abs(value) >= limit:
            return '$%s%s' % (format((value / limit).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), 'f'), suffix)
    return '$%s' % format(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), 'f')


def fmt_pct(fraction, *, sign=True):
    pct = (Decimal(fraction) * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return ('+' if sign and pct >= 0 else '') + format(pct, 'f') + '%'


def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return '%d일 %d시간' % (days, hours)
    if hours:
        return '%d시간 %d분' % (hours, minutes)
    if minutes:
        return '%d분 %d초' % (minutes, secs) if minutes < 10 and secs else '%d분' % minutes
    return '%d초' % secs


def price_sol(sol_lamports, qty_raw, decimals):
    """SOL per whole token of a fill (after the slippage haircut, before the fee): ``paper.Fill.price_sol_per_token``."""
    return Decimal(sol_lamports) / LAMPORTS / (Decimal(qty_raw) / Decimal(10) ** int(decimals))


def exit_reason_label(code, stage=None):
    if code == 'TAKE_PROFIT':
        return '익절 TP%d' % stage if isinstance(stage, int) and stage > 0 else '익절 (TP)'
    return REASONS.get(code, str(code or '알 수 없음'))


def links(mint):
    return ('<a href="https://dexscreener.com/solana/%s">DexScreener</a> · <a href="https://solscan.io/token/%s">Solscan</a>'
            % (esc(mint), esc(mint)))


def split_text(text, limit=MAX_TEXT):
    """Split at line boundaries (every line is self-contained HTML), so no tag is cut."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ''
    for line in text.split('\n'):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ''
            parts.append(line[:limit])
            line = line[limit:]
        candidate = line if not current else current + '\n' + line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def strip_tags(text):
    return html.unescape(re.sub(r'<[^>]+>', '', text))


# ------------------------------------------------------------------------------------------------ events -> data
def fill_event(view, row, *, sol_usd_max_age):
    """Structured facts of one fill (exact Decimals from the store's integers). The message is rendered from this."""
    mint = row['mint']
    price = price_sol(row['sol_lamports'], row['qty_raw'], row['decimals'])
    out = {'fill_id': row['id'], 'side': row['side'], 'mint': mint, 'ts': row['ts'], 'price_sol': price,
           'size_sol': sol_of(row['sol_lamports']), 'fee_sol': sol_of(row['fee_lamports']),
           'strategy_version': row['strategy_version']}
    if row['side'] == 'buy':
        features = view.screen_features(mint)
        sol_usd = _dec(features.get('sol_usd'))
        out['sol_usd'] = sol_usd
        out['price_usd'] = price * sol_usd if sol_usd else None
        supply, decimals = _dec(features.get('supply_raw')), features.get('decimals', row['decimals'])
        out['market_cap_usd'] = (supply / Decimal(10) ** int(decimals) * price * sol_usd) if supply and sol_usd else None
        return out
    sol_usd = view.sol_usd_near(row['ts'], sol_usd_max_age)
    out['price_usd'] = price * sol_usd if sol_usd else None
    record = view.state_for_fill(row['id']) or {}
    state = record.get('state') or {}
    full = row['qty_after'] == 0
    out['full'] = full
    reason = state.get('reason') if full else state.get('last_reason')
    stage = state.get('stage')
    if reason == 'TAKE_PROFIT' and isinstance(stage, int):
        stage = stage + 1 if full else stage          # a rung row carries the stage AFTER the fill
    out['reason'], out['reason_label'] = reason, exit_reason_label(reason, stage)
    initial = state.get('initial_qty_raw')
    out['fraction_sold'] = Decimal(row['qty_raw']) / Decimal(int(initial)) if initial else None
    out['remaining_fraction'] = Decimal(row['qty_after']) / Decimal(int(initial)) if initial else None
    out['realized_sol'] = sol_of(row['realized_lamports'])
    out['realized_pct'] = (Decimal(row['realized_lamports']) / Decimal(row['cost_sold_lamports'])
                           if row['cost_sold_lamports'] else None)
    open_id = record.get('open_fill_id')
    opened = view.fill(open_id) if open_id else None
    out['hold_seconds'] = row['ts'] - opened['ts'] if opened else None
    if full and open_id:
        fills = view.mint_fills(mint, open_id, row['id'])
        cost = sum(f['sol_lamports'] + f['fee_lamports'] for f in fills if f['side'] == 'buy')
        pnl = state.get('trade_pnl_lamports')
        pnl = int(pnl) if type(pnl) is int else sum(f['realized_lamports'] for f in fills if f['side'] == 'sell')
        out['trade_pnl_sol'] = sol_of(pnl)
        out['trade_pnl_pct'] = Decimal(pnl) / Decimal(cost) if cost else None
        out['trade_cost_sol'] = sol_of(cost)
    return out


def render_fill(event, label):
    """BUY / SELL message body (without the mode tag). ``label`` is the escaped-later symbol or short CA."""
    mint = event['mint']
    if event['side'] == 'buy':
        lines = ['🟢 <b>매수 %s</b>' % esc(label), '<code>%s</code>' % esc(mint),
                 '진입가: %s SOL%s' % (fmt_price(event['price_sol']),
                                     ' ($%s)' % fmt_price(event['price_usd']) if event.get('price_usd') else ''),
                 '규모: %s (수수료 %s)' % (fmt_sol(event['size_sol']), fmt_amount(event['fee_sol'], 9))]
        if event.get('market_cap_usd'):
            lines.append('시총: %s' % fmt_usd_big(event['market_cap_usd']))
        lines += ['전략: %s' % esc(event['strategy_version']), links(mint)]
        return '\n'.join(lines)
    icon, kind = ('🔴', '전량') if event['full'] else ('🟡', '부분')
    lines = ['%s <b>매도 %s</b> (%s)' % (icon, esc(label), kind), '<code>%s</code>' % esc(mint),
             '사유: %s' % esc(event['reason_label'])]
    if event.get('fraction_sold') is not None:
        lines.append('매도 비율: %s (잔여 %s)' % (fmt_pct(event['fraction_sold'], sign=False),
                                              fmt_pct(event['remaining_fraction'], sign=False)))
    lines.append('청산가: %s SOL%s' % (fmt_price(event['price_sol']),
                                    ' ($%s)' % fmt_price(event['price_usd']) if event.get('price_usd') else ''))
    pct = ' (%s)' % fmt_pct(event['realized_pct']) if event.get('realized_pct') is not None else ''
    lines.append('실현손익: %s%s' % (fmt_sol(event['realized_sol'], sign=True), pct))
    if event.get('hold_seconds') is not None:
        lines.append('보유: %s' % fmt_duration(event['hold_seconds']))
    if event['full'] and event.get('trade_pnl_sol') is not None:
        pct = ', %s' % fmt_pct(event['trade_pnl_pct']) if event.get('trade_pnl_pct') is not None else ''
        lines.append('<b>거래 총손익: %s</b> (수수료 포함%s)' % (fmt_sol(event['trade_pnl_sol'], sign=True), pct))
    lines.append(links(mint))
    return '\n'.join(lines)


def render_halt(event):
    payload = event['payload']
    if event['kind'] == 'halt':
        return ('⛔ <b>거래 중단 (HALT)</b>\n사유: <code>%s</code>\n신규 체결이 모두 멈췄습니다. 확인 후 '
                '<code>python -m lean --clear-halt</code>' % esc(str(payload.get('reason', 'HALTED'))[:300]))
    return '⛔ <b>중단 해제 (clear-halt)</b>\n이전 사유: <code>%s</code>' % esc(str(payload.get('previous') or '-')[:300])


# ------------------------------------------------------------------------------------------------ the notifier
class Notifier:
    def __init__(self, *, db, cfg, telegram, chat_id, state_dir, helius=None, clock=time.time, sleep=time.sleep):
        self.db, self.cfg, self.tg, self.chat_id = Path(db), cfg, telegram, str(chat_id)
        self.state_dir = Path(state_dir)
        self.helius, self.clock, self.sleep = helius, clock, sleep
        self.tz = ZoneInfo(cfg['timezone'])
        allowed = cfg['allowed_chat_ids'] if cfg['allowed_chat_ids'] is not None else [chat_id]
        self.allowed = {str(c) for c in allowed}
        self.tag = '[%s]' % cfg['mode']
        self.state_path = self.state_dir / STATE_FILE
        self.state = self._load_state()
        self.failures = 0
        self.backoff_until = 0.0
        self.updates_backoff_until = 0.0
        self.updates_failures = 0
        self._long_polled = False
        self.dirty = False

    # -- state ----------------------------------------------------------------------------------------------------
    def _load_state(self):
        try:
            data = json.loads(self.state_path.read_text())
            if not isinstance(data, dict) or data.get('version') != STATE_VERSION:
                raise ValueError
            return data
        except FileNotFoundError:
            return {'version': STATE_VERSION, 'fill_cursor': None, 'event_cursor': None, 'update_offset': None,
                    'outbox': [], 'dropped': 0, 'digest': [], 'last_summary_day': None, 'names': {}}
        except (OSError, ValueError):
            # an unreadable state file must not re-send history: refuse to run rather than guess
            raise NotifyError('STATE_FILE_INVALID') from None

    def save(self):
        """Atomic: mkstemp in the state dir, fsync, rename (cursors and outbox move together)."""
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix='.notify.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(self.state, stream, sort_keys=True, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.state_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        self.dirty = False

    # -- outbox ---------------------------------------------------------------------------------------------------
    def enqueue(self, text, chat_id=None):
        """Bounded outbox: on overflow the OLDEST message is dropped and counted (a notice is sent later)."""
        for part in split_text(text):
            self.state['outbox'].append({'chat_id': str(chat_id or self.chat_id), 'text': part})
        limit = int(self.cfg['max_outbox'])
        while len(self.state['outbox']) > limit:
            self.state['outbox'].pop(0)
            self.state['dropped'] += 1
        self.dirty = True

    def tagged(self, body):
        return '%s %s' % (self.tag, body)

    def flush(self):
        """Send what the outbox holds, oldest first, until a failure (then back off). Returns the number sent."""
        now = self.clock()
        if now < self.backoff_until:
            return 0
        sent = 0
        while sent < int(self.cfg['max_sends_per_cycle']):
            if self.state['dropped']:
                item = {'chat_id': self.chat_id, 'text': self.tagged('⚠️ 알림 %d건이 대기열 초과로 누락되었습니다 '
                                                                     '(%d messages dropped)' % (self.state['dropped'],
                                                                                                self.state['dropped'])),
                        'notice': True}
            elif self.state['outbox']:
                item = self.state['outbox'][0]
            else:
                break
            try:
                self.tg.send(item['chat_id'], item['text'] if not item.get('plain') else strip_tags(item['text']),
                             html_mode=not item.get('plain'))
            except TelegramError as error:
                if error.permanent and not item.get('notice'):
                    # Telegram refused this message itself (e.g. an entity it cannot parse): retry once as plain text,
                    # then give up on it (counted as dropped) so one bad message never blocks the queue.
                    if not item.get('plain'):
                        item['plain'] = True
                    else:
                        self.state['outbox'].pop(0)
                        self.state['dropped'] += 1
                    self.dirty = True
                    log.warning('telegram refused a message: %s', error.code)
                    continue
                self.failures += 1
                if error.retry_after is not None:
                    delay = error.retry_after
                else:
                    delay = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (self.failures - 1))
                self.backoff_until = self.clock() + delay
                log.warning('telegram send failed: %s (retry in %.0fs, %d queued)', error.code, delay,
                            len(self.state['outbox']))
                break
            self.failures = 0
            if item.get('notice'):
                self.state['dropped'] = 0
            else:
                self.state['outbox'].pop(0)
            self.save()                     # after EVERY send: a crash can repeat at most the one message in flight
            sent += 1
        return sent

    # -- quiet hours / digest -------------------------------------------------------------------------------------
    def local(self, ts):
        return datetime.datetime.fromtimestamp(ts, self.tz)

    def quiet(self, ts):
        hours = self.cfg['quiet_hours']
        if not hours:
            return False
        start, end = (_hhmm(v) for v in hours)
        if start == end:
            return False
        now = self.local(ts)
        minute = now.hour * 60 + now.minute
        a, b = start[0] * 60 + start[1], end[0] * 60 + end[1]
        return a <= minute < b if a < b else (minute >= a or minute < b)

    def trade_message(self, body, ts):
        """BUY/SELL: batched into the digest during quiet hours, otherwise queued now."""
        if self.quiet(ts):
            self.state['digest'].append(body)
            while len(self.state['digest']) > int(self.cfg['max_outbox']):
                self.state['digest'].pop(0)
                self.state['digest_dropped'] = self.state.get('digest_dropped', 0) + 1
            self.dirty = True
        else:
            self.enqueue(self.tagged(body))

    def flush_digest(self):
        if not self.state['digest'] or self.quiet(self.clock()):
            return False
        bodies, dropped = self.state['digest'], self.state.get('digest_dropped', 0)
        head = '🌙 <b>조용한 시간 요약</b> (%d건%s)' % (len(bodies) + dropped, ', %d건 누락' % dropped if dropped else '')
        self.enqueue(self.tagged(head + '\n\n' + '\n\n'.join(bodies)))
        self.state['digest'], self.state['digest_dropped'] = [], 0
        self.dirty = True
        return True

    # -- names ----------------------------------------------------------------------------------------------------
    def label(self, view, mint):
        """Symbol from the store, else ONE cached Helius DAS getAsset per mint, else the short CA."""
        names = self.state['names']
        if mint not in names:
            found = view.name_from_store(mint)
            if found is None and self.helius is not None and self.cfg['name_lookup'] and _BASE58.match(mint):
                try:
                    found, _raw, _meta = self.helius.get_asset(mint)
                except Exception as error:        # ProviderError carries a code only; never fatal
                    log.info('name lookup failed: %s', getattr(error, 'code', type(error).__name__))
                    found = None
            names[mint] = found if found and (found.get('symbol') or found.get('name')) else None
            while len(names) > MAX_NAMES:
                names.pop(next(iter(names)))
            self.dirty = True
        found = names.get(mint)
        if found:
            return found.get('symbol') or found.get('name')
        return short_ca(mint)

    # -- the store ------------------------------------------------------------------------------------------------
    def poll_store(self):
        """New fills and halt events since the cursors -> messages. Returns the number of messages produced."""
        try:
            view = StoreView(open_store(self.db))
        except StoreUnavailable as error:
            log.warning('store unavailable: %s', error)
            return 0
        produced = 0
        try:
            fill_max, event_max = view.max_ids()
            if self.state['fill_cursor'] is None or self.state['event_cursor'] is None:
                # FIRST start: never replay history
                self.state['fill_cursor'], self.state['event_cursor'] = fill_max, event_max
                self.dirty = True
                log.info('first start: cursors set to fill %d, event %d', fill_max, event_max)
                return 0
            if fill_max < self.state['fill_cursor'] or event_max < self.state['event_cursor']:
                log.warning('store ids moved backwards (new store?): cursors reset to the current maximum')
                self.state['fill_cursor'], self.state['event_cursor'] = fill_max, event_max
                self.dirty = True
                return 0
            halts = view.halt_events_after(self.state['event_cursor'])
            for event in halts:
                self.enqueue(self.tagged(render_halt(event)))        # always immediate, even in quiet hours
                self.state['event_cursor'] = event['id']
                produced += 1
            for row in view.fills_after(self.state['fill_cursor']):
                try:
                    facts = fill_event(view, row, sol_usd_max_age=self.cfg['sol_usd_max_age_s'])
                    body = render_fill(facts, self.label(view, row['mint']))
                except (KeyError, TypeError, ValueError, ArithmeticError) as error:
                    # one unreadable row must not block the feed: a minimal message, and the cursor moves on
                    log.warning('fill %s rendered minimally: %s', row.get('id'), type(error).__name__)
                    body = '%s <b>%s %s</b>\n<code>%s</code>\n(세부 정보를 읽지 못했습니다)' % (
                        '🟢' if row.get('side') == 'buy' else '🔴', '매수' if row.get('side') == 'buy' else '매도',
                        esc(short_ca(row.get('mint'))), esc(row.get('mint')))
                self.trade_message(body, self.clock())
                self.state['fill_cursor'] = row['id']
                produced += 1
            if len(halts) < HALT_BATCH and self.state['event_cursor'] < event_max:
                self.state['event_cursor'] = event_max                # other event kinds: nothing to announce
            self.dirty = self.dirty or produced > 0
            return produced
        except (sqlite3.Error, paper.AccountingHalt, KeyError, TypeError, ValueError) as error:
            log.warning('store read failed: %s', type(error).__name__)
            return produced
        finally:
            view.close()

    # -- summary / commands ---------------------------------------------------------------------------------------
    def portfolio(self, view, now):
        positions, cash, realized = view.replay()
        states = view.open_states()
        marks = view.latest_marks(positions, states, pool_fee_bps=int(self.cfg['pool_fee_bps']))
        rows, equity = [], cash
        for mint, position in sorted(positions.items(), key=lambda kv: kv[1].opened_at):
            mark = marks.get(mint)
            value = mark[0] if mark else position.cost_lamports
            equity += value
            rows.append({'mint': mint, 'cost': position.cost_lamports, 'mark': mark[0] if mark else None,
                         'mark_ts': mark[1] if mark else None,
                         'unrealized': (mark[0] - position.cost_lamports) if mark else None, 'opened_at': position.opened_at})
        return {'positions': rows, 'cash': cash, 'realized_total': realized, 'equity': equity, 'initial': view.initial_cash()}

    def summary_data(self, view, start, end):
        closed = view.closed(start, end)
        realized = sum(f['realized_lamports'] for f in view.fills_between(start, end) if f['side'] == 'sell')
        wins = sum(1 for t in closed if t['pnl'] > 0)
        book = self.portfolio(view, end)
        best = max(closed, key=lambda t: t['pnl']) if closed else None
        worst = min(closed, key=lambda t: t['pnl']) if closed else None
        return {'start': start, 'end': end, 'trades': len(closed), 'wins': wins,
                'win_rate': Decimal(wins) / Decimal(len(closed)) if closed else None, 'realized': realized,
                'best': best, 'worst': worst, **book}

    def _position_lines(self, view, book, now):
        lines = []
        for p in book['positions']:
            name = esc(self.label(view, p['mint']))
            if p['unrealized'] is None:
                lines.append(' • %s: 원가 %s, 시세 없음' % (name, fmt_sol(sol_of(p['cost']))))
            else:
                age = ' · %s 전 시세' % fmt_duration(now - p['mark_ts']) if now - p['mark_ts'] > 300 else ''
                lines.append(' • %s: 미실현 %s (%s)%s' % (name, fmt_sol(sol_of(p['unrealized']), sign=True),
                                                        fmt_pct(Decimal(p['unrealized']) / Decimal(p['cost'])), age))
        return lines

    def _trade_line(self, view, title, trade):
        pct = ' (%s)' % fmt_pct(Decimal(trade['pnl']) / Decimal(trade['cost'])) if trade['cost'] else ''
        return '%s: %s %s%s' % (title, esc(self.label(view, trade['mint'])), fmt_sol(sol_of(trade['pnl']), sign=True), pct)

    def render_summary(self, view, data):
        end = self.local(data['end'])
        lines = ['📊 <b>일일 요약</b> (%s 기준 24시간)' % end.strftime('%m/%d %H:%M'),
                 '거래: %d건 · 승률 %s' % (data['trades'], fmt_pct(data['win_rate'], sign=False) if data['win_rate'] is not None else '-'),
                 '실현손익: %s' % fmt_sol(sol_of(data['realized']), sign=True)]
        lines.append('보유 포지션: %d개' % len(data['positions']))
        lines += self._position_lines(view, data, data['end'])
        change = Decimal(data['equity'] - data['initial']) / Decimal(data['initial'])
        lines.append('자산: %s (시작 %s 대비 %s)' % (fmt_sol(sol_of(data['equity'])), fmt_sol(sol_of(data['initial'])), fmt_pct(change)))
        if data['best'] is not None:
            lines.append(self._trade_line(view, '최고', data['best']))
            lines.append(self._trade_line(view, '최저', data['worst']))
        return '\n'.join(lines)

    def maybe_summary(self):
        at = self.cfg['daily_summary_at']
        if at is None:
            return False
        now = self.clock()
        local = self.local(now)
        hour, minute = _hhmm(at)
        scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        today = local.date().isoformat()
        if local < scheduled or self.state['last_summary_day'] == today:
            return False
        if self.state['last_summary_day'] is None and now - scheduled.timestamp() > 600:
            self.state['last_summary_day'] = today          # first start after today's time: wait for tomorrow
            self.dirty = True
            return False
        try:
            view = StoreView(open_store(self.db))
        except StoreUnavailable as error:
            log.warning('summary skipped: %s', error)
            return False
        try:
            end = scheduled.timestamp()
            text = self.render_summary(view, self.summary_data(view, end - 86400, end))
        except (sqlite3.Error, paper.AccountingHalt, KeyError, TypeError, ValueError) as error:
            log.warning('summary failed: %s', type(error).__name__)
            return False
        finally:
            view.close()
        self.enqueue(self.tagged(text))
        self.state['last_summary_day'] = today
        self.dirty = True
        return True

    def day_start(self, now):
        local = self.local(now)
        return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def cmd_status(self, view, now):
        book = self.portfolio(view, now)
        today = sum(f['realized_lamports'] for f in view.fills_between(self.day_start(now), now + 1) if f['side'] == 'sell')
        lines = ['📋 <b>현황</b>', '현금: %s' % fmt_sol(sol_of(book['cash'])),
                 '자산: %s (시작 %s 대비 %s)' % (fmt_sol(sol_of(book['equity'])), fmt_sol(sol_of(book['initial'])),
                                            fmt_pct(Decimal(book['equity'] - book['initial']) / Decimal(book['initial']))),
                 '오늘 실현손익: %s' % fmt_sol(sol_of(today), sign=True), '보유 포지션: %d개' % len(book['positions'])]
        return '\n'.join(lines + self._position_lines(view, book, now))

    def cmd_today(self, view, now):
        fills = view.fills_between(self.day_start(now), now + 1)
        lines = ['🗓 <b>오늘 체결</b> (%d건)' % len(fills)]
        for f in fills[-40:]:
            when = self.local(f['ts']).strftime('%H:%M')
            name = esc(self.label(view, f['mint']))
            if f['side'] == 'buy':
                lines.append('%s 🟢 %s %s' % (when, name, fmt_sol(sol_of(f['sol_lamports']))))
            else:
                lines.append('%s %s %s %s' % (when, '🔴' if f['qty_after'] == 0 else '🟡', name,
                                              fmt_sol(sol_of(f['realized_lamports']), sign=True)))
        if len(fills) > 40:
            lines.insert(1, '(최근 40건만 표시)')
        if not fills:
            lines.append('없음')
        return '\n'.join(lines)

    def cmd_pnl(self, view, now):
        realized = {}
        for f in view._fills("WHERE side='sell'"):
            realized[f['strategy_version']] = realized.get(f['strategy_version'], 0) + f['realized_lamports']
        trades = {}
        for t in view.closed():
            entry = trades.setdefault(t['strategy_version'], [0, 0])
            entry[0] += 1
            entry[1] += t['pnl'] > 0
        lines = ['💰 <b>누적 손익 (전략별)</b>']
        for version in sorted(set(realized) | set(trades)):
            count, wins = trades.get(version, [0, 0])
            rate = fmt_pct(Decimal(wins) / Decimal(count), sign=False) if count else '-'
            lines.append('%s: %s · %d건 · 승률 %s' % (esc(version), fmt_sol(sol_of(realized.get(version, 0)), sign=True), count, rate))
        if len(lines) == 1:
            lines.append('없음')
        return '\n'.join(lines)

    COMMANDS = {'/status': 'cmd_status', '/today': 'cmd_today', '/pnl': 'cmd_pnl'}

    def poll_commands(self, timeout=0):
        """One getUpdates long poll. Only ``allowed_chat_ids`` are answered; anyone else is ignored silently."""
        if not self.cfg['commands'] or self.clock() < self.updates_backoff_until:
            return 0
        first = self.state['update_offset'] is None
        try:
            updates = self.tg.get_updates(None if first else self.state['update_offset'], timeout=timeout)
        except TelegramError as error:
            self.updates_failures += 1
            delay = error.retry_after if error.retry_after is not None else \
                min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (self.updates_failures - 1))
            self.updates_backoff_until = self.clock() + delay
            log.warning('telegram getUpdates failed: %s', error.code)
            return 0
        self.updates_failures = 0
        self._long_polled = timeout > 0
        answered, offset = 0, self.state['update_offset'] or 0
        for update in updates:
            if not isinstance(update, dict) or type(update.get('update_id')) is not int:
                continue
            offset = max(offset, update['update_id'] + 1)
            if first:
                continue                                    # first start: pending commands are not answered
            message = update.get('message') if isinstance(update.get('message'), dict) else {}
            chat = message.get('chat') if isinstance(message.get('chat'), dict) else {}
            if str(chat.get('id')) not in self.allowed:
                continue                                    # unauthorized: silently ignored, not even logged by id
            text = message.get('text') if isinstance(message.get('text'), str) else ''
            command = text.strip().split(' ')[0].split('@')[0].lower() if text.strip() else ''
            handler = self.COMMANDS.get(command)
            if handler is None:
                continue
            try:
                view = StoreView(open_store(self.db))
            except StoreUnavailable:
                self.enqueue(self.tagged('저장소를 읽을 수 없습니다. 잠시 후 다시 시도하세요.'), chat['id'])
                answered += 1
                continue
            try:
                reply = getattr(self, handler)(view, self.clock())
            except (sqlite3.Error, paper.AccountingHalt, KeyError, TypeError, ValueError) as error:
                reply = '명령 처리 실패: %s' % esc(type(error).__name__)
            finally:
                view.close()
            self.enqueue(self.tagged(reply), chat['id'])
            answered += 1
        if offset != self.state['update_offset']:
            self.state['update_offset'] = offset
            self.dirty = True
        return answered

    # -- loop -----------------------------------------------------------------------------------------------------
    def run_once(self, *, wait=False):
        """One cycle: store -> digest -> summary -> send -> commands (long poll when ``wait``) -> send."""
        self.poll_store()
        self.flush_digest()
        self.maybe_summary()
        if self.dirty:
            self.save()                      # cursors + produced messages persisted BEFORE any send
        self.flush()
        timeout = int(self.cfg['poll_interval_s']) if wait else 0
        self._long_polled = False
        if self.poll_commands(timeout=timeout):
            self.flush()
        if self.dirty:
            self.save()
        if wait and not self._long_polled:          # no successful long poll waited for us: sleep instead
            self.sleep(float(self.cfg['poll_interval_s']))

    def run(self, stop=None):
        while stop is None or not stop.is_set():
            try:
                self.run_once(wait=True)
            except NotifyError:
                raise
            except Exception as error:                    # a bug in one cycle never ends the notifier
                log.error('cycle failed: %s', redact(type(error).__name__))
                self.sleep(float(self.cfg['poll_interval_s']) or 1.0)


# ------------------------------------------------------------------------------------------------ CLI
def discover_chats(telegram):
    """Chats that messaged the bot: [{'chat_id', 'first_name'}] (first name, or the group title)."""
    seen = {}
    for update in telegram.get_updates(None, timeout=0):
        for key in ('message', 'edited_message', 'channel_post', 'my_chat_member'):
            item = update.get(key) if isinstance(update, dict) else None
            chat = item.get('chat') if isinstance(item, dict) else None
            if isinstance(chat, dict) and 'id' in chat:
                seen[str(chat['id'])] = chat.get('first_name') or chat.get('title') or chat.get('username') or ''
    return [{'chat_id': cid, 'first_name': name} for cid, name in seen.items()]


def _helius(credentials, opener=None):
    from lean import providers
    try:
        keys = providers.load_keys(Path(credentials) / 'provider-keys.json')
    except providers.ProviderError:
        log.info('no provider keys: token names fall back to the short CA')
        return None
    kwargs = {'opener': opener} if opener else {}
    return providers.Helius(keys['helius'], providers.Transport('helius', lane='exit', deadline_seconds=10.0, max_attempts=2,
                                                                **kwargs))


def main(argv=None, *, opener=None):
    p = argparse.ArgumentParser(description='Telegram notifier for the lean paper desk (reads the store, never writes it)')
    p.add_argument('--db')
    p.add_argument('--config')
    p.add_argument('--credentials', default=os.environ.get('CREDENTIALS_DIRECTORY'),
                   help='directory holding telegram.json and provider-keys.json (default $CREDENTIALS_DIRECTORY)')
    p.add_argument('--state-dir', default=os.environ.get('STATE_DIRECTORY'))
    p.add_argument('--once', action='store_true', help='one cycle, then exit')
    p.add_argument('--discover-chat', action='store_true', help='print the chat ids / first names that messaged the bot')
    a = p.parse_args(argv)
    if not a.credentials:
        print(json.dumps({'status': 'ERROR', 'code': 'CREDENTIALS_DIR_MISSING'}), file=sys.stderr)
        return 2
    try:
        creds = load_telegram(Path(a.credentials) / 'telegram.json', need_chat=not a.discover_chat)
    except NotifyError as error:
        print(json.dumps({'status': 'ERROR', 'code': str(error)}), file=sys.stderr)
        return 2
    logging.getLogger().addFilter(_RedactingFilter(creds['bot_token']))
    for handler in logging.getLogger().handlers:
        handler.addFilter(_RedactingFilter(creds['bot_token']))
    telegram = Telegram(creds['bot_token'], opener=opener)
    if a.discover_chat:
        try:
            chats = discover_chats(telegram)
        except TelegramError as error:
            print(json.dumps({'status': 'ERROR', 'code': error.code, 'description': error.description}), file=sys.stderr)
            return 3
        print(json.dumps({'status': 'OK', 'chats': chats}, ensure_ascii=False))
        if not chats:
            print('No chats yet: send /start to the bot from your Telegram account, then run this again.', file=sys.stderr)
        return 0
    if not a.db or not a.state_dir:
        print(json.dumps({'status': 'ERROR', 'code': 'DB_AND_STATE_DIR_REQUIRED'}), file=sys.stderr)
        return 2
    try:
        cfg = load_config(a.config)
    except (NotifyError, OSError, ValueError) as error:
        print(json.dumps({'status': 'ERROR', 'code': redact(error)}), file=sys.stderr)
        return 2
    os.makedirs(a.state_dir, mode=0o700, exist_ok=True)
    notifier = Notifier(db=a.db, cfg=cfg, telegram=telegram, chat_id=creds['chat_id'], state_dir=a.state_dir,
                        helius=_helius(a.credentials, opener) if cfg['name_lookup'] else None)
    if a.once:
        notifier.run_once(wait=False)
        return 0
    import signal
    import threading
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    notifier.run(stop)
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s %(message)s')
    sys.exit(main())


# --- L10 hook (additive): fills shown to people carry no refundable ATA rent (price, fee, realized, cost), see lean.execution.economic.
# The accounting replay (``StoreView.replay``) keeps reading the raw ``_fills`` rows, so cash and PnL checks are unchanged. ---
def _install_economic_fills():
    from lean import execution
    plain = {name: getattr(StoreView, name) for name in ('fills_after', 'fills_between', 'fill', 'mint_fills')}

    def economic_row(view, row):
        state = view.state_for_fill(row['id']) or {}
        return execution.economic(row, (state.get('state') or {}).get('execution'))

    def wrap(name):
        function = plain[name]

        def method(self, *args, **kwargs):
            result = function(self, *args, **kwargs)
            if isinstance(result, list):
                return [economic_row(self, r) for r in result]
            return None if result is None else economic_row(self, result)
        method.__name__ = name
        return method
    for name in plain:
        setattr(StoreView, name, wrap(name))


_install_economic_fills()
# --- end L10 ---
