"""Counterfactual candidate tracking: learn whether the filters pick winners.

Research only. Records the forward PumpSwap vault-reserve price path of every
migration candidate (admitted, rejected or never dispatched) in its OWN
append-only store. It never touches the entry/monitoring budgets, the ledger or
a fill; its requests are charged against its own recorded hourly allowance and
go through the shared provider pacing. Prices here are NOT fill evidence.

    python -m tools.research.counterfactual init --store S [--allowance-per-hour 300]
    python -m tools.research.counterfactual ingest --store S --discovery-db D [--journal J] [--ledger L] [--decisions-db X]
    python -m tools.research.counterfactual sample --store S [--systemd-credentials]
    python -m tools.research.counterfactual report --store S
"""
import argparse
import base64
import json
import math
import os
import sqlite3
import statistics
import sys
import time
from contextlib import closing
from decimal import Decimal
from pathlib import Path

HORIZONS = (300, 900, 1800, 3600, 7200, 21600)  # +5m +15m +30m +1h +2h +6h
LABEL = 'RESEARCH_ONLY_NOT_FILL_EVIDENCE'
DEFAULT_ALLOWANCE = 300
DEFAULT_GRACE = 120
BATCH_TASKS = 50  # two vault accounts per task -> 100 accounts, the RPC maximum
BASELINE_HORIZON = HORIZONS[0]  # ONE baseline for every candidate: the +5m sample
WINDOW_MAX_SECONDS = 7200       # the dispatcher selects candidates 300..7200 s after the migration
NULL_POOL_BACKOFF = (60, 900)   # retry a not-yet-visible pool account after 60 s, doubling, capped at 15 min
NULL_POOL_IN_WINDOW = 30        # ...but every 30 s while a sample window is open, and never past its start
MAX_RESULT_ROWS = 100000
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SCHEMA = '''
CREATE TABLE candidates(mint TEXT PRIMARY KEY,pool TEXT NOT NULL,signature TEXT NOT NULL,slot INTEGER NOT NULL,
  migrated_at REAL NOT NULL,discovery_seq INTEGER NOT NULL,added_at REAL NOT NULL);
CREATE TABLE outcomes(id INTEGER PRIMARY KEY AUTOINCREMENT,mint TEXT NOT NULL,at REAL NOT NULL,payload TEXT NOT NULL);
CREATE TABLE vaults(mint TEXT PRIMARY KEY,base_vault TEXT,quote_vault TEXT,status TEXT NOT NULL,code TEXT,at REAL NOT NULL);
CREATE TABLE requests(id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,kind TEXT NOT NULL,accounts INTEGER NOT NULL);
CREATE TABLE request_results(request_id INTEGER PRIMARY KEY,status TEXT NOT NULL,code TEXT,slot INTEGER);
CREATE TABLE samples(mint TEXT NOT NULL,horizon INTEGER NOT NULL,due_at REAL NOT NULL,sampled_at REAL,slot INTEGER,
  base_raw TEXT,quote_raw TEXT,price TEXT,status TEXT NOT NULL,code TEXT,request_id INTEGER,PRIMARY KEY(mint,horizon));
CREATE TABLE policy(id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,allowance_per_hour INTEGER NOT NULL,grace_seconds INTEGER NOT NULL);
CREATE TABLE vault_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT,mint TEXT NOT NULL,at REAL NOT NULL,code TEXT NOT NULL);
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
'''
APPEND_ONLY = ('candidates', 'outcomes', 'vaults', 'requests', 'request_results', 'samples', 'policy', 'vault_attempts')


class CounterfactualError(ValueError):
    pass


