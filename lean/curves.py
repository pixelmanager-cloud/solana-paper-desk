"""C2: pump.fun BONDING-CURVE price-path recorder (universe #2, RECORDING ONLY). PAPER ONLY, read-only towards the chain.

A pump.fun token trades on its bonding curve BEFORE it graduates to PumpSwap. The pool paths (``lean.paths``) start only after
graduation; this module records the curve phase into its OWN ``curves.sqlite`` so analysis can read one continuous path per
mint (``join_by_mint``). Nothing here is read by the trader: no entry or exit decision depends on a curve row, and a recorder
failure never reaches the trader (every public method swallows and counts its own failures).

Polling. ONE ``getProgramAccounts``-style call per poll on the pump.fun program, on the shared LOW provider lane (``lean.providers
.low``), which never waits: a shed lane means the poll is skipped and a ``curve_poll_gaps`` row says so. The RPC method is
configurable (``method``):
  * ``getProgramAccounts``    one call returns EVERY curve account of the program. On mainnet that is far too large for one
                              response (the transport refuses more than 8 MiB: a gap with ``RESPONSE_OVERSIZED``); kept for fakes and
                              small clusters.
  * ``getProgramAccountsV2``  Helius' paginated variant: ``limit``, ``paginationKey`` and ``changedSinceSlot`` (only curves that
                              changed since the last complete poll). The coordinator verifies this call live; the request and the
                              response shapes here follow Helius' documentation and are tested with fakes only.
The filter is the 8-byte account discriminator at offset 0 (all layout versions). ``complete`` is NOT filtered on the server, so the
graduation (complete = 1) is seen.

Account layout relied on (``desk/schemas/pump.json`` ``BondingCurve``; a test pins these offsets against the schema):
  discriminator 17b7f83760d8ac60 = [23,183,248,55,96,216,172,96] (offset 0, 8 bytes), then little-endian
  8 virtual_token_reserves u64 | 16 virtual_quote_reserves u64 (SOL, lamports) | 24 real_token_reserves u64 | 32 real_quote_reserves u64 |
  40 token_total_supply u64 | 48 complete bool | 49 creator pubkey | 81 is_mayhem_mode bool | 82 is_cashback_coin bool |
  83 quote_mint pubkey | 115 creator_fee_bps u64 | 123 can_edit_creator_fee bool | 124 is_holder_reward bool  (125 bytes today).
Older curves are shorter (49, 81, 82, 83, 115 bytes); fields past the end are None, a missing quote_mint means SOL. Bytes past what is
known are ignored (a newer layout only appends). The account does NOT contain the mint: the curve address is the PDA
``["bonding-curve", mint]`` of the pump program, so a mint's curve is found by computing the address (``curve_address``).

Price, progress. ``price_sol`` = (virtual_quote / 1e9) / (virtual_token / 1e6) (pump.fun mints have 6 decimals). ``progress`` = 1 -
real_token_reserves / ``initial_real_token_reserves`` (793.1M tokens x 1e6, pump.fun's own UI figure), clamped to [0, 1]. ``sol_raised``
= real_quote_reserves / 1e9. Only SOL-quoted curves (quote_mint absent or the wrapped-SOL mint) are tracked.

Tracking. A curve is tracked from the first poll that shows it incomplete with progress >= ``min_progress`` (default 0.5), at most
``max_tracked`` at once. A mark is written when its reserves or flag change (an unchanged curve writes nothing: between two marks with no
gap row in between nothing changed). The path ends with ``GRADUATED`` (complete seen; the final mark is written first) or ``STALE``
(no change for ``stale_s``; the slot is freed and a later change starts a new segment).

Budget. ``getProgramAccounts`` costs ~10 credits per call. The hard cap ``max_credits_month`` (default 500,000 = 69 calls/hour) is turned
into a rolling hour budget that is checked before EVERY call (restart-proof: it is read from ``curve_polls``) and into a minimum
interval, so the requested ``poll_s`` (10 s) is stretched to about 52 s by default: ``estimate`` and ``health`` show both.

Retention follows lean.paths (L07R2): its own file, WAL, size rollover to ``curves-YYYYMMDD[-n].sqlite`` carrying the live paths, and a
free-disk floor below which nothing is written.
"""
import base64
import binascii
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path

from lean.providers import LANE_SHED, ProviderError

log = logging.getLogger('lean.curves')

PUMP = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
SOL_MINT = 'So11111111111111111111111111111111111111112'
DISCRIMINATOR = bytes([23, 183, 248, 55, 96, 216, 172, 96])
# offsets of the fields (see the module docstring); CORE_LEN is the shortest account this module decodes
OFFSETS = {'virtual_token_reserves': 8, 'virtual_quote_reserves': 16, 'real_token_reserves': 24, 'real_quote_reserves': 32,
           'token_total_supply': 40, 'complete': 48, 'creator': 49, 'is_mayhem_mode': 81, 'is_cashback_coin': 82, 'quote_mint': 83,
           'creator_fee_bps': 115, 'can_edit_creator_fee': 123, 'is_holder_reward': 124}
