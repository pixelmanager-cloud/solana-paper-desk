"""Offline replay of the lean strategy on recorded price paths. PAPER ONLY, EXECUTION_UNVERIFIED. Read-only, no network,
no clock, no randomness: the same paths and config always give the same trades.

Nothing here re-implements a rule. Entry and exit decisions are `lean.strategy.entry_decision` / `exit_decision`
called directly, fills are `lean.paper.buy` / `sell` / `apply_fill` (integer lamports, the same fee and adverse
slippage as the live trader), and EVERY MARK is `lean.adapters.mark` applied to the recorded vault amounts: the very
function the live position loop marks with, with no extra haircut and no reserve rebuilt from a price or a liquidity
figure. A change to the strategy, the paper rules or the mark changes the replay with it.

Input contract (what the L07R2 recorder, `lean/paths.py`, writes into its own `paths.sqlite`; this module only reads):
  * table `paths`: one row per recorded mint: `start_ts`, `features` (the screen features, JSON), `target` (JSON: `decimals`,
    `fee_raw` = the pool fees inside the quote vault at the screen), `entered`, `reason` (why it was not entered, live).
  * table `path_marks`: `(mint, ts, base_raw, quote_raw, status)`; `base_raw` / `quote_raw` are the pool's vault amounts, status
    is OK, or ACCOUNT_MISSING / EMPTY_POOL (the pool is gone: a rug signal, no amounts), or MALFORMED (unknown, skipped, counted).
  A path without a usable start or with fewer than 2 usable marks is skipped and counted; a corrupt mark (a non-integer or a
  non-positive vault amount) is skipped and counted. Corrupt evidence never becomes an entry. A DEAD mark (ACCOUNT_MISSING /
  EMPTY_POOL) blocks any entry or exit at that mark and values an open position at 0; a position still in a dead pool when its
  path ends is written off as `RUG_WRITEOFF` (a closed trade with its whole remaining cost lost) so rugs are never silently
  censored out of the statistics.

Simulation model (every item is an ASSUMPTION the calibration test and the report make visible):
  * Quotes are synthesised from the recorded vault amounts with a constant-product pool and a pool fee of `pool_fee_bps`
    (default 30; the runner's `pool_fee_bps`). Real Jupiter routes can differ (multi-hop, different impact). The mark of a held
    position is `adapters.mark` of its quantity at the same reserves.
  * An entry is decided at the screen's features RE-COMPUTED at the entry mark: market cap, liquidity and the spendable reserve
    come from that mark's vault amounts (the pool fees, the virtual reserves, the supply and SOL/USD stay the screen's values).
    The band `min/max_market_cap_usd`, `min_liquidity_usd` is therefore judged when the order would be placed, not at the screen.
  * `first_mark` fills AT the first mark with ts >= start ts + `entry_delay_s`; `pullback` and `momentum` are decided at the
    trigger mark and FILL AT THE NEXT recorded mark (the order cannot fill at the very price that triggered it). If a portfolio
    gate blocks the candidate at that moment it is dropped, like the live loop.
  * The marks are ~15 s apart, so a stop can only fire at the next recorded mark; live held checks run every <= 10 s.
  * All paths are merged in time order into ONE portfolio (positions cap, exposure, cooldowns, loss streak, UTC-day
    loss stop), so concurrent paths compete for the same caps exactly as live.
  * A position still open when its path ends is `OPEN_AT_END`: reported, valued at its last mark, and EXCLUDED from trade
    statistics (it is censored, not a win or a loss).
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from typing import Optional, Sequence

from lean import adapters as A
from lean import paper
from lean import strategy as S

ZERO, ONE = Decimal(0), Decimal(1)
LAMPORTS = Decimal(paper.LAMPORTS)
DAY = 86400
DEFAULT_DECIMALS = 6
MIN_MARKS = 2
DEAD_STATUSES = ('ACCOUNT_MISSING', 'EMPTY_POOL')


class ReplayError(ValueError):
    pass


@dataclass(frozen=True)
class Point:
    ts: float
    base_raw: Optional[int] = None  # the pool's base (token) vault amount, raw units; None only for a dead mark
    quote_raw: Optional[int] = None  # the pool's quote (SOL) vault amount, lamports; None only for a dead mark
    status: str = 'OK'  # OK | ACCOUNT_MISSING | EMPTY_POOL (dead)

    @property
    def dead(self) -> bool:
        return self.status != 'OK'

    @property
    def price(self) -> Optional[Decimal]:
        """The pool's spot price in raw units (quote per base vault amount): only ratios of it are used, by the entry timing."""
        return None if self.dead else Decimal(self.quote_raw) / Decimal(self.base_raw)


