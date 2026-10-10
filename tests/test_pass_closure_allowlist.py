"""T22G: pass closure is an ALLOW-list; nested blockers, owner proof, hold durability, no new hard cap.

Fixtures only (synthetic bytes); no network, no credentials. The lock evidence is injected (tests.lock_sources)
so every test runs identically on Linux and macOS.
"""
import contextlib
import json
import os
import sqlite3
import time
import unittest
from contextlib import closing
from unittest.mock import patch

from desk import paper_cycle as cycle, paper_cycle_no_entry as no_entry, paper_pass_closure as closure
from desk import paper_read_sources as transport, paper_terminal_reconciliation as terminal
from desk.evidence import EvidenceStore
from desk.model import digest
from tests import legacy_null_pass, lock_sources
from tests import test_empty_history_no_entry as empty_history_tests
from tests import test_pass_closure as pass_closure_tests, test_paper_cycle as paper_cycle_tests


def fresh():
    h = paper_cycle_tests.PaperCycleTests('test_empty_cycle_and_restart_preserve_new_experiment')
    h.setUp()
    return h


class EveryExampleOfTheReviewStaysLatched(unittest.TestCase):
    """Item 2: the verified examples that the old deny-list closed (and silently un-latched)."""
    EXAMPLES = {
        'quote envelope binding': ValueError('quote envelope binding'),
        'mint binding': ValueError('mint binding'),
        'pool envelope binding': ValueError('pool envelope binding'),
        'retained evidence unavailable': ValueError('retained evidence unavailable'),
        'history page digest conflict': ValueError('History page digest conflict'),
        'captured history changed': ValueError('Captured history changed'),
        'TLS_ERROR': transport.PaperReadError('TLS_ERROR'),
        'UNCLASSIFIED_ERROR': transport.PaperReadError('UNCLASSIFIED_ERROR'),
        'COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID': cycle.CycleBlocked('COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID'),
        'bare OSError': OSError('SYNTHETIC_TEST_ONLY'),
        'RuntimeError defect': RuntimeError('SYNTHETIC_TEST_ONLY'),
    }

    def test_classification_is_allow_list_only(self):
        for name, error in self.EXAMPLES.items():
            with self.subTest(name):
                self.assertFalse(closure.classify(error)[1])

    def test_each_example_holds_the_pass_end_to_end(self):
        # A typed CycleBlocked raised before any charge ends as a zero-charge BLOCKED result (no latch, nothing to
        # hold); its charged counterpart is covered by NestedBlockersTests and SourceFailureNeedsEvidence.
        for name, error in self.EXAMPLES.items():
            if isinstance(error, cycle.CycleBlocked):
                continue
            with self.subTest(name):
                h = fresh()
                self.addCleanup(h.doCleanups)

                def raising(progress, scan, error=error):
                    raise error
                try:                       # typed CycleBlocked/MonitoringBlocked become a BLOCKED result, others propagate
                    h.run_cycle(source_factory=raising)
                except Exception as caught:
                    self.assertIsInstance(caught, type(error))
                store = h.f.progress.store
                with closing(store.connect()) as c:
                    statuses = [r[0] for r in c.execute('SELECT status FROM paper_pass_closures')]
                    null = c.execute('SELECT count(*) FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()[0]
                self.assertEqual((statuses, null), (['INTEGRITY_HOLD'], 1))
                self.assertEqual(terminal.gate(store, h.f.jobs.path, (), ledger_locked=str(h.path)), 'OBSERVATION_RECOVERY_REQUIRED')

    def test_transient_classes_still_close(self):
        transient = {'timeout': TimeoutError('x'), 'reset': ConnectionResetError('x'), 'interrupt': KeyboardInterrupt(),
                     'exit': SystemExit('SYNTHETIC crash boundary'), 'transport code': transport.PaperReadError('TRANSPORT_ERROR'),
                     'deadline': transport.PaperReadError('DEADLINE_EXCEEDED'), 'typed blocker': cycle.CycleBlocked('UNRESOLVED_QUOTE_DEMAND')}
        for name, error in transient.items():
            with self.subTest(name):
                self.assertTrue(closure.classify(error)[1])

    def test_http_rejected_closes_only_when_the_retained_attempt_says_transient(self):
        h = fresh()
        self.addCleanup(h.doCleanups)
        store = h.f.progress.store

        def attempt(status, code='HTTP_REJECTED'):
            return store.save({'kind': 'paper_read_attempt_v1', 'scan_id': 's', 'failure_code': code, 'http_status': status,
                               'requests_used': 1})
        for status, expected in ((429, True), (503, True), (408, True), (401, False), (403, False), (400, False), (None, False)):
            with self.subTest(status):
                error = transport.PaperReadError('HTTP_REJECTED', attempt(status))
                self.assertEqual(closure.classify(error, store)[1], expected)
        self.assertFalse(closure.classify(transport.PaperReadError('HTTP_REJECTED'), store)[1])       # no original
        self.assertFalse(closure.classify(transport.PaperReadError('HTTP_REJECTED', 'f' * 64), store)[1])  # unreadable


class SourceFailureNeedsEvidence(pass_closure_tests.Base):
    def test_a_failed_read_is_closed_only_with_a_transient_attempt_original(self):
        first = self.fail_entry()                                             # ConnectionResetError through the real transport
        self.assertIn('SOURCE_REQUEST_FAILED', first['blockers'])
        self.assertEqual([r[3] for r in self.rows()], ['FAILED_CHARGED'])

    def test_a_tls_failure_through_the_real_transport_holds_the_pass(self):
        import ssl
        first = self.fail_entry(ssl.SSLCertVerificationError('SYNTHETIC_TEST_ONLY'))
        self.assertIn('SOURCE_REQUEST_FAILED', first['blockers'])
        self.assertEqual([r[3] for r in self.rows()], ['INTEGRITY_HOLD'])
        self.assertEqual(self.gate(), 'OBSERVATION_RECOVERY_REQUIRED')

    def test_an_unclassified_failure_through_the_real_transport_holds_the_pass(self):
        first = self.fail_entry(OSError('SYNTHETIC_TEST_ONLY'))
        self.assertIn('SOURCE_REQUEST_FAILED', first['blockers'])
        self.assertEqual([r[3] for r in self.rows()], ['INTEGRITY_HOLD'])

    def test_source_request_failed_without_any_attempt_original_holds(self):
        self.h.f.fail = 'getAccountInfo'                                      # fixture failure: no attempt original retained
        first = self.h.run_cycle()
        self.assertIn('SOURCE_REQUEST_FAILED', first['blockers'])
        self.assertEqual([r[3] for r in self.rows()], ['INTEGRITY_HOLD'])

    def test_a_forged_closure_naming_an_unproven_failure_does_not_verify(self):
        self.fail_entry()
        rec, row = self.record(), self.rows()[0]
        self.assertEqual(rec['attempt_refs'] != [], True)
        for name, change in {'no refs': {'attempt_refs': []}, 'cause off the allow-list': {'cause': 'MINT_BINDING'},
                             'cause of an integrity class': {'cause': 'TLS_ERROR'}}.items():
            with self.subTest(name), self.assertRaises(ValueError):
                closure._verify(self.store, self.progress, {**rec, **change})


class NestedBlockersTests(unittest.TestCase):
    """Item 3: a refused R1 publication must not be undone by the generic closure."""

    def build(self, code, refuse_publish=False):
        real = cycle.build_market_event

        def forged(*args, **kwargs):
            diagnostic = real(*args, **kwargs)
            if code is not None and diagnostic['event'] is None:
                diagnostic = {**diagnostic, 'blockers': [code]}
            return diagnostic
        patches = [patch.object(cycle, 'build_market_event', forged)]
        if refuse_publish:
            def refusing(*a, **k):
                raise ValueError('forced refusal SYNTHETIC_TEST_ONLY')
            patches.append(patch.object(no_entry, 'publish', refusing))
        # The shared fixture installs tests.legacy_null_pass (closure machinery OFF) to certify pre-T22 states; these
        # tests are about the closure machinery itself, so install() is skipped for this fixture.
        patches.append(patch.object(legacy_null_pass, 'install', lambda case: None))
        for p in patches:
            p.start()
        try:
            t = empty_history_tests.RetainedEmptyTests('test_missing_capture_stays_pending')
            t.legacy_null_pass = contextlib.nullcontext
            t.setUp()
        finally:
            for p in patches:
                p.stop()
        self.addCleanup(t.doCleanups)
        return t

    def statuses(self, t):
        with closing(t.store.connect()) as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='paper_pass_closures'").fetchone()
            return [r[0] for r in c.execute('SELECT status FROM paper_pass_closures')] if exists else []

    def test_integrity_codes_nested_in_a_producer_blocker_hold_the_pass(self):
        for code in ('COORDINATOR_TARGET_BINDING_MISMATCH', 'COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID',
                     'SOL_USD_TRUSTED_INPUT_INVALID', 'COLLECTOR_OBSERVATION_REJECTED', 'EXPERIMENTAL_EVENT_CONTRACT_INVALID'):
            with self.subTest(code):
                t = self.build(code)
                self.assertEqual(t.result['blockers'], ['MARKET_PRODUCER_BLOCKED'])
                self.assertEqual(self.statuses(t), ['INTEGRITY_HOLD'])
                self.assertEqual(terminal.gate(t.store, t.ctx['research_db'], ()), 'OBSERVATION_RECOVERY_REQUIRED')

    def test_ordinary_window_blockers_with_a_refused_publication_close_as_failed_charged(self):
        t = self.build('MISSING_WINDOW_MEASUREMENT:net_buy_ratio', refuse_publish=True)
        self.assertEqual(self.statuses(t), ['FAILED_CHARGED'])
        self.assertIsNone(terminal.gate(t.store, t.ctx['research_db'], ()))

    def test_integrity_nested_blockers_with_a_refused_publication_still_hold(self):
        t = self.build('COORDINATOR_TARGET_BINDING_MISMATCH', refuse_publish=True)
        self.assertEqual(self.statuses(t), ['INTEGRITY_HOLD'])

    def test_nested_check_unit(self):
        clear = closure.nested_blockers_clear
        base = {'blockers': ['MARKET_PRODUCER_BLOCKED'], 'diagnostics': [{'scan_id': 's', 'blockers': ['MISSING_WINDOW_MEASUREMENT:net_buy_ratio']}]}
        self.assertTrue(clear(base))
        self.assertFalse(clear({**base, 'diagnostics': [{'scan_id': 's', 'blockers': ['COLLECTOR_ANYTHING']}]}))
        self.assertFalse(clear({**base, 'blockers': ['MARKET_PRODUCER_BLOCKED', 'MONITORING_CHARGE_OR_ADMISSION_MISMATCH']}))
        self.assertFalse(clear({**base, 'diagnostics': [{'blockers': 'MISSING_WINDOW_MEASUREMENT:net_buy_ratio'}]}))
        self.assertFalse(clear({**base, 'diagnostics': 'x'}))
        self.assertFalse(clear(None))
        self.assertTrue(clear({**base, 'diagnostics': [{'blockers': ['FREEZE']}]}, hazards=('FREEZE',)))
        self.assertFalse(clear({**base, 'diagnostics': [{'blockers': ['FREEZE']}]}))
        witness = {'blockers': ['RETAINED_MIGRATION_WITNESS_REQUIRED'],
                   'diagnostics': [{'scan_id': 's', 'graduation': {'status': 'NOT_OBSERVED', 'blockers': ['MIGRATION_WITNESS_ABSENT']}}]}
        self.assertTrue(clear(witness))
        witness['diagnostics'][0]['graduation']['blockers'] = ['CONFLICTING_MIGRATION_TIMESTAMPS']
        self.assertFalse(clear(witness))


class OwnerProofTests(pass_closure_tests.Base):
    def dead_pass(self):
        self.h.run_cycle(candidates=())                                       # creates the three lock files
        intent = self.store.save({'kind': 'paper_cycle_intent_v1', 'closure_v1': True, 'config_hash': digest(self.h.cfg),
                                  'ledger': str(self.h.path), 'targets': [],
                                  'admissions': {self.scan: self.progress.admission(self.scan)}})
        with closing(self.store.connect()) as c:
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)', ('d1' * 16, intent))
        return 'd1' * 16

    def lock_paths(self):
        return {'research': str(self.h.f.jobs.path) + '.jobs-worker.lock',
                'evidence': str(self.store.path) + '.ownership-invocation.lock',
                'ledger': str(self.h.path) + '.paper-cycle.lock'}

    def recover(self):
        return closure.recover_abandoned(self.store, self.progress, ledger_db=self.h.path, cfg=self.h.cfg,
                                         clock=lambda: self.h.f.at, research_db=self.h.f.jobs.path)

    def test_nothing_holds_the_locks_so_the_dead_pass_is_closed(self):
        pass_id = self.dead_pass()
        lock_sources.free(self)
        result = self.recover()
        self.assertEqual((result['closed'], result['refused']), ([pass_id], []))

    def test_another_process_holding_any_lock_closes_nothing(self):
        pass_id = self.dead_pass()
        for name, path in self.lock_paths().items():
            with self.subTest(name):
                lock_sources.held(self, path)
                result = self.recover()
                self.assertEqual(result['closed'], [])
                self.assertEqual(result['refused'], [(None, 'Lock owner not provably gone; nothing is closed')])
                self.assertEqual([p[0] for p in self.null_passes()], [pass_id])

    def test_a_deleted_and_recreated_lock_file_hides_nothing(self):
        pass_id = self.dead_pass()
        for name, path in self.lock_paths().items():
            with self.subTest(name):
                lock_sources.deleted_holder(self, path)          # a live owner still holds the unlinked inode
                self.assertEqual(self.recover()['closed'], [])
                self.assertEqual([p[0] for p in self.null_passes()], [pass_id])

    def test_a_missing_lock_file_or_an_unreadable_table_is_owner_unknown(self):
        pass_id = self.dead_pass()
        lock_sources.free(self)
        os.unlink(self.lock_paths()['evidence'])
        self.assertEqual(self.recover()['closed'], [])
        open(self.lock_paths()['evidence'], 'a').close()
        lock_sources.unreadable(self)
        self.assertEqual(self.recover()['closed'], [])
        self.assertEqual([p[0] for p in self.null_passes()], [pass_id])

    def test_run_once_with_a_live_other_owner_leaves_the_dead_pass_latched(self):
        pass_id = self.dead_pass()
        lock_sources.deleted_holder(self, self.lock_paths()['ledger'])
        result = self.h.run_cycle(candidates=())
        self.assertEqual((result['status'], result['blockers']), ('RECOVERY_REQUIRED', ['OBSERVATION_RECOVERY_REQUIRED']))
        self.assertEqual([p[0] for p in self.null_passes()], [pass_id])


