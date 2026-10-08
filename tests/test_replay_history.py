import json,tempfile,unittest
from pathlib import Path
from desk.evidence import EvidenceStore
from desk.history import collect_history
from desk.replay_history import replay_history,reconstruct_launch_history
from desk.model import digest
from desk.decode import decode

class HistoryReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=EvidenceStore(Path(self.tmp.name)/'e.sqlite')
        payload=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-launch.json').read_text())['payload']
        obs=decode(payload);event=next(x for x in obs['program_observations'] if x.get('name')=='CreateEvent')
        self.mint=event['fields']['mint'];self.at=event['fields']['timestamp']
        f=payload['params']['result'];self.raw={**f['transaction'],'slot':f['slot'],'blockTime':self.at,'signature':f['signature']}
    def capture(self,pages):
        i=iter(pages)
        return collect_history(self.mint,self.at-1,self.at+1,lambda *a:next(i),max_pages=len(pages),capture=self.store.save,token_accounts='none')
    def rehash(self,c):
        c.pop('evidence_hash',None);c['evidence_hash']=digest(c)
    def test_mainnet_launch_redecoded_without_network(self):
        observations,c=self.capture([{'data':[self.raw]}])
        self.assertEqual(replay_history(c,self.store),(observations,c))
        result=reconstruct_launch_history({'mint':self.mint,'history_queries':[c]},self.store)
        self.assertTrue(result['launch_verified']);self.assertTrue(result['query_coverage_verified'])
        self.assertFalse(result['transfer_history_complete']);self.assertFalse(result['eligible_for_trading'])
        self.assertGreater(result['account_queries']['required'],0)
        self.assertIn('ACCOUNT_HISTORY_QUERY_MISSING_OR_AMBIGUOUS',result['account_queries']['reasons'])
    def test_partial_history_can_verify_anchor_but_not_coverage(self):
        _,c=self.capture([{'data':[self.raw],'paginationToken':'next'}])
        result=reconstruct_launch_history({'mint':self.mint,'history_queries':[c]},self.store)
        self.assertTrue(result['launch_verified']);self.assertFalse(result['query_coverage_verified'])
        self.assertIn('HISTORY_RANGE_NOT_EXHAUSTED',result['query_reasons'])
    def test_asserted_complete_flags_cannot_override_raw_page(self):
        _,c=self.capture([{'data':[self.raw],'paginationToken':'next'}]);c['query_coverage_verified']=True;self.rehash(c)
        with self.assertRaises(ValueError):replay_history(c,self.store)
    def test_rebound_range_is_rejected_even_with_new_checksum(self):
        _,c=self.capture([{'data':[self.raw]}]);c['start']-=1;self.rehash(c)
        with self.assertRaisesRegex(ValueError,'binding'):replay_history(c,self.store)
    def test_legacy_unbound_pages_cannot_be_promoted(self):
        _,c=self.capture([{'data':[self.raw]}]);del c['pages'][0]['request_evidence_hash'];self.rehash(c)
        with self.assertRaises(KeyError):replay_history(c,self.store)
    def test_page_cursor_chain_is_checked(self):
        _,c=self.capture([{'data':[self.raw],'paginationToken':'next'},{'data':[]}])
        self.assertEqual(replay_history(c,self.store)[1],c)
        c['pages'].reverse();self.rehash(c)
        with self.assertRaises(ValueError):replay_history(c,self.store)
    def test_missing_page_blocks_replay(self):
        _,c=self.capture([{'data':[self.raw]}])
        with self.store.connect() as db:db.execute('DELETE FROM pages WHERE hash=?',(c['pages'][0]['payload_hash'],))
        with self.assertRaises(ValueError):replay_history(c,self.store)
    def test_duplicate_raw_records_remain_a_coverage_gap(self):
        _,c=self.capture([{'data':[self.raw,self.raw]}]);_,rebuilt=replay_history(c,self.store)
        self.assertIn('HISTORY_DUPLICATE_RECORD',rebuilt['reasons']);self.assertFalse(rebuilt['query_coverage_verified'])
    def test_account_queries_replay_and_deduplicate_shared_transactions(self):
        _,c=self.capture([{'data':[self.raw]}]);report={'mint':self.mint,'history_queries':[c]}
        initial=reconstruct_launch_history(report,self.store)
        for account in initial['inventory']['accounts']:
            _,q=collect_history(account['address'],self.at-1,self.at+1,lambda *a:{'data':[self.raw]},
                                max_pages=1,capture=self.store.save,token_accounts='none')
            report['history_queries'].append(q)
        result=reconstruct_launch_history(report,self.store)
        self.assertEqual(result['account_queries']['verified'],result['account_queries']['required'])
        self.assertEqual(result['observed_transaction_count'],1)
        self.assertFalse(result['transfer_history_complete'])
    def test_entry_gates_use_replayed_launch_not_report_assertion(self):
        from desk.entry_evidence import evaluate
        _,c=self.capture([{'data':[self.raw]}])
        report={'mint':self.mint,'history_queries':[c],'launch_anchors':[{'verified':False}]}
        report['report_hash']=digest(report)
        result=evaluate(report,self.store)
        self.assertEqual(result['gates']['launch_anchor']['status'],'VERIFIED_COMPONENT')
        self.assertEqual(result['gates']['transfer_history']['status'],'BLOCKED')
        self.assertFalse(result['eligible_for_trading'])
    def test_total_replay_page_budget_is_bounded(self):
        _,c=self.capture([{'data':[self.raw]}])
        with self.assertRaisesRegex(ValueError,'budget'):
            reconstruct_launch_history({'mint':self.mint,'history_queries':[c]*19},self.store)
    def test_exact_slot_query_replays_without_timestamp_filter(self):
        slot=self.raw['slot'];calls=[]
        def rpc(method,params):calls.append(params);return {'data':[self.raw]}
        _,c=collect_history(self.mint,1,2,rpc,max_pages=1,capture=self.store.save,token_accounts='none',slot_range={'gte':0,'lt':slot+1})
        self.assertNotIn('blockTime',calls[0][1]['filters'])
        self.assertEqual(c['slot_range'],{'gte':0,'lt':slot+1})
        self.assertEqual(replay_history(c,self.store)[1],c)
        self.assertTrue(reconstruct_launch_history({'mint':self.mint,'history_queries':[c]},self.store)['launch_verified'])
    def test_provider_record_beyond_slot_cutoff_blocks_coverage(self):
        slot=self.raw['slot']
        _,c=collect_history(self.mint,1,2,lambda *a:{'data':[self.raw]},max_pages=1,capture=self.store.save,token_accounts='none',slot_range={'gte':0,'lt':slot})
        self.assertFalse(c['query_coverage_verified']);self.assertIn('HISTORY_SLOT_OUTSIDE_QUERY',c['reasons'])
