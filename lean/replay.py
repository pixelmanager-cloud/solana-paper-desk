"""Offline replay of the lean strategy on recorded price paths. PAPER ONLY, EXECUTION_UNVERIFIED. Read-only, no network,
no clock, no randomness: the same paths and config always give the same trades.

Nothing here re-implements a rule. Entry and exit decisions are `lean.strategy.entry_decision` / `exit_decision`
called directly, fills are `lean.paper.buy` / `sell` / `apply_fill` (integer lamports, the same fee and adverse
slippage as the live trader), so a change to either module changes the replay with it.

Input contract (what L07 writes into `observations`; this module only reads):
  * `kind='path_start'`, `mint`, `ts` = when the candidate passed the hazard checks; `meta` JSON
    {"features": {market_cap_usd, liquidity_usd, reserve_sol, ...}, "not_entered_reason": str|null, "decimals": int(opt)}
  * `kind='path_mark'`, `mint`, `ts`; `meta` JSON {"price_sol": SOL per whole token, "liquidity_sol": SOL-side pool reserve}
    (the same two fields are accepted from a JSON `raw` body). `liquidity_sol` may be null: then fills are linear at the
    spot price with no price impact.
A path without a usable start, or with fewer than 2 usable marks, is skipped and counted; a corrupt mark (non-finite,
<= 0) is skipped and counted. Corrupt evidence never becomes an entry.

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
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional, Sequence

from lean import paper
from lean import strategy as S

ZERO, ONE = Decimal(0), Decimal(1)
LAMPORTS = Decimal(paper.LAMPORTS)
DAY = 86400
DEFAULT_DECIMALS = 6
MIN_MARKS = 2


class ReplayError(ValueError):
    pass


@dataclass(frozen=True)
class Point:
    ts: float
    price_sol: Decimal  # SOL per whole token
    liquidity_sol: Optional[Decimal] = None  # SOL-side reserve; None = unknown (no impact)


@dataclass(frozen=True)
class Path:
    mint: str
    start_ts: float
    features: dict
    points: tuple
    decimals: int = DEFAULT_DECIMALS
    not_entered_reason: Optional[str] = None  # what the live trader said; informational only


@dataclass(frozen=True)
class ReplayConfig:
    initial_cash_sol: Decimal = Decimal(5)
    pool_fee_bps: int = 30
    entry_delay_s: float = 0
    decimals: int = DEFAULT_DECIMALS

    def __post_init__(self):
        if not (isinstance(self.initial_cash_sol, Decimal) and self.initial_cash_sol.is_finite() and self.initial_cash_sol > 0):
            raise ReplayError('initial_cash_sol must be a positive Decimal')
        if type(self.pool_fee_bps) is not int or not 0 <= self.pool_fee_bps < 10000:
            raise ReplayError('pool_fee_bps must be an integer in [0, 10000)')
        if isinstance(self.entry_delay_s, bool) or not isinstance(self.entry_delay_s, (int, float)) or not math.isfinite(self.entry_delay_s) or self.entry_delay_s < 0:
            raise ReplayError('entry_delay_s must be a finite number >= 0')

    def to_dict(self):
        return {'initial_cash_sol': str(self.initial_cash_sol), 'pool_fee_bps': self.pool_fee_bps, 'entry_delay_s': self.entry_delay_s}


def paper_config(cfg: S.StrategyConfig) -> paper.PaperConfig:
    """The fee and slippage the live trader would use for this strategy config (exact conversion or an error)."""
    fee = cfg.fixed_fee_sol * LAMPORTS
    bps = cfg.adverse_slippage_bps
    if fee != fee.to_integral_value() or bps != bps.to_integral_value():
        raise ReplayError('fixed_fee_sol / adverse_slippage_bps are not whole lamports / bps')
    return paper.PaperConfig(fee_lamports=int(fee), slippage_bps=int(bps))


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
        rt = S.RoundTrip(S.Quote('buy', Decimal(lamports) / LAMPORTS, _tokens(tokens, path.decimals), now),
                         S.Quote('sell', _tokens(fill.qty_raw, path.decimals), Decimal(back) / LAMPORTS, now))
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
            return S.Position(mint=path.mint, opened_at=int(h.opened_ts), qty=_tokens(pos.qty_raw, path.decimals),
                              initial_qty=_tokens(h.initial_qty, path.decimals), cost_left=Decimal(pos.cost_lamports) / LAMPORTS,
                              stop_ratio=h.stop_ratio, stage=h.stage, peak_ratio=h.peak_ratio, touched_15=h.touched_15)

        mark = Decimal(h.last_mark) / LAMPORTS
        d = S.exit_decision(view(), mark, None, now, cfg, liquidate=liquidate, mark_at=now)
        _apply_updates(h, d)
        if d.action == 'BLOCKED':
            bump('BLOCKED_EXIT')
        if not (d.action == 'SELL' and d.quote_required):
            return
        fraction = d.qty / _tokens(pos.qty_raw, path.decimals)
        fraction = ONE if fraction >= ONE else fraction
        sell_qty = pos.qty_raw if fraction == ONE else int(Decimal(pos.qty_raw) * fraction)  # the formula paper.sell uses
        if sell_qty <= 0:
            bump('ZERO_SELL')
            return
        out = quote_sell(point, sell_qty, path.decimals, rcfg.pool_fee_bps)
        if out <= 0:
            bump('BLOCKED_EXIT')
            return
        sq = S.Quote('sell', d.qty, Decimal(out) / LAMPORTS, now)
        d2 = S.exit_decision(view(), mark, sq, now, cfg, liquidate=liquidate, mark_at=now)
        _apply_updates(h, d2)
        if d2.action != 'SELL':
            bump('BLOCKED_EXIT')
            return
        pq = paper.Quote(path.mint, 'sell', sell_qty, out, path.decimals, point.ts)
        fill = paper.sell(pos, pq, fraction, pcfg)
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

    for ts, i, j in events:
        path = paths[i]
        point = path.points[j]
        now = int(math.floor(ts))
        day = now // DAY
        if state['day'] != day:
            state['day'] = day
            state['day_start'] = Decimal(equity_lamports()) / LAMPORTS
        if path.mint in held and held[path.mint].path is path:
            try_exit(path, point, now)
        elif i not in considered and ts >= path.start_ts + rcfg.entry_delay_s:
            considered.add(i)
            if path.mint in held:
                skipped.append({'mint': path.mint, 'ts': ts, 'reasons': ['ALREADY_HELD']})
            else:
                try_enter(path, point, now)

    for mint, h in held.items():
        pos = positions[mint]
        last = h.path.points[-1]
        value = h.realized + h.last_mark - pos.cost_lamports  # realized so far + (what is left worth - its basis)
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
            body = _json_or_none(meta)
            if body is None or not isinstance(body.get('features'), dict):
                bump('START_UNREADABLE')
            elif mint in starts:
                bump('START_DUPLICATE')  # first one wins; a later duplicate never moves the entry point
            else:
                starts[mint] = (float(ts), body)
            continue
        body = _json_or_none(meta) or {}
        if 'price_sol' not in body:
            body = {**(_json_or_none(raw) or {}), **body}
        price = finite_positive(body.get('price_sol'))
        liq_raw = body.get('liquidity_sol')
        liq = finite_positive(liq_raw)
        if price is None:
            bump('MARK_BAD_PRICE')
        elif liq_raw is not None and liq is None:
            bump('MARK_BAD_LIQUIDITY')  # a corrupt reserve must not silently turn into "no price impact"
        else:
            marks.setdefault(mint, []).append(Point(float(ts), price, liq))
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
        decimals = body.get('decimals', default_decimals)
        if type(decimals) is not int or not 0 <= decimals <= 18:
            bump('PATH_BAD_DECIMALS')
            continue
        paths.append(Path(mint, start_ts, dict(body['features']), tuple(points), decimals, body.get('not_entered_reason')))
    for mint in marks:
        if mint not in starts:
            bump('MARKS_WITHOUT_START')
    paths.sort(key=lambda p: (p.start_ts, p.mint))
    return paths, skipped


# ---------------------------------------------------------------------------------------------- calibration
def live_sells(connection) -> dict:
    """mint -> ordered list of live SELL reasons, and the set of mints that were bought, from the generic `events` table the
    runner writes (kind 'fill', payload {side, mint, reason?}). A sell without a reason is recorded as None, never guessed."""
    sells: dict = {}
    bought: list = []
    still_open: set = set()
    try:
        for mint, qty_after in connection.execute('SELECT mint, qty_after FROM fills ORDER BY id'):
            (still_open.add if qty_after else still_open.discard)(mint)
    except Exception:
        still_open = set()  # no typed fills table: every live position is treated as closed (exact comparison)
    try:
        cur = connection.execute("SELECT kind, payload FROM events WHERE kind='fill' ORDER BY id")
    except Exception:
        return {'bought': [], 'sells': {}, 'open': still_open}
    for kind, payload in cur:
        body = _json_or_none(payload)
        if not body or not isinstance(body.get('mint'), str):
            continue
        if body.get('side') == 'buy':
            if body['mint'] not in bought:
                bought.append(body['mint'])
        elif body.get('side') == 'sell':
            sells.setdefault(body['mint'], []).append(body.get('reason'))
    return {'bought': bought, 'sells': sells, 'open': still_open}


def calibrate(connection, cfg: S.StrategyConfig, rcfg: Optional[ReplayConfig] = None) -> dict:
    """Replay `cfg` on the paths of the tokens the live trader actually bought and compare the ordered sell reasons.
    Mismatches are listed, never hidden. Live positions that are still open compare as a PREFIX of the replay."""
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
            'strategy_version': cfg.strategy_version, 'config_hash': cfg.config_hash, 'replay_assumptions': (rcfg or ReplayConfig()).to_dict()}
