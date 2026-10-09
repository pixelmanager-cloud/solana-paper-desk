"""Executable synthetic lifecycle, durable restart and failure boundaries."""
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

from desk.engine import initial_state, transition
from desk.ledger import Ledger
from desk.model import decimal
from desk.paper_runner import FixtureAdapter, initialize, run_once, MAX_EVENTS, MAX_FIXTURE_BYTES
from tests.helpers import T, config, control, event, ROOT


def adapter(*events):
    return FixtureAdapter({'provenance':'SYNTHETIC_TEST_ONLY','events':list(events)})


class PaperRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'new-paper.sqlite';self.cfg=config()
        initialize(self.path,self.cfg)
        self.network=patch('socket.socket',side_effect=AssertionError('No network permitted'))
        self.network.start();self.addCleanup(self.network.stop)

    def snapshot(self):
        with sqlite3.connect(self.path) as c:
            return list(c.iterdump())

    def state(self):
        ledger=Ledger(self.path,must_exist=True)
        try:return ledger.report()
        finally:ledger.close()

    def once(self,fixture,now=T,**kwargs):
        return run_once(self.path,self.cfg,fixture,now=now,**kwargs)

    def test_entry_refresh_full_exit_exact_accounting_restart_idempotence(self):
        fixture=FixtureAdapter.from_file(ROOT/'fixtures/paper_runner/lifecycle.json')
        entered=self.once(fixture)
        buy=next(o for o in entered['outcomes'] if o.get('side')=='buy')
        position=entered['paper']['positions'][0]
        self.assertEqual(position['provenance'],'SYNTHETIC_TEST_ONLY')
        self.assertEqual(position['mark_at'],T)
        refreshed=self.once(fixture,T+1)
        self.assertEqual(refreshed['paper']['positions'][0]['mark_at'],T+1)
        self.assertFalse(any(o['type']=='fill' for o in refreshed['outcomes']))
        exited=self.once(fixture,T+2)
        sell=next(o for o in exited['outcomes'] if o.get('side')=='sell')
        self.assertEqual(sell['quantity'],buy['quantity'])
        self.assertEqual(exited['paper']['positions'],[])
        self.assertEqual(exited['paper']['realized_pnl_sol'],sell['realized_pnl_sol'])
        self.assertEqual(decimal(exited['paper']['cash_sol']),decimal(self.cfg['initial_equity_sol'])+decimal(sell['realized_pnl_sol']))
        before=self.snapshot()
        replay=self.once(FixtureAdapter.from_file(ROOT/'fixtures/paper_runner/lifecycle.json'),T+2)
        self.assertEqual(replay['outcomes'],[])
        self.assertEqual(before,self.snapshot())
        self.assertFalse(exited['paper']['automatic_entry_enabled'])
        self.assertEqual(exited['paper']['runner_status'],'SYNTHETIC_CHECKPOINT_RECORDED')
        self.assertEqual(exited['paper']['runner_provenance'],'SYNTHETIC_TEST_ONLY')
        self.assertEqual(exited['paper']['runner_liveness'],'UNKNOWN')
        self.assertEqual(exited['paper']['last_run_evidence']['event_id'],
                         next(e['event_id'] for e in fixture.events if e['ts']==T+2))

    def test_existing_position_observed_before_candidate_regardless_of_fixture_order(self):
        self.once(adapter(event()))
        log=[]
        class Traced(FixtureAdapter):
            def observations(self,positions,now,limit):
                log.append(('observations',positions))
                return super().observations(positions,now,limit)
            def candidates(self,positions,now,limit):
                log.append(('candidates',positions))
                return super().candidates(positions,now,limit)
        fixture=Traced({'provenance':'SYNTHETIC_TEST_ONLY','events':[event(T+60,mint='SYNTHETIC_B'),event(T+60)]})
        result=self.once(fixture,T+60)
        self.assertEqual([item[0] for item in log],['observations','candidates'])
        self.assertEqual(result['delivered_event_ids'],[event(T+60)['event_id'],event(T+60,mint='SYNTHETIC_B')['event_id']])
        self.assertEqual(result['paper']['strategy_mode'],'RUNNING')
        self.assertEqual(len(result['paper']['positions']),2)

    def test_missing_observation_blocks_candidates_even_before_mark_ttl(self):
        entered=self.once(adapter(event()))
        fixture=adapter(event(T+1,mint='SYNTHETIC_B'))
        with patch.object(fixture,'candidates',side_effect=AssertionError('No candidate phase during missing observation')):
            result=self.once(fixture,T+1)
        self.assertEqual(result['status'],'OBSERVATIONS_UNAVAILABLE')
        self.assertEqual(result['paper']['cash_sol'],entered['paper']['cash_sol'])
        self.assertEqual(result['paper']['positions'],[{**entered['paper']['positions'][0],'mark_age_seconds':1}])

    def test_outage_clock_expires_without_inventory_cash_pnl_or_daily_reset(self):
        self.once(adapter(event()))
        before=self.state()['state']
        fixture=adapter()
        with patch.object(fixture,'observations',side_effect=OSError('SENSITIVE_FIXTURE_OUTAGE')):
            result=self.once(fixture,T+86400)
        after=self.state()['state']
        self.assertEqual(result['status'],'OBSERVATIONS_UNAVAILABLE')
        self.assertEqual(result['monitor']['status'],'MARKS_EXPIRED')
        for key in ('cash','realized_pnl','day_start_equity','day'):
            self.assertEqual(before[key],after[key])
        for key in ('qty','cost_left','mark_value'):
            self.assertEqual(before['positions']['SYNTHETIC_A'][key],after['positions']['SYNTHETIC_A'][key])
        self.assertEqual(after['mode'],'EXIT_ONLY')
        self.assertIsNone(result['paper']['estimated_equity_sol'])
        self.assertNotIn('SENSITIVE_FIXTURE_OUTAGE',json.dumps(result))
        self.assertFalse(any(o.get('side')=='sell' for o in result['monitor']['outcomes']))
        saved=self.snapshot();self.once(adapter(),T+86401)
        self.assertEqual(saved,self.snapshot())

    def test_pause_exit_only_and_resume_preserve_existing_controls(self):
        self.once(adapter(event()),controls=[control(T,'PAUSE_ENTRY')])
        self.assertEqual(self.state()['state']['positions'],{})
        self.once(adapter(event(T+60)),T+60,controls=[control(T+60,'RESUME')])
        self.assertTrue(self.state()['state']['positions'])
        result=self.once(adapter(event(T+61,danger=True)),T+61,controls=[control(T+61,'EXIT_ONLY')])
        self.assertEqual(result['paper']['positions'],[])
        self.assertTrue(any(o.get('side')=='sell' for o in result['outcomes']))
        self.assertEqual(result['paper']['strategy_mode'],'EXIT_ONLY')

    def test_resume_cannot_bypass_unresolved_exit_and_recovery_does_not_resume(self):
        self.once(adapter(event()))
        self.once(adapter(),T+11)
        result=self.once(adapter(event(T+12)),T+12,controls=[control(T+12,'RESUME')])
        self.assertEqual(result['paper']['strategy_mode'],'EXIT_ONLY')
        self.assertEqual(result['paper']['positions'][0]['mark_status'],'MODEL_ESTIMATE')
        self.assertEqual(result['paper']['positions'][0]['mark_at'],T+12)

    def test_stale_or_unverified_exit_does_not_fill_from_a_danger_assertion(self):
        self.once(adapter(event()))
        result=self.once(adapter(event(T+1,danger=True,sellability={})),T+1)
        self.assertEqual(result['paper']['strategy_mode'],'EXIT_ONLY')
        self.assertTrue(result['paper']['positions'])
        self.assertTrue(any(o['type']=='blocked_exit' for o in result['outcomes']))
        self.assertFalse(any(o.get('side')=='sell' for o in result['outcomes']))

    def test_clock_regression_does_not_call_adapter_or_write(self):
        self.once(adapter(event()))
        before=self.snapshot();fixture=adapter()
        with patch.object(fixture,'observations',side_effect=AssertionError('Clock behind')):
            self.assertEqual(self.once(fixture,T-1)['status'],'CLOCK_BEHIND_LEDGER')
        self.assertEqual(before,self.snapshot())

    def test_adapter_identity_freshness_count_and_payload_bounds_before_fills(self):
        for forged in (event(provenance='MAINNET_OBSERVATION'),event(provenance='live')):
            with self.assertRaises(ValueError):adapter(forged)
        with self.assertRaises(ValueError):self.once(object())
        for limit in (0,True,MAX_EVENTS+1):
            with self.assertRaises(ValueError):self.once(adapter(),limit=limit)
        fixture=adapter(event())
        for values in ([event(T-1)], [event(T+1)], [event()]*2,
                       [event(extra='x'*(MAX_FIXTURE_BYTES+1))]):
            before=self.snapshot()
            with patch.object(fixture,'candidates',return_value=values):
                result=self.once(fixture)
            self.assertEqual(result['status'],'CANDIDATES_UNAVAILABLE')
            self.assertEqual(before,self.snapshot())
        with self.assertRaises(ValueError):adapter(*[event(T+i) for i in range(MAX_EVENTS+1)])

    def test_controls_and_adapters_cannot_exceed_total_run_bound(self):
        fixture=adapter(event(),event(mint='SYNTHETIC_B'))
        result=self.once(fixture,limit=1)
        self.assertEqual(len(result['delivered_event_ids']),1)
        before=self.snapshot()
        with self.assertRaises(ValueError):self.once(adapter(),controls=(control(T,'RESUME') for _ in range(100)),limit=1)
        self.assertEqual(before,self.snapshot())

    def test_decision_and_risk_provenance_are_preserved_without_asserting_completeness(self):
        record=event(decision_provenance={'source_hash':'a'*64,'decision':'REJECT'},
                     paper_risk={'ownership_history':'UNRESOLVED','experimental':True})
        self.once(adapter(record))
        with sqlite3.connect(self.path) as c:
            saved=json.loads(c.execute('SELECT payload FROM events WHERE event_id=?',(record['event_id'],)).fetchone()[0])
        self.assertEqual(saved,record)
        self.assertEqual(saved['paper_risk']['ownership_history'],'UNRESOLVED')
        # Metadata is only preserved, not interpreted as policy or permission.
        self.assertIn('LIVE_FEATURE_ADAPTER_NOT_READY',transition(initial_state(self.cfg),event(provenance='MAINNET_OBSERVATION'),self.cfg)[1][0]['reasons'])

    def test_existing_legacy_changed_config_and_missing_checkpoint_never_migrate(self):
        before=self.snapshot()
        with self.assertRaises(FileExistsError):initialize(self.path,self.cfg)
        cfg=copy.deepcopy(self.cfg);cfg['fixed_fee_sol']='0.01'
        with self.assertRaises(ValueError):run_once(self.path,cfg,adapter(event()),now=T)
        self.assertEqual(before,self.snapshot())
        with sqlite3.connect(self.path) as c:c.execute("DELETE FROM metadata WHERE key='paper_runner'")
        before=self.snapshot()
        with self.assertRaises(ValueError):self.once(adapter(event()))
        self.assertEqual(before,self.snapshot())
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO metadata VALUES('paper_runner','SYNTHETIC_TEST_ONLY')")
            c.execute('DELETE FROM state')
        before=self.snapshot()
        with self.assertRaises(ValueError):self.once(adapter(event()))
        self.assertEqual(before,self.snapshot())

    def test_transition_crash_rolls_back_then_restart_redelivers(self):
        before=self.snapshot()
        def crash(state,record,cfg):
            transition(state,record,cfg)
            raise SystemExit('simulated precommit crash')
        with patch('desk.paper_runner.transition',side_effect=crash),self.assertRaises(SystemExit):
            self.once(adapter(event()))
        self.assertEqual(before,self.snapshot())
        self.assertTrue(self.once(adapter(event()))['paper']['positions'])

    def test_process_death_after_commit_restart_does_not_double_fill(self):
        child='''
import os,sys
from desk import paper_runner as r
from desk.model import load_config
original=r.Ledger.apply
def die(self,event,*args):
    result=original(self,event,*args)
    if event['kind']=='market':os._exit(73)
    return result
r.Ledger.apply=die
r.run_once(sys.argv[1],load_config('config/paper.json'),r.FixtureAdapter.from_file('fixtures/paper_runner/lifecycle.json'),now=int(sys.argv[2]))
'''
        process=subprocess.run([sys.executable,'-c',child,str(self.path),str(T)],cwd=ROOT,
                               env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,timeout=10)
        self.assertEqual(process.returncode,73)
        before=self.snapshot()
        result=self.once(FixtureAdapter.from_file(ROOT/'fixtures/paper_runner/lifecycle.json'))
        self.assertEqual(result['outcomes'],[])
        self.assertEqual(before,self.snapshot())
        self.assertEqual(len(self.state()['outcomes']),1)

    def test_cli_module_runs_new_experiment_and_complete_lifecycle(self):
        path=Path(self.tmp.name)/'cli-paper.sqlite'
        prefix=[sys.executable,'-m','desk.paper_runner','--config',str(ROOT/'config/paper.json')]
        def invoke(*args):
            return subprocess.run(prefix+list(args),cwd=ROOT,capture_output=True,text=True,timeout=10)
        self.assertEqual(invoke('init','--db',str(path)).returncode,0)
        self.assertEqual(invoke('init','--db',str(path)).returncode,2)
        for now in (T,T+1,T+2,T+2):
            result=invoke('once','--db',str(path),'--fixture',str(ROOT/'fixtures/paper_runner/lifecycle.json'),'--now',str(now))
            self.assertEqual(result.returncode,0,result.stderr)
            final=json.loads(result.stdout)
        self.assertEqual(final['paper']['positions'],[])
        self.assertEqual(final['outcomes'],[])
        self.assertEqual(final['provenance'],'SYNTHETIC_TEST_ONLY')


if __name__=='__main__':unittest.main()
