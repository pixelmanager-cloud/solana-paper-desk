"""SYNTHETIC_TEST_ONLY actual persisted pacing/provisioning, no provider I/O."""
import contextlib,io,json,fcntl
from unittest.mock import patch
import unittest
from desk import paper_monitor_operator as op,paper_cycle_cli as cli,paper_read_sources as transport
from desk.monitoring_budget import MonitoringBudget
from desk.model import canonical
from tests import test_paper_cycle as fixtures
from tests import test_paper_cycle_cli as cli_fixtures
from tests.test_paper_read_sources import Response

class MonitorOperatorTests(unittest.TestCase):
 def case(self,entry=False):
  f=fixtures.PaperCycleTests();self.addCleanup(f.doCleanups);f.setUp()
  f.http_calls=[];f.sell_output=10_000_000
  if entry:self.assertEqual(f.actual_cycle()['status'],'COMPLETE')
  return f
 def call(self,f,fn,**kwargs):return fn(f.f.jobs.path,f.f.progress.store.path,f.path,f.cfg,**kwargs)
 def test_explicit_provision_once_and_lock_contention_never_resets(self):
  f=self.case()
  self.assertEqual(self.call(f,op.provision)['status'],'PROVISIONED')
  before=list(f.f.progress.store.connect().iterdump())
  with self.assertRaises(ValueError):self.call(f,op.provision)
  self.assertEqual(before,list(f.f.progress.store.connect().iterdump()))
  with open(str(f.path)+'.paper-cycle.lock','a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   with self.assertRaises(ValueError):self.call(f,op.preflight,clock=lambda:f.f.at)
  self.assertEqual(before,list(f.f.progress.store.connect().iterdump()))
 def test_unprovisioned_preflight_never_creates_budget(self):
  f=self.case();before=list(f.f.progress.store.connect().iterdump())
  with self.assertRaises(Exception):self.call(f,op.preflight,clock=lambda:f.f.at)
  self.assertEqual(before,list(f.f.progress.store.connect().iterdump()))
 def test_actual_five_read_guard_expiry_and_old_marks_without_polling_or_mutation(self):
  f=self.case(entry=True);self.call(f,op.provision)
  now=[f.f.at];budget=MonitoringBudget(f.f.progress.store,f.path,f.cfg,clock=lambda:now[0])
  source=transport.PaperReadSources(f.f.progress,f.target.scan_id,monitoring_budget=budget)
  class Opener:
   def open(self,request,*,timeout):return Response(canonical({'jsonrpc':'2.0','id':transport.RPC_ID,'result':110}).encode())
  with patch.object(transport,'build_opener',return_value=Opener()),patch.object(transport.os.environ,'get',return_value='SYNTHETIC_TEST_ONLY'),patch.object(transport.time,'time',side_effect=lambda:now[0]):
   for _ in range(56):source.rpc('getSlot',[{'commitment':'finalized'}],timeout_seconds=1)
  original_usage=f.f.progress.admission(f.target.scan_id)['requests_used']
  before=list(f.f.progress.store.connect().iterdump())
  result=self.call(f,op.preflight,clock=lambda:now[0]+11)
  self.assertEqual(result['required_pass_reads'],5);self.assertEqual(result['monitoring_budget']['remaining'],4)
  self.assertIn('MONITORING_PASS_ALLOWANCE_INSUFFICIENT',result['blockers'])
  self.assertFalse(result['saved_marks'][0]['fresh_by_age'])
  self.assertEqual(before,list(f.f.progress.store.connect().iterdump()))
  now[0]+=3600
  result=self.call(f,op.preflight,clock=lambda:now[0]);self.assertEqual(result['monitoring_budget']['remaining'],60)
  self.assertEqual(result['monitoring_budget']['total_used'],56)
  self.assertFalse(result['saved_marks'][0]['fresh_by_age']);self.assertEqual(before,list(f.f.progress.store.connect().iterdump()))
  self.assertEqual(f.f.progress.admission(f.target.scan_id)['requests_used'],original_usage)
 def test_cli_insufficient_allowance_refuses_before_credentials_and_preserves144(self):
  f=cli_fixtures.PaperCycleCliTests();self.addCleanup(f.doCleanups);f.setUp()
  pacing={'kind':'paper_monitoring_preflight_v1','status':'BLOCKED','blockers':['MONITORING_PASS_ALLOWANCE_INSUFFICIENT']}
  with patch.object(op,'preflight',return_value=pacing),patch.object(cli,'_credentials',side_effect=AssertionError('credential read')):
   code,result=f.invoke(*f.args(),'--monitoring','--systemd-credentials')
  self.assertEqual(code,2);self.assertEqual(result['monitoring_preflight'],pacing)
  self.assertFalse(list(f.root.glob('*.sqlite')))
  f.value['candidates']=[f.row()];f.save()
  with patch.object(op,'preflight',side_effect=AssertionError('must refuse first')):
   self.assertEqual(f.invoke(*f.args(),'--monitoring')[0],2)

if __name__=='__main__':unittest.main()
