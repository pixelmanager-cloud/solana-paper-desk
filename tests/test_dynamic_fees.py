import base64,copy,json,unittest
from pathlib import Path
from desk.dynamic_fees import parse_fee_config,fee_address,fee_schema,standard_sol_fees
from desk.programs import BorshReader
class DynamicFeeTests(unittest.TestCase):
    def setUp(self):
        self.p=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-dynamic-fees.json').read_text())
        self.a=copy.deepcopy(self.p['response']['value'])
        self.config={'configuration_complete':True,'fields':{'flat_fees':{'lp_fee_bps':20,'protocol_fee_bps':5,'creator_fee_bps':0},'fee_tiers':[
            {'market_cap_lamports_threshold':100,'fees':{'lp_fee_bps':20,'protocol_fee_bps':5,'creator_fee_bps':95}},
            {'market_cap_lamports_threshold':200,'fees':{'lp_fee_bps':20,'protocol_fee_bps':5,'creator_fee_bps':45}}]}}
    def test_mainnet_documented_allocation_decodes(self):
        self.assertEqual(str(fee_address()[0]),self.p['address']);r=parse_fee_config(self.a)
        self.assertTrue(r['configuration_complete']);self.assertEqual(len(r['fields']['fee_tiers']),25)
        self.assertEqual((r['allocated_bytes'],r['decoded_bytes'],r['reserved_bytes']),(4097,2097,2000))
        self.assertFalse(standard_sol_fees(r,100,100,200,canonical=True)['fee_amounts_verified'])
    def encode(self,raw):
        self.a['data'][0]=base64.b64encode(raw).decode()
        return parse_fee_config(self.a)
    def test_unrecognized_allocations_do_not_gain_approval(self):
        raw=base64.b64decode(self.a['data'][0])
        for size in (2097,2511,2513,4072,4074,4096,4098):
            r=self.encode((raw+bytes(10))[:size])
            self.assertFalse(r['configuration_complete']);self.assertIn('DYNAMIC_FEE_ALLOCATION_UNSUPPORTED',r['reasons'])
    def version_data(self,size):
        raw=base64.b64decode(self.p['response']['value']['data'][0])
        # Actual serialized prefix: 65 + vector length + 25 forty-byte tiers.
        end=1069 if size==2512 else 2073
        return raw[:end]+bytes(size-end)
    def test_older_allocations_default_only_absent_fields(self):
        for size in (2512,4073):
            r=self.encode(self.version_data(size));self.assertTrue(r['configuration_complete'])
            self.assertEqual(len(r['fields']['stable_fee_tiers']),0 if size==2512 else 25)
            self.assertEqual(sum(r['fields']['exotic_flat_fees'].values()),0)
    def test_old_stale_capacity_is_not_read_as_new_fields(self):
        raw=bytearray(self.version_data(2512));raw[1069:1073]=(1).to_bytes(4,'little')
        r=self.encode(raw);self.assertEqual(r['fields']['stable_fee_tiers'],[])
        self.assertFalse(r['configuration_complete']);self.assertIn('DYNAMIC_FEE_NONZERO_RESERVED_BYTES',r['reasons'])
    def test_nonzero_reserved_capacity_is_unapproved(self):
        raw=bytearray(base64.b64decode(self.a['data'][0]));raw[-1]=1
        self.assertIn('DYNAMIC_FEE_NONZERO_RESERVED_BYTES',self.encode(raw)['reasons'])
    def test_vector_overrun_and_wrong_bump_are_unapproved(self):
        raw=bytearray(base64.b64decode(self.a['data'][0]));raw[65:69]=(200).to_bytes(4,'little')
        self.assertIn('DYNAMIC_FEE_LAYOUT_INCOMPLETE',self.encode(raw)['reasons'])
        raw=bytearray(base64.b64decode(self.p['response']['value']['data'][0]));raw[8]^=1
        self.assertIn('DYNAMIC_FEE_BUMP_MISMATCH',self.encode(raw)['reasons'])
    def test_tier_boundary_uses_integer_market_cap(self):
        for quote,expected in [(99,95),(100,95),(199,95),(200,45),(201,45)]:
            r=standard_sol_fees(self.config,100,100,quote,canonical=True)
            self.assertEqual(r['fees_bps']['creator_fee_bps'],expected);self.assertFalse(r['fee_amounts_verified'])
    def test_noncanonical_uses_flat_fees(self):
        r=standard_sol_fees(self.config,100,100,200,canonical=False)
        self.assertEqual(r['fees_bps']['creator_fee_bps'],0);self.assertIsNone(r['tier_threshold_lamports'])
    def test_virtual_reserves_and_zero_divisor_are_not_silently_used(self):
        with self.assertRaises(ValueError):standard_sol_fees(self.config,100,100,200,canonical=True,virtual_quote_reserves=1)
        with self.assertRaises(ValueError):standard_sol_fees(self.config,100,0,200,canonical=True)
    def test_large_raw_amounts_do_not_round_through_float(self):
        r=standard_sol_fees(self.config,2**53+1,3,7,canonical=True)
        self.assertEqual(r['market_cap_lamports'],str(7*(2**53+1)//3))
    def test_wrong_program_and_truncated_config_rejected(self):
        self.a['owner']='11111111111111111111111111111111'
        with self.assertRaises(ValueError):parse_fee_config(self.a)
        self.a=copy.deepcopy(self.p['response']['value']);self.a['data'][0]=base64.b64encode(base64.b64decode(self.a['data'][0])[:20]).decode()
        self.assertFalse(parse_fee_config(self.a)['configuration_complete'])
