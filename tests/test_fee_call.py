import base64,copy,json,unittest
from pathlib import Path
from desk.fee_call import decode_quote_fee_call,check_fee_query
from desk.dynamic_fees import fee_schema
from desk.providers import SOL
class FeeCallTests(unittest.TestCase):
    def setUp(self):
        self.p=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-fee-call.json').read_text())
        fields=decode_quote_fee_call(base64.b64decode(self.p['data_base64']),self.p['accounts'])
        self.row={**self.p,'program':fee_schema()['address'],'stack_height':3,'parent_instruction':'2.0','instruction':'2.1'}
        self.inv={'stack_metadata_verified':True,'instructions':[self.row]}
        self.bind={'passed':True,'instruction':'2.0','account_bindings':{'fee_config':self.p['accounts'][0]}}
        self.fees={'passed':True,'fee_schedule':fields}
    def check(self):return check_fee_query(self.inv,self.bind,self.fees)
    def test_public_fee_arguments_with_synthetic_direct_router_context(self):
        r=self.check();self.assertEqual(r['arguments']['quote_mint'],SOL);self.assertTrue(r['passed']);self.assertFalse(r['full_route_policy_passed'])
    def test_market_cap_or_pool_kind_cannot_be_substituted(self):
        self.fees['fee_schedule']['market_cap_lamports']='1';self.assertIn('FEE_QUERY_ARGUMENT_MISMATCH',self.check()['reasons'])
    def test_other_caller_or_depth_rejected(self):
        self.row['parent_instruction']='2.9';self.assertIn('FEE_QUERY_CALLER_MISMATCH',self.check()['reasons'])
        self.row['parent_instruction']='2.0';self.row['stack_height']=4;self.assertFalse(self.check()['passed'])
    def test_extra_fee_calls_or_accounts_rejected(self):
        self.inv['instructions'].append(copy.deepcopy(self.row));self.assertFalse(self.check()['passed']);self.inv['instructions'].pop()
        self.row['accounts']=self.row['accounts']+[SOL];self.assertFalse(self.check()['passed'])
    def test_changed_discriminator_and_trailing_data_rejected(self):
        raw=base64.b64decode(self.row['data_base64'])
        for data in (bytes(8)+raw[8:],raw+b'\0',raw[:8]+b'\2'+raw[9:]):
            self.row['data_base64']=base64.b64encode(data).decode();self.assertFalse(self.check()['passed'])
    def test_no_trust_from_call_when_fee_totals_are_unverified(self):
        self.fees['passed']=False;self.assertIn('FEE_QUERY_CONTEXT_UNVERIFIED',self.check()['reasons'])

    def test_original_public_call_depth_is_outside_router_profile(self):
        self.row['stack_height']=self.p['stack_height']
        self.assertFalse(self.check()['passed'])
