"""SYNTHETIC_TEST_ONLY (L20): order-flow / bot-pump features (lean/flow.py) and their use by the learner. Fixtures only, no network.

The swaps are served by tests/lean/tape.py (a constant-product pool tape in fake time, jsonParsed bodies). The collector is the REAL
``FlowCollector`` writing into a REAL lean store; the learner integration reads that store with the REAL ``lean.learn.build_dataset``
next to a REAL L07R2 path recorder file. Every value that only exists AFTER a decision is a canary (a burst of sells by fresh wallets):
none may reach a row or a learning feature."""
import json
import os
import tempfile
import threading
import unittest
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lean import flow as FL, learn as LN, strategy as S
from lean.providers import LANE_SHED, ProviderError
from lean.store import Store
from lean.wallet_signals import SignalError
from tests.lean import tape as TP
from tests.lean.test_labels import RecorderMixin

ROOT = Path(__file__).resolve().parents[2]
STRATEGY = S.StrategyConfig.load(ROOT / 'config' / 'lean' / 'strategy-default.json')
T = 1_800_000_000.0


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def token(tag):
    return SimpleNamespace(tag=tag, mint=TP.wallet(10_000 + tag), pool=TP.wallet(20_000 + tag), base_vault=TP.wallet(30_000 + tag),
                           quote_vault=TP.wallet(40_000 + tag), base_raw=2 * 10 ** 14, quote_lamports=80 * 10 ** 9)


class Now:
    def __init__(self, t=T):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class TapeHelius:
    """``Helius.rpc`` over a tape at the collector's clock; ``gate(method, n)`` may return an exception to raise (nothing sent)."""

    def __init__(self, tape, clock, gate=None):
        self.tape, self.clock, self.gate, self.n = tape, clock, gate, 0

    def rpc(self, method, params):
        self.n += 1
        if self.gate is not None:
            error = self.gate(method, self.n)
            if error is not None:
                raise error
        result = self.tape.handlers()[method](params, self.clock())
        return result, json.dumps(result).encode(), {'attempts': 1}


def view(side, *, signer='S', sol=10, spot=None, failed=False):
    return {'side': side, 'signer': signer, 'sol_lamports': sol, 'spot': None if spot is None else D(spot), 'failed': failed,
            'signer_token_delta': -1 if side == 'sell' else 1, 'signer_sol_delta': 1 if side == 'sell' else -1}


