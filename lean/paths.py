"""L07R/L07R2 price-path recorder: strategy-tuning data for every candidate that passed the HAZARD checks, entered or not.

PAPER ONLY and read-only towards the chain: one batched ``getMultipleAccounts`` of pool vault accounts on the shared LOW
provider lane (``lean.providers.low``), nothing else. A recorder failure NEVER reaches the trader: every public method
swallows its own failures, counts them in ``health()`` and returns.

Storage (L07R2): its OWN SQLite file, ``paths.sqlite`` in the state dir (``paths.db``), with its own connection and lock.
The trader's append-only accounting store ``lean.sqlite`` gets NO path rows, and the recorder never reads or locks it:
the entry outcome comes from the runner's memory (``outcome``). Plain indexed tables, append-only by convention:
  paths       one row per recorded mint: start/end time, entered, stage + reasons it was not entered, screen reasons,
              holders_checked, the screen features (JSON) and the target (vault keys, reference quantity and cost)
  path_marks  (mint, ts, slot, base_raw, quote_raw, net_sol, liquidity_sol, status), index (mint, ts)
  path_gaps   (mint, ts, cause): a due mark that was skipped (shed, HTTP_429, budget, a provider code, stopped,
              write_failed, disk_low), index (mint, ts)
  path_ends   (mint, ts, why, marks, gaps): COMPLETE after path_hours, EXPIRED_WHILE_DOWN on resume
The live paths are ``paths`` without a ``path_ends`` row: a restart resumes them from the file. When the file passes
``max_db_gb`` it is rolled over: renamed to ``paths-YYYYMMDD[-n].sqlite`` (logged) and a fresh ``paths.sqlite`` starts
with the live paths carried over (``carried_from``). Below ``min_free_gb`` of free disk nothing is written (gaps).

Which candidates (``eligible``): a screen that PASSED, or one rejected ONLY for the soft market reasons (market cap or
liquidity outside the band). Hazard rejects and failed screens are never recorded. Every soft entry outcome after a
passed screen is recorded: cost cap, portfolio limits, entry throttle, cooldown, a failed quote, entries stopped. A
candidate queued for a retry is decided on its final attempt. A market-band reject stops the screen before the holder
check, so those paths carry ``holders_checked = 0``.

``net_sol`` is ``lean.adapters.mark`` (THE function the live position loop marks with) of the reference quantity: the
filled quantity for an entered candidate, else the tokens ``ref_size_sol`` buys at the path-start reserves.
``liquidity_sol`` = 2 x (quote vault - the pool fees seen at the screen) in SOL.

Budget: at most 100 ACCOUNTS per call (the RPC limit) = 50 pools. The low lane never waits; when it has no token right
now, THIS thread paces itself within 80% of ``interval_s``. A shed (429 / -32005 on any lane) or the exhausted budget
ends the poll; the skipped paths get ``path_gaps`` rows and are polled FIRST next time (the order rotates), so the
newest paths are not the ones always dropped. ``stop`` is checked between calls.
"""
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from lean import adapters as A
from lean.providers import LANE_SHED, MAX_MULTIPLE_ACCOUNTS, ProviderError, _pubkey

log = logging.getLogger('lean.paths')

SOFT_SCREEN_REASONS = frozenset({'MARKET_CAP_BELOW_MIN', 'MARKET_CAP_ABOVE_MAX', 'LIQUIDITY_BELOW_MIN'})
ACCOUNTS_PER_POOL = 2
POOLS_PER_CALL = MAX_MULTIPLE_ACCOUNTS // ACCOUNTS_PER_POOL          # 50 pools = 100 accounts
PACE_BUDGET = 0.8            # a poll may pace its calls over at most 80% of the interval
GB = 1024 ** 3
DEFAULTS = {'enabled': False, 'db': None, 'path_hours': 6.0, 'interval_s': 15.0, 'ref_size_sol': '0.2', 'max_paths': 2000,
            'max_db_gb': 2.0, 'min_free_gb': 2.0}


