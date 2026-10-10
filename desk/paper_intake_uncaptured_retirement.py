"""Exact reviewed seq1340 quarantine. Request five remains charged and unknown.

No completed intake, provider failure or no-I/O inference. No journal mutation.
"""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
from . import paper_migration_no_entry as prior, paper_terminal_reconciliation as terminal
from . import paper_preparation_retirement as base, runtime_compatibility as runtime
from .model import canonical, digest
from .evidence import EvidenceStore

TABLE='paper_intake_uncaptured_retirement'
POLICY=Path(__file__).resolve().parents[1]/'config/paper-intake-uncaptured-retirement.json'
CASE={'dispatch_id':'4d9735989e084e7b944092640d535232','intent_hash':'7550e5ef8daf5391e5208c80876d5898e3e314d2adb0be7c1d91bc8c6227a8b8','scan_id':'df8ac72596cf4d30bf552aaf1b2c6adf','producer_context_hash':'fd31decc9049fb5f522e9a2491173bc3a52576ac0b3eef6b246e1632b8a59a42','history_id':'fa05899b577a3291e41a043a2844e8a69a5f4af0c4742a1f9de7f322cd0ecd53'}
FIELDS={'version','kind',*CASE,'context','producer_context','successor_context','source_hash','config_hash','journal_prefix','parent_receipt_hash','admission','canonical_acquisition','history_inventory_hash','ledger_original_hash','ledger_prefix_hash','runtime_receipts_hash','ledger_backup_hash','stopped_witness_hash','before','after','transport_outcome','no_retry','entry_authorized'}
MARKER={'kind':'intake_uncaptured_retirement_installed_v1','table':TABLE}


