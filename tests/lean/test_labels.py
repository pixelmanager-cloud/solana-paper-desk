"""SYNTHETIC_TEST_ONLY: outcome labels for recorded price paths (lean/labels.py). Fixtures only, no network.

Paths are synthetic pools of 1e8 tokens (6 decimals) against 100 SOL. A mark is the live `adapters.mark` of a reference quantity of
1e12 raw tokens that cost 1 SOL; the test carries an independent integer oracle of that mark so a different mark model is caught."""
import dataclasses
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from lean import labels as LB, paths as L, replay as R, strategy as S

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / 'config' / 'lean' / 'strategy-default.json'
T0 = 19675 * 86400 + 3600
BASE, QUOTE0 = 10 ** 14, 100 * 10 ** 9
QTY, COST = 10 ** 12, 10 ** 9
POOL_FEE, FIXED_FEE = 25, 50_000
CFG = LB.LabelConfig(pool_fee_bps=POOL_FEE, fee_lamports=FIXED_FEE)


def oracle_ratio(quote_scale=1, base_scale=1):
    """The live mark of QTY over COST in plain integer arithmetic: constant product, pool fee, fixed fee, no haircut."""
    base, quote = BASE * base_scale, int(QUOTE0 * D(str(quote_scale)))
    gross = quote * QTY // (base + QTY)
    return D(gross * (10000 - POOL_FEE) // 10000 - FIXED_FEE) / COST


def pt(k, quote=1, base=1, status='OK', dt=15):
    if status != 'OK':
        return R.Point(T0 + dt * k, None, None, status)
    return R.Point(T0 + dt * k, BASE * base, int(QUOTE0 * D(str(quote))), 'OK')


def label(points, start=T0, cfg=CFG, fee_raw=0):
    return LB.label_points('M', start, points, QTY, COST, fee_raw, cfg)


class MarkTests(unittest.TestCase):
    def test_mark_is_the_live_mark_over_the_cost_basis(self):
        for q, b in ((1, 1), (1.4, 1), (0.8, 1), (1, 20), (0.25, 1)):
            self.assertEqual(LB.mark_ratio(pt(0, q, b), QTY, COST, CFG), oracle_ratio(q, b), (q, b))

    def test_dead_mark_is_worth_nothing(self):
        self.assertEqual(LB.mark_ratio(pt(0, status='EMPTY_POOL'), QTY, COST, CFG), 0)

    def test_the_haircut_free_mark_is_what_decides_a_label_at_the_edge(self):
        # find the quote scale where the oracle ratio crosses the stop line, then probe either side of it
        lo, hi = D('0.5'), D('1')
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if oracle_ratio(mid) <= CFG.stop_ratio else (lo, mid)
        above, below = hi + D('0.0004'), lo - D('0.0004')           # ~0.04% either side: a 50 bps haircut would flip 'above'
        self.assertEqual(label([pt(0), pt(1, above)]).label, 'FLAT')
        self.assertEqual(label([pt(0), pt(1, below)]).label, 'LOSS')


class LabelTests(unittest.TestCase):
    def test_win_before_stop_even_if_it_gives_it_all_back(self):
        lab = label([pt(0), pt(1, 1.1), pt(2, 1.5), pt(3, 1.2), pt(4, 0.7)])
        self.assertEqual((lab.label, lab.t_event, lab.rug_cause), ('WIN', 30, None))

    def test_loss_when_the_stop_comes_first_even_if_it_rallies_later(self):
        lab = label([pt(0), pt(1, 0.9), pt(2, 0.8), pt(3, 2.0)])
        self.assertEqual((lab.label, lab.t_event), ('LOSS', 30))

    def test_flat(self):
        lab = label([pt(0), pt(1, 1.05), pt(2, 0.95), pt(3, 1.1)])
        self.assertEqual((lab.label, lab.t_event, lab.rug_cause), ('FLAT', None, None))

    def test_rug_by_price_while_the_liquidity_stays(self):
        lab = label([pt(0), pt(1, 1, 1), pt(2, 1, 20)])             # 20x tokens dumped into the pool: the mark falls 95%, the SOL stays
        self.assertEqual((lab.label, lab.rug_cause, lab.t_event), ('RUG', 'PRICE', 30))

    def test_rug_by_liquidity_alone(self):
        lab = label([pt(0), pt(1, 0.25)])                           # 75% of the SOL left; the mark is still above -80%
        self.assertGreater(oracle_ratio(0.25), CFG.rug_price_ratio)
        self.assertEqual((lab.label, lab.rug_cause), ('RUG', 'LIQUIDITY'))

    def test_liquidity_drop_is_measured_on_spendable_reserve_net_of_pool_fees(self):
        fee_raw = 10 * 10 ** 9
        spendable0 = QUOTE0 - fee_raw
        keep = int(spendable0 * D('0.31')) + fee_raw                # 69% down: not a rug yet
        gone = int(spendable0 * D('0.29')) + fee_raw                # 71% down
        mk = lambda k, q: R.Point(T0 + 15 * k, BASE, q)
        self.assertEqual(label([mk(0, QUOTE0), mk(1, keep)], fee_raw=fee_raw).label, 'LOSS')
        self.assertEqual(label([mk(0, QUOTE0), mk(1, gone)], fee_raw=fee_raw).rug_cause, 'LIQUIDITY')

    def test_a_dead_pool_is_a_rug(self):
        lab = label([pt(0), pt(1), pt(2, status='ACCOUNT_MISSING')])
        self.assertEqual((lab.label, lab.rug_cause, lab.t_event), ('RUG', 'POOL_DEAD', 30))

    def test_the_price_rug_needs_the_first_hour_but_the_stop_still_counts(self):
        late = label([pt(0), pt(1, 0.8), R.Point(T0 + 3700, BASE * 20, QUOTE0, 'OK')])
        self.assertEqual(late.label, 'LOSS')                        # past the window a price-only collapse is a stop-out, not a rug
        edge = label([pt(0), R.Point(T0 + 3600, BASE * 20, QUOTE0, 'OK')])
        self.assertEqual((edge.label, edge.rug_cause), ('RUG', 'PRICE'))      # exactly at the window's end still counts

    def test_precedence_by_time(self):
        # a stop and then a collapse: RUG
        self.assertEqual(label([pt(0), pt(1, 0.8), pt(2, 0.1)]).label, 'RUG')
        # a win and then a collapse: the trader had banked TP1, WIN
        self.assertEqual(label([pt(0), pt(1, 1.5), pt(2, 0.1)]).label, 'WIN')
        # a stop, then a rally to +40%: the stop came first, LOSS
        self.assertEqual(label([pt(0), pt(1, 0.8), pt(2, 1.6)]).label, 'LOSS')
        # a win and then the pool disappearing: WIN as well
        self.assertEqual(label([pt(0), pt(1, 1.5), pt(2, status='EMPTY_POOL')]).label, 'WIN')
        # a win and a liquidity collapse on the very same mark (the SOL leaves, the price in tokens is huge): RUG
        same = label([pt(0), R.Point(T0 + 15, BASE // 100, int(QUOTE0 * D('0.25')), 'OK')])
        self.assertGreaterEqual(LB.mark_ratio(R.Point(T0 + 15, BASE // 100, int(QUOTE0 * D('0.25')), 'OK'), QTY, COST, CFG), CFG.win_ratio)
        self.assertEqual((same.label, same.rug_cause), ('RUG', 'LIQUIDITY'))

    def test_extremes_times_and_end_return(self):
        lab = label([pt(0), pt(1, 1.3), pt(2, 0.9), pt(3, 1.05)])
        self.assertAlmostEqual(lab.max_up, float(oracle_ratio(1.3) - 1), 9)
        self.assertAlmostEqual(lab.max_down, float(oracle_ratio(0.9) - 1), 9)
        self.assertEqual((lab.t_max_up, lab.t_max_down), (15, 30))
        self.assertAlmostEqual(lab.end_return, float(oracle_ratio(1.05) - 1), 9)
        self.assertEqual((lab.n_marks, lab.label_version, lab.mint), (4, LB.LABEL_VERSION, 'M'))

    def test_marks_before_the_start_are_never_looked_at(self):
        before = R.Point(T0 - 60, BASE, 1)                          # a collapse an instant before the decision
        lab = label([before, pt(0), pt(1, 1.05)])
        self.assertEqual((lab.label, lab.n_marks), ('FLAT', 2))

    def test_no_usable_mark(self):
        self.assertIsNone(label([R.Point(T0 - 5, BASE, QUOTE0)]))

    def test_the_label_is_a_function_of_the_marks_only(self):
        marks = [pt(0), pt(1, 1.5), pt(2, 0.4)]
        self.assertEqual(label(marks), label(list(reversed(marks))))
        self.assertEqual(label(marks), label(marks))


class ConfigTests(unittest.TestCase):
    def test_lines_come_from_the_live_strategy(self):
        strategy = S.StrategyConfig.load(DEFAULT)
        cfg = LB.LabelConfig.from_strategy(strategy)
        self.assertEqual((cfg.win_ratio, cfg.stop_ratio, cfg.fee_lamports, cfg.pool_fee_bps), (D('1.4'), D('0.82'), 50_000, 25))
        raw = json.loads(DEFAULT.read_text())
        raw['stop_fraction'] = '0.25'
        raw['tp_ladder'][0]['trigger'] = '1.6'
        other = LB.LabelConfig.from_strategy(S.StrategyConfig.from_dict(raw))
        self.assertEqual((other.win_ratio, other.stop_ratio), (D('1.6'), D('0.75')))

    def test_bad_configs_are_refused(self):
        for kw in ({'win_ratio': D('0.9')}, {'stop_ratio': D('1.1')}, {'rug_price_ratio': D('0.9')}, {'rug_liquidity_drop': D('1')},
                   {'rug_window_s': 0}, {'rug_window_s': float('nan')}, {'pool_fee_bps': -1}, {'pool_fee_bps': True}, {'fee_lamports': -1},
                   {'win_ratio': 1.4}, {'stop_ratio': D('NaN')}):
            with self.assertRaises(LB.LabelError, msg=kw):
                LB.LabelConfig(**kw)

    def test_bad_reference_is_refused(self):
        for qty, cost in ((0, 1), (1, 0), (-1, 1), (1.5, 1), (True, 1)):
            with self.assertRaises(LB.LabelError, msg=(qty, cost)):
                LB.label_points('M', T0, [pt(0)], qty, cost, 0, CFG)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))

    def start_row(self, mint, start, **target):
        feats = {'decimals': 6, 'supply_raw': str(10 ** 15), 'sol_usd': '150'}
        tgt = {'mint': mint, 'pool': 'P', 'base_vault': 'B', 'quote_vault': 'Q', 'decimals': 6, 'fee_raw': 0, 'qty_raw': QTY, 'cost_lamports': COST, 'basis': 'ref_size'}
        tgt.update(target)
        return {'mint': mint, 'candidate_id': None, 'start_ts': start, 'ends_ts': start + 21600, 'entered': 0, 'stage': 'screen', 'reason': '[]',
                'screen_reasons': '[]', 'holders_checked': 0, 'features': json.dumps(feats), 'target': json.dumps(tgt), 'carried_from': None,
                'code_version': 'c', 'strategy_version': 's'}

    def store(self, name, rows):
        store = L.PathStore(self.dir / name)
        for mint, start, scales, target in rows:
            if scales is None:
                continue
            store.insert_path(self.start_row(mint, start, **target))
            store.add(marks=[(mint, start + 15 * k, 1, BASE, int(QUOTE0 * D(str(s))), None, None, 'OK') for k, s in enumerate(scales)])
        store.close()
        c = sqlite3.connect((self.dir / name).as_uri() + '?mode=ro', uri=True)
        self.addCleanup(c.close)
        return c

    def test_labels_from_a_real_recorder_file_with_skips_counted(self):
        c = self.store('p.sqlite', [('W', T0, [1, 1.5], {}), ('L', T0 + 10, [1, 0.8], {}), ('BAD', T0 + 20, [1, 1], {'qty_raw': 0}),
                                    ('NOREF', T0 + 30, [1, 1], {'cost_lamports': 'x'})])
        paths, _ = R.load_paths(c)
        labels, skipped = LB.label_paths(paths, LB.references([c]), CFG)
        self.assertEqual({m: lab.label for m, lab in labels.items()}, {'W': 'WIN', 'L': 'LOSS'})
        self.assertEqual(skipped, {'LABEL_NO_REFERENCE': 2})

    def test_a_path_carried_over_two_files_gets_one_label_from_its_first_start(self):
        old = self.store('old.sqlite', [('M', T0, [1, 1.1], {})])
        new = self.store('new.sqlite', [('M', T0 + 5, [1.1, 0.8], {'qty_raw': 5 * QTY})])
        paths, skipped = R.load_paths([old, new])
        refs = LB.references([old, new])
        labels, label_skipped = LB.label_paths(paths, refs, CFG)
        self.assertEqual(list(labels), ['M'])
        self.assertEqual(refs['M'], (QTY, COST))                    # the first row's reference, like the first start
        self.assertEqual(labels['M'].start_ts, T0)
        self.assertEqual(label_skipped, {})

    def test_duplicate_paths_are_labelled_once(self):
        c = self.store('p.sqlite', [('M', T0, [1, 1.5], {})])
        paths, _ = R.load_paths(c)
        labels, skipped = LB.label_paths(paths + paths, LB.references([c]), CFG)
        self.assertEqual((len(labels), skipped), (1, {'LABEL_DUPLICATE': 1}))

    def test_nothing_is_written(self):
        c = self.store('p.sqlite', [('M', T0, [1, 1.5], {})])
        import hashlib
        before = hashlib.sha256((self.dir / 'p.sqlite').read_bytes()).hexdigest()
        paths, _ = R.load_paths(c)
        LB.label_paths(paths, LB.references([c]), CFG)
        self.assertEqual(hashlib.sha256((self.dir / 'p.sqlite').read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
