"""SYNTHETIC_TEST_ONLY: offline replay of the lean strategy on recorded price paths (lean/replay.py). Fixtures only, no network.

Paths are synthetic: price = 1e-6 SOL/token times a ratio list sampled every 15 s, with a 100 SOL pool. Stores are real
lean.store.Store files so the loader is exercised on the actual schema."""
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from lean import paper, replay as R, strategy as S
from lean.store import Store

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / 'config' / 'lean' / 'strategy-default.json'
DAY0 = 19675 * 86400  # a UTC midnight
T0 = DAY0 + 3600  # one hour into the day: tests spanning a few hours stay inside one UTC day unless they say otherwise
BASE_PRICE = D('0.000001')
FEATS = {'market_cap_usd': '100000', 'liquidity_usd': '20000', 'reserve_sol': '100'}
CV, SV = 'testcode', 'lean-1'


def cfg(**over):
    raw = json.loads(DEFAULT.read_text())
    raw.update(over)
    return S.StrategyConfig.from_dict(raw)


def points(start, ratios, dt=15, liq=100):
    return tuple(R.Point(start + dt * k, BASE_PRICE * D(str(r)), None if liq is None else D(liq)) for k, r in enumerate(ratios))


def path(mint, start, ratios, feats=None, **kw):
    return R.Path(mint, start, dict(FEATS if feats is None else feats), points(start, ratios, **{k: kw.pop(k) for k in ('dt', 'liq') if k in kw}), **kw)


def oracle_buy_tokens(lamports=100_000_000, price=BASE_PRICE, liq=D(100), fee_bps=30):
    eff = D(lamports) / 10**9 * (1 - D(fee_bps) / 10000)
    return int(((liq / price) * eff / (liq + eff) * 10**6).to_integral_value(rounding='ROUND_DOWN'))


def oracle_sell_lamports(qty_raw, ratio, liq=D(100), fee_bps=30):
    price = BASE_PRICE * ratio
    eff = D(qty_raw) / 10**6 * (1 - D(fee_bps) / 10000)
    return int((liq * eff / ((liq / price) + eff) * 10**9).to_integral_value(rounding='ROUND_DOWN'))


def oracle_net_ratio(ratio):
    held = oracle_buy_tokens() * 9950 // 10000
    net = oracle_sell_lamports(held, ratio) * 9950 // 10000 - 50_000
    return D(net) / D(100_050_000)


RALLY_THEN_FADE = [1, 1, 1.05, 1.1, 1.5, 2.1, 3.2, 3.0, 2.0, 1.5]
CRASH = [1, 1, 0.9, 0.7, 0.6]


def trade_of(result, mint):
    return next(t for t in result.trades if t.mint == mint)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))


