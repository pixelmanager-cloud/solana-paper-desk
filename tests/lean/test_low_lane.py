"""L07R: the shared LOW-priority provider lane (lean.providers). Fake openers and clocks only; no network."""
import socket
import threading
import time
import unittest
from unittest import mock

from lean import providers as P

KEY = 'TEST-KEY-0123456789'
KEYS = {'helius': KEY, 'jupiter': KEY}


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class Clock:
    def __init__(self, start=1000.0):
        self.t, self.sleeps = start, []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class Resp:
    def __init__(self, body=b'{}', status=200, headers=None):
        self.body, self.status, self.headers = body, status, headers or {}

    def read(self, n):
        return self.body[:n]

    def close(self):
        pass


class Opener:
    """Answers every request with ``make()``; counts requests (thread safe)."""

    def __init__(self, make=lambda: Resp()):
        self.make, self.count, self._lock = make, 0, threading.Lock()

    def __call__(self, request, timeout):
        with self._lock:
            self.count += 1
        return self.make()


def transport(provider, opener, clock, lane, **kw):
    return P.Transport(provider, opener=opener, clock=clock.now, monotonic=clock.now, sleep=clock.sleep,
                       rng=lambda: 0.5, lane=lane, **kw)


def request():
    return P.Request('https://x.invalid/')


