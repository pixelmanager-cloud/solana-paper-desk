"""Synthetic accounting integration; never live entry authorization."""
import copy
import json
from pathlib import Path
import tempfile
import sqlite3
import unittest

from desk.engine import initial_state, transition
from desk.ledger import Ledger
from tests.helpers import config, control, T
from tests.test_paper_experimental_scoring import experimental


class ExperimentalEngineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {**config(), 'experimental_policy_version': 1}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'paper.sqlite'

    def apply(self, e, cfg=None):
        ledger = Ledger(self.path)
        try:
            return ledger.apply(e, cfg or self.cfg, transition, initial_state)
        finally:
            ledger.close()

    def test_entry_exit_restart_preserves_unknown_risk_and_accounting(self):
        entry = experimental()
        before = copy.deepcopy(entry)
        bought = self.apply(entry)
        self.assertEqual(bought[0]['side'], 'buy')
        self.assertIsNone(bought[0]['scores']['safety'])
        self.assertIsNone(entry['top10_pct'])
        self.assertEqual(entry, before)
        policy = bought[0]['entry_policy']
        self.assertEqual(policy['risk_flags'], ['UNRESOLVED_OWNERSHIP_HISTORY'])
        self.assertFalse(policy['source_authenticated'])
        self.assertEqual(self.apply(entry), [])
        self.apply(control(T+1, 'LIQUIDATE'))
        sold = self.apply(experimental(ts=T+2))
        fill = next(x for x in sold if x.get('side') == 'sell')
        self.assertEqual(fill['entry_policy'], policy)
        self.assertEqual(self.apply(experimental(ts=T+2)), [])
        ledger = Ledger(self.path, must_exist=True)
        try:
            state = json.loads(ledger.db.execute('SELECT payload FROM state').fetchone()[0])
            self.assertEqual(state['positions'], {})
            self.assertEqual(state['realized_pnl'], fill['realized_pnl_sol'])
        finally:
            ledger.close()

    def test_event_json_cannot_opt_in_strict_default(self):
        with self.assertRaises(ValueError):
            transition(initial_state(config()), experimental(), config())

    def test_existing_experiment_cannot_switch_policy(self):
        self.apply(control(T, 'PAUSE_ENTRY'), cfg=config())
        with self.assertRaises(ValueError): self.apply(experimental(ts=T+1))

    def test_unsupported_config_rejects_before_mutating_state(self):
        for version in (True, '1', 3, 0):
            cfg={**config(), 'experimental_policy_version':version}
            state=initial_state(cfg); before=copy.deepcopy(state)
            with self.assertRaises(ValueError): transition(state, experimental(), cfg)
            self.assertEqual(state,before)

    def test_known_hazards_missing_inputs_and_live_source_still_block(self):
        for changes in ({'danger':True}, {'route_available':False},
                        {'mint_revoked':False}, {'sellability':None},
                        {'bundle_evidence':None}, {'provenance':'LIVE_PROVIDER'}):
            state=initial_state(self.cfg)
            state,out=transition(state,experimental(**changes),self.cfg)
            self.assertFalse(any(x['type']=='fill' for x in out),changes)
            self.assertEqual(state['positions'],{})
        bad=experimental();del bad['flow']
        with self.assertRaises(ValueError): transition(initial_state(self.cfg),bad,self.cfg)

    def test_outage_clock_preserves_position_and_blocks_fabricated_exit(self):
        self.apply(experimental())
        out=self.apply({'schema_version':1,'event_id':'outage','ts':T+20,'kind':'clock','actor':'paper_monitor'})
        self.assertTrue(any(x['type']=='blocked_exit' for x in out))
        self.assertFalse(any(x['type']=='fill' for x in out))

    def test_profile_two_omits_history_only_and_rejects_known_deployer_hazard(self):
        cfg = {**self.cfg, 'experimental_policy_version': 2}
        e = experimental()
        e['paper_experimental']['policy_version'] = 2
        for key in ('fresh_wallet_ratio', 'dev_launches_7d', 'manip_safety'):
            e[key] = None
            e['paper_experimental']['ownership_unknowns'][key] = {'status':'UNKNOWN','reasons':['HISTORY_UNAVAILABLE']}
        state, out = transition(initial_state(cfg), e, cfg)
        self.assertTrue(any(x.get('side') == 'buy' for x in out))
        self.assertIsNone(out[0]['scores']['safety'])
        self.assertEqual(out[0]['entry_policy']['policy_version'], 2)
        bad=copy.deepcopy(e);bad['dev_launches_7d']=3
        del bad['paper_experimental']['ownership_unknowns']['dev_launches_7d']
        state,out=transition(initial_state(cfg),bad,cfg)
        self.assertEqual(state['positions'],{})
        self.assertIn('REPEAT_DEPLOYER',out[0]['reasons'])
        bad=copy.deepcopy(e);bad['flow']=None
        with self.assertRaises(ValueError):transition(initial_state(cfg),bad,cfg)
        with self.assertRaises(ValueError):transition(initial_state(self.cfg),e,self.cfg)

    def test_restart_rejects_removed_or_promoted_policy_without_writes(self):
        from desk.paper_checkpoint import read_checkpoint
        for variant in ('missing','promoted','identity','scores'):
            with self.subTest(variant=variant):
                path=Path(self.tmp.name)/f'{variant}.sqlite'
                ledger=Ledger(path)
                ledger.apply(experimental(),self.cfg,transition,initial_state)
                state=json.loads(ledger.db.execute('SELECT payload FROM state').fetchone()[0])
                p=state['positions']['SYNTHETIC_A']
                if variant=='missing':del p['entry_policy']
                elif variant=='promoted':
                    p['entry_policy']['source_authenticated']=True
                    p['entry_policy']['entry_authorized']=True
                elif variant=='identity':p['entry_event_id']='missing-event'
                else:p['entry_scores']['safety']='100'
                ledger.db.execute('UPDATE state SET payload=?',(json.dumps(state),))
                before=list(ledger.db.iterdump())
                with self.assertRaises(ValueError):ledger.apply(experimental(ts=T+1,danger=True),self.cfg,transition,initial_state)
                with self.assertRaises(ValueError):read_checkpoint(ledger.db)
                self.assertEqual(list(ledger.db.iterdump()),before)
                ledger.close()

    def test_restart_rejects_nonentry_rebinding_and_conflicting_buy(self):
        from desk.paper_checkpoint import read_checkpoint
        for variant in ('nonentry', 'duplicate', 'quantity', 'policy'):
            with self.subTest(variant=variant):
                ledger=Ledger(Path(self.tmp.name)/f'buy-{variant}.sqlite')
                entry=experimental()
                ledger.apply(entry,self.cfg,transition,initial_state)
                if variant=='nonentry':
                    other=copy.deepcopy(entry);other['event_id']='nonentry-observation'
                    self.assertEqual(ledger.apply(other,self.cfg,transition,initial_state),[])
                    state=json.loads(ledger.db.execute('SELECT payload FROM state').fetchone()[0])
                    state['positions']['SYNTHETIC_A']['entry_event_id']=other['event_id']
                    ledger.db.execute('UPDATE state SET payload=?',(json.dumps(state),))
                else:
                    row=ledger.db.execute('SELECT payload FROM outcomes').fetchone()[0]
                    if variant=='duplicate':
                        ledger.db.execute('INSERT INTO outcomes(event_id,payload) VALUES(?,?)',(entry['event_id'],row))
                    else:
                        buy=json.loads(row)
                        if variant=='quantity':buy['quantity']='1'
                        else:buy['entry_policy']['risk_flags']=[]
                        ledger.db.execute('UPDATE outcomes SET payload=?',(json.dumps(buy),))
                before=list(ledger.db.iterdump())
                with self.assertRaises(ValueError):read_checkpoint(ledger.db)
                with self.assertRaises(ValueError):ledger.apply(entry,self.cfg,transition,initial_state)
                self.assertEqual(list(ledger.db.iterdump()),before)
                ledger.close()
