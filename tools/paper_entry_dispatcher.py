"""Explicit bounded discovery-to-paper orchestration; dry run unless --execute.

No runtime edits, budget creation/reset, retry/recovery, signing or broadcasting.
The dispatcher journal is a separate experiment-bound operator record. Existing
acquisition/intake/history/cycle functions retain all provider and evidence gates.
"""
import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desk import paper_cycle as cycle, paper_cycle_cli as cli
from desk import paper_monitor_operator as monitor
from desk import paper_terminal_reconciliation as terminal
from desk import runtime_compatibility as runtime
from desk import ownership_acquisition as acquisition, migration_slot_intake as migration
from desk import provider_pacing as pacing
from desk.job_persistence import BIRTH_ACQUISITION_V1, JobPersistence
from desk.history_progress import HistoryProgress
from desk.model import canonical, digest
from desk.decode import decode
from desk.evidence import EvidenceStore
from desk.programs import address
from tools import history_first_paper_entry as entry

PROVENANCE = 'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE'
MAX_DISPATCHES = 4096
MAX_PAYLOAD = 65536
MAX_JOURNAL = 32 * 1024 * 1024
SCHEMAS = {
    'context': 'CREATE TABLE context(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL,hash TEXT NOT NULL)',
    'intents': 'CREATE TABLE intents(id TEXT PRIMARY KEY,mint TEXT NOT NULL UNIQUE,signature TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,hash TEXT NOT NULL)',
    'results': 'CREATE TABLE results(id TEXT PRIMARY KEY,payload TEXT NOT NULL,hash TEXT NOT NULL)',
}


def _guards():
    guards = {}
    for table in SCHEMAS:
        for operation in ('UPDATE', 'DELETE'):
            name = f'no_{table}_{operation.lower()}'
            guards[name] = (f'CREATE TRIGGER {name} BEFORE {operation} ON {table} '
                            "BEGIN SELECT RAISE(ABORT,'Immutable dispatcher record'); END")
        name = f'no_{table}_replace'
        match = 'id=NEW.id OR rowid=NEW.rowid'
        if table == 'intents':
            match += ' OR mint=NEW.mint OR signature=NEW.signature'
        guards[name] = (f'CREATE TRIGGER {name} BEFORE INSERT ON {table} '
                       f'WHEN EXISTS(SELECT 1 FROM {table} WHERE {match}) '
                       "BEGIN SELECT RAISE(ABORT,'Immutable dispatcher identity'); END")
    return guards


def _path(value, *, existing=True, private=False):
    p = Path(value)
    if not p.is_absolute() or p.resolve() != p:
        raise ValueError('Canonical absolute path required')
    if existing:
        s = p.stat()
        if not p.is_file() or s.st_nlink != 1 or (private and s.st_mode & 0o077):
            raise ValueError('Private regular single-link path required')
    elif not p.parent.is_dir() or p.exists():
        raise ValueError('Fresh journal in existing directory required')
    if private and p.parent.stat().st_mode & 0o077:
        raise ValueError('Private parent directory required')
    return p


def _identity(p):
    s = p.stat()
    return {'path': str(p), 'device': s.st_dev, 'inode': s.st_ino}


def _now():
    n = time.time()
    if not math.isfinite(n) or not 0 <= n < 2**63:
        raise ValueError('Host clock invalid')
    return n


