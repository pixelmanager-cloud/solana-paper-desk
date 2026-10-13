"""Offline exit-rule optimizer (C1): ``python -m lean.tune --lean-db L --paths-db P --grid grid.json --out-dir D``.

PAPER ONLY, read-only: both databases are opened in plain ``mode=ro``, nothing is sent anywhere, the trader is never touched,
and a proposal is NEVER applied (it is a file for the coordinator and CK to review).

Pipeline
  1. ``lean.replay`` replays every recorded path under every policy of the grid (real ``exit_decision`` on real
     ``adapters.mark`` marks, tiered PumpSwap fee + slippage + a fee per fill, fills at the next mark).
  2. Descriptive table, ALL data, in-sample and labelled so: EV per trade, a bootstrap CI, the first / second half of the
     paths, the 5th percentile, P(return <= -50%), exit reasons, how far fills land past the trigger. Benjamini-Hochberg
     q-values over the whole grid. This table selects nothing.
  3. Selection is a purged, time-blocked, expanding walk-forward: the paths are cut into ``blocks`` equal-count blocks by start
     time; for fold j the TRAIN set is blocks 0..j-1 minus every path whose observation window (``path_hours`` = the embargo)
     reaches into block j, and the TEST set is block j. On the train set only, each policy gets a centred-bootstrap p-value for
     "EV <= 0", Benjamini-Hochberg is applied over the grid, and the best train EV among the BH-significant policies is chosen
     (none significant = no selection). The test block is then read ONCE, for that policy only.
  4. The pooled out-of-sample series (every fold's test returns of the policy its train set chose) gets the headline CI.
     ``proposal.json`` is written ONLY when that CI's lower bound is above zero net of costs with at least ``min_oos_trades``
     trades; otherwise the file ``NO_PROPOSAL`` says why.
  5. Calibration (with the live ``lean.sqlite``): the live ladder is replayed on the paths of positions the trader really held and
     its exit reasons are compared with the live ones; a mismatch is listed, never hidden.
"""
import argparse
import datetime
import html
import json
import os
import random
import sqlite3
import sys
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

from lean import replay as R, strategy as S

LABEL = 'PAPER ONLY / EXECUTION_UNVERIFIED / OFFLINE REPLAY'
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STRATEGY = ROOT / 'config' / 'lean' / 'strategy-default.json'
GRID_KEYS = {'strategy', 'entry', 'costs', 'calibration_costs', 'calibration_lag', 'lag', 'path_hours', 'blocks', 'alpha', 'bootstrap', 'seed',
             'min_oos_trades', 'families'}


class TuneError(ValueError):
    pass


# ------------------------------------------------------------------------------------------------------- statistics
def mean(values):
    return sum(values) / len(values) if values else None


