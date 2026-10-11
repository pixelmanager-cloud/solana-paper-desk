"""Outcome labels and the live-exit PnL of recorded price paths (L19, reworked in LINT3). PAPER ONLY, offline, read-only:
nothing here trades, calls a provider, or writes a file or a database.

Input: the L07R2 recorder's OWN file ``paths.sqlite`` (``lean.paths``) and its rolled-over archives ``paths-*.sqlite`` beside it,
each opened with plain ``mode=ro`` (``lean.report.open_ro``). The tables read are exactly the recorder's schema:
  paths       (mint, candidate_id, start_ts, ends_ts, entered, stage, reason, screen_reasons, holders_checked, features, target, ...)
  path_marks  (mint, ts, slot, base_raw, quote_raw, net_sol, liquidity_sol, status)
  path_gaps   (mint, ts, cause)                 a due mark that was NOT taken (shed, budget, a provider code, stopped, disk_low...)
  path_ends   (mint, ts, why, marks, gaps)      COMPLETE / EXPIRED_WHILE_DOWN
A path carried over into a newer file (``carried_from``) keeps its oldest start; its marks and gaps from every file are merged.

The decision. A label starts at the candidate's DECISION time (``decision_ts``): the trader's last screen decision of the candidate
at or before the path start, or the entry decision that followed it (the same definition as the L12F v3 feature row). It is read
from ``lean.sqlite`` ``decisions`` by the caller (``lean.learn``); this module only takes it as an argument. Nothing at or before it
is a label input except the reference cost.

The reference position. The mark is ``lean.adapters.mark`` (THE function the live position loop marks with: constant-product output
of the held quantity on the recorded vault amounts, less the pool fee and the fixed paper fee) of a position that is what the live
paper trader would hold: the real fill for an entered candidate (``target.basis == 'fill'``: its quantity and cost, fee included),
else a ``lean.paper.buy`` of the recorder's reference quote (``ref_size_sol`` at the screen reserves; the paper slippage haircut on
the tokens and the fixed fee on the cost). The mark ratio is ``mark / cost``.

Labels (``LABEL_VERSION`` 2):
  RUG   within ``rug_window_s`` (1 h) the ratio falls to ``rug_price_ratio`` (0.2 = -80%) or below, OR at any time the spendable quote
        reserve falls by ``rug_liquidity_drop`` (70%) or more from the DECISION-time reserve (the screen's ``quote_spendable_raw``),
        OR the pool is gone (an ACCOUNT_MISSING / EMPTY_POOL mark)
  WIN   the ratio reaches the strategy's first take-profit trigger (+40%) BEFORE the stop line (``initial_stop_ratio``, -18%)
  LOSS  the stop line first
  FLAT  none of those over the whole recorded path (the path must have ENDED; an unfinished path is never FLAT)
  INSUFFICIENT_PATH  the path is not observed well enough to say: no mark, too many gaps (``max_gap_fraction`` of the due marks up to
        the deciding mark, a gap row or a MALFORMED mark being a missing observation), a hole longer than ``max_hole_s`` between two
        observations (or before the first one, or after the last one for FLAT), or a FLAT path that has not ended yet.
        A gap is NEVER read as a flat price: a missing mark is missing, and too many of them make the path unusable.
Precedence is by time: the first of {win, stop, rug} decides, except that a rug beats a stop that came before it and never beats a
win that came before it (TP1 would have been banked). A rug and a win on the same mark is a RUG.

The live-exit PnL (``simulate_exits``) is a MINIMAL replay: the same reference position, every recorded mark through
``lean.strategy.exit_decision`` exactly as ``lean.runner._manage`` calls it (mark, then a constant-product sell quote after the pool
fee, then ``exit_decision`` again with that quote, then ``lean.paper.sell``), the ladder state carried as the runner carries it. One
path at a time on its own position (no portfolio contention). A dead pool can neither be marked nor sold; a path that ends dead is a
``RUG_WRITEOFF`` of what is left; a position still open at the end is ``OPEN_AT_END`` valued at its last mark. Not modelled: the L10
latency fills and the L11 held-risk DANGER exits (both act on live data the recorder does not keep).
"""
import json
import math
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from lean import adapters as A, paper, strategy as S

LABEL_VERSION = 2
LABELS = ('RUG', 'WIN', 'LOSS', 'FLAT')
INSUFFICIENT = 'INSUFFICIENT_PATH'
DEAD_STATUSES = ('ACCOUNT_MISSING', 'EMPTY_POOL')
DEFAULT_POOL_FEE_BPS = 25                 # the runner's default (lean.json pool_fee_bps)
BIG_CASH = 10 ** 18                       # one isolated position: the cash never binds


