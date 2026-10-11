"""SYNTHETIC_TEST_ONLY: offline replay of the lean strategy on recorded price paths (lean/replay.py). Fixtures only, no network.

Paths are synthetic: a pool of 1e8 tokens (6 decimals) against 100 SOL, the quote vault scaled by a ratio list sampled every 15 s
(price is proportional to the quote vault). Marks are `Point(ts, base_raw, quote_raw)` exactly as the recorder stores them, and the
paths database is a real `lean.paths.PathStore` file so the loader is exercised on the actual schema."""
import dataclasses
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from lean import adapters as A, paper, paths as L, replay as R, strategy as S
from lean.store import Store

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / 'config' / 'lean' / 'strategy-default.json'
DAY0 = 19675 * 86400  # a UTC midnight
T0 = DAY0 + 3600  # one hour into the day: tests spanning a few hours stay inside one UTC day unless they say otherwise
BASE_RAW = 10 ** 14                      # 1e8 tokens at 6 decimals
QUOTE0 = 100 * 10 ** 9                   # 100 SOL: price 1e-6 SOL per token, market cap 150000 USD at SOL/USD 150, liquidity 30000 USD
SUPPLY = 10 ** 15                        # 1e9 tokens
FEATS = {'decimals': 6, 'supply_raw': str(SUPPLY), 'sol_usd': '150', 'token_program': 'Tokenkeg'}
CV, SV = 'testcode', 'lean-1'
FEE_BPS = 30


def cfg(**over):
    raw = json.loads(DEFAULT.read_text())
    raw.update(over)
    return S.StrategyConfig.from_dict(raw)


def quote_at(ratio, quote0=QUOTE0):
    return int(quote0 * D(str(ratio)))


def points(start, ratios, dt=15, base=BASE_RAW, quote0=QUOTE0):
    return tuple(R.Point(start + dt * k, base, quote_at(r, quote0)) for k, r in enumerate(ratios))


def path(mint, start, ratios, feats=None, **kw):
    pts = points(start, ratios, **{k: kw.pop(k) for k in ('dt', 'base', 'quote0') if k in kw})
    base = dict(FEATS if feats is None else feats)
    return R.Path(mint, start, A.entry_features(base, mint), pts, kw.pop('decimals', 6), kw.pop('not_entered_reason', None),
                  kw.pop('fee_raw', 0), kw.pop('virtual_quote_raw', 0), int(base.get('supply_raw', SUPPLY)), D(str(base.get('sol_usd', '150'))), **kw)


# ---- an independent oracle: plain integer arithmetic written out in the test, no lean code
def oracle_buy_tokens(lamports=100_000_000, ratio=1, fee_bps=FEE_BPS):
    eff = lamports * (10000 - fee_bps) // 10000
    out = BASE_RAW * eff // (quote_at(ratio) + eff)
    return out * 9950 // 10000                      # the paper fill holds this after 50 bps adverse slippage


def oracle_mark(qty_raw, ratio, fee_bps=FEE_BPS):
    """THE live mark: constant-product output less the pool fee less the fixed fee. No slippage haircut."""
    gross = quote_at(ratio) * qty_raw // (BASE_RAW + qty_raw)
    return gross * (10000 - fee_bps) // 10000 - 50_000


def oracle_sell_lamports(qty_raw, ratio, fee_bps=FEE_BPS):
    gross = quote_at(ratio) * qty_raw // (BASE_RAW + qty_raw)
    return gross * (10000 - fee_bps) // 10000 * 9950 // 10000          # the fill: the quote after 50 bps adverse slippage


