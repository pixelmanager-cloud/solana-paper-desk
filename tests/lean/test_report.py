"""L06: lean report. Fixtures only; the store layouts below follow the documented read contract in lean/report.py."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lean import report

NOW = 1_800_000_000.0


class Store:
    """Minimal generic layout: records(kind,payload,strategy_version,code_version,at)."""
    def __init__(self, root, name='lean.sqlite', wal=False):
        self.path = Path(root) / name
        self.c = sqlite3.connect(self.path)
        if wal:
            self.c.execute('PRAGMA journal_mode=WAL')
        self.c.execute('CREATE TABLE records(id INTEGER PRIMARY KEY,kind TEXT,payload TEXT,strategy_version TEXT,code_version TEXT,at REAL)')
        self.t = NOW

    def add(self, kind, strategy='s1', **payload):
        self.t += 1
        self.c.execute('INSERT INTO records(kind,payload,strategy_version,code_version,at) VALUES(?,?,?,?,?)',
                       (kind, json.dumps(payload), strategy, 'c1', payload.pop('at', self.t)))

    def raw(self, kind, text):
        self.c.execute('INSERT INTO records(kind,payload,strategy_version,code_version,at) VALUES(?,?,?,?,?)', (kind, text, 's1', 'c1', NOW))

    def trade(self, mint, buy_sol, sell_sol, reason='TP', strategy='s1', qty=100, hold=600, fee=0.0001, sells=None):
        t0 = self.t + 1
        self.add('fill', strategy, mint=mint, side='buy', qty=qty, sol=buy_sol, fee_sol=fee, at=t0)
        for i, (q, sol, why) in enumerate(sells or [(qty, sell_sol, reason)]):
            self.add('fill', strategy, mint=mint, side='sell', qty=q, sol=sol, fee_sol=fee, reason=why, at=t0 + hold + i)
        self.t = max(self.t, t0 + hold + len(sells or [0]))

    def done(self):
        self.c.commit()
        self.c.close()
        return str(self.path)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def store(self, **kw):
        return Store(self.root, **kw)


class Funnel(Base):
    def test_funnel_counts_and_rejection_reasons(self):
        s = self.store()
        for m in 'abcde':
            s.add('candidate', mint=m)
        s.add('screen', mint='a', passed=True, reasons=[])
        s.add('screen', mint='b', passed=False, reasons=['ACTIVE_MINT_AUTHORITY', 'LOW_LIQUIDITY'])
        s.add('screen', mint='c', passed=False, reasons=['LOW_LIQUIDITY'])
        s.add('screen', mint='d', passed=False, reasons=[])
        s.trade('a', 0.02, 0.03)
        d = report.build(s.done(), now=NOW)['funnel']['data']
        self.assertEqual((d['candidates'], d['screened'], d['passed'], d['entered']), (5, 4, 1, 1))
        self.assertEqual(d['rejection_reasons'], {'LOW_LIQUIDITY': 2, 'ACTIVE_MINT_AUTHORITY': 1, 'UNSPECIFIED': 1})

    def test_rescreen_uses_latest_screen_per_mint(self):
        s = self.store()
        s.add('screen', mint='a', passed=False, reasons=['X'])
        s.add('screen', mint='a', passed=True, reasons=[])
        d = report.build(s.done(), now=NOW)['funnel']['data']
        self.assertEqual((d['passed'], d['rejection_reasons']), (1, {}))

    def test_candidate_without_screen_is_not_screened(self):
        s = self.store()
        s.add('candidate', mint='a')
        d = report.build(s.done(), now=NOW)['funnel']['data']
        self.assertEqual((d['candidates'], d['screened']), (1, 0))


class Pnl(Base):
    def test_closed_trades_pnl_return_hold_and_exit_reasons(self):
        s = self.store()
        s.trade('a', 0.02, 0.03, 'TP', hold=100)
        s.trade('b', 0.02, 0.015, 'STOP', hold=300)
        r = report.build(s.done(), now=NOW)
        rows = {x['mint']: x for x in r['trades']['data']['rows']}
        self.assertAlmostEqual(rows['a']['pnl_sol'], 0.03 - 0.0001 - 0.02 - 0.0001, 9)
        self.assertAlmostEqual(rows['a']['return'], rows['a']['pnl_sol'] / 0.0201, 5)
        self.assertEqual((rows['a']['hold_seconds'], rows['b']['hold_seconds']), (100.0, 300.0))
        self.assertEqual(set(r['exit_reasons']['data']), {'TP', 'STOP'})
        self.assertEqual(r['trades']['data']['wins'], 1)
        self.assertEqual(r['trades']['data']['sample'], 'INSUFFICIENT_SAMPLE')

    def test_partial_exits_ladder_then_final(self):
        s = self.store()
        s.trade('a', 0.02, None, sells=[(40, 0.012, 'TP1'), (60, 0.03, 'TRAIL')])
        row = report.build(s.done(), now=NOW)['trades']['data']['rows'][0]
        self.assertEqual((row['status'], row['final_exit_reason'], row['exit_reasons']), ('CLOSED', 'TRAIL', ['TP1', 'TRAIL']))
        self.assertAlmostEqual(row['proceeds_sol'], 0.012 + 0.03 - 0.0002, 9)

    def test_open_position_is_listed_not_scored(self):
        s = self.store()
        s.add('fill', mint='a', side='buy', qty=100, sol=0.02, fee_sol=0.0001)
        s.add('fill', mint='a', side='sell', qty=30, sol=0.01, fee_sol=0.0001, reason='TP1')
        d = report.build(s.done(), now=NOW)['trades']['data']
        self.assertEqual((d['open'], d['closed_scored'], d['overall_pnl_sol']), (1, 0, 0.0))
        self.assertIsNone(d['rows'][0]['pnl_sol'])

    def test_reentry_after_close_is_a_second_trade(self):
        s = self.store()
        s.trade('a', 0.02, 0.03)
        s.trade('a', 0.02, 0.01, 'STOP')
        self.assertEqual(report.build(s.done(), now=NOW)['trades']['data']['closed_scored'], 2)

    def test_sell_exceeding_position_is_flagged_and_excluded(self):
        s = self.store()
        s.trade('a', 0.02, None, sells=[(150, 0.5, 'TP')])
        d = report.build(s.done(), now=NOW)['trades']['data']
        self.assertEqual((d['closed_scored'], d['flagged']), (0, 1))
        self.assertIn('SELL_EXCEEDS_POSITION', [a['code'] for a in d['anomalies']])

    def test_fills_are_ordered_by_time_not_by_row_order(self):
        s = self.store()
        s.add('fill', mint='a', side='sell', qty=10, sol=0.03, fee_sol=0, reason='TP', at=NOW + 50)
        s.add('fill', mint='a', side='buy', qty=10, sol=0.02, fee_sol=0, at=NOW + 10)
        d = report.build(s.done(), now=NOW)['trades']['data']
        self.assertEqual((d['closed_scored'], d['anomaly_count']), (1, 0))

    def test_buy_and_sell_with_the_same_timestamp_pair_up(self):
        s = self.store()
        s.add('fill', mint='a', side='sell', qty=10, sol=0.03, fee_sol=0, reason='TP', at=NOW + 5)
        s.add('fill', mint='a', side='buy', qty=10, sol=0.02, fee_sol=0, at=NOW + 5)
        self.assertEqual(report.build(s.done(), now=NOW)['trades']['data']['closed_scored'], 1)

    def test_sell_without_position_is_an_anomaly(self):
        s = self.store()
        s.add('fill', mint='a', side='sell', qty=1, sol=0.1, fee_sol=0)
        d = report.build(s.done(), now=NOW)['trades']['data']
        self.assertEqual([a['code'] for a in d['anomalies']], ['SELL_WITHOUT_POSITION'])

    def test_per_strategy_version_and_sample_flag_at_30(self):
        s = self.store()
        for i in range(30):
            s.trade('m%d' % i, 0.02, 0.025, strategy='v2')
        for i in range(5):
            s.trade('n%d' % i, 0.02, 0.015, 'STOP', strategy='v1')
        d = report.build(s.done(), now=NOW)['pnl_by_strategy']['data']
        self.assertEqual((d['v2']['sample'], d['v1']['sample']), ('OK', 'INSUFFICIENT_SAMPLE'))
        self.assertEqual((d['v2']['trades'], d['v2']['wins'], d['v1']['wins']), (30, 30, 0))

    def test_sample_boundary_is_exactly_thirty(self):
        s = self.store()
        for i in range(29):
            s.trade('m%d' % i, 0.02, 0.025)
        self.assertEqual(report.build(s.done(), now=NOW)['trades']['data']['sample'], 'INSUFFICIENT_SAMPLE')
        s = Store(self.root, 'b.sqlite')
        for i in range(30):
            s.trade('m%d' % i, 0.02, 0.025)
        self.assertEqual(report.build(s.done(), now=NOW)['trades']['data']['sample'], 'OK')

    def test_unreadable_fills_never_enter_the_totals(self):
        s = self.store()
        s.trade('a', 0.02, 0.03)
        for bad in ({'mint': 'b', 'side': 'buy', 'qty': 'x', 'sol': 1}, {'mint': 'b', 'side': 'hold', 'qty': 1, 'sol': 1},
                    {'mint': 'b', 'side': 'buy', 'qty': 1, 'sol': -1}, {'mint': 'b', 'side': 'buy', 'qty': 0, 'sol': 1},
                    {'mint': 'b', 'side': 'buy', 'qty': 1, 'sol': float('nan')}, {'side': 'buy', 'qty': 1, 'sol': 1}):
            s.add('fill', **bad)
        d = report.build(s.done(), now=NOW)['trades']['data']
        self.assertEqual(d['closed_scored'], 1)
        self.assertEqual(d['anomaly_count'], 6)


class Robustness(Base):
    def test_corrupt_rows_are_counted_and_do_not_stop_the_report(self):
        s = self.store()
        s.trade('a', 0.02, 0.03)
        s.raw('fill', 'not json')
        s.raw('fill', '[1,2]')
        s.raw('mystery', '{}')
        r = report.build(s.done(), now=NOW)
        self.assertEqual(r['trades']['data']['closed_scored'], 1)
        self.assertEqual(sum(r['skipped'].values()), 3)

    def test_html_escapes_hostile_strings(self):
        s = self.store()
        s.add('screen', mint='<script>alert(1)</script>', passed=False, reasons=['<img src=x onerror=1>'])
        s.trade('"><b>m', 0.02, 0.03, reason='<i>TP</i>', strategy='<u>v')
        text = report.render_html(report.build(s.done(), now=NOW))
        for raw in ('<script>', '<img', '<b>m', '<i>TP', '<u>v'):
            self.assertNotIn(raw, text)

    def test_html_is_self_contained(self):
        s = self.store()
        text = report.render_html(report.build(s.done(), now=NOW))
        for needle in ('http://', 'https://', '<script', '<link', 'src='):
            self.assertNotIn(needle, text)
        self.assertIn('EXECUTION_UNVERIFIED', text)
        self.assertIn('NOT_ASSESSED', text)

    def test_empty_store_reports_zeros(self):
        r = report.build(self.store().done(), now=NOW)
        self.assertEqual(r['funnel']['data']['candidates'], 0)
        self.assertEqual(r['trades']['data']['closed_scored'], 0)

    def test_store_without_known_tables_is_an_empty_report_not_a_crash(self):
        path = self.root / 'x.sqlite'
        sqlite3.connect(path).executescript('CREATE TABLE other(a)')
        self.assertEqual(report.build(path, now=NOW)['events'], 0)

    def test_json_is_strict(self):
        s = self.store()
        s.trade('a', 0.02, 0.03)
        json.dumps(report.build(s.done(), now=NOW), allow_nan=False)

    def test_typed_table_layout_with_payload_and_with_plain_columns(self):
        path = self.root / 't.sqlite'
        c = sqlite3.connect(path)
        c.execute('CREATE TABLE candidates(id INTEGER PRIMARY KEY,mint TEXT,strategy_version TEXT,at REAL)')
        c.execute('CREATE TABLE fills(id INTEGER PRIMARY KEY,payload TEXT,strategy_version TEXT,at REAL)')
        c.execute("INSERT INTO candidates(mint,at) VALUES('a',1)")
        for i, (side, q, sol) in enumerate((('buy', 10, 0.02), ('sell', 10, 0.03))):
            c.execute('INSERT INTO fills(payload,strategy_version,at) VALUES(?,?,?)', (json.dumps({'mint': 'a', 'side': side, 'qty': q, 'sol': sol, 'fee_sol': 0, 'reason': 'TP'}), 'v1', 10 + i))
        c.commit(); c.close()
        r = report.build(path, now=NOW)
        self.assertEqual((r['funnel']['data']['candidates'], r['trades']['data']['closed_scored']), (1, 1))
        self.assertEqual(list(r['pnl_by_strategy']['data']), ['v1'])


class ReadOnly(Base):
    def digest(self, path):
        return [(p.name, p.read_bytes()) for p in sorted(path.parent.iterdir())]

    def test_never_modifies_or_adds_files_beside_the_store(self):
        for wal in (False, True):
            with self.subTest(wal=wal):
                root = self.root / ('w' if wal else 'r')
                root.mkdir()
                s = Store(root, wal=wal)
                s.trade('a', 0.02, 0.03)
                path = Path(s.done())
                before = self.digest(path)
                report.build(path, now=NOW)
                after = self.digest(path)
                if wal:
                    # L09b D10: plain mode=ro (never immutable). On an idle WAL store SQLite may create the empty -wal
                    # and the -shm index (as the running trader's own connection does); the database file is untouched.
                    extra = {name: data for name, data in after if name not in dict(before)}
                    self.assertLessEqual(set(extra), {path.name + '-wal', path.name + '-shm'})
                    self.assertEqual(extra.get(path.name + '-wal', b''), b'')
                    after = [(name, data) for name, data in after if name not in extra]
                self.assertEqual(after, before)

    def test_write_attempt_through_the_report_connection_fails(self):
        s = self.store()
        s.trade('a', 0.02, 0.03)
        c = report.open_ro(s.done())
        with self.assertRaises(sqlite3.OperationalError):
            c.execute("INSERT INTO records(kind) VALUES('x')")

    def test_symlink_or_missing_store_is_refused(self):
        real = Path(self.store().done())
        link = self.root / 'link.sqlite'
        os.symlink(real, link)
        for bad in (link, self.root / 'nope.sqlite', self.root):
            with self.assertRaises(report.ReportError):
                report.open_ro(bad)


class Counterfactual(Base):
    def cf(self, rows):
        path = self.root / 'cf.sqlite'
        c = sqlite3.connect(path)
        c.execute('CREATE TABLE samples(mint TEXT,horizon INTEGER,status TEXT,price TEXT,PRIMARY KEY(mint,horizon))')
        c.executemany('INSERT INTO samples VALUES(?,?,?,?)', rows)
        c.commit(); c.close()
        return str(path)

    def build(self, rows, **kw):
        s = self.store()
        s.add('screen', mint='up', passed=False, reasons=['R'])
        s.add('screen', mint='down', passed=False, reasons=['R'])
        s.add('screen', mint='dead', passed=False, reasons=['R'])
        s.add('screen', mint='nobase', passed=False, reasons=['R'])
        s.add('screen', mint='nohorizon', passed=False, reasons=['R'])
        s.add('screen', mint='kept', passed=True, reasons=[])
        return report.build(s.done(), counterfactual_db=self.cf(rows), now=NOW, **kw)['counterfactual']

    def test_forward_returns_use_the_5m_baseline_and_dead_pools_are_minus_100(self):
        c = self.build([('up', 300, 'OK', '1'), ('up', 3600, 'OK', '2'), ('down', 300, 'OK', '2'), ('down', 3600, 'OK', '1'),
                        ('dead', 300, 'OK', '1'), ('dead', 3600, 'POOL_DEAD', None),
                        ('nobase', 900, 'OK', '1'), ('nobase', 3600, 'OK', '5'),
                        ('nohorizon', 300, 'OK', '1'), ('kept', 300, 'OK', '1'), ('kept', 3600, 'OK', '9')])
        g = c['data']['groups']['R']
        self.assertEqual((g['rejected'], g['with_return'], g['baseline_missing'], g['horizon_missing'], g['pool_dead']), (5, 3, 1, 1, 1))
        self.assertAlmostEqual(g['return']['mean'], (1 - 0.5 - 1) / 3, 5)
        self.assertAlmostEqual(g['share_positive'], 1 / 3, 5)
        self.assertEqual(g['sample'], 'INSUFFICIENT_SAMPLE')

    def test_unusable_baselines_are_counted_never_rebaselined(self):
        c = self.build([('up', 300, 'POOL_DEAD', None), ('up', 3600, 'OK', '2'), ('down', 300, 'OK', '0'), ('down', 3600, 'OK', '1'),
                        ('dead', 300, 'ERROR', '1'), ('dead', 3600, 'OK', '3')])
        g = c['data']['groups']['R']
        self.assertEqual((g['with_return'], g['baseline_missing']), (0, 5))

    def test_passed_mints_are_not_counterfactual_rejections(self):
        c = self.build([('kept', 300, 'OK', '1'), ('kept', 3600, 'OK', '9')])
        self.assertEqual(c['data']['groups']['R']['rejected'], 5)

    def test_unsampled_horizon_is_an_error_section_not_a_crash(self):
        c = self.build([], horizon=1234)
        self.assertEqual(c['status'], 'ERROR')

    def test_missing_counterfactual_store_is_error_but_report_still_builds(self):
        s = self.store()
        s.trade('a', 0.02, 0.03)
        r = report.build(s.done(), counterfactual_db=self.root / 'missing.sqlite', now=NOW)
        self.assertEqual(r['counterfactual']['status'], 'ERROR')
        self.assertEqual(r['trades']['data']['closed_scored'], 1)

    def test_not_provided(self):
        self.assertEqual(report.build(self.store().done(), now=NOW)['counterfactual']['status'], 'NOT_PROVIDED')


class Output(Base):
    def test_cli_writes_html_and_json_exclusively_with_0600(self):
        s = self.store()
        s.trade('a', 0.02, 0.03)
        db = s.done()
        out = self.root / 'out'
        out.mkdir()
        self.assertEqual(report.main(['--db', db, '--out-dir', str(out), '--now', str(NOW)]), 0)
        files = sorted(out.iterdir())
        self.assertEqual([f.suffix for f in files], ['.html', '.json'])
        for f in files:
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(files[1].read_text())['kind'], 'lean_report_v1')
        self.assertEqual(report.main(['--db', db, '--out-dir', str(out), '--now', str(NOW)]), 2)   # same second: never overwritten
        self.assertEqual(len(list(out.iterdir())), 2)

    def test_missing_out_dir_and_bad_db_exit_2(self):
        db = self.store().done()
        self.assertEqual(report.main(['--db', db, '--out-dir', str(self.root / 'nope')]), 2)
        self.assertEqual(report.main(['--db', str(self.root / 'nope.sqlite'), '--out-dir', str(self.root)]), 2)


class RealLeanStore(unittest.TestCase):
    """L09: the report reads the real lean.store layout (integer lamport columns, JSON reasons, position_state)."""

    def setUp(self):
        import tempfile as _tempfile
        from lean import paper as P
        from lean.store import Store as LeanStore
        self.tmp = _tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(os.path.realpath(self.tmp.name)) / 'lean.sqlite'
        s = LeanStore(self.path, initial_cash_sol='10', code_version='c1', strategy_version='lean-1', clock=lambda: 1000.0)
        self.addCleanup(s.close)
        cfg = P.PaperConfig()
        for i, mint in enumerate(('A' * 32, 'B' * 32, 'C' * 32)):
            s.add_candidate(mint, ts=100.0 + i)
            s.add_decision('screen', 'PASS' if i < 2 else 'REJECT', mint=mint, reasons=[] if i < 2 else ['ACTIVE_MINT_AUTHORITY'], ts=101.0 + i)
        s.add_decision('entry', 'SKIP', mint='B' * 32, reasons=['COST_BUDGET'], ts=103.0)
        s.add_observation('quote:buy', b'\x00' * (1024 * 1024), mint='A' * 32, ts=104.0)    # a big BLOB the report must not load
        buy = P.buy(P.Quote('A' * 32, 'buy', 200_000_000, 10 ** 9, 6, 110.0), '0.2', cfg)
        open_id = s.add_fill(buy, state={'event': 'open', 'state': {'stage': 0}})
        (pos,) = s.positions().values()
        part = P.sell(pos, P.Quote('A' * 32, 'sell', 300_000_000, 90_000_000, 6, 200.0), cfg=cfg, qty_raw=300_000_000)
        s.add_fill(part, state={'event': 'rung', 'open_fill_id': open_id, 'state': {'stage': 1, 'last_reason': 'TAKE_PROFIT'}})
        (pos,) = s.positions().values()
        last = P.sell(pos, P.Quote('A' * 32, 'sell', pos.qty_raw, 120_000_000, 6, 400.0), cfg=cfg, qty_raw=pos.qty_raw)
        s.add_fill(last, state={'event': 'closed', 'open_fill_id': open_id, 'state': {'reason': 'TRAILING_STOP'}})
        s.add_error('HTTP_503', transient=True, mint='B' * 32, ts=120.0)
        # generic events of kinds the typed tables cover must not be counted twice
        s.record('fill', {'mint': 'A' * 32, 'side': 'buy', 'qty': 1, 'sol': 5}, code_version='c1', strategy_version='lean-1')
        s.record('screen', {'mint': 'Z' * 32, 'passed': True}, code_version='c1', strategy_version='lean-1')
        self.store = s

    def test_store_columns_reasons_exit_reasons_and_pnl(self):
        summary = report.build(self.path, now=NOW)
        funnel = summary['funnel']['data']
        self.assertEqual((funnel['candidates'], funnel['screened'], funnel['passed'], funnel['entered'], funnel['exited']), (3, 3, 2, 1, 1))
        self.assertEqual(funnel['rejection_reasons'], {'ACTIVE_MINT_AUTHORITY': 1})
        self.assertEqual(funnel['entry_decision_rejections'], {'COST_BUDGET': 1})
        trades = summary['trades']['data']
        self.assertEqual((trades['closed_scored'], trades['anomaly_count']), (1, 0))
        self.assertAlmostEqual(trades['overall_pnl_sol'], self.store.realized() / 1e9, places=6)
        self.assertEqual(trades['rows'][0]['exit_reasons'], ['TAKE_PROFIT', 'TRAILING_STOP'])
        self.assertEqual(list(summary['exit_reasons']['data']), ['TRAILING_STOP'])
        self.assertEqual(summary['errors']['data'], {'HTTP_503': 1})
        self.assertIn('exited', report.render_html(summary))

    def test_observation_blobs_are_never_selected(self):
        statements = []
        original = report.open_ro

        def traced(path):
            connection = original(path)
            connection.set_trace_callback(statements.append)
            return connection
        with mock.patch.object(report, 'open_ro', traced):
            report.build(self.path, now=NOW)
        observation_reads = [s for s in statements if 'observations' in s]
        self.assertTrue(observation_reads)
        import re
        self.assertTrue(all('raw' not in s and not re.search(r'SELECT\s+\*', s) for s in observation_reads), observation_reads)


if __name__ == '__main__':
    unittest.main()
