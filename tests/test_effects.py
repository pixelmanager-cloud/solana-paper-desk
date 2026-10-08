import copy
import unittest
from desk.effects import check_effects
from desk.providers import SOL
class EffectTests(unittest.TestCase):
    def value(self):
        def row(i,m,n):return {'accountIndex':i,'owner':'wallet','mint':m,'uiTokenAmount':{'amount':str(n)}}
        return {'err':None,'fee':5,'preBalances':[100,20,20],'postBalances':[195,20,20],
                'preTokenBalances':[row(1,'mint',100),row(2,'other',50)],
                'postTokenBalances':[row(1,'mint',90),row(2,'other',50)]}
    def check(self,v):return check_effects(v,['wallet','holding','otherholding'],'wallet','mint',10,100)
    def test_excessive_network_fee_rejected(self):
        v=self.value();v['fee']=50001
        self.assertIn('SIMULATION_FEE_EXCEEDS_BUDGET',self.check(v)['reasons'])
    def test_exact_debit_and_native_proceeds(self):self.assertTrue(self.check(self.value())['passed'])
    def test_unrelated_asset_drain_rejected(self):
        v=self.value();v['postTokenBalances'][1]['uiTokenAmount']['amount']='49'
        self.assertIn('UNRELATED_TOKEN_LOSS',self.check(v)['reasons'])
    def test_overdebit_rejected(self):
        v=self.value();v['postTokenBalances'][0]['uiTokenAmount']['amount']='80'
        self.assertIn('EXACT_INPUT_DEBIT_MISMATCH',self.check(v)['reasons'])
    def test_rent_return_does_not_fake_proceeds(self):
        v=self.value();v['postBalances']=[125,0,20];v['postTokenBalances']=v['postTokenBalances'][1:]
        r=self.check(v);self.assertEqual(r['native_wealth_delta_lamports'],'5');self.assertFalse(r['passed'])
    def test_missing_metadata_is_unknown(self):
        v=self.value();del v['preBalances']
        self.assertFalse(self.check(v)['passed'])
    def test_identity_change_rejected(self):
        v=self.value();v['postTokenBalances'][0]['owner']='attacker'
        self.assertIn('TOKEN_ACCOUNT_IDENTITY_CHANGED',self.check(v)['reasons'])
    def test_missing_existing_account_not_assumed_zero(self):
        v=self.value();v['postTokenBalances']=v['postTokenBalances'][1:]
        self.assertIn('MISSING_EXISTING_POST_TOKEN_BALANCE',self.check(v)['reasons'])
    def test_wrapped_sol_not_double_counted(self):
        v=self.value();v['preTokenBalances'][1]['mint']=SOL;v['postTokenBalances'][1]['mint']=SOL
        v['preTokenBalances'][1]['uiTokenAmount']['amount']='50';v['postTokenBalances'][1]['uiTokenAmount']['amount']='0'
        v['preBalances'][2]=70;v['postBalances'][2]=20;v['postBalances'][0]=245
        self.assertEqual(self.check(v)['native_wealth_delta_lamports'],'95');self.assertTrue(self.check(v)['passed'])

