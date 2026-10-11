"""SYNTHETIC_TEST_ONLY: L14 end to end with the REAL lean modules (runner, candidates, screen, strategy, paper, store, sources,
watchlist). Only the HTTP layer is faked (tests/lean/fakeworld.py). No network, no credentials.

Two discovery databases: the runner's own pump discovery and a second configured source ``pump_b``.

  late_liq     soft-rejected (market cap / liquidity below min) at first, healthy from t+1500 s: re-screened and ENTERED
  hazard       mint authority from the start: rejected, never watched, never re-screened
  flip         soft at first, then a mint authority appears at t+1200 s: the next re-screen ends the watch as HAZARD
  forever_low  soft for the whole run: re-screened every interval, then EXPIRED after watch_hours
  b_plain      only in pump_b, healthy: entered, source recorded as pump_b
  b_late       only in pump_b, soft at first and healthy later: watched under pump_b and ENTERED
  dup          in BOTH discovery databases: handled once, by whichever source saw its (older) frame first
"""
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from lean import sources as S
from lean.__main__ import build_runner, load_config
from tests.lean.fakeworld import FakeTime, T0, Token, World

ROOT = Path(__file__).resolve().parents[2]
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}
LOW = Decimal('0.25')


class TimedToken(Token):
    """Pool reserves and hazards keyed on absolute test time (the base class keys them on the first buy quote)."""

    def __init__(self, tag, *, low_until=0, hazard_from=None, **kw):
        super().__init__(tag, **kw)
        self.low_until, self.hazard_from = T0 + low_until, None if hazard_from is None else T0 + hazard_from

    def multiplier(self, t):
        return LOW if t < self.low_until else Decimal(1)

    def accounts(self, t):
        self.hazard = 'mint_authority' if self.hazard_from is not None and t >= self.hazard_from else self.hazard
        return super().accounts(t)


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class SourcesAndWatchlistEndToEnd(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.clock = FakeTime(T0)
        self.t = {
            'late_liq': TimedToken(21, low_until=1500),
            'hazard': Token(23, hazard='mint_authority'),
            'flip': TimedToken(25, low_until=10 ** 9, hazard_from=1200),
            'forever_low': TimedToken(27, low_until=10 ** 9),
            'b_plain': Token(29),
            'b_late': TimedToken(31, low_until=1500),
            'dup': Token(33),
        }
        self.role = {tok.mint: role for role, tok in self.t.items()}
        main = [self.t[k] for k in ('late_liq', 'hazard', 'flip', 'forever_low', 'dup')]
        second = [self.t[k] for k in ('b_plain', 'b_late', 'dup')]
        self.world = World(self.root, main, self.clock)
        self.world.add_frames([T0 + o for o in (0, 60, 120, 180, 300)])
        (self.root / 'b').mkdir()
        self.world_b = World(self.root / 'b', second, self.clock)
        self.world_b.add_frames([T0 + o for o in (240, 300, 360)])
        self.world.tokens.update({tok.mint: tok for tok in second})
        self.cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        self.cfg['sources'] = [{'name': 'pump_b', 'type': 'pump_graduation', 'discovery_db': str(self.world_b.discovery_db)}]
        self.cfg['watchlist'] = {'enabled': True, 'watch_interval_s': 300, 'watch_hours': 0.5, 'watch_max_per_pass': 5}
        self.state = self.root / 'state'

    def runner(self):
        return build_runner(self.cfg, state_dir=self.state, discovery_db=self.world.discovery_db, keys=KEYS,
                            code_version='e2e-l14', clock=self.clock.time,
                            transport_kwargs={'opener': self.world.opener, 'clock': self.clock.time,
                                              'monotonic': self.clock.monotonic, 'sleep': self.clock.sleep, 'rng': lambda: 0.5})

    def run_for(self, r, seconds, step=60):
        end = T0 + seconds
        tick = self.clock.time()
        while tick <= end:
            if self.clock.time() < tick:
                self.clock.t = tick
            r.position_pass()
            r.candidate_pass()
            self.assertIsNone(r.halted, r.halted)
            self.assertTrue(r.store.check_invariants())
            tick += step
        return r

    def screens(self, store, role):
        mint = self.t[role].mint
        return [(r['action'], tuple(__import__('json').loads(r['reasons']))) for r in store.rows('decisions', mint=mint, kind='screen', limit=1000)]

    def event_mints(self, store, kind):
        import json
        return [(json.loads(e['payload'])['mint'], json.loads(e['payload']).get('why')) for e in store.rows('events', kind=kind, limit=1000)]

    def test_sources_watchlist_and_funnel(self):
        r = self.run_for(self.runner(), 4000)
        store = r.store
        entered = {self.role[f['mint']] for f in store.rows('fills', limit=1000) if f['side'] == 'buy'}

        # soft rejects were re-screened and entered; hazards never were
        self.assertEqual(entered, {'late_liq', 'b_plain', 'b_late', 'dup'})
        first = self.screens(store, 'late_liq')[0]
        self.assertEqual(first[0], 'REJECT')
        self.assertTrue(set(first[1]) <= {'MARKET_CAP_BELOW_MIN', 'LIQUIDITY_BELOW_MIN'}, first)
        self.assertGreater(len(self.screens(store, 'late_liq')), 2)
        self.assertEqual(self.screens(store, 'late_liq')[-1][0], 'PASS')
        self.assertEqual(len(self.screens(store, 'hazard')), 1)                    # rejected once, never re-screened
        self.assertIn('hazard', {self.role[m] for m in [self.t['hazard'].mint]})
        watched = {self.role[m] for m, _ in self.event_mints(store, 'watch_add')}
        self.assertEqual(watched, {'late_liq', 'flip', 'forever_low', 'b_late'})   # never hazard, never plain, never dup

        # the end of every watch is typed
        ended = {self.role[m]: why for m, why in self.event_mints(store, 'watch_end')}
        self.assertEqual(ended, {'late_liq': 'ENTERED', 'flip': 'HAZARD', 'forever_low': 'EXPIRED', 'b_late': 'ENTERED'})
        flip = self.screens(store, 'flip')
        self.assertEqual(flip[-1][0], 'REJECT')
        self.assertIn('ACTIVE_MINT_AUTHORITY', flip[-1][1])
        n_flip = len(flip)
        # forever_low: re-screened about every 300 s for 0.5 h, then no more
        low = self.screens(store, 'forever_low')
        self.assertTrue(4 <= len(low) <= 8, len(low))
        self.assertTrue(all(a == 'REJECT' and set(rs) <= {'MARKET_CAP_BELOW_MIN', 'LIQUIDITY_BELOW_MIN'} for a, rs in low), low)

        # the extra source: its own cursor, its name on the rows, one handling for the shared mint
        self.assertEqual(S_cursor(self.state, 'pump_b'), 3)
        rows = {self.role[c['mint']]: __import__('json').loads(c['meta'])['source'] for c in store.rows('candidates', limit=100)}
        self.assertEqual(rows, {'late_liq': 'pump_graduation', 'hazard': 'pump_graduation', 'flip': 'pump_graduation',
                                'forever_low': 'pump_graduation', 'b_plain': 'pump_b', 'b_late': 'pump_b', 'dup': 'pump_graduation'})   # dup: the older frame (main db) was seen first
        self.assertEqual(len(self.screens(store, 'dup')), 1)
        self.assertEqual(r.cursor, 5)                                              # the pump cursor is untouched by pump_b

        # funnel per source
        funnel = S.funnel_by_source(store)
        pump, b = funnel['pump_graduation'], funnel['pump_b']
        self.assertEqual((pump['candidates'], pump['entered'], pump['watched'], pump['entered_after_watch']), (5, 2, 3, 1))
        self.assertEqual((b['candidates'], b['entered'], b['watched'], b['entered_after_watch']), (2, 2, 1, 1))
        self.assertEqual(pump['watch_ended'], {'ENTERED': 1, 'HAZARD': 1, 'EXPIRED': 1})
        self.assertEqual(b['watch_ended'], {'ENTERED': 1})
        self.assertGreaterEqual(pump['rescreens'], 6)
        self.assertEqual(pump['rejection_reasons'].get('ACTIVE_MINT_AUTHORITY'), 2)   # hazard + flip, by their LAST screen
        # nothing after the end: no further screens once ended
        self.run_for(r, 6000)
        self.assertEqual(len(self.screens(store, 'flip')), n_flip)
        self.assertEqual(len(self.screens(store, 'forever_low')), len(low))

    def test_restart_keeps_the_watchlist_and_does_not_restart_the_window(self):
        r = self.run_for(self.runner(), 900)
        first_at = {m: e['first_at'] for m, e in r.watch.entries.items()}
        self.assertEqual({self.role[m] for m in first_at}, {'late_liq', 'flip', 'forever_low', 'b_late'})
        r.store.close()
        r2 = self.runner()
        self.assertEqual({m: e['first_at'] for m, e in r2.watch.entries.items()}, first_at)
        self.run_for(r2, 4000)
        entered = {self.role[f['mint']] for f in r2.store.rows('fills', limit=1000) if f['side'] == 'buy'}
        self.assertEqual(entered, {'late_liq', 'b_plain', 'b_late', 'dup'})
        self.assertEqual(len([e for e in r2.store.rows('events', kind='watch_add', limit=100)]), 4)   # no double adds

    def test_off_by_default_and_without_extra_sources_nothing_changes(self):
        self.cfg['sources'], self.cfg['watchlist'] = [], {}
        r = self.run_for(self.runner(), 4000)
        self.assertIsNone(r.watch)
        self.assertIsNone(r.extra_sources)
        self.assertEqual(r.store.rows('events', kind='watch_add', limit=10), [])
        self.assertEqual(len(self.screens(r.store, 'late_liq')), 1)                # a soft reject stays rejected
        self.assertEqual(len(r.store.rows('candidates', limit=100)), 5)            # pump_b was never read

    def test_a_broken_extra_source_never_stops_the_pump_scan(self):
        self.cfg['sources'][0]['discovery_db'] = str(self.root / 'missing.sqlite')
        r = self.run_for(self.runner(), 1000)
        codes = {e['code'] for e in r.store.rows('errors', limit=1000)}
        self.assertTrue(codes & {'DISCOVERY_UNAVAILABLE', 'SOURCE_FAILED'}, codes)
        self.assertEqual(len(r.store.rows('candidates', limit=100)), 5)
        self.assertIsNone(r.halted)


def S_cursor(state, name):
    from lean import candidates as C
    return C.read_cursor(os.path.join(state, 'cursor-%s.json' % name))


if __name__ == '__main__':
    unittest.main()
