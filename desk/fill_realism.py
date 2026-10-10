"""Opt-in, versioned latency measurement of simulated paper fills (EXECUTION_UNVERIFIED).

With ``paper_fill_realism_version: 1`` (absent = off, behaviour unchanged), every
simulated BUY/SELL fill produced by a cycle is followed by bounded re-quotes of
the SAME route/input at +2s, +5s and +10s after the decision. Each attempt goes
through the cycle's existing charged budget classes (investigation admission for
entries, the shared monitoring allowance for held exits); a failed attempt is
charged like any other. Results are appended to ``paper_fill_realism_samples``
inside the ledger file, bound to the fill's event id, and are trigger-guarded
against UPDATE/DELETE. The recorded paper fill, checkpoint and accounting are
never touched: this module only reads outcomes and appends measurements.

Drift convention (all signed basis points, Decimal, stored as strings):
  out_drift_bps       = (requote simulated out / decision simulated out - 1) * 1e4
                        (negative = fewer output units than the paper fill got)
  adverse_slippage_bps = -out_drift_bps (positive = the delayed fill is worse)
  price_drift_bps     = (requote SOL-per-token / decision SOL-per-token - 1) * 1e4
This is a measurement of quote drift, not a verified fill or a latency guarantee.
"""
from decimal import Decimal, localcontext
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from . import quote_execution as qe
from .live_observation import ProviderObservation, ingest_quote, ObservationError
from .model import canonical, digest
from .providers import SOL

KEY = 'paper_fill_realism_version'
VERSION = 1
DELAYS = (2, 5, 10)
LATE_SECONDS = 3          # a sample later than delay+3s is skipped, not requested
MAX_WAIT_SLACK = 1        # refuse to sleep longer than delay+1s (clock defects)
MONITORING_RESERVE = 16  # keep two held mark+exit passes (8 reads each) affordable; works for 60 or 3600 caps
INVESTIGATION_RESERVE = 8  # a held mark + exit pass (4 + 4 reads) must stay affordable on the 18-read admission
MAX_JSON_BYTES = 256 * 1024
STATUS = qe.STATUS
TABLE = 'paper_fill_realism_samples'
CODES_STOP = ('SHARED_BUDGET_CHARGE_OR_IDENTITY_MISMATCH', 'MONITORING_CHARGE_OR_ADMISSION_MISMATCH',
              'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED', 'CYCLE_REQUEST_BUDGET_EXHAUSTED',
              'MONITORING_REQUEST_BUDGET_EXHAUSTED', 'PERSISTED_ADMISSION_REQUIRED',
              'REALISM_MONITORING_RESERVE', 'REALISM_INVESTIGATION_RESERVE', 'REALISM_SOURCE_UNAVAILABLE',
              'MONITORING_OPEN_POSITION_REQUIRED')

SCHEMA = f'''
CREATE TABLE IF NOT EXISTS {TABLE}(
  fill_event_id TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('buy','sell')),
  delay_seconds INTEGER NOT NULL CHECK(delay_seconds IN (2,5,10)),
  mint TEXT NOT NULL, decision_at INTEGER NOT NULL, config_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('MEASURED','FAILED')), code TEXT,
  charged INTEGER NOT NULL CHECK(charged IN (0,1)), measured_at INTEGER, elapsed_seconds TEXT,
  decision_input_raw INTEGER, decision_estimated_out_raw INTEGER, decision_simulated_out_raw INTEGER,
  decision_quote_hash TEXT, requote_estimated_out_raw INTEGER, requote_min_out_raw INTEGER,
  requote_simulated_out_raw INTEGER, requote_hash TEXT, requote_json TEXT,
  decision_price_sol TEXT, requote_price_sol TEXT, price_drift_bps TEXT, out_drift_bps TEXT,
  adverse_slippage_bps TEXT, execution_status TEXT NOT NULL CHECK(execution_status='{STATUS}'),
  PRIMARY KEY(fill_event_id, side, delay_seconds),
  CHECK((status='MEASURED' AND code IS NULL AND requote_json IS NOT NULL AND out_drift_bps IS NOT NULL)
     OR (status='FAILED' AND code IS NOT NULL)));
CREATE TRIGGER IF NOT EXISTS {TABLE}_bound BEFORE INSERT ON {TABLE}
  WHEN NOT EXISTS(SELECT 1 FROM events WHERE event_id=NEW.fill_event_id AND ts=NEW.decision_at)
  BEGIN SELECT RAISE(ABORT,'fill realism sample must bind to a ledger event'); END;
CREATE TRIGGER IF NOT EXISTS {TABLE}_no_update BEFORE UPDATE ON {TABLE}
  BEGIN SELECT RAISE(ABORT,'fill realism samples are append-only'); END;
CREATE TRIGGER IF NOT EXISTS {TABLE}_no_delete BEFORE DELETE ON {TABLE}
  BEGIN SELECT RAISE(ABORT,'fill realism samples are append-only'); END;
'''


