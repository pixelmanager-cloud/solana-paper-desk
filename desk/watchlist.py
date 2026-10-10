"""Append-only watchlist of "not yet" paper candidates (`paper_watchlist_version: 1`).

A candidate rejected for a reason that can still change (too young, market cap or
liquidity out of range, empty five-minute window, momentum/flow not met) is scheduled
for a *fresh* re-evaluation. Every re-evaluation is a new scan with a new dispatcher
intent whose requests are charged to the normal budgets; the original scan, its
outcome and its charges are never retried, rewritten or refunded. Anything not
explicitly classified NOT_YET is PERMANENT (fail closed), and a permanent, accepted,
exhausted or expired mint can never be scheduled again: the chain trigger rejects any
row after a terminal row. Paper only; nothing here reads a provider.

Config (all scalars; absent flag means the previous behaviour is unchanged):
  paper_watchlist_version: 1
  paper_watchlist_backoff_first_seconds:  600   (delay after evaluation 1)
  paper_watchlist_backoff_second_seconds: 1800  (after evaluation 2)
  paper_watchlist_backoff_third_seconds:  7200  (after evaluation 3 and later)
  paper_watchlist_max_reevaluations:      4
"""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3

from .model import canonical, digest

VERSION = 1
# An evaluation that starts closer to its age-window end than this cannot finish preparation (the dispatcher refuses
# `Candidate expired during preparation` AFTER the intent is written, which strands an unresolved intent). The
# margin applies when SCHEDULING a re-evaluation and when SELECTING any candidate for a versioned journal.
PREPARATION_MARGIN_SECONDS = 900
# Share of a bounded no-entry table at which re-evaluations stop being scheduled (fresh hints still run).
CAPACITY_PAUSE_FRACTION = 0.8
KEY = 'paper_watchlist_version'
BACKOFF_KEYS = (('paper_watchlist_backoff_first_seconds', 600),
                ('paper_watchlist_backoff_second_seconds', 1800),
                ('paper_watchlist_backoff_third_seconds', 7200))
MAX_KEY, MAX_DEFAULT, MAX_LIMIT = 'paper_watchlist_max_reevaluations', 4, 16
MIN_BACKOFF = 60
TABLE = 'watchlist_events'
KINDS = ('ENROLLED', 'PERMANENT', 'EXPIRED', 'EXHAUSTED', 'ACCEPTED')
TERMINAL = ('PERMANENT', 'EXPIRED', 'EXHAUSTED', 'ACCEPTED')
MAX_ROWS = 100_000
HINT_KEYS = {'seq', 'payload_hash', 'raw_hash', 'received_at', 'mint', 'pool', 'signature', 'slot'}

# Exactly the spec's "not yet" conditions. Everything else is PERMANENT, including any
# code added to the engine later, until a reviewer lists it here.
NOT_YET = {
    'AGE': 'engine age window: too young now (too old expires the entry, see expiry)',
    'MARKET_CAP': 'market cap outside the configured range; can move into range',
    'LIQUIDITY': 'pool liquidity below the minimum; can grow',
    'MOMENTUM_OR_WASH': 'momentum not met; can build',
    'MOMENTUM_OR_OBSERVED_CHURN': 'observable momentum/churn not met; can change',
    'ENTRY_SCORE': 'entry/flow score below threshold; can change',
    'MARKET_PRODUCER_BLOCKED': 'market event could not be produced from the captured window',
    'HISTORY_FEATURE_EMPTY_WINDOW': 'empty five-minute trade window; trades can arrive',
    'HISTORY_REQUIRED_MEASUREMENTS_UNAVAILABLE': 'insufficient history for required measurements',
    'HISTORY_FEATURE_MOMENTUM_STALE': 'latest captured momentum too old; fresh scan re-captures',
}
# Inner producer blockers (paper_market_adapter) for a window that is empty or stale RIGHT NOW. Every other producer
# blocker (binding/integrity/identity codes) is deliberately absent => PERMANENT.
NOT_YET_PREFIXES = ('MISSING_WINDOW_MEASUREMENT:', 'STALE_WINDOW_MEASUREMENT:')
_MEASUREMENT = re.compile(r'[a-z][a-z0-9_]{0,63}')
# Documented examples only; the rule is "not in NOT_YET => PERMANENT".
PERMANENT_EXAMPLES = (
    'DANGER', 'UNVERIFIED_SAFETY', 'UNSUPPORTED_POOL', 'KNOWN_OWNERSHIP_HAZARD', 'SAFETY', 'REPEAT_DEPLOYER',
    'DATA_UNHEALTHY', 'NO_ROUTE', 'ACTIVE_MINT_AUTHORITY', 'ACTIVE_FREEZE_AUTHORITY', 'MAYHEM_POOL',
    'LIVE_FEATURE_ADAPTER_NOT_READY', 'HISTORY_FEATURE_RECORD_BYTES_EXCEEDED',
    'HISTORY_FEATURE_RECORD_COUNT_EXCEEDED', 'HISTORY_FEATURE_AGGREGATE_BYTES_EXCEEDED',
    'HISTORY_FRESH_ENTRY_REQUESTS_UNAVAILABLE', 'EXACT_FRESH_ROUNDTRIP_QUOTES_REQUIRED')

