"""Synthetic completions of a saved public shape, never recovered raw evidence.

Only saved raw Pump create/buy/CreateEvent bytes are copied. Every raw SPL,
System/ATA instruction, header/signature envelope and endpoint state is synthetic.
The saved notification remains unchanged and is tested separately as unresolved.
"""
import ast
import copy
import hashlib
import json
from pathlib import Path
import struct
import unittest

from solders.pubkey import Pubkey

from desk.programs import BorshReader, unbase58
from desk.security import base58, TOKEN_PROGRAM
from research.create_v2_lifecycle import (report_create_v2_lifecycle, TOKEN_2022,
    SYSTEM, ATA, PUMP, PR48_HEAD, GRADUATION_EVIDENCE, SUPPLY)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SHA = 'd80cf9876fdb3e9b465e4103fa14bf2baaf09483dad9a27a57ff7339624b3bf2'


def public_reference():
    return json.loads((ROOT/'fixtures/mainnet-launch.json').read_bytes())['payload']['params']['result']


def key(value):
    return bytes(Pubkey.from_string(value))


def string(value):
    data = value.encode('utf-8')
    return struct.pack('<I', len(data)) + data


def synthetic():
    reference = public_reference()['transaction']
    create = reference['transaction']['message']['instructions'][2]
    buy = reference['transaction']['message']['instructions'][4]
    accounts = create['accounts']
    mint, authority, curve, curve_ata, _, payer = accounts[:6]
    holder = str(Pubkey.find_program_address([key(payer), key(TOKEN_2022), key(mint)], Pubkey.from_string(ATA))[0])
    reader = BorshReader(unbase58(create['data'])[8:], {})
    text = {name: reader.read('string') for name in ('name','symbol','uri')}
    amount = int.from_bytes(unbase58(buy['data'])[8:16], 'little')
    keys = list(dict.fromkeys([payer,mint]+accounts+buy['accounts']+[holder]))

    def ix(program, ordered, raw, depth=None):
        result = {'programIdIndex':keys.index(program), 'accounts':[keys.index(a) for a in ordered],
                  'data':base58(raw) if raw else ''}
        if depth is not None:result['stackHeight']=depth
        return result

    def alloc(target, size, owner, depth):
        return ix(SYSTEM,[payer,target],struct.pack('<IQQ',0,1000000,size)+key(owner),depth)

    def ata_children(target, owner, depth):
        return [ix(TOKEN_2022,[mint],b'\x15\x07\0',depth),alloc(target,170,TOKEN_2022,depth),
                ix(TOKEN_2022,[target],b'\x16',depth),ix(TOKEN_2022,[target,mint],b'\x12'+key(owner),depth)]

    event = reference['meta']['innerInstructions'][0]['instructions'][14]
    children = [alloc(mint,234,TOKEN_2022,2),
        ix(TOKEN_2022,[mint],bytes([39,0])+bytes(32)+key(mint),2),
        ix(TOKEN_2022,[mint],bytes([20,6])+key(authority)+b'\0',2),alloc(curve,141,PUMP,2),
        ix(ATA,[payer,curve_ata,curve,mint,SYSTEM,TOKEN_2022],b'',2),
        *ata_children(curve_ata,curve,3),
        ix(SYSTEM,[payer,mint],struct.pack('<IQ',2,934720),2),
        ix(TOKEN_2022,[mint,authority,mint,authority],bytes.fromhex('d2e11ea258b84d8d')+
            b''.join(string(text[f]) for f in ('name','symbol','uri')),2),
        ix(TOKEN_2022,[mint,authority],bytes.fromhex('d7e4a6e45464567b')+bytes(32),2),
        ix(TOKEN_2022,[mint,curve_ata,authority],b'\7'+struct.pack('<Q',SUPPLY),2),
        ix(TOKEN_2022,[mint,authority],b'\6\0\0',2),
        ix(PUMP,event['accounts'],unbase58(event['data']),2)]
    record = {'version':'legacy','slot':454321337,'transaction':{'signatures':['1'*64]*2,
        'message':{'accountKeys':keys,'header':{'numRequiredSignatures':2,
            'numReadonlySignedAccounts':0,'numReadonlyUnsignedAccounts':0},
            'instructions':[ix(PUMP,accounts,unbase58(create['data'])),
                ix(ATA,[payer,holder,payer,mint,SYSTEM,TOKEN_2022],b'\1'),
                ix(PUMP,buy['accounts'],unbase58(buy['data']))]}},
        'meta':{'err':None,'innerInstructions':[{'index':0,'instructions':children},
             {'index':1,'instructions':ata_children(holder,payer,2)},
             {'index':2,'instructions':[ix(TOKEN_2022,[curve_ata,mint,holder,curve],
                b'\x0c'+struct.pack('<Q',amount)+b'\6',2)]}]}}
    metadata = bytes(32)+key(mint)+b''.join(string(text[f]) for f in ('name','symbol','uri'))+bytes(4)
    mint_state = (bytes(36)+struct.pack('<QBB',SUPPLY,6,1)+bytes(36)+bytes(83)+b'\1'
                  +struct.pack('<HH',18,64)+bytes(32)+key(mint)
                  +struct.pack('<HH',19,len(metadata))+metadata)

    def token_state(owner, amount):
        return (key(mint)+key(owner)+struct.pack('<Q',amount)+bytes(36)+b'\1'+bytes(12)
                +bytes(8)+bytes(36)+b'\2'+struct.pack('<HH',7,0))

    endpoints = [{'address':address,'program_owner':TOKEN_2022,'executable':False,
        'slot':454321337,'transaction_signature':'1'*64,'raw':raw}
        for address,raw in ((mint,mint_state),(curve_ata,token_state(curve,SUPPLY-amount)),
                            (holder,token_state(payer,amount)))]
    context = {'slot':454321337,'signature':'1'*64,'provenance':'SYNTHETIC_COMPLETION_NOT_CHAIN_EVIDENCE'}
    return record,endpoints,context