class LaneShareTests(unittest.TestCase):
    def tearDown(self):
        P.configure_lanes()

    def test_default_split_sums_to_the_provider_rate(self):
        self.assertEqual(P.LANE_SHARES, {'main': 0.55, 'exit': 0.25, 'low': 0.20})
        c = Clock()
        rates = {lane: P.shared_limiter('helius', lane, clock=c.now, sleep=c.sleep).rate for lane in P.LANES}
        self.assertAlmostEqual(rates['main'], 5.5)
        self.assertAlmostEqual(rates['exit'], 2.5)
        self.assertAlmostEqual(rates['low'], 2.0)
        self.assertAlmostEqual(sum(rates.values()), P.DEFAULT_RATES['helius'][0])
        self.assertIsInstance(P.shared_limiter('helius', 'low', clock=c.now, sleep=c.sleep), P.LowLaneLimiter)
        self.assertNotIsInstance(P.shared_limiter('helius', 'exit', clock=c.now, sleep=c.sleep), P.LowLaneLimiter)

    def test_shares_are_validated(self):
        for bad in ({'main': 0.6, 'exit': 0.25, 'low': 0.2},          # sum above 1
                    {'main': 0.55, 'exit': 0.25},                    # missing lane
                    {'main': 0.55, 'exit': 0.25, 'low': 0.2, 'x': 0.0},
                    {'main': 0.55, 'exit': 0, 'low': 0.2},           # an exit lane of zero
                    {'main': 0.55, 'exit': True, 'low': 0.2},
                    {'main': '0.55', 'exit': 0.25, 'low': 0.2},
                    [0.55, 0.25, 0.2]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                P.configure_lanes(bad)
        with self.assertRaises(ValueError):
            P.configure_lanes(None, low_shed_s=-1)
        self.assertEqual(P.LANE_SHARES, P.DEFAULT_LANE_SHARES)          # a refused config changes nothing

    def test_configured_shares_reach_new_limiters(self):
        P.configure_lanes({'main': 0.5, 'exit': 0.3, 'low': 0.1}, low_shed_s=45)
        c = Clock()
        self.assertAlmostEqual(P.shared_limiter('helius', 'exit', clock=c.now, sleep=c.sleep).rate, 3.0)
        low = P.shared_limiter('helius', 'low', clock=c.now, sleep=c.sleep)
        self.assertAlmostEqual(low.rate, 1.0)
        self.assertEqual(low.shed_s, 45.0)

    def test_kraken_has_no_low_lane_and_the_low_providers_have_no_kraken(self):
        with self.assertRaises(ValueError):
            P.shared_limiter('kraken', 'low')
        low = P.low(KEYS)
        self.assertIsNone(low.kraken)
        self.assertEqual((low.helius.transport.lane, low.jupiter.transport.lane), ('low', 'low'))
        self.assertEqual(low.helius.transport.max_attempts, 1)
        self.assertIs(low.helius.transport.limiter, P.shared_limiter('helius', 'low'))


class NonBlockingTests(unittest.TestCase):
    def tearDown(self):
        P.configure_lanes()

    def test_no_token_fails_fast_with_lane_shed_and_never_sleeps(self):
        c = Clock()
        low = P.LowLaneLimiter(2.0, 2, clock=c.now, sleep=c.sleep)
        self.assertEqual((low.acquire(), low.acquire()), (0.0, 0.0))
        with self.assertRaises(P.ProviderError) as cm:
            low.acquire(deadline=c.now() + 100)                      # even a generous deadline never buys a wait
        self.assertEqual((cm.exception.code, cm.exception.transient, cm.exception.meta['why']), ('LANE_SHED', True, 'no_token'))
        self.assertTrue(P.is_shed(cm.exception))
        self.assertEqual(c.sleeps, [])
        c.t += 0.5                                                    # 2/s: one token back
        self.assertEqual(low.acquire(), 0.0)
        self.assertEqual(low.stats, {'granted': 3, 'shed_no_token': 1, 'shed_backoff': 0, 'sheds': 0})

    def test_a_shed_low_call_sends_nothing(self):
        c = Clock()
        opener = Opener()
        t = transport('helius', opener, c, 'low')
        t.limiter.shed()
        with self.assertRaises(P.ProviderError) as cm:
            t.call('rpc:getMultipleAccounts', request)
        self.assertEqual((cm.exception.code, cm.exception.provider, cm.exception.meta['why']), ('LANE_SHED', 'helius', 'backoff'))
        self.assertEqual((opener.count, c.sleeps), (0, []))

    def test_low_lane_never_retries(self):
        c = Clock()
        opener = Opener(lambda: Resp(b'busy', status=503))
        with self.assertRaises(P.ProviderError) as cm:
            transport('helius', opener, c, 'low', max_attempts=5).call('e', request)
        self.assertEqual((cm.exception.code, opener.count, c.sleeps), ('HTTP_503', 1, []))

    def test_flood_of_low_calls_never_delays_the_exit_lane(self):
        """1000 low-lane calls at one instant: 2 go out (the low burst), 998 shed at once; the exit lane's bucket is
        untouched and an exit call goes out with zero wait, nothing ever slept."""
        c = Clock()
        low_opener, exit_opener = Opener(), Opener()
        low = transport('helius', low_opener, c, 'low')
        exits = transport('helius', exit_opener, c, 'exit')
        shed = 0
        for _ in range(1000):
            try:
                low.call('rpc:getMultipleAccounts', request)
            except P.ProviderError as error:
                self.assertEqual(error.code, 'LANE_SHED')
                shed += 1
        self.assertEqual((low_opener.count, shed), (2, 998))
        self.assertEqual(exits.limiter._tokens, exits.limiter.burst)   # the flood took nothing from the exit lane
        for _ in range(2):
            _raw, meta = exits.call('rpc:getMultipleAccounts', request)
            self.assertEqual(meta['attempts'], 1)
        self.assertEqual((exit_opener.count, c.sleeps), (2, []))

    def test_threaded_flood_of_low_calls_does_not_slow_exit_acquisition(self):
        """Real clock: 8 threads hammer the low lane while exit calls are timed; each exit call is immediate."""
        P.configure_lanes()
        low = P.Transport('helius', opener=Opener(), lane='low')
        exits = P.Transport('helius', opener=Opener(), lane='exit')
        stop, granted, shed = threading.Event(), [], []

        def flood():
            while not stop.is_set():
                try:
                    low.call('rpc:getMultipleAccounts', request)
                    granted.append(1)
                except P.ProviderError:
                    shed.append(1)
        threads = [threading.Thread(target=flood) for _ in range(8)]
        [t.start() for t in threads]
        try:
            time.sleep(0.05)
            waits = []
            for _ in range(2):                                          # within the exit burst (2.5)
                began = time.monotonic()
                exits.call('rpc:getMultipleAccounts', request)
                waits.append(time.monotonic() - began)
        finally:
            stop.set()
            [t.join() for t in threads]
        self.assertGreater(len(shed), 100)                               # the flood really was a flood
        self.assertLessEqual(len(granted), 2 + 2 * 2)                    # low burst + 2/s for well under 2 s
        self.assertLess(max(waits), 0.25, waits)


class SheddingTests(unittest.TestCase):
    def tearDown(self):
        P.configure_lanes()

    def test_429_on_the_main_lane_sheds_the_low_lane_not_the_exit_lane(self):
        c = Clock()
        main = transport('helius', Opener(lambda: Resp(status=429)), c, 'main', max_attempts=1)
        low_opener, exit_opener = Opener(), Opener()
        low = transport('helius', low_opener, c, 'low')
        exits = transport('helius', exit_opener, c, 'exit')
        with self.assertRaises(P.ProviderError) as cm:
            main.call('rpc:getMultipleAccounts', request)
        self.assertEqual(cm.exception.code, 'HTTP_429')
        with self.assertRaises(P.ProviderError) as cm:
            low.call('rpc:getMultipleAccounts', request)
        self.assertEqual((cm.exception.code, cm.exception.meta['why']), ('LANE_SHED', 'backoff'))
        exits.call('rpc:getMultipleAccounts', request)                  # exits: untouched, no wait
        self.assertEqual((low_opener.count, exit_opener.count, c.sleeps), (0, 1, []))
        c.t += P.LOW_SHED_SECONDS - 1
        with self.assertRaises(P.ProviderError):
            low.call('rpc:getMultipleAccounts', request)
        c.t += 1.5
        low.call('rpc:getMultipleAccounts', request)                    # the window is over
        self.assertEqual(low_opener.count, 1)

    def test_429_on_the_exit_lane_also_sheds_the_low_lane(self):
        c = Clock()
        exits = transport('jupiter', Opener(lambda: Resp(status=429)), c, 'exit', max_attempts=1)
        low_opener = Opener()
        low = transport('jupiter', low_opener, c, 'low')
        with self.assertRaises(P.ProviderError):
            exits.call('quote', request)
        with self.assertRaises(P.ProviderError) as cm:
            low.call('quote', request)
        self.assertEqual(cm.exception.code, 'LANE_SHED')
        self.assertEqual(low_opener.count, 0)

    def test_429_on_the_low_lane_sheds_it_for_retry_after_and_blocks_nobody_else(self):
        P.configure_lanes(None, low_shed_s=10)
        c = Clock()
        low_opener = Opener(lambda: Resp(status=429, headers={'Retry-After': '25'}))
        low = transport('helius', low_opener, c, 'low')
        main = transport('helius', Opener(), c, 'main')
        with self.assertRaises(P.ProviderError) as cm:
            low.call('e', request)
        self.assertEqual(cm.exception.code, 'HTTP_429')
        main.call('e', request)                                          # main: no block, no sleep
        self.assertEqual(c.sleeps, [])
        c.t += 15                                                        # past low_shed_s 10, inside Retry-After 25
        with self.assertRaises(P.ProviderError) as cm:
            low.call('e', request)
        self.assertEqual(cm.exception.code, 'LANE_SHED')
        self.assertEqual(low_opener.count, 1)
        self.assertEqual(low.limiter.stats['sheds'], 1)
        c.t += 11
        low_opener.make = lambda: Resp()
        low.call('e', request)
        self.assertEqual(low_opener.count, 2)


if __name__ == '__main__':
    unittest.main()
