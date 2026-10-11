"""SYNTHETIC_TEST_ONLY unless explicitly public; never retired-scan retry authority."""
import base64
import copy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import unittest
from unittest.mock import patch
from desk import quote_execution as qe, paper_cycle as cycle
from desk.model import canonical,digest,BOOST_VOLUME_FEATURE,BOOST_SIGNAL_PROFILE
from desk.live_observation import ProviderObservation,SourceRecord
from desk.boosted_paper import replay_pool,validate_quote
from tests import test_paper_pool_compatibility as pool_fixtures
from tests import test_token2022_paper as vertical_fixtures
from tests.test_paper_cycle import dump


class BoostPolicyTests(unittest.TestCase):
 def setUp(self):
  self.h=pool_fixtures.PoolCompatibilityTests();self.h.setUp();self.h.virtual=1000000
 def test_pricing_and_spendable_are_separate_originals_preserved(self):
  original=self.h.capture();saved=copy.deepcopy(original)
  checked=self.h.verify(2);self.assertTrue(checked['liquidity_control_verified'],checked)
  obs=self.h.ingest(original,2)
  self.assertEqual(obs.reserve_lamports,999700)
  self.assertEqual(obs.spot_sol_per_token,Decimal('0.002'))
  self.assertEqual(checked['effective_quote_reserve_raw'],'2000000')
  self.assertEqual(checked['boost_reserves_raw'],'1000300')
  self.assertEqual(original,saved)
  self.assertIn('BOOST_POOL_UNSUPPORTED',self.h.verify(1)['reasons'])
 def test_negative_boost_bounds_underflow_and_hazards_reject(self):
  for virtual in (-301,-2**127,2**127-1):
   self.h.virtual=virtual;self.assertFalse(self.h.verify(2)['liquidity_control_verified'])
  self.h.virtual=1000000;self.h.creator_fee=1000000
  self.assertFalse(self.h.verify(2)['liquidity_control_verified'])
  self.h.creator_fee=200
  for field in (243,244,261,269,270,300):
   old=self.h.f.raw;raw=bytearray(old+bytes(301-len(old)));raw[field]=1;self.h.f.raw=bytes(raw)
   self.assertFalse(self.h.verify(2)['liquidity_control_verified']);self.h.f.raw=old
 def test_explicit_optin_does_not_rewrite_old_config(self):
  from desk.token2022_paper import selected
  cfg={'mode':'paper','paper_signal_policy_version':3,'paper_quote_execution_version':1,'paper_token_profile_version':2}
  self.assertEqual(selected(cfg),2)
  for changed in ({'mode':'live'},{'paper_signal_policy_version':2},{'paper_quote_execution_version':0},{'paper_token_profile_version':True}):
   with self.assertRaises(ValueError):selected({**cfg,**changed})
 def test_sell_capacity_before_protocol_creator_and_haircut_one_unit_boundary(self):
  # Independently pin SDK rounded obligation, not a provider output comparison.
  from desk.sell_fees import standard_sell_amounts
  fees={'lp_fee_bps':100,'protocol_fee_bps':1000,'creator_fee_bps':1000}
  output=standard_sell_amounts(1000,1000,2000,fees,has_creator=True)
  obligation=int(output['gross_quote_raw'])-int(output['lp_fee_raw'])
  self.assertEqual(obligation,990);self.assertEqual(output['user_output_raw'],'790')
  # Original atomic mint/config/source replay is exercised separately below.
  from desk.boosted_paper import require_capacity
  self.assertEqual(require_capacity(output,990),990)
  with self.assertRaises(ValueError):require_capacity(output,989)


