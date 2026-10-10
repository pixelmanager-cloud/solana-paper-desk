"""Read a validated production round trip; no provider I/O or database mutation.
Run again after service/process restart and compare the evidence identity.
"""
import argparse,json,sqlite3
from contextlib import closing
from decimal import Decimal
from datetime import datetime,timezone
from desk.paper_cycle_cli import _config
from desk.paper_monitor_operator import _context
from desk.runtime_compatibility import implementation_hash
from desk.model import digest
from desk.quote_execution import raw_quantity
p=argparse.ArgumentParser();p.add_argument('--source',required=True);a=p.parse_args()
if implementation_hash()!=a.source:raise ValueError('Exact reviewed runtime required')
cfg=_config('/etc/solana-paper/paper-kraken-reviewed.json')
with _context('/var/lib/solana-desk/research.sqlite','/var/lib/solana-desk/evidence.sqlite','/var/lib/solana-desk/paper-kraken-77de75a2.sqlite',cfg) as (store,ledger,state):
 with closing(sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True)) as c:
  c.execute('BEGIN')
  if c.execute('SELECT count(*) FROM outcomes').fetchone()[0]>10000 or c.execute('SELECT 1 FROM outcomes WHERE length(CAST(payload AS BLOB))>1048576 LIMIT 1').fetchone():raise ValueError('Outcome bound')
  fills=[]
  for seq,event_id,payload in c.execute('SELECT seq,event_id,payload FROM outcomes ORDER BY seq'):
   v=json.loads(payload)
   if v.get('type')!='fill':continue
   if v.get('execution_status')!='EXECUTION_UNVERIFIED' or v.get('provenance')!='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE':raise ValueError('Live capture paper labels required')
   ts=c.execute('SELECT ts,payload FROM events WHERE event_id=?',(event_id,)).fetchone()
   if ts is None or ts[0]>state['last_ts']:raise ValueError('Checkpoint event binding')
   event=json.loads(ts[1])
   if event.get('mint')!=v['mint'] or event.get('provenance')!=v['provenance']:raise ValueError('Event identity mismatch')
   fills.append({'event_pool':event.get('pool'),'event_taker':event.get('taker'),'event_source_hash':digest(event),'seq':seq,'event_id':event_id,'timestamp':ts[0],'utc':datetime.fromtimestamp(ts[0],timezone.utc).isoformat(),**v})
  result={'status':'NO_COMPLETE_PAPER_CYCLE','fill_count':len(fills),'positions':len(state['positions']),'cash_sol':state['cash'],'realized_pnl_sol':state['realized_pnl'],'source':a.source}
  if fills:
   buy=fills[0]
   if buy['side']!='buy':raise ValueError('First fill must be BUY')
   mint=buy['mint'];trade=[x for x in fills if x['mint']==mint];sells=[x for x in trade if x['side']=='sell']
   if len([x for x in trade if x['side']=='buy'])!=1:raise ValueError('Multiple entries require separate round-trip attribution')
   if sells and mint not in state['positions']:
    decimals=buy['quote_execution']['mint_decimals']
    if any(x['event_pool']!=buy['event_pool'] or x['event_taker']!=buy['event_taker'] or x['quote_execution']['mint_decimals']!=decimals for x in sells):raise ValueError('Position identity mismatch')
    if sum(raw_quantity(x['quantity'],decimals) for x in sells)!=raw_quantity(buy['quantity'],decimals):raise ValueError('Exact raw inventory mismatch')
    quantity=Decimal(buy['quantity']);closed=sum((Decimal(x['quantity']) for x in sells),Decimal(0))
    if quantity<=0 or quantity!=closed or any(x['timestamp']<=buy['timestamp'] for x in sells):raise ValueError('Closed quantity/time mismatch')
    cost=Decimal(buy['amount_sol'])+Decimal(buy['fee_sol']);proceeds=sum((Decimal(x['proceeds_sol']) for x in sells),Decimal(0));pnl=sum((Decimal(x['realized_pnl_sol']) for x in sells),Decimal(0))
    if abs(proceeds-cost-pnl)>Decimal('0.00000000000000000001'):raise ValueError('Net fee-adjusted accounting mismatch')
    if len(fills)!=len(trade) or state['positions']:raise ValueError('Other trading activity requires separate attribution')
    if abs(Decimal(state['cash'])-Decimal(cfg['initial_equity_sol'])-pnl)>Decimal('0.00000000000000000001') or abs(Decimal(state['realized_pnl'])-pnl)>Decimal('0.00000000000000000001'):raise ValueError('Checkpoint cash/PnL mismatch')
    result.update(status='VALIDATED_LIVE_DATA_PAPER_ROUND_TRIP',mint=mint,buy=buy,sells=sells,initial_cost_sol=str(cost),net_proceeds_sol=str(proceeds),trade_net_pnl_sol=str(pnl),checkpoint_hash=digest(state),checkpoint_last_ts=state['last_ts'],trade_identity=digest({'buy':buy,'sells':sells}),restart_verified=False,monitoring_evidence_verified=False,profitability_established=False)
print(json.dumps(result,sort_keys=True))
