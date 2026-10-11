"""SYNTHETIC_TEST_ONLY: L11F held-position rug handling, end to end with the REAL lean modules (runner with the D1 quote marks and the D2
exit-failing clock / write-off). Only HTTP is faked (tests/lean/fakeworld.py); discovery is a real discovery database.

Each scenario enters one fake token at the first candidate pass (age 0 = the first BUY quote) and then changes the world:
  rug_liq        the pool's quote reserve falls 85 % at age 600: a forced DANGER exit at a fresh quote (trigger LIQUIDITY_DROP)
  pool_closed    the pool account and its vaults vanish at age 600 while the route still quotes: POOL_GONE after TWO empty reads
  blip           the vaults are missing for ONE read only: nothing happens
  migrated       the pool account changes owner program at age 600: POOL_GONE after two account checks
  no_route       the sell route disappears at age 600: NO_ROUTE after 120 s of probes, the forced exit fails (D2 clock starts), the
                 position is valued at 0 (never at cost, never at its last price), written off by D2 as RUG_WRITEOFF at zero proceeds
  route_back     the route returns: the forced exit is taken at a real quote (no write-off)
  outage         Jupiter answers 503: NOT "no route"
  restart        the process dies with a trigger / a failing exit pending: both survive
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


class RecordingWorld(World):
    """Remembers every Jupiter quote request: (time, input mint, output mint)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.jlog = []

    def _jupiter(self, path, q, t):
        self.jlog.append((t, q['inputMint'], q['outputMint']))
        return super()._jupiter(path, q, t)


