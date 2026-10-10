"""SYNTHETIC_TEST_ONLY: opt-in latency measurement of simulated paper fills.

Provider bytes are scripted fixtures (no provider access). Known-answer numbers are
hand computed in comments. Ledgers for the report math are built from the real
Ledger schema with explicit synthetic fill outcomes.
"""
import contextlib
import hashlib
import io
import json
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from desk import fill_realism as fr, paper_cycle as cycle, quote_execution as qe
from desk.ledger import Ledger
from desk.live_observation import ProviderObservation
from desk.model import canonical, digest
from desk.paper_read_sources import PaperReadError
from tests import test_paper_cycle as cycle_fixtures
from tests.helpers import T, config
from tools.research import fill_realism_report as report

BUY = {'input_raw': 100_000_000, 'estimated_output_raw': 1_000_000_000, 'simulated_output_raw': 980_000_000,
       'quote_hash': 'q-buy', 'mint_decimals': 6, 'direction': 'buy'}
SELL = {'input_raw': 1_000_000_000, 'estimated_output_raw': 125_000_000, 'simulated_output_raw': 120_050_000,
        'quote_hash': 'q-sell', 'mint_decimals': 6, 'direction': 'sell'}


def sample_row(event, side, delay, record, req_est, req_sim, *, mint='MINT', ts=T, status='MEASURED', code=None, charged=1):
    base = {'fill_event_id': event, 'side': side, 'delay_seconds': delay, 'mint': mint, 'decision_at': ts,
            'config_hash': 'cfg', 'status': status, 'code': code, 'charged': charged,
            'decision_input_raw': record['input_raw'], 'decision_estimated_out_raw': record['estimated_output_raw'],
            'decision_simulated_out_raw': record['simulated_output_raw'], 'decision_quote_hash': record['quote_hash']}
    if status == 'FAILED':
        return base
    body = {'delay': delay, 'est': req_est}
    values = fr.drift(side, (record['input_raw'], record['estimated_output_raw'], record['simulated_output_raw']),
                      req_est, req_sim, record['mint_decimals'])
    return {**base, 'requote_estimated_out_raw': req_est, 'requote_min_out_raw': req_est * 99 // 100,
            'requote_simulated_out_raw': req_sim, 'requote_hash': digest(body), 'requote_json': canonical(body),
            'measured_at': ts + delay, 'elapsed_seconds': str(delay), **values}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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
        for bad in (0, -1, True.__class__('1') if False else None):
            with self.assertRaises(qe.QuoteExecutionError):
                fr.drift('buy', (100_000_000, 1_000_000_000, 980_000_000), 950_000_000, bad or 0, 6)

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
        self.path = Path(self.tmp.name) / 'ledger.sqlite'
        ledger = Ledger(self.path); ledger.close()
        with contextlib.closing(sqlite3.connect(self.path)) as c, c:
            c.execute("INSERT INTO events(event_id,ts,payload,payload_hash) VALUES('e1',?, '{}', 'h')", (T,))
            c.execute("INSERT INTO events(event_id,ts,payload,payload_hash) VALUES('e2',?, '{}', 'h')", (T + 1,))

    def test_append_only_and_bound(self):
        self.assertTrue(fr._insert(self.path, sample_row('e1', 'buy', 2, BUY, 950_000_000, 931_000_000)))
        self.assertFalse(fr._insert(self.path, sample_row('e1', 'buy', 2, BUY, 900_000_000, 800_000_000)))  # one per identity
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute(f'SELECT requote_estimated_out_raw FROM {fr.TABLE}').fetchall(), [(950_000_000,)])
            for sql in (f'UPDATE {fr.TABLE} SET out_drift_bps=0', f'DELETE FROM {fr.TABLE}'):
                with self.assertRaises(sqlite3.IntegrityError):
                    c.execute(sql)
        with self.assertRaises(sqlite3.IntegrityError):  # missing event
            fr._insert(self.path, sample_row('nope', 'buy', 2, BUY, 950_000_000, 931_000_000))
        with self.assertRaises(sqlite3.IntegrityError):  # event exists but decision time differs
            fr._insert(self.path, sample_row('e1', 'buy', 5, BUY, 950_000_000, 931_000_000, ts=T + 99))

    def test_invalid_delay_or_label_refused(self):
        bad = sample_row('e1', 'buy', 3, BUY, 950_000_000, 931_000_000)
        with self.assertRaises(sqlite3.IntegrityError):
            fr._insert(self.path, bad)
        with sqlite3.connect(self.path) as c:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute(f"INSERT INTO {fr.TABLE}(fill_event_id,side,delay_seconds,mint,decision_at,config_hash,status,"
                          "code,charged,execution_status) VALUES('e1','buy',2,'M',?,'c','FAILED','X',0,'VERIFIED')", (T,))


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
        rows = [sample_row('buy-A', 'buy', 2, BUY, 950_000_000, 931_000_000, mint='A'),
                sample_row('sell-A', 'sell', 2, SELL, 112_500_000, 108_045_000, mint='A', ts=T + 600),
                sample_row('buy-A', 'buy', 5, BUY, 900_000_000, 882_000_000, mint='A'),
                sample_row('sell-A', 'sell', 5, SELL, 0, 0, mint='A', ts=T + 600, status='FAILED',
                           code='REQUOTE_INVALID'),
                sample_row('buy-B', 'buy', 2, BUY, 900_000_000, 882_000_000, mint='B', ts=T + 700)]
        for row in rows:
            fr._insert(self.path, row)
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def fill(self, c, event, mint, payload, ts):
        c.execute("INSERT INTO events(event_id,ts,payload,payload_hash) VALUES(?,?,'{}','h')", (event, ts))
        c.execute('INSERT INTO outcomes(event_id,payload) VALUES(?,?)', (event, canonical(payload)))

    def test_known_answers(self):
        r = report.report(self.path)
        self.assertEqual((r['status'], r['execution_status'], r['paper_only'], r['samples'], r['verified_samples']),
                         ('REPORTED', 'EXECUTION_UNVERIFIED', True, 5, 5))
        # buy 2s drifts: A -500, B -1000 -> mean -750, median -750, p10 -950, p90 -550
        stats = r['aggregate']['buy']['2']['out_drift_bps']
        self.assertEqual([Decimal(stats[k]) for k in ('mean', 'median', 'p10', 'p90', 'min', 'max')],
                         [Decimal(-750), Decimal(-750), Decimal(-950), Decimal(-550), Decimal(-1000), Decimal(-500)])
        self.assertEqual(r['aggregate']['sell']['5']['failed'], 1)
        self.assertEqual(r['aggregate']['sell']['5']['failure_codes'], {'REQUOTE_INVALID': 1})
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

    def tamper(self, sql):
        with contextlib.closing(sqlite3.connect(self.path)) as c, c:
            for trigger in ('no_update', 'no_delete'):
                c.execute(f'DROP TRIGGER IF EXISTS {fr.TABLE}_{trigger}')
            c.execute(sql)

    def test_tampered_drift_excluded_and_flagged(self):
        self.tamper(f"UPDATE {fr.TABLE} SET out_drift_bps='0' WHERE fill_event_id='buy-A' AND delay_seconds=2")
        r = report.report(self.path)
        self.assertEqual(r['verified_samples'], 4)
        self.assertEqual([e['code'] for e in r['integrity_errors']], ['DRIFT_MISMATCH'])
        self.assertIsNone([t for t in r['trades'] if t['mint'] == 'A'][0]['latency_adjusted_pnl_sol']['2'])

    def test_tampered_decision_binding_and_requote_hash_flagged(self):
        self.tamper(f"UPDATE {fr.TABLE} SET decision_quote_hash='forged' WHERE fill_event_id='sell-A' AND delay_seconds=2")
        self.tamper(f"UPDATE {fr.TABLE} SET requote_hash='forged' WHERE fill_event_id='buy-B'")
        codes = sorted(e['code'] for e in report.report(self.path)['integrity_errors'])
        self.assertEqual(codes, ['DECISION_BINDING_MISMATCH', 'REQUOTE_HASH_MISMATCH'])

    def test_read_only_and_symlink_refused(self):
        before = sha(self.path)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(report.main(['--ledger', str(self.path)]), 0)
        self.assertEqual(json.loads(out.getvalue())['status'], 'REPORTED')
        self.assertEqual(sha(self.path), before)
        link = Path(self.tmp.name) / 'link.sqlite'; link.symlink_to(self.path)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(report.main(['--ledger', str(link)]), 2)
            self.assertEqual(report.main(['--ledger', str(self.path) + '.none']), 2)

    def test_ledger_without_samples_reports_no_data(self):
        plain = Path(self.tmp.name) / 'plain.sqlite'
        Ledger(plain).close()
        self.assertEqual(report.report(plain)['status'], 'NO_REALISM_DATA')


