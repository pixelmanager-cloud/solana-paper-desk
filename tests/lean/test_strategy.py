"""Known-answer and adversarial tests for lean/strategy.py. Fixtures only, no network.

Each exit/entry case names the desk test or engine line whose behaviour it mirrors, because lean must keep the
current paper experiment's semantics: tests/test_desk.py (ladder/time stop/cooldown), tests/test_held_watcher.py
(trailing arithmetic), desk/engine.py (priority order, cost formula).
"""
import copy
import json
import random
import unittest
from decimal import Decimal as D
from pathlib import Path

from lean import strategy as S

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / "config" / "lean" / "strategy-default.json"
T0 = 1_000_000


def cfg(**over):
    raw = json.loads(DEFAULT.read_text())
    raw.update(over)
    return S.StrategyConfig.from_dict(raw)


def pos(**kw):
    base = dict(mint="MINT", opened_at=T0, qty=D(1000), initial_qty=D(1000), cost_left=D("0.1"), stop_ratio=D("0.82"))
    base.update(kw)
    return S.Position(**base)


AUTO = -12345  # replaced by now-1 in ExitTests.decide; direct exit_decision calls pass an explicit time


def sellq(qty, out="0.1", at=AUTO):
    return S.Quote("sell", D(qty), D(out), at)


def port(**kw):
    base = dict(now=T0 + 7200, equity=D(5), cash=D(5), exposure=D(0), day_start_equity=D(5))
    base.update(kw)
    return S.Portfolio(**base)


FEATURES = {"mint": "MINT", "market_cap_usd": "100000", "liquidity_usd": "20000", "reserve_sol": "100"}


def roundtrip(now, spent="0.1", tokens="1000", back="0.099", buy_at=None, sell_at=None):
    buy = S.Quote("buy", D(spent), D(tokens), now - 1 if buy_at is None else buy_at)
    sell = S.Quote("sell", D(tokens) * D("0.995"), D(back), now - 1 if sell_at is None else sell_at)
    return S.RoundTrip(buy, sell)


class ConfigTests(unittest.TestCase):
    def test_default_matches_current_paper_experiment(self):
        paper = json.loads((ROOT / "config" / "paper.json").read_text())
        c = S.StrategyConfig.load(DEFAULT)
        for key in ("stop_fraction", "trailing_fraction", "max_roundtrip_cost_fraction", "fixed_fee_sol",
                    "adverse_slippage_bps", "min_market_cap_usd", "max_market_cap_usd", "min_liquidity_usd",
                    "max_position_fraction", "max_exposure_fraction", "liquidity_fraction", "min_order_sol",
                    "fee_reserve_sol", "daily_pause_fraction"):
            ours = "daily_loss_stop_fraction" if key == "daily_pause_fraction" else key
            self.assertEqual(getattr(c, ours), D(paper[key]), key)
        for key in ("time_stop_seconds", "max_hold_seconds", "cooldown_seconds", "stop_cooldown_seconds",
                    "max_positions", "price_ttl_seconds"):
            self.assertEqual(getattr(c, key), paper[key], key)
        self.assertEqual(c.daily_liquidate_fraction, D(paper["daily_liquidate_fraction"]))
        self.assertEqual([(r.trigger, r.fraction, r.stop_ratio) for r in c.tp_ladder],
                         [(D("1.4"), D("0.3"), D(1)), (D(2), D("0.3"), D("1.4")), (D(3), D("0.2"), D(2))])
        self.assertEqual(c.initial_stop_ratio, D("0.82"))

    def test_hash_is_stable_and_sensitive(self):
        self.assertEqual(cfg().config_hash, cfg().config_hash)
        self.assertNotEqual(cfg().config_hash, cfg(stop_fraction="0.2").config_hash)

    def test_unknown_missing_and_malformed_values_fail_closed(self):
        raw = json.loads(DEFAULT.read_text())
        bad = []
        extra = dict(raw, stopp_fraction="0.1"); bad.append(extra)
        missing = dict(raw); del missing["stop_fraction"]; bad.append(missing)
        for key, value in [("stop_fraction", "NaN"), ("stop_fraction", "Infinity"), ("stop_fraction", True),
                           ("stop_fraction", None), ("stop_fraction", "0"), ("stop_fraction", "1"),
                           ("stop_fraction", "abc"), ("max_positions", "4"), ("max_positions", 0),
                           ("max_positions", 4.0), ("max_positions", True), ("time_stop_seconds", False), ("min_order_sol", "0"), ("adverse_slippage_bps", "10000"),
                           ("daily_liquidate_fraction", "0.01"), ("touched_ratio", "1"),
                           ("max_position_fraction", "0.5"), ("strategy_version", ""),
                           ("min_market_cap_usd", "3000000"), ("cooldown_seconds", -1)]:
            bad.append(dict(raw, **{key: value}))
        ladder = copy.deepcopy(raw["tp_ladder"])
        for mutate in (lambda l: l.pop(), lambda l: l[0].update(trigger="0.9"), lambda l: l[1].update(trigger="1.3"),
                       lambda l: l[2].update(stop_ratio="3"), lambda l: l[0].update(fraction="0.9"),
                       lambda l: l[0].pop("stop_ratio")):
            l = copy.deepcopy(ladder); mutate(l); bad.append(dict(raw, tp_ladder=l))
        bad.append([]); bad.append(None)
        for item in bad:
            with self.assertRaises(S.ConfigError, msg=repr(item)[:120]):
                S.StrategyConfig.from_dict(item)


