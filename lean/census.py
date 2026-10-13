"""C3: launchpad census (recording only). Every ``interval_s`` seconds, how many NEW pools/tokens did each launchpad create?

PAPER ONLY and read-only: ``getSignaturesForAddress`` and ``getTransaction`` on the shared LOW lane (a call goes out at once or
is shed and sends nothing), no signing, no sending. Nothing in the trader reads these rows. They go into their OWN
``census.sqlite`` (the lean store is untouched) as append-only rows, one per launchpad per interval:

  ts, launchpad, program, signatures, failed, sampled, creates_in_sample, new_count, exact, truncated, sample_mints, note

* ``signatures``  EXACT count of transactions that touched the program since the cursor (``until`` = the newest signature of the
                  previous poll). A poll that fills ``sig_limit`` x ``max_pages`` without reaching the cursor sets
                  ``truncated`` and ``signatures`` is then a lower bound.
* ``new_count``   creates. A create is an instruction log line that matches one of the launchpad's configured patterns while the
                  launchpad's program is the innermost running program (``match_creates``). Counting creates needs the
                  transaction logs, which cost one call each, so at most ``max_tx_per_poll`` successful transactions per launchpad
                  are fetched, evenly spread over the new ones. ``exact`` = 1 when every successful transaction was fetched;
                  otherwise ``new_count`` is the extrapolation ``round(creates_in_sample * ok / sampled)`` (null when nothing was
                  sampled) and ``creates_in_sample`` / ``sampled`` carry the raw evidence.
* ``sample_mints`` up to 3 mints of the sampled create transactions (non-SOL mints in ``postTokenBalances``); empty when unknown.
* The first poll of a launchpad only sets its cursor (``note = 'BASELINE'``): history is never counted as "new".
* pump.fun graduations are not polled on chain: they are counted from the discovery database (read-only, its own cursor), exactly.

The patterns are CONFIG, so a new program is a config entry. The two defaults below come from the public program IDLs as the
author remembers them and were NOT verified against a live transaction (no provider access from the worker): run
``match_creates`` on the ``getTransaction`` of a known create before trusting the counts.

Budget (Helius standard calls are 1 credit): the worst case per month is
``launchpads * (max_pages + max_tx_per_poll) * 30 * 86400 / interval_s`` and the config is REFUSED when that exceeds
``credit_budget_month`` (default 50 000). Defaults: 2 * (1 + 4) * 4320 = 43 200. A runtime hourly cap of
``credit_budget_month / 720`` calls enforces it too. A shed or an error skips that launchpad for this poll and writes nothing for
it (no fake zero); the next poll resumes from the same cursor, so nothing is lost, only delayed.
"""
import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from lean import candidates as C
from lean.flow import HourBudget, LowCalls, Stop
from lean.providers import MAX_TX_VERSION, ProviderError

SOL_MINT = 'So11111111111111111111111111111111111111112'
LABEL = 'CENSUS'
PUMP_NAME = 'pump_fun_graduations'
DEFAULT_LAUNCHPADS = (
    {'name': 'raydium_launchlab', 'program': 'LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj',
     'create_log_patterns': ['Instruction: Initialize']},
    {'name': 'meteora_dbc', 'program': 'dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN',
     'create_log_patterns': ['Instruction: InitializeVirtualPoolWithSplToken', 'Instruction: InitializeVirtualPoolWithToken2022']},
)
DEFAULTS = {'enabled': False, 'db': 'census.sqlite', 'interval_s': 600, 'sig_limit': 100, 'max_pages': 1, 'max_tx_per_poll': 4,
            'credit_budget_month': 50_000, 'pace_s': 0.5, 'shed_wait_s': 5.0, 'reserve_tokens': 2, 'pump_graduations': True,
            'launchpads': None}
MONTH_SECONDS = 30 * 86400
MAX_SAMPLE_MINTS = 3


class ConfigError(ValueError):
    pass


def _name_ok(name):
    return isinstance(name, str) and 1 <= len(name) <= 40 and name.replace('_', '').isalnum() and name[0].isalpha() and name == name.lower()


