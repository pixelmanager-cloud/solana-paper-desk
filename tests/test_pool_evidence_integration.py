"""Integrated offline transport -> bridge -> protected receipt -> point replay.

The fixed coordinator source is simulated locally, never live evidence. Only
urllib's HTTPS handler is replaced: production RPC validation and all persistence
and admission code execute unchanged. No live key or network is used.
"""
import base64
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from email.message import Message
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import HTTPSHandler, HTTPHandler, build_opener
from urllib.response import addinfourl

from desk import coordinator_rpc
from desk.account_classification import classify_account
from desk.classification_exposure_adapter import ReplayScope, adapt_classification
from desk.distribution_exposure import Kind
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.model import digest
from desk.pool_capture_bridge import CoordinatorTransport, Investigation, PoolCaptureBridge, canonical_accounts
from desk.pool_receipt_ledger import CoordinatorBoundary, PoolReceiptLedger
from desk.pool_vault_admission import GENESIS, admit_pool_vault
from desk.programs import unbase58
from desk.providers import SOL
from desk.security import TOKEN_PROGRAM
from desk.sell_fees import check_sell_fee_totals

ROOT = Path(__file__).resolve().parents[1]
KEY = 'SYNTHETIC_OFFLINE_POOL_TRANSPORT_ONLY'


class Harness:
    def __init__(self, test, mutate=None):
        (ROOT/'work').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=ROOT/'work')
        test.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve(); root.chmod(0o700)
        self.fixture = json.loads((ROOT/'fixtures/pool-vault-admission-legacy.json').read_text())
        self.query = self.fixture['query']; self.floor = self.query['snapshot_slot']; self.bank = self.floor+7
        self.wall = self.query['snapshot_time']+2
        self.store = EvidenceStore(root/'evidence.sqlite')
        self.progress = HistoryProgress(self.store)
        descriptor = {'kind':'ownership_admission_v1', 'scan_id':'integrated-pool',
                      'mint':self.query['mint'], 'created':self.wall}
        admission = self.progress.admit('integrated-pool', descriptor)
        self.binding = Investigation('integrated-pool', admission['descriptor_hash'], self.query['mint'])
        rpc = coordinator_rpc.HeliusMainnetRPC()
        self.boundary = CoordinatorBoundary('simulated-fixed-source', root, root/'receipts.sqlite',
                                           self.store.path, frozenset({rpc.source}))
        self.ledger = PoolReceiptLedger.open_writer(self.boundary)
        self.bridge = PoolCaptureBridge(self.ledger, CoordinatorTransport(rpc.source, rpc), clock=lambda:self.wall)
        self.requests = []
        harness = self

        class LocalHTTPS(HTTPSHandler):
            def https_open(self, request):
                payload = json.loads(request.data)
                method, params = payload['method'], payload['params']
                used = harness.progress.admission(harness.binding.scan_id)['requests_used']
                test.assertEqual(used, len(harness.requests)+1)
                test.assertEqual(payload['jsonrpc'], '2.0'); test.assertEqual(payload['id'], 1)
                harness.requests.append((method, deepcopy(params), used))
                if method == 'getGenesisHash': result = GENESIS
                elif method == 'getSlot': result = harness.floor
                elif method == 'getMultipleAccounts':
                    result = deepcopy(harness.fixture['evidence'][harness.fixture['refs']['snapshot']]['result'])
                    result['context']['slot'] = harness.bank
                elif method == 'getBlockTime':
                    test.assertEqual(params, [harness.bank])
                    result = harness.query['snapshot_time']
                else: raise AssertionError('Unexpected method')
                if mutate: result = mutate(method, result)
                body = json.dumps({'jsonrpc':'2.0', 'id':1, 'result':result}).encode()
                headers = Message(); headers['Content-Length'] = str(len(body))
                response = addinfourl(io.BytesIO(body), headers, request.full_url, 200)
                response.msg = 'synthetic local response'
                return response

        class NoHTTP(HTTPHandler):
            def http_open(self, request): raise AssertionError('No fallback network')
        self.opener = build_opener(coordinator_rpc._NoRedirect(), LocalHTTPS(), NoHTTP())

    def capture(self, **changes):
        args = dict(capture_id='integrated-capture', investigation=self.binding, pool=self.query['pool'])
        args.update(changes)
        with patch.dict(os.environ, {'HELIUS_API_KEY':KEY}), \
             patch.object(coordinator_rpc, 'build_opener', return_value=self.opener), \
             patch('socket.create_connection', side_effect=AssertionError('No network')):
            return self.bridge.capture(**args)

    def reader(self): return PoolReceiptLedger(self.boundary)

    def receipt(self):
        receipt, = self.reader().policy(pool=self.query['pool'], mint=self.query['mint'], slot=self.bank).receipts
        return receipt

    def labels(self, **changes):
        receipt = self.receipt(); reader = self.reader()
        with reader.policy_view(pool=self.query['pool'], mint=self.query['mint'], slot=self.bank) as view:
            results = []
            for account in canonical_accounts(self.query['mint'])[4:]:
                args = dict(account=account, pool=self.query['pool'], mint=self.query['mint'],
                            snapshot_slot=self.bank, snapshot_time=receipt.snapshot_time,
                            now=self.wall, refs=receipt.refs, policy=view.policy, load=view.load_evidence)
                args.update(changes); results.append(admit_pool_vault(**args))
            return results

    def records(self):
        result = []
        for path in (self.store.path, self.boundary.ledger_path):
            with closing(EvidenceStore(path, read_only=True).connect()) as connection:
                tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                result.append([(table, connection.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall()) for table in tables])
        return result


