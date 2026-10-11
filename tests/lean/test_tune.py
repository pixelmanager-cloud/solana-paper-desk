"""SYNTHETIC_TEST_ONLY: walk-forward tuning (lean/tune.py). Fixtures only, no network.

The regime-shift fixture: older paths (type Y) steadily bleed, so a TIGHT stop wins in-sample; the newest paths (type X)
dip and then rally, so the base (wide) stop wins out-of-sample. Ranking must follow the out-of-sample result only."""
import hashlib
import io
import contextlib
import json
import os
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from lean import replay as R, strategy as S, tune as T
from lean.store import Store

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / 'config' / 'lean' / 'strategy-default.json'
DAY0 = 19675 * 86400
T0 = DAY0 + 3600
BASE_PRICE = D('0.000001')
FEATS = {'market_cap_usd': '100000', 'liquidity_usd': '20000', 'reserve_sol': '100'}
NOW = DAY0 + 86400 * 3 + 12 * 3600  # 2026-... explicit: no hidden clock in the tests
X_DIP_RALLY = [1, 1, 0.88, 0.88, 1.2, 1.8, 2.6, 3.4, 3.3, 2.2, 1.5]
Y_BLEED = [1, 1, 0.97, 0.93, 0.88, 0.7, 0.6]
# type W: pops +30% (never reaching the 1.4x first rung), then fades. A lower first rung (1.2x) banks part of it and wins.
W_POP_FADE = [1, 1, 1.1, 1.3, 1.3, 1.25, 1.1, 0.9, 0.7, 0.6]
PROPOSE_GRID = {'tp_ladder.0.trigger': ['1.2']}
# type D: pumps, dumps 20%, then rallies hard. Entering on the pullback buys the dump; entering at the first mark gets stopped out by it.
D_DUMP_RALLY = [1, 1, 0.8, 0.8, 1.2, 1.7, 2.2, 2.8, 2.7, 1.2]
TIMING_GRID = {'replay.entry_timing': [{'kind': 'pullback', 'pullback_pct': '0.15'}, {'kind': 'momentum', 'momentum_minutes': 3}]}


def cfg(**over):
    raw = json.loads(DEFAULT.read_text())
    raw.update(over)
    return S.StrategyConfig.from_dict(raw)


# the daily stop / streak pause must not interfere: this suite is about stop width, not portfolio brakes
BASE = cfg(daily_loss_stop_fraction='0.9', daily_liquidate_fraction='0.95', loss_streak_pause_at=100, stop_fraction='0.18')


