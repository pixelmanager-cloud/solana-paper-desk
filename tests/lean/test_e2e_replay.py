"""SYNTHETIC_TEST_ONLY: C1 end to end. The test_e2e_real scenario (20 candidates, a Helius outage, a mid-run restart) runs on
fakeworld with the REAL runner and the REAL path recorder; then ``lean.tune`` replays the recorded paths.

Calibration: the live ladder replayed (real ``exit_decision``, real ``adapters.mark`` on the stored raw reserves) on the paths of the
7 positions the trader really held reproduces EVERY live exit reason (STOP, TAKE_PROFIT x3 then TRAILING_STOP, TP then STOP, TIME_STOP),
with live-equivalent costs and the live fill timing; a mark model that is off by a few percent does NOT, so the check has teeth.
"""
import hashlib
import io
import json
import os
import socket
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from lean import adapters as A, replay as R, tune as T
from tests.lean import test_e2e_paths as P

STRATEGY = P.E.ROOT / 'config' / 'lean' / 'strategy-default.json'
LIVE_COSTS = {'fee_model': 'flat', 'flat_bps': 25, 'slippage_bps': 50, 'fee_sol': '0.00005'}      # the baseline runner's own costs
EXPECTED = {'stop': ['STOP'], 'ladder': ['TAKE_PROFIT', 'TAKE_PROFIT', 'TAKE_PROFIT', 'TRAILING_STOP'], 'time': ['TIME_STOP'],
            'tp1_stop': ['TAKE_PROFIT', 'STOP'], 'quick_stop': ['STOP'], 'outage': ['TIME_STOP'], 'late_stop': ['STOP']}
GRID = {'families': [{'family': 'live_ladder'}, {'family': 'full_tp', 'x': [1.3, 1.5], 'stop': 0.18}, {'family': 'hold', 'minutes': [5, 30]},
                     {'family': 'trailing', 'fraction': 0.2}, {'family': 'partial_tp_trail', 'x': 1.4, 'sell_fraction': 0.5, 'fraction': 0.2}],
        'calibration_costs': LIVE_COSTS, 'calibration_lag': 0, 'blocks': 3, 'bootstrap': 200, 'path_hours': 6, 'seed': 5}


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class ReplayOnTheLiveScenario(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.case = P.EndToEndPathsTest('test_paths_recorded_for_entered_and_soft_rejected_and_exits_unaffected')
        cls.case.setUp()
        out = cls.case.run_scenario('replay', True)
        cls.role, cls.runner, cls.root = out['role'], out['runner'], out['root']
        cls.state = cls.root / 'state'
        cls.runner.store.close()
        cls.runner.paths.db.close()
        cls.lean, cls.paths = cls.state / 'lean.sqlite', cls.state / 'paths.sqlite'

    @classmethod
    def tearDownClass(cls):
        cls.case.doCleanups()

    def analyse(self, **kw):
        grid = dict(GRID, **kw)
        return T.analyse(self.lean, self.paths, grid, strategy_path=STRATEGY, now=1.0)

    def test_the_live_ladder_reproduces_every_live_exit_reason(self):
        report = self.analyse()
        cal = report['calibration']
        self.assertEqual((cal['compared'], cal['matched']), (7, 7), cal['mismatches'])
        by_role = {self.role[r['mint']]: r['replay'] for r in cal['rows']}
        self.assertEqual(by_role, EXPECTED)
        self.assertEqual((report['data']['paths_total'], report['data']['entered_live']), (16, 7))

    def test_the_default_tier_costs_and_next_mark_fills_keep_the_same_reasons(self):
        costs = R.costs_from(None, R.load_fee_tiers())
        prepared = [x for x in (R.prepare(p, costs, 200_000_000) for p in R.load_paths(self.paths).values()) if x]
        cal = T.calibrate(self.lean, prepared, T.S.StrategyConfig.load(STRATEGY), costs, 1)
        self.assertEqual((cal['compared'], cal['matched']), (7, 7), cal['mismatches'])

    def test_a_wrong_mark_model_is_caught_by_the_calibration(self):
        real = A.mark

        def low(qty, base, quote, **kw):                    # a mark that is ~12% low (the known adapters.mark issue) shifts every trigger
            return real(qty, base, quote, **kw) * R.Decimal('0.88')
        with mock.patch.object(A, 'mark', low):
            report = self.analyse()
        cal = report['calibration']
        self.assertLess(cal['matched'], cal['compared'])
        self.assertTrue(cal['mismatches'])

    def test_every_policy_is_replayed_on_every_enterable_path_and_the_report_is_complete(self):
        report = self.analyse()
        self.assertEqual(len(report['policies']), 1 + 2 + 2 + 1 + 1)
        self.assertTrue(all(p['n'] == report['data']['enterable'] for p in report['policies']))
        live = next(p for p in report['policies'] if p['policy'] == 'live_ladder()')
        self.assertIn('STOP', live['exit_reasons'])
        self.assertIn('TAKE_PROFIT', live['exit_reasons'])
        self.assertTrue(report['walk_forward']['folds'])

    def test_cli_on_the_recorded_databases_is_read_only_and_writes_the_report(self):
        out = self.root / 'out'
        out.mkdir()
        grid = self.root / 'grid.json'
        grid.write_text(json.dumps(GRID))
        files = [self.lean, self.paths]
        digest = lambda: [(hashlib.sha256(f.read_bytes()).hexdigest(), f.stat().st_mtime_ns) for f in files]
        before = digest()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = T.main(['--lean-db', str(self.lean), '--paths-db', str(self.paths), '--grid', str(grid), '--out-dir', str(out),
                           '--strategy', str(STRATEGY), '--now', '9'])
        self.assertEqual(code, 0, buffer.getvalue())
        self.assertEqual(digest(), before)
        names = sorted(os.listdir(out))
        self.assertIn('tune-report.json', names)
        self.assertTrue('proposal.json' in names or 'NO_PROPOSAL' in names)
        report = json.loads((out / 'tune-report.json').read_text())
        self.assertEqual(report['calibration']['matched'], 7)


if __name__ == '__main__':
    unittest.main()
