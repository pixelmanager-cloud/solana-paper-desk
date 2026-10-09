"""Bounded read-only experiment accounting; never a profitability acceptance gate.

Usage: python -m desk.experiment_report /path/to/paper.sqlite [--now EPOCH]
JSON goes to stdout; invalid/partial evidence exits nonzero without repairing it.
"""
import argparse
import json
import sqlite3
import time
from contextlib import closing
from decimal import localcontext
from pathlib import Path

from .model import decimal, digest
from .paper_checkpoint import read_checkpoint, RecoveryRequired

MAX_ROWS = 10000
MAX_BYTES = 32 * 1024 * 1024
EPS = decimal('1e-20')  # Existing engine's inventory-close threshold.


def _number(value):
    if not isinstance(value, str) or len(value) > 128:
        raise RecoveryRequired('REPORT_DECIMAL_INVALID')
    number = decimal(value)
    if abs(number.as_tuple().exponent) > 128 or len(number.as_tuple().digits) > 128:
        raise RecoveryRequired('REPORT_DECIMAL_LIMIT')
    return number


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def experiment_report(path, *, now=None):
    now = int(time.time()) if now is None else now
    if type(now) is not int or now < 0:
        raise ValueError('Invalid report observation time')
    path = Path(path)
    if not path.is_file():
        raise RecoveryRequired('EXPERIMENT_MISSING')
    if path.stat().st_size > 128 * 1024 * 1024:
        raise RecoveryRequired('REPORT_DATABASE_LIMIT')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as c:
        c.execute('PRAGMA query_only=ON'); c.execute('BEGIN')
        total = 0
        for table in ('events', 'outcomes', 'state', 'metadata', 'raw_events'):
            column = 'value' if table == 'metadata' else 'payload'
            count, size, largest = c.execute(
                f'SELECT COUNT(*),COALESCE(SUM(length(CAST({column} AS BLOB))),0),'
                f'COALESCE(MAX(length(CAST({column} AS BLOB))),0) FROM '
                f'(SELECT {column} FROM {table} LIMIT {MAX_ROWS + 1})').fetchone()
            if count > MAX_ROWS or largest > 2 * 1024 * 1024:
                raise RecoveryRequired('REPORT_ROW_LIMIT')
            total += size
        if total > MAX_BYTES:
            raise RecoveryRequired('REPORT_BYTE_LIMIT')
        state = read_checkpoint(c)
        if state is None:
            raise RecoveryRequired('EXPERIMENT_EMPTY')
        if len(state['positions']) > 100:
            raise RecoveryRequired('REPORT_POSITION_LIMIT')
        identity = dict(c.execute("SELECT key,value FROM metadata WHERE key IN ('config','config_hash','implementation_hash')"))
        if not all(_hash(identity[k]) for k in ('config_hash', 'implementation_hash')):
            raise RecoveryRequired('EXPERIMENT_IDENTITY_INVALID')
        cfg = json.loads(identity['config'])
        ttl = cfg['price_ttl_seconds']
        if type(ttl) is not int or ttl <= 0:
            raise RecoveryRequired('REPORT_CONFIG_INVALID')
        events = {}
        markets = []; prices = []; provenance = {'synthetic': 0, 'non_synthetic_claims': 0, 'unknown': 0}
        first = last = None
        for event_id, ts, payload, key in c.execute('SELECT event_id,ts,payload,payload_hash FROM events ORDER BY seq'):
            event = json.loads(payload)
            if (not isinstance(event, dict) or event.get('event_id') != event_id or event.get('ts') != ts
                    or type(ts) is not int or ts < 0 or digest(event) != key
                    or event.get('kind') not in ('market', 'clock', 'control') or event_id in events):
                raise RecoveryRequired('EVENT_INTEGRITY_INVALID')
            events[event_id] = event
            first = ts if first is None else min(first, ts); last = ts if last is None else max(last, ts)
            if event['kind'] == 'market':
                markets.append(ts)
                at = event.get('price_at')
                if type(at) is not int or not 0 <= at <= ts:
                    raise RecoveryRequired('OBSERVATION_TIME_INVALID')
                prices.append(at)
                source = event.get('provenance')
                category = ('synthetic' if source == 'SYNTHETIC_TEST_ONLY' else
                            'non_synthetic_claims' if isinstance(source, str) and source and source != 'UNKNOWN' else 'unknown')
                provenance[category] += 1
        raw_times = []
        for received, payload in c.execute('SELECT received_at,payload FROM raw_events'):
            if type(received) is not int or received < 0:
                raise RecoveryRequired('RAW_OBSERVATION_TIME_INVALID')
            raw_times.append(received)
            raw = json.loads(payload)
            source = raw.get('provenance') if isinstance(raw, dict) else None
            category = ('synthetic' if source == 'SYNTHETIC_TEST_ONLY' else
                        'non_synthetic_claims' if isinstance(source, str) and source and source != 'UNKNOWN' else 'unknown')
            provenance[category] += 1
        with localcontext() as ctx:
            ctx.Emax = 999999; ctx.Emin = -999999
            ctx.prec = 512  # Bounded decimal strings + <=10000 additions; no reporting truncation.
            positions = {}; trade_pnl = {}; closed_pnl = decimal('0'); fees = decimal('0'); realized = decimal('0'); cash = _number(cfg['initial_equity_sol'])
            closed = partial = buys = sells = rejects = blocked = 0
            for event_id, payload in c.execute('SELECT event_id,payload FROM outcomes ORDER BY seq'):
                outcome = json.loads(payload)
                if not isinstance(outcome, dict) or event_id not in events:
                    raise RecoveryRequired('OUTCOME_INTEGRITY_INVALID')
                kind = outcome.get('type')
                if kind == 'reject': rejects += 1
                elif kind == 'blocked_exit': blocked += 1
                elif kind == 'fill':
                    event = events[event_id]; mint = outcome['mint']; qty = _number(outcome['quantity'])
                    fee = _number(outcome['fee_sol'])
                    if event['kind'] != 'market' or event.get('mint') != mint or qty <= 0 or fee < 0:
                        raise RecoveryRequired('FILL_IDENTITY_INVALID')
                    fees += fee
                    if outcome['side'] == 'buy':
                        amount = _number(outcome['amount_sol'])
                        if mint in positions or amount <= 0:
                            raise RecoveryRequired('TRADE_INVENTORY_INVALID')
                        positions[mint] = qty; trade_pnl[mint] = decimal('0'); cash -= amount + fee; buys += 1
                    elif outcome['side'] == 'sell':
                        if mint not in positions or qty - positions[mint] > EPS:
                            raise RecoveryRequired('TRADE_INVENTORY_INVALID')
                        proceeds = _number(outcome['proceeds_sol']); pnl = _number(outcome['realized_pnl_sol'])
                        if proceeds < 0:
                            raise RecoveryRequired('FILL_ACCOUNTING_INVALID')
                        cash += proceeds; realized += pnl; trade_pnl[mint] += pnl; sells += 1
                        positions[mint] -= qty
                        if positions[mint] <= EPS:
                            closed += 1; closed_pnl += trade_pnl.pop(mint); del positions[mint]
                        else: partial += 1
                    else: raise RecoveryRequired('FILL_SIDE_INVALID')
                elif kind != 'control':
                    raise RecoveryRequired('OUTCOME_TYPE_INVALID')
            if (set(positions) != set(state['positions'])
                    or any(abs(positions[m] - _number(state['positions'][m]['qty'])) > EPS for m in positions)
                    or any(abs(trade_pnl[m] - _number(state['positions'][m]['trade_pnl'])) > EPS for m in positions)
                    or abs(cash - _number(state['cash'])) > EPS
                    or abs(realized - _number(state['realized_pnl'])) > EPS):
                raise RecoveryRequired('ACCOUNTING_CHECKPOINT_MISMATCH')
            inventory = []
            for mint, p in sorted(state['positions'].items()):
                cost = _number(p['cost_left']); mark = _number(p['mark_value'])
                if min(cost, mark) < 0:
                    raise RecoveryRequired('POSITION_ACCOUNTING_INVALID')
                fresh = (0 <= now - p['mark_at'] <= ttl and p['mark_status'] == 'MODEL_ESTIMATE' and not p['exit_blocked'])
                inventory.append({'mint': mint, 'quantity': p['qty'], 'cost_left_sol': str(cost),
                    'model_mark_sol': str(mark) if fresh else None,
                    'model_unrealized_pnl_sol': str(mark-cost) if fresh else None,
                    'mark_at': p['mark_at'], 'mark_status': p['mark_status'], 'model_mark_current': fresh,
                    'exit_blocked': p['exit_blocked'], 'valuation_verified': False})
            drawdown = _number(state['max_drawdown'])
            if not 0 <= drawdown <= 1:
                raise RecoveryRequired('DRAWDOWN_INVALID')
            return {'kind': 'read_only_experiment_performance_v1', 'status': 'REPORTED',
                'config_hash': identity['config_hash'], 'implementation_hash': identity['implementation_hash'],
                'evaluated_at': now, 'data_provenance_counts': provenance,
                'data_provenance_scope': 'Market events and raw payload declarations; non-synthetic claims are not authenticated live data.',
                'data_provenance': 'SYNTHETIC_ONLY' if markets and provenance['synthetic'] == len(markets)+len(raw_times) else 'LIVE_CLAIMS_OR_UNKNOWN_UNVERIFIED',
                'first_event_at': first, 'last_event_at': last, 'event_span_seconds': last-first,
                'market_observation_count': len(markets),
                'raw_observation_count': len(raw_times), 'first_raw_observation_at': min(raw_times) if raw_times else None,
                'last_raw_observation_at': max(raw_times) if raw_times else None,
                'observation_span_seconds': max(raw_times)-min(raw_times) if raw_times else max(markets)-min(markets) if markets else None,
                'observation_span_basis': 'RAW_RECEIPT_TIMES' if raw_times else 'MARKET_EVENT_TIMES',
                'first_price_observation_at': min(prices) if prices else None, 'last_price_observation_at': max(prices) if prices else None,
                'closed_trade_count': closed, 'partial_sell_fill_count': partial,
                'buy_fill_count': buys, 'sell_fill_count': sells,
                'net_realized_pnl_sol': str(realized), 'closed_trade_realized_pnl_sol': str(closed_pnl),
                'open_trade_realized_pnl_sol': str(realized-closed_pnl), 'recorded_fill_fees_sol': str(fees),
                'fee_accounting': 'Fees already included in recorded cost/proceeds/PnL; do not subtract again. Unrecorded costs unknown.',
                'saved_max_drawdown_fraction': str(drawdown),
                'drawdown_scope': 'Saved engine peak/drawdown uses modeled equity; stale/unverified marks and unrecorded costs limit interpretation.',
                'open_position_count': len(inventory), 'unvalued_or_blocked_position_count': sum(not p['model_mark_current'] for p in inventory),
                'open_inventory': inventory, 'reject_outcome_count': rejects, 'blocked_exit_outcome_count': blocked,
                'profitability_verdict': 'NOT_ASSESSED',
                'notice': 'One saved paper experiment. Partial realized results and unrealized marks are not closed trades or live profitability acceptance; span does not prove continuous coverage.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('db'); parser.add_argument('--now', type=int)
    args = parser.parse_args(argv)
    try:
        result = experiment_report(args.db, now=args.now)
    except (RecoveryRequired, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        print(json.dumps({'status': 'RECOVERY_REQUIRED', 'reason': str(exc) if isinstance(exc, RecoveryRequired) else 'EXPERIMENT_UNAVAILABLE'}))
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
