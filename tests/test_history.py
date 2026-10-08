import json,copy,unittest
from pathlib import Path
from desk.history import collect_history
from desk.security import base58
MINT=base58(bytes([7])*32)
class HistoryTests(unittest.TestCase):
    def setUp(self):
        f=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-launch.json').read_text())['payload']['params']['result']
        self.raw={**f['transaction'],'slot':f['slot'],'blockTime':100,'signature':f['signature']}
    def collect(self,pages,n=3):
        i=iter(pages)
        return collect_history(MINT,90,110,lambda *x:next(i),max_pages=n)
    def test_saved_page_can_be_replayed_by_report_hash(self):
        import tempfile
        from desk.evidence import EvidenceStore
        with tempfile.TemporaryDirectory() as d:
            store=EvidenceStore(Path(d)/'evidence.sqlite');page={'data':[self.raw]}
            _,coverage=collect_history(MINT,90,110,lambda *x:page,capture=store.save)
            self.assertTrue(coverage['raw_pages_persisted'])
            self.assertEqual(store.load(coverage['pages'][0]['payload_hash']),page)

    def test_verified_address_query_never_whole_token(self):
        obs,c=self.collect([{'data':[self.raw]}]);self.assertEqual(len(obs),1)
        self.assertTrue(c['query_coverage_verified']);self.assertFalse(c['launch_history_complete'])
    def test_decode_gap_not_coverage(self):
        _,c=self.collect([{'data':[{}]}]);self.assertTrue(c['query_range_exhausted']);self.assertFalse(c['query_coverage_verified'])
    def test_duplicate_page_is_not_clean(self):
        _,c=self.collect([{'data':[self.raw],'paginationToken':'a'},{'data':[self.raw]}])
        self.assertIn('HISTORY_DUPLICATE_RECORD',c['reasons'])
    def test_cursor_cycle(self):
        _,c=self.collect([{'data':[],'paginationToken':'a'},{'data':[],'paginationToken':'b'},{'data':[],'paginationToken':'a'}])
        self.assertIn('HISTORY_CURSOR_CYCLE',c['reasons'])
    def test_wrong_time(self):
        self.raw['blockTime']=None
        obs,c=self.collect([{'data':[self.raw]}]);self.assertEqual(obs,[]);self.assertFalse(c['query_coverage_verified'])
    def test_budget_limit_not_complete(self):
        _,c=self.collect([{'data':[self.raw],'paginationToken':'a'}],1)
        self.assertFalse(c['query_coverage_verified']);self.assertEqual(c['next_cursor'],'a')
    def test_conflicting_signature(self):
        changed=copy.deepcopy(self.raw);changed['slot']+=1
        _,c=self.collect([{'data':[self.raw,changed]}]);self.assertIn('HISTORY_CONFLICTING_RECORD',c['reasons'])
    def test_invalid_cursor(self):
        _,c=self.collect([{'data':[],'paginationToken':123}]);self.assertIn('HISTORY_INVALID_CURSOR',c['reasons'])

class DurableHistoryTests(unittest.TestCase):
    def test_partial_page_failure_rolls_back_records_and_cursor(self):
        import tempfile
        from desk.ledger import Ledger
        from desk.providers import backfill
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'history.sqlite')
            try:
                ledger.record_raw('history:s1',0,1,{'signature':'s1','slot':1})
                page={'data':[{'signature':'s2','slot':2},{'signature':'s1','slot':9}],'paginationToken':'next'}
                with self.assertRaises(ValueError):backfill(ledger,MINT,1,10,1,lambda *x:page)
                self.assertEqual(ledger.db.execute('select count(*) from raw_events').fetchone()[0],1)
                self.assertEqual(ledger.db.execute('select count(*) from metadata').fetchone()[0],0)
            finally:ledger.close()
    def test_nonadjacent_cursor_cycle_across_restart(self):
        import tempfile
        from desk.ledger import Ledger
        from desk.providers import backfill
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'history.sqlite';ledger=Ledger(path)
            backfill(ledger,MINT,1,10,1,lambda *x:{'data':[],'paginationToken':'a'});ledger.close()
            ledger=Ledger(path)
            try:
                backfill(ledger,MINT,1,10,1,lambda *x:{'data':[],'paginationToken':'b'})
                with self.assertRaises(ValueError):backfill(ledger,MINT,1,10,1,lambda *x:{'data':[],'paginationToken':'a'})
                checkpoint=json.loads(ledger.db.execute('select value from metadata').fetchone()[0])
                self.assertEqual(checkpoint['cursor'],'b');self.assertEqual(len(checkpoint['pages']),2)
            finally:ledger.close()
