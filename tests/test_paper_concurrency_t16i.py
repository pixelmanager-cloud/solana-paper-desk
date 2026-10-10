"""SYNTHETIC_TEST_ONLY: T16I. After the merge with the T22 line: a phase cut stays a terminal no-entry, every remaining
post-intent raise ends as a typed terminal result, the classifiers know the new kind, and the caps are enforced."""
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

from desk import paper_concurrency as pc, paper_cycle as cycle, watchlist
from tests import test_paper_concurrency_held_latency as held
from tools import paper_entry_dispatcher as tool
from tools.research import counterfactual, funnel_report


class PostIntentTerminal(held.CheckpointTerminalResult):
    def test_a_phase_cut_after_the_merge_is_a_terminal_no_entry_never_a_hold_or_failed_charged(self):
        """T16H's `phase.finish()` runs AHEAD of T22G's post-acquire raise and `phase.check()` sits OUTSIDE the rpc_state
        capture; otherwise the cut would be classified non-transient and become an INTEGRITY_HOLD / unresolved intent."""
        self.book()
        spies = {name: patch.object(tool, name, side_effect=AssertionError(name)) for name in ('_hold_dispatch', '_close_dispatch')}
        for spy in spies.values():
            spy.start()
            self.addCleanup(spy.stop)
        calls, real_guard = [], tool._held_guard

        @held.contextlib.contextmanager
        def slow_guard(ctx, scan=None):
            self.sim += 20
            calls.append(1)
            with real_guard(ctx, scan):
                yield
        with patch.object(tool, '_held_guard', slow_guard):
            result = self.timed_dispatch(acquisition=0, intake=0, prep_cycle=0)
        self.assertEqual((result['paper_status'], result['checkpoint_no_entry']['reasons']), ('NO_ENTRY', ['PHASE_CAP_EXCEEDED']))
        self.assertEqual(self.journal_results()[-1]['result']['kind'], tool.CHECKPOINT_KIND)
        self.assert_not_orphaned()

    # -- item 2: every remaining post-intent raise ends typed ---------------------------------------------------------
    def test_a_busy_lock_in_the_checkpoint_gate_ends_typed(self):
        self.book()
        real = tool.monitor._context
        import inspect

        def context(*args, **kwargs):
            if inspect.stack()[1].function == 'gate':
                raise ValueError('Cycle ledger busy')
            return real(*args, **kwargs)
        with self.real_pass_with(code=0), patch.object(tool.monitor, '_context', context):
            result = self.timed_dispatch(acquisition=70, intake=40, caps=held.SLOW_CAPS, real_held=True)
        self.assertEqual((result['paper_status'], result['checkpoint_no_entry']['reasons']), ('NO_ENTRY', ['CHECKPOINT_STATE_UNAVAILABLE']))
        self.assertEqual(self.journal_results()[-1]['result']['mode'], 'UNKNOWN')
        self.assert_not_orphaned()

    def test_gate_busy_messages_are_the_three_non_blocking_locks(self):
        self.assertEqual(tool.BUSY_MESSAGES, ('Research worker busy', 'Evidence invocation busy', 'Cycle ledger busy'))
        for message in tool.BUSY_MESSAGES:
            cadence = tool._HeldCadence(tool.plan(**self.args))
            with patch.object(tool.monitor, '_context', side_effect=ValueError(message)):
                halt = cadence.gate(self.cfg, 0)
            self.assertEqual(halt['reasons'], ['CHECKPOINT_STATE_UNAVAILABLE'], message)
        with patch.object(tool.monitor, '_context', side_effect=ValueError('Reviewed context changed')), self.assertRaises(ValueError):
            tool._HeldCadence(tool.plan(**self.args)).gate(self.cfg, 0)         # an integrity message is never swallowed

    def refusing_preflight(self, message):
        real = tool._preflight

        def preflight(ctx, scan=None):
            if scan is not None:
                raise ValueError(message)
            return real(ctx, scan)
        return patch.object(tool, '_preflight', preflight)

    def test_a_preflight_refusal_after_the_intent_ends_typed_at_intake_and_preparation(self):
        cases = {'Held-position priority or paused ledger': ['LEDGER_MODE_NOT_RUNNING'],
                 'Monitoring pending or blocked': ['MONITORING_BLOCKED'],
                 'Cycle ledger busy': ['CHECKPOINT_STATE_UNAVAILABLE'],
                 'Concurrent entry refused: MAX_POSITIONS_REACHED,MONITORING_RESERVE_INSUFFICIENT':
                     ['MAX_POSITIONS_REACHED', 'MONITORING_RESERVE_INSUFFICIENT']}
        self.book()
        for number, (message, reasons) in enumerate(cases.items()):
            with self.subTest(message=message), self.refusing_preflight(message):
                if number:
                    self.append_second_candidate(seed=40 + number)
                    self.tick()
                result = self.timed_dispatch(intents=2 + number, raw=self.second_raw)
                self.assertEqual((result['paper_status'], result['checkpoint_no_entry']['reasons']), ('NO_ENTRY', reasons))
                self.assertEqual(result['checkpoint_no_entry']['phase'], 'intake')
                self.assertEqual(self.journal_results()[-1]['result']['kind'], tool.CHECKPOINT_KIND)

    def test_an_integrity_refusal_after_the_intent_still_raises_and_holds(self):
        """Only book-state refusals are terminal no-entries: context/identity/source changes keep T22H's durable hold."""
        self.book()
        held_calls = []
        with self.refusing_preflight('Context file identity changed'), \
                patch.object(tool, '_hold_dispatch', side_effect=lambda *a, **k: held_calls.append(a)), \
                self.assertRaisesRegex(ValueError, 'Context file identity changed'):
            self.timed_dispatch()
        self.assertEqual(len(held_calls), 1)

    def test_an_unreadable_ledger_while_building_the_overrun_halt_still_ends_typed(self):
        import inspect
        self.book()
        real = cycle._state

        def state(path, cfg):
            if inspect.stack()[1].function == 'current_mode':        # only the halt's own mode lookup fails
                raise sqlite3.OperationalError('database is locked')
            return real(path, cfg)
        with patch.object(cycle, '_state', state):
            result = self.timed_dispatch(acquisition=70)
        self.assertEqual((result['paper_status'], result['checkpoint_no_entry']['reasons']), ('NO_ENTRY', ['PHASE_CAP_EXCEEDED']))
        self.assertEqual(self.journal_results()[-1]['result']['mode'], 'UNKNOWN')
        self.assert_not_orphaned()

    def test_the_size_bound_shrinks_the_record_instead_of_raising(self):
        halt = {'reasons': sorted(tool.CHECKPOINT_REASONS), 'ran': True, 'exit_code': 2, 'mode': 'RUNNING'}
        intent = {'version': 1, 'at': 1.0}
        full = tool.checkpoint_record('a' * 32, intent, 'b' * 32, 'intake', halt)
        outcome = lambda r: {'version': 1, 'intent_hash': tool.digest(intent), 'at': 0.0, 'scan_id': 'b' * 32, 'result': r}
        size = len(tool.canonical(outcome(full)).encode())
        small = tool.bounded_checkpoint_record('a' * 32, intent, 'b' * 32, 'intake', halt, size - 1)
        self.assertEqual(len(small['reasons']), 1)
        self.assertLess(len(tool.canonical(outcome(small)).encode()), size)
        self.assertTrue(tool._checkpoint_record_valid(small, 'a' * 32, 'b' * 32, tool.digest(intent)))
        self.assertEqual(tool.bounded_checkpoint_record('a' * 32, intent, 'b' * 32, 'intake', halt, size), full)

    def test_the_size_bound_in_the_dispatcher_still_publishes_a_terminal_result(self):
        self.book()
        with self.real_pass_with(code=2), patch.object(tool, 'MAX_PAYLOAD', 480):
            result = self.timed_dispatch(acquisition=70, intake=40, caps=held.SLOW_CAPS, real_held=True)
        self.assertEqual(result['paper_status'], 'NO_ENTRY')
        self.assertEqual(len(self.journal_results()[-1]['result']['reasons']), 1)

    def test_the_dispatch_rate_bound_that_the_reserve_assumes_is_enforced_before_the_intent(self):
        self.book()
        before = (self.charges(), self.count('intents'), self.count('results'))
        with patch.object(pc, 'MAX_ENTRY_DISPATCHES_PER_HOUR', 1), \
                self.assertRaisesRegex(ValueError, 'Entry dispatch rate bound reached: 1 per hour'):
            self.timed_dispatch()
        self.assertEqual((self.charges(), self.count('intents'), self.count('results')), before)   # zero cost, zero intent
        self.assertEqual(pc.MAX_ENTRY_DISPATCHES_PER_HOUR * pc.CHECKPOINTS_PER_DISPATCH, pc.CHECKPOINT_PASSES_PER_HOUR)

    # -- item 4: caps are enforced ----------------------------------------------------------------------------------
    def test_the_alarm_interrupts_a_request_in_flight_at_the_phase_deadline(self):
        phase = tool._Phase('acquisition', 0.15, time.monotonic)
        started = time.monotonic()
        with self.assertRaises(tool._PhaseCut), tool._Deadline(phase):
            time.sleep(5)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(phase.cut)
        import signal
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0.0)           # disarmed afterwards
        with tool._Deadline(tool._Phase('x', 5, time.monotonic)):                  # a request inside the budget is untouched
            pass

    def test_each_request_is_armed_with_at_most_the_remaining_phase_budget(self):
        self.book()
        import signal
        armed = []
        real = signal.setitimer
        with patch('signal.setitimer', side_effect=lambda which, seconds, *a: armed.append(seconds) or real(which, seconds, *a)):
            self.timed_dispatch()
        positive = [x for x in armed if x > 0]
        self.assertTrue(positive)
        self.assertTrue(all(x <= max(pc.PHASE_CAPS.values()) for x in positive), positive)

    def test_a_pacing_backoff_longer_than_the_remaining_budget_is_a_cut_before_waiting(self):
        self.book()
        with sqlite3.connect(self.pacer) as c:
            c.execute("UPDATE state SET blocked_until=? WHERE provider='helius'", (time.time() + 1000,))
        before = len(self.calls)
        result = self.timed_dispatch()
        self.assertEqual(result['checkpoint_no_entry']['reasons'], ['PACING_BACKOFF_EXCEEDS_PHASE_BUDGET'])
        self.assertEqual(result['checkpoint_no_entry']['phase'], 'acquisition')
        self.assertEqual([c for c in self.calls[before:] if c != 'intake'], [], 'no provider request was started')

    def test_pacing_wait_reads_the_shared_store_read_only(self):
        with sqlite3.connect(self.pacer) as c:
            c.execute("UPDATE state SET blocked_until=?, next_at=? WHERE provider='helius'", (500.0, 100.0))
        self.assertEqual(tool._pacing_wait(self.pacer, 'helius', 400.0), 100.0)
        self.assertEqual(tool._pacing_wait(self.pacer, 'helius', 600.0), 0.0)
        self.assertEqual(tool._pacing_wait(self.pacer, 'nobody', 0.0), 0.0)
        self.assertEqual(tool._pacing_wait('/nonexistent/pacing.sqlite', 'helius', 0.0), 0.0)


