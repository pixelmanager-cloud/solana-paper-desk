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
import json
import logging
import os
from pathlib import Path
import sqlite3
from . import paper_terminal_reconciliation as terminal, runtime_compatibility as runtime
from . import paper_preparation_retirement as historical
from . import verified_index, pass_inventory
from .history_progress import HistoryProgress
from .model import BOOST_VOLUME_FEATURE, OBSERVABLE_FORMULAS, canonical, digest

TABLE = 'paper_cycle_no_entry'
KIND = 'paper_cycle_no_entry_v1'
MAX_ROWS = 8192  # below the 10000 original-pass ceiling
ANCHOR_EVENTS = 256  # bounded immutable ledger prefix; counts must never shrink
# Category (a): deterministic outcomes of verified, completed reads; no entry.
NORMAL = frozenset({
    'MARKET_PRODUCER_BLOCKED', 'RETAINED_MIGRATION_WITNESS_REQUIRED',
    'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED', 'CYCLE_REQUEST_BUDGET_EXHAUSTED',
    'FEATURE_HISTORY_PAGE_LIMIT'})
# Codes whose cause is not recoverable from retained originals stay category (b):
# EVENT_READER_SIZE_LIMIT and QUOTE_DEMAND_LIMIT (the oversized event/demand plan is not retained).
# Inner vocabulary of MARKET_PRODUCER_BLOCKED (desk/paper_market_adapter.py). paper_cycle.fulfill_quotes collapses every
# adapter blocker into that one top-level code, so the top-level code alone cannot tell an empty/stale window (a
# deterministic outcome of verified reads) from corrupt, contradictory or unbound supplied evidence. Only this
# positive list is category (a); any other retained producer blocker leaves the pass NULL (fail closed).
WINDOW_BLOCKER_PREFIXES = ('MISSING_WINDOW_MEASUREMENT:', 'STALE_WINDOW_MEASUREMENT:')
WINDOW_MEASUREMENTS = frozenset(OBSERVABLE_FORMULAS) | {
    'net_buy_ratio', 'unique_buyers_5m', 'volume_vs_liq', BOOST_VOLUME_FEATURE, 'drawdown_from_high'}
# Integrity / contradiction / binding codes: never a normal rejection, even if an operator declares one as a hazard.
PRODUCER_INTEGRITY = frozenset({
    'COLLECTOR_TARGET_OBSERVATION_REQUIRED', 'COORDINATOR_TARGET_BINDING_MISMATCH', 'COLLECTOR_OBSERVATION_REJECTED',
    'COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID', 'BOUNDED_RAW_TRADE_SEQUENCE_REQUIRED',
    'CAPTURED_HISTORY_WINDOW_STALE_OR_FUTURE', 'SOL_USD_SOURCE_OR_EXACT_BLOCK_TIME_MISSING',
    'SOL_USD_TRUSTED_INPUT_INVALID', 'EXPLICIT_OBSERVABLE_SIGNAL_PROFILE_REQUIRED',
    'EXPLICIT_PAPER_FEE_ASSUMPTION_REQUIRED', 'PAPER_EVENT_BYTE_LIMIT_EXCEEDED', 'EXPERIMENTAL_EVENT_CONTRACT_INVALID'})
RESERVED_PREFIXES = WINDOW_BLOCKER_PREFIXES + ('SOL_USD:', 'ORIGINAL_', 'COLLECTOR_', 'COORDINATOR_')


def producer_blocker_is_normal(code, declared_hazards=()):
    """True only for a window-measurement blocker of a known measurement, or a hazard the operator declared."""
    if type(code) is not str:
        return False
    for prefix in WINDOW_BLOCKER_PREFIXES:
        if code.startswith(prefix):
            return code[len(prefix):] in WINDOW_MEASUREMENTS
    if code in PRODUCER_INTEGRITY or code.startswith(RESERVED_PREFIXES):
        return False
    return code in declared_hazards


