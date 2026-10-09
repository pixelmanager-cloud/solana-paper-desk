"""Charged one-attempt collector reads; caller holds research then invocation lock.

Use BoundedSource(s.rpc_source_id, s.quote_source_id, s.rpc, s.quote) with
s=PaperReadSources(progress, scan_id). Credentials come only from runtime env.
No admission creation, retries, source authentication or executable fill proof.
Original bounded wire outcomes are saved in the existing evidence store before
return/raise; the collector independently saves its original parsed responses.
"""
import base64
import math
import os
import time
from http.client import HTTPResponse, IncompleteRead
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from . import coordinator_rpc as strict
from . import original_byte_read_transport as syntax
from . import original_byte_slot_transport as wire
from .history_progress import HistoryProgress
from .model import canonical, digest
from .programs import address
from .providers import SOL
from . import provider_pacing

MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PRICE_RESPONSE_BYTES = 65536
RPC_ID = 'paper-read-v1'


class PaperReadError(ValueError):
    def __init__(self, code, evidence_hash=None):
        super().__init__(code)
        self.code = code
        self.evidence_hash = evidence_hash


def _read_chunked(response, limit, remaining):
    """Keep urllib entity decoding, but validate the boundaries it discards.

    This narrow profile accepts plain hex chunks and an empty trailer section.
    Extensions/trailers are unsupported. Framing has a separate 64KiB cap.
    Overrides apply only to this response, never to the global HTTP client.
    """
    if not isinstance(response, HTTPResponse) or not response.chunked or response.chunk_left is not None:
        raise PaperReadError('RESPONSE_HEADERS_INVALID')
    framing_left = 65536
    def boundary():
        nonlocal framing_left
        remaining()
        left = response.chunk_left
        if not left:
            if left is not None:
                framing_left -= 2
                if response._safe_read(2) != b'\r\n': raise IncompleteRead(b'')
            remaining()
            line = response.fp.readline(128)
            framing_left -= len(line)
            if (framing_left < 2 or not line.endswith(b'\r\n') or
                    not 1 <= len(line[:-2]) <= 16 or
                    any(c not in b'0123456789abcdefABCDEF' for c in line[:-2])):
                raise IncompleteRead(b'')
            left = int(line[:-2], 16)
            if left == 0:
                remaining()
                if response._safe_read(2) != b'\r\n': raise IncompleteRead(b'')
                response._close_conn()
                left = None
            response.chunk_left = left
        return left
    original = response._get_chunk_left
    response._get_chunk_left = boundary
    try:
        return response.read(limit + 1)
    finally:
        response._get_chunk_left = original


