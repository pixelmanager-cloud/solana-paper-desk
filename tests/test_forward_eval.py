"""tools.research.forward_eval: synthetic ledgers with known answers (no network, no providers)."""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from desk.engine import initial_state, transition
from desk.ledger import Ledger
from tests.helpers import T, config, event
from tools.research import forward_eval as F

DAY = 100000
# (exit-event overrides, exit delay seconds) -> DANGER loss, STOP loss, DANGER win, TIME_STOP loss
SCRIPT = [(dict(danger=True), 3000), (dict(reserve_sol='40'), 3000),
          (dict(reserve_sol='160', danger=True), 3000), ({}, 3000)]


def build(path, cfg=None, script=SCRIPT, start=T, close_last=True):
    """Apply the script; return per-trade (cost, pnl, reason, hold, closed_at) taken from raw outcomes."""
    cfg = cfg or config()
    ledger = Ledger(path)
    expected, t = [], start
    for i, (changes, delay) in enumerate(script):
        mint = 'SYNTHETIC_%d' % i
        buy = [o for o in ledger.apply(event(t, mint=mint, graduated_at=t - 600), cfg, transition, initial_state)
               if o['type'] == 'fill'][0]
        if not close_last and i == len(script) - 1:
            break
        sell = [o for o in ledger.apply(event(t + delay, mint=mint, graduated_at=t - 600, **changes),
                                        cfg, transition, initial_state) if o['type'] == 'fill'][0]
        expected.append({'cost': D(buy['amount_sol']) + D(buy['fee_sol']), 'pnl': D(sell['realized_pnl_sol']),
                         'reason': sell['reason'], 'hold': delay, 'closed_at': t + delay})
        t += DAY
    ledger.close()
    return expected


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = F.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ForwardEvalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def ledger(self, name='a.sqlite', **kw):
        path = self.dir / name
        return path, build(path, **kw)

    def test_known_answers_single_experiment(self):
        path, exp = self.ledger()
        report = F.evaluate([path], now=T + 10 * DAY)
        self.assertEqual(report['execution_status'], 'EXECUTION_UNVERIFIED')
        self.assertTrue(report['paper_only'])
        self.assertEqual(report['profitability_verdict'], 'NOT_ASSESSED')
        (row,) = report['experiments']
        self.assertEqual(row['execution_status'], 'EXECUTION_UNVERIFIED')
        a = row['all']
        returns = sorted(t['pnl'] / t['cost'] for t in exp)
        self.assertEqual(a['closed_trades'], 4)
        self.assertEqual((a['wins'], a['losses'], a['flat']), (1, 3, 0))
        self.assertEqual(D(a['net_pnl_sol']), sum(t['pnl'] for t in exp))
        self.assertAlmostEqual(D(a['mean_return']), sum(returns) / 4, places=25)
        self.assertAlmostEqual(D(a['median_return']), (returns[1] + returns[2]) / 2, places=25)
        self.assertEqual(a['exit_reasons'], {'DANGER': 2, 'STOP': 1, 'TIME_STOP': 1})
        self.assertEqual(D(a['mean_hold_seconds']), D(3000))
        self.assertIn('INSUFFICIENT_SAMPLE', a['warning'])
        self.assertFalse(a['mfe_mae']['available'])
        # drawdown from the closed-trade equity curve, computed independently
        equity = peak = D(config()['initial_equity_sol']); worst = D(0)
        for t in exp:
            equity += t['pnl']; peak = max(peak, equity); worst = max(worst, (peak - equity) / peak)
        self.assertAlmostEqual(D(a['max_drawdown_fraction']), worst, places=25)
        self.assertGreater(worst, 0)

    def test_partial_take_profit_then_final_exit_is_one_trade_with_final_reason(self):
        path = self.dir / 'tp.sqlite'
        cfg, ledger = config(), Ledger(path)
        apply = lambda e: ledger.apply(e, cfg, transition, initial_state)
        buy = apply(event(T))[0]
        fills = [o for e in (event(T + 5, reserve_sol='160'), event(T + 10, reserve_sol='160', danger=True))
                 for o in apply(e) if o['type'] == 'fill']
        ledger.close()
        self.assertEqual([f['reason'] for f in fills], ['TAKE_PROFIT', 'DANGER'])
        (row,) = F.evaluate([path], now=T + 10)['experiments']
        a = row['all']
        self.assertEqual(a['closed_trades'], 1)
        self.assertEqual(a['exit_reasons'], {'DANGER': 1})
        self.assertEqual(a['partial_take_profit_fills'], 1)
        self.assertEqual(D(a['mean_hold_seconds']), 10)
        cost = D(buy['amount_sol']) + D(buy['fee_sol'])
        self.assertAlmostEqual(D(a['mean_return']), sum(D(f['realized_pnl_sol']) for f in fills) / cost, places=25)

    def test_flat_trade_counts_as_neither_win_nor_loss(self):
        trades = [dict(pnl=D(0), cost=D(1), **{'return': D(0)}, hold_seconds=5, exit_reason='TIME_STOP',
                       reasons=['TIME_STOP'], closed_seq=1, closed_at=1)]
        a = F.block(trades, D(5), 50, 0)
        self.assertEqual((a['wins'], a['losses'], a['flat']), (0, 0, 1))

    def test_insufficient_sample_is_printed_not_hidden(self):
        path, _ = self.ledger()
        code, out, err = run_main('--ledger', str(path), '--now', str(T + 10 * DAY))
        self.assertEqual(code, 0)
        self.assertIn('WARNING', err)
        self.assertIn('INSUFFICIENT_SAMPLE', err)
        self.assertIn('INSUFFICIENT_SAMPLE', json.loads(out)['experiments'][0]['all']['warning'])

    def test_thirty_two_trades_drop_the_warning(self):
        path, exp = self.ledger(script=([(dict(reserve_sol='160', danger=True), 3000)] * 3 + [(dict(danger=True), 3000)]) * 8)
        row = F.evaluate([path], now=T + 40 * DAY, resamples=200)['experiments'][0]['all']
        self.assertEqual(row['closed_trades'], 32)
        self.assertNotIn('warning', row)
        self.assertAlmostEqual(D(row['net_pnl_sol']), sum(t['pnl'] for t in exp), places=25)

    def test_different_config_hashes_never_share_a_row(self):
        a, ea = self.ledger('a.sqlite')
        other = dict(config(), version='2026-10-11.other.1')
        b, eb = self.ledger('b.sqlite', cfg=other, script=SCRIPT[:2])
        report = F.evaluate([a, b], now=T + 10 * DAY)
        rows = {r['version']: r for r in report['experiments']}
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(*[r['config_hash'] for r in rows.values()])
        self.assertEqual(rows[config()['version']]['all']['closed_trades'], 4)
        self.assertEqual(rows['2026-10-11.other.1']['all']['closed_trades'], 2)
        self.assertEqual(D(rows['2026-10-11.other.1']['all']['net_pnl_sol']), sum(t['pnl'] for t in eb))

    def test_single_experiment_flag_refuses_mixing(self):
        a, _ = self.ledger('a.sqlite')
        b, _ = self.ledger('b.sqlite', cfg=dict(config(), version='x.1'))
        with self.assertRaises(F.EvalError):
            F.evaluate([a, b], now=T + 10 * DAY, single=True)
        code, out, err = run_main('--ledger', str(a), '--ledger', str(b), '--single-experiment')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('REFUSED', err)

    def test_same_config_ledgers_pool_and_duplicate_ledger_refused(self):
        a, ea = self.ledger('a.sqlite')
        b, eb = self.ledger('b.sqlite', script=SCRIPT[:1], start=T + 50 * DAY)
        (row,) = F.evaluate([a, b], now=T + 60 * DAY)['experiments']
        self.assertEqual(row['all']['closed_trades'], 5)
        self.assertEqual(len(row['ledgers']), 2)
        with self.assertRaises(F.EvalError):
            F.evaluate([a, a], now=T + 60 * DAY)
        os.link(a, self.dir / 'hard.sqlite')
        with self.assertRaises(F.EvalError):
            F.evaluate([a, self.dir / 'hard.sqlite'], now=T + 60 * DAY)

    def test_holdout_split_partitions_trades(self):
        path, exp = self.ledger()
        cut = exp[2]['closed_at']  # trades 0,1 train; 2,3 holdout (>= cut)
        row = F.evaluate([path], holdout_from=cut, now=T + 10 * DAY)['experiments'][0]
        self.assertEqual((row['train']['closed_trades'], row['holdout']['closed_trades']), (2, 2))
        self.assertEqual(D(row['train']['net_pnl_sol']), exp[0]['pnl'] + exp[1]['pnl'])
        self.assertEqual(D(row['holdout']['net_pnl_sol']), exp[2]['pnl'] + exp[3]['pnl'])
        self.assertIn('INSUFFICIENT_SAMPLE', row['holdout']['warning'])
        self.assertEqual(F.parse_time('2026-10-10T00:00:00Z'), 1791590400)
        with self.assertRaises(F.EvalError):
            F.parse_time('2026-10-10T00:00:00')

    def test_empty_holdout_side_is_reported_not_crashing(self):
        path, _ = self.ledger()
        row = F.evaluate([path], holdout_from=T + 99 * DAY, now=T + 10 * DAY)['experiments'][0]
        self.assertEqual(row['holdout']['closed_trades'], 0)
        self.assertIn('INSUFFICIENT_SAMPLE', row['holdout']['warning'])
        self.assertEqual(row['train']['closed_trades'], 4)

    def test_only_open_positions(self):
        path, exp = self.ledger(script=[({}, 3000)], close_last=False)
        (row,) = F.evaluate([path], now=T + 10)['experiments']
        self.assertEqual(exp, [])
        self.assertEqual(row['all']['closed_trades'], 0)
        self.assertIn('INSUFFICIENT_SAMPLE', row['all']['warning'])
        self.assertEqual(row['open_positions_excluded'], 1)
        self.assertNotIn('net_pnl_sol', row['all'])

    def test_ledger_without_trades_is_zero_not_error(self):
        path = self.dir / 'rej.sqlite'
        ledger = Ledger(path)
        out = ledger.apply(event(T, danger=True, graduated_at=T - 99999), config(), transition, initial_state)
        ledger.close()
        self.assertTrue(all(o['type'] != 'fill' for o in out))
        (row,) = F.evaluate([path], now=T + 10)['experiments']
        self.assertEqual(row['all']['closed_trades'], 0)

    def test_empty_ledger_fails_closed(self):
        path = self.dir / 'empty.sqlite'
        Ledger(path).close()
        with self.assertRaises(F.EvalError):
            F.evaluate([path], now=T)
        self.assertEqual(run_main('--ledger', str(path))[0], 2)

    def test_bootstrap_ci_is_deterministic_and_bounded(self):
        values = [D(x) for x in ('-0.1', '0.05', '0.2', '-0.02', '0.07')]
        ci = F.bootstrap_ci(values, 500, 7)
        self.assertEqual(ci, F.bootstrap_ci(values, 500, 7))
        self.assertNotEqual(ci, F.bootstrap_ci(values, 500, 8))
        self.assertTrue(float(min(values)) <= ci[0] <= ci[1] <= float(max(values)))
        self.assertEqual(F.bootstrap_ci([D('0.1'), D('0.1'), D('0.1')], 100, 0), [0.1, 0.1])
        self.assertIsNone(F.bootstrap_ci([D('0.1')], 100, 0))

    def test_corrupt_or_tampered_ledger_refused(self):
        path, _ = self.ledger()
        with sqlite3.connect(path) as c:  # inflate a recorded realized PnL: replay must catch it
            seq, payload = c.execute("SELECT seq,payload FROM outcomes WHERE payload LIKE '%realized_pnl_sol%'").fetchone()
            fill = json.loads(payload); fill['realized_pnl_sol'] = '99'
            c.execute('UPDATE outcomes SET payload=? WHERE seq=?', (json.dumps(fill), seq))
        with self.assertRaises(F.EvalError):
            F.evaluate([path], now=T + 10 * DAY)

    def test_symlinked_ledger_refused_and_read_is_byte_identical(self):
        path, _ = self.ledger()
        link = self.dir / 'link.sqlite'
        os.symlink(path, link)
        with self.assertRaises(F.EvalError):
            F.evaluate([link], now=T + 10 * DAY)
        before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        run_main('--ledger', str(path), '--now', str(T + 10 * DAY))
        self.assertEqual((hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns), before)
        # a WAL-mode ledger opened mode=ro may get -shm/-wal sidecars from SQLite itself; the db file is untouched


if __name__ == '__main__':
    unittest.main()
