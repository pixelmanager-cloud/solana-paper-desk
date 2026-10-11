"""Synthetic exporter/cycle integration and inactive unit contract."""
import json
import io
from contextlib import redirect_stdout
from dataclasses import replace
from desk import paper_cycle as cycle,quote_execution as qe
from pathlib import Path
import unittest
from unittest.mock import patch
from desk import paper_monitor_service as service
from tests import test_paper_target_export as exporter_fixtures
from tests import test_paper_cycle_cli as cli_fixtures

class MonitorServiceTests(unittest.TestCase):
 def args(self,f,cfg):return ['--config',str(cfg),'--research-db',str(f.research),'--evidence-db',str(f.evidence),
                            '--ledger-db',str(f.f.path),'--pool-fee-bps','25']
 def test_actual_exported_remaining_quantity_and_positions_only_invocation(self):
  f=exporter_fixtures.TargetExportTests();self.addCleanup(f.doCleanups);f.setUp()
  f.f.http_calls=[];f.f.sell_output=10_000_000
  self.assertEqual(f.f.actual_cycle()['status'],'COMPLETE')
  state=cycle._state(f.f.path,f.f.cfg);position=state['positions'][f.target.mint]
  initial=qe.raw_quantity(position['qty'],position['quote_execution']['mint_decimals'])
  item=replace(f.f.item,target=replace(f.target,amount_raw=initial))
  f.f.sell_output=20_000_000
  self.assertEqual(f.f.actual_cycle(positions=(item,),candidates=())['status'],'COMPLETE')
  position=cycle._state(f.f.path,f.f.cfg)['positions'][f.target.mint]
  remaining=qe.raw_quantity(position['qty'],position['quote_execution']['mint_decimals'])
  self.assertLess(remaining,initial)
  cfg=f.f.path.parent/'cfg.json';cfg.write_text(json.dumps(f.f.cfg))
  seen=[]
  def invoke(argv):
   targetfile=argv[argv.index('--targets')+1]
   payload=json.loads(Path(targetfile).read_text());seen.append(payload)
   self.assertEqual(payload['candidates'],[]);self.assertEqual(payload['usd_evidence_refs'],[])
   self.assertIn('--monitoring',argv);self.assertNotIn('provision-monitoring',argv)
   self.assertEqual(payload['position_targets'][0]['amount_raw'],remaining)
   return 2
  with patch.object(service.cli,'main',side_effect=invoke),patch('socket.socket',side_effect=AssertionError('provider')):
   self.assertEqual(service.main(self.args(f,cfg)),2)
  self.assertEqual(len(seen),1)
 def test_dependency_hold_stops_before_export_or_credentials(self):
  f=cli_fixtures.PaperCycleCliTests();self.addCleanup(f.doCleanups);f.setUp()
  args=['--config',str(f.config_path),'--research-db','missing-r','--evidence-db','missing-e',
        '--ledger-db','missing-l','--pool-fee-bps','25','--systemd-credentials','--dependency-blocker','RUNTIME_HOLD']
  with redirect_stdout(io.StringIO()),patch.object(service,'export_targets',side_effect=AssertionError('export')),patch.object(service.cli,'_credentials',side_effect=AssertionError('credentials')):
   self.assertEqual(service.main(args),2)
 def test_actual_empty_export_does_not_load_credentials_or_call_cycle(self):
  f=exporter_fixtures.TargetExportTests();self.addCleanup(f.doCleanups);f.setUp()
  cfg=f.f.path.parent/'cfg.json';cfg.write_text(json.dumps(f.f.cfg))
  with redirect_stdout(io.StringIO()),patch.object(service.cli,'main',side_effect=AssertionError('cycle')),patch.object(service.cli,'_credentials',side_effect=AssertionError('credentials')):
   self.assertEqual(service.main(self.args(f,cfg)+['--systemd-credentials']),0)
 def test_inactive_units_no_enable_retry_listener_or_provision(self):
  root=Path(__file__).resolve().parents[1]
  svc=(root/'deploy/desk-paper-held-cycle.service').read_text()
  timer=(root/'deploy/desk-paper-held-cycle.timer').read_text()
  for text in (svc,timer):self.assertNotIn('[Install]',text);self.assertNotIn('WantedBy=',text)
  self.assertIn('Type=oneshot',svc);self.assertIn('User=solana-desk',svc)
  self.assertIn('LoadCredential=',svc);self.assertIn('TimeoutStartSec=20',svc)
  self.assertIn('RUNTIME_REVIEW_AND_PATH_BINDINGS_REQUIRED',svc)
  for value in ('Restart=','provision-monitoring','--control','--port','--taker'):self.assertNotIn(value,svc)
  self.assertIn('OnUnitInactiveSec=5min',timer);self.assertIn('Persistent=false',timer)

if __name__=='__main__':unittest.main()
