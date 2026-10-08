"""Synthetic raw attack traces and unchanged public parsed-only witnesses.

No signing, broadcast, RPC, transport or positive legacy launch fixture. Public
fixture hashes pin existing captures, not proof of their finality/coverage.
"""
import ast
import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from research.execution_trace import reconstruct_execution_trace, PROFILE
from desk.legacy_controls import normalize_legacy_control
from desk.security import TOKEN_PROGRAM, TOKEN_2022, base58

ROOT = Path(__file__).resolve().parents[1]
WALLET, MINT, ATA, CHILD, OTHER, TABLE = [base58(bytes([n]) * 32) for n in range(1, 7)]
KEYS = [WALLET, MINT, ATA, CHILD, OTHER, TOKEN_PROGRAM, TOKEN_2022]


def raw(program=2, accounts=None, data=b'', height=1):
    return {'programIdIndex': program, 'accounts': [] if accounts is None else accounts,
            'data': base58(data) if data else '', 'stackHeight': height}


def record(heights=(), outer_count=1):
    return {'version': 'legacy', 'slot': 123, 'transaction': {'signatures': ['untrusted'],
            'message': {'accountKeys': list(KEYS), 'instructions': [raw() for _ in range(outer_count)]}},
            'meta': {'err': None, 'innerInstructions': [{'index': 0, 'instructions':
                     [raw(program=3 if h == 2 else 5, height=h) for h in heights]}] if heights else []}}


def rows(result):
    return {row['instruction_path']: row for row in result['instructions']}