class ReplayTests(unittest.TestCase):
    c = cfg()

    def test_entry_and_exit_fills_match_an_independent_pool_oracle(self):
        r = R.replay([path('M', T0, CRASH)], self.c)
        buy_fill, sell_fill = r.fills
        # oracle: constant product, 30 bps pool fee, then paper's 50 bps haircut; size 2% of 5 SOL = 0.1 SOL, fee 50_000 lamports
        held = oracle_buy_tokens() * 9950 // 10000
        self.assertEqual((buy_fill.side, buy_fill.sol_lamports, buy_fill.fee_lamports, buy_fill.qty_raw), ('buy', 100_000_000, 50_000, held))
        # the stop fires at the 0.7 mark (the 0.9 mark is still above the 0.82 line): sell everything at that price
        self.assertEqual((sell_fill.side, sell_fill.qty_raw), ('sell', held))
        self.assertEqual(sell_fill.sol_lamports, oracle_sell_lamports(held, D('0.7')) * 9950 // 10000)
        self.assertEqual(sell_fill.realized_lamports, sell_fill.sol_lamports - 50_000 - (100_000_000 + 50_000))
        self.assertEqual(trade_of(r, 'M').realized_lamports, sell_fill.realized_lamports)
        self.assertTrue(all(f.label == 'EXECUTION_UNVERIFIED' for f in r.fills))

    def test_marks_use_slippage_and_fee_at_the_stop_boundary(self):
        # choose a price at which the NET mark ratio is 0.8199 (just inside the 0.82 stop). Without the 50 bps haircut or the
        # exit fee in the mark the ratio would sit above 0.82 and the position would be held.
        lo, hi = D('0.5'), D(1)
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if oracle_net_ratio(mid) < D('0.8199') else (lo, mid)
        stop_price = (lo + hi) / 2
        r = R.replay([path('M', T0, [1, 1, stop_price])], self.c)
        self.assertEqual(trade_of(r, 'M').exit_reason, 'STOP')
        above = R.replay([path('M', T0, [1, 1, stop_price * D('1.002')])], self.c)  # 0.2% higher price: ratio ~0.8215 -> hold
        self.assertEqual(trade_of(above, 'M').status, 'OPEN_AT_END')

    def test_open_at_end_value_is_cash_change_plus_remaining_mark(self):
        r = R.replay([path('M', T0, [1, 1, 1.5, 1.5, 1.5])], self.c)  # one TAKE_PROFIT, then held
        t = trade_of(r, 'M')
        self.assertEqual((t.status, t.reasons), ('OPEN_AT_END', ['TAKE_PROFIT']))
        self.assertEqual(t.realized_lamports, (r.final_cash_lamports - 5 * paper.LAMPORTS) + t.mark_lamports)

    def test_entries_are_recorded_without_skips(self):
        r = R.replay([path('M', T0, [1, 1, 1])], self.c)
        self.assertEqual((trade_of(r, 'M').status, trade_of(r, 'M').cost_lamports, r.skipped_entries), ('OPEN_AT_END', 100_050_000, []))

    def test_ladder_then_stop_sequence_and_accounting_identity(self):
        r = R.replay([path('M', T0, RALLY_THEN_FADE)], self.c)
        t = trade_of(r, 'M')
        self.assertEqual(t.reasons, ['TAKE_PROFIT', 'TAKE_PROFIT', 'TAKE_PROFIT', 'STOP'])  # last rung moves the stop to 2.0x cost
        self.assertEqual((t.status, t.exit_reason, t.fills), ('CLOSED', 'STOP', 5))
        self.assertGreater(t.realized_lamports, 0)
        self.assertEqual(r.final_cash_lamports - 5 * paper.LAMPORTS, t.realized_lamports)  # cash moved by exactly the realized pnl
        self.assertEqual(r.anomalies, {})

    def test_stop_loss_sets_cooldown_and_negative_pnl(self):
        r = R.replay([path('M', T0, CRASH)], self.c)
        t = trade_of(r, 'M')
        self.assertEqual((t.exit_reason, t.reasons), ('STOP', ['STOP']))
        self.assertLess(t.realized_lamports, 0)
        self.assertEqual(R.metrics(r.trades, D(5))['win_rate'], 0.0)

    def test_time_stop_fires_at_2700s_not_before(self):
        flat = [1.0] * 200  # 15 s apart -> 2985 s
        r = R.replay([path('M', T0, flat)], cfg(stop_fraction='0.5'))
        t = trade_of(r, 'M')
        self.assertEqual(t.exit_reason, 'TIME_STOP')
        self.assertEqual(t.exit_ts - t.entry_ts, 2700)

    def test_max_hold_after_touching_115(self):
        ratios = [1.0, 1.3] + [1.25] * 1500  # touches +15% (cost basis) so the time stop is disarmed; never reaches 1.4
        r = R.replay([path('M', T0, ratios, dt=15)], cfg(stop_fraction='0.5', max_hold_seconds=3600))
        t = trade_of(r, 'M')
        self.assertEqual(t.exit_reason, 'MAX_HOLD')
        self.assertEqual(t.exit_ts - t.entry_ts, 3600)

    def test_open_at_end_is_censored_not_counted(self):
        r = R.replay([path('M', T0, [1, 1, 1.02])], self.c)
        self.assertEqual([t.status for t in r.trades], ['OPEN_AT_END'])
        m = R.metrics(r.trades, D(5))
        self.assertEqual((m['n_trades'], m['profit_factor_note']), (0, 'NO_TRADES'))

    def test_no_lookahead_future_marks_cannot_change_past_decisions(self):
        ratios = list(RALLY_THEN_FADE)
        a = trade_of(R.replay([path('M', T0, ratios)], self.c), 'M')
        # same path with a different (crashing / pumping) future after the exit point: the trade must be identical
        for tail in ([0.01] * 5, [50.0] * 5):
            b = trade_of(R.replay([path('M', T0, ratios + tail)], self.c), 'M')
            self.assertEqual((a.reasons, a.realized_lamports, a.exit_ts), (b.reasons, b.realized_lamports, b.exit_ts))
        # and truncating the path just after the stop gives the same trade
        cut = trade_of(R.replay([path('M', T0, ratios[:9])], self.c), 'M')
        self.assertEqual((a.reasons, a.realized_lamports), (cut.reasons, cut.realized_lamports))

    def test_deterministic_and_input_not_mutated(self):
        paths = [path('A', T0, RALLY_THEN_FADE), path('B', T0 + 120, CRASH)]
        one, two = R.replay(paths, self.c), R.replay(paths, self.c)
        self.assertEqual([t.to_dict() for t in one.trades], [t.to_dict() for t in two.trades])
        self.assertEqual(one.final_cash_lamports, two.final_cash_lamports)

    def test_entry_filters_reject_with_strategy_reasons(self):
        bad = [('MARKET_CAP', dict(FEATS, market_cap_usd='1000')), ('LIQUIDITY', dict(FEATS, liquidity_usd='10')),
               ('MARKET_CAP_UNKNOWN', {k: v for k, v in FEATS.items() if k != 'market_cap_usd'}),
               ('LIQUIDITY_UNKNOWN', dict(FEATS, liquidity_usd='NaN'))]
        for reason, feats in bad:
            r = R.replay([path('M', T0, CRASH, feats=feats)], self.c)
            self.assertEqual(r.trades, [], reason)
            self.assertEqual(r.skipped_entries[0]['reasons'], [reason])

    def test_cost_budget_blocks_thin_pools(self):
        r = R.replay([path('M', T0, [1, 1, 1], liq=2)], self.c)  # 0.1 SOL into a 2 SOL pool: ~5% impact each way
        self.assertEqual(r.trades, [])
        self.assertEqual(r.skipped_entries[0]['reasons'], ['COST_BUDGET'])

    def test_unknown_liquidity_means_no_price_impact(self):
        thin = R.replay([path('M', T0, CRASH, liq=None)], self.c)
        self.assertEqual(trade_of(thin, 'M').exit_reason, 'STOP')

    def test_entry_delay_uses_a_later_mark(self):
        ratios = [1, 1, 1, 0.4, 0.4]
        prompt = R.replay([path('M', T0, ratios)], self.c)
        late = R.replay([path('M', T0, ratios)], self.c, R.ReplayConfig(entry_delay_s=45))
        self.assertEqual(trade_of(prompt, 'M').entry_ts, T0)
        self.assertEqual(trade_of(late, 'M').entry_ts, T0 + 45)

    def test_candidate_is_considered_once_even_if_blocked(self):
        # five concurrent candidates, one per minute: the 5th meets MAX_POSITIONS and is dropped, never retried later
        paths = [path('M%d' % i, T0 + 60 * i, [1.0] * 40) for i in range(5)]
        r = R.replay(paths, cfg(stop_fraction='0.9', time_stop_seconds=100000))
        self.assertEqual(sorted(t.mint for t in r.trades), ['M0', 'M1', 'M2', 'M3'])
        self.assertEqual([(s['mint'], s['reasons']) for s in r.skipped_entries], [('M4', ['MAX_POSITIONS'])])

    def test_entry_throttle_one_per_minute(self):
        r = R.replay([path('A', T0, [1.0] * 6), path('B', T0 + 15, [1.0] * 6)], self.c)
        self.assertEqual([t.mint for t in r.trades], ['A'])
        self.assertEqual(r.skipped_entries[0]['reasons'], ['ENTRY_THROTTLE'])

    def test_daily_loss_stop_blocks_later_entries_and_resets_next_utc_day(self):
        c = cfg(daily_loss_stop_fraction='0.01', loss_streak_pause_at=100)  # 1% of 5 SOL = 0.05 SOL
        paths = [path('L%d' % i, T0 + 3600 * i, CRASH) for i in range(5)]
        r = R.replay(paths, c)
        self.assertTrue(r.skipped_entries)
        self.assertEqual(len(r.closed) + len(r.skipped_entries), 5)
        self.assertTrue(all(s['reasons'] == ['DAILY_LOSS_STOP'] for s in r.skipped_entries))
        self.assertEqual([t.mint for t in r.closed], ['L%d' % i for i in range(len(r.closed))])  # the earliest candidates trade
        # the same losses spread over two UTC days: the second day starts from its own equity, so it trades again
        two_days = [path('D%d' % i, DAY0 + 80000 + 3000 * i, CRASH) for i in range(6)]  # crosses midnight after the 2nd path
        r2 = R.replay(two_days, c)
        entered_after_midnight = [t for t in r2.closed if t.entry_ts >= DAY0 + 86400]
        self.assertTrue(entered_after_midnight)

    def test_loss_streak_pause_after_six_losers(self):
        paths = [path('L%d' % i, T0 + 3600 * i, CRASH) for i in range(8)]
        r = R.replay(paths, cfg(daily_loss_stop_fraction='0.5', daily_liquidate_fraction='0.9'))
        self.assertEqual(len(r.closed), 6)
        self.assertTrue(all(s['reasons'] == ['LOSS_STREAK_PAUSE'] for s in r.skipped_entries))

    def test_cooldown_blocks_same_mint_only_via_new_path_and_config_changes_result(self):
        wide = R.replay([path('M', T0, [1, 1, 0.84, 1.0, 1.0, 1.0])], cfg(stop_fraction='0.30'))
        tight = R.replay([path('M', T0, [1, 1, 0.84, 1.0, 1.0, 1.0])], cfg(stop_fraction='0.05'))
        self.assertEqual(trade_of(wide, 'M').status, 'OPEN_AT_END')
        self.assertEqual(trade_of(tight, 'M').exit_reason, 'STOP')

    def test_fee_and_slippage_come_from_the_strategy_config_via_paper(self):
        pc = R.paper_config(self.c)
        self.assertEqual((pc.fee_lamports, pc.slippage_bps), (50_000, 50))
        free = R.replay([path('M', T0, CRASH)], cfg(fixed_fee_sol='0', adverse_slippage_bps='0'))
        costly = R.replay([path('M', T0, CRASH)], self.c)
        self.assertGreater(trade_of(free, 'M').realized_lamports, trade_of(costly, 'M').realized_lamports)
        with self.assertRaises(R.ReplayError):
            R.paper_config(cfg(fixed_fee_sol='0.0000000005'))  # not a whole number of lamports

    def test_replay_config_validation(self):
        for kw in ({'pool_fee_bps': -1}, {'pool_fee_bps': 10000}, {'pool_fee_bps': 30.0}, {'entry_delay_s': -1},
                   {'entry_delay_s': float('nan')}, {'initial_cash_sol': 5}, {'initial_cash_sol': D(0)}):
            with self.assertRaises(R.ReplayError, msg=kw):
                R.ReplayConfig(**kw)

    def test_property_cash_never_negative_and_realized_reconciles(self):
        import random
        rnd = random.Random(7)
        for _ in range(30):
            paths = []
            for i in range(6):
                walk, v = [1.0], 1.0
                for _ in range(rnd.randint(5, 60)):
                    v = max(0.05, v * rnd.uniform(0.85, 1.2))
                    walk.append(round(v, 4))
                paths.append(path('R%d' % i, T0 + rnd.randint(0, 900), walk))
            r = R.replay(paths, self.c)
            realized_closed = sum(t.realized_lamports for t in r.closed)
            self.assertGreaterEqual(r.final_cash_lamports, 0)
            open_basis = sum(t.cost_lamports for t in r.open_at_end)
            self.assertLessEqual(abs(5 * paper.LAMPORTS + realized_closed - r.final_cash_lamports), open_basis + 1)  # open cost still out
            for t in r.closed:
                self.assertGreater(t.cost_lamports, 0)
                self.assertEqual(t.reasons[-1], t.exit_reason)


class LiquidationAndMarksTests(unittest.TestCase):
    c = cfg()

    def test_daily_liquidation_sells_flat_positions_at_their_next_mark(self):
        c = cfg(daily_loss_stop_fraction='0.005', daily_liquidate_fraction='0.01', loss_streak_pause_at=100)  # liquidate at -0.05 SOL
        flat = path('FLAT', T0, [1.0] * 30)
        loser = path('LOSS', T0 + 60, [1, 1, 0.5, 0.5, 0.5])  # a stop-out of ~0.05 SOL breaches the 1% daily limit
        r = R.replay([flat, loser], c)
        self.assertIn(trade_of(r, 'LOSS').exit_reason, ('STOP', 'LIQUIDATE'))  # the same breach may liquidate the loser first
        self.assertEqual(trade_of(r, 'FLAT').exit_reason, 'LIQUIDATE')
        self.assertGreaterEqual(trade_of(r, 'FLAT').exit_ts, trade_of(r, 'LOSS').entry_ts)
        quiet = R.replay([flat], c)
        self.assertEqual(trade_of(quiet, 'FLAT').status, 'OPEN_AT_END')

    def test_touching_115_persists_so_a_later_dip_does_not_trigger_the_time_stop(self):
        r = R.replay([path('M', T0, [1.0, 1.3] + [1.0] * 400)], cfg(stop_fraction='0.5', max_hold_seconds=5000))
        t = trade_of(r, 'M')
        self.assertEqual(t.exit_reason, 'MAX_HOLD')  # without the persisted touch, TIME_STOP would have fired at 2700 s

    def test_peak_persists_so_trailing_stop_uses_the_best_mark(self):
        r = R.replay([path('M', T0, [1, 1, 1.5, 2.1, 3.2, 3.0, 2.15])], self.c)
        t = trade_of(r, 'M')
        self.assertEqual(t.reasons, ['TAKE_PROFIT', 'TAKE_PROFIT', 'TAKE_PROFIT', 'TRAILING_STOP'])

    def test_unsellable_stop_is_blocked_and_the_position_stays_open(self):
        # price collapses so far that the sell nets nothing after slippage and the fee: STUCK_POSITION, retried, never filled
        r = R.replay([path('M', T0, [1, 1, 0.0003, 0.0003])], self.c)
        self.assertEqual(trade_of(r, 'M').status, 'OPEN_AT_END')
        self.assertGreaterEqual(r.anomalies.get('BLOCKED_EXIT', 0), 1)
        self.assertEqual([f.side for f in r.fills], ['buy'])

    def test_a_second_path_for_a_held_mint_is_skipped(self):
        r = R.replay([path('M', T0, [1.0] * 30), path('M', T0 + 70, [1.0] * 30)], cfg(stop_fraction='0.9', time_stop_seconds=100000))
        self.assertEqual([s['reasons'] for s in r.skipped_entries], [['ALREADY_HELD']])
        self.assertEqual(len(r.trades), 1)


class MetricsTests(unittest.TestCase):
    def t(self, pnl_lamports):
        return R.Trade('M', 'CLOSED', 0, 1, 100_000_000, pnl_lamports, 'STOP', ['STOP'], 2)

    def test_known_answers(self):
        pnl = [10, -4, 6, -2, 20]  # in units of 1e-9 SOL x 1e6 => use lamports directly
        trades = [self.t(x * 1_000_000) for x in pnl]
        m = R.metrics(trades, D(5))
        self.assertEqual(m['n_trades'], 5)
        self.assertEqual(m['win_rate'], 0.6)
        self.assertEqual(D(m['mean_pnl_sol']), D('0.006'))
        self.assertEqual(D(m['median_pnl_sol']), D('0.006'))
        self.assertEqual(D(m['total_pnl_sol']), D('0.03'))
        self.assertEqual(D(m['profit_factor']), D(36) / D(6))
        # cumulative: 10, 6, 12, 10, 30 -> peak 12 then 10: drawdown 4 (max of 4 at idx2 -> 4, then 2)
        self.assertEqual(D(m['max_drawdown_sol']), D('0.004'))
        self.assertEqual(D(m['max_drawdown_fraction']), D('0.004') / 5)

    def test_even_median_no_losses_and_empty(self):
        m = R.metrics([self.t(2_000_000), self.t(4_000_000)], D(5))
        self.assertEqual(D(m['median_pnl_sol']), D('0.003'))
        self.assertEqual((m['profit_factor'], m['profit_factor_note']), (None, 'NO_LOSSES'))
        flat = R.metrics([self.t(0)], D(5))
        self.assertEqual((flat['profit_factor'], flat['profit_factor_note'], flat['win_rate']), (None, 'NO_PNL', 0.0))
        self.assertEqual(R.metrics([], D(5))['n_trades'], 0)

    def test_open_trades_are_ignored(self):
        open_trade = R.Trade('O', 'OPEN_AT_END', 0, 1, 100, -999_999_999, None, [], 1)
        self.assertEqual(R.metrics([open_trade, self.t(1_000_000)], D(5))['n_trades'], 1)

    def test_output_is_json_safe(self):
        json.dumps(R.metrics([self.t(-5), self.t(5)], D(5)), allow_nan=False)


def build_store(directory, specs, name='lean.sqlite', *, starts=True):
    """specs: [(mint, start_ts, ratios, meta_overrides)] -> a real Store with path_start + path_mark observations."""
    store = Store(Path(directory) / name, initial_cash_sol=5, code_version=CV, strategy_version=SV, clock=lambda: float(T0))
    for mint, start, ratios, extra in specs:
        if starts:
            store.add_observation('path_start', b'{}', mint=mint, ts=start,
                                  meta={'features': dict(FEATS), 'not_entered_reason': None, **extra.get('start', {})})
        for k, ratio in enumerate(ratios):
            meta = {'price_sol': str(BASE_PRICE * D(str(ratio))), 'liquidity_sol': '100', **extra.get('mark', {})}
            store.add_observation('path_mark', b'{}', mint=mint, ts=start + 15 * k, meta=meta)
    return store


def ro(path):
    c = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)
    return c