RESULT_FIELDS = {'kind', 'status', 'execution_status', 'live_readiness', 'attempted_requests', 'outcomes',
                 'events', 'blockers', 'budget', 'diagnostics', 'usd_evidence_refs', 'pass_id',
                 'intent_hash', 'attempt_refs', 'investigation_attempted_requests',
                 'monitoring_attempted_requests'}
FIELDS = {'kind', 'version', 'status', 'execution_status', 'live_readiness', 'entry_authorized', 'mode',
          'pass_id', 'scan_id', 'intent_hash', 'result_hash', 'blocker', 'admission_before',
          'admission_after', 'charged', 'attempt_refs', 'ledger'}
SQL = (f'CREATE TABLE {TABLE}(pass_id TEXT PRIMARY KEY,scan_id TEXT NOT NULL UNIQUE,'
       'intent_hash TEXT NOT NULL,outcome_hash TEXT NOT NULL)')


def _guards(limit):
    guards = {TABLE+'_insert': f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE pass_id=NEW.pass_id OR scan_id=NEW.scan_id OR rowid=NEW.rowid) OR (SELECT count(*) FROM {TABLE})>={limit} BEGIN SELECT RAISE(ABORT,'Cycle no-entry immutable'); END"}
    guards |= {TABLE+'_'+a.lower(): f"CREATE TRIGGER {TABLE}_{a.lower()} BEFORE {a} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Cycle no-entry immutable'); END" for a in ('UPDATE', 'DELETE')}
    return guards


GUARDS = _guards(MAX_ROWS)
ROTATION_WARN_FRACTION = 0.8   # warn at 80% of MAX_ROWS so the operator rotates while flat, long before the hard stop
WARNING_KIND = 'rotation_warning_v1'


class RotationRequired(ValueError):
    """The retired-rejection table is full. Only new no-entry publications stop; nothing else is latched."""


REFUSAL_SUFFIX = '.publish-refused.jsonl'
REFUSAL_KIND = 'publish_refused_v1'
MAX_REFUSAL_BYTES = 1024 * 1024        # the log is append-only; past this it stops growing (logging continues)
LOG = logging.getLogger(__name__)


def refusal_path(store):
    return str(store.path) + REFUSAL_SUFFIX


