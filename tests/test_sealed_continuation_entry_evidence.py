"""Read-only sealed-admission consumer attacks with existing synthetic raw replay."""
import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

from desk.decision_runner import assess, consume
from desk.entry_evidence import POLICY
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.model import canonical, digest
from desk.ownership_worker import saved_progress
from tests import test_ownership_integration as fixtures


class SealedContinuationEntryEvidenceTests(unittest.TestCase):
    rpc = fixtures.OwnershipIntegrationTests.rpc
    run_worker = fixtures.OwnershipIntegrationTests.run_worker

    def setUp(self):
        fixtures.OwnershipIntegrationTests.setUp(self)
        self.report.pop('report_hash')
        self.report['eligible_for_trading'] = False
        self.report['report_hash'] = digest(self.report)
        with sqlite3.connect(self.db) as c:
            c.execute('UPDATE scans SET result=?', (canonical(self.report),))
            c.row_factory = sqlite3.Row
            self.scan = dict(c.execute('SELECT * FROM scans').fetchone())
        self.progress = HistoryProgress(self.store)
        self.descriptor = {'kind': 'ownership_admission_v1', 'scan_id': 'scan',
                           'mint': self.mint, 'created': 110}
        self.binding = self.progress.admit('scan', self.descriptor)['descriptor_hash']
        for _ in range(7):
            self.assertTrue(self.progress.reserve('scan'))
        self.progress.prepare_source('scan', self.binding, self.scan)
        self.progress.seal_source('scan', self.binding, digest(self.scan))
        result = self.run_worker()
        self.assertTrue(result['snapshot']['reconciled'])
        self.head = saved_progress(self.store, self.scan)
        with self.store.connect() as c:
            self.admission = c.execute('SELECT * FROM ownership_admissions').fetchone()
            self.budget = c.execute('SELECT * FROM ownership_budgets').fetchone()

    def decide(self, now=110, scan=None, store=None):
        with patch('socket.socket', side_effect=AssertionError('No live/network access')):
            return assess(self.scan if scan is None else scan, now,
                          store or EvidenceStore(self.evidence, read_only=True))

    def gate(self, decision):
        self.assertEqual(decision['decision'], 'REJECT')
        self.assertFalse(decision['eligible_for_trading'])
        self.assertIn('CURRENT_HOLDER_BUNDLE_EXPOSURE_UNVERIFIED', decision['reasons'])
        self.assertIn('EXACT_ENTRY_EXIT_ROUTE_POLICY_NOT_VERIFIED', decision['reasons'])
        return decision['entry_evidence']['gates']['history_snapshot']

    def assert_blocked(self, decision=None):
        decision = decision or self.decide()
        self.assertEqual(self.gate(decision)['status'], 'BLOCKED')
        self.assertIn('CONTINUATION_RAW_EVIDENCE_UNVERIFIED', decision['reasons'])

    def reset_binding(self):
        with self.store.connect() as c:
            c.execute('DELETE FROM ownership_admissions')
            c.execute('INSERT INTO ownership_admissions VALUES(?,?,?,?,?,?)', self.admission)
            c.execute('UPDATE ownership_budgets SET source_hash=?,used=?,ceiling=? WHERE id=?',
                      (*self.budget[1:], self.budget[0]))

    def test_exact_seal_verifies_only_replayed_historical_component(self):
        result = self.decide()
        self.assertEqual(self.gate(result)['status'], 'VERIFIED_COMPONENT')
        continued = result['entry_evidence']['continuation_evidence']
        self.assertTrue(continued['replay']['reconciled'])
        self.assertEqual(continued['requests_used'], 11)
        self.assertEqual(continued['progress_hash'], self.head['evidence_hash'])
        self.assertEqual(self.binding, self.budget[1])
        self.assertNotEqual(self.binding, digest(self.scan))
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT * FROM ownership_budgets').fetchone(), self.budget)
            self.assertEqual(c.execute('SELECT * FROM ownership_admissions').fetchone(), self.admission)

    def test_admitted_prepared_unknown_null_or_missing_seal_never_uses_legacy_fallback(self):
        for state in ('ADMITTED', 'PREPARED', 'sealed', 'BROKEN', ''):
            with self.subTest(state=state):
                with self.store.connect() as c:
                    c.execute('UPDATE ownership_admissions SET state=?', (state,))
                    # Even a legacy-looking source hash must not bypass a present row.
                    c.execute('UPDATE ownership_budgets SET source_hash=?', (digest(self.scan),))
                self.assert_blocked()
                self.reset_binding()
        with self.store.connect() as c: c.execute('DELETE FROM ownership_admissions')
        self.assert_blocked()
        self.reset_binding()
        with self.store.connect() as c: c.execute('DROP TABLE ownership_admissions')
        self.assert_blocked()

    def test_missing_prepared_fields_and_completed_hash_rejected(self):
        for field in ('prepared_source', 'prepared_used', 'completed_source_hash'):
            with self.subTest(field=field):
                with self.store.connect() as c:
                    c.execute(f'UPDATE ownership_admissions SET {field}=NULL')
                self.assert_blocked()
                self.reset_binding()

    def test_malformed_and_noncanonical_json_rejected(self):
        for field in ('descriptor', 'prepared_source'):
            original = self.admission[1 if field == 'descriptor' else 3]
            for value in ('{', 'null', '[]', 'true', '{}', ' ' + original,
                          original[:-1] + ',"duplicate":0}'):
                with self.subTest(field=field, value=value[:20]):
                    with self.store.connect() as c:
                        c.execute(f'UPDATE ownership_admissions SET {field}=?', (value,))
                    self.assert_blocked()
                    self.reset_binding()

    def test_rehashed_descriptor_cannot_rebind_scan_mint_created_or_shape(self):
        for field, value in (('scan_id', 'other'), ('mint', self.owner), ('created', 111),
                             ('created', True), ('kind', 'other'), ('extra', 1)):
            with self.subTest(field=field, value=value):
                descriptor = {**self.descriptor, field: value}
                with self.store.connect() as c:
                    c.execute('UPDATE ownership_admissions SET descriptor=?', (canonical(descriptor),))
                    c.execute('UPDATE ownership_budgets SET source_hash=?', (digest(descriptor),))
                self.assert_blocked()
                self.reset_binding()

    def test_wrong_descriptor_hash_or_completed_hash_rejected(self):
        for sql, value in (('UPDATE ownership_budgets SET source_hash=?', digest(self.scan)),
                           ('UPDATE ownership_budgets SET source_hash=?', 'f' * 64),
                           ('UPDATE ownership_admissions SET completed_source_hash=?', self.binding),
                           ('UPDATE ownership_admissions SET completed_source_hash=?', 'f' * 64)):
            with self.subTest(sql=sql):
                with self.store.connect() as c: c.execute(sql, (value,))
                self.assert_blocked()
                self.reset_binding()

    def test_exact_source_bytes_and_complete_projection_required_even_if_rehashed(self):
        for mode in ('whitespace', 'mint', 'created', 'status', 'id', 'extra', 'result_object'):
            with self.subTest(mode=mode):
                source = dict(self.scan)
                if mode == 'whitespace': source['result'] = ' ' + source['result']
                elif mode == 'mint': source['mint'] = self.owner
                elif mode == 'created': source['created'] = 111
                elif mode == 'status': source['status'] = 'COMPLETE_ELIGIBLE'
                elif mode == 'id': source['id'] = 'other'
                elif mode == 'extra': source['extra'] = True
                else: source['result'] = self.report
                with self.store.connect() as c:
                    c.execute('UPDATE ownership_admissions SET prepared_source=?,completed_source_hash=?',
                              (canonical(source), digest(source)))
                self.assert_blocked()
                self.reset_binding()

    def test_completed_source_rebinding_cannot_reuse_progress(self):
        source = dict(self.scan)
        report = {**self.report, 'observed_at': 1000}
        report.pop('report_hash'); report['report_hash'] = digest(report)
        source['result'] = canonical(report)
        with self.store.connect() as c:
            c.execute('UPDATE ownership_admissions SET prepared_source=?,completed_source_hash=?',
                      (canonical(source), digest(source)))
        self.assert_blocked(self.decide(now=1000, scan=source))

    def test_prepared_calls_and_shared_counter_must_match_and_remain_charged(self):
        for prepared, used, ceiling in ((6, 11, 18), (8, 11, 18), ('bad', 11, 18),
                                        (7, 6, 18), (7, 10, 18), (7, 19, 18),
                                        (7, 11, 17), (7, 11, 19), (7, 11.5, 18)):
            with self.subTest(prepared=prepared, used=used, ceiling=ceiling):
                with self.store.connect() as c:
                    c.execute('UPDATE ownership_admissions SET prepared_used=?', (prepared,))
                    c.execute('UPDATE ownership_budgets SET used=?,ceiling=?', (used, ceiling))
                self.assert_blocked()
                self.reset_binding()

    def test_sealed_report_must_reject_eligibility_and_wrong_calls_even_when_rehashed(self):
        for field, value in (('eligible_for_trading', True), ('calls', True), ('calls', 8)):
            with self.subTest(field=field):
                report = {**self.report, field: value}
                report.pop('report_hash'); report['report_hash'] = digest(report)
                source = {**self.scan, 'result': canonical(report)}
                with self.store.connect() as c:
                    c.execute('UPDATE ownership_admissions SET prepared_source=?,completed_source_hash=?',
                              (canonical(source), digest(source)))
                self.assert_blocked(self.decide(scan=source))
                self.reset_binding()

    def test_seal_cannot_substitute_for_missing_raw_history(self):
        query = self.head['history_queries'][0]
        with self.store.connect() as c:
            c.execute('DELETE FROM pages WHERE hash=?', (query['pages'][0]['payload_hash'],))
        self.assert_blocked()

    def test_exact_seal_never_refreshes_original_age(self):
        result = self.decide(now=1000)
        self.assertEqual(self.gate(result)['status'], 'VERIFIED_COMPONENT')
        self.assertEqual(result['observed_at'], 110)
        self.assertIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', result['reasons'])

    def test_legacy_source_bound_budget_supported_with_or_without_admission_table(self):
        with self.store.connect() as c:
            c.execute('DELETE FROM ownership_admissions')
            c.execute('UPDATE ownership_budgets SET source_hash=?', (digest(self.scan),))
        self.assertEqual(self.gate(self.decide())['status'], 'VERIFIED_COMPONENT')
        with self.store.connect() as c: c.execute('DROP TABLE ownership_admissions')
        self.assertEqual(self.gate(self.decide())['status'], 'VERIFIED_COMPONENT')

    def test_readonly_consumer_performs_no_schema_or_data_writes(self):
        with self.store.connect() as c: before = list(c.iterdump())
        statements = []
        class TracedStore(EvidenceStore):
            def connect(inner):
                c = super().connect()
                c.set_trace_callback(statements.append)
                return c
        store = TracedStore(self.evidence, read_only=True)
        self.assertEqual(self.gate(self.decide(store=store))['status'], 'VERIFIED_COMPONENT')
        with self.store.connect() as c: self.assertEqual(list(c.iterdump()), before)
        self.assertTrue(any('ownership_admissions' in sql for sql in statements))
        self.assertTrue(all(sql.lstrip().upper().startswith(('SELECT', 'BEGIN', 'COMMIT'))
                            for sql in statements), statements)

    def test_admission_and_counter_reads_share_one_sqlite_snapshot(self):
        with self.store.connect() as c: c.execute('PRAGMA journal_mode=WAL')
        changed = []
        outer = self
        class RacingStore(EvidenceStore):
            def connect(inner):
                c = super().connect()
                def trace(sql):
                    if sql.startswith('SELECT descriptor,state,prepared_source') and not changed:
                        changed.append(True)
                        with outer.store.connect() as writer:
                            writer.execute("UPDATE ownership_admissions SET state='PREPARED'")
                            writer.execute('UPDATE ownership_budgets SET used=6')
                c.set_trace_callback(trace)
                return c
        store = RacingStore(self.evidence, read_only=True)
        result = self.decide(store=store)
        self.assertTrue(changed)
        self.assertEqual(self.gate(result)['status'], 'VERIFIED_COMPONENT')
        self.assertEqual(result['entry_evidence']['ownership_requests_used'], 11)
        self.assert_blocked()

    def test_missing_admission_schema_is_blocked_without_readonly_repair(self):
        with self.store.connect() as c:
            c.execute('ALTER TABLE ownership_admissions RENAME COLUMN state TO broken_state')
            before = list(c.iterdump())
        self.assert_blocked()
        with self.store.connect() as c:
            self.assertEqual(list(c.iterdump()), before)

    def test_v5_journal_revision_preserves_existing_v4_evaluation_and_source(self):
        self.assertEqual(POLICY, 'persisted-entry-evidence-v5')
        journal = self.db.parent / 'journal.sqlite'
        with patch('desk.entry_evidence.POLICY', 'persisted-entry-evidence-v4'):
            first = consume(self.db, journal, now=110, evidence_db=self.evidence)
        self.assertEqual(first['consumed'], 1)
        with sqlite3.connect(journal) as c:
            original = c.execute('SELECT source_payload,decision FROM decisions').fetchone()
            old_revision = c.execute('SELECT policy,decision FROM decision_evaluations').fetchone()
        second = consume(self.db, journal, now=1000, evidence_db=self.evidence)
        self.assertEqual(second['consumed'], 1)
        self.assertEqual(self.gate(second['decisions'][0])['status'], 'VERIFIED_COMPONENT')
        self.assertIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', second['decisions'][0]['reasons'])
        with sqlite3.connect(journal) as c:
            self.assertEqual(c.execute('SELECT source_payload,decision FROM decisions').fetchone(), original)
            self.assertEqual(c.execute('SELECT policy,decision FROM decision_evaluations WHERE policy=?',
                                       (old_revision[0],)).fetchone(), old_revision)
            self.assertEqual({r[0] for r in c.execute('SELECT policy FROM decision_evaluations')},
                {'persisted-entry-evidence-v4:' + self.head['evidence_hash'],
                 POLICY + ':' + self.head['evidence_hash']})
        self.assertEqual(consume(self.db, journal, now=1000, evidence_db=self.evidence)['consumed'], 0)
