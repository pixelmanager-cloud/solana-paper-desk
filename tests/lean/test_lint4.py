"""SYNTHETIC_TEST_ONLY: LINT4 integration of L20 (sell_evidence + flow), L21 (risk_external), L22 (honeypot_sim), L23 (bot_exit) and
L24 (clusters) onto integration/r1. Real lean modules, only HTTP faked; no network, no credentials, nothing signed or sent.

* Lanes: ``lanes.helius_rps`` (absent = 10/s, unchanged) and the shipped 20/s x 0.35/0.20/0.45 split = main 7 / exit 4 / low 9 req/s,
  Jupiter unchanged; bad rates and shares above 1 are refused at load; the ``reserve_tokens`` yield still leaves a token at 9/s.
* Credits: the cost table (getAsset / getProgramAccounts 10, getTransactionsForAddress >= 10, others 1, RugCheck 0) and, over budget,
  EVERY low-lane data collector is shed (no request leaves) while screening, honeypot_sim (main lane) and exits still go out.
* Shutdown: the path recorder and all six auxiliary threads share ONE deadline (<= 10 s, systemd TimeoutStopSec=30).
* Production: the live r1 config (r1 example + the five production edits) loads unchanged with every new feature OFF, and adding exactly
  the enable-list blocks of the shipped example turns them all on.
"""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from lean import __main__ as entry, flow, ops, providers as P, runner as R, sell_evidence
from tests.lean.fakeworld import FakeTime, T0, Token, World
from tests.lean.test_e2e_held_risk import Scenario

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / 'config' / 'lean' / 'lean.example.json'
R1_EXAMPLE = ROOT / 'tests' / 'lean' / 'lean.r1-live.json'          # byte copy of the integration/r1 example (L20)
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}
NEW_KEYS = ('sell_evidence', 'flow', 'honeypot_sim', 'bot_exit', 'clusters', 'risk_external')


def production_r1():
    """The LIVE config: the integration/r1 example with exactly the five production edits."""
    raw = json.loads(R1_EXAMPLE.read_text())
    raw['strategy_config'] = str(ROOT / 'config' / 'lean' / 'strategy-default.json')
    raw['initial_cash_sol'] = '100'
    raw['screen']['min_age_seconds'] = 60
    raw['ops']['credit_budget_month'] = None
    raw['wallet_signals']['window_s'] = 60
    return raw


def write(raw, d, name='lean.json'):
    path = Path(d) / name
    path.write_text(json.dumps(raw))
    return path


