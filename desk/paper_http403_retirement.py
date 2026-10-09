"""Exact reviewed operator retirement of one closed Price-V3 HTTP403 pass.

No response body was retained on urllib HTTPError. This certifies neither the
reason for HTTP403 nor token safety. Original NULL pass and charges are retained.
"""
import base64
from contextlib import closing, ExitStack
from pathlib import Path
import sqlite3
from urllib.parse import urlencode

from . import paper_terminal_reconciliation as terminal, runtime_compatibility as runtime
from .model import canonical, digest
from .providers import SOL

POLICY = Path(__file__).resolve().parents[1] / 'config/paper-http403-retirement.json'
TABLE = 'paper_http403_retirements'
FIELDS = {'version','association','pass_id','intent_hash','outcome_hash','attempt_refs',
          'source_hash','context','config_hash','scan_id','before','after','ledger_anchors',
          'response_body_unavailable','failed_attempt_binding','failure'}
ATTEMPT_FIELDS = {'kind','scan_id','requests_used','source_id','method','params',
                  'request_bytes_base64','response_bytes_base64','observed_at','http_status','failure_code'}


def _shape(v):
    return (type(v) is dict and set(v)==FIELDS and type(v['version']) is int and v['version']==1
        and v['association']=='EXPLICIT_REVIEWED_HTTP403' and terminal._id(v['pass_id'])
        and type(v['scan_id']) is str and 1<=len(v['scan_id'])<=256
        and all(runtime._hash(v[k]) for k in ('intent_hash','outcome_hash','source_hash','config_hash'))
        and type(v['attempt_refs']) is list and len(v['attempt_refs'])==5
        and all(runtime._hash(k) for k in v['attempt_refs']) and len(set(v['attempt_refs']))==5
        and type(v['before']) is int and type(v['after']) is int and 0<=v['before']<v['after']<=18
        and v['after']==v['before']+5 and v['response_body_unavailable'] is True
        and v['failed_attempt_binding']=='EXPLICIT_REVIEWED_NOT_INTRINSIC'
        and v['failure']=='PRICE_V3_HTTP403_ACCESS_FAILURE'
        and type(v['context']) is dict and set(v['context'])=={'research_db','evidence_db','ledger_db','pacing_db'}
        and runtime._context_shape({k:v['context'][k] for k in ('research_db','evidence_db','ledger_db')})
        and type(v['context']['pacing_db']) is str and str(Path(v['context']['pacing_db']).resolve())==v['context']['pacing_db']
        and type(v['ledger_anchors']) is dict and set(v['ledger_anchors'])==terminal.ANCHORS
        and all(runtime._hash(k) for k in v['ledger_anchors'].values()))


def _approved(v):
    with POLICY.open('rb') as f:raw=f.read(terminal.MAX_BYTES+1)
    if len(raw)>terminal.MAX_BYTES:raise ValueError('HTTP403 policy byte bound')
    p=runtime._parse(raw.decode())
    if (type(p) is not dict or set(p)!={'version','associations'} or type(p['version']) is not int or p['version']!=1
            or type(p['associations']) is not list or len(p['associations'])>32):raise ValueError('HTTP403 policy malformed')
    identities=set();found=False
    for pin in p['associations']:
        if not _shape(pin):raise ValueError('HTTP403 pin malformed')
        identity=(pin['context']['evidence_db'],pin['pass_id'])
        if identity in identities:raise ValueError('HTTP403 pin conflicting')
        identities.add(identity);found |= pin==v
    if not found:raise ValueError('HTTP403 association not explicitly reviewed')


def _schema():
    return f'CREATE TABLE {TABLE}(pass_id TEXT PRIMARY KEY,scan_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,payload_hash TEXT NOT NULL)'


def _guards():
    return {TABLE+'_insert':f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN (SELECT COUNT(*) FROM {TABLE})>=32 OR EXISTS(SELECT 1 FROM {TABLE} WHERE pass_id=NEW.pass_id OR scan_id=NEW.scan_id) BEGIN SELECT RAISE(ABORT,'HTTP403 retirement capacity'); END"} | {
        TABLE+'_'+a.lower():f"CREATE TRIGGER {TABLE}_{a.lower()} BEFORE {a} ON {TABLE} BEGIN SELECT RAISE(ABORT,'HTTP403 retirement immutable'); END" for a in ('UPDATE','DELETE')}


