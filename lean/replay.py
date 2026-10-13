"""Offline exit-policy replay on recorded price paths (C1). PAPER ONLY, read-only, no network, no runner hook.

The input is what the path recorder (``lean.paths``) wrote: ``paths`` / ``path_marks`` / ``path_gaps`` / ``path_ends`` in
``paths.sqlite`` and the rolled-over ``paths-*.sqlite`` archives, opened in plain ``mode=ro``. For every path a
hypothetical entry is replayed under an exit POLICY, and the policy's decisions are made by the REAL
``lean.strategy.exit_decision`` (the function the live position loop calls), on marks computed by the REAL
``lean.adapters.mark`` from the stored raw reserves:

  entry   at the first usable mark of the path, ``entry.size_sol`` spent. Costs: the PumpSwap fee of the market-cap TIER
          at that mark (``fixtures/mainnet-dynamic-fees.json``: lp + protocol + creator bps, by market cap in lamports),
          ``slippage_bps`` adverse slippage, and ``fee_sol`` per fill.
  exit    the decision is taken on a mark; the SELL fills at the NEXT available mark (``lag`` 1, the default; ``lag`` 0 fills at the
          decision mark) with the same cost model on that mark's reserves and tier. If the path ends first, the position is
          closed at its last mark (reason PATH_END, counted).
  data    ``path_gaps`` are MISSING data, never a flat price: nothing is forward-filled, decisions only exist at marks that
          were really read, and a fill that crosses a recorded gap is counted. ``ACCOUNT_MISSING`` / ``MALFORMED`` marks are
          missing data; ``EMPTY_POOL`` is a drained pool = a mark of zero.

Exit families (``Policy``; ``stop`` omitted = no stop except a drained pool, only ``live_ladder`` defaults to the configured
stop), each a parameterisation of ``exit_decision`` (a ``StrategyConfig`` copy plus the starting
position fields; the decision function itself is never reimplemented):
  live_ladder        the configured ladder / stop / trailing / time stop (optionally overriding stop, trailing, hold)
  full_tp            sell everything when the net ratio reaches ``x`` (stop optional, hold cap optional)
  hold               sell everything after ``minutes`` (stop optional)
  tp_time_cap        full TP at ``x`` or a hard cap after ``minutes``
  trailing           a trailing stop of ``fraction`` armed from the entry (stop optional)
  partial_tp_trail   sell ``sell_fraction`` at ``x``, then trail the rest by ``fraction`` (the one family that sets a position
                     field after a fill, to arm the trailing stop)
The ratio is mark / cost, both net of the same fixed fee, exactly as live.

Mark models (LINT5). A mark row the recorder wrote with ``marks.model: effective`` (M1: ``path_marks.model / virtual_raw /
fee_bps``) is replayed with THAT row's model and fee: the reserves are vault + virtual and the fee is the row's own ``fee_bps``
(the dynamic FeeConfig tier the recorder used), through the SAME helpers the labels use (``lean.labels.mark_lamports`` /
``sell_quote_lamports`` = ``lean.marks.sell_output``; the entry is ``lean.marks.buy_output``), whatever ``costs.fee_model`` says.
A vault row (pre-M1, or recorded under ``marks.model: vault``) keeps the vault reserves and the fixture tier / flat fee above;
its recorded ``fee_bps`` is the configured flat fee, not the pool's, so it is not used. ``adapters.mark`` (vault rows) and the
labels helpers (effective rows) are called through ``Prepared.value`` / ``Prepared.proceeds`` only.
"""
import dataclasses
import itertools
import json
import sqlite3
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from lean import adapters as A, labels as LB, marks as M, paper, strategy as S

LAMPORTS = paper.LAMPORTS
MAX_BPS = paper.MAX_BPS
ZERO, ONE = Decimal(0), Decimal(1)
FAR = 10 ** 9                       # "never": a time rule that is switched off
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEES = ROOT / 'fixtures' / 'mainnet-dynamic-fees.json'
USABLE = ('OK', 'EMPTY_POOL')       # marks that carry information; everything else is missing data
FAMILIES = ('live_ladder', 'full_tp', 'hold', 'tp_time_cap', 'trailing', 'partial_tp_trail')


