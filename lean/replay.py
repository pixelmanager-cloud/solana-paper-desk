"""Offline replay of the lean strategy on recorded price paths. PAPER ONLY, EXECUTION_UNVERIFIED. Read-only, no network,
no clock, no randomness: the same paths and config always give the same trades.

Nothing here re-implements a rule. Entry and exit decisions are `lean.strategy.entry_decision` / `exit_decision`
called directly, fills are `lean.paper.buy` / `sell` / `apply_fill` (integer lamports, the same fee and adverse
slippage as the live trader), so a change to either module changes the replay with it.

Input contract (what L07 `lean/paths.py` writes into `observations`; this module only reads):
  * `kind='path_start'`, `mint`, `ts`; `raw` = canonical JSON {"features": <the screen features>, "reason": str|null, ...},
    `meta` = {"entered": bool, "reason": str|null, ...}. Features are turned into strategy features with
    `lean.adapters.entry_features` (the runner's own conversion), `decimals` comes from the features.
  * `kind='path_mark'`, `mint`, `ts`; `meta` (and `raw`) = {"price_sol", "liquidity_sol", "status", ...}. `liquidity_sol` is
    TWO-SIDED (2 x the SOL reserve), so the replay uses half of it as the pool's SOL reserve. `status` is OK, or
    ACCOUNT_MISSING / EMPTY_POOL (the pool is gone: a rug signal, price null), or MALFORMED (unknown, skipped and counted).
A path without a usable start, or with fewer than 2 usable marks, is skipped and counted; a corrupt mark (non-finite, <= 0)
is skipped and counted. Corrupt evidence never becomes an entry. A DEAD mark (ACCOUNT_MISSING / EMPTY_POOL) blocks any entry
or exit at that mark and values an open position at 0; a position still in a dead pool when its path ends is written off as
`RUG_WRITEOFF` (a closed trade with its whole remaining cost lost) so rugs are never silently censored out of the statistics.

Simulation model (every item is an ASSUMPTION the calibration test and the report make visible):
  * Quotes are synthesised from the recorded spot price and SOL reserve with a constant-product pool and a pool fee of
    `pool_fee_bps` (default 30). Real Jupiter routes can differ (multi-hop, different impact).
  * A candidate is considered once, at the first mark with ts >= start ts + `entry_delay_s` (default 0 = optimistic: live
    entry has latency). If a portfolio gate blocks it at that moment it is dropped, like the live loop.
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
from decimal import Decimal, InvalidOperation
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
    price_sol: Optional[Decimal]  # SOL per whole token; None only for a dead mark
    liquidity_sol: Optional[Decimal] = None  # SOL-side reserve (half of L07's two-sided figure); None = unknown (no impact)
    status: str = 'OK'  # OK | ACCOUNT_MISSING | EMPTY_POOL (dead)

    @property
    def dead(self) -> bool:
        return self.status != 'OK'


@dataclass(frozen=True)
class Path:
    mint: str
    start_ts: float
    features: dict
    points: tuple
    decimals: int = DEFAULT_DECIMALS
    not_entered_reason: Optional[str] = None  # what the live trader said; informational only


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
    ENTRY_TIMING_NO_TRIGGER. Once triggered it is considered exactly once: a portfolio block at that moment drops it, as live."""
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
        self.samples.append((point.ts, point.price_sol))
        self.high = point.price_sol if self.high is None else max(self.high, point.price_sol)
        if t.kind == 'first_mark':
            return True
        if t.kind == 'pullback':
            return (self.high - point.price_sol) / self.high >= t.pullback_pct
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
    decimals: int = DEFAULT_DECIMALS
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
def _tokens(raw, decimals):
    return Decimal(raw) / (Decimal(10) ** decimals)


def quote_buy(point: Point, lamports: int, decimals: int, pool_fee_bps: int) -> int:
    """Raw tokens out for `lamports` SOL in (rounded down)."""
    eff = Decimal(lamports) / LAMPORTS * (ONE - Decimal(pool_fee_bps) / 10000)
    if point.liquidity_sol is None:
        out = eff / point.price_sol
    else:
        reserve_tokens = point.liquidity_sol / point.price_sol
        out = reserve_tokens * eff / (point.liquidity_sol + eff)
    return int((out * Decimal(10) ** decimals).to_integral_value(rounding='ROUND_DOWN'))


