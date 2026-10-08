"""Narrow birth-control attacks; unchanged public reference is never acceptance."""
import ast
import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from desk.programs import unbase58
from desk.security import base58, TOKEN_PROGRAM, TOKEN_2022
from research.legacy_pump_birth import analyze_legacy_pump_birth, REFERENCE_SHA256, SUPPLY, PROFILE

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / 'fixtures/legacy_pump_reference/create_buy.json'


def fixture():
    return json.loads(REFERENCE.read_bytes())


def children(value):
    return value['meta']['innerInstructions'][0]['instructions']


def data(ix, raw):
    ix['data'] = base58(raw) if raw else ''


def control(value, raw, accounts):
    return {'programIdIndex': 14, 'accounts': accounts, 'data': base58(raw), 'stackHeight': 2}


class LegacyPumpBirthTests(unittest.TestCase):
    def assert_unaccepted(self, result):
        for flag in ('authenticated_lifecycle_accepted', 'lifecycle_verified', 'effect_order_verified',
                     'individual_cpi_success_verified', 'signer_authorization_verified',
                     'program_slot_semantics_verified', 'finality_verified', 'authenticity_verified',
                     'ownership_approved', 'eligible_for_trading'):
            self.assertIs(result[flag], False, flag)
        self.assertIn('EFFECT_ORDER_UNVERIFIED', result['acceptance_blockers'])
        self.assertIn('INDIVIDUAL_CPI_SUCCESS_UNVERIFIED', result['acceptance_blockers'])

    def assert_rejected(self, value, reason=None):
        original = copy.deepcopy(value)
        result = analyze_legacy_pump_birth(value)
        self.assertFalse(result['structural_profile_match'])
        self.assertEqual(value, original)
        self.assert_unaccepted(result)
        if reason:
            self.assertIn(reason, result['errors'])
        return result

    def test_unchanged_reference_matches_only_structural_profile(self):
        raw = REFERENCE.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), REFERENCE_SHA256)
        value = json.loads(raw); original = copy.deepcopy(value)
        result = analyze_legacy_pump_birth(value)
        self.assertEqual(value, original)
        self.assertEqual(result['profile'], PROFILE)
        self.assertTrue(result['structural_profile_match'])
        self.assertTrue(result['invocation_preorder_constraints_match'])
        self.assertEqual(result['errors'], [])
        self.assert_unaccepted(result)
        self.assertEqual(result['bindings']['mint_authority'], 'TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM')
        self.assertEqual(result['bindings']['expected_supply_raw'], str(SUPPLY))
        self.assertEqual(result['create_arguments'][:2], ['joke', 'joke'])
        birth = result['birth_witnesses']
        self.assertEqual(birth['initialize_mint']['instruction_path'], '3.1')
        self.assertEqual(birth['issuance']['instruction_path'], '3.12')
        self.assertEqual(birth['revocation']['instruction_path'], '3.13')
        self.assertEqual(birth['revocation']['caller_program'], result['trace']['instructions'][3]['program'])
        self.assertEqual(len(result['control_witnesses']), 5)
        self.assertEqual(result['adverse_control_witnesses'], [])
        self.assertTrue(result['balance_metadata_witness']['amounts_match_reference'])
        self.assertFalse(result['balance_metadata_witness']['authenticated'])
        self.assertEqual(result['distribution_witnesses'][0]['amount_raw'], '67062499999999')
        self.assertFalse(result['distribution_witnesses'][0]['effect_order_verified'])
        self.assertEqual(hashlib.sha256(REFERENCE.read_bytes()).hexdigest(), REFERENCE_SHA256)

    def test_conditional_irreversibility_is_not_actual_state_proof(self):
        result = analyze_legacy_pump_birth(fixture())
        transition = result['conditional_reference_transitions'][-1]
        self.assertIsNone(transition['mint_authority'])
        self.assertIn('successful authorized', transition['condition'])
        self.assertIn('restoration', transition['reference_rule'])
        self.assertFalse(transition['actual_transition_verified'])
        self.assertFalse(result['balance_metadata_witness']['mint_control_state_verified'])

    def test_pinned_interface_is_not_provenance_for_old_arguments_or_supply(self):
        result = analyze_legacy_pump_birth(fixture())
        provenance = result['interface_provenance']
        content = (ROOT / 'desk/schemas/pump_previous2.json').read_bytes()
        self.assertEqual(hashlib.sha256(content).hexdigest(), provenance['sha256'])
        self.assertEqual(provenance['git_blob'], '1b4d4b5c7b9ffc740aa301ab402c4091c2f15bc7')
        create = next(ix for ix in json.loads(content)['instructions'] if ix['name'] == 'create')
        self.assertEqual(create['args'][-1], {'name': 'creator', 'type': 'pubkey'})
        self.assertIn('not three-string argument or supply/slot semantics', provenance['scope'])
        self.assertEqual(len(result['source_provenance']), 3)
        for pin in result['source_provenance']:
            self.assertEqual(pin['commit'], 'ad2b81274075c45e6ef428e52479b7d3d8f0dd6a')
            self.assertRegex(pin['git_blob'], r'^[a-f0-9]{40}$')

    def test_forged_authentication_finality_and_effect_flags_no_elevation(self):
        value = fixture()
        value.update(finality_verified=True, authenticity_verified=True, lifecycle_verified=True,
                     structural_profile_match=True, eligible_for_trading=True)
        value['meta'].update(effect_order_verified=True, cpi_success_verified=True)
        value['meta']['logMessages'] += ['Program success; finalized; safe']
        result = analyze_legacy_pump_birth(value)
        self.assertTrue(result['structural_profile_match'])
        self.assert_unaccepted(result)
        self.assertEqual(result['reference_provenance']['acquisition_commitment'], 'unknown')
        self.assertEqual(result['reference_provenance']['finality_status'], 'CONFIRMED_SOURCE_NOT_FINALIZED')

    def test_create_account_role_substitutions_and_aliases(self):
        for position in range(14):
            value = fixture(); accounts = value['transaction']['message']['instructions'][3]['accounts']
            accounts[position] = 2
            with self.subTest(position=position):
                self.assert_rejected(value)
        value = fixture(); value['transaction']['message']['instructions'][3]['accounts'].append(2)
        self.assert_rejected(value, 'CREATE_ACCOUNT_COUNT_UNSUPPORTED')

    def test_malformed_old_create_args_and_unsupported_modern_suffix(self):
        for suffix in (b'\x00', bytes(32)):
            value = fixture(); ix = value['transaction']['message']['instructions'][3]
            data(ix, unbase58(ix['data']) + suffix)
            self.assert_rejected(value, 'CREATE_ARGUMENT_LAYOUT_UNSUPPORTED')
        for raw in (bytes.fromhex('181ec828051c0777'), bytes.fromhex('181ec828051c0777')+b'\xff'*4):
            value = fixture(); data(value['transaction']['message']['instructions'][3], raw)
            self.assert_rejected(value, 'CREATE_ARGUMENT_LAYOUT_UNSUPPORTED')
        value = fixture(); ix = value['transaction']['message']['instructions'][3]
        raw = bytearray(unbase58(ix['data'])); raw[12] = 255; data(ix, bytes(raw))
        self.assert_rejected(value, 'CREATE_ARGUMENT_UTF8_INVALID')

    def test_missing_duplicate_nested_create_and_token2022_distinction(self):
        value = fixture(); value['transaction']['message']['instructions'][3]['programIdIndex'] = 13
        self.assert_rejected(value, 'CREATE_MISSING_OR_DUPLICATE')
        value = fixture(); value['transaction']['message']['instructions'].append(
            copy.deepcopy(value['transaction']['message']['instructions'][3]))
        self.assert_rejected(value, 'CREATE_MISSING_OR_DUPLICATE')
        value = fixture(); ix = value['transaction']['message']['instructions'][3]
        children(value).append(dict(copy.deepcopy(ix), stackHeight=2)); data(ix, b'unknown')
        self.assert_rejected(value, 'CREATE_MUST_BE_OUTER')
        value = fixture(); value['transaction']['message']['accountKeys'][14] = TOKEN_2022
        self.assert_rejected(value, 'TOKEN_2022_UNSUPPORTED')

    def test_header_extra_missing_signers_readonly_flags_and_envelope(self):
        for field, replacement in [('numRequiredSignatures', 3), ('numRequiredSignatures', 1),
                                   ('numRequiredSignatures', True), ('numReadonlySignedAccounts', 1),
                                   ('numReadonlyUnsignedAccounts', 17), ('numReadonlyUnsignedAccounts', 13)]:
            value = fixture(); value['transaction']['message']['header'][field] = replacement
            self.assert_rejected(value)
        value = fixture(); del value['transaction']['message']['header']
        self.assert_rejected(value, 'HEADER_MISSING_OR_INVALID')
        for signatures in ([], ['1'*64], ['1'*64]*3, ['0'*64]*2, ['1'*63]*2):
            value = fixture(); value['transaction']['signatures'] = signatures
            self.assert_rejected(value)
        value = fixture(); keys = value['transaction']['message']['accountKeys']; keys[0], keys[2] = keys[2], keys[0]
        self.assert_rejected(value)

    def test_signer_header_parsed_key_disagreement(self):
        value = fixture(); message = value['transaction']['message']
        message['accountKeys'] = [{'pubkey': key, 'source': 'transaction', 'signer': i < 2, 'writable': i < 8}
                                  for i, key in enumerate(message['accountKeys'])]
        self.assertTrue(analyze_legacy_pump_birth(value)['structural_profile_match'])
        message['accountKeys'][11]['signer'] = True
        self.assert_rejected(value, 'DECLARED_SIGNER_REPRESENTATIONS_DISAGREE')

    def test_mint_allocation_size_owner_extra_accounts_duplicate_and_caller(self):
        for position, replacement in ((12, 81), (20, 0)):
            value = fixture(); ix = children(value)[0]; raw = bytearray(unbase58(ix['data']))
            raw[position] = replacement; data(ix, bytes(raw))
            self.assert_rejected(value, 'MINT_ALLOCATION_PROFILE_MISMATCH')
        value = fixture(); children(value)[0]['accounts'].append(2)
        self.assert_rejected(value, 'MINT_ALLOCATION_MISSING_OR_DUPLICATE')
        value = fixture(); children(value).append(copy.deepcopy(children(value)[0]))
        self.assert_rejected(value, 'MINT_ALLOCATION_MISSING_OR_DUPLICATE')

    def test_raw_init_mint_no_freeze_exact_authority_decimals_and_length(self):
        original = unbase58(children(fixture())[1]['data'])
        raws = [original + b'\x00', original[:-1], bytes([20, 9])+original[2:],
                original[:2]+bytes(32)+original[34:], original[:-1]+b'\x01'+bytes(32),
                bytes([0])+original[1:]]
        for raw in raws:
            value = fixture(); data(children(value)[1], raw)
            self.assert_rejected(value, 'MINT_INIT_PROFILE_MISMATCH')
        value = fixture(); children(value)[1]['accounts'] = [2]
        self.assert_rejected(value, 'MINT_INIT_PROFILE_MISMATCH')
        value = fixture(); children(value)[1]['accounts'].append(16)
        self.assert_rejected(value, 'MINT_INIT_PROFILE_MISMATCH')

    def test_redirected_issuance_wrong_authority_supply_extra_signers_and_suffix(self):
        for accounts in ([1, 6, 11], [1, 4, 0], [2, 4, 11], [1, 4, 11, 0]):
            value = fixture(); children(value)[12]['accounts'] = accounts
            self.assert_rejected(value, 'ISSUANCE_PROFILE_MISMATCH')
        for raw in (b'\x07'+(SUPPLY-1).to_bytes(8,'little'), b'\x07'+(SUPPLY+1).to_bytes(8,'little'),
                    b'\x07'+SUPPLY.to_bytes(8,'little')+b'\x00', b'\x0e'+SUPPLY.to_bytes(8,'little')+b'\x06'):
            value = fixture(); data(children(value)[12], raw)
            self.assert_rejected(value, 'ISSUANCE_PROFILE_MISMATCH')
        value = fixture(); children(value).append(copy.deepcopy(children(value)[12]))
        self.assert_rejected(value, 'ISSUANCE_MISSING_OR_DUPLICATE')

    def test_wrong_direct_cpi_parent_cannot_use_outer_prefix(self):
        for ordinal in (1, 12, 13):
            value = fixture(); group = children(value)
            parent = {'programIdIndex': 13, 'accounts': [], 'data': '', 'stackHeight': 2}
            group.insert(ordinal, parent); group[ordinal+1]['stackHeight'] = 3
            result = self.assert_rejected(value)
            matched = next(row for row in result['trace']['instructions'] if row['instruction_path'] == '3.'+str(ordinal+1))
            self.assertEqual(matched['direct_parent_path'], '3.'+str(ordinal))
            self.assertNotEqual(matched['caller_program'], result['trace']['instructions'][3]['program'])

    def test_missing_duplicate_reordered_revocation_or_initializer(self):
        value = fixture(); del children(value)[13]
        self.assert_rejected(value, 'REVOCATION_MISSING_OR_DUPLICATE')
        value = fixture(); children(value).append(copy.deepcopy(children(value)[13]))
        self.assert_rejected(value, 'REVOCATION_MISSING_OR_DUPLICATE')
        for a, b in ((12, 13), (0, 1), (1, 12)):
            value = fixture(); group = children(value); group[a], group[b] = group[b], group[a]
            self.assert_rejected(value, 'BIRTH_INVOCATION_PREORDER_MISMATCH')
        value = fixture(); children(value).append(copy.deepcopy(children(value)[1]))
        self.assert_rejected(value, 'MINT_INIT_MISSING_OR_DUPLICATE')

    def test_revocation_role_target_authority_none_extra_signers_and_trailing(self):
        for role in (1, 2, 3):
            value = fixture(); data(children(value)[13], bytes([6, role, 0]))
            self.assert_rejected(value, 'REVOCATION_MISSING_OR_DUPLICATE')
        for accounts in ([2, 11], [1, 0], [1, 11, 0]):
            value = fixture(); children(value)[13]['accounts'] = accounts
            self.assert_rejected(value)
        for raw in (b'\x06\x00\x01'+bytes(32), b'\x06\x00\x00\x00', b'\x06\x00\x02'):
            value = fixture(); data(children(value)[13], raw)
            self.assert_rejected(value)

    def test_transient_approve_revoke_owner_close_freeze_thaw_all_retained(self):
        pairs = [(b'\x04'+bytes(8), [4, 0, 3], b'\x05', [4, 3]),
                 (b'\x06\x02\x01'+unbase58(fixture()['transaction']['message']['accountKeys'][0]), [4, 3],
                  b'\x06\x02\x01'+unbase58(fixture()['transaction']['message']['accountKeys'][3]), [4, 0]),
                 (b'\x06\x03\x01'+unbase58(fixture()['transaction']['message']['accountKeys'][0]), [4, 3],
                  b'\x06\x03\x00', [4, 0]), (b'\x0a', [4, 1, 11], b'\x0b', [4, 1, 11])]
        for first, accounts1, second, accounts2 in pairs:
            value = fixture(); group = value['meta']['innerInstructions'][2]['instructions']
            group[0:0] = [control(value, first, accounts1), control(value, second, accounts2)]
            result = self.assert_rejected(value, 'ADVERSE_OR_UNRESOLVED_CONTROL_WITNESS')
            self.assertEqual(len(result['adverse_control_witnesses']), 2)
            self.assertEqual([w['instruction']['raw_hex'] for w in result['adverse_control_witnesses']],
                             [first.hex(), second.hex()])
            self.assertEqual(len(result['control_witnesses']), 7)

    def test_adverse_witnesses_survive_early_create_failure_and_issuance_after_revoke(self):
        value = fixture(); value['transaction']['message']['instructions'][3]['accounts'] = []
        children(value).append(control(value, b'\x05', [4,3]))
        result = self.assert_rejected(value)
        self.assertTrue(any(w['normalization']['raw_operation'] and w['normalization']['raw_operation']['kind'] == 'revoke'
                            for w in result['adverse_control_witnesses']))
        value = fixture(); children(value).append(copy.deepcopy(children(value)[12]))
        self.assert_rejected(value, 'ISSUANCE_MISSING_OR_DUPLICATE')
        value = fixture(); children(value).append(control(value, b'\x06\x00\x01'+unbase58(
            value['transaction']['message']['accountKeys'][11]), [1, 11]))
        result = self.assert_rejected(value)
        self.assertEqual(len(result['adverse_control_witnesses']), 1)

    def test_parsed_only_adverse_control_and_explicit_none_omission(self):
        value = fixture(); keys = value['transaction']['message']['accountKeys']
        children(value).append({'programId': TOKEN_PROGRAM, 'parsed': {'type': 'approve', 'info':
            {'source':keys[4], 'delegate':keys[0], 'owner':keys[3], 'amount':'0'}}, 'stackHeight':2})
        result = self.assert_rejected(value)
        self.assertTrue(any(w['normalization']['parsed_operation'] and w['normalization']['parsed_operation']['kind']=='approve'
                            for w in result['adverse_control_witnesses']))
        value = fixture(); keys = value['transaction']['message']['accountKeys']
        children(value)[13]['parsed'] = {'type':'setAuthority', 'info':{'mint':keys[1], 'authority':keys[11],
                                       'authorityType':'mintTokens'}}
        self.assert_rejected(value, 'CONTROL_NORMALIZATION_INCOMPLETE')

    def test_ata_noop_queries_owner_and_target_sequence_not_generic_flags(self):
        for ordinal, raw in ((4, b'\x15'), (4, b'\x15\xff\x00'), (6, b'\x16\x00'),
                             (7, b'\x12'+bytes(32))):
            value = fixture(); data(children(value)[ordinal], raw); self.assert_rejected(value)
        value = fixture(); children(value)[6]['accounts'] = [6]
        self.assert_rejected(value, 'ATA_CONTROL_LAYOUT_MISMATCH')
        value = fixture(); group = children(value); group[6],group[7] = group[7],group[6]
        self.assert_rejected(value, 'ATA_CONTROL_LAYOUT_MISMATCH')
        value = fixture(); data(children(value)[3], b'\x01')
        self.assert_rejected(value, 'ATA_SETUP_VARIANT_UNSUPPORTED')

    def test_external_distribution_before_birth_wrong_recipient_and_extra_signers(self):
        value = fixture(); distribution = copy.deepcopy(value['meta']['innerInstructions'][2]['instructions'][0])
        children(value).insert(13, distribution)
        self.assert_rejected(value, 'DISTRIBUTION_CALLER_OR_OUTER_ORDER_MISMATCH')
        for accounts in ([4,2,3], [4,6,0], [4,6,3,0]):
            value = fixture(); value['meta']['innerInstructions'][2]['instructions'][0]['accounts'] = accounts
            self.assert_rejected(value, 'DISTRIBUTION_LAYOUT_OR_BINDING_MISMATCH')
        value = fixture(); ix=value['meta']['innerInstructions'][2]['instructions'][0]; data(ix,b'\x03'+(SUPPLY+1).to_bytes(8,'little'))
        self.assert_rejected(value, 'DISTRIBUTION_AMOUNT_INVALID')

    def test_malformed_or_contradictory_balance_metadata(self):
        mutations = [('accountIndex',True),('owner','wrong'),('programId',TOKEN_2022),('mint','wrong'),
                     ('uiTokenAmount',{'amount':'1','decimals':6}),('uiTokenAmount',{'amount':1,'decimals':6}),
                     ('uiTokenAmount',{'amount':'0','decimals':True})]
        for field, replacement in mutations:
            value = fixture(); value['meta']['postTokenBalances'][0][field] = replacement
            self.assert_rejected(value)
        value = fixture(); value['meta']['postTokenBalances'].append(copy.deepcopy(value['meta']['postTokenBalances'][0]))
        self.assert_rejected(value, 'DUPLICATE_BALANCE_METADATA_INDEX')
        value = fixture(); value['meta']['preTokenBalances'] = copy.deepcopy(value['meta']['postTokenBalances'])
        self.assert_rejected(value, 'PREEXISTING_MINT_BALANCE_CONTRADICTION')
        value = fixture(); del value['meta']['postTokenBalances']
        result = analyze_legacy_pump_birth(value)
        self.assertTrue(result['structural_profile_match'])
        self.assertIn('TOKEN_BALANCE_METADATA_UNAVAILABLE', result['unknowns'])
        self.assert_unaccepted(result)

    def test_missing_depth_and_metadata_cannot_match_or_accept(self):
        for ordinal in (1, 12, 13):
            value = fixture(); del children(value)[ordinal]['stackHeight']
            self.assert_rejected(value)
        value = fixture(); value['meta']['err'] = {'InstructionError':[3,'Custom']}
        self.assert_rejected(value)
        value = fixture(); value['meta']['innerInstructions'] = None
        self.assert_rejected(value)

    def test_metadata_and_event_are_retained_opaque_without_schema_exception(self):
        result = analyze_legacy_pump_birth(fixture())
        paths = {row['instruction_path'] for row in result['uninterpreted_witnesses']}
        self.assertTrue({'3.8','3.9','3.10','3.11','3.14','5.3'} <= paths)
        self.assertIn('OPAQUE_METADATA_AND_EVENT_SEMANTICS_UNVERIFIED', result['acceptance_blockers'])
        self.assertNotIn('event', result['birth_witnesses'])
        self.assertTrue(result['reference_provenance']['reference_only'])

    def test_pure_no_transport_files_or_production_consumers(self):
        value = fixture()
        with patch('builtins.open', side_effect=AssertionError('No IO')), \
             patch('socket.socket', side_effect=AssertionError('No network')):
            result = analyze_legacy_pump_birth(value)
        self.assertTrue(result['structural_profile_match'])
        for path in (ROOT/'desk').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn('legacy_pump_birth',node.module or '')
                elif isinstance(node, ast.Import):
                    self.assertFalse(any('legacy_pump_birth' in name.name for name in node.names))


if __name__ == '__main__':
    unittest.main()
