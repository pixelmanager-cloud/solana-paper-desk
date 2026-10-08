"""Verify saved public signatures; source-built legacy shapes never sign.

No private keys, keypairs, signer creation, RPC or cryptography mocking. Public
v0 witnesses plus byte-exact legacy format checks and adversarial mutations.
"""
import ast
import base64
import copy
import hashlib
import json
from pathlib import Path
import unittest

from solders.message import Message, from_bytes_versioned, to_bytes_versioned
from solders.signature import Signature

from desk.security import base58
from research.signed_message_witness import validate_signed_message_witness, MAX_PACKET_BYTES

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT/'fixtures/mainnet-launch-raw.json'
HISTORICAL = ROOT/'fixtures/legacy_pump_reference/create_buy.json'
KEY = base58(bytes([7])*32)
BLOCKHASH = base58(bytes([8])*32)


def sample():
    return json.loads(PUBLIC.read_bytes())['result']


def legacy():
    # Source-built shape with an existing public signature that cannot verify
    # this different message. This deliberately creates no positive witness.
    original = sample()
    m = original['transaction']['message']
    return {'version':'legacy','transaction':{'signatures':[original['transaction']['signatures'][0]],
        'message':{'header':{'numRequiredSignatures':1,'numReadonlySignedAccounts':0,'numReadonlyUnsignedAccounts':1},
            'accountKeys':[m['accountKeys'][0],KEY], 'recentBlockhash':BLOCKHASH,
            'instructions':[{'programIdIndex':1,'accounts':[0,0],'data':''}]}}}


def shortvec(n):
    out=[]
    while True:
        part=n&127;n>>=7;out.append(part|(128 if n else 0))
        if not n:return bytes(out)


