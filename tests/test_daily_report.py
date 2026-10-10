"""tools.research.daily_report: fixture stores with known numbers (SYNTHETIC_TEST_ONLY, no network, no provider)."""
import contextlib
import io
import json
import os
import re
import socket
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from tests import test_forward_eval as fwd
from tests import test_funnel_report as ffix
from tests.test_shadow_strategies import A as SHADOW_A, BASE as SHADOW_BASE, PATHS, candidate as shadow_candidate
from tools.research import counterfactual as cf
from tools.research import daily_report as dr
from tools.research import forward_eval, shadow_strategies as ss

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_000_000.0
NOW = 1_700_000_000.0           # 2023-11-14T22:13:20Z
EVIL = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'


def ok(data):
    return {'status': 'OK', 'data': data}


def funnel(candidates=120, buys=6, unresolved=None):
    stages = ['discovered', 'selectable', 'dispatched', 'admitted', 'history_ok', 'observations_ok', 'engine_eligible', 'buy']
    reached = dict(zip(stages, [candidates, 100, 80, 60, 40, 30, 12, buys]))
    return {'kind': 'funnel_report_v1', 'candidates': candidates, 'stages': stages,
            'unresolved': {'count': sum((unresolved or {}).values()), 'by_code': unresolved or {}},
            'latch_check': {'status': 'READ', 'null_passes': 0, 'no_entry_receipts': 3},
            'total': {'candidates': candidates, 'reached': reached,
                      'died_before': {'engine_eligible': {'ENGINE:' + EVIL: 18, 'COST_BUDGET': 5}, 'dispatched': {'AGE_WINDOW': 20}}, 'pending': {}}}


def cf_report(rejected_mean='0.50', rejected_n=40, bought_mean='0.10', bought_n=35):
    def group(n, mean):
        return {'candidates': n, 'with_return': n, 'baseline_missing': 0, 'pool_died': 0, 'dead_at_baseline': 0, 'dead_at_horizon': 0,
                'mean_return': mean, 'median_return': mean, 'p10_return': mean, 'p90_return': mean, 'share_positive': '1', 'median_max_gain': mean}
    return {'label': cf.LABEL, 'horizon_seconds': 3600, 'distinct_candidates': rejected_n + bought_n, 'unknown_breakdown': {},
            'groups': {'BOUGHT': group(bought_n, bought_mean), 'REJECTED:ENGINE:COST_BUDGET': group(rejected_n, rejected_mean)}}


def health(status='OK'):
    return {'kind': 'desk_healthcheck_v1', 'status': status,
            'checks': [{'check': 'ledger', 'severity': 'OK', 'detail': 'fine'}] +
                      ([{'check': 'discovery' + EVIL, 'severity': status, 'detail': EVIL}] if status != 'OK' else [])}


def budgets(used=1200, cap=3600):
    return {'status': 'OK', 'monitoring': {'cap': cap, 'used_in_window': used, 'remaining_in_window': cap - used, 'pending_reservations': 0,
                                           'blocked': None, 'total': 5000, 'high_water': 3000},
            'ownership': {'exhausted': 0, 'total': 4, 'budgets': []}}


def forward_data(trades=4, net='0.0123'):
    block = {'closed_trades': trades, 'wins': 2, 'losses': 2, 'flat': 0, 'net_pnl_sol': net, 'mean_return': '0.01', 'max_drawdown_fraction': '0.02',
             'mean_return_bootstrap_ci95': ['-0.05', '0.06']}
    return {'kind': 'forward_eval_v2', 'experiments': [{'config_hash': 'a' * 64, 'version': 'v-' + EVIL, 'all': block, 'open_positions_excluded': 1}]}


def sections(**changes):
    base = {'health': ok(health()), 'budgets': ok(budgets()), 'funnel': ok(funnel()), 'counterfactual': ok(cf_report()),
            'shadow': dr._not_provided('x'), 'forward': ok(forward_data()), 'realism': ok({'status': 'NO_REALISM_DATA', 'fills': 4})}
    base.update(changes)
    return base


def sh_horizons():
    from tools.research import shadow_strategies as ss
    return tuple(ss.HORIZONS)


class TempDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)


