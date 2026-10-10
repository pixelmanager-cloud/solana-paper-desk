"""SYNTHETIC_TEST_ONLY: T16G. Held-leg latency while a REAL dispatcher entry runs, with the phase durations the code
budgets (acquisition 12 s, intake 10 s, preparation 18 s, cycle 12 s), plus 3-position restart behaviour.

The simulated clock advances only by those budgets (the wrappers below add each phase's duration after the real
phase function ran); `time.time` is never frozen for a phase. No network, credentials or provider.
"""
import contextlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from desk import paper_concurrency as pc, paper_cycle as cycle
from desk import paper_read_sources as transport
from tests import test_paper_concurrency_pipeline as pipe
from tools import paper_entry_dispatcher as tool

ACQ, INTAKE = pc.ACQUISITION_SECONDS, pc.INTAKE_SECONDS
PREP_CYCLE = pc.PREPARATION_SECONDS + pc.CYCLE_SECONDS
SLOW_CAPS = {'acquisition': 80.0, 'intake': 50.0}   # operator-raised caps: the T16G stress scenario predates the enforced caps


class Latency(pipe.ConcurrentPipeline):
    """Two clocks. `f.at` is the DATA clock of the synthetic wire fixtures (their trade times are tied to it, so it
    cannot move between phases of one dispatch). `self.sim` is the WALL clock the held cadence reads: it starts where
    the scheduler tick's held legs ended and advances only by the budgeted phase durations and by each held leg."""

    def setUp(self):
        super().setUp()
        self.real_run_once = cycle.run_once
        self.sim = None
        self.held_log = []            # (start, end, mint) on the wall clock

    def held_leg(self, mint, *, sell_output=82_000_000):
        with patch.object(cycle, 'run_once', self.real_run_once):    # dispatch() patches run_once for the ENTRY only
            return super().held_leg(mint, sell_output=sell_output)

    def append_second_candidate(self, seed=17):
        """A distinct migration per seed (the shared helper's default seed keeps its old single candidate)."""
        original = self.append_distinct_migration
        with patch.object(self, 'append_distinct_migration', lambda **kw: original(seed=seed, **kw)):
            return super().append_second_candidate()

    def tick(self):
        """The scheduler's held-first legs, as `_concurrent_tick` runs them (idle first so every mark goes stale)."""
        self.f.at += (60 - self.f.at % 60) + 5
        for mint in pc.held_order(cycle._state(self.ledger, self.cfg)['positions']):
            self.f.at += 8                                           # one leg is ~7.8 s of paced requests
            start = self.f.at
            self.held_leg(mint)
            self.held_log.append((start - 8, start, mint))
        self.sim = self.f.at

    def checkpoint_pass(self, ctx=None):
        for mint in pc.held_order(cycle._state(self.ledger, self.cfg)['positions']):
            self.held_log.append((self.sim, self.sim + 8, mint))
            self.sim += 8
            self.held_leg(mint)                                      # a REAL monitoring cycle, charged and answered
        return 0

    def timed_dispatch(self, *, intents=2, raw=None, acquisition=ACQ, intake=INTAKE, prep_cycle=PREP_CYCLE, checkpoints=True,
                       caps=None, real_held=False):
        outer = self
        acquire, intake_fn, execute = tool.acquisition.acquire, tool.migration.intake, tool.entry.execute
        cadence = tool._HeldCadence

        def slow(real, seconds):
            def run(*a, **k):
                result = real(*a, **k)
                outer.sim += seconds
                return result
            return run

        def slow_execute(*a, **k):
            outer.sim += prep_cycle        # preparation + cycle elapse BEFORE the engine decides
            outer.decision = outer.sim
            return execute(*a, **k)
        if real_held:                                   # the REAL _held_pass -> paper_monitor_service.main, in process
            held = contextlib.nullcontext()
        else:
            held = patch.object(tool, '_held_pass', side_effect=self.checkpoint_pass if checkpoints else (lambda ctx: 0))
        with (held, patch.object(tool.acquisition, 'acquire', slow(acquire, acquisition)),
              patch.object(tool.migration, 'intake', slow(intake_fn, intake)),
              patch.object(tool.entry, 'execute', slow_execute),
              patch.object(tool, '_HeldCadence', side_effect=lambda ctx: cadence(ctx, clock=lambda: outer.sim)),
              patch.dict(pc.PHASE_CAPS, caps or {}),
              patch.object(tool.concurrency, 'clock', side_effect=lambda: self.f.at)):
            return self.dispatch(intents=intents, raw=raw or self.second_raw)

    def max_gap(self):
        ends = sorted(end for _, end, _ in self.held_log) + [self.decision]
        return max(b - a for a, b in zip(ends, ends[1:]))


