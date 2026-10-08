"""Synthetic exact-byte control witnesses, never a positive launch fixture.

Public parsed-only controls below stay raw-layout-incomplete; the public legacy
mint is not Pump and the public Pump create_v2 is Token-2022. No transport,
signature generation, chain execution or runtime positive approval is used.
"""
import ast
import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from desk.legacy_controls import normalize_legacy_control, PROFILE
from desk.security import TOKEN_PROGRAM, TOKEN_2022, base58

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT, MINT, AUTHORITY, OTHER = [base58(bytes([n]) * 32) for n in range(1, 5)]


def parsed(kind, **info):
    return {'type': kind, 'info': info}


def witnesses():
    return [
        (b'\x06\x00\x00', [MINT, AUTHORITY], parsed(
            'setAuthority', mint=MINT, authority=AUTHORITY, authorityType='mintTokens', newAuthority=None)),
        (b'\x04' + (2**64 - 1).to_bytes(8, 'little'), [ACCOUNT, OTHER, AUTHORITY], parsed(
            'approve', source=ACCOUNT, delegate=OTHER, owner=AUTHORITY, amount=str(2**64 - 1))),
        (b'\x05', [ACCOUNT, AUTHORITY], parsed('revoke', source=ACCOUNT, owner=AUTHORITY)),
        (b'\x0a', [ACCOUNT, MINT, AUTHORITY], parsed(
            'freezeAccount', account=ACCOUNT, mint=MINT, freezeAuthority=AUTHORITY)),
        (b'\x0b', [ACCOUNT, MINT, AUTHORITY], parsed(
            'thawAccount', account=ACCOUNT, mint=MINT, freezeAuthority=AUTHORITY)),
        (b'\x0d' + (11).to_bytes(8, 'little') + b'\x06', [ACCOUNT, MINT, OTHER, AUTHORITY], parsed(
            'approveChecked', source=ACCOUNT, mint=MINT, delegate=OTHER, owner=AUTHORITY,
            tokenAmount={'amount': '11', 'decimals': 6, 'uiAmount': 0.000011, 'uiAmountString': '0.000011'})),
        (b'\x15', [MINT], parsed('getAccountDataSize', mint=MINT)),
        (b'\x15\x07\x00', [MINT], parsed('getAccountDataSize', mint=MINT, extensionTypes=['immutableOwner'])),
        (b'\x16', [ACCOUNT], parsed('initializeImmutableOwner', account=ACCOUNT)),
    ]