def record_refusal(store, *, pass_id, scan_id, blocker, error, at):
    """Make a swallowed publish() refusal visible: one typed JSON line beside the evidence store plus a log record.

    Fail closed as before: the caller still leaves the pass unretired. This never raises (a monitoring aid must not
    change the cycle outcome); a failure to write is logged instead. Secrets are not involved: only identifiers,
    the exception class and a bounded message are kept.
    """
    row = {'kind': REFUSAL_KIND, 'at': at, 'pass_id': pass_id, 'scan_id': scan_id, 'blocker': blocker,
           'reason': type(error).__name__, 'detail': str(error)[:200]}
    LOG.warning('publish_refused pass=%s scan=%s blocker=%s reason=%s detail=%s',
                pass_id, scan_id, blocker, row['reason'], row['detail'])
    try:
        line = (canonical(row) + '\n').encode()
        fd = os.open(refusal_path(store), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            if os.fstat(fd).st_size + len(line) <= MAX_REFUSAL_BYTES:
                os.write(fd, line)
                os.fsync(fd)
            else:
                LOG.error('publish_refused log full; rotate the store set (RUNBOOK rotation)')
        finally:
            os.close(fd)
    except OSError as failure:
        LOG.error('publish_refused row not written: %s', type(failure).__name__)
    return row


def read_refusals(store_path):
    """Typed rows of the refusal log (oldest first); malformed lines are reported, never skipped silently."""
    try:
        with open(str(store_path) + REFUSAL_SUFFIX, 'rb') as handle:
            raw = handle.read(MAX_REFUSAL_BYTES + 1)
    except FileNotFoundError:
        return []
    out = []
    for number, line in enumerate(raw.splitlines(), 1):
        try:
            row = json.loads(line)
            if type(row) is not dict or row.get('kind') not in (REFUSAL_KIND, WARNING_KIND):
                raise ValueError
        except ValueError:
            row = {'kind': REFUSAL_KIND, 'reason': 'MALFORMED_LINE', 'line': number}
        out.append(row)
    return out


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


def _index(store, scan, refs):
    """Charge index of ONE scan, built from its explicitly bound attempt refs.

    Per-scan, O(refs): each ref is loaded by hash, so the cost does not depend on how many unrelated pages
    the store holds (the former whole-store scan hard-latched every cycle above 4096 pages). Completeness is
    not inferred from the store: the caller requires the refs to cover exactly the charged range
    before+1..after with one attempt per charge, and the refs are hash-bound by the published row.
    """
    if type(refs) is not list or len(refs) > 18 or len(set(refs)) != len(refs) or not all(runtime._hash(k) for k in refs):
        raise ValueError('Cycle no-entry attempt refs malformed')
    found = {}
    for key in refs:
        record = terminal._load(store, key)
        if type(record) is not dict or record.get('kind') != 'paper_read_attempt_v1' or record.get('scan_id') != scan:
            raise ValueError('Cycle no-entry attempt ref is not an attempt of this scan')
        used = record.get('requests_used')
        if type(used) is not int or not 1 <= used <= 18:
            raise ValueError('Cycle no-entry attempt charge malformed')
        if used in found:
            raise ValueError('Cycle no-entry attempt charge ambiguous')
        found[used] = (key, record)
    return {scan: found}


def _check_result(result, blocker, scan, before, after, hazards=()):
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
    if blocker == 'RETAINED_MIGRATION_WITNESS_REQUIRED' and graduation[0]['graduation'].get('blockers') != ['MIGRATION_WITNESS_ABSENT']:
        # T22F R5: only the plain absence of a witness is a normal rejection. Conflicting timestamps, account-binding
        # mismatches, regressing slot times and malformed raw transactions are contradictory evidence (category b).
        raise ValueError('Only an absent migration witness is a normal rejection')
    if blocker == 'MARKET_PRODUCER_BLOCKED' and not any(
            type(d) is dict and d.get('scan_id') == scan and type(d.get('blockers')) is list and d['blockers']
            and all(type(x) is str for x in d['blockers']) for d in result['diagnostics']):
        raise ValueError('Producer blockers must be retained')
    if blocker == 'MARKET_PRODUCER_BLOCKED' and any(
            type(d) is dict and 'blockers' in d and not (
                type(d['blockers']) is list and all(producer_blocker_is_normal(x, hazards) for x in d['blockers']))
            for d in result['diagnostics']):
        raise ValueError('Producer blockers outside the normal vocabulary')


def _consistent(blocker, before, after, records):
    """Necessary conditions of the blocker that the retained charged originals must show."""
    if blocker == 'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED' and after != 18:
        raise ValueError('Investigation budget exhaustion requires the full ceiling charged')
    if blocker == 'CYCLE_REQUEST_BUDGET_EXHAUSTED' and after-before != 18:
        raise ValueError('Cycle budget exhaustion requires eighteen charged requests')
    if blocker == 'FEATURE_HISTORY_PAGE_LIMIT' and sum(r.get('method') == 'getTransactionsForAddress' for r in records) < 8:
        raise ValueError('History page limit requires eight retained history pages')


def _proof(store, progress, rec, *, publishing=False):
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
    declared = intent['targets'][0].get('known_hazards', ())
    if type(declared) not in (list, tuple) or not all(type(x) is str for x in declared):
        raise ValueError('Cycle no-entry declared hazards malformed')
    _check_result(result, rec['blocker'], scan, before['requests_used'], after['requests_used'], tuple(declared))
    if result['pass_id'] != rec['pass_id'] or result['intent_hash'] != rec['intent_hash']:
        raise ValueError('Cycle no-entry result/pass binding')
    # Complete, successful, unambiguous charged-attempt inventory (per-scan, from the bound refs).
    if type(rec['attempt_refs']) is not list:
        raise ValueError('Cycle no-entry attempt inventory mismatch')
    index = _index(store, scan, rec['attempt_refs'])
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
    _consistent(rec['blocker'], before['requests_used'], after['requests_used'],
                [index[scan][n][1] for n in range(before['requests_used']+1, after['requests_used']+1)])
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
    # The refs the pass itself retained (charged read attempts plus USD evidence), ordered by charge index.
    supplied = result.get('attempt_refs'); usd = result.get('usd_evidence_refs')
    if type(supplied) is not list or type(usd) is not list:
        raise ValueError('Cycle no-entry attempt refs required')
    unique = list(dict.fromkeys(supplied+usd))
    indexed = _index(store, scan, unique)[scan]
    attempts = [indexed[n][0] for n in range(before['requests_used']+1, after['requests_used']+1) if n in indexed]
    rec = {'kind': KIND, 'version': 1, 'status': 'NO_ENTRY', 'execution_status': 'EXECUTION_UNVERIFIED',
           'live_readiness': False, 'entry_authorized': False, 'mode': 'single_candidate', 'pass_id': pass_id,
           'scan_id': scan, 'intent_hash': intent_hash, 'result_hash': result_hash, 'blocker': result['blockers'][0],
           'admission_before': before, 'admission_after': after, 'charged': after['requests_used']-before['requests_used'],
           'attempt_refs': attempts, 'ledger': {'path': intent['ledger'], **ledger}}
    _proof(store, progress, rec, publishing=True)
    key = store.save(rec)
    with closing(store.connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            _proof(store, progress, rec, publishing=True)
            existing = rows(c)
            if any(row[0] == pass_id or row[1] == scan for row in existing):
                raise ValueError('Cycle no-entry already bound')
            if len(existing) >= MAX_ROWS:
                # Hard stop. It refuses only THIS publication (the caller closes the pass FAILED_CHARGED, so no
                # position or pass is left latched) and every retained row still replays: rotate while flat.
                raise RotationRequired('Cycle no-entry table full (%d rows); rotate the store set when flat' % len(existing))
            if not existing:
                c.execute(SQL)
                for sql in GUARDS.values():
                    c.execute(sql)
            c.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?)', (pass_id, scan, intent_hash, key))
            if c.execute('UPDATE paper_observation_passes SET outcome_hash=? WHERE id=? AND intent_hash=? AND outcome_hash IS NULL',
                         (key, pass_id, intent_hash)).rowcount != 1:
                raise ValueError('Cycle no-entry pass publication changed')
            # T24R F7: the full proof above passed; record what was proved in the same transaction as the receipt.
            verified_index.record(c, INDEX_KIND, pass_id, key, verified_index.proof_digest(INDEX_KIND, key, pass_id, scan, intent_hash))
            pass_inventory.record(c, pass_id, intent_hash, key, KIND, scan, intent_hash)      # T24S: same transaction as the outcome
            verified_index.rows(c)
            total = len(rows(c)); c.commit()
        except BaseException:
            c.rollback(); raise
    if total >= int(MAX_ROWS * ROTATION_WARN_FRACTION):
        warn_rotation(store, total)
    warn_if_crowded(store)
    return key


PAGE_SOFT_LIMIT = 100_000      # evidence pages: advisory only (T24R F7). There is no store-wide page-count latch any more.
INDEX_KIND = 'cycle_no_entry'


def storage_headroom(store):
    """Pages and compressed bytes held by the evidence store against the advisory page limit and its write budget."""
    with closing(store.connect()) as c:
        pages, size = c.execute('SELECT count(*), COALESCE(sum(length(payload)),0) FROM pages').fetchone()
    byte_limit = getattr(store, 'max_bytes', 256 * 1024 * 1024)
    return {'pages': pages, 'pages_limit': PAGE_SOFT_LIMIT, 'bytes': size, 'bytes_limit': byte_limit,
            'pages_warn': pages >= int(PAGE_SOFT_LIMIT * ROTATION_WARN_FRACTION),
            'bytes_warn': size >= int(byte_limit * ROTATION_WARN_FRACTION)}


def warn_if_crowded(store):
    """80% rotation warnings for evidence pages / bytes: a typed row in the refusal log (read_refusals) plus the log."""
    try:
        h = storage_headroom(store)
    except (sqlite3.Error, OSError, ValueError) as failure:
        LOG.error('evidence headroom not measured: %s', type(failure).__name__)
        return None
    if h['pages_warn']:
        warn_rotation(store, h['pages'], limit=h['pages_limit'], label='evidence_pages')
    if h['bytes_warn']:
        warn_rotation(store, h['bytes'], limit=h['bytes_limit'], label='evidence_bytes')
    return h


def warn_rotation(store, total, *, limit=None, label='no_entry'):
    """Typed, visible 80% capacity warning (log + the refusal log file the healthcheck reads)."""
    limit = MAX_ROWS if limit is None else limit
    LOG.warning('rotation_warning %s rows=%d of %d; rotate the store set while flat (RUNBOOK "Rotate when flat")',
                label, total, limit)
    try:
        line = (canonical({'kind': WARNING_KIND, 'table': label, 'rows': total, 'limit': limit}) + '\n').encode()
        fd = os.open(refusal_path(store), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            if os.fstat(fd).st_size + len(line) <= MAX_REFUSAL_BYTES:
                os.write(fd, line)
        finally:
            os.close(fd)
    except OSError as failure:
        LOG.error('rotation warning row not written: %s', type(failure).__name__)


def gate(store, research, scan_ids, *, ledger_locked=None, review_source=None, full=False):
    """Replay new rejections and a bounded deterministic sample; deleting the table cannot erase a bound outcome.

    A receipt with a verified-digest row (T24R F7) gets the cheap binding check except for ``verified_index.SAMPLE`` of them
    per call; a receipt without one is always replayed in full. ``full=True`` replays everything."""
    progress = HistoryProgress.__new__(HistoryProgress); progress.store = store
    full = full or verified_index.full_replay_forced()
    with closing(store.connect()) as c:
        retired = rows(c)
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_observation_passes'").fetchone():
            if retired:
                raise ValueError('Cycle no-entry original pass table missing')
            return None
        terminal._passes(c)
        newest = c.execute('SELECT count(*),COALESCE(max(rowid),0) FROM paper_observation_passes').fetchone()
        index = verified_index.rows(c)
    # T24S: page loads only for passes that are new since the last call and a bounded sample (desk/pass_inventory.py).
    inventory = pass_inventory.completed(store, (KIND,), persist=review_source is None, full=full)
    bound = set()
    for identity, scan, intent, intent_key, outcome_key in inventory.of(KIND):
        if intent != intent_key or scan is None:
            raise ValueError('Cycle no-entry outcome original pass conflict')
        bound.add((identity, scan, intent_key, outcome_key))
    if bound != set(retired):
        raise ValueError('Cycle no-entry inventory incomplete')
    seed = digest({'passes': newest[0], 'last': newest[1]})
    replay, quick = verified_index.plan(index, INDEX_KIND, retired, seed=seed, full=full)
    for identity, scan, intent_key, outcome_key in quick:
        rec = terminal._load(store, outcome_key)
        if digest(rec) != outcome_key or (rec.get('pass_id'), rec.get('scan_id'), rec.get('intent_hash')) != (identity, scan, intent_key):
            raise ValueError('Cycle no-entry row/outcome conflict')
    for identity, scan, intent_key, outcome_key in replay:
        rec = terminal._load(store, outcome_key)
        if (rec.get('pass_id'), rec.get('scan_id'), rec.get('intent_hash')) != (identity, scan, intent_key):
            raise ValueError('Cycle no-entry row/outcome conflict')
        from .paper_cycle import _lock, canonical_job_path
        path = canonical_job_path(rec['ledger']['path'])
        with ExitStack() as locks:
            if ledger_locked != str(path) and not locks.enter_context(_lock(str(path)+'.paper-cycle.lock')):
                raise ValueError('Cycle no-entry ledger busy')
            original = _proof(store, progress, rec)
        if original['targets'][0]['target']['scan_id'] != scan:
            raise ValueError('Cycle no-entry scan conflict')
    return 'REJECTED_SCAN_RETIRED' if any(row[1] in scan_ids for row in retired) else None