@dataclass(frozen=True)
class Path:
    mint: str
    start_ts: float
    features: dict
    points: tuple
    decimals: int = DEFAULT_DECIMALS
    not_entered_reason: Optional[str] = None  # what the live trader said; informational only
    fee_raw: int = 0  # pool fees inside the quote vault at the screen (not spendable)
    virtual_quote_raw: int = 0  # virtual quote reserves of the pool (boosted pools), from the screen
    supply_raw: Optional[int] = None
    sol_usd: Optional[Decimal] = None


TIMING_KINDS = ('first_mark', 'pullback', 'momentum')


@dataclass(frozen=True)
class EntryTiming:
    """WHEN a candidate that passed screening is first offered to the entry rules. Causal: every trigger uses only marks
    at or before the current one.
      * first_mark: the first mark at/after start + entry_delay_s (the L08 behaviour).
      * pullback:  the first mark whose price is at least `pullback_pct` (a fraction, 0.10 = 10%) below the highest mark seen
                   since the path started (the first mark counts as the initial high, so an immediate dip also qualifies).
      * momentum:  the first mark at which prices sampled every minute for the last `momentum_minutes` minutes (the latest
                   mark at or before t, t-60 s, ..., t-N*60 s) are STRICTLY increasing; needs N minutes of history.
    A candidate that has not triggered within `max_wait_s` of its start (or before its path ends) is dropped and recorded as
    ENTRY_TIMING_NO_TRIGGER. Once triggered it is considered exactly once: a portfolio block at that moment drops it, as live.
    `ReplayConfig.entry_delay_s` still counts from the path start, and the trigger must hold at the mark where the delay has
    elapsed (it is not a latency after the signal)."""
    kind: str = 'first_mark'
    pullback_pct: Optional[Decimal] = None
    momentum_minutes: Optional[int] = None
    max_wait_s: float = 3600

    def __post_init__(self):
        if self.kind not in TIMING_KINDS:
            raise ReplayError('entry timing kind must be one of %s' % (TIMING_KINDS,))
        if isinstance(self.max_wait_s, bool) or not isinstance(self.max_wait_s, (int, float)) or not math.isfinite(self.max_wait_s) or self.max_wait_s <= 0:
            raise ReplayError('max_wait_s must be a finite number > 0')
        if self.kind == 'pullback':
            if not (isinstance(self.pullback_pct, Decimal) and self.pullback_pct.is_finite() and 0 < self.pullback_pct < 1) or self.momentum_minutes is not None:
                raise ReplayError('pullback needs pullback_pct in (0, 1) and no momentum_minutes')
        elif self.kind == 'momentum':
            if type(self.momentum_minutes) is not int or not 1 <= self.momentum_minutes <= 60 or self.pullback_pct is not None:
                raise ReplayError('momentum needs momentum_minutes in [1, 60] and no pullback_pct')
        elif self.pullback_pct is not None or self.momentum_minutes is not None:
            raise ReplayError('first_mark takes no parameters')

    @classmethod
    def from_spec(cls, spec):
        """'first_mark' | {'kind': ..., 'pullback_pct': '0.1', 'momentum_minutes': 3, 'max_wait_s': 1800}; strict on keys."""
        if isinstance(spec, EntryTiming):
            return spec
        if isinstance(spec, str):
            spec = {'kind': spec}
        if not isinstance(spec, dict) or set(spec) - {'kind', 'pullback_pct', 'momentum_minutes', 'max_wait_s'} or 'kind' not in spec:
            raise ReplayError('bad entry timing spec')
        values = dict(spec)
        if values.get('pullback_pct') is not None:
            pct = finite_positive(values['pullback_pct'])
            if pct is None:
                raise ReplayError('pullback_pct must be a positive number')
            values['pullback_pct'] = pct
        return cls(**values)

    @property
    def label(self):
        if self.kind == 'pullback':
            return 'pullback(%s)' % self.pullback_pct.normalize()
        if self.kind == 'momentum':
            return 'momentum(%dm)' % self.momentum_minutes
        return 'first_mark'

    def to_dict(self):
        return {'kind': self.kind, 'pullback_pct': None if self.pullback_pct is None else str(self.pullback_pct),
                'momentum_minutes': self.momentum_minutes, 'max_wait_s': float(self.max_wait_s)}