SQL = (f"CREATE TABLE {TABLE}(seq INTEGER PRIMARY KEY AUTOINCREMENT,mint TEXT NOT NULL,"
       f"evaluation INTEGER NOT NULL CHECK(evaluation BETWEEN 1 AND {MAX_LIMIT + 1}),"
       f"kind TEXT NOT NULL CHECK(kind IN {KINDS!r}),at REAL NOT NULL,scan_id TEXT NOT NULL,"
       "codes TEXT NOT NULL,classification TEXT NOT NULL CHECK(classification IN ('NOT_YET','PERMANENT','ACCEPTED')),"
       "next_eval_at REAL,hint TEXT NOT NULL,payload_hash TEXT NOT NULL,UNIQUE(mint,evaluation,kind),UNIQUE(scan_id))")
_LAST = f"(SELECT MAX(seq) FROM {TABLE} WHERE mint=NEW.mint)"
GUARDS = {
    f'{TABLE}_chain': (
        f"CREATE TRIGGER {TABLE}_chain BEFORE INSERT ON {TABLE} WHEN (SELECT count(*) FROM {TABLE})>={MAX_ROWS} OR NOT ("
        f"(NEW.evaluation=1 AND NOT EXISTS(SELECT 1 FROM {TABLE} WHERE mint=NEW.mint)) OR "
        f"EXISTS(SELECT 1 FROM {TABLE} w WHERE w.mint=NEW.mint AND w.seq={_LAST} AND w.kind='ENROLLED' AND "
        "(w.evaluation=NEW.evaluation-1 OR (NEW.kind='EXPIRED' AND w.evaluation=NEW.evaluation)) "
        "AND w.hint=NEW.hint AND w.at<=NEW.at)) "
        "OR (NEW.kind='ENROLLED')!=(NEW.next_eval_at IS NOT NULL) "
        "OR (NEW.kind='ACCEPTED')!=(NEW.classification='ACCEPTED') "
        "OR (NEW.kind='ENROLLED' AND NEW.classification!='NOT_YET') "
        "OR (NEW.kind IN ('EXPIRED','EXHAUSTED') AND NEW.classification!='NOT_YET') "
        "OR (NEW.kind='PERMANENT' AND NEW.classification!='PERMANENT') "
        "BEGIN SELECT RAISE(ABORT,'Watchlist chain'); END"),
    f'{TABLE}_update': f"CREATE TRIGGER {TABLE}_update BEFORE UPDATE ON {TABLE} BEGIN SELECT RAISE(ABORT,'Watchlist immutable'); END",
    f'{TABLE}_delete': f"CREATE TRIGGER {TABLE}_delete BEFORE DELETE ON {TABLE} BEGIN SELECT RAISE(ABORT,'Watchlist immutable'); END",
}


