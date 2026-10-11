"""lean.providers: fake opener only. Fixtures are synthetic (shape follows public API docs).
No network: socket.connect is patched to fail for the whole module."""
import base64
import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import traceback
import unittest
from decimal import Decimal
from unittest import mock
from urllib.error import HTTPError, URLError

from lean import providers as P

KEY = 'SECRETKEY0123456789abcdefSECRET'
MINT_A = 'So11111111111111111111111111111111111111112'
MINT_B = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
TAKER = '11111111111111111111111111111111'


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class Clock:
    def __init__(self, start=1000.0):
        self.t = start
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class Resp:
    def __init__(self, body=b'{}', status=200, headers=None):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status = status
        self.headers = headers or {}
        self.closed = False

    def read(self, n):
        return self.body[:n]

    def close(self):
        self.closed = True


class Opener:
    """Scripted: each item is a Resp, an exception instance, or a callable(request)."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if callable(item):
            item = item(request)
        if isinstance(item, BaseException):
            raise item
        return item


def transport(provider, opener, clock=None, **kw):
    clock = clock or Clock()
    t = P.Transport(provider, opener=opener, clock=clock.now, monotonic=clock.now, sleep=clock.sleep,
                    rng=lambda: 0.5, **kw)
    t.test_clock = clock
    return t


class RateLimiterTests(unittest.TestCase):
    def test_burst_then_paced_total_wait(self):
        c = Clock()
        r = P.RateLimiter(10, 10, clock=c.now, sleep=c.sleep)
        for _ in range(25):
            r.acquire()
        self.assertAlmostEqual(sum(c.sleeps), 1.5)          # 15 tokens beyond the burst at 10/s

    def test_kraken_default_is_one_request_per_two_seconds(self):
        c = Clock()
        rate, burst = P.DEFAULT_RATES['kraken']
        r = P.RateLimiter(rate, burst, clock=c.now, sleep=c.sleep)
        r.acquire(); self.assertEqual(c.sleeps, [])
        r.acquire(); self.assertEqual(c.sleeps, [2.0])
        self.assertEqual(P.DEFAULT_RATES['helius'][0], 10.0)
        self.assertEqual(P.DEFAULT_RATES['jupiter'][0], 4.0)

    def test_refill_after_idle_is_capped_at_burst(self):
        c = Clock()
        r = P.RateLimiter(4, 4, clock=c.now, sleep=c.sleep)
        for _ in range(4):
            r.acquire()
        c.t += 1000
        for _ in range(4):
            r.acquire()
        self.assertEqual(c.sleeps, [])
        r.acquire()
        self.assertEqual(len(c.sleeps), 1)

    def test_block_for_delays_next_caller(self):
        c = Clock()
        r = P.RateLimiter(10, 10, clock=c.now, sleep=c.sleep)
        r.block_for(7)
        r.acquire()
        self.assertEqual(c.sleeps, [7.0])

    def test_concurrent_callers_never_over_issue(self):
        r = P.RateLimiter(200, 1)
        stamps = []
        lock = threading.Lock()

        def work():
            for _ in range(10):
                r.acquire()
                with lock:
                    stamps.append(time.monotonic())
        threads = [threading.Thread(target=work) for _ in range(4)]
        started = time.monotonic()
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(len(stamps), 40)
        self.assertGreaterEqual(max(stamps) - started, 39 / 200 * 0.9)   # 39 paced tokens at 200/s

    def test_invalid_rates_rejected(self):
        for rate in (0, -1, 'x', float('nan')):
            with self.assertRaises(ValueError):
                P.RateLimiter(rate)

    def test_backoff_is_exponential_jittered_and_capped(self):
        low = [P.backoff_delay(n, rng=lambda: 0.0) for n in range(8)]
        high = [P.backoff_delay(n, rng=lambda: 1.0) for n in range(8)]
        self.assertEqual(low[:3], [0.25, 0.5, 1.0])
        self.assertEqual(high[:3], [0.5, 1.0, 2.0])
        self.assertEqual(high[-1], P.BACKOFF_CAP)
        self.assertTrue(all(l < h for l, h in zip(low, high)))


class TransportTests(unittest.TestCase):
    def call(self, t, max_bytes=P.MAX_RESPONSE_BYTES):
        return t.call('e', lambda: P.Request('https://x.invalid/?api-key=' + KEY), max_bytes=max_bytes)

    def test_success_meta_and_raw(self):
        t = transport('helius', Opener(Resp(b'{"a":1}')))
        raw, meta = self.call(t)
        self.assertEqual(raw, b'{"a":1}')
        self.assertEqual((meta['attempts'], meta['http_status'], meta['bytes']), (1, 200, 7))
        self.assertEqual(len(meta['raw_sha256']), 64)

    def test_transient_http_retried_with_backoff_then_succeeds(self):
        o = Opener(Resp(status=503), Resp(status=502), Resp(b'{}'))
        t = transport('jupiter', o)
        raw, meta = self.call(t)
        self.assertEqual(meta['attempts'], 3)
        self.assertEqual(len(o.requests), 3)
        self.assertEqual(t.test_clock.sleeps[:2], [0.375, 0.75])   # rng 0.5: [d/2,d] midpoint

    def test_429_honours_retry_after_and_blocks_limiter(self):
        o = Opener(Resp(status=429, headers={'Retry-After': '5'}), Resp(b'{}'))
        t = transport('jupiter', o)
        _, meta = self.call(t)
        self.assertEqual(meta['attempts'], 2)
        self.assertIn(5.0, t.test_clock.sleeps)

    def test_retry_after_is_bounded(self):
        o = Opener(Resp(status=429, headers={'Retry-After': '99999'}), Resp(b'{}'))
        t = transport('jupiter', o, deadline_seconds=1000)
        self.call(t)
        self.assertEqual(max(t.test_clock.sleeps), P.MAX_RETRY_AFTER)

    def test_retry_after_longer_than_deadline_fails_fast_instead_of_sleeping(self):
        o = Opener(Resp(status=429, headers={'Retry-After': '30'}), Resp(b'{}'))
        t = transport('jupiter', o)          # default 20s deadline
        with self.assertRaises(P.ProviderError) as cm:
            self.call(t)
        self.assertEqual((cm.exception.code, cm.exception.transient), ('HTTP_429', True))
        self.assertEqual(t.test_clock.sleeps, [])

    def test_exhausted_retries_raise_last_transient_error(self):
        o = Opener(Resp(status=500))
        t = transport('helius', o)
        with self.assertRaises(P.ProviderError) as cm:
            self.call(t)
        e = cm.exception
        self.assertEqual((e.code, e.transient, e.provider, e.meta['attempts']), ('HTTP_500', True, 'helius', 3))
        self.assertEqual(len(o.requests), 3)

    def test_non_transient_http_not_retried(self):
        for status, code in ((400, 'HTTP_400'), (404, 'HTTP_404'), (401, 'AUTH_REJECTED'), (403, 'AUTH_REJECTED'), (302, 'REDIRECT_REFUSED')):
            o = Opener(Resp(status=status))
            with self.assertRaises(P.ProviderError) as cm:
                self.call(transport('helius', o))
            self.assertEqual((cm.exception.code, cm.exception.transient, len(o.requests)), (code, False, 1), status)

    def test_network_failures_are_transient(self):
        cases = {'TIMEOUT': TimeoutError('timed out'), 'CONNECTION_ERROR': ConnectionResetError('reset'),
                 'TIMEOUT ': URLError(TimeoutError('timed out')), 'CONNECTION_ERROR ': URLError(OSError('refused'))}
        for code, exc in cases.items():
            with self.assertRaises(P.ProviderError) as cm:
                self.call(transport('helius', Opener(exc)))
            self.assertEqual((cm.exception.code, cm.exception.transient), (code.strip(), True))

    def test_http_error_exceptions_are_typed(self):
        e = HTTPError('https://x.invalid/?api-key=' + KEY, 429, 'Too Many', {'Retry-After': '1'}, None)
        o = Opener(e, Resp(b'{}'))
        _, meta = self.call(transport('jupiter', o))
        self.assertEqual(meta['attempts'], 2)

    def test_deadline_stops_retries(self):
        c = Clock()
        o = Opener(Resp(status=503))
        t = transport('helius', o, c, deadline_seconds=0.3, max_attempts=10)
        with self.assertRaises(P.ProviderError) as cm:
            self.call(t)
        self.assertLess(len(o.requests), 10)
        self.assertTrue(cm.exception.transient)

    def test_oversized_truncated_and_encoded_bodies(self):
        with self.assertRaises(P.ProviderError) as cm:
            self.call(transport('helius', Opener(Resp(b'x' * 100))), max_bytes=10)
        self.assertEqual((cm.exception.code, cm.exception.transient), ('RESPONSE_OVERSIZED', False))
        with self.assertRaises(P.ProviderError) as cm:
            self.call(transport('helius', Opener(Resp(b'abc', headers={'Content-Length': '99'}))))
        self.assertEqual((cm.exception.code, cm.exception.transient), ('RESPONSE_TRUNCATED', True))
        with self.assertRaises(P.ProviderError) as cm:
            self.call(transport('helius', Opener(Resp(b'abc', headers={'Content-Encoding': 'gzip'}))))
        self.assertEqual((cm.exception.code, cm.exception.transient), ('RESPONSE_HEADERS_INVALID', False))

    def test_responses_are_closed(self):
        r = Resp(b'{}')
        self.call(transport('helius', Opener(r)))
        self.assertTrue(r.closed)

    def test_malformed_json_is_non_transient_and_keeps_raw(self):
        for body in (b'{', b'\xff\xfe', b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'[' * 5000):
            with self.assertRaises(P.ProviderError) as cm:
                transport('helius', Opener(Resp(body))).json('e', lambda: P.Request('https://x.invalid/'))
            e = cm.exception
            self.assertEqual((e.code, e.transient, e.raw), ('RESPONSE_MALFORMED', False, body), body[:10])

    def test_default_opener_refuses_redirects(self):
        handler = P._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, 'x', {}, 'http://elsewhere'))


class SecretLeakTests(unittest.TestCase):
    """The key must not appear in any exception text, chain, repr, meta or log record."""

    def assert_clean(self, text):
        self.assertNotIn(KEY, text)
        self.assertNotIn('SECRETKEY', text)

    def run_failure(self, exc_factory):
        def boom(request):
            return exc_factory(request.full_url)
        h = P.Helius(KEY, transport('helius', Opener(boom)))
        with self.assertLogs('lean.providers', level='DEBUG') as logs:
            with self.assertRaises(P.ProviderError) as cm:
                h.rpc('getSlot', [])
        e = cm.exception
        text = ' '.join([str(e), repr(e), ''.join(traceback.format_exception(e)), json.dumps(e.meta, default=str),
                         ' '.join(logs.output), repr(h), repr(h.transport.__dict__.get('provider'))])
        self.assert_clean(text)
        self.assertIsNone(e.__cause__)
        self.assertIsNone(e.__context__)
        self.assertTrue(e.__suppress_context__)
        return e

    def test_urlerror_reason_containing_url(self):
        self.run_failure(lambda url: URLError(f'tunnel failed for {url}'))

    def test_httperror_with_url(self):
        self.run_failure(lambda url: HTTPError(url, 500, 'boom ' + url, {}, None))

    def test_oserror_with_url(self):
        self.run_failure(lambda url: OSError(f'cannot reach {url}'))

    def test_generic_exception_with_url(self):
        self.run_failure(lambda url: RuntimeError(f'weird {url}'))

    def test_success_path_logs_never_contain_key(self):
        h = P.Helius(KEY, transport('helius', Opener(rpc_ok(5))))
        with self.assertLogs('lean.providers', level='DEBUG') as logs:
            h.rpc('getSlot', [])
        self.assertTrue(logs.output)
        self.assert_clean(' '.join(logs.output))
        with self.assertLogs('lean.providers', level='DEBUG') as logs:
            with self.assertRaises(P.ProviderError):
                P.Jupiter(KEY, transport('jupiter', Opener(Resp(status=503)))).price([MINT_A])
        self.assertGreaterEqual(len(logs.output), 3)
        self.assert_clean(' '.join(logs.output))

    def test_provider_body_echoing_key_is_not_surfaced_in_error_text(self):
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32000, 'message': 'bad ' + KEY}}).encode()
        h = P.Helius(KEY, transport('helius', Opener(Resp(body))))
        with self.assertRaises(P.ProviderError) as cm:
            h.rpc('getSlot', [])
        self.assert_clean(str(cm.exception) + repr(cm.exception) + json.dumps(cm.exception.meta, default=str))

    def test_key_validation_errors_do_not_echo(self):
        for bad in ('', 'has space', 'x' * 513, 'tab\tkey', None, 5):
            with self.assertRaises(P.ProviderError) as cm:
                P.Helius(bad)
            self.assertNotIn(str(bad) if bad else 'zzz', str(cm.exception) or 'zzz')
        for cls in (P.Helius, P.Jupiter):
            self.assertNotIn(KEY, repr(cls(KEY, transport('helius', Opener(Resp())))))

    def test_key_only_in_intended_request_location(self):
        o = Opener(Resp({'jsonrpc': '2.0', 'id': 1, 'result': 5}))
        P.Helius(KEY, transport('helius', o)).rpc('getSlot', [])
        req = o.requests[0][0]
        self.assertIn('api-key=' + KEY, req.full_url)
        self.assertNotIn(KEY, (req.data or b'').decode())
        self.assertNotIn(KEY, json.dumps(dict(req.header_items())))
        o = Opener(Resp({}))
        P.Jupiter(KEY, transport('jupiter', o)).price([MINT_A])
        req = o.requests[0][0]
        self.assertNotIn(KEY, req.full_url)
        self.assertEqual(req.get_header('X-api-key'), KEY)


class KeyFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))

    def write(self, text, mode=0o600, name='keys.json'):
        p = self.dir / name
        p.write_text(text)
        p.chmod(mode)
        return p

    def test_loads_private_file(self):
        self.assertEqual(P.load_keys(self.write(json.dumps({'helius': KEY, 'jupiter': 'j'}))), {'helius': KEY, 'jupiter': 'j'})

    def test_refuses_group_world_readable_symlink_and_bad_content(self):
        good = self.write(json.dumps({'helius': KEY}), name='good.json')
        link = self.dir / 'link.json'
        link.symlink_to(good)
        cases = [self.write(json.dumps({'helius': KEY}), 0o644, 'open.json'), link, self.dir / 'missing.json',
                 self.write('{', name='bad.json'), self.write('[]', name='list.json'), self.write('{}', name='empty.json'),
                 self.write(json.dumps({'helius': 5}), name='int.json'),
                 self.write(json.dumps({'helius': 'a b'}), name='space.json')]
        for case in cases:
            with self.assertRaises(P.ProviderError) as cm:
                P.load_keys(case)
            self.assertEqual((cm.exception.code, cm.exception.transient), ('KEY_FILE_INVALID', False))
            self.assertNotIn(KEY, str(cm.exception) + repr(cm.exception))


def rpc_ok(result, ident=1):
    return Resp({'jsonrpc': '2.0', 'id': ident, 'result': result})


def account(data=b'\x01\x02', owner=MINT_B):
    return {'data': [base64.b64encode(data).decode(), 'base64'], 'executable': False, 'lamports': 5, 'owner': owner}


class HeliusTests(unittest.TestCase):
    def test_write_and_unknown_methods_refused_before_any_io(self):
        o = Opener(rpc_ok(1))
        h = P.Helius(KEY, transport('helius', o))
        for method in ('sendTransaction', 'sendBundle', 'simulateTransaction', 'requestAirdrop', 'signTransaction',
                       'GETSLOT', '', None, 'getSlot ', 'getAccountInfo\n'):
            with self.assertRaises(P.ProviderError) as cm:
                h.rpc(method, [])
            self.assertEqual((cm.exception.code, cm.exception.transient), ('METHOD_NOT_ALLOWED', False), method)
        self.assertEqual(o.requests, [])
        self.assertFalse(any(m.startswith(('send', 'sign', 'simulate', 'request')) for m in P.READ_ONLY_RPC))

    def test_rpc_roundtrip_and_request_shape(self):
        o = Opener(rpc_ok(123))
        result, raw, meta = P.Helius(KEY, transport('helius', o)).rpc('getSlot', [])
        self.assertEqual(result, 123)
        req = o.requests[0][0]
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(json.loads(req.data), {'jsonrpc': '2.0', 'id': 1, 'method': 'getSlot', 'params': []})
        self.assertEqual(meta['endpoint'], 'rpc:getSlot')

    def test_rpc_error_codes(self):
        def err(code):
            return Resp({'jsonrpc': '2.0', 'id': 1, 'error': {'code': code, 'message': 'm'}})
        h = P.Helius(KEY, transport('helius', Opener(err(-32005))))
        with self.assertRaises(P.ProviderError) as cm:
            h.rpc('getSlot', [])
        self.assertEqual((cm.exception.code, cm.exception.transient, cm.exception.meta['rpc_error_code']), ('RPC_ERROR', True, -32005))
        h = P.Helius(KEY, transport('helius', Opener(err(-32602))))
        with self.assertRaises(P.ProviderError) as cm:
            h.rpc('getSlot', [])
        self.assertFalse(cm.exception.transient)

    def test_contradictory_envelopes_rejected(self):
        for body in ({'jsonrpc': '2.0', 'id': 2, 'result': 1}, {'jsonrpc': '2.0', 'id': 1}, [1], {'id': 1, 'result': 1},
                     {'jsonrpc': '1.0', 'id': 1, 'result': 1}):
            with self.assertRaises(P.ProviderError) as cm:
                P.Helius(KEY, transport('helius', Opener(Resp(body)))).rpc('getSlot', [])
            self.assertEqual((cm.exception.code, cm.exception.transient), ('RESPONSE_INVALID', False), body)
            self.assertTrue(cm.exception.raw)

    def test_get_multiple_accounts(self):
        value = [account(), None, account(b'\x00' * 82)]
        o = Opener(rpc_ok({'context': {'slot': 77}, 'value': value}))
        parsed, raw, meta = P.Helius(KEY, transport('helius', o)).get_multiple_accounts([MINT_A, MINT_B, TAKER])
        self.assertEqual(parsed['slot'], 77)
        self.assertEqual(parsed['accounts'][0]['data'], b'\x01\x02')
        self.assertIsNone(parsed['accounts'][1])
        self.assertEqual(len(parsed['accounts'][2]['data']), 82)
        params = json.loads(o.requests[0][0].data)['params']
        self.assertEqual(params[0], [MINT_A, MINT_B, TAKER])
        self.assertEqual(params[1]['encoding'], 'base64')

    def test_get_multiple_accounts_rejects_bad_shapes(self):
        bad = [rpc_ok({'value': [account(), account()]}), rpc_ok({'value': 'x'}), rpc_ok({'value': [None, None]}),
               rpc_ok({'value': [{'data': ['!!!', 'base64'], 'lamports': 1, 'owner': MINT_B}]}),
               rpc_ok({'value': [{'data': ['AA==', 'base58'], 'lamports': 1, 'owner': MINT_B}]}),
               rpc_ok({'value': [{'data': ['AA==', 'base64'], 'lamports': 1.5, 'owner': MINT_B}]}),
               rpc_ok({'value': [{'data': 'AA==', 'lamports': 1, 'owner': MINT_B}]}), rpc_ok(None)]
        for response in bad:
            with self.assertRaises(P.ProviderError) as cm:
                P.Helius(KEY, transport('helius', Opener(response))).get_multiple_accounts([MINT_A, MINT_B][:1])
            self.assertEqual(cm.exception.code, 'RESPONSE_INVALID')
            self.assertTrue(cm.exception.raw)

    def test_argument_validation_before_io(self):
        o = Opener(rpc_ok({}))
        h = P.Helius(KEY, transport('helius', o))
        for keys in ([], ['short'], [MINT_A] * 101, ['0' * 40], [5], [MINT_A + 'x' * 10]):
            with self.assertRaises(P.ProviderError) as cm:
                h.get_multiple_accounts(keys)
            self.assertEqual(cm.exception.code, 'ARGUMENT_INVALID')
        self.assertEqual(o.requests, [])


def build_response(in_amount='1000', out='5000', threshold='4900', route=None):
    r = {'inAmount': in_amount, 'outAmount': out, 'otherAmountThreshold': threshold, 'priceImpactPct': '0.01',
         'routePlan': [{'swapInfo': {'label': 'PumpSwap'}}] if route is None else route}
    return {k: v for k, v in r.items() if v is not None}


class JupiterTests(unittest.TestCase):
    def quote(self, body, amount=1000):
        o = Opener(Resp(body))
        out = P.Jupiter(KEY, transport('jupiter', o)).quote(MINT_A, MINT_B, amount, TAKER)
        return out, o

    def test_quote_ok(self):
        (q, raw, meta), o = self.quote(build_response())
        self.assertEqual((q.in_amount, q.out_amount, q.other_amount_threshold, q.price_impact_pct, q.route_labels),
                         (1000, 5000, 4900, Decimal('0.01'), ('PumpSwap',)))
        self.assertEqual(meta['endpoint'], 'quote')
        url = o.requests[0][0].full_url
        self.assertTrue(url.startswith('https://api.jup.ag/swap/v2/build?'))
        self.assertIn('amount=1000', url)

    def test_quote_rejects_malformed_and_contradictory(self):
        bad = [build_response(in_amount='999'), build_response(out='0'), build_response(out='-5'), build_response(out='1.5'),
               build_response(out=5000), build_response(threshold='9999999'), build_response(route=[]),
               build_response(route=[{}]), build_response(out=str(2 ** 64)), build_response(in_amount=None), [1], 'x']
        for body in bad:
            with self.assertRaises(P.ProviderError) as cm:
                self.quote(body)
            e = cm.exception
            self.assertIn(e.code, ('QUOTE_INVALID',), body)
            self.assertFalse(e.transient)
            self.assertTrue(e.raw)

    def test_quote_arguments(self):
        o = Opener(Resp(build_response()))
        j = P.Jupiter(KEY, transport('jupiter', o))
        for args in ((MINT_A, MINT_B, 0, TAKER), (MINT_A, MINT_B, -1, TAKER), (MINT_A, MINT_B, 2 ** 64, TAKER),
                     (MINT_A, MINT_B, True, TAKER), (MINT_A, MINT_B, '5', TAKER), ('bad', MINT_B, 5, TAKER)):
            with self.assertRaises(P.ProviderError):
                j.quote(*args)
        self.assertEqual(o.requests, [])

    def test_price_isolates_bad_tokens(self):
        body = {MINT_A: {'usdPrice': 150.25, 'decimals': 9}, MINT_B: {'usdPrice': -1}}
        (prices, raw, meta), = [P.Jupiter(KEY, transport('jupiter', Opener(Resp(body)))).price([MINT_A, MINT_B, TAKER])]
        self.assertEqual(prices, {MINT_A: Decimal('150.25')})
        self.assertEqual((meta['invalid'], meta['missing']), ([MINT_B], [TAKER]))

    def test_price_invalid_values(self):
        for value in ('1', None, True, 0, -2, {'x': 1}, [1]):
            body = {MINT_A: {'usdPrice': value}}
            prices, _, meta = P.Jupiter(KEY, transport('jupiter', Opener(Resp(body)))).price([MINT_A])
            self.assertEqual((prices, meta['invalid']), ({}, [MINT_A]), value)

    def test_price_nan_body_is_malformed(self):
        with self.assertRaises(P.ProviderError) as cm:
            P.Jupiter(KEY, transport('jupiter', Opener(Resp(b'{"%s":{"usdPrice":NaN}}' % MINT_A.encode())))).price([MINT_A])
        self.assertEqual(cm.exception.code, 'RESPONSE_MALFORMED')

    def test_price_argument_limits(self):
        j = P.Jupiter(KEY, transport('jupiter', Opener(Resp({}))))
        for ids in ([], [MINT_A, MINT_A], [MINT_A] * 51):
            with self.assertRaises(P.ProviderError):
                j.price(ids)


def kraken_body(price='150.12', trade_at=1000.0, error=None, count=1):
    rows = [[price, '1.5', trade_at, 'b', 'l', '', 123456]] * count
    body = {'error': error or [], 'result': {'SOLUSD': rows, 'last': '1791417600000000000'}}
    return json.dumps(body).encode()


class KrakenTests(unittest.TestCase):
    def test_sol_usd_ok(self):
        c = Clock(1002.0)
        price, raw, meta = P.Kraken(transport('kraken', Opener(Resp(kraken_body())), c)).sol_usd()
        self.assertEqual(price, Decimal('150.12'))
        self.assertEqual(meta['endpoint'], 'sol_usd')
        self.assertEqual(Decimal(meta['trade_at']), 1000)

    def test_consecutive_calls_are_paced_two_seconds_apart(self):
        c = Clock(1002.0)
        k = P.Kraken(transport('kraken', Opener(Resp(kraken_body(trade_at=1000.0))), c))
        k.sol_usd()
        self.assertEqual(c.sleeps, [])
        k.sol_usd()
        self.assertEqual(c.sleeps, [2.0])

    def test_invalid_and_stale_trades(self):
        cases = [(kraken_body(trade_at=900.0), 'TRADE_STALE_OR_FUTURE', True),
                 (kraken_body(trade_at=1010.0), 'TRADE_STALE_OR_FUTURE', True),
                 (kraken_body(price='0'), 'KRAKEN_RESPONSE_INVALID', False),
                 (kraken_body(price='abc'), 'KRAKEN_RESPONSE_INVALID', False),
                 (kraken_body(count=2), 'KRAKEN_RESPONSE_INVALID', False),
                 (kraken_body(error=['EGeneral:Internal error']), 'KRAKEN_RESPONSE_INVALID', False),
                 (b'{"error":[],"result":{}}', 'KRAKEN_RESPONSE_INVALID', False)]
        for body, code, transient in cases:
            with self.assertRaises(P.ProviderError) as cm:
                P.Kraken(transport('kraken', Opener(Resp(body)), Clock(1002.0))).sol_usd()
            self.assertEqual((cm.exception.code, cm.exception.transient), (code, transient), body)
            self.assertEqual(cm.exception.raw, body)

    def test_kraken_throttle_in_body_is_transient_and_blocks_limiter(self):
        t = transport('kraken', Opener(Resp(kraken_body(error=['EAPI:Rate limit exceeded']))), Clock(1002.0))
        with self.assertRaises(P.ProviderError) as cm:
            P.Kraken(t).sol_usd()
        self.assertEqual((cm.exception.code, cm.exception.transient), ('KRAKEN_RATE_LIMITED', True))
        t.test_clock.sleeps.clear()
        t.limiter.acquire()
        self.assertGreaterEqual(t.test_clock.sleeps[0], P.MAX_RETRY_AFTER - 1)

    def test_malformed_http_is_typed(self):
        with self.assertRaises(P.ProviderError) as cm:
            P.Kraken(transport('kraken', Opener(Resp(status=500)), Clock(1002.0))).sol_usd()
        self.assertEqual((cm.exception.code, cm.exception.transient), ('HTTP_500', True))


class NoNetworkTests(unittest.TestCase):
    def test_default_opener_would_hit_patched_socket(self):
        """Proves the module-level socket guard is active, so every other test is hermetic."""
        with socket.socket() as s, self.assertRaises(AssertionError):
            s.connect(('127.0.0.1', 9))


if __name__ == '__main__':
    unittest.main()
