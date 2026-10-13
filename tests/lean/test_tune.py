"""SYNTHETIC_TEST_ONLY: lean.tune - statistics, the purged walk-forward, the proposal rule, calibration and the CLI.
Paths are synthetic (planted edge / pure noise) written with the real ``lean.paths`` schema."""
import hashlib
import io
import json
import os
import random
import sqlite3
import tempfile
import unittest
from collections import defaultdict
from contextlib import redirect_stdout
from pathlib import Path

from lean import replay as R, strategy as S, tune as T
from tests.lean.test_replay import B, BASE, FLAT0, Q0, SIZE, STEP, make_db

FAMILIES = [{'family': 'live_ladder'}, {'family': 'full_tp', 'x': [1.3, 1.5], 'stop': 0.3}, {'family': 'hold', 'minutes': [1, 5]},
            {'family': 'trailing', 'fraction': 0.2}]


def grid(**kw):
    g = {'families': FAMILIES, 'blocks': 4, 'bootstrap': 200, 'seed': 7, 'alpha': 0.1, 'path_hours': 1, 'min_oos_trades': 20}
    g.update(kw)
    return g


def planted(i, rng):
    """Spikes to ~1.7-2.0x within a minute, then collapses: a quick full take-profit has a real edge, holding loses."""
    top = 1.75 + rng.random() * 0.25
    return [1, 1.05, 1.2 + rng.random() * 0.1, top, top - .1, 1.1, 0.7, 0.5, 0.45, 0.4] + [0.4] * 10


def noise(i, rng):
    ratios, price = [1], 1.0
    for _ in range(25):
        price *= 1 + rng.choice([-1, 1]) * (0.02 + rng.random() * 0.05)
        ratios.append(price)
    return ratios


def dataset(path, n, maker, seed=3, spacing=600.0):
    rng = random.Random(seed)
    return make_db(path, [dict(mint='M%04d' % i, ratios=maker(i, rng), start=1_000_000.0 + spacing * i, entered=int(i % 5 == 0), hours=1) for i in range(n)])


class StatsTests(unittest.TestCase):
    def test_quantile_and_mean(self):
        self.assertEqual(T.quantile([1, 2, 3, 4], .5), 2.5)
        self.assertEqual(T.quantile([5], .1), 5)
        self.assertIsNone(T.quantile([], .5))
        self.assertIsNone(T.mean([]))

    def test_bootstrap_ci_is_seeded_brackets_the_mean_and_shrinks_with_n(self):
        source = random.Random(1)
        values = [source.gauss(0.05, 0.3) for _ in range(40)]
        a = T.mean_ci(values, 500, random.Random(9))
        self.assertEqual(a, T.mean_ci(values, 500, random.Random(9)))
        m, lo, hi = a
        self.assertLess(lo, m)
        self.assertGreater(hi, m)
        bigger = [source.gauss(0.05, 0.3) for _ in range(800)]
        _, lo2, hi2 = T.mean_ci(bigger, 500, random.Random(9))
        self.assertLess(hi2 - lo2, hi - lo)
        self.assertEqual(T.mean_ci([], 100, random.Random(1)), (None, None, None))
        self.assertEqual(T.mean_ci([0.2], 100, random.Random(1)), (0.2, None, None))

    def test_p_value_small_for_an_edge_large_for_noise_and_one_for_a_loss(self):
        rng = random.Random(4)
        edge = [0.2 + rng.gauss(0, 0.1) for _ in range(60)]
        noisy = [rng.gauss(0, 0.3) for _ in range(60)]
        self.assertLess(T.p_value_not_positive(edge, 500, random.Random(1)), 0.01)
        self.assertGreater(T.p_value_not_positive(noisy, 500, random.Random(1)), 0.05)
        self.assertEqual(T.p_value_not_positive([-0.1] * 30, 500, random.Random(1)), 1.0)
        self.assertEqual(T.p_value_not_positive([0.3], 500, random.Random(1)), 1.0)

    def test_benjamini_hochberg_known_answer(self):
        p = [0.01, 0.04, 0.03, 0.005]
        q = T.benjamini_hochberg(p)
        self.assertEqual([round(x, 6) for x in q], [0.02, 0.04, 0.04, 0.02])
        self.assertEqual(T.benjamini_hochberg([]), [])
        self.assertEqual(T.benjamini_hochberg([0.9, 0.95]), [0.95, 0.95])
        self.assertTrue(all(0 <= x <= 1 for x in T.benjamini_hochberg([0, 1, 0.5])))

    def test_describe(self):
        def trade(ts, ret, reasons=('STOP',), past=(), end=False, crossed=0):
            return R.Trade('m', ts, False, ret, 0, 1, list(reasons), [], list(past), end, crossed)
        trades = [trade(1, -0.6, past=[-0.1]), trade(2, -0.2), trade(3, 0.5, ('TAKE_PROFIT',), past=[0.02]), trade(4, 0.3, end=True, crossed=1)]
        d = T.describe(trades, 100, random.Random(1))
        self.assertEqual((d['n'], d['ev']), (4, 0.0))
        self.assertEqual((d['n_first_half'], d['n_second_half']), (2, 2))
        self.assertAlmostEqual(d['ev_first_half'], -0.4)
        self.assertAlmostEqual(d['ev_second_half'], 0.4)
        self.assertEqual(d['p_le_minus_50'], 0.25)
        self.assertEqual(d['exit_reasons'], {'STOP': 3, 'TAKE_PROFIT': 1})
        self.assertEqual((d['path_end_share'], d['fills_across_a_gap']), (0.25, 1))
        self.assertEqual(d['fill_past_trigger']['n'], 2)
        self.assertEqual(d['fill_past_trigger']['worst'], -0.1)


class GridTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        g = T.load_grid({'families': [{'family': 'live_ladder'}]})
        self.assertEqual((g['lag'], g['blocks'], g['alpha'], g['size_lamports']), (1, 5, 0.1, 200_000_000))
        for bad in ({}, {'families': []}, {'families': FAMILIES, 'typo': 1}, {'families': FAMILIES, 'lag': 2}, {'families': FAMILIES, 'blocks': 2},
                    {'families': FAMILIES, 'alpha': 1}, {'families': FAMILIES, 'bootstrap': 5}, {'families': FAMILIES, 'path_hours': 0},
                    {'families': FAMILIES, 'entry': {'size_sol': '0.0000000001'}}, {'families': FAMILIES, 'entry': {'x': 1}},
                    {'families': FAMILIES, 'min_oos_trades': 0}, {'families': FAMILIES, 'calibration_lag': 5}):
            with self.assertRaises(T.TuneError, msg=bad):
                T.load_grid(bad)
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'g.json'
            f.write_text('{')
            with self.assertRaises(T.TuneError):
                T.load_grid(f)
            with self.assertRaises(T.TuneError):
                T.load_grid(Path(tmp) / 'missing.json')


class Spy(list):
    """A list of trades that records which indices were read."""
    def __init__(self, items, name, log):
        super().__init__(items)
        self.name, self.log = name, log

    def __getitem__(self, i):
        self.log[self.name].append(i)
        return super().__getitem__(i)


