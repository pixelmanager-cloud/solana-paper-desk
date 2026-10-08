import json
from pathlib import Path
import base64
import importlib.util
import unittest
from desk.security import TOKEN_PROGRAM,base58
from desk.providers import PUMPSWAP,PUMP,SOL
from desk.pools import verify_pool,parse_pool,ATA
from desk.model import digest

@unittest.skipUnless(importlib.util.find_spec('solders'),'optional solders dependency')
class PoolTests(unittest.TestCase):
    def setUp(self):
        from solders.pubkey import Pubkey
        self.Pubkey=Pubkey;self.mint=Pubkey.from_bytes(bytes([7])*32)
        self.creator=Pubkey.find_program_address([b'pool-authority',bytes(self.mint)],Pubkey.from_string(PUMP))[0]
        self.pool,self.bump=Pubkey.find_program_address([b'pool',bytes(2),bytes(self.creator),bytes(self.mint),bytes(Pubkey.from_string(SOL))],Pubkey.from_string(PUMPSWAP))
        self.lp=Pubkey.find_program_address([b'pool_lp_mint',bytes(self.pool)],Pubkey.from_string(PUMPSWAP))[0]
        self.vaults=[Pubkey.find_program_address([bytes(self.pool),bytes(Pubkey.from_string(TOKEN_PROGRAM)),bytes(m)],Pubkey.from_string(ATA))[0] for m in [self.mint,Pubkey.from_string(SOL)]]
        self.raw=bytes([241,154,109,4,17,177,109,188])+bytes([self.bump])+bytes(2)+b''.join(bytes(x) for x in [self.creator,self.mint,Pubkey.from_string(SOL),self.lp,*self.vaults])+(1000).to_bytes(8,'little')+bytes(32)
        self.global_account=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-fee-config.json').read_text())['response']['value']
        self.dynamic_account=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-dynamic-fees.json').read_text())['response']['value']
        self.lp_supply=0;self.delegate=False;self.owner=PUMPSWAP;self.wrong_vault=False
    def account(self,data,owner):return {'owner':owner,'executable':False,'data':[base64.b64encode(data).decode(),'base64']}
    def rpc(self,method,params):
        if method=='getAccountInfo':return {'context':{'slot':100},'value':self.account(self.raw,self.owner)}
        values=[]
        for mint in [self.mint,self.Pubkey.from_string(SOL)]:
            d=bytearray(165);d[:32]=bytes(mint);d[32:64]=bytes(32) if self.wrong_vault else bytes(self.pool);d[64:72]=(1000000).to_bytes(8,'little');d[108]=1;d[72]=int(self.delegate)
            values.append(self.account(d,TOKEN_PROGRAM))
        d=bytearray(82);d[:4]=(1).to_bytes(4,'little');d[4:36]=bytes(self.pool);d[36:44]=self.lp_supply.to_bytes(8,'little');d[45]=1
        values.append(self.account(d,TOKEN_PROGRAM));values.append(self.account(self.raw,self.owner));values.append(self.global_account);values.append(self.dynamic_account)
        d=bytearray(82);d[36:44]=(10**15).to_bytes(8,'little');d[44]=6;d[45]=1;values.append(self.account(d,TOKEN_PROGRAM))
        return {'context':{'slot':101},'value':values}
    def verify(self):return verify_pool(str(self.pool),str(self.mint),self.rpc,capture=digest)
    def test_exact_pool_pdas_and_vaults(self):
        r=self.verify();self.assertTrue(r['identity_verified']);self.assertTrue(r['canonical_migration_pool']);self.assertTrue(r['liquidity_control_verified'])
    def test_outstanding_lp_not_assumed_locked(self):
        self.lp_supply=100
        r=self.verify();self.assertFalse(r['liquidity_control_verified']);self.assertIn('OUTSTANDING_WITHDRAWABLE_LP_SUPPLY',r['reasons'])
    def test_wrong_program_and_vault_owner_rejected(self):
        self.owner=TOKEN_PROGRAM
        with self.assertRaises(ValueError):self.verify()
        self.owner=PUMPSWAP;self.wrong_vault=True
        with self.assertRaises(ValueError):self.verify()
    def test_delegated_vault_rejected(self):
        self.delegate=True
        with self.assertRaises(ValueError):self.verify()
    def test_unknown_trailing_bytes_never_pass_liquidity_gate(self):
        self.raw+=bytes(28)+b'unknown'
        r=self.verify();self.assertIn('POOL_LAYOUT_HAS_UNKNOWN_EXTENSION',r['reasons']);self.assertFalse(r['liquidity_control_verified'])
    def test_partial_identity_layout_rejected(self):
        self.raw=self.raw[:70]
        with self.assertRaises(ValueError):self.verify()

    def test_invalid_legacy_lp_length_does_not_pass_burn_check(self):
        def rpc(method,params):
            result=self.rpc(method,params)
            if method=='getMultipleAccounts':
                a=result['value'][2];a['data'][0]=base64.b64encode(base64.b64decode(a['data'][0])+b'padding').decode()
            return result
        r=verify_pool(str(self.pool),str(self.mint),rpc,capture=digest)
        self.assertFalse(r['liquidity_control_verified']);self.assertIn('INVALID_LP_MINT_LAYOUT',r['reasons'])
    def test_executable_vault_is_rejected(self):
        def rpc(method,params):
            result=self.rpc(method,params)
            if method=='getMultipleAccounts':result['value'][0]['executable']=True
            return result
        with self.assertRaises(ValueError):verify_pool(str(self.pool),str(self.mint),rpc,capture=digest)
    def test_changed_pool_between_requests_rejected(self):
        def rpc(method,params):
            r=self.rpc(method,params)
            if method=='getMultipleAccounts':
                data=bytearray(base64.b64decode(r['value'][3]['data'][0]));data[211]^=1
                r['value'][3]['data'][0]=base64.b64encode(data).decode()
            return r
        with self.assertRaises(ValueError):verify_pool(str(self.pool),str(self.mint),rpc,capture=digest)
    def test_pool_is_in_same_snapshot_as_vaults_and_lp(self):
        saved=[]
        def rpc(method,params):
            if method=='getMultipleAccounts':self.assertEqual(params[0][:4],list(map(str,[*self.vaults,self.lp,self.pool])));self.assertEqual(len(params[0]),7)
            return self.rpc(method,params)
        def capture(e):saved.append(e);return digest(e)
        r=verify_pool(str(self.pool),str(self.mint),rpc,capture=capture)
        self.assertTrue(r['snapshot_atomic']);self.assertTrue(r['identity_evidence_verified'])
        self.assertEqual(r['evidence_hash'],digest(saved[0]));self.assertEqual(len(saved[0]['result']['value']),7)
    def test_backward_snapshot_slot_cannot_verify_liquidity(self):
        def rpc(method,params):
            r=self.rpc(method,params)
            if method=='getMultipleAccounts':r['context']['slot']=99
            return r
        r=verify_pool(str(self.pool),str(self.mint),rpc,capture=digest)
        self.assertFalse(r['liquidity_control_verified']);self.assertIn('POOL_SNAPSHOT_SLOT_DRIFT',r['reasons'])
    def test_missing_persistence_cannot_verify_liquidity(self):
        r=verify_pool(str(self.pool),str(self.mint),self.rpc)
        self.assertFalse(r['liquidity_control_verified']);self.assertFalse(r['identity_evidence_verified'])
    def test_partial_atomic_batch_rejected(self):
        def rpc(method,params):
            r=self.rpc(method,params)
            if method=='getMultipleAccounts':r['value'].pop()
            return r
        with self.assertRaises(ValueError):verify_pool(str(self.pool),str(self.mint),rpc,capture=digest)
    def test_mainnet_atomic_pool_stays_unapproved(self):
        import json
        from pathlib import Path
        p=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-pool-fee-snapshot.json').read_text())
        def rpc(method,params):
            return p['capture']['discovery'] if method=='getAccountInfo' else p['capture']['result']
        r=verify_pool(p['pool'],p['mint'],rpc,capture=digest)
        self.assertTrue(r['identity_evidence_verified']);self.assertTrue(r['snapshot_atomic'])
        self.assertFalse(r['liquidity_control_verified']);self.assertIn('VIRTUAL_RESERVES_REQUIRE_SPECIAL_PRICING',r['reasons'])

    def test_documented_zero_pool_capacity_supported(self):
        for size in (287,300,301):
            raw=self.raw+bytes(size-len(self.raw))
            fields=parse_pool(self.account(raw,self.owner))
            self.assertEqual(fields['unknown_trailing_bytes'],0)
            self.assertEqual(fields['protocol_fees'],0)
    def test_unrecognized_or_nonzero_pool_capacity_stays_unknown(self):
        for size in (272,280,288,299,302):
            fields=parse_pool(self.account(self.raw+bytes(size-len(self.raw)),self.owner))
            self.assertGreater(fields['unknown_trailing_bytes'],0)
        raw=self.raw+bytes(300-len(self.raw))+b'X'
        self.assertGreater(parse_pool(self.account(raw,self.owner))['unknown_trailing_bytes'],0)
    def test_accrued_fees_cannot_be_used_as_trading_reserves(self):
        for offset in (271,279):
            raw=bytearray(self.raw+bytes(301-len(self.raw)));raw[offset:offset+8]=(123).to_bytes(8,'little');self.raw=bytes(raw)
            r=self.verify();self.assertFalse(r['liquidity_control_verified'])
            self.assertIn('ACCRUED_POOL_FEES_REQUIRE_RESERVE_ADJUSTMENT',r['reasons'])

    def test_fee_schedule_and_mint_are_bound_to_atomic_snapshot(self):
        from desk.dynamic_fees import fee_address
        def rpc(method,params):
            if method=='getMultipleAccounts':self.assertEqual(params[0][5:],[str(fee_address()[0]),str(self.mint)])
            return self.rpc(method,params)
        r=verify_pool(str(self.pool),str(self.mint),rpc,capture=digest)
        self.assertTrue(r['dynamic_fee_config']['configuration_complete'])
        self.assertEqual(r['base_mint_policy']['supply_raw'],str(10**15))
        self.assertEqual(r['dynamic_fee_config']['slot'],r['base_mint_policy']['slot'])
    def test_atomic_mint_authority_and_unknown_fees_block_liquidity(self):
        for index,offset in ((6,0),(5,-1)):
            def rpc(method,params):
                r=self.rpc(method,params)
                if method=='getMultipleAccounts':
                    a=r['value'][index];d=bytearray(base64.b64decode(a['data'][0]));d[offset]=1;a['data'][0]=base64.b64encode(d).decode()
                return r
            r=verify_pool(str(self.pool),str(self.mint),rpc,capture=digest)
            self.assertFalse(r['liquidity_control_verified'])
    def test_missing_atomic_fee_account_is_rejected(self):
        def rpc(method,params):
            r=self.rpc(method,params)
            if method=='getMultipleAccounts':r['value'][5]=None
            return r
        with self.assertRaises(ValueError):verify_pool(str(self.pool),str(self.mint),rpc,capture=digest)
    def test_reward_and_creator_override_profiles_cannot_pass_liquidity(self):
        original=self.raw
        for offset,reason in ((244,'CASHBACK_POOL_REQUIRES_FEE_POLICY'),(270,'HOLDER_REWARD_POOL_REQUIRES_FEE_POLICY'),(261,'POOL_CREATOR_FEE_OVERRIDE_REQUIRES_POLICY')):
            raw=bytearray(original+bytes(301-len(original)));raw[offset]=1;self.raw=bytes(raw)
            r=self.verify();self.assertFalse(r['liquidity_control_verified']);self.assertIn(reason,r['reasons'])
