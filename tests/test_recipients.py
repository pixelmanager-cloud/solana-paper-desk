import base64,unittest
from desk.recipients import check_sell_recipients
from desk.security import TOKEN_PROGRAM,base58
from desk.providers import SOL

class RecipientTests(unittest.TestCase):
    def setUp(self):
        fields=['user','pool','base_mint','user_base_token_account','pool_base_token_account','pool_quote_token_account','user_quote_token_account','protocol_fee_recipient_token_account','coin_creator_vault_ata']
        self.names={name:base58(bytes([i+1])*32) for i,name in enumerate(fields)}
        self.bindings={'passed':True,'instruction':'2.0','account_bindings':self.names}
        self.rows=[self.ix('user_base_token_account','pool_base_token_account','user',100,self.names['base_mint']),self.ix('pool_quote_token_account','user_quote_token_account','pool',90,SOL)]
    def ix(self,source,dest,authority,amount,mint):
        return {'program':TOKEN_PROGRAM,'instruction':'2.1','parent_instruction':'2.0','stack_height':3,
            'accounts':[self.names[source],mint,self.names[dest],self.names[authority]],
            'data_base64':base64.b64encode(b'\x0c'+amount.to_bytes(8,'little')+b'\x06').decode()}
    def check(self):return check_sell_recipients({'stack_metadata_verified':True,'instructions':self.rows},self.bindings,100,90)
    def test_bound_transfers_are_not_full_fee_approval(self):
        r=self.check();self.assertTrue(r['passed']);self.assertFalse(r['fee_amounts_verified']);self.assertFalse(r['full_route_policy_passed'])
    def test_unapproved_fee_recipient_rejected(self):
        fee=self.ix('pool_quote_token_account','protocol_fee_recipient_token_account','pool',2,SOL)
        fee['accounts'][2]=base58(bytes([99])*32);self.rows.append(fee)
        self.assertIn('AMM_TOKEN_RECIPIENT_OR_SOURCE_UNAPPROVED',self.check()['reasons'])
    def test_configured_fee_transfers_accounted_separately(self):
        self.rows.extend([self.ix('pool_quote_token_account','protocol_fee_recipient_token_account','pool',2,SOL),self.ix('pool_quote_token_account','coin_creator_vault_ata','pool',3,SOL)])
        r=self.check();self.assertTrue(r['passed']);self.assertEqual(r['protocol_fee_raw'],'2');self.assertEqual(r['creator_fee_raw'],'3')
    def test_transfer_under_other_caller_rejected(self):
        self.rows[0]['parent_instruction']='2.5'
        self.assertIn('TOKEN_TRANSFER_OUTSIDE_APPROVED_AMM_CALL',self.check()['reasons'])
    def test_wrong_vault_authority_rejected(self):
        self.rows[1]['accounts'][-1]=self.names['user']
        self.assertIn('AMM_OUTPUT_TRANSFER_AUTHORITY_OR_MINT_MISMATCH',self.check()['reasons'])
    def test_changed_input_mint_rejected(self):
        self.rows[0]['accounts'][1]=SOL
        self.assertIn('AMM_INPUT_TRANSFER_AUTHORITY_OR_MINT_MISMATCH',self.check()['reasons'])
    def test_missing_output_cannot_pass(self):
        self.rows.pop();self.assertIn('AMM_TRANSFER_OUTPUT_BELOW_MINIMUM',self.check()['reasons'])
    def test_recipient_role_alias_cannot_hide_fees_as_proceeds(self):
        self.names['protocol_fee_recipient_token_account']=self.names['user_quote_token_account']
        self.assertIn('AMM_RECIPIENT_ROLES_OVERLAP',self.check()['reasons'])
    def test_buyback_is_counted_separately_from_proceeds(self):
        self.names['buyback_fee_recipient_token_account']=base58(bytes([12])*32)
        self.rows.append(self.ix('pool_quote_token_account','buyback_fee_recipient_token_account','pool',4,SOL))
        r=self.check();self.assertTrue(r['passed']);self.assertEqual(r['buyback_fee_raw'],'4');self.assertEqual(r['user_output_raw'],'90')
        self.assertFalse(r['fee_amounts_verified'])
    def test_buyback_cannot_alias_user_output(self):
        self.names['buyback_fee_recipient_token_account']=self.names['user_quote_token_account']
        self.assertIn('AMM_RECIPIENT_ROLES_OVERLAP',self.check()['reasons'])
