"""Real durable admissions, mocked HTTPS only; no credential/provider access."""
import base64
from email.message import Message
from http.client import HTTPResponse, IncompleteRead
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit, parse_qs

from desk import paper_read_sources as m
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.model import canonical
from desk.providers import SOL
from desk.security import TOKEN_PROGRAM, base58

KEY = 'synthetic-fixture-key'
MINT = base58(bytes([7])*32)
TAKER = base58(bytes([8])*32)


class Response:
    status = 200
    def __init__(self, body, headers=()):
        self.body = body; self.reads = 0; self.headers = Message()
        for k,v in headers: self.headers[k] = v
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self, n):
        self.reads += 1
        if isinstance(self.body, Exception): raise self.body
        return self.body[:n]


class PaperReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'evidence.sqlite'
        self.store = EvidenceStore(self.path); self.progress = HistoryProgress(self.store)
        self.progress.admit('scan',{'kind':'ownership_admission_v1','scan_id':'scan','mint':MINT,'created':100})
        self.source = m.PaperReadSources(self.progress,'scan')
        self.account = {'owner':TOKEN_PROGRAM,'executable':False,'data':[base64.b64encode(bytes(82)).decode(),'base64']}
        self.result = {'context':{'slot':101},'value':self.account}
        self.params = [MINT,{'encoding':'base64','commitment':'confirmed'}]
        self.quote = {'inAmount':'10','outAmount':'20','routePlan':[], 'unknown':{'keep':None}}
    def body(self, result=None): return canonical({'jsonrpc':'2.0','id':m.RPC_ID,'result':self.result if result is None else result}).encode()
    def call(self, response=None, kind='rpc', clock=None, opened_error=None):
        response = Response(self.body()) if response is None else response
        before = self.progress.admission('scan')['requests_used']
        class Opener:
            def open(inner,request,*,timeout):
                self.assertEqual(self.progress.admission('scan')['requests_used'],before+1)
                self.request=request;self.timeout=timeout
                if opened_error:raise opened_error
                return response
        opener = Opener()
        with patch.object(m.os.environ,'get',return_value=KEY), patch.object(m,'build_opener',return_value=opener) as build, patch.object(m.time,'time',return_value=100):
            with patch.object(m.time,'monotonic',side_effect=clock) if clock is not None else patch.object(m.time,'monotonic',return_value=1):
                if kind=='quote': result=self.source.quote(SOL,MINT,10,TAKER,timeout_seconds=3)
                elif kind=='price': result=self.source.sol_price(timeout_seconds=3)
                else: result=self.source.rpc('getAccountInfo',self.params,timeout_seconds=3)
        self.assertEqual(build.call_count,1)
        return result
    def outcome(self, error):return self.store.load(error.evidence_hash)
    def test_rpc_original_confirmed_request_single_attempt_and_persisted_wire(self):
        raw=b' {"id":"paper-read-v1", "jsonrpc":"2.0", "result":'+canonical(self.result).encode()+b'}\n'
        self.assertEqual(self.call(Response(raw)),self.result)
        self.assertEqual(json.loads(self.request.data)['params'],self.params)
        self.assertEqual(self.request.method,'POST');self.assertEqual(self.timeout,3)
        self.assertEqual(urlsplit(self.request.full_url).netloc,'mainnet.helius-rpc.com')
        with self.store.connect() as c: keys=[r[0] for r in c.execute('SELECT hash FROM pages')]
        record=self.store.load(keys[-1]);self.assertEqual(base64.b64decode(record['response_bytes_base64']),raw)
        self.assertEqual(base64.b64decode(record['request_bytes_base64']),self.request.data)
        self.assertIsNone(record['failure_code']);self.assertNotIn(KEY,canonical(record))
    def test_quote_and_sol_price_fixed_get_paths_preserve_provider_fields(self):
        for kind,body,path in [('quote',self.quote,'/swap/v2/build'),('price',{SOL:{'usdPrice':100,'blockId':101,'decimals':9}},'/price/v3')]:
            result=self.call(Response(canonical(body).encode()),kind=kind)
            self.assertEqual(result['response'],body);self.assertEqual(result['observed_at'],100)
            self.assertEqual(self.request.method,'GET');self.assertEqual(urlsplit(self.request.full_url).path,path)
            self.assertEqual(self.request.get_header('X-api-key'),KEY)
            if kind=='price':self.assertEqual(parse_qs(urlsplit(self.request.full_url).query),{'ids':[SOL]})
            else:self.assertEqual(result['request']['slippageBps'],'100')
        self.assertEqual(self.progress.admission('scan')['requests_used'],2)
    def test_confirmed_multiple_accounts_keeps_key_order_floor(self):
        params=[[MINT,SOL],{'encoding':'base64','commitment':'confirmed','minContextSlot':100}]
        body=self.body({'context':{'slot':101},'value':[self.account,None]})
        with patch.object(m.os.environ,'get',return_value=KEY),patch.object(m,'build_opener') as b:
            b.return_value.open.return_value=Response(body)
            result=self.source.rpc('getMultipleAccounts',params,timeout_seconds=3)
            self.assertEqual(result['value'],[self.account,None]);self.assertEqual(json.loads(b.return_value.open.call_args.args[0].data)['params'],params)
    def test_invalid_grammar_before_credentials_network_or_charge(self):
        variants=[('getSlot',[]),('getAccountInfo',[MINT,{'encoding':'base64','commitment':'finalized'}]),('getMultipleAccounts',[[MINT,MINT],{'encoding':'base64','commitment':'confirmed','minContextSlot':0}]),('getMultipleAccounts',[[MINT],{'encoding':'base64','commitment':'confirmed','minContextSlot':True}])]
        with patch.object(m.os.environ,'get',side_effect=AssertionError('credentials')),patch.object(m,'build_opener',side_effect=AssertionError('network')):
            for method,params in variants:
                with self.assertRaisesRegex(m.PaperReadError,'REQUEST_INVALID'):self.source.rpc(method,params,timeout_seconds=3)
            for timeout in [True,0,-1,float('nan'),float('inf'),16]:
                with self.assertRaisesRegex(m.PaperReadError,'DEADLINE_INVALID'):self.source.rpc('getAccountInfo',self.params,timeout_seconds=timeout)
        self.assertEqual(self.progress.admission('scan')['requests_used'],0)
    def test_failure_charged_exact_partial_preserved_and_redacted(self):
        for body in [b'',b'{"looks":"valid"}',self.body()]:
            with self.assertRaises(m.PaperReadError) as caught:self.call(Response(IncompleteRead(body)))
            error=caught.exception;self.assertEqual(error.code,'RESPONSE_TRUNCATED');self.assertEqual(base64.b64decode(self.outcome(error)['response_bytes_base64']),body)
        with self.assertRaises(m.PaperReadError) as caught:self.call(opened_error=RuntimeError('secret-url-'+KEY))
        self.assertEqual(str(caught.exception),'TRANSPORT_ERROR');self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(KEY,canonical(self.outcome(caught.exception)))
        self.assertEqual(self.progress.admission('scan')['requests_used'],4)
        self.assertEqual(self.outcome(error)['observed_at'],100)
    def test_header_ambiguity_transfer_encoding_caps_truncation(self):
        for headers in [[('Transfer-Encoding','gzip')],[('Content-Encoding','gzip')],[('Content-Length','1'),('Content-Length','1')],[('Content-Length','-1')],[('Content-Length',str(m.MAX_RESPONSE_BYTES+1))]]:
            r=Response(self.body(),headers)
            with self.assertRaises(m.PaperReadError):self.call(r)
            self.assertEqual(r.reads,0)
        with self.assertRaisesRegex(m.PaperReadError,'RESPONSE_TRUNCATED'):self.call(Response(self.body(),[('Content-Length','1')]))
        with self.assertRaisesRegex(m.PaperReadError,'RESPONSE_OVERSIZED'):self.call(Response(b'x'*(m.MAX_RESPONSE_BYTES+1)))
    def test_strict_json_ids_errors_and_bounds(self):
        bodies=[b'{"id":"paper-read-v1","id":"paper-read-v1","jsonrpc":"2.0","result":null}',b'{"jsonrpc":"2.0","id":true,"result":null}',b'{"jsonrpc":"2.0","id":"wrong","result":null}',b'{"x":NaN}',b'{"x":1e999}',b'{"x":123456789012345678901}',b'{}{}',b'\xff',self.body({'context':{'slot':True},'value':None})]
        for body in bodies:
            with self.assertRaises(m.PaperReadError):self.call(Response(body))
        error=canonical({'jsonrpc':'2.0','id':m.RPC_ID,'error':{'code':-1,'message':'provider diagnostic'}}).encode()
        with self.assertRaisesRegex(m.PaperReadError,'RPC_ERROR'):self.call(Response(error))
    def test_deadline_includes_reservation_time_and_late_response_retained(self):
        with self.assertRaisesRegex(m.PaperReadError,'DEADLINE_EXCEEDED') as caught:self.call(clock=[1,5])
        self.assertIsNone(self.outcome(caught.exception)['response_bytes_base64'])
        with self.assertRaisesRegex(m.PaperReadError,'DEADLINE_EXCEEDED') as caught:self.call(clock=[1,1.5,2,5])
        self.assertEqual(base64.b64decode(self.outcome(caught.exception)['response_bytes_base64']),self.body())
        self.assertLessEqual(self.timeout,2.5)
    def test_real_httpresponse_rejects_transfer_framing_before_read(self):
        class Socket:
            def __init__(self,raw):self.raw=raw
            def makefile(self,*a):return io.BytesIO(self.raw)
        for framing in [b'Transfer-Encoding: gzip\r\n',b'Transfer-Encoding: chunked\r\nContent-Length: 5\r\n',b'Transfer-Encoding: chunked\r\nTransfer-Encoding: gzip\r\n']:
            response=HTTPResponse(Socket(b'HTTP/1.1 200 OK\r\n'+framing+b'\r\n0\r\n'));response.begin()
            with patch.object(response,'read',side_effect=AssertionError('must not read')),self.assertRaises(m.PaperReadError):self.call(response)
        raw=self.body();response=HTTPResponse(Socket(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(raw)).encode()+b'\r\n\r\n'+raw));response.begin()
        self.assertEqual(self.call(response),self.result)
    def chunked_response(self, body, headers=b'Transfer-Encoding: chunked\r\n'):
        class Socket:
            def makefile(inner,*args):return io.BytesIO(b'HTTP/1.1 200 OK\r\n'+headers+b'\r\n'+body)
        response=HTTPResponse(Socket());response.begin();return response
    def test_chunked_exact_original_entity_and_charge_survive_restart(self):
        raw=b' '+self.body()+b'\n'
        parts=[raw[:13],raw[13:]]
        framed=b''.join(f'{len(p):x}\r\n'.encode()+p+b'\r\n' for p in parts)+b'0\r\n\r\n'
        self.assertEqual(self.call(self.chunked_response(framed)),self.result)
        with self.store.connect() as c: keys=[r[0] for r in c.execute('SELECT hash FROM pages')]
        outcomes=[self.store.load(key) for key in keys]
        attempt=next(row for row in outcomes if row.get('kind')=='paper_read_attempt_v1')
        self.assertEqual(base64.b64decode(attempt['response_bytes_base64']),raw)
        self.assertEqual(HistoryProgress(EvidenceStore(self.path)).admission('scan')['requests_used'],1)
    def test_chunked_conflicting_duplicate_and_unsupported_headers_never_read(self):
        for headers in [b'Transfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n',b'Transfer-Encoding: chunked\r\nContent-Length: 0\r\n',b'Transfer-Encoding: gzip, chunked\r\n',b'Transfer-Encoding: CHUNKED\r\n',b'Transfer-Encoding: chunked\r\nContent-Encoding: gzip\r\n']:
            response=self.chunked_response(b'0\r\n\r\n',headers)
            with patch.object(response,'read',side_effect=AssertionError('must not read')),self.assertRaisesRegex(m.PaperReadError,'RESPONSE_HEADERS_INVALID') as caught:
                self.call(response)
            self.assertIsNone(self.outcome(caught.exception)['response_bytes_base64'])
    def test_chunked_truncated_body_retains_partial_without_success(self):
        raw=self.body()
        frame=f'{len(raw):x}\r\n'.encode()+raw+b'\r\n'
        with self.assertRaisesRegex(m.PaperReadError,'RESPONSE_TRUNCATED') as caught:
            self.call(self.chunked_response(frame))
        self.assertEqual(base64.b64decode(self.outcome(caught.exception)['response_bytes_base64']),raw)
    def test_chunked_oversized_entity_and_deadline_remain_blocked(self):
        raw=b'x'*(m.MAX_RESPONSE_BYTES+1)
        framed=f'{len(raw):x}\r\n'.encode()+raw+b'\r\n0\r\n\r\n'
        with self.assertRaisesRegex(m.PaperReadError,'RESPONSE_OVERSIZED'):
            self.call(self.chunked_response(framed))
        raw=self.body();framed=f'{len(raw):x}\r\n'.encode()+raw+b'\r\n0\r\n\r\n'
        with self.assertRaisesRegex(m.PaperReadError,'DEADLINE_EXCEEDED') as caught:
            self.call(self.chunked_response(framed),clock=[1,1.5,2,5])
        self.assertEqual(base64.b64decode(self.outcome(caught.exception)['response_bytes_base64']),raw)
    def test_missing_credentials_charge_and_exhaustion_survives_reconstruction(self):
        with patch.object(m.os.environ,'get',return_value=None),patch.object(m,'build_opener',side_effect=AssertionError('network')):
            for _ in range(18):
                with self.assertRaisesRegex(m.PaperReadError,'CREDENTIAL_UNAVAILABLE'):self.source.sol_price(timeout_seconds=3)
            reopened=m.PaperReadSources(HistoryProgress(EvidenceStore(self.path)),'scan')
            with self.assertRaisesRegex(m.PaperReadError,'BUDGET_EXHAUSTED'):reopened.rpc('getAccountInfo',self.params,timeout_seconds=3)
        self.assertEqual(self.progress.admission('scan')['requests_used'],18)
    def test_missing_admission_never_creates_budget_or_network(self):
        other=m.PaperReadSources(self.progress,'missing')
        with patch.object(m.os.environ,'get',side_effect=AssertionError('credential')):
            with self.assertRaisesRegex(m.PaperReadError,'ADMISSION_REQUIRED'):other.sol_price(timeout_seconds=3)
        self.assertIsNone(self.progress.admission('missing'))

    def test_redirect_and_http_failures_static_charged_once(self):
        for error in [m.strict.CoordinatorRPCError('https://secret/'+KEY),HTTPError('https://secret/'+KEY,429,'secret',{},io.BytesIO(b'secret'))]:
            before=self.progress.admission('scan')['requests_used']
            with self.assertRaisesRegex(m.PaperReadError,'HTTP_REJECTED') as caught:self.call(opened_error=error)
            self.assertEqual(self.progress.admission('scan')['requests_used'],before+1)
            self.assertNotIn(KEY,canonical(self.outcome(caught.exception)))

    def test_outcome_write_failure_does_not_refund_attempt(self):
        with patch.object(self.store,'save',side_effect=OSError('secret-'+KEY)):
            with self.assertRaisesRegex(m.PaperReadError,'OUTCOME_PERSISTENCE_FAILED'):self.call()
        self.assertEqual(self.progress.admission('scan')['requests_used'],1)

    def test_original_params_detached_before_transport(self):
        class Opener:
            def open(inner,request,*,timeout):
                self.params[1]['commitment']='processed'
                self.assertEqual(json.loads(request.data)['params'][1]['commitment'],'confirmed')
                return Response(self.body())
        with patch.object(m.os.environ,'get',return_value=KEY),patch.object(m,'build_opener',return_value=Opener()):
            self.source.rpc('getAccountInfo',self.params,timeout_seconds=3)
        with self.store.connect() as c:key=c.execute('SELECT hash FROM pages').fetchone()[0]
        self.assertEqual(self.store.load(key)['params'][1]['commitment'],'confirmed')

    def test_quote_requires_admitted_mint_sol_pair_before_charge(self):
        with patch.object(m.os.environ,'get',side_effect=AssertionError('credential')):
            with self.assertRaisesRegex(m.PaperReadError,'ADMISSION_REQUIRED'):
                self.source.quote(SOL,TAKER,10,TAKER,timeout_seconds=3)
        self.assertEqual(self.progress.admission('scan')['requests_used'],0)

    def test_exact_price_bytes_and_charged_slot_time_witnesses(self):
        price_raw=b' {"'+SOL.encode()+b'": {"usdPrice": 100.00, "blockId":105,"decimals":9}}\n'
        requests=[]
        class Opener:
            def open(inner,request,*,timeout):
                requests.append(request)
                self.assertEqual(self.progress.admission('scan')['requests_used'],len(requests))
                if request.method=='GET':return Response(price_raw)
                body=json.loads(request.data)
                if body['method']=='getSlot':
                    self.assertEqual(body['params'],[{'commitment':'finalized'}]);return Response(self.body(110))
                self.assertEqual(body['method'],'getBlockTime');self.assertEqual(body['params'],[105])
                return Response(self.body(995))
        with patch.object(m.os.environ,'get',return_value=KEY),patch.object(m,'build_opener',return_value=Opener()),patch.object(m.time,'time',return_value=998):
            slot,slot_ref=self.source.rpc_with_evidence('getSlot',[{'commitment':'finalized'}],timeout_seconds=3)
            price=self.source.sol_price(timeout_seconds=3)
            actual_block=price['response'][SOL]['blockId']
            block_time,time_ref=self.source.rpc_with_evidence('getBlockTime',[actual_block],timeout_seconds=3)
        self.assertEqual((slot,actual_block,block_time),(110,105,995))
        record=self.store.load(price['evidence_hash'])
        self.assertEqual(base64.b64decode(record['response_bytes_base64']),price_raw)
        self.assertNotEqual(canonical(price['response']).encode(),price_raw)
        self.assertEqual(record['observed_at'],price['observed_at']);self.assertEqual(record['http_status'],200)
        for ref in (slot_ref,time_ref,price['evidence_hash']):
            saved=self.store.load(ref);self.assertIsNone(saved['failure_code']);self.assertEqual(saved['scan_id'],'scan')
        self.assertEqual(self.store.load(time_ref)['params'],[actual_block])
        self.assertEqual(self.progress.admission('scan')['requests_used'],3)

    def test_price_parser_limit_exact_boundary_and_oversize(self):
        body=canonical({SOL:{'usdPrice':100,'blockId':105,'decimals':9}}).encode()
        raw=body+b' '*(m.MAX_PRICE_RESPONSE_BYTES-len(body))
        result=self.call(Response(raw),kind='price')
        self.assertEqual(len(base64.b64decode(self.store.load(result['evidence_hash'])['response_bytes_base64'])),65536)
        with self.assertRaisesRegex(m.PaperReadError,'RESPONSE_OVERSIZED') as caught:self.call(Response(raw+b' '),kind='price')
        saved=self.outcome(caught.exception);self.assertIsNone(saved['response_bytes_base64'])
        self.assertEqual(saved['failure_code'],'RESPONSE_OVERSIZED')
        response=Response(raw,[('Content-Length','65537')])
        with self.assertRaisesRegex(m.PaperReadError,'RESPONSE_OVERSIZED'):self.call(response,kind='price')
        self.assertEqual(response.reads,0)

    def test_slot_time_grammar_before_charge_and_unknown_time_rejects(self):
        variants=[('getSlot',[{'commitment':'confirmed'}]),('getSlot',[{'commitment':'finalized','other':True}]),('getBlockTime',[True]),('getBlockTime',[-1]),('getBlockTime',[2**63]),('getBlockTime',[1,2])]
        with patch.object(m.os.environ,'get',side_effect=AssertionError('credential')):
            for method,params in variants:
                with self.assertRaisesRegex(m.PaperReadError,'REQUEST_INVALID'):self.source.rpc_with_evidence(method,params,timeout_seconds=3)
        self.assertEqual(self.progress.admission('scan')['requests_used'],0)
        raw=b'{"jsonrpc":"2.0","id":"paper-read-v1","result":null}'
        with patch.object(m.os.environ,'get',return_value=KEY),patch.object(m,'build_opener') as b:
            b.return_value.open.return_value=Response(raw)
            with self.assertRaisesRegex(m.PaperReadError,'RESPONSE_INVALID') as caught:self.source.rpc_with_evidence('getBlockTime',[105],timeout_seconds=3)
        saved=self.outcome(caught.exception);self.assertEqual(saved['params'],[105])
        self.assertEqual(base64.b64decode(saved['response_bytes_base64']),raw)
        self.assertEqual(self.progress.admission('scan')['requests_used'],1)