class Lanes(unittest.TestCase):
    def setUp(self):
        P.configure_lanes()
        self.addCleanup(P.configure_lanes)

    def test_absent_helius_rps_keeps_todays_10_per_second(self):
        P.configure_lanes({'main': 0.55, 'exit': 0.25, 'low': 0.2})
        self.assertEqual(P.RATES['helius'], P.DEFAULT_RATES['helius'])
        self.assertEqual(P.lane_rates('helius'), {'main': 5.5, 'exit': 2.5, 'low': 2.0})
        with tempfile.TemporaryDirectory() as d:
            cfg = entry.load_config(write(production_r1(), d))
        self.assertIsNone(cfg['lanes']['helius_rps'])

    def test_the_shipped_example_gives_main_7_exit_4_low_9_and_leaves_jupiter_alone(self):
        cfg = entry.load_config(EXAMPLE)
        self.assertEqual(cfg['lanes']['helius_rps'], 20.0)
        self.assertEqual(cfg['lanes']['shares'], {'main': 0.35, 'exit': 0.2, 'low': 0.45})
        P.configure_lanes(cfg['lanes']['shares'], low_shed_s=cfg['lanes']['low_shed_s'], helius_rps=cfg['lanes']['helius_rps'])
        self.assertEqual(P.lane_rates('helius'), {'main': 7.0, 'exit': 4.0, 'low': 9.0})
        self.assertEqual(P.lane_rates('jupiter'), {'main': 1.4, 'exit': 0.8, 'low': 1.8})
        clock, sleep = (lambda: 0.0), (lambda s: None)
        low = P.shared_limiter('helius', 'low', clock=clock, sleep=sleep)
        self.assertEqual((low.rate, low.burst), (9.0, 9.0))
        self.assertEqual(P.shared_limiter('helius', 'exit', clock=clock, sleep=sleep).rate, 4.0)
        self.assertLessEqual(sum(P.lane_rates('helius').values()), 50.0)        # within the Developer plan's 50 RPS
        P.configure_lanes()                                                     # a reset drops the configured rate
        self.assertEqual(P.RATES['helius'], P.DEFAULT_RATES['helius'])

    def test_bad_rates_and_shares_above_one_are_refused_at_load(self):
        for bad in (0, -1, 501, True, 'twenty', float('nan'), float('inf')):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as d:
                raw = production_r1()
                raw['lanes']['helius_rps'] = bad
                text = json.dumps(raw)
                path = Path(d) / 'lean.json'
                path.write_text(text)
                with self.assertRaises(entry.ConfigError):
                    entry.load_config(path)
        with tempfile.TemporaryDirectory() as d:
            raw = production_r1()
            raw['lanes'].update(helius_rps=20, main=0.4, exit=0.2, low=0.45)    # 1.05
            with self.assertRaises(entry.ConfigError):
                entry.load_config(write(raw, d))
        self.assertEqual(P.RATES['helius'], P.DEFAULT_RATES['helius'])          # a refused config changes nothing

    def test_reserve_tokens_still_leaves_a_token_for_the_other_low_lane_users_at_9_per_second(self):
        """The L13/L20/L23/L24 collectors call only while 1 + reserve_tokens tokens are available (a peek, nothing taken). At the
        burst of 9 a reserve-1 collector (lean.flow.LowCalls, as flow and sell_evidence use it) takes 8 and then yields; another
        user still gets the last token."""
        for rps, share, expected in ((20, 0.45, 8), (10, 0.2, 1)):             # LINT4 example vs the r1 default (burst 2)
            with self.subTest(rps=rps):
                P.configure_lanes({'main': 0.35, 'exit': 0.2, 'low': 0.45} if rps == 20 else None, helius_rps=rps if rps == 20 else None)
                now = [0.0]
                sent = []

                def opener(request, timeout):
                    sent.append(json.loads(request.data)['method'])
                    from tests.lean.fakeworld import Resp
                    return Resp({'jsonrpc': '2.0', 'id': 1, 'result': 1})
                waits = []
                low = P.low(KEYS, opener=opener, clock=lambda: now[0], monotonic=lambda: now[0], sleep=lambda s: None)
                calls = flow.LowCalls(low.helius, limit=100, budget=flow.HourBudget(1000, lambda: now[0]), wait=waits.append,
                                      pace_s=0.5, patience_s=1.0, reserve=1)
                with self.assertRaises(flow.Stop) as stop:
                    for _ in range(100):
                        calls.rpc('getSlot', [])
                self.assertEqual(stop.exception.code, P.LANE_SHED)
                self.assertEqual(len(sent), expected)
                self.assertEqual(waits, [0.5, 0.5])                                # paced, never a busy loop
                low.helius.rpc('getSlot', [])                                       # the token left for the others
                self.assertEqual(len(sent), expected + 1)