class LoadTests(Tmp):
    def load(self, specs, **kw):
        store = build_store(self.dir, specs)
        store.close()
        c = ro(self.dir / 'lean.sqlite')
        self.addCleanup(c.close)
        return R.load_paths(c, **kw)

    def test_round_trip_and_sorting(self):
        paths, skipped = self.load([('B', T0 + 100, CRASH, {}), ('A', T0, CRASH, {})])
        self.assertEqual([p.mint for p in paths], ['A', 'B'])
        self.assertEqual((len(paths[0].points), paths[0].decimals, skipped), (5, 6, {}))
        self.assertEqual(paths[0].points[0].price_sol, BASE_PRICE)
        self.assertEqual(paths[0].points[0].liquidity_sol, D(100))

    def test_corrupt_marks_are_dropped_and_counted_never_repaired(self):
        for key, value, counter in (('price_sol', 'NaN', 'MARK_BAD_PRICE'), ('price_sol', '0', 'MARK_BAD_PRICE'), ('price_sol', '-1', 'MARK_BAD_PRICE'),
                                    ('price_sol', None, 'MARK_BAD_PRICE'), ('price_sol', True, 'MARK_BAD_PRICE'), ('price_sol', 'abc', 'MARK_BAD_PRICE'),
                                    ('liquidity_sol', 'Infinity', 'MARK_BAD_LIQUIDITY'), ('liquidity_sol', '-5', 'MARK_BAD_LIQUIDITY'),
                                    ('liquidity_sol', '0', 'MARK_BAD_LIQUIDITY')):
            store = build_store(self.dir, [('M', T0, [1, 1, 1], {})], name='x%d.sqlite' % abs(hash((key, str(value)))))
            store.add_observation('path_mark', b'{}', mint='M', ts=T0 + 100, meta={'price_sol': '0.000001', 'liquidity_sol': '100', key: value})
            store.close()
            c = ro(store.path)
            paths, skipped = R.load_paths(c)
            c.close()
            self.assertEqual(skipped.get(counter), 1, (key, value))
            self.assertEqual(len(paths[0].points), 3)

    def test_missing_liquidity_key_is_allowed_null_means_no_impact_but_garbage_is_not(self):
        paths, skipped = self.load([('M', T0, [1, 1, 1], {'mark': {'liquidity_sol': None}})])
        self.assertTrue(all(p.liquidity_sol is None for p in paths[0].points))
        self.assertEqual(skipped, {})

    def test_unusable_paths_are_skipped_with_reasons(self):
        paths, skipped = self.load([('ONE', T0, [1], {}), ('BADDEC', T0, [1, 1], {'start': {'decimals': 99}}),
                                    ('OK', T0, [1, 1], {'start': {'decimals': 9}})])
        self.assertEqual([p.mint for p in paths], ['OK'])
        self.assertEqual(paths[0].decimals, 9)
        self.assertEqual((skipped['PATH_TOO_FEW_MARKS'], skipped['PATH_BAD_DECIMALS']), (1, 1))

    def test_marks_without_a_start_cannot_be_replayed(self):
        store = build_store(self.dir, [('M', T0, [1, 1, 1], {})], starts=False)
        store.close()
        c = ro(store.path)
        paths, skipped = R.load_paths(c)
        c.close()
        self.assertEqual((paths, skipped), ([], {'MARKS_WITHOUT_START': 1}))

    def test_marks_before_start_and_duplicate_ts_and_duplicate_start(self):
        store = build_store(self.dir, [('M', T0, [1, 1, 1], {})])
        store.add_observation('path_mark', b'{}', mint='M', ts=T0 - 50, meta={'price_sol': '0.5', 'liquidity_sol': '100'})
        store.add_observation('path_mark', b'{}', mint='M', ts=T0 + 15, meta={'price_sol': '9', 'liquidity_sol': '100'})  # same ts as an earlier mark
        store.add_observation('path_start', b'{}', mint='M', ts=T0 + 5, meta={'features': {'market_cap_usd': '1'}})
        store.close()
        c = ro(store.path)
        paths, skipped = R.load_paths(c)
        c.close()
        self.assertEqual((skipped['MARK_BEFORE_START'], skipped['MARK_DUPLICATE_TS'], skipped['START_DUPLICATE']), (1, 1, 1))
        self.assertEqual(paths[0].start_ts, T0)  # the first start wins
        self.assertEqual(paths[0].features['market_cap_usd'], '100000')
        self.assertEqual(paths[0].points[1].price_sol, BASE_PRICE)  # the first mark at a timestamp wins

    def test_since_until_window_and_price_from_raw_body(self):
        store = build_store(self.dir, [('A', T0, [1, 1], {}), ('B', T0 + 1000, [1, 1], {})])
        store.add_observation('path_start', b'{}', mint='C', ts=T0 + 2000, meta={'features': dict(FEATS)})
        for k in range(2):
            store.add_observation('path_mark', json.dumps({'price_sol': '0.000001', 'liquidity_sol': '100'}).encode(), mint='C', ts=T0 + 2000 + 15 * k)
        store.close()
        c = ro(store.path)
        everything, _ = R.load_paths(c)
        window, _ = R.load_paths(c, since=T0 + 500, until=T0 + 1500)
        c.close()
        self.assertEqual([p.mint for p in everything], ['A', 'B', 'C'])
        self.assertEqual([p.mint for p in window], ['B'])

    def test_unreadable_start_meta(self):
        store = build_store(self.dir, [('A', T0, [1, 1], {})])
        store.add_observation('path_start', b'{}', mint='Z', ts=T0, meta={'nofeatures': 1})
        store.close()
        c = ro(store.path)
        _, skipped = R.load_paths(c)
        c.close()
        self.assertEqual(skipped.get('START_UNREADABLE'), 1)


