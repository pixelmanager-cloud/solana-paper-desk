"""SYNTHETIC_TEST_ONLY: opt-in latency measurement of simulated paper fills, measured OUTSIDE the trading pass.

Provider bytes are scripted fixtures (no provider access). Known-answer numbers are hand computed in
comments. The trading pass only enqueues; the worker owns every request, sleep and charge.
"""
import contextlib
import copy
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from desk import fill_realism as fr, paper_cycle as cycle, quote_execution as qe
from desk import paper_terminal_reconciliation as terminal
from desk.ledger import Ledger
from desk.model import canonical, digest
from tests import test_paper_cycle as cycle_fixtures
from tests.helpers import T, config
from tools.ops import fill_realism_worker as worker
from tools.research import fill_realism_report as report

BUY = {'input_raw': 100_000_000, 'estimated_output_raw': 1_000_000_000, 'simulated_output_raw': 980_000_000,
       'quote_hash': 'q-buy', 'mint_decimals': 6, 'direction': 'buy'}
SELL = {'input_raw': 1_000_000_000, 'estimated_output_raw': 125_000_000, 'simulated_output_raw': 120_050_000,
        'quote_hash': 'q-sell', 'mint_decimals': 6, 'direction': 'sell'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def job_row(event, side, mint, record, ts, *, cfg_hash='cfg', decision_at=None, held=0, pool='POOL', taker='TAKER'):
    return {'fill_event_id': event, 'side': side, 'mint': mint, 'scan_id': 'scan-' + mint, 'pool': pool, 'taker': taker,
            'decision_ts': ts, 'decision_at': float(ts if decision_at is None else decision_at), 'held': held,
            'mint_decimals': record['mint_decimals'], 'mint_observed_at': ts - 1, 'record_json': json.dumps(record, sort_keys=True, separators=(',', ':'))}


def insert_jobs(ledger_path, jobs, cfg_hash='cfg'):
    return fr.enqueue(ledger_path, jobs, cfg_hash, now=1.0)


def sample_dict(job, delay, req_est, req_sim, *, status='MEASURED', code=None, charged=1, lag=None):
    base = {'fill_event_id': job['fill_event_id'], 'side': job['side'], 'delay_seconds': delay, 'status': status, 'code': code,
            'due_at': job['decision_at'] + delay, 'charged': charged, 'execution_status': fr.STATUS}
    if lag is not None:
        base['lag_seconds'] = str(lag)
    if status != 'MEASURED':
        return base
    record = json.loads(job['record_json'])
    body = {'delay': delay, 'est': req_est}
    values = fr.drift(job['side'], (record['input_raw'], record['estimated_output_raw'], record['simulated_output_raw']),
                      req_est, req_sim, record['mint_decimals'])
    return {**base, 'requote_estimated_out_raw': req_est, 'requote_min_out_raw': req_est * 99 // 100,
            'requote_simulated_out_raw': req_sim, 'requote_hash': digest(body), 'requote_json': canonical(body),
            'requote_observed_at': int(base['due_at']), 'lag_seconds': '0', **values}


def put_samples(ledger_path, rows):
    with contextlib.closing(fr.connect(fr.store_path(ledger_path))) as c:
        for row in rows:
            names = ','.join(row)
            c.execute(f'INSERT INTO {fr.SAMPLE_TABLE}({names}) VALUES({",".join("?" * len(row))})', tuple(row.values()))


class MathTests(unittest.TestCase):
    def test_buy_drift_known_answer(self):
        # price0 = 0.1/1000, price1 = 0.1/950 -> +526.3157894736842105... bps; sim 931/980 = 0.95 -> -500 bps.
        d = fr.drift('buy', (100_000_000, 1_000_000_000, 980_000_000), 950_000_000, 931_000_000, 6)
        self.assertEqual(Decimal(d['out_drift_bps']), Decimal(-500))
        self.assertEqual(Decimal(d['adverse_slippage_bps']), Decimal(500))
        self.assertEqual(Decimal(d['decision_price_sol']), Decimal('0.0001'))
        self.assertEqual(round(Decimal(d['price_drift_bps']), 12), Decimal('526.315789473684'))

    def test_sell_drift_known_answer(self):
        # sell 1000 tokens: price0 = 0.1/1000, price1 = 0.09/1000 -> -1000 bps; sim 85.5/95 = 0.9 -> -1000 bps.
        d = fr.drift('sell', (1_000_000_000, 100_000_000, 95_000_000), 90_000_000, 85_500_000, 6)
        self.assertEqual((Decimal(d['price_drift_bps']), Decimal(d['out_drift_bps']), Decimal(d['adverse_slippage_bps'])),
                         (Decimal(-1000), Decimal(-1000), Decimal(1000)))

    def test_better_requote_is_negative_adverse_slippage(self):
        d = fr.drift('buy', (100_000_000, 1_000_000_000, 980_000_000), 1_100_000_000, 1_078_000_000, 6)
        self.assertEqual(Decimal(d['adverse_slippage_bps']), Decimal(-1000))

    def test_nonpositive_amount_refused(self):
        for bad in (0, -1):
            with self.assertRaises(qe.QuoteExecutionError):
                fr.drift('buy', (100_000_000, 1_000_000_000, 980_000_000), 950_000_000, bad, 6)

    def test_latency_adjusted_pnl_known_answer(self):
        # rb=0.95, rs=0.9, gross 0.12005 -> 0.12005*0.9*0.95 = 0.10264275; -fee = 0.10259275; cost 0.10005 -> 0.00254275
        pnl = fr.latency_adjusted_pnl({'amount_sol': '0.1', 'fee_sol': '0.00005'},
            [{'proceeds_sol': '0.12', 'fee_sol': '0.00005'}],
            {'decision_simulated_out_raw': 980_000_000, 'requote_simulated_out_raw': 931_000_000},
            [{'decision_simulated_out_raw': 120_050_000, 'requote_simulated_out_raw': 108_045_000}])
        self.assertEqual(pnl, Decimal('0.00254275'))

    def test_identity_samples_reproduce_paper_pnl(self):
        pnl = fr.latency_adjusted_pnl({'amount_sol': '0.1', 'fee_sol': '0.00005'},
            [{'proceeds_sol': '0.12', 'fee_sol': '0.00005'}],
            {'decision_simulated_out_raw': 7, 'requote_simulated_out_raw': 7},
            [{'decision_simulated_out_raw': 9, 'requote_simulated_out_raw': 9}])
        self.assertEqual(pnl, Decimal('0.12') - Decimal('0.1') - Decimal('0.00005'))

    def test_config_selection(self):
        base = {**config(), 'paper_signal_policy_version': 3, 'paper_quote_execution_version': 1}
        self.assertEqual(fr.selected(base), 0)
        self.assertEqual(fr.selected({**base, fr.KEY: 1}), 1)
        for bad in (0, 2, True, '1', 1.0):
            with self.assertRaises(qe.QuoteExecutionError):
                qe.config({**base, fr.KEY: bad})
        with self.assertRaises(qe.QuoteExecutionError):
            fr.selected({**config(), fr.KEY: 1})  # requires the quote-execution profile


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / 'ledger.sqlite'
        self.job = job_row('e1', 'buy', 'MINT', BUY, T)
        self.assertEqual(insert_jobs(self.ledger, [self.job]), 1)
        self.store = fr.store_path(self.ledger)

    def test_store_is_a_separate_file_and_jobs_are_unique(self):
        self.assertTrue(str(self.store).endswith('.fill-realism.sqlite'))
        self.assertFalse(self.ledger.exists())                      # enqueueing never creates or opens the ledger
        self.assertEqual(insert_jobs(self.ledger, [self.job]), 0)   # one job per fill identity
        self.assertEqual(insert_jobs(self.ledger, [job_row('e1', 'sell', 'MINT', SELL, T + 9)]), 1)

    def test_append_only_and_bound_to_a_job(self):
        put_samples(self.ledger, [sample_dict(self.job, 2, 950_000_000, 931_000_000)])
        with contextlib.closing(fr.connect(self.store)) as c:
            c.execute(f"INSERT INTO {fr.ATTEMPT_TABLE} VALUES('e1','buy',5,1.0)")
            c.execute(f'INSERT INTO {fr.POLICY_TABLE}(at,allowance_per_hour) VALUES(1.0,60)')
            for table in (fr.JOB_TABLE, fr.ATTEMPT_TABLE, fr.SAMPLE_TABLE, fr.POLICY_TABLE):
                self.assertGreater(c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)  # triggers fire only on existing rows
            for sql in (f'UPDATE {fr.SAMPLE_TABLE} SET out_drift_bps=0', f'DELETE FROM {fr.SAMPLE_TABLE}',
                        f'UPDATE {fr.JOB_TABLE} SET mint=mint', f'DELETE FROM {fr.JOB_TABLE}',
                        f'UPDATE {fr.ATTEMPT_TABLE} SET started_at=0', f'UPDATE {fr.POLICY_TABLE} SET allowance_per_hour=1'):
                with self.assertRaises(sqlite3.DatabaseError):
                    c.execute(sql)
        with self.assertRaises(sqlite3.IntegrityError):               # a sample needs its job
            put_samples(self.ledger, [sample_dict({**self.job, 'fill_event_id': 'ghost'}, 2, 1, 1)])
        with self.assertRaises(sqlite3.IntegrityError):               # one sample per (fill, side, delay)
            put_samples(self.ledger, [sample_dict(self.job, 2, 900_000_000, 800_000_000)])
        with self.assertRaises(sqlite3.IntegrityError):
            put_samples(self.ledger, [sample_dict(self.job, 3, 950_000_000, 931_000_000)])

    def test_status_and_label_checks(self):
        with contextlib.closing(fr.connect(self.store)) as c:
            with self.assertRaises(sqlite3.IntegrityError):  # a failure must carry a code
                c.execute(f"INSERT INTO {fr.SAMPLE_TABLE}(fill_event_id,side,delay_seconds,status,due_at,charged,execution_status) "
                          "VALUES('e1','buy',2,'FAILED',1.0,0,'EXECUTION_UNVERIFIED')")
            with self.assertRaises(sqlite3.IntegrityError):  # label cannot be promoted
                c.execute(f"INSERT INTO {fr.SAMPLE_TABLE}(fill_event_id,side,delay_seconds,status,code,due_at,charged,execution_status) "
                          "VALUES('e1','buy',2,'FAILED','X',1.0,0,'VERIFIED')")

    def test_enqueue_never_raises(self):
        unwritable = Path(self.tmp.name) / 'missing-dir' / 'ledger.sqlite'
        self.assertEqual(insert_jobs(unwritable, [self.job]), 0)                  # directory does not exist
        with patch.object(fr, 'connect', side_effect=MemoryError('simulated')):
            self.assertEqual(fr.enqueue(self.ledger, [job_row('e9', 'buy', 'M', BUY, T)], 'cfg'), 0)
        with patch.object(fr, 'connect', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):                            # a real interrupt is not swallowed
                fr.enqueue(self.ledger, [job_row('e9', 'buy', 'M', BUY, T)], 'cfg')
        self.assertEqual(fr.enqueue(self.ledger, [{'fill_event_id': 'broken'}], 'cfg'), 0)  # malformed job spec

    def test_collect_is_pure_bounded_and_never_raises(self):
        class Target: scan_id, pool, taker = 'scan', 'pool', 'taker'
        class Source: observed_at = 7
        class Mint: source = Source()
        class Collected: target, mint = Target(), Mint()
        outcomes = [{'type': 'fill', 'side': 'buy', 'mint': 'M', 'quote_execution': BUY}, {'type': 'reject'},
                    {'type': 'fill', 'side': 'sell', 'mint': 'M', 'quote_execution': {**SELL, 'direction': 'buy'}},   # wrong leg
                    {'type': 'fill', 'side': 'sell', 'mint': 'M', 'quote_execution': {**SELL, 'x': 'y' * 20000}}]    # oversize
        jobs = fr.collect(outcomes, {'event_id': 'e', 'ts': T}, Collected(), False, wall_clock=lambda: 12.5)
        self.assertEqual([(j['side'], j['decision_at'], j['decision_ts'], j['held']) for j in jobs], [('buy', 12.5, T, 0)])
        self.assertEqual(fr.collect(outcomes, {'event_id': 'e', 'ts': T}, object(), True), [])    # malformed collected: still no raise
        self.assertEqual(fr.collect(None, {}, None, False), [])


class Clock:
    def __init__(self, now):
        self.now, self.sleeps = float(now), []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds); self.now += seconds


