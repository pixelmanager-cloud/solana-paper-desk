"""SYNTHETIC_TEST_ONLY (L20): sellability evidence (lean/sell_evidence.py) and its booking (lean/execution.py finish_sell, paper.write_off).
Fixtures only, no network: the pools are tests/lean/tape.py tapes in fake time; the store is a real lean store."""
import json
import os
import tempfile
import threading
import time
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from lean import execution as X, flow as FL, notify as N, paper, sell_evidence as SE
from lean.providers import LANE_SHED, PAPER_TAKER, ProviderError
from lean.store import Store
from tests.lean import tape as TP
from tests.lean.test_flow import Now, TapeHelius, token

T = 1_800_000_000.0
CFG = SE.config({'enabled': True})


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class ConfigTests(unittest.TestCase):
    def test_absent_is_off_and_the_defaults(self):
        self.assertFalse(SE.config(None)['enabled'])
        self.assertEqual((CFG['min_organic_sells'], CFG['sell_window_s'], CFG['unverified_sell_policy'], CFG['max_tx_samples'], CFG['cache_s']),
                         (2, 300, 'flag_only', 8, 60))
        self.assertEqual(SE.config(CFG), CFG)

    def test_unknown_and_wrong_values_are_refused(self):
        for bad in ({'enable': True}, {'enabled': 'yes'}, {'unverified_sell_policy': 'drop'}, {'min_organic_sells': 0}, {'max_tx_samples': 2.0},
                    {'cache_s': -1}, {'pace_s': 0}, {'probe_timeout_s': True}, [], 'x'):
            with self.subTest(bad=bad), self.assertRaises(SE.ConfigError):
                SE.config(bad)