def selected(cfg):
    """0 = off (key absent). 1 requires the quote-execution paper profile."""
    value = cfg.get(KEY)
    if value is None:
        return 0
    if type(value) is not int or value != VERSION or cfg.get('paper_quote_execution_version') != qe.VERSION:
        raise qe.QuoteExecutionError('FILL_REALISM_CONFIG_INVALID')
    return VERSION


def _sleep(seconds):
    time.sleep(seconds)


def _dec(value):
    return Decimal(value)


def _price(side, in_raw, out_raw, decimals):
    with localcontext() as ctx:
        ctx.prec = 60
        if side == 'buy':
            return (qe.units(in_raw, 9)) / qe.units(out_raw, decimals)
        return qe.units(out_raw, 9) / qe.units(in_raw, decimals)


def drift(side, decision, requote_estimated, requote_simulated, decimals):
    """Pure drift math. ``decision`` = (input_raw, estimated_out_raw, simulated_out_raw)."""
    in_raw, dec_est, dec_sim = decision
    if not all(type(v) is int and v > 0 for v in (in_raw, dec_est, dec_sim, requote_estimated, requote_simulated)):
        raise qe.QuoteExecutionError('REALISM_AMOUNT_INVALID')
    with localcontext() as ctx:
        ctx.prec = 60
        p0, p1 = _price(side, in_raw, dec_est, decimals), _price(side, in_raw, requote_estimated, decimals)
        out = (Decimal(requote_simulated) / Decimal(dec_sim) - 1) * 10000
        return {'decision_price_sol': format(p0, 'f'), 'requote_price_sol': format(p1, 'f'),
                'price_drift_bps': format((p1 / p0 - 1) * 10000, 'f'),
                'out_drift_bps': format(out, 'f'), 'adverse_slippage_bps': format(-out, 'f')}


def ensure_schema(db):
    db.executescript(SCHEMA)


def collect(outcomes, event, collected, source, held, plain_source=None):
    """Detached job specs for fills of one delivered event (nothing is read or charged here)."""
    jobs = []
    for outcome in outcomes:
        if outcome.get('type') != 'fill' or outcome.get('side') not in ('buy', 'sell'):
            continue
        record = outcome.get('quote_execution')
        if type(record) is not dict or record.get('direction') != outcome['side']:
            continue
        jobs.append({'event_id': event['event_id'], 'decision_at': event['ts'], 'side': outcome['side'],
                     'mint': outcome['mint'], 'record': dict(record), 'target': collected.target,
                     'typed_mint': collected.mint, 'source': source, 'held': bool(held), 'plain_source': plain_source})
    return jobs


def _monitoring_provisioned(progress):
    try:
        with progress.store.connect() as c:
            return c.execute('SELECT 1 FROM paper_monitoring_budget LIMIT 1').fetchone() is not None
    except sqlite3.Error:
        return False


def _position_open(path, mint):
    """Conservative: any doubt means the position is still open and needs its exit budget."""
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as c:
            row = c.execute('SELECT payload FROM state WHERE id=1').fetchone()
        return mint in json.loads(row[0])['positions']
    except (sqlite3.Error, TypeError, KeyError, ValueError, OSError):
        return True


