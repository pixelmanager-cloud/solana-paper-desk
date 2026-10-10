"""Explicit reviewed no-retry retirement of one incomplete dispatcher history preparation.

Original intent has no ledger/source anchors: association is independent operator
review, never intrinsic proof. No pass updates, history advancement or requests.
"""
import base64
import hashlib
import math
from contextlib import closing, ExitStack
from pathlib import Path
import sqlite3
from . import runtime_compatibility as runtime, paper_terminal_reconciliation as terminal
from .model import canonical, digest
from .evidence import EvidenceStore
from .history_progress import HistoryProgress
from . import paper_preparation_retirement as base

POLICY=Path(__file__).resolve().parents[1]/'config/paper-dispatch-preparation-retirement.json'
TABLE='paper_dispatch_preparation_retirements'
MAX_RECEIPTS=32
MAX_INVENTORY_PAGES=4096
MAX_INVENTORY_BYTES=256*1024*1024
FIELDS={'version','association','pass_id','intent_hash','dispatch_id','dispatch_intent_hash','dispatch_context_hash','journal_path','journal_original_hash','attempt_refs',
        'source_hash','producer_source_hash','context','config_hash','scan_id','before','after',
        'history_id','history_inventory_hash','ledger_anchors','ledger_original_hash','ledger_prefix_hash','ledger_backup_hash','failure','no_retry','successor_context','stopped_witness_hash'}


def _schema():
    return f'CREATE TABLE {TABLE}(pass_id TEXT PRIMARY KEY,scan_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,payload_hash TEXT NOT NULL)'


def _guards():
    result={TABLE+'_insert':f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN (SELECT COUNT(*) FROM {TABLE})>={MAX_RECEIPTS} OR EXISTS(SELECT 1 FROM {TABLE} WHERE pass_id=NEW.pass_id OR scan_id=NEW.scan_id) BEGIN SELECT RAISE(ABORT,'Preparation retirement capacity'); END"}
    return result|{TABLE+'_'+a.lower():f"CREATE TRIGGER {TABLE}_{a.lower()} BEFORE {a} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Preparation retirement immutable'); END" for a in ('UPDATE','DELETE')}


def shape(v):
    return (type(v) is dict and set(v)==FIELDS and type(v['version']) is int and v['version']==1
        and v['association']=='EXPLICIT_REVIEWED_DISPATCH_PREPARATION_UNCAPTURED' and terminal._id(v['pass_id'])
        and terminal._id(v['scan_id']) and terminal._id(v['dispatch_id']) and type(v['journal_path']) is str and Path(v['journal_path']).is_absolute() and str(Path(v['journal_path']).resolve())==v['journal_path'] and all(runtime._hash(v[k]) for k in
            ('intent_hash','dispatch_intent_hash','dispatch_context_hash','journal_original_hash','stopped_witness_hash','source_hash','producer_source_hash','config_hash','history_id','history_inventory_hash','ledger_original_hash','ledger_prefix_hash','ledger_backup_hash'))
        and type(v['before']) is int and v['before']==6 and type(v['after']) is int and v['after']==12
        and type(v['successor_context']) is dict and v['successor_context'].get('source_hash')==v['source_hash'] and v['successor_context'].get('journal')==v['journal_path']
        and v['failure']=='RESERVATION_OUTCOME_UNCAPTURED' and v['no_retry'] is True
        and type(v['attempt_refs']) is list and 1<=len(v['attempt_refs'])<=11
        and all(runtime._hash(x) for x in v['attempt_refs']) and len(set(v['attempt_refs']))==len(v['attempt_refs'])
        and type(v['context']) is dict and set(v['context'])=={'research_db','evidence_db','ledger_db','pacing_db'}
        and runtime._context_shape({k:v['context'][k] for k in ('research_db','evidence_db','ledger_db')})
        and type(v['context']['pacing_db']) is str and str(Path(v['context']['pacing_db']).resolve())==v['context']['pacing_db']
        and type(v['ledger_anchors']) is dict and set(v['ledger_anchors'])==terminal.ANCHORS
        and all(runtime._hash(x) for x in v['ledger_anchors'].values()))


def approved(v):
    with POLICY.open('rb') as f:raw=f.read(65537)
    if len(raw)>65536:raise ValueError('Preparation policy bound')
    p=runtime._parse(raw.decode())
    if type(p) is not dict or set(p)!={'version','retirements'} or type(p['version']) is not int or p['version']!=1 or type(p['retirements']) is not list or len(p['retirements'])>MAX_RECEIPTS:raise ValueError('Preparation policy shape')
    ids=set();scans=set()
    for pin in p['retirements']:
        if not shape(pin):raise ValueError('Preparation pin malformed')
        identity=(pin['context']['evidence_db'],pin['pass_id']);scan=(pin['context']['evidence_db'],pin['scan_id'])
        if identity in ids or scan in scans:raise ValueError('Preparation pin conflict')
        ids.add(identity);scans.add(scan)
    if v not in p['retirements']:raise ValueError('Preparation association not independently reviewed')


def rows(c):
    base._schema_bounds(c)
    objects=c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name=? OR substr(name,1,?)=? OR tbl_name=?",(TABLE,len(TABLE)+1,TABLE+'_',TABLE)).fetchall()
    if not objects:return []
    expected={('table',TABLE,TABLE,_schema())}|{('trigger',k,TABLE,s) for k,s in _guards().items()}
    expected|={('index',f'sqlite_autoindex_{TABLE}_{i}',TABLE,None) for i in (1,2)}
    if len(objects)!=len(expected) or set(objects)!=expected or dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(TABLE,)))!=_guards():raise ValueError('Preparation schema/guards malformed')
    n=c.execute('SELECT count(*) FROM '+TABLE).fetchone()[0]
    if not 1<=n<=MAX_RECEIPTS:raise ValueError('Preparation receipt count')
    bounds=c.execute('SELECT typeof(pass_id),length(CAST(pass_id AS BLOB)),typeof(scan_id),length(CAST(scan_id AS BLOB)),typeof(payload),length(CAST(payload AS BLOB)),typeof(payload_hash),length(CAST(payload_hash AS BLOB)) FROM '+TABLE).fetchall()
    if any(r[:5]!=('text',32,'text',32,'text') or not 0<r[5]<=65536 or r[6:]!=('text',64) for r in bounds):raise ValueError('Preparation receipt scalar bound')
    result=[]
    for identity,scan,raw,key in c.execute('SELECT * FROM '+TABLE):
        v=runtime._parse(raw)
        if not shape(v) or v['pass_id']!=identity or v['scan_id']!=scan or canonical(v)!=raw or digest(v)!=key:raise ValueError('Preparation receipt binding')
        approved(v);result.append(v)
    return result


