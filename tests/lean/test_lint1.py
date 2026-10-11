"""SYNTHETIC_TEST_ONLY: LINT1 integration fixes (real lean modules, only HTTP faked).

L11F x L10F  with the execution model on, the position pass returned from the two-phase exits early and the held-risk probes
             (NO_ROUTE / account checks) never ran.
L11F lanes   route probes and account checks ran serially on the EXIT lane (Jupiter 1 req/s, burst 1): N held positions
             stalled the position thread ~N-1 s every probe round. They now use the shared non-blocking LOW lane; a
             LANE_SHED sends nothing, records no error and the probe is retried next pass.
"""
import json
import unittest
from pathlib import Path

from lean import providers
from tests.lean.fakeworld import T0, Token
from tests.lean.test_e2e_held_risk import Scenario
from tests.lean import test_e2e_real as E2E
from tests.lean.test_e2e_real import ROOT, steps

EXECUTION = {'exec_delay_s': 3, 'priority_fee_sol': '0.0001', 'jito_tip_sol': '0.0001', 'ata_rent_sol': '0.00203928'}


class HeldRiskWithExecution(Scenario):
    def test_no_route_is_still_detected_when_the_two_phase_exits_run(self):
        token = Token(21, no_route_from=600)
        r = self.build(token)
        self.cfg['execution'] = dict(EXECUTION)
        r.store.close()
        r = self.runner()
        self.assertIsNotNone(r.execution)
        self.run_until(r, 1500)
        triggers = [e['reason'] for e in self.events(r, 'trigger')]
        self.assertEqual(triggers, ['NO_ROUTE'])                       # the probes ran after run_exits
        self.assertTrue(self.events(r, 'no_route'))


class HeldRiskOnTheLowLane(Scenario):
    def held(self, n):
        tokens = [Token(21 + 2 * i, path=steps((0, 1.02))) for i in range(n)]
        r = self.build(*tokens, ready=[T0 + 120 * i for i in range(n)])
        self.run_until(r, 120 * n + 60)
        self.assertEqual(len(r.store.positions()), n)
        return r

    def test_probes_use_the_low_lane(self):
        r = self.held(1)
        self.assertEqual(r.held_risk.low.jupiter.transport.lane, 'low')
        self.assertEqual(r.held_risk.low.helius.transport.lane, 'low')

    def test_a_probe_round_never_blocks_the_position_thread(self):
        r = self.held(4)
        for life in r.held_risk.lives.values():
            life.last_probe = None                                     # every position due at once
        r.held_risk.passes = 0                                         # and the account check too
        before = self.clock.time()
        r.held_risk.after_exits(r)
        self.assertEqual(self.clock.time(), before, 'the probe round slept (blocking lane)')

    def test_a_shed_probe_sends_nothing_records_no_error_and_is_retried(self):
        r = self.held(1)
        mint = next(iter(r.store.positions()))

        def shed(*_args, **_kwargs):
            raise providers.ProviderError(providers.LANE_SHED, True, meta={'why': 'no_token'})
        r.held_risk.low.jupiter.quote = shed
        life = next(iter(r.held_risk.lives.values()))
        life.last_probe = None
        errors_before = len(r.store.rows('errors', limit=100000))
        sent_before = len(self.world.jlog)
        r.held_risk.after_exits(r)
        self.assertEqual(len(self.world.jlog), sent_before)
        self.assertEqual(len(r.store.rows('errors', limit=100000)), errors_before)
        self.assertIsNone(life.last_probe)                             # due again on the next pass
        self.assertGreaterEqual(r.held_risk.counts['shed'], 1)
        self.assertIn('shed', json.loads(json.dumps(r.held_risk.health())))
        self.assertIn(mint, r.store.positions())


class CreditsSeeEveryLane(unittest.TestCase):
    """L15: the tracker metered only the main/exit bundles, so the low lane (the path recorder alone is ~2,160 Helius calls/h)
    was never counted, and its CREDIT_SHED only answered ``allow(lane)`` calls nobody made."""

    def ops(self, budget=None):
        from lean import ops
        o = ops.Ops(ops.load_ops_config({'credit_budget_month': budget}), clock=lambda: 0.0, notify=lambda m: False, environ={})
        o.meter(None)                                                   # registers the tracker as a call observer
        return o

    def tearDown(self):
        providers.configure_lanes()

    def test_a_low_lane_call_is_counted_and_a_shed_one_is_not(self):
        from tests.lean.test_low_lane import Clock, Opener, request, transport
        o, c = self.ops(), Clock()
        low = transport('helius', Opener(), c, 'low')
        low.call('rpc:getMultipleAccounts', request)
        self.assertEqual(o.credits.health()['calls'], {'helius': {'getMultipleAccounts': 1}})
        providers.shed_low('helius', 30, clock=c.now, sleep=c.sleep)
        with self.assertRaises(providers.ProviderError):
            low.call('rpc:getMultipleAccounts', request)                # nothing sent: no credit
        self.assertEqual(o.credits.health()['calls'], {'helius': {'getMultipleAccounts': 1}})

    def test_over_budget_the_shared_low_lane_is_shed_and_exits_are_not(self):
        from tests.lean.test_low_lane import Clock, Opener, request, transport
        o, c = self.ops(budget=1), Clock()
        o.lane_clock = {'clock': c.now, 'sleep': c.sleep}
        o.credits.shedding = True                                       # projection above the budget
        o.credits.status = lambda now=None: (10.0, 'SHEDDING')
        self.assertTrue(o.shed_low_lane())
        low, exits = transport('helius', Opener(), c, 'low'), transport('helius', Opener(), c, 'exit')
        with self.assertRaises(providers.ProviderError) as cm:
            low.call('rpc:getMultipleAccounts', request)
        self.assertEqual((cm.exception.code, cm.exception.meta['why']), ('LANE_SHED', 'backoff'))
        exits.call('rpc:getMultipleAccounts', request)                 # exits untouched
        self.assertEqual(c.sleeps, [])


