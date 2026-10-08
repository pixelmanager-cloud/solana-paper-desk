"""Synthetic transport only; no production observation or live acceptance.

The coordinator_capture source branch is a simulated trusted-local harness;
synthetic opt-in receipts are tested separately and never production authorized.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress, ownership_lock_path
from desk.model import digest
from desk.pool_capture_bridge import CoordinatorTransport, Investigation, PoolCaptureBridge, canonical_accounts
from desk.pool_receipt_ledger import ApprovedSource, CoordinatorBoundary, PoolReceiptLedger
from desk.pool_vault_admission import GENESIS

ROOT = Path(__file__).resolve().parents[1]


class FixtureRPC:
    def __init__(self, fixture, *, mutate=None, fail=None):
        self.fixture = deepcopy(fixture); self.calls = []; self.fail = fail; self.mutate = mutate

    def __call__(self, method, params):
        self.calls.append((method, deepcopy(params)))
        if method == self.fail: raise ValueError('SECRET provider URL MUST NOT PERSIST')
        q = self.fixture['query']
        if method == 'getGenesisHash': result = GENESIS
        elif method == 'getSlot': result = q['snapshot_slot']
        elif method == 'getMultipleAccounts': result = deepcopy(self.fixture['evidence'][self.fixture['refs']['snapshot']]['result'])
        elif method == 'getBlockTime': result = q['snapshot_time']
        else: raise AssertionError('Unsupported I/O')
        if self.mutate: result = self.mutate(method, result)
        return result


def die_pending(boundary, fixture, investigation, capture):
    ledger = PoolReceiptLedger.open_writer(boundary)
    def rpc(method, params): os._exit(77)
    bridge = PoolCaptureBridge(ledger, CoordinatorTransport(next(s for s in boundary.sources if s.source_id == 'A'), rpc),
                               clock=lambda: fixture['query']['snapshot_time']+2)
    bridge.capture(capture_id=capture, investigation=investigation, pool=fixture['query']['pool'])


def die_before_publication(boundary, fixture, investigation):
    ledger = PoolReceiptLedger.open_writer(boundary)
    ledger.publish = lambda *args: os._exit(78)
    bridge = PoolCaptureBridge(ledger, CoordinatorTransport(next(s for s in boundary.sources if s.source_id == 'A'), FixtureRPC(fixture)),
                               clock=lambda: fixture['query']['snapshot_time']+2)
    bridge.capture(capture_id='recover-completed', investigation=investigation, pool=fixture['query']['pool'])


@unittest.skipUnless(Path('/proc/self/mountinfo').is_file(), 'Linux local filesystem ledger contract')
class PoolCaptureBridgeTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'work').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT/'work'); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); os.chmod(self.root, 0o700)
        self.store = EvidenceStore(self.root/'evidence.sqlite')
        self.fixture = json.loads((ROOT/'fixtures/pool-vault-admission-legacy.json').read_text())
        self.q = self.fixture['query']; self.at = self.q['snapshot_time']+2
        self.progress = HistoryProgress(self.store)
        d = {'kind': 'ownership_admission_v1', 'scan_id': 'existing-scan', 'mint': self.q['mint'], 'created': self.at}
        a = self.progress.admit('existing-scan', d)
        self.investigation = Investigation('existing-scan', a['descriptor_hash'], self.q['mint'])
        self.sources = frozenset({ApprovedSource('A', 'coordinator_capture'), ApprovedSource('B', 'coordinator_capture')})
        self.boundary = CoordinatorBoundary('simulated-local-coordinator', self.root, self.root/'receipts.sqlite', self.store.path, self.sources)
        self.ledger = PoolReceiptLedger.open_writer(self.boundary)
        self.rpc = FixtureRPC(self.fixture)
        self.bridge = self.make_bridge(self.rpc)

    def make_bridge(self, rpc, *, source='A', ledger=None, clock=None):
        ledger = ledger or self.ledger
        return PoolCaptureBridge(ledger, CoordinatorTransport(next(s for s in ledger.config.sources if s.source_id == source), rpc),
                                 clock=clock or (lambda: self.at))

    def capture(self, bridge=None, *, name='capture-1', investigation=None, pool=None):
        return (bridge or self.bridge).capture(capture_id=name, investigation=investigation or self.investigation, pool=pool or self.q['pool'])

    def receipts(self):
        return self.ledger.policy(pool=self.q['pool'], mint=self.q['mint'], slot=self.q['snapshot_slot']).receipts

    def used(self): return self.progress.admission(self.investigation.scan_id)['requests_used']

    def blocked(self, outcome, reason=None):
        self.assertEqual(outcome['status'], 'BLOCKED')
        self.assertFalse(outcome['eligible_for_trading']); self.assertFalse(outcome['chain_authenticated'])
        self.assertFalse(outcome['historical_interval_exclusion_allowed'])
        if reason: self.assertEqual(outcome['reason'], reason)

    def test_positive_exact_committed_fixture_replay_and_request_refs(self):
        result = self.capture()
        self.assertEqual(result['status'], 'CAPTURED_POINT', result)
        self.assertEqual(result['provider_calls'], 4); self.assertEqual(self.used(), 4)
        self.assertEqual([m for m, p in self.rpc.calls], ['getGenesisHash','getSlot','getMultipleAccounts','getBlockTime'])
        self.assertEqual(self.rpc.calls[2][1], [canonical_accounts(self.q['mint']), {'encoding':'base64','commitment':'finalized','minContextSlot':100}])
        self.assertEqual(self.rpc.calls[3][1], [100])
        receipt, = self.receipts()
        self.assertEqual(receipt.captured_at, self.at)
        self.assertEqual(receipt.refs.snapshot, self.fixture['refs']['snapshot'])
        self.assertEqual(receipt.refs.snapshot_request, self.fixture['refs']['snapshot_request'])
        self.assertEqual(receipt.refs.block_time, self.fixture['refs']['block_time'])
        self.assertEqual(receipt.refs.block_time_request, self.fixture['refs']['block_time_request'])
        for label in result['labels']:
            self.assertTrue(label['snapshot_label_admitted'])
            self.assertFalse(label['eligible_for_trading']); self.assertFalse(label['chain_authenticated'])
            self.assertFalse(label['historical_interval_exclusion_allowed']); self.assertFalse(label['continuity_verified'])

    def test_same_id_restart_is_zero_io_and_idempotent(self):
        first = self.capture(); receipts = self.receipts()
        rpc = FixtureRPC(self.fixture)
        second = self.capture(self.make_bridge(rpc, ledger=PoolReceiptLedger.open_writer(self.boundary)))
        self.assertEqual(second['status'], first['status']); self.assertEqual(second['provider_calls'], 0)
        self.assertEqual(rpc.calls, []); self.assertEqual(self.used(), 4); self.assertEqual(self.receipts(), receipts)

    def test_each_stage_failure_charged_and_never_retried_or_replaced(self):
        for i, method in enumerate(['getGenesisHash','getSlot','getMultipleAccounts','getBlockTime'],1):
            with self.subTest(method=method):
                # Separate pool journal for each failure case.
                self.tmp.cleanup(); self.setUp()
                rpc = FixtureRPC(self.fixture, fail=method); bridge = self.make_bridge(rpc)
                self.blocked(self.capture(bridge), 'CAPTURE_ATTEMPT_FAILED')
                self.assertEqual(self.used(), i); self.assertEqual(len(rpc.calls), i)
                self.blocked(self.capture(bridge), 'CAPTURE_IN_FLIGHT_OR_FAILED')
                self.blocked(self.capture(bridge,name='healthy-new-id'), 'PRIOR_CAPTURE_UNRESOLVED')
                self.assertEqual(self.used(), i)
                with self.store.connect() as c:
                    bodies = ' '.join(r[0] for r in c.execute('SELECT body FROM pool_capture_events'))
                    self.assertNotIn('SECRET', bodies)

    def test_every_io_observes_already_durable_shared_reservation(self):
        rpc = FixtureRPC(self.fixture); counts = []
        def checked(method, params): counts.append(self.used()); return rpc(method,params)
        result = self.capture(self.make_bridge(checked))
        self.assertEqual(result['status'], 'CAPTURED_POINT'); self.assertEqual(counts, [1,2,3,4])

    def test_existing_attempts_no_reset_at_shared18_ceiling(self):
        for _ in range(16): self.assertTrue(self.progress.reserve('existing-scan'))
        result = self.capture(); self.blocked(result, 'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED')
        self.assertEqual(self.used(), 18); self.assertEqual(result['provider_calls'], 2)
        self.assertEqual(len(self.rpc.calls), 2)
        self.blocked(self.capture(name='new-budget-bypass'), 'PRIOR_CAPTURE_UNRESOLVED')
        self.assertEqual(self.used(), 18)

    def test_zero_budget_does_not_call_transport(self):
        for _ in range(18): self.progress.reserve('existing-scan')
        result = self.capture(); self.blocked(result, 'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED')
        self.assertEqual(result['provider_calls'], 0); self.assertEqual(self.rpc.calls, [])

    def test_unknown_or_cross_mint_investigation_no_io(self):
        for binding in [replace(self.investigation,scan_id='new'), replace(self.investigation,descriptor_hash='0'*64),
                        replace(self.investigation,mint='So11111111111111111111111111111111111111112')]:
            self.blocked(self.capture(investigation=binding), 'EXISTING_INVESTIGATION_MISMATCH')
        self.assertEqual(self.used(), 0)

    def test_candidate_cannot_supply_source_receipt_or_transport_mapping(self):
        with self.assertRaises(ValueError): PoolCaptureBridge(self.ledger, {'source_id':'A','rpc':self.rpc})
        with self.assertRaises(TypeError): self.bridge.capture(capture_id='x',investigation=self.investigation,pool=self.q['pool'],source_id='B')
        with self.assertRaises(ValueError): PoolCaptureBridge(self.ledger, CoordinatorTransport(ApprovedSource('EVIL','coordinator_capture'),self.rpc))
        with self.assertRaises(ValueError): PoolCaptureBridge(PoolReceiptLedger(self.boundary),CoordinatorTransport(next(iter(self.sources)),self.rpc))
        self.assertEqual(self.used(),0)

    def test_forged_pool_rejected_without_io(self):
        self.blocked(self.capture(pool=self.q['base_vault']), 'CANONICAL_POOL_MINT_MISMATCH')
        self.assertEqual(self.used(),0)

    def test_wrong_genesis_charged_not_accepted_and_new_id_blocked(self):
        bridge = self.make_bridge(FixtureRPC(self.fixture, mutate=lambda m,r: 'wrong-cluster' if m=='getGenesisHash' else r))
        self.blocked(self.capture(bridge), 'CAPTURE_GENESIS_MISMATCH'); self.assertEqual(self.used(),1)
        self.blocked(self.capture(name='healthy'), 'PRIOR_CAPTURE_UNRESOLVED'); self.assertEqual(self.used(),1)

    def test_setup_bool_slot_is_not_integer(self):
        bridge = self.make_bridge(FixtureRPC(self.fixture,mutate=lambda m,r: True if m=='getSlot' else r))
        self.blocked(self.capture(bridge),'CAPTURE_SLOT_AMBIGUOUS'); self.assertEqual(self.used(),2)

    def test_null_snapshot_context_is_durable_ambiguous(self):
        bridge = self.make_bridge(FixtureRPC(self.fixture,mutate=lambda m,r: {'context':None,'value':[]} if m=='getMultipleAccounts' else r))
        self.blocked(self.capture(bridge),'CAPTURE_SCOPE_AMBIGUOUS'); self.assertEqual(self.used(),3)
        self.blocked(self.capture(name='healthy'),'PRIOR_CAPTURE_UNRESOLVED')

    def test_atomic_bank_advancement_preserves_floor_and_admits_only_actual_point(self):
        def advance(m, r):
            if m == 'getMultipleAccounts': r['context']['slot'] = 101
            return r
        rpc = FixtureRPC(self.fixture, mutate=advance)
        result = self.capture(self.make_bridge(rpc))
        self.assertEqual(result['status'], 'CAPTURED_POINT', result)
        self.assertEqual(result['snapshot_slot'], 101)
        self.assertEqual(result['request_min_context_slot'], 100)
        self.assertEqual(rpc.calls[2][1][1]['minContextSlot'], 100)
        self.assertEqual(rpc.calls[3], ('getBlockTime', [101]))
        receipts = self.ledger.policy(pool=self.q['pool'], mint=self.q['mint'], slot=101).receipts
        receipt, = receipts
        self.assertEqual(receipt.slot, 101)
        snapshot = self.store.load(receipt.refs.snapshot)
        manifest = self.store.load(receipt.refs.snapshot_request)
        self.assertEqual(snapshot['params'][1]['minContextSlot'], 100)
        self.assertEqual(manifest['params'], snapshot['params'])
        self.assertEqual(manifest['response_hash'], receipt.refs.snapshot)
        self.assertEqual(self.store.load(receipt.refs.block_time)['params'], [101])
        self.assertEqual(self.store.load(receipt.refs.block_time_request)['params'], [101])
        self.assertEqual(self.receipts(), frozenset()) # No receipt invented at request floor S.
        for label in result['labels']:
            self.assertEqual(label['snapshot_slot'], 101)
            self.assertEqual(label['request_min_context_slot'], 100)
            self.assertFalse(label['historical_interval_exclusion_allowed'])
            self.assertFalse(label['continuity_verified'])
            self.assertFalse(label['eligible_for_trading'])
        repeated = self.capture(self.make_bridge(rpc))
        self.assertEqual(repeated['status'], 'CAPTURED_POINT')
        self.assertEqual(repeated['provider_calls'], 0)
        self.assertEqual(self.used(), 4)

    def test_bank_below_floor_is_preserved_and_rejected_at_actual_slot(self):
        rpc = FixtureRPC(self.fixture, mutate=lambda m,r: {**r, 'context': {'slot':99}} if m=='getMultipleAccounts' else r)
        result = self.capture(self.make_bridge(rpc))
        self.blocked(result, 'POINT_ADMISSION_REJECTED')
        self.assertEqual(result['snapshot_slot'],99)
        self.assertEqual(result['request_min_context_slot'],100)
        self.assertIn('ATOMIC_SNAPSHOT_BELOW_REQUEST_FLOOR',result['labels'][0]['reasons'])
        receipt, = self.ledger.policy(pool=self.q['pool'],mint=self.q['mint'],slot=99).receipts
        self.assertEqual(self.store.load(receipt.refs.snapshot)['params'][1]['minContextSlot'],100)
        self.assertEqual(rpc.calls[3], ('getBlockTime',[99]))
        self.assertEqual(self.used(),4)

    def test_same_actual_bank_conflicts_across_sources_with_different_floors(self):
        def advance(method,result):
            if method=='getMultipleAccounts':result['context']['slot']=101
            return result
        original_rpc=FixtureRPC(self.fixture,mutate=advance)
        first_bridge=self.make_bridge(original_rpc)
        self.assertEqual(self.capture(first_bridge)['status'],'CAPTURED_POINT')
        def conflict(method,result):
            if method=='getSlot':return 99
            if method=='getMultipleAccounts':
                result['context']['slot']=101
                raw=bytearray(base64.b64decode(result['value'][4]['data'][0]))
                raw[64:72]=(6001).to_bytes(8,'little')
                result['value'][4]['data'][0]=base64.b64encode(raw).decode()
            return result
        second_rpc=FixtureRPC(self.fixture,mutate=conflict)
        second_bridge=self.make_bridge(second_rpc,source='B')
        outcome=self.capture(second_bridge,name='actual-bank-conflict')
        self.blocked(outcome,'POINT_ADMISSION_REJECTED')
        self.assertEqual(second_rpc.calls[2][1][1]['minContextSlot'],99)
        self.assertEqual(second_rpc.calls[3],('getBlockTime',[101]))
        self.assertIn('ACQUISITION_CAPTURE_CONFLICT',outcome['labels'][0]['reasons'])
        original=self.capture(first_bridge)
        self.blocked(original,'POINT_ADMISSION_REJECTED')
        self.assertIn('ACQUISITION_CAPTURE_CONFLICT',original['labels'][0]['reasons'])
        self.assertEqual(original['provider_calls'],0)
        self.assertEqual(len(self.ledger.policy(pool=self.q['pool'],mint=self.q['mint'],slot=101).receipts),2)
        self.assertEqual(self.used(),8)

    def test_same_actual_bank_equivalent_captures_with_different_floors(self):
        def first(method,result):
            if method=='getMultipleAccounts':result['context']['slot']=101
            return result
        self.assertEqual(self.capture(self.make_bridge(FixtureRPC(self.fixture,mutate=first)))['status'],'CAPTURED_POINT')
        def second(method,result):
            if method=='getSlot':return 99
            if method=='getMultipleAccounts':result['context']['slot']=101
            return result
        outcome=self.capture(self.make_bridge(FixtureRPC(self.fixture,mutate=second),source='B'),name='equivalent-bank')
        self.assertEqual(outcome['status'],'CAPTURED_POINT',outcome)
        self.assertEqual(outcome['request_min_context_slot'],99)
        self.assertEqual(outcome['labels'][0]['supporting_capture_count'],2)
        self.assertEqual(self.used(),8)

    def test_protocol_forgery_remains_approved_raw_observation_but_blocked(self):
        def forge(m,r):
            if m=='getMultipleAccounts':r['value'][4]['owner']=self.q['pool']
            return r
        result=self.capture(self.make_bridge(FixtureRPC(self.fixture,mutate=forge)))
        self.blocked(result,'POINT_ADMISSION_REJECTED'); self.assertEqual(len(self.receipts()),1)
        self.blocked(self.capture(name='healthy-retry'),'POINT_ADMISSION_REJECTED')
        self.assertEqual(len(self.receipts()),2)

    def test_same_source_contradiction_both_ref_selection_directions(self):
        self.assertEqual(self.capture()['status'],'CAPTURED_POINT')
        def alter(m,r):
            if m=='getMultipleAccounts':
                raw=bytearray(base64.b64decode(r['value'][4]['data'][0])); raw[64:72]=(6001).to_bytes(8,'little')
                r['value'][4]['data'][0]=base64.b64encode(raw).decode()
            return r
        result=self.capture(self.make_bridge(FixtureRPC(self.fixture,mutate=alter)),name='contradiction')
        self.blocked(result,'POINT_ADMISSION_REJECTED'); self.assertEqual(len(self.receipts()),2)
        self.assertIn('ACQUISITION_CAPTURE_CONFLICT',result['labels'][0]['reasons'])
        original=self.capture(); self.blocked(original,'POINT_ADMISSION_REJECTED')
        self.assertIn('ACQUISITION_CAPTURE_CONFLICT',original['labels'][0]['reasons'])
        self.assertEqual(self.used(),8)

    def test_cross_source_blocktime_conflict_and_equivalent_captures(self):
        self.capture()
        equivalent=self.capture(self.make_bridge(FixtureRPC(self.fixture),source='B'),name='source-b')
        self.assertEqual(equivalent['status'],'CAPTURED_POINT'); self.assertEqual(len(self.receipts()),2)
        bad=self.make_bridge(FixtureRPC(self.fixture,mutate=lambda m,r:r-1 if m=='getBlockTime' else r),source='B')
        result=self.capture(bad,name='source-b-conflict'); self.blocked(result,'POINT_ADMISSION_REJECTED')
        self.assertEqual(len(self.receipts()),3)
        self.blocked(self.capture(),'POINT_ADMISSION_REJECTED')

    def test_stale_completed_observation_not_pruned(self):
        self.capture()
        bridge=self.make_bridge(FixtureRPC(self.fixture),source='B',clock=lambda:self.at+61)
        result=self.capture(bridge,name='later')
        self.assertEqual(len(self.receipts()),2)
        # Original refs are stale even though a healthy equivalent later exists.
        self.blocked(self.capture(self.make_bridge(FixtureRPC(self.fixture),clock=lambda:self.at+61)),'POINT_ADMISSION_REJECTED')

    def test_completed_capture_recovers_before_publication_without_io(self):
        process=multiprocessing.get_context('fork').Process(target=die_before_publication,args=(self.boundary,self.fixture,self.investigation))
        process.start(); process.join(15); self.assertFalse(process.is_alive()); self.assertEqual(process.exitcode,78)
        self.assertEqual(self.used(),4); self.assertEqual(len(self.receipts()),0)
        result=self.capture(name='recover-completed')
        self.assertEqual(result['status'],'CAPTURED_POINT',result); self.assertEqual(result['provider_calls'],0)
        self.assertEqual(len(self.receipts()),1); self.assertEqual(self.rpc.calls,[])

    def test_actual_process_death_pending_is_durable_and_blocks_other_source(self):
        process=multiprocessing.get_context('fork').Process(target=die_pending,args=(self.boundary,self.fixture,self.investigation,'crash'))
        process.start(); process.join(15); self.assertFalse(process.is_alive()); self.assertEqual(process.exitcode,77)
        self.assertEqual(self.used(),1)
        self.blocked(self.capture(name='crash'),'CAPTURE_IN_FLIGHT_OR_FAILED')
        self.blocked(self.capture(self.make_bridge(FixtureRPC(self.fixture),source='B'),name='healthy'),'PRIOR_CAPTURE_UNRESOLVED')
        self.assertEqual(self.used(),1)

    def test_concurrent_same_capture_is_single_four_requests(self):
        rpc=FixtureRPC(self.fixture); bridge=self.make_bridge(rpc)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(lambda _:self.capture(bridge),range(2)))
        self.assertIn('CAPTURED_POINT',[r['status'] for r in results])
        self.assertEqual(self.used(),4); self.assertEqual(len(rpc.calls),4); self.assertEqual(len(self.receipts()),1)

    def test_request_and_invocation_lock_contention_precedes_io(self):
        for invocation in (True,False):
            with open(ownership_lock_path(self.store,invocation=invocation),'a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                self.blocked(self.capture(),'BUSY')
        self.assertEqual(self.used(),0)

    def test_lock_symlink_and_hardlink_rejected(self):
        target=self.root/'alias'; target.touch()
        lock=Path(ownership_lock_path(self.store,invocation=True)); lock.symlink_to(target)
        self.blocked(self.capture()); self.assertEqual(self.used(),0)
        lock.unlink(); os.link(target,lock)
        self.blocked(self.capture(),'OWNERSHIP_LOCK_INVALID'); self.assertEqual(self.used(),0)

    def test_missing_response_and_tampered_journal_block_no_salvage(self):
        self.capture()
        receipt,=self.receipts()
        with self.store.connect() as c:c.execute('DELETE FROM pages WHERE hash=?',(receipt.refs.snapshot,))
        self.blocked(self.capture()); self.assertEqual(self.used(),4)
        with self.store.connect() as c:
            c.execute('DROP TRIGGER protect_pool_capture_events_update')
            c.execute("UPDATE pool_capture_events SET body='{}' WHERE seq=2")
        self.blocked(self.capture(),'CAPTURE_SCHEMA_INVALID'); self.assertEqual(self.used(),4)

    def test_plain_sqlite_replace_all_event_unique_keys_preserves_original_rows(self):
        self.assertEqual(self.capture()['status'], 'CAPTURED_POINT')
        # A fresh plain SQLite connection defaults to recursive_triggers=0:
        # implicit REPLACE deletes would bypass the ordinary DELETE guard.
        with sqlite3.connect(self.store.path) as c:
            self.assertEqual(c.execute('PRAGMA recursive_triggers').fetchone()[0], 0)
            original = c.execute('SELECT rowid,* FROM pool_capture_events ORDER BY seq').fetchall()
            head = c.execute('SELECT * FROM pool_capture_head').fetchall()
            seq, capture, body, previous, key = original[0][1:]
            unused_seq = original[-1][1]+1
            for sql, args in (
                ('INSERT OR REPLACE INTO pool_capture_events VALUES(?,?,?,?,?)',
                 (unused_seq, 'forged-hash-collision', body, previous, key)),
                ('INSERT OR REPLACE INTO pool_capture_events VALUES(?,?,?,?,?)',
                 (seq, 'forged-seq-collision', body, previous, 'f'*64)),
                ('INSERT OR REPLACE INTO pool_capture_events(rowid,capture,body,previous,hash) VALUES(?,?,?,?,?)',
                 (original[0][0], 'forged-rowid-collision', body, previous, 'e'*64)),
            ):
                with self.subTest(sql=sql, capture=args[1]):
                    with self.assertRaises(sqlite3.IntegrityError): c.execute(sql, args)
                    c.commit()
                    self.assertEqual(c.execute('SELECT rowid,* FROM pool_capture_events ORDER BY seq').fetchall(), original)
                    self.assertEqual(c.execute('SELECT * FROM pool_capture_head').fetchall(), head)
        outcome = self.capture()
        self.assertEqual(outcome['status'], 'CAPTURED_POINT', outcome)
        self.assertEqual(outcome['provider_calls'], 0)
        self.assertEqual(self.used(), 4)

    def test_missing_journal_head_not_reinitialized(self):
        self.capture()
        with self.store.connect() as c:c.execute('DROP TABLE pool_capture_head')
        self.blocked(self.capture(),'CAPTURE_SCHEMA_INVALID'); self.assertEqual(self.used(),4)

    def test_cross_ledger_and_store_identity_cannot_rebind_journal(self):
        self.capture()
        other=PoolReceiptLedger.open_writer(replace(self.boundary,ledger_id='other',ledger_path=self.root/'other.sqlite'))
        self.blocked(self.capture(self.make_bridge(FixtureRPC(self.fixture),ledger=other)),'CAPTURE_BOUNDARY_MISMATCH')
        self.assertEqual(self.used(),4)

    def test_synthetic_opt_in_distinct_and_never_production_authorized(self):
        # Fresh journal, separately provisioned synthetic registry.
        self.tmp.cleanup(); self.setUp()
        boundary=replace(self.boundary,ledger_id='synthetic',ledger_path=self.root/'synthetic.sqlite',
                         sources=frozenset({ApprovedSource('fixture','synthetic_fixture')}),allow_synthetic_fixtures=True)
        ledger=PoolReceiptLedger.open_writer(boundary)
        result=self.capture(self.make_bridge(FixtureRPC(self.fixture),source='fixture',ledger=ledger))
        self.assertEqual(result['status'],'CAPTURED_POINT',result)
        for label in result['labels']:
            self.assertEqual(label['source_kind'],'synthetic_fixture')
            self.assertFalse(label['production_snapshot_exclusion_allowed'])

    def test_equivalent_same_source_same_second_is_idempotent_publication(self):
        self.assertEqual(self.capture()['status'], 'CAPTURED_POINT')
        outcome = self.capture(name='equivalent-second-capture')
        self.assertEqual(outcome['status'], 'CAPTURED_POINT', outcome)
        self.assertEqual(self.used(), 8)
        self.assertEqual(len(self.receipts()), 1)

    def test_receipt_ledger_missing_or_contradictory_receipts_block_actual_bridge(self):
        self.capture()
        receipt, = self.receipts()
        missing = replace(receipt, source_id='B', refs=replace(receipt.refs, snapshot='0'*64))
        self.ledger.publish('independent-approved-missing', missing)
        outcome = self.capture()
        self.blocked(outcome, 'POINT_ADMISSION_REJECTED')
        self.assertIn('ACQUISITION_CAPTURE_AMBIGUOUS', outcome['labels'][0]['reasons'])
        self.assertEqual(self.used(), 4)

    def test_guarded_policy_view_used_and_detached_policy_never_called(self):
        with patch.object(self.ledger, 'policy', side_effect=AssertionError('detached policy forbidden')):
            self.assertEqual(self.capture()['status'], 'CAPTURED_POINT')

    def test_completed_recovery_publication_survives_other_damaged_observation(self):
        self.capture()
        original, = self.receipts()
        # Create a completed contradictory raw capture then simulate death in
        # the publication window. Do not retry or invent a replacement bank.
        def mutate(method, result):
            if method == 'getMultipleAccounts':
                raw = bytearray(base64.b64decode(result['value'][4]['data'][0]))
                raw[64:72] = (6001).to_bytes(8, 'little')
                result['value'][4]['data'][0] = base64.b64encode(raw).decode()
            return result
        bridge = self.make_bridge(FixtureRPC(self.fixture, mutate=mutate), source='B')
        publish = self.ledger.publish
        def interrupt(identity, receipt):
            if receipt.source_id == 'B': raise OSError('simulated publication interruption')
            return publish(identity, receipt)
        with patch.object(self.ledger, 'publish', side_effect=interrupt):
            self.blocked(self.capture(bridge, name='completed-unpublished'))
        self.assertEqual(len(self.receipts()), 1)
        with self.store.connect() as c:
            c.execute('DELETE FROM pages WHERE hash=?', (original.refs.snapshot,))
        self.blocked(self.capture())
        self.assertEqual(len(self.receipts()), 2) # New contradiction published despite older damage.
        self.assertEqual(self.used(), 8)

    def test_exact_capture_identity_cannot_move_to_other_source_or_investigation(self):
        self.capture()
        self.blocked(self.capture(self.make_bridge(FixtureRPC(self.fixture), source='B')), 'CAPTURE_IDENTITY_MISMATCH')
        descriptor = {'kind': 'ownership_admission_v1', 'scan_id': 'other-scan', 'mint': self.q['mint'], 'created': self.at}
        admission = self.progress.admit('other-scan', descriptor)
        binding = Investigation('other-scan', admission['descriptor_hash'], self.q['mint'])
        self.blocked(self.capture(investigation=binding), 'CAPTURE_IDENTITY_MISMATCH')
        self.assertEqual(self.used(), 4)
        self.assertEqual(self.progress.admission('other-scan')['requests_used'], 0)

    def test_delayed_exact_slot_clock_does_not_refresh_snapshot_freshness(self):
        wall = [self.at]
        rpc = FixtureRPC(self.fixture)
        def delayed(method, params):
            result = rpc(method, params)
            if method == 'getBlockTime': wall[0] += 61
            return result
        outcome = self.capture(self.make_bridge(delayed, clock=lambda: wall[0]))
        self.blocked(outcome, 'POINT_ADMISSION_REJECTED')
        self.assertIn('ACQUISITION_TIME_STALE_OR_FUTURE', outcome['labels'][0]['reasons'])
        receipt, = self.receipts()
        self.assertEqual(receipt.captured_at, self.at)
        self.assertEqual(self.used(), 4)

    def test_malformed_clock_cannot_be_replaced_by_healthy_retry(self):
        rpc = FixtureRPC(self.fixture, mutate=lambda m, r: None if m == 'getBlockTime' else r)
        self.blocked(self.capture(self.make_bridge(rpc)), 'CAPTURE_SCOPE_AMBIGUOUS')
        self.assertEqual(self.used(), 4)
        self.blocked(self.capture(name='healthy-clock-retry'), 'CAPTURE_SCOPE_AMBIGUOUS')
        self.assertEqual(self.used(), 4)
        self.assertEqual(self.receipts(), frozenset())

    def test_incomplete_six_account_batch_published_as_ambiguous_not_excluded(self):
        def missing(method, result):
            if method == 'getMultipleAccounts': result['value'].pop()
            return result
        outcome = self.capture(self.make_bridge(FixtureRPC(self.fixture, mutate=missing)))
        self.blocked(outcome, 'POINT_ADMISSION_REJECTED')
        self.assertIn('ATOMIC_ACCOUNT_SET_INCOMPLETE', outcome['labels'][0]['reasons'])
        self.assertEqual(len(self.receipts()), 1)
        self.blocked(self.capture(name='healthy-batch'), 'POINT_ADMISSION_REJECTED')
        self.assertEqual(len(self.receipts()), 2)

    def test_invalid_wall_clock_blocks_without_network(self):
        result=self.capture(self.make_bridge(self.rpc,clock=lambda:True))
        self.blocked(result,'COORDINATOR_CLOCK_INVALID'); self.assertEqual(self.rpc.calls,[])
        self.assertEqual(self.used(),1) # Reservation retained, never refunded.

    def test_provider_genesis_method_is_narrow_read_only_allowlisted(self):
        from desk.providers import helius_rpc
        with patch('desk.providers.api_key',return_value='synthetic-fixture-key'), patch('desk.providers.fetch_json',return_value={'result':GENESIS}) as fetch:
            self.assertEqual(helius_rpc('getGenesisHash',[]),GENESIS)
            self.assertEqual(fetch.call_args.args[1]['method'],'getGenesisHash')
            for method in ('sendTransaction','sendBundle','requestAirdrop'):
                with self.assertRaises(ValueError):helius_rpc(method,[])
            self.assertEqual(fetch.call_count,1)
