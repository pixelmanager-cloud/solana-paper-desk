import base64,copy,unittest
from solders.pubkey import Pubkey
from desk.amm import check_sell_bindings
from desk.fee_config import config_address
from desk.programs import schemas,unbase58
from desk.providers import PUMPSWAP,SOL
from desk.security import TOKEN_PROGRAM,base58
from desk.instructions import ATA,JUPITER

class AMMBindingTests(unittest.TestCase):
    def setUp(self):
        self.wallet,self.mint,self.holding,self.poolkey,self.creator,self.recipient=[base58(bytes([n])*32) for n in range(1,7)]
        def pda(seeds,program):return str(Pubkey.find_program_address(seeds,Pubkey.from_string(program))[0])
        def ata(w,m):return pda([unbase58(w),unbase58(TOKEN_PROGRAM),unbase58(m)],ATA)
        self.spec=next(x for x in schemas()[PUMPSWAP].values() if x['name']=='sell')
        self.named={x['name']:x.get('address',self.wallet) for x in self.spec['accounts']}
        creator_vault=pda([b'creator_vault',unbase58(self.creator)],PUMPSWAP)
        self.named.update(global_config=config_address(),pool=self.poolkey,user=self.wallet,base_mint=self.mint,quote_mint=SOL,
            user_base_token_account=self.holding,user_quote_token_account=ata(self.wallet,SOL),
            pool_base_token_account=ata(self.poolkey,self.mint),pool_quote_token_account=ata(self.poolkey,SOL),
            base_token_program=TOKEN_PROGRAM,quote_token_program=TOKEN_PROGRAM,
            protocol_fee_recipient=self.recipient,protocol_fee_recipient_token_account=ata(self.recipient,SOL),
            coin_creator_vault_authority=creator_vault,coin_creator_vault_ata=ata(creator_vault,SOL),
            event_authority=pda([b'__event_authority'],PUMPSWAP))
        fee=next(x for x in self.spec['accounts'] if x['name']=='fee_config')
        self.named['fee_config']=pda([bytes(x['value']) for x in fee['pda']['seeds']],self.named['fee_program'])
        self.pool={'pool':self.poolkey,'coin_creator':self.creator,'slot':100,'identity_evidence_verified':True,'liquidity_control_verified':True,
            'is_mayhem_mode':False,'global_config':{'address':config_address(),'slot':100,'configuration_complete':True,'sell_disabled':False,'fields':{'protocol_fee_recipients':[self.recipient]}},
            'vaults':[{'mint':m,'address':ata(self.poolkey,m)} for m in (self.mint,SOL)]}
        self.raw=bytes(self.spec['discriminator'])+(100).to_bytes(8,'little')+(10).to_bytes(8,'little')
    def row(self):return {'instruction':'2.0','stack_height':2,'parent_program':JUPITER,'program':PUMPSWAP,'accounts':[self.named[x['name']] for x in self.spec['accounts']], 'data_base64':base64.b64encode(self.raw).decode()}
    def check(self):return check_sell_bindings({'stack_metadata_verified':True,'instructions':[self.row()]},self.pool,self.mint,self.wallet,self.holding,100,10,101)
    def test_bound_accounts_approve_standard_fee_membership_only(self):
        r=self.check();self.assertTrue(r['passed']);self.assertFalse(r['full_route_policy_passed']);self.assertTrue(r['fee_recipient_authorization_verified'])
    def test_wrong_pool_vault_rejected(self):
        self.named['pool_base_token_account']=self.wallet;self.assertIn('AMM_ACCOUNT_BINDING_MISMATCH',self.check()['reasons'])
    def test_fee_ata_recipient_substitution_rejected(self):
        self.named['protocol_fee_recipient_token_account']=self.wallet;self.assertIn('AMM_PROTOCOL_FEE_ATA_MISMATCH',self.check()['reasons'])
    def test_creator_vault_substitution_rejected(self):
        self.named['coin_creator_vault_authority']=self.wallet;self.assertIn('AMM_CREATOR_VAULT_MISMATCH',self.check()['reasons'])
    def test_stale_or_unapproved_pool_rejected(self):
        self.pool['slot']=50;self.pool['liquidity_control_verified']=False
        self.assertIn('AMM_POOL_EVIDENCE_STALE',self.check()['reasons']);self.assertFalse(self.check()['passed'])
    def test_input_and_minimum_enforced(self):
        self.raw=bytes(self.spec['discriminator'])+(101).to_bytes(8,'little')+(9).to_bytes(8,'little')
        r=self.check();self.assertIn('AMM_INPUT_AMOUNT_MISMATCH',r['reasons']);self.assertIn('AMM_MINIMUM_OUTPUT_TOO_LOW',r['reasons'])
    def test_unknown_trailing_bytes_rejected(self):
        self.raw+=b'\x00';self.assertIn('AMM_SELL_LAYOUT_UNSUPPORTED',self.check()['reasons'])
    def test_multiple_sells_rejected(self):
        r=check_sell_bindings({'stack_metadata_verified':True,'instructions':[self.row(),self.row()]},self.pool,self.mint,self.wallet,self.holding,100,10,101)
        self.assertIn('AMM_REQUIRES_ONE_SELL_INSTRUCTION',r['reasons'])
    def test_valid_ata_for_unlisted_fee_recipient_still_rejected(self):
        self.named['protocol_fee_recipient']=self.wallet
        self.named['protocol_fee_recipient_token_account']=str(Pubkey.find_program_address([unbase58(self.wallet),unbase58(TOKEN_PROGRAM),unbase58(SOL)],Pubkey.from_string(ATA))[0])
        r=self.check();self.assertFalse(r['passed']);self.assertFalse(r['fee_recipient_authorization_verified'])
    def test_sell_disabled_configuration_rejected(self):
        self.pool['global_config']['sell_disabled']=True;self.assertFalse(self.check()['passed'])
    def test_wrong_config_slot_or_incomplete_layout_rejected(self):
        self.pool['global_config']['slot']=99;self.assertFalse(self.check()['passed'])
        self.pool['global_config']['slot']=100;self.pool['global_config']['configuration_complete']=False
        self.assertFalse(self.check()['passed'])
    def test_mayhem_recipient_profile_not_silently_approved(self):
        self.pool['is_mayhem_mode']=True;self.assertFalse(self.check()['fee_recipient_authorization_verified'])
    def test_amm_from_wrong_caller_or_unknown_stack_rejected(self):
        row=self.row();row['parent_program']=TOKEN_PROGRAM
        r=check_sell_bindings({'stack_metadata_verified':True,'instructions':[row]},self.pool,self.mint,self.wallet,self.holding,100,10,101)
        self.assertIn('AMM_SELL_CALLER_UNVERIFIED',r['reasons'])
        r=check_sell_bindings({'instructions':[self.row()]},self.pool,self.mint,self.wallet,self.holding,100,10,101)
        self.assertFalse(r['passed'])

    def modern(self):
        self.pool.update(is_cashback_coin=False,is_holder_reward=False)
        buyer=base58(bytes([9])*32)
        self.pool['global_config']['fields']['buyback_fee_recipients']=[buyer]
        pool_v2=str(Pubkey.find_program_address([b'pool-v2',unbase58(self.mint)],Pubkey.from_string(PUMPSWAP))[0])
        buyer_ata=str(Pubkey.find_program_address([unbase58(buyer),unbase58(TOKEN_PROGRAM),unbase58(SOL)],Pubkey.from_string(ATA))[0])
        row=self.row();row['accounts'] += [pool_v2,buyer,buyer_ata]
        return row
    def check_row(self,row):
        return check_sell_bindings({'stack_metadata_verified':True,'instructions':[row]},self.pool,self.mint,self.wallet,self.holding,100,10,101)
    def test_modern_standard_sell_binds_buyback_and_pool_v2(self):
        r=self.check_row(self.modern());self.assertTrue(r['passed']);self.assertTrue(r['fee_recipient_authorization_verified'])
        self.assertEqual(r['account_profile'],'STANDARD_WITH_BUYBACK');self.assertFalse(r['full_route_policy_passed'])
    def test_unlisted_buyback_wallet_and_wrong_ata_are_rejected(self):
        row=self.modern();self.pool['global_config']['fields']['buyback_fee_recipients']=[]
        self.assertIn('AMM_BUYBACK_RECIPIENT_UNAUTHORIZED_OR_UNKNOWN',self.check_row(row)['reasons'])
        row=self.modern();row['accounts'][-1]=self.wallet
        self.assertIn('AMM_BUYBACK_FEE_ATA_MISMATCH',self.check_row(row)['reasons'])
    def test_wrong_pool_v2_and_unknown_reward_profiles_are_rejected(self):
        row=self.modern();row['accounts'][-3]=self.wallet
        self.assertIn('AMM_POOL_V2_PDA_MISMATCH',self.check_row(row)['reasons'])
        row=self.modern();self.pool.pop('is_cashback_coin')
        self.assertIn('AMM_REWARD_OR_UNKNOWN_PROFILE_UNSUPPORTED',self.check_row(row)['reasons'])
    def test_cashback_additional_accounts_are_not_accepted_as_standard(self):
        row=self.modern();row['accounts'] += [self.wallet,self.wallet]
        self.assertIn('AMM_SELL_LAYOUT_UNSUPPORTED',self.check_row(row)['reasons'])