class MetricsAndAdviceTests(unittest.TestCase):
    def test_metrics_are_the_exact_source_numbers(self):
        m = dr.metrics_of(sections())
        self.assertEqual((m['funnel_candidates'], m['funnel_buys'], m['funnel_unresolved']), (120, 6, 0))
        self.assertEqual((m['counterfactual_candidates'], m['forward_closed_trades'], m['forward_net_pnl_sol']), (75, 4, '0.0123'))
        self.assertEqual((m['monitoring_used_in_window'], m['monitoring_cap'], m['health_status']), (1200, 3600, 'OK'))

    def test_unresolved_latch_is_the_first_changed_line_and_the_first_advice(self):
        s = sections(funnel=ok(funnel(unresolved={'UNRESOLVED:MARKET_PRODUCER_BLOCKED': 2})))
        summary = dr.build(s, now=NOW)
        self.assertTrue(summary['changed'][0].startswith('ALERT: 2 UNRESOLVED'))
        self.assertTrue(summary['next'][0].startswith('Resolve the UNRESOLVED latch first'))

    def test_insufficient_sample_is_stated_with_the_exact_n(self):
        summary = dr.build(sections(), now=NOW)
        self.assertIn('INSUFFICIENT_SAMPLE: 4 closed paper trades < 30', ' '.join(summary['next']))
        enough = dr.build(sections(forward=ok(forward_data(trades=30))), now=NOW)
        self.assertNotIn('INSUFFICIENT_SAMPLE: 30', ' '.join(enough['next']))

    def test_filter_that_rejects_winners_is_named_only_with_enough_samples(self):
        text = ' '.join(dr.build(sections(), now=NOW)['next'])
        self.assertIn('Review the filter behind REJECTED:ENGINE:COST_BUDGET', text)
        self.assertIn('50.00%', text)
        self.assertIn('n=40', text)
        self.assertIn('10.00% for bought', text)
        thin = ' '.join(dr.build(sections(counterfactual=ok(cf_report(rejected_n=29))), now=NOW)['next'])
        self.assertNotIn('Review the filter', thin)
        self.assertNotIn('non-harmful', thin)                 # T42: n=29 is not evidence that a filter is harmless
        self.assertIn('INSUFFICIENT_SAMPLE', thin)
        worse = ' '.join(dr.build(sections(counterfactual=ok(cf_report(rejected_mean='0.05'))), now=NOW)['next'])
        self.assertNotIn('Review the filter', worse)
        self.assertIn('non-harmful', worse)

    def test_non_harmful_is_never_claimed_without_an_adequate_rejection_group(self):
        """T42 item 3: 'filters look non-harmful' needs at least one REJECTED group with n >= 30 that does not beat the baseline."""
        for label, report in (('every group below 30', cf_report(rejected_n=29, bought_n=29)),
                              ('bought adequate, rejected thin', cf_report(rejected_n=29, bought_n=60)),
                              ('rejected thin, no bought baseline', cf_report(rejected_n=5, bought_n=0)),
                              ('empty groups', dict(cf_report(), groups={}))):
            with self.subTest(label):
                text = ' '.join(dr.build(sections(counterfactual=ok(report)), now=NOW)['next'])
                self.assertNotIn('non-harmful', text)
        text = ' '.join(dr.build(sections(counterfactual=ok(cf_report(rejected_n=29, bought_n=29))), now=NOW)['next'])
        self.assertIn('INSUFFICIENT_SAMPLE: every counterfactual group has n<30', text)
        self.assertIn('largest rejection group n=29', text)

    def test_non_harmful_is_claimed_at_exactly_n_30_with_a_baseline_beating_group(self):
        text = ' '.join(dr.build(sections(counterfactual=ok(cf_report(rejected_n=30, rejected_mean='0.05'))), now=NOW)['next'])
        self.assertIn('non-harmful', text)
        self.assertNotIn('INSUFFICIENT_SAMPLE: every counterfactual group', text)

    def test_health_alert_and_section_errors_are_visible_not_swallowed(self):
        s = sections(health=ok(health('CRITICAL')), forward={'status': 'ERROR', 'error': 'EvalError', 'detail': EVIL})
        summary = dr.build(s, now=NOW)
        joined = ' '.join(summary['changed'])
        self.assertIn('healthcheck status is CRITICAL', joined)
        self.assertIn('section forward could not be built (EvalError)', joined)
        self.assertEqual(summary['section_status']['forward'], 'ERROR')

    def test_changed_lines_show_signed_deltas_against_the_previous_report(self):
        yesterday = dr.build(sections(funnel=ok(funnel(candidates=100, buys=4)), forward=ok(forward_data(trades=2, net='0.01'))), now=NOW - 86400)
        today = dr.build(sections(), now=NOW, previous=yesterday)
        joined = '\n'.join(today['changed'])
        self.assertIn('candidates seen: 120 (+20 vs 2023-11-13)', joined)
        self.assertIn('paper buys: 6 (+2 vs 2023-11-13)', joined)
        self.assertIn('closed paper trades: 4 (+2 vs 2023-11-13)', joined)
        self.assertIn('net paper PnL (SOL): 0.0123 (+0.0023 vs 2023-11-13)', joined)
        self.assertEqual(today['previous_date'], '2023-11-13')
        down = dr.build(sections(funnel=ok(funnel(candidates=90))), now=NOW, previous=yesterday)
        self.assertIn('candidates seen: 90 (-10 vs 2023-11-13)', '\n'.join(down['changed']))

    def test_no_previous_report_is_said_plainly(self):
        self.assertIn('No earlier report found', ' '.join(dr.build(sections(), now=NOW)['changed']))


