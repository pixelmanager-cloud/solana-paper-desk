"""SYNTHETIC_TEST_ONLY: walk-forward tuning (lean/tune.py). Fixtures only, no network.

Paths are real `lean.paths.PathStore` files holding raw vault amounts (a 1e8-token pool against 100 SOL, the quote vault scaled by
a ratio list sampled every 15 s). The windows are train (oldest) / select / confirm (newest). Configs are RANKED on the select
window only; the confirm window can only accept or reject the winner and never influences the choice."""
import hashlib
import io
import contextlib
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from lean import paths as L, replay as R, strategy as S, tune as T
from lean.store import Store

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / 'config' / 'lean' / 'strategy-default.json'
DAY0 = 19675 * 86400
T0 = DAY0 + 3600
BASE_RAW = 10 ** 14
QUOTE0 = 100 * 10 ** 9
FEATS = {'decimals': 6, 'supply_raw': str(10 ** 15), 'sol_usd': '150', 'token_program': 'Tokenkeg'}
NOW = DAY0 + 86400 * 3 + 12 * 3600  # explicit: no hidden clock in the tests
X_DIP_RALLY = [1, 1, 0.88, 0.88, 1.2, 1.8, 2.6, 3.4, 3.3, 2.2, 1.5]
Y_BLEED = [1, 1, 0.97, 0.93, 0.88, 0.7, 0.6]
# type W: pops +30% (never reaching the 1.4x first rung), then fades. A lower first rung (1.2x) banks part of it and wins.
W_POP_FADE = [1, 1, 1.1, 1.3, 1.3, 1.25, 1.1, 0.9, 0.7, 0.6]
# type L: pops 25% (banked at the 1.2x rung), then gaps down to 0.5x: the rung variant loses LESS than the base but still loses
L_SMALL_POP = [1, 1, 1.25, 1.25, 0.5, 0.5, 0.5]
# type R: grinds up through +30% and then rallies on. Banking part of it at the 1.2x rung sells early; the base rung (1.4x) sells later and higher.
R_STEP_RALLY = [1, 1, 1.1, 1.3, 1.3, 1.6, 2.2, 3.0, 3.4, 3.3, 2.2]
PROPOSE_GRID = {'tp_ladder.0.trigger': ['1.2']}
# type D: pumps, dumps 20%, then rallies hard. Entering on the pullback buys the dump; entering at the first mark gets stopped out by it.
D_DUMP_RALLY = [1, 1, 0.8, 0.8, 1.2, 1.7, 2.2, 2.8, 2.7, 1.2]
TIMING_GRID = {'replay.entry_timing': [{'kind': 'pullback', 'pullback_pct': '0.15'}, {'kind': 'momentum', 'momentum_minutes': 3}]}
MIN = dict(min_select=5, min_confirm=5)


def cfg(**over):
    raw = json.loads(DEFAULT.read_text())
    raw.update(over)
    return S.StrategyConfig.from_dict(raw)


# the daily stop / streak pause must not interfere: this suite is about stop width, not portfolio brakes
BASE = cfg(daily_loss_stop_fraction='0.9', daily_liquidate_fraction='0.95', loss_streak_pause_at=100, stop_fraction='0.18')


def start_row(mint, start):
    return {'mint': mint, 'candidate_id': None, 'start_ts': start, 'ends_ts': start + 21600, 'entered': 0, 'stage': 'screen', 'reason': '[]',
            'screen_reasons': '[]', 'holders_checked': 0, 'features': json.dumps(FEATS),
            'target': json.dumps({'mint': mint, 'pool': 'POOL', 'base_vault': 'BV', 'quote_vault': 'QV', 'decimals': 6, 'fee_raw': 0,
                                  'qty_raw': 1_000_000, 'cost_lamports': 100_000_000, 'basis': 'ref_size'}),
            'carried_from': None, 'code_version': 'c', 'strategy_version': 'lean-1'}


