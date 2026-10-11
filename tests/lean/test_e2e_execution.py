"""SYNTHETIC_TEST_ONLY: L10 end to end. Real lean modules (runner, strategy, store, providers, candidates, report); only HTTP is
faked (tests/lean/fakeworld.py). The fake clock's sleep advances the same clock the pools' price paths read, so the
execution delay really moves the quote.

Tokens (price paths are keyed on the seconds since the token's FIRST buy quote):
  ramp_up     pool price rises ~1%/s for 10 s after entry   -> q1 is WORSE than q0: BUY fills at q1 (latency tax ~300 bps)
  ramp_down   pool price falls ~1%/s for 10 s after entry   -> q1 is BETTER than q0: BUY fills at q0 (tax 0)
  drop_exit   flat, then falls 0.2%/s from 600 s            -> STOP; the SELL quote at q1 (3 s later) is lower than q0
  no_route    the second buy quote (q1) answers HTTP 400    -> ENTRY_ABORTED_LATENCY, no fill, no error row, no halt
"""
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from lean import execution as X, report
from lean.__main__ import build_runner, load_config
from tests.lean.fakeworld import FakeTime, Resp, T0, Token, World

ROOT = Path(__file__).resolve().parents[2]
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}
EXEC = {'exec_delay_s': 3, 'priority_fee_sol': '0.0001', 'jito_tip_sol': '0.0001', 'ata_rent_sol': '0.00203928'}
D = Decimal


def ramp(sign):
    return lambda age: D(1) + sign * D('0.01') * min(D(age), D(10))


def drop(age):
    return D(1) if age < 600 else max(D('0.3'), D(1) - D('0.002') * (D(age) - 600))


SCENARIO = [('ramp_up', dict(path=ramp(1)), 0), ('ramp_down', dict(path=ramp(-1)), 120),
            ('drop_exit', dict(path=drop), 240), ('no_route', dict(), 360)]
