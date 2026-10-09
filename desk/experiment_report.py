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

from .model import decimal, digest, canonical, PAPER_EXPERIMENTAL, validate_event
from .paper_checkpoint import read_checkpoint, RecoveryRequired

MAX_ROWS = 10000
MAX_BYTES = 32 * 1024 * 1024
EPS = decimal('1e-20')  # Existing engine's inventory-close threshold.


def _number(value):
    if not isinstance(value, str) or len(value) > 512:
        raise RecoveryRequired('REPORT_DECIMAL_INVALID')
    number = decimal(value)
    if abs(number.as_tuple().exponent) > 512 or len(number.as_tuple().digits) > 512:
        raise RecoveryRequired('REPORT_DECIMAL_LIMIT')
    return number


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _quote_fill(outcome, event, cfg):
    """Replay original quote/mint bytes; normalized units alone cannot bind dust."""
    from . import quote_execution as qe
    try:
        record = outcome['quote_execution']
        if outcome['execution_status'] != qe.STATUS or outcome['simulation'] != 'quote_minimum_with_adverse_slippage':
            raise ValueError()
        sources = [qe.SourceRecord(record[prefix + '_source_id'], record[prefix + '_observed_at'],
                                  record[prefix + '_hash'], record['original_' + prefix + '_json'])
                   for prefix in ('quote', 'mint')]
        quote_source, mint_source = sources
        token = qe.ingest_mint(lambda: qe.ProviderObservation(mint_source.source_id,
            mint_source.observed_at, qe._original(mint_source)), mint=outcome['mint'],
            now=event['ts'], max_age_seconds=cfg['price_ttl_seconds'])
        quote = qe.ingest_quote(lambda: qe.ProviderObservation(quote_source.source_id,
            quote_source.observed_at, qe._original(quote_source)), mint=token,
            direction=outcome['side'], amount_raw=record['input_raw'], taker=event['taker'],
            expected_pool=event['pool'], now=event['ts'], max_age_seconds=cfg['price_ttl_seconds'])
        book = qe._book(event, (quote,), cfg) if event['kind'] == 'quote_exit' else qe._Book('', (quote,), token.decimals)
        if qe.canonical(book.record(quote, cfg)) != qe.canonical(record):
            raise ValueError()
        raw = qe.output_raw(quote, cfg) if outcome['side'] == 'buy' else quote.input_raw
        if qe.raw_quantity(outcome['quantity'], token.decimals) != raw:
            raise ValueError()
        if _number(outcome['fee_sol']) != decimal(cfg['fixed_fee_sol']):
            raise ValueError()
        if outcome['side'] == 'buy':
            if _number(outcome['amount_sol']) != quote.input_units: raise ValueError()
        elif _number(outcome['proceeds_sol']) != qe.units(qe.output_raw(quote, cfg), 9) - decimal(cfg['fixed_fee_sol']):
            raise ValueError()
        return raw, token.decimals
    except (ValueError, TypeError, KeyError, OverflowError):
        raise RecoveryRequired('QUOTE_FILL_BINDING_INVALID') from None