class _Watch:
    """Per-path causal state for an EntryTiming."""

    def __init__(self, timing: EntryTiming):
        self.timing, self.high, self.samples = timing, None, []

    def update(self, point: Point) -> bool:
        t = self.timing
        if point.dead:
            return False
        self.samples.append((point.ts, point.price))
        self.high = point.price if self.high is None else max(self.high, point.price)
        if t.kind == 'first_mark':
            return True
        if t.kind == 'pullback':
            return (self.high - point.price) / self.high >= t.pullback_pct
        chain = []
        for k in range(t.momentum_minutes + 1):
            target = point.ts - 60 * k
            older = [price for ts, price in self.samples if ts <= target]
            if not older:
                return False
            chain.append(older[-1])
        return all(chain[k] > chain[k + 1] for k in range(len(chain) - 1))


@dataclass(frozen=True)
class ReplayConfig:
    initial_cash_sol: Decimal = Decimal(5)
    pool_fee_bps: int = 30
    entry_delay_s: float = 0
    entry_timing: EntryTiming = EntryTiming()

    def __post_init__(self):
        if not (isinstance(self.initial_cash_sol, Decimal) and self.initial_cash_sol.is_finite() and self.initial_cash_sol > 0):
            raise ReplayError('initial_cash_sol must be a positive Decimal')
        if type(self.pool_fee_bps) is not int or not 0 <= self.pool_fee_bps < 10000:
            raise ReplayError('pool_fee_bps must be an integer in [0, 10000)')
        if isinstance(self.entry_delay_s, bool) or not isinstance(self.entry_delay_s, (int, float)) or not math.isfinite(self.entry_delay_s) or self.entry_delay_s < 0:
            raise ReplayError('entry_delay_s must be a finite number >= 0')
        if not isinstance(self.entry_timing, EntryTiming):
            raise ReplayError('entry_timing must be an EntryTiming')

    def to_dict(self):
        return {'initial_cash_sol': str(self.initial_cash_sol), 'pool_fee_bps': self.pool_fee_bps, 'entry_delay_s': float(self.entry_delay_s),
                'entry_timing': self.entry_timing.to_dict()}


def paper_config(cfg: S.StrategyConfig) -> paper.PaperConfig:
    """The fee and slippage the live trader uses for this strategy config (lean.adapters.paper_config, the runner's own)."""
    try:
        return A.paper_config(cfg)
    except A.AdapterError as error:
        raise ReplayError(str(error)) from None


# ------------------------------------------------------------------------------------------------- pool maths
def quote_buy(point: Point, lamports: int, pool_fee_bps: int) -> int:
    """Raw tokens out for `lamports` SOL in: constant product on the recorded vault amounts, after the pool fee (rounded down)."""
    eff = lamports * (paper.MAX_BPS - pool_fee_bps) // paper.MAX_BPS
    return point.base_raw * eff // (point.quote_raw + eff)


def quote_sell(point: Point, qty_raw: int, pool_fee_bps: int) -> int:
    """Lamports out for `qty_raw` tokens in: the same constant-product output `adapters.mark` uses, after the pool fee."""
    return point.quote_raw * qty_raw // (point.base_raw + qty_raw) * (paper.MAX_BPS - pool_fee_bps) // paper.MAX_BPS


def features_at(path: Path, point: Point) -> Optional[dict]:
    """The strategy's entry features as they are AT `point`: the screen's features with the reserve-dependent ones re-computed
    from the mark's vault amounts exactly as `lean.candidates` computes them (price from the EFFECTIVE quote reserve, liquidity
    from the SPENDABLE one). None when the pool state cannot be priced."""
    gross = point.quote_raw
    spendable = gross - path.fee_raw
    effective = gross + path.virtual_quote_raw
    if spendable <= 0 or effective <= 0 or point.base_raw <= 0:
        return None
    out = dict(path.features)
    with localcontext() as ctx:
        ctx.prec = 60
        supply_ui = Decimal(path.supply_raw) / (Decimal(10) ** path.decimals)
        base_ui = Decimal(point.base_raw) / (Decimal(10) ** path.decimals)
        price = (Decimal(effective) / Decimal(10) ** 9) / base_ui
        out.update(base_reserve_raw=str(point.base_raw), quote_gross_raw=str(gross), quote_spendable_raw=str(spendable),
                   quote_effective_raw=str(effective), price_sol_per_token=format(price, 'f'),
                   market_cap_usd=format(supply_ui * price * path.sol_usd, 'f'),
                   liquidity_usd=format(2 * (Decimal(spendable) / Decimal(10) ** 9) * path.sol_usd, 'f'))
    return A.entry_features(out, path.mint)


def finite_positive(value) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d > 0 else None


