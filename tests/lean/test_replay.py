"""SYNTHETIC_TEST_ONLY: lean.replay - the offline exit-policy replay on recorded paths. Paths are written with the REAL
``lean.paths.PathStore`` schema; the decisions are the REAL ``strategy.exit_decision`` on the REAL ``adapters.mark``."""
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from lean import adapters as A, paths as L, replay as R, strategy as S

ROOT = Path(__file__).resolve().parents[2]
BASE = S.StrategyConfig.load(ROOT / 'config' / 'lean' / 'strategy-default.json')
B = 10 ** 15                    # base reserve = supply (so market cap in lamports = the quote reserve)
Q0 = 80 * 10 ** 9               # 80 SOL pool: below the 420 SOL tier edge
SIZE = 200_000_000
STEP = 15.0
TIERS = R.load_fee_tiers()


def costs(**kw):
    return R.costs_from(kw or None, TIERS)


FLAT0 = {'fee_model': 'flat', 'flat_bps': 0, 'slippage_bps': 0, 'fee_sol': '0.000001'}      # near-zero costs: analytic answers


def path_row(mint, start=1000.0, entered=1, supply=B, hours=6):
    return dict(mint=mint, candidate_id=1, start_ts=start, ends_ts=start + hours * 3600, entered=entered, stage=None, reason='[]',
                screen_reasons='[]', holders_checked=1, features=json.dumps({'supply_raw': str(supply)}), target='{}',
                carried_from=None, code_version='c', strategy_version='s')


def make_db(path, specs):
    """specs: dict(mint, ratios | marks, start, gaps, end, quote0, entered, supply, base). ``ratios`` scale the quote reserve."""
    store = L.PathStore(path)
    for s in specs:
        mint, start = s['mint'], s.get('start', 1000.0)
        store.insert_path(path_row(mint, start, s.get('entered', 1), s.get('supply', B)))
        marks = s.get('marks')
        if marks is None:
            q0, base = s.get('quote0', Q0), s.get('base', B)
            marks = [(start + STEP * i, base, int(q0 * r), 'OK') for i, r in enumerate(s['ratios'])]
        store.add(marks=[(mint, ts, i, b, q, None, None, status) for i, (ts, b, q, status) in enumerate(marks)],
                  gaps=[(mint, g, 'HTTP_429') for g in s.get('gaps', ())], ends=[(mint, start + 1, s['end'], len(marks), 0)] if s.get('end') else ())
    store.close()
    return path


def replay(ratios, policy, cost=None, lag=1, **spec):
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(Path(tmp) / 'paths.sqlite', [dict(mint='M1', ratios=ratios, **spec)])
        c = cost or costs()
        prep = R.prepare(R.load_paths(db)['M1'], c, SIZE)
        return R.replay_path(prep, policy, BASE, c, lag)


def pol(family, **params):
    return R.expand_grid([dict(family=family, **params)])[0]


class FeeTierTests(unittest.TestCase):
    def test_fixture_tiers_by_market_cap(self):
        sol = 10 ** 9
        self.assertEqual(TIERS.at(0), 125)                          # 2 + 93 + 30 bps
        self.assertEqual(TIERS.at(419 * sol), 125)
        self.assertEqual(TIERS.at(420 * sol), 120)                  # 20 + 5 + 95
        self.assertEqual(TIERS.at(1469 * sol), 120)
        self.assertEqual(TIERS.at(1470 * sol), 115)
        self.assertLess(TIERS.at(98_000 * sol), 40)                 # the top tiers are far cheaper (~0.30% at >=98k SOL)
        self.assertEqual(TIERS.bps, tuple(sorted(TIERS.bps, reverse=True)))

    def test_bad_tables_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for body in ('{', '{}', json.dumps({'parsed': {'fields': {'fee_tiers': []}}}),
                         json.dumps({'parsed': {'fields': {'fee_tiers': [{'market_cap_lamports_threshold': 5, 'fees': {'lp_fee_bps': 1, 'protocol_fee_bps': 1, 'creator_fee_bps': 1}}]}}})):
                f = Path(tmp) / 'f.json'
                f.write_text(body)
                with self.assertRaises(R.ReplayError):
                    R.load_fee_tiers(f)

    def test_costs_validation(self):
        for bad in ({'fee_model': 'x'}, {'slippage_bps': -1}, {'slippage_bps': 10000}, {'flat_bps': 1.5}, {'fee_sol': 0}, {'typo': 1}, {'fee_sol': '0.0000000001'}, []):
            with self.assertRaises(R.ReplayError, msg=bad):
                R.costs_from(bad, TIERS)
        c = R.costs_from(None, TIERS)
        self.assertEqual((c.fee_model, c.slippage_bps, c.fee_lamports), ('tier', 50, 250_000))


