"""Outcome labels for recorded price paths (L19). Paper only, offline, read-only: nothing here trades or calls a provider.

Every candidate that has a recorded path in the recorder's ``paths.sqlite`` (``lean.paths``: entered or not, hazard-free) gets ONE
label, computed from its ``path_marks`` starting at the screen/entry time:

  RUG   the mark falls to ``rug_price_ratio`` (0.2 = -80%) of its cost within ``rug_window_s`` (1 h), OR the spendable quote reserve
        ("liquidity") falls by ``rug_liquidity_drop`` (0.7) or more from the first mark, OR the pool is gone (a dead mark)
  WIN   the mark reaches ``win_ratio`` (the strategy's first take-profit rung, 1.4 = +40%) BEFORE the stop line
  LOSS  the mark reaches the stop line (1 - stop_fraction, 0.82) first
  FLAT  none of those within the recorded path

Precedence is by time: the first of {win, stop, rug} decides, except that a rug always beats a stop that came before it (a stop-out
followed by the pool collapsing is a RUG: that is what the label is for) and never beats a win that came before it (the trader
would have banked TP1). A rug and a win on the same mark is a RUG. The mark is ``lean.adapters.mark`` (constant-product output of
the reference quantity on the recorded vault amounts, less the pool fee and the fixed fee, no slippage haircut): the very function
the live position loop marks with, divided by the reference cost basis stored with the path. Also recorded: ``max_up``,
``max_down`` (fractions vs cost) and the seconds from the start to each extreme.

A label is a pure function of the marks: ``label_version`` says which definition produced it. There is at most one label per mint
(a path carried over into a newer recorder file keeps its first start; ``lean.replay.load_paths`` merges the marks).
"""
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from lean import adapters as A, paper, replay as R

LABEL_VERSION = 1
LABELS = ('RUG', 'WIN', 'LOSS', 'FLAT')
DEFAULT_POOL_FEE_BPS = 25                 # the runner's default (lean.json pool_fee_bps)


class LabelError(ValueError):
    pass


@dataclass(frozen=True)
class LabelConfig:
    win_ratio: Decimal = Decimal('1.4')
    stop_ratio: Decimal = Decimal('0.82')
    rug_price_ratio: Decimal = Decimal('0.2')
    rug_window_s: float = 3600.0
    rug_liquidity_drop: Decimal = Decimal('0.7')
    pool_fee_bps: int = DEFAULT_POOL_FEE_BPS
    fee_lamports: int = 50_000                                # the strategy's fixed fee, taken from its paper config

    def __post_init__(self):
        for name in ('win_ratio', 'stop_ratio', 'rug_price_ratio', 'rug_liquidity_drop'):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise LabelError('%s must be a finite positive Decimal' % name)
        if not self.stop_ratio < 1 < self.win_ratio:
            raise LabelError('need stop_ratio < 1 < win_ratio')
        if not self.rug_price_ratio < self.stop_ratio:
            raise LabelError('rug_price_ratio must be below the stop line')
        if not self.rug_liquidity_drop < 1:
            raise LabelError('rug_liquidity_drop must be below 1')
        if isinstance(self.rug_window_s, bool) or not isinstance(self.rug_window_s, (int, float)) or not math.isfinite(self.rug_window_s) or self.rug_window_s <= 0:
            raise LabelError('rug_window_s must be a positive finite number')
        if isinstance(self.pool_fee_bps, bool) or not isinstance(self.pool_fee_bps, int) or not 0 <= self.pool_fee_bps < 10000:
            raise LabelError('pool_fee_bps must be an integer in [0, 10000)')
        if isinstance(self.fee_lamports, bool) or not isinstance(self.fee_lamports, int) or self.fee_lamports < 0:
            raise LabelError('fee_lamports must be an integer >= 0')

    @classmethod
    def from_strategy(cls, strategy, pool_fee_bps=DEFAULT_POOL_FEE_BPS, **over):
        """The win / stop lines of the live strategy (its first take-profit rung and its stop) and its fixed paper fee."""
        pcfg = R.paper_config(strategy)
        return cls(win_ratio=Decimal(strategy.tp_ladder[0].trigger), stop_ratio=Decimal(1) - Decimal(strategy.stop_fraction),
                   pool_fee_bps=pool_fee_bps, fee_lamports=pcfg.fee_lamports, **over)

    def to_dict(self):
        return {'label_version': LABEL_VERSION, 'win_ratio': str(self.win_ratio), 'stop_ratio': str(self.stop_ratio),
                'rug_price_ratio': str(self.rug_price_ratio), 'rug_window_s': float(self.rug_window_s),
                'rug_liquidity_drop': str(self.rug_liquidity_drop), 'pool_fee_bps': self.pool_fee_bps, 'fee_lamports': self.fee_lamports}