def _attempts(store,scan,*,intent_hash=None):
    # Compressed pages have no kind/scan index. A bounded whole-set scalar
    # preflight precedes streaming decompression; unrelated pages are not retained.
    with closing(store.connect()) as c:
        c.execute('BEGIN')
        invalid=c.execute("SELECT 1 FROM pages WHERE typeof(hash)!='text' OR length(CAST(hash AS BLOB))!=64 OR typeof(payload)!='blob' OR length(payload) NOT BETWEEN 1 AND ? OR typeof(raw_bytes)!='integer' OR CASE WHEN typeof(raw_bytes)='integer' THEN raw_bytes NOT BETWEEN 1 AND ? ELSE 1 END LIMIT 1",(terminal.MAX_PROOF_COMPRESSED_BYTES,terminal.MAX_PROOF_RAW_BYTES)).fetchone()
        if invalid:raise ValueError('Preparation attempt inventory scalar bound')
        bounds=c.execute('SELECT count(*),COALESCE(sum(raw_bytes),0),COALESCE(sum(length(payload)+length(CAST(hash AS BLOB))),0) FROM pages').fetchone()
        if bounds[0]>MAX_INVENTORY_PAGES or bounds[1]>MAX_INVENTORY_BYTES or bounds[2]>MAX_INVENTORY_BYTES:raise ValueError('Preparation attempt inventory bound')
        keys=[r[0] for r in c.execute('SELECT hash FROM pages ORDER BY hash')]
    result=[]
    for key in keys:
        value=terminal._load(store,key)
        if intent_hash is not None and type(value) is dict and value.get('kind')=='history_first_paper_preparation_outcome_v1' and value.get('intent_hash')==intent_hash:raise ValueError('Conflicting completed preparation outcome')
        if type(value) is dict and value.get('kind')=='paper_read_attempt_v1' and value.get('scan_id')==scan:
            used=value.get('requests_used')
            if type(used) is not int or not 1<=used<=11:raise ValueError('Preparation attempt charge bound')
            result.append((used,key,value))
    result.sort(key=lambda r:r[:2])
    if len(result)>11 or len({r[0] for r in result})!=len(result):raise ValueError('Preparation duplicate attempt charge')
    return result