class CompleteUpdateGuardTests(pass_closure_tests.Base):
    def test_a_concurrently_closed_pass_is_not_overwritten_by_the_complete_update(self):
        real = EvidenceStore.save
        state = {'done': False}

        def saving(store, payload):
            key = real(store, payload)
            if type(payload) is dict and payload.get('kind') == 'paper_cycle_v1' and not state['done']:
                state['done'] = True
                pass_id = self.null_passes()[0][0]
                closure.close(store, self.progress, pass_id=pass_id, status='ABANDONED_CHARGED', cause=closure.ABANDONED_CAUSE)
            return key
        with patch.object(EvidenceStore, 'save', saving):
            result = self.h.actual_cycle()                                 # a COMPLETE entry pass (it has admissions)
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertTrue(state['done'])
        rows = self.rows()
        self.assertEqual([r[3] for r in rows], ['ABANDONED_CHARGED'])
        self.assertEqual(self.passes()[0][2], rows[0][2])                  # still bound to the closure, not the cycle result
        self.assertIsNone(self.gate())


class HoldDurabilityTests(pass_closure_tests.Base):
    dead_pass = OwnerProofTests.dead_pass          # helpers only: do not re-run OwnerProofTests' own tests
    lock_paths = OwnerProofTests.lock_paths
    recover = OwnerProofTests.recover

    def test_a_hold_that_cannot_be_written_leaves_a_sentinel_recovery_honours(self):
        pass_id = self.dead_pass()
        with patch.object(closure, 'hold', side_effect=sqlite3.OperationalError('disk I/O error')), patch.object(time, 'sleep'):
            self.assertTrue(closure.hold_durably(self.store, pass_id=pass_id, cause='LEDGER_INTEGRITY_FAILURE'))
        self.assertTrue(os.path.exists(closure.sentinel_path(self.store, pass_id)))
        lock_sources.free(self)
        result = self.recover()
        self.assertEqual((result['closed'], result['refused']), ([], []))
        self.assertEqual([p[0] for p in self.null_passes()], [pass_id])            # never abandoned
        os.unlink(closure.sentinel_path(self.store, pass_id))                       # control: without it the pass is abandonable
        self.assertEqual(self.recover()['closed'], [pass_id])

    def test_a_failing_hold_in_a_failing_pass_is_still_protected_end_to_end(self):
        def raising(progress, scan):
            raise ValueError('mint binding')
        with patch.object(closure, 'hold', side_effect=sqlite3.OperationalError('database is locked')), patch.object(time, 'sleep'):
            with self.assertRaises(ValueError):
                self.h.run_cycle(source_factory=raising)
        pass_id = self.null_passes()[0][0]
        self.assertTrue(os.path.exists(closure.sentinel_path(self.store, pass_id)))
        lock_sources.free(self)
        self.assertEqual(self.recover()['closed'], [])

    def test_hold_durably_retries_before_falling_back(self):
        pass_id = self.dead_pass()
        real = closure.hold
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise sqlite3.OperationalError('database is locked')
            return real(*args, **kwargs)
        with patch.object(closure, 'hold', flaky), patch.object(time, 'sleep'):
            self.assertTrue(closure.hold_durably(self.store, pass_id=pass_id, cause='LEDGER_INTEGRITY_FAILURE'))
        self.assertEqual((len(calls), [r[3] for r in self.rows()]), (3, ['INTEGRITY_HOLD']))
        self.assertFalse(os.path.exists(closure.sentinel_path(self.store, pass_id)))