def _insert(path, row):
    row = {**row, 'execution_status': STATUS}
    names = ','.join(row)
    with closing(sqlite3.connect(path, timeout=20, isolation_level=None)) as c:
        ensure_schema(c)
        try:
            c.execute(f'INSERT INTO {TABLE}({names}) VALUES({",".join("?" * len(row))})', tuple(row.values()))
        except sqlite3.IntegrityError as error:
            if str(error).startswith('UNIQUE constraint failed'):
                return False  # an identity already has its one immutable sample
            raise  # unbound/invalid rows must surface, never be silently dropped
    return True


def measure(jobs, cfg, ledger_path, budget, allowance, *, wall_clock=None):
    """Take the +2/+5/+10s samples. Never raises into the paper cycle; returns a summary."""
    from . import paper_cycle as cycle
    from .monitoring_budget import MonitoringBlocked
    if selected(cfg) != VERSION:
        return []
    clock = wall_clock or budget.wall_clock
    config_hash = digest(cfg)
    summary = []
    for job in jobs:
        record, side, stop = job['record'], job['side'], None
        # The monitoring allowance only funds quotes for an OPEN position; once a fill has closed it, the
        # re-quotes are charged to the scan's investigation admission through a plain (unbound) source.
        open_now = _position_open(ledger_path, job['mint'])
        use_monitoring = job['held'] and open_now
        # Investigation reads are shared with later held exits on the same 18-read admission unless the
        # dedicated monitoring allowance is provisioned: never let measurement starve an exit.
        reserve = INVESTIGATION_RESERVE if (not use_monitoring and open_now
                                            and not _monitoring_provisioned(budget.progress)) else 0
        job = {**job, 'use_monitoring': use_monitoring}
        if job['held'] and not use_monitoring:
            try:
                job['source'] = job['plain_source']()
            except Exception:   # a source that cannot even be built must not reach the cycle
                job['source'], stop = None, 'REALISM_SOURCE_UNAVAILABLE'
        decision = (record['input_raw'], record['estimated_output_raw'], record['simulated_output_raw'])
        for delay in DELAYS:
            base = {'fill_event_id': job['event_id'], 'side': side, 'delay_seconds': delay, 'mint': job['mint'],
                    'decision_at': job['decision_at'], 'config_hash': config_hash,
                    'decision_input_raw': decision[0], 'decision_estimated_out_raw': decision[1],
                    'decision_simulated_out_raw': decision[2], 'decision_quote_hash': record['quote_hash']}
            charged_before = budget.attempted + budget.monitoring_attempted
            row = None
            try:
                if stop is not None:
                    row = {**base, 'status': 'FAILED', 'code': stop, 'charged': 0}
                else:
                    wait = job['decision_at'] + delay - clock()
                    if wait > delay + MAX_WAIT_SLACK:
                        row = {**base, 'status': 'FAILED', 'code': 'REALISM_CLOCK_UNAVAILABLE', 'charged': 0}
                    else:
                        if wait > 0:
                            _sleep(wait)
                        elapsed = clock() - job['decision_at']
                        if elapsed > delay + LATE_SECONDS:
                            row = {**base, 'status': 'FAILED', 'code': 'REALISM_SAMPLE_LATE', 'charged': 0,
                                   'elapsed_seconds': format(Decimal(str(elapsed)), 'f')}
                        elif use_monitoring and allowance is not None and allowance.snapshot()['remaining'] - 1 < MONITORING_RESERVE:
                            row = {**base, 'status': 'FAILED', 'code': 'REALISM_MONITORING_RESERVE', 'charged': 0}
                        elif not use_monitoring and reserve and (lambda a: a is None or a['request_ceiling'] - a['requests_used'] - 1 < reserve)(
                                budget.progress.admission(job['target'].scan_id)):
                            row = {**base, 'status': 'FAILED', 'code': 'REALISM_INVESTIGATION_RESERVE', 'charged': 0}
                        else:
                            row = _sample(cycle, job, cfg, budget, allowance, clock, base, delay, decision, elapsed)
            except (cycle.CycleBlocked, MonitoringBlocked) as error:
                row = {**base, 'status': 'FAILED', 'code': error.code}
            except (ObservationError, qe.QuoteExecutionError, ValueError, TypeError, KeyError, ArithmeticError):
                row = {**base, 'status': 'FAILED', 'code': 'REQUOTE_INVALID'}
            row.setdefault('charged', 1 if budget.attempted + budget.monitoring_attempted > charged_before else 0)
            if row['status'] == 'FAILED' and row['code'] in CODES_STOP:
                stop = row['code']
            try:
                _insert(ledger_path, row)
            except sqlite3.Error:
                row = {**row, 'status': 'FAILED', 'code': 'REALISM_STORE_UNAVAILABLE'}
                stop = 'REALISM_STORE_UNAVAILABLE'
            summary.append({k: row.get(k) for k in ('fill_event_id', 'side', 'delay_seconds', 'status', 'code',
                                                    'charged', 'out_drift_bps', 'adverse_slippage_bps')})
    return summary


