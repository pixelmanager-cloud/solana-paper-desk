"""SYNTHETIC_TEST_ONLY: T22I - pre-transport transient history failures, dispatcher hold gaps, closure consistency.

Fixtures only (synthetic bytes); no network or credentials.
"""
import os
import sqlite3
import unittest
from unittest.mock import patch

from desk import paper_pass_closure as closure
from desk.paper_read_sources import PaperReadError
from tests import test_t22h_holds as holds
from tests import test_pass_closure as pass_closure_tests
from tools import paper_entry_dispatcher as tool


class Deadline(PaperReadError):
    pass


class PreTransportTransient(unittest.TestCase):
    def setUp(self):
        from tests.test_history_progress import HistoryProgressTests
        self.t = HistoryProgressTests('test_failure_consumes_budget_and_keeps_checkpoint')
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def advance(self, error):
        key = self.t.seed()

        def fail(*args):
            raise error
        return key, self.t.progress.advance(key, fail)

    def test_the_sources_own_deadline_before_any_transport_is_the_retryable_state(self):
        before = self.t.progress.snapshot(self.t.seed())['requests_used']
        self.setUp()
        key, result = self.advance(PaperReadError('DEADLINE_EXCEEDED'))
        self.assertEqual(result['status'], 'RETRYABLE_ERROR')
        self.assertIsNone(result.get('failure_evidence'))
        self.assertEqual(self.t.progress.snapshot(key)['requests_used'], before + 1)      # the charge is kept

    def test_every_other_evidence_less_failure_still_propagates(self):
        for name, error in {
                'TLS code without original': PaperReadError('TLS_ERROR'),
                'unclassified code': PaperReadError('UNCLASSIFIED_ERROR'),
                'binding': PaperReadError('HISTORY_BINDING_INVALID'),
                'reservation': PaperReadError('HISTORY_RESERVATION_REQUIRED'),
                'request invalid': PaperReadError('REQUEST_INVALID'),
                'abandoned lease code': PaperReadError('ABANDONED_CHARGED'),
                'deadline subclass': Deadline('DEADLINE_EXCEEDED'),
                'free-text ValueError naming the code': ValueError('DEADLINE_EXCEEDED'),
                'digest conflict': ValueError('History page digest conflict'),
                'cached request mismatch': ValueError('Cached request mismatch'),
                'bare OSError': OSError('SYNTHETIC_TEST_ONLY'),
                'deadline with a missing original': PaperReadError('DEADLINE_EXCEEDED', 'f' * 64)}.items():
            with self.subTest(name):
                self.setUp()
                key = self.t.seed()
                before = self.t.progress.snapshot(key)

                def fail(*args, error=error):
                    raise error
                with self.assertRaises(type(error)):
                    self.t.progress.advance(key, fail)
                after = self.t.progress.snapshot(key)
                self.assertNotEqual(after['status'], 'RETRYABLE_ERROR')
                self.assertEqual((after['coverage'], after['requests_used']), (before['coverage'], before['requests_used'] + 1))

    def test_the_transient_vocabulary_is_the_monitoring_one(self):
        for code in ('TRANSPORT_ERROR', 'RPC_ERROR', 'PACING_DEADLINE_EXCEEDED', 'PACING_QUEUE_FULL'):
            with self.subTest(code):
                self.setUp()
                self.assertEqual(self.advance(PaperReadError(code))[1]['status'], 'RETRYABLE_ERROR')


class HoldWriteFallbacks(holds.DispatcherBase):
    def refuse_blocked(self):
        self.refuse(lambda *a, **k: {'status': 'ACQUISITION_RETRY_OR_EVIDENCE_BLOCKED', 'scan_id': None})

    def tearDown(self):
        del tool._HOLD_FAILED[:]

    def setUp(self):
        super().setUp()
        del tool._HOLD_FAILED[:]

    def failing_first(self):
        real = tool._write_hold
        calls = []

        def write(path, identity, cause):
            calls.append(path)
            return False if len(calls) == 1 else real(path, identity, cause)
        return patch.object(tool, '_write_hold', side_effect=write), calls

    def test_a_failed_first_location_falls_back_to_a_second_durable_hold(self):
        patcher, calls = self.failing_first()
        with patcher:
            self.refuse_blocked()
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])
        self.assertFalse(os.path.lexists(calls[0]))                 # not beside the journal ...
        self.assertTrue(os.path.lexists(calls[1]))                  # ... but beside the evidence database
        self.assertEqual(tool._HOLD_FAILED, [])
        self.assertStillUnresolved()                                # and it is never abandoned

    def test_when_both_locations_fail_the_hold_is_visibly_not_durable_and_dispatch_is_refused(self):
        with patch.object(tool, '_write_hold', return_value=False):
            self.refuse_blocked()
        self.assertEqual(len(tool._HOLD_FAILED), 1)
        self.assertIn('DISPATCH_HOLD_NOT_DURABLE', tool._HOLD_FAILED[0])
        with self.assertRaises(tool.DispatchHoldFailed):
            self.h.invoke(execute=True, systemd_credentials=True)

    def test_a_dangling_symlink_hold_still_holds(self):
        self.refuse_blocked()
        directory = os.path.dirname(str(self.h.journal))
        (name,) = self.holds()
        os.remove(os.path.join(directory, name))
        os.symlink(os.path.join(directory, 'nowhere'), os.path.join(directory, name))
        self.assertStillUnresolved()