# -------------------------------------------------------------------------------------------------- results
@dataclass
class Trade:
    mint: str
    status: str  # CLOSED | OPEN_AT_END
    entry_ts: float
    exit_ts: float
    cost_lamports: int  # buy size + buy fee
    realized_lamports: int  # sum over sells (fees and the buy fee included); for OPEN_AT_END, marked to the last mark
    exit_reason: Optional[str]
    reasons: list = field(default_factory=list)  # every sell reason in order (TAKE_PROFIT..., final)
    fills: int = 0
    mark_lamports: Optional[int] = None  # OPEN_AT_END only: net value of what is still held at its last mark

    @property
    def pnl_sol(self) -> Decimal:
        return Decimal(self.realized_lamports) / LAMPORTS

    @property
    def return_fraction(self) -> Decimal:
        return Decimal(self.realized_lamports) / Decimal(self.cost_lamports)

    @property
    def hold_s(self) -> float:
        return self.exit_ts - self.entry_ts

    def to_dict(self):
        return {'mint': self.mint, 'status': self.status, 'entry_ts': self.entry_ts, 'exit_ts': self.exit_ts,
                'hold_s': self.hold_s, 'cost_sol': str(Decimal(self.cost_lamports) / LAMPORTS), 'pnl_sol': str(self.pnl_sol),
                'return': str(self.return_fraction), 'exit_reason': self.exit_reason, 'reasons': list(self.reasons), 'fills': self.fills}


@dataclass
class ReplayResult:
    trades: list  # closed and open-at-end, in the order they closed / ended
    skipped_entries: list  # [{mint, ts, reasons}]
    anomalies: dict  # counters: BLOCKED_EXIT, ZERO_SELL, ...
    paths_considered: int
    initial_cash_sol: Decimal
    final_cash_lamports: int
    fills: list = field(default_factory=list)  # every paper Fill, in order (the same objects lean.paper produced)

    @property
    def closed(self):
        return [t for t in self.trades if t.status == 'CLOSED']

    @property
    def open_at_end(self):
        return [t for t in self.trades if t.status == 'OPEN_AT_END']


# --------------------------------------------------------------------------------------------------- replay
class _Held:
    __slots__ = ('path', 'opened_ts', 'initial_qty', 'stage', 'stop_ratio', 'peak_ratio', 'touched_15', 'last_mark',
                 'cost0', 'realized', 'reasons', 'fills')

    def __init__(self, path, opened_ts, initial_qty, cfg, cost0):
        fields = S.new_position_fields(cfg)
        self.path, self.opened_ts, self.initial_qty, self.cost0 = path, opened_ts, initial_qty, cost0
        self.stage, self.stop_ratio, self.peak_ratio, self.touched_15 = (fields[k] for k in ('stage', 'stop_ratio', 'peak_ratio', 'touched_15'))
        self.last_mark, self.realized, self.reasons, self.fills = cost0, 0, [], 1