def _sample(cycle, job, cfg, budget, allowance, clock, base, delay, decision, elapsed):
    fresh = cycle._Budget(budget.progress, budget.wall_clock, budget.monotonic)
    fresh.attempted, fresh.monitoring_attempted = budget.attempted, budget.monitoring_attempted
    spender = cycle._HeldBudget(fresh, allowance) if job['use_monitoring'] else fresh
    target, source, side = job['target'], job['source'], job['side']
    input_mint, output_mint = (SOL, target.mint) if side == 'buy' else (target.mint, SOL)
    try:
        payload = spender.call(target.scan_id, lambda timeout: source.quote(
            input_mint, output_mint, decision[0], target.taker, timeout_seconds=timeout))
    finally:
        budget.attempted, budget.monitoring_attempted = fresh.attempted, fresh.monitoring_attempted
    # Measurement-only: the typed mint (decimals/identity) is the decision-time one, so allow its age
    # to span the delay. The re-quote itself must still be fresh under the configured TTL.
    ttl = cfg['price_ttl_seconds']
    now = spender.now()
    quote = ingest_quote(lambda: ProviderObservation(source.quote_source_id, payload['observed_at'], payload),
                         mint=job['typed_mint'], direction=side, amount_raw=decision[0], taker=target.taker,
                         expected_pool=target.pool, now=now, max_age_seconds=ttl + delay + LATE_SECONDS + 15)
    if len(quote.source.original_json.encode()) > MAX_JSON_BYTES:
        raise qe.QuoteExecutionError('REALISM_JSON_BOUND')
    simulated = qe.output_raw(quote, cfg)
    values = drift(side, decision, quote.estimated_output_raw, simulated, job['record']['mint_decimals'])
    return {**base, 'status': 'MEASURED', 'code': None, 'charged': 1, 'measured_at': now,
            'elapsed_seconds': format(Decimal(str(elapsed)), 'f'),
            'requote_estimated_out_raw': quote.estimated_output_raw, 'requote_min_out_raw': quote.minimum_output_raw,
            'requote_simulated_out_raw': simulated, 'requote_hash': quote.source.raw_hash,
            'requote_json': quote.source.original_json, **values}


# ---- read-only reporting math (shared with tools/research/fill_realism_report.py) ----

def latency_adjusted_pnl(buy, sells, buy_sample, sell_samples):
    """Re-compute one round trip as if every leg filled at the delayed re-quote.

    buy: dict(amount_sol, fee_sol); sells: [dict(proceeds_sol, fee_sol)] aligned with sell_samples.
    *_sample: dict(decision_simulated_out_raw, requote_simulated_out_raw). Returns Decimal PnL (SOL).
    Tokens received scale by rb; each leg's gross proceeds (net proceeds + fee) scale by rs*rb;
    fixed fees are unchanged. Missing/failed samples must be filtered by the caller (never imputed).
    """
    with localcontext() as ctx:
        ctx.prec = 60
        rb = _dec(buy_sample['requote_simulated_out_raw']) / _dec(buy_sample['decision_simulated_out_raw'])
        total = -(_dec(buy['amount_sol']) + _dec(buy['fee_sol']))
        for sell, sample in zip(sells, sell_samples):
            rs = _dec(sample['requote_simulated_out_raw']) / _dec(sample['decision_simulated_out_raw'])
            fee = _dec(sell['fee_sol'])
            total += (_dec(sell['proceeds_sol']) + fee) * rs * rb - fee
        return total
