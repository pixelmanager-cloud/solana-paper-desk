import base64,copy,unittest
from solders.hash import Hash
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from desk.compile import compile_unsigned
from desk.instructions import SYSTEM
class CompileTests(unittest.TestCase):
    def setUp(self):
        self.wallet='FzULv8pR9Rd7cyVKjVkzmJ1eqEmgwDnzjYyNUcEJtoG9'
        self.response={'swapInstruction':{'programId':SYSTEM,'data':base64.b64encode((2).to_bytes(4,'little')+bytes(8)).decode(),
          'accounts':[{'pubkey':self.wallet,'isSigner':True,'isWritable':True},{'pubkey':SYSTEM,'isSigner':False,'isWritable':True}]}}
    def compile(self,rpc=lambda *args:None):return compile_unsigned(self.response,self.wallet,str(Hash.default()),rpc)
    def test_null_signature_and_resolved_keys(self):
        r=self.compile();tx=VersionedTransaction.from_bytes(r['raw']);self.assertEqual(tx.signatures,[Signature.default()]);self.assertEqual(r['keys'],[str(x) for x in tx.message.account_keys])
    def test_unexpected_signer_rejected(self):
        self.response['swapInstruction']['accounts'][1]['isSigner']=True
        with self.assertRaises(ValueError):self.compile()
    def test_wrong_lookup_owner_rejected(self):
        self.response['addressesByLookupTableAddress']={SYSTEM:[]}
        with self.assertRaisesRegex(ValueError,'lookup table owner'):self.compile(lambda *a:{'value':[{'owner':SYSTEM,'executable':False}]})
    def test_missing_lookup_response_rejected(self):
        self.response['addressesByLookupTableAddress']={SYSTEM:[]}
        with self.assertRaisesRegex(ValueError,'Missing lookup'):self.compile(lambda *a:{'value':[]})
    def test_tip_rejected(self):
        self.response['tipInstruction']=copy.deepcopy(self.response['swapInstruction'])
        with self.assertRaises(ValueError):self.compile()
    def test_packet_budget_rejected(self):
        self.response['swapInstruction']['data']=base64.b64encode(bytes(1300)).decode()
        with self.assertRaises(ValueError):self.compile()