def replay(paths: Sequence[Path], cfg: S.StrategyConfig, rcfg: Optional[ReplayConfig] = None) -> ReplayResult:
    rcfg = rcfg or ReplayConfig()
    pcfg = paper_config(cfg)
    events = sorted(((p.ts, i, j) for i, path in enumerate(paths) for j, p in enumerate(path.points)))
    positions: dict = {}
    cash = int(rcfg.initial_cash_sol * LAMPORTS)
    held: dict = {}
    cooldowns: dict = {}
    trades, skipped, anomalies, fills = [], [], {}, []
    state = {'day': None, 'day_start': Decimal(cash) / LAMPORTS, 'last_entry_minute': -1, 'loss_streak': 0}
    considered = set()

    def bump(name):
        anomalies[name] = anomalies.get(name, 0) + 1

    def equity_lamports():
        return cash + sum(h.last_mark for h in held.values())

    def portfolio(now):
        exposure = sum((Decimal(p.cost_lamports) for p in positions.values()), ZERO) / LAMPORTS
        return S.Portfolio(now=now, equity=Decimal(equity_lamports()) / LAMPORTS, cash=Decimal(cash) / LAMPORTS, exposure=exposure,
                           day_start_equity=state['day_start'], open_mints=frozenset(positions), cooldowns=dict(cooldowns),
                           last_entry_minute=state['last_entry_minute'], loss_streak=state['loss_streak'])

    def net_mark(point, pos):
        """EXACTLY the live mark: adapters.mark of the held quantity at the recorded vault amounts (no extra haircut)."""
        return int(A.mark(pos.qty_raw, point.base_raw, point.quote_raw, pool_fee_bps=rcfg.pool_fee_bps, pcfg=pcfg) * LAMPORTS)  # 0 when nothing is left after the fees

    def try_enter(path, point, now):
        nonlocal cash, positions
        feats = features_at(path, point)
        if feats is None:
            skipped.append({'mint': path.mint, 'ts': point.ts, 'reasons': ['POOL_STATE_UNPRICEABLE']})
            return
        pre = S.entry_decision(feats, None, portfolio(now), cfg)
        if pre.action != 'BUY':
            skipped.append({'mint': path.mint, 'ts': point.ts, 'reasons': list(pre.reasons)})
            return
        lamports = paper.to_lamports(pre.size_sol)
        tokens = quote_buy(point, lamports, rcfg.pool_fee_bps)
        if tokens <= 0:
            skipped.append({'mint': path.mint, 'ts': point.ts, 'reasons': ['ZERO_OUTPUT']})
            return
        buy_q = paper.Quote(path.mint, 'buy', lamports, tokens, path.decimals, point.ts)
        try:
            fill = paper.buy(buy_q, pre.size_sol, pcfg)
        except paper.PaperError:
            skipped.append({'mint': path.mint, 'ts': point.ts, 'reasons': ['PAPER_REFUSED']})
            return
        back = quote_sell(point, fill.qty_raw, rcfg.pool_fee_bps)
        if back <= 0:
            skipped.append({'mint': path.mint, 'ts': point.ts, 'reasons': ['SELL_QUOTE_INVALID']})
            return
        rt = A.roundtrip(buy_q, now, paper.Quote(path.mint, 'sell', fill.qty_raw, back, path.decimals, point.ts), now)
        decision = S.entry_decision(feats, rt, portfolio(now), cfg)
        if decision.action != 'BUY':
            skipped.append({'mint': path.mint, 'ts': point.ts, 'reasons': list(decision.reasons)})
            return
        positions, cash = paper.apply_fill(positions, cash, fill)
        fills.append(fill)
        pos = positions[path.mint]
        h = _Held(path, point.ts, fill.qty_raw, cfg, pos.cost_lamports)
        held[path.mint] = h
        state['last_entry_minute'] = now // 60
        h.last_mark = net_mark(point, pos)

    def try_exit(path, point, now):
        nonlocal cash, positions
        pos = positions[path.mint]
        h = held[path.mint]
        h.last_mark = net_mark(point, pos)
        port = portfolio(now)
        liquidate = S.should_liquidate(port, cfg)

        def view():
            state = {'initial_qty_raw': h.initial_qty, 'stop_ratio': h.stop_ratio, 'stage': h.stage,
                     'peak_ratio': h.peak_ratio, 'touched_15': h.touched_15}
            return A.strategy_position(pos, state)

        mark = A.sol(h.last_mark)
        d = S.exit_decision(view(), mark, None, now, cfg, mark_at=now, liquidate=liquidate)
        _apply_updates(h, d)
        if d.action == 'BLOCKED':
            bump('BLOCKED_EXIT')
        if not (d.action == 'SELL' and d.quote_required):
            return
        try:
            sell_qty = A.sell_qty_raw(d.qty, pos.qty_raw)
        except A.AdapterError:
            bump('ZERO_SELL')
            return
        out = quote_sell(point, sell_qty, rcfg.pool_fee_bps)
        if out <= 0:
            bump('BLOCKED_EXIT')
            return
        pq = paper.Quote(path.mint, 'sell', sell_qty, out, path.decimals, point.ts)
        d2 = S.exit_decision(view(), mark, A.strategy_quote(pq, 'sell', now), now, cfg, mark_at=now, liquidate=liquidate)
        _apply_updates(h, d2)
        if d2.action != 'SELL':
            bump('BLOCKED_EXIT')
            return
        fill = paper.sell(pos, pq, cfg=pcfg, qty_raw=sell_qty)
        positions, cash = paper.apply_fill(positions, cash, fill)
        fills.append(fill)
        h.realized += fill.realized_lamports
        h.reasons.append(d2.reason)
        h.fills += 1
        h.stage = d2.after_fill.get('stage', h.stage)
        h.stop_ratio = d2.after_fill.get('stop_ratio', h.stop_ratio)
        if path.mint not in positions:  # fully closed
            pnl = Decimal(h.realized)  # sum of per-sell realized: already net of the full cost basis and all fees
            trades.append(Trade(path.mint, 'CLOSED', h.opened_ts, point.ts, h.cost0, h.realized, d2.reason, list(h.reasons), h.fills))
            state['loss_streak'] = S.next_loss_streak(state['loss_streak'], pnl)
            cooldowns[path.mint] = S.cooldown_until(d2.reason, now, cfg)
            del held[path.mint]
        else:
            h.last_mark = net_mark(point, positions[path.mint])

    watches, pending = {}, set()
    for ts, i, j in events:
        path = paths[i]
        point = path.points[j]
        now = int(math.floor(ts))
        day = now // DAY
        if state['day'] != day:
            state['day'] = day
            state['day_start'] = Decimal(equity_lamports()) / LAMPORTS
        if path.mint in held and held[path.mint].path is path:
            if point.dead:
                held[path.mint].last_mark = 0  # an empty/missing pool is worth nothing and cannot be sold into
                bump('POOL_DEAD_MARK')
            else:
                try_exit(path, point, now)
        elif i in pending and i not in considered:
            # pullback / momentum: the trigger was the PREVIOUS mark; the order fills at this one
            considered.add(i)
            pending.discard(i)
            if point.dead:
                skipped.append({'mint': path.mint, 'ts': ts, 'reasons': ['ENTRY_MARK_DEAD']})
            elif path.mint in held:
                skipped.append({'mint': path.mint, 'ts': ts, 'reasons': ['ALREADY_HELD']})
            else:
                try_enter(path, point, now)
        elif i not in considered:
            triggered = watches.setdefault(i, _Watch(rcfg.entry_timing)).update(point)
            if ts - path.start_ts > rcfg.entry_timing.max_wait_s:
                considered.add(i)
                skipped.append({'mint': path.mint, 'ts': ts, 'reasons': ['ENTRY_TIMING_NO_TRIGGER']})
            elif triggered and not point.dead and ts >= path.start_ts + rcfg.entry_delay_s:
                if rcfg.entry_timing.kind != 'first_mark':
                    pending.add(i)  # decided now, filled at the next recorded mark
                    continue
                considered.add(i)
                if path.mint in held:
                    skipped.append({'mint': path.mint, 'ts': ts, 'reasons': ['ALREADY_HELD']})
                else:
                    try_enter(path, point, now)
    for i, path in enumerate(paths):
        if i in pending and i not in considered and path.points:
            skipped.append({'mint': path.mint, 'ts': path.points[-1].ts, 'reasons': ['ENTRY_NO_NEXT_MARK']})
        elif i not in considered and path.points:
            skipped.append({'mint': path.mint, 'ts': path.points[-1].ts, 'reasons': ['ENTRY_TIMING_NO_TRIGGER']})

    for mint, h in held.items():
        pos = positions[mint]
        last = h.path.points[-1]
        value = h.realized + h.last_mark - pos.cost_lamports  # realized so far + (what is left worth - its basis)
        if last.dead:  # the path ended inside a dead pool: the rest is lost, not "still open"
            trades.append(Trade(mint, 'CLOSED', h.opened_ts, last.ts, h.cost0, value, 'RUG_WRITEOFF', list(h.reasons) + ['RUG_WRITEOFF'], h.fills))
            bump('RUG_WRITEOFF')
            continue
        trades.append(Trade(mint, 'OPEN_AT_END', h.opened_ts, last.ts, h.cost0, value, None, list(h.reasons), h.fills, h.last_mark))
    return ReplayResult(trades, skipped, anomalies, len(considered), rcfg.initial_cash_sol, cash, fills)


