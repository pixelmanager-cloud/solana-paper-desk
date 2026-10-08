import base64,unittest
from desk.extensions import inspect_mint_extensions
from desk.security import TOKEN_2022
class ExtensionTests(unittest.TestCase):
    def mint(self,entries=()):
        raw=bytearray(166);raw[45]=1;raw[165]=1
        for kind,payload in entries:raw+=kind.to_bytes(2,'little')+len(payload).to_bytes(2,'little')+payload
        return raw
    def inspect(self,raw):return inspect_mint_extensions({'owner':TOKEN_2022,'executable':False,'data':[base64.b64encode(raw).decode(),'base64']})
    def test_delegate_and_hook_identified_never_approved(self):
        r=self.inspect(self.mint([(12,bytes(32)),(14,bytes(64))]))
        self.assertTrue(r['layout_inventory_complete']);self.assertFalse(r['eligible_for_trading'])
        self.assertEqual(len(r['capabilities']),2)
    def test_unknown_fails_inventory(self):self.assertIn('UNKNOWN_TOKEN_EXTENSION',self.inspect(self.mint([(300,b'')]))['reasons'])
    def test_duplicate_fails_inventory(self):self.assertIn('DUPLICATE_TOKEN_EXTENSION',self.inspect(self.mint([(12,bytes(32))]*2))['reasons'])
    def test_truncation(self):self.assertIn('TRUNCATED_EXTENSION_VALUE',self.inspect(self.mint([(12,bytes(32))])[:-1])['reasons'])
    def test_wrong_account_type(self):
        d=self.mint();d[165]=2
        self.assertFalse(self.inspect(d)['layout_inventory_complete'])
    def test_nonzero_padding(self):
        d=self.mint();d[82]=1
        self.assertFalse(self.inspect(d)['layout_inventory_complete'])
    def test_account_only_extension(self):self.assertIn('ACCOUNT_EXTENSION_ON_MINT',self.inspect(self.mint([(7,b'')]))['reasons'])
    def test_hidden_tail_after_terminator(self):
        d=self.mint();d+=bytes(4)+b'evil'
        self.assertIn('NONZERO_DATA_AFTER_EXTENSION_TERMINATOR',self.inspect(d)['reasons'])
    def test_zero_padding_and_base_mint(self):
        self.assertTrue(self.inspect(self.mint()+bytes(8))['layout_inventory_complete'])
        self.assertTrue(self.inspect(self.mint()[:82])['layout_inventory_complete'])
    def test_metadata_only_still_excluded(self):
        r=self.inspect(self.mint([(18,bytes(64)),(19,b'metadata')]))
        self.assertEqual(r['capabilities'],[]);self.assertFalse(r['eligible_for_trading'])
