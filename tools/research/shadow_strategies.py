"""Shadow strategy variants: parallel virtual A/B on the T26 counterfactual price paths.

    python -m tools.research.shadow_strategies --store counterfactual.sqlite \
        --grid config/experiments/shadow/exit-grid.json [--holdout-from UTC|epoch] [--features f.json] \
        [--min-trades 30] [--parity-ledger ledger.sqlite] [--json]

SIMULATED_SHADOW, never trading evidence, no provider request, no ledger writes. Every variant is
evaluated by calling the REAL ``desk.engine.transition`` on events synthesized from the sampled PumpSwap
reserves (provenance SYNTHETIC_TEST_ONLY, in-memory state only). Costs come from the engine: pool fee,
adverse slippage and the fixed fee. Each candidate is simulated in an isolated state, first entry only,
so trades are comparable across variants. Candidate age windows are measured since the migration HINT
RECEIPT (the counterfactual store records the discovery receive time, not the on-chain block time).

What a sparse path can and cannot tell you
------------------------------------------
The store holds a handful of samples per candidate (+5m ... +6h). ``simulate`` is the NOMINAL run: the
engine sees only the sampled instants, strictly causally. It is one possible history, not a bracket.

``bracket_trade`` is the BRACKET. It assumes nothing about the path between two samples except the
explicit, printed assumption below, and then enumerates, with the real engine, every way the live engine
could have observed that gap:

  stated assumption  Between two consecutive samples the live engine may observe up to ``max_intra_marks``
                     (default 3) extra marks, in any order and at any time, each at a pool price (quote reserve
                     per base unit) inside [lo, hi] with lo = (1 - excursion) * min(price_a, price_b) and
                     hi = (1 + excursion) * max(price_a, price_b) (``excursion_fraction``, default 0.30).
                     A gap that contains a POOL_DEAD sample, or that ends at the pool's death, has lo = 0.
                     Intra-gap marks use the pool depth (base reserve) of the earlier sample. Fills happen at
                     the observed price. Nothing else is assumed (no monotone or linear path).

Between two samples the engine's behaviour only changes at its own thresholds, so the enumeration marks
exactly the prices where those thresholds trigger (and the extremes lo / hi): the current stop level, the +15% touch that cancels the
time-stop (and the highest price at which a time-stop still fills), the next ladder rung, the trailing level (which depends on the peak) and the time-stop / max-hold
deadlines. This covers a stop that is crossed and recovered between samples, several rungs hit on one gap
(the engine sells one rung per observed mark, so a coarse gap can hide several), and an unknown peak for the
trailing level. A dynamic program over the resulting engine states returns the true minimum and maximum PnL
under that assumption (``pnl_lo`` / ``pnl_hi``); a trade whose bounds differ is AMBIGUOUS and the share of
ambiguous trades is reported per variant. With ``max_intra_marks = 0`` the bracket collapses to the nominal run.
The bracket is conditioned on BOTH ends of every gap on purpose: it describes paths consistent with the data;
every engine decision inside a branch sees only that branch's marks. The live monitor is assumed to keep marks
fresh within ``price_ttl_seconds``: there is no carry of a stale price past its time-to-live (the previous
"carry" model fed a last sample as if fresh and has been removed).

Signal features (flow, holders, wash, ...) are NOT in the price paths: unless a features file supplies them,
every candidate gets the same neutral features (printed in the output), so variants differ in structure
(windows, stop, trailing, time-stop, max-hold, sizing), not in signal quality. A features file must carry an
``as_of`` per candidate: those features are known only from that instant, entries before it are refused
(counted as FEATURES_NOT_YET_KNOWN) and a feature that would be observed after the decision is never used. The
take-profit ladder is fixed inside ``desk.engine.manage_position`` and is not a config key.
"""
import argparse
import base64
import copy
import hashlib
import itertools
import json
import math
import random
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

from desk import engine
from desk.model import D, canonical
from desk.strategy import swap_quote
from tools.research import counterfactual as cf

LABEL = 'SIMULATED_SHADOW'
HORIZONS = cf.HORIZONS
BASELINE_HORIZON = cf.BASELINE_HORIZON
MODELS = ('samples',)
DEFAULT_ASSUMPTIONS = {'sol_usd': '150', 'token_decimals': 6, 'token_supply_tokens': '1000000000',
                       'pool_fee_bps': '25', 'excursion_fraction': '0.30', 'max_intra_marks': 3}
BOOTSTRAP = 4000
MAX_BOOTSTRAP = 100000
MIN_TRADES = 30
MAX_ENGINE_EVENTS = 20000          # per candidate; beyond this the bracket is reported as truncated, never guessed
TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
NEUTRAL_FEATURES = {
    'graduated': True, 'mint_revoked': True, 'freeze_revoked': True, 'lp_verified': True, 'extensions_safe': True,
    'data_healthy': True, 'flow_confirmed': False, 'danger': False, 'route_available': True,
    'top10_pct': '10', 'dev_pct': '0', 'bundle_pct': '0', 'cluster_pct': '0', 'fresh_wallet_ratio': '0',
    'flow': '90', 'manip_safety': '0', 'manip_flow': '0', 'net_buy_ratio': '.8', 'unique_buyers_5m': 50,
    'volume_vs_liq': '2', 'drawdown_from_high': '0', 'wash_score': '0', 'dev_launches_7d': 1}
# The engine's fixed ladder and its +15% touch (desk.engine.manage_position); tests pin them to the engine.
LADDER_TRIGGERS = (D('1.4'), D('2'), D('3'))
TOUCH_RATIO = D('1.15')
MARGIN = D('1e-9')
SELECTION_STAGES = ('candidates', 'no_sample_rows', 'truncated_paths', 'dead_before_baseline', 'no_priced_sample',
                    'baseline_not_priced', 'evaluated')


