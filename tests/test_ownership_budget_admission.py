"""Fixture-only lifecycle, legacy migration, crash and concurrency regressions."""
import copy,json,sqlite3,tempfile,threading,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.model import canonical,digest
from desk.security import base58
from desk.ownership_worker import advance
from tests import test_ownership_worker as worker_fixtures

MINT=base58(bytes([7])*32)

class AdmissionBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'evidence.sqlite';self.store=EvidenceStore(self.path)
        self.progress=HistoryProgress(self.store)
        self.descriptor={'kind':'ownership_admission_v1','scan_id':'scan','mint':MINT,'created':20}
        self.binding=self.progress.admit('scan',self.descriptor)['descriptor_hash']
    def source(self,calls=0):
        report={'mint':MINT,'calls':calls,'eligible_for_trading':False}
        report['report_hash']=digest(report)
        return {'id':'scan','mint':MINT,'created':20,'status':'COMPLETE','result':json.dumps(report)}
    def prepare(self,calls=0):return self.progress.prepare_source('scan',self.binding,self.source(calls))
    def seal(self,source):return self.progress.seal_source('scan',self.binding,digest(source))
    def test_reservation_is_durable_before_fixture_io_and_failure_cannot_refund(self):
        def provider():
            reopened=HistoryProgress(EvidenceStore(self.path))
            self.assertEqual(reopened.admission('scan')['requests_used'],1)
            raise OSError('fixture provider failure')
        self.assertTrue(self.progress.reserve('scan'))
        with self.assertRaises(OSError):provider()
        self.assertEqual(self.progress.admit('scan',self.descriptor)['requests_used'],1)
        self.assertTrue(self.progress.reserve('scan'))
        self.assertEqual(self.progress.admission('scan')['requests_used'],2)
    def test_reopen_prepare_seal_recovery_never_changes_descriptor_or_counter(self):
        self.progress.reserve('scan');source=self.source(1);prepared=self.prepare(1)
        self.assertEqual(prepared['state'],'PREPARED');self.assertFalse(self.progress.reserve('scan'))
        reopened=HistoryProgress(EvidenceStore(self.path))
        self.assertEqual(reopened.admission('scan')['prepared_source'],source)
        sealed=reopened.seal_source('scan',self.binding,digest(source))
        self.assertEqual(sealed['state'],'SEALED');self.assertTrue(reopened.reserve('scan'))
        self.assertEqual(reopened.prepare_source('scan',self.binding,source)['state'],'SEALED')
        self.assertEqual(reopened.seal_source('scan',self.binding,digest(source))['requests_used'],2)
        reopened.budget('scan',digest(source),1)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT source_hash,used,ceiling FROM ownership_budgets').fetchone(),(self.binding,2,18))
    def test_prepared_source_blocks_history_and_bank_before_io(self):
        job=self.progress.create('scan',MINT,0,21,{'gte':0,'lt':21});self.prepare()
        result=self.progress.advance(job,lambda *a:self.fail('Prepared history I/O'))
        self.assertEqual(result['blocked'],'INVESTIGATION_SOURCE_PREPARED')
        result=self.progress.capture_bank('scan',MINT,[base58(bytes([8])*32)],lambda *a:self.fail('Prepared bank I/O'))
        self.assertEqual(result['blocked'],'INVESTIGATION_SOURCE_PREPARED')
        self.assertEqual(self.progress.admission('scan')['requests_used'],0)
    def test_seal_requires_prepared_exact_hash(self):
        with self.assertRaises(ValueError):self.seal(self.source())
        self.prepare()
        for descriptor_hash,source_hash in [('b'*64,digest(self.source())),(self.binding,'c'*64)]:
            with self.assertRaises(ValueError):self.progress.seal_source('scan',descriptor_hash,source_hash)
        self.assertEqual(self.progress.admission('scan')['state'],'PREPARED')
    def test_descriptor_and_ceiling_cannot_rebind_on_retry(self):
        for field,value in [('mint',base58(bytes([8])*32)),('created',21),('scan_id','different'),('kind','asserted')]:
            changed={**self.descriptor,field:value}
            with self.assertRaises(ValueError):self.progress.admit('scan',changed)
        with self.assertRaises(ValueError):self.progress.admit('scan',self.descriptor,17)
        self.assertEqual(self.progress.admission('scan')['descriptor'],self.descriptor)
    def test_source_shape_identity_hash_eligibility_and_usage_rejected(self):
        self.progress.reserve('scan')
        for field,value in [('id','other'),('mint',base58(bytes([8])*32)),('created',21),('status','RUNNING'),('created',True)]:
            changed={**self.source(1),field:value}
            with self.assertRaises(ValueError):self.progress.prepare_source('scan',self.binding,changed)
        for calls in (0,2,True,19):
            with self.assertRaises(ValueError):self.progress.prepare_source('scan',self.binding,self.source(calls))
        changed=self.source(1);report=json.loads(changed['result']);report['calls']=0;changed['result']=json.dumps(report)
        with self.assertRaises(ValueError):self.progress.prepare_source('scan',self.binding,changed)
        changed=self.source(1);report=json.loads(changed['result']);report['eligible_for_trading']=True
        report.pop('report_hash');report['report_hash']=digest(report);changed['result']=json.dumps(report)
        with self.assertRaises(ValueError):self.progress.prepare_source('scan',self.binding,changed)
        self.assertEqual(self.progress.admission('scan')['state'],'ADMITTED')
    def test_prepared_source_cannot_change_even_only_result_whitespace(self):
        source=self.source();self.prepare();changed=copy.deepcopy(source)
        changed['result']=canonical(json.loads(changed['result']))
        self.assertNotEqual(source,changed)
        with self.assertRaises(ValueError):self.progress.prepare_source('scan',self.binding,changed)
        self.seal(source)
        with self.assertRaises(ValueError):self.progress.budget('scan',digest(changed),0)
    def test_counter_ceiling_shared_before_and_after_seal(self):
        for _ in range(17):self.assertTrue(self.progress.reserve('scan'))
        source=self.source(17);self.prepare(17);self.seal(source)
        job=self.progress.create('scan',MINT,0,21)
        seen=[]
        def provider(*a):seen.append(a);return {'data':[]}
        result=self.progress.advance(job,provider)
        self.assertEqual(result['requests_used'],18);self.assertEqual(len(seen),1)
        self.assertFalse(self.progress.reserve('scan'))
        self.progress.budget('scan',digest(source),17)
        reopened=HistoryProgress(EvidenceStore(self.path))
        other=reopened.create('scan',base58(bytes([8])*32),0,21)
        result=reopened.advance(other,lambda *a:self.fail('Ceiling bypass'))
        self.assertEqual(result['blocked'],'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED')
        self.assertEqual(reopened.admit('scan',self.descriptor)['requests_used'],18)
    def test_concurrent_reservations_spend_exactly_one_counter(self):
        barrier=threading.Barrier(24)
        def attempt():barrier.wait(timeout=10);return self.progress.reserve('scan')
        with ThreadPoolExecutor(max_workers=24) as pool:results=list(pool.map(lambda _:attempt(),range(24)))
        self.assertEqual(sum(results),18);self.assertEqual(self.progress.admission('scan')['requests_used'],18)
    def test_concurrent_distinct_prepare_only_one_source_wins(self):
        first=self.source();second=copy.deepcopy(first)
        report=json.loads(second['result']);report['notice']='alternate';report.pop('report_hash');report['report_hash']=digest(report)
        second['result']=json.dumps(report);barrier=threading.Barrier(2)
        def prepare(source):
            barrier.wait(timeout=10)
            try:self.progress.prepare_source('scan',self.binding,source);return True
            except ValueError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(prepare,[first,second]))
        self.assertEqual(sum(results),1)
        winner=self.progress.admission('scan')['prepared_source'];self.seal(winner)
        loser=second if winner==first else first
        with self.assertRaises(ValueError):self.seal(loser)
    def test_prepare_racing_reservation_cannot_omit_or_refund_attempt(self):
        barrier=threading.Barrier(2)
        def reserve():barrier.wait(timeout=10);return self.progress.reserve('scan')
        def prepare():
            barrier.wait(timeout=10)
            try:self.prepare();return True
            except ValueError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            reserved=pool.submit(reserve);prepared=pool.submit(prepare)
            spent,ready=reserved.result(),prepared.result()
        self.assertNotEqual(spent,ready)
        state=self.progress.admission('scan')
        self.assertEqual(state['requests_used'],int(spent))
        if spent:self.prepare(1)
        source=self.source(int(spent));self.seal(source)
        self.assertEqual(self.progress.admission('scan')['prepared_requests_used'],int(spent))

    def test_concurrent_idempotent_admission_and_seal(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.progress.admit('scan',self.descriptor),range(4)))
        self.assertTrue(all(r['requests_used']==0 for r in results));self.prepare()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.seal(self.source()),range(4)))
        self.assertTrue(all(r['state']=='SEALED' for r in results))
    def test_sqlite_abort_prepare_and_seal_roll_back_and_can_recover(self):
        self.progress.reserve('scan')
        with self.store.connect() as c:
            c.execute("CREATE TRIGGER stop_prepare BEFORE UPDATE ON ownership_admissions WHEN NEW.state='PREPARED' BEGIN SELECT RAISE(ABORT,'fixture crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.prepare(1)
        self.assertEqual(self.progress.admission('scan')['state'],'ADMITTED')
        with self.store.connect() as c:c.execute('DROP TRIGGER stop_prepare')
        self.prepare(1)
        with self.store.connect() as c:
            c.execute("CREATE TRIGGER stop_seal BEFORE UPDATE ON ownership_admissions WHEN NEW.state='SEALED' BEGIN SELECT RAISE(ABORT,'fixture crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.seal(self.source(1))
        self.assertEqual(self.progress.admission('scan')['state'],'PREPARED')
        with self.store.connect() as c:c.execute('DROP TRIGGER stop_seal')
        self.assertEqual(self.seal(self.source(1))['requests_used'],1)
    def test_admission_insert_crash_rolls_back_budget_row(self):
        with self.store.connect() as c:
            c.execute("CREATE TRIGGER stop_admit BEFORE INSERT ON ownership_admissions BEGIN SELECT RAISE(ABORT,'fixture crash'); END")
        descriptor={**self.descriptor,'scan_id':'other'}
        with self.assertRaises(sqlite3.IntegrityError):self.progress.admit('other',descriptor)
        with self.store.connect() as c:self.assertIsNone(c.execute("SELECT 1 FROM ownership_budgets WHERE id='other'").fetchone())
    def test_corrupt_lifecycle_blocks_reservation_without_io(self):
        self.prepare()
        with self.store.connect() as c:c.execute("UPDATE ownership_admissions SET prepared_source='{}' WHERE id='scan'")
        with self.assertRaises(ValueError):self.progress.reserve('scan')
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT used FROM ownership_budgets').fetchone()[0],0)
    def test_process_death_after_reservation_and_preparation_recovers_exact_source(self):
        import subprocess,sys
        code="""
import json,os,sys
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
p=HistoryProgress(EvidenceStore(sys.argv[1]))
d=json.loads(sys.argv[2]);a=p.admit('scan',d)
if sys.argv[3]=='reserve':
    assert p.reserve('scan')
else:
    p.prepare_source('scan',a['descriptor_hash'],json.loads(sys.argv[3]))
os._exit(7)
"""
        args=[sys.executable,'-c',code,str(self.path),canonical(self.descriptor)]
        self.assertEqual(subprocess.run(args+['reserve'],capture_output=True,timeout=10).returncode,7)
        self.assertEqual(self.progress.admission('scan')['requests_used'],1)
        self.assertEqual(subprocess.run(args+[canonical(self.source(1))],capture_output=True,timeout=10).returncode,7)
        recovered=HistoryProgress(EvidenceStore(self.path)).admission('scan')
        self.assertEqual(recovered['state'],'PREPARED');self.assertEqual(recovered['prepared_source'],self.source(1))
        self.assertFalse(self.progress.reserve('scan'));self.assertEqual(self.seal(self.source(1))['requests_used'],1)

    def test_prepared_backup_restores_exact_recovery_source_and_usage(self):
        self.progress.reserve('scan');prepared=self.prepare(1);target=Path(self.tmp.name)/'restore.sqlite'
        with self.store.connect() as c,sqlite3.connect(target) as out:c.backup(out)
        restored=HistoryProgress(EvidenceStore(target))
        self.assertEqual(restored.admission('scan'),prepared)
        sealed=restored.seal_source('scan',self.binding,prepared['completed_source_hash'])
        self.assertEqual(sealed['requests_used'],1);self.assertEqual(self.progress.admission('scan')['state'],'PREPARED')

class LegacyBudgetMigrationTests(unittest.TestCase):
    def test_additive_schema_keeps_existing_row_and_evidence_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            store=EvidenceStore(Path(root)/'evidence.sqlite');key=store.save({'fixture':'legacy original'})
            with store.connect() as c:
                c.execute('CREATE TABLE ownership_budgets(id TEXT PRIMARY KEY,source_hash TEXT NOT NULL,used INTEGER NOT NULL,ceiling INTEGER NOT NULL)')
                c.execute('INSERT INTO ownership_budgets VALUES(?,?,?,?)',('legacy','a'*64,7,18))
                before=c.execute('SELECT * FROM ownership_budgets').fetchall();payload=c.execute('SELECT * FROM pages').fetchall()
                ddl=c.execute("SELECT sql FROM sqlite_master WHERE name='ownership_budgets'").fetchone()
            progress=HistoryProgress(store);progress.budget('legacy','a'*64,0)
            self.assertIsNone(progress.admission('legacy'))
            descriptor={'kind':'ownership_admission_v1','scan_id':'legacy','mint':MINT,'created':20}
            with self.assertRaises(ValueError):progress.admit('legacy',descriptor)
            with store.connect() as c:
                self.assertEqual(c.execute('SELECT * FROM ownership_budgets').fetchall(),before)
                self.assertEqual(c.execute('SELECT * FROM pages').fetchall(),payload)
                self.assertEqual(c.execute("SELECT sql FROM sqlite_master WHERE name='ownership_budgets'").fetchone(),ddl)
            self.assertTrue(progress.reserve('legacy'));self.assertEqual(store.load(key),{'fixture':'legacy original'})

class AdmissionWorkerCompatibilityTests(unittest.TestCase):
    def setUp(self):
        worker_fixtures.OwnershipWorkerTests.setUp(self)
        # New admission seeds require explicit research-only rejection; legacy
        # fixture reports intentionally need no descriptor or new fields.
        self.report['eligible_for_trading']=False;self.report.pop('report_hash')
        self.report['report_hash']=digest(self.report)
        with sqlite3.connect(self.db) as c:c.execute('UPDATE scans SET result=?',(json.dumps(self.report),))
    def admit(self):
        self.progress=HistoryProgress(self.store)
        descriptor={'kind':'ownership_admission_v1','scan_id':'scan','mint':self.mint,'created':20}
        self.binding=self.progress.admit('scan',descriptor)['descriptor_hash']
        for _ in range(7):self.progress.reserve('scan')
        with sqlite3.connect(self.db) as c:
            c.row_factory=sqlite3.Row;self.source=dict(c.execute('SELECT * FROM scans').fetchone())
    def test_existing_worker_accepts_exact_sealed_source_without_second_counter(self):
        self.admit();self.progress.prepare_source('scan',self.binding,self.source)
        self.progress.seal_source('scan',self.binding,digest(self.source))
        result=advance(self.db,self.evidence,'scan',lambda *a:{'data':[]})
        self.assertEqual(result['requests_used'],8);self.assertFalse(result['eligible_for_trading'])
        again=advance(self.db,self.evidence,'scan',lambda *a:self.fail('Completed history fetched again'))
        self.assertEqual(again['requests_used'],8)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT id,source_hash,used FROM ownership_budgets').fetchall(),[('scan',self.binding,8)])
        with sqlite3.connect(self.db) as c:
            c.row_factory=sqlite3.Row;self.assertEqual(dict(c.execute('SELECT * FROM scans').fetchone()),self.source)
    def test_unsealed_and_changed_completed_source_worker_fail_before_io(self):
        self.admit()
        with self.assertRaises(ValueError):advance(self.db,self.evidence,'scan',lambda *a:self.fail('Unsealed I/O'))
        self.progress.prepare_source('scan',self.binding,self.source)
        with self.assertRaises(ValueError):advance(self.db,self.evidence,'scan',lambda *a:self.fail('Prepared I/O'))
        self.progress.seal_source('scan',self.binding,digest(self.source))
        report=json.loads(self.source['result']);report['notice']='changed';report.pop('report_hash');report['report_hash']=digest(report)
        with sqlite3.connect(self.db) as c:c.execute('UPDATE scans SET result=?',(json.dumps(report),))
        with self.assertRaises(ValueError):advance(self.db,self.evidence,'scan',lambda *a:self.fail('Changed source I/O'))
        self.assertEqual(self.progress.admission('scan')['requests_used'],7)
