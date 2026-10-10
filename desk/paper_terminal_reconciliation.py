"""One narrow terminal candidate-rejection certificate, never a recovery waiver.

Original NULL passes and transport pages remain unchanged. Legacy association is
explicit reviewed operator evidence; RPC observations are not authenticated state.
All callers hold research -> evidence -> ledger locks. No requests or retries.
"""
import base64
import json
import zlib
from contextvars import ContextVar
from contextlib import closing, ExitStack
from pathlib import Path
import sqlite3

from .model import canonical, digest
from . import runtime_compatibility as runtime
from .paper_checkpoint import read_checkpoint
from .history_progress import HistoryProgress, canonical_ownership_path
from .job_persistence import canonical_job_path
from .token2022_paper import selected
from .pools import verify_pool
from .live_observation import ProviderObservation, ingest_mint, ingest_pool, ObservationError, PolicyRejection, MINT_POLICY_REJECTIONS, POOL_POLICY_REJECTIONS
from . import provider_pacing

POLICY = Path(__file__).resolve().parents[1]/'config/paper-terminal-reconciliation.json'
TABLE = 'paper_terminal_reconciliations'
MAX_RECEIPTS = 256
MAX_BYTES = 65536
MAX_PROOF_RAW_BYTES = 16*1024*1024  # original EvidenceStore page ceiling
MAX_PROOF_COMPRESSED_BYTES = MAX_PROOF_RAW_BYTES+65536  # bounded zlib overhead
HAZARDS = MINT_POLICY_REJECTIONS | POOL_POLICY_REJECTIONS
ANCHORS = {'metadata_hash','checkpoint_hash','events_hash','outcomes_hash'}
FIELDS = {'version','association','pass_id','intent_hash','outcome_hash','attempt_refs',
    'invocation_hash','source_hash','context','config_hash','scan_id','before','after',
    'ledger_anchors','hazards'}


def _id(value):
    return type(value) is str and len(value)==32 and all(x in '0123456789abcdef' for x in value)


def _schema():
    return f'CREATE TABLE {TABLE}(pass_id TEXT PRIMARY KEY,scan_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,payload_hash TEXT NOT NULL)'


def _guards():
    result={TABLE+'_insert':f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN (SELECT COUNT(*) FROM {TABLE})>={MAX_RECEIPTS} OR EXISTS(SELECT 1 FROM {TABLE} WHERE pass_id=NEW.pass_id OR scan_id=NEW.scan_id) BEGIN SELECT RAISE(ABORT,'Terminal reconciliation capacity'); END"}
    return result|{TABLE+'_'+a.lower():f"CREATE TRIGGER {TABLE}_{a.lower()} BEFORE {a} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Terminal reconciliation immutable'); END" for a in ('UPDATE','DELETE')}


def _shape(v):
    return (type(v) is dict and set(v)==FIELDS and type(v['version']) is int and v['version']==1
        and v['association'] in ('EXPLICIT_REVIEWED_LEGACY','INTRINSIC') and _id(v['pass_id'])
        and type(v['scan_id']) is str and 1<=len(v['scan_id'])<=256
        and all(runtime._hash(v[k]) for k in ('intent_hash','outcome_hash','source_hash','config_hash'))
        and (runtime._hash(v['invocation_hash']) and len(v['attempt_refs'])==3 if v['association']=='EXPLICIT_REVIEWED_LEGACY' else v['invocation_hash'] is None)
        and type(v['attempt_refs']) is list and len(v['attempt_refs']) in (1,3)
        and len(set(v['attempt_refs']))==len(v['attempt_refs']) and all(runtime._hash(x) for x in v['attempt_refs'])
        and type(v['before']) is int and type(v['after']) is int and 0<=v['before']<v['after']<=18 and v['after']==v['before']+len(v['attempt_refs'])
        and type(v['context']) is dict and set(v['context'])=={'research_db','evidence_db','ledger_db','pacing_db'}
        and runtime._context_shape({k:v['context'][k] for k in ('research_db','evidence_db','ledger_db')})
        and (v['context']['pacing_db'] is None or type(v['context']['pacing_db']) is str and str(Path(v['context']['pacing_db']).resolve())==v['context']['pacing_db'])
        and type(v['ledger_anchors']) is dict and set(v['ledger_anchors'])==ANCHORS
        and all(runtime._hash(x) for x in v['ledger_anchors'].values())
        and type(v['hazards']) is list and bool(v['hazards']) and len(v['hazards'])<=len(HAZARDS)
        and len(set(v['hazards']))==len(v['hazards']) and set(v['hazards'])<=HAZARDS)


def _approved(v):
    with POLICY.open('rb') as stream:raw=stream.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES:raise ValueError('Terminal policy byte bound')
    p=runtime._parse(raw.decode())
    if (type(p) is not dict or set(p)!={'version','associations'} or type(p['version']) is not int or p['version']!=1
            or type(p['associations']) is not list or len(p['associations'])>32):raise ValueError('Terminal policy malformed')
    ids=set();matched=False
    for pin in p['associations']:
        if not _shape(pin) or pin['association']!='EXPLICIT_REVIEWED_LEGACY':raise ValueError('Terminal pin malformed')
        identity=(pin['context']['evidence_db'],pin['pass_id'])
        if identity in ids:raise ValueError('Terminal pin duplicate/conflicting')
        ids.add(identity)
        if pin==v:matched=True
    if not matched:raise ValueError('Legacy association not explicitly reviewed')


def _rows(c):
    objects=list(c.execute("SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'paper_terminal_%'"))
    if not objects:return []
    expected={('table',TABLE):_schema()}|{('trigger',k):v for k,v in _guards().items()}
    if dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(TABLE,)))!=_guards():raise ValueError('Terminal receipt unexpected guards')
    # UNIQUE scan index is SQLite-owned, never an extra user guard.
    if {(a,b):s for a,b,s in objects}!=expected:raise ValueError('Terminal receipt schema/guards malformed')
    n=c.execute(f'SELECT COUNT(*) FROM {TABLE}').fetchone()[0]
    if not 1<=n<=MAX_RECEIPTS:raise ValueError('Terminal receipt count bound/partial publication')
    shapes=c.execute(f'SELECT typeof(pass_id),typeof(scan_id),typeof(payload),typeof(payload_hash),length(CAST(pass_id AS BLOB)),length(CAST(scan_id AS BLOB)),length(CAST(payload AS BLOB)),length(CAST(payload_hash AS BLOB)) FROM {TABLE}').fetchall()
    if any(r[:4]!=('text',)*4 or r[4]!=32 or not 1<=r[5]<=256 or not 0<r[6]<=MAX_BYTES or r[7]!=64 for r in shapes):
        raise ValueError('Terminal receipt scalar bound')
    rows=[]
    for identity,scan,payload,key in c.execute(f'SELECT pass_id,scan_id,payload,payload_hash FROM {TABLE}'):
        v=runtime._parse(payload)
        if not _shape(v) or v['pass_id']!=identity or v['scan_id']!=scan or digest(v)!=key:raise ValueError('Terminal receipt identity/hash malformed')
        if v['association']=='EXPLICIT_REVIEWED_LEGACY':_approved(v)
        rows.append(v)
    return rows


