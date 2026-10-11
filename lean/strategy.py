"""Pure entry/exit/portfolio decisions for the lean paper trader.

No I/O, no clock, no randomness: every function takes its inputs explicitly so decisions are replayable.
Semantics follow desk/engine.py (see tests/lean/test_strategy.py for the desk test each case mirrors), simplified:
no entry score (screening is lean.candidates' job), no per-trade/daily-risk sizing limits, no latches.

Money is SOL as Decimal. Marks are the NET value (fee already deducted) of the whole remaining position.
Slippage: quotes are raw route quotes; `adverse_slippage_bps` is applied here ONLY to estimate round-trip cost and
by lean.paper when filling. Never apply it twice to the same number.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Mapping, Optional

ZERO, ONE = Decimal(0), Decimal(1)
SATOSHI = Decimal("0.000000001")
LADDER_STAGES = 3  # stage >= LADDER_STAGES arms the trailing stop (desk: p["stage"] >= 3)


class ConfigError(ValueError):
    pass


def _dec(value, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ConfigError(f"{name} must be a number")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ConfigError(f"{name} must be a number") from None
    if not d.is_finite():
        raise ConfigError(f"{name} must be finite")
    return d


def _int(value, name: str) -> int:
    if type(value) is not int:
        raise ConfigError(f"{name} must be an integer")
    return value


@dataclass(frozen=True)
class Rung:
    trigger: Decimal
    fraction: Decimal  # of the INITIAL quantity
    stop_ratio: Decimal  # new stop (cost ratio) once the rung has filled


@dataclass(frozen=True)
class StrategyConfig:
    strategy_version: str
    min_market_cap_usd: Decimal
    max_market_cap_usd: Decimal
    min_liquidity_usd: Decimal
    max_roundtrip_cost_fraction: Decimal
    fixed_fee_sol: Decimal
    adverse_slippage_bps: Decimal
    price_ttl_seconds: int
    stop_fraction: Decimal
    trailing_fraction: Decimal
    time_stop_seconds: int
    max_hold_seconds: int
    touched_ratio: Decimal
    tp_ladder: tuple
    cooldown_seconds: int
    stop_cooldown_seconds: int
    max_positions: int
    max_position_fraction: Decimal
    max_exposure_fraction: Decimal
    liquidity_fraction: Decimal
    min_order_sol: Decimal
    fee_reserve_sol: Decimal
    daily_loss_stop_fraction: Decimal
    daily_liquidate_fraction: Decimal
    loss_streak_halve_after: int
    loss_streak_pause_at: int
    config_hash: str = field(default="", compare=False)

    _DECIMALS = ("min_market_cap_usd", "max_market_cap_usd", "min_liquidity_usd", "max_roundtrip_cost_fraction",
                 "fixed_fee_sol", "adverse_slippage_bps", "stop_fraction", "trailing_fraction", "touched_ratio",
                 "max_position_fraction", "max_exposure_fraction", "liquidity_fraction", "min_order_sol",
                 "fee_reserve_sol", "daily_loss_stop_fraction", "daily_liquidate_fraction")
    _INTS = ("price_ttl_seconds", "time_stop_seconds", "max_hold_seconds", "cooldown_seconds",
             "stop_cooldown_seconds", "max_positions", "loss_streak_halve_after", "loss_streak_pause_at")

    @classmethod
    def from_dict(cls, raw: Mapping) -> "StrategyConfig":
        """Strict: unknown or missing keys, non-finite numbers and out-of-range values are errors (a typo in a
        risk parameter must not silently fall back to a default)."""
        if not isinstance(raw, Mapping):
            raise ConfigError("config must be an object")
        names = {"strategy_version", "tp_ladder", *cls._DECIMALS, *cls._INTS}
        extra, missing = set(raw) - names, names - set(raw)
        if extra or missing:
            raise ConfigError(f"config keys: unknown={sorted(extra)} missing={sorted(missing)}")
        version = raw["strategy_version"]
        if not isinstance(version, str) or not version:
            raise ConfigError("strategy_version must be a non-empty string")
        values = {k: _dec(raw[k], k) for k in cls._DECIMALS}
        values.update({k: _int(raw[k], k) for k in cls._INTS})
        ladder_raw = raw["tp_ladder"]
        if not isinstance(ladder_raw, (list, tuple)) or len(ladder_raw) != LADDER_STAGES:
            raise ConfigError(f"tp_ladder must have exactly {LADDER_STAGES} rungs")
        rungs = []
        for i, r in enumerate(ladder_raw):
            if not isinstance(r, Mapping) or set(r) != {"trigger", "fraction", "stop_ratio"}:
                raise ConfigError(f"tp_ladder[{i}] needs trigger, fraction, stop_ratio")
            rungs.append(Rung(*(_dec(r[k], f"tp_ladder[{i}].{k}") for k in ("trigger", "fraction", "stop_ratio"))))
        cfg = cls(strategy_version=version, tp_ladder=tuple(rungs), **values)
        cfg._validate()
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        object.__setattr__(cfg, "config_hash", digest)
        return cfg

    @classmethod
    def load(cls, path) -> "StrategyConfig":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def _validate(self) -> None:
        def unit(name, lo=ZERO, hi=ONE):
            v = getattr(self, name)
            if not lo < v < hi:
                raise ConfigError(f"{name} must be in ({lo}, {hi})")

        for name in ("stop_fraction", "trailing_fraction", "max_roundtrip_cost_fraction", "max_position_fraction",
                     "max_exposure_fraction", "liquidity_fraction", "daily_loss_stop_fraction",
                     "daily_liquidate_fraction"):
            unit(name)
        if self.daily_liquidate_fraction < self.daily_loss_stop_fraction:
            raise ConfigError("daily_liquidate_fraction must be >= daily_loss_stop_fraction")
        if self.max_position_fraction > self.max_exposure_fraction:
            raise ConfigError("max_position_fraction must be <= max_exposure_fraction")
        if not ZERO < self.min_market_cap_usd <= self.max_market_cap_usd:
            raise ConfigError("market cap bounds invalid")
        for name in ("min_liquidity_usd", "fixed_fee_sol", "min_order_sol", "fee_reserve_sol", "adverse_slippage_bps"):
            if getattr(self, name) < ZERO:
                raise ConfigError(f"{name} must be >= 0")
        if self.min_order_sol <= ZERO or self.adverse_slippage_bps >= 10000:
            raise ConfigError("min_order_sol must be > 0 and slippage < 10000 bps")
        for name in ("price_ttl_seconds", "time_stop_seconds", "max_hold_seconds", "max_positions",
                     "loss_streak_halve_after", "loss_streak_pause_at"):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name} must be >= 1")
        if self.cooldown_seconds < 0 or self.stop_cooldown_seconds < 0:
            raise ConfigError("cooldowns must be >= 0")
        if self.touched_ratio <= ONE:
            raise ConfigError("touched_ratio must be > 1")
        prev = ONE
        fractions = ZERO
        for i, r in enumerate(self.tp_ladder):
            if r.trigger <= prev or not ZERO < r.fraction < ONE or r.stop_ratio < ZERO or r.stop_ratio >= r.trigger:
                raise ConfigError(f"tp_ladder[{i}] invalid (triggers must rise above 1; stop below trigger)")
            prev = r.trigger
            fractions += r.fraction
        if fractions >= ONE:
            raise ConfigError("tp_ladder fractions must leave a remainder for the trailing stop")

    @property
    def initial_stop_ratio(self) -> Decimal:
        return ONE - self.stop_fraction


# ----------------------------------------------------------------------------------------------- inputs

@dataclass(frozen=True)
class Quote:
    """A route quote. `observed_at` is epoch seconds; amounts are SOL for the SOL leg, token units for the token leg."""
    side: str  # "buy" (SOL in, tokens out) or "sell" (tokens in, SOL out)
    in_amount: Decimal
    out_amount: Decimal
    observed_at: int
    route_available: bool = True


@dataclass(frozen=True)
class RoundTrip:
    """Entry evidence: a buy quote and a sell quote for the (slippage-adjusted) tokens that buy would return."""
    buy: Quote
    sell: Quote


@dataclass(frozen=True)
class Portfolio:
    now: int
    equity: Decimal
    cash: Decimal
    exposure: Decimal  # sum of cost_left over open positions
    day_start_equity: Decimal
    open_mints: frozenset = frozenset()
    cooldowns: Mapping = field(default_factory=dict)  # mint -> epoch until which re-entry is refused
    last_entry_minute: int = -1
    loss_streak: int = 0


@dataclass(frozen=True)
class Position:
    mint: str
    opened_at: int
    qty: Decimal
    initial_qty: Decimal
    cost_left: Decimal
    stop_ratio: Decimal  # required: take it from new_position_fields(cfg) so a config change cannot be silently ignored
    stage: int = 0
    peak_ratio: Decimal = ONE
    touched_15: bool = False


@dataclass(frozen=True)
class Decision:
    action: str  # BUY | SKIP | HOLD | SELL | BLOCKED
    reasons: tuple = ()
    reason: Optional[str] = None  # primary exit reason or skip reason
    size_sol: Decimal = ZERO  # BUY: SOL to spend (before the fixed fee)
    qty: Decimal = ZERO  # SELL: token quantity to sell
    fraction: Decimal = ZERO  # SELL: fraction of the INITIAL quantity (1 for a full exit)
    quote_required: bool = False  # SELL decided from the mark; fetch a sell quote then call exit_decision again
    position_updates: Mapping = field(default_factory=dict)  # persist even when HOLD (peak_ratio, touched_15)
    after_fill: Mapping = field(default_factory=dict)  # apply after the SELL fill (stage, stop_ratio)
    detail: Mapping = field(default_factory=dict)


def _skip(*reasons: str, **detail) -> Decision:
    return Decision("SKIP", reasons=tuple(reasons), reason=reasons[0], detail=detail)


def _num(value) -> Optional[Decimal]:
    """Finite non-negative Decimal or None. Missing/garbage evidence is never coerced to a passing value."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d >= ZERO else None