CORE_LEN = 49
CURRENT_LEN = 125
DECIMALS = 6
INITIAL_REAL_TOKEN_RESERVES = 793_100_000 * 10 ** DECIMALS
GB = 1024 ** 3
HOURS_PER_MONTH = 720
MAX_ENTRIES = 200_000
METHODS = ('getProgramAccounts', 'getProgramAccountsV2')
_BASE58 = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')
_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
DEFAULTS = {
    'enabled': False, 'db': None, 'poll_s': 10.0, 'method': 'getProgramAccounts', 'min_progress': 0.5, 'max_tracked': 500,
    'page_limit': 1000, 'max_pages': 3, 'stale_s': 7200.0, 'changed_since': True, 'data_slice_len': CURRENT_LEN,
    'credit_per_call': 10, 'max_credits_month': 500_000, 'max_db_gb': 2.0, 'min_free_gb': 2.0,
    'initial_real_token_reserves': INITIAL_REAL_TOKEN_RESERVES,
}


class ConfigError(ValueError):
    pass


class CurveError(ValueError):
    """A curve account that cannot be decoded (the code says why)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def config(raw):
    """lean.json ``curves`` -> validated settings. Absent / null = disabled. Unknown keys and wrong types are refused."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ConfigError('curves must be an object')
    unknown = set(raw) - set(DEFAULTS) - {'_comment'}
    if unknown:
        raise ConfigError('curves: unknown keys %s' % sorted(unknown))
    out = {k: raw.get(k, v) for k, v in DEFAULTS.items()}
    for key in ('enabled', 'changed_since'):
        if type(out[key]) is not bool:
            raise ConfigError('curves.%s must be true or false' % key)
    if out['db'] is not None and (not isinstance(out['db'], str) or not out['db'].strip()):
        raise ConfigError('curves.db must be a path or null')
    if out['method'] not in METHODS:
        raise ConfigError('curves.method must be one of %s' % (METHODS,))
    for key, low, high in (('poll_s', 1, 3600), ('min_progress', 0, 1), ('stale_s', 60, 7 * 86400), ('max_db_gb', 0.001, 1000),
                           ('min_free_gb', 0, 1000)):
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or not low <= value <= high:
            raise ConfigError('curves.%s out of range' % key)
        out[key] = float(value)
    for key, low, high in (('max_tracked', 1, 100_000), ('page_limit', 1, 10_000), ('max_pages', 1, 100), ('credit_per_call', 1, 1000),
                           ('max_credits_month', 1, 10 ** 9), ('initial_real_token_reserves', 1, 2 ** 64 - 1)):
        if type(out[key]) is not int or not low <= out[key] <= high:
            raise ConfigError('curves.%s must be an integer in [%d, %d]' % (key, low, high))
    if out['data_slice_len'] is not None and (type(out['data_slice_len']) is not int or not CORE_LEN <= out['data_slice_len'] <= 4096):
        raise ConfigError('curves.data_slice_len must be null or an integer in [%d, 4096]' % CORE_LEN)
    return out


def db_path(cfg, state_dir):
    """``curves.db`` (absolute, or relative to the state dir), default ``<state-dir>/curves.sqlite``."""
    path = Path(cfg.get('db') or 'curves.sqlite')
    return path if path.is_absolute() else Path(state_dir) / path


# ------------------------------------------------------------------------------------------------------ base58 / PDA
def b58encode(data):
    n, out = int.from_bytes(data, 'big'), ''
    while n:
        n, r = divmod(n, 58)
        out = _ALPHABET[r] + out
    return '1' * (len(data) - len(data.lstrip(b'\0'))) + out


DISCRIMINATOR_B58 = b58encode(DISCRIMINATOR)


def curve_address(mint):
    """The bonding-curve account of ``mint``: the PDA ["bonding-curve", mint] of the pump program."""
    from desk.graduation_witness import _pda
    from desk.programs import address, unbase58
    address(mint)
    return _pda([b'bonding-curve', unbase58(mint)], PUMP)


# ----------------------------------------------------------------------------------------------------------- decoding
def decode_curve(data):
    """Raw account bytes -> dict. Raises ``CurveError`` (``code``) for anything that is not a curve this module understands."""
    if not isinstance(data, (bytes, bytearray)):
        raise CurveError('NOT_BYTES')
    data = bytes(data)
    if len(data) < CORE_LEN:
        raise CurveError('TOO_SHORT')
    if data[:8] != DISCRIMINATOR:
        raise CurveError('BAD_DISCRIMINATOR')
    if data[OFFSETS['complete']] not in (0, 1):
        raise CurveError('INVALID_COMPLETE_FLAG')
    u64 = lambda name: int.from_bytes(data[OFFSETS[name]:OFFSETS[name] + 8], 'little')
    out = {'virtual_token_reserves': u64('virtual_token_reserves'), 'virtual_quote_reserves': u64('virtual_quote_reserves'),
           'real_token_reserves': u64('real_token_reserves'), 'real_quote_reserves': u64('real_quote_reserves'),
           'token_total_supply': u64('token_total_supply'), 'complete': bool(data[OFFSETS['complete']]), 'account_len': len(data),
           'creator': None, 'quote_mint': None, 'is_mayhem_mode': None, 'is_cashback_coin': None}
    if len(data) >= OFFSETS['creator'] + 32:
        out['creator'] = b58encode(data[OFFSETS['creator']:OFFSETS['creator'] + 32])
    for name in ('is_mayhem_mode', 'is_cashback_coin'):
        if len(data) > OFFSETS[name]:
            if data[OFFSETS[name]] not in (0, 1):
                raise CurveError('INVALID_FLAG')
            out[name] = bool(data[OFFSETS[name]])
    if len(data) >= OFFSETS['quote_mint'] + 32:
        out['quote_mint'] = b58encode(data[OFFSETS['quote_mint']:OFFSETS['quote_mint'] + 32])
    return out