def _discovery(c):
    if c.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
        raise ValueError('Existing rollback discovery required')
    shape = c.execute("SELECT COUNT(*),COALESCE(MAX(length(CAST(filter AS BLOB))),0),COALESCE(SUM(typeof(filter)!='text' OR typeof(version)!='integer' OR typeof(high_water) NOT IN ('integer','real') OR typeof(last_reservation)!='integer'),0) FROM settings").fetchone()
    if shape[0]!=1 or not 32<=shape[1]<=44 or shape[2]:
        raise ValueError('Discovery settings scalar shape invalid')
    metadata = c.execute("SELECT COUNT(*),COALESCE(SUM(length(CAST(sql AS BLOB))),0),COALESCE(MAX(length(CAST(sql AS BLOB))),0),COALESCE(MAX(length(CAST(name AS BLOB))),0) FROM sqlite_master WHERE sql IS NOT NULL").fetchone()
    if metadata[0]>64 or metadata[1]>65536 or metadata[2]>16384 or metadata[3]>128:
        raise ValueError('Discovery schema bound invalid')
    guards = dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
    for table in ('reservations','completions','frames','raw_events','policies'):
        for action in ('UPDATE','DELETE'):
            name = f'{table}_{action.lower()}'
            sql = f"CREATE TRIGGER {name} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'Original discovery record is immutable'); END"
            if guards.get(name)!=sql:
                raise ValueError('Original discovery guard invalid')
    row = c.execute('SELECT version,filter,high_water,last_reservation FROM settings WHERE id=1').fetchone()
    if (not row or row[0] != 2 or type(row[2]) not in (int, float)
            or not math.isfinite(row[2]) or row[2] < 0
            or type(row[3]) is not int or row[3] < 0):
        raise ValueError('Discovery identity invalid')
    address(row[1])
    schema = list(c.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"))
    return {'version': 2, 'filter': row[1], 'schema_hash': digest(schema)}


def plan(*, config, research_db, evidence_db, ledger_db, discovery_db,
         pacing_db, journal, taker, amount_raw, pool_fee_bps):
    address(taker)
    if type(amount_raw) is not int or not 0 < amount_raw < 2**64:
        raise ValueError('Exact positive u64 entry size required')
    paths = {k: _path(v) for k, v in {
        'config': config, 'research_db': research_db, 'evidence_db': evidence_db,
        'ledger_db': ledger_db, 'discovery_db': discovery_db,
        'pacing_db': pacing_db}.items()}
    if len(set(paths.values()) | {Path(journal)}) != 7:
        raise ValueError('Distinct context paths required')
    cfg = cli._config(paths['config']); cycle._config(cfg)
    if cfg.get('paper_usd_valuation_version') != 1:
        raise ValueError('Explicit Kraken experiment required')
    # Reuse actual strict target size/taker/fee grammar without granting evidence.
    row = {'scan_id': 'grammar-only', 'mint': taker, 'pool': taker, 'taker': taker,
           'amount_raw': amount_raw, 'provenance': PROVENANCE,
           'pool_fee_bps': pool_fee_bps, 'graduation_refs': [], 'known_hazards': []}
    if pool_fee_bps is None:
        raise ValueError('Reviewed fee hypothesis required')
    with tempfile.TemporaryDirectory(prefix='dispatch-grammar-') as d:
        p = Path(d) / 'targets.json'
        p.write_text(canonical({'position_targets': [], 'candidates': [row], 'usd_evidence_refs': []}))
        cli.load_targets(p)
    if os.environ.get(pacing.ENV) != str(paths['pacing_db']):
        raise ValueError('Explicit shared pacing environment mismatch')
    p = pacing.configured(priority='investigation')
    if p is None or 'kraken' not in p.providers:
        raise ValueError('Reviewed Kraken pacing migration required')
    with closing(sqlite3.connect(paths['discovery_db'].as_uri() + '?mode=ro', uri=True)) as c:
        discovery = _discovery(c)
    return {'version': 1, 'journal': str(_path(journal, existing=Path(journal).exists(), private=True)),
            'paths': {k: _identity(v) for k, v in paths.items()},
            'config_hash': digest(cfg), 'source_hash': runtime.implementation_hash(),
            'tool_hash': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'entry_tool_hash': hashlib.sha256(Path(entry.__file__).read_bytes()).hexdigest(),
            'discovery': discovery, 'taker': taker, 'amount_raw': amount_raw,
            'pool_fee_bps': pool_fee_bps, 'minimum_age': 300, 'maximum_age': 7200}


def _parse(payload, h):
    value = json.loads(payload, object_pairs_hook=cli._object,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite')))
    if canonical(value) != payload or digest(value) != h:
        raise ValueError('Dispatcher original invalid')
    return value


def _read_journal(c):
    actual = dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    guards = dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
    if actual != SCHEMAS or guards != _guards() or c.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
        raise ValueError('Dispatcher schema/guards invalid')
    values = {}
    for table in SCHEMAS:
        count, size, bad = c.execute(
            f'SELECT COUNT(*),COALESCE(SUM(length(CAST(payload AS BLOB))),0),'
            f"COALESCE(SUM(typeof(payload)!='text' OR length(CAST(payload AS BLOB))>{MAX_PAYLOAD} "
            "OR typeof(hash)!='text' OR length(CAST(hash AS BLOB))!=64),0) "
            f'FROM {table}').fetchone()
        if count > (1 if table == 'context' else MAX_DISPATCHES) or size > 16*1024*1024 or bad:
            raise ValueError('Dispatcher input bound invalid')
        values[table] = {i: _parse(payload, h) for i, payload, h in c.execute(f'SELECT id,payload,hash FROM {table}')}
    return values


def _validate(c, expected):
    values = _read_journal(c)
    retired={}; bindings={}; historical_contexts={}
    if values['context'] != {1: expected}:
        if set(values['context'])!={1}:raise ValueError('Reviewed dispatcher context mismatch')
        from desk.paper_migration_no_entry import lineage as continuation
        edge=continuation(c,expected,values['context'][1])
        if edge is None:
            from desk.paper_dispatch_preparation_retirement import lineage
            retired=lineage(expected,values['context'][1])
        else:
            bindings,retired,historical_contexts=edge
    return _check_records(c,expected,values,bindings,retired,historical_contexts=historical_contexts)


def _check_records(c,expected,values,bindings,retired,*,historical_contexts=None):
    historical_contexts={} if historical_contexts is None else historical_contexts
    if set(values['results']) - set(values['intents']):
        raise ValueError('Orphan dispatch result')
    for i, mint, signature in c.execute('SELECT id,mint,signature FROM intents'):
        value = values['intents'][i]
        if (type(i) is not str or len(i)!=32 or any(x not in '0123456789abcdef' for x in i)
                or set(value) != {'version','context_hash','at','hint'} or value['version'] != 1
                or value['context_hash'] != bindings.get(i,retired.get(i,digest(expected))) or type(value['at']) not in (int,float) or not math.isfinite(value['at'])
                or value['at'] < 0 or value['hint']['mint'] != mint
                or value['hint']['signature'] != signature):
            raise ValueError('Dispatch intent binding invalid')
        hint = value['hint']
        if (set(hint) != {'seq','payload_hash','raw_hash','received_at','mint','pool','signature','slot'}
                or type(hint['seq']) is not int or hint['seq'] <= 0
                or type(hint['received_at']) not in (int,float) or not math.isfinite(hint['received_at'])
                or not 300 <= value['at']-hint['received_at'] <= 7200
                or not all(runtime._hash(hint[k]) for k in ('payload_hash','raw_hash'))):
            raise ValueError('Dispatch hint grammar invalid')
        migration._hints('dispatch-check', mint, hint['pool'], signature, hint['slot'], PROVENANCE)
        if i in values['results']:
            result = values['results'][i]
            if (set(result) != {'version','intent_hash','at','scan_id','result'}
                    or result['version'] != 1 or result['intent_hash'] != digest(value)
                    or type(result['at']) not in (int,float) or not math.isfinite(result['at'])
                    or result['at'] < value['at']):
                raise ValueError('Dispatch result binding invalid')
            original_result = result['result']
            if type(original_result) is dict and original_result.get('kind') == 'dispatcher_migration_no_entry_v1':
                from desk import paper_migration_no_entry
                store=EvidenceStore(expected['paths']['evidence_db']['path'],read_only=True)
                original=paper_migration_no_entry.verify(store,original_result)
                if (original['dispatch_id']!=i or original['scan_id']!=result['scan_id']
                        or original['intent_hash']!=digest(value) or original['hint']!=hint
                        or original['config_hash']!=expected['config_hash']
                        or any(original['context'][k]!=expected['paths'][k]['path'] for k in original['context'])):
                    raise ValueError('Migration disposition dispatcher binding')
                continue
            if type(original_result) is dict and original_result.get('kind') == 'history_preparation_no_entry_v1':
                from desk import history_preparation_rejection
                store = EvidenceStore(expected['paths']['evidence_db']['path'],read_only=True)
                from desk.history_progress import HistoryProgress
                progress = HistoryProgress.__new__(HistoryProgress); progress.store = store
                original = history_preparation_rejection.verify(store,progress,original_result)
                context = original['context']
                if (original_result['scan_id'] != result['scan_id']
                        or original['target']['target']['mint'] != mint
                        or original['config_hash'] != expected['config_hash']
                        or any(context[k] != expected['paths'][k]['path'] for k in
                               ('research_db','evidence_db','ledger_db','pacing_db'))):
                    raise ValueError('Preparation rejection dispatcher context conflict')
                continue
            if type(original_result) is dict and original_result.get('kind') == 'dispatcher_token_rejection_v1':
                record_context=historical_contexts.get(value['context_hash'],expected)
                if digest(record_context)!=value['context_hash']:raise ValueError('Unknown completed context')
                if _rejection_evidence(record_context, value, result['scan_id']) != original_result:
                    raise ValueError('Rejection disposition conflict')
                continue
            if type(original_result) is not dict or not runtime._hash(original_result.get('evidence_hash')):
                raise ValueError('Original cycle result required')
            store = EvidenceStore(expected['paths']['evidence_db']['path'],read_only=True)
            original = terminal._load(store,original_result['evidence_hash'])
            if original != {k:v for k,v in original_result.items() if k not in ('evidence_hash','terminal_receipt_hash')}:
                raise ValueError('Original cycle result conflict')
            original_intent = terminal._load(store,original['intent_hash'])
            if (original_intent.get('kind')!='paper_cycle_intent_v1'
                    or result['scan_id'] not in original_intent['admissions']
                    or original_intent['admissions'][result['scan_id']]['descriptor']['mint'] != mint
                    or original_intent['config_hash'] != expected['config_hash']
                    or original_intent['ledger'] != expected['paths']['ledger_db']['path']):
                raise ValueError('Original cycle intent conflict')
    if set(values['intents']) != set(values['results'])|set(retired) or set(retired)&set(values['results']):
        raise ValueError('Unresolved dispatch; no retry or automatic recovery')
    return values


@contextmanager
def _journal(path):
    p = _path(path, private=True)
    if p.stat().st_size > MAX_JOURNAL or any(Path(str(p)+suffix).exists() for suffix in ('-wal','-shm','-journal')):
        raise ValueError('Dispatcher storage invalid or interrupted')
    with cycle._lock(str(p)+'.dispatcher.lock') as acquired:
        if not acquired:
            raise ValueError('Dispatcher busy')
        with closing(sqlite3.connect(p.as_uri()+'?mode=rw', uri=True, isolation_level=None)) as c:
            c.execute('PRAGMA synchronous=FULL')
            yield c


def initialize(expected, *, approved_context_hash):
    if digest(expected) != approved_context_hash:
        raise ValueError('Explicit reviewed context hash required')
    p = _path(expected['journal'], existing=False, private=True)
    # No budget, experiment, admission, schema or pacer initialization.
    _preflight(expected)
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
    with closing(sqlite3.connect(p, isolation_level=None)) as c:
        c.execute('PRAGMA synchronous=FULL')
        c.execute('BEGIN IMMEDIATE')
        for sql in (*SCHEMAS.values(), *_guards().values()):
            c.execute(sql)
        c.execute('INSERT INTO context VALUES(1,?,?)', (canonical(expected), digest(expected)))
        c.commit()
    return {'status': 'ACTIVATED', 'context_hash': digest(expected)}


def _preflight(ctx, scan=None):
    paths = {k: v['path'] for k, v in ctx['paths'].items()}
    fresh = plan(**paths, journal=ctx['journal'], taker=ctx['taker'],
                 amount_raw=ctx['amount_raw'], pool_fee_bps=ctx['pool_fee_bps'])
    if fresh != ctx:
        raise ValueError('Reviewed context changed')
    cfg = cli._config(paths['config'])
    with monitor._context(paths['research_db'], paths['evidence_db'], paths['ledger_db'], cfg) as (store, ledger, state):
        if state['positions'] or state['mode'] != 'RUNNING':
            raise ValueError('Held-position priority or paused ledger')
        blocked = terminal.gate(store, Path(paths['research_db']), (() if scan is None else (scan,)), ledger_locked=str(ledger))
        if blocked:
            raise ValueError('Observation recovery or retired scan')
        snapshot = monitor.MonitoringBudget(store, ledger, cfg).snapshot()
        if snapshot['status'] != 'AVAILABLE' or snapshot.get('blockers'):
            raise ValueError('Monitoring pending or blocked')
        pacer = pacing.configured(priority='investigation')
        if pacer is None or str(pacer.path) != paths['pacing_db']:
            raise ValueError('Pacer context mismatch')
        with closing(pacer._connect()) as c:
            if (c.execute('SELECT 1 FROM state WHERE pending IS NOT NULL LIMIT 1').fetchone()
                    or c.execute('SELECT 1 FROM waiters LIMIT 1').fetchone()):
                raise ValueError('Provider pacing pending')
        for key, identity in ctx['paths'].items():
            if _identity(Path(paths[key])) != identity:
                raise ValueError('Context file identity changed')
        if digest(cfg) != ctx['config_hash'] or runtime.implementation_hash() != ctx['source_hash']:
            raise ValueError('Source/config context changed')


@contextmanager
def _held_guard(ctx, scan=None):
    """Caller already owns research/evidence locks; ledger is acquired last.

    Used by acquisition's budget-reserved RPC and intake's existing before-I/O
    callback. No new provider function, budget charge or credential lookup.
    """
    ledger = Path(ctx['paths']['ledger_db']['path'])
    with cycle._lock(str(ledger)+'.paper-cycle.lock') as acquired:
        if not acquired:
            raise ValueError('Held cycle or ledger busy')
        cfg = cli._config(ctx['paths']['config']['path'])
        if digest(cfg)!=ctx['config_hash'] or runtime.implementation_hash()!=ctx['source_hash']:
            raise ValueError('Source/config changed before I/O')
        state = cycle._state(ledger,cfg)
        if state['positions'] or state['mode']!='RUNNING':
            raise ValueError('Held-position priority before I/O')
        store = EvidenceStore(ctx['paths']['evidence_db']['path'],read_only=True)
        blocked = terminal.gate(store,Path(ctx['paths']['research_db']['path']),
                                (() if scan is None else (scan,)),ledger_locked=str(ledger))
        if blocked:
            raise ValueError('Observation recovery or retired scan before I/O')
        yield


def _intake_guard(ctx, scan):
    with _held_guard(ctx, scan):
        pass



def _bounded_text(c, table, column, key, identity, limit=MAX_PAYLOAD):
    # Scalar-only preflight and content fetch share the same read transaction.
    row = c.execute(f'SELECT typeof({column}),length(CAST({column} AS BLOB)) FROM {table} WHERE {key}=?', (identity,)).fetchall()
    if len(row) != 1 or row[0][0] != 'text' or not 0 < row[0][1] <= limit:
        raise ValueError('Rejection proof text bound invalid')
    text = c.execute(f'SELECT {column} FROM {table} WHERE {key}=?', (identity,)).fetchone()[0]
    value = json.loads(text,object_pairs_hook=cli._object,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite')))
    if canonical(value) != text:
        raise ValueError('Rejection original not canonical')
    return value


def _rejection(ctx, intent, scan, publish=None):
    """Replay a one-read deterministic rejection; never recover interrupted jobs.

    Publication stays inside canonical locks. Restart repeats the same proof;
    the status string alone never retires an unresolved dispatcher intent.
    """
    _preflight(ctx,scan)
    paths = {k:v['path'] for k,v in ctx['paths'].items()}
    cfg = cli._config(paths['config'])
    with monitor._context(paths['research_db'],paths['evidence_db'],paths['ledger_db'],cfg) as (store,ledger,state):
        if state['positions'] or state['mode'] != 'RUNNING':
            raise ValueError('Held-position priority or paused ledger')
        if (digest(cfg) != ctx['config_hash'] or runtime.implementation_hash() != ctx['source_hash']
                or terminal.gate(store,Path(paths['research_db']),(scan,),ledger_locked=str(ledger))):
            raise ValueError('Rejection context or recovery conflict')
        snapshot = monitor.MonitoringBudget(store,ledger,cfg).snapshot()
        if snapshot['status'] != 'AVAILABLE' or snapshot.get('blockers'):
            raise ValueError('Monitoring pending or blocked')
        pacer = pacing.configured(priority='investigation')
        if pacer is None or str(pacer.path) != paths['pacing_db']:
            raise ValueError('Pacer context mismatch')
        with closing(pacer._connect()) as pc:
            if (pc.execute('SELECT 1 FROM state WHERE pending IS NOT NULL LIMIT 1').fetchone()
                    or pc.execute('SELECT 1 FROM waiters LIMIT 1').fetchone()):
                raise ValueError('Provider pacing pending')
        for key,identity in ctx['paths'].items():
            if _identity(Path(paths[key])) != identity:
                raise ValueError('Rejection context identity changed')
        return _rejection_evidence(ctx,intent,scan,publish)


def _rejection_evidence(ctx,intent,scan,publish=None):
    """Original completed evidence replay, not a fresh operation/preflight.

    A saved old rejection retains its old context/source after an explicitly
    reviewed continuation. Publication still enters through _rejection and all
    current operational gates; this function alone never creates a result.
    """
    paths={k:v['path'] for k,v in ctx['paths'].items()}
    cfg=cli._config(paths['config'])
    if digest(cfg)!=ctx['config_hash']:raise ValueError('Rejection config changed')
    for key,identity in ctx['paths'].items():
        if _identity(Path(paths[key]))!=identity:raise ValueError('Rejection identity changed')
    store=EvidenceStore(paths['evidence_db'],read_only=True)
    # Both original databases are read in fixed snapshots under their locks.
    with closing(sqlite3.connect(Path(paths['research_db']).as_uri()+'?mode=ro',uri=True)) as rc, closing(sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True)) as ec:
        rc.execute('BEGIN'); ec.execute('BEGIN')
        descriptor = _bounded_text(rc,'scan_jobs','descriptor','scan_id',scan)
        report = _bounded_text(rc,'scans','result','id',scan)
        admission_descriptor = _bounded_text(ec,'ownership_admissions','descriptor','id',scan)
        prepared = _bounded_text(ec,'ownership_admissions','prepared_source','id',scan)
        shape = ec.execute("SELECT typeof(a.state),length(a.state),typeof(a.prepared_used),typeof(a.completed_source_hash),length(a.completed_source_hash),typeof(b.source_hash),length(b.source_hash),typeof(b.used),typeof(b.ceiling) FROM ownership_admissions a JOIN ownership_budgets b ON b.id=a.id WHERE a.id=?",(scan,)).fetchall()
        if shape != [('text',6,'integer','text',64,'text',64,'integer','integer')]:
            raise ValueError('Sealed admission scalar shape invalid')
        shape = rc.execute("SELECT typeof(s.id),length(s.id),typeof(s.mint),length(s.mint),typeof(s.created),typeof(s.status),length(s.status),typeof(j.kind),length(j.kind),typeof(j.descriptor_version),typeof(j.descriptor_hash),length(j.descriptor_hash) FROM scans s JOIN scan_jobs j ON j.scan_id=s.id WHERE s.id=?",(scan,)).fetchall()
        if (len(shape) != 1 or shape[0][:3] != ('text',32,'text') or not 32 <= shape[0][3] <= 44
                or shape[0][4:] != ('integer','text',8,'text',len(BIRTH_ACQUISITION_V1),'integer','text',64)):
            raise ValueError('Completed job scalar shape invalid')
        admission = HistoryProgress.inspect_admission(ec,scan)
        row = rc.execute('SELECT id,mint,created,status FROM scans WHERE id=?',(scan,)).fetchone()
        if not row or row[1] != intent['hint']['mint'] or row[3] != 'COMPLETE':
            raise ValueError('Completed matching rejection job required')
        jobs = JobPersistence.__new__(JobPersistence); jobs.path=Path(paths['research_db'])
        expected_descriptor = jobs._descriptor(scan,row[1],row[2],BIRTH_ACQUISITION_V1,
            store.path,cfg.get('paper_token_profile_version',0))
        job = rc.execute('SELECT kind,descriptor_version,descriptor_hash FROM scan_jobs WHERE scan_id=?',(scan,)).fetchone()
        if canonical(descriptor) != canonical(expected_descriptor) or job != (BIRTH_ACQUISITION_V1,descriptor['schema_version'],digest(descriptor)):
            raise ValueError('Exact acquisition descriptor required')
        source = dict(zip(('id','mint','created','status'),row)) | {'result':canonical(report)}
        if (admission is None or admission['state'] != 'SEALED'
                or type(admission['requests_used']) is not int or admission['requests_used'] != 1
                or type(admission['prepared_requests_used']) is not int or admission['prepared_requests_used'] != 1
                or type(admission['request_ceiling']) is not int or admission['request_ceiling'] != 18
                or admission_descriptor != admission['descriptor']
                or admission_descriptor['mint'] != row[1] or admission_descriptor['created'] != row[2]
                or prepared != source or admission['prepared_source'] != source
                or admission['completed_source_hash'] != digest(source)):
            raise ValueError('Exact sealed one-charge rejection required')
        setup_row = ec.execute('SELECT typeof(job_descriptor_hash),length(job_descriptor_hash),typeof(mint_hash),length(mint_hash),typeof(cutoff_hash) FROM ownership_acquisition_setup WHERE scan_id=?',(scan,)).fetchall()
        if setup_row != [('text',64,'text',64,'null')]:
            raise ValueError('Exact mint-only setup required')
        job_hash,mint_hash,cutoff = ec.execute('SELECT job_descriptor_hash,mint_hash,cutoff_hash FROM ownership_acquisition_setup WHERE scan_id=?',(scan,)).fetchone()
        if job_hash != digest(descriptor) or not runtime._hash(mint_hash):
            raise ValueError('Setup binding invalid')
        retained = terminal._load(store,mint_hash)
        if (type(retained) is not dict or retained.get('method') != 'getAccountInfo'
                or retained.get('params') != [row[1],{'encoding':'base64','commitment':'confirmed'}]):
            raise ValueError('Exact retained mint request required')
        setup = {'mint_hash':mint_hash,'cutoff_hash':None,'mint':retained['result']}
        policy = acquisition._policy(setup,mint=row[1],token_profile_version=cfg.get('paper_token_profile_version',0))
        reasons = policy.get('reasons')
        if (policy.get('decision') != 'SKIP' or type(reasons) is not list or not reasons
                or len(reasons) != len(set(reasons))
                or not set(reasons) <= {'ACTIVE_MINT_AUTHORITY','ACTIVE_FREEZE_AUTHORITY'}):
            raise ValueError('Not an allowlisted deterministic token rejection')
        # Compare the entire original report/source, not selected summary flags.
        if canonical(acquisition._source(descriptor,setup,admission,None,'UNSUPPORTED_TOKEN')) != canonical(source):
            raise ValueError('Replayed rejection source/report conflict')
        proof = {'kind':'dispatcher_token_rejection_v1','intent_hash':digest(intent),
            'context_hash':digest(ctx),'source_hash':ctx['source_hash'],'config_hash':ctx['config_hash'],
            'scan_id':scan,'mint':row[1],'signature':intent['hint']['signature'],
            'job_descriptor_hash':digest(descriptor),'admission_hash':digest(admission),
            'completed_source_hash':digest(source),'report_hash':report['report_hash'],
            'mint_response_hash':mint_hash,'requests_before':0,'requests_after':1,
            'request_ceiling':18,'token_policy':policy,'entry_authorized':False,
            'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False}
        if publish is not None:
            publish(proof)
        return proof

def _migration_event_hint(raw, decoded, parent):
    """Original websocket event syntax only, including NULL blockTime.

    No timestamp substitution, finality/authentication or graduation verdict.
    The finalized migration intake remains mandatory after acquisition.
    """
    path = parent['instruction']
    if parent.get('status') != 'IDENTIFIED' or not path.isdecimal() or '.' in path:
        return False
    container = raw['params']['result']['transaction'] if raw.get('method') == 'transactionNotification' else raw
    groups = [g for g in (container['meta'].get('innerInstructions') or [])
              if type(g.get('index')) is int and str(g['index']) == path]
    if len(groups) != 1:
        return False
    events = []
    for event in decoded['program_observations']:
        if (event.get('name') != 'CompletePumpAmmMigrationEvent'
                or event.get('status') != 'EVENT_DECODED' or event.get('schema_complete') is not True
                or event.get('program') != parent['program']):
            continue
        parts = event['instruction'].split('.')
        if len(parts) != 2 or parts[0] != path or not parts[1].isdecimal():
            continue
        i = int(parts[1]); instructions = groups[0]['instructions']
        if i >= len(instructions) or type(instructions[i].get('stackHeight')) is not int or instructions[i]['stackHeight'] != 2:
            continue
        fields = event.get('fields',{})
        if fields.get('mint') != parent.get('mint') or fields.get('pool') != parent.get('pool'):
            continue
        events.append(event)
    return len(events) == 1

def _select(ctx, c, now):
    discovery = Path(ctx['paths']['discovery_db']['path'])
    research = Path(ctx['paths']['research_db']['path'])
    with closing(sqlite3.connect(discovery.as_uri()+'?mode=ro', uri=True)) as d, closing(sqlite3.connect(research.as_uri()+'?mode=ro', uri=True)) as r:
        d.execute('BEGIN')
        if _discovery(d) != ctx['discovery']:
            raise ValueError('Discovery identity changed')
        rows = d.execute('SELECT seq,source_id,received_at,slot,payload_hash,typeof(payload),length(CAST(payload AS BLOB)) FROM raw_events ORDER BY seq DESC LIMIT 500').fetchall()
        for seq, source, received, slot, h, kind, length in rows:
            if (type(received) not in (int,float) or not math.isfinite(received) or received < 0
                    or type(seq) is not int or seq <= 0 or type(slot) is not int or not 0 <= slot < 2**63
                    or kind != 'text' or not 0 < length <= 200000):
                raise ValueError('Malformed discovery row')
            if not 300 <= now-received <= 7200:
                continue
            payload = d.execute('SELECT payload FROM raw_events WHERE seq=?', (seq,)).fetchone()[0]
            raw = json.loads(payload, object_pairs_hook=cli._object,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite')))
            raw_hash = hashlib.sha256(payload.encode()).hexdigest()
            receipt = d.execute('SELECT r.kind,o.at,o.bytes,o.records,o.code,typeof(f.payload),length(f.payload),f.sha256 FROM frames f JOIN completions o ON o.id=f.id JOIN reservations r ON r.id=f.id WHERE f.sha256=? AND o.frame_hash=? AND o.at=?', (raw_hash, raw_hash, received)).fetchall()
            if (digest(raw) != h or len(receipt) != 1
                    or receipt[0] != ('RECEIVE',received,len(payload.encode()),1,'RECEIVED','blob',len(payload.encode()),raw_hash)):
                raise ValueError('Original discovery receipt invalid')
            frame = d.execute('SELECT payload FROM frames WHERE sha256=?', (raw_hash,)).fetchone()[0]
            if frame != payload.encode():
                raise ValueError('Original discovery bytes conflict')
            # Integrity above is mandatory even for unsupported notification shapes.
            envelope = raw.get('params',{}).get('result') if type(raw) is dict and type(raw.get('params')) is dict else None
            if type(envelope) is not dict:
                continue
            if envelope.get('signature') != source.removeprefix('confirmed:') or envelope.get('slot') != slot:
                raise ValueError('Discovery metadata conflict')
            try:
                decoded = decode(raw)
            except (ValueError,KeyError,TypeError,IndexError,AttributeError,OverflowError):
                continue  # Local undecodable hint, never an integrity exemption.
            if decoded['signature'] != source.removeprefix('confirmed:') or decoded['slot'] != slot:
                raise ValueError('Discovery metadata conflict')
            if decoded['status'] != 'OBSERVED':
                continue
            hints = []
            for o in decoded['program_observations']:
                if o.get('name') not in ('migrate','migrate_v2') or o.get('status') != 'IDENTIFIED':
                    continue
                mint, pool = o.get('mint'), o.get('pool')
                try:
                    migration._hints('selection-only',mint,pool,decoded['signature'],slot,PROVENANCE)
                except migration.IntakeBlocked:
                    continue  # Unsupported hint/pool, not a global receipt failure.
                if not _migration_event_hint(raw,decoded,o):
                    continue  # No-op calls are not migration selection witnesses.
                hints.append({'seq':seq,'payload_hash':h,'raw_hash':raw_hash,'received_at':received,
                              'mint':mint,'pool':pool,'signature':decoded['signature'],'slot':slot})
            if len(hints) != 1:
                continue  # Ambiguous notification is never candidate selection.
            hint = hints[0]
            if (c.execute('SELECT 1 FROM intents WHERE mint=? OR signature=?', (hint['mint'],hint['signature'])).fetchone()
                    or r.execute('SELECT 1 FROM scans WHERE mint=? LIMIT 1', (hint['mint'],)).fetchone()):
                continue
            return hint
    return None


def _write(c, table, identity, value, hint=None):
    c.execute('BEGIN IMMEDIATE')
    try:
        if table == 'intents':
            c.execute('INSERT INTO intents VALUES(?,?,?,?,?)', (identity,hint['mint'],hint['signature'],canonical(value),digest(value)))
        else:
            c.execute('INSERT INTO results VALUES(?,?,?)', (identity,canonical(value),digest(value)))
        c.commit()
    except BaseException:
        c.rollback(); raise


def dispatch(expected, *, execute=False, systemd_credentials=False):
    paths = {k: v['path'] for k,v in expected['paths'].items()}
    with _journal(expected['journal']) as journal:
        _validate(journal, expected)
        _preflight(expected)
        now = _now()
        last = journal.execute('SELECT payload FROM intents ORDER BY rowid DESC LIMIT 1').fetchone()
        if last and now < json.loads(last[0])['at']:
            raise ValueError('Dispatcher clock rollback')
        hint = _select(expected, journal, now)
        if hint is None:
            return {'status':'NO_CANDIDATE','attempted_requests':0}
        if not execute:
            return {'status':'DRY_RUN','hint':hint,'attempted_requests':0,'entry_authorized':False}
        if not systemd_credentials:
            raise ValueError('Explicit managed credentials required')
        if journal.execute('SELECT COUNT(*) FROM intents').fetchone()[0] >= MAX_DISPATCHES:
            raise ValueError('Dispatcher journal capacity exhausted; no reset')
        identity = uuid.uuid4().hex
        intent = {'version':1,'context_hash':digest(expected),'at':now,'hint':hint}
        # A competing held/worker invocation is a zero-intent refusal. Complete
        # the final lock reacquisition before publishing the irreversible latch.
        _preflight(expected)
        from desk.providers import helius_rpc
        cfg = cli._config(paths['config'])
        with monitor._context(paths['research_db'],paths['evidence_db'],paths['ledger_db'],cfg) as (store, ledger, state):
            if state['positions'] or state['mode'] != 'RUNNING':
                raise ValueError('Held-position priority or paused ledger')
            cli._credentials()
            jobs = JobPersistence.__new__(JobPersistence); jobs.path = Path(paths['research_db'])
            with closing(jobs.connect()) as c:
                if c.execute('SELECT 1 FROM scans WHERE mint=? LIMIT 1',(hint['mint'],)).fetchone():
                    raise ValueError('Already admitted candidate')
            if terminal.gate(store,jobs.path,(),ledger_locked=str(ledger)):
                raise ValueError('Observation recovery required')
            _write(journal,'intents',identity,intent,hint)
            # From this point any interruption stays unresolved; never retry or
            # erase an intent merely because acquisition has not yet spent.
            scan = jobs.admit(hint['mint'],kind=BIRTH_ACQUISITION_V1,evidence_db=store.path,
                              paper_token_profile_version=cfg.get('paper_token_profile_version',0))
        def guarded_rpc(method,params):
            with _held_guard(expected,scan):
                return helius_rpc(method,params)
        acquired = acquisition.acquire(paths['research_db'],paths['evidence_db'],guarded_rpc,
                                      scan_id=scan,paper_token_profile_version=cfg.get('paper_token_profile_version',0))
        scan = acquired.get('scan_id')
        if scan and acquired.get('status') == 'UNSUPPORTED_TOKEN':
            def publish_rejection(proof):
                outcome = {'version':1,'intent_hash':digest(intent),'at':_now(),'scan_id':scan,'result':proof}
                _write(journal,'results',identity,outcome)
            _rejection(expected,intent,scan,publish_rejection)
            return {'status':'TOKEN_REJECTED','dispatch_id':identity,'scan_id':scan,
                    'entry_authorized':False,'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False}
        if not scan or acquired.get('status') not in ('SEED_HISTORY_PARTIAL','SEED_HISTORY_ACQUIRED','LAUNCH_OR_INVENTORY_UNVERIFIED'):
            raise ValueError('Acquisition incomplete; dispatcher recovery required')
        _preflight(expected,scan)
        retained = migration.intake(paths['research_db'],paths['evidence_db'],scan_id=scan,
            mint=hint['mint'],pool=hint['pool'],signature=hint['signature'],slot=hint['slot'],
            provenance=PROVENANCE,credentials_loader=lambda:_intake_guard(expected,scan),paper_token_profile_version=cli._config(paths['config']).get('paper_token_profile_version',0))
        if retained['status'] != 'RETAINED_MIGRATION_WITNESS':
            from desk import paper_migration_no_entry
            # Only a narrow replay-proved unsupported quote can publish. Every
            # missing/uncaptured/unknown/generic mismatch still raises unresolved.
            disposition=paper_migration_no_entry.publish(journal,expected,identity,scan)
            outcome={'version':1,'intent_hash':digest(intent),'at':_now(),'scan_id':scan,'result':disposition}
            if len(canonical(outcome).encode())>MAX_PAYLOAD:raise ValueError('Migration outcome exceeds bound')
            _write(journal,'results',identity,outcome)
            return {'status':'DISPATCHED','dispatch_id':identity,'scan_id':scan,'paper_status':'NO_ENTRY',
                    'entry_authorized':False,'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False}
        _preflight(expected,scan)
        if not 300 <= _now()-hint['received_at'] <= 7200:
            raise ValueError('Candidate expired during preparation')
        row = {'scan_id':scan,'mint':hint['mint'],'pool':hint['pool'],'taker':expected['taker'],
               'amount_raw':expected['amount_raw'],'provenance':PROVENANCE,
               'pool_fee_bps':expected['pool_fee_bps'],'known_hazards':[],
               'graduation_refs':retained['request_evidence_refs']}
        rejection_published = False
        def publish_no_entry(result):
            nonlocal rejection_published
            from desk import history_preparation_rejection
            from desk.history_progress import HistoryProgress
            store=EvidenceStore(paths['evidence_db'],read_only=True)
            progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
            original=history_preparation_rejection.verify(store,progress,result)
            if result['scan_id']!=scan or original['config_hash']!=expected['config_hash']:
                raise ValueError('Preparation result publication binding')
            outcome={'version':1,'intent_hash':digest(intent),'at':_now(),'scan_id':scan,'result':result}
            if len(canonical(outcome).encode())>MAX_PAYLOAD:raise ValueError('Dispatch outcome exceeds bound')
            _write(journal,'results',identity,outcome)
            rejection_published = True
        with tempfile.TemporaryDirectory(prefix='dispatch-target-') as d:
            target = Path(d)/'targets.json'
            target.write_text(canonical({'position_targets':[],'candidates':[row],'usd_evidence_refs':[]}))
            result = entry.execute(paths['config'],paths['research_db'],paths['evidence_db'],paths['ledger_db'],target,
                                   live=True,systemd_credentials=True,no_entry_publish=publish_no_entry)
        if rejection_published:
            return {'status':'DISPATCHED','dispatch_id':identity,'scan_id':scan,'paper_status':'NO_ENTRY',
                    'entry_authorized':False,'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False}
        outcome = {'version':1,'intent_hash':digest(intent),'at':_now(),'scan_id':scan,'result':result}
        if len(canonical(outcome).encode()) > MAX_PAYLOAD:
            raise ValueError('Dispatch outcome exceeds bound; original evidence retained')
        _write(journal,'results',identity,outcome)
        return {'status':'DISPATCHED','dispatch_id':identity,'scan_id':scan,
                'paper_status':result['status'],'entry_authorized':False,
                'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    for name in ('config','research-db','evidence-db','ledger-db','discovery-db','pacing-db','journal','taker','pool-fee-bps'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--amount-raw',type=int,required=True)
    p.add_argument('--plan',action='store_true')
    p.add_argument('--initialize',action='store_true')
    p.add_argument('--approved-context-hash')
    p.add_argument('--execute',action='store_true')
    p.add_argument('--systemd-credentials',action='store_true')
    a = p.parse_args(argv)
    try:
        if sum((a.plan,a.initialize,a.execute)) > 1:
            raise ValueError('Choose one explicit operation')
        expected = plan(**{k:getattr(a,k) for k in ('config','research_db','evidence_db','ledger_db','discovery_db','pacing_db','journal','taker','amount_raw','pool_fee_bps')})
        if a.plan:
            _preflight(expected)
            result = {'status':'PLAN','context':expected,'context_hash':digest(expected),'entry_authorized':False}
        elif a.initialize:
            result = initialize(expected,approved_context_hash=a.approved_context_hash)
        else:
            result = dispatch(expected,execute=a.execute,systemd_credentials=a.systemd_credentials)
        print(canonical(result)); return 0
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError,OverflowError,RecursionError):
        print(canonical({'status':'BLOCKED','blockers':['DISPATCH_CONTEXT_OR_EVIDENCE_UNAVAILABLE'],
                         'entry_authorized':False,'live_readiness':False})); return 2


if __name__ == '__main__':
    raise SystemExit(main())
