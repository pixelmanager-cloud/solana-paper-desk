"""Opt-in, versioned latency measurement of simulated paper fills (EXECUTION_UNVERIFIED).

With ``paper_fill_realism_version: 1`` (absent = off, behaviour byte-identical) the
trading pass only ENQUEUES one append-only job per simulated BUY/SELL fill, after
its pass outcome is durable. It never sleeps, requotes, charges a budget or can
raise. A separate worker (``tools/ops/fill_realism_worker.py``) re-quotes the
SAME route/input at +2s, +5s and +10s after the decision, with its own request
allowance and the shared provider pacing, and appends the samples.

The measurement store is its own SQLite file next to the ledger
(``<ledger>.fill-realism.sqlite``): the ledger, its checkpoint, the research and
evidence stores and every NULL/outcome pass state are never touched by the worker.

Drift convention (signed basis points, Decimal, stored as strings):
  out_drift_bps       = (requote simulated out / decision simulated out - 1) * 1e4
                        (negative = fewer output units than the paper fill got)
  adverse_slippage_bps = -out_drift_bps (positive = the delayed fill is worse)
  price_drift_bps     = (requote SOL-per-token / decision SOL-per-token - 1) * 1e4
This is a measurement of quote drift, not a verified fill or a latency guarantee.
"""
from contextlib import closing
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sqlite3
import time

from . import quote_execution as qe

KEY = 'paper_fill_realism_version'
VERSION = 1
DELAYS = (2, 5, 10)
STATUS = qe.STATUS
STORE_SUFFIX = '.fill-realism.sqlite'
MAX_JOBS = 100_000
MAX_RECORD_BYTES = 8192

JOB_TABLE = 'fill_realism_jobs'
ATTEMPT_TABLE = 'fill_realism_attempts'
SAMPLE_TABLE = 'fill_realism_samples'
POLICY_TABLE = 'fill_realism_policy'
TABLE = SAMPLE_TABLE
SAMPLE_STATUSES = ('MEASURED', 'FAILED', 'LATE', 'STALE')


def _guards(table):
    return (f"CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT,'fill realism records are append-only'); END;\n"
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT,'fill realism records are append-only'); END;\n")


SCHEMA = f'''
CREATE TABLE IF NOT EXISTS {JOB_TABLE}(
  fill_event_id TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('buy','sell')),
  mint TEXT NOT NULL, scan_id TEXT NOT NULL, pool TEXT NOT NULL, taker TEXT NOT NULL,
  decision_ts INTEGER NOT NULL, decision_at REAL NOT NULL, config_hash TEXT NOT NULL,
  held INTEGER NOT NULL CHECK(held IN (0,1)), mint_decimals INTEGER NOT NULL,
  mint_observed_at INTEGER NOT NULL, record_json TEXT NOT NULL CHECK(length(record_json)<={MAX_RECORD_BYTES}),
  enqueued_at REAL NOT NULL, execution_status TEXT NOT NULL CHECK(execution_status='{STATUS}'),
  PRIMARY KEY(fill_event_id, side));
CREATE TABLE IF NOT EXISTS {ATTEMPT_TABLE}(
  fill_event_id TEXT NOT NULL, side TEXT NOT NULL, delay_seconds INTEGER NOT NULL CHECK(delay_seconds IN (2,5,10)),
  started_at REAL NOT NULL, PRIMARY KEY(fill_event_id, side, delay_seconds),
  FOREIGN KEY(fill_event_id, side) REFERENCES {JOB_TABLE}(fill_event_id, side));
CREATE TABLE IF NOT EXISTS {SAMPLE_TABLE}(
  fill_event_id TEXT NOT NULL, side TEXT NOT NULL, delay_seconds INTEGER NOT NULL CHECK(delay_seconds IN (2,5,10)),
  status TEXT NOT NULL CHECK(status IN ('MEASURED','FAILED','LATE','STALE')), code TEXT,
  due_at REAL NOT NULL, started_at REAL, completed_at REAL, lag_seconds TEXT,
  charged INTEGER NOT NULL CHECK(charged IN (0,1)),
  requote_estimated_out_raw INTEGER, requote_min_out_raw INTEGER, requote_simulated_out_raw INTEGER,
  requote_hash TEXT, requote_json TEXT, requote_observed_at INTEGER,
  decision_price_sol TEXT, requote_price_sol TEXT, price_drift_bps TEXT, out_drift_bps TEXT, adverse_slippage_bps TEXT,
  execution_status TEXT NOT NULL CHECK(execution_status='{STATUS}'),
  PRIMARY KEY(fill_event_id, side, delay_seconds),
  FOREIGN KEY(fill_event_id, side) REFERENCES {JOB_TABLE}(fill_event_id, side),
  CHECK((status='MEASURED' AND code IS NULL AND requote_json IS NOT NULL AND out_drift_bps IS NOT NULL)
     OR (status<>'MEASURED' AND code IS NOT NULL AND out_drift_bps IS NULL)));
CREATE TABLE IF NOT EXISTS {POLICY_TABLE}(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, allowance_per_hour INTEGER NOT NULL CHECK(allowance_per_hour BETWEEN 1 AND 3600));
''' + ''.join(_guards(t) for t in (JOB_TABLE, ATTEMPT_TABLE, SAMPLE_TABLE, POLICY_TABLE))


