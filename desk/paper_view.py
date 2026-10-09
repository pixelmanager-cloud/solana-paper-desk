"""Read-only, bounded paper-ledger projection for the local dashboard."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from .model import decimal as dec,ZERO,digest,validate_event,PAPER_EXPERIMENTAL
from .paper_checkpoint import read_checkpoint,RecoveryRequired,policy_version


def _implementation_hash():
    root=Path(__file__).parent
    return digest({str(p.relative_to(root)):p.read_text() for p in sorted(root.rglob('*'))
                   if p.is_file() and p.suffix in ('.py','.json')})


def _history_preflight(c):
    """Charge the whole history in the caller's snapshot before loading bodies."""
    count=c.execute('SELECT COUNT(*) FROM (SELECT 1 FROM events LIMIT 10001)').fetchone()[0]
    if type(count) is not int or count>10000:
        raise RecoveryRequired('RUNNER_HISTORY_LIMIT')
    invalid=c.execute("""SELECT 1 FROM events WHERE
        typeof(seq)!='integer' OR seq<1 OR typeof(ts)!='integer' OR ts<0 OR
        typeof(event_id)!='text' OR length(CAST(event_id AS BLOB)) NOT BETWEEN 1 AND 262144 OR
        typeof(payload)!='text' OR length(CAST(payload AS BLOB)) NOT BETWEEN 1 AND 262144 OR
        typeof(payload_hash)!='text' OR length(CAST(payload_hash AS BLOB))!=64 LIMIT 1""").fetchone()
    total=c.execute('''SELECT COALESCE(SUM(length(CAST(payload AS BLOB))+
        length(CAST(event_id AS BLOB))+length(CAST(payload_hash AS BLOB))),0) FROM events''').fetchone()[0]
    if invalid or type(total) is not int or total>16*1024*1024:
        raise RecoveryRequired('RUNNER_HISTORY_LIMIT')


def _event_json(payload,cfg):
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError('Duplicate event field')
            value[key]=item
        return value
    event=json.loads(payload,object_pairs_hook=unique)
    if type(event) is not dict or type(event.get('schema_version')) is not int:
        raise ValueError('Invalid event version/type')
    # Only the checkpoint's hash-bound experiment config selects the policy.
    # Match engine.transition: experimental validation is market-only; operator
    # controls and monitor clocks always use the strict grammar.
    version=policy_version(cfg)
    if version in (1,2,3) and event.get('kind')=='market':
        validate_event(event,mode=PAPER_EXPERIMENTAL,policy_version=version)
    else:
        validate_event(event)
    return event


def _runner_evidence(c,state,marker,now,ttl,cfg):
    """PR103 marker/bootstrap identifies saved synthetic history, never liveness.

    Future LIVE_PAPER requires a separately reviewed adapter identity/source and
    admission contract. Its marker is unsupported here and grants no permission.
    """
    if marker is not None and marker!='SYNTHETIC_TEST_ONLY':
        raise RecoveryRequired('RUNNER_MARKER_UNSUPPORTED')
    _history_preflight(c)
    bootstrap=None;last=None;market=None;total=0;count=0
    for seq,event_id,ts,payload,key in c.execute('SELECT seq,event_id,ts,payload,payload_hash FROM events ORDER BY seq LIMIT 10001'):
        count+=1;total+=len(payload.encode())
        if count>10000 or len(payload.encode())>256*1024 or total>16*1024*1024:
            raise RecoveryRequired('RUNNER_HISTORY_LIMIT')
        try:event=_event_json(payload,cfg)
        except (ValueError,TypeError,KeyError,AttributeError,RecursionError,OverflowError):
            raise RecoveryRequired('RUNNER_EVENT_INVALID') from None
        if (not isinstance(event,dict) or event.get('event_id')!=event_id
                or type(event.get('ts')) is not int or event['ts']!=ts or digest(event)!=key):
            raise RecoveryRequired('RUNNER_EVENT_INVALID')
        if event_id=='paper-runner:init':bootstrap=(seq,event)
        last={'event_id':event_id,'kind':event.get('kind'),'ts':ts,'payload_hash':key}
        if event.get('kind')=='market':
            if marker is not None and event.get('provenance')!='SYNTHETIC_TEST_ONLY':
                raise RecoveryRequired('RUNNER_PROVENANCE_MISMATCH')
            if market is None or ts>=market['ts']:
                market={**last,'provenance':event.get('provenance','UNKNOWN')}
    if marker is not None:
        init={'schema_version':1,'event_id':'paper-runner:init','ts':0,'kind':'clock','actor':'paper_monitor'}
        if bootstrap!=(1,init) or any(p['provenance']!=marker for p in state['positions'].values()):
            raise RecoveryRequired('RUNNER_INITIALIZATION_INVALID')
    age=now-state['last_ts'];market_age=now-market['ts'] if market else None
    initialized=marker=='SYNTHETIC_TEST_ONLY'
    return {'runner_status':('SYNTHETIC_INITIALIZED' if initialized and count==1 else
                             'SYNTHETIC_CHECKPOINT_RECORDED' if initialized else 'NOT_CONNECTED'),
            'runner_provenance':marker,'runner_liveness':'UNKNOWN',
            'last_event_age_seconds':age,'last_market_at':market['ts'] if market else None,
            'last_market_age_seconds':market_age,
            'last_market_currentness':('NO_MARKET_OBSERVATION' if market is None else
                                      'FUTURE' if market_age<0 else 'RECENT_SAVED_OBSERVATION' if market_age<=ttl else 'STALE'),
            'last_run_evidence':last if initialized and count>1 else None,
            'runner_evidence_scope':'Last committed experiment event only; run completion and process liveness are not persisted.'}