def config(raw):
    """lean.json ``census`` -> validated settings. Absent / null = disabled; unknown keys and wrong types are refused."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ConfigError('census must be an object')
    unknown = set(raw) - set(DEFAULTS) - {'_comment'}
    if unknown:
        raise ConfigError('census: unknown keys %s' % sorted(unknown))
    out = {k: raw.get(k, v) for k, v in DEFAULTS.items()}
    for key in ('enabled', 'pump_graduations'):
        if type(out[key]) is not bool:
            raise ConfigError('census.%s must be true or false' % key)
    db = out['db']
    if not isinstance(db, str) or not db or '/' in db or '\\' in db or db in ('.', '..') or not db.endswith('.sqlite'):
        raise ConfigError('census.db must be a plain file name ending in .sqlite (it lives in the state dir)')
    for key, low, high in (('interval_s', 60, 86400), ('sig_limit', 1, 1000), ('max_pages', 1, 5), ('max_tx_per_poll', 0, 20),
                           ('credit_budget_month', 1000, 10_000_000), ('reserve_tokens', 0, 10)):
        if type(out[key]) is not int or not low <= out[key] <= high:
            raise ConfigError('census.%s must be an integer in [%d, %d]' % (key, low, high))
    for key, low, high in (('pace_s', 0.05, 60), ('shed_wait_s', 0, 600)):
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or not low <= value <= high:
            raise ConfigError('census.%s out of range' % key)
        out[key] = float(value)
    pads = DEFAULT_LAUNCHPADS if out['launchpads'] is None else out['launchpads']
    if not isinstance(pads, (list, tuple)) or len(pads) > 20:
        raise ConfigError('census.launchpads must be a list of at most 20')
    clean, names = [], {PUMP_NAME}
    for pad in pads:
        if not isinstance(pad, dict) or set(pad) - {'name', 'program', 'create_log_patterns'}:
            raise ConfigError('census.launchpads entries: name, program, create_log_patterns')
        name, program, patterns = pad.get('name'), pad.get('program'), pad.get('create_log_patterns')
        if not _name_ok(name) or name in names:
            raise ConfigError('census launchpad name must be unique lowercase [a-z0-9_]')
        if not isinstance(program, str) or not 32 <= len(program) <= 44:
            raise ConfigError('census launchpad %s: program must be a base58 address' % name)
        if (not isinstance(patterns, (list, tuple)) or not 1 <= len(patterns) <= 10
                or any(not isinstance(p, str) or not 3 <= len(p) <= 120 for p in patterns)):
            raise ConfigError('census launchpad %s: 1-10 create_log_patterns of 3-120 characters' % name)
        names.add(name)
        clean.append({'name': name, 'program': program, 'create_log_patterns': list(patterns)})
    out['launchpads'] = clean
    worst = worst_case_credits_per_month(out)
    if worst > out['credit_budget_month']:
        raise ConfigError('census worst case %d credits/month exceeds credit_budget_month %d' % (worst, out['credit_budget_month']))
    return out


def worst_case_credits_per_month(cfg):
    polls = MONTH_SECONDS / cfg['interval_s']
    return int(len(cfg['launchpads']) * (cfg['max_pages'] + cfg['max_tx_per_poll']) * polls)


# ------------------------------------------------------------------------------------------------------------- matching
def match_creates(transaction, program, patterns):
    """Number of create instructions of ``program`` in a ``getTransaction`` result (0 for a failed or unreadable one).

    Walks ``meta.logMessages`` with the program-invocation stack (``Program X invoke [n]`` ... ``Program X success``) and counts a
    ``Program log: <text>`` line when the innermost running program is ``program`` and ``text`` contains a pattern. A pattern in
    another program's log (e.g. the token program's own ``Instruction: InitializeMint``) never counts."""
    try:
        meta = transaction['meta']
        if meta is None or meta.get('err') is not None:
            return 0
        logs = meta['logMessages']
    except (TypeError, KeyError, AttributeError):
        return 0
    if not isinstance(logs, list):
        return 0
    stack, count = [], 0
    for line in logs:
        if not isinstance(line, str):
            continue
        parts = line.split(' ')
        if len(parts) >= 4 and parts[0] == 'Program' and parts[2] == 'invoke':
            stack.append(parts[1])
        elif len(parts) >= 3 and parts[0] == 'Program' and parts[2] in ('success', 'failed:'):
            if stack:
                stack.pop()
        elif line.startswith('Program log: ') and stack and stack[-1] == program:
            text = line[len('Program log: '):]
            if any(p in text for p in patterns):
                count += 1
    return count


def sample_mints(transaction):
    """Distinct non-SOL mints in ``meta.postTokenBalances`` (the new token of a create), at most ``MAX_SAMPLE_MINTS``."""
    try:
        balances = transaction['meta']['postTokenBalances']
    except (TypeError, KeyError):
        return []
    out = []
    for item in balances if isinstance(balances, list) else ():
        mint = item.get('mint') if isinstance(item, dict) else None
        if isinstance(mint, str) and mint != SOL_MINT and mint not in out:
            out.append(mint)
    return out[:MAX_SAMPLE_MINTS]


def spread(items, k):
    """``k`` items evenly spread over ``items`` (all of them when k >= len), deterministic, order kept."""
    n = len(items)
    if k <= 0:
        return []
    if k >= n:
        return list(items)
    return [items[(2 * i + 1) * n // (2 * k)] for i in range(k)]


# --------------------------------------------------------------------------------------------------------------- storage
SCHEMA = (
    'CREATE TABLE IF NOT EXISTS census(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, launchpad TEXT NOT NULL, '
    'program TEXT, signatures INTEGER, failed INTEGER, sampled INTEGER, creates_in_sample INTEGER, new_count INTEGER, '
    'exact INTEGER NOT NULL CHECK(exact IN (0,1)), truncated INTEGER NOT NULL CHECK(truncated IN (0,1)), sample_mints TEXT NOT NULL, '
    "note TEXT NOT NULL, label TEXT NOT NULL CHECK(label='CENSUS'), code_version TEXT NOT NULL)",
    'CREATE TABLE IF NOT EXISTS cursors(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, launchpad TEXT NOT NULL, '
    'cursor TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS census_pad ON census(launchpad, id)',
    'CREATE INDEX IF NOT EXISTS cursors_pad ON cursors(launchpad, id)',
    *[f'CREATE TRIGGER IF NOT EXISTS {t}_no_{a.lower()} BEFORE {a} ON {t} BEGIN SELECT RAISE(ABORT, \'{t} is append-only\'); END'
      for t in ('census', 'cursors') for a in ('UPDATE', 'DELETE')],
)


class CensusDb:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        for statement in SCHEMA:
            self.db.execute(statement)

    def close(self):
        self.db.close()

    def cursor(self, launchpad):
        with self.lock:
            row = self.db.execute('SELECT cursor FROM cursors WHERE launchpad=? ORDER BY id DESC LIMIT 1', (launchpad,)).fetchone()
        return row[0] if row else None

    def add_cursor(self, launchpad, cursor, ts):
        with self.lock:
            self.db.execute('INSERT INTO cursors(ts,launchpad,cursor) VALUES(?,?,?)', (ts, launchpad, str(cursor)))

    def add_row(self, ts, launchpad, program, code_version, *, signatures=None, failed=None, sampled=None, creates_in_sample=None,
                new_count=None, exact=0, truncated=0, mints=(), note=''):
        with self.lock:
            return self.db.execute(
                'INSERT INTO census(ts,launchpad,program,signatures,failed,sampled,creates_in_sample,new_count,exact,truncated,'
                'sample_mints,note,label,code_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (ts, launchpad, program, signatures, failed, sampled, creates_in_sample, new_count, 1 if exact else 0,
                 1 if truncated else 0, json.dumps(list(mints)), note, LABEL, code_version)).lastrowid

    def rows(self, launchpad=None):
        with self.lock:
            sql = 'SELECT * FROM census' + (' WHERE launchpad=?' if launchpad else '') + ' ORDER BY id'
            cur = self.db.execute(sql, (launchpad,) if launchpad else ())
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------------------------------------------- census
class Census:
    def __init__(self, *, db, helius, cfg, code_version, discovery_db=None, clock=time.time, sleep=time.sleep, stop=None):
        self.db, self.helius, self.cfg, self.code_version = db, helius, cfg, code_version
        self.discovery_db, self.clock, self.sleep = discovery_db, clock, sleep
        self.stop = stop or threading.Event()
        self.budget = HourBudget(max(1, cfg['credit_budget_month'] // 720), clock)
        self.counts = {'polls': 0, 'rows': 0, 'calls': 0, 'shed': 0, 'errors': 0, 'baselines': 0}
        self.last_poll = None
        self.errors_by_code = {}

    def health(self):
        return {'enabled': True, 'counts': dict(self.counts), 'last_poll': self.last_poll, 'calls_last_hour': self.budget.used(),
                'hourly_call_cap': self.budget.limit, 'worst_case_credits_per_month': worst_case_credits_per_month(self.cfg),
                'errors_by_code': dict(self.errors_by_code)}

    def _error(self, code):
        self.counts['errors'] += 1
        self.errors_by_code[code] = self.errors_by_code.get(code, 0) + 1

    def run_loop(self):
        while not self.stop.is_set():
            try:
                self.poll_once()
            except Exception:                                    # recording only: never raises into the runner
                self._error('POLL_FAILED')
            self.stop.wait(self.cfg['interval_s'])

    def poll_once(self):
        self.last_poll = self.clock()
        self.counts['polls'] += 1
        if self.cfg['pump_graduations'] and self.discovery_db:
            self._pump()
        for pad in self.cfg['launchpads']:
            if self.stop.is_set():
                break
            try:
                self._launchpad(pad)
            except sqlite3.Error:
                self._error('CENSUS_DB_FAILED')

    # -- pump.fun graduations: exact, from the discovery database (read-only, own cursor)
    def _pump(self):
        raw = self.db.cursor(PUMP_NAME)
        try:
            cursor = int(raw) if raw is not None else None
        except ValueError:
            self._error('CURSOR_INVALID')
            return
        now = self.clock()
        try:
            if cursor is None:
                # baseline: everything that exists now is history; the cursor moves to the newest frame
                last = self._newest_seq()
                self.db.add_cursor(PUMP_NAME, last if last is not None else 0, now)
                self.db.add_row(now, PUMP_NAME, None, self.code_version, note='BASELINE', exact=True)
                self.counts['baselines'] += 1
                return
            found, nxt, truncated = [], cursor, False
            for _ in range(10):
                scan = C.scan_new(self.discovery_db, nxt, now=now, min_age_seconds=0, max_age_seconds=10 ** 12, limit=500)
                found += list(scan.candidates)
                moved = scan.next_cursor > nxt
                nxt = scan.next_cursor
                if not moved or len(scan.candidates) < 400:
                    break
            else:
                truncated = True
        except C.DiscoveryUnavailable:
            self._error('DISCOVERY_UNAVAILABLE')
            return
        except (OSError, ValueError, sqlite3.Error):
            self._error('DISCOVERY_INVALID')
            return
        self.db.add_row(now, PUMP_NAME, None, self.code_version, signatures=len(found), failed=0, sampled=len(found),
                        creates_in_sample=len(found), new_count=len(found), exact=not truncated, truncated=truncated,
                        mints=[c.mint for c in found[:MAX_SAMPLE_MINTS]], note='discovery')
        self.counts['rows'] += 1
        if nxt > cursor:
            self.db.add_cursor(PUMP_NAME, nxt, now)

    def _newest_seq(self):
        with closing(C._connect_ro(self.discovery_db)) as d:
            row = d.execute('SELECT MAX(seq) FROM raw_events').fetchone()
        return row[0] if row else None

    # -- on-chain launchpads
    def _launchpad(self, pad):
        name, program, patterns = pad['name'], pad['program'], pad['create_log_patterns']
        cfg, now = self.cfg, self.clock()
        cursor = self.db.cursor(name)
        pages_cap = 1 if cursor is None else cfg['max_pages']
        limit = 1 if cursor is None else cfg['sig_limit']
        calls = LowCalls(self.helius, limit=pages_cap + cfg['max_tx_per_poll'], budget=self.budget, wait=self.sleep, pace_s=cfg['pace_s'],
                         patience_s=cfg['shed_wait_s'], stop=self.stop, reserve=cfg['reserve_tokens'])
        sigs, truncated = [], False
        try:
            before = None
            for page in range(pages_cap):
                options = {'limit': limit, 'commitment': 'confirmed'}
                if cursor is not None:
                    options['until'] = cursor
                if before:
                    options['before'] = before
                batch = calls.rpc('getSignaturesForAddress', [program, options])
                self.counts['calls'] += 1
                if not isinstance(batch, list):
                    raise _Skip('RESPONSE_INVALID')
                if any(not isinstance(s, dict) or not isinstance(s.get('signature'), str) for s in batch):
                    raise _Skip('RESPONSE_INVALID')
                sigs += batch
                if len(batch) < limit:
                    break
                before = batch[-1]['signature']
            else:
                truncated = cursor is not None
        except Stop as stop:
            if stop.code == 'LANE_SHED':
                self.counts['shed'] += 1
            elif stop.code not in ('STOPPED',):
                self._error(stop.code)
            return
        except ProviderError as error:
            self._error(error.code)
            return
        except _Skip as skip:
            self._error(skip.code)
            return
        if not sigs:
            if cursor is None:
                return                                       # an idle program: still no baseline, try again next poll
            self.db.add_row(now, name, program, self.code_version, signatures=0, failed=0, sampled=0, creates_in_sample=0,
                            new_count=0, exact=True, note='')
            self.counts['rows'] += 1
            return
        newest = sigs[0]['signature']
        if cursor is None:
            self.db.add_cursor(name, newest, now)
            self.db.add_row(now, name, program, self.code_version, note='BASELINE', exact=True)
            self.counts['baselines'] += 1
            return
        ok = [s['signature'] for s in sigs if s.get('err') is None]
        failed = len(sigs) - len(ok)
        picked = spread(ok, cfg['max_tx_per_poll'])
        sampled, creates, mints = 0, 0, []
        for signature in picked:
            try:
                transaction = calls.rpc('getTransaction', [signature, {'encoding': 'json', 'maxSupportedTransactionVersion': MAX_TX_VERSION,
                                                                         'commitment': 'confirmed'}])
                self.counts['calls'] += 1
            except Stop as stop:
                if stop.code == 'LANE_SHED':
                    self.counts['shed'] += 1
                elif stop.code != 'STOPPED':
                    self._error(stop.code)
                break
            except ProviderError as error:
                self._error(error.code)
                continue
            if not isinstance(transaction, dict):
                continue                                     # not found yet / pruned: not a sample
            sampled += 1
            n = match_creates(transaction, program, patterns)
            if n:
                creates += n
                for mint in sample_mints(transaction):
                    if mint not in mints and len(mints) < MAX_SAMPLE_MINTS:
                        mints.append(mint)
        if self.stop.is_set() and sampled < len(picked):
            return                                           # a stopped poll writes nothing; the cursor did not move
        exact = sampled == len(ok)
        if exact:
            new_count = creates
        elif sampled:
            new_count = int(round(creates * len(ok) / sampled))
        else:
            new_count = None
        self.db.add_row(now, name, program, self.code_version, signatures=len(sigs), failed=failed, sampled=sampled,
                        creates_in_sample=creates, new_count=new_count, exact=exact, truncated=truncated, mints=mints,
                        note='SAMPLED' if not exact else '')
        self.db.add_cursor(name, newest, now)
        self.counts['rows'] += 1


class _Skip(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def build(cfg, *, state_dir, keys, code_version, discovery_db=None, clock=time.time, transport_kwargs=None):
    """The census of a validated ``census`` config on the shared LOW lane; None when disabled."""
    if not cfg['enabled']:
        return None
    from lean import providers
    transport_kwargs = dict(transport_kwargs or {})
    helius = providers.low(keys, **transport_kwargs).helius
    db = CensusDb(Path(state_dir) / cfg['db'])
    return Census(db=db, helius=helius, cfg=cfg, code_version=code_version, discovery_db=discovery_db, clock=clock,
                  sleep=transport_kwargs.get('sleep', time.sleep))