class WorkerTests(unittest.TestCase):
    """Real worker, real store, scripted client and clock: nothing sleeps or touches a network."""

    def setUp(self):
        self.q = cycle_fixtures.QuoteOnlyCycleTests('test_entry_reuses_existing_buy_and_only_reads_exact_reverse_once')
        self.q.setUp(); self.addCleanup(self.q.doCleanups)
        self.cfg = {**self.q.cfg, fr.KEY: 1}
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / 'ledger.sqlite'                  # never created: the worker cannot need it
        self.store = fr.store_path(self.ledger)
        buy = self.q.f.buy
        self.record = {'direction': 'buy', 'input_raw': buy.input_raw, 'estimated_output_raw': buy.estimated_output_raw,
                       'simulated_output_raw': qe.output_raw(buy, self.cfg), 'quote_hash': buy.source.raw_hash,
                       'mint_decimals': self.q.token.decimals}
        self.clock = Clock(T)
        self.script, self.calls = [], []
        self.age = 0

    def add_job(self, event='fill-1', decision_at=None, cfg_hash=None, ts=T):
        job = job_row(event, 'buy', self.q.f.mint, self.record, ts, decision_at=decision_at, pool=self.q.f.pool, taker=self.q.f.wallet)
        job['mint_observed_at'] = self.q.token.source.observed_at
        fr.enqueue(self.ledger, [job], cfg_hash or digest(self.cfg), now=1.0)
        return job

    def client(self, input_mint, output_mint, amount, taker, *, timeout_seconds):
        step = self.script.pop(0) if self.script else 990_000
        self.calls.append((self.clock.now, amount))
        self.clock.now += 0.3                                                # request latency
        if isinstance(step, BaseException):
            raise step
        output, observed = step if isinstance(step, tuple) else (step, int(self.clock.now))
        payload = json.loads(self.q.f.quote('buy', amount, output).source.original_json)
        payload['observed_at'] = observed
        payload['age_seconds'] = self.age
        return payload

    def go(self, **kw):
        defaults = dict(client=self.client, clock=self.clock, sleep=self.clock.sleep, wait_seconds=12.0)
        return worker.run(self.store, self.cfg, **{**defaults, **kw})

    def rows(self):
        with contextlib.closing(sqlite3.connect(self.store)) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(f'SELECT * FROM {fr.SAMPLE_TABLE} ORDER BY due_at,fill_event_id,delay_seconds')]

    def attempts(self):
        with contextlib.closing(sqlite3.connect(self.store)) as c:
            return c.execute(f'SELECT COUNT(*) FROM {fr.ATTEMPT_TABLE}').fetchone()[0]

    def test_three_samples_scheduled_on_absolute_time_and_charged_to_its_own_allowance(self):
        self.add_job(); self.script[:] = [990_000, 970_000, 940_000]
        summary = self.go()
        self.assertEqual((summary['measured'], summary['charged']), (3, 3))
        rows = self.rows()
        self.assertEqual([r['delay_seconds'] for r in rows], [2, 5, 10])
        for r in rows:
            self.assertGreaterEqual(r['started_at'], r['due_at'])           # never before decision + delay
            self.assertLess(Decimal(r['lag_seconds']), 1)
            self.assertEqual((r['status'], r['charged'], r['execution_status']), ('MEASURED', 1, 'EXECUTION_UNVERIFIED'))
        self.assertAlmostEqual(self.clock.sleeps[0], 2.0)                    # slept until the +2s instant, not a fixed delay
        self.assertEqual(self.attempts(), 3)
        slip = int(Decimal(self.cfg['adverse_slippage_bps']))
        for row, out in zip(rows, (990_000, 970_000, 940_000)):
            sim = (out * 99 // 100) * (10000 - slip) // 10000
            self.assertEqual(row['requote_simulated_out_raw'], sim)
            expected = (Decimal(sim) / Decimal(self.record['simulated_output_raw']) - 1) * 10000
            self.assertEqual(round(Decimal(row['out_drift_bps']), 20), round(expected, 20))
        self.assertTrue(all(c[1] == self.record['input_raw'] for c in self.calls))
        self.assertFalse(self.ledger.exists())                               # the worker never needed the ledger

    def test_concurrent_fills_keep_their_2s_and_5s_samples(self):
        self.add_job('fill-1', decision_at=T); self.add_job('fill-2', decision_at=T + 1)
        summary = self.go(wait_seconds=15)
        self.assertEqual((summary['measured'], summary['late'], summary['stale']), (6, 0, 0))
        self.assertEqual([r['status'] for r in self.rows()], ['MEASURED'] * 6)
        due_order = [r['due_at'] for r in self.rows()]
        self.assertEqual(due_order, sorted(due_order))                       # processed by absolute due time across jobs
        self.assertEqual([c[0] for c in self.calls], sorted(c[0] for c in self.calls))
        self.assertEqual({(r['fill_event_id'], r['delay_seconds']) for r in self.rows()},
                         {(f, d) for f in ('fill-1', 'fill-2') for d in (2, 5, 10)})

    def test_stale_or_cached_quotes_are_excluded_and_counted(self):
        self.add_job()
        self.script[:] = [(990_000, T + 1), 970_000, 940_000]               # +2s quote claims an earlier observation time
        self.age = 0
        self.go(max_samples=1)
        first = self.rows()[0]
        self.assertEqual((first['status'], first['code'], first['charged']), ('STALE', 'REALISM_STALE_QUOTE', 1))
        self.assertIsNone(first['out_drift_bps'])                            # no drift number is ever stored for it
        self.age = 7                                                         # a CDN-cached copy (Age header) is also refused
        self.go(max_samples=1)
        self.assertEqual(self.rows()[1]['code'], 'REALISM_STALE_QUOTE')
        self.age = 0
        self.go()
        self.assertEqual([r['status'] for r in self.rows()], ['STALE', 'STALE', 'MEASURED'])

    def test_late_start_records_actual_lag_and_makes_no_request(self):
        self.add_job(decision_at=T); self.clock.now = T + 9.5                # worker was down until +9.5s
        summary = self.go()
        rows = self.rows()
        self.assertEqual([(r['delay_seconds'], r['status'], r['code'], r['charged']) for r in rows],
                         [(2, 'LATE', 'REALISM_SAMPLE_LATE', 0), (5, 'LATE', 'REALISM_SAMPLE_LATE', 0), (10, 'MEASURED', None, 1)])
        self.assertEqual((Decimal(rows[0]['lag_seconds']), Decimal(rows[1]['lag_seconds'])), (Decimal('7.5'), Decimal('4.5')))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((summary['late'], summary['charged']), (2, 1))

    def test_every_failure_mode_is_recorded_and_the_worker_survives(self):
        self.add_job()
        self.script[:] = [worker.QuoteClientError('HTTP_REJECTED'), RuntimeError('SECRET api-key=abc'), ValueError('bad')]
        self.go()
        rows = self.rows()
        self.assertEqual([(r['status'], r['code'], r['charged']) for r in rows],
                         [('FAILED', 'HTTP_REJECTED', 1), ('FAILED', 'REALISM_INTERNAL_ERROR', 1), ('FAILED', 'REQUOTE_INVALID', 1)])
        self.assertNotIn('SECRET', canonical(rows))
        # An invalid provider body is a recorded, charged failure too.
        self.add_job('fill-2', decision_at=self.clock.now)
        def broken(input_mint, output_mint, amount, taker, *, timeout_seconds):
            self.clock.now += 0.1
            return {'kind': 'unsigned_route_probe', 'observed_at': int(self.clock.now), 'request': {}, 'response': {}}
        self.go(client=broken)
        invalid = [r for r in self.rows() if r['fill_event_id'] == 'fill-2']
        self.assertEqual({(r['status'], r['code']) for r in invalid}, {('FAILED', 'REQUOTE_INVALID')})

    def test_own_allowance_is_enforced_and_rolls(self):
        self.add_job()
        summary = self.go(allowance_per_hour=2)
        rows = self.rows()
        self.assertEqual([(r['status'], r['code'], r['charged']) for r in rows],
                         [('MEASURED', None, 1), ('MEASURED', None, 1), ('FAILED', 'REALISM_ALLOWANCE_EXHAUSTED', 0)])
        self.assertEqual((len(self.calls), summary['charged']), (2, 2))
        self.add_job('fill-2', decision_at=self.clock.now + 3601)            # an hour later the rolling window has room
        self.clock.now += 3601
        self.go(allowance_per_hour=2)
        self.assertEqual(len(self.calls), 4)

    def test_restart_resumes_without_double_sampling_or_double_charging(self):
        self.add_job()
        self.go(max_samples=1)
        self.assertEqual((len(self.rows()), len(self.calls)), (1, 1))
        self.go()                                                           # a "restarted" worker: same store, new run
        self.go()                                                           # and again: nothing left to do
        self.assertEqual((len(self.rows()), len(self.calls), self.attempts()), (3, 3, 3))
        self.assertEqual(len({(r['fill_event_id'], r['delay_seconds']) for r in self.rows()}), 3)

    def test_kill_mid_request_is_recorded_charged_and_never_rerequested(self):
        self.add_job()
        self.script[:] = [990_000, KeyboardInterrupt(), 940_000]            # SIGKILL stand-in on the +5s request
        with self.assertRaises(KeyboardInterrupt):
            self.go()
        self.assertEqual((len(self.rows()), self.attempts()), (1, 2))        # attempt row exists, sample row does not
        summary = self.go()                                                 # restart
        self.assertEqual(summary['interrupted'], 1)
        rows = self.rows()
        self.assertEqual([(r['delay_seconds'], r['status'], r['code'], r['charged']) for r in rows],
                         [(2, 'MEASURED', None, 1), (5, 'FAILED', 'REALISM_INTERRUPTED', 1), (10, 'MEASURED', None, 1)])
        self.assertEqual((len(self.calls), self.attempts()), (3, 3))         # the interrupted sample was never requested again

    def test_kill_before_the_first_request_costs_nothing(self):
        self.add_job()
        with patch.object(worker, '_sample', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.go()
        self.assertEqual((len(self.rows()), self.attempts(), len(self.calls)), (0, 0, 0))
        self.assertEqual(self.go()['measured'], 3)

    def test_follow_mode_picks_up_jobs_that_arrive_while_running(self):
        added = []
        def sleep(seconds):
            self.clock.sleeps.append(seconds); self.clock.now += seconds
            if not added and self.clock.now >= T + 2:
                added.append(self.add_job('late-arrival', decision_at=self.clock.now))
        self.go(sleep=sleep, follow_seconds=20, wait_seconds=12)
        self.assertEqual(len(added), 1)
        self.assertEqual({r['fill_event_id'] for r in self.rows()}, {'late-arrival'})
        self.assertEqual([r['status'] for r in self.rows()], ['MEASURED'] * 3)
        self.assertLessEqual(self.clock.now, T + 20 + 1)                 # bounded: the service run ends by itself
        # Without follow the same empty store returns immediately.
        quiet = Clock(T)
        self.assertEqual(worker.run(self.store, self.cfg, client=self.client, clock=quiet, sleep=quiet.sleep)['samples'], 0)
        self.assertEqual(quiet.sleeps, [])

    def test_config_mismatch_and_off(self):
        self.add_job(cfg_hash='another-config-hash')
        self.go()
        self.assertEqual({(r['status'], r['code'], r['charged']) for r in self.rows()}, {('FAILED', 'REALISM_CONFIG_MISMATCH', 0)})
        self.assertEqual(len(self.calls), 0)
        off = Path(self.tmp.name) / 'off.sqlite'
        self.assertEqual(worker.run(off, self.q.cfg, client=self.client)['status'], 'OFF')
        self.assertFalse(off.exists())

    def test_worker_cannot_touch_trading_stores(self):
        source = Path(worker.__file__).read_text()
        for forbidden in ('Ledger(', 'paper_observation_passes', 'evidence.sqlite', 'JobPersistence', 'HistoryProgress', 'run_once', 'MonitoringBudget', 'reserve('):
            self.assertNotIn(forbidden, source)
        opened = []
        real = sqlite3.connect
        def spy(target, *a, **k):
            opened.append(str(target)); return real(target, *a, **k)
        self.add_job()
        with patch('sqlite3.connect', spy):
            self.go()
        self.assertEqual({o.split('?')[0].replace('file:', '') for o in opened}, {str(self.store)})

    def test_main_without_jobs_is_a_noop(self):
        out = io.StringIO()
        cfg_path = Path(self.tmp.name) / 'cfg.json'; cfg_path.write_text(canonical(self.cfg))
        with contextlib.redirect_stdout(out):
            self.assertEqual(worker.main(['--ledger', str(self.ledger), '--config', str(cfg_path)]), 0)
        self.assertEqual(json.loads(out.getvalue())['status'], 'NO_JOBS')


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.sqlite'
        ledger = Ledger(self.path); ledger.close()
        with contextlib.closing(sqlite3.connect(self.path)) as c, c:
            c.execute("INSERT INTO metadata VALUES('config_hash','cfg')")
            c.execute("INSERT INTO metadata VALUES('config',?)", (json.dumps({'version': 'v-test', fr.KEY: 1}),))
            self.fill(c, 'buy-A', 'A', {'type': 'fill', 'side': 'buy', 'mint': 'A', 'amount_sol': '0.1', 'fee_sol': '0.00005',
                                        'quantity': '1000', 'quote_execution': BUY}, T)
            self.fill(c, 'sell-A', 'A', {'type': 'fill', 'side': 'sell', 'mint': 'A', 'reason': 'STOP', 'quantity': '1000',
                                         'proceeds_sol': '0.12', 'fee_sol': '0.00005', 'realized_pnl_sol': '0.01995',
                                         'quote_execution': SELL}, T + 600)
            self.fill(c, 'buy-B', 'B', {'type': 'fill', 'side': 'buy', 'mint': 'B', 'amount_sol': '0.1', 'fee_sol': '0.00005',
                                        'quantity': '1000', 'quote_execution': BUY}, T + 700)
            self.fill(c, 'buy-C', 'C', {'type': 'fill', 'side': 'buy', 'mint': 'C', 'amount_sol': '0.1', 'fee_sol': '0.00005',
                                        'quantity': '1000', 'quote_execution': BUY}, T + 800)   # never enqueued
        self.jobs = {'buy-A': job_row('buy-A', 'buy', 'A', BUY, T), 'sell-A': job_row('sell-A', 'sell', 'A', SELL, T + 600),
                     'buy-B': job_row('buy-B', 'buy', 'B', BUY, T + 700)}
        insert_jobs(self.path, list(self.jobs.values()))
        put_samples(self.path, [
            sample_dict(self.jobs['buy-A'], 2, 950_000_000, 931_000_000),
            sample_dict(self.jobs['sell-A'], 2, 112_500_000, 108_045_000),
            sample_dict(self.jobs['buy-A'], 5, 900_000_000, 882_000_000),
            sample_dict(self.jobs['sell-A'], 5, 0, 0, status='FAILED', code='REQUOTE_INVALID'),
            sample_dict(self.jobs['buy-B'], 2, 900_000_000, 882_000_000),
            sample_dict(self.jobs['buy-B'], 5, 0, 0, status='LATE', code='REALISM_SAMPLE_LATE', charged=0, lag=6),
            sample_dict(self.jobs['buy-B'], 10, 0, 0, status='STALE', code='REALISM_STALE_QUOTE')])

    def fill(self, c, event, mint, payload, ts):
        c.execute("INSERT INTO events(event_id,ts,payload,payload_hash) VALUES(?,?,'{}','h')", (event, ts))
        c.execute('INSERT INTO outcomes(event_id,payload) VALUES(?,?)', (event, canonical(payload)))

    def test_known_answers(self):
        r = report.report(self.path)
        self.assertEqual((r['status'], r['execution_status'], r['paper_only'], r['samples'], r['verified_samples']),
                         ('REPORTED', 'EXECUTION_UNVERIFIED', True, 7, 7))
        # buy 2s drifts: A -500, B -1000 -> mean -750, median -750, p10 -950, p90 -550
        stats = r['aggregate']['buy']['2']['out_drift_bps']
        self.assertEqual([Decimal(stats[k]) for k in ('mean', 'median', 'p10', 'p90', 'min', 'max')],
                         [Decimal(-750), Decimal(-750), Decimal(-950), Decimal(-550), Decimal(-1000), Decimal(-500)])
        self.assertEqual(r['aggregate']['sell']['5']['not_measured'], 1)
        self.assertEqual(r['aggregate']['sell']['5']['charged_attempts'], 1)
        by = {t['mint']: t for t in r['trades']}
        self.assertEqual((by['A']['status'], by['B']['status']), ('CLOSED', 'OPEN_OR_PARTIAL'))
        self.assertEqual(Decimal(by['A']['paper_pnl_sol']), Decimal('0.01995'))
        self.assertEqual(Decimal(by['A']['latency_adjusted_pnl_sol']['2']), Decimal('0.00254275'))
        self.assertIsNone(by['A']['latency_adjusted_pnl_sol']['5'])    # failed sell sample: never imputed
        self.assertIsNone(by['A']['latency_adjusted_pnl_sol']['10'])   # no sample at all
        self.assertIsNone(by['B']['latency_adjusted_pnl_sol']['2'])    # still open
        two = r['pnl_by_delay']['2']
        self.assertEqual((two['trades_measured'], Decimal(two['delta_sol'])), (1, Decimal('0.00254275') - Decimal('0.01995')))
        self.assertEqual((r['pnl_by_delay']['5']['trades_measured'], r['pnl_by_delay']['5']['trades_unmeasured']), (0, 1))

    def test_coverage_is_explicit_per_side_and_never_hidden(self):
        r = report.report(self.path)
        buy, sell = r['coverage']['buy'], r['coverage']['sell']
        self.assertEqual((buy['fills'], buy['enqueued'], buy['not_enqueued']), (3, 2, 1))       # buy-C was never enqueued
        self.assertEqual((sell['fills'], sell['enqueued'], sell['not_enqueued']), (1, 1, 0))
        self.assertEqual({k: buy['delays']['2'][k] for k in ('measured', 'pending', 'complete')}, {'measured': 2, 'pending': 0, 'complete': False})
        self.assertEqual(buy['delays']['5']['late'], 1); self.assertEqual(buy['delays']['5']['non_measured_codes'], {'REALISM_SAMPLE_LATE': 1})
        self.assertEqual(buy['delays']['10']['stale'], 1); self.assertEqual(buy['delays']['10']['pending'], 1)
        self.assertEqual(sell['delays']['5']['non_measured_codes'], {'REQUOTE_INVALID': 1})
        self.assertEqual(sell['delays']['10']['pending'], 1)
        self.assertFalse(r['coverage_complete']); self.assertIn('PARTIAL_COVERAGE', r['coverage_warning'])
        self.assertEqual(r['aggregate']['buy']['5']['lag_seconds']['n'], 2)   # the measured and the LATE sample both carry a lag

    def test_full_coverage_is_reported_as_complete(self):
        # Keep only the closed trade A and measure every leg at every delay.
        fresh = Path(self.tmp.name) / 'full.sqlite'; Ledger(fresh).close()
        with contextlib.closing(sqlite3.connect(fresh)) as c, c:
            c.execute("INSERT INTO metadata VALUES('config_hash','cfg')")
            c.execute("INSERT INTO metadata VALUES('config',?)", (json.dumps({'version': 'v', fr.KEY: 1}),))
            self.fill(c, 'buy-A', 'A', {'type': 'fill', 'side': 'buy', 'mint': 'A', 'amount_sol': '0.1', 'fee_sol': '0.00005', 'quantity': '1000', 'quote_execution': BUY}, T)
            self.fill(c, 'sell-A', 'A', {'type': 'fill', 'side': 'sell', 'mint': 'A', 'reason': 'STOP', 'quantity': '1000', 'proceeds_sol': '0.12',
                                         'fee_sol': '0.00005', 'realized_pnl_sol': '0.01995', 'quote_execution': SELL}, T + 600)
        jobs = [self.jobs['buy-A'], self.jobs['sell-A']]
        insert_jobs(fresh, jobs)
        put_samples(fresh, [sample_dict(j, d, 950_000_000 if j['side'] == 'buy' else 112_500_000,
                                        931_000_000 if j['side'] == 'buy' else 108_045_000) for j in jobs for d in fr.DELAYS])
        r = report.report(fresh)
        self.assertTrue(r['coverage_complete']); self.assertIsNone(r['coverage_warning'])
        self.assertEqual(Decimal(r['trades'][0]['latency_adjusted_pnl_sol']['10']), Decimal('0.00254275'))

    def tamper(self, sql, args=()):
        with contextlib.closing(sqlite3.connect(fr.store_path(self.path))) as c, c:
            for table in (fr.SAMPLE_TABLE, fr.JOB_TABLE):
                for trigger in ('no_update', 'no_delete'):
                    c.execute(f'DROP TRIGGER IF EXISTS {table}_{trigger}')
            c.execute(sql, args)

    def test_tampered_drift_excluded_and_flagged(self):
        self.tamper(f"UPDATE {fr.SAMPLE_TABLE} SET out_drift_bps='0' WHERE fill_event_id='buy-A' AND delay_seconds=2")
        r = report.report(self.path)
        self.assertEqual(r['verified_samples'], 6)
        self.assertEqual([e['code'] for e in r['integrity_errors']], ['DRIFT_MISMATCH'])
        self.assertIsNone([t for t in r['trades'] if t['mint'] == 'A'][0]['latency_adjusted_pnl_sol']['2'])

    def test_tampered_job_binding_and_requote_hash_flagged(self):
        record = json.dumps({**SELL, 'quote_hash': 'forged'}, sort_keys=True, separators=(',', ':'))
        self.tamper(f"UPDATE {fr.JOB_TABLE} SET record_json=? WHERE fill_event_id='sell-A'", (record,))
        self.tamper(f"UPDATE {fr.SAMPLE_TABLE} SET requote_hash='forged' WHERE fill_event_id='buy-B' AND delay_seconds=2")
        codes = sorted(e['code'] for e in report.report(self.path)['integrity_errors'])
        self.assertEqual(codes, ['DECISION_BINDING_MISMATCH', 'DECISION_BINDING_MISMATCH', 'REQUOTE_HASH_MISMATCH'])

    def test_job_without_a_ledger_fill_is_flagged(self):
        insert_jobs(self.path, [job_row('ghost', 'buy', 'G', BUY, T + 5)])
        self.assertIn('JOB_WITHOUT_LEDGER_FILL', [e['code'] for e in report.report(self.path)['integrity_errors']])

    def test_read_only_and_symlink_refused(self):
        before = (sha(self.path), sha(fr.store_path(self.path)))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(report.main(['--ledger', str(self.path)]), 0)
        self.assertEqual(json.loads(out.getvalue())['status'], 'REPORTED')
        self.assertEqual((sha(self.path), sha(fr.store_path(self.path))), before)
        link = Path(self.tmp.name) / 'link.sqlite'; link.symlink_to(self.path)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(report.main(['--ledger', str(link)]), 2)
            self.assertEqual(report.main(['--ledger', str(self.path) + '.none']), 2)

    def test_ledger_without_a_store_reports_no_data(self):
        plain = Path(self.tmp.name) / 'plain.sqlite'
        Ledger(plain).close()
        self.assertEqual(report.report(plain)['status'], 'NO_REALISM_DATA')


class CycleIntegrationTests(unittest.TestCase):
    """Real entry/exit cycle harness (synthetic wire bytes): the pass only enqueues."""

    def make(self, flag, *, monitoring=False):
        fx = cycle_fixtures.PaperCycleTests('run_cycle')
        fx.setUp(); self.addCleanup(fx.doCleanups)
        fx.http_calls, fx.sell_output = [], 10_000_000
        if flag:
            fx.cfg = {**fx.cfg, fr.KEY: 1}
            fx.path = Path(fx.f.tmp.name) / 'realism-cycle.sqlite'
            cycle.initialize(fx.path, fx.cfg)
        if monitoring:
            allowance = cycle.MonitoringBudget(fx.f.progress.store, fx.path, fx.cfg, clock=lambda: fx.f.at)
            allowance.provision()
        return fx

    def held_item(self, fx):
        from dataclasses import replace
        p = cycle._state(fx.path, fx.cfg)['positions'][fx.target.mint]
        return replace(fx.item, target=replace(fx.target, amount_raw=qe.raw_quantity(p['qty'], 6)), graduation_refs=())

    def store_rows(self, fx, table):
        with contextlib.closing(sqlite3.connect(fr.store_path(fx.path))) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(f'SELECT * FROM {table} ORDER BY rowid')]

    def test_entry_only_enqueues_and_changes_nothing_else(self):
        off, on = self.make(False), self.make(True)
        baseline = off.actual_cycle()
        with patch('time.sleep', side_effect=AssertionError('the trading pass must never sleep for measurement')):
            entry = on.actual_cycle()
        self.assertEqual((baseline['status'], entry['status']), ('COMPLETE', 'COMPLETE'))
        self.assertEqual(entry['attempted_requests'], baseline['attempted_requests'])      # zero extra charged requests
        self.assertNotIn('fill_realism', entry)
        jobs = self.store_rows(on, fr.JOB_TABLE)
        self.assertEqual([(j['side'], j['held'], j['execution_status']) for j in jobs], [('buy', 0, 'EXECUTION_UNVERIFIED')])
        job = jobs[0]
        fill = next(x for x in entry['outcomes'] if x.get('side') == 'buy')
        self.assertEqual(json.loads(job['record_json']), fill['quote_execution'])
        self.assertEqual(job['config_hash'], digest(on.cfg)); self.assertEqual(job['mint'], on.target.mint)
        self.assertIsInstance(job['decision_at'], float)                                    # sub-second precision
        self.assertGreaterEqual(job['decision_at'], job['decision_ts'] - 1)
        self.assertEqual(self.store_rows(on, fr.SAMPLE_TABLE), [])                          # measuring is not the pass's business
        with contextlib.closing(sqlite3.connect(on.path)) as c:                             # the ledger knows nothing about it
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse({n for n in names if 'realism' in n})
        self.assertEqual(entry['evidence_hash'], digest({k: v for k, v in entry.items() if k != 'evidence_hash'}))

    def test_trading_result_is_identical_whether_or_not_jobs_are_enqueued(self):
        enqueued, plain = self.make(True), self.make(True)
        a = enqueued.actual_cycle()
        with patch.object(fr, 'enqueue', return_value=0):
            b = plain.actual_cycle()
        self.assertTrue(fr.store_path(enqueued.path).exists()); self.assertFalse(fr.store_path(plain.path).exists())
        keys = ('status', 'attempted_requests', 'investigation_attempted_requests', 'monitoring_attempted_requests', 'blockers')
        self.assertEqual({k: a[k] for k in keys}, {k: b[k] for k in keys})
        fields = ('type', 'side', 'reason', 'amount_sol', 'quantity', 'fee_sol', 'estimated_cost_fraction', 'simulation', 'execution_status')
        fills = lambda r: [{k: x.get(k) for k in fields} for x in r['outcomes'] if x.get('type') == 'fill']
        self.assertTrue(fills(a)); self.assertEqual(fills(a), fills(b))
        state = lambda f: {m: {k: p[k] for k in ('qty', 'cost_left', 'initial_cost', 'stage')} for m, p in cycle._state(f.path, f.cfg)['positions'].items()}
        self.assertEqual(list(state(enqueued).values()), list(state(plain).values()))
        with contextlib.closing(sqlite3.connect(enqueued.path)) as x, contextlib.closing(sqlite3.connect(plain.path)) as y:
            for table in ('events', 'outcomes'):
                self.assertEqual(x.execute(f'SELECT COUNT(*) FROM {table}').fetchone(), y.execute(f'SELECT COUNT(*) FROM {table}').fetchone())

    def test_jobs_are_enqueued_only_after_the_pass_outcome_is_durable(self):
        on = self.make(True)
        seen = []
        real = fr.enqueue
        def spy(ledger_path, jobs, cfg_hash, **kw):
            with contextlib.closing(on.f.progress.store.connect()) as c:
                seen.append(c.execute('SELECT outcome_hash IS NOT NULL FROM paper_observation_passes').fetchall())
            return real(ledger_path, jobs, cfg_hash, **kw)
        with patch.object(fr, 'enqueue', spy):
            result = on.actual_cycle()
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(len(seen), 1)
        self.assertEqual(set(seen[0]), {(1,)})        # every pass already had its outcome when the job was enqueued

    def test_default_off_leaves_no_trace(self):
        off = self.make(False)
        before = set(os.listdir(Path(off.path).parent))
        result = off.actual_cycle()
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertFalse(fr.store_path(off.path).exists())
        self.assertFalse([n for n in os.listdir(Path(off.path).parent) if 'realism' in n])
        self.assertNotIn('fill_realism', result)

    def test_enqueue_failure_never_reaches_the_pass_and_the_gate_stays_clear(self):
        for failure in (sqlite3.OperationalError('disk I/O error'), OSError('read-only file system'), MemoryError('boom')):
            with self.subTest(failure=type(failure).__name__):
                on = self.make(True)
                with patch.object(fr, 'connect', side_effect=failure):
                    result = on.actual_cycle()
                self.assertEqual(result['status'], 'COMPLETE', result)
                self.assertIn('evidence_hash', result)
                self.assertFalse(fr.store_path(on.path).exists())
                with contextlib.closing(on.f.progress.store.connect()) as c:
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()[0], 0)
                self.assertIsNone(terminal.gate(on.f.progress.store, on.f.jobs.path, ()))

    def test_worker_crash_leaves_the_trading_stores_byte_identical(self):
        on = self.make(True)
        on.actual_cycle()
        store = fr.store_path(on.path)
        trading = (sha(on.path), sha(on.f.progress.store.path), sha(on.f.jobs.path))
        def kill(*a, **k):
            raise KeyboardInterrupt
        clock = Clock(self.store_rows(on, fr.JOB_TABLE)[0]['decision_at'])
        with self.assertRaises(KeyboardInterrupt):
            worker.run(store, on.cfg, client=kill, clock=clock, sleep=clock.sleep)
        self.assertEqual((sha(on.path), sha(on.f.progress.store.path), sha(on.f.jobs.path)), trading)
        self.assertIsNone(terminal.gate(on.f.progress.store, on.f.jobs.path, ()))
        with contextlib.closing(on.f.progress.store.connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()[0], 0)

    def test_held_exits_are_enqueued_as_held_and_measurement_charges_nothing(self):
        on = self.make(True, monitoring=True)
        off = self.make(False, monitoring=True)
        entry, entry_off = on.actual_cycle(), off.actual_cycle()
        item, refs = self.held_item(on), tuple(entry['usd_evidence_refs'])
        item_off, refs_off = self.held_item(off), tuple(entry_off['usd_evidence_refs'])
        mark = on.actual_cycle(positions=(item,), candidates=(), usd_refs=refs, monitoring=True)
        self.assertEqual(mark['status'], 'COMPLETE', mark)
        self.assertEqual(len(self.store_rows(on, fr.JOB_TABLE)), 1)                     # a mark pass has no fill: no job
        on.sell_output = off.sell_output = 30_000_000                                   # partial take-profit exit
        partial = on.actual_cycle(positions=(item,), candidates=(), monitoring=True)
        partial_off = off.actual_cycle(positions=(item_off,), candidates=(), monitoring=True)
        self.assertEqual((partial['status'], partial_off['status']), ('COMPLETE', 'COMPLETE'))
        for key in ('attempted_requests', 'investigation_attempted_requests', 'monitoring_attempted_requests'):
            self.assertEqual(partial[key], partial_off[key], key)                       # measurement added no charged request
        jobs = self.store_rows(on, fr.JOB_TABLE)
        self.assertEqual([(j['side'], j['held']) for j in jobs], [('buy', 0), ('sell', 1)])
        sell = next(x for x in partial['outcomes'] if x.get('side') == 'sell')
        self.assertEqual(json.loads(jobs[1]['record_json']), sell['quote_execution'])
        self.assertEqual(self.store_rows(on, fr.SAMPLE_TABLE), [])
        self.assertIn(on.target.mint, cycle._state(on.path, on.cfg)['positions'])      # still open after the partial exit


if __name__ == '__main__':
    unittest.main()
