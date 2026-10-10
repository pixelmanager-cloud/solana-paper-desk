"""SYNTHETIC_TEST_ONLY: T22H item 1 - a history failure is closed only when the retained attempt original proves it TRANSIENT.

Every probe goes through the REAL history path: PaperHistorySource -> PaperReadSources transport (opener patched with explicit
synthetic wire bytes / errors) -> HistoryProgress.advance -> cycle -> closure. Nothing injects a result through `source_factory`.
Before T22H, `HistoryProgress.advance` turned ANY ValueError/OSError/KeyError/TypeError/IndexError into RETRYABLE_ERROR, the cycle
raised the allow-listed HISTORY_RECOVERY_REQUIRED, and the pass was closed FAILED_CHARGED with the scan retired.
"""
import ssl
import unittest
from contextlib import closing
from urllib.error import HTTPError, URLError

from desk import paper_cycle as cycle, paper_pass_closure as closure, paper_terminal_reconciliation as terminal
from tests import test_pass_closure as pass_closure_tests


def http(status):
    return HTTPError('https://rpc.invalid/', status, 'SYNTHETIC', {}, None)


class Probe(pass_closure_tests.Base):
    def history_cycle(self, failure=None, result=None):
        self.h.history_failure, self.h.history_result = failure, result
        self.h.http_calls = []
        return self.h.actual_cycle()

    def statuses(self):
        return [r[3] for r in self.rows()]

    def assertHeld(self, outcome):
        self.assertEqual(self.statuses(), ['INTEGRITY_HOLD'], outcome)
        self.assertEqual(len(self.null_passes()), 1)
        self.assertEqual(self.gate(), 'OBSERVATION_RECOVERY_REQUIRED')

    def assertClosed(self, outcome):
        self.assertEqual(self.statuses(), ['FAILED_CHARGED'], outcome)
        self.assertEqual(self.null_passes(), [])
        self.assertEqual(self.gate((self.scan,)), 'REJECTED_SCAN_RETIRED')

    def run_expecting(self, failure=None, result=None):
        try:
            return self.history_cycle(failure, result)
        except Exception as error:               # a defect-class exception propagates after the hold is written
            return error


class EveryNonTransientHistoryFailureStaysLatched(Probe):
    CASES = {
        'TLS_ERROR': ssl.SSLCertVerificationError('SYNTHETIC_TEST_ONLY'),
        'UNCLASSIFIED_ERROR (bare OSError)': OSError('SYNTHETIC_TEST_ONLY'),
        'UNCLASSIFIED_ERROR (ValueError)': ValueError('SYNTHETIC_TEST_ONLY'),
        'URLError carrying only a message': URLError('SYNTHETIC_TEST_ONLY'),
        'HTTP 401': http(401),
        'HTTP 403': http(403),
        'HTTP 400': http(400),
    }

    def test_each_failure_holds_the_pass_and_the_global_gate(self):
        for name, failure in self.CASES.items():
            with self.subTest(name):
                super().setUp()
                outcome = self.run_expecting(failure)
                self.assertHeld(outcome)

    def test_a_malformed_history_page_holds_the_pass(self):
        for name, result in {'data is not a list': {'data': 'x', 'paginationToken': None},
                             'rows are not objects': {'data': [1, 2, 3], 'paginationToken': None},
                             'no data key': {'paginationToken': None}}.items():
            with self.subTest(name):
                super().setUp()
                self.assertHeld(self.run_expecting(None, result))


class TransientHistoryFailuresStillClose(Probe):
    CASES = {'connection reset': ConnectionResetError('SYNTHETIC_TEST_ONLY'), 'timeout': TimeoutError('SYNTHETIC_TEST_ONLY'),
             'HTTP 429': http(429), 'HTTP 503': http(503), 'HTTP 408': http(408)}

    def test_each_transient_failure_closes_failed_charged_and_retires_the_scan(self):
        for name, failure in self.CASES.items():
            with self.subTest(name):
                super().setUp()
                result = self.history_cycle(failure)
                self.assertEqual(result['blockers'], ['HISTORY_RECOVERY_REQUIRED'], result)
                self.assertClosed(result)
                record = self.record()
                self.assertEqual(record['cause'], 'HISTORY_RECOVERY_REQUIRED')
                self.assertTrue(record['attempt_refs'], 'the closure binds the failed page original')
                closure._verify(self.store, self.progress, record, row=self.rows()[0])


