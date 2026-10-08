"""Disconnected one-POST finalized getSlot byte exchange, never an attestation.

Only future reviewed journal integration may use this primitive. Its caller
must durably charge/fence the attempt before invoking it; this module has no
budget, retry, journal, source-provisioning or acceptance capability.
"""
from dataclasses import dataclass, field
from http.client import IncompleteRead
import json
from urllib.error import HTTPError
from urllib.parse import urlencode

from . import coordinator_rpc as fixed

MAX_REQUEST_BYTES = 1024
MAX_RESPONSE_BYTES = 65536
MAX_ID_BYTES = 128


class SlotRequestInvalid(ValueError):
    """Fixed request rejection before credential lookup or opener creation."""


@dataclass(frozen=True)
class SlotByteExchange:
    # Raw provider evidence may contain sensitive strings. Do not print it.
    request_bytes: bytes = field(repr=False)
    response_bytes: bytes | None = field(repr=False)
    failure_code: str | None

    def diagnostic(self):
        return {'status':'REJECT', 'failure_code':self.failure_code,
                'request_bytes':len(self.request_bytes),
                'response_bytes':len(self.response_bytes) if self.response_bytes is not None else None,
                'budget_reserved':False, 'intent_authenticated':False,
                'source_authenticated':False, 'finality_authenticated':False,
                'entry_allowed':False}


def _integer(raw):
    # Avoid conversion of attacker-controlled arbitrarily long JSON integers.
    if len(raw.lstrip('-'))>20:raise ValueError('Integer bound')
    return int(raw)


def _parse(raw):
    return json.loads(raw.decode('utf-8'),object_pairs_hook=fixed._object,
                      parse_constant=fixed._constant,parse_float=fixed._float,
                      parse_int=_integer)


def validate_slot_request(raw):
    """Return decoded string ID; never normalize/rebuild the supplied bytes."""
    try:
        if type(raw) is not bytes or not 1<=len(raw)<=MAX_REQUEST_BYTES:
            raise ValueError('Request bound')
        request=_parse(raw)
        if (type(request) is not dict or set(request)!={'jsonrpc','id','method','params'}
                or type(request['jsonrpc']) is not str or request['jsonrpc']!='2.0'
                or type(request['method']) is not str or request['method']!='getSlot'
                or type(request['id']) is not str or not 1<=len(request['id'].encode('utf-8'))<=MAX_ID_BYTES
                or type(request['params']) is not list or len(request['params'])!=1
                or type(request['params'][0]) is not dict or set(request['params'][0])!={'commitment'}
                or type(request['params'][0]['commitment']) is not str
                or request['params'][0]['commitment']!='finalized'):
            raise ValueError('Request grammar')
        return request['id']
    except Exception:
        raise SlotRequestInvalid('Finalized slot request invalid') from None


def _response_failure(raw, request_id):
    """Local framing/result syntax only; no finality/source/intent authority."""
    try:
        body=_parse(raw)
        if (type(body) is not dict or body.get('jsonrpc')!='2.0'
                or type(body.get('id')) is not str or body['id']!=request_id
                or set(body) not in ({'jsonrpc','id','result'},{'jsonrpc','id','error'})):
            return 'RESPONSE_INVALID'
        if 'error' in body:
            error=body['error']
            if (type(error) is not dict or not {'code','message'}<=set(error)<= {'code','message','data'}
                    or type(error['code']) is not int or not -2**63<=error['code']<2**63
                    or type(error['message']) is not str):
                return 'RESPONSE_INVALID'
            return 'RPC_ERROR'
        if type(body['result']) is not int or not 0<=body['result']<2**64:
            return 'RESPONSE_INVALID'
        return None
    except Exception:
        return 'RESPONSE_INVALID'


def _header(headers, name, default=None):
    # Actual HTTPMessage supports get_all; dict fixtures have a single value.
    if hasattr(headers,'get_all'):
        values=headers.get_all(name,[])
        if type(values) is not list or len(values)>1:raise ValueError('Ambiguous header')
        value=values[0] if values else default
    else:value=headers.get(name,default)
    if value is not None and type(value) is not str:raise ValueError('Header type')
    return value


