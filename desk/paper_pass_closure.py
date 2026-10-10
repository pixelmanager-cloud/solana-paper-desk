"""Generic terminal closure of paper passes that end without a verified result.

A pass that charged requests and then failed (a transient read, a stale or worthless quote, a deadline, an
exception) or whose process died leaves `paper_observation_passes.outcome_hash` NULL, which fails the global
gate closed. This module retires such a pass with an immutable, replay-verifiable record instead of a
per-incident receipt:

* FAILED_CHARGED   - written by the owning process as it fails.
* INTEGRITY_HOLD   - written by the owning process when it ends with a category (b) cause. It deliberately leaves
  the pass NULL (the global latch holds) but records that the process *finished*, so the recovery step below can
  never mistake a deliberate latch for an abandoned pass.
* ABANDONED_CHARGED - written by a deterministic, bounded recovery step run under the research -> evidence ->
  ledger locks. A held flock cannot outlive its process, so a NULL pass seen while the recovery step owns all
  three locks belongs to a process that is gone; no wall-clock age is trusted.

Charges stay charged (admission counters and monitoring reservations are never reset or refunded). The record
binds the original intent, the per-scan charge window (before/after), the retained attempt originals that can be
found, the monitoring reservation range (every reservation must carry an outcome) and an immutable ledger prefix
anchor. Corrupt, contradictory or unrecoverable states are refused here and keep the latch (category b). No
request, retry, signing or entry is ever created.
"""
from contextlib import closing, ExitStack
from pathlib import Path
import sqlite3

from . import paper_terminal_reconciliation as terminal, runtime_compatibility as runtime
from .history_progress import HistoryProgress
from .model import canonical, digest
from .paper_checkpoint import read_checkpoint

TABLE = 'paper_pass_closures'
KIND = 'paper_pass_closure_v1'
STATUSES = ('FAILED_CHARGED', 'ABANDONED_CHARGED')
HOLD = 'INTEGRITY_HOLD'
ABANDONED_CAUSE = 'LEASE_GONE'
MAX_ROWS = 8192            # below the 10000 original-pass ceiling
MAX_REFS = 64
MAX_RECOVER = 16
INTENT_KINDS = ('paper_cycle_intent_v1', 'history_first_paper_preparation_v4')
FIELDS = {'kind', 'version', 'status', 'cause', 'role', 'pass_id', 'intent_hash', 'result_hash', 'scans',
          'attempt_refs', 'monitoring', 'ledger', 'retired_scan', 'execution_status', 'live_readiness',
          'entry_authorized'}
# Causes that mean the retained evidence is corrupt, contradictory or unidentifiable: never closed here.
INTEGRITY_MARKERS = ('BINDING_INVALID', 'MISMATCH', 'PERSISTENCE_FAILED', 'RECOVERYREQUIRED', 'LEDGER_INTEGRITY',
                     'OBSERVATION_RECOVERY_REQUIRED', 'MONITORING_RECOVERY_REQUIRED', 'ACCOUNTING_INVALID',
                     'EVIDENCE_INVALID', 'MUTATED', 'IDENTITY', 'INTEGRITY', 'CONTEXT_BINDING', 'CHECKPOINT',
                     'OUTCOME_PENDING', 'CLOCK_ROLLBACK', 'IMPLEMENTATION', 'CONFIG_MISMATCH',
                     # corrupt or unreadable retained evidence (message text of EvidenceStore/terminal._load errors)
                     'CHECKSUM', 'CORRUPT', 'EVIDENCE MISSING', 'EVIDENCE ENCODING', 'EVIDENCE_MISSING', 'ENCODING MISMATCH',
                     'ORIGINAL EVIDENCE')
# Exception types that are never an ordinary provider/transport failure: stored data is unreadable or inconsistent.
INTEGRITY_TYPES = ('DatabaseError', 'IntegrityError', 'DataError', 'error', 'JSONDecodeError', 'UnicodeDecodeError')
HOLD_FIELDS = {'kind', 'version', 'status', 'cause', 'pass_id', 'intent_hash', 'result_hash', 'execution_status',
               'live_readiness', 'entry_authorized'}
SQL = (f'CREATE TABLE {TABLE}(pass_id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,closure_hash TEXT NOT NULL,'
       'status TEXT NOT NULL,retired_scan TEXT UNIQUE)')