class EvidenceRules(Probe):
    def test_the_cause_is_evidence_bearing_not_a_plain_allow_list_entry(self):
        self.assertIn('HISTORY_RECOVERY_REQUIRED', closure.EVIDENCE_CAUSES)
        self.assertFalse(closure.classify(cycle.CycleBlocked('HISTORY_RECOVERY_REQUIRED'), self.store)[1])
        self.assertFalse(closure.classify(cycle.CycleBlocked('HISTORY_RECOVERY_REQUIRED', 'f' * 64), self.store)[1])

    def test_a_stale_retryable_state_without_an_original_holds(self):
        """RETRYABLE_ERROR found at entry (a previous process died after marking it): there is nothing to prove, so it holds."""
        self.history_cycle(ConnectionResetError('SYNTHETIC_TEST_ONLY'))
        super().setUp()
        key = self.progress.create(self.scan, self.h.target.pool, self.h.f.at - 300, self.h.f.at + 1, page_size=50)
        with closing(self.store.connect()) as c:
            c.execute("UPDATE ownership_history SET status='RETRYABLE_ERROR' WHERE id=?", (key,))
        outcome = self.run_expecting()
        self.assertEqual(self.statuses()[-1:], ['INTEGRITY_HOLD'], outcome)

    def test_a_closure_naming_history_recovery_without_a_transient_original_does_not_verify(self):
        self.history_cycle(ConnectionResetError('SYNTHETIC_TEST_ONLY'))
        record = self.record()
        for name, change in {'no refs': {'attempt_refs': []}}.items():
            with self.subTest(name), self.assertRaises(ValueError):
                closure._verify(self.store, self.progress, {**record, **change})


class AdvanceItself(unittest.TestCase):
    """HistoryProgress.advance no longer swallows what it cannot prove transient."""

    def setUp(self):
        from tests.test_history_progress import HistoryProgressTests
        self.t = HistoryProgressTests('test_failure_consumes_budget_and_keeps_checkpoint')
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def test_free_text_and_bare_errors_propagate_and_keep_the_checkpoint(self):
        for error in (ValueError('History page digest conflict'), OSError('sensitive provider error'), KeyError('x'),
                      TypeError('x'), IndexError('x')):
            with self.subTest(type(error).__name__):
                self.setUp()
                key = self.t.seed()
                before = self.t.progress.snapshot(key)

                def fail(*args, error=error):
                    raise error
                with self.assertRaises(type(error)):
                    self.t.progress.advance(key, fail)
                after = self.t.progress.snapshot(key)
                self.assertEqual((after['coverage'], after['requests_used']), (before['coverage'], before['requests_used'] + 1))
                self.assertNotEqual(after['status'], 'RETRYABLE_ERROR')

    def test_an_error_proven_transient_by_its_original_is_the_retryable_state(self):
        from desk.paper_read_sources import PaperReadError
        key = self.t.seed()
        original = self.t.store.save({'kind': 'paper_read_attempt_v1', 'scan_id': 's', 'failure_code': 'TRANSPORT_ERROR',
                                      'requests_used': 1})

        def fail(*args):
            raise PaperReadError('TRANSPORT_ERROR', original)
        result = self.t.progress.advance(key, fail)
        self.assertEqual((result['status'], result['failure_evidence']), ('RETRYABLE_ERROR', original))
        self.assertNotIn('sensitive', str(result))

    def test_a_latching_original_or_a_missing_one_propagates(self):
        from desk.paper_read_sources import PaperReadError
        key = self.t.seed()
        tls = self.t.store.save({'kind': 'paper_read_attempt_v1', 'scan_id': 's', 'failure_code': 'TLS_ERROR', 'requests_used': 1})
        for evidence in (tls, None, 'f' * 64):
            with self.subTest(evidence=evidence):
                def fail(*args, evidence=evidence):
                    raise PaperReadError('TRANSPORT_ERROR', evidence)
                with self.assertRaises(PaperReadError):
                    self.t.progress.advance(key, fail)


if __name__ == '__main__':
    unittest.main()