class ShadowSectionTests(unittest.TestCase):
    def shadow_data(self, min_trades=30):
        candidates = [shadow_candidate(PATHS['pump'], mint='M%d' % i, migrated=T0 + i) for i in range(3)]
        grid = ss.load_grid(ROOT / 'config/experiments/shadow/exit-grid.json')
        return ss.run(candidates, grid, holdout_from=T0 + 1, min_trades=min_trades)

    def test_unranked_leaderboard_says_not_to_pick_a_variant(self):
        data = self.shadow_data()
        self.assertFalse([r for r in data['leaderboard'] if r.get('rank')])
        summary = dr.build(sections(shadow=ok(data)), now=NOW)
        self.assertEqual(summary['metrics']['shadow_ranked_variants'], 0)
        self.assertIn('no variant has the minimum holdout trades', ' '.join(summary['next']))
        page = dr.render_html(summary, sections(shadow=ok(data)))
        self.assertIn('UNRANKED_FEWER_THAN_30_HOLDOUT_TRADES', page)
        self.assertIn('INSUFFICIENT_SAMPLE', page)

    def test_a_ranked_variant_with_ci_above_zero_is_a_replicate_first_candidate(self):
        data = self.shadow_data(min_trades=1)
        ranked = [r for r in data['leaderboard'] if r.get('rank')]
        self.assertTrue(ranked)
        top = ranked[0]
        top['holdout']['bootstrap_ci'] = ('0.2', '0.4')
        top['holdout']['trades'] = 40
        text = ' '.join(dr.build(sections(shadow=ok(data)), now=NOW)['next'])
        self.assertIn('Shadow candidate: %s' % top['variant'], text)
        self.assertIn('replicate on new data', text)
        top['holdout']['bootstrap_ci'] = ('-0.2', '0.4')
        self.assertIn('distinguishable from noise', ' '.join(dr.build(sections(shadow=ok(data)), now=NOW)['next']))


class HtmlTests(unittest.TestCase):
    def page(self, **changes):
        s = sections(**changes)
        return dr.render_html(dr.build(s, now=NOW), s)

    def test_every_dynamic_string_is_escaped_and_the_page_is_self_contained(self):
        page = self.page(health=ok(health('CRITICAL')), forward={'status': 'ERROR', 'error': 'X', 'detail': EVIL})
        self.assertNotIn('<script', page.lower())
        self.assertNotIn('<img', page.lower())
        self.assertNotIn('onerror=alert(2)>', page)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', page)
        for forbidden in ('http://', 'https://', '@import', '<link', '<iframe', 'url('):
            self.assertNotIn(forbidden, page)
        self.assertIsNone(re.search(r'<[^>]*\bsrc\s*=', page))       # no real tag loads anything (the escaped text above is inert)

    def test_labels_and_sample_sizes_are_on_the_page(self):
        page = self.page()
        self.assertIn('PAPER ONLY', page)
        self.assertIn('EXECUTION_UNVERIFIED', page)
        self.assertIn('NOT_ASSESSED', page)
        self.assertIn('n=120', page)
        self.assertIn('trades=4 INSUFFICIENT_SAMPLE', page)
        self.assertIn('n=40', page)
        self.assertNotIn('n=40 INSUFFICIENT_SAMPLE', page)

    def test_known_funnel_and_budget_numbers_are_rendered(self):
        page = self.page()
        self.assertRegex(page, r'<td>buy</td><td class="n">6</td><td class="n">5\.00%</td>')
        self.assertIn('1200 / 3600', page)

    def test_the_changed_and_next_blocks_are_capped_at_ten_lines_with_a_pointer_to_the_json(self):
        summary = dr.build(sections(), now=NOW)
        summary['changed'] = ['line %d' % i for i in range(14)]
        page = dr.render_html(summary, sections())
        self.assertIn('<li>line 9</li>', page)
        self.assertNotIn('<li>line 10</li>', page)
        self.assertIn('+4 more in the JSON summary', page)

    def test_empty_and_young_data_render_without_division_or_crash(self):
        s = {'health': dr._not_provided('x'), 'budgets': dr._not_provided('x'), 'funnel': ok(funnel(candidates=0, buys=0)),
             'counterfactual': ok({'label': cf.LABEL, 'horizon_seconds': 3600, 'distinct_candidates': 0, 'unknown_breakdown': {}, 'groups': {}}),
             'shadow': dr._not_provided('x'), 'forward': ok({'kind': 'forward_eval_v2', 'experiments': []}), 'realism': dr._not_provided('x')}
        summary = dr.build(s, now=NOW)
        page = dr.render_html(summary, s)
        self.assertIn('n=0 INSUFFICIENT_SAMPLE', page)
        self.assertIn('NOT_PROVIDED', page)
        self.assertIn('No experiment.', page)
        self.assertIn('Counterfactual store has no outcome groups yet', ' '.join(summary['next']))