class TraceOrderingTests(unittest.TestCase):
    def test_numeric_outer_and_inner_preorder_not_decoder_or_lexical_order(self):
        value = record(outer_count=12)
        value['meta']['innerInstructions'] = [
            {'index': 10, 'instructions': [raw(height=2)]},
            {'index': 2, 'instructions': [raw(height=2) for _ in range(12)]},
            {'index': 0, 'instructions': [raw(height=2)]}]
        result = reconstruct_execution_trace(value)
        self.assertEqual(result['status'], 'complete')
        paths = [row['instruction_path'] for row in result['instructions']]
        self.assertEqual(paths[:5], ['0', '0.0', '1', '2', '2.0'])
        self.assertLess(paths.index('2.2'), paths.index('2.10'))
        self.assertLess(paths.index('2.11'), paths.index('3'))
        self.assertLess(paths.index('9'), paths.index('10'))
        self.assertEqual(rows(result)['2.10']['path_components'], [2, 10])
        self.assertEqual(rows(result)['2.10']['group_source_position'], 1)
        self.assertEqual([row['invocation_ordinal'] for row in result['instructions']], list(range(26)))

    def test_nested_parent_not_outer_prefix_and_return_to_ancestor(self):
        result = reconstruct_execution_trace(record([2, 3, 4, 3, 2, 3, 2]))
        self.assertEqual(result['status'], 'complete')
        rr = rows(result)
        for path, parent in {'0.0': '0', '0.1': '0.0', '0.2': '0.1',
                             '0.3': '0.0', '0.4': '0', '0.5': '0.4', '0.6': '0'}.items():
            self.assertEqual(rr[path]['direct_parent_path'], parent)
            self.assertEqual(rr[path]['caller_program'], rr[parent]['program'])
        self.assertNotEqual(rr['0.1']['caller_program'], rr['0']['program'])

    def test_stack_resets_for_each_outer(self):
        value = record([2, 3], 2)
        value['meta']['innerInstructions'].append({'index': 1, 'instructions': [raw(height=3)]})
        result = reconstruct_execution_trace(value)
        self.assertIn('IMPOSSIBLE_STACK_JUMP', result['errors'])
        self.assertIsNone(rows(result)['1.0']['direct_parent_path'])

    def test_null_and_omitted_outer_stack_height_are_intrinsic_roots(self):
        for height in (None, 'omitted'):
            value = record([2])
            if height == 'omitted':
                del value['transaction']['message']['instructions'][0]['stackHeight']
            else:
                value['transaction']['message']['instructions'][0]['stackHeight'] = height
            result = reconstruct_execution_trace(value)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(rows(result)['0.0']['direct_parent_path'], '0')

    def test_outer_contradictory_depth_rejected(self):
        for height in (0, 2, True, '1', -1):
            with self.subTest(height=height):
                value = record([2])
                value['transaction']['message']['instructions'][0]['stackHeight'] = height
                result = reconstruct_execution_trace(value)
                self.assertIn('OUTER_STACK_HEIGHT_CONTRADICTION', result['errors'])
                self.assertIsNone(rows(result)['0.0']['caller_program'])

    def test_missing_depth_poisoning_no_stale_parent_and_local_recovery(self):
        value = record([2, None, 3, 4, 3, 2, 3])
        result = reconstruct_execution_trace(value)
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(result['errors'])
        rr = rows(result)
        self.assertIsNone(rr['0.1']['direct_parent_path'])
        self.assertIsNone(rr['0.2']['direct_parent_path'])
        self.assertEqual(rr['0.3']['direct_parent_path'], '0.2')
        self.assertIsNone(rr['0.4']['direct_parent_path'])
        self.assertEqual(rr['0.5']['direct_parent_path'], '0')
        self.assertEqual(rr['0.6']['direct_parent_path'], '0.5')
        del value['meta']['innerInstructions'][0]['instructions'][1]['stackHeight']
        missing = reconstruct_execution_trace(value)
        self.assertFalse(rows(missing)['0.1']['stack_height_presence'])
        self.assertTrue(rows(result)['0.1']['stack_height_presence'])

    def test_impossible_depth_jumps_rejected(self):
        for heights in ([3], [2, 4], [2, 3, 2, 4], [2, 3, 4, 2, 5]):
            with self.subTest(heights=heights):
                result = reconstruct_execution_trace(record(heights))
                self.assertEqual(result['status'], 'rejected')
                self.assertIn('IMPOSSIBLE_STACK_JUMP', result['errors'])
                self.assertIsNone(result['instructions'][-1]['direct_parent_path'])

    def test_invalid_depth_types_bounds_and_unsupported_explicit_indices(self):
        for height in (True, False, 1, 0, -1, 17, 2.0, '2', {}, []):
            with self.subTest(height=height):
                result = reconstruct_execution_trace(record([height]))
                self.assertIn('INNER_STACK_HEIGHT_INVALID', result['errors'])
        value = record([2, 2])
        value['meta']['innerInstructions'][0]['instructions'][1]['index'] = 0
        self.assertIn('UNSUPPORTED_INSTRUCTION_FIELDS', reconstruct_execution_trace(value)['errors'])

    def test_duplicate_groups_not_chosen_or_silently_merged(self):
        value = record([2], 2)
        value['meta']['innerInstructions'].append(copy.deepcopy(value['meta']['innerInstructions'][0]))
        result = reconstruct_execution_trace(value)
        self.assertIn('DUPLICATE_INNER_GROUP_INDEX', result['errors'])
        self.assertEqual(result['instructions'], [])
        self.assertEqual(result['source_record'], value)

    def test_bad_group_indices_shapes_and_budget_fail_closed(self):
        for index in (-1, 1, True, '0', 0.0):
            value = record([2]); value['meta']['innerInstructions'][0]['index'] = index
            self.assertEqual(reconstruct_execution_trace(value)['status'], 'rejected')
        for mutation in (None, {}, '0', [raw(height=2)]):
            value = record([2]); value['meta']['innerInstructions'][0] = mutation
            self.assertEqual(reconstruct_execution_trace(value)['status'], 'rejected')
        value = record([2] * 256)
        self.assertIn('INSTRUCTION_INSPECTION_BUDGET', reconstruct_execution_trace(value)['errors'])
        value = record(outer_count=65)
        self.assertIn('OUTER_INSPECTION_BUDGET_OR_SHAPE', reconstruct_execution_trace(value)['errors'])


