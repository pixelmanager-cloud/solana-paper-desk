"""SYNTHETIC_TEST_ONLY: T22H - durable dispatcher holds, the abandonment proof, and the L1-L5 hold/closure fixes.

Fixtures only (synthetic bytes); no network or credentials. Lock ownership is INJECTED (tests.lock_sources), so every test runs
identically on Linux and macOS (the kill tests fork a child, but the owner proof is read from the injected table).
"""
import json
import os
import sqlite3
import ssl
import unittest
from contextlib import closing
from unittest.mock import patch

from desk import paper_cycle as cycle, paper_pass_closure as closure, paper_terminal_reconciliation as terminal
from tests import lock_sources
from tests import test_history_first_paper_entry as history_first_fixture
from tests import test_paper_entry_dispatcher as dispatch_fixture
from tests import test_pass_closure as pass_closure_tests
from tools import paper_entry_dispatcher as tool

KILLED = 137


class DispatcherBase(unittest.TestCase):
    def setUp(self):
        self.h = dispatch_fixture.DispatcherTests('test_dry_run_no_admission_credentials_or_io_and_context_activation')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        lock_sources.free(self)                      # "the dispatcher process is gone": portable owner evidence
        self.later = tool._now() + tool.ABANDON_AFTER_SECONDS + 1

    def results(self):
        with sqlite3.connect(self.h.journal) as c:
            return [json.loads(r[0]) for r in c.execute('SELECT payload FROM results ORDER BY rowid')]

    def holds(self):
        directory = os.path.dirname(str(self.h.journal))
        return sorted(n for n in os.listdir(directory) if tool.HOLD_SUFFIX in n)

    def refuse(self, acquire):
        with patch.object(tool.cli, '_credentials'), patch.object(tool.acquisition, 'acquire', side_effect=acquire):
            with self.assertRaises((ValueError, OSError)):
                self.h.invoke(execute=True, systemd_credentials=True)

    def recover(self):
        with patch.object(tool, '_now', return_value=self.later), patch.object(tool.cli, '_credentials'), \
                patch('desk.providers.helius_rpc', side_effect=AssertionError('no provider call during recovery')):
            return self.h.invoke(execute=True, systemd_credentials=True)

    def assertStillUnresolved(self):
        with self.assertRaisesRegex(ValueError, 'Unresolved dispatch'):
            self.recover()
        self.assertEqual((self.h.count('intents'), self.h.count('results')), (1, 0))

    def kill_after_intent(self):
        def work():
            with patch.object(tool.cli, '_credentials'), patch('desk.providers.helius_rpc', side_effect=lambda *a, **k: os._exit(KILLED)):
                self.h.invoke(execute=True, systemd_credentials=True)
            os._exit(0)
        pid = os.fork()
        if pid == 0:
            try:
                work()
            finally:
                os._exit(99)
        return os.WEXITSTATUS(os.waitpid(pid, 0)[1])

    def scan(self):
        with self.h.f.jobs.connect() as c:
            return c.execute('SELECT id FROM scans').fetchone()[0]


