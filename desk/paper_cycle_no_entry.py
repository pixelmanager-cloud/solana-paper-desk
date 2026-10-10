"""Generic terminal NO_ENTRY outcome for normal, typed, verified cycle rejections.

A charged paper-cycle pass that ends in an allow-listed deterministic blocker
(category a) publishes this immutable outcome in the same evidence transaction
that fills the pass outcome. Anything else (category b: transport ambiguity,
binding/clock/integrity failures, recovery states, crashes) keeps its NULL
outcome and the global gate keeps failing closed. The proof is parameterised by
the pass shape, replayed on every gate, and bound two ways to the pass table.

Charged requests stay charged, the scan is retired (never retried), the ledger
prefix is anchored, and no entry, retry, counter reset or waiver is created.
Caller owns research -> evidence -> ledger locks.
"""
from contextlib import closing, ExitStack
from pathlib import Path
import sqlite3
from . import paper_terminal_reconciliation as terminal, runtime_compatibility as runtime
from . import paper_preparation_retirement as historical
from .history_progress import HistoryProgress
from .model import canonical, digest

TABLE = 'paper_cycle_no_entry'
KIND = 'paper_cycle_no_entry_v1'
MAX_ROWS = 8192  # below the 10000 original-pass ceiling
ANCHOR_EVENTS = 256  # bounded immutable ledger prefix; counts must never shrink
# Category (a): deterministic outcomes of verified, completed reads; no entry.
NORMAL = frozenset({
    'MARKET_PRODUCER_BLOCKED', 'RETAINED_MIGRATION_WITNESS_REQUIRED',
    'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED', 'CYCLE_REQUEST_BUDGET_EXHAUSTED',
    'FEATURE_HISTORY_PAGE_LIMIT', 'EVENT_READER_SIZE_LIMIT', 'QUOTE_DEMAND_LIMIT'})
RESULT_FIELDS = {'kind', 'status', 'execution_status', 'live_readiness', 'attempted_requests', 'outcomes',
                 'events', 'blockers', 'budget', 'diagnostics', 'usd_evidence_refs', 'pass_id',
                 'intent_hash', 'attempt_refs', 'investigation_attempted_requests',
                 'monitoring_attempted_requests'}
FIELDS = {'kind', 'version', 'status', 'execution_status', 'live_readiness', 'entry_authorized', 'mode',
          'pass_id', 'scan_id', 'intent_hash', 'result_hash', 'blocker', 'admission_before',
          'admission_after', 'charged', 'attempt_refs', 'ledger'}
SQL = (f'CREATE TABLE {TABLE}(pass_id TEXT PRIMARY KEY,scan_id TEXT NOT NULL UNIQUE,'
       'intent_hash TEXT NOT NULL,outcome_hash TEXT NOT NULL)')
GUARDS = {TABLE+'_insert': f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE pass_id=NEW.pass_id OR scan_id=NEW.scan_id OR rowid=NEW.rowid) OR (SELECT count(*) FROM {TABLE})>={MAX_ROWS} BEGIN SELECT RAISE(ABORT,'Cycle no-entry immutable'); END"}
GUARDS |= {TABLE+'_'+a.lower(): f"CREATE TRIGGER {TABLE}_{a.lower()} BEFORE {a} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Cycle no-entry immutable'); END" for a in ('UPDATE', 'DELETE')}


def ledger_snapshot(path):
    """Counts plus a bounded immutable prefix anchor of the cycle ledger."""
    with closing(sqlite3.connect(Path(path).as_uri()+'?mode=ro', uri=True)) as c:
        c.execute('BEGIN')
        events = c.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        outcomes = c.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0]
        ke, ko = min(events, ANCHOR_EVENTS), min(outcomes, ANCHOR_EVENTS)
        return {'events': events, 'outcomes': outcomes,
                'events_hash': runtime._prefix(c, 'events', ke), 'outcomes_hash': runtime._prefix(c, 'outcomes', ko)}


def _anchor_holds(path, ledger):
    with closing(sqlite3.connect(Path(path).as_uri()+'?mode=ro', uri=True)) as c:
        c.execute('BEGIN')
        events = c.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        outcomes = c.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0]
        return (events >= ledger['events'] and outcomes >= ledger['outcomes']
                and runtime._prefix(c, 'events', min(ledger['events'], ANCHOR_EVENTS)) == ledger['events_hash']
                and runtime._prefix(c, 'outcomes', min(ledger['outcomes'], ANCHOR_EVENTS)) == ledger['outcomes_hash'])