class HandlerFailuresAreHolds(holds.DispatcherBase):
    def setUp(self):
        super().setUp()
        del tool._HOLD_FAILED[:]

    def timeout_acquire(self):
        def acquire(research, evidence, rpc, **kwargs):
            try:
                rpc('getSlot', [])
            except TimeoutError:
                pass
            return {'status': 'ACQUISITION_RETRY_OR_EVIDENCE_BLOCKED', 'scan_id': None}
        return acquire

    def test_an_exception_inside_classify_writes_a_hold(self):
        with patch('desk.paper_pass_closure.classify', side_effect=RuntimeError('SYNTHETIC_TEST_ONLY')):
            self.refuse(lambda *a, **k: {'status': 'ACQUISITION_RETRY_OR_EVIDENCE_BLOCKED', 'scan_id': None})
        (name,) = self.holds()
        self.assertIn('DISPATCH_CLASSIFY_FAILED:RuntimeError',
                      open(os.path.join(os.path.dirname(str(self.h.journal)), name)).read())
        self.assertStillUnresolved()

    def test_an_exception_inside_close_dispatch_writes_a_hold(self):
        with patch('desk.providers.helius_rpc', side_effect=TimeoutError('x')), \
                patch.object(tool, '_close_dispatch', side_effect=sqlite3.OperationalError('SYNTHETIC_TEST_ONLY')):
            self.refuse(self.timeout_acquire())
        self.assertEqual(len(self.holds()), 1)
        self.assertEqual(self.h.count('results'), 0)
        self.assertStillUnresolved()

    def test_a_clean_transient_close_still_writes_no_hold(self):
        with patch('desk.providers.helius_rpc', side_effect=TimeoutError('x')):
            self.refuse(self.timeout_acquire())
        self.assertEqual((self.holds(), self.h.count('results')), ([], 1))


class SentinelLexists(pass_closure_tests.Base):
    def test_a_dangling_symlink_sentinel_keeps_the_pass_from_recovery(self):
        from contextlib import closing
        eligible = self.store.save({'kind': 'paper_cycle_intent_v1', 'closure_v1': True, 'n': 1})
        with closing(self.store.connect()) as c:
            c.execute('CREATE TABLE IF NOT EXISTS paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)', ('%032x' % 1, eligible))
        os.symlink('/nonexistent/synthetic', closure.sentinel_path(self.store, '%032x' % 1))
        for lock in (str(self.h.f.jobs.path) + '.jobs-worker.lock', str(self.store.path) + '.ownership-invocation.lock',
                     str(self.h.path) + '.paper-cycle.lock'):
            open(lock, 'a').close()
        attempted = []
        with patch.object(closure, 'close', side_effect=lambda store, progress, **kw: attempted.append(kw['pass_id'])):
            closure.recover_abandoned(self.store, self.progress, ledger_db=str(self.h.path), cfg=self.h.cfg,
                                      clock=lambda: 0, research_db=str(self.h.f.jobs.path))
        self.assertEqual(attempted, [])


class ClosureConsistency(pass_closure_tests.Base):
    def attempts(self, n, method='getTransactionsForAddress'):
        return [self.store.save({'kind': 'paper_read_attempt_v1', 'scan_id': 's', 'method': method, 'requests_used': i + 1})
                for i in range(n)]

    def test_investigation_needs_exactly_the_ceiling(self):
        for after, ok in ((17, False), (18, True), (19, False)):
            with self.subTest(after=after):
                scans = {'s': {'before': 0, 'after': after}}
                if ok:
                    closure._consistent('INVESTIGATION_REQUEST_BUDGET_EXHAUSTED', scans)
                else:
                    with self.assertRaises(closure.HoldRequired):
                        closure._consistent('INVESTIGATION_REQUEST_BUDGET_EXHAUSTED', scans)

    def test_cycle_needs_exactly_eighteen_charged_in_one_scan(self):
        closure._consistent('CYCLE_REQUEST_BUDGET_EXHAUSTED', {'s': {'before': 2, 'after': 20}})
        for scans in ({'s': {'before': 2, 'after': 19}}, {'s': {'before': 2, 'after': 21}},
                      {'a': {'before': 0, 'after': 9}, 'b': {'before': 0, 'after': 9}}):
            with self.subTest(scans=scans), self.assertRaises(closure.HoldRequired):
                closure._consistent('CYCLE_REQUEST_BUDGET_EXHAUSTED', scans)

    def test_history_page_limit_counts_retained_history_originals_not_charges(self):
        scans = {'s': {'before': 0, 'after': 18}}
        closure._consistent('FEATURE_HISTORY_PAGE_LIMIT', scans, self.store, self.attempts(8))
        for name, refs in {'seven pages': self.attempts(7),
                           'eighteen charges of another method': self.attempts(18, 'getSlot'),
                           'no references': [],
                           'seven pages and a missing original': self.attempts(7) + ['f' * 64]}.items():
            with self.subTest(name), self.assertRaises(closure.HoldRequired):
                closure._consistent('FEATURE_HISTORY_PAGE_LIMIT', scans, self.store, refs)

    def test_a_duplicated_reference_counts_once(self):
        scans = {'s': {'before': 0, 'after': 18}}
        refs = self.attempts(7)
        with self.assertRaises(closure.HoldRequired):
            closure._consistent('FEATURE_HISTORY_PAGE_LIMIT', scans, self.store, refs + [refs[0]])


if __name__ == '__main__':
    unittest.main()