# --------------------------------------------------------------------------------------- portfolio

def daily_drawdown(portfolio: Portfolio) -> Decimal:
    return max(ZERO, portfolio.day_start_equity - portfolio.equity)


def portfolio_blockers(portfolio: Portfolio, cfg: StrategyConfig, mint: Optional[str] = None) -> list:
    """Ledger-only reasons a new entry is refused. No I/O: lets the runner skip a doomed candidate before spending
    any provider request."""
    reasons = []
    if portfolio.day_start_equity <= ZERO or portfolio.equity <= ZERO:
        reasons.append("PORTFOLIO_INVALID")
    elif daily_drawdown(portfolio) >= cfg.daily_loss_stop_fraction * portfolio.day_start_equity:
        reasons.append("DAILY_LOSS_STOP")
    if portfolio.loss_streak >= cfg.loss_streak_pause_at:
        reasons.append("LOSS_STREAK_PAUSE")
    if len(portfolio.open_mints) >= cfg.max_positions:
        reasons.append("MAX_POSITIONS")
    if mint is not None:
        if mint in portfolio.open_mints:
            reasons.append("ALREADY_HELD")
        if portfolio.cooldowns.get(mint, 0) > portfolio.now:
            reasons.append("COOLDOWN")
    if portfolio.last_entry_minute == portfolio.now // 60:
        reasons.append("ENTRY_THROTTLE")
    return reasons