class Credits(unittest.TestCase):
    def test_the_cost_table(self):
        cfg = entry.load_config(EXAMPLE)
        tracker = ops.CreditTracker(costs=cfg['ops']['credit_costs'], budget=cfg['ops']['credit_budget_month'], clock=lambda: T0)
        self.assertEqual(cfg['ops']['credit_budget_month'], 9_000_000)
        for endpoint, cost in (('das:getAsset', 10), ('rpc:getProgramAccounts', 10), ('rpc:getTransaction', 1),
                               ('rpc:getSignaturesForAddress', 1), ('rpc:getMultipleAccounts', 1), ('rpc:simulateTransaction', 1)):
            self.assertEqual(tracker.cost('helius', endpoint.split(':', 1)[1]), cost, endpoint)
        self.assertGreaterEqual(tracker.cost('helius', 'getTransactionsForAddress'), 10)
        self.assertEqual((tracker.cost('rugcheck', 'report'), tracker.cost('jupiter', 'quote')), (0, 0))
        before = tracker.credits
        tracker.observe_call('helius', 'das:getAsset', 1)
        tracker.observe_call('rugcheck', 'report', 1)
        self.assertEqual(tracker.credits - before, 10)
        defaults = ops.CreditTracker(costs=ops.load_ops_config({})['credit_costs'], clock=lambda: T0)      # no override at all
        self.assertEqual((defaults.cost('rugcheck', 'report'), defaults.cost('helius', 'getAsset')), (0, 10))
        # the same table without an ops.credit_costs override (production r1 has one for helius only): getAsset is 10 there too
        self.assertEqual(ops.load_ops_config({'credit_costs': {'helius': {'default': 1, 'getProgramAccounts': 10}}})['credit_costs']['helius']['getAsset'], 10)

    def test_over_budget_every_low_lane_collector_is_shed_and_screening_honeypot_and_exits_are_not(self):
        P.configure_lanes()
        self.addCleanup(P.configure_lanes)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp))
            token = Token(41)
            clock = FakeTime(T0)
            world = World(root, [token], clock)
            world.add_frames([T0])
            cfg = entry.load_config(EXAMPLE)
            cfg['ops'] = ops.load_ops_config({**json.loads(EXAMPLE.read_text())['ops'], 'credit_budget_month': 1000})
            r = entry.build_runner(cfg, state_dir=root / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='lint4',
                                   clock=clock.time, transport_kwargs={'opener': world.opener, 'clock': clock.time,
                                                                       'monotonic': clock.monotonic, 'sleep': clock.sleep})
            self.addCleanup(r.store.close)
            paths = r._l07r_build()
            collectors = {'paths': paths.helius, 'features': r.features.helius, 'wallet_signals': r.wallet_signals.helius,
                          'flow': r.flow.helius, 'clusters': r.clusters.helius, 'bot_exit': r.bot_exit.low.helius,
                          'sell_evidence': r.execution.sell_evidence.helius, 'held_risk': r.held_risk.low.helius}
            for name, client in collectors.items():
                self.assertEqual(client.transport.lane, 'low', name)
            self.assertEqual(r.providers.helius.transport.lane, 'main')                 # screening AND honeypot_sim (runner.providers)
            self.assertIsNotNone(r.honeypot)
            self.assertEqual(r.exit_providers.helius.transport.lane, 'exit')
            self.assertEqual(r.risk_external.client.transport.provider, 'rugcheck')     # its own bucket, never a Helius lane
            # over budget: 5000 credits in the last hour against a budget of 1000 a month
            r.ops.credits.first_seen = clock.time() - 3600
            r.ops.credits.record('helius', 'getAsset', 500)
            self.assertEqual(r.ops.credits.status()[1], 'SHEDDING')
            self.assertTrue(r.ops.shed_low_lane())
            sent = len(world.calls)
            for name, client in collectors.items():
                with self.assertRaises(P.ProviderError, msg=name) as shed:
                    client.rpc('getTokenLargestAccounts', [token.mint, {'commitment': 'confirmed'}])
                self.assertEqual(shed.exception.code, P.LANE_SHED, name)
            self.assertEqual(len(world.calls), sent, 'a shed low lane sent a request')
            r.providers.helius.rpc('getTokenLargestAccounts', [token.mint, {'commitment': 'confirmed'}])       # screening / honeypot
            r.exit_providers.helius.get_multiple_accounts([token.base_vault])                                # exit marks
            self.assertEqual(len(world.calls), sent + 2)
            health = r.ops.credits.health()
            self.assertEqual(set(health['shed_lanes']), {'low', 'paths', 'features', 'wallet_signals', 'flow', 'sell_evidence',
                                                          'bot_exit', 'clusters'})
            for never in ('main', 'exit', 'screen', 'honeypot_sim', 'risk_external'):
                self.assertTrue(r.ops.credits.allow(never), never)