def make_db(directory, kinds, name='paths.sqlite', spacing=3600):
    """kinds: list of ratio lists in time order -> (the live store's lean.sqlite, a real recorder paths.sqlite)."""
    store = L.PathStore(Path(directory) / name)
    for i, ratios in enumerate(kinds):
        mint, start = 'P%03d' % i, T0 + spacing * i
        store.insert_path(start_row(mint, start))
        store.add(marks=[(mint, start + 15 * k, 1, BASE_RAW, int(QUOTE0 * D(str(r))), None, None, 'OK') for k, r in enumerate(ratios)])
    store.close()
    live = Path(directory) / 'lean.sqlite'
    if not live.exists():
        Store(live, initial_cash_sol=5, code_version='c', strategy_version='lean-1', clock=lambda: float(T0)).close()
    return live, Path(directory) / name


def regime(select, confirm, train=Y_BLEED, n=(20, 10, 10)):
    """Older -> newer: n[0] train paths, n[1] select paths, n[2] confirm paths."""
    return [train] * n[0] + [select] * n[1] + [confirm] * n[2]


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

    def test_three_windows_by_start_time_regardless_of_input_order(self):
        ps = self.paths(20)
        train, select, confirm, bounds = T.split_paths(list(reversed(ps)))
        self.assertEqual(([p.mint for p in train], [p.mint for p in select], [p.mint for p in confirm]),
                         (['M%d' % i for i in range(10)], ['M%d' % i for i in range(10, 15)], ['M%d' % i for i in range(15, 20)]))
        self.assertEqual(bounds, (ps[10].start_ts, ps[15].start_ts))
        self.assertLess(max(p.start_ts for p in train), min(p.start_ts for p in select))
        self.assertLess(max(p.start_ts for p in select), min(p.start_ts for p in confirm))

    def test_custom_fractions(self):
        train, select, confirm, _ = T.split_paths(self.paths(10), (0.6, 0.2, 0.2))
        self.assertEqual((len(train), len(select), len(confirm)), (6, 2, 2))

    def test_embargo_boundary_is_inclusive_and_applies_to_both_boundaries(self):
        _, select, confirm, _ = T.split_paths(self.paths(20), embargo_s=100)  # M11 starts exactly boundary+100 and is kept
        self.assertEqual(([p.mint for p in select][:2], [p.mint for p in confirm][:2]), (['M11', 'M12'], ['M16', 'M17']))

    def test_embargo_drops_paths_near_the_boundary(self):
        _, select, confirm, _ = T.split_paths(self.paths(20), embargo_s=250)
        self.assertEqual(([p.mint for p in select], [p.mint for p in confirm]), (['M13', 'M14'], ['M18', 'M19']))
        with self.assertRaises(T.TuneError):
            T.split_paths(self.paths(20), embargo_s=10_000)

    def test_bad_fractions_and_too_few_paths(self):
        for fr in ((1, 0, 0), (0.5, 0.5), (0.5, 0.3, 0.3), (0.5, 0.25, -0.25), (True, 0.5, 0.5), ('a', 'b', 'c'), (float('nan'), 0.5, 0.5), 0.7, None):
            with self.assertRaises(T.TuneError, msg=fr):
                T.split_paths(self.paths(10), fr)
        for n in (0, 1, 2):
            with self.assertRaises(T.TuneError):
                T.split_paths(self.paths(n))
        T.split_paths(self.paths(4))