def quote_sell(point: Point, qty_raw: int, decimals: int, pool_fee_bps: int) -> int:
    """Lamports out for `qty_raw` tokens in (rounded down)."""
    eff = _tokens(qty_raw, decimals) * (ONE - Decimal(pool_fee_bps) / 10000)
    if point.liquidity_sol is None:
        out = eff * point.price_sol
    else:
        reserve_tokens = point.liquidity_sol / point.price_sol
        out = point.liquidity_sol * eff / (reserve_tokens + eff)
    return int((out * LAMPORTS).to_integral_value(rounding='ROUND_DOWN'))


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

    def net_mark(path, point, pos):
        gross = quote_sell(point, pos.qty_raw, path.decimals, rcfg.pool_fee_bps)
        return max(0, gross * (paper.MAX_BPS - pcfg.slippage_bps) // paper.MAX_BPS - pcfg.fee_lamports)

    def try_enter(path, point, now):
        nonlocal cash, positions
        feats = dict(path.features)
        feats['mint'] = path.mint
        pre = S.entry_decision(feats, None, portfolio(now), cfg)
        if pre.action != 'BUY':
            skipped.append({'mint': path.mint, 'ts': point.ts, 'reasons': list(pre.reasons)})
            return
        lamports = paper.to_lamports(pre.size_sol)
        tokens = quote_buy(point, lamports, path.decimals, rcfg.pool_fee_bps)
        if tokens <= 0:
            skipped.append({'mint': path.mint, 'ts': point.ts, 'reasons': ['ZERO_OUTPUT']})
            return
        buy_q = paper.Quote(path.mint, 'buy', lamports, tokens, path.decimals, point.ts)
        try:
            fill = paper.buy(buy_q, pre.size_sol, pcfg)
        except paper.PaperError:
            skipped.append({'mint': path.mint, 'ts': point.ts, 'reasons': ['PAPER_REFUSED']})
            return
        back = quote_sell(point, fill.qty_raw, path.decimals, rcfg.pool_fee_bps)
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
        h.last_mark = net_mark(path, point, pos)

    def try_exit(path, point, now):
        nonlocal cash, positions
        pos = positions[path.mint]
        h = held[path.mint]
        h.last_mark = net_mark(path, point, pos)
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
        out = quote_sell(point, sell_qty, path.decimals, rcfg.pool_fee_bps)
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
            h.last_mark = net_mark(path, point, positions[path.mint])

    watches = {}
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
        elif i not in considered:
            triggered = watches.setdefault(i, _Watch(rcfg.entry_timing)).update(point)
            if ts - path.start_ts > rcfg.entry_timing.max_wait_s:
                considered.add(i)
                skipped.append({'mint': path.mint, 'ts': ts, 'reasons': ['ENTRY_TIMING_NO_TRIGGER']})
            elif triggered and not point.dead and ts >= path.start_ts + rcfg.entry_delay_s:
                considered.add(i)
                if path.mint in held:
                    skipped.append({'mint': path.mint, 'ts': ts, 'reasons': ['ALREADY_HELD']})
                else:
                    try_enter(path, point, now)
    for i, path in enumerate(paths):
        if i not in considered and path.points:
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


def load_paths(connection, *, since: Optional[float] = None, until: Optional[float] = None, default_decimals: int = DEFAULT_DECIMALS):
    """Build Paths from a lean store (any sqlite3 connection, read-only is enough). Returns (paths, skipped) where
    `skipped` counts every row/path that could not be used and why."""
    skipped: dict = {}

    def bump(name):
        skipped[name] = skipped.get(name, 0) + 1

    starts, marks = {}, {}
    cur = connection.execute("SELECT ts, kind, mint, raw, meta FROM observations WHERE kind IN ('path_start','path_mark') ORDER BY ts, id")
    for ts, kind, mint, raw, meta in cur:
        if not isinstance(mint, str) or not mint:
            bump('ROW_NO_MINT')
            continue
        if kind == 'path_start':
            payload = {**(_json_or_none(meta) or {}), **(_json_or_none(raw) or {})}  # L07: features in raw, entered/reason in meta
            if not isinstance(payload.get('features'), dict):
                bump('START_UNREADABLE')
            elif mint in starts:
                bump('START_DUPLICATE')  # first one wins; a later duplicate never moves the entry point
            else:
                starts[mint] = (float(ts), payload)
            continue
        body = _json_or_none(meta) or {}
        if 'price_sol' not in body and 'status' not in body:
            body = {**(_json_or_none(raw) or {}), **body}
        status = body.get('status', 'OK')
        if status in DEAD_STATUSES:
            marks.setdefault(mint, []).append(Point(float(ts), None, None, status))
            continue
        if status != 'OK':
            bump('MARK_' + (status if status == 'MALFORMED' else 'UNKNOWN_STATUS'))
            continue
        price = finite_positive(body.get('price_sol'))
        liq_raw = body.get('liquidity_sol')
        liq = finite_positive(liq_raw)
        if price is None:
            bump('MARK_BAD_PRICE')
        elif liq_raw is not None and liq is None:
            bump('MARK_BAD_LIQUIDITY')  # a corrupt reserve must not silently turn into "no price impact"
        else:
            marks.setdefault(mint, []).append(Point(float(ts), price, None if liq is None else liq / 2))  # two-sided -> SOL side
    paths = []
    for mint, (start_ts, body) in starts.items():
        if (since is not None and start_ts < since) or (until is not None and start_ts >= until):
            continue
        points = []
        seen = set()
        for p in marks.get(mint, []):
            if p.ts < start_ts:
                bump('MARK_BEFORE_START')
            elif p.ts in seen:
                bump('MARK_DUPLICATE_TS')
            else:
                seen.add(p.ts)
                points.append(p)
        if len(points) < MIN_MARKS:
            bump('PATH_TOO_FEW_MARKS')
            continue
        decimals = body['features'].get('decimals', default_decimals)
        if type(decimals) is not int or not 0 <= decimals <= 18:
            bump('PATH_BAD_DECIMALS')
            continue
        paths.append(Path(mint, start_ts, A.entry_features(body['features'], mint), tuple(points), decimals, body.get('reason')))
    for mint in marks:
        if mint not in starts:
            bump('MARKS_WITHOUT_START')
    paths.sort(key=lambda p: (p.start_ts, p.mint))
    return paths, skipped


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


def calibrate(connection, cfg: S.StrategyConfig, rcfg: Optional[ReplayConfig] = None) -> dict:
    """Replay `cfg` on the paths of the tokens the live trader actually bought and compare the ordered sell reasons.
    Mismatches are listed, never hidden. Live positions that are still open compare as a PREFIX of the replay."""
    rcfg = rcfg or ReplayConfig(initial_cash_sol=store_initial_cash_sol(connection) or ReplayConfig().initial_cash_sol)
    live = live_sells(connection)
    paths, skipped = load_paths(connection)
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
