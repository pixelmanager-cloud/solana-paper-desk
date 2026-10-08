"""Source-built synthetic accounting and unchanged public raw launch replay.

Synthetic balances demonstrate arithmetic only, not captured execution/state.
No provider, signer or broadcast; opaque UI token floats never enter accounting.
"""
import ast
import copy
import hashlib
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from desk.security import base58, TOKEN_PROGRAM
from research.create_v2_auxiliary import SYSTEM, COMPUTE
from research.execution_trace import reconstruct_execution_trace
from research.native_lamports import report_native_lamports, U64_MAX, MESSAGE_PIN

ROOT = Path(__file__).resolve().parents[1]
PAYER, TARGET, OTHER, PROGRAM, TABLE = [base58(bytes([n]) * 32) for n in range(1, 6)]


def ix(raw, accounts=(0, 1), program=3, height=1):
    return {'programIdIndex': program, 'accounts': list(accounts),
            'data': base58(raw) if raw else '', 'stackHeight': height}


def transfer(amount=100, accounts=(0, 1), height=1):
    return ix(struct.pack('<IQ', 2, amount), accounts, height=height)


def synthetic():
    return {'version': 'legacy', 'slot': 123, 'transaction': {'signatures': ['synthetic-not-authenticated'],
        'message': {'accountKeys': [PAYER, TARGET, OTHER, SYSTEM, PROGRAM, COMPUTE],
            'header': {'numRequiredSignatures': 1, 'numReadonlySignedAccounts': 0, 'numReadonlyUnsignedAccounts': 3},
            'instructions': [transfer()]}}, 'meta': {'err': None, 'fee': 5, 'innerInstructions': [],
                'preBalances': [1000, 0, 0, 1, 1, 1], 'postBalances': [895, 100, 0, 1, 1, 1], 'rewards': []}}


def public():
    return json.loads((ROOT/'fixtures/mainnet-launch-raw.json').read_bytes())['result']