class ConfigTests(unittest.TestCase):
    def test_absent_is_off_and_defaults_are_valid(self):
        self.assertFalse(FL.config(None)['enabled'])
        self.assertEqual(FL.config({}), FL.config(None))
        cfg = FL.config({'enabled': True})
        self.assertEqual((cfg['sig_pages'], cfg['max_txs'], cfg['max_calls_per_hour']), (3, 12, 900))
        self.assertEqual(FL.config(cfg), cfg)                                   # idempotent

    def test_unknown_and_wrong_values_are_refused(self):
        for bad in ({'enable': True}, {'enabled': 1}, {'window_s': 5}, {'max_txs': 1}, {'max_txs': 2.5}, {'pace_s': 0}, {'sig_limit': 5000},
                    {'max_calls_per_hour': 0}, {'shed_wait_s': -1}, {'pass_interval_s': float('nan')}, 'x', []):
            with self.subTest(bad=bad), self.assertRaises(FL.ConfigError):
                FL.config(bad)


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.clock = Now(T + 100)
        self.tape = TP.Tape(None)
        self.tok = token(1)
        self.trades = self.tape.add(self.tok, [(T, TP.wallet(1), 'buy', 10 ** 9, False), (T + 1, TP.wallet(2), 'sell', 5 * 10 ** 8, False),
                                               (T + 2, TP.wallet(3), 'sell', 5 * 10 ** 8, True)])

    def body(self, i):
        return self.tape.transaction([TP.signature(1, i), {'encoding': 'jsonParsed', 'maxSupportedTransactionVersion': 1}], T + 100)

    def test_a_buy_and_a_sell_from_the_pool_vaults_and_the_signer(self):
        buy = FL.parse_trade(self.body(0), TP.signature(1, 0), self.tok.mint, self.tok.pool)
        self.assertEqual((buy['side'], buy['signer'], buy['sol_lamports'], buy['failed']), ('buy', TP.wallet(1), 10 ** 9, False))
        self.assertGreater(buy['signer_token_delta'], 0)
        self.assertEqual(buy['signer_sol_delta'], -10 ** 9)                     # the fee is added back: only the swap is left
        self.assertEqual(buy['spot'], D(80 * 10 ** 9 + 10 ** 9) / D(2 * 10 ** 14 - buy['token_raw']))
        sell = FL.parse_trade(self.body(1), TP.signature(1, 1), self.tok.mint, self.tok.pool)
        self.assertEqual((sell['side'], sell['signer_sol_delta']), ('sell', 5 * 10 ** 8))
        self.assertLess(sell['signer_token_delta'], 0)
        self.assertTrue(FL.is_organic_sell(sell))
        self.assertFalse(FL.is_organic_sell(sell, exclude={TP.wallet(2)}))      # the creator / the taker never count
        self.assertFalse(FL.is_organic_sell(buy))

    def test_only_a_sell_into_the_pool_with_proceeds_to_the_signer_is_organic(self):
        sell = FL.parse_trade(self.body(1), TP.signature(1, 1), self.tok.mint, self.tok.pool)
        self.assertFalse(FL.is_organic_sell({**sell, 'signer_sol_delta': 0}))           # the proceeds went elsewhere
        self.assertFalse(FL.is_organic_sell({**sell, 'side': 'buy'}))
        self.assertFalse(FL.is_organic_sell({**sell, 'side': None}))                    # not a swap of this pool (an OTC transfer)
        body = self.body(1)
        body['meta']['postBalances'][0] = body['meta']['preBalances'][0] - TP.FEE       # tokens out, no SOL in
        self.assertFalse(FL.is_organic_sell(FL.parse_trade(body, 's', self.tok.mint, self.tok.pool)))

    def test_a_failed_sell_is_never_a_sell(self):
        failed = FL.parse_trade(self.body(2), TP.signature(1, 2), self.tok.mint, self.tok.pool)
        self.assertTrue(failed['failed'])
        self.assertIsNone(failed['side'])
        self.assertFalse(FL.is_organic_sell(failed))

    def test_a_wsol_payout_counts_and_a_transfer_is_not_a_swap(self):
        body = self.body(1)
        body['meta']['postBalances'][0] = body['meta']['preBalances'][0] - TP.FEE        # the proceeds stay as WSOL
        body['meta']['preTokenBalances'].append({'accountIndex': 5, 'mint': TP.SOL, 'owner': TP.wallet(2), 'uiTokenAmount': {'amount': '0'}})
        body['meta']['postTokenBalances'].append({'accountIndex': 5, 'mint': TP.SOL, 'owner': TP.wallet(2), 'uiTokenAmount': {'amount': str(5 * 10 ** 8)}})
        self.assertTrue(FL.is_organic_sell(FL.parse_trade(body, 's', self.tok.mint, self.tok.pool)))
        transfer = self.body(1)
        for side in ('preTokenBalances', 'postTokenBalances'):                 # the pool's vaults do not move: not a swap of this pool
            transfer['meta'][side] = [b for b in transfer['meta'][side] if b['owner'] != self.tok.pool]
        self.assertIsNone(FL.parse_trade(transfer, 's', self.tok.mint, self.tok.pool)['side'])

    def test_malformed_bodies_raise_and_a_missing_one_is_none(self):
        self.assertIsNone(FL.parse_trade(None, 's', self.tok.mint, self.tok.pool))
        good = self.body(0)
        for bad in ({}, {**good, 'slot': '1'}, {**good, 'meta': None}, {**good, 'transaction': {}}, {**good, 'blockTime': 'x'},
                    {**good, 'meta': {**good['meta'], 'preBalances': [1, -1]}},
                    {**good, 'meta': {**good['meta'], 'postTokenBalances': [{'accountIndex': 2, 'mint': self.tok.mint, 'owner': 'bad', 'uiTokenAmount': {'amount': '1'}}]}}):
            with self.subTest(bad=str(bad)[:60]), self.assertRaises(SignalError):
                FL.parse_trade(bad, 's', self.tok.mint, self.tok.pool)


