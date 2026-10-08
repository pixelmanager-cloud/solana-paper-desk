"""Read-only, bounded paper-ledger projection for the local dashboard."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from .model import decimal as dec,ZERO


def paper_status(path,*,now=None):
    now=int(time.time()) if now is None else now
    if type(now) is not int or now<0:raise ValueError('Invalid observation time')
    result={'mode':'PAPER_ONLY','automatic_entry_enabled':False,'runner_status':'NOT_CONNECTED',
        'status':'NOT_CONFIGURED','positions':[],'recent_outcomes':[],
        'cash_sol':None,'realized_pnl_sol':None,'estimated_equity_sol':None,
        'notice':'Simulated research accounting. Automatic entries remain disabled; paper results are not evidence of profitability.'}
    path=Path(path)
    if not path.is_file():return result
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as c:
            c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
            row=c.execute('SELECT payload FROM state WHERE id=1').fetchone()
            if not row:return {**result,'status':'EMPTY_LEDGER'}
            state=json.loads(row[0]);metadata=dict(c.execute("SELECT key,value FROM metadata WHERE key IN ('config','config_hash','implementation_hash')"))
            cfg=json.loads(metadata['config']);ttl=cfg['price_ttl_seconds']
            if type(ttl) is not int or ttl<=0:raise ValueError('Invalid price TTL')
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
            # One read transaction binds outcomes and state to the same checkpoint.
            outcomes=[{'event_id':r[0],'ts':r[1],'outcome':json.loads(r[2])} for r in c.execute(
                'SELECT o.event_id,e.ts,o.payload FROM outcomes o JOIN events e ON e.event_id=o.event_id ORDER BY o.seq DESC LIMIT 50')]
            result.update(status='LEDGER_PRESENT',strategy_mode=state['mode'],last_event_at=state['last_ts'],
                config_hash=metadata.get('config_hash'),implementation_hash=metadata.get('implementation_hash'),
                cash_sol=str(cash),realized_pnl_sol=str(realized),positions=positions,recent_outcomes=outcomes,
                estimated_equity_sol=str(cash+marks) if all_fresh else None,
                valuation_status='MODEL_ESTIMATE' if all_fresh else 'STALE_OR_EXIT_UNVERIFIED')
            return result
    except (sqlite3.Error,ValueError,KeyError,TypeError,OverflowError):
        return {**result,'status':'LEDGER_UNAVAILABLE','notice':'Paper ledger could not be verified. No balances or outcomes are shown; inspect the local service and saved database.'}