class Hanging:
    """A worker whose thread ignores ``stop`` for a while (a slow in-flight request)."""

    def __init__(self, seconds):
        self.seconds = seconds

    def run(self, *args):
        time.sleep(self.seconds)

    def run_loop(self, *args):
        time.sleep(self.seconds)

    def resume(self):
        pass

    def health(self):
        return {'enabled': True}


class Shutdown(Scenario):
    def test_the_recorder_and_all_six_auxiliary_threads_share_one_deadline(self):
        r = self.build(Token(21))
        self.addCleanup(r.store.close)
        for name in ('features', 'wallet_signals', 'flow', 'bot_exit', 'clusters', 'risk_external'):
            setattr(r, name, Hanging(3.0))
        r._l07r_build = lambda: Hanging(3.0)
        r.stop.set()                                                       # the loops end at once; only the joins remain
        with mock.patch.object(R, 'AUX_JOIN_S', 0.4):
            began = time.monotonic()
            self.assertTrue(r.run(install_signals=False))
            elapsed = time.monotonic() - began
        self.assertLess(elapsed, 1.0, 'each thread got its own deadline (7 x 0.4 s)')
        self.assertEqual(set(r._aux_threads), {'features', 'wallet_signals', 'flow', 'bot_exit', 'clusters', 'risk_external'})
        self.assertTrue(all(t.daemon for t in list(r._aux_threads.values()) + [r._l07r_thread]))
        self.assertLessEqual(R.AUX_JOIN_S, 10.0)                            # well inside TimeoutStopSec=30
        health = r.health()
        for name in ('flow', 'bot_exit', 'clusters', 'risk_external'):
            self.assertTrue(health[name]['alive'], name)                    # still hanging: health says so

    def test_the_path_recorder_is_joined_too(self):
        r = self.build(Token(21))
        self.addCleanup(r.store.close)
        r._l07r_build = lambda: Hanging(0.2)
        r.stop.set()
        with mock.patch.object(R, 'AUX_JOIN_S', 2.0):
            r.run(install_signals=False)
        self.assertFalse(r._l07r_thread.is_alive())                         # waited for within the shared deadline

    def test_every_worker_gets_the_runner_stop(self):
        r = self.build(Token(21))
        self.addCleanup(r.store.close)
        for name in ('wallet_signals', 'flow', 'bot_exit', 'clusters'):
            setattr(r, name, Hanging(0.0))
        r.risk_external = Hanging(0.0)
        specs = {name: args for name, _target, args in r._aux_specs()}
        for name in ('wallet_signals', 'flow', 'bot_exit', 'clusters'):
            self.assertIs(getattr(r, name).stop, r.stop, name)
        self.assertEqual(specs['risk_external'], (r.stop,))
        self.assertEqual(specs['bot_exit'], (r,))