class Analysis(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))

    def run_analysis(self, maker, n=160, **kw):
        self.counter = getattr(self, 'counter', 0) + 1
        paths = dataset(self.dir / ('p%d.sqlite' % self.counter), n, maker)
        return T.analyse(None, paths, grid(**kw), now=1.0)

    def test_planted_edge_gives_a_proposal_with_the_right_policy(self):
        r = self.run_analysis(planted)
        wf = r['walk_forward']
        self.assertTrue(wf['selections'], r['no_proposal_reason'])
        self.assertTrue(all(s.startswith('full_tp') for s in wf['selections']), wf['selections'])
        self.assertGreater(wf['pooled']['ci_lo'], 0)
        self.assertIsNotNone(r['proposal'], r['no_proposal_reason'])
        self.assertEqual(r['proposal']['family'], 'full_tp')
        self.assertIn('NEVER AUTOMATIC', r['proposal']['apply'])
        self.assertGreaterEqual(wf['pooled']['n'], 20)

    def test_pure_noise_gets_no_proposal_and_no_confirmed_policy(self):
        r = self.run_analysis(noise)
        self.assertIsNone(r['proposal'])
        self.assertTrue(r['no_proposal_reason'])
        self.assertEqual(r['walk_forward']['selections'], [])
        self.assertTrue(all(f['reason'] in ('NO_POLICY_SURVIVES_BH', 'TRAIN_TOO_SMALL') for f in r['walk_forward']['folds']))

    def test_too_little_data_is_reported_not_guessed(self):
        r = self.run_analysis(planted, n=6)
        self.assertIsNone(r['proposal'])
        self.assertIn('INSUFFICIENT_DATA', r['no_proposal_reason'])

    def test_a_planted_edge_that_is_too_small_a_sample_stays_below_the_minimum(self):
        r = self.run_analysis(planted, min_oos_trades=10_000)
        self.assertIsNone(r['proposal'])
        self.assertIn('OOS_TRADES_BELOW_MINIMUM', r['no_proposal_reason'])

    def test_descriptive_table_is_in_sample_complete_and_labelled(self):
        r = self.run_analysis(planted)
        self.assertEqual(len(r['policies']), 1 + 2 + 2 + 1)
        row = next(x for x in r['policies'] if x['policy'].startswith('full_tp(stop=0.3,x=1.5'))
        for key in ('n', 'ev', 'ci_lo', 'ci_hi', 'p5', 'p_le_minus_50', 'ev_first_half', 'ev_second_half', 'q_value', 'fill_past_trigger', 'exit_reasons'):
            self.assertIn(key, row)
        self.assertTrue(any('in-sample' in n for n in r['notes']))
        self.assertIn('IN-SAMPLE', T.render_html(r))

    def walk(self, results_wrapper=None, mutate=None, blocks=4):
        paths = dataset(self.dir / ('w%d.sqlite' % blocks), 160, planted)
        cfg = T.load_grid(grid(blocks=4))
        cfg['blocks'] = blocks
        costs = R.costs_from(None, R.load_fee_tiers())
        prepared = [x for x in (R.prepare(p, costs, cfg['size_lamports']) for p in sorted(R.load_paths(paths).values(), key=lambda p: p.start_ts)) if x]
        results = T.run_policies(prepared, cfg['policies'], BASE, costs, 1)
        if mutate:
            mutate(results, prepared)
        log = defaultdict(list)
        wrapped = {k: Spy(v, k, log) for k, v in results.items()}
        wf = T.walk_forward(prepared, wrapped, cfg['policies'], cfg, random.Random(1), 3600)
        return wf, prepared, results, log

    def test_the_test_block_is_read_once_and_only_for_the_selected_policy(self):
        # two blocks = exactly one fold, so every read of block 1 happens in that fold (later folds legitimately TRAIN on earlier test blocks)
        wf, prepared, _, log = self.walk(blocks=2)
        order = sorted(range(len(prepared)), key=lambda i: (prepared[i].path.start_ts, prepared[i].path.mint))
        blocks = T.time_blocks(None, order, 2)
        self.assertEqual(len(wf['folds']), 1)
        self.assertIsNotNone(wf['folds'][0]['selected'])
        for fold in wf['folds']:
            test = set(blocks[fold['fold']])
            for name, reads in log.items():
                touched = [i for i in reads if i in test]
                if name == fold['selected']:
                    self.assertEqual(sorted(touched), sorted(test), 'the selected policy reads each test path exactly once')
                else:
                    self.assertEqual(touched, [], '%s must not be read on the test block of fold %d' % (name, fold['fold']))

    def test_selection_is_blind_to_the_test_block(self):
        base, prepared, _, _ = self.walk()

        def sabotage(results, prep):                 # make every NON-selected policy look wonderful on the whole data
            for name, trades in results.items():
                if not name.startswith('full_tp'):
                    for t in trades:
                        t.ret = 50.0
        # train sets include earlier blocks of all data, so sabotage everything; selection must follow the TRAIN returns it sees.
        wf, _, _, _ = self.walk(mutate=sabotage)
        self.assertNotEqual(wf['selections'], base['selections'])          # train data changed -> a different pick (the selection is data-driven)
        # ...but changing ONLY test-block returns of the other policies cannot change what was selected:
        paths = dataset(self.dir / 'w2.sqlite', 160, planted)
        cfg = T.load_grid(grid(blocks=4))
        costs = R.costs_from(None, R.load_fee_tiers())
        prepared = [x for x in (R.prepare(p, costs, cfg['size_lamports']) for p in sorted(R.load_paths(paths).values(), key=lambda p: p.start_ts)) if x]
        results = T.run_policies(prepared, cfg['policies'], BASE, costs, 1)
        order = sorted(range(len(prepared)), key=lambda i: (prepared[i].path.start_ts, prepared[i].path.mint))
        blocks = T.time_blocks(None, order, 4)
        first = T.walk_forward(prepared, results, cfg['policies'], cfg, random.Random(1), 3600)
        for name, trades in results.items():
            if not name.startswith('full_tp'):
                for i in blocks[3]:
                    trades[i].ret = 99.0                                     # the LAST test block of the others: pure peeking bait
        second = T.walk_forward(prepared, results, cfg['policies'], cfg, random.Random(1), 3600)
        self.assertEqual([f['selected'] for f in first['folds']], [f['selected'] for f in second['folds']])
        self.assertEqual(first['pooled'], second['pooled'])

    def test_train_sets_are_purged_by_the_embargo(self):
        wf, prepared, _, _ = self.walk()
        self.assertTrue(any(f['purged_paths'] > 0 for f in wf['folds']))
        order = sorted(range(len(prepared)), key=lambda i: (prepared[i].path.start_ts, prepared[i].path.mint))
        blocks = T.time_blocks(None, order, 4)
        for fold in wf['folds']:
            j = fold['fold']
            test_start = min(prepared[i].path.start_ts for i in blocks[j])
            expected_train = [i for b in blocks[:j] for i in b if prepared[i].path.start_ts + 3600 <= test_start]
            self.assertEqual(fold['train_paths'], len(expected_train))
            self.assertEqual(fold['purged_paths'], sum(len(b) for b in blocks[:j]) - len(expected_train))
            self.assertTrue(all(prepared[i].path.start_ts + 3600 <= test_start for i in expected_train))

    def test_bh_is_applied_over_the_whole_grid_in_every_fold(self):
        wf, _, _, _ = self.walk()
        for fold in wf['folds']:
            if fold['selected']:
                self.assertEqual(fold['n_policies'], 6)
                self.assertLessEqual(fold['train_q'], 0.1)
                self.assertGreater(fold['train_ev'], 0)

    def test_blocks_are_equal_count_contiguous_and_cover_everything(self):
        blocks = T.time_blocks(None, list(range(10)), 3)
        self.assertEqual(sum(blocks, []), list(range(10)))
        self.assertEqual([len(b) for b in blocks], [3, 3, 4])


class Outputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.paths = dataset(self.dir / 'paths.sqlite', 160, planted)
        self.lean = self.dir / 'lean.sqlite'
        connection = sqlite3.connect(self.lean)
        connection.execute('CREATE TABLE fills(id INTEGER PRIMARY KEY, mint TEXT, side TEXT)')
        connection.execute('CREATE TABLE position_state(id INTEGER PRIMARY KEY, mint TEXT, event TEXT, state TEXT)')
        connection.commit()
        connection.close()
        (self.dir / 'out').mkdir()
        self.grid = self.dir / 'grid.json'
        self.grid.write_text(json.dumps(grid()))

    def cli(self, *extra, out='out'):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = T.main(['--lean-db', str(self.lean), '--paths-db', str(self.paths), '--grid', str(self.grid), '--out-dir', str(self.dir / out),
                           '--now', '5', *extra])
        return code, json.loads(buffer.getvalue())

    def digest(self, *files):
        return [(hashlib.sha256(Path(f).read_bytes()).hexdigest(), os.stat(f).st_mtime_ns) for f in files]

    def test_cli_writes_report_json_and_proposal_exclusively_0600_and_leaves_the_databases_alone(self):
        before = self.digest(self.paths, self.lean)
        code, out = self.cli()
        self.assertEqual((code, out['status'], out['proposal']), (0, 'OK', True))
        self.assertEqual(self.digest(self.paths, self.lean), before)
        names = sorted(os.listdir(self.dir / 'out'))
        self.assertEqual(names, ['proposal.json', 'tune-report.html', 'tune-report.json'])
        for n in names:
            self.assertEqual(os.stat(self.dir / 'out' / n).st_mode & 0o777, 0o600)
        proposal = json.loads((self.dir / 'out' / 'proposal.json').read_text())
        self.assertEqual(proposal['kind'], 'exit_policy_proposal_v1')
        report = json.loads((self.dir / 'out' / 'tune-report.json').read_text())
        self.assertEqual(report['kind'], 'lean_tune_v1')
        self.assertIn('<!doctype html>', (self.dir / 'out' / 'tune-report.html').read_text())

    def test_no_proposal_writes_the_marker_file_and_never_both(self):
        self.paths = dataset(self.dir / 'noise.sqlite', 160, noise)
        code, out = self.cli()
        self.assertEqual((code, out['proposal']), (0, False))
        names = sorted(os.listdir(self.dir / 'out'))
        self.assertEqual(names, ['NO_PROPOSAL', 'tune-report.html', 'tune-report.json'])
        self.assertTrue((self.dir / 'out' / 'NO_PROPOSAL').read_text().startswith('NO_PROPOSAL:'))

    def test_nothing_is_overwritten_and_bad_inputs_exit_2(self):
        self.assertEqual(self.cli()[0], 0)
        before = self.digest(*(self.dir / 'out' / n for n in os.listdir(self.dir / 'out')))
        code, out = self.cli()
        self.assertEqual((code, out['status']), (2, 'ERROR'))
        self.assertEqual(out['code'], 'TuneError')                                         # the explicit guard, not just O_EXCL
        self.assertIn('nothing is overwritten', out['detail'])
        self.assertEqual(self.digest(*(self.dir / 'out' / n for n in os.listdir(self.dir / 'out'))), before)
        self.assertEqual(self.cli(out='missing')[0], 2)
        (self.dir / 'link').symlink_to(self.dir / 'out')
        self.assertEqual(self.cli(out='link')[0], 2)
        self.grid.write_text('{')
        self.assertEqual(self.cli(out='out')[0], 2)

    def test_symlinked_or_missing_databases_are_refused(self):
        link = self.dir / 'paths-link.sqlite'
        link.symlink_to(self.paths)
        self.paths = link
        self.assertEqual(self.cli()[0], 2)
        self.paths = self.dir / 'nope.sqlite'
        self.assertEqual(self.cli()[0], 2)

    def test_hostile_strings_are_escaped_in_the_report(self):
        report = T.analyse(None, self.paths, grid(), now=1.0)
        report['calibration'] = {'compared': 1, 'matched': 0, 'mismatches': [{'mint': '<script>alert(1)</script>', 'live': ['<b>'], 'replay': ['"x"']}],
                                 'unmatched_paths': []}
        page = T.render_html(report)
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertIn('&lt;script&gt;', page)


