"""Synthetic accounting integration; never live entry authorization."""
import copy
import json
from pathlib import Path
import tempfile
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
        for version in (True, '1', 2, 0):
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
