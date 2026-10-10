"""Shadow strategy variants: parallel virtual A/B on the T26 counterfactual price paths.

    python -m tools.research.shadow_strategies --store counterfactual.sqlite \
        --grid config/experiments/shadow/exit-grid.json [--holdout-from UTC|epoch] [--json]

SIMULATED_SHADOW, never trading evidence, no provider request, no ledger. Every
variant is evaluated by calling the REAL ``desk.engine.transition`` on events
synthesized from the sampled PumpSwap reserves (provenance SYNTHETIC_TEST_ONLY,
in-memory state only). Costs come from the engine: pool fee, adverse slippage
and the fixed fee. Each candidate is simulated in an isolated state, first entry
only, so trades are comparable across variants.

Coarse paths: the store holds a handful of samples per candidate, so the order of events inside a window
is unknown. The simulation is strictly causal (a decision never sees a later sample) and brackets what it
cannot know:
  samples  the engine sees only the sampled instants, so exits fill at the first sampled price (late, gap risk)
  carry    the engine is also called at the position's time-stop / max-hold deadline inside a window, with the
           last sample seen before it
  stops    a STOP / TRAILING_STOP seen at a sample may really have crossed earlier; its best case fills exactly
           at the level the engine tested (an analytic bound; the observed-sample fill is the worst case)
Every statistic is reported as an interval [worst, best]; a trade whose bounds differ is AMBIGUOUS. Entries
are decided only on real samples.
Signal features (flow, holders, wash, ...) are NOT in the price paths: unless a
features file supplies them, every candidate gets the same neutral features, so
variants differ in structure (windows, stop, trailing, time-stop, max-hold,
sizing), not in signal quality. The take-profit ladder is fixed inside
``desk.engine.manage_position`` and is not a config key, so it is not a grid axis.
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
from decimal import Decimal, localcontext
from pathlib import Path

from desk import engine
from desk.model import D

LABEL = 'SIMULATED_SHADOW'
MODELS = ('samples', 'carry')
DEFAULT_ASSUMPTIONS = {'sol_usd': '150', 'token_decimals': 6, 'token_supply_tokens': '1000000000',
                       'pool_fee_bps': '25'}
BOOTSTRAP = 4000
TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
NEUTRAL_FEATURES = {
    'graduated': True, 'mint_revoked': True, 'freeze_revoked': True, 'lp_verified': True, 'extensions_safe': True,
    'data_healthy': True, 'flow_confirmed': False, 'danger': False, 'route_available': True,
    'top10_pct': '10', 'dev_pct': '0', 'bundle_pct': '0', 'cluster_pct': '0', 'fresh_wallet_ratio': '0',
    'flow': '90', 'manip_safety': '0', 'manip_flow': '0', 'net_buy_ratio': '.8', 'unique_buyers_5m': 50,
    'volume_vs_liq': '2', 'drawdown_from_high': '0', 'wash_score': '0', 'dev_launches_7d': 1}


class ShadowError(ValueError):
    pass


# ---------------------------------------------------------------- inputs
def load_candidates(store):
    """Priced counterfactual paths (read-only). Dead/failed/missed samples carry no price and are skipped."""
    out = []
    with closing(sqlite3.connect(Path(store).as_uri() + '?mode=ro', uri=True)) as c:
        c.execute('BEGIN')
        for mint, pool, migrated in c.execute('SELECT mint,pool,migrated_at FROM candidates ORDER BY migrated_at,mint').fetchall():
            rows = c.execute("SELECT horizon,base_raw,quote_raw FROM samples WHERE mint=? AND status='OK' AND price IS NOT NULL ORDER BY horizon", (mint,)).fetchall()
            died = c.execute("SELECT COUNT(*) FROM samples WHERE mint=? AND status='POOL_DEAD'", (mint,)).fetchone()[0] > 0
            samples = [{'horizon': h, 'base_raw': int(b), 'quote_raw': int(q)} for h, b, q in rows]
            if samples:
                out.append({'mint': mint, 'pool': pool, 'migrated_at': float(migrated), 'samples': samples, 'pool_died': died})
    return out


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


def build_config(base, overrides):
    cfg = copy.deepcopy(base)
    for key, value in overrides.items():
        if key not in cfg or type(value) is not type(cfg[key]):
            raise ShadowError(f'Override {key!r} must exist in the base config with the same type')
        cfg[key] = value
    return cfg


# ------------------------------------------------------------ synthesis
def _clean_evidence(ts):
    return {'as_of': ts, 'launch_history_complete': True, 'funding_history_complete': True,
            'holder_coverage_pct': '100', 'launch_slot': 1000,
            'holders': [{'wallet': f'w{i:03}', 'pct': '1'} for i in range(100)],
            'early_buys': [{'wallet': f'w{i:03}', 'slot': 1010 + i, 'ts': ts - 300 + i} for i in range(100)],
            'funding': [], 'transfers': [], 'known_bad_wallets': []}


def synth_event(candidate, ts, base_raw, quote_raw, serial, assumptions, features=None):
    """A strict-mode market event whose reserves are the sampled vault balances (SYNTHETIC_TEST_ONLY)."""
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


# ------------------------------------------------------------ simulation
def simulate(candidate, cfg, assumptions, model, features=None):
    """First entry only, strictly causal. Returns a trade dict, or {'entered': False, 'reject': reason}.

    ``samples`` feeds the engine only at sampled instants. ``carry`` additionally feeds it at the position's
    timer deadlines (time-stop, max-hold) that fall inside a window, using the LAST sample seen before the
    deadline. Neither ever reads a sample taken after the decision instant.
    """
    if model not in MODELS:
        raise ShadowError('Unknown path model')
    points = _path_points(candidate)
    state = engine.initial_state(cfg)
    mint = candidate['mint']
    serial = 0
    entry = None
    first_reject = None

    def feed(ts, base_raw, quote_raw):
        nonlocal serial
        serial += 1
        before = copy.deepcopy(state['positions'].get(mint))
        e = synth_event(candidate, ts, base_raw, quote_raw, serial, assumptions, features)
        _, out = engine.transition(state, e, cfg)
        return out, before

    for index, point in enumerate(points):
        if entry is not None and model == 'carry':
            opened = state['positions'][mint]['opened_at']
            previous = points[index - 1]
            for deadline in sorted({opened + cfg['time_stop_seconds'], opened + cfg['max_hold_seconds']}):
                if previous[0] < deadline < point[0]:
                    out, before = feed(deadline, previous[1], previous[2])
                    if mint not in state['positions']:
                        return _trade(candidate, cfg, state, entry, deadline, out, before)
        out, before = feed(*point)
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
    # A stop that fires at the first sample after a crossing could really have filled anywhere between the
    # observed sample price and the stop level. Best case = filled exactly at the level the engine tested.
    best = pnl
    level = None
    if fill['reason'] == 'STOP':
        level = D(before['stop_ratio'])
    elif fill['reason'] == 'TRAILING_STOP':
        level = D(before['peak_ratio']) * (1 - D(cfg['trailing_fraction']))
    if level is not None:
        best = pnl - D(fill['realized_pnl_sol']) + D(before['cost_left']) * (level - 1)
        best = max(pnl, best)
    return {'entered': True, 'mint': candidate['mint'], 'entry_at': entry['at'], 'exit_at': exit_at,
            'exit_reason': fill['reason'], 'pnl_sol': pnl, 'pnl_best_sol': best, 'cost_sol': cost,
            'return': pnl / cost, 'horizon_end': False}


# ----------------------------------------------------------- statistics
def combine(per_model):
    """One bracketed trade per entered candidate: lo = worst model, hi = best case over models and stop levels."""
    out = []
    models = list(per_model)
    for i in range(len(per_model[models[0]])):
        trades = [per_model[m][i] for m in models]
        cost = trades[0]['cost_sol']
        lo = min(t['pnl_sol'] for t in trades)
        hi = max(t['pnl_best_sol'] for t in trades)
        out.append({'mint': trades[0]['mint'], 'entry_at': trades[0]['entry_at'], 'cost_sol': cost,
                    'pnl_lo': lo, 'pnl_hi': hi, 'ret_lo': lo / cost, 'ret_hi': hi / cost,
                    'exit_reasons': sorted({t['exit_reason'] for t in trades}), 'ambiguous': lo != hi})
    return out


def _max_drawdown(pnls):
    peak = worst = running = D(0)
    for p in pnls:
        running += p
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return worst


def _bootstrap(returns, name, alpha):
    if len(returns) < 2:
        return (None, None)
    rng = random.Random(int(hashlib.sha256(name.encode()).hexdigest()[:16], 16))
    values = [float(r) for r in returns]
    means = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(BOOTSTRAP))
    return (means[max(0, int(math.floor(alpha / 2 * BOOTSTRAP)))], means[min(BOOTSTRAP - 1, int(math.ceil((1 - alpha / 2) * BOOTSTRAP)) - 1)])


def summarize(trades, *, name='variant', n_variants=1):
    """Intervals over the bracketed trades (``combine`` output), ordered by entry time."""
    trades = sorted(trades, key=lambda t: (t['entry_at'], t['mint']))
    n = len(trades)
    if n == 0:
        return {'trades': 0, 'ambiguous_trades': 0, 'net_pnl_sol': (None, None), 'win_rate': (None, None),
                'mean_return': (None, None), 'max_drawdown_sol': (None, None), 'bootstrap_ci': (None, None), 'alpha': None}
    alpha = 0.05 / max(1, n_variants)
    lo_ci = _bootstrap([t['ret_lo'] for t in trades], name + ':lo', alpha)
    hi_ci = _bootstrap([t['ret_hi'] for t in trades], name + ':hi', alpha)
    return {'trades': n, 'ambiguous_trades': sum(t['ambiguous'] for t in trades),
            'net_pnl_sol': (sum((t['pnl_lo'] for t in trades), D(0)), sum((t['pnl_hi'] for t in trades), D(0))),
            'win_rate': (Decimal(sum(1 for t in trades if t['pnl_lo'] > 0)) / n, Decimal(sum(1 for t in trades if t['pnl_hi'] > 0)) / n),
            'mean_return': (sum((t['ret_lo'] for t in trades), D(0)) / n, sum((t['ret_hi'] for t in trades), D(0)) / n),
            'max_drawdown_sol': tuple(sorted((_max_drawdown([t['pnl_lo'] for t in trades]), _max_drawdown([t['pnl_hi'] for t in trades])))),
            'bootstrap_ci': (lo_ci[0], hi_ci[1]) if lo_ci[0] is not None else (None, None), 'alpha': alpha}


def evaluate_variant(candidates, variant, base, assumptions, features=None):
    cfg = build_config(base, variant['overrides'])
    per_model = {m: [] for m in MODELS}
    rejects = {}
    for cand in candidates:
        results = {m: simulate(cand, cfg, assumptions, m, (features or {}).get(cand['mint'])) for m in MODELS}
        if not results['samples']['entered']:
            reason = results['samples']['reject'] or 'NO_SAMPLE_PASSED'
            rejects[reason] = rejects.get(reason, 0) + 1
            if results['carry']['entered']:
                raise ShadowError('Timer events must never open a position')
            continue
        for m in MODELS:
            per_model[m].append(results[m])
    return (combine(per_model) if per_model['samples'] else []), rejects


def split_time(candidates, holdout_from):
    if holdout_from is None:
        ordered = sorted(c['migrated_at'] for c in candidates)
        holdout_from = ordered[int(len(ordered) * 0.7)] if ordered else 0
    return holdout_from


def run(candidates, grid, *, holdout_from=None, features=None):
    cut = split_time(candidates, holdout_from)
    parts = {'train': [c for c in candidates if c['migrated_at'] < cut], 'holdout': [c for c in candidates if c['migrated_at'] >= cut]}
    n = len(grid['variants'])
    board = []
    for variant in grid['variants']:
        row = {'variant': variant['name'], 'overrides': variant['overrides']}
        for part, cands in parts.items():
            trades, rejects = evaluate_variant(cands, variant, grid['base'], grid['assumptions'], features)
            row[part] = summarize(trades, name=variant['name'] + part, n_variants=n)
            row[part]['entry_rejects'] = rejects
        board.append(row)
    def key(r):  # holdout-only ranking on the conservative end of the mean-return interval
        lo = r['holdout']['mean_return'][0]
        return (lo is None, -(lo if lo is not None else 0))
    ranked = sorted(board, key=key)
    return {'label': LABEL, 'grid': grid['name'], 'variants_tried': n, 'holdout_from': cut,
            'candidates': {k: len(v) for k, v in parts.items()},
            'ranking': 'holdout-only, by the LOWER bound of the mean return over path models',
            'multiple_comparisons': f'{n} variants tried: bootstrap intervals use alpha=0.05/{n} (Bonferroni); the train ranking is shown, not used',
            'assumptions': grid['assumptions'], 'leaderboard': ranked}


# ----------------------------------------------------------------- CLI
def _fmt(x):
    if isinstance(x, tuple):
        return '[' + ', '.join(_fmt(i) for i in x) + ']'
    if x is None:
        return 'n/a'
    return f'{float(x):.4f}' if isinstance(x, (Decimal, float)) else str(x)


def render(result):
    lines = [f"{LABEL} — {result['grid']}: {result['variants_tried']} variants, holdout from {result['holdout_from']}",
             result['multiple_comparisons'], result['ranking'], '']
    for r in result['leaderboard']:
        for part in ('holdout', 'train'):
            s = r[part]
            lines.append(f"{r['variant'][:48]:48} {part:7} n={s['trades']:3} amb={s['ambiguous_trades']:3} pnl={_fmt(s['net_pnl_sol'])} "
                         f"win={_fmt(s['win_rate'])} mean={_fmt(s['mean_return'])} dd={_fmt(s['max_drawdown_sol'])} ci={_fmt(s['bootstrap_ci'])}")
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
    parser.add_argument('--store', required=True)
    parser.add_argument('--grid', required=True)
    parser.add_argument('--holdout-from')
    parser.add_argument('--features', help='JSON {mint: {event feature overrides}} from real decision evidence')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        features = json.loads(Path(args.features).read_text()) if args.features else None
        result = run(load_candidates(args.store), load_grid(args.grid), holdout_from=_time(args.holdout_from), features=features)
    except (ShadowError, sqlite3.Error, OSError, ValueError, KeyError) as error:
        print(json.dumps({'status': 'BLOCKED', 'error': type(error).__name__, 'label': LABEL}))
        return 2
    print(json.dumps(result, default=str, sort_keys=True) if args.json else render(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