class ShadowError(ValueError):
    pass


class _Truncated(Exception):
    pass


# ---------------------------------------------------------------- inputs
def load_candidates(store, selection=None, *, include_truncated=False):
    """Counterfactual paths (read-only, T02F open rule). Fills ``selection`` with exclusion counts if given.

    ``include_truncated`` also evaluates paths that are not yet complete (counted as ``truncated_included``;
    their position is liquidated at the last sample: horizon_end). A candidate carries its OK-priced
    samples plus what the other rows say: a pool is TERMINALLY dead only if
    a POOL_DEAD sample has no priced sample after it (a transient closed vault that later reappears is a gap,
    not a death, and the gap gets lo = 0). Nothing is dropped silently: ``selection`` counts every exclusion.
    """
    out = []
    counts = {k: 0 for k in SELECTION_STAGES}
    counts.update(failed_or_missed_gaps=0, transient_death=0, terminal_death=0, truncated_included=0)
    with closing(cf._connect(store, readonly=True)) as c:
        c.execute('BEGIN')
        for mint, pool, migrated in c.execute('SELECT mint,pool,migrated_at FROM candidates ORDER BY migrated_at,mint').fetchall():
            counts['candidates'] += 1
            rows = c.execute('SELECT horizon,status,base_raw,quote_raw,price FROM samples WHERE mint=? ORDER BY horizon', (mint,)).fetchall()
            status = {h: s for h, s, *_ in rows}
            ok = [(h, int(b), int(q)) for h, s, b, q, p in rows if s == 'OK' and p is not None and b is not None and q is not None]
            dead = [h for h, s, *_ in rows if s == 'POOL_DEAD']
            last_ok = max((h for h, _, _ in ok), default=-1)
            terminal = next((h for h in dead if h > last_ok), None)
            transient = [h for h in dead if h < last_ok]
            cand = {'mint': mint, 'pool': pool, 'migrated_at': float(migrated), 'pool_died': terminal is not None,
                    'died_at': terminal, 'transient_dead': transient,
                    'samples': [{'horizon': h, 'base_raw': b, 'quote_raw': q} for h, b, q in ok],
                    'complete': all(h in status for h in HORIZONS),
                    'baseline': 'OK' if any(h == BASELINE_HORIZON for h, _, _ in ok) else (
                        'DEAD' if status.get(BASELINE_HORIZON) == 'POOL_DEAD' else 'MISSING')}
            if not rows:
                counts['no_sample_rows'] += 1
            elif not cand['complete'] and not include_truncated:
                counts['truncated_paths'] += 1
            elif cand['baseline'] == 'DEAD':
                counts['dead_before_baseline'] += 1
            elif not ok:
                counts['no_priced_sample'] += 1
            elif cand['baseline'] == 'MISSING':
                counts['baseline_not_priced'] += 1
            else:
                counts['evaluated'] += 1
                counts['truncated_included'] += not cand['complete']
                counts['failed_or_missed_gaps'] += any(s in ('FAILED', 'MISSED') for s in status.values())
                counts['transient_death'] += bool(transient)
                counts['terminal_death'] += terminal is not None
                out.append(cand)
    if selection is not None:
        remaining, funnel = counts['candidates'], []
        for stage in SELECTION_STAGES[1:-1]:
            remaining -= counts[stage]
            funnel.append({'stage': stage, 'excluded': counts[stage], 'remaining': remaining})
        counts['funnel'] = funnel         # totals at each exclusion stage, in the order they are applied
        selection.update(counts)
    return out


def load_outcomes(store):
    """mint -> report group of the T26F classification (read-only): BOUGHT | REJECTED:<stage>:<code> | NOT_DISPATCHED | UNKNOWN."""
    groups = {}
    with closing(cf._connect(store, readonly=True)) as c:
        c.execute('BEGIN')
        for mint, in c.execute('SELECT mint FROM candidates').fetchall():
            last = c.execute('SELECT payload FROM outcomes WHERE mint=? ORDER BY id DESC LIMIT 1', (mint,)).fetchone()
            try:
                payload = json.loads(last[0]) if last else None
            except ValueError:
                payload = {}
            names, _detail = cf.groups_of(payload)
            groups[mint] = names[0]       # primary code; a candidate is never counted twice in the split
    return groups


def load_grid(path):
    spec = json.loads(Path(path).read_text())
    if type(spec) is not dict or spec.get('version') != 'shadow-grid-1' or not {'base_config'} <= set(spec):
        raise ShadowError('Unsupported shadow grid file')
    root = Path(__file__).resolve().parents[2]
    base_path = Path(spec['base_config'])
    base = json.loads((base_path if base_path.is_absolute() else root / base_path).read_text())
    assumptions = {**DEFAULT_ASSUMPTIONS, **spec.get('assumptions', {})}
    if set(assumptions) != set(DEFAULT_ASSUMPTIONS):
        raise ShadowError('Unknown assumption key')
    _validate_assumptions(assumptions)
    variants = [{'name': v['name'], 'overrides': v.get('overrides', {})} for v in spec.get('variants', [])]
    grid = spec.get('grid')
    if grid:
        keys = sorted(grid)
        for combo in itertools.product(*(grid[k] for k in keys)):
            variants.append({'name': ','.join(f'{k}={v}' for k, v in zip(keys, combo)), 'overrides': dict(zip(keys, combo))})
    if not variants or len({v['name'] for v in variants}) != len(variants):
        raise ShadowError('Variant names must be unique and non-empty')
    for v in variants:
        build_config(base, v['overrides'])  # fail closed on unknown keys / type changes
    return {'base': base, 'assumptions': assumptions, 'variants': variants, 'name': spec.get('name', Path(path).stem)}