def should_liquidate(portfolio: Portfolio, cfg: StrategyConfig) -> bool:
    """Daily loss at/over the liquidate fraction: every position is sold (desk: DAILY_LIQUIDATE -> LIQUIDATE)."""
    return (portfolio.day_start_equity > ZERO
            and daily_drawdown(portfolio) >= cfg.daily_liquidate_fraction * portfolio.day_start_equity)


def entry_size(features: Mapping, portfolio: Portfolio, cfg: StrategyConfig) -> tuple:
    """(size_sol, limits). Smallest of: per-position cap, pool-liquidity cap, spendable cash, remaining exposure room;
    halved after `loss_streak_halve_after` consecutive losses; rounded DOWN to 1e-9."""
    limits = {
        "capital": cfg.max_position_fraction * portfolio.equity,
        "cash": max(ZERO, portfolio.cash - cfg.fee_reserve_sol - cfg.fixed_fee_sol),
        "exposure": max(ZERO, cfg.max_exposure_fraction * portfolio.equity - portfolio.exposure),
    }
    reserve = _num(features.get("reserve_sol"))
    if reserve is not None:
        limits["liquidity"] = cfg.liquidity_fraction * 2 * reserve
    size = max(ZERO, min(limits.values()))
    if portfolio.loss_streak >= cfg.loss_streak_halve_after:
        size /= 2
    return size.quantize(SATOSHI, rounding=ROUND_DOWN), limits


def feature_blockers(features: Mapping, cfg: StrategyConfig) -> list:
    """Market-cap and liquidity band. Missing or malformed values reject (fail closed); they never pass."""
    reasons = []
    mcap = _num(features.get("market_cap_usd"))
    if mcap is None:
        reasons.append("MARKET_CAP_UNKNOWN")
    elif not cfg.min_market_cap_usd <= mcap <= cfg.max_market_cap_usd:
        reasons.append("MARKET_CAP")
    liq = _num(features.get("liquidity_usd"))
    if liq is None:
        reasons.append("LIQUIDITY_UNKNOWN")
    elif liq < cfg.min_liquidity_usd:
        reasons.append("LIQUIDITY")
    return reasons