class LabelError(ValueError):
    pass


# ------------------------------------------------------------------------------------------------------------- config
@dataclass(frozen=True)
class LabelConfig:
    win_ratio: Decimal = Decimal('1.4')
    stop_ratio: Decimal = Decimal('0.82')
    rug_price_ratio: Decimal = Decimal('0.2')
    rug_window_s: float = 3600.0
    rug_liquidity_drop: Decimal = Decimal('0.7')
    max_gap_fraction: float = 0.25        # missing observations / due observations, up to the deciding mark
    max_hole_s: float = 180.0             # the longest time without an observation (the recorder polls every 15 s)

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
        for name in ('rug_window_s', 'max_hole_s'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise LabelError('%s must be a positive finite number' % name)
        g = self.max_gap_fraction
        if isinstance(g, bool) or not isinstance(g, (int, float)) or not math.isfinite(g) or not 0 <= g < 1:
            raise LabelError('max_gap_fraction must be in [0, 1)')

    @classmethod
    def from_strategy(cls, strategy, **over):
        """The win / stop lines of the live strategy: its first take-profit trigger and its initial stop ratio."""
        return cls(win_ratio=Decimal(strategy.tp_ladder[0].trigger), stop_ratio=Decimal(strategy.initial_stop_ratio), **over)

    def to_dict(self):
        return {'label_version': LABEL_VERSION, 'win_ratio': str(self.win_ratio), 'stop_ratio': str(self.stop_ratio),
                'rug_price_ratio': str(self.rug_price_ratio), 'rug_window_s': float(self.rug_window_s),
                'rug_liquidity_drop': str(self.rug_liquidity_drop), 'max_gap_fraction': float(self.max_gap_fraction),
                'max_hole_s': float(self.max_hole_s)}


# ------------------------------------------------------------------------------------------------------------- paths
@dataclass(frozen=True)
class Point:
    ts: float
    base_raw: Optional[int] = None        # the pool's base (token) vault amount; None for a dead mark
    quote_raw: Optional[int] = None       # the pool's quote (SOL) vault amount in lamports; None for a dead mark
    status: str = 'OK'                    # OK | ACCOUNT_MISSING | EMPTY_POOL

    @property
    def dead(self):
        return self.status != 'OK'


@dataclass(frozen=True)
class PathRecord:
    mint: str
    candidate_id: Optional[int]
    start_ts: float
    ends_ts: float
    entered: bool
    screen_reasons: tuple                 # the screen's reject reasons ([] = the screen passed)
    reasons: tuple                        # why it was not entered ([] when entered)
    features: dict                        # the screen features (JSON) of the path row
    target: dict                          # the recorder's reference: decimals, fee_raw, qty_raw, cost_lamports, basis
    points: tuple                         # Points, by time
    missing: tuple                        # times of the observations that were due but not taken (path_gaps + MALFORMED marks)
    ended: Optional[str] = None           # path_ends.why, None while the path is live

    @property
    def decimals(self):
        return int(self.target['decimals'])

    @property
    def fee_raw(self):
        return int(self.target.get('fee_raw', 0))


def path_files(paths_db):
    """The recorder file and its rolled-over archives beside it (``paths-*.sqlite``), archives first. Missing files are skipped."""
    main = Path(paths_db)
    out = [p for p in sorted(main.parent.glob('paths-*.sqlite')) if p.is_file() and not p.is_symlink()]
    return out + [main]


def open_paths(paths_db, extra=()):
    """Read-only (plain ``mode=ro``, ``query_only``) connections to the recorder file, its archives and any ``extra`` files."""
    from lean.report import open_ro
    files, seen = [], set()
    for f in list(extra) + path_files(paths_db):
        key = str(Path(f).resolve())
        if key not in seen:
            seen.add(key)
            files.append(f)
    return [open_ro(f) for f in files]


def _json(value, kind):
    try:
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode('utf-8')
        out = json.loads(value)
        return out if isinstance(out, kind) else None
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def iter_paths(connections, skipped):
    """PathRecords one at a time, oldest start first, from one or several recorder files (any order). Only ONE path's marks are
    in memory at a time (indexed per-mint reads: ``path_marks_mint_ts`` / ``path_gaps_mint_ts``), so a multi-GB recorder history
    fits a small memory cap. A mint's row with the OLDEST start wins (a carried-over copy keeps the original start); marks and gaps
    of every file are merged (duplicates by time collapse). Corrupt rows are skipped and counted in ``skipped``, never repaired."""
    if isinstance(connections, sqlite3.Connection):
        connections = [connections]

    def bump(name, n=1):
        skipped[name] = skipped.get(name, 0) + n
    rows, mark_mints = {}, set()
    for connection in connections:
        for (mint, cid, start_ts, ends_ts, entered, reason, screen_reasons, features, target) in connection.execute(
                'SELECT mint, candidate_id, start_ts, ends_ts, entered, reason, screen_reasons, features, target FROM paths'):
            if not isinstance(mint, str) or not mint or not _finite(start_ts) or not _finite(ends_ts):
                bump('PATH_ROW_UNREADABLE')
                continue
            if mint in rows:
                bump('PATH_CARRIED_OVER')
                if float(start_ts) >= rows[mint][0]:
                    continue
            rows[mint] = (float(start_ts), float(ends_ts), cid if type(cid) is int else None, bool(entered), screen_reasons, reason,
                          features, target)
        mark_mints.update(m for (m,) in connection.execute('SELECT DISTINCT mint FROM path_marks'))
    for mint in mark_mints - set(rows):
        bump('MARKS_WITHOUT_PATH')
    for mint, (start_ts, ends_ts, cid, entered, screen_reasons, reason, features, target) in sorted(rows.items(), key=lambda kv: (kv[1][0], kv[0])):
        features, target = _json(features, dict), _json(target, dict)
        if features is None or target is None:
            bump('PATH_ROW_UNREADABLE')
            continue
        marks, missing, ended = {}, set(), None
        for connection in connections:
            for ts, base, quote, status in connection.execute('SELECT ts, base_raw, quote_raw, status FROM path_marks WHERE mint=?', (mint,)):
                if not _finite(ts):
                    bump('MARK_UNREADABLE')
                    continue
                if status in DEAD_STATUSES:
                    marks.setdefault(float(ts), Point(float(ts), None, None, status))
                elif status == 'OK' and type(base) is int and type(quote) is int and base > 0 and quote > 0:
                    marks.setdefault(float(ts), Point(float(ts), base, quote))
                else:                                               # MALFORMED / corrupt amounts: an observation that was NOT made
                    bump('MARK_NOT_USABLE')
                    missing.add(float(ts))
            missing.update(float(ts) for (ts,) in connection.execute('SELECT ts FROM path_gaps WHERE mint=?', (mint,)) if _finite(ts))
            end = connection.execute('SELECT why FROM path_ends WHERE mint=?', (mint,)).fetchone()
            if end is not None and ended is None:
                ended = str(end[0])
        yield PathRecord(mint, cid, start_ts, ends_ts, entered, tuple(_json(screen_reasons, list) or ()), tuple(_json(reason, list) or ()),
                         features, target, tuple(sorted(marks.values(), key=lambda p: p.ts)),
                         tuple(sorted(t for t in missing if t not in marks)), ended)


def load_paths(connections):
    """``(paths, skipped)``: ``iter_paths`` as a list (small files and tests)."""
    skipped = {}
    return list(iter_paths(connections, skipped)), skipped


# ------------------------------------------------------------------------------------------------------------- position
@dataclass(frozen=True)
class Reference:
    qty_raw: int                          # what the live paper trader would hold
    cost_lamports: int                    # its cost basis, fees included
    decimals: int
    basis: str                            # 'fill' | 'ref_size'


def reference(path: PathRecord, pcfg: paper.PaperConfig) -> Reference:
    """The position the live paper trader holds (``fill``) or would hold (``ref_size``: a ``paper.buy`` of the recorder's reference
    quote: slippage on the tokens, the fixed fee on the cost). LabelError when the target is unusable."""
    t = path.target
    qty, cost, basis = t.get('qty_raw'), t.get('cost_lamports'), t.get('basis')
    decimals = t.get('decimals')
    if type(qty) is not int or type(cost) is not int or qty <= 0 or cost <= 0 or type(decimals) is not int or not 0 <= decimals <= 30:
        raise LabelError('unusable reference target')
    if basis == 'fill':
        return Reference(qty, cost, decimals, basis)
    if basis != 'ref_size':
        raise LabelError('unknown reference basis')
    try:
        fill = paper.buy(paper.Quote(path.mint, 'buy', cost, qty, decimals, path.start_ts), A.sol(cost), pcfg)
    except paper.PaperError as error:
        raise LabelError(str(error)) from None
    return Reference(fill.qty_raw, fill.sol_lamports + fill.fee_lamports, decimals, basis)


def mark_lamports(point: Point, qty_raw: int, pool_fee_bps: int, pcfg) -> int:
    """THE live mark (``lean.adapters.mark``) in lamports; a dead or empty pool is worth nothing."""
    if point.dead:
        return 0
    try:
        return int(A.mark(qty_raw, point.base_raw, point.quote_raw, pool_fee_bps=pool_fee_bps, pcfg=pcfg) * paper.LAMPORTS)
    except A.AdapterError:
        return 0


# ------------------------------------------------------------------------------------------------------------- labels
@dataclass(frozen=True)
class Label:
    mint: str
    label: str                            # RUG | WIN | LOSS | FLAT | INSUFFICIENT_PATH
    label_version: int
    decision_ts: float
    n_marks: int
    t_event: Optional[float]              # seconds from the decision to the deciding mark (None for FLAT / INSUFFICIENT_PATH)
    rug_cause: Optional[str]              # PRICE | LIQUIDITY | POOL_DEAD (RUG only)
    max_up: Optional[float]               # best ratio - 1 (over the observed marks)
    max_down: Optional[float]
    t_max_up: Optional[float]
    t_max_down: Optional[float]
    end_return: Optional[float]           # the last mark's ratio - 1
    gap_fraction: float = 0.0
    max_hole_s: float = 0.0
    why_insufficient: Optional[str] = None

    @property
    def usable(self):
        return self.label in LABELS

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _coverage(decision_ts, until, points, missing):
    """(gap fraction, longest hole in seconds) of the observations in [decision_ts, until]."""
    seen = [p.ts for p in points if decision_ts <= p.ts <= until]
    lost = [t for t in missing if decision_ts <= t <= until]
    due = len(seen) + len(lost)
    fraction = len(lost) / due if due else 1.0
    hole, last = 0.0, decision_ts
    for ts in seen:
        hole, last = max(hole, ts - last), ts
    return fraction, max(hole, until - last)


def label_path(path: PathRecord, decision_ts: float, ref: Reference, cfg: LabelConfig, *, pool_fee_bps, pcfg) -> Label:
    """ONE label from the marks at or after ``decision_ts`` (an earlier mark is never looked at)."""
    points = [p for p in path.points if p.ts >= decision_ts]
    missing = [t for t in path.missing if t >= decision_ts]

    def insufficient(why, n=0, fraction=1.0, hole=0.0):
        return Label(path.mint, INSUFFICIENT, LABEL_VERSION, decision_ts, n, None, None, None, None, None, None, None,
                     round(fraction, 6), round(hole, 3), why)
    if not points:
        return insufficient('NO_MARK')
    spendable0 = path.features.get('quote_spendable_raw')
    try:
        base_liquidity = int(spendable0)
    except (TypeError, ValueError):
        base_liquidity = None
    if base_liquidity is None or base_liquidity <= 0:                      # no decision-time reserve: the first live mark
        first_live = next((p for p in points if not p.dead), None)
        base_liquidity = None if first_live is None else first_live.quote_raw - path.fee_raw
    floor = None if not base_liquidity or base_liquidity <= 0 else Decimal(base_liquidity) * (1 - cfg.rug_liquidity_drop)
    t_win = t_stop = t_rug = None
    cause = None
    best = worst = None
    last = None
    cost = Decimal(ref.cost_lamports)
    for p in points:
        age = p.ts - decision_ts
        ratio = Decimal(mark_lamports(p, ref.qty_raw, pool_fee_bps, pcfg)) / cost
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
            elif floor is not None and Decimal(p.quote_raw - path.fee_raw) <= floor:
                t_rug, cause = age, 'LIQUIDITY'
    if t_rug is not None and (t_win is None or t_rug <= t_win):
        name, t_event = 'RUG', t_rug
    elif t_win is not None and (t_stop is None or t_win < t_stop):
        name, t_event, cause = 'WIN', t_win, None
    elif t_stop is not None:
        name, t_event, cause = 'LOSS', t_stop, None
    else:
        name, t_event, cause = 'FLAT', None, None
    if name == 'FLAT':
        if path.ended is None:
            return insufficient('PATH_NOT_ENDED', len(points))
        until = max(path.ends_ts, points[-1].ts) if path.ended == 'COMPLETE' else path.ends_ts
    else:
        until = decision_ts + t_event
    fraction, hole = _coverage(decision_ts, until, points, missing)
    if fraction > cfg.max_gap_fraction:
        return insufficient('TOO_MANY_GAPS', len(points), fraction, hole)
    if hole > cfg.max_hole_s:
        return insufficient('HOLE', len(points), fraction, hole)
    return Label(path.mint, name, LABEL_VERSION, decision_ts, len(points), t_event, cause if name == 'RUG' else None,
                 float(best[0] - 1), float(worst[0] - 1), best[1], worst[1], float(last - 1), round(fraction, 6), round(hole, 3))


# ------------------------------------------------------------------------------------------------------------- live exits
@dataclass
class SimTrade:
    mint: str
    status: str                           # CLOSED | OPEN_AT_END | RUG_WRITEOFF
    pnl_lamports: int                     # realized over every sell (fees included); OPEN_AT_END: + what is left at its last mark
    cost_lamports: int
    exit_reason: Optional[str]
    reasons: list = field(default_factory=list)
    exit_ts: Optional[float] = None


def sell_quote_lamports(point: Point, qty_raw: int, pool_fee_bps: int) -> int:
    """Lamports out for ``qty_raw`` tokens: the constant-product output ``adapters.mark`` uses, after the pool fee."""
    return point.quote_raw * qty_raw // (point.base_raw + qty_raw) * (paper.MAX_BPS - pool_fee_bps) // paper.MAX_BPS


def simulate_exits(path: PathRecord, decision_ts: float, ref: Reference, strategy: S.StrategyConfig, *, pool_fee_bps, pcfg,
                   until: Optional[float] = None) -> SimTrade:
    """The live exit rules on one path from the decision: ``strategy.exit_decision`` at every recorded mark, as the runner calls it."""
    buy = paper.Fill(ts=float(decision_ts), mint=path.mint, side='buy', qty_raw=ref.qty_raw, sol_lamports=ref.cost_lamports,
                     fee_lamports=0, slippage_bps=0, decimals=ref.decimals)
    positions, cash = paper.apply_fill({}, BIG_CASH, buy)
    state = A.apply_updates({'initial_qty_raw': ref.qty_raw}, S.new_position_fields(strategy))     # the runner's open state
    realized, reasons, last_value, last = 0, [], ref.cost_lamports, None
    for point in path.points:
        if point.ts < decision_ts or (until is not None and point.ts > until):
            continue
        last = point
        if path.mint not in positions:
            break
        pos = positions[path.mint]
        if point.dead:
            last_value = 0                                    # a dead pool is worth nothing and cannot be sold into
            continue
        now = int(math.floor(point.ts))
        value = mark_lamports(point, pos.qty_raw, pool_fee_bps, pcfg)
        last_value = value
        mark = A.sol(value)
        held = A.strategy_position(pos, state)
        decision = S.exit_decision(held, mark, None, now, strategy, mark_at=now)
        if decision.position_updates:
            state = A.apply_updates(state, decision.position_updates)
            held = A.strategy_position(pos, state)
        if decision.action != 'SELL' or not decision.quote_required:
            continue
        try:
            qty = A.sell_qty_raw(decision.qty, pos.qty_raw)
        except A.AdapterError:
            continue
        out = sell_quote_lamports(point, qty, pool_fee_bps)
        if out <= 0:
            continue
        quote = paper.Quote(path.mint, 'sell', qty, out, ref.decimals, point.ts)
        final = S.exit_decision(held, mark, A.strategy_quote(quote, 'sell', now), now, strategy, mark_at=now)
        if final.action != 'SELL' or A.sell_qty_raw(final.qty, pos.qty_raw) != qty:
            continue
        fill = paper.sell(pos, quote, cfg=pcfg, qty_raw=qty)
        positions, cash = paper.apply_fill(positions, cash, fill)
        realized += fill.realized_lamports
        reasons.append(final.reason)
        if path.mint not in positions:
            return SimTrade(path.mint, 'CLOSED', realized, ref.cost_lamports, final.reason, reasons, point.ts)
        state = A.apply_updates(state, final.after_fill)
        last_value = mark_lamports(point, positions[path.mint].qty_raw, pool_fee_bps, pcfg)
    pos = positions[path.mint]
    if last is not None and last.dead:
        return SimTrade(path.mint, 'RUG_WRITEOFF', realized - pos.cost_lamports, ref.cost_lamports, 'RUG_WRITEOFF',
                        reasons + ['RUG_WRITEOFF'], last.ts)
    return SimTrade(path.mint, 'OPEN_AT_END', realized + last_value - pos.cost_lamports, ref.cost_lamports, None, reasons,
                    None if last is None else last.ts)

