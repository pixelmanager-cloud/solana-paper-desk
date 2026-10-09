"""Synthetic durable history fixtures; HTTPS opener is always mocked."""
import unittest
from unittest.mock import patch
from tests.test_paper_read_sources import Response, KEY, MINT
from tests import test_paper_read_sources as fixtures
from desk.paper_history_source import PaperHistorySource
from desk.paper_read_sources import PaperReadError
from desk import paper_read_sources as transport
from desk.model import canonical

class HistorySourceTests(unittest.TestCase):
    def setUp(self):
        fixtures.PaperReadTests.setUp(self)
        self.key = self.progress.create('scan', MINT, 700, 1001)
    def advance(self, response):
        source = PaperHistorySource(self.progress, 'scan', self.key, timeout_seconds=3)
        class Opener:
            def open(inner, request, *, timeout):
                self.assertEqual(self.progress.admission('scan')['requests_used'],1)
                self.assertLessEqual(timeout,3)
                return response
        with patch.object(transport.os.environ,'get',return_value=KEY), patch.object(transport,'build_opener',return_value=Opener()):
            state = self.progress.advance(self.key, source)
        return state,source
    def test_reserved_once_original_page_and_wire(self):
        raw = b' {"jsonrpc":"2.0","id":"paper-read-v1","result":{"data":[]}}\n'
        state,source = self.advance(Response(raw))
        self.assertEqual(state['status'],'DONE');self.assertEqual(state['requests_used'],1)
        import base64
        record=self.store.load(source.evidence_hash)
        self.assertEqual(base64.b64decode(record['response_bytes_base64']),raw)
        self.assertIsNone(record['failure_code'])
        manifest=self.store.load(state['coverage']['pages'][0]['request_evidence_hash'])
        self.assertEqual(manifest['params'][1]['filters']['tokenAccounts'],'none')
    def test_unreserved_mismatched_and_repeated_rejected(self):
        source=PaperHistorySource(self.progress,'scan',self.key,timeout_seconds=3)
        with self.assertRaises(PaperReadError):source('getTransactionsForAddress',[])
        with self.assertRaises(PaperReadError):PaperHistorySource(self.progress,'other',self.key,timeout_seconds=3)
        state,source=self.advance(Response(canonical({'jsonrpc':'2.0','id':'paper-read-v1','result':{'data':[]}}).encode()))
        with self.assertRaises(PaperReadError):source('getTransactionsForAddress',[])
        self.assertEqual(state['requests_used'],1)
    def test_failure_retained_and_charged(self):
        from http.client import IncompleteRead
        state,source=self.advance(Response(IncompleteRead(b'{partial',10)))
        self.assertEqual(state['status'],'RETRYABLE_ERROR');self.assertEqual(state['requests_used'],1)
        record=self.store.load(source.evidence_hash)
        self.assertEqual(record['failure_code'],'RESPONSE_TRUNCATED')


    def test_continuation_exact_cursor_without_double_charge(self):
        calls=[]
        class Opener:
            def open(inner, request, *, timeout):
                import json
                body=json.loads(request.data);calls.append(body)
                return Response(canonical({'jsonrpc':'2.0','id':'paper-read-v1','result':
                    {'data':[], 'paginationToken':'cursor'} if len(calls)==1 else {'data':[]}}).encode())
        with patch.object(transport.os.environ,'get',return_value=KEY),patch.object(transport,'build_opener',return_value=Opener()):
            first=self.progress.advance(self.key,PaperHistorySource(self.progress,'scan',self.key,timeout_seconds=3))
            second=self.progress.advance(self.key,PaperHistorySource(self.progress,'scan',self.key,timeout_seconds=3))
        self.assertEqual(first['status'],'PENDING');self.assertEqual(second['status'],'DONE')
        self.assertEqual(second['requests_used'],2);self.assertEqual(len(calls),2)
        self.assertEqual(calls[1]['params'][1]['paginationToken'],'cursor')

    def test_matching_request_without_reservation_rejects_before_network(self):
        source=PaperHistorySource(self.progress,'scan',self.key,timeout_seconds=3)
        params=[MINT,{'transactionDetails':'full','sortOrder':'asc','limit':100,'commitment':'finalized',
                     'encoding':'jsonParsed','maxSupportedTransactionVersion':1,
                     'filters':{'blockTime':{'gte':700,'lt':1001},'status':'any','tokenAccounts':'none'}}]
        with patch.object(transport,'build_opener') as opened:
            with self.assertRaises(PaperReadError):source('getTransactionsForAddress',params)
        opened.assert_not_called();self.assertEqual(self.progress.admission('scan')['requests_used'],0)

    def test_bad_deadline_and_slot_query_refuse(self):
        with self.assertRaises(PaperReadError):PaperHistorySource(self.progress,'scan',self.key,timeout_seconds=True)
        other=self.progress.create('scan',MINT,700,1001,slot_range={'gte':0,'lt':100})
        with self.assertRaises(PaperReadError):PaperHistorySource(self.progress,'scan',other,timeout_seconds=3)

    def test_cross_budget_substitution_and_restart_reject_before_io(self):
        from desk.history_progress import HistoryProgress
        self.progress.admit('other',{'kind':'ownership_admission_v1','scan_id':'other','mint':MINT,'created':100})
        source=PaperHistorySource(self.progress,'scan',self.key,timeout_seconds=3)
        with self.store.connect() as connection:
            connection.execute('UPDATE ownership_history SET budget=? WHERE id=?',('other',self.key))
        with patch.object(transport.os.environ,'get') as credential,patch.object(transport,'build_opener') as opener:
            state=self.progress.advance(self.key,source)
        credential.assert_not_called();opener.assert_not_called()
        self.assertEqual(state['status'],'RETRYABLE_ERROR')
        self.assertEqual(self.progress.admission('scan')['requests_used'],0)
        self.assertEqual(self.progress.admission('other')['requests_used'],1)
        self.assertIsNone(source.evidence_hash)
        reopened=HistoryProgress(self.store)
        for scan in ('scan','other'):
            with self.assertRaises(PaperReadError):PaperHistorySource(reopened,scan,self.key,timeout_seconds=3)
        self.assertEqual(reopened.admission('scan')['requests_used'],0)
        self.assertEqual(reopened.admission('other')['requests_used'],1)

    def test_budget_substitution_at_transport_reservation_boundary_rejects(self):
        self.progress.admit('other',{'kind':'ownership_admission_v1','scan_id':'other','mint':MINT,'created':100})
        source=PaperHistorySource(self.progress,'scan',self.key,timeout_seconds=3)
        read=source._bound_snapshot
        calls=0
        def rebound():
            nonlocal calls
            calls+=1
            # Call-time check passes, then transport admission/reservation rechecks.
            if calls==2:
                with self.store.connect() as connection:
                    connection.execute('UPDATE ownership_history SET budget=? WHERE id=?',('other',self.key))
            return read()
        with patch.object(source,'_bound_snapshot',side_effect=rebound),patch.object(transport.os.environ,'get') as credential,patch.object(transport,'build_opener') as opener:
            state=self.progress.advance(self.key,source)
        credential.assert_not_called();opener.assert_not_called()
        self.assertEqual(state['status'],'RETRYABLE_ERROR')
        self.assertEqual(self.progress.admission('scan')['requests_used'],1)
        self.assertEqual(self.progress.admission('other')['requests_used'],0)

    def test_interrupted_attempt_restart_preserves_window_and_counter(self):
        from desk.history_progress import HistoryProgress
        class Interrupted(BaseException):pass
        class Opener:
            def open(inner,request,*,timeout):raise Interrupted()
        with patch.object(transport.os.environ,'get',return_value=KEY),patch.object(transport,'build_opener',return_value=Opener()):
            with self.assertRaises(Interrupted):
                self.progress.advance(self.key,PaperHistorySource(self.progress,'scan',self.key,timeout_seconds=3))
        self.progress=HistoryProgress(self.store)
        before=self.progress.snapshot(self.key)
        self.assertEqual(before['requests_used'],1);self.assertEqual(before['attempts'],1)
        self.assertEqual(before['status'],'PENDING');self.assertIsNone(before['coverage'])
        class Success:
            def open(inner,request,*,timeout):
                self.assertEqual(self.progress.admission('scan')['requests_used'],2)
                return Response(canonical({'jsonrpc':'2.0','id':'paper-read-v1','result':{'data':[]}}).encode())
        with patch.object(transport.os.environ,'get',return_value=KEY),patch.object(transport,'build_opener',return_value=Success()):
            after=self.progress.advance(self.key,PaperHistorySource(self.progress,'scan',self.key,timeout_seconds=3))
        self.assertEqual(after['query'],before['query']);self.assertEqual(after['requests_used'],2)
        self.assertEqual(after['attempts'],2);self.assertEqual(after['status'],'DONE')
