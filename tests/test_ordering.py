import copy, unittest
from desk.ordering import block_order,collect_ordering
from desk.model import digest
from desk.security import base58
from tests import test_continuity

class OrderingTests(unittest.TestCase):
    def setUp(self):
        self.helper=test_continuity.ContinuityTests();self.helper.setUp();self.mint=self.helper.mint
        self.a=self.helper.event(10,None,'100',True);self.b=self.helper.event(10,'100','90')
        for n,o in enumerate((self.a,self.b),1):o.update(signature=base58(bytes([n])*64),block_time=100)
        self.block={'blockhash':self.mint,'previousBlockhash':self.mint,'parentSlot':9,'blockTime':100,'signatures':[self.a['signature'],self.b['signature']]}
    def collect(self,block=None,capture=digest,max_blocks=2):
        def rpc(method,params):
            self.assertEqual(method,'getBlock');self.assertEqual(params[1]['commitment'],'finalized');self.assertEqual(params[1]['transactionDetails'],'signatures')
            return self.block if block is None else block
        return collect_ordering(self.mint,[self.a,self.b],rpc,capture=capture,max_blocks=max_blocks)
    def test_ordered_block_enables_continuity_not_completeness(self):
        from desk.continuity import reconcile_history
        proof=self.collect();self.assertTrue(proof['verified'])
        r=reconcile_history(self.mint,[self.b,self.a],ordering=proof)
        self.assertTrue(r['passed']);self.assertFalse(r['transfer_history_complete'])
    def test_unpersisted_proof_not_used(self):
        self.assertFalse(self.collect(capture=None)['verified'])
    def test_wrong_timestamp_rejected(self):
        self.block['blockTime']=101;self.assertFalse(self.collect()['verified'])
    def test_missing_signature_rejected(self):
        self.block['signatures'].pop();self.assertFalse(self.collect()['verified'])
    def test_duplicate_signature_rejected(self):
        self.block['signatures'].append(self.a['signature']);self.assertFalse(self.collect()['verified'])
    def test_budget_exhaustion_is_explicit(self):
        r=self.collect(max_blocks=0);self.assertIn('BLOCK_ORDERING_BUDGET_EXHAUSTED',r['reasons']);self.assertEqual(r['proofs'],[])
    def test_block_order_is_not_inferred_from_balances(self):
        from desk.continuity import reconcile_history
        self.block['signatures'].reverse()
        self.assertFalse(reconcile_history(self.mint,[self.a,self.b],ordering=self.collect())['passed'])
    def test_provider_failure_stays_unknown(self):
        def rpc(*args):raise ValueError('unavailable')
        self.assertFalse(collect_ordering(self.mint,[self.a,self.b],rpc,capture=digest)['verified'])
    def test_public_finalized_block_fixture(self):
        import json
        from pathlib import Path
        p=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-block-order.json').read_text())
        r=block_order(p['capture']['slot'],p['capture']['result'],p['observations'])
        self.assertTrue(r['verified']);self.assertGreater(len(r['positions']),1)