class ReplayError(ValueError):
    pass


# ------------------------------------------------------------------------------------------------ fees and costs
@dataclass(frozen=True)
class FeeTiers:
    """PumpSwap's market-cap tiered fee, in total bps per side (lp + protocol + creator)."""
    thresholds: tuple
    bps: tuple
    source: str = ''

    def at(self, mcap_lamports):
        i = bisect_right(self.thresholds, mcap_lamports) - 1
        return self.bps[max(0, i)]


def load_fee_tiers(path=DEFAULT_FEES):
    try:
        data = json.loads(Path(path).read_text())
        tiers = data['parsed']['fields']['fee_tiers']
        rows = sorted((int(t['market_cap_lamports_threshold']),
                       int(t['fees']['lp_fee_bps']) + int(t['fees']['protocol_fee_bps']) + int(t['fees']['creator_fee_bps']))
                      for t in tiers)
    except (OSError, ValueError, KeyError, TypeError):
        raise ReplayError('fee tier table unreadable: %s' % path) from None
    if not rows or rows[0][0] != 0 or any(not 0 <= b < MAX_BPS for _, b in rows):
        raise ReplayError('fee tier table invalid')
    return FeeTiers(tuple(t for t, _ in rows), tuple(b for _, b in rows), str(path))


@dataclass(frozen=True)
class Costs:
    """``fee_model``: ``tier`` (the mcap tier of the fixture table) or ``flat`` (``flat_bps`` per side, the live runner's
    ``pool_fee_bps``, used to calibrate against live fills)."""
    tiers: FeeTiers
    fee_model: str = 'tier'
    flat_bps: int = 25
    slippage_bps: int = 50
    fee_lamports: int = 250_000

    def side_bps(self, mcap_lamports):
        return self.tiers.at(mcap_lamports) if self.fee_model == 'tier' else self.flat_bps

    @property
    def pcfg(self):
        return paper.PaperConfig(fee_lamports=self.fee_lamports, slippage_bps=self.slippage_bps)


def costs_from(raw, tiers):
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw) - {'fee_model', 'flat_bps', 'slippage_bps', 'fee_sol'}:
        raise ReplayError('costs keys: fee_model, flat_bps, slippage_bps, fee_sol')
    model = raw.get('fee_model', 'tier')
    if model not in ('tier', 'flat'):
        raise ReplayError('fee_model must be tier or flat')
    flat, slip = raw.get('flat_bps', 25), raw.get('slippage_bps', 50)
    for name, value in (('flat_bps', flat), ('slippage_bps', slip)):
        if type(value) is not int or not 0 <= value < MAX_BPS:
            raise ReplayError('%s must be an integer in [0, 10000)' % name)
    try:
        fee = paper.to_lamports(raw.get('fee_sol', '0.00025'))
    except paper.PaperError:
        raise ReplayError('fee_sol must be a positive whole number of lamports') from None
    return Costs(tiers, model, flat, slip, fee)


# --------------------------------------------------------------------------------------------------- reading paths
@dataclass
class PathData:
    mint: str
    start_ts: float
    ends_ts: float
    entered: bool
    features: dict
    target: dict
    marks: list = field(default_factory=list)       # (ts, base_raw, quote_raw, status, model, virtual_raw, fee_bps), ascending, no dups
    gaps: list = field(default_factory=list)        # ascending timestamps of skipped polls
    end_why: str = None