class PaperReadSources:
    rpc_source_id = 'helius-mainnet-paper-confirmed-v1'
    quote_source_id = 'jupiter-swap-v2-build-paper-v1'
    price_source_id = 'jupiter-price-v3-sol-paper-v1'

    def __init__(self, progress, scan_id, *, monitoring_budget=None):
        if not isinstance(progress, HistoryProgress) or type(scan_id) is not str or not 1 <= len(scan_id) <= 256:
            raise PaperReadError('ADMISSION_REQUIRED')
        self.progress = progress
        self.scan_id = scan_id
        if monitoring_budget is not None:
            from .monitoring_budget import MonitoringBudget
            if type(monitoring_budget) is not MonitoringBudget:
                raise PaperReadError('MONITORING_CONFIGURATION_INVALID')
        self.monitoring_budget = monitoring_budget

    def rpc(self, method, params, *, timeout_seconds):
        return self.rpc_with_evidence(method, params, timeout_seconds=timeout_seconds)[0]

    def rpc_with_evidence(self, method, params, *, timeout_seconds):
        """Return (unchanged result, original successful attempt evidence hash).

        The record contains exact wire bytes and actual acquisition time. Slot
        bounds/block-time associations remain trusted coordinator inputs.
        """
        try:
            if type(params) is not list: raise ValueError()
            if method == 'getSlot':
                if params != [{'commitment': 'finalized'}]: raise ValueError()
            elif method == 'getBlockTime':
                if len(params) != 1 or type(params[0]) is not int or not 0 <= params[0] < 2**63: raise ValueError()
            elif method == 'getAccountInfo':
                if len(params) != 2: raise ValueError()
                keys, options = params
                address(keys)
                if options != {'encoding': 'base64', 'commitment': 'confirmed'}: raise ValueError()
            elif method == 'getMultipleAccounts':
                if len(params) != 2: raise ValueError()
                keys, options = params
                if type(keys) is not list or not 1 <= len(keys) <= 100: raise ValueError()
                for key in keys: address(key)
                if len(set(keys)) != len(keys): raise ValueError()
                if (type(options) is not dict or set(options) != {'encoding', 'commitment', 'minContextSlot'}
                        or options['encoding'] != 'base64' or options['commitment'] != 'confirmed'
                        or type(options['minContextSlot']) is not int or not 0 <= options['minContextSlot'] < 2**63):
                    raise ValueError()
            else:
                raise ValueError()
            request = {'jsonrpc': '2.0', 'id': RPC_ID, 'method': method, 'params': params}
            body = canonical(request).encode('utf-8')
            if len(body) > MAX_REQUEST_BYTES: raise ValueError()
        except Exception:
            raise PaperReadError('REQUEST_INVALID') from None
        return self._attempt(method, wire._parse(body)['params'], body, timeout_seconds)

    def quote(self, input_mint, output_mint, amount, taker, *, timeout_seconds):
        try:
            for key in (input_mint, output_mint, taker): address(key)
            if type(amount) is not int or not 0 < amount < 2**64 or input_mint == output_mint: raise ValueError()
            query = {'inputMint': input_mint, 'outputMint': output_mint, 'amount': str(amount),
                     'taker': taker, 'slippageBps': '100', 'transactionVersion': '0'}
            body = urlencode(query).encode('ascii')
            if len(body) > MAX_REQUEST_BYTES: raise ValueError()
        except Exception:
            raise PaperReadError('REQUEST_INVALID') from None
        return self._attempt('jupiter_probe', query, body, timeout_seconds)[0]

    def sol_price(self, *, timeout_seconds):
        """Original SOL-only Price V3 response; usdPrice/blockId require parsing."""
        query = {'ids': SOL}
        return self._attempt('jupiter_price_v3', query, urlencode(query).encode('ascii'), timeout_seconds)[0]

    def _attempt(self, method, params, request_bytes, timeout_seconds):
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 15:
            raise PaperReadError('DEADLINE_INVALID')
        try:
            started = time.monotonic()
            if type(started) not in (int, float) or not math.isfinite(started): raise ValueError()
        except Exception:
            raise PaperReadError('DEADLINE_INVALID') from None
        deadline = started + timeout_seconds
        monitoring_reservation = None
        try:
            if self.monitoring_budget is not None:
                from .monitoring_budget import MonitoringBlocked
                try:
                    monitoring_reservation = self.monitoring_budget.reserve_read(self.progress, self.scan_id, method, params)
                except MonitoringBlocked as error:
                    raise PaperReadError(error.code) from None
                used = monitoring_reservation['investigation_requests_used']
            else:
                admission = self.progress.admission(self.scan_id)
                if admission is None or admission['state'] not in ('ADMITTED', 'SEALED'): raise ValueError()
                if method == 'jupiter_probe' and (params['inputMint'], params['outputMint']) not in (
                        (SOL, admission['descriptor']['mint']), (admission['descriptor']['mint'], SOL)):
                    raise ValueError()
                if not self.progress.reserve(self.scan_id): raise PaperReadError('BUDGET_EXHAUSTED')
                used = self.progress.admission(self.scan_id)['requests_used']
        except PaperReadError:
            raise
        except Exception:
            raise PaperReadError('ADMISSION_REQUIRED') from None
        raw = None
        http_status = None
        completed = None
        code = 'CREDENTIAL_UNAVAILABLE'
        result = None
        pacer = None
        pacing_ticket = None
        pacing_release = True
        provider = 'jupiter' if method in ('jupiter_probe', 'jupiter_price_v3') else 'helius'
        def remaining():
            now = time.monotonic()
            if not math.isfinite(now) or now < started or now >= deadline: raise PaperReadError('DEADLINE_EXCEEDED')
            return deadline - now
        try:
            pacer = provider_pacing.configured(priority='held' if self.monitoring_budget is not None else 'investigation')
            if pacer is not None: pacing_ticket = pacer.acquire(provider, timeout_seconds=remaining())
            name = 'JUPITER_API_KEY' if method in ('jupiter_probe', 'jupiter_price_v3') else 'HELIUS_API_KEY'
            key = os.environ.get(name)
            if type(key) is not str or not 1 <= len(key) <= 512 or any(not 33 <= ord(c) <= 126 for c in key):
                raise PaperReadError(code)
            code = 'TRANSPORT_ERROR'
            if method in ('jupiter_probe', 'jupiter_price_v3'):
                path = '/swap/v2/build' if method == 'jupiter_probe' else '/price/v3'
                request = Request('https://api.jup.ag' + path + '?' + request_bytes.decode('ascii'),
                                  headers={'Accept': 'application/json', 'x-api-key': key}, method='GET')
            else:
                request = Request(strict._ENDPOINT + '?' + urlencode({'api-key': key}), data=request_bytes,
                                  headers={'Content-Type': 'application/json', 'Accept': 'application/json'}, method='POST')
            opener = build_opener(strict._NoRedirect())
            with opener.open(request, timeout=remaining()) as response:
                code = 'HTTP_REJECTED'
                http_status = response.status if type(response.status) is int else None
                if pacer is not None and provider_pacing.should_throttle(response.status, response.headers):
                    pacing_release = False
                    pacer.throttle(provider, response.headers, ticket=pacing_ticket)
                    pacing_ticket = None
                if type(response.status) is not int or response.status != 200: raise PaperReadError(code)
                code = 'RESPONSE_HEADERS_INVALID'
                transfer = wire._header(response.headers, 'Transfer-Encoding')
                if transfer is not None and transfer != 'chunked': raise PaperReadError(code)
                if wire._header(response.headers, 'Content-Encoding', 'identity').lower() != 'identity': raise PaperReadError(code)
                length = wire._header(response.headers, 'Content-Length')
                if transfer is not None and length is not None: raise PaperReadError(code)
                if length is not None:
                    if not 1 <= len(length) <= 20 or not length.isascii() or not length.isdecimal(): raise PaperReadError(code)
                    length = int(length)
                    limit = MAX_PRICE_RESPONSE_BYTES if method == 'jupiter_price_v3' else MAX_RESPONSE_BYTES
                    if length > limit: raise PaperReadError('RESPONSE_OVERSIZED')
                limit = MAX_PRICE_RESPONSE_BYTES if method == 'jupiter_price_v3' else MAX_RESPONSE_BYTES
                remaining()
                code = 'TRANSPORT_ERROR'
                try:
                    # urllib removes HTTP chunk framing; retain its unchanged
                    # entity bytes, with the same bounded read and deadline.
                    observed = (_read_chunked(response, limit, remaining) if transfer is not None
                                else response.read(limit + 1))
                except IncompleteRead as error:
                    if type(error.partial) is bytes and len(error.partial) <= limit:
                        raw = error.partial
                        raise PaperReadError('RESPONSE_TRUNCATED') from None
                    raise PaperReadError('RESPONSE_OVERSIZED') from None
                if type(observed) is not bytes: raise PaperReadError('RESPONSE_INVALID')
                if len(observed) > limit: raise PaperReadError('RESPONSE_OVERSIZED')
                raw = observed
                if length is not None and len(raw) != length: raise PaperReadError('RESPONSE_TRUNCATED')
            code = 'CLOCK_INVALID'
            at = time.time()
            if type(at) not in (int, float) or not math.isfinite(at) or not 0 <= at < 2**63: raise PaperReadError(code)
            completed = int(at)
            remaining()
            code = 'RESPONSE_INVALID'
            payload = wire._parse(raw)
            syntax._bounded_json(payload)
            if method in ('jupiter_probe', 'jupiter_price_v3'):
                if type(payload) is not dict or (method == 'jupiter_probe' and any(field not in payload for field in ('inAmount', 'outAmount', 'routePlan'))):
                    raise PaperReadError(code)
                result = payload
            else:
                if (type(payload) is not dict or payload.get('jsonrpc') != '2.0' or type(payload.get('id')) is not str
                        or payload['id'] != RPC_ID or set(payload) not in ({'jsonrpc','id','result'}, {'jsonrpc','id','error'})):
                    raise PaperReadError(code)
                if 'error' in payload:
                    error = payload['error']
                    if (type(error) is not dict or not {'code', 'message'} <= set(error) <= {'code','message','data'}
                            or type(error['code']) is not int or not -2**63 <= error['code'] < 2**63
                            or type(error['message']) is not str): raise PaperReadError(code)
                    raise PaperReadError('RPC_ERROR')
                syntax._result({'method': method, 'params': params}, payload['result'])
                result = payload['result']
            remaining()
            code = None
        except PaperReadError as error:
            code = error.code
        except Exception as error:
            if isinstance(error, HTTPError):
                code = 'HTTP_REJECTED'
                http_status = error.code if type(error.code) is int else None
                if pacer is not None and provider_pacing.should_throttle(error.code, error.headers):
                    pacing_release = False
                    try:
                        pacer.throttle(provider, error.headers, ticket=pacing_ticket)
                        pacing_ticket = None
                    except provider_pacing.PacingError as pacing_error: code = pacing_error.code
                try: error.close()
                except Exception: pass
            elif isinstance(error, strict.CoordinatorRPCError): code = 'HTTP_REJECTED'
            elif isinstance(error, provider_pacing.PacingError): code = error.code
        if pacing_ticket is not None and pacing_release:
            try:
                pacer.finish(provider, pacing_ticket)
                remaining()
            except provider_pacing.PacingError as error: code = error.code
            except PaperReadError as error: code = error.code
        if raw is not None and completed is None:
            # Retain acquisition time for bounded partial/invalid observations
            # when the clock is available; never erase the original failure.
            try:
                at = time.time()
                if type(at) in (int, float) and math.isfinite(at) and 0 <= at < 2**63:
                    completed = int(at)
            except Exception:
                pass
        record = {'kind': 'paper_read_attempt_v1', 'scan_id': self.scan_id, 'requests_used': used,
                  'source_id': self.price_source_id if method == 'jupiter_price_v3' else self.quote_source_id if method == 'jupiter_probe' else self.rpc_source_id,
                  'method': method, 'params': params, 'request_bytes_base64': base64.b64encode(request_bytes).decode(),
                  'response_bytes_base64': None if raw is None else base64.b64encode(raw).decode(),
                  'observed_at': completed, 'http_status': http_status, 'failure_code': code}
        if monitoring_reservation is not None:
            record['monitoring_reservation'] = monitoring_reservation
        try:
            evidence_hash = self.progress.store.save(record)
            if evidence_hash != digest(record): raise ValueError()
            if monitoring_reservation is not None:
                self.monitoring_budget.retain_outcome(monitoring_reservation, evidence_hash)
        except Exception:
            raise PaperReadError('OUTCOME_PERSISTENCE_FAILED') from None
        if code is not None: raise PaperReadError(code, evidence_hash) from None
        if method == 'jupiter_price_v3':
            return {'kind': 'sol_usd_price_probe', 'observed_at': completed, 'request': params,
                    'response': result, 'evidence_hash': evidence_hash, 'http_status': http_status,
                    'notice': 'Provider valuation estimate, not executable price or independently verified block.'}, evidence_hash
        if method == 'jupiter_probe':
            return {'kind': 'unsigned_route_probe', 'observed_at': completed, 'request': params,
                            'response': result, 'notice': 'No signing or submission. Quote is not a guaranteed fill.'}, evidence_hash
        return result, evidence_hash