GUARDS = {TABLE+'_insert': f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE pass_id=NEW.pass_id OR rowid=NEW.rowid) OR (SELECT count(*) FROM {TABLE})>={MAX_ROWS} BEGIN SELECT RAISE(ABORT,'Pass closure immutable'); END"}
GUARDS |= {TABLE+'_'+a.lower(): f"CREATE TRIGGER {TABLE}_{a.lower()} BEFORE {a} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Pass closure immutable'); END" for a in ('UPDATE', 'DELETE')}


class ClosureRefused(ValueError):
    """The pass cannot be proved safe to close; the NULL latch stays (fail closed)."""


def is_integrity(cause):
    text = str(cause).upper()
    return any(marker in text for marker in INTEGRITY_MARKERS)


def cause_of(error):
    code = getattr(error, 'code', None)
    if type(code) is str and 1 <= len(code) <= 128:
        return code
    name = type(error).__name__
    if name in INTEGRITY_TYPES or isinstance(error, sqlite3.DatabaseError):
        return 'EXCEPTION_INTEGRITY_' + name.upper()
    if not isinstance(error, (OSError, ValueError, KeyboardInterrupt, SystemExit)):
        # an unclassified defect (RuntimeError, TypeError, ...): stays visible and latched until reviewed
        return 'EXCEPTION_INTEGRITY_UNCLASSIFIED_' + name.upper()
    return ('EXCEPTION_' + name.upper() + ':' + ' '.join(str(error).split())[:80])[:128]


def certified_ids(c):
    """Pass ids already certified by any reviewed per-incident receipt; their NULL outcome is load-bearing."""
    from .paper_http403_retirement import rows as http_rows
    from .paper_preparation_retirement import rows as preparation_rows
    from .paper_dispatch_preparation_retirement import rows as dispatch_rows
    ids = {v['pass_id'] for v in terminal._rows(c)}
    ids |= {v['pass_id'] for v in http_rows(c)} | {v['pass_id'] for v in preparation_rows(c)} | {v['pass_id'] for v in dispatch_rows(c)}
    return ids


def _baselines(intent):
    """(scan -> requests_used before, scans that were open positions, ledger path)."""
    if type(intent) is not dict or intent.get('kind') not in INTENT_KINDS or intent.get('closure_v1') is not True:
        raise ClosureRefused('Unsupported or pre-closure pass intent')
    if intent['kind'] == 'paper_cycle_intent_v1':
        admissions = intent['admissions']
        if type(admissions) is not dict or not 1 <= len(admissions) <= 32:
            raise ClosureRefused('Cycle intent admissions malformed')
        before = {scan: a['requests_used'] for scan, a in admissions.items()}
        positions = intent.get('positions', [])
        if type(positions) is not list or not set(positions) <= set(before):
            raise ClosureRefused('Cycle intent positions malformed')
        ledger = intent['ledger']
    else:
        scan = intent['target']['target']['scan_id']
        before = {scan: intent['admission']['requests_used']}
        positions = []
        ledger = intent['context']['ledger_db']
    if any(type(k) is not str or not 1 <= len(k) <= 256 or type(v) is not int or v < 0 for k, v in before.items()):
        raise ClosureRefused('Intent charge baseline malformed')
    if type(ledger) is not str or not ledger.startswith('/'):
        raise ClosureRefused('Intent ledger path malformed')
    return before, set(positions), ledger


def _table_rows(c):
    """Verified schema/guards/scalars of the closure table; [] when it does not exist yet."""
    from . import paper_preparation_retirement as historical
    historical._schema_bounds(c)
    objects = c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name=? OR substr(name,1,?)=? OR tbl_name=?",
                        (TABLE, len(TABLE)+1, TABLE+'_', TABLE)).fetchall()
    if not objects:
        return []
    expected = {('table', TABLE, TABLE, SQL)} | {('trigger', n, TABLE, s) for n, s in GUARDS.items()}
    expected |= {('index', f'sqlite_autoindex_{TABLE}_{i}', TABLE, None) for i in (1, 2)}
    if set(objects) != expected or len(objects) != len(expected):
        raise ValueError('Pass closure schema malformed')
    count = c.execute(f'SELECT count(*) FROM {TABLE}').fetchone()[0]
    if not 0 <= count <= MAX_ROWS:
        raise ValueError('Pass closure count invalid')
    if c.execute(f"SELECT 1 FROM {TABLE} WHERE typeof(pass_id)!='text' OR length(CAST(pass_id AS BLOB))!=32 OR typeof(intent_hash)!='text' OR length(CAST(intent_hash AS BLOB))!=64 OR typeof(closure_hash)!='text' OR length(CAST(closure_hash AS BLOB))!=64 OR status NOT IN ('FAILED_CHARGED','ABANDONED_CHARGED','INTEGRITY_HOLD') OR (retired_scan IS NOT NULL AND (typeof(retired_scan)!='text' OR length(CAST(retired_scan AS BLOB)) NOT BETWEEN 1 AND 256)) LIMIT 1").fetchone():
        raise ValueError('Pass closure scalar malformed')
    result = c.execute(f'SELECT pass_id,intent_hash,closure_hash,status,retired_scan FROM {TABLE}').fetchall()
    if any(not terminal._id(p) or not runtime._hash(i) or not runtime._hash(k) or (st == HOLD and r is not None)
           for p, i, k, st, r in result):
        raise ValueError('Pass closure scalar malformed')
    return result


