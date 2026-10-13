"""SYNTHETIC_TEST_ONLY: the selection learner (lean/learn.py). Fixtures only, no network.

Rows are built directly (label, entry-time features, simulated PnL) so the statistics can be checked against hand arithmetic; the
planted-structure and pure-noise datasets come from a seeded generator. The full pipeline on the real runner is in test_e2e_learn."""
import contextlib
import datetime
import hashlib
import io
import json
import os
import random
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lean import features as F, labels as LB, learn as LN, strategy as S, wallet_signals as W
from lean.store import Store
from tests.lean import test_wallet_signals as WS
from tests.lean.test_labels import RecorderMixin

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / 'config' / 'lean' / 'strategy-default.json'
T0 = 19675 * 86400 + 3600
NOW = T0 + 10 ** 6
NAMES = ('dev_holding_pct', 'top10_pct', 'tx_count_first_10m')
STRATEGY = S.StrategyConfig.load(DEFAULT)
SMALL = dict(min_n=15, min_confirm=10, min_bucket=5)                  # the production minimums are 50 / 30 / 20; the maths is the same


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def lab(mint, name, ts, end_return=0.0):
    return LB.Label(mint, name, LB.LABEL_VERSION, ts, 10, None if name == 'FLAT' else 30.0, 'PRICE' if name == 'RUG' else None,
                    0.5 if name == 'WIN' else 0.05, -0.9 if name == 'RUG' else -0.1, 30.0, 30.0, end_return)


def row(i, name='FLAT', pnl_sol=0.0, feats=None, enterable=True, ts=None):
    ts = T0 + 100 * i if ts is None else ts
    f = {n: None for n in NAMES}
    f.update({k: None if v is None else D(str(v)) for k, v in (feats or {}).items()})
    return LN.Row('M%05d' % i, ts, lab('M%05d' % i, name, ts, pnl_sol), f, int(round(pnl_sol * 10 ** 9)), enterable, 'CLOSED' if enterable else None)


def planted(n=600, seed=1, p_rug_hi=0.85, p_rug_lo=0.05):
    """dev_holding_pct above 15 usually rugs (half the candidates: inside the 70% reject cap); everything else is a mildly profitable
    random walk. top10 and tx count are noise. The first n rows of planted(n + k) are planted(n)."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        dev, top10, tx = rng.uniform(0, 30), rng.uniform(10, 80), rng.randint(0, 120)
        rug = rng.random() < (p_rug_hi if dev > 15 else p_rug_lo)
        if rug:
            name, pnl = 'RUG', -0.08 + rng.gauss(0, 0.01)
        else:
            pnl = 0.012 + rng.gauss(0, 0.05)
            name = 'WIN' if pnl > 0.04 else ('LOSS' if pnl < -0.03 else 'FLAT')
        rows.append(row(i, name, pnl, {'dev_holding_pct': round(dev, 2), 'top10_pct': round(top10, 2), 'tx_count_first_10m': tx}))
    return rows


def noise(n=600, seed=1, mu=0.0, sd=0.05):
    """Outcomes drawn independently of every feature; ``mu`` < 0 is the live situation (every window loses on average)."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        pnl = rng.gauss(mu, sd)
        name = rng.choice(('RUG', 'WIN', 'LOSS', 'FLAT'))
        rows.append(row(i, name, pnl, {'dev_holding_pct': round(rng.uniform(0, 30), 2), 'top10_pct': round(rng.uniform(10, 80), 2),
                                       'tx_count_first_10m': rng.randint(0, 120)}))
    return rows


