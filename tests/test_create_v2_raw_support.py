"""Unchanged raw/parsed public references and explicitly mutated adversarial copies.

No endpoint snapshots, acquisition, signing, broadcasting or provider requests.
Auxiliary syntax tests use source-built bytes; mutated copies are not captures.
"""
import ast
import copy
import hashlib
import json
from pathlib import Path
import struct
import unittest

from solders.pubkey import Pubkey

from desk.programs import unbase58
from desk.security import base58
from research.create_v2_auxiliary import normalize_create_v2_auxiliary, SYSTEM, COMPUTE, FEES, SOURCE_PINS
from research.create_v2_lifecycle import report_create_v2_lifecycle, PUMP
from research.execution_trace import reconstruct_execution_trace
from research.token2022_instructions import normalize_token2022_instruction
from tests.test_create_v2_lifecycle import synthetic

ROOT = Path(__file__).resolve().parents[1]
RAW_FILE_SHA = '27d481575656d2b254a63bf699a32ce3dd599ce3b55fc8b04da2aa79dd356520'
RAW_CANONICAL_SHA = '2b49cd4c6fe24a121cb59894c8a6fe0e82e4cf51db28e88151612984ccc9a0c7'
PARSED_FILE_SHA = 'd80cf9876fdb3e9b465e4103fa14bf2baaf09483dad9a27a57ff7339624b3bf2'


def raw_fixture():
    return json.loads((ROOT/'fixtures/mainnet-launch-raw.json').read_bytes())['result']


def parsed_fixture():
    return json.loads((ROOT/'fixtures/mainnet-launch.json').read_bytes())['payload']['params']['result']['transaction']


def report(record, endpoints=None):
    return report_create_v2_lifecycle(record,endpoint_states=endpoints,source_context={
        'slot':record['slot'],'signature':record['transaction']['signatures'][0]})


def instruction_at(record, path):
    pieces=list(map(int,path.split('.')))
    if len(pieces)==1:return record['transaction']['message']['instructions'][pieces[0]]
    group=next(g for g in record['meta']['innerInstructions'] if g['index']==pieces[0])
    return group['instructions'][pieces[1]]


def data(ix, raw):
    ix['data']=base58(raw) if raw else ''