def _validate_assumptions(a):
    try:
        excursion = Decimal(str(a['excursion_fraction']))
    except Exception:
        raise ShadowError('excursion_fraction must be a decimal') from None
    if not Decimal(0) <= excursion <= Decimal(2) or type(a['max_intra_marks']) is not int or not 0 <= a['max_intra_marks'] <= 4:
        raise ShadowError('excursion_fraction must be in [0, 2] and max_intra_marks an integer in [0, 4]')


def build_config(base, overrides):
    cfg = copy.deepcopy(base)
    for key, value in overrides.items():
        if key not in cfg or type(value) is not type(cfg[key]):
            raise ShadowError(f'Override {key!r} must exist in the base config with the same type')
        cfg[key] = value
    return cfg


def load_features(path, candidates=()):
    """{mint: {'as_of': epoch, <event feature overrides>}}. Every entry must say when it became known."""
    raw = json.loads(Path(path).read_text())
    if type(raw) is not dict:
        raise ShadowError('Features file must be an object keyed by mint')
    out = {}
    for mint, entry in raw.items():
        if type(entry) is not dict or 'as_of' not in entry or type(entry['as_of']) not in (int, float) or isinstance(entry['as_of'], bool):
            raise ShadowError(f'Features for {mint!r} must carry a numeric as_of (when they became known)')
        unknown = set(entry) - {'as_of'} - set(NEUTRAL_FEATURES)
        if unknown:
            raise ShadowError(f'Unknown feature fields for {mint!r}: {sorted(unknown)}')
        out[mint] = {'as_of': float(entry['as_of']), 'fields': {k: v for k, v in entry.items() if k != 'as_of'}}
    ends = {c['mint']: c['migrated_at'] + max((s['horizon'] for s in c['samples']), default=0) for c in candidates}
    for mint, entry in out.items():
        if mint in ends and entry['as_of'] > ends[mint]:
            raise ShadowError(f'Features for {mint!r} are observed after the end of its price path (look-ahead)')
    return out


# ------------------------------------------------------------ synthesis
def _clean_evidence(ts):
    return {'as_of': ts, 'launch_history_complete': True, 'funding_history_complete': True,
            'holder_coverage_pct': '100', 'launch_slot': 1000,
            'holders': [{'wallet': f'w{i:03}', 'pct': '1'} for i in range(100)],
            'early_buys': [{'wallet': f'w{i:03}', 'slot': 1010 + i, 'ts': ts - 300 + i} for i in range(100)],
            'funding': [], 'transfers': [], 'known_bad_wallets': []}


def synth_event(candidate, ts, base_raw, quote_raw, serial, assumptions, features=None):
    """A strict-mode market event whose reserves are the sampled vault balances (SYNTHETIC_TEST_ONLY)."""
    from decimal import localcontext
    with localcontext() as ctx:
        ctx.prec = 40
        reserve_sol = D(quote_raw) / D(10) ** 9
        reserve_tokens = D(base_raw) / D(10) ** assumptions['token_decimals']
        price_usd = reserve_sol / reserve_tokens * D(assumptions['sol_usd'])
        mcap = price_usd * D(assumptions['token_supply_tokens'])
    data = bytearray(82)
    data[36:44] = (10 ** 15).to_bytes(8, 'little'); data[44], data[45] = 6, 1
    mint = candidate['mint']
    e = {'schema_version': 1, 'event_id': f'shadow:{mint}:{ts}:{serial}', 'kind': 'market', 'ts': ts, 'mint': mint,
         'pool': candidate['pool'], 'venue': 'pumpswap', 'provenance': 'SYNTHETIC_TEST_ONLY',
         'graduated_at': int(candidate['migrated_at']), 'price_at': ts, 'holder_at': ts, 'flow_at': ts, 'momentum_at': ts,
         'reserve_sol': format(reserve_sol, 'f'), 'reserve_tokens': format(reserve_tokens, 'f'),
         'sol_usd': str(assumptions['sol_usd']), 'market_cap_usd': format(mcap, 'f'), 'pool_fee_bps': str(assumptions['pool_fee_bps']),
         **copy.deepcopy(NEUTRAL_FEATURES), **copy.deepcopy(features or {}),
         'bundle_evidence': _clean_evidence(ts), 'sellability': {'kind': 'synthetic_model'},
         'token_evidence': {'mint': mint, 'observed_at': ts, 'account': {
             'owner': TOKEN_PROGRAM, 'executable': False, 'data': [base64.b64encode(data).decode(), 'base64']}}}
    return e


def _path_points(candidate):
    return [(int(candidate['migrated_at'] + s['horizon']), s['base_raw'], s['quote_raw']) for s in candidate['samples']]


def _features_for(features, mint, ts):
    """(fields, known): the candidate's features at decision time ``ts``. Unknown candidates get neutral features."""
    entry = (features or {}).get(mint)
    if entry is None:
        return None, True
    return (entry['fields'], True) if ts >= entry['as_of'] else (None, False)