def selected(cfg):
    """0 when absent; 1 when explicitly and validly enabled; otherwise fail closed."""
    if KEY not in cfg:
        return 0
    if type(cfg[KEY]) is not int or cfg[KEY] != VERSION or cfg.get('mode') != 'paper':
        raise ValueError('Unsupported watchlist version')
    settings(cfg)
    return VERSION


def settings(cfg):
    delays = []
    for key, default in BACKOFF_KEYS:
        value = cfg.get(key, default)
        if type(value) is not int or not MIN_BACKOFF <= value <= 86400:
            raise ValueError('Invalid watchlist backoff')
        delays.append(value)
    if delays != sorted(delays):
        raise ValueError('Watchlist backoff must not shrink')
    limit = cfg.get(MAX_KEY, MAX_DEFAULT)
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError('Invalid watchlist re-evaluation limit')
    max_age = cfg.get('max_age_seconds')
    if type(max_age) is not int or max_age <= 0:
        raise ValueError('Engine age window required')
    return {'backoff': delays, 'max_reevaluations': limit, 'max_age_seconds': max_age}


def classify(codes):
    """'NOT_YET' only when there is at least one code and every code is listed; else PERMANENT."""
    if type(codes) not in (list, tuple) or not codes or any(type(c) is not str or not c for c in codes):
        return 'PERMANENT'
    return 'NOT_YET' if all(_not_yet(c) for c in codes) else 'PERMANENT'


def _not_yet(code):
    if code in NOT_YET:
        return True
    for prefix in NOT_YET_PREFIXES:
        if code.startswith(prefix) and _MEASUREMENT.fullmatch(code[len(prefix):]):
            return True
    return False


def delay_after(evaluation, cfg):
    delays = settings(cfg)['backoff']
    return delays[min(evaluation, len(delays)) - 1]


def expires_at(hint, cfg):
    return hint['received_at'] + settings(cfg)['max_age_seconds']


def _hint(hint):
    if type(hint) is not dict or set(hint) != HINT_KEYS:
        raise ValueError('Watchlist hint grammar')
    return canonical(hint)


def initialize(path):
    """Explicit new store only; never adopts or resets an existing file."""
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or path.exists() or not path.parent.is_dir():
        raise ValueError('Fresh canonical watchlist path required')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path, isolation_level=None)) as c:
        c.execute('PRAGMA journal_mode=DELETE')
        c.execute('BEGIN IMMEDIATE')
        for sql in (SQL, *GUARDS.values()):
            c.execute(sql)
        c.execute('COMMIT')