def _apply_updates(h: _Held, d: S.Decision) -> None:
    h.peak_ratio = d.position_updates.get('peak_ratio', h.peak_ratio)
    h.touched_15 = d.position_updates.get('touched_15', h.touched_15)


# ----------------------------------------------------------------------------------------------- metrics
def metrics(trades: Sequence[Trade], initial_cash_sol: Decimal) -> dict:
    """n, win rate, mean/median PnL per trade (SOL), max drawdown of the cumulative realized PnL (SOL and fraction of the
    starting cash), profit factor. Only CLOSED trades count (OPEN_AT_END is censored)."""
    closed = [t for t in trades if t.status == 'CLOSED']
    n = len(closed)
    if n == 0:
        return {'n_trades': 0, 'win_rate': None, 'mean_pnl_sol': None, 'median_pnl_sol': None, 'total_pnl_sol': '0',
                'max_drawdown_sol': '0', 'max_drawdown_fraction': '0', 'profit_factor': None, 'profit_factor_note': 'NO_TRADES'}
    pnls = [t.pnl_sol for t in closed]
    ordered = sorted(pnls)
    median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    wins = sum(1 for p in pnls if p > 0)
    gross_win = sum((p for p in pnls if p > 0), ZERO)
    gross_loss = -sum((p for p in pnls if p < 0), ZERO)
    cum = peak = dd = ZERO
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    out = {'n_trades': n, 'win_rate': wins / n, 'mean_pnl_sol': str(sum(pnls, ZERO) / n), 'median_pnl_sol': str(median),
           'total_pnl_sol': str(sum(pnls, ZERO)), 'max_drawdown_sol': str(dd),
           'max_drawdown_fraction': str(dd / initial_cash_sol)}
    if gross_loss > 0:
        out['profit_factor'], out['profit_factor_note'] = str(gross_win / gross_loss), None
    else:
        out['profit_factor'], out['profit_factor_note'] = None, 'NO_LOSSES' if gross_win > 0 else 'NO_PNL'
    return out