class LegacyControlNormalizerTests(unittest.TestCase):
    def normalize(self, raw, accounts, value, **kwargs):
        return normalize_legacy_control(TOKEN_PROGRAM, raw=raw, accounts=accounts, parsed=value, **kwargs)

    def assert_unapproved(self, result):
        for field in ('authority_authenticated', 'evidence_authenticated', 'deployed_code_verified',
                      'lifecycle_verified', 'ownership_approved', 'eligible_for_trading'):
            self.assertIs(result[field], False, field)
        self.assertIn('UNSUPPORTED_TOKEN_CONTROL_OPERATION', result['runtime_blockers'])
        self.assertNotIn('safe', result)
        self.assertNotIn('passed', result)

    def test_exact_supported_byte_and_parsed_profiles_keep_bindings(self):
        for raw, accounts, value in witnesses():
            with self.subTest(kind=value['type'], raw=raw.hex()):
                result = self.normalize(raw, accounts, value)
                self.assertTrue(result['normalization_complete'], result['reasons'])
                self.assertTrue(result['representations_match'])
                self.assertEqual(result['raw_operation'], result['parsed_operation'])
                self.assertEqual(result['raw_operation']['kind'], value['type'])
                self.assertEqual(result['raw_hex'], raw.hex())
                self.assertEqual(result['raw_sha256'], hashlib.sha256(raw).hexdigest())
                self.assert_unapproved(result)

    def test_all_four_authority_roles_none_and_key_are_distinct(self):
        for role_id, role in enumerate(('mintTokens', 'freezeAccount', 'accountOwner', 'closeAccount')):
            for new in (None, OTHER):
                with self.subTest(role=role, new=new):
                    target_field = 'mint' if role_id < 2 else 'account'
                    target = MINT if role_id < 2 else ACCOUNT
                    raw = bytes([6, role_id, 0 if new is None else 1])
                    if new is not None:
                        raw += bytes([4]) * 32
                    value = parsed('setAuthority', **{target_field: target, 'authority': AUTHORITY,
                                                     'authorityType': role, 'newAuthority': new})
                    result = self.normalize(raw, [target, AUTHORITY], value)
                    self.assertTrue(result['normalization_complete'])
                    operation = result['raw_operation']
                    self.assertEqual(operation['authority_role'], role)
                    self.assertEqual(operation['new_authority'], new)
                    self.assertEqual(operation['new_authority_presence'],
                                     'explicit_none' if new is None else 'explicit_key')
                    self.assertEqual(operation['target_mint'], target if role_id < 2 else None)
                    self.assertEqual(operation['target_account'], target if role_id >= 2 else None)
                    self.assert_unapproved(result)  # AccountOwner(None) is syntax, not successful execution.

    def test_missing_new_authority_cannot_become_explicit_null(self):
        raw, accounts, value = witnesses()[0]
        value['info'].pop('newAuthority')
        result = self.normalize(raw, accounts, value)
        self.assertFalse(result['normalization_complete'])
        self.assertIn('PARSED_FIELD_MISSING', result['reasons'])
        self.assertFalse(result['parsed_field_presence']['newAuthority'])
        self.assertEqual(result['raw_operation']['new_authority_presence'], 'explicit_none')
        self.assertIsNone(result['parsed_operation'])
        self.assertEqual(result['field_errors'], [{'reason': 'PARSED_FIELD_MISSING', 'field': 'newAuthority'}])

    def test_every_required_parsed_field_has_explicit_presence_check(self):
        for raw, accounts, value in witnesses():
            for field in value['info']:
                if field == 'extensionTypes':
                    continue  # Official parser omits only an empty extension request.
                with self.subTest(kind=value['type'], missing=field):
                    missing = copy.deepcopy(value)
                    missing['info'].pop(field)
                    result = self.normalize(raw, accounts, missing)
                    self.assertFalse(result['normalization_complete'])
                    self.assertIn('PARSED_FIELD_MISSING', result['reasons'])
                    self.assertFalse(result['parsed_field_presence'][field])

    def test_optional_extension_request_presence_is_not_invented(self):
        raw, accounts, value = witnesses()[7]
        value['info'].pop('extensionTypes')
        result = self.normalize(raw, accounts, value)
        self.assertTrue(result['parsed_fields_complete'])
        self.assertFalse(result['parsed_field_presence']['extensionTypes'])
        self.assertIn('CONTROL_RAW_PARSED_MISMATCH', result['reasons'])
        empty = self.normalize(b'\x15', [MINT], parsed('getAccountDataSize', mint=MINT))
        self.assertTrue(empty['normalization_complete'])
        self.assertFalse(empty['parsed_field_presence']['extensionTypes'])

    def test_raw_only_is_distinct_from_malformed_supplied_parsed(self):
        raw, accounts, _ = witnesses()[0]
        raw_only = normalize_legacy_control(TOKEN_PROGRAM, raw=raw, accounts=accounts)
        self.assertTrue(raw_only['normalization_complete'])
        self.assertFalse(raw_only['parsed_supplied'])
        self.assertIsNone(raw_only['representations_match'])
        result = self.normalize(raw, accounts, None)
        self.assertFalse(result['normalization_complete'])
        self.assertTrue(result['parsed_supplied'])
        self.assertIn('PARSED_CONTROL_SHAPE_INVALID', result['reasons'])

    def test_parsed_only_never_claims_exact_raw_layout(self):
        _, _, value = witnesses()[0]
        result = normalize_legacy_control(TOKEN_PROGRAM, parsed=value)
        self.assertTrue(result['parsed_fields_complete'])
        self.assertEqual(result['parsed_operation']['target_mint'], MINT)
        self.assertEqual(result['parsed_operation']['new_authority_presence'], 'explicit_none')
        self.assertFalse(result['raw_layout_complete'])
        self.assertFalse(result['normalization_complete'])
        self.assertIn('RAW_CONTROL_BYTES_MISSING_OR_INVALID', result['reasons'])
        self.assert_unapproved(result)

    def test_truncation_and_suffixes_never_silently_disappear(self):
        cases = witnesses() + [(b'\x06\x00\x01' + bytes([4]) * 32, [MINT, AUTHORITY], parsed(
            'setAuthority', mint=MINT, authority=AUTHORITY, authorityType='mintTokens', newAuthority=OTHER))]
        for raw, accounts, value in cases:
            for altered in [raw[:n] for n in range(len(raw))] + [raw + b'\x00', raw + b'unknown']:
                # Empty size query is a separately supported, fully known shape;
                # its mismatching parsed extension list still fails normalization.
                with self.subTest(kind=value['type'], altered=altered.hex()):
                    result = self.normalize(altered, accounts, value)
                    self.assertFalse(result['normalization_complete'])
                    self.assertTrue(result['reasons'])
                    self.assert_unapproved(result)

    def test_instruction_option_is_one_byte_not_account_coption(self):
        for raw in (b'\x06\x00' + bytes(4), b'\x06\x00' + (1).to_bytes(4, 'little') + bytes([4]) * 32,
                    b'\x06\x00\x02', b'\x06\x00\xff'):
            with self.subTest(raw=raw.hex()):
                result = self.normalize(raw, [MINT, AUTHORITY], witnesses()[0][2])
                self.assertFalse(result['raw_layout_complete'])

    def test_unknown_roles_and_json_case_aliases_are_not_legacy_roles(self):
        for role in range(4, 256):
            result = normalize_legacy_control(TOKEN_PROGRAM, raw=bytes([6, role, 0]), accounts=[MINT, AUTHORITY])
            self.assertIn('CONTROL_AUTHORITY_ROLE_UNSUPPORTED', result['reasons'])
        for role in ('MintTokens', 'AccountOwner', 'permanentDelegate', None, True, []):
            with self.subTest(role=role):
                value = copy.deepcopy(witnesses()[0][2])
                value['info']['authorityType'] = role
                self.assertFalse(self.normalize(witnesses()[0][0], [MINT, AUTHORITY], value)['normalization_complete'])

    def test_unknown_instruction_tags_and_multisig_are_retained_and_rejected(self):
        supported = {4, 5, 6, 10, 11, 13, 21, 22}
        for tag in set(range(256)) - supported:
            raw = bytes([tag]) + bytes(34)
            result = normalize_legacy_control(TOKEN_PROGRAM, raw=raw, accounts=[ACCOUNT])
            self.assertIn('CONTROL_TAG_UNSUPPORTED', result['reasons'])
            self.assertEqual(result['raw_tag'], tag)
            self.assertEqual(result['raw_hex'], raw.hex())
        for kind in ('initializeMultisig', 'initializeMultisig2', 'futureControl'):
            result = normalize_legacy_control(TOKEN_PROGRAM, parsed=parsed(kind, account=ACCOUNT))
            self.assertIn('PARSED_CONTROL_TYPE_UNSUPPORTED', result['reasons'])

    def test_exact_account_counts_reject_extra_signer_accounts(self):
        for raw, accounts, value in witnesses():
            for altered in (accounts[:-1], accounts + [OTHER]):
                with self.subTest(kind=value['type'], count=len(altered)):
                    result = self.normalize(raw, altered, value)
                    self.assertFalse(result['normalization_complete'])
                    self.assertEqual(result['account_witness'], altered)

    def test_raw_parsed_target_authority_and_delegate_substitutions_rejected(self):
        for raw, accounts, value in witnesses():
            for field in ('mint', 'account', 'source', 'authority', 'owner', 'delegate', 'freezeAuthority'):
                if field not in value['info']:
                    continue
                with self.subTest(kind=value['type'], field=field):
                    altered = copy.deepcopy(value)
                    altered['info'][field] = base58(bytes([9]) * 32)
                    result = self.normalize(raw, accounts, altered)
                    self.assertIn('CONTROL_RAW_PARSED_MISMATCH', result['reasons'])
                    self.assertNotEqual(result['raw_operation'], result['parsed_operation'])

    def test_explicit_new_key_cannot_be_replaced_with_null(self):
        value = copy.deepcopy(witnesses()[0][2])
        result = self.normalize(b'\x06\x00\x01' + bytes([4]) * 32, [MINT, AUTHORITY], value)
        self.assertIn('CONTROL_RAW_PARSED_MISMATCH', result['reasons'])
        self.assertEqual(result['raw_operation']['new_authority_presence'], 'explicit_key')
        self.assertEqual(result['parsed_operation']['new_authority_presence'], 'explicit_none')

    def test_account_and_mint_roles_cannot_alias_through_extra_json_fields(self):
        value = copy.deepcopy(witnesses()[0][2])
        value['info']['account'] = MINT
        result = self.normalize(witnesses()[0][0], [MINT, AUTHORITY], value)
        self.assertIn('PARSED_CONTROL_EXTRA_FIELDS', result['reasons'])
        self.assertTrue(result['parsed_field_presence']['account'])

    def test_malformed_parsed_shapes_and_null_required_values_fail(self):
        raw, accounts, _ = witnesses()[0]
        for value in (None, [], {}, {'type': 'setAuthority'}, {'type': [], 'info': {}},
                      {'type': 'setAuthority', 'info': None}, {'type': 'setAuthority', 'info': {1: None}}):
            with self.subTest(value=value):
                self.assertFalse(self.normalize(raw, accounts, value)['normalization_complete'])
        for field in ('mint', 'authority'):
            value = copy.deepcopy(witnesses()[0][2])
            value['info'][field] = None
            self.assertIn('CONTROL_ADDRESS_INVALID', self.normalize(raw, accounts, value)['reasons'])

    def test_amounts_and_decimals_are_integer_exact_not_ui_values(self):
        raw, accounts, value = witnesses()[5]
        for amount in (None, True, 11, 11.0, '-1', '1e1', str(2**64), '9' * 100, '١١'):
            with self.subTest(amount=amount):
                altered = copy.deepcopy(value)
                altered['info']['tokenAmount']['amount'] = amount
                self.assertIn('PARSED_AMOUNT_INVALID', self.normalize(raw, accounts, altered)['reasons'])
        for decimals in (None, True, '6', 256, -1):
            altered = copy.deepcopy(value)
            altered['info']['tokenAmount']['decimals'] = decimals
            self.assertIn('PARSED_DECIMALS_INVALID', self.normalize(raw, accounts, altered)['reasons'])
        for field in ('amount', 'decimals'):
            altered = copy.deepcopy(value)
            altered['info']['tokenAmount'].pop(field)
            self.assertIn('PARSED_FIELD_MISSING', self.normalize(raw, accounts, altered)['reasons'])
        value['info']['tokenAmount']['uiAmount'] = 999999
        result = self.normalize(raw, accounts, value)
        self.assertTrue(result['normalization_complete'])
        self.assertEqual(result['raw_operation']['amount_raw'], '11')

    def test_zero_and_maximum_approvals_are_not_safe_classifications(self):
        for amount in (0, 2**64 - 1):
            result = self.normalize(b'\x04' + amount.to_bytes(8, 'little'), [ACCOUNT, OTHER, AUTHORITY],
                                    parsed('approve', source=ACCOUNT, delegate=OTHER, owner=AUTHORITY, amount=str(amount)))
            self.assertTrue(result['normalization_complete'])
            self.assertEqual(result['raw_operation']['delegate'], OTHER)
            self.assertEqual(result['raw_operation']['amount_raw'], str(amount))
            self.assert_unapproved(result)

    def test_extension_requests_are_an_exact_two_shape_profile(self):
        for raw in (b'\x15\x00\x00', b'\x15\x07', b'\x15\x07\x00\x07\x00', b'\x15\xff\xff'):
            self.assertIn('CONTROL_EXTENSION_REQUEST_UNSUPPORTED', self.normalize(raw, [MINT], witnesses()[6][2])['reasons'])
        for extensions in (None, {}, ['immutableOwner', 'immutableOwner'], ['transferHook'], ['permanentDelegate']):
            value = parsed('getAccountDataSize', mint=MINT, extensionTypes=extensions)
            self.assertIn('CONTROL_EXTENSION_REQUEST_UNSUPPORTED', self.normalize(b'\x15', [MINT], value)['reasons'])

    def test_legacy_immutable_owner_rule_is_conditional_noop_not_state_proof(self):
        result = self.normalize(*witnesses()[8])
        self.assertTrue(result['normalization_complete'])
        self.assertIn('requires_uninitialized_account', result['reference_semantics'])
        self.assertIn('owner_is_not_immutable', result['reference_semantics'])
        self.assert_unapproved(result)

    def test_token2022_and_unknown_programs_never_inherit_legacy_decoding(self):
        for program in (TOKEN_2022, OTHER, None):
            for raw, accounts, value in witnesses():
                result = normalize_legacy_control(program, raw=raw, accounts=accounts, parsed=value)
                self.assertFalse(result['normalization_complete'])
                self.assertIsNone(result['raw_operation'])
                self.assertIsNone(result['parsed_operation'])
                self.assertIsNone(result['reference_semantics'])
                self.assertEqual(result['parsed_witness'], value)
                self.assertEqual(result['program_family'], 'token2022' if program == TOKEN_2022 else 'unknown')
                self.assert_unapproved(result)

    def test_resolved_signer_flags_are_preserved_without_pda_authentication(self):
        raw, accounts, value = witnesses()[0]
        metas = [{'pubkey': accounts[0], 'isSigner': False, 'isWritable': True},
                 {'pubkey': accounts[1], 'isSigner': False, 'isWritable': False}]
        evidence = {'instruction_path': '2.13', 'parent_instruction_path': '2', 'caller_program': OTHER,
                    'stack_height': 2, 'slot': 123, 'payload_hash': 'a' * 64, 'caller_verified': True,
                    'safe': True, 'lifecycle_verified': True, 'signature': 'untrusted-signature'}
        result = self.normalize(raw, metas, value, evidence=evidence)
        self.assertTrue(result['normalization_complete'])
        self.assertFalse(result['account_metas'][1]['is_signer'])
        self.assertTrue(result['account_metas'][1]['signer_present'])
        self.assertEqual(result['untrusted_evidence'], evidence)
        self.assertEqual(result['instruction_path_components'], [2, 13])
        self.assert_unapproved(result)
        bare = self.normalize(raw, accounts, value)
        self.assertIsNone(bare['account_metas'][1]['is_signer'])
        self.assertFalse(bare['account_metas'][1]['signer_present'])

    def test_malformed_flags_addresses_and_unresolved_indices_do_not_coerce(self):
        raw, accounts, value = witnesses()[0]
        for altered in ([0, 1], [{'pubkey': accounts[0], 'isSigner': 'false'}, accounts[1]],
                        [{'pubkey': accounts[0], 'isWritable': 1}, accounts[1]],
                        ['wrong', accounts[1]], [{'pubkey': accounts[0], 'signer': True}, accounts[1]]):
            with self.subTest(accounts=altered):
                result = self.normalize(raw, altered, value)
                self.assertFalse(result['normalization_complete'])
                self.assertEqual(result['account_witness'], altered)

    def test_caller_and_order_context_is_structural_not_approval(self):
        for evidence in ({'instruction_path': '02.1'}, {'instruction_path': '2.1.3'},
                         {'instruction_path': '٢.١'}, {'instruction_path': '2.1\n'},
                         {'parent_instruction_path': []}, {'stack_height': True},
                         {'stack_height': 17}, {'slot': -1}, {'caller_program': 'wrong'}, []):
            with self.subTest(evidence=evidence):
                result = self.normalize(*witnesses()[0], evidence=evidence)
                self.assertFalse(result['normalization_complete'])
                self.assertEqual(result['untrusted_evidence'], evidence)
        result = self.normalize(*witnesses()[0], evidence={'instruction_path': '2.10'})
        self.assertEqual(result['instruction_path_components'], [2, 10])
        self.assertFalse(result['evidence_authenticated'])

    def test_restored_controls_keep_separate_instruction_witnesses(self):
        for indices in ((1, 2), (3, 4)):
            rows = [self.normalize(*witnesses()[i], evidence={'instruction_path': f'2.{j}'})
                    for j, i in enumerate(indices)]
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0]['raw_operation']['kind'], rows[1]['raw_operation']['kind'])
            for row in rows:
                self.assertTrue(row['normalization_complete'])
                self.assert_unapproved(row)

    def test_inputs_and_reference_pins_cannot_be_mutated_through_output(self):
        raw, accounts, value = witnesses()[0]
        evidence = {'instruction_path': '2.13', 'signers': [AUTHORITY]}
        originals = copy.deepcopy((accounts, value, evidence))
        result = self.normalize(raw, accounts, value, evidence=evidence)
        self.assertEqual((accounts, value, evidence), originals)
        result['account_witness'].append(OTHER)
        result['parsed_witness']['info']['mint'] = OTHER
        result['untrusted_evidence']['signers'].append(OTHER)
        result['source_provenance'][0]['commit'] = 'forged'
        self.assertEqual((accounts, value, evidence), originals)
        again = self.normalize(raw, accounts, value, evidence=evidence)
        self.assertEqual(again['source_provenance'][0]['commit'], 'ad2b81274075c45e6ef428e52479b7d3d8f0dd6a')
        self.assertEqual(again['profile'], PROFILE)

    def test_public_parsed_only_controls_keep_provenance_and_remain_incomplete(self):
        for name, family, outer, child in (
                ('mainnet-distribution.json', TOKEN_PROGRAM, 0, 13),
                ('mainnet-launch.json', TOKEN_2022, 2, 13)):
            with self.subTest(fixture=name):
                path = ROOT / 'fixtures' / name
                fixture = json.loads(path.read_text())
                payload = fixture['payload']
                envelope = payload['params']['result'] if 'params' in payload else payload
                container = envelope['transaction'] if 'params' in payload else envelope
                group = next(g for g in container['meta']['innerInstructions'] if g['index'] == outer)
                ix = group['instructions'][child]
                self.assertEqual(ix['programId'], family)
                value = ix['parsed']
                result = normalize_legacy_control(family, parsed=value, evidence={
                    'instruction_path': f'{outer}.{child}', 'stack_height': ix['stackHeight'],
                    'slot': envelope['slot'], 'fixture_provenance': fixture['provenance'],
                    'fixture_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
                self.assertFalse(result['normalization_complete'])
                self.assertEqual(result['parsed_witness'], value)
                self.assertTrue(result['parsed_field_presence']['newAuthority'])
                self.assert_unapproved(result)
                if family == TOKEN_PROGRAM:
                    self.assertEqual(result['parsed_operation']['target_mint'], fixture['mint'])
                    self.assertIn('RAW_CONTROL_BYTES_MISSING_OR_INVALID', result['reasons'])
                    self.assertNotEqual(container['transaction']['message']['instructions'][0]['programId'],
                                        '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P')
                else:
                    self.assertIn('TOKEN_2022_CONTROL_UNSUPPORTED', result['reasons'])

    def test_pure_function_does_not_read_files_or_call_transport(self):
        with patch('builtins.open', side_effect=AssertionError('No file I/O')), \
                patch('socket.socket', side_effect=AssertionError('No network')), \
                patch('desk.providers.helius_rpc', side_effect=AssertionError('No provider')):
            result = self.normalize(*witnesses()[0])
        self.assertTrue(result['normalization_complete'])
        self.assert_unapproved(result)

    def test_runtime_consumers_do_not_import_the_foundation(self):
        # This scoped module must not become a positive runtime gate by accident.
        for path in (ROOT / 'desk').glob('*.py'):
            if path.name == 'legacy_controls.py':
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn('legacy_controls', node.module or '', path.name)
                elif isinstance(node, ast.Import):
                    self.assertFalse(any('legacy_controls' in n.name for n in node.names), path.name)


if __name__ == '__main__':
    unittest.main()