def _journal(v,*,initial=False):
    from tools import paper_entry_dispatcher as dispatch
    path=dispatch._path(v['journal_path'],private=True)
    if path.stat().st_size>dispatch.MAX_JOURNAL or any(Path(str(path)+x).exists() for x in ('-wal','-shm','-journal')):raise ValueError('Original dispatcher storage')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN');base._schema_bounds(c)
        if dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))!=dispatch.SCHEMAS or dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))!=dispatch._guards() or c.execute('PRAGMA journal_mode').fetchone()!=('delete',):raise ValueError('Original dispatcher schema')
        objects=set(c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master'))
        expected={('table',k,k,sql) for k,sql in dispatch.SCHEMAS.items()}
        expected|={('trigger',k,sql.split(' ON ')[1].split(' ')[0],sql) for k,sql in dispatch._guards().items()}
        expected|={('index','sqlite_autoindex_intents_'+str(i),'intents',None) for i in (1,2,3)}|{('index','sqlite_autoindex_results_1','results',None)}
        if objects!=expected:raise ValueError('Exact dispatcher schema inventory')
        inventory=[];values={}
        for table in dispatch.SCHEMAS:
            cols=[x[1] for x in c.execute('PRAGMA table_xinfo('+table+')')]
            if any(x.casefold() in ('rowid','_rowid_','oid') for x in cols):raise ValueError('Journal row identity')
            sizes='+'.join('COALESCE(length(CAST("'+x+'" AS BLOB)),0)' for x in cols)
            n,total,big=c.execute('SELECT count(*),COALESCE(sum('+sizes+'),0),COALESCE(max('+sizes+'),0) FROM '+table).fetchone()
            if n>(1 if table=='context' else 4096) or total>16*1024*1024 or big>65536:raise ValueError('Journal scalar bound')
            records=c.execute('SELECT rowid,* FROM '+table+' ORDER BY rowid').fetchall()
            # Exact supported scalar types, including original hidden row identity.
            for r in records:
                if type(r[0]) is not int or (table=='context' and type(r[1]) is not int) or any(type(x) is not str for x in r[2:] if table=='context') or (table!='context' and any(type(x) is not str for x in r[1:])):raise ValueError('Journal scalar type')
            inventory.append([table,records]);values[table]=records
        original=[r for r in values['intents'] if r[1]==v['dispatch_id']]
        if len(values['context'])!=1 or len(original)!=1 or any(r[1]==v['dispatch_id'] for r in values['results']) or (initial and (values['results'] or len(values['intents'])!=1)):raise ValueError('Exact original unresolved dispatcher required')
        inventory=[['context',values['context']],['intents',original],['results',[]]]
        context=runtime._parse(values['context'][0][2]);row=original[0]
        intent=runtime._parse(row[-2])
        if values['context'][0][1]!=1 or digest(context)!=values['context'][0][3] or canonical(context)!=values['context'][0][2] or digest(intent)!=row[-1] or canonical(intent)!=row[-2]:raise ValueError('Journal original hashes')
        if row[1]!=v['dispatch_id'] or row[-1]!=v['dispatch_intent_hash'] or digest(context)!=v['dispatch_context_hash'] or digest(inventory)!=v['journal_original_hash']:raise ValueError('Journal reviewed binding')
        if set(intent)!={'version','context_hash','at','hint'} or type(intent['version']) is not int or intent['version']!=1 or intent['context_hash']!=digest(context):raise ValueError('Dispatcher intent grammar')
        if context['source_hash']!=v['producer_source_hash'] or context['config_hash']!=v['config_hash'] or context['journal']!=str(path):raise ValueError('Historical dispatcher source/config')
        for k,p in v['context'].items():
            if context['paths'][k]['path']!=p:raise ValueError('Dispatcher path association')
        hint=intent['hint']
        if type(intent['at']) not in (int,float) or not math.isfinite(intent['at']) or type(hint) is not dict or set(hint)!={'seq','payload_hash','raw_hash','received_at','mint','pool','signature','slot'} or type(hint['seq']) is not int or hint['seq']<=0 or type(hint['received_at']) not in (int,float) or not math.isfinite(hint['received_at']) or not 300<=intent['at']-hint['received_at']<=7200 or not all(runtime._hash(hint[k]) for k in ('payload_hash','raw_hash')):raise ValueError('Original dispatcher hint grammar')
        from .migration_slot_intake import _hints
        _hints('retirement-review',hint['mint'],hint['pool'],hint['signature'],hint['slot'],'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE')
        if intent['hint']['mint']!=row[2] or intent['hint']['signature']!=row[3]:raise ValueError('Dispatcher target association')
        return context,intent


def proof(store,progress,v,cfg,*,current_budget):
    if not shape(v):raise ValueError('Dispatch preparation shape')
    with closing(store.connect()) as c:
        c.execute('BEGIN');terminal._passes(c)
        if c.execute('SELECT intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?',(v['pass_id'],)).fetchone()!=(v['intent_hash'],None):raise ValueError('Original preparation NULL required')
    intent=terminal._load(store,v['intent_hash'])
    if type(intent) is not dict or set(intent)!={'kind','config_hash','target','admission'} or intent['kind']!='history_first_paper_preparation_v1' or intent['config_hash']!=digest(cfg) or digest(cfg)!=v['config_hash']:raise ValueError('Original preparation intent')
    from .paper_cycle import CycleTarget
    from .paper_observation_collector import ObservationTarget
    from dataclasses import asdict
    item=intent['target'];target=ObservationTarget(**item['target']);bound=CycleTarget(target,**{k:x for k,x in item.items() if k!='target'})
    if asdict(bound)!=item or target.scan_id!=v['scan_id'] or type(target.amount_raw) is not int or not 0<target.amount_raw<2**64 or bound.known_hazards or bound.provenance!='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE' or type(bound.history_as_of) is not int or not 300<=bound.history_as_of<2**63:raise ValueError('Original target binding')
    from .programs import address
    for key in (target.mint,target.pool,target.taker):address(key)
    ctx,dispatch=_journal(v,initial=current_budget)
    wanted=dict(ctx);wanted.update({k:v['successor_context'].get(k) for k in ('source_hash','tool_hash','entry_tool_hash')})
    if wanted!=v['successor_context']:raise ValueError('Only explicit source/tool lineage permitted')
    witness=terminal._load(store,v['stopped_witness_hash'])
    expected={'kind':'dispatcher_stopped_review_v1','dispatch_id':v['dispatch_id'],'dispatch_intent_hash':v['dispatch_intent_hash'],'producer_source_hash':v['producer_source_hash'],'exit_status':2,'service_active':False,'timer_active':False,'observed_at':witness.get('observed_at')}
    if witness!=expected or type(witness['observed_at']) is not int or witness['observed_at']<=dispatch['at']:raise ValueError('Exact independent ended-invocation witness required')
    if (ctx['taker'],ctx['amount_raw'],ctx['pool_fee_bps'])!=(target.taker,target.amount_raw,bound.pool_fee_bps) or (dispatch['hint']['mint'],dispatch['hint']['pool'])!=(target.mint,target.pool):raise ValueError('Dispatcher/preparation target mismatch')
    before=intent['admission'];after=progress.admission(v['scan_id'])
    if type(before) is not dict or before.get('state')!='SEALED' or type(before.get('requests_used')) is not int or before['requests_used']!=6 or before.get('request_ceiling')!=18 or before['descriptor']['mint']!=target.mint or after!={**before,'requests_used':12}:raise ValueError('Exact unchanged 12/18 admission')
    query={'address':target.pool,'start':bound.history_as_of-300,'end':bound.history_as_of+1,'token_accounts_filter':'none','page_size':50}
    if digest({'budget':v['scan_id'],'query':query})!=v['history_id']:raise ValueError('History query identity')
    histories=base._histories(store,v['scan_id'])
    if digest(histories)!=v['history_inventory_hash']:raise ValueError('Original history inventory changed')
    found=[r for r in histories if r[1]==v['history_id']]
    if len(found)!=1 or found[0][2]!=v['scan_id'] or found[0][3]!=canonical(query) or found[0][5:]!=('RETRYABLE_ERROR',6) or found[0][4] is None:raise ValueError('Exact sixth unresolved reservation')
    coverage=runtime._parse(found[0][4])
    if len(coverage['pages'])!=5 or coverage.get('exhausted') or not coverage.get('next_cursor'):raise ValueError('Exact incomplete five-page history')
    from .replay_history import replay_history
    replay_history(coverage,store)
    attempts=_attempts(store,v['scan_id'],intent_hash=v['intent_hash'])
    if [x[1] for x in attempts]!=v['attempt_refs'] or [x[0] for x in attempts]!=[5,6,7,8,9,10,11]:raise ValueError('Complete retained attempt inventory; reservation12 must remain uncaptured')
    prior=None
    for charge,key,r in attempts:
        if charge<7:continue
        options={'transactionDetails':'full','sortOrder':'asc','limit':50,'commitment':'finalized','encoding':'jsonParsed','maxSupportedTransactionVersion':1,'filters':{'blockTime':{'gte':query['start'],'lt':query['end']},'status':'any','tokenAccounts':'none'}}
        if prior:options['paginationToken']=prior
        params=[target.pool,options];body=canonical({'jsonrpc':'2.0','id':'paper-read-v1','method':'getTransactionsForAddress','params':params}).encode()
        if r['method']!='getTransactionsForAddress' or r['params']!=params or r['source_id']!='helius-mainnet-paper-confirmed-v1' or r['failure_code'] is not None or type(r['http_status']) is not int or r['http_status']!=200 or type(r['observed_at']) is not int or r['request_bytes_base64']!=base64.b64encode(body).decode():raise ValueError('Original successful history transport')
        if set(r)!={'kind','scan_id','requests_used','source_id','method','params','request_bytes_base64','response_bytes_base64','observed_at','http_status','failure_code'} or not bound.history_as_of<=r['observed_at']<=witness['observed_at']:raise ValueError('Captured attempt grammar/time')
        response=terminal._wire(r['response_bytes_base64'],16*1024*1024)
        if response.get('jsonrpc')!='2.0' or response.get('id')!='paper-read-v1' or set(response)!={'jsonrpc','id','result'}:raise ValueError('History response envelope')
        data=response['result'];page=coverage['pages'][charge-7]
        if len(data['data'])!=50 or digest(data)!=page['payload_hash'] or not data.get('paginationToken'):raise ValueError('Exact full nonterminal page')
        prior=data['paginationToken']
    with closing(sqlite3.connect(Path(v['context']['ledger_db']).as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN')
        if base._ledger_originals(c,prefix=not current_budget)!=v['ledger_original_hash' if current_budget else 'ledger_prefix_hash']:raise ValueError('Ledger originals changed')

def plan(research_db,evidence_db,ledger_db,cfg,*,pass_id,dispatch_id,pacing_db,producer_source_hash,ledger_backup,journal_path,stopped_witness_hash):
    context=terminal._context(research_db,evidence_db,ledger_db,pacing_db)
    if not terminal._id(pass_id) or not terminal._id(dispatch_id) or not runtime._hash(producer_source_hash) or pacing_db is None:raise ValueError('Exact preparation identities required')
    store=EvidenceStore(context['evidence_db'],read_only=True);progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
    with closing(store.connect()) as c:
        terminal._passes(c);row=c.execute('SELECT intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?',(pass_id,)).fetchone()
    if row is None or row[1] is not None:raise ValueError('Original preparation NULL required')
    intent=terminal._load(store,row[0]);scan=intent['target']['target']['scan_id']
    as_of=intent['target']['history_as_of'];pool=intent['target']['target']['pool']
    query={'address':pool,'start':as_of-300,'end':as_of+1,'token_accounts_filter':'none','page_size':50}
    backup=Path(ledger_backup)
    if not backup.is_absolute() or not backup.is_file() or backup.resolve(strict=True)!=backup or backup.samefile(context['ledger_db']) or backup.stat().st_size>32*1024*1024:raise ValueError('Distinct bounded canonical pre-attempt ledger backup required')
    with closing(sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN');backup_anchors,_=terminal._ledger(c,cfg,initial=True,historical_source=producer_source_hash);original_hash=base._ledger_originals(c);prefix_hash=base._ledger_originals(c,prefix=True)
    with closing(sqlite3.connect(Path(context['ledger_db']).as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN');anchors,_=terminal._ledger(c,cfg,initial=True,historical_source=producer_source_hash)
        if anchors!=backup_anchors or base._ledger_originals(c)!=original_hash:raise ValueError('Ledger differs from original pre-attempt backup')
    v={'version':1,'association':'EXPLICIT_REVIEWED_DISPATCH_PREPARATION_UNCAPTURED','pass_id':pass_id,'intent_hash':row[0],'dispatch_id':dispatch_id,'journal_path':str(Path(journal_path).resolve()),
       'attempt_refs':[r[1] for r in _attempts(store,scan,intent_hash=row[0])],'source_hash':runtime.implementation_hash(),'producer_source_hash':producer_source_hash,
       'context':context,'config_hash':digest(cfg),'scan_id':scan,'before':6,'after':12,'history_id':digest({'budget':scan,'query':query}),
       'history_inventory_hash':digest(base._histories(store,scan)),'ledger_anchors':anchors,'ledger_original_hash':original_hash,'ledger_prefix_hash':prefix_hash,'ledger_backup_hash':hashlib.sha256(backup.read_bytes()).hexdigest(),'failure':'RESERVATION_OUTCOME_UNCAPTURED','no_retry':True,'stopped_witness_hash':stopped_witness_hash,'successor_context':{}}
    v.update(_journal_pin(v))
    from tools import paper_entry_dispatcher as dispatcher
    old,_=_journal(v,initial=True)
    v['successor_context']=dispatcher.plan(**{k:x['path'] for k,x in old['paths'].items()},journal=old['journal'],taker=old['taker'],amount_raw=old['amount_raw'],pool_fee_bps=old['pool_fee_bps'])
    proof(store,progress,v,cfg,current_budget=True);terminal._pacing(context['pacing_db'])
    with closing(store.connect()) as c:
        pending=c.execute('SELECT id,intent_hash FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchall()
        certified=terminal._rows(c)
        from .paper_http403_retirement import rows as http_rows
        certified+=http_rows(c)+base.rows(c)+rows(c)
        identities={(x['pass_id'],x['intent_hash']) for x in certified}|{(pass_id,row[0])}
        if any(tuple(r) not in identities for r in pending):raise ValueError('Unrelated pending pass')
        terminal._monitoring(c,store=store,ledger=Path(context['ledger_db']),cfg=cfg,pacing=context['pacing_db'])
    with closing(sqlite3.connect(Path(context['pacing_db']).as_uri()+'?mode=ro',uri=True)) as c:
        if c.execute('SELECT 1 FROM waiters LIMIT 1').fetchone():raise ValueError('Pacing waiter pending')
    return v


def _locked(function,research_db,evidence_db,ledger_db,cfg,**args):
    from .paper_cycle import _lock
    from .paper_observe_cli import _worker_lock
    context=terminal._context(research_db,evidence_db,ledger_db,args.get('pacing_db'))
    with ExitStack() as locks:
        if locks.enter_context(_worker_lock(context['research_db'])) is None:raise ValueError('Research busy')
        for path in (context['evidence_db']+'.ownership-invocation.lock',context['ledger_db']+'.paper-cycle.lock'):
            if not locks.enter_context(_lock(path)):raise ValueError('Preparation context busy')
        if not locks.enter_context(_lock(str(Path(args['journal_path']).resolve())+'.dispatcher.lock')):raise ValueError('Dispatcher busy')
        runtime.require_transition_context(research_db,evidence_db,ledger_db,reviewed_context={k:context[k] for k in ('research_db','evidence_db','ledger_db')})
        return function(research_db,evidence_db,ledger_db,cfg,**args)


def review_plan(*args,**kw):return _locked(plan,*args,**kw)


def _append(research_db,evidence_db,ledger_db,cfg,**args):
    v=plan(research_db,evidence_db,ledger_db,cfg,**args);approved(v)
    store=EvidenceStore(v['context']['evidence_db'],read_only=True);store.read_only=False
    with closing(store.connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            fresh=plan(research_db,evidence_db,ledger_db,cfg,**args)
            if fresh!=v:raise ValueError('Preparation changed before append')
            approved(fresh);old=rows(c)
            from .paper_http403_retirement import rows as http_rows
            if any(x['pass_id']==v['pass_id'] or x['scan_id']==v['scan_id'] for x in terminal._rows(c)+http_rows(c)+base.rows(c)):raise ValueError('Conflicting retirement family')
            for x in old:
                if x['pass_id']==v['pass_id']:
                    if x!=v:raise ValueError('Conflicting preparation replay')
                    c.rollback();return {'status':'ALREADY_RECORDED','receipt_hash':digest(v),'retired_scan':v['scan_id'],'entry_authorized':False}
                if x['scan_id']==v['scan_id']:raise ValueError('Retired scan conflict')
            if not old:
                c.execute(_schema())
                for sql in _guards().values():c.execute(sql)
            c.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?)',(v['pass_id'],v['scan_id'],canonical(v),digest(v)))
            rows(c);c.commit()
        except BaseException:c.rollback();raise
    return {'status':'RECORDED','receipt_hash':digest(v),'retired_scan':v['scan_id'],'entry_authorized':False}


def reconcile(*args,**kw):return _locked(_append,*args,**kw)


def _journal_pin(v):
    from tools import paper_entry_dispatcher as dispatch
    path=dispatch._path(v['journal_path'],private=True)
    if path.stat().st_size>dispatch.MAX_JOURNAL:raise ValueError('Journal size')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN');base._schema_bounds(c);inventory=[];context=None;intent_hash=None
        for table in dispatch.SCHEMAS:
            cols=[x[1] for x in c.execute('PRAGMA table_xinfo('+table+')')]
            if not cols or len(cols)>6:raise ValueError('Journal columns')
            sizes='+'.join('COALESCE(length(CAST("'+x.replace('"','""')+'" AS BLOB)),0)' for x in cols)
            n,total,big=c.execute('SELECT count(*),COALESCE(sum('+sizes+'),0),COALESCE(max('+sizes+'),0) FROM '+table).fetchone()
            if n>1 or total>65536 or big>65536:raise ValueError('First dispatch journal bound')
            records=c.execute('SELECT rowid,* FROM '+table+' ORDER BY rowid').fetchall();inventory.append([table,records])
            if table=='context' and len(records)==1:context=records[0][-1]
            if table=='intents' and len(records)==1 and records[0][1]==v['dispatch_id']:intent_hash=records[0][-1]
        if not runtime._hash(context) or not runtime._hash(intent_hash):raise ValueError('Journal hashes')
        return {'dispatch_intent_hash':intent_hash,'dispatch_context_hash':context,'journal_original_hash':digest(inventory)}


def main(argv=None):
    import argparse,json
    from .paper_cycle_cli import _config
    p=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    for key in ('research-db','evidence-db','ledger-db','config','pass-id','dispatch-id','pacing-db','producer-source-hash','ledger-backup','journal-path','stopped-witness-hash'):p.add_argument('--'+key,required=True)
    mode=p.add_mutually_exclusive_group();mode.add_argument('--plan',action='store_true');mode.add_argument('--apply',action='store_true')
    a=p.parse_args(argv)
    try:
        value=(reconcile if a.apply else review_plan)(a.research_db,a.evidence_db,a.ledger_db,_config(a.config),**{k:getattr(a,k) for k in ('pass_id','dispatch_id','pacing_db','producer_source_hash','ledger_backup','journal_path','stopped_witness_hash')})
        print(json.dumps(value,sort_keys=True));return 0
    except (ValueError,TypeError,KeyError,IndexError,OSError,sqlite3.Error,AttributeError):
        print(json.dumps({'status':'BLOCKED','blockers':['DISPATCH_PREPARATION_RETIREMENT_UNPROVED'],'entry_authorized':False}));return 2

if __name__=='__main__':raise SystemExit(main())


def lineage(expected,original):
    """One explicit reviewed context edge; never rewrite the journal context."""
    store=EvidenceStore(expected['paths']['evidence_db']['path'],read_only=True)
    with closing(store.connect()) as c:c.execute('BEGIN');matches=[v for v in rows(c) if v['journal_path']==expected['journal']]
    if len(matches)!=1:raise ValueError('Explicit dispatcher lineage missing/ambiguous')
    v=matches[0]
    if digest(original)!=v['dispatch_context_hash'] or expected!=v['successor_context']:raise ValueError('Exact successor dispatcher context required')
    if terminal.gate(store,Path(v['context']['research_db']),(v['scan_id'],))!='REJECTED_SCAN_RETIRED':raise ValueError('Dispatcher intent not retired')
    return {v['dispatch_id']:v['dispatch_context_hash']}