def _ledger_valid(path):
    with closing(sqlite3.connect(Path(path).as_uri()+'?mode=ro', uri=True)) as c:
        c.execute('BEGIN')
        if read_checkpoint(c) is None:        # raises RecoveryRequired on any integrity failure
            raise ClosureRefused('Ledger checkpoint missing')


def _monitoring_window(c, intent, *, require_outcomes=True):
    """(first, last) reservation ids charged since the pass began, or None. Every one must carry an outcome."""
    before = intent.get('monitoring_total')
    if before is None:
        return None
    if type(before) is not int or before < 0:
        raise ClosureRefused('Intent monitoring baseline malformed')
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_monitoring_reservations'").fetchone():
        return None
    total = c.execute('SELECT count(*),COALESCE(max(id),0) FROM paper_monitoring_reservations').fetchone()
    if total[0] != total[1] or total[0] < before:
        raise ClosureRefused('Monitoring reservations inconsistent with the pass baseline')
    if total[0] == before:
        return None
    first, last = before+1, total[0]
    done = c.execute('SELECT count(*) FROM paper_monitoring_reservations r JOIN paper_monitoring_outcomes o ON o.reservation_id=r.id '
                     'WHERE r.id BETWEEN ? AND ?', (first, last)).fetchone()[0]
    if require_outcomes and done != last-first+1:
        raise ClosureRefused('Monitoring reservation without an outcome')
    return {'first': first, 'last': last}


def _verify_hold(store, rec, *, row=None):
    if (type(rec) is not dict or set(rec) != HOLD_FIELDS or rec['kind'] != KIND or rec['version'] != 1
            or rec['status'] != HOLD or rec['execution_status'] != 'EXECUTION_UNVERIFIED'
            or rec['live_readiness'] is not False or rec['entry_authorized'] is not False
            or not terminal._id(rec['pass_id']) or not runtime._hash(rec['intent_hash'])
            or type(rec['cause']) is not str or not 1 <= len(rec['cause']) <= 128
            or (rec['result_hash'] is not None and not runtime._hash(rec['result_hash']))):
        raise ValueError('Pass integrity hold shape')
    if row is not None and (row[0], row[1], row[2], row[3]) != (rec['pass_id'], rec['intent_hash'], digest(rec), HOLD):
        raise ValueError('Pass integrity hold row binding')