class HeliusFinalizedSlotTransport:
    """Fixed endpoint/credential selection; constructor has no I/O or options.

    One validated invocation can open at most once. This class is disconnected
    from all acquisition/runtime callers. Repeated direct calls are not an
    idempotency/budget mechanism: later journal integration must enforce that.
    """
    __slots__=()

    def __call__(self, request_bytes):
        request_id=validate_slot_request(request_bytes)  # Before secrets/network.
        return _exchange(request_bytes,request_id,MAX_RESPONSE_BYTES,_response_failure)


def _exchange(request_bytes,request_id,response_limit,response_failure):
    """Private shared fixed transport; validators and bounds are module-owned."""
    raw=None
    code='CREDENTIAL_UNAVAILABLE'
    try:
        key=fixed.os.environ.get('HELIUS_API_KEY')
        if type(key) is not str or not 1<=len(key)<=512 or any(not 33<=ord(c)<=126 for c in key):
            return SlotByteExchange(request_bytes,None,code)
        code='TRANSPORT_ERROR'
        request=fixed.Request(fixed._ENDPOINT+'?'+urlencode({'api-key':key}),
            data=request_bytes,headers={'Content-Type':'application/json','Accept':'application/json'},method='POST')
        # Reuse the accepted no-redirect handler and default verified TLS /
        # proxy handling. No custom endpoint, opener, credentials or retry.
        opener=fixed.build_opener(fixed._NoRedirect())
        with opener.open(request,timeout=fixed._TIMEOUT_SECONDS) as response:
            code='HTTP_REJECTED'
            if type(response.status) is not int or response.status!=200:
                return SlotByteExchange(request_bytes,None,code)
            code='RESPONSE_HEADERS_INVALID'
            # urllib's HTTPResponse can decode unsupported/ambiguous TE
            # and tolerate incomplete chunk trailers. Reject every TE
            # occurrence before reading; no chunked parser is supported.
            if _header(response.headers,'Transfer-Encoding') is not None:
                return SlotByteExchange(request_bytes,None,code)
            encoding=_header(response.headers,'Content-Encoding','identity')
            if encoding.lower()!='identity':return SlotByteExchange(request_bytes,None,code)
            length=_header(response.headers,'Content-Length')
            if length is not None:
                if not 1<=len(length)<=20 or not length.isascii() or not length.isdecimal():
                    return SlotByteExchange(request_bytes,None,code)
                length=int(length)
                if length>response_limit:return SlotByteExchange(request_bytes,None,'RESPONSE_OVERSIZED')
            code='TRANSPORT_ERROR'
            try:
                observed=response.read(response_limit+1)
            except IncompleteRead as error:
                partial=error.partial
                if type(partial) is not bytes:return SlotByteExchange(request_bytes,None,'RESPONSE_INVALID')
                if len(partial)>response_limit:return SlotByteExchange(request_bytes,None,'RESPONSE_OVERSIZED')
                raw=partial
                return SlotByteExchange(request_bytes,raw,'RESPONSE_TRUNCATED')
            if type(observed) is not bytes:return SlotByteExchange(request_bytes,None,'RESPONSE_INVALID')
            if len(observed)>response_limit:return SlotByteExchange(request_bytes,None,'RESPONSE_OVERSIZED')
            raw=observed
            if length is not None and len(raw)!=length:
                return SlotByteExchange(request_bytes,raw,'RESPONSE_TRUNCATED')
        return SlotByteExchange(request_bytes,raw,response_failure(raw,request_id))
    except Exception as error:
        if isinstance(error,HTTPError):
            code='HTTP_REJECTED'
            try:error.close()
            except Exception:pass
        elif isinstance(error,fixed.CoordinatorRPCError):code='HTTP_REJECTED'
        # No exception text, headers, URL, credential or provider error data
        # is copied into failure provenance. Captured bounded bytes remain
        # opaque evidence, including invalid framing and RPC error bodies.
        return SlotByteExchange(request_bytes,raw,code)
