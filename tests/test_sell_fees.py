import base64,copy,unittest
from desk.sell_fees import standard_sell_amounts,check_sell_fee_totals
from desk.security import TOKEN_PROGRAM
import tests.test_pools as pool_tests

class SellFeeTests(unittest.TestCase):
    def setUp(self):
        helper=pool_tests.PoolTests();helper.setUp();self.pool=helper.verify()
        names={'fee_config':self.pool['dynamic_fee_config']['address'],'global_config':self.pool['global_config']['address'],'base_mint':str(helper.mint)}
        self.bindings={'passed':True,'account_bindings':names}
        raw=helper.rpc('getMultipleAccounts',[])['value']
        self.keys=[v['address'] for v in self.pool['vaults']]+[names['fee_config'],names['global_config'],names['base_mint']]
        self.sim={'err':None,'accounts':[raw[0],raw[1],raw[5],raw[4],raw[6]],'preTokenBalances':[
            {'accountIndex':i,'mint':v['mint'],'owner':self.pool['pool'],'programId':TOKEN_PROGRAM,'uiTokenAmount':{'amount':v['amount_raw']}}
            for i,v in enumerate(self.pool['vaults'])]}
        from desk.dynamic_fees import standard_sol_fees
        rates=standard_sol_fees(self.pool['dynamic_fee_config'],10**15,1000000,1000000,canonical=True)['fees_bps']
        expected=standard_sell_amounts(100000,1000000,1000000,rates,has_creator=False)
        self.rec={'passed':True,'user_output_raw':expected['user_output_raw'],'creator_fee_raw':'0','protocol_fee_raw':expected['protocol_total_raw'],'buyback_fee_raw':'0'}
    def check(self):return check_sell_fee_totals(self.pool,self.bindings,self.rec,self.sim,self.keys,100000)
    def test_integer_rounding_charges_each_fee_separately(self):
        r=standard_sell_amounts(1000,10000,10000,{'lp_fee_bps':20,'protocol_fee_bps':5,'creator_fee_bps':95},has_creator=True)
        self.assertEqual(r,{'gross_quote_raw':'909','lp_fee_raw':'2','protocol_total_raw':'1','creator_fee_raw':'9','user_output_raw':'897'})
    def test_absent_creator_does_not_charge_creator_fee(self):
        r=standard_sell_amounts(1000,10000,10000,{'lp_fee_bps':20,'protocol_fee_bps':5,'creator_fee_bps':95},has_creator=False)
        self.assertEqual(r['creator_fee_raw'],'0');self.assertEqual(r['user_output_raw'],'906')
    def test_dust_negative_and_invalid_rates_are_rejected(self):
        for amount,fees in ((1,{'lp_fee_bps':20,'protocol_fee_bps':5,'creator_fee_bps':95}),(100,{'lp_fee_bps':10001,'protocol_fee_bps':0,'creator_fee_bps':0})):
            with self.assertRaises(ValueError):standard_sell_amounts(amount,10000,10000,fees,has_creator=True)
    def test_large_values_remain_integer_exact(self):
        n=2**53+1;r=standard_sell_amounts(n,n,n,{'lp_fee_bps':0,'protocol_fee_bps':0,'creator_fee_bps':0},has_creator=False)
        self.assertEqual(int(r['user_output_raw']),n//2)
    def test_matching_totals_do_not_approve_split_or_full_policy(self):
        r=self.check();self.assertTrue(r['passed']);self.assertFalse(r['fee_amounts_verified']);self.assertFalse(r['full_route_policy_passed'])
    def test_fee_diversion_or_user_underpayment_rejected(self):
        for key in ('user_output_raw','creator_fee_raw','protocol_fee_raw','buyback_fee_raw'):
            saved=self.rec[key];self.rec[key]=str(int(saved)+1)
            self.assertFalse(self.check()['passed']);self.rec[key]=saved
    def test_reserves_must_match_simulation_prestate(self):
        self.sim['preTokenBalances'][0]['uiTokenAmount']['amount']='999999'
        self.assertIn('SELL_FEE_RESERVES_CHANGED_SINCE_SNAPSHOT',self.check()['reasons'])
    def test_missing_or_duplicate_prestate_cannot_pass(self):
        saved=copy.deepcopy(self.sim['preTokenBalances']);self.sim['preTokenBalances']=[]
        self.assertFalse(self.check()['passed']);self.sim['preTokenBalances']=saved+[saved[0]]
        self.assertFalse(self.check()['passed'])
    def test_supply_or_fee_configuration_changes_cannot_pass(self):
        for index,offset in ((4,36),(2,41)):
            saved=self.sim['accounts'][index]['data'][0];raw=bytearray(base64.b64decode(saved));raw[offset]^=1
            self.sim['accounts'][index]['data'][0]=base64.b64encode(raw).decode()
            self.assertFalse(self.check()['passed']);self.sim['accounts'][index]['data'][0]=saved
    def test_mismatched_snapshot_slots_and_special_profiles_rejected(self):
        self.pool['dynamic_fee_config']['slot']-=1;self.assertFalse(self.check()['passed'])
        self.pool['dynamic_fee_config']['slot']+=1;self.pool['is_cashback_coin']=True;self.assertFalse(self.check()['passed'])
    def test_failed_simulation_or_unknown_bindings_rejected(self):
        self.sim['err']={'InstructionError':[0,'error']};self.assertFalse(self.check()['passed'])
        self.sim['err']=None;self.bindings['passed']=False;self.assertFalse(self.check()['passed'])