def two_kinds_of_loser(n=600, seed=5, bad_above=15.0, bad=-0.10, good=-0.02):
    """Every candidate loses; dev_holding_pct > ``bad_above`` marks the BIG losers. A real signal, but trading the rest still loses."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        dev = rng.uniform(0, 30)
        pnl = (bad if dev > bad_above else good) + rng.gauss(0, 0.02)
        rows.append(row(i, 'RUG' if dev > bad_above else 'LOSS', pnl, {'dev_holding_pct': round(dev, 2), 'top10_pct': round(rng.uniform(10, 80), 2),
                                                                         'tx_count_first_10m': rng.randint(0, 120)}))
    return rows


def run(rows, **kw):
    return LN.learn(rows, **dict(dict(SMALL, now=NOW, feature_names=NAMES), **kw))


class StatsTests(unittest.TestCase):
    def test_wilson_interval(self):
        self.assertEqual(LN.wilson(0, 0), (None, None))
        lo, hi = LN.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, 3)
        self.assertAlmostEqual(hi, 0.7634, 3)
        lo, hi = LN.wilson(0, 20)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.1611, 3)
        lo, hi = LN.wilson(20, 20)
        self.assertAlmostEqual(lo, 0.8389, 3)
        self.assertEqual(hi, 1.0)
        self.assertLess(LN.wilson(50, 100)[1] - LN.wilson(50, 100)[0], LN.wilson(5, 10)[1] - LN.wilson(5, 10)[0])   # more data, a tighter interval

    def test_student_t_tail_matches_the_tables(self):
        self.assertEqual(LN.t_sf(0.0, 5), 0.5)
        self.assertAlmostEqual(LN.t_sf(1.0, 1), 0.25, 9)                       # Cauchy: atan
        self.assertAlmostEqual(LN.t_sf(-1.0, 1), 0.75, 9)
        self.assertAlmostEqual(LN.t_sf(2.228, 10), 0.025, 4)                   # the 97.5% critical values of the t table
        self.assertAlmostEqual(LN.t_sf(2.042, 30), 0.025, 4)
        self.assertAlmostEqual(LN.t_sf(2.457, 30), 0.01, 4)
        self.assertAlmostEqual(LN.t_sf(1.6448536, 10 ** 7), 0.05, 5)           # -> the normal
        self.assertAlmostEqual(LN.t_sf(2.0, 10), 0.036694, 5)
        self.assertAlmostEqual(LN.t_sf(2.5, 4.4), 1 - LN.t_sf(-2.5, 4.4), 12)

    def test_welch_is_kept_versus_rejected(self):
        w = LN.welch([1, 2, 3, 4], [0, 0, 1, 1])
        self.assertAlmostEqual(w['t'], 2 / 0.5 ** 0.5, 5)
        self.assertAlmostEqual(w['df'], 0.25 / ((5 / 12) ** 2 / 3 + (1 / 12) ** 2 / 3), 2)
        self.assertTrue(LN.t_sf(w['t'], 5) < w['p'] < LN.t_sf(w['t'], 4))
        self.assertIsNone(LN.welch([1], [1, 2]))
        self.assertEqual(LN.welch([1, 1], [0, 0])['p'], 0.0)
        self.assertEqual(LN.welch([0, 0], [1, 1])['p'], 1.0)
        self.assertEqual(LN.welch([1, 1], [1, 1])['p'], 0.5)
        losers = [-0.1, -0.08, -0.12, -0.09, -0.11] * 6                       # every candidate loses, the same way
        self.assertAlmostEqual(LN.welch(losers, losers)['p'], 0.5, 9)          # (a t-test of the rejected PnL against 0 says p ~ 1e-30)

    def test_benjamini_hochberg(self):
        q = LN.benjamini_hochberg([0.01, 0.04, 0.03, 0.005])
        for got, want in zip(q, [0.02, 0.04, 0.04, 0.02]):
            self.assertAlmostEqual(got, want, 12)
        self.assertEqual(LN.benjamini_hochberg([1.0, 1.0]), [1.0, 1.0])
        self.assertEqual(LN.benjamini_hochberg([]), [])
        self.assertEqual(LN.benjamini_hochberg([0.5, 0.9]), [0.9, 0.9])

    def test_random_rule_null_is_a_uniform_subset_of_the_same_size_and_seeded(self):
        values = list(range(1, 11))
        null = LN.RandomRuleNull({0: values}, 1000, 7)
        self.assertEqual(null.rejected_sums({0: 0}), [0] * 1000)
        self.assertEqual(set(null.rejected_sums({0: 10})), {55})
        ones = null.rejected_sums({0: 1})
        counts = {v: ones.count(v) for v in values}
        self.assertTrue(all(60 <= c <= 140 for c in counts.values()), counts)  # each value ~100 times in 1000 draws
        self.assertAlmostEqual(sum(null.rejected_sums({0: 3})) / 1000, 16.5, delta=0.6)
        self.assertEqual(null.rejected_sums({0: 4}), LN.RandomRuleNull({0: values}, 1000, 7).rejected_sums({0: 4}))
        self.assertNotEqual(null.rejected_sums({0: 4}), LN.RandomRuleNull({0: values}, 1000, 8).rejected_sums({0: 4}))
        two = LN.RandomRuleNull({0: values, 1: [100, 200]}, 1000, 7).rejected_sums({0: 10, 1: 1})
        self.assertEqual(set(two), {155, 255})                                 # the counts are kept per block (stratified)
        for bad in (999, 10.0, True):
            with self.assertRaises(LN.LearnError):
                LN.RandomRuleNull({0: values}, bad, 7)

    def test_the_null_is_beaten_by_a_real_split_and_not_by_a_random_one_in_a_losing_world(self):
        rng = random.Random(4)
        rows = [row(i, 'LOSS', rng.gauss(-0.06, 0.05), {'dev_holding_pct': i % 2}) for i in range(200)]
        null = LN.RandomRuleNull({0: [r.pnl_lamports for r in rows]}, 1000, 3)
        at_random = LN.contrast([(0, rows, [rng.random() < 0.5 for _ in rows])], null)
        self.assertGreater(at_random['gain_per_candidate_sol'], 0.02)           # rejecting at random "saves" money...
        self.assertFalse(at_random['beats_null'])                               # ...and is no better than a random rule
        self.assertGreater(at_random['perm_p'], 0.05)
        worst = sorted(rows, key=lambda r: r.pnl_lamports)[:60]
        ids = {id(r) for r in worst}
        real = LN.contrast([(0, rows, [id(r) in ids for r in rows])], null)
        self.assertTrue(real['beats_null'])
        self.assertLessEqual(real['perm_p'], 0.002)
        self.assertGreater(real['diff_kept_minus_rejected_sol'], real['null_p95_diff_sol'])


class DecisionTests(unittest.TestCase):
    def test_the_decision_is_the_last_screen_or_the_entry_after_it_never_after_the_path_start(self):
        d = [(100.0, 'screen', 1), (101.0, 'entry', 1), (200.0, 'screen', 1), (203.0, 'entry', 1), (500.0, 'screen', 1), (90.0, 'screen', 2)]
        d.sort()
        self.assertEqual(LN.decision_time(d, 1, 150.0), 101.0)
        self.assertEqual(LN.decision_time(d, 1, 204.0), 203.0)
        self.assertEqual(LN.decision_time(d, 1, 202.0), 200.0)            # the entry after the start is not the decision
        self.assertEqual(LN.decision_time(d, 2, 1000.0), 90.0)
        self.assertIsNone(LN.decision_time(d, 3, 1000.0))
        self.assertIsNone(LN.decision_time(d, 1, 99.0))

    def test_enterable_means_entered_or_blocked_only_by_the_portfolio(self):
        def p(entered, screen_reasons=(), reasons=()):
            return LB.PathRecord('M', 1, 0.0, 1.0, entered, tuple(screen_reasons), tuple(reasons), {}, {}, (), ())
        self.assertTrue(LN.enterable(p(True)))
        self.assertTrue(LN.enterable(p(False, reasons=['MAX_POSITIONS', 'ENTRY_THROTTLE'])))
        self.assertFalse(LN.enterable(p(False, reasons=['COST_BUDGET'])))
        self.assertFalse(LN.enterable(p(False, reasons=['MAX_POSITIONS', 'NO_ROUTE'])))
        self.assertFalse(LN.enterable(p(False, screen_reasons=['MARKET_CAP_ABOVE_MAX'], reasons=['MARKET_CAP_ABOVE_MAX'])))
        self.assertFalse(LN.enterable(p(False)))


class FieldGuardTests(unittest.TestCase):
    """The per-field guard on rows made by the REAL builders (lean.features.build_row; lean.wallet_signals.WalletSignals.collect in
    WriterIntegrationTests)."""

    def row(self, decision_ts=T0, late=5.0, version=None, **extra):
        payload = F.build_row(SimpleNamespace(mint='M'), SimpleNamespace(passed=True, reasons=(), features={'market_cap_usd': '90000'}),
                              entered=True, extra=extra.pop('fields', {'dev_holding_pct': '12', 'top10_pct': '40', 'tx_count_first_10m': 7}),
                              screen_at=decision_ts, collected_at=decision_ts + late, decision_ts=decision_ts,
                              flags=extra.pop('flags', {'dev_holding_pct': 'AT_COLLECTION', 'tx_count_first_10m': 'CUT_AT_DECISION'}))
        if version is not None:
            payload['features_version'] = version
        return json.loads(F.canonical(payload))

    def values(self, row, **policy):
        counts = LN._Counts()
        return LN.feature_values(row, T0, LN.LookaheadPolicy(**policy), counts), counts

    def test_an_on_time_row_is_used_whole(self):
        v, c = self.values(self.row())
        self.assertEqual((v['dev_holding_pct'], v['top10_pct'], v['tx_count_first_10m'], v['market_cap_usd']), (D(12), D(40), D(7), D(90000)))
        self.assertEqual((c.skipped, c.excluded), ({}, {}))

    def test_a_late_at_collection_snapshot_is_excluded_and_counted_the_rest_kept(self):
        v, c = self.values(self.row(late=LN.DEFAULT_MAX_LATE_S + 0.001))
        self.assertIsNone(v['dev_holding_pct'])
        self.assertEqual((v['top10_pct'], v['tx_count_first_10m']), (D(40), D(7)))              # unflagged / cut at the decision: kept
        self.assertEqual(c.excluded, {'dev_holding_pct': {'AT_COLLECTION_LATE': 1}})
        v, _ = self.values(self.row(late=LN.DEFAULT_MAX_LATE_S))
        self.assertEqual(v['dev_holding_pct'], D(12))                                          # the threshold itself is allowed
        v, _ = self.values(self.row(late=100), max_late_s=200)
        self.assertEqual(v['dev_holding_pct'], D(12))                                          # configurable

    def test_an_unknown_flag_is_post_decision_until_proven_otherwise(self):
        v, c = self.values(self.row(flags={'top10_pct': 'READ_AFTER_EXIT'}))
        self.assertIsNone(v['top10_pct'])
        self.assertEqual(c.excluded, {'top10_pct': {'FLAG_READ_AFTER_EXIT': 1}})

    def test_rows_before_v3_are_never_used(self):
        v, c = self.values(self.row(version=2))
        self.assertEqual(set(v.values()), {None})
        self.assertEqual(c.skipped, {'FEATURES_PRE_V3': 1})

    def test_the_last_row_at_or_before_the_decision_wins_and_a_later_one_never_replaces_it(self):
        a, b, later = self.row(decision_ts=T0 - 50), self.row(decision_ts=T0), self.row(decision_ts=T0 + 0.01)
        counts = LN._Counts()
        self.assertIs(LN._pick([a, b, later], T0, None, LN.LookaheadPolicy(), counts, 'FEATURES'), b)
        self.assertEqual(counts.skipped, {'FEATURES_AFTER_DECISION': 1})
        self.assertIsNone(LN._pick([later], T0, None, LN.LookaheadPolicy(), LN._Counts(), 'FEATURES'))
        b['_candidate_id'] = 7
        self.assertIs(LN._pick([a, b], T0, 7, LN.LookaheadPolicy(), LN._Counts(), 'FEATURES'), b)    # another candidate's row is ignored
        a['_candidate_id'] = 8
        self.assertIsNone(LN._pick([a], T0, 7, LN.LookaheadPolicy(), LN._Counts(), 'FEATURES'))

    def test_unusable_values_are_unknown_not_zero(self):
        v, _ = self.values(self.row(fields={'dev_holding_pct': 'abc', 'top10_pct': True, 'tx_count_first_10m': 'Infinity'}, flags={}))
        self.assertEqual({v[n] for n in ('dev_holding_pct', 'top10_pct', 'tx_count_first_10m')}, {None})

    def test_policy_validation(self):
        for bad in (dict(max_late_s=-1), dict(max_late_s=float('nan')), dict(eps_s=True)):
            with self.assertRaises(LN.LearnError):
                LN.LookaheadPolicy(**bad)


class WriterIntegrationTests(RecorderMixin, unittest.TestCase):
    """The REAL writers end to end: the trader's decisions (Store.add_decision), the L12F row writer (FeatureRecorder._write ->
    build_row), the L13 collector (WalletSignals.collect on a fake chain) and the L07R2 recorder (PathRecorder on a fake RPC), in one
    state dir; the learner reads the two files mode=ro. Every value that only exists AFTER a decision is the CANARY 666 (or a buyer
    count of 6): none may reach a learning row, and each exclusion is counted. Relaxing the guard lets the canaries in (sensitivity)."""
    CANARY = D(666)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.setup_recorder(os.path.realpath(tmp.name))
        self.lean = Store(self.dir / 'lean.sqlite', initial_cash_sol='10', code_version='t', strategy_version='lean-1', clock=self.clock.now)
        self.addCleanup(self.lean.close)
        self.chain = WS.Chain()
        self.wclock = self.fclock = 0.0                   # the collectors' own threads have their own notion of "now"
        self.signals = W.WalletSignals(store=self.lean, helius=WS.ChainHelius(self.chain), cfg={}, code_version='t', strategy_version='lean-1',
                                       clock=lambda: self.wclock, sleep=lambda s: None)
        self.feat = F.FeatureRecorder(store=self.lean, helius=None, code_version='t', strategy_version='lean-1', clock=lambda: self.fclock)
        self.expected = {}

    def slot_at(self, t):
        return int(WS.GRAD + (t - WS.GRAD_TIME) / 0.4)

    def candidate(self, i, *, late=False, supply_from_screen=True, wallet='cut', price=(1, 1.5)):
        """One candidate: decision, feature rows, wallet row, path. Returns its mint."""
        mint = WS.addr(10 + i)
        pool = WS.addr(100 + i)
        decision = self.clock.now()
        cid = self.lean.add_candidate(mint, pool=pool, slot=1, migrated_at=decision - 60, ts=decision - 1)
        self.lean.add_decision('screen', 'PASS', mint=mint, candidate_id=cid, reasons=[],
                                features={'supply_raw': str(WS.SUPPLY)} if supply_from_screen else {}, ts=decision)
        self.lean.add_decision('entry', 'SKIP', mint=mint, candidate_id=cid, reasons=['MAX_POSITIONS'], ts=decision)
        # the wallet chain: two same-slot buyers + one 8 s later BEFORE the decision; three more buyers 20 s AFTER it
        grad = self.slot_at(decision - 60)
        a, b, c = WS.addr(150 + 3 * i), WS.addr(151 + 3 * i), WS.addr(152 + 3 * i)
        self.chain.swap(grad, {a: 10 ** 12, b: 10 ** 12}, mint=mint, pool=pool)
        self.chain.swap(grad + 20, {c: 10 ** 12}, mint=mint, pool=pool)
        self.chain.swap(self.slot_at(decision + 20), {WS.addr(200 + k): 10 ** 13 for k in range(3)}, mint=mint, pool=pool)
        # the path: the recorder starts 1.5 s after the decision
        mint_, _ = self.start(cid=cid, mint=mint)
        # the feature row the recorder writes for this decision (5 s late, or 100 s late: its dev snapshot is then post-decision)
        lateness = 100.0 if late else 5.0
        self.fclock = decision + lateness
        dev = self.CANARY if late else D(i) / 4 + 1
        self.feat._write(SimpleNamespace(mint=mint), cid, SimpleNamespace(passed=True, reasons=(), features={'market_cap_usd': str(90000 + i)}),
                         False, 'MAX_POSITIONS', decision, decision, extra={'dev_holding_pct': str(dev), 'tx_count_first_10m': 3 + i},
                         flags={'dev_holding_pct': 'AT_COLLECTION', 'tx_count_first_10m': 'CUT_AT_DECISION'}, calls=2)
        # a re-screen 60 s later wrote another row (its decision is after this candidate's): canary everywhere
        self.feat._write(SimpleNamespace(mint=mint), cid, SimpleNamespace(passed=True, reasons=(), features={'market_cap_usd': '666'}),
                         False, 'MAX_POSITIONS', decision + 60, decision + 60, extra={'dev_holding_pct': '666', 'tx_count_first_10m': 666})
        # the wallet collector runs ~125 s after graduation
        self.wclock = decision + 70
        self.signals.collect(cid, mint, pool, grad, decision_ts=None if wallet == 'unknown' else decision)
        self.walk(mint_, list(price))
        self.expected[mint] = {'dev': None if late else D(i) / 4 + 1, 'tx': D(3 + i), 'mc': D(90000 + i), 'wallet': wallet,
                               'supply': supply_from_screen}
        self.clock.t += 300
        return mint

    def build(self, **policy):
        return LN.build_dataset(self.dir / 'lean.sqlite', self.dir / 'paths.sqlite', STRATEGY, pool_fee_bps=25,
                                policy=LN.LookaheadPolicy(**policy))

    def scenario(self):
        mints = [self.candidate(0), self.candidate(1, late=True), self.candidate(2, supply_from_screen=False),
                 self.candidate(3, wallet='unknown'), self.candidate(4, price=(1, 0.7))]
        self.lean.close()
        self.rec.db.close()
        return mints

    def test_no_post_decision_value_reaches_a_learning_row(self):
        mints = self.scenario()
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.dir / 'lean.sqlite', self.dir / 'paths.sqlite')}
        ds = self.build()
        rows = {r.mint: r for r in ds.rows}
        self.assertEqual(sorted(rows), sorted(mints))
        for mint in mints:
            row, exp = rows[mint], self.expected[mint]
            for name, value in row.features.items():
                self.assertNotEqual(value, self.CANARY, (mint, name))
            self.assertEqual(row.features['dev_holding_pct'], exp['dev'])
            self.assertEqual(row.features['tx_count_first_10m'], exp['tx'])
            self.assertEqual(row.features['market_cap_usd'], exp['mc'])
            if exp['wallet'] == 'unknown':                                           # the window was not cut: nothing of it is used
                self.assertEqual({row.features[n] for n in LN.WALLET_FEATURES}, {None})
                continue
            self.assertEqual((row.features['same_slot_buyers'], row.features['sniper_count'], row.features['smart_buyers_count']),
                             (D(2), D(2), D(0)))
            self.assertEqual(row.features['bundle_score'], D('0.6667'))             # 2 bundled of the 3 PRE-decision buyers, never of 6
            if exp['supply']:
                self.assertEqual(row.features['bundled_supply_pct'], D(2 * 10 ** 12 * 100) / WS.SUPPLY)
            else:
                self.assertIsNone(row.features['bundled_supply_pct'])               # the supply was read after the decision
            self.assertEqual(row.ts, row.label.decision_ts)
        self.assertEqual(rows[mints[0]].label.label, 'WIN')
        self.assertEqual(rows[mints[4]].label.label, 'LOSS')
        self.assertEqual(ds.stats['skipped'], {'FEATURES_AFTER_DECISION': 5, 'WALLET_SIGNALS_NO_DECISION_TS': 1})
        self.assertEqual(ds.stats['excluded'], {'dev_holding_pct': {'AT_COLLECTION_LATE': 1},
                                                'bundled_supply_pct': {'SUPPLY_READ_AFTER_DECISION': 1}})
        for path, digest in before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_the_guard_is_what_keeps_the_canaries_out(self):
        mints = self.scenario()
        lax = {r.mint: r for r in self.build(max_late_s=10 ** 6).rows}
        self.assertEqual(lax[mints[1]].features['dev_holding_pct'], self.CANARY)               # the late snapshot
        with mock.patch.object(LN, '_pick', lambda rows, *a, **k: rows[-1] if rows else None):
            picked = {r.mint: r for r in self.build().rows}
        self.assertEqual(picked[mints[0]].features['tx_count_first_10m'], self.CANARY)        # the later re-screen row
        self.assertIsNone(picked[mints[3]].features['bundle_score'])                          # 2nd layer: the uncut window is still out
        raw = lambda row, *a, **k: {n: None if row is None else LN._to_decimal(row.get(n)) for n in LN.WALLET_FEATURES}
        with mock.patch.object(LN, '_pick', lambda rows, *a, **k: rows[-1] if rows else None), mock.patch.object(LN, 'wallet_values', raw):
            unguarded = {r.mint: r for r in self.build().rows}
        self.assertEqual(unguarded[mints[3]].features['bundle_score'], D('0.3333'))           # 2 of 6 buyers: the window ran past the decision
        self.assertIsNotNone(unguarded[mints[2]].features['bundled_supply_pct'])              # the supply read after the decision

    def test_a_path_with_too_many_gaps_never_becomes_a_learning_row(self):
        good = self.candidate(0)
        gappy = self.candidate(1, price=('gap', 'gap', 'gap', 1, 1.5))           # real path_gaps rows (503s) before the first marks
        self.lean.close()
        self.rec.db.close()
        ds = self.build()
        self.assertEqual([r.mint for r in ds.rows], [good])
        self.assertEqual(ds.stats['labels'], {'INSUFFICIENT_PATH': 1, 'WIN': 1})
        self.assertEqual(ds.stats['skipped']['INSUFFICIENT_PATH:TOO_MANY_GAPS'], 1)
        loose = LN.build_dataset(self.dir / 'lean.sqlite', self.dir / 'paths.sqlite', STRATEGY, pool_fee_bps=25,
                                 label_cfg=LB.LabelConfig.from_strategy(STRATEGY, max_gap_fraction=0.9))
        self.assertEqual(sorted(r.mint for r in loose.rows), sorted([good, gappy]))

    def test_every_wallet_field_check_on_a_real_collected_row(self):
        self.candidate(0)
        raw = json.loads(self.lean.db.execute("SELECT raw FROM observations WHERE kind='wallet_signals'").fetchone()[0])
        self.assertEqual((raw['version'], raw['window_after_decision_s'], raw['supply_source']), (LN.WALLET_VERSION, 0.0, 'screen'))

        def vals(**over):
            counts = LN._Counts()
            return LN.wallet_values({**raw, **over}, raw['decision_ts'], LN.LookaheadPolicy(), counts), counts
        v, c = vals()
        self.assertEqual((v['bundle_score'], v['smart_buyers_count'], c.excluded), (D('0.6667'), D(0), {}))
        for after in (None, 12.0):
            v, c = vals(window_after_decision_s=after)
            self.assertEqual(set(v.values()), {None})
            self.assertEqual(set(c.excluded), {'bundle_score', 'sniper_count', 'same_slot_buyers', 'bundled_supply_pct', 'smart_buyers_count'})
        for asof in (raw['decision_ts'] + 5, None):
            v, c = vals(smart_asof_ts=asof)
            self.assertIsNone(v['smart_buyers_count'])
            self.assertEqual(c.excluded, {'smart_buyers_count': {'SMART_TABLE_NOT_AS_OF_DECISION': 1}})
            self.assertEqual(v['bundle_score'], D('0.6667'))
        v, c = vals(version='wallet_signals_v1')
        self.assertEqual((set(v.values()), c.skipped), ({None}, {'WALLET_SIGNALS_OLD_VERSION': 1}))

    def test_a_label_never_reads_a_mark_before_the_decision(self):
        mints = self.scenario()
        ds = self.build()
        c = sqlite3.connect((self.dir / 'paths.sqlite').as_uri() + '?mode=ro', uri=True)
        self.addCleanup(c.close)
        for row in ds.rows:
            first = c.execute('SELECT MIN(ts) FROM path_marks WHERE mint=?', (row.mint,)).fetchone()[0]
            self.assertGreater(first, row.ts)
            self.assertEqual(row.label.n_marks, c.execute('SELECT COUNT(*) FROM path_marks WHERE mint=? AND ts>=?', (row.mint, row.ts)).fetchone()[0])
        self.assertEqual(len(ds.rows), len(mints))


class PlanTests(unittest.TestCase):
    TS = [T0 + 100.0 * i for i in range(200)]

    def test_confirm_is_the_newest_block_dev_is_older_and_folds_roll_forward(self):
        p = LN.plan_windows(self.TS, confirm_fraction=0.3, min_confirm=10, min_n=15)
        self.assertEqual((p['n_confirm'], p['confirm'], p['dev'], p['purged']), (60, (140, 200), 140, 0))
        self.assertEqual([f['test'] for f in p['folds']], [(28, 56), (56, 84), (84, 112), (112, 140)])   # block 0 only trains
        self.assertEqual([f['train'] for f in p['folds']], [(0, 28), (0, 56), (0, 84), (0, 112)])        # the past only
        self.assertTrue(p['feasible'] and p['fresh'])
        self.assertEqual(p['why'], [])

    def test_purge_and_embargo_are_the_label_horizon_and_no_tighter(self):
        emb = 1000.0
        p = LN.plan_windows(self.TS, confirm_fraction=0.3, min_confirm=10, min_n=15, embargo_s=emb)
        c0 = p['confirm'][0]
        self.assertEqual((p['dev'], p['purged']), (131, 9))
        self.assertTrue(all(self.TS[i] + emb <= self.TS[c0] for i in range(p['dev'])))       # no dev label runs into the confirm block
        self.assertGreater(self.TS[p['dev']] + emb, self.TS[c0])                            # ... and only those are purged
        for f in p['folds']:
            a, (t0, t1) = f['test'][0], f['train']
            self.assertTrue(all(self.TS[i] + emb <= self.TS[a] for i in range(t0, t1)))
            self.assertGreater(self.TS[t1] + emb, self.TS[a])
            self.assertEqual(f['n_train'], t1 - t0)

    def test_the_support_adapts_to_the_data_so_no_configuration_is_impossible(self):
        self.assertEqual([LN.support(n) for n in (0, 60, 100, 300, 1000)], [10, 10, 10, 30, 50])
        self.assertEqual(LN.support(60, min_n=4), 4)                          # min_n is the ceiling, and the floor when it is lower
        self.assertEqual(LN.support(300, min_n=15), 15)
        self.assertEqual(LN.support(120, min_n=15), 12)
        # the L19 dead end: select = 60 rows, min_n 50, reject cap 80% -> no rule could ever qualify. Now a 60-row training set needs 10.
        self.assertLessEqual(LN.support(60), LN.MAX_REJECT_FRACTION * 60)
        feasible_since = None
        for n in range(0, 700, 3):
            ts = [T0 + 400.0 * i for i in range(n)]
            p = LN.plan_windows(ts, embargo_s=3600)
            for f in p['folds']:
                if f['feasible']:
                    self.assertLessEqual(f['min_n'], LN.MAX_REJECT_FRACTION * f['n_train'])
            if p['feasible'] and feasible_since is None:
                feasible_since = n
            if feasible_since is not None:
                self.assertTrue(p['feasible'], n)                               # more data never makes it infeasible again
        self.assertIsNotNone(feasible_since)
        self.assertLess(feasible_since, 200)

    def test_a_confirm_block_that_was_already_tested_is_not_fresh(self):
        p = LN.plan_windows(self.TS, confirm_fraction=0.3, min_confirm=10, min_n=15, consumed_until=self.TS[150])
        self.assertTrue(p['feasible'])
        self.assertFalse(p['fresh'])
        self.assertIn('CONFIRM_BLOCK_ALREADY_TESTED', p['why'])
        self.assertTrue(LN.plan_windows(self.TS, confirm_fraction=0.3, min_confirm=10, min_n=15, consumed_until=self.TS[139] - 1)['fresh'])
        self.assertFalse(LN.plan_windows(self.TS, confirm_fraction=0.3, min_confirm=10, min_n=15, consumed_until=self.TS[139] - 1,
                                         embargo_s=200)['fresh'])               # the embargo also separates a new block from the old one

    def test_too_few_candidates(self):
        for n in (0, 1, 59, 60):
            p = LN.plan_windows(self.TS[:n])
            self.assertFalse(p['feasible'])
            self.assertTrue(p['why'][0].startswith('CONFIRM_BLOCK_NEEDS_60'), p['why'])

    def test_eta_is_the_first_feasible_fresh_confirm_at_the_observed_rate(self):
        ts = [T0 + 100.0 * i for i in range(50)]                               # 36 enterable candidates per hour
        eta = LN.confirm_eta(ts)
        self.assertAlmostEqual(eta['rate_per_hour'], 36.0, 6)
        extra = next(x for x in range(1, 1000)
                     if LN.plan_windows(ts + [ts[-1] + 100.0 * (j + 1) for j in range(x)])['feasible'])
        self.assertEqual(eta['needed_enterable'], 50 + extra)
        self.assertAlmostEqual(eta['eta_hours'], round(extra / 36.0, 2), 6)
        self.assertEqual(eta['needed_enterable'], 85)                          # confirm 60 (2 x 30) + 25 dev for one 20-row training set
        self.assertEqual(LN.confirm_eta(ts[:85] if len(ts) >= 85 else [T0 + 100.0 * i for i in range(85)])['eta_hours'], 0.0)
        self.assertEqual(LN.confirm_eta([T0])['eta_hours'], None)
        self.assertEqual(LN.confirm_eta([])['eta_hours'], None)
        ledger = LN.confirm_eta([T0 + 100.0 * i for i in range(300)], consumed_until=T0 + 100.0 * 299, embargo_s=3600)
        p = LN.plan_windows([T0 + 100.0 * i for i in range(ledger['needed_enterable'])], consumed_until=T0 + 100.0 * 299, embargo_s=3600)
        self.assertTrue(p['feasible'] and p['fresh'])
        self.assertGreater(ledger['eta_hours'], 1.0)                           # a whole new block after the tested one plus the embargo


class EvaluateTests(unittest.TestCase):
    def test_kept_versus_rejected_and_the_gain_over_a_random_rule(self):
        rows = [row(0, 'RUG', -0.08, {'dev_holding_pct': 20}), row(1, 'RUG', -0.06, {'dev_holding_pct': 12}), row(2, 'WIN', 0.10, {'dev_holding_pct': 2}),
                row(3, 'FLAT', 0.0, {'dev_holding_pct': 1}), row(4, 'WIN', 0.04, {'dev_holding_pct': None})]
        rule = LN.Rule((LN.Condition('dev_holding_pct', '>', D(8)),))
        m = LN.evaluate(rows, [rule])
        self.assertEqual((m['n'], m['n_rejected'], m['n_kept'], m['reject_fraction'], m['coverage']), (5, 2, 3, 0.4, 0.6))
        self.assertAlmostEqual(m['baseline_mean_pnl_sol'], 0.0, 9)
        self.assertAlmostEqual(m['policy_mean_pnl_sol'], (0.10 + 0.0 + 0.04) / 5, 9)
        self.assertAlmostEqual(m['gain_per_candidate_sol'], 0.14 / 5, 9)
        self.assertAlmostEqual(m['gain_over_random_sol'], (2 * 0.0 / 5 + 0.14) / 5, 9)
        self.assertAlmostEqual(m['kept_mean_pnl_sol'], 0.14 / 3, 9)
        self.assertAlmostEqual(m['rejected_mean_pnl_sol'], -0.07, 9)
        self.assertAlmostEqual(m['diff_kept_minus_rejected_sol'], 0.14 / 3 + 0.07, 9)
        self.assertEqual(m['welch_t'], LN.welch([0.10, 0.0, 0.04], [-0.08, -0.06])['t'])
        self.assertEqual((m['rug_rate_rejected'], m['rug_rate_kept'], m['win_rate_kept']), (1.0, 0.0, round(2 / 3, 6)))
        self.assertNotIn('rejected_t', m)                                        # the t-test of the rejected PnL against 0 is gone

    def test_in_a_losing_world_rejecting_saves_money_but_gains_nothing_over_random(self):
        rows = [row(i, 'LOSS', -0.05, {'dev_holding_pct': i}) for i in range(100)]
        m = LN.evaluate(rows, [LN.Rule((LN.Condition('dev_holding_pct', '>', D(49)),))])
        self.assertAlmostEqual(m['gain_per_candidate_sol'], 0.025, 9)            # the old objective: a "gain"
        self.assertAlmostEqual(m['gain_over_random_sol'], 0.0, 9)
        self.assertAlmostEqual(m['diff_kept_minus_rejected_sol'], 0.0, 9)
        self.assertEqual(m['welch_p'], 0.5)

    def test_an_unknown_value_never_matches(self):
        r = row(0, 'RUG', -0.1, {'dev_holding_pct': None})
        for op in ('>', '<='):
            self.assertFalse(LN.Rule((LN.Condition('dev_holding_pct', op, D(8)),)).matches(r))

    def test_ops_are_complementary_on_known_values(self):
        r = row(0, feats={'dev_holding_pct': 8})
        self.assertFalse(LN.Condition('dev_holding_pct', '>', D(8)).holds(r))
        self.assertTrue(LN.Condition('dev_holding_pct', '<=', D(8)).holds(r))

    def test_empty_rule_set_rejects_nothing(self):
        m = LN.evaluate([row(0, 'RUG', -0.1)], [])
        self.assertEqual((m['n_rejected'], m['gain_per_candidate_sol'], m['gain_over_random_sol'], m['diff_kept_minus_rejected_sol']), (0, 0.0, 0.0, None))
        self.assertEqual(LN.evaluate([], [])['n'], 0)

    def test_rule_text_and_dict(self):
        rule = LN.Rule((LN.Condition('dev_holding_pct', '>', D('8.0')), LN.Condition('top10_pct', '<=', D('45'))))
        self.assertEqual(rule.text(), 'dev_holding_pct > 8 AND top10_pct <= 45')
        self.assertEqual(rule.to_dict()['if'][0], {'feature': 'dev_holding_pct', 'op': '>', 'value': '8'})
        self.assertEqual(rule.to_dict()['action'], 'REJECT')


class MineTests(unittest.TestCase):
    def test_recovers_the_planted_single_condition(self):
        found = LN.mine(planted()[:300], NAMES, min_n=15)
        self.assertTrue(found)
        top = found[0][0]
        self.assertEqual([c.feature for c in top.conditions][0], 'dev_holding_pct')
        self.assertEqual(top.conditions[0].op, '>')
        self.assertTrue(10 <= top.conditions[0].value <= 20, top.text())
        self.assertGreater(found[0][1]['train_gain_over_random_sol'], 0.01)

    def test_recovers_a_planted_two_condition_rule(self):
        rng = random.Random(7)
        rows = []
        for i in range(800):
            dev, top10 = rng.uniform(0, 30), rng.uniform(10, 80)
            rug = dev > 8 and top10 > 45 and rng.random() < 0.9
            pnl = -0.08 + rng.gauss(0, 0.01) if rug else 0.01 + rng.gauss(0, 0.04)
            rows.append(row(i, 'RUG' if rug else 'FLAT', pnl, {'dev_holding_pct': round(dev, 2), 'top10_pct': round(top10, 2)}))
        found = LN.mine(rows[:400], NAMES, min_n=15)
        top = found[0][0]
        self.assertEqual(sorted(c.feature for c in top.conditions), ['dev_holding_pct', 'top10_pct'])
        by_feature = {c.feature: c for c in top.conditions}
        self.assertEqual((by_feature['dev_holding_pct'].op, by_feature['top10_pct'].op), ('>', '>'))
        pair_gain = found[0][1]['train_gain_over_random_sol']
        singles = [m['train_gain_over_random_sol'] for r, m in found if len(r.conditions) == 1]
        self.assertTrue(singles and pair_gain > max(singles))

    def test_minimum_support_is_enforced(self):
        rows = planted(200)[:100]
        for rule, stats in LN.mine(rows, NAMES, min_n=40):
            self.assertGreaterEqual(stats['train_rejected'], 40)
        self.assertEqual(LN.mine(rows, NAMES, min_n=10 ** 6), [])

    def test_a_rule_that_rejects_winners_is_never_mined(self):
        rows = [row(i, 'WIN', 0.05, {'dev_holding_pct': i}) for i in range(100)]
        self.assertEqual(LN.mine(rows, NAMES, min_n=5), [])

    def test_rejecting_average_losers_is_not_a_gain(self):
        """Every candidate loses the same: the old objective (-sum of the rejected PnL) found a "gain" in every rule."""
        rows = [row(i, 'LOSS', -0.05, {'dev_holding_pct': i, 'top10_pct': 100 - i}) for i in range(100)]
        self.assertEqual(LN.mine(rows, NAMES, min_n=5), [])

    def test_deterministic_order_and_top_k(self):
        rows = planted()[:300]
        a, b = LN.mine(rows, NAMES, min_n=15, top_k=7), LN.mine(rows, NAMES, min_n=15, top_k=7)
        self.assertEqual([(r.text(), s) for r, s in a], [(r.text(), s) for r, s in b])
        self.assertLessEqual(len(a), 7)
        gains = [s['train_gain_over_random_sol'] for _, s in a]
        self.assertEqual(gains, sorted(gains, reverse=True))

    def test_empty_and_constant_features(self):
        self.assertEqual(LN.mine([], NAMES), [])
        rows = [row(i, 'RUG', -0.1 - i / 1000, {'dev_holding_pct': 5}) for i in range(60)]
        self.assertEqual(LN.mine(rows, NAMES, min_n=5), [])           # nothing to split on: every rule would reject everything

    def test_a_rule_that_stops_trading_is_not_a_selection_rule(self):
        rows = [row(i, 'RUG', -0.1 - i / 1000, {'dev_holding_pct': i}) for i in range(100)]   # every candidate loses, the later ones more
        for rule, stats in LN.mine(rows, NAMES, min_n=5):
            self.assertLessEqual(stats['train_rejected'], 70)
        found = LN.mine(rows, NAMES, min_n=5, max_reject_fraction=1.0)
        self.assertTrue(any(s['train_rejected'] > 70 for _, s in found))

    def test_thresholds_come_from_the_grid_rows_feature_values_only(self):
        rows = planted()[:200]
        grid = [row(i, feats={'dev_holding_pct': v}) for i, v in enumerate((1.5, 2.5, 3.5))]
        for rule, _ in LN.mine(rows, ('dev_holding_pct',), min_n=5, grid_rows=grid):
            self.assertIn(rule.conditions[0].value, (D('1.5'), D('2.5'), D('3.5')))

    def test_a_pair_must_beat_both_of_its_parts(self):
        rng = random.Random(11)
        rows = []
        for i in range(800):
            dev, junk = rng.uniform(0, 30), rng.uniform(0, 100)
            rug = dev > 12 and rng.random() < 0.9
            rows.append(row(i, 'RUG' if rug else 'FLAT', -0.08 + rng.gauss(0, 0.01) if rug else 0.01 + rng.gauss(0, 0.04),
                            {'dev_holding_pct': round(dev, 2), 'top10_pct': round(junk, 2)}))
        found = LN.mine(rows, NAMES, min_n=15)
        for rule, stats in found:
            if len(rule.conditions) == 2:
                for c in rule.conditions:
                    alone = LN.evaluate(rows, [LN.Rule((c,))])['gain_over_random_sol']
                    self.assertGreater(stats['train_gain_over_random_sol'], alone - 1e-12, rule.text())


class CrossValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.planted = run(planted())

    def test_every_rule_is_scored_only_on_blocks_after_its_training_data(self):
        r = self.planted
        cv = {f['fold']: f for f in r['split']['cv']}
        self.assertEqual(sorted(cv), [1, 2, 3, 4])
        for f in cv.values():
            self.assertLess(f['train_end_ts'] + r['split']['embargo_s'], f['test_start_ts'] + 1e-9)
        for c in r['candidates']:
            self.assertTrue(c['folds'])
            self.assertEqual([t['fold'] for t in c['train']], c['folds'])
            self.assertTrue(all(cv[j]['feasible'] for j in c['folds']))
            self.assertEqual(c['oos']['n'], sum(cv[j]['test'] for j in c['folds']))    # pooled over its test blocks only
        self.assertLess(r['split']['dev_end_ts'], r['split']['confirm_start_ts'])

    def test_a_fold_never_trains_on_its_test_block_or_later(self):
        """Rolling origin: changing the outcomes of the LAST development block changes nothing that folds 1-3 mined (they never saw it),
        and changing fold k's test block changes nothing fold k mined."""
        base = sorted(planted(), key=lambda x: (x.ts, x.mint))
        r = self.planted
        cv = {f['fold']: f for f in r['split']['cv']}

        def mined(result, folds):
            return sorted((c['rule']['text'], json.dumps(t, sort_keys=True)) for c in result['candidates'] for t in c['train'] if t['fold'] in folds)
        for k in (4, 3):
            start = next(i for i, x in enumerate(base) if x.ts == cv[k]['test_start_ts'])
            changed = [LN.Row(x.mint, x.ts, x.label, x.features, -x.pnl_lamports, True) if start <= i < r['split']['dev'] else x
                       for i, x in enumerate(base)]
            other = run(changed)
            self.assertEqual(mined(other, range(1, k + 1)), mined(r, range(1, k + 1)), k)
            self.assertNotEqual(mined(other, range(k + 1, 5)) if k < 4 else 'x', mined(r, range(k + 1, 5)) if k < 4 else 'y')

    def test_bh_runs_over_every_tested_rule_and_says_how_many(self):
        r = self.planted
        self.assertEqual(r['tested']['rules'], len(r['candidates']))
        self.assertGreater(r['tested']['rules'], 10)
        ps = [c['p'] for c in r['candidates']]
        for c, q in zip(r['candidates'], LN.benjamini_hochberg(ps)):
            self.assertAlmostEqual(c['q'], q, 9)
            self.assertGreaterEqual(c['q'], c['p'] - 1e-15)
            self.assertEqual('NOT_SIGNIFICANT' in c['flags'], c['q'] > LN.FDR_Q)
        for c in r['candidates']:
            if c['chosen']:
                self.assertEqual(c['flags'], [])
                self.assertTrue(c['oos']['beats_null'])
                self.assertEqual(c['oos']['null_draws'], LN.NULL_DRAWS)
        self.assertEqual(r['tested']['passed_null'], sum(1 for c in r['candidates'] if not c['flags']))

    def test_flags_explain_why_a_rule_was_not_chosen(self):
        allowed = {'INSUFFICIENT', 'REJECTS_TOO_MUCH', 'NO_GAIN', 'NOT_SIGNIFICANT', 'NOT_ABOVE_NULL'}
        for c in self.planted['candidates']:
            self.assertLessEqual(set(c['flags']), allowed)
            if not c['flags']:
                self.assertIn(c['kind'], ('PROFITABLE', 'BLEED_REDUCER'))
        flagged = [c for c in self.planted['candidates'] if c['flags']]
        self.assertTrue(flagged)
        self.assertTrue(all(not c['chosen'] for c in flagged))

    def test_every_out_of_fold_flag_matches_its_numbers(self):
        """The flags are the evidence: each one is set exactly when its number says so, over worlds where each one occurs."""
        rng = random.Random(9)
        drift = []                                                         # top10 rises over time and the high ones rug: rules mined on
        for i in range(600):                                               # the past reject (almost) everything in the later blocks
            top10 = 10 + 70 * i / 600 + rng.uniform(-15, 15)
            rug = rng.random() < (0.8 if top10 > 45 else 0.1)
            drift.append(row(i, 'RUG' if rug else 'FLAT', -0.08 + rng.gauss(0, 0.01) if rug else 0.012 + rng.gauss(0, 0.05),
                             {'top10_pct': round(top10, 2), 'dev_holding_pct': round(rng.uniform(0, 30), 2)}))
        seen = set()
        floor = min(LN.MIN_N_FLOOR, SMALL['min_n'])
        for r in [self.planted, run(drift)] + [run(noise(seed=s)) for s in range(1, 9)]:
            for c in r['candidates']:
                o, f = c['oos'], set(c['flags'])
                seen |= f
                self.assertEqual('INSUFFICIENT' in f, o['n_rejected'] < floor or o['n_kept'] < floor or o['welch_p'] is None, c)
                self.assertEqual('REJECTS_TOO_MUCH' in f, o['reject_fraction'] > LN.MAX_REJECT_FRACTION, c)
                self.assertEqual('NO_GAIN' in f, not (o['diff_kept_minus_rejected_sol'] or 0) > 0, c)
                self.assertEqual('NOT_SIGNIFICANT' in f, c['q'] > LN.FDR_Q, c)
                gated = not f - {'NOT_ABOVE_NULL'}
                self.assertEqual('NOT_ABOVE_NULL' in f, gated and not o['beats_null'], c)
                if c['chosen']:
                    self.assertEqual(f, set())
        self.assertLessEqual({'INSUFFICIENT', 'REJECTS_TOO_MUCH', 'NO_GAIN', 'NOT_SIGNIFICANT'}, seen)

    def test_a_rule_that_passes_bh_but_not_its_random_rule_null_is_never_chosen(self):
        with mock.patch.object(LN.RandomRuleNull, 'rejected_sums', lambda self, counts: [-10 ** 18] * self.draws):
            r = run(planted())                                             # a null that no rule can beat
        self.assertEqual((r['proposal_status'], r['selected']), ('NO_SELECTED_RULE', []))
        passed_bh = [c for c in r['candidates'] if c['flags'] == ['NOT_ABOVE_NULL']]
        self.assertTrue(passed_bh)
        self.assertTrue(all(not c['oos']['beats_null'] for c in passed_bh))

    def test_the_chosen_union_stays_under_the_reject_cap(self):
        """Two real, independent rug markers: dev > 12 (60%) and tx > 72 (40%). Each is a fine rule alone, but together they reject
        ~76%: the second is skipped, so the set is never "stop trading"."""
        rng = random.Random(21)
        rows = []
        for i in range(900):
            dev, tx = rng.uniform(0, 30), rng.randint(0, 120)
            rug = dev > 12 or tx > 72
            rows.append(row(i, 'RUG' if rug else 'FLAT', -0.08 + rng.gauss(0, 0.01) if rug else 0.03 + rng.gauss(0, 0.02),
                            {'dev_holding_pct': round(dev, 2), 'tx_count_first_10m': tx, 'top10_pct': round(rng.uniform(10, 80), 2)}))
        r = run(rows)
        self.assertTrue(r['selected'])
        self.assertLessEqual(r['union']['dev']['reject_fraction'], LN.MAX_REJECT_FRACTION)
        eligible = [c for c in r['candidates'] if not c['flags']]
        self.assertTrue({c['rule']['if'][0]['feature'] for c in eligible} >= {'dev_holding_pct', 'tx_count_first_10m'})
        dev = sorted(rows, key=lambda x: (x.ts, x.mint))[:r['split']['dev']]
        chosen = [LN.Rule(tuple(LN.Condition(c['feature'], c['op'], D(c['value'])) for c in s['if'])) for s in r['selected']]
        skipped = [c for c in eligible if not c['chosen']
                   and LN.evaluate(dev, chosen + [LN.Rule(tuple(LN.Condition(x['feature'], x['op'], D(x['value'])) for x in c['rule']['if']))])['reject_fraction']
                   > LN.MAX_REJECT_FRACTION]
        self.assertTrue(skipped)                                           # the cap is what kept them out

    def test_a_rule_that_loses_out_of_fold_is_dropped(self):
        rows = planted()
        flipped = [LN.Row(r.mint, r.ts, r.label, r.features, -r.pnl_lamports if i % 2 else r.pnl_lamports, True) for i, r in enumerate(rows)]
        r = run(flipped)                                                     # half the outcomes reversed: the "signal" cancels
        self.assertFalse(r['confirmed'])

    def test_at_most_max_rules_and_each_adds_something(self):
        r = run(planted(1200), max_rules=2)
        self.assertLessEqual(len(r['selected']), 2)
        if len(r['selected']) == 2:
            rules = [LN.Rule(tuple(LN.Condition(c['feature'], c['op'], D(c['value'])) for c in s['if'])) for s in r['selected']]
            dev = sorted(planted(1200), key=lambda x: (x.ts, x.mint))[:r['split']['dev']]
            self.assertGreater(LN.evaluate(dev, rules)['gain_over_random_sol'], LN.evaluate(dev, rules[:1])['gain_over_random_sol'])

    def test_confirm_verdicts(self):
        good = {'n_rejected': 40, 'n_kept': 40, 'reject_fraction': 0.5, 'diff_kept_minus_rejected_sol': 0.05, 'welch_p': 0.001,
                'beats_null': True, 'perm_p': 0.001, 'kept_mean_pnl_sol': 0.01}
        self.assertEqual(LN.confirm_verdict(good, min_confirm=30), 'CONFIRMED')
        self.assertTrue(LN.confirmed(good, min_confirm=30))
        self.assertEqual(LN.confirm_verdict(dict(good, kept_mean_pnl_sol=0.0), min_confirm=30), 'BLEED_REDUCER')
        self.assertEqual(LN.confirm_verdict(dict(good, kept_mean_pnl_sol=-0.01), min_confirm=30), 'BLEED_REDUCER')
        self.assertFalse(LN.confirmed(dict(good, kept_mean_pnl_sol=-0.01), min_confirm=30))
        self.assertEqual(LN.confirm_verdict(dict(good, reject_fraction=0.75), min_confirm=30), 'CONFIRMED')   # capped on dev, not here
        for bad in (dict(good, n_rejected=29), dict(good, n_kept=29), dict(good, diff_kept_minus_rejected_sol=0.0), dict(good, diff_kept_minus_rejected_sol=None), dict(good, welch_p=0.051),
                    dict(good, welch_p=None), dict(good, beats_null=False), dict(good, perm_p=0.06), dict(good, perm_p=None)):
            self.assertEqual(LN.confirm_verdict(bad, min_confirm=30), 'NOT_CONFIRMED', bad)


class LearnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.noise = {seed: run(noise(seed=seed)) for seed in range(1, 61)}

    def test_planted_structure_is_recovered_and_confirmed(self):
        r = run(planted())
        self.assertEqual(r['proposal_status'], 'CONFIRMED')
        self.assertTrue(r['selected'])
        first = r['selected'][0]['if'][0]
        self.assertEqual((first['feature'], first['op']), ('dev_holding_pct', '>'))
        self.assertTrue(10 <= D(first['value']) <= 20)
        conf = r['union']['confirm']
        self.assertIs(conf, r['confirm_test'])
        self.assertTrue(r['confirm_consumed'])
        self.assertGreater(conf['kept_mean_pnl_sol'], 0)
        self.assertGreater(conf['diff_kept_minus_rejected_sol'], conf['null_p95_diff_sol'])
        self.assertLessEqual(conf['welch_p'], LN.ALPHA)
        self.assertLessEqual(conf['perm_p'], LN.ALPHA)
        self.assertLessEqual(conf['reject_fraction'], LN.MAX_REJECT_FRACTION)
        self.assertGreater(conf['rug_rate_rejected'], conf['rug_rate_kept'])
        self.assertGreater(conf['gain_over_random_sol'], 0)
        self.assertGreater(conf['policy_mean_pnl_sol'], r['baseline']['confirm']['baseline_mean_pnl_sol'])
        self.assertEqual(r['feasibility']['eta_hours'], 0.0)
        self.assertIsNone(r['bleed_reducer'])

    def test_pure_noise_produces_no_confirmed_rule(self):
        for seed, r in self.noise.items():
            self.assertFalse(r['confirmed'], seed)
            self.assertIn(r['proposal_status'], ('NO_RULE_FOUND', 'NO_SELECTED_RULE', 'NOT_CONFIRMED'), seed)

    def test_noise_does_reach_the_confirm_gate_so_the_gate_is_what_stops_it(self):
        """A few noise worlds get a rule through BH and the out-of-fold null (about the 5% the FDR allows): the confirm test stops them."""
        reached = [seed for seed, r in self.noise.items() if r['proposal_status'] == 'NOT_CONFIRMED']
        self.assertTrue(reached, 'no noise seed reached the confirm block: the confirm gate is untested')
        self.assertLessEqual(len(reached), 6)                               # <= 10% of the worlds pass development (FDR 5%)
        for seed in reached:
            self.assertTrue(self.noise[seed]['confirm_consumed'])

    def test_noise_with_a_losing_baseline_is_never_confirmed(self):
        """THE regression test for the L19 bug: every window loses on average (like the live -0.04..-0.08 SOL per candidate), and the
        outcomes ignore every feature. The old test (rejected PnL against 0) confirmed a rule in EVERY one of these worlds. (Only the API
        both versions share is used, so this test also runs against the old learner.)"""
        statuses = []
        for seed in range(1, 31):
            rows = noise(seed=seed, mu=-0.05, sd=0.1)
            self.assertLess(sum(x.pnl_lamports for x in rows[:300]), 0)
            self.assertLess(sum(x.pnl_lamports for x in rows[300:]), 0)
            r = run(rows)
            self.assertFalse(r['confirmed'], seed)
            self.assertNotEqual(r['proposal_status'], 'CONFIRMED', seed)
            statuses.append(r['proposal_status'])
        self.assertLessEqual(statuses.count('BLEED_REDUCER'), 1, statuses)

    def test_a_stop_trading_rule_cannot_win(self):
        """90% of the candidates (dev_holding_pct > 3) lose big, the rest make money: "reject 90%" would keep only winners, but it is
        "stop trading", not selection. Any rule under the 70% cap keeps losers, so nothing is proposed; lifting the cap lets it through."""
        rows = two_kinds_of_loser(bad_above=3.0, bad=-0.10, good=0.03)
        r = run(rows)
        self.assertFalse(r['confirmed'])
        self.assertEqual(r['selected'] and r['confirm_test'] is not None and r['confirm_test']['kept_mean_pnl_sol'] > 0, False)
        for c in r['candidates']:
            self.assertTrue(all(t['train_reject_fraction'] <= LN.MAX_REJECT_FRACTION for t in c['train']))
        if r['union']:
            self.assertLessEqual(r['union']['dev']['reject_fraction'], LN.MAX_REJECT_FRACTION)
        lax = run(rows, max_reject_fraction=0.95)
        self.assertEqual(lax['proposal_status'], 'CONFIRMED')
        self.assertGreater(lax['confirm_test']['reject_fraction'], LN.MAX_REJECT_FRACTION)
        self.assertLess(lax['confirm_test']['coverage'], 0.3)

    def test_a_rule_that_only_reduces_the_bleed_is_reported_never_proposed(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run(two_kinds_of_loser(), proposals_dir=Path(tmp) / 'p', strategy=STRATEGY)
            doc = json.loads(Path(r['proposal']).read_text())
        self.assertEqual(r['proposal_status'], 'BLEED_REDUCER')
        self.assertFalse(r['confirmed'])
        self.assertTrue(r['confirm_test']['beats_null'])
        self.assertLess(r['confirm_test']['kept_mean_pnl_sol'], 0)
        self.assertEqual(r['bleed_reducer']['rules'], r['selected'])
        self.assertIn('NOT a proposal', r['bleed_reducer']['note'])
        self.assertEqual((doc['status'], doc['outcome'], doc['proposed']['selection_rules'], doc['requires_runner_change']),
                         ('NO_PROPOSAL', 'BLEED_REDUCER', [], []))
        self.assertEqual(doc['bleed_reducer']['rules'], r['selected'])
        self.assertTrue(any(w.startswith('BLEED_REDUCER') for w in r['warnings']))

    def test_the_confirm_block_never_chooses_anything(self):
        base = planted()
        a = run(base)
        n_conf = a['split']['confirm']
        changed = base[:-n_conf] + [LN.Row(r.mint, r.ts, r.label, {k: (None if v is None else -v) for k, v in r.features.items()}, -r.pnl_lamports, True)
                                    for r in base[-n_conf:]]
        b = run(changed)
        self.assertEqual(a['candidates'], b['candidates'])
        self.assertEqual(a['selected'], b['selected'])
        self.assertEqual(a['union']['dev'], b['union']['dev'])
        self.assertNotEqual(a['union']['confirm'], b['union']['confirm'])
        self.assertEqual(b['proposal_status'], 'NOT_CONFIRMED')
        self.assertIs(a['confirm_used_for_selection'], False)
        with tempfile.TemporaryDirectory() as tmp:                         # chosen on dev but not confirmed: nothing is proposed
            doc = json.loads(Path(run(changed, proposals_dir=Path(tmp) / 'p')['proposal']).read_text())
        self.assertTrue(b['selected'])
        self.assertEqual((doc['status'], doc['outcome'], doc['proposed']['selection_rules'], doc['requires_runner_change']),
                         ('NO_PROPOSAL', 'NOT_CONFIRMED', [], []))
        self.assertEqual(doc['evidence']['selected_on_dev'], b['selected'])     # the evidence still shows what development chose
        self.assertIs(doc['evidence']['confirm_consumed'], True)

    def test_a_confirm_block_is_used_once(self):
        """The selection files are the ledger: a run cannot confirm on candidates an earlier run already tested; it waits (with an ETA)
        for a fresh block after them plus the embargo."""
        with tempfile.TemporaryDirectory() as tmp:
            props = Path(tmp) / 'p'
            first = run(planted(600), proposals_dir=props, now=NOW)
            self.assertEqual((first['proposal_status'], first['confirm_consumed']), ('CONFIRMED', True))
            again = run(planted(600), proposals_dir=props, now=NOW + 1)
            self.assertEqual(again['proposal_status'], 'CONFIRM_PENDING')
            self.assertEqual((again['confirm_test'], again['confirm_consumed'], again['union']['confirm']), (None, False, None))
            self.assertEqual(again['split']['consumed_until'], first['split']['confirm_end_ts'])
            self.assertGreater(again['feasibility']['eta_hours'], 0)
            self.assertFalse(again['feasibility']['feasible'])
            doc = json.loads(Path(again['proposal']).read_text())
            self.assertEqual((doc['status'], doc['evidence']['confirm_consumed']), ('NO_PROPOSAL', False))
            need = again['feasibility']['needed_enterable']
            fresh = run(planted(need), proposals_dir=props, now=NOW + 2)    # exactly the ETA's candidates: a fresh block
            self.assertEqual(fresh['proposal_status'], 'CONFIRMED')
            self.assertGreater(fresh['split']['confirm_start_ts'], first['split']['confirm_end_ts'])
            self.assertEqual(run(planted(need - 1), proposals_dir=props, now=NOW + 3, consumed_until=first['split']['confirm_end_ts'])['proposal_status'],
                             'CONFIRM_PENDING')
        self.assertEqual(run(planted(600), consumed_until=None)['proposal_status'], 'CONFIRMED')    # an explicit None: no ledger

    def test_the_ledger_fails_closed_and_reads_the_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            props = Path(tmp)
            late = datetime.datetime.fromtimestamp(T0 + 10 ** 6, datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            (props / ('%s-selection.json' % late)).write_text('{not json')
            ledger = LN.consumed_confirm_until(props)
            self.assertEqual((ledger['files'], ledger['unreadable']), (1, 1))
            self.assertEqual(ledger['until'], T0 + 10 ** 6)                     # unreadable: consumed up to the time in its name
            self.assertEqual(run(planted(), proposals_dir=props, now=NOW + 5)['proposal_status'], 'CONFIRM_PENDING')
        with tempfile.TemporaryDirectory() as tmp:
            props = Path(tmp)
            for name, outcome in (('20231115T000000Z', 'NOT_CONFIRMED'), ('20231116T000000Z', 'NO_SELECTED_RULE')):
                (props / ('%s-selection.json' % name)).write_text(json.dumps({'selection_version': 2, 'outcome': outcome}))
            (props / 'notes.json').write_text('{}')
            self.assertEqual(LN.consumed_confirm_until(props)['until'], LN._name_time('20231115T000000Z-selection.json'))
            self.assertEqual(LN.consumed_confirm_until(props / 'missing')['until'], None)

    def test_only_enterable_candidates_are_mined_but_tables_use_all(self):
        rows = planted()
        mixed = [LN.Row(r.mint, r.ts, r.label, r.features, r.pnl_lamports, i % 3 != 0) for i, r in enumerate(rows)]
        r = run(mixed)
        self.assertEqual(r['counts']['candidates'], 600)
        self.assertEqual(r['counts']['enterable'], sum(1 for x in mixed if x.enterable))
        self.assertEqual(r['split']['enterable'], r['counts']['enterable'])
        self.assertEqual(sum(b['n'] for b in r['tables']['dev_holding_pct']['buckets']), 600)

    def test_too_few_candidates_is_a_status_with_an_eta_not_a_crash(self):
        r = run([row(0), row(1)])
        self.assertEqual((r['proposal_status'], r['confirmed'], r['selected']), ('INSUFFICIENT_DATA', False, []))
        self.assertEqual(r['counts']['candidates'], 2)
        self.assertGreater(r['feasibility']['eta_hours'], 0)
        self.assertEqual(r['feasibility']['rate_per_hour'], 36.0)
        self.assertTrue(any('No feasible fresh confirm block yet' in w for w in r['warnings']))

    def test_bad_arguments_are_refused(self):
        for kw in ({'min_n': 0}, {'min_n': True}, {'min_confirm': 1}, {'min_bucket': 1.5}, {'folds': 1}, {'null_draws': 999},
                   {'null_draws': 1000.0}, {'alpha': 0}, {'alpha': 0.6}, {'fdr_q': float('nan')}, {'max_reject_fraction': 0},
                   {'max_reject_fraction': 1.1}, {'confirm_fraction': 1}, {'confirm_fraction': 0}, {'embargo_s': -1},
                   {'embargo_s': float('inf')}, {'consumed_until': float('nan')}, {'max_rules': 0}):
            with self.assertRaises(LN.LearnError, msg=kw):
                run(planted(60), **kw)

    def test_default_thresholds_are_the_documented_ones(self):
        self.assertEqual((LN.MIN_N, LN.MIN_N_FLOOR, LN.MIN_CONFIRM, LN.MIN_BUCKET, LN.MAX_REJECT_FRACTION, LN.NULL_DRAWS, LN.FOLDS,
                          LN.CONFIRM_FRACTION, LN.ALPHA, LN.FDR_Q), (50, 10, 30, 20, 0.7, 1000, 5, 0.3, 0.05, 0.05))
        r = LN.learn(planted(200), now=NOW, feature_names=NAMES)            # the defaults on 200 candidates: feasible, but thin
        self.assertEqual(r['split']['confirm'], 60)
        self.assertTrue(any('noise-dominated' in w for w in r['warnings']))

    def test_deterministic_seeded_and_json_safe(self):
        a, b = run(planted()), run(planted())
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        json.dumps(a, allow_nan=False)
        c = run(planted(), seed=7)
        self.assertEqual((c['proposal_status'], c['selected']), (a['proposal_status'], a['selected']))
        self.assertEqual(c['thresholds']['seed'], 7)


class TablesTests(unittest.TestCase):
    def test_buckets_partition_the_rows_and_ties_share_a_bucket(self):
        rows = [row(i, 'RUG' if i < 30 else 'WIN', -0.1 if i < 30 else 0.1, {'dev_holding_pct': 5 if i % 2 else 20}) for i in range(100)]
        t = LN.quantile_tables(rows, NAMES, buckets=4, min_bucket=5)['dev_holding_pct']
        self.assertEqual(sum(b['n'] for b in t['buckets']), 100)
        self.assertEqual(len(t['buckets']), 2)                       # two distinct values: two buckets, never one value in two buckets
        self.assertEqual([(b['lo'], b['hi']) for b in t['buckets']], [('5', '5'), ('20', '20')])
        self.assertEqual(t['n'], 100)

    def test_rates_wilson_and_insufficient(self):
        rows = [row(i, 'RUG' if i < 8 else 'WIN', -0.1 if i < 8 else 0.1, {'top10_pct': i}) for i in range(40)]
        t = LN.quantile_tables(rows, NAMES, buckets=2, min_bucket=25)['top10_pct']
        first = t['buckets'][0]
        self.assertEqual((first['n'], first['rug_rate'], first['win_rate']), (20, 0.4, 0.6))
        lo, hi = LN.wilson(8, 20)
        self.assertEqual(first['rug_ci'], [lo, hi])
        self.assertTrue(all(b['insufficient'] for b in t['buckets']) and t['insufficient'])
        self.assertEqual(first['mean_sim_pnl_sol'], round((8 * -0.1 + 12 * 0.1) / 20, 9))
        wide = LN.quantile_tables(rows, NAMES, buckets=2, min_bucket=5)['top10_pct']
        self.assertFalse(wide['insufficient'])

    def test_missing_feature_has_an_empty_table(self):
        t = LN.quantile_tables([row(0)], NAMES)['dev_holding_pct']
        self.assertEqual((t['n'], t['buckets'], t['insufficient']), (0, [], True))

    def test_winners_vs_rugs_medians(self):
        rows = [row(0, 'WIN', 0.1, {'dev_holding_pct': 1}), row(1, 'WIN', 0.1, {'dev_holding_pct': 3}), row(2, 'RUG', -0.1, {'dev_holding_pct': 20}),
                row(3, 'RUG', -0.1, {'dev_holding_pct': 30}), row(4, 'RUG', -0.1, {'dev_holding_pct': 40}), row(5, 'LOSS', -0.02, {})]
        w = LN.winners_vs_rugs(rows, NAMES)
        self.assertEqual((w['WIN']['n'], w['WIN']['features']['dev_holding_pct']), (2, {'n': 2, 'median': '2'}))
        self.assertEqual(w['RUG']['features']['dev_holding_pct'], {'n': 3, 'median': '30'})
        self.assertEqual(w['LOSS']['features']['dev_holding_pct'], {'n': 0, 'median': None})
        self.assertEqual(w['FLAT']['n'], 0)


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.strategy = S.StrategyConfig.load(DEFAULT)

    def test_written_never_applied_never_overwritten(self):
        props = self.dir / 'proposals'
        before = hashlib.sha256(DEFAULT.read_bytes()).hexdigest()
        r = run(planted(), proposals_dir=props, strategy=self.strategy)
        self.assertEqual((r['proposal_status'], r['confirmed']), ('CONFIRMED', True))
        path = Path(r['proposal'])
        self.assertEqual((path.parent, path.name.endswith('-selection.json')), (props, True))
        doc = json.loads(path.read_text())
        self.assertEqual(doc['status'], 'SELECTION_PROPOSAL_NOT_APPLIED')
        self.assertTrue(doc['never_applied_automatically'])
        self.assertEqual(doc['current']['strategy_version'], self.strategy.strategy_version)
        self.assertEqual(doc['current']['selection_rules'], [])
        self.assertEqual(doc['diff']['selection_rules']['from'], [])
        self.assertEqual(doc['diff']['selection_rules']['to'], doc['proposed']['selection_rules'])
        self.assertEqual(doc['proposed']['selection_rules'][0]['action'], 'REJECT')
        self.assertEqual({'baseline', 'union', 'split', 'thresholds'} <= set(doc['evidence']), True)
        for window in ('dev', 'confirm'):
            self.assertIn(window, doc['evidence']['union'])
        self.assertEqual((doc['evidence']['chosen_on'], doc['evidence']['confirmed_on']), ('dev_cv', 'confirm'))
        self.assertEqual(doc['evidence']['tested']['rules'], len(r['candidates']))
        self.assertIs(doc['evidence']['confirm_consumed'], True)
        self.assertEqual(doc['evidence']['confirm_test'], r['confirm_test'])
        self.assertEqual(doc['evidence']['feasibility']['eta_hours'], 0.0)
        self.assertIsNone(doc['bleed_reducer'])
        self.assertIn('selection bias', ' '.join(doc['warnings']))
        self.assertTrue(any(x.startswith('wait_for_enrichment:') and 'dev_holding_pct' in x for x in doc['requires_runner_change']))
        self.assertEqual(hashlib.sha256(DEFAULT.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in props.iterdir()), [path.name])
        with self.assertRaises(FileExistsError):
            run(planted(), proposals_dir=props, strategy=self.strategy)

    def test_an_unconfirmed_run_writes_a_file_that_proposes_nothing(self):
        props = self.dir / 'proposals'
        r = run(noise(seed=3), proposals_dir=props, strategy=self.strategy)
        self.assertFalse(r['confirmed'])
        doc = json.loads(Path(r['proposal']).read_text())
        self.assertEqual((doc['status'], doc['outcome']), ('NO_PROPOSAL', r['proposal_status']))
        self.assertEqual((doc['proposed']['selection_rules'], doc['diff']['selection_rules']['to'], doc['requires_runner_change']), ([], [], []))
        self.assertTrue(doc['never_applied_automatically'])
        self.assertIsNone(run(noise(seed=3))['proposal'])                     # no directory, no file

    def test_screen_time_features_need_no_runner_wait(self):
        rng = random.Random(5)
        rows = []
        for i in range(600):
            mc = rng.uniform(10, 200)
            rug = mc > 120 and rng.random() < 0.9
            rows.append(LN.Row('M%d' % i, T0 + 100 * i, lab('M%d' % i, 'RUG' if rug else 'FLAT', T0), {'market_cap_usd': D(str(round(mc, 2)))},
                               int((-0.08 if rug else 0.01) * 10 ** 9 + rng.gauss(0, 10 ** 7)), True))
        r = LN.learn(rows, now=NOW, feature_names=('market_cap_usd',), proposals_dir=self.dir / 'p', strategy=self.strategy, **SMALL)
        doc = json.loads(Path(r['proposal']).read_text())
        self.assertEqual(doc['requires_runner_change'], ['selection_rules'])

    def test_symlinked_directory_refused(self):
        real = self.dir / 'real'
        real.mkdir()
        link = self.dir / 'link'
        link.symlink_to(real)
        with self.assertRaises(LN.LearnError):
            run(planted(), proposals_dir=link, strategy=self.strategy)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))

    def test_section_has_the_required_content(self):
        text = LN.render_section(run(planted()))
        for needle in ('What winners vs rugs looked like at entry', 'PAPER ONLY', 'Never applied automatically', 'DEVELOPMENT window', 'dev_holding_pct',
                       'rug rate', 'Quantile tables', 'CHOSEN', 'NOT used to choose', 'Benjamini-Hochberg', 'random rule', 'BH q', 'null p',
                       'ETA to the first feasible confirm', 'tested once'):
            self.assertIn(needle, text)

    def test_page_is_a_full_document(self):
        page = LN.render_html(run(planted()))
        self.assertTrue(page.startswith('<!doctype html>'))
        self.assertIn('<style>', page)

    def test_hostile_strings_are_escaped(self):
        rows = planted()
        r = run(rows)
        r['candidates'][0]['rule']['text'] = '<script>alert(1)</script>'
        r['proposal_status'] = '<b>x</b>'
        r['warnings'].append('<img src=x>')
        page = LN.render_html(r)
        for raw in ('<script>alert', '<b>x</b>', '<img src=x>'):
            self.assertNotIn(raw, page)
        self.assertIn('&lt;script&gt;', page)

    def test_insufficient_buckets_are_flagged(self):
        r = run(planted(), min_bucket=500)
        self.assertIn('INSUFFICIENT', LN.render_section(r))

    def test_the_cli_on_a_fresh_deployment_reports_insufficient_data(self):
        """Day one on the VPS: an empty lean.sqlite and paths.sqlite. The CLI still writes its page and a NO_PROPOSAL file, rc 0."""
        from lean.paths import PathStore
        Store(self.dir / 'lean.sqlite', initial_cash_sol='10', code_version='t', strategy_version='lean-1').close()
        PathStore(self.dir / 'paths.sqlite').close()
        out = self.dir / 'out'
        out.mkdir()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = LN.main(['--lean-db', str(self.dir / 'lean.sqlite'), '--paths-db', str(self.dir / 'paths.sqlite'), '--out-dir', str(out),
                            '--embargo-s', '21600', '--now', str(NOW)])
        self.assertEqual(code, 0, buf.getvalue())
        summary = json.loads(buf.getvalue())
        self.assertEqual((summary['proposal_status'], summary['confirmed'], summary['candidates']), ('INSUFFICIENT_DATA', False, 0))
        self.assertEqual(json.loads(Path(summary['proposal']).read_text())['status'], 'NO_PROPOSAL')
        self.assertIn('What winners vs rugs looked like at entry', Path(summary['html']).read_text())
        for bad in (['--config', 'x.json', '--strategy', str(DEFAULT)], ['--fractions', '0.5,0.5'], ['--null-draws', '10'],
                    ['--max-reject-fraction', '0']):
            err = io.StringIO()
            with contextlib.redirect_stdout(err), contextlib.redirect_stderr(io.StringIO()):
                try:
                    rc = LN.main(['--lean-db', str(self.dir / 'lean.sqlite'), '--paths-db', str(self.dir / 'paths.sqlite'),
                                  '--out-dir', str(out), '--now', str(NOW + 1)] + bad)
                except SystemExit as stop:                        # argparse: mutually exclusive options
                    rc = stop.code
            self.assertEqual(rc, 2, bad)

    def test_write_report_is_exclusive(self):
        r = run(planted())
        out = self.dir / 'out'
        out.mkdir()
        html_path, json_path = LN.write_report(out, r)
        self.assertEqual(json.loads(json_path.read_text())['label'], LN.LABEL)
        with self.assertRaises(FileExistsError):
            LN.write_report(out, r)
        with self.assertRaises(LN.LearnError):
            LN.write_report(self.dir / 'missing', r)


if __name__ == '__main__':
    unittest.main()
