"""Synthetic original transactions, actual transport/replay/ledger, no providers."""
import copy
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
import time
import unittest
from unittest.mock import patch
from desk import paper_cycle as cycle, paper_read_sources as transport, provider_pacing as pacing
from desk.model import canonical,digest
from desk.paper_history_source import PaperHistorySource
from desk.security import base58
from tools import history_first_paper_entry as entry, history_preparation_rejection as rejection
from tests import test_paper_entry_dispatcher as fixture
from tests.test_live_strategy_features import transaction
from tests.test_paper_read_sources import Response


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.DispatcherTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.store=self.f.f.progress.store;self.progress=self.f.f.progress
        self.target=self.f.f.target();self.as_of=int(time.time())
        self.item=cycle.CycleTarget(self.target,provenance='SYNTHETIC_TEST_ONLY',
            graduated_at=None,holder_at=None,pool_fee_bps='25',history_as_of=self.as_of)
        self.cfg=self.f.cfg;self.ledger=self.f.ledger;self.pacer=self.f.pacer
        self.context={'research_db':str(self.f.f.jobs.path),'evidence_db':str(self.store.path),
                      'ledger_db':str(self.ledger),'pacing_db':str(self.pacer)}
        for _ in range(6):self.assertTrue(self.progress.reserve(self.target.scan_id))
        self.budget=entry._PreparationBudget(self.progress,self.item,self.cfg)
        with closing(self.store.connect()) as c:
            c.execute('CREATE TABLE paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
        self.identity='a'*32
        self.intent=self.store.save(rejection.intent(self.store,self.ledger,self.cfg,self.item,
            self.progress.admission(self.target.scan_id),self.context))
        with closing(self.store.connect()) as c:c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)',(self.identity,self.intent))
    def rows(self,n,start=0,padding=0):
        result=[]
        for i in range(start,start+n):
            raw=transaction(base58((i+1).to_bytes(64,'big')),i+10,self.as_of-1,quote=200,base=100)
            if padding:raw['meta'].setdefault('logMessages',[]).append('Program log: SYNTHETIC '+('x'*padding))
            result.append(raw)
        return result
    def advance(self,result,*,native=False,latency=0):
        owner=self
        class Opener:
            def open(self,request,*,timeout):
                if latency:time.sleep(latency)
                return Response(canonical({'jsonrpc':'2.0','id':transport.RPC_ID,'result':result}).encode())
        native_configured=lambda **kw:pacing.Pacer(owner.pacer,**kw)
        with patch.dict('os.environ',{'HELIUS_API_KEY':'SYNTHETIC_TEST_ONLY'}),patch.object(transport,'build_opener',return_value=Opener()):
            with patch.object(pacing,'configured',side_effect=native_configured) if native else patch.object(pacing,'configured',return_value=None):
                return self.budget.call(self.target.scan_id,lambda timeout:self.progress.advance(self.budget.history_id,
                    PaperHistorySource(self.progress,self.target.scan_id,self.budget.history_id,timeout_seconds=timeout)))
    def publish(self,reason):
        return rejection.publish(self.store,self.progress,self.ledger,self.cfg,pass_id=self.identity,
            intent_hash=self.intent,history_id=self.budget.history_id,reason=reason)
    def marker(self):
        with closing(self.store.connect()) as c:return c.execute('SELECT outcome_hash FROM paper_observation_passes WHERE id=?',(self.identity,)).fetchone()[0]
    def test_three_full_pages_cross_aggregate_and_publish_exact_no_entry(self):
        self.advance({'data':self.rows(50,padding=13000),'paginationToken':'p2'})
        self.advance({'data':self.rows(50,start=50,padding=13000),'paginationToken':'p3'})
        with self.assertRaises(entry.PreparationRejected) as caught:
            self.advance({'data':self.rows(50,start=100,padding=13000),'paginationToken':'p4'})
        self.assertEqual(caught.exception.code,'HISTORY_FEATURE_AGGREGATE_BYTES_EXCEEDED')
        state=self.progress.snapshot(self.budget.history_id);self.assertEqual(state['requests_used'],9)
        before=cycle._state(self.ledger,self.cfg)
        result=self.publish(caught.exception.code)
        self.assertEqual(rejection.verify(self.store,self.progress,result)['admission']['requests_used'],6)
        self.assertEqual(self.marker(),result['evidence_hash']);self.assertEqual(result['status'],'NO_ENTRY')
        self.assertFalse(result['entry_authorized']);self.assertEqual(len(result['attempt_refs']),3)
        self.assertEqual(cycle._state(self.ledger,self.cfg),before)
        with closing(self.store.connect()) as c:
            with self.assertRaises(sqlite3.IntegrityError):c.execute('INSERT OR REPLACE INTO '+rejection.TABLE+' VALUES(?,?,?,?)',(self.identity,self.target.scan_id,self.intent,result['evidence_hash']))
        for _ in range(2):rejection.verify(self.store,self.progress,result)
        self.assertEqual(self.publish(caught.exception.code),result)
    def test_seven_fresh_requests_reserved_before_sixth_page(self):
        for n in range(5):self.advance({'data':self.rows(1,start=n),'paginationToken':'p'+str(n)})
        with self.assertRaisesRegex(entry.PreparationRejected,'HISTORY_FRESH_ENTRY_REQUESTS_UNAVAILABLE'):
            self.budget.call(self.target.scan_id,lambda _:self.fail('must not invoke or charge'))
        self.assertEqual(self.progress.admission(self.target.scan_id)['requests_used'],11)
        result=self.publish('HISTORY_FRESH_ENTRY_REQUESTS_UNAVAILABLE');rejection.verify(self.store,self.progress,result)
        self.assertEqual(result['admission_after']['request_ceiling']-result['admission_after']['requests_used'],7)
    def test_missing_attempt_and_changed_ledger_never_publish(self):
        self.advance({'data':self.rows(1),'paginationToken':'next'})
        for _ in range(4):self.progress.reserve(self.target.scan_id)
        with self.assertRaisesRegex(ValueError,'attempt missing'):self.publish('HISTORY_FRESH_ENTRY_REQUESTS_UNAVAILABLE')
        self.assertIsNone(self.marker())
    def test_duplicate_attempt_conflict_and_partial_publication_block(self):
        for n in range(5):self.advance({'data':self.rows(1,start=n),'paginationToken':'p'+str(n)})
        attempts=rejection._attempts(self.store,self.target.scan_id,6,11)
        self.store.save({**attempts[0][1],'source_id':'SYNTHETIC_CONFLICT'})
        with self.assertRaisesRegex(ValueError,'ambiguous'):self.publish('HISTORY_FRESH_ENTRY_REQUESTS_UNAVAILABLE')
        self.assertIsNone(self.marker())
    def test_record_limit_and_raw_corruption_block_no_false_disposition(self):
        with self.assertRaisesRegex(entry.PreparationRejected,'RECORD_BYTES'):
            self.advance({'data':self.rows(1,padding=140000),'paginationToken':'next'})
        state=self.progress.snapshot(self.budget.history_id)
        key=state['coverage']['pages'][0]['payload_hash']
        with closing(self.store.connect()) as c:c.execute('UPDATE pages SET raw_bytes=raw_bytes+1 WHERE hash=?',(key,))
        with self.assertRaises(ValueError):self.publish('HISTORY_FEATURE_RECORD_BYTES_EXCEEDED')
        self.assertIsNone(self.marker())
    def test_native_clock_preparation_can_exceed_fresh_ten_seconds(self):
        started=time.monotonic()
        for n in range(5):
            state=self.advance({'data':self.rows(1,start=n),**({'paginationToken':'p'+str(n)} if n<4 else {})},native=True,latency=4.5 if n==0 else 0)
        elapsed=time.monotonic()-started
        self.assertGreater(elapsed,10);self.assertLess(elapsed,18)
        self.assertEqual(state['status'],'DONE');self.assertEqual(state['requests_used'],11)
        self.assertLessEqual(self.budget.remaining(),15)
        # The original independent fresh budget is still exactly ten seconds.
        fresh=cycle._Budget(self.progress,time.time,time.monotonic)
        self.assertLessEqual(fresh.remaining(),10)
        self.assertEqual(self.item.history_as_of,self.as_of)