def oracle_net_ratio(ratio):
    return D(oracle_mark(oracle_buy_tokens(), ratio)) / D(100_050_000)


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
        held = oracle_buy_tokens()
        self.assertEqual((buy_fill.side, buy_fill.sol_lamports, buy_fill.fee_lamports, buy_fill.qty_raw), ('buy', 100_000_000, 50_000, held))
        # the stop fires at the 0.7 mark (the 0.9 mark is still above the 0.82 line): sell everything at that price
        self.assertEqual((sell_fill.side, sell_fill.qty_raw), ('sell', held))
        self.assertEqual(sell_fill.sol_lamports, oracle_sell_lamports(held, D('0.7')))
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

    def test_entry_filters_judge_the_pool_at_the_entry_mark_with_strategy_reasons(self):
        # market cap = supply x price x SOL/USD and liquidity = 2 x spendable SOL x SOL/USD, both from the vault amounts of THAT mark
        cases = [('MARKET_CAP', dict(base=100 * BASE_RAW), CRASH),                 # price 1e-8: market cap 1500 USD
                 ('MARKET_CAP', dict(), [40, 40, 40]),                            # price x40: market cap 6M USD
                 ('LIQUIDITY', dict(base=BASE_RAW // 20), [D('0.05')] * 3)]       # 5 SOL against 5e6 tokens: market cap in band, liquidity 1500 USD
        for reason, kw, ratios in cases:
            r = R.replay([path('M', T0, ratios, **kw)], self.c)
            self.assertEqual(r.trades, [], reason)
            self.assertIn(reason, r.skipped_entries[0]['reasons'], reason)

    def test_the_entry_band_is_judged_at_the_entry_mark_not_at_the_screen(self):
        # priced 40x too high at the first mark (out of the market-cap band), back in band 30 s later: with first_mark timing the
        # candidate is considered ONCE at its first mark and dropped; a pullback entry fills at an in-band mark and trades
        ratios = [40, 40, 1, 1, 1, 1]
        first = R.replay([path('M', T0, ratios)], self.c)
        self.assertEqual(first.trades, [])
        self.assertEqual(first.skipped_entries[0]['reasons'], ['MARKET_CAP'])
        pull = R.replay([path('M', T0, ratios)], cfg(stop_fraction='0.9', time_stop_seconds=100000),
                        R.ReplayConfig(entry_timing=R.EntryTiming('pullback', pullback_pct=D('0.5'))))
        self.assertEqual(trade_of(pull, 'M').entry_ts, T0 + 45)       # triggered at 30 s (40 -> 1), filled at the next mark, in band

    def test_the_spendable_reserve_excludes_the_pool_fees_and_the_price_includes_virtual_reserves(self):
        p = path('M', T0, [1, 1], fee_raw=QUOTE0 // 2)                 # half of the quote vault is pool fees: liquidity 15000 USD
        f = R.features_at(p, p.points[0])
        self.assertEqual((f['quote_gross_raw'], f['quote_spendable_raw'], f['reserve_sol']), (str(QUOTE0), str(QUOTE0 // 2), '50'))
        self.assertEqual(D(f['liquidity_usd']), D(15000))
        self.assertEqual(D(f['market_cap_usd']), D(150000))             # the price follows the gross (effective) quote reserve
        v = path('M', T0, [1, 1], virtual_quote_raw=QUOTE0)            # a boosted pool: effective reserve = 2 x gross
        self.assertEqual(D(R.features_at(v, v.points[0])['market_cap_usd']), D(300000))
        self.assertEqual(D(R.features_at(v, v.points[0])['liquidity_usd']), D(30000))
        self.assertIsNone(R.features_at(path('M', T0, [1, 1], fee_raw=QUOTE0), p.points[0]))      # nothing spendable: not priceable

    def test_cost_budget_blocks_thin_pools(self):
        strict = cfg(min_liquidity_usd='100', max_roundtrip_cost_fraction='0.03')
        thin = R.replay([path('M', T0, [1, 1, 1], base=BASE_RAW // 50, quote0=2 * 10 ** 9)], strict)     # a 2 SOL pool: ~6% round-trip impact
        self.assertEqual(thin.trades, [])
        self.assertEqual(thin.skipped_entries[0]['reasons'], ['COST_BUDGET'])
        self.assertEqual(len(R.replay([path('M', T0, [1, 1, 1])], strict).trades), 1)                  # the 100 SOL pool is fine under the same cap

    def test_reserves_decide_the_price_impact_of_the_exit(self):
        # the same price path in a deep and in a thin (but tradable) pool: the thin pool pays less on exit
        deep = R.replay([path('M', T0, CRASH)], cfg(min_liquidity_usd='100', max_roundtrip_cost_fraction='0.9'))
        thin = R.replay([path('M', T0, CRASH, base=BASE_RAW // 20, quote0=5 * 10 ** 9)], cfg(min_liquidity_usd='100', max_roundtrip_cost_fraction='0.9'))
        self.assertEqual((trade_of(deep, 'M').exit_reason, trade_of(thin, 'M').exit_reason), ('STOP', 'STOP'))
        self.assertLess(trade_of(thin, 'M').realized_lamports, trade_of(deep, 'M').realized_lamports)

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


def dead_path(mint, start, ratios, dead_from=None, status='ACCOUNT_MISSING', recover_at=None):
    """A path whose marks from index `dead_from` are dead (and optionally recover from `recover_at`)."""
    p = path(mint, start, ratios)
    pts = tuple(R.Point(q.ts, None, None, status) if dead_from is not None and k >= dead_from and (recover_at is None or k < recover_at) else q
                for k, q in enumerate(p.points))
    return dataclasses.replace(p, points=pts)


class DeadPoolTests(Tmp):
    c = cfg()

    def test_a_position_in_a_pool_that_stays_dead_is_written_off_not_censored(self):
        r = R.replay([dead_path('M', T0, [1, 1, 1, 1, 1, 1], dead_from=3)], self.c)
        t = trade_of(r, 'M')
        self.assertEqual((t.status, t.exit_reason, t.reasons), ('CLOSED', 'RUG_WRITEOFF', ['RUG_WRITEOFF']))
        self.assertEqual(t.realized_lamports, -100_050_000)  # the whole cost basis (size + buy fee) is lost
        self.assertEqual(r.anomalies, {'POOL_DEAD_MARK': 3, 'RUG_WRITEOFF': 1})
        self.assertEqual([f.side for f in r.fills], ['buy'])  # nothing was sold into a dead pool
        m = R.metrics(r.trades, D(5))
        self.assertEqual((m['n_trades'], m['win_rate']), (1, 0.0))

    def test_write_off_keeps_what_was_already_realized(self):
        r = R.replay([dead_path('M', T0, [1, 1, 1.5, 1.5, 1.5, 1.5], dead_from=3)], self.c)  # TP1 before the pool dies
        t = trade_of(r, 'M')
        self.assertEqual((t.status, t.reasons), ('CLOSED', ['TAKE_PROFIT', 'RUG_WRITEOFF']))
        sold = next(f for f in r.fills if f.side == 'sell')
        self.assertEqual(t.realized_lamports, sold.realized_lamports - (100_050_000 - sold.cost_sold_lamports))

    def test_a_pool_that_recovers_exits_normally(self):
        r = R.replay([dead_path('M', T0, [1, 1, 1, 1, 0.5, 0.5], dead_from=2, recover_at=4)], self.c)
        t = trade_of(r, 'M')
        self.assertEqual((t.status, t.exit_reason), ('CLOSED', 'STOP'))
        self.assertEqual(r.anomalies, {'POOL_DEAD_MARK': 2})

    def test_never_enters_at_a_dead_mark_and_dead_equity_is_zero(self):
        r = R.replay([dead_path('M', T0, [1, 1, 1], dead_from=0, recover_at=1)], self.c)
        self.assertEqual(trade_of(r, 'M').entry_ts, T0 + 15)  # the first mark is dead; the entry waits for a real one
        none = R.replay([dead_path('X', T0, [1, 1, 1], dead_from=0)], self.c)
        self.assertEqual((none.trades, [s['reasons'] for s in none.skipped_entries]), ([], [['ENTRY_TIMING_NO_TRIGGER']]))


class EntryTimingTests(unittest.TestCase):
    c = cfg()
    DIP = [1, 1.2, 1.2, 1.0, 1.0, 1.0]       # the high is 1.2; the 4th mark is 16.7% below it
    RAMP = [1 + k * D('0.001') for k in range(60)]  # +0.1% every 15 s

    def entry_ts(self, ratios, timing, mint='M', **kw):
        r = R.replay([path(mint, T0, ratios, **kw)], cfg(stop_fraction='0.9', time_stop_seconds=100000), R.ReplayConfig(entry_timing=timing))
        return (trade_of(r, mint).entry_ts if r.trades else None), r

    def test_validation_and_specs(self):
        for bad in ({'kind': 'nope'}, {'kind': 'pullback'}, {'kind': 'pullback', 'pullback_pct': D(1)}, {'kind': 'pullback', 'pullback_pct': D('0.1'), 'momentum_minutes': 3},
                    {'kind': 'momentum'}, {'kind': 'momentum', 'momentum_minutes': 0}, {'kind': 'momentum', 'momentum_minutes': 61},
                    {'kind': 'momentum', 'momentum_minutes': True}, {'kind': 'first_mark', 'pullback_pct': D('0.1')},
                    {'kind': 'first_mark', 'max_wait_s': 0}, {'kind': 'first_mark', 'max_wait_s': float('nan')}):
            with self.assertRaises(R.ReplayError, msg=bad):
                R.EntryTiming(**bad)
        for bad in ({'kind': 'pullback', 'pullback_pct': 'abc'}, {'kind': 'pullback', 'pullback_pct': '-0.1'}, {'kind': 'pullback', 'pullback_pct': '0.1', 'extra': 1},
                    {'pullback_pct': '0.1'}, 5, None, [], 'bogus'):
            with self.assertRaises(R.ReplayError, msg=bad):
                R.EntryTiming.from_spec(bad)
        self.assertEqual(R.EntryTiming.from_spec('first_mark'), R.EntryTiming())
        t = R.EntryTiming.from_spec({'kind': 'pullback', 'pullback_pct': '0.10'})
        self.assertEqual((t.label, t.pullback_pct), ('pullback(0.1)', D('0.10')))
        self.assertEqual(R.EntryTiming.from_spec({'kind': 'momentum', 'momentum_minutes': 3}).label, 'momentum(3m)')
        with self.assertRaises(R.ReplayError):
            R.ReplayConfig(entry_timing='first_mark')

    def test_first_mark_is_the_default_behaviour(self):
        a, _ = self.entry_ts(self.DIP, R.EntryTiming())
        self.assertEqual(a, T0)

    def test_pullback_enters_at_the_first_mark_that_is_deep_enough(self):
        at, r = self.entry_ts(self.DIP, R.EntryTiming('pullback', pullback_pct=D('0.1')))
        self.assertEqual(at, T0 + 60)  # triggered at 45 s (1.0 vs high 1.2: 16.7% down), FILLED at the next mark
        none, r2 = self.entry_ts(self.DIP, R.EntryTiming('pullback', pullback_pct=D('0.2')))
        self.assertIsNone(none)
        self.assertEqual(r2.skipped_entries[0]['reasons'], ['ENTRY_TIMING_NO_TRIGGER'])

    def test_pullback_threshold_is_inclusive_and_the_first_mark_is_the_initial_high(self):
        at, _ = self.entry_ts([1, 1, 0.9, 0.9], R.EntryTiming('pullback', pullback_pct=D('0.1')))
        self.assertEqual(at, T0 + 45)  # triggered at 30 s (exactly 10% below the first mark's price), filled at 45 s
        flat, _ = self.entry_ts([1] * 8, R.EntryTiming('pullback', pullback_pct=D('0.1')))
        self.assertIsNone(flat)

    def test_pullback_is_measured_from_the_highest_mark_seen_not_the_latest(self):
        at, _ = self.entry_ts([1, 1.5, 1.4, 1.3, 1.2, 1.1], R.EntryTiming('pullback', pullback_pct=D('0.2')))
        self.assertEqual(at, T0 + 75)  # triggered at 60 s (1.2 is 20% below 1.5; 1.4 / 1.3 are not), filled at 75 s

    def test_momentum_needs_strictly_rising_minute_samples(self):
        at, _ = self.entry_ts(self.RAMP, R.EntryTiming('momentum', momentum_minutes=3))
        self.assertEqual(at, T0 + 195)  # triggered at 180 s (3 full minutes of history), filled at the next mark
        at1, _ = self.entry_ts(self.RAMP, R.EntryTiming('momentum', momentum_minutes=1))
        self.assertEqual(at1, T0 + 75)
        step, r = self.entry_ts([1, 1, 1, 1, 1.3, 1.3, 1.3, 1.3, 1.3, 1.3, 1.3, 1.3, 1.3], R.EntryTiming('momentum', momentum_minutes=2))
        self.assertIsNone(step)  # one step up is not two rising minutes: the plateau after it is not momentum
        one, _ = self.entry_ts([1, 1, 1, 1, 1.3, 1.3], R.EntryTiming('momentum', momentum_minutes=1))
        self.assertEqual(one, T0 + 75)  # but for a single minute the step itself is a rise (triggered at 60 s, filled at 75 s)

    def test_momentum_is_not_fooled_by_a_dip_inside_the_window(self):
        ratios = [1.0, 1.01, 1.02, 1.03, 1.04, 0.9, 1.05, 1.06, 1.07, 1.08, 1.09, 1.10, 1.11, 1.12, 1.13, 1.14]
        at, _ = self.entry_ts(ratios, R.EntryTiming('momentum', momentum_minutes=1))
        self.assertEqual(at, T0 + 75)  # trigger at 60 s: k=4 (1.04) beats k=0 (1.0); the later dip does not matter; filled at 75 s
        at2, _ = self.entry_ts([1.0, 1.01, 1.02, 1.03, 0.99, 1.05, 1.06, 1.07, 1.08, 1.09], R.EntryTiming('momentum', momentum_minutes=1))
        self.assertEqual(at2, T0 + 90)  # at k=4 the sample a minute ago (k=0, 1.0) is above 0.99: no momentum; k=5 (75 s) triggers, filled at 90 s

    def test_a_trigger_exactly_at_max_wait_still_enters(self):
        at, _ = self.entry_ts([1, 1, 1, 1, 0.8, 0.8], R.EntryTiming('pullback', pullback_pct=D('0.15'), max_wait_s=60))
        self.assertEqual(at, T0 + 75)  # the dip is the mark at 60 s: waiting up to 60 s is allowed; the order fills at the next mark
        late, _ = self.entry_ts([1, 1, 1, 1, 1, 0.8, 0.8], R.EntryTiming('pullback', pullback_pct=D('0.15'), max_wait_s=60))
        self.assertIsNone(late)        # one mark later it is too late

    def test_timeout_drops_the_candidate_once(self):
        timing = R.EntryTiming('pullback', pullback_pct=D('0.9'), max_wait_s=60)
        at, r = self.entry_ts([1.0] * 12, timing)
        self.assertIsNone(at)
        self.assertEqual([s['reasons'] for s in r.skipped_entries], [['ENTRY_TIMING_NO_TRIGGER']])
        late, r2 = self.entry_ts([1, 1, 1, 1, 1, 0.5, 0.5, 0.5], R.EntryTiming('pullback', pullback_pct=D('0.4'), max_wait_s=60))
        self.assertIsNone(late)  # the dip came at 75 s, after the 60 s wait
        self.assertEqual(len(r2.skipped_entries), 1)

    def test_timing_is_causal(self):
        ratios = list(self.DIP)
        a, _ = self.entry_ts(ratios, R.EntryTiming('pullback', pullback_pct=D('0.1')))
        for tail in ([0.01] * 4, [9.0] * 4):
            b, _ = self.entry_ts(ratios + tail, R.EntryTiming('pullback', pullback_pct=D('0.1')))
            self.assertEqual(a, b)
        m1, _ = self.entry_ts(self.RAMP, R.EntryTiming('momentum', momentum_minutes=3))
        m2, _ = self.entry_ts(self.RAMP[:14] + [0.1] * 10, R.EntryTiming('momentum', momentum_minutes=3))
        self.assertEqual(m1, m2)

    def test_a_later_entry_changes_the_outcome(self):
        pump_dump_rally = [1, 1, 0.8, 0.8, 1.2, 1.7, 2.2, 2.8, 2.7, 1.2]
        first = R.replay([path('M', T0, pump_dump_rally)], self.c)
        pull = R.replay([path('M', T0, pump_dump_rally)], self.c, R.ReplayConfig(entry_timing=R.EntryTiming('pullback', pullback_pct=D('0.15'))))
        self.assertEqual(trade_of(first, 'M').exit_reason, 'STOP')
        self.assertLess(trade_of(first, 'M').realized_lamports, 0)
        self.assertEqual(trade_of(pull, 'M').entry_ts, T0 + 45)
        self.assertGreater(trade_of(pull, 'M').realized_lamports, 0)

    def test_entry_delay_still_applies_on_top_of_a_trigger(self):
        at, _ = self.entry_ts(self.DIP, R.EntryTiming('pullback', pullback_pct=D('0.1')), )
        r = R.replay([path('M', T0, self.DIP + [1.0] * 6)], cfg(stop_fraction='0.9'),
                     R.ReplayConfig(entry_delay_s=60, entry_timing=R.EntryTiming('pullback', pullback_pct=D('0.1'))))
        self.assertEqual(at, T0 + 60)
        self.assertEqual(trade_of(r, 'M').entry_ts, T0 + 75)  # triggered at 45 s; the delay holds the decision until the 60 s mark, which fills at 75 s

    def test_a_trigger_on_the_last_mark_has_no_next_mark_to_fill_at(self):
        none, r = self.entry_ts([1, 1.2, 1.0], R.EntryTiming('pullback', pullback_pct=D('0.1')))
        self.assertIsNone(none)
        self.assertEqual([s['reasons'] for s in r.skipped_entries], [['ENTRY_NO_NEXT_MARK']])

    def test_a_next_mark_in_a_dead_pool_is_not_an_entry(self):
        dead = path('M', T0, [1, 1.2, 1.0])
        dead = R.Path(dead.mint, dead.start_ts, dead.features, dead.points + (R.Point(T0 + 45, None, None, 'EMPTY_POOL'), R.Point(T0 + 60, BASE_RAW, QUOTE0)),
                      dead.decimals, None, dead.fee_raw, dead.virtual_quote_raw, dead.supply_raw, dead.sol_usd)
        r = R.replay([dead], self.c, R.ReplayConfig(entry_timing=R.EntryTiming('pullback', pullback_pct=D('0.1'))))
        self.assertEqual(r.trades, [])
        self.assertEqual([s['reasons'] for s in r.skipped_entries], [['ENTRY_MARK_DEAD']])

    def test_first_mark_fills_at_the_mark_itself_and_the_others_one_mark_later(self):
        self.assertEqual(self.entry_ts([1, 1, 1], R.EntryTiming())[0], T0)
        for timing in (R.EntryTiming('pullback', pullback_pct=D('0.1')), R.EntryTiming('momentum', momentum_minutes=1)):
            ratios = [1, 0.8, 0.8, 0.8, 0.8, 0.8] if timing.kind == 'pullback' else self.RAMP[:40]
            at, r = self.entry_ts(ratios, timing)
            trigger = next(i for i in range(len(ratios)))                # replayed independently below
            self.assertEqual(at % 15, T0 % 15)
            self.assertGreater(at, T0)

    def test_the_fill_uses_the_reserves_of_the_fill_mark_not_the_trigger_mark(self):
        timing = R.EntryTiming('pullback', pullback_pct=D('0.1'))
        _, r = self.entry_ts([1, 0.9, 0.7, 0.7, 0.7], timing)          # triggered at 15 s (0.9), filled at 30 s (0.7)
        (buy,) = [f for f in r.fills if f.side == 'buy']
        self.assertEqual(buy.qty_raw, oracle_buy_tokens(ratio=D('0.7')))
        self.assertNotEqual(buy.qty_raw, oracle_buy_tokens(ratio=D('0.9')))

    def test_timing_is_part_of_the_reported_assumptions(self):
        d = R.ReplayConfig(entry_timing=R.EntryTiming('momentum', momentum_minutes=2)).to_dict()
        self.assertEqual(d['entry_timing']['kind'], 'momentum')
        json.dumps(d, allow_nan=False)



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


def start_row(mint, start, feats=None, reason=None, **target):
    """A `paths` row exactly as lean.paths.PathRecorder.start writes it."""
    f = dict(FEATS if feats is None else feats)
    tgt = {'mint': mint, 'pool': 'POOL', 'base_vault': 'BV', 'quote_vault': 'QV', 'decimals': f.get('decimals', 6), 'fee_raw': 0,
           'qty_raw': 1_000_000, 'cost_lamports': 100_000_000, 'basis': 'ref_size'}
    tgt.update(target)
    return {'mint': mint, 'candidate_id': None, 'start_ts': start, 'ends_ts': start + 21600, 'entered': 0, 'stage': 'screen',
            'reason': json.dumps([reason] if reason else []), 'screen_reasons': '[]', 'holders_checked': 0,
            'features': json.dumps(f), 'target': json.dumps(tgt), 'carried_from': None, 'code_version': CV, 'strategy_version': SV}


def mark_row(mint, ts, ratio=1, *, base=BASE_RAW, status='OK', quote=None):
    q = quote_at(ratio) if quote is None else quote
    return (mint, ts, 1, base if status == 'OK' else None, q if status == 'OK' else None, None, None, status)


def build_paths(directory, specs, name='paths.sqlite', *, starts=True):
    """specs: [(mint, start_ts, ratios, extras)] -> a real lean.paths.PathStore file with `paths` + `path_marks` rows."""
    store = L.PathStore(Path(directory) / name)
    for mint, start, ratios, extra in specs:
        if starts:
            store.insert_path(start_row(mint, start, extra.get('features'), extra.get('reason'), **extra.get('target', {})))
        store.add(marks=[mark_row(mint, start + 15 * k, ratio) for k, ratio in enumerate(ratios)])
    return store


def ro(path):
    return sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)


class LoadTests(Tmp):
    def load(self, specs, **kw):
        store = build_paths(self.dir, specs)
        store.close()
        c = ro(self.dir / 'paths.sqlite')
        self.addCleanup(c.close)
        return R.load_paths(c, **kw)

    def with_store(self, specs, extra, **kw):
        store = build_paths(self.dir, specs)
        extra(store)
        store.close()
        c = ro(store.path)
        self.addCleanup(c.close)
        return R.load_paths(c, **kw)

    def test_round_trip_keeps_the_raw_reserves_and_sorts(self):
        paths, skipped = self.load([('B', T0 + 100, CRASH, {}), ('A', T0, CRASH, {})])
        self.assertEqual([p.mint for p in paths], ['A', 'B'])
        self.assertEqual((len(paths[0].points), paths[0].decimals, skipped), (5, 6, {}))
        self.assertEqual((paths[0].points[0].base_raw, paths[0].points[0].quote_raw), (BASE_RAW, QUOTE0))
        self.assertEqual((paths[0].points[3].base_raw, paths[0].points[3].quote_raw), (BASE_RAW, quote_at(0.7)))
        self.assertEqual((paths[0].supply_raw, paths[0].sol_usd, paths[0].fee_raw), (SUPPLY, D(150), 0))

    def test_corrupt_reserves_are_dropped_and_counted_never_repaired(self):
        for k, (base, quote) in enumerate(((0, 5), (5, 0), (-1, 5), (5, -3), ('x', 5), (5, 'x'), (None, 5), (5, None), (1.5, 5))):
            def extra(store, base=base, quote=quote):
                store.db.execute('INSERT INTO path_marks(mint,ts,slot,base_raw,quote_raw,status) VALUES(?,?,?,?,?,?)', ('M', T0 + 100, 1, base, quote, 'OK'))
            d = Path(self.dir) / ('c%d' % k)
            d.mkdir()
            store = build_paths(d, [('M', T0, [1, 1, 1], {})])
            extra(store)
            store.close()
            c = ro(store.path)
            paths, skipped = R.load_paths(c)
            c.close()
            self.assertEqual(skipped.get('MARK_BAD_RESERVES'), 1, (base, quote))
            self.assertEqual(len(paths[0].points), 3)

    def test_dead_marks_load_as_dead_points_and_other_statuses_are_counted(self):
        def extra(store):
            store.add(marks=[mark_row('M', T0 + 100, status='ACCOUNT_MISSING'), mark_row('M', T0 + 110, status='EMPTY_POOL'),
                             mark_row('M', T0 + 120, status='MALFORMED'), mark_row('M', T0 + 130, status='WEIRD')])
        paths, skipped = self.with_store([('M', T0, [1, 1, 1, 1], {})], extra)
        self.assertEqual([(p.status, p.dead) for p in paths[0].points], [('OK', False)] * 4 + [('ACCOUNT_MISSING', True), ('EMPTY_POOL', True)])
        self.assertEqual(skipped, {'MARK_MALFORMED': 1, 'MARK_UNKNOWN_STATUS': 1})

    def test_the_reason_the_live_trader_gave_for_not_entering_is_kept(self):
        paths, _ = self.load([('M', T0, [1, 1], {'reason': 'COST_BUDGET'})])
        self.assertEqual(paths[0].not_entered_reason, 'COST_BUDGET')

    def test_features_come_from_the_recorded_start_and_are_normalised_by_the_live_adapter(self):
        paths, _ = self.load([('M', T0, [1, 1], {'features': dict(FEATS, quote_spendable_raw='50000000000')})])
        self.assertEqual(paths[0].features['mint'], 'M')
        self.assertEqual(paths[0].features, A.entry_features(dict(FEATS, quote_spendable_raw='50000000000'), 'M'))

    def test_unusable_paths_are_skipped_with_reasons(self):
        paths, skipped = self.load([('ONE', T0, [1], {}), ('BADDEC', T0, [1, 1], {'target': {'decimals': 99}}),
                                    ('OK', T0, [1, 1], {'features': dict(FEATS, decimals=9), 'target': {'decimals': 9}}),
                                    ('NOSUP', T0, [1, 1], {'features': {k: v for k, v in FEATS.items() if k != 'supply_raw'}}),
                                    ('NOUSD', T0, [1, 1], {'features': dict(FEATS, sol_usd='0')})])
        self.assertEqual([p.mint for p in paths], ['OK'])
        self.assertEqual(paths[0].decimals, 9)
        self.assertEqual((skipped['PATH_TOO_FEW_MARKS'], skipped['PATH_BAD_DECIMALS'], skipped['PATH_FEATURES_INCOMPLETE']), (1, 1, 2))

    def test_marks_without_a_start_cannot_be_replayed(self):
        store = build_paths(self.dir, [('M', T0, [1, 1, 1], {})], starts=False)
        store.close()
        c = ro(store.path)
        paths, skipped = R.load_paths(c)
        c.close()
        self.assertEqual((paths, skipped), ([], {'MARKS_WITHOUT_START': 1}))

    def test_marks_before_start_and_duplicate_ts(self):
        paths, skipped = self.with_store([('M', T0, [1, 1, 1], {})], lambda s: s.add(marks=[mark_row('M', T0 - 50, 0.5), mark_row('M', T0 + 15, 9)]))
        self.assertEqual((skipped['MARK_BEFORE_START'], skipped['MARK_DUPLICATE_TS']), (1, 1))
        self.assertEqual(paths[0].points[1].quote_raw, QUOTE0)  # the first mark at a timestamp wins

    def test_a_path_carried_into_a_newer_file_keeps_its_first_start_and_merges_marks(self):
        old = build_paths(self.dir, [('M', T0, [1, 1, 1], {})], name='old.sqlite')
        old.close()
        new = build_paths(self.dir, [('M', T0 + 5, [1], {'features': dict(FEATS, market_cap_usd='1')})], name='new.sqlite')
        new.add(marks=[mark_row('M', T0 + 60, 2), mark_row('M', T0 + 75, 2)])
        new.close()
        conns = [ro(old.path), ro(new.path)]
        for c in conns:
            self.addCleanup(c.close)
        paths, skipped = R.load_paths(conns)
        self.assertEqual(skipped['START_DUPLICATE'], 1)
        self.assertEqual(paths[0].start_ts, T0)
        self.assertEqual([p.ts for p in paths[0].points], [T0, T0 + 5, T0 + 15, T0 + 30, T0 + 60, T0 + 75])
        self.assertNotEqual(paths[0].features.get('market_cap_usd'), '1')

    def test_since_until_window(self):
        paths, _ = self.load([('A', T0, [1, 1], {}), ('B', T0 + 1000, [1, 1], {}), ('C', T0 + 2000, [1, 1], {})], since=T0 + 500, until=T0 + 1500)
        self.assertEqual([p.mint for p in paths], ['B'])

    def test_unreadable_start_rows(self):
        def extra(store):
            row = start_row('Z', T0)
            row['features'] = '{"x": '
            store.insert_path(row)
            row = start_row('Y', T0)
            row['target'] = '[]'
            store.insert_path(row)
            store.add(marks=[mark_row('Z', T0, 1), mark_row('Y', T0, 1)])
        _, skipped = self.with_store([('A', T0, [1, 1], {})], extra)
        self.assertEqual(skipped.get('START_UNREADABLE'), 2)

    def test_open_paths_reads_a_live_recorder_file_without_modifying_the_data_file(self):
        import hashlib
        store = build_paths(self.dir, [('A', T0, [1, 1, 1], {})])
        digest = lambda: hashlib.sha256(Path(store.path).read_bytes()).hexdigest()
        store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        before = digest()
        (conn,) = R.open_paths(store.path)
        paths, _ = R.load_paths(conn)
        conn.close()
        store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        store.close()
        self.assertEqual(len(paths), 1)
        self.assertEqual(before, digest())


class CalibrationTests(Tmp):
    c = cfg()
    n = 0

    def live(self, store, mint, reasons, *, close=True):
        """The live trader's footprint in L09's layout: typed buy (+ a closing sell) fills and decisions kind='exit'."""
        pc = paper.PaperConfig()
        buy_fill = paper.buy(paper.Quote(mint, 'buy', 100_000_000, 99_000_000, 6, float(T0)), '0.1', pc)
        store.add_fill(buy_fill)
        held = store.positions()[mint]
        if close:
            store.add_fill(paper.sell(held, paper.Quote(mint, 'sell', held.qty_raw, 90_000_000, 6, float(T0 + 60)), 1, pc))
        for reason in reasons:
            store.add_decision('exit', 'SELL', mint=mint, reasons=[reason], features={})

    def run_cal(self, specs, live, *, close=True):
        CalibrationTests.n += 1
        d = self.dir / ('k%d' % CalibrationTests.n)
        d.mkdir()
        paths = build_paths(d, specs)
        paths.close()
        store = Store(d / 'lean.sqlite', initial_cash_sol=5, code_version=CV, strategy_version=SV, clock=lambda: float(T0))
        for mint, reasons in live:
            self.live(store, mint, reasons, close=close)
        store.close()
        lc, pc = ro(store.path), ro(paths.path)
        self.addCleanup(lc.close)
        self.addCleanup(pc.close)
        return R.calibrate(lc, [pc], self.c)

    def test_reproduces_live_exit_reasons(self):
        cal = self.run_cal([('A', T0, RALLY_THEN_FADE, {}), ('B', T0 + 600, CRASH, {})],
                           [('A', ['TAKE_PROFIT', 'TAKE_PROFIT', 'TAKE_PROFIT', 'STOP']), ('B', ['STOP'])])
        self.assertEqual((cal['status'], cal['matched'], cal['tokens']), ('OK', 2, 2), cal['rows'])

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
        cal = self.run_cal([('A', T0, CRASH, {'features': dict(FEATS, supply_raw=str(10 ** 12))})], [('A', ['STOP'])])  # market cap far below the floor
        self.assertEqual(cal['rows'][0]['note'], 'NOT_ENTERED_IN_REPLAY')
        self.assertEqual(cal['status'], 'MISMATCH')

    def test_extra_or_missing_sells_do_not_match(self):
        for live in (['STOP', 'STOP'], ['TAKE_PROFIT'], []):
            cal = self.run_cal([('A', T0, CRASH, {})], [('A', live)])
            self.assertEqual(cal['status'], 'MISMATCH', live)

    def test_no_live_fills(self):
        d = self.dir / 'nf'
        d.mkdir()
        paths = build_paths(d, [('A', T0, CRASH, {})])
        paths.close()
        store = Store(d / 'lean.sqlite', initial_cash_sol=5, code_version=CV, strategy_version=SV, clock=lambda: float(T0))
        store.close()
        lc, pc = ro(store.path), ro(paths.path)
        self.addCleanup(lc.close)
        self.addCleanup(pc.close)
        self.assertEqual(R.calibrate(lc, [pc], self.c)['status'], 'NO_LIVE_FILLS')

    def test_open_live_position_matches_as_prefix_of_replay(self):
        cal = self.run_cal([('A', T0, RALLY_THEN_FADE, {})], [('A', ['TAKE_PROFIT'])], close=False)
        self.assertEqual(cal['status'], 'OK')
        self.assertEqual(cal['rows'][0]['replay_reasons'], ['TAKE_PROFIT', 'TAKE_PROFIT', 'TAKE_PROFIT', 'STOP'])


if __name__ == '__main__':
    unittest.main()
