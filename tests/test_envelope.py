import base64,copy,json,unittest
from pathlib import Path
from desk.envelope import check_sell_envelope
from desk.instructions import SYSTEM,COMPUTE

class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.p=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-sell-simulation.json').read_text())
        self.outer=self.p['outer'];self.wallet=self.p['wallet']
    def check(self):return check_sell_envelope(self.outer,self.wallet)
    def test_public_capture_setup_cleanup(self):
        r=self.check();self.assertTrue(r['passed']);self.assertFalse(r['full_route_policy_passed']);self.assertEqual(r['network_fee_bound_lamports'],9200)
    def test_close_existing_input_account_rejected(self):
        self.outer[-1]['accounts'][0]['pubkey']=self.outer[2]['accounts'][2]['pubkey']
        self.assertIn('ENVELOPE_UNAPPROVED_TOKEN_OPERATION_OR_RECIPIENT',self.check()['reasons'])
    def test_close_rent_to_external_wallet_rejected(self):
        self.outer[-1]['accounts'][1]['pubkey']=self.p['mint']
        self.assertFalse(self.check()['passed'])
    def test_setup_for_external_owner_rejected(self):
        self.outer[1]['accounts'][2]['pubkey']=self.p['mint']
        self.assertIn('ENVELOPE_UNAPPROVED_ACCOUNT_SETUP',self.check()['reasons'])
    def test_system_transfer_even_with_allowed_program_rejected(self):
        self.outer.insert(2,{'programId':SYSTEM,'data':base64.b64encode((2).to_bytes(4,'little')+(1000).to_bytes(8,'little')).decode(),'accounts':self.outer[-1]['accounts'][1:]})
        self.assertIn('ENVELOPE_UNAPPROVED_OUTER_PROGRAM',self.check()['reasons'])
    def test_missing_close_rejected(self):
        self.outer.pop();self.assertIn('ENVELOPE_REQUIRES_NATIVE_SOL_CLEANUP',self.check()['reasons'])
    def test_duplicate_route_rejected(self):
        self.outer.insert(3,copy.deepcopy(self.outer[2]));self.assertFalse(self.check()['passed'])
    def test_setup_after_swap_rejected(self):
        self.outer[1],self.outer[2]=self.outer[2],self.outer[1]
        self.assertIn('ENVELOPE_SETUP_AFTER_SWAP',self.check()['reasons'])
    def test_priority_fee_budget(self):
        self.outer[0]['data']=base64.b64encode(b'\x03'+(40000).to_bytes(8,'little')).decode()
        self.assertIn('ENVELOPE_NETWORK_FEE_EXCEEDS_BUDGET',self.check()['reasons'])
    def test_duplicate_compute_setting(self):
        self.outer.insert(0,copy.deepcopy(self.outer[0]));self.assertFalse(self.check()['passed'])
    def test_oversized_compute_limit(self):
        self.outer.insert(0,{'programId':COMPUTE,'accounts':[],'data':base64.b64encode(b'\x02'+(1400001).to_bytes(4,'little')).decode()})
        self.assertIn('ENVELOPE_COMPUTE_LIMIT_OUTSIDE_BUDGET',self.check()['reasons'])
    def test_wallet_signature_required_for_close(self):
        self.outer[-1]['accounts'][2]['isSigner']=False
        self.assertFalse(self.check()['passed'])
