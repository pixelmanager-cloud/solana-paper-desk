"""Disconnected, one-POST coordinator transport; provisioning/review required.

RPC over TLS is provider evidence, not cryptographic proof of chain state.
The caller must reserve its charged attempt before invoking this adapter.
"""
import json
import math
import os
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .pool_receipt_ledger import ApprovedSource
from .programs import address

_ENDPOINT = 'https://mainnet.helius-rpc.com/'
_SOURCE = ApprovedSource('helius-mainnet-single-request-v1', 'coordinator_capture')
_TIMEOUT_SECONDS = 15
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_METHODS = frozenset(('getGenesisHash', 'getSlot', 'getMultipleAccounts', 'getBlockTime'))


class CoordinatorRPCError(ValueError):
    """Static safe message only; no URL, credential or provider body."""


class _NoRedirect(HTTPRedirectHandler):
    def _reject(self, request, response, code, message, headers):
        response.close()
        raise CoordinatorRPCError('Coordinator RPC redirect rejected')

    http_error_301 = http_error_302 = http_error_303 = http_error_307 = http_error_308 = _reject


def _params(method, params):
    if type(method) is not str or method not in _METHODS or type(params) is not list:
        raise ValueError('Invalid method/params')
    if method == 'getGenesisHash':
        if params: raise ValueError('No genesis parameters')
    elif method == 'getSlot':
        if params != [{'commitment': 'finalized'}]: raise ValueError('Finalized slot required')
    elif method == 'getBlockTime':
        if len(params) != 1 or type(params[0]) is not int or params[0] < 0:
            raise ValueError('Invalid clock slot')
    else:
        if len(params) != 2 or type(params[0]) is not list or not 1 <= len(params[0]) <= 100:
            raise ValueError('Bounded account keys required')
        for key in params[0]:
            if type(key) is not str or not 32 <= len(key) <= 44: raise ValueError('Invalid account key')
            address(key)
        options = params[1]
        if (type(options) is not dict or set(options) - {'encoding', 'commitment', 'minContextSlot'}
                or options.get('encoding') != 'base64' or options.get('commitment') != 'finalized'):
            raise ValueError('Finalized base64 accounts required')
        if 'minContextSlot' in options and (type(options['minContextSlot']) is not int or options['minContextSlot'] < 0):
            raise ValueError('Invalid bank slot')


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('Duplicate JSON member')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Non-JSON numeric constant')


def _float(value):
    number = float(value)
    if not math.isfinite(number): raise ValueError('Unrepresentable JSON number')
    return number


def _result(payload):
    if (type(payload) is not dict or payload.get('jsonrpc') != '2.0'
            or type(payload.get('id')) is not int or payload['id'] != 1
            or (set(payload) != {'jsonrpc', 'id', 'result'} and set(payload) != {'jsonrpc', 'id', 'error'})):
        raise ValueError('Invalid RPC envelope')
    if 'error' in payload:
        error = payload['error']
        if (type(error) is not dict or not {'code', 'message'} <= set(error)
                or set(error) - {'code', 'message', 'data'}
                or type(error['code']) is not int or type(error['message']) is not str):
            raise ValueError('Invalid RPC error')
        raise ValueError('RPC error')
    return payload['result']


class HeliusMainnetRPC:
    """Fixed identity/endpoint; no candidate URL, source or credential arguments.

    Constructor performs no I/O. The coordinator must explicitly provision this
    descriptor in its trusted roster before any future bridge use. Result content
    still requires independent bank/genesis/account/policy validation.
    """
    __slots__ = ()

    @property
    def source(self):
        return _SOURCE

    def __call__(self, method, params):
        try:
            _params(method, params)
            # Local invocation context only. Never store the credential on the
            # adapter, in the source descriptor, records, logs or exceptions.
            key = os.environ.get('HELIUS_API_KEY')
            if type(key) is not str or not 1 <= len(key) <= 512 or any(not 33 <= ord(c) <= 126 for c in key):
                raise ValueError('Credential unavailable')
            request = Request(_ENDPOINT + '?' + urlencode({'api-key': key}),
                              data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params},
                                              allow_nan=False).encode('utf-8'),
                              headers={'Content-Type': 'application/json', 'Accept': 'application/json'}, method='POST')
            # One open, no retries, redirects, discovery or fallback. Default TLS
            # verification and inherited proxy handling remain enabled.
            opener = build_opener(_NoRedirect())
            with opener.open(request, timeout=_TIMEOUT_SECONDS) as response:
                if response.status != 200: raise ValueError('HTTP failure')
                encoding = response.headers.get('Content-Encoding', 'identity')
                if encoding.lower() != 'identity': raise ValueError('Encoded body unsupported')
                length = response.headers.get('Content-Length')
                if length is not None and (not length.isascii() or not length.isdecimal()
                                           or int(length) > _MAX_RESPONSE_BYTES):
                    raise ValueError('Invalid response length')
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                if type(raw) is not bytes or len(raw) > _MAX_RESPONSE_BYTES:
                    raise ValueError('Response exceeds budget')
                if length is not None and len(raw) != int(length): raise ValueError('Truncated response')
            return _result(json.loads(raw.decode('utf-8'), object_pairs_hook=_object,
                                      parse_constant=_constant, parse_float=_float))
        except Exception as error:
            if isinstance(error, HTTPError):
                try:
                    error.close()
                except Exception:
                    pass
            # Suppress exception chains too: malformed URL/headers, credential
            # lookup, urllib/http errors and parser errors can contain secrets.
            raise CoordinatorRPCError('Coordinator RPC request failed') from None