def report(value):
    record,endpoints,context = value
    return report_create_v2_lifecycle(record,endpoint_states=endpoints,source_context=context)


def children(value, outer=0):
    return value[0]['meta']['innerInstructions'][outer]['instructions']


def data(ix, raw):
    ix['data']=base58(raw) if raw else ''


class CreateV2LifecycleTests(unittest.TestCase):
    def unapproved(self, result):
        for flag in ('authenticated_lifecycle_accepted','lifecycle_verified','snapshot_authenticated',
                     'effect_order_verified','individual_cpi_success_verified','signer_authorization_verified',
                     'program_slot_semantics_verified','finality_verified','ownership_approved','eligible_for_trading'):
            self.assertIs(result[flag],False,flag)
        self.assertEqual(len(result['graduation_evidence']),len(GRADUATION_EVIDENCE))
        self.assertTrue(all(e['verified'] is False for e in result['graduation_evidence']))
        self.assertTrue(all(i['cpi_success']=='unknown' and i['effect_verified'] is False for i in result['inventory']))

    def reject(self, value, code=None):
        original=copy.deepcopy(value)
        result=report(value)
        self.assertEqual(value,original)
        self.assertFalse(result['supported_sequence_agreement'])
        self.unapproved(result)
        if code:self.assertIn(code,result['errors'])
        return result

    def test_synthetic_sequence_agreement_never_lifecycle_acceptance(self):
        value=synthetic(); original=copy.deepcopy(value)
        result=report(value)
        self.assertEqual(value,original)
        self.assertEqual(result['errors'],[])
        self.assertTrue(result['supported_sequence_agreement'])
        self.assertTrue(result['observed_inventory_agreement'])
        self.assertEqual(len(result['inventory']),23)
        self.assertEqual(len(result['bindings']),18)
        self.assertEqual(len(result['endpoint_states']),3)
        self.assertTrue(result['endpoint_amount_comparison']['amounts_agree'])
        self.assertFalse(result['endpoint_amount_comparison']['prestate_inferred'])
        self.assertIn('CREATE_OPTIONAL_SUFFIX_AND_EOF_UNRESOLVED',result['unknowns'])
        self.assertEqual(result['dependency']['exact_head'],PR48_HEAD)
        self.unapproved(result)
        json.dumps(result)

    def test_saved_launch_unmodified_has_explicit_missing_dependencies(self):
        original=(ROOT/'fixtures/mainnet-launch.json').read_bytes()
        self.assertEqual(hashlib.sha256(original).hexdigest(),FIXTURE_SHA)
        reference=public_reference(); record=reference['transaction'];before=copy.deepcopy(record)
        result=report_create_v2_lifecycle(record,source_context={
            'slot':reference['slot'],'signature':reference['signature'],'provenance':'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE'})
        self.assertEqual(record,before)
        self.assertFalse(result['supported_sequence_agreement'])
        self.assertIn('RAW_STATE_MISSING',result['unknowns'])
        self.assertIn('RAW_INSTRUCTION_UNAVAILABLE',result['unknowns'])
        self.assertIn('MESSAGE_HEADER_MISSING',result['unknowns'])
        self.assertEqual(len(result['inventory']),33)
        tokens=[i for i in result['inventory'] if i['syntax'] is not None]
        self.assertEqual(len(tokens),13)
        self.assertTrue(all(i['syntax']['raw_operation'] is None for i in tokens))
        pointer=next(i for i in tokens if i['instruction']['witness']['parsed']['type']=='initializeMetadataPointer')
        self.assertFalse(pointer['syntax']['parsed_field_presence']['authority'])
        self.assertEqual(result['endpoint_states'],[])
        self.unapproved(result)
        self.assertEqual((ROOT/'fixtures/mainnet-launch.json').read_bytes(),original)

    def test_every_create_account_role_swap_or_extra_fails(self):
        for index in range(16):
            value=synthetic();accounts=value[0]['transaction']['message']['instructions'][0]['accounts']
            accounts[index]=accounts[(index+1)%16]
            with self.subTest(index=index):self.reject(value)
        value=synthetic();value[0]['transaction']['message']['instructions'][0]['accounts'].append(0)
        self.reject(value,'CREATE_V2_ACCOUNT_COUNT_UNSUPPORTED')

    def test_legacy_ata_seed_is_rejected(self):
        value=synthetic();keys=value[0]['transaction']['message']['accountKeys'];mint=keys[1]
        curve=keys[value[0]['transaction']['message']['instructions'][0]['accounts'][2]]
        wrong=str(Pubkey.find_program_address([key(curve),key(TOKEN_PROGRAM),key(mint)],Pubkey.from_string(ATA))[0])
        curve_index=value[0]['transaction']['message']['instructions'][0]['accounts'][3]
        keys[curve_index]=wrong
        self.reject(value,'CREATE_PDA_BINDING_MISMATCH:associated_bonding_curve')

    def test_unconstrained_mayhem_vault_is_not_invented_ata(self):
        result=report(synthetic())
        vault=next(a for a in result['opaque_accounts'] if a['name']=='mayhem_token_vault')
        self.assertEqual(vault['reason'],'UNCONSTRAINED_WRITABLE_VAULT')
        self.assertFalse(vault['state_and_effects_verified'])
        self.assertIn('OPAQUE_MAYHEM_ACCOUNT_EFFECTS',result['unknowns'])
        self.unapproved(result)

    def test_direct_parent_cannot_be_replaced_by_outer_prefix(self):
        for index in (5,6,7,8):
            value=synthetic();children(value)[index]['stackHeight']=2
            self.reject(value,'ATA_CREATION_CHILDREN_MISSING_EXTRA_OR_EXISTING_CASE_UNPROVED')
        value=synthetic();children(value)[10]['stackHeight']=3
        self.reject(value)

    def test_missing_depth_impossible_jump_unknown_not_guessed(self):
        value=synthetic();del children(value)[7]['stackHeight']
        result=self.reject(value)
        self.assertIn('CALLER_STACK_HEIGHT_UNAVAILABLE',result['unknowns'])
        self.assertIsNone(result['inventory'][8]['instruction']['direct_parent_path'])
        value=synthetic();children(value)[0]['stackHeight']=4
        self.reject(value,'IMPOSSIBLE_STACK_JUMP')

    def test_missing_raw_pointer_does_not_use_parsed_none(self):
        value=synthetic();ix=children(value)[1];del ix['data']
        ix['parsed']={'type':'initializeMetadataPointer','info':{'authority':None,'metadataAddress':value[0]['transaction']['message']['accountKeys'][1]}}
        result=self.reject(value,'INITIALIZEMETADATAPOINTER_MISSING_OR_DUPLICATE')
        syntax=next(i['syntax'] for i in result['inventory'] if i['instruction']['instruction_path']=='0.1')
        self.assertIsNone(syntax['raw_operation'])
        self.assertTrue(syntax['parsed_field_presence']['authority'])

    def test_raw_and_parsed_together_remain_unresolved(self):
        value=synthetic();children(value)[2]['parsed']={'type':'initializeMint2','info':{'freezeAuthority':None,'decimals':9}}
        result=self.reject(value)
        self.assertIn('PARSED_RAW_AGREEMENT_UNPROVED:0.2',result['unknowns'])
        self.assertIn('RAW_PARSED_COMPARISON_UNSUPPORTED',result['unknowns'])

    def test_pointer_active_or_redirected_and_freeze_some(self):
        for raw in (bytes([39,0])+key(SYSTEM)+key(SYSTEM),
                    bytes([39,0])+bytes(32)+key(PUMP),
                    bytes([39,0])+key(PUMP)+key(synthetic()[0]['transaction']['message']['accountKeys'][1])):
            value=synthetic();data(children(value)[1],raw)
            self.reject(value,'POINTER_ROLE_OR_CALLER_MISMATCH')
        value=synthetic();raw=unbase58(children(value)[2]['data']);data(children(value)[2],raw[:-1]+b'\1'+key(SYSTEM))
        self.reject(value,'MINT_INIT_ROLE_OR_CALLER_MISMATCH')

    def test_issuance_destination_amount_authority_roles(self):
        for account_position in (0,1,2):
            value=synthetic();children(value)[12]['accounts'][account_position]=0
            self.reject(value,'ISSUANCE_ROLE_AMOUNT_OR_CALLER_MISMATCH')
        value=synthetic();data(children(value)[12],b'\7'+struct.pack('<Q',SUPPLY-1))
        self.reject(value,'ISSUANCE_ROLE_AMOUNT_OR_CALLER_MISMATCH')

    def test_missing_duplicate_reorder_mint_revocation(self):
        value=synthetic();del children(value)[13]
        self.reject(value,'SETAUTHORITY_MISSING_OR_DUPLICATE')
        value=synthetic();children(value).insert(14,copy.deepcopy(children(value)[13]))
        self.reject(value,'SETAUTHORITY_MISSING_OR_DUPLICATE')
        value=synthetic();rows=children(value);rows[12],rows[13]=rows[13],rows[12]
        self.reject(value,'BIRTH_INVOCATION_PREORDER_MISMATCH')
        value=synthetic();data(children(value)[13],b'\6\0\1'+key(PUMP))
        self.reject(value,'MINT_REVOCATION_ROLE_OR_CALLER_MISMATCH')

    def test_metadata_revocation_authority_swap_redirect_restore(self):
        for position in (0,1):
            value=synthetic();children(value)[11]['accounts'][position]=0
            self.reject(value,'METADATA_REVOCATION_ROLE_OR_CALLER_MISMATCH')
        value=synthetic();data(children(value)[11],bytes.fromhex('d7e4a6e45464567b')+key(PUMP))
        self.reject(value,'METADATA_REVOCATION_ROLE_OR_CALLER_MISMATCH')
        value=synthetic();children(value).insert(12,copy.deepcopy(children(value)[11]))
        self.reject(value,'UPDATETOKENMETADATAAUTHORITY_MISSING_OR_DUPLICATE')

    def test_transient_control_witnesses_retained_even_after_bad_create(self):
        for raw in (b'\4'+struct.pack('<Q',0),b'\5',b'\6\2\1'+key(PUMP),b'\6\3\0',b'\x0a',b'\x0b',b'\x0d'+bytes(9)):
            value=synthetic();ix=copy.deepcopy(children(value)[13]);data(ix,raw);children(value).append(ix)
            result=self.reject(value)
            self.assertTrue(result['adverse_control_witnesses'])
            self.assertEqual(result['adverse_control_witnesses'][-1]['instruction']['raw_hex'],raw.hex())
            value[0]['transaction']['message']['instructions'][0]['accounts'].append(0)
            result=self.reject(value)
            self.assertTrue(any(w['instruction']['raw_hex']==raw.hex() for w in result['adverse_control_witnesses']))

    def test_extra_system_transfer_never_ignored_as_native_sol(self):
        for target in (0,1):
            value=synthetic();ix=copy.deepcopy(children(value)[9]);ix['accounts']=[0,target]
            children(value,2).append(ix)
            result=self.reject(value)
            self.assertIn('UNMATCHED_OBSERVED_INSTRUCTION:2.1',result['errors'])
            self.assertEqual(result['inventory'][-1]['instruction']['program'],SYSTEM)

    def test_extra_unknown_program_cpi_no_ignored_shortcut(self):
        value=synthetic();ix=copy.deepcopy(children(value)[13]);ix['programIdIndex']=0;data(ix,b'unknown');children(value).append(ix)
        result=self.reject(value)
        self.assertIn('UNMATCHED_OBSERVED_INSTRUCTION:0.15',result['errors'])
        self.assertFalse(result['observed_inventory_agreement'])

    def test_extra_signers_and_readonly_privileges(self):
        for field,replacement in (('numRequiredSignatures',3),('numRequiredSignatures',1),
                                   ('numRequiredSignatures',True),('numReadonlySignedAccounts',1),
                                   ('numReadonlyUnsignedAccounts',999),('numReadonlyUnsignedAccounts',30)):
            value=synthetic();value[0]['transaction']['message']['header'][field]=replacement
            self.reject(value)
        value=synthetic();value[0]['transaction']['signatures']=['1'*64]
        self.reject(value,'SIGNATURE_ENVELOPE_INVALID')

    def test_missing_header_cannot_gain_agreement_from_flags(self):
        value=synthetic();del value[0]['transaction']['message']['header']
        value[0]['signer_authorization_verified']=True
        result=self.reject(value)
        self.assertIn('MESSAGE_HEADER_MISSING',result['unknowns'])

    def test_partial_create_suffix_eof_never_decoded_as_false(self):
        for suffix in (b'',b'\0',bytes(9),bytes(10),bytes(11),b'\xff',b'unsupported'):
            value=synthetic();ix=value[0]['transaction']['message']['instructions'][0]
            raw=unbase58(ix['data']);reader=BorshReader(raw[8:],{})
            for t in ('string','string','string','pubkey','bool'):reader.read(t)
            data(ix,raw[:8+reader.pos]+suffix)
            result=report(value);args=result['create_arguments']
            self.assertEqual(args['suffix_raw_hex'],suffix.hex())
            self.assertFalse(args['schema_complete'])
            self.assertEqual(args['observed_suffix_bytes_agree'],suffix==bytes(9))
            if suffix != bytes(9):self.assertFalse(result['supported_sequence_agreement'])
            self.assertEqual(args['optional_fields']['is_holder_reward']['presence'],'unresolved')
            self.assertIsNone(args['optional_fields']['is_cashback_enabled']['value'])
            self.unapproved(result)

    def test_mandatory_prefix_truncation_invalid_bool_and_mayhem(self):
        value=synthetic();ix=value[0]['transaction']['message']['instructions'][0];raw=unbase58(ix['data'])
        for replacement in (raw[:8],raw[:13],raw[:100]):
            v=synthetic();data(v[0]['transaction']['message']['instructions'][0],replacement);self.reject(v)
        reader=BorshReader(raw[8:],{})
        for t in ('string','string','string','pubkey'):reader.read(t)
        pos=8+reader.pos
        for flag in (1,2):
            v=synthetic();data(v[0]['transaction']['message']['instructions'][0],raw[:pos]+bytes([flag])+raw[pos+1:]);self.reject(v)

    def test_existing_idempotent_ata_no_prestate_inference(self):
        value=synthetic();value[0]['meta']['innerInstructions'][1]['instructions']=[]
        self.reject(value,'ATA_CREATION_CHILDREN_MISSING_EXTRA_OR_EXISTING_CASE_UNPROVED')

    def test_ata_extra_child_wrong_owner_allocation_or_program(self):
        value=synthetic();children(value,1).append(copy.deepcopy(children(value,1)[3]))
        self.reject(value,'ATA_CREATION_CHILDREN_MISSING_EXTRA_OR_EXISTING_CASE_UNPROVED')
        value=synthetic();data(children(value,1)[3],b'\x12'+key(PUMP))
        self.reject(value,'ATA_INIT_ROLE_MISMATCH')
        value=synthetic();raw=bytearray(unbase58(children(value,1)[1]['data']));raw[12:20]=struct.pack('<Q',165);data(children(value,1)[1],bytes(raw))
        self.reject(value,'ALLOCATION_ROLE_OR_LAYOUT_MISMATCH')

    def test_distribution_redirect_wrong_authority_amount_or_decimals(self):
        for pos in range(4):
            value=synthetic();children(value,2)[0]['accounts'][pos]=0
            self.reject(value,'DISTRIBUTION_ROLE_AMOUNT_OR_DECIMALS_MISMATCH')
        for amount,decimals in ((0,6),(SUPPLY+1,6),(100,9),(100,6)):
            value=synthetic();data(children(value,2)[0],b'\x0c'+struct.pack('<Q',amount)+bytes([decimals]))
            self.reject(value)

    def test_distribution_under_ata_parent_not_pump(self):
        value=synthetic();ix=children(value,2).pop();children(value,1).append(ix)
        self.reject(value,'ATA_CREATION_CHILDREN_MISSING_EXTRA_OR_EXISTING_CASE_UNPROVED')

    def test_buy_exact_schema_extra_accounts_suffix_or_wrong_base_binding(self):
        for mutation in ('extra','suffix','mint','amount'):
            value=synthetic();ix=value[0]['transaction']['message']['instructions'][2]
            if mutation=='extra':ix['accounts'].append(0)
            elif mutation=='suffix':data(ix,unbase58(ix['data'])+b'\0')
            elif mutation=='mint':ix['accounts'][1]=0
            else:data(ix,unbase58(ix['data'])[:8]+struct.pack('<QQ',1,2))
            self.reject(value)

    def test_create_event_binding_complete_schema_required(self):
        value=synthetic();ix=children(value)[14];data(ix,unbase58(ix['data'])+b'\0')
        self.reject(value,'PUMP_INNER_EVENT_OR_OPERATION_UNRESOLVED')
        value=synthetic();del children(value)[14]
        self.reject(value,'CREATE_EVENT_MISSING_OR_DUPLICATE')

    def test_endpoint_missing_states_or_raw_never_inferred(self):
        value=synthetic();value=(value[0],None,value[2]);result=self.reject(value)
        self.assertIn('RAW_STATE_MISSING',result['unknowns'])
        value=synthetic();value[1].pop();result=self.reject(value)
        self.assertIn('RAW_STATE_MISSING_REQUIRED_ENDPOINTS',result['unknowns'])
        value=synthetic();value[1][0]['raw']=None;result=self.reject(value)
        self.assertTrue(any(u.startswith('RAW_STATE_MISSING:') for u in result['unknowns']))
        self.assertIsNone(result['endpoint_amount_comparison'])

    def test_independently_supplied_endpoint_wrong_mint_or_controls(self):
        for endpoint,offset,data_value in ((1,0,key(PUMP)),(1,72,struct.pack('<I',1)+key(PUMP)),
            (2,108,b'\2'),(2,129,struct.pack('<I',1)+key(PUMP)),(0,0,struct.pack('<I',1)+key(PUMP))):
            value=synthetic();raw=bytearray(value[1][endpoint]['raw']);raw[offset:offset+len(data_value)]=data_value
            value[1][endpoint]['raw']=bytes(raw)
            self.reject(value,'ENDPOINT_STATE_PROFILE_MISMATCH:'+value[1][endpoint]['address'])

    def test_endpoint_amounts_cannot_fabricate_distribution(self):
        for index in (1,2):
            value=synthetic();raw=bytearray(value[1][index]['raw']);raw[64:72]=struct.pack('<Q',0)
            value[1][index]['raw']=bytes(raw)
            self.reject(value,'ENDPOINT_AMOUNT_OR_SUPPLY_DISAGREEMENT')

    def test_endpoint_duplicates_unbound_and_malformed_inventory(self):
        value=synthetic();value[1].append(copy.deepcopy(value[1][0]));self.reject(value,'DUPLICATE_ENDPOINT_ADDRESS')
        value=synthetic();value[1][0]['address']=PUMP;self.reject(value,'ENDPOINT_ADDRESS_UNBOUND')
        value=synthetic();value[1][0]=None;self.reject(value,'ENDPOINT_ENVELOPE_MALFORMED')
        value=synthetic();self.reject((value[0],{},value[2]),'ENDPOINT_ENVELOPE_SHAPE_OR_BUDGET')

    def test_endpoint_slot_signature_stale_or_missing_not_trusted(self):
        for field,replacement,code in (('slot',454321338,'ENDPOINT_SOURCE_SLOT_MISMATCH'),
            ('slot',True,'ENDPOINT_SLOT_INVALID'),('transaction_signature',base58(bytes([1])*64),'ENDPOINT_TRANSACTION_BINDING_MISMATCH')):
            value=synthetic();value[1][0][field]=replacement;self.reject(value,code)
        value=synthetic();del value[1][0]['slot'];self.reject(value)
        value=synthetic();del value[1][0]['transaction_signature'];self.reject(value)
        value=synthetic();value[2]['signature']=base58(bytes([1])*64);self.reject(value,'SOURCE_SIGNATURE_DISAGREEMENT')

    def test_record_slot_mismatch_coordinator_reproduction(self):
        value=synthetic();value[0]['slot']=1
        result=self.reject(value,'RECORD_SOURCE_SLOT_MISMATCH')
        self.assertIn('ENDPOINT_RECORD_SLOT_MISMATCH',result['errors'])

    def test_context_signature_missing_coordinator_reproduction(self):
        for explicit_none in (False,True):
            value=synthetic()
            if explicit_none:value[2]['signature']=None
            else:value[2].pop('signature')
            result=self.reject(value)
            self.assertIn('SOURCE_SIGNATURE_MISSING',result['unknowns'])

    def test_all_slots_overflow_coordinator_reproduction(self):
        value=synthetic();value[0]['slot']=value[2]['slot']=2**64
        for state in value[1]:state['slot']=2**64
        result=self.reject(value,'RECORD_SLOT_INVALID')
        self.assertIn('SOURCE_SLOT_INVALID',result['errors'])
        self.assertIn('ENDPOINT_SLOT_INVALID',result['errors'])

    def test_each_slot_witness_strict_u64_not_bool_float_string_or_overflow(self):
        for location,code in ((0,'RECORD_SLOT_INVALID'),(2,'SOURCE_SLOT_INVALID'),(1,'ENDPOINT_SLOT_INVALID')):
            for invalid in (-1,True,False,1.0,'454321337',2**64,2**128):
                value=synthetic()
                if location==1:value[1][0]['slot']=invalid
                else:value[location]['slot']=invalid
                with self.subTest(location=location,invalid=invalid):self.reject(value,code)

    def test_slot_u64_boundaries_agree_only_with_all_explicit_bindings(self):
        for slot in (0,2**64-1):
            value=synthetic();value[0]['slot']=value[2]['slot']=slot
            for state in value[1]:state['slot']=slot
            result=report(value)
            self.assertTrue(result['supported_sequence_agreement'])
            self.assertEqual(result['errors'],[])
            self.unapproved(result)

    def test_missing_record_slot_is_unknown_not_filled_from_context(self):
        value=synthetic();value[0].pop('slot')
        result=self.reject(value)
        self.assertIn('RECORD_SLOT_MISSING',result['unknowns'])

    def test_signature_witnesses_invalid_or_cross_binding_conflicts(self):
        for invalid in ('','0'*64,'1'*63,5,{},True):
            value=synthetic();value[2]['signature']=invalid
            self.reject(value,'SOURCE_SIGNATURE_INVALID')
            value=synthetic();value[1][0]['transaction_signature']=invalid
            self.reject(value,'ENDPOINT_TRANSACTION_BINDING_INVALID')
        value=synthetic();value[0]['slot']=value[2]['slot']=1
        result=self.reject(value,'ENDPOINT_RECORD_SLOT_MISMATCH')
        self.assertIn('ENDPOINT_SOURCE_SLOT_MISMATCH',result['errors'])

    def test_forged_success_finality_and_endpoint_trust_flags_ignored(self):
        value=synthetic();value[0]['meta'].update(cpi_success_verified=True,effect_order_verified=True)
        value[0]['lifecycle_verified']=True;value[2].update(finality_verified=True,authenticated=True)
        for state in value[1]:state.update(authenticated=True,eligible=True,finalized=True)
        result=report(value)
        self.assertTrue(result['supported_sequence_agreement'])
        self.unapproved(result)
        self.assertTrue(all(not e['capture_authenticated'] for e in result['endpoint_states']))

    def test_failed_transaction_or_ambiguous_trace_no_agreement(self):
        value=synthetic();value[0]['meta']['err']={'InstructionError':[2,'Custom']}
        result=self.reject(value)
        self.assertIn('FAILED_TRANSACTION_EXECUTED_OUTER_PREFIX_UNKNOWN',result['unknowns'])
        value=synthetic();value[0]['meta']['innerInstructions'][1]['index']=0
        self.reject(value,'DUPLICATE_INNER_GROUP_INDEX')

    def test_malformed_records_and_signature_shape_return_diagnostics(self):
        for record in (None,[],{}, {'transaction':None}, {'transaction':{'signatures':5}}):
            result=report_create_v2_lifecycle(record,endpoint_states=synthetic()[1])
            self.assertFalse(result['supported_sequence_agreement'])
            self.unapproved(result)
        value=synthetic();value[0]['transaction']['signatures']=5
        self.reject(value,'SIGNATURE_ENVELOPE_INVALID')

    def test_unsupported_extension_control_is_retained_as_unresolved(self):
        value=synthetic();ix=copy.deepcopy(children(value)[13]);data(ix,b'\x27\1'+bytes(32));children(value).append(ix)
        result=self.reject(value)
        self.assertTrue(any(w['instruction']['raw_hex']==(b'\x27\1'+bytes(32)).hex()
                            for w in result['adverse_control_witnesses']))

    def test_no_production_import_and_no_parser_replication(self):
        source=(ROOT/'research/create_v2_lifecycle.py').read_text()
        self.assertNotIn('urllib',source)
        self.assertNotIn('requests',source)
        self.assertIn('normalize_token2022_instruction',source)
        self.assertIn('decode_token2022_state',source)
        for path in (ROOT/'desk').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node,ast.ImportFrom):self.assertNotIn('create_v2_lifecycle',node.module or '')
                if isinstance(node,ast.Import):self.assertFalse(any('create_v2_lifecycle' in n.name for n in node.names))


if __name__ == '__main__':
    unittest.main()