@dataclass(frozen=True)
class Label:
    mint: str
    label: str
    label_version: int
    start_ts: float
    n_marks: int
    t_event: Optional[float]                # seconds from the start to the mark that decided the label (None for FLAT)
    rug_cause: Optional[str]                # PRICE | LIQUIDITY | POOL_DEAD (only for RUG)
    max_up: float                           # best mark vs cost - 1
    max_down: float                         # worst mark vs cost - 1
    t_max_up: float                         # seconds from the start to the best mark
    t_max_down: float                       # seconds from the start to the worst mark
    end_return: float                       # the last mark vs cost - 1 (buy and hold to the end of the recorded path)

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def mark_ratio(point, qty_raw, cost_lamports, cfg: LabelConfig) -> Decimal:
    """The live mark of the reference quantity at this point over its cost; a dead mark is worth nothing."""
    if point.dead:
        return Decimal(0)
    pcfg = paper.PaperConfig(fee_lamports=cfg.fee_lamports, slippage_bps=0)
    value = A.mark(qty_raw, point.base_raw, point.quote_raw, pool_fee_bps=cfg.pool_fee_bps, pcfg=pcfg)
    return value * 10 ** 9 / Decimal(cost_lamports)


def label_points(mint, start_ts, points, qty_raw, cost_lamports, fee_raw, cfg: LabelConfig) -> Optional[Label]:
    """One label from the marks at or after ``start_ts`` (earlier marks are never looked at). None without a usable mark."""
    if type(qty_raw) is not int or type(cost_lamports) is not int or qty_raw <= 0 or cost_lamports <= 0:
        raise LabelError('reference quantity and cost must be positive integers')
    ordered = sorted((p for p in points if p.ts >= start_ts), key=lambda p: p.ts)
    if not ordered:
        return None
    first_live = next((p for p in ordered if not p.dead), None)
    base_liquidity = None if first_live is None else first_live.quote_raw - fee_raw
    t_win = t_stop = t_rug = None
    cause = None
    best = worst = None
    last = None
    for p in ordered:
        age = p.ts - start_ts
        ratio = mark_ratio(p, qty_raw, cost_lamports, cfg)
        last = ratio
        if best is None or ratio > best[0]:
            best = (ratio, age)
        if worst is None or ratio < worst[0]:
            worst = (ratio, age)
        if t_win is None and ratio >= cfg.win_ratio:
            t_win = age
        if t_stop is None and ratio <= cfg.stop_ratio:
            t_stop = age
        if t_rug is None:
            if p.dead:
                t_rug, cause = age, 'POOL_DEAD'
            elif age <= cfg.rug_window_s and ratio <= cfg.rug_price_ratio:
                t_rug, cause = age, 'PRICE'
            elif base_liquidity and base_liquidity > 0 and Decimal(p.quote_raw - fee_raw) <= Decimal(base_liquidity) * (1 - cfg.rug_liquidity_drop):
                t_rug, cause = age, 'LIQUIDITY'
    if t_rug is not None and (t_win is None or t_rug <= t_win):
        label, t_event = 'RUG', t_rug
    elif t_win is not None and (t_stop is None or t_win < t_stop):
        label, t_event, cause = 'WIN', t_win, None
    elif t_stop is not None:
        label, t_event, cause = 'LOSS', t_stop, None
    else:
        label, t_event, cause = 'FLAT', None, None
    return Label(mint, label, LABEL_VERSION, start_ts, len(ordered), t_event, cause if label == 'RUG' else None,
                 float(best[0] - 1), float(worst[0] - 1), best[1], worst[1], float(last - 1))


def references(connections) -> dict:
    """``{mint: (qty_raw, cost_lamports)}`` from each path's recorded ``target`` (the quantity ``adapters.mark`` marks and what it
    cost). The first row of a mint wins, as in ``lean.replay.load_paths``."""
    import json
    out = {}
    for connection in connections:
        for mint, target in connection.execute('SELECT mint, target FROM paths ORDER BY start_ts, mint'):
            if mint in out:
                continue
            try:
                t = json.loads(target)
                qty, cost = t['qty_raw'], t['cost_lamports']
            except (ValueError, TypeError, KeyError):
                out[mint] = None
                continue
            out[mint] = (qty, cost) if type(qty) is int and type(cost) is int and qty > 0 and cost > 0 else None
    return out


def label_paths(paths, refs, cfg: LabelConfig):
    """-> ({mint: Label}, skipped). ``paths`` are ``lean.replay.Path`` objects; ``refs`` from ``references``."""
    labels, skipped = {}, {}
    for path in paths:
        ref = refs.get(path.mint)
        if ref is None:
            skipped['LABEL_NO_REFERENCE'] = skipped.get('LABEL_NO_REFERENCE', 0) + 1
            continue
        label = label_points(path.mint, path.start_ts, path.points, ref[0], ref[1], path.fee_raw, cfg)
        if label is None:
            skipped['LABEL_NO_MARKS'] = skipped.get('LABEL_NO_MARKS', 0) + 1
            continue
        if path.mint in labels:
            skipped['LABEL_DUPLICATE'] = skipped.get('LABEL_DUPLICATE', 0) + 1     # one label per candidate, ever
            continue
        labels[path.mint] = label
    return labels, skipped