class CreateV2RawSupportTests(unittest.TestCase):
    def unapproved(self, value):
        for name in ('lifecycle_verified','ownership_approved','eligible_for_trading'):
            self.assertIs(value[name],False,name)
        if 'authenticated_lifecycle_accepted' in value:
            for name in ('authenticated_lifecycle_accepted','snapshot_authenticated','effect_order_verified',
                         'individual_cpi_success_verified','signer_authorization_verified','finality_verified'):
                self.assertIs(value[name],False,name)
            self.assertTrue(all(e['verified'] is False for e in value['graduation_evidence']))

    def rejected(self, record, code=None):
        before=copy.deepcopy(record);r=report(record)
        self.assertEqual(record,before)
        self.assertFalse(r['supported_sequence_agreement'])
        self.unapproved(r)
        if code:self.assertIn(code,r['errors'])
        return r

    def test_original_raw_and_parsed_bytes_unchanged_and_canonical_digest_distinguished(self):
        raw=(ROOT/'fixtures/mainnet-launch-raw.json').read_bytes()
        parsed=(ROOT/'fixtures/mainnet-launch.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),RAW_FILE_SHA)
        self.assertEqual(hashlib.sha256(parsed).hexdigest(),PARSED_FILE_SHA)
        envelope=json.loads(raw)
        canonical=json.dumps(envelope,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
        self.assertEqual(hashlib.sha256(canonical).hexdigest(),RAW_CANONICAL_SHA)
        before=copy.deepcopy(envelope);r=report(envelope['result'])
        self.assertEqual(envelope,before)
        self.assertEqual((ROOT/'fixtures/mainnet-launch-raw.json').read_bytes(),raw)
        self.assertEqual((ROOT/'fixtures/mainnet-launch.json').read_bytes(),parsed)
        self.unapproved(r)

    def test_raw_rows_resolve_same_program_parent_and_known_accounts_as_parsed(self):
        raw=reconstruct_execution_trace(raw_fixture());parsed=reconstruct_execution_trace(parsed_fixture())
        self.assertEqual(raw['status'],'complete')
        self.assertEqual(len(raw['instructions']),33)
        for a,b in zip(raw['instructions'],parsed['instructions']):
            self.assertEqual(a['instruction_path'],b['instruction_path'])
            self.assertEqual(a['program'],b['program'])
            self.assertEqual(a['direct_parent_path'],b['direct_parent_path'])
            if b['accounts'] is not None:self.assertEqual(a['accounts'],b['accounts'])
            if b['raw_hex'] is not None:self.assertEqual(a['raw_hex'],b['raw_hex'])

    def test_cited_token_controls_already_have_complete_pinned_raw_syntax(self):
        trace=reconstruct_execution_trace(raw_fixture())
        targets={'2.5':'getAccountDataSize','2.7':'initializeImmutableOwner',
                 '2.11':'updateTokenMetadataAuthority','2.13':'setAuthority',
                 '3.0':'getAccountDataSize','3.2':'initializeImmutableOwner'}
        for row in trace['instructions']:
            if row['instruction_path'] not in targets:continue
            syntax=normalize_token2022_instruction(row['program'],raw=bytes.fromhex(row['raw_hex']),accounts=row['accounts'])
            self.assertTrue(syntax['normalization_complete'])
            self.assertEqual(syntax['raw_operation']['kind'],targets[row['instruction_path']])
            self.unapproved(syntax)

    def test_raw_birth_roles_resolve_without_hiding_eight_incomplete_buy_rows(self):
        record=raw_fixture();r=report(record)
        self.assertEqual(r['errors'],[])
        self.assertFalse(r['observed_inventory_agreement'])
        self.assertFalse(r['supported_sequence_agreement'])
        self.assertEqual(r['adverse_control_witnesses'],[])
        self.assertEqual(r['inventory_summary'],{'total_observed':33,'raw_complete':33,
            'unresolved_role_paths':[],'incomplete_structural_role_paths':['4.0','4.1','4.3','4.4','4.5','4.6','4.7','4.8'],
            'ignored_instructions':0,'actual_execution_coverage_verified':False})
        self.assertIn('RAW_STATE_MISSING',r['unknowns'])
        self.assertEqual(r['endpoint_states'],[])
        self.assertEqual(len(r['unresolved_effect_witnesses']),7)
        self.assertEqual(r['birth_witnesses']['mint_revocation'],'2.13')
        self.assertEqual(len(r['partial_event_witnesses']),1)
        self.assertEqual(len(r['partial_event_witnesses'][0]['unknown_suffix_hex']),16)
        self.assertFalse(r['partial_event_witnesses'][0]['schema_complete'])
        self.unapproved(r)

    def test_curve_allocation_is_distinct_from_later_curve_sol_transfer(self):
        r=report(raw_fixture());by_path={i['instruction']['instruction_path']:i for i in r['inventory']}
        allocation=by_path['2.3']['auxiliary_syntax']['operation']
        transfer=by_path['4.5']['auxiliary_syntax']['operation']
        self.assertEqual(allocation['kind'],'createAccount')
        self.assertEqual(allocation['space'],141)
        self.assertEqual(transfer['kind'],'transfer')
        self.assertEqual(transfer['lamports_raw'],'493827159')
        self.assertEqual(allocation['target'],transfer['destination'])
        self.assertFalse(by_path['4.5']['structural_role_complete'])
        self.assertEqual(by_path['2.3']['instruction']['direct_parent_path'],'2')
        self.assertEqual(by_path['4.5']['instruction']['direct_parent_path'],'4')

    def test_duplicate_actual_curve_allocation_still_rejects(self):
        r=raw_fixture();g=r['meta']['innerInstructions'][0]['instructions'];g.insert(4,copy.deepcopy(g[3]))
        self.rejected(r,'CURVE_ALLOCATION_MISSING_OR_DUPLICATE')

    def test_auxiliary_exact_compute_and_fee_fields_use_primary_schema(self):
        r=report(raw_fixture());rows={i['instruction']['instruction_path']:i for i in r['inventory']}
        self.assertEqual(rows['0']['auxiliary_syntax']['operation'],{'kind':'setComputeUnitLimit','units':219163})
        self.assertEqual(rows['1']['auxiliary_syntax']['operation'],{'kind':'setComputeUnitPrice','micro_lamports_raw':'628634'})
        fee=rows['4.1']['auxiliary_syntax']['operation']
        self.assertEqual(fee['kind'],'getFeesWithQuoteMint')
        self.assertEqual(fee['config_program'],PUMP)
        self.assertEqual(fee['quote_mint'],SYSTEM)
        self.assertIs(fee['is_pump_pool'],True)
        self.assertFalse(rows['4.1']['structural_role_complete'])
        self.unapproved(rows['4.1']['auxiliary_syntax'])

    def test_all_supported_auxiliary_truncations_and_suffixes_reject(self):
        rows=[i for i in report(raw_fixture())['inventory'] if i['auxiliary_syntax']]
        for item in rows:
            row=item['instruction'];raw=bytes.fromhex(row['raw_hex'])
            for size in range(len(raw)):
                n=normalize_create_v2_auxiliary(row['program'],raw=raw[:size],accounts=row['accounts'])
                self.assertFalse(n['syntax_complete'],(row['instruction_path'],size))
                self.unapproved(n)
            for suffix in (b'\0',b'evil'):
                self.assertFalse(normalize_create_v2_auxiliary(row['program'],raw=raw+suffix,accounts=row['accounts'])['syntax_complete'])

    def test_unknown_tags_programs_malformed_accounts_not_generic_permissions(self):
        for program,raw,accounts in ((SYSTEM,struct.pack('<I',1)+bytes(32),[SYSTEM,PUMP]),
            (COMPUTE,b'\1'+bytes(4),[]),(COMPUTE,b'\4'+bytes(4),[]),
            (PUMP,b'\2'+bytes(4),[]),(SYSTEM,bytes(52),[]),
            (SYSTEM,bytes(52),[SYSTEM,PUMP,FEES]),(SYSTEM,bytes(52),[SYSTEM,'bad']),
            (COMPUTE,b'\2'+bytes(4),[PUMP]),(FEES,bytes(57),[SYSTEM,PUMP])):
            n=normalize_create_v2_auxiliary(program,raw=raw,accounts=accounts)
            self.assertFalse(n['syntax_complete'])
            self.unapproved(n)

    def test_fee_bool_wrong_config_program_quote_or_pda_rejects(self):
        for mode in ('bool','program','quote','config'):
            r=raw_fixture();ix=instruction_at(r,'4.1');raw=bytearray(unbase58(ix['data']))
            if mode=='bool':raw[8]=2
            elif mode=='program':ix['accounts'][1]=0
            elif mode=='config':ix['accounts'][0]=0
            else:raw[25:57]=bytes(Pubkey.from_string(PUMP))
            data(ix,bytes(raw));self.rejected(r)

    def test_fee_query_wrong_direct_parent_does_not_use_outer_prefix(self):
        r=raw_fixture();instruction_at(r,'4.1')['stackHeight']=3
        result=self.rejected(r)
        self.assertIn('4.1',result['inventory_summary']['unresolved_role_paths'])
        witness=next(i for i in result['inventory'] if i['instruction']['instruction_path']=='4.1')
        self.assertEqual(witness['instruction']['direct_parent_path'],'4.0')

    def test_compute_duplicate_or_inner_cpi_never_ignored(self):
        r=raw_fixture();data(instruction_at(r,'1'),unbase58(instruction_at(r,'0')['data']))
        self.rejected(r,'DUPLICATE_COMPUTE_OPERATION')
        r=raw_fixture();ix=copy.deepcopy(instruction_at(r,'0'));ix['stackHeight']=2
        r['meta']['innerInstructions'][0]['instructions'].append(ix)
        self.rejected(r,'COMPUTE_CONTEXT_OR_SYNTAX_UNSUPPORTED')

    def test_control_restoration_and_unknown_program_still_reject_and_remain_visible(self):
        for raw in (b'\4'+bytes(8),b'\6\2\1'+bytes(Pubkey.from_string(PUMP)),b'\x0a'):
            r=raw_fixture();ix=copy.deepcopy(instruction_at(r,'2.13'));data(ix,raw)
            r['meta']['innerInstructions'][0]['instructions'].append(ix)
            out=self.rejected(r)
            self.assertTrue(any(w['instruction']['raw_hex']==raw.hex() for w in out['adverse_control_witnesses']))
        r=raw_fixture();ix=copy.deepcopy(instruction_at(r,'4.1'));ix['programIdIndex']=0
        r['meta']['innerInstructions'][2]['instructions'].append(ix)
        self.rejected(r,'UNMATCHED_OBSERVED_INSTRUCTION:4.9')

    def test_buy_transfer_unknown_recipient_malformed_sender_or_suffix_not_allowed(self):
        for mode in ('recipient','sender','suffix'):
            r=raw_fixture();ix=instruction_at(r,'4.5')
            if mode=='recipient':ix['accounts'][1]=1
            elif mode=='sender':ix['accounts'][0]=1
            else:data(ix,unbase58(ix['data'])+b'\0')
            self.rejected(r)

    def test_user_volume_allocation_wrong_owner_size_or_key_rejects(self):
        for mode in ('owner','size','key'):
            r=raw_fixture();ix=instruction_at(r,'4.0');raw=bytearray(unbase58(ix['data']))
            if mode=='owner':raw[20:52]=bytes(Pubkey.from_string(SYSTEM))
            elif mode=='size':raw[12:20]=struct.pack('<Q',165)
            else:ix['accounts'][1]=1
            data(ix,bytes(raw));self.rejected(r)

    def test_opaque_trade_event_bytes_retained_never_made_complete(self):
        r=raw_fixture();ix=instruction_at(r,'4.8');old=unbase58(ix['data'])
        data(ix,old[:-8]+b'\xff'*8)
        out=report(r)
        self.assertFalse(out['observed_inventory_agreement'])
        self.assertFalse(out['supported_sequence_agreement'])
        self.assertEqual(out['partial_event_witnesses'][0]['unknown_suffix_hex'],'ff'*8)
        self.assertIn('TRADE_EVENT_SCHEMA_SUFFIX_UNRESOLVED:4.8',out['unknowns'])
        self.unapproved(out)

    def test_duplicate_or_redirected_partial_trade_event_rejects(self):
        r=raw_fixture();g=r['meta']['innerInstructions'][2]['instructions'];g.append(copy.deepcopy(g[-1]))
        self.rejected(r,'DUPLICATE_PARTIAL_TRADE_EVENT')
        r=raw_fixture();ix=instruction_at(r,'4.8');raw=bytearray(unbase58(ix['data']));raw[16:48]=bytes(Pubkey.from_string(PUMP));data(ix,bytes(raw))
        self.rejected(r,'TRADE_EVENT_PREFIX_BINDING_MISMATCH')

    def test_even_synthetic_endpoint_shapes_do_not_resolve_raw_buy_effects(self):
        r=raw_fixture();_,endpoints,_=synthetic()
        for state in endpoints:state['transaction_signature']=r['transaction']['signatures'][0]
        out=report(r,endpoints)
        self.assertEqual(out['errors'],[])
        self.assertTrue(out['endpoint_amount_comparison']['amounts_agree'])
        self.assertFalse(out['observed_inventory_agreement'])
        self.assertFalse(out['supported_sequence_agreement'])
        self.unapproved(out)

    def test_later_slot_state_not_substituted_for_missing_launch_endpoint(self):
        r=raw_fixture();_,endpoints,_=synthetic()
        for state in endpoints:
            state['transaction_signature']=r['transaction']['signatures'][0];state['slot']=454452430
        out=report(r,endpoints)
        self.assertFalse(out['supported_sequence_agreement'])
        self.assertIn('ENDPOINT_SOURCE_SLOT_MISMATCH',out['errors'])
        self.assertIn('ENDPOINT_RECORD_SLOT_MISMATCH',out['errors'])
        self.unapproved(out)

    def test_source_schema_hashes_and_no_production_consumers(self):
        self.assertEqual(len(SOURCE_PINS),3)
        self.assertEqual(hashlib.sha256((ROOT/'desk/schemas/pump_fees.json').read_bytes()).hexdigest(),
            'd87b52305fd6b2ec487d4ba1e08a49990c23fa9b8b76092b2097df0164fa3859')
        for path in (ROOT/'desk').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node,ast.ImportFrom):self.assertNotIn('create_v2_auxiliary',node.module or '')
                if isinstance(node,ast.Import):self.assertFalse(any('create_v2_auxiliary' in n.name for n in node.names))


if __name__=='__main__':
    unittest.main()
