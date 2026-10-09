"""SYNTHETIC_TEST_ONLY: executable runner config opt-in, never live acceptance."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from desk.model import decimal, digest
from desk.paper_runner import FixtureAdapter, initialize, run_once
from tests.helpers import ROOT, T, config, control, event
from tests.test_paper_experimental_scoring import experimental


class ExperimentalRunnerConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'paper.sqlite'
        self.cfg={**config(),'experimental_policy_version':1}
        self.fixture={'provenance':'SYNTHETIC_TEST_ONLY','events':[
            experimental(),experimental(ts=T+1),experimental(ts=T+2,danger=True)]}
        self.config_path=self.root/'config.json';self.config_path.write_text(json.dumps(self.cfg))
        self.fixture_path=self.root/'fixture.json';self.fixture_path.write_text(json.dumps(self.fixture))
        guard=patch('socket.socket',side_effect=AssertionError('Synthetic only'))
        guard.start();self.addCleanup(guard.stop)

    def records(self):
        with sqlite3.connect(self.path) as c:return list(c.iterdump())

    def cli(self,*args,cfg=None):
        return subprocess.run([sys.executable,'-m','desk.paper_runner','--config',str(cfg or self.config_path),*args],
            cwd=ROOT,env={'PATH':os.defpath,'PYTHONPATH':str(ROOT),'PYTHONDONTWRITEBYTECODE':'1'},
            capture_output=True,text=True,timeout=30)

    def test_actual_cli_entry_refresh_exit_restart_preserves_unknowns_and_risk(self):
        initialized=self.cli('init','--db',str(self.path))
        self.assertEqual(initialized.returncode,0,initialized.stderr)
        outcomes=[]
        for now in (T,T+1,T+2):
            result=self.cli('once','--db',str(self.path),'--fixture',str(self.fixture_path),'--now',str(now))
            self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads(result.stdout);outcomes.extend(report['outcomes'])
            self.assertFalse(report['automatic_entry_enabled'])
            self.assertEqual(report['provenance'],'SYNTHETIC_TEST_ONLY')
            if now==T+1:
                self.assertEqual(report['paper']['positions'][0]['mark_at'],T+1)
                self.assertFalse(any(row['type']=='fill' for row in report['outcomes']))
        buy=next(row for row in outcomes if row.get('side')=='buy')
        sell=next(row for row in outcomes if row.get('side')=='sell')
        self.assertEqual(buy['quantity'],sell['quantity'])
        self.assertEqual(sell['entry_policy'],buy['entry_policy'])
        self.assertEqual(buy['entry_policy']['risk_flags'],['UNRESOLVED_OWNERSHIP_HISTORY'])
        self.assertFalse(buy['entry_policy']['source_authenticated'])
        self.assertIsNone(buy['scores']['safety'])
        self.assertEqual(report['paper']['positions'],[])
        self.assertEqual(decimal(report['paper']['cash_sol']),decimal(self.cfg['initial_equity_sol'])+decimal(sell['realized_pnl_sol']))
        with sqlite3.connect(self.path) as c:
            for delivered in self.fixture['events']:
                saved=json.loads(c.execute('SELECT payload FROM events WHERE event_id=?',(delivered['event_id'],)).fetchone()[0])
                self.assertEqual(saved,delivered)
                self.assertTrue(all(saved[key] is None for key in ('top10_pct','dev_pct','bundle_pct','cluster_pct')))
            self.assertEqual(c.execute("SELECT value FROM metadata WHERE key='config_hash'").fetchone()[0],digest(self.cfg))
        before=self.records()
        restarted=self.cli('once','--db',str(self.path),'--fixture',str(self.fixture_path),'--now',str(T+2))
        self.assertEqual(restarted.returncode,0,restarted.stderr)
        self.assertEqual(json.loads(restarted.stdout)['outcomes'],[])
        self.assertEqual(self.records(),before)

    def test_fixture_json_cannot_opt_in_and_config_cannot_adopt_old_ledger(self):
        with self.assertRaises(ValueError):FixtureAdapter(self.fixture)
        with self.assertRaises(ValueError):FixtureAdapter(self.fixture,cfg=config())
        initialize(self.path,config());before=self.records()
        result=self.cli('once','--db',str(self.path),'--fixture',str(self.fixture_path),'--now',str(T))
        self.assertEqual(result.returncode,2)
        self.assertEqual(self.records(),before)
        opted=FixtureAdapter(self.fixture,cfg=self.cfg)
        with self.assertRaises(ValueError):run_once(self.path,self.cfg,opted,now=T)
        self.assertEqual(self.records(),before)

    def test_experimental_ledger_cannot_switch_back_to_strict_config(self):
        initialize(self.path,self.cfg)
        run_once(self.path,self.cfg,FixtureAdapter(self.fixture,cfg=self.cfg),now=T)
        before=self.records()
        strict=self.root/'strict.json';strict.write_text(json.dumps(config()))
        numeric=self.root/'numeric.json'
        numeric.write_text(json.dumps({'provenance':'SYNTHETIC_TEST_ONLY','events':[event(T+1)]}))
        result=self.cli('once','--db',str(self.path),'--fixture',str(numeric),'--now',str(T+1),cfg=strict)
        self.assertEqual(result.returncode,2)
        self.assertEqual(self.records(),before)

    def test_run_batch_uses_current_config_not_adapter_construction_permission(self):
        initialize(self.path,config());before=self.records()
        opted=FixtureAdapter(self.fixture,cfg=self.cfg)
        result=run_once(self.path,config(),opted,now=T)
        self.assertEqual(result['status'],'CANDIDATES_UNAVAILABLE')
        self.assertEqual(self.records(),before)

    def test_live_provenance_missing_metadata_and_invalid_versions_refuse(self):
        for version in (True,'1',2,0):
            with self.assertRaises(ValueError):FixtureAdapter(self.fixture,cfg={**config(),'experimental_policy_version':version})
        for changes in ({'provenance':'MAINNET_OBSERVATION'},{'provenance':'LIVE_PROVIDER'},{'paper_experimental':None}):
            bad=copy.deepcopy(self.fixture);bad['events'][0].update(changes)
            with self.assertRaises(ValueError):FixtureAdapter(bad,cfg=self.cfg)
        initialize(self.path,self.cfg);before=self.records()
        opted=FixtureAdapter(self.fixture,cfg=self.cfg)
        bad=experimental(provenance='MAINNET_OBSERVATION')
        with patch.object(opted,'candidates',return_value=[bad]):
            result=run_once(self.path,self.cfg,opted,now=T)
        self.assertEqual(result['status'],'CANDIDATES_UNAVAILABLE')
        self.assertEqual(self.records(),before)

    def test_experimental_outage_and_controls_do_not_invent_exit_or_permission(self):
        initialize(self.path,self.cfg)
        opted=FixtureAdapter(self.fixture,cfg=self.cfg)
        entered=run_once(self.path,self.cfg,opted,now=T)
        missing=FixtureAdapter({'provenance':'SYNTHETIC_TEST_ONLY','events':[]},cfg=self.cfg)
        expired=run_once(self.path,self.cfg,missing,now=T+11)
        self.assertEqual(expired['status'],'OBSERVATIONS_UNAVAILABLE')
        self.assertEqual(expired['paper']['strategy_mode'],'EXIT_ONLY')
        self.assertEqual(expired['paper']['cash_sol'],entered['paper']['cash_sol'])
        self.assertEqual(expired['paper']['positions'][0]['quantity'],entered['paper']['positions'][0]['quantity'])
        refreshed=FixtureAdapter({'provenance':'SYNTHETIC_TEST_ONLY','events':[experimental(ts=T+12)]},cfg=self.cfg)
        resumed=run_once(self.path,self.cfg,refreshed,now=T+12,controls=[control(T+12,'RESUME')])
        self.assertEqual(resumed['paper']['strategy_mode'],'EXIT_ONLY')
        self.assertFalse(resumed['automatic_entry_enabled'])
        self.assertFalse(any(row['type']=='fill' for row in resumed['outcomes']))