def schema():return f'CREATE TABLE {TABLE}(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL,payload_hash TEXT NOT NULL)'
def guards():
    result={TABLE+'_insert':f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN EXISTS(SELECT 1 FROM {TABLE}) BEGIN SELECT RAISE(ABORT,'Intake retirement immutable'); END"}
    return result|{TABLE+'_'+a.lower():f"CREATE TRIGGER {TABLE}_{a.lower()} BEFORE {a} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Intake retirement immutable'); END" for a in ('UPDATE','DELETE')}


def shape(v):
    if (type(v) is not dict or set(v)!=FIELDS or type(v['version']) is not int or v['version']!=1 or v['kind']!='EXPLICIT_REVIEWED_INTAKE_RESERVATION_UNCAPTURED'
        or any(v[k]!=x for k,x in CASE.items()) or type(v['before']) is not int or v['before']!=4 or type(v['after']) is not int or v['after']!=5
        or v['transport_outcome']!='UNKNOWN_UNCAPTURED' or v['no_retry'] is not True or v['entry_authorized'] is not False):raise ValueError('Exact historical intake case required')
    if not all(runtime._hash(v[k]) for k in ('source_hash','config_hash','parent_receipt_hash','history_inventory_hash','ledger_original_hash','ledger_prefix_hash','runtime_receipts_hash','ledger_backup_hash','stopped_witness_hash')):raise ValueError('Intake proof identities')
    if type(v['context']) is not dict or set(v['context'])!={'research_db','evidence_db','ledger_db','pacing_db'}:raise ValueError('Intake context')
    if terminal._context(**dict(zip(('research','evidence','ledger','pacing'),(v['context'][k] for k in ('research_db','evidence_db','ledger_db','pacing_db')))))!=v['context']:raise ValueError('Canonical intake context')


def approved(v):
    shape(v)
    with POLICY.open('rb') as f:raw=f.read(1024*1024+1)
    if len(raw)>1024*1024:raise ValueError('Intake policy bound')
    policy=runtime._parse(raw.decode())
    if type(policy) is not dict or set(policy)!={'version','retirements'} or type(policy['version']) is not int or policy['version']!=1 or type(policy['retirements']) is not list or len(policy['retirements'])!=1 or policy['retirements'][0]!=v:raise ValueError('Exact independent intake pin required')


def rows(c):
    base._schema_bounds(c)
    objects=c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name=? OR tbl_name=? OR substr(name,1,?)=?',(TABLE,TABLE,len(TABLE)+1,TABLE+'_')).fetchall()
    if not objects:return []
    expected={('table',TABLE,TABLE,schema())}|{('trigger',k,TABLE,s) for k,s in guards().items()}
    if len(objects)!=len(expected) or set(objects)!=expected:raise ValueError('Intake retirement schema/guards')
    if c.execute('SELECT count(*) FROM '+TABLE).fetchone()!=(1,):raise ValueError('Partial intake retirement')
    scalars=c.execute('SELECT id,typeof(payload),length(CAST(payload AS BLOB)),typeof(payload_hash),length(CAST(payload_hash AS BLOB)) FROM '+TABLE).fetchone()
    if scalars[0]!=1 or scalars[1]!='text' or not 0<scalars[2]<=1024*1024 or scalars[3:]!=('text',64):raise ValueError('Intake retirement scalar bound')
    raw,key=c.execute('SELECT payload,payload_hash FROM '+TABLE).fetchone();v=runtime._parse(raw)
    if canonical(v)!=raw or digest(v)!=key:raise ValueError('Intake receipt hash')
    approved(v);return [v]


def _capture(store,producer,scan,hint):
    from .job_persistence import JobPersistence
    from .ownership_acquisition import _Setup,_policy,_validate_binding
    from .paper_observation_collector import _admission,ObservationTarget,_Blocked
    from .paper_target_export import _bounded
    from .migration_slot_intake import _hints
    from .replay_history import replay_history
    progress=prior._progress(store);ctx={k:producer['paths'][k]['path'] for k in ('research_db','evidence_db','ledger_db','pacing_db')}
    _hints(scan,hint['mint'],hint['pool'],hint['signature'],hint['slot'],'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE')
    jobs=JobPersistence.__new__(JobPersistence);jobs.path=Path(ctx['research_db'])
    def connect():
        c=sqlite3.connect(jobs.path.as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row;return c
    jobs.connect=connect
    with closing(connect()) as c:
        c.execute('BEGIN');_bounded(c,'scan_jobs','descriptor','WHERE scan_id=?',(scan,),1,8192);_bounded(c,'scans','result','WHERE id=?',(scan,),1,2*1024*1024)
    try:_admission(jobs,progress,ObservationTarget(scan,hint['mint'],hint['pool'],hint['mint'],1))
    except _Blocked:raise ValueError('Exact admitted intake required') from None
    admission=progress.admission(scan);descriptor=jobs.descriptor(scan);source=jobs.source(scan)
    if (admission['state']!='SEALED' or admission['prepared_requests_used']!=4 or admission['requests_used']!=5 or admission['request_ceiling']!=18 or source!=admission['prepared_source'] or source['status']!='COMPLETE'):raise ValueError('Exact preserved four plus uncaptured fifth charge')
    report=runtime._parse(source['result'])
    if report.get('report_hash')!=digest({k:x for k,x in report.items() if k!='report_hash'}):raise ValueError('Acquisition report hash')
    setup=_Setup.__new__(_Setup);setup.store=store;setup.descriptor=descriptor;setup.identity=scan;state=setup.read();_validate_binding(report,descriptor,state)
    mint=terminal._load(store,state['mint_hash']);cutoff=terminal._load(store,state['cutoff_hash'])
    if mint.get('method')!='getAccountInfo' or mint.get('params')!=[hint['mint'],{'encoding':'base64','commitment':'confirmed'}] or cutoff.get('method')!='getSlot' or cutoff.get('params')!=[{'commitment':'finalized'}]:raise ValueError('Canonical acquisition request binding')
    if _policy(state,mint=hint['mint'],token_profile_version=descriptor.get('paper_token_profile_version',0))['decision']!='PASS_TOKEN_POLICY' or hint['slot']>state['cutoff']:raise ValueError('Original token/cutoff binding')
    seeds=report.get('history_queries')
    if type(seeds) is not list or len(seeds)!=1 or seeds[0]['address']!=hint['mint'] or len(seeds[0]['pages'])!=2 or seeds[0].get('slot_range')!={'gte':0,'lt':state['cutoff']+1}:raise ValueError('Exact two acquisition seed records')
    replay_history(seeds[0],store)
    query={'address':hint['pool'],'start':0,'end':1,'token_accounts_filter':'none','slot_range':{'gte':hint['slot'],'lt':hint['slot']+1}}
    identity=digest({'budget':scan,'query':query});history=base._histories(store,scan)
    if identity!=CASE['history_id'] or len(history)!=1 or history[0][1:]!=(identity,scan,canonical(query),None,'RETRYABLE_ERROR',1):raise ValueError('Exact NULL first intake reservation')
    if base._attempts(store,scan):raise ValueError('Uncaptured fifth cannot have any retained transport')
    evidence={'evidence_class':'FOUR_CANONICAL_ACQUISITION_RECORDS_NOT_ORIGINAL_WIRE','descriptor_hash':digest(descriptor),'source_hash':digest(source),'setup_hash':digest(state),'mint_hash':state['mint_hash'],'cutoff_hash':state['cutoff_hash'],'seed_request_refs':[p['request_evidence_hash'] for p in seeds[0]['pages']]}
    return admission,evidence,digest(history)


def _prior(store,c,producer,*,review_source=None):
    with closing(store.connect()) as ec:
        candidates=[v for v in prior.rows(ec) if v['association']=='EXPLICIT_REVIEWED_LEGACY_SENTINEL' and v['successor_context']==producer]
    if len(candidates)!=1:raise ValueError('Exact prior migration lineage required')
    old=candidates[0];prior.proof(store,old,review_source=review_source);prior._prefix(c,old)
    from . import paper_dispatch_preparation_retirement as parent
    with closing(store.connect()) as ec:parents=[p for p in parent.rows(ec) if digest(p)==old['parent_receipt_hash']]
    if len(parents)!=1 or parents[0]['successor_context']!=old['producer_context']:raise ValueError('Original parent pin changed')
    p=parents[0];original=runtime._parse(old['journal_prefix']['context'][0][2])
    if digest(original)!=p['dispatch_context_hash']:raise ValueError('Original context mismatch')
    bindings={r[1]:runtime._parse(r[-2])['context_hash'] for r in old['journal_prefix']['intents']}
    contexts={digest(original):original,digest(old['producer_context']):old['producer_context'],digest(producer):producer}
    if any(h not in contexts for h in bindings.values()):raise ValueError('Unknown prior intent context')
    return old,bindings,{p['dispatch_id']:p['dispatch_context_hash'],old['dispatch_id']:digest(old['producer_context'])},contexts


def _runtime_prefix(c):
    # Preserve the already-used first extension, not merely its two ancestors.
    from . import runtime_extensions as extensions
    values=extensions._bounded_rows(c)
    if not values or values[0][0]!=1:raise ValueError('Original first extension missing')
    return {'first_and_continuation':prior._receipt_history(c),'first_extension':values[0]}


def proof(store,v,*,initial=False,cfg=None,review_source=None):
    shape(v);ctx=v['context'];producer=v['producer_context']
    if str(store.path)!=ctx['evidence_db'] or digest(producer)!=v['producer_context_hash'] or producer['config_hash']!=v['config_hash'] or any(producer['paths'][k]['path']!=ctx[k] for k in ctx):raise ValueError('Pinned producer context required')
    from tools import paper_entry_dispatcher as dispatcher
    path=dispatcher._path(producer['journal'],private=True)
    if path.stat().st_size>dispatcher.MAX_JOURNAL or any(Path(str(path)+x).exists() for x in ('-wal','-shm','-journal')):raise ValueError('Interrupted journal')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN');values=prior._prefix(c,v,initial=initial)
        old,bindings,retired,contexts=_prior(store,c,producer,review_source=review_source)
        if digest(old)!=v['parent_receipt_hash']:raise ValueError('Prior migration pin changed')
        intent=values['intents'].get(v['dispatch_id'])
        if intent is None or digest(intent)!=v['intent_hash'] or intent['context_hash']!=digest(producer):raise ValueError('Original unresolved intent')
        if initial and (len(values['intents'])!=4 or len(values['results'])!=1):raise ValueError('Exact four intent one result prefix')
        prefix_values={table:{row[1]:runtime._parse(row[-2]) for row in records} for table,records in v['journal_prefix'].items()}
        class PrefixReader:
            def execute(self,sql):
                if sql!='SELECT id,mint,signature FROM intents':raise ValueError('Unexpected prefix query')
                return [(r[1],r[2],r[3]) for r in v['journal_prefix']['intents']]
        dispatcher._check_records(PrefixReader(),producer,prefix_values,bindings|{v['dispatch_id']:digest(producer)},retired|{v['dispatch_id']:digest(producer)},historical_contexts=contexts)
    wanted=dict(producer);wanted.update({k:v['successor_context'].get(k) for k in ('source_hash','tool_hash','entry_tool_hash')})
    if wanted!=v['successor_context'] or wanted['source_hash']!=v['source_hash']:raise ValueError('Source/tool-only successor')
    admission,evidence,history_hash=_capture(store,producer,v['scan_id'],intent['hint'])
    if (admission,evidence,history_hash)!=(v['admission'],v['canonical_acquisition'],v['history_inventory_hash']):raise ValueError('Retained proof changed')
    witness=terminal._load(store,v['stopped_witness_hash'])
    expected={'kind':'migration_dispatch_stopped_review_v1','dispatch_id':v['dispatch_id'],'dispatch_intent_hash':v['intent_hash'],'producer_source_hash':producer['source_hash'],'exit_status':2,'service_active':False,'timer_active':False,'observed_at':witness.get('observed_at')}
    if witness!=expected or type(witness['observed_at']) is not int or not intent['at']<=witness['observed_at']<2**63:raise ValueError('Exact stopped owner witness')
    with closing(sqlite3.connect(Path(ctx['ledger_db']).as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN');_,saved_cfg=terminal._ledger(c,cfg,initial=initial,historical_source=review_source)
        if digest(saved_cfg)!=v['config_hash'] or base._ledger_originals(c,prefix=True)!=v['ledger_prefix_hash'] or digest(_runtime_prefix(c))!=v['runtime_receipts_hash']:raise ValueError('Original ledger/runtime prefix changed')
        if initial and base._ledger_originals(c)!=v['ledger_original_hash']:raise ValueError('Ledger changed before append')
    with closing(store.connect()) as c:terminal._monitoring(c,store=store,ledger=Path(ctx['ledger_db']),cfg=saved_cfg,pacing=ctx['pacing_db'],review_source=review_source)
    terminal._pacing(ctx['pacing_db'])
    with closing(sqlite3.connect(Path(ctx['pacing_db']).as_uri()+'?mode=ro',uri=True)) as c:
        if c.execute('SELECT 1 FROM waiters LIMIT 1').fetchone():raise ValueError('Pacing waiter pending')
    return v


def _plan(store,producer,identity,scan,backup,stopped,review_source):
    from .paper_cycle_cli import _config
    from tools import paper_entry_dispatcher as dispatcher
    if identity!=CASE['dispatch_id'] or scan!=CASE['scan_id'] or digest(producer)!=CASE['producer_context_hash']:raise ValueError('Only exact historical case')
    cfg=_config(producer['paths']['config']['path']);ctx={k:producer['paths'][k]['path'] for k in ('research_db','evidence_db','ledger_db','pacing_db')}
    with closing(sqlite3.connect(Path(producer['journal']).as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN');values,prefix=prior._journal(c);intent=values['intents'].get(identity)
        if intent is None or digest(intent)!=CASE['intent_hash'] or identity in values['results']:raise ValueError('Exact unresolved dispatch required')
        old,*_=_prior(store,c,producer,review_source=review_source)
    admission,evidence,history_hash=_capture(store,producer,scan,intent['hint'])
    path=Path(backup)
    if not path.is_absolute() or not path.is_file() or path.resolve(strict=True)!=path or path.samefile(ctx['ledger_db']) or path.stat().st_size>32*1024*1024:raise ValueError('Distinct bounded original backup')
    with closing(sqlite3.connect(Path(ctx['ledger_db']).as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN');terminal._ledger(c,cfg,initial=True,historical_source=review_source);original=base._ledger_originals(c);prefix_hash=base._ledger_originals(c,prefix=True);receipts=_runtime_prefix(c)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN')
        if base._ledger_originals(c)!=original or _runtime_prefix(c)!=receipts:raise ValueError('Backup original rows/receipts differ')
    successor=dispatcher.plan(**{k:x['path'] for k,x in producer['paths'].items()},journal=producer['journal'],taker=producer['taker'],amount_raw=producer['amount_raw'],pool_fee_bps=producer['pool_fee_bps'])
    v={'version':1,'kind':'EXPLICIT_REVIEWED_INTAKE_RESERVATION_UNCAPTURED',**CASE,'context':ctx,'producer_context':producer,'successor_context':successor,'source_hash':runtime.implementation_hash(),'config_hash':digest(cfg),'journal_prefix':prefix,'parent_receipt_hash':digest(old),'admission':admission,'canonical_acquisition':evidence,'history_inventory_hash':history_hash,'ledger_original_hash':original,'ledger_prefix_hash':prefix_hash,'runtime_receipts_hash':digest(receipts),'ledger_backup_hash':hashlib.sha256(path.read_bytes()).hexdigest(),'stopped_witness_hash':stopped,'before':4,'after':5,'transport_outcome':'UNKNOWN_UNCAPTURED','no_retry':True,'entry_authorized':False}
    proof(store,v,initial=True,cfg=cfg,review_source=review_source)
    if terminal._gate(store,Path(ctx['research_db']),(),ledger_locked=ctx['ledger_db'],review_source=review_source) is not None:raise ValueError('Other observation recovery pending')
    return v


def review_plan(producer_context,dispatch_id,scan_id,*,ledger_backup,stopped_witness_hash,review_source=None,apply=False):
    if review_source is not None and (apply or review_source!=producer_context['source_hash'] or not runtime._hash(review_source)):raise ValueError('Producer override read-only only')
    ctx={k:producer_context['paths'][k]['path'] for k in ('research_db','evidence_db','ledger_db','pacing_db')};store=EvidenceStore(ctx['evidence_db'],read_only=True)
    with prior._locks(ctx,producer_context['journal']):
        v=_plan(store,producer_context,dispatch_id,scan_id,ledger_backup,stopped_witness_hash,review_source)
        if not apply:return v
        approved(v);store.read_only=False
        with closing(store.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                fresh=_plan(store,producer_context,dispatch_id,scan_id,ledger_backup,stopped_witness_hash,None)
                if fresh!=v:raise ValueError('Proof changed before append')
                old=rows(c)
                if old:
                    if old!=[v]:raise ValueError('Conflicting intake receipt')
                    c.rollback();return {'status':'ALREADY_RECORDED','receipt_hash':digest(v)}
                c.execute(schema())
                for sql in guards().values():c.execute(sql)
                c.execute('INSERT INTO '+TABLE+' VALUES(1,?,?)',(canonical(v),digest(v)))
                import zlib
                raw=canonical(MARKER).encode();key=digest(MARKER);compressed=zlib.compress(raw)
                scalar=c.execute("SELECT typeof(payload),length(payload),typeof(raw_bytes),CASE WHEN typeof(raw_bytes)='integer' THEN raw_bytes END FROM pages WHERE hash=? LIMIT 2",(key,)).fetchall()
                if scalar:
                    if scalar!=[('blob',len(compressed),'integer',len(raw))] or terminal._load(store,key)!=MARKER:raise ValueError('Marker conflict')
                else:
                    used=c.execute('SELECT COALESCE(sum(length(payload)),0) FROM pages').fetchone()[0]
                    if used+len(compressed)>store.max_bytes:raise ValueError('Evidence capacity')
                    c.execute('INSERT INTO pages VALUES(?,?,?)',(key,compressed,len(raw)))
                rows(c);c.commit()
            except BaseException:c.rollback();raise
        return {'status':'RECORDED','receipt_hash':digest(v),'retired_scan':scan_id,'entry_authorized':False}


def gate(store,research,scans,*,ledger_locked=None,review_source=None):
    with closing(store.connect()) as c:
        c.execute('BEGIN');values=rows(c);installed=c.execute('SELECT 1 FROM pages WHERE hash=?',(digest(MARKER),)).fetchone()
    if not values:
        if installed:raise ValueError('Intake retirement table missing')
        return None
    if not installed or terminal._load(store,digest(MARKER))!=MARKER:raise ValueError('Partial intake publication')
    v=values[0]
    if str(research)!=v['context']['research_db']:raise ValueError('Intake research mismatch')
    from .paper_cycle import _lock
    from contextlib import ExitStack
    with ExitStack() as locks:
        if ledger_locked!=v['context']['ledger_db'] and not locks.enter_context(_lock(v['context']['ledger_db']+'.paper-cycle.lock')):raise ValueError('Intake ledger busy')
        proof(store,v,review_source=review_source)
    return 'REJECTED_SCAN_RETIRED' if v['scan_id'] in scans else None


def lineage(c,expected,original,*,ledger_locked=None):
    store=EvidenceStore(expected['paths']['evidence_db']['path'],read_only=True)
    with closing(store.connect()) as ec:certs=rows(ec)
    if not certs:return None
    v=certs[0]
    if expected!=v['successor_context'] or expected['journal']!=v['producer_context']['journal']:raise ValueError('Exact intake successor required')
    proof(store,v);prior._prefix(c,v)
    old,bindings,retired,contexts=_prior(store,c,v['producer_context'])
    if original!=runtime._parse(v['journal_prefix']['context'][0][2]):raise ValueError('Original journal context changed')
    bindings.update({r[1]:runtime._parse(r[-2])['context_hash'] for r in v['journal_prefix']['intents']})
    if any(h not in contexts for h in bindings.values()):raise ValueError('Unknown prefix context')
    return bindings,retired|{v['dispatch_id']:digest(v['producer_context'])},contexts


def main(argv=None):
    import argparse,json
    p=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    for k in ('producer-context','dispatch-id','scan-id','ledger-backup','stopped-witness-hash'):p.add_argument('--'+k,required=True)
    p.add_argument('--review-source');p.add_argument('--apply',action='store_true');a=p.parse_args(argv)
    try:
        with Path(a.producer_context).open('rb') as f:raw=f.read(65537)
        if len(raw)>65536:raise ValueError('Producer context bound')
        result=review_plan(runtime._parse(raw.decode()),a.dispatch_id,a.scan_id,ledger_backup=a.ledger_backup,stopped_witness_hash=a.stopped_witness_hash,review_source=a.review_source,apply=a.apply)
        print(json.dumps(result,sort_keys=True));return 0
    except (ValueError,TypeError,KeyError,IndexError,OSError,sqlite3.Error,AttributeError):
        print(json.dumps({'status':'BLOCKED','entry_authorized':False}));return 2

if __name__=='__main__':raise SystemExit(main())
