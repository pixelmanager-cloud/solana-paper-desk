"""SYNTHETIC_TEST_ONLY: configurable whole-pass deadline (T24R F9). Fixtures only; no network, no provider keys.

The default stays the historical 10 s and an absent key changes nothing. A configured deadline is validated when the config
is loaded, bounded by the reviewed range, consistent with the rendered systemd TimeoutStartSec values, and an overrun is
the allow-listed CYCLE_DEADLINE_UNAVAILABLE (FAILED_CHARGED closure), never a latch.
"""
import json
import re
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from desk import paper_cycle as cycle, paper_pass_closure as closure
from tests.test_kraken_lifecycle import KrakenLifecycleTests, actual_cycle

ROOT = Path(__file__).resolve().parents[1]
FRESH = ROOT / 'deploy' / 'fresh'


class Progress:
    def admission(self, scan):
        return None


class DeadlineConfigTests(unittest.TestCase):
    def cfg(self, **extra):
        return {'mode': 'paper', **extra}

    def test_absent_key_is_the_historical_ten_seconds(self):
        self.assertEqual(cycle.deadline_seconds({}), 10)
        self.assertEqual((cycle.DEFAULT_DEADLINE_SECONDS, cycle.DEADLINE_RANGE), (10, (10, 20)))

    def test_valid_values_are_returned_unchanged(self):
        for value in range(10, 21):
            self.assertEqual(cycle.deadline_seconds({cycle.DEADLINE_KEY: value}), value)

    def test_everything_else_is_refused_at_load(self):
        for bad in (9, 21, 0, -10, 10.0, 15.5, '15', None, True, False, [15], {}, 2 ** 63, float('nan')):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'reviewed range'):
                cycle.deadline_seconds({cycle.DEADLINE_KEY: bad})

    def test_config_validation_runs_where_the_config_is_loaded(self):
        base = json.loads((ROOT / 'config' / 'paper.json').read_text())
        with patch.object(cycle, 'qe') as qe:
            qe.config.return_value = True
            cfg = dict(base, mode='paper', paper_signal_policy_version=3)
            cycle._config(cfg)
            cycle._config(dict(cfg, **{cycle.DEADLINE_KEY: 20}))
            with self.assertRaisesRegex(ValueError, 'reviewed range'):
                cycle._config(dict(cfg, **{cycle.DEADLINE_KEY: 99}))


class BudgetTests(unittest.TestCase):
    def budget(self, deadline=None, now=None):
        clock = [0.0]
        wall = lambda: 1_800_000_000
        mono = lambda: clock[0]
        pieces = (Progress(), wall, mono) + (() if deadline is None else (deadline,))
        return cycle._Budget(*pieces), clock

    def test_default_budget_expires_at_exactly_ten_seconds(self):
        budget, clock = self.budget()
        clock[0] = 9.999
        self.assertAlmostEqual(budget.remaining(), 0.001, places=6)
        clock[0] = 10.0
        with self.assertRaisesRegex(cycle.CycleBlocked, 'CYCLE_DEADLINE_UNAVAILABLE'):
            budget.remaining()

    def test_configured_budget_uses_its_own_deadline(self):
        budget, clock = self.budget(17)
        clock[0] = 16.5
        self.assertAlmostEqual(budget.remaining(), 0.5, places=6)
        clock[0] = 17.0
        with self.assertRaisesRegex(cycle.CycleBlocked, 'CYCLE_DEADLINE_UNAVAILABLE'):
            budget.remaining()

    def test_monotonic_clock_going_backwards_is_still_refused(self):
        budget, clock = self.budget(20)
        clock[0] = 5.0
        budget.remaining()
        clock[0] = 4.0
        with self.assertRaisesRegex(cycle.CycleBlocked, 'CYCLE_DEADLINE_UNAVAILABLE'):
            budget.remaining()

    def test_per_request_timeout_never_exceeds_the_sources_own_ceiling(self):
        '''A 20 s pass deadline must not hand a 20 s timeout to a source that refuses anything above 15 s.'''
        from desk import paper_read_sources
        seen = []
        budget, clock = self.budget(20)

        class P(Progress):
            def admission(self, scan):
                return {'state': 'ADMITTED', 'requests_used': 0, 'request_ceiling': 18}
        budget.progress = P()
        with self.assertRaises(Exception):
            budget.call('s', lambda timeout: seen.append(timeout) or (_ for _ in ()).throw(RuntimeError('stop')))
        self.assertEqual(seen, [cycle.REQUEST_TIMEOUT_MAX])
        self.assertEqual(cycle.REQUEST_TIMEOUT_MAX, 15)
        self.assertIn('<= 15', open(paper_read_sources.__file__).read())

    def test_an_overrun_is_on_the_closure_allow_list_not_a_latch(self):
        self.assertIn('CYCLE_DEADLINE_UNAVAILABLE', closure.TRANSIENT_CAUSES)


