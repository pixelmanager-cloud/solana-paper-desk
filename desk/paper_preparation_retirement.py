"""Explicit reviewed no-retry retirement of one oversized history preparation.

Original intent has no ledger/source anchors: association is independent operator
review, never intrinsic proof. No pass updates, history advancement or requests.
"""
import base64
import hashlib
from contextlib import closing, ExitStack
from pathlib import Path
import sqlite3
from . import runtime_compatibility as runtime, paper_terminal_reconciliation as terminal
from .model import canonical, digest
from .evidence import EvidenceStore
from .history_progress import HistoryProgress

POLICY=Path(__file__).resolve().parents[1]/'config/paper-preparation-retirement.json'
TABLE='paper_preparation_retirements'
MAX_RECEIPTS=32
MAX_INVENTORY_PAGES=4096
MAX_INVENTORY_BYTES=256*1024*1024
FIELDS={'version','association','pass_id','intent_hash','failed_attempt_hash','attempt_refs',
        'source_hash','producer_source_hash','context','config_hash','scan_id','before','after',
        'history_id','history_inventory_hash','ledger_anchors','ledger_original_hash','ledger_prefix_hash','ledger_backup_hash','failure','no_retry'}


def _schema():
    return f'CREATE TABLE {TABLE}(pass_id TEXT PRIMARY KEY,scan_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,payload_hash TEXT NOT NULL)'


