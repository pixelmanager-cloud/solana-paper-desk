"""SYNTHETIC_TEST_ONLY: T16J. Post-intent refusals end as typed terminal results (pacing contention, the held guard's mid-phase
refusals), a SIGALRM phase cut after a charged intake reservation leaves nothing latched, the checkpoint codes are NOT_YET only as
codes of their own result kind, and the docs state the single flag-gated rollover rule."""
import contextlib
import json
import signal
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from desk import migration_slot_intake as migration, watchlist
from tests import test_paper_concurrency_held_latency as held
from tools import paper_entry_dispatcher as tool

ROOT = Path(__file__).resolve().parents[1]


class RefusalMapping(unittest.TestCase):
    def mode(self):
        return 'RUNNING'

    def test_every_post_intent_contention_message_maps_to_a_checkpoint_reason(self):
        expected = {'Provider pacing pending': ['PROVIDER_PACING_PENDING'],
                    'Held-position priority before I/O': ['HELD_POSITION_PRIORITY'],
                    'Held cycle or ledger busy': ['CHECKPOINT_STATE_UNAVAILABLE']}
        for message, reasons in expected.items():
            with self.subTest(message=message):
                halt = tool._refusal_halt(ValueError(message), self.mode)
                self.assertEqual((halt['reasons'], halt['ran'], halt['exit_code'], halt['mode']), (reasons, False, None, 'RUNNING'))
                self.assertLessEqual(set(halt['reasons']), tool.CHECKPOINT_REASONS)

    def test_integrity_refusals_are_not_mapped(self):
        for message in ('Reviewed context changed', 'Context file identity changed', 'Source/config context changed',
                        'Source/config changed before I/O', 'Observation recovery or retired scan', 'Pacer context mismatch',
                        'Provider pacing pending, but different', 'held cycle or ledger busy'):
            with self.subTest(message=message):
                self.assertIsNone(tool._refusal_halt(ValueError(message), self.mode))

    def test_the_new_reason_is_valid_in_a_record_and_not_yet_for_the_watchlist(self):
        intent = {'version': 1, 'at': 1.0}
        halt = {'reasons': ['PROVIDER_PACING_PENDING'], 'ran': False, 'exit_code': None, 'mode': 'RUNNING'}
        record = tool.checkpoint_record('a' * 32, intent, 'b' * 32, 'intake', halt)
        self.assertTrue(tool._checkpoint_record_valid(record, 'a' * 32, 'b' * 32, tool.digest(intent)))
        self.assertEqual(watchlist.classify(watchlist.reason_codes(record)[0]), 'NOT_YET')
        self.assertEqual(set(tool.CHECKPOINT_REASONS), set(watchlist.CHECKPOINT_NOT_YET))