class WorstCaseTests(unittest.TestCase):
    """Known answers of worst_case_seconds with the T36 cadences (Helius 0.1, Jupiter 0.25, Kraken 2.0 s)."""

    def test_known_answers(self):
        # entry: 8 non-Kraken reads x (0.25 + L) + 1 Kraken read x (2.0 + L)
        self.assertAlmostEqual(cycle.worst_case_seconds('entry', 0.5), 8 * 0.75 + 2.5)
        self.assertAlmostEqual(cycle.worst_case_seconds('entry', 1.0), 8 * 1.25 + 3.0)
        self.assertAlmostEqual(cycle.worst_case_seconds('entry', 0.0), 8 * 0.25 + 2.0)
        self.assertAlmostEqual(cycle.worst_case_seconds('entry_usd_v1', 1.0), 6 * 1.25 + 3.0)
        self.assertAlmostEqual(cycle.worst_case_seconds('held_leg', 0.5), 5 * 0.75 + 2.5)

    def test_the_arithmetic_behind_the_ten_second_default_and_the_range(self):
        self.assertLess(cycle.worst_case_seconds('entry', 0.5), cycle.DEFAULT_DEADLINE_SECONDS)       # 8.5 s: fits with 0.5 s latency
        self.assertGreater(cycle.worst_case_seconds('entry', 1.0), cycle.DEFAULT_DEADLINE_SECONDS)    # 13 s: does not fit with 1.0 s
        self.assertLess(cycle.worst_case_seconds('entry', 1.7), cycle.DEADLINE_RANGE[1])              # 20 s ceiling covers ~1.78 s latency
        self.assertGreater(cycle.worst_case_seconds('entry', 1.9), cycle.DEADLINE_RANGE[1])

    def test_monotone_in_latency_and_in_pacing(self):
        values = [cycle.worst_case_seconds('entry', latency / 10) for latency in range(0, 30)]
        self.assertEqual(values, sorted(values))
        slower = dict(cycle.PACING_SECONDS, helius=2.0, jupiter=2.0)
        self.assertGreater(cycle.worst_case_seconds('entry', 0.5, slower), cycle.worst_case_seconds('entry', 0.5))

    def test_request_counts_match_the_reserve_in_the_preparation_intent(self):
        other, kraken = cycle.CYCLE_REQUESTS['entry']
        self.assertEqual(other + kraken, 9)      # history_preparation_rejection.intent: fresh_requests_reserved without USD v1
        other, kraken = cycle.CYCLE_REQUESTS['entry_usd_v1']
        self.assertEqual(other + kraken, 7)      # ... and with paper_usd_valuation_version 1
        self.assertEqual(cycle.PACING_SECONDS['kraken'], 2.0)


class RenderedUnitTests(unittest.TestCase):
    """The deadline must fit inside the systemd TimeoutStartSec values of the rendered units (a kill is a latch)."""

    def timeout(self, unit):
        return int(re.search(r'(?m)^TimeoutStartSec=(\d+)$', (FRESH / unit).read_text()).group(1))

    def test_held_unit_fits_every_leg_at_the_largest_deadline(self):
        positions = json.loads((ROOT / 'config' / 'paper.json').read_text())['max_positions']
        overhead = 20                                           # target export, accounting, interpreter start
        self.assertLessEqual(positions * cycle.DEADLINE_RANGE[1] + overhead, self.timeout('desk-paper-held-cycle.service'))

    def test_entry_unit_fits_preparation_plus_the_deadline(self):
        preparation = 18 + 2                                    # preparation_seconds + the pacing sleep between the phases
        self.assertLessEqual(preparation + cycle.DEADLINE_RANGE[1] + 60, self.timeout('desk-paper-entry-dispatcher.service'))

    def test_units_still_carry_the_timeouts_this_analysis_assumes(self):
        self.assertEqual((self.timeout('desk-paper-held-cycle.service'), self.timeout('desk-paper-entry-dispatcher.service'),
                          self.timeout('desk-decisions.service')), (120, 600, 60))


class LatencyLifecycleTests(KrakenLifecycleTests):
    """Real transport/collector/history/builder/engine path (synthetic wire bytes) with 1.6 s of monotonic time per provider response."""
    test_entry_held_exit_restart_accounting_and_originals = None
    test_entry_saved_valuation_tampering_and_missing_version_refuse_read_and_duplicate = None
    test_partial_full_exit_preserves_investigation_basis_and_monitoring_charges = None

    def slow_entry(self, cfg_extra):
        from desk import paper_cycle
        outer = self.h
        outer.cfg = outer.cfg | cfg_extra
        outer.path = Path(outer.f.tmp.name) / 'deadline-experiment.sqlite'
        paper_cycle.initialize(outer.path, outer.cfg)

        class Slow(list):
            def append(self, item):
                outer.f.tick += 1.6
                super().append(item)
        outer.http_calls = Slow()
        outer.f.tick = 1
        return actual_cycle(outer)

    def test_default_deadline_aborts_but_the_pass_is_closed_not_latched(self):
        result = self.slow_entry({})
        self.assertEqual(result['status'], 'BLOCKED', result)
        self.assertEqual(result['blockers'], ['CYCLE_DEADLINE_UNAVAILABLE'])
        with sqlite3.connect(self.h.f.progress.store.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()[0], 0)
            self.assertEqual([r[0] for r in c.execute('SELECT status FROM paper_pass_closures')], ['FAILED_CHARGED'])
        self.assertGreater(self.h.f.progress.admission(self.h.target.scan_id)['requests_used'], 0, 'charges stay charged')

    def test_a_configured_deadline_lets_the_same_slow_entry_complete(self):
        result = self.slow_entry({cycle.DEADLINE_KEY: 20})
        self.assertEqual(result['status'], 'COMPLETE', result['blockers'])
        self.assertEqual(result['attempted_requests'], 7)


if __name__ == '__main__':
    unittest.main()
