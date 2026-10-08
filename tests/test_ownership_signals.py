import unittest
from unittest.mock import patch
from desk.ownership_signals import funding_groups
class FundingGroupTests(unittest.TestCase):
    def observations(self):
        return [{'signature':w,'slot':101,'block_time':100,'commitment':'finalized_provider_response',
                 'program_observations':[{'mint':'mint','kind':'BUY_INTENT','status':'IDENTIFIED','wallet':w}]} for w in ('a','b')]
    def queries(self):
        return [{'address':w,'start':0,'end':101,'token_accounts_filter':'balanceChanged'} for w in ('a','b')]
    def replay(self,q,store):
        return [{'signature':'fund-'+q['address'],'slot':100,'block_time':99,'status':'OBSERVED','commitment':'finalized_provider_response',
                 'transfers':[{'asset':'SOL','source':'source','destination':q['address'],'amount_raw':'1000000'}]}], {'query_coverage_verified':True,'evidence_hash':'a'*64}
    def test_shared_source_is_unknown_not_private_controller(self):
        with patch('desk.ownership_signals.replay_history',side_effect=self.replay):
            r=funding_groups('mint',self.observations(),self.queries(),None,{},100)
        self.assertEqual(r['shared_sources'][0]['wallets'],['a','b'])
        self.assertEqual(r['shared_sources'][0]['classification'],'UNKNOWN_SERVICE_OR_PRIVATE_SOURCE')
        self.assertFalse(r['funding_history_complete']);self.assertFalse(r['common_control_verified'])
    def test_missing_history_is_not_zero_bundle_exposure(self):
        r=funding_groups('mint',self.observations(),[],None,{},100)
        self.assertIn('EARLY_BUYER_FUNDING_QUERY_MISSING_OR_AMBIGUOUS',r['reasons'])
        self.assertFalse(r['funding_history_complete'])
    def test_later_buyers_do_not_enter_launch_cohort(self):
        rows=self.observations();rows[1]['slot']=105
        with patch('desk.ownership_signals.replay_history',side_effect=self.replay):
            r=funding_groups('mint',rows,self.queries(),None,{},100)
        self.assertEqual(r['early_wallets'],['a']);self.assertEqual(r['shared_sources'],[])
    def test_ambiguous_funding_query_blocks_attribution(self):
        with patch('desk.ownership_signals.replay_history',side_effect=self.replay):
            r=funding_groups('mint',self.observations(),self.queries()*2,None,{},100)
        self.assertEqual(r['edges'],[]);self.assertFalse(r['common_control_verified'])