def _verify(store, progress, rec, *, row=None, ledger=True):
    """Replay a closure record against its originals. Raises ValueError on any doubt."""
    if (type(rec) is not dict or set(rec) != FIELDS or rec['kind'] != KIND or type(rec['version']) is not int
            or rec['version'] != 1 or rec['status'] not in STATUSES or rec['execution_status'] != 'EXECUTION_UNVERIFIED'
            or rec['live_readiness'] is not False or rec['entry_authorized'] is not False
            or rec['role'] not in ('ENTRY', 'HELD') or not terminal._id(rec['pass_id'])
            or not runtime._hash(rec['intent_hash']) or type(rec['cause']) is not str or not 1 <= len(rec['cause']) <= 128
            or is_integrity(rec['cause']) or type(rec['attempt_refs']) is not list or len(rec['attempt_refs']) > MAX_REFS
            or not all(runtime._hash(k) for k in rec['attempt_refs'])):
        raise ValueError('Pass closure shape')
    if rec['status'] == 'ABANDONED_CHARGED' and (rec['cause'] != ABANDONED_CAUSE or rec['result_hash'] is not None):
        raise ValueError('Abandoned closure must name the lease and carry no result')
    if rec['result_hash'] is not None:
        result = terminal._load(store, rec['result_hash'])
        if (not runtime._hash(rec['result_hash']) or type(result) is not dict or result.get('pass_id') not in (None, rec['pass_id'])
                or result.get('intent_hash') not in (None, rec['intent_hash'])):
            raise ValueError('Pass closure result binding')
    if row is not None and (row[0], row[1], row[2], row[3]) != (rec['pass_id'], rec['intent_hash'], digest(rec), rec['status']):
        raise ValueError('Pass closure row binding')
    intent = terminal._load(store, rec['intent_hash'])
    before, positions, ledger_path = _baselines(intent)
    if rec['role'] != ('HELD' if positions else 'ENTRY') or set(rec['scans']) != set(before):
        raise ValueError('Pass closure role/scan binding')
    refs_by_scan = {}
    for scan, window in rec['scans'].items():
        current = progress.admission(scan)
        if (type(window) is not dict or set(window) != {'before', 'after'} or window['before'] != before[scan]
                or type(window['after']) is not int or current is None
                or not window['before'] <= window['after'] <= current['request_ceiling']
                or current['requests_used'] < window['after']):
            raise ValueError('Pass closure charge window or refund')
        refs_by_scan[scan] = {}
    for key in rec['attempt_refs']:
        attempt = terminal._load(store, key)
        scan = attempt.get('scan_id') if type(attempt) is dict else None
        used = attempt.get('requests_used') if type(attempt) is dict else None
        if (type(attempt) is not dict or attempt.get('kind') != 'paper_read_attempt_v1' or scan not in rec['scans']
                or type(used) is not int or not rec['scans'][scan]['before'] < used <= rec['scans'][scan]['after']
                or used in refs_by_scan[scan]):
            raise ValueError('Pass closure attempt reference')
        refs_by_scan[scan][used] = key
    monitoring = rec['monitoring']
    if monitoring is not None:
        first = intent.get('monitoring_total')
        if (type(monitoring) is not dict or set(monitoring) != {'first', 'last'} or type(first) is not int
                or monitoring['first'] != first+1 or type(monitoring['last']) is not int or monitoring['last'] < monitoring['first']):
            raise ValueError('Pass closure monitoring window')
        with closing(store.connect()) as c:
            count = c.execute('SELECT count(*) FROM paper_monitoring_reservations r JOIN paper_monitoring_outcomes o '
                              'ON o.reservation_id=r.id WHERE r.id BETWEEN ? AND ?',
                              (monitoring['first'], monitoring['last'])).fetchone()[0]
        if count != monitoring['last']-monitoring['first']+1:
            raise ValueError('Pass closure monitoring outcomes incomplete')
    led = rec['ledger']
    if (type(led) is not dict or set(led) != {'path', 'events', 'outcomes', 'events_hash', 'outcomes_hash'}
            or led['path'] != ledger_path or type(led['events']) is not int or type(led['outcomes']) is not int
            or not runtime._hash(led['events_hash']) or not runtime._hash(led['outcomes_hash'])):
        raise ValueError('Pass closure ledger anchor shape')
    if ledger:
        from .paper_cycle_no_entry import _anchor_holds
        if not _anchor_holds(ledger_path, led):
            raise ValueError('Pass closure ledger prefix changed')
    retired = rec['retired_scan']
    if retired is not None and (rec['role'] != 'ENTRY' or rec['status'] != 'FAILED_CHARGED' or set(rec['scans']) != {retired}):
        raise ValueError('Pass closure retirement binding')
    return intent