class Watchlist:
    def __init__(self, path, *, read_only=False):
        path = Path(path)
        info = os.lstat(path)
        if path.is_symlink() or not path.is_file() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError('Private regular single-link watchlist required')
        mode = 'ro' if read_only else 'rw'
        self.path = path
        self.db = sqlite3.connect(f'{path.as_uri()}?mode={mode}', uri=True, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        try:
            self._validate()
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def _validate(self):
        schema = dict(self.db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        if schema != {TABLE: SQL, **GUARDS}:
            raise ValueError('Watchlist schema or guards invalid')
        if self.db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
            raise ValueError('Rollback-journal watchlist required')

    def last(self, mint):
        row = self.db.execute(f'SELECT * FROM {TABLE} WHERE mint=? ORDER BY seq DESC LIMIT 1', (mint,)).fetchone()
        return None if row is None else dict(row)

    def events(self):
        return [dict(r) for r in self.db.execute(f'SELECT * FROM {TABLE} ORDER BY seq')]

    def evaluation_for(self, mint):
        """Number of the next evaluation for a mint: 1 when unknown, last+1 only after ENROLLED."""
        row = self.last(mint)
        if row is None:
            return 1
        if row['kind'] != 'ENROLLED':
            raise ValueError('Mint is not eligible for another evaluation')
        return row['evaluation'] + 1

    def due(self, now, cfg, *, limit=8):
        """Enrolled mints whose backoff elapsed and whose age window is still open, oldest due first."""
        rows = self.db.execute(
            f"SELECT * FROM {TABLE} w WHERE w.kind='ENROLLED' AND w.next_eval_at<=? AND "
            f"w.seq=(SELECT MAX(seq) FROM {TABLE} WHERE mint=w.mint) ORDER BY w.next_eval_at,w.seq LIMIT ?",
            (now, int(limit) * 4)).fetchall()
        due = []
        for row in rows:
            hint = json.loads(row['hint'])
            if now > expires_at(hint, cfg) - PREPARATION_MARGIN_SECONDS:
                continue
            due.append({'mint': row['mint'], 'evaluation': row['evaluation'] + 1, 'hint': hint,
                        'next_eval_at': row['next_eval_at']})
            if len(due) >= limit:
                break
        return due

    def record(self, *, mint, evaluation, scan_id, codes, at, hint, cfg, accepted=False):
        """Append the disposition of one evaluation; idempotent for the identical record."""
        if (type(evaluation) is not int or evaluation < 1 or type(scan_id) is not str or not scan_id
                or type(at) not in (int, float) or at < 0 or type(mint) is not str or hint.get('mint') != mint):
            raise ValueError('Watchlist record grammar')
        codes = sorted(set(codes))
        text = _hint(hint)
        cfg_settings = settings(cfg)
        if accepted:
            kind, cls, nxt = 'ACCEPTED', 'ACCEPTED', None
        else:
            cls = classify(codes)
            if cls == 'PERMANENT':
                kind, nxt = 'PERMANENT', None
            else:
                delay = delay_after(evaluation, cfg)
                if evaluation - 1 >= cfg_settings['max_reevaluations']:
                    kind, nxt = 'EXHAUSTED', None
                elif at + delay > expires_at(hint, cfg) - PREPARATION_MARGIN_SECONDS:
                    kind, nxt = 'EXPIRED', None      # the next evaluation could not finish preparation in time
                else:
                    kind, nxt = 'ENROLLED', at + delay
        body = {'mint': mint, 'evaluation': evaluation, 'kind': kind, 'at': at, 'scan_id': scan_id, 'codes': codes,
                'classification': cls, 'next_eval_at': nxt, 'hint': text}
        existing = self.db.execute(f'SELECT payload_hash FROM {TABLE} WHERE mint=? AND evaluation=? AND kind=?',
                                   (mint, evaluation, kind)).fetchone()
        if existing is not None:
            if existing[0] != digest(body):
                raise ValueError('Conflicting watchlist record')
            return kind
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute(f'INSERT INTO {TABLE}(mint,evaluation,kind,at,scan_id,codes,classification,next_eval_at,hint,payload_hash) '
                            'VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (mint, evaluation, kind, at, scan_id, canonical(codes), cls, nxt, text, digest(body)))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return kind

    def expire(self, now, cfg):
        """Append EXPIRED for enrolled mints whose age window closed; never touches earlier rows."""
        rows = self.db.execute(
            f"SELECT * FROM {TABLE} w WHERE w.kind='ENROLLED' AND "
            f"w.seq=(SELECT MAX(seq) FROM {TABLE} WHERE mint=w.mint)").fetchall()
        expired = []
        for row in rows:
            hint = json.loads(row['hint'])
            if now <= expires_at(hint, cfg):
                continue
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.db.execute(
                    f'INSERT INTO {TABLE}(mint,evaluation,kind,at,scan_id,codes,classification,next_eval_at,hint,payload_hash) '
                    "VALUES(?,?,?,?,?,?,?,NULL,?,?)",
                    (row['mint'], row['evaluation'], 'EXPIRED', max(now, row['at']), row['scan_id'] + ':expired',
                     row['codes'], 'NOT_YET', row['hint'],
                     digest({'mint': row['mint'], 'evaluation': row['evaluation'], 'kind': 'EXPIRED'})))
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            expired.append(row['mint'])
        return expired


def capacity_pressure(evidence_db):
    """Bounded no-entry tables that are >= 80% full: [{'table','rows','cap'}]. Read-only; raises on an unreadable store.

    Every re-evaluation is a new scan and consumes a row of these append-only, hard-capped tables when it ends
    in a no-entry. Scheduling re-evaluations stops while any is at/over the threshold so fresh candidates keep
    their room; the caller reports the pressure as a health warning. Nothing is deleted or reset.
    """
    from . import history_preparation_rejection as preparation, paper_cycle_no_entry as cycle_no_entry
    caps = ((preparation.TABLE, preparation.MAX_REJECTIONS), (cycle_no_entry.TABLE, cycle_no_entry.MAX_ROWS))
    pressure = []
    path = Path(evidence_db)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as c:
        for table, cap in caps:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                continue
            rows = c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            if rows >= CAPACITY_PAUSE_FRACTION * cap:
                pressure.append({'table': table, 'rows': rows, 'cap': cap})
    return pressure


def reason_codes(result, mint=None, scan_id=None):
    """Rejection codes carried by a dispatch result; ([], accepted) when nothing is rejected.

    Understands the typed results the dispatcher already publishes: engine rejects and
    blockers of a completed cycle, history-preparation no-entry, token-policy rejection
    and migration dispositions. Unknown shapes yield no codes, which classifies PERMANENT.

    With `mint`/`scan_id` (the dispatcher always passes both) only evidence about THIS candidate counts: an
    outcome carrying a different mint (a held position's reject) and a diagnostic of another scan are ignored.
    An outcome or diagnostic that names no mint/scan stays in (its code is then unclassified => PERMANENT).
    """
    codes, accepted = [], False
    if type(result) is not dict:
        return codes, accepted
    kind = result.get('kind')
    if kind == 'history_preparation_no_entry_v1' and type(result.get('reason')) is str:
        codes.append(result['reason'])
    elif kind == 'dispatcher_token_rejection_v1':
        policy = result.get('token_policy')
        codes.extend(x for x in (policy or {}).get('reasons', []) if type(x) is str)
    elif kind == 'dispatcher_migration_no_entry_v1' and type(result.get('reason')) is str:
        codes.append(result['reason'])
    for outcome in result.get('outcomes', []) if type(result.get('outcomes')) is list else []:
        if type(outcome) is not dict:
            continue
        if mint is not None and 'mint' in outcome and outcome['mint'] != mint:
            continue
        if outcome.get('type') == 'fill' and outcome.get('side') == 'buy':
            accepted = True
        elif outcome.get('type') == 'reject':
            reasons = outcome.get('reasons')
            codes.extend(r for r in (reasons if type(reasons) is list else [outcome.get('reason')]) if type(r) is str)
    if type(result.get('blockers')) is list:
        codes.extend(b for b in result['blockers'] if type(b) is str)
    for diagnostic in result.get('diagnostics', []) if type(result.get('diagnostics')) is list else []:
        if scan_id is not None and type(diagnostic) is dict and 'scan_id' in diagnostic and diagnostic['scan_id'] != scan_id:
            continue
        if type(diagnostic) is dict and type(diagnostic.get('blockers')) is list:
            codes.extend(b for b in diagnostic['blockers'] if type(b) is str)
    return sorted(set(codes)), accepted


def main(argv=None):
    """`init PATH` creates a new store; `show PATH` prints the append-only events (read-only)."""
    import argparse
    p = argparse.ArgumentParser(prog='desk.watchlist')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('init').add_argument('path')
    sub.add_parser('show').add_argument('path')
    a = p.parse_args(argv)
    try:
        if a.command == 'init':
            initialize(a.path)
            print(canonical({'status': 'INITIALIZED', 'path': a.path}))
        else:
            with closing(Watchlist(a.path, read_only=True)) as wl:
                print(canonical({'status': 'OK', 'events': wl.events()}))
        return 0
    except (ValueError, OSError, sqlite3.Error):
        print(canonical({'status': 'BLOCKED'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
