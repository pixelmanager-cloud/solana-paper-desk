"""Fixture-only receipt persistence. Coordinator receipts are harness simulations.

No trusted-source authenticity or real acquisition is claimed. Persistent files
are under the checkout's local work filesystem, never volatile /tmp for this API.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from desk.evidence import EvidenceStore
from desk.model import digest
from desk.pool_receipt_ledger import ApprovedSource, CoordinatorBoundary, LedgerError, PoolReceiptLedger, SCHEMA
from desk.pool_vault_admission import AcquisitionReceipt, EvidenceRefs, admit_pool_vault

ROOT = Path(__file__).resolve().parents[1]


def _publish_process(boundary, receipt, identity, queue):
    try:
        ledger = PoolReceiptLedger.open_writer(boundary)
        queue.put(('OK', ledger.publish(identity, receipt)))
    except Exception as exc:
        queue.put(('ERROR', type(exc).__name__, str(exc)))


def _crash_process(boundary, receipt):
    ledger = PoolReceiptLedger.open_writer(boundary)
    original = ledger._append
    def interrupted(*args):
        original(*args)
        os._exit(77)
    ledger._append = interrupted
    ledger.publish('crash-publication', receipt)


@unittest.skipUnless(Path('/proc/self/mountinfo').is_file(), 'Linux local filesystem ledger contract')
class PoolReceiptLedgerTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'work').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT/'work')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); os.chmod(self.root, 0o700)
        self.store = EvidenceStore(self.root/'evidence.sqlite')
        self.fixture = json.loads((ROOT/'fixtures/pool-vault-admission-legacy.json').read_text())
        for key, payload in self.fixture['evidence'].items():
            self.assertEqual(self.store.save(payload), key)
        self.q = self.fixture['query']; self.refs = EvidenceRefs(**self.fixture['refs'])
        self.at = self.q['snapshot_time']+2
        self.receipt = AcquisitionReceipt('fixture-source', 'synthetic_fixture', 'mainnet-beta',
                         '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d', self.q['pool'], self.q['mint'],
                         self.q['snapshot_slot'], self.q['snapshot_time'], self.at, self.refs)
        self.boundary = CoordinatorBoundary('test-coordinator-ledger', self.root, self.root/'receipts.sqlite',
                         self.store.path, frozenset({ApprovedSource('fixture-source', 'synthetic_fixture')}),
                         allow_synthetic_fixtures=True)
        self.writer = PoolReceiptLedger.open_writer(self.boundary)

    def scope(self):
        return dict(pool=self.q['pool'], mint=self.q['mint'], slot=self.q['snapshot_slot'])

    def admit(self, ledger, *, refs=None, now=None, policy=None):
        return admit_pool_vault(account=self.q['base_vault'], pool=self.q['pool'], mint=self.q['mint'],
                    snapshot_slot=self.q['snapshot_slot'], snapshot_time=self.q['snapshot_time'],
                    now=self.at if now is None else now, refs=refs or self.refs,
                    policy=ledger.policy(**self.scope()) if policy is None else policy,
                    load=ledger.load_evidence)

    def rejected(self, result, reason=None):
        self.assertFalse(result['snapshot_label_admitted'])
        self.assertFalse(result['production_snapshot_exclusion_allowed'])
        self.assertFalse(result['historical_interval_exclusion_allowed'])
        self.assertFalse(result['eligible_for_trading'])
        self.assertFalse(result['chain_authenticated'])
        if reason: self.assertIn(reason, result['reasons'])

    def corrupt_policy_blocks_actual_admission(self, ledger):
        with self.assertRaises((LedgerError, sqlite3.Error, OSError)):
            ledger.policy(**self.scope())
        # The future bridge must withhold policy on any ledger failure; neither
        # an empty salvaged policy nor candidate JSON may replace it.
        result = admit_pool_vault(account=self.q['base_vault'], pool=self.q['pool'], mint=self.q['mint'],
                 snapshot_slot=self.receipt.slot, snapshot_time=self.receipt.snapshot_time, now=self.at,
                 refs=self.refs, policy=None, load=ledger.load_evidence)
        self.rejected(result, 'TRUSTED_SOURCE_POLICY_REQUIRED')

    def second_capture(self, *, malformed=False):
        snapshot = deepcopy(self.fixture['evidence'][self.refs.snapshot])
        vault = snapshot['result']['value'][4]
        if malformed:
            vault['data'] = ['!', 'base64']
        else:
            raw = bytearray(base64.b64decode(vault['data'][0])); raw[64:72] = (6001).to_bytes(8, 'little')
            vault['data'][0] = base64.b64encode(raw).decode()
        request = deepcopy(self.fixture['evidence'][self.refs.snapshot_request])
        snapshot_hash = self.store.save(snapshot); request['response_hash'] = snapshot_hash
        return replace(self.receipt, refs=replace(self.refs, snapshot=snapshot_hash,
                                                  snapshot_request=self.store.save(request)))

    def simulate_coordinator_boundary(self):
        # Separate trusted harness configuration, never promotion of persisted
        # fixture receipts. The production path is exercised without live claims.
        other_root = self.root/'coordinator'; other_root.mkdir(mode=0o700)
        boundary = CoordinatorBoundary('simulated-coordinator', other_root, other_root/'receipts.sqlite',
                         self.store.path, frozenset({ApprovedSource('coordinator-A', 'coordinator_capture'),
                                                    ApprovedSource('coordinator-B', 'coordinator_capture')}))
        writer = PoolReceiptLedger.open_writer(boundary)
        return writer, replace(self.receipt, source_id='coordinator-A', source_kind='coordinator_capture')

    def test_roundtrip_receipt_refs_policy_and_actual_admission(self):
        publication = self.writer.publish('capture-1', self.receipt)
        reader = PoolReceiptLedger(self.boundary)
        policy = reader.policy(**self.scope())
        self.assertEqual(policy.receipts, frozenset({self.receipt}))
        self.assertEqual(next(iter(policy.receipts)).refs.hashes(), self.refs.hashes())
        self.assertEqual(policy.allowed_source_ids, frozenset({'fixture-source'}))
        self.assertIn(publication, policy.policy_id)
        result = self.admit(reader)
        self.assertTrue(result['snapshot_label_admitted'])
        self.assertEqual(result['source_kind'], 'synthetic_fixture')
        self.assertFalse(result['production_snapshot_exclusion_allowed'])
        self.assertFalse(result['chain_authenticated'])
        self.assertFalse(result['continuity_verified'])
        self.assertFalse(result['eligible_for_trading'])

    def test_idempotent_append_restart_preserves_journal_identity(self):
        first = self.writer.publish('capture-1', self.receipt)
        before = self.writer.policy(**self.scope())
        writer = PoolReceiptLedger.open_writer(self.boundary)
        self.assertEqual(writer.publish('capture-1', self.receipt), first)
        self.assertEqual(writer.policy(**self.scope()), before)
        with sqlite3.connect(self.boundary.ledger_path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM coordinator_receipts').fetchone()[0], 1)

    def test_publication_identity_collision_and_alias_do_not_mutate_records(self):
        self.writer.publish('capture-1', self.receipt)
        before = self.writer.policy(**self.scope())
        for identity, receipt in [('capture-1', replace(self.receipt, captured_at=self.at+1)),
                                  ('alias-capture', self.receipt)]:
            with self.assertRaises(LedgerError): self.writer.publish(identity, receipt)
        self.assertEqual(self.writer.policy(**self.scope()), before)

    def test_candidate_json_evidence_contents_and_reader_cannot_self_issue(self):
        for candidate in [self.fixture, {'verified': True, 'receipt': self.receipt},
                          self.fixture['evidence'][self.refs.snapshot]]:
            with self.assertRaises(LedgerError): self.writer.publish('candidate', candidate)
        reader = PoolReceiptLedger(self.boundary)
        with self.assertRaises(LedgerError): reader.publish('candidate', self.receipt)
        with self.assertRaises(LedgerError): PoolReceiptLedger.open_writer({'root': str(self.root), 'verified': True})
        self.rejected(self.admit(reader), 'ACQUISITION_RECEIPT_MISSING_OR_CONFLICTING')

    def test_receipt_source_kind_network_genesis_cannot_be_reclassified(self):
        for changes in [dict(source_id='unknown'), dict(source_kind='coordinator_capture'),
                        dict(network='devnet'), dict(genesis_hash='other-chain')]:
            with self.assertRaises(LedgerError): self.writer.publish('candidate', replace(self.receipt, **changes))
        self.assertEqual(self.writer.policy(**self.scope()).receipts, frozenset())

    def test_conflicting_observations_across_allowed_sources_reach_actual_admission(self):
        writer, first = self.simulate_coordinator_boundary()
        second = replace(self.second_capture(), source_id='coordinator-B', source_kind='coordinator_capture')
        writer.publish('capture-A', first)
        positive = self.admit(writer)
        self.assertTrue(positive['production_snapshot_exclusion_allowed'])
        self.assertFalse(positive['chain_authenticated'])
        writer.publish('capture-B', second)
        policy = writer.policy(**self.scope())
        self.assertEqual(policy.receipts, frozenset({first, second}))
        for refs in (first.refs, second.refs):
            self.rejected(self.admit(writer, refs=refs), 'ACQUISITION_CAPTURE_CONFLICT')
        with self.assertRaises(TypeError): writer.policy(**self.scope(), source_ids={'coordinator-A'})
        with self.assertRaises(TypeError): writer.policy(**self.scope(), refs=first.refs)

    def test_stale_contradictions_are_not_filtered_by_policy(self):
        writer, first = self.simulate_coordinator_boundary()
        second = replace(self.second_capture(), source_id='coordinator-B', source_kind='coordinator_capture',
                         captured_at=self.q['snapshot_time'])
        writer.publish('capture-A', first); writer.publish('capture-B', second)
        now = self.q['snapshot_time']+60
        self.assertEqual(len(writer.policy(**self.scope()).receipts), 2)
        self.rejected(self.admit(writer, now=now), 'ACQUISITION_CAPTURE_CONFLICT')

    def test_malformed_raw_observations_not_dropped_for_healthy_selection(self):
        self.writer.publish('capture-good', self.receipt)
        bad = self.second_capture(malformed=True)
        self.writer.publish('capture-bad', bad)
        self.assertIn(bad, self.writer.policy(**self.scope()).receipts)
        self.rejected(self.admit(self.writer), 'ACQUISITION_CAPTURE_AMBIGUOUS')

    def test_semantically_malformed_receipt_times_are_preserved_as_blockers(self):
        self.writer.publish('capture-good', self.receipt)
        bad = replace(self.second_capture(), captured_at=-1)
        self.writer.publish('capture-bad-time', bad)
        self.assertIn(bad, self.writer.policy(**self.scope()).receipts)
        self.rejected(self.admit(self.writer), 'ACQUISITION_CAPTURE_AMBIGUOUS')

    def test_missing_raw_capture_remains_approved_observation_and_blocks_admission(self):
        self.writer.publish('capture-good', self.receipt)
        missing = replace(self.receipt, refs=replace(self.refs, snapshot='0'*64))
        self.writer.publish('capture-missing', missing)
        self.assertIn(missing, self.writer.policy(**self.scope()).receipts)
        self.rejected(self.admit(self.writer), 'ACQUISITION_CAPTURE_AMBIGUOUS')

    def test_conflicting_block_times_same_scope_are_not_filtered(self):
        self.writer.publish('capture-good', self.receipt)
        clock = deepcopy(self.fixture['evidence'][self.refs.block_time]); clock['result'] += 1
        clock_hash = self.store.save(clock)
        request = deepcopy(self.fixture['evidence'][self.refs.block_time_request]); request['response_hash'] = clock_hash
        other = replace(self.receipt, snapshot_time=clock['result'], refs=replace(self.refs,
                        block_time=clock_hash, block_time_request=self.store.save(request)))
        self.writer.publish('capture-other-time', other)
        self.assertEqual(len(self.writer.policy(**self.scope()).receipts), 2)
        self.rejected(self.admit(self.writer), 'ACQUISITION_CAPTURE_CONFLICT')

    def test_different_scopes_remain_saved_but_not_mixed_with_query(self):
        self.writer.publish('capture-good', self.receipt)
        other = replace(self.receipt, slot=self.receipt.slot+1)
        self.writer.publish('capture-next-slot', other)
        self.assertEqual(self.writer.policy(**self.scope()).receipts, frozenset({self.receipt}))
        self.assertEqual(self.writer.policy(pool=other.pool, mint=other.mint, slot=other.slot).receipts, frozenset({other}))

    def test_source_registry_and_ledger_identity_cannot_change_on_reopen(self):
        self.writer.publish('capture-good', self.receipt)
        for config in [replace(self.boundary, ledger_id='other-ledger'),
                       replace(self.boundary, max_age_seconds=300),
                       replace(self.boundary, sources=frozenset({ApprovedSource('other', 'synthetic_fixture')}))]:
            with self.assertRaises(LedgerError): PoolReceiptLedger(config)

    def test_in_process_source_policy_change_or_reader_promotion_cannot_issue(self):
        reader = PoolReceiptLedger(self.boundary)
        with self.assertRaises(AttributeError): reader.writer = True
        with self.assertRaises(LedgerError): reader.publish('candidate', self.receipt)
        self.writer.config = replace(self.boundary, max_age_seconds=300)
        self.corrupt_policy_blocks_actual_admission(self.writer)

    def test_ambiguous_registry_and_synthetic_opt_in_rejected(self):
        sources = frozenset({ApprovedSource('same', 'synthetic_fixture'), ApprovedSource('same', 'coordinator_capture')})
        for config in [replace(self.boundary, sources=sources), replace(self.boundary, allow_synthetic_fixtures=False),
                       replace(self.boundary, sources=frozenset({ApprovedSource(' source ', 'synthetic_fixture')}))]:
            with self.assertRaises(LedgerError): PoolReceiptLedger.open_writer(config)

    def test_canonical_paths_no_symlinks_relative_aliases_or_identity_overlap(self):
        alias = self.root/'alias.sqlite'; alias.symlink_to(self.boundary.ledger_path)
        for config in [replace(self.boundary, ledger_path=alias),
                       replace(self.boundary, ledger_path=Path('receipts.sqlite')),
                       replace(self.boundary, ledger_path=self.root/'..'/self.root.name/'receipts.sqlite'),
                       replace(self.boundary, evidence_path=self.boundary.ledger_path),
                       replace(self.boundary, evidence_path=self.writer.lock_path)]:
            with self.assertRaises((LedgerError, sqlite3.Error)): PoolReceiptLedger.open_writer(config)

    def test_cross_store_even_identical_content_and_paths_are_rejected(self):
        other = self.root/'other-evidence.sqlite'; shutil.copyfile(self.store.path, other)
        with self.assertRaises(LedgerError): PoolReceiptLedger(replace(self.boundary, evidence_path=other))
        plain = self.root/'candidate.json'; plain.write_text('{}')
        with self.assertRaises(sqlite3.Error): PoolReceiptLedger.open_writer(replace(self.boundary, evidence_path=plain))
        db = self.root/'research.sqlite'
        with sqlite3.connect(db) as c: c.execute('CREATE TABLE scans(id TEXT)')
        with self.assertRaises(LedgerError): PoolReceiptLedger.open_writer(replace(self.boundary, evidence_path=db))

    def test_hardlinks_and_permission_relaxation_fail_closed(self):
        linked = self.root/'alias-hard.sqlite'; os.link(self.store.path, linked)
        with self.assertRaises(LedgerError): self.writer.policy(**self.scope())
        linked.unlink()
        for path, unsafe, safe in [(self.root, 0o755, 0o700), (self.boundary.ledger_path, 0o644, 0o600),
                                  (self.store.path, 0o666, 0o644), (self.writer.lock_path, 0o644, 0o600)]:
            os.chmod(path, unsafe)
            try:
                with self.assertRaises(LedgerError): self.writer.policy(**self.scope())
            finally: os.chmod(path, safe)

    def test_evidence_inode_replacement_rejected_on_restart(self):
        path = self.store.path
        backup = path.with_name(path.name+'.copy'); shutil.copyfile(path, backup)
        os.chmod(backup, path.stat().st_mode & 0o777)
        os.replace(backup, path)
        with self.assertRaises(LedgerError): self.writer.policy(**self.scope())
        with self.assertRaises(LedgerError): PoolReceiptLedger(self.boundary)

    def test_ledger_and_lock_replacement_each_independently_rejected(self):
        for identity in ['ledger_path', 'lock_path']:
            root = self.root/identity; root.mkdir(mode=0o700)
            boundary = replace(self.boundary, root=root, ledger_path=root/'receipts.sqlite')
            ledger = PoolReceiptLedger.open_writer(boundary)
            path = ledger.path if identity == 'ledger_path' else ledger.lock_path
            copy = path.with_name(path.name+'.copy'); shutil.copyfile(path, copy); os.chmod(copy, 0o600); os.replace(copy, path)
            with self.assertRaises(LedgerError): ledger.policy(**self.scope())
            with self.assertRaises(LedgerError): PoolReceiptLedger(boundary)

    def test_missing_ledger_never_reinitializes_or_grants_admission(self):
        self.writer.publish('capture-good', self.receipt)
        self.boundary.ledger_path.unlink()
        with self.assertRaises(FileNotFoundError): PoolReceiptLedger(self.boundary)
        with self.assertRaises(LedgerError): PoolReceiptLedger.open_writer(self.boundary)
        result = admit_pool_vault(account=self.q['base_vault'], pool=self.q['pool'], mint=self.q['mint'],
                 snapshot_slot=self.receipt.slot, snapshot_time=self.receipt.snapshot_time, now=self.at,
                 refs=self.refs, policy=None, load=self.store.load)
        self.rejected(result, 'TRUSTED_SOURCE_POLICY_REQUIRED')

    def test_missing_lock_reopen_cannot_silently_replace_coordinator_identity(self):
        self.writer.publish('capture-good', self.receipt)
        self.writer.lock_path.unlink()
        self.corrupt_policy_blocks_actual_admission(self.writer)
        with self.assertRaises(FileNotFoundError): PoolReceiptLedger(self.boundary)
        with self.assertRaises(LedgerError): PoolReceiptLedger.open_writer(self.boundary)

    def test_ordinary_sql_update_delete_and_replace_cannot_rewrite_receipts(self):
        self.writer.publish('capture-good', self.receipt)
        before = self.writer.policy(**self.scope())
        with sqlite3.connect(self.boundary.ledger_path) as c:
            for sql in ["DELETE FROM coordinator_receipts", "UPDATE coordinator_receipts SET source_id='other'",
                        'INSERT OR REPLACE INTO coordinator_receipts SELECT * FROM coordinator_receipts',
                        'UPDATE coordinator_receipts SET seq=99', 'DELETE FROM ledger_descriptor',
                        'INSERT OR REPLACE INTO ledger_descriptor SELECT * FROM ledger_descriptor', 'DELETE FROM ledger_head']:
                with self.assertRaises(sqlite3.IntegrityError): c.execute(sql)
        self.assertEqual(self.writer.policy(**self.scope()), before)

    def test_partial_payload_tamper_or_missing_receipt_abort_complete_policy(self):
        self.writer.publish('capture-good', self.receipt)
        self.writer.publish('capture-two', replace(self.receipt, captured_at=self.at+1))
        with sqlite3.connect(self.boundary.ledger_path) as c:
            c.execute('DROP TRIGGER receipt_no_update')
            c.execute("UPDATE coordinator_receipts SET payload='{}' WHERE seq=2")
            c.execute(SCHEMA['receipt_no_update'])
        self.corrupt_policy_blocks_actual_admission(self.writer)
        with self.assertRaises(LedgerError): PoolReceiptLedger(self.boundary)

    def test_missing_receipt_chain_and_schema_trigger_tamper_fail_closed(self):
        self.writer.publish('capture-good', self.receipt)
        with sqlite3.connect(self.boundary.ledger_path) as c:
            c.execute('DROP TRIGGER receipt_no_delete'); c.execute('DELETE FROM coordinator_receipts')
            c.execute(SCHEMA['receipt_no_delete'])
        self.corrupt_policy_blocks_actual_admission(self.writer)
        with sqlite3.connect(self.boundary.ledger_path) as c: c.execute('DROP TRIGGER receipt_no_replace')
        with self.assertRaises(LedgerError): PoolReceiptLedger(self.boundary)

    def test_transaction_failure_rolls_back_receipt_and_head_then_retry(self):
        original = self.writer._append
        def fail_after_append(*args):
            original(*args)
            raise RuntimeError('fixture crash before commit')
        with patch.object(self.writer, '_append', side_effect=fail_after_append):
            with self.assertRaises(RuntimeError): self.writer.publish('capture-retry', self.receipt)
        self.assertEqual(PoolReceiptLedger(self.boundary).policy(**self.scope()).receipts, frozenset())
        self.writer.publish('capture-retry', self.receipt)
        self.assertTrue(self.admit(self.writer)['snapshot_label_admitted'])

    def test_concurrent_threads_idempotent_and_distinct_appends_survive_restart(self):
        def publish(index):
            writer = PoolReceiptLedger.open_writer(self.boundary)
            return writer.publish('shared-publication', self.receipt)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(publish, range(16)))
        self.assertEqual(len(set(results)), 1)
        def distinct(index):
            return PoolReceiptLedger.open_writer(self.boundary).publish(f'capture-{index}',
                                                   replace(self.receipt, captured_at=self.at+index+1))
        with ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(distinct, range(8)))
        policy = PoolReceiptLedger(self.boundary).policy(**self.scope())
        self.assertEqual(len(policy.receipts), 9)

    def test_spawned_process_publication_and_abrupt_transaction_restart(self):
        context = multiprocessing.get_context('spawn'); queue = context.Queue()
        processes = [context.Process(target=_publish_process, args=(self.boundary, self.receipt, 'shared-process', queue)) for _ in range(4)]
        for process in processes: process.start()
        for process in processes:
            process.join(15); self.assertEqual(process.exitcode, 0)
        results = [queue.get(timeout=2) for _ in processes]
        self.assertTrue(all(result[0] == 'OK' for result in results), results)
        self.assertEqual(len({result[1] for result in results}), 1)
        child = context.Process(target=_crash_process, args=(self.boundary, replace(self.receipt, captured_at=self.at+1)))
        child.start(); child.join(15); self.assertEqual(child.exitcode, 77)
        restarted = PoolReceiptLedger.open_writer(self.boundary)
        self.assertEqual(restarted.policy(**self.scope()).receipts, frozenset({self.receipt}))
        restarted.publish('crash-publication', replace(self.receipt, captured_at=self.at+1))
        self.assertEqual(len(restarted.policy(**self.scope()).receipts), 2)
        queue.close(); queue.join_thread()

    def test_scope_capacity_is_rejected_never_truncated_to_healthy_capture(self):
        # Real admission ceiling is 256; lower ledger-wide bound in this test
        # exercises the no-truncation publication guard without 10k appends.
        with patch('desk.pool_receipt_ledger.MAX_RECORDS', 1):
            self.writer.publish('capture-good', self.receipt)
            with self.assertRaises(LedgerError): self.writer.publish('capture-two', replace(self.receipt, captured_at=self.at+1))
        self.assertEqual(len(self.writer.policy(**self.scope()).receipts), 1)

    def test_actual_256_receipt_policy_ceiling_never_truncates_observations(self):
        for index in range(257):
            self.writer.publish(f'capture-{index}', replace(self.receipt, captured_at=self.at+index))
        self.corrupt_policy_blocks_actual_admission(self.writer)

    def test_reader_cannot_observe_partial_publication_before_rollback(self):
        from threading import Event
        entered, release, reading = Event(), Event(), Event()
        self.writer.publish('existing', self.receipt)
        original = self.writer._append
        def interrupted(*args):
            original(*args); entered.set()
            if not release.wait(3): raise RuntimeError('fixture coordination timeout')
            raise RuntimeError('fixture rollback')
        reader = PoolReceiptLedger(self.boundary)
        def read():
            reading.set()
            return reader.policy(**self.scope())
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(self.writer, '_append', side_effect=interrupted):
            publication = pool.submit(self.writer.publish, 'partial', replace(self.receipt, captured_at=self.at+1))
            self.assertTrue(entered.wait(2))
            reconstruction = pool.submit(read)
            self.assertTrue(reading.wait(2)); self.assertFalse(reconstruction.done())
            release.set()
            with self.assertRaises(RuntimeError): publication.result(timeout=4)
            self.assertEqual(reconstruction.result(timeout=4).receipts, frozenset({self.receipt}))

    def test_guarded_policy_view_serializes_publication_with_actual_admission(self):
        from threading import Event
        self.writer.publish('capture-good', self.receipt)
        other = self.second_capture()
        reader = PoolReceiptLedger(self.boundary)
        started = Event()
        def publish():
            started.set()
            return self.writer.publish('capture-conflict', other)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with reader.policy_view(**self.scope()) as view:
                publication = pool.submit(publish)
                self.assertTrue(started.wait(2)); self.assertFalse(publication.done())
                result = admit_pool_vault(account=self.q['base_vault'], pool=self.q['pool'], mint=self.q['mint'],
                         snapshot_slot=self.receipt.slot, snapshot_time=self.receipt.snapshot_time, now=self.at,
                         refs=self.refs, policy=view.policy, load=view.load_evidence)
                self.assertTrue(result['snapshot_label_admitted'])
                self.assertFalse(result['production_snapshot_exclusion_allowed'])
            publication.result(timeout=4)
            with self.assertRaises(LedgerError): view.load_evidence(self.refs.snapshot)
        self.rejected(self.admit(reader), 'ACQUISITION_CAPTURE_CONFLICT')

    def test_reject_volatile_tmpfs_and_special_file_identities(self):
        from desk.pool_receipt_ledger import _local_fs
        with patch.object(Path, 'read_text', return_value='1 0 0:1 / /tmp rw - tmpfs tmpfs rw'):
            with self.assertRaises(LedgerError): _local_fs(Path('/tmp'))
        fifo = self.root/'fifo'; os.mkfifo(fifo)
        with self.assertRaises(LedgerError): PoolReceiptLedger.open_writer(replace(self.boundary, evidence_path=fifo))