def is_sol_quoted(curve):
    """A curve from before ``quote_mint`` existed is SOL-quoted; otherwise the quote mint must be wrapped SOL."""
    return curve['quote_mint'] in (None, SOL_MINT)


def price_sol(curve):
    """SOL per whole token from the virtual reserves, or None when a reserve is empty."""
    vtr, vsr = curve['virtual_token_reserves'], curve['virtual_quote_reserves']
    if vtr <= 0 or vsr <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 60
        return (Decimal(vsr) / Decimal(10 ** 9)) / (Decimal(vtr) / Decimal(10 ** DECIMALS))


def progress(curve, initial_real=INITIAL_REAL_TOKEN_RESERVES):
    """1 - real_token_reserves / initial, clamped to [0, 1] (pump.fun's own progress figure)."""
    with localcontext() as ctx:
        ctx.prec = 40
        value = Decimal(1) - Decimal(curve['real_token_reserves']) / Decimal(initial_real)
    return min(Decimal(1), max(Decimal(0), value))


def _fmt(value, places=18):
    return None if value is None else format(value.quantize(Decimal(1).scaleb(-places)), 'f')


def estimate(cfg):
    """Calls/hour and credits/month of a validated config: the requested cadence and the one the credit budget allows."""
    budget_hour = cfg['max_credits_month'] / HOURS_PER_MONTH
    floor_interval = cfg['credit_per_call'] * 3600.0 / budget_hour
    effective = max(cfg['poll_s'], floor_interval)
    calls_hour = 3600.0 / effective
    return {'requested_poll_s': cfg['poll_s'], 'effective_poll_s': round(effective, 3), 'calls_per_hour': round(calls_hour, 2),
            'credits_per_hour': round(calls_hour * cfg['credit_per_call'], 1),
            'credits_per_month': round(calls_hour * cfg['credit_per_call'] * HOURS_PER_MONTH, 0),
            'max_calls_per_hour_by_budget': int(budget_hour // cfg['credit_per_call']),
            'worst_case_pages_per_poll': cfg['max_pages'] if cfg['method'] == 'getProgramAccountsV2' else 1}


# ------------------------------------------------------------------------------------------------------------- the file
SCHEMA = (
    'CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS curve_paths(curve TEXT NOT NULL, segment INTEGER NOT NULL, start_ts REAL NOT NULL, start_slot INTEGER, '
    'creator TEXT, quote_mint TEXT, account_len INTEGER NOT NULL, start_progress TEXT NOT NULL, carried_from TEXT, '
    'code_version TEXT NOT NULL, PRIMARY KEY(curve, segment))',
    'CREATE TABLE IF NOT EXISTS curve_marks(curve TEXT NOT NULL, segment INTEGER NOT NULL, ts REAL NOT NULL, slot INTEGER, '
    'virtual_token INTEGER NOT NULL, virtual_quote INTEGER NOT NULL, real_token INTEGER NOT NULL, real_quote INTEGER NOT NULL, '
    'supply INTEGER NOT NULL, complete INTEGER NOT NULL, price_sol TEXT, progress TEXT NOT NULL, sol_raised TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS curve_marks_curve_ts ON curve_marks(curve, ts)',
    'CREATE TABLE IF NOT EXISTS curve_ends(curve TEXT NOT NULL, segment INTEGER NOT NULL, ts REAL NOT NULL, why TEXT NOT NULL, '
    'marks INTEGER, PRIMARY KEY(curve, segment))',
    'CREATE TABLE IF NOT EXISTS curve_poll_gaps(ts REAL NOT NULL, cause TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS curve_poll_gaps_ts ON curve_poll_gaps(ts)',
    'CREATE TABLE IF NOT EXISTS curve_polls(ts REAL NOT NULL, method TEXT NOT NULL, calls INTEGER NOT NULL, credits REAL NOT NULL, '
    'accounts INTEGER NOT NULL, tracked INTEGER NOT NULL, marks INTEGER NOT NULL, slot INTEGER, ok INTEGER NOT NULL)',
    'CREATE INDEX IF NOT EXISTS curve_polls_ts ON curve_polls(ts)',
)
PATH_COLUMNS = ('curve', 'segment', 'start_ts', 'start_slot', 'creator', 'quote_mint', 'account_len', 'start_progress',
                'carried_from', 'code_version')
MARK_COLUMNS = ('curve', 'segment', 'ts', 'slot', 'virtual_token', 'virtual_quote', 'real_token', 'real_quote', 'supply', 'complete',
                'price_sol', 'progress', 'sol_raised')


class CurveStore:
    """``curves.sqlite``: this recorder's own file, connection and lock."""

    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self._lock = threading.RLock()
        self.db = None
        self._open()

    def _open(self):
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ValueError('curves db must be a regular file')
        if not self.path.parent.is_dir():
            raise ValueError('curves db directory missing')
        if not self.path.exists():
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')         # research data: a crash may lose the last poll, never more
        self.db.execute('PRAGMA busy_timeout=5000')
        self.tx(lambda: [self.db.execute(sql) for sql in SCHEMA] + [self.db.execute("INSERT OR IGNORE INTO meta VALUES('schema','curves-1')")])

    def tx(self, function):
        with self._lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                value = function()
                self.db.execute('COMMIT')
                return value
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                raise

    def meta(self, key, default=None):
        with self._lock:
            row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return default if row is None else row[0]

    def write(self, *, paths=(), marks=(), ends=(), gaps=(), polls=(), meta=None):
        """One transaction for everything a poll produced."""
        def go():
            for row in paths:
                self.db.execute('INSERT INTO curve_paths(%s) VALUES(%s)' % (','.join(PATH_COLUMNS), ','.join('?' * len(PATH_COLUMNS))),
                                tuple(row[c] for c in PATH_COLUMNS))
            if marks:
                self.db.executemany('INSERT INTO curve_marks(%s) VALUES(%s)' % (','.join(MARK_COLUMNS), ','.join('?' * len(MARK_COLUMNS))),
                                    [tuple(m[c] for c in MARK_COLUMNS) for m in marks])
            if ends:
                self.db.executemany('INSERT INTO curve_ends(curve,segment,ts,why,marks) VALUES(?,?,?,?,?)', ends)
            if gaps:
                self.db.executemany('INSERT INTO curve_poll_gaps(ts,cause) VALUES(?,?)', gaps)
            if polls:
                self.db.executemany('INSERT INTO curve_polls(ts,method,calls,credits,accounts,tracked,marks,slot,ok) VALUES(?,?,?,?,?,?,?,?,?)', polls)
            for key, value in (meta or {}).items():
                self.db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))
        if paths or marks or ends or gaps or polls or meta:
            self.tx(go)

    def live(self):
        """(path row, last mark row or None) of every path without an end, oldest first."""
        with self._lock:
            rows = self.db.execute('SELECT %s FROM curve_paths p WHERE NOT EXISTS (SELECT 1 FROM curve_ends e WHERE e.curve=p.curve '
                                   'AND e.segment=p.segment) ORDER BY start_ts, curve' % ','.join('p.' + c for c in PATH_COLUMNS)).fetchall()
            out = []
            for row in rows:
                path = dict(zip(PATH_COLUMNS, row))
                last = self.db.execute('SELECT %s FROM curve_marks WHERE curve=? AND segment=? ORDER BY ts DESC LIMIT 1' % ','.join(MARK_COLUMNS),
                                       (path['curve'], path['segment'])).fetchone()
                out.append((path, None if last is None else dict(zip(MARK_COLUMNS, last))))
            return out

    def next_segment(self, curve):
        with self._lock:
            return (self.db.execute('SELECT COALESCE(MAX(segment),-1)+1 FROM curve_paths WHERE curve=?', (curve,)).fetchone()[0])

    def poll_usage(self, since):
        """(calls, credits) spent by polls at or after ``since`` (restart-proof budget input)."""
        with self._lock:
            return self.db.execute('SELECT COALESCE(SUM(calls),0), COALESCE(SUM(credits),0) FROM curve_polls WHERE ts>=?', (since,)).fetchone()

    def last_poll_ts(self):
        with self._lock:
            return self.db.execute('SELECT MAX(ts) FROM curve_polls').fetchone()[0]

    def size_bytes(self):
        total = 0
        for suffix in ('', '-wal'):
            try:
                total += os.path.getsize(str(self.path) + suffix)
            except OSError:
                pass
        return total

    def free_bytes(self):
        return shutil.disk_usage(self.path.parent).free

    def rollover(self):
        """Rename this file to ``curves-YYYYMMDD[-n].sqlite`` and start a fresh one carrying the live paths (and their last mark)."""
        with self._lock:
            live = self.live()
            cursor = self.meta('changed_since_slot')
            self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            self.db.close()
            day = datetime.fromtimestamp(self.clock(), timezone.utc).strftime('%Y%m%d')
            archive, n = self.path.with_name('curves-%s.sqlite' % day), 1
            while archive.exists():
                archive, n = self.path.with_name('curves-%s-%d.sqlite' % (day, n)), n + 1
            os.rename(self.path, archive)
            for suffix in ('-wal', '-shm'):
                try:
                    os.unlink(str(self.path) + suffix)
                except FileNotFoundError:
                    pass
            self._open()
            self.write(paths=[dict(path, carried_from=archive.name) for path, _last in live],
                       marks=[dict(last) for _path, last in live if last is not None],
                       meta={'changed_since_slot': cursor} if cursor is not None else None)
            return archive

    def close(self):
        with self._lock:
            if self.db is not None:
                self.db.close()