class CalibrationTests(Tmp):
    c = cfg()

    def live(self, store, mint, reasons, *, close=True):
        store.record('fill', {'mint': mint, 'side': 'buy'}, code_version=CV, strategy_version=SV)
        for reason in reasons:
            store.record('fill', {'mint': mint, 'side': 'sell', 'reason': reason}, code_version=CV, strategy_version=SV)

    def run_cal(self, specs, live):
        store = build_store(self.dir, specs)
        for mint, reasons in live:
            self.live(store, mint, reasons)
        store.close()
        conn = ro(store.path)
        self.addCleanup(conn.close)
        return R.calibrate(conn, self.c)

    def test_reproduces_live_exit_reasons(self):
        cal = self.run_cal([('A', T0, RALLY_THEN_FADE, {}), ('B', T0 + 600, CRASH, {})],
                           [('A', ['TAKE_PROFIT', 'TAKE_PROFIT', 'TAKE_PROFIT', 'STOP']), ('B', ['STOP'])])
        self.assertEqual((cal['status'], cal['matched'], cal['tokens']), ('OK', 2, 2))

    def test_mismatch_is_reported_not_hidden(self):
        cal = self.run_cal([('A', T0, RALLY_THEN_FADE, {}), ('B', T0 + 600, CRASH, {})],
                           [('A', ['TAKE_PROFIT', 'TAKE_PROFIT', 'TAKE_PROFIT', 'STOP']), ('B', ['TIME_STOP'])])
        self.assertEqual((cal['status'], cal['matched'], cal['mismatched']), ('MISMATCH', 1, 1))
        bad = [r for r in cal['rows'] if not r['match']][0]
        self.assertEqual((bad['mint'], bad['live_reasons'], bad['replay_reasons'], bad['note']), ('B', ['TIME_STOP'], ['STOP'], 'EXIT_REASONS_DIFFER'))

    def test_unreplayable_live_tokens_are_listed(self):
        cal = self.run_cal([('A', T0, CRASH, {})], [('A', ['STOP']), ('GONE', ['STOP'])])
        notes = {r['mint']: r['note'] for r in cal['rows']}
        self.assertEqual((cal['status'], notes['GONE']), ('MISMATCH', 'NO_PATH'))

    def test_replay_that_does_not_enter_is_a_mismatch(self):
        cal = self.run_cal([('A', T0, CRASH, {'start': {'features': dict(FEATS, market_cap_usd='1')}})], [('A', ['STOP'])])
        self.assertEqual(cal['rows'][0]['note'], 'NOT_ENTERED_IN_REPLAY')
        self.assertEqual(cal['status'], 'MISMATCH')

    def test_extra_or_missing_sells_do_not_match(self):
        for live in (['STOP', 'STOP'], ['TAKE_PROFIT'], []):
            cal = self.run_cal([('A', T0, CRASH, {})], [('A', live)])
            self.assertEqual(cal['status'], 'MISMATCH', live)
            os.unlink(self.dir / 'lean.sqlite')
            for suffix in ('-wal', '-shm'):
                p = self.dir / ('lean.sqlite' + suffix)
                if p.exists():
                    os.unlink(p)

    def test_no_live_fills(self):
        store = build_store(self.dir, [('A', T0, CRASH, {})])
        store.close()
        conn = ro(store.path)
        self.addCleanup(conn.close)
        self.assertEqual(R.calibrate(conn, self.c)['status'], 'NO_LIVE_FILLS')

    def test_open_live_position_matches_as_prefix_of_replay(self):
        store = build_store(self.dir, [('A', T0, RALLY_THEN_FADE, {})])
        pc = paper.PaperConfig()
        qty = 99_000_000
        fill = paper.buy(paper.Quote('A', 'buy', 100_000_000, qty, 6, float(T0)), '0.1', pc)
        store.add_fill(fill)  # a real typed fill: the position is OPEN in the store
        store.record('fill', {'mint': 'A', 'side': 'buy'}, code_version=CV, strategy_version=SV)
        store.record('fill', {'mint': 'A', 'side': 'sell', 'reason': 'TAKE_PROFIT'}, code_version=CV, strategy_version=SV)
        store.close()
        conn = ro(store.path)
        self.addCleanup(conn.close)
        cal = R.calibrate(conn, self.c)
        self.assertEqual(cal['status'], 'OK')
        self.assertEqual(cal['rows'][0]['replay_reasons'], ['TAKE_PROFIT', 'TAKE_PROFIT', 'TAKE_PROFIT', 'STOP'])


if __name__ == '__main__':
    unittest.main()
