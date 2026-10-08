import base64,copy,unittest
from desk.debits import check_sell_debits
from desk.security import base58,TOKEN_PROGRAM
from desk.instructions import SYSTEM
from desk.programs import unbase58

class DebitTests(unittest.TestCase):
    def setUp(self):
        self.wallet,self.mint,self.holding,self.other,self.dest=[base58(bytes([n])*32) for n in range(1,6)]
        self.keys=[self.wallet,self.mint,self.holding,self.other,self.dest]
        self.value={'preBalances':[10000000,1,1,1,0],'preTokenBalances':[
            {'accountIndex':2,'owner':self.wallet,'mint':self.mint},
            {'accountIndex':3,'owner':self.wallet,'mint':self.dest}]}
        self.rows=[self.transfer(self.holding,self.dest,10)]
    def transfer(self,src,dst,n):return {'program':TOKEN_PROGRAM,'instruction':'0.1','accounts':[src,dst,self.wallet],
        'data_base64':base64.b64encode(b'\x03'+n.to_bytes(8,'little')).decode()}
    def check(self):return check_sell_debits({'inventory_checks_passed':True,'instructions':self.rows},self.value,self.keys,self.wallet,self.mint,self.holding,10,rent_exempt_lamports=2039280)
    def test_exact_gross_debit_is_partial_check_only(self):
        r=self.check();self.assertTrue(r['passed']);self.assertFalse(r['full_route_policy_passed'])
    def test_overdebit_and_refund_cannot_hide_gross_transfer(self):
        self.rows=[self.transfer(self.holding,self.dest,20),self.transfer(self.dest,self.holding,10)]
        self.assertIn('GROSS_SELL_DEBIT_MISMATCH',self.check()['reasons'])
    def test_unrelated_transfer_and_return_rejected(self):
        self.rows.extend([self.transfer(self.other,self.dest,5),self.transfer(self.dest,self.other,5)])
        self.assertIn('UNRELATED_GROSS_TOKEN_DEBIT',self.check()['reasons'])
    def test_external_native_transfer_rejected(self):
        self.rows.append({'program':SYSTEM,'instruction':'0.2','accounts':[self.wallet,self.dest],
            'data_base64':base64.b64encode((2).to_bytes(4,'little')+(1).to_bytes(8,'little')).decode()})
        self.assertIn('UNAPPROVED_GROSS_NATIVE_DEBIT',self.check()['reasons'])
    def test_wrong_authority_rejected(self):
        self.rows[0]['accounts'][-1]=self.other
        self.assertIn('GROSS_DEBIT_AUTHORITY_OR_MINT_MISMATCH',self.check()['reasons'])
    def test_extra_multisig_accounts_rejected(self):
        self.rows[0]['accounts'].append(self.other)
        self.assertIn('GROSS_DEBIT_TOKEN_LAYOUT_UNSUPPORTED',self.check()['reasons'])
    def rent(self,n=2039280,target=None):
        from solders.pubkey import Pubkey
        from desk.instructions import ATA
        from desk.providers import SOL
        ata=str(Pubkey.find_program_address([unbase58(self.wallet),unbase58(TOKEN_PROGRAM),unbase58(SOL)],Pubkey.from_string(ATA))[0])
        if ata not in self.keys:self.keys.append(ata);self.value['preBalances'].append(0)
        self.rows.append({'program':SYSTEM,'instruction':'0.3','accounts':[self.wallet,target or ata],
            'data_base64':base64.b64encode(bytes(4)+n.to_bytes(8,'little')+(165).to_bytes(8,'little')+unbase58(TOKEN_PROGRAM)).decode()})
    def test_wallet_wsol_rent_is_bounded(self):
        self.rent();self.assertTrue(self.check()['passed'])
        self.rows.pop();self.rent(5000001);self.assertIn('SELL_RENT_BUDGET_EXCEEDED',self.check()['reasons'])
    def test_external_rent_destination_rejected(self):
        self.rent(target=self.dest);self.assertIn('UNAPPROVED_RENT_RECIPIENT_OR_LAYOUT',self.check()['reasons'])
    def test_public_multihop_fixture_remains_outside_policy(self):
        import json
        from pathlib import Path
        from desk.instructions import inventory
        p=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-sell-simulation.json').read_text())
        holding=next(p['keys'][r['accountIndex']] for r in p['simulation']['preTokenBalances'] if r['owner']==p['wallet'] and r['mint']==p['mint'])
        r=check_sell_debits(inventory(p['outer'],p['simulation'],p['keys'],p['wallet']),p['simulation'],p['keys'],p['wallet'],p['mint'],holding,int(p['amount_raw']))
        self.assertEqual(r['gross_token_debit_raw'],p['amount_raw']);self.assertFalse(r['passed'])
        self.assertIn('GROSS_DEBIT_INVENTORY_UNVERIFIED',r['reasons']);self.assertFalse(r['full_route_policy_passed'])
    def test_rent_below_budget_still_must_match_chain_requirement(self):
        self.rent(2039281);self.assertIn('SELL_RENT_AMOUNT_MISMATCH',self.check()['reasons'])
    def test_missing_rent_quote_cannot_pass_creation(self):
        self.rent()
        r=check_sell_debits({'inventory_checks_passed':True,'instructions':self.rows},self.value,self.keys,self.wallet,self.mint,self.holding,10)
        self.assertIn('EXACT_SELL_RENT_UNVERIFIED',r['reasons'])
    def test_repeated_rent_creation_rejected(self):
        self.rent(100);self.rent(100)
        self.assertIn('MULTIPLE_SELL_RENT_CREATIONS',self.check()['reasons'])