# ------------------------------------------------------------------------------------------------------- the recorder
class _Live:
    __slots__ = ('segment', 'last', 'last_change', 'marks')

    def __init__(self, segment, last, last_change, marks=0):
        self.segment, self.last, self.last_change, self.marks = segment, last, last_change, marks


class CurveRecorder:
    def __init__(self, *, db, helius, cfg, code_version, clock=time.time):
        self.db, self.helius, self.cfg, self.clock, self.code_version = db, helius, cfg, clock, code_version
        self.stop = threading.Event()
        self.method = cfg['method']
        self.min_progress = Decimal(str(cfg['min_progress']))
        self.initial_real = cfg['initial_real_token_reserves']
        self.credit_per_call = cfg['credit_per_call']
        self.budget_hour = cfg['max_credits_month'] / HOURS_PER_MONTH
        self.live = {}                                       # curve -> _Live
        self._lock = threading.Lock()
        self.cursor = None                                   # changedSinceSlot (V2): the slot of the last COMPLETE poll
        self.last_poll_at = None
        self.last_error = None
        self.errors_by_code = {}
        self.stats = {'polls': 0, 'calls': 0, 'pages': 0, 'accounts': 0, 'marks': 0, 'started': 0, 'graduated': 0, 'stale': 0,
                      'gaps': 0, 'shed': 0, 'budget_skips': 0, 'disk_low': 0, 'decode_errors': 0, 'bad_owner': 0, 'non_sol': 0,
                      'below_min_progress': 0, 'complete_untracked': 0, 'capacity_dropped': 0, 'unchanged': 0, 'write_failures': 0,
                      'rollovers': 0, 'credits': 0.0}
        self._floor_bytes = 0
        self.avg_pages = 1.0                                 # moving average of the calls a poll needed (V2 pagination)
        self.resume()

    # -- bookkeeping ----------------------------------------------------------------------------------------------
    def _bump(self, name, n=1):
        with self._lock:
            self.stats[name] += n

    def _fail(self, code, error=None):
        code = str(getattr(error, 'code', None) or code)[:64]
        with self._lock:
            self.errors_by_code[code] = self.errors_by_code.get(code, 0) + 1
            self.last_error = code
        log.debug('curve recorder: %s (%s)', code, type(error).__name__ if error is not None else '-')

    def effective_interval_s(self):
        """The requested cadence, stretched so that (calls per poll x credits per call) fits the hourly credit budget. V2 polls
        may page: the average pages per poll so far (starting at 1) is part of the cost of a poll."""
        return max(self.cfg['poll_s'], self.credit_per_call * max(1.0, self.avg_pages) * 3600.0 / self.budget_hour)

    def health(self):
        with self._lock:
            out = {'enabled': True, 'tracked': len(self.live), 'last_poll_at': self.last_poll_at, 'last_error': self.last_error,
                   'errors_by_code': dict(self.errors_by_code), 'cursor_slot': self.cursor, **self.stats}
        out['estimate'] = estimate(self.cfg)
        out['avg_pages_per_poll'] = round(self.avg_pages, 3)
        out['effective_poll_s'] = round(self.effective_interval_s(), 3)
        out['db'] = str(self.db.path)
        out['db_bytes'] = self.db.size_bytes()
        try:
            calls, credits = self.db.poll_usage(self.clock() - 3600)
            out['calls_last_hour'], out['credits_last_hour'] = calls, credits
        except sqlite3.Error:
            pass
        return out

    # -- restart --------------------------------------------------------------------------------------------------
    def resume(self):
        """Live paths and the changedSinceSlot cursor from the file. Never raises."""
        try:
            now = self.clock()
            for path, last in self.db.live():
                if last is not None:
                    self.live[path['curve']] = _Live(path['segment'], last, last['ts'], 0)
                else:
                    self.live[path['curve']] = _Live(path['segment'], None, path['start_ts'], 0)
            cursor = self.db.meta('changed_since_slot')
            self.cursor = int(cursor) if cursor is not None and cursor.isdecimal() else None
            previous = self.db.last_poll_ts()
            if previous is not None and now - previous > 2 * self.effective_interval_s():
                self.db.write(gaps=[(now, 'DOWNTIME')])
                self._bump('gaps')
            return len(self.live)
        except Exception as error:                            # noqa: BLE001
            self._fail('RESUME_FAILED', error)
            return 0

    # -- request --------------------------------------------------------------------------------------------------
    def _params(self, page_key):
        opts = {'encoding': 'base64', 'commitment': 'confirmed',
                'filters': [{'memcmp': {'offset': 0, 'bytes': DISCRIMINATOR_B58}}]}
        if self.cfg['data_slice_len']:
            opts['dataSlice'] = {'offset': 0, 'length': self.cfg['data_slice_len']}
        if self.method == 'getProgramAccounts':
            opts['withContext'] = True
        else:
            opts['limit'] = self.cfg['page_limit']
            if self.cfg['changed_since'] and self.cursor is not None:
                opts['changedSinceSlot'] = self.cursor
            if page_key is not None:
                opts['paginationKey'] = page_key
        return [PUMP, opts]

    @staticmethod
    def normalize(result):
        """(entries, slot, pagination_key) of a v1 list, a v1 ``withContext`` object or a V2 object. ValueError on anything else."""
        slot, key = None, None
        if isinstance(result, list):
            entries = result
        elif isinstance(result, dict):
            entries = result.get('accounts', result.get('value'))
            context = result.get('context')
            slot = context.get('slot') if isinstance(context, dict) else result.get('slot')
            key = result.get('paginationKey')
        else:
            raise ValueError('MALFORMED_RESPONSE')
        if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
            raise ValueError('MALFORMED_RESPONSE')
        if slot is not None and (type(slot) is not int or slot < 0):
            raise ValueError('MALFORMED_RESPONSE')
        if key is not None and (not isinstance(key, str) or not key or len(key) > 4096):
            raise ValueError('MALFORMED_RESPONSE')
        return entries, slot, key

    # -- one poll -------------------------------------------------------------------------------------------------
    def poll(self):
        """One round. Never raises; returns the number of marks written."""
        try:
            return self._poll()
        except Exception as error:                            # noqa: BLE001 - never reaches the trader
            self._fail('POLL_FAILED', error)
            return 0

    def _poll(self):
        now = self.clock()
        with self._lock:
            self.stats['polls'] += 1
            self.last_poll_at = now
        if self.cfg['min_free_gb'] and self._free() < self.cfg['min_free_gb'] * GB:
            self._bump('disk_low')                              # below the floor NOTHING is written (the counter is the gap record)
            return 0
        entries, slot, complete, calls, credits, error_code = [], None, True, 0, 0.0, None
        page_key = None
        for _page in range(self.cfg['max_pages'] if self.method == 'getProgramAccountsV2' else 1):
            if self.stop.is_set():
                error_code, complete = 'stopped', False
                break
            _calls, spent = self.db.poll_usage(now - 3600)
            if spent + credits + self.credit_per_call > self.budget_hour + 1e-9:
                error_code, complete = 'budget', False
                self._bump('budget_skips')
                break
            try:
                result, _raw, meta = self.helius.rpc(self.method, self._params(page_key))
            except ProviderError as error:
                complete = False
                if error.code == LANE_SHED:                    # nothing was sent: shed means skip
                    self._bump('shed')
                    error_code = 'shed'
                else:
                    attempts = max(1, int((error.meta or {}).get('attempts', 1) or 1))
                    calls += 1
                    credits += self.credit_per_call * attempts
                    self._fail('PROVIDER', error)
                    error_code = error.code
                break
            attempts = max(1, int((meta or {}).get('attempts', 1) or 1))
            calls += 1
            credits += self.credit_per_call * attempts
            self._bump('pages')
            try:
                page, page_slot, next_key = self.normalize(result)
            except ValueError:
                self._fail('MALFORMED_RESPONSE')
                error_code, complete = 'MALFORMED_RESPONSE', False
                break
            entries += page
            slot = page_slot if page_slot is not None else slot
            page_key = next_key
            if next_key is None:
                break
        else:
            complete = page_key is None                        # ran out of pages with more to come: not a complete poll
            if not complete:
                error_code = 'max_pages'
        self._bump('calls', calls)
        if calls:
            self.avg_pages = 0.8 * self.avg_pages + 0.2 * calls
        marks_written = self._process(now, entries, slot, complete, calls, credits, error_code)
        self._maybe_rollover()
        return marks_written

    def _process(self, now, entries, slot, complete, calls, credits, error_code):
        new_paths, marks, ends, accepted = [], [], [], 0
        updates = {}                                           # curve -> _Live after a successful write
        removed = []
        tracked_now = len(self.live)
        seen = set()
        for entry in entries:
            curve = self._entry(entry)
            if curve is None:
                continue
            pubkey, decoded = curve
            if pubkey in seen:
                continue
            seen.add(pubkey)
            accepted += 1
            state = self.live.get(pubkey)
            prog = progress(decoded, self.initial_real)
            if state is None:
                if decoded['complete']:
                    self._bump('complete_untracked')
                    continue
                if prog < self.min_progress:
                    self._bump('below_min_progress')
                    continue
                if tracked_now + len(new_paths) >= self.cfg['max_tracked']:
                    self._bump('capacity_dropped')
                    continue
                segment = self.db.next_segment(pubkey)
                new_paths.append({'curve': pubkey, 'segment': segment, 'start_ts': now, 'start_slot': slot,
                                  'creator': decoded['creator'], 'quote_mint': decoded['quote_mint'],
                                  'account_len': decoded['account_len'], 'start_progress': _fmt(prog, 6), 'carried_from': None,
                                  'code_version': self.code_version})
                mark = self._mark(pubkey, segment, now, slot, decoded, prog)
                marks.append(mark)
                updates[pubkey] = _Live(segment, mark, now, 1)
                continue
            last = state.last
            if last is not None and self._same(last, decoded):
                self._bump('unchanged')
                continue
            mark = self._mark(pubkey, state.segment, now, slot, decoded, prog)
            marks.append(mark)
            updates[pubkey] = _Live(state.segment, mark, now, state.marks + 1)
            if decoded['complete']:
                ends.append((pubkey, state.segment, now, 'GRADUATED', state.marks + 1))
                removed.append(pubkey)
        for pubkey, state in self.live.items():                # stale sweep (also for curves not in this poll)
            if pubkey in removed or pubkey in updates:
                continue
            if now - state.last_change >= self.cfg['stale_s']:
                ends.append((pubkey, state.segment, now, 'STALE', state.marks))
                removed.append(pubkey)
        meta = None
        if complete and slot is not None and self.method == 'getProgramAccountsV2':
            meta = {'changed_since_slot': slot}
        poll_row = (now, self.method, calls, credits, accepted, tracked_now, len(marks), slot, int(error_code is None))
        gaps = [] if error_code is None else [(now, error_code)]
        if not self._write(paths=new_paths, marks=marks, ends=ends, gaps=gaps, polls=[poll_row] if calls else [], meta=meta):
            return 0
        with self._lock:
            for pubkey, state in updates.items():
                self.live[pubkey] = state
            for pubkey in removed:
                self.live.pop(pubkey, None)
            for _c, _s, _t, why, _m in ends:
                self.stats['graduated' if why == 'GRADUATED' else 'stale'] += 1
            self.stats['started'] += len(new_paths)
            self.stats['marks'] += len(marks)
            self.stats['accounts'] += accepted
            self.stats['credits'] += credits
            self.stats['gaps'] += len(gaps)
            if meta:
                self.cursor = slot
        return len(marks)

    def _entry(self, entry):
        """(curve pubkey, decoded curve) of one program-account entry, or None (and the reason is counted)."""
        try:
            pubkey, account = entry['pubkey'], entry['account']
            if not isinstance(pubkey, str) or _BASE58.fullmatch(pubkey) is None or account.get('owner') != PUMP:
                self._bump('bad_owner')
                return None
            data = account['data']
            if not isinstance(data, list) or len(data) != 2 or data[1] != 'base64':
                raise CurveError('BAD_ENCODING')
            try:
                raw = base64.b64decode(data[0], validate=True)
            except (binascii.Error, ValueError):
                raise CurveError('BAD_BASE64') from None
            decoded = decode_curve(raw)
        except CurveError as error:
            self._bump('decode_errors')
            self._fail('DECODE_' + error.code)
            return None
        except (KeyError, TypeError, AttributeError):
            self._bump('decode_errors')
            self._fail('DECODE_MALFORMED_ENTRY')
            return None
        if not is_sol_quoted(decoded):
            self._bump('non_sol')
            return None
        return pubkey, decoded

    @staticmethod
    def _same(last, decoded):
        return (last['virtual_token'] == decoded['virtual_token_reserves'] and last['virtual_quote'] == decoded['virtual_quote_reserves']
                and last['real_token'] == decoded['real_token_reserves'] and last['real_quote'] == decoded['real_quote_reserves']
                and bool(last['complete']) == decoded['complete'])

    @staticmethod
    def _mark(curve, segment, now, slot, decoded, prog):
        return {'curve': curve, 'segment': segment, 'ts': now, 'slot': slot, 'virtual_token': decoded['virtual_token_reserves'],
                'virtual_quote': decoded['virtual_quote_reserves'], 'real_token': decoded['real_token_reserves'],
                'real_quote': decoded['real_quote_reserves'], 'supply': decoded['token_total_supply'],
                'complete': int(decoded['complete']), 'price_sol': _fmt(price_sol(decoded)), 'progress': _fmt(prog, 6),
                'sol_raised': format(Decimal(decoded['real_quote_reserves']) / Decimal(10 ** 9), 'f')}

    # -- storage --------------------------------------------------------------------------------------------------
    def _write(self, **rows):
        try:
            self.db.write(**rows)
            return True
        except Exception as error:                            # noqa: BLE001 - a failed write is a gap, never an exception
            self._bump('write_failures')
            self._fail('WRITE_FAILED', error)
            return False

    def _free(self):
        try:
            return self.db.free_bytes()
        except OSError:
            return float('inf')

    def _maybe_rollover(self):
        try:
            size = self.db.size_bytes()
            if size > self.cfg['max_db_gb'] * GB and size > 2 * self._floor_bytes:
                archive = self.db.rollover()
                self._floor_bytes = self.db.size_bytes()
                self._bump('rollovers')
                log.warning('curves db passed %.3f GB: rolled over to %s', self.cfg['max_db_gb'], archive.name)
        except Exception as error:                            # noqa: BLE001
            self._fail('ROLLOVER_FAILED', error)

    # -- the thread -----------------------------------------------------------------------------------------------
    def run_loop(self, *, monotonic=time.monotonic):
        """Poll every ``effective_interval_s`` until ``stop`` is set. For its own daemon thread; never raises."""
        while not self.stop.is_set():
            began = monotonic()
            self.poll()
            self.stop.wait(max(0.0, self.effective_interval_s() - (monotonic() - began)))


