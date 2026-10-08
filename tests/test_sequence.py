import base64,copy,unittest
from unittest.mock import patch
from solders.hash import Hash
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
from solders.system_program import transfer,TransferParams
from desk.sequence import simulate_sequence
class SequenceTests(unittest.TestCase):
    def setUp(self):
        self.wallet=Pubkey.from_string('FzULv8pR9Rd7cyVKjVkzmJ1eqEmgwDnzjYyNUcEJtoG9');self.other=Pubkey.from_bytes(bytes([9])*32)
        msg=MessageV0.try_compile(self.wallet,[transfer(TransferParams(from_pubkey=self.wallet,to_pubkey=self.other,lamports=1))],[],Hash.default())
        self.transactions=[bytes(VersionedTransaction.populate(msg,[Signature.default()]))]*2
        def a(amount):return {'owner':'11111111111111111111111111111111','executable':False,'lamports':amount,'data':['','base64']}
        self.result={'context':{'slot':123},'value':{'summary':'succeeded','transactionResults':[
            {'err':None,'preExecutionAccounts':[a(10)],'postExecutionAccounts':[a(9)]},
            {'err':None,'preExecutionAccounts':[a(9)],'postExecutionAccounts':[a(8)]}]}}
        self.calls=[]
    def rpc(self,method,params):
        self.calls.append(method);self.assertEqual(method,'simulateBundle');self.assertTrue(params[1]['skipSigVerify']);self.assertEqual(params[1]['simulationBank'],{'commitment':{'commitment':'confirmed'}});self.assertEqual(len(params[1]['preExecutionAccountsConfigs']),2)
        return self.result
    def run_sequence(self):return simulate_sequence(self.transactions,[str(self.wallet)],self.rpc)
    def test_sequential_states_do_not_grant_entry(self):
        r=self.run_sequence();self.assertTrue(r['passed']);self.assertFalse(r['eligible_for_trading']);self.assertFalse(r['submitted'])
    def test_independent_simulation_states_rejected(self):
        self.result['value']['transactionResults'][1]['preExecutionAccounts'][0]['lamports']=10
        self.assertIn('SEQUENCE_STATE_CONTINUITY_UNVERIFIED',self.run_sequence()['reasons'])
    def test_failed_second_leg_rejected(self):
        self.result['value']['transactionResults'][1]['err']='InsufficientFunds';self.assertFalse(self.run_sequence()['passed'])
    def test_missing_account_state_rejected(self):
        self.result['value']['transactionResults'][0].pop('postExecutionAccounts');self.assertFalse(self.run_sequence()['passed'])
    def test_missing_result_rejected(self):
        self.result['value']['transactionResults'].pop();self.assertFalse(self.run_sequence()['passed'])
    def test_nonzero_signature_rejected_before_rpc(self):
        tx=VersionedTransaction.from_bytes(self.transactions[0]);self.transactions[0]=bytes(VersionedTransaction.populate(tx.message,[Signature.from_bytes(bytes([1])*64)]))
        with self.assertRaises(ValueError):self.run_sequence()
        self.assertFalse(self.calls)
    def test_stale_sequence_rejected(self):
        with patch('desk.sequence.time.monotonic',side_effect=[0,11]):self.assertIn('SEQUENCE_RESPONSE_STALE',self.run_sequence()['reasons'])
    def test_oversized_or_single_sequence_rejected(self):
        self.transactions=self.transactions[:1]
        with self.assertRaises(ValueError):self.run_sequence()
    def test_mainnet_sequence_state_witness_replay(self):
        import json
        from pathlib import Path
        from desk.sequence import account_identity
        p=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-sequence-simulation.json').read_text())
        sequence=p['sequence'];rows=sequence['result']['value']['transactionResults']
        self.assertTrue(p['second_leg_alone_failed']);self.assertTrue(p['target_absent_before']);self.assertTrue(p['target_absent_after'])
        self.assertEqual([account_identity(a) for a in rows[0]['postExecutionAccounts']],
                         [account_identity(a) for a in rows[1]['preExecutionAccounts']])
        self.assertEqual(rows[0]['postExecutionAccounts'][1]['lamports'],1000000)
        self.assertFalse(sequence['signed']);self.assertFalse(sequence['submitted']);self.assertFalse(sequence['eligible_for_trading'])