class TransportError(CounterfactualError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _is_wal_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        head = os.read(fd, 100)
    finally:
        os.close(fd)
    return len(head) >= 20 and head[:16] == b'SQLite format 3\x00' and head[18] == 2


def open_mode(path):
    """T02F rule: ``immutable`` ONLY for a quiet WAL file (header byte 18 == 2, no ``-wal``); else ``ro``.

    A plain read-only open of a WAL database with no sidecars makes SQLite create ``-wal``/``-shm``
    (unwritable under ReadOnlyPaths, or owned by the wrong user). A rollback-journal store (for example
    the discovery or pacing database) can change under the read and is never opened immutable.
    """
    p = Path(path)
    return 'immutable' if _is_wal_file(p) and not Path(str(p) + '-wal').exists() else 'ro'


def _connect(path, *, readonly=False):
    path = Path(path)
    if readonly:
        if os.path.islink(path):
            raise CounterfactualError('Symlinked store refused')
        uri = path.resolve().as_uri() + ('?immutable=1' if open_mode(path) == 'immutable' else '?mode=ro')
        c = sqlite3.connect(uri, uri=True, timeout=2)
        c.execute('PRAGMA query_only=1')
        return c
    return sqlite3.connect(path, isolation_level=None, timeout=5)


def init(store, *, allowance_per_hour=DEFAULT_ALLOWANCE, grace_seconds=DEFAULT_GRACE, now=None):
    if type(allowance_per_hour) is not int or not 1 <= allowance_per_hour <= 3600 or type(grace_seconds) is not int or not 0 <= grace_seconds <= 3600:
        raise CounterfactualError('Allowance/grace out of range')
    path = Path(store)
    if path.exists():
        raise CounterfactualError('Store already exists; the counterfactual store is append-only')
    now = time.time() if now is None else now
    with closing(_connect(path)) as c:
        c.execute('BEGIN IMMEDIATE')
        for statement in SCHEMA.strip().split(';'):
            if statement.strip():
                c.execute(statement)
        for table in APPEND_ONLY:
            for action in ('UPDATE', 'DELETE'):
                c.execute(f"CREATE TRIGGER {table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'Counterfactual record is immutable'); END")
        c.execute('INSERT INTO policy(at,allowance_per_hour,grace_seconds) VALUES(?,?,?)', (now, allowance_per_hour, grace_seconds))
        c.execute('INSERT INTO meta VALUES(?,?)', ('label', LABEL))
        c.execute('INSERT INTO meta VALUES(?,?)', ('last_discovery_seq', '0'))
        c.commit()
    path.chmod(0o600)


def set_allowance(store, allowance_per_hour, *, grace_seconds=None, now=None):
    """Append a new recorded policy row; earlier rows stay as history."""
    if type(allowance_per_hour) is not int or not 1 <= allowance_per_hour <= 3600:
        raise CounterfactualError('Allowance out of range (1..3600)')
    if grace_seconds is not None and (type(grace_seconds) is not int or not 0 <= grace_seconds <= 3600):
        raise CounterfactualError('Grace out of range (0..3600)')
    with closing(_connect(store)) as c:
        grace = c.execute('SELECT grace_seconds FROM policy ORDER BY id DESC LIMIT 1').fetchone()[0] if grace_seconds is None else grace_seconds
        c.execute('INSERT INTO policy(at,allowance_per_hour,grace_seconds) VALUES(?,?,?)',
                  (time.time() if now is None else now, allowance_per_hour, grace))


def _policy(c):
    return c.execute('SELECT allowance_per_hour,grace_seconds FROM policy ORDER BY id DESC LIMIT 1').fetchone()


# ---------------------------------------------------------------- ingestion
def add_candidate(store, *, mint, pool, signature, slot, migrated_at, seq, now=None):
    with closing(_connect(store)) as c:
        c.execute('INSERT OR IGNORE INTO candidates VALUES(?,?,?,?,?,?,?)',
                  (mint, pool, signature, slot, float(migrated_at), seq, time.time() if now is None else now))
        return c.total_changes > 0


def discovery_cutoff(now, grace):
    """Oldest migration worth tracking: after this a candidate has no future horizon left to sample."""
    return now - (HORIZONS[-1] + grace)


def first_start_cursor(discovery_db, cutoff):
    """Cursor for a brand-new store: just before the first frame young enough to matter (never backfill)."""
    with closing(_connect(discovery_db, readonly=True)) as d:
        d.execute('BEGIN')
        first = d.execute('SELECT MIN(seq) FROM raw_events WHERE received_at>=?', (cutoff,)).fetchone()[0]
        if first is not None:
            return first - 1
        return d.execute('SELECT COALESCE(MAX(seq),0) FROM raw_events').fetchone()[0]


def discovery_hints(discovery_db, *, after_seq=0, limit=500, min_received=None):
    """Migration hints from continuous discovery (read-only), same decode as the dispatcher.

    Rows whose stored hash does not match their payload are skipped and counted:
    a research tool must never learn from altered originals. Frames received before
    ``min_received`` are skipped WITHOUT decoding (too old to have any horizon left).
    """
    from desk.decode import decode
    from desk.model import digest
    from tools import paper_entry_dispatcher as dispatcher
    hints, skipped = [], 0
    with closing(_connect(discovery_db, readonly=True)) as d:
        d.execute('BEGIN')
        rows = d.execute('SELECT seq,source_id,received_at,slot,payload_hash FROM raw_events WHERE seq>? ORDER BY seq LIMIT ?', (after_seq, limit)).fetchall()
        for seq, source, received, slot, stored_hash in rows:
            if min_received is not None and type(received) in (int, float) and received < min_received:
                continue
            try:
                payload = d.execute('SELECT payload FROM raw_events WHERE seq=?', (seq,)).fetchone()[0]
                raw = json.loads(payload, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite')))
                if digest(raw) != stored_hash or type(received) not in (int, float) or not math.isfinite(received):
                    raise ValueError('Original discovery record altered')
                decoded = decode(raw)
                if decoded['status'] != 'OBSERVED':
                    continue
                found = [o for o in decoded['program_observations']
                         if o.get('name') in ('migrate', 'migrate_v2') and o.get('status') == 'IDENTIFIED'
                         and dispatcher._migration_event_hint(raw, decoded, o)]
            except (ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError):
                skipped += 1
                continue
            if len(found) == 1:  # ambiguous notifications are never candidates
                hints.append({'seq': seq, 'mint': found[0]['mint'], 'pool': found[0]['pool'],
                              'signature': decoded['signature'], 'slot': slot, 'migrated_at': received})
    return hints, skipped, (rows[-1][0] if rows else after_seq)


# ------------------------------------------------------- outcome classification
# Same stage/code vocabulary as the funnel report: TOKEN, MIGRATION, HISTORY, OBSERVATIONS, ENGINE, DECISION.
def _code(value, default):
    if type(value) is str and value:
        return value[:96]
    if isinstance(value, dict):
        for key in ('code', 'reason', 'status'):
            if type(value.get(key)) is str and value[key]:
                return value[key][:96]
    return default


def classify_result(body, *, scan_id=None, no_entry_scans=frozenset()):
    """One stored dispatcher result -> {'class','stage','codes','detail'}. Every known shape is parsed.

    BOUGHT: the cycle result holds a BUY fill. REJECTED:<stage>:<code>: a typed normal rejection.
    UNRESOLVED:<code>: a BLOCKED cycle whose blocker is a latch (not in T01's ``NORMAL`` set, no
    ``paper_cycle_no_entry`` row for the scan, and no terminal receipt): an integrity-relevant stop, never a
    filter rejection. A result carrying ``terminal_receipt_hash`` was certified terminal by the cycle itself
    (known-hazard rejection such as MAYHEM_POOL) and stays a rejection.
    UNKNOWN: anything unresolved, corrupt, recovery-required or of an unrecognised kind (never guessed).
    """
    if not isinstance(body, dict):
        return {'class': 'UNKNOWN', 'stage': None, 'codes': [], 'detail': 'RESULT_NOT_AN_OBJECT'}
    kind = body.get('kind')
    if kind == 'dispatcher_token_rejection_v1':
        policy = body.get('token_policy') if isinstance(body.get('token_policy'), dict) else {}
        codes = [r[:96] for r in policy.get('reasons', []) if type(r) is str and r] or ['TOKEN_POLICY_SKIP']
        return {'class': 'REJECTED', 'stage': 'TOKEN', 'codes': codes, 'detail': None}
    if kind == 'dispatcher_migration_no_entry_v1':
        return {'class': 'REJECTED', 'stage': 'MIGRATION', 'codes': [_code(body.get('reason'), 'NO_ENTRY')], 'detail': None}
    if kind == 'history_preparation_no_entry_v1':
        return {'class': 'REJECTED', 'stage': 'HISTORY', 'codes': [_code(body.get('reason'), 'NO_ENTRY')], 'detail': None}
    if kind == 'paper_cycle_v1':
        outcomes = [o for o in body.get('outcomes', []) if isinstance(o, dict)]
        if any(o.get('type') == 'fill' and o.get('side') == 'buy' for o in outcomes):
            return {'class': 'BOUGHT', 'stage': None, 'codes': [], 'detail': None}
        status = body.get('status')
        if status == 'COMPLETE':
            codes = []
            for o in outcomes:
                if o.get('type') != 'reject':
                    continue
                for c in [_code(o.get('reason'), 'REJECTED')] + [r[:96] for r in o.get('reasons', []) if type(r) is str and r]:
                    if c not in codes:
                        codes.append(c)
            if codes:
                return {'class': 'REJECTED', 'stage': 'ENGINE', 'codes': codes, 'detail': None}
            return {'class': 'UNKNOWN', 'stage': None, 'codes': [], 'detail': 'COMPLETE_WITHOUT_FILL_OR_REJECTION'}
        if status == 'RECOVERY_REQUIRED':
            return {'class': 'UNKNOWN', 'stage': None, 'codes': [], 'detail': 'RECOVERY_REQUIRED'}
        blockers = [b[:96] for b in body.get('blockers', []) if type(b) is str and b]
        if blockers:
            from desk.paper_cycle_no_entry import NORMAL   # imported, never copied: one source of truth
            certified = type(body.get('terminal_receipt_hash')) is str and len(body['terminal_receipt_hash']) == 64 \
                and all(ch in '0123456789abcdef' for ch in body['terminal_receipt_hash'])
            # T01 only retires single-blocker results, so a multi-blocker result stays latched in the desk.
            if scan_id in no_entry_scans or (len(blockers) == 1 and blockers[0] in NORMAL):
                return {'class': 'REJECTED', 'stage': 'OBSERVATIONS', 'codes': blockers, 'detail': None}
            if certified:   # receipt not re-verified here; kept separable from verified rejections
                return {'class': 'REJECTED', 'stage': 'OBSERVATIONS', 'codes': blockers,
                        'detail': 'TERMINAL_RECEIPT_UNVERIFIED'}
            return {'class': 'UNRESOLVED', 'stage': 'OBSERVATIONS', 'codes': blockers, 'detail': 'LATCH_NOT_A_NORMAL_REJECTION'}
        return {'class': 'UNKNOWN', 'stage': None, 'codes': [], 'detail': 'BLOCKED_WITHOUT_CODE'}
    return {'class': 'UNKNOWN', 'stage': None, 'codes': [], 'detail': 'UNKNOWN_RESULT_KIND'}


class OutcomeReader:
    """Read-only view of the journal, the ledger and the decision store, opened ONCE and reused.

    Priority: ledger BUY fill > typed journal result > decision-store rejection > NOT_DISPATCHED.
    A missing store is UNKNOWN (never silently NOT_DISPATCHED), unreadable rows are UNKNOWN.
    """

    def __init__(self, journal_db=None, ledger_db=None, decisions_db=None, evidence_db=None):
        self.journal = self.buys = self.decisions = None
        self.no_entry = frozenset()
        self.errors = {}
        if evidence_db is not None:
            try:
                self.no_entry = self._load_no_entry(evidence_db)
            except (sqlite3.Error, OSError, ValueError, CounterfactualError):
                self.errors['evidence'] = 'EVIDENCE_UNREADABLE'   # a latch can then not be proven normal: stays UNRESOLVED
        if journal_db is not None:
            try:
                self.journal = _connect(journal_db, readonly=True)
                self.journal.execute('BEGIN')
            except (sqlite3.Error, OSError, CounterfactualError):
                self.journal, self.errors['journal'] = None, 'JOURNAL_UNREADABLE'
        if ledger_db is not None:
            try:
                self.buys = self._load_buys(ledger_db)
            except (sqlite3.Error, OSError, ValueError, CounterfactualError):
                self.errors['ledger'] = 'LEDGER_UNREADABLE'
        if decisions_db is not None:
            try:
                self.decisions = self._load_decisions(decisions_db)
            except (sqlite3.Error, OSError, ValueError, CounterfactualError):
                self.errors['decisions'] = 'DECISIONS_UNREADABLE'

    @staticmethod
    def _load_buys(ledger_db):
        with closing(_connect(ledger_db, readonly=True)) as c:
            c.execute('BEGIN')
            return {m for (m,) in c.execute("SELECT json_extract(payload,'$.mint') FROM outcomes WHERE json_extract(payload,'$.type')='fill' "
                                            "AND json_extract(payload,'$.side')='buy' LIMIT ?", (MAX_RESULT_ROWS,)) if m}

    @staticmethod
    def _load_no_entry(evidence_db):
        with closing(_connect(evidence_db, readonly=True)) as c:
            c.execute('BEGIN')
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_cycle_no_entry'").fetchone():
                return frozenset()
            return frozenset(s for (s,) in c.execute('SELECT scan_id FROM paper_cycle_no_entry LIMIT ?', (MAX_RESULT_ROWS,)))

    @staticmethod
    def _load_decisions(decisions_db):
        found = {}
        with closing(_connect(decisions_db, readonly=True)) as c:
            c.execute('BEGIN')
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='decisions'").fetchone():
                raise ValueError('decisions table missing')
            for (text,) in c.execute('SELECT decision FROM decisions LIMIT ?', (MAX_RESULT_ROWS,)):
                try:
                    value = json.loads(text)
                except ValueError:
                    continue
                if isinstance(value, dict) and type(value.get('mint')) is str and value.get('decision') == 'REJECT':
                    reasons = [r[:96] for r in value.get('reasons', []) if type(r) is str and r]
                    found[value['mint']] = reasons or ['REJECTED_WITHOUT_REASON']
        return found

    def close(self):
        if self.journal is not None:
            self.journal.close()
            self.journal = None

    def classify(self, mint):
        if self.buys is not None and mint in self.buys:
            return {'v': 2, 'class': 'BOUGHT', 'stage': None, 'codes': [], 'detail': None, 'source': 'LEDGER'}
        if self.journal is None and self.decisions is None:
            return {'v': 2, 'class': 'UNKNOWN', 'stage': None, 'codes': [], 'source': None,
                    'detail': self.errors.get('journal') or 'NO_OUTCOME_SOURCE'}
        if self.journal is not None:
            try:
                row = self.journal.execute('SELECT id FROM intents WHERE mint=?', (mint,)).fetchone()
                result = None if row is None else self.journal.execute('SELECT payload FROM results WHERE id=?', (row[0],)).fetchone()
                if row is not None:
                    if result is None:
                        return {'v': 2, 'class': 'UNKNOWN', 'stage': None, 'codes': [], 'source': 'JOURNAL', 'detail': 'UNRESOLVED_NO_RESULT'}
                    wrapper = json.loads(result[0])
                    body = wrapper.get('result')
                    got = classify_result(body, scan_id=wrapper.get('scan_id'), no_entry_scans=self.no_entry)
                    if got['class'] == 'UNRESOLVED' and self.errors.get('evidence'):
                        got['detail'] = 'LATCH_EVIDENCE_UNREADABLE'   # a no-entry row could not be looked up
                    return {'v': 2, **got, 'source': 'JOURNAL'}
            except (sqlite3.Error, ValueError, TypeError, AttributeError):
                return {'v': 2, 'class': 'UNKNOWN', 'stage': None, 'codes': [], 'source': 'JOURNAL', 'detail': 'OUTCOME_UNREADABLE'}
        if self.decisions is not None and mint in self.decisions:
            return {'v': 2, 'class': 'REJECTED', 'stage': 'DECISION', 'codes': self.decisions[mint], 'detail': None, 'source': 'DECISIONS'}
        if self.journal is not None:
            if self.errors.get('decisions'):   # a decision-store rejection cannot be ruled out: never claim NOT_DISPATCHED
                return {'v': 2, 'class': 'UNKNOWN', 'stage': None, 'codes': [], 'source': 'JOURNAL', 'detail': self.errors['decisions']}
            return {'v': 2, 'class': 'NOT_DISPATCHED', 'stage': None, 'codes': [], 'detail': None, 'source': 'JOURNAL'}
        return {'v': 2, 'class': 'UNKNOWN', 'stage': None, 'codes': [], 'source': None, 'detail': self.errors.get('journal') or 'JOURNAL_NOT_PROVIDED'}


def _outcome(journal_db, mint, *, ledger_db=None, decisions_db=None, evidence_db=None):
    """Single-candidate convenience wrapper (tests/tools); production paths reuse one OutcomeReader."""
    reader = OutcomeReader(journal_db, ledger_db, decisions_db, evidence_db)
    try:
        return reader.classify(mint)
    finally:
        reader.close()


def _is_final(payload, migrated_at, now, grace):
    """A typed result never changes. NOT_DISPATCHED and a decision-store rejection are provisional until the
    dispatch window has closed (a later journal result or ledger BUY outranks them); UNRESOLVED never is final."""
    if type(payload) is not dict or payload.get('v') != 2:
        return False
    window_closed = now > migrated_at + WINDOW_MAX_SECONDS + grace
    if payload['class'] == 'BOUGHT':
        return True
    if payload['class'] == 'REJECTED':
        return payload.get('source') != 'DECISIONS' or window_closed
    return payload['class'] == 'NOT_DISPATCHED' and window_closed


def ingest(store, discovery_db, *, journal_db=None, ledger_db=None, decisions_db=None, evidence_db=None, now=None, limit=500):
    now = time.time() if now is None else now
    with closing(_connect(store)) as c:
        after = int(c.execute("SELECT value FROM meta WHERE key='last_discovery_seq'").fetchone()[0])
        grace = _policy(c)[1]
    cutoff = discovery_cutoff(now, grace)
    if after == 0:
        # First start: never backfill the whole history, begin at the first frame that can still be sampled.
        after = first_start_cursor(discovery_db, cutoff)
        with closing(_connect(store)) as c:
            c.execute("INSERT OR REPLACE INTO meta VALUES('last_discovery_seq',?)", (str(after),))
    hints, skipped, last = discovery_hints(discovery_db, after_seq=after, limit=limit, min_received=cutoff)
    added = 0
    for h in hints:
        added += bool(add_candidate(store, mint=h['mint'], pool=h['pool'], signature=h['signature'], slot=h['slot'],
                                    migrated_at=h['migrated_at'], seq=h['seq'], now=now))
    with closing(_connect(store)) as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES('last_discovery_seq',?)", (str(last),))
    refreshed = refresh_outcomes(store, journal_db, ledger_db=ledger_db, decisions_db=decisions_db,
                                 evidence_db=evidence_db, now=now)
    return {'candidates_added': added, 'rows_skipped': skipped, 'last_seq': last, 'cursor_start': after,
            'outcomes_appended': refreshed['appended'], 'outcomes_final': refreshed['final']}


def refresh_outcomes(store, journal_db, *, ledger_db=None, decisions_db=None, evidence_db=None, now=None):
    """Append an outcome row only when the classification changed, and only for candidates whose
    latest outcome is not final. One journal/ledger/decision view is opened once for the whole pass."""
    now = time.time() if now is None else now
    appended = final = 0
    with closing(_connect(store)) as c:
        grace = _policy(c)[1]
        rows = c.execute('SELECT c.mint,c.migrated_at,(SELECT payload FROM outcomes o WHERE o.mint=c.mint ORDER BY o.id DESC LIMIT 1) '
                         'FROM candidates c').fetchall()
        reader = None
        try:
            for mint, migrated, last in rows:
                try:
                    previous = json.loads(last) if last is not None else None
                except ValueError:
                    previous = None
                if previous is not None and _is_final(previous, migrated, now, grace):
                    final += 1
                    continue
                if reader is None:
                    reader = OutcomeReader(journal_db, ledger_db, decisions_db, evidence_db)
                new = reader.classify(mint)
                if previous != new:
                    c.execute('INSERT INTO outcomes(mint,at,payload) VALUES(?,?,?)', (mint, now, json.dumps(new, sort_keys=True)))
                    appended += 1
        finally:
            if reader is not None:
                reader.close()
    return {'appended': appended, 'final': final}


# ----------------------------------------------------------------- price
def token_account_amount(account, *, mint, owner):
    """Raw amount of an SPL token account; any deviation fails closed."""
    from desk.security import TOKEN_PROGRAM, TOKEN_2022, base58
    if type(account) is not dict or account.get('owner') not in (TOKEN_PROGRAM, TOKEN_2022) or account.get('executable') is not False:
        raise CounterfactualError('VAULT_ACCOUNT_INVALID')
    data = account.get('data')
    if type(data) is not list or len(data) != 2 or data[1] != 'base64' or type(data[0]) is not str:
        raise CounterfactualError('VAULT_ACCOUNT_ENCODING_INVALID')
    raw = base64.b64decode(data[0], validate=True)
    if len(raw) < 165 or base58(raw[0:32]) != mint or base58(raw[32:64]) != owner:
        raise CounterfactualError('VAULT_ACCOUNT_IDENTITY_MISMATCH')
    return int.from_bytes(raw[64:72], 'little')


def price_from_reserves(base_raw, quote_raw):
    """PumpSwap constant product spot: quote lamports per raw base unit."""
    if type(base_raw) is not int or type(quote_raw) is not int or base_raw < 0 or quote_raw < 0:
        raise CounterfactualError('RESERVES_INVALID')
    return None if base_raw == 0 or quote_raw == 0 else Decimal(quote_raw) / Decimal(base_raw)


def metrics(samples):
    """Return per horizon, max gain/drawdown, liquidity and pool death.

    ONE baseline for every candidate: the +5m sample (``BASELINE_HORIZON``) price. If it is missing, failed
    or unpriced the candidate has NO returns (``baseline='MISSING'``) and is counted as such by the report;
    it is never re-baselined to a later horizon. A pool that is dead at +5m has ``baseline='DEAD'`` and
    counts as -100% at every horizon. A dead pool is -100% at its horizon and at every later horizon that
    has no priced sample of its own: survivorship is never hidden by dropping the dead.
    """
    by_horizon = {s['horizon']: s for s in samples}
    out = {'baseline_horizon': BASELINE_HORIZON, 'baseline': 'MISSING', 'returns': {}, 'liquidity_lamports': {},
           'max_gain': None, 'max_drawdown': None, 'died_at': None,
           'pool_died': any(s['status'] == 'POOL_DEAD' for s in samples)}
    for s in samples:
        if s['status'] in ('OK', 'POOL_DEAD') and s['quote_raw'] is not None:
            out['liquidity_lamports'][s['horizon']] = 2 * int(s['quote_raw'])
    base = by_horizon.get(BASELINE_HORIZON)
    if base is None:
        return out
    if base['status'] == 'POOL_DEAD':
        out.update(baseline='DEAD', died_at=BASELINE_HORIZON, max_gain=Decimal(0), max_drawdown=Decimal(-1))
        out['returns'] = {h: Decimal(-1) for h in HORIZONS}
        return out
    if base['status'] != 'OK' or base['price'] is None:
        return out
    p0 = Decimal(base['price'])
    out['baseline'] = 'OK'
    peak, worst, dead = p0, Decimal(0), False
    for horizon in HORIZONS:
        sample = by_horizon.get(horizon)
        if sample is not None and sample['status'] == 'OK' and sample['price'] is not None:
            price = Decimal(sample['price'])
            out['returns'][horizon] = price / p0 - 1
            dead = False
            peak = max(peak, price)
            worst = min(worst, price / peak - 1)
        elif (sample is not None and sample['status'] == 'POOL_DEAD') or dead:
            dead = True
            out['returns'][horizon] = Decimal(-1)
            out['died_at'] = out['died_at'] if out['died_at'] is not None else horizon
    out['max_gain'] = max(r for h, r in out['returns'].items() if r != -1 or h == BASELINE_HORIZON) if out['returns'] else None
    out['max_drawdown'] = Decimal(-1) if out['pool_died'] else worst
    return out


# --------------------------------------------------------------- sampling
class HeliusTransport:
    """Batched JSON-RPC through the shared Helius pacing; the key comes from the managed credential."""

    def __init__(self, pacer):
        from desk.coordinator_rpc import _ENDPOINT
        import os
        key = os.environ.get('HELIUS_API_KEY')
        if pacer is None:
            raise TransportError('PACING_NOT_CONFIGURED')
        if type(key) is not str or not 1 <= len(key) <= 512 or any(not 33 <= ord(ch) <= 126 for ch in key):
            raise TransportError('CREDENTIAL_UNAVAILABLE')
        self.pacer, self.key, self.endpoint = pacer, key, _ENDPOINT

    def __call__(self, method, params):
        from urllib.error import HTTPError, URLError
        from urllib.parse import urlencode
        from urllib.request import Request, build_opener, HTTPRedirectHandler
        from desk import provider_pacing
        try:
            ticket = self.pacer.acquire('helius', timeout_seconds=20)
        except provider_pacing.PacingError as error:
            raise TransportError(error.code) from None
        body = json.dumps({'jsonrpc': '2.0', 'id': 'counterfactual-v1', 'method': method, 'params': params}).encode()
        request = Request(self.endpoint + '?' + urlencode({'api-key': self.key}), data=body, method='POST',
                          headers={'Content-Type': 'application/json', 'Accept': 'application/json'})

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        released = False
        try:
            with build_opener(NoRedirect()).open(request, timeout=15) as response:
                if provider_pacing.should_throttle(response.status, response.headers):
                    self.pacer.throttle('helius', response.headers, ticket=ticket); released = True
                    raise TransportError('HTTP_THROTTLED')
                if response.status != 200:
                    raise TransportError('HTTP_REJECTED')
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            if provider_pacing.should_throttle(error.code, error.headers) and not released:
                self.pacer.throttle('helius', error.headers, ticket=ticket); released = True
            raise TransportError('HTTP_REJECTED') from None
        except (URLError, OSError, TimeoutError):
            raise TransportError('TRANSPORT_ERROR') from None
        finally:
            if not released:
                try:
                    self.pacer.finish('helius', ticket)
                except provider_pacing.PacingError:
                    pass
        if len(raw) > MAX_RESPONSE_BYTES:
            raise TransportError('RESPONSE_OVERSIZED')
        try:
            decoded = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite')))
        except ValueError:
            raise TransportError('RESPONSE_INVALID') from None
        if type(decoded) is not dict or set(decoded) != {'jsonrpc', 'id', 'result'} or decoded['id'] != 'counterfactual-v1':
            raise TransportError('RESPONSE_INVALID')
        return decoded['result']


def _charge(c, now, kind, accounts):
    """Atomically enforce the rolling-hour allowance and record the request as charged."""
    c.execute('BEGIN IMMEDIATE')
    allowance = _policy(c)[0]
    used = c.execute('SELECT COUNT(*) FROM requests WHERE at>?', (now - 3600,)).fetchone()[0]
    if used >= allowance:
        c.rollback()
        return None
    rid = c.execute('INSERT INTO requests(at,kind,accounts) VALUES(?,?,?)', (now, kind, accounts)).lastrowid
    c.commit()
    return rid


def _call(c, transport, now, kind, addresses):
    """One charged batched getMultipleAccounts; returns (request_id, slot, values) or (request_id, None, code)."""
    rid = _charge(c, now, kind, len(addresses))
    if rid is None:
        return None, None, 'ALLOWANCE_EXHAUSTED'
    try:
        result = transport('getMultipleAccounts', [addresses, {'encoding': 'base64', 'commitment': 'confirmed'}])
        slot = result['context']['slot']
        values = result['value']
        if type(slot) is not int or type(values) is not list or len(values) != len(addresses):
            raise CounterfactualError('RESPONSE_INVALID')
    except TransportError as error:
        c.execute('INSERT INTO request_results VALUES(?,?,?,NULL)', (rid, 'FAILED', error.code))
        return rid, None, error.code
    except (CounterfactualError, KeyError, TypeError):
        c.execute('INSERT INTO request_results VALUES(?,?,?,NULL)', (rid, 'FAILED', 'RESPONSE_INVALID'))
        return rid, None, 'RESPONSE_INVALID'
    c.execute('INSERT INTO request_results VALUES(?,?,NULL,?)', (rid, 'OK', slot))
    return rid, slot, values


def _ensure_attempts_table(c):
    """Stores created before ``vault_attempts`` existed gain it (append-only like every other table)."""
    c.execute('CREATE TABLE IF NOT EXISTS vault_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT,mint TEXT NOT NULL,at REAL NOT NULL,code TEXT NOT NULL)')
    for action in ('UPDATE', 'DELETE'):
        c.execute(f"CREATE TRIGGER IF NOT EXISTS vault_attempts_{action.lower()} BEFORE {action} ON vault_attempts "
                  "BEGIN SELECT RAISE(ABORT,'Counterfactual record is immutable'); END")


def null_pool_delay(attempts, *, last=None, migrated=None, grace=DEFAULT_GRACE):
    """Backoff before the next try of a pool account that was not visible yet.

    Base: 60 s, 120 s ... capped at 15 min. With ``last`` (time of the previous try) and ``migrated`` the
    delay is also capped so a sample window can never be overshot: the next try happens no later than the
    start of the next window that has not opened (migrated + horizon), and every ``NULL_POOL_IN_WINDOW``
    seconds while ``last`` lies inside a window [migrated + h, migrated + h + grace].
    """
    base = min(NULL_POOL_BACKOFF[0] * 2 ** max(0, attempts - 1), NULL_POOL_BACKOFF[1])
    if last is None or migrated is None:
        return base
    for h in HORIZONS:
        start, end = migrated + h, migrated + h + grace
        if last < start:
            return min(base, max(start - last, 1))
        if last <= end:
            return min(base, NULL_POOL_IN_WINDOW)
    return base


def _resolve_vaults(c, transport, now, summary):
    from desk.pools import parse_pool
    from desk.providers import SOL
    grace = _policy(c)[1]
    todo = []
    for mint, pool, migrated in c.execute('SELECT mint,pool,migrated_at FROM candidates WHERE mint NOT IN (SELECT mint FROM vaults) '
                                          'ORDER BY migrated_at').fetchall():
        attempts, last = c.execute('SELECT COUNT(*),COALESCE(MAX(at),0) FROM vault_attempts WHERE mint=?', (mint,)).fetchone()
        if now > migrated + HORIZONS[-1] + grace:
            # The sampling window has passed: stop retrying and record why, once.
            c.execute('INSERT INTO vaults VALUES(?,?,?,?,?,?)', (mint, None, None, 'UNRESOLVED',
                      'POOL_ACCOUNT_NULL_WINDOW_PASSED' if attempts else 'WINDOW_PASSED_BEFORE_RESOLUTION', now))
            summary['abandoned'] = summary.get('abandoned', 0) + 1
            continue
        if attempts and now < last + null_pool_delay(attempts, last=last, migrated=migrated, grace=grace):
            continue  # backing off: a null pool account usually means the pool is not visible yet
        todo.append((mint, pool))
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        rid, slot, values = _call(c, transport, now, 'POOL', [p for _, p in chunk])
        if slot is None:
            summary['failed_requests'] += rid is not None
            if values == 'ALLOWANCE_EXHAUSTED':
                summary['allowance_exhausted'] = True
                return
            continue  # transient batch failure: candidates stay unresolved and retry next run
        summary['requests'] += 1
        for (mint, pool), account in zip(chunk, values):
            if account is None:   # not (yet) visible: retry with backoff until the horizon window passes
                c.execute('INSERT INTO vault_attempts(mint,at,code) VALUES(?,?,?)', (mint, now, 'POOL_ACCOUNT_NULL'))
                summary['null_pool'] = summary.get('null_pool', 0) + 1
                continue
            try:
                fields = parse_pool(account)
                if fields['base_mint'] != mint or fields['quote_mint'] != SOL:
                    raise CounterfactualError('POOL_IDENTITY_MISMATCH')
                row = (mint, fields['pool_base_token_account'], fields['pool_quote_token_account'], 'OK', None, now)
            except (ValueError, KeyError, TypeError, CounterfactualError, StopIteration):
                row = (mint, None, None, 'UNRESOLVED', 'POOL_ACCOUNT_INVALID', now)
            c.execute('INSERT INTO vaults VALUES(?,?,?,?,?,?)', row)


def due_tasks(c, now):
    grace = _policy(c)[1]
    rows = c.execute('''SELECT c.mint,c.pool,c.migrated_at,v.base_vault,v.quote_vault FROM candidates c
                        JOIN vaults v ON v.mint=c.mint WHERE v.status='OK' ORDER BY c.migrated_at''').fetchall()
    due, missed = [], []
    for mint, pool, migrated, base_vault, quote_vault in rows:
        done = {h for (h,) in c.execute('SELECT horizon FROM samples WHERE mint=?', (mint,))}
        for h in HORIZONS:
            if h in done or now < migrated + h:
                continue
            (missed if now > migrated + h + grace else due).append((mint, pool, h, migrated + h, base_vault, quote_vault))
    return due, missed


def sample(store, transport, *, now=None):
    now = time.time() if now is None else now
    summary = {'requests': 0, 'failed_requests': 0, 'samples': 0, 'missed': 0, 'allowance_exhausted': False}
    with closing(_connect(store)) as c:
        _ensure_attempts_table(c)
        _resolve_vaults(c, transport, now, summary)
        due, missed = due_tasks(c, now)
        for mint, _, h, due_at, *_ in missed:  # late samples would be mislabeled; record the gap instead
            c.execute("INSERT OR IGNORE INTO samples(mint,horizon,due_at,status,code) VALUES(?,?,?,'MISSED','SCHEDULE_MISSED')", (mint, h, due_at))
            summary['missed'] += 1
        for i in range(0, len(due), BATCH_TASKS):
            if summary['allowance_exhausted']:
                break
            chunk = due[i:i + BATCH_TASKS]
            addresses = [a for t in chunk for a in (t[4], t[5])]
            rid, slot, values = _call(c, transport, now, 'SAMPLE', addresses)
            if slot is None:
                summary['failed_requests'] += rid is not None
                summary['allowance_exhausted'] |= values == 'ALLOWANCE_EXHAUSTED'
                continue
            summary['requests'] += 1
            c.execute('BEGIN IMMEDIATE')
            for n, (mint, pool, h, due_at, *_) in enumerate(chunk):
                base_account, quote_account = values[2 * n], values[2 * n + 1]
                try:
                    if base_account is None or quote_account is None:  # closed vault = dead pool, never a missing sample
                        row = ('POOL_DEAD', 'VAULT_CLOSED', None, None, None)
                    else:
                        from desk.providers import SOL
                        base_raw = token_account_amount(base_account, mint=mint, owner=pool)
                        quote_raw = token_account_amount(quote_account, mint=SOL, owner=pool)
                        price = price_from_reserves(base_raw, quote_raw)
                        row = ('OK', None, str(base_raw), str(quote_raw), None if price is None else format(price, 'f'))
                        if price is None:
                            row = ('POOL_DEAD', 'ZERO_RESERVE', str(base_raw), str(quote_raw), None)
                except (CounterfactualError, ValueError, TypeError):
                    row = ('FAILED', 'VAULT_ACCOUNT_INVALID', None, None, None)  # malformed provider data fails closed
                c.execute('INSERT OR IGNORE INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                          (mint, h, due_at, now, slot, row[2], row[3], row[4], row[0], row[1], rid))
                summary['samples'] += 1
            c.commit()
    return summary


# ----------------------------------------------------------------- report
def groups_of(payload):
    """Report groups of one candidate: BOUGHT | REJECTED:<stage>:<code> (one per code) | UNRESOLVED:<code> | NOT_DISPATCHED | UNKNOWN.

    A candidate rejected for several reasons appears once per reason (each filter's own view); the
    report also states the number of distinct candidates so groups are never read as a partition.
    """
    if type(payload) is not dict or payload.get('v') != 2:
        return ['UNKNOWN'], ('LEGACY_OUTCOME_FORMAT' if type(payload) is dict else 'NO_OUTCOME_RECORDED')
    kind = payload.get('class')
    if kind == 'BOUGHT':
        return ['BOUGHT'], None
    if kind == 'REJECTED':
        codes = payload.get('codes') or ['UNSPECIFIED']
        return [f"REJECTED:{payload.get('stage') or 'UNKNOWN_STAGE'}:{code}" for code in codes], None
    if kind == 'UNRESOLVED':   # latches are shown on their own and never counted as a filter's rejection
        return [f"UNRESOLVED:{code}" for code in (payload.get('codes') or ['UNSPECIFIED'])], None
    if kind == 'NOT_DISPATCHED':
        return ['NOT_DISPATCHED'], None
    return ['UNKNOWN'], payload.get('detail') or 'UNSPECIFIED'


def _rank(values, q):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def report(store, *, horizon=3600):
    """Forward-return distribution per outcome group at one horizon (default +1h).

    Fixed +5m baseline; pools that died count as -100% (never dropped); candidates with no usable baseline
    sample are excluded from return statistics and counted (``baseline_missing``) per group.
    """
    groups, unknown, distinct = {}, {}, 0
    with closing(_connect(store, readonly=True)) as c:
        c.execute('BEGIN')
        for mint, in c.execute('SELECT mint FROM candidates').fetchall():
            distinct += 1
            samples = [dict(zip(('horizon', 'status', 'price', 'quote_raw'), r)) for r in c.execute(
                'SELECT horizon,status,price,quote_raw FROM samples WHERE mint=? ORDER BY horizon', (mint,))]
            last = c.execute('SELECT payload FROM outcomes WHERE mint=? ORDER BY id DESC LIMIT 1', (mint,)).fetchone()
            m = metrics(samples)
            try:
                payload = json.loads(last[0]) if last else None
            except ValueError:
                payload = {}
            names, detail = groups_of(payload)
            if detail:
                unknown[detail] = unknown.get(detail, 0) + 1
            for name in names:
                g = groups.setdefault(name, {'candidates': 0, 'with_return': 0, 'returns': [], 'died': 0, 'max_gains': [],
                                             'baseline_missing': 0, 'dead_at_baseline': 0, 'dead_at_horizon': 0})
                g['candidates'] += 1
                g['died'] += m['pool_died']
                if m['baseline'] == 'MISSING':
                    g['baseline_missing'] += 1
                    continue
                g['dead_at_baseline'] += m['baseline'] == 'DEAD'
                if horizon in m['returns']:
                    g['with_return'] += 1
                    g['returns'].append(m['returns'][horizon])
                    g['dead_at_horizon'] += m['returns'][horizon] == -1
                if m['max_gain'] is not None:
                    g['max_gains'].append(m['max_gain'])
    result = {'label': LABEL, 'horizon_seconds': horizon, 'distinct_candidates': distinct, 'unknown_breakdown': dict(sorted(unknown.items())),
              'baseline': f'the +{BASELINE_HORIZON // 60}m sample, fixed for every candidate; a missing baseline excludes the candidate '
                          '(counted as baseline_missing) and never re-baselines; the migration-time price is not observed',
              'survivorship': 'pools that died are -100% (dead_at_horizon / dead_at_baseline) and included in the return statistics',
              'groups': {}}
    for name, g in sorted(groups.items()):
        r = g['returns']
        result['groups'][name] = {
            'candidates': g['candidates'], 'with_return': g['with_return'], 'baseline_missing': g['baseline_missing'],
            'pool_died': g['died'], 'dead_at_baseline': g['dead_at_baseline'], 'dead_at_horizon': g['dead_at_horizon'],
            'mean_return': str(sum(r) / len(r)) if r else None, 'median_return': str(statistics.median(r)) if r else None,
            'p10_return': str(_rank(r, .1)) if r else None, 'p90_return': str(_rank(r, .9)) if r else None,
            'share_positive': str(Decimal(sum(1 for x in r if x > 0)) / len(r)) if r else None,
            'median_max_gain': str(statistics.median(g['max_gains'])) if g['max_gains'] else None}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'ingest', 'sample', 'report', 'set-allowance'):
        p = sub.add_parser(name)
        p.add_argument('--store', required=True)
        if name == 'init':
            p.add_argument('--allowance-per-hour', type=int, default=DEFAULT_ALLOWANCE)
            p.add_argument('--grace-seconds', type=int, default=DEFAULT_GRACE)
        if name == 'set-allowance':
            p.add_argument('--allowance-per-hour', type=int, required=True)
        if name == 'ingest':
            p.add_argument('--discovery-db', required=True)
            p.add_argument('--journal')
            p.add_argument('--ledger', help='read-only ledger: BUY fills make a candidate BOUGHT')
            p.add_argument('--decisions-db', help='read-only decision store (paper-decisions.sqlite)')
            p.add_argument('--evidence-db', help='read-only evidence store: paper_cycle_no_entry rows prove a latch was a normal rejection')
        if name == 'sample':
            p.add_argument('--systemd-credentials', action='store_true')
        if name == 'report':
            p.add_argument('--horizon', type=int, default=3600, choices=HORIZONS)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            init(args.store, allowance_per_hour=args.allowance_per_hour, grace_seconds=args.grace_seconds); out = {'status': 'INITIALIZED'}
        elif args.command == 'set-allowance':
            set_allowance(args.store, args.allowance_per_hour); out = {'status': 'RECORDED'}
        elif args.command == 'ingest':
            out = ingest(args.store, args.discovery_db, journal_db=args.journal, ledger_db=args.ledger,
                         decisions_db=args.decisions_db, evidence_db=args.evidence_db)
        elif args.command == 'sample':
            from desk import provider_pacing
            if args.systemd_credentials:
                from desk.paper_cycle_cli import _credentials
                _credentials()
            out = sample(args.store, HeliusTransport(provider_pacing.configured(priority='investigation')))
        else:
            out = report(args.store, horizon=args.horizon)
    except (CounterfactualError, sqlite3.Error, OSError, ValueError, KeyError) as error:
        print(json.dumps({'status': 'BLOCKED', 'error': type(error).__name__, 'label': LABEL}))
        return 2
    print(json.dumps(out, sort_keys=True, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