def rows(c):
    objects=list(c.execute("SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'paper_http403_%'"))
    if not objects:return []
    expected={('table',TABLE):_schema()} | {('trigger',k):v for k,v in _guards().items()}
    if ({(a,b):s for a,b,s in objects}!=expected or dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(TABLE,)))!=_guards()):
        raise ValueError('HTTP403 schema/guards malformed')
    if not 1<=c.execute(f'SELECT COUNT(*) FROM {TABLE}').fetchone()[0]<=32:raise ValueError('HTTP403 count/partial publication')
    shapes=c.execute(f'SELECT typeof(pass_id),typeof(scan_id),typeof(payload),typeof(payload_hash),length(CAST(pass_id AS BLOB)),length(CAST(scan_id AS BLOB)),length(CAST(payload AS BLOB)),length(CAST(payload_hash AS BLOB)) FROM {TABLE}').fetchall()
    if any(r[:4]!=('text',)*4 or r[4]!=32 or not 1<=r[5]<=256 or not 0<r[6]<=terminal.MAX_BYTES or r[7]!=64 for r in shapes):raise ValueError('HTTP403 scalar bound')
    result=[]
    for identity,scan,payload,key in c.execute(f'SELECT pass_id,scan_id,payload,payload_hash FROM {TABLE}'):
        v=runtime._parse(payload)
        if not _shape(v) or v['pass_id']!=identity or v['scan_id']!=scan or digest(v)!=key:raise ValueError('HTTP403 receipt malformed')
        _approved(v);result.append(v)
    return result