class ShippedExampleConfig(unittest.TestCase):
    """config/lean/lean.example.json is the deploy template: every key, every merged feature ON, through the REAL loader."""
    EXAMPLE = ROOT / 'config' / 'lean' / 'lean.example.json'

    def test_the_real_loader_accepts_it_with_every_feature_on(self):
        from lean import __main__ as entry, execution, held_risk, watchlist
        raw = json.loads(self.EXAMPLE.read_text())
        self.assertEqual(set(raw) - {'_comment'}, set(entry.CONFIG_KEYS))            # every key the runner reads
        cfg = entry.load_config(self.EXAMPLE)
        self.assertTrue(cfg['paths'] and cfg['paths']['enabled'])
        self.assertEqual(cfg['route_check'], {'enabled': True})
        self.assertEqual(execution.ExecConfig.from_dict(cfg['execution']), execution.ExecConfig())
        self.assertTrue(held_risk.Config.from_dict(cfg['held_risk']).enabled)
        self.assertTrue(watchlist.WatchConfig.from_dict(cfg['watchlist']).enabled)
        self.assertEqual(cfg['ops']['credit_budget_month'], 10_000_000)            # ASSUMED Helius Developer plan
        self.assertTrue(Path(cfg['strategy_config']).is_file())

    def test_python_m_lean_once_accepts_it(self):
        import io
        import os
        import tempfile
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock
        from lean import __main__ as entry
        from tests.lean.fakeworld import FakeTime, World
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp))
            world = World(root, [Token(41), Token(43)], FakeTime(T0))
            world.add_frames([T0, T0])
            keys = root / 'provider-keys.json'
            keys.write_text(json.dumps({'HELIUS_API_KEY': 'TEST-HELIUS-KEY-0000', 'JUPITER_API_KEY': 'TEST-JUPITER-KEY-0000'}))
            keys.chmod(0o400)
            out, err = io.StringIO(), io.StringIO()
            with mock.patch('lean.providers.default_opener', world.opener), mock.patch('logging.basicConfig'), \
                    redirect_stdout(out), redirect_stderr(err):
                rc = entry.main(['--config', str(self.EXAMPLE), '--state-dir', str(root / 'state'), '--discovery-db',
                                 str(world.discovery_db), '--keys-file', str(keys), '--once'])
            self.assertEqual(rc, 0, err.getvalue())
            health = json.loads((root / 'state' / 'health.json').read_text())
            self.assertIsNone(health['halted'])
            self.assertTrue(health['paths']['enabled'])
            self.assertIsNotNone(health['held_risk'])
            self.assertEqual(health['ops']['credits']['budget_month'], 10_000_000)
            self.assertNotIn('TEST-HELIUS-KEY', out.getvalue() + err.getvalue() + json.dumps(health))


class EveryFeatureOnEndToEnd(E2E.EndToEndRealModulesTest):
    """The 20-candidate e2e scenario (helius outage, restart) with the SHIPPED example config: every merged feature at once.
    Invariants and position states are checked after every tick by ``run_scenario``; nothing halts."""
    test_twenty_candidates_end_to_end = None

    def setUp(self):
        super().setUp()
        from lean.__main__ import load_config
        self.cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')

    def test_all_features_together(self):
        r, _snaps = self.run_scenario()
        store = r.store
        fills = store.rows('fills', limit=10000)
        self.assertTrue([f for f in fills if f['side'] == 'buy'])
        self.assertTrue([f for f in fills if f['side'] == 'sell'])
        self.assertTrue(store.rows('decisions', kind='route_check', limit=10000))          # L16
        states = [json.loads(s['state']) for s in store.rows('position_state', limit=10000)]
        self.assertTrue([s for s in states if isinstance(s.get('execution'), dict)])        # L10
        self.assertIsNotNone(r.held_risk)                                                   # L11
        self.assertIsNotNone(r.watch)                                                       # L14
        self.assertGreater(r.ops.credits.health()['credits_used'], 0)                      # L15
        self.assertIsNone(r.halted)


if __name__ == '__main__':
    unittest.main()