def _passes(c):
    if c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='paper_observation_passes'").fetchone()!=('CREATE TABLE paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)',):
        raise ValueError('Original pass schema malformed')
    if c.execute('SELECT COUNT(*) FROM paper_observation_passes').fetchone()[0]>10000:
        raise ValueError('Original pass count bound')
    if c.execute("SELECT 1 FROM paper_observation_passes WHERE typeof(id)!='text' OR length(CAST(id AS BLOB))!=32 OR typeof(intent_hash)!='text' OR length(CAST(intent_hash AS BLOB))!=64 OR (outcome_hash IS NOT NULL AND (typeof(outcome_hash)!='text' OR length(CAST(outcome_hash AS BLOB))!=64)) LIMIT 1").fetchone():
        raise ValueError('Original pass scalar malformed')


def _context(research,evidence,ledger,pacing):
    paths=[canonical_job_path(research),canonical_ownership_path(evidence),canonical_job_path(ledger)]
    if len(set(paths))!=3 or not all(p.is_file() for p in paths):raise ValueError('Existing distinct context required')
    if pacing is not None:
        pacing,_=provider_pacing._path(pacing)
        if pacing in paths:raise ValueError('Distinct pacing required')
    return dict(zip(('research_db','evidence_db','ledger_db'),map(str,paths)))|{'pacing_db':None if pacing is None else str(pacing)}


def _pacing(path):
    if path is None:return
    pacer=provider_pacing.Pacer(path)
    with closing(pacer._connect()) as c:
        c.execute('BEGIN')
        if c.execute('SELECT 1 FROM state WHERE pending IS NOT NULL LIMIT 1').fetchone():raise ValueError('Provider outcome pending')
        if c.execute('SELECT 1 FROM waiters LIMIT 1').fetchone():raise ValueError('Provider waiter unresolved')


def _ledger(c,cfg,*,initial,historical_source=None):
    from .paper_cycle import INIT,MARKER
    from .engine import initial_state
    # Bounded shape before read_checkpoint fetches its original fields.
    from .runtime_extensions import _metadata
    saved=_metadata(c)
    metadata=runtime._parse(saved['config']) if cfg is None else cfg
    if saved.get('config')!=canonical(metadata) or saved.get('config_hash')!=digest(metadata) or saved.get('paper_cycle')!=MARKER:
        raise ValueError('Terminal ledger configuration mismatch')
    shape=c.execute('SELECT COUNT(*),COALESCE(MAX(length(CAST(payload AS BLOB))),0) FROM state').fetchone()
    if shape[0]!=1 or not 0<shape[1]<=runtime.MAX_BYTES:raise ValueError('Terminal checkpoint bound')
    if historical_source is None:state=read_checkpoint(c)
    else:
        from .paper_checkpoint import validate_checkpoint
        runtime.require_runtime(c,implementation=historical_source)
        state=validate_checkpoint(c,c.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
    runtime._history(c,metadata)
    if initial and (state!=initial_state(metadata) or c.execute('SELECT COUNT(*) FROM events').fetchone()[0]!=1
            or c.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0]!=0
            or c.execute('SELECT event_id,payload_hash FROM events WHERE seq=1').fetchone()!=(INIT['event_id'],digest(INIT))):
        raise ValueError('Exact unchanged INIT-only ledger required')
    return {'metadata_hash':digest(saved),'checkpoint_hash':digest(state),
            'events_hash':runtime._prefix(c,'events',1),'outcomes_hash':runtime._prefix(c,'outcomes',0)},metadata


