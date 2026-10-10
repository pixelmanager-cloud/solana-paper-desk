"""Synthetic complete acquisition + finalized intake + actual oversize preparation.

No real provider access. Failure, NULL row, reservation and charges are original
production-interface artifacts; no passing proof or gate mocks.
"""
import base64
import hashlib
from contextlib import closing, redirect_stdout
from dataclasses import asdict
import copy
import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from desk import paper_dispatch_preparation_retirement as retire, paper_terminal_reconciliation as terminal
from desk import ownership_acquisition as acquisition, migration_slot_intake as intake
from desk import paper_read_sources as transport, paper_cycle as cycle
from desk.model import canonical,digest
from desk.security import base58
from desk.history_progress import HistoryProgress
# Byte-identical historical producer from PR188 7d8af35cbfd76e71aca3c004ab2f684a38f16444,
# tools/history_first_paper_entry.py. PR189's prospective early NO_ENTRY path must
# not replace the original v1 producer when reconstructing the uncaptured case.
# Imported dependencies/validators remain the current code under test.
from tests.fixtures import history_first_paper_entry_pre189 as entry
from tests import test_paper_observation_collector as fixtures
from tests.test_graduation_witness import fixture as migration_fixture
from tests.helpers import config
from types import SimpleNamespace
from desk import provider_pacing as pace
import time
from tests.test_paper_read_sources import Response

class DispatchPreparationRetirementTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(hashlib.sha256(Path(entry.__file__).read_bytes()).hexdigest(),
            '2cce07d1a2f86c129abc869d29fc152190f283b82c2db11389413e53fb129ef5')
        fixture=fixtures.PaperObservationCollectorTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.at=int(time.time());root=Path(fixture.tmp.name).resolve()
        raw,mint,pool=migration_fixture();raw["transaction"]["signatures"]=[base58(bytes([9])*64)]
        cfg=config()|{'paper_signal_policy_version':3,'paper_quote_execution_version':1,'paper_usd_valuation_version':1}
        ledger=root/'ledger.sqlite';cycle.initialize(ledger,cfg)
        config_path=root/'config.json';config_path.write_text(canonical(cfg))
        pacer=root/'pacing.sqlite';pace.initialize(pacer)
        from desk import kraken_pacing_migration as migration
        migration_policy=root/'kraken-policy.json';migration_policy.write_text(canonical({'version':1,'pins':[]}))
        p=patch.object(migration,'POLICY',migration_policy);p.start();self.addCleanup(p.stop)
        pin=migration.review_plan(pacer);migration_policy.write_text(canonical({'version':1,'pins':[pin]}));migration.migrate(pacer)
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
        original=entry.PaperHistorySource.__call__
        calls=[0]
        def historical_call(source,method,params):
            calls[0]+=1
            if calls[0]==6:
                # Reservation already charged; no transport attempt is fabricated.
                raise transport.PaperReadError('DEADLINE_EXCEEDED')
            return original(source,method,params)
        class Pages:
            def open(self,request,*,timeout):
                q=json.loads(request.data);rows=[]
                for n in range(50):
                    raw=copy.deepcopy(outer.f.raw)
                    raw['transaction']['signatures']=[base58(bytes([calls[0]])*32+n.to_bytes(32,'big'))]
                    raw['blockTime']=q['params'][1]['filters']['blockTime']['gte']+calls[0]
                    rows.append(raw)
                return Response(canonical({'jsonrpc':'2.0','id':transport.RPC_ID,'result':{'data':rows,'paginationToken':'page-'+str(calls[0])}}).encode())
        with patch.object(entry.PaperHistorySource,'__call__',historical_call),patch.object(entry.cli,'_credentials'),patch.object(transport,'build_opener',return_value=Pages()),patch.dict('os.environ',{'HELIUS_API_KEY':'SYNTHETIC_TEST_ONLY'}):
            with self.assertRaises(ValueError):entry.execute(self.f.config,self.f.f.jobs.path,self.f.f.progress.store.path,self.f.ledger,path,live=True,systemd_credentials=True)
        with self.f.f.progress.store.connect() as c:
            self.pass_id,self.intent_hash=c.execute('SELECT id,intent_hash FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()
        original_intent=self.f.f.progress.store.load(self.intent_hash)
        self.assertEqual(set(original_intent),{'kind','config_hash','target','admission'})
        self.assertEqual(original_intent['kind'],'history_first_paper_preparation_v1')
        self.assertEqual(original_intent['admission']['requests_used'],6)
        self.assertEqual(calls[0],6)
        self.assertEqual(self.f.f.progress.admission(self.scan)['requests_used'],12)
        from tools import paper_entry_dispatcher as dispatcher
        self.journal=self.f.root/'dispatch.sqlite';self.dispatch_id='a'*32
        from discovery import continuous
        discovery=root/'continuous.sqlite';continuous.initialize(discovery)
        context=dispatcher.plan(config=self.f.config,research_db=self.f.f.jobs.path,evidence_db=self.f.f.progress.store.path,ledger_db=self.f.ledger,discovery_db=discovery,pacing_db=self.f.pacer,journal=self.journal,taker=self.f.f.taker,amount_raw=100000000,pool_fee_bps='25')
        intent={'version':1,'context_hash':digest(context),'at':self.f.f.at,'hint':{'seq':1,'payload_hash':'a'*64,'raw_hash':'b'*64,'received_at':self.f.f.at-600,'mint':self.f.mint,'pool':self.f.pool,'signature':self.f.raw['transaction']['signatures'][0],'slot':self.f.raw['slot']}}
        with closing(sqlite3.connect(self.journal)) as c:
            for sql in (*dispatcher.SCHEMAS.values(),*dispatcher._guards().values()):c.execute(sql)
            c.execute('INSERT INTO context VALUES(1,?,?)',(canonical(context),digest(context)))
            c.execute('INSERT INTO intents VALUES(?,?,?,?,?)',(self.dispatch_id,self.f.mint,intent['hint']['signature'],canonical(intent),digest(intent)));c.commit()
        self.journal.chmod(0o600)
        self.stopped=self.f.f.progress.store.save({'kind':'dispatcher_stopped_review_v1','dispatch_id':self.dispatch_id,'dispatch_intent_hash':digest(intent),'producer_source_hash':self.source,'exit_status':2,'service_active':False,'timer_active':False,'observed_at':self.f.f.at+60})
        self.args=(self.f.f.jobs.path,self.f.f.progress.store.path,self.f.ledger,self.f.cfg)
        self.kw=dict(pass_id=self.pass_id,dispatch_id=self.dispatch_id,journal_path=self.journal,stopped_witness_hash=self.stopped,pacing_db=self.f.pacer,producer_source_hash=self.source,ledger_backup=self.backup)
        self.policy=self.f.root/'retirement.json';self.policy.write_text(canonical({'version':1,'retirements':[]}))
        p=patch.object(retire,'POLICY',self.policy);p.start();self.addCleanup(p.stop)
        self.pin=retire.review_plan(*self.args,**self.kw)
        self.policy.write_text(canonical({'version':1,'retirements':[self.pin]}))
        self.expected=context
    def gate(self,scans=()):return terminal.gate(self.f.f.progress.store,self.f.f.jobs.path,scans)
    def apply(self):return retire.reconcile(*self.args,**self.kw)
    def sql(self,sql,args=()):
        with self.f.f.progress.store.connect() as c:c.execute(sql,args)
    def replace_page(self,key,change):
        value=self.f.f.progress.store.load(key);change(value);return self.f.f.progress.store.save(value)
    def test_exact_unknown_reservation_plan_preserves_null_charges_and_journal(self):
        self.assertEqual(self.pin['after'],12)
        self.assertEqual(self.pin['failure'],'RESERVATION_OUTCOME_UNCAPTURED')
        self.assertEqual([x[0] for x in retire._attempts(self.f.f.progress.store,self.scan) if x[0]>=7],[7,8,9,10,11])
        before=self.journal.read_bytes();ledger=self.f.ledger.read_bytes();admission=self.f.f.progress.admission(self.scan)
        result=self.apply();self.assertEqual(result['status'],'RECORDED')
        self.assertEqual(self.journal.read_bytes(),before);self.assertEqual(self.f.ledger.read_bytes(),ledger)
        self.assertEqual(self.f.f.progress.admission(self.scan),admission)

    def test_gate_and_journal_validation_do_not_fabricate_result(self):
        self.assertEqual(self.gate(),'OBSERVATION_RECOVERY_REQUIRED')
        self.apply();self.assertIsNone(self.gate());self.assertEqual(self.gate((self.scan,)),'REJECTED_SCAN_RETIRED')
        with sqlite3.connect(self.journal) as c:self.assertEqual(c.execute('SELECT count(*) FROM results').fetchone(),(0,))

    def test_empty_policy_refuses_apply(self):
        self.policy.write_text(canonical({'version':1,'retirements':[]}))
        with self.assertRaises(ValueError):self.apply()
    def test_idempotence_and_immutable_guards(self):
        self.apply();self.assertEqual(self.apply()['status'],'ALREADY_RECORDED')
        for sql in ('DELETE FROM '+retire.TABLE,'UPDATE '+retire.TABLE+' SET payload=payload'):
            with self.assertRaises(sqlite3.Error):self.sql(sql)
    def test_unrelated_null_pass_refuses_apply(self):
        self.sql('INSERT INTO paper_observation_passes VALUES(?,?,NULL)',('b'*32,self.intent_hash))
        with self.assertRaisesRegex(ValueError,'Unrelated pending'):self.apply()
    def test_attempt_twelve_cannot_be_invented(self):
        record=retire._attempts(self.f.f.progress.store,self.scan)[-1][2]
        record={**record,'requests_used':12};self.f.f.progress.store.save(record)
        with self.assertRaisesRegex(ValueError,'charge bound'):self.apply()
    def test_missing_successful_attempt_refuses(self):
        key=[r[1] for r in retire._attempts(self.f.f.progress.store,self.scan) if r[0]==9][0]
        self.sql('DELETE FROM pages WHERE hash=?',(key,))
        with self.assertRaises(ValueError):self.apply()
    def test_mutated_counter_refuses(self):
        self.sql('UPDATE ownership_budgets SET used=13 WHERE id=?',(self.scan,))
        with self.assertRaisesRegex(ValueError,'12/18'):self.apply()
    def test_mutated_history_reservation_refuses(self):
        self.sql('UPDATE ownership_history SET attempts=5 WHERE id=?',(self.pin['history_id'],))
        with self.assertRaises(ValueError):self.apply()
    def test_completed_prep_artifact_refuses(self):
        self.f.f.progress.store.save({'kind':'history_first_paper_preparation_outcome_v1','intent_hash':self.intent_hash})
        with self.assertRaisesRegex(ValueError,'Conflicting completed'):self.apply()
    def test_ledger_changed_refuses(self):
        with sqlite3.connect(self.f.ledger) as c:c.execute("UPDATE state SET payload='{}'")
        with self.assertRaises(ValueError):self.apply()
    def test_stopped_witness_requires_ended_owner(self):
        witness=self.f.f.progress.store.load(self.stopped);witness['service_active']=True
        self.kw['stopped_witness_hash']=self.f.f.progress.store.save(witness)
        with self.assertRaisesRegex(ValueError,'ended-invocation'):retire.review_plan(*self.args,**self.kw)
    def test_rollback_no_partial_schema(self):
        with patch.object(retire,'rows',side_effect=[[],[],ValueError('fault')]):
            with self.assertRaises(ValueError):self.apply()
        with self.f.f.progress.store.connect() as c:self.assertIsNone(c.execute('SELECT 1 FROM sqlite_master WHERE name=?',(retire.TABLE,)).fetchone())
    def test_real_first_receipt_then_continuation_then_retirement_with_distinct_backup(self):
        from tools import paper_entry_dispatcher as dispatcher
        from desk import runtime_continuation as continuation
        native='f'*64;producer='e'*64
        context={k:str(p) for k,p in zip(('research_db','evidence_db','ledger_db'),self.args[:3])}
        with sqlite3.connect(self.f.ledger) as c:c.execute("UPDATE metadata SET value=? WHERE key='implementation_hash'",(native,))
        runtime_policy=self.f.root/'runtime-policy.json'
        runtime_policy.write_text(canonical({'version':1,'transitions':[{'predecessor':native,'successor':producer,'config_hash':digest(self.f.cfg),'context':context}]}))
        cont_policy=self.f.root/'continuation-policy.json'
        with patch.object(retire.runtime,'POLICY',runtime_policy):
            with patch.object(retire.runtime,'implementation_hash',return_value=producer):
                retire.runtime.transition(*self.args,predecessor=native,successor=producer)
            with sqlite3.connect(self.f.ledger) as c:first,first_hash=continuation._first(c)
            self.backup.unlink()
            with closing(sqlite3.connect(self.f.ledger)) as old,closing(sqlite3.connect(self.backup)) as saved:old.backup(saved)
            with sqlite3.connect(self.journal) as c:
                for name in dispatcher._guards():c.execute('DROP TRIGGER '+name)
                ctx=copy.deepcopy(self.expected);ctx['source_hash']=producer
                raw=c.execute('SELECT payload FROM intents').fetchone()[0];intent=json.loads(raw);intent['context_hash']=digest(ctx)
                c.execute('UPDATE context SET payload=?,hash=?',(canonical(ctx),digest(ctx)))
                c.execute('UPDATE intents SET payload=?,hash=?',(canonical(intent),digest(intent)))
                for sql in dispatcher._guards().values():c.execute(sql)
            witness=self.f.f.progress.store.load(self.stopped);witness.update(producer_source_hash=producer,dispatch_intent_hash=digest(intent))
            self.kw.update(producer_source_hash=producer,stopped_witness_hash=self.f.f.progress.store.save(witness))
            with self.assertRaises(ValueError):retire.review_plan(*self.args,**self.kw) # current runtime required first
            with sqlite3.connect(self.backup) as c:
                with self.assertRaises(ValueError):terminal._ledger(c,self.f.cfg,initial=True,historical_source=producer) # backup is not active canonical ledger
            cont_policy.write_text(canonical({'version':1,'continuations':[{'first_receipt_hash':first_hash,'predecessor':producer,'successor':self.source,'config_hash':digest(self.f.cfg),'context':context}]}))
            with patch.object(continuation,'POLICY',cont_policy):
                continuation.continue_runtime(*self.args,first_receipt_hash=first_hash,predecessor=producer,successor=self.source)
                with sqlite3.connect(self.f.ledger) as c:
                    with self.assertRaises(ValueError):terminal._ledger(c,self.f.cfg,initial=True,historical_source=producer) # continued runtime cannot masquerade as producer
                pin=retire.review_plan(*self.args,**self.kw)
                self.assertEqual(pin['first_receipt_hash'],first_hash)
                self.policy.write_text(canonical({'version':1,'retirements':[pin]}));self.apply()
                self.assertIsNone(self.gate());self.assertEqual(self.gate((self.scan,)),'REJECTED_SCAN_RETIRED')
                with sqlite3.connect(self.journal) as c:
                    dispatcher._validate(c,pin['successor_context'])
                    self.assertEqual(c.execute('SELECT count(*) FROM intents').fetchone(),(1,));self.assertEqual(c.execute('SELECT count(*) FROM results').fetchone(),(0,))
                    bad=copy.deepcopy(pin['successor_context']);bad['amount_raw']+=1
                    with self.assertRaises(ValueError):dispatcher._validate(c,bad)
                with sqlite3.connect(self.f.ledger) as c:self.assertEqual(continuation._first(c),(first,first_hash))

    def test_oversized_attempt_hash_refused_before_load(self):
        self.sql('INSERT INTO pages VALUES(?,?,?)',('x'*(20*1024*1024),b'x',1))
        with patch.object(terminal,'_load',side_effect=AssertionError('must not load')):
            with self.assertRaisesRegex(ValueError,'scalar bound'):retire._attempts(self.f.f.progress.store,self.scan)

    def test_oversized_history_status_refused(self):
        self.sql('UPDATE ownership_history SET status=? WHERE id=?',('x'*(20*1024*1024),self.pin['history_id']))
        with self.assertRaisesRegex(ValueError,'scalar bound'):self.apply()

    def test_no_implicit_lineage_adoption(self):
        from tools import paper_entry_dispatcher as dispatcher
        successor=copy.deepcopy(self.expected);successor['source_hash']='e'*64
        with sqlite3.connect(self.journal) as c:
            with self.assertRaises(ValueError):dispatcher._validate(c,successor)
        self.assertEqual(self.f.f.progress.admission(self.scan)['requests_used'],12)

    def test_pending_pacer_blocks_apply(self):
        with sqlite3.connect(self.f.pacer) as c:c.execute("UPDATE state SET pending='unresolved' WHERE provider='helius'")
        with self.assertRaises(ValueError):self.apply()

    def test_retired_journal_prefix_cannot_change_rowid(self):
        self.apply()
        from tools import paper_entry_dispatcher as dispatcher
        with sqlite3.connect(self.journal) as c:
            for name in dispatcher._guards():c.execute('DROP TRIGGER '+name)
            c.execute('UPDATE intents SET rowid=44')
            for sql in dispatcher._guards().values():c.execute(sql)
        with self.assertRaisesRegex(ValueError,'Journal reviewed binding'):self.gate()

    def test_intake_http403_or_missing_body_cannot_be_retired(self):
        for charge in (5,6):
            with self.subTest(charge=charge):
                key=next(key for used,key,_ in retire._attempts(self.f.f.progress.store,self.scan) if used==charge)
                original=self.f.f.progress.store.load(key);changed={**original,'http_status':403,'failure_code':'HTTP_STATUS_403','response_bytes_base64':None}
                self.sql('DELETE FROM pages WHERE hash=?',(key,));new=self.f.f.progress.store.save(changed)
                try:
                    with self.assertRaisesRegex(ValueError,'successful intake'):retire.review_plan(*self.args,**self.kw)
                finally:
                    self.sql('DELETE FROM pages WHERE hash=?',(new,));self.f.f.progress.store.save(original)

    def test_intake_request_binding_must_match_retained_slot(self):
        key=next(key for used,key,_ in retire._attempts(self.f.f.progress.store,self.scan) if used==5)
        original=self.f.f.progress.store.load(key);changed=copy.deepcopy(original)
        changed['params'][1]['filters']['slot']['lt']+=1
        changed['request_bytes_base64']=base64.b64encode(canonical({'jsonrpc':'2.0','id':transport.RPC_ID,'method':changed['method'],'params':changed['params']}).encode()).decode()
        self.sql('DELETE FROM pages WHERE hash=?',(key,));self.f.f.progress.store.save(changed)
        with self.assertRaisesRegex(ValueError,'successful intake'):retire.review_plan(*self.args,**self.kw)
