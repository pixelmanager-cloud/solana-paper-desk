"""Synthetic wire fixtures and real in-memory HTTPResponse; no socket/key reads."""
import base64
import copy
from dataclasses import FrozenInstanceError
from http.client import HTTPResponse, IncompleteRead
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from desk import original_byte_read_transport as m
from desk.history import collect_history
from desk.security import TOKEN_PROGRAM, TOKEN_2022, base58

KEY = 'synthetic-transport-only-key-?:/@'
MINT = base58(bytes([1]) * 32)
OTHER = base58(bytes([2]) * 32)
ACCOUNT = {'owner': TOKEN_PROGRAM, 'executable': False,
           'data': [base64.b64encode(bytes(82)).decode(), 'base64'],
           'lamports': 123, 'rentEpoch': 2**64-1, 'space': 82}
HISTORY = [MINT, {'transactionDetails': 'full', 'sortOrder': 'asc', 'limit': 100,
                 'commitment': 'finalized', 'encoding': 'jsonParsed',
                 'maxSupportedTransactionVersion': 1,
                 'filters': {'slot': {'gte': 0, 'lt': 21}, 'status': 'any', 'tokenAccounts': 'none'}}]
SHAPES = {
    'getSlot': ([{'commitment': 'finalized'}], 20),
    'getGenesisHash': ([], base58(bytes([4]) * 32)),
    'getBlockTime': ([21], 100),
    'getAccountInfo': ([MINT, {'encoding': 'base64', 'commitment': 'confirmed'}],
                       {'context': {'slot': 19}, 'value': ACCOUNT}),
    'getMultipleAccounts': ([[MINT, OTHER], {'encoding': 'base64', 'commitment': 'finalized',
                                          'minContextSlot': 20}],
                            {'context': {'slot': 21, 'apiVersion': 'fixture-v1'}, 'value': [ACCOUNT, None]}),
    'getTransactionsForAddress': (HISTORY, {'data': [], 'paginationToken': None}),
}


def request(method, params=None, identity='read-1'):
    return json.dumps({'jsonrpc': '2.0', 'id': identity, 'method': method,
                       'params': copy.deepcopy(SHAPES[method][0] if params is None else params)}).encode()


def response(result, identity='read-1'):
    return json.dumps({'jsonrpc': '2.0', 'id': identity, 'result': result}).encode()


