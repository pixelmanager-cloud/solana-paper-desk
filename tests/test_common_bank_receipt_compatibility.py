"""Offline compatibility proofs, not a mixed-profile production implementation."""
import base64
from dataclasses import replace
from pathlib import Path
import sqlite3
import unittest

from desk.common_bank_view import CommonBankError
from desk.pool_receipt_ledger import ApprovedSource, LedgerError, PoolReceiptLedger
from desk.pool_vault_admission import _bound_capture, _capture_state, _Reject, EvidenceRefs, admit_pool_vault
from tests import test_common_bank_view as fixtures


@unittest.skipUnless(Path('/proc/self/mountinfo').is_file(), 'Protected ledger requires Linux')
class CommonBankReceiptCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.CommonBankViewTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.ledger = self.f.f.reader
        self.writer = self.f.f.writer
        self.receipt = self.f.f.receipt

    def scope(self):
        return dict(pool=self.receipt.pool, mint=self.receipt.mint, slot=self.receipt.slot)

    def original_state(self):
        refs = self.receipt.refs
        records = [self.f.store.load(k) for k in
                   (refs.snapshot, refs.snapshot_request, refs.block_time, refs.block_time_request)]
        return _capture_state(_bound_capture(records, refs, self.f.pool,
                                              self.receipt.slot, self.receipt.snapshot_time))

    def union_state(self):
        view = self.f.validate()
        # Detached semantic comparison only, never an RPC envelope or receipt.
        return view, _capture_state([view.account(k) for k in self.f.pool])

    def rows(self):
        with sqlite3.connect(self.ledger.path) as c:
            return tuple(c.execute('SELECT * FROM ' + table).fetchall() for table in
                         ('ledger_descriptor', 'ledger_head', 'coordinator_receipts'))

    def test_original_union_and_v1_same_bank_semantic_join_preserves_all_bytes(self):
        rows = self.rows()
        before = {p.name:p.read_bytes() for p in self.f.f.root.iterdir() if p.is_file()}
        view, state = self.union_state()
        self.assertEqual(state, self.original_state())
        self.assertEqual((view.slot, view.block_time),
                         (self.receipt.slot, self.receipt.snapshot_time))
        with self.ledger.policy_view(**self.scope()) as policy:
            self.assertEqual(policy.policy.receipts, frozenset({self.receipt}))
        self.assertEqual(rows, self.rows())
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.f.f.root.iterdir() if p.is_file()})
        self.assertEqual(self.f.store.load(view.parent_hash), self.f.bank)
        self.assertEqual(len(self.f.bank['result']['value']), 8)
        self.assertFalse(view.diagnostic()['eligible_for_trading'])
        self.assertFalse(view.diagnostic()['source_authenticated'])

    def test_same_slot_valid_but_conflicting_union_is_not_partitioned_by_profile(self):
        original = self.original_state()
        value = self.f.bank['result']['value'][4]
        raw = bytearray(base64.b64decode(value['data'][0]))
        raw[64:72] = (41).to_bytes(8, 'little')
        value['data'][0] = base64.b64encode(raw).decode()
        self.f.bind()
        view, state = self.union_state()
        self.assertEqual(view.slot, self.receipt.slot)
        self.assertNotEqual(state, original)
        self.assertFalse(view.diagnostic()['ownership_approved'])
        # Current v1 cannot see the union: a separate ledger would lose this conflict.
        with self.ledger.policy_view(**self.scope()) as policy:
            self.assertEqual(len(policy.policy.receipts), 1)

    def test_different_clock_is_visible_even_with_identical_six_account_state(self):
        self.f.clock['result'] = 111
        self.f.bind()
        view, state = self.union_state()
        self.assertEqual(state, self.original_state())
        self.assertNotEqual(view.block_time, self.receipt.snapshot_time)

    def test_incomplete_manifest_cannot_become_an_empty_competing_set(self):
        self.f.manifest['discovery_hash'] = 'f' * 64
        self.f.key = self.f.store.save(self.f.manifest)
        with self.assertRaises(CommonBankError): self.f.validate()
        self.assertEqual(len(self.ledger.policy(**self.scope()).receipts), 1)

    def test_v1_original_binding_rejects_union_without_slicing(self):
        view = self.f.validate()
        records = [self.f.bank, self.f.store.load(self.f.manifest['bank_request_hash']),
                   self.f.clock, self.f.store.load(self.f.manifest['clock_request_hash'])]
        refs = EvidenceRefs(view.parent_hash, self.f.manifest['bank_request_hash'],
                            self.f.manifest['clock_response_hash'], self.f.manifest['clock_request_hash'])
        with self.assertRaises(_Reject):
            _bound_capture(records, refs, self.f.pool, view.slot, view.block_time)

    def test_source_profile_extension_refused_without_changing_original_chain(self):
        before = self.rows()
        config = replace(self.ledger.config, sources=frozenset({
            ApprovedSource('fixture', 'synthetic_fixture', profile='pumpswap-common-bank-parent-point-v2')}))
        for opener in (PoolReceiptLedger, PoolReceiptLedger.open_writer):
            with self.assertRaises(LedgerError): opener(config)
        self.assertEqual(before, self.rows())
        self.assertEqual(len(self.ledger.policy(**self.scope()).receipts), 1)

    def test_extra_version_table_is_not_an_old_reader_or_writer_fence(self):
        # Explicitly prove why the proposed transition needs a REQUIRED schema fence.
        with sqlite3.connect(self.ledger.path) as c:
            c.execute('CREATE TABLE proposed_format(version INTEGER NOT NULL)')
            c.execute('INSERT INTO proposed_format VALUES(2)')
        before = self.rows()
        self.assertEqual(len(self.ledger.policy(**self.scope()).receipts), 1)
        self.writer.publish('synthetic-point', self.receipt)  # exact existing idempotent ID
        self.assertEqual(before, self.rows())

    def test_required_trigger_fences_both_old_paths_without_rewriting_rows(self):
        before = self.rows()
        with sqlite3.connect(self.ledger.path) as c:
            original = c.execute("SELECT sql FROM sqlite_master WHERE name='receipt_no_replace'").fetchone()[0]
            c.execute('DROP TRIGGER receipt_no_replace')
            # Same protection plus a schema discriminator; not a production v2 migration.
            c.execute(original.replace('BEGIN', 'BEGIN SELECT 2;', 1))
        with self.assertRaises(LedgerError): self.ledger.policy(**self.scope())
        with self.assertRaises(LedgerError): self.writer.publish('other-id', self.receipt)
        self.assertEqual(before, self.rows())

    def test_transactional_schema_transition_rollback_retains_exact_v1_identity(self):
        before = self.rows()
        with sqlite3.connect(self.ledger.path) as c:
            original = c.execute("SELECT sql FROM sqlite_master WHERE name='receipt_no_replace'").fetchone()[0]
            c.execute('BEGIN IMMEDIATE')
            c.execute('CREATE TABLE proposed_format(version INTEGER NOT NULL)')
            c.execute('DROP TRIGGER receipt_no_replace')
            c.execute(original.replace('BEGIN', 'BEGIN SELECT 2;', 1))
            c.rollback()
            self.assertEqual(c.execute("SELECT sql FROM sqlite_master WHERE name='receipt_no_replace'").fetchone()[0], original)
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='proposed_format'").fetchone())
        self.assertEqual(before, self.rows())
        self.assertEqual(len(self.ledger.policy(**self.scope()).receipts), 1)

    def admit(self, refs, now):
        with self.ledger.policy_view(**self.scope()) as view:
            return admit_pool_vault(account=self.f.pool[4], pool=self.receipt.pool,
                mint=self.receipt.mint, snapshot_slot=self.receipt.slot,
                snapshot_time=self.receipt.snapshot_time, now=now, refs=refs,
                policy=view.policy, load=view.load_evidence)

    def test_expired_original_conflict_still_blocks_fresh_selected_v1(self):
        snapshot = self.f.store.load(self.receipt.refs.snapshot)
        value = snapshot['result']['value'][4]
        raw = bytearray(base64.b64decode(value['data'][0]))
        raw[64:72] = (41).to_bytes(8, 'little')
        value['data'][0] = base64.b64encode(raw).decode()
        key = self.f.store.save(snapshot)
        request = self.f.store.load(self.receipt.refs.snapshot_request)
        request['response_hash'] = key
        refs = replace(self.receipt.refs, snapshot=key,
                       snapshot_request=self.f.store.save(request))
        self.writer.publish('fresh-conflict', replace(self.receipt, refs=refs, captured_at=170))
        self.assertGreaterEqual(173, self.receipt.captured_at + 60)
        result = self.admit(refs, 173)
        self.assertIn('ACQUISITION_CAPTURE_CONFLICT', result['reasons'])
        self.assertFalse(result['eligible_for_trading'])
        self.assertEqual(len(self.ledger.policy(**self.scope()).receipts), 2)

    def test_missing_original_competing_record_is_not_ignored(self):
        refs = replace(self.receipt.refs, snapshot='f' * 64)
        self.writer.publish('missing-competing-original', replace(self.receipt, refs=refs))
        result = self.admit(self.receipt.refs, 120)
        self.assertIn('ACQUISITION_CAPTURE_AMBIGUOUS', result['reasons'])
        self.assertFalse(result['eligible_for_trading'])
        self.assertEqual(len(self.ledger.policy(**self.scope()).receipts), 2)
