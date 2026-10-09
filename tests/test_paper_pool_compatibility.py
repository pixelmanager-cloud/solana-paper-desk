"""SYNTHETIC_TEST_ONLY: canonical LP/nonboost fee accounting, never live proof."""
import base64
import copy
from decimal import Decimal
import unittest
from unittest.mock import patch
from desk import pools
from desk.live_observation import ProviderObservation, ingest_pool, ObservationError
from desk.model import digest
from desk.security import TOKEN_2022
from tests import test_pools as fixtures, test_token2022_paper as vertical


class PoolCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.PoolTests();self.f.setUp()
        self.original_rpc=self.f.rpc
        self.lp_change=lambda account:None
        self.protocol_fee=100;self.creator_fee=200;self.virtual=-300

    def rpc(self,method,params):
        f=self.f;raw=bytearray(f.raw+bytes(301-len(f.raw)))
        raw[245:261]=self.virtual.to_bytes(16,'little',signed=True)
        raw[271:279]=self.protocol_fee.to_bytes(8,'little')
        raw[279:287]=self.creator_fee.to_bytes(8,'little')
        original=f.raw
        try:
            f.raw=bytes(raw);result=copy.deepcopy(self.original_rpc(method,params))
        finally:f.raw=original
        if method=='getMultipleAccounts':
            lp=result['value'][2];lp['owner']=TOKEN_2022
            d=bytearray(base64.b64decode(lp['data'][0]));d[44]=9
            lp['data'][0]=base64.b64encode(d).decode();self.lp_change(lp)
        return result

    def verify(self,profile=1):
        return pools.verify_pool(str(self.f.pool),str(self.f.mint),self.rpc,capture=digest,token_profile_version=profile)

    def capture(self):
        saved=[]
        pools.verify_pool(str(self.f.pool),str(self.f.mint),self.rpc,capture=lambda raw:saved.append(raw) or digest(raw),token_profile_version=1)
        return saved[0]

    def ingest(self,raw,profile=1):
        return ingest_pool(lambda:ProviderObservation('synthetic-pool',100,raw),mint=str(self.f.mint),pool=str(self.f.pool),now=100,token_profile_version=profile)

    def test_exact_lp_and_proven_nonboost_fees_use_net_once_preserve_gross(self):
        raw=self.capture();original=copy.deepcopy(raw);r=self.verify()
        self.assertTrue(r['liquidity_control_verified'],r['reasons'])
        self.assertEqual(r['gross_quote_reserve_raw'],'1000000')
        self.assertEqual(r['virtual_quote_reserves_raw'],'-300');self.assertEqual(r['boost_reserves_raw'],'0')
        for field in ('quote_reserve_raw','spendable_quote_reserve_raw','effective_quote_reserve_raw'):
            self.assertEqual(r[field],'999700')
        observed=self.ingest(raw)
        self.assertEqual(observed.reserve_lamports,999700)
        self.assertEqual(observed.reserve_sol,Decimal('0.000999700'))
        self.assertEqual(observed.gross_reserve_lamports,1000000)
        self.assertEqual(observed.accrued_protocol_fees_lamports,100)
        self.assertEqual(observed.accrued_creator_fees_lamports,200)
        self.assertEqual(observed.source.raw_hash,digest(original));self.assertEqual(raw,original)
        self.assertIn('ACCRUED_POOL_FEES_REQUIRE_RESERVE_ADJUSTMENT',self.verify(0)['reasons'])
        with self.assertRaises(ObservationError):self.ingest(raw,0)

    def test_lp_hazardous_or_unknown_layouts_never_pass(self):
        for change in ('length81','length83','extension','owner','executable','decimals','initialized','authority_option','authority','freeze','supply'):
            with self.subTest(change=change):
                def mutate(a):
                    d=bytearray(base64.b64decode(a['data'][0]))
                    if change=='length81':d=d[:-1]
                    elif change=='length83':d+=b'\0'
                    elif change=='extension':d+=bytes(83)+b'\x01'+b'\x12\0\x40\0'+bytes(64)
                    elif change=='owner':a['owner']=str(self.f.mint)
                    elif change=='executable':a['executable']=True
                    elif change=='decimals':d[44]=6
                    elif change=='initialized':d[45]=2
                    elif change=='authority_option':d[:4]=(2).to_bytes(4,'little')
                    elif change=='authority':d[4:36]=bytes(32)
                    elif change=='freeze':d[46:50]=(1).to_bytes(4,'little')
                    elif change=='supply':d[36:44]=(1).to_bytes(8,'little')
                    a['data'][0]=base64.b64encode(d).decode()
                self.lp_change=mutate
                r=self.verify();self.assertFalse(r['liquidity_control_verified'])
                with self.assertRaises(ObservationError):self.ingest(self.capture())

    def test_fee_underflow_zero_and_u64_extremes_block_physical_capacity(self):
        for fee in (1000000,1000001,2**64-1):
            self.protocol_fee=fee;self.creator_fee=0;self.virtual=-fee
            r=self.verify();self.assertIn('POOL_QUOTE_FEE_BALANCE_INVALID',r['reasons'])
            self.assertFalse(r['liquidity_control_verified'])
            with self.assertRaises(ObservationError):self.ingest(self.capture())

    def test_boost_signed_boundaries_and_zero_virtual_with_fees_stay_blocked(self):
        for virtual_quote in (0,1,-299,-301,2**127-1,-2**127):
            self.virtual=virtual_quote
            r=self.verify();self.assertIn('BOOST_POOL_UNSUPPORTED',r['reasons'])
            self.assertFalse(r['liquidity_control_verified'])
            with self.assertRaises(ObservationError):self.ingest(self.capture())

    def test_each_fee_bucket_and_zero_fee_profile(self):
        for protocol,creator in ((0,0),(100,0),(0,100),(100,200)):
            self.protocol_fee=protocol;self.creator_fee=creator;self.virtual=-(protocol+creator)
            self.assertEqual(self.verify()['quote_reserve_raw'],str(1000000-protocol-creator))
            self.assertTrue(self.verify()['liquidity_control_verified'])

    def test_lp_and_fee_support_do_not_waive_other_pool_hazards(self):
        original=self.f.raw
        for offset in (243,244,261,269,270,300):
            raw=bytearray(original+bytes(301-len(original)));raw[offset]=1;self.f.raw=bytes(raw)
            self.assertFalse(self.verify()['liquidity_control_verified'])
            with self.assertRaises(ObservationError):self.ingest(self.capture())
        self.f.raw=original


