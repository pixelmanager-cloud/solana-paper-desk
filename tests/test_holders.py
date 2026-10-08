import unittest
from desk.holders import enumerate_holders
from desk.security import base58
MINT=base58(bytes([7])*32);OWNER=base58(bytes([8])*32)
def row(i,n):return {'address':base58(bytes([i])*32),'mint':MINT,'owner':OWNER,'amount':n,'frozen':False,'delegated_amount':0}
class HolderTests(unittest.TestCase):
    def run_pages(self,pages,supply=100,slots=None):
        def rpc(method,p):
            self.assertEqual(method,'getTokenAccounts');idx=p['page']-1
            return {'token_accounts':pages[idx],'last_indexed_slot':slots[idx] if slots else 100}
        return enumerate_holders(MINT,supply,100,rpc,max_pages=len(pages),page_size=2)
    def test_reconciled_two_page_supply(self):
        r=self.run_pages([[row(1,30),row(2,50)],[row(3,20)]])
        self.assertTrue(r['coverage_verified']);self.assertEqual(r['owner_count'],1)
        self.assertEqual(r['holders'][0]['amount_raw'],'100')
    def test_budget_exhausted_does_not_claim_full_history(self):
        r=self.run_pages([[row(1,50),row(2,50)]])
        self.assertFalse(r['coverage_verified']);self.assertIn('HOLDER_PAGE_BUDGET_EXHAUSTED',r['reasons'])
    def test_duplicate_accounts_never_double_count(self):
        r=self.run_pages([[row(1,50),row(2,50)],[row(1,50)]])
        self.assertEqual(r['observed_supply_raw'],'100');self.assertFalse(r['coverage_verified'])
    def test_supply_drift_and_slot_drift_fail_closed(self):
        r=self.run_pages([[row(1,99)]],slots=[500])
        self.assertIn('HOLDER_SUPPLY_NOT_RECONCILED',r['reasons']);self.assertIn('HOLDER_SNAPSHOT_SLOT_DRIFT',r['reasons'])
    def test_wrong_mint_rejected(self):
        r=row(1,100);r['mint']=OWNER
        with self.assertRaises(ValueError):self.run_pages([[r]])
    def test_amount_precision_and_boolean_rejection(self):
        r=self.run_pages([[row(1,2**53+1)]],supply=2**53+1)
        self.assertEqual(r['observed_supply_raw'],str(2**53+1))
        with self.assertRaises(ValueError):self.run_pages([[row(1,True)]])
    def test_missing_delegation_not_assumed_zero(self):
        x=row(1,100);x.pop('delegated_amount');r=self.run_pages([[x]])
        self.assertFalse(r['coverage_verified']);self.assertIsNone(r['accounts'][0]['delegated_raw'])
        self.assertIn('HOLDER_DELEGATION_STATE_MISSING',r['reasons'])
    def test_null_delegation_remains_unknown(self):
        x=row(1,100);x['delegated_amount']=None
        self.assertFalse(self.run_pages([[x]])['coverage_verified'])
    def test_delegate_overflow_rejected(self):
        x=row(1,100);x['delegated_amount']=2**64
        with self.assertRaises(ValueError):self.run_pages([[x]])
    def test_indexed_coverage_is_not_atomic_snapshot(self):
        r=self.run_pages([[row(1,100)]])
        self.assertTrue(r['coverage_verified']);self.assertFalse(r['snapshot_atomic'])