class RealisticPhases(Latency):
    def start_book(self):
        self.first_entry()
        self.append_second_candidate()
        self.tick()

    def test_realistic_entry_keeps_the_gap_inside_the_cadence_and_needs_no_extra_leg(self):
        self.start_book()
        legs_before = len(self.held_log)
        second = self.timed_dispatch()
        self.assertEqual((second['status'], second['paper_status']), ('DISPATCHED', 'COMPLETE'), second)
        self.assertEqual(len(self.held_log), legs_before, 'a 52 s entry fits inside the cadence; no checkpoint leg is spent')
        self.assertEqual(self.decision - self.held_log[-1][1], pc.ENTRY_SECONDS)       # every phase really elapsed
        self.assertLessEqual(self.max_gap(), pc.HELD_MAX_GAP_SECONDS)

    def test_slow_phases_force_a_checkpoint_leg_and_still_fill(self):
        """Pacing waits stretch acquisition to 70 s and intake to 40 s: a held position would go 140 s unmonitored; the
        checkpoint before the cycle bounds it by running a real leg between phases."""
        self.start_book()
        legs_before = len(self.held_log)
        second = self.timed_dispatch(acquisition=70, intake=40, caps=SLOW_CAPS)
        self.assertEqual((second['status'], second['paper_status']), ('DISPATCHED', 'COMPLETE'), second)
        self.assertEqual(len(cycle._state(self.ledger, self.cfg)['positions']), 2)
        self.assertEqual(len(self.held_log), legs_before + 1, 'exactly one checkpoint leg, before the cycle')
        self.assertLessEqual(self.max_gap(), pc.HELD_MAX_GAP_SECONDS)
        self.assertEqual(self.monitoring_rows(), (10, 10), 'the checkpoint leg is charged to monitoring, once')

    def test_without_the_checkpoint_the_same_entry_leaves_a_gap_over_the_cadence(self):
        self.start_book()
        self.timed_dispatch(acquisition=70, intake=40, checkpoints=False, caps=SLOW_CAPS)
        self.assertGreater(self.max_gap(), pc.HELD_MAX_GAP_SECONDS)

    def test_pure_rule(self):
        self.assertFalse(pc.checkpoint_due(10 ** 6, 10 ** 6, {}))          # flat books never need it
        self.assertTrue(pc.checkpoint_due(100, 30, {'A': 1}))
        self.assertFalse(pc.checkpoint_due(10, 30, {'A': 1}))
        self.assertTrue(pc.checkpoint_due(80, 30, {'A': 1, 'B': 1, 'C': 1}))   # legs are part of the gap: 80+30+23.4 > 120
        self.assertFalse(pc.checkpoint_due(60, 30, {'A': 1, 'B': 1, 'C': 1}))