class RealToolTests(TempDir):
    """The imported tools run for real on fixture stores; the numbers below are the fixtures' known answers."""

    def counterfactual_store(self, rejected=31, bought=30, horizons=(300, 3600)):
        store = self.dir / 'counterfactual.sqlite'
        cf.init(store, allowance_per_hour=300, now=T0 - 10)
        plan = [('REJECTED', rejected, 150), ('BOUGHT', bought, 110)]
        seq, minted = 0, []
        for kind, n, hour_price in plan:
            for _ in range(n):
                seq += 1
                mint = 'SYNTH%03d' % seq
                cf.add_candidate(store, mint=mint, pool='P' + mint, signature='sig%d' % seq, slot=1, migrated_at=T0, seq=seq, now=T0)
                minted.append((kind, hour_price, mint))
        with closing(sqlite3.connect(store)) as c:
            for kind, hour_price, mint in minted:
                if True:
                    for horizon, price in tuple((h, 100) for h in horizons if h != 3600) + ((3600, hour_price),):
                        c.execute("INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,'OK',NULL,NULL)", (mint, horizon, T0 + horizon, T0 + horizon, 1, '1', '1', str(price)))
                    payload = ({'v': 2, 'class': 'REJECTED', 'stage': 'ENGINE', 'codes': ['COST_BUDGET'], 'detail': None} if kind == 'REJECTED'
                               else {'v': 2, 'class': 'BOUGHT', 'stage': None, 'codes': [], 'detail': None})
                    c.execute('INSERT INTO outcomes(mint,at,payload) VALUES(?,?,?)', (mint, T0, json.dumps(payload)))
            c.commit()
        return store

    def test_collect_runs_the_real_tools_and_reports_known_numbers(self):
        ledger = self.dir / 'ledger.sqlite'
        expected = fwd.build(ledger)
        store = self.counterfactual_store()
        found = dr.collect({'ledger': str(ledger), 'counterfactual_store': str(store), 'resamples': 50, 'grid': str(ROOT / 'config/experiments/shadow/exit-grid.json')}, NOW)
        self.assertEqual({n: found[n]['status'] for n in ('health', 'budgets', 'funnel')}, {n: 'NOT_PROVIDED' for n in ('health', 'budgets', 'funnel')})
        self.assertEqual(found['counterfactual']['status'], 'OK', found['counterfactual'])
        groups = found['counterfactual']['data']['groups']
        self.assertEqual((groups['BOUGHT']['with_return'], groups['REJECTED:ENGINE:COST_BUDGET']['with_return']), (30, 31))
        self.assertEqual((groups['BOUGHT']['mean_return'], groups['REJECTED:ENGINE:COST_BUDGET']['mean_return']), ('0.1', '0.5'))
        self.assertEqual(found['forward']['status'], 'OK', found['forward'])
        row = found['forward']['data']['experiments'][0]
        self.assertEqual(row['all']['closed_trades'], len(expected))
        self.assertEqual(sum(__import__('decimal').Decimal(row['all']['net_pnl_sol']) for _ in [0]), sum(e['pnl'] for e in expected))
        self.assertEqual(found['realism']['data']['status'], 'NO_REALISM_DATA')
        summary = dr.build(found, now=NOW)
        self.assertIn('Review the filter behind REJECTED:ENGINE:COST_BUDGET', ' '.join(summary['next']))
        self.assertIn('INSUFFICIENT_SAMPLE: %d closed paper trades < 30' % len(expected), ' '.join(summary['next']))

    def features_store(self, mints=('SYNTH001',), at=T0 + 10, name='features.sqlite'):
        from tools.research import features as ft
        path = self.dir / name
        ft.init(path)
        records = {(m, float(at)): {'features': {'flow': '90', 'top10_pct': '10'},
                                    'provenance': {k: {'source': 'ledger.events', 'ref': 'e#h', 'at': float(at), 'role': 'input'}
                                                   for k in ('flow', 'top10_pct')}} for m in mints}
        ft.append_rows(path, records, now=NOW)
        return path

    def shadow(self, **extra):
        ledger = self.dir / 'ledger.sqlite'
        fwd.build(ledger)
        # complete price paths (every horizon), otherwise the shadow engine sets every candidate aside as truncated
        store = self.counterfactual_store(horizons=sh_horizons())
        opts = {'ledger': str(ledger), 'counterfactual_store': str(store), 'resamples': 50,
                'grid': str(ROOT / 'config/experiments/shadow/exit-grid.json'), **extra}
        found = dr.collect(opts, NOW)
        self.assertEqual(found['shadow']['status'], 'OK', found['shadow'])
        return found

    def test_shadow_runs_with_the_real_features_of_the_store_and_says_which_were_real(self):
        found = self.shadow(features_store=str(self.features_store()))
        data = found['shadow']['data']
        self.assertEqual(data['features_supplied'], ['SYNTH001'])
        label = data['features_label']
        self.assertEqual((label['source'], label['mode']), ('features_store', 'REAL_FEATURES_FOR_SOME_CANDIDATES'))
        self.assertEqual((label['real_candidates'], label['candidates']), (1, 61))
        self.assertEqual(label['neutral_candidates'], 60)
        self.assertEqual(label['fields'], ['flow', 'top10_pct'])
        page = dr.render_html(dr.build(found, now=NOW), found)
        self.assertIn('real features for 1 of 61 candidates', page)
        self.assertIn('neutral features for 60', page)

    def test_without_a_features_store_everything_is_labelled_neutral(self):
        label = self.shadow()['shadow']['data']['features_label']
        self.assertEqual((label['source'], label['mode'], label['real_candidates'], label['neutral_candidates']),
                         (None, 'NEUTRAL_FEATURES_ONLY', 0, 61))

    def test_a_missing_features_store_is_neutral_not_an_error(self):
        found = self.shadow(features_store=str(self.dir / 'absent.sqlite'))
        self.assertEqual(found['shadow']['data']['features_label']['mode'], 'NEUTRAL_FEATURES_ONLY')

    def test_an_explicit_features_file_wins_over_the_store_and_is_labelled_file(self):
        explicit = self.dir / 'features.json'
        explicit.write_text(json.dumps({'SYNTH002': {'as_of': T0 + 20, 'flow': '55'}}))
        found = self.shadow(features_store=str(self.features_store()), features=str(explicit))
        data = found['shadow']['data']
        self.assertEqual(data['features_supplied'], ['SYNTH002'])
        self.assertEqual((data['features_label']['source'], data['features_label']['real_candidates']), ('file', 1))

    def test_a_features_store_row_after_the_price_path_is_refused_as_look_ahead_not_used(self):
        found = self.shadow(features_store=str(self.features_store(at=T0 + 10 ** 6)))
        self.assertEqual(found['shadow']['status'], 'OK')
        self.assertEqual(found['shadow']['data']['features_label']['real_candidates'], 0)

    def test_the_features_store_is_read_only_and_never_written(self):
        path = self.features_store()
        before = path.read_bytes()
        self.shadow(features_store=str(path))
        self.assertEqual(path.read_bytes(), before)

    def test_a_failing_tool_degrades_only_its_own_section(self):
        ledger = self.dir / 'ledger.sqlite'
        fwd.build(ledger)
        with mock.patch.object(forward_eval, 'evaluate', side_effect=forward_eval.EvalError('<b>boom</b>')):
            found = dr.collect({'ledger': str(ledger)}, NOW)
        # T42: the class name only; the exception text can carry paths or secret-bearing provider messages and is dropped
        self.assertEqual(found['forward'], {'status': 'ERROR', 'error': 'EvalError'})
        self.assertEqual(found['realism']['status'], 'OK')
        page = dr.render_html(dr.build(found, now=NOW), found)
        self.assertIn('EvalError', page)
        self.assertNotIn('boom', page)

    def test_an_error_section_never_carries_the_exception_text(self):
        for text in ('SECRET api-key=abc /var/lib/x.sqlite', '<script>x</script>', 'x' * 500):
            with self.subTest(text[:12]):
                result = dr._safe(lambda: (_ for _ in ()).throw(RuntimeError(text)))
                self.assertEqual(result, {'status': 'ERROR', 'error': 'RuntimeError'})

    def test_an_unreadable_path_is_an_error_section_not_a_crash(self):
        found = dr.collect({'ledger': str(self.dir / 'missing.sqlite'), 'counterfactual_store': str(self.dir / 'nope.sqlite')}, NOW)
        for name in ('forward', 'realism', 'counterfactual', 'shadow'):
            self.assertEqual(found[name]['status'], 'ERROR', name)

    def test_empty_stores_produce_zero_numbers_not_errors(self):
        from desk.ledger import Ledger
        ledger = self.dir / 'empty.sqlite'
        Ledger(ledger).close()
        store = self.dir / 'cf.sqlite'
        cf.init(store, now=T0)
        found = dr.collect({'ledger': str(ledger), 'counterfactual_store': str(store)}, NOW)
        self.assertEqual(found['counterfactual']['data']['distinct_candidates'], 0)
        summary = dr.build(found, now=NOW)
        dr.render_html(summary, found)


