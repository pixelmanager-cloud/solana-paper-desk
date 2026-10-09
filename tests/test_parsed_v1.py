"""Public retained parsed-v1 row; mutations are SYNTHETIC_TEST_ONLY."""
import copy
import json
from pathlib import Path
import unittest
from desk.decode import decode
from desk.parsed_v1 import validate
from desk.model import digest
from desk.security import base58
from desk.history import collect_history

FIXTURE=Path(__file__).parent/'fixtures'/'parsed_v1_public_row.json'
ROW_DIGEST='bdb59f95de4018ad7d01091d2d0aa5e7365254e2c1387401d7018394f146803d'


class ParsedV1Tests(unittest.TestCase):
    def setUp(self):
        self.row=json.loads(FIXTURE.read_text());self.message=self.row['transaction']['message']
        self.inner=self.row['meta']['innerInstructions'][0]['instructions']
    def reject(self, mutate):
        row=copy.deepcopy(self.row);mutate(row)
        with self.assertRaises((ValueError,KeyError,TypeError)):decode(row)
    def test_public_original_digest_event_and_accounting_without_authority(self):
        original=copy.deepcopy(self.row);self.assertEqual(digest(self.row),ROW_DIGEST)
        r=decode(self.row);self.assertEqual(self.row,original);self.assertEqual(r['payload_hash'],ROW_DIGEST)
        self.assertEqual(r['signature'],self.row['transaction']['signatures'][0]);self.assertEqual(r['slot'],454959916)
        self.assertEqual(r['status'],'OBSERVED');self.assertEqual(r['commitment'],'unverified')
        event=next(p for p in r['program_observations'] if p.get('name')=='BuyEvent')
        self.assertEqual(event['fields']['base_amount_out'],830011286755)
        self.assertEqual([v['delta_raw'] for v in r['token_deltas']],['398901433','98567','-830011286755',None])
        for limitation in ('HISTORY_INCOMPLETE','NOT_TRADE_EVIDENCE','UNDECODED_PROGRAM_INSTRUCTIONS','UNDECODED_TOKEN_INSTRUCTION',
                           'V1_JSONPARSED_STRUCTURE_NOT_AUTHENTICATED','RPC_PRIVILEGES_MAY_BE_DEMOTED_NOT_CPI_AUTHORITY'):
            self.assertIn(limitation,r['limitations'])
        self.assertNotIn('outer_message_privileges',r);self.assertNotIn('entry_authorized',r)
    def test_demoted_flags_do_not_reconstruct_header_or_writable_order(self):
        self.message['accountKeys'][2]['writable']=False
        self.message['accountKeys'][4]['writable']=False
        self.assertEqual(decode(self.row)['status'],'OBSERVED')
        self.reject(lambda r:r['transaction']['message'].update(header={'numRequiredSignatures':2,'numReadonlySignedAccounts':1,'numReadonlyUnsignedAccounts':14}))
    def test_encoded_owners_and_authorities_need_not_be_static(self):
        other=base58(bytes([43])*32)
        self.inner[2]['parsed']['info']['owner']=other
        self.assertEqual(decode(self.row)['status'],'OBSERVED')
        control={'program':'spl-token','programId':self.inner[2]['programId'],'stackHeight':3,
                 'parsed':{'type':'setAuthority','info':{'account':self.inner[2]['parsed']['info']['account'],'newAuthority':other}}}
        self.inner.extend([control,{**copy.deepcopy(control),'parsed':{'type':'revoke','info':control['parsed']['info']}}])
        r=decode(self.row);self.assertEqual([x['type'] for x in r['token_control_operations']],['setAuthority','revoke'])
        self.assertIn('UNSUPPORTED_TOKEN_CONTROL_OPERATION',r['limitations'])
    def test_repeated_instruction_accounts_allowed_but_duplicate_keys_refused(self):
        self.assertLess(len(set(self.message['instructions'][1]['accounts'])),len(self.message['instructions'][1]['accounts']))
        validate(self.row)
        self.reject(lambda r:r['transaction']['message']['accountKeys'].__setitem__(3,r['transaction']['message']['accountKeys'][2]))
    def test_version_types_unknown_and_compiled_v1_refused(self):
        for value in (True,False,'1',2,1.0,None):
            with self.subTest(value=value):self.reject(lambda r:r.update(version=value))
        self.reject(lambda r:r['transaction']['message'].update(accountKeys=[x['pubkey'] for x in self.message['accountKeys']]))
    def test_v1_required_config_exact_fields_and_missing_vs_null(self):
        for field in self.message['transactionConfig']:
            self.reject(lambda r:r['transaction']['message']['transactionConfig'].pop(field))
        for value in (None,[],True):self.reject(lambda r:r['transaction']['message'].update(transactionConfig=value))
        self.reject(lambda r:r['transaction']['message'].pop('transactionConfig'))
        self.reject(lambda r:r['transaction']['message']['transactionConfig'].update(unknown=None))
        self.message['transactionConfig']={k:None for k in self.message['transactionConfig']};validate(self.row)
    def test_config_integer_widths_and_heap_constraints(self):
        for field,bits in (('priorityFee',64),('computeUnitLimit',32),('loadedAccountsDataSizeLimit',32)):
            for value in (True,-1,2**bits,'1',1.5):
                with self.subTest(field=field,value=value):self.reject(lambda r:r['transaction']['message']['transactionConfig'].update({field:value}))
            row=copy.deepcopy(self.row);row['transaction']['message']['transactionConfig'][field]=2**bits-1;validate(row)
        for value in (True,0,32767,32769,262145,2**32,'32768'):
            self.reject(lambda r:r['transaction']['message']['transactionConfig'].update(heapSize=value))
        for value in (32768,262144,None):
            row=copy.deepcopy(self.row);row['transaction']['message']['transactionConfig']['heapSize']=value;validate(row)
    def test_key_bounds_sources_flags_and_signer_prefix(self):
        for field,value in (('source','lookupTable'),('signer',1),('writable',0),('pubkey','bad')):
            self.reject(lambda r:r['transaction']['message']['accountKeys'][2].update({field:value}))
        self.reject(lambda r:r['transaction']['message']['accountKeys'][0].update(writable=False))
        self.reject(lambda r:r['transaction']['message']['accountKeys'][3].update(signer=True))
        self.reject(lambda r:r['transaction']['message'].update(accountKeys=[]))
        row=copy.deepcopy(self.row)
        for i in range(38):row['transaction']['message']['accountKeys'].append({'pubkey':base58((i+300).to_bytes(32,'little')),'signer':False,'writable':False,'source':'transaction'})
        for side in ('preBalances','postBalances'):row['meta'][side].extend([0]*38)
        validate(row)
        row['transaction']['message']['accountKeys'].append({'pubkey':base58(bytes([45])*32),'signer':False,'writable':False,'source':'transaction'})
        with self.assertRaises(ValueError):validate(row)
    def test_signature_count_encoding_and_twelve_bound(self):
        for value in ([],self.row['transaction']['signatures'][:1],['bad']*2,['1'*63]*2,[True]*2):
            self.reject(lambda r:r['transaction'].update(signatures=value))
        row=copy.deepcopy(self.row)
        for i,entry in enumerate(row['transaction']['message']['accountKeys']):entry['signer']=i<12
        row['transaction']['signatures']=[base58(bytes([i+1])*64) for i in range(12)];validate(row)
        row['transaction']['message']['accountKeys'][12]['signer']=True
        row['transaction']['signatures'].append(base58(bytes([50])*64))
        with self.assertRaises(ValueError):validate(row)
    def test_no_lookup_or_unknown_structural_fields(self):
        self.reject(lambda r:r['transaction']['message'].update(addressTableLookups=[]))
        self.reject(lambda r:r['meta'].update(loadedAddresses={'writable':[],'readonly':[]}))
        for mutate in (lambda r:r.update(unknown=True),lambda r:r['meta'].update(unknown=None),
                       lambda r:r['transaction'].update(unknown=0),lambda r:r['transaction']['message'].update(unknown=0),
                       lambda r:r['transaction']['message']['accountKeys'][2].update(unknown=False)):
            self.reject(mutate)
    def test_program_accounts_and_instruction_shape_bindings(self):
        other=base58(bytes([55])*32)
        for ixfield,value in (('programId',other),('accounts',[other]),('data','0'),('stackHeight',True),('programIdIndex',0)):
            self.reject(lambda r:r['transaction']['message']['instructions'][1].update({ixfield:value}))
        self.reject(lambda r:r['transaction']['message']['instructions'][0]['parsed']['info'].update(nonceAccount=other))
        self.reject(lambda r:r['meta']['innerInstructions'][0]['instructions'][0].update(stackHeight=1))
        self.reject(lambda r:r['transaction']['message']['instructions'][1].update(stackHeight=2))
        self.reject(lambda r:r['transaction']['message']['instructions'][0]['parsed'].update(unknown=0))
        row=copy.deepcopy(self.row);row['transaction']['message']['instructions'][1]['accounts']=[self.message['accountKeys'][0]['pubkey']]*255;validate(row)
        row['transaction']['message']['instructions'][1]['accounts'].append(self.message['accountKeys'][0]['pubkey'])
        with self.assertRaises(ValueError):validate(row)
    def test_fee_payer_cannot_be_outer_program_and_history_keeps_gap(self):
        row=copy.deepcopy(self.row)
        row['transaction']['message']['instructions'][1]['programId']=self.message['accountKeys'][0]['pubkey']
        with self.assertRaises(ValueError):decode(row)
        saved=[]
        def capture(value):saved.append(copy.deepcopy(value));return digest(value)
        observed,coverage=collect_history(self.message['accountKeys'][7]['pubkey'],1791570800,1791570900,
                    lambda *_:{'data':[row],'paginationToken':None},capture=capture)
        self.assertEqual(observed,[]);self.assertIn('HISTORY_DECODE_GAP',coverage['reasons'])
        self.assertFalse(coverage['query_coverage_verified']);self.assertEqual(saved[0]['data'][0],row)
        # Outer-only rule: syntax checking CPI observations does not invent
        # caller/program authority or apply message sanitizer to runtime trace.
        row=copy.deepcopy(self.row);row['meta']['innerInstructions'][0]['instructions'][0]['programId']=self.message['accountKeys'][0]['pubkey']
        validate(row)

    def test_outer64_limit_and_inner_parent_identity(self):
        row=copy.deepcopy(self.row);row['transaction']['message']['instructions']=[copy.deepcopy(self.message['instructions'][0]) for _ in range(64)];validate(row)
        row['transaction']['message']['instructions'].append(copy.deepcopy(self.message['instructions'][0]))
        with self.assertRaises(ValueError):validate(row)
        for value in (True,-1,2,256,'1'):
            self.reject(lambda r:r['meta']['innerInstructions'][0].update(index=value))
        self.reject(lambda r:r['meta']['innerInstructions'].append(copy.deepcopy(r['meta']['innerInstructions'][0])))
        self.reject(lambda r:r['meta']['innerInstructions'][0].update(unknown=0))
    def test_balance_arrays_indices_amounts_and_conflicting_identities(self):
        for side in ('preBalances','postBalances'):
            for value in ([],[0]*27,[True]*26,[-1]*26,[2**64]*26):self.reject(lambda r:r['meta'].update({side:value}))
        for value in (True,-1,26,'2',256):self.reject(lambda r:r['meta']['postTokenBalances'][0].update(accountIndex=value))
        self.reject(lambda r:r['meta']['postTokenBalances'].append(copy.deepcopy(r['meta']['postTokenBalances'][0])))
        for value in (True,'-1','01','١','18446744073709551616'):
            self.reject(lambda r:r['meta']['postTokenBalances'][0]['uiTokenAmount'].update(amount=value))
        for value in (True,-1,256,'9'):
            self.reject(lambda r:r['meta']['postTokenBalances'][0]['uiTokenAmount'].update(decimals=value))
        self.reject(lambda r:r['meta']['postTokenBalances'][0].update(mint=self.message['accountKeys'][0]['pubkey']))
        self.reject(lambda r:r['meta']['postTokenBalances'][0]['uiTokenAmount'].update(unknown=None))
    def test_envelope_fee_status_and_resource_bounds(self):
        for name,value in (('slot',True),('blockTime',-1),('transactionIndex',True)):
            self.reject(lambda r:r.update({name:value}))
        self.reject(lambda r:r['meta'].update(fee=True))
        self.reject(lambda r:r['meta'].update(status={'Err':None}))
        self.reject(lambda r:r['meta'].update(logMessages=['x'*32769]))
        self.reject(lambda r:r['meta'].update(logMessages=['x'*30000]*71))
        self.reject(lambda r:r['meta'].update(logMessages=[[]]*1025))
        row=copy.deepcopy(self.row);row['meta']['logMessages']=row
        with self.assertRaises(ValueError):validate(row)
    def test_visible_wire_lower_bound_exact_4096_boundary(self):
        row=copy.deepcopy(self.row)
        ix=copy.deepcopy(self.message['instructions'][1]);ix['accounts']=[self.message['accountKeys'][0]['pubkey']]
        row['transaction']['message']['instructions']=[ix];row['meta']['innerInstructions']=[]
        # 42 fixed bytes +26*32 keys +2*64 signatures +4 instruction header
        # +16 explicit config bytes +1 account index +3073 data =4096.
        ix['data']='1'*3073;validate(row)
        ix['data']+='1'
        with self.assertRaises(ValueError):validate(row)

    def test_visible_wire_and_total_instruction_resource_bounds(self):
        self.reject(lambda r:r['transaction']['message']['instructions'][1].update(data=base58(bytes(4096))))
        self.reject(lambda r:r['transaction']['message']['instructions'][1].update(data='1'*5601))
        self.reject(lambda r:r['meta']['innerInstructions'][0].update(instructions=[self.inner[0]]*1025))
        self.reject(lambda r:r['transaction']['message']['instructions'][0]['parsed']['info'].update(extra=[[0]*1024 for _ in range(20)]))
        for value in (True,float('inf'),-1):
            self.reject(lambda r:r['meta']['postTokenBalances'][0]['uiTokenAmount'].update(uiAmount=value))

    def test_failed_v1_valid_shape_has_no_links_and_malformed_refuses(self):
        self.row['meta']['err']={'InstructionError':[1,'Custom']};self.row['meta']['status']={'Err':self.row['meta']['err']}
        r=decode(self.row);self.assertEqual(r['status'],'FAILED')
        for field in ('transfers','token_deltas','program_observations','token_account_initializations'):self.assertEqual(r[field],[])
        self.assertIn('V1_JSONPARSED_STRUCTURE_NOT_AUTHENTICATED',r['limitations'])
        self.reject(lambda r:r['transaction']['message'].pop('transactionConfig'))
        self.reject(lambda r:r['transaction']['message'].update(accountKeys=[x['pubkey'] for x in self.message['accountKeys']]))
    def test_legacy_v0_and_failed_unknown_behavior_unchanged(self):
        for version in ('legacy',0):
            row=copy.deepcopy(self.row);row['version']=version
            observed=decode(row)
            self.assertEqual(observed['status'],'OBSERVED');self.assertNotIn('V1_JSONPARSED_STRUCTURE_NOT_AUTHENTICATED',observed['limitations'])
        row=copy.deepcopy(self.row);row['version']=2;row['meta']['err']={'error':'synthetic'}
        self.assertEqual(decode(row)['status'],'FAILED')
    def test_history_keeps_original_row_and_no_decode_gap_but_corruption_gaps(self):
        saved=[]
        def capture(value):saved.append(copy.deepcopy(value));return digest(value)
        result=collect_history(self.message['accountKeys'][7]['pubkey'],1791570800,1791570900,
                               lambda *_:{'data':[self.row],'paginationToken':None},capture=capture)
        self.assertNotIn('HISTORY_DECODE_GAP',result[1]['reasons'])
        self.assertEqual(saved[0]['data'][0],self.row)
        row=copy.deepcopy(self.row);row['transaction']['message']['transactionConfig']['priorityFee']=True
        result=collect_history(self.message['accountKeys'][7]['pubkey'],1791570800,1791570900,lambda *_:{'data':[row],'paginationToken':None})
        self.assertIn('HISTORY_DECODE_GAP',result[1]['reasons']);self.assertFalse(result[1]['query_coverage_verified'])


if __name__=='__main__':unittest.main()