class CheckpointCodeScope(unittest.TestCase):
    def test_the_codes_are_not_yet_only_as_codes_of_the_checkpoint_result_kind(self):
        for reason in watchlist.CHECKPOINT_NOT_YET:
            with self.subTest(reason=reason):
                codes, _ = watchlist.reason_codes({'kind': watchlist.CHECKPOINT_KIND, 'reasons': [reason]})
                self.assertEqual((codes, watchlist.classify(codes)), (['CHECKPOINT:' + reason], 'NOT_YET'))
                self.assertEqual(watchlist.classify([reason]), 'PERMANENT')                # bare word: not in the spec's list
                self.assertNotIn(reason, watchlist.NOT_YET)

    def test_the_global_not_yet_set_is_exactly_the_specs_ten(self):
        self.assertEqual(sorted(watchlist.NOT_YET), sorted(
            ['AGE', 'MARKET_CAP', 'LIQUIDITY', 'MOMENTUM_OR_WASH', 'MOMENTUM_OR_OBSERVED_CHURN', 'ENTRY_SCORE',
             'MARKET_PRODUCER_BLOCKED', 'HISTORY_FEATURE_EMPTY_WINDOW', 'HISTORY_REQUIRED_MEASUREMENTS_UNAVAILABLE',
             'HISTORY_FEATURE_MOMENTUM_STALE']))

    def test_an_engine_reject_or_blocker_with_a_checkpoint_word_is_permanent(self):
        engine_reject = {'kind': 'paper_cycle_v1', 'status': 'COMPLETE', 'outcomes': [
            {'type': 'reject', 'reason': 'MAX_POSITIONS_REACHED', 'reasons': ['MAX_POSITIONS_REACHED'], 'mint': 'm'}]}
        codes, _ = watchlist.reason_codes(engine_reject, mint='m')
        self.assertEqual((codes, watchlist.classify(codes)), (['MAX_POSITIONS_REACHED'], 'PERMANENT'))

    def test_the_prefix_cannot_be_forged_by_another_result_kind(self):
        for kind in ('paper_cycle_v1', 'history_preparation_no_entry_v1', 'dispatcher_migration_no_entry_v1', None):
            forged = {'kind': kind, 'blockers': ['CHECKPOINT:HELD_PASS_NONZERO'], 'reason': 'CHECKPOINT:PHASE_CAP_EXCEEDED',
                      'reasons': ['CHECKPOINT:HELD_POSITION_PRIORITY'],
                      'outcomes': [{'type': 'reject', 'reason': 'CHECKPOINT:MONITORING_BLOCKED'}]}
            with self.subTest(kind=kind):
                codes, _ = watchlist.reason_codes(forged)
                self.assertTrue(codes)
                self.assertTrue(all(c.startswith('UNTRUSTED_CHECKPOINT:') for c in codes if 'CHECKPOINT' in c), codes)
                self.assertEqual(watchlist.classify(codes), 'PERMANENT')

    def test_a_checkpoint_result_mixing_a_forged_or_unknown_reason_is_permanent(self):
        codes, _ = watchlist.reason_codes({'kind': watchlist.CHECKPOINT_KIND, 'reasons': ['HELD_PASS_NONZERO', 'NOPE'], 'blockers': ['CHECKPOINT:PHASE_CAP_EXCEEDED']})
        self.assertEqual(watchlist.classify(codes), 'PERMANENT')
        self.assertEqual(watchlist.classify(['CHECKPOINT:NOPE']), 'PERMANENT')
        self.assertEqual(watchlist.classify(['CHECKPOINT:']), 'PERMANENT')
        self.assertEqual(watchlist.classify(['checkpoint:HELD_PASS_NONZERO']), 'PERMANENT')


class DocsStateTheOneRolloverRule(unittest.TestCase):
    def test_the_activation_section_makes_the_flag_the_condition_of_every_after_mark_rollover(self):
        text = (ROOT / 'docs' / 'MULTI_POSITION.md').read_text()
        activation = text.split('## Recommended activation', 1)[1].split('## Not done', 1)[0]
        self.assertIn('"paper_rollover_after_mark_version": 1', activation)
        self.assertIn('every\n   after-mark rollover needs it', activation)
        self.assertNotIn('additionally lets a single', text)
        self.assertNotIn('which previously rolled the day unconditionally)', text.split('### The ONE rollover rule', 1)[0])

    def test_the_code_agrees_the_batched_marks_roll_only_under_the_flag(self):
        from desk import engine
        self.assertFalse(engine.rollover_after_mark({}))
        self.assertTrue(engine.rollover_after_mark({engine.ROLLOVER_KEY: 1, 'paper_concurrent_entries_version': 1}))


