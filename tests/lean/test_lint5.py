"""SYNTHETIC_TEST_ONLY: LINT5 integration fixes for the Round C branches (C1 replay / tune, C3 census, C4 backfill, C2 curves).

C1: a mark row the recorder wrote with ``marks.model: effective`` is replayed with ITS model (vault + virtual reserves) and ITS
fee_bps through the labels / marks helpers; a vault row keeps the vault reserves and the fixture tier; a proposal needs the
proposed policy's own test-block CI lower bound above zero.
"""
import json
import socket
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from lean import adapters as A, labels as LB, marks as M, paths as L, replay as R, strategy as S, tune as T
from tests.lean.fakeworld import T0
from tests.lean.test_m1 import EFFECTIVE, World1, boosted
from tests.lean.test_e2e_real import ROOT, steps

BASE = S.StrategyConfig.load(ROOT / 'config' / 'lean' / 'strategy-default.json')
B = 10 ** 15
Q0 = 80 * 10 ** 9
VIRT = 40 * 10 ** 9
SIZE = 200_000_000


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def _db(path, rows, start=1000.0):
    store = L.PathStore(path)
    store.insert_path(dict(mint='M1', candidate_id=1, start_ts=start, ends_ts=start + 6 * 3600, entered=1, stage=None, reason='[]',
                           screen_reasons='[]', holders_checked=1, features=json.dumps({'supply_raw': str(B)}), target='{}',
                           carried_from=None, code_version='c', strategy_version='s'))
    store.add(marks=rows)
    store.close()
    return path


def eff_rows(ratios, *, model='effective', fee=120, virtual=VIRT):
    return [('M1', 1000.0 + 15 * i, i, B, int(Q0 * r), None, None, 'OK', model, virtual if model == 'effective' else None, 0, fee,
             'dynamic' if model == 'effective' else 'config') for i, r in enumerate(ratios)]


