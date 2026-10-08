"""Synthetic bank and request fixtures; no network or provider credentials."""
import copy,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.ownership_worker import advance
from desk.model import digest
from tests import test_ownership_snapshot as snapshot_fixtures
from tests import test_ownership_worker as worker_fixtures

class BankCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=EvidenceStore(Path(self.tmp.name)/'bank.sqlite');self.progress=HistoryProgress(self.store)
        f=snapshot_fixtures.OwnershipSnapshotTests();f.setUp();self.f=f
        self.progress.budget('scan','a'*64,7)
    def capture(self,rpc):return self.progress.capture_bank('scan',self.f.mint,[self.f.key],rpc)
    def used(self):
        with self.store.connect() as c:return c.execute('SELECT used FROM ownership_budgets').fetchone()[0]
    def test_bank_saved_before_clock_and_restart_keeps_cutoff(self):
        def rpc(method,params):
            self.assertEqual(self.used(),8);return self.f.snapshot['result']
        self.capture(rpc);bank=self.progress.bank('scan');self.assertIsNone(bank['block_time_hash'])
        restarted=HistoryProgress(EvidenceStore(self.store.path))
        calls=[]
        def clock(method,params):calls.append((method,params));self.assertEqual(self.used(),9);return 100
        restarted.capture_bank('scan',self.f.mint,[self.f.key],clock)
        self.assertEqual(calls,[('getBlockTime',[20])]);self.assertEqual(restarted.bank('scan')['snapshot_hash'],bank['snapshot_hash'])
        self.capture(lambda *a:self.fail('Completed bank fetched again'))
    def test_interrupted_clock_is_charged_and_bank_retained(self):
        self.capture(lambda *a:self.f.snapshot['result']);bank=self.progress.bank('scan')
        def crash(*a):raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.capture(crash)
        self.assertEqual(self.used(),9);self.assertEqual(self.progress.bank('scan'),bank)
        self.capture(lambda method,params:100);self.assertEqual(self.used(),10)
    def test_incomplete_batch_is_not_persisted(self):
        result=copy.deepcopy(self.f.snapshot['result']);result['value'].pop()
        self.assertEqual(self.capture(lambda *a:result)['blocked'],'PROVIDER_RETRY_REQUIRED')
        self.assertIsNone(self.progress.bank('scan'));self.assertEqual(self.used(),8)
    def test_changed_frontier_cannot_rebind_bank(self):
        self.capture(lambda *a:self.f.snapshot['result'])
        with self.assertRaises(ValueError):self.progress.capture_bank('scan',self.f.mint,[],lambda *a:self.fail('Changed frontier'))
        self.assertEqual(self.used(),8)
    def test_captured_matching_and_mismatching_banks_reach_replay_validator(self):
        from desk.ownership_snapshot import replay_snapshot
        self.capture(lambda *a:self.f.snapshot['result']);self.capture(lambda *a:100)
        bank=self.progress.bank('scan')
        # Isolate the replay adapter with a known synthetic ending fixture;
        # collection, persisted bank/clock and reconciliation are unmocked.
        with patch('desk.replay_history.reconstruct_launch_history',return_value=self.f.history):
            result=replay_snapshot({'mint':self.f.mint},self.store,bank['snapshot_hash'],bank['block_time_hash'])
            self.assertTrue(result['reconciled']);self.assertFalse(result['eligible_for_trading'])
            self.f.history['account_continuity']['end_states'][0]['amount_raw']='99'
            result=replay_snapshot({'mint':self.f.mint},self.store,bank['snapshot_hash'],bank['block_time_hash'])
            self.assertFalse(result['reconciled'])
            self.assertIn('HISTORY_SNAPSHOT_BALANCE_OR_CONTROL_MISMATCH',result['reasons'])

    def test_atomic_bank_head_failure_keeps_budget_but_no_orphan(self):
        self.progress.bank('scan')
        with self.store.connect() as c:
            c.execute("CREATE TRIGGER reject_bank BEFORE INSERT ON ownership_banks BEGIN SELECT RAISE(ABORT,'fixture crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.capture(lambda *a:self.f.snapshot['result'])
        self.assertIsNone(self.progress.bank('scan'));self.assertEqual(self.used(),8)
        with self.assertRaises(ValueError):self.store.load(digest(self.f.snapshot))
    def test_storage_limit_does_not_publish_a_cutoff(self):
        self.store.max_bytes=0
        self.assertEqual(self.capture(lambda *a:self.f.snapshot['result'])['blocked'],'PROVIDER_RETRY_REQUIRED')
        self.assertIsNone(self.progress.bank('scan'));self.assertEqual(self.used(),8)
    def test_concurrent_bank_collection_spends_nothing(self):
        import fcntl
        with open(str(self.store.path)+'.ownership.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(self.capture(lambda *a:self.fail('Concurrent I/O'))['blocked'],'BUSY')
        self.assertEqual(self.used(),7)

    def test_symlink_alias_uses_real_request_lock_for_bank_and_history(self):
        import fcntl
        alias=self.store.path.parent/'alias.sqlite';alias.symlink_to(self.store.path)
        aliased=HistoryProgress(EvidenceStore(alias))
        key=aliased.create('scan',self.f.mint,90,110)
        with open(str(self.store.path)+'.ownership.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=aliased.capture_bank('scan',self.f.mint,[self.f.key],lambda *a:self.fail('Alias bank I/O'))
            self.assertEqual(result['blocked'],'BUSY');self.assertFalse(result['attempted'])
            result=aliased.advance(key,lambda *a:self.fail('Alias history I/O'))
            self.assertTrue(result['busy']);self.assertEqual(result['requests_used'],7)
        self.assertFalse(Path(str(alias)+'.ownership.lock').exists())

    def test_hardlink_created_after_progress_init_is_rejected_by_both_locks(self):
        import os
        alias=self.store.path.parent/'hardlink.sqlite'
        key=self.progress.create('scan',self.f.mint,90,110)
        os.link(self.store.path,alias)
        with self.assertRaisesRegex(ValueError,'one hard link'):
            self.capture(lambda *a:self.fail('Hardlink bank I/O'))
        with self.assertRaisesRegex(ValueError,'one hard link'):
            self.progress.advance(key,lambda *a:self.fail('Hardlink history I/O'))
        self.assertEqual(self.used(),7)

    def test_exhausted_budget_prevents_snapshot_io(self):
        for _ in range(11):self.progress.reserve('scan')
        self.assertEqual(self.capture(lambda *a:self.fail('Over budget'))['blocked'],'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED')

class WorkerSnapshotIntegrationTests(unittest.TestCase):
    setUp=worker_fixtures.OwnershipWorkerTests.setUp
    def test_collection_then_slot_requests_resume_without_new_bank(self):
        # The inventory adapter is mocked to isolate orchestration; all page and
        # snapshot persistence, request replay and budget accounting remain real.
        fixture=snapshot_fixtures.OwnershipSnapshotTests();fixture.setUp()
        snapshot=copy.deepcopy(fixture.snapshot);snapshot['params'][0][0]=self.mint
        snapshot['result']['value'][0]=self.store.load(self.mintkey)['result']['value']
        calls=[]
        def rpc(method,params):
            calls.append((method,params))
            if method=='getMultipleAccounts':return snapshot['result']
            if method=='getBlockTime':return 100
            return {'data':[]}
        inventory={'initialization_inventory_verified':True,'accounts':[{'address':fixture.key}]}
        with patch('desk.account_history.account_inventory',return_value=inventory):
            first=advance(self.db,self.evidence,'scan',rpc,max_calls=2)
            bank=first['snapshot_evidence']['snapshot_hash']
            second=advance(self.db,self.evidence,'scan',rpc,max_calls=4)
            third=advance(self.db,self.evidence,'scan',rpc,max_calls=4)
        self.assertEqual(sum(m=='getMultipleAccounts' for m,p in calls),1)
        self.assertEqual(sum(m=='getBlockTime' for m,p in calls),1)
        self.assertEqual(third['snapshot_evidence']['snapshot_hash'],bank)
        bounded=[p for m,p in calls if m=='getTransactionsForAddress' and 'slot' in p[1]['filters']]
        self.assertEqual({p[0] for p in bounded},{self.mint,fixture.key})
        for p in bounded:self.assertEqual(p[1]['filters']['slot'],{'gte':0,'lt':21});self.assertNotIn('blockTime',p[1]['filters'])
        self.assertFalse(second['snapshot']['reconciled']);self.assertFalse(third['eligible_for_trading'])
        self.assertEqual(third['requests_used'],7+len(calls))