class Scenario(unittest.TestCase):
    maxDiff = None

    def build(self, *tokens, held_risk=None, outages=None, ready=None, unexitable_after_s=None):
        """``held_risk``: None = enabled with defaults; False = the key stays as shipped (disabled); a dict = enabled with overrides."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.state = self.root / 'state'
        self.clock = FakeTime(T0)
        self.tokens = list(tokens)
        self.world = RecordingWorld(self.root, self.tokens, self.clock)
        self.world.add_frames(ready or [T0] * len(tokens))
        for provider, window in (outages or {}).items():
            self.world.outages[provider] = window
        self.cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        if held_risk is not False:
            self.cfg['held_risk'] = {**self.cfg['held_risk'], 'enabled': True, **(held_risk or {})}
        if unexitable_after_s is not None:
            self.cfg['unexitable_after_s'] = unexitable_after_s
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
        return [c for c in r.store.closed_positions() if mint is None or c['mint'] == mint]

    def fills(self, r, mint, side=None):
        return [f for f in r.store.rows('fills', mint=mint, limit=1000) if side is None or f['side'] == side]

    def pstate(self, r, mint):
        return r.store.position_states()[mint]['state']


class RugExits(Scenario):
    def test_a_liquidity_rug_is_a_forced_danger_exit_at_a_fresh_quote(self):
        token = Token(21, path=steps((600, 0.15)))
        r = self.build(token)
        self.run_until(r, 1500)
        (closed,) = self.closed(r)
        self.assertEqual((closed['state']['reason'], closed['state']['rug_trigger']), ('DANGER', 'LIQUIDITY_DROP'))
        self.assertEqual(r.store.positions(), {})
        baseline = self.events(r, 'baseline')[0]
        self.assertEqual((baseline['quote_reserve_lamports'], baseline['source']), (token.quote_lamports, 'screen'))
        (trigger,) = self.events(r, 'trigger')
        self.assertEqual((trigger['reason'], trigger['detail']['baseline_lamports']), ('LIQUIDITY_DROP', token.quote_lamports))
        self.assertLess(trigger['detail']['now_lamports'], token.quote_lamports * 0.3)
        # filled at the post-rug quote with the ordinary paper fee and slippage, labelled EXECUTION_UNVERIFIED
        (sell,) = self.fills(r, token.mint, 'sell')
        buy = self.fills(r, token.mint, 'buy')[0]
        self.assertEqual((sell['label'], sell['fee_lamports'], sell['slippage_bps']), (LABEL, 50_000, 50))
        self.assertLess(sell['sol_lamports'], buy['sol_lamports'] // 3)
        sells = [d for d in r.store.rows('decisions', mint=token.mint, kind='exit', limit=100) if d['action'] == 'SELL']
        self.assertEqual(json.loads(sells[0]['reasons']), ['DANGER'])
        # a rug cools down like a stop (the longer embargo)
        stop_cooldown = r.cfg.stop_cooldown_seconds
        self.assertEqual(closed['state']['cooldown_until'], int(sell['ts']) + stop_cooldown)
        self.assertEqual(json.loads((self.state / 'health.json').read_text())['held_risk']['triggers'], 1)

    def test_the_exit_pass_makes_one_quote_and_no_route_probe(self):
        token = Token(21, path=steps((600, 0.15)))
        r = self.build(token)
        self.run_until(r, 1500)
        exit_quote = max(i for i, x in enumerate(self.world.jlog) if x[1] == token.mint)
        t_exit = self.world.jlog[exit_quote][0]
        in_that_pass = [x for x in self.world.jlog if abs(x[0] - t_exit) < 5]
        self.assertEqual(len(in_that_pass), 1)                                              # the exit quote only: no probe in that pass

    def test_probes_and_account_checks_never_queue_ahead_of_an_exit(self):
        rug, healthy = Token(21, path=steps((600, 0.15))), Token(23, path=steps((0, 1.02)))
        r = self.build(rug, healthy, ready=[T0, T0 + 120])
        self.run_until(r, 1500)
        exit_quote = max(i for i, x in enumerate(self.world.jlog) if x[1] == rug.mint)           # the rug's one sell quote
        t_exit = self.world.jlog[exit_quote][0]
        same_pass = [x for x in self.world.jlog if abs(x[0] - t_exit) < 5]
        self.assertEqual(same_pass[0], self.world.jlog[exit_quote], 'the exit quote is the FIRST request of its pass')
        self.assertTrue(any(x[1] == healthy.mint for x in same_pass[1:]), 'the healthy position is probed in the same pass, after it')

    def test_a_transient_failure_of_the_exit_quote_is_retried_and_the_trigger_is_sticky(self):
        token = Token(21, path=steps((600, 0.15)))
        r = self.build(token, outages={'jupiter': (T0 + 590, T0 + 1200)})
        self.run_until(r, 2000)
        (closed,) = self.closed(r)
        self.assertEqual((closed['state']['reason'], closed['state']['rug_trigger']), ('DANGER', 'LIQUIDITY_DROP'))
        self.assertNotIn('exit_failing_since', closed['state'])                              # a transient failure never starts the clock
        (sell,) = self.fills(r, token.mint, 'sell')
        self.assertGreaterEqual(sell['ts'], T0 + 1200)                                      # sold only once the provider answered
        codes = [e['code'] for e in r.store.rows('errors', limit=10000) if e['scope'] == 'quote:exit']
        self.assertTrue(codes and set(codes) == {'HTTP_503'})

    def test_a_pending_trigger_survives_a_restart(self):
        token = Token(21, path=steps((600, 0.15)))
        r = self.build(token, outages={'jupiter': (T0 + 590, T0 + 1500)})
        self.run_until(r, 900)
        self.assertEqual(r.held_risk.trigger(token.mint, r.store.position_states()[token.mint]['open_fill_id']), 'LIQUIDITY_DROP')
        r.store.close()
        self.clock.t += 5
        r2 = self.runner()
        life = r2.held_risk.lives[(token.mint, r2.store.position_states()[token.mint]['open_fill_id'])]
        self.assertEqual(life.trigger[0], 'LIQUIDITY_DROP')                                  # replayed, not lost
        self.run_until(r2, 2000)
        (closed,) = self.closed(r2)
        self.assertEqual(closed['state']['reason'], 'DANGER')                                # a lost trigger would have been a STOP

    def test_a_small_drop_is_left_to_the_normal_strategy(self):
        token = Token(21, path=steps((600, 0.5)))                                             # 50 % < 70 %: the plain STOP
        r = self.build(token)
        self.run_until(r, 1500)
        (closed,) = self.closed(r)
        self.assertEqual(closed['state']['reason'], 'STOP')
        self.assertNotIn('rug_trigger', closed['state'])
        self.assertEqual(self.events(r, 'trigger'), [])

    def test_a_closed_pool_needs_two_consecutive_empty_reads(self):
        token = Token(21, pool_closed_from=600)
        r = self.build(token)
        self.run_until(r, 1500)
        (closed,) = self.closed(r)
        self.assertEqual((closed['state']['reason'], closed['state']['rug_trigger']), ('DANGER', 'POOL_GONE'))
        (trigger,) = self.events(r, 'trigger')
        self.assertEqual((trigger['detail']['why'], trigger['detail']['reads']), ('vault account missing', 2))
        misses = self.events(r, 'vault_miss')
        self.assertEqual([m['n'] for m in misses], [1, 2])
        (sell,) = self.fills(r, token.mint, 'sell')
        self.assertTrue(0 <= sell['ts'] - misses[1]['at'] < 5)                                # sold in the pass of the SECOND empty read
        self.assertGreater(misses[1]['at'], misses[0]['at'])

    def test_one_empty_read_is_a_hiccup_not_a_rug(self):
        token = Token(21, pool_closed_from=600, pool_closed_until=630)
        r = self.build(token)
        self.run_until(r, 1200)
        self.assertEqual([m['n'] for m in self.events(r, 'vault_miss')], [1])
        self.assertEqual(len(self.events(r, 'vault_seen')), 1)                               # the counter was reset
        self.assertEqual(self.events(r, 'trigger'), [])
        self.assertIn(token.mint, r.store.positions())

    def test_the_confirmation_count_survives_a_restart(self):
        token = Token(21, pool_closed_from=600)
        r = self.build(token, held_risk={'pool_gone_confirmations': 3})
        self.run_until(r, 660)                                                               # two empty reads so far
        self.assertEqual([m['n'] for m in self.events(r, 'vault_miss')], [1, 2])
        r.store.close()
        self.clock.t += 5
        r2 = self.runner()
        life = r2.held_risk.lives[(token.mint, r2.store.position_states()[token.mint]['open_fill_id'])]
        self.assertEqual(life.vault_misses, 2)
        self.run_until(r2, 800)
        (closed,) = self.closed(r2)
        self.assertEqual(closed['state']['rug_trigger'], 'POOL_GONE')                        # the third read, not a fresh count of three

    def test_a_pool_that_changed_owner_program_is_pool_gone_after_two_checks(self):
        token = Token(21, owner_change_from=600)
        r = self.build(token)
        self.run_until(r, 2000)
        (closed,) = self.closed(r)
        self.assertEqual((closed['state']['reason'], closed['state']['rug_trigger']), ('DANGER', 'POOL_GONE'))
        (trigger,) = self.events(r, 'trigger')
        self.assertEqual((trigger['detail']['why'], trigger['detail']['reads']), ('pool owner changed', 2))

    def test_a_healthy_position_is_untouched(self):
        token = Token(21, path=steps((0, 1.05)))
        r = self.build(token)
        self.run_until(r, 3200)
        (closed,) = self.closed(r)
        self.assertEqual(closed['state']['reason'], 'TIME_STOP')
        self.assertEqual([self.events(r, e) for e in ('trigger', 'no_route', 'freeze_flag')], [[], [], []])


class FailingExit(Scenario):
    """The route is gone: the forced exit cannot be quoted, D2's clock runs, and the position is worth 0 meanwhile."""

    def test_no_route_is_valued_at_zero_then_written_off_by_d2_as_rug_writeoff(self):
        token = Token(21, no_route_from=600)
        seen = {}

        def check(r, t):
            mark = r.marks.get(token.mint)
            state = r.store.position_states().get(token.mint)
            if state and state['state'].get('exit_failing_since') is not None and 'at' not in seen:
                seen['at'] = t
            if 'at' in seen and t > seen['at'] and t < seen['at'] + 3600 and mark is not None:
                seen.setdefault('marks', []).append(mark)
                seen.setdefault('cost_basis', []).append(list(r.cost_basis_mints))
        r = self.build(token)
        self.run_until(r, 700 + 7200 + 900, check=check)
        # detection: the no-route clock needs 120 s of probes, then the forced exit fails and D2's clock starts
        (no_route,) = self.events(r, 'no_route')
        (trigger,) = self.events(r, 'trigger')
        self.assertEqual(trigger['reason'], 'NO_ROUTE')
        self.assertGreaterEqual(trigger['detail']['since'], no_route['since'])
        (closed,) = self.closed(r)
        since = closed['state']['exit_failing_since']
        self.assertGreater(since, trigger['detail']['since'] + 119)
        self.assertEqual((closed['state']['reason'], closed['state']['rug_trigger']), ('RUG_WRITEOFF', 'NO_ROUTE'))
        # valuation while failing: ZERO, from the held-risk source, and never the cost basis
        self.assertTrue(seen['marks'])
        for value, at, source in seen['marks']:
            self.assertEqual((value, source), (Decimal(0), 'held_risk'))
        self.assertTrue(all(token.mint not in basis for basis in seen['cost_basis']))
        # D2's write-off: zero proceeds, no fee, the whole cost lost, after unexitable_after_s from the FIRST failure
        (sell,) = self.fills(r, token.mint, 'sell')
        buy = self.fills(r, token.mint, 'buy')[0]
        self.assertEqual((sell['sol_lamports'], sell['fee_lamports'], sell['slippage_bps'], sell['label']), (0, 0, 0, LABEL))
        self.assertEqual(closed['state']['trade_pnl_lamports'], -(buy['sol_lamports'] + buy['fee_lamports']))
        self.assertGreaterEqual(sell['ts'] - since, 7200)
        self.assertLess(sell['ts'] - since, 7200 + 2 * TICK)
        self.assertEqual(closed['state']['cooldown_until'], int(sell['ts']) + r.cfg.stop_cooldown_seconds)
        self.assertEqual(r.store.positions(), {})
        self.assertEqual(r.store.cash(), r.store.initial_cash - buy['sol_lamports'] - buy['fee_lamports'])
        self.assertTrue(r.store.check_invariants())
        decisions = [d for d in r.store.rows('decisions', mint=token.mint, kind='exit', limit=1000) if d['action'] == 'WRITE_OFF']
        self.assertEqual([json.loads(d['reasons']) for d in decisions], [['RUG_WRITEOFF']])

    def test_without_a_trigger_the_same_failure_is_still_unexitable(self):
        token = Token(21, path=steps((600, 0.5)), no_route_from=600)                         # an ordinary STOP that cannot be quoted
        r = self.build(token, held_risk=False)
        self.assertIsNone(r.held_risk)
        self.run_until(r, 700 + 7200 + 900)
        (closed,) = self.closed(r)
        self.assertEqual(closed['state']['reason'], 'UNEXITABLE')
        self.assertNotIn('rug_trigger', closed['state'])

    def test_a_route_that_never_existed_is_valued_at_zero_and_written_off(self):
        token = Token(21, no_route_from=30)
        r = self.build(token, unexitable_after_s=900)
        self.run_until(r, 1800)
        (closed,) = self.closed(r)
        self.assertEqual(closed['state']['reason'], 'RUG_WRITEOFF')
        (sell,) = self.fills(r, token.mint, 'sell')
        buy = self.fills(r, token.mint, 'buy')[0]
        self.assertEqual((sell['sol_lamports'], sell['fee_lamports']), (0, 0))
        self.assertEqual(r.store.cash(), r.store.initial_cash - buy['sol_lamports'] - buy['fee_lamports'])

    def test_a_route_that_comes_back_is_used_at_a_real_quote(self):
        token = Token(21, no_route_from=600, route_back_at=1500)
        r = self.build(token)
        self.run_until(r, 2400)
        (closed,) = self.closed(r)
        self.assertEqual((closed['state']['reason'], closed['state']['rug_trigger']), ('DANGER', 'NO_ROUTE'))
        self.assertNotIn('exit_failing_since', closed['state'])                              # cleared by the successful sale
        (sell,) = self.fills(r, token.mint, 'sell')
        self.assertGreater(sell['sol_lamports'], 0)
        self.assertEqual(sell['fee_lamports'], 50_000)                                        # a real (quoted) paper sell
        self.assertGreaterEqual(sell['ts'], T0 + 1500)

    def test_a_worthless_route_is_not_a_sale_below_the_fee(self):
        token = Token(21, path=steps((600, 0.0000001)))
        r = self.build(token, unexitable_after_s=600)
        self.run_until(r, 2500)
        (closed,) = self.closed(r)
        self.assertEqual((closed['state']['reason'], closed['state']['rug_trigger']), ('RUG_WRITEOFF', 'LIQUIDITY_DROP'))
        self.assertEqual(closed['state']['exit_failing_reason'], 'STUCK_POSITION')
        (sell,) = self.fills(r, token.mint, 'sell')
        self.assertEqual(sell['sol_lamports'], 0)
        self.assertTrue(r.store.check_invariants())

    def test_a_provider_outage_is_not_no_route(self):
        token = Token(21, path=steps((0, 1.05)))
        r = self.build(token, outages={'jupiter': (T0 + 600, T0 + 1800)})
        self.run_until(r, 1700)
        self.assertEqual([self.events(r, e) for e in ('no_route', 'trigger')], [[], []])
        self.assertIn(token.mint, r.store.positions())                                         # still held, still managed
        self.assertIn('HTTP_503', {e['code'] for e in r.store.rows('errors', limit=10000)})

    def test_the_failing_state_and_its_clock_survive_a_restart(self):
        token = Token(21, no_route_from=600)
        r = self.build(token)
        self.run_until(r, 1500)
        before = self.pstate(r, token.mint)
        self.assertEqual(before['rug_trigger'], 'NO_ROUTE')                                  # stamped on the persisted state
        since = before['exit_failing_since']
        r.store.close()
        self.clock.t += 5
        r2 = self.runner()
        self.assertEqual(r2.held_risk.health()['failing_now'], [token.mint])
        self.run_until(r2, 1560)
        self.assertEqual(r2.marks[token.mint][0], Decimal(0))                                 # still valued at 0 after the restart
        self.run_until(r2, (since - T0) + 7200 + 2 * TICK)
        (closed,) = self.closed(r2)
        self.assertEqual((closed['state']['reason'], closed['state']['exit_failing_since']), ('RUG_WRITEOFF', since))
        (sell,) = self.fills(r2, token.mint, 'sell')
        self.assertLess(sell['ts'] - since, 7200 + 2 * TICK)                                  # the clock was NOT restarted


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
        self.assertEqual((closed['state']['reason'], closed['state']['rug_trigger']), ('DANGER', 'FREEZE'))