# ------------------------------------------------------------ nominal run
def simulate(candidate, cfg, assumptions, model='samples', features=None):
    """NOMINAL run: first entry only, strictly causal, the engine sees only the sampled instants.

    Returns a trade dict, or {'entered': False, 'reject': reason}. This is one possible history; see
    ``bracket_trade`` for the bracket of all histories consistent with the samples.
    """
    if model not in MODELS:
        raise ShadowError('Unknown path model')
    points = _path_points(candidate)
    state = engine.initial_state(cfg)
    mint = candidate['mint']
    serial = 0
    entry = None
    first_reject = None

    def feed(ts, base_raw, quote_raw, fields=None):
        nonlocal serial
        serial += 1
        before = copy.deepcopy(state['positions'].get(mint))
        e = synth_event(candidate, ts, base_raw, quote_raw, serial, assumptions, fields)
        _, out = engine.transition(state, e, cfg)
        return out, before

    for point in points:
        fields, known = _features_for(features, mint, point[0])
        if entry is None and not known:
            first_reject = first_reject or 'FEATURES_NOT_YET_KNOWN'
            continue
        out, before = feed(*point, fields if entry is None else None)
        if entry is None:
            buy = next((x for x in out if x.get('type') == 'fill' and x.get('side') == 'buy'), None)
            if buy is None:
                first_reject = first_reject or next((x.get('reason') for x in out if x.get('type') == 'reject'), None)
                continue
            position = state['positions'][mint]
            entry = {'at': point[0], 'initial_cost': position['initial_cost']}
            continue
        if mint not in state['positions']:
            return _trade(candidate, cfg, state, entry, point[0], out, before)
    if entry is None:
        return {'entered': False, 'reject': first_reject}
    if candidate.get('pool_died'):
        # The pool died while the position was still open: nothing can be sold. Remaining cost is lost.
        position = state['positions'][mint]
        pnl = D(state['realized_pnl']) - D(position['cost_left'])
        cost = D(entry['initial_cost'])
        return {'entered': True, 'mint': mint, 'entry_at': entry['at'], 'exit_at': points[-1][0], 'exit_reason': 'POOL_DEAD',
                'pnl_sol': pnl, 'pnl_best_sol': pnl, 'cost_sol': cost, 'return': pnl / cost, 'horizon_end': True}
    # Path ended while holding: real engine LIQUIDATE at the final sampled reserves (flagged horizon_end).
    last = points[-1]
    ts = last[0] + 1
    control = {'schema_version': 1, 'event_id': f'shadow:{mint}:control', 'ts': ts, 'kind': 'control', 'command': 'LIQUIDATE', 'actor': 'operator'}
    engine.transition(state, control, cfg)
    out, before = feed(ts + 1, last[1], last[2])
    trade = _trade(candidate, cfg, state, entry, ts + 1, out, before)
    trade['horizon_end'] = True
    return trade


def _exit_fill(out):
    final = [x for x in out if x.get('type') == 'fill' and x.get('side') == 'sell' and x['reason'] != 'TAKE_PROFIT']
    return final[0] if final else None


def _trade(candidate, cfg, state, entry, exit_at, out, before):
    if candidate['mint'] in state['positions']:  # still open (partial ladder sells only)
        raise ShadowError('Position unexpectedly open at trade close')
    fill = _exit_fill(out)
    if fill is None or before is None:
        raise ShadowError('Closed position without a final engine sell')
    pnl = D(state['realized_pnl'])
    cost = D(entry['initial_cost'])
    return {'entered': True, 'mint': candidate['mint'], 'entry_at': entry['at'], 'exit_at': exit_at,
            'exit_reason': fill['reason'], 'pnl_sol': pnl, 'pnl_best_sol': pnl, 'cost_sol': cost,
            'return': pnl / cost, 'horizon_end': False}


# ------------------------------------------------------------ the bracket
def _fee_bps(assumptions):
    return str(assumptions['pool_fee_bps'])


def _ratio(pos, base_raw, quote_raw, cfg, assumptions):
    """The engine's mark ratio of ``pos`` at the given reserves (same swap_quote the engine uses)."""
    e = {'route_available': True, 'reserve_sol': format(D(quote_raw) / D(10) ** 9, 'f'),
         'reserve_tokens': format(D(base_raw) / D(10) ** assumptions['token_decimals'], 'f'), 'pool_fee_bps': _fee_bps(assumptions)}
    gross = swap_quote(e, D(pos['qty']), 'sell', cfg)
    return max(D(0), gross - D(cfg['fixed_fee_sol'])) / D(pos['cost_left'])


def _quote_raw_for(pos, base_raw, ratio, cfg, assumptions, rounding):
    """Quote-vault lamports at which the engine's mark ratio of ``pos`` equals ``ratio`` (linear in the SOL reserve)."""
    e = {'route_available': True, 'reserve_sol': '1', 'reserve_tokens': format(D(base_raw) / D(10) ** assumptions['token_decimals'], 'f'),
         'pool_fee_bps': _fee_bps(assumptions)}
    per_sol = swap_quote(e, D(pos['qty']), 'sell', cfg)
    sol = (ratio * D(pos['cost_left']) + D(cfg['fixed_fee_sol'])) / per_sol
    lamports = (sol * D(10) ** 9).to_integral_value(rounding=rounding)
    return max(1, int(lamports))


class _Node:
    __slots__ = ('ts', 'base', 'quote', 'dead', 'price')

    def __init__(self, ts, base=None, quote=None, dead=False):
        self.ts, self.base, self.quote, self.dead = ts, base, quote, dead
        self.price = D(0) if dead else D(quote) / D(base)      # quote reserve per base unit: the pool price


def _key(state, mint):
    p = state['positions'].get(mint)
    if p is None:
        return ('closed', state['mode'])
    return (state['mode'], p['qty'], p['cost_left'], p['stage'], p['stop_ratio'], str(D(p['peak_ratio']).quantize(D('1e-12'))),
            p['touched_15'], p['opened_at'], p.get('exit_blocked'))