def _index(store, scans):
    """One bounded pass over retained attempt originals, keyed scan -> charge index."""
    with closing(store.connect()) as c:
        bad = c.execute("SELECT 1 FROM pages WHERE typeof(hash)!='text' OR length(CAST(hash AS BLOB))!=64 OR typeof(payload)!='blob' OR length(payload) NOT BETWEEN 1 AND ? OR typeof(raw_bytes)!='integer' OR CASE WHEN typeof(raw_bytes)='integer' THEN raw_bytes NOT BETWEEN 1 AND ? ELSE 1 END LIMIT 1",
                        (terminal.MAX_PROOF_COMPRESSED_BYTES, terminal.MAX_PROOF_RAW_BYTES)).fetchone()
        count, total, compressed = c.execute('SELECT count(*),COALESCE(sum(raw_bytes),0),COALESCE(sum(length(payload)),0) FROM pages').fetchone()
        if bad or count > 4096 or total > 256*1024*1024 or compressed > 256*1024*1024:
            raise ValueError('Cycle no-entry attempt inventory bound')
        keys = [r[0] for r in c.execute('SELECT hash FROM pages ORDER BY hash')]
    found = {scan: {} for scan in scans}
    for key in keys:
        record = terminal._load(store, key)
        if type(record) is not dict or record.get('kind') != 'paper_read_attempt_v1' or record.get('scan_id') not in found:
            continue
        used = record.get('requests_used')
        if type(used) is not int or not 1 <= used <= 18:
            raise ValueError('Cycle no-entry attempt charge malformed')
        if used in found[record['scan_id']]:
            raise ValueError('Cycle no-entry attempt charge ambiguous')
        found[record['scan_id']][used] = (key, record)
    return found


def _check_result(result, blocker, scan, before, after):
    if (type(result) is not dict or set(result) != RESULT_FIELDS or result['kind'] != 'paper_cycle_v1'
            or result['status'] != 'BLOCKED' or result['blockers'] != [blocker] or blocker not in NORMAL
            or result['execution_status'] != 'EXECUTION_UNVERIFIED' or result['live_readiness'] is not False
            or result['events'] != [] or result['outcomes'] != []
            or type(result['attempted_requests']) is not int or result['attempted_requests'] != after-before
            or type(result['investigation_attempted_requests']) is not int
            or result['investigation_attempted_requests'] != after-before
            or type(result['monitoring_attempted_requests']) is not int or result['monitoring_attempted_requests'] != 0
            or result['budget'] != {scan: {'used': after, 'ceiling': 18}}
            or type(result['attempt_refs']) is not list or type(result['usd_evidence_refs']) is not list
            or not all(runtime._hash(k) for k in result['attempt_refs']+result['usd_evidence_refs'])
            or type(result['diagnostics']) is not list):
        raise ValueError('Normal typed cycle rejection result required')
    graduation = [d for d in result['diagnostics'] if type(d) is dict and 'graduation' in d]
    if len(graduation) != 1 or graduation[0].get('scan_id') != scan or type(graduation[0]['graduation']) is not dict:
        raise ValueError('Candidate graduation diagnostic required')
    observed = graduation[0]['graduation'].get('status') == 'OBSERVED_MIGRATION'
    if (blocker == 'RETAINED_MIGRATION_WITNESS_REQUIRED') == observed:
        raise ValueError('Blocker disagrees with graduation diagnostic')
    if blocker == 'MARKET_PRODUCER_BLOCKED' and not any(
            type(d) is dict and d.get('scan_id') == scan and type(d.get('blockers')) is list and d['blockers']
            and all(type(x) is str for x in d['blockers']) for d in result['diagnostics']):
        raise ValueError('Producer blockers must be retained')