class ProductionConfig(unittest.TestCase):
    def test_the_live_config_loads_unchanged_with_every_new_feature_off(self):
        P.configure_lanes()
        self.addCleanup(P.configure_lanes)
        raw = production_r1()
        for key in NEW_KEYS:
            self.assertNotIn(key, raw)
        with tempfile.TemporaryDirectory() as d:
            cfg = entry.load_config(write(raw, d))
            self.assertFalse(cfg['sell_evidence']['enabled'])
            self.assertFalse(cfg['flow']['enabled'])
            self.assertFalse(cfg['honeypot_sim']['enabled'])
            self.assertFalse(cfg['risk_external']['enabled'])
            self.assertIsNone(cfg['bot_exit'])
            self.assertIsNone(cfg['clusters'])
            self.assertEqual(cfg['lanes']['shares'], {'main': 0.55, 'exit': 0.25, 'low': 0.2})
            self.assertIsNone(cfg['lanes']['helius_rps'])
            self.assertIsNone(cfg['ops']['credit_budget_month'])
            self.assertEqual((cfg['initial_cash_sol'], cfg['screen']['min_age_seconds']), ('100', 60))
            world = World(Path(os.path.realpath(d)), [Token(41)], FakeTime(T0))
            r = entry.build_runner(cfg, state_dir=Path(d) / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='lint4',
                                   transport_kwargs={'opener': world.opener})
            self.addCleanup(r.store.close)
            self.assertEqual(P.lane_rates('helius'), {'main': 5.5, 'exit': 2.5, 'low': 2.0})       # today's lanes
            self.assertEqual([n for n, _t, _a in r._aux_specs()], ['features', 'wallet_signals'])   # nothing new starts
            for name in ('flow', 'honeypot', 'bot_exit', 'clusters', 'risk_external'):
                self.assertIsNone(getattr(r, name), name)
            self.assertIsNone(r.execution.sell_evidence)
            health = r.health()
            self.assertEqual(health['sell_evidence'], {'enabled': False})
            self.assertIsNone(health['honeypot_sim'])
            for name in ('flow', 'bot_exit', 'clusters', 'risk_external'):
                self.assertEqual(health[name], {'enabled': False, 'alive': False}, name)

    def test_adding_exactly_the_enable_list_turns_everything_on(self):
        P.configure_lanes()
        self.addCleanup(P.configure_lanes)
        shipped = json.loads(EXAMPLE.read_text())
        raw = production_r1()
        for key in NEW_KEYS:                                                 # the six new blocks, verbatim
            raw[key] = copy.deepcopy(shipped[key])
        raw['lanes'] = copy.deepcopy(shipped['lanes'])                        # helius_rps 20, 0.35 / 0.20 / 0.45
        raw['ops']['credit_costs'] = copy.deepcopy(shipped['ops']['credit_costs'])
        raw['ops']['credit_budget_month'] = shipped['ops']['credit_budget_month']
        with tempfile.TemporaryDirectory() as d:
            cfg = entry.load_config(write(raw, d))
            self.assertEqual(cfg['sell_evidence']['unverified_sell_policy'], 'flag_only')
            world = World(Path(os.path.realpath(d)), [Token(41)], FakeTime(T0))
            r = entry.build_runner(cfg, state_dir=Path(d) / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='lint4',
                                   transport_kwargs={'opener': world.opener})
            self.addCleanup(r.store.close)
            self.assertEqual(P.lane_rates('helius'), {'main': 7.0, 'exit': 4.0, 'low': 9.0})
            self.assertEqual([n for n, _t, _a in r._aux_specs()],
                             ['features', 'wallet_signals', 'flow', 'clusters', 'bot_exit', 'risk_external'])
            self.assertIsNotNone(r.honeypot)
            self.assertIsNotNone(r.execution.sell_evidence)
            self.assertEqual(r.bot_exit.cfg.mode, 'shadow')
            self.assertIsNone(r.bot_exit.trigger('x', 1))                     # shadow never arms an exit
            self.assertEqual(r.ops.credits.budget, 9_000_000)

    def test_the_example_lists_every_key_and_l23_stays_shadow(self):
        raw = json.loads(EXAMPLE.read_text())
        self.assertEqual(set(raw) - {'_comment'}, set(entry.CONFIG_KEYS))
        self.assertEqual(raw['bot_exit']['mode'], 'shadow')
        pumpers = [rule for rule in raw['bot_exit']['rules'] if rule['name'] == 'PUMPERS_OUT'][0]['all']
        self.assertIn({'metric': 'tx_rate_ratio', 'op': '<', 'value': '0.6'}, pumpers)
        self.assertEqual(sell_evidence.config(raw['sell_evidence'])['unverified_sell_policy'], 'flag_only')


if __name__ == '__main__':
    unittest.main()