class RealFunnelShapeTests(ffix.FunnelFixture):
    """The REAL funnel_report.build output (13 candidates, known fates) flows through the daily report."""

    def test_real_funnel_numbers_alerts_and_escaping(self):
        section = ok(self.build())
        s = sections(funnel=section)
        summary = dr.build(s, now=NOW)
        self.assertEqual((summary['metrics']['funnel_candidates'], summary['metrics']['funnel_buys'], summary['metrics']['funnel_unresolved']), (13, 2, 2))
        self.assertTrue(summary['changed'][0].startswith('ALERT: 2 UNRESOLVED latch(es)'))
        page = dr.render_html(summary, s)
        self.assertIn('UNRESOLVED latch', page)
        self.assertRegex(page, r'<td>engine_eligible</td><td class="n">2</td><td class="n">15\.38%</td>')
        self.assertIn('n=13 INSUFFICIENT_SAMPLE', page)


class RealHealthAndBudgetTests(TempDir):
    def test_health_runs_for_real_and_a_critical_status_is_reported_not_hidden(self):
        from desk.ledger import Ledger
        root = self.dir / 'root'
        root.mkdir()
        Ledger(root / 'paper-ledger.sqlite').close()
        found = dr.collect({'root': str(root), 'discovery_db': str(self.dir / 'discovery.sqlite'), 'pacing_db': str(self.dir / 'pacing.sqlite'),
                            'no_systemd': True}, NOW)
        self.assertEqual(found['health']['status'], 'OK')
        self.assertEqual(found['health']['data']['kind'], 'desk_healthcheck_v1')
        self.assertEqual(found['health']['data']['status'], 'CRITICAL')       # the missing stores are not papered over
        summary = dr.build(found, now=NOW)
        self.assertIn('healthcheck status is CRITICAL', ' '.join(summary['changed']))
        self.assertIn('Fix the failing healthcheck items', ' '.join(summary['next']))

    def test_budgets_read_the_monitoring_table_for_real(self):
        evidence = self.dir / 'evidence.sqlite'
        with closing(sqlite3.connect(evidence)) as c:
            c.executescript("""
                CREATE TABLE paper_monitoring_budget(id INTEGER PRIMARY KEY,version INTEGER,cap INTEGER,window_seconds INTEGER,high_water INTEGER,
                    total INTEGER,blocked TEXT,ledger TEXT,config_hash TEXT);
                CREATE TABLE paper_monitoring_reservations(id INTEGER PRIMARY KEY,at REAL,a TEXT,b TEXT,c TEXT,d TEXT,e TEXT);
                CREATE TABLE paper_monitoring_outcomes(reservation_id INTEGER PRIMARY KEY,x TEXT);""")
            c.execute("INSERT INTO paper_monitoring_budget VALUES(1,1,3600,3600,10,10,NULL,'L','H')")
            for i in range(7):
                c.execute('INSERT INTO paper_monitoring_reservations VALUES(?,?,?,?,?,?,?)', (i + 1, NOW - 10 - i, 's', 'm', 'c', 'x', 'p'))
            c.commit()
        ledger = self.dir / 'ledger.sqlite'
        ledger.write_bytes(b'')
        found = dr.collect({'evidence_db': str(evidence), 'ledger': str(ledger)}, NOW)
        self.assertEqual(found['budgets']['status'], 'OK', found['budgets'])
        self.assertEqual((found['budgets']['data']['monitoring']['used_in_window'], found['budgets']['data']['monitoring']['cap']), (7, 3600))
        self.assertEqual(dr.metrics_of(found)['monitoring_used_in_window'], 7)
        self.assertIn('7 / 3600', dr.render_html(dr.build(found, now=NOW), found))


