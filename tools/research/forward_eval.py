"""Read-only forward-evaluation report for versioned paper experiments.

    python -m tools.research.forward_eval --ledger L1 [--ledger L2 ...] [--holdout-from UTC]

Paper-only and EXECUTION_UNVERIFIED. Every ledger is first validated with the
existing ``desk.experiment_report`` replay (fail closed) and must not change
while it is evaluated. Rows are keyed by (config_hash, implementation_hash);
ledgers with different config hashes are never merged.
"""
import argparse
import hashlib
import json
import random
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path

from desk.experiment_report import experiment_report
from desk.paper_checkpoint import RecoveryRequired
from desk.strategy import swap_quote

MIN_SAMPLE = 30
EPS = Decimal('1e-20')
LABEL = 'EXECUTION_UNVERIFIED'
MAX_TRADE_ROWS = 500
POOL_MODEL = ('SEQUENTIAL_SINGLE_BASELINE: trades of all pooled ledgers are ordered by (closed_at, ledger path, '
              'outcome seq) and replayed on ONE equity curve that starts at the shared initial equity; '
              'it is not a portfolio of N independent ledgers')


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


def _fingerprint(c):
    """Cheap identity of everything the evaluation reads; equal fingerprints = same data."""
    parts = []
    for table in ('events', 'outcomes'):
        parts.append(c.execute('SELECT count(*),coalesce(max(seq),0) FROM ' + table).fetchone())
    state = c.execute('SELECT payload FROM state').fetchall()
    meta = c.execute('SELECT key,value FROM metadata ORDER BY key').fetchall()
    return hashlib.sha256(json.dumps([parts, state, meta]).encode()).hexdigest()


def _connect(path):
    c = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2, isolation_level=None)
    c.execute('PRAGMA query_only=ON')
    return c


def _marks(trade, events, cfg, ttl):
    """MFE/MAE of one trade from journaled held market events (constant-product model marks)."""
    sells = sorted(trade['sells'])
    ratios = []
    for seq, ts, e in events:
        if not trade['opened_seq'] < seq <= trade['closed_event_seq']:
            continue
        qty, cost = trade['qty0'], trade['cost']
        for sell_seq, sold in sells:
            if sell_seq < seq:
                cost -= cost * sold / qty
                qty -= sold
        if qty <= EPS or e.get('kind') != 'market' or not e.get('route_available') \
                or ts - e['price_at'] > ttl:
            continue
        with localcontext() as ctx:
            ctx.prec = 60
            mark = max(Decimal(0), swap_quote(e, qty, 'sell', cfg) - Decimal(cfg['fixed_fee_sol']))
            ratios.append(mark / cost)
    if not ratios:
        return {'status': 'NO_HELD_MARKS', 'mfe': None, 'mae': None, 'samples': 0}
    return {'status': 'OK', 'mfe': max(ratios) - 1, 'mae': min(ratios) - 1, 'samples': len(ratios)}


