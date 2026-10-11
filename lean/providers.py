"""Read-only provider clients for the lean paper trader (Helius, Jupiter, Kraken).

Paper only: there is no signing and no transaction submission. The Helius RPC
method allowlist is read-only and anything else is refused before any I/O.

Every call returns ``(parsed, raw_bytes, meta)`` or raises ``ProviderError(code,
transient)``. ``transient`` means a retry may help (timeout, connection reset,
HTTP 408/429/5xx, truncated body); everything else is non-transient. Transient
failures are retried inside the call with exponential backoff and jitter within a
bounded deadline, so a caller sees at most one exception per call.

Secrets: keys are read from a JSON credential file at runtime. A key is only ever
placed in the outgoing request. It never reaches an exception message, a log
record, a ``repr`` or a chained exception (all provider errors are raised
``from None`` and carry only a stable code and a bounded metadata dict).

Rate limiting is an in-process token bucket per provider: no shared database. Every
client in the process shares one bucket per provider and lane (``shared_limiter``):
the ``main`` lane (candidate screening) and the ``exit`` lane (held-position marks and
exit quotes) split the plan budget, so screening and the 429 blocks it provokes never
starve exits. A wait is bounded by the call's deadline (``RATE_LIMITED``, transient).
Error responses keep their (bounded) body in ``ProviderError.raw``.
"""
import base64
from dataclasses import dataclass
from decimal import Decimal, DecimalException
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import stat
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

log = logging.getLogger('lean.providers')

# Paid-plan sized defaults: requests per second, burst.
DEFAULT_RATES = {'helius': (10.0, 10), 'jupiter': (4.0, 4), 'kraken': (0.5, 1)}
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SMALL_RESPONSE_BYTES = 64 * 1024
REQUEST_TIMEOUT = 10.0
DEADLINE_SECONDS = 20.0
MAX_ATTEMPTS = 3
BACKOFF_BASE = 0.5
BACKOFF_CAP = 8.0
MAX_RETRY_AFTER = 30.0
MAX_MULTIPLE_ACCOUNTS = 100
MAX_PRICE_IDS = 50
SOL_MINT = 'So11111111111111111111111111111111111111112'
READ_ONLY_RPC = frozenset({
    'getAccountInfo', 'getMultipleAccounts', 'getTokenLargestAccounts', 'getTokenSupply',
    'getTokenAccountsByOwner', 'getSlot', 'getBlockTime', 'getBalance', 'getTransaction',
    'getSignaturesForAddress', 'getLatestBlockhash', 'getMinimumBalanceForRentExemption',
    'getProgramAccounts', 'getTokenAccountBalance'})
TRANSIENT_RPC_CODES = frozenset({-32005, -32004, -32016})
_BASE58 = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')


class ProviderError(Exception):
    """Typed failure. The message is only provider and code: never a URL or key."""

    def __init__(self, code, transient, *, provider=None, raw=None, meta=None):
        self.code = code
        self.transient = bool(transient)
        self.provider = provider
        self.raw = raw            # raw response bytes when available (research retention)
        self.meta = dict(meta or {})
        super().__init__(f'{provider or "provider"}:{code}')

    def __reduce__(self):
        return (ProviderError, (self.code, self.transient), {'provider': self.provider})


# ---------------------------------------------------------------- rate limiting