class OutputTests(TempDir):
    def summary(self, now=NOW, **changes):
        s = sections(**changes)
        return dr.build(s, now=now), s

    def test_files_are_created_exclusively_0600_and_never_overwritten(self):
        out = self.dir / 'reports'
        out.mkdir()
        summary, s = self.summary()
        html_path, json_path = dr.write_report(out, summary, s)
        self.assertEqual((html_path.name, json_path.name), ('2023-11-14.html', '2023-11-14.json'))
        self.assertEqual({oct(p.stat().st_mode & 0o777) for p in (html_path, json_path)}, {'0o600'})
        before = (html_path.read_bytes(), json_path.read_bytes())
        other, s2 = self.summary(forward=ok(forward_data(trades=99)))
        with self.assertRaises(dr.ReportError) as raised:
            dr.write_report(out, other, s2)
        self.assertEqual(str(raised.exception), 'REPORT_EXISTS')
        self.assertEqual((html_path.read_bytes(), json_path.read_bytes()), before)
        self.assertEqual(json.loads(json_path.read_text())['metrics']['forward_closed_trades'], 4)

    def test_write_exclusive_itself_refuses_existing_files_and_symlinks_even_if_the_precheck_raced(self):
        target = self.dir / 'x.html'
        target.write_text('keep')
        with self.assertRaises(FileExistsError):
            dr.write_exclusive(target, 'new')
        self.assertEqual(target.read_text(), 'keep')
        victim = self.dir / 'victim.txt'
        victim.write_text('keep')
        link = self.dir / 'y.html'
        link.symlink_to(victim)
        with self.assertRaises(OSError):
            dr.write_exclusive(link, 'new')
        self.assertEqual(victim.read_text(), 'keep')
        fresh = self.dir / 'z.html'
        dr.write_exclusive(fresh, 'ok')
        self.assertEqual((fresh.read_text(), oct(fresh.stat().st_mode & 0o777)), ('ok', '0o600'))

    def test_symlinked_or_missing_out_dir_and_symlinked_target_are_refused(self):
        summary, s = self.summary()
        real = self.dir / 'real'
        real.mkdir()
        link = self.dir / 'link'
        link.symlink_to(real)
        for bad in (link, self.dir / 'missing'):
            with self.assertRaises(dr.ReportError):
                dr.write_report(bad, summary, s)
        victim = self.dir / 'victim.txt'
        victim.write_text('keep')
        (real / '2023-11-14.html').symlink_to(victim)
        with self.assertRaises(dr.ReportError):
            dr.write_report(real, summary, s)
        self.assertEqual(victim.read_text(), 'keep')

    def test_a_failed_json_write_does_not_leave_an_html_only_day(self):
        out = self.dir / 'reports'
        out.mkdir()
        summary, s = self.summary()
        real = dr.write_exclusive
        calls = []

        def flaky(path, text):
            calls.append(path)
            if str(path).endswith('.json'):
                raise OSError('disk full')
            return real(path, text)
        with mock.patch.object(dr, 'write_exclusive', flaky):
            with self.assertRaises(OSError):
                dr.write_report(out, summary, s)
        self.assertEqual(os.listdir(out), [])
        dr.write_report(out, summary, s)      # the day is still free

    def test_find_previous_takes_the_newest_valid_earlier_day_only(self):
        out = self.dir
        good = lambda day, **kw: json.dumps({'kind': dr.KIND, 'date': day, 'metrics': {'funnel_candidates': 1}, **kw})
        (out / '2023-11-10.json').write_text(good('2023-11-10'))
        (out / '2023-11-12.json').write_text(good('2023-11-12'))
        (out / '2023-11-13.json').write_text('{not json')                                  # malformed: skipped
        (out / '2023-11-14.json').write_text(good('2023-11-14'))                           # same day: never "previous"
        (out / '2023-11-15.json').write_text(good('2023-11-15'))                           # future
        (out / '2023-11-11.json').write_text(json.dumps({'kind': 'other', 'date': '2023-11-11', 'metrics': {}}))
        self.assertEqual(dr.find_previous(out, '2023-11-14')['date'], '2023-11-12')
        (out / '2023-11-12.json').write_text(good('2023-11-09'))                           # filename/date mismatch: skipped
        self.assertEqual(dr.find_previous(out, '2023-11-14')['date'], '2023-11-10')
        self.assertIsNone(dr.find_previous(self.dir / 'absent', '2023-11-14'))
        real = out / 'real.json'
        real.write_text(good('2023-11-13'))
        (out / '2023-11-13.json').unlink()
        (out / '2023-11-13.json').symlink_to(real)                                          # symlink: skipped
        self.assertEqual(dr.find_previous(out, '2023-11-14')['date'], '2023-11-10')