def entry_decision(features: Mapping, quote: Optional[RoundTrip], portfolio: Portfolio, cfg: StrategyConfig) -> Decision:
    """BUY with `size_sol` or SKIP with reasons. With quote=None this is the cheap pre-quote gate: it returns
    BUY(size_sol, quote_required=True) meaning "fetch round-trip quotes for this size and call again"."""
    mint = features.get("mint")
    reasons = portfolio_blockers(portfolio, cfg, mint if isinstance(mint, str) else None)
    reasons += feature_blockers(features, cfg)
    if not isinstance(mint, str) or not mint:
        reasons.append("MINT_MISSING")
    if reasons:
        return _skip(*reasons)
    size, limits = entry_size(features, portfolio, cfg)
    if size < cfg.min_order_sol:
        return _skip("BELOW_MINIMUM", size_sol=str(size), limits={k: str(v) for k, v in limits.items()})
    if quote is None:
        return Decision("BUY", size_sol=size, quote_required=True, detail={"limits": {k: str(v) for k, v in limits.items()}})
    bad = _roundtrip_blockers(quote, size, portfolio.now, cfg)
    if bad:
        return _skip(*bad, size_sol=str(size))
    cost = roundtrip_cost_fraction(quote, cfg)
    if cost > cfg.max_roundtrip_cost_fraction:
        return _skip("COST_BUDGET", estimated_cost_fraction=str(cost), size_sol=str(size))
    return Decision("BUY", reasons=("ENTRY",), reason="ENTRY", size_sol=quote.buy.in_amount,
                    detail={"estimated_cost_fraction": str(cost), "limits": {k: str(v) for k, v in limits.items()}})


def _quote_blockers(q: Quote, side: str, now: int, cfg: StrategyConfig) -> list:
    if not isinstance(q, Quote) or q.side != side:
        return [f"{side.upper()}_QUOTE_MISSING"]
    out = []
    if not q.route_available:
        out.append("NO_ROUTE")
    if not (isinstance(q.in_amount, Decimal) and isinstance(q.out_amount, Decimal)
            and q.in_amount.is_finite() and q.out_amount.is_finite() and q.in_amount > ZERO and q.out_amount > ZERO):
        out.append(f"{side.upper()}_QUOTE_INVALID")
    if type(q.observed_at) is not int or not 0 <= now - q.observed_at <= cfg.price_ttl_seconds:
        out.append(f"{side.upper()}_QUOTE_STALE")
    return out


def _roundtrip_blockers(rt, size: Decimal, now: int, cfg: StrategyConfig) -> list:
    if not isinstance(rt, RoundTrip):
        return ["ROUNDTRIP_QUOTE_MISSING"]
    out = _quote_blockers(rt.buy, "buy", now, cfg) + _quote_blockers(rt.sell, "sell", now, cfg)
    if out:
        return out
    if rt.buy.in_amount > size or rt.buy.in_amount < cfg.min_order_sol:
        out.append("QUOTE_EXCEEDS_RISK_ALLOCATION")
    if rt.sell.in_amount > rt.buy.out_amount:
        out.append("SELL_QUOTE_EXCEEDS_BOUGHT")  # would price tokens the buy never returns
    return out


def roundtrip_cost_fraction(rt: RoundTrip, cfg: StrategyConfig) -> Decimal:
    """(spent - expected_sell + 5 fees) / spent: entry fee plus the four possible sells, slippage on both legs
    (desk/engine.py: roundtrip = (amount - expected_sell + 5*fee) / amount)."""
    keep = ONE - cfg.adverse_slippage_bps / 10000
    spent = rt.buy.in_amount
    expected_sell = rt.sell.out_amount * keep
    return (spent - expected_sell + 5 * cfg.fixed_fee_sol) / spent


def sell_quote_tokens(buy: Quote, cfg: StrategyConfig) -> Decimal:
    """Tokens the paper fill will actually hold after adverse slippage; the sell leg of RoundTrip is quoted for this."""
    return buy.out_amount * (ONE - cfg.adverse_slippage_bps / 10000)


def new_position_fields(cfg: StrategyConfig) -> dict:
    return {"stage": 0, "stop_ratio": cfg.initial_stop_ratio, "peak_ratio": ONE, "touched_15": False}


# ------------------------------------------------------------------------------------------------ exits