class RateLimiter:
    """Thread-safe token bucket. ``acquire`` reserves a token and sleeps its wait.

    Reserving before sleeping (tokens may go negative) keeps concurrent callers
    ordered and never over-issues.
    """

    def __init__(self, rate_per_second, burst=None, *, clock=time.monotonic, sleep=time.sleep):
        if not isinstance(rate_per_second, (int, float)) or not 0 < rate_per_second <= 1000:
            raise ValueError('rate_per_second out of range')
        self.rate = float(rate_per_second)
        self.burst = float(burst if burst is not None else max(1, rate_per_second))
        if self.burst < 1:
            raise ValueError('burst must be at least 1')
        self._clock, self._sleep = clock, sleep
        self._tokens = self.burst
        self._stamp = clock()
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def acquire(self, deadline=None):
        """Block until a request may be sent; return the seconds waited.

        ``deadline`` is an absolute time on this limiter's clock. If the wait (token debt or a 429 block) would run past
        it, no token is taken and ``ProviderError('RATE_LIMITED', transient=True)`` is raised at once: a caller never
        sleeps beyond its own call deadline."""
        with self._lock:
            now = self._clock()
            tokens = min(self.burst, self._tokens + (now - self._stamp) * self.rate) - 1.0
            wait = max(0.0, -tokens / self.rate, self._blocked_until - now)
            if deadline is not None and now + wait > deadline:
                self._tokens, self._stamp = tokens + 1.0, now
                raise ProviderError('RATE_LIMITED', True, meta={'wait_s': round(wait, 3)})
            self._tokens, self._stamp = tokens, now
        if wait > 0:
            self._sleep(wait)
        return wait

    def block_for(self, seconds):
        """Provider asked us to back off (429 Retry-After): delay every caller."""
        with self._lock:
            self._blocked_until = max(self._blocked_until, self._clock() + max(0.0, float(seconds)))


# One limiter per (provider, lane) for the whole process: two client instances never double the budget. The ``exit``
# lane is a reserved share of the plan budget for held-position marks and exit quotes, so candidate screening (and a 429
# it provokes) can never starve exits. The lane shares of one provider sum to its plan rate.
LANE_SHARES = {'main': 0.75, 'exit': 0.25}
_SHARED = {}
_SHARED_LOCK = threading.Lock()


def shared_limiter(provider, lane='main', *, clock=time.monotonic, sleep=time.sleep):
    """The process-wide limiter of ``provider``/``lane``. Keyed by clock and sleep too, so a test with a fake clock gets
    its own limiter while every real client in the process shares one."""
    if lane not in LANE_SHARES:
        raise ValueError('unknown lane')
    if provider == 'kraken':
        lane = 'main'            # exits never need SOL/USD: Kraken keeps one undivided bucket
    key = (provider, lane, clock, sleep)
    with _SHARED_LOCK:
        limiter = _SHARED.get(key)
        if limiter is None:
            rate, burst = DEFAULT_RATES[provider]
            share = 1.0 if provider == 'kraken' else LANE_SHARES[lane]   # helius 7.5+2.5/s, jupiter 3+1/s
            limiter = _SHARED[key] = RateLimiter(rate * share, max(1.0, burst * share), clock=clock, sleep=sleep)
        return limiter


def backoff_delay(attempt, *, rng=random.random, base=BACKOFF_BASE, cap=BACKOFF_CAP):
    """Exponential backoff with equal jitter: in [d/2, d], d = min(cap, base*2**attempt)."""
    d = min(cap, base * (2 ** attempt))
    return d / 2 + rng() * d / 2


# ---------------------------------------------------------------- strict JSON

def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate key')
        out[key] = value
    return out


def _reject_constant(_):
    raise ValueError('non-finite constant')


def parse_json(raw):
    """Duplicate-key and NaN/Infinity rejecting parse; fractions stay exact (Decimal)."""
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique, parse_float=Decimal,
                      parse_constant=_reject_constant)


# ---------------------------------------------------------------- credentials

KEY_NAMES = {'HELIUS_API_KEY': 'helius', 'JUPITER_API_KEY': 'jupiter', 'helius': 'helius', 'jupiter': 'jupiter'}