class _Explorer:
    """Dynamic program over the engine states a sparse path can hide (see the module docstring)."""

    def __init__(self, candidate, cfg, assumptions):
        self.candidate, self.cfg, self.a = candidate, cfg, assumptions
        self.mint = candidate['mint']
        self.excursion = D(str(assumptions['excursion_fraction']))
        self.marks = int(assumptions['max_intra_marks'])
        self.nodes = [_Node(*p) for p in _path_points(candidate)]
        if candidate.get('pool_died'):
            self.nodes.append(_Node(int(candidate['migrated_at'] + candidate['died_at']), dead=True))
        self.transient = [int(candidate['migrated_at'] + h) for h in candidate.get('transient_dead', [])]
        self.memo = {}
        self.events = 0
        self.serial = 0

    # ---- engine plumbing
    def feed(self, state, ts, base_raw, quote_raw):
        self.events += 1
        if self.events > MAX_ENGINE_EVENTS:
            raise _Truncated()
        self.serial += 1
        e = synth_event(self.candidate, ts, base_raw, quote_raw, f'x{self.serial}', self.a)
        return engine.transition(state, e, self.cfg)[1]

    def liquidate(self, state, node):
        st = copy.deepcopy(state)
        ts = node.ts + 1
        control = {'schema_version': 1, 'event_id': f'shadow:{self.mint}:control', 'ts': ts, 'kind': 'control',
                   'command': 'LIQUIDATE', 'actor': 'operator'}
        engine.transition(st, control, self.cfg)
        self.feed(st, ts + 1, node.base, node.quote)
        if self.mint in st['positions']:
            raise ShadowError('Position survived the horizon-end liquidation')
        return D(st['realized_pnl']) - D(state['realized_pnl'])

    # ---- one gap
    def levels(self, state, lo_p, hi_p, base):
        """(label, quote_raw) marks, at pool depth ``base``, that can change the engine's behaviour inside [lo_p, hi_p]."""
        p = state['positions'][self.mint]
        pos_base = D(base)
        found = [('LO', max(1, int((lo_p * pos_base).to_integral_value(rounding=ROUND_FLOOR)))),
                 ('HI', max(1, int((hi_p * pos_base).to_integral_value(rounding=ROUND_CEILING))))]
        triggers = [('STOP', D(p['stop_ratio']) * (1 - MARGIN), ROUND_FLOOR)]
        if p['stage'] >= 3:
            triggers.append(('TRAIL', D(p['peak_ratio']) * (1 - D(self.cfg['trailing_fraction'])) * (1 - MARGIN), ROUND_FLOOR))
        if not p['touched_15']:
            triggers.append(('TOUCH', TOUCH_RATIO * (1 + MARGIN), ROUND_CEILING))
            # TIME_STOP fires only while the +15% touch has not happened: the HIGHEST price at which a timer exit still
            # fills is just below that touch (the best fill of a time-stop; the lowest is the LO extreme).
            triggers.append(('TIMECAP', TOUCH_RATIO * (1 - MARGIN), ROUND_FLOOR))
        if p['stage'] < len(LADDER_TRIGGERS):
            triggers.append(('RUNG', LADDER_TRIGGERS[p['stage']] * (1 + MARGIN), ROUND_CEILING))
        for label, ratio, rounding in triggers:
            quote = _quote_raw_for(p, base, ratio, self.cfg, self.a, rounding)
            if lo_p <= D(quote) / pos_base <= hi_p:
                found.append((label, quote))
        seen, out = set(), []
        for label, quote in found:
            if quote not in seen:
                seen.add(quote)
                out.append((label, quote))
        return out

    def ts_choices(self, state, t_prev, t_end, left):
        """Earliest slot, plus a slot AT each unreached deadline (so events fall before and at/after it)."""
        p = state['positions'][self.mint]
        deadlines = {p['opened_at'] + self.cfg['time_stop_seconds'], p['opened_at'] + self.cfg['max_hold_seconds']}
        slots = {t_prev + 1} | {d for d in deadlines if t_prev < d < t_end}
        return sorted(t for t in slots if t + left - 1 < t_end)

    def value(self, i, state):
        """(lo, hi, path_lo, path_hi): min/max FUTURE realized PnL from the post-event state of sample ``i``."""
        key = (i, _key(state, self.mint))
        if key in self.memo:
            return self.memo[key]
        if self.mint not in state['positions']:
            result = (D(0), D(0), (), ())
        elif i == len(self.nodes) - 1:
            delta = self.liquidate(state, self.nodes[i])
            marker = (('HORIZON_END', 0.0, self.nodes[i].ts, None),)
            result = (delta, delta, marker, marker)
        else:
            result = self.gap(i, state)
        self.memo[key] = result
        return result

    def gap(self, i, state):
        a, b = self.nodes[i], self.nodes[i + 1]
        lo = D(0) if (b.dead or any(a.ts < t < b.ts for t in self.transient)) else (1 - self.excursion) * min(a.price, b.price)
        hi = (1 + self.excursion) * max(a.price, b.price)
        start = D(state['realized_pnl'])
        best = {'lo': None, 'hi': None}

        def record(total_lo, total_hi, path_lo, path_hi):
            # On a tie keep the path with fewer marks: the report shows the MINIMAL sequence that reaches a bound.
            if best['lo'] is None or total_lo < best['lo'][0] or (total_lo == best['lo'][0] and len(path_lo) < len(best['lo'][1])):
                best['lo'] = (total_lo, path_lo)
            if best['hi'] is None or total_hi > best['hi'][0] or (total_hi == best['hi'][0] and len(path_hi) < len(best['hi'][1])):
                best['hi'] = (total_hi, path_hi)

        def finish(st, path, closed_by_mark=False):
            if closed_by_mark or self.mint not in st['positions']:
                delta = D(st['realized_pnl']) - start
                record(delta, delta, path, path)
                return
            st2 = copy.deepcopy(st)
            if b.dead:                           # the pool died with the position still open: nothing can be sold
                delta = D(st2['realized_pnl']) - start - D(st2['positions'][self.mint]['cost_left'])
                marked = path + (('POOL_DEAD', 0.0, b.ts, None),)
                record(delta, delta, marked, marked)
                return
            self.feed(st2, b.ts, b.base, b.quote)
            delta = D(st2['realized_pnl']) - start
            f_lo, f_hi, p_lo, p_hi = self.value(i + 1, st2)
            record(delta + f_lo, delta + f_hi, path + p_lo, path + p_hi)

        def dfs(st, t_prev, left, path):
            finish(st, path)
            if left == 0:
                return
            for label, quote in self.levels(st, lo, hi, a.base):
                for ts in self.ts_choices(st, t_prev, b.ts, left):
                    st2 = copy.deepcopy(st)
                    self.feed(st2, ts, a.base, quote)
                    step = path + ((label, float(D(quote) / D(a.base)), ts, quote),)
                    if self.mint not in st2['positions']:
                        finish(st2, step, closed_by_mark=True)
                    else:
                        dfs(st2, ts, left - 1, step)

        dfs(copy.deepcopy(state), a.ts, self.marks, ())
        return (best['lo'][0], best['hi'][0], best['lo'][1], best['hi'][1])


