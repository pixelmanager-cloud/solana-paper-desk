"""SYNTHETIC_TEST_ONLY: T16G. Held-leg latency while a REAL dispatcher entry runs, with the phase durations the code
budgets (acquisition 12 s, intake 10 s, preparation 18 s, cycle 12 s), plus 3-position restart behaviour.

The simulated clock advances only by those budgets (the wrappers below add each phase's duration after the real
phase function ran); `time.time` is never frozen for a phase. No network, credentials or provider.
"""
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

    def timed_dispatch(self, *, intents=2, raw=None, acquisition=ACQ, intake=INTAKE, prep_cycle=PREP_CYCLE, checkpoints=True):
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
        held = patch.object(tool, '_held_pass', side_effect=self.checkpoint_pass if checkpoints else (lambda ctx: 0))
        with (held, patch.object(tool.acquisition, 'acquire', slow(acquire, acquisition)),
              patch.object(tool.migration, 'intake', slow(intake_fn, intake)),
              patch.object(tool.entry, 'execute', slow_execute),
              patch.object(tool, '_HeldCadence', side_effect=lambda ctx: cadence(ctx, clock=lambda: outer.sim)),
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
        second = self.timed_dispatch(acquisition=70, intake=40)
        self.assertEqual((second['status'], second['paper_status']), ('DISPATCHED', 'COMPLETE'), second)
        self.assertEqual(len(cycle._state(self.ledger, self.cfg)['positions']), 2)
        self.assertEqual(len(self.held_log), legs_before + 1, 'exactly one checkpoint leg, before the cycle')
        self.assertLessEqual(self.max_gap(), pc.HELD_MAX_GAP_SECONDS)
        self.assertEqual(self.monitoring_rows(), (10, 10), 'the checkpoint leg is charged to monitoring, once')

    def test_without_the_checkpoint_the_same_entry_leaves_a_gap_over_the_cadence(self):
        self.start_book()
        self.timed_dispatch(acquisition=70, intake=40, checkpoints=False)
        self.assertGreater(self.max_gap(), pc.HELD_MAX_GAP_SECONDS)

    def test_pure_rule(self):
        self.assertFalse(pc.checkpoint_due(10 ** 6, 10 ** 6, {}))          # flat books never need it
        self.assertTrue(pc.checkpoint_due(100, 30, {'A': 1}))
        self.assertFalse(pc.checkpoint_due(10, 30, {'A': 1}))
        self.assertTrue(pc.checkpoint_due(80, 30, {'A': 1, 'B': 1, 'C': 1}))   # legs are part of the gap: 80+30+23.4 > 120
        self.assertFalse(pc.checkpoint_due(60, 30, {'A': 1, 'B': 1, 'C': 1}))


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
        self.f.at += 70                                              # restart: the next tick's legs
        for mint in pc.held_order(cycle._state(self.ledger, self.cfg)['positions']):
            leg = self.held_leg(mint)
            self.assertEqual((leg['status'], leg['attempted_requests']), ('RECOVERY_REQUIRED', 0), leg)
        self.assertEqual(self.totals(), crashed, 'no duplicate charge, reservation or fill after the restart')
        self.append_second_candidate(seed=34)
        with self.assertRaisesRegex(ValueError, 'Monitoring transport unresolved|Monitoring pending or blocked|Observation recovery'):
            self.timed_dispatch(intents=4, raw=self.second_raw)
        self.assertEqual(self.totals(), crashed, 'a pending monitoring outcome also refuses every new entry before any charge')
        self.assertEqual(len(cycle._state(self.ledger, self.cfg)['positions']), 3)

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


for _cls in (RealisticPhases, ThreePositionRestart):
    for _name in dir(pipe.ConcurrentPipeline):
        if _name.startswith('test_') and _name not in vars(_cls):
            setattr(_cls, _name, None)

if __name__ == '__main__':
    unittest.main()