class MeasureTests(unittest.TestCase):
    """measure() against the real charged _Budget/progress with a scripted quote source."""

    def setUp(self):
        self.q = cycle_fixtures.QuoteOnlyCycleTests('test_entry_reuses_existing_buy_and_only_reads_exact_reverse_once')
        self.q.setUp(); self.addCleanup(self.q.doCleanups)
        self.cfg = {**self.q.cfg, fr.KEY: 1}
        self.scan = self.q.target.scan_id
        self.path = Path(self.q.admissions.tmp.name) / 'measure-ledger.sqlite'
        Ledger(self.path).close()
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO events(event_id,ts,payload,payload_hash) VALUES('fill-1',?, '{}', 'h')", (T,))
        buy = self.q.f.buy
        self.record = {'direction': 'buy', 'input_raw': buy.input_raw, 'estimated_output_raw': buy.estimated_output_raw,
                       'simulated_output_raw': qe.output_raw(buy, self.cfg), 'quote_hash': buy.source.raw_hash,
                       'mint_decimals': self.q.token.decimals}
        self.script, self.calls, self.sleeps, self.now = [], [], [], [T]
        outer = self
        progress = self.q.admissions.progress
        class Source:
            quote_source_id = 'fixture:synthetic-quote'
            def quote(self, input_mint, output_mint, amount, taker, *, timeout_seconds):
                outer.assertTrue(progress.reserve(outer.scan))  # the durable per-attempt charge
                outer.calls.append((input_mint, output_mint, amount))
                step = outer.script.pop(0)
                if isinstance(step, Exception):
                    raise step
                return json.loads(outer.q.f.quote('buy', amount, step).source.original_json)
        self.source = Source()
        self.budget = cycle._Budget(progress, lambda: self.now[0], lambda: 1)

    def job(self):
        item = type('Collected', (), {})()
        item.target, item.mint = self.q.target, self.q.token
        return fr.collect([{'type': 'fill', 'side': 'buy', 'mint': self.q.f.mint, 'quote_execution': self.record}],
                          {'event_id': 'fill-1', 'ts': T}, item, self.source, False)

    def run_measure(self, **kwargs):
        with patch.object(fr, '_sleep', side_effect=lambda s: self.sleeps.append(s)):
            return fr.measure(self.job(), self.cfg, self.path, self.budget, None, **kwargs)

    def rows(self):
        with sqlite3.connect(self.path) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(f'SELECT * FROM {fr.TABLE} ORDER BY delay_seconds')]

    def used(self):
        return self.q.admissions.progress.admission(self.scan)['requests_used']

    def test_three_samples_charged_and_scheduled(self):
        used = self.used()
        self.script[:] = [990_000, 970_000, 940_000]
        summary = self.run_measure()
        self.assertEqual([s['status'] for s in summary], ['MEASURED'] * 3)
        self.assertEqual(self.sleeps, [2, 5, 10])
        self.assertEqual(self.used() - used, 3)
        self.assertEqual(self.budget.attempted, 3)
        rows = self.rows()
        self.assertEqual([r['delay_seconds'] for r in rows], [2, 5, 10])
        self.assertTrue(all(r['charged'] == 1 and r['execution_status'] == 'EXECUTION_UNVERIFIED' for r in rows))
        slip = int(Decimal(self.cfg['adverse_slippage_bps']))
        for row, out in zip(rows, (990_000, 970_000, 940_000)):
            sim = (out * 99 // 100) * (10000 - slip) // 10000
            self.assertEqual(row['requote_simulated_out_raw'], sim)
            expected = (Decimal(sim) / Decimal(self.record['simulated_output_raw']) - 1) * 10000
            self.assertEqual(round(Decimal(row['out_drift_bps']), 20), round(expected, 20))
        self.assertTrue(all(c[2] == self.record['input_raw'] for c in self.calls))

    def test_failed_attempt_is_charged_and_recorded(self):
        used = self.used()
        self.script[:] = [990_000, PaperReadError('SYNTHETIC_TRANSPORT_FAILURE'), 940_000]
        summary = self.run_measure()
        self.assertEqual([s['status'] for s in summary], ['MEASURED', 'FAILED', 'MEASURED'])
        self.assertEqual(self.used() - used, 3)
        failed = self.rows()[1]
        self.assertEqual((failed['code'], failed['charged']), ('SYNTHETIC_TRANSPORT_FAILURE', 1))

    def test_invalid_requote_is_failed_but_charged(self):
        used = self.used()
        self.script[:] = [990_000, 990_000, 990_000]
        with patch.object(fr, 'ingest_quote', side_effect=fr.ObservationError('bad')):
            summary = self.run_measure()
        self.assertEqual({s['code'] for s in summary}, {'REQUOTE_INVALID'})
        self.assertEqual(self.used() - used, 3)

    def test_exhausted_budget_stops_without_further_requests(self):
        progress = self.q.admissions.progress
        with progress.store.connect() as c:   # monitoring provisioned -> no reserve; real exhaustion path
            c.execute('CREATE TABLE paper_monitoring_budget(id INTEGER PRIMARY KEY)')
            c.execute('INSERT INTO paper_monitoring_budget VALUES(1)')
        while progress.reserve(self.scan):
            pass
        used = self.used()
        summary = self.run_measure()
        self.assertEqual([s['status'] for s in summary], ['FAILED'] * 3)
        self.assertEqual({s['code'] for s in summary}, {'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED'})
        self.assertEqual((self.calls, self.used(), [r['charged'] for r in self.rows()]), ([], used, [0, 0, 0]))

    def test_late_sample_is_skipped_without_a_request(self):
        self.now[0] = T + 100
        summary = self.run_measure()
        self.assertEqual({s['code'] for s in summary}, {'REALISM_SAMPLE_LATE'})
        self.assertEqual((self.calls, self.sleeps), ([], []))

    def test_clock_defect_refuses_unbounded_sleep(self):
        self.now[0] = T - 1000
        summary = self.run_measure()
        self.assertEqual({s['code'] for s in summary}, {'REALISM_CLOCK_UNAVAILABLE'})
        self.assertEqual((self.calls, self.sleeps), ([], []))

    def test_investigation_reserve_protects_later_exits(self):
        progress = self.q.admissions.progress
        ceiling = progress.admission(self.scan)['request_ceiling']
        while progress.admission(self.scan)['requests_used'] < ceiling - 9:
            self.assertTrue(progress.reserve(self.scan))
        self.script[:] = [990_000, 970_000, 940_000]
        used = self.used()
        summary = self.run_measure()   # ledger has no checkpoint -> position assumed open -> reserve of 8 reads
        self.assertEqual([(s['status'], s['code'], s['charged']) for s in summary],
                         [('MEASURED', None, 1), ('FAILED', 'REALISM_INVESTIGATION_RESERVE', 0),
                          ('FAILED', 'REALISM_INVESTIGATION_RESERVE', 0)])
        self.assertEqual((self.used(), ceiling - self.used()), (used + 1, 8))

    def test_provisioned_monitoring_removes_the_investigation_reserve(self):
        progress = self.q.admissions.progress
        ceiling = progress.admission(self.scan)['request_ceiling']
        while progress.admission(self.scan)['requests_used'] < ceiling - 4:
            self.assertTrue(progress.reserve(self.scan))
        with progress.store.connect() as c:
            c.execute('CREATE TABLE paper_monitoring_budget(id INTEGER PRIMARY KEY)')
            c.execute('INSERT INTO paper_monitoring_budget VALUES(1)')
        self.script[:] = [990_000, 970_000, 940_000]
        self.assertEqual([s['status'] for s in self.run_measure()], ['MEASURED'] * 3)

    def test_off_means_nothing_is_done(self):
        with patch.object(fr, '_sleep') as sleep:
            self.assertEqual(fr.measure(self.job(), {k: v for k, v in self.cfg.items() if k != fr.KEY},
                                        self.path, self.budget, None), [])
        sleep.assert_not_called()
        with sqlite3.connect(self.path) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (fr.TABLE,)).fetchone())

    def test_store_failure_never_raises_into_the_cycle(self):
        self.script[:] = [990_000, 990_000, 990_000]
        with sqlite3.connect(self.path) as c:   # unbound event id -> trigger abort on insert
            c.execute("DELETE FROM events WHERE event_id='fill-1'")
        summary = self.run_measure()
        self.assertEqual({s['code'] for s in summary[:1]}, {'REALISM_STORE_UNAVAILABLE'})
        self.assertEqual(len(self.calls), 1)   # latched after the first store failure: no further charged requests


