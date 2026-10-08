import unittest
from desk.account_history import collect_account_histories
from desk.model import digest
from desk.security import base58
from tests.test_decode import transaction
class AccountCollectionTests(unittest.TestCase):
    def setUp(self):
        self.mint=base58(bytes([1])*32);self.keys=[base58(bytes([n])*32) for n in (2,3,4)]
        self.inventory={'mint':self.mint,'initialization_inventory_verified':True,'history_start':0,'history_end':20,
            'accounts':[{'address':x} for x in self.keys[:2]]}
        self.calls=[];self.page={'data':[]};self.capture=digest
    def rpc(self,method,params):
        self.calls.append(params);self.assertEqual(params[1]['filters']['tokenAccounts'],'none');return self.page
    def collect(self,seed=()):return collect_account_histories(self.inventory,list(seed),self.rpc,capture=self.capture,max_accounts=2)
    def test_complete_queries_do_not_approve_transfer_history(self):
        r=self.collect();self.assertTrue(r['all_required_account_queries_verified']);self.assertFalse(r['transfer_history_complete']);self.assertFalse(r['eligible_for_trading']);self.assertEqual(len(self.calls),2)
    def test_unverified_inventory_uses_no_provider_calls(self):
        self.inventory['initialization_inventory_verified']=False
        self.assertFalse(self.collect()['all_required_account_queries_verified']);self.assertFalse(self.calls)
    def test_account_budget_preserved(self):
        self.inventory['accounts'].append({'address':self.keys[2]})
        r=self.collect();self.assertEqual(len(self.calls),2);self.assertEqual(r['unqueried_accounts'],[self.keys[2]])
    def test_cursor_page_limit_blocks_component_coverage(self):
        self.page['paginationToken']='next';r=self.collect()
        self.assertFalse(r['all_required_account_queries_verified']);self.assertIn('HISTORY_RANGE_NOT_EXHAUSTED',r['reasons'])
    def test_unpersisted_pages_cannot_pass(self):
        self.capture=None;self.assertFalse(self.collect()['all_required_account_queries_verified'])
    def test_provider_failure_leaves_explicit_gap(self):
        def failed(*args):raise ValueError('provider unavailable')
        self.rpc=failed;r=self.collect();self.assertIn('ACCOUNT_HISTORY_QUERY_FAILED',r['reasons'])
    def test_new_endpoint_extends_unresolved_frontier(self):
        obs={'signature':'seed','payload_hash':'a','slot':1,'token_deltas':[{'mint':self.mint,'account':self.keys[2]}]}
        self.assertIn('HISTORICAL_ACCOUNT_FRONTIER_NOT_CLOSED',self.collect([obs])['reasons'])
    def test_conflicting_observations_quarantined_not_chosen(self):
        a={'signature':'seed','payload_hash':'a','slot':1};b={**a,'payload_hash':'b'}
        r=self.collect([a,b]);self.assertFalse(r['observations']);self.assertIn('ACCOUNT_HISTORY_CONFLICTING_TRANSACTION',r['reasons'])