def close(store, progress, *, pass_id, status, cause, result_hash=None, attempt_refs=(), ledger_before=None):
    """Retire one unresolved pass. Caller holds research -> evidence -> ledger locks. Raises ClosureRefused/ValueError."""
    if status not in STATUSES:
        raise ClosureRefused('Unknown closure status')
    if is_integrity(cause):
        raise ClosureRefused('Integrity cause stays latched')
    with closing(store.connect()) as c:
        row = c.execute('SELECT intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?', (pass_id,)).fetchone()
        if row is None or row[1] is not None:
            raise ClosureRefused('Not an unresolved original pass')
        intent_hash = row[0]
        if pass_id in certified_ids(c):
            raise ClosureRefused('Pass is certified by a reviewed receipt; its NULL outcome is load-bearing')
        intent = terminal._load(store, intent_hash)
        before, positions, ledger_path = _baselines(intent)
        monitoring = _monitoring_window(c, intent) if positions else None
        others = c.execute('SELECT count(*) FROM paper_observation_passes WHERE outcome_hash IS NULL AND id!=? AND intent_hash=?',
                           (pass_id, intent_hash)).fetchone()[0]
    if others:
        raise ClosureRefused('Ambiguous duplicate unresolved pass')
    _ledger_valid(ledger_path)
    from .paper_cycle_no_entry import ledger_snapshot
    snapshot = ledger_snapshot(ledger_path)
    scans = {}
    for scan, start in before.items():
        current = progress.admission(scan)
        if (current is None or current['state'] not in ('ADMITTED', 'SEALED') or type(current['requests_used']) is not int
                or not start <= current['requests_used'] <= current['request_ceiling']):
            raise ClosureRefused('Charge accounting for scan is not provable')
        scans[scan] = {'before': start, 'after': current['requests_used']}
    refs = []
    for key in dict.fromkeys(attempt_refs):
        attempt = terminal._load(store, key)
        scan = attempt.get('scan_id') if type(attempt) is dict else None
        used = attempt.get('requests_used') if type(attempt) is dict else None
        if (type(attempt) is dict and attempt.get('kind') == 'paper_read_attempt_v1' and scan in scans
                and type(used) is int and scans[scan]['before'] < used <= scans[scan]['after']):
            refs.append(key)
    retired = None
    if (status == 'FAILED_CHARGED' and not positions and len(scans) == 1 and ledger_before is not None
            and all(w['after'] > w['before'] for w in scans.values())
            and (ledger_before['events'], ledger_before['outcomes']) == (snapshot['events'], snapshot['outcomes'])
            and ledger_before['events_hash'] == snapshot['events_hash']
            and ledger_before['outcomes_hash'] == snapshot['outcomes_hash']):
        retired = next(iter(scans))        # a failed lone candidate that changed nothing is never retried
    rec = {'kind': KIND, 'version': 1, 'status': status, 'cause': cause, 'role': 'HELD' if positions else 'ENTRY',
           'pass_id': pass_id, 'intent_hash': intent_hash, 'result_hash': result_hash if status == 'FAILED_CHARGED' else None,
           'scans': scans, 'attempt_refs': refs[:MAX_REFS], 'monitoring': monitoring,
           'ledger': {'path': ledger_path, **snapshot}, 'retired_scan': retired,
           'execution_status': 'EXECUTION_UNVERIFIED', 'live_readiness': False, 'entry_authorized': False}
    _verify(store, progress, rec)
    key = store.save(rec)
    with closing(store.connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            existing = _table_rows(c)
            if any(r[0] == pass_id for r in existing):
                raise ClosureRefused('Pass closure already bound')
            if not existing and not c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
                c.execute(SQL)
                for sql in GUARDS.values():
                    c.execute(sql)
            c.execute(f'INSERT INTO {TABLE} VALUES(?,?,?,?,?)', (pass_id, intent_hash, key, status, retired))
            if c.execute('UPDATE paper_observation_passes SET outcome_hash=? WHERE id=? AND intent_hash=? AND outcome_hash IS NULL',
                         (key, pass_id, intent_hash)).rowcount != 1:
                raise ClosureRefused('Pass publication changed')
            _table_rows(c)
            c.commit()
        except BaseException:
            c.rollback()
            raise
    return key


def hold(store, *, pass_id, cause, result_hash=None):
    """Durably record that a pass FINISHED with an integrity cause and must stay latched (pass stays NULL)."""
    with closing(store.connect()) as c:
        row = c.execute('SELECT intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?', (pass_id,)).fetchone()
    if row is None or row[1] is not None:
        raise ClosureRefused('Not an unresolved original pass')
    rec = {'kind': KIND, 'version': 1, 'status': HOLD, 'cause': str(cause)[:128] or 'UNSPECIFIED', 'pass_id': pass_id,
           'intent_hash': row[0], 'result_hash': result_hash, 'execution_status': 'EXECUTION_UNVERIFIED',
           'live_readiness': False, 'entry_authorized': False}
    _verify_hold(store, rec)
    key = store.save(rec)
    with closing(store.connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            existing = _table_rows(c)
            if any(r[0] == pass_id for r in existing):
                raise ClosureRefused('Pass already bound')
            if not existing and not c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
                c.execute(SQL)
                for sql in GUARDS.values():
                    c.execute(sql)
            c.execute(f'INSERT INTO {TABLE} VALUES(?,?,?,?,NULL)', (pass_id, row[0], key, HOLD))
            _table_rows(c)
            c.commit()
        except BaseException:
            c.rollback()
            raise
    return key


def recover_abandoned(store, progress, *, ledger_db, cfg, clock):
    """Close every unresolved pass left by a dead process. Caller owns research -> evidence -> ledger locks.

    Only passes created by this code (intent closure_v1) that are neither integrity-held nor certified by a reviewed
    receipt are eligible; everything else keeps its NULL outcome untouched. At most MAX_RECOVER passes are attempted
    per call. Returns {'closed': [...], 'refused': [(pass_id, reason)], 'monitoring_resolved': [...]}.
    """
    outcome = {'closed': [], 'refused': [], 'monitoring_resolved': []}
    with closing(store.connect()) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_observation_passes'").fetchone():
            return outcome
        skip = {r[0] for r in _table_rows(c) if r[3] == HOLD} | certified_ids(c)
        pending = [r[0] for r in c.execute('SELECT id FROM paper_observation_passes WHERE outcome_hash IS NULL ORDER BY rowid LIMIT 1024')
                   if r[0] not in skip]
        has_monitoring = bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_monitoring_outcomes'").fetchone())
        dangling = has_monitoring and c.execute('SELECT 1 FROM paper_monitoring_reservations r LEFT JOIN paper_monitoring_outcomes o '
                                                'ON o.reservation_id=r.id WHERE o.reservation_id IS NULL LIMIT 1').fetchone()
    eligible = []
    for pass_id in pending:
        with closing(store.connect()) as c:
            intent_hash = c.execute('SELECT intent_hash FROM paper_observation_passes WHERE id=?', (pass_id,)).fetchone()[0]
        try:
            if terminal._load(store, intent_hash).get('closure_v1') is True:
                eligible.append(pass_id)
        except (ValueError, OSError, sqlite3.Error):
            pass                                             # unreadable intent: never touched
    if len(eligible) > MAX_RECOVER:
        outcome['refused'].append((None, 'Too many unresolved passes for bounded recovery'))
        return outcome
    if dangling and eligible:
        from .monitoring_budget import MonitoringBudget, MonitoringBlocked
        try:
            outcome['monitoring_resolved'] = MonitoringBudget(store, ledger_db, cfg, clock=clock).abandon_pending()
        except (MonitoringBlocked, ValueError, OSError, sqlite3.Error) as error:
            outcome['refused'].append((None, 'Monitoring reservations unresolved: ' + str(error)[:100]))
    for pass_id in eligible:
        try:
            close(store, progress, pass_id=pass_id, status='ABANDONED_CHARGED', cause=ABANDONED_CAUSE)
            outcome['closed'].append(pass_id)
        except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as error:
            outcome['refused'].append((pass_id, str(error)[:120]))
    return outcome


def gate(store, research, scan_ids, *, ledger_locked=None, review_source=None):
    """Cheap binding inventory on every call; full replay only for closures that retire a requested scan."""
    with closing(store.connect()) as c:
        retired = _table_rows(c)
        if not retired:
            return None
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_observation_passes'").fetchone():
            raise ValueError('Pass closure original pass table missing')
        bound = c.execute(f'SELECT count(*) FROM {TABLE} t JOIN paper_observation_passes p ON p.id=t.pass_id '
                          'AND p.intent_hash=t.intent_hash AND p.outcome_hash=t.closure_hash WHERE t.status!=?', (HOLD,)).fetchone()[0]
        holds = c.execute(f'SELECT count(*) FROM {TABLE} t JOIN paper_observation_passes p ON p.id=t.pass_id '
                          'AND p.intent_hash=t.intent_hash WHERE t.status=?', (HOLD,)).fetchone()[0]
    if bound != sum(1 for r in retired if r[3] != HOLD) or holds != sum(1 for r in retired if r[3] == HOLD):
        raise ValueError('Pass closure binding inventory incomplete')
    matched = [row for row in retired if row[4] is not None and row[4] in scan_ids]
    if matched:
        progress = HistoryProgress.__new__(HistoryProgress)
        progress.store = store
        from .paper_cycle import _lock, canonical_job_path
        for row in matched:
            rec = terminal._load(store, row[2])
            if digest(rec) != row[2] or rec.get('retired_scan') != row[4]:
                raise ValueError('Pass closure row/record conflict')
            path = canonical_job_path(rec['ledger']['path'])
            with ExitStack() as locks:
                if ledger_locked != str(path) and not locks.enter_context(_lock(str(path)+'.paper-cycle.lock')):
                    raise ValueError('Pass closure ledger busy')
                _verify(store, progress, rec, row=row)
        return 'REJECTED_SCAN_RETIRED'
    return None