class ReportAndConfig(Scenario):
    def test_the_report_counts_rugs_and_writeoffs_separately(self):
        rug = Token(21, path=steps((600, 0.15)))
        gone = Token(23, no_route_from=30)
        r = self.build(rug, gone, ready=[T0, T0 + 180], unexitable_after_s=900)       # entries are throttled to one a minute
        self.run_until(r, 3000)
        summary = report.build(r.store.path, now=self.clock.time())
        data = summary['held_risk']['data']
        self.assertEqual((data['rug_exit']['trades'], data['rug_writeoff']['trades']), (1, 1))
        self.assertLess(Decimal(data['rug_exit']['pnl_sol']), 0)
        self.assertLess(Decimal(data['rug_writeoff']['pnl_sol']), 0)
        self.assertEqual(data['triggers'], {'LIQUIDITY_DROP': 1, 'NO_ROUTE': 1})
        self.assertEqual(data['unsellable_open'], [])
        self.assertIn('DANGER', summary['exit_reasons']['data'])
        self.assertIn('RUG_WRITEOFF', summary['exit_reasons']['data'])
        self.assertIn('Rugs and write-offs', report.render_html(summary))

    def test_a_rug_exit_that_is_failing_now_is_listed(self):
        token = Token(21, no_route_from=600)
        r = self.build(token)
        self.run_until(r, 1500)
        data = report.build(r.store.path, now=self.clock.time())['held_risk']['data']
        (row,) = data['unsellable_open']
        self.assertEqual((row['mint'], row['trigger']), (token.mint, 'NO_ROUTE'))
        self.assertIsNotNone(row['since'])

    def test_disabled_by_default_and_when_the_key_is_absent(self):
        shipped = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        self.assertFalse(shipped['held_risk']['enabled'])
        token = Token(21, path=steps((600, 0.15)))
        r = self.build(token, held_risk=False)
        self.assertIsNone(r.held_risk)
        self.run_until(r, 1500)
        (closed,) = self.closed(r)
        self.assertEqual(closed['state']['reason'], 'STOP')                                   # today's behaviour
        self.assertIsNone(json.loads((self.state / 'health.json').read_text())['held_risk'])
        # an entirely absent key behaves the same as the shipped (disabled) one
        raw = json.loads((ROOT / 'config' / 'lean' / 'lean.example.json').read_text())
        del raw['held_risk']
        path = self.root / 'no-key.json'
        raw['strategy_config'] = str(ROOT / 'config' / 'lean' / 'strategy-default.json')
        path.write_text(json.dumps(raw))
        self.assertEqual(load_config(path)['held_risk'], {})
        from lean import held_risk
        self.assertIsNone(held_risk.build({}, None, code_version='c', strategy_version='s', clock=lambda: 0))

    def test_the_marks_stay_three_tuples_and_health_reports_the_source(self):
        token = Token(21, no_route_from=600)
        r = self.build(token)
        self.run_until(r, 1500)
        self.assertTrue(all(len(v) == 3 for v in r.marks.values()))
        health = json.loads((self.state / 'health.json').read_text())
        self.assertEqual(health['marks'][token.mint]['source'], 'held_risk')
        self.assertEqual(health['held_risk']['failing_now'], [token.mint])


if __name__ == '__main__':
    unittest.main()
