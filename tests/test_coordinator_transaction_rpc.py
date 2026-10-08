"""Raw getTransaction framing only; synthetic signatures and local handlers."""
import copy
import json
import unittest
from dataclasses import asdict
from unittest.mock import Mock, patch
from urllib.error import URLError

from desk import coordinator_rpc as rpc
from desk.pool_receipt_ledger import ApprovedSource
from desk.security import base58
from tests import test_coordinator_rpc as fixtures


class CoordinatorTransactionRPCTests(unittest.TestCase):
    setUp = fixtures.CoordinatorRPCTests.setUp
    opener = fixtures.CoordinatorRPCTests.opener
    invoke = fixtures.CoordinatorRPCTests.invoke
    rejected = fixtures.CoordinatorRPCTests.rejected

    def params(self, signature=None):
        return [base58(bytes(range(64))) if signature is None else signature,
                {'encoding': 'json', 'commitment': 'finalized', 'maxSupportedTransactionVersion': 0}]

    def transaction(self, result, params=None):
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result}).encode()
        return self.invoke(body=body, method='getTransaction', params=self.params() if params is None else params)

    def invalid(self, params):
        with patch.object(rpc.os.environ, 'get', side_effect=AssertionError('Credential read')) as key, \
                patch.object(rpc, 'build_opener') as opener:
            self.rejected(lambda: self.transport('getTransaction', params))
            key.assert_not_called(); opener.assert_not_called()

    def test_exact_finalized_request_single_post_and_raw_result(self):
        params = self.params()
        raw = {'slot': 123, 'blockTime': 456, 'version': 0,
               'transaction': {'signatures': [params[0]], 'message': {'accountKeys': [], 'instructions': []}},
               'meta': {'err': None, 'preBalances': [], 'postBalances': []}}
        original = copy.deepcopy(params)
        self.assertEqual(self.transaction(raw, params), raw)
        self.assertEqual(params, original)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.method, 'POST')
        self.assertEqual(json.loads(request.data), {'jsonrpc': '2.0', 'id': 1,
                         'method': 'getTransaction', 'params': original})

    def test_null_failed_cross_signature_and_opaque_result_are_unchanged(self):
        for raw in (None, {'slot': 9, 'meta': {'err': {'InstructionError': [0, 'Custom']}}},
                    {'transaction': {'signatures': ['different']}, 'mint': 'unbound'},
                    {'unsupported_fields': [1, {'observation': 'opaque'}]}):
            with self.subTest(raw=raw): self.assertEqual(self.transaction(raw), raw)
        # Transport does not validate transaction success, signer, mint or state.
        self.assertFalse(hasattr(self.transport, 'eligible_for_trading'))

    def test_canonical_sixty_four_byte_signature_boundaries(self):
        for raw in (bytes(64), bytes([255]) * 64, bytes(range(64)), bytes(63) + b'\1'):
            with self.subTest(raw=raw.hex()):
                self.assertEqual(self.transaction({'raw': True}, self.params(base58(raw))), {'raw': True})

    def test_invalid_signature_alphabet_size_and_types_fail_before_io(self):
        class StringSubclass(str): pass
        valid = self.params()[0]
        invalid = [None, True, 0, valid.encode(), StringSubclass(valid), '', '1' * 63, '1' * 65,
                   base58(bytes([255]) * 63), base58(bytes([255]) * 65), 'z' * 88,
                   '1' + valid, base58(bytes([255]) * 64) + '1', valid[:20] + '0' + valid[21:],
                   'O' * 87, 'I' * 87, 'l' * 87, '\u00e9' * 87, ' ' + valid, valid + '\n', '1' * 10000]
        for signature in invalid:
            with self.subTest(signature=signature): self.invalid([signature, self.params()[1]])

    def test_exact_parameter_container_and_options_no_extras_or_omissions(self):
        class ListSubclass(list): pass
        class DictSubclass(dict): pass
        valid = self.params()
        invalid = [None, {}, tuple(valid), ListSubclass(valid), [], valid[:1], valid + ['extra'],
                   [valid[0], None], [valid[0], []], [valid[0], DictSubclass(valid[1])]]
        for name in valid[1]:
            options = dict(valid[1]); options.pop(name)
            invalid.append([valid[0], options])
        for name, value in [('encoding', 'jsonParsed'), ('encoding', 'base64'), ('encoding', None),
                            ('commitment', 'confirmed'), ('commitment', 'processed'), ('commitment', None),
                            ('maxSupportedTransactionVersion', False), ('maxSupportedTransactionVersion', True),
                            ('maxSupportedTransactionVersion', 0.0), ('maxSupportedTransactionVersion', '0'),
                            ('maxSupportedTransactionVersion', None), ('maxSupportedTransactionVersion', -1),
                            ('maxSupportedTransactionVersion', 1), ('minContextSlot', 123),
                            ('source_id', 'candidate'), ('mint', 'candidate'), ('url', 'https://candidate.invalid')]:
            options = {**valid[1], name: value}; invalid.append([valid[0], options])
        class ZeroSubclass(int): pass
        class StringSubclass(str): pass
        for name, value in [('maxSupportedTransactionVersion', ZeroSubclass(0)),
                            ('encoding', StringSubclass('json')), ('commitment', StringSubclass('finalized'))]:
            invalid.append([valid[0], {**valid[1], name: value}])
        for params in invalid:
            with self.subTest(params=params): self.invalid(params)

    def test_transaction_redirects_never_issue_second_request(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.requests.clear()
                self.rejected(lambda: self.invoke(status=status, headers={'Location': 'https://other.invalid/'},
                                  method='getTransaction', params=self.params()))
                self.assertEqual(len(self.requests), 1)

    def test_transaction_errors_no_retry_and_sanitized(self):
        for payload in ({'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32000, 'message': fixtures.KEY}},
                        {'jsonrpc': '2.0', 'id': 1, 'error': fixtures.KEY},
                        {'jsonrpc': '2.0', 'id': '1', 'result': None},
                        {'jsonrpc': '2.0', 'id': 1, 'result': None, 'error': None}):
            self.requests.clear()
            self.rejected(lambda: self.invoke(body=json.dumps(payload).encode(),
                          method='getTransaction', params=self.params()))
            self.assertEqual(len(self.requests), 1)
        for error in (URLError(fixtures.KEY), TimeoutError(fixtures.KEY), ValueError(fixtures.KEY)):
            opener = Mock(); opener.open.side_effect = error
            with patch.object(rpc, 'build_opener', return_value=opener):
                self.rejected(lambda: self.transport('getTransaction', self.params()))
            self.assertEqual(opener.open.call_count, 1)

    def test_transaction_byte_limit_and_json_failures_still_reject(self):
        for body in (b'x' * (rpc._MAX_RESPONSE_BYTES + 1), b'\xff', fixtures.KEY.encode(),
                     b'{"jsonrpc":"2.0","id":1,"result":null,"result":null}'):
            self.requests.clear()
            self.rejected(lambda: self.invoke(body=body, method='getTransaction', params=self.params()))
            self.assertEqual(len(self.requests), 1)

    def test_source_descriptor_unchanged_no_new_candidate_authority(self):
        expected = ApprovedSource('helius-mainnet-single-request-v1', 'coordinator_capture')
        before = asdict(self.transport.source)
        self.transaction(None)
        self.assertEqual(self.transport.source, expected)
        self.assertEqual(asdict(self.transport.source), before)