class TraceKeyTests(unittest.TestCase):
    def versioned(self):
        value = record([2])
        value['version'] = 0
        message = value['transaction']['message']
        message['accountKeys'] = [WALLET, ATA]
        message['addressTableLookups'] = [{'accountKey': TABLE, 'writableIndexes': [4, 7], 'readonlyIndexes': [2]}]
        message['instructions'] = [raw(program=4, accounts=[0, 2, 3, 1])]
        value['meta']['loadedAddresses'] = {'writable': [MINT, CHILD], 'readonly': [TOKEN_PROGRAM]}
        value['meta']['innerInstructions'][0]['instructions'] = [raw(program=4, accounts=[2, 0], height=2)]
        return value

    def test_static_then_all_writable_then_all_readonly(self):
        value = self.versioned()
        message = value['transaction']['message']
        message['addressTableLookups'].append({'accountKey': OTHER, 'writableIndexes': [9], 'readonlyIndexes': [6]})
        value['meta']['loadedAddresses'] = {'writable': [MINT, CHILD, OTHER], 'readonly': [TOKEN_PROGRAM, TOKEN_2022]}
        message['instructions'][0] = raw(program=6, accounts=[0, 2, 4, 5, 3])
        result = reconstruct_execution_trace(value)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual([key['pubkey'] for key in result['key_witnesses']],
                         [WALLET, ATA, MINT, CHILD, OTHER, TOKEN_PROGRAM, TOKEN_2022])
        self.assertEqual(rows(result)['0']['program'], TOKEN_2022)
        self.assertEqual(rows(result)['0']['accounts'], [WALLET, MINT, OTHER, TOKEN_PROGRAM, CHILD])
        self.assertFalse(result['lookup_tables_authenticated'])

    def test_expanded_keys_not_double_appended_and_demoted_writable(self):
        value = self.versioned()
        expanded = [{'pubkey': key, 'source': source, 'signer': False, 'writable': False}
                    for key, source in [(WALLET, 'transaction'), (ATA, 'transaction'),
                                        (MINT, 'lookupTable'), (CHILD, 'lookupTable'), (TOKEN_PROGRAM, 'lookupTable')]]
        value['transaction']['message']['accountKeys'] = expanded
        for remove_loaded in (False, True):
            candidate = copy.deepcopy(value)
            if remove_loaded:
                del candidate['meta']['loadedAddresses']
            result = reconstruct_execution_trace(candidate)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(len(result['key_witnesses']), 5)
            self.assertEqual(result['key_witnesses'][2]['segment'], 'loaded_writable')
            self.assertFalse(result['key_witnesses'][2]['witness']['writable'])

    def test_expanded_vs_loaded_disagreement_rejected(self):
        value = self.versioned()
        value['transaction']['message']['accountKeys'] = [
            {'pubkey': key, 'source': 'transaction' if i < 2 else 'lookupTable', 'signer': False, 'writable': False}
            for i, key in enumerate([WALLET, ATA, CHILD, MINT, TOKEN_PROGRAM])]
        self.assertIn('PARSED_LOADED_KEYS_DISAGREE', reconstruct_execution_trace(value)['errors'])

    def test_expanded_source_flags_and_shape_rejected(self):
        value = record()
        value['transaction']['message']['accountKeys'] = [
            {'pubkey': key, 'source': 'transaction', 'signer': False, 'writable': False} for key in KEYS]
        for field, content in [('source', None), ('source', 'lookupTable'), ('signer', 1), ('writable', 'false')]:
            candidate = copy.deepcopy(value)
            candidate['transaction']['message']['accountKeys'][0][field] = content
            self.assertEqual(reconstruct_execution_trace(candidate)['status'], 'rejected')
        del value['transaction']['message']['accountKeys'][0]['source']
        self.assertEqual(reconstruct_execution_trace(value)['status'], 'unknown')

    def test_loaded_signer_and_readonly_writable_contradictions(self):
        for field, position in [('signer', 2), ('writable', 4)]:
            value = self.versioned()
            value['transaction']['message']['accountKeys'] = [
                {'pubkey': key, 'source': 'transaction' if i < 2 else 'lookupTable', 'signer': False, 'writable': False}
                for i, key in enumerate([WALLET, ATA, MINT, CHILD, TOKEN_PROGRAM])]
            value['transaction']['message']['accountKeys'][position][field] = True
            self.assertEqual(reconstruct_execution_trace(value)['status'], 'rejected')

    def test_missing_loaded_data_and_descriptor_counts_do_not_invent_keys(self):
        value = self.versioned()
        del value['meta']['loadedAddresses']
        result = reconstruct_execution_trace(value)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('LOADED_ADDRESSES_UNAVAILABLE', result['reasons'])
        self.assertFalse(result['instructions'])
        for counts in ([4], [4, 7, 8]):
            value = self.versioned()
            value['transaction']['message']['addressTableLookups'][0]['writableIndexes'] = counts
            self.assertIn('LOOKUP_COUNT_DISAGREEMENT', reconstruct_execution_trace(value)['errors'])

    def test_duplicate_key_aliases_and_lookup_indices_rejected(self):
        for part in ('static', 'loaded'):
            value = self.versioned()
            if part == 'static':
                value['transaction']['message']['accountKeys'][1] = WALLET
            else:
                value['meta']['loadedAddresses']['writable'][0] = WALLET
            self.assertIn('DUPLICATE_TRANSACTION_KEY', reconstruct_execution_trace(value)['errors'])
        value = self.versioned()
        value['transaction']['message']['addressTableLookups'][0]['readonlyIndexes'] = [4]
        self.assertIn('DUPLICATE_LOOKUP_INDEX', reconstruct_execution_trace(value)['errors'])
        value = self.versioned()
        value['transaction']['message']['addressTableLookups'].append(
            copy.deepcopy(value['transaction']['message']['addressTableLookups'][0]))
        self.assertIn('DUPLICATE_LOOKUP_TABLE', reconstruct_execution_trace(value)['errors'])

    def test_malformed_keys_mixed_encodings_and_legacy_lookup_claims(self):
        for key in (None, '', '0' * 32, '1' * 31, '1' * 33, 4, {}, []):
            value = record(); value['transaction']['message']['accountKeys'][0] = key
            self.assertEqual(reconstruct_execution_trace(value)['status'], 'rejected')
        value = self.versioned(); value['version'] = 'legacy'
        self.assertIn('LEGACY_LOOKUP_CONTRADICTION', reconstruct_execution_trace(value)['errors'])
        value = record(); value['meta']['loadedAddresses'] = {'writable': [OTHER], 'readonly': []}
        self.assertIn('LOOKUP_COUNT_DISAGREEMENT', reconstruct_execution_trace(value)['errors'])
        value = record(); value['transaction']['message']['accountKeys'] = [base58(bytes([i])*32) for i in range(65)]
        self.assertIn('KEY_INSPECTION_BUDGET_OR_SHAPE', reconstruct_execution_trace(value)['errors'])