def proof(store,progress,v,cfg,*,current_budget):
    if not _shape(v):raise ValueError('HTTP403 proof shape')
    with closing(store.connect()) as c:
        terminal._passes(c)
        if c.execute('SELECT intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?',(v['pass_id'],)).fetchone()!=(v['intent_hash'],None):raise ValueError('Original NULL pass required')
    intent=terminal._load(store,v['intent_hash']);outcome=terminal._load(store,v['outcome_hash']);scan=v['scan_id']
    if (type(intent) is not dict or intent.get('kind')!='paper_cycle_intent_v1'
            or intent.get('config_hash')!=v['config_hash'] or intent.get('source_hash')!=v['source_hash']
            or intent.get('terminal_context')!=v['context'] or intent.get('ledger_anchors')!=v['ledger_anchors']
            or intent.get('ledger')!=v['context']['ledger_db'] or type(intent.get('targets')) is not list or len(intent['targets'])!=1
            or type(intent.get('admissions')) is not dict or set(intent['admissions'])!={scan}):raise ValueError('HTTP403 exact intent/context required')
    target=intent['targets'][0]['target'];before=intent['admissions'][scan];after=progress.admission(scan)
    if (target['scan_id']!=scan or type(before.get('requests_used')) is not int or before['requests_used']!=v['before']
            or before.get('request_ceiling')!=18 or before.get('state')!='SEALED'
            or before['descriptor']['mint']!=target['mint'] or after is None
            or after!={**before,'requests_used':v['after']}):raise ValueError('HTTP403 exact unchanged admission/charges required')
    fields={'attempt_refs','attempted_requests','blockers','budget','diagnostics','events','execution_status','intent_hash',
            'investigation_attempted_requests','kind','live_readiness','monitoring_attempted_requests','outcomes','pass_id','status','usd_evidence_refs'}
    if (type(outcome) is not dict or set(outcome)!=fields or outcome['kind']!='paper_cycle_v1' or outcome['status']!='BLOCKED'
            or outcome['blockers']!=['HTTP_REJECTED'] or outcome['execution_status']!='EXECUTION_UNVERIFIED'
            or outcome['live_readiness'] is not False or outcome['events']!=[] or outcome['outcomes']!=[] or outcome['usd_evidence_refs']!=[]
            or any(type(outcome[k]) is not int or outcome[k]!=n for k,n in (('attempted_requests',5),('investigation_attempted_requests',5),('monitoring_attempted_requests',0)))
            or outcome['pass_id']!=v['pass_id'] or outcome['intent_hash']!=v['intent_hash']
            or outcome['attempt_refs']!=v['attempt_refs'][:4] or outcome['budget']!={scan:{'ceiling':18,'used':v['after']}}):raise ValueError('HTTP403 exact persisted non-entry result required')
    diagnostics=outcome['diagnostics']
    if (type(diagnostics) is not list or len(diagnostics)!=1 or set(diagnostics[0])!={'scan_id','graduation'}
            or diagnostics[0]['scan_id']!=scan or diagnostics[0]['graduation'].get('status')!='OBSERVED_MIGRATION'
            or diagnostics[0]['graduation'].get('entry_authorized') is not False or diagnostics[0]['graduation'].get('blockers')!=[]):raise ValueError('HTTP403 pre-entry result required')
    records=[terminal._load(store,k) for k in v['attempt_refs']]
    for i,r in enumerate(records):
        if (type(r) is not dict or set(r)!=ATTEMPT_FIELDS or r['kind']!='paper_read_attempt_v1' or r['scan_id']!=scan
                or type(r['requests_used']) is not int or r['requests_used']!=v['before']+i+1):raise ValueError('HTTP403 complete five-charge inventory required')
    failed=records[-1]
    if (type(failed['http_status']) is not int or failed['http_status']!=403 or failed['failure_code']!='HTTP_REJECTED'
            or failed['method']!='jupiter_price_v3' or failed['source_id']!='jupiter-price-v3-sol-paper-v1'
            or failed['params']!={'ids':SOL} or failed['request_bytes_base64']!=base64.b64encode(urlencode({'ids':SOL}).encode('ascii')).decode()
            or failed['response_bytes_base64'] is not None or failed['observed_at'] is not None):raise ValueError('Only retained body-unavailable Price HTTP403 supported')
    successful=records[:4];responses=[]
    for i,r in enumerate(successful):
        if (type(r['http_status']) is not int or r['http_status']!=200 or r['failure_code'] is not None
                or type(r['observed_at']) is not int or not 0<=r['observed_at']<2**63):raise ValueError('Successful retained preceding transport required')
        if i<3:
            if r['source_id']!='helius-mainnet-paper-confirmed-v1':raise ValueError('RPC source mismatch')
            request=terminal._wire(r['request_bytes_base64'],32768);response=terminal._wire(r['response_bytes_base64'],2*1024*1024)
            if request!={'id':'paper-read-v1','jsonrpc':'2.0','method':r['method'],'params':r['params']} or set(response)!={'id','jsonrpc','result'} or response['id']!='paper-read-v1' or response['jsonrpc']!='2.0':raise ValueError('RPC request/response mismatch')
            from . import original_byte_read_transport as syntax
            syntax._bounded_json(response)
            syntax._result({'method':r['method'],'params':r['params']},response['result'])
            responses.append(response['result'])
        else:
            expected={'inputMint':SOL,'outputMint':target['mint'],'amount':str(target['amount_raw']),'taker':target['taker'],'slippageBps':'100','transactionVersion':'0'}
            if (r['method']!='jupiter_probe' or r['source_id']!='jupiter-swap-v2-build-paper-v1' or r['params']!=expected
                    or r['request_bytes_base64']!=base64.b64encode(urlencode(expected).encode('ascii')).decode()):raise ValueError('Exact candidate quote request required')
            quote=terminal._wire(r['response_bytes_base64'],2*1024*1024)
            if type(quote) is not dict or not {'inAmount','outAmount','routePlan'}<=set(quote):raise ValueError('Retained successful quote transport required')
    times=[r['observed_at'] for r in successful]
    if times!=sorted(times) or times[-1]-times[0]>15:raise ValueError('Successful attempt ordering/timing inconsistent')
    opts={'commitment':'confirmed','encoding':'base64'}
    if (records[0]['method']!='getAccountInfo' or records[0]['params']!=[target['mint'],opts]
            or records[1]['method']!='getAccountInfo' or records[1]['params']!=[target['pool'],opts]):raise ValueError('Exact mint/discovery sequence required')
    # Reproduce the unchanged atomic request/capture; this is structural evidence,
    # never token safety or an execution/authentication approval.
    calls=[];captures=[]
    def rpc(method,params):
        index=len(calls)+1;calls.append(index)
        if index>2 or (records[index]['method'],records[index]['params'])!=(method,params):raise ValueError('Atomic request disagreement')
        return responses[index]
    from .pools import verify_pool
    from .token2022_paper import selected
    verify_pool(target['pool'],target['mint'],rpc,capture=lambda x:captures.append(x) or digest(x),token_profile_version=selected(cfg))
    if calls!=[1,2] or len(captures)!=1 or terminal._load(store,digest(captures[0]))!=captures[0]:raise ValueError('Original atomic capture missing')


def plan(research_db,evidence_db,ledger_db,cfg,*,pass_id,outcome_hash,attempt_refs,pacing_db):
    from .evidence import EvidenceStore
    from .history_progress import HistoryProgress
    context=terminal._context(research_db,evidence_db,ledger_db,pacing_db)
    if pacing_db is None or not terminal._id(pass_id) or not runtime._hash(outcome_hash) or type(attempt_refs) not in (list,tuple) or len(attempt_refs)!=5:raise ValueError('Exact HTTP403 proof identities required')
    store=EvidenceStore(context['evidence_db'],read_only=True);progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
    with closing(store.connect()) as c:
        terminal._passes(c);row=c.execute('SELECT intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?',(pass_id,)).fetchone()
        terminal._monitoring(c,store=store)
    if row is None or row[1] is not None:raise ValueError('Original NULL pass required')
    intent=terminal._load(store,row[0]);scan=next(iter(intent['admissions']))
    with closing(sqlite3.connect(Path(context['ledger_db']).as_uri()+'?mode=ro',uri=True)) as c:anchors,_=terminal._ledger(c,cfg,initial=True)
    v={'version':1,'association':'EXPLICIT_REVIEWED_HTTP403','pass_id':pass_id,'intent_hash':row[0],'outcome_hash':outcome_hash,
       'attempt_refs':list(attempt_refs),'source_hash':intent['source_hash'],'context':context,'config_hash':digest(cfg),'scan_id':scan,
       'before':intent['admissions'][scan]['requests_used'],'after':progress.admission(scan)['requests_used'],'ledger_anchors':anchors,
       'response_body_unavailable':True,'failed_attempt_binding':'EXPLICIT_REVIEWED_NOT_INTRINSIC','failure':'PRICE_V3_HTTP403_ACCESS_FAILURE'}
    proof(store,progress,v,cfg,current_budget=True);terminal._pacing(context['pacing_db'])
    return v