def _find_entry(candidate, cfg, assumptions, features):
    """Nominal prefix up to the entry decision: entries are decided only on real samples."""
    points = _path_points(candidate)
    state = engine.initial_state(cfg)
    mint = candidate['mint']
    first_reject = None
    for index, point in enumerate(points):
        fields, known = _features_for(features, mint, point[0])
        if not known:
            first_reject = first_reject or 'FEATURES_NOT_YET_KNOWN'
            continue
        e = synth_event(candidate, point[0], point[1], point[2], f'e{index}', assumptions, fields)
        _, out = engine.transition(state, e, cfg)
        if next((x for x in out if x.get('type') == 'fill' and x.get('side') == 'buy'), None) is not None:
            return index, state, None
        first_reject = first_reject or next((x.get('reason') for x in out if x.get('type') == 'reject'), None)
    return None, None, first_reject


def _describe(path):
    return [{'mark': label, 'price': price, 'ts': ts, 'quote_raw': quote} for label, price, ts, quote in path]


def bracket_trade(candidate, cfg, assumptions, features=None):
    """Bracket [pnl_lo, pnl_hi] of the first entry under the stated gap assumption, plus the nominal run.

    Returns {'entered': False, 'reject': ...}, or a trade with ``ambiguous`` (bounds differ), the argmin /
    argmax mark sequences (``worst_path`` / ``best_path``) and ``truncated`` when the event budget ran out
    (then no bounds are reported). The nominal run is always inside the bracket (checked).
    """
    nominal = simulate(candidate, cfg, assumptions, 'samples', features)
    if not nominal['entered']:
        return nominal
    index, state, reject = _find_entry(candidate, cfg, assumptions, features)
    if index is None:
        raise ShadowError('Entry found by the nominal run but not by the bracket prefix')
    mint = candidate['mint']
    position = state['positions'][mint]
    cost = D(position['initial_cost'])
    explorer = _Explorer(candidate, cfg, assumptions)
    try:
        f_lo, f_hi, p_lo, p_hi = explorer.value(index, copy.deepcopy(state))
    except _Truncated:
        return {'entered': True, 'truncated': True, 'mint': mint, 'entry_at': nominal['entry_at'], 'nominal': nominal}
    pnl_lo, pnl_hi = D(state['realized_pnl']) + f_lo, D(state['realized_pnl']) + f_hi
    tolerance = D('1e-18')
    if not pnl_lo - tolerance <= nominal['pnl_sol'] <= pnl_hi + tolerance:
        raise ShadowError('Bracket does not contain the nominal run')
    return {'entered': True, 'truncated': False, 'mint': mint, 'entry_at': nominal['entry_at'], 'cost_sol': cost,
            'pnl_lo': pnl_lo, 'pnl_hi': pnl_hi, 'ret_lo': pnl_lo / cost, 'ret_hi': pnl_hi / cost,
            'ambiguous': pnl_lo != pnl_hi, 'exit_reasons': [nominal['exit_reason']], 'horizon_end': nominal['horizon_end'],
            'worst_path': _describe(p_lo), 'best_path': _describe(p_hi), 'nominal': nominal,
            'engine_events': explorer.events}


# ----------------------------------------------------------- statistics
def _quantile(sorted_values, q):
    """Linear interpolation between order statistics (type 7): no index collapse for tiny alpha."""
    position = q * (len(sorted_values) - 1)
    low = int(math.floor(position))
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def bootstrap_size(alpha):
    """Resamples grow with the correction so each tail keeps about 20 draws (bounded for run time)."""
    return int(min(MAX_BOOTSTRAP, max(BOOTSTRAP, math.ceil(40 / alpha))))


def _bootstrap(returns, name, alpha):
    if len(returns) < 2:
        return (None, None)
    rng = random.Random(int(hashlib.sha256(name.encode()).hexdigest()[:16], 16))
    values = [float(r) for r in returns]
    n, size = len(values), bootstrap_size(alpha)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(size))
    return (_quantile(means, alpha / 2), _quantile(means, 1 - alpha / 2))


def _max_drawdown(pnls):
    peak = worst = running = D(0)
    for p in pnls:
        running += p
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return worst


