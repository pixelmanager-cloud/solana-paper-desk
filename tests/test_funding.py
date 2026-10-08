import copy,unittest
from desk.funding import observed_funding
class FundingTests(unittest.TestCase):
    def obs(self,signature='a',slot=9,at=100,amount='1000000'):
        return {'signature':signature,'slot':slot,'block_time':at,'status':'OBSERVED','commitment':'finalized_provider_response','transfers':[{'asset':'SOL','source':'source','destination':'buyer','amount_raw':amount,'instruction':'0'}]}
    def run_check(self,observations):return observed_funding(observations,'buyer',{'slot':10,'block_time':100,'signature':'buy','commitment':'finalized_provider_response'})
    def test_same_second_later_slot_is_not_prebuy_funding(self):
        self.assertFalse(self.run_check([self.obs(slot=11)])['edges'])
    def test_same_slot_is_explicitly_ambiguous(self):
        r=self.run_check([self.obs(slot=10)]);self.assertFalse(r['edges']);self.assertEqual(len(r['ambiguous_transfers']),1)
        self.assertIn('FUNDING_SAME_SLOT_ORDER_UNVERIFIED',r['reasons'])
    def test_earlier_slot_keeps_source_unknown(self):
        r=self.run_check([self.obs()]);self.assertEqual(r['edges'][0]['source_kind'],'unknown');self.assertFalse(r['funding_history_complete'])
    def test_split_transfers_cross_floor_in_aggregate(self):
        r=self.run_check([self.obs('a',amount='600000'),self.obs('b',amount='500000')])
        self.assertEqual(r['edges'][0]['amount_raw'],'1100000');self.assertTrue(r['edges'][0]['fragmented'])
    def test_duplicate_observations_do_not_inflate_funding(self):
        o=self.obs(amount='600000');self.assertFalse(self.run_check([o,copy.deepcopy(o)])['edges'])
    def test_conflicting_signature_is_quarantined(self):
        r=self.run_check([self.obs(),self.obs(amount='2000000')]);self.assertFalse(r['edges']);self.assertIn('FUNDING_CONFLICTING_OBSERVATIONS',r['reasons'])
    def test_failed_future_and_self_transfers_are_excluded(self):
        failed=self.obs();failed['status']='FAILED';self_transfer=self.obs('self');self_transfer['transfers'][0]['source']='buyer'
        self.assertFalse(self.run_check([failed,self_transfer,self.obs('future',at=101)])['edges'])
    def test_unknown_time_and_invalid_amount_remain_unknown(self):
        r=self.run_check([self.obs(at=None),self.obs('bad',amount='-1')]);self.assertFalse(r['edges'])
        self.assertIn('FUNDING_CHAIN_TIME_UNKNOWN',r['reasons']);self.assertIn('FUNDING_AMOUNT_INVALID',r['reasons'])

    def ordered(self,proof,obs=None):
        buy={'slot':10,'block_time':100,'signature':'buy','commitment':'finalized_provider_response'}
        return observed_funding(obs or [self.obs(slot=10)],'buyer',buy,ordering={'proofs':proof})
    def proof(self):
        return {'slot':10,'block_time':100,'verified':True,'evidence_hash':'a'*64,'positions':{'a':2,'buy':3}}
    def test_persisted_block_position_resolves_prior_same_slot_transfer(self):
        r=self.ordered([self.proof()]);self.assertEqual(len(r['edges']),1)
        self.assertEqual(r['edges'][0]['ordering'],'FINALIZED_BLOCK_POSITION')
        self.assertEqual(r['edges'][0]['transfers'][0]['ordering_evidence_hash'],'a'*64)
    def test_later_same_slot_transfer_is_excluded_by_chain_order(self):
        p=self.proof();p['positions']['a']=4;r=self.ordered([p]);self.assertFalse(r['edges']);self.assertFalse(r['ambiguous_transfers'])
    def test_same_transaction_is_not_resolved_by_block_position(self):
        p=self.proof();r=self.ordered([p],[self.obs('buy',slot=10)]);self.assertFalse(r['edges']);self.assertTrue(r['ambiguous_transfers'])
    def test_missing_conflicting_or_malformed_proofs_never_promote_order(self):
        for field,value in [('evidence_hash',None),('block_time',99),('positions',{'a':2,'buy':2}),('verified',False)]:
            p=self.proof();p[field]=value;self.assertFalse(self.ordered([p])['edges'])
        p=self.proof();p['positions']['a']=4;self.assertFalse(self.ordered([self.proof(),p])['edges'])
    def test_unfinalized_observation_cannot_be_funding_evidence(self):
        obs=self.obs();obs['commitment']='confirmed';r=self.run_check([obs])
        self.assertFalse(r['edges']);self.assertIn('FUNDING_OBSERVATION_FINALITY_UNVERIFIED',r['reasons'])

    def test_funding_must_precede_all_observed_buys_in_first_slot(self):
        buy={'slot':10,'block_time':100,'signature':'later-buy','same_slot_buy_signatures':['first-buy','later-buy'],'commitment':'finalized_provider_response'}
        proof=self.proof();proof['positions']={'first-buy':1,'a':2,'later-buy':3}
        r=observed_funding([self.obs(slot=10)],'buyer',buy,ordering={'proofs':[proof]})
        self.assertFalse(r['edges'])
        del proof['positions']['first-buy']
        r=observed_funding([self.obs(slot=10)],'buyer',buy,ordering={'proofs':[proof]})
        self.assertFalse(r['edges']);self.assertTrue(r['ambiguous_transfers'])