class CycleIntegrationTests(unittest.TestCase):
    """Real entry/exit cycle harness (synthetic wire bytes) with and without the flag."""

    def make(self, flag, *, monitoring=False):
        fx = cycle_fixtures.PaperCycleTests('run_cycle')
        fx.setUp(); self.addCleanup(fx.doCleanups)
        fx.http_calls, fx.sell_output = [], 10_000_000
        if flag:
            fx.cfg = {**fx.cfg, fr.KEY: 1}
            fx.path = Path(fx.f.tmp.name) / 'realism-cycle.sqlite'
            cycle.initialize(fx.path, fx.cfg)
        if monitoring:
            self.allowance = cycle.MonitoringBudget(fx.f.progress.store, fx.path, fx.cfg, clock=lambda: fx.f.at)
            self.allowance.provision()  # explicit synthetic coordinator operation, never run_once
        return fx

    def sleeper(self, fx, outputs):
        calls = []
        def sleep(seconds):
            calls.append(seconds)
            fx.buy_output_raw = outputs[len(calls) - 1]
        return calls, sleep

    def held_item(self, fx):
        from dataclasses import replace
        p = cycle._state(fx.path, fx.cfg)['positions'][fx.target.mint]
        return replace(fx.item, target=replace(fx.target, amount_raw=qe.raw_quantity(p['qty'], 6)), graduation_refs=())

    def test_entry_measured_charged_and_fill_unchanged(self):
        off, on = self.make(False), self.make(True, monitoring=True)
        baseline = off.actual_cycle()
        calls, sleep = self.sleeper(on, [990_000, 970_000, 0])   # third response is invalid: failed but charged
        with patch.object(fr, '_sleep', side_effect=sleep):
            entry = on.actual_cycle()
        self.assertEqual((baseline['status'], entry['status']), ('COMPLETE', 'COMPLETE'))
        self.assertEqual((baseline['attempted_requests'], entry['attempted_requests']), (9, 12))
        self.assertNotIn('fill_realism', baseline)
        self.assertEqual([(s['status'], s['code'], s['charged']) for s in entry['fill_realism']],
                         [('MEASURED', None, 1), ('MEASURED', None, 1), ('FAILED', 'REQUOTE_INVALID', 1)])
        self.assertEqual(entry['budget'][on.target.scan_id]['used'], baseline['budget'][off.target.scan_id]['used'] + 3)
        self.assertEqual(calls, [2, 5, 10])
        buy = lambda r: next(x for x in r['outcomes'] if x.get('side') == 'buy')
        for key in ('amount_sol', 'quantity', 'fee_sol', 'estimated_cost_fraction', 'execution_status'):
            self.assertEqual(buy(entry)[key], buy(baseline)[key])
        self.assertEqual(buy(entry)['quote_execution']['simulated_output_raw'],
                         buy(baseline)['quote_execution']['simulated_output_raw'])
        r = report.report(on.path)   # bound to the fill and visible to the read-only report
        self.assertEqual((r['verified_samples'], r['integrity_errors']), (3, []))
        self.assertEqual(r['aggregate']['buy']['2']['measured'], 1)
        self.assertEqual(r['aggregate']['buy']['10']['failure_codes'], {'REQUOTE_INVALID': 1})
        self.assertIn(on.target.mint, cycle._state(on.path, on.cfg)['positions'])

    def test_legacy_held_exit_is_never_starved_by_entry_measurement(self):
        on = self.make(True)   # monitoring NOT provisioned: held reads share the 18-read admission
        with patch.object(fr, '_sleep', side_effect=lambda s: None):
            entry = on.actual_cycle()
            self.assertEqual([(s['status'], s['code'], s['charged']) for s in entry['fill_realism']],
                             [('MEASURED', None, 1), ('FAILED', 'REALISM_INVESTIGATION_RESERVE', 0),
                              ('FAILED', 'REALISM_INVESTIGATION_RESERVE', 0)])
            item, refs = self.held_item(on), tuple(entry['usd_evidence_refs'])
            self.assertEqual(on.actual_cycle(positions=(item,), candidates=(), usd_refs=refs)['status'], 'COMPLETE')
            on.sell_output = 7_000_000
            exit_result = on.actual_cycle(positions=(item,), candidates=(), usd_refs=refs)
        self.assertEqual(exit_result['status'], 'COMPLETE', exit_result)
        self.assertTrue(any(x.get('side') == 'sell' for x in exit_result['outcomes']))
        self.assertEqual(exit_result['budget'][on.target.scan_id]['used'], 18)
        self.assertEqual({s['code'] for s in exit_result['fill_realism']}, {'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED'})
        self.assertEqual(cycle._state(on.path, on.cfg)['positions'], {})

    def test_default_off_leaves_no_trace(self):
        off = self.make(False)
        with patch.object(fr, '_sleep') as sleep:
            entry = off.actual_cycle()
        sleep.assert_not_called()
        self.assertEqual(entry['attempted_requests'], 9)
        self.assertNotIn('fill_realism', entry)
        with sqlite3.connect(off.path) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (fr.TABLE,)).fetchone())

    def test_full_exit_samples_charge_investigation_because_monitoring_needs_an_open_position(self):
        on = self.make(True, monitoring=True)
        with patch.object(fr, '_sleep', side_effect=lambda s: None):
            entry = on.actual_cycle()
            item, refs = self.held_item(on), tuple(entry['usd_evidence_refs'])
            mark = on.actual_cycle(positions=(item,), candidates=(), usd_refs=refs, monitoring=True)
            self.assertEqual(mark['status'], 'COMPLETE', mark)
            self.assertNotIn('fill_realism', mark)     # a mark pass has no fill: nothing measured
            used = on.f.progress.admission(on.target.scan_id)['requests_used']
            total = self.allowance.snapshot()['total_used']
            on.sell_output = 7_000_000
            exit_result = on.actual_cycle(positions=(item,), candidates=(), usd_refs=refs, monitoring=True)
        self.assertEqual(exit_result['status'], 'COMPLETE', exit_result)
        self.assertEqual([(s['side'], s['status'], s['charged']) for s in exit_result['fill_realism']], [('sell', 'MEASURED', 1)] * 3)
        self.assertEqual(exit_result['investigation_attempted_requests'], 3)
        self.assertEqual(on.f.progress.admission(on.target.scan_id)['requests_used'] - used, 3)
        self.assertEqual(self.allowance.snapshot()['total_used'] - total, exit_result['monitoring_attempted_requests'])
        r = report.report(on.path)
        trade = r['trades'][0]
        self.assertEqual((trade['status'], r['aggregate']['sell']['2']['measured']), ('CLOSED', 1))
        # Re-quote bytes equal the decision bytes in this fixture -> latency-adjusted PnL equals the paper PnL.
        self.assertEqual(Decimal(trade['latency_adjusted_pnl_sol']['2']), Decimal(trade['paper_pnl_sol']))

    def test_partial_exit_samples_charge_the_monitoring_allowance(self):
        on = self.make(True, monitoring=True)
        with patch.object(fr, '_sleep', side_effect=lambda s: None):
            on.actual_cycle()
            item = self.held_item(on)
            used = on.f.progress.admission(on.target.scan_id)['requests_used']
            total = self.allowance.snapshot()['total_used']
            on.sell_output = 30_000_000
            partial = on.actual_cycle(positions=(item,), candidates=(), monitoring=True)
        self.assertEqual(partial['status'], 'COMPLETE', partial)
        self.assertEqual([(s['status'], s['charged']) for s in partial['fill_realism']], [('MEASURED', 1)] * 3)
        self.assertEqual((partial['monitoring_attempted_requests'], partial['investigation_attempted_requests']), (5 + 3, 0))
        self.assertEqual(on.f.progress.admission(on.target.scan_id)['requests_used'], used)
        self.assertEqual(self.allowance.snapshot()['total_used'] - total, 8)
        self.assertIn(on.target.mint, cycle._state(on.path, on.cfg)['positions'])   # still open
        self.assertEqual(report.report(on.path)['trades'][0]['status'], 'OPEN_OR_PARTIAL')


if __name__ == '__main__':
    unittest.main()
