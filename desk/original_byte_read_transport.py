"""Disconnected original-wire OWN-1 read syntax, not admission or attestation.

Only the enumerated accepted producer request shapes are supported. No caller
endpoint, transport options, retries, budget management or runtime wiring.
"""
import base64

from . import original_byte_slot_transport as slot
from .programs import address, unbase58
from .security import base58

MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 65536
MAX_HISTORY_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CURSOR_BYTES = 1024
MAX_JSON_NODES = 100000
MAX_JSON_DEPTH = 64
METHODS = frozenset(('getSlot', 'getAccountInfo', 'getTransactionsForAddress',
                     'getGenesisHash', 'getMultipleAccounts', 'getBlockTime'))
# Same immutable opaque evidence/fixed REJECT diagnostics; no new trust fields.
ReadByteExchange = slot.SlotByteExchange


class ReadRequestInvalid(ValueError):
    """Fixed rejection before credentials or opener creation."""


def _need(condition):
    if not condition:
        raise ValueError('Unsupported read syntax')


def _key(value):
    _need(type(value) is str)
    address(value)
    _need(base58(unbase58(value)) == value)


def _uint(value, ceiling=2**64):
    _need(type(value) is int and 0 <= value < ceiling)


def _text(value, limit):
    _need(type(value) is str and 1 <= len(value.encode('utf-8')) <= limit)


def _params(method, params):
    _need(type(params) is list)
    if method == 'getSlot':
        _need(params == [{'commitment': 'finalized'}])
    elif method == 'getGenesisHash':
        _need(params == [])
    elif method == 'getBlockTime':
        _need(len(params) == 1)
        _uint(params[0], 2**63)
    elif method == 'getAccountInfo':
        _need(len(params) == 2)
        _key(params[0])
        _need(type(params[1]) is dict and
              params[1] == {'encoding': 'base64', 'commitment': 'confirmed'})
    elif method == 'getMultipleAccounts':
        _need(len(params) == 2 and type(params[0]) is list and 1 <= len(params[0]) <= 100)
        for key in params[0]:
            _key(key)
        _need(len(set(params[0])) == len(params[0]))
        options = params[1]
        _need(type(options) is dict and set(options) in (
            {'encoding', 'commitment'}, {'encoding', 'commitment', 'minContextSlot'})
            and options['encoding'] == 'base64' and options['commitment'] == 'finalized')
        if 'minContextSlot' in options:
            _uint(options['minContextSlot'], 2**63)
    else:
        _need(method == 'getTransactionsForAddress' and len(params) == 2)
        _key(params[0])
        options = params[1]
        fields = {'transactionDetails', 'sortOrder', 'limit', 'commitment',
                  'encoding', 'maxSupportedTransactionVersion', 'filters'}
        _need(type(options) is dict and set(options) in (fields, fields | {'paginationToken'}))
        _need(options['transactionDetails'] == 'full' and options['sortOrder'] == 'asc'
              and type(options['limit']) is int and options['limit'] == 100
              and options['commitment'] == 'finalized' and options['encoding'] == 'jsonParsed'
              and type(options['maxSupportedTransactionVersion']) is int
              and options['maxSupportedTransactionVersion'] == 1)
        filters = options['filters']
        _need(type(filters) is dict and set(filters) == {'slot', 'status', 'tokenAccounts'}
              and filters['status'] == 'any' and filters['tokenAccounts'] == 'none')
        bounds = filters['slot']
        _need(type(bounds) is dict and set(bounds) == {'gte', 'lt'})
        _uint(bounds['gte'])
        _need(bounds['gte'] == 0 and type(bounds['lt']) is int and 0 < bounds['lt'] <= 2**64)
        if 'paginationToken' in options:
            _text(options['paginationToken'], MAX_CURSOR_BYTES)


def validate_read_request(raw):
    """Return local parsed grammar only; supplied wire bytes are never rebuilt."""
    try:
        _need(type(raw) is bytes and 1 <= len(raw) <= MAX_REQUEST_BYTES)
        request = slot._parse(raw)
        _need(type(request) is dict and set(request) == {'jsonrpc', 'id', 'method', 'params'}
              and request['jsonrpc'] == '2.0' and type(request['method']) is str
              and request['method'] in METHODS)
        _text(request['id'], slot.MAX_ID_BYTES)
        _params(request['method'], request['params'])
        if request['method'] == 'getSlot':
            slot.validate_slot_request(raw)  # Preserve its existing 1 KiB grammar.
        elif request['method'] not in ('getMultipleAccounts', 'getTransactionsForAddress'):
            _need(len(raw) <= slot.MAX_REQUEST_BYTES)
        return request
    except Exception:
        raise ReadRequestInvalid('Original-byte read request invalid') from None


