import base64
import importlib.util
import unittest
import time
from desk.security import TOKEN_PROGRAM,base58

@unittest.skipUnless(importlib.util.find_spec('solders'),'optional solders dependency')
class SimulationTests(unittest.TestCase):
    def setUp(self):
        from solders.keypair import Keypair
        self.wallet=str(Keypair.from_seed(bytes(32)).pubkey())
        self.mint=base58(bytes([7])*32);self.holding=base58(bytes([8])*32)
        self.calls=[];self.err=None;self.extra=False
    def account(self,raw):return {'owner':TOKEN_PROGRAM,'executable':False,'data':[base64.b64encode(raw).decode(),'base64']}
    def token(self):
        from desk.programs import unbase58
        d=bytearray(165);d[:32]=bytes([7])*32;d[32:64]=unbase58(self.wallet);d[64:72]=(1000).to_bytes(8,'little');d[108]=1
        return self.account(d)
    def rpc(self,method,params):
        self.calls.append(method)
        if method=='getMultipleAccounts':
            d=bytearray(82);d[36:44]=(10000).to_bytes(8,'little');d[45]=1
            return {'context':{'slot':123},'value':[{'owner':'11111111111111111111111111111111','executable':False,'lamports':100000},self.token(),self.account(d)]}
        if method=='getLatestBlockhash':return {'value':{'blockhash':base58(bytes([9])*32)}}
        if method=='simulateTransaction':
            raw=base64.b64decode(params[0]);self.assertEqual(raw[1:65],bytes(64));self.assertFalse(params[1]['sigVerify'])
            return {'context':{'slot':124},'value':{'err':self.err,'accounts':[None,self.token()]}}
        self.fail('Unexpected RPC method: '+method)
    def quote(self,*args):
        return {'observed_at':int(time.time()),'response':{'inputMint':self.mint,'outputMint':'So11111111111111111111111111111111111111112','inAmount':'10','otherAmountThreshold':'1',
          'swapInstruction':{'programId':TOKEN_PROGRAM,'data':'','accounts':[{'pubkey':self.holding if self.extra else self.wallet,'isSigner':True,'isWritable':True}]}}}
    def test_no_signing_and_no_approval_from_success(self):
        from desk.simulate import simulate_sell
        r=simulate_sell(self.mint,self.wallet,self.holding,10,self.rpc,self.quote)
        self.assertTrue(r['simulation_ok']);self.assertFalse(r['signed']);self.assertFalse(r['submitted']);self.assertFalse(r['eligible_for_trading'])
    def test_stale_quote_never_fresh(self):
        from desk.simulate import simulate_sell
        def old_quote(*args):
            r=self.quote(*args);r['observed_at']=1;return r
        self.assertFalse(simulate_sell(self.mint,self.wallet,self.holding,10,self.rpc,old_quote)['fresh'])
    def test_unexpected_signer_rejected(self):
        from desk.simulate import simulate_sell
        self.extra=True
        with self.assertRaises(ValueError):simulate_sell(self.mint,self.wallet,self.holding,10,self.rpc,self.quote)
        self.assertNotIn('simulateTransaction',self.calls)
    def test_insufficient_tokens_rejected(self):
        from desk.simulate import simulate_sell
        with self.assertRaises(ValueError):simulate_sell(self.mint,self.wallet,self.holding,1001,self.rpc,self.quote)
    def test_failed_simulation_remains_failed(self):
        from desk.simulate import simulate_sell
        self.err={'InstructionError':[0,'InvalidAccountData']}
        r=simulate_sell(self.mint,self.wallet,self.holding,10,self.rpc,self.quote)
        self.assertFalse(r['simulation_ok'])
    def test_captured_unsigned_simulation_has_replayable_hash(self):
        import tempfile
        from pathlib import Path
        from desk.simulate import simulate_sell
        from desk.evidence import EvidenceStore
        with tempfile.TemporaryDirectory() as d:
            store=EvidenceStore(Path(d)/'evidence.sqlite')
            r=simulate_sell(self.mint,self.wallet,self.holding,10,self.rpc,self.quote,capture=store.save)
            self.assertTrue(r['raw_evidence_persisted']);p=store.load(r['evidence_hash'])
            self.assertEqual(base64.b64decode(p['unsigned_transaction'])[1:65],bytes(64))
            self.assertFalse(r['eligible_for_trading']);self.assertFalse(r['amm_bindings']['passed'])
    def test_wrong_capture_hash_is_rejected(self):
        from desk.simulate import simulate_sell
        with self.assertRaisesRegex(ValueError,'evidence not persisted'):
            simulate_sell(self.mint,self.wallet,self.holding,10,self.rpc,self.quote,capture=lambda e:'wrong')
    def test_offline_replay_recomputes_identical_checks_without_rpc(self):
        import tempfile
        from pathlib import Path
        from desk.simulate import simulate_sell
        from desk.evidence import EvidenceStore
        from desk.replay_sell import replay_sell
        with tempfile.TemporaryDirectory() as d:
            store=EvidenceStore(Path(d)/'evidence.sqlite');live=simulate_sell(self.mint,self.wallet,self.holding,10,self.rpc,self.quote,capture=store.save)
            original=store.load(live['evidence_hash']);calls=len(self.calls);replay=replay_sell(store,live['evidence_hash']);self.assertEqual(len(self.calls),calls)
            for name in ('balance_effects','account_controls','instruction_inventory','wallet_debit_checks','router_checks','envelope_checks','amm_bindings','recipient_checks','fee_checks','fee_query','sell_event_checks','fee_split_checks','setup_checks','route_coverage'):
                self.assertEqual(live[name],replay[name])
            self.assertFalse(replay['fresh']);self.assertFalse(replay['eligible_for_trading'])
            self.assertTrue(replay['instruction_inventory']['outer_message_privileges']['declarations_consistent'])
            self.assertFalse(replay['instruction_inventory']['runtime_cpi_privileges_authenticated'])
            self.assertEqual(store.load(live['evidence_hash']),original)
    def test_replay_rejects_altered_route_keys_and_missing_pool_evidence(self):
        import tempfile,copy
        from pathlib import Path
        from desk.simulate import simulate_sell
        from desk.evidence import EvidenceStore
        from desk.replay_sell import replay_sell
        with tempfile.TemporaryDirectory() as d:
            store=EvidenceStore(Path(d)/'evidence.sqlite');live=simulate_sell(self.mint,self.wallet,self.holding,10,self.rpc,self.quote,capture=store.save);original=store.load(live['evidence_hash'])
            for name,value in [('keys',[]),('pool_evidence_hash','a'*64),('schema_version',1),('amount_raw','11')]:
                record=copy.deepcopy(original);record[name]=value
                with self.assertRaises(ValueError):replay_sell(store,store.save(record))

    def test_replay_store_is_read_only_and_missing_store_not_created(self):
        import tempfile
        from pathlib import Path
        from desk.evidence import EvidenceStore
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'evidence.sqlite'
            with self.assertRaises(ValueError):EvidenceStore(path,read_only=True)
            self.assertFalse(path.exists())
            store=EvidenceStore(path);key=store.save({'test':'saved'});reader=EvidenceStore(path,read_only=True)
            self.assertEqual(reader.load(key),{'test':'saved'})
            with self.assertRaisesRegex(ValueError,'read-only'):reader.save({'test':'new'})