class CheckpointTerminalResult(Latency):
    """T16H: a post-intent checkpoint (or an overrunning phase) ends the intent as a typed terminal NO_ENTRY."""
    def book(self):
        self.first_entry()
        self.append_second_candidate()
        self.tick()

    def journal_results(self):
        with sqlite3.connect(self.journal) as c:
            return [json.loads(p) for (p,) in c.execute('SELECT payload FROM results ORDER BY rowid')]

    def assert_not_orphaned(self, refused=None):
        """The dispatcher's own journal validation (no 'Unresolved dispatch') accepts the stores."""
        budget = tool.monitor.MonitoringBudget
        with patch.object(tool.time, 'time', side_effect=lambda: self.f.at), \
                patch.object(tool.monitor, 'MonitoringBudget',
                             side_effect=lambda store, ledger, cfg: budget(store, ledger, cfg, clock=lambda: max(self.f.at, pipe.REAL_TIME()))), \
                patch.object(tool.concurrency, 'clock', side_effect=lambda: self.f.at):
            if refused:      # journal validation (which raises 'Unresolved dispatch') runs BEFORE the gates that refuse here
                with self.assertRaisesRegex(ValueError, refused):
                    self.invoke()
            else:
                self.assertEqual(self.invoke()['status'], 'NO_CANDIDATE')

    def real_pass_with(self, side_effect=None, code=2):
        """paper_monitor_service.main runs for real (export, leg planning); only each leg's cycle CLI is replaced."""
        from desk import paper_monitor_service as service
        return patch.object(service.cli, 'main', side_effect=side_effect or (lambda argv: code))

    def test_blocked_leg_in_the_real_held_pass_ends_as_terminal_no_entry_with_the_code_surfaced(self):
        self.book()
        before = (self.charges(), self.fills() if hasattr(self, 'fills') else None)
        with self.real_pass_with(code=2):
            result = self.timed_dispatch(acquisition=70, intake=40, caps=SLOW_CAPS, real_held=True)
        self.assertEqual((result['status'], result['paper_status']), ('DISPATCHED', 'NO_ENTRY'), result)
        self.assertEqual(result['checkpoint_no_entry']['reasons'], ['HELD_PASS_NONZERO'])
        self.assertEqual(result['checkpoint_no_entry']['held_pass'], {'ran': True, 'exit_code': 2})
        self.assertEqual(result['checkpoint_no_entry']['phase'], 'intake')     # 70 s of acquisition + legs would break the gap
        rows = self.journal_results()
        self.assertEqual(rows[-1]['result']['kind'], tool.CHECKPOINT_KIND)
        self.assertEqual(rows[-1]['result']['held_pass']['exit_code'], 2)
        self.assertEqual(len(cycle._state(self.ledger, self.cfg)['positions']), 1)
        self.assert_not_orphaned()
        self.assertGreater(self.charges()[self.entry_scan()], 0, 'the acquisition and intake charges stay charged')
        # not a latch: with the held pass healthy the next candidate enters normally
        self.append_second_candidate(seed=44)
        self.tick()
        again = self.timed_dispatch(intents=3, raw=self.second_raw)
        self.assertEqual((again['status'], again['paper_status']), ('DISPATCHED', 'COMPLETE'), again)

    def entry_scan(self):
        return list(self.charges())[-1]

    def test_a_leg_that_moves_the_mode_ends_as_terminal_no_entry(self):
        self.book()
        from desk.engine import initial_state, transition
        from desk.ledger import Ledger
        from tests.helpers import control

        def pause(argv):
            ledger = Ledger(self.ledger, must_exist=True)
            try:
                ledger.apply(control(int(self.f.at) + 7, 'EXIT_ONLY'), self.cfg, transition, initial_state)
            finally:
                ledger.close()
            return 0
        with self.real_pass_with(side_effect=pause):
            result = self.timed_dispatch(acquisition=70, intake=40, caps=SLOW_CAPS, real_held=True)
        self.assertEqual(result['paper_status'], 'NO_ENTRY', result)
        self.assertEqual(result['checkpoint_no_entry']['reasons'], ['LEDGER_MODE_NOT_RUNNING'])
        self.assertEqual(self.journal_results()[-1]['result']['mode'], 'EXIT_ONLY')
        self.assert_not_orphaned(refused='paused ledger')

    def test_gate_reasons_for_unresolved_exit_and_blocked_monitoring(self):
        from contextlib import contextmanager
        cadence = tool._HeldCadence(self.expected_context() if hasattr(self, 'expected_context') else tool.plan(**self.args))
        blocked = {'mode': 'RUNNING', 'positions': {'A': {'exit_blocked': 'EXACT_FRESH_SELL_QUOTE_REQUIRED', 'mark_status': 'UNVERIFIED_EXIT'}}}

        @contextmanager
        def context(*args):
            yield object(), object(), blocked
        class Budget:
            def __init__(self, *a):
                pass
            def snapshot(self):
                return {'status': 'AVAILABLE', 'blockers': ['MONITORING_OUTCOME_PENDING']}
        with patch.object(tool.monitor, '_context', context), patch.object(tool.monitor, 'MonitoringBudget', Budget):
            halt = cadence.gate(self.cfg, 0)
        self.assertEqual(sorted(halt['reasons']), ['HELD_EXIT_UNRESOLVED', 'MONITORING_BLOCKED'])
        self.assertEqual(halt['exit_code'], 0)

    def test_the_checkpoint_before_the_cycle_halts_with_the_preparation_phase(self):
        self.book()
        with self.real_pass_with(code=2):
            result = self.timed_dispatch(acquisition=45, intake=40, caps={'acquisition': 80.0, 'intake': 50.0}, real_held=True)
        self.assertEqual((result['paper_status'], result['checkpoint_no_entry']['phase'], result['checkpoint_no_entry']['reasons']),
                         ('NO_ENTRY', 'preparation', ['HELD_PASS_NONZERO']))
        self.assert_not_orphaned()

    def test_checkpoint_record_binding_and_shape_are_verified(self):
        intent = {'version': 1, 'at': 1.0}
        halt = {'reasons': ['HELD_PASS_NONZERO'], 'ran': True, 'exit_code': 2, 'mode': 'RUNNING'}
        good = tool.checkpoint_record('a' * 32, intent, 'b' * 32, 'intake', halt)
        valid = lambda r, d='a' * 32, s='b' * 32, h=tool.digest(intent): tool._checkpoint_record_valid(r, d, s, h)
        self.assertTrue(valid(good))
        self.assertFalse(valid(good, d='c' * 32))
        self.assertFalse(valid(good, s='c' * 32))
        self.assertFalse(valid(good, h='0' * 64))
        for change in ({'phase': 'cycle'}, {'reasons': []}, {'reasons': ['NOPE']}, {'reasons': ['MONITORING_BLOCKED', 'HELD_PASS_NONZERO']},
                       {'entry_authorized': True}, {'no_retry': False}, {'version': 2}, {'kind': 'x'}, {'extra': 1},
                       {'held_pass': {'ran': True}}, {'held_pass': {'ran': 1, 'exit_code': 2}}, {'mode': 3}):
            self.assertFalse(valid({**good, **change}), change)

    def test_acquisition_overrun_is_cut_with_a_terminal_no_entry(self):
        self.book()
        result = self.timed_dispatch(acquisition=70)                    # default cap 30 s
        self.assertEqual((result['paper_status'], result['checkpoint_no_entry']['reasons'], result['checkpoint_no_entry']['phase']),
                         ('NO_ENTRY', ['PHASE_CAP_EXCEEDED'], 'acquisition'))
        self.assertEqual(len(cycle._state(self.ledger, self.cfg)['positions']), 1)
        self.assert_not_orphaned()

    def test_intake_overrun_is_cut_with_a_terminal_no_entry(self):
        self.book()
        result = self.timed_dispatch(intake=40)                          # default cap 25 s
        self.assertEqual((result['paper_status'], result['checkpoint_no_entry']['reasons'], result['checkpoint_no_entry']['phase']),
                         ('NO_ENTRY', ['PHASE_CAP_EXCEEDED'], 'intake'))
        self.assert_not_orphaned()

    def test_no_request_starts_once_the_acquisition_cap_has_passed(self):
        self.book()
        calls, seen = [], len(self.calls)
        real_guard = tool._held_guard

        @contextlib.contextmanager
        def slow_guard(ctx, scan=None):
            self.sim += 20                                               # every setup request takes 20 s
            calls.append(len(self.calls))
            with real_guard(ctx, scan):
                yield
        with patch.object(tool, '_held_guard', slow_guard):
            result = self.timed_dispatch(acquisition=0, intake=0, prep_cycle=0)
        self.assertEqual(result['checkpoint_no_entry']['reasons'], ['PHASE_CAP_EXCEEDED'])
        self.assertEqual(result['checkpoint_no_entry']['phase'], 'acquisition')
        provider_calls = [c for c in self.calls[seen:] if c != 'intake']
        self.assertEqual(len(calls), 2, 'the guard ran for two requests; the third started after the 30 s cap and was cut')
        self.assertEqual(len(provider_calls), 2)
        self.assert_not_orphaned()

    def test_caps_are_enforced_not_just_estimated(self):
        self.assertEqual(pc.PHASE_CAPS, {'acquisition': 30.0, 'intake': 25.0})
        self.assertGreater(pc.PHASE_CAPS['acquisition'], pc.ACQUISITION_SECONDS)
        self.assertGreater(pc.PHASE_CAPS['intake'], pc.INTAKE_SECONDS)
        # worst case before the cycle: both caps + legs for a full book must fit the held gap with the cycle phase
        self.assertLessEqual(pc.PHASE_CAPS['acquisition'] + pc.held_wall_seconds(4), pc.HELD_MAX_GAP_SECONDS)


