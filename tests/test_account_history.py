import copy,unittest
from desk.account_history import account_inventory
from desk.decode import decode
from desk.security import TOKEN_PROGRAM,base58
from tests import test_launch
class AccountHistoryTests(unittest.TestCase):
    setUp=test_launch.LaunchTests.setUp
    def coverage(self):return {'address':self.mint,'token_accounts_filter':'none','query_coverage_verified':True,'raw_pages_persisted':True,'start':self.d['block_time']-1,'end':self.d['block_time']+1}
    def test_birth_fixture_discovers_initialized_accounts(self):
        r=account_inventory(self.mint,[self.d],self.coverage())
        self.assertTrue(r['initialization_inventory_verified']);self.assertGreater(r['account_count'],0);self.assertFalse(r['transfer_history_complete']);self.assertFalse(r['eligible_for_trading'])
    def test_closed_zero_balance_account_retained(self):
        key=base58(bytes([31])*32)
        self.d['token_account_initializations'].append({'mint':self.mint,'account':key,'owner':self.mint,'program':TOKEN_PROGRAM,'instruction':'0.99'})
        r=account_inventory(self.mint,[self.d],self.coverage());self.assertIn(key,[a['address'] for a in r['accounts']])
    def test_unpersisted_or_partial_query_cannot_verify_inventory(self):
        c=self.coverage();c['raw_pages_persisted']=False
        self.assertFalse(account_inventory(self.mint,[self.d],c)['initialization_inventory_verified'])
        c=self.coverage();c['query_coverage_verified']=False
        self.assertFalse(account_inventory(self.mint,[self.d],c)['initialization_inventory_verified'])
    def test_wallet_related_filter_not_mint_account_discovery(self):
        c=self.coverage();c['token_accounts_filter']='balanceChanged'
        self.assertIn('ACCOUNT_DISCOVERY_QUERY_NOT_VERIFIED',account_inventory(self.mint,[self.d],c)['reasons'])
    def test_missing_inner_instructions_prevent_complete_inventory(self):
        self.d['limitations'].append('INNER_INSTRUCTIONS_UNAVAILABLE')
        self.assertIn('ACCOUNT_INITIALIZATION_DECODING_INCOMPLETE',account_inventory(self.mint,[self.d],self.coverage())['reasons'])
    def test_missing_initialization_witness_is_explicit(self):
        self.d['token_account_initializations']=[]
        r=account_inventory(self.mint,[self.d],self.coverage());self.assertIn('TOKEN_ACCOUNTS_WITHOUT_INITIALIZATION_WITNESS',r['reasons'])
    def test_birth_outside_query_rejected(self):
        c=self.coverage();c['start']=self.d['block_time']+1
        self.assertFalse(account_inventory(self.mint,[self.d],c)['initialization_inventory_verified'])
    def test_unknown_token_instruction_prevents_inventory_pass(self):
        self.d['limitations'].append('UNDECODED_TOKEN_INSTRUCTION')
        self.assertFalse(account_inventory(self.mint,[self.d],self.coverage())['initialization_inventory_verified'])