# One active gate only; cached bytes never survive invocation or become shared
# mutable decoded objects. Every hit still checks the current SQL payload.
_GATE_BYTES = ContextVar('terminal_gate_verified_bytes', default=None)
MAX_GATE_CACHE_BYTES = 96 * 1024 * 1024
MAX_GATE_CLASSIFICATION_BYTES = 8 * 1024 * 1024
MAX_GATE_CACHE_PAGES = 512


def _current_proof_bytes(store,key):
    if not runtime._hash(key):raise ValueError('Evidence identity malformed')
    # Pin SQL shape and payload to one read snapshot. In particular, do not
    # fetch a corrupt TEXT raw_bytes value or any compressed content as part of
    # the scalar preflight; both can otherwise materialize unbounded data.
    with closing(store.connect()) as c:
        c.execute('BEGIN')
        shapes=c.execute("SELECT typeof(hash),length(CAST(hash AS BLOB)),typeof(payload),length(CAST(payload AS BLOB)),typeof(raw_bytes),CASE WHEN typeof(raw_bytes)='integer' THEN raw_bytes ELSE NULL END FROM pages WHERE hash=? LIMIT 2",(key,)).fetchall()
        if (len(shapes)!=1 or shapes[0][:3]!=('text',64,'blob')
                or type(shapes[0][3]) is not int or not 0<shapes[0][3]<=MAX_PROOF_COMPRESSED_BYTES
                or shapes[0][4]!='integer' or type(shapes[0][5]) is not int
                or not 0<shapes[0][5]<=MAX_PROOF_RAW_BYTES):
            raise ValueError('Evidence missing or oversized: invalid proof scalar shape')
        rows=c.execute("SELECT payload,raw_bytes FROM pages WHERE hash=? AND typeof(hash)='text' AND length(CAST(hash AS BLOB))=64 AND typeof(payload)='blob' AND length(payload) BETWEEN 1 AND ? AND typeof(raw_bytes)='integer' AND raw_bytes BETWEEN 1 AND ? LIMIT 2",(key,MAX_PROOF_COMPRESSED_BYTES,MAX_PROOF_RAW_BYTES)).fetchall()
        if (len(rows)!=1 or type(rows[0][0]) is not bytes or len(rows[0][0])!=shapes[0][3]
                or type(rows[0][1]) is not int or rows[0][1]!=shapes[0][5]):
            raise ValueError('Proof content changed or exceeded preflight')
        compressed,raw_bytes=rows[0]
    return compressed,raw_bytes