def load_keys(path):
    """Read the provider keys from a private credential file and return ``{'helius': ..., 'jupiter': ...}``.

    Accepts the production systemd credential format ``{"HELIUS_API_KEY": "...", "JUPITER_API_KEY": "..."}`` (the one
    ``desk/paper_cycle_cli.py`` loads from ``%d/provider-keys.json``) and the short ``{"helius", "jupiter"}`` form. Both
    keys are required; values are stripped. The file must be a regular non-symlink file readable only by its owner (or
    group-read, as systemd ``LoadCredential`` may install it: 0400/0440/0600)."""
    try:
        p = Path(path)
        info = os.lstat(p)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o037 or info.st_size > 16384:
            raise ValueError
        value = json.loads(p.read_text(), object_pairs_hook=_unique)
        if not isinstance(value, dict):
            raise ValueError
        keys = {}
        for name, key in value.items():
            if name not in KEY_NAMES:
                continue
            key = key.strip() if isinstance(key, str) else key
            if (not isinstance(key, str) or not 1 <= len(key) <= 512 or any(not 33 <= ord(c) <= 126 for c in key)
                    or KEY_NAMES[name] in keys):
                raise ValueError
            keys[KEY_NAMES[name]] = key
        if set(keys) != {'helius', 'jupiter'}:
            raise ValueError
        return keys
    except (OSError, ValueError):
        # Deliberately generic: never echo file contents or the parse error.
        raise ProviderError('KEY_FILE_INVALID', False) from None


def _check_key(key):
    if not isinstance(key, str) or not 1 <= len(key) <= 512 or any(not 33 <= ord(c) <= 126 for c in key):
        raise ProviderError('KEY_INVALID', False)
    return key


# ---------------------------------------------------------------- transport

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def default_opener(request, timeout):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


