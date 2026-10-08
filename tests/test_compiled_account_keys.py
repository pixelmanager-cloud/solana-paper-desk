"""Synthetic compiled JSON only; no parsed instruction or authority fabrication."""
import copy
import unittest
from desk.decode import decode
from desk.model import digest
from desk.security import base58, TOKEN_PROGRAM


def fixture():
    keys=[base58(bytes([i])*32) for i in range(1,5)]
    return {'slot':10,'version':'legacy','transaction':{'signatures':['first','second'],
        'message':{'accountKeys':keys,'header':{'numRequiredSignatures':2,
        'numReadonlySignedAccounts':1,'numReadonlyUnsignedAccounts':1},
        'instructions':[{'programIdIndex':3,'accounts':[0,1,2],'data':'1'}]}},
        'meta':{'err':None,'preTokenBalances':[],'postTokenBalances':[],
                'innerInstructions':[{'index':0,'instructions':[{'programIdIndex':3,'accounts':[1,2],'data':'1','stackHeight':2}]}]}}


class CompiledAccountKeysTests(unittest.TestCase):
    def lookup(self):
        raw=fixture();raw['version']=0
        message=raw['transaction']['message']
        message['addressTableLookups']=[{'accountKey':base58(bytes([8])*32),'writableIndexes':[7,2],'readonlyIndexes':[9]}]
        raw['meta']['loadedAddresses']={'writable':[base58(bytes([5])*32),base58(bytes([6])*32)],'readonly':[TOKEN_PROGRAM]}
        message['instructions'][0]={'programIdIndex':6,'accounts':[4,5,0],'data':'7'}
        return raw
    def test_legacy_header_privileges_raw_bytes_unchanged(self):
        raw=fixture();before=copy.deepcopy(raw);result=decode(raw)
        self.assertEqual(raw,before);self.assertEqual(result['payload_hash'],digest(before))
        rows=result['outer_message_privileges']
        self.assertEqual([(r['signer'],r['writable']) for r in rows],[(True,True),(True,False),(False,True),(False,False)])
        self.assertIn('COMPILED_PRIVILEGES_NOT_CPI_AUTHORITY',result['limitations'])
        self.assertEqual(result['transfers'],[])
    def test_v0_loaded_order_no_signers_or_parsed_token_history(self):
        raw=self.lookup();result=decode(raw);rows=result['outer_message_privileges']
        self.assertEqual([r['pubkey'] for r in rows[4:]],raw['meta']['loadedAddresses']['writable']+raw['meta']['loadedAddresses']['readonly'])
        self.assertEqual([(r['signer'],r['writable']) for r in rows[4:]],[(False,True),(False,True),(False,False)])
        self.assertEqual(result['mint_initializations'],[]);self.assertEqual(result['token_supply_changes'],[])
        self.assertIn('UNDECODED_TOKEN_INSTRUCTION',result['limitations'])
        self.assertNotIn('parsed',raw['transaction']['message']['instructions'][0])
    def test_loaded_token_balance_indices_resolved(self):
        raw=self.lookup()
        for side,amount in [('pre','5'),('post','7')]:
            raw['meta'][side+'TokenBalances']=[{'accountIndex':4,'mint':raw['transaction']['message']['accountKeys'][0],
                'uiTokenAmount':{'amount':amount,'decimals':6}}]
        result=decode(raw)
        self.assertEqual(result['token_deltas'][0]['account'],raw['meta']['loadedAddresses']['writable'][0])
        self.assertEqual(result['token_deltas'][0]['delta_raw'],'2')
    def test_unknown_or_absent_compiled_version_rejected(self):
        for version in (None,True,False,1,'0','future'):
            raw=fixture();raw['version']=version
            with self.subTest(version=version),self.assertRaises(ValueError):decode(raw)
        raw=fixture();del raw['version']
        with self.assertRaises(ValueError):decode(raw)
    def test_malformed_header_counts_rejected(self):
        for field,values in [('numRequiredSignatures',(True,-1,0,5,'2')),
                             ('numReadonlySignedAccounts',(True,-1,2,3)),
                             ('numReadonlyUnsignedAccounts',(True,-1,3,'1'))]:
            for value in values:
                raw=fixture();raw['transaction']['message']['header'][field]=value
                with self.subTest(field=field,value=value),self.assertRaises(ValueError):decode(raw)
        for header in (None,{}, {'numRequiredSignatures':2}):
            raw=fixture();raw['transaction']['message']['header']=header
            with self.assertRaises(ValueError):decode(raw)
    def test_signature_header_count_mismatch(self):
        for signatures in ([],['one'],['one','two','three'],['one',None]):
            raw=fixture();raw['transaction']['signatures']=signatures
            with self.assertRaises(ValueError):decode(raw)
    def test_duplicate_or_invalid_static_keys(self):
        for value in ('bad',None,fixture()['transaction']['message']['accountKeys'][0]):
            raw=fixture();raw['transaction']['message']['accountKeys'][1]=value
            with self.assertRaises(ValueError):decode(raw)
    def test_loaded_count_shape_duplicate_and_static_collision(self):
        for field,value in [('loadedAddresses',None),('loadedAddresses',{}),('loadedAddresses',{'writable':[],'readonly':[]}),
                             ('loadedAddresses',{'writable':'bad','readonly':[]})]:
            raw=self.lookup();raw['meta'][field]=value
            with self.assertRaises(ValueError):decode(raw)
        for value in (TOKEN_PROGRAM,fixture()['transaction']['message']['accountKeys'][0],'bad'):
            raw=self.lookup();raw['meta']['loadedAddresses']['writable'][0]=value
            with self.assertRaises(ValueError):decode(raw)
    def test_lookup_indices_and_descriptor_inconsistencies(self):
        for value in (True,-1,256,'1'):
            raw=self.lookup();raw['transaction']['message']['addressTableLookups'][0]['writableIndexes'][0]=value
            with self.assertRaises(ValueError):decode(raw)
        for mutate in ('duplicate_index','duplicate_table','missing','legacy'):
            raw=self.lookup();lookups=raw['transaction']['message']['addressTableLookups']
            if mutate=='duplicate_index':lookups[0]['readonlyIndexes']=[7]
            elif mutate=='duplicate_table':lookups.append(copy.deepcopy(lookups[0]))
            elif mutate=='missing':del raw['transaction']['message']['addressTableLookups']
            else:raw['version']='legacy'
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):decode(raw)
    def test_program_account_and_inner_parent_indices_strict(self):
        for value in (True,-1,4,256,'1'):
            for inner in (False,True):
                for field in ('programIdIndex','accounts'):
                    raw=fixture();ix=(raw['meta']['innerInstructions'][0]['instructions'][0] if inner else raw['transaction']['message']['instructions'][0])
                    ix[field]=[value] if field=='accounts' else value
                    with self.subTest(value=value,inner=inner,field=field),self.assertRaises(ValueError):decode(raw)
        for value in (True,-1,1,'0'):
            raw=fixture();raw['meta']['innerInstructions'][0]['index']=value
            with self.assertRaises(ValueError):decode(raw)
    def test_conflicting_program_or_fabricated_parsed_fields_rejected(self):
        for field,value in [('programId',TOKEN_PROGRAM),('parsed',{'type':'mintTo','info':{}})]:
            raw=fixture();raw['transaction']['message']['instructions'][0][field]=value
            with self.assertRaises(ValueError):decode(raw)
    def test_key_limit_and_boolean_version_do_not_pass_integer_checks(self):
        raw=fixture();raw['transaction']['message']['accountKeys']*=65
        with self.assertRaises(ValueError):decode(raw)
    def test_cpi_does_not_acquire_signer_privileges(self):
        raw=self.lookup();raw['meta']['innerInstructions'][0]['instructions'][0]['accounts']=[4,5,6]
        result=decode(raw)
        self.assertTrue(all(not r['signer'] for r in result['outer_message_privileges'][4:]))
        self.assertTrue(all('signer' not in event for event in result['program_observations']))
    def test_token_balance_indices_and_duplicate_inner_groups_rejected(self):
        for value in (True,'0',-1,4):
            raw=fixture();raw['meta']['preTokenBalances']=[{'accountIndex':value}]
            with self.assertRaises(ValueError):decode(raw)
        raw=fixture();raw['meta']['innerInstructions']*=2
        with self.assertRaises(ValueError):decode(raw)

    def test_empty_compiled_keys_rejected_but_existing_empty_parsed_view_preserved(self):
        raw=fixture();raw['transaction']['message']['accountKeys']=[]
        with self.assertRaises(ValueError):decode(raw)
        raw['transaction']['message']={'accountKeys':[],'instructions':[]}
        raw['meta']['innerInstructions']=[];del raw['version']
        self.assertNotIn('outer_message_privileges',decode(raw))

    def test_header_counts_must_fit_message_byte_width(self):
        raw=fixture();message=raw['transaction']['message']
        message['accountKeys']=[base58(bytes([i])*32) for i in range(256)]
        message['header']={'numRequiredSignatures':256,'numReadonlySignedAccounts':0,'numReadonlyUnsignedAccounts':0}
        raw['transaction']['signatures']=['signature']*256
        with self.assertRaises(ValueError):decode(raw)

    def test_raw_token_control_remains_unknown_not_a_parsed_authority_witness(self):
        raw=self.lookup();raw['transaction']['message']['instructions'][0]['data']=base58(bytes([6,0,0]))
        result=decode(raw)
        self.assertIn('UNDECODED_TOKEN_INSTRUCTION',result['limitations'])
        self.assertEqual(result['mint_initializations'],[])
        self.assertEqual(result['token_supply_changes'],[])
        self.assertEqual(result['transfers'],[])

    def test_zero_lookup_v0_and_legacy_without_loaded_metadata(self):
        raw=fixture();raw['version']=0;raw['transaction']['message']['addressTableLookups']=[]
        self.assertEqual(len(decode(raw)['outer_message_privileges']),4)
        raw['version']='legacy';raw['meta']['loadedAddresses']={'writable':[],'readonly':[]}
        self.assertEqual(len(decode(raw)['outer_message_privileges']),4)