class SignedMessageWitnessTests(unittest.TestCase):
    def no_approval(self, result):
        for f in ('metadata_authenticated','finality_verified','alt_resolved_values_authenticated',
            'execution_success_verified','signer_authorization_verified','authenticated_lifecycle_accepted',
            'lifecycle_verified','ownership_approved','eligible_for_trading'):
            self.assertIs(result[f],False,f)
        for w in result['signer_witnesses']:
            self.assertIs(w['authorization_for_execution_verified'],False)

    def run_record(self, source):
        before=copy.deepcopy(source)
        out=validate_signed_message_witness(source)
        self.assertEqual(source,before)
        self.no_approval(out)
        return out

    def reject(self, source, code=None):
        out=self.run_record(source)
        self.assertFalse(out['message_authenticated'])
        self.assertEqual(out['status'],'rejected')
        if code:self.assertIn(code,out['errors'])
        return out

    def test_public_raw_all_signatures_exact_source_and_signed_bytes(self):
        saved=PUBLIC.read_bytes();out=self.run_record(saved)
        self.assertTrue(out['message_authenticated'])
        self.assertTrue(out['message_schema_complete'])
        self.assertTrue(out['signature_checks_complete'])
        self.assertEqual(out['errors'],[])
        self.assertEqual(out['source_bytes_sha256'],'27d481575656d2b254a63bf699a32ce3dd599ce3b55fc8b04da2aa79dd356520')
        self.assertEqual(out['source_sha256'],'2b49cd4c6fe24a121cb59894c8a6fe0e82e4cf51db28e88151612984ccc9a0c7')
        self.assertEqual(out['message_sha256'],'e6643b57707da44b5e9ba56ea16025d26ffc0bf11b05deccd3223fd25aad4443')
        self.assertEqual(out['message_size_bytes'],931)
        self.assertEqual(out['transaction_wire_size_bytes'],1060)
        self.assertEqual(out['declared_lookup_slots'],16)
        self.assertEqual([w['signature_verified'] for w in out['signer_witnesses']],[True,True])
        self.assertEqual([w['static_account_index'] for w in out['signer_witnesses']],[0,1])
        self.assertEqual(PUBLIC.read_bytes(),saved)
        raw=base64.b64decode(out['message_bytes_base64']);self.assertEqual(raw[0],128)
        self.assertEqual(to_bytes_versioned(from_bytes_versioned(raw)),raw)

    def test_second_public_v0_legacy_pump_reference_not_legacy_wire_or_finality(self):
        saved=HISTORICAL.read_bytes();out=self.run_record(saved)
        self.assertEqual(out['version'],0)
        self.assertTrue(out['message_authenticated'])
        self.assertEqual(out['source_bytes_sha256'],'89dcc14ecad1432598e4937da2dfa9955af8637a5ae373418e9631c174991749')
        self.assertEqual(out['message_sha256'],'05b53d27a69f58d54620c13b68cd8b9a96d1160cd39a31bc56872b2b2b945560')
        self.assertEqual(out['transaction_wire_size_bytes'],945)
        self.assertEqual(HISTORICAL.read_bytes(),saved)

    def test_exact_legacy_wire_encoding_without_version_prefix_or_signing(self):
        r=legacy();out=self.reject(r,'SIGNATURE_VERIFICATION_FAILED:0')
        self.assertTrue(out['message_schema_complete'])
        self.assertTrue(out['signature_checks_complete'])
        from solders.pubkey import Pubkey
        m=r['transaction']['message']
        expected=(b'\1\0\1'+shortvec(2)+b''.join(bytes(Pubkey.from_string(k)) for k in m['accountKeys'])
            +bytes(Pubkey.from_string(BLOCKHASH))+shortvec(1)+b'\1'+shortvec(2)+b'\0\0'+shortvec(0))
        raw=base64.b64decode(out['message_bytes_base64'])
        self.assertEqual(raw,expected)
        self.assertIsInstance(from_bytes_versioned(raw),Message)
        self.assertEqual(bytes(Message.from_bytes(raw)),expected)
        self.assertFalse(out['signer_witnesses'][0]['signature_verified'])

    def test_legacy_optional_empty_lookup_and_empty_instruction_list(self):
        r=legacy();a=self.reject(r)
        r['transaction']['message']['addressTableLookups']=[]
        b=self.reject(r);self.assertEqual(a['message_sha256'],b['message_sha256'])
        r['transaction']['message']['instructions']=[]
        out=self.reject(r);self.assertTrue(out['message_schema_complete'])

    def test_individual_signature_tamper_checks_all_signers_without_short_circuit(self):
        for index in (0,1):
            r=sample();s=bytearray(bytes(Signature.from_string(r['transaction']['signatures'][index])));s[0]^=1
            r['transaction']['signatures'][index]=base58(bytes(s))
            out=self.reject(r,'SIGNATURE_VERIFICATION_FAILED:'+str(index))
            self.assertTrue(out['signature_checks_complete'])
            self.assertEqual(len(out['signer_witnesses']),2)
            self.assertEqual(out['signer_witnesses'][1-index]['signature_verified'],True)
            self.assertFalse(out['signer_witnesses'][index]['signature_verified'])

    def test_signature_order_is_bound_to_header_signer_order(self):
        r=sample();r['transaction']['signatures'].reverse()
        out=self.reject(r)
        self.assertEqual(out['errors'],['SIGNATURE_VERIFICATION_FAILED:0','SIGNATURE_VERIFICATION_FAILED:1'])

    def test_message_bytes_tamper_invalidates_every_signature(self):
        for mode in ('data','account','program','blockhash','static_order','header','lookup_index','lookup_table','lookup_order'):
            r=sample();m=r['transaction']['message']
            if mode=='data':m['instructions'][0]['data']=base58(b'\2\0\0\0\0')
            elif mode=='account':m['instructions'][2]['accounts'].reverse()
            elif mode=='program':m['instructions'][0]['programIdIndex']=1
            elif mode=='blockhash':m['recentBlockhash']=BLOCKHASH
            elif mode=='static_order':m['accountKeys'][2:4]=reversed(m['accountKeys'][2:4])
            elif mode=='header':m['header']['numReadonlyUnsignedAccounts']-=1
            elif mode=='lookup_index':m['addressTableLookups'][0]['readonlyIndexes'][0]=255
            elif mode=='lookup_table':m['addressTableLookups'][0]['accountKey']=KEY
            else:m['addressTableLookups'][0]['readonlyIndexes'].reverse()
            out=self.reject(r)
            self.assertTrue(out['message_schema_complete'],mode)
            self.assertTrue(out['signature_checks_complete'],mode)
            self.assertFalse(any(w['signature_verified'] for w in out['signer_witnesses']),mode)

    def test_metadata_request_slot_finality_and_loaded_values_are_unsigned(self):
        original=self.run_record(sample())
        for mode in ('loaded_missing','loaded_malformed','loaded_duplicate','entire_meta','slot','fake_approval','stack_height','request'):
            r=sample()
            if mode=='loaded_missing':del r['meta']['loadedAddresses']
            elif mode=='loaded_malformed':r['meta']['loadedAddresses']='untrusted garbage'
            elif mode=='loaded_duplicate':r['meta']['loadedAddresses']={'writable':[KEY]*5,'readonly':[KEY]*11}
            elif mode=='entire_meta':r['meta']={'err':'failed','fee':1.5,'fake_finalized':True}
            elif mode=='slot':r['slot']=1;r['blockTime']=0;r['transactionIndex']=999
            elif mode=='fake_approval':r.update(ownership_approved=True,eligible_for_trading=True)
            elif mode=='stack_height':r['transaction']['message']['instructions'][0]['stackHeight']=999
            else:r={'kind':'rpc_response_v1','method':'getTransaction','params':['different-signature',{'commitment':'processed'}],'result':r}
            out=self.run_record(r)
            self.assertTrue(out['message_authenticated'],mode)
            self.assertEqual(out['message_sha256'],original['message_sha256'])
            self.assertNotEqual(out['source_sha256'],original['source_sha256'])

    def test_meta_and_request_absent_still_verify_only_signed_message(self):
        r=sample();r={k:v for k,v in r.items() if k in ('transaction','version')}
        self.assertTrue(self.run_record(r)['message_authenticated'])

    def test_signature_missing_extra_duplicates_malformed_and_zero(self):
        for values,code in (([], 'SIGNATURE_COUNT_MISMATCH'),
            ([sample()['transaction']['signatures'][0]]*2,'DUPLICATE_SIGNATURE'),
            ([sample()['transaction']['signatures'][0]]*3,'SIGNATURE_COUNT_MISMATCH')):
            r=sample();r['transaction']['signatures']=values;self.reject(r,code)
        for bad in (None,True,'','1'*63,'1'*65,'0'*88,'1'*89):
            r=sample();r['transaction']['signatures'][0]=bad
            self.reject(r,'SIGNATURE_ENCODING_OR_LENGTH_INVALID')
        r=sample();r['transaction']['signatures'][0]='1'*64
        self.reject(r,'SIGNATURE_VERIFICATION_FAILED:0')

    def test_version_explicit_legacy_or_exact_integer_zero_only(self):
        for version in (None,False,True,0.0,1,-1,'0','Legacy',{'version':0}):
            r=sample();r['version']=version;self.reject(r,'VERSION_MISSING_OR_UNSUPPORTED')
        r=sample();del r['version'];self.reject(r,'VERSION_MISSING_OR_UNSUPPORTED')
        r=sample();r['version']='legacy';self.reject(r,'LOOKUP_SHAPE_VERSION_OR_BOUND')

    def test_header_strict_fields_counts_and_writable_payer(self):
        for field in ('numRequiredSignatures','numReadonlySignedAccounts','numReadonlyUnsignedAccounts'):
            for bad in (True,False,1.0,'1',None,-1,256):
                r=sample();r['transaction']['message']['header'][field]=bad;self.reject(r,'HEADER_INVALID')
        for values in ((0,0,4),(19,0,0),(2,2,4),(2,0,17)):
            r=sample();h=r['transaction']['message']['header']
            h.update(numRequiredSignatures=values[0],numReadonlySignedAccounts=values[1],numReadonlyUnsignedAccounts=values[2])
            self.reject(r,'HEADER_COUNT_OR_FEE_PAYER_INVALID')
        for mode in ('missing','extra'):
            r=sample();h=r['transaction']['message']['header']
            if mode=='missing':del h['numReadonlySignedAccounts']
            else:h['ignored']=0
            self.reject(r,'HEADER_INVALID')

    def test_static_keys_no_duplicates_parsed_expansion_or_noncanonical_values(self):
        r=sample();m=r['transaction']['message'];m['accountKeys'][2]=m['accountKeys'][0]
        self.reject(r,'DUPLICATE_STATIC_KEY')
        for bad in ('bad','1'*31,'1'*33,'0'*44,None,{'pubkey':KEY}):
            r=sample();r['transaction']['message']['accountKeys'][0]=bad;self.reject(r,'STATIC_KEY_INVALID')
        r=sample();r['transaction']['message']['accountKeys']*=4
        self.reject(r,'STATIC_KEY_SHAPE_OR_BOUND')

    def test_missing_or_bad_blockhash(self):
        for bad in (None,0,'bad','1'*33,'0'*44):
            r=sample();r['transaction']['message']['recentBlockhash']=bad;self.reject(r,'RECENT_BLOCKHASH_INVALID')
        r=sample();del r['transaction']['message']['recentBlockhash'];self.reject(r,'COMPILED_MESSAGE_SHAPE_REQUIRED')

    def test_compiled_indices_range_boolean_float_and_dynamic_program_forbidden(self):
        for field in ('programIdIndex','accounts'):
            for value in (-1,True,1.0,'1',256,None):
                r=sample();ix=r['transaction']['message']['instructions'][2]
                if field=='accounts':ix[field][0]=value
                else:ix[field]=value
                self.reject(r,'INVALID_COMPILED_INDEX')
        r=sample();r['transaction']['message']['instructions'][2]['programIdIndex']=18
        self.reject(r,'INVALID_COMPILED_INDEX')
        r=sample();r['transaction']['message']['instructions'][2]['accounts'][0]=34
        self.reject(r,'INVALID_COMPILED_INDEX')
        r=sample();r['transaction']['message']['instructions'][2]['programIdIndex']=0
        self.reject(r,'PROGRAM_CANNOT_BE_FEE_PAYER')

    def test_lookup_descriptor_missing_empty_duplicate_and_typed_indices(self):
        for mode in ('missing','empty','duplicate_table','duplicate_index','alias_segments','bad_key','bad_index','extra'):
            r=sample();m=r['transaction']['message'];l=m['addressTableLookups'][0]
            if mode=='missing':del m['addressTableLookups']
            elif mode=='empty':l['writableIndexes']=[];l['readonlyIndexes']=[]
            elif mode=='duplicate_table':m['addressTableLookups'].append(copy.deepcopy(l))
            elif mode=='duplicate_index':l['writableIndexes'].append(l['writableIndexes'][0])
            elif mode=='alias_segments':l['readonlyIndexes'][0]=l['writableIndexes'][0]
            elif mode=='bad_key':l['accountKey']='bad'
            elif mode=='bad_index':l['readonlyIndexes'][0]=True
            else:l['extra']='ignored'
            self.reject(r)

    def test_lookup_total_account_bound_and_no_resolved_metadata_dependency(self):
        r=sample();l=r['transaction']['message']['addressTableLookups'][0]
        l['writableIndexes']=list(range(47));l['readonlyIndexes']=[]
        self.reject(r,'TOTAL_ACCOUNT_BOUND')
        r=sample();l=r['transaction']['message']['addressTableLookups'][0]
        r['transaction']['message']['addressTableLookups'].append({'accountKey':KEY,'writableIndexes':[7],'readonlyIndexes':[]})
        out=self.reject(r)  # index7 in a DIFFERENT table is legal syntax.
        self.assertTrue(out['message_schema_complete'])

    def test_raw_instruction_required_unknown_fields_and_no_parsed_repair(self):
        for mode in ('parsed','programId','missing_data','extra'):
            r=sample();ix=r['transaction']['message']['instructions'][2]
            if mode=='missing_data':del ix['data']
            else:ix[mode]={} if mode=='parsed' else KEY
            self.reject(r,'RAW_COMPILED_INSTRUCTION_REQUIRED')
        r=sample();r['transaction']['message']['unknown_signed_field']=True
        self.reject(r,'COMPILED_MESSAGE_SHAPE_REQUIRED')
        r=sample();r['transaction']['extra']='ignored'
        self.reject(r,'COMPILED_TRANSACTION_SHAPE_REQUIRED')

    def test_packet_data_and_instruction_bounds_no_partial_crypto_approval(self):
        r=legacy();r['transaction']['message']['instructions'][0]['data']=base58(bytes(1100))
        self.reject(r,'SIGNED_TRANSACTION_PACKET_BOUND')
        r=sample();r['transaction']['message']['instructions']*=13
        self.reject(r,'OUTER_INSTRUCTION_SHAPE_OR_BOUND')
        for bad in (None,True,'0', '1'*(2*MAX_PACKET_BYTES+1)):
            r=sample();r['transaction']['message']['instructions'][0]['data']=bad
            self.reject(r,'INSTRUCTION_DATA_INVALID_OR_BOUND')
        r=sample();r['transaction']['message']['instructions'][2]['accounts']=[0]*65
        self.reject(r,'INSTRUCTION_ACCOUNT_SHAPE_OR_BOUND')

    def test_source_bytes_identity_canonical_order_duplicate_fields_and_encoding(self):
        original=json.loads(PUBLIC.read_bytes())
        a=self.run_record(original)
        self.assertIsNone(a['source_bytes_sha256'])
        raw=json.dumps(original,sort_keys=True,indent=2).encode();b=self.run_record(raw)
        self.assertEqual(a['source_sha256'],b['source_sha256'])
        self.assertNotEqual(b['source_bytes_sha256'],hashlib.sha256(PUBLIC.read_bytes()).hexdigest())
        self.reject(b'{"version":0,"version":"legacy"}','DUPLICATE_JSON_FIELD')
        self.reject(b'\xff','SOURCE_INVALID_UTF8_JSON')
        self.reject(b'not json','SOURCE_INVALID_UTF8_JSON')
        self.reject(b'{"x":NaN}','SOURCE_NONFINITE_NUMBER')
        self.reject(b'{"x":"\\ud800"}','SOURCE_NON_UTF8_SCALAR')

    def test_source_bounds_cycles_nonfinite_non_json_and_unsupported_envelope(self):
        cases=[]
        r=sample();r['x']='x'*20001;cases.append((r,'SOURCE_STRING_BOUND'))
        r=sample();r['x']=['x'*10000]*110;cases.append((r,'SOURCE_BYTE_BOUND'))
        r=sample();r['x']=[None]*33000;cases.append((r,'SOURCE_NODE_OR_DEPTH_BOUND'))
        r=sample();x=None
        for _ in range(34):x=[x]
        r['x']=x;cases.append((r,'SOURCE_NODE_OR_DEPTH_BOUND'))
        for r,code in cases:self.reject(r,code)
        r=sample();r['x']=r;out=validate_signed_message_witness(r);self.no_approval(out);self.assertIn('SOURCE_CYCLE',out['errors'])
        for value in (float('nan'),float('inf'),b'bytes',set()):
            r=sample();r['x']=value;out=validate_signed_message_witness(r)
            self.assertFalse(out['message_authenticated']);self.assertTrue(out['errors'])
        self.reject(b' '*(1<<20)+b' ','SOURCE_BYTE_BOUND')
        self.reject({'jsonrpc':'2.0','result':sample()},'SAVED_GET_TRANSACTION_ENVELOPE_REQUIRED')
        self.reject(json.loads((ROOT/'fixtures/mainnet-launch.json').read_bytes()),'SAVED_GET_TRANSACTION_ENVELOPE_REQUIRED')

    def test_no_signer_creation_network_or_production_consumer(self):
        module=(ROOT/'research/signed_message_witness.py').read_text()
        self.assertNotIn('Keypair',module)
        tree=ast.parse(module)
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom):self.assertFalse(any(x in (node.module or '') for x in ('keypair','requests','urllib','socket')))
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute):self.assertNotIn(node.func.attr,('sign','sign_message','try_compile'))
        for path in (ROOT/'desk').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node,ast.ImportFrom):self.assertNotIn('signed_message_witness',node.module or '')
                if isinstance(node,ast.Import):self.assertFalse(any('signed_message_witness' in n.name for n in node.names))


if __name__=='__main__':
    unittest.main()