class Transport:
    """One provider's HTTP path: limiter, retries, bounded read, typed errors.

    ``opener(request, timeout)`` returns an object with ``status``, ``headers``
    (``.get``) and ``read(n)``; it may raise urllib/socket exceptions. Injectable
    so tests need no network.
    """

    def __init__(self, provider, limiter=None, *, opener=None, clock=time.time,
                 monotonic=time.monotonic, sleep=time.sleep, rng=random.random,
                 max_attempts=MAX_ATTEMPTS, deadline_seconds=DEADLINE_SECONDS,
                 request_timeout=REQUEST_TIMEOUT, lane='main'):
        self.provider, self.lane = provider, lane
        self.limiter = limiter or shared_limiter(provider, lane, clock=monotonic, sleep=sleep)
        self.opener = opener or default_opener          # resolved at construction (patchable in tests)
        self.clock, self.monotonic, self.sleep, self.rng = clock, monotonic, sleep, rng
        self.max_attempts, self.deadline, self.request_timeout = max_attempts, deadline_seconds, request_timeout

    def _once(self, request, timeout, max_bytes):
        """One attempt: ``(status, raw)`` or ProviderError (no secrets in it)."""
        response = None
        try:
            response = self.opener(request, timeout)
            status = getattr(response, 'status', None)
            headers = getattr(response, 'headers', None) or {}
            if type(status) is not int:
                raise ProviderError('RESPONSE_INVALID', False)
            if status != 200:
                raise _http_error(status, headers, _body(response, max_bytes))
            encoding = (headers.get('Content-Encoding') or 'identity').lower()
            if encoding != 'identity':
                raise ProviderError('RESPONSE_HEADERS_INVALID', False)
            raw = response.read(max_bytes + 1)
            if type(raw) is not bytes:
                raise ProviderError('RESPONSE_INVALID', False)
            if len(raw) > max_bytes:
                raise ProviderError('RESPONSE_OVERSIZED', False)
            length = headers.get('Content-Length')
            if length is not None and str(length).isdecimal() and int(length) != len(raw):
                raise ProviderError('RESPONSE_TRUNCATED', True)
            return status, raw
        except ProviderError:
            raise
        except HTTPError as error:
            raise _http_error(error.code, error.headers or {}, _body(error, max_bytes)) from None
        except (TimeoutError, OSError, URLError, EOFError) as error:
            transient_timeout = isinstance(error, TimeoutError) or 'timed out' in str(getattr(error, 'reason', error))
            raise ProviderError('TIMEOUT' if transient_timeout else 'CONNECTION_ERROR', True) from None
        except Exception as error:   # incl. http.client.IncompleteRead; never echo it
            code = 'RESPONSE_TRUNCATED' if type(error).__name__ == 'IncompleteRead' else 'TRANSPORT_ERROR'
            raise ProviderError(code, code == 'RESPONSE_TRUNCATED') from None
        finally:
            close = getattr(response, 'close', None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def call(self, endpoint, request_factory, *, max_bytes=MAX_RESPONSE_BYTES):
        """Return ``(raw, meta)``. ``request_factory()`` builds a fresh Request."""
        started = self.monotonic()
        attempts = 0
        last = None
        while attempts < self.max_attempts:
            remaining = self.deadline - (self.monotonic() - started)
            if remaining <= 0:
                break
            try:
                self.limiter.acquire(deadline=started + self.deadline)
            except ProviderError as error:
                last = last or error
                break
            attempts += 1
            sent_at = self.clock()
            try:
                status, raw = self._once(request_factory(), min(self.request_timeout, max(0.1, remaining)), max_bytes)
            except ProviderError as error:
                last = error
                log.debug('provider=%s endpoint=%s attempt=%d code=%s transient=%s',
                          self.provider, endpoint, attempts, error.code, error.transient)
                if not error.transient:
                    break
                retry_after = error.meta.get('retry_after')
                if retry_after is not None:
                    self.limiter.block_for(retry_after)
                delay = retry_after if retry_after is not None else backoff_delay(attempts - 1, rng=self.rng)
                if attempts >= self.max_attempts or self.monotonic() - started + delay >= self.deadline:
                    break
                self.sleep(delay)
                continue
            received_at = self.clock()
            meta = {'provider': self.provider, 'endpoint': endpoint, 'http_status': status,
                    'attempts': attempts, 'sent_at': sent_at, 'received_at': received_at,
                    'latency_s': round(received_at - sent_at, 6), 'bytes': len(raw),
                    'raw_sha256': hashlib.sha256(raw).hexdigest()}
            log.debug('provider=%s endpoint=%s attempt=%d status=200 bytes=%d', self.provider, endpoint, attempts, len(raw))
            return raw, meta
        error = last or ProviderError('DEADLINE_EXCEEDED', True)
        error.provider = self.provider
        error.meta = {**error.meta, 'endpoint': endpoint, 'attempts': attempts}
        error.__context__ = None     # drop the original transport exception: it may carry the request URL
        raise error from None

    def json(self, endpoint, request_factory, *, max_bytes=MAX_RESPONSE_BYTES):
        raw, meta = self.call(endpoint, request_factory, max_bytes=max_bytes)
        try:
            return parse_json(raw), raw, meta
        except (ValueError, UnicodeError, RecursionError, DecimalException):
            raise ProviderError('RESPONSE_MALFORMED', False, provider=self.provider, raw=raw, meta=meta) from None


def _body(response, max_bytes):
    """The (bounded) body of an error response, kept for research (e.g. a Jupiter 400 "no route"); None if unreadable."""
    try:
        raw = response.read(min(max_bytes, MAX_SMALL_RESPONSE_BYTES))
    except Exception:
        return None
    return raw if type(raw) is bytes and raw else None


def _http_error(code, headers, raw=None):
    retry_after = None
    value = headers.get('Retry-After') if hasattr(headers, 'get') else None
    if isinstance(value, str) and value.isdecimal():
        retry_after = min(float(int(value)), MAX_RETRY_AFTER)
    if code == 429:
        return ProviderError('HTTP_429', True, raw=raw, meta={'http_status': code, 'retry_after': retry_after})
    if code == 408 or 500 <= code <= 599:
        return ProviderError(f'HTTP_{code}', True, raw=raw, meta={'http_status': code, 'retry_after': retry_after})
    if code in (401, 403):
        return ProviderError('AUTH_REJECTED', False, raw=raw, meta={'http_status': code})
    if 300 <= code <= 399:
        return ProviderError('REDIRECT_REFUSED', False, raw=raw, meta={'http_status': code})
    return ProviderError(f'HTTP_{code}', False, raw=raw, meta={'http_status': code})


def _pubkey(value):
    if not isinstance(value, str) or _BASE58.fullmatch(value) is None:
        raise ProviderError('ARGUMENT_INVALID', False)
    return value


def _fail(provider, code, transient=False, raw=None, meta=None):
    return ProviderError(code, transient, provider=provider, raw=raw, meta=meta)


# ---------------------------------------------------------------- Helius

class Helius:
    URL = 'https://mainnet.helius-rpc.com/'

    def __init__(self, key, transport=None, **kwargs):
        self._key = _check_key(key)
        self.transport = transport or Transport('helius', **kwargs)

    def __repr__(self):
        return 'Helius(key=<redacted>)'

    def rpc(self, method, params):
        if method not in READ_ONLY_RPC:
            raise _fail('helius', 'METHOD_NOT_ALLOWED')
        if not isinstance(params, (list, tuple)):
            raise _fail('helius', 'ARGUMENT_INVALID')
        try:
            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params},
                              allow_nan=False, default=_json_default).encode()
        except (TypeError, ValueError):
            raise _fail('helius', 'ARGUMENT_INVALID') from None

        def request():
            return Request(self.URL + '?' + urlencode({'api-key': self._key}), data=body, method='POST',
                           headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
        parsed, raw, meta = self.transport.json('rpc:' + method, request)
        if not isinstance(parsed, dict) or parsed.get('id') != 1 or parsed.get('jsonrpc') != '2.0':
            raise _fail('helius', 'RESPONSE_INVALID', raw=raw, meta=meta)
        if 'error' in parsed:
            error = parsed['error']
            code = error.get('code') if isinstance(error, dict) else None
            meta = {**meta, 'rpc_error_code': code if isinstance(code, int) else None}
            raise _fail('helius', 'RPC_ERROR', isinstance(code, int) and code in TRANSIENT_RPC_CODES, raw=raw, meta=meta)
        if 'result' not in parsed:
            raise _fail('helius', 'RESPONSE_INVALID', raw=raw, meta=meta)
        return parsed['result'], raw, meta

    def get_multiple_accounts(self, pubkeys):
        keys = list(pubkeys)
        if not 1 <= len(keys) <= MAX_MULTIPLE_ACCOUNTS:
            raise _fail('helius', 'ARGUMENT_INVALID')
        for k in keys:
            _pubkey(k)
        result, raw, meta = self.rpc('getMultipleAccounts', [keys, {'encoding': 'base64', 'commitment': 'confirmed'}])
        validate_multiple_accounts(result, len(keys), raw=raw, meta=meta)
        return result, raw, meta


def validate_multiple_accounts(result, count, *, raw=None, meta=None):
    """THE getMultipleAccounts shape, used by every lean caller: the RPC ``result`` object unchanged,
    ``{'context': {'slot': int}, 'value': [None | {'data': [base64, 'base64'], 'owner': str, 'lamports': int,
    'executable': bool, ...}]}``, exactly as the desk decoders (``account_bytes``, ``mint_policy``, ``parse_pool``)
    expect it. Raises ``RESPONSE_INVALID`` (with the raw bytes) otherwise."""
    try:
        if not isinstance(result, dict) or not isinstance(result.get('context'), dict):
            raise ValueError
        slot, value = result['context'].get('slot'), result.get('value')
        if type(slot) is not int or slot < 0 or not isinstance(value, list) or len(value) != count:
            raise ValueError
        for item in value:
            if item is None:
                continue
            data = item.get('data') if isinstance(item, dict) else None
            if (not isinstance(data, list) or len(data) != 2 or data[1] != 'base64' or not isinstance(data[0], str)
                    or not isinstance(item.get('owner'), str) or type(item.get('lamports')) is not int
                    or type(item.get('executable')) is not bool):
                raise ValueError
            base64.b64decode(data[0], validate=True)
    except (ValueError, TypeError, AttributeError):
        raise _fail('helius', 'RESPONSE_INVALID', raw=raw, meta=meta) from None
    return result


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError('unsupported')


# ---------------------------------------------------------------- Jupiter

@dataclass(frozen=True)
class Quote:
    """Validated swap quote. Amounts are exact integers in base units."""
    in_amount: int
    out_amount: int
    other_amount_threshold: 'int | None'
    price_impact_pct: 'Decimal | None'
    route_labels: tuple


def _u64(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 20:
        n = int(value)
        if 0 <= n < 2 ** 64:
            return n
    raise ValueError('u64')


def parse_quote(parsed, requested_amount, *, input_mint, output_mint):
    """Pure strict parser; raises ValueError on malformed or contradictory quotes. The response must echo the request:
    same input and output mint, ``swapMode`` ExactIn and ``inAmount`` equal to the requested amount."""
    if not isinstance(parsed, dict):
        raise ValueError('object')
    if parsed.get('inputMint') != input_mint or parsed.get('outputMint') != output_mint:
        raise ValueError('mints')
    if parsed.get('swapMode') != 'ExactIn':
        raise ValueError('swap mode')
    in_amount, out_amount = _u64(parsed.get('inAmount')), _u64(parsed.get('outAmount'))
    if in_amount != requested_amount or out_amount <= 0:
        raise ValueError('amounts')
    route = parsed.get('routePlan')
    if not isinstance(route, list) or not 1 <= len(route) <= 16:
        raise ValueError('route')
    labels = []
    for hop in route:
        info = hop.get('swapInfo') if isinstance(hop, dict) else None
        if not isinstance(info, dict):
            raise ValueError('hop')
        labels.append(str(info.get('label', ''))[:64])
    threshold = parsed.get('otherAmountThreshold')
    threshold = None if threshold is None else _u64(threshold)
    if threshold is not None and threshold > out_amount:
        raise ValueError('threshold')
    impact = parsed.get('priceImpactPct', parsed.get('priceImpact'))
    if impact is not None:
        impact = Decimal(str(impact))
        if not impact.is_finite():
            raise ValueError('impact')
    return Quote(in_amount, out_amount, threshold, impact, tuple(labels))


class Jupiter:
    BASE = 'https://api.jup.ag'

    def __init__(self, key, transport=None, **kwargs):
        self._key = _check_key(key)
        self.transport = transport or Transport('jupiter', **kwargs)

    def __repr__(self):
        return 'Jupiter(key=<redacted>)'

    def _request(self, path, query):
        def build():
            return Request(self.BASE + path + '?' + urlencode(query), method='GET',
                           headers={'Accept': 'application/json', 'x-api-key': self._key})
        return build

    def quote(self, input_mint, output_mint, amount, taker, *, slippage_bps=100):
        _pubkey(input_mint), _pubkey(output_mint), _pubkey(taker)
        if type(amount) is not int or not 0 < amount < 2 ** 64 or type(slippage_bps) is not int or not 0 <= slippage_bps <= 10000:
            raise _fail('jupiter', 'ARGUMENT_INVALID')
        query = {'inputMint': input_mint, 'outputMint': output_mint, 'amount': str(amount), 'taker': taker,
                 'slippageBps': str(slippage_bps), 'transactionVersion': '0'}
        parsed, raw, meta = self.transport.json('quote', self._request('/swap/v2/build', query))
        try:
            quote = parse_quote(parsed, amount, input_mint=input_mint, output_mint=output_mint)
        except (ValueError, TypeError, DecimalException):
            raise _fail('jupiter', 'QUOTE_INVALID', raw=raw, meta=meta) from None
        return quote, raw, meta

    def price(self, mints):
        """USD prices keyed by mint. Missing or malformed entries are listed in meta, not raised,
        so one bad token cannot fail the batch."""
        ids = list(mints)
        if not 1 <= len(ids) <= MAX_PRICE_IDS or len(set(ids)) != len(ids):
            raise _fail('jupiter', 'ARGUMENT_INVALID')
        for m in ids:
            _pubkey(m)
        parsed, raw, meta = self.transport.json('price', self._request('/price/v3', {'ids': ','.join(ids)}))
        if not isinstance(parsed, dict):
            raise _fail('jupiter', 'RESPONSE_INVALID', raw=raw, meta=meta)
        prices, invalid, missing = {}, [], []
        for mint in ids:
            item = parsed.get(mint)
            if item is None:
                missing.append(mint)
                continue
            value = item.get('usdPrice') if isinstance(item, dict) else None
            if type(value) not in (int, Decimal) or isinstance(value, bool) or not Decimal(value).is_finite() or value <= 0:
                invalid.append(mint)
                continue
            prices[mint] = Decimal(value)
        return prices, raw, {**meta, 'missing': missing, 'invalid': invalid}


# ---------------------------------------------------------------- Kraken

class Kraken:
    def __init__(self, transport=None, **kwargs):
        self.transport = transport or Transport('kraken', **kwargs)

    def sol_usd(self):
        """Latest SOLUSD trade price (Decimal), validated and freshness-checked by the desk's
        pure strict parser (``desk.kraken_usd_observation``)."""
        from desk.kraken_usd_observation import (URL, KrakenTradesResponse, TrustedTimeBounds,
                                                 parse_kraken_usd)

        def build():
            return Request(URL, method='GET', headers={'Accept': 'application/json'})
        raw, meta = self.transport.call('sol_usd', build, max_bytes=MAX_SMALL_RESPONSE_BYTES)
        acquired = format(Decimal(repr(meta['received_at'])), 'f')
        try:
            observation = parse_kraken_usd(KrakenTradesResponse('GET', URL, raw, acquired, meta['http_status']),
                                           bounds=TrustedTimeBounds(acquired))
        except (ValueError, DecimalException):
            raise _fail('kraken', 'RESPONSE_INVALID', raw=raw, meta=meta) from None
        if observation.status != 'MEASURED':
            if b'Rate limit' in raw:     # Kraken reports throttling as HTTP 200 with an error entry
                self.transport.limiter.block_for(MAX_RETRY_AFTER)
                raise _fail('kraken', 'KRAKEN_RATE_LIMITED', True, raw=raw, meta=meta)
            stale = {'TRADE_STALE_OR_FUTURE', 'ACQUISITION_STALE_OR_FUTURE'}
            raise _fail('kraken', observation.blockers[0] if observation.blockers else 'RESPONSE_INVALID',
                        bool(stale & set(observation.blockers)), raw=raw,
                        meta={**meta, 'blockers': list(observation.blockers)})
        return observation.usd_price, raw, {**meta, 'trade_at': observation.trade_at}


# ---------------------------------------------------------------- the providers object

# Jupiter's swap/v2/build quote needs a syntactically valid taker. PAPER ONLY: this is the public address the desk's paper
# quotes already use (config/*.json "taker"). It is only an identity for route building; no private key for it exists in
# this repository, nothing here signs or submits, and the built transaction is discarded.
PAPER_TAKER = '6E2G75Z3uJEnPo9EvzmLTxp8KB78m3RDsFBjoCTVHZD2'


@dataclass(frozen=True)
class Providers:
    """What ``lean.candidates.screen`` and the runner expect: ``.helius``, ``.jupiter``, ``.kraken``."""
    helius: object
    jupiter: object
    kraken: object


def build_providers(keys, *, lane='main', **transport_kwargs):
    """Clients for one lane (``main`` for candidates, ``exit`` for held positions), on the process-wide limiters."""
    def transport(provider):
        return Transport(provider, lane=lane, **transport_kwargs)
    return Providers(Helius(keys['helius'], transport('helius')), Jupiter(keys['jupiter'], transport('jupiter')),
                     Kraken(transport('kraken')))
