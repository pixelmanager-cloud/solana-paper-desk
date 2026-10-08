import copy,json,tempfile,unittest
from pathlib import Path
from desk.engine import initial_state,transition
from desk.ledger import Ledger
from desk.monitor import tick
from tests.helpers import config,event,T,control

class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'paper.sqlite';self.cfg=config()
    def seed(self):
        l=Ledger(self.path);l.apply(event(),self.cfg,transition,initial_state);state=l.report()['state'];l.close();return state
    def report(self):
        l=Ledger(self.path);r=l.report();l.close();return r
    def test_outage_expires_without_changing_cash_inventory_or_pnl(self):
        before=self.seed();r=tick(self.path,self.cfg,now=T+11);after=self.report()['state']
        self.assertEqual(r['status'],'MARKS_EXPIRED');self.assertEqual(after['mode'],'EXIT_ONLY')
        for key in ('cash','realized_pnl','day_start_equity','day'):self.assertEqual(before[key],after[key])
        for key in ('qty','cost_left','mark_value'):self.assertEqual(before['positions']['SYNTHETIC_A'][key],after['positions']['SYNTHETIC_A'][key])
        self.assertEqual(after['positions']['SYNTHETIC_A']['mark_status'],'STALE')
        self.assertFalse(any(x.get('side')=='sell' for x in r['outcomes']))
    def test_restart_is_idempotent_and_resume_cannot_bypass_expiry(self):
        self.seed();tick(self.path,self.cfg,now=T+11);before=self.report()
        self.assertEqual(tick(self.path,self.cfg,now=T+12)['status'],'NO_CHANGE')
        self.assertEqual(before['replay_hash'],self.report()['replay_hash'])
        l=Ledger(self.path);l.apply(control(T+13,'RESUME'),self.cfg,transition,initial_state);self.assertEqual(l.report()['state']['mode'],'EXIT_ONLY');l.close()
    def test_ttl_boundary_and_clock_regression_do_not_write(self):
        self.seed();before=self.report()['replay_hash']
        self.assertEqual(tick(self.path,self.cfg,now=T+10)['status'],'NO_CHANGE')
        self.assertEqual(tick(self.path,self.cfg,now=T-1)['status'],'CLOCK_BEHIND_LEDGER')
        self.assertEqual(before,self.report()['replay_hash'])
    def test_missing_ledger_is_not_created(self):
        self.assertEqual(tick(self.path,self.cfg,now=T)['status'],'NOT_CONFIGURED');self.assertFalse(self.path.exists())
    def test_configuration_change_rolls_back_expiry(self):
        self.seed();before=self.report()['replay_hash'];cfg=copy.deepcopy(self.cfg);cfg['fixed_fee_sol']='0.01'
        with self.assertRaises(ValueError):tick(self.path,cfg,now=T+11)
        self.assertEqual(before,self.report()['replay_hash'])
    def test_clock_cannot_carry_market_or_entry_assertions(self):
        clock={'schema_version':1,'event_id':'clock','ts':T,'kind':'clock','actor':'paper_monitor','data_healthy':True}
        with self.assertRaises(ValueError):transition(initial_state(self.cfg),clock,self.cfg)
    def test_midnight_outage_does_not_reset_daily_loss_baseline(self):
        self.seed();before=self.report()['state'];tick(self.path,self.cfg,now=T+86400);after=self.report()['state']
        self.assertEqual(before['day'],after['day']);self.assertEqual(before['day_start_equity'],after['day_start_equity'])

    def test_resume_after_midnight_cannot_reset_stale_daily_baseline(self):
        self.seed();tick(self.path,self.cfg,now=T+11);before=self.report()['state']
        ledger=Ledger(self.path);out=ledger.apply(control(T+86400,'RESUME'),self.cfg,transition,initial_state);after=ledger.report()['state'];ledger.close()
        self.assertEqual(before['day'],after['day']);self.assertEqual(before['day_start_equity'],after['day_start_equity'])
        self.assertEqual(after['mode'],'EXIT_ONLY');self.assertTrue(any(o.get('reason')=='DAY_ROLLOVER_DEFERRED_UNVERIFIED_MARKS' for o in out))
    def test_unverified_exit_evidence_cannot_refresh_mark_without_exit_trigger(self):
        before=self.seed();ledger=Ledger(self.path)
        out=ledger.apply(event(T+1,provenance='MAINNET_OBSERVATION'),self.cfg,transition,initial_state);after=ledger.report()['state'];ledger.close()
        position=after['positions']['SYNTHETIC_A'];self.assertEqual(position['mark_status'],'UNVERIFIED_EXIT')
        self.assertEqual(position['mark_at'],T);self.assertEqual(before['cash'],after['cash']);self.assertEqual(after['mode'],'EXIT_ONLY')
        self.assertTrue(any(o.get('reason')=='EXIT_SELLABILITY_UNVERIFIED' for o in out))
    def test_empty_portfolio_can_roll_daily_baseline_normally(self):
        state=initial_state(self.cfg);state['day']='2000-01-01';state['day_gross_losses']='1'
        state,out=transition(state,control(T,'PAUSE_ENTRY'),self.cfg)
        self.assertNotEqual(state['day'],'2000-01-01');self.assertEqual(state['day_gross_losses'],'0')