class TuneTests(Tmp):
    def tune(self, kinds, **kw):
        grid = kw.pop('grid', PROPOSE_GRID)
        live, paths = make_db(self.dir, kinds, kw.pop('name', 'paths.sqlite'))
        for k, v in MIN.items():
            kw.setdefault(k, v)
        kw.setdefault('base', BASE)
        kw.setdefault('now', NOW)
        return T.tune(live, paths, grid, **kw)

    def tight_wins_select_but_not_confirm(self):
        """A lower first take-profit rung banks the pop-and-fade select window; the rally in the newest window punishes it."""
        return regime(W_POP_FADE, R_STEP_RALLY, train=W_POP_FADE)

    def test_ranking_uses_the_select_window_only_never_the_confirm_window(self):
        r = self.tune(self.tight_wins_select_but_not_confirm())
        by = {c['label']: c for c in r['configs']}
        tight = by['tp_ladder.0.trigger=1.2']
        self.assertGreater(D(tight['select']['total_pnl_sol']), D(by['base']['select']['total_pnl_sol']))      # the tight stop wins select
        self.assertGreater(D(by['base']['confirm']['total_pnl_sol']), D(tight['confirm']['total_pnl_sol']))    # and loses confirm
        self.assertEqual(r['configs'][0]['label'], 'tp_ladder.0.trigger=1.2')                                    # the rank follows select
        self.assertEqual(r['winner'], 'tp_ladder.0.trigger=1.2')
        self.assertEqual([c['rank'] for c in r['configs']], [1, 2])
        self.assertTrue(all(c['confirm_used_for_selection'] is False for c in r['configs']))
        self.assertEqual((r['split']['train_paths'], r['split']['select_paths'], r['split']['confirm_paths']), (20, 10, 10))
        self.assertEqual((by['base']['train']['n_trades'], by['base']['select']['n_trades'], by['base']['confirm']['n_trades']), (20, 10, 10))

    def test_a_winner_the_confirm_window_does_not_support_is_not_proposed(self):
        props = self.dir / 'proposals'
        r = self.tune(self.tight_wins_select_but_not_confirm(), proposals_dir=props)
        self.assertEqual((r['proposal_status'], r['proposal']), ('NOT_CONFIRMED', None))
        self.assertFalse(props.exists())

    def test_the_choice_does_not_change_when_only_the_confirm_window_changes(self):
        a = self.tune(regime(W_POP_FADE, R_STEP_RALLY, train=W_POP_FADE), name='a.sqlite')
        b = self.tune(regime(W_POP_FADE, W_POP_FADE, train=W_POP_FADE), name='b.sqlite')
        self.assertEqual([c['label'] for c in a['configs']], [c['label'] for c in b['configs']])
        self.assertEqual([c['select'] for c in a['configs']], [c['select'] for c in b['configs']])
        self.assertEqual(a['winner'], b['winner'])
        self.assertNotEqual([c['confirm'] for c in a['configs']], [c['confirm'] for c in b['configs']])

    def test_every_config_reports_all_required_statistics_in_every_window(self):
        r = self.tune(self.tight_wins_select_but_not_confirm())
        for entry in r['configs']:
            for side in ('train', 'select', 'confirm'):
                for key in ('n_trades', 'win_rate', 'mean_pnl_sol', 'median_pnl_sol', 'max_drawdown_sol', 'profit_factor'):
                    self.assertIn(key, entry[side])
        json.dumps(r, allow_nan=False)  # JSON-safe: no NaN / Infinity anywhere

    def test_insufficient_below_the_minimum_select_trades_and_ranked_after_sufficient(self):
        r = self.tune(self.tight_wins_select_but_not_confirm(), min_select=T.MIN_SELECT, min_confirm=T.MIN_CONFIRM)
        self.assertEqual((T.MIN_SELECT, T.MIN_CONFIRM), (50, 30))
        self.assertTrue(all('INSUFFICIENT' in c['flags'] for c in r['configs']))
        self.assertEqual((r['proposal_status'], r['proposal']), ('NO_SUFFICIENT_ALTERNATIVE', None))
        self.assertTrue(any('fewer than the minimum' in w for w in r['warnings']))

    def test_sufficient_always_outranks_insufficient(self):
        db = regime(Y_BLEED, Y_BLEED)
        r = self.tune(db, grid={'min_market_cap_usd': ['200000']})  # filters every candidate: zero trades and zero loss beats base's losses
        self.assertEqual([(c['label'], c['flags']) for c in r['configs']], [('base', []), ('min_market_cap_usd=200000', ['INSUFFICIENT'])])
        self.assertLess(D(r['configs'][0]['select']['total_pnl_sol']), D(r['configs'][1]['select']['total_pnl_sol']))

    def test_selection_bias_and_assumption_warnings_present(self):
        r = self.tune(self.tight_wins_select_but_not_confirm())
        text = ' '.join(r['warnings'])
        self.assertIn('selection bias', text)
        self.assertIn('SIMULATION', text)
        self.assertEqual(r['replay_assumptions']['pool_fee_bps'], 30)

    def test_proposal_written_only_when_the_select_winner_is_confirmed(self):
        kinds = [W_POP_FADE] * 40
        props = self.dir / 'proposals'
        before_default = self.sha(DEFAULT)
        r = self.tune(kinds, grid=PROPOSE_GRID, proposals_dir=props)
        self.assertEqual(r['proposal_status'], 'WRITTEN')
        doc = json.loads(Path(r['proposal']).read_text())
        self.assertEqual(Path(r['proposal']).parent, props)
        self.assertEqual(doc['status'], 'PROPOSAL_NOT_APPLIED')
        self.assertEqual(doc['current']['strategy_version'], 'lean-1')
        self.assertNotEqual(doc['proposed_strategy_version'], 'lean-1')
        self.assertEqual(set(doc['diff']), {'tp_ladder.0.trigger'})
        self.assertEqual(doc['diff']['tp_ladder.0.trigger'], {'from': '1.4', 'to': '1.2'})
        self.assertEqual(S.StrategyConfig.from_dict(doc['proposed_config']).strategy_version, doc['proposed_strategy_version'])
        ev = doc['evidence']
        self.assertGreater(D(ev['select']['total_pnl_sol']), D(ev['base_select']['total_pnl_sol']))
        self.assertGreater(D(ev['confirm']['total_pnl_sol']), D(ev['base_confirm']['total_pnl_sol']))
        self.assertEqual((ev['chosen_on'], ev['confirmed_on'], ev['configs_evaluated']), ('select', 'confirm', 2))
        self.assertIn('selection bias', ' '.join(doc['warnings']))
        self.assertEqual(self.sha(DEFAULT), before_default)  # a proposal never edits the live config
        self.assertEqual(sorted(p.name for p in props.iterdir()), [Path(r['proposal']).name])

    def test_proposal_file_is_never_overwritten(self):
        props = self.dir / 'proposals'
        kinds = [W_POP_FADE] * 40
        self.tune(kinds, grid=PROPOSE_GRID, proposals_dir=props)
        with self.assertRaises(FileExistsError):
            self.tune(kinds, grid=PROPOSE_GRID, proposals_dir=props, name='again.sqlite')

    def test_no_proposal_when_base_wins_the_select_window(self):
        props = self.dir / 'proposals'
        r = self.tune(regime(X_DIP_RALLY, X_DIP_RALLY), proposals_dir=props)
        self.assertEqual((r['proposal_status'], r['proposal']), ('NO_SELECTION_IMPROVEMENT', None))
        self.assertFalse(props.exists())

    def test_no_directory_means_no_proposal_file(self):
        r = self.tune([W_POP_FADE] * 40, grid=PROPOSE_GRID)
        self.assertEqual((r['proposal_status'], r['proposal']), ('WOULD_PROPOSE_NO_DIRECTORY_GIVEN', None))

    def test_base_with_too_few_select_trades_blocks_proposal(self):
        r = self.tune([W_POP_FADE] * 40, grid=PROPOSE_GRID, min_select=11)
        self.assertEqual((r['proposal_status'], r['proposal']), ('NO_SUFFICIENT_ALTERNATIVE', None))

    def test_too_few_confirm_trades_is_not_confirmed(self):
        r = self.tune([W_POP_FADE] * 40, grid=PROPOSE_GRID, min_confirm=11, proposals_dir=self.dir / 'proposals')
        self.assertEqual((r['proposal_status'], r['proposal']), ('NOT_CONFIRMED', None))

    def test_alternative_must_also_be_profitable_in_the_select_window_not_merely_less_bad(self):
        r = self.tune(regime(Y_BLEED, X_DIP_RALLY, train=X_DIP_RALLY), proposals_dir=self.dir / 'proposals',
                      grid={'stop_fraction': ['0.08']})
        by = {c['label']: c for c in r['configs']}
        self.assertGreater(D(by['stop_fraction=0.08']['select']['total_pnl_sol']), D(by['base']['select']['total_pnl_sol']))
        self.assertLess(D(by['stop_fraction=0.08']['select']['total_pnl_sol']), 0)
        self.assertEqual((r['proposal_status'], r['proposal']), ('NO_SELECTION_IMPROVEMENT', None))

    def test_a_confirm_window_that_loses_money_does_not_confirm_even_if_it_loses_less_than_the_base(self):
        r = self.tune(regime(W_POP_FADE, L_SMALL_POP, train=W_POP_FADE), proposals_dir=self.dir / 'proposals')
        by = {c['label']: c for c in r['configs']}
        best, base = by['tp_ladder.0.trigger=1.2'], by['base']
        self.assertGreater(D(best['confirm']['total_pnl_sol']), D(base['confirm']['total_pnl_sol']))   # less bad than the base...
        self.assertLess(D(best['confirm']['mean_pnl_sol']), 0)                                           # ...but not profitable
        self.assertGreaterEqual(best['confirm']['n_trades'], 5)
        self.assertEqual((r['winner'], r['proposal_status'], r['proposal']), ('tp_ladder.0.trigger=1.2', 'NOT_CONFIRMED', None))

    def test_the_minimum_select_trade_count_is_inclusive(self):
        kinds = [W_POP_FADE] * 40
        at = self.tune(kinds, min_select=10)
        over = self.tune(kinds, min_select=11, name='b.sqlite')
        self.assertEqual([c['flags'] for c in at['configs']], [[], []])
        self.assertEqual([c['flags'] for c in over['configs']], [['INSUFFICIENT'], ['INSUFFICIENT']])

    def test_an_alternative_equal_to_base_is_not_an_improvement(self):
        r = self.tune([X_DIP_RALLY] * 40, grid={'stop_fraction': ['0.30']}, proposals_dir=self.dir / 'proposals')  # same trades, same pnl
        by = {c['label']: c for c in r['configs']}
        self.assertEqual(by['stop_fraction=0.30']['select']['total_pnl_sol'], by['base']['select']['total_pnl_sol'])
        self.assertGreater(D(by['base']['select']['mean_pnl_sol']), 0)
        self.assertEqual((r['proposal_status'], r['proposal']), ('NO_SELECTION_IMPROVEMENT', None))

    def test_insufficient_base_cannot_be_beaten_so_no_proposal(self):
        weak_base = cfg(daily_loss_stop_fraction='0.9', daily_liquidate_fraction='0.95', loss_streak_pause_at=100, min_market_cap_usd='200000')
        props = self.dir / 'proposals'
        r = self.tune([X_DIP_RALLY] * 40, base=weak_base, grid={'min_market_cap_usd': ['50000']}, proposals_dir=props)
        by = {c['label']: c for c in r['configs']}
        self.assertEqual(by['base']['flags'], ['INSUFFICIENT'])
        self.assertEqual(by['min_market_cap_usd=50000']['flags'], [])
        self.assertEqual((r['proposal_status'], r['proposal']), ('BASE_INSUFFICIENT', None))
        self.assertFalse(props.exists())

    def test_databases_are_not_modified(self):
        live, paths = make_db(self.dir, regime(Y_BLEED, Y_BLEED))
        before = {p: self.sha(p) for p in (live, paths)}
        T.tune(live, paths, {'stop_fraction': ['0.08']}, base=BASE, now=NOW, **MIN)
        self.assertEqual({p: self.sha(p) for p in (live, paths)}, before)
        # a plain mode=ro open of a WAL database may leave -wal / -shm beside it (SQLite recreates them to read); the data files
        # themselves are byte-identical and nothing else may appear
        allowed = {'lean.sqlite', 'paths.sqlite', 'lean.sqlite-wal', 'lean.sqlite-shm', 'paths.sqlite-wal', 'paths.sqlite-shm'}
        self.assertLessEqual({p.name for p in self.dir.iterdir()}, allowed)
        self.assertTrue({'lean.sqlite', 'paths.sqlite'} <= {p.name for p in self.dir.iterdir()})
        for sidecar in self.dir.glob('*-wal'):
            self.assertEqual(sidecar.stat().st_size, 0)   # nothing was ever written through the read-only connection

    def test_too_few_paths(self):
        with self.assertRaises(T.TuneError):
            self.tune([Y_BLEED, Y_BLEED])

    def test_deterministic(self):
        kinds = self.tight_wins_select_but_not_confirm()
        a, b = self.tune(kinds, name='a.sqlite'), self.tune(kinds, name='b.sqlite')
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_unusable_rows_are_surfaced(self):
        live, paths = make_db(self.dir, regime(Y_BLEED, Y_BLEED))
        store = L.PathStore(paths)
        store.db.execute("INSERT INTO path_marks(mint,ts,slot,base_raw,quote_raw,status) VALUES('P000',?,1,0,5,'OK')", (T0 + 7,))
        store.close()
        r = T.tune(live, paths, {'stop_fraction': ['0.08']}, base=BASE, now=NOW, **MIN)
        self.assertEqual(r['load_skipped'], {'MARK_BAD_RESERVES': 1})
        self.assertTrue(any('unusable' in w for w in r['warnings']))

    def test_rolled_over_archives_are_read_oldest_first(self):
        d1, d2 = self.dir / 'a', self.dir / 'b'
        d1.mkdir()
        d2.mkdir()
        kinds = regime(Y_BLEED, Y_BLEED)
        live, p1 = make_db(d1, kinds[:20])
        _, p2 = make_db(d2, kinds[20:])
        # the second file's mints restart at P000: shift them so that they are the newer paths
        s = L.PathStore(p2)
        s.db.execute("UPDATE paths SET start_ts = start_ts + 1000000, mint = 'N' || mint")
        s.db.execute("UPDATE path_marks SET ts = ts + 1000000, mint = 'N' || mint")
        s.close()
        r = T.tune(live, [p1, p2], {'stop_fraction': ['0.08']}, base=BASE, now=NOW, **MIN)
        self.assertEqual(r['split']['paths_total'], 40)


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

    def test_variants_are_ranked_on_the_select_window_and_the_better_timing_wins(self):
        live, paths = make_db(self.dir, [D_DUMP_RALLY] * 40)
        r = T.tune(live, paths, TIMING_GRID, base=BASE, now=NOW, **MIN)
        top = r['configs'][0]
        self.assertEqual(top['label'], 'entry_timing=pullback(0.15)')
        self.assertEqual(top['replay']['entry_timing']['kind'], 'pullback')
        by = {c['label']: c for c in r['configs']}
        self.assertLess(D(by['base']['select']['total_pnl_sol']), 0)
        self.assertGreater(D(top['select']['total_pnl_sol']), 0)
        self.assertGreater(D(top['train']['total_pnl_sol']), 0)   # the variant is applied in every window, not just one
        self.assertLess(D(by['base']['train']['total_pnl_sol']), 0)
        self.assertEqual(by['entry_timing=momentum(3m)']['select']['n_trades'], 0)  # the dump path never builds 3 rising minutes
        self.assertIn('INSUFFICIENT', by['entry_timing=momentum(3m)']['flags'])
        self.assertEqual(r['configs'][-1]['label'], 'entry_timing=momentum(3m)')  # insufficient ranks last even though it never lost

    def test_timing_proposal_names_the_runner_change_it_needs(self):
        live, paths = make_db(self.dir, [D_DUMP_RALLY] * 40)
        props = self.dir / 'proposals'
        r = T.tune(live, paths, TIMING_GRID, base=BASE, now=NOW, **MIN, proposals_dir=props)
        self.assertEqual(r['proposal_status'], 'WRITTEN')
        doc = json.loads(Path(r['proposal']).read_text())
        self.assertEqual(doc['diff'], {})  # no strategy parameter changes
        self.assertEqual(doc['requires_runner_change'], ['replay.entry_timing'])
        self.assertEqual(doc['replay_overrides'], {'replay.entry_timing': {'kind': 'pullback', 'pullback_pct': '0.15'}})
        self.assertEqual(doc['status'], 'PROPOSAL_NOT_APPLIED')

    def test_report_shows_the_timing_variant(self):
        live, paths = make_db(self.dir, [D_DUMP_RALLY] * 40)
        html = T.render_html(T.tune(live, paths, TIMING_GRID, base=BASE, now=NOW, **MIN))
        self.assertIn('entry_timing=pullback(0.15)', html)
        self.assertIn('pullback_pct', html)  # the replay assumptions list the base timing config