class CliTests(TempDir):
    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dr.main(list(argv))
        return code, json.loads(out.getvalue())

    def test_two_days_end_to_end_without_any_network_access(self):
        ledger = self.dir / 'ledger.sqlite'
        fwd.build(ledger)
        reports = self.dir / 'reports'
        reports.mkdir()
        base = ['--out-dir', str(reports), '--ledger', str(ledger), '--resamples', '50']

        def no_network(*a, **k):
            raise AssertionError('the report must not open a socket')
        with mock.patch.object(socket.socket, 'connect', no_network), mock.patch.object(socket, 'create_connection', no_network):
            code, first = self.run_main(*base, '--now', str(NOW - 86400))
            self.assertEqual((code, first['status']), (0, 'WRITTEN'))
            code, second = self.run_main(*base, '--now', str(NOW))
            self.assertEqual((code, second['status']), (0, 'WRITTEN'))
            code, again = self.run_main(*base, '--now', str(NOW))
        self.assertEqual((code, again['error']), (2, 'REPORT_EXISTS'))
        summary = json.loads((reports / '2023-11-14.json').read_text())
        self.assertEqual(summary['previous_date'], '2023-11-13')
        self.assertEqual(summary['section_status']['forward'], 'OK')
        self.assertEqual(summary['section_status']['funnel'], 'NOT_PROVIDED')
        self.assertTrue(any('closed paper trades: 4 (+0 vs 2023-11-13)' in line for line in summary['changed']), summary['changed'])
        self.assertEqual((summary['label'], summary['paper_only'], summary['live_readiness']), ('EXECUTION_UNVERIFIED', True, False))

    def test_root_implies_the_fresh_layout_paths(self):
        derived = dr._derive({'root': '/r'})
        self.assertEqual(derived['ledger'], '/r/paper-ledger.sqlite')
        self.assertEqual(derived['journal'], '/r/entry-dispatch/dispatch.sqlite')
        self.assertEqual(derived['counterfactual_store'], '/r/counterfactual/counterfactual.sqlite')
        self.assertEqual(derived['features_store'], '/r/features/features.sqlite')
        self.assertEqual(dr._derive({'root': '/r', 'ledger': '/x.sqlite'})['ledger'], '/x.sqlite')