def _proof(store, progress, rec, index, *, publishing=False):
    if (type(rec) is not dict or set(rec) != FIELDS or rec['kind'] != KIND or type(rec['version']) is not int
            or rec['version'] != 1 or rec['status'] != 'NO_ENTRY' or rec['execution_status'] != 'EXECUTION_UNVERIFIED'
            or rec['live_readiness'] is not False or rec['entry_authorized'] is not False
            or rec['mode'] != 'single_candidate' or rec['blocker'] not in NORMAL
            or not terminal._id(rec['pass_id']) or type(rec['scan_id']) is not str or not 1 <= len(rec['scan_id']) <= 256
            or not runtime._hash(rec['intent_hash']) or not runtime._hash(rec['result_hash'])):
        raise ValueError('Cycle no-entry shape')
    scan = rec['scan_id']
    intent = terminal._load(store, rec['intent_hash']); result = terminal._load(store, rec['result_hash'])
    if (type(intent) is not dict or intent.get('kind') != 'paper_cycle_intent_v1' or type(intent.get('targets')) is not list
            or len(intent['targets']) != 1 or intent['targets'][0]['target']['scan_id'] != scan
            or type(intent.get('admissions')) is not dict or set(intent['admissions']) != {scan}):
        raise ValueError('Single candidate cycle intent required')
    before = intent['admissions'][scan]; after = rec['admission_after']
    if (before != rec['admission_before'] or before.get('state') not in ('ADMITTED', 'SEALED') or before.get('request_ceiling') != 18
            or type(before.get('requests_used')) is not int or type(after) is not dict
            or after != {**before, 'requests_used': after.get('requests_used')} or type(after['requests_used']) is not int
            or not before['requests_used'] < after['requests_used'] <= 18
            or rec['charged'] != after['requests_used']-before['requests_used']):
        raise ValueError('Cycle no-entry admission binding')
    _check_result(result, rec['blocker'], scan, before['requests_used'], after['requests_used'])
    if result['pass_id'] != rec['pass_id'] or result['intent_hash'] != rec['intent_hash']:
        raise ValueError('Cycle no-entry result/pass binding')
    # Complete, successful, unambiguous charged-attempt inventory.
    refs = []
    for n in range(before['requests_used']+1, after['requests_used']+1):
        if n not in index.get(scan, {}):
            raise ValueError('Cycle no-entry charged attempt missing')
        key, record = index[scan][n]
        if (record.get('failure_code') is not None or type(record.get('http_status')) is not int or record['http_status'] != 200
                or type(record.get('observed_at')) is not int):
            raise ValueError('Cycle no-entry charged attempt incomplete or failed')
        refs.append(key)
    if refs != rec['attempt_refs'] or not set(result['attempt_refs']+result['usd_evidence_refs']) <= set(refs):
        raise ValueError('Cycle no-entry attempt inventory mismatch')
    if progress.admission(scan) != after:
        raise ValueError('Cycle no-entry live charge changed')
    ledger = rec['ledger']
    if (type(ledger) is not dict or set(ledger) != {'path', 'events', 'outcomes', 'events_hash', 'outcomes_hash'}
            or ledger['path'] != intent.get('ledger') or not runtime._hash(ledger['events_hash'])
            or not runtime._hash(ledger['outcomes_hash']) or type(ledger['events']) is not int or type(ledger['outcomes']) is not int
            or not _anchor_holds(ledger['path'], ledger)):
        raise ValueError('Cycle no-entry ledger anchor')
    if publishing:
        with closing(sqlite3.connect(Path(ledger['path']).as_uri()+'?mode=ro', uri=True)) as c:
            if (c.execute('SELECT COUNT(*) FROM events').fetchone()[0] != ledger['events']
                    or c.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0] != ledger['outcomes']):
                raise ValueError('Ledger changed during rejected pass')
    return intent


def rows(c):
    historical._schema_bounds(c)
    objects = c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name=? OR substr(name,1,?)=? OR tbl_name=?",
                        (TABLE, len(TABLE)+1, TABLE+'_', TABLE)).fetchall()
    if not objects:
        return []
    expected = {('table', TABLE, TABLE, SQL)} | {('trigger', n, TABLE, s) for n, s in GUARDS.items()}
    expected |= {('index', f'sqlite_autoindex_{TABLE}_{i}', TABLE, None) for i in (1, 2)}
    if set(objects) != expected or len(objects) != len(expected):
        raise ValueError('Cycle no-entry schema malformed')
    count = c.execute('SELECT count(*) FROM '+TABLE).fetchone()[0]
    if not 1 <= count <= MAX_ROWS:
        raise ValueError('Cycle no-entry count invalid')
    if c.execute('SELECT 1 FROM '+TABLE+" WHERE typeof(pass_id)!='text' OR length(CAST(pass_id AS BLOB))!=32 OR typeof(scan_id)!='text' OR length(CAST(scan_id AS BLOB)) NOT BETWEEN 1 AND 256 OR typeof(intent_hash)!='text' OR length(CAST(intent_hash AS BLOB))!=64 OR typeof(outcome_hash)!='text' OR length(CAST(outcome_hash AS BLOB))!=64 LIMIT 1").fetchone():
        raise ValueError('Cycle no-entry scalar malformed')
    result = c.execute('SELECT pass_id,scan_id,intent_hash,outcome_hash FROM '+TABLE).fetchall()
    if any(not terminal._id(p) or not runtime._hash(i) or not runtime._hash(o) for p, _, i, o in result):
        raise ValueError('Cycle no-entry scalar malformed')
    return result