# ----------------------------------------------------------------------------------------------------------------- join
def _open_ro(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('database missing or not a regular file: %s' % path.name)
    resolved = path.resolve()
    connection = sqlite3.connect(resolved.as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.execute('PRAGMA query_only=1')
    return connection


def join_by_mint(curves_db, paths_db, mint):
    """One continuous series for ``mint``: its bonding-curve marks (curves.sqlite) followed by its PumpSwap pool marks
    (paths.sqlite), both read-only. Entries are ``{'ts', 'source': 'curve'|'pool', 'price_sol', 'progress' (curve only)}`` in time
    order. The curve phase ends with the ``GRADUATED`` end row (if seen); a ``STALE`` segment may precede it. Archives are not read."""
    curve = curve_address(mint)
    with _open_ro(curves_db) as c:
        c.row_factory = sqlite3.Row
        marks = [dict(r) for r in c.execute('SELECT * FROM curve_marks WHERE curve=? ORDER BY ts, rowid', (curve,))]
        ends = [dict(r) for r in c.execute('SELECT * FROM curve_ends WHERE curve=? ORDER BY ts', (curve,))]
        paths = [dict(r) for r in c.execute('SELECT * FROM curve_paths WHERE curve=? ORDER BY segment', (curve,))]
    series = [{'ts': m['ts'], 'source': 'curve', 'segment': m['segment'], 'price_sol': m['price_sol'], 'progress': m['progress'],
               'complete': bool(m['complete'])} for m in marks]
    pool_marks = []
    if paths_db is not None:
        with _open_ro(paths_db) as p:
            p.row_factory = sqlite3.Row
            pool_marks = [dict(r) for r in p.execute('SELECT * FROM path_marks WHERE mint=? ORDER BY ts, rowid', (mint,))
                          ] if p.execute("SELECT 1 FROM sqlite_master WHERE name='path_marks'").fetchone() else []
    for m in pool_marks:
        if m['status'] != 'OK' or not m['base_raw'] or not m['quote_raw']:
            continue
        with localcontext() as ctx:
            ctx.prec = 60
            spot = (Decimal(m['quote_raw']) / Decimal(10 ** 9)) / (Decimal(m['base_raw']) / Decimal(10 ** DECIMALS))
        series.append({'ts': m['ts'], 'source': 'pool', 'price_sol': _fmt(spot), 'progress': None})
    series.sort(key=lambda row: (row['ts'], 0 if row['source'] == 'curve' else 1))
    return {'mint': mint, 'curve': curve, 'paths': paths, 'ends': ends, 'series': series,
            'curve_marks': len(marks), 'pool_marks': len([s for s in series if s['source'] == 'pool'])}


# ------------------------------------------------------------------------------------------------------------- wiring
def build(cfg, *, state_dir, keys, code_version, clock=time.time, transport_kwargs=None):
    """The recorder of a validated (``config``) lean.json ``curves`` block (LOW lane, own file), or None when disabled."""
    if cfg is None or not cfg['enabled']:
        return None
    from lean import providers
    helius = providers.low(keys, **dict(transport_kwargs or {})).helius
    return CurveRecorder(db=CurveStore(db_path(cfg, state_dir), clock=clock), helius=helius, cfg=cfg, code_version=code_version, clock=clock)


def summary(db):
    """Read-only report of a curves.sqlite: counts, ends by reason, gaps by cause and credits spent."""
    with _open_ro(db) as c:
        one = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        out = {'paths': one('SELECT COUNT(*) FROM curve_paths'), 'marks': one('SELECT COUNT(*) FROM curve_marks'),
               'live': one('SELECT COUNT(*) FROM curve_paths p WHERE NOT EXISTS (SELECT 1 FROM curve_ends e WHERE e.curve=p.curve AND e.segment=p.segment)'),
               'ends': dict(c.execute('SELECT why, COUNT(*) FROM curve_ends GROUP BY why')),
               'gaps': dict(c.execute('SELECT cause, COUNT(*) FROM curve_poll_gaps GROUP BY cause')),
               'polls': one('SELECT COUNT(*) FROM curve_polls'), 'calls': one('SELECT COALESCE(SUM(calls),0) FROM curve_polls'),
               'credits': one('SELECT COALESCE(SUM(credits),0) FROM curve_polls'),
               'first_poll': one('SELECT MIN(ts) FROM curve_polls'), 'last_poll': one('SELECT MAX(ts) FROM curve_polls')}
        out['graduated_with_final_mark'] = one('SELECT COUNT(*) FROM curve_ends e WHERE e.why=? AND EXISTS (SELECT 1 FROM curve_marks m WHERE m.curve=e.curve '
                                               'AND m.segment=e.segment AND m.complete=1)', 'GRADUATED')
    return out


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description='Bonding-curve recorder tools (paper only, read-only)')
    sub = p.add_subparsers(dest='command', required=True)
    r = sub.add_parser('report', help='summary of a curves.sqlite plus the calls/hour and credits/month estimate')
    r.add_argument('--db', required=True)
    r.add_argument('--config', help='lean.json: use its curves block for the estimate (default: the built-in defaults)')
    a = p.parse_args(argv)
    try:
        raw = None
        if a.config:
            raw = json.loads(Path(a.config).read_text()).get('curves')
        cfg = config({**(raw or {}), 'enabled': True})
        print(json.dumps({'status': 'OK', 'summary': summary(a.db), 'estimate': estimate(cfg)}, sort_keys=True, default=str))
        return 0
    except Exception as error:                                # noqa: BLE001
        print(json.dumps({'status': 'ERROR', 'code': type(error).__name__, 'detail': str(error)[:200]}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