class NoNewHardCapTests(pass_closure_tests.Base):
    def test_closure_table_has_no_count_trigger_and_warns_at_eighty_percent(self):
        self.assertNotIn('count(*)', closure.GUARDS['paper_pass_closures_insert'])
        with patch.object(closure, 'PASS_CEILING', 1):
            self.fail_entry()
        warned = [r for r in no_entry.read_refusals(self.store.path) if r['kind'] == no_entry.WARNING_KIND]
        self.assertEqual([(w['table'], w['rows'], w['limit']) for w in warned], [('paper_pass_closures', 1, 1)])

    def test_default_ceiling_is_the_existing_original_pass_ceiling(self):
        self.assertEqual(closure.PASS_CEILING, 10000)


class PreparationHandlerTests(unittest.TestCase):
    def test_a_failure_while_publishing_the_typed_rejection_holds_the_pass(self):
        from tests import test_history_first_paper_entry as fixture
        from desk import paper_history_preparation as prep, history_preparation_rejection as rejection
        h = fixture.HistoryFirstTests('test_dry_run_no_credentials_no_history_or_provider_spend')
        h.setUp()
        self.addCleanup(h.doCleanups)
        h.row['provenance'] = 'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE'
        h.save()

        def rejected(progress, item, *args):
            progress.reserve(item.target.scan_id)
            raise prep.PreparationRejected('FEATURE_HISTORY_PAGE_LIMIT')
        with patch.object(fixture.tool.cli, '_credentials'), patch.object(fixture.tool.cycle, '_history', side_effect=rejected), \
                patch.object(rejection, 'publish', side_effect=RuntimeError('publication boom')):
            with self.assertRaises(RuntimeError):
                h.invoke(live=True, systemd_credentials=True)
        store = h.f.f.progress.store
        with closing(store.connect()) as c:
            self.assertEqual([r[0] for r in c.execute('SELECT status FROM paper_pass_closures')], ['INTEGRITY_HOLD'])
            self.assertEqual(c.execute('SELECT count(*) FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()[0], 1)


class DispatcherOwnerProofTests(unittest.TestCase):
    setUp = pass_closure_tests.DispatcherClosureTests.setUp           # helpers only: do not re-run DispatcherClosureTests' own tests
    results = pass_closure_tests.DispatcherClosureTests.results
    kill_after_intent = pass_closure_tests.DispatcherClosureTests.kill_after_intent

    def test_a_live_dispatcher_on_a_recreated_lock_file_is_never_abandoned(self):
        self.assertEqual(self.kill_after_intent(), pass_closure_tests.KILLED)
        later = self.tool._now() + self.tool.ABANDON_AFTER_SECONDS + 1
        lock = str(self.h.journal) + '.dispatcher.lock'
        self.assertTrue(os.path.exists(lock))
        lock_sources.deleted_holder(self, lock)
        with patch.object(self.tool, '_now', return_value=later), patch.object(self.tool.cli, '_credentials'), \
                self.assertRaisesRegex(ValueError, 'Unresolved dispatch'):
            self.h.invoke(execute=True, systemd_credentials=True)
        self.assertEqual((self.h.count('intents'), self.h.count('results')), (1, 0))

    def test_abandoned_when_nothing_holds_the_dispatcher_lock(self):
        self.assertEqual(self.kill_after_intent(), pass_closure_tests.KILLED)
        later = self.tool._now() + self.tool.ABANDON_AFTER_SECONDS + 1
        lock_sources.free(self)
        with patch.object(self.tool, '_now', return_value=later), patch.object(self.tool.cli, '_credentials'):
            self.assertEqual(self.h.invoke(execute=True, systemd_credentials=True)['status'], 'NO_CANDIDATE')
        self.assertEqual(self.results()[0]['result']['status'], 'ABANDONED_CHARGED')


if __name__ == '__main__':
    unittest.main()