class GridTests(unittest.TestCase):
    def test_cartesian_expansion_and_dedupe(self):
        out = R.expand_grid([{'family': 'full_tp', 'x': [1.3, 1.5], 'stop': [0.1, 0.18]}, {'family': 'live_ladder'}, {'family': 'live_ladder'}])
        self.assertEqual(len(out), 5)
        self.assertEqual(sorted(p.name for p in out if p.family == 'full_tp')[0], 'full_tp(stop=0.1,x=1.3)')

    def test_errors(self):
        for bad in ([], 'x', [{}], [{'family': 'nope'}], [{'family': 'full_tp'}], [{'family': 'full_tp', 'x': 1.5, 'typo': 1}],
                    [{'family': 'full_tp', 'x': 1}], [{'family': 'full_tp', 'x': 0.5}], [{'family': 'hold', 'minutes': 0}],
                    [{'family': 'trailing', 'fraction': 1.2}], [{'family': 'trailing', 'fraction': 0.2, 'stop': 1}],
                    [{'family': 'partial_tp_trail', 'x': 1.5, 'sell_fraction': 1, 'fraction': .2}], [{'family': 'hold', 'minutes': 'abc'}],
                    [{'family': 'hold', 'minutes': True}], [{'family': 'live_ladder', 'stop': -1}]):
            with self.assertRaises(R.ReplayError, msg=bad):
                R.expand_grid(bad)


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))

    def digest(self, *files):
        return [(hashlib.sha256(f.read_bytes()).hexdigest(), f.stat().st_mtime_ns) for f in files]

    def test_archives_and_a_carried_path_are_one_path(self):
        make_db(self.dir / 'paths-20261011.sqlite', [dict(mint='M1', ratios=[1, 1, 1], end=None), dict(mint='M2', ratios=[1, 1.2], end='COMPLETE')])
        make_db(self.dir / 'paths.sqlite', [dict(mint='M1', ratios=[1, 1, 1, 1.1, 1.2], gaps=[1050.0]), dict(mint='M3', ratios=[1, 1], start=5000.0)])
        found = R.load_paths(self.dir / 'paths.sqlite')
        self.assertEqual(sorted(found), ['M1', 'M2', 'M3'])
        self.assertEqual([m[0] for m in found['M1'].marks], [1000.0, 1015.0, 1030.0, 1045.0, 1060.0])      # united, no duplicates
        self.assertEqual(found['M1'].gaps, [1050.0])
        self.assertEqual(found['M2'].end_why, 'COMPLETE')
        self.assertEqual(len(R.load_paths(self.dir / 'paths.sqlite', archives=False)), 2)

    def test_reading_never_modifies_the_files(self):
        a = make_db(self.dir / 'paths-20261011.sqlite', [dict(mint='M2', ratios=[1, 1.2])])
        b = make_db(self.dir / 'paths.sqlite', [dict(mint='M1', ratios=[1, 1, 1])])
        before = self.digest(a, b)
        R.load_paths(b)
        self.assertEqual(self.digest(a, b), before)                                        # the databases: same bytes, same mtime
        # a plain mode=ro open of a WAL file may leave its -wal/-shm sidecars (they are SQLite's, not data); nothing else appears
        self.assertEqual({n for n in os.listdir(self.dir) if not n.endswith(('-wal', '-shm'))}, {a.name, b.name})
        connection = R._ro(b)
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute('INSERT INTO meta VALUES(?,?)', ('k', 'v'))
        connection.close()

    def test_symlink_and_missing_files_are_refused(self):
        real = make_db(self.dir / 'real.sqlite', [dict(mint='M1', ratios=[1, 1])])
        link = self.dir / 'paths.sqlite'
        link.symlink_to(real)
        with self.assertRaises(R.ReplayError):
            R.load_paths(link)
        with self.assertRaises(R.ReplayError):
            R.load_paths(self.dir / 'missing.sqlite')

    def test_an_unreadable_row_is_skipped_not_guessed(self):
        db = make_db(self.dir / 'paths.sqlite', [dict(mint='M1', ratios=[1, 1]), dict(mint='M2', ratios=[1, 1])])
        connection = sqlite3.connect(db)
        connection.execute("UPDATE paths SET features='{' WHERE mint='M2'")
        connection.commit()
        connection.close()
        self.assertEqual(sorted(R.load_paths(db)), ['M1'])