def summarize(trades, *, name='variant', n_variants=1):
    """Intervals over the bracketed trades (``bracket_trade`` output), ordered by entry time."""
    trades = sorted(trades, key=lambda t: (t['entry_at'], t['mint']))
    n = len(trades)
    if n == 0:
        return {'trades': 0, 'ambiguous_trades': 0, 'ambiguous_share': None, 'nominal_mean_return': None,
                'net_pnl_sol': (None, None), 'win_rate': (None, None),
                'mean_return': (None, None), 'max_drawdown_sol': (None, None), 'bootstrap_ci': (None, None), 'alpha': None,
                'bootstrap_resamples': None}
    alpha = 0.05 / max(1, n_variants)
    lo_ci = _bootstrap([t['ret_lo'] for t in trades], name + ':lo', alpha)
    hi_ci = _bootstrap([t['ret_hi'] for t in trades], name + ':hi', alpha)
    ambiguous = sum(t['ambiguous'] for t in trades)
    nominal = [t['nominal']['pnl_sol'] / t['cost_sol'] for t in trades if 'nominal' in t]
    return {'trades': n, 'ambiguous_trades': ambiguous, 'ambiguous_share': ambiguous / n,
            'nominal_mean_return': (sum(nominal, D(0)) / len(nominal)) if nominal else None,   # ONE possible history, not a bound
            'net_pnl_sol': (sum((t['pnl_lo'] for t in trades), D(0)), sum((t['pnl_hi'] for t in trades), D(0))),
            'win_rate': (Decimal(sum(1 for t in trades if t['pnl_lo'] > 0)) / n, Decimal(sum(1 for t in trades if t['pnl_hi'] > 0)) / n),
            'mean_return': (sum((t['ret_lo'] for t in trades), D(0)) / n, sum((t['ret_hi'] for t in trades), D(0)) / n),
            'max_drawdown_sol': tuple(sorted((_max_drawdown([t['pnl_lo'] for t in trades]), _max_drawdown([t['pnl_hi'] for t in trades])))),
            'bootstrap_ci': (lo_ci[0], hi_ci[1]) if lo_ci[0] is not None else (None, None), 'alpha': alpha,
            'bootstrap_resamples': bootstrap_size(alpha)}


def evaluate_variant(candidates, variant, base, assumptions, features=None):
    """(bracketed trades, entry-reject counts, truncated count) for one variant over the given candidates."""
    cfg = build_config(base, variant['overrides'])
    trades, rejects, truncated = [], {}, 0
    for cand in candidates:
        result = bracket_trade(cand, cfg, assumptions, features)
        if not result['entered']:
            reason = result['reject'] or 'NO_SAMPLE_PASSED'
            rejects[reason] = rejects.get(reason, 0) + 1
        elif result['truncated']:
            truncated += 1
        else:
            trades.append(result)
    return trades, rejects, truncated


def split_time(candidates, holdout_from):
    if holdout_from is None:
        ordered = sorted(c['migrated_at'] for c in candidates)
        holdout_from = ordered[int(len(ordered) * 0.7)] if ordered else 0
    return holdout_from


def rank_rows(board, min_trades=MIN_TRADES):
    """Holdout-only ranking: needs ``min_trades`` holdout trades; orders by the Bonferroni-corrected bootstrap CI LOWER
    bound of the mean return (over the worst-case bounds). Unranked rows follow, with the reason."""
    ranked, unranked = [], []
    for row in board:
        low = row['holdout']['bootstrap_ci'][0]
        if row['holdout']['trades'] >= min_trades and low is not None:
            row['rank_status'] = 'RANKED'
            ranked.append(row)
        else:
            row['rank_status'] = f"UNRANKED_FEWER_THAN_{min_trades}_HOLDOUT_TRADES"
            unranked.append(row)
    ranked.sort(key=lambda r: -r['holdout']['bootstrap_ci'][0])
    for position, row in enumerate(ranked, 1):
        row['rank'] = position
    for row in unranked:
        row['rank'] = None
    return ranked + sorted(unranked, key=lambda r: r['variant'])


def run(candidates, grid, *, holdout_from=None, features=None, outcomes=None, selection=None, min_trades=MIN_TRADES):
    cut = split_time(candidates, holdout_from)
    parts = {'train': [c for c in candidates if c['migrated_at'] < cut], 'holdout': [c for c in candidates if c['migrated_at'] >= cut]}
    n = len(grid['variants'])
    board = []
    for variant in grid['variants']:
        row = {'variant': variant['name'], 'overrides': variant['overrides']}
        for part, cands in parts.items():
            trades, rejects, truncated = evaluate_variant(cands, variant, grid['base'], grid['assumptions'], features)
            row[part] = summarize(trades, name=variant['name'] + part, n_variants=n)
            row[part]['entry_rejects'] = rejects
            row[part]['bracket_truncated'] = truncated
            row[part]['horizon_end_trades'] = sum(t['horizon_end'] for t in trades)
            if outcomes is not None:
                groups = {}
                for t in trades:
                    groups.setdefault(outcomes.get(t['mint'], 'UNKNOWN'), []).append(t)
                row[part]['by_outcome'] = {g: {k: v for k, v in summarize(ts, name=variant['name'] + part + g, n_variants=n).items()
                                               if k in ('trades', 'ambiguous_trades', 'ambiguous_share', 'mean_return', 'net_pnl_sol')}
                                           for g, ts in sorted(groups.items())}
        board.append(row)
    leaderboard = rank_rows(board, min_trades)
    a = grid['assumptions']
    return {'label': LABEL, 'grid': grid['name'], 'variants_tried': n, 'holdout_from': cut,
            'candidates': {k: len(v) for k, v in parts.items()}, 'selection': selection,
            'age_basis': 'seconds since migration HINT RECEIPT (the store has no on-chain migration time)',
            'ranking': f'holdout-only, minimum {min_trades} holdout trades, by the Bonferroni-corrected bootstrap CI LOWER bound of the '
                       'mean return over the worst-case bounds',
            'multiple_comparisons': f'{n} variants tried: bootstrap intervals use alpha=0.05/{n} (Bonferroni); the train ranking is shown, not used',
            'bracket_assumption': f"between two samples the live engine may observe up to {a['max_intra_marks']} extra marks at any price whose "
                                  f"mark ratio lies in [(1-{a['excursion_fraction']})*min(a,b), (1+{a['excursion_fraction']})*max(a,b)] "
                                  '(lo = 0 across a dead sample); fills at the observed price; max_intra_marks=0 is the nominal run',
            'assumptions': a, 'neutral_features': NEUTRAL_FEATURES, 'features_supplied': sorted(features) if features else [],
            'leaderboard': leaderboard}


