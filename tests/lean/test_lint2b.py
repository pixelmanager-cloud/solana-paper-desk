"""SYNTHETIC_TEST_ONLY: LINT2b pre-deploy fixes that cross modules (runner shutdown + health, L11 account-check retry, the
example config, the low-lane starvation simulation). Real lean modules, only HTTP faked; no network, no credentials."""
import json
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from lean import providers, runner as R, wallet_signals
from lean.__main__ import load_config
from tests.lean import lint2b_starvation as SIM
from tests.lean.fakeworld import Token
from tests.lean.test_e2e_held_risk import Scenario
from tests.lean.test_e2e_real import ROOT, T0, steps


class Hanging:
    """A feature worker whose thread ignores ``stop`` for a while (a slow in-flight request)."""

    def __init__(self, seconds):
        self.seconds, self.started = seconds, threading.Event()

    def run(self, stop, *args):
        self.started.set()
        time.sleep(self.seconds)

    def run_loop(self):
        self.run(None)

    def health(self):
        return {'enabled': True, 'queued': 7}


class ShutdownAndHealth(Scenario):
    def test_the_auxiliary_threads_share_one_deadline(self):
        r = self.build(Token(21))
        self.addCleanup(r.store.close)
        r.features, r.wallet_signals = Hanging(3.0), Hanging(3.0)
        r.stop.set()                                                   # the loops end at once; only the joins remain
        with mock.patch.object(R, 'AUX_JOIN_S', 0.4):
            began = time.monotonic()
            r.run(install_signals=False)
            elapsed = time.monotonic() - began
        self.assertLess(elapsed, 0.75, 'each thread got its own deadline (2 x 0.4 s)')
        self.assertLessEqual(R.AUX_JOIN_S, 10.0)

    def test_health_says_whether_each_thread_is_alive_and_its_queue_depth(self):
        r = self.build(Token(21))
        self.addCleanup(r.store.close)
        r.features, r.wallet_signals = Hanging(0.6), Hanging(0.6)
        r.stop.set()
        with mock.patch.object(R, 'AUX_JOIN_S', 0.0):
            r.run(install_signals=False)
        health = r.health()
        self.assertEqual((health['features']['alive'], health['features']['queued']), (True, 7))
        self.assertTrue(health['wallet_signals']['alive'])
        time.sleep(0.8)
        self.assertFalse(r.health()['features']['alive'])
        r.features = r.wallet_signals = None
        self.assertEqual(r.health()['wallet_signals'], {'enabled': False, 'alive': False})


class AccountCheckRetry(Scenario):
    def held(self):
        r = self.build(Token(21, path=steps((0, 1.02))))
        self.addCleanup(r.store.close)
        self.run_until(r, 180)
        self.assertEqual(len(r.store.positions()), 1)
        return r

    def test_a_shed_account_check_is_retried_once_on_the_next_pass(self):
        r = self.held()
        hr = r.held_risk
        calls = []

        def shed(keys):
            calls.append(hr.passes)
            raise providers.ProviderError(providers.LANE_SHED, True, meta={'why': 'no_token'})
        hr.low.helius.get_multiple_accounts = shed
        for life in hr.lives.values():
            life.last_probe = self.clock.time()                       # no route probes in this test
        hr.passes = 0
        for _ in range(4):
            hr.after_exits(r)
        self.assertEqual(calls, [1, 2])                               # the scheduled check, ONE retry, then the next cycle
        self.assertEqual(hr.counts['account_retries'], 1)
        hr.passes = 5
        hr.after_exits(r)                                             # pass 6: not due, nothing pending
        hr.after_exits(r)                                             # pass 7: the next scheduled check
        self.assertEqual(calls, [1, 2, 7])


class ExampleConfig(unittest.TestCase):
    def test_pace_and_the_window_against_the_screen_age(self):
        cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        ws = wallet_signals.make_config(cfg['wallet_signals'])
        self.assertEqual(ws['pace_s'], 0.5)
        self.assertLessEqual(ws['window_s'], cfg['screen']['min_age_seconds'])   # else every row flags window_cut_by_decision
        self.assertEqual(ws['reserve_tokens'], 1)
        self.assertEqual(cfg['features']['shed_wait_s'], 10.0)


class Starvation(unittest.TestCase):
    """The deterministic low-lane simulation (tests/lean/lint2b_starvation.py; numbers in reports/LINT2b.md)."""

    def test_l13_does_not_make_path_gaps_or_l11_misses_worse(self):
        res = SIM.simulate(seeds=range(3), hours=12)
        without, with_l13 = res['LINT2b without L13'], res['LINT2b with L13 (pace 0.5, reserve 1)']
        gap = lambda m: m['path_gaps'] / m['path_due']
        self.assertLessEqual(gap(with_l13), gap(without) + 0.0005)
        self.assertLessEqual(with_l13['l11_missed'], without['l11_missed'] * 1.25 + 5)
        self.assertGreater(gap(res['r1 with L13 (pace 0.25, no reserve)']), gap(without) + 0.01)    # what r1 would have done
        self.assertLess(with_l13['l12_shed_rows'] / with_l13['l12_rows'], 0.05)
        self.assertLess(with_l13['l11_missed'], res['r1 with L13 (pace 0.25, no reserve)']['l11_missed'] / 3)


if __name__ == '__main__':
    unittest.main()
