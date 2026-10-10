"""Fill-realism worker: measures quote drift of simulated paper fills OUTSIDE the trading pass.

    python -m tools.ops.fill_realism_worker --ledger L --config C [--allowance-per-hour 60]
        [--wait-seconds 12] [--late-seconds 4] [--systemd-credentials]

Reads the jobs the trading pass enqueued in ``<ledger>.fill-realism.sqlite`` (paper
config ``paper_fill_realism_version: 1``) and re-quotes each fill's SAME route and
input at +2s, +5s and +10s after the decision. It owns that one store: it never
opens the ledger, research or evidence stores, so nothing it does can touch a
NULL/outcome pass state or a position. Every provider request is charged to its OWN
rolling-hour allowance (recorded in the store) and goes through the shared
provider pacing; a failed attempt is charged and recorded, never retried.

Samples are scheduled by absolute due time across all jobs (a priority queue), so
simultaneous fills keep their +2s and +5s samples when the pacing allows; a sample
that cannot start within ``--late-seconds`` of its due time is recorded LATE with
its actual lag instead of being measured. A quote is valid only if it was observed
at or after decision + delay (otherwise REALISM_STALE_QUOTE). A kill mid-request
leaves an attempt row: the next start records it REALISM_INTERRUPTED (charged) and
never re-requests, so there is no double sampling and no double charge.
EXECUTION_UNVERIFIED: a re-quote is not a fill.
"""
import argparse
import json
import math
import os
import sqlite3
import sys
import time
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from desk import fill_realism as fr, quote_execution as qe
from desk.live_observation import MintObservation, ObservationError, ProviderObservation, SourceRecord, ingest_quote
from desk.model import digest
from desk.providers import SOL

DEFAULT_ALLOWANCE = 60
LATE_SECONDS = 4.0
MAX_SLEEP = 15.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT = 8.0
PENDING_BATCH = 64


class QuoteClientError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class JupiterQuoteClient:
    """The same unsigned route probe the trading pass uses, through the shared Jupiter pacing."""

    def __init__(self, pacer, key):
        if pacer is None:
            raise QuoteClientError('PACING_NOT_CONFIGURED')
        if type(key) is not str or not 1 <= len(key) <= 512 or any(not 33 <= ord(ch) <= 126 for ch in key):
            raise QuoteClientError('CREDENTIAL_UNAVAILABLE')
        self.pacer, self.key = pacer, key

    def __call__(self, input_mint, output_mint, amount, taker, *, timeout_seconds):
        from urllib.error import HTTPError, URLError
        from urllib.parse import urlencode
        from urllib.request import HTTPRedirectHandler, Request, build_opener
        from desk import provider_pacing
        query = {'inputMint': input_mint, 'outputMint': output_mint, 'amount': str(amount), 'taker': taker,
                 'slippageBps': '100', 'transactionVersion': '0'}
        try:
            ticket = self.pacer.acquire('jupiter', timeout_seconds=timeout_seconds)
        except provider_pacing.PacingError as error:
            raise QuoteClientError(error.code) from None
        request = Request('https://api.jup.ag/swap/v2/build?' + urlencode(query), method='GET',
                          headers={'Accept': 'application/json', 'x-api-key': self.key})

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        released = False
        try:
            with build_opener(NoRedirect()).open(request, timeout=REQUEST_TIMEOUT) as response:
                if provider_pacing.should_throttle(response.status, response.headers):
                    self.pacer.throttle('jupiter', response.headers, ticket=ticket); released = True
                    raise QuoteClientError('HTTP_THROTTLED')
                if response.status != 200:
                    raise QuoteClientError('HTTP_REJECTED')
                if (response.headers.get('Content-Encoding') or 'identity').lower() != 'identity':
                    raise QuoteClientError('RESPONSE_HEADERS_INVALID')
                age = response.headers.get('Age')
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            if provider_pacing.should_throttle(error.code, error.headers) and not released:
                self.pacer.throttle('jupiter', error.headers, ticket=ticket); released = True
            raise QuoteClientError('HTTP_REJECTED') from None
        except (URLError, OSError, TimeoutError):
            raise QuoteClientError('TRANSPORT_ERROR') from None
        finally:
            if not released:
                try:
                    self.pacer.finish('jupiter', ticket)
                except provider_pacing.PacingError:
                    pass
        completed = int(time.time())
        if len(raw) > MAX_RESPONSE_BYTES:
            raise QuoteClientError('RESPONSE_OVERSIZED')
        try:
            result = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite')))
        except ValueError:
            raise QuoteClientError('RESPONSE_INVALID') from None
        if type(result) is not dict or any(k not in result for k in ('inAmount', 'outAmount', 'routePlan')):
            raise QuoteClientError('RESPONSE_INVALID')
        age_seconds = int(age) if type(age) is str and age.isascii() and age.isdecimal() and len(age) < 9 else 0
        return {'kind': 'unsigned_route_probe', 'observed_at': completed, 'request': query, 'response': result,
                'age_seconds': age_seconds}


