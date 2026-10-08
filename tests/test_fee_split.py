import unittest
from desk.fee_split import check_protocol_split
class FeeSplitTests(unittest.TestCase):
    def setUp(self):
        self.pool={'identity_evidence_verified':True,'liquidity_control_verified':True,'slot':10,'global_config':{'configuration_complete':True,'slot':10,'fields':{'buyback_basis_points':5000}}}
        self.bind={'passed':True,'account_profile':'STANDARD_WITH_BUYBACK'};self.fees={'passed':True,'expected':{'protocol_total_raw':'1765'}}
        self.rec={'passed':True,'protocol_fee_raw':'883','buyback_fee_raw':'882'}
    def check(self):return check_protocol_split(self.pool,self.bind,self.fees,self.rec)
    def test_odd_fee_floors_buyback_and_keeps_remainder(self):
        r=self.check();self.assertTrue(r['passed']);self.assertFalse(r['fee_amounts_verified']);self.assertFalse(r['full_route_policy_passed'])
    def test_same_total_with_diverted_split_is_rejected(self):
        self.rec.update(protocol_fee_raw='882',buyback_fee_raw='883');self.assertFalse(self.check()['passed'])
    def test_changed_config_and_unknown_rate_remain_unsupported(self):
        self.pool['global_config']['slot']=9;self.assertFalse(self.check()['passed'])
        self.pool['global_config']['slot']=10;self.pool['global_config']['fields']['buyback_basis_points']=6000
        self.assertIn('FEE_SPLIT_PROFILE_UNSUPPORTED',self.check()['reasons'])
    def test_missing_buyback_accounts_and_negative_totals_rejected(self):
        self.bind['account_profile']='LEGACY_BASE_ACCOUNTS';self.assertFalse(self.check()['passed'])
        self.bind['account_profile']='STANDARD_WITH_BUYBACK';self.fees['expected']['protocol_total_raw']='-1';self.assertFalse(self.check()['passed'])
    def test_zero_and_even_totals(self):
        for total in (0,2,2456830):
            self.fees['expected']['protocol_total_raw']=str(total);self.rec.update(protocol_fee_raw=str(total//2),buyback_fee_raw=str(total//2))
            self.assertTrue(self.check()['passed'])
