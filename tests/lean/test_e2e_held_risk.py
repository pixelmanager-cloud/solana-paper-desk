"""SYNTHETIC_TEST_ONLY: L11 held-position rug / unsellable handling, end to end with the REAL lean modules. Only HTTP is faked
(tests/lean/fakeworld.py); discovery is a real discovery database. No network, no credentials.

Each scenario enters one fake token at the first candidate pass (age 0 = the first BUY quote) and then changes the world:
  rug_liq        the pool's quote reserve falls 85 % at age 600 (a rug): RUG_EXIT at a fresh quote, trigger LIQUIDITY_DROP
  pool_closed    the pool account and its vaults vanish at age 600 while the route still quotes: RUG_EXIT, POOL_GONE
  migrated       the pool account changes owner program at age 600: RUG_EXIT, POOL_GONE
  no_route       the sell route disappears at age 600: after 120 s of probes UNSELLABLE (valued at the last executable quote),
                 written off after 6 h as RUG_WRITEOFF with no fee; accounting invariants hold throughout
  never_routable the sell route is gone before the first probe: UNSELLABLE valued at 0, written off at 0 (a position is only ever
                 opened with an executable sell leg, so "never" means "from 30 s after entry")
  route_back     the route is gone from 600 to 1500: UNSELLABLE, then the exit is taken when it returns (ROUTE_RESTORED)
  outage         Jupiter answers 503 for 20 minutes: NOT "no route" (nothing triggers, nothing is marked)
  freeze         a freeze authority appears at age 600: flagged and reported, the position carries on (and exits only when
                 ``freeze_forces_exit`` is set)
  restart        the process dies while a position is UNSELLABLE: the state, the clock and the valuation survive
"""
import json
import os
import socket
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from lean import report
from lean.__main__ import build_runner, load_config
from tests.lean.fakeworld import FakeTime, T0, Token, World
from tests.lean.test_e2e_real import KEYS, ROOT, steps

