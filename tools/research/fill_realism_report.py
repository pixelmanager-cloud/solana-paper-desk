"""Read-only latency-adjusted fill report (EXECUTION_UNVERIFIED, paper only).

``python -m tools.research.fill_realism_report --ledger L`` reads the ledger
with mode=ro, verifies every stored +2/+5/+10s sample against its decision fill
and recomputes the drift from raw integers, then reports per-trade and
aggregate drift plus the paper round trip re-priced as if each leg had filled at
the delayed re-quote ("what a bot with N seconds of latency would have got").
Unverifiable samples are listed under integrity_errors and excluded; failed or
missing samples are counted, never imputed. Nothing is written.
"""
import argparse
from decimal import Decimal, localcontext
import json
import os
from pathlib import Path
import sqlite3
import sys
from contextlib import closing

from desk import fill_realism as fr
from desk.model import digest

SIDES = ('buy', 'sell')
STATS = ('out_drift_bps', 'adverse_slippage_bps', 'price_drift_bps')
MAX_ERRORS = 50


def _quantile(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    position = Decimal(len(ordered) - 1) * Decimal(str(q))
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _stats(values):
    if not values:
        return None
    with localcontext() as ctx:
        ctx.prec = 60
        return {'n': len(values), 'mean': format(sum(values) / len(values), 'f'),
                'median': format(_quantile(values, 0.5), 'f'), 'p10': format(_quantile(values, 0.1), 'f'),
                'p90': format(_quantile(values, 0.9), 'f'), 'min': format(min(values), 'f'),
                'max': format(max(values), 'f')}


def _verify(row, fill):
    """Return an error code, or None when the sample is bound to its fill and self-consistent."""
    record = fill['payload'].get('quote_execution') or {}
    decision = (row['decision_input_raw'], row['decision_estimated_out_raw'], row['decision_simulated_out_raw'])
    if (fill['payload'].get('side') != row['side'] or fill['payload'].get('mint') != row['mint']
            or decision != (record.get('input_raw'), record.get('estimated_output_raw'), record.get('simulated_output_raw'))
            or row['decision_quote_hash'] != record.get('quote_hash')):
        return 'DECISION_BINDING_MISMATCH'
    if row['status'] != 'MEASURED':
        return None
    try:
        if digest(json.loads(row['requote_json'])) != row['requote_hash']:
            return 'REQUOTE_HASH_MISMATCH'
        stored = fr.drift(row['side'], decision, row['requote_estimated_out_raw'],
                          row['requote_simulated_out_raw'], record['mint_decimals'])
        if any(Decimal(stored[k]) != Decimal(row[k]) for k in stored):
            return 'DRIFT_MISMATCH'
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return 'SAMPLE_UNVERIFIABLE'
    return None


def _trades(fills):
    """Pair fills per mint in ledger order: a BUY opens, SELLs attach until quantity is sold."""
    open_by_mint, trades = {}, []
    for fill in fills:
        payload = fill['payload']
        if payload['side'] == 'buy':
            trade = {'mint': payload['mint'], 'buy': fill, 'sells': [], 'qty': Decimal(payload['quantity']), 'sold': Decimal(0)}
            open_by_mint[payload['mint']] = trade
            trades.append(trade)
        elif payload['mint'] in open_by_mint:
            trade = open_by_mint[payload['mint']]
            trade['sells'].append(fill)
            trade['sold'] += Decimal(payload['quantity'])
            if trade['sold'] >= trade['qty']:
                del open_by_mint[payload['mint']]
    return trades


def report(ledger):
    path = Path(ledger)
    if os.path.islink(path) or not path.is_file():
        raise ValueError('Existing non-symlink ledger required')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as c:
        c.row_factory = sqlite3.Row
        meta = dict(c.execute("SELECT key,value FROM metadata WHERE key IN ('config_hash','config')").fetchall())
        config = json.loads(meta['config']) if meta.get('config') else {}
        fills = [{'event_id': r['event_id'], 'ts': r['ts'], 'payload': json.loads(r['payload'])} for r in c.execute(
            "SELECT o.event_id AS event_id,e.ts AS ts,o.payload AS payload FROM outcomes o JOIN events e "
            "ON e.event_id=o.event_id WHERE json_extract(o.payload,'$.type')='fill' ORDER BY o.seq")]
        have = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (fr.TABLE,)).fetchone()
        rows = [dict(r) for r in c.execute(f'SELECT * FROM {fr.TABLE} ORDER BY fill_event_id,side,delay_seconds')] if have else []
    base = {'kind': 'paper_fill_realism_report_v1', 'execution_status': fr.STATUS, 'paper_only': True,
            'live_readiness': False, 'config_hash': meta.get('config_hash'), 'config_version': config.get('version'),
            'realism_version': config.get(fr.KEY), 'delays_seconds': list(fr.DELAYS), 'fills': len(fills)}
    if not have:
        return {**base, 'status': 'NO_REALISM_DATA', 'samples': 0}
    by_fill = {(f['event_id'], f['payload']['side']): f for f in fills}
    errors, good = [], {}
    for row in rows:
        fill = by_fill.get((row['fill_event_id'], row['side']))
        code = 'UNKNOWN_FILL' if fill is None else _verify(row, fill)
        if code:
            if len(errors) < MAX_ERRORS:
                errors.append({'fill_event_id': row['fill_event_id'], 'side': row['side'],
                               'delay_seconds': row['delay_seconds'], 'code': code})
            continue
        good[(row['fill_event_id'], row['side'], row['delay_seconds'])] = row
    aggregate = {side: {} for side in SIDES}
    for side in SIDES:
        for delay in fr.DELAYS:
            sel = [r for (e, s, d), r in good.items() if s == side and d == delay]
            measured = [r for r in sel if r['status'] == 'MEASURED']
            codes = {}
            for r in sel:
                if r['status'] == 'FAILED':
                    codes[r['code']] = codes.get(r['code'], 0) + 1
            aggregate[side][str(delay)] = {'measured': len(measured), 'failed': len(sel) - len(measured),
                'charged_attempts': sum(r['charged'] for r in sel), 'failure_codes': codes,
                **{k: _stats([Decimal(r[k]) for r in measured]) for k in STATS}}
    out_trades, summary = [], {str(d): {'trades_measured': 0, 'trades_unmeasured': 0, 'paper_pnl_sol': Decimal(0),
                                         'latency_adjusted_pnl_sol': Decimal(0)} for d in fr.DELAYS}
    for trade in _trades(fills):
        buy, sells = trade['buy'], trade['sells']
        closed = trade['sold'] >= trade['qty']
        with localcontext() as ctx:
            ctx.prec = 60
            paper = (sum((Decimal(s['payload']['proceeds_sol']) for s in sells), Decimal(0))
                     - Decimal(buy['payload']['amount_sol']) - Decimal(buy['payload']['fee_sol']))
        item = {'mint': trade['mint'], 'entry_event_id': buy['event_id'], 'status': 'CLOSED' if closed else 'OPEN_OR_PARTIAL',
                'exit_event_ids': [s['event_id'] for s in sells], 'execution_status': fr.STATUS,
                'paper_pnl_sol': format(paper, 'f') if closed else None,
                'ledger_realized_pnl_sol': format(sum((Decimal(s['payload']['realized_pnl_sol']) for s in sells), Decimal(0)), 'f'),
                'drift': {}, 'latency_adjusted_pnl_sol': {}}
        for delay in fr.DELAYS:
            legs = [good.get((f['event_id'], f['payload']['side'], delay)) for f in [buy, *sells]]
            item['drift'][str(delay)] = {('buy' if i == 0 else 'sell_%d' % i): (
                {'out_drift_bps': leg['out_drift_bps'], 'adverse_slippage_bps': leg['adverse_slippage_bps'],
                 'price_drift_bps': leg['price_drift_bps']} if leg and leg['status'] == 'MEASURED' else None)
                for i, leg in enumerate(legs)}
            if not closed:
                item['latency_adjusted_pnl_sol'][str(delay)] = None
            elif any(leg is None or leg['status'] != 'MEASURED' for leg in legs):
                item['latency_adjusted_pnl_sol'][str(delay)] = None
                summary[str(delay)]['trades_unmeasured'] += 1
            else:
                adjusted = fr.latency_adjusted_pnl(buy['payload'], [s['payload'] for s in sells], legs[0], legs[1:])
                item['latency_adjusted_pnl_sol'][str(delay)] = format(adjusted, 'f')
                s = summary[str(delay)]
                s['trades_measured'] += 1
                s['paper_pnl_sol'] += paper
                s['latency_adjusted_pnl_sol'] += adjusted
        out_trades.append(item)
    pnl = {d: {'trades_measured': v['trades_measured'], 'trades_unmeasured': v['trades_unmeasured'],
               'paper_pnl_sol': format(v['paper_pnl_sol'], 'f'),
               'latency_adjusted_pnl_sol': format(v['latency_adjusted_pnl_sol'], 'f'),
               'delta_sol': format(v['latency_adjusted_pnl_sol'] - v['paper_pnl_sol'], 'f')} for d, v in summary.items()}
    return {**base, 'status': 'REPORTED', 'samples': len(rows), 'verified_samples': len(good),
            'integrity_errors': errors, 'aggregate': aggregate, 'pnl_by_delay': pnl, 'trades': out_trades,
            'model': 'each leg re-priced at the delayed re-quote; tokens received scale proceeds; fixed fees unchanged'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only latency-adjusted paper fill report (EXECUTION_UNVERIFIED)')
    parser.add_argument('--ledger', required=True)
    args = parser.parse_args(argv)
    try:
        result, code = report(args.ledger), 0
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError, json.JSONDecodeError):
        result, code = {'kind': 'paper_fill_realism_report_v1', 'status': 'UNAVAILABLE', 'execution_status': fr.STATUS,
                        'paper_only': True, 'blockers': ['LEDGER_UNAVAILABLE_OR_INVALID']}, 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