class ThreePositionRestart(Latency):
    def fills(self):
        with sqlite3.connect(self.ledger) as c:
            return sum(1 for (p,) in c.execute('SELECT payload FROM outcomes') if json.loads(p).get('type') == 'fill')

    def three_positions(self, upto=3):
        self.first_entry()
        for n in range(2, upto + 1):
            self.append_second_candidate(seed=20 + n)
            self.tick()
            result = self.timed_dispatch(intents=n, raw=self.second_raw)
            self.assertEqual((result['status'], result['paper_status']), ('DISPATCHED', 'COMPLETE'), result)
        self.assertEqual(len(cycle._state(self.ledger, self.cfg)['positions']), upto)

    def totals(self):
        return (self.charges(), self.monitoring_rows(), self.fills(), self.count('intents'), self.count('results'))

    def test_crash_mid_leg_with_three_positions_latches_without_duplicate_charges_or_fills(self):
        self.three_positions()
        self.tick()                                                  # a healthy pass: 3 legs x 5 requests
        base = self.totals()
        self.assertEqual(base[1][0] - base[1][1], 0)

        class Crash(transport.PaperReadSources):
            def rpc(self, method, params, *, timeout_seconds):
                self.monitoring_budget.reserve_read(self.progress, self.scan_id, method, params)
                raise SystemExit('SYNTHETIC_TEST_ONLY crash boundary')
        with patch.object(transport, 'PaperReadSources', Crash), self.assertRaises(SystemExit):
            self.held_leg(pc.held_order(cycle._state(self.ledger, self.cfg)['positions'])[0])
        crashed = self.totals()
        self.assertEqual(crashed[1][0] - crashed[1][1], 1, 'one reservation without an outcome')
        self.assertEqual((crashed[0], crashed[2:]), (base[0], base[2:]))      # nothing else moved
        self.f.at += 20                                              # restart inside the 60 s abandonment window
        for mint in pc.held_order(cycle._state(self.ledger, self.cfg)['positions']):
            leg = self.held_leg(mint)
            self.assertEqual((leg['status'], leg['attempted_requests']), ('RECOVERY_REQUIRED', 0), leg)
        self.assertEqual(self.totals(), crashed, 'no duplicate charge, reservation or fill after the restart')
        self.append_second_candidate(seed=34)
        with self.assertRaisesRegex(ValueError, 'Monitoring transport unresolved|Monitoring pending or blocked|Observation recovery'):
            self.timed_dispatch(intents=4, raw=self.second_raw)
        self.assertEqual(self.totals(), crashed, 'a pending monitoring outcome also refuses every new entry before any charge')
        self.assertEqual(len(cycle._state(self.ledger, self.cfg)['positions']), 3)
        # After ABANDON_AFTER_SECONDS (T22G's reviewed rule, merged in T16I) the orphan is closed as ABANDONED_CHARGED: it
        # stays charged (never refunded, never repeated), and the held legs resume with their own five requests each.
        self.f.at += 60
        first = pc.held_order(cycle._state(self.ledger, self.cfg)['positions'])[0]
        leg = self.held_leg(first)
        self.assertEqual((leg['status'], leg['monitoring_attempted_requests']), ('COMPLETE', 5), leg)
        after = self.totals()
        self.assertEqual(after[1][0], crashed[1][0] + 5)                     # the orphan's reservation is still counted
        self.assertEqual(after[1][1], crashed[1][1] + 6)                     # its ABANDONED_CHARGED outcome + the leg's five
        self.assertEqual((after[0], after[2]), (crashed[0], crashed[2]))      # no investigation charge, no fill

    def test_interrupted_entry_restart_never_repeats_the_intent_or_its_charges(self):
        self.three_positions(upto=2)
        self.append_second_candidate(seed=33)
        self.tick()
        before = self.totals()

        def die(*a, **k):
            raise SystemExit('SYNTHETIC_TEST_ONLY crash after intent and acquisition')
        with patch.object(tool.migration, 'intake', die), self.assertRaises(SystemExit):
            self.timed_dispatch(intents=3, raw=self.second_raw)
        crashed = self.totals()
        self.assertEqual(crashed[3], before[3] + 1, 'the durable intent was written before any I/O')
        self.assertEqual(crashed[2], before[2])
        self.f.at += 70
        with self.assertRaises(ValueError):
            self.timed_dispatch(intents=3, raw=self.second_raw)      # restart: the unresolved intent refuses a retry
        again = self.totals()
        self.assertEqual((again[0], again[2], again[3]), (crashed[0], crashed[2], crashed[3]))
        self.assertEqual(len(cycle._state(self.ledger, self.cfg)['positions']), 2)


for _cls in (RealisticPhases, ThreePositionRestart, CheckpointTerminalResult):
    for _name in dir(pipe.ConcurrentPipeline):
        if _name.startswith('test_') and _name not in vars(_cls):
            setattr(_cls, _name, None)

if __name__ == '__main__':
    unittest.main()
