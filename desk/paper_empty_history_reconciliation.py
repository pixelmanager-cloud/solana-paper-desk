"""Reviewed retirement of a captured empty-window, pre-entry paper pass.

No entry evidence, retry, counter reset or original NULL publication is created.
"""
import base64
from contextlib import closing, ExitStack
from pathlib import Path
import sqlite3
from urllib.parse import urlencode
from . import paper_terminal_reconciliation as terminal, runtime_compatibility as runtime
from . import history_preparation_rejection as preparation
from .model import canonical, digest
from .evidence import EvidenceStore
from .history_progress import HistoryProgress

ASSOCIATION='EXPLICIT_REVIEWED_EMPTY_HISTORY'
POLICY=Path(__file__).resolve().parents[1]/'config/paper-empty-history-reconciliation.json'
DETAILS={'preparation_pass_id','preparation_intent_hash','preparation_outcome_hash',
         'history_id','coverage_hash','dispatcher_context','dispatch_id','dispatcher_result_hash'}


def shape(v):
    return (type(v) is dict and set(v)==terminal.FIELDS|{'empty_history'}
        and type(v['version']) is int and v['version']==1 and v['association']==ASSOCIATION
        and terminal._id(v['pass_id']) and terminal._id(v['scan_id'])
        and all(runtime._hash(v[k]) for k in ('intent_hash','outcome_hash','source_hash','config_hash'))
        and type(v['attempt_refs']) is list and len(v['attempt_refs'])==5
        and all(runtime._hash(k) for k in v['attempt_refs']) and len(set(v['attempt_refs']))==5
        and v['invocation_hash'] is None and v['hazards']==[]
        and type(v['before']) is int and type(v['after']) is int and 0<=v['before']<v['after']<=18
        and v['after']==v['before']+5 and type(v['context']) is dict
        and set(v['context'])=={'research_db','evidence_db','ledger_db','pacing_db'}
        and runtime._context_shape({k:v['context'][k] for k in ('research_db','evidence_db','ledger_db')})
        and type(v['context']['pacing_db']) is str
        and str(Path(v['context']['pacing_db']).resolve())==v['context']['pacing_db']
        and type(v['ledger_anchors']) is dict and set(v['ledger_anchors'])==terminal.ANCHORS
        and all(runtime._hash(k) for k in v['ledger_anchors'].values())
        and type(v['empty_history']) is dict and set(v['empty_history'])==DETAILS
        and terminal._id(v['empty_history']['preparation_pass_id'])
        and terminal._id(v['empty_history']['dispatch_id'])
        and all(runtime._hash(v['empty_history'][k]) for k in DETAILS-{'preparation_pass_id','dispatch_id','dispatcher_context'})
        and type(v['empty_history']['dispatcher_context']) is dict)


def approved(v):
    if not shape(v):raise ValueError('Exact empty-history receipt shape required')
    with POLICY.open('rb') as f:raw=f.read(terminal.MAX_BYTES+1)
    if len(raw)>terminal.MAX_BYTES:raise ValueError('Empty-history policy bound')
    policy=runtime._parse(raw.decode())
    if (type(policy) is not dict or set(policy)!={'version','associations'} or type(policy['version']) is not int
            or policy['version']!=1 or policy['associations']!=[v]):
        raise ValueError('One independently reviewed empty-history retirement required')