class ClassifierTests(unittest.TestCase):
    RESULT = {'kind': 'dispatcher_checkpoint_no_entry_v1', 'reasons': ['HELD_PASS_NONZERO', 'MONITORING_BLOCKED']}

    def test_watchlist_treats_a_held_side_cut_as_not_yet(self):
        codes, accepted = watchlist.reason_codes(self.RESULT)
        self.assertEqual((codes, accepted), (['HELD_PASS_NONZERO', 'MONITORING_BLOCKED'], False))
        self.assertEqual(watchlist.classify(codes), 'NOT_YET')
        for reason in sorted(tool.CHECKPOINT_REASONS):
            self.assertEqual(watchlist.classify(watchlist.reason_codes({**self.RESULT, 'reasons': [reason]})[0]), 'NOT_YET', reason)
        self.assertEqual(watchlist.classify(watchlist.reason_codes({**self.RESULT, 'reasons': ['HELD_PASS_NONZERO', 'TOKEN_X']})[0]), 'PERMANENT')
        self.assertEqual(watchlist.reason_codes({'kind': 'dispatcher_checkpoint_no_entry_v1', 'reasons': 'x'}), ([], False))

    def test_counterfactual_classifies_it_as_rejected_checkpoint(self):
        self.assertEqual(counterfactual.classify_result(self.RESULT),
                         {'class': 'REJECTED', 'stage': 'CHECKPOINT', 'codes': ['HELD_PASS_NONZERO', 'MONITORING_BLOCKED'], 'detail': None})
        self.assertEqual(counterfactual.classify_result({'kind': 'dispatcher_checkpoint_no_entry_v1'})['codes'], ['NO_ENTRY'])

    def test_funnel_gives_it_its_own_stage_not_unresolved(self):
        furthest, reason, extra, _, _ = funnel_report.classify_result(self.RESULT)
        self.assertEqual((furthest, reason, extra), ('admitted', 'CHECKPOINT:HELD_PASS_NONZERO', ['CHECKPOINT:MONITORING_BLOCKED']))
        self.assertFalse(reason.startswith('UNRESOLVED'))


for _name in dir(held.CheckpointTerminalResult):
    if _name.startswith('test_') and _name not in vars(PostIntentTerminal):
        setattr(PostIntentTerminal, _name, None)

if __name__ == '__main__':
    unittest.main()
