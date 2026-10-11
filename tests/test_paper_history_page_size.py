"""Synthetic full parsed pages; real storage/replay/transport, mocked HTTPS only."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from desk import paper_read_sources as transport
from desk.evidence import EvidenceStore
from desk.history import collect_history
from desk.history_progress import HistoryProgress
from desk.model import canonical,digest
from desk.paper_history_source import PaperHistorySource,PAPER_HISTORY_PAGE_SIZE
from desk.replay_history import replay_history
from desk.security import base58
from tests import test_paper_history_source as fixtures
from tests.test_paper_read_sources import Response,KEY,MINT

class PaperHistoryPageSizeTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.HistorySourceTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.store,self.progress=self.f.store,self.f.progress
        self.key=self.progress.create('scan',MINT,700,1001,page_size=PAPER_HISTORY_PAGE_SIZE)
        source=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-launch.json').read_text())['payload']['params']['result']
        self.raw={**source['transaction'],'slot':source['slot'],'blockTime':900,'signature':source['signature']}
    def rows(self,n,start=0,padding=0):
        rows=[]
        for i in range(start,start+n):
            raw=copy.deepcopy(self.raw);raw['signature']=base58(i.to_bytes(64,'little'))
            raw['transaction']['signatures'][0]=raw['signature']
            raw['slot']=self.raw['slot']+i
            if padding:raw['meta'].setdefault('logMessages',[]).append('Program log: SYNTHETIC '+('x'*padding))
            rows.append(raw)
        return rows
    def advance(self,result,calls):
        class Opener:
            def open(self,request,*,timeout):
                calls.append(json.loads(request.data))
                return Response(canonical({'jsonrpc':'2.0','id':'paper-read-v1','result':result}).encode())
        adapter=PaperHistorySource(self.progress,'scan',self.key,timeout_seconds=15)
        with patch.object(transport.os.environ,'get',return_value=KEY),patch.object(transport,'build_opener',return_value=Opener()):
            state=self.progress.advance(self.key,adapter)
        return state,adapter
    def test_large_normal_pages_replay_exactly_across_restart_and_cursor(self):
        calls=[];first={'data':self.rows(50,padding=10000),'paginationToken':'next50'}
        self.assertLess(len(canonical(first).encode()),transport.MAX_RESPONSE_BYTES)
        self.assertGreater(len(canonical({'data':self.rows(100,padding=10000)}).encode()),transport.MAX_RESPONSE_BYTES)
        state,_=self.advance(first,calls)
        self.assertEqual((state['status'],state['requests_used']),('PENDING',1))
        self.progress=HistoryProgress(self.store)
        state,_=self.advance({'data':self.rows(50,start=50)},calls)
        self.assertEqual((state['status'],state['requests_used']),('DONE',2))
        self.assertEqual([r['params'][1]['limit'] for r in calls],[50,50])
        self.assertEqual(calls[1]['params'][1]['paginationToken'],'next50')
        observations,coverage=replay_history(state['coverage'],self.store)
        self.assertEqual(len(observations),100);self.assertEqual(coverage,state['coverage'])
        self.assertEqual(coverage['page_size'],50);self.assertFalse(coverage['launch_history_complete'])
        self.assertEqual(self.progress.snapshot(self.key)['query']['page_size'],50)
    def test_server_exceeds_requested_rows_fails_charged_with_raw_retained(self):
        state,adapter=self.advance({'data':self.rows(51)},[])
        self.assertEqual((state['status'],state['requests_used']),('RETRYABLE_ERROR',1))
        self.assertIsNone(state['coverage']);self.assertIsNotNone(adapter.evidence_hash)
        record=self.store.load(adapter.evidence_hash)
        self.assertIsNone(record['failure_code']) # HTTP syntax success is not valid page coverage.
    def test_one_giant_transaction_still_fails_original_response_cap_and_keeps_charge(self):
        state,adapter=self.advance({'data':self.rows(1,padding=transport.MAX_RESPONSE_BYTES)},[])
        self.assertEqual((state['status'],state['requests_used']),('RETRYABLE_ERROR',1))
        self.assertIsNone(state['coverage']);record=self.store.load(adapter.evidence_hash)
        self.assertEqual(record['failure_code'],'RESPONSE_OVERSIZED')
        self.assertEqual(self.progress.admission('scan')['request_ceiling'],18)
    def test_legacy_hundred_row_query_and_coverage_replay_unchanged(self):
        calls=[]
        _,coverage=collect_history(MINT,700,1001,lambda method,params:(calls.append(params) or {'data':self.rows(40)}),capture=self.store.save,token_accounts='none')
        self.assertEqual(calls[0][1]['limit'],100);self.assertNotIn('page_size',coverage)
        observations,rebuilt=replay_history(coverage,self.store)
        self.assertEqual(len(observations),40);self.assertEqual(rebuilt,coverage)
        self.assertNotIn('page_size',self.progress.snapshot(self.f.key)['query'])
    def test_size_tampering_and_bad_types_reject_without_new_charge(self):
        for value in (True,False,0,101,20.0,'20'):
            with self.subTest(value=value),self.assertRaises(ValueError):self.progress.create('scan',MINT,700,1001,page_size=value)
        state,_=self.advance({'data':self.rows(1)},[])
        forged=copy.deepcopy(state['coverage']);forged['page_size']=19
        forged['evidence_hash']=digest({k:v for k,v in forged.items() if k!='evidence_hash'})
        with self.assertRaises(ValueError):replay_history(forged,self.store)
        self.assertEqual(self.progress.admission('scan')['requests_used'],1)

    def test_seed_smaller_page_coverage_retains_query_size_for_continuation(self):
        _,coverage=collect_history(MINT,701,1002,lambda *args:{'data':self.rows(1),'paginationToken':'seed-next'},max_pages=1,capture=self.store.save,token_accounts='none',page_size=50)
        key=self.progress.seed('scan',coverage)
        self.key=key
        self.assertEqual(key,self.progress.create('scan',MINT,701,1002,page_size=50))
        calls=[]
        state,_=self.advance({'data':self.rows(1,start=1)},calls)
        self.assertEqual(state['status'],'DONE');self.assertEqual(state['requests_used'],1)
        self.assertEqual(calls[0]['params'][1]['paginationToken'],'seed-next')
        self.assertEqual(calls[0]['params'][1]['limit'],50)
        self.assertEqual(len(replay_history(state['coverage'],self.store)[0]),2)

    def incomplete_window(self,prior_charges,expected_calls,blocker):
        from types import SimpleNamespace
        from desk import paper_cycle,live_strategy_features
        for _ in range(prior_charges):self.assertTrue(self.progress.reserve('scan'))
        calls=[];owner=self
        class Opener:
            def open(self,request,*,timeout):
                calls.append(json.loads(request.data));n=len(calls)
                result={'data':owner.rows(1,start=n),'paginationToken':'more-'+str(n)}
                return Response(canonical({'jsonrpc':'2.0','id':'paper-read-v1','result':result}).encode())
        item=SimpleNamespace(target=SimpleNamespace(scan_id='scan',pool=MINT),history_as_of=1000)
        budget=paper_cycle._Budget(self.progress,lambda:1000,lambda:1)
        with patch.object(transport.os.environ,'get',return_value=KEY),patch.object(transport,'build_opener',return_value=Opener()):
            with self.assertRaisesRegex(paper_cycle.CycleBlocked,blocker):paper_cycle._history(self.progress,item,budget,PaperHistorySource)
        self.assertEqual(len(calls),expected_calls)
        state=self.progress.snapshot(self.key);coverage=state['coverage']
        self.assertEqual(state['requests_used'],prior_charges+expected_calls)
        self.assertFalse(coverage['query_range_exhausted']);self.assertFalse(coverage['query_coverage_verified'])
        self.assertFalse(coverage['launch_history_complete'])
        pairs=[];rows=[]
        for p in coverage['pages']:
            response=self.store.load(p['payload_hash']);rows.extend(response['data'])
            pairs.append({'request':self.store.load(p['request_evidence_hash']),'response':response})
        measured=live_strategy_features.calculate(rows,pool=MINT,as_of=1000,provenance='SYNTHETIC_TEST_ONLY',history_pages=pairs)
        self.assertFalse(measured['window']['coverage_complete'])
        self.assertIn('HISTORY_WINDOW_BINDING_OR_EXHAUSTION_UNAVAILABLE',measured['blockers'])
    def test_eight_page_limit_preserves_partial_history_without_window_promotion(self):
        self.incomplete_window(0,8,'FEATURE_HISTORY_PAGE_LIMIT')
    def test_shared_eighteen_limit_preserves_prior_charges_and_partial_history(self):
        self.incomplete_window(16,2,'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED')
