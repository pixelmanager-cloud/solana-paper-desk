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
        self.assertIn('<= 15', Path(paper_read_sources.__file__).read_text())

    def test_an_overrun_is_on_the_closure_allow_list_not_a_latch(self):
        self.assertIn('CYCLE_DEADLINE_UNAVAILABLE', closure.TRANSIENT_CAUSES)


class WorstCaseTests(unittest.TestCase):
    """Known answers of worst_case_seconds with the T36 cadences (Helius 0.1, Jupiter 0.25, Kraken 2.0 s), sized on the 18-read MAXIMUM."""

    def test_known_answers(self):
        # entry: 17 non-Kraken reads x (0.25 + L) + 1 Kraken read x (2.0 + L)
        self.assertAlmostEqual(cycle.worst_case_seconds('entry', 0.0), 17 * 0.25 + 2.0)
        self.assertAlmostEqual(cycle.worst_case_seconds('entry', 0.5), 17 * 0.75 + 2.5)
        self.assertAlmostEqual(cycle.worst_case_seconds('entry', 1.0), 17 * 1.25 + 3.0)
        self.assertAlmostEqual(cycle.worst_case_seconds('held_leg', 0.5), 17 * 0.75 + 2.5)
        # one contender queued on the shared pacer places one read in front of ours per cadence
        self.assertAlmostEqual(cycle.worst_case_seconds('entry', 0.5, contenders=1), 17 * (2 * 0.25 + 0.5) + (2 * 2.0 + 0.5))
        self.assertAlmostEqual(cycle.worst_case_seconds('entry', 0.5, contenders=2), 17 * (3 * 0.25 + 0.5) + (3 * 2.0 + 0.5))

    def test_the_arithmetic_behind_the_ten_second_default_and_the_range(self):
        deadline, ceiling = cycle.DEFAULT_DEADLINE_SECONDS, cycle.DEADLINE_RANGE[1]
        # no contention: 6.25 s of cadence + 18 L. The 10 s default only covers a MAXIMUM pass when responses take <= ~0.2 s ...
        self.assertLess(cycle.worst_case_seconds('entry', 0.2), deadline)
        self.assertGreater(cycle.worst_case_seconds('entry', 0.25), deadline)
        # ... and the 20 s ceiling when they take <= ~0.76 s. A typical pass reads far less than 18, and an overrun is the allow-listed
        # CYCLE_DEADLINE_UNAVAILABLE (FAILED_CHARGED, nothing latches), so these are bounds, not predictions.
        self.assertLess(cycle.worst_case_seconds('entry', 0.76), ceiling)
        self.assertGreater(cycle.worst_case_seconds('entry', 0.77), ceiling)
        # with the shared pacer contended the default cannot cover a maximum pass at all
        self.assertGreater(cycle.worst_case_seconds('entry', 0.0, contenders=cycle.CONTENDING_UNITS), deadline)

    def test_monotone_in_latency_in_pacing_and_in_contention(self):
        values = [cycle.worst_case_seconds('entry', latency / 10) for latency in range(0, 30)]
        self.assertEqual(values, sorted(values))
        slower = dict(cycle.PACING_SECONDS, helius=2.0, jupiter=2.0)
        self.assertGreater(cycle.worst_case_seconds('entry', 0.5, slower), cycle.worst_case_seconds('entry', 0.5))
        self.assertGreater(cycle.worst_case_seconds('entry', 0.5, contenders=1), cycle.worst_case_seconds('entry', 0.5))

    def test_request_counts_are_the_per_scan_maximum_not_the_preparation_reserve(self):
        from desk import history_progress
        for kind in ('entry', 'held_leg'):
            other, kraken = cycle.CYCLE_REQUESTS[kind]
            self.assertEqual((other + kraken, kraken), (18, 1), kind)
        self.assertEqual(set(cycle.CYCLE_REQUESTS), {'entry', 'held_leg'})      # the 9 / 7-read reserve variants are gone
        source = Path(history_progress.__file__).read_text()
        self.assertIn('def admit(self,identity,descriptor,ceiling=18)', source)  # the ceiling the 18 comes from
        self.assertEqual(cycle.PACING_SECONDS['kraken'], 2.0)

    def test_unit_bound_adds_contention_and_the_gate_calls(self):
        for kind in ('entry', 'held_leg'):
            expected = (cycle.worst_case_seconds(kind, 0.5, contenders=cycle.CONTENDING_UNITS)
                        + cycle.GATE_CALLS[kind] * cycle.GATE_SECONDS_BUDGET)
            self.assertAlmostEqual(cycle.unit_worst_case_seconds(kind, 0.5), expected)
        self.assertGreater(cycle.unit_worst_case_seconds('entry', 0.5), cycle.worst_case_seconds('entry', 0.5))


class RenderedUnitTests(unittest.TestCase):
    """The deadline must fit inside the systemd TimeoutStartSec values of the rendered units (a kill is a latch)."""

    def timeout(self, unit):
        return int(re.search(r'(?m)^TimeoutStartSec=(\d+)$', (FRESH / unit).read_text()).group(1))

    def test_held_unit_fits_every_leg_at_the_largest_deadline(self):
        positions = json.loads((ROOT / 'config' / 'paper.json').read_text())['max_positions']
        overhead = 20                                           # target export, accounting, interpreter start
        leg = cycle.DEADLINE_RANGE[1] + cycle.GATE_CALLS['held_leg'] * cycle.GATE_SECONDS_BUDGET   # the deadline caps the reads
        self.assertLessEqual(positions * leg + overhead, self.timeout('desk-paper-held-cycle.service'))

    def test_entry_unit_fits_preparation_plus_the_deadline_and_every_gate_call(self):
        preparation = 18 + 2                                    # preparation_seconds + the pacing sleep between the phases
        gates = cycle.GATE_CALLS['entry'] * cycle.GATE_SECONDS_BUDGET
        self.assertLessEqual(preparation + cycle.DEADLINE_RANGE[1] + gates + 60, self.timeout('desk-paper-entry-dispatcher.service'))

    def test_the_decisions_unit_does_no_provider_io_and_its_waits_fit_its_timeout(self):
        from desk import decision_runner
        unit = (FRESH / 'desk-decisions.service').read_text()
        self.assertIn('PrivateNetwork=true', unit)               # no provider reads: no pacer contention, no read deadline
        busy_timeout = 15                                        # decision_runner: PRAGMA busy_timeout=15000 for the consumer batch
        self.assertIn('busy_timeout=15000', Path(decision_runner.__file__).read_text())
        waits = decision_runner._JOURNAL_INIT_TIMEOUT + busy_timeout                 # its own deadlines, back to back
        self.assertLessEqual(waits + 20, self.timeout('desk-decisions.service'))      # + interpreter start and the bounded batch

    def test_the_pass_deadline_range_still_fits_the_unit_bound_with_gate_cost(self):
        self.assertLess(cycle.GATE_SECONDS_BUDGET * cycle.GATE_CALLS['entry'], 10)

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