class ExitTests(unittest.TestCase):
    c = cfg()

    def decide(self, ratio, position=None, quote=None, now=T0 + 100, **kw):
        position = position or pos()
        if quote is not None and quote.observed_at == AUTO:
            quote = S.Quote(quote.side, quote.in_amount, quote.out_amount, now - 1, quote.route_available)
        kw.setdefault("mark_at", now)
        return S.exit_decision(position, position.cost_left * D(ratio), quote, now, self.c, **kw)

    def test_stop_boundary_inclusive(self):
        # desk: ratio <= stop_ratio (0.82)
        self.assertEqual(self.decide("0.82").reason, "STOP")
        self.assertEqual(self.decide("0.8201").action, "HOLD")
        d = self.decide("0.5", quote=sellq(1000))
        self.assertEqual((d.action, d.reason, d.qty, d.fraction), ("SELL", "STOP", D(1000), D(1)))

    def test_take_profit_rung_one_sells_thirty_percent_of_initial_and_moves_stop(self):
        d = self.decide("1.4", quote=sellq(300))
        self.assertEqual((d.action, d.reason, d.qty), ("SELL", "TAKE_PROFIT", D(300)))
        self.assertEqual(d.after_fill, {"stage": 1, "stop_ratio": D(1)})
        self.assertEqual(self.decide("1.3999").action, "HOLD")

    def test_one_rung_per_observation_even_if_price_jumped_past_several(self):
        # desk comment: "One rung per observed quote. Requote on the next event."
        d = self.decide("3.5", quote=sellq(300))
        self.assertEqual((d.reason, d.qty, d.after_fill["stage"]), ("TAKE_PROFIT", D(300), 1))

    def test_ladder_sequence_matches_desk_ladder_test(self):
        # tests/test_desk.py ladder: three rungs leave 20% of the initial quantity, then it is liquidated.
        p, sold = pos(), D(0)
        for ratio, stage in (("1.4", 0), ("2", 1), ("3", 2)):
            p = S.Position(**{**p.__dict__, "stage": stage, "stop_ratio": self.c.tp_ladder[stage - 1].stop_ratio if stage else p.stop_ratio})
            d = S.exit_decision(p, p.cost_left * D(ratio), sellq(p.initial_qty * self.c.tp_ladder[stage].fraction, at=T0 + 49), T0 + 50, self.c, mark_at=T0 + 50)
            self.assertEqual(d.reason, "TAKE_PROFIT")
            sold += d.qty
            p = S.Position(**{**p.__dict__, "qty": p.qty - d.qty, "cost_left": p.cost_left * (p.qty - d.qty) / p.qty})
        self.assertEqual(p.qty, D(200))
        self.assertEqual(sold, D(800))

    def test_stop_ratio_after_rung_protects_profit(self):
        p = pos(stage=1, stop_ratio=D(1), qty=D(700))
        self.assertEqual(S.exit_decision(p, p.cost_left * D("0.99"), None, T0 + 60, self.c, mark_at=T0 + 60).reason, "STOP")

    def test_trailing_only_after_all_rungs_and_uses_peak(self):
        # tests/test_held_watcher.py: stage 3, peak 3, stop 1.4: 2.0 <= 3*0.7 trails, 2.2 holds.
        p = pos(stage=3, peak_ratio=D(3), stop_ratio=D("1.4"))
        self.assertEqual(self.decide("2.0", p).reason, "TRAILING_STOP")
        self.assertEqual(self.decide("2.1", p).reason, "TRAILING_STOP")  # <= boundary (2.1 == 3*0.7)
        self.assertEqual(self.decide("2.2", p).action, "HOLD")
        early = pos(stage=2, peak_ratio=D(3), stop_ratio=D("1.4"))
        self.assertEqual(self.decide("2.0", early).action, "HOLD")  # not armed before the last rung

    def test_peak_update_precedes_the_trailing_test_and_is_persisted_on_hold(self):
        p = pos(stage=3, peak_ratio=D(3), stop_ratio=D("1.4"))
        d = self.decide("4", p)
        self.assertEqual((d.action, d.position_updates), ("HOLD", {"peak_ratio": D(4), "touched_15": True}))
        # The new peak raises the trailing line within the same observation: 4.0 -> line 2.8; a 3.0 mark holds,
        self.assertEqual(self.decide("3", pos(stage=3, peak_ratio=D(4), stop_ratio=D("1.4"))).action, "HOLD")
        # 2.7 trails.
        self.assertEqual(self.decide("2.7", pos(stage=3, peak_ratio=D(4), stop_ratio=D("1.4"))).reason, "TRAILING_STOP")

    def test_max_hold_and_time_stop(self):
        # tests/test_desk.py::test_time_stop_does_not_need_new_entry_signal: TIME_STOP exactly at 2700 s.
        self.assertEqual(self.decide("1.0", now=T0 + 2699).action, "HOLD")
        self.assertEqual(self.decide("1.0", now=T0 + 2700).reason, "TIME_STOP")
        self.assertEqual(self.decide("1.0", pos(touched_15=True), now=T0 + 2700).action, "HOLD")
        self.assertEqual(self.decide("1.0", pos(touched_15=True), now=T0 + 21599).action, "HOLD")
        self.assertEqual(self.decide("1.0", pos(touched_15=True), now=T0 + 21600).reason, "MAX_HOLD")
        self.assertEqual(self.decide("1.0", now=T0 + 21600).reason, "MAX_HOLD")  # max hold outranks time stop

    def test_touching_115_this_observation_disarms_time_stop(self):
        d = self.decide("1.15", now=T0 + 2700)
        self.assertEqual(d.action, "HOLD")
        self.assertEqual(d.position_updates["touched_15"], True)

    def test_priority_liquidate_danger_stop(self):
        q = sellq(1000)
        self.assertEqual(self.decide("0.5", quote=q, liquidate=True, danger=True).reason, "LIQUIDATE")
        self.assertEqual(self.decide("0.5", quote=q, danger=True).reason, "DANGER")
        self.assertEqual(self.decide("0.5", pos(stage=3, peak_ratio=D(3)), quote=q).reason, "STOP")  # stop before trailing

    def test_forced_exit_does_not_wait_for_a_mark(self):
        for kw in ({"liquidate": True}, {"danger": True}):
            d = S.exit_decision(pos(), None, sellq(1000, at=T0 + 9), T0 + 10, self.c, mark_at=None, **kw)
            self.assertEqual((d.action, d.qty), ("SELL", D(1000)))

    def test_missing_stale_or_garbage_mark_blocks_never_sells_or_holds_silently(self):
        for mark, at in ((None, None), (D("NaN"), None), (D("Infinity"), None), (D("-1"), None), (D("0.1"), T0 - 1000),
                         (D("0.1"), T0 + 1000)):
            d = S.exit_decision(pos(), mark, sellq(1000, at=T0 + 99), T0 + 100, self.c, mark_at=at)
            self.assertEqual((d.action, d.reason), ("BLOCKED", "STALE_OR_UNAVAILABLE_MARK"), (mark, at))
        d = S.exit_decision(pos(cost_left=D(0)), D(1), None, T0 + 100, self.c, mark_at=T0 + 100)
        self.assertEqual(d.action, "BLOCKED")
        # L09: a mark without a read time is never fresh, and mark_at is a required argument
        d = S.exit_decision(pos(), D("0.05"), None, T0 + 100, self.c, mark_at=None)
        self.assertEqual(d.reason, "STALE_OR_UNAVAILABLE_MARK")
        with self.assertRaises(TypeError):
            S.exit_decision(pos(), D("0.05"), None, T0 + 100, self.c)

    def test_fresh_mark_at_ttl_boundary_ok(self):
        d = S.exit_decision(pos(), D("0.05"), None, T0 + 100, self.c, mark_at=T0 + 90)
        self.assertEqual(d.reason, "STOP")
        d = S.exit_decision(pos(), D("0.05"), None, T0 + 100, self.c, mark_at=T0 + 89)
        self.assertEqual(d.action, "BLOCKED")

    def test_sell_without_quote_asks_for_one(self):
        d = self.decide("0.5")
        self.assertEqual((d.action, d.quote_required, d.qty), ("SELL", True, D(1000)))

    def test_bad_sell_quotes_block_and_remember_the_wanted_exit(self):
        stale = sellq(1000, at=T0 + 100 - 11)
        wrong_size = sellq(999)
        no_route = S.Quote("sell", D(1000), D("0.1"), T0 + 99, route_available=False)
        zero_out = sellq(1000, out="0")
        dust = sellq(1000, out="0.00005")  # nets to <= 0 after slippage and fee
        wrong_side = S.Quote("buy", D(1000), D("0.1"), T0 + 99)
        for q, why in ((stale, "SELL_QUOTE_STALE"), (wrong_size, "SELL_QUOTE_SIZE_MISMATCH"), (no_route, "NO_ROUTE"),
                       (zero_out, "SELL_QUOTE_INVALID"), (dust, "STUCK_POSITION"), (wrong_side, "SELL_QUOTE_MISSING")):
            d = self.decide("0.5", quote=q)
            self.assertEqual((d.action, d.reason), ("BLOCKED", why), why)
            self.assertIn("STOP", d.reasons)

    def test_blocked_exit_still_persists_new_peak(self):
        p = pos(stage=3, peak_ratio=D(3), stop_ratio=D("1.4"))
        d = self.decide("4.5", p)  # holds, peak -> 4.5
        self.assertEqual(d.position_updates["peak_ratio"], D("4.5"))

    def test_take_profit_clamps_to_remaining_quantity(self):
        p = pos(qty=D(100), initial_qty=D(1000), stage=2, stop_ratio=D("1.4"))
        d = S.exit_decision(p, p.cost_left * D(3), sellq(100, at=T0 + 59), T0 + 60, self.c, mark_at=T0 + 60)
        self.assertEqual((d.reason, d.qty), ("TAKE_PROFIT", D(100)))

    def test_property_random_walks_never_oversell_or_regress(self):
        rnd = random.Random(20261011)
        for _ in range(300):
            p = pos()
            now = T0
            for _ in range(60):
                now += rnd.randint(1, 900)
                ratio = D(str(round(rnd.uniform(0.3, 4.5), 3)))
                d = S.exit_decision(p, p.cost_left * ratio, sellq(1, at=now), now, self.c, mark_at=now)
                if d.action == "SELL" or (d.action == "BLOCKED" and d.qty > 0):
                    self.assertTrue(D(0) < d.qty <= p.qty)
                if d.action == "SELL":
                    pre = S.exit_decision(p, p.cost_left * ratio, None, now, self.c, mark_at=now)
                    q = sellq(pre.qty, at=now)
                    d = S.exit_decision(p, p.cost_left * ratio, q, now, self.c, mark_at=now)
                    if d.action != "SELL":
                        continue
                    left = p.qty - d.qty
                    if left <= D("1e-20"):
                        break
                    new = {**p.__dict__, "qty": left, "cost_left": p.cost_left * left / p.qty}
                    new.update(d.position_updates); new.update(d.after_fill)
                    self.assertGreaterEqual(new.get("stage", 0), p.stage)
                    p = S.Position(**new)
                else:
                    p = S.Position(**{**p.__dict__, **d.position_updates})
                self.assertGreaterEqual(p.peak_ratio, D(1))
                self.assertLessEqual(p.stage, 3)