def quantile(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    index = (len(ordered) - 1) * q
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def bootstrap_means(values, n_boot, rng):
    n = len(values)
    return [sum(rng.choices(values, k=n)) / n for _ in range(n_boot)]


def mean_ci(values, n_boot, rng, level=0.95):
    """(mean, lower, upper): percentile bootstrap. (None, None, None) for no data; a single value has no interval."""
    if not values:
        return None, None, None
    m = mean(values)
    if len(values) < 2:
        return m, None, None
    boots = sorted(bootstrap_means(values, n_boot, rng))
    tail = (1 - level) / 2
    return m, quantile(boots, tail), quantile(boots, 1 - tail)


def p_value_not_positive(values, n_boot, rng):
    """One-sided centred-bootstrap p-value for H0: mean <= 0 (small = the mean is credibly above zero)."""
    if len(values) < 2:
        return 1.0
    m = mean(values)
    if m <= 0:
        return 1.0
    boots = bootstrap_means(values, n_boot, rng)
    return (1 + sum(1 for b in boots if b - m >= m)) / (n_boot + 1)


def benjamini_hochberg(pvalues):
    """BH adjusted p-values (q-values), same order as the input."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [1.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        q[i] = min(1.0, running)
    return q


def describe(trades, n_boot, rng):
    """Everything the report says about one policy's trades."""
    returns = [t.ret for t in trades]
    m, lo, hi = mean_ci(returns, n_boot, rng)
    ordered = sorted(trades, key=lambda t: t.start_ts)
    half = len(ordered) // 2
    first, second = [t.ret for t in ordered[:half]], [t.ret for t in ordered[half:]]
    past = [x for t in trades for x in t.past_trigger]
    reasons = Counter(r for t in trades for r in t.reasons)
    return {'n': len(returns), 'ev': m, 'ci_lo': lo, 'ci_hi': hi, 'median': quantile(returns, .5) if returns else None,
            'p5': quantile(returns, .05) if returns else None,
            'p_le_minus_50': (sum(1 for r in returns if r <= -0.5) / len(returns)) if returns else None,
            'ev_first_half': mean(first), 'ev_second_half': mean(second), 'n_first_half': len(first), 'n_second_half': len(second),
            'fill_past_trigger': {'n': len(past), 'p5': quantile(past, .05) if past else None, 'median': quantile(past, .5) if past else None,
                                  'p95': quantile(past, .95) if past else None, 'worst': min(past) if past else None},
            'exit_reasons': dict(sorted(reasons.items())), 'path_end_share': (sum(1 for t in trades if t.path_end) / len(trades)) if trades else None,
            'fills_across_a_gap': sum(t.crossed_gap for t in trades)}


# ------------------------------------------------------------------------------------------------------------ config
def load_grid(source):
    """A grid.json path (or the already parsed object) -> validated settings with the expanded ``policies``."""
    try:
        raw = source if isinstance(source, dict) else json.loads(Path(source).read_text())
    except (OSError, ValueError):
        raise TuneError('grid file unreadable') from None
    if not isinstance(raw, dict) or set(raw) - GRID_KEYS - {'_comment'}:
        raise TuneError('grid keys: %s' % sorted(GRID_KEYS))
    cfg = {'lag': 1, 'blocks': 5, 'alpha': 0.1, 'bootstrap': 1000, 'seed': 1, 'min_oos_trades': 30, 'path_hours': None,
           'entry': {'size_sol': '0.2'}, 'costs': {}, 'calibration_costs': None, 'calibration_lag': None, 'strategy': None}
    cfg.update({k: v for k, v in raw.items() if k != '_comment'})
    for name, low, high in (('blocks', 3, 50), ('bootstrap', 100, 100000), ('min_oos_trades', 1, 10 ** 7), ('seed', 0, 2 ** 31)):
        if type(cfg[name]) is not int or not low <= cfg[name] <= high:
            raise TuneError('%s must be an integer in [%d, %d]' % (name, low, high))
    for name in ('lag', 'calibration_lag'):
        if cfg[name] is not None and (type(cfg[name]) is not int or cfg[name] not in (0, 1)):
            raise TuneError('%s must be 0 or 1' % name)
    if isinstance(cfg['alpha'], bool) or not isinstance(cfg['alpha'], (int, float)) or not 0 < cfg['alpha'] < 1:
        raise TuneError('alpha must be in (0, 1)')
    if cfg['path_hours'] is not None and (isinstance(cfg['path_hours'], bool) or not isinstance(cfg['path_hours'], (int, float))
                                          or not 0 < cfg['path_hours'] <= 72):
        raise TuneError('path_hours must be in (0, 72]')
    if not isinstance(cfg['entry'], dict) or set(cfg['entry']) - {'size_sol'}:
        raise TuneError('entry keys: size_sol')
    try:
        cfg['size_lamports'] = R.paper.to_lamports(cfg['entry'].get('size_sol', '0.2'))
    except R.paper.PaperError:
        raise TuneError('entry.size_sol must be a positive whole number of lamports') from None
    try:
        cfg['policies'] = R.expand_grid(cfg.get('families'))
    except R.ReplayError as error:
        raise TuneError(str(error)) from None
    return cfg


# ------------------------------------------------------------------------------------------------------ the analysis
def run_policies(prepared, policies, base_cfg, costs, lag):
    """{policy name: [Trade]} over every enterable path (independent of any fold: nothing here can leak)."""
    return {p.name: [R.replay_path(prep, p, base_cfg, costs, lag) for prep in prepared] for p in policies}


def time_blocks(trades_by_policy, order, blocks):
    """Indices of the time-ordered paths split into ``blocks`` equal-count contiguous blocks."""
    n = len(order)
    return [order[n * b // blocks: n * (b + 1) // blocks] for b in range(blocks)]


def walk_forward(prepared, results, policies, cfg, rng, embargo_s):
    """Purged, time-blocked, expanding walk-forward. Returns {'folds': [...], 'pooled': {...}}."""
    order = sorted(range(len(prepared)), key=lambda i: (prepared[i].path.start_ts, prepared[i].path.mint))
    blocks = time_blocks(None, order, cfg['blocks'])
    folds, pooled, selections = [], [], []
    for j in range(1, len(blocks)):
        test = blocks[j]
        if not test:
            continue
        test_start = min(prepared[i].path.start_ts for i in test)
        train = [i for b in blocks[:j] for i in b if prepared[i].path.start_ts + embargo_s <= test_start]
        purged = sum(len(b) for b in blocks[:j]) - len(train)
        fold = {'fold': j, 'train_paths': len(train), 'purged_paths': purged, 'test_paths': len(test),
                'test_start': test_start, 'selected': None, 'reason': None}
        if len(train) < 10:
            fold['reason'] = 'TRAIN_TOO_SMALL'
            folds.append(fold)
            continue
        pvals, means = [], []
        for p in policies:
            r = [results[p.name][i].ret for i in train]
            pvals.append(p_value_not_positive(r, cfg['bootstrap'], rng))
            means.append(mean(r))
        qvals = benjamini_hochberg(pvals)
        ok = [k for k, q in enumerate(qvals) if q <= cfg['alpha'] and means[k] > 0]
        fold['n_policies'] = len(policies)
        fold['n_significant'] = len(ok)
        if not ok:
            fold['reason'] = 'NO_POLICY_SURVIVES_BH'
            folds.append(fold)
            continue
        best = max(ok, key=lambda k: (means[k], policies[k].name))
        policy = policies[best]
        test_returns = [results[policy.name][i].ret for i in test]          # the test block is read ONCE, for this policy only
        m, lo, hi = mean_ci(test_returns, cfg['bootstrap'], rng)
        fold.update(selected=policy.name, train_ev=means[best], train_q=qvals[best], test_n=len(test_returns), test_ev=m,
                    test_ci_lo=lo, test_ci_hi=hi)
        pooled.extend(test_returns)
        selections.append(policy.name)
        folds.append(fold)
    m, lo, hi = mean_ci(pooled, cfg['bootstrap'], rng)
    return {'folds': folds, 'pooled': {'n': len(pooled), 'ev': m, 'ci_lo': lo, 'ci_hi': hi,
                                       'p5': quantile(pooled, .05) if pooled else None,
                                       'p_le_minus_50': (sum(1 for r in pooled if r <= -0.5) / len(pooled)) if pooled else None},
            'selections': selections}


def decide_proposal(wf, cfg, results, policies):
    """(proposal dict or None, reason). Only a pooled out-of-sample CI whose lower bound is above zero, net of costs."""
    pooled = wf['pooled']
    if not wf['selections']:
        return None, 'NO_FOLD_SELECTED_A_POLICY'
    if pooled['n'] < cfg['min_oos_trades']:
        return None, 'OOS_TRADES_BELOW_MINIMUM (%d < %d)' % (pooled['n'], cfg['min_oos_trades'])
    if pooled['ci_lo'] is None or pooled['ci_lo'] <= 0:
        return None, 'OOS_CI_LOWER_BOUND_NOT_ABOVE_ZERO (%s)' % (None if pooled['ci_lo'] is None else '%.4f' % pooled['ci_lo'])
    latest = next(f for f in reversed(wf['folds']) if f['selected'])
    # LINT5: the policy proposed must ALSO clear zero on its own test block (the pooled series mixes every fold's selection)
    if latest.get('test_ci_lo') is None or latest['test_ci_lo'] <= 0:
        return None, 'PROPOSED_POLICY_TEST_BLOCK_CI_NOT_ABOVE_ZERO (%s)' % (
            None if latest.get('test_ci_lo') is None else '%.4f' % latest['test_ci_lo'])
    policy = next(p for p in policies if p.name == latest['selected'])
    return {'kind': 'exit_policy_proposal_v1', 'label': LABEL, 'apply': 'NEVER AUTOMATIC: review by the coordinator and CK first',
            'policy': policy.name, 'family': policy.family, 'params': dict(policy.params),
            'selected_in_folds': wf['selections'], 'evidence': {'pooled_out_of_sample': pooled, 'latest_fold': latest}}, None


# ---------------------------------------------------------------------------------------------------- calibration
def live_reasons(lean_db):
    """{mint: [live exit reasons in order]} of positions that CLOSED, from the trader's position_state (read-only)."""
    connection = R._ro(lean_db)
    try:
        out = {}
        buys = {m for (m,) in connection.execute("SELECT DISTINCT mint FROM fills WHERE side='buy'")}
        for mint, event, text in connection.execute("SELECT mint, event, state FROM position_state WHERE event IN ('rung','closed') ORDER BY id"):
            try:
                state = json.loads(text)
            except ValueError:
                continue
            reason = state.get('reason') if event == 'closed' else state.get('last_reason')
            out.setdefault(mint, []).append((event, reason))
        closed = {m for m, rows in out.items() if rows and rows[-1][0] == 'closed'}
        return {m: [r for _, r in out[m]] for m in out if m in closed and m in buys}
    finally:
        connection.close()


def calibrate(lean_db, prepared, base_cfg, costs, lag):
    live = live_reasons(lean_db)
    by_mint = {p.path.mint: p for p in prepared}
    policy = R.Policy('live_ladder', ())
    rows, matched = [], 0
    for mint, reasons in sorted(live.items()):
        prep = by_mint.get(mint)
        if prep is None:
            rows.append({'mint': mint, 'live': reasons, 'replay': None, 'match': None, 'note': 'NO_PATH_OR_NOT_ENTERABLE'})
            continue
        replayed = R.replay_path(prep, policy, base_cfg, costs, lag).reasons
        ok = replayed == reasons
        matched += ok
        rows.append({'mint': mint, 'live': reasons, 'replay': replayed, 'match': ok, 'note': None})
    compared = sum(1 for r in rows if r['match'] is not None)
    return {'compared': compared, 'matched': matched, 'mismatches': [r for r in rows if r['match'] is False],
            'unmatched_paths': [r for r in rows if r['match'] is None], 'rows': rows}


# --------------------------------------------------------------------------------------------------------- the run
def analyse(lean_db, paths_db, grid, *, strategy_path=None, fees_path=None, now=None, max_paths=None):
    cfg = load_grid(grid)
    base_cfg = S.StrategyConfig.load(strategy_path or cfg['strategy'] or DEFAULT_STRATEGY)
    tiers = R.load_fee_tiers(fees_path or R.DEFAULT_FEES)
    try:
        costs = R.costs_from(cfg['costs'], tiers)
        cal_costs = R.costs_from(cfg['calibration_costs'], tiers) if cfg['calibration_costs'] is not None else costs
    except R.ReplayError as error:
        raise TuneError(str(error)) from None
    loaded = R.load_paths(paths_db)
    paths = sorted(loaded.values(), key=lambda p: (p.start_ts, p.mint))
    if max_paths:
        paths = paths[:max_paths]
    prepared = [x for x in (R.prepare(p, costs, cfg['size_lamports']) for p in paths) if x is not None]
    policies = cfg['policies']
    path_hours = cfg['path_hours'] or max([(p.ends_ts - p.start_ts) / 3600 for p in paths] or [6.0])
    embargo = path_hours * 3600
    rng = random.Random(cfg['seed'])
    results = run_policies(prepared, policies, base_cfg, costs, cfg['lag'])
    # descriptive, in-sample, whole data
    rows = []
    for p in policies:
        trades = results[p.name]
        d = describe(trades, cfg['bootstrap'], rng)
        d.update(policy=p.name, family=p.family, params=dict(p.params),
                 p_value=p_value_not_positive([t.ret for t in trades], cfg['bootstrap'], rng))
        rows.append(d)
    for row, q in zip(rows, benjamini_hochberg([r['p_value'] for r in rows])):
        row['q_value'] = q
    if len(prepared) >= 2 * cfg['blocks']:
        wf = walk_forward(prepared, results, policies, cfg, rng, embargo)
        proposal, why = decide_proposal(wf, cfg, results, policies)
    else:
        wf, proposal, why = {'folds': [], 'pooled': {'n': 0}, 'selections': []}, None, 'INSUFFICIENT_DATA (%d enterable paths, need %d)' % (len(prepared), 2 * cfg['blocks'])
    calibration = None
    if lean_db is not None:
        try:
            calibration = calibrate(lean_db, prepared, base_cfg, cal_costs, cfg['lag'] if cfg['calibration_lag'] is None else cfg['calibration_lag'])
        except (R.ReplayError, sqlite3.Error) as error:
            calibration = {'error': type(error).__name__}
    data = {'paths_total': len(paths), 'enterable': len(prepared), 'marks': sum(len(x.ts) for x in prepared),
            'gaps': sum(len(p.gaps) for p in paths), 'files': [f.name for f in R.archive_files(paths_db)],
            'unenterable': len(paths) - len(prepared), 'entered_live': sum(1 for x in prepared if x.path.entered),
            'span': [paths[0].start_ts, paths[-1].start_ts] if paths else None}
    return {'kind': 'lean_tune_v1', 'label': LABEL, 'generated_at': datetime.datetime.fromtimestamp(time.time() if now is None else now, datetime.timezone.utc).isoformat(),
            'settings': {k: cfg[k] for k in ('lag', 'blocks', 'alpha', 'bootstrap', 'seed', 'min_oos_trades')} | {
                'path_hours': path_hours, 'embargo_seconds': embargo, 'size_lamports': cfg['size_lamports'],
                'costs': {'fee_model': costs.fee_model, 'flat_bps': costs.flat_bps, 'slippage_bps': costs.slippage_bps,
                          'fee_lamports': costs.fee_lamports, 'fee_tiers': costs.tiers.source},
                'strategy_version': base_cfg.strategy_version, 'policies': len(policies)},
            'data': data, 'policies': rows, 'walk_forward': wf, 'calibration': calibration,
            'proposal': proposal, 'no_proposal_reason': why,
            'notes': ['The policy table is in-sample and descriptive: it selects nothing.',
                      'Selection is on train blocks only; each test block is read once.']}


# ----------------------------------------------------------------------------------------------------------- output
def _f(value, places=4):
    return '' if value is None else ('%.*f' % (places, value) if isinstance(value, float) else str(value))


def render_html(r):
    e = html.escape
    t = lambda headers, rows: '<div class="w"><table><tr>%s</tr>%s</table></div>' % (
        ''.join('<th>%s</th>' % e(h) for h in headers), ''.join('<tr>%s</tr>' % ''.join('<td>%s</td>' % e(str(c)) for c in row) for row in rows))
    d, wf = r['data'], r['walk_forward']
    parts = ['<h1>Exit-rule optimizer</h1><p class="b">%s &middot; %s &middot; not trading evidence. Nothing here is applied automatically.</p>' % (e(LABEL), e(r['generated_at'])),
             '<h2>Data</h2><p>%d paths (%d enterable, %d entered live), %d usable marks, %d recorded gaps, files: %s</p>' % (
                 d['paths_total'], d['enterable'], d['entered_live'], d['marks'], d['gaps'], e(', '.join(d['files']))),
             '<p>Costs: %s &middot; lag %s &middot; embargo %s s &middot; size %s lamports</p>' % (
                 e(json.dumps(r['settings']['costs'])), r['settings']['lag'], r['settings']['embargo_seconds'], r['settings']['size_lamports'])]
    parts.append('<h2>Verdict</h2><p>%s</p>' % (e('PROPOSAL: ' + r['proposal']['policy']) if r['proposal'] else e('NO_PROPOSAL: %s' % r['no_proposal_reason'])))
    pooled = wf['pooled']
    parts.append('<h2>Out-of-sample (walk-forward, pooled)</h2><p>n=%s EV=%s CI=[%s, %s] p5=%s P(&le;-50%%)=%s</p>' % (
        pooled.get('n'), _f(pooled.get('ev')), _f(pooled.get('ci_lo')), _f(pooled.get('ci_hi')), _f(pooled.get('p5')), _f(pooled.get('p_le_minus_50'))))
    parts.append(t(['fold', 'train', 'purged', 'test', 'selected', 'train EV', 'q', 'test n', 'test EV', 'test CI lo', 'test CI hi', 'why not'],
                   [[f['fold'], f['train_paths'], f['purged_paths'], f['test_paths'], f.get('selected') or '-', _f(f.get('train_ev')), _f(f.get('train_q')),
                     f.get('test_n', ''), _f(f.get('test_ev')), _f(f.get('test_ci_lo')), _f(f.get('test_ci_hi')), f.get('reason') or ''] for f in wf['folds']]))
    parts.append('<h2>All policies (IN-SAMPLE, descriptive only)</h2>')
    parts.append(t(['policy', 'n', 'EV', 'CI lo', 'CI hi', 'q (BH)', 'EV 1st half', 'EV 2nd half', 'p5', 'P(&le;-50%)', 'past trigger p5', 'median', 'worst', 'PATH_END', 'gap fills', 'exits'],
                   [[x['policy'], x['n'], _f(x['ev']), _f(x['ci_lo']), _f(x['ci_hi']), _f(x['q_value'], 3), _f(x['ev_first_half']), _f(x['ev_second_half']),
                     _f(x['p5']), _f(x['p_le_minus_50'], 3), _f(x['fill_past_trigger']['p5']), _f(x['fill_past_trigger']['median']), _f(x['fill_past_trigger']['worst']),
                     _f(x['path_end_share'], 3), x['fills_across_a_gap'], json.dumps(x['exit_reasons'])] for x in r['policies']]))
    c = r['calibration']
    parts.append('<h2>Calibration (live ladder replayed on the live paths)</h2>')
    if c is None:
        parts.append('<p>No live database.</p>')
    elif 'error' in c:
        parts.append('<p>ERROR %s</p>' % e(c['error']))
    else:
        parts.append('<p>%d compared, %d matched, %d mismatched, %d without a usable path</p>' % (c['compared'], c['matched'], len(c['mismatches']), len(c['unmatched_paths'])))
        parts.append(t(['mint', 'live', 'replay'], [[m['mint'], ','.join(map(str, m['live'])), ','.join(map(str, m['replay']))] for m in c['mismatches']]))
    css = ('body{font:14px/1.45 system-ui,sans-serif;margin:0;background:#fafafa;color:#1a1a1a}main{max-width:1200px;margin:0 auto;padding:16px}'
           'table{border-collapse:collapse;background:#fff;font-size:12px}th,td{border:1px solid #ddd;padding:3px 6px;text-align:left}.b{background:#fff3cd;padding:8px}'
           '.w{overflow-x:auto}@media(prefers-color-scheme:dark){body{background:#161616;color:#e8e8e8}table{background:#1f1f1f}th,td{border-color:#444}.b{background:#3a3217}}')
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Exit-rule optimizer</title><style>%s</style></head><body><main>%s</main></body></html>' % (css, ''.join(parts))


def _write_exclusive(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(text)


def write_outputs(out_dir, report):
    out_dir = Path(out_dir)
    if out_dir.is_symlink() or not out_dir.is_dir():
        raise TuneError('out-dir missing or a symlink')
    names = ['tune-report.html', 'tune-report.json', 'proposal.json' if report['proposal'] else 'NO_PROPOSAL']
    if any(os.path.lexists(out_dir / n) for n in ('tune-report.html', 'tune-report.json', 'proposal.json', 'NO_PROPOSAL')):
        raise TuneError('an output file already exists: nothing is overwritten')
    written = []
    try:
        _write_exclusive(out_dir / names[0], render_html(report))
        written.append(out_dir / names[0])
        _write_exclusive(out_dir / names[1], json.dumps(report, sort_keys=True, default=str, allow_nan=False) + '\n')
        written.append(out_dir / names[1])
        if report['proposal']:
            _write_exclusive(out_dir / names[2], json.dumps(report['proposal'], sort_keys=True, default=str, indent=1) + '\n')
        else:
            _write_exclusive(out_dir / names[2], 'NO_PROPOSAL: %s\n' % report['no_proposal_reason'])
        written.append(out_dir / names[2])
    except BaseException:
        for p in written:
            os.unlink(p)
        raise
    return written


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--lean-db', required=True, help='lean.sqlite (opened read-only; calibration against live exits)')
    p.add_argument('--paths-db', required=True, help='paths.sqlite (+ the paths-*.sqlite archives beside it), read-only')
    p.add_argument('--grid', required=True)
    p.add_argument('--out-dir', required=True)
    p.add_argument('--strategy', help='StrategyConfig json (default: the grid\'s "strategy", else config/lean/strategy-default.json)')
    p.add_argument('--fees', help='fee tier table (default fixtures/mainnet-dynamic-fees.json)')
    p.add_argument('--max-paths', type=int, help='only the first N paths by start time (quick runs)')
    p.add_argument('--now', type=float)
    a = p.parse_args(argv)
    try:
        report = analyse(a.lean_db, a.paths_db, a.grid, strategy_path=a.strategy, fees_path=a.fees, now=a.now, max_paths=a.max_paths)
        written = write_outputs(a.out_dir, report)
    except (TuneError, R.ReplayError, S.ConfigError, sqlite3.Error, OSError) as error:
        print(json.dumps({'status': 'ERROR', 'code': type(error).__name__, 'detail': str(error)[:200]}))
        return 2
    print(json.dumps({'status': 'OK', 'proposal': bool(report['proposal']), 'files': [str(x) for x in written]}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
