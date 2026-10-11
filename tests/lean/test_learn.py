"""SYNTHETIC_TEST_ONLY: the selection learner (lean/learn.py). Fixtures only, no network.

Rows are built directly (label, entry-time features, simulated PnL) so the statistics can be checked against hand arithmetic; the
planted-structure and pure-noise datasets come from a seeded generator. The full pipeline on the real runner is in test_e2e_learn."""
import contextlib
import hashlib
import io
import json
import os
import random
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from lean import labels as LB, learn as LN, strategy as S

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / 'config' / 'lean' / 'strategy-default.json'
T0 = 19675 * 86400 + 3600
NOW = T0 + 10 ** 6
NAMES = ('dev_holding_pct', 'top10_pct', 'tx_count_first_10m')
SMALL = dict(min_n=15, min_confirm=10, min_bucket=5)                  # the production minimums are 50 / 30 / 20; the maths is the same


def lab(mint, name, ts, end_return=0.0):
    return LB.Label(mint, name, LB.LABEL_VERSION, ts, 10, None if name == 'FLAT' else 30.0, 'PRICE' if name == 'RUG' else None,
                    0.5 if name == 'WIN' else 0.05, -0.9 if name == 'RUG' else -0.1, 30.0, 30.0, end_return)


def row(i, name='FLAT', pnl_sol=0.0, feats=None, enterable=True, ts=None):
    ts = T0 + 100 * i if ts is None else ts
    f = {n: None for n in NAMES}
    f.update({k: None if v is None else D(str(v)) for k, v in (feats or {}).items()})
    return LN.Row('M%05d' % i, ts, lab('M%05d' % i, name, ts, pnl_sol), f, int(round(pnl_sol * 10 ** 9)), enterable, 'CLOSED' if enterable else None)