def _guards():
    result={TABLE+'_insert':f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN (SELECT COUNT(*) FROM {TABLE})>={MAX_RECEIPTS} OR EXISTS(SELECT 1 FROM {TABLE} WHERE pass_id=NEW.pass_id OR scan_id=NEW.scan_id) BEGIN SELECT RAISE(ABORT,'Preparation retirement capacity'); END"}
    return result|{TABLE+'_'+a.lower():f"CREATE TRIGGER {TABLE}_{a.lower()} BEFORE {a} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Preparation retirement immutable'); END" for a in ('UPDATE','DELETE')}


def shape(v):
    return (type(v) is dict and set(v)==FIELDS and type(v['version']) is int and v['version']==1
        and v['association']=='EXPLICIT_REVIEWED_PREPARATION_OVERSIZE' and terminal._id(v['pass_id'])
        and terminal._id(v['scan_id']) and all(runtime._hash(v[k]) for k in
            ('intent_hash','failed_attempt_hash','source_hash','producer_source_hash','config_hash','history_id','history_inventory_hash','ledger_original_hash','ledger_prefix_hash','ledger_backup_hash'))
        and type(v['before']) is int and v['before']==6 and type(v['after']) is int and v['after']==7
        and v['failure']=='RESPONSE_OVERSIZED' and v['no_retry'] is True
        and type(v['attempt_refs']) is list and 1<=len(v['attempt_refs'])<=7
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


def _schema_bounds(c):
    # Scalar-only inventory bounds before any schema SQL or column name fetch.
    bad=c.execute("SELECT 1 FROM sqlite_master WHERE typeof(type)!='text' OR length(CAST(type AS BLOB)) NOT BETWEEN 1 AND 16 OR typeof(name)!='text' OR length(CAST(name AS BLOB)) NOT BETWEEN 1 AND 256 OR typeof(tbl_name)!='text' OR length(CAST(tbl_name AS BLOB)) NOT BETWEEN 1 AND 256 OR (typeof(sql)!='null' AND typeof(sql)!='text') LIMIT 1").fetchone()
    bounds=c.execute('SELECT count(*),COALESCE(sum(length(CAST(type AS BLOB))+length(CAST(name AS BLOB))+length(CAST(tbl_name AS BLOB))+COALESCE(length(CAST(sql AS BLOB)),0)),0) FROM sqlite_master').fetchone()
    if bad or bounds[0]>256 or bounds[1]>1024*1024:raise ValueError('Preparation schema scalar bound')


def rows(c):
    _schema_bounds(c)
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


def _histories(store,scan):
    with closing(store.connect()) as c:
        c.execute('BEGIN')
        _schema_bounds(c)
        expected='CREATE TABLE ownership_history(id TEXT PRIMARY KEY,budget TEXT NOT NULL,query TEXT NOT NULL,coverage TEXT,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0)'
        if c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='ownership_history'").fetchone()!=(expected,):raise ValueError('Preparation history schema')
        # Includes original row identity and canonical strings, not lossy summaries.
        invalid=c.execute("SELECT 1 FROM ownership_history WHERE budget=? AND (typeof(id)!='text' OR length(CAST(id AS BLOB))!=64 OR typeof(budget)!='text' OR length(CAST(budget AS BLOB))!=32 OR typeof(query)!='text' OR length(CAST(query AS BLOB)) NOT BETWEEN 1 AND 1048576 OR (typeof(coverage)!='null' AND (typeof(coverage)!='text' OR length(CAST(coverage AS BLOB)) NOT BETWEEN 1 AND 1048576)) OR typeof(status)!='text' OR length(CAST(status AS BLOB)) NOT BETWEEN 1 AND 64 OR typeof(attempts)!='integer' OR CASE WHEN typeof(attempts)='integer' THEN attempts NOT BETWEEN 0 AND 18 ELSE 1 END) LIMIT 1",(scan,)).fetchone()
        if invalid:raise ValueError('Preparation history scalar bound')
        bounds=c.execute('SELECT count(*),COALESCE(sum(length(CAST(id AS BLOB))+length(CAST(budget AS BLOB))+length(CAST(query AS BLOB))+COALESCE(length(CAST(coverage AS BLOB)),0)+length(CAST(status AS BLOB))),0) FROM ownership_history WHERE budget=?',(scan,)).fetchone()
        if bounds[0]>18 or bounds[1]>2*1024*1024:raise ValueError('Preparation history bound')
        return c.execute('SELECT rowid,id,budget,query,coverage,status,attempts FROM ownership_history WHERE budget=? ORDER BY id',(scan,)).fetchall()


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
        kind,scan_id,original_intent=terminal._classification(store,key)
        if intent_hash is not None and kind=='history_first_paper_preparation_outcome_v1' and original_intent==intent_hash:raise ValueError('Conflicting completed preparation outcome')
        if kind=='paper_read_attempt_v1' and scan_id==scan:
            value=terminal._load(store,key)
            used=value.get('requests_used')
            if type(used) is not int or not 1<=used<=7:raise ValueError('Preparation attempt charge bound')
            result.append((used,key,value))
    result.sort(key=lambda r:r[:2])
    if len(result)>7 or len({r[0] for r in result})!=len(result):raise ValueError('Preparation duplicate attempt charge')
    return result



def _ledger_originals(c,*,prefix=False):
    native={'metadata','events','outcomes','state','raw_events','health','sqlite_sequence'}
    from . import runtime_continuation, runtime_extensions
    extras={runtime.TABLE,runtime_continuation.TABLE,runtime_extensions.TABLE}
    _schema_bounds(c)
    bounds=c.execute('SELECT count(*),COALESCE(sum(length(CAST(name AS BLOB))+length(CAST(tbl_name AS BLOB))+COALESCE(length(CAST(sql AS BLOB)),0)),0) FROM sqlite_master').fetchone()
    if bounds[0]>256 or bounds[1]>1024*1024:raise ValueError('Preparation ledger schema bound')
    schema=c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
    if {r[1] for r in schema if r[0]=='table'}-extras != native or any(r[2] not in native|extras for r in schema):raise ValueError('Preparation ledger schema inventory')
    original=[r for r in schema if r[2] in native]
    records=[];total=0
    for table in sorted(native):
        q='"'+table+'"'
        cols=[r[1] for r in c.execute('PRAGMA table_xinfo('+q+')')]
        if not 1<=len(cols)<=64 or any(n.casefold() in ('rowid','_rowid_','oid') for n in cols):raise ValueError('Preparation native row identity unavailable')
        fields=','.join('"'+n.replace('"','""')+'"' for n in cols)
        sizes='+'.join('COALESCE(length(CAST("'+n.replace('"','""')+'" AS BLOB)),0)' for n in cols)
        bound=c.execute('SELECT count(*),COALESCE(sum('+sizes+'),0),COALESCE(max('+sizes+'),0) FROM '+q).fetchone()
        total+=bound[1]
        if bound[0]>10000 or total>32*1024*1024 or bound[2]>16*1024*1024:raise ValueError('Preparation ledger row bound')
        types=','.join('typeof("'+n.replace('"','""')+'")' for n in cols)
        where=" WHERE seq<=1" if prefix and table=='events' else ' WHERE 0' if prefix and table in ('state','sqlite_sequence','outcomes','raw_events','health') else ''
        for row in c.execute('SELECT rowid,'+fields+','+types+' FROM '+q+where+' ORDER BY rowid'):
            records.append([table,*[{'blob':base64.b64encode(x).decode()} if type(x) is bytes else x for x in row]])
    return digest({'schema':original,'rows':records})

def proof(store,progress,v,cfg,*,current_budget):
    if not shape(v):raise ValueError('Preparation proof shape')
    with closing(store.connect()) as c:
        terminal._passes(c)
        if c.execute('SELECT intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?',(v['pass_id'],)).fetchone()!=(v['intent_hash'],None):raise ValueError('Original preparation NULL required')
    intent=terminal._load(store,v['intent_hash'])
    if type(intent) is not dict or set(intent)!={'kind','config_hash','target','admission'} or intent['kind']!='history_first_paper_preparation_v1' or intent['config_hash']!=v['config_hash'] or digest(cfg)!=v['config_hash']:raise ValueError('Exact original preparation intent required')
    from .paper_cycle import CycleTarget
    from .paper_observation_collector import ObservationTarget
    from dataclasses import asdict
    item=intent['target']
    if type(item) is not dict or type(item.get('target')) is not dict:raise ValueError('Preparation target shape')
    target=ObservationTarget(**item['target']);bound=CycleTarget(target,**{k:x for k,x in item.items() if k!='target'})
    if asdict(bound)!=item or target.scan_id!=v['scan_id'] or type(target.amount_raw) is not int or not 0<target.amount_raw<2**64 or bound.known_hazards or bound.provenance!='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE' or type(bound.history_as_of) is not int or not 300<=bound.history_as_of<2**63:raise ValueError('Preparation original target binding')
    from .programs import address
    for key in (target.mint,target.pool,target.taker):address(key)
    before=intent['admission'];after=progress.admission(v['scan_id'])
    if type(before) is not dict or before.get('state')!='SEALED' or before.get('requests_used')!=6 or type(before.get('requests_used')) is not int or before.get('request_ceiling')!=18 or before['descriptor']['mint']!=target.mint or after!={**before,'requests_used':7}:raise ValueError('Preparation exact sealed admission/charge binding')
    query={'address':target.pool,'start':bound.history_as_of-300,'end':bound.history_as_of+1,'token_accounts_filter':'none'}
    if digest({'budget':v['scan_id'],'query':query})!=v['history_id']:raise ValueError('Preparation query identity')
    histories=_histories(store,v['scan_id'])
    if digest(histories)!=v['history_inventory_hash']:raise ValueError('Preparation query/reservation inventory changed')
    found=[r for r in histories if r[1]==v['history_id']]
    if len(found)!=1 or found[0][2:]!=(v['scan_id'],canonical(query),None,'RETRYABLE_ERROR',1):raise ValueError('Exact initial failed preparation reservation required')
    attempts=_attempts(store,v['scan_id'],intent_hash=v['intent_hash'])
    if [r[1] for r in attempts]!=v['attempt_refs'] or not attempts or attempts[-1][:2]!=(7,v['failed_attempt_hash']):raise ValueError('Complete original preparation attempt inventory differs')
    record=attempts[-1][2]
    fields={'kind','scan_id','requests_used','source_id','method','params','request_bytes_base64','response_bytes_base64','observed_at','http_status','failure_code'}
    options={'transactionDetails':'full','sortOrder':'asc','limit':100,'commitment':'finalized','encoding':'jsonParsed','maxSupportedTransactionVersion':1,'filters':{'blockTime':{'gte':query['start'],'lt':query['end']},'status':'any','tokenAccounts':'none'}}
    params=[target.pool,options]
    body=canonical({'jsonrpc':'2.0','id':'paper-read-v1','method':'getTransactionsForAddress','params':params}).encode()
    if set(record)!=fields or record['method']!='getTransactionsForAddress' or record['params']!=params or record['source_id']!='helius-mainnet-paper-confirmed-v1' or record['failure_code']!='RESPONSE_OVERSIZED' or type(record['http_status']) is not int or record['http_status']!=200 or record['response_bytes_base64'] is not None or record['observed_at'] is not None or record['request_bytes_base64']!=base64.b64encode(body).decode():raise ValueError('Exact original oversize transport failure required')
    with closing(sqlite3.connect(Path(v['context']['ledger_db']).as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN')
        if _ledger_originals(c,prefix=not current_budget)!=v['ledger_original_hash' if current_budget else 'ledger_prefix_hash']:raise ValueError('Preparation ledger originals changed')
    # Missing response/time remain missing; failure is not complete history proof.
    return None


def plan(research_db,evidence_db,ledger_db,cfg,*,pass_id,failed_attempt_hash,pacing_db,producer_source_hash,ledger_backup):
    context=terminal._context(research_db,evidence_db,ledger_db,pacing_db)
    if not terminal._id(pass_id) or not runtime._hash(failed_attempt_hash) or not runtime._hash(producer_source_hash) or pacing_db is None:raise ValueError('Exact preparation identities required')
    store=EvidenceStore(context['evidence_db'],read_only=True);progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
    with closing(store.connect()) as c:
        terminal._passes(c);row=c.execute('SELECT intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?',(pass_id,)).fetchone()
    if row is None or row[1] is not None:raise ValueError('Original preparation NULL required')
    intent=terminal._load(store,row[0]);scan=intent['target']['target']['scan_id']
    as_of=intent['target']['history_as_of'];pool=intent['target']['target']['pool']
    query={'address':pool,'start':as_of-300,'end':as_of+1,'token_accounts_filter':'none'}
    backup=Path(ledger_backup)
    if not backup.is_absolute() or not backup.is_file() or backup.resolve(strict=True)!=backup or backup.samefile(context['ledger_db']) or backup.stat().st_size>32*1024*1024:raise ValueError('Distinct bounded canonical pre-attempt ledger backup required')
    with closing(sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN');backup_anchors,_=terminal._ledger(c,cfg,initial=True,historical_source=producer_source_hash);original_hash=_ledger_originals(c);prefix_hash=_ledger_originals(c,prefix=True)
    with closing(sqlite3.connect(Path(context['ledger_db']).as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN');anchors,_=terminal._ledger(c,cfg,initial=True,historical_source=producer_source_hash)
        if anchors!=backup_anchors or _ledger_originals(c)!=original_hash:raise ValueError('Ledger differs from original pre-attempt backup')
    v={'version':1,'association':'EXPLICIT_REVIEWED_PREPARATION_OVERSIZE','pass_id':pass_id,'intent_hash':row[0],'failed_attempt_hash':failed_attempt_hash,
       'attempt_refs':[r[1] for r in _attempts(store,scan,intent_hash=row[0])],'source_hash':runtime.implementation_hash(),'producer_source_hash':producer_source_hash,
       'context':context,'config_hash':digest(cfg),'scan_id':scan,'before':6,'after':7,'history_id':digest({'budget':scan,'query':query}),
       'history_inventory_hash':digest(_histories(store,scan)),'ledger_anchors':anchors,'ledger_original_hash':original_hash,'ledger_prefix_hash':prefix_hash,'ledger_backup_hash':hashlib.sha256(backup.read_bytes()).hexdigest(),'failure':'RESPONSE_OVERSIZED','no_retry':True}
    proof(store,progress,v,cfg,current_budget=True);terminal._pacing(context['pacing_db'])
    return v


def _locked(function,research_db,evidence_db,ledger_db,cfg,**args):
    from .paper_cycle import _lock
    from .paper_observe_cli import _worker_lock
    context=terminal._context(research_db,evidence_db,ledger_db,args.get('pacing_db'))
    with ExitStack() as locks:
        if locks.enter_context(_worker_lock(context['research_db'])) is None:raise ValueError('Research busy')
        for path in (context['evidence_db']+'.ownership-invocation.lock',context['ledger_db']+'.paper-cycle.lock'):
            if not locks.enter_context(_lock(path)):raise ValueError('Preparation context busy')
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
            if any(x['pass_id']==v['pass_id'] or x['scan_id']==v['scan_id'] for x in terminal._rows(c)+http_rows(c)):raise ValueError('Conflicting retirement family')
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


def main(argv=None):
    import argparse,json
    from .paper_cycle_cli import _config
    p=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    for key in ('research-db','evidence-db','ledger-db','config','pass-id','failed-attempt-hash','pacing-db','producer-source-hash','ledger-backup'):p.add_argument('--'+key,required=True)
    mode=p.add_mutually_exclusive_group();mode.add_argument('--plan',action='store_true');mode.add_argument('--apply',action='store_true')
    a=p.parse_args(argv)
    try:
        fn=reconcile if a.apply else review_plan
        value=fn(a.research_db,a.evidence_db,a.ledger_db,_config(a.config),pass_id=a.pass_id,failed_attempt_hash=a.failed_attempt_hash,pacing_db=a.pacing_db,producer_source_hash=a.producer_source_hash,ledger_backup=a.ledger_backup)
        print(json.dumps(value,sort_keys=True));return 0
    except (ValueError,TypeError,KeyError,IndexError,OSError,sqlite3.Error,AttributeError):
        print(json.dumps({'status':'BLOCKED','blockers':['PREPARATION_RETIREMENT_UNPROVED'],'entry_authorized':False}));return 2

if __name__=='__main__':raise SystemExit(main())