class Calibration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))

    def live_db(self, rows, buys):
        db = self.dir / 'lean.sqlite'
        connection = sqlite3.connect(db)
        connection.execute('CREATE TABLE fills(id INTEGER PRIMARY KEY, mint TEXT, side TEXT)')
        connection.execute('CREATE TABLE position_state(id INTEGER PRIMARY KEY, mint TEXT, event TEXT, state TEXT)')
        for m in buys:
            connection.execute('INSERT INTO fills(mint, side) VALUES(?,?)', (m, 'buy'))
        for m, event, state in rows:
            connection.execute('INSERT INTO position_state(mint, event, state) VALUES(?,?,?)', (m, event, json.dumps(state)))
        connection.commit()
        connection.close()
        return db

    def test_live_reasons_only_for_closed_positions_that_were_bought(self):
        db = self.live_db([('A', 'open', {}), ('A', 'rung', {'last_reason': 'TAKE_PROFIT'}), ('A', 'closed', {'reason': 'STOP'}),
                           ('B', 'open', {}), ('B', 'rung', {'last_reason': 'TAKE_PROFIT'}), ('C', 'closed', {'reason': 'STOP'})], buys=['A', 'B'])
        self.assertEqual(T.live_reasons(db), {'A': ['TAKE_PROFIT', 'STOP']})         # B still open; C has no buy

    def test_matches_are_counted_and_mismatches_are_reported_not_hidden(self):
        paths = make_db(self.dir / 'paths.sqlite', [dict(mint='STP', ratios=[1, 1, 0.9, 0.7, 0.6, 0.6]), dict(mint='FLT', ratios=[1, 1, 1, 1, 1, 1]),
                                                      dict(mint='NOP', ratios=[1])])
        live = self.live_db([('STP', 'closed', {'reason': 'STOP'}), ('FLT', 'closed', {'reason': 'STOP'}), ('NOP', 'closed', {'reason': 'STOP'}),
                             ('GONE', 'closed', {'reason': 'STOP'})], buys=['STP', 'FLT', 'NOP', 'GONE'])
        costs = R.costs_from(FLAT0, R.load_fee_tiers())
        prepared = [x for x in (R.prepare(p, costs, SIZE) for p in R.load_paths(paths).values()) if x]
        c = T.calibrate(live, prepared, BASE, costs, 0)
        self.assertEqual((c['compared'], c['matched']), (2, 1))
        self.assertEqual([m['mint'] for m in c['mismatches']], ['FLT'])
        self.assertEqual(c['mismatches'][0]['replay'], ['PATH_END'])
        self.assertEqual(sorted(r['mint'] for r in c['unmatched_paths']), ['GONE', 'NOP'])


if __name__ == '__main__':
    unittest.main()