class ActualCycleCompatibilityTests(unittest.TestCase):
    def test_actual_entry_monitor_exit_restart_with_token2022_lp_and_net_reserves(self):
        f=vertical.VerticalProfileTests();f.setUp();self.addCleanup(f.doCleanups)
        protocol=f.f.f.protocol
        raw=bytearray(protocol.raw+bytes(301-len(protocol.raw)))
        raw[245:261]=(-3000000).to_bytes(16,'little',signed=True)
        raw[271:279]=(1000000).to_bytes(8,'little');raw[279:287]=(2000000).to_bytes(8,'little')
        protocol.raw=bytes(raw);original=protocol.rpc
        def rpc(method,params):
            result=original(method,params)
            if method=='getMultipleAccounts':
                lp=result['value'][2];d=bytearray(base64.b64decode(lp['data'][0]));d[44]=9
                lp['owner']=TOKEN_2022;lp['data'][0]=base64.b64encode(d).decode()
            return result
        protocol.rpc=rpc
        from desk import paper_market_adapter
        observations=[];original_ingest=paper_market_adapter.ingest_pool
        def checked_ingest(*args,**kwargs):
            observed=original_ingest(*args,**kwargs);observations.append(observed)
            fees=observed.accrued_protocol_fees_lamports+observed.accrued_creator_fees_lamports
            self.assertEqual(fees,3000000)
            self.assertEqual(observed.reserve_lamports,observed.gross_reserve_lamports-fees)
            self.assertEqual(observed.reserve_lamports,observed.gross_reserve_lamports+observed.virtual_quote_reserves_lamports)
            self.assertEqual(observed.reserve_sol,Decimal(observed.reserve_lamports)/Decimal(10**9))
            return observed
        with patch.object(paper_market_adapter,'ingest_pool',side_effect=checked_ingest):
            f.test_actual_entry_mark_exit_restart_and_costs()
        self.assertGreaterEqual(len(observations),3)  # Entry, held mark, exit; replay also checked.