class DeliberateRefusalIsAHold(DispatcherBase):
    def test_the_blocked_evidence_status_writes_a_hold_and_is_never_abandoned(self):
        self.refuse(lambda *a, **k: {'status': 'ACQUISITION_RETRY_OR_EVIDENCE_BLOCKED', 'scan_id': None})
        self.assertEqual(len(self.holds()), 1)
        self.assertStillUnresolved()

    def test_a_tls_error_during_acquisition_writes_a_hold_and_is_never_abandoned(self):
        def acquire(research, evidence, rpc, **kwargs):
            try:
                rpc('getSlot', [])
            except ssl.SSLError:
                pass
            return {'status': 'ACQUISITION_RETRY_OR_EVIDENCE_BLOCKED', 'scan_id': None}
        with patch('desk.providers.helius_rpc', side_effect=ssl.SSLCertVerificationError('SYNTHETIC_TEST_ONLY')):
            self.refuse(acquire)
        self.assertEqual(len(self.holds()), 1)
        self.assertStillUnresolved()

    def test_an_integrity_stop_after_the_intent_writes_a_hold(self):
        self.refuse(lambda *a, **k: (_ for _ in ()).throw(ValueError('mint binding mismatch')))
        self.assertEqual(len(self.holds()), 1)
        self.assertStillUnresolved()

    def test_the_hold_names_the_intent_and_is_private(self):
        self.refuse(lambda *a, **k: {'status': 'ACQUISITION_RETRY_OR_EVIDENCE_BLOCKED', 'scan_id': None})
        (name,) = self.holds()
        path = os.path.join(os.path.dirname(str(self.h.journal)), name)
        with sqlite3.connect(self.h.journal) as c:
            identity = c.execute('SELECT id FROM intents').fetchone()[0]
        self.assertTrue(name.endswith(tool.HOLD_SUFFIX + identity))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), '0o600')
        self.assertEqual(json.loads(open(path).read())['dispatch_id'], identity)

    def test_a_transient_failure_closes_and_writes_no_hold(self):
        def acquire(research, evidence, rpc, **kwargs):
            try:
                rpc('getSlot', [])
            except TimeoutError:
                pass
            return {'status': 'ACQUISITION_RETRY_OR_EVIDENCE_BLOCKED', 'scan_id': None}
        with patch('desk.providers.helius_rpc', side_effect=TimeoutError('x')):
            self.refuse(acquire)
        self.assertEqual((self.holds(), self.h.count('results')), ([], 1))
        self.assertEqual(self.results()[0]['result']['cause'], 'TRANSPORT_ERROR')

    def test_a_hold_that_is_a_symlink_still_holds(self):
        self.refuse(lambda *a, **k: {'status': 'ACQUISITION_RETRY_OR_EVIDENCE_BLOCKED', 'scan_id': None})
        directory = os.path.dirname(str(self.h.journal))
        (name,) = self.holds()
        target = os.path.join(directory, 'elsewhere')
        os.rename(os.path.join(directory, name), target)
        os.symlink(target, os.path.join(directory, name))
        self.assertStillUnresolved()


class AbandonmentNeedsEverything(DispatcherBase):
    def test_a_killed_dispatcher_without_a_hold_is_still_abandoned(self):
        self.assertEqual(self.kill_after_intent(), KILLED)
        self.assertEqual(self.holds(), [])
        self.assertEqual(self.recover()['status'], 'NO_CANDIDATE')
        self.assertEqual(self.results()[0]['result']['status'], 'ABANDONED_CHARGED')

    def test_a_latching_attempt_original_of_the_intents_scan_prevents_abandonment(self):
        self.assertEqual(self.kill_after_intent(), KILLED)
        for index, code in enumerate(('TLS_ERROR', 'UNCLASSIFIED_ERROR')):
            self.h.f.progress.store.save({'kind': 'paper_read_attempt_v1', 'scan_id': self.scan(), 'requests_used': index + 1,
                                          'failure_code': code})
            with self.subTest(code):
                self.assertStillUnresolved()

    def test_transient_attempt_originals_do_not_prevent_abandonment(self):
        self.assertEqual(self.kill_after_intent(), KILLED)
        self.h.f.progress.store.save({'kind': 'paper_read_attempt_v1', 'scan_id': self.scan(), 'requests_used': 1,
                                      'failure_code': 'TRANSPORT_ERROR'})
        self.assertEqual(self.recover()['status'], 'NO_CANDIDATE')

    def test_an_attempt_original_of_another_scan_is_not_this_intents_evidence(self):
        self.assertEqual(self.kill_after_intent(), KILLED)
        self.h.f.progress.store.save({'kind': 'paper_read_attempt_v1', 'scan_id': 'another-scan', 'requests_used': 1,
                                      'failure_code': 'TLS_ERROR'})
        self.assertEqual(self.recover()['status'], 'NO_CANDIDATE')

    def test_a_live_owner_still_prevents_abandonment(self):
        self.assertEqual(self.kill_after_intent(), KILLED)
        lock_sources.held(self, str(self.h.journal) + '.dispatcher.lock')
        self.assertStillUnresolved()