class PostIntentTerminals(held.CheckpointTerminalResult):
    def refusing_preflight(self, message):
        real = tool._preflight

        def preflight(ctx, scan=None):
            if scan is not None:
                raise ValueError(message)
            return real(ctx, scan)
        return patch.object(tool, '_preflight', preflight)

    def no_holds(self):
        spies = [patch.object(tool, name, side_effect=AssertionError(name)) for name in ('_hold_dispatch', '_close_dispatch')]
        for spy in spies:
            spy.start()
            self.addCleanup(spy.stop)

    def terminal(self, result, reasons, phase):
        self.assertEqual((result['paper_status'], result['checkpoint_no_entry']['reasons'], result['checkpoint_no_entry']['phase']),
                         ('NO_ENTRY', reasons, phase), result)
        self.assertEqual(self.journal_results()[-1]['result']['kind'], tool.CHECKPOINT_KIND)
        self.assert_not_orphaned()

    # -- item 3a: provider pacing pending after the intent is contention ----------------------------------------------------
    def test_provider_pacing_pending_after_the_intent_is_a_typed_terminal_no_entry(self):
        self.book()
        self.no_holds()
        with self.refusing_preflight('Provider pacing pending'):
            result = self.timed_dispatch(intents=2, raw=self.second_raw)
        self.terminal(result, ['PROVIDER_PACING_PENDING'], 'intake')

    def test_pacing_pending_before_the_intent_is_still_a_zero_cost_refusal(self):
        self.book()
        self.append_second_candidate(seed=44)
        self.tick()
        before = self.count('intents')
        with patch.object(tool, '_preflight', side_effect=ValueError('Provider pacing pending')), \
                self.assertRaisesRegex(ValueError, 'Provider pacing pending'):
            self.timed_dispatch(intents=3, raw=self.second_raw)
        self.assertEqual(self.count('intents'), before, 'no intent was written')

    # -- item 3b: the held guard's refusals in the middle of a phase ---------------------------------------------------------
    def refusing_guard(self, message):
        @contextlib.contextmanager
        def guard(ctx, scan=None):
            raise ValueError(message)
            yield                                                   # pragma: no cover
        return patch.object(tool, '_held_guard', guard)

    def test_the_held_guard_refusing_before_an_acquisition_call_ends_typed(self):
        cases = {'Held-position priority before I/O': ['HELD_POSITION_PRIORITY'],
                 'Held cycle or ledger busy': ['CHECKPOINT_STATE_UNAVAILABLE']}
        self.book()
        self.no_holds()
        for number, (message, reasons) in enumerate(cases.items()):
            with self.subTest(message=message):
                if number:
                    self.append_second_candidate(seed=50 + number)
                    self.tick()
                with self.refusing_guard(message):
                    result = self.timed_dispatch(intents=2 + number, raw=self.second_raw)
                self.terminal(result, reasons, 'acquisition')

    def test_the_held_guard_refusing_before_the_intake_io_ends_typed(self):
        cases = {'Held-position priority before I/O': ['HELD_POSITION_PRIORITY'],
                 'Held cycle or ledger busy': ['CHECKPOINT_STATE_UNAVAILABLE']}
        self.book()
        self.no_holds()
        for number, (message, reasons) in enumerate(cases.items()):
            with self.subTest(message=message):
                if number:
                    self.append_second_candidate(seed=60 + number)
                    self.tick()
                with patch.object(tool, '_intake_guard', side_effect=ValueError(message)):
                    result = self.timed_dispatch(intents=2 + number, raw=self.second_raw)
                self.terminal(result, reasons, 'intake')

    def test_other_guard_failures_still_raise_and_hold(self):
        """Only the two book-state refusals are typed; a changed source/config before I/O is an integrity failure."""
        self.book()
        held_calls = []
        with self.refusing_guard('Source/config changed before I/O'), \
                patch.object(tool, '_hold_dispatch', side_effect=lambda *a, **k: held_calls.append(a)), \
                self.assertRaises(ValueError):
            self.timed_dispatch(intents=2, raw=self.second_raw)
        self.assertEqual(len(held_calls), 1)

    # -- item 4: SIGALRM after a charged reservation inside intake ------------------------------------------------------------------
    def test_an_alarm_after_the_charged_intake_reservation_leaves_a_clean_next_dispatch(self):
        self.book()
        self.no_holds()
        fired = []
        real_call = migration._SlotSource.__call__

        def alarm_after_the_charge(source, method, params):
            fired.append(1)                      # `advance` already reserved and charged this attempt
            if signal.getsignal(signal.SIGALRM) in (signal.SIG_DFL, signal.SIG_IGN, None):
                raise AssertionError('the dispatcher armed no SIGALRM handler: sending the signal would kill the test process')
            signal.raise_signal(signal.SIGALRM)  # the dispatcher's own _Deadline handler turns it into the phase cut
            return real_call(source, method, params)
        before = sum(self.charges().values())
        with patch.object(migration._SlotSource, '__call__', alarm_after_the_charge):
            result = self.timed_dispatch(intents=2, raw=self.second_raw)
        self.assertEqual(fired, [1])
        self.terminal(result, ['PHASE_CAP_EXCEEDED'], 'intake')
        self.assertGreater(sum(self.charges().values()), before, 'the interrupted attempt stays charged')
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0.0, 'no alarm left armed')
        # The next dispatch (a different candidate) is not latched by the ambiguous reservation of the retired scan.
        self.append_second_candidate(seed=70)
        self.tick()
        again = self.timed_dispatch(intents=3, raw=self.second_raw)
        self.assertEqual((again['status'], again['paper_status']), ('DISPATCHED', 'COMPLETE'), again)
        self.assert_not_orphaned()


for _name in dir(held.pipe.ConcurrentPipeline):
    if _name.startswith('test_') and _name not in vars(PostIntentTerminals):
        setattr(PostIntentTerminals, _name, None)
for _name in dir(held.CheckpointTerminalResult):
    if _name.startswith('test_') and _name not in vars(PostIntentTerminals):
        setattr(PostIntentTerminals, _name, None)

if __name__ == '__main__':
    unittest.main()
