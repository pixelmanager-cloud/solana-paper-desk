"""Synthetic read-only capture: one original signature, no production RPC."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress, ownership_lock_path
from desk.model import canonical, digest
from desk.raw_transaction_capture import RawTransactionCapture, TransactionBinding, ReadOnlyRPC, CONFIG, SCHEMA
from desk.security import base58, TOKEN_2022

MINT=base58(bytes([1])*32);OWNER=base58(bytes([2])*32)
SIGNATURE=base58(bytes([3])*64);OTHER_SIGNATURE=base58(bytes([4])*64)


def transaction():
    return {'version':'legacy','slot':10,'blockTime':100,'transaction':{'signatures':[SIGNATURE],
        'message':{'accountKeys':[OWNER,MINT,TOKEN_2022],
                   'header':{'numRequiredSignatures':1,'numReadonlySignedAccounts':0,'numReadonlyUnsignedAccounts':1},
                   'instructions':[{'programIdIndex':2,'accounts':[1],'data':base58(b'\x16')}] }},
        'meta':{'err':None,'innerInstructions':[{'index':0,'instructions':[{'programIdIndex':2,'accounts':[1],
                                                                                  'data':base58(b'\x14\x06'+bytes([2])*32+b'\0'),'stackHeight':2}]}]}}


def die_during_capture(path,binding,before_reserve):
    store=EvidenceStore(path)
    def rpc(*args):os._exit(77)
    capture=RawTransactionCapture(store,ReadOnlyRPC('fixture-source',rpc))
    if before_reserve:
        with patch.object(HistoryProgress,'reserve',lambda *a:os._exit(76)):capture.capture(binding)
    else:capture.capture(binding)


class RawTransactionCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=EvidenceStore(self.root/'evidence.sqlite')
        self.progress=HistoryProgress(self.store)
        self.descriptor={'kind':'ownership_admission_v1','scan_id':'scan','mint':MINT,'created':100}
        self.admission=self.progress.admit('scan',self.descriptor)
        self.binding=TransactionBinding('scan',self.admission['descriptor_hash'],MINT,SIGNATURE)
        self.calls=[];self.response=transaction()
        self.capture=RawTransactionCapture(self.store,ReadOnlyRPC('fixture-source',self.rpc))
    def rpc(self,method,params):
        self.calls.append((method,deepcopy(params)))
        self.assertEqual(self.progress.admission('scan')['requests_used'],len(self.calls))
        self.assertEqual((method,params),('getTransaction',[SIGNATURE,CONFIG]))
        with closing(self.store.connect()) as c:self.assertEqual(c.execute('SELECT count(*) FROM raw_transaction_claims').fetchone()[0],1)
        return deepcopy(self.response)
    def assert_unapproved(self,result):
        for flag in ('lifecycle_verified','finality_authenticated','signature_authenticated','source_authenticated',
                     'caller_privileges_verified','cpi_success_verified','account_state_verified','ownership_approved','eligible_for_trading'):
            self.assertIs(result[flag],False,flag)
    def test_positive_preserves_request_raw_response_and_zero_io_replay(self):
        original=deepcopy(self.response);result=self.capture.capture(self.binding)
        self.assertEqual(result['state'],'COMPLETE',result);self.assertEqual(result['provider_calls'],1)
        self.assertEqual(result['diagnostics']['slot'],10);self.assert_unapproved(result)
        self.assertIn('PR49 dedicated transport',result['transport_dependency'])
        request=self.store.load(result['request_hash']);response=self.store.load(result['response_hash'])
        self.assertEqual(request,{'kind':'raw_transaction_request_v1','method':'getTransaction','params':[SIGNATURE,CONFIG]})
        self.assertEqual(response,{'kind':'rpc_response_v1','method':'getTransaction','params':[SIGNATURE,CONFIG],'result':original})
        again=RawTransactionCapture(self.store,ReadOnlyRPC('fixture-source',lambda *a:self.fail('Replay RPC'))).capture(self.binding)
        self.assertEqual(again['state'],'COMPLETE');self.assertEqual(again['provider_calls'],0)
        self.assertEqual(again['response_hash'],result['response_hash']);self.assertEqual(again['requests_used'],1)
        self.assertEqual(self.response,original)
    def test_loaded_mint_presence_and_loaded_account_order_validated(self):
        message=self.response['transaction']['message'];message['accountKeys']=[OWNER,TOKEN_2022]
        self.response['version']=0
        message['addressTableLookups']=[{'accountKey':base58(bytes([7])*32),'writableIndexes':[0],'readonlyIndexes':[]}]
        self.response['meta']['loadedAddresses']={'writable':[MINT],'readonly':[]}
        message['instructions'][0].update(programIdIndex=1,accounts=[2])
        self.response['meta']['innerInstructions'][0]['instructions'][0].update(programIdIndex=1,accounts=[2])
        self.assertEqual(self.capture.capture(self.binding)['state'],'COMPLETE')
    def test_missing_admission_legacy_budget_wrong_hash_mint_scan_and_signature_no_io(self):
        for binding in (replace(self.binding,scan_id='new'),replace(self.binding,descriptor_hash='0'*64),
                        replace(self.binding,mint=OWNER),replace(self.binding,signature='sig'),{'scan_id':'scan'}):
            with self.subTest(binding=binding):self.assertEqual(self.capture.capture(binding)['state'],'BLOCKED')
        self.progress.budget('legacy','a'*64,0)
        self.assertEqual(self.capture.capture(replace(self.binding,scan_id='legacy',descriptor_hash='a'*64))['state'],'BLOCKED')
        self.assertEqual(self.calls,[]);self.assertEqual(self.progress.admission('scan')['requests_used'],0)
        with closing(self.store.connect()) as c:self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='raw_transaction_claims'").fetchone())
    def test_failure_null_failed_and_invalid_raw_are_preserved_and_terminal(self):
        mutations=[lambda r:None,lambda r:{**r,'slot':True},lambda r:{**r,'meta':None},
                   lambda r:r['transaction']['signatures'].__setitem__(0,OTHER_SIGNATURE),
                   lambda r:r['transaction']['message']['accountKeys'].__setitem__(1,base58(bytes([8])*32)),
                   lambda r:r['meta'].update(err={'InstructionError':[0,'Custom']}),
                   lambda r:r['meta'].update(err=True),lambda r:r['meta'].update(innerInstructions=None),
                   lambda r:r['meta']['innerInstructions'][0]['instructions'][0].update(parsed={'type':'mintTo'}),
                   lambda r:r['transaction']['message']['instructions'][0].update(programIdIndex=True),
                   lambda r:r.update(version=1)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.setUp();raw=deepcopy(self.response);changed=mutate(raw)
                self.response=changed if isinstance(changed,dict) else None if changed is None and mutate==mutations[0] else raw
                result=self.capture.capture(self.binding);self.assertEqual(result['state'],'REJECTED',result)
                self.assertEqual(self.store.load(result['response_hash'])['result'],self.response)
                self.assertEqual(self.capture.capture(self.binding)['provider_calls'],0);self.assertEqual(len(self.calls),1)
                self.assert_unapproved(result)
    def test_provider_exception_is_charged_sanitized_and_cannot_use_new_signature(self):
        def fail(method,params):self.assertEqual(self.progress.admission('scan')['requests_used'],1);raise OSError('SECRET fixture URL')
        result=RawTransactionCapture(self.store,ReadOnlyRPC('fixture-source',fail)).capture(self.binding)
        self.assertEqual(result['state'],'FAILED');self.assertEqual(result['requests_used'],1)
        self.assertNotIn('SECRET',canonical(result));self.assertNotIn('SECRET',canonical(self.store.load(result['response_hash'])))
        self.assertEqual(self.capture.capture(self.binding)['state'],'FAILED')
        changed=self.capture.capture(replace(self.binding,signature=OTHER_SIGNATURE))
        self.assertEqual(changed['reason'],'CAPTURE_BINDING_CHANGED');self.assertEqual(self.calls,[])
    def test_signature_and_source_rebind_after_completion_cannot_hide_or_recharge(self):
        self.capture.capture(self.binding)
        self.assertEqual(self.capture.capture(replace(self.binding,signature=OTHER_SIGNATURE))['reason'],'CAPTURE_BINDING_CHANGED')
        other=RawTransactionCapture(self.store,ReadOnlyRPC('other-source',lambda *a:self.fail('Source rebind RPC')))
        self.assertEqual(other.capture(self.binding)['reason'],'CAPTURE_BINDING_CHANGED');self.assertEqual(len(self.calls),1)
    def test_shared_eighteen_ceiling_reservation_and_refusal_never_reset(self):
        for _ in range(17):self.assertTrue(self.progress.reserve('scan'))
        def last(method,params):self.assertEqual(self.progress.admission('scan')['requests_used'],18);return self.response
        result=RawTransactionCapture(self.store,ReadOnlyRPC('fixture-source',last)).capture(self.binding)
        self.assertEqual(result['state'],'COMPLETE');self.assertEqual(result['requests_used'],18)
        self.assertFalse(self.progress.reserve('scan'));self.assertEqual(self.capture.capture(self.binding)['provider_calls'],0)
        self.setUp()
        for _ in range(18):self.assertTrue(self.progress.reserve('scan'))
        result=self.capture.capture(self.binding);self.assertEqual(result['state'],'REFUSED');self.assertEqual(result['provider_calls'],0)
        self.assertEqual(result['reason'],'REQUEST_BUDGET_EXHAUSTED');self.assertEqual(self.calls,[])
        self.assertEqual(self.capture.capture(self.binding)['state'],'REFUSED')
    def seal(self):
        report={'mint':MINT,'calls':0,'eligible_for_trading':False};report['report_hash']=digest(report)
        source={'id':'scan','mint':MINT,'created':100,'status':'COMPLETE','result':canonical(report)}
        admission=self.progress.prepare_source('scan',self.binding.descriptor_hash,source)
        return source,admission
    def test_prepared_freezes_and_sealed_capture_preserves_original_source_bytes(self):
        source,admission=self.seal()
        result=self.capture.capture(self.binding);self.assertEqual(result['state'],'REFUSED');self.assertEqual(result['reason'],'SOURCE_PREPARED')
        self.assertEqual(self.calls,[]);self.assertEqual(self.progress.admission('scan')['prepared_source'],source)
        self.setUp();source,admission=self.seal();self.progress.seal_source('scan',self.binding.descriptor_hash,digest(source))
        result=self.capture.capture(self.binding);self.assertEqual(result['state'],'COMPLETE');self.assertEqual(result['requests_used'],1)
        after=self.progress.admission('scan');self.assertEqual(after['prepared_source'],source);self.assertEqual(after['completed_source_hash'],digest(source))
        self.assertTrue(self.progress.reserve('scan'));self.assertEqual(self.capture.capture(self.binding)['requests_used'],2)
        self.assertEqual(self.progress.admission('scan')['descriptor_hash'],self.binding.descriptor_hash)
    def test_process_death_before_reservation_or_during_io_is_durable_ambiguous(self):
        for before_reserve in (True,False):
            with self.subTest(before_reserve=before_reserve):
                self.setUp();child=multiprocessing.get_context('fork').Process(target=die_during_capture,args=(str(self.store.path),self.binding,before_reserve))
                child.start();child.join(5);self.assertFalse(child.is_alive());self.assertEqual(child.exitcode,76 if before_reserve else 77)
                result=self.capture.capture(self.binding);self.assertEqual(result['state'],'PENDING');self.assertEqual(result['provider_calls'],0)
                self.assertEqual(result['requests_used'],0 if before_reserve else 1)
                self.assertEqual(self.capture.capture(replace(self.binding,signature=OTHER_SIGNATURE))['reason'],'CAPTURE_BINDING_CHANGED')
                self.assertEqual(self.calls,[])
    def test_crash_after_response_before_terminal_and_after_terminal_never_recaptures(self):
        original=RawTransactionCapture._finish
        for committed in (False,True):
            self.setUp()
            def crash(capture,claim,body):
                if committed:original(capture,claim,body)
                raise KeyboardInterrupt()
            with patch.object(RawTransactionCapture,'_finish',crash):
                with self.assertRaises(KeyboardInterrupt):self.capture.capture(self.binding)
            replay=self.capture.capture(self.binding)
            self.assertEqual(replay['state'],'COMPLETE' if committed else 'PENDING');self.assertEqual(replay['provider_calls'],0)
            self.assertEqual(replay['requests_used'],1);self.assertEqual(len(self.calls),1)
            with closing(self.store.connect()) as c:self.assertEqual(c.execute('SELECT count(*) FROM pages').fetchone()[0],2)
    def test_storage_failure_retains_pending_and_spent_request(self):
        original=self.store.save
        def fail(record):
            if record['kind']=='rpc_response_v1':raise ValueError('fixture storage cap')
            return original(record)
        with patch.object(self.store,'save',fail):result=self.capture.capture(self.binding)
        self.assertEqual(result['state'],'BLOCKED');self.assertEqual(result['provider_calls'],1)
        replay=self.capture.capture(self.binding);self.assertEqual(replay['state'],'PENDING');self.assertEqual(replay['requests_used'],1)
        self.assertEqual(len(self.calls),1)
    def test_sql_terminal_abort_leaves_raw_response_pending_and_charged(self):
        original=RawTransactionCapture._finish
        def abort(capture,claim,body):
            with closing(capture.store.connect()) as c:
                c.execute("CREATE TRIGGER fixture_publication_abort BEFORE INSERT ON raw_transaction_results BEGIN SELECT RAISE(ABORT,'fixture storage crash'); END")
            return original(capture,claim,body)
        with patch.object(RawTransactionCapture,'_finish',abort):result=self.capture.capture(self.binding)
        self.assertEqual(result['state'],'BLOCKED');self.assertEqual(result['provider_calls'],1)
        self.assertEqual(result['requests_used'],1)
        replay=self.capture.capture(self.binding);self.assertEqual(replay['state'],'PENDING');self.assertEqual(replay['provider_calls'],0)
        self.assertEqual(len(self.calls),1)
        with closing(self.store.connect()) as c:self.assertEqual(c.execute('SELECT count(*) FROM pages').fetchone()[0],2)

    def test_real_symlink_concurrency_and_invocation_and_request_contention(self):
        pause=threading.Event();resume=threading.Event();self.addCleanup(resume.set)
        def held(method,params):pause.set();self.assertTrue(resume.wait(5));return self.rpc(method,params)
        alias=self.root/'alias.sqlite';alias.symlink_to(self.store.path)
        alternate=RawTransactionCapture(EvidenceStore(alias),ReadOnlyRPC('fixture-source',self.rpc))
        with ThreadPoolExecutor(max_workers=1) as executor:
            running=executor.submit(RawTransactionCapture(self.store,ReadOnlyRPC('fixture-source',held)).capture,self.binding)
            self.assertTrue(pause.wait(5));busy=alternate.capture(self.binding);self.assertEqual(busy['reason'],'BUSY')
            self.assertEqual(busy['provider_calls'],0);resume.set();self.assertEqual(running.result(5)['state'],'COMPLETE')
        self.assertEqual(len(self.calls),1)
        for invocation in (True,False):
            with open(ownership_lock_path(self.store,invocation=invocation),'a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertEqual(self.capture.capture(self.binding)['reason'],'BUSY')
    def test_hardlinks_readonly_and_copied_enrolled_database_fail_before_io(self):
        self.capture.capture(self.binding)
        clone=self.root/'copy.sqlite'
        with closing(self.store.connect()) as source,closing(sqlite3.connect(clone)) as target:source.backup(target)
        copied=RawTransactionCapture(EvidenceStore(clone),ReadOnlyRPC('fixture-source',lambda *a:self.fail('Copied DB recapture')))
        self.assertEqual(copied.capture(self.binding)['reason'],'CAPTURE_BINDING_CHANGED')
        with self.assertRaises(ValueError):RawTransactionCapture(EvidenceStore(self.store.path,read_only=True),ReadOnlyRPC('fixture-source',self.rpc))
        hard=self.root/'hard.sqlite';os.link(self.store.path,hard)
        with self.assertRaises(ValueError):RawTransactionCapture(self.store,ReadOnlyRPC('fixture-source',self.rpc))
    def test_immutable_rows_resist_alias_updates_delete_replace_and_rebinding(self):
        self.capture.capture(self.binding)
        with closing(self.store.connect()) as c:
            for table in ('raw_transaction_claims','raw_transaction_results'):
                before=c.execute('SELECT rowid,* FROM '+table).fetchall()
                for alias in ('rowid','_rowid_','oid'):
                    with self.assertRaises(sqlite3.IntegrityError):c.execute('UPDATE OR REPLACE '+table+' SET '+alias+'=99')
                with self.assertRaises(sqlite3.IntegrityError):c.execute('DELETE FROM '+table)
                with self.assertRaises(sqlite3.IntegrityError):c.execute('INSERT OR REPLACE INTO '+table+' SELECT * FROM '+table)
                self.assertEqual(c.execute('SELECT rowid,* FROM '+table).fetchall(),before)
    def test_corrupt_missing_evidence_or_budget_regression_never_recapture(self):
        result=self.capture.capture(self.binding)
        with closing(self.store.connect()) as c:c.execute('DELETE FROM pages WHERE hash=?',(result['response_hash'],))
        replay=self.capture.capture(self.binding);self.assertEqual(replay['state'],'BLOCKED');self.assertEqual(len(self.calls),1)
        self.setUp();self.capture.capture(self.binding)
        with closing(self.store.connect()) as c:c.execute('UPDATE ownership_budgets SET used=0')
        self.assertEqual(self.capture.capture(self.binding)['reason'],'CAPTURE_BUDGET_REGRESSION');self.assertEqual(len(self.calls),1)
    def test_token2022_diagnostic_uses_original_sealed_budget_without_changing_ownership_gate(self):
        from desk.ownership_acquisition import acquire
        from desk.ownership_worker import advance
        from desk.job_persistence import JobPersistence
        research=self.root/'research.sqlite'
        account={'owner':TOKEN_2022,'executable':False,'data':[base64.b64encode(bytes(82)).decode(),'base64']}
        def mint_rpc(method,params):
            self.assertEqual(method,'getAccountInfo');return {'value':account}
        seed=acquire(research,self.store.path,mint_rpc,mint=MINT)
        self.assertEqual(seed['status'],'UNSUPPORTED_TOKEN');uid=seed['scan_id']
        jobs=JobPersistence(research);original=jobs.source(uid)
        admission=self.progress.admission(uid)
        self.assertEqual(admission['requests_used'],1);self.assertEqual(admission['state'],'SEALED')
        def raw_rpc(method,params):
            self.assertEqual(self.progress.admission(uid)['requests_used'],2)
            self.assertEqual((method,params),('getTransaction',[SIGNATURE,CONFIG]));return transaction()
        capture=RawTransactionCapture(self.store,ReadOnlyRPC('fixture-source',raw_rpc))
        result=capture.capture(TransactionBinding(uid,admission['descriptor_hash'],MINT,SIGNATURE))
        self.assertEqual(result['state'],'COMPLETE');self.assertEqual(result['requests_used'],2);self.assert_unapproved(result)
        continued=advance(research,self.store.path,uid,lambda *a:self.fail('Unsupported token RPC'))
        self.assertEqual(continued['status'],'UNSUPPORTED_TOKEN');self.assertEqual(continued['provider_calls'],0)
        self.assertEqual(jobs.source(uid),original);self.assertEqual(json.loads(original['result'])['calls'],1)
        self.assertEqual(self.progress.admission(uid)['prepared_source'],original)
        self.assertEqual(self.progress.admission(uid)['requests_used'],2)

    def test_rowid_replace_collisions_preserve_two_claims_results_and_shared_counters(self):
        self.capture.capture(self.binding)
        descriptor={**self.descriptor,'scan_id':'second'}
        admission=self.progress.admit('second',descriptor)
        def second_rpc(method,params):
            self.assertEqual(self.progress.admission('second')['requests_used'],1);return transaction()
        capture=RawTransactionCapture(self.store,ReadOnlyRPC('fixture-source',second_rpc))
        self.assertEqual(capture.capture(TransactionBinding('second',admission['descriptor_hash'],MINT,SIGNATURE))['state'],'COMPLETE')
        with closing(self.store.connect()) as c:
            budgets=c.execute('SELECT * FROM ownership_budgets ORDER BY id').fetchall()
            for table in ('raw_transaction_claims','raw_transaction_results'):
                before=c.execute('SELECT rowid,* FROM '+table+' ORDER BY rowid').fetchall()
                for alias in ('rowid','_rowid_','oid'):
                    with self.assertRaises(sqlite3.IntegrityError):
                        c.execute('UPDATE OR REPLACE '+table+' SET '+alias+'=? WHERE scan_id=?',(before[1][0],before[0][1]))
                with self.assertRaises(sqlite3.IntegrityError):
                    c.execute('INSERT OR REPLACE INTO '+table+'(rowid,scan_id,body,body_hash) VALUES(?,?,?,?)',(before[1][0],'third','{}','f'*64))
                self.assertEqual(c.execute('SELECT rowid,* FROM '+table+' ORDER BY rowid').fetchall(),before)
            self.assertEqual(c.execute('SELECT * FROM ownership_budgets ORDER BY id').fetchall(),budgets)

    def test_schema_or_record_corruption_blocks_replay_without_io(self):
        self.capture.capture(self.binding)
        with closing(self.store.connect()) as c:
            c.execute('DROP TRIGGER raw_transaction_claims_update')
            c.execute("CREATE TRIGGER raw_transaction_claims_update BEFORE UPDATE ON raw_transaction_claims BEGIN SELECT 1; END")
        self.assertEqual(self.capture.capture(self.binding)['reason'],'CAPTURE_SCHEMA_MISMATCH')
        self.assertEqual(len(self.calls),1)
        self.setUp();self.capture.capture(self.binding)
        with closing(self.store.connect()) as c:
            c.execute('DROP TRIGGER raw_transaction_claims_update')
            c.execute("UPDATE raw_transaction_claims SET body='{}'")
            c.execute(SCHEMA['raw_transaction_claims_update'])
        self.assertEqual(self.capture.capture(self.binding)['reason'],'CAPTURE_RECORD_CORRUPT')
        self.assertEqual(len(self.calls),1)

    def test_partial_schema_damage_never_recreates_or_recaptures(self):
        for state in ('PENDING','FAILED','COMPLETE','REFUSED'):
            for name in SCHEMA:
                with self.subTest(state=state,missing=name):
                    self.setUp()
                    if state=='PENDING':
                        with patch.object(self.capture,'_finish',side_effect=OSError('publication crash')):
                            self.assertEqual(self.capture.capture(self.binding)['state'],'BLOCKED')
                    if state=='FAILED':
                        def fail_rpc(*args):
                            self.calls.append(args);raise OSError('fixture failure')
                        self.capture=RawTransactionCapture(self.store,ReadOnlyRPC('fixture-source',fail_rpc))
                        self.assertEqual(self.capture.capture(self.binding)['state'],'FAILED')
                    elif state=='REFUSED':
                        for _ in range(18):self.assertTrue(self.progress.reserve('scan'))
                        self.assertEqual(self.capture.capture(self.binding)['state'],'REFUSED')
                    elif state=='COMPLETE':
                        self.assertEqual(self.capture.capture(self.binding)['state'],'COMPLETE')
                    with closing(self.store.connect()) as c:
                        c.execute(('DROP TABLE ' if name in ('raw_transaction_claims','raw_transaction_results') else 'DROP TRIGGER ')+name)
                        def snapshot():
                            tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                            return (c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall(),
                                    [(t,c.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall()) for t in tables])
                        before=snapshot()
                        calls=len(self.calls)
                        for binding,source in ((self.binding,'fixture-source'),
                                               (replace(self.binding,signature=OTHER_SIGNATURE),'fixture-source'),
                                               (self.binding,'new-source')):
                            reopened=RawTransactionCapture(self.store,ReadOnlyRPC(source,lambda *a:self.fail('Damaged journal RPC')))
                            result=reopened.capture(binding)
                            self.assertEqual((result['state'],result['reason'],result['provider_calls']),
                                             ('BLOCKED','CAPTURE_SCHEMA_MISMATCH',0))
                            self.assertEqual(snapshot(),before)
                        self.assertEqual(len(self.calls),calls)

    def test_fresh_schema_initialization_is_atomic_on_failure(self):
        with patch.dict(SCHEMA,{'raw_transaction_invalid':'INVALID SQL'}):
            self.assertEqual(self.capture.capture(self.binding)['state'],'BLOCKED')
        with closing(self.store.connect()) as c:
            self.assertEqual(c.execute("SELECT name FROM sqlite_master WHERE name GLOB 'raw_transaction_*'").fetchall(),[])
        self.assertEqual(self.progress.admission('scan')['requests_used'],0)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.capture.capture(self.binding)['state'],'COMPLETE')

    def test_no_missing_foundation_creates_an_admission_or_budget(self):
        empty=EvidenceStore(self.root/'empty.sqlite')
        with self.assertRaises(ValueError):RawTransactionCapture(empty,ReadOnlyRPC('fixture-source',self.rpc))
        with closing(empty.connect()) as c:self.assertEqual(c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[('pages',)])
