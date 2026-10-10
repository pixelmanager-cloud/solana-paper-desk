"""Narrow captured intake rejection and explicit historical journal continuation.

Canonical acquisition evidence is NOT original transport. Only intake has raw
request/response bytes. Certificates quarantine scans; they never authorize entry.
"""
from contextlib import closing, ExitStack
import hashlib
import math
from pathlib import Path
import sqlite3
from . import runtime_compatibility as runtime, paper_terminal_reconciliation as terminal
from . import paper_preparation_retirement as base
from .model import canonical,digest
from .evidence import EvidenceStore
from .history_progress import HistoryProgress

TABLE='paper_migration_no_entry'
POLICY=Path(__file__).resolve().parents[1]/'config/paper-migration-recovery.json'
MAX=32
LEGACY_HASH='bd8751533ee11955952342729d794fd34306bcc20b349bc9ef596c973dbef474'
FIELDS={'version','kind','association','dispatch_id','scan_id','intent_hash','hint','context','config_hash',
        'source_hash','producer_context','successor_context','journal_prefix','parent_receipt_hash',
        'history_id','history_inventory_hash','admission','canonical_acquisition','wire_attempt_refs',
        'migration','evaluated_at','reason','ledger_original_hash','ledger_prefix_hash','ledger_backup_hash',
        'stopped_witness_hash','runtime_receipts_hash','entry_authorized','execution_status','no_retry'}


