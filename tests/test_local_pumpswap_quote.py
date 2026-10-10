"""Synthetic original account captures; no provider or Jupiter calls."""
import copy
import base64
from unittest.mock import patch
import unittest

from desk.local_pumpswap_quote import quote_exact_input
from desk.live_observation import ProviderObservation
from desk.model import digest
from desk.pools import verify_pool
from tests import test_pools as pool_fixtures


class LocalQuoteTests(unittest.TestCase):
    def setUp(self):
        self.f = pool_fixtures.PoolTests(); self.f.setUp()
        self.guard = patch('socket.socket', side_effect=AssertionError('no network'))
        self.guard.start(); self.addCleanup(self.guard.stop)

    def capture(self):
        saved = []
        verify_pool(str(self.f.pool), str(self.f.mint), self.f.rpc,
                    capture=lambda p: saved.append(p) or digest(p), token_profile_version=1)
        return saved[0]

    def quote(self, direction='buy', payload=None, **changes):
        payload = self.capture() if payload is None else payload
        args = dict(capture_hash=digest(payload), expected_source_id='fixture:rpc',
                    mint=str(self.f.mint), pool=str(self.f.pool), taker=str(self.f.mint),
                    direction=direction, amount_raw=10000, slippage_bps=100,
                    now=1000, token_profile_version=1)
        args.update(changes)
        return quote_exact_input(ProviderObservation('fixture:rpc',1000,payload), **args)

    def test_exact_buy_sell_fee_rounding_and_buffer_vectors(self):
        buy, sell = self.quote(), self.quote('sell')
        self.assertEqual(buy['internal_swap_raw'], '9974')
        self.assertEqual(sell['internal_swap_raw'], '9900')
        for result in (buy, sell):
            self.assertEqual(result['estimated_output_raw'], '9875')
            self.assertEqual(result['minimum_output_raw'], '9776')
            self.assertEqual(result['fees_raw'], {'lp_fee_bps':'20','protocol_fee_bps':'5','creator_fee_bps':'0'})
            self.assertFalse(result['entry_authorized']);self.assertFalse(result['execution_verified'])
            self.assertEqual(result['execution_status'],'EXECUTION_UNVERIFIED')
        self.assertNotIn('routePlan',buy)

    def test_pinned_official_sdk_differential_vectors(self):
        # Independently produced by actual official SDK2.1.0, not this module.
        for amount,buy,sell in ((100,96,97),(1000,995,996),(10000,9875,9875),
                                (99999,90700,90680),(100000,90701,90681),
                                (500000,332777,332499)):
            with self.subTest(amount=amount):
                self.assertEqual(int(self.quote(amount_raw=amount)['estimated_output_raw']),buy)
                self.assertEqual(int(self.quote('sell',amount_raw=amount)['estimated_output_raw']),sell)

    def test_preserves_original_and_request_binding(self):
        payload=self.capture();before=copy.deepcopy(payload)
        result=self.quote(payload=payload)
        self.assertEqual(payload,before)
        self.assertEqual(result['capture_hash'],digest(before))
        self.assertEqual(result['request_hash'],digest(result['request']))
        self.assertNotEqual(result['request_hash'],self.quote('sell',payload)['request_hash'])

    def test_stale_future_source_hash_and_raw_identity_reject(self):
        for changes in ({'now':1011},{'now':999},{'expected_source_id':'other'},
                        {'capture_hash':'0'*64},{'pool':str(self.f.mint)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.quote(**changes)

    def test_boolean_u64_tiny_input_slippage_and_profile_reject(self):
        for changes in ({'amount_raw':True},{'amount_raw':0},{'amount_raw':2**64},
                        {'amount_raw':1},{'slippage_bps':True},{'slippage_bps':10000},
                        {'token_profile_version':0},{'token_profile_version':True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.quote(**changes)

    def test_known_hazards_and_delegate_not_bypassed(self):
        self.f.lp_supply=1
        with self.assertRaises(ValueError):self.quote()
        self.f.lp_supply=0;self.f.delegate=True
        with self.assertRaises(ValueError):self.quote()

    def test_corrupt_and_rehashed_snapshot_control_reject(self):
        payload=self.capture();payload['params'][0][0]=str(self.f.mint)
        with self.assertRaises(ValueError):self.quote(payload=payload)

    def test_boosted_and_negative_virtual_profiles_and_physical_capacity(self):
        from tests.test_paper_pool_compatibility import PoolCompatibilityTests
        h=PoolCompatibilityTests();h.setUp();h.virtual=1000000
        self.f=h.f
        payload=h.capture()
        result=self.quote('sell',payload,token_profile_version=2)
        self.assertGreater(int(result['estimated_output_raw']),0)
        with self.assertRaises(ValueError):self.quote('sell',payload,token_profile_version=1)
        # Same legitimate atomic state but too-large sale: SDK gross outflow
        # net of LP cannot be paid, regardless of smaller user-net estimate.
        with self.assertRaisesRegex(ValueError,'Physical quote reserves'):
            self.quote('sell',payload,token_profile_version=2,amount_raw=10**12)
        h.virtual=-300
        self.assertGreater(int(self.quote('sell',h.capture(),token_profile_version=2)['estimated_output_raw']),0)

    def test_unknown_program_and_missing_accounts_reject(self):
        for mutate in (lambda p:p['result']['value'][3].update(owner=str(self.f.mint)),
                       lambda p:p['result']['value'].pop()):
            payload=self.capture();mutate(payload)
            with self.assertRaises(ValueError):self.quote(payload=payload)

    def test_actual_token2022_lp_nonboost_profiles(self):
        from tests.test_paper_pool_compatibility import PoolCompatibilityTests
        h=PoolCompatibilityTests();h.setUp();self.f=h.f
        for profile in (1,2):
            result=self.quote(payload=h.capture(),token_profile_version=profile)
            self.assertEqual(result['request']['token_profile_version'],profile)

    def test_metadata_only_token2022_base_and_transfer_fee_rejection(self):
        from solders.pubkey import Pubkey
        from desk.security import TOKEN_2022
        from tests import test_token2022_paper as vertical, test_token2022_state as layout
        ata=Pubkey.find_program_address([bytes(self.f.pool),bytes(Pubkey.from_string(TOKEN_2022)),bytes(self.f.mint)],Pubkey.from_string('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'))[0]
        raw=bytearray(self.f.raw);raw[139:171]=bytes(ata);self.f.raw=bytes(raw)
        rpc=self.f.rpc
        def wrapped(method,params):
            result=copy.deepcopy(rpc(method,params))
            if method=='getMultipleAccounts':
                mint_raw=base64.b64decode(result['value'][6]['data'][0])
                result['value'][6]=vertical.account(vertical.mint_bytes(str(self.f.mint),mint_raw))
                vault_raw=base64.b64decode(result['value'][0]['data'][0])
                result['value'][0]=vertical.account(vault_raw+b'\x02'+layout.tlv(7))
            return result
        self.f.rpc=wrapped
        payload=self.capture()
        for profile in (1,2):self.quote(payload=payload,token_profile_version=profile)
        mint=payload['result']['value'][6]
        raw=base64.b64decode(mint['data'][0])+layout.tlv(1,b'\0'*108)
        mint['data'][0]=base64.b64encode(raw).decode()
        with self.assertRaises(ValueError):self.quote(payload=payload)


if __name__ == '__main__':unittest.main()