def paper_status(path,*,now=None,expected_config=None):
    now=int(time.time()) if now is None else now
    if type(now) is not int or now<0:raise ValueError('Invalid observation time')
    result={'mode':'PAPER_ONLY','automatic_entry_enabled':False,'runner_status':'NOT_CONNECTED',
        'runner_liveness':'UNKNOWN','runner_provenance':None,
        'status':'NOT_CONFIGURED','positions':[],'recent_outcomes':[],
        'cash_sol':None,'realized_pnl_sol':None,'estimated_equity_sol':None,
        'notice':'Simulated research accounting. Automatic entries remain disabled; paper results are not evidence of profitability.'}
    path=Path(path)
    if not path.is_file():return result
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as c:
            c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
            state=read_checkpoint(c)
            if state is None:return {**result,'status':'EMPTY_LEDGER'}
            metadata=dict(c.execute("SELECT key,value FROM metadata WHERE key IN ('config','config_hash','implementation_hash','paper_runner')"))
            # read_checkpoint already verifies the persisted config/hash binding.
            # Projection uses that experiment's actual pinned config, never the
            # repository default. A caller can additionally require an exact cfg.
            if expected_config is not None and digest(expected_config)!=metadata['config_hash']:
                raise RecoveryRequired('CONFIG_IMPLEMENTATION_MISMATCH')
            if _implementation_hash()!=metadata['implementation_hash']:
                raise RecoveryRequired('CONFIG_IMPLEMENTATION_MISMATCH')
            cfg=json.loads(metadata['config']);ttl=cfg['price_ttl_seconds']
            if type(ttl) is not int or ttl<=0:raise ValueError('Invalid price TTL')
            runner=_runner_evidence(c,state,metadata.get('paper_runner'),now,ttl,cfg)
            cash=dec(state['cash']);realized=dec(state['realized_pnl']);positions=[];marks=ZERO;all_fresh=True
            if not isinstance(state['positions'],dict) or len(state['positions'])>100:raise ValueError('Invalid position state')
            for mint,p in sorted(state['positions'].items()):
                at=p['mark_at'];mark=dec(p['mark_value']);cost=dec(p['cost_left']);qty=dec(p['qty'])
                if min(mark,cost,qty)<0 or type(at) is not int:raise ValueError('Invalid position accounting')
                status=p.get('mark_status','UNKNOWN');blocked=p.get('exit_blocked')
                fresh=0<=now-at<=ttl and status=='MODEL_ESTIMATE' and not blocked
                all_fresh=all_fresh and fresh;marks+=mark
                positions.append({'mint':mint,'quantity':str(qty),'cost_left_sol':str(cost),
                    'last_model_value_sol':str(mark),'unrealized_pnl_sol':str(mark-cost) if fresh else None,
                    'mark_at':at,'mark_age_seconds':now-at,'mark_status':status if fresh or blocked else 'STALE',
                    'exit_blocked':blocked,'provenance':p.get('provenance','UNKNOWN'),
                    'valuation_verified':False})
                if 'entry_policy' in p:
                    positions[-1]['entry_policy']=p['entry_policy']
                if 'quote_execution' in p:
                    positions[-1]['execution_status']='EXECUTION_UNVERIFIED'
                    positions[-1]['quote_execution']={key:value for key,value in p['quote_execution'].items()
                                                     if key not in ('original_quote_json','original_mint_json')}
            # One read transaction binds outcomes and state to the same checkpoint.
            outcomes=[{'event_id':r[0],'ts':r[1],'outcome':json.loads(r[2])} for r in c.execute(
                'SELECT o.event_id,e.ts,o.payload FROM outcomes o JOIN events e ON e.event_id=o.event_id ORDER BY o.seq DESC LIMIT 50')]
            result.update(**runner,status='LEDGER_PRESENT',strategy_mode=state['mode'],last_event_at=state['last_ts'],
                config_hash=metadata.get('config_hash'),implementation_hash=metadata.get('implementation_hash'),
                cash_sol=str(cash),realized_pnl_sol=str(realized),positions=positions,recent_outcomes=outcomes,
                estimated_equity_sol=str(cash+marks) if all_fresh else None,
                valuation_status='MODEL_ESTIMATE' if all_fresh else 'STALE_OR_EXIT_UNVERIFIED')
            if cfg.get('paper_quote_execution_version') is not None:
                result['execution_status']='EXECUTION_UNVERIFIED'
                result['notice']+=' Quote-based paper execution is EXECUTION_UNVERIFIED; source authentication, transaction validity and actual fills are not established.'
            return result
    except RecoveryRequired as exc:
        return {**result,'status':'RECOVERY_REQUIRED','recovery_reason':str(exc),
            'notice':'Paper ledger is incomplete or corrupt. Recovery is required; original records are preserved. No balances, positions or outcomes can be confirmed.'}
    except (sqlite3.Error,ValueError,KeyError,TypeError,OverflowError,UnicodeError,RecursionError):
        return {**result,'status':'LEDGER_UNAVAILABLE','notice':'Paper ledger could not be verified. No balances or outcomes are shown; inspect the local service and saved database.'}
