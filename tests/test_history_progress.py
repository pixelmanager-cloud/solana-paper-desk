import tempfile,unittest
from pathlib import Path
from desk.evidence import EvidenceStore
from desk.history import collect_history
from desk.history_progress import HistoryProgress
from desk.security import base58
from desk.model import digest
MINT=base58(bytes([7])*32)
class HistoryProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'e.sqlite';self.store=EvidenceStore(self.path);self.progress=HistoryProgress(self.store)
        self.progress.budget('scan',digest({'scan':1}),2)
    def seed(self):
        _,coverage=collect_history(MINT,10,20,lambda *a:{'data':[],'paginationToken':'next'},max_pages=1,capture=self.store.save,token_accounts='none')
        return self.progress.seed('scan',coverage)
    def test_restart_uses_saved_cursor_and_never_refetches_first_page(self):
        key=self.seed();calls=[]
        def rpc(method,params):calls.append(params);return {'data':[]}
        result=HistoryProgress(EvidenceStore(self.path)).advance(key,rpc)
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][1]['paginationToken'],'next')
        self.assertEqual(result['status'],'DONE');self.assertEqual(result['requests_used'],3)
        self.assertEqual(self.progress.advance(key,lambda *a:self.fail('Already complete'))['requests_used'],3)
    def test_failure_consumes_budget_and_keeps_checkpoint(self):
        key=self.seed();before=self.progress.snapshot(key)['coverage']
        def fail(*args):raise OSError('sensitive provider error')
        result=self.progress.advance(key,fail)
        self.assertEqual(result['coverage'],before);self.assertEqual(result['requests_used'],3)
        self.assertEqual(result['status'],'RETRYABLE_ERROR');self.assertNotIn('sensitive',str(result))
        self.assertEqual(self.progress.advance(key,lambda *a:{'data':[]})['requests_used'],4)
    def test_strict_failure_needs_a_transient_original(self):
        # T22H/T22J: the paper cycle's strict path accepts a failure as RETRYABLE_ERROR only with a transient original.
        from desk.paper_read_sources import PaperReadError
        key=self.seed();before=self.progress.snapshot(key)['coverage']
        with self.assertRaises(OSError):self.progress.advance(key,lambda *a:(_ for _ in ()).throw(OSError('sensitive')),strict=True)
        original=self.store.save({'kind':'paper_read_attempt_v1','scan_id':'s','failure_code':'TRANSPORT_ERROR','requests_used':1})
        def fail(*args):raise PaperReadError('TRANSPORT_ERROR',original)
        result=self.progress.advance(key,fail,strict=True)
        self.assertEqual(result['coverage'],before);self.assertEqual(result['requests_used'],4)
        self.assertEqual((result['status'],result['failure_evidence']),('RETRYABLE_ERROR',original));self.assertNotIn('sensitive',str(result))
    def test_budget_is_shared_across_accounts_and_survives_reopen(self):
        self.progress.budget('small',digest({'scan':2}),17)
        a=self.progress.create('small',MINT,10,20)
        b=self.progress.create('small',base58(bytes([8])*32),10,20)
        self.progress.advance(a,lambda *a:{'data':[]})
        result=HistoryProgress(EvidenceStore(self.path)).advance(b,lambda *a:self.fail('Over budget'))
        self.assertEqual(result['blocked'],'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED')
    def test_budget_identity_cannot_be_reset_or_rebound(self):
        key=self.seed();self.progress.advance(key,lambda *a:{'data':[]})
        self.progress.budget('scan',digest({'scan':1}),0)
        self.assertEqual(self.progress.snapshot(key)['requests_used'],3)
        with self.assertRaises(ValueError):self.progress.budget('scan',digest({'scan':99}),0)
    def test_corrupt_cached_coverage_does_not_spend_or_call(self):
        key=self.seed()
        with self.store.connect() as c:c.execute("UPDATE ownership_history SET coverage='{}' WHERE id=?",(key,))
        with self.assertRaises(ValueError):self.progress.advance(key,lambda *a:self.fail('Corrupt input'))
        self.assertEqual(self.progress.snapshot(key)['requests_used'],2)
    def test_cursor_cycle_is_retained_as_gap(self):
        key=self.seed();result=self.progress.advance(key,lambda *a:{'data':[],'paginationToken':'next'})
        self.assertIn('HISTORY_CURSOR_CYCLE',result['coverage']['reasons'])
        self.assertFalse(result['coverage']['query_coverage_verified'])
    def test_concurrent_worker_does_not_reserve_request(self):
        import fcntl
        key=self.seed()
        with open(str(self.path)+'.ownership.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=self.progress.advance(key,lambda *a:self.fail('Concurrent call'))
            self.assertTrue(result['busy']);self.assertEqual(result['requests_used'],2)
    def test_database_backup_preserves_cursor_and_spent_budget(self):
        import sqlite3
        key=self.seed();restored=Path(self.tmp.name)/'restored.sqlite'
        with self.store.connect() as source,sqlite3.connect(restored) as target:source.backup(target)
        progress=HistoryProgress(EvidenceStore(restored));calls=[]
        def rpc(method,params):calls.append(params);return {'data':[]}
        result=progress.advance(key,rpc)
        self.assertEqual(result['requests_used'],3);self.assertEqual(calls[0][1]['paginationToken'],'next')
        self.assertEqual(self.progress.snapshot(key)['requests_used'],2)