def _ro(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ReplayError('database missing or not a regular file: %s' % path.name)
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.execute('PRAGMA query_only=1')
    return connection


def archive_files(paths_db):
    """The live file and its ``paths-*.sqlite`` rollover archives (same directory), oldest first."""
    live = Path(paths_db)
    found = sorted(p for p in live.parent.glob('paths-*.sqlite') if p.is_file() and not p.is_symlink() and p.name != live.name)
    return found + [live]


def load_paths(paths_db, archives=True):
    """{mint: PathData} merged over the live file and its archives. A path carried into a new file after a rollover is one
    path: marks are united by timestamp (no duplicates), gaps and the end likewise."""
    out = {}
    for file in (archive_files(paths_db) if archives else [Path(paths_db)]):
        connection = _ro(file)
        try:
            rows = connection.execute('SELECT mint, start_ts, ends_ts, entered, features, target FROM paths ORDER BY start_ts').fetchall()
            for mint, start, ends, entered, features, target in rows:
                try:
                    f, t = json.loads(features), json.loads(target)
                except ValueError:
                    continue                                  # an unreadable row is skipped, never guessed
                if mint not in out:
                    out[mint] = PathData(mint, float(start), float(ends), bool(entered), f if isinstance(f, dict) else {},
                                         t if isinstance(t, dict) else {})
            # LINT5: a recorder file with the M1 mark-input columns: each row keeps its own model / virtual reserves / fee
            m1 = {'model', 'virtual_raw', 'fee_bps'} <= {r[1] for r in connection.execute('PRAGMA table_info(path_marks)')}
            sql = ('SELECT mint, ts, base_raw, quote_raw, status, model, virtual_raw, fee_bps FROM path_marks ORDER BY ts' if m1 else
                   'SELECT mint, ts, base_raw, quote_raw, status, NULL, NULL, NULL FROM path_marks ORDER BY ts')
            for mint, ts, base, quote, status, model, virtual, fee in connection.execute(sql):
                if mint in out:
                    out[mint].marks.append((float(ts), base, quote, status, model, virtual, fee))
            for mint, ts in connection.execute('SELECT mint, ts FROM path_gaps ORDER BY ts'):
                if mint in out:
                    out[mint].gaps.append(float(ts))
            for mint, why in connection.execute('SELECT mint, why FROM path_ends'):
                if mint in out:
                    out[mint].end_why = why
        finally:
            connection.close()
    for p in out.values():
        seen, marks = set(), []
        for m in sorted(p.marks, key=lambda m: m[0]):
            if m[0] not in seen:
                seen.add(m[0])
                marks.append(m)
        p.marks, p.gaps = marks, sorted(set(p.gaps))
    return out


# ---------------------------------------------------------------------------------------------------------- policies
@dataclass(frozen=True)
class Policy:
    family: str
    params: tuple                       # sorted ((name, value-string), ...)

    @property
    def name(self):
        return '%s(%s)' % (self.family, ','.join('%s=%s' % kv for kv in self.params))

    def get(self, key, default=None):
        for k, v in self.params:
            if k == key:
                return Decimal(v)
        return default


PARAMS = {
    'live_ladder': {'stop', 'trailing', 'hold_min'},
    'full_tp': {'x', 'stop', 'hold_min'},
    'hold': {'minutes', 'stop'},
    'tp_time_cap': {'x', 'minutes', 'stop'},
    'trailing': {'fraction', 'stop'},
    'partial_tp_trail': {'x', 'sell_fraction', 'fraction', 'stop', 'after_stop'},
}
REQUIRED = {'full_tp': {'x'}, 'hold': {'minutes'}, 'tp_time_cap': {'x', 'minutes'}, 'trailing': {'fraction'},
            'partial_tp_trail': {'x', 'sell_fraction', 'fraction'}}


def _dec(value, name):
    if isinstance(value, bool) or value is None:
        raise ReplayError('%s must be a number' % name)
    try:
        d = Decimal(str(value))
    except ArithmeticError:
        raise ReplayError('%s must be a number' % name) from None
    if not d.is_finite():
        raise ReplayError('%s must be finite' % name)
    return d


def expand_grid(families):
    """grid.json ``families``: [{"family": "full_tp", "x": [1.3, 1.5], "stop": 0.18}, ...] -> [Policy]. A list value is a
    dimension of the grid, a scalar a constant. Unknown families / parameters / out-of-range values are errors."""
    if not isinstance(families, list) or not families:
        raise ReplayError('families must be a non-empty list')
    out, seen = [], set()
    for spec in families:
        if not isinstance(spec, dict) or spec.get('family') not in FAMILIES:
            raise ReplayError('family must be one of %s' % (FAMILIES,))
        family = spec['family']
        keys = set(spec) - {'family'}
        if keys - PARAMS[family] or REQUIRED.get(family, set()) - keys:
            raise ReplayError('%s parameters: allowed %s, required %s' % (family, sorted(PARAMS[family]), sorted(REQUIRED.get(family, ()))))
        names = sorted(keys)
        axes = [spec[k] if isinstance(spec[k], list) else [spec[k]] for k in names]
        for combo in itertools.product(*axes):
            params = tuple((k, str(_dec(v, k).normalize())) for k, v in zip(names, combo))
            policy = Policy(family, params)
            _check(policy)
            if policy not in seen:
                seen.add(policy)
                out.append(policy)
    return out


def _check(policy):
    g = policy.get
    for key, low, high in (('stop', 0, 1), ('trailing', 0, 1), ('fraction', 0, 1), ('sell_fraction', 0, 1), ('after_stop', 0, 100)):
        v = g(key)
        if v is not None and not (low < v < high if key != 'after_stop' else low <= v < high):
            raise ReplayError('%s: %s out of range' % (policy.name, key))
    for key in ('x',):
        v = g(key)
        if v is not None and v <= ONE:
            raise ReplayError('%s: x must be above 1' % policy.name)
    for key in ('minutes', 'hold_min'):
        v = g(key)
        if v is not None and not 0 < v <= 100000:
            raise ReplayError('%s: %s out of range' % (policy.name, key))


def build_exit(policy, base):
    """(StrategyConfig copy, initial stage, initial stop ratio) that make ``exit_decision`` implement ``policy``."""
    g = policy.get
    stop = g('stop')
    stop_ratio = (ONE - stop) if stop is not None else ZERO         # no stop = only a drained pool (ratio 0) stops it
    off = dict(time_stop_seconds=FAR, max_hold_seconds=FAR)         # time rules are off unless the family asks for them
    family = policy.family
    if family == 'live_ladder':
        cfg = dataclasses.replace(base, trailing_fraction=g('trailing', base.trailing_fraction))
        if g('hold_min') is not None:
            cfg = dataclasses.replace(cfg, max_hold_seconds=int(g('hold_min') * 60))
        return cfg, 0, ONE - g('stop') if g('stop') is not None else base.initial_stop_ratio
    if family == 'full_tp':
        cfg = dataclasses.replace(base, tp_ladder=(S.Rung(g('x'), ONE, ZERO),), **off)
        if g('hold_min') is not None:
            cfg = dataclasses.replace(cfg, max_hold_seconds=int(g('hold_min') * 60))
        return cfg, 0, stop_ratio
    if family == 'hold':
        return dataclasses.replace(base, tp_ladder=(), **dict(off, max_hold_seconds=int(g('minutes') * 60))), 0, stop_ratio
    if family == 'tp_time_cap':
        cfg = dataclasses.replace(base, tp_ladder=(S.Rung(g('x'), ONE, ZERO),), **dict(off, max_hold_seconds=int(g('minutes') * 60)))
        return cfg, 0, stop_ratio
    if family == 'trailing':
        return dataclasses.replace(base, tp_ladder=(), trailing_fraction=g('fraction'), **off), S.LADDER_STAGES, stop_ratio
    if family == 'partial_tp_trail':
        cfg = dataclasses.replace(base, tp_ladder=(S.Rung(g('x'), g('sell_fraction'), g('after_stop', ONE)),),
                                  trailing_fraction=g('fraction'), **off)
        return cfg, 0, stop_ratio
    raise ReplayError('unknown family')


# ------------------------------------------------------------------------------------------------------------ replay
@dataclass
class Prepared:
    path: PathData
    ts: list
    base: list
    quote: list
    bps: list                       # the side fee (bps) at each mark (an effective row: its own recorded fee_bps)
    gap_ts: list
    entry_idx: int
    qty0: int
    cost0: int                      # lamports: spend + the entry fill fee
    supply: int
    points: list = field(default_factory=list)      # LINT5: a labels.Point for an effective row, None for a vault row

    def value(self, i, qty, pcfg):
        """The live mark (Decimal SOL, net of the fixed fee) of ``qty`` at mark ``i``."""
        point = self.points[i] if self.points else None
        if point is not None:
            return A.sol(LB.mark_lamports(point, qty, None, pcfg))
        return _net_mark(qty, self.base[i], self.quote[i], self.bps[i], pcfg)

    def proceeds(self, i, qty, costs):
        """Lamports a sell of ``qty`` at mark ``i`` returns: pool output after the fee, slippage, minus the fill fee."""
        point = self.points[i] if self.points else None
        if point is not None:
            return LB.sell_quote_lamports(point, qty, None) * (MAX_BPS - costs.slippage_bps) // MAX_BPS - costs.fee_lamports
        return _proceeds(qty, self.base[i], self.quote[i], self.bps[i], costs)


@dataclass
class Trade:
    mint: str
    start_ts: float
    entered_live: bool
    ret: float                      # (proceeds - cost) / cost, net of every cost
    pnl_lamports: int
    cost_lamports: int
    reasons: list
    fills: list                     # (ts, qty, proceeds_lamports, reason)
    past_trigger: list              # per exit: fractional change of the per-token net value from decision mark to fill mark
    path_end: bool = False
    crossed_gap: int = 0
    marks_used: int = 0


def _net_mark(qty, base, quote, bps, pcfg):
    """THE call into ``adapters.mark`` (the live position loop's mark). A drained / unusable pool is worth zero."""
    try:
        return A.mark(qty, base, quote, pool_fee_bps=bps, pcfg=pcfg)
    except A.AdapterError:
        return ZERO


def prepare(path, costs, size_lamports):
    """Usable marks, tier fees and the entry at the first OK mark. None when the path cannot be entered."""
    supply = path.features.get('supply_raw')
    try:
        supply = int(supply)
    except (TypeError, ValueError):
        return None
    if supply <= 0:
        return None
    ts, base, quote, bps, points = [], [], [], [], []
    for row in path.marks:
        t, b, q, status = row[:4]
        model, virtual, fee = row[4:7] if len(row) >= 7 else (None, None, None)
        if status not in USABLE or b is None or q is None:
            continue
        point = effective_point(t, b, q, status, model, virtual, fee)
        ts.append(t)
        base.append(int(b))
        quote.append(int(q))
        points.append(point)
        if point is not None:
            bps.append(point.fee_bps)                                  # the row's own (dynamic tier) fee
        else:
            bps.append(costs.side_bps(int(q) * supply // int(b)) if int(b) > 0 else costs.side_bps(0))
    entry = next((i for i in range(len(ts)) if base[i] > 0 and quote[i] > 0), None)
    if entry is None or len(ts) - entry < 2:                          # nothing after the entry to decide on
        return None
    if points[entry] is not None:                                      # effective row: the program's buy (fee off the input)
        try:
            out = M.buy_output(size_lamports, base[entry], M.effective_quote(quote[entry], points[entry].virtual_raw), bps[entry])
        except M.MarkError:
            return None
    else:
        spent = size_lamports * (MAX_BPS - bps[entry]) // MAX_BPS
        out = base[entry] * spent // (quote[entry] + spent)
    qty0 = out * (MAX_BPS - costs.slippage_bps) // MAX_BPS
    if qty0 <= 0:
        return None
    return Prepared(path, ts, base, quote, bps, path.gaps, entry, qty0, size_lamports + costs.fee_lamports, supply,
                    points=points if any(p is not None for p in points) else [])


def effective_point(ts, base, quote, status, model, virtual, fee):
    """A ``labels.Point`` for a row the recorder marked with the effective model (same acceptance as ``labels.iter_paths``),
    else None (a vault row)."""
    if status != 'OK' or model != 'effective' or type(base) is not int or type(quote) is not int or base <= 0 or quote <= 0:
        return None
    if type(virtual) is not int or type(fee) is not int or not 0 <= fee < MAX_BPS:
        return None
    point = LB.Point(float(ts), base, quote, virtual_raw=virtual, fee_bps=fee, model='effective')
    return point if point.effective else None


def _proceeds(qty, base, quote, bps, costs):
    gross = quote * qty // (base + qty) if base > 0 and quote > 0 else 0
    return gross * (MAX_BPS - bps) // MAX_BPS * (MAX_BPS - costs.slippage_bps) // MAX_BPS - costs.fee_lamports


def replay_path(prep, policy, base_cfg, costs, lag=1):
    """One path under one policy -> Trade. Decisions by ``strategy.exit_decision`` at marks, fills at the next mark (lag 1)."""
    cfg, stage, stop_ratio = build_exit(policy, base_cfg)
    pcfg = costs.pcfg
    ts = prep.ts
    cost_left = Decimal(prep.cost0) / LAMPORTS
    pos = S.Position(mint=prep.path.mint, opened_at=int(ts[prep.entry_idx]), qty=Decimal(prep.qty0), initial_qty=Decimal(prep.qty0),
                     cost_left=cost_left, stop_ratio=stop_ratio, stage=stage)
    qty, held = prep.qty0, prep.qty0
    proceeds_total, reasons, fills, past, crossed = 0, [], [], [], 0
    last = len(ts) - 1
    arm_after_tp = policy.family == 'partial_tp_trail'

    def sell(decision_idx, fill_idx, amount, reason):
        nonlocal qty, proceeds_total, crossed
        p = prep.proceeds(fill_idx, amount, costs)
        proceeds_total += p
        qty -= amount
        fills.append((ts[fill_idx], amount, p, reason))
        reasons.append(reason)
        if fill_idx != decision_idx:
            lo, hi = bisect_right(prep.gap_ts, ts[decision_idx]), bisect_left(prep.gap_ts, ts[fill_idx])
            crossed += 1 if hi > lo else 0
            at_fill = prep.value(fill_idx, amount, pcfg)
            at_trigger = prep.value(decision_idx, amount, pcfg)
            if at_trigger > 0:
                past.append(float(at_fill / at_trigger - ONE))
        return p

    path_end = False
    marks_used = 0
    for i in range(prep.entry_idx + 1, len(ts)):
        marks_used += 1
        mark = prep.value(i, qty, pcfg)
        now = int(ts[i])
        d = S.exit_decision(pos, mark, None, now, cfg, mark_at=now)
        updates = dict(d.position_updates)
        if updates:
            pos = dataclasses.replace(pos, peak_ratio=updates.get('peak_ratio', pos.peak_ratio),
                                      touched_15=updates.get('touched_15', pos.touched_15))
        if d.action != 'SELL' or not d.quote_required:
            continue
        amount = min(qty, int(d.qty.to_integral_value(rounding='ROUND_DOWN')))
        if amount <= 0:
            continue
        j = i + 1 if lag >= 1 and i < last else i
        full = amount == qty
        fraction_before = Decimal(qty)
        sell(i, j, amount, d.reason)
        if full:
            break
        cost_left = cost_left * Decimal(qty) / fraction_before
        after = dict(d.after_fill)
        pos = dataclasses.replace(pos, qty=Decimal(qty), cost_left=cost_left, stage=after.get('stage', pos.stage),
                                  stop_ratio=after.get('stop_ratio', pos.stop_ratio))
        if arm_after_tp and d.reason == 'TAKE_PROFIT':
            pos = dataclasses.replace(pos, stage=S.LADDER_STAGES)           # the one field this module sets: arm the trailing stop
    if qty > 0:                                                    # the path ended with the position open
        path_end = True
        sell(last, last, qty, 'PATH_END')
    pnl = proceeds_total - prep.cost0
    return Trade(mint=prep.path.mint, start_ts=prep.path.start_ts, entered_live=prep.path.entered, ret=pnl / prep.cost0,
                 pnl_lamports=pnl, cost_lamports=prep.cost0, reasons=reasons, fills=fills, past_trigger=past, path_end=path_end,
                 crossed_gap=crossed, marks_used=marks_used)
