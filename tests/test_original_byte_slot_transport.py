"""Mock opener only; synthetic credentials, no listeners/provider or key reads."""
from dataclasses import FrozenInstanceError
from email.message import Message
from http.client import HTTPResponse, IncompleteRead
import io
import json
import ssl
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs,urlsplit
from urllib.request import HTTPSHandler, HTTPHandler, ProxyHandler, build_opener
from urllib.response import addinfourl

from desk import original_byte_slot_transport as m

KEY='fixture-only-credential-?:/@'
REQUEST=b' \n { "params" : [ { "commitment" : "finalized" } ], "method":"getSlot", "id":"bound-\\u0031", "jsonrpc" : "2.0" } \t '
RESPONSE=b'\r\n { "result" : 42, "id" : "bound-1", "jsonrpc":"2.0" } \t '


class SlotTransportTests(unittest.TestCase):
    def setUp(self):
        self.transport=m.HeliusFinalizedSlotTransport();self.requests=[]

    def opener(self,body=RESPONSE,status=200,headers=None):
        requests=self.requests
        class LocalHTTPS(HTTPSHandler):
            def https_open(handler,request):
                requests.append(request);values=Message()
                for name,value in (headers or []):values[name]=value
                response=addinfourl(io.BytesIO(body),values,request.full_url,status)
                response.msg='fixture response';return response
        class LocalHTTP(HTTPHandler):
            def http_open(handler,request):raise AssertionError('Redirect/fallback HTTP reached')
        # No proxy discovery or real network handler invocation.
        return build_opener(ProxyHandler({}),m.fixed._NoRedirect(),LocalHTTPS(),LocalHTTP())

    def invoke(self,body=RESPONSE,status=200,headers=None,request=REQUEST):
        opener=self.opener(body,status,headers)
        with patch.object(m.fixed.os.environ,'get',return_value=KEY) as credential,patch.object(m.fixed,'build_opener',return_value=opener),patch.object(opener,'open',wraps=opener.open) as opened:
            result=self.transport(request)
        credential.assert_called_once_with('HELIUS_API_KEY');self.assertEqual(opened.call_count,1)
        self.assertEqual(opened.call_args.kwargs,{'timeout':15})
        return result

    def test_exact_whitespace_field_order_escape_request_and_response_identity(self):
        result=self.invoke(headers=[('Content-Length',str(len(RESPONSE)))])
        self.assertIsNone(result.failure_code);self.assertIs(result.request_bytes,REQUEST)
        self.assertEqual(result.response_bytes,RESPONSE);self.assertEqual(self.requests[0].data,REQUEST)
        self.assertNotEqual(self.requests[0].data,json.dumps(json.loads(REQUEST)).encode())
        with self.assertRaises(FrozenInstanceError):result.response_bytes=b'changed'

    def test_fixed_endpoint_headers_one_post_and_no_caller_options(self):
        self.invoke();request=self.requests[0];url=urlsplit(request.full_url)
        self.assertEqual((url.scheme,url.netloc,url.path,url.fragment),('https','mainnet.helius-rpc.com','/',''))
        self.assertEqual(parse_qs(url.query),{'api-key':[KEY]});self.assertEqual(request.method,'POST')
        self.assertEqual(request.get_header('Content-type'),'application/json')
        self.assertEqual(request.get_header('Accept'),'application/json')
        with self.assertRaises(TypeError):m.HeliusFinalizedSlotTransport('https://user.invalid')
        with self.assertRaises(AttributeError):self.transport.url='https://user.invalid'

    def test_constructor_does_no_credentials_or_opener_io(self):
        with patch.object(m.fixed.os.environ,'get',side_effect=AssertionError('credential')),patch.object(m.fixed,'build_opener',side_effect=AssertionError('opener')):
            m.HeliusFinalizedSlotTransport()

    def test_mock_opener_retains_default_verified_tls_context(self):
        opener=self.opener()
        handler=next(h for h in opener.handlers if isinstance(h,HTTPSHandler))
        self.assertEqual(handler._context.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(handler._context.check_hostname)

    def test_invalid_requests_fail_before_credentials_and_opener(self):
        valid=json.loads(REQUEST);variants=[]
        for key,value in (('id',1),('id',True),('id',None),('id',''),('id','x'*129),('id','é'*65),
                          ('jsonrpc','1.0'),('jsonrpc',True),('method','getAccountInfo'),('method','getTransactionsForAddress'),
                          ('method','sendTransaction'),('params',[]),('params',{}),('params',[{'commitment':'confirmed'}]),
                          ('params',[{'commitment':True}]),('params',[{'commitment':'finalized','url':KEY}]),
                          ('url','https://user.invalid'),('source_id',KEY),('result',42)):
            variants.append(json.dumps({**valid,key:value}).encode())
        variants.extend((json.dumps({k:v for k,v in valid.items() if k!='id'}).encode(),b'[]',b'null',b'{}',b'\xff',b'\xef\xbb\xbf'+REQUEST,
            REQUEST+b'{}',b'{',b'['*1024,b' '*(m.MAX_REQUEST_BYTES+1),REQUEST.decode(),bytearray(REQUEST),memoryview(REQUEST),None))
        with patch.object(m.fixed.os.environ,'get',side_effect=AssertionError('credential')) as credential,patch.object(m.fixed,'build_opener',side_effect=AssertionError('opener')) as build:
            for raw in variants:
                with self.subTest(raw=repr(raw)[:60]),self.assertRaises(m.SlotRequestInvalid) as raised:self.transport(raw)
                self.assertEqual(str(raised.exception),'Finalized slot request invalid');self.assertTrue(raised.exception.__suppress_context__)
            credential.assert_not_called();build.assert_not_called()

    def test_duplicate_escaped_keys_constants_and_surrogate_ids_reject_before_io(self):
        raw_requests=[REQUEST.replace(b'"jsonrpc" : "2.0"',b'"jsonrpc":"2.0","jsonrpc":"2.0"'),
            REQUEST.replace(b'"commitment" : "finalized"',b'"commitment":"finalized","\\u0063ommitment":"finalized"'),
            REQUEST.replace(b'"bound-\\u0031"',b'"\\ud800"'),
            REQUEST.replace(b'"bound-\\u0031"',b'NaN'),REQUEST.replace(b'"bound-\\u0031"',b'Infinity'),
            REQUEST.replace(b'"bound-\\u0031"',b'1e999'),REQUEST.replace(b'"bound-\\u0031"',b'9'*21)]
        with patch.object(m.fixed.os.environ,'get',side_effect=AssertionError('credential')) as credential,patch.object(m.fixed,'build_opener') as build:
            for raw in raw_requests:
                with self.subTest(raw=raw),self.assertRaises(m.SlotRequestInvalid):self.transport(raw)
            credential.assert_not_called();build.assert_not_called()

    def test_request_exact_byte_limit_and_utf8_id_limit(self):
        raw=REQUEST+b' '*(m.MAX_REQUEST_BYTES-len(REQUEST));self.assertIsNone(self.invoke(request=raw).failure_code)
        request=json.loads(REQUEST);request['id']='é'*64
        raw=json.dumps(request,ensure_ascii=False).encode();response=json.dumps({'jsonrpc':'2.0','id':request['id'],'result':0},ensure_ascii=False).encode()
        self.assertIsNone(self.invoke(response,request=raw).failure_code)

    def test_response_id_type_exact_equality_and_envelope_shape(self):
        variants=[[],{},None,{'jsonrpc':'1.0','id':'bound-1','result':42},
            {'jsonrpc':'2.0','id':1,'result':42},{'jsonrpc':'2.0','id':True,'result':42},
            {'jsonrpc':'2.0','id':'bound-2','result':42},{'jsonrpc':'2.0','result':42},
            {'jsonrpc':'2.0','id':'bound-1'},{'jsonrpc':'2.0','id':'bound-1','result':42,'error':None},
            {'jsonrpc':'2.0','id':'bound-1','result':42,'extra':KEY}]
        for body in variants:
            with self.subTest(body=body):
                raw=json.dumps(body).encode();result=self.invoke(raw)
                self.assertEqual(result.failure_code,'RESPONSE_INVALID');self.assertEqual(result.response_bytes,raw)

    def test_slot_u64_syntax_bool_null_float_negative_and_overflow(self):
        for slot in (0,2**64-1):
            raw=json.dumps({'jsonrpc':'2.0','id':'bound-1','result':slot}).encode()
            self.assertIsNone(self.invoke(raw).failure_code)
        for slot in (True,None,42.0,-1,2**64,'42',[],{}):
            with self.subTest(slot=slot):self.assertEqual(self.invoke(json.dumps({'jsonrpc':'2.0','id':'bound-1','result':slot}).encode()).failure_code,'RESPONSE_INVALID')
        raw=b'{"jsonrpc":"2.0","id":"bound-1","result":'+b'9'*4000+b'}'
        self.assertEqual(self.invoke(raw).failure_code,'RESPONSE_INVALID')

    def test_rpc_error_keeps_original_body_but_redacts_result_representation(self):
        raw=json.dumps({'jsonrpc':'2.0','id':'bound-1','error':{'code':-32000,'message':KEY,'data':{'private':KEY}}}).encode()
        result=self.invoke(raw);self.assertEqual(result.failure_code,'RPC_ERROR');self.assertEqual(result.response_bytes,raw)
        self.assertNotIn(KEY,repr(result));self.assertNotIn(KEY,json.dumps(result.diagnostic()))
        for field,value in (('code',True),('code',1.5),('code',2**63),('message',{}),('extra',KEY)):
            payload={'code':-32000,'message':KEY,field:value}
            result=self.invoke(json.dumps({'jsonrpc':'2.0','id':'bound-1','error':payload}).encode())
            self.assertEqual(result.failure_code,'RESPONSE_INVALID')

    def test_invalid_utf8_duplicates_constants_trailing_and_deep_response_preserved(self):
        variants=[b'\xff',b'{',RESPONSE+b'{}',b'{"jsonrpc":"2.0","id":"bound-1","result":NaN}',
            b'{"jsonrpc":"2.0","id":"bound-1","result":1e999}',
            b'{"jsonrpc":"2.0","id":"bound-1","result":0,"result":1}',
            b'{"jsonrpc":"2.0","id":"bound-1","error":{"code":-1,"message":"x","message":"y"}}',
            b'['*1100+b'0'+b']'*1100]
        for raw in variants:
            result=self.invoke(raw);self.assertEqual(result.failure_code,'RESPONSE_INVALID');self.assertEqual(result.response_bytes,raw)

    def test_response_exact_byte_cap_and_oversized_body_is_not_truncated_evidence(self):
        raw=RESPONSE+b' '*(m.MAX_RESPONSE_BYTES-len(RESPONSE))
        result=self.invoke(raw,headers=[('Content-Length',str(len(raw)))]);self.assertIsNone(result.failure_code);self.assertEqual(result.response_bytes,raw)
        result=self.invoke(raw+b' ');self.assertEqual(result.failure_code,'RESPONSE_OVERSIZED');self.assertIsNone(result.response_bytes)

    def test_headers_encoding_lengths_duplicates_and_caps(self):
        for headers,code in (([('Content-Encoding','gzip')],'RESPONSE_HEADERS_INVALID'),
            ([('Content-Length','-1')],'RESPONSE_HEADERS_INVALID'),([('Content-Length','１')],'RESPONSE_HEADERS_INVALID'),
            ([('Content-Length','9'*30)],'RESPONSE_HEADERS_INVALID'),([('Content-Length',str(m.MAX_RESPONSE_BYTES+1))],'RESPONSE_OVERSIZED'),
            ([('Content-Length','1'),('Content-Length','2')],'RESPONSE_HEADERS_INVALID'),
            ([('Content-Encoding','identity'),('Content-Encoding','gzip')],'RESPONSE_HEADERS_INVALID')):
            result=self.invoke(headers=headers);self.assertEqual(result.failure_code,code);self.assertIsNone(result.response_bytes)
        result=self.invoke(headers=[('Content-Length',str(len(RESPONSE)+1))]);self.assertEqual(result.failure_code,'RESPONSE_TRUNCATED');self.assertEqual(result.response_bytes,RESPONSE)

    def test_all_redirect_codes_one_attempt_without_fallback(self):
        for status in (301,302,303,307,308):
            for target in ('https://other.invalid/?api-key='+KEY,'http://other.invalid/','/other'):
                self.requests.clear();result=self.invoke(KEY.encode(),status,[('Location',target)])
                self.assertEqual(result.failure_code,'HTTP_REJECTED');self.assertIsNone(result.response_bytes)
                self.assertEqual(len(self.requests),1);self.assertNotIn(KEY,repr(result))

    def test_http_error_statuses_and_failures_are_redacted_single_attempt(self):
        for status in (201,204,300,304,400,401,429,500,503):
            self.requests.clear();result=self.invoke(KEY.encode(),status)
            self.assertEqual(result.failure_code,'HTTP_REJECTED');self.assertIsNone(result.response_bytes);self.assertEqual(len(self.requests),1)
        for error in (URLError(KEY),HTTPError('https://key.invalid/'+KEY,403,KEY,{},io.BytesIO(KEY.encode())),TimeoutError(KEY),OSError(KEY),ValueError(KEY)):
            opener=Mock();opener.open.side_effect=error
            with patch.object(m.fixed.os.environ,'get',return_value=KEY),patch.object(m.fixed,'build_opener',return_value=opener):result=self.transport(REQUEST)
            self.assertIn(result.failure_code,('TRANSPORT_ERROR','HTTP_REJECTED'));opener.open.assert_called_once()
            self.assertNotIn(KEY,repr(result));self.assertNotIn(KEY,json.dumps(result.diagnostic()))

    def test_missing_malformed_credential_does_not_construct_opener(self):
        for key in (None,'','a'*513,'space key','\n',123):
            with patch.object(m.fixed.os.environ,'get',return_value=key),patch.object(m.fixed,'build_opener') as build:
                result=self.transport(REQUEST)
            self.assertEqual(result.failure_code,'CREDENTIAL_UNAVAILABLE');self.assertEqual(result.request_bytes,REQUEST);build.assert_not_called()

    def test_read_limit_and_close_even_on_truncation_and_header_refusal(self):
        for failure in ('length','incomplete','oversized_header','nonbytes','oversized_partial','nonbytes_partial'):
            response=Mock();response.status=200;response.headers={}
            response.read.return_value=RESPONSE
            if failure=='length':response.headers={'Content-Length':str(len(RESPONSE)+1)}
            if failure=='incomplete':response.read.side_effect=IncompleteRead(RESPONSE,100)
            if failure=='oversized_header':response.headers={'Content-Length':str(m.MAX_RESPONSE_BYTES+1)}
            if failure=='nonbytes':response.read.return_value=RESPONSE.decode()
            if failure=='oversized_partial':response.read.side_effect=IncompleteRead(b'x'*(m.MAX_RESPONSE_BYTES+1),100)
            if failure=='nonbytes_partial':response.read.side_effect=IncompleteRead('invalid',100)
            opener=MagicMock();opener.open.return_value.__enter__.return_value=response
            with patch.object(m.fixed.os.environ,'get',return_value=KEY),patch.object(m.fixed,'build_opener',return_value=opener):result=self.transport(REQUEST)
            self.assertIsNotNone(result.failure_code);opener.open.assert_called_once();self.assertEqual(opener.open.return_value.__exit__.call_count,1)
            if failure=='oversized_header':response.read.assert_not_called()
            else:response.read.assert_called_once_with(m.MAX_RESPONSE_BYTES+1)
            if failure=='incomplete':self.assertEqual(result.response_bytes,RESPONSE)
            if failure=='oversized_partial':self.assertEqual(result.failure_code,'RESPONSE_OVERSIZED');self.assertIsNone(result.response_bytes)
            if failure=='nonbytes_partial':self.assertEqual(result.failure_code,'RESPONSE_INVALID');self.assertIsNone(result.response_bytes)

    def test_url_header_and_read_exception_messages_not_returned(self):
        with patch.object(m.fixed.os.environ,'get',return_value=KEY),patch.object(m.fixed,'Request',side_effect=ValueError(KEY)),patch.object(m.fixed,'build_opener') as build:
            result=self.transport(REQUEST);build.assert_not_called()
        self.assertEqual(result.failure_code,'TRANSPORT_ERROR');self.assertNotIn(KEY,repr(result))
        response=Mock();response.status=200;response.headers.get.side_effect=ValueError(KEY)
        # Use a dict-like headers object so the deliberate get failure is reached.
        class Headers:
            def get(self,*args):raise ValueError(KEY)
        response.headers=Headers();opener=MagicMock();opener.open.return_value.__enter__.return_value=response
        with patch.object(m.fixed.os.environ,'get',return_value=KEY),patch.object(m.fixed,'build_opener',return_value=opener):result=self.transport(REQUEST)
        self.assertEqual(result.failure_code,'RESPONSE_HEADERS_INVALID');self.assertNotIn(KEY,repr(result))

    def test_diagnostics_never_reserve_or_assert_authentication(self):
        result=self.invoke();self.assertEqual(result.diagnostic()['status'],'REJECT')
        for key,value in result.diagnostic().items():
            if type(value) is bool:self.assertFalse(value,key)

    def test_existing_result_only_api_keeps_integer_id_and_scalar_return(self):
        old=m.fixed.HeliusMainnetRPC();raw=b' { "jsonrpc":"2.0", "id":1,"result":42 } '
        opener=self.opener(raw)
        with patch.object(m.fixed.os.environ,'get',return_value=KEY),patch.object(m.fixed,'build_opener',return_value=opener):
            self.assertEqual(old('getSlot',[{'commitment':'finalized'}]),42)
        self.assertEqual(json.loads(self.requests[-1].data)['id'],1)
        # The older result API still refuses the new string-ID framing.
        opener=self.opener(RESPONSE)
        with patch.object(m.fixed.os.environ,'get',return_value=KEY),patch.object(m.fixed,'build_opener',return_value=opener):
            with self.assertRaises(m.fixed.CoordinatorRPCError):old('getSlot',[{'commitment':'finalized'}])


class RealHTTPFramingTests(unittest.TestCase):
    """Real stdlib HTTP parser, in-memory socket only; never opens a socket."""
    def invoke_wire(self, headers, body, reject=False):
        wire=b'HTTP/1.1 200 OK\r\n'+headers+b'\r\n'+body
        class MemorySocket:
            def makefile(self, *args):return io.BytesIO(wire)
        response=HTTPResponse(MemorySocket());response.begin()
        opener=Mock();opener.open.return_value=response
        with patch.object(response,'read',wraps=response.read) as read, \
             patch.object(m.fixed.os.environ,'get',return_value=KEY), \
             patch.object(m.fixed,'build_opener',return_value=opener):
            result=m.HeliusFinalizedSlotTransport()(REQUEST)
            if reject:read.assert_not_called()
            else:read.assert_called_once_with(m.MAX_RESPONSE_BYTES+1)
        opener.open.assert_called_once()
        args,kwargs=opener.open.call_args
        self.assertEqual(args[0].data,REQUEST)
        self.assertEqual(args[0].get_method(),'POST')
        self.assertEqual(kwargs,{'timeout':m.fixed._TIMEOUT_SECONDS})
        self.assertTrue(response.isclosed())
        self.assertNotIn(KEY,repr(result))
        return result

    def test_review_four_transfer_framing_reproductions(self):
        chunked=f'{len(RESPONSE):x}\r\n'.encode()+RESPONSE+b'\r\n0\r\n\r\n'
        cases=[
            (b'Transfer-Encoding: gzip\r\n',RESPONSE),
            (b'Transfer-Encoding: chunked\r\nContent-Length: '+str(len(RESPONSE)).encode()+b'\r\n',chunked),
            (b'Transfer-Encoding: chunked\r\nTransfer-Encoding: gzip\r\n',chunked),
            (b'Transfer-Encoding: chunked\r\n',chunked[:-2]),
        ]
        for headers,body in cases:
            with self.subTest(headers=headers,body=body):
                result=self.invoke_wire(headers,body,reject=True)
                self.assertEqual(result.failure_code,'RESPONSE_HEADERS_INVALID')
                self.assertIsNone(result.response_bytes)
                self.assertEqual(result.request_bytes,REQUEST)

    def test_all_transfer_encodings_rejected_even_valid_or_empty(self):
        for encoding in [b'',b'identity',b'chunked',b'gzip, chunked',b'CHUNKED']:
            with self.subTest(encoding=encoding):
                result=self.invoke_wire(b'tRaNsFeR-EnCoDiNg: '+encoding+b'\r\n',b'not read',reject=True)
                self.assertEqual(result.failure_code,'RESPONSE_HEADERS_INVALID')
                self.assertIsNone(result.response_bytes)

    def test_real_no_transfer_encoding_exact_length_and_eof_controls(self):
        for headers in [b'Content-Length: '+str(len(RESPONSE)).encode()+b'\r\n',b'Connection: close\r\n']:
            with self.subTest(headers=headers):
                result=self.invoke_wire(headers,RESPONSE)
                self.assertIsNone(result.failure_code)
                self.assertEqual(result.response_bytes,RESPONSE)

    def test_real_truncated_length_and_duplicate_length_controls(self):
        result=self.invoke_wire(b'Content-Length: '+str(len(RESPONSE)+1).encode()+b'\r\n',RESPONSE)
        self.assertEqual(result.failure_code,'RESPONSE_TRUNCATED')
        self.assertEqual(result.response_bytes,RESPONSE)
        result=self.invoke_wire(b'Content-Length: 1\r\nContent-Length: 2\r\n',RESPONSE,reject=True)
        self.assertEqual(result.failure_code,'RESPONSE_HEADERS_INVALID')
        self.assertIsNone(result.response_bytes)
