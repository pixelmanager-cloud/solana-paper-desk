"""Committed synthetic and existing public fixtures; no network/provider calls.

Policies below are constructed separately by the test harness. Re-pinning a
corrupt fixture simulates a trusted acquisition carrying invalid protocol state,
not authority for candidates to mint their own acquisition receipts.
"""
import base64
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from desk.evidence import EvidenceStore
from desk.model import digest
from desk.pool_vault_admission import (AcquisitionReceipt, EvidenceRefs, GENESIS, NETWORK,
                                      TrustedSourcePolicy, admit_pool_vault)
from desk.providers import SOL
from desk.security import TOKEN_PROGRAM, TOKEN_2022

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'


class PoolVaultAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((FIXTURES / 'pool-vault-admission-legacy.json').read_text())
        self.evidence = deepcopy(self.fixture['evidence'])
        self.refs = EvidenceRefs(**self.fixture['refs'])
        self.q = self.fixture['query']
        self.captured_at = self.q['snapshot_time'] + 2
        self.receipt = AcquisitionReceipt('synthetic-test-capture', 'synthetic_fixture', NETWORK,
                                         GENESIS, self.q['pool'], self.q['mint'], self.q['snapshot_slot'],
                                         self.q['snapshot_time'], self.captured_at, self.refs)
        self.policy = TrustedSourcePolicy('independent-test-harness-policy',
                                         frozenset({self.receipt.source_id}), frozenset({self.receipt}),
                                         allow_synthetic_fixtures=True)

    def admit(self, **changes):
        args = dict(account=self.q['base_vault'], pool=self.q['pool'], mint=self.q['mint'],
                    snapshot_slot=self.q['snapshot_slot'], snapshot_time=self.q['snapshot_time'],
                    now=self.captured_at, refs=self.refs, policy=self.policy,
                    load=lambda h: deepcopy(self.evidence[h]))
        args.update(changes)
        return admit_pool_vault(**args)

    def rejected(self, result, reason=None):
        self.assertEqual(result['label'], 'UNKNOWN')
        self.assertFalse(result['snapshot_label_admitted'])
        self.assertFalse(result['production_snapshot_exclusion_allowed'])
        self.assertFalse(result['historical_interval_exclusion_allowed'])
        self.assertFalse(result['chain_authenticated'])
        self.assertFalse(result['eligible_for_trading'])
        self.assertTrue(result['reasons'])
        if reason:
            self.assertIn(reason, result['reasons'])

    def repin(self, snapshot=None, clock=None, request=None, clock_request=None):
        snapshot = deepcopy(snapshot or self.evidence[self.refs.snapshot])
        clock = deepcopy(clock or self.evidence[self.refs.block_time])
        if request is None:
            request = {'kind': 'pool_vault_request_v1', 'network': NETWORK, 'genesis_hash': GENESIS,
                       'method': snapshot['method'], 'params': deepcopy(snapshot['params']),
                       'response_hash': digest(snapshot)}
        if clock_request is None:
            clock_request = {'kind': 'pool_vault_request_v1', 'network': NETWORK, 'genesis_hash': GENESIS,
                             'method': clock['method'], 'params': deepcopy(clock['params']),
                             'response_hash': digest(clock)}
        payloads = [snapshot, request, clock, clock_request]
        self.refs = EvidenceRefs(*map(digest, payloads))
        self.evidence = {digest(p): p for p in payloads}
        self.receipt = replace(self.receipt, refs=self.refs)
        self.policy = replace(self.policy, receipts=frozenset({self.receipt}))

    def mutate_bytes(self, index, offset, value):
        s = deepcopy(self.evidence[self.refs.snapshot])
        account = s['result']['value'][index]
        raw = bytearray(base64.b64decode(account['data'][0]))
        raw[offset:offset+len(value)] = value
        account['data'][0] = base64.b64encode(raw).decode()
        self.repin(snapshot=s)

    def test_committed_positive_fixture_replays_both_exact_vaults(self):
        for account, mint, amount in [(self.q['base_vault'], self.q['mint'], '6000'),
                                      (self.q['quote_vault'], SOL, '7000000')]:
            r = self.admit(account=account)
            self.assertEqual(r['label'], 'POOL_VAULT')
            self.assertTrue(r['snapshot_label_admitted'])
            self.assertTrue(r['acquisition_provenance_trusted'])
            self.assertTrue(r['content_integrity_verified'])
            self.assertTrue(r['request_binding_verified'])
            self.assertTrue(r['protocol_identity_verified'])
            self.assertEqual(r['token_program'], TOKEN_PROGRAM)
            self.assertEqual(r['token_authority'], self.q['pool'])
            self.assertEqual((r['vault_mint'], r['amount_raw']), (mint, amount))
            self.assertEqual(r['reasons'], [])
            self.assertFalse(r['production_snapshot_exclusion_allowed'])
            self.assertFalse(r['historical_interval_exclusion_allowed'])
            self.assertFalse(r['continuity_verified'])
            self.assertFalse(r['eligible_for_trading'])
            self.assertFalse(r['ownership_approval'])
            self.assertFalse(r['chain_authenticated'])

    def test_offline_content_addressed_store_replay_preserves_originals(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = EvidenceStore(Path(tmp) / 'fixture.sqlite')
            original = deepcopy(self.evidence)
            for key, payload in self.evidence.items():
                self.assertEqual(store.save(payload), key)
            r = self.admit(load=store.load)
            self.assertTrue(r['snapshot_label_admitted'])
            self.assertEqual(self.evidence, original)
            for key, payload in self.evidence.items():
                self.assertEqual(store.load(key), payload)

    def test_simulated_coordinator_receipt_does_not_claim_chain_authentication(self):
        # A harness-generated receipt exercises the production policy branch;
        # this synthetic fixture is not asserted to be an actual mainnet capture.
        receipt = replace(self.receipt, source_kind='coordinator_capture')
        r = self.admit(policy=replace(self.policy, receipts=frozenset({receipt}), allow_synthetic_fixtures=False))
        self.assertTrue(r['production_snapshot_exclusion_allowed'])
        self.assertFalse(r['chain_authenticated'])
        self.assertFalse(r['historical_interval_exclusion_allowed'])
        self.assertFalse(r['private_control_proven'])

    def test_missing_policy_and_candidate_json_policy_cannot_admit(self):
        for policy in [None, {}, {'verified': True, 'trusted_hashes': list(self.refs.hashes())},
                       {'policy_id': self.policy.policy_id, 'receipts': [self.receipt]}]:
            self.rejected(self.admit(policy=policy), 'TRUSTED_SOURCE_POLICY_REQUIRED')

    def test_policy_profile_and_resource_limits_cannot_be_overridden(self):
        for policy in [replace(self.policy, profile='any-pool'), replace(self.policy, policy_id=''),
                       replace(self.policy, max_age_seconds=301), replace(self.policy, max_age_seconds=True),
                       replace(self.policy, max_age_seconds=0), replace(self.policy, allow_synthetic_fixtures=1),
                       replace(self.policy, allowed_source_ids=set(self.policy.allowed_source_ids)),
                       replace(self.policy, receipts=tuple(self.policy.receipts)),
                       replace(self.policy, receipts=frozenset(
                           replace(self.receipt, source_id=f'source-{i}') for i in range(257)))]:
            with self.subTest(policy=policy):
                self.rejected(self.admit(policy=policy), 'SOURCE_POLICY_INVALID')

    def test_empty_and_untrusted_duplicate_receipts_never_choose_one(self):
        self.rejected(self.admit(policy=replace(self.policy, receipts=frozenset())),
                      'ACQUISITION_RECEIPT_MISSING_OR_CONFLICTING')
        receipts = frozenset({self.receipt, replace(self.receipt, source_id='other')})
        self.rejected(self.admit(policy=replace(self.policy, receipts=receipts)),
                      'ACQUISITION_SOURCE_UNTRUSTED')

    def test_source_cluster_genesis_and_fixture_opt_in_are_independent(self):
        for receipt in [replace(self.receipt, source_id='untrusted'), replace(self.receipt, network='devnet'),
                        replace(self.receipt, genesis_hash='forged'), replace(self.receipt, source_kind='verified')]:
            self.rejected(self.admit(policy=replace(self.policy, receipts=frozenset({receipt}))),
                          'ACQUISITION_SOURCE_UNTRUSTED')
        self.rejected(self.admit(policy=replace(self.policy, allow_synthetic_fixtures=False)),
                      'SYNTHETIC_SOURCE_NOT_ALLOWED')

    def test_receipt_scope_and_time_bindings_not_candidate_controlled(self):
        for receipt in [replace(self.receipt, pool=self.q['mint']), replace(self.receipt, mint=SOL),
                        replace(self.receipt, slot=99), replace(self.receipt, slot=True),
                        replace(self.receipt, snapshot_time=self.q['snapshot_time']+1)]:
            self.rejected(self.admit(policy=replace(self.policy, receipts=frozenset({receipt}))),
                          'ACQUISITION_SCOPE_MISMATCH')
        for at in [self.q['snapshot_time']-1, self.q['snapshot_time']+301, True]:
            receipt = replace(self.receipt, captured_at=at)
            self.rejected(self.admit(policy=replace(self.policy, receipts=frozenset({receipt}))),
                          'ACQUISITION_TIME_STALE_OR_FUTURE')

    def test_exclusive_expiry_and_future_capture_block(self):
        for now in [self.captured_at-1, self.captured_at+60]:
            self.rejected(self.admit(now=now), 'ACQUISITION_TIME_STALE_OR_FUTURE')
        self.assertTrue(self.admit(now=self.captured_at+59)['snapshot_label_admitted'])

    def test_query_point_cannot_be_reused_at_other_time_or_slot(self):
        for changes in [dict(snapshot_slot=101), dict(snapshot_time=self.q['snapshot_time']+1)]:
            self.rejected(self.admit(**changes), 'ACQUISITION_SCOPE_MISMATCH')
        for changes in [dict(now=True), dict(snapshot_slot=True), dict(snapshot_time='1700000000')]:
            self.rejected(self.admit(**changes), 'QUERY_TIME_OR_SLOT_INVALID')

    def test_content_hash_mismatch_is_not_protocol_verification(self):
        self.evidence[self.refs.snapshot]['result']['value'][4]['owner'] = TOKEN_2022
        r = self.admit()
        self.rejected(r, 'EVIDENCE_CONTENT_HASH_MISMATCH')
        self.assertTrue(r['acquisition_provenance_trusted'])
        self.assertFalse(r['content_integrity_verified'])
        self.assertFalse(r['protocol_identity_verified'])

    def test_candidate_rehashed_forgery_has_no_external_receipt(self):
        s = deepcopy(self.evidence[self.refs.snapshot])
        s['result']['value'][4]['verified'] = True
        s['result']['value'][4]['owner'] = TOKEN_2022
        key = digest(s)
        self.evidence[key] = s
        candidate_refs = replace(self.refs, snapshot=key)
        self.rejected(self.admit(refs=candidate_refs), 'ACQUISITION_RECEIPT_MISSING_OR_CONFLICTING')

    def test_missing_unreadable_or_oversized_evidence_fails_closed(self):
        del self.evidence[self.refs.block_time_request]
        self.rejected(self.admit(), 'EVIDENCE_UNREADABLE')
        self.setUp()
        def fail(_):
            raise RuntimeError('private implementation error not exposed')
        r = self.admit(load=fail)
        self.rejected(r, 'EVIDENCE_UNREADABLE')
        self.assertNotIn('private implementation error', str(r))
        s = deepcopy(self.evidence[self.refs.snapshot]); s['padding'] = 'X'*65536
        self.repin(snapshot=s)
        self.rejected(self.admit(), 'EVIDENCE_SHAPE_OR_SIZE_UNSUPPORTED')

    def test_normalized_verified_flags_never_substitute_for_raw_state(self):
        s = deepcopy(self.evidence[self.refs.snapshot])
        s.update(verified=True, identity_verified=True, chain_authenticated=True,
                 classification='POOL_VAULT', evidence_hash='f'*64)
        s['result']['value'][4]['owner'] = TOKEN_2022
        self.repin(snapshot=s)
        self.rejected(self.admit(), 'ACCOUNT_PROGRAM_OR_METADATA_UNSUPPORTED')

    def test_wrong_pool_pda_even_independently_attested_is_rejected(self):
        self.q['pool'] = self.q['mint']
        self.receipt = replace(self.receipt, pool=self.q['pool'])
        self.policy = replace(self.policy, receipts=frozenset({self.receipt}))
        self.rejected(self.admit(), 'CANONICAL_POOL_PDA_MISMATCH')

    def test_nonvault_address_cannot_get_pool_private_or_service_label(self):
        for account in [self.q['pool'], self.q['mint'], SOL]:
            r = self.admit(account=account)
            self.rejected(r, 'VAULT_ACCOUNT_ATA_MISMATCH')
            self.assertFalse(r['private_control_proven'])

    def test_pool_raw_identity_fields_reconstructed_not_asserted(self):
        for offset, value in [(8, bytes([0])), (9, bytes([1, 0])), (11, bytes(32)),
                              (43, bytes(32)), (75, bytes(32)), (107, bytes(32)),
                              (139, bytes(32)), (171, bytes(32))]:
            with self.subTest(offset=offset):
                self.setUp()
                self.mutate_bytes(0, offset, value)
                self.rejected(self.admit(), 'POOL_RAW_IDENTITY_MISMATCH')

    def test_unrecognized_pool_layout_or_feature_flags_rejected(self):
        for offset in [243, 244, 269, 270]:
            self.setUp(); self.mutate_bytes(0, offset, bytes([1]))
            self.rejected(self.admit(), 'POOL_PROFILE_UNSUPPORTED')
        self.setUp(); self.mutate_bytes(0, 300, bytes([1]))
        self.rejected(self.admit(), 'POOL_RAW_IDENTITY_MISMATCH')
        self.setUp(); self.mutate_bytes(0, 0, bytes([0]))
        self.rejected(self.admit(), 'RAW_EVIDENCE_OR_QUERY_MALFORMED')
        self.setUp()
        s = deepcopy(self.evidence[self.refs.snapshot]); a = s['result']['value'][0]
        a['data'][0] = base64.b64encode(base64.b64decode(a['data'][0])+bytes(1)).decode()
        a['space'] = 302
        self.repin(snapshot=s)
        self.rejected(self.admit(), 'ACCOUNT_LAYOUT_UNSUPPORTED')

    def test_vault_mint_authority_delegate_close_state_native_controls(self):
        cases = [(0, bytes(32), 'VAULT_MINT_OR_AUTHORITY_MISMATCH'),
                 (32, bytes(32), 'VAULT_MINT_OR_AUTHORITY_MISMATCH'),
                 (72, (1).to_bytes(4, 'little'), 'VAULT_DELEGATE_UNSUPPORTED'),
                 (72, (2).to_bytes(4, 'little'), 'VAULT_DELEGATE_UNSUPPORTED'),
                 (121, (1).to_bytes(8, 'little'), 'VAULT_DELEGATE_UNSUPPORTED'),
                 (108, bytes([2]), 'VAULT_FROZEN_OR_UNINITIALIZED'),
                 (108, bytes([0]), 'VAULT_FROZEN_OR_UNINITIALIZED'),
                 (109, (1).to_bytes(4, 'little'), 'BASE_VAULT_NATIVE_UNSUPPORTED'),
                 (129, (2).to_bytes(4, 'little'), 'VAULT_CLOSE_AUTHORITY_UNSUPPORTED'),
                 (129, (1).to_bytes(4, 'little'), 'VAULT_CLOSE_AUTHORITY_UNSUPPORTED')]
        for offset, value, reason in cases:
            with self.subTest(offset=offset, value=value):
                self.setUp(); self.mutate_bytes(4, offset, value)
                self.rejected(self.admit(), reason)

    def test_pool_owned_close_authority_supported_exactly(self):
        from solders.pubkey import Pubkey
        s = deepcopy(self.evidence[self.refs.snapshot]); a = s['result']['value'][4]
        raw = bytearray(base64.b64decode(a['data'][0])); raw[129:133] = (1).to_bytes(4, 'little')
        raw[133:165] = bytes(Pubkey.from_string(self.q['pool']))
        a['data'][0] = base64.b64encode(raw).decode(); self.repin(snapshot=s)
        self.assertTrue(self.admit()['snapshot_label_admitted'])

    def test_native_quote_controls_and_reserve_lamports(self):
        for offset, value in [(109, bytes(4)), (109, (2).to_bytes(4, 'little')), (113, bytes(8))]:
            self.setUp(); self.mutate_bytes(5, offset, value)
            self.rejected(self.admit(), 'NATIVE_VAULT_RESERVE_UNSUPPORTED')
        self.setUp()
        s = deepcopy(self.evidence[self.refs.snapshot]); s['result']['value'][5]['lamports'] += 1
        self.repin(snapshot=s)
        self.rejected(self.admit(), 'NATIVE_VAULT_RESERVE_UNSUPPORTED')

    def test_all_token_programs_and_executable_controls_reconstructed(self):
        for index in range(6):
            for changed in [{'owner': TOKEN_2022}, {'executable': True}, {'executable': 0},
                            {'lamports': True}, {'lamports': 2**64}, {'space': 0}]:
                with self.subTest(index=index, changed=changed):
                    self.setUp()
                    s = deepcopy(self.evidence[self.refs.snapshot]); s['result']['value'][index].update(changed)
                    self.repin(snapshot=s)
                    self.rejected(self.admit())

    def test_mint_and_lp_authority_and_initialization_checks(self):
        for index, offset, value in [(1, 0, (1).to_bytes(4, 'little')),
                                     (1, 0, (2).to_bytes(4, 'little')),
                                     (1, 46, (1).to_bytes(4, 'little')),
                                     (1, 46, (2).to_bytes(4, 'little')),
                                     (1, 45, bytes([0])), (1, 36, bytes(8)),
                                     (1, 44, bytes([10])), (2, 44, bytes([6])),
                                     (3, 4, bytes(32)), (3, 0, bytes(4)),
                                     (3, 46, (1).to_bytes(4, 'little'))]:
            with self.subTest(index=index, offset=offset):
                self.setUp(); self.mutate_bytes(index, offset, value)
                self.rejected(self.admit())

    def test_base_vault_cannot_exceed_same_bank_supply(self):
        self.mutate_bytes(4, 64, (10001).to_bytes(8, 'little'))
        self.rejected(self.admit(), 'VAULT_AMOUNT_EXCEEDS_BASE_SUPPLY')

    def test_request_method_commitment_key_order_and_minimum_slot(self):
        for mutate in [lambda s: s.update(method='getAccountInfo'),
                       lambda s: s['params'][1].update(commitment='confirmed'),
                       lambda s: s['params'][1].update(minContextSlot=99),
                       lambda s: s['params'][1].update(encoding='jsonParsed'),
                       lambda s: s['params'][0].reverse(),
                       lambda s: s['params'][0].__setitem__(1, SOL),
                       lambda s: s.update(error={'code': -1})]:
            self.setUp()
            s = deepcopy(self.evidence[self.refs.snapshot]); mutate(s); self.repin(snapshot=s)
            self.rejected(self.admit(), 'RPC_REQUEST_OR_RESPONSE_MISMATCH')

    def test_rehashed_manifest_cannot_rebind_network_request_or_response(self):
        for changes in [dict(network='devnet'), dict(genesis_hash='forged'), dict(response_hash='0'*64),
                        dict(method='getAccountInfo'), dict(verified=True), dict(params=[])]:
            self.setUp()
            request = deepcopy(self.evidence[self.refs.snapshot_request]); request.update(changes)
            self.repin(request=request)
            self.rejected(self.admit(), 'RPC_MANIFEST_BINDING_MISMATCH')

    def test_atomic_slot_partial_values_and_missing_accounts(self):
        for mutate in [lambda s: s['result']['context'].update(slot=101),
                       lambda s: s['result']['context'].update(slot=True),
                       lambda s: s['result']['value'].pop(),
                       lambda s: s['result']['value'].__setitem__(4, None),
                       lambda s: s['result']['value'].__setitem__(4, s['result']['value'][5])]:
            self.setUp()
            s = deepcopy(self.evidence[self.refs.snapshot]); mutate(s); self.repin(snapshot=s)
            self.rejected(self.admit())

    def test_block_time_exact_slot_and_result_cannot_be_replaced(self):
        for mutate in [lambda c: c.update(result=self.q['snapshot_time']+1),
                       lambda c: c.update(result=None), lambda c: c.update(result=True),
                       lambda c: c.update(params=[101]), lambda c: c.update(method='getSlot')]:
            self.setUp()
            clock = deepcopy(self.evidence[self.refs.block_time]); mutate(clock); self.repin(clock=clock)
            self.rejected(self.admit())

    def test_boolean_rpc_slot_cannot_equal_integer_slot(self):
        self.q['snapshot_slot'] = 1
        self.receipt = replace(self.receipt, slot=1)
        s = deepcopy(self.evidence[self.refs.snapshot]); s['params'][1]['minContextSlot'] = True
        s['result']['context']['slot'] = 1
        c = deepcopy(self.evidence[self.refs.block_time]); c['params'] = [True]
        self.repin(snapshot=s, clock=c)
        self.rejected(self.admit(), 'RPC_SLOT_TYPE_INVALID')

    def test_existing_public_snapshots_cannot_be_upgraded_by_asserted_labels(self):
        for name in ['mainnet-pool-snapshot.json', 'mainnet-pool-fee-snapshot.json']:
            self.setUp()
            fixture = json.loads((FIXTURES / name).read_text())
            # Trusting a capture in the test harness does not repair its protocol
            # profile, missing atomic mint accounts or confirmed request binding.
            self.q.update(pool=fixture['pool'], mint=fixture['mint'],
                          snapshot_slot=fixture['capture']['result']['context']['slot'],
                          base_vault=fixture['result']['vaults'][1]['address'])
            self.receipt = replace(self.receipt, pool=self.q['pool'], mint=self.q['mint'],
                                   slot=self.q['snapshot_slot'])
            raw_capture = fixture['capture']
            envelope = {'kind': 'rpc_response_v1', 'method': raw_capture['method'],
                        'params': raw_capture['params'], 'result': raw_capture['result']}
            self.repin(snapshot=envelope)
            self.rejected(self.admit(), 'RPC_REQUEST_OR_RESPONSE_MISMATCH')
            self.assertTrue(fixture['result']['identity_verified'])

    def test_malformed_reference_address_and_account_encoding(self):
        self.rejected(self.admit(refs=replace(self.refs, snapshot='not-a-hash')), 'EVIDENCE_REFERENCES_INVALID')
        self.rejected(self.admit(account='bad-address'))
        s = deepcopy(self.evidence[self.refs.snapshot]); s['result']['value'][4]['data'] = ['!', 'base64']
        self.repin(snapshot=s)
        self.rejected(self.admit(), 'RAW_EVIDENCE_OR_QUERY_MALFORMED')

    def test_request_flag_claiming_continuity_never_authorizes_interval(self):
        s = deepcopy(self.evidence[self.refs.snapshot]); s.update(continuity_verified=True,
                       observed_slot=0, expires_slot=1000000, eligible_for_trading=True)
        self.repin(snapshot=s)
        r = self.admit()
        self.assertTrue(r['snapshot_label_admitted'])
        self.assertFalse(r['historical_interval_exclusion_allowed'])
        self.assertFalse(r['continuity_verified'])
        self.assertFalse(r['eligible_for_trading'])
        self.assertNotIn('expires_slot', r)

    def test_slot_and_clock_integer_layout_limits(self):
        for changes in [dict(snapshot_slot=2**64), dict(snapshot_time=2**63), dict(now=2**63)]:
            self.rejected(self.admit(**changes), 'QUERY_TIME_OR_SLOT_INVALID')

    def paired_captures(self, *, mutate=None, other_source=None, other_time=None):
        """Harness-only coordinator endorsements; no live acquisition claim."""
        first_refs = self.refs
        first_evidence = deepcopy(self.evidence)
        first = replace(self.receipt, source_kind='coordinator_capture')
        self.receipt = first
        snapshot = deepcopy(self.evidence[self.refs.snapshot])
        clock = deepcopy(self.evidence[self.refs.block_time])
        if mutate:
            mutate(snapshot)
        if other_time is not None:
            clock['result'] = other_time
            self.receipt = replace(self.receipt, snapshot_time=other_time)
        self.repin(snapshot=snapshot, clock=clock)
        second_refs = self.refs
        second = replace(self.receipt, source_id=other_source or first.source_id)
        self.evidence.update(first_evidence)
        self.policy = replace(self.policy, allowed_source_ids=frozenset({first.source_id, second.source_id}),
                              receipts=frozenset({first, second}), allow_synthetic_fixtures=False)
        return first_refs, second_refs, first, second

    @staticmethod
    def change_base_amount(snapshot):
        account = snapshot['result']['value'][4]
        raw = bytearray(base64.b64decode(account['data'][0]))
        raw[64:72] = (6001).to_bytes(8, 'little')
        account['data'][0] = base64.b64encode(raw).decode()

    def test_contradictory_same_point_captures_reject_both_ref_directions_and_vaults(self):
        first_refs, second_refs, _, _ = self.paired_captures(mutate=self.change_base_amount)
        self.assertNotEqual(first_refs, second_refs)
        for refs in (first_refs, second_refs):
            for account in (self.q['base_vault'], self.q['quote_vault']):
                with self.subTest(refs=refs, account=account):
                    self.rejected(self.admit(refs=refs, account=account), 'ACQUISITION_CAPTURE_CONFLICT')

    def test_different_allowed_sources_cannot_select_convenient_capture(self):
        first_refs, second_refs, _, _ = self.paired_captures(
            mutate=self.change_base_amount, other_source='independent-source-two')
        for refs in (first_refs, second_refs):
            self.rejected(self.admit(refs=refs), 'ACQUISITION_CAPTURE_CONFLICT')

    def test_contradictory_block_times_are_same_slot_conflict_not_distinct_scope(self):
        first_time = self.q['snapshot_time']
        first_refs, second_refs, _, _ = self.paired_captures(other_time=first_time+1)
        for refs, at in [(first_refs, first_time), (second_refs, first_time+1)]:
            self.rejected(self.admit(refs=refs, snapshot_time=at), 'ACQUISITION_CAPTURE_CONFLICT')

    def test_stale_competing_capture_does_not_erase_immutable_slot_contradiction(self):
        first_refs, _, first, second = self.paired_captures(mutate=self.change_base_amount)
        second = replace(second, captured_at=self.q['snapshot_time'])
        policy = replace(self.policy, receipts=frozenset({first, second}), max_age_seconds=1)
        self.rejected(self.admit(refs=first_refs, policy=policy), 'ACQUISITION_CAPTURE_CONFLICT')

    def test_future_competing_capture_is_ambiguous_even_with_equivalent_state(self):
        first_refs, second_refs, first, second = self.paired_captures(
            mutate=lambda s: s.update(transport_annotation='duplicate transport'))
        second = replace(second, captured_at=self.captured_at+1)
        policy = replace(self.policy, receipts=frozenset({first, second}))
        self.rejected(self.admit(refs=first_refs, policy=policy), 'ACQUISITION_CAPTURE_AMBIGUOUS')
        self.rejected(self.admit(refs=second_refs, policy=policy), 'ACQUISITION_TIME_STALE_OR_FUTURE')

    def test_duplicate_equivalent_endorsements_are_admitted_with_cached_reads(self):
        refs, _, first, second = self.paired_captures(other_source='independent-source-two')
        second = replace(second, captured_at=second.captured_at-1)
        policy = replace(self.policy, receipts=frozenset({first, second, first}))
        loaded = []
        def load(key):
            loaded.append(key)
            return deepcopy(self.evidence[key])
        result = self.admit(refs=refs, policy=policy, load=load)
        self.assertTrue(result['production_snapshot_exclusion_allowed'])
        self.assertEqual(result['supporting_capture_count'], 1)
        self.assertEqual(result['source_ids'], sorted(policy.allowed_source_ids))
        self.assertEqual(result['expires_at'], second.captured_at+policy.max_age_seconds)
        self.assertEqual(len(loaded), 4)
        self.assertEqual(len(set(loaded)), 4)
        self.assertFalse(result['historical_interval_exclusion_allowed'])
        self.assertFalse(result['eligible_for_trading'])

    def test_metadata_distinct_refs_with_identical_raw_state_are_equivalent(self):
        def metadata_only(snapshot):
            snapshot['result']['context']['apiVersion'] = 'another-provider-version'
            snapshot['transport_annotation'] = 'not bank state'
            for value in snapshot['result']['value']:
                value.pop('space')
        first_refs, second_refs, _, _ = self.paired_captures(
            mutate=metadata_only, other_source='independent-source-two')
        self.assertNotEqual(first_refs, second_refs)
        for refs in (first_refs, second_refs):
            result = self.admit(refs=refs)
            self.assertTrue(result['production_snapshot_exclusion_allowed'])
            self.assertEqual(result['amount_raw'], '6000')
            self.assertEqual(result['supporting_capture_count'], 2)
            self.assertEqual(len(result['conflict_check_evidence_hashes']), 6)
            self.assertFalse(result['chain_authenticated'])

    def test_unavailable_or_malformed_trusted_competing_capture_is_ambiguous(self):
        first_refs, second_refs, _, _ = self.paired_captures(mutate=self.change_base_amount)
        del self.evidence[second_refs.snapshot]
        self.rejected(self.admit(refs=first_refs), 'ACQUISITION_CAPTURE_AMBIGUOUS')
        self.setUp()
        def wrong_request(snapshot):
            snapshot['params'][1]['commitment'] = 'confirmed'
        first_refs, _, _, _ = self.paired_captures(mutate=wrong_request)
        self.rejected(self.admit(refs=first_refs), 'ACQUISITION_CAPTURE_AMBIGUOUS')

    def test_same_point_raw_control_or_lamport_conflict_is_quarantined(self):
        for change in [lambda s: s['result']['value'][0].update(lamports=2039281),
                       lambda s: s['result']['value'][4].update(owner=TOKEN_2022)]:
            self.setUp()
            first_refs, _, _, _ = self.paired_captures(mutate=change)
            self.rejected(self.admit(refs=first_refs))

    def test_other_slot_chain_or_pool_receipts_are_not_this_snapshot_scope(self):
        for change in [dict(slot=101), dict(network='devnet'), dict(genesis_hash='other-chain'),
                       dict(pool=SOL), dict(mint=SOL)]:
            self.setUp()
            first_refs, _, first, second = self.paired_captures(mutate=self.change_base_amount)
            other = replace(second, **change)
            policy = replace(self.policy, receipts=frozenset({first, other}))
            result = self.admit(refs=first_refs, policy=policy)
            self.assertTrue(result['production_snapshot_exclusion_allowed'])
            self.assertEqual(result['supporting_capture_count'], 1)