@unittest.skipUnless(Path('/proc/self/mountinfo').is_file(), 'Linux protected local receipt contract')
class PoolEvidenceIntegrationTests(unittest.TestCase):
    def point_only(self, label):
        for flag in ('chain_authenticated', 'historical_interval_exclusion_allowed', 'continuity_verified',
                     'private_control_proven', 'ownership_approval', 'eligible_for_trading'):
            self.assertIs(label[flag], False)

    def test_fixed_transport_receipt_reader_replays_actual_bank_not_request_floor(self):
        h = Harness(self); result = h.capture()
        self.assertEqual(result['status'], 'CAPTURED_POINT', result)
        self.assertEqual(result['provider_calls'], 4)
        self.assertEqual([used for _, _, used in h.requests], [1,2,3,4])
        self.assertEqual(h.requests[2][:2], ('getMultipleAccounts', [canonical_accounts(h.query['mint']),
                         {'encoding':'base64','commitment':'finalized','minContextSlot':h.floor}]))
        receipt = h.receipt(); self.assertEqual(receipt.slot, h.bank)
        self.assertEqual(receipt.source_id, coordinator_rpc.HeliusMainnetRPC().source.source_id)
        snapshot = h.store.load(receipt.refs.snapshot); request = h.store.load(receipt.refs.snapshot_request)
        self.assertEqual(snapshot['result']['context']['slot'], h.bank)
        self.assertEqual(snapshot['params'][1]['minContextSlot'], h.floor)
        self.assertEqual(request['params'], snapshot['params'])
        self.assertEqual(request['response_hash'], digest(snapshot))
        self.assertEqual(h.store.load(receipt.refs.block_time_request)['params'], [h.bank])
        self.assertFalse(h.reader().policy(pool=h.query['pool'], mint=h.query['mint'], slot=h.floor).receipts)
        original = h.records()
        self.assertNotIn(KEY,repr(original))
        admission = h.progress.admission(h.binding.scan_id)
        self.assertEqual((admission['requests_used'],admission['request_ceiling']),(4,18))
        self.assertEqual(admission['descriptor_hash'],h.binding.descriptor_hash)
        for label in h.labels():
            self.assertEqual(label['label'], 'POOL_VAULT'); self.assertTrue(label['snapshot_label_admitted'])
            self.assertEqual(label['snapshot_slot'], h.bank)
            self.assertEqual(label['request_min_context_slot'], h.floor); self.point_only(label)
        for label in h.labels(snapshot_slot=h.floor):
            self.assertFalse(label['snapshot_label_admitted'])
            self.assertIn('ACQUISITION_SCOPE_MISMATCH', label['reasons'])
        self.assertEqual(h.records(), original); self.assertEqual(len(h.requests), 4)

    def test_freshness_expiry_through_read_only_replay_preserves_original_bank_and_age(self):
        h = Harness(self); self.assertEqual(h.capture()['status'], 'CAPTURED_POINT')
        receipt = h.receipt(); original = h.records()
        h.wall = receipt.captured_at+h.boundary.max_age_seconds-1
        self.assertTrue(all(label['snapshot_label_admitted'] for label in h.labels()))
        h.wall += 1
        for label in h.labels():
            self.assertEqual(label['label'], 'UNKNOWN'); self.point_only(label)
            self.assertIn('ACQUISITION_TIME_STALE_OR_FUTURE', label['reasons'])
        replay = h.capture()
        self.assertEqual(replay['status'], 'BLOCKED'); self.assertEqual(replay['provider_calls'], 0)
        self.assertEqual(h.receipt(), receipt); self.assertEqual(h.records(), original)
        self.assertEqual(len(h.requests), 4)

    def test_wrong_raw_vault_mint_or_authority_survives_receipt_but_blocks_both_labels(self):
        for index, offset, replacement in ((4,0,unbase58(SOL)), (5,32,bytes(32))):
            with self.subTest(index=index, offset=offset):
                def mutate(method, result):
                    if method == 'getMultipleAccounts':
                        account = result['value'][index]
                        raw = bytearray(base64.b64decode(account['data'][0])); raw[offset:offset+32] = replacement
                        account['data'][0] = base64.b64encode(raw).decode()
                    return result
                h = Harness(self, mutate); result = h.capture()
                self.assertEqual(result['status'], 'BLOCKED'); self.assertEqual(result['provider_calls'], 4)
                snapshot = h.store.load(h.receipt().refs.snapshot)
                self.assertEqual(base64.b64decode(snapshot['result']['value'][index]['data'][0])[offset:offset+32],replacement)
                for label in h.labels():
                    self.assertEqual(label['label'], 'UNKNOWN')
                    self.assertIn('VAULT_MINT_OR_AUTHORITY_MISMATCH',label['reasons']); self.point_only(label)

    def test_wrong_query_mint_vault_and_coordinator_config_cannot_rebind_persisted_capture(self):
        h = Harness(self); h.capture(); original = h.records()
        for changes, reason in (({'mint':SOL}, 'ACQUISITION_SCOPE_MISMATCH'),
                                ({'account':h.query['pool']}, 'VAULT_ACCOUNT_ATA_MISMATCH')):
            for label in h.labels(**changes):
                self.assertFalse(label['snapshot_label_admitted']); self.assertIn(reason,label['reasons'])
        for boundary in (replace(h.boundary,max_age_seconds=61),
                         replace(h.boundary,sources=frozenset({replace(next(iter(h.boundary.sources)),source_id='other')}))):
            with self.assertRaises(ValueError): PoolReceiptLedger(boundary)
        binding = replace(h.binding, mint=SOL)
        self.assertEqual(h.capture(investigation=binding)['reason'], 'EXISTING_INVESTIGATION_MISMATCH')
        self.assertEqual(h.records(), original); self.assertEqual(len(h.requests),4)

    def test_point_receipt_label_cannot_be_promoted_to_interval_classification(self):
        h = Harness(self); h.capture(); label = h.labels()[0]
        snapshot = h.store.load(h.receipt().refs.snapshot)
        raw = {'genesis_hash':GENESIS, 'method':'getAccountInfo',
               'params':[label['account'], {'encoding':'base64','commitment':'finalized'}],
               'result':{'context':{'slot':h.bank}, 'value':snapshot['result']['value'][4]}}
        scope = ReplayScope(GENESIS,h.query['mint'],'holder_exclusion',h.bank,h.bank,
                            label['snapshot_time'],label['snapshot_time'],h.wall)
        adapted = adapt_classification(label['account'],scope=scope,initial_raw=raw,current_raw=raw,
                                       candidate=label,candidate_hash=digest(label))
        self.assertFalse(adapted.verified); self.assertFalse(adapted.ownership_approval)
        self.assertEqual(adapted.to_core().kind,Kind.UNRESOLVED)
        # Even deliberately trusting this point-result hash cannot supply the
        # different interval-record schema; no translation is implemented.
        classified = classify_account(label['account'],TOKEN_PROGRAM,mint=h.query['mint'],
                        purpose='holder_exclusion',now=h.wall,slot=h.bank,
                        evidence_hashes=[digest(label)],trusted_hashes=frozenset({digest(label)}),load=lambda _:label)
        self.assertFalse(classified['classification_resolved']); self.assertFalse(classified['exclusion_allowed'])

    def test_mismatched_fee_bank_annotations_cannot_turn_vault_identity_into_fee_approval(self):
        def mutate(method,result):
            if method == 'getMultipleAccounts':
                result['global_config'] = {'slot':99,'configuration_complete':True,'sell_disabled':False}
                result['dynamic_fee_config'] = {'slot':99,'configuration_complete':True}
                result['fee_amounts_verified'] = True
            return result
        h = Harness(self,mutate); self.assertEqual(h.capture()['status'],'CAPTURED_POINT')
        snapshot = h.store.load(h.receipt().refs.snapshot)
        self.assertNotEqual(snapshot['result']['global_config']['slot'],h.bank)
        for label in h.labels():
            self.assertNotIn('global_config',label); self.assertNotIn('fee_amounts_verified',label)
            fees = check_sell_fee_totals(label,{'passed':True},{'passed':True},{},[],1)
            self.assertFalse(fees['passed']); self.assertFalse(fees['fee_amounts_verified'])
            self.assertFalse(fees['full_route_policy_passed'])
            self.assertEqual(fees['reasons'],['SELL_FEE_EVIDENCE_UNVERIFIED']); self.point_only(label)

    def test_fee_config_accounts_cannot_be_spliced_into_six_account_identity_batch(self):
        def mutate(method,result):
            if method == 'getMultipleAccounts':
                result['value'].append(json.loads((ROOT/'fixtures/mainnet-fee-config.json').read_text())['response']['value'])
            return result
        h = Harness(self,mutate); outcome = h.capture()
        self.assertEqual(outcome['status'],'BLOCKED')
        self.assertEqual(len(h.store.load(h.receipt().refs.snapshot)['result']['value']),7)
        for label in h.labels():
            self.assertIn('ATOMIC_ACCOUNT_SET_INCOMPLETE',label['reasons']); self.point_only(label)
