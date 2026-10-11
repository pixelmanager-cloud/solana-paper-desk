"""SYNTHETIC_TEST_ONLY: outcome labels and the live-exit PnL of recorded paths (lean/labels.py, LINT3). Fixtures only, no network.

The paths are written by the REAL L07R2 recorder (``lean.paths.PathRecorder`` + ``PathStore``, its own paths.sqlite) behind a fake
getMultipleAccounts (tests/lean/test_paths.Rpc): a 503 is a real ``path_gaps`` row, a missing vault a real ACCOUNT_MISSING mark, a
rollover a real archive file. The label code reads those files with plain ``mode=ro`` only. An independent integer oracle of the live
mark (constant product, pool fee, fixed fee, no haircut) guards the mark model."""
import hashlib
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from lean import adapters as A, labels as LB, paper, paths as L, providers as P, strategy as S
from tests.lean.test_paths import BASE0, GROSS0, KEY, SPENDABLE0, Clock, Rpc, features, key

ROOT = Path(__file__).resolve().parents[2]
STRATEGY = S.StrategyConfig.load(ROOT / 'config' / 'lean' / 'strategy-default.json')
PCFG = A.paper_config(STRATEGY)                       # fee 50 000 lamports, slippage 50 bps: the live paper config
POOL_FEE = 25
CFG = LB.LabelConfig.from_strategy(STRATEGY)
FEE_RAW = GROSS0 - SPENDABLE0


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def oracle_mark(qty, base, quote):
    gross = quote * qty // (base + qty)
    return max(0, gross * (10000 - POOL_FEE) // 10000 - 50_000)


class RecorderMixin:
    """A real recorder on its own file; ``walk`` advances 15 s per step and polls: a number scales the quote vault (the price),
    ('drain', f) scales BOTH vaults (liquidity leaves, the price stays), 'gap' answers 503, 'dead' removes the vaults.
    (A mixin, so other test modules can drive the real recorder without re-running these tests.)"""

    def setup_recorder(self, directory, clock=None):
        P.configure_lanes()
        self.addCleanup(P.configure_lanes)
        self.dir = Path(directory)
        self.clock, self.rpc = clock or Clock(), Rpc()
        self.store = L.PathStore(self.dir / 'paths.sqlite', clock=self.clock.now)
        self.addCleanup(self.store.close)
        transport = P.Transport('helius', opener=self.rpc, clock=self.clock.now, monotonic=self.clock.now, sleep=self.clock.sleep,
                                rng=lambda: 0.5, lane='low')
        self.rec = L.PathRecorder(db=self.store, helius=P.Helius(KEY, transport), pcfg=PCFG, code_version='test', strategy_version='lean-1',
                                  pool_fee_bps=POOL_FEE, path_hours=0.25, interval_s=15, min_free_gb=0, clock=self.clock.now,
                                  sleep=self.clock.sleep)
        self.n = 0

    def start(self, *, entered=False, fill=None, reasons=('MAX_POSITIONS',), screen_reasons=(), cid=None, mint=None):
        self.n += 1
        mint = mint or key('mint%d' % self.n)
        f = features(mint)
        self.rpc.amounts[f['pool_base_token_account']], self.rpc.amounts[f['pool_quote_token_account']] = BASE0, GROSS0
        decision = self.clock.now()
        self.clock.t += 1.5                                                     # the runner hands the candidate over after its decision
        self.assertTrue(self.rec.start(mint, cid, f, entered=entered, stage=None if entered else 'entry', fill=fill,
                                       reasons=[] if entered else list(reasons), screen_reasons=list(screen_reasons)))
        return mint, decision

    def walk(self, mint, steps):
        f = features(mint)
        b, q = f['pool_base_token_account'], f['pool_quote_token_account']
        for step in steps:
            self.clock.t += 15
            self.rpc.status = 200
            if step == 'gap':
                self.rpc.status = 503
            elif step == 'dead':
                self.rpc.amounts.pop(b, None), self.rpc.amounts.pop(q, None)
            elif isinstance(step, tuple) and step[0] == 'drain':
                self.rpc.amounts[b], self.rpc.amounts[q] = int(BASE0 * step[1]), int(GROSS0 * D(str(step[1])))
            elif isinstance(step, tuple) and step[0] == 'skip':                # the recorder was down: no poll, no gap row
                self.clock.t += step[1]
                continue
            else:
                self.rpc.amounts[b], self.rpc.amounts[q] = BASE0, int(GROSS0 * D(str(step)))
            self.rec.poll()
        self.rpc.status = 200

    def finish(self, mint, pattern=(1,)):
        """Keep polling every 15 s (cycling ``pattern``) until the recorder ends the path (path_ends COMPLETE), as it does live."""
        i = 0
        while mint in self.rec.active():
            self.walk(mint, [pattern[i % len(pattern)]])
            i += 1

    def paths(self):
        connections = LB.open_paths(self.dir / 'paths.sqlite')
        try:
            out, skipped = LB.load_paths(connections)
        finally:
            for c in connections:
                c.close()
        return {p.mint: p for p in out}, skipped

    def label(self, mint, decision, **cfg):
        path = self.paths()[0][mint]
        return LB.label_path(path, decision, LB.reference(path, PCFG), LB.LabelConfig.from_strategy(STRATEGY, **cfg),
                             pool_fee_bps=POOL_FEE, pcfg=PCFG)


class Recorded(RecorderMixin, unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.setup_recorder(os.path.realpath(tmp.name))


class MarkAndReferenceTests(Recorded):
    def test_the_mark_is_the_live_adapters_mark(self):
        for qty, base, quote in ((10 ** 12, BASE0, GROSS0), (7 * 10 ** 11, BASE0 // 3, GROSS0 * 2), (10 ** 9, 10 ** 15, 10 ** 9)):
            self.assertEqual(LB.mark_lamports(LB.Point(1.0, base, quote), qty, POOL_FEE, PCFG), oracle_mark(qty, base, quote))
            self.assertEqual(LB.mark_lamports(LB.Point(1.0, base, quote), qty, POOL_FEE, PCFG),
                             int(A.mark(qty, base, quote, pool_fee_bps=POOL_FEE, pcfg=PCFG) * paper.LAMPORTS))
        self.assertEqual(LB.mark_lamports(LB.Point(1.0, None, None, 'EMPTY_POOL'), 10 ** 12, POOL_FEE, PCFG), 0)

    def test_reference_is_the_live_paper_position(self):
        mint, _ = self.start()
        path = self.paths()[0][mint]
        t = path.target
        ref = LB.reference(path, PCFG)
        self.assertEqual(ref.basis, 'ref_size')
        self.assertEqual(ref.qty_raw, t['qty_raw'] * (10000 - 50) // 10000)          # paper.buy: the slippage haircut on the tokens
        self.assertEqual(ref.cost_lamports, t['cost_lamports'] + 50_000)             # ... and the fixed fee on the cost
        fill = paper.Fill(ts=1.0, mint='x', side='buy', qty_raw=123_456_789, sol_lamports=10 ** 8, fee_lamports=50_000, slippage_bps=50, decimals=6)
        mint2, _ = self.start(entered=True, fill=fill)
        ref2 = LB.reference(self.paths()[0][mint2], PCFG)
        self.assertEqual((ref2.basis, ref2.qty_raw, ref2.cost_lamports), ('fill', 123_456_789, 10 ** 8 + 50_000))

    def test_an_unusable_target_is_refused(self):
        mint, _ = self.start()
        path = self.paths()[0][mint]
        for bad in ({'qty_raw': 0}, {'cost_lamports': '1'}, {'basis': 'guess'}, {'decimals': 99}):
            with self.assertRaises(LB.LabelError):
                LB.reference(LB.PathRecord(**{**path.__dict__, 'target': {**path.target, **bad}}), PCFG)

    def test_lines_come_from_the_live_strategy_and_bad_configs_are_refused(self):
        self.assertEqual((CFG.win_ratio, CFG.stop_ratio), (D('1.4'), D('0.82')))
        for bad in (dict(win_ratio=D('0.9')), dict(stop_ratio=D('1.1')), dict(rug_price_ratio=D('0.9')), dict(rug_liquidity_drop=D('1')),
                    dict(rug_window_s=0), dict(max_gap_fraction=1.0), dict(max_gap_fraction=-0.1), dict(max_hole_s=float('nan')),
                    dict(win_ratio=1.4)):
            with self.assertRaises(LB.LabelError, msg=bad):
                LB.LabelConfig(**bad)


class LabelTests(Recorded):
    def test_win_before_the_stop_even_if_it_gives_it_all_back(self):
        mint, decision = self.start()
        self.walk(mint, [1, 1.3, 1.6, 0.5, 0.1])
        lab = self.label(mint, decision)
        self.assertEqual((lab.label, lab.t_event), ('WIN', 46.5))                  # the third mark: 1.5 s hand-over + 3 x 15 s
        self.assertEqual(lab.decision_ts, decision)

    def test_loss_when_the_stop_comes_first_even_if_it_rallies(self):
        mint, decision = self.start()
        self.walk(mint, [1, 0.7, 2.0])
        self.assertEqual(self.label(mint, decision).label, 'LOSS')

    def test_rug_by_price_and_a_rug_beats_an_earlier_stop(self):
        mint, decision = self.start()
        self.walk(mint, [1, 0.7, 0.1])
        lab = self.label(mint, decision)
        self.assertEqual((lab.label, lab.rug_cause, lab.t_event), ('RUG', 'PRICE', 46.5))

    def test_rug_by_a_dead_pool(self):
        mint, decision = self.start()
        self.walk(mint, [1, 1.1, 'dead'])
        lab = self.label(mint, decision)
        self.assertEqual((lab.label, lab.rug_cause), ('RUG', 'POOL_DEAD'))

    def test_liquidity_is_measured_from_the_decision_time_reserve_not_the_first_mark(self):
        """The pool is drained 75 % (both vaults: the price barely moves) BEFORE the first mark. Against the first mark nothing would
        ever look drained; against the screen's spendable reserve it is a RUG at the first mark."""
        mint, decision = self.start()
        self.walk(mint, [('drain', 0.25), ('drain', 0.25), ('drain', 0.25)])
        lab = self.label(mint, decision)
        self.assertEqual((lab.label, lab.rug_cause, lab.t_event), ('RUG', 'LIQUIDITY', 16.5))
        self.assertGreater(lab.max_down, -0.5)                                     # the price alone was no rug

    def test_flat_only_for_a_finished_fully_observed_path(self):
        mint, decision = self.start()
        self.walk(mint, [1, 1.05, 0.95] * 5)
        self.assertEqual((self.label(mint, decision).label, self.label(mint, decision).why_insufficient), ('INSUFFICIENT_PATH', 'PATH_NOT_ENDED'))
        self.finish(mint, (1, 1.05, 0.95))
        lab = self.label(mint, decision)
        self.assertEqual(lab.label, 'FLAT', lab)
        self.assertIsNone(lab.t_event)

    def test_marks_before_the_decision_are_never_read(self):
        mint, decision = self.start()
        self.walk(mint, [0.05, 1, 1.5])
        path = self.paths()[0][mint]
        later = path.points[0].ts + 0.5                                             # a decision after the crash mark
        lab = LB.label_path(path, later, LB.reference(path, PCFG), CFG, pool_fee_bps=POOL_FEE, pcfg=PCFG)
        self.assertEqual((lab.label, lab.n_marks), ('WIN', 2))
        self.assertEqual(self.label(mint, decision).label, 'RUG')

    def test_extremes_and_end_return(self):
        mint, decision = self.start()
        self.walk(mint, [1, 1.2, 0.9, 1.1])
        self.finish(mint, (1.1,))
        lab = self.label(mint, decision)
        self.assertEqual(lab.label, 'FLAT')
        self.assertGreater(lab.max_up, 0.15)
        self.assertLess(lab.max_down, -0.1)
        self.assertEqual((lab.t_max_up, lab.t_max_down), (31.5, 46.5))
        self.assertAlmostEqual(lab.end_return, lab.max_up - (lab.max_up - lab.end_return), 9)


class GapTests(Recorded):
    """A gap is a missing observation, never a flat price."""

    def test_a_flat_looking_path_full_of_gaps_is_insufficient_not_flat(self):
        mint, decision = self.start()
        self.finish(mint, (1, 'gap', 'gap', 'gap'))                              # one mark in four, the price never moves
        with sqlite3.connect((self.dir / 'paths.sqlite').as_uri() + '?mode=ro', uri=True) as c:
            gaps = c.execute('SELECT COUNT(*) FROM path_gaps WHERE mint=?', (mint,)).fetchone()[0]          # real gap rows
            marks = c.execute('SELECT COUNT(*) FROM path_marks WHERE mint=?', (mint,)).fetchone()[0]
        self.assertGreater(gaps, 2 * marks)
        lab = self.label(mint, decision)
        self.assertEqual((lab.label, lab.why_insufficient), ('INSUFFICIENT_PATH', 'TOO_MANY_GAPS'))
        self.assertAlmostEqual(lab.gap_fraction, gaps / (gaps + marks), 5)
        self.assertEqual(self.label(mint, decision, max_gap_fraction=0.8).label, 'FLAT')        # only the threshold decides

    def test_a_few_gaps_are_tolerated(self):
        mint, decision = self.start()
        self.walk(mint, [1, 1, 'gap', 1, 1, 1, 1, 1, 1.5])
        lab = self.label(mint, decision)
        self.assertEqual(lab.label, 'WIN')
        self.assertAlmostEqual(lab.gap_fraction, 1 / 9, 5)

    def test_gaps_after_the_deciding_mark_do_not_matter(self):
        mint, decision = self.start()
        self.walk(mint, [1, 1.5] + ['gap'] * 10)
        self.assertEqual(self.label(mint, decision).label, 'WIN')

    def test_a_recorder_outage_without_gap_rows_is_a_hole(self):
        mint, decision = self.start()
        self.walk(mint, [1, ('skip', 600), 0.7])                                  # 10 min down, then a stop line: when did it fall?
        lab = self.label(mint, decision)
        self.assertEqual((lab.label, lab.why_insufficient), ('INSUFFICIENT_PATH', 'HOLE'))
        self.assertGreater(lab.max_hole_s, 600)

    def test_a_path_that_ended_while_the_recorder_was_down_is_never_flat(self):
        mint, decision = self.start()
        self.walk(mint, [1, 1, 1])
        self.store.add(ends=[(mint, self.clock.now() + 3600, 'EXPIRED_WHILE_DOWN', None, None)])
        lab = self.label(mint, decision)
        self.assertEqual((lab.label, lab.why_insufficient), ('INSUFFICIENT_PATH', 'HOLE'))

    def test_no_mark_at_all(self):
        mint, decision = self.start()
        self.walk(mint, ['gap', 'gap'])
        self.assertEqual(self.label(mint, decision).why_insufficient, 'NO_MARK')


class FileTests(Recorded):
    def test_a_rolled_over_path_is_one_path_from_its_first_start_with_every_mark(self):
        mint, decision = self.start()
        self.walk(mint, [1, 1.1])
        archive = self.store.rollover()                                           # the real rollover: paths-YYYYMMDD.sqlite + carried row
        self.walk(mint, [1.2, 1.5])
        self.assertTrue(archive.exists())
        paths, skipped = self.paths()                                             # the archive is found beside paths.sqlite
        path = paths[mint]
        self.assertEqual(len(path.points), 4)
        self.assertEqual(path.start_ts, decision + 1.5)
        self.assertEqual(skipped, {'PATH_CARRIED_OVER': 1})
        self.assertEqual(self.label(mint, decision).label, 'WIN')

    def test_files_are_only_read(self):
        mint, decision = self.start()
        self.walk(mint, [1, 'gap', 1.5])
        self.store.close()
        before = hashlib.sha256((self.dir / 'paths.sqlite').read_bytes()).hexdigest()
        self.paths()
        self.label(mint, decision)
        self.assertEqual(hashlib.sha256((self.dir / 'paths.sqlite').read_bytes()).hexdigest(), before)

    def test_corrupt_rows_are_counted_never_used(self):
        mint, decision = self.start()
        self.walk(mint, [1, 1.5])
        self.store.add(marks=[(mint, self.clock.now() + 1, 1, -5, 10, None, None, 'OK'), ('orphan', 1.0, 1, 1, 1, None, None, 'OK')])
        self.store.db.execute("INSERT INTO paths(mint,start_ts,ends_ts,entered,reason,screen_reasons,holders_checked,features,target,"
                              "code_version,strategy_version) VALUES('broken',1,2,0,'[]','[]',0,'not json','{}','c','s')")
        paths, skipped = self.paths()
        self.assertEqual(set(paths), {mint})
        self.assertEqual(skipped, {'MARK_NOT_USABLE': 1, 'MARKS_WITHOUT_PATH': 1, 'PATH_ROW_UNREADABLE': 1})
        self.assertIn(self.clock.now() + 1, paths[mint].missing)                 # a corrupt mark is a missing observation


class LiveExitTests(Recorded):
    def sim(self, mint, decision):
        path = self.paths()[0][mint]
        return LB.simulate_exits(path, decision, LB.reference(path, PCFG), STRATEGY, pool_fee_bps=POOL_FEE, pcfg=PCFG)

    def test_the_take_profit_ladder_and_the_trailing_stop(self):
        mint, decision = self.start()
        self.walk(mint, [1, 1.5, 2.2, 3.2, 3.2, 2.1])                           # 2.1: under the 30 % trail, above the rung-3 stop (2)
        t = self.sim(mint, decision)
        self.assertEqual(t.status, 'CLOSED')
        self.assertEqual(t.reasons, ['TAKE_PROFIT', 'TAKE_PROFIT', 'TAKE_PROFIT', 'TRAILING_STOP'])
        self.assertGreater(t.pnl_lamports, 0)

    def test_a_stop_and_its_exact_paper_pnl(self):
        mint, decision = self.start()
        self.walk(mint, [1, 0.75])
        path = self.paths()[0][mint]
        ref = LB.reference(path, PCFG)
        t = self.sim(mint, decision)
        self.assertEqual((t.status, t.reasons), ('CLOSED', ['STOP']))
        point = path.points[1]
        out = point.quote_raw * ref.qty_raw // (point.base_raw + ref.qty_raw) * (10000 - POOL_FEE) // 10000
        self.assertEqual(t.pnl_lamports, out * (10000 - 50) // 10000 - 50_000 - ref.cost_lamports)   # paper.sell, by hand

    def test_a_pool_that_dies_is_written_off_and_a_live_one_stays_open(self):
        mint, decision = self.start()
        self.walk(mint, [1, 1.1, 'dead'])
        t = self.sim(mint, decision)
        self.assertEqual((t.status, t.pnl_lamports), ('RUG_WRITEOFF', -LB.reference(self.paths()[0][mint], PCFG).cost_lamports))
        mint2, decision2 = self.start()
        self.walk(mint2, [1, 1.1, 1.05])
        t2 = self.sim(mint2, decision2)
        self.assertEqual((t2.status, t2.reasons), ('OPEN_AT_END', []))

    def test_no_mark_before_the_decision_is_traded(self):
        mint, decision = self.start()
        self.walk(mint, [0.5, 1, 1.5])
        path = self.paths()[0][mint]
        t = LB.simulate_exits(path, path.points[1].ts, LB.reference(path, PCFG), STRATEGY, pool_fee_bps=POOL_FEE, pcfg=PCFG)
        self.assertEqual(t.reasons, ['TAKE_PROFIT'])                              # the 0.5 mark (a STOP) came before that decision


if __name__ == '__main__':
    unittest.main()
