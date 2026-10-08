import base64,copy,unittest
from desk.security import TOKEN_PROGRAM,base58
from desk.ownership_snapshot import reconcile_snapshot
class OwnershipSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.mint=base58(bytes([1])*32);self.owner=base58(bytes([2])*32);self.key=base58(bytes([3])*32)
        m=bytearray(82);m[36:44]=(100).to_bytes(8,'little');m[45]=1
        h=bytearray(165);h[:32]=bytes([1])*32;h[32:64]=bytes([2])*32;h[64:72]=(100).to_bytes(8,'little');h[108]=1
        vals=[{'owner':TOKEN_PROGRAM,'executable':False,'data':[base64.b64encode(x).decode(),'base64']} for x in (m,h)]
        self.snapshot={'method':'getMultipleAccounts','params':[[self.mint,self.key],{'encoding':'base64','commitment':'finalized'}],
                       'result':{'context':{'slot':20},'value':vals}}
        self.clock={'method':'getBlockTime','params':[20],'result':100}
        self.history={'query_coverage_verified':True,'history_end':101,'slot_range':{'gte':0,'lt':21},
          'inventory':{'initialization_inventory_verified':True,'accounts':[{'address':self.key}]},
          'account_queries':{'reasons':[],'verified':1,'required':1},'observed_movements':{'passed':True},'block_ordering':{'reasons':[]},
          'account_continuity':{'passed':True,'end_states':[{'account':self.key,'slot':19,'closed':False,'owner':self.owner,'program':TOKEN_PROGRAM,'amount_raw':'100'}]}}
    def check(self):return reconcile_snapshot(self.mint,self.history,self.snapshot,self.clock)
    def test_matching_accounting_is_not_common_ownership_approval(self):
        r=self.check();self.assertTrue(r['reconciled']);self.assertFalse(r['eligible_for_trading']);self.assertFalse(r['common_control_verified'])
    def test_finality_and_block_time_are_required(self):
        self.snapshot['params'][1]['commitment']='confirmed'
        with self.assertRaises(ValueError):self.check()
        self.snapshot['params'][1]['commitment']='finalized';self.clock['params']=[21]
        with self.assertRaises(ValueError):self.check()
    def test_same_total_with_changed_account_history_is_rejected(self):
        self.history['account_continuity']['end_states'][0]['amount_raw']='99'
        self.assertIn('HISTORY_SNAPSHOT_BALANCE_OR_CONTROL_MISMATCH',self.check()['reasons'])
    def test_history_gap_cannot_be_hidden_by_matching_supply(self):
        self.history['account_queries']['verified']=0;self.assertFalse(self.check()['reconciled'])
    def test_history_after_snapshot_is_rejected(self):
        self.history['account_continuity']['end_states'][0]['slot']=21
        self.assertIn('HISTORY_ENDS_AFTER_SNAPSHOT',self.check()['reasons'])
    def test_time_gap_is_not_a_snapshot_match(self):
        self.history['slot_range']['lt']=20;self.assertFalse(self.check()['reconciled'])
    def test_closed_account_must_be_absent(self):
        end=self.history['account_continuity']['end_states'][0];end['closed']=True;end['amount_raw']=None
        self.assertIn('CLOSED_ACCOUNT_REAPPEARED',self.check()['reasons'])
    def test_missing_or_extra_frontier_account_rejected(self):
        self.snapshot['params'][0].append(base58(bytes([4])*32));self.snapshot['result']['value'].append(None)
        with self.assertRaises(ValueError):self.check()