def _load(store,key):
    compressed,raw_bytes=_current_proof_bytes(store,key)
    cache=_GATE_BYTES.get()
    identity=(str(store.path.resolve()),key)
    cached=cache['pages'].get(identity) if cache is not None else None
    if cached is not None and cached[0]==compressed and len(cached[1])==raw_bytes:
        return json.loads(cached[1],object_pairs_hook=runtime._unique,
            parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite proof content')))
    inflater=zlib.decompressobj()
    try:
        raw=inflater.decompress(compressed,raw_bytes+1)
        if (not inflater.eof or inflater.unused_data or inflater.unconsumed_tail or len(raw)!=raw_bytes):
            raise ValueError('Evidence encoding mismatch')
        value=json.loads(raw,object_pairs_hook=runtime._unique,
            parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite proof content')))
    except zlib.error as error:
        raise ValueError('Evidence encoding mismatch') from error
    if digest(value)!=key:raise ValueError('Original evidence missing/corrupt')
    if cache is not None and identity not in cache['pages']:
        size=len(compressed)+len(raw)
        if len(cache['pages'])<MAX_GATE_CACHE_PAGES and cache['bytes']+size<=MAX_GATE_CACHE_BYTES:
            cache['pages'][identity]=(compressed,raw);cache['bytes']+=size
    return value


def _classification(store,key):
    """Only the two preparation inventory scanners use these immutable scalars."""
    cache=_GATE_BYTES.get()
    identity=(str(store.path.resolve()),key)
    compressed,raw_bytes=_current_proof_bytes(store,key)
    previous=cache['classifications'].get(identity) if cache is not None else None
    if previous is not None and previous[:2]==(compressed,raw_bytes):
        return previous[2]
    value=_load(store,key)
    scalars=tuple(value.get(name) if type(value) is dict and type(value.get(name)) is str else None
                  for name in ('kind','scan_id','intent_hash'))
    if cache is not None and identity not in cache['classifications']:
        size=len(compressed)+sum(len(x.encode()) for x in scalars if x is not None)+128
        if (all(x is None or len(x)<=128 for x in scalars)
                and len(cache['classifications'])<MAX_GATE_CACHE_PAGES
                and cache['classification_bytes']+size<=MAX_GATE_CLASSIFICATION_BYTES):
            cache['classifications'][identity]=(compressed,raw_bytes,scalars)
            cache['classification_bytes']+=size
    return scalars


def _wire(encoded,limit):
    if type(encoded) is not str or len(encoded)>4*((limit+2)//3):raise ValueError('Wire byte bound')
    raw=base64.b64decode(encoded,validate=True)
    if not raw or len(raw)>limit or base64.b64encode(raw).decode()!=encoded:raise ValueError('Wire bytes malformed')
    return runtime._parse(raw.decode())


def _proof(store,progress,v,cfg,*,current_budget):
    with closing(store.connect()) as c:
        _passes(c)
        row=c.execute('SELECT intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?',(v['pass_id'],)).fetchone()
    if row!=(v['intent_hash'],None):raise ValueError('Original NULL pass required')
    intent=_load(store,v['intent_hash']);outcome=_load(store,v['outcome_hash'])
    scan=v['scan_id']
    if (type(intent) is not dict or intent.get('kind')!='paper_cycle_intent_v1'
            or intent.get('config_hash')!=v['config_hash'] or intent.get('ledger')!=v['context']['ledger_db']
            or type(intent.get('targets')) is not list or len(intent['targets'])!=1
            or type(intent.get('admissions')) is not dict or set(intent['admissions'])!={scan}):raise ValueError('Single candidate legacy intent required')
    item=intent['targets'][0];target=item['target'];before=intent['admissions'][scan]
    if (target['scan_id']!=scan or type(before.get('requests_used')) is not int or before['requests_used']!=v['before']
            or before.get('request_ceiling')!=18 or before.get('state') not in ('ADMITTED','SEALED')
            or before['descriptor']['mint']!=target['mint']):raise ValueError('Original admission binding mismatch')
    after=progress.admission(scan)
    if (after is None or after['requests_used']!=v['after'] or after['request_ceiling']!=18
            or after['descriptor']!=before['descriptor'] or after['descriptor_hash']!=before['descriptor_hash']):
        raise ValueError('Retired admission/charges changed')
    if current_budget and after!={**before,'requests_used':v['after']}:
        raise ValueError('Admission changed beyond exact three charges')
    outcome_fields={'attempted_requests','blockers','budget','diagnostics','events','execution_status',
        'investigation_attempted_requests','kind','live_readiness','monitoring_attempted_requests','outcomes','status','usd_evidence_refs'}
    if v['association']=='INTRINSIC':outcome_fields|={'pass_id','intent_hash','attempt_refs','terminal_hazards'}
    if (type(outcome) is not dict or set(outcome)!=outcome_fields or outcome.get('kind')!='paper_cycle_v1' or outcome.get('status')!='BLOCKED'
            or outcome.get('blockers')!=['SOURCE_CONTENT_REJECTED'] or outcome.get('execution_status')!='EXECUTION_UNVERIFIED'
            or outcome.get('live_readiness') is not False or outcome.get('events')!=[] or outcome.get('outcomes')!=[]
            or outcome.get('usd_evidence_refs')!=[] or type(outcome.get('attempted_requests')) is not int or outcome['attempted_requests']!=len(v['attempt_refs'])
            or type(outcome.get('investigation_attempted_requests')) is not int or outcome['investigation_attempted_requests']!=len(v['attempt_refs'])
            or type(outcome.get('monitoring_attempted_requests')) is not int or outcome['monitoring_attempted_requests']!=0
            or outcome.get('budget')!={scan:{'used':v['after'],'ceiling':18}}):raise ValueError('Only known non-entry three-RPC rejection supported')
    diagnostics=outcome.get('diagnostics')
    if (type(diagnostics) is not list or len(diagnostics)!=1 or type(diagnostics[0]) is not dict
            or set(diagnostics[0])!={'scan_id','graduation'} or diagnostics[0]['scan_id']!=scan
            or diagnostics[0]['graduation'].get('status')!='OBSERVED_MIGRATION'
            or diagnostics[0]['graduation'].get('blockers')!=[]
            or diagnostics[0]['graduation'].get('entry_authorized') is not False):raise ValueError('Only pre-entry candidate rejection supported')
    if v['association']=='INTRINSIC':
        if (intent.get('terminal_context')!=v['context'] or intent.get('ledger_anchors')!=v['ledger_anchors']
                or intent.get('source_hash')!=v['source_hash'] or outcome.get('pass_id')!=v['pass_id']
                or outcome.get('intent_hash')!=v['intent_hash'] or outcome.get('attempt_refs')!=v['attempt_refs']
                or outcome.get('terminal_hazards')!=v['hazards']):raise ValueError('Intrinsic pass/attempt/snapshot binding missing')
    records=[];responses=[]
    fields={'kind','scan_id','requests_used','source_id','method','params','request_bytes_base64','response_bytes_base64','observed_at','http_status','failure_code'}
    for i,key in enumerate(v['attempt_refs']):
        record=_load(store,key)
        if (type(record) is not dict or set(record)!=fields or record['kind']!='paper_read_attempt_v1' or record['scan_id']!=scan
                or type(record['requests_used']) is not int or record['requests_used']!=v['before']+i+1
                or record['source_id']!='helius-mainnet-paper-confirmed-v1' or type(record['http_status']) is not int or record['http_status']!=200
                or record['failure_code'] is not None or type(record['observed_at']) is not int or not 0<=record['observed_at']<2**63):raise ValueError('Complete unambiguous charged RPC required')
        request=_wire(record['request_bytes_base64'],32768);response=_wire(record['response_bytes_base64'],2*1024*1024)
        if request!={'id':'paper-read-v1','jsonrpc':'2.0','method':record['method'],'params':record['params']} or set(response)!={'id','jsonrpc','result'} or response['id']!='paper-read-v1' or response['jsonrpc']!='2.0':raise ValueError('Request/response binding invalid')
        records.append(record);responses.append(response['result'])
    if [r['observed_at'] for r in records]!=sorted(r['observed_at'] for r in records) or records[-1]['observed_at']-records[0]['observed_at']>10:raise ValueError('Attempt timing inconsistent')
    opts={'commitment':'confirmed','encoding':'base64'}
    if (records[0]['method']!='getAccountInfo' or records[0]['params']!=[target['mint'],opts]
            or len(records)==3 and (records[1]['method']!='getAccountInfo' or records[1]['params']!=[target['pool'],opts]
            or records[2]['method']!='getMultipleAccounts')):raise ValueError('Exact mint/discovery/atomic request sequence required')
    raw=responses[0];at=records[0]['observed_at']
    mint_payload={'mint':target['mint'],'observed_at':at,'slot':raw['context']['slot'],'account':raw['value']}
    try:
        ingest_mint(lambda:ProviderObservation(records[0]['source_id'],at,mint_payload),mint=target['mint'],now=at,token_profile_version=selected(cfg))
    except PolicyRejection as error:
        if len(records)!=1 or error.domain!='mint' or list(error.reasons)!=v['hazards']:
            raise ValueError('Terminal mint rejection disagreement') from None
        return
    if len(records)!=3:raise ValueError('No known terminal mint rejection')
    calls=[]
    def rpc(method,params):
        index=len(calls)+1
        if index>2 or records[index]['method']!=method or records[index]['params']!=params:raise ValueError('Atomic snapshot request disagreement')
        calls.append(index);return responses[index]
    captures=[]
    def capture(value):captures.append(value);return digest(value)
    checked=verify_pool(target['pool'],target['mint'],rpc,capture=capture,token_profile_version=selected(cfg))
    if len(captures)!=1 or _load(store,digest(captures[0]))!=captures[0] or raw['value']['owner']!=responses[2]['value'][6]['owner']:
        raise ValueError('Mint/atomic token program mismatch')
    try:
        ingest_pool(lambda:ProviderObservation(records[2]['source_id'],records[2]['observed_at'],captures[0]),
            mint=target['mint'],pool=target['pool'],now=records[2]['observed_at'],token_profile_version=selected(cfg))
    except PolicyRejection as error:
        if error.domain!='pool' or list(error.reasons)!=v['hazards']:raise ValueError('Nonterminal pool ingestion failure') from None
    else:raise ValueError('No terminal pool rejection')
    if calls!=[1,2] or not checked['reasons'] or set(checked['reasons'])!=set(v['hazards']) or not set(checked['reasons'])<=HAZARDS:raise ValueError('Known terminal pool hazard not reproduced')
    if v['association']=='EXPLICIT_REVIEWED_LEGACY':
        witness=_load(store,v['invocation_hash'])
        if type(witness) is not list or len(witness)!=4:raise ValueError('Exact reviewed invocation witness required')
        ids=set();units=set();times=[]
        for r in witness:
            if (type(r) is not dict or not {'MESSAGE','__REALTIME_TIMESTAMP','_SYSTEMD_UNIT','UNIT','INVOCATION_ID'}<=set(r)
                    or set(r)-{'MESSAGE','__REALTIME_TIMESTAMP','_SYSTEMD_UNIT','UNIT','INVOCATION_ID','JOB_RESULT'}
                    or not _id(r['INVOCATION_ID']) or r['_SYSTEMD_UNIT']!='init.scope'
                    or type(r['MESSAGE']) is not str or len(r['MESSAGE'])>4096
                    or type(r['__REALTIME_TIMESTAMP']) is not str or not r['__REALTIME_TIMESTAMP'].isdigit() or len(r['__REALTIME_TIMESTAMP'])>20):raise ValueError('Invocation manager witness malformed')
            ids.add(r['INVOCATION_ID']);units.add(r['UNIT']);times.append(int(r['__REALTIME_TIMESTAMP']))
        if (len(ids)!=1 or len(units)!=1 or times!=sorted(times) or not 0<times[-1]-times[0]<=60_000_000
                or not times[0]//1_000_000<=records[0]['observed_at']<=records[-1]['observed_at']<=times[-1]//1_000_000
                or not witness[0]['MESSAGE'].startswith('Started '+witness[0]['UNIT']+' - ')
                or 'tools/history_first_paper_entry.py ' not in witness[0]['MESSAGE']
                or '--ledger-db '+v['context']['ledger_db'] not in witness[0]['MESSAGE']
                or '--research-db '+v['context']['research_db'] not in witness[0]['MESSAGE']
                or '--evidence-db '+v['context']['evidence_db'] not in witness[0]['MESSAGE']
                or witness[0].get('JOB_RESULT')!='done'
                or witness[1]['MESSAGE']!=witness[1]['UNIT']+': Main process exited, code=exited, status=2/INVALIDARGUMENT'
                or witness[2]['MESSAGE']!=witness[2]['UNIT']+": Failed with result 'exit-code'."
                or not witness[3]['MESSAGE'].startswith(witness[3]['UNIT']+': Consumed ')):raise ValueError('Invocation timing/command/termination disagreement')


def _monitoring(c, *, store=None, ledger=None, cfg=None, pacing=None, review_source=None):
    tables={x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'paper_monitoring_%'")}
    if not tables:return
    base={'paper_monitoring_budget','paper_monitoring_reservations','paper_monitoring_outcomes'}
    permitted=base|{'paper_monitoring_context_handoff','paper_monitoring_allowance_upgrade','paper_monitoring_context_successors'}
    if not base<=tables or not tables<=permitted:raise ValueError('Monitoring schema partial/unknown')
    if store is not None:
        from .monitoring_budget import MonitoringBudget
        from .evidence import EvidenceStore
        from .monitoring_handoff import read
        checked=EvidenceStore(store.path,read_only=True);checked.read_only=False
        handoff=read(c)
        if handoff:
            from .monitoring_successor import active
            tail=active(c,handoff)[0]
            ledger=tail['context']['new_ledger_db'];pacing=tail['context']['pacing_db']
            with closing(sqlite3.connect(Path(ledger).as_uri()+'?mode=ro',uri=True)) as current:
                from .runtime_extensions import _metadata
                cfg=runtime._parse(_metadata(current)['config'])
        elif ledger is None:
            row=c.execute('SELECT ledger FROM paper_monitoring_budget WHERE id=1').fetchone()
            if row is None:raise ValueError('Monitoring identity missing')
            ledger=row[0]
            with closing(sqlite3.connect(Path(ledger).as_uri()+'?mode=ro',uri=True)) as current:
                from .runtime_extensions import _metadata
                cfg=runtime._parse(_metadata(current)['config'])
        budget=MonitoringBudget(checked,ledger,cfg)
        if review_source is None:
            budget._checkpoint()
        else:
            # Read-only proposal validates the actual canonical predecessor;
            # active production gates never supply this private planning option.
            if not runtime._hash(review_source):raise ValueError('Review source malformed')
            budget.code_hash=review_source
            from .monitoring_handoff import validate_active
            if validate_active(c,budget,checkpoint=False) is None:
                with closing(sqlite3.connect(Path(ledger).as_uri()+'?mode=ro',uri=True)) as current:
                    runtime.require_runtime(current,implementation=review_source)
                    from .paper_checkpoint import read_checkpoint
                    read_checkpoint(current,_implementation=review_source)
        budget._accounting(c)
        if pacing is not None:_pacing(pacing)
    if c.execute('SELECT 1 FROM paper_monitoring_budget WHERE blocked IS NOT NULL LIMIT 1').fetchone():raise ValueError('Monitoring latch unresolved')
    if c.execute('SELECT 1 FROM paper_monitoring_reservations r LEFT JOIN paper_monitoring_outcomes o ON o.reservation_id=r.id WHERE o.reservation_id IS NULL LIMIT 1').fetchone():raise ValueError('Monitoring transport unresolved')


def plan(research_db,evidence_db,ledger_db,cfg,*,pass_id,outcome_hash,attempt_refs,invocation_hash,pacing_db):
    """READ ONLY candidate pin. This cannot authorize or append an association.

    Caller must hold canonical locks for reviewable stable anchors. Production
    pin review must separately certify the complete retained-attempt inventory.
    """
    from .evidence import EvidenceStore
    if (not _id(pass_id) or not runtime._hash(outcome_hash) or not runtime._hash(invocation_hash)
            or type(attempt_refs) not in (tuple,list) or len(attempt_refs)!=3
            or not all(runtime._hash(k) for k in attempt_refs) or len(set(attempt_refs))!=3
            or pacing_db is None):raise ValueError('Exact bounded legacy proof identities and pacing required')
    context=_context(research_db,evidence_db,ledger_db,pacing_db)
    store=EvidenceStore(context['evidence_db'],read_only=True);progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
    with closing(store.connect()) as c:
        _passes(c)
        row=c.execute('SELECT intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?',(pass_id,)).fetchone()
        _monitoring(c)
    if row is None or row[1] is not None:raise ValueError('Original NULL pass required')
    intent=_load(store,row[0]);scan=next(iter(intent['admissions']))
    with closing(sqlite3.connect(Path(context['ledger_db']).as_uri()+'?mode=ro',uri=True)) as c:
        anchors,_=_ledger(c,cfg,initial=True)
    v={'version':1,'association':'EXPLICIT_REVIEWED_LEGACY','pass_id':pass_id,'intent_hash':row[0],'outcome_hash':outcome_hash,
       'attempt_refs':list(attempt_refs),'invocation_hash':invocation_hash,'source_hash':runtime.implementation_hash(),
       'context':context,'config_hash':digest(cfg),'scan_id':scan,'before':intent['admissions'][scan]['requests_used'],
       'after':progress.admission(scan)['requests_used'],'ledger_anchors':anchors,'hazards':[]}
    # Derive reasons from exact raw atomic replay, never from supplied summary.
    records=[_load(store,k) for k in v['attempt_refs']]
    responses=[_wire(r['response_bytes_base64'],2*1024*1024)['result'] for r in records]
    calls=[]
    def rpc(method,params):
        i=len(calls)+1;calls.append(i)
        if i>2 or (method,params)!=(records[i]['method'],records[i]['params']):raise ValueError('Raw request disagreement')
        return responses[i]
    target=intent['targets'][0]['target']
    v['hazards']=sorted(set(verify_pool(target['pool'],target['mint'],rpc,capture=digest,token_profile_version=selected(cfg))['reasons']))
    if not _shape(v):raise ValueError('Unsupported reconciliation shape/profile')
    _proof(store,progress,v,cfg,current_budget=True);_pacing(context['pacing_db'])
    with closing(store.connect()) as c:
        _monitoring(c,store=store,ledger=context['ledger_db'],cfg=cfg,pacing=context['pacing_db'])
    return v


def _insert(c,v):
    rows=_rows(c)
    for old in rows:
        if old['pass_id']==v['pass_id']:
            if old!=v:raise ValueError('Conflicting terminal replay')
            return 'ALREADY_RECORDED'
        if old['scan_id']==v['scan_id']:raise ValueError('Retired scan cannot be reused')
    if len(rows)>=MAX_RECEIPTS:raise ValueError('Terminal receipt capacity exhausted')
    if not rows:
        c.execute(_schema())
        for sql in _guards().values():c.execute(sql)
    c.execute(f'INSERT INTO {TABLE} VALUES(?,?,?,?)',(v['pass_id'],v['scan_id'],canonical(v),digest(v)))
    return 'RECORDED'


def reconcile(research_db,evidence_db,ledger_db,cfg,*,pass_id,outcome_hash,attempt_refs,invocation_hash,pacing_db):
    """Explicit coordinator-only append. No pass update, retry or counter reset."""
    from .paper_cycle import _lock
    from .paper_observe_cli import _worker_lock,_existing_context
    context=_context(research_db,evidence_db,ledger_db,pacing_db)
    with ExitStack() as stack:
        if stack.enter_context(_worker_lock(context['research_db'])) is None:raise ValueError('Research worker busy')
        if not stack.enter_context(_lock(context['evidence_db']+'.ownership-invocation.lock')):raise ValueError('Evidence invocation busy')
        if not stack.enter_context(_lock(context['ledger_db']+'.paper-cycle.lock')):raise ValueError('Ledger cycle busy')
        v=plan(research_db,evidence_db,ledger_db,cfg,pass_id=pass_id,outcome_hash=outcome_hash,attempt_refs=attempt_refs,invocation_hash=invocation_hash,pacing_db=pacing_db)
        _approved(v)
        runtime.require_transition_context(research_db,evidence_db,ledger_db,reviewed_context={k:context[k] for k in ('research_db','evidence_db','ledger_db')})
        _,store,_=_existing_context(Path(context['research_db']),Path(context['evidence_db']),())
        with closing(store.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                # Revalidate all original evidence and counters while the writer
                # transaction is held; validation is nonmutating even on replay.
                _monitoring(c)
                fresh=plan(research_db,evidence_db,ledger_db,cfg,pass_id=pass_id,outcome_hash=outcome_hash,
                    attempt_refs=attempt_refs,invocation_hash=invocation_hash,pacing_db=pacing_db)
                if fresh!=v:raise ValueError('Reviewed evidence changed before append')
                _approved(fresh)
                status=_insert(c,v);c.commit()
            except BaseException:c.rollback();raise
        return {'status':status,'receipt_hash':digest(v),'retired_scan':v['scan_id'],'entry_authorized':False}


def gate(store,research,scan_ids,*,ledger_locked=None):
    """Active gate always validates the current runtime, never a proposal."""
    token=_GATE_BYTES.set({'pages':{},'bytes':0,'classifications':{},'classification_bytes':0})
    try:
        return _gate(store,research,scan_ids,ledger_locked=ledger_locked)
    finally:
        _GATE_BYTES.reset(token)


def _gate(store,research,scan_ids,*,ledger_locked=None,review_source=None):
    """Private read-only predecessor validation for coordinator pin planning."""
    from .paper_intake_uncaptured_retirement import gate as intake_gate
    intake=intake_gate(store,research,scan_ids,ledger_locked=ledger_locked,review_source=review_source)
    if intake is not None:return intake
    from .paper_migration_no_entry import gate as migration_gate
    migration = migration_gate(store,research,scan_ids,ledger_locked=ledger_locked,review_source=review_source)
    if migration is not None:
        return migration
    from .history_preparation_rejection import gate as preparation_rejection_gate
    rejection = preparation_rejection_gate(store, research, scan_ids, ledger_locked=ledger_locked)
    if rejection is not None:
        return rejection
    from .paper_cycle import _lock
    from .evidence import EvidenceStore
    with closing(store.connect()) as c:
        found=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_observation_passes'").fetchone()
        if not found:
            from .paper_http403_retirement import rows as http_rows
            from .paper_preparation_retirement import rows as preparation_rows
            from .paper_dispatch_preparation_retirement import rows as dispatch_rows
            if _rows(c) or http_rows(c) or preparation_rows(c) or dispatch_rows(c):raise ValueError('Original pass table missing')
            _monitoring(c,store=store,review_source=review_source)
            return None
        _passes(c)
        from .paper_http403_retirement import rows as http_rows
        from .paper_preparation_retirement import rows as preparation_rows
        from .paper_dispatch_preparation_retirement import rows as dispatch_rows
        rows=_rows(c)+http_rows(c)+preparation_rows(c)+dispatch_rows(c)
        if len({v['pass_id'] for v in rows})!=len(rows) or len({v['scan_id'] for v in rows})!=len(rows):raise ValueError('Conflicting terminal receipts')
        _monitoring(c,store=store,review_source=review_source)
        pending=c.execute('SELECT id,intent_hash FROM paper_observation_passes WHERE outcome_hash IS NULL LIMIT 257').fetchall()
    if len(pending)>256:raise ValueError('Pending pass bound')
    certified={}
    for v in rows:
        if v['context']['research_db']!=str(research) or v['context']['evidence_db']!=str(store.path):raise ValueError('Receipt context mismatch')
        path=Path(v['context']['ledger_db'])
        with ExitStack() as stack:
            if ledger_locked!=str(path):
                if not stack.enter_context(_lock(str(path)+'.paper-cycle.lock')):raise ValueError('Receipt ledger busy')
            historical_source=review_source
            with closing(store.connect()) as evidence:
                from .monitoring_handoff import read as read_handoff
                from .monitoring_successor import rows as successor_rows
                first=read_handoff(evidence)
                if first:
                    history=successor_rows(evidence,first)
                    retired=[first]+history
                    matches=[edge for edge,_ in retired if edge['context']['old_ledger_db']==str(path) and edge['old_config_hash']==v['config_hash']]
                    if matches:historical_source=matches[-1]['old_source']
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
                anchors,cfg=_ledger(c,None,initial=False,historical_source=historical_source)
            if (anchors['metadata_hash']!=v['ledger_anchors']['metadata_hash'] or anchors['events_hash']!=v['ledger_anchors']['events_hash']
                    or digest(cfg)!=v['config_hash']):raise ValueError('Certified ledger prefix/config changed')
            with closing(store.connect()) as c:
                _monitoring(c,store=store,ledger=path,cfg=cfg,pacing=v['context']['pacing_db'],review_source=review_source)
            progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
            if v['association']=='EXPLICIT_REVIEWED_HTTP403':
                from .paper_http403_retirement import proof as http_proof
                http_proof(store,progress,v,cfg,current_budget=False)
            elif v['association']=='EXPLICIT_REVIEWED_PREPARATION_OVERSIZE':
                from .paper_preparation_retirement import proof as preparation_proof
                preparation_proof(store,progress,v,cfg,current_budget=False)
            elif v['association']=='EXPLICIT_REVIEWED_DISPATCH_PREPARATION_UNCAPTURED':
                from .paper_dispatch_preparation_retirement import proof as dispatch_proof
                dispatch_proof(store,progress,v,cfg,current_budget=False)
            else:_proof(store,progress,v,cfg,current_budget=False)
        _pacing(v['context']['pacing_db'])
        certified[v['pass_id']]=v['intent_hash']
    if any(v['scan_id'] in scan_ids for v in rows):return 'REJECTED_SCAN_RETIRED'
    if any(certified.get(identity)!=key for identity,key in pending):return 'OBSERVATION_RECOVERY_REQUIRED'
    return None


def certify_intrinsic(store,progress,cfg,*,pass_id,intent_hash,outcome_hash,attempt_refs,hazards):
    """Only the locked INIT-only cycle can certify its complete intrinsic result."""
    intent=_load(store,intent_hash);scan=next(iter(intent['admissions']))
    v={'version':1,'association':'INTRINSIC','pass_id':pass_id,'intent_hash':intent_hash,'outcome_hash':outcome_hash,
       'attempt_refs':list(attempt_refs),'invocation_hash':None,'source_hash':intent['source_hash'],
       'context':intent['terminal_context'],'config_hash':digest(cfg),'scan_id':scan,
       'before':intent['admissions'][scan]['requests_used'],'after':progress.admission(scan)['requests_used'],
       'ledger_anchors':intent['ledger_anchors'],'hazards':list(hazards)}
    if not _shape(v) or v['source_hash']!=runtime.implementation_hash():raise ValueError('Intrinsic shape/source unsupported')
    with closing(sqlite3.connect(Path(v['context']['ledger_db']).as_uri()+'?mode=ro',uri=True)) as c:
        anchors,_=_ledger(c,cfg,initial=True)
    if anchors!=v['ledger_anchors']:raise ValueError('Ledger changed during rejected pass')
    _proof(store,progress,v,cfg,current_budget=True);_pacing(v['context']['pacing_db'])
    with closing(store.connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        try:_monitoring(c,store=store,ledger=v['context']['ledger_db'],cfg=cfg,pacing=v['context']['pacing_db']);_insert(c,v);c.commit()
        except BaseException:c.rollback();raise
    return digest(v)


def review_plan(research_db,evidence_db,ledger_db,cfg,**proof):
    """Locked, read-only candidate pin output; never installs policy or receipt."""
    from .paper_cycle import _lock
    from .paper_observe_cli import _worker_lock
    context=_context(research_db,evidence_db,ledger_db,proof.get('pacing_db'))
    with ExitStack() as stack:
        if stack.enter_context(_worker_lock(context['research_db'])) is None:raise ValueError('Research worker busy')
        if not stack.enter_context(_lock(context['evidence_db']+'.ownership-invocation.lock')):raise ValueError('Evidence invocation busy')
        if not stack.enter_context(_lock(context['ledger_db']+'.paper-cycle.lock')):raise ValueError('Ledger cycle busy')
        runtime.require_transition_context(research_db,evidence_db,ledger_db,reviewed_context={k:context[k] for k in ('research_db','evidence_db','ledger_db')})
        return plan(research_db,evidence_db,ledger_db,cfg,**proof)


def main(argv=None):
    import argparse,json
    from .paper_cycle_cli import _config
    parser=argparse.ArgumentParser(description='Explicit reviewed terminal candidate rejection; no retry/reset')
    for name in ('research-db','evidence-db','ledger-db','config','pass-id','outcome-hash','invocation-hash','pacing-db'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--attempt-ref',action='append',required=True)
    parser.add_argument('--plan',action='store_true',help='Read-only proposed pin, never authorization')
    args=parser.parse_args(argv)
    try:
        proof=dict(pass_id=args.pass_id,outcome_hash=args.outcome_hash,attempt_refs=tuple(args.attempt_ref),
            invocation_hash=args.invocation_hash,pacing_db=args.pacing_db)
        function=review_plan if args.plan else reconcile
        result=function(args.research_db,args.evidence_db,args.ledger_db,_config(args.config),**proof)
        print(json.dumps(result,sort_keys=True));return 0
    except (ValueError,TypeError,KeyError,IndexError,OSError,sqlite3.Error):
        print(json.dumps({'status':'BLOCKED','blockers':['TERMINAL_RECONCILIATION_UNPROVED'],'entry_authorized':False}));return 2


if __name__=='__main__':raise SystemExit(main())