def _rows(c, sql, args=()):
    c.row_factory = sqlite3.Row
    return c.execute(sql, args).fetchall()


def _insert_sample(c, row):
    names = ','.join(row)
    c.execute(f'INSERT INTO {fr.SAMPLE_TABLE}({names}) VALUES({",".join("?" * len(row))})', tuple(row.values()))


def _resolve_interrupted(c, summary):
    """An attempt without a sample was killed mid-flight: record it charged, never re-request."""
    for r in _rows(c, f'''SELECT a.fill_event_id,a.side,a.delay_seconds,j.decision_at FROM {fr.ATTEMPT_TABLE} a
                          JOIN {fr.JOB_TABLE} j USING(fill_event_id,side)
                          WHERE NOT EXISTS(SELECT 1 FROM {fr.SAMPLE_TABLE} s WHERE s.fill_event_id=a.fill_event_id
                            AND s.side=a.side AND s.delay_seconds=a.delay_seconds)'''):
        _insert_sample(c, {'fill_event_id': r['fill_event_id'], 'side': r['side'], 'delay_seconds': r['delay_seconds'],
                           'status': 'FAILED', 'code': 'REALISM_INTERRUPTED', 'due_at': r['decision_at'] + r['delay_seconds'],
                           'charged': 1, 'execution_status': fr.STATUS})
        summary['interrupted'] += 1


def _pending(c):
    return _rows(c, f'''SELECT j.*, d.delay AS delay, j.decision_at + d.delay AS due_at FROM {fr.JOB_TABLE} j
        CROSS JOIN (SELECT 2 AS delay UNION ALL SELECT 5 UNION ALL SELECT 10) d
        WHERE NOT EXISTS(SELECT 1 FROM {fr.SAMPLE_TABLE} s WHERE s.fill_event_id=j.fill_event_id AND s.side=j.side AND s.delay_seconds=d.delay)
          AND NOT EXISTS(SELECT 1 FROM {fr.ATTEMPT_TABLE} a WHERE a.fill_event_id=j.fill_event_id AND a.side=j.side AND a.delay_seconds=d.delay)
        ORDER BY due_at, j.fill_event_id, j.side, d.delay LIMIT {PENDING_BATCH}''')


def _allowance_used(c, now):
    return c.execute(f'SELECT COUNT(*) FROM {fr.ATTEMPT_TABLE} WHERE started_at>?', (now - 3600,)).fetchone()[0]


def _allowance(c):
    return c.execute(f'SELECT allowance_per_hour FROM {fr.POLICY_TABLE} ORDER BY id DESC LIMIT 1').fetchone()[0]


def _sample(c, job, cfg, client, clock, late_seconds):
    """One due (job, delay). Never raises: every outcome becomes exactly one sample row."""
    delay, due = job['delay'], job['due_at']
    base = {'fill_event_id': job['fill_event_id'], 'side': job['side'], 'delay_seconds': delay, 'due_at': due,
            'execution_status': fr.STATUS}
    started = clock()
    lag = started - due
    if job['config_hash'] != digest(cfg):
        return {**base, 'status': 'FAILED', 'code': 'REALISM_CONFIG_MISMATCH', 'charged': 0}
    if lag > late_seconds:
        return {**base, 'status': 'LATE', 'code': 'REALISM_SAMPLE_LATE', 'charged': 0, 'lag_seconds': format(Decimal(str(lag)), 'f')}
    if _allowance_used(c, started) >= _allowance(c):
        return {**base, 'status': 'FAILED', 'code': 'REALISM_ALLOWANCE_EXHAUSTED', 'charged': 0}
    c.execute(f'INSERT INTO {fr.ATTEMPT_TABLE} VALUES(?,?,?,?)', (job['fill_event_id'], job['side'], delay, started))
    base = {**base, 'started_at': started, 'lag_seconds': format(Decimal(str(max(0.0, lag))), 'f'), 'charged': 1}
    try:
        record = json.loads(job['record_json'])
        side, amount = job['side'], record['input_raw']
        input_mint, output_mint = (SOL, job['mint']) if side == 'buy' else (job['mint'], SOL)
        payload = client(input_mint, output_mint, amount, job['taker'], timeout_seconds=min(REQUEST_TIMEOUT, max(1.0, late_seconds + 4)))
        completed = clock()
        base['completed_at'] = completed
        observed = payload.get('observed_at')
        if type(observed) is not int or payload.get('age_seconds', 0) > 1 or observed < math.floor(due):
            return {**base, 'status': 'STALE', 'code': 'REALISM_STALE_QUOTE'}  # cached/early: excluded, counted
        if completed - due > late_seconds + REQUEST_TIMEOUT:
            return {**base, 'status': 'LATE', 'code': 'REALISM_RESPONSE_LATE'}
        mint = MintObservation(job['mint'], 0, job['mint_decimals'], 1, None, None,
                               SourceRecord('fill-realism-decision-mint', job['mint_observed_at'], digest({'mint': job['mint']}), '{}'))
        quote = ingest_quote(lambda: ProviderObservation('fill-realism-quote', observed, payload), mint=mint, direction=side,
                             amount_raw=amount, taker=job['taker'], expected_pool=job['pool'], now=observed,
                             max_age_seconds=observed - job['mint_observed_at'] + 1)
        simulated = qe.output_raw(quote, cfg)
        values = fr.drift(side, (amount, record['estimated_output_raw'], record['simulated_output_raw']),
                          quote.estimated_output_raw, simulated, record['mint_decimals'])
        return {**base, 'status': 'MEASURED', 'code': None, 'requote_observed_at': observed,
                'requote_estimated_out_raw': quote.estimated_output_raw, 'requote_min_out_raw': quote.minimum_output_raw,
                'requote_simulated_out_raw': simulated, 'requote_hash': quote.source.raw_hash,
                'requote_json': quote.source.original_json, **values}
    except QuoteClientError as error:
        return {**base, 'status': 'FAILED', 'code': error.code}
    except (ObservationError, qe.QuoteExecutionError, ValueError, TypeError, KeyError, ArithmeticError):
        return {**base, 'status': 'FAILED', 'code': 'REQUOTE_INVALID'}
    except Exception:  # noqa: BLE001 - the attempt is already charged; record it, keep the worker alive
        return {**base, 'status': 'FAILED', 'code': 'REALISM_INTERNAL_ERROR'}


