"""Synthetic complete acquisition + finalized intake + actual oversize preparation.

No real provider access. Failure, NULL row, reservation and charges are original
production-interface artifacts; no passing proof or gate mocks.
"""
import base64
from contextlib import closing, redirect_stdout
from dataclasses import asdict
import copy
import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from desk import paper_preparation_retirement as retire, paper_terminal_reconciliation as terminal
from desk import ownership_acquisition as acquisition, migration_slot_intake as intake
from desk import paper_read_sources as transport, paper_cycle as cycle
from desk.model import canonical,digest
from desk.security import base58
from desk.history_progress import HistoryProgress
# Immutable pre189 producer from PR188 1a4e76c3f761ed80bd016a8384179e92825840e5.
# SHA256 2cce07d1a2f86c129abc869d29fc152190f283b82c2db11389413e53fb129ef5.
from tests.fixtures import history_first_paper_entry_pre189 as entry
from tests import test_paper_observation_collector as fixtures
from tests.test_graduation_witness import fixture as migration_fixture
from tests.helpers import config
from types import SimpleNamespace
from desk import provider_pacing as pace
import time
from tests.test_paper_read_sources import Response

class PreparationRetirementTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.PaperObservationCollectorTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.at=int(time.time());root=Path(fixture.tmp.name).resolve()
        raw,mint,pool=migration_fixture();raw["transaction"]["signatures"]=[base58(bytes([9])*64)]
        cfg=config()|{'paper_signal_policy_version':3,'paper_quote_execution_version':1}
        ledger=root/'ledger.sqlite';cycle.initialize(ledger,cfg)
        config_path=root/'config.json';config_path.write_text(canonical(cfg))
        pacer=root/'pacing.sqlite';pace.initialize(pacer)
        clock=[float(fixture.at)]
        def configured(**kw):return pace.Pacer(pacer,clock=lambda:clock[0],monotonic=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n),**kw)
        for p in (patch.object(pace,'configured',side_effect=configured),patch.dict('os.environ',{pace.ENV:str(pacer)})):
            p.start();self.addCleanup(p.stop)
        self.f=SimpleNamespace(f=fixture,root=root,raw=raw,mint=mint,pool=pool,ledger=ledger,cfg=cfg,config=config_path,pacer=pacer)
        self.source=retire.runtime.implementation_hash();self.seed=0
        def rpc(method,params):
            if method=='getAccountInfo':return {'value':copy.deepcopy(self.f.f.protocol.rpc('getMultipleAccounts',[])['value'][6])}
            if method=='getSlot':return 120
            self.assertEqual(method,'getTransactionsForAddress');self.seed+=1
            return {'data':[],'paginationToken':'next-seed' if self.seed==1 else None}
        report=acquisition.acquire(self.f.f.jobs.path,self.f.f.progress.store.path,rpc,mint=self.f.mint)
        self.scan=report['scan_id'];self.assertEqual(self.f.f.progress.admission(self.scan)['requests_used'],4)
        self.wires=0;outer=self
        class IntakeOpener:
            def open(self,request,*,timeout):
                outer.wires+=1
                data={'data':[outer.f.raw] if outer.wires==1 else [],'paginationToken':'next-slot' if outer.wires==1 else None}
                return Response(canonical({'jsonrpc':'2.0','id':transport.RPC_ID,'result':data}).encode())
        with patch.object(transport,'build_opener',return_value=IntakeOpener()),patch.dict('os.environ',{'HELIUS_API_KEY':'SYNTHETIC_TEST_ONLY'}):
            migration=intake.intake(self.f.f.jobs.path,self.f.f.progress.store.path,scan_id=self.scan,mint=self.f.mint,pool=self.f.pool,signature=self.f.raw['transaction']['signatures'][0],slot=self.f.raw['slot'],provenance='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE')
        self.assertEqual(migration['status'],'RETAINED_MIGRATION_WITNESS',migration)
        self.assertEqual(self.f.f.progress.admission(self.scan)['requests_used'],6)
        self.backup=self.f.root/'before.sqlite'
        with closing(sqlite3.connect(self.f.ledger)) as old,closing(sqlite3.connect(self.backup)) as new:old.backup(new)
        target={'scan_id':self.scan,'mint':self.f.mint,'pool':self.f.pool,'taker':self.f.f.taker,'amount_raw':100000000,'pool_fee_bps':'25','provenance':'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE','known_hazards':[],'graduation_refs':migration['request_evidence_refs']}
        path=self.f.root/'targets.json';path.write_text(canonical({'position_targets':[],'candidates':[target],'usd_evidence_refs':[]}))
        class Oversize:
            def open(self,request,*,timeout):
                return Response(b'UNREAD_OVERSIZED_BODY',[('Content-Length',str(transport.MAX_RESPONSE_BYTES+1))])
        # Generate the historical limit-100 request through real reservation and
        # transport interfaces, even when fresh producer defaults become 50.
        from desk import paper_history_source
        with patch.object(paper_history_source,'PAPER_HISTORY_PAGE_SIZE',100,create=True),patch.object(entry.cli,'_credentials'),patch.object(transport,'build_opener',return_value=Oversize()),patch.dict('os.environ',{'HELIUS_API_KEY':'SYNTHETIC_TEST_ONLY'}):
            with self.assertRaises(ValueError):entry.execute(self.f.config,self.f.f.jobs.path,self.f.f.progress.store.path,self.f.ledger,path,live=True,systemd_credentials=True)
        with self.f.f.progress.store.connect() as c:
            self.pass_id,self.intent_hash=c.execute('SELECT id,intent_hash FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()
        self.failed=[x for x in retire._attempts(self.f.f.progress.store,self.scan) if x[0]==7][0][1]
        self.args=(self.f.f.jobs.path,self.f.f.progress.store.path,self.f.ledger,self.f.cfg)
        self.kw=dict(pass_id=self.pass_id,failed_attempt_hash=self.failed,pacing_db=self.f.pacer,producer_source_hash=self.source,ledger_backup=self.backup)
        self.policy=self.f.root/'retirement.json';self.policy.write_text(canonical({'version':1,'retirements':[]}))
        p=patch.object(retire,'POLICY',self.policy);p.start();self.addCleanup(p.stop)
        self.pin=retire.review_plan(*self.args,**self.kw)
        self.policy.write_text(canonical({'version':1,'retirements':[self.pin]}))
    def gate(self,scans=()):return terminal.gate(self.f.f.progress.store,self.f.f.jobs.path,scans)
    def apply(self):return retire.reconcile(*self.args,**self.kw)
    def sql(self,sql,args=()):
        with self.f.f.progress.store.connect() as c:c.execute(sql,args)
    def replace_page(self,key,change):
        value=self.f.f.progress.store.load(key);change(value);return self.f.f.progress.store.save(value)
    def test_actual_failure_plan_append_originals_and_no_retry_gate(self):
        before=self.f.f.progress.admission(self.scan);ledger=self.f.ledger.read_bytes()
        self.assertEqual(self.gate(),'OBSERVATION_RECOVERY_REQUIRED')
        result=self.apply();self.assertEqual(result['status'],'RECORDED')
        self.assertIsNone(self.gate());self.assertEqual(self.gate((self.scan,)),'REJECTED_SCAN_RETIRED')
        self.assertEqual(self.apply()['status'],'ALREADY_RECORDED')
        self.assertEqual(before,self.f.f.progress.admission(self.scan));self.assertEqual(ledger,self.f.ledger.read_bytes())
        with self.f.f.progress.store.connect() as c:self.assertEqual(c.execute('SELECT intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?',(self.pass_id,)).fetchone(),(self.intent_hash,None))
    def test_unrelated_null_remains_blocked(self):
        self.apply();self.sql('INSERT INTO paper_observation_passes VALUES(?,?,NULL)',('a'*32,'b'*64))
        self.assertEqual(self.gate(),'OBSERVATION_RECOVERY_REQUIRED')
    def test_no_review_pin_no_apply(self):
        self.policy.write_text(canonical({'version':1,'retirements':[]}))
        with self.assertRaises(ValueError):self.apply()
        self.assertEqual(self.gate(),'OBSERVATION_RECOVERY_REQUIRED')
    def test_plan_no_schema_or_ledger_writes(self):
        before={p:p.read_bytes() for p in (self.f.ledger,self.f.f.jobs.path,self.f.f.progress.store.path,self.f.pacer)}
        self.assertEqual(retire.review_plan(*self.args,**self.kw),self.pin)
        self.assertEqual(before,{p:p.read_bytes() for p in before})
    def test_additional_charge_refused(self):
        self.assertTrue(self.f.f.progress.reserve(self.scan))
        with self.assertRaises(ValueError):self.apply()
    def test_duplicate_charged_attempt_refused(self):
        self.replace_page(self.failed,lambda v:v.update(request_bytes_base64=v['request_bytes_base64']+'AA'))
        with self.assertRaises(ValueError):self.apply()
    def test_query_rowid_changed_refused(self):
        self.sql('UPDATE ownership_history SET rowid=rowid+100 WHERE id=?',(self.pin['history_id'],))
        with self.assertRaises(ValueError):self.apply()
    def test_reservation_status_changed_refused(self):
        self.sql("UPDATE ownership_history SET status='DONE' WHERE id=?",(self.pin['history_id'],))
        with self.assertRaises(ValueError):self.apply()
    def test_query_scope_changed_refused(self):
        self.sql("UPDATE ownership_history SET query='{}' WHERE id=?",(self.pin['history_id'],))
        with self.assertRaises(ValueError):self.apply()
    def test_no_transport_body_fabrication(self):
        altered=self.replace_page(self.failed,lambda v:v.update(response_bytes_base64=base64.b64encode(b'{}').decode()))
        self.kw['failed_attempt_hash']=altered
        with self.assertRaises(ValueError):retire.review_plan(*self.args,**self.kw)
    def test_ledger_original_rowid_change_refused(self):
        with closing(sqlite3.connect(self.f.ledger)) as c:c.execute('UPDATE metadata SET rowid=rowid+100');c.commit()
        with self.assertRaises(ValueError):self.apply()
    def test_ledger_raw_or_health_changes_refused(self):
        with closing(sqlite3.connect(self.f.ledger)) as c:c.execute("INSERT INTO health(ts,code,details) VALUES(1,'x','x')");c.commit()
        with self.assertRaises(ValueError):self.apply()
    def test_receipt_guard_loss_blocks_gate(self):
        self.apply();self.sql('DROP TRIGGER '+retire.TABLE+'_update')
        with self.assertRaises(ValueError):self.gate()
    def test_append_immutable_delete_update_replace(self):
        self.apply()
        for sql in ('DELETE FROM '+retire.TABLE,"UPDATE "+retire.TABLE+" SET payload='{}'",'INSERT OR REPLACE INTO '+retire.TABLE+' SELECT * FROM '+retire.TABLE):
            with self.subTest(sql=sql),self.assertRaises(sqlite3.IntegrityError):self.sql(sql)
    def test_policy_removal_invalidates_saved_receipt(self):
        self.apply();self.policy.write_text(canonical({'version':1,'retirements':[]}))
        with self.assertRaises(ValueError):self.gate()
    def test_current_ledger_not_original_backup_refused(self):
        self.kw['ledger_backup']=self.f.ledger
        with self.assertRaises(ValueError):retire.review_plan(*self.args,**self.kw)
    def test_missing_failed_record_refused(self):
        self.sql('DELETE FROM pages WHERE hash=?',(self.failed,))
        with self.assertRaises(ValueError):self.apply()
    def test_inventory_bound_before_page_load(self):
        with patch.object(retire,'MAX_INVENTORY_BYTES',1),patch.object(terminal,'_load',side_effect=AssertionError('unbounded load')):
            with self.assertRaisesRegex(ValueError,'inventory bound'):retire._attempts(self.f.f.progress.store,self.scan)

    def test_future_valid_ledger_control_does_not_reopen_retired_scan(self):
        self.apply()
        from desk.ledger import Ledger
        from desk.engine import transition,initial_state
        ledger=Ledger(self.f.ledger,must_exist=True)
        try:ledger.apply({'schema_version':1,'event_id':'pause-after-retirement','ts':int(time.time()),'kind':'control','actor':'operator','command':'PAUSE_ENTRY'},self.f.cfg,transition,initial_state)
        finally:ledger.close()
        self.assertIsNone(self.gate());self.assertEqual(self.gate((self.scan,)),'REJECTED_SCAN_RETIRED')
    def test_completed_preparation_artifact_contradiction_refused(self):
        self.f.f.progress.store.save({'kind':'history_first_paper_preparation_outcome_v1','intent_hash':self.intent_hash,'history_as_of':1,'admission':{}})
        with self.assertRaisesRegex(ValueError,'Conflicting completed'):self.apply()
    def test_old_source_plan_append_then_explicit_runtime_transition(self):
        old='f'*64
        for path in (self.f.ledger,self.backup):
            with closing(sqlite3.connect(path)) as c:c.execute("UPDATE metadata SET value=? WHERE key='implementation_hash'",(old,));c.commit()
        self.kw['producer_source_hash']=old
        pin=retire.review_plan(*self.args,**self.kw)
        self.assertEqual(pin['producer_source_hash'],old);self.assertEqual(pin['source_hash'],self.source)
        self.policy.write_text(canonical({'version':1,'retirements':[pin]}))
        self.apply()
        with self.assertRaises(ValueError):self.gate() # New runtime is NOT claimed active.
        policy=self.f.root/'runtime.json'
        ctx={k:str(p) for k,p in zip(('research_db','evidence_db','ledger_db'),self.args[:3])}
        policy.write_text(canonical({'version':1,'transitions':[{'predecessor':old,'successor':self.source,'config_hash':digest(self.f.cfg),'context':ctx}]}))
        with patch.object(retire.runtime,'POLICY',policy):
            result=retire.runtime.transition(*self.args,predecessor=old,successor=self.source)
            self.assertEqual(result['status'],'RECORDED');self.assertIsNone(self.gate())
            self.assertEqual(self.gate((self.scan,)),'REJECTED_SCAN_RETIRED')
    def test_apply_rollback_leaves_no_partial_schema(self):
        with patch.object(retire,'rows',side_effect=[[],ValueError('publication fault')]):
            with self.assertRaisesRegex(ValueError,'publication fault'):self.apply()
        with self.f.f.progress.store.connect() as c:
            self.assertIsNone(c.execute('SELECT 1 FROM sqlite_master WHERE name=?',(retire.TABLE,)).fetchone())
            self.assertIsNone(c.execute('SELECT outcome_hash FROM paper_observation_passes WHERE id=?',(self.pass_id,)).fetchone()[0])
        self.assertEqual(self.f.f.progress.admission(self.scan)['requests_used'],7)
    def test_cli_defaults_to_readonly_proposal_and_requires_explicit_apply(self):
        args=[]
        for name,value in dict(research_db=self.args[0],evidence_db=self.args[1],ledger_db=self.args[2],config=self.f.config,**self.kw).items():args+=['--'+name.replace('_','-'),str(value)]
        with redirect_stdout(io.StringIO()) as out:self.assertEqual(retire.main(args),0)
        self.assertEqual(json.loads(out.getvalue()),self.pin)
        with self.f.f.progress.store.connect() as c:self.assertIsNone(c.execute('SELECT 1 FROM sqlite_master WHERE name=?',(retire.TABLE,)).fetchone())
        self.policy.write_text(canonical({'version':1,'retirements':[]}))
        with redirect_stdout(io.StringIO()):self.assertEqual(retire.main(args+['--apply']),2)
    def test_failure_type_and_request_mutations_cannot_be_relabelled(self):
        original=self.f.f.progress.store.load(self.failed)
        changes=[{'failure_code':'TRANSPORT_ERROR'},{'http_status':403},{'observed_at':1},{'method':'getSlot'},{'source_id':'caller-source'},{'request_bytes_base64':base64.b64encode(b'{}').decode()}]
        for delta in changes:
            with self.subTest(delta=delta):
                changed=original|delta;key=self.f.f.progress.store.save(changed)
                self.sql('DELETE FROM pages WHERE hash=?',(self.failed,))
                try:
                    with self.assertRaises(ValueError):retire.review_plan(*self.args,**(self.kw|{'failed_attempt_hash':key}))
                finally:self.sql('DELETE FROM pages WHERE hash=?',(key,));self.f.f.progress.store.save(original)

    def test_oversized_page_hash_refused_before_any_proof_load(self):
        self.sql('UPDATE pages SET hash=? WHERE hash=?',('x'*(20*1024*1024),self.failed))
        with patch.object(terminal,'_load',side_effect=AssertionError('must preflight before proof loads')) as load:
            with self.assertRaisesRegex(ValueError,'inventory scalar bound'):
                retire._attempts(self.f.f.progress.store,self.scan)
            load.assert_not_called()

    def test_oversized_history_status_refused_before_row_materialization(self):
        self.sql('UPDATE ownership_history SET status=? WHERE budget=?',('x'*(20*1024*1024),self.scan))
        with self.assertRaisesRegex(ValueError,'history scalar bound'):
            retire._histories(self.f.f.progress.store,self.scan)

    def test_all_history_materialized_fields_have_scalar_preflight(self):
        for field,value in (('id','x'*65),('query',sqlite3.Binary(b'invalid')),('coverage','x'*1048577),('status',sqlite3.Binary(b'invalid')),('attempts','x'*1048577)):
            with self.subTest(field=field):
                with self.f.f.progress.store.connect() as c:
                    saved=c.execute('SELECT rowid,'+field+' FROM ownership_history WHERE budget=? LIMIT 1',(self.scan,)).fetchall()
                    c.execute('UPDATE ownership_history SET '+field+'=? WHERE rowid=?',(value,saved[0][0]))
                try:
                    with self.assertRaisesRegex(ValueError,'history scalar bound'):retire._histories(self.f.f.progress.store,self.scan)
                finally:
                    with self.f.f.progress.store.connect() as c:
                        for rowid,old in saved:c.execute('UPDATE ownership_history SET '+field+'=? WHERE rowid=?',(old,rowid))

    def test_oversized_schema_sql_refused_before_inventory_fetch(self):
        with self.f.f.progress.store.connect() as c:
            c.execute('PRAGMA writable_schema=ON')
            original=c.execute("SELECT sql FROM sqlite_master WHERE name='ownership_history'").fetchone()[0]
            c.execute("UPDATE sqlite_master SET sql=? WHERE name='ownership_history'",(original+' /*'+'x'*(2*1024*1024)+'*/',))
        with self.assertRaisesRegex(ValueError,'schema scalar bound'):
            retire._histories(self.f.f.progress.store,self.scan)