END = T0 + 5400


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class ExecutionE2E(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.clock = FakeTime(T0)
        self.tokens = [Token(61 + 2 * i, **kw) for i, (_, kw, _) in enumerate(SCENARIO)]
        self.role = {t.mint: name for t, (name, _, _) in zip(self.tokens, SCENARIO)}
        self.by_role = {name: t for name, t in zip(self.role.values(), self.tokens)}
        self.world = World(self.root, self.tokens, self.clock)
        self.world.add_frames([T0 + off for _, _, off in SCENARIO])
        self.buy_quotes = {}
        base_opener = self.world.opener

        def opener(request, timeout):
            from urllib.parse import parse_qs, urlsplit
            url = urlsplit(request.full_url)
            if url.hostname == 'api.jup.ag':
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if q['inputMint'] == 'So11111111111111111111111111111111111111112':
                    n = self.buy_quotes[q['outputMint']] = self.buy_quotes.get(q['outputMint'], 0) + 1
                    if self.role.get(q['outputMint']) == 'no_route' and n >= 2:        # q0 + the screen-time quote passed; q1 has no route
                        return Resp(b'{"error":"Could not find any route","errorCode":"COULD_NOT_FIND_ANY_ROUTE"}', 400)
            return base_opener(request, timeout)
        self.opener = opener

    def runner(self, state, execution=EXEC):
        cfg = load_config(ROOT / 'tests' / 'lean' / 'lean.baseline.json')
        cfg['execution'] = execution
        return build_runner(cfg, state_dir=state, discovery_db=self.world.discovery_db, keys=KEYS, code_version='e2e-exec',
                            clock=self.clock.time,
                            transport_kwargs={'opener': self.opener, 'clock': self.clock.time, 'monotonic': self.clock.monotonic,
                                              'sleep': self.clock.sleep, 'rng': lambda: 0.5})

    def run_all(self, state=None, execution=EXEC):
        r = self.runner(state or self.root / 'state', execution)
        tick = T0
        while tick <= END:
            if self.clock.time() < tick:
                self.clock.t = tick
            r.position_pass()
            r.candidate_pass()
            r.write_health()
            self.assertIsNone(r.halted, r.halted)
            self.assertTrue(r.store.check_invariants())
            self.assertTrue(r.store.check_position_states())
            tick += 60
        return r

    # helpers
    def exec_rows(self, store):
        """[(role, side, payload, fill row)] from the position_state rows written with each fill."""
        fills = {f['id']: f for f in store.rows('fills', limit=10000)}
        out = []
        for row in store.rows('position_state', limit=10000):
            if row['fill_id'] is None:
                continue
            payload = json.loads(row['state']).get('execution')
            if payload:
                out.append((self.role[row['mint']], payload['side'], payload, fills[row['fill_id']]))
        return out

    # ---------------------------------------------------------------------------------------------------------
    def test_scenario(self):
        r = self.run_all()
        store = r.store
        rows = self.exec_rows(store)
        by = {(role, side): (p, f) for role, side, p, f in rows}
        decisions = store.rows('decisions', limit=10000)
        obs = {o['id']: o for o in store.rows('observations', limit=100000)}

        # BUY: q1 worse than q0 -> fill at q1 (the worse), latency tax = (q0 - fill)/q0 on the tokens received
        p, f = by[('ramp_up', 'buy')]
        self.assertEqual(p['fill'], 'q1')
        self.assertLess(p['q1_out'], p['q0_out'])
        self.assertEqual(p['fill_out'], p['q1_out'])
        self.assertEqual(p['latency_tax_bps'], X.tax_bps(p['q0_out'], p['q1_out']))
        self.assertGreater(Decimal(p['latency_tax_bps']), D(250))
        self.assertLess(Decimal(p['latency_tax_bps']), D(350))
        self.assertEqual(f['qty_raw'], p['q1_out'] * (10000 - 50) // 10000)                  # slippage haircut on the fill quote
        self.assertEqual(f['quote_ref'], str(p['q1_ref']))
        # the delay is real: the two quotes are 3 s apart in (fake) wall time
        self.assertEqual(obs[p['q1_ref']]['ts'] - obs[p['q0_ref']]['ts'], 3.0)
        # cost model on the first buy: base + priority + tip + ATA rent
        self.assertEqual(f['fee_lamports'], 50_000 + 100_000 + 100_000 + 2_039_280)
        self.assertEqual((p['base_fee_lamports'], p['priority_fee_lamports'], p['jito_tip_lamports'], p['ata_rent_paid_lamports']),
                         (50_000, 100_000, 100_000, 2_039_280))

        # BUY: q1 better -> still fill at q0 (the worse of the two), tax 0
        p, f = by[('ramp_down', 'buy')]
        self.assertGreater(p['q1_out'], p['q0_out'])
        self.assertEqual((p['fill'], p['fill_out'], p['latency_tax_bps']), ('q0', p['q0_out'], '0.00'))
        self.assertEqual(f['qty_raw'], p['q0_out'] * (10000 - 50) // 10000)

        # SELL: always q1; the price fell in the 3 s -> positive tax; the ATA rent comes back with the full close
        p, f = by[('drop_exit', 'sell')]
        self.assertEqual(p['fill'], 'q1')
        self.assertLess(p['q1_out'], p['q0_out'])
        self.assertEqual(f['sol_lamports'], p['q1_out'] * (10000 - 50) // 10000 + 2_039_280)
        self.assertEqual(f['fee_lamports'], 50_000 + 100_000 + 100_000)
        self.assertEqual(p['ata_refund_lamports'], 2_039_280)
        self.assertGreater(Decimal(p['latency_tax_bps']), D(0))
        self.assertEqual(f['quote_ref'], str(p['q1_ref']))
        exits = [d for d in decisions if d['kind'] == 'exit' and self.role[d['mint']] == 'drop_exit' and d['action'] == 'SELL']
        self.assertEqual(json.loads(exits[0]['features'])['quote_ref'], p['q1_ref'])        # the exit decision saw the quote it filled at
        self.assertEqual(obs[p['q1_ref']]['ts'] - obs[p['q0_ref']]['ts'], 3.0)

        # q1 without a route: dropped and recorded, not an error, no fill, no halt
        aborted = [d for d in decisions if d['action'] == 'ENTRY_ABORTED_LATENCY']
        self.assertEqual([self.role[d['mint']] for d in aborted], ['no_route'])
        self.assertEqual(json.loads(aborted[0]['reasons']), ['Q1_UNAVAILABLE:HTTP_400'])
        self.assertFalse([f for f in store.rows('fills', limit=10000) if self.role[f['mint']] == 'no_route'])
        self.assertFalse([e for e in store.rows('errors', limit=10000) if e['mint'] == self.by_role['no_route'].mint])
        self.assertEqual(r.counts['entry_aborted_latency'], 1)

        # accounting: every position closed, rent netted to zero, cash = initial + realized, store replay agrees
        self.assertEqual(store.positions(), {})
        paid = sum(p['ata_rent_paid_lamports'] for _, _, p, _ in rows)
        refunded = sum(p['ata_refund_lamports'] for _, _, p, _ in rows)
        self.assertEqual((paid, refunded), (3 * 2_039_280, 3 * 2_039_280))
        self.assertEqual(store.cash(), store.initial_cash + store.realized())
        self.assertEqual(sum(int(c['state']['trade_pnl_lamports']) for c in store.closed_positions()), store.realized())

        # report: latency tax p50/p90 per side and the fee breakdown agree with the fills
        summary = report.build(store.path, now=self.clock.time())
        x = summary['execution']
        self.assertEqual(x['status'], 'OK')
        d = x['data']
        self.assertEqual(d['fills_with_execution'], len(rows))
        self.assertEqual(d['latency_tax_bps']['buy']['n'], 3)
        self.assertEqual(d['latency_tax_bps']['sell']['n'], len(rows) - 3)
        self.assertIsNotNone(d['latency_tax_bps']['buy']['p90'])
        self.assertEqual(d['entries_aborted_latency'], 1)
        self.assertEqual(d['sample'], 'INSUFFICIENT_SAMPLE')
        all_fees = sum(f['fee_lamports'] for _, _, _, f in rows)
        self.assertEqual(Decimal(d['fees_sol']['trading_fees_total']) * 10 ** 9, all_fees - paid)
        self.assertEqual(Decimal(d['fees_sol']['ata_rent_outstanding']), 0)
        html_text = report.render_html(summary)
        self.assertIn('Execution: latency tax and fees', html_text)
        health = json.loads((self.root / 'state' / 'health.json').read_text())
        self.assertIsNone(health['halted'])

    def test_restart_mid_position_keeps_rent_and_fee_state(self):
        state = self.root / 'state'
        r = self.runner(state)
        tick = T0
        while tick <= T0 + 1200:                      # up to the entries, positions still open
            if self.clock.time() < tick:
                self.clock.t = tick
            r.position_pass(); r.candidate_pass()
            tick += 60
        opened = {self.role[m]: s['state'] for m, s in r.store.position_states().items()}
        self.assertIn('ramp_up', opened)
        self.assertEqual(opened['ramp_up']['ata_rent_lamports'], 2_039_280)
        r.store.close()
        r = self.runner(state)                        # a fresh process: the per-position cost state comes from the store
        while tick <= END:
            if self.clock.time() < tick:
                self.clock.t = tick
            r.position_pass(); r.candidate_pass()
            self.assertIsNone(r.halted, r.halted)
            tick += 60
        self.assertEqual(r.store.positions(), {})
        refunds = [p['ata_refund_lamports'] for _, side, p, _ in self.exec_rows(r.store) if side == 'sell' and p['ata_refund_lamports']]
        self.assertEqual(refunds, [2_039_280] * 3)

    def test_transfer_fee_flows_through_the_fill_the_state_and_the_exit(self):
        with mock.patch.object(X, 'transfer_fee_bps_from_screen', return_value=200):
            r = self.run_all()
        rows = self.exec_rows(r.store)
        buys = [(p, f) for _, side, p, f in rows if side == 'buy']
        self.assertEqual(len(buys), 3)
        for p, f in buys:
            held = p['fill_out'] * (10000 - 50) // 10000
            self.assertEqual(f['qty_raw'], held * 9800 // 10000)
            self.assertEqual(p['transfer_fee_lost_tokens_raw'], held - f['qty_raw'])
        sells = [(p, f) for _, side, p, f in rows if side == 'sell']
        self.assertTrue(sells and all(p['transfer_fee_bps'] == 200 and p['transfer_fee_lost_lamports'] > 0 for p, _ in sells))
        self.assertEqual(r.store.cash(), r.store.initial_cash + r.store.realized())

    def test_unreadable_transfer_fee_aborts_the_entry_without_an_error(self):
        with mock.patch.object(X, 'transfer_fee_bps_from_screen', side_effect=X.ExecutionError('TRANSFER_FEE_UNREADABLE')):
            r = self.run_all()
        self.assertEqual(r.store.rows('fills', limit=100), [])
        aborted = [d for d in r.store.rows('decisions', limit=1000) if d['action'] == 'ENTRY_ABORTED_LATENCY']
        self.assertEqual({tuple(json.loads(d['reasons'])) for d in aborted}, {('TRANSFER_FEE_UNREADABLE',)})
        self.assertFalse(r.store.rows('errors', limit=1000))

    def test_exit_requote_failure_leaves_the_position_open_and_it_exits_later(self):
        """q1 fails for the first exit attempt (Jupiter 503 window): nothing is sold, no duplicate, the next tick exits."""
        fail = {'on': False, 'sold_quotes': 0}
        base = self.opener

        def opener(request, timeout):
            from urllib.parse import parse_qs, urlsplit
            url = urlsplit(request.full_url)
            if fail['on'] and url.hostname == 'api.jup.ag':
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if q['outputMint'] == 'So11111111111111111111111111111111111111112':
                    fail['sold_quotes'] += 1
                    if fail['sold_quotes'] > 1:                                       # q0 passes, every re-quote attempt fails
                        return Resp(b'{"error":"unavailable"}', 503)
            return base(request, timeout)
        self.opener = opener
        r = self.runner(self.root / 'state')
        tick = T0
        first_blocked = None
        while tick <= END:
            if self.clock.time() < tick:
                self.clock.t = tick
            if tick >= T0 + 240 + 720 and first_blocked is None and any(self.role[m] == 'drop_exit' for m in r.store.positions()):
                fail['on'] = True
            had = any(self.role[m] == 'drop_exit' for m in r.store.positions())
            r.position_pass(); r.candidate_pass()
            if fail['on'] and first_blocked is None and had and any(self.role[m] == 'drop_exit' for m in r.store.positions()):
                first_blocked = tick                       # the exit was wanted, the re-quote failed, the position is still open
                fail['on'] = False
            self.assertIsNone(r.halted, r.halted)
            tick += 60
        self.assertIsNotNone(first_blocked)
        self.assertEqual(r.store.positions(), {})
        sells = [f for f in r.store.rows('fills', limit=1000) if f['side'] == 'sell' and self.role[f['mint']] == 'drop_exit']
        self.assertEqual(len(sells), 1)
        self.assertIn('HTTP_503', {e['code'] for e in r.store.rows('errors', limit=1000)})

    def test_default_off_is_todays_behaviour(self):
        r = self.run_all(execution=None)
        rows = self.exec_rows(r.store)
        self.assertEqual(rows, [])
        fills = r.store.rows('fills', limit=1000)
        self.assertTrue(fills)
        self.assertEqual({f['fee_lamports'] for f in fills}, {50_000})                  # fixed fee only, no rent, no tip
        self.assertEqual(self.exec_rows(r.store), [])
        self.assertFalse([d for d in r.store.rows('decisions', limit=1000) if d['action'] == 'ENTRY_ABORTED_LATENCY'])
        self.assertEqual(report.build(r.store.path, now=self.clock.time())['execution']['data']['fills_with_execution'], 0)
        # the no_route token's second buy quote is simply not asked for without the model
        self.assertIn('no_route', {self.role[f['mint']] for f in fills})


if __name__ == '__main__':
    unittest.main()


# ----------------------------------------------------------------------------------------------------------------------------
# L10F: two-phase exits, q1 failures through D2, rent-free presentation. Seven tokens, six of which crash at the same instant.
N_TOKENS = 7
DROP_AT = T0 + 600


class TwoPhaseExits(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.clock = FakeTime(T0)
        crash = lambda age: D(1) if self.clock.time() < DROP_AT else D('0.7')       # absolute time: six exits trigger in one pass
        self.tokens = [Token(81 + 2 * i, path=(crash if i < 6 else None)) for i in range(N_TOKENS)]
        self.role = {t.mint: 'crash%d' % i if i < 6 else 'flat' for i, t in enumerate(self.tokens)}
        self.world = World(self.root, self.tokens, self.clock)
        self.world.add_frames([T0 + 60 * i for i in range(N_TOKENS)])
        strategy = json.loads((ROOT / 'config' / 'lean' / 'strategy-default.json').read_text())
        strategy.update(max_positions=8, max_exposure_fraction='0.4')
        self.strategy_path = self.root / 'strategy.json'
        self.strategy_path.write_text(json.dumps(strategy))
        self.fail_sell_quotes = {}                                                    # mint -> number of sell quotes still allowed
        base = self.world.opener

        def opener(request, timeout):
            from urllib.parse import parse_qs, urlsplit
            url = urlsplit(request.full_url)
            if url.hostname == 'api.jup.ag':
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if q['outputMint'] == 'So11111111111111111111111111111111111111112' and q['inputMint'] in self.fail_sell_quotes:
                    left = self.fail_sell_quotes[q['inputMint']]
                    if left <= 0:
                        return Resp(b'{"error":"Could not find any route","errorCode":"COULD_NOT_FIND_ANY_ROUTE"}', 400)
                    self.fail_sell_quotes[q['inputMint']] = left - 1
            return base(request, timeout)
        self.opener = opener

    def runner(self, unexitable_after_s=7200):
        cfg = load_config(ROOT / 'tests' / 'lean' / 'lean.baseline.json')
        cfg.update(execution=EXEC, strategy_config=str(self.strategy_path), unexitable_after_s=unexitable_after_s)
        return build_runner(cfg, state_dir=self.root / 'state', discovery_db=self.world.discovery_db, keys=KEYS, code_version='e2e-l10f',
                            clock=self.clock.time,
                            transport_kwargs={'opener': self.opener, 'clock': self.clock.time, 'monotonic': self.clock.monotonic,
                                              'sleep': self.clock.sleep, 'rng': lambda: 0.5})

    def tick_to(self, r, end, step=60):
        tick = int(self.clock.time() // step * step) if self.clock.time() > T0 else T0
        while tick <= end:
            if self.clock.time() < tick:
                self.clock.t = tick
            r.position_pass(); r.candidate_pass(); r.write_health()
            self.assertIsNone(r.halted, r.halted)
            self.assertTrue(r.store.check_invariants())
            tick += step

    def enter_all(self):
        r = self.runner()
        self.tick_to(r, DROP_AT - 30)                               # all seven are open before the crash
        self.assertEqual(len(r.store.positions()), N_TOKENS)
        return r

    # ------------------------------------------------------------------------------------------------------------
    def test_six_simultaneous_exits_share_one_delay_and_all_fill_in_the_same_pass(self):
        r = self.enter_all()
        delays, calls = [], []
        real_sleep = r.execution.sleep
        r.execution.sleep = lambda s: (delays.append(s), real_sleep(s))
        manage = r._manage

        def spy(mint, position, row, liquidate):
            calls.append((self.role[mint], r.execution._phase))
            return manage(mint, position, row, liquidate)
        r._manage = spy
        self.clock.t = DROP_AT + 30
        fills_before = len(r.store.rows('fills', limit=10000))
        exits = r.position_pass()
        self.assertEqual(exits, 6)
        self.assertEqual(sorted(r.store.positions()), [t.mint for t in self.tokens if self.role[t.mint] == 'flat'])
        # ONE shared delay for the pass (serial re-quoting would sleep six times), never longer than exec_delay_s
        self.assertEqual(len(delays), 1, delays)
        self.assertLessEqual(delays[0], 3.0)
        # phase order: the flat position is finished in phase 1; every crashed one is seen twice, 'collect' then 'finish'
        self.assertEqual(calls.count(('flat', 'collect')), 1)
        self.assertNotIn(('flat', 'finish'), calls)
        for i in range(6):
            self.assertEqual([p for role, p in calls if role == 'crash%d' % i], ['collect', 'finish'])
        self.assertLess(calls.index(('flat', 'collect')), calls.index(('crash0', 'finish')))
        # every exit is a SELL decision on its q1 (the decision as of the decision time: no STALE blocks although the pass lasted
        # longer than the strategy's 10 s freshness window because of the providers' own rate limits)
        decisions = [d for d in r.store.rows('decisions', limit=10000) if d['kind'] == 'exit']
        self.assertEqual({d['action'] for d in decisions}, {'SELL'})
        self.assertEqual(len(decisions), 6)
        self.assertEqual(len(r.store.rows('fills', limit=10000)) - fills_before, 6)
        # each q1 is at least exec_delay_s after its own q0
        obs = {o['id']: o for o in r.store.rows('observations', limit=100000)}
        payloads = [json.loads(s['state'])['execution'] for s in r.store.rows('position_state', limit=10000)
                    if s['event'] == 'closed' and s['fill_id']]
        self.assertEqual(len(payloads), 6)
        for p in payloads:
            self.assertGreaterEqual(obs[p['q1_ref']]['ts'] - obs[p['q0_ref']]['ts'], 3.0)
            self.assertGreater(Decimal(p['latency_tax_bps']) + D(1), D(0))
        # the flat position is still being managed normally afterwards
        self.assertEqual(r.execution._phase, None)
        self.assertEqual(r.position_pass(), 0)

    def test_q1_failure_goes_through_d2_and_the_write_off_clears_the_rent(self):
        r = self.runner(unexitable_after_s=180)
        self.tick_to(r, DROP_AT - 30)
        victim = self.tokens[0].mint
        self.fail_sell_quotes[victim] = 1                       # q0 passes, every later sell quote of this token has no route
        self.tick_to(r, DROP_AT + 30)
        state = r.store.position_states()[victim]['state']
        self.assertIsNotNone(state.get('exit_failing_since'), 'a failed q1 must start the D2 write-off clock')
        self.assertEqual(state['exit_failing_reason'], 'HTTP_400')
        self.assertIn(victim, r.store.positions())                # not sold, not lost
        other = [t.mint for t in self.tokens[1:6]]
        self.assertTrue(all(m not in r.store.positions() for m in other))   # the others exited normally in the same pass
        self.tick_to(r, DROP_AT + 400)
        self.assertNotIn(victim, r.store.positions())
        closed = [c for c in r.store.closed_positions() if c['mint'] == victim]
        self.assertEqual(closed[0]['state']['reason'], 'UNEXITABLE')
        self.assertEqual(r.counts['write_offs'], 1)
        # the written-off position's rent is lost and cleared from the outstanding rent, never refunded
        x = report.build(r.store.path, now=self.clock.time())['execution']['data']
        self.assertEqual(Decimal(x['fees_sol']['ata_rent_written_off']), Decimal(2_039_280) / 10 ** 9)
        self.assertEqual(Decimal(x['fees_sol']['ata_rent_outstanding']), Decimal(1) * 2_039_280 / 10 ** 9)   # only the flat position's
        self.assertTrue(r.store.check_invariants())
        self.assertEqual(r.store.cash() + sum(p.cost_lamports for p in r.store.positions().values()),
                         r.store.initial_cash + r.store.realized())

    def test_prices_fees_and_returns_shown_to_people_exclude_the_refundable_rent(self):
        from lean import notify
        r = self.enter_all()
        self.clock.t = DROP_AT + 30
        r.position_pass()
        store = r.store
        raw = {f['id']: f for f in store.rows('fills', limit=10000)}
        payloads = {s['fill_id']: json.loads(s['state'])['execution'] for s in store.rows('position_state', limit=10000)
                    if s['fill_id'] and 'execution' in json.loads(s['state'])}
        # report: trade cost / proceeds are rent-free and the PnL is exactly the store's
        summary = report.build(store.path, now=self.clock.time())
        rows = {t['mint']: t for t in summary['trades']['data']['rows'] if t['status'] == 'CLOSED'}
        self.assertEqual(len(rows), 6)
        for c in store.closed_positions():
            buy = next(f for f in raw.values() if f['mint'] == c['mint'] and f['side'] == 'buy')
            sell = raw[c['fill_id']]
            row = rows[c['mint']]
            self.assertAlmostEqual(row['cost_sol'], (buy['sol_lamports'] + buy['fee_lamports'] - 2_039_280) / 1e9, places=6)
            self.assertAlmostEqual(row['proceeds_sol'], (sell['sol_lamports'] - 2_039_280 - sell['fee_lamports']) / 1e9, places=6)
            self.assertAlmostEqual(row['pnl_sol'], int(c['state']['trade_pnl_lamports']) / 1e9, places=6)
        # notifier: the SELL price is the fill quote's, not inflated by the refund; the BUY fee has no rent in it
        view = notify.StoreView(notify.open_store(store.path))
        try:
            for f in view.fills_after(0):
                event = notify.fill_event(view, f, sol_usd_max_age=3600)
                raw_row, payload = raw[f['id']], payloads[f['id']]
                if f['side'] == 'buy':
                    self.assertEqual(f['fee_lamports'], 50_000 + 100_000 + 100_000 + 2_039_280 - payload['ata_rent_paid_lamports'])
                    self.assertEqual(event['fee_sol'], Decimal(f['fee_lamports']) / 10 ** 9)
                else:
                    self.assertEqual(f['sol_lamports'], raw_row['sol_lamports'] - payload['ata_refund_lamports'])
                    self.assertEqual(f['sol_lamports'], payload['q1_out'] * (10000 - 50) // 10000)
                    self.assertEqual(event['size_sol'], Decimal(payload['q1_out'] * 9950 // 10000) / 10 ** 9)
                    pnl = [c for c in store.closed_positions() if c['fill_id'] == f['id']][0]
                    self.assertEqual(event['trade_pnl_sol'] * 10 ** 9, int(pnl['state']['trade_pnl_lamports']))
                    self.assertEqual(event['realized_sol'] * 10 ** 9, f['realized_lamports'])
                    self.assertEqual(f['realized_lamports'], raw_row['realized_lamports'] - payload['ata_refund_lamports'] + payload['ata_rent_basis_sold_lamports'])
            # the accounting replay still reads the raw rows: cash is the store's cash
            self.assertEqual(view.replay()[1], store.cash())
        finally:
            view.close()

    def test_cost_checks_and_marks_use_the_fee_the_fills_pay(self):
        r = self.runner()
        self.assertEqual(r.cfg.fixed_fee_sol, D('0.00025'))
        self.assertEqual(r.pcfg.fee_lamports, 250_000)
        start = r.store.latest_event('execution_config')[1]
        self.assertEqual((start['tx_extra_lamports'], start['fixed_fee_sol']), (200_000, '0.00025'))
        # the round-trip cost cap counts 5 fees: priority fee + tip move a knife-edge entry from BUY to COST_BUDGET
        from lean import strategy as S
        base_cfg = S.StrategyConfig.load(self.strategy_path)
        rt = S.RoundTrip(S.Quote('buy', D('0.2'), D('1000000'), 1_800_000_000), S.Quote('sell', D('1000000'), D('0.1853'), 1_800_000_000))
        self.assertLess(S.roundtrip_cost_fraction(rt, base_cfg), base_cfg.max_roundtrip_cost_fraction)
        self.assertGreater(S.roundtrip_cost_fraction(rt, r.cfg), base_cfg.max_roundtrip_cost_fraction)