def _entry_risk_flags(event, outcome, cfg):
    version = cfg.get('paper_signal_policy_version', cfg.get('experimental_policy_version'))
    if version is None:
        return []
    from .strategy import experimental_scores
    with localcontext() as policy_context:
        policy_context.prec = 28  # Original saved policy representation, independent of accounting precision.
        expected = experimental_scores(event, mode=PAPER_EXPERIMENTAL, policy_version=version)
    if canonical(outcome.get('entry_policy')) != canonical(expected):
        raise RecoveryRequired('REPORT_ENTRY_RISK_BINDING_INVALID')
    return list(expected['risk_flags'])


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
        quote_mode = cfg.get('paper_quote_execution_version') == 1
        ttl = cfg['price_ttl_seconds']
        if type(ttl) is not int or ttl <= 0:
            raise RecoveryRequired('REPORT_CONFIG_INVALID')
        events = {}
        markets = []; exits = []; prices = []; provenance = {'synthetic': 0, 'non_synthetic_claims': 0, 'unknown': 0}
        first = last = None
        for event_id, ts, payload, key in c.execute('SELECT event_id,ts,payload,payload_hash FROM events ORDER BY seq'):
            event = json.loads(payload)
            if (not isinstance(event, dict) or event.get('event_id') != event_id or event.get('ts') != ts
                    or type(ts) is not int or ts < 0 or digest(event) != key
                    or event.get('kind') not in ('market', 'clock', 'control', 'quote_exit') or event_id in events):
                raise RecoveryRequired('EVENT_INTEGRITY_INVALID')
            if event['kind'] == 'quote_exit':
                try:
                    if not quote_mode: raise ValueError()
                    validate_event(event)  # Confirmed strict exit dispatcher, never entry-profile validation.
                except (ValueError, TypeError, KeyError):
                    raise RecoveryRequired('QUOTE_EXIT_EVENT_INVALID') from None
            events[event_id] = event
            first = ts if first is None else min(first, ts); last = ts if last is None else max(last, ts)
            if event['kind'] in ('market', 'quote_exit'):
                (markets if event['kind'] == 'market' else exits).append(ts)
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
            ctx.prec = 400 if quote_mode else 512  # Bounded decimal strings + <=10000 additions; no reporting truncation.
            positions = {}; basis = {}; entry_basis = {}; trade_pnl = {}; closed_pnl = decimal('0'); fees = decimal('0'); realized = decimal('0'); cash = _number(cfg['initial_equity_sol'])
            raw_positions = {}; denominations = {}; held_entries = {}
            quote_fills = []; retained_risks = set(); entry_risks = {}
            closed = partial = buys = sells = rejects = blocked = 0
            for outcome_seq, event_id, payload in c.execute('SELECT seq,event_id,payload FROM outcomes ORDER BY seq'):
                outcome = json.loads(payload)
                if not isinstance(outcome, dict) or event_id not in events:
                    raise RecoveryRequired('OUTCOME_INTEGRITY_INVALID')
                kind = outcome.get('type')
                if kind == 'reject': rejects += 1
                elif kind == 'blocked_exit': blocked += 1
                elif kind == 'fill':
                    event = events[event_id]; mint = outcome['mint']; qty = _number(outcome['quantity'])
                    fee = _number(outcome['fee_sol'])
                    if event['kind'] not in ('market', 'quote_exit') or event.get('mint') != mint or qty <= 0 or fee < 0:
                        raise RecoveryRequired('FILL_IDENTITY_INVALID')
                    if event['kind'] == 'quote_exit':
                        if (not quote_mode or outcome['side'] != 'sell' or mint not in raw_positions
                                or event['current_quantity_raw'] != raw_positions[mint]
                                or event['mint_decimals'] != denominations[mint]
                                or any(event[key] != held_entries[mint][key] for key in ('pool', 'taker', 'provenance'))):
                            raise RecoveryRequired('QUOTE_EXIT_HELD_POSITION_MISMATCH')
                    if quote_mode:
                        raw, denomination = _quote_fill(outcome, event, cfg)
                        if outcome['side'] == 'buy':
                            entry_risks[mint] = _entry_risk_flags(event, outcome, cfg)
                        record = outcome['quote_execution']
                        risks = sorted(set(record['risk_flags']) | set(entry_risks.get(mint, [])))
                        retained_risks.update(risks)
                        quote_fills.append({'outcome_seq': outcome_seq, 'event_id': event_id,
                            'event_hash': digest(event), 'mint': mint, 'side': outcome['side'],
                            **({'exit_source_evidence': event['source_evidence']} if event['kind'] == 'quote_exit' else {}),
                            'execution_status': 'EXECUTION_UNVERIFIED',
                            'source_authenticated': False, 'transaction_verified': False,
                            'actual_fill_verified': False,
                            'assumptions': record['assumptions'], 'risk_flags': risks,
                            'quote_source_id': record['quote_source_id'], 'quote_hash': record['quote_hash'],
                            'quote_observed_at': record['quote_observed_at'],
                            'mint_source_id': record['mint_source_id'], 'mint_hash': record['mint_hash'],
                            'mint_observed_at': record['mint_observed_at']})
                    fees += fee
                    if outcome['side'] == 'buy':
                        amount = _number(outcome['amount_sol'])
                        if mint in positions or amount <= 0:
                            raise RecoveryRequired('TRADE_INVENTORY_INVALID')
                        if quote_mode:
                            raw_positions[mint] = raw; denominations[mint] = denomination
                            held_entries[mint] = {key: event[key] for key in ('pool', 'taker', 'provenance')}
                        positions[mint] = qty; basis[mint] = amount + fee; entry_basis[mint] = basis[mint]
                        trade_pnl[mint] = decimal('0'); cash -= basis[mint]; buys += 1
                    elif outcome['side'] == 'sell':
                        if (mint not in positions or (raw > raw_positions[mint] or denomination != denominations[mint]
                                if quote_mode else qty - positions[mint] > EPS)):
                            raise RecoveryRequired('TRADE_INVENTORY_INVALID')
                        proceeds = _number(outcome['proceeds_sol']); pnl = _number(outcome['realized_pnl_sol'])
                        if proceeds < 0:
                            raise RecoveryRequired('FILL_ACCOUNTING_INVALID')
                        # Reconstruct disposal cost from entry debit and inventory, never declared PnL.
                        disposed = (basis[mint] * decimal(str(raw)) / decimal(str(raw_positions[mint]))
                                    if quote_mode else basis[mint] * qty / positions[mint])
                        derived_pnl = proceeds - disposed
                        if (pnl != derived_pnl if quote_mode else abs(pnl - derived_pnl) > EPS):
                            raise RecoveryRequired('FILL_COST_BASIS_MISMATCH')
                        basis[mint] -= disposed
                        cash += proceeds; realized += derived_pnl; trade_pnl[mint] += derived_pnl; sells += 1
                        positions[mint] -= qty
                        if quote_mode: raw_positions[mint] -= raw
                        if (raw_positions[mint] == 0 if quote_mode else positions[mint] <= EPS):
                            # Engine discards <=EPS quantity on closure. Unreconciled material
                            # residual basis is not a successful closed-trade accounting report.
                            if abs(basis[mint]) > EPS:
                                raise RecoveryRequired('CLOSED_COST_BASIS_UNRECONCILED')
                            if quote_mode: del raw_positions[mint], denominations[mint]
                            closed += 1; closed_pnl += trade_pnl.pop(mint)
                            del positions[mint], basis[mint], entry_basis[mint]
                        else: partial += 1
                    else: raise RecoveryRequired('FILL_SIDE_INVALID')
                elif kind != 'control':
                    raise RecoveryRequired('OUTCOME_TYPE_INVALID')
            if (set(positions) != set(state['positions'])
                    or any((positions[m] != _number(state['positions'][m]['qty']) if quote_mode else abs(positions[m] - _number(state['positions'][m]['qty'])) > EPS) for m in positions)
                    or any((trade_pnl[m] != _number(state['positions'][m]['trade_pnl']) if quote_mode else abs(trade_pnl[m] - _number(state['positions'][m]['trade_pnl'])) > EPS) for m in positions)
                    or any((basis[m] != _number(state['positions'][m]['cost_left']) if quote_mode else abs(basis[m] - _number(state['positions'][m]['cost_left'])) > EPS) for m in positions)
                    or any((entry_basis[m] != _number(state['positions'][m]['initial_cost']) if quote_mode else abs(entry_basis[m] - _number(state['positions'][m]['initial_cost'])) > EPS) for m in positions)
                    or abs(cash - _number(state['cash'])) > EPS
                    or abs(realized - _number(state['realized_pnl'])) > EPS):
                raise RecoveryRequired('ACCOUNTING_CHECKPOINT_MISMATCH')
            if abs(cash + sum(basis.values(), decimal('0')) - _number(cfg['initial_equity_sol']) - realized) > EPS:
                raise RecoveryRequired('COST_CONSERVATION_MISMATCH')
            inventory = []
            for mint, p in sorted(state['positions'].items()):
                cost = basis[mint]; mark = _number(p['mark_value'])
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
                **({'execution': {'status': 'EXECUTION_UNVERIFIED', 'model_version': 1,
                    'source_authenticated': False, 'transaction_verified': False,
                    'actual_fill_verified': False, 'risk_flags': sorted(retained_risks),
                    'fills': quote_fills,
                    'source_reference_scope': 'Original quote/mint hashes and source/time declarations in journal outcomes; content binding only, not authenticated HTTP or chain evidence.',
                    'notice': 'Quote-mode paper fills are simulated estimates. Recorded fee/slippage assumptions do not establish actual execution or paid costs.'}} if quote_mode else {}),
                'config_hash': identity['config_hash'], 'implementation_hash': identity['implementation_hash'],
                'evaluated_at': now, 'data_provenance_counts': provenance,
                'data_provenance_scope': 'Market events and raw payload declarations; non-synthetic claims are not authenticated live data.',
                'data_provenance': 'SYNTHETIC_ONLY' if (markets or exits) and provenance['synthetic'] == len(markets)+len(exits)+len(raw_times) else 'LIVE_CLAIMS_OR_UNKNOWN_UNVERIFIED',
                'first_event_at': first, 'last_event_at': last, 'event_span_seconds': last-first,
                'market_observation_count': len(markets),
                **({'quote_exit_observation_count': len(exits)} if quote_mode else {}),
                'raw_observation_count': len(raw_times), 'first_raw_observation_at': min(raw_times) if raw_times else None,
                'last_raw_observation_at': max(raw_times) if raw_times else None,
                'observation_span_seconds': max(raw_times)-min(raw_times) if raw_times else max(markets+exits)-min(markets+exits) if markets or exits else None,
                'observation_span_basis': 'RAW_RECEIPT_TIMES' if raw_times else 'MARKET_EVENT_TIMES',
                'first_price_observation_at': min(prices) if prices else None, 'last_price_observation_at': max(prices) if prices else None,
                'closed_trade_count': closed, 'partial_sell_fill_count': partial,
                'buy_fill_count': buys, 'sell_fill_count': sells,
                'net_realized_pnl_sol': str(realized), 'closed_trade_realized_pnl_sol': str(closed_pnl),
                'open_trade_realized_pnl_sol': str(realized-closed_pnl), 'recorded_fill_fees_sol': str(fees),
                'fee_accounting': 'Buy fees included once in reconstructed entry cost; sell proceeds recorded net. Sell fee inclusion in net proceeds is a journal declaration, not independently verified. Do not subtract fees again; unrecorded costs unknown.',
                'accounting_scope': 'Cost basis and realized PnL reconstructed from recorded buy debits, buy fees, sell quantities and net proceeds; journal economic truth and fees are not authenticated. Legacy reconciliation tolerance 1e-20; quoted inventory closes only at zero raw units and exact quote basis/PnL reconciliation is required.',
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