class ReadTransportTests(unittest.TestCase):
    def exchange(self, sent, body, headers=b'', status=200, reject_read=False):
        # HTTPResponse's real header/body parser sees the entire synthetic wire.
        wire = f'HTTP/1.1 {status} Fixture\r\n'.encode() + headers + b'\r\n' + body
        class MemorySocket:
            def makefile(self, *args): return io.BytesIO(wire)
        received = HTTPResponse(MemorySocket()); received.begin()
        opener = Mock(); opener.open.return_value = received
        with patch.object(received, 'read', wraps=received.read) as read, \
             patch.object(m.slot.fixed.os.environ, 'get', return_value=KEY) as credential, \
             patch.object(m.slot.fixed, 'build_opener', return_value=opener) as builder:
            result = m.HeliusOriginalByteReadTransport()(sent)
            credential.assert_called_once_with('HELIUS_API_KEY')
            builder.assert_called_once()
            self.assertIsInstance(builder.call_args.args[0], m.slot.fixed._NoRedirect)
            if reject_read: read.assert_not_called()
            else:
                cap = (m.MAX_HISTORY_RESPONSE_BYTES if m.validate_read_request(sent)['method'] ==
                       'getTransactionsForAddress' else m.MAX_RESPONSE_BYTES)
                read.assert_called_once_with(cap + 1)
        opener.open.assert_called_once()
        req = opener.open.call_args.args[0]
        self.assertEqual(opener.open.call_args.kwargs, {'timeout': 15})
        self.assertEqual(req.data, sent); self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(urlsplit(req.full_url).scheme, 'https')
        self.assertEqual(urlsplit(req.full_url).hostname, 'mainnet.helius-rpc.com')
        self.assertEqual(parse_qs(urlsplit(req.full_url).query), {'api-key': [KEY]})
        self.assertTrue(received.isclosed())
        self.assertEqual(result.request_bytes, sent)
        self.assertNotIn(KEY, repr(result)); self.assertNotIn(KEY, str(result.diagnostic()))
        self.assertEqual(result.diagnostic()['status'], 'REJECT')
        for flag in ('budget_reserved', 'intent_authenticated', 'source_authenticated',
                     'finality_authenticated', 'entry_allowed'):
            self.assertIs(result.diagnostic()[flag], False)
        return result

    def invalid_request(self, sent):
        with patch.object(m.slot.fixed.os.environ, 'get') as credential, \
             patch.object(m.slot.fixed, 'build_opener') as opener:
            with self.assertRaisesRegex(m.ReadRequestInvalid, '^Original-byte read request invalid$'):
                m.HeliusOriginalByteReadTransport()(sent)
            credential.assert_not_called(); opener.assert_not_called()

    def test_six_exact_methods_preserve_whitespace_escapes_and_string_id(self):
        for method, (params, result) in SHAPES.items():
            with self.subTest(method=method):
                sent = b' \n' + request(method).replace(b'read-1', b'read-\\u0031') + b'\t '
                body = b'\n ' + response(result) + b' \r\n'
                captured = self.exchange(sent, body)
                self.assertIsNone(captured.failure_code); self.assertEqual(captured.response_bytes, body)
                with self.assertRaises(FrozenInstanceError): captured.failure_code = 'changed'

    def test_constructor_has_no_io_or_caller_options(self):
        with patch.object(m.slot.fixed.os.environ, 'get') as key, \
             patch.object(m.slot.fixed, 'build_opener') as opener:
            m.HeliusOriginalByteReadTransport(); key.assert_not_called(); opener.assert_not_called()
        with self.assertRaises(TypeError): m.HeliusOriginalByteReadTransport(endpoint='https://example.invalid')

    def test_unknown_methods_and_envelope_attacks_before_credentials(self):
        template = json.loads(request('getGenesisHash'))
        for change in ({'method': 'getTransaction'}, {'method': 'sendTransaction'}, {'method': 'getTokenAccountsByOwner'},
                       {'method': True}, {'id': 1}, {'id': True}, {'id': None}, {'id': ''},
                       {'jsonrpc': 2}, {'params': None}, {'url': 'https://example.invalid'}, {'id': '\ud800'}):
            with self.subTest(change=change): self.invalid_request(json.dumps(template | change).encode())
        for sent in (b'['+request('getGenesisHash')+b']', request('getGenesisHash')+b'{}',
                     request('getGenesisHash').replace(b'"id":', b'"id":"duplicate", "id":'),
                     b'\xef\xbb\xbf'+request('getGenesisHash'), bytearray(request('getGenesisHash')),
                     request('getGenesisHash')+bytes([255])):
            with self.subTest(sent=sent): self.invalid_request(sent)

    def test_utf8_id_and_request_caps(self):
        self.assertEqual(m.validate_read_request(request('getGenesisHash', identity='é'*64))['id'], 'é'*64)
        self.invalid_request(request('getGenesisHash', identity='é'*65))
        self.invalid_request(b' '*m.MAX_REQUEST_BYTES+request('getGenesisHash'))
        for method in ('getSlot', 'getGenesisHash', 'getAccountInfo', 'getBlockTime'):
            self.invalid_request(request(method)+b' '*m.slot.MAX_REQUEST_BYTES)

    def test_mint_request_exact_legacy_check_shape_only(self):
        for params in ([MINT], [MINT, {'encoding': 'base64', 'commitment': 'finalized'}],
                       [MINT, {'encoding': 'jsonParsed', 'commitment': 'confirmed'}],
                       [MINT, {'encoding': 'base64', 'commitment': 'confirmed', 'dataSlice': {'length': 82}}],
                       ['invalid', SHAPES['getAccountInfo'][0][1]], [True, SHAPES['getAccountInfo'][0][1]]):
            with self.subTest(params=params): self.invalid_request(request('getAccountInfo', params))

    def test_genesis_and_actual_clock_params_no_bool_overflow_or_suffix(self):
        self.invalid_request(request('getGenesisHash', [{}]))
        for value in (True, -1, 2**63, 1.0, '21', None):
            self.invalid_request(request('getBlockTime', [value]))
        self.invalid_request(request('getBlockTime', [21, {'commitment': 'finalized'}]))

    def test_unique_ordered_accounts_100_bound_and_8k_wire(self):
        keys = [base58(i.to_bytes(32, 'big')) for i in range(1, 102)]
        for count in (1, 100):
            params = [keys[:count], {'encoding': 'base64', 'commitment': 'finalized', 'minContextSlot': 0}]
            sent = request('getMultipleAccounts', params)
            self.assertEqual(m.validate_read_request(sent)['params'][0], keys[:count])
            result = {'context': {'slot': 1}, 'value': [None]*count}
            self.assertIsNone(self.exchange(sent, response(result)).failure_code)
        for bad in ([], keys, [MINT, MINT], ['invalid'], [True]):
            self.invalid_request(request('getMultipleAccounts', [bad, SHAPES['getMultipleAccounts'][0][1]]))
        sent = request('getMultipleAccounts')
        self.assertEqual(m.validate_read_request(sent+b' '*(m.MAX_REQUEST_BYTES-len(sent)))['method'], 'getMultipleAccounts')
        self.invalid_request(sent+b' '*(m.MAX_REQUEST_BYTES-len(sent)+1))

    def test_bank_options_only_accepted_floor_and_unfloored_form(self):
        self.assertEqual(m.validate_read_request(request('getMultipleAccounts', [[MINT],
            {'encoding': 'base64', 'commitment': 'finalized'}]))['params'][0], [MINT])
        for options in ({'encoding': 'jsonParsed', 'commitment': 'finalized'},
                        {'encoding': 'base64', 'commitment': 'confirmed'},
                        {'encoding': 'base64', 'commitment': 'finalized', 'dataSlice': {}},
                        {'encoding': 'base64', 'commitment': 'finalized', 'minContextSlot': True},
                        {'encoding': 'base64', 'commitment': 'finalized', 'minContextSlot': -1},
                        {'encoding': 'base64', 'commitment': 'finalized', 'minContextSlot': 2**63}):
            self.invalid_request(request('getMultipleAccounts', [[MINT], options]))

    def test_actual_production_history_shapes_and_continuation_cursor(self):
        # Pure collector generates its exact accepted grammar; no durable/runtime wiring.
        calls = []
        def fixture(method, params):
            calls.append(m.validate_read_request(request(method, params)))
            return {'data': [], 'paginationToken': 'cursor-1'} if len(calls)==1 else {'data': []}
        collect_history(MINT, 0, 101, fixture, max_pages=2, token_accounts='none', slot_range={'gte': 0, 'lt': 21})
        self.assertEqual(len(calls), 2)
        self.assertNotIn('paginationToken', calls[0]['params'][1])
        self.assertEqual(calls[1]['params'][1]['paginationToken'], 'cursor-1')
        self.assertEqual(calls[0]['params'][1], HISTORY[1])

    def test_history_rejects_broad_filters_and_pagination_overflow(self):
        mutations = [({'limit': 101}), ({'limit': True}), ({'maxSupportedTransactionVersion': True}),
                     ({'maxSupportedTransactionVersion': 0}), ({'encoding': 'json'}),
                     ({'commitment': 'confirmed'}), ({'transactionDetails': 'signatures'}),
                     ({'sortOrder': 'desc'}), ({'paginationToken': ''}), ({'paginationToken': True}),
                     ({'paginationToken': 'é'*513}), ({'filters': {'slot': {'gte': 1, 'lt': 21}, 'status': 'any', 'tokenAccounts': 'none'}}),
                     ({'filters': {'blockTime': {'gte': 0, 'lt': 101}, 'status': 'any', 'tokenAccounts': 'none'}}),
                     ({'filters': {'slot': {'gte': 0, 'lt': 21}, 'status': 'any', 'tokenAccounts': 'all'}}),
                     ({'filters': {'slot': {'gte': False, 'lt': 21}, 'status': 'any', 'tokenAccounts': 'none'}}),
                     ({'filters': {'slot': {'gte': 0, 'lt': 0}, 'status': 'any', 'tokenAccounts': 'none'}}),
                     ({'filters': {'slot': {'gte': 0, 'lt': 2**64+1}, 'status': 'any', 'tokenAccounts': 'none'}})]
        for change in mutations:
            with self.subTest(change=change): self.invalid_request(request('getTransactionsForAddress', [MINT, HISTORY[1] | change]))
        for cursor in ('é'*512, 'opaque\\cursor\x00'):
            self.assertEqual(m.validate_read_request(request('getTransactionsForAddress', [MINT, HISTORY[1] | {'paginationToken': cursor}]))['params'][1]['paginationToken'], cursor)

    def test_strict_response_envelopes_ids_duplicates_overflows(self):
        for method, (_params, result) in SHAPES.items():
            body = response(result)
            attacks = [response(result, True), response(result, 'other'), body+b'{}',
                       body.replace(b'"id":', b'"id":"duplicate", "id":'),
                       body.replace(b'"jsonrpc": "2.0"', b'"jsonrpc":2'),
                       body[:-1]+b',"error":{"code":1,"message":"bad"}}',
                       b'\xff', body.replace(b'"result":', b'"result":NaN,"unused":'),
                       body[:-1]+b',"overflow":184467440737095516160}']
            for raw in attacks:
                with self.subTest(method=method, raw=raw):
                    captured = self.exchange(request(method), raw)
                    self.assertEqual(captured.failure_code, 'RESPONSE_INVALID')
                    self.assertEqual(captured.response_bytes, raw)

    def test_rpc_errors_original_bytes_not_redacted_into_evidence(self):
        for method in SHAPES:
            raw = json.dumps({'jsonrpc': '2.0', 'id': 'read-1', 'error': {'code': -32000,
                'message': KEY, 'data': {'url': KEY}}}).encode()
            result = self.exchange(request(method), raw)
            self.assertEqual(result.failure_code, 'RPC_ERROR'); self.assertEqual(result.response_bytes, raw)
        for error in ({'code': True, 'message': 'bad'}, {'code': 2**63, 'message': 'bad'},
                      {'code': 1, 'message': None}, {'code': 1, 'message': 'bad', 'extra': 1}):
            raw = json.dumps({'jsonrpc': '2.0', 'id': 'read-1', 'error': error}).encode()
            self.assertEqual(self.exchange(request('getGenesisHash'), raw).failure_code, 'RESPONSE_INVALID')

    def test_genesis_clock_result_types_and_null_time_unavailable(self):
        for method, values in [('getGenesisHash', ['bad', True, None]),
                               ('getBlockTime', [None, True, -1, 1.5, 2**63])]:
            for value in values:
                with self.subTest(method=method, value=value):
                    raw = response(value); result = self.exchange(request(method), raw)
                    self.assertEqual(result.failure_code, 'RESPONSE_INVALID'); self.assertEqual(result.response_bytes, raw)

    def test_bank_actual_slot_can_advance_but_not_regress_or_change_coverage(self):
        good = copy.deepcopy(SHAPES['getMultipleAccounts'][1])
        attacks = [good | {'context': {'slot': 19}}, good | {'context': {'slot': True}},
                   good | {'context': {'slot': 2**63}}, good | {'context': {'slot': 21, 'commitment': 'finalized'}},
                   good | {'context': {'slot': 21, 'apiVersion': ''}}, good | {'value': [ACCOUNT]},
                   good | {'value': [ACCOUNT, None, None]}, good | {'value': None}, good | {'extra': 1}]
        for bad in attacks:
            with self.subTest(bad=bad):
                raw=response(bad); result=self.exchange(request('getMultipleAccounts'), raw)
                self.assertEqual(result.failure_code, 'RESPONSE_INVALID'); self.assertEqual(result.response_bytes, raw)

    def test_account_null_and_unsupported_owner_preserved_without_token_policy(self):
        for value in (None, ACCOUNT | {'owner': TOKEN_2022}, ACCOUNT | {'owner': OTHER}):
            raw=response({'context': {'slot': 19}, 'value': value})
            result=self.exchange(request('getAccountInfo'), raw)
            self.assertIsNone(result.failure_code); self.assertEqual(result.response_bytes, raw)

    def test_malformed_account_fields_retained_invalid_not_normalized(self):
        bad_values = [ACCOUNT | {'data': ['AA==junk', 'base64']}, ACCOUNT | {'data': ['AB==', 'base64']},
                      ACCOUNT | {'data': ['AAAA', 'jsonParsed']}, ACCOUNT | {'owner': 'bad'},
                      ACCOUNT | {'executable': 0}, ACCOUNT | {'lamports': True},
                      ACCOUNT | {'rentEpoch': 2**64}, ACCOUNT | {'space': 1}, ACCOUNT | {'extra': 1}, {}]
        for bad in bad_values:
            with self.subTest(bad=bad):
                raw=response({'context': {'slot': 19}, 'value': bad})
                result=self.exchange(request('getAccountInfo'), raw)
                self.assertEqual(result.failure_code, 'RESPONSE_INVALID'); self.assertEqual(result.response_bytes, raw)

    def test_saved_synthetic_raw_history_preserved_and_no_row_semantics_inferred(self):
        saved=json.loads((Path(__file__).parents[1]/'fixtures/cloud_history/synthetic.json').read_text())
        # Established synthetic raw records from existing fixture, no fabricated CPI witnesses.
        rows=saved['records']
        raw=response({'data': rows})
        result=self.exchange(request('getTransactionsForAddress'), raw)
        self.assertIsNone(result.failure_code); self.assertEqual(result.response_bytes, raw)
        # Incomplete transaction dictionaries must remain downstream decode gaps.
        raw=response({'data': [{}], 'paginationToken': 'same'})
        self.assertIsNone(self.exchange(request('getTransactionsForAddress'), raw).failure_code)

    def test_history_page_and_cursor_shapes_bounded(self):
        for bad in ({'data': [{}]*101}, {'data': [None]}, {'data': {}},
                    {'data': [], 'paginationToken': ''}, {'data': [], 'paginationToken': True},
                    {'data': [], 'paginationToken': 'é'*513}, {'data': [], 'complete': True}):
            with self.subTest(bad=bad):
                raw=response(bad); result=self.exchange(request('getTransactionsForAddress'), raw)
                self.assertEqual(result.failure_code, 'RESPONSE_INVALID'); self.assertEqual(result.response_bytes, raw)

    def test_body_cap_boundaries_small_bank_large_history(self):
        for method in ('getMultipleAccounts', 'getTransactionsForAddress'):
            cap=m.MAX_HISTORY_RESPONSE_BYTES if method=='getTransactionsForAddress' else m.MAX_RESPONSE_BYTES
            body=response(SHAPES[method][1]); at_limit=body+b' '*(cap-len(body))
            self.assertIsNone(self.exchange(request(method), at_limit).failure_code)
            result=self.exchange(request(method), at_limit+b' ')
            self.assertEqual(result.failure_code,'RESPONSE_OVERSIZED'); self.assertIsNone(result.response_bytes)
            result=self.exchange(request(method),b'not read', b'Content-Length: '+str(cap+1).encode()+b'\r\n',reject_read=True)
            self.assertEqual(result.failure_code,'RESPONSE_OVERSIZED'); self.assertIsNone(result.response_bytes)

    def test_json_depth_node_and_utf8_bounds_preserve_original_failed_body(self):
        deep={}
        for _ in range(70): deep={'nested': deep}
        for bad in ({'data': [deep]}, {'data': [{'array': [None]*m.MAX_JSON_NODES}]},
                    {'data': [{'surrogate': '\ud800'}]}):
            raw=response(bad); result=self.exchange(request('getTransactionsForAddress'),raw)
            self.assertEqual(result.failure_code,'RESPONSE_INVALID'); self.assertEqual(result.response_bytes,raw)

    def test_all_methods_reject_transfer_encoding_before_body_read(self):
        for method in SHAPES:
            for header in (b'Transfer-Encoding: gzip\r\n', b'Transfer-Encoding: chunked\r\nContent-Length: 1\r\n',
                           b'Transfer-Encoding: chunked\r\nTransfer-Encoding: gzip\r\n',
                           b'Transfer-Encoding: chunked\r\n', b'Transfer-Encoding: \r\n'):
                result=self.exchange(request(method),b'1\r\na\r\n0\r\n',header,reject_read=True)
                self.assertEqual(result.failure_code,'RESPONSE_HEADERS_INVALID'); self.assertIsNone(result.response_bytes)

    def test_headers_status_and_truncated_response_one_post(self):
        for method in ('getMultipleAccounts','getTransactionsForAddress'):
            raw=response(SHAPES[method][1])
            for header in (b'Content-Encoding: gzip\r\n', b'Content-Length: 1\r\nContent-Length: 2\r\n'):
                self.assertEqual(self.exchange(request(method),raw,header,reject_read=True).failure_code,'RESPONSE_HEADERS_INVALID')
            result=self.exchange(request(method),raw,b'Content-Length: '+str(len(raw)+1).encode()+b'\r\n')
            self.assertEqual(result.failure_code,'RESPONSE_TRUNCATED'); self.assertEqual(result.response_bytes,raw)
            for status in (301,302,303,307,308,500):
                self.assertEqual(self.exchange(request(method),raw,status=status,reject_read=True).failure_code,'HTTP_REJECTED')

    def test_credential_transport_error_and_partial_redaction(self):
        for method in ('getAccountInfo','getTransactionsForAddress','getMultipleAccounts'):
            sent=request(method)
            with patch.object(m.slot.fixed.os.environ,'get',return_value=None), patch.object(m.slot.fixed,'build_opener') as builder:
                result=m.HeliusOriginalByteReadTransport()(sent)
                self.assertEqual(result.failure_code,'CREDENTIAL_UNAVAILABLE'); builder.assert_not_called()
            for error in (URLError(KEY), HTTPError('https://'+KEY,503,KEY,{},io.BytesIO(KEY.encode()))):
                opener=Mock(); opener.open.side_effect=error
                with patch.object(m.slot.fixed.os.environ,'get',return_value=KEY), patch.object(m.slot.fixed,'build_opener',return_value=opener):
                    result=m.HeliusOriginalByteReadTransport()(sent)
                opener.open.assert_called_once(); self.assertNotIn(KEY,repr(result)); self.assertIsNone(result.response_bytes)
                self.assertEqual(result.failure_code,'HTTP_REJECTED' if isinstance(error,HTTPError) else 'TRANSPORT_ERROR')
        received=Mock(); received.__enter__=Mock(return_value=received); received.__exit__=Mock()
        received.status=200; received.headers={}; received.read.side_effect=IncompleteRead(b'original-partial')
        opener=Mock(); opener.open.return_value=received
        with patch.object(m.slot.fixed.os.environ,'get',return_value=KEY), patch.object(m.slot.fixed,'build_opener',return_value=opener):
            result=m.HeliusOriginalByteReadTransport()(request('getTransactionsForAddress'))
        self.assertEqual(result.failure_code,'RESPONSE_TRUNCATED'); self.assertEqual(result.response_bytes,b'original-partial')
        received.read.assert_called_once_with(m.MAX_HISTORY_RESPONSE_BYTES+1)

    def test_slot_transport_compatibility_same_exchange_and_rejections(self):
        from tests.test_original_byte_slot_transport import REQUEST, RESPONSE
        result=self.exchange(REQUEST,RESPONSE)
        self.assertIsNone(result.failure_code); self.assertEqual(result.response_bytes,RESPONSE)
        self.invalid_request(request('getSlot',[{'commitment':'confirmed'}]))