# --------------------------------------------------------- parity replay
def replay_ledger(ledger_path):
    """Replay the RECORDED events of a (strict-mode) ledger through the shadow's own engine path and compare every
    decision with the outcomes the live writer recorded. Read-only; returns {'events','matched','mismatches'}."""
    with closing(cf._connect(ledger_path, readonly=True)) as c:
        c.execute('BEGIN')
        raw = c.execute("SELECT value FROM metadata WHERE key='config'").fetchone()
        if raw is None:
            raise ShadowError('Ledger has no saved config')
        cfg = json.loads(raw[0])
        from desk import quote_execution as qe
        if qe.config(cfg):
            raise ShadowError('Quote-execution ledgers need their recorded quotes and cannot be replayed from events alone')
        recorded = {}
        for event_id, payload in c.execute('SELECT event_id,payload FROM outcomes ORDER BY seq'):
            recorded.setdefault(event_id, []).append(payload)
        events = [(eid, json.loads(p)) for eid, p in c.execute('SELECT event_id,payload FROM events ORDER BY seq')]
    state = engine.initial_state(cfg)
    mismatches = []
    for event_id, event in events:
        _, out = engine.transition(state, event, cfg)
        got = [canonical(o) for o in out]
        want = recorded.get(event_id, [])
        if got != want:
            mismatches.append({'event_id': event_id, 'shadow': got, 'recorded': want})
    return {'events': len(events), 'matched': len(events) - len(mismatches), 'mismatches': mismatches}


# ----------------------------------------------------------------- CLI
def _fmt(x):
    if isinstance(x, tuple):
        return '[' + ', '.join(_fmt(i) for i in x) + ']'
    if x is None:
        return 'n/a'
    return f'{float(x):.4f}' if isinstance(x, (Decimal, float)) else str(x)


def render(result):
    lines = [f"{LABEL} — {result['grid']}: {result['variants_tried']} variants, holdout from {result['holdout_from']}",
             result['multiple_comparisons'], result['ranking'], 'bracket assumption: ' + result['bracket_assumption'],
             'age basis: ' + result['age_basis']]
    if result.get('selection'):
        lines.append('selection: ' + ', '.join(f'{k}={v}' for k, v in result['selection'].items() if k != 'funnel'))
        lines.append('selection funnel: ' + ' -> '.join(f"{f['stage']}(-{f['excluded']})={f['remaining']}" for f in result['selection']['funnel']))
    lines.append('')
    for r in result['leaderboard']:
        for part in ('holdout', 'train'):
            s = r[part]
            lines.append(f"#{r['rank'] or '-':>2} {r['variant'][:46]:46} {part:7} n={s['trades']:3} amb={s['ambiguous_trades']:3} ({_fmt(s['ambiguous_share'])}) "
                         f"pnl={_fmt(s['net_pnl_sol'])} win={_fmt(s['win_rate'])} mean={_fmt(s['mean_return'])} "
                         f"dd={_fmt(s['max_drawdown_sol'])} ci={_fmt(s['bootstrap_ci'])} nominal-mean={_fmt(s['nominal_mean_return'])}"
                         + ('' if part == 'train' else f"  [{r['rank_status']}]"))
    return '\n'.join(lines)


def _time(value):
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ShadowError('--holdout-from needs a timezone (use Z)')
        return stamp.timestamp()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--store')
    parser.add_argument('--grid')
    parser.add_argument('--holdout-from')
    parser.add_argument('--features', help='JSON {mint: {"as_of": epoch, <event feature overrides>}} from real decision evidence')
    parser.add_argument('--min-trades', type=int, default=MIN_TRADES, help='holdout trades required to be ranked')
    parser.add_argument('--no-outcomes', action='store_true', help='do not join the T26F outcome classification')
    parser.add_argument('--include-truncated', action='store_true', help='also evaluate paths that are not yet complete (flagged, liquidated at the last sample)')
    parser.add_argument('--parity-ledger', help='replay this ledger\'s recorded events through the shadow engine path and compare decisions')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.parity_ledger:
            result = replay_ledger(args.parity_ledger)
            print(json.dumps({'label': LABEL, **{k: result[k] for k in ('events', 'matched')}, 'mismatches': result['mismatches']}, sort_keys=True))
            return 0 if not result['mismatches'] else 3
        if not args.store or not args.grid:
            raise ShadowError('--store and --grid are required')
        selection = {}
        candidates = load_candidates(args.store, selection, include_truncated=args.include_truncated)
        features = load_features(args.features, candidates) if args.features else None
        outcomes = None if args.no_outcomes else load_outcomes(args.store)
        result = run(candidates, load_grid(args.grid), holdout_from=_time(args.holdout_from), features=features,
                     outcomes=outcomes, selection=selection, min_trades=args.min_trades)
    except (ShadowError, sqlite3.Error, OSError, ValueError, KeyError, cf.CounterfactualError) as error:
        print(json.dumps({'status': 'BLOCKED', 'error': type(error).__name__, 'label': LABEL}))
        return 2
    print(json.dumps(result, default=str, sort_keys=True) if args.json else render(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
