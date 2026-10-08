import copy,unittest
from desk.distribution import resolve_transfers,trace_distribution
from desk.security import TOKEN_PROGRAM,base58
class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.mint=base58(bytes([1])*32);self.a,self.b,self.c=[base58(bytes([i])*32) for i in (2,3,4)]
        self.x,self.y,self.z=[base58(bytes([i])*32) for i in (5,6,7)]
    def obs(self,slot=11,amount=10,source=None,dest=None,source_owner=None,dest_owner=None):
        source=source or self.x;dest=dest or self.y
        return {'signature':str(slot),'slot':slot,'block_time':slot,'status':'OBSERVED','commitment':'finalized_provider_response',
         'token_deltas':[{'account':source,'owner':source_owner or self.a,'mint':self.mint,'program':TOKEN_PROGRAM},{'account':dest,'owner':dest_owner or self.b,'mint':self.mint,'program':TOKEN_PROGRAM}],
         'transfers':[{'asset':'TOKEN','source':source,'destination':dest,'program':TOKEN_PROGRAM,'amount_raw':str(amount),'instruction':'0.1'}]}
    def trace(self,observations,vaults=()):
        return trace_distribution(self.mint,1000,observations,{self.a:{'slot':10,'signature':'10','instruction':'0'}},
            [{'address':self.z,'wallet':self.c,'amount_raw':'100'}],vaults)
    def test_owner_resolved_from_same_transaction_not_current_holding(self):
        r=resolve_transfers(self.mint,[self.obs()]);self.assertEqual(r['edges'][0]['source'],self.a);self.assertEqual(r['edges'][0]['source_kind'],'unknown')
    def test_conflicting_pre_post_owner_not_chosen(self):
        o=self.obs();o['token_deltas'].append({**o['token_deltas'][0],'owner':self.c})
        self.assertFalse(resolve_transfers(self.mint,[o])['edges'])
    def test_missing_destination_identity_not_guessed(self):
        o=self.obs();o['token_deltas'].pop();self.assertFalse(resolve_transfers(self.mint,[o])['edges'])
    def test_failed_transaction_cannot_create_path(self):
        o=self.obs();o['status']='FAILED';self.assertFalse(resolve_transfers(self.mint,[o])['edges'])
    def test_observed_two_hop_path_never_approves(self):
        r=self.trace([self.obs(),self.obs(12,source=self.y,dest=self.z,source_owner=self.b,dest_owner=self.c)])
        self.assertEqual(len(r['links']),2);self.assertEqual(r['observed_reached_current_gross_supply_pct'],'10');self.assertFalse(r['eligible_for_trading'])
    def test_vault_edge_excluded_by_exact_account(self):
        r=self.trace([self.obs()],(self.y,));self.assertFalse(r['links']);self.assertEqual(r['excluded_verified_vault_transfers'],1)
    def test_transfer_before_seed_buy_not_traced(self):self.assertFalse(self.trace([self.obs(9)])['links'])
    def test_same_slot_different_signature_not_ordered_lexically(self):
        second=self.obs(11,source=self.y,dest=self.z,source_owner=self.b,dest_owner=self.c);second['signature']='other'
        r=self.trace([self.obs(),second]);self.assertEqual(len(r['links']),1);self.assertIn('DISTRIBUTION_SAME_SLOT_ORDER_UNKNOWN',r['reasons'])
    def test_split_materiality_does_not_advance_at_first_dust_send(self):
        r=self.trace([self.obs(11,2),self.obs(13,3),self.obs(12,source=self.y,dest=self.z,source_owner=self.b,dest_owner=self.c)])
        self.assertEqual(len(r['links']),1)
    def test_duplicate_observation_does_not_inflate_materiality(self):
        o=self.obs(11,3);r=self.trace([o,copy.deepcopy(o)]);self.assertFalse(r['links'])
    def test_persisted_mainnet_history_owner_resolution(self):
        import json
        from pathlib import Path
        from desk.decode import decode
        p=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-distribution.json').read_text())
        obs=decode(p['payload']);obs['commitment']='finalized_provider_response'
        r=resolve_transfers(p['mint'],[obs]);self.assertEqual(r['edges'],p['expected_edges']);self.assertEqual(r['reasons'],p['expected_reasons']);self.assertFalse(r['history_complete'])
    def ordered_trace(self,observations,positions):
        return trace_distribution(self.mint,1000,observations,{self.a:{'slot':10,'signature':'10','instruction':'0'}},
            [{'address':self.z,'wallet':self.c,'amount_raw':'100'}],ordering={'proofs':[
                {'slot':11,'positions':positions,'verified':True,'evidence_hash':'persisted-test-evidence'}]})
    def test_finalized_order_allows_same_slot_relay(self):
        second=self.obs(11,source=self.y,dest=self.z,source_owner=self.b,dest_owner=self.c);second['signature']='other'
        r=self.ordered_trace([second,self.obs()],{'11':4,'other':5})
        self.assertEqual(len(r['links']),2);self.assertFalse(r['eligible_for_trading'])
    def test_same_slot_earlier_relay_is_not_reachable(self):
        second=self.obs(11,source=self.y,dest=self.z,source_owner=self.b,dest_owner=self.c);second['signature']='other'
        r=self.ordered_trace([self.obs(),second],{'11':5,'other':4})
        self.assertEqual(len(r['links']),1)
    def test_ordered_split_transfers_use_threshold_arrival(self):
        a=self.obs(11,2);b=self.obs(11,3);b['signature']='last'
        relay=self.obs(11,source=self.y,dest=self.z,source_owner=self.b,dest_owner=self.c);relay['signature']='relay'
        self.assertEqual(len(self.ordered_trace([a,b,relay],{'11':1,'relay':2,'last':3})['links']),1)
        self.assertEqual(len(self.ordered_trace([a,b,relay],{'11':1,'last':2,'relay':3})['links']),2)
    def test_earlier_indirect_arrival_not_hidden_by_later_direct_path(self):
        fourth=base58(bytes([20])*32);fourth_account=base58(bytes([21])*32)
        direct=self.obs(20)
        via_c=self.obs(11,source=self.x,dest=self.z,source_owner=self.a,dest_owner=self.c)
        back_to_b=self.obs(12,source=self.z,dest=self.y,source_owner=self.c,dest_owner=self.b)
        relay=self.obs(13,source=self.y,dest=fourth_account,source_owner=self.b,dest_owner=fourth)
        r=self.trace([direct,via_c,back_to_b,relay])
        self.assertIn(fourth,r['reached_wallets'])
        self.assertEqual(next(x['depth'] for x in r['links'] if x['destination']==fourth),3)
    def test_many_small_recipients_cannot_hide_material_outflow(self):
        r=self.trace([self.obs(11,3),self.obs(12,3,dest=self.z,dest_owner=self.c)])
        self.assertIn('FRAGMENTED_DISTRIBUTION_REQUIRES_REVIEW',r['reasons'])
        self.assertEqual(r['fragmented_outflows'][0]['amount_raw'],'6')
        self.assertEqual(r['fragmented_outflows'][0]['classification'],'SPLIT_OUTFLOW_NOT_COMMON_CONTROL_PROOF')
    def test_fragmentation_excludes_verified_vault_and_pre_buy_sends(self):
        for observations,vaults in [([self.obs(11,3),self.obs(12,3,dest=self.z,dest_owner=self.c)],(self.z,)),
                ([self.obs(9,3),self.obs(12,3,dest=self.z,dest_owner=self.c)],())]:
            self.assertFalse(self.trace(observations,vaults)['fragmented_outflows'])
    def test_small_aggregate_dust_not_material_fanout(self):
        r=self.trace([self.obs(11,2),self.obs(12,2,dest=self.z,dest_owner=self.c)])
        self.assertFalse(r['fragmented_outflows'])