def selected(cfg):
    """0 = off (key absent). 1 requires the quote-execution paper profile."""
    value = cfg.get(KEY)
    if value is None:
        return 0
    if type(value) is not int or value != VERSION or cfg.get('paper_quote_execution_version') != qe.VERSION:
        raise qe.QuoteExecutionError('FILL_REALISM_CONFIG_INVALID')
    return VERSION


def store_path(ledger_path):
    return Path(str(ledger_path) + STORE_SUFFIX)


def connect(path, *, readonly=False):
    if readonly:
        return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    c = sqlite3.connect(path, timeout=5, isolation_level=None)
    c.execute('PRAGMA foreign_keys=ON')
    return c


def ensure_schema(c, *, allowance_per_hour=None, now=None):
    c.executescript(SCHEMA)
    if allowance_per_hour is not None and c.execute(f'SELECT 1 FROM {POLICY_TABLE}').fetchone() is None:
        c.execute(f'INSERT INTO {POLICY_TABLE}(at,allowance_per_hour) VALUES(?,?)',
                  (time.time() if now is None else now, allowance_per_hour))


# ----------------------------------------------------------------- math
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


# --------------------------------------------- enqueue (the ONLY pass-side code)
def capture(cfg, outcomes, event, collected, held, wall_clock=time.time):
    """The whole in-pass hook: flag check + job specs. NEVER raises (a defect here must not latch the store).

    A malformed realism key is refused up front by ``qe.config``; should one still reach this point the
    measurement is simply off for the pass."""
    try:
        if selected(cfg) != VERSION:
            return []
        return collect(outcomes, event, collected, held, wall_clock)
    except BaseException as error:  # noqa: BLE001
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return []


def collect(outcomes, event, collected, held, wall_clock=time.time):
    """Detached job specs for the fills of one delivered event. Pure, bounded, NEVER raises."""
    try:
        jobs = []
        decision_at = float(wall_clock())
        for outcome in outcomes:
            if outcome.get('type') != 'fill' or outcome.get('side') not in ('buy', 'sell'):
                continue
            record = outcome.get('quote_execution')
            if type(record) is not dict or record.get('direction') != outcome['side']:
                continue
            text = json.dumps(record, sort_keys=True, separators=(',', ':'))
            if len(text) > MAX_RECORD_BYTES:
                continue
            target = collected.target
            jobs.append({'fill_event_id': event['event_id'], 'side': outcome['side'], 'mint': outcome['mint'],
                         'scan_id': target.scan_id, 'pool': target.pool, 'taker': target.taker,
                         'decision_ts': int(event['ts']), 'decision_at': decision_at, 'held': 1 if held else 0,
                         'mint_decimals': int(record['mint_decimals']), 'mint_observed_at': int(collected.mint.source.observed_at),
                         'record_json': text})
        return jobs
    except BaseException as error:  # noqa: BLE001 - a measurement defect must never reach the pass
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return []


def enqueue(ledger_path, jobs, cfg_hash, *, now=None):
    """Append job rows to the separate measurement store. Swallows EVERY error; returns rows added."""
    if not jobs:
        return 0
    try:
        added = 0
        with closing(connect(store_path(ledger_path))) as c:
            ensure_schema(c)
            for job in jobs:
                try:
                    c.execute(f'INSERT INTO {JOB_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                        job['fill_event_id'], job['side'], job['mint'], job['scan_id'], job['pool'], job['taker'],
                        job['decision_ts'], job['decision_at'], cfg_hash, job['held'], job['mint_decimals'],
                        job['mint_observed_at'], job['record_json'], time.time() if now is None else now, STATUS))
                    added += 1
                except sqlite3.IntegrityError:
                    pass  # an identity already has its one job
        return added
    except BaseException as error:  # noqa: BLE001
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return 0


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
