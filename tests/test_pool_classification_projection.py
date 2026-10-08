"""Synthetic source/history plus protected fixture receipts; never live proof."""
import base64
import copy
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from desk.evidence import EvidenceStore
from desk.history import collect_history
from desk.model import canonical, digest
from desk.ownership_worker import advance, saved_progress
from desk.pool_classification_projection import project_pool_vault
from desk.pool_receipt_ledger import ApprovedSource, CoordinatorBoundary, PoolReceiptLedger
from desk.pool_vault_admission import AcquisitionReceipt, EvidenceRefs, GENESIS, NETWORK
from desk.programs import unbase58
from desk.security import base58, TOKEN_2022
from tests import test_ownership_multihistory_integration as multi

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(Path('/proc/self/mountinfo').is_file(), 'Linux guarded ledger reads')
class PoolClassificationProjectionTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'work').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT/'work'); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); os.chmod(self.root, 0o700)
        self.h = multi.OwnershipMultiHistoryIntegrationTests(); self.h.setUp(); self.addCleanup(self.h.doCleanups)
        h = self.h; h.root = self.root; h.path = self.root/'evidence.sqlite'; h.store = EvidenceStore(h.path)
        fixture = json.loads((ROOT/'fixtures/pool-vault-admission-legacy.json').read_text())
        self.q = fixture['query']; old = h.f; first = old['accounts'][0]
        mapping = {old['mint']:self.q['mint'], first:self.q['base_vault'],
                   old['owners'][first]:self.q['pool']}
        from solders.pubkey import Pubkey
        from desk.providers import PUMP
        def curve(mint):
            return str(Pubkey.find_program_address([b'bonding-curve',bytes(Pubkey.from_string(mint))],
                                                   Pubkey.from_string(PUMP))[0])
        mapping[curve(old['mint'])] = curve(self.q['mint'])
        def raw_replace(raw):
            for before, after in mapping.items(): raw = raw.replace(unbase58(before), unbase58(after))
            return raw
        def transformed(value):
            if isinstance(value, dict):
                result = {mapping.get(k,k): transformed(v) for k,v in value.items()}
                if type(value.get('data')) is str:
                    result['data'] = base58(raw_replace(unbase58(value['data'])))
                elif type(value.get('data')) is list and len(value['data']) == 2 and value['data'][1] == 'base64':
                    result['data'] = [base64.b64encode(raw_replace(base64.b64decode(value['data'][0]))).decode(),'base64']
                return result
            if isinstance(value, list): return [transformed(v) for v in value]
            return mapping.get(value,value) if type(value) is str else value
        h.f = transformed(old); h.records = h.f['records']
        snapshot = copy.deepcopy(fixture['evidence'][fixture['refs']['snapshot']])
        snapshot['params'][1]['minContextSlot'] = 20; snapshot['result']['context']['slot'] = 20
        for index, offset, amount in ((1,36,100),(4,64,40)):
            value = snapshot['result']['value'][index]; raw = bytearray(base64.b64decode(value['data'][0]))
            raw[offset:offset+8] = amount.to_bytes(8,'little'); value['data'][0] = base64.b64encode(raw).decode()
        h.f['snapshot']['result']['value'][0] = copy.deepcopy(snapshot['result']['value'][1])
        h.f['snapshot']['result']['value'][1] = copy.deepcopy(snapshot['result']['value'][4])
        snapshot_key = h.store.save(snapshot)
        request = copy.deepcopy(fixture['evidence'][fixture['refs']['snapshot_request']])
        request.update(params=snapshot['params'],response_hash=snapshot_key)
        clock = {'kind':'rpc_response_v1','method':'getBlockTime','params':[20],'result':110}
        clock_key = h.store.save(clock)
        clock_request = {'kind':'pool_vault_request_v1','network':NETWORK,'genesis_hash':GENESIS,
                         'method':'getBlockTime','params':[20],'response_hash':clock_key}
        self.refs = EvidenceRefs(snapshot_key,h.store.save(request),clock_key,h.store.save(clock_request))
        self.receipt = AcquisitionReceipt('fixture','synthetic_fixture',NETWORK,GENESIS,
                          self.q['pool'],self.q['mint'],20,110,112,self.refs)
        boundary = CoordinatorBoundary('projection-fixture',self.root,self.root/'receipts.sqlite',h.path,
                     frozenset({ApprovedSource('fixture','synthetic_fixture')}),allow_synthetic_fixtures=True)
        self.writer = PoolReceiptLedger.open_writer(boundary)
        self.writer.publish('synthetic-point',self.receipt)
        self.reader = PoolReceiptLedger(boundary)
        _, initial = collect_history(h.f['mint'],90,120,h.transport,max_pages=2,
                                     capture=h.store.save,token_accounts='none')
        mint_key = h.store.save({'method':'getAccountInfo','params':[h.f['mint'],
            {'encoding':'base64','commitment':'confirmed'}], 'result':{'value':h.f['snapshot']['result']['value'][0]}})
        report = {'mint':h.f['mint'],'observed_at':120,'calls':3,'findings':[], 'unknowns':[],
                  'mint_evidence_hash':mint_key,'history_queries':[initial]}
        report['report_hash'] = digest(report)
        self.scan = {'id':'multi','mint':h.f['mint'],'created':120,'status':'COMPLETE','result':canonical(report)}
        self.db = self.root/'research.sqlite'
        with sqlite3.connect(self.db) as c:
            c.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
            c.execute('INSERT INTO scans VALUES(?,?,?,?,?)',tuple(self.scan.values()))
        for _ in range(4): advance(self.db,h.path,'multi',h.transport,max_calls=3)
        self.head = saved_progress(EvidenceStore(h.path,read_only=True),self.scan)

    def project(self, **changes):
        query = dict(revision_hash=self.head['evidence_hash'], account=self.q['base_vault'],
                     pool=self.q['pool'],now=120); query.update(changes)
        return project_pool_vault(self.db,self.reader,'multi',**query)

    def assert_blocked(self, result, reason):
        self.assertFalse(result['point_binding_verified'],result)
        self.assertEqual(result['label'],'UNKNOWN'); self.assertIn(reason,result['reasons'])
        self.assertFalse(result['exclusion_allowed']); self.assertFalse(result['eligible_for_trading'])

    def test_actual_history_and_ledger_point_binding_no_writes_or_provider(self):
        before = {p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        calls = len(self.h.calls)
        with patch('desk.providers.helius_rpc',side_effect=AssertionError('no provider')):
            result = self.project(); self.assertEqual(result,self.project())
        self.assertTrue(result['point_binding_verified'],result)
        self.assertEqual(result['label'],'POOL_VAULT'); self.assertEqual(result['source_hash'],digest(self.scan))
        self.assertEqual((result['snapshot_slot'],result['snapshot_time'],result['requests_used']),(20,110,13))
        for flag in ('production_point_prerequisite','historical_interval_exclusion_allowed',
                     'classification_resolved','private_control_proven','ownership_approval','chain_authenticated','eligible_for_trading'):
            self.assertIs(result[flag],False)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        self.assertEqual(calls,len(self.h.calls))

    def test_stale_or_forged_revision_wrong_account_and_pool(self):
        self.assert_blocked(self.project(revision_hash='a'*64),'SOURCE_REVISION_RAW_BANK_UNVERIFIED')
        self.assert_blocked(self.project(account=self.q['quote_vault']),'VAULT_NOT_IN_REVISION_BANK')
        self.assert_blocked(self.project(pool=self.h.f['owners'][self.h.f['accounts'][1]]),
                            'SOURCE_APPROVED_POOL_POINT_UNAVAILABLE')
        self.assert_blocked(self.project(now=172),'SOURCE_APPROVED_POOL_POINT_REJECTED')

    def test_changed_source_summary_cannot_grant_binding(self):
        report = json.loads(self.scan['result']); report.update(validated=True,source_authenticated=True)
        with sqlite3.connect(self.db) as c: c.execute('UPDATE scans SET result=?',(canonical(report),))
        self.assert_blocked(self.project(),'SOURCE_REVISION_RAW_BANK_UNVERIFIED')

    def test_raw_lamport_source_substitution_same_slot_rejects(self):
        snapshot = copy.deepcopy(self.h.store.load(self.refs.snapshot))
        snapshot['result']['value'][4]['lamports'] += 1
        key = self.h.store.save(snapshot); request = copy.deepcopy(self.h.store.load(self.refs.snapshot_request))
        request['response_hash'] = key
        refs = replace(self.refs,snapshot=key,snapshot_request=self.h.store.save(request))
        self.writer.publish('contradictory-point',replace(self.receipt,refs=refs,captured_at=113))
        self.assert_blocked(self.project(),'SOURCE_APPROVED_POOL_POINT_REJECTED')

    def test_token2022_receipts_fail_closed(self):
        snapshot = copy.deepcopy(self.h.store.load(self.refs.snapshot))
        snapshot['result']['value'][4]['owner'] = TOKEN_2022
        key = self.h.store.save(snapshot); request = copy.deepcopy(self.h.store.load(self.refs.snapshot_request))
        request['response_hash'] = key
        refs = replace(self.refs,snapshot=key,snapshot_request=self.h.store.save(request))
        self.writer.publish('token2022-point',replace(self.receipt,refs=refs,captured_at=113))
        self.assert_blocked(self.project(),'SOURCE_APPROVED_POOL_POINT_REJECTED')

    def test_same_slot_canonical_bank_metadata_substitution_rejects(self):
        record = self.h.store.load(self.head['evidence_hash'])
        refs = record['snapshot_evidence']
        bank = self.h.store.load(refs['snapshot_hash'])
        bank['result']['value'][1]['lamports'] += 1
        record['snapshot_evidence']['snapshot_hash'] = self.h.store.save(bank)
        key = self.h.store.save(record)
        with self.h.store.connect() as c:
            c.execute('UPDATE ownership_banks SET snapshot_hash=? WHERE budget=?',
                      (record['snapshot_evidence']['snapshot_hash'],'multi'))
            c.execute('UPDATE ownership_heads SET evidence_hash=? WHERE scan_id=?',(key,'multi'))
        self.head['evidence_hash'] = key
        self.assert_blocked(self.project(),'REVISION_POOL_POINT_RAW_STATE_CONFLICT')

    def test_wrong_block_time_observation_is_not_filtered(self):
        self.writer.publish('wrong-time',replace(self.receipt,snapshot_time=111,captured_at=113))
        self.assert_blocked(self.project(),'SOURCE_APPROVED_POOL_POINT_REJECTED')

    def test_unreconciled_history_is_retained_and_never_excluded(self):
        record = self.h.store.load(self.head['evidence_hash'])
        refs = record['snapshot_evidence']; bank = self.h.store.load(refs['snapshot_hash'])
        raw = bytearray(base64.b64decode(bank['result']['value'][2]['data'][0]))
        raw[64:72] = (24).to_bytes(8,'little')
        bank['result']['value'][2]['data'][0] = base64.b64encode(raw).decode()
        record['snapshot_evidence']['snapshot_hash'] = self.h.store.save(bank)
        key = self.h.store.save(record)
        with self.h.store.connect() as c:
            c.execute('UPDATE ownership_banks SET snapshot_hash=? WHERE budget=?',
                      (record['snapshot_evidence']['snapshot_hash'],'multi'))
            c.execute('UPDATE ownership_heads SET evidence_hash=? WHERE scan_id=?',(key,'multi'))
        self.head['evidence_hash'] = key
        result = self.project()
        self.assertTrue(result['point_binding_verified'],result)
        self.assertIn('SNAPSHOT_SUPPLY_NOT_RECONCILED',result['history_reasons'])
        self.assertFalse(result['historical_interval_exclusion_allowed'])
        self.assertFalse(result['classification_resolved']); self.assertFalse(result['ownership_approval'])

    def test_candidate_json_and_writer_are_not_trusted_dependencies(self):
        query = dict(revision_hash=self.head['evidence_hash'],account=self.q['base_vault'],pool=self.q['pool'],now=120)
        for ledger in ({'validated':True,'trusted_hashes':[self.refs.snapshot]},self.writer):
            self.assert_blocked(project_pool_vault(self.db,ledger,'multi',**query),
                                'PROTECTED_READ_ONLY_RECEIPT_LEDGER_REQUIRED')

    def test_persisted_self_hash_without_receipt_cannot_admit(self):
        boundary = replace(self.reader.config,ledger_id='empty-fixture',ledger_path=self.root/'empty.sqlite')
        PoolReceiptLedger.open_writer(boundary)
        self.reader = PoolReceiptLedger(boundary)
        # All valid self-hashed raw records still exist, but no source endorsement.
        self.assert_blocked(self.project(),'SOURCE_APPROVED_POOL_POINT_UNAVAILABLE')

    def test_corrupt_receipt_head_has_no_cached_policy_fallback(self):
        self.assertTrue(self.project()['point_binding_verified'])
        with sqlite3.connect(self.reader.path) as c:
            c.execute("UPDATE ledger_head SET hash=?",('a'*64,))
        self.assert_blocked(self.project(),'CLASSIFICATION_POINT_READ_OR_BINDING_UNAVAILABLE')

    def test_guard_teardown_failure_cannot_return_a_positive_prefix(self):
        from desk.control_obligations import read_guard
        @contextmanager
        def failing_guard(path):
            with read_guard(path):
                yield
            if Path(path) == self.h.path:
                raise ValueError('synthetic guard teardown failure')
        with patch('desk.pool_classification_projection.read_guard',failing_guard):
            result = self.project()
        self.assert_blocked(result,'CLASSIFICATION_POINT_READ_OR_BINDING_UNAVAILABLE')
        self.assertIsNone(result['admission'])

    def test_simulated_coordinator_source_is_only_point_prerequisite(self):
        # Trusted harness setup, never promotion of candidate JSON or live proof.
        boundary = replace(self.reader.config,ledger_id='simulated-coordinator',
            ledger_path=self.root/'coordinator.sqlite',allow_synthetic_fixtures=False,
            sources=frozenset({ApprovedSource('harness-coordinator','coordinator_capture')}))
        writer = PoolReceiptLedger.open_writer(boundary)
        writer.publish('harness-point',replace(self.receipt,source_id='harness-coordinator',
                                              source_kind='coordinator_capture'))
        self.reader = PoolReceiptLedger(boundary)
        result = self.project()
        self.assertTrue(result['point_binding_verified'],result)
        self.assertTrue(result['production_point_prerequisite'])
        self.assertEqual(result['expires_at'],172)
        for flag in ('historical_interval_exclusion_allowed','classification_resolved',
                     'exclusion_allowed','private_control_proven','ownership_approval',
                     'chain_authenticated','eligible_for_trading'):
            self.assertIs(result[flag],False)
