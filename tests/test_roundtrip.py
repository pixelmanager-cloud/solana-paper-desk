import copy,hashlib,json,time,unittest
from pathlib import Path
from unittest.mock import patch
from solders.hash import Hash
from desk.roundtrip import simulate_roundtrip
class RoundtripTests(unittest.TestCase):
    def setUp(self):
        self.p=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-roundtrip-simulation.json').read_text())
        self.r=self.p['result'];self.sequence=self.r['sequence'];self.rows=self.sequence['result']['value']['transactionResults'];self.quotes=copy.deepcopy(self.p['quote_responses']);self.calls=[]
    def rpc(self,method,params):
        self.calls.append(method)
        if method=='getLatestBlockhash':return {'value':{'blockhash':str(Hash.default())}}
        if method=='getMultipleAccounts':
            watch=self.sequence['watch'];pre=self.rows[0]['preExecutionAccounts']
            return {'context':{'slot':self.r['slot']-1},'value':[pre[watch.index(k)] for k in params[0]]}
        self.fail('Unexpected network method')
    def quote(self,*args):return {'observed_at':int(time.time()),'response':self.quotes.pop(0)}
    def run_roundtrip(self):
        legs=[{**leg,'raw':b'fixture-parser-test'} for leg in self.p['legs']]
        # Explicit synthetic compiled-byte stand-ins must carry their own hashes;
        # the unchanged public fixture does not retain its original wire bytes.
        sequence={**self.sequence,'transaction_hashes':[hashlib.sha256(leg['raw']).hexdigest() for leg in legs]}
        with patch('desk.roundtrip.compile_unsigned',side_effect=legs),patch('desk.roundtrip.simulate_sequence',return_value=sequence):
            return simulate_roundtrip(self.r['mint'],self.r['wallet'],int(self.r['spend_lamports']),rpc=self.rpc,quote=self.quote)
    def test_captured_two_leg_effects_and_residual(self):
        r=self.run_roundtrip();self.assertTrue(r['effects_passed']);self.assertEqual(r['residual_token_raw'],'25489');self.assertEqual(r['net_native_wealth_delta_lamports'],'-143010')
        self.assertFalse(r['eligible_for_trading']);self.assertFalse(r['transaction_policy_ok']);self.assertFalse(r['submitted'])
    def test_buy_surcharge_rejected(self):
        self.rows[0]['postBalances'][0]-=1
        self.assertIn('EXACT_NATIVE_INPUT_DEBIT_MISMATCH',self.run_roundtrip()['reasons'])
    def test_wrong_quote_input_rejected(self):
        self.quotes[0]['inAmount']='1'
        with self.assertRaisesRegex(ValueError,'Buy route identity'):self.run_roundtrip()
    def test_partial_balance_metadata_blocks_effects(self):
        self.rows[1].pop('postTokenBalances')
        self.assertFalse(self.run_roundtrip()['effects_passed'])
    def test_failed_sequence_blocks_effects(self):
        self.sequence['passed']=False;self.sequence['reasons']=['SEQUENCE_EXECUTION_FAILED']
        self.assertFalse(self.run_roundtrip()['effects_passed'])
    def test_malformed_returned_states_block_effects(self):
        self.rows[1]['postExecutionAccounts'].pop()
        self.assertFalse(self.run_roundtrip()['effects_passed'])
    def test_oversized_diagnostic_never_contacts_provider(self):
        with self.assertRaises(ValueError):simulate_roundtrip(self.r['mint'],self.r['wallet'],100000001,rpc=self.rpc,quote=self.quote)
        self.assertFalse(self.calls)
    def test_real_fixture_account_state_cannot_contradict_success_metadata(self):
        self.rows[0]['postExecutionAccounts'][0]['lamports']+=1
        self.assertIn('POST_LAMPORT_STATE_MISMATCH',self.run_roundtrip()['reasons'])
    def test_post_buy_mint_authority_cannot_appear(self):
        import base64
        at=self.sequence['watch'].index(self.r['mint'])
        account=self.rows[0]['postExecutionAccounts'][at]
        raw=bytearray(base64.b64decode(account['data'][0]));raw[:4]=(1).to_bytes(4,'little');raw[4:36]=bytes([9])*32
        account['data'][0]=base64.b64encode(raw).decode()
        self.assertIn('POST_ACTIVE_MINT_AUTHORITY',self.run_roundtrip()['reasons'])