def _locked(function,research_db,evidence_db,ledger_db,cfg,**args):
    from .paper_cycle import _lock
    from .paper_observe_cli import _worker_lock
    context=terminal._context(research_db,evidence_db,ledger_db,args.get('pacing_db'))
    with ExitStack() as stack:
        if stack.enter_context(_worker_lock(context['research_db'])) is None:raise ValueError('Research worker busy')
        for path in (context['evidence_db']+'.ownership-invocation.lock',context['ledger_db']+'.paper-cycle.lock'):
            if not stack.enter_context(_lock(path)):raise ValueError('Context busy')
        runtime.require_transition_context(research_db,evidence_db,ledger_db,reviewed_context={k:context[k] for k in ('research_db','evidence_db','ledger_db')})
        return function(research_db,evidence_db,ledger_db,cfg,**args)


def review_http403_plan(*args,**kw):
    """Locked READ ONLY pin proposal; independent review certifies full inventory."""
    return _locked(plan,*args,**kw)


def _append(research_db,evidence_db,ledger_db,cfg,**args):
    from .evidence import EvidenceStore
    v=plan(research_db,evidence_db,ledger_db,cfg,**args);_approved(v)
    store=EvidenceStore(v['context']['evidence_db'],read_only=True);store.read_only=False
    with closing(store.connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            fresh=plan(research_db,evidence_db,ledger_db,cfg,**args)
            if fresh!=v:raise ValueError('HTTP403 proof changed before append')
            _approved(fresh);old=rows(c)
            if any(x['pass_id']==v['pass_id'] or x['scan_id']==v['scan_id'] for x in terminal._rows(c)):raise ValueError('Conflicting policy receipt')
            for x in old:
                if x['pass_id']==v['pass_id']:
                    if x!=v:raise ValueError('Conflicting HTTP403 replay')
                    c.rollback();return {'status':'ALREADY_RECORDED','receipt_hash':digest(v),'retired_scan':v['scan_id'],'entry_authorized':False}
                if x['scan_id']==v['scan_id']:raise ValueError('Retired scan conflict')
            if not old:
                c.execute(_schema())
                for sql in _guards().values():c.execute(sql)
            c.execute(f'INSERT INTO {TABLE} VALUES(?,?,?,?)',(v['pass_id'],v['scan_id'],canonical(v),digest(v)))
            rows(c);c.commit()
        except BaseException:c.rollback();raise
    return {'status':'RECORDED','receipt_hash':digest(v),'retired_scan':v['scan_id'],'entry_authorized':False}


def reconcile_http403(*args,**kw):
    """Explicit reviewed append only. No pass update, transport, retry or reset."""
    return _locked(_append,*args,**kw)


def main(argv=None):
    import argparse,json
    from .paper_cycle_cli import _config
    parser=argparse.ArgumentParser(description='Explicit reviewed Price HTTP403 retirement; no retry/reset')
    for name in ('research-db','evidence-db','ledger-db','config','pass-id','outcome-hash','pacing-db'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--attempt-ref',action='append',required=True)
    parser.add_argument('--plan',action='store_true',help='Read-only proposed pin, never authorization')
    args=parser.parse_args(argv)
    try:
        function=review_http403_plan if args.plan else reconcile_http403
        result=function(args.research_db,args.evidence_db,args.ledger_db,_config(args.config),pass_id=args.pass_id,
                        outcome_hash=args.outcome_hash,attempt_refs=args.attempt_ref,pacing_db=args.pacing_db)
        print(json.dumps(result,sort_keys=True));return 0
    except (ValueError,TypeError,KeyError,IndexError,OSError,sqlite3.Error):
        print(json.dumps({'status':'BLOCKED','blockers':['HTTP403_RETIREMENT_UNPROVED'],'entry_authorized':False}));return 2


if __name__=='__main__':raise SystemExit(main())
