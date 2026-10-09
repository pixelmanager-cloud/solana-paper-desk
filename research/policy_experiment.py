"""Offline proposal contract; saved-policy accounting is never a counterfactual.

python -m research.policy_experiment --training DB --holdout DB --trials JSON --now EPOCH
No provider transport, ledger writer, policy application or optimizer is imported.
"""
import argparse
import json
import sqlite3
import tempfile
from contextlib import closing
from decimal import Decimal, localcontext
from pathlib import Path

from desk.experiment_report import experiment_report
from desk.model import digest
from desk.paper_checkpoint import RecoveryRequired

TUNABLE = ('min_age_seconds', 'max_age_seconds', 'min_liquidity_usd')
MAX_DATASETS = 16
MAX_TRIALS = 32
MIN_CLOSED = {'training': 20, 'holdout': 10}


class InvalidExperiment(ValueError):
    pass


def _snapshot(path, now):
    """Validate one immutable SQLite backup, avoiding cross-read source races."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
        raise InvalidExperiment('SAVED_EXPERIMENT_MISSING_OR_OVERSIZED')
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / 'snapshot.sqlite'
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(snapshot)) as dest:
                page_size = source.execute('PRAGMA page_size').fetchone()[0]
                def bounded_backup(status, remaining, total):
                    if total * page_size > 128 * 1024 * 1024:
                        raise InvalidExperiment('SAVED_EXPERIMENT_MISSING_OR_OVERSIZED')
                source.backup(dest, pages=256, progress=bounded_backup)
        report = experiment_report(snapshot, now=now)
        with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True)) as c:
            cfg = json.loads(c.execute("SELECT value FROM metadata WHERE key='config'").fetchone()[0])
            samples = list(c.execute('SELECT event_id,payload_hash FROM events ORDER BY seq'))
            events = [json.loads(row[0]) for row in c.execute('SELECT payload FROM events')]
            raw = [digest(json.loads(row[0])) for row in c.execute('SELECT payload FROM raw_events')]
        if cfg.get('mode') != 'paper' or digest(cfg) != report['config_hash']:
            raise InvalidExperiment('SAVED_CONFIG_INVALID')
        times = [report[key] for key in ('first_event_at', 'last_event_at',
            'first_raw_observation_at', 'last_raw_observation_at',
            'first_price_observation_at', 'last_price_observation_at') if report[key] is not None]
        for event in events:
            times.extend(event[key] for key in ('holder_at', 'flow_at', 'momentum_at')
                         if event.get(key) is not None)
            window = event.get('paper_signal_profile', {}).get('window', {})
            if window:
                times.extend(window[key] for key in ('start_inclusive', 'end_inclusive'))
        for fill in report.get('execution', {}).get('fills', []):
            times.extend(fill[key] for key in ('quote_observed_at', 'mint_observed_at'))
        if not times or any(type(t) is not int or not 0 <= t <= now for t in times):
            raise InvalidExperiment('SAMPLE_TIME_INVALID')
        identities = {('event_id', x[0]) for x in samples} | {('event_hash', x[1]) for x in samples}
        identities |= {('raw_hash', x) for x in raw}
        # Neither a path nor unvalidated JSON report is the evidence authority.
        return cfg, report, identities, min(times), max(times)


def _proposals(cfg, trials):
    if type(trials) is not list or not 1 <= len(trials) <= MAX_TRIALS:
        raise InvalidExperiment('TRIAL_LIMIT_OR_SHAPE_INVALID')
    result = []; seen = set()
    for overrides in trials:
        if type(overrides) is not dict or not overrides or not set(overrides) <= set(TUNABLE):
            raise InvalidExperiment('FROZEN_OR_UNSUPPORTED_POLICY_FIELD')
        candidate = dict(cfg)
        for key, value in overrides.items():
            if key not in cfg:
                raise InvalidExperiment('UNSUPPORTED_CONFIG_FIELD')
            if key.endswith('_seconds'):
                if type(value) is not int or not 0 < value < 2**31:
                    raise InvalidExperiment('AGE_THRESHOLD_INVALID')
            elif (type(value) is not str or len(value) > 64):
                raise InvalidExperiment('LIQUIDITY_THRESHOLD_INVALID')
            else:
                try:
                    number = Decimal(value)
                    if not number.is_finite() or not 0 < number <= Decimal('1e15'):
                        raise ValueError()
                except (ValueError, ArithmeticError):
                    raise InvalidExperiment('LIQUIDITY_THRESHOLD_INVALID') from None
            candidate[key] = value
        if candidate['min_age_seconds'] > candidate['max_age_seconds']:
            raise InvalidExperiment('AGE_RANGE_INVALID')
        key = digest(candidate)
        if key in seen or key == digest(cfg):
            raise InvalidExperiment('DUPLICATE_OR_UNCHANGED_TRIAL')
        seen.add(key)
        result.append({'kind': 'entry_threshold_proposal_v1', 'trial': len(result) + 1,
            'base_config_hash': digest(cfg), 'candidate_config_hash': key,
            'overrides': dict(overrides), 'counterfactual_net_pnl_sol': None,
            'comparison_status': 'UNSUPPORTED_COUNTERFACTUAL', 'entry_authorized': False,
            'promotion_authorized': False})
    return result


def evaluate(training, holdout, trials, *, now):
    if type(now) is not int or not 0 <= now < 2**63:
        raise InvalidExperiment('OBSERVATION_TIME_INVALID')
    if (type(training) is not list or type(holdout) is not list or not training
            or len(training) + len(holdout) > MAX_DATASETS):
        raise InvalidExperiment('TRAINING_AND_HOLDOUT_REQUIRED_OR_LIMIT')
    seen = set(); cfg = None; implementation = None; groups = {}
    for name, paths in (('training', training), ('holdout', holdout)):
        rows = []
        for path in paths:
            current, report, identities, start, end = _snapshot(path, now)
            if cfg is None:
                cfg, implementation = current, report['implementation_hash']
            if current != cfg or report['implementation_hash'] != implementation:
                raise InvalidExperiment('MIXED_POLICY_OR_IMPLEMENTATION')
            if identities & seen:
                raise InvalidExperiment('DUPLICATE_OR_LEAKED_SAMPLE')
            seen.update(identities)
            if rows and start <= rows[-1][1]:
                raise InvalidExperiment('OVERLAPPING_OR_UNORDERED_SAMPLE')
            rows.append((start, end, report))
        groups[name] = rows
    if groups['holdout'] and groups['holdout'][0][0] <= groups['training'][-1][1]:
        raise InvalidExperiment('HOLDOUT_NOT_STRICTLY_LATER')
    proposals = _proposals(cfg, trials)
    partitions = {}; blockers = ['COUNTERFACTUAL_OUTCOMES_AND_PORTFOLIO_PATH_UNAVAILABLE']
    with localcontext() as ctx:
        ctx.prec = 512
        for name, rows in groups.items():
            reports = [r[2] for r in rows]
            closed = sum(r['closed_trade_count'] for r in reports)
            if closed < MIN_CLOSED[name]: blockers.append(name.upper() + '_CLOSED_TRADE_MINIMUM_NOT_MET')
            if any(r['open_position_count'] for r in reports): blockers.append(name.upper() + '_OPEN_LIFECYCLE')
            partitions[name] = {'sample_count': len(rows), 'first_at': rows[0][0] if rows else None, 'last_at': rows[-1][1] if rows else None,
                'verified_report_hashes': [digest(r) for r in reports],
                'closed_trade_count': closed,
                'observed_closed_net_pnl_sol': str(sum((Decimal(r['closed_trade_realized_pnl_sol']) for r in reports), Decimal(0))),
                'observed_open_trade_realized_pnl_sol': [r['open_trade_realized_pnl_sol'] for r in reports],
                'marks': [r['open_inventory'] for r in reports],
                'recorded_fees_sol': str(sum((Decimal(r['recorded_fill_fees_sol']) for r in reports), Decimal(0))),
                'saved_drawdown_fractions': [r['saved_max_drawdown_fraction'] for r in reports],
                'data_provenance': [r['data_provenance'] for r in reports],
                'risk_flags': sorted({flag for r in reports for flag in r.get('execution', {}).get('risk_flags', [])}),
                'execution_assumptions': [fill['assumptions'] for r in reports for fill in r.get('execution', {}).get('fills', [])]}
    insufficient = any(x != blockers[0] for x in blockers)
    return {'kind': 'offline_entry_policy_experiment_v1',
        'status': 'INSUFFICIENT_DATA' if insufficient else 'BLOCKED_COUNTERFACTUAL',
        'base_config_hash': digest(cfg), 'implementation_hash': implementation,
        'proposal_trials_count': len(proposals), 'optimization_trials_performed': 0,
        'trial_scope': 'This supplied batch only; external prior searches are unknown.',
        'minimum_closed_trades': dict(MIN_CLOSED),
        'minimum_scope': 'Research screening floors only, not statistical sufficiency or profitability proof.',
        'chronology_scope': 'Strict non-overlap of retained event, raw receipt, price/feature/quote times and explicit signal-window bounds; hidden source lookback and population independence are not proven.', 'partitions': partitions, 'proposals': proposals,
        'tunable_fields': list(TUNABLE), 'frozen_fields': sorted(set(cfg) - set(TUNABLE)),
        'execution_status': 'EXECUTION_UNVERIFIED', 'source_authenticated': False,
        'entry_authorized': False, 'promotion_authorized': False, 'profitability_verdict': 'NOT_ASSESSED',
        'blockers': blockers, 'fee_slippage_policy': {key: cfg[key] for key in ('fixed_fee_sol', 'adverse_slippage_bps')},
        'notice': 'Observed saved-policy net accounting includes recorded fees once. Slippage is a model assumption; unrecorded costs remain unknown. Marks are not closed trades. Drawdowns are saved modeled values, not a combined portfolio curve. No rejected/unobserved return, threshold ranking, profitability guarantee or automatic promotion.'}


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidExperiment('DUPLICATE_TRIAL_JSON_KEY')
        result[key] = value
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--training', action='append', required=True)
    p.add_argument('--holdout', action='append', default=[])
    p.add_argument('--trials', required=True); p.add_argument('--now', type=int, required=True)
    a = p.parse_args(argv)
    try:
        path = Path(a.trials)
        if path.stat().st_size > 16384: raise InvalidExperiment('TRIAL_INPUT_LIMIT')
        result = evaluate(a.training, a.holdout, json.loads(path.read_text(), object_pairs_hook=_unique_keys), now=a.now)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        reason = str(error) if isinstance(error, (InvalidExperiment, RecoveryRequired)) else 'SAVED_EXPERIMENT_INVALID'
        print(json.dumps({'kind': 'offline_entry_policy_experiment_v1', 'status': 'INVALID_INPUT',
            'reason': reason, 'entry_authorized': False, 'promotion_authorized': False}))
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False)); return 0


if __name__ == '__main__':
    raise SystemExit(main())