def proof(store,progress,v,cfg,*,current_budget=True,review_source=None):
    if not shape(v):raise ValueError('Empty-history proof shape')
    scan=v['scan_id'];ctx=v['context'];details=v['empty_history']
    if str(store.path)!=ctx['evidence_db'] or digest(cfg)!=v['config_hash']:
        raise ValueError('Empty-history context/config changed')
    with closing(store.connect()) as c:
        terminal._passes(c)
        if c.execute('SELECT intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?',(v['pass_id'],)).fetchone()!=(v['intent_hash'],None):
            raise ValueError('Original empty-history NULL pass required')
        if c.execute('SELECT intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?',(details['preparation_pass_id'],)).fetchone()!=(details['preparation_intent_hash'],details['preparation_outcome_hash']):
            raise ValueError('Original completed preparation required')
    intent=terminal._load(store,v['intent_hash']);result=terminal._load(store,v['outcome_hash'])
    if (type(intent) is not dict or intent.get('kind')!='paper_cycle_intent_v1'
            or intent.get('config_hash')!=v['config_hash'] or intent.get('source_hash')!=v['source_hash']
            or intent.get('terminal_context')!=ctx or intent.get('ledger_anchors')!=v['ledger_anchors']
            or intent.get('ledger')!=ctx['ledger_db'] or type(intent.get('targets')) is not list or len(intent['targets'])!=1
            or type(intent.get('admissions')) is not dict or set(intent['admissions'])!={scan}):
        raise ValueError('Exact original single-candidate intent required')
    item=intent['targets'][0];target=item['target'];before=intent['admissions'][scan];after=progress.admission(scan)
    if (target['scan_id']!=scan or before.get('state')!='SEALED' or before.get('requests_used')!=v['before']
            or before.get('request_ceiling')!=18 or before['descriptor']['mint']!=target['mint']
            or after!={**before,'requests_used':v['after']}):raise ValueError('Exact original admission and charges required')
    fields={'attempted_requests','blockers','budget','diagnostics','events','execution_status','intent_hash',
        'investigation_attempted_requests','kind','live_readiness','monitoring_attempted_requests','outcomes','pass_id','status','usd_evidence_refs','attempt_refs'}
    if (type(result) is not dict or set(result)!=fields or result['kind']!='paper_cycle_v1' or result['status']!='BLOCKED'
            or result['blockers']!=['MARKET_PRODUCER_BLOCKED'] or result['execution_status']!='EXECUTION_UNVERIFIED'
            or result['live_readiness'] is not False or result['events']!=[] or result['outcomes']!=[]
            or result['pass_id']!=v['pass_id'] or result['intent_hash']!=v['intent_hash']
            or result['attempt_refs']!=v['attempt_refs'][:4] or result['usd_evidence_refs']!=v['attempt_refs'][4:]
            or result['budget']!={scan:{'used':v['after'],'ceiling':18}}
            or any(type(result[k]) is not int or result[k]!=n for k,n in
                   (('attempted_requests',5),('investigation_attempted_requests',5),('monitoring_attempted_requests',0)))):
        raise ValueError('Exact retained empty-window BLOCKED result required')
    prep=terminal._load(store,details['preparation_intent_hash']);done=terminal._load(store,details['preparation_outcome_hash'])
    if (type(prep) is not dict or prep.get('kind')!='history_first_paper_preparation_v3'
            or prep.get('feature_semantics_version')!=2 or prep.get('source_hash')!=v['source_hash']
            or prep.get('context')!=ctx or prep.get('config_hash')!=v['config_hash'] or prep.get('target')!=item
            or done!={'kind':'history_first_paper_preparation_outcome_v1','intent_hash':details['preparation_intent_hash'],
                      'history_as_of':item['history_as_of'],'admission':before}
            or prep.get('admission')!={**before,'requests_used':v['before']-1}):
        raise ValueError('Original preparation/cycle handoff changed')
    history=progress.snapshot(details['history_id']);coverage=history['coverage']
    query={'address':target['pool'],'start':item['history_as_of']-300,'end':item['history_as_of']+1,
           'token_accounts_filter':'none','page_size':50}
    if (history['query']!=query or details['history_id']!=digest({'budget':scan,'query':query})
            or history['status']!='DONE' or history['attempts']!=1 or type(coverage) is not dict
            or coverage.get('evidence_hash')!=details['coverage_hash'] or coverage.get('query_range_exhausted') is not True
            or coverage.get('query_coverage_verified') is not True or len(coverage.get('pages',[]))!=1):
        raise ValueError('Exact verified exhausted empty window required')
    from .replay_history import replay_history
    replay_history(coverage,store,retain_observations=False)
    page=coverage['pages'][0];response=terminal._load(store,page['payload_hash'])
    if response.get('data')!=[] or response.get('paginationToken') is not None or page.get('records')!=0:
        raise ValueError('Nonempty or incomplete history cannot retire this pass')
    historical=preparation._attempts(store,scan,v['before']-1,v['before'])
    from .paper_terminal_reconciliation import _wire
    rec=historical[0][1];request=terminal._load(store,page['request_evidence_hash'])
    if (rec['method']!='getTransactionsForAddress' or rec['params']!=request['params']
            or rec['failure_code'] is not None or rec['http_status']!=200
            or rec['source_id']!='helius-mainnet-paper-confirmed-v1'
            or _wire(rec['request_bytes_base64'],128*1024)!={'jsonrpc':'2.0','id':'paper-read-v1','method':request['method'],'params':request['params']}
            or _wire(rec['response_bytes_base64'],2*1024*1024)!={'jsonrpc':'2.0','id':'paper-read-v1','result':response}):
        raise ValueError('Original empty-history wire binding invalid')
    attempts=preparation._attempts(store,scan,v['before'],v['after'])
    if [key for key,_ in attempts]!=v['attempt_refs']:raise ValueError('Complete fresh charged inventory required')
    records=[record for _,record in attempts]
    for n,r in enumerate(records):
        if (r['failure_code'] is not None or type(r['http_status']) is not int or r['http_status']!=200
                or type(r['observed_at']) is not int or not 0<=r['observed_at']<2**63):
            raise ValueError('Successful captured fresh attempt required')
        if n<3:
            if r['source_id']!='helius-mainnet-paper-confirmed-v1':raise ValueError('Fresh RPC source changed')
            request=_wire(r['request_bytes_base64'],128*1024);response=_wire(r['response_bytes_base64'],2*1024*1024)
            if request!={'jsonrpc':'2.0','id':'paper-read-v1','method':r['method'],'params':r['params']} or set(response)!={'jsonrpc','id','result'} or response['jsonrpc']!='2.0' or response['id']!='paper-read-v1':
                raise ValueError('Fresh RPC raw binding invalid')
            from . import original_byte_read_transport as syntax
            syntax._bounded_json(response);syntax._result({'method':r['method'],'params':r['params']},response['result'])
    opts={'encoding':'base64','commitment':'confirmed'}
    if (records[0]['method']!='getAccountInfo' or records[0]['params']!=[target['mint'],opts]
            or records[1]['method']!='getAccountInfo' or records[1]['params']!=[target['pool'],opts]
            or records[2]['method']!='getMultipleAccounts'):raise ValueError('Exact fresh RPC sequence required')
    quote=records[3];params={'inputMint':'So11111111111111111111111111111111111111112','outputMint':target['mint'],
        'amount':str(target['amount_raw']),'taker':target['taker'],'slippageBps':'100','transactionVersion':'0'}
    if (quote['method']!='jupiter_probe' or quote['source_id']!='jupiter-swap-v2-build-paper-v1' or quote['params']!=params
            or quote['request_bytes_base64']!=base64.b64encode(urlencode(params).encode()).decode()):raise ValueError('Original quote binding invalid')
    _wire(quote['response_bytes_base64'],2*1024*1024)
    from .kraken_usd_observation import from_attempt
    from_attempt(records[4],now=records[4]['acquired_at_decimal'],scan=scan)
    times=[r['observed_at'] for r in records]
    if times!=sorted(times) or times[-1]-times[0]>10:raise ValueError('Original fresh phase timing invalid')
    from tools import paper_entry_dispatcher as dispatcher
    dc=details['dispatcher_context']
    if dc.get('source_hash')!=v['source_hash'] or dc.get('config_hash')!=v['config_hash'] or any(dc['paths'][k]['path']!=ctx[k] for k in ctx):
        raise ValueError('Original dispatcher context changed')
    path=dispatcher._path(dc['journal'],private=True)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN');values=dispatcher._read_journal(c)
    di=values['intents'].get(details['dispatch_id']);dr=values['results'].get(details['dispatch_id'])
    if (type(di) is not dict or type(dr) is not dict or di['context_hash']!=digest(dc)
            or di['hint']['mint']!=target['mint'] or di['hint']['pool']!=target['pool']
            or digest(dr)!=details['dispatcher_result_hash'] or dr['intent_hash']!=digest(di) or dr['scan_id']!=scan
            or dr['result']!={**result,'evidence_hash':v['outcome_hash']}):raise ValueError('Original completed dispatcher result changed')
    with closing(sqlite3.connect(Path(ctx['ledger_db']).as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN');anchors,_=terminal._ledger(c,cfg,initial=True,historical_source=review_source)
        if anchors!=v['ledger_anchors']:raise ValueError('Empty-window ledger effects or anchors changed')
    with closing(store.connect()) as c:terminal._monitoring(c,store=store,review_source=review_source)
    terminal._pacing(ctx['pacing_db'])


def plan(research_db,evidence_db,ledger_db,cfg,*,pass_id,outcome_hash,empty_history,pacing_db,review_source=None):
    ctx=terminal._context(research_db,evidence_db,ledger_db,pacing_db)
    store=EvidenceStore(ctx['evidence_db'],read_only=True);progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
    with closing(store.connect()) as c:
        terminal._passes(c);row=c.execute('SELECT intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?',(pass_id,)).fetchone()
    if row is None or row[1] is not None:raise ValueError('Original NULL pass required')
    intent=terminal._load(store,row[0]);result=terminal._load(store,outcome_hash);scan=next(iter(intent['admissions']))
    v={'version':1,'association':ASSOCIATION,'pass_id':pass_id,'intent_hash':row[0],'outcome_hash':outcome_hash,
       'attempt_refs':result['attempt_refs']+result['usd_evidence_refs'],'invocation_hash':None,'source_hash':intent['source_hash'],
       'context':ctx,'config_hash':digest(cfg),'scan_id':scan,'before':intent['admissions'][scan]['requests_used'],
       'after':progress.admission(scan)['requests_used'],'ledger_anchors':intent['ledger_anchors'],'hazards':[],
       'empty_history':empty_history}
    proof(store,progress,v,cfg,review_source=review_source)
    return v


def reconcile(research_db,evidence_db,ledger_db,cfg,*,pin):
    """Append a reviewed terminal receipt; retain original NULL, result and usage."""
    approved(pin);ctx=terminal._context(research_db,evidence_db,ledger_db,pin['context']['pacing_db'])
    if ctx!=pin['context']:raise ValueError('Exact reviewed recovery context required')
    from .paper_cycle import _lock
    from .paper_observe_cli import _worker_lock
    with ExitStack() as locks:
        if locks.enter_context(_worker_lock(Path(ctx['research_db']))) is None:raise ValueError('Research busy')
        for path in (ctx['evidence_db']+'.ownership-invocation.lock',ctx['ledger_db']+'.paper-cycle.lock'):
            if not locks.enter_context(_lock(path)):raise ValueError('Empty-history context busy')
        runtime.require_transition_context(research_db,evidence_db,ledger_db,reviewed_context={k:ctx[k] for k in ('research_db','evidence_db','ledger_db')})
        store=EvidenceStore(evidence_db,read_only=True);store.read_only=False
        progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
        proof(store,progress,pin,cfg,review_source=pin['source_hash'])
        with closing(store.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                proof(store,progress,pin,cfg,review_source=pin['source_hash'])
                old=terminal._rows(c)
                matches=[v for v in old if v['pass_id']==pin['pass_id'] or v['scan_id']==pin['scan_id']]
                if matches:
                    if matches!=[pin]:raise ValueError('Conflicting empty-history receipt')
                    c.rollback();return {'status':'ALREADY_RECORDED','receipt_hash':digest(pin),'retired_scan':pin['scan_id']}
                terminal._insert(c,pin);c.commit()
            except BaseException:c.rollback();raise
    return {'status':'RECORDED','receipt_hash':digest(pin),'retired_scan':pin['scan_id']}
