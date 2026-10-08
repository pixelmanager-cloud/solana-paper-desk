"""Only synthetic provider fixtures; production raw replay and CLI code execute."""
import copy,json,os,sqlite3,subprocess,sys,tempfile,threading,unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.job_persistence import JobPersistence,BIRTH_ACQUISITION_V1,SCREEN,_Worker
from desk.model import canonical,digest
from desk.ownership_acquisition import acquire,_Setup
from desk.ownership_worker import advance
from desk.replay_history import reconstruct_launch_history
from desk.decision_runner import consume
from desk.security import TOKEN_2022,base58
from tests.test_ownership_integration import synthetic_launch

class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.db=self.root/'research.sqlite';self.evidence=self.root/'evidence.sqlite'
        self.mint,self.owner,self.account,self.raw,self.values=synthetic_launch()
        self.calls=[];self.fail_method=None;self.pages=None;self.cutoff=20
    def jobs(self):return JobPersistence(self.db)
    def admit(self,mint=None):
        with patch('desk.job_persistence.time.time',return_value=100000):
            return self.jobs().admit(mint or self.mint,kind=BIRTH_ACQUISITION_V1,evidence_db=self.evidence)
    def source(self,uid):return self.jobs().source(uid)
    def progress(self):return HistoryProgress(EvidenceStore(self.evidence))
    def rpc(self,method,params):
        self.calls.append((method,copy.deepcopy(params)))
        with EvidenceStore(self.evidence).connect() as c:
            self.assertEqual(c.execute('SELECT used FROM ownership_budgets').fetchone()[0],len(self.calls))
        if method==self.fail_method:raise OSError('secret-provider-error-fixture')
        if method=='getAccountInfo':return {'value':self.values[0],'context':{'slot':19}}
        if method=='getSlot':self.assertEqual(params,[{'commitment':'finalized'}]);return self.cutoff
        if method=='getMultipleAccounts':return {'context':{'slot':20},'value':self.values}
        if method=='getBlockTime':return 105
        self.assertEqual(method,'getTransactionsForAddress')
        self.assertEqual(params[1]['filters']['slot'],{'gte':0,'lt':21});self.assertNotIn('blockTime',params[1]['filters'])
        if self.pages is not None:
            index=int(params[1].get('paginationToken','0'));return copy.deepcopy(self.pages[index])
        return {'data':[self.raw]}
    def run_acquire(self,uid=None):
        with patch('desk.job_persistence.time.time',return_value=100000):
            return acquire(self.db,self.evidence,self.rpc,**({'scan_id':uid} if uid else {'mint':self.mint}))
    def test_old_birth_positive_shared_budget_and_readonly_sealed_consumer(self):
        result=self.run_acquire();uid=result['scan_id'];report=result['report'];source=self.source(uid)
        self.assertEqual(result['status'],'SEED_HISTORY_ACQUIRED');self.assertEqual(result['provider_calls'],3)
        self.assertEqual(report['calls'],3);self.assertLess(self.raw['blockTime'],report['observed_at']-21600)
        self.assertEqual(report['history_queries'][0]['slot_range'],{'gte':0,'lt':21})
        replay=reconstruct_launch_history(report,EvidenceStore(self.evidence,read_only=True))
        self.assertTrue(replay['launch_verified']);self.assertTrue(replay['inventory']['initialization_inventory_verified'])
        self.assertEqual(self.progress().admission(uid)['state'],'SEALED')
        for _ in range(3):continued=advance(self.db,self.evidence,uid,self.rpc,max_calls=4)
        self.assertTrue(continued['snapshot']['reconciled']);self.assertEqual(continued['requests_used'],6)
        decision=consume(self.db,self.root/'journal.sqlite',now=100000,evidence_db=self.evidence)['decisions'][0]
        component=decision['entry_evidence']['continuation_evidence']
        self.assertTrue(component['replay']['reconciled'])
        self.assertFalse(decision['eligible_for_trading']);self.assertEqual(decision['observed_at'],100000)
        self.assertEqual(source,self.source(uid));self.assertFalse(continued['eligible_for_trading'])
        retry=self.run_acquire(uid);self.assertEqual(retry['status'],'ALREADY_COMPLETE');self.assertEqual(len(self.calls),6)
    def test_ordinary_screen_keeps_six_hour_default_and_rejects_old_birth(self):
        from desk.screen import screen
        def rpc(method,params):
            if method=='getAccountInfo':return {'value':self.values[0]}
            if method=='getTokenLargestAccounts':return {'value':[]}
            if method=='getTransactionsForAddress':return {'data':[self.raw]}
            raise OSError('fixture unsupported optional scan probe')
        result=screen(self.mint,rpc=rpc,quote=lambda *a:(_ for _ in ()).throw(OSError()),clock=lambda:100000)
        self.assertEqual(result['history']['window_seconds'],21600)
        self.assertFalse(any(a['verified'] for a in result['launch_anchors']))
        self.assertFalse(result['eligible_for_trading'])
    def test_two_page_seed_partial_immutable_then_ownership_continuation(self):
        self.pages=[{'data':[self.raw],'paginationToken':'1'},{'data':[self.raw],'paginationToken':'2'},{'data':[]}]
        result=self.run_acquire();uid=result['scan_id'];source=self.source(uid)
        self.assertEqual(result['provider_calls'],4);self.assertEqual(result['status'],'SEED_HISTORY_PARTIAL')
        coverage=result['report']['history_queries'][0];self.assertEqual(len(coverage['pages']),2)
        # Duplicate fixtures intentionally remain incomplete, never invent ownership.
        continued=advance(self.db,self.evidence,uid,self.rpc)
        self.assertEqual(continued['requests_used'],5);self.assertEqual(source,self.source(uid))
        self.assertFalse(continued['history']['inventory']['initialization_inventory_verified'])
    def test_unsupported_token_never_spends_cutoff_or_history(self):
        self.values[0]['owner']=TOKEN_2022
        result=self.run_acquire();self.assertEqual(result['status'],'UNSUPPORTED_TOKEN')
        self.assertEqual(result['provider_calls'],1);self.assertEqual(result['report']['findings'],['TOKEN_2022_NOT_ALLOWED'])
        self.assertIsNone(result['report']['acquisition']['cutoff_hash']);self.assertFalse(result['eligible_for_trading'])
    def test_unknown_creation_anchor_does_not_become_supported(self):
        self.raw['transaction']['message']['instructions'][0]['programId']=base58(bytes([9])*32)
        result=self.run_acquire();self.assertEqual(result['status'],'LAUNCH_OR_INVENTORY_UNVERIFIED')
        continued=advance(self.db,self.evidence,result['scan_id'],self.rpc)
        self.assertFalse(continued['history']['launch_verified']);self.assertFalse(continued['eligible_for_trading'])
    def test_setup_failure_charged_resume_id_no_duplicate_reset_or_error_leak(self):
        self.fail_method='getSlot';failed=self.run_acquire();uid=failed['scan_id']
        self.assertEqual(failed['requests_used'],2);self.assertEqual(self.source(uid)['status'],'INTERRUPTED')
        self.assertNotIn('secret-provider',canonical(failed))
        with self.assertRaises(ValueError):self.run_acquire()
        self.fail_method=None;result=self.run_acquire(uid)
        self.assertEqual(result['requests_used'],4);self.assertEqual(result['provider_calls'],2)
        self.assertEqual(sum(m=='getAccountInfo' for m,p in self.calls),1)
        with self.jobs().connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM scans').fetchone()[0],1)
    def test_history_error_keeps_cutoff_and_cursor_then_resume(self):
        self.pages=[{'data':[self.raw],'paginationToken':'1'},{'data':[]}]
        original=self.rpc
        def failing(method,params):
            if method=='getTransactionsForAddress' and params[1].get('paginationToken')=='1':
                self.fail_method=method
            return original(method,params)
        uid=self.admit();failed=acquire(self.db,self.evidence,failing,scan_id=uid)
        self.assertEqual(failed['requests_used'],4);self.assertEqual(failed['status'],'PROVIDER_RETRY_REQUIRED')
        self.fail_method=None;result=self.run_acquire(uid)
        self.assertEqual(result['requests_used'],4);self.assertEqual(result['provider_calls'],0)
        self.assertEqual(self.calls[-1][1][1]['paginationToken'],'1')
        self.assertEqual(sum(m=='getSlot' for m,p in self.calls),1)
    def test_invalid_cutoff_finality_never_persists_or_queries_history(self):
        for index,value in enumerate((True,-1,{'context':{'slot':20}},None)):
            with self.subTest(value=value):
                self.db=self.root/f'case{index}-research.sqlite';self.evidence=self.root/f'case{index}-evidence.sqlite'
                self.calls=[];self.cutoff=value;uid=self.admit()
                result=self.run_acquire(uid)
                self.assertEqual(result['status'],'ACQUISITION_RETRY_OR_EVIDENCE_BLOCKED')
                self.assertEqual(result['requests_used'],2)
                with EvidenceStore(self.evidence).connect() as c:
                    self.assertIsNone(c.execute('SELECT cutoff_hash FROM ownership_acquisition_setup').fetchone()[0])
                self.assertEqual(len(self.calls),2)
    def test_original_cutoff_retained_after_provider_changes_on_restart(self):
        self.fail_method='getTransactionsForAddress';result=self.run_acquire();uid=result['scan_id']
        self.cutoff=500;self.fail_method=None;result=self.run_acquire(uid)
        self.assertEqual(result['report']['acquisition']['cutoff'],20)
        self.assertEqual(sum(m=='getSlot' for m,p in self.calls),1)
    def test_failed_setup_budget_exhaustion_is_terminal_without_extra_rpc(self):
        self.fail_method='getAccountInfo';result=self.run_acquire();uid=result['scan_id']
        for _ in range(3):result=self.run_acquire(uid)
        self.assertEqual(result['requests_used'],4);self.assertEqual(len(self.calls),4)
        terminal=self.run_acquire(uid);self.assertEqual(terminal['status'],'ACQUISITION_REQUEST_LIMIT_REACHED')
        self.assertEqual(terminal['provider_calls'],0);self.assertEqual(terminal['report']['calls'],4)
        self.assertEqual(self.progress().admission(uid)['state'],'SEALED')
    def test_wrong_evidence_or_research_identity_before_provider_calls(self):
        uid=self.admit()
        with self.assertRaises(ValueError):acquire(self.db,self.root/'wrong.sqlite',self.rpc,scan_id=uid)
        with self.assertRaises(ValueError):acquire(self.db,self.db,self.rpc,scan_id=uid)
        copied=self.root/'copy.sqlite'
        with self.jobs().connect() as c,sqlite3.connect(copied) as out:c.backup(out)
        with self.assertRaises(ValueError):acquire(copied,self.evidence,self.rpc,scan_id=uid)
        self.assertEqual(self.calls,[]);self.assertFalse((self.root/'wrong.sqlite').exists())
    def test_screen_jobs_cannot_resume_as_acquisition(self):
        uid=self.jobs().admit(self.mint)
        with self.assertRaises(ValueError):self.run_acquire(uid)
        self.assertEqual(self.source(uid)['status'],'QUEUED');self.assertEqual(self.calls,[])
    def test_stale_fenced_generation_cannot_interrupt_or_publish(self):
        uid=self.admit();jobs=self.jobs()
        with jobs.worker() as worker:
            claim=worker.claim_acquisition(uid)
            with self.assertRaises(ValueError):worker.interrupt_acquisition(replace(claim,generation=claim.generation+1))
            with self.assertRaises(ValueError):worker.publish(claim,'COMPLETE',{'eligible_for_trading':False})
            worker.interrupt_acquisition(claim);new=worker.claim_acquisition(uid)
            with self.assertRaises(ValueError):worker.interrupt_acquisition(claim)
            self.assertEqual(new.generation,claim.generation+1)
            worker.interrupt_acquisition(new)
    def test_completed_scan_cannot_be_claimed_and_original_result_preserved(self):
        result=self.run_acquire();uid=result['scan_id'];before=self.source(uid)
        with self.jobs().worker() as worker:
            with self.assertRaises(ValueError):worker.claim_acquisition(uid)
        self.assertEqual(self.source(uid),before)
    def test_interrupted_acquisitions_count_queue_capacity(self):
        ids=[self.admit(base58(bytes([30+i])*32)) for i in range(3)]
        with self.jobs().worker() as worker:
            for uid in ids:worker.interrupt_acquisition(worker.claim_acquisition(uid))
        with self.assertRaisesRegex(ValueError,'Queue full'):self.admit(base58(bytes([40])*32))
    def test_concurrent_real_symlink_workers_do_not_claim_or_publish_competing_seed(self):
        uid=self.admit();alias=self.root/'alias.sqlite';alias.symlink_to(self.db)
        entered=threading.Event();release=threading.Event();original=self.rpc
        def pause(method,params):
            if method=='getSlot':entered.set();self.assertTrue(release.wait(10))
            return original(method,params)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(acquire,self.db,self.evidence,pause,scan_id=uid)
            try:
                self.assertTrue(entered.wait(10))
                busy=acquire(alias,self.evidence,lambda *a:self.fail('Competing RPC'),scan_id=uid)
                self.assertEqual(busy['status'],'BUSY');self.assertEqual(self.source(uid)['status'],'RUNNING')
            finally:release.set()
            self.assertEqual(future.result()['status'],'SEED_HISTORY_ACQUIRED')
        self.assertEqual(len(self.calls),3)
    def test_evidence_busy_leaves_queued_job_and_no_budget(self):
        import fcntl
        uid=self.admit();store=EvidenceStore(self.evidence)
        with open(str(store.path)+'.ownership-invocation.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            busy=self.run_acquire(uid);self.assertEqual(busy['status'],'BUSY')
        self.assertEqual(self.source(uid)['status'],'QUEUED');self.assertEqual(self.calls,[])
    def test_setup_and_pointer_publication_are_atomic_on_storage_abort(self):
        uid=self.admit()
        original=_Setup.save
        def fail(setup,name,record):
            if name=='cutoff_hash':
                with setup.store.connect() as c:
                    c.execute("CREATE TRIGGER reject_setup BEFORE UPDATE ON ownership_acquisition_setup WHEN NEW.cutoff_hash IS NOT NULL BEGIN SELECT RAISE(ABORT,'fixture crash'); END")
            return original(setup,name,record)
        with patch.object(_Setup,'save',fail):
            with self.assertRaises(sqlite3.IntegrityError):self.run_acquire(uid)
        key=digest({'method':'getSlot','params':[{'commitment':'finalized'}],'result':20})
        with self.assertRaises(ValueError):EvidenceStore(self.evidence).load(key)
        self.assertEqual(self.progress().admission(uid)['requests_used'],2)
        with EvidenceStore(self.evidence).connect() as c:c.execute('DROP TRIGGER reject_setup')
        result=self.run_acquire(uid);self.assertEqual(result['requests_used'],4)
    def test_prepare_and_seal_crashes_recover_identical_report_without_rpc(self):
        uid=self.admit();original=HistoryProgress.prepare_source
        def crash(*a,**kw):original(*a,**kw);raise KeyboardInterrupt()
        with patch.object(HistoryProgress,'prepare_source',crash):
            with self.assertRaises(KeyboardInterrupt):self.run_acquire(uid)
        prepared=self.progress().admission(uid);self.assertEqual(prepared['state'],'PREPARED')
        original_publish=_Worker.publish_acquisition
        def after_seal(*a,**kw):raise KeyboardInterrupt()
        with patch.object(_Worker,'publish_acquisition',after_seal):
            with self.assertRaises(KeyboardInterrupt):self.run_acquire(uid)
        self.assertEqual(self.progress().admission(uid)['state'],'SEALED')
        recovered=acquire(self.db,self.evidence,lambda *a:self.fail('Recovery recapture'),scan_id=uid)
        self.assertEqual(recovered['status'],'RECOVERED_COMPLETE')
        self.assertEqual(self.source(uid),prepared['prepared_source']);self.assertEqual(len(self.calls),3)
    def test_setup_hash_method_or_finality_rebinding_blocks_resume(self):
        self.fail_method='getTransactionsForAddress';uid=self.run_acquire()['scan_id']
        store=EvidenceStore(self.evidence)
        wrong=store.save({'method':'getSlot','params':[{'commitment':'confirmed'}],'result':20})
        with store.connect() as c:
            with self.assertRaises(sqlite3.IntegrityError):c.execute('UPDATE ownership_acquisition_setup SET cutoff_hash=?',(wrong,))
            c.execute('DROP TRIGGER acquisition_setup_update')
            c.execute('UPDATE ownership_acquisition_setup SET cutoff_hash=?',(wrong,))
        with self.assertRaises(ValueError):acquire(self.db,self.evidence,lambda *a:self.fail('Wrong finality'),scan_id=uid)
        self.assertEqual(self.progress().admission(uid)['requests_used'],3)
    def test_missing_charged_setup_cannot_recapture_or_reset(self):
        self.fail_method='getSlot';uid=self.run_acquire()['scan_id']
        with EvidenceStore(self.evidence).connect() as c:
            with self.assertRaises(sqlite3.IntegrityError):c.execute('DELETE FROM ownership_acquisition_setup')
            c.execute('DROP TRIGGER acquisition_setup_delete')
            c.execute('DELETE FROM ownership_acquisition_setup')
        with self.assertRaises(ValueError):acquire(self.db,self.evidence,lambda *a:self.fail('Recapture'),scan_id=uid)
        self.assertEqual(self.progress().admission(uid)['requests_used'],2)

    def test_archive_crash_boundaries_preserve_spent_failures_and_source(self):
        import desk.ownership_acquisition as module
        self.fail_method='getSlot';uid=self.run_acquire()['scan_id'];self.fail_method=None
        archive=module._archive_seed
        for after in (False,True):
            def crash(store,source):
                if after:archive(store,source)
                raise KeyboardInterrupt()
            with patch.object(module,'_archive_seed',crash):
                with self.assertRaises(KeyboardInterrupt):self.run_acquire(uid)
            admission=self.progress().admission(uid)
            self.assertEqual(admission['requests_used'],4)
            self.assertEqual(json.loads(admission['prepared_source']['result'])['calls'],4)
            self.assertEqual(len(self.calls),4)
        recovered=acquire(self.db,self.evidence,lambda *a:self.fail('Handoff recharge'),scan_id=uid)
        self.assertEqual(recovered['provider_calls'],0)
        with EvidenceStore(self.evidence).connect() as c:
            row=c.execute('SELECT source_hash,budget,attempts FROM ownership_acquisition_history').fetchone()
            self.assertEqual(row,(digest(admission['prepared_source']),uid,1))
            with self.assertRaises(sqlite3.IntegrityError):c.execute('DELETE FROM ownership_acquisition_history')
        terminal=acquire(self.db,self.evidence,lambda *a:self.fail('Sealed acquisition resumed'),scan_id=uid)
        self.assertEqual(terminal['status'],'ALREADY_COMPLETE');self.assertEqual(terminal['requests_used'],4)

    def test_setup_row_identity_aliases_and_replace_collisions_preserve_charged_jobs(self):
        first=self.admit();second=self.admit(self.owner)
        store=EvidenceStore(self.evidence);progress=HistoryProgress(store)
        for uid in (first,second):
            descriptor=self.jobs().descriptor(uid)
            admission=progress.admit(uid,{'kind':'ownership_admission_v1','scan_id':uid,
                                         'mint':descriptor['mint'],'created':descriptor['admitted_at']})
            _Setup(store,descriptor,admission)
            self.assertTrue(progress.reserve(uid))
        with store.connect() as c:
            before=c.execute('SELECT rowid,* FROM ownership_acquisition_setup ORDER BY rowid').fetchall()
            budget=c.execute('SELECT * FROM ownership_budgets ORDER BY id').fetchall()
            for alias in ('rowid','_rowid_','oid'):
                for conflict in ('','OR REPLACE'):
                    for target in (before[1][0],99):
                        with self.subTest(alias=alias,conflict=conflict,target=target):
                            with self.assertRaises(sqlite3.IntegrityError):
                                c.execute('UPDATE '+conflict+' ownership_acquisition_setup SET '+alias+'=? WHERE scan_id=?',(target,first))
                            self.assertEqual(c.execute('SELECT rowid,* FROM ownership_acquisition_setup ORDER BY rowid').fetchall(),before)
                            self.assertEqual(c.execute('SELECT * FROM ownership_budgets ORDER BY id').fetchall(),budget)
            # Existing databases receive the new guard when setup is reopened.
            c.execute('DROP TRIGGER acquisition_setup_identity')
        _Setup(store,self.jobs().descriptor(first),progress.admission(first))
        with store.connect() as c:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE OR REPLACE ownership_acquisition_setup SET rowid=? WHERE scan_id=?",(before[1][0],first))
        self.assertEqual(progress.admission(first)['requests_used'],1)
        self.assertEqual(progress.admission(second)['requests_used'],1)

    def test_real_cli_fixture_backend_and_mutually_exclusive_target(self):
        fixture=self.root/'fixture.json';fixture.write_text(canonical({'mint':self.mint,'raw':self.raw,'values':self.values}))
        child="""
import json,sys
from desk import cli
from desk import providers
f=json.load(open(sys.argv[1]))
def rpc(method,params):
    if method=='getAccountInfo':return {'value':f['values'][0]}
    if method=='getSlot':return 20
    if method=='getTransactionsForAddress':return {'data':[f['raw']]}
    raise AssertionError(method)
providers.helius_rpc=rpc
sys.argv=['desk','ownership-acquire','--db',sys.argv[2],'--evidence-db',sys.argv[3],'--mint',f['mint']]
cli.main()
"""
        run=subprocess.run([sys.executable,'-c',child,str(fixture),str(self.db),str(self.evidence)],capture_output=True,text=True,timeout=10)
        self.assertEqual(run.returncode,0,run.stderr);result=json.loads(run.stdout)
        self.assertEqual(result['status'],'SEED_HISTORY_ACQUIRED');self.assertFalse(result['eligible_for_trading'])
        bad=subprocess.run([sys.executable,'-m','desk','ownership-acquire','--db',str(self.db),'--evidence-db',str(self.evidence),
                            '--mint',self.mint,'--scan-id',result['scan_id']],capture_output=True,text=True,timeout=10)
        self.assertEqual(bad.returncode,2)