class PrepareTests(unittest.TestCase):
    def prep(self, spec, c=None):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(Path(tmp) / 'paths.sqlite', [dict(mint='M1', **spec)])
            return R.prepare(R.load_paths(db)['M1'], c or costs(), SIZE)

    def test_unenterable_paths(self):
        self.assertIsNone(self.prep(dict(ratios=[1])))                                  # nothing after the entry
        self.assertIsNone(self.prep(dict(marks=[(1000.0, B, Q0, 'ACCOUNT_MISSING'), (1015.0, B, Q0, 'MALFORMED')])))
        self.assertIsNone(self.prep(dict(ratios=[1, 1], supply=0)))
        self.assertIsNone(self.prep(dict(ratios=[0, 0])))                                # a drained pool cannot be entered

    def test_missing_marks_are_dropped_not_filled(self):
        p = self.prep(dict(marks=[(1000.0, B, Q0, 'OK'), (1015.0, None, None, 'ACCOUNT_MISSING'), (1030.0, B, Q0, 'OK'), (1045.0, B, 0, 'EMPTY_POOL')]))
        self.assertEqual(p.ts, [1000.0, 1030.0, 1045.0])

    def test_entry_quantity_and_cost_known_answer(self):
        c = costs(fee_model='flat', flat_bps=100, slippage_bps=50, fee_sol='0.0001')
        p = self.prep(dict(ratios=[1, 1]), c)
        spent = SIZE * 9900 // 10000
        out = B * spent // (Q0 + spent)
        self.assertEqual(p.qty0, out * 9950 // 10000)
        self.assertEqual(p.cost0, SIZE + 100_000)

    def test_tier_follows_the_market_cap_of_each_mark(self):
        sol = 10 ** 9
        p = self.prep(dict(ratios=[1, 6, 25]))                                           # 80 SOL, 480 SOL, 2000 SOL market caps
        self.assertEqual(p.bps, [125, 120, 115])


class ReplayTests(unittest.TestCase):
    def test_stop_fills_at_the_next_mark_with_lag_one_and_at_the_decision_mark_with_lag_zero(self):
        ratios = [1, 1, 0.9, 0.7, 0.5, 0.5]
        t1 = replay(ratios, pol('live_ladder'), lag=1)
        t0 = replay(ratios, pol('live_ladder'), lag=0)
        self.assertEqual(t1.reasons, ['STOP'])
        self.assertEqual([f[0] for f in t1.fills], [1000.0 + STEP * 4])                  # triggered at mark 3, filled at mark 4
        self.assertEqual([f[0] for f in t0.fills], [1000.0 + STEP * 3])
        self.assertLess(t1.ret, t0.ret)                                                  # the next mark is lower: the lag costs money
        self.assertAlmostEqual(t1.past_trigger[0], 0.5 / 0.7 - 1, places=2)
        self.assertEqual(t0.past_trigger, [])

    def test_full_ladder_then_trailing(self):
        t = replay([1, 1.5, 1.52, 2.1, 2.15, 3.1, 3.2, 3.3, 2.2, 2.0], pol('live_ladder'), cost=costs(**FLAT0), lag=0)
        self.assertEqual(t.reasons, ['TAKE_PROFIT', 'TAKE_PROFIT', 'TAKE_PROFIT', 'TRAILING_STOP'])
        self.assertGreater(t.ret, 1.0)

    def test_full_take_profit_and_hold_and_time_cap(self):
        c = costs(**FLAT0)
        tp = replay([1, 1.2, 1.4, 1.6, 1.6, 1.0], pol('full_tp', x=1.5), cost=c, lag=0)
        self.assertEqual(tp.reasons, ['TAKE_PROFIT'])
        self.assertAlmostEqual(tp.ret, 0.6, delta=0.02)               # 1.6x less the price impact of our own size
        hold = replay([1] * 5 + [1.3] * 10, pol('hold', minutes=1), cost=c, lag=0)
        self.assertEqual(hold.reasons, ['MAX_HOLD'])
        self.assertEqual(hold.fills[0][0] - 1000.0, 60.0)                                # >= 60 s after the entry mark
        cap = replay([1] * 20, pol('tp_time_cap', x=2, minutes=2), cost=c, lag=0)
        self.assertEqual(cap.reasons, ['MAX_HOLD'])
        self.assertEqual(replay([1, 1, 2.5, 1], pol('tp_time_cap', x=2, minutes=2), cost=c, lag=0).reasons, ['TAKE_PROFIT'])

    def test_stop_is_off_unless_asked_for(self):
        crash = [1, 0.5, 0.3, 0.3, 0.3]
        self.assertEqual(replay(crash, pol('hold', minutes=1000), cost=costs(**FLAT0), lag=0).reasons, ['PATH_END'])
        self.assertEqual(replay(crash, pol('hold', minutes=1000, stop=0.2), cost=costs(**FLAT0), lag=0).reasons, ['STOP'])

    def test_trailing_from_the_entry(self):
        c = costs(**FLAT0)
        up_down = replay([1, 1.4, 2.0, 1.9, 1.5, 1.4, 1.4], pol('trailing', fraction=0.2), cost=c, lag=0)
        self.assertEqual(up_down.reasons, ['TRAILING_STOP'])
        self.assertAlmostEqual(up_down.ret, 0.5, places=1)                               # 2.0 peak * 0.8 = 1.6 -> first mark under it is 1.5
        self.assertEqual(replay([1, 1.1, 1.2, 1.3, 1.3], pol('trailing', fraction=0.2), cost=c, lag=0).reasons, ['PATH_END'])

    def test_partial_take_profit_then_trailing(self):
        t = replay([1, 1.2, 1.5, 1.9, 2.4, 1.8, 1.6], pol('partial_tp_trail', x=1.4, sell_fraction=0.5, fraction=0.2), cost=costs(**FLAT0), lag=0)
        self.assertEqual(t.reasons, ['TAKE_PROFIT', 'TRAILING_STOP'])
        self.assertAlmostEqual(t.fills[0][1] / (t.fills[0][1] + t.fills[1][1]), 0.5, places=3)

    def test_recorded_gaps_are_missing_data_not_flat_prices(self):
        # the price crashed and recovered entirely inside a gap: the replay never saw a stop price, and says so
        marks = [(1000.0, B, Q0, 'OK'), (1015.0, B, Q0, 'OK'), (1090.0, B, Q0, 'OK'), (1105.0, B, Q0, 'OK')]
        seen = replay(None, pol('live_ladder'), cost=costs(**FLAT0), lag=0, marks=marks, gaps=[1030.0, 1045.0, 1060.0, 1075.0])
        self.assertEqual(seen.reasons, ['PATH_END'])
        self.assertEqual(seen.marks_used, 3)                                              # only real marks are decision points

    def test_a_fill_across_a_gap_is_counted(self):
        marks = [(1000.0, B, Q0, 'OK'), (1015.0, B, int(Q0 * .9), 'OK'), (1030.0, B, int(Q0 * .7), 'OK'), (1100.0, B, int(Q0 * .6), 'OK')]
        t = replay(None, pol('live_ladder'), lag=1, marks=marks, gaps=[1045.0, 1060.0, 1075.0])
        self.assertEqual((t.reasons, t.crossed_gap), (['STOP'], 1))
        self.assertEqual(t.fills[0][0], 1100.0)

    def test_account_missing_marks_are_skipped_and_empty_pool_is_a_total_loss(self):
        marks = [(1000.0, B, Q0, 'OK'), (1015.0, None, None, 'ACCOUNT_MISSING'), (1030.0, B, Q0, 'OK'), (1045.0, B, 0, 'EMPTY_POOL'), (1060.0, B, 0, 'EMPTY_POOL')]
        t = replay(None, pol('live_ladder'), lag=1, marks=marks)
        self.assertEqual(t.reasons, ['STOP'])
        self.assertLess(t.ret, -0.99)                                                     # the drained pool is worth nothing, the fee is still paid
        self.assertEqual(t.fills[0][2], -250_000)

    def test_a_path_that_ends_open_is_closed_at_its_last_mark_and_counted(self):
        t = replay([1, 1.05, 1.05, 1.05], pol('live_ladder'))
        self.assertEqual((t.reasons, t.path_end), (['PATH_END'], True))

    def test_cost_model_known_answer(self):
        c = costs(fee_model='flat', flat_bps=100, slippage_bps=50, fee_sol='0.0001')
        t = replay([1, 1, 0.5], pol('live_ladder'), cost=c, lag=0)
        spent = SIZE * 9900 // 10000
        qty = (B * spent // (Q0 + spent)) * 9950 // 10000
        q1 = int(Q0 * .5)
        expected = q1 * qty // (B + qty) * 9900 // 10000 * 9950 // 10000 - 100_000
        self.assertEqual(t.fills[0][2], expected)
        self.assertEqual(t.pnl_lamports, expected - (SIZE + 100_000))
        self.assertAlmostEqual(t.ret, t.pnl_lamports / (SIZE + 100_000))

    def test_the_tier_fee_changes_the_result(self):
        small = replay([1, 1.3, 1.6], pol('full_tp', x=1.5), lag=0, quote0=80 * 10 ** 9)          # 125 bps tier
        big = replay([1, 1.3, 1.6], pol('full_tp', x=1.5), lag=0, quote0=1000 * 10 ** 9)          # 115 bps tier
        self.assertEqual(small.reasons, big.reasons)
        self.assertGreater(big.ret, small.ret)

    def test_decisions_are_the_real_exit_decision_on_the_real_mark(self):
        calls, marks = [], []
        real_decision, real_mark = S.exit_decision, A.mark

        def spy_decision(position, mark, quote, now, cfg, **kw):
            calls.append((mark, now, kw['mark_at']))
            return real_decision(position, mark, quote, now, cfg, **kw)

        def spy_mark(qty, base, quote, **kw):
            marks.append((base, quote, kw['pool_fee_bps']))
            return real_mark(qty, base, quote, **kw)
        with mock.patch.object(S, 'exit_decision', spy_decision), mock.patch.object(A, 'mark', spy_mark):
            replay([1, 1.1, 1.2, 1.1], pol('live_ladder'), lag=1)
        self.assertEqual(len(calls), 3)                                                    # one decision per mark after the entry
        self.assertTrue(all(now == at for _, now, at in calls))
        self.assertIn((B, int(Q0 * 1.1), 125), marks)                                       # the stored raw reserves and the tier fee

    def test_replay_is_deterministic_and_does_not_mutate_the_prepared_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(Path(tmp) / 'paths.sqlite', [dict(mint='M1', ratios=[1, 1.2, 1.6, 0.9, 0.5])])
            c = costs()
            prep = R.prepare(R.load_paths(db)['M1'], c, SIZE)
            snapshot = (list(prep.ts), list(prep.base), list(prep.quote), prep.qty0, prep.cost0)
            first = [R.replay_path(prep, p, BASE, c) for p in R.expand_grid([{'family': 'live_ladder'}, {'family': 'full_tp', 'x': [1.4, 1.5]}])]
            second = [R.replay_path(prep, p, BASE, c) for p in R.expand_grid([{'family': 'live_ladder'}, {'family': 'full_tp', 'x': [1.4, 1.5]}])]
            self.assertEqual(first, second)
            self.assertEqual(snapshot, (list(prep.ts), list(prep.base), list(prep.quote), prep.qty0, prep.cost0))


if __name__ == '__main__':
    unittest.main()