def config(raw):
    """lean.json ``paths`` -> validated settings. Absent = disabled. Unknown keys are refused."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError('paths must be an object')
    if set(raw) - set(DEFAULTS) - {'_comment'}:
        raise ValueError('paths: unknown keys %s' % sorted(set(raw) - set(DEFAULTS) - {'_comment'}))
    out = {k: raw.get(k, v) for k, v in DEFAULTS.items()}
    if type(out['enabled']) is not bool:
        raise ValueError('paths.enabled must be true or false')
    if out['db'] is not None and (not isinstance(out['db'], str) or not out['db'].strip()):
        raise ValueError('paths.db must be a path or null')
    for key, low, high in (('path_hours', 0.01, 72), ('interval_s', 1, 3600), ('max_db_gb', 0.001, 1000),
                           ('min_free_gb', 0, 1000)):
        if isinstance(out[key], bool) or not isinstance(out[key], (int, float)) or not low <= out[key] <= high:
            raise ValueError('paths.%s out of range' % key)
        out[key] = float(out[key])
    if type(out['max_paths']) is not int or not 1 <= out['max_paths'] <= 100_000:
        raise ValueError('paths.max_paths out of range')
    try:
        size = Decimal(str(out['ref_size_sol']))
    except ArithmeticError:
        raise ValueError('paths.ref_size_sol must be a decimal') from None
    if isinstance(out['ref_size_sol'], bool) or not size.is_finite() or not Decimal('0.001') <= size <= 1000:
        raise ValueError('paths.ref_size_sol out of range')
    out['ref_size_sol'] = str(size)
    return out


def db_path(cfg, state_dir):
    """``paths.db`` (absolute, or relative to the state dir), default ``<state-dir>/paths.sqlite``."""
    value = cfg.get('db') or 'paths.sqlite'
    path = Path(value)
    return path if path.is_absolute() else Path(state_dir) / path


def eligible(screen):
    """True when the screen shows no hazard: it passed, or it was rejected ONLY for soft market-band reasons."""
    if screen is None or getattr(screen, 'error', None):
        return False
    if screen.passed:
        return True
    reasons = tuple(screen.reasons or ())
    return bool(reasons) and all(r in SOFT_SCREEN_REASONS for r in reasons)


def outcome(screen, *, entry_reasons=None, fill=None, error=None):
    """(stage, reasons, entered) of one handled candidate, from what the runner KNOWS in memory (no store reads):
    the screen, the reasons of its last entry decision (pre-quote gate or round trip), the BUY fill, the error code."""
    if not screen.passed:
        return 'screen', list(screen.reasons), False
    if fill is not None:
        return 'entry', [], True
    if entry_reasons:
        reasons = list(entry_reasons)
        return 'entry', (['NOT_FILLED'] if reasons == ['ENTRY'] else reasons), False
    if error:
        return 'quote', ['ENTRY_FAILED:' + str(error)], False
    return 'entry', ['ENTRIES_STOPPED'], False


@dataclass(frozen=True)
class Target:
    mint: str
    pool: str
    base_vault: str
    quote_vault: str
    decimals: int
    fee_raw: int            # pool fees in the quote vault at the screen (excluded from liquidity_sol)
    qty_raw: int            # the reference quantity marked by adapters.mark
    cost_lamports: int      # what that quantity cost (net_sol / cost = the strategy's mark ratio)
    basis: str              # 'fill' (entered) or 'ref_size'


@dataclass
class _Path:
    target: Target
    started: float
    ends: float
    marks: int = 0
    gaps: int = 0


# ------------------------------------------------------------------------------------------------------ the file
SCHEMA = (
    'CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS paths(mint TEXT PRIMARY KEY, candidate_id INTEGER, start_ts REAL NOT NULL, '
    'ends_ts REAL NOT NULL, entered INTEGER NOT NULL, stage TEXT, reason TEXT NOT NULL, screen_reasons TEXT NOT NULL, '
    'holders_checked INTEGER NOT NULL, features TEXT NOT NULL, target TEXT NOT NULL, carried_from TEXT, '
    'code_version TEXT NOT NULL, strategy_version TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS path_marks(mint TEXT NOT NULL, ts REAL NOT NULL, slot INTEGER, base_raw INTEGER, '
    'quote_raw INTEGER, net_sol TEXT, liquidity_sol TEXT, status TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS path_marks_mint_ts ON path_marks(mint, ts)',
    'CREATE TABLE IF NOT EXISTS path_gaps(mint TEXT NOT NULL, ts REAL NOT NULL, cause TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS path_gaps_mint_ts ON path_gaps(mint, ts)',
    'CREATE TABLE IF NOT EXISTS path_ends(mint TEXT PRIMARY KEY, ts REAL NOT NULL, why TEXT NOT NULL, marks INTEGER, '
    'gaps INTEGER)',
)
PATH_COLUMNS = ('mint', 'candidate_id', 'start_ts', 'ends_ts', 'entered', 'stage', 'reason', 'screen_reasons',
                'holders_checked', 'features', 'target', 'carried_from', 'code_version', 'strategy_version')


class PathStore:
    """``paths.sqlite``: the recorder's own file, connection and lock. Never the trader's ``lean.sqlite``."""

    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self._lock = threading.RLock()
        self.db = None
        self._open()

    def _open(self):
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ValueError('paths db must be a regular file')
        if not self.path.parent.is_dir():
            raise ValueError('paths db directory missing')
        if not self.path.exists():
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')        # research data: a crash may lose the last poll, never more
        self.db.execute('PRAGMA busy_timeout=5000')
        self._tx(lambda: [self.db.execute(sql) for sql in SCHEMA]
                 + [self.db.execute("INSERT OR IGNORE INTO meta VALUES('schema','paths-1')")])

    def _tx(self, function):
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

    def insert_path(self, row):
        """False when the mint already has a path in this file."""
        def write():
            if self.db.execute('SELECT 1 FROM paths WHERE mint=?', (row['mint'],)).fetchone():
                return False
            self.db.execute('INSERT INTO paths(%s) VALUES(%s)' % (','.join(PATH_COLUMNS), ','.join('?' * len(PATH_COLUMNS))),
                            tuple(row.get(c) for c in PATH_COLUMNS))
            return True
        return self._tx(write)

    def add(self, marks=(), gaps=(), ends=()):
        """One transaction: mark tuples, gap tuples ``(mint, ts, cause)``, end tuples ``(mint, ts, why, marks, gaps)``."""
        def write():
            if marks:
                self.db.executemany('INSERT INTO path_marks(mint,ts,slot,base_raw,quote_raw,net_sol,liquidity_sol,status) '
                                    'VALUES(?,?,?,?,?,?,?,?)', marks)
            if gaps:
                self.db.executemany('INSERT INTO path_gaps(mint,ts,cause) VALUES(?,?,?)', gaps)
            if ends:
                self.db.executemany('INSERT OR IGNORE INTO path_ends(mint,ts,why,marks,gaps) VALUES(?,?,?,?,?)', ends)
        if marks or gaps or ends:
            self._tx(write)

    def live_paths(self):
        """Rows of ``paths`` without an end, oldest first."""
        with self._lock:
            cur = self.db.execute('SELECT %s FROM paths WHERE mint NOT IN (SELECT mint FROM path_ends) ORDER BY start_ts, mint'
                                  % ','.join(PATH_COLUMNS))
            return [dict(zip(PATH_COLUMNS, r)) for r in cur.fetchall()]

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
        """Rename this file to ``paths-YYYYMMDD[-n].sqlite`` and start a fresh one carrying the live paths. Returns the
        archive path."""
        with self._lock:
            live = self.live_paths()
            self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            self.db.close()
            day = datetime.fromtimestamp(self.clock(), timezone.utc).strftime('%Y%m%d')
            archive, n = self.path.with_name('paths-%s.sqlite' % day), 1
            while archive.exists():
                archive, n = self.path.with_name('paths-%s-%d.sqlite' % (day, n)), n + 1
            os.rename(self.path, archive)
            for suffix in ('-wal', '-shm'):
                try:
                    os.unlink(str(self.path) + suffix)
                except FileNotFoundError:
                    pass
            self._open()
            for row in live:
                self.insert_path(dict(row, carried_from=archive.name))
            return archive

    def close(self):
        with self._lock:
            if self.db is not None:
                self.db.close()


class _Stopped(Exception):
    """Internal: ``stop`` was set while this poll was pacing."""


# ------------------------------------------------------------------------------------------------------ the recorder
class PathRecorder:
    def __init__(self, *, db, helius, pcfg, code_version, strategy_version, pool_fee_bps=25, path_hours=6.0,
                 interval_s=15.0, ref_size_sol='0.2', max_paths=2000, max_db_gb=2.0, min_free_gb=2.0, clock=time.time,
                 sleep=time.sleep):
        self.db, self.helius, self.pcfg, self.clock, self.sleep = db, helius, pcfg, clock, sleep
        self.code_version, self.strategy_version = code_version, strategy_version
        self.pool_fee_bps = int(pool_fee_bps)
        self.path_s, self.interval_s = float(path_hours) * 3600, float(interval_s)
        self.ref_lamports = A.lamports(Decimal(str(ref_size_sol)))
        self.max_paths = int(max_paths)
        self.max_db_bytes, self.min_free_bytes = float(max_db_gb) * GB, float(min_free_gb) * GB
        self._paths = {}                           # mint -> _Path
        self._cursor = 0                           # rotation: the next poll starts where the last one was cut short
        self._floor_bytes = 0                      # file size right after the last rollover (the carried paths)
        self._lock = threading.Lock()              # in-memory state only: never held across I/O
        self.stats = {'started': 0, 'resumed': 0, 'ended': 0, 'polls': 0, 'calls': 0, 'marks': 0, 'gaps': 0, 'shed': 0,
                      'paced': 0, 'errors': 0, 'write_failures': 0, 'hazard_skipped': 0, 'duplicates': 0,
                      'capacity_dropped': 0, 'rollovers': 0, 'disk_low': 0}
        self.errors_by_code = {}
        self.last_error = None
        self.last_poll_at = None

    # -- bookkeeping ----------------------------------------------------------------------------------------------
    def _bump(self, name, n=1):
        with self._lock:
            self.stats[name] += n

    def _fail(self, code, error=None):
        code = str(getattr(error, 'code', None) or code)[:64]
        with self._lock:
            self.stats['errors'] += 1
            self.errors_by_code[code] = self.errors_by_code.get(code, 0) + 1
            self.last_error = code
        log.debug('path recorder: %s (%s)', code, type(error).__name__ if error is not None else '-')

    def active(self):
        with self._lock:
            return {m: p.started for m, p in self._paths.items()}

    def health(self):
        with self._lock:
            out = {'enabled': True, 'active_paths': len(self._paths), 'last_poll_at': self.last_poll_at,
                   'last_error': self.last_error, 'errors_by_code': dict(self.errors_by_code), **self.stats}
        out['db'] = str(self.db.path)
        out['db_bytes'] = self.db.size_bytes()
        limiter = getattr(getattr(self.helius, 'transport', None), 'limiter', None)
        if isinstance(getattr(limiter, 'stats', None), dict):
            out['low_lane'] = dict(limiter.stats)
        return out

    # -- starting a path ------------------------------------------------------------------------------------------
    def on_candidate(self, note):
        """Runner hook after a candidate's FINAL attempt. ``note`` is runner memory: ``{'mint', 'cid', 'screen',
        'entry_reasons', 'fill', 'error'}``. Never touches the trader's store; never raises. True when a path started."""
        try:
            screen = note['screen']
            if not eligible(screen):
                self._bump('hazard_skipped')
                return False
            stage, reasons, entered = outcome(screen, entry_reasons=note.get('entry_reasons'), fill=note.get('fill'),
                                              error=note.get('error'))
            return self.start(note['mint'], note.get('cid'), screen.features, entered=entered, stage=stage, reasons=reasons,
                              screen_reasons=list(screen.reasons or ()), fill=note.get('fill') if entered else None)
        except Exception as error:                                     # noqa: BLE001 - never reaches the trader
            self._fail('START_FAILED', error)
            return False

    def _target(self, mint, features, fill):
        base_vault, quote_vault = _pubkey(features['pool_base_token_account']), _pubkey(features['pool_quote_token_account'])
        base0, gross0, spendable0 = (int(features[k]) for k in ('base_reserve_raw', 'quote_gross_raw', 'quote_spendable_raw'))
        if base0 <= 0 or gross0 <= 0 or not 0 <= spendable0 <= gross0:
            raise ValueError('pool reserves at the screen are unusable')
        if fill is not None:                                           # the position's cost basis = paid + fee
            qty, cost, basis = int(fill.qty_raw), int(fill.sol_lamports) + int(fill.fee_lamports), 'fill'
        else:
            spent = self.ref_lamports * (10_000 - self.pool_fee_bps) // 10_000
            qty, cost, basis = base0 * spent // (gross0 + spent), self.ref_lamports, 'ref_size'
        if qty <= 0:
            raise ValueError('reference quantity rounds to nothing')
        return Target(mint=_pubkey(mint), pool=str(features.get('pool')), base_vault=base_vault, quote_vault=quote_vault,
                      decimals=int(features['decimals']), fee_raw=gross0 - spendable0, qty_raw=qty, cost_lamports=cost,
                      basis=basis)

    def start(self, mint, candidate_id, features, *, entered, stage, reasons, screen_reasons=(), fill=None):
        """Begin recording ``mint`` (no provider I/O: the vault keys come from the screen). Never raises."""
        try:
            target = self._target(mint, features, fill)
            now = self.clock()
            with self._lock:
                if mint in self._paths:
                    self.stats['duplicates'] += 1
                    return False
                if len(self._paths) >= self.max_paths:
                    self.stats['capacity_dropped'] += 1
                    return False
            row = {'mint': mint, 'candidate_id': candidate_id, 'start_ts': now, 'ends_ts': now + self.path_s,
                   'entered': int(bool(entered)), 'stage': None if entered else stage,
                   'reason': json.dumps([] if entered else list(reasons)), 'screen_reasons': json.dumps(list(screen_reasons)),
                   'holders_checked': int(features.get('holder_check') == 'OK'),
                   'features': json.dumps(_jsonable(features), sort_keys=True, default=str),
                   'target': json.dumps(asdict(target), sort_keys=True), 'carried_from': None,
                   'code_version': self.code_version, 'strategy_version': self.strategy_version}
            if not self.db.insert_path(row):
                self._bump('duplicates')
                return False
            with self._lock:
                self._paths.setdefault(mint, _Path(target, now, now + self.path_s))
                self.stats['started'] += 1
            return True
        except Exception as error:                                     # noqa: BLE001
            self._fail('START_FAILED', error)
            return False

    def resume(self):
        """After a restart: the live paths of the file (``paths`` without an end); expired ones get their end row.
        Never raises; returns the number of paths resumed."""
        try:
            now, restored, expired = self.clock(), 0, []
            for row in self.db.live_paths():
                target = Target(**json.loads(row['target']))
                if row['ends_ts'] <= now:
                    expired.append((row['mint'], now, 'EXPIRED_WHILE_DOWN', None, None))
                    continue
                with self._lock:
                    if row['mint'] not in self._paths:
                        self._paths[row['mint']] = _Path(target, row['start_ts'], row['ends_ts'])
                        restored += 1
            if expired:
                self.db.add(ends=expired)
                self._bump('ended', len(expired))
            self._bump('resumed', restored)
            return restored
        except Exception as error:                                     # noqa: BLE001
            self._fail('RESUME_FAILED', error)
            return 0

    # -- polling --------------------------------------------------------------------------------------------------
    def poll(self, stop=None):
        """One round: end expired paths, then ONE getMultipleAccounts per <=50 pools (low lane) and one write.
        Never raises; returns the number of marks written."""
        try:
            return self._poll(stop)
        except Exception as error:                                     # noqa: BLE001
            self._fail('POLL_FAILED', error)
            return 0

    def _poll(self, stop):
        now = self.clock()
        with self._lock:
            self.stats['polls'] += 1
            self.last_poll_at = now
            expired = [(m, p) for m, p in self._paths.items() if now >= p.ends]
            for mint, _p in expired:
                del self._paths[mint]
            ordered = sorted(self._paths.items(), key=lambda item: (item[1].started, item[0]))
            start = self._cursor % len(ordered) if ordered else 0
        ends = [(m, now, 'COMPLETE', p.marks, p.gaps) for m, p in expired]
        due = ordered[start:] + ordered[:start]
        marks, gaps, marked, first_gap = [], [], [], None
        deadline = now + self.interval_s * PACE_BUDGET
        if due and self.min_free_bytes and self._free() < self.min_free_bytes:
            self._bump('disk_low')
            self._bump('gaps', len(due))
            self._write([], [], ends)                                  # nothing else is written while the disk is low
            return 0
        offset = 0
        while offset < len(due):
            chunk = due[offset:offset + POOLS_PER_CALL]
            if stop is not None and stop.is_set():
                cause = 'stopped'
            else:
                keys = [k for _m, p in chunk for k in (p.target.base_vault, p.target.quote_vault)]
                cause = None
                try:
                    result = self._call(keys, deadline, stop)
                except _Stopped:
                    cause = 'stopped'
                except ProviderError as error:
                    if error.code == LANE_SHED:                        # nothing was sent
                        self._bump('shed')
                        cause = 'shed' if error.meta.get('why') == 'backoff' else 'budget'
                    else:
                        self._bump('calls')
                        self._fail('PROVIDER', error)
                        cause = error.code
                except Exception as error:                             # noqa: BLE001
                    self._fail('POLL_CALL_FAILED', error)
                    cause = 'POLL_CALL_FAILED'
            if cause is None:
                self._bump('calls')
                at, slot = self.clock(), result['context']['slot']
                for i, (mint, path) in enumerate(chunk):
                    marks.append(self._mark_row(mint, path, result['value'][2 * i], result['value'][2 * i + 1], at, slot))
                    marked.append(path)
                offset += len(chunk)
                continue
            first_gap = offset if first_gap is None else first_gap
            stop_here = cause in ('stopped', 'shed', 'budget', 'HTTP_429')
            skipped = due[offset:] if stop_here else chunk
            gaps += [(mint, now, cause, path) for mint, path in skipped]
            if stop_here:
                break
            offset += len(chunk)
        with self._lock:
            self._cursor = 0 if first_gap is None or not due else (start + first_gap) % len(due)
        written = self._write(marks, [(m, ts, c) for m, ts, c, _p in gaps], ends)
        with self._lock:
            if written:
                for path in marked:
                    path.marks += 1
            else:
                gaps += [(None, None, 'write_failed', p) for p in marked]
            for *_x, path in gaps:
                path.gaps += 1
            self.stats['marks'] += len(marks) if written else 0
            self.stats['gaps'] += len(gaps)
            self.stats['ended'] += len(ends) if written else 0
        self._maybe_rollover()
        return len(marks) if written else 0

    def _write(self, marks, gaps, ends):
        try:
            self.db.add(marks=marks, gaps=gaps, ends=ends)
            return True
        except Exception as error:                                     # noqa: BLE001 - a failed write is a gap
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
            # the carried live paths alone may be large: a fresh file must at least double before it rolls again
            if size > self.max_db_bytes and size > 2 * self._floor_bytes:
                archive = self.db.rollover()
                self._floor_bytes = self.db.size_bytes()
                self._bump('rollovers')
                log.warning('paths db passed %.3f GB: rolled over to %s; a fresh %s carries the live paths',
                            self.max_db_bytes / GB, archive.name, self.db.path.name)
        except Exception as error:                                     # noqa: BLE001
            self._fail('ROLLOVER_FAILED', error)

    def _call(self, keys, deadline, stop):
        """One low-lane getMultipleAccounts. The lane never waits; when it has no token RIGHT NOW (not a shed), this
        recorder thread paces itself (sleeps one token interval, holding no lock) while the poll is inside its time
        budget. A shed window is never waited."""
        while True:
            try:
                return self.helius.get_multiple_accounts(keys)[0]
            except ProviderError as error:
                pace = self._pace_s()
                if error.code != LANE_SHED or error.meta.get('why') != 'no_token' or self.clock() + pace > deadline:
                    raise
                if stop is not None and stop.is_set():
                    raise _Stopped from None
                self._bump('paced')
                self.sleep(pace)

    def _pace_s(self):
        limiter = getattr(getattr(self.helius, 'transport', None), 'limiter', None)
        rate = getattr(limiter, 'rate', None)
        return 1.0 / rate if isinstance(rate, float) and rate > 0 else 0.5

    def _mark_row(self, mint, path, base_account, quote_account, at, slot):
        t = path.target
        status, base, quote, net, liquidity = 'OK', None, None, None, None
        if base_account is None or quote_account is None:
            status = 'ACCOUNT_MISSING'
        else:
            try:
                base, quote = A.vault_amount(base_account), A.vault_amount(quote_account)
            except A.AdapterError:
                status, base, quote = 'MALFORMED', None, None
        if status == 'OK':
            liquidity = format(A.sol(2 * max(0, quote - t.fee_raw)), 'f')
            try:
                net = format(A.mark(t.qty_raw, base, quote, pool_fee_bps=self.pool_fee_bps, pcfg=self.pcfg), 'f')
            except A.AdapterError:
                status = 'EMPTY_POOL'
        return (mint, at, slot, base, quote, net, liquidity, status)

    # -- the thread -----------------------------------------------------------------------------------------------
    def run(self, stop, *, monotonic=time.monotonic):
        """Poll every ``interval_s`` until ``stop`` (a threading.Event) is set. For its own thread; never raises."""
        while not stop.is_set():
            began = monotonic()
            try:
                self.poll(stop)
            except BaseException as error:                             # noqa: B902 - the trader must never notice
                self._fail('POLL_FAILED', error)
            stop.wait(max(0.0, self.interval_s - (monotonic() - began)))


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def build(cfg, *, state_dir, keys, pcfg, code_version, strategy_version, pool_fee_bps, clock=time.time,
          transport_kwargs=None):
    """The recorder of a validated lean.json ``paths`` config, with its own ``paths.sqlite`` and the LOW lane; None if
    disabled. Called by ``Runner.run`` only (never by ``--once`` / ``--clear-halt``)."""
    if not cfg['enabled']:
        return None
    from lean import providers
    transport_kwargs = dict(transport_kwargs or {})
    helius = providers.low(keys, **transport_kwargs).helius
    return PathRecorder(db=PathStore(db_path(cfg, state_dir), clock=clock), helius=helius, pcfg=pcfg,
                        code_version=code_version, strategy_version=strategy_version, pool_fee_bps=pool_fee_bps,
                        path_hours=cfg['path_hours'], interval_s=cfg['interval_s'], ref_size_sol=cfg['ref_size_sol'],
                        max_paths=cfg['max_paths'], max_db_gb=cfg['max_db_gb'], min_free_gb=cfg['min_free_gb'],
                        clock=clock, sleep=transport_kwargs.get('sleep', time.sleep))