class ReportTests(Tmp):
    def result(self, grid=None):
        live, paths = make_db(self.dir, regime(Y_BLEED, X_DIP_RALLY))
        return T.tune(live, paths, grid or {'stop_fraction': ['0.08']}, base=BASE, now=NOW, **MIN)

    def test_html_is_escaped_and_complete(self):
        r = self.result({'stop_fraction': ['0.08', '<script>alert(1)</script>']})
        html = T.render_html(r)
        self.assertNotIn('<script>alert', html)
        self.assertIn('&lt;script&gt;', html)
        for needle in ('SELECT window', 'selection bias', 'PAPER ONLY', 'Never applied automatically', 'Invalid grid combinations', 'rank'):
            self.assertIn(needle, html)

    def test_untrusted_strings_in_every_header_are_escaped(self):
        live, paths = make_db(self.dir, regime(Y_BLEED, X_DIP_RALLY))
        hostile = cfg(strategy_version='<i>v</i>', daily_loss_stop_fraction='0.9', daily_liquidate_fraction='0.95', loss_streak_pause_at=100)
        html = T.render_html(T.tune(live, paths, {'stop_fraction': ['0.08']}, base=hostile, now=NOW, **MIN))
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
        live, paths = make_db(self.dir, regime(Y_BLEED, X_DIP_RALLY))
        grid = self.dir / 'grid.json'
        grid.write_text(json.dumps({'stop_fraction': ['0.08']}))
        out = self.dir / 'out'
        out.mkdir()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = T.main(['--db', str(live), '--paths-db', str(paths), '--grid', str(grid), '--out-dir', str(out), '--min-select', '5', '--min-confirm', '5', '--now', str(NOW), '--calibrate'])
        self.assertEqual(code, 0)
        summary = json.loads(buf.getvalue())
        self.assertEqual((summary['status'], summary['proposal'], summary['calibration']), ('OK', None, 'NO_LIVE_FILLS'))
        self.assertTrue(Path(summary['html']).is_file())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bad = T.main(['--db', str(live), '--paths-db', str(paths), '--grid', str(self.dir / 'nope.json'), '--out-dir', str(out), '--now', '1'])
        self.assertEqual(bad, 2)
        self.assertEqual(json.loads(buf.getvalue())['status'], 'ERROR')
        grid.write_text(json.dumps({'unknown_param': ['1']}))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(T.main(['--db', str(live), '--paths-db', str(paths), '--grid', str(grid), '--out-dir', str(out), '--now', '2']), 2)


if __name__ == '__main__':
    unittest.main()