class EntryTests(unittest.TestCase):
    c = cfg()

    def decide(self, features=None, quote="auto", portfolio=None):
        portfolio = portfolio or port()
        features = FEATURES if features is None else features
        if quote == "auto":
            quote = roundtrip(portfolio.now)
        return S.entry_decision(features, quote, portfolio, self.c)

    def test_size_is_two_percent_of_equity_then_confirmed_by_quote(self):
        pre = self.decide(quote=None)
        self.assertEqual((pre.action, pre.quote_required, pre.size_sol), ("BUY", True, D("0.1")))
        d = self.decide()
        self.assertEqual((d.action, d.reason, d.size_sol), ("BUY", "ENTRY", D("0.1")))
        # cost = (0.1 - 0.099*0.995 + 5*0.00005) / 0.1
        self.assertEqual(D(d.detail["estimated_cost_fraction"]), (D("0.1") - D("0.099") * D("0.995") + D("0.00025")) / D("0.1"))

    def test_size_limits_each_bind(self):
        size = lambda p=None, f=None: S.entry_size(f or FEATURES, p or port(), self.c)[0]
        self.assertEqual(size(f=dict(FEATURES, reserve_sol="1")), D("0.03"))  # 0.015*2*1
        self.assertEqual(size(port(exposure=D("0.35"))), D("0.05"))  # 0.08*5 - 0.35
        self.assertEqual(size(port(cash=D("0.4"))), D("0.09995"))  # cash - 0.3 reserve - 0.00005 fee
        self.assertEqual(size(port(loss_streak=4)), D("0.05"))  # halved
        self.assertEqual(size(port(equity=D("5.33333333333"))), D("0.106666666"))  # rounded DOWN to 1e-9

    def test_below_minimum_and_no_room(self):
        self.assertEqual(self.decide(portfolio=port(exposure=D("0.395"))).reasons, ("BELOW_MINIMUM",))
        self.assertEqual(self.decide(portfolio=port(cash=D("0.3"))).reasons, ("BELOW_MINIMUM",))

    def test_portfolio_caps(self):
        full = frozenset({"A", "B", "C", "D"})
        self.assertIn("MAX_POSITIONS", self.decide(portfolio=port(open_mints=full)).reasons)
        three = frozenset({"A", "B", "C"})
        self.assertEqual(self.decide(portfolio=port(open_mints=three, exposure=D("0.1"))).action, "BUY")
        self.assertIn("ALREADY_HELD", self.decide(portfolio=port(open_mints=frozenset({"MINT"}))).reasons)

    def test_cooldown_and_throttle(self):
        # tests/test_desk.py::test_cooldown_and_out_of_order: re-entry refused while cooling down.
        now = port().now
        self.assertIn("COOLDOWN", self.decide(portfolio=port(cooldowns={"MINT": now + 1})).reasons)
        self.assertEqual(self.decide(portfolio=port(cooldowns={"MINT": now})).action, "BUY")
        self.assertEqual(self.decide(portfolio=port(cooldowns={"OTHER": now + 99})).action, "BUY")
        self.assertIn("ENTRY_THROTTLE", self.decide(portfolio=port(last_entry_minute=now // 60)).reasons)
        self.assertEqual(self.decide(portfolio=port(last_entry_minute=now // 60 - 1)).action, "BUY")

    def test_daily_loss_stop_and_liquidation_thresholds(self):
        stop = lambda eq: self.decide(quote=None, portfolio=port(equity=D(eq)))  # pre-quote gate: sizing shrinks with equity
        self.assertEqual(stop("4.7001").action, "BUY")
        self.assertIn("DAILY_LOSS_STOP", stop("4.7").reasons)  # 6% of 5 = 0.3, inclusive
        self.assertFalse(S.should_liquidate(port(equity=D("4.5001")), self.c))
        self.assertTrue(S.should_liquidate(port(equity=D("4.5")), self.c))
        self.assertFalse(S.should_liquidate(port(equity=D(6)), self.c))  # gains never liquidate

    def test_loss_streak_pause(self):
        self.assertEqual(self.decide(quote=None, portfolio=port(loss_streak=5)).action, "BUY")
        self.assertIn("LOSS_STREAK_PAUSE", self.decide(portfolio=port(loss_streak=6)).reasons)

    def test_invalid_portfolio_fails_closed(self):
        for p in (port(equity=D(0)), port(day_start_equity=D(0)), port(equity=D(-1))):
            self.assertIn("PORTFOLIO_INVALID", self.decide(portfolio=p).reasons)

    def test_market_cap_and_liquidity_bands_inclusive(self):
        f = lambda **kw: dict(FEATURES, **kw)
        self.assertEqual(self.decide(f(market_cap_usd="50000")).action, "BUY")
        self.assertEqual(self.decide(f(market_cap_usd="2000000")).action, "BUY")
        self.assertEqual(self.decide(f(market_cap_usd="49999.99")).reasons, ("MARKET_CAP",))
        self.assertEqual(self.decide(f(market_cap_usd="2000000.01")).reasons, ("MARKET_CAP",))
        self.assertEqual(self.decide(f(liquidity_usd="8000")).action, "BUY")
        self.assertEqual(self.decide(f(liquidity_usd="7999.99")).reasons, ("LIQUIDITY",))

    def test_missing_or_corrupt_evidence_rejects(self):
        for key in ("market_cap_usd", "liquidity_usd"):
            for bad in (None, "NaN", "Infinity", "-5", "abc", True, [], {}):
                feats = dict(FEATURES, **{key: bad})
                d = self.decide(feats)
                self.assertEqual(d.action, "SKIP", (key, bad))
                self.assertTrue(d.reason.endswith("_UNKNOWN"), (key, bad, d.reasons))
            feats = dict(FEATURES); del feats[key]
            self.assertEqual(self.decide(feats).action, "SKIP")
        for mint in (None, "", 5):
            self.assertIn("MINT_MISSING", self.decide(dict(FEATURES, mint=mint)).reasons)

    def test_garbage_reserve_cannot_remove_the_liquidity_cap(self):
        # A malformed reserve is ignored (no cap added) but the 2% capital cap still bounds the size.
        d = self.decide(dict(FEATURES, reserve_sol="NaN"))
        self.assertEqual(d.size_sol, D("0.1"))

    def test_cost_budget_boundary(self):
        # cost = (0.1 - back*0.995 + 0.00025)/0.1 ; back that yields exactly 8%: 0.1*(0.92)=... solve
        back = (D("0.1") + D("0.00025") - D("0.008")) / D("0.995")
        ok = self.decide(quote=roundtrip(port().now, back=str(back.quantize(D("1e-12"), rounding="ROUND_CEILING"))))
        self.assertEqual(ok.action, "BUY")
        bad = self.decide(quote=roundtrip(port().now, back=str(back - D("0.0001"))))
        self.assertEqual((bad.action, bad.reason), ("SKIP", "COST_BUDGET"))

    def test_cost_cap_is_inclusive_at_exactly_eight_percent(self):
        # zero slippage makes the arithmetic exact: (0.1 - 0.09225 + 5*0.00005) / 0.1 == 0.08
        c0 = cfg(adverse_slippage_bps="0")
        now = port().now
        exact = S.RoundTrip(S.Quote("buy", D("0.1"), D(1000), now - 1), S.Quote("sell", D(1000), D("0.09225"), now - 1))
        self.assertEqual(S.roundtrip_cost_fraction(exact, c0), D("0.08"))
        self.assertEqual(S.entry_decision(FEATURES, exact, port(), c0).action, "BUY")
        worse = S.RoundTrip(exact.buy, S.Quote("sell", D(1000), D("0.09224"), now - 1))
        self.assertEqual(S.entry_decision(FEATURES, worse, port(), c0).reason, "COST_BUDGET")

    def test_quote_evidence_must_be_fresh_sized_and_consistent(self):
        now = port().now
        cases = {
            "BUY_QUOTE_STALE": roundtrip(now, buy_at=now - 11),
            "SELL_QUOTE_STALE": roundtrip(now, sell_at=now - 11),
            "BUY_QUOTE_INVALID": roundtrip(now, tokens="0"),
            "QUOTE_EXCEEDS_RISK_ALLOCATION": roundtrip(now, spent="0.2"),
            "SELL_QUOTE_EXCEEDS_BOUGHT": S.RoundTrip(roundtrip(now).buy, S.Quote("sell", D(2000), D("0.1"), now - 1)),
            "ROUNDTRIP_QUOTE_MISSING": "not a quote",
        }
        cases["BUY_QUOTE_STALE"].__class__  # keep linters quiet
        for why, rt in cases.items():
            d = self.decide(quote=rt)
            self.assertEqual((d.action, d.reason), ("SKIP", why), why)
        future = roundtrip(now, buy_at=now + 5)
        self.assertEqual(self.decide(quote=future).reason, "BUY_QUOTE_STALE")  # a quote from the future is not fresh
        below_min = roundtrip(now, spent="0.005")
        self.assertEqual(self.decide(quote=below_min).reason, "QUOTE_EXCEEDS_RISK_ALLOCATION")
        no_route = S.RoundTrip(S.Quote("buy", D("0.1"), D(1000), now - 1, route_available=False), roundtrip(now).sell)
        self.assertEqual(self.decide(quote=no_route).reason, "NO_ROUTE")

    def test_buy_uses_the_quoted_input_not_the_planned_size(self):
        d = self.decide(quote=roundtrip(port().now, spent="0.06", tokens="600", back="0.0595"))
        self.assertEqual((d.action, d.size_sol), ("BUY", D("0.06")))

    def test_rejection_reasons_accumulate_for_the_funnel_report(self):
        d = self.decide(dict(FEATURES, market_cap_usd="1", liquidity_usd="1"),
                        portfolio=port(open_mints=frozenset("ABCD"), loss_streak=6))
        self.assertEqual(set(d.reasons), {"MAX_POSITIONS", "LOSS_STREAK_PAUSE", "MARKET_CAP", "LIQUIDITY"})

    def test_roundtrip_sell_leg_is_quoted_for_slippage_adjusted_tokens(self):
        self.assertEqual(S.sell_quote_tokens(S.Quote("buy", D("0.1"), D(1000), T0), self.c), D(995))


class BookkeepingTests(unittest.TestCase):
    c = cfg()

    def test_cooldowns(self):
        self.assertEqual(S.cooldown_until("STOP", 100, self.c), 100 + 7200)
        for r in ("TRAILING_STOP", "TIME_STOP", "MAX_HOLD", "DANGER", "LIQUIDATE", "TAKE_PROFIT"):
            self.assertEqual(S.cooldown_until(r, 100, self.c), 100 + 1800, r)

    def test_loss_streak(self):
        self.assertEqual(S.next_loss_streak(3, D("-0.0001")), 4)
        self.assertEqual(S.next_loss_streak(3, D(0)), 0)  # desk: only strictly negative extends the streak
        self.assertEqual(S.next_loss_streak(3, D("0.01")), 0)

    def test_position_requires_an_explicit_stop_ratio(self):
        with self.assertRaises(TypeError):
            S.Position(mint="M", opened_at=1, qty=D(1), initial_qty=D(1), cost_left=D(1))
        self.assertEqual(S.new_position_fields(cfg(stop_fraction="0.25"))["stop_ratio"], D("0.75"))

    def test_new_position_fields(self):
        self.assertEqual(S.new_position_fields(self.c),
                         {"stage": 0, "stop_ratio": D("0.82"), "peak_ratio": D(1), "touched_15": False})

    def test_decisions_are_pure(self):
        p = pos(); before = copy.deepcopy(p.__dict__)
        a = S.exit_decision(p, D("0.05"), None, T0 + 5, self.c, mark_at=T0 + 5)
        b = S.exit_decision(p, D("0.05"), None, T0 + 5, self.c, mark_at=T0 + 5)
        self.assertEqual(a, b)
        self.assertEqual(p.__dict__, before)


class L09IntegrationTests(unittest.TestCase):
    """Review fixes: exact sell leg, whole-unit TP quantities, required mark_at, UTC-day loss baseline."""
    c = cfg()

    def test_sell_leg_must_be_for_exactly_the_tokens_the_fill_will_hold(self):
        now = port().now
        rt = roundtrip(now)
        self.assertEqual(rt.sell.in_amount, S.sell_quote_tokens(rt.buy, self.c))
        self.assertEqual(S.entry_decision(FEATURES, rt, port(), self.c).action, "BUY")
        # a small sell leg priced at a good rate would hide the price impact of selling the whole position
        small = S.RoundTrip(rt.buy, S.Quote("sell", D(10), D("0.00099"), now - 1))
        d = S.entry_decision(FEATURES, small, port(), self.c)
        self.assertEqual((d.action, d.reason), ("SKIP", "SELL_QUOTE_SIZE_MISMATCH"))

    def test_sell_quote_tokens_is_floored_to_a_raw_unit(self):
        self.assertEqual(S.sell_quote_tokens(S.Quote("buy", D("0.1"), D(1001), T0), self.c), D(995))   # 995.995 -> 995

    def test_tp_quantity_is_a_whole_raw_unit(self):
        p = pos(qty=D(1001), initial_qty=D(1001))
        d = S.exit_decision(p, p.cost_left * D("1.5"), None, T0 + 10, self.c, mark_at=T0 + 10)
        self.assertEqual((d.reason, d.qty), ("TAKE_PROFIT", D(300)))                 # floor(1001 * 0.3)
        d = S.exit_decision(p, p.cost_left * D("1.5"), sellq(300, at=T0 + 9), T0 + 10, self.c, mark_at=T0 + 10)
        self.assertEqual(d.action, "SELL")

    def test_day_start_resets_at_utc_midnight_only_with_fresh_marks(self):
        day = 20_000 * 86400
        first = S.roll_day_start(None, day + 10, D(5), marks_fresh=False)
        self.assertEqual(first, (20_000, D(5)))
        self.assertEqual(S.roll_day_start(first, day + 86399, D(4), marks_fresh=True), first)      # same UTC day
        self.assertEqual(S.roll_day_start(first, day + 86400, D(4), marks_fresh=False), first)     # deferred: stale marks
        self.assertEqual(S.roll_day_start(first, day + 86410, D(4), marks_fresh=True), (20_001, D(4)))


if __name__ == "__main__":
    unittest.main()