class TraceInstructionTests(unittest.TestCase):
    def test_exact_raw_bytes_accounts_repetitions_and_provenance(self):
        value = record([2])
        value['meta']['innerInstructions'][0]['instructions'][0] = raw(5, [1, 0, 1], b'\x00\xff\x00', 2)
        result = reconstruct_execution_trace(value)
        row = rows(result)['0.0']
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(row['raw_hex'], '00ff00')
        self.assertEqual(row['accounts'], [MINT, WALLET, MINT])
        self.assertEqual(row['account_indices'], [1, 0, 1])
        self.assertEqual(row['raw_sha256'], hashlib.sha256(b'\x00\xff\x00').hexdigest())
        self.assertEqual(row['source_path'], 'meta.innerInstructions.0.instructions.0')

    def test_partially_decoded_matches_raw_resolution(self):
        value = record([2])
        value['meta']['innerInstructions'][0]['instructions'][0] = {
            'programId': TOKEN_PROGRAM, 'accounts': [MINT, WALLET], 'data': '3', 'stackHeight': 2}
        row = rows(reconstruct_execution_trace(value))['0.0']
        self.assertEqual(row['accounts'], [MINT, WALLET])
        self.assertEqual(row['account_indices'], [1, 0])
        self.assertEqual(row['raw_hex'], '02')

    def control(self, program=5):
        value = record([2])
        ix = raw(program, [1, 0], b'\x06\x00\x00', 2)
        ix['parsed'] = {'type': 'setAuthority', 'info': {'mint': MINT, 'authority': WALLET,
                       'authorityType': 'mintTokens', 'newAuthority': None}}
        value['meta']['innerInstructions'][0]['instructions'][0] = ix
        return value

    def test_normalizer_comparison_is_syntax_only_and_handoff_retains_parent(self):
        value = self.control()
        result = reconstruct_execution_trace(value)
        row = rows(result)['0.0']
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(row['parsed_agreement'])
        self.assertFalse(row['control_comparison']['ownership_approved'])
        # Explicit future-consumer handoff, not a production integration.
        normalized = normalize_legacy_control(row['program'], raw=bytes.fromhex(row['raw_hex']),
            accounts=row['accounts'], evidence={'instruction_path': row['instruction_path'],
            'parent_instruction_path': row['direct_parent_path'], 'caller_program': row['caller_program'],
            'stack_height': row['stack_height']})
        self.assertTrue(normalized['normalization_complete'])
        self.assertFalse(normalized['authority_authenticated'])

    def test_parsed_raw_target_role_amount_and_none_presence_attacks(self):
        for field, content in [('mint', OTHER), ('authority', OTHER),
                               ('authorityType', 'closeAccount'), ('newAuthority', OTHER)]:
            value = self.control(); value['meta']['innerInstructions'][0]['instructions'][0]['parsed']['info'][field] = content
            self.assertEqual(reconstruct_execution_trace(value)['status'], 'rejected')
        value = self.control()
        del value['meta']['innerInstructions'][0]['instructions'][0]['parsed']['info']['newAuthority']
        result = reconstruct_execution_trace(value)
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('PARSED_FIELD_MISSING', rows(result)['0.0']['control_comparison']['reasons'])
        value = self.control()
        ix = value['meta']['innerInstructions'][0]['instructions'][0]
        ix.update(accounts=[1, 4, 0], data=base58(b'\x04'+(7).to_bytes(8, 'little')),
                  parsed={'type': 'approve', 'info': {'source': MINT, 'delegate': OTHER, 'owner': WALLET, 'amount': '8'}})
        self.assertEqual(reconstruct_execution_trace(value)['status'], 'rejected')

    def test_control_trailing_bytes_and_program_disagreement_rejected(self):
        value = self.control()
        value['meta']['innerInstructions'][0]['instructions'][0]['data'] = base58(b'\x06\x00\x00\x00')
        self.assertEqual(reconstruct_execution_trace(value)['status'], 'rejected')
        value = self.control()
        value['meta']['innerInstructions'][0]['instructions'][0]['programId'] = TOKEN_2022
        self.assertIn('RAW_PARSED_PROGRAM_DISAGREEMENT', reconstruct_execution_trace(value)['errors'])

    def test_unsupported_raw_parsed_comparison_not_silently_trusted(self):
        for program in (2, 6):
            result = reconstruct_execution_trace(self.control(program))
            self.assertEqual(result['status'], 'unknown')
            self.assertIn('RAW_PARSED_COMPARISON_UNSUPPORTED', result['reasons'])
            self.assertIsNone(rows(result)['0.0']['parsed_agreement'])
        value = self.control(); value['meta']['innerInstructions'][0]['instructions'][0]['parsed'] = None
        self.assertIn('MALFORMED_PARSED_INSTRUCTION', reconstruct_execution_trace(value)['errors'])

    def test_parsed_only_does_not_recreate_raw_account_order_or_bytes(self):
        value = self.control()
        ix = value['meta']['innerInstructions'][0]['instructions'][0]
        del ix['programIdIndex']; del ix['accounts']; del ix['data']; ix['programId'] = TOKEN_PROGRAM
        result = reconstruct_execution_trace(value)
        self.assertEqual(result['status'], 'unknown')
        row = rows(result)['0.0']
        self.assertIsNone(row['raw_hex']); self.assertIsNone(row['accounts'])
        self.assertEqual(row['direct_parent_path'], '0')
        self.assertEqual(row['witness']['parsed'], ix['parsed'])

    def test_invalid_raw_indices_and_data_and_unbound_keys(self):
        for field, content in [('programIdIndex', -1), ('programIdIndex', True), ('programIdIndex', 7),
                               ('accounts', [True]), ('accounts', [-1]), ('accounts', [7]),
                               ('accounts', '1'), ('data', None), ('data', '0'), ('data', ['3', 'base64'])]:
            value = record([2]); value['meta']['innerInstructions'][0]['instructions'][0][field] = content
            self.assertEqual(reconstruct_execution_trace(value)['status'], 'rejected')
        value = record([2])
        value['meta']['innerInstructions'][0]['instructions'][0] = {'programId': TABLE, 'accounts': [], 'data': '', 'stackHeight': 2}
        self.assertIn('INSTRUCTION_KEY_NOT_IN_TRANSACTION', reconstruct_execution_trace(value)['errors'])