class ProbeTests(unittest.TestCase):
    """``probe`` on tapes; ``now`` is T + 1000."""

    def run_probe(self, trades, *, gate=None, exclude=(), **cfg):
        tape, clock = TP.Tape(None), Now(T + 1000)
        tok = token(3)
        tape.add(tok, trades)
        helius = TapeHelius(tape, clock, gate)
        conf = SE.config({'enabled': True, **cfg})
        calls = FL.LowCalls(helius, limit=1 + conf['max_tx_samples'], budget=FL.HourBudget(1000, clock), wait=lambda s: None, pace_s=0.1,
                            patience_s=0)
        result = SE.probe(calls, tok.pool, tok.mint, now=T + 1000, cfg=conf, exclude=set(exclude))
        return result, tape

    def test_an_organic_pool_is_verified_and_the_probe_stops_early(self):
        result, tape = self.run_probe(TP.organic(T, T + 1000, seed=5))
        self.assertEqual((result['verdict'], result['sellers']), ('VERIFIED', 2))
        self.assertLess(result['calls'], 1 + 8)
        self.assertEqual(result['calls'], 1 + result['sampled'])

    def test_a_honeypot_is_unverified_after_the_whole_sample(self):
        result, tape = self.run_probe(TP.honeypot(T, T + 1000))
        self.assertEqual((result['verdict'], result['sellers'], result['sampled'], result['calls']), ('UNVERIFIED', 0, 8, 9))
        self.assertEqual(result['why'], 'TOO_FEW_ORGANIC_SELLERS')
        self.assertEqual(result['window_txs'], 10)                                  # the failed sells are not even candidates

    def test_a_quiet_pool_and_old_sells_are_not_evidence(self):
        result, _ = self.run_probe(TP.organic(T, T + 600, seed=5))                  # every trade older than 300 s
        self.assertEqual((result['verdict'], result['why'], result['calls']), ('UNVERIFIED', 'NO_SELLS_IN_WINDOW', 1))
        result, _ = self.run_probe(TP.organic(T, T + 600, seed=5) + [(T + 900 + i, TP.wallet(700 + i), 'buy', 10 ** 8, False) for i in range(5)])
        self.assertEqual((result['verdict'], result['sampled']), ('UNVERIFIED', 5))

    def test_the_coin_creator_and_the_taker_never_count(self):
        creator = TP.wallet(4242)
        trades = TP.only_by(T + 700, T + 1000, creator) + TP.only_by(T + 701, T + 1000, PAPER_TAKER, base=950)
        result, _ = self.run_probe(trades, exclude={creator, PAPER_TAKER})
        self.assertEqual((result['verdict'], result['sellers']), ('UNVERIFIED', 0))
        result, _ = self.run_probe(trades, exclude={PAPER_TAKER}, min_organic_sells=1)
        self.assertEqual((result['verdict'], result['sellers']), ('VERIFIED', 1))
        result, _ = self.run_probe(trades, exclude=set())
        self.assertEqual(result['verdict'], 'VERIFIED')                            # sanity: without the exclusion they would count

    def test_a_full_page_that_does_not_reach_the_window_start_is_unknown_not_unverified(self):
        # LINT4 review: on a busy pool a 1000-signature page covers ~90 s of the 300 s window; absence is not proven
        trades = [(T + 1000 - 50 + i // 10, TP.wallet(800 + i % 7), 'buy', 10 ** 8, False) for i in range(60)]
        result, _ = self.run_probe(trades, sig_limit=40)
        self.assertEqual((result['verdict'], result['why']), ('UNKNOWN', 'WINDOW_NOT_COVERED'))
        result, _ = self.run_probe(trades, sig_limit=1000)
        self.assertEqual(result['verdict'], 'UNVERIFIED')                           # the whole window was seen: absence is proven
        result, _ = self.run_probe(trades, max_unverified_window=30)
        self.assertEqual((result['verdict'], result['why']), ('UNKNOWN', 'SAMPLE_TOO_SMALL'))

    def test_one_wallet_selling_many_times_is_one_seller(self):
        trades = TP.only_by(T + 700, T + 1000, TP.wallet(4343))                          # many sells, ONE signer
        result, _ = self.run_probe(trades)
        self.assertEqual((result['verdict'], result['sellers']), ('UNVERIFIED', 1))

    def test_a_periodic_bot_cannot_hide_its_sellers_from_the_sample(self):
        trades = []
        for i in range(16):                                                              # buy, sell, sell every 18 s, distinct sellers
            t = T + 1000 - 288 + 18 * i
            trades += [(t, TP.wallet(600 + i), 'buy', 10 ** 8, False), (t + 6, TP.wallet(700 + i), 'sell', 10 ** 8, False),
                       (t + 12, TP.wallet(800 + i), 'sell', 10 ** 8, False)]
        result, _ = self.run_probe(trades, max_tx_samples=4)
        self.assertEqual(result['window_txs'], 48)                                       # an even spread (every 12th) sees only buys
        self.assertEqual(result['verdict'], 'VERIFIED')

    def test_an_incomplete_sample_a_shed_or_a_provider_error_is_unknown(self):
        def fail_second_tx(method, n):
            return ProviderError('HTTP_500', True) if n == 3 else None
        result, _ = self.run_probe(TP.honeypot(T, T + 1000), gate=fail_second_tx)
        self.assertEqual((result['verdict'], result['why'], result['sampled']), ('UNKNOWN', 'SAMPLE_INCOMPLETE', 7))
        window = ProviderError(LANE_SHED, True, meta={'why': 'backoff'})
        result, _ = self.run_probe(TP.honeypot(T, T + 1000), gate=lambda m, n: window if n == 4 else None)
        self.assertEqual((result['verdict'], result['why']), ('UNKNOWN', LANE_SHED))
        result, _ = self.run_probe(TP.honeypot(T, T + 1000), gate=lambda m, n: ProviderError('HTTP_503', True) if n == 1 else None)
        self.assertEqual((result['verdict'], result['why']), ('UNKNOWN', 'PROVIDER_HTTP_503'))
        result, _ = self.run_probe(TP.honeypot(T, T + 1000), gate=lambda m, n: ProviderError(LANE_SHED, True, meta={'why': 'no_token'}))
        self.assertEqual((result['verdict'], result['why']), ('UNKNOWN', LANE_SHED))   # no patience left: absence not proven

    def test_payload_fields(self):
        self.assertEqual(SE.payload_fields(SE.unknown('LANE_SHED')), {'sell_evidence': 'UNKNOWN', 'sell_evidence_why': 'LANE_SHED'})
        ok = SE.payload_fields({'verdict': 'VERIFIED', 'sellers': 2, 'sampled': 3, 'window_txs': 40, 'why': None})
        self.assertEqual(ok, {'sell_evidence': {'verdict': 'VERIFIED', 'sellers': 2, 'sampled': 3, 'window_txs': 40}})
        bad = SE.payload_fields({'verdict': 'UNVERIFIED', 'sellers': 0, 'sampled': 8, 'why': 'TOO_FEW_ORGANIC_SELLERS'})
        self.assertEqual(bad['sell_status'], 'SELL_UNVERIFIED')


class ProberTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(os.path.realpath(tmp.name))
        self.clock = Now(T + 1000)
        self.store = Store(self.dir / 'lean.sqlite', initial_cash_sol='10', code_version='t', strategy_version='s', clock=self.clock)
        self.addCleanup(self.store.close)
        self.tape = TP.Tape(None)
        self.tok = token(3)
        self.tape.add(self.tok, TP.organic(T, T + 2000, seed=5))
        self.release = threading.Event()
        self.release.set()

    def prober(self, gate=None, **cfg):
        def blocking(method, n):
            self.release.wait(10)
            return gate(method, n) if gate else None
        self.helius = TapeHelius(self.tape, self.clock, blocking)
        return SE.SellEvidence(SE.config({'enabled': True, **cfg}), helius=self.helius, store=self.store, clock=self.clock)

    def test_start_never_blocks_and_a_pending_probe_is_unknown(self):
        self.release.clear()
        p = self.prober()
        started = time.monotonic()
        handle = p.start(self.tok.mint, self.tok.pool)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIs(p.start(self.tok.mint, self.tok.pool), handle)                  # one probe in flight per pool
        self.assertEqual(p.verdict(handle), {'verdict': 'UNKNOWN', 'why': 'PENDING', 'sellers': None, 'sampled': None})
        waited = p.wait([handle], 0.05)
        self.assertLess(waited, 1.0)                                                  # bounded by the timeout, not by the probe
        self.release.set()
        p.wait([handle], 5)
        self.assertEqual(p.verdict(handle)['verdict'], 'VERIFIED')
        self.assertEqual(p.counts['pending_at_fill'], 1)

    def test_conclusive_results_are_cached_for_cache_s_and_unknown_never(self):
        p = self.prober()
        h = p.start(self.tok.mint, self.tok.pool)
        p.wait([h], 5)
        n = self.helius.n
        self.clock.t += 59
        cached = p.start(self.tok.mint, self.tok.pool)
        self.assertTrue(cached.event.is_set() and cached.result['cached'])
        self.assertEqual(self.helius.n, n)
        self.clock.t += 2
        h = p.start(self.tok.mint, self.tok.pool)
        p.wait([h], 5)
        self.assertGreater(self.helius.n, n)
        shed = ProviderError(LANE_SHED, True, meta={'why': 'backoff'})
        p = self.prober(gate=lambda m, k: shed)
        h = p.start(self.tok.mint, self.tok.pool)
        p.wait([h], 5)
        self.assertEqual(p.verdict(h)['verdict'], 'UNKNOWN')
        h2 = p.start(self.tok.mint, self.tok.pool)
        self.assertFalse(h2.cached)

    def test_the_hourly_cap_ends_in_unknown(self):
        p = self.prober(max_calls_per_hour=2)
        h = p.start(self.tok.mint, self.tok.pool)
        p.wait([h], 5)
        result = p.verdict(h)
        if result['verdict'] != 'VERIFIED':                                          # two calls can be enough on a lucky sample
            self.assertEqual((result['verdict'], result['why']), ('UNKNOWN', 'HOURLY_BUDGET'))
        self.clock.t += 61
        h = p.start(self.tok.mint, self.tok.pool)
        p.wait([h], 5)
        self.assertEqual((p.verdict(h)['verdict'], p.verdict(h)['why']), ('UNKNOWN', 'HOURLY_BUDGET'))
        self.assertEqual(p.budget.used(), 2)

    def test_the_coin_creator_comes_from_the_pool_account_the_screen_kept(self):
        from tests.lean.fakeworld import Token
        tok = Token(77, coin_creator_tag=99)
        body = json.dumps({'result': {'value': [tok.mint_account(), tok.account(tok.pool_raw, 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA')]}})
        self.store.add_observation('screen:accounts_mint_pool', body.encode(), mint=tok.mint, ts=T)
        p = self.prober()
        self.assertEqual(p.creator(tok.mint), tok.coin_creator)
        self.assertIsNone(p.creator(TP.wallet(1)))


class BookingTests(unittest.TestCase):
    """``finish_sell`` + ``paper.write_off(qty_raw=)`` through the ONE accounting rule (``paper.apply_fill``)."""

    def setUp(self):
        self.x = X.Execution(X.ExecConfig())
        self.pcfg = paper.PaperConfig(fee_lamports=250_000, slippage_bps=50)
        q = paper.Quote('M', 'buy', 10 ** 9, 10 ** 12, 6, T)
        fill = paper.buy(q, '1', self.pcfg)
        fill = paper.Fill(**{**fill.__dict__, 'fee_lamports': fill.fee_lamports + 2_039_280})       # the first buy pays the ATA rent
        self.positions, self.cash = paper.apply_fill({}, 10 * 10 ** 9, fill)
        self.position = self.positions['M']
        self.state = {'ata_rent_lamports': 2_039_280, 'initial_qty_raw': self.position.qty_raw, 'transfer_fee_bps': 0}

    def sell(self, qty, meta):
        q = paper.Quote('M', 'sell', qty, 2 * 10 ** 9 * qty // self.position.qty_raw, 6, T + 60, ref=7)
        fill = paper.sell(self.position, q, cfg=self.pcfg, qty_raw=qty)
        return fill, self.x.finish_sell(fill, self.position, self.state, meta)

    def meta(self, **extra):
        return {'delay_s': 3, 'q0_ref': 6, 'q0_out': 10 ** 9, 'q1_ref': 7, 'q1_out': 10 ** 9, 'fill': 'q1', 'fill_out': 10 ** 9, 'fill_ref': 7, **extra}

    def unverified(self, policy='zero'):
        return self.meta(sell_evidence={'verdict': 'UNVERIFIED', 'sellers': 0, 'sampled': 8}, sell_status='SELL_UNVERIFIED',
                         unverified_sell_policy=policy)

    def test_a_partial_unverified_sell_is_a_zero_proceeds_write_off_of_the_same_qty(self):
        qty = self.position.qty_raw * 3 // 10
        quoted, (fill, extra) = self.sell(qty, self.unverified())
        self.assertEqual((fill.side, fill.qty_raw, fill.sol_lamports, fill.fee_lamports, fill.quote_ref), ('sell', qty, 0, 0, 7))
        self.assertEqual(fill.cost_sold_lamports, self.position.cost_lamports * qty // self.position.qty_raw)
        self.assertEqual(fill.realized_lamports, -fill.cost_sold_lamports)
        p = extra['execution']
        self.assertEqual((p['kind'], p['write_off_reason'], p['sell_status'], p['unverified_sell_policy']),
                         ('write_off', 'SELL_UNVERIFIED', 'SELL_UNVERIFIED', 'zero'))
        self.assertEqual(p['unverified_quote_sol_lamports'], quoted.sol_lamports)
        self.assertEqual((p['ata_rent_written_off_lamports'], p['ata_refund_lamports']), (0, 0))
        self.assertEqual(p['ata_rent_basis_sold_lamports'], 2_039_280 - 2_039_280 * (self.position.qty_raw - qty) // self.position.qty_raw)
        positions, cash = paper.apply_fill(self.positions, self.cash, fill)                # the one accounting rule accepts it
        self.assertEqual(cash, self.cash)
        self.assertEqual(positions['M'].qty_raw, self.position.qty_raw - qty)
        econ = X.economic({'side': 'sell', 'sol_lamports': 0, 'fee_lamports': 0, 'realized_lamports': fill.realized_lamports,
                           'cost_sold_lamports': fill.cost_sold_lamports}, p)
        self.assertEqual(econ['realized_lamports'], fill.realized_lamports + p['ata_rent_basis_sold_lamports'])

    def test_a_full_unverified_close_writes_the_rent_off(self):
        _, (fill, extra) = self.sell(self.position.qty_raw, self.unverified())
        self.assertEqual((fill.sol_lamports, fill.realized_lamports), (0, -self.position.cost_lamports))
        p = extra['execution']
        self.assertEqual((p['ata_rent_written_off_lamports'], p['ata_rent_basis_sold_lamports']), (2_039_280, 0))
        positions, _ = paper.apply_fill(self.positions, self.cash, fill)
        self.assertEqual(positions, {})

    def test_flag_only_verified_and_unknown_sells_are_booked_at_the_quote(self):
        plain_quote, (plain, _) = self.sell(1000, self.meta())
        for meta in (self.unverified('flag_only'), self.meta(sell_evidence={'verdict': 'VERIFIED', 'sellers': 2, 'sampled': 3}),
                     self.meta(sell_evidence='UNKNOWN', sell_evidence_why='LANE_SHED')):
            _, (fill, extra) = self.sell(1000, meta)
            self.assertEqual(fill, plain)
            self.assertNotEqual(extra['execution'].get('kind'), 'write_off')
            self.assertEqual(extra['execution']['sell_evidence'], meta['sell_evidence'])
        self.assertEqual(self.sell(1000, self.unverified('flag_only'))[1][1]['execution']['sell_status'], 'SELL_UNVERIFIED')

    def test_the_write_off_constructor_refuses_bad_quantities_and_keeps_the_d2_shape(self):
        for bad in (0, -1, self.position.qty_raw + 1, 1.5, True):
            with self.subTest(bad=bad), self.assertRaises(paper.PaperError):
                paper.write_off(self.position, T, qty_raw=bad)
        whole = paper.write_off(self.position, T)
        self.assertEqual(whole, paper.write_off(self.position, T, qty_raw=self.position.qty_raw))
        self.assertEqual(X.economic({'side': 'sell', 'sol_lamports': 0, 'fee_lamports': 0, 'realized_lamports': -5, 'cost_sold_lamports': 5},
                                    self.x.write_off_state(self.state)['execution'])['realized_lamports'], -5)


class MessageTests(unittest.TestCase):
    def test_the_sell_message_says_it_was_not_verified(self):
        event = {'side': 'sell', 'mint': 'M' * 32, 'full': False, 'reason_label': '익절 TP1', 'fraction_sold': D('0.3'),
                 'remaining_fraction': D('0.7'), 'price_sol': D(0), 'realized_sol': D('-0.003'), 'realized_pct': D('-1'),
                 'sell_unverified': True, 'unverified_sell_policy': 'zero'}
        text = N.render_fill(event, 'TKN')
        self.assertIn('⚠️ 매도 검증 안됨', text)
        self.assertIn('0 SOL로 처리', text)
        self.assertNotIn('매도 검증 안됨', N.render_fill({**event, 'sell_unverified': False}, 'TKN'))


if __name__ == '__main__':
    unittest.main()