def exit_decision(position: Position, mark: Optional[Decimal], quote: Optional[Quote], now: int,
                  cfg: StrategyConfig, *, liquidate: bool = False, danger: bool = False,
                  mark_at: Optional[int] = None) -> Decision:
    """HOLD / SELL / BLOCKED for one open position.

    `mark` is the net SOL value of the whole remaining quantity. A stale (older than price_ttl_seconds) or missing
    mark BLOCKS (evidence unknown: never a silent hold, never a sale at a guessed price) except that `liquidate` and
    `danger` still sell when the quote is usable, because those exits must not wait for a fresh mark.
    Exit priority follows desk/engine.py manage_position: LIQUIDATE, DANGER, STOP, TRAILING_STOP, MAX_HOLD, TIME_STOP,
    then at most one TAKE_PROFIT rung per observation.

    quote=None with a triggered exit returns SELL(quote_required=True): fetch a sell quote for `qty`, call again.
    """
    forced = "LIQUIDATE" if liquidate else "DANGER" if danger else None
    mark_ok = (isinstance(mark, Decimal) and mark.is_finite() and mark >= ZERO and position.cost_left > ZERO
               and (mark_at is None or 0 <= now - mark_at <= cfg.price_ttl_seconds))
    if not mark_ok and not forced:
        return Decision("BLOCKED", reasons=("STALE_OR_UNAVAILABLE_MARK",), reason="STALE_OR_UNAVAILABLE_MARK")
    updates: dict = {}
    reason = forced
    ratio = None
    if mark_ok:
        ratio = mark / position.cost_left
        peak = max(position.peak_ratio, ratio)
        touched = position.touched_15 or ratio >= cfg.touched_ratio
        if peak != position.peak_ratio:
            updates["peak_ratio"] = peak
        if touched != position.touched_15:
            updates["touched_15"] = touched
        age = now - position.opened_at
        if reason is None:
            if ratio <= position.stop_ratio:
                reason = "STOP"
            elif position.stage >= LADDER_STAGES and ratio <= peak * (ONE - cfg.trailing_fraction):
                reason = "TRAILING_STOP"
            elif age >= cfg.max_hold_seconds:
                reason = "MAX_HOLD"
            elif not touched and age >= cfg.time_stop_seconds:
                reason = "TIME_STOP"
    detail = {"ratio": str(ratio)} if ratio is not None else {}
    if reason is not None:
        return _sell(position, reason, ONE, quote, now, cfg, updates, {}, detail)
    if position.stage < len(cfg.tp_ladder):
        rung = cfg.tp_ladder[position.stage]
        if ratio >= rung.trigger:
            return _sell(position, "TAKE_PROFIT", rung.fraction, quote, now, cfg, updates,
                         {"stage": position.stage + 1, "stop_ratio": rung.stop_ratio}, detail)
    return Decision("HOLD", reasons=(), reason=None, position_updates=updates, detail=detail)


def _sell(position, reason, fraction, quote, now, cfg, updates, after_fill, detail) -> Decision:
    qty = position.qty if fraction >= ONE else min(position.qty, position.initial_qty * fraction)
    common = dict(reasons=(reason,), reason=reason, qty=qty, fraction=fraction, position_updates=updates,
                  after_fill=after_fill, detail=detail)
    if qty <= ZERO:
        return Decision("BLOCKED", **{**common, "reasons": ("ZERO_QUANTITY",), "reason": "ZERO_QUANTITY"})
    if quote is None:
        return Decision("SELL", quote_required=True, **common)
    bad = _quote_blockers(quote, "sell", now, cfg)
    if not bad and quote.in_amount != qty:
        bad = ["SELL_QUOTE_SIZE_MISMATCH"]
    if bad:
        # The exit stays wanted; the runner retries next loop. Position updates are still returned so the peak is kept.
        return Decision("BLOCKED", **{**common, "reasons": tuple(bad) + (reason,), "reason": bad[0],
                                      "detail": {**detail, "wanted_exit": reason}})
    net = quote.out_amount * (ONE - cfg.adverse_slippage_bps / 10000) - cfg.fixed_fee_sol
    if net <= ZERO:
        return Decision("BLOCKED", **{**common, "reasons": ("STUCK_POSITION", reason), "reason": "STUCK_POSITION"})
    return Decision("SELL", **common)


# ------------------------------------------------------------------------------- bookkeeping helpers

def cooldown_until(reason: str, now: int, cfg: StrategyConfig) -> int:
    """Re-entry embargo after a full exit (desk: STOP gets the longer cooldown)."""
    return now + (cfg.stop_cooldown_seconds if reason == "STOP" else cfg.cooldown_seconds)


def next_loss_streak(previous: int, trade_pnl: Decimal) -> int:
    """Updated when a position is fully closed; trade_pnl is the SUM over all of its fills."""
    return previous + 1 if trade_pnl < ZERO else 0