def make_db(directory, kinds, name='lean.sqlite', spacing=3600):
    """kinds: list of ratio lists in time order -> a real lean Store with path observations."""
    store = Store(Path(directory) / name, initial_cash_sol=5, code_version='c', strategy_version='lean-1', clock=lambda: float(T0))
    for i, ratios in enumerate(kinds):
        mint, start = 'P%03d' % i, T0 + spacing * i
        store.add_observation('path_start', json.dumps({'features': dict(FEATS), 'reason': None}).encode(), mint=mint, ts=start,
                              meta={'entered': False, 'reason': None})
        for k, ratio in enumerate(ratios):
            store.add_observation('path_mark', b'{}', mint=mint, ts=start + 15 * k,
                                  meta={'price_sol': str(BASE_PRICE * D(str(ratio))), 'liquidity_sol': '200', 'status': 'OK'})  # two-sided, as L07 writes it
    store.close()
    return Path(directory) / name


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))

    def sha(self, path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class GridTests(unittest.TestCase):
    def test_product_base_first_duplicates_dropped(self):
        cands, invalid = T.expand_grid({'stop_fraction': ['0.1', '0.18', '0.25'], 'trailing_fraction': ['0.2', '0.3']}, BASE)
        self.assertEqual(cands[0]['label'], 'base')
        self.assertEqual(len(cands), 1 + 6 - 1)  # 0.18/0.3 equals the base config (also written '0.30' in the file) and is dropped
        self.assertEqual(invalid, [])
        self.assertEqual(len({c['cfg'].config_hash for c in cands}), len(cands))
        self.assertEqual(BASE.stop_fraction, D('0.18'))  # the base is never mutated

    def test_invalid_combinations_are_reported_not_dropped(self):
        cands, invalid = T.expand_grid({'stop_fraction': ['0.1', '1.5', 'abc']}, BASE)
        self.assertEqual([c['label'] for c in cands], ['base', 'stop_fraction=0.1'])
        self.assertEqual([i['label'] for i in invalid], ['stop_fraction=1.5', 'stop_fraction=abc'])
        self.assertTrue(all(i['error'] for i in invalid))

    def test_ladder_keys(self):
        cands, _ = T.expand_grid({'tp_ladder.0.trigger': ['1.3', '1.5']}, BASE)
        self.assertEqual([c['cfg'].tp_ladder[0].trigger for c in cands[1:]], [D('1.3'), D('1.5')])
        for bad in ('tp_ladder.9.trigger', 'tp_ladder.0.nope', 'tp_ladder.x.trigger'):
            with self.assertRaises(T.TuneError, msg=bad):
                T.expand_grid({bad: ['1']}, BASE)

    def test_bad_grids_refused(self):
        for grid in ({'nope': ['1']}, {'strategy_version': ['x']}, {'stop_fraction': []}, {'stop_fraction': '0.1'}, [], None):
            with self.assertRaises(T.TuneError, msg=grid):
                T.expand_grid(grid, BASE)
        with self.assertRaises(T.TuneError):
            T.expand_grid({'stop_fraction': [str(D('0.01') * i) for i in range(1, 50)], 'trailing_fraction': [str(D('0.01') * i) for i in range(1, 50)]}, BASE, max_configs=100)

    def test_whole_ladder_replacement_is_validated_not_trusted(self):
        good = [{'trigger': '1.3', 'fraction': '0.3', 'stop_ratio': '1'}, {'trigger': '2', 'fraction': '0.3', 'stop_ratio': '1.3'},
                {'trigger': '3', 'fraction': '0.2', 'stop_ratio': '2'}]
        cands, invalid = T.expand_grid({'tp_ladder': [good, 'x']}, BASE)
        self.assertEqual((len(cands), len(invalid)), (2, 1))

    def test_config_round_trip(self):
        self.assertEqual(S.StrategyConfig.from_dict(T.config_to_dict(BASE)).config_hash, S.StrategyConfig.from_dict(T.config_to_dict(BASE)).config_hash)
        self.assertEqual(S.StrategyConfig.from_dict(T.config_to_dict(BASE)), BASE)


class SplitTests(unittest.TestCase):
    def paths(self, n, start=T0, step=100):
        return [R.Path('M%d' % i, start + step * i, dict(FEATS), ()) for i in range(n)]

    def test_older_seventy_newest_thirty_regardless_of_input_order(self):
        ps = self.paths(10)
        older, newer, boundary = T.split_paths(list(reversed(ps)), 0.7)
        self.assertEqual(([p.mint for p in older], [p.mint for p in newer]), (['M%d' % i for i in range(7)], ['M7', 'M8', 'M9']))
        self.assertEqual(boundary, ps[7].start_ts)
        self.assertLess(max(p.start_ts for p in older), min(p.start_ts for p in newer))

    def test_embargo_boundary_is_inclusive(self):
        _, newer, _ = T.split_paths(self.paths(10), 0.7, embargo_s=100)  # M8 starts exactly boundary+100 and is kept
        self.assertEqual([p.mint for p in newer], ['M8', 'M9'])

    def test_embargo_drops_paths_near_the_boundary(self):
        _, newer, boundary = T.split_paths(self.paths(10), 0.7, embargo_s=150)
        self.assertEqual([p.mint for p in newer], ['M9'])
        with self.assertRaises(T.TuneError):
            T.split_paths(self.paths(10), 0.7, embargo_s=10_000)

    def test_bounds(self):
        for split in (0, 1, -0.1, 1.5, True, 'x', float('nan')):
            with self.assertRaises(T.TuneError, msg=split):
                T.split_paths(self.paths(10), split)
        for n in (0, 1):
            with self.assertRaises(T.TuneError):
                T.split_paths(self.paths(n), 0.7)
        T.split_paths(self.paths(2), 0.7)


class TuneTests(Tmp):
    def regime_db(self, name='lean.sqlite'):
        return make_db(self.dir, [Y_BLEED] * 14 + [X_DIP_RALLY] * 6, name)

    def run_tune(self, db, **kw):
        kw.setdefault('base', BASE)
        kw.setdefault('min_oos', 5)
        kw.setdefault('now', NOW)
        return T.tune(db, kw.pop('grid', {'stop_fraction': ['0.08', '0.12']}), 0.7, **kw)

    def test_ranking_uses_out_of_sample_only(self):
        r = self.run_tune(self.regime_db())
        by = {c['label']: c for c in r['configs']}
        tight = by['stop_fraction=0.08']
        self.assertGreater(D(tight['in_sample']['total_pnl_sol']), D(by['base']['in_sample']['total_pnl_sol']))  # wins in-sample...
        self.assertEqual([c['label'] for c in r['configs']][0], 'base')  # ...but loses out-of-sample, and the rank follows OOS
        self.assertGreater(D(by['base']['out_of_sample']['total_pnl_sol']), D(tight['out_of_sample']['total_pnl_sol']))
        self.assertEqual([c['rank'] for c in r['configs']], [1, 2, 3])
        self.assertEqual((r['split']['in_sample_paths'], r['split']['out_of_sample_paths']), (14, 6))
        self.assertEqual(by['base']['in_sample']['n_trades'], 14)
        self.assertEqual(by['base']['out_of_sample']['n_trades'], 6)

    def test_every_config_reports_all_required_statistics_in_and_out_of_sample(self):
        r = self.run_tune(self.regime_db())
        for entry in r['configs']:
            for side in ('in_sample', 'out_of_sample'):
                m = entry[side]
                for key in ('n_trades', 'win_rate', 'mean_pnl_sol', 'median_pnl_sol', 'max_drawdown_sol', 'profit_factor'):
                    self.assertIn(key, m)
        json.dumps(r, allow_nan=False)  # JSON-safe: no NaN / Infinity anywhere

    def test_insufficient_below_fifty_oos_trades_and_ranked_after_sufficient(self):
        r = self.run_tune(self.regime_db(), min_oos=T.MIN_OOS)
        self.assertTrue(all('INSUFFICIENT' in c['flags'] for c in r['configs']))
        self.assertEqual(r['proposal_status'], 'NO_SUFFICIENT_ALTERNATIVE')
        self.assertIsNone(r['proposal'])
        self.assertTrue(any('fewer than the minimum' in w for w in r['warnings']))
        mixed = self.run_tune(self.regime_db('b.sqlite'), min_oos=6)  # base has 6 OOS trades: sufficient; 'tight' exits early but also trades 6
        self.assertEqual([('INSUFFICIENT' in c['flags']) for c in mixed['configs']], [False, False, False])

    def test_sufficient_always_outranks_insufficient(self):
        entries = [{'flags': ['INSUFFICIENT'], 'out_of_sample': {'total_pnl_sol': '9', 'n_trades': 1}, 'label': 'a'},
                   {'flags': [], 'out_of_sample': {'total_pnl_sol': '1', 'n_trades': 60}, 'label': 'b'}]
        entries.sort(key=lambda e: ('INSUFFICIENT' in e['flags'], -T._total(e), -e['out_of_sample']['n_trades'], e['label']))
        self.assertEqual([e['label'] for e in entries], ['b', 'a'])

    def test_selection_bias_and_assumption_warnings_present(self):
        r = self.run_tune(self.regime_db())
        text = ' '.join(r['warnings'])
        self.assertIn('selection bias', text)
        self.assertIn('SIMULATION', text)
        self.assertEqual(r['replay_assumptions']['pool_fee_bps'], 30)

    def test_proposal_written_only_when_alternative_beats_base_out_of_sample(self):
        db = make_db(self.dir, [W_POP_FADE] * 20)
        props = self.dir / 'proposals'
        before_default = self.sha(DEFAULT)
        r = self.run_tune(db, grid=PROPOSE_GRID, proposals_dir=props)
        self.assertEqual(r['proposal_status'], 'WRITTEN')
        doc = json.loads(Path(r['proposal']).read_text())
        self.assertEqual(Path(r['proposal']).parent, props)
        self.assertEqual(doc['status'], 'PROPOSAL_NOT_APPLIED')
        self.assertEqual(doc['current']['strategy_version'], 'lean-1')
        self.assertNotEqual(doc['proposed_strategy_version'], 'lean-1')
        self.assertEqual(set(doc['diff']), {'tp_ladder.0.trigger'})
        self.assertEqual(doc['diff']['tp_ladder.0.trigger'], {'from': '1.4', 'to': '1.2'})
        self.assertEqual(S.StrategyConfig.from_dict(doc['proposed_config']).strategy_version, doc['proposed_strategy_version'])
        self.assertGreater(D(doc['evidence']['out_of_sample']['total_pnl_sol']), D(doc['evidence']['base_out_of_sample']['total_pnl_sol']))
        self.assertEqual(doc['evidence']['configs_evaluated'], 2)
        self.assertIn('selection bias', ' '.join(doc['warnings']))
        self.assertEqual(self.sha(DEFAULT), before_default)  # a proposal never edits the live config
        self.assertEqual(sorted(p.name for p in props.iterdir()), [Path(r['proposal']).name])

    def test_proposal_file_is_never_overwritten(self):
        db = make_db(self.dir, [W_POP_FADE] * 20)
        props = self.dir / 'proposals'
        self.run_tune(db, grid=PROPOSE_GRID, proposals_dir=props)
        with self.assertRaises(FileExistsError):
            self.run_tune(db, grid=PROPOSE_GRID, proposals_dir=props)

    def test_no_proposal_when_base_wins_out_of_sample(self):
        props = self.dir / 'proposals'
        r = self.run_tune(self.regime_db(), proposals_dir=props)
        self.assertEqual((r['proposal_status'], r['proposal']), ('NO_OUT_OF_SAMPLE_IMPROVEMENT', None))
        self.assertFalse(props.exists())

    def test_no_directory_means_no_proposal_file(self):
        db = make_db(self.dir, [W_POP_FADE] * 20)
        r = self.run_tune(db, grid=PROPOSE_GRID)
        self.assertEqual((r['proposal_status'], r['proposal']), ('WOULD_PROPOSE_NO_DIRECTORY_GIVEN', None))

    def test_base_with_too_few_oos_trades_blocks_proposal(self):
        db = make_db(self.dir, [W_POP_FADE] * 20)
        r = self.run_tune(db, grid=PROPOSE_GRID, min_oos=7)
        self.assertEqual(r['proposal_status'], 'NO_SUFFICIENT_ALTERNATIVE')
        self.assertIsNone(r['proposal'])

    def test_alternative_must_also_be_profitable_not_merely_less_bad(self):
        db = make_db(self.dir, [X_DIP_RALLY] * 14 + [Y_BLEED] * 6)  # the tight stop loses less OOS than base, but still loses
        r = self.run_tune(db, proposals_dir=self.dir / 'proposals')
        by = {c['label']: c for c in r['configs']}
        self.assertGreater(D(by['stop_fraction=0.08']['out_of_sample']['total_pnl_sol']), D(by['base']['out_of_sample']['total_pnl_sol']))
        self.assertLess(D(by['stop_fraction=0.08']['out_of_sample']['total_pnl_sol']), 0)
        self.assertEqual((r['proposal_status'], r['proposal']), ('NO_OUT_OF_SAMPLE_IMPROVEMENT', None))

    def test_zero_trade_config_cannot_outrank_a_sufficient_losing_one(self):
        db = make_db(self.dir, [Y_BLEED] * 20)
        r = self.run_tune(db, grid={'min_market_cap_usd': ['200000']})  # filters every candidate: 0 trades, total 0 > base's losses
        order = [(c['label'], c['flags']) for c in r['configs']]
        self.assertEqual(order, [('base', []), ('min_market_cap_usd=200000', ['INSUFFICIENT'])])
        self.assertLess(D(r['configs'][0]['out_of_sample']['total_pnl_sol']), D(r['configs'][1]['out_of_sample']['total_pnl_sol']))

    def test_an_alternative_equal_to_base_is_not_an_improvement(self):
        db = make_db(self.dir, [X_DIP_RALLY] * 20)
        r = self.run_tune(db, grid={'stop_fraction': ['0.30']}, proposals_dir=self.dir / 'proposals')  # same trades, same pnl
        by = {c['label']: c for c in r['configs']}
        self.assertEqual(by['stop_fraction=0.30']['out_of_sample']['total_pnl_sol'], by['base']['out_of_sample']['total_pnl_sol'])
        self.assertGreater(D(by['base']['out_of_sample']['mean_pnl_sol']), 0)
        self.assertEqual((r['proposal_status'], r['proposal']), ('NO_OUT_OF_SAMPLE_IMPROVEMENT', None))

    def test_insufficient_base_cannot_be_beaten_so_no_proposal(self):
        db = make_db(self.dir, [X_DIP_RALLY] * 20)
        weak_base = cfg(daily_loss_stop_fraction='0.9', daily_liquidate_fraction='0.95', loss_streak_pause_at=100, min_market_cap_usd='200000')
        props = self.dir / 'proposals'
        r = self.run_tune(db, base=weak_base, grid={'min_market_cap_usd': ['50000']}, proposals_dir=props)
        by = {c['label']: c for c in r['configs']}
        self.assertEqual(by['base']['flags'], ['INSUFFICIENT'])
        self.assertEqual(by['min_market_cap_usd=50000']['flags'], [])
        self.assertEqual((r['proposal_status'], r['proposal']), ('BASE_INSUFFICIENT', None))
        self.assertFalse(props.exists())

    def test_database_is_not_modified(self):
        db = self.regime_db()
        before = self.sha(db)
        self.run_tune(db)
        self.assertEqual(self.sha(db), before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ['lean.sqlite'])  # no -wal/-shm/journal left behind

    def test_too_few_paths(self):
        db = make_db(self.dir, [Y_BLEED])
        with self.assertRaises(T.TuneError):
            self.run_tune(db)

    def test_deterministic(self):
        db = self.regime_db()
        a, b = self.run_tune(db), self.run_tune(db)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_unusable_rows_are_surfaced(self):
        store = Store(self.regime_db(), code_version='c', strategy_version='lean-1')
        store.add_observation('path_mark', b'{}', mint='P000', ts=T0 + 7, meta={'price_sol': 'NaN', 'liquidity_sol': '100'})
        store.close()
        r = self.run_tune(store.path)
        self.assertEqual(r['load_skipped'], {'MARK_BAD_PRICE': 1})
        self.assertTrue(any('unusable' in w for w in r['warnings']))


class TimingGridTests(Tmp):
    def test_replay_keys_expand_alongside_strategy_keys(self):
        cands, invalid = T.expand_grid({'replay.entry_timing': ['first_mark', {'kind': 'pullback', 'pullback_pct': '0.1'}, 'bogus', {'kind': 'momentum', 'momentum_minutes': 99}],
                                        'stop_fraction': ['0.18', '0.25']}, BASE)
        labels = [c['label'] for c in cands]
        self.assertEqual(labels[0], 'base')
        self.assertIn('entry_timing=pullback(0.1) stop_fraction=0.25', labels)  # labels list keys in sorted order
        self.assertEqual(len(cands), 1 + 3)  # first_mark+0.18 is the base; pullback x2 stops, first_mark+0.25
        self.assertEqual(len(invalid), 4)  # bogus x2 and momentum 99 x2
        self.assertTrue(all('replay.entry_timing' in i['error'] for i in invalid))
        self.assertEqual(sorted(c['rcfg'].entry_timing.label for c in cands[1:]), ['first_mark', 'pullback(0.1)', 'pullback(0.1)'])

    def test_unknown_or_malformed_replay_keys(self):
        with self.assertRaises(T.TuneError):
            T.expand_grid({'replay.pool_fee_bps': [10]}, BASE)
        with self.assertRaises(T.TuneError):
            T.expand_grid({'replay.entry_timing': []}, BASE)
        cands, invalid = T.expand_grid({'replay.entry_delay_s': [-5, 30, 30.0]}, BASE)
        self.assertEqual(len(invalid), 1)
        self.assertEqual(len(cands), 2)  # 30 and 30.0 are the same setting

    def test_the_base_replay_config_is_kept_and_not_mutated(self):
        base_r = R.ReplayConfig(pool_fee_bps=25)
        cands, _ = T.expand_grid({'replay.entry_delay_s': [30]}, BASE, base_rcfg=base_r)
        self.assertEqual((cands[0]['rcfg'], cands[1]['rcfg'].pool_fee_bps, cands[1]['rcfg'].entry_delay_s), (base_r, 25, 30))
        self.assertEqual(base_r.entry_delay_s, 0)

    def test_variants_are_ranked_out_of_sample_and_the_better_timing_wins(self):
        db = make_db(self.dir, [D_DUMP_RALLY] * 20)
        r = T.tune(db, TIMING_GRID, 0.7, base=BASE, min_oos=5, now=NOW)
        top = r['configs'][0]
        self.assertEqual(top['label'], 'entry_timing=pullback(0.15)')
        self.assertEqual(top['replay']['entry_timing']['kind'], 'pullback')
        by = {c['label']: c for c in r['configs']}
        self.assertLess(D(by['base']['out_of_sample']['total_pnl_sol']), 0)
        self.assertGreater(D(top['out_of_sample']['total_pnl_sol']), 0)
        self.assertEqual(by['entry_timing=momentum(3m)']['out_of_sample']['n_trades'], 0)  # the dump path never builds 3 rising minutes
        self.assertIn('INSUFFICIENT', by['entry_timing=momentum(3m)']['flags'])
        self.assertEqual(r['configs'][-1]['label'], 'entry_timing=momentum(3m)')  # insufficient ranks last even though it never lost

    def test_timing_proposal_names_the_runner_change_it_needs(self):
        db = make_db(self.dir, [D_DUMP_RALLY] * 20)
        props = self.dir / 'proposals'
        r = T.tune(db, TIMING_GRID, 0.7, base=BASE, min_oos=5, now=NOW, proposals_dir=props)
        self.assertEqual(r['proposal_status'], 'WRITTEN')
        doc = json.loads(Path(r['proposal']).read_text())
        self.assertEqual(doc['diff'], {})  # no strategy parameter changes
        self.assertEqual(doc['requires_runner_change'], ['replay.entry_timing'])
        self.assertEqual(doc['replay_overrides'], {'replay.entry_timing': {'kind': 'pullback', 'pullback_pct': '0.15'}})
        self.assertEqual(doc['status'], 'PROPOSAL_NOT_APPLIED')

    def test_report_shows_the_timing_variant(self):
        db = make_db(self.dir, [D_DUMP_RALLY] * 20)
        html = T.render_html(T.tune(db, TIMING_GRID, 0.7, base=BASE, min_oos=5, now=NOW))
        self.assertIn('entry_timing=pullback(0.15)', html)
        self.assertIn('pullback_pct', html)  # the replay assumptions list the base timing config


class ReportTests(Tmp):
    def result(self, grid=None):
        db = make_db(self.dir, [Y_BLEED] * 14 + [X_DIP_RALLY] * 6)
        return T.tune(db, grid or {'stop_fraction': ['0.08']}, 0.7, base=BASE, min_oos=5, now=NOW)

    def test_html_is_escaped_and_complete(self):
        r = self.result({'stop_fraction': ['0.08', '<script>alert(1)</script>']})
        html = T.render_html(r)
        self.assertNotIn('<script>alert', html)
        self.assertIn('&lt;script&gt;', html)
        for needle in ('OUT-OF-SAMPLE', 'selection bias', 'PAPER ONLY', 'Never applied automatically', 'Invalid grid combinations', 'rank'):
            self.assertIn(needle, html)

    def test_untrusted_strings_in_every_header_are_escaped(self):
        db = make_db(self.dir, [Y_BLEED] * 14 + [X_DIP_RALLY] * 6)
        hostile = cfg(strategy_version='<i>v</i>', daily_loss_stop_fraction='0.9', daily_liquidate_fraction='0.95', loss_streak_pause_at=100)
        html = T.render_html(T.tune(db, {'stop_fraction': ['0.08']}, 0.7, base=hostile, min_oos=5, now=NOW))
        self.assertNotIn('<i>v</i>', html)
        self.assertIn('&lt;i&gt;v&lt;/i&gt;', html)

    def test_html_flags_insufficient(self):
        r = self.result()
        r['configs'][0]['flags'] = ['INSUFFICIENT']
        self.assertIn('INSUFFICIENT', T.render_html(r))

    def test_calibration_section_lists_mismatches_escaped(self):
        cal = {'status': 'MISMATCH', 'matched': 0, 'tokens': 1, 'rows': [{'mint': '<b>M</b>', 'live_reasons': ['STOP'], 'replay_reasons': ['TIME_STOP'], 'match': False, 'note': 'EXIT_REASONS_DIFFER'}]}
        html = T.render_html(self.result(), cal)
        self.assertIn('Calibration', html)
        self.assertIn('&lt;b&gt;M&lt;/b&gt;', html)
        self.assertNotIn('<b>M</b>', html)

    def test_write_report_is_exclusive_and_json_valid(self):
        r = self.result()
        out = self.dir / 'out'
        out.mkdir()
        html_path, json_path = T.write_report(out, r)
        self.assertEqual(json.loads(json_path.read_text())['base']['strategy_version'], 'lean-1')
        with self.assertRaises(FileExistsError):
            T.write_report(out, r)
        with self.assertRaises(T.TuneError):
            T.write_report(self.dir / 'missing', r)

    def test_cli_ok_and_error_paths(self):
        db = make_db(self.dir, [Y_BLEED] * 14 + [X_DIP_RALLY] * 6)
        grid = self.dir / 'grid.json'
        grid.write_text(json.dumps({'stop_fraction': ['0.08']}))
        out = self.dir / 'out'
        out.mkdir()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = T.main(['--db', str(db), '--grid', str(grid), '--out-dir', str(out), '--min-oos', '5', '--now', str(NOW), '--calibrate'])
        self.assertEqual(code, 0)
        summary = json.loads(buf.getvalue())
        self.assertEqual((summary['status'], summary['proposal'], summary['calibration']), ('OK', None, 'NO_LIVE_FILLS'))
        self.assertTrue(Path(summary['html']).is_file())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bad = T.main(['--db', str(db), '--grid', str(self.dir / 'nope.json'), '--out-dir', str(out), '--now', '1'])
        self.assertEqual(bad, 2)
        self.assertEqual(json.loads(buf.getvalue())['status'], 'ERROR')
        grid.write_text(json.dumps({'unknown_param': ['1']}))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(T.main(['--db', str(db), '--grid', str(grid), '--out-dir', str(out), '--now', '2']), 2)


if __name__ == '__main__':
    unittest.main()