def planted(n=600, seed=1, p_rug_hi=0.85, p_rug_lo=0.05):
    """dev_holding_pct above 8 usually rugs; everything else is a mildly profitable random walk. top10 and tx count are noise."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        dev, top10, tx = rng.uniform(0, 30), rng.uniform(10, 80), rng.randint(0, 120)
        rug = rng.random() < (p_rug_hi if dev > 8 else p_rug_lo)
        if rug:
            name, pnl = 'RUG', -0.08 + rng.gauss(0, 0.01)
        else:
            pnl = 0.012 + rng.gauss(0, 0.05)
            name = 'WIN' if pnl > 0.04 else ('LOSS' if pnl < -0.03 else 'FLAT')
        rows.append(row(i, name, pnl, {'dev_holding_pct': round(dev, 2), 'top10_pct': round(top10, 2), 'tx_count_first_10m': tx}))
    return rows


def noise(n=600, seed=1):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        pnl = rng.gauss(0, 0.05)
        name = rng.choice(('RUG', 'WIN', 'LOSS', 'FLAT'))
        rows.append(row(i, name, pnl, {'dev_holding_pct': round(rng.uniform(0, 30), 2), 'top10_pct': round(rng.uniform(10, 80), 2),
                                       'tx_count_first_10m': rng.randint(0, 120)}))
    return rows


def run(rows, **kw):
    kw = dict(SMALL, now=NOW, feature_names=NAMES, **kw)
    return LN.learn(rows, **kw)


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

    def test_t_statistic(self):
        self.assertIsNone(LN.t_statistic([1.0]))
        self.assertAlmostEqual(LN.t_statistic([1, 2, 3]), 2 / (1 / 3 ** 0.5), 9)
        self.assertEqual(LN.t_statistic([-1, -1, -1]), -1e9)
        self.assertEqual(LN.t_statistic([0, 0]), 0.0)
        self.assertLess(LN.t_statistic([-3, -2, -4, -1]), 0)


class LookaheadTests(unittest.TestCase):
    def rows(self, **over):
        base = {'mint': 'M', 'fields': {'dev_holding_pct': '12', 'top10_pct': '40'}, 'missing': {}, '_ts': T0 - 10, '_screen_at': T0 - 12, '_collected_at': T0 - 8}
        base.update(over)
        return base

    def test_a_row_known_before_the_decision_is_used(self):
        known, why, dropped = LN.features_known_at([self.rows()], T0, NAMES)
        self.assertEqual((known['dev_holding_pct'], known['top10_pct'], known['tx_count_first_10m'], why, dropped), (D(12), D(40), None, None, 0))

    def test_a_row_collected_after_the_decision_plus_lag_is_never_used(self):
        late = self.rows(_collected_at=T0 + LN.DEFAULT_MAX_LAG_S + 1, fields={'dev_holding_pct': '99'})
        known, why, dropped = LN.features_known_at([late], T0, NAMES)
        self.assertEqual((known, why, dropped), (None, 'FEATURES_AFTER_DECISION', 1))
        known, why, _ = LN.features_known_at([late], T0, NAMES, max_lag_s=LN.DEFAULT_MAX_LAG_S + 5)
        self.assertEqual(known['dev_holding_pct'], D(99))                   # the lag is the only knob, and it is explicit

    def test_a_row_screened_after_the_decision_is_never_used(self):
        later = self.rows(_screen_at=T0 + 60, _collected_at=T0 + 61, fields={'dev_holding_pct': '99'})
        self.assertEqual(LN.features_known_at([later], T0, NAMES, max_lag_s=10 ** 6)[:2], (None, 'FEATURES_AFTER_DECISION'))

    def test_the_last_acceptable_row_wins_and_a_later_row_cannot_replace_it(self):
        a = self.rows(fields={'dev_holding_pct': '1'})
        b = self.rows(fields={'dev_holding_pct': '2'}, _screen_at=T0 - 5, _collected_at=T0 - 4)
        leak = self.rows(fields={'dev_holding_pct': '77'}, _screen_at=T0 + 500, _collected_at=T0 + 501)
        known, _, dropped = LN.features_known_at([a, b, leak], T0, NAMES)
        self.assertEqual((known['dev_holding_pct'], dropped), (D(2), 1))

    def test_no_rows(self):
        self.assertEqual(LN.features_known_at([], T0, NAMES)[:2], (None, 'NO_FEATURES_ROW'))

    def test_unusable_values_are_unknown_not_zero(self):
        r = self.rows(fields={'dev_holding_pct': 'abc', 'top10_pct': True, 'tx_count_first_10m': float('inf')})
        known, _, _ = LN.features_known_at([r], T0, NAMES)
        self.assertEqual(set(known.values()), {None})


class SplitTests(unittest.TestCase):
    def test_three_windows_by_time_in_any_input_order(self):
        rows = [row(i) for i in range(20)]
        mine, select, confirm, bounds = LN.split_rows(list(reversed(rows)))
        self.assertEqual((len(mine), len(select), len(confirm)), (10, 5, 5))
        self.assertLess(max(r.ts for r in mine), min(r.ts for r in select))
        self.assertLess(max(r.ts for r in select), min(r.ts for r in confirm))
        self.assertEqual(bounds, (rows[10].ts, rows[15].ts))

    def test_embargo(self):
        rows = [row(i) for i in range(20)]
        _, select, confirm, _ = LN.split_rows(rows, embargo_s=250)
        self.assertEqual(([r.mint for r in select][0], [r.mint for r in confirm][0]), ('M00013', 'M00018'))
        with self.assertRaises(LN.LearnError):
            LN.split_rows(rows, embargo_s=10 ** 6)

    def test_bad_arguments(self):
        rows = [row(i) for i in range(10)]
        for fr in ((1, 0, 0), (0.5, 0.5), (0.5, 0.3, 0.3), (float('nan'), 0.5, 0.5), (True, 0.5, 0.5), 0.7, None):
            with self.assertRaises(LN.LearnError, msg=fr):
                LN.split_rows(rows, fr)
        for emb in (-1, float('nan'), True, 'x'):
            with self.assertRaises(LN.LearnError, msg=emb):
                LN.split_rows(rows, embargo_s=emb)
        with self.assertRaises(LN.LearnError):
            LN.split_rows(rows[:2])


class EvaluateTests(unittest.TestCase):
    def test_gain_is_the_pnl_we_no_longer_take(self):
        rows = [row(0, 'RUG', -0.08, {'dev_holding_pct': 20}), row(1, 'RUG', -0.06, {'dev_holding_pct': 12}), row(2, 'WIN', 0.10, {'dev_holding_pct': 2}),
                row(3, 'FLAT', 0.0, {'dev_holding_pct': 1}), row(4, 'WIN', 0.04, {'dev_holding_pct': None})]
        rule = LN.Rule((LN.Condition('dev_holding_pct', '>', D(8)),))
        m = LN.evaluate(rows, [rule])
        self.assertEqual((m['n'], m['n_rejected'], m['n_kept']), (5, 2, 3))
        self.assertAlmostEqual(m['baseline_mean_pnl_sol'], 0.0 / 5 + (-0.08 - 0.06 + 0.10 + 0 + 0.04) / 5, 9)
        self.assertAlmostEqual(m['policy_mean_pnl_sol'], (0.10 + 0.0 + 0.04) / 5, 9)
        self.assertAlmostEqual(m['gain_per_candidate_sol'], 0.14 / 5, 9)
        self.assertAlmostEqual(m['rejected_mean_pnl_sol'], -0.07, 9)
        self.assertEqual((m['rug_rate_rejected'], m['rug_rate_kept'], m['win_rate_kept']), (1.0, 0.0, round(2 / 3, 6)))

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
        self.assertEqual((m['n_rejected'], m['gain_per_candidate_sol']), (0, 0.0))

    def test_rule_text_and_dict(self):
        rule = LN.Rule((LN.Condition('dev_holding_pct', '>', D('8.0')), LN.Condition('top10_pct', '<=', D('45'))))
        self.assertEqual(rule.text(), 'dev_holding_pct > 8 AND top10_pct <= 45')
        self.assertEqual(rule.to_dict()['if'][0], {'feature': 'dev_holding_pct', 'op': '>', 'value': '8'})
        self.assertEqual(rule.to_dict()['action'], 'REJECT')


class MineTests(unittest.TestCase):
    def test_recovers_the_planted_single_condition(self):
        rows = planted()
        mine_rows = LN.split_rows(rows)[0]
        found = LN.mine(mine_rows, NAMES, min_n=15)
        self.assertTrue(found)
        top = found[0][0]
        self.assertEqual([c.feature for c in top.conditions][0], 'dev_holding_pct')
        self.assertEqual(top.conditions[0].op, '>')
        self.assertTrue(2 <= top.conditions[0].value <= 12, top.text())
        self.assertGreater(found[0][1]['train_gain_per_candidate_sol'], 0.02)

    def test_recovers_a_planted_two_condition_rule(self):
        rng = random.Random(7)
        rows = []
        for i in range(800):
            dev, top10 = rng.uniform(0, 30), rng.uniform(10, 80)
            rug = dev > 8 and top10 > 45 and rng.random() < 0.9
            pnl = -0.08 + rng.gauss(0, 0.01) if rug else 0.01 + rng.gauss(0, 0.04)
            rows.append(row(i, 'RUG' if rug else 'FLAT', pnl, {'dev_holding_pct': round(dev, 2), 'top10_pct': round(top10, 2)}))
        found = LN.mine(LN.split_rows(rows)[0], NAMES, min_n=15)
        top = found[0][0]
        self.assertEqual(sorted(c.feature for c in top.conditions), ['dev_holding_pct', 'top10_pct'])
        by_feature = {c.feature: c for c in top.conditions}
        self.assertEqual((by_feature['dev_holding_pct'].op, by_feature['top10_pct'].op), ('>', '>'))
        pair_gain = found[0][1]['train_gain_per_candidate_sol']
        singles = [m['train_gain_per_candidate_sol'] for r, m in found if len(r.conditions) == 1]
        self.assertTrue(singles and pair_gain > max(singles))

    def test_minimum_support_is_enforced(self):
        rows = planted(200)
        mine_rows = LN.split_rows(rows)[0]
        for rule, stats in LN.mine(mine_rows, NAMES, min_n=40):
            self.assertGreaterEqual(stats['train_rejected'], 40)
        self.assertEqual(LN.mine(mine_rows, NAMES, min_n=10 ** 6), [])

    def test_a_rule_that_rejects_winners_is_never_mined(self):
        rows = [row(i, 'WIN', 0.05, {'dev_holding_pct': i}) for i in range(100)]
        self.assertEqual(LN.mine(rows, NAMES, min_n=5), [])

    def test_deterministic_order_and_top_k(self):
        rows = LN.split_rows(planted())[0]
        a, b = LN.mine(rows, NAMES, min_n=15, top_k=7), LN.mine(rows, NAMES, min_n=15, top_k=7)
        self.assertEqual([(r.text(), s) for r, s in a], [(r.text(), s) for r, s in b])
        self.assertLessEqual(len(a), 7)
        gains = [s['train_gain_per_candidate_sol'] for _, s in a]
        self.assertEqual(gains, sorted(gains, reverse=True))

    def test_empty_and_constant_features(self):
        self.assertEqual(LN.mine([], NAMES), [])
        rows = [row(i, 'RUG', -0.1, {'dev_holding_pct': 5}) for i in range(60)]
        self.assertEqual(LN.mine(rows, NAMES, min_n=5), [])           # nothing to split on: every rule would reject (almost) everything

    def test_a_rule_that_stops_trading_is_not_a_selection_rule(self):
        rows = [row(i, 'RUG', -0.1, {'dev_holding_pct': i}) for i in range(100)]          # every candidate loses
        for rule, stats in LN.mine(rows, NAMES, min_n=5):
            self.assertLessEqual(stats['train_rejected'], 80)
        found = LN.mine(rows, NAMES, min_n=5, max_reject_fraction=1.0)
        self.assertTrue(any(s['train_rejected'] > 80 for _, s in found))
        chosen, evidence = LN.select_rules([(LN.Rule((LN.Condition('dev_holding_pct', '>', D(-1)),)), {})], rows, min_n=5)
        self.assertEqual(chosen, [])
        self.assertIn('REJECTS_TOO_MUCH', evidence[0]['flags'])

    def test_a_pair_must_beat_both_of_its_parts(self):
        rng = random.Random(11)
        rows = []
        for i in range(800):
            dev, junk = rng.uniform(0, 30), rng.uniform(0, 100)
            rug = dev > 8 and rng.random() < 0.9
            rows.append(row(i, 'RUG' if rug else 'FLAT', -0.08 + rng.gauss(0, 0.01) if rug else 0.01 + rng.gauss(0, 0.04),
                            {'dev_holding_pct': round(dev, 2), 'top10_pct': round(junk, 2)}))
        found = LN.mine(rows, NAMES, min_n=15)
        gains = {r.text(): s['train_gain_per_candidate_sol'] for r, s in found}
        for rule, stats in found:
            if len(rule.conditions) == 2:
                for c in rule.conditions:
                    alone = LN.evaluate(rows, [LN.Rule((c,))])['gain_per_candidate_sol']
                    self.assertGreater(stats['train_gain_per_candidate_sol'], alone - 1e-12, rule.text())


class SelectTests(unittest.TestCase):
    def test_flags_explain_why_a_candidate_was_not_chosen(self):
        rows = planted()
        mine_rows, select_rows, _, _ = LN.split_rows(rows)
        cands = LN.mine(mine_rows, NAMES, min_n=15)
        chosen, evidence = LN.select_rules(cands, select_rows, min_n=15)
        self.assertTrue(chosen)
        self.assertTrue(all(not e['flags'] for e in evidence if e['rule'] in chosen))
        _, strict = LN.select_rules(cands, select_rows, min_n=10 ** 6)
        self.assertTrue(all('INSUFFICIENT' in e['flags'] for e in strict))
        self.assertEqual(LN.select_rules(cands, select_rows, min_n=10 ** 6)[0], [])

    def test_a_rule_that_loses_on_select_is_dropped(self):
        rows = planted()
        mine_rows, select_rows, _, _ = LN.split_rows(rows)
        cands = LN.mine(mine_rows, NAMES, min_n=15)
        flipped = [LN.Row(r.mint, r.ts, r.label, r.features, -r.pnl_lamports, True) for r in select_rows]     # on this window the rejects would have WON
        chosen, evidence = LN.select_rules(cands, flipped, min_n=15)
        self.assertEqual(chosen, [])
        self.assertTrue(all('NO_GAIN' in e['flags'] for e in evidence))

    def test_a_barely_positive_rule_must_also_be_significant(self):
        rng = random.Random(3)
        select_rows = [row(i, 'FLAT', rng.gauss(0, 0.05), {'dev_holding_pct': rng.uniform(0, 30)}) for i in range(300)]
        rule = LN.Rule((LN.Condition('dev_holding_pct', '>', D(8)),))
        chosen, evidence = LN.select_rules([(rule, {})], select_rows, min_n=15)
        self.assertEqual(chosen, [])
        self.assertIn('NOT_SIGNIFICANT', evidence[0]['flags'])

    def test_at_most_max_rules_and_each_adds_something(self):
        rows = planted(1200)
        mine_rows, select_rows, _, _ = LN.split_rows(rows)
        cands = LN.mine(mine_rows, NAMES, min_n=15)
        chosen, _ = LN.select_rules(cands, select_rows, min_n=15, max_rules=2)
        self.assertLessEqual(len(chosen), 2)
        if len(chosen) == 2:
            self.assertGreater(LN.evaluate(select_rows, chosen)['gain_per_candidate_sol'], LN.evaluate(select_rows, chosen[:1])['gain_per_candidate_sol'])

    def test_confirm_criteria(self):
        good = {'n_rejected': 40, 'gain_per_candidate_sol': 0.01, 'rejected_t': -3.0}
        self.assertTrue(LN.confirmed(good, min_confirm=30))
        for bad in (dict(good, n_rejected=29), dict(good, gain_per_candidate_sol=0.0), dict(good, gain_per_candidate_sol=None),
                    dict(good, rejected_t=-2.0), dict(good, rejected_t=None)):
            self.assertFalse(LN.confirmed(bad, min_confirm=30), bad)


class LearnTests(unittest.TestCase):
    def test_planted_structure_is_recovered_and_confirmed(self):
        r = run(planted())
        self.assertEqual(r['proposal_status'], 'CONFIRMED_NO_DIRECTORY_GIVEN')
        self.assertTrue(r['selected'])
        first = r['selected'][0]['if'][0]
        self.assertEqual((first['feature'], first['op']), ('dev_holding_pct', '>'))
        self.assertTrue(2 <= D(first['value']) <= 12)
        conf = r['union']['confirm']
        self.assertGreater(conf['gain_per_candidate_sol'], 0)
        self.assertGreater(conf['rug_rate_rejected'], conf['rug_rate_kept'])
        self.assertLessEqual(conf['rejected_t'], -LN.Z_CONFIRM)
        self.assertGreater(r['union']['confirm']['policy_mean_pnl_sol'], r['baseline']['confirm']['baseline_mean_pnl_sol'])

    def test_pure_noise_produces_no_confirmed_rule(self):
        for seed in range(1, 41):
            r = run(noise(seed=seed))
            self.assertNotEqual(r['proposal_status'], 'WRITTEN', seed)
            self.assertNotEqual(r['proposal_status'], 'CONFIRMED_NO_DIRECTORY_GIVEN', seed)
            self.assertIn(r['proposal_status'], ('NO_RULE_FOUND', 'NO_SELECTED_RULE', 'NOT_CONFIRMED'), seed)

    def test_noise_does_reach_the_confirm_gate_so_the_gate_is_what_stops_it(self):
        """Some noise datasets get a rule through mining AND selection with a positive confirm gain: only the significance gate stops them."""
        reached = [seed for seed in range(1, 41) if run(noise(seed=seed))['proposal_status'] == 'NOT_CONFIRMED']
        self.assertTrue(reached)
        weak = [seed for seed in reached if (run(noise(seed=seed))['union']['confirm']['gain_per_candidate_sol'] or 0) > 0]
        self.assertTrue(weak, 'no noise seed has a positive but insignificant confirm gain: the confirm gate is untested')

    def test_the_confirm_window_never_chooses_anything(self):
        base = planted()
        a = run(base)
        n_conf = a['split']['confirm']
        changed = base[:-n_conf] + [LN.Row(r.mint, r.ts, r.label, {k: (None if v is None else -v) for k, v in r.features.items()}, -r.pnl_lamports, True)
                                    for r in base[-n_conf:]]
        b = run(changed)
        self.assertEqual(a['candidates'], b['candidates'])
        self.assertEqual(a['selected'], b['selected'])
        self.assertEqual(a['union']['mine'], b['union']['mine'])
        self.assertEqual(a['union']['select'], b['union']['select'])
        self.assertNotEqual(a['union']['confirm'], b['union']['confirm'])
        self.assertEqual(b['proposal_status'], 'NOT_CONFIRMED')
        self.assertIs(a['confirm_used_for_selection'], False)

    def test_a_chosen_set_is_accepted_or_rejected_as_a_whole(self):
        r = run(planted(1500), max_rules=3)
        if len(r['selected']) >= 2:
            self.assertEqual(r['union']['confirm']['n_rejected'], LN.evaluate(LN.split_rows([x for x in planted(1500)])[2], [
                LN.Rule(tuple(LN.Condition(c['feature'], c['op'], D(c['value'])) for c in rule['if'])) for rule in r['selected']])['n_rejected'])

    def test_only_enterable_candidates_are_mined_but_tables_use_all(self):
        rows = planted()
        mixed = [LN.Row(r.mint, r.ts, r.label, r.features, r.pnl_lamports, i % 3 != 0) for i, r in enumerate(rows)]
        r = run(mixed)
        self.assertEqual(r['counts']['candidates'], 600)
        self.assertEqual(r['counts']['enterable'], sum(1 for x in mixed if x.enterable))
        self.assertEqual(sum(t['buckets'][0]['n'] for t in [r['tables']['dev_holding_pct']]) > 0, True)
        self.assertEqual(sum(b['n'] for b in r['tables']['dev_holding_pct']['buckets']), 600)

    def test_too_few_candidates(self):
        with self.assertRaises(LN.LearnError):
            run([row(0), row(1)])

    def test_bad_minimums_are_refused(self):
        for kw in ({'min_n': 0}, {'min_n': True}, {'min_confirm': -1}, {'min_bucket': 1.5}):
            with self.assertRaises(LN.LearnError, msg=kw):
                run(planted(60), **kw)

    def test_default_minimums_are_the_documented_ones(self):
        self.assertEqual((LN.MIN_N, LN.MIN_CONFIRM, LN.MIN_BUCKET), (50, 30, 20))
        r = LN.learn(planted(200), now=NOW, feature_names=NAMES)            # 50 / 30 with only ~50 per window: nothing can be chosen or confirmed
        self.assertNotIn(r['proposal_status'], ('WRITTEN', 'CONFIRMED_NO_DIRECTORY_GIVEN'))
        self.assertTrue(any('noise-dominated' in w for w in r['warnings']))

    def test_insufficient_support_flag_below_min_n(self):
        r = run(planted(), min_n=15)
        r2 = run(planted(), min_n=1000)
        self.assertEqual(r2['proposal_status'], 'NO_RULE_FOUND')
        self.assertNotEqual(r['proposal_status'], 'NO_RULE_FOUND')

    def test_deterministic_and_json_safe(self):
        a, b = run(planted()), run(planted())
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        json.dumps(a, allow_nan=False)


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
        self.assertEqual(r['proposal_status'], 'WRITTEN')
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
        for window in ('mine', 'select', 'confirm'):
            self.assertIn(window, doc['evidence']['union'])
        self.assertEqual((doc['evidence']['chosen_on'], doc['evidence']['confirmed_on']), ('select', 'confirm'))
        self.assertIn('selection bias', ' '.join(doc['warnings']))
        self.assertTrue(any(x.startswith('wait_for_enrichment:') and 'dev_holding_pct' in x for x in doc['requires_runner_change']))
        self.assertEqual(hashlib.sha256(DEFAULT.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in props.iterdir()), [path.name])
        with self.assertRaises(FileExistsError):
            run(planted(), proposals_dir=props, strategy=self.strategy)

    def test_nothing_is_written_without_confirmation(self):
        props = self.dir / 'proposals'
        r = run(noise(seed=3), proposals_dir=props, strategy=self.strategy)
        self.assertNotEqual(r['proposal_status'], 'WRITTEN')
        self.assertIsNone(r['proposal'])
        self.assertFalse(props.exists())

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
        for needle in ('What winners vs rugs looked like at entry', 'PAPER ONLY', 'Never applied automatically', 'SELECT window', 'dev_holding_pct',
                       'rug rate', 'Quantile tables', 'CHOSEN', 'NOT used to choose'):
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
