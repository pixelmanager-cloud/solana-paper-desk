"""Existing durable acquisitions and actual captured fixture history; offline only."""
import io
import json
from pathlib import Path
import sqlite3
from dataclasses import replace
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from desk import paper_target_export as m
from desk.paper_cycle_cli import load_targets
from tests import test_paper_cycle as fixtures

class TargetExportTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.PaperCycleTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.target=self.f.target
        self.output=self.f.path.parent/'new-targets.json'
        self.candidate={'scan_id':self.target.scan_id,'pool':self.target.pool,'taker':self.target.taker,
                        'amount_raw':self.target.amount_raw,'provenance':'SYNTHETIC_TEST_ONLY','known_hazards':[]}
        self.research=self.f.f.jobs.path;self.evidence=self.f.f.progress.store.path
    def capture(self):
        old=self.f.f.progress.store.load(self.f.item.graduation_refs[0]);response=self.f.f.progress.store.load(old['response_hash'])
        key=self.f.f.progress.create(self.target.scan_id,self.target.pool,self.f.f.at-1000,self.f.f.at+1)
        before=self.f.f.progress.admission(self.target.scan_id)['requests_used']
        self.f.f.progress.advance(key,lambda method,params:response)
        self.assertEqual(self.f.f.progress.admission(self.target.scan_id)['requests_used'],before+1)
    def export(self,**kwargs):
        return m.export_targets(self.research,self.evidence,self.output,pool_fee_bps='25',
                                now=self.f.f.at,**({'candidate':self.candidate}|kwargs))
    def dump(self,path):
        with sqlite3.connect(path) as c:return list(c.iterdump())
    def test_candidate_actual_retained_capture_cli_handoff_and_no_database_writes(self):
        self.capture();before=[self.dump(p) for p in (self.research,self.evidence)]
        with patch('socket.socket',side_effect=AssertionError('No network')):result=self.export()
        self.assertEqual(result['status'],'EXPORTED');self.assertFalse(result['live_readiness'])
        positions,candidates,usd=load_targets(self.output)
        self.assertEqual(positions,());self.assertEqual(usd,())
        self.assertEqual(candidates[0].target,self.target);self.assertIsNone(candidates[0].graduated_at)
        self.assertTrue(candidates[0].graduation_refs)
        self.assertEqual(before,[self.dump(p) for p in (self.research,self.evidence)])
        saved=self.output.read_bytes()
        with self.assertRaises(m.ExportBlocked):self.export()
        self.assertEqual(self.output.read_bytes(),saved)
    def test_missing_migration_wrong_pool_and_missing_admission_refuse_output(self):
        with self.assertRaises(m.ExportBlocked):self.export()
        self.assertFalse(self.output.exists());self.capture()
        bad={**self.candidate,'pool':self.target.mint}
        with self.assertRaises(m.ExportBlocked):self.export(candidate=bad)
        with self.f.f.progress.store.connect() as c:c.execute('DELETE FROM ownership_admissions WHERE id=?',(self.target.scan_id,))
        with self.assertRaises(m.ExportBlocked):self.export()
        self.assertFalse(self.output.exists())
    def test_real_cli_explicit_inputs_and_blocked_diagnostics(self):
        argv=['--research-db',str(self.research),'--evidence-db',str(self.evidence),'--output',str(self.output),
              '--pool-fee-bps','25','candidate','--scan-id',self.target.scan_id,'--pool',self.target.pool,
              '--taker',self.target.taker,'--amount-raw',str(self.target.amount_raw),'--provenance','SYNTHETIC_TEST_ONLY']
        out=io.StringIO()
        with redirect_stdout(out):code=m.main(argv)
        self.assertEqual(code,2);self.assertIn('RETAINED_MIGRATION_WITNESS_REQUIRED',out.getvalue())
        self.capture()
        with redirect_stdout(io.StringIO()):self.assertEqual(m.main(argv),0)
    def test_positions_derive_original_scan_and_current_partial_raw_inventory(self):
        self.f.http_calls=[];self.f.sell_output=10_000_000
        first=self.f.actual_cycle(candidates=(self.f.item,))
        self.assertEqual(first['status'],'COMPLETE')
        self.f.f.at+=1;self.f.sell_output=30_000_000
        p=m._state(self.f.path,self.f.cfg)['positions'][self.target.mint]
        item=replace(self.f.item,target=replace(self.target,amount_raw=m.qe.raw_quantity(p['qty'],6)))
        second=self.f.actual_cycle(positions=(item,),candidates=(),usd_refs=tuple(first['usd_evidence_refs']))
        self.assertEqual(second['status'],'COMPLETE')
        state=m._state(self.f.path,self.f.cfg);p=state['positions'][self.target.mint]
        self.assertEqual(m.qe.raw_quantity(p['qty'],6),item.target.amount_raw-item.target.amount_raw*3//10)
        before=[self.dump(x) for x in (self.research,self.evidence,self.f.path)]
        result=m.export_targets(self.research,self.evidence,self.output,pool_fee_bps='25',ledger_db=self.f.path,cfg=self.f.cfg,now=self.f.f.at)
        self.assertEqual(result['positions'],1)
        positions,candidates,usd=load_targets(self.output)
        self.assertEqual(candidates,());self.assertEqual(positions[0].target.scan_id,self.target.scan_id)
        self.assertEqual(positions[0].target.amount_raw,m.qe.raw_quantity(p['qty'],p['quote_execution']['mint_decimals']))
        self.assertEqual(before,[self.dump(x) for x in (self.research,self.evidence,self.f.path)])

    def test_output_collision_atomic_publication_and_corrupt_checkpoint_refuse(self):
        self.capture()
        with patch.object(m.os,'link',side_effect=FileExistsError()):
            with self.assertRaises(FileExistsError):self.export()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.output.parent.glob('tmp*')),[])
        self.f.http_calls=[];self.f.sell_output=10_000_000
        self.f.actual_cycle(candidates=(self.f.item,))
        with sqlite3.connect(self.f.path) as c:
            state=json.loads(c.execute('SELECT payload FROM state').fetchone()[0])
            state['positions'][self.target.mint]['cost_left']='0'
            c.execute('UPDATE state SET payload=?',(m.canonical(state),))
        with self.assertRaises(ValueError):m.export_targets(self.research,self.evidence,self.output,pool_fee_bps='25',ledger_db=self.f.path,cfg=self.f.cfg)
        self.assertFalse(self.output.exists())

    def test_preflight_schema_and_query_bounds_before_replay(self):
        self.capture()
        with self.f.f.progress.store.connect() as c:
            c.execute('UPDATE ownership_history SET query=? WHERE budget=?',('x'*(1024*1024+1),self.target.scan_id))
        with patch.object(m,'replay_history',side_effect=AssertionError('Before body replay')):
            with self.assertRaisesRegex(m.ExportBlocked,'RETAINED_INPUT_BOUND_EXCEEDED'):self.export()
        self.assertFalse(self.output.exists())
        with self.f.f.jobs.connect() as c:c.execute("DELETE FROM scan_job_migrations WHERE name='legacy_screen_descriptors'")
        with self.assertRaisesRegex(m.ExportBlocked,'EXISTING_DISPATCH_SCHEMA_REQUIRED'):self.export()

    def test_positions_config_malformed_duplicate_and_oversized_before_databases(self):
        config=self.output.parent/'operator-config.json'
        argv=['--research-db',str(self.research),'--evidence-db',str(self.evidence),'--output',str(self.output),
              '--pool-fee-bps','25','positions','--ledger-db',str(self.f.path),'--config',str(config)]
        for body in ('[]','null','"not-config"','{"mode":"paper","mode":"paper"}','x'*65537):
            with self.subTest(body=body[:50]):
                config.write_text(body)
                with patch.object(m,'export_targets',side_effect=AssertionError('No database access')),redirect_stdout(io.StringIO()):
                    self.assertEqual(m.main(argv),2)
        self.assertFalse(self.output.exists())
