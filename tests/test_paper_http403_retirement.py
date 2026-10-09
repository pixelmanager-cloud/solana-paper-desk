"""Synthetic actual transport HTTPError, never a production/provider replay."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from desk import paper_http403_retirement as r, paper_terminal_reconciliation as terminal
from desk import provider_pacing, paper_cycle as cycle
from desk.model import canonical,digest
from tests import test_paper_cycle as fixtures
from tests.test_paper_read_sources import Response


def dump(path):
    with sqlite3.connect(path) as c:return list(c.iterdump())


class HTTP403RetirementTests(unittest.TestCase):
    def setUp(self):
        self.h=fixtures.PaperCycleTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.f=self.h.f;self.store=self.f.progress.store;self.cfg=self.h.cfg
        self.policy=Path(self.f.tmp.name)/'http403-policy.json'
        self.policy.write_text(canonical({'version':1,'associations':[]}))
        p=patch.object(r,'POLICY',self.policy);p.start();self.addCleanup(p.stop)
        self.pacing=Path(self.f.tmp.name)/'http403-pacing.sqlite';provider_pacing.initialize(self.pacing)
        self.h.http_calls=[];self.h.sell_output=10_000_000
        # Explicit completed synthetic history preparation before cycle intent.
        from desk.ownership_acquisition import _Setup
        _Setup(self.store,self.f.jobs.descriptor(self.h.target.scan_id),self.f.progress.admission(self.h.target.scan_id))
        for _ in range(7):self.assertTrue(self.f.progress.reserve(self.h.target.scan_id))
        source=self.f.jobs.source(self.h.target.scan_id)
        report={'mint':self.h.target.mint,'eligible_for_trading':False,'calls':7};report['report_hash']=digest(report)
        source.update(status='COMPLETE',result=canonical(report))
        admission=self.f.progress.admission(self.h.target.scan_id)
        self.f.progress.prepare_source(self.h.target.scan_id,admission['descriptor_hash'],source)
        self.f.progress.seal_source(self.h.target.scan_id,admission['descriptor_hash'],digest(source))
        with self.f.jobs.connect() as c:c.execute("UPDATE scans SET status='COMPLETE',result=? WHERE id=?",(source['result'],self.h.target.scan_id))
        key=self.f.progress.create(self.h.target.scan_id,self.h.target.pool,self.f.at-300,self.f.at+1)
        self.assertEqual(self.f.progress.advance(key,lambda *_:{'data':[],'paginationToken':None})['status'],'DONE')
        def response(raw):
            if 'usdPrice' in json.loads(raw).get(fixtures.SOL,{}):
                raise HTTPError('https://api.jup.ag/price/v3',403,'synthetic forbidden',{},None)
            return Response(raw)
        # Isolate helper's synthetic credential getenv from cycle's real pacing
        # path lookup; neither path reads any real credential.
        with patch.object(fixtures,'Response',side_effect=response), patch.object(fixtures.transport,'os',SimpleNamespace(environ=SimpleNamespace(get=lambda *_:None))), patch.object(cycle,'os',SimpleNamespace(environ=SimpleNamespace(get=lambda _:str(self.pacing)))):
            result=self.h.actual_cycle()
        self.assertEqual(result['blockers'],['HTTP_REJECTED'],result)
        self.assertEqual(result['attempted_requests'],5,result)
        self.assertEqual(self.f.progress.admission(self.h.target.scan_id)['requests_used'],13)
        # Fetch synthetic persisted attempts; original source omitted failed ref.
        refs=list(result['attempt_refs']);self.assertEqual(len(refs),4)
        with self.store.connect() as c:keys=[x[0] for x in c.execute('SELECT hash FROM pages')]
        failures=[k for k in keys if self.store.load(k).get('kind')=='paper_read_attempt_v1' and self.store.load(k).get('http_status')==403]
        self.assertEqual(len(failures),1);refs+=failures
        failed=self.store.load(failures[0]);self.assertIsNone(failed['response_bytes_base64']);self.assertIsNone(failed['observed_at'])
        self.args={'pass_id':result['pass_id'],'outcome_hash':result['evidence_hash'],'attempt_refs':refs,'pacing_db':self.pacing}
        self.pin=r.review_http403_plan(self.f.jobs.path,self.store.path,self.h.path,self.cfg,**self.args)

    def approve(self):self.policy.write_text(canonical({'version':1,'associations':[self.pin]}))
    def reconcile(self,**kw):return r.reconcile_http403(self.f.jobs.path,self.store.path,self.h.path,self.cfg,**(self.args|kw))
    def gate(self,scans=()):return terminal.gate(self.store,self.f.jobs.path,scans)

    def test_actual_http403_reviewed_retirement_original_null_charges_and_no_retry(self):
        before_e=dump(self.store.path);before_l=dump(self.h.path);before_j=dump(self.f.jobs.path);before_p=dump(self.pacing)
        with self.assertRaisesRegex(ValueError,'not explicitly reviewed'):self.reconcile()
        self.assertEqual(dump(self.store.path),before_e);self.assertEqual(self.gate(),'OBSERVATION_RECOVERY_REQUIRED')
        self.approve();result=self.reconcile();self.assertEqual(result['status'],'RECORDED');self.assertFalse(result['entry_authorized'])
        after=dump(self.store.path);self.assertEqual(self.reconcile()['status'],'ALREADY_RECORDED');self.assertEqual(dump(self.store.path),after)
        self.assertEqual(dump(self.h.path),before_l);self.assertEqual(dump(self.f.jobs.path),before_j);self.assertEqual(dump(self.pacing),before_p)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?',(self.pin['pass_id'],)).fetchone(),(self.pin['intent_hash'],None))
        self.assertIsNone(self.gate());self.assertEqual(self.gate((self.h.target.scan_id,)),'REJECTED_SCAN_RETIRED')
        calls=len(self.h.http_calls);result=self.h.actual_cycle();self.assertEqual(result['blockers'],['REJECTED_SCAN_RETIRED']);self.assertEqual(len(self.h.http_calls),calls)
        other=self.f.target();self.assertIsNone(self.gate((other.scan_id,)))
        with self.store.connect() as c:c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)',('f'*32,'e'*64))
        self.assertEqual(self.gate((other.scan_id,)),'OBSERVATION_RECOVERY_REQUIRED')

    def test_wrong_status_ambiguous_body_source_request_and_counter_refuse(self):
        self.approve();failed=self.store.load(self.args['attempt_refs'][-1])
        for field,value in [('http_status',401),('http_status',True),('failure_code','TRANSPORT_ERROR'),('source_id','other'),('method','jupiter_probe'),('response_bytes_base64','e30='),('observed_at',self.f.at),('requests_used',12),('params',{'ids':self.h.target.mint})]:
            with self.subTest(field=field,value=value):
                record=copy.deepcopy(failed);record[field]=value;refs=self.args['attempt_refs'][:4]+[self.store.save(record)];before=dump(self.store.path)
                with self.assertRaises(ValueError):self.reconcile(attempt_refs=refs)
                self.assertEqual(dump(self.store.path),before)
        for refs in [self.args['attempt_refs'][:4],self.args['attempt_refs'][::-1],[self.args['attempt_refs'][0]]*5]:
            before=dump(self.store.path)
            with self.assertRaises((ValueError,KeyError)):self.reconcile(attempt_refs=refs)
            self.assertEqual(dump(self.store.path),before)

    def test_pending_pacing_and_changed_ledger_refuse_nonmutating(self):
        self.approve()
        with sqlite3.connect(self.pacing) as c:c.execute("UPDATE state SET pending=? WHERE provider='jupiter'",('a'*32,))
        before=dump(self.store.path);pacing=dump(self.pacing)
        with self.assertRaisesRegex(ValueError,'pending'):self.reconcile()
        self.assertEqual(dump(self.store.path),before);self.assertEqual(dump(self.pacing),pacing)
        with sqlite3.connect(self.pacing) as c:c.execute('UPDATE state SET pending=NULL')
        with sqlite3.connect(self.h.path) as c:
            state=json.loads(c.execute('SELECT payload FROM state').fetchone()[0]);state['cash']='4';c.execute('UPDATE state SET payload=?',(canonical(state),))
        ledger=dump(self.h.path)
        with self.assertRaises(ValueError):self.reconcile()
        self.assertEqual(dump(self.store.path),before);self.assertEqual(dump(self.h.path),ledger)

    def test_receipt_guards_partial_publication_and_original_evidence_corruption(self):
        self.approve();self.reconcile()
        with self.store.connect() as c:
            with self.assertRaises(sqlite3.IntegrityError):c.execute(f'DELETE FROM {r.TABLE}')
            c.execute(f'DROP TRIGGER {r.TABLE}_update')
        before=dump(self.store.path)
        with self.assertRaisesRegex(ValueError,'schema/guards'):self.gate()
        self.assertEqual(dump(self.store.path),before)

    def test_all_three_consumers_retire_before_credentials_and_unrelated_wrapper_passes(self):
        from tools import history_first_paper_entry as tool
        from desk.paper_observe_cli import observe
        from desk.ownership_acquisition import _Setup
        self.approve();self.reconcile()
        before=dump(self.store.path)
        with patch.object(tool.cli,'_credentials',side_effect=AssertionError('no credentials')):
            with self.assertRaisesRegex(ValueError,'REJECTED_SCAN_RETIRED'):
                with tool._context(self.f.jobs.path,self.store.path,self.h.path,self.cfg,self.h.item):pass
        self.assertEqual(observe(self.f.jobs.path,self.store.path,candidates=(self.h.target,))['stopped_reason'],'REJECTED_SCAN_RETIRED')
        self.assertEqual(dump(self.store.path),before)
        other=self.f.target();_Setup(self.store,self.f.jobs.descriptor(other.scan_id),self.f.progress.admission(other.scan_id))
        before=dump(self.store.path)
        with patch.object(tool.time,'time',return_value=self.f.at),patch.object(tool.cli,'_credentials',side_effect=AssertionError('no credentials')):
            with tool._context(self.f.jobs.path,self.store.path,self.h.path,self.cfg,replace(self.h.item,target=other)) as (_,progress):
                self.assertEqual(progress.admission(other.scan_id)['requests_used'],0)
        self.assertEqual(dump(self.store.path),before)

    def test_changed_result_missing_binding_attempt_evidence_and_additional_charge_refuse(self):
        self.approve()
        original=self.store.load(self.args['outcome_hash'])
        for field,value in [('intent_hash','a'*64),('pass_id','a'*32),('events',[{}]),('outcomes',[{'side':'buy'}]),('attempt_refs',self.args['attempt_refs']),('attempted_requests',True)]:
            with self.subTest(field=field):
                value_record=copy.deepcopy(original);value_record[field]=value;key=self.store.save(value_record);before=dump(self.store.path)
                with self.assertRaises(ValueError):self.reconcile(outcome_hash=key)
                self.assertEqual(dump(self.store.path),before)
        with self.store.connect() as c:c.execute('DELETE FROM pages WHERE hash=?',(self.args['attempt_refs'][-1],))
        before=dump(self.store.path)
        with self.assertRaisesRegex(ValueError,'Evidence'):self.reconcile()
        self.assertEqual(dump(self.store.path),before)

    def test_partial_ddl_and_failed_insert_roll_back(self):
        self.approve();before=dump(self.store.path)
        original=r._guards
        def broken():return original() | {'synthetic':"CREATE TRIGGER synthetic BEFORE INSERT ON paper_http403_retirements BEGIN SELECT RAISE(ABORT,'synthetic crash'); END"}
        with patch.object(r,'_guards',side_effect=broken),self.assertRaises(sqlite3.IntegrityError):self.reconcile()
        self.assertEqual(dump(self.store.path),before)
        with self.store.connect() as c:c.execute(r._schema())
        before=dump(self.store.path)
        with self.assertRaisesRegex(ValueError,'schema/guards'):self.gate()
        self.assertEqual(dump(self.store.path),before)