class UnitTemplateTests(unittest.TestCase):
    def test_timer_is_daily_at_2330_utc_and_installable(self):
        timer = (ROOT / 'deploy/fresh/desk-daily-report.timer').read_text()
        self.assertIn('OnCalendar=*-*-* 23:30:00 UTC', timer)
        self.assertIn('Unit=desk-daily-report.service', timer)
        self.assertIn('WantedBy=timers.target', timer)

    def test_service_is_read_only_networkless_credentialless_and_resource_limited(self):
        text = (ROOT / 'deploy/fresh/desk-daily-report.service').read_text()
        code = re.sub(r'(?m)^#.*$', '', text)
        self.assertIn('-m tools.research.daily_report', code)
        self.assertIn('--out-dir <STATE_DIR>/reports', code)
        self.assertNotIn('[Install]', text)
        self.assertNotIn('LoadCredential', code)
        self.assertNotIn('provider-keys', code)
        self.assertIn('RestrictAddressFamilies=AF_UNIX', code)
        self.assertNotIn('AF_INET', code)
        # T42: only the reports directory is writable (it used to be the whole state directory)
        self.assertEqual(re.search(r'ReadWritePaths=(.*)', code).group(1).split(), ['<STATE_DIR>/reports'])
        self.assertIn('<FRESH_ROOT>', re.search(r'ReadOnlyPaths=(.*)', code).group(1))
        for limit in ('MemoryMax=', 'TasksMax=', 'CPUQuota=', 'Nice=', 'TimeoutStartSec='):
            self.assertIn(limit, code)

    def test_service_is_hardened_like_the_other_research_units(self):
        """T42 item 5: the same network/CPU/IO isolation as desk-features (T39/T40)."""
        code = re.sub(r'(?m)^#.*$', '', (ROOT / 'deploy/fresh/desk-daily-report.service').read_text())
        features = re.sub(r'(?m)^#.*$', '', (ROOT / 'deploy/fresh/desk-features.service').read_text())

        def value(text, key):
            found = re.findall(r'(?m)^%s=(.*)$' % key, text)
            self.assertEqual(len(found), 1, key)
            return found[0]
        for key in ('PrivateNetwork', 'CPUWeight', 'IOWeight', 'IOSchedulingClass'):   # Nice differs on purpose (15 vs 10)
            self.assertEqual(value(code, key), value(features, key), key)
        self.assertEqual(value(code, 'PrivateNetwork'), 'true')
        self.assertEqual((value(code, 'CPUWeight'), value(code, 'IOWeight')), ('20', '20'))


if __name__ == '__main__':
    unittest.main()