def publish(store, progress, *, pass_id, intent_hash, result, ledger):
    """Atomically retire a normal rejection; raises (leaving the NULL latch) on any doubt."""
    if type(result) is not dict or len(result.get('blockers', ())) != 1 or result['blockers'][0] not in NORMAL:
        raise ValueError('Not a category (a) rejection')
    result_hash = store.save(result)
    intent = terminal._load(store, intent_hash)
    scan = intent['targets'][0]['target']['scan_id']
    before = intent['admissions'][scan]; after = progress.admission(scan)
    index = _index(store, {scan})
    attempts = [index[scan][n][0] for n in range(before['requests_used']+1, after['requests_used']+1) if n in index[scan]]
    rec = {'kind': KIND, 'version': 1, 'status': 'NO_ENTRY', 'execution_status': 'EXECUTION_UNVERIFIED',
           'live_readiness': False, 'entry_authorized': False, 'mode': 'single_candidate', 'pass_id': pass_id,
           'scan_id': scan, 'intent_hash': intent_hash, 'result_hash': result_hash, 'blocker': result['blockers'][0],
           'admission_before': before, 'admission_after': after, 'charged': after['requests_used']-before['requests_used'],
           'attempt_refs': attempts, 'ledger': {'path': intent['ledger'], **ledger}}
    _proof(store, progress, rec, index, publishing=True)
    key = store.save(rec)
    with closing(store.connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            _proof(store, progress, rec, _index(store, {scan}), publishing=True)
            existing = rows(c)
            if any(row[0] == pass_id or row[1] == scan for row in existing):
                raise ValueError('Cycle no-entry already bound')
            if not existing:
                c.execute(SQL)
                for sql in GUARDS.values():
                    c.execute(sql)
            c.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?)', (pass_id, scan, intent_hash, key))
            if c.execute('UPDATE paper_observation_passes SET outcome_hash=? WHERE id=? AND intent_hash=? AND outcome_hash IS NULL',
                         (key, pass_id, intent_hash)).rowcount != 1:
                raise ValueError('Cycle no-entry pass publication changed')
            rows(c); c.commit()
        except BaseException:
            c.rollback(); raise
    return key


def gate(store, research, scan_ids, *, ledger_locked=None, review_source=None):
    """Replay every retained rejection; deleting the table cannot erase a bound outcome."""
    progress = HistoryProgress.__new__(HistoryProgress); progress.store = store
    with closing(store.connect()) as c:
        retired = rows(c)
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_observation_passes'").fetchone():
            if retired:
                raise ValueError('Cycle no-entry original pass table missing')
            return None
        terminal._passes(c)
        passes = c.execute('SELECT id,intent_hash,outcome_hash FROM paper_observation_passes WHERE outcome_hash IS NOT NULL').fetchall()
    bound = set()
    for identity, intent_key, outcome_key in passes:
        kind, scan, intent = terminal._classification(store, outcome_key)
        if kind == KIND:
            if intent != intent_key or scan is None:
                raise ValueError('Cycle no-entry outcome original pass conflict')
            bound.add((identity, scan, intent_key, outcome_key))
    if bound != set(retired):
        raise ValueError('Cycle no-entry inventory incomplete')
    if retired:
        index = _index(store, {scan for _, scan, _, _ in retired})
        for identity, scan, intent_key, outcome_key in retired:
            rec = terminal._load(store, outcome_key)
            if (rec.get('pass_id'), rec.get('scan_id'), rec.get('intent_hash')) != (identity, scan, intent_key):
                raise ValueError('Cycle no-entry row/outcome conflict')
            from .paper_cycle import _lock, canonical_job_path
            path = canonical_job_path(rec['ledger']['path'])
            with ExitStack() as locks:
                if ledger_locked != str(path) and not locks.enter_context(_lock(str(path)+'.paper-cycle.lock')):
                    raise ValueError('Cycle no-entry ledger busy')
                original = _proof(store, progress, rec, index)
            if original['targets'][0]['target']['scan_id'] != scan:
                raise ValueError('Cycle no-entry scan conflict')
    return 'REJECTED_SCAN_RETIRED' if any(row[1] in scan_ids for row in retired) else None
