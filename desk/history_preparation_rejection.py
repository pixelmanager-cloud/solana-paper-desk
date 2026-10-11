"""Bounded NO_ENTRY proof for a fully captured history-only preparation.

Caller owns research/evidence/ledger locks. Ambiguity is never a rejection.
Dispatcher and global-gate integration are separate reviewed consumers.
"""
from contextlib import closing, ExitStack
from pathlib import Path
import sqlite3
import time
from desk import paper_terminal_reconciliation as terminal, runtime_compatibility as runtime
from desk import paper_preparation_retirement as historical
from desk import verified_index, pass_inventory
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.live_strategy_features import MAX_RECORDS, MAX_RECORD_BYTES, MAX_TOTAL_BYTES, calculate
from desk.model import canonical,digest
from desk.replay_history import replay_history

TABLE='paper_history_preparation_rejections'
MAX_REJECTIONS=512
WINDOW_MAX_PAGES=4096   # pages written during ONE preparation (intent..outcome); not a store-wide cap
KIND='history_preparation_rejection'
PAGE_KIND='history_preparation_no_entry_v1'   # the kind field of the retained outcome page
REASONS={'HISTORY_FEATURE_RECORD_BYTES_EXCEEDED','HISTORY_FEATURE_RECORD_COUNT_EXCEEDED',
         'HISTORY_FEATURE_AGGREGATE_BYTES_EXCEEDED','HISTORY_FRESH_ENTRY_REQUESTS_UNAVAILABLE',
         'HISTORY_FEATURE_MOMENTUM_STALE','HISTORY_FEATURE_EMPTY_WINDOW','HISTORY_REQUIRED_MEASUREMENTS_UNAVAILABLE'}
SQL=f'CREATE TABLE {TABLE}(pass_id TEXT PRIMARY KEY,scan_id TEXT NOT NULL UNIQUE,intent_hash TEXT NOT NULL,outcome_hash TEXT NOT NULL)'
GUARDS={TABLE+'_insert':f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE pass_id=NEW.pass_id OR scan_id=NEW.scan_id OR rowid=NEW.rowid) OR (SELECT count(*) FROM {TABLE})>={MAX_REJECTIONS} BEGIN SELECT RAISE(ABORT,'Preparation rejection immutable'); END"}
GUARDS|={TABLE+'_'+a.lower():f"CREATE TRIGGER {TABLE}_{a.lower()} BEFORE {a} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Preparation rejection immutable'); END" for a in ('UPDATE','DELETE')}


def _passes(c, cutoff=None):
    terminal._passes(c)
    rows=c.execute('SELECT rowid,id,intent_hash,outcome_hash FROM paper_observation_passes'+(' WHERE rowid<=?' if cutoff is not None else '')+' ORDER BY rowid',() if cutoff is None else (cutoff,)).fetchall()
    return rows


