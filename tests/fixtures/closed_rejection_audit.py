"""SYNTHETIC_TEST_ONLY closed-rejection audit, no runtime code patches.

Run from repository root: PYTHONPATH=. python tests/fixtures/closed_rejection_audit.py
Pinned baseline: 0dcc2117b094bfced0e92feebac9f2df47cfd1d4.
Uses existing actual-cycle transport/collector/decoder/adapter/engine/ledger
fixture with synthetic HTTP and explicit fixed clocks. PriceV3 fixture, not
Kraken timing or live acceptance. Transaction generation alone varies.
The restart is a zero-candidate cycle: no retry or extra provider requests.
Results are diagnostic observations, not assertions that perpetuate the bug.
"""
import json, sqlite3
from unittest.mock import patch
from tests import test_paper_cycle as f
original=f.transaction
for mode in ('sell_only','latest_slot_tie','one_buyer','stale_trade'):
 t=f.PaperCycleTests();t.setUp();t.http_calls=[];t.sell_output=10000000
 def tx(*args,**kwargs):
  a=list(args)
  if mode=='sell_only':kwargs['side']='sell'
  if mode=='latest_slot_tie':a[1]=100
  if mode=='one_buyer':kwargs['wallet']=f.base58(bytes([9])*32)
  if mode=='stale_trade':a[2]-=60
  return original(*a,**kwargs)
 try:
  with patch.object(f,'transaction',side_effect=tx):r=t.actual_cycle()
  with sqlite3.connect(t.f.progress.store.path) as c:passes=c.execute('SELECT outcome_hash IS NULL FROM paper_observation_passes').fetchall()
  if mode=='stale_trade':
   from desk import history_preparation_rejection as rejection
   with sqlite3.connect(t.f.progress.store.path) as c:hid=c.execute('SELECT id FROM ownership_history').fetchone()[0]
   cov=t.f.progress.snapshot(hid)['coverage']
   measured=rejection.bounds(t.f.progress.store,cov,cfg=t.cfg,as_of=t.f.at,semantics_version=2)
   print(json.dumps({'mode':mode,'preparation_bounds':measured,'preparation_reason':rejection.reason_for(measured,18,9,now=t.f.at,momentum_ttl=t.cfg['momentum_ttl_seconds'],exhausted=True,semantics_version=2)}))
  after=t.actual_cycle(candidates=())
  print(json.dumps({'mode':mode,'status':r['status'],'calls':r['attempted_requests'],'blockers':r['blockers'],'diagnostic_blockers':[d.get('blockers') for d in r['diagnostics'] if 'blockers' in d], 'outcomes':[{'type':o.get('type'),'reason':o.get('reason')} for o in r['outcomes']],'nulls':passes,'next_status':after['status'],'next_blockers':after['blockers'],'next_calls':after['attempted_requests']}))
 finally:t.doCleanups()