def schema():return f'CREATE TABLE {TABLE}(dispatch_id TEXT PRIMARY KEY,scan_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,payload_hash TEXT NOT NULL)'
def guards():
    sql={TABLE+'_insert':f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN (SELECT count(*) FROM {TABLE})>={MAX} OR EXISTS(SELECT 1 FROM {TABLE} WHERE dispatch_id=NEW.dispatch_id OR scan_id=NEW.scan_id) BEGIN SELECT RAISE(ABORT,'Migration disposition conflict'); END"}
    return sql|{TABLE+'_'+a.lower():f"CREATE TRIGGER {TABLE}_{a.lower()} BEFORE {a} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Migration disposition immutable'); END" for a in ('UPDATE','DELETE')}


def shape(v):
    if type(v) is not dict or set(v)!=FIELDS or type(v['version']) is not int or v['version']!=1 or v['kind']!='dispatcher_migration_no_entry_v1':raise ValueError('Migration disposition shape')
    if v['association'] not in ('CAPTURED_UNSUPPORTED_QUOTE','EXPLICIT_REVIEWED_LEGACY_SENTINEL') or v['entry_authorized'] is not False or v['execution_status']!='EXECUTION_UNVERIFIED' or v['no_retry'] is not True:raise ValueError('Migration disposition semantics')
    if not all(terminal._id(v[k]) for k in ('dispatch_id','scan_id')) or not all(runtime._hash(v[k]) for k in ('intent_hash','config_hash','source_hash','history_id','history_inventory_hash','ledger_original_hash','ledger_prefix_hash','runtime_receipts_hash')):raise ValueError('Migration disposition identities')
    if type(v['evaluated_at']) is not int or not 0<=v['evaluated_at']<2**63 or type(v['context']) is not dict or set(v['context'])!={'research_db','evidence_db','ledger_db','pacing_db'}:raise ValueError('Migration disposition context')
    terminal._context(**dict(zip(('research','evidence','ledger','pacing'),(v['context'][k] for k in ('research_db','evidence_db','ledger_db','pacing_db')))))
    if not isinstance(v['wire_attempt_refs'],list) or not 1<=len(v['wire_attempt_refs'])<=2 or len(set(v['wire_attempt_refs']))!=len(v['wire_attempt_refs']) or not all(runtime._hash(x) for x in v['wire_attempt_refs']):raise ValueError('Migration wire inventory')
    historical=v['association']=='EXPLICIT_REVIEWED_LEGACY_SENTINEL'
    if historical:
        if not all(runtime._hash(v[k]) for k in ('ledger_backup_hash','stopped_witness_hash','parent_receipt_hash')) or type(v['successor_context']) is not dict:raise ValueError('Historical certificate required')
    elif any(v[k] is not None for k in ('ledger_backup_hash','stopped_witness_hash','parent_receipt_hash','successor_context')):raise ValueError('No automatic historical adoption')
    return historical


def approved(v):
    with POLICY.open('rb') as f:raw=f.read(2*1024*1024+1)
    if len(raw)>2*1024*1024:raise ValueError('Migration policy bound')
    p=runtime._parse(raw.decode())
    if type(p) is not dict or set(p)!={'version','recoveries'} or type(p['version']) is not int or p['version']!=1 or type(p['recoveries']) is not list or len(p['recoveries'])>1:raise ValueError('Exact first context continuation only')
    for pin in p['recoveries']:
        if not shape(pin):raise ValueError('Historical recovery pin required')
    if v not in p['recoveries']:raise ValueError('Historical association not reviewed')


def rows(c):
    base._schema_bounds(c)
    found=c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name=? OR substr(name,1,?)=? OR tbl_name=?',(TABLE,len(TABLE)+1,TABLE+'_',TABLE)).fetchall()
    if not found:return []
    expected={('table',TABLE,TABLE,schema())}|{('trigger',k,TABLE,s) for k,s in guards().items()}|{('index','sqlite_autoindex_'+TABLE+'_'+str(i),TABLE,None) for i in (1,2)}
    if len(found)!=len(expected) or set(found)!=expected:raise ValueError('Migration disposition schema partial/conflicting')
    bound=c.execute('SELECT count(*),COALESCE(sum(length(CAST(payload AS BLOB))),0),COALESCE(max(length(CAST(payload AS BLOB))),0) FROM '+TABLE).fetchone()
    if not 1<=bound[0]<=MAX or bound[1]>2*1024*1024 or bound[2]>1024*1024:raise ValueError('Migration disposition bounds')
    bad=c.execute('SELECT 1 FROM '+TABLE+" WHERE typeof(dispatch_id)!='text' OR length(CAST(dispatch_id AS BLOB))!=32 OR typeof(scan_id)!='text' OR length(CAST(scan_id AS BLOB))!=32 OR typeof(payload)!='text' OR typeof(payload_hash)!='text' OR length(CAST(payload_hash AS BLOB))!=64 LIMIT 1").fetchone()
    if bad:raise ValueError('Migration disposition scalar types')
    values=[]
    for identity,scan,raw,key in c.execute('SELECT dispatch_id,scan_id,payload,payload_hash FROM '+TABLE):
        v=runtime._parse(raw)
        historical=shape(v)
        if identity!=v['dispatch_id'] or scan!=v['scan_id'] or canonical(v)!=raw or digest(v)!=key:raise ValueError('Migration disposition hash')
        if historical:approved(v)
        values.append(v)
    if sum(v['association']=='EXPLICIT_REVIEWED_LEGACY_SENTINEL' for v in values)>1:raise ValueError('Ambiguous context continuation')
    return values


def _journal(c):
    from tools import paper_entry_dispatcher as dispatcher
    values=dispatcher._read_journal(c)
    objects=c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master').fetchall()
    expected={('table',k,k,s) for k,s in dispatcher.SCHEMAS.items()}|{('trigger',k,s.split(' ON ')[1].split(' ')[0],s) for k,s in dispatcher._guards().items()}|{('index','sqlite_autoindex_intents_'+str(i),'intents',None) for i in (1,2,3)}|{('index','sqlite_autoindex_results_1','results',None)}
    if len(objects)!=len(expected) or set(objects)!=expected:raise ValueError('Exact original journal schema')
    result={}
    for table in dispatcher.SCHEMAS:
        raw=c.execute('SELECT rowid,* FROM '+table+' ORDER BY rowid').fetchall()
        if any(type(r[0]) is not int or (table=='context' and type(r[1]) is not int) or any(type(x) is not str for x in (r[2:] if table=='context' else r[1:])) for r in raw):raise ValueError('Journal row identities/types')
        result[table]=[list(r) for r in raw]
    return values,result


def _prefix(c,v,*,initial=False):
    values,actual=_journal(c);prefix=v['journal_prefix']
    if type(prefix) is not dict or set(prefix)!=set(actual) or len(prefix['context'])!=1:raise ValueError('Journal prefix shape')
    for table,old in prefix.items():
        if actual[table][:len(old)]!=old or (initial and actual[table]!=old):raise ValueError('Original journal prefix changed')
    original=[r for r in prefix['intents'] if r[1]==v['dispatch_id']]
    if len(original)!=1 or original[0][-1]!=v['intent_hash'] or any(r[1]==v['dispatch_id'] for r in actual['results']):raise ValueError('Historical unresolved intent changed')
    return values


def _decline(raw,hint,now,historical):
    from . import graduation_witness as g
    from .decode import decode
    from .programs import unbase58
    targets=[r for r in raw if r['transaction']['signatures'][0]==hint['signature'] and r['slot']==hint['slot']]
    if len(targets)!=1:raise ValueError('Exact target transaction required')
    decoded=decode(targets[0]);obs=decoded['program_observations']
    if decoded['status']!='OBSERVED':raise ValueError('Failed or unobserved migration')
    parents=[o for o in obs if o.get('program')==g.PUMP and o.get('name') in ('migrate','migrate_v2') and o.get('status')=='IDENTIFIED' and '.' not in o['instruction'] and o.get('mint')==hint['mint'] and o.get('pool')==hint['pool']]
    if len(parents)!=1:raise ValueError('Exact identified migration required')
    parent=parents[0];a=parent['accounts'];events=[o for o in obs if o.get('name')=='CompletePumpAmmMigrationEvent' and o.get('program')==g.PUMP and o.get('status')=='EVENT_DECODED' and o.get('schema_complete') is True and o['instruction'].split('.')[0]==parent['instruction']]
    if len(events)!=1:raise ValueError('Concrete decoded event required; absence alone is insufficient')
    event=events[0];f=event['fields'];mint=hint['mint'];pool=hint['pool']
    authority=g._pda([b'pool-authority',unbase58(mint)],g.PUMP);curve=g._pda([b'bonding-curve',unbase58(mint)],g.PUMP)
    # Generic account mismatches are never converted to a healthy disposition.
    if not all((a.get('pool_authority')==authority,a.get('mint',a.get('base_mint'))==mint,a.get('bonding_curve')==curve,a.get('pool')==pool,a.get('quote_mint',a.get('wsol_mint'))==g.SOL,a.get('program')==g.PUMP,f.get('mint')==mint,f.get('bonding_curve')==curve,f.get('pool')==pool,f.get('user')==a.get('user'))):raise ValueError('Non-quote account binding mismatch')
    if historical:
        if parent['name']!='migrate' or f.get('quote_mint')!=g.NATIVE_SOL_SENTINEL:raise ValueError('Exact historical legacy sentinel only')
        from . import _migration_decline_legacy_v1 as old
        if hashlib.sha256(Path(old.__file__).read_bytes()).hexdigest()!=LEGACY_HASH:raise ValueError('Historical verifier bytes changed')
        measured=old.extract_graduation(raw,mint=mint,pool=pool,now=now,provenance='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE')
        reason='HISTORICAL_LEGACY_SENTINEL_DECLINED'
    else:
        if f.get('quote_mint') in (g.SOL,g.NATIVE_SOL_SENTINEL):raise ValueError('Supported quote or unknown binding is not automatic rejection')
        measured=g.extract_graduation(raw,mint=mint,pool=pool,now=now,provenance='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE')
        reason='UNSUPPORTED_MIGRATION_EVENT_QUOTE'
    if measured['status']!='UNKNOWN' or measured['witnesses'] or measured['blockers']!=['MIGRATION_ACCOUNT_BINDING_MISMATCH','MIGRATION_WITNESS_ABSENT']:raise ValueError('Exact deterministic declined result required')
    return measured,reason


def _capture(store,progress,ctx,scan,hint,now,historical):
    from .job_persistence import JobPersistence
    from .ownership_acquisition import _Setup,_policy,_validate_binding
    from .paper_observation_collector import _admission,ObservationTarget
    from .migration_slot_intake import _hints
    from .replay_history import replay_history
    _hints(scan,hint['mint'],hint['pool'],hint['signature'],hint['slot'],'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE')
    jobs=JobPersistence.__new__(JobPersistence);jobs.path=Path(ctx['research_db'])
    def readonly():
        c=sqlite3.connect(jobs.path.as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row;return c
    jobs.connect=readonly
    from .paper_target_export import _bounded
    with closing(jobs.connect()) as c:
        c.execute('BEGIN');_bounded(c,'scan_jobs','descriptor','WHERE scan_id=?',(scan,),1,8192);_bounded(c,'scans','result','WHERE id=?',(scan,),1,2*1024*1024)
    from .paper_observation_collector import _Blocked
    try:_admission(jobs,progress,ObservationTarget(scan,hint['mint'],hint['pool'],hint['mint'],1))
    except _Blocked:raise ValueError('Persisted admission required') from None
    admission=progress.admission(scan);descriptor=jobs.descriptor(scan);source=jobs.source(scan)
    if admission['state']!='SEALED' or admission['prepared_requests_used']!=4 or admission['request_ceiling']!=18 or source!=admission['prepared_source'] or source['status']!='COMPLETE':raise ValueError('Exact completed four-charge acquisition required')
    report=runtime._parse(source['result'])
    if report.get('report_hash')!=digest({k:x for k,x in report.items() if k!='report_hash'}):raise ValueError('Original acquisition source hash')
    setup=_Setup.__new__(_Setup);setup.store=store;setup.descriptor=descriptor;setup.identity=scan;state=setup.read();_validate_binding(report,descriptor,state)
    mint_record=terminal._load(store,state['mint_hash']);cutoff_record=terminal._load(store,state['cutoff_hash'])
    if mint_record.get('method')!='getAccountInfo' or mint_record.get('params')!=[hint['mint'],{'encoding':'base64','commitment':'confirmed'}] or cutoff_record.get('method')!='getSlot' or cutoff_record.get('params')!=[{'commitment':'finalized'}]:raise ValueError('Exact canonical acquisition methods required')
    if _policy(state,mint=hint['mint'],token_profile_version=descriptor.get('paper_token_profile_version',0))['decision']!='PASS_TOKEN_POLICY' or hint['slot']>state['cutoff']:raise ValueError('Canonical mint/cutoff rejection')
    query={'address':hint['pool'],'start':0,'end':1,'token_accounts_filter':'none','slot_range':{'gte':hint['slot'],'lt':hint['slot']+1}}
    identity=digest({'budget':scan,'query':query});snapshot=progress.snapshot(identity);coverage=snapshot['coverage']
    if snapshot['query']!=query or snapshot['status']!='DONE' or not coverage or not coverage['query_coverage_verified'] or not coverage['query_range_exhausted'] or not 1<=snapshot['attempts']<=2 or snapshot['attempts']!=len(coverage['pages']) or admission['requests_used']!=4+snapshot['attempts']:raise ValueError('Complete finalized slot coverage required')
    replay_history(coverage,store)
    histories=base._histories(store,scan)
    if len(histories)!=1 or histories[0][1]!=identity:raise ValueError('Only exact intake reservation query permitted')
    seeds=report.get('history_queries')
    if type(seeds) is not list or len(seeds)!=1 or len(seeds[0]['pages'])!=2 or seeds[0]['address']!=hint['mint'] or seeds[0].get('slot_range')!={'gte':0,'lt':state['cutoff']+1}:raise ValueError('Canonical acquisition seed inventory')
    replay_history(seeds[0],store)
    attempts=base._attempts(store,scan)
    if [n for n,_,_ in attempts]!=list(range(5,admission['requests_used']+1)):raise ValueError('Complete raw intake attempt inventory required')
    raw=[]
    for (_,key,r),page in zip(attempts,coverage['pages']):
        req=terminal._load(store,page['request_evidence_hash']);response=terminal._load(store,page['payload_hash'])
        wire=terminal._wire(r['request_bytes_base64'],128*1024);captured=terminal._wire(r['response_bytes_base64'],2*1024*1024)
        options={'transactionDetails':'full','sortOrder':'asc','limit':100,'commitment':'finalized','encoding':'jsonParsed','maxSupportedTransactionVersion':1,'filters':{'slot':query['slot_range'],'status':'any','tokenAccounts':'none'}}
        if page['request_cursor']:options['paginationToken']=page['request_cursor']
        expected=[hint['pool'],options]
        if (r['method']!='getTransactionsForAddress' or r['params']!=expected or req['params']!=expected or r['failure_code'] is not None or type(r['observed_at']) is not int or not 0<=r['observed_at']<=now or type(r['http_status']) is not int or r['http_status']!=200 or r['source_id']!='helius-mainnet-paper-confirmed-v1' or wire!={'jsonrpc':'2.0','id':'paper-read-v1','method':r['method'],'params':expected} or captured!={'jsonrpc':'2.0','id':'paper-read-v1','result':response}):raise ValueError('Successful original finalized wire required')
        raw.extend(response['data'])
    measured,reason=_decline(raw,hint,now,historical)
    canonical_acquisition={'evidence_class':'CANONICAL_METHOD_PARAMS_RESULTS_NOT_ORIGINAL_WIRE','descriptor_hash':digest(descriptor),'source_hash':digest(source),'setup_hash':digest(state),'prepared_source_hash':admission['completed_source_hash']}
    return {'history_id':identity,'history_inventory_hash':digest(base._histories(store,scan)),'admission':admission,'canonical_acquisition':canonical_acquisition,'wire_attempt_refs':[key for _,key,_ in attempts],'migration':measured,'reason':reason}


def _progress(store):
    value=HistoryProgress.__new__(HistoryProgress);value.store=store;return value


def _receipt_history(c):
    from . import runtime_continuation as continuation
    result={}
    base._schema_bounds(c)
    for table in (runtime.TABLE,continuation.TABLE):
        present=c.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?',(table,)).fetchone()
        if not present:continue
        cols=[r[1] for r in c.execute('PRAGMA table_xinfo('+table+')')]
        if len(cols)>8 or any(x.casefold() in ('rowid','_rowid_','oid') for x in cols):raise ValueError('Runtime receipt identity')
        sizes='+'.join('COALESCE(length(CAST("'+x+'" AS BLOB)),0)' for x in cols)
        n,total,big=c.execute('SELECT count(*),COALESCE(sum('+sizes+'),0),COALESCE(max('+sizes+'),0) FROM '+table).fetchone()
        if n!=1 or total>65536 or big>65536:raise ValueError('Runtime receipt scalar bound')
        result[table]=c.execute('SELECT rowid,* FROM '+table).fetchall()
    return result


def proof(store,v,*,initial=False,cfg=None):
    historical=shape(v);ctx=v['context'];progress=_progress(store)
    if str(store.path)!=ctx['evidence_db']:raise ValueError('Exact evidence context')
    from tools import paper_entry_dispatcher as dispatcher
    path=dispatcher._path(v['producer_context']['journal'],private=True)
    if v['producer_context']['config_hash']!=v['config_hash'] or any(v['producer_context']['paths'][k]['path']!=ctx[k] for k in ctx):raise ValueError('Original context binding')
    if path.stat().st_size>dispatcher.MAX_JOURNAL or any(Path(str(path)+x).exists() for x in ('-wal','-shm','-journal')):raise ValueError('Journal interrupted')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN')
        values=_prefix(c,v,initial=initial) if historical else _journal(c)[0]
        intent=values['intents'].get(v['dispatch_id'])
        if not intent or digest(intent)!=v['intent_hash'] or intent['hint']!=v['hint'] or intent['context_hash']!=digest(v['producer_context']):raise ValueError('Exact original intent required')
    with closing(sqlite3.connect(Path(ctx['ledger_db']).as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN');_,saved_cfg=terminal._ledger(c,cfg,initial=initial)
        if digest(saved_cfg)!=v['config_hash'] or base._ledger_originals(c,prefix=True)!=v['ledger_prefix_hash']:raise ValueError('Ledger prefix/config changed')
        if initial and base._ledger_originals(c)!=v['ledger_original_hash']:raise ValueError('Ledger changed before publication')
        if historical and digest(_receipt_history(c))!=v['runtime_receipts_hash']:raise ValueError('Original runtime receipts changed')
    rebuilt=_capture(store,progress,ctx,v['scan_id'],v['hint'],v['evaluated_at'],historical)
    if any(v[k]!=x for k,x in rebuilt.items()):raise ValueError('Captured migration proof changed')
    with closing(store.connect()) as c:
        c.execute('BEGIN');terminal._monitoring(c,store=store,ledger=Path(ctx['ledger_db']),cfg=saved_cfg,pacing=ctx['pacing_db'])
    terminal._pacing(ctx['pacing_db'])
    with closing(sqlite3.connect(Path(ctx['pacing_db']).as_uri()+'?mode=ro',uri=True)) as c:
        if c.execute('SELECT 1 FROM waiters LIMIT 1').fetchone():raise ValueError('Pacing waiters pending')
    if historical:
        from . import paper_dispatch_preparation_retirement as parent
        with closing(store.connect()) as c:matches=[p for p in parent.rows(c) if digest(p)==v['parent_receipt_hash']]
        if len(matches)!=1 or matches[0]['successor_context']!=v['producer_context'] or matches[0]['journal_path']!=str(path):raise ValueError('Exact original lineage parent required')
        old=matches[0]
        original=runtime._parse(v['journal_prefix']['context'][0][2])
        if digest(original)!=old['dispatch_context_hash']:raise ValueError('Original journal context changed')
        wanted={**v['producer_context'],**{k:v['successor_context'].get(k) for k in ('source_hash','tool_hash','entry_tool_hash')}}
        if wanted!=v['successor_context'] or v['successor_context']['source_hash']!=v['source_hash']:raise ValueError('Only explicit source/tool context continuation')
        manager=terminal._load(store,v['stopped_witness_hash'])
        expected={'kind':'migration_dispatch_stopped_review_v1','dispatch_id':v['dispatch_id'],'dispatch_intent_hash':v['intent_hash'],'producer_source_hash':v['producer_context']['source_hash'],'exit_status':2,'service_active':False,'timer_active':False,'observed_at':manager.get('observed_at')}
        if manager!=expected or type(manager['observed_at']) is not int or not v['evaluated_at']<=manager['observed_at']<2**63:raise ValueError('Exact ended service witness required')
    return v


def _make(store,c,cfg,producer,identity,scan,*,historical=False,backup=None,stopped=None):
    from tools import paper_entry_dispatcher as dispatcher
    values,prefix=_journal(c)
    intent=values['intents'].get(identity)
    if not intent or identity in values['results']:raise ValueError('Exact unresolved intent required')
    now=int(intent['at'])
    attempts=base._attempts(store,scan)
    if attempts:now=max(now,*[r['observed_at'] for _,_,r in attempts])
    ctx={k:producer['paths'][k]['path'] for k in ('research_db','evidence_db','ledger_db','pacing_db')}
    captured=_capture(store,_progress(store),ctx,scan,intent['hint'],now,historical)
    with closing(sqlite3.connect(Path(ctx['ledger_db']).as_uri()+'?mode=ro',uri=True)) as ledger:
        ledger.execute('BEGIN');terminal._ledger(ledger,cfg,initial=True)
        original=base._ledger_originals(ledger);initial_prefix=base._ledger_originals(ledger,prefix=True);receipt_history=_receipt_history(ledger)
    parent_hash=None;successor=None;backup_hash=None
    if historical:
        from . import paper_dispatch_preparation_retirement as parent
        with closing(store.connect()) as evidence:parents=parent.rows(evidence)
        matches=[p for p in parents if p['successor_context']==producer and p['journal_path']==producer['journal']]
        if len(matches)!=1:raise ValueError('Exact original retirement parent')
        parent_hash=digest(matches[0]);successor=dispatcher.plan(**{k:x['path'] for k,x in producer['paths'].items()},journal=producer['journal'],taker=producer['taker'],amount_raw=producer['amount_raw'],pool_fee_bps=producer['pool_fee_bps'])
        p=Path(backup)
        if not p.is_absolute() or not p.is_file() or p.resolve(strict=True)!=p or p.samefile(ctx['ledger_db']) or p.stat().st_size>32*1024*1024:raise ValueError('Bounded distinct original ledger backup')
        with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)) as saved:
            saved.execute('BEGIN')
            if base._ledger_originals(saved)!=original or _receipt_history(saved)!=receipt_history:raise ValueError('Ledger originals/first/continuation differ from pre-attempt backup')
        backup_hash=hashlib.sha256(p.read_bytes()).hexdigest()
    v={'version':1,'kind':'dispatcher_migration_no_entry_v1','association':'EXPLICIT_REVIEWED_LEGACY_SENTINEL' if historical else 'CAPTURED_UNSUPPORTED_QUOTE',
       'dispatch_id':identity,'scan_id':scan,'intent_hash':digest(intent),'hint':intent['hint'],'context':ctx,'config_hash':digest(cfg),
       'source_hash':runtime.implementation_hash(),'producer_context':producer,'successor_context':successor,'journal_prefix':prefix,
       'parent_receipt_hash':parent_hash,**captured,'evaluated_at':now,'ledger_original_hash':original,'ledger_prefix_hash':initial_prefix,
       'ledger_backup_hash':backup_hash,'stopped_witness_hash':stopped,'runtime_receipts_hash':digest(receipt_history),
       'entry_authorized':False,'execution_status':'EXECUTION_UNVERIFIED','no_retry':True}
    proof(store,v,initial=True,cfg=cfg)
    # Validate every completed old record using the normal dispatcher verifier;
    # this exact target is the only newly proved unresolved exception.
    retired={}
    if values['context']!={1:producer}:
        from .paper_dispatch_preparation_retirement import lineage as original_lineage
        retired=original_lineage(producer,values['context'][1])
    dispatcher._check_records(c,producer,values,retired|{identity:intent['context_hash']},retired|{identity:intent['context_hash']})
    if terminal.gate(store,Path(ctx['research_db']),(),ledger_locked=ctx['ledger_db']) is not None:raise ValueError('Other observation work unresolved')
    return v


def _insert(store,v):
    store.read_only=False
    with closing(store.connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            existing=rows(c)
            for old in existing:
                if old['dispatch_id']==v['dispatch_id']:
                    if old!=v:raise ValueError('Conflicting disposition replay')
                    c.rollback();return 'ALREADY_RECORDED'
                if old['scan_id']==v['scan_id']:raise ValueError('Already retired scan')
            if not existing:
                c.execute(schema())
                for sql in guards().values():c.execute(sql)
            c.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?)',(v['dispatch_id'],v['scan_id'],canonical(v),digest(v)))
            import zlib
            marker={'kind':'migration_disposition_publication_v1','dispatch_id':v['dispatch_id'],'scan_id':v['scan_id'],'receipt_hash':digest(v)}
            raw=canonical(marker).encode();key=digest(marker);compressed=zlib.compress(raw)
            old=c.execute('SELECT payload,raw_bytes FROM pages WHERE hash=?',(key,)).fetchone()
            if old is None:
                used=c.execute('SELECT COALESCE(sum(length(payload)),0) FROM pages').fetchone()[0]
                if used+len(compressed)>store.max_bytes:raise ValueError('Evidence storage bound')
                c.execute('INSERT INTO pages VALUES(?,?,?)',(key,compressed,len(raw)))
            elif terminal._load(store,key)!=marker:raise ValueError('Publication marker conflict')
            rows(c);c.commit()
        except BaseException:c.rollback();raise
    return 'RECORDED'


def _locks(ctx,journal,*,owned_journal=False):
    from .paper_observe_cli import _worker_lock
    from .paper_cycle import _lock
    stack=ExitStack()
    try:
        if stack.enter_context(_worker_lock(ctx['research_db'])) is None:raise ValueError('Research busy')
        for path in (ctx['evidence_db']+'.ownership-invocation.lock',ctx['ledger_db']+'.paper-cycle.lock'):
            if not stack.enter_context(_lock(path)):raise ValueError('Context busy')
        if not owned_journal and not stack.enter_context(_lock(journal+'.dispatcher.lock')):raise ValueError('Dispatcher busy')
        runtime.require_transition_context(ctx['research_db'],ctx['evidence_db'],ctx['ledger_db'],reviewed_context={k:ctx[k] for k in ('research_db','evidence_db','ledger_db')})
        return stack
    except BaseException:stack.close();raise


def review_plan(producer,dispatch_id,scan_id,*,ledger_backup,stopped_witness_hash,apply=False):
    ctx={k:producer['paths'][k]['path'] for k in ('research_db','evidence_db','ledger_db','pacing_db')}
    store=EvidenceStore(ctx['evidence_db'],read_only=True)
    from .paper_cycle_cli import _config
    from tools import paper_entry_dispatcher as dispatcher
    cfg=_config(producer['paths']['config']['path'])
    with _locks(ctx,producer['journal']),closing(sqlite3.connect(Path(producer['journal']).as_uri()+'?mode=ro',uri=True)) as journal:
        journal.execute('BEGIN')
        v=_make(store,journal,cfg,producer,dispatch_id,scan_id,historical=True,backup=ledger_backup,stopped=stopped_witness_hash)
        if not apply:return v
        approved(v)
        fresh=_make(store,journal,cfg,producer,dispatch_id,scan_id,historical=True,backup=ledger_backup,stopped=stopped_witness_hash)
        if fresh!=v:raise ValueError('Recovery changed before publication')
        status=_insert(store,v)
        return {'status':status,'receipt_hash':digest(v),'retired_scan':scan_id,'entry_authorized':False}


def publish(c,producer,identity,scan):
    ctx={k:producer['paths'][k]['path'] for k in ('research_db','evidence_db','ledger_db','pacing_db')}
    store=EvidenceStore(ctx['evidence_db'],read_only=True)
    from .paper_cycle_cli import _config
    cfg=_config(producer['paths']['config']['path'])
    with _locks(ctx,producer['journal'],owned_journal=True):
        v=_make(store,c,cfg,producer,identity,scan)
        _insert(store,v)
    return {**v,'evidence_hash':digest(v)}


def verify(store,result):
    key=result.get('evidence_hash');v={k:x for k,x in result.items() if k!='evidence_hash'}
    if digest(v)!=key:raise ValueError('Migration disposition result hash')
    with closing(store.connect()) as c:
        records=rows(c)
    if v not in records:raise ValueError('Migration disposition publication incomplete')
    proof(store,v)
    return v


def gate(store,research,scan_ids,*,ledger_locked=None):
    from .paper_cycle import _lock
    with closing(store.connect()) as c:records=rows(c)
    if not records:
        # Absence must not erase a committed disposition. Streaming, bounded
        # original-page inspection is needed only when the table is absent.
        with closing(store.connect()) as c:
            c.execute('BEGIN')
            bad=c.execute("SELECT 1 FROM pages WHERE typeof(hash)!='text' OR length(CAST(hash AS BLOB))!=64 OR typeof(payload)!='blob' OR length(payload)>? OR typeof(raw_bytes)!='integer' OR raw_bytes NOT BETWEEN 1 AND ? LIMIT 1",(terminal.MAX_PROOF_COMPRESSED_BYTES,terminal.MAX_PROOF_RAW_BYTES)).fetchone()
            n,total=c.execute('SELECT count(*),COALESCE(sum(raw_bytes),0) FROM pages').fetchone()
            if bad or n>base.MAX_INVENTORY_PAGES or total>base.MAX_INVENTORY_BYTES:raise ValueError('Disposition artifact bounds')
            keys=[r[0] for r in c.execute('SELECT hash FROM pages')]
        for key in keys:
            value=terminal._load(store,key)
            if type(value) is dict and value.get('kind')=='migration_disposition_publication_v1':raise ValueError('Disposition table missing after publication')
    for v in records:
        marker={'kind':'migration_disposition_publication_v1','dispatch_id':v['dispatch_id'],'scan_id':v['scan_id'],'receipt_hash':digest(v)}
        if terminal._load(store,digest(marker))!=marker:raise ValueError('Disposition publication incomplete')
        if v['context']['research_db']!=str(research) or v['context']['evidence_db']!=str(store.path):raise ValueError('Migration gate context')
        with ExitStack() as stack:
            if ledger_locked!=v['context']['ledger_db'] and not stack.enter_context(_lock(v['context']['ledger_db']+'.paper-cycle.lock')):raise ValueError('Migration ledger busy')
            proof(store,v)
    if any(v['scan_id'] in scan_ids for v in records):return 'REJECTED_SCAN_RETIRED'
    return None


def lineage(c,expected,original):
    store=EvidenceStore(expected['paths']['evidence_db']['path'],read_only=True)
    with closing(store.connect()) as evidence:certs=[v for v in rows(evidence) if v['association']=='EXPLICIT_REVIEWED_LEGACY_SENTINEL' and v['producer_context']['journal']==expected['journal']]
    if not certs:return None
    if len(certs)!=1:raise ValueError('Ambiguous context continuation')
    v=certs[0]
    if expected!=v['successor_context']:raise ValueError('Exact reviewed successor context required')
    proof(store,v)
    values=_prefix(c,v)
    if original!=runtime._parse(v['journal_prefix']['context'][0][2]):raise ValueError('Original context prefix conflict')
    from .paper_dispatch_preparation_retirement import lineage as parent_lineage
    retired=parent_lineage(v['producer_context'],original)
    binding={r[1]:runtime._parse(r[-2])['context_hash'] for r in v['journal_prefix']['intents']}
    permitted=set(retired.values())|{digest(v['producer_context'])}
    if any(value not in permitted for value in binding.values()):raise ValueError('Unknown historical intent context')
    return binding,retired|{v['dispatch_id']:digest(v['producer_context'])}


def main(argv=None):
    import argparse,json
    p=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    for k in ('producer-context','dispatch-id','scan-id','ledger-backup','stopped-witness-hash'):p.add_argument('--'+k,required=True)
    p.add_argument('--apply',action='store_true');a=p.parse_args(argv)
    try:
        with Path(a.producer_context).open('rb') as f:raw=f.read(65537)
        if len(raw)>65536:raise ValueError('Producer context byte bound')
        value=review_plan(runtime._parse(raw.decode()),a.dispatch_id,a.scan_id,ledger_backup=a.ledger_backup,stopped_witness_hash=a.stopped_witness_hash,apply=a.apply)
        print(json.dumps(value,sort_keys=True));return 0
    except (ValueError,TypeError,KeyError,IndexError,AttributeError,OSError,sqlite3.Error,OverflowError,RecursionError):
        print(json.dumps({'status':'BLOCKED','blockers':['MIGRATION_RECOVERY_UNPROVED'],'entry_authorized':False}));return 2

if __name__=='__main__':raise SystemExit(main())