class TraceUnknownAndPublicTests(unittest.TestCase):
    def test_missing_and_failed_metadata_do_not_claim_all_outer_executed(self):
        for mode in ('absent_meta', 'absent_err', 'failed', 'absent_inner', 'null_inner'):
            value = record(outer_count=2)
            if mode == 'absent_meta': del value['meta']
            elif mode == 'absent_err': del value['meta']['err']
            elif mode == 'failed': value['meta']['err'] = {'InstructionError': [0, 'Custom']}
            elif mode == 'absent_inner': del value['meta']['innerInstructions']
            else: value['meta']['innerInstructions'] = None
            result = reconstruct_execution_trace(value)
            self.assertEqual(result['status'], 'unknown')
            self.assertFalse(result['operation_effect_order_verified'])
            if mode in ('absent_meta', 'absent_err', 'failed'):
                self.assertFalse(any(row['execution_observed'] for row in result['instructions']))

    def test_success_cannot_establish_caught_cpi_success_or_caller_effect_timing(self):
        value = record([2, 3])
        value['meta']['logMessages'] = ['Program failed: custom program error', 'Program success']
        result = reconstruct_execution_trace(value)
        self.assertEqual(result['status'], 'complete')
        self.assertFalse(result['cpi_success_verified'])
        self.assertFalse(result['operation_effect_order_verified'])
        self.assertIn('INDIVIDUAL_CPI_SUCCESS', result['unresolved_context'])
        self.assertIn('CALLER_EFFECT_TIMING', result['unresolved_context'])

    def test_forged_summary_flags_no_elevation_and_copied_source_identity(self):
        value = record([2])
        value.update(ownership_approved=True, authority_authenticated=True, lifecycle_verified=True)
        original = copy.deepcopy(value)
        result = reconstruct_execution_trace(value)
        self.assertEqual(value, original)
        self.assertEqual(result['profile'], PROFILE)
        self.assertEqual(result['source_sha256'], hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        for flag in ('evidence_authenticated', 'lookup_tables_authenticated', 'authority_authenticated',
                     'operation_effect_order_verified', 'cpi_success_verified', 'lifecycle_verified',
                     'ownership_approved', 'eligible_for_trading'):
            self.assertIs(result[flag], False)
        value['meta']['innerInstructions'].clear()
        self.assertEqual(result['source_record'], original)

    def test_unsupported_versions_non_json_binary_and_notification_wrappers(self):
        for version in (None, True, 1, '0', 'legacy-v1'):
            value = record(); value['version'] = version
            self.assertFalse(reconstruct_execution_trace(value)['syntax_complete'])
        for value in ({'transaction': ['bytes', 'base64']}, {'transaction': {'message': {}}},
                      {'params': {'result': record()}}, {'bad': float('nan')}, []):
            self.assertFalse(reconstruct_execution_trace(value)['syntax_complete'])

    def test_unchanged_public_nested_parent_provenance_and_raw_gaps(self):
        captures = [('mainnet-launch', 'd80cf9876fdb3e9b465e4103fa14bf2baaf09483dad9a27a57ff7339624b3bf2',
                     {'2.5': '2.4', '2.6': '2.4', '2.7': '2.4', '2.8': '2.4', '2.13': '2'}, 33),
                    ('mainnet-distribution', 'b5b6dfb1668a0307f2f3b95e6b1bb35361ebc122d97abe0b1f1cac6d26795803',
                     {'0.8': '0.7', '0.9': '0.7', '0.10': '0.7', '0.13': '0', '1.2': '1'}, 35)]
        for name, digest, parents, count in captures:
            with self.subTest(capture=name):
                content = (ROOT / 'fixtures' / (name + '.json')).read_bytes()
                self.assertEqual(hashlib.sha256(content).hexdigest(), digest)
                fixture = json.loads(content)
                value = fixture['payload']
                if name == 'mainnet-launch': value = value['params']['result']['transaction']
                before = copy.deepcopy(value)
                result = reconstruct_execution_trace(value)
                self.assertEqual(value, before)
                self.assertEqual(result['status'], 'unknown')
                self.assertEqual(result['errors'], [])
                self.assertEqual(result['reasons'], ['RAW_INSTRUCTION_UNAVAILABLE'])
                self.assertEqual(len(result['instructions']), count)
                rr = rows(result)
                for path, parent in parents.items():
                    self.assertEqual(rr[path]['direct_parent_path'], parent)
                self.assertIsNone(rr['2.13' if name == 'mainnet-launch' else '0.13']['raw_hex'])
                self.assertFalse(result['ownership_approved'])
                # The original public domain distinction stays explicit.
                self.assertEqual(rr['2.13' if name == 'mainnet-launch' else '0.13']['program'],
                                 TOKEN_2022 if name == 'mainnet-launch' else TOKEN_PROGRAM)
                if name == 'mainnet-distribution':
                    self.assertEqual(rr['0']['program'], 'dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN')

    def test_format_source_pins_and_disconnected_pure_scope(self):
        result = reconstruct_execution_trace(record([2]))
        self.assertEqual(len(result['source_provenance']), 6)
        for pin in result['source_provenance']:
            self.assertEqual(pin['repository'], 'solana-labs/solana')
            self.assertEqual(pin['commit'], 'd9f20e951a06b61e4505da0955228020b96a8915')
            self.assertRegex(pin['git_blob'], r'^[0-9a-f]{40}$')
        # Detect accidental production wiring, rather than mirroring algorithm.
        for path in (ROOT / 'desk').rglob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn('execution_trace', node.module or '')
                    self.assertNotEqual(node.module, 'research')
                elif isinstance(node, ast.Import):
                    self.assertFalse(any('execution_trace' in name.name or name.name == 'research'
                                         for name in node.names))
        with patch('socket.socket', side_effect=AssertionError('No network')), \
             patch('builtins.open', side_effect=AssertionError('No IO')):
            self.assertEqual(reconstruct_execution_trace(record([2]))['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