class ControlTests(unittest.TestCase):
    def setUp(self):
        import base64
        from desk.security import base58,TOKEN_PROGRAM
        self.wallet=base58(bytes([2])*32)
        self.raw=bytearray(165);self.raw[:32]=bytes([3])*32;self.raw[32:64]=bytes([2])*32;self.raw[108]=1
        self.encode=lambda d:{'owner':TOKEN_PROGRAM,'executable':False,'lamports':20,'data':[base64.b64encode(d).decode(),'base64']}
        self.payer={'owner':'11111111111111111111111111111111','executable':False,'lamports':100,'data':['','base64']}
    def value(self):
        from desk.security import base58,TOKEN_PROGRAM
        return {'accounts':[self.payer,self.encode(self.raw)],'postBalances':[100,20],
            'postTokenBalances':[{'accountIndex':1,'owner':self.wallet,'mint':base58(bytes([3])*32),
                'programId':TOKEN_PROGRAM,'uiTokenAmount':{'amount':'0'}}]}
    def check(self):
        from desk.effects import check_account_controls
        return check_account_controls(self.value(),[self.wallet,'holding'],self.wallet,[1])
    def test_clean_control(self):self.assertTrue(self.check()['passed'])
    def test_hidden_delegate_without_balance_loss(self):
        self.raw[72:76]=(1).to_bytes(4,'little');self.raw[76:108]=bytes([9])*32
        self.assertIn('TOKEN_ACCOUNT_DELEGATE',self.check()['reasons'])
    def test_external_close_authority(self):
        self.raw[129:133]=(1).to_bytes(4,'little');self.raw[133:165]=bytes([9])*32
        self.assertIn('EXTERNAL_CLOSE_AUTHORITY',self.check()['reasons'])
    def test_freeze_after_sale(self):
        self.raw[108]=2
        self.assertIn('FROZEN_OR_UNINITIALIZED_HOLDING',self.check()['reasons'])
    def test_payer_assigned_to_program(self):
        self.payer['owner']='malicious'
        self.assertIn('PAYER_ACCOUNT_CONTROL_CHANGED',self.check()['reasons'])
    def test_owner_change(self):
        self.raw[32:64]=bytes([9])*32
        self.assertIn('OWNED_ACCOUNT_CONTROL_UNVERIFIED',self.check()['reasons'])
    def test_partial_state_unknown(self):
        from desk.effects import check_account_controls
        self.assertFalse(check_account_controls({'accounts':[self.payer]},[self.wallet,'holding'],self.wallet,[1])['passed'])
    def test_closed_account_requires_zero_balance(self):
        from desk.effects import check_account_controls
        v={'accounts':[self.payer,None],'postBalances':[100,0],'postTokenBalances':[]}
        self.assertTrue(check_account_controls(v,[self.wallet,'holding'],self.wallet,[1])['passed'])
        v['postBalances'][1]=20
        self.assertFalse(check_account_controls(v,[self.wallet,'holding'],self.wallet,[1])['passed'])
    def test_zero_minimum_rejected(self):
        with self.assertRaises(ValueError):check_effects({},[],self.wallet,'mint',10,0)

    def check_value(self,value):
        from desk.effects import check_account_controls
        return check_account_controls(value,[self.wallet,'holding'],self.wallet,[1])
    def test_account_bytes_disagree_with_token_metadata(self):
        self.raw[64:72]=(1).to_bytes(8,'little')
        self.assertIn('POST_TOKEN_STATE_MISMATCH',self.check()['reasons'])
    def test_lamport_metadata_disagrees_with_state(self):
        v=self.value();v['postBalances'][0]+=1
        self.assertIn('POST_LAMPORT_STATE_MISMATCH',self.check_value(v)['reasons'])
    def test_token_program_metadata_disagrees_with_state(self):
        v=self.value();v['postTokenBalances'][0]['programId']='wrong'
        self.assertIn('POST_TOKEN_STATE_MISMATCH',self.check_value(v)['reasons'])
    def test_owned_token_absent_from_metadata_not_ignored(self):
        v=self.value();v['postTokenBalances']=[]
        self.assertIn('OWNED_POST_TOKEN_METADATA_MISSING',self.check_value(v)['reasons'])
    def test_token_metadata_for_closed_account_rejected(self):
        v=self.value();v['accounts'][1]=None;v['postBalances'][1]=0
        self.assertIn('POST_TOKEN_STATE_MISSING',self.check_value(v)['reasons'])
    def test_missing_post_balances_cannot_pass_controls(self):
        v=self.value();v.pop('postBalances')
        self.assertFalse(self.check_value(v)['passed'])

class BuyEffectTests(unittest.TestCase):
    value=EffectTests.value
    def buy(self,v):return check_effects(v,['wallet','holding','otherholding'],'wallet','mint',10,20,direction='buy')
    def buy_value(self):
        v=self.value();v['postBalances']=[85,20,20];v['postTokenBalances'][0]['uiTokenAmount']['amount']='120';return v
    def test_buy_exact_sol_debit_and_minimum_tokens(self):
        r=self.buy(self.buy_value());self.assertTrue(r['passed']);self.assertEqual(r['received_raw'],'20')
    def test_buy_hidden_sol_surcharge_rejected(self):
        v=self.buy_value();v['postBalances'][0]=84
        self.assertIn('EXACT_NATIVE_INPUT_DEBIT_MISMATCH',self.buy(v)['reasons'])
    def test_buy_below_minimum_rejected(self):
        v=self.buy_value();v['postTokenBalances'][0]['uiTokenAmount']['amount']='119'
        self.assertIn('BOUGHT_TOKENS_BELOW_ROUTE_MINIMUM',self.buy(v)['reasons'])
    def test_new_holding_rent_is_not_swap_spend(self):
        v=self.buy_value();v['preBalances'][1]=0;v['preTokenBalances']=v['preTokenBalances'][1:];v['postBalances'][0]=65
        self.assertTrue(self.buy(v)['passed'])
