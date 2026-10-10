"""Read-only forward-evaluation report for versioned paper experiments.

    python -m tools.research.forward_eval --ledger L1 [--ledger L2 ...] [--holdout-from UTC]

Paper-only and EXECUTION_UNVERIFIED. Every ledger is first validated with the
existing ``desk.experiment_report`` replay (fail closed). Rows are keyed by
config_hash; ledgers with different config hashes are never merged.
"""
import argparse
import json
import os
import random
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path

from desk.experiment_report import experiment_report
from desk.paper_checkpoint import RecoveryRequired

MIN_SAMPLE = 30
EPS = Decimal('1e-20')
LABEL = 'EXECUTION_UNVERIFIED'


class EvalError(ValueError):
    pass


def parse_time(value):
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise EvalError('--holdout-from needs a timezone (use Z)')
        return int(stamp.timestamp())


def read_ledger(path, now=None):
    """Validated trade list + identity for one ledger. Never writes."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise EvalError('Ledger must be a regular, non-symlinked file: %s' % path)
    try:
        validation = experiment_report(path, now=now)
    except RecoveryRequired as error:
        raise EvalError('Ledger failed validation (%s): %s' % (error, path)) from None
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as c:
        c.execute('PRAGMA query_only=ON')
        meta = dict(c.execute("SELECT key,value FROM metadata WHERE key IN ('config','config_hash','implementation_hash')"))
        cfg = json.loads(meta['config'])
        ts = {event_id: t for event_id, t in c.execute('SELECT event_id,ts FROM events')}
        open_trades, trades = {}, []
        for seq, event_id, payload in c.execute('SELECT seq,event_id,payload FROM outcomes ORDER BY seq'):
            outcome = json.loads(payload)
            if outcome.get('type') != 'fill':
                continue
            mint, at = outcome['mint'], ts[event_id]
            if outcome['side'] == 'buy':
                open_trades[mint] = {'mint': mint, 'opened_at': at, 'entry_seq': seq,
                                     'cost': Decimal(outcome['amount_sol']) + Decimal(outcome['fee_sol']),
                                     'qty': Decimal(outcome['quantity']), 'pnl': Decimal(0),
                                     'reasons': [], 'fees': Decimal(outcome['fee_sol'])}
            else:
                t = open_trades[mint]
                t['qty'] -= Decimal(outcome['quantity'])
                t['pnl'] += Decimal(outcome['realized_pnl_sol'])
                t['reasons'].append(outcome['reason'])
                if t['qty'] <= EPS:
                    t.update(closed_at=at, closed_seq=seq, exit_reason=outcome['reason'])
                    trades.append(open_trades.pop(mint))
    for t in trades:
        t['return'] = t['pnl'] / t['cost']
        t['hold_seconds'] = t['closed_at'] - t['opened_at']
    return {'path': str(path), 'config_hash': meta['config_hash'],
            'implementation_hash': meta['implementation_hash'],
            'version': cfg.get('version'), 'initial_equity': Decimal(cfg['initial_equity_sol']),
            'quote_mode': cfg.get('paper_quote_execution_version') == 1,
            'trades': trades, 'open_positions': validation['open_position_count'],
            'data_provenance': validation['data_provenance'], 'identity': os.stat(path)[1:3]}


def bootstrap_ci(values, resamples, seed):
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    floats = [float(v) for v in values]
    n = len(floats)
    means = sorted(sum(rng.choices(floats, k=n)) / n for _ in range(resamples))
    return [round(means[int(0.025 * (resamples - 1))], 10), round(means[int(0.975 * (resamples - 1))], 10)]


def median(values):
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def max_drawdown(trades, initial):
    equity = peak = initial
    worst = Decimal(0)
    for t in sorted(trades, key=lambda t: t['closed_seq']):
        equity += t['pnl']
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    return worst


def block(trades, initial, resamples, seed):
    n = len(trades)
    out = {'closed_trades': n}
    if n < MIN_SAMPLE:
        out['warning'] = 'INSUFFICIENT_SAMPLE: %d closed trades < %d; do not infer anything' % (n, MIN_SAMPLE)
    if not n:
        return out
    returns = [t['return'] for t in trades]
    with localcontext() as ctx:
        ctx.prec = 60
        total = sum((t['pnl'] for t in trades), Decimal(0))
        out.update({
            'wins': sum(t['pnl'] > 0 for t in trades), 'losses': sum(t['pnl'] < 0 for t in trades),
            'flat': sum(t['pnl'] == 0 for t in trades),
            'net_pnl_sol': str(total),
            'mean_return': str(sum(returns, Decimal(0)) / n), 'median_return': str(median(returns)),
            'mean_hold_seconds': str(Decimal(sum(t['hold_seconds'] for t in trades)) / n),
            'median_hold_seconds': str(median([Decimal(t['hold_seconds']) for t in trades])),
            'exit_reasons': {r: sum(t['exit_reason'] == r for t in trades)
                             for r in sorted({t['exit_reason'] for t in trades})},
            'partial_take_profit_fills': sum(t['reasons'][:-1].count('TAKE_PROFIT') for t in trades),
            'max_drawdown_fraction': str(max_drawdown(trades, initial)),
            'mean_return_bootstrap_ci95': bootstrap_ci(returns, resamples, seed),
            'bootstrap': {'resamples': resamples, 'seed': seed, 'method': 'percentile'},
            'mfe_mae': {'available': False, 'reason': 'MARKS_NOT_RECORDED: the journal stores per-fill outcomes, '
                        'not per-event held marks; per-trade MFE/MAE would need an unrecorded mark series'},
        })
    return out


def evaluate(paths, holdout_from=None, now=None, resamples=2000, seed=0, single=False):
    ledgers, seen = [], set()
    for path in paths:
        ledger = read_ledger(path, now)
        if ledger['identity'] in seen:
            raise EvalError('Same ledger given twice: %s' % path)
        seen.add(ledger['identity'])
        ledgers.append(ledger)
    groups = {}
    for ledger in ledgers:
        groups.setdefault(ledger['config_hash'], []).append(ledger)
    if single and len(groups) > 1:
        raise EvalError('Ledgers belong to %d different experiments (config hashes); refused' % len(groups))
    experiments = []
    for config_hash, members in groups.items():
        trades = [t for m in members for t in m['trades']]
        initial = members[0]['initial_equity']
        row = {'config_hash': config_hash, 'version': members[0]['version'],
               'implementation_hashes': sorted({m['implementation_hash'] for m in members}),
               'ledgers': [m['path'] for m in members], 'execution_status': LABEL, 'paper_only': True,
               'data_provenance': sorted({m['data_provenance'] for m in members}),
               'quote_mode': members[0]['quote_mode'],
               'open_positions_excluded': sum(m['open_positions'] for m in members),
               'all': block(trades, initial, resamples, seed)}
        if holdout_from is not None:
            row['holdout_from'] = holdout_from
            row['train'] = block([t for t in trades if t['closed_at'] < holdout_from], initial, resamples, seed)
            row['holdout'] = block([t for t in trades if t['closed_at'] >= holdout_from], initial, resamples, seed)
        experiments.append(row)
    return {'kind': 'forward_eval_v1', 'execution_status': LABEL, 'paper_only': True,
            'profitability_verdict': 'NOT_ASSESSED', 'experiments': experiments}


def warnings(report):
    for row in report['experiments']:
        for name in ('all', 'train', 'holdout'):
            if name in row and 'warning' in row[name]:
                yield '%s [%s] %s' % (row['config_hash'][:12], name, row[name]['warning'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--ledger', action='append', required=True)
    parser.add_argument('--holdout-from')
    parser.add_argument('--now', type=int)
    parser.add_argument('--resamples', type=int, default=2000)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--single-experiment', action='store_true')
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.ledger, parse_time(args.holdout_from), args.now,
                          args.resamples, args.seed, args.single_experiment)
    except (EvalError, sqlite3.Error, ValueError, KeyError, OSError) as error:
        print(json.dumps({'status': 'REFUSED', 'reason': str(error)}), file=sys.stderr)
        return 2
    for line in warnings(report):
        print('WARNING ' + line, file=sys.stderr)
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
