import base64,copy,json,unittest
from pathlib import Path
from desk.router import check_sell_route,route_schema,RouteReader
from desk.instructions import JUPITER
class RouterTests(unittest.TestCase):
    def setUp(self):
        p=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-sell-simulation.json').read_text())
        self.p=p;self.ix=next(x for x in p['outer'] if x['programId']==JUPITER)
        self.holding=self.ix['accounts'][2]['pubkey'];self.raw=base64.b64decode(self.ix['data'])
        schema=route_schema();types={x['name']:x['type'] for x in schema['types']}
        spec=next(x for x in schema['instructions'] if bytes(x['discriminator'])==self.raw[:8])
        reader=RouteReader(self.raw[8:],types)
        for arg in spec['args'][:-1]:reader.read(arg['type'])
        index=next(i for i,x in enumerate(types['Swap']['variants']) if x['name']=='PumpSwapSell')
        self.ix['data']=base64.b64encode(self.raw[:8+reader.pos]+(1).to_bytes(4,'little')+bytes([index])+(10000).to_bytes(2,'little')+b'\x00\x01').decode()
    def check(self):return check_sell_route(self.ix,self.p['mint'],self.p['wallet'],self.holding,int(self.p['amount_raw']),1)
    def test_narrow_direct_sell_argument_profile(self):
        r=self.check();self.assertTrue(r['passed']);self.assertFalse(r['full_route_policy_passed'])
    def test_real_multihop_route_remains_unapproved(self):
        self.ix['data']=base64.b64encode(self.raw).decode()
        r=self.check();self.assertIn('ROUTE_OUTSIDE_DIRECT_PUMPSWAP_SELL_PROFILE',r['reasons'])
        self.assertEqual(len(r['arguments']['route_plan']),3)
    def test_changed_recipient(self):
        self.ix['accounts'][5]['pubkey']=self.holding
        self.assertIn('ROUTE_IDENTITY_OR_RECIPIENT_MISMATCH',self.check()['reasons'])
    def test_changed_input_amount(self):
        raw=bytearray(base64.b64decode(self.ix['data']));raw[9:17]=(123).to_bytes(8,'little');self.ix['data']=base64.b64encode(raw).decode()
        self.assertIn('ROUTE_INPUT_AMOUNT_MISMATCH',self.check()['reasons'])
    def test_slippage_and_extra_fees(self):
        raw=bytearray(base64.b64decode(self.ix['data']));raw[25:27]=(101).to_bytes(2,'little');raw[27:29]=(1).to_bytes(2,'little');self.ix['data']=base64.b64encode(raw).decode()
        r=self.check();self.assertIn('ROUTE_SLIPPAGE_EXCEEDS_BUDGET',r['reasons']);self.assertIn('ROUTE_EXTRA_FEES_NOT_ALLOWED',r['reasons'])
    def test_unknown_trailing_bytes(self):
        self.ix['data']=base64.b64encode(base64.b64decode(self.ix['data'])+b'unknown').decode()
        self.assertIn('ROUTE_TRAILING_BYTES',self.check()['reasons'])
    def test_fixed_event_authority_mismatch(self):
        self.ix['accounts'][10]['pubkey']=self.holding
        self.assertIn('ROUTER_FIXED_ACCOUNT_MISMATCH',self.check()['reasons'])
    def test_minimum_output_bound(self):
        r=check_sell_route(self.ix,self.p['mint'],self.p['wallet'],self.holding,int(self.p['amount_raw']),10**9)
        self.assertIn('ROUTE_MINIMUM_BELOW_REQUIRED',r['reasons'])
    def test_schema_tampering_rejected(self):
        from unittest.mock import patch
        route_schema.cache_clear()
        try:
            with patch.object(Path,'read_bytes',return_value=b'{}'):
                with self.assertRaises(ValueError):route_schema()
        finally:route_schema.cache_clear()