def read_ledger(path, now=None):
    """Validated trade list + identity for one ledger. Never writes."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise EvalError('Ledger must be a regular, non-symlinked file: %s' % path)
    try:
        with closing(_connect(path)) as c:
            before = _fingerprint(c)
        validation = experiment_report(path, now=now)
    except RecoveryRequired as error:
        raise EvalError('Ledger failed validation (%s): %s' % (error, path)) from None
    except sqlite3.Error as error:
        raise EvalError('Ledger unreadable (%s): %s' % (error, path)) from None
    key = str(path.resolve(strict=True))
    with closing(_connect(path)) as c:
        c.execute('BEGIN')  # one snapshot for everything below
        if _fingerprint(c) != before:
            raise EvalError('LEDGER_CHANGED_DURING_EVAL (between validation and read): %s' % path)
        meta = dict(c.execute("SELECT key,value FROM metadata WHERE key IN ('config','config_hash','implementation_hash')"))
        cfg = json.loads(meta['config'])
        quote_mode = cfg.get('paper_quote_execution_version') == 1
        info = {event_id: (seq, ts) for event_id, seq, ts in c.execute('SELECT event_id,seq,ts FROM events')}
        open_trades, trades = {}, []
        for seq, event_id, payload in c.execute('SELECT seq,event_id,payload FROM outcomes ORDER BY seq'):
            outcome = json.loads(payload)
            if outcome.get('type') != 'fill':
                continue
            mint, (event_seq, at) = outcome['mint'], info[event_id]
            if outcome['side'] == 'buy':
                open_trades[mint] = {'mint': mint, 'ledger': key, 'opened_at': at, 'opened_seq': event_seq,
                                     'cost': Decimal(outcome['amount_sol']) + Decimal(outcome['fee_sol']),
                                     'qty0': Decimal(outcome['quantity']), 'qty': Decimal(outcome['quantity']),
                                     'pnl': Decimal(0), 'reasons': [], 'sells': []}
            else:
                t = open_trades[mint]
                sold = Decimal(outcome['quantity'])
                t['qty'] -= sold
                t['pnl'] += Decimal(outcome['realized_pnl_sol'])
                t['reasons'].append(outcome['reason'])
                t['sells'].append((event_seq, sold))
                if t['qty'] <= EPS:
                    t.update(closed_at=at, closed_seq=seq, closed_event_seq=event_seq, exit_reason=outcome['reason'])
                    trades.append(open_trades.pop(mint))
        held = {}
        if trades and not quote_mode:
            wanted = {t['mint'] for t in trades}
            for seq, ts, payload in c.execute('SELECT seq,ts,payload FROM events ORDER BY seq'):
                e = json.loads(payload)
                if e.get('mint') in wanted:
                    held.setdefault(e['mint'], []).append((seq, ts, e))
    with closing(_connect(path)) as c:
        if _fingerprint(c) != before:
            raise EvalError('LEDGER_CHANGED_DURING_EVAL (after read): %s' % path)
    for t in trades:
        t['return'] = t['pnl'] / t['cost']
        t['hold_seconds'] = t['closed_at'] - t['opened_at']
        t['marks'] = ({'status': 'UNAVAILABLE_QUOTE_MODE', 'mfe': None, 'mae': None, 'samples': 0} if quote_mode
                      else _marks(t, held.get(t['mint'], []), cfg, cfg['price_ttl_seconds']))
    stat = path.stat()
    return {'path': key, 'config_hash': meta['config_hash'],
            'implementation_hash': meta['implementation_hash'],
            'version': cfg.get('version'), 'initial_equity': Decimal(cfg['initial_equity_sol']),
            'quote_mode': quote_mode, 'trades': trades, 'open_positions': validation['open_position_count'],
            'data_provenance': validation['data_provenance'], 'identity': (stat.st_dev, stat.st_ino)}


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


def order(trades):
    return sorted(trades, key=lambda t: (t['closed_at'], t['ledger'], t['closed_seq']))


def max_drawdown(trades, start_equity):
    equity = peak = start_equity
    worst = Decimal(0)
    for t in order(trades):
        equity += t['pnl']
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    return worst


def _marks_block(trades):
    statuses = {t['marks']['status'] for t in trades}
    marked = [t['marks'] for t in trades if t['marks']['status'] == 'OK']
    if not marked:
        return {'status': 'UNAVAILABLE_QUOTE_MODE' if 'UNAVAILABLE_QUOTE_MODE' in statuses else 'NO_HELD_MARKS',
                'trades_with_marks': 0}
    mfe, mae = [m['mfe'] for m in marked], [m['mae'] for m in marked]
    return {'status': 'OK', 'trades_with_marks': len(marked),
            'basis': 'constant-product model mark of the remaining position at each journaled held market '
                     'event, relative to remaining cost; no sell_simulation cap applied, so MFE can be overstated',
            'mean_mfe': str(sum(mfe, Decimal(0)) / len(mfe)), 'max_mfe': str(max(mfe)),
            'mean_mae': str(sum(mae, Decimal(0)) / len(mae)), 'min_mae': str(min(mae))}


def block(trades, start_equity, resamples, seed):
    n = len(trades)
    out = {'closed_trades': n, 'start_equity_sol': str(start_equity)}
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
            'max_drawdown_fraction': str(max_drawdown(trades, start_equity)),
            'mean_return_bootstrap_ci95': bootstrap_ci(returns, resamples, seed),
            'bootstrap': {'resamples': resamples, 'seed': seed, 'method': 'percentile'},
            'mfe_mae': _marks_block(trades),
        })
    return out


def _trade_rows(trades):
    rows = [{'mint': t['mint'], 'ledger': t['ledger'], 'opened_at': t['opened_at'], 'closed_at': t['closed_at'],
             'exit_reason': t['exit_reason'], 'pnl_sol': str(t['pnl']), 'return': str(t['return']),
             'mfe_mae_status': t['marks']['status'],
             'mfe': None if t['marks']['mfe'] is None else str(t['marks']['mfe']),
             'mae': None if t['marks']['mae'] is None else str(t['marks']['mae'])}
            for t in order(trades)]
    return rows[:MAX_TRADE_ROWS], len(rows) > MAX_TRADE_ROWS


def evaluate(paths, holdout_from=None, now=None, resamples=2000, seed=0, single=False,
             pool_implementations=False):
    ledgers, seen = [], set()
    for path in paths:
        ledger = read_ledger(path, now)
        if ledger['identity'] in seen:
            raise EvalError('Same ledger given twice: %s' % path)
        seen.add(ledger['identity'])
        ledgers.append(ledger)
    if single and len({l['config_hash'] for l in ledgers}) > 1:
        raise EvalError('Ledgers belong to different experiments (config hashes); refused')
    groups = {}
    for ledger in ledgers:
        key = (ledger['config_hash'],) if pool_implementations else (ledger['config_hash'], ledger['implementation_hash'])
        groups.setdefault(key, []).append(ledger)
    experiments = []
    for key in sorted(groups):
        members = sorted(groups[key], key=lambda m: m['path'])
        trades = [t for m in members for t in m['trades']]
        initial = members[0]['initial_equity']
        if any(m['initial_equity'] != initial for m in members):
            raise EvalError('Initial equity differs inside one config hash')
        impls = sorted({m['implementation_hash'] for m in members})
        row = {'config_hash': key[0], 'version': members[0]['version'], 'implementation_hashes': impls,
               'ledgers': [m['path'] for m in members], 'execution_status': LABEL, 'paper_only': True,
               'data_provenance': sorted({m['data_provenance'] for m in members}),
               'quote_mode': members[0]['quote_mode'],
               'open_positions_excluded': sum(m['open_positions'] for m in members),
               'equity_baseline_sol': str(initial), 'pooled_equity_model': POOL_MODEL,
               'all': block(trades, initial, resamples, seed)}
        if len(impls) > 1:
            row['warnings'] = ['POOLED_IMPLEMENTATIONS: %d different implementation hashes share this row '
                               '(--pool-implementations); results may mix different code behaviour' % len(impls)]
        row['trades'], row['trades_truncated'] = _trade_rows(trades)
        if holdout_from is not None:
            at_cut = initial + sum((t['pnl'] for t in trades if t['closed_at'] < holdout_from), Decimal(0))
            row.update(holdout_from=holdout_from, split_basis='ENTRY_TIME',
                       split_note='train = trades ENTERED before the cut (they may close after it); holdout = '
                                  'trades entered at/after it. Holdout drawdown starts from equity at the cut '
                                  '(initial + PnL of trades closed before it); train drawdown from initial equity.',
                       train=block([t for t in trades if t['opened_at'] < holdout_from], initial, resamples, seed),
                       holdout=block([t for t in trades if t['opened_at'] >= holdout_from], at_cut, resamples, seed))
        experiments.append(row)
    return {'kind': 'forward_eval_v2', 'execution_status': LABEL, 'paper_only': True,
            'profitability_verdict': 'NOT_ASSESSED', 'experiments': experiments}


def warnings(report):
    for row in report['experiments']:
        for name in ('all', 'train', 'holdout'):
            if name in row and 'warning' in row[name]:
                yield '%s [%s] %s' % (row['config_hash'][:12], name, row[name]['warning'])
        for text in row.get('warnings', ()):
            yield '%s %s' % (row['config_hash'][:12], text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--ledger', action='append', required=True)
    parser.add_argument('--holdout-from')
    parser.add_argument('--now', type=int)
    parser.add_argument('--resamples', type=int, default=2000)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--single-experiment', action='store_true')
    parser.add_argument('--pool-implementations', action='store_true',
                        help='merge ledgers with the same config hash but different implementation hashes')
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.ledger, parse_time(args.holdout_from), args.now, args.resamples, args.seed,
                          args.single_experiment, args.pool_implementations)
    except (EvalError, sqlite3.Error, ValueError, KeyError, OSError) as error:
        print(json.dumps({'status': 'REFUSED', 'reason': str(error)}), file=sys.stderr)
        return 2
    for line in warnings(report):
        print('WARNING ' + line, file=sys.stderr)
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