# ------------------------------------------------------------------------------------------------ loading
def _json_or_none(value):
    try:
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode('utf-8')
        out = json.loads(value)
        return out if isinstance(out, dict) else None
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def _int_or_none(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def load_paths(connections, *, since: Optional[float] = None, until: Optional[float] = None, default_decimals: int = DEFAULT_DECIMALS):
    """Build Paths from the recorder's `paths.sqlite` (one sqlite3 connection, or several: the rolled-over archives followed by
    the live file; read-only is enough). Returns (paths, skipped) where `skipped` counts every row/path that could not be used
    and why. A path carried over into a newer file keeps its first (oldest) start; marks are merged by timestamp."""
    if isinstance(connections, sqlite3.Connection):
        connections = [connections]
    skipped: dict = {}

    def bump(name):
        skipped[name] = skipped.get(name, 0) + 1

    starts, marks = {}, {}
    for connection in connections:
        for mint, start_ts, features, target, reason in connection.execute('SELECT mint, start_ts, features, target, reason FROM paths ORDER BY start_ts, mint'):
            if not isinstance(mint, str) or not mint:
                bump('ROW_NO_MINT')
                continue
            if mint in starts:
                bump('START_DUPLICATE')  # first one wins; a carried-over duplicate never moves the entry point
                continue
            features, target = _json_or_none(features), _json_or_none(target)
            if not isinstance(features, dict) or not isinstance(target, dict) or not isinstance(start_ts, (int, float)) or isinstance(start_ts, bool) \
                    or not math.isfinite(start_ts):
                bump('START_UNREADABLE')
                continue
            starts[mint] = (float(start_ts), features, target, reason)
        for mint, ts, base, quote, status in connection.execute('SELECT mint, ts, base_raw, quote_raw, status FROM path_marks ORDER BY mint, ts'):
            if not isinstance(mint, str) or not mint or not isinstance(ts, (int, float)) or isinstance(ts, bool) or not math.isfinite(ts):
                bump('ROW_NO_MINT' if not isinstance(mint, str) or not mint else 'MARK_BAD_TS')
                continue
            if status in DEAD_STATUSES:
                marks.setdefault(mint, []).append(Point(float(ts), None, None, status))
            elif status != 'OK':
                bump('MARK_' + (status if status == 'MALFORMED' else 'UNKNOWN_STATUS'))
            elif type(base) is not int or type(quote) is not int or base <= 0 or quote <= 0:
                bump('MARK_BAD_RESERVES')  # a corrupt vault amount must never become a price
            else:
                marks.setdefault(mint, []).append(Point(float(ts), base, quote))
    paths = []
    for mint, (start_ts, features, target, reason) in starts.items():
        if (since is not None and start_ts < since) or (until is not None and start_ts >= until):
            continue
        points, seen = [], set()
        for p in marks.get(mint, []):
            if p.ts < start_ts:
                bump('MARK_BEFORE_START')
            elif p.ts in seen:
                bump('MARK_DUPLICATE_TS')
            else:
                seen.add(p.ts)
                points.append(p)
        points.sort(key=lambda p: p.ts)
        if len(points) < MIN_MARKS:
            bump('PATH_TOO_FEW_MARKS')
            continue
        decimals = target.get('decimals', features.get('decimals', default_decimals))
        fee_raw, virtual = _int_or_none(target.get('fee_raw', 0)), _int_or_none(features.get('virtual_quote_reserves_raw', 0))
        supply, sol_usd = _int_or_none(features.get('supply_raw')), finite_positive(features.get('sol_usd'))
        if type(decimals) is not int or not 0 <= decimals <= 18:
            bump('PATH_BAD_DECIMALS')
            continue
        if fee_raw is None or virtual is None or supply is None or supply <= 0 or sol_usd is None:
            bump('PATH_FEATURES_INCOMPLETE')  # the entry band cannot be judged at the entry mark without the supply and SOL/USD
            continue
        reasons = _json_list(reason)
        paths.append(Path(mint, start_ts, A.entry_features(features, mint), tuple(points), decimals, ','.join(map(str, reasons)) or None,
                          fee_raw, virtual, supply, sol_usd))
    for mint in marks:
        if mint not in starts:
            bump('MARKS_WITHOUT_START')
    paths.sort(key=lambda p: (p.start_ts, p.mint))
    return paths, skipped


def _json_list(value):
    try:
        out = json.loads(value)
        return out if isinstance(out, list) else []
    except (ValueError, TypeError):
        return []


def open_paths(*files):
    """Read-only connections (plain `mode=ro`, `query_only`) to the recorder's files, oldest first. A plain read-only open of a
    WAL file may leave `-wal` / `-shm` beside it; the data file is never modified."""
    from lean.report import open_ro
    return [open_ro(f) for f in files]


# ---------------------------------------------------------------------------------------------- calibration
def live_sells(connection) -> dict:
    """What the live trader did, from its own store: mints bought (typed `fills`, in order), the ordered SELL reasons per mint
    (`decisions` rows kind='exit' action='SELL', reasons[0]; a sell without a readable reason is None, never guessed) and the
    mints whose position is still open (typed `fills.qty_after` of the last fill > 0)."""
    sells: dict = {}
    bought: list = []
    still_open: set = set()
    try:
        for mint, side, qty_after in connection.execute('SELECT mint, side, qty_after FROM fills ORDER BY id'):
            if side == 'buy' and mint not in bought:
                bought.append(mint)
            (still_open.add if qty_after else still_open.discard)(mint)
        for mint, reasons in connection.execute("SELECT mint, reasons FROM decisions WHERE kind='exit' AND action='SELL' ORDER BY id"):
            try:
                parsed = json.loads(reasons)
                reason = parsed[0] if isinstance(parsed, list) and parsed else None
            except (ValueError, TypeError):
                reason = None
            sells.setdefault(mint, []).append(reason)
    except sqlite3.Error:
        return {'bought': [], 'sells': {}, 'open': set()}  # not a lean store with the typed tables: nothing to compare
    return {'bought': bought, 'sells': sells, 'open': still_open}


def store_initial_cash_sol(connection) -> Optional[Decimal]:
    try:
        row = connection.execute("SELECT value FROM meta WHERE key='initial_cash_lamports'").fetchone()
        return Decimal(int(row[0])) / LAMPORTS if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return None


def calibrate(connection, path_connections, cfg: S.StrategyConfig, rcfg: Optional[ReplayConfig] = None) -> dict:
    """Replay `cfg` on the paths (the recorder's `paths.sqlite`, via `path_connections`) of the tokens the live trader (the store
    `connection`) actually bought and compare the ordered sell reasons. Mismatches are listed, never hidden. Live positions that
    are still open compare as a PREFIX of the replay."""
    rcfg = rcfg or ReplayConfig(initial_cash_sol=store_initial_cash_sol(connection) or ReplayConfig().initial_cash_sol)
    live = live_sells(connection)
    paths, skipped = load_paths(path_connections)
    by_mint = {p.mint: p for p in paths}
    traded = [by_mint[m] for m in live['bought'] if m in by_mint]
    result = replay(traded, cfg, rcfg)
    replayed = {t.mint: t for t in result.trades}
    rows = []
    for mint in live['bought']:
        live_reasons = live['sells'].get(mint, [])
        row = {'mint': mint, 'live_reasons': live_reasons, 'replay_reasons': None, 'replay_status': None, 'match': False, 'note': None}
        if mint not in by_mint:
            row['note'] = 'NO_PATH'
        elif mint not in replayed:
            row['note'] = 'NOT_ENTERED_IN_REPLAY'
        else:
            t = replayed[mint]
            row['replay_status'] = t.status
            row['replay_reasons'] = list(t.reasons)
            if mint in live['open']:  # live position not yet closed: its sells so far must be a prefix of the replay's
                row['match'] = row['replay_reasons'][:len(live_reasons)] == live_reasons
            else:
                row['match'] = t.status == 'CLOSED' and row['replay_reasons'] == live_reasons
            if not row['match']:
                row['note'] = 'EXIT_REASONS_DIFFER'
        rows.append(row)
    matched = sum(1 for r in rows if r['match'])
    status = 'NO_LIVE_FILLS' if not rows else 'OK' if matched == len(rows) else 'MISMATCH'
    return {'status': status, 'tokens': len(rows), 'matched': matched, 'mismatched': len(rows) - matched,
            'match_rate': matched / len(rows) if rows else None, 'rows': rows, 'load_skipped': skipped,
            'strategy_version': cfg.strategy_version, 'config_hash': cfg.config_hash, 'replay_assumptions': rcfg.to_dict()}
