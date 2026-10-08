import base64,unittest
from desk.holders import verify_holder_snapshot
from desk.model import digest
from desk.security import TOKEN_PROGRAM,base58
class HolderSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.mint=base58(bytes([1])*32);self.owner=base58(bytes([2])*32);self.key=base58(bytes([3])*32)
        self.e={'mint':self.mint,'coverage_verified':True,'supply_raw':'100','indexed_slot_max':10,
            'accounts':[{'address':self.key,'wallet':self.owner,'amount_raw':'100','frozen':False,'delegated_raw':'0'}]}
        self.m=bytearray(82);self.m[36:44]=(100).to_bytes(8,'little');self.m[45]=1
        self.h=bytearray(165);self.h[:32]=bytes([1])*32;self.h[32:64]=bytes([2])*32;self.h[64:72]=(100).to_bytes(8,'little');self.h[108]=1
        self.slot=11;self.calls=[]
    def rpc(self,method,params):
        self.calls.append((method,params));return {'context':{'slot':self.slot},'value':[
            {'owner':TOKEN_PROGRAM,'executable':False,'data':[base64.b64encode(x).decode(),'base64']} for x in (self.m,self.h)]}
    def check(self):return verify_holder_snapshot(self.e,self.rpc,capture=digest)
    def test_one_context_checks_mint_and_every_account(self):
        r=self.check();self.assertTrue(r['verified']);self.assertTrue(r['snapshot_atomic']);self.assertEqual(self.calls[0][1][0],[self.mint,self.key])
    def test_balance_changed_since_index_is_unknown(self):
        self.h[64:72]=(99).to_bytes(8,'little');r=self.check();self.assertFalse(r['verified']);self.assertIn('INDEXED_HOLDER_STATE_CHANGED',r['reasons'])
    def test_owner_change_rejected(self):
        self.h[32:64]=bytes([4])*32;self.assertFalse(self.check()['verified'])
    def test_supply_burn_between_reads_rejected(self):
        self.m[36:44]=(99).to_bytes(8,'little');self.assertIn('SNAPSHOT_MINT_SUPPLY_CHANGED',self.check()['reasons'])
    def test_delegate_with_zero_allowance_is_visible(self):
        self.h[72:76]=(1).to_bytes(4,'little');self.h[76:108]=bytes([4])*32
        self.assertEqual(self.check()['delegated_accounts'],[self.key])
    def test_slot_gap_rejected(self):
        self.slot=100;self.assertFalse(self.check()['verified'])
    def test_unverified_index_does_not_call_provider(self):
        self.e['coverage_verified']=False;self.assertFalse(self.check()['verified']);self.assertFalse(self.calls)
    def test_budget_never_expands_to_multiple_snapshots(self):
        self.e['accounts']=self.e['accounts']*100;self.assertFalse(self.check()['verified']);self.assertFalse(self.calls)
    def test_mainnet_single_bank_snapshot_replay(self):
        import json
        from pathlib import Path
        p=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-holder-snapshot.json').read_text())
        r=verify_holder_snapshot(p['enumeration'],lambda *args:p['rpc']['response'],capture=digest)
        self.assertTrue(r['verified']);self.assertEqual(r['account_count'],17);self.assertEqual(r['observed_supply_raw'],'800017057543498')
    def test_unpersisted_snapshot_cannot_verify_live_coverage(self):
        r=verify_holder_snapshot(self.e,self.rpc)
        self.assertFalse(r['verified']);self.assertFalse(r['raw_evidence_persisted'])
        self.assertIn('HOLDER_SNAPSHOT_EVIDENCE_NOT_PERSISTED',r['reasons'])
    def test_bad_capture_hash_rejected(self):
        with self.assertRaisesRegex(ValueError,'evidence hash'):
            verify_holder_snapshot(self.e,self.rpc,capture=lambda p:'wrong')
    def test_raw_response_and_request_are_replayable(self):
        saved=[]
        def capture(p):saved.append(p);return digest(p)
        r=verify_holder_snapshot(self.e,self.rpc,capture=capture)
        self.assertTrue(r['verified']);self.assertTrue(r['raw_evidence_persisted'])
        self.assertEqual(r['evidence_hash'],digest(saved[0]));self.assertEqual(saved[0]['params'][0],[self.mint,self.key])
        self.assertEqual(saved[0]['result']['context']['slot'],11)
