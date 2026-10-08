import copy, unittest
from desk.continuity import reconcile_history
from desk.security import TOKEN_PROGRAM,base58

class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.mint,self.a,self.owner=[base58(bytes([n])*32) for n in (1,2,3)]
    def event(self,slot,pre,post,init=False,close=False,owner=None):
        owner=owner or self.owner
        amount=int(post or 0)-int(pre or 0)
        return {'signature':str(slot),'slot':slot,'status':'OBSERVED','commitment':'finalized_provider_response',
            'token_deltas':[{'account':self.a,'mint':self.mint,'owner':owner,'program':TOKEN_PROGRAM,'pre_raw':pre,'post_raw':post}],
            'token_account_initializations':[{'account':self.a,'mint':self.mint,'owner':owner,'program':TOKEN_PROGRAM}] if init else [],
            'token_account_closures':[{'account':self.a}] if close else [],
            'token_supply_changes':[{'account':self.a,'mint':self.mint,'amount_raw':str(abs(amount)),
                'direction':'mint' if amount>0 else 'burn','instruction':'0'}] if amount else []}
    def check(self,*events):return reconcile_history(self.mint,list(events))
    def test_continuous_lifetime_does_not_grant_complete_history(self):
        r=self.check(self.event(1,None,'100',True),self.event(2,'100','90'))
        self.assertTrue(r['passed']);self.assertEqual(r['end_states'][0]['amount_raw'],'90')
        self.assertFalse(r['transfer_history_complete']);self.assertFalse(r['eligible_for_trading'])
    def test_individually_balanced_transactions_can_hide_gap(self):
        r=self.check(self.event(1,None,'100',True),self.event(2,'99','90'))
        self.assertIn('HISTORY_BALANCE_DISCONTINUITY',r['reasons'])
    def test_history_without_birth_is_unknown(self):
        self.assertIn('HISTORY_INITIAL_STATE_UNKNOWN',self.check(self.event(1,'100','90'))['reasons'])
    def test_same_slot_does_not_sort_by_signature(self):
        a=self.event(1,None,'100',True);b=self.event(1,'100','90');b['signature']='different'
        self.assertIn('HISTORY_SAME_SLOT_ORDER_UNKNOWN',self.check(a,b)['reasons'])
    def test_close_and_later_recreate_can_be_witnessed(self):
        r=self.check(self.event(1,None,'100',True),self.event(2,'100',None,close=True),self.event(3,None,'50',True,owner=self.mint))
        self.assertTrue(r['passed'])
    def test_missing_reopen_is_unknown(self):
        r=self.check(self.event(1,None,'0',True),self.event(2,'0',None,close=True),self.event(3,'0','50'))
        self.assertIn('HISTORY_ACCOUNT_REOPEN_UNWITNESSED',r['reasons'])
    def test_owner_changed_between_records_rejected(self):
        r=self.check(self.event(1,None,'100',True),self.event(2,'100','90',owner=self.mint))
        self.assertIn('HISTORY_ACCOUNT_CONTROL_CHANGED',r['reasons'])
    def test_transient_account_retained(self):
        e=self.event(1,None,None,True,True);e['token_deltas']=[]
        r=self.check(e);self.assertTrue(r['passed']);self.assertTrue(r['end_states'][0]['closed'])
    def test_duplicate_records_rejected(self):
        e=self.event(1,None,'100',True)
        self.assertIn('HISTORY_DUPLICATE_TRANSACTION',self.check(e,copy.deepcopy(e))['reasons'])
    def test_failed_transaction_cannot_bridge_gap(self):
        e=self.event(2,'100','90');e['status']='FAILED'
        r=self.check(self.event(1,None,'100',True),e,self.event(3,'90','80'))
        self.assertIn('HISTORY_BALANCE_DISCONTINUITY',r['reasons'])
    def test_unknown_owner_rejected(self):
        e=self.event(1,None,'100',True);e['token_deltas'][0]['owner']=None
        self.assertIn('HISTORY_OWNER_UNKNOWN',self.check(e)['reasons'])
