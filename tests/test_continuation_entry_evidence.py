"""Trusted continued history-to-bank component; synthetic provider I/O only."""
import copy
import json
import sqlite3
import threading
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from desk.decision_runner import assess, consume, recent_decisions
from desk.entry_evidence import POLICY
from desk.evidence import EvidenceStore
from desk.model import canonical, digest
from desk.ownership_worker import saved_progress
from tests import test_ownership_integration as fixtures


class ContinuationEntryEvidenceTests(unittest.TestCase):
    setUp = fixtures.OwnershipIntegrationTests.setUp
    rpc = fixtures.OwnershipIntegrationTests.rpc
    run_worker = fixtures.OwnershipIntegrationTests.run_worker

    def prepare(self):
        result = self.run_worker()
        self.assertTrue(result['snapshot']['reconciled'])
        return saved_progress(self.store, self.scan)

    def decide(self, progress=None, now=110, scan=None):
        with patch('socket.socket', side_effect=AssertionError('No provider/network access')):
            return assess(self.scan if scan is None else scan, now,
                          EvidenceStore(self.evidence, read_only=True), progress)

    def gate(self, decision):
        self.assertEqual(decision['decision'], 'REJECT')
        self.assertFalse(decision['eligible_for_trading'])
        for reason in ('FUNDING_SERVICE_CLASSIFICATION_NOT_VERIFIED_FOR_ENTRY',
                       'CURRENT_HOLDER_BUNDLE_EXPOSURE_UNVERIFIED',
                       'EXACT_ENTRY_EXIT_ROUTE_POLICY_NOT_VERIFIED',
                       'LIVE_FLOW_MOMENTUM_AND_COST_INPUTS_UNVERIFIED'):
            self.assertIn(reason, decision['reasons'])
        return decision['entry_evidence']['gates']['history_snapshot']

    def publish(self, record):
        record = copy.deepcopy(record)
        record.pop('evidence_hash', None)
        key = self.store.save(record)
        with self.store.connect() as c:
            c.execute('UPDATE ownership_heads SET evidence_hash=? WHERE scan_id=?', (key, 'scan'))
        return {**record, 'evidence_hash': key}

    def test_real_persisted_fixture_verifies_only_history_snapshot_component(self):
        progress = self.prepare()
        before = self.scan['result']
        d = self.decide()
        self.assertEqual(self.gate(d)['status'], 'VERIFIED_COMPONENT')
        self.assertNotIn('HISTORY_TO_CURRENT_SNAPSHOT_NOT_RECONCILED', d['reasons'])
        self.assertEqual(d['entry_evidence']['gates']['transfer_history']['status'], 'BLOCKED')
        self.assertEqual(d['entry_evidence']['ownership_progress_hash'], progress['evidence_hash'])
        raw = d['entry_evidence']['continuation_evidence']
        self.assertTrue(raw['replay']['reconciled'])
        self.assertEqual(raw['replay']['slot'], 20)
        self.assertEqual(raw['replay']['block_time'], 105)
        for q in progress['history_queries']:
            for page in q['pages']:
                self.assertIn(page['payload_hash'], self.gate(d)['evidence_hashes'])
                self.assertIn(page['request_evidence_hash'], self.gate(d)['evidence_hashes'])
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute('SELECT result FROM scans').fetchone()[0], before)

    def test_no_summary_or_unpersisted_progress_can_verify_component(self):
        flags = {'snapshot': {'reconciled': True}, 'eligible_for_trading': True,
                 'evidence_hash': 'a' * 64, 'history_queries': [], 'requests_used': 7}
        d = self.decide(flags)
        self.assertEqual(self.gate(d)['status'], 'BLOCKED')
        self.assertIn('HISTORY_TO_CURRENT_SNAPSHOT_NOT_RECONCILED', d['reasons'])

    def test_caller_flags_are_ignored_and_reference_is_resolved_from_store(self):
        progress = self.prepare()
        progress.update(history_queries=[], requests_used=0, snapshot={'reconciled': False},
                        snapshot_evidence={}, eligible_for_trading=True, observed_at=1000)
        d = self.decide(progress, now=1000)
        self.assertEqual(self.gate(d)['status'], 'VERIFIED_COMPONENT')
        self.assertEqual(d['entry_evidence']['ownership_requests_used'], 11)
        self.assertEqual(d['observed_at'], 110)
        self.assertIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', d['reasons'])

    def test_forged_persisted_summary_does_not_override_bad_raw_bank(self):
        self.bad_balance = True
        self.run_worker()
        progress = saved_progress(self.store, self.scan)
        progress.update(snapshot={'reconciled': True, 'reasons': []},
                        history={'transfer_history_complete': True}, eligible_for_trading=True)
        self.publish(progress)
        d = self.decide()
        self.assertEqual(self.gate(d)['status'], 'BLOCKED')
        self.assertIn('HISTORY_SNAPSHOT_BALANCE_OR_CONTROL_MISMATCH', d['reasons'])

    def test_new_hash_cannot_rebind_canonical_bank_or_clock(self):
        progress = self.prepare()
        for field in ('snapshot_hash', 'block_time_hash'):
            with self.subTest(field=field):
                changed = copy.deepcopy(progress)
                record = self.store.load(changed['snapshot_evidence'][field])
                if field == 'snapshot_hash': record['result']['context']['slot'] = 21
                else: record['params'] = [21]
                changed['snapshot_evidence'][field] = self.store.save(record)
                self.publish(changed)
                self.assertEqual(self.gate(self.decide())['status'], 'BLOCKED')

    def test_wrong_canonical_slot_clock_or_finality_fails_raw_replay(self):
        progress = self.prepare()
        original_bank = progress['snapshot_evidence']
        for mode in ('slot', 'clock', 'finality'):
            with self.subTest(mode=mode):
                changed = copy.deepcopy(progress)
                snapshot = self.store.load(original_bank['snapshot_hash'])
                clock = self.store.load(original_bank['block_time_hash'])
                if mode == 'slot':
                    snapshot['result']['context']['slot'] = 21
                    clock['params'] = [21]
                elif mode == 'clock': clock['params'] = [21]
                else: snapshot['params'][1]['commitment'] = 'confirmed'
                bank, at = self.store.save(snapshot), self.store.save(clock)
                with self.store.connect() as c:
                    c.execute('UPDATE ownership_banks SET snapshot_hash=?,clock_hash=?', (bank, at))
                changed['snapshot_evidence'] = {'snapshot_hash': bank, 'block_time_hash': at}
                self.publish(changed)
                self.assertEqual(self.gate(self.decide())['status'], 'BLOCKED')

    def test_cross_scan_cross_mint_and_rehashed_source_are_rejected(self):
        progress = self.prepare()
        for mode in ('scan', 'mint', 'time'):
            with self.subTest(mode=mode):
                scan = dict(self.scan)
                if mode == 'scan':
                    scan['id'] = 'other'
                    with self.store.connect() as c:
                        c.execute('INSERT OR REPLACE INTO ownership_heads VALUES(?,?)', ('other', progress['evidence_hash']))
                else:
                    report = json.loads(scan['result'])
                    if mode == 'mint': scan['mint'] = report['mint'] = self.owner
                    else: report['observed_at'] = 1000
                    report.pop('report_hash')
                    report['report_hash'] = digest(report)
                    scan['result'] = canonical(report)
                d = self.decide(scan=scan)
                self.assertEqual(self.gate(d)['status'], 'BLOCKED')

    def test_progress_source_fields_and_noncanonical_hash_rejected(self):
        progress = self.prepare()
        for field in ('source_hash', 'source_report_hash', 'scan_id', 'kind'):
            with self.subTest(field=field):
                changed = copy.deepcopy(progress)
                changed[field] = 'forged'
                self.publish(changed)
                self.assertEqual(self.gate(self.decide())['status'], 'BLOCKED')
        self.publish(progress)
        progress['evidence_hash'] = '0' * 64
        self.assertEqual(self.gate(self.decide(progress))['status'], 'BLOCKED')

    def test_missing_and_tampered_request_raw_page_and_bank_are_blocked(self):
        progress = self.prepare()
        q = progress['history_queries'][0]
        keys = [q['pages'][0]['payload_hash'], q['pages'][0]['request_evidence_hash'],
                progress['snapshot_evidence']['snapshot_hash'], progress['snapshot_evidence']['block_time_hash']]
        for key in keys:
            with self.store.connect() as c:
                original = c.execute('SELECT payload,raw_bytes FROM pages WHERE hash=?', (key,)).fetchone()
            for mode in ('missing', 'tamper', 'encoding'):
                with self.subTest(key=key, mode=mode):
                    with self.store.connect() as c:
                        if mode == 'missing': c.execute('DELETE FROM pages WHERE hash=?', (key,))
                        else:
                            raw = b'{}'
                            payload = zlib.compress(raw) if mode == 'tamper' else b'invalid-zlib'
                            c.execute('UPDATE pages SET payload=?,raw_bytes=? WHERE hash=?', (payload, len(raw), key))
                    self.assertEqual(self.gate(self.decide())['status'], 'BLOCKED')
                    with self.store.connect() as c:
                        c.execute('INSERT OR REPLACE INTO pages VALUES(?,?,?)', (key, *original))

    def test_rehashed_query_cutoff_and_unbound_job_rejected(self):
        progress = self.prepare()
        changed = copy.deepcopy(progress)
        q = changed['history_queries'][0]
        q['slot_range']['lt'] = 22
        q.pop('evidence_hash')
        q['evidence_hash'] = digest(q)
        self.publish(changed)
        self.assertEqual(self.gate(self.decide())['status'], 'BLOCKED')
        self.publish(progress)
        with self.store.connect() as c:
            c.execute("UPDATE ownership_history SET id='forged' WHERE id=(SELECT id FROM ownership_history LIMIT 1)")
        self.assertEqual(self.gate(self.decide())['status'], 'BLOCKED')

    def test_missing_wrong_source_overbudget_or_undercharged_accounting_rejected(self):
        self.prepare()
        for source, used, ceiling in [('0' * 64, 11, 18), (digest(dict(self.scan)), 19, 18),
                                      (digest(dict(self.scan)), 8, 18), (digest(dict(self.scan)), 11, 19)]:
            with self.subTest(used=used, ceiling=ceiling, source=source):
                with self.store.connect() as c:
                    c.execute('UPDATE ownership_budgets SET source_hash=?,used=?,ceiling=?', (source, used, ceiling))
                self.assertEqual(self.gate(self.decide())['status'], 'BLOCKED')
        with self.store.connect() as c: c.execute('DELETE FROM ownership_budgets')
        self.assertEqual(self.gate(self.decide())['status'], 'BLOCKED')

    def test_stale_original_observation_remains_stale_after_successful_component(self):
        self.prepare()
        d = self.decide(now=1000)
        self.assertEqual(self.gate(d)['status'], 'VERIFIED_COMPONENT')
        self.assertEqual(d['observed_at'], 110)
        self.assertEqual(d['evaluated_at'], 1000)
        self.assertIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', d['reasons'])

    def test_partial_bank_does_not_verify_and_next_revision_preserves_original(self):
        journal = self.db.parent / 'journal.sqlite'
        self.run_worker(1)
        first = consume(self.db, journal, now=110, evidence_db=self.evidence)
        self.assertEqual(self.gate(first['decisions'][0])['status'], 'BLOCKED')
        with sqlite3.connect(journal) as c:
            original = c.execute('SELECT source_payload,decision FROM decisions').fetchone()
        self.prepare()
        later = consume(self.db, journal, now=1000, evidence_db=self.evidence)
        self.assertEqual(self.gate(later['decisions'][0])['status'], 'VERIFIED_COMPONENT')
        self.assertIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', later['decisions'][0]['reasons'])
        self.assertEqual(consume(self.db, journal, now=1000, evidence_db=self.evidence)['consumed'], 0)
        with sqlite3.connect(journal) as c:
            self.assertEqual(c.execute('SELECT source_payload,decision FROM decisions').fetchone(), original)
            self.assertEqual(c.execute('SELECT count(*) FROM decision_evaluations').fetchone()[0], 2)
        self.assertEqual(len(recent_decisions(journal)['decisions']), 1)

    def test_concurrent_consumers_record_one_revision(self):
        self.prepare()
        journal = self.db.parent / 'journal.sqlite'
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(consume, self.db, journal, now=110, evidence_db=self.evidence) for _ in range(2)]
            results = [f.result(timeout=10) for f in futures]
        self.assertEqual(sum(r['consumed'] for r in results), 1)
        with sqlite3.connect(journal) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM decisions').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM decision_evaluations').fetchone()[0], 1)

    def test_head_changes_during_replay_use_captured_revision_then_new_revision(self):
        old = self.prepare()
        journal = self.db.parent / 'journal.sqlite'
        paused, release = threading.Event(), threading.Event()
        from desk.ownership_snapshot import reconcile_snapshot
        def reconcile(*args):
            if threading.current_thread().name.startswith('consumer'):
                paused.set()
                if not release.wait(10): raise AssertionError('Fixture timeout')
            return reconcile_snapshot(*args)
        with patch('desk.ownership_snapshot.reconcile_snapshot', side_effect=reconcile), \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix='consumer') as pool:
            future = pool.submit(consume, self.db, journal, now=110, evidence_db=self.evidence)
            try:
                self.assertTrue(paused.wait(10))
                self.run_worker()
                new = saved_progress(self.store, self.scan)
                self.assertNotEqual(new['evidence_hash'], old['evidence_hash'])
            finally: release.set()
            first = future.result(timeout=10)
        d = first['decisions'][0]
        self.assertEqual(self.gate(d)['status'], 'VERIFIED_COMPONENT')
        self.assertEqual(d['entry_evidence']['ownership_progress_hash'], old['evidence_hash'])
        second = consume(self.db, journal, now=110, evidence_db=self.evidence)
        self.assertEqual(second['decisions'][0]['entry_evidence']['ownership_progress_hash'], new['evidence_hash'])
        with sqlite3.connect(journal) as c:
            policies = {r[0] for r in c.execute('SELECT policy FROM decision_evaluations')}
            self.assertEqual(policies, {POLICY + ':' + old['evidence_hash'], POLICY + ':' + new['evidence_hash']})

    def test_invalid_revision_gets_own_journal_row_without_overwriting_valid_revision(self):
        good = self.prepare()
        journal = self.db.parent / 'journal.sqlite'
        consume(self.db, journal, now=110, evidence_db=self.evidence)
        with sqlite3.connect(journal) as c:
            original = c.execute('SELECT decision FROM decisions').fetchone()[0]
        bad = copy.deepcopy(good)
        bad['source_hash'] = '0' * 64
        bad = self.publish(bad)
        result = consume(self.db, journal, now=1000, evidence_db=self.evidence)
        self.assertEqual(result['consumed'], 1)
        self.assertEqual(self.gate(result['decisions'][0])['status'], 'BLOCKED')
        with sqlite3.connect(journal) as c:
            self.assertEqual(c.execute('SELECT decision FROM decisions').fetchone()[0], original)
            policies = {r[0] for r in c.execute('SELECT policy FROM decision_evaluations')}
            self.assertEqual(policies, {POLICY + ':' + good['evidence_hash'], POLICY + ':' + bad['evidence_hash']})
        self.assertEqual(consume(self.db, journal, now=1000, evidence_db=self.evidence)['consumed'], 0)
        self.assertEqual(self.gate(recent_decisions(journal)['decisions'][0])['status'], 'BLOCKED')

    def test_new_policy_keeps_previous_policy_and_original_decision(self):
        self.prepare()
        journal = self.db.parent / 'journal.sqlite'
        consume(self.db, journal, now=110, evidence_db=self.evidence)
        with sqlite3.connect(journal) as c:
            c.execute("UPDATE decision_evaluations SET policy='persisted-entry-evidence-v3:historical'")
            c.execute("UPDATE decisions SET decision='original historical rejection'")
        result = consume(self.db, journal, now=110, evidence_db=self.evidence)
        self.assertEqual(result['consumed'], 1)
        with sqlite3.connect(journal) as c:
            self.assertEqual(c.execute('SELECT decision FROM decisions').fetchone()[0], 'original historical rejection')
            self.assertEqual(c.execute('SELECT count(*) FROM decision_evaluations').fetchone()[0], 2)