def run(store, cfg, *, client, clock=time.time, sleep=time.sleep, wait_seconds=12.0, late_seconds=LATE_SECONDS,
        allowance_per_hour=DEFAULT_ALLOWANCE, max_samples=None):
    """Process due samples; sleep (bounded) for samples due within ``wait_seconds``; then return."""
    if fr.selected(cfg) != fr.VERSION:
        return {'status': 'OFF', 'samples': 0}
    summary = {'status': 'DONE', 'samples': 0, 'measured': 0, 'late': 0, 'stale': 0, 'failed': 0, 'charged': 0, 'interrupted': 0}
    with closing(fr.connect(store)) as c:
        fr.ensure_schema(c, allowance_per_hour=allowance_per_hour)
        _resolve_interrupted(c, summary)
        deadline = clock() + wait_seconds
        while max_samples is None or summary['samples'] < max_samples:
            pending = _pending(c)
            if not pending:
                break
            job = pending[0]
            now = clock()
            if job['due_at'] > now:
                if job['due_at'] > deadline:
                    break  # the next sample is beyond this run; a timer starts the worker again
                sleep(min(job['due_at'] - now, MAX_SLEEP))
                continue
            row = _sample(c, job, cfg, client, clock, late_seconds)
            try:
                _insert_sample(c, row)
            except sqlite3.Error:
                summary['status'] = 'STORE_ERROR'  # the attempt row stays: restart records it INTERRUPTED
                break
            summary['samples'] += 1
            summary['charged'] += row['charged']
            key = {'MEASURED': 'measured', 'LATE': 'late', 'STALE': 'stale'}.get(row['status'], 'failed')
            summary[key] += 1
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--ledger', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--allowance-per-hour', type=int, default=DEFAULT_ALLOWANCE)
    parser.add_argument('--wait-seconds', type=float, default=12.0)
    parser.add_argument('--late-seconds', type=float, default=LATE_SECONDS)
    parser.add_argument('--systemd-credentials', action='store_true')
    args = parser.parse_args(argv)
    try:
        from desk import provider_pacing
        from desk.paper_cycle_cli import _config
        cfg = _config(args.config)
        store = fr.store_path(args.ledger)
        if not store.is_file() or store.is_symlink():
            print(json.dumps({'status': 'NO_JOBS', 'execution_status': fr.STATUS}))
            return 0
        if args.systemd_credentials:
            from desk.paper_cycle_cli import _credentials
            _credentials()
        client = JupiterQuoteClient(provider_pacing.configured(priority='investigation'), os.environ.get('JUPITER_API_KEY'))
        result = run(store, cfg, client=client, wait_seconds=args.wait_seconds, late_seconds=args.late_seconds,
                     allowance_per_hour=args.allowance_per_hour)
    except (QuoteClientError, ValueError, OSError, sqlite3.Error, KeyError, TypeError) as error:
        print(json.dumps({'status': 'BLOCKED', 'error': type(error).__name__, 'execution_status': fr.STATUS}))
        return 2
    print(json.dumps({**result, 'execution_status': fr.STATUS}, sort_keys=True))
    return 0 if result['status'] in ('DONE', 'OFF') else 2


if __name__ == '__main__':
    sys.exit(main())
