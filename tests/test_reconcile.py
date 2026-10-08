import copy,json,unittest
from pathlib import Path
from desk.decode import decode
from desk.reconcile import reconcile_movements
from desk.security import TOKEN_PROGRAM,base58
class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.mint=base58(bytes([1])*32);self.a=base58(bytes([2])*32);self.b=base58(bytes([3])*32)
        self.obs={'status':'OBSERVED','commitment':'finalized_provider_response','limitations':[],
          'token_deltas':[{'account':self.a,'mint':self.mint,'program':TOKEN_PROGRAM,'pre_raw':'100','post_raw':'90'},
                          {'account':self.b,'mint':self.mint,'program':TOKEN_PROGRAM,'pre_raw':'0','post_raw':'10'}],
          'transfers':[{'asset':'TOKEN','source':self.a,'destination':self.b,'program':TOKEN_PROGRAM,'amount_raw':'10','instruction':'0.1'}]}
    def check(self):return reconcile_movements(self.mint,self.obs)
    def test_explicit_transfer_explains_both_balances(self):self.assertTrue(self.check()['passed'])
    def test_unexplained_debit_or_hidden_fee_rejected(self):
        self.obs['token_deltas'][0]['post_raw']='89';self.assertIn('TOKEN_MOVEMENT_BALANCE_MISMATCH',self.check()['reasons'])
    def test_new_account_requires_initialization_witness(self):
        self.obs['token_deltas'][1]['pre_raw']=None;self.assertFalse(self.check()['passed'])
        self.obs['token_account_initializations']=[{'account':self.b,'mint':self.mint,'program':TOKEN_PROGRAM}];self.assertTrue(self.check()['passed'])
    def test_closed_account_requires_close_witness(self):
        self.obs['token_deltas'][0].update(pre_raw='10',post_raw=None);self.assertFalse(self.check()['passed'])
        self.obs['token_account_closures']=[{'account':self.a}];self.assertTrue(self.check()['passed'])
    def test_mint_and_burn_are_explicit(self):
        self.obs['token_deltas'][0]['post_raw']='110';self.assertFalse(self.check()['passed'])
        self.obs['token_supply_changes']=[{'mint':self.mint,'account':self.a,'amount_raw':'25','direction':'mint','instruction':'0.2'},
            {'mint':self.mint,'account':self.a,'amount_raw':'5','direction':'burn','instruction':'0.3'}]
        self.assertTrue(self.check()['passed'])
    def test_duplicate_instruction_does_not_double_count(self):
        self.obs['transfers'].append(copy.deepcopy(self.obs['transfers'][0]));self.assertFalse(self.check()['passed'])
    def test_missing_token_instructions_never_reconciled(self):
        self.obs['limitations']=['UNDECODED_TOKEN_INSTRUCTION'];self.assertFalse(self.check()['passed'])
    def test_mainnet_transfer_fixture(self):
        p=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-distribution.json').read_text())
        obs=decode(p['payload']);obs['commitment']='finalized_provider_response'
        self.assertTrue(reconcile_movements(p['mint'],obs)['passed'])
    def test_wrapped_sol_not_misclassified_as_ordinary_mint(self):
        from desk.providers import SOL
        self.assertIn('WRAPPED_SOL_RECONCILIATION_UNSUPPORTED',reconcile_movements(SOL,self.obs)['reasons'])
