"""Unsigned declarations from real compilation; no runtime/CPI authentication.

The saved public fixture has route metas/simulation but no serialized message or
ALT witness. New message/ALT cases are explicitly synthetic reconstructions of
its narrow direct-route variant, never claims about the captured transaction.
"""
import base64
import copy
import json
from pathlib import Path
import unittest
from solders.hash import Hash
from solders.pubkey import Pubkey
from solders.message import MessageV0,MessageHeader
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from desk.compile import compile_unsigned,declared_message_privileges
from desk.instructions import inventory,COMPUTE,ATA,JUPITER,TOKEN_PROGRAM
from desk.route_coverage import check_route_coverage,REQUIRED,ROW_CHECKS,instruction_receipt
from desk.envelope import check_sell_envelope
from desk.security import base58
from tests import test_router


class OuterMessagePrivilegeTests(unittest.TestCase):
    def setUp(self):
        fixture=test_router.RouterTests();fixture.setUp();self.fixture=fixture
        self.outer=fixture.p['outer'];self.wallet=fixture.p['wallet']
        self.response={'computeBudgetInstructions':[x for x in self.outer if x['programId']==COMPUTE],
            'setupInstructions':[x for x in self.outer if x['programId']==ATA],
            'swapInstruction':next(x for x in self.outer if x['programId']==JUPITER),
            'cleanupInstruction':self.outer[-1]}
        self.table=base58(bytes([77])*32)
        addresses=list(dict.fromkeys(a['pubkey'] for ix in self.outer for a in ix['accounts'] if a['pubkey']!=self.wallet))
        self.response['addressesByLookupTableAddress']={self.table:addresses}
        # Official ALT state discriminator=1, deactivation=u64::MAX, remaining
        # metadata/padding zero; solders independently deserializes this fixture.
        raw=(1).to_bytes(4,'little')+((1<<64)-1).to_bytes(8,'little')+bytes(44)+b''.join(bytes(Pubkey.from_string(a)) for a in addresses)
        self.lookup={'value':[{'owner':'AddressLookupTab1e1111111111111111111111111','executable':False,
                              'data':[base64.b64encode(raw).decode(),'base64']}]}
        self.calls=[]
        def rpc(method,params):
            self.calls.append((method,params));return copy.deepcopy(self.lookup)
        self.compiled=compile_unsigned(self.response,self.wallet,str(Hash.default()),rpc)
    def inv(self,compiled=None,outer=None,keys=None,value=None):
        return inventory(outer or self.compiled['outer'],value or {'innerInstructions':[]},
                         keys or self.compiled['keys'],self.wallet,
                         compiled=self.compiled if compiled is None else compiled)
    def checks(self):
        checks={k:{'passed':True} for k in REQUIRED}
        for k in ROW_CHECKS:checks[k]['checked_instructions']=[]
        checks['envelope']=check_sell_envelope(self.compiled['outer'],self.wallet)
        checks['router']=self.fixture.check()
        self.assertTrue(checks['envelope']['passed']);self.assertTrue(checks['router']['passed'])
        return checks
    def assert_unapproved(self,value):
        for key in ('runtime_cpi_privileges_authenticated','source_authenticated','finality_authenticated'):
            self.assertIs(value[key],False)
        if 'transaction_policy_ok' in value:
            self.assertFalse(value['transaction_policy_ok']);self.assertFalse(value['full_route_policy_passed'])
    def test_real_compiled_header_static_loaded_order_and_exact_outer_positions(self):
        result=self.inv();proof=result['outer_message_privileges'];tx=VersionedTransaction.from_bytes(self.compiled['raw'])
        self.assertTrue(proof['declarations_consistent']);self.assertFalse(proof['alt_contents_authenticated'])
        self.assertEqual(proof['header']['numRequiredSignatures'],1)
        self.assertEqual([p['pubkey'] for p in proof['account_privileges']],self.compiled['keys'])
        self.assertTrue(any(p['segment']=='loaded_writable' for p in proof['account_privileges']))
        self.assertTrue(any(p['segment']=='loaded_readonly' for p in proof['account_privileges']))
        for row,ix in zip(result['instructions'],tx.message.instructions):
            self.assertEqual(row['accounts'],[self.compiled['keys'][i] for i in ix.accounts])
            for declaration,i in zip(row['declared_account_privileges'],ix.accounts):
                self.assertEqual(declaration,{**proof['account_privileges'][i],'message_index':i})
                if declaration['segment']!='static':self.assertFalse(declaration['signer'])
        coverage=check_route_coverage(result,self.checks())
        self.assertTrue(coverage['coverage_passed']);self.assertTrue(coverage['outer_message_privileges_bound'])
        self.assert_unapproved(result);self.assert_unapproved(coverage)
    def test_requested_flags_and_message_promotions_are_distinct_and_receipts_detached(self):
        result=self.inv();found=False
        for row in result['instructions']:
            for requested,declared in zip(row['requested_account_metas'],row['declared_account_privileges']):
                if not requested['isWritable'] and declared['writable']:found=True
            receipt=instruction_receipt(row)
            if receipt['requested_account_metas']:
                receipt['requested_account_metas'][0]['isWritable']=not receipt['requested_account_metas'][0]['isWritable']
                self.assertNotEqual(receipt['requested_account_metas'],row['requested_account_metas'])
        self.assertTrue(found,'Expected local readonly/global writable promotion in synthetic route')
        # Global header flags are derived even when a local instruction requests
        # less; no equality with the original quote flags is assumed.
        self.assertEqual(result['outer_message_privileges'],self.compiled['declared_message_privileges'])
    def test_tampered_header_program_raw_account_and_key_order_are_explicitly_invalid(self):
        tx=VersionedTransaction.from_bytes(self.compiled['raw']);m=tx.message
        bad_msg=MessageV0(m.header,m.account_keys,m.recent_blockhash,list(reversed(m.instructions)),m.address_table_lookups)
        bad={**self.compiled,'raw':bytes(VersionedTransaction.populate(bad_msg,[Signature.default()]))}
        variants=[(bad,None,None)]
        for header in (MessageHeader(1,0,m.header.num_readonly_unsigned_accounts-1),MessageHeader(1,0,255)):
            changed=MessageV0(header,m.account_keys,m.recent_blockhash,m.instructions,m.address_table_lookups)
            variants.append(({**self.compiled,'raw':bytes(VersionedTransaction.populate(changed,[Signature.default()]))},None,None))
        variants.append(({**self.compiled,'raw':b'invalid bytes'},None,None))
        outer=copy.deepcopy(self.compiled['outer']);outer[0]['data']='AA==';variants.append((self.compiled,outer,None))
        outer=copy.deepcopy(self.compiled['outer']);outer[1]['accounts'].reverse();variants.append((self.compiled,outer,None))
        keys=copy.deepcopy(self.compiled['keys']);keys[0],keys[1]=keys[1],keys[0];variants.append((self.compiled,None,keys))
        for flag in (None,1):
            outer=copy.deepcopy(self.compiled['outer']);outer[1]['accounts'][0]['isWritable']=flag
            variants.append((self.compiled,outer,None))
        for compiled,outer,keys in variants:
            with self.subTest(outer=outer is not None,keys=keys is not None):
                result=self.inv(compiled,outer,keys)
                self.assertIsNone(result['outer_message_privileges'])
                self.assertIn('OUTER_MESSAGE_PRIVILEGES_CONTRADICTORY_OR_INVALID',result['outer_privilege_reasons'])
                self.assertFalse(check_route_coverage(result,self.checks())['coverage_passed'])
    def test_missing_or_changed_lookup_witness_and_rpc_loaded_order_reject(self):
        for snapshot in (None,{**self.compiled['lookup_snapshot'],'params':[[self.table],{'encoding':'base64','commitment':'finalized'}]},
                         {**self.compiled['lookup_snapshot'],'result':{'value':[]}}):
            result=self.inv({**self.compiled,'lookup_snapshot':snapshot})
            self.assertIsNone(result['outer_message_privileges'])
        changed=copy.deepcopy(self.compiled)
        account=changed['lookup_snapshot']['result']['value'][0]
        raw=bytearray(base64.b64decode(account['data'][0]));raw[56:88]=bytes([99])*32
        account['data'][0]=base64.b64encode(raw).decode()
        self.assertIsNone(self.inv(changed)['outer_message_privileges'])
        loaded=copy.deepcopy(self.compiled['declared_message_privileges']['loaded_addresses'])
        good=self.inv(value={'innerInstructions':[],'loadedAddresses':loaded})
        self.assertEqual(good['simulation_loaded_addresses_status'],'MATCHED_UNAUTHENTICATED')
        loaded['writable'].reverse()
        self.assertIsNone(self.inv(value={'innerInstructions':[],'loadedAddresses':loaded})['outer_message_privileges'])
    def test_saved_public_fixture_has_no_message_witness_and_cannot_assert_privileges(self):
        root=Path(__file__).resolve().parents[1]
        saved=json.loads((root/'fixtures/mainnet-sell-simulation.json').read_text());before=copy.deepcopy(saved)
        result=inventory(saved['outer'],saved['simulation'],saved['keys'],saved['wallet'])
        self.assertIsNone(result['outer_message_privileges']);self.assertIn('OUTER_MESSAGE_PRIVILEGES_UNAVAILABLE',result['outer_privilege_reasons'])
        self.assertTrue(all(r['declared_account_privileges'] is None for r in result['instructions']))
        self.assert_unapproved(result);self.assertEqual(saved,before)
    def test_cpi_rows_never_inherit_message_signer_or_writable_authority(self):
        key=self.compiled['keys'].index(JUPITER)
        value={'innerInstructions':[{'index':2,'instructions':[{'programIdIndex':key,'accounts':[0],
                                                               'data':'1','stackHeight':2}]}]}
        result=self.inv(value=value);inner=result['instructions'][-1]
        self.assertEqual(inner['stack_height'],2);self.assertIsNone(inner['declared_account_privileges'])
        self.assertIsNone(inner['requested_account_metas']);self.assert_unapproved(result)
    def test_arbitrary_compiler_flags_and_tampered_inventory_cannot_replace_message(self):
        fake={**self.compiled,'declared_message_privileges':{'verified':True},'privileges':[{'signer':True}]}
        result=self.inv(fake);self.assertEqual(result['outer_message_privileges'],self.compiled['declared_message_privileges'])
        for mutation in ('declared','requested','hash','keys'):
            changed=copy.deepcopy(result)
            if mutation=='declared':changed['instructions'][1]['declared_account_privileges'][0]['writable']=False
            elif mutation=='requested':changed['instructions'][1]['requested_account_metas'][0]['isWritable']=False
            elif mutation=='hash':changed['outer_message_privileges']['message_hash']='a'*64
            else:changed['transaction_keys'].reverse()
            coverage=check_route_coverage(changed,self.checks())
            self.assertFalse(coverage['coverage_passed']);self.assert_unapproved(coverage)