class FigureTests(unittest.TestCase):
    def test_a_staircase_of_identical_buys(self):
        out, missing = FL.figures([view('buy', signer='B%d' % (i % 2), sol=500, spot=1 + i) for i in range(10)])
        self.assertEqual((out['monotonic_up_frac'], out['trade_size_cv'], out['max_drawdown_pre']), (1.0, 0.0, 0.0))
        self.assertEqual((out['buys'], out['sells'], out['buy_sell_ratio'], out['unique_buyers'], out['unique_sellers']), (10, 0, 10.0, 2, 0))
        self.assertEqual(missing, {})
        self.assertEqual((out['txs_per_unique_wallet'], out['top5_signer_tx_share'], out['longest_up_streak']), (5.0, 1.0, 9))
        self.assertEqual(out['max_runup_pct'], 900.0)                          # 1 -> 10

    def test_wash_trading_and_path_shape_by_hand(self):
        prices = [D(1), D(2), D(4), D(2), D(4)]
        signers = ['A', 'A', 'B', 'A', 'C']
        sides = ['buy', 'sell', 'buy', 'buy', 'buy']
        out, _ = FL.figures([view(side, signer=w, sol=10, spot=p) for side, w, p in zip(sides, signers, prices)])
        self.assertEqual((out['txs_per_unique_wallet'], out['roundtrip_wallets'], out['roundtrip_share']), (round(5 / 3, 4), 1, round(1 / 3, 4)))
        self.assertEqual(out['top5_signer_tx_share'], 1.0)
        self.assertEqual((out['longest_up_streak'], out['max_runup_pct'], out['max_drawdown_pre']), (2, 300.0, 0.5))
        import math
        logs = [math.log(2), math.log(2), math.log(0.5), math.log(2)]
        mean = sum(logs) / 4
        self.assertEqual(out['return_vol'], round(math.sqrt(sum((x - mean) ** 2 for x in logs) / 4), 8))
        six = [view('buy', signer='W%d' % (i % 6), sol=10, spot=1) for i in range(12)]
        self.assertEqual(FL.figures(six)[0]['top5_signer_tx_share'], round(10 / 12, 4))

    def test_the_runup_is_from_the_running_low(self):
        out, _ = FL.figures([view('buy', sol=1, spot=p) for p in (4, 2, 3)])
        self.assertEqual((out['max_runup_pct'], out['max_drawdown_pre']), (50.0, 0.5))

    def test_a_choppy_tape(self):
        prices = [10, 12, 9, 11, 8, 12]
        sizes = [100, 300, 50, 700, 120, 400]
        trades = [view('buy' if i % 2 == 0 else 'sell', signer='W%d' % i, sol=s, spot=p) for i, (p, s) in enumerate(zip(prices, sizes))]
        out, _ = FL.figures(trades)
        self.assertEqual(out['monotonic_up_frac'], 0.6)                         # 3 up of 5 moves
        self.assertEqual(out['max_drawdown_pre'], float(round(D(4) / 12, 6)))   # 12 -> 8
        mean = sum(sizes) / 6
        sd = (sum((x - mean) ** 2 for x in sizes) / 6) ** 0.5
        self.assertEqual(out['trade_size_cv'], round(sd / mean, 4))
        self.assertEqual((out['buy_sell_ratio'], out['unique_sellers']), (1.0, 3))

    def test_nothing_to_measure_is_null_with_a_reason(self):
        out, missing = FL.figures([])
        self.assertEqual({out[n] for n in FL.SAMPLE_FIELDS}, {None})
        self.assertEqual(set(missing.values()), {'NO_SWAPS_IN_SAMPLE'})
        out, missing = FL.figures([view('buy', sol=5, spot=3)])
        self.assertEqual((missing['trade_size_cv'], missing['monotonic_up_frac']), ('FEWER_THAN_2_SWAPS', 'FEWER_THAN_2_PRICES'))
        self.assertEqual(out['max_drawdown_pre'], 0.0)
        out, missing = FL.figures([view('buy', sol=5, spot=3), view('buy', sol=5, spot=3)])
        self.assertEqual(missing['monotonic_up_frac'], 'NO_PRICE_CHANGE')

    def test_the_sample_covers_every_stratum_in_order_and_never_aliases_a_periodic_pattern(self):
        rows = [{'signature': TP.signature(1, i), 'side': ('buy', 'sell', 'sell')[i % 3]} for i in range(46)]
        self.assertEqual(FL.stratified(rows[:5], 8), rows[:5])
        picked = FL.stratified(rows, 4)
        idx = [rows.index(r) for r in picked]
        self.assertEqual(idx, sorted(idx))
        self.assertEqual([i * 46 // 4 <= x < (i + 1) * 46 // 4 for i, x in enumerate(idx)], [True] * 4)
        self.assertEqual(picked, FL.stratified(rows, 4))                              # deterministic
        aliased = [rows[round(i * 45 / 3)]['side'] for i in range(4)]                  # an even spread sees only the buys
        self.assertEqual(set(aliased), {'buy'})
        self.assertIn('sell', {r['side'] for r in picked})


class PlumbingTests(unittest.TestCase):
    def test_the_hour_budget_slides_and_refunds(self):
        clock = Now()
        budget = FL.HourBudget(2, clock)
        self.assertTrue(budget.take() and budget.take())
        self.assertFalse(budget.take())
        budget.refund()
        self.assertTrue(budget.take())
        clock.t += 3601
        self.assertEqual(budget.used(), 0)

    def test_low_calls_refund_a_shed_pace_no_token_and_stop_on_a_window(self):
        budget, waits = FL.HourBudget(10, Now()), []
        sheds = iter([ProviderError(LANE_SHED, True, meta={'why': 'no_token'}), None])

        class H:
            def rpc(self, method, params):
                error = next(sheds)
                if error:
                    raise error
                return 'ok', b'', {}
        calls = FL.LowCalls(H(), limit=3, budget=budget, wait=waits.append, pace_s=0.5, patience_s=2)
        self.assertEqual(calls.rpc('m', []), 'ok')
        self.assertEqual((calls.used, budget.used(), waits), (1, 1, [0.5]))     # the shed sent nothing: refunded, not counted

        class Window:
            def rpc(self, method, params):
                raise ProviderError(LANE_SHED, True, meta={'why': 'backoff'})
        calls = FL.LowCalls(Window(), limit=3, budget=budget, wait=waits.append, pace_s=0.5, patience_s=2)
        with self.assertRaises(FL.Stop) as stop:
            calls.rpc('m', [])
        self.assertEqual((stop.exception.code, calls.used, len(waits)), (LANE_SHED, 0, 1))
        calls = FL.LowCalls(H(), limit=0, budget=budget, wait=waits.append, pace_s=0.5, patience_s=2)
        with self.assertRaises(FL.Stop) as stop:
            calls.rpc('m', [])
        self.assertEqual(stop.exception.code, 'CALL_CAP')
        event = threading.Event()
        event.set()
        with self.assertRaises(FL.Stop) as stop:
            FL.LowCalls(H(), limit=3, budget=budget, wait=waits.append, pace_s=0.5, patience_s=2, stop=event).rpc('m', [])
        self.assertEqual(stop.exception.code, 'STOPPED')


class CollectorBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(os.path.realpath(tmp.name))
        self.clock = Now()
        self.store = Store(self.dir / 'lean.sqlite', initial_cash_sol='10', code_version='t', strategy_version='s', clock=self.clock)
        self.addCleanup(self.store.close)
        self.tape = TP.Tape(None)

    def collector(self, gate=None, **cfg):
        self.helius = TapeHelius(self.tape, self.clock, gate)
        return FL.FlowCollector(store=self.store, helius=self.helius, cfg=FL.config({'enabled': True, **cfg}), code_version='t',
                                strategy_version='s', clock=self.clock, sleep=self.clock.sleep)

    def decision(self, tok, at, *, migrated_at=None, action='PASS'):
        cid = self.store.candidate_id(tok.mint) or self.store.add_candidate(tok.mint, pool=tok.pool, slot=1,
                                                                            migrated_at=migrated_at if migrated_at is not None else at - 600, ts=at - 1)
        self.store.add_decision('screen', action, mint=tok.mint, candidate_id=cid, reasons=[], features={}, ts=at)
        return cid

    def rows(self):
        return [json.loads(r['raw']) for r in self.store.rows('observations', kind='flow', limit=1000)]

    def fetched_times(self, pool):
        return [self.tape.pools[pool]['trades'][self.tape.by_sig[s][1]][0] for m, s, _ in self.tape.calls if m == 'getTransaction'
                and self.tape.by_sig[s][0] == pool]


class CollectorTests(CollectorBase):
    def test_a_bot_pump_and_an_organic_pool_and_nothing_after_the_decision(self):
        bot, org = token(1), token(2)
        d = T + 1000.5
        self.tape.add(bot, TP.bot_pump(T + 600, d + 40) + [(d + 5 + i, TP.wallet(9000 + i), 'sell', 3 * 10 ** 9, False) for i in range(20)])
        self.tape.add(org, TP.organic(T + 600, d + 40, seed=3) + [(d + 5 + i, TP.wallet(9100 + i), 'sell', 3 * 10 ** 9, False) for i in range(20)])
        self.decision(bot, d)
        self.decision(org, d)
        self.clock.t = d + 30                                                    # collected 30 s late: the canaries are on chain by now
        c = self.collector()
        self.assertEqual(c.flow_pass(), 2)
        rows = {r['mint']: r for r in self.rows()}
        b, o = rows[bot.mint], rows[org.mint]
        # the staircase: ~5 tx/s, identical sizes, every sampled move up, never a pullback; the canary sells are invisible
        self.assertEqual((b['version'], b['decision_ts'], b['window_after_decision_s'], b['late_s']), ('flow_v1', d, 0.0, 30.0))
        self.assertEqual((b['window_end'], b['covered_from'], b['window_txs']), (T + 1000, T + 821, 179 * 5))   # whole seconds before d
        self.assertEqual(b['tx_rate_per_s'], 5.0)
        self.assertEqual((b['trade_size_cv'], b['monotonic_up_frac'], b['max_drawdown_pre']), (0.0, 1.0, 0.0))
        self.assertEqual((b['unique_sellers'], b['sells'], b['buys']), (0, 0, 12))
        # wash trading (#1): three bot wallets churn every swap; path shape (#7): one smooth unbroken climb
        self.assertGreaterEqual(b['txs_per_unique_wallet'], 4.0)
        self.assertEqual((b['top5_signer_tx_share'], b['roundtrip_wallets'], b['roundtrip_share']), (1.0, 0, 0.0))
        self.assertEqual(b['longest_up_streak'], 11)
        self.assertGreater(b['max_runup_pct'], 50)                              # +77 % over the window, never a down-move
        self.assertEqual(b['flags']['trade_size_cv'], 'SAMPLE')
        self.assertFalse(b['partial'], b['unavailable'])
        # organic: slow, uneven sizes, a mixed tape
        self.assertLess(o['tx_rate_per_s'], 0.3)
        self.assertGreater(o['trade_size_cv'], 0.3)
        self.assertLess(o['monotonic_up_frac'], 0.9)
        self.assertGreater(o['unique_sellers'], 0)
        self.assertLess(o['txs_per_unique_wallet'], 1.5)
        self.assertLess(o['top5_signer_tx_share'], 0.7)
        self.assertLess(o['longest_up_streak'], 11)
        self.assertGreater(o['return_vol'], 5 * b['return_vol'])               # the staircase's returns barely vary
        # nothing after the decision was ever fetched (a whole block-time second before it)
        for pool in (bot.pool, org.pool):
            times = self.fetched_times(pool)
            self.assertTrue(times)
            self.assertTrue(all(int(t) + 1 <= d for t in times), pool)
        # bounded: pages + samples, the hourly counter agrees with the rows
        self.assertTrue(all(r['calls'] <= 3 + 12 for r in rows.values()))
        self.assertEqual(c.budget.used(), sum(r['calls'] for r in rows.values()))
        self.assertEqual(json.loads(self.store.rows('observations', kind='flow', limit=1)[0]['meta'])['version'], 'flow_v1')

    def test_the_same_tape_without_the_post_decision_burst_gives_the_same_row(self):
        d = T + 1000.5
        rows = []
        for burst in (False, True):
            self.setUp()
            bot = token(1)
            extra = [(d + 0.6 + i * 0.01, TP.wallet(9000 + i), 'buy', 9 * 10 ** 9, False) for i in range(50)] if burst else []
            self.tape.add(bot, TP.bot_pump(T + 600, d + 1) + extra)
            self.decision(bot, d)
            self.clock.t = d + 10
            self.collector().flow_pass()
            row = self.rows()[0]
            rows.append({k: row[k] for k in FL.FEATURES})
        self.assertEqual(rows[0], rows[1])

    def test_truncated_pages_flag_the_rate_and_a_decision_never_reached_is_unavailable(self):
        bot = token(1)
        d = T + 1000.5
        self.tape.add(bot, TP.bot_pump(T + 600, d + 200))
        self.decision(bot, d)
        self.clock.t = d + 100
        self.collector(sig_limit=200, sig_pages=3).flow_pass()                  # 600 signatures: 500 after the decision, 100 before
        row = self.rows()[0]
        self.assertEqual(row['flags']['tx_rate_per_s'], 'TRUNCATED_SPAN')
        self.assertEqual(row['tx_rate_per_s'], 5.0)                              # over the covered span only
        self.setUp()
        self.tape.add(bot, TP.bot_pump(T + 600, d + 200))
        self.decision(bot, d)
        self.clock.t = d + 150
        self.collector(sig_limit=200, sig_pages=3).flow_pass()                  # 750 after the decision: never reached
        row = self.rows()[0]
        self.assertEqual(row['unavailable']['window'], 'DECISION_NOT_REACHED')
        self.assertEqual({row[n] for n in FL.FEATURES}, {None})
        self.assertEqual(row['calls'], 3)

    def test_a_shed_first_call_is_retried_later_and_retries_end_in_a_closed_row(self):
        bot = token(1)
        d = T + 1000.5
        self.tape.add(bot, TP.bot_pump(T + 600, d))
        self.decision(bot, d)
        self.clock.t = d + 5
        window = ProviderError(LANE_SHED, True, meta={'why': 'backoff'})
        c = self.collector(gate=lambda m, n: window if n == 1 else None, retry_delay_s=30)
        c.flow_pass()
        self.assertEqual(self.rows(), [])                                        # nothing learned: no row now
        c.flow_pass()                                                            # not before retry_delay_s
        self.assertEqual(self.rows(), [])
        self.clock.t += 31
        c.flow_pass()
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse(self.rows()[0]['partial'])
        self.setUp()
        self.tape.add(bot, TP.bot_pump(T + 600, d))
        self.decision(bot, d)
        self.clock.t = d + 5
        c = self.collector(gate=lambda m, n: ProviderError('HTTP_503', True), max_retries=1, retry_delay_s=0)
        c.flow_pass()
        c.flow_pass()
        row = self.rows()[0]
        self.assertEqual(row['unavailable']['window'], 'RETRIES_EXHAUSTED:HTTP_503')

    def test_no_token_is_paced_and_the_hour_cap_holds(self):
        bot = token(1)
        d = T + 1000.5
        self.tape.add(bot, TP.bot_pump(T + 600, d))
        self.decision(bot, d)
        self.clock.t = d + 5
        no_token = ProviderError(LANE_SHED, True, meta={'why': 'no_token'})
        c = self.collector(gate=lambda m, n: no_token if n in (2, 3) else None)
        c.flow_pass()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.clock.t, d + 5 + 1.0)                             # two paced waits of 0.5 s on the collector's own clock
        self.setUp()
        self.tape.add(bot, TP.bot_pump(T + 600, d))
        self.decision(bot, d)
        self.clock.t = d + 5
        c = self.collector(max_calls_per_hour=5)
        c.flow_pass()
        row = self.rows()[0]
        self.assertEqual((row['calls'], row['unavailable']['stopped']), (5, 'HOURLY_BUDGET'))
        self.assertEqual(c.budget.used(), 5)

    def test_one_row_per_pass_decision_across_a_restart_and_a_stop_writes_nothing(self):
        a, b = token(1), token(2)
        d = T + 1000.5
        self.tape.add(a, TP.bot_pump(T + 600, d))
        self.tape.add(b, TP.organic(T + 600, d))
        self.decision(a, d)
        self.decision(b, d + 1, action='REJECT')                                # only PASS decisions are collected
        self.clock.t = d + 5
        stop = threading.Event()
        stop.set()
        c = self.collector()
        c.stop = stop
        c.flow_pass()
        self.assertEqual(self.rows(), [])
        c = self.collector()
        c.flow_pass()
        self.decision(a, d + 60)                                                 # a re-screen PASS: its own row
        self.clock.t = d + 70
        self.collector().flow_pass()                                             # a new process: the first decision is not redone
        rows = self.rows()
        self.assertEqual([(r['mint'], r['decision_ts']) for r in rows], [(a.mint, d), (a.mint, d + 60)])

    def test_a_body_that_disagrees_with_its_signature_time_is_dropped(self):
        bot = token(1)
        d = T + 1000.5
        self.tape.add(bot, TP.bot_pump(T + 600, d))
        self.decision(bot, d)
        self.clock.t = d + 5
        c = self.collector()
        plain = self.tape.transaction
        self.tape.transaction = lambda params, t: {**plain(params, t), 'blockTime': int(d) + 3}
        c.flow_pass()
        row = self.rows()[0]
        self.assertEqual((row['trades'], row['sample_skipped'], row['unavailable']['trade_size_cv']), (0, 12, 'NO_SWAPS_IN_SAMPLE'))

    def test_a_periodic_bot_does_not_alias_the_sample(self):
        tok = token(1)
        d = T + 1000.5
        trades = []
        for i in range(12):                                                              # buy, sell, sell every 3 s, distinct sellers
            t = T + 1000 - 36 + 3 * i
            trades += [(t, TP.wallet(600 + i), 'buy', 10 ** 8, False), (t + 1, TP.wallet(700 + i), 'sell', 10 ** 8, False),
                       (t + 2, TP.wallet(800 + i), 'sell', 10 ** 8, False)]
        self.tape.add(tok, trades)
        self.decision(tok, d, migrated_at=T + 1000 - 36)
        self.clock.t = d + 5
        self.collector().flow_pass()
        row = self.rows()[0]
        self.assertEqual(row['window_txs'], 36)                                          # an even spread (every 3rd) sees only buys
        self.assertGreater(row['sells'], 0)
        self.assertGreater(row['unique_sellers'], 0)

    def test_the_window_is_clipped_at_graduation(self):
        bot = token(1)
        d = T + 1000.5
        self.tape.add(bot, TP.bot_pump(d - 60, d))
        self.decision(bot, d, migrated_at=d - 60)
        self.clock.t = d + 5
        self.collector().flow_pass()
        row = self.rows()[0]
        self.assertEqual((row['window_start'], row['window_s_effective'], row['window_clipped_at_graduation']), (d - 60, 59.0, True))
        self.assertEqual(row['tx_rate_per_s'], 5.0)


class LearnerIntegrationTests(RecorderMixin, unittest.TestCase):
    """The REAL writers: Store decisions, the REAL FlowCollector on a tape, the REAL L07R2 path recorder; the learner reads both files
    mode=ro. The canary (a post-decision burst of sells by fresh wallets) must never reach a learning row."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.setup_recorder(os.path.realpath(tmp.name))            # self.store = the L07R2 PathStore, self.clock its clock
        self.lean = Store(self.dir / 'lean.sqlite', initial_cash_sol='10', code_version='t', strategy_version='lean-1', clock=self.clock.now)
        self.addCleanup(self.lean.close)
        self.tape = TP.Tape(None)

    def candidate(self, i, *, late_row=False, tamper=None):
        tok = token(50 + i)
        decision = self.clock.now()
        cid = self.lean.add_candidate(tok.mint, pool=tok.pool, slot=1, migrated_at=decision - 600, ts=decision - 1)
        self.lean.add_decision('screen', 'PASS', mint=tok.mint, candidate_id=cid, reasons=[], features={}, ts=decision)
        self.lean.add_decision('entry', 'SKIP', mint=tok.mint, candidate_id=cid, reasons=['MAX_POSITIONS'], ts=decision)
        self.tape.add(tok, TP.bot_pump(decision - 400, decision) + [(decision + 2 + k, TP.wallet(9500 + k), 'sell', 4 * 10 ** 9, False)
                                                                     for k in range(10)])
        flow_clock = Now(decision + 20)
        c = FL.FlowCollector(store=self.lean, helius=TapeHelius(self.tape, flow_clock), cfg=FL.config({'enabled': True}), code_version='t',
                             strategy_version='lean-1', clock=flow_clock, sleep=flow_clock.sleep)
        c.collect(1, decision, tok.mint, cid, tok.pool, decision - 600)
        if late_row:                                    # a re-screen 60 s later wrote another row (its decision is after this one's)
            c.collect(2, decision + 60, tok.mint, cid, tok.pool, decision - 600)
        if tamper is not None:
            row = json.loads(self.lean.rows('observations', kind='flow', mint=tok.mint, limit=10)[-1]['raw'])
            row.update(tamper)
            self.lean.add_observation('flow', json.dumps(row).encode(), mint=tok.mint, candidate_id=cid, meta={}, ts=decision)
        self.start(cid=cid, mint=tok.mint)
        self.walk(tok.mint, [1, 1.5])
        self.clock.t += 300
        return tok.mint

    def build(self):
        self.lean.close()
        self.rec.db.close()
        return LN.build_dataset(self.dir / 'lean.sqlite', self.dir / 'paths.sqlite', STRATEGY, pool_fee_bps=25)

    def test_flow_features_reach_the_learner_as_of_the_decision_only(self):
        plain = self.candidate(0)
        later = self.candidate(1, late_row=True)
        uncut = self.candidate(2, tamper={'window_after_decision_s': 12.0, 'tx_rate_per_s': 666})
        truncated = self.candidate(3, tamper={'flags': {'tx_rate_per_s': 'TRUNCATED_SPAN'}})
        old = self.candidate(4, tamper={'version': 'flow_v0'})
        ds = self.build()
        rows = {r.mint: r for r in ds.rows}
        self.assertEqual(set(LN.FLOW_FEATURES) <= set(ds.stats['feature_names']), True)
        for mint in (plain, later):
            f = rows[mint].features
            self.assertEqual((f['tx_rate_per_s'], f['trade_size_cv'], f['monotonic_up_frac'], f['unique_sellers']), (D(5), D(0), D(1), D(0)))
        self.assertEqual({rows[uncut].features[n] for n in LN.FLOW_FEATURES}, {None})
        self.assertIsNone(rows[truncated].features['tx_rate_per_s'])
        self.assertEqual(rows[truncated].features['monotonic_up_frac'], D(1))
        self.assertEqual({rows[old].features[n] for n in LN.FLOW_FEATURES}, {None})
        self.assertEqual(ds.stats['skipped'].get('FLOW_AFTER_DECISION'), 1)
        self.assertEqual(ds.stats['skipped'].get('FLOW_OLD_VERSION'), 1)
        self.assertEqual(ds.stats['excluded']['tx_rate_per_s'], {'WINDOW_NOT_CUT_AT_DECISION': 1, 'FLAG_TRUNCATED_SPAN': 1})
        for row in ds.rows:
            self.assertNotIn(D(666), row.features.values())

    def test_a_rule_on_a_flow_feature_asks_the_runner_to_wait_for_it(self):
        rule = SimpleNamespace(conditions=[LN.Condition('monotonic_up_frac', '>', D('0.9'))])
        self.assertEqual(LN.runner_changes([rule]), ['selection_rules', 'wait_for_flow:monotonic_up_frac'])


if __name__ == '__main__':
    unittest.main()