class NativeLamportTests(unittest.TestCase):
    def unapproved(self, result):
        for field in ('evidence_authenticated', 'balance_effects_verified', 'fee_verified', 'rent_verified',
                'recipient_authorization_verified', 'individual_cpi_success_verified', 'effect_order_verified',
                'authenticated_lifecycle_accepted', 'lifecycle_verified', 'ownership_approved', 'eligible_for_trading'):
            self.assertIs(result[field], False, field)
        for row in result['accounts'] + result['movements'] + result['inventory']:
            self.assertIs(row['effects_verified'], False)
        if result['aggregate']:self.assertIs(result['aggregate']['effects_verified'], False)

    def run_record(self, record):
        before = copy.deepcopy(record)
        out = report_native_lamports(record)
        self.assertEqual(record, before)
        self.unapproved(out)
        return out

    def test_positive_exact_transfer_conditional_arithmetic_only(self):
        out = self.run_record(synthetic())
        self.assertEqual(out['errors'], [])
        self.assertEqual(out['gaps'], [])
        self.assertTrue(out['comparison_complete'])
        self.assertTrue(out['predicted_net_agreement'])
        self.assertTrue(out['observed_instruction_model_complete'])
        self.assertEqual(out['status'], 'net_agreement_only')
        self.assertEqual(out['accounts'][0]['conditional_predicted_delta'], '-105')
        self.assertEqual(out['accounts'][1]['residual'], '0')
        self.assertEqual(out['aggregate']['observed_delta'], '-5')
        self.assertIn('DECLARED_FEE_NOT_INDEPENDENTLY_ESTABLISHED', out['unknowns'])
        self.assertEqual(out['fee_payer']['pubkey'], PAYER)

    def test_create_account_is_movement_not_rent_or_owner_approval(self):
        r = synthetic()
        r['transaction']['message']['instructions'][0] = ix(struct.pack('<IQQ', 0, 100, 170) + bytes([4])*32)
        out = self.run_record(r)
        self.assertTrue(out['predicted_net_agreement'])
        self.assertEqual(out['movements'][0]['kind'], 'createAccount')
        self.assertIn('RENT_EXEMPTION_AND_RENT_EFFECTS_UNESTABLISHED', out['unknowns'])

    def test_original_public_sample_zero_residual_retains_all_gaps(self):
        path = ROOT/'fixtures/mainnet-launch-raw.json'; saved = path.read_bytes()
        self.assertEqual(hashlib.sha256(saved).hexdigest(), '27d481575656d2b254a63bf699a32ce3dd599ce3b55fc8b04da2aa79dd356520')
        out = self.run_record(public())
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(len(out['inventory']), 33)
        self.assertEqual(len(out['accounts']), 34)
        self.assertEqual(len(out['movements']), 11)
        self.assertEqual(len(out['unmodeled_instruction_paths']), 20)
        self.assertEqual(len(out['cpi_outcome_unknown_paths']), 28)
        self.assertTrue(out['predicted_net_agreement'])
        self.assertFalse(out['observed_instruction_model_complete'])
        self.assertEqual(out['aggregate']['declared_fee'], '147774')
        self.assertEqual(out['aggregate']['observed_delta'], '-147774')
        self.assertEqual(out['aggregate']['absolute_account_residual_sum'], '0')
        self.assertEqual(out['gaps'], [])
        self.assertEqual(out['errors'], [])
        self.assertEqual([m['instruction_path'] for m in out['movements']],
            ['2.0','2.3','2.6','2.9','3.1','4.0','4.3','4.4','4.5','4.6','4.7'])
        self.assertIn('UNMODELED_DIRECT_LAMPORT_MUTATIONS_OR_OTHER_PROGRAM_EFFECTS_POSSIBLE', out['unknowns'])
        self.assertEqual(out['accounts'][1]['observed_delta'], '2773680')

    def test_account_residuals_cannot_cancel_in_aggregate(self):
        r = synthetic(); r['meta']['postBalances'][1] -= 7; r['meta']['postBalances'][2] += 7
        out = self.run_record(r)
        self.assertTrue(out['aggregate']['net_agrees'])
        self.assertEqual(out['aggregate']['absolute_account_residual_sum'], '14')
        self.assertEqual(out['gaps'], ['ACCOUNT_RESIDUAL:1', 'ACCOUNT_RESIDUAL:2'])
        self.assertFalse(out['predicted_net_agreement'])

    def test_aggregate_residual_detects_unexplained_loss_or_gain(self):
        for amount in (-1, 1):
            r = synthetic(); r['meta']['postBalances'][0] += amount
            out = self.run_record(r)
            self.assertIn('AGGREGATE_RESIDUAL', out['gaps'])
            self.assertEqual(out['aggregate']['residual'], str(amount))

    def test_same_raw_movement_at_different_paths_counted_each_once(self):
        r = synthetic(); r['transaction']['message']['instructions'].append(transfer())
        r['meta']['postBalances'][:2] = [795, 200]
        out = self.run_record(r)
        self.assertTrue(out['predicted_net_agreement'])
        self.assertEqual([m['instruction_path'] for m in out['movements']], ['0','1'])
        self.assertEqual(out['movements'][0]['raw_sha256'], out['movements'][1]['raw_sha256'])

    def test_duplicate_inventory_path_defense_never_double_counts(self):
        r = synthetic(); trace = reconstruct_execution_trace(r)
        trace['instructions'].append(copy.deepcopy(trace['instructions'][0]))
        with patch('research.native_lamports.reconstruct_execution_trace', return_value=trace):
            out = self.run_record(r)
        self.assertIn('DUPLICATE_INVENTORY_PATH:0', out['errors'])
        self.assertEqual(len(out['movements']), 1)
        self.assertEqual(len(out['inventory']), 2)
        self.assertFalse(out['predicted_net_agreement'])

    def test_cpi_committed_assumption_never_promoted_from_success_logs(self):
        r = synthetic(); r['transaction']['message']['instructions'] = [ix(b'opaque', (), 4)]
        r['meta']['innerInstructions'] = [{'index':0,'instructions':[transfer(height=2)]}]
        r['meta']['logMessages'] = ['Program '+SYSTEM+' success']
        out = self.run_record(r)
        self.assertTrue(out['predicted_net_agreement'])
        self.assertEqual(out['cpi_outcome_unknown_paths'], ['0.0'])
        self.assertFalse(out['observed_instruction_model_complete'])
        self.assertIn('INDIVIDUAL_CPI_OUTCOMES_AND_CAUGHT_FAILURES_UNESTABLISHED', out['unknowns'])

    def test_caught_cpi_failure_witness_is_not_suppressed(self):
        r = synthetic(); r['transaction']['message']['instructions'] = [ix(b'opaque', (), 4)]
        r['meta']['innerInstructions'] = [{'index':0,'instructions':[transfer(height=2)]}]
        r['meta']['postBalances'][:2] = [995, 0]
        r['meta']['logMessages'] = ['Program '+SYSTEM+' failed: custom program error: 0x1', 'Program '+PROGRAM+' success']
        out = self.run_record(r)
        self.assertEqual(len(out['movements']), 1)
        self.assertEqual(out['accounts'][0]['residual'], '100')
        self.assertEqual(out['accounts'][1]['residual'], '-100')
        self.assertFalse(out['predicted_net_agreement'])
        self.assertTrue(out['aggregate']['net_agrees'])

    def test_failed_transaction_keeps_conditional_invocations_but_not_agreement(self):
        r = synthetic(); r['meta']['err'] = {'InstructionError':[0,'InvalidArgument']}
        r['meta']['postBalances'][:2] = [995,0]
        out = self.run_record(r)
        self.assertFalse(out['comparison_complete'])
        self.assertFalse(out['predicted_net_agreement'])
        self.assertEqual(len(out['movements']),1)
        self.assertIn('TRANSACTION_NOT_SUCCESSFUL_MODEL_COMMIT_ASSUMPTION_UNRESOLVED',out['unknowns'])

    def test_missing_execution_status_or_inner_inventory_not_assumed_complete(self):
        for field in ('err','innerInstructions'):
            r = synthetic(); del r['meta'][field]
            out = self.run_record(r)
            self.assertFalse(out['comparison_complete'])
            self.assertFalse(out['predicted_net_agreement'])

    def test_unknown_program_direct_mutation_produces_residual_and_retained_row(self):
        r = synthetic(); r['transaction']['message']['instructions'].append(ix(b'direct write', (1,2),4))
        r['meta']['postBalances'][1] -= 9; r['meta']['postBalances'][2] += 9
        out = self.run_record(r)
        self.assertEqual(out['unmodeled_instruction_paths'], ['1'])
        self.assertEqual(len(out['inventory']),2)
        self.assertFalse(out['predicted_net_agreement'])
        self.assertTrue(out['aggregate']['net_agrees'])

    def test_token_close_refund_is_unsupported_direct_effect_not_inferred_rent(self):
        r=synthetic();r['transaction']['message']['accountKeys'].append(TOKEN_PROGRAM)
        r['meta']['preBalances'].append(1);r['meta']['postBalances'].append(1)
        r['meta']['preBalances'][2]=200;r['meta']['postBalances'][2]=0
        r['meta']['postBalances'][0]+=200
        r['transaction']['message']['instructions'].append(ix(b'\x09',(2,0,0),6))
        out=self.run_record(r)
        self.assertEqual(out['unmodeled_instruction_paths'],['1'])
        self.assertIn('ACCOUNT_RESIDUAL:0',out['gaps'])
        self.assertIn('ACCOUNT_RESIDUAL:2',out['gaps'])
        self.assertTrue(out['aggregate']['net_agrees'])
        self.assertFalse(out['predicted_net_agreement'])
        self.assertFalse(out['rent_verified'])

    def test_zero_net_unknown_effect_does_not_grant_coverage(self):
        r = synthetic(); r['transaction']['message']['instructions'].append(ix(b'unknown', (1,2),4))
        out = self.run_record(r)
        self.assertTrue(out['predicted_net_agreement'])
        self.assertFalse(out['observed_instruction_model_complete'])
        self.assertEqual(out['unmodeled_instruction_paths'],['1'])

    def test_unsupported_system_variants_never_ignored_even_if_balances_fit(self):
        for raw in (struct.pack('<I',1)+bytes(32),struct.pack('<I',8)+bytes(40),struct.pack('<I',3)+bytes(20),b''):
            r = synthetic();r['transaction']['message']['instructions'][0] = ix(raw)
            r['meta']['postBalances'][:2] = [995,0]
            out = self.run_record(r)
            self.assertIn('UNSUPPORTED_OR_MALFORMED_AUXILIARY:0',out['errors'])
            self.assertEqual(out['unmodeled_instruction_paths'],['0'])
            self.assertFalse(out['predicted_net_agreement'])

    def test_every_system_layout_truncation_and_suffix_refused(self):
        for raw in (struct.pack('<IQ',2,100),struct.pack('<IQQ',0,100,170)+bytes([4])*32):
            for broken in [raw[:n] for n in range(len(raw))]+[raw+b'\0']:
                r=synthetic();r['transaction']['message']['instructions'][0]=ix(broken)
                out=self.run_record(r)
                self.assertFalse(out['predicted_net_agreement'])
                self.assertEqual(out['movements'],[])

    def test_fee_and_balance_strict_u64_no_coercion_defaults_or_floats(self):
        for field in ('fee','preBalances','postBalances'):
            for bad in (-1, U64_MAX+1,True,1.0,'1',None):
                r=synthetic()
                if field=='fee':r['meta'][field]=bad
                else:r['meta'][field][0]=bad
                out=self.run_record(r)
                self.assertTrue(out['errors'],(field,bad))
                self.assertFalse(out['comparison_complete'])
                self.assertFalse(out['predicted_net_agreement'])
        for field in ('fee','preBalances','postBalances'):
            r=synthetic();del r['meta'][field]
            self.assertFalse(self.run_record(r)['predicted_net_agreement'])

    def test_u64_boundary_and_large_aggregate_exact_integers(self):
        r=synthetic();r['transaction']['message']['instructions'][0]=transfer(U64_MAX)
        r['meta']['fee']=0;r['meta']['preBalances']=[U64_MAX,0,U64_MAX,1,1,1]
        r['meta']['postBalances']=[0,U64_MAX,U64_MAX,1,1,1]
        out=self.run_record(r)
        self.assertTrue(out['predicted_net_agreement'])
        self.assertEqual(out['aggregate']['pre_lamports'],str(2*U64_MAX+3))
        self.assertEqual(out['accounts'][0]['observed_delta'],str(-U64_MAX))

    def test_zero_fee_and_self_transfer_repeated_account_reference_are_explicit(self):
        r=synthetic();r['meta']['fee']=0;r['transaction']['message']['instructions'][0]=transfer(100,(0,0))
        r['meta']['postBalances']=list(r['meta']['preBalances'])
        out=self.run_record(r)
        self.assertTrue(out['predicted_net_agreement'])
        self.assertEqual(len(out['movements']),1)
        self.assertEqual(out['movements'][0]['account_indices'],[0,0])
        self.assertEqual(out['aggregate']['conditional_predicted_delta'],'0')

    def test_predicted_endpoint_outside_u64_is_gap_not_order_claim(self):
        for amount in (U64_MAX,):
            r=synthetic();r['transaction']['message']['instructions'][0]=transfer(amount)
            out=self.run_record(r)
            self.assertIn('CONDITIONAL_PREDICTED_ENDPOINT_OUTSIDE_U64:0',out['gaps'])
            self.assertFalse(out['predicted_net_agreement'])

    def test_balance_lengths_missing_extra_and_malformed_arrays(self):
        for value in ([],[1], [1]*7,{},None):
            r=synthetic();r['meta']['preBalances']=value
            out=self.run_record(r);self.assertEqual(out['accounts'],[])
            self.assertFalse(out['comparison_complete'])

    def test_fee_payer_header_missing_malformed_readonly_or_unsigned_rejects(self):
        for header in (None,{}, {'numRequiredSignatures':0,'numReadonlySignedAccounts':0,'numReadonlyUnsignedAccounts':3},
                {'numRequiredSignatures':1,'numReadonlySignedAccounts':1,'numReadonlyUnsignedAccounts':3},
                {'numRequiredSignatures':7,'numReadonlySignedAccounts':0,'numReadonlyUnsignedAccounts':0},
                {'numRequiredSignatures':True,'numReadonlySignedAccounts':0,'numReadonlyUnsignedAccounts':3},
                {'numRequiredSignatures':1,'numReadonlySignedAccounts':0,'numReadonlyUnsignedAccounts':6}):
            r=synthetic();r['transaction']['message']['header']=header
            out=self.run_record(r)
            self.assertIn('FEE_PAYER_HEADER_MISSING_OR_CONTRADICTORY',out['errors'])
            self.assertIsNone(out['fee_payer'])
            self.assertFalse(out['predicted_net_agreement'])

    def test_loaded_key_order_balance_binding_and_lookup_duplicates(self):
        r=synthetic();r['version']=0
        r['transaction']['message']['addressTableLookups']=[{'accountKey':TABLE,'writableIndexes':[2],'readonlyIndexes':[3]}]
        r['meta']['loadedAddresses']={'writable':[base58(bytes([7])*32)],'readonly':[base58(bytes([8])*32)]}
        r['transaction']['message']['instructions'][0]['accounts'][1]=6
        r['meta']['preBalances'] += [0,2];r['meta']['postBalances']=[895,0,0,1,1,1,100,2]
        out=self.run_record(r)
        self.assertTrue(out['predicted_net_agreement'])
        self.assertEqual(out['accounts'][6]['segment'],'loaded_writable')
        self.assertEqual(out['accounts'][7]['segment'],'loaded_readonly')
        r['transaction']['message']['addressTableLookups'][0]['readonlyIndexes']=[2]
        self.assertIn('TRACE:DUPLICATE_LOOKUP_INDEX',self.run_record(r)['errors'])

    def test_missing_duplicate_or_malformed_keys_and_invalid_instruction_indices(self):
        for mode in ('duplicate','malformed','missing','program','account','bool'):
            r=synthetic();m=r['transaction']['message']
            if mode=='duplicate':m['accountKeys'][1]=m['accountKeys'][0]
            elif mode=='malformed':m['accountKeys'][1]='bad'
            elif mode=='missing':del m['accountKeys']
            elif mode=='program':m['instructions'][0]['programIdIndex']=6
            elif mode=='account':m['instructions'][0]['accounts'][1]=6
            else:m['instructions'][0]['accounts'][1]=True
            out=self.run_record(r)
            self.assertTrue(out['errors']);self.assertFalse(out['predicted_net_agreement'])

    def test_duplicate_inner_group_index_is_not_duplicate_movement(self):
        r=synthetic();r['transaction']['message']['instructions'].append(ix(b'opaque',(),4));g={'index':0,'instructions':[transfer(height=2)]}
        r['meta']['innerInstructions']=[g,copy.deepcopy(g)]
        out=self.run_record(r)
        self.assertIn('TRACE:DUPLICATE_INNER_GROUP_INDEX',out['errors'])
        self.assertEqual(out['movements'],[])

    def test_caller_depth_unknown_retains_raw_movement_but_no_complete_comparison(self):
        r=synthetic();r['transaction']['message']['instructions']=[ix(b'opaque',(),4)]
        child=transfer(height=None);r['meta']['innerInstructions']=[{'index':0,'instructions':[child]}]
        out=self.run_record(r)
        self.assertEqual(len(out['movements']),1)
        self.assertIsNone(out['movements'][0]['direct_parent_path'])
        self.assertFalse(out['comparison_complete'])

    def test_rewards_and_compute_not_independently_reconciled(self):
        r=synthetic();r['meta']['rewards']=[{'pubkey':TARGET,'lamports':100}]
        r['transaction']['message']['instructions'].append(ix(b'\3'+struct.pack('<Q',999999),(),5))
        out=self.run_record(r)
        self.assertEqual(len(out['movements']),1)
        self.assertTrue(out['predicted_net_agreement'])
        self.assertIn('NONEMPTY_REWARDS_EFFECTS_UNMODELED',out['unknowns'])
        self.assertFalse(out['fee_verified'])

    def test_source_depth_nodes_strings_bytes_cycles_and_non_json_bounded(self):
        values=[]
        r=synthetic();r['extra']='x'*20001;values.append((r,'SOURCE_STRING_BOUND'))
        r=synthetic();r['extra']=['x'*10000 for _ in range(110)];values.append((r,'SOURCE_BYTE_BOUND'))
        r=synthetic();r['extra']=[None]*33000;values.append((r,'SOURCE_DEPTH_OR_NODE_BOUND'))
        r=synthetic();v=None
        for _ in range(34):v=[v]
        r['extra']=v;values.append((r,'SOURCE_DEPTH_OR_NODE_BOUND'))
        r=synthetic();r['extra']=r;values.append((r,'SOURCE_CYCLE'))
        for r,code in values:
            out=report_native_lamports(r);self.unapproved(out)
            self.assertIn(code,out['errors']);self.assertEqual(out['inventory'],[])
        for value in (set(),b'raw',float('nan'),float('inf'),1.0):
            r=synthetic();r['extra']=value;out=report_native_lamports(r)
            self.assertIn('SOURCE_NON_JSON_OR_FLOAT',out['errors'])
        r=synthetic();r['extra']=1<<257
        self.assertIn('SOURCE_INTEGER_BOUND',report_native_lamports(r)['errors'])

    def test_original_ui_float_is_opaque_never_raw_lamport_coercion(self):
        r=synthetic();r['meta']['preTokenBalances']=[{'uiTokenAmount':{'uiAmount':1.25}}]
        out=self.run_record(r);self.assertTrue(out['predicted_net_agreement'])
        r['meta']['fee']=1.25;self.assertFalse(self.run_record(r)['predicted_net_agreement'])
        r=synthetic();r['meta']['preTokenBalances']=[{'uiTokenAmount':{'uiAmount':float('nan')}}]
        self.assertIn('SOURCE_NON_JSON_OR_FLOAT',report_native_lamports(r)['errors'])

    def test_key_and_instruction_budget_limits(self):
        r=synthetic();r['transaction']['message']['accountKeys']=[base58(bytes([n])*32) for n in range(65)]
        self.assertFalse(self.run_record(r)['predicted_net_agreement'])
        r=synthetic();r['transaction']['message']['instructions']=[transfer()]*65
        self.assertIn('TRACE:OUTER_INSPECTION_BUDGET_OR_SHAPE',self.run_record(r)['errors'])
        r=synthetic();r['meta']['innerInstructions']=[{'index':0,'instructions':[transfer(height=2)]*256}]
        self.assertIn('TRACE:INSTRUCTION_INSPECTION_BUDGET',self.run_record(r)['errors'])

    def test_raw_parsed_second_representation_unknown_never_discarded(self):
        r=synthetic();r['transaction']['message']['instructions'][0]['parsed']={
            'type':'transfer','info':{'source':PAYER,'destination':OTHER,'lamports':100}}
        out=self.run_record(r)
        self.assertFalse(out['comparison_complete'])
        self.assertFalse(out['predicted_net_agreement'])
        self.assertEqual(len(out['movements']),1)
        self.assertIn('TRACE:RAW_PARSED_COMPARISON_UNSUPPORTED',out['unknowns'])

    def test_missing_loaded_keys_and_duplicate_static_loaded_identity_reject(self):
        r=synthetic();r['version']=0;r['transaction']['message']['addressTableLookups']=[{
            'accountKey':TABLE,'writableIndexes':[1],'readonlyIndexes':[]}]
        out=self.run_record(r)
        self.assertFalse(out['predicted_net_agreement'])
        self.assertIn('TRACE:LOADED_ADDRESSES_UNAVAILABLE',out['unknowns'])
        r['meta']['loadedAddresses']={'writable':[PAYER],'readonly':[]}
        self.assertIn('TRACE:DUPLICATE_TRANSACTION_KEY',self.run_record(r)['errors'])

    def test_no_production_consumer_or_runtime_changes(self):
        self.assertEqual(MESSAGE_PIN[1],'d9f20e951a06b61e4505da0955228020b96a8915')
        for path in (ROOT/'desk').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node,ast.ImportFrom):self.assertNotIn('native_lamports',node.module or '')
                if isinstance(node,ast.Import):self.assertFalse(any('native_lamports' in n.name for n in node.names))


if __name__ == '__main__':
    unittest.main()