class BoostCycleTests(unittest.TestCase):
 def setUp(self):
  self.h=vertical_fixtures.VerticalProfileTests();self.h.profile=2;self.addCleanup(self.h.doCleanups);self.h.setUp()
  self.f=self.h.f;self.f.buy_output_raw=60_000_000;self.f.sell_output_per_unit_raw=170_000
  protocol=self.f.f.protocol
  raw=bytearray(protocol.raw+bytes(301-len(protocol.raw)));raw[245:261]=(10**11).to_bytes(16,'little',signed=True);protocol.raw=bytes(raw)
 def test_actual_entry_mark_exit_restart_accounting_and_original_boundaries(self):
  f=self.f;entry=f.actual_cycle();self.assertEqual(entry['status'],'COMPLETE',entry)
  buy=next(o for o in entry['outcomes'] if o.get('side')=='buy');refs=tuple(entry['usd_evidence_refs'])
  position=cycle._state(f.path,f.cfg)['positions'][f.target.mint]
  item=replace(f.item,target=replace(f.target,amount_raw=qe.raw_quantity(position['qty'],6)))
  mark=f.actual_cycle(positions=(item,),candidates=(),usd_refs=refs);self.assertEqual(mark['status'],'COMPLETE',mark)
  self.assertFalse(any(o.get('type')=='fill' for o in mark['outcomes']))
  f.sell_output_per_unit_raw=110_000
  exited=f.actual_cycle(positions=(item,),candidates=(),usd_refs=refs);self.assertEqual(exited['status'],'COMPLETE',exited)
  sell=next(o for o in exited['outcomes'] if o.get('side')=='sell')
  state=cycle._state(f.path,f.cfg);self.assertEqual(state['positions'],{})
  self.assertEqual(Decimal(state['cash']),Decimal(f.cfg['initial_equity_sol'])-Decimal(buy['amount_sol'])-Decimal(buy['fee_sol'])+Decimal(sell['proceeds_sol']))
  before=dump(f.path);restart=f.actual_cycle(candidates=(),usd_refs=refs);self.assertEqual(restart['attempted_requests'],0);self.assertEqual(dump(f.path),before)
  self.assertEqual(f.f.progress.admission(f.target.scan_id)['requests_used'],17)
  from desk.experiment_report import experiment_report
  self.assertIsNotNone(experiment_report(self.f.path,now=self.f.f.at))
 def test_closed_entry_reverse_proof_is_required_and_replayed(self):
  import sqlite3,json
  self.test_actual_entry_mark_exit_restart_accounting_and_original_boundaries()
  f=self.f
  with sqlite3.connect(f.path) as c:
   identity,payload=c.execute("SELECT rowid,payload FROM outcomes WHERE json_extract(payload,'$.side')='buy'").fetchone()
   original=json.loads(payload)
   self.assertIn('roundtrip_quote_execution',original)
  from desk.experiment_report import experiment_report
  from desk.paper_checkpoint import RecoveryRequired
  for change in ('missing','hash','quantity','cost'):
   forged=copy.deepcopy(original)
   if change=='missing':forged.pop('roundtrip_quote_execution')
   elif change=='hash':forged['roundtrip_quote_execution']['paper_pool_evidence']['raw_hash']='0'*64
   elif change=='quantity':forged['roundtrip_quote_execution']['input_raw']+=1
   else:forged['estimated_cost_fraction']='0'
   with sqlite3.connect(f.path) as c:c.execute('UPDATE outcomes SET payload=? WHERE rowid=?',(canonical(forged),identity))
   with self.assertRaises(RecoveryRequired):experiment_report(f.path,now=f.f.at)
   with self.assertRaisesRegex(RecoveryRequired,'CHECKPOINT_INVALID'):cycle._state(f.path,f.cfg)
  with sqlite3.connect(f.path) as c:c.execute('UPDATE outcomes SET payload=? WHERE rowid=?',(payload,identity))
  self.assertIsNotNone(experiment_report(f.path,now=f.f.at))
 def test_nonterminating_entry_cost_survives_actual_lifecycle_and_restart(self):
  # Odd lamports produce a nonterminating cost fraction; retain exact entry precision.
  self.f.target=replace(self.f.target,amount_raw=10_000_001)
  self.f.item=replace(self.f.item,target=self.f.target)
  self.test_actual_entry_mark_exit_restart_accounting_and_original_boundaries()
 def test_actual_acquisition_and_monitoring_share_original_charges(self):
  from desk.ownership_acquisition import acquire
  f=self.f;request=f.f.progress.store.load(f.item.graduation_refs[0]);rows=f.f.progress.store.load(request['response_hash'])['data']
  def rpc(method,params):
   if method=='getAccountInfo':return {'context':{'slot':100},'value':f.f.protocol.rpc('getMultipleAccounts',[])['value'][6]}
   if method=='getSlot':return 110
   if method=='getTransactionsForAddress':return {'data':rows,'paginationToken':None}
   raise AssertionError('Unexpected acquisition read')
  acquired=acquire(f.f.jobs.path,f.f.progress.store.path,rpc,scan_id=f.target.scan_id,paper_token_profile_version=2)
  self.assertEqual(acquired['provider_calls'],3);self.assertEqual(acquired['report']['token_policy']['decision'],'PASS_TOKEN_POLICY')
  source=f.f.jobs.source(f.target.scan_id)
  entry=f.actual_cycle();self.assertEqual(entry['status'],'COMPLETE',entry)
  self.assertEqual(f.f.progress.admission(f.target.scan_id)['requests_used'],12)
  p=cycle._state(f.path,f.cfg)['positions'][f.target.mint]
  item=replace(f.item,target=replace(f.target,amount_raw=qe.raw_quantity(p['qty'],6)),graduation_refs=())
  allowance=cycle.MonitoringBudget(f.f.progress.store,f.path,f.cfg,clock=lambda:f.f.at);allowance.provision()
  mark=f.actual_cycle(positions=(item,),candidates=(),monitoring=True);self.assertEqual(mark['status'],'COMPLETE',mark)
  f.sell_output_per_unit_raw=110_000
  closed=f.actual_cycle(positions=(item,),candidates=(),monitoring=True);self.assertEqual(closed['status'],'COMPLETE',closed)
  self.assertEqual(cycle._state(f.path,f.cfg)['positions'],{})
  self.assertEqual(f.f.progress.admission(f.target.scan_id)['requests_used'],12)
  self.assertEqual(allowance.snapshot()['total_used'],8)
  self.assertEqual(f.f.jobs.source(f.target.scan_id),source)
 def test_actual_partial_full_restart_exact_quote_inputs(self):
  f=self.f;entry=f.actual_cycle();self.assertEqual(entry['status'],'COMPLETE',entry);refs=tuple(entry['usd_evidence_refs'])
  position=cycle._state(f.path,f.cfg)['positions'][f.target.mint];raw=qe.raw_quantity(position['qty'],6)
  item=replace(f.item,target=replace(f.target,amount_raw=raw))
  poolraw=bytearray(f.f.protocol.raw);poolraw[245:261]=(500*10**9).to_bytes(16,'little',signed=True);f.f.protocol.raw=bytes(poolraw)
  f.sell_output_per_unit_raw=500_000
  partial=f.actual_cycle(positions=(item,),candidates=(),usd_refs=refs);self.assertEqual(partial['status'],'COMPLETE',partial)
  sold=next(o for o in partial['outcomes'] if o.get('side')=='sell');self.assertEqual(sold['quote_execution']['input_raw'],raw*3//10)
  remaining=cycle._state(f.path,f.cfg)['positions'][f.target.mint];self.assertEqual(qe.raw_quantity(remaining['qty'],6),raw-raw*3//10)
  f.sell_output_per_unit_raw=110_000
  item=replace(item,target=replace(item.target,amount_raw=qe.raw_quantity(remaining['qty'],6)))
  closed=f.actual_cycle(positions=(item,),candidates=(),usd_refs=refs);self.assertEqual(closed['status'],'COMPLETE',closed)
  self.assertEqual(cycle._state(f.path,f.cfg)['positions'],{})
  self.assertEqual(f.f.progress.admission(f.target.scan_id)['requests_used'],18)
  self.assertEqual(f.actual_cycle(candidates=())['attempted_requests'],0)
 def test_profile_feature_identity_physical_sizing_and_config_switch(self):
  f=self.f;entry=f.actual_cycle();self.assertEqual(entry['status'],'COMPLETE',entry)
  buy=next(o for o in entry['outcomes'] if o.get('side')=='buy')
  self.assertEqual(buy['entry_policy']['volume_feature'],BOOST_VOLUME_FEATURE)
  state=cycle._state(f.path,f.cfg);position=state['positions'][f.target.mint]
  self.assertEqual(position['entry_policy']['signal_profile']['name'],BOOST_SIGNAL_PROFILE)
  import sqlite3,json
  with sqlite3.connect(f.path) as c:event=json.loads(c.execute('SELECT payload FROM events WHERE event_id=?',(entry['events'][0],)).fetchone()[0])
  self.assertIsNone(event['volume_vs_liq']);self.assertIn(BOOST_VOLUME_FEATURE,event)
  self.assertEqual(Decimal(event['reserve_sol']),Decimal(100))
  self.assertEqual(Decimal(event['market_cap_usd']),Decimal(200000))
  before=dump(f.path)
  with self.assertRaises(cycle.CycleBlocked):cycle._state(f.path,{**f.cfg,'paper_token_profile_version':1})
  self.assertEqual(dump(f.path),before)
 def test_tampered_pool_source_and_history_subprofile_refuse_before_write(self):
  f=self.f;entry=f.actual_cycle();self.assertEqual(entry['status'],'COMPLETE',entry)
  state=cycle._state(f.path,f.cfg);position=state['positions'][f.target.mint]
  for mutate in ('hash','physical','name'):
   forged=copy.deepcopy(position)
   if mutate=='hash':forged['quote_execution']['paper_pool_evidence']['raw_hash']='0'*64
   elif mutate=='physical':forged['quote_execution']['paper_pool_evidence']['original_json']='{}'
   else:forged['quote_execution']['token_profile']['name']='old-profile'
   with self.assertRaises((ValueError,KeyError)):qe.validate_position(f.target.mint,forged,f.cfg)
 def test_detached_entry_pool_binding_and_stale_original_are_not_replayed(self):
  import sqlite3,json
  f=self.f;entry=f.actual_cycle();self.assertEqual(entry['status'],'COMPLETE',entry)
  with sqlite3.connect(f.path) as c:event=json.loads(c.execute('SELECT payload FROM events WHERE event_id=?',(entry['events'][0],)).fetchone()[0])
  for change in ('hash','slot','clock','basis','name','legacy_numeric'):
   forged=copy.deepcopy(event)
   if change=='hash':forged['paper_source_evidence']['pool_hash']='0'*64
   elif change=='slot':forged['paper_source_evidence']['pool_slot']+=1
   elif change=='clock':forged['ts']+=11
   elif change=='basis':forged['paper_signal_profile']['volume_formula']='invented'
   elif change=='name':forged['paper_signal_profile']['name']='observable-flow-churn-concentration-v1'
   else:forged['volume_vs_liq']='1'
   with self.assertRaises(ValueError):
    if change in ('hash','slot','clock'):replay_pool(forged,f.cfg)
    else:
     from desk.model import observable_signal_profile
     observable_signal_profile(forged,token_profile_version=2)
  from desk.model import observable_signal_profile
  for profile in (0,1):
   with self.assertRaises(ValueError):observable_signal_profile(event,token_profile_version=profile)
 def test_held_physical_capacity_failure_preserves_ledger_and_charged_originals(self):
  f=self.f;entry=f.actual_cycle();self.assertEqual(entry['status'],'COMPLETE',entry)
  p=cycle._state(f.path,f.cfg)['positions'][f.target.mint]
  item=replace(f.item,target=replace(f.target,amount_raw=qe.raw_quantity(p['qty'],6)))
  raw=bytearray(f.f.protocol.raw);raw[245:261]=(2*10**15).to_bytes(16,'little',signed=True);f.f.protocol.raw=bytes(raw)
  before=dump(f.path);used=f.f.progress.admission(f.target.scan_id)['requests_used']
  stopped=f.actual_cycle(positions=(item,),candidates=(),usd_refs=tuple(entry['usd_evidence_refs']))
  self.assertEqual(stopped['status'],'BLOCKED',stopped)
  self.assertFalse(any(o.get('type')=='fill' for o in stopped['outcomes']))
  self.assertEqual(dump(f.path),before)
  self.assertEqual(f.f.progress.admission(f.target.scan_id)['requests_used'],used+4)
  self.assertEqual(stopped['attempted_requests'],4)
  restart=f.actual_cycle(positions=(item,),candidates=())
  self.assertEqual(restart['status'],'RECOVERY_REQUIRED');self.assertEqual(restart['attempted_requests'],0)


class BoostHistoryTests(unittest.TestCase):
 def test_original_signed_event_pricing_denominator_is_new_measured_identity(self):
  from tests import test_live_strategy_features as fx
  from desk.live_strategy_features import calculate
  from desk.programs import unbase58
  from desk.security import base58
  row=fx.transaction()
  ix=row['transaction']['message']['instructions'][0]
  data=unbase58(ix['data']);fields=next(t['type']['fields'] for t in fx.SCHEMA['types'] if t['name']=='BuyEvent')
  offset=16
  for field in fields:
   t=field['type']
   if field['name']=='virtual_quote_reserves':
    data=data[:offset]+(1000).to_bytes(16,'little',signed=True)+data[offset+16:]
   if field['name']=='can_boost':data=data[:offset]+b'\x01'+data[offset+1:]
   size=32 if t=='pubkey' else 1 if t=='bool' else 4+int.from_bytes(data[offset:offset+4],'little') if t=='string' else int(t[1:])//8
   offset+=size
  ix['data']=base58(data);original=copy.deepcopy(row)
  args=dict(pool=fx.POOL,as_of=fx.T+2,provenance='SYNTHETIC_TEST_ONLY',history_pages=fx.history_pages([row]))
  old=calculate([row],**args);new=calculate([row],**args,token_profile_version=2)
  self.assertEqual(old['fields']['volume_vs_liq']['status'],'UNKNOWN')
  self.assertEqual(new['fields']['volume_vs_liq']['status'],'UNKNOWN')
  measured=new['fields'][BOOST_VOLUME_FEATURE]
  self.assertEqual(measured['status'],'MEASURED_WINDOW');self.assertEqual(Decimal(measured['value']),Decimal('0.05'))
  self.assertEqual(new['source_hashes'],[digest(row)]);self.assertEqual(new['reserve_basis'],'EFFECTIVE_PRICING_NOT_PHYSICAL_LIQUIDITY')
  self.assertNotEqual(new['manifest_hash'],old['manifest_hash']);self.assertEqual(row,original)