class HistoryFirstHolds(unittest.TestCase):
    """L1/L2: paper_history_preparation.prepare."""

    def setUp(self):
        self.h = history_first_fixture.HistoryFirstTests('test_dry_run_no_credentials_no_history_or_provider_spend')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.row['provenance'] = 'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE'
        self.h.save()
        lock_sources.free(self)

    def statuses(self):
        with closing(self.h.f.f.progress.store.connect()) as c:
            names = c.execute("SELECT name FROM sqlite_master WHERE name='paper_pass_closures'").fetchall()
            statuses = [r[0] for r in c.execute('SELECT status FROM paper_pass_closures')] if names else []
            null = c.execute('SELECT count(*) FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()[0]
        return statuses, null

    def test_l1_an_sqlite_error_while_anchoring_the_ledger_is_not_a_nameerror(self):
        def transient(progress, item, *args):
            progress.reserve(item.target.scan_id)
            raise cycle.CycleBlocked('TRANSPORT_ERROR')
        from desk import paper_cycle_no_entry
        real = paper_cycle_no_entry.ledger_snapshot
        calls = []

        def snapshot(path):
            calls.append(path)
            if len(calls) == 1:                              # prepare()'s own anchor; the closure's later read works
                raise sqlite3.OperationalError('SYNTHETIC_TEST_ONLY')
            return real(path)
        with patch.object(history_first_fixture.tool.cli, '_credentials'), \
                patch.object(paper_cycle_no_entry, 'ledger_snapshot', side_effect=snapshot), \
                patch.object(history_first_fixture.tool.cycle, '_history', side_effect=transient):
            with self.assertRaises(cycle.CycleBlocked):                  # not NameError: the anchor is simply unavailable
                self.h.invoke(live=True, systemd_credentials=True)
        self.assertEqual(self.statuses(), (['FAILED_CHARGED'], 0))      # the transient fault is closed; before: a NameError left a NULL pass with no hold

    def test_l2_captured_history_changed_holds_the_pass(self):
        def changed(progress, item, *args):
            return item.history_as_of + 5, [], None
        with patch.object(history_first_fixture.tool.cli, '_credentials'), \
                patch.object(history_first_fixture.tool.cycle, '_history', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'Captured history changed'):
                self.h.invoke(live=True, systemd_credentials=True)
        self.assertEqual(self.statuses(), (['INTEGRITY_HOLD'], 1))
        store = self.h.f.f.progress.store
        self.assertEqual(terminal.gate(store, self.h.f.f.jobs.path, (), ledger_locked=str(self.h.f.path)), 'OBSERVATION_RECOVERY_REQUIRED')


class RecoveryScansTheSameSet(pass_closure_tests.Base):
    """L3: a hold sentinel must be honoured over the SAME ordered set that `pending` is taken from."""

    def test_a_sentinel_on_an_early_pass_is_honoured_when_there_are_more_than_1024_unresolved(self):
        eligible = self.store.save({'kind': 'paper_cycle_intent_v1', 'closure_v1': True, 'n': 1})
        legacy = self.store.save({'kind': 'paper_cycle_intent_v1', 'n': 2})
        # rowid order and id order are OPPOSITE: the first rows have the largest ids, so an unordered LIMIT served by the
        # id index picks a different 1024 rows than the rowid-ordered one the pending list uses.
        with closing(self.store.connect()) as c:
            c.execute('CREATE TABLE IF NOT EXISTS paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
            c.execute('BEGIN')
            for i in range(1100):
                intent = eligible if i < 4 else legacy
                c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)', ('%032x' % (10 ** 20 - i), intent))
            c.execute('COMMIT')
            first = c.execute('SELECT id FROM paper_observation_passes ORDER BY rowid LIMIT 1').fetchone()[0]
        open(closure.sentinel_path(self.store, first), 'w').write('{}')
        for lock in (str(self.h.f.jobs.path) + '.jobs-worker.lock', str(self.store.path) + '.ownership-invocation.lock',
                     str(self.h.path) + '.paper-cycle.lock'):
            open(lock, 'a').close()                           # the owner proof needs the lock files to exist
        attempted = []
        with patch.object(closure, 'close', side_effect=lambda store, progress, **kw: attempted.append(kw['pass_id'])):
            closure.recover_abandoned(self.store, self.progress, ledger_db=str(self.h.path), cfg=self.h.cfg,
                                      clock=lambda: 0, research_db=str(self.h.f.jobs.path))
        self.assertNotIn(first, attempted)
        self.assertEqual(len(attempted), 3)


class HoldBeforeResult(pass_closure_tests.Base):
    """L4: the hold reaches the disk BEFORE the result page, so a kill in between cannot leave an abandonable pass."""

    def test_a_kill_after_the_result_is_saved_cannot_lead_to_abandonment(self):
        self.h.f.fail = 'getAccountInfo'                                     # SOURCE_REQUEST_FAILED with no attempt original: must hold

        def work():
            with patch.object(cycle, '_close_unfinished', side_effect=lambda *a, **k: os._exit(pass_closure_tests.KILLED)):
                self.h.run_cycle()
        self.assertEqual(self.in_child(work), pass_closure_tests.KILLED)
        self.assertEqual(len(self.null_passes()), 1)
        self.assertEqual(self.rows(), [])                                    # the hold row was never written...
        (pass_id,) = [p[0] for p in self.null_passes()]
        self.assertTrue(os.path.exists(closure.sentinel_path(self.store, pass_id)))      # ...but the sentinel was
        outcome = closure.recover_abandoned(self.store, self.progress, ledger_db=str(self.h.path), cfg=self.h.cfg,
                                            clock=lambda: 0, research_db=str(self.h.f.jobs.path))
        self.assertEqual(outcome['closed'], [])
        self.assertEqual(len(self.null_passes()), 1)
        self.assertEqual(self.gate(), 'OBSERVATION_RECOVERY_REQUIRED')

    def test_a_closable_blocked_pass_gets_no_sentinel(self):
        first = self.fail_entry()                                            # transient connection reset with a retained original
        self.assertEqual([r[3] for r in self.rows()], ['FAILED_CHARGED'])
        pass_id = self.passes()[0][0]
        self.assertFalse(os.path.exists(closure.sentinel_path(self.store, pass_id)), first)


class ConsistencyOfBudgetCauses(pass_closure_tests.Base):
    """L5: publish's _consistent conditions apply to the closure too."""

    def test_a_forged_budget_cause_on_a_one_request_pass_does_not_verify(self):
        self.fail_entry()
        record, row = self.record(), self.rows()[0]
        self.assertEqual(record['scans'], {self.scan: {'before': 0, 'after': 1}})
        for cause in ('INVESTIGATION_REQUEST_BUDGET_EXHAUSTED', 'CYCLE_REQUEST_BUDGET_EXHAUSTED', 'FEATURE_HISTORY_PAGE_LIMIT'):
            with self.subTest(cause), self.assertRaises(ValueError):
                closure._verify(self.store, self.progress, {**record, 'cause': cause})

    def test_the_conditions_themselves(self):
        ok = {'s': {'before': 0, 'after': 18}}
        for cause in ('INVESTIGATION_REQUEST_BUDGET_EXHAUSTED', 'CYCLE_REQUEST_BUDGET_EXHAUSTED'):
            closure._consistent(cause, ok)
        # T22I item 5: the history-page condition counts RETAINED getTransactionsForAddress originals (see tests/test_t22i.py)
        closure._consistent('TRANSPORT_ERROR', {'s': {'before': 0, 'after': 1}})
        for cause, scans in (('INVESTIGATION_REQUEST_BUDGET_EXHAUSTED', {'s': {'before': 0, 'after': 17}}),
                             ('CYCLE_REQUEST_BUDGET_EXHAUSTED', {'s': {'before': 1, 'after': 18}})):
            with self.subTest(cause), self.assertRaises(closure.HoldRequired):
                closure._consistent(cause, scans)


if __name__ == '__main__':
    unittest.main()