def intent(store,ledger,cfg,item,admission,context):
    from dataclasses import asdict
    with closing(store.connect()) as c:
        rows=_passes(c)
    with closing(sqlite3.connect(Path(ledger).as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN');anchors,_=terminal._ledger(c,cfg,initial=True)
        originals=historical._ledger_originals(c);prefix=historical._ledger_originals(c,prefix=True)
    return {'kind':'history_first_paper_preparation_v4','feature_semantics_version':2,'config_hash':digest(cfg),'target':asdict(item),
        'admission':admission,'context':context,'source_hash':runtime.implementation_hash(),
        'ledger_anchors':anchors,'ledger_original_hash':originals,'ledger_prefix_hash':prefix,
        'pass_cutoff':rows[-1][0] if rows else 0,'passes_hash':digest(rows),'preparation_seconds':18,
        'fresh_requests_reserved':7 if cfg.get('paper_usd_valuation_version',0)==1 else 9}


def bounds(store,coverage,*,cfg=None,as_of=None,semantics_version=1,required_measurements=False):
    if coverage is None:return {'records':0,'record_bytes':0,'max_record_bytes':0,'page_pair_bytes':0,'momentum_at':None}
    if type(semantics_version) is not int or semantics_version not in (1,2):raise ValueError('Preparation semantics version')
    result={'records':0,'record_bytes':0,'max_record_bytes':0,'page_pair_bytes':0,'momentum_at':None}
    if required_measurements:result['missing_measurements']=None
    raw_rows=[];pairs=[]
    if semantics_version==2:
        from .streaming_history import RetainedHistoryPages
        pairs=RetainedHistoryPages(store,coverage)
        raw_rows=pairs.records()
    replay_history(coverage,store,retain_observations=False,load_page=pairs._load if semantics_version==2 else None)
    source=pairs if semantics_version==2 else ({'response':store.load(p['payload_hash']),'request':store.load(p['request_evidence_hash'])} for p in coverage['pages'])
    for pair in source:
        response=pair['response'];request=pair['request']
        result['page_pair_bytes']+=len(canonical({'request':request,'response':response}).encode())
        if semantics_version==1:
            raw_rows.extend(response['data']);pairs.append({'request':request,'response':response})
        else:
            del request
        for raw in response['data']:
            size=len(canonical(raw).encode());result['records']+=1;result['record_bytes']+=size
            result['max_record_bytes']=max(result['max_record_bytes'],size)
    if semantics_version==2:
        pair=None;response=None;raw=None  # release the last page before feature replay
    if cfg is not None and coverage['query_range_exhausted'] and reason_for(result,18,0,semantics_version=semantics_version) is None:
        measured=calculate(raw_rows,pool=coverage['address'],as_of=as_of,
            provenance='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE',history_pages=pairs,
            token_profile_version=cfg.get('paper_token_profile_version',0),semantics_version=semantics_version)
        if required_measurements and measured['window']['coverage_complete']:
            from .model import OBSERVABLE_FORMULAS,BOOST_VOLUME_FEATURE
            volume=BOOST_VOLUME_FEATURE if cfg.get('paper_token_profile_version',0)==2 else 'volume_vs_liq'
            names=set(OBSERVABLE_FORMULAS)|{'net_buy_ratio','unique_buyers_5m',volume,'drawdown_from_high'}
            result['missing_measurements']=sorted(name for name in names if measured['fields'][name]['status']!='MEASURED_WINDOW')
        row=measured['fields']['momentum_at']
        if row['status']=='MEASURED_WINDOW' and type(row['observed_at']) is int:
            result['momentum_at']=row['observed_at']
    return result


def reason_for(measured,remaining,reserve,*,now=None,momentum_ttl=None,exhausted=False,semantics_version=1,empty_window=False):
    if empty_window and exhausted and measured['records']==0:return 'HISTORY_FEATURE_EMPTY_WINDOW'
    if empty_window and exhausted and measured.get('missing_measurements'):return 'HISTORY_REQUIRED_MEASUREMENTS_UNAVAILABLE'
    if measured['max_record_bytes']>MAX_RECORD_BYTES:return 'HISTORY_FEATURE_RECORD_BYTES_EXCEEDED'
    if measured['records']>MAX_RECORDS:return 'HISTORY_FEATURE_RECORD_COUNT_EXCEEDED'
    if semantics_version==1 and max(measured['record_bytes'],measured['page_pair_bytes'])>MAX_TOTAL_BYTES:return 'HISTORY_FEATURE_AGGREGATE_BYTES_EXCEEDED'
    if remaining<reserve+(0 if exhausted else 1):return 'HISTORY_FRESH_ENTRY_REQUESTS_UNAVAILABLE'
    if (type(measured.get('momentum_at')) is int and type(now) is int
            and type(momentum_ttl) is int and now-measured['momentum_at']>momentum_ttl):
        return 'HISTORY_FEATURE_MOMENTUM_STALE'
    return None


def _window(store,intent_hash,allowed_outcome):
    """Pages saved while THIS preparation ran: after its intent page and not after its outcome page.

    A preparation's charged attempts (and any partial outcome of it) are written between its intent and its outcome, so the
    scan is O(pages of this preparation), not O(every page the store has ever kept) (T24R F7). Rowid order is insertion
    order (nothing is deleted); a store whose rowids were renumbered fails closed ('charged attempt missing')."""
    with closing(store.connect()) as c:
        lo=c.execute('SELECT rowid FROM pages WHERE hash=?',(intent_hash,)).fetchone()
        if lo is None:raise ValueError('Preparation intent page missing')
        hi=c.execute('SELECT rowid FROM pages WHERE hash=?',(allowed_outcome,)).fetchone() if allowed_outcome else None
        hi=hi[0] if hi else c.execute('SELECT COALESCE(max(rowid),0) FROM pages').fetchone()[0]
        return lo[0],hi


def _attempts(store,scan,before,after,*,intent_hash=None,allowed_outcome=None):
    if intent_hash is not None:
        lo,hi=_window(store,intent_hash,allowed_outcome)
        where,args=' WHERE rowid BETWEEN ? AND ?',(lo,hi)
    else:   # legacy callers without an intent (retired-incident reconciliation) keep the whole-store scan
        where,args='',()
    with closing(store.connect()) as c:
        bad=c.execute("SELECT 1 FROM pages"+where+(" AND " if where else " WHERE ")+"(typeof(hash)!='text' OR length(CAST(hash AS BLOB))!=64 OR typeof(payload)!='blob' OR length(payload) NOT BETWEEN 1 AND ? OR typeof(raw_bytes)!='integer' OR CASE WHEN typeof(raw_bytes)='integer' THEN raw_bytes NOT BETWEEN 1 AND ? ELSE 1 END) LIMIT 1",args+(terminal.MAX_PROOF_COMPRESSED_BYTES,terminal.MAX_PROOF_RAW_BYTES)).fetchone()
        count,total,compressed=c.execute('SELECT count(*),COALESCE(sum(raw_bytes),0),COALESCE(sum(length(payload)),0) FROM pages'+where,args).fetchone()
        # The bound is per preparation window now; the store as a whole has no page-count latch (rotation warnings instead).
        if bad or count>WINDOW_MAX_PAGES or total>256*1024*1024 or compressed>256*1024*1024:raise ValueError('Preparation attempt inventory bound')
        keys=[r[0] for r in c.execute('SELECT hash FROM pages'+where+' ORDER BY '+('rowid' if where else 'hash'),args)]
    found={}
    for key in keys:
        record=terminal._load(store,key)
        if (intent_hash is not None and type(record) is dict
                and record.get('intent_hash')==intent_hash
                and record.get('kind') in ('history_first_paper_preparation_outcome_v1','history_preparation_no_entry_v1')
                and key!=allowed_outcome):
            raise ValueError('Preparation partial or conflicting outcome retained')
        if type(record) is not dict or record.get('kind')!='paper_read_attempt_v1' or record.get('scan_id')!=scan:continue
        used=record.get('requests_used')
        if type(used) is not int or not 1<=used<=18:raise ValueError('Preparation attempt charge malformed')
        if before<used<=after:
            if used in found:raise ValueError('Preparation attempt charge ambiguous')
            found[used]=(key,record)
    if set(found)!=set(range(before+1,after+1)):raise ValueError('Preparation charged attempt missing')
    return [found[n] for n in range(before+1,after+1)]


def _proof(store,progress,value,*,publishing=False,cfg=None,review_source=None):
    expected={'kind','status','execution_status','live_readiness','entry_authorized','pass_id','scan_id','intent_hash','reason','history_id','coverage_hash','attempt_refs','admission_after','bounds','rejected_at'}
    if type(value) is not dict or set(value)!=expected or value['kind']!='history_preparation_no_entry_v1' or value['status']!='NO_ENTRY' or value['execution_status']!='EXECUTION_UNVERIFIED' or value['live_readiness'] is not False or value['entry_authorized'] is not False or value['reason'] not in REASONS:raise ValueError('Preparation rejection shape')
    original=terminal._load(store,value['intent_hash'])
    fields={'kind','config_hash','target','admission','context','source_hash','ledger_anchors','ledger_original_hash','ledger_prefix_hash','pass_cutoff','passes_hash','preparation_seconds','fresh_requests_reserved'}
    semantics=1
    if type(original) is dict and original.get('kind') in ('history_first_paper_preparation_v3','history_first_paper_preparation_v4'):
        fields=fields|{'feature_semantics_version'};semantics=2
        if type(original.get('feature_semantics_version')) is not int or original['feature_semantics_version']!=2:raise ValueError('Streaming preparation semantics')
    if type(original) is not dict or set(original)!=fields or original['kind'] not in ('history_first_paper_preparation_v2','history_first_paper_preparation_v3','history_first_paper_preparation_v4') or not runtime._hash(original['source_hash']) or original['preparation_seconds']!=18 or type(original['pass_cutoff']) is not int or not 0<=original['pass_cutoff']<=10000:raise ValueError('Original preparation intent malformed')
    ctx=original['context'];scan=value['scan_id'];item=original['target'];before=original['admission'];after=value['admission_after']
    with closing(sqlite3.connect(Path(ctx['ledger_db']).as_uri()+'?mode=ro',uri=True)) as c:
        saved_cfg=runtime._parse(c.execute("SELECT value FROM metadata WHERE key='config'").fetchone()[0])
    reserve=7 if saved_cfg.get('paper_usd_valuation_version',0)==1 else 9
    if digest(saved_cfg)!=original['config_hash'] or original['fresh_requests_reserved']!=reserve:
        raise ValueError('Preparation policy reserve conflict')
    if type(value['rejected_at']) is not int or value['rejected_at']<item['history_as_of']:
        raise ValueError('Preparation rejection clock malformed')
    if ctx['evidence_db']!=str(store.path) or item['target']['scan_id']!=scan or type(before['requests_used']) is not int or type(after['requests_used']) is not int or after!={**before,'requests_used':after['requests_used']} or not 0<=before['requests_used']<=after['requests_used']<=18 or after['request_ceiling']!=18:raise ValueError('Preparation admission binding')
    state=progress.snapshot(value['history_id'])
    query={'address':item['target']['pool'],'start':item['history_as_of']-300,'end':item['history_as_of']+1,'token_accounts_filter':'none'}
    if state['query'].get('page_size',100)!=50 or {k:v for k,v in state['query'].items() if k!='page_size'}!=query or digest({'budget':scan,'query':state['query']})!=value['history_id'] or state['status'] not in ('PENDING','DONE') or state['coverage'] is None or state['coverage']['evidence_hash']!=value['coverage_hash']:raise ValueError('Preparation coverage binding')
    measured=bounds(store,state['coverage'],cfg=saved_cfg,as_of=item['history_as_of'],semantics_version=semantics,required_measurements=original['kind']=='history_first_paper_preparation_v4')
    if measured!=value['bounds'] or reason_for(measured,18-after['requests_used'],reserve,
            now=value['rejected_at'],momentum_ttl=saved_cfg['momentum_ttl_seconds'],
            exhausted=state['coverage']['query_range_exhausted'],semantics_version=semantics,empty_window=original['kind']=='history_first_paper_preparation_v4')!=value['reason']:raise ValueError('Preparation deterministic reason conflict')
    if value['reason'] in ('HISTORY_FEATURE_EMPTY_WINDOW','HISTORY_REQUIRED_MEASUREMENTS_UNAVAILABLE') and state['coverage']['query_coverage_verified'] is not True:
        raise ValueError('Verified exhausted empty history required')
    attempts=_attempts(store,scan,before['requests_used'],after['requests_used'],
        intent_hash=value['intent_hash'],allowed_outcome=digest(value))
    if [key for key,_ in attempts]!=value['attempt_refs'] or len(attempts)!=state['attempts'] or len(attempts)!=len(state['coverage']['pages']):raise ValueError('Preparation reservation inventory mismatch')
    for (_,record),page in zip(attempts,state['coverage']['pages']):
        request=store.load(page['request_evidence_hash']);response=store.load(page['payload_hash'])
        wire=terminal._wire(record['request_bytes_base64'],128*1024)
        captured=terminal._wire(record['response_bytes_base64'],2*1024*1024)
        if record['method']!='getTransactionsForAddress' or record['params']!=request['params'] or record['source_id']!='helius-mainnet-paper-confirmed-v1' or record['failure_code'] is not None or type(record['http_status']) is not int or record['http_status']!=200 or type(record['observed_at']) is not int or wire!={'jsonrpc':'2.0','id':'paper-read-v1','method':request['method'],'params':request['params']} or captured!={'jsonrpc':'2.0','id':'paper-read-v1','result':response}:raise ValueError('Preparation original attempt conflict')
    with closing(store.connect()) as c:
        rows=_passes(c,original['pass_cutoff'])
        if digest(rows)!=original['passes_hash']:raise ValueError('Preparation prior passes changed')
        own=c.execute('SELECT intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?',(value['pass_id'],)).fetchone()
        if own!=(value['intent_hash'],None if publishing else digest(value)):raise ValueError('Preparation pass binding')
        if publishing and c.execute('SELECT id FROM paper_observation_passes WHERE rowid>?',(original['pass_cutoff'],)).fetchall()!=[(value['pass_id'],)]:raise ValueError('Fresh cycle entered during preparation')
        terminal._monitoring(c,store=store,review_source=review_source)
    with closing(sqlite3.connect(Path(ctx['ledger_db']).as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN')
        if publishing:
            if cfg is None or digest(cfg)!=original['config_hash']:raise ValueError('Preparation config mismatch')
            anchors,_=terminal._ledger(c,cfg,initial=True)
            if anchors!=original['ledger_anchors'] or historical._ledger_originals(c)!=original['ledger_original_hash']:raise ValueError('Preparation ledger changed')
        elif historical._ledger_originals(c,prefix=True)!=original['ledger_prefix_hash']:raise ValueError('Preparation ledger prefix changed')
    terminal._pacing(ctx['pacing_db'])
    if progress.admission(scan)!=after:raise ValueError('Preparation live charge changed')
    return original


def rows(c):
    historical._schema_bounds(c)
    objects=c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name=? OR substr(name,1,?)=? OR tbl_name=?",(TABLE,len(TABLE)+1,TABLE+'_',TABLE)).fetchall()
    if not objects:return []
    expected={('table',TABLE,TABLE,SQL)}|{('trigger',name,TABLE,sql) for name,sql in GUARDS.items()}|{('index',f'sqlite_autoindex_{TABLE}_{i}',TABLE,None) for i in (1,2)}
    if set(objects)!=expected or len(objects)!=len(expected):raise ValueError('Preparation rejection schema malformed')
    count=c.execute('SELECT count(*) FROM '+TABLE).fetchone()[0]
    if not 1<=count<=MAX_REJECTIONS:raise ValueError('Preparation rejection count invalid')
    invalid=c.execute('SELECT 1 FROM '+TABLE+" WHERE typeof(pass_id)!='text' OR length(CAST(pass_id AS BLOB))!=32 OR typeof(scan_id)!='text' OR length(CAST(scan_id AS BLOB))!=32 OR typeof(intent_hash)!='text' OR length(CAST(intent_hash AS BLOB))!=64 OR typeof(outcome_hash)!='text' OR length(CAST(outcome_hash AS BLOB))!=64 LIMIT 1").fetchone()
    if invalid:raise ValueError('Preparation rejection scalar malformed')
    result=c.execute('SELECT pass_id,scan_id,intent_hash,outcome_hash FROM '+TABLE).fetchall()
    if any(not terminal._id(p) or not terminal._id(s) or not runtime._hash(i) or not runtime._hash(o) for p,s,i,o in result):raise ValueError('Preparation rejection scalar malformed')
    return result


def publish(store,progress,ledger,cfg,*,pass_id,intent_hash,history_id,reason):
    original=terminal._load(store,intent_hash);scan=original['target']['target']['scan_id'];after=progress.admission(scan)
    if str(Path(ledger))!=original['context']['ledger_db'] or digest(cfg)!=original['config_hash']:
        raise ValueError('Preparation publication context conflict')
    with closing(store.connect()) as c:
        existing=rows(c)
    for identity,retired,intent_key,outcome_key in existing:
        if identity==pass_id:
            value=terminal._load(store,outcome_key)
            if retired!=scan or intent_key!=intent_hash or value['history_id']!=history_id or value['reason']!=reason:
                raise ValueError('Preparation rejection replay conflict')
            result={**value,'evidence_hash':outcome_key};verify(store,progress,result)
            return result
        if retired==scan:raise ValueError('Preparation scan already retired')
    state=progress.snapshot(history_id)
    attempts=_attempts(store,scan,original['admission']['requests_used'],after['requests_used'],intent_hash=intent_hash)
    value={'kind':'history_preparation_no_entry_v1','status':'NO_ENTRY','execution_status':'EXECUTION_UNVERIFIED','live_readiness':False,'entry_authorized':False,
        'pass_id':pass_id,'scan_id':scan,'intent_hash':intent_hash,'reason':reason,'history_id':history_id,
        'coverage_hash':state['coverage']['evidence_hash'],'attempt_refs':[key for key,_ in attempts],
        'admission_after':after,'bounds':bounds(store,state['coverage'],cfg=cfg,as_of=original['target']['history_as_of'],semantics_version=2 if original['kind'] in ('history_first_paper_preparation_v3','history_first_paper_preparation_v4') else 1,required_measurements=original['kind']=='history_first_paper_preparation_v4'),
        'rejected_at':time.time_ns()//10**9}
    _proof(store,progress,value,publishing=True,cfg=cfg)
    key=store.save(value)
    with closing(store.connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            _proof(store,progress,value,publishing=True,cfg=cfg)
            existing=rows(c)
            if any(row[0]==pass_id or row[1]==scan for row in existing):raise ValueError('Preparation rejection already bound')
            if not existing:
                c.execute(SQL)
                for sql in GUARDS.values():c.execute(sql)
            c.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?)',(pass_id,scan,intent_hash,key))
            if c.execute('UPDATE paper_observation_passes SET outcome_hash=? WHERE id=? AND intent_hash=? AND outcome_hash IS NULL',(key,pass_id,intent_hash)).rowcount!=1:raise ValueError('Preparation pass publication changed')
            # T24R F7: the full proof above passed; record what was proved in the same transaction as the receipt.
            verified_index.record(c,KIND,pass_id,key,verified_index.proof_digest(KIND,key,pass_id,scan,intent_hash))
            pass_inventory.record(c,pass_id,intent_hash,key,PAGE_KIND,scan,intent_hash)      # T24S: same transaction as the outcome
            verified_index.rows(c);rows(c);c.commit()
        except BaseException:c.rollback();raise
    from desk.paper_cycle_no_entry import warn_if_crowded
    warn_if_crowded(store)
    return {**value,'evidence_hash':key}


def verify(store,progress,result,*,review_source=None):
    if type(result) is not dict or not runtime._hash(result.get('evidence_hash')):raise ValueError('Preparation result reference required')
    value=terminal._load(store,result['evidence_hash'])
    if result!={**value,'evidence_hash':digest(value)}:raise ValueError('Preparation result conflict')
    with closing(store.connect()) as c:
        if (value['pass_id'],value['scan_id'],value['intent_hash'],digest(value)) not in rows(c):raise ValueError('Preparation rejection publication incomplete')
    return _proof(store,progress,value,review_source=review_source)


def gate(store,research,scan_ids,*,ledger_locked=None,review_source=None,full=False):
    """Companion global-gate hook: replay new rejections and a bounded deterministic sample; never trust a marker.

    Receipts with a verified-digest row (T24R F7) are re-checked cheaply except for ``verified_index.SAMPLE`` of them per call;
    receipts without one are always replayed in full. ``full=True`` replays everything."""
    progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
    full=full or verified_index.full_replay_forced()
    with closing(store.connect()) as c:
        retired=rows(c)
        found=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_observation_passes'").fetchone()
        if not found:
            if retired:raise ValueError('Preparation original pass table missing')
            return None
        terminal._passes(c)
        total,last=c.execute('SELECT count(*),COALESCE((SELECT id FROM paper_observation_passes ORDER BY rowid DESC LIMIT 1),0) FROM paper_observation_passes').fetchone()
        index=verified_index.rows(c)
    # Two-way inventory: deleting the rejection table cannot erase a bound outcome's retirement. T24S: page loads only for
    # passes that are new since the last call and a bounded sample (desk/pass_inventory.py); the rest is checked as SQL scalars.
    inventory=pass_inventory.completed(store,(PAGE_KIND,),persist=review_source is None,full=full)
    bound=set()
    for identity,scan,page_intent,intent_key,outcome_key in inventory.of(PAGE_KIND):
        if page_intent!=intent_key:raise ValueError('Preparation outcome original pass conflict')
        bound.add((identity,scan,intent_key,outcome_key))
    if bound!=set(retired):raise ValueError('Preparation rejection inventory incomplete')
    seed=digest({'passes':total,'last':last})
    replay,quick=verified_index.plan(index,KIND,retired,seed=seed,full=full)
    quick,rest=verified_index.split_quick(quick,seed=seed,kind=KIND)
    with closing(store.connect()) as c:
        if verified_index.missing_pages(c,[item[3] for item in rest]):raise ValueError('Preparation rejection binding changed')
    for identity,scan,intent_key,outcome_key in quick:
        value=terminal._load(store,outcome_key)
        if digest(value)!=outcome_key or (value['pass_id'],value['scan_id'],value['intent_hash'])!=(identity,scan,intent_key):
            raise ValueError('Preparation rejection binding changed')
        if terminal._load(store,intent_key)['context']['research_db']!=str(research):raise ValueError('Preparation rejection research context')
    for identity,scan,intent_key,outcome_key in replay:
        value=terminal._load(store,outcome_key)
        intent_record=terminal._load(store,intent_key)
        from .paper_cycle import _lock, canonical_job_path
        path=canonical_job_path(intent_record['context']['ledger_db'])
        with ExitStack() as locks:
            if ledger_locked!=str(path):
                if not locks.enter_context(_lock(str(path)+'.paper-cycle.lock')):
                    raise ValueError('Preparation receipt ledger busy')
            original=verify(store,progress,{**value,'evidence_hash':outcome_key},review_source=review_source)
            if original['context']['research_db']!=str(research):raise ValueError('Preparation rejection research context')
    return 'REJECTED_SCAN_RETIRED' if any(row[1] in scan_ids for row in retired) else None