def _bounded_json(value):
    # Iterative traversal: no recursive validation or unbounded nested walk.
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        _need(nodes <= MAX_JSON_NODES and depth <= MAX_JSON_DEPTH)
        if type(item) is str:
            item.encode('utf-8')  # Reject escaped lone surrogates too.
        elif type(item) is dict:
            _need(nodes + len(pending) + len(item) <= MAX_JSON_NODES)
            for key, child in item.items():
                key.encode('utf-8')
                pending.append((child, depth + 1))
        elif type(item) is list:
            _need(nodes + len(pending) + len(item) <= MAX_JSON_NODES)
            pending.extend((child, depth + 1) for child in item)


def _context(value, floor=0):
    _need(type(value) is dict and set(value) in ({'slot'}, {'slot', 'apiVersion'}))
    _uint(value['slot'], 2**63)
    _need(value['slot'] >= floor)
    if 'apiVersion' in value:
        _text(value['apiVersion'], 128)


def _account(value):
    if value is None:
        return  # Missing/closed is explicit, never zero or proven closure.
    _need(type(value) is dict and {'owner', 'executable', 'data'} <= set(value)
          <= {'owner', 'executable', 'data', 'lamports', 'rentEpoch', 'space'})
    _key(value['owner'])
    _need(type(value['executable']) is bool and type(value['data']) is list
          and len(value['data']) == 2 and type(value['data'][0]) is str
          and value['data'][1] == 'base64')
    encoded = value['data'][0].encode('ascii')
    decoded = base64.b64decode(encoded, validate=True)
    _need(base64.b64encode(decoded) == encoded)
    for field in ('lamports', 'rentEpoch', 'space'):
        if field in value:
            _uint(value[field])
    if 'space' in value:
        _need(value['space'] == len(decoded))
    # No owner-program/layout/authority policy here: preserve unsupported states.


def _result(request, result):
    method, params = request['method'], request['params']
    if method == 'getSlot':
        _uint(result)
    elif method == 'getGenesisHash':
        _key(result)  # Syntax only; not a trusted network/genesis pin.
    elif method == 'getBlockTime':
        _uint(result, 2**63)
    elif method in ('getAccountInfo', 'getMultipleAccounts'):
        _need(type(result) is dict and set(result) == {'context', 'value'})
        floor = params[1].get('minContextSlot', 0)
        _context(result['context'], floor)
        if method == 'getAccountInfo':
            _account(result['value'])
        else:
            _need(type(result['value']) is list and len(result['value']) == len(params[0]))
            for value in result['value']:
                _account(value)
    else:
        _need(type(result) is dict and set(result) in ({'data'}, {'data', 'paginationToken'})
              and type(result['data']) is list and len(result['data']) <= 100)
        _need(all(type(row) is dict for row in result['data']))
        if result.get('paginationToken') is not None:
            _text(result['paginationToken'], MAX_CURSOR_BYTES)
        # Rows stay opaque. Decode gaps/failed txs/order/cursor cycles/coverage
        # must still be checked by existing history replay; no summary elevation.


def _response_failure(raw, request):
    try:
        body = slot._parse(raw)
        _bounded_json(body)
        _need(type(body) is dict and body.get('jsonrpc') == '2.0'
              and type(body.get('id')) is str and body['id'] == request['id']
              and set(body) in ({'jsonrpc', 'id', 'result'}, {'jsonrpc', 'id', 'error'}))
        if 'error' in body:
            error = body['error']
            _need(type(error) is dict and {'code', 'message'} <= set(error) <= {'code', 'message', 'data'}
                  and type(error['code']) is int and -2**63 <= error['code'] < 2**63
                  and type(error['message']) is str)
            return 'RPC_ERROR'
        _result(request, body['result'])
        return None
    except Exception:
        return 'RESPONSE_INVALID'


class HeliusOriginalByteReadTransport:
    """Fixed one-POST transport; no I/O at construction, no source capability."""
    __slots__ = ()

    def __call__(self, request_bytes):
        request = validate_read_request(request_bytes)  # Before credentials/I/O.
        if request['method'] == 'getSlot':
            return slot.HeliusFinalizedSlotTransport()(request_bytes)
        limit = (MAX_HISTORY_RESPONSE_BYTES if request['method'] == 'getTransactionsForAddress'
                 else MAX_RESPONSE_BYTES)
        return slot._exchange(request_bytes, request['id'], limit,
                              lambda raw, _id: _response_failure(raw, request))
