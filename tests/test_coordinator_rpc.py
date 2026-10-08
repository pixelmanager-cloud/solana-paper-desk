"""Local urllib handlers/mocks only: no listener, keys or real provider calls."""
import io
import json
import os
import traceback
import unittest
from dataclasses import FrozenInstanceError
from email.message import Message
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPSHandler, HTTPHandler, build_opener
from urllib.response import addinfourl
from unittest.mock import MagicMock, Mock, patch

from desk import coordinator_rpc as rpc

KEY = 'fixture-only-credential-?:/@'


class CoordinatorRPCTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'HELIUS_API_KEY': KEY})
        self.env.start(); self.addCleanup(self.env.stop)
        self.transport = rpc.HeliusMainnetRPC()
        self.requests = []

    def opener(self, body=None, status=200, headers=None):
        if body is None: body = b'{"jsonrpc":"2.0","id":1,"result":42}'
        requests = self.requests
        class LocalHTTPS(HTTPSHandler):
            def https_open(handler, request):
                requests.append(request)
                values = Message()
                for name, value in (headers or {}).items(): values[name] = value
                response = addinfourl(io.BytesIO(body), values, request.full_url, status)
                response.msg = 'fixture response'
                return response
        class LocalHTTP(HTTPHandler):
            def http_open(handler, request):
                requests.append(request)
                raise AssertionError('Redirect/fallback reached HTTP')
        return build_opener(rpc._NoRedirect(), LocalHTTPS(), LocalHTTP())

    def invoke(self, body=None, status=200, headers=None, method='getSlot', params=None):
        with patch.object(rpc, 'build_opener', return_value=self.opener(body, status, headers)):
            return self.transport(method, [{'commitment': 'finalized'}] if params is None else params)

    def rejected(self, callback):
        with self.assertRaises(rpc.CoordinatorRPCError) as raised: callback()
        self.assertNotIn(KEY, str(raised.exception))
        self.assertNotIn(KEY, ''.join(traceback.format_exception(raised.exception)))
        self.assertTrue(raised.exception.__suppress_context__)
        return raised.exception

    def test_one_post_fixed_endpoint_timeout_and_rpc_identity(self):
        opener = self.opener()
        with patch.object(rpc, 'build_opener', return_value=opener), patch.object(opener, 'open', wraps=opener.open) as open_:
            self.assertEqual(self.transport('getSlot', [{'commitment': 'finalized'}]), 42)
        self.assertEqual(open_.call_count, 1)
        self.assertEqual(open_.call_args.kwargs, {'timeout': 15})
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]; url = urlsplit(request.full_url)
        self.assertEqual((url.scheme, url.netloc, url.path, url.fragment), ('https', 'mainnet.helius-rpc.com', '/', ''))
        self.assertEqual(parse_qs(url.query), {'api-key': [KEY]})
        self.assertEqual(request.method, 'POST')
        self.assertEqual(json.loads(request.data), {'jsonrpc': '2.0', 'id': 1, 'method': 'getSlot', 'params': [{'commitment': 'finalized'}]})

    def test_all_redirect_codes_rejected_without_second_request(self):
        for status in (301, 302, 303, 307, 308):
            for target in ('https://other.invalid/?api-key=' + KEY, 'http://other.invalid/', '/other'):
                with self.subTest(status=status, target=target):
                    self.requests.clear()
                    self.rejected(lambda: self.invoke(b'provider key ' + KEY.encode(), status, {'Location': target}))
                    self.assertEqual(len(self.requests), 1)

    def test_http_errors_and_non_success_never_retry(self):
        for status in (201, 204, 300, 304, 400, 401, 429, 500, 503):
            with self.subTest(status=status):
                self.requests.clear()
                self.rejected(lambda: self.invoke(KEY.encode(), status))
                self.assertEqual(len(self.requests), 1)

    def test_open_failures_no_retry_no_secret_in_error_chain(self):
        for error in (URLError(KEY), HTTPError('https://key.invalid/' + KEY, 403, KEY, {}, None),
                      TimeoutError(KEY), OSError(KEY), ValueError('bad URL: ' + KEY), RuntimeError(KEY)):
            with self.subTest(error=type(error).__name__):
                opener = Mock(); opener.open.side_effect = error
                with patch.object(rpc, 'build_opener', return_value=opener):
                    self.rejected(lambda: self.transport('getGenesisHash', []))
                self.assertEqual(opener.open.call_count, 1)

    def test_malformed_url_and_response_header_errors_are_sanitized(self):
        with patch.object(rpc, 'Request', side_effect=ValueError('bad URL ' + KEY)), patch.object(rpc, 'build_opener') as build:
            self.rejected(lambda: self.transport('getGenesisHash', []))
            build.assert_not_called()
        response = Mock(); response.status = 200
        response.headers.get.side_effect = ValueError(KEY)
        opener = MagicMock(); opener.open.return_value.__enter__.return_value = response
        with patch.object(rpc, 'build_opener', return_value=opener):
            self.rejected(lambda: self.transport('getGenesisHash', []))
        self.assertEqual(opener.open.call_count, 1)

    def test_envelope_version_id_result_and_error_shapes(self):
        invalid = [[], {}, {'jsonrpc': '1.0', 'id': 1, 'result': 1},
                   {'jsonrpc': '2.0', 'id': True, 'result': 1}, {'jsonrpc': '2.0', 'id': '1', 'result': 1},
                   {'jsonrpc': '2.0', 'id': 2, 'result': 1}, {'jsonrpc': '2.0', 'result': 1},
                   {'jsonrpc': '2.0', 'id': 1}, {'jsonrpc': '2.0', 'id': 1, 'result': 1, 'error': None},
                   {'jsonrpc': '2.0', 'id': 1, 'result': 1, 'extra': KEY}]
        for error in (None, KEY, [], {}, {'code': True, 'message': KEY}, {'code': -1, 'message': {}},
                      {'code': -1, 'message': KEY, 'extra': 1}, {'code': -1, 'message': KEY, 'data': {'key': KEY}}):
            invalid.append({'jsonrpc': '2.0', 'id': 1, 'error': error})
        for payload in invalid:
            with self.subTest(payload=payload):
                self.requests.clear()
                self.rejected(lambda: self.invoke(json.dumps(payload).encode()))
                self.assertEqual(len(self.requests), 1)
        # Null is a valid JSON-RPC result (e.g. unavailable block time), not an
        # attestation. The future bridge must reject unavailable required data.
        self.assertIsNone(self.invoke(b'{"jsonrpc":"2.0","id":1,"result":null}'))

    def test_invalid_json_utf8_duplicates_and_constants_rejected(self):
        for body in (KEY.encode(), b'\xff', b'{', b'{"jsonrpc":"2.0","id":1,"id":1,"result":0}',
                     b'{"jsonrpc":"2.0","id":1,"result":NaN}', b'{"jsonrpc":"2.0","id":1,"result":Infinity}',
                     b'{"jsonrpc":"2.0","id":1,"result":1e999}',
                     b'{"jsonrpc":"2.0","id":1,"result":{"key":1,"key":2}}'):
            with self.subTest(body=body): self.rejected(lambda: self.invoke(body))

    def test_response_read_bound_oversize_lengths_encoding_and_truncation(self):
        self.rejected(lambda: self.invoke(b'x' * (rpc._MAX_RESPONSE_BYTES + 1)))
        for headers in ({'Content-Length': str(rpc._MAX_RESPONSE_BYTES + 1)}, {'Content-Length': '-1'},
                        {'Content-Length': KEY}, {'Content-Length': '999'}, {'Content-Encoding': 'gzip'}):
            with self.subTest(headers=headers): self.rejected(lambda: self.invoke(headers=headers))
        response = Mock(); response.status = 200; response.headers = {}
        response.read.return_value = b'{"jsonrpc":"2.0","id":1,"result":0}'
        opener = MagicMock(); opener.open.return_value.__enter__.return_value = response
        with patch.object(rpc, 'build_opener', return_value=opener): self.assertEqual(self.transport('getGenesisHash', []), 0)
        response.read.assert_called_once_with(rpc._MAX_RESPONSE_BYTES + 1)

    def test_allowlist_and_bound_params_fail_before_key_or_network(self):
        invalid = [('sendTransaction', []), ('simulateTransaction', []), ('getAccountInfo', []),
                   ('getGenesisHash', ['https://candidate.invalid']), ('getSlot', []),
                   ('getBlockTime', [True]), ('getBlockTime', [-1]), ('getMultipleAccounts', [[], {}]),
                   ('getMultipleAccounts', [['1' * 32] * 101, {'encoding': 'base64', 'commitment': 'finalized'}]),
                   ('getMultipleAccounts', [['1' * 32], {'encoding': 'base64', 'commitment': 'confirmed'}]),
                   ('getMultipleAccounts', [['1' * 32], {'encoding': 'base64', 'commitment': 'finalized', 'source_id': KEY}])]
        with patch.object(rpc.os.environ, 'get', side_effect=AssertionError('Credential accessed')) as credential, patch.object(rpc, 'build_opener') as build:
            for method, params in invalid:
                with self.subTest(method=method, params=params): self.rejected(lambda: self.transport(method, params))
            credential.assert_not_called(); build.assert_not_called()

    def test_all_four_methods_and_local_invocation_credential_rotation(self):
        cases = [('getGenesisHash', []), ('getSlot', [{'commitment': 'finalized'}]), ('getBlockTime', [42]),
                 ('getMultipleAccounts', [['1' * 32], {'encoding': 'base64', 'commitment': 'finalized', 'minContextSlot': 42}])]
        for method, params in cases:
            self.assertEqual(self.invoke(method=method, params=params), 42)
        with patch.dict(os.environ, {'HELIUS_API_KEY': 'rotated-fixture-only'}): self.invoke()
        self.assertEqual(parse_qs(urlsplit(self.requests[-1].full_url).query), {'api-key': ['rotated-fixture-only']})

    def test_missing_bad_credentials_do_not_open_or_store_key(self):
        for value in ('', 'contains space', 'key\ninvalid', 'x' * 513):
            with self.subTest(value=value), patch.dict(os.environ, {'HELIUS_API_KEY': value}), patch.object(rpc, 'build_opener') as build:
                self.rejected(lambda: self.transport('getGenesisHash', [])); build.assert_not_called()
        with patch.dict(os.environ, {}, clear=True), patch.object(rpc, 'build_opener') as build:
            self.rejected(lambda: self.transport('getGenesisHash', [])); build.assert_not_called()
        self.assertNotIn(KEY, repr(self.transport))
        self.assertNotIn(KEY, repr(self.transport.source))

    def test_fixed_source_descriptor_not_candidate_supplied_or_mutable(self):
        with patch.object(rpc.os.environ, 'get') as credential, patch.object(rpc, 'build_opener') as build:
            transport = rpc.HeliusMainnetRPC()
            credential.assert_not_called(); build.assert_not_called()
        self.assertEqual(transport.source.source_id, 'helius-mainnet-single-request-v1')
        self.assertEqual(transport.source.source_kind, 'coordinator_capture')
        self.assertEqual(transport.source, self.transport.source)
        with self.assertRaises(TypeError): rpc.HeliusMainnetRPC(source='candidate')
        with self.assertRaises(AttributeError): transport.source = 'candidate'
        with self.assertRaises(FrozenInstanceError): transport.source.source_id = 'candidate'

    def test_read_failure_is_single_attempt_and_closes_response(self):
        response = Mock(); response.status = 200; response.headers = {}
        response.read.side_effect = TimeoutError(KEY)
        opener = MagicMock(); opener.open.return_value.__enter__.return_value = response
        with patch.object(rpc, 'build_opener', return_value=opener):
            self.rejected(lambda: self.transport('getBlockTime', [42]))
        self.assertEqual(opener.open.call_count, 1)
        response.read.assert_called_once_with(rpc._MAX_RESPONSE_BYTES + 1)
        self.assertEqual(opener.open.return_value.__exit__.call_count, 1)

    def test_exact_response_byte_ceiling_is_supported_and_body_budget_is_authoritative(self):
        prefix = b'{"jsonrpc":"2.0","id":1,"result":42}'
        raw = prefix + b' ' * (rpc._MAX_RESPONSE_BYTES - len(prefix))
        self.assertEqual(self.invoke(raw, headers={'Content-Length': str(len(raw))}), 42)
        self.rejected(lambda: self.invoke(raw + b' ', headers={'Content-Length': '1'}))
        response = Mock(); response.status = 200
        response.headers = {'Content-Length': str(rpc._MAX_RESPONSE_BYTES + 1)}
        opener = MagicMock(); opener.open.return_value.__enter__.return_value = response
        with patch.object(rpc, 'build_opener', return_value=opener):
            self.rejected(lambda: self.transport('getGenesisHash', []))
        response.read.assert_not_called()