class C1EffectiveReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.costs = R.costs_from(None, R.load_fee_tiers())

    def prep(self, rows, costs=None):
        folder = Path(tempfile.mkdtemp(dir=self.tmp.name))                   # one recorder file per call
        db = _db(folder / 'paths.sqlite', rows)
        return R.prepare(R.load_paths(db)['M1'], costs or self.costs, SIZE)

    def test_effective_rows_use_virtual_reserves_and_their_own_fee(self):
        p = self.prep(eff_rows([1, 1.1, 1.3, 0.9]))
        self.assertEqual(p.bps, [120, 120, 120, 120])                       # the row's fee, not the fixture tier (125 at 80 SOL)
        self.assertTrue(all(pt is not None and pt.effective for pt in p.points))
        expect_qty = M.buy_output(SIZE, B, Q0 + VIRT, 120) * 9950 // 10000
        self.assertEqual(p.qty0, expect_qty)
        point = LB.Point(1015.0, B, int(Q0 * 1.1), virtual_raw=VIRT, fee_bps=120, model='effective')
        self.assertEqual(p.value(1, p.qty0, self.costs.pcfg), A.sol(LB.mark_lamports(point, p.qty0, None, self.costs.pcfg)))
        out = LB.sell_quote_lamports(point, p.qty0, None)
        self.assertEqual(p.proceeds(1, p.qty0, self.costs), out * 9950 // 10000 - 250_000)

    def test_flat_costs_never_override_an_effective_rows_fee(self):
        flat = R.costs_from({'fee_model': 'flat', 'flat_bps': 25}, R.load_fee_tiers())
        self.assertEqual(self.prep(eff_rows([1, 1.1]), flat).bps, [120, 120])

    def test_vault_rows_keep_the_vault_reserves_and_the_fixture_tier(self):
        p = self.prep(eff_rows([1, 1.1, 1.3], model='vault', fee=25))
        self.assertEqual(p.points, [])
        self.assertEqual(p.bps, [125, 125, 125])                            # the recorded 25 bps is the config fee, not the pool's
        spent = SIZE * (10000 - 125) // 10000
        self.assertEqual(p.qty0, B * spent // (Q0 + spent) * 9950 // 10000)

    def test_the_virtual_reserve_changes_the_decisions(self):
        # +40% on the vault is only +26.7% on vault + virtual: a full TP at 1.3 fires on the vault model only
        ratios = [1, 1, 1.4, 1.4, 1.4]
        pol = R.expand_grid([{'family': 'full_tp', 'x': 1.3}])[0]
        eff = R.replay_path(self.prep(eff_rows(ratios)), pol, BASE, self.costs, 0)
        vault = R.replay_path(self.prep(eff_rows(ratios, model='vault')), pol, BASE, self.costs, 0)
        self.assertEqual(vault.reasons, ['TAKE_PROFIT'])
        self.assertEqual(eff.reasons, ['PATH_END'])

    def test_a_malformed_effective_row_falls_back_to_vault_not_guessed(self):
        rows = eff_rows([1, 1.1])
        rows[1] = rows[1][:9] + (None,) + rows[1][10:]                      # virtual missing
        p = self.prep(rows)
        self.assertIsNotNone(p.points[0])
        self.assertIsNone(p.points[1])
        self.assertEqual(p.bps[1], 125)


class C1ReplayReproducesTheEffectiveRecorder(World1):
    """E2E (real runner + real recorder on fakeworld, marks.model effective): the replay's mark of the recorder's own reference
    position equals the recorder's ``net_sol`` on every row (same helper, same inputs)."""

    def test_replay_marks_equal_the_recorded_effective_marks(self):
        token = boosted(path=steps((120, 1.1), (240, 0.9)))
        r = self.build(token, cfg_over=EFFECTIVE)
        r.l07r_start_paths()
        for i in range(30):
            self.clock.t = T0 + 15 * i
            r.position_pass()
            r.candidate_pass()
            r.paths.poll()
        db = r.paths.db.path
        rows = sqlite3.connect(db).execute("SELECT ts, net_sol FROM path_marks WHERE mint=? AND status='OK' ORDER BY ts",
                                           (token.mint,)).fetchall()
        self.assertGreater(len(rows), 10)
        target = json.loads(sqlite3.connect(db).execute('SELECT target FROM paths WHERE mint=?', (token.mint,)).fetchone()[0])
        costs = R.costs_from({'fee_sol': str(A.sol(r.pcfg.fee_lamports))}, R.load_fee_tiers())
        prep = R.prepare(R.load_paths(db)[token.mint], costs, SIZE)
        self.assertTrue(prep.points and all(p is not None for p in prep.points))
        self.assertEqual(set(prep.bps), {120})
        recorded = {ts: Decimal(net) for ts, net in rows}
        for i, ts in enumerate(prep.ts):
            mine = prep.value(i, target['qty_raw'], costs.pcfg)
            self.assertLessEqual(abs(mine - recorded[ts]), Decimal('0.000000002'), (i, mine, recorded[ts]))


class C1ProposalNeedsItsOwnTestBlock(unittest.TestCase):
    def test_pooled_ci_above_zero_but_the_proposed_policys_block_below_is_refused(self):
        pol = R.expand_grid([{'family': 'hold', 'minutes': 2}])[0]
        wf = {'selections': [pol.name, pol.name], 'pooled': {'n': 100, 'ci_lo': 0.01},
              'folds': [{'selected': pol.name, 'test_ci_lo': 0.05}, {'selected': pol.name, 'test_ci_lo': -0.02}]}
        proposal, why = T.decide_proposal(wf, {'min_oos_trades': 30}, {}, [pol])
        self.assertIsNone(proposal)
        self.assertIn('TEST_BLOCK', why)
        wf['folds'][-1]['test_ci_lo'] = 0.001
        proposal, why = T.decide_proposal(wf, {'min_oos_trades': 30}, {}, [pol])
        self.assertEqual(proposal['policy'], pol.name)


if __name__ == '__main__':
    unittest.main()