TICK = 60
LABEL = 'EXECUTION_UNVERIFIED'


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class Scenario(unittest.TestCase):
    maxDiff = None

    def build(self, *tokens, held_risk=None, outages=None, ready=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.state = self.root / 'state'
        self.clock = FakeTime(T0)
        self.tokens = list(tokens)
        self.world = World(self.root, self.tokens, self.clock)
        self.world.add_frames(ready or [T0] * len(tokens))
        for provider, window in (outages or {}).items():
            self.world.outages[provider] = window
        self.cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        if held_risk is not None:
            self.cfg['held_risk'] = held_risk
        return self.runner()

    def runner(self):
        return build_runner(self.cfg, state_dir=self.state, discovery_db=self.world.discovery_db, keys=KEYS,
                            code_version='e2e-test', clock=self.clock.time,
                            transport_kwargs={'opener': self.world.opener, 'clock': self.clock.time,
                                              'monotonic': self.clock.monotonic, 'sleep': self.clock.sleep, 'rng': lambda: 0.5})

    def run_until(self, r, end, *, check=None):
        tick = self.clock.time()
        while tick <= T0 + end:
            if self.clock.time() < tick:
                self.clock.t = tick
            r.position_pass()
            r.candidate_pass()
            r.write_health()
            self.assertIsNone(r.halted, r.halted)
            self.assertTrue(r.store.check_invariants())
            self.assertTrue(r.store.check_position_states())
            if check:
                check(r, tick - T0)
            tick += TICK
        return r

    # -- readers
    def events(self, r, ev=None):
        rows = [json.loads(x['payload']) for x in r.store.rows('events', kind='held_risk', limit=100000)]
        return [p for p in rows if ev is None or p['ev'] == ev]

    def closed(self, r, mint=None):
        rows = [c for c in r.store.closed_positions() if mint is None or c['mint'] == mint]
        return rows

    def fills(self, r, mint, side=None):
        return [f for f in r.store.rows('fills', mint=mint, limit=1000) if side is None or f['side'] == side]


class RugExits(Scenario):
    def test_a_liquidity_rug_is_sold_at_a_fresh_quote_as_rug_exit_not_stop(self):
        token = Token(21, path=steps((600, 0.15)))
        r = self.build(token)
        self.run_until(r, 1500)
        (closed,) = self.closed(r)
        self.assertEqual((closed['state']['reason'], closed['state']['trigger']), ('RUG_EXIT', 'LIQUIDITY_DROP'))
        self.assertEqual(r.store.positions(), {})
        baseline = self.events(r, 'baseline')[0]
        self.assertEqual(baseline['quote_reserve_lamports'], token.quote_lamports)              # the reserve at the screen
        (trigger,) = self.events(r, 'trigger')
        self.assertEqual((trigger['reason'], trigger['detail']['baseline_lamports']), ('LIQUIDITY_DROP', token.quote_lamports))
        self.assertLess(trigger['detail']['now_lamports'], token.quote_lamports * 0.3)
        # filled at the post-rug quote with the ordinary paper fee and slippage, labelled EXECUTION_UNVERIFIED
        (sell,) = self.fills(r, token.mint, 'sell')
        buy = self.fills(r, token.mint, 'buy')[0]
        self.assertEqual((sell['label'], sell['fee_lamports'], sell['slippage_bps']), (LABEL, 50_000, 50))
        self.assertLess(sell['sol_lamports'], buy['sol_lamports'] // 3)
        exit_decisions = [d for d in r.store.rows('decisions', mint=token.mint, kind='exit', limit=100) if d['action'] == 'SELL']
        self.assertEqual(json.loads(exit_decisions[0]['reasons']), ['RUG_EXIT', 'LIQUIDITY_DROP'])
        self.assertEqual(r.held_risk.counts['rug_exits'], 1)
        self.assertEqual(json.loads((self.state / 'health.json').read_text())['held_risk']['rug_exits'], 1)

    def test_the_exit_pass_makes_one_quote_and_no_route_probe(self):
        token = Token(21, path=steps((600, 0.15)))
        r = self.build(token)
        per_tick, last = {}, [0]

        def check(r, t):
            jupiter = sum(1 for p, m, _ in self.world.calls if p == 'jupiter')
            per_tick[t], last[0] = jupiter - last[0], jupiter
        self.run_until(r, 1500, check=check)
        sold_at = next(f['ts'] for f in self.fills(r, token.mint, 'sell')) - T0
        self.assertEqual(per_tick[sold_at], 1)                                              # the exit quote only: no probe in that pass

    def test_a_transient_failure_of_the_exit_quote_is_retried_not_unsellable(self):
        token = Token(21, path=steps((600, 0.15)))
        r = self.build(token, outages={'jupiter': (T0 + 590, T0 + 1200)})
        self.run_until(r, 2000)
        self.assertEqual(self.events(r, 'unsellable'), [])
        (closed,) = self.closed(r)
        self.assertEqual((closed['state']['reason'], closed['state']['trigger']), ('RUG_EXIT', 'LIQUIDITY_DROP'))
        (sell,) = self.fills(r, token.mint, 'sell')
        self.assertGreaterEqual(sell['ts'], T0 + 1200)                                      # sold only once the provider answered
        codes = [e['code'] for e in r.store.rows('errors', limit=10000) if e['scope'] == 'rug_exit']
        self.assertTrue(codes and set(codes) == {'HTTP_503'})

    def test_a_small_drop_is_left_to_the_normal_strategy(self):
        token = Token(21, path=steps((600, 0.5)))                                             # 50 % < 70 %: the plain STOP
        r = self.build(token)
        self.run_until(r, 1500)
        (closed,) = self.closed(r)
        self.assertEqual(closed['state']['reason'], 'STOP')
        self.assertEqual(self.events(r, 'trigger'), [])

    def test_a_closed_pool_with_a_route_still_quoting_is_sold_as_pool_gone(self):
        token = Token(21, pool_closed_from=600)
        r = self.build(token)
        self.run_until(r, 1500)
        (closed,) = self.closed(r)
        self.assertEqual((closed['state']['reason'], closed['state']['trigger']), ('RUG_EXIT', 'POOL_GONE'))
        (trigger,) = self.events(r, 'trigger')
        self.assertIn('missing', trigger['detail']['why'])

    def test_a_pool_that_changed_owner_program_is_pool_gone(self):
        token = Token(21, owner_change_from=600)
        r = self.build(token)
        self.run_until(r, 2000)
        (closed,) = self.closed(r)
        self.assertEqual((closed['state']['reason'], closed['state']['trigger']), ('RUG_EXIT', 'POOL_GONE'))
        (trigger,) = self.events(r, 'trigger')
        self.assertEqual(trigger['detail']['why'], 'pool owner changed')

    def test_a_healthy_position_is_untouched(self):
        token = Token(21, path=steps((0, 1.05)))
        r = self.build(token)
        self.run_until(r, 3200)
        (closed,) = self.closed(r)
        self.assertEqual(closed['state']['reason'], 'TIME_STOP')
        self.assertEqual([self.events(r, e) for e in ('trigger', 'unsellable', 'no_route', 'freeze_flag')], [[], [], [], []])


class Unsellable(Scenario):
    def test_no_route_becomes_unsellable_then_a_writeoff_at_the_last_executable_quote(self):
        token = Token(21, no_route_from=600)
        seen = {}

        def check(r, t):
            held = r.held_risk.lives
            life = next(iter(held.values()), None)
            if life is not None and life.unsellable and 'at' not in seen:
                seen['at'] = t
                seen['equity_mark'] = r.marks[token.mint][0]
        r = self.build(token)
        self.run_until(r, 700 + 21600 + 600, check=check)
        # UNSELLABLE after the 120 s no-route clock (probes are 60 s apart), not before
        (no_route,) = self.events(r, 'no_route')
        (unsellable,) = self.events(r, 'unsellable')
        self.assertGreaterEqual(unsellable['since'] - no_route['since'], 120)
        self.assertLess(unsellable['since'] - no_route['since'], 120 + 2 * TICK)
        self.assertEqual(unsellable['reason'], 'NO_ROUTE:HTTP_400')
        value = unsellable['value_lamports']
        self.assertGreater(value, 0)
        self.assertEqual(value, no_route['last_good_value_lamports'])                         # the last executable quote
        self.assertEqual(seen['equity_mark'], Decimal(value) / 10 ** 9)                       # equity values it there, not at cost
        # written off 6 h after it became UNSELLABLE, at that value, with no fee
        (closed,) = self.closed(r)
        self.assertEqual(closed['state']['reason'], 'RUG_WRITEOFF')
        (sell,) = self.fills(r, token.mint, 'sell')
        buy = self.fills(r, token.mint, 'buy')[0]
        self.assertEqual((sell['sol_lamports'], sell['fee_lamports'], sell['slippage_bps'], sell['label']), (value, 0, 0, LABEL))
        self.assertEqual(sell['realized_lamports'], value - (buy['sol_lamports'] + buy['fee_lamports']))
        self.assertGreaterEqual(sell['ts'] - unsellable['since'], 21600)
        self.assertLess(sell['ts'] - unsellable['since'], 21600 + 2 * TICK)
        self.assertEqual(r.store.positions(), {})
        self.assertTrue(r.store.check_invariants())
        self.assertEqual(r.held_risk.counts['writeoffs'], 1)

    def test_a_token_that_never_had_a_route_is_valued_at_zero(self):
        token = Token(21, no_route_from=30)
        r = self.build(token)
        self.run_until(r, 400 + 21600 + 600)
        (unsellable,) = self.events(r, 'unsellable')
        self.assertEqual(unsellable['value_lamports'], 0)
        (closed,) = self.closed(r)
        self.assertEqual(closed['state']['reason'], 'RUG_WRITEOFF')
        (sell,) = self.fills(r, token.mint, 'sell')
        buy = self.fills(r, token.mint, 'buy')[0]
        self.assertEqual((sell['sol_lamports'], sell['fee_lamports']), (0, 0))
        self.assertEqual(closed['state']['trade_pnl_lamports'], -(buy['sol_lamports'] + buy['fee_lamports']))   # the whole cost is lost
        self.assertEqual(r.store.cash(), r.store.initial_cash - buy['sol_lamports'] - buy['fee_lamports'])
        self.assertTrue(r.store.check_invariants())

    def test_a_route_that_comes_back_is_used(self):
        token = Token(21, no_route_from=600, route_back_at=1500)
        r = self.build(token)
        self.run_until(r, 2400)
        (unsellable,) = self.events(r, 'unsellable')
        self.assertLess(unsellable['since'], T0 + 1500)
        (closed,) = self.closed(r)
        self.assertEqual((closed['state']['reason'], closed['state']['trigger']), ('RUG_EXIT', 'ROUTE_RESTORED'))
        (sell,) = self.fills(r, token.mint, 'sell')
        self.assertGreater(sell['sol_lamports'], 0)
        self.assertEqual(sell['fee_lamports'], 50_000)                                        # a real (quoted) paper sell
        self.assertGreaterEqual(sell['ts'], T0 + 1500)
        self.assertEqual(r.held_risk.counts['writeoffs'], 0)

    def test_a_provider_outage_is_not_no_route(self):
        token = Token(21, path=steps((0, 1.05)))
        r = self.build(token, outages={'jupiter': (T0 + 600, T0 + 1800)})
        self.run_until(r, 1700)
        self.assertEqual([self.events(r, e) for e in ('no_route', 'trigger', 'unsellable')], [[], [], []])
        self.assertIn(token.mint, r.store.positions())                                         # still held, still managed
        codes = {e['code'] for e in r.store.rows('errors', limit=10000)}
        self.assertIn('HTTP_503', codes)

    def test_unsellable_state_the_no_route_clock_and_the_valuation_survive_a_restart(self):
        token = Token(21, no_route_from=600)
        r = self.build(token)
        self.run_until(r, 1500)
        (unsellable,) = self.events(r, 'unsellable')
        self.assertEqual(r.held_risk.health()['unsellable_now'], [token.mint])
        r.store.close()
        self.clock.t += 5
        r2 = self.runner()
        self.assertEqual(r2.held_risk.health()['unsellable_now'], [token.mint])
        self.assertEqual(r2.held_risk.life(token.mint, r2.store.position_states()[token.mint]['open_fill_id']).unsellable['since'],
                         unsellable['since'])
        self.run_until(r2, 1500 + 120)                                                         # a few passes after the restart
        self.assertEqual(r2.marks[token.mint][0], Decimal(unsellable['value_lamports']) / 10 ** 9)
        # the write-off clock is NOT restarted by the restart
        self.run_until(r2, (unsellable['since'] - T0) + 21600 + 2 * TICK)
        (closed,) = self.closed(r2)
        self.assertEqual(closed['state']['reason'], 'RUG_WRITEOFF')
        (sell,) = self.fills(r2, token.mint, 'sell')
        self.assertLess(sell['ts'] - unsellable['since'], 21600 + 2 * TICK)
        self.assertEqual(len(self.events(r2, 'unsellable')), 1)

    def test_a_worthless_route_is_unsellable_with_value_zero_not_a_sale_below_the_fee(self):
        token = Token(21, path=steps((600, 0.0000001)))
        r = self.build(token, held_risk={'unsellable_writeoff_s': 600})
        self.run_until(r, 2500)
        events = self.events(r, 'unsellable')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['value_lamports'], events[0]['value_lamports'])
        (closed,) = self.closed(r)
        self.assertEqual(closed['state']['reason'], 'RUG_WRITEOFF')
        self.assertTrue(r.store.check_invariants())


class FreezeAuthority(Scenario):
    def test_a_freeze_authority_is_flagged_and_reported_but_the_position_carries_on(self):
        token = Token(21, path=steps((0, 1.05)), freeze_from=600)
        r = self.build(token)
        self.run_until(r, 1500)
        self.assertEqual(len(self.events(r, 'freeze_flag')), 1)                                # once, not on every check
        self.assertEqual(self.events(r, 'trigger'), [])
        self.assertIn(token.mint, r.store.positions())
        self.assertEqual(r.held_risk.counts['freeze_flags'], 1)
        self.run_until(r, 3200)
        (closed,) = self.closed(r)
        self.assertEqual(closed['state']['reason'], 'TIME_STOP')
        self.assertEqual(report.build(r.store.path, now=self.clock.time())['held_risk']['data']['freeze_flags'], 1)

    def test_the_flag_can_force_an_exit(self):
        token = Token(21, path=steps((0, 1.05)), freeze_from=600)
        r = self.build(token, held_risk={'freeze_forces_exit': True})
        self.run_until(r, 1800)
        (closed,) = self.closed(r)
        self.assertEqual((closed['state']['reason'], closed['state']['trigger']), ('RUG_EXIT', 'FREEZE'))


class ReportAndConfig(Scenario):
    def test_the_report_counts_rugs_and_writeoffs_separately(self):
        rug = Token(21, path=steps((600, 0.15)))
        gone = Token(23, no_route_from=30)
        r = self.build(rug, gone, ready=[T0, T0 + 180])        # entries are throttled to one a minute
        self.run_until(r, 22600)
        summary = report.build(r.store.path, now=self.clock.time())
        data = summary['held_risk']['data']
        self.assertEqual((data['rug_exit']['trades'], data['rug_writeoff']['trades']), (1, 1))
        self.assertLess(Decimal(data['rug_exit']['pnl_sol']), 0)
        self.assertLess(Decimal(data['rug_writeoff']['pnl_sol']), 0)
        self.assertEqual(data['triggers'], {'LIQUIDITY_DROP': 1, 'NO_ROUTE': 1})
        self.assertEqual(data['unsellable_open'], [])
        self.assertIn('RUG_EXIT', summary['exit_reasons']['data'])
        self.assertIn('RUG_WRITEOFF', summary['exit_reasons']['data'])
        html = report.render_html(summary)
        self.assertIn('Rugs and write-offs', html)

    def test_an_open_unsellable_position_is_listed_with_its_value(self):
        token = Token(21, no_route_from=600)
        r = self.build(token)
        self.run_until(r, 1500)
        data = report.build(r.store.path, now=self.clock.time())['held_risk']['data']
        (row,) = data['unsellable_open']
        self.assertEqual(row['mint'], token.mint)
        self.assertGreater(Decimal(data['unsellable_open_value_sol']), 0)

    def test_disabled_means_todays_behaviour(self):
        token = Token(21, path=steps((600, 0.15)))
        r = self.build(token, held_risk={'enabled': False})
        self.assertIsNone(r.held_risk)
        self.run_until(r, 1500)
        (closed,) = self.closed(r)
        self.assertEqual(closed['state']['reason'], 'STOP')
        self.assertIsNone(json.loads((self.state / 'health.json').read_text())['held_risk'])


if __name__ == '__main__':
    unittest.main()
