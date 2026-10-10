"""SYNTHETIC_TEST_ONLY: in-memory paths through the REAL desk.engine; no provider, ledger writes or live store.

T27F: the bracket is checked three ways that do not share code with the dynamic program: known answers for the
three review scenarios, an independent sequential engine run that replays the argmin/argmax mark sequences and
must reproduce the bounds exactly, and a property test that throws random intra-gap mark schedules at the
engine and requires every outcome to lie inside the bracket.
"""
import contextlib
import copy
import io
import json
import random
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from desk import engine
from desk.ledger import Ledger
from desk.model import D
from tests import helpers
from tools.research import counterfactual as cf
from tools.research import shadow_strategies as sh

ROOT = Path(__file__).resolve().parents[1]
BASE = json.loads((ROOT / 'config/paper.json').read_text())
A = dict(sh.DEFAULT_ASSUMPTIONS)
T0 = 1_000_000.0
HORIZONS = (300, 900, 1800, 3600, 7200, 21600)
# 2e8 tokens (raw 2e14 at 6 decimals) and 80 SOL: mcap ~ $60k, liquidity ~ $24k -> passes the live windows.
PATHS = {'rug': [80, 80, 40, 20, 10, 5], 'pump': [80, 100, 160, 200, 150, 120], 'flat': [80] * 6}


def candidate(quotes, mint='M1', migrated=T0, died=None, horizons=HORIZONS, transient=()):
    """``died`` = the horizon of the first POOL_DEAD sample (terminal death); ``transient`` = dead samples that later reappear."""
    return {'mint': mint, 'pool': 'P-' + mint, 'migrated_at': migrated, 'pool_died': died is not None, 'died_at': died,
            'transient_dead': list(transient),
            'samples': [{'horizon': h, 'base_raw': 2 * 10 ** 14, 'quote_raw': int(q * 1e9)} for h, q in zip(horizons, quotes)]}


def assumptions(**changes):
    return {**A, **changes}


TOLERANCE = Decimal('1e-20')   # the engine works at 28 digits; the DP sums per-gap deltas, the oracle reads the final total


def close(a, b):
    return abs(Decimal(a) - Decimal(b)) <= TOLERANCE


class ShadowTests(unittest.TestCase):
    def trade(self, quotes, cfg=None, **kw):
        return sh.simulate(candidate(quotes, **kw), cfg or BASE, A, 'samples')

    # ------------------------------------------------------------ nominal known answers
    def test_stop_known_answer_nominal(self):
        t = self.trade(PATHS['rug'])
        self.assertEqual((t['exit_reason'], t['exit_at'], t['entry_at']), ('STOP', 1001800, 1000300))
        self.assertLess(t['pnl_sol'], 0)
        self.assertEqual(t['pnl_sol'], t['pnl_best_sol'])        # the nominal run is ONE history: no interval here

    def test_time_stop_nominal_exit_is_the_first_sample_after_the_deadline(self):
        s = self.trade(PATHS['flat'])
        self.assertEqual((s['exit_reason'], s['exit_at']), ('TIME_STOP', 1003600))
        self.assertTrue(Decimal('-0.0016') < s['pnl_sol'] < Decimal('-0.0013'))        # round-trip fee + slippage + pool fee only

    def test_profitable_path_runs_to_horizon_end_through_engine_liquidate(self):
        t = self.trade(PATHS['pump'])
        self.assertEqual((t['exit_reason'], t['horizon_end']), ('LIQUIDATE', True))
        self.assertGreater(t['pnl_sol'], 0)

    def test_pool_death_while_holding_loses_remaining_cost(self):
        t = self.trade([80, 80, 80], died=3600, horizons=HORIZONS[:3])   # flat: no ladder sale, no timer reached
        self.assertEqual((t['exit_reason'], t['horizon_end']), ('POOL_DEAD', True))
        self.assertEqual(t['return'], Decimal(-1))
        partial = self.trade([80, 100, 160], died=3600, horizons=HORIZONS[:3])
        self.assertEqual(partial['exit_reason'], 'POOL_DEAD')
        self.assertTrue(Decimal(-1) < partial['return'] < 0)                          # the +40% ladder sale is kept, the rest is lost

    def test_rejected_candidates_report_the_engine_reason(self):
        r = sh.simulate(candidate([0.5] * 6), BASE, A, 'samples')   # liquidity far below the floor
        self.assertEqual((r['entered'], r['reject']), (False, 'MARKET_CAP'))
        cfg = sh.build_config(BASE, {'min_age_seconds': 900})
        t = sh.simulate(candidate(PATHS['flat']), cfg, A, 'samples')
        self.assertEqual(t['entry_at'], 1000900)                    # the age window moves the entry to the +15m sample
        # T27F: features are keyed by mint and say when they became known (as_of); known from the start here.
        danger = sh.simulate(candidate(PATHS['flat']), BASE, A, 'samples', {'M1': {'as_of': 0.0, 'fields': {'danger': True}}})
        self.assertEqual((danger['entered'], danger['reject']), (False, 'DANGER'))

    # ---------------------------------------------------------- engine parity (nominal path)
    def test_variant_equal_to_live_config_replays_the_real_engine_exactly(self):
        for name, quotes in PATHS.items():
            with self.subTest(path=name):
                c = candidate(quotes)
                state = engine.initial_state(BASE)
                points = sh._path_points(c)
                closed = False
                for serial, (ts, base_raw, quote_raw) in enumerate(points, 1):
                    _, out = engine.transition(state, sh.synth_event(c, ts, base_raw, quote_raw, serial, A), BASE)
                    if c['mint'] not in state['positions'] and any(x.get('side') == 'sell' for x in out):
                        closed = True
                        break
                if not closed:  # held to the end of the path: the same engine LIQUIDATE the tool performs
                    ts, base_raw, quote_raw = points[-1]
                    engine.transition(state, {'schema_version': 1, 'event_id': 'ctl', 'ts': ts + 1, 'kind': 'control',
                                              'command': 'LIQUIDATE', 'actor': 'operator'}, BASE)
                    engine.transition(state, sh.synth_event(c, ts + 2, base_raw, quote_raw, 99, A), BASE)
                    self.assertEqual(state['positions'], {})
                trade = sh.simulate(c, BASE, A, 'samples')
                self.assertEqual(trade['pnl_sol'], Decimal(state['realized_pnl']))

    def test_every_decision_is_made_by_the_real_engine(self):
        calls = []
        real = engine.transition
        def spy(state, e, cfg, **kw):
            calls.append((e['kind'], e['ts'], e.get('provenance')))
            return real(state, e, cfg, **kw)
        with patch.object(engine, 'transition', spy):
            sh.bracket_trade(candidate(PATHS['rug']), BASE, A)
        self.assertGreaterEqual(len(calls), 20)
        self.assertEqual({p for k, _, p in calls if k == 'market'}, {'SYNTHETIC_TEST_ONLY'})

    def test_changed_parameters_change_the_engine_outcome(self):
        tight = self.trade(PATHS['rug'], sh.build_config(BASE, {'stop_fraction': '0.05'}))
        loose = self.trade(PATHS['rug'], sh.build_config(BASE, {'stop_fraction': '0.60'}))
        self.assertEqual(tight['exit_at'], 1001800)
        self.assertGreater(loose['exit_at'], tight['exit_at'])       # a 60% stop needs the later, deeper sample
        self.assertLess(loose['pnl_sol'], tight['pnl_sol'])

    def test_the_nominal_run_never_reads_a_later_sample(self):
        for name, quotes in PATHS.items():
            with self.subTest(path=name):
                c = candidate(quotes); points = sh._path_points(c)
                seen = []
                real = engine.transition
                def spy(state, e, cfg, **kw):
                    if e['kind'] == 'market':
                        seen.append((e['ts'], e['reserve_sol']))
                    return real(state, e, cfg, **kw)
                with patch.object(engine, 'transition', spy):
                    sh.simulate(c, BASE, A, 'samples')
                for ts, reserve in seen:
                    known = [p for p in points if p[0] <= ts]
                    self.assertTrue(known, 'event before any sample')
                    expected = known[-1] if ts <= points[-1][0] else points[-1]
                    self.assertEqual(Decimal(reserve), Decimal(expected[2]) / 10 ** 9, (ts, expected[0]))

    def test_changing_the_future_cannot_change_an_earlier_nominal_exit(self):
        base = candidate(PATHS['rug'])
        tampered = copy.deepcopy(base)
        for s in tampered['samples'][3:]:
            s['quote_raw'] = 10 ** 15; s['base_raw'] = 1                  # absurd future after the +30m exit
        a = sh.simulate(base, BASE, A, 'samples'); b = sh.simulate(tampered, BASE, A, 'samples')
        for key in ('entry_at', 'exit_at', 'exit_reason', 'pnl_sol'):
            self.assertEqual(a[key], b[key], key)

    # ------------------------------------------------------------- statistics
    def fake(self, mint, entry_at, lo, hi, cost='0.1'):
        cost = Decimal(cost)
        return {'mint': mint, 'entry_at': entry_at, 'cost_sol': cost, 'pnl_lo': Decimal(lo), 'pnl_hi': Decimal(hi),
                'ret_lo': Decimal(lo) / cost, 'ret_hi': Decimal(hi) / cost, 'exit_reasons': ['STOP'], 'ambiguous': lo != hi}

    def test_summary_known_answers(self):
        trades = [self.fake('a', 1, '0.01', '0.01'), self.fake('b', 2, '-0.03', '-0.01'), self.fake('c', 3, '0.02', '0.02'),
                  self.fake('d', 4, '-0.02', '0.005')]
        s = sh.summarize(trades, name='x', n_variants=10)
        self.assertEqual((s['trades'], s['ambiguous_trades'], s['ambiguous_share']), (4, 2, 0.5))
        self.assertEqual(s['net_pnl_sol'], (Decimal('-0.02'), Decimal('0.025')))
        self.assertEqual(s['win_rate'], (Decimal('0.5'), Decimal('0.75')))
        self.assertEqual(s['mean_return'], (Decimal('-0.05'), Decimal('0.0625')))
        lo_series = [Decimal(x) for x in ('0.01', '-0.03', '0.02', '-0.02')]
        self.assertEqual(sh._max_drawdown(lo_series), Decimal('-0.03'))
        self.assertEqual(min(s['max_drawdown_sol']), Decimal('-0.03'))
        self.assertEqual(s['alpha'], 0.005)                                       # Bonferroni over ten variants
        self.assertEqual(s, sh.summarize(trades, name='x', n_variants=10))        # deterministic bootstrap
        low, high = s['bootstrap_ci']; self.assertLessEqual(low, high)
        self.assertEqual(sh.summarize([], name='x')['trades'], 0)

    def test_bootstrap_resamples_scale_with_the_correction_and_interpolate(self):
        # T27F item 5: at alpha = 0.05/1000 the old index floor(alpha/2*4000) collapsed to the single smallest mean.
        self.assertEqual(sh.bootstrap_size(0.05), sh.BOOTSTRAP)
        self.assertEqual(sh.bootstrap_size(0.05 / 1000), min(sh.MAX_BOOTSTRAP, 40 * 1000 * 20))
        self.assertGreater(sh.bootstrap_size(0.05 / 100), sh.BOOTSTRAP)
        self.assertEqual(sh._quantile([0.0, 1.0], 0.25), 0.25)
        self.assertEqual(sh._quantile([0.0, 10.0, 20.0], 0.75), 15.0)
        rng = random.Random(3)
        returns = [Decimal(str(rng.uniform(-0.3, 0.5))) for _ in range(40)]
        low, high = sh._bootstrap(returns, 'v', 0.05 / 500)
        values = sorted(float(r) for r in returns)
        mean = sum(values) / len(values)
        self.assertLess(low, mean); self.assertGreater(high, mean)
        self.assertGreater(low, min(values)); self.assertLess(high, max(values))

    # ------------------------------------------------------------ grids/inputs
    def test_grid_expansion_and_fail_closed_validation(self):
        grid = sh.load_grid(ROOT / 'config/experiments/shadow/exit-grid.json')
        self.assertEqual(len(grid['variants']), 1 + 3 * 2 * 2)
        self.assertEqual(grid['assumptions']['excursion_fraction'], '0.30')
        self.assertEqual(sh.load_grid(ROOT / 'config/experiments/shadow/live-baseline.json')['variants'][0]['overrides'], {})
        with tempfile.TemporaryDirectory() as d:
            def write(spec):
                p = Path(d) / 'g.json'; p.write_text(json.dumps({'version': 'shadow-grid-1', 'base_config': 'config/paper.json', **spec})); return p
            for bad in ({'variants': [{'name': 'x', 'overrides': {'no_such_key': 1}}]},
                        {'variants': [{'name': 'x', 'overrides': {'stop_fraction': 0.18}}]},                 # wrong type
                        {'variants': [{'name': 'x'}, {'name': 'x'}]},                                        # duplicate name
                        {'variants': []},
                        {'variants': [{'name': 'x'}], 'assumptions': {'surprise': 1}},
                        {'variants': [{'name': 'x'}], 'assumptions': {'excursion_fraction': '3'}},
                        {'variants': [{'name': 'x'}], 'assumptions': {'excursion_fraction': 'abc'}},
                        {'variants': [{'name': 'x'}], 'assumptions': {'max_intra_marks': 9}},
                        {'variants': [{'name': 'x'}], 'assumptions': {'max_intra_marks': True}}):
                with self.subTest(bad=bad), self.assertRaises(sh.ShadowError):
                    sh.load_grid(write(bad))

    def test_module_has_no_provider_ledger_or_write_path(self):
        source = Path(sh.__file__).read_text()
        for forbidden in ('urllib', 'provider_pacing', 'HELIUS', 'Ledger(', 'paper_cycle', 'INSERT ', 'UPDATE ', 'DELETE ', 'socket'):
            self.assertNotIn(forbidden, source)


# ---------------------------------------------------------------------------------------------------------------
class IndependentOracle:
    """Sequential real-engine run of the samples plus an explicit intra-gap mark schedule (shares no DP code)."""

    def __init__(self, cand, cfg, a):
        self.cand, self.cfg, self.a = cand, cfg, a
        self.points = sh._path_points(cand)
        self.mint = cand['mint']

    def run(self, schedule):
        """schedule: {gap_start_index: [(ts, quote_raw), ...]} -> realized PnL (Decimal), or None if never entered."""
        state = engine.initial_state(self.cfg)
        serial = [0]
        def feed(ts, base, quote):
            serial[0] += 1
            return engine.transition(state, sh.synth_event(self.cand, ts, base, quote, f'o{serial[0]}', self.a), self.cfg)[1]
        entered = None
        for index, (ts, base, quote) in enumerate(self.points):
            if entered is not None:
                for mts, mq in schedule.get(index - 1, []):
                    feed(mts, self.points[index - 1][1], mq)
                    if self.mint not in state['positions']:
                        return Decimal(state['realized_pnl'])
            out = feed(ts, base, quote)
            if entered is None:
                if any(x.get('type') == 'fill' and x.get('side') == 'buy' for x in out):
                    entered = index
                continue
            if self.mint not in state['positions']:
                return Decimal(state['realized_pnl'])
        if entered is None:
            return None
        last = len(self.points) - 1
        if self.cand['died_at'] is not None:
            for mts, mq in schedule.get(last, []):
                feed(mts, self.points[last][1], mq)
                if self.mint not in state['positions']:
                    return Decimal(state['realized_pnl'])
            return Decimal(state['realized_pnl']) - Decimal(state['positions'][self.mint]['cost_left'])
        ts, base, quote = self.points[last]
        engine.transition(state, {'schema_version': 1, 'event_id': 'oracle-ctl', 'ts': ts + 1, 'kind': 'control',
                                  'command': 'LIQUIDATE', 'actor': 'operator'}, self.cfg)
        feed(ts + 2, base, quote)
        return Decimal(state['realized_pnl'])

    def schedule_of(self, path):
        """Turn a DP mark sequence into an oracle schedule (marks are assigned to the gap that contains them)."""
        schedule = {}
        for mark in path:
            if mark['quote_raw'] is None:
                continue
            gap = max(i for i, p in enumerate(self.points) if p[0] < mark['ts'])
            schedule.setdefault(gap, []).append((mark['ts'], mark['quote_raw']))
        return schedule

    def interval(self, gap):
        """Independent price-space interval of a gap, from the stated assumption."""
        a = Decimal(str(self.a['excursion_fraction']))
        pa = D(self.points[gap][2]) / D(self.points[gap][1])
        if gap + 1 < len(self.points):
            pb = D(self.points[gap + 1][2]) / D(self.points[gap + 1][1])
            dead = False
        else:
            pb, dead = D(0), True
        lo = D(0) if dead else (1 - a) * min(pa, pb)
        return lo, (1 + a) * max(pa, pb)


class BracketKnownAnswerTests(unittest.TestCase):
    def bracket(self, quotes, cfg=None, a=None, **kw):
        return sh.bracket_trade(candidate(quotes, **kw), cfg or BASE, a or A)

    def replay(self, quotes, trade, cfg=None, a=None, **kw):
        oracle = IndependentOracle(candidate(quotes, **kw), cfg or BASE, a or A)
        return (oracle.run(oracle.schedule_of(trade['worst_path'])), oracle.run(oracle.schedule_of(trade['best_path'])))

    def assertTight(self, quotes, trade, cfg=None, a=None, **kw):
        worst, best = self.replay(quotes, trade, cfg, a, **kw)
        self.assertTrue(close(worst, trade['pnl_lo']) and close(best, trade['pnl_hi']), (worst, best, trade['pnl_lo'], trade['pnl_hi']))

    def test_the_nominal_run_is_always_inside_and_the_bounds_replay_exactly(self):
        for name, quotes in PATHS.items():
            with self.subTest(path=name):
                t = self.bracket(quotes)
                self.assertTrue(t['pnl_lo'] <= t['nominal']['pnl_sol'] <= t['pnl_hi'])
                self.assertTight(quotes, t)

    def test_zero_extra_marks_collapse_the_bracket_to_the_nominal_run(self):
        for name, quotes in PATHS.items():
            with self.subTest(path=name):
                t = self.bracket(quotes, a=assumptions(max_intra_marks=0))
                self.assertEqual((t['pnl_lo'], t['pnl_hi'], t['ambiguous']), (t['nominal']['pnl_sol'],) * 2 + (False,))

    def test_stop_crossed_and_recovered_between_samples(self):
        cfg = sh.build_config(BASE, {'time_stop_seconds': 21600})            # no timer in play: only the stop is at stake
        wide = self.bracket(PATHS['flat'], cfg)
        self.assertTrue(wide['ambiguous'])
        self.assertEqual(wide['worst_path'][0]['mark'], 'LO')                 # worst: it gaps through the stop to the low
        self.assertLess(wide['ret_lo'], Decimal('-0.25'))                     # lo = 0.7 x price: about -30% of the position
        best_stop = self.bracket(PATHS['flat'], cfg, assumptions(max_intra_marks=1, excursion_fraction='0.30'))
        self.assertGreaterEqual(best_stop['ret_hi'], best_stop['nominal']['pnl_sol'] / best_stop['cost_sol'])   # the nominal outcome stays reachable
        # A recovered stop needs the excursion to reach the stop level: with +-12% it cannot, and the path is unambiguous.
        narrow = self.bracket(PATHS['flat'], cfg, assumptions(excursion_fraction='0.12'))
        self.assertFalse(narrow['ambiguous'])
        self.assertEqual(narrow['pnl_lo'], narrow['nominal']['pnl_sol'])
        self.assertTight(PATHS['flat'], wide, cfg)
        # The stop LEVEL itself (best fill of a stop) is a mark of the enumeration for a path that ends below it.
        rug = self.bracket(PATHS['rug'])
        self.assertEqual(rug['best_path'][0]['mark'], 'STOP')
        self.assertAlmostEqual(float(rug['ret_hi']), -0.18, delta=0.02)
        self.assertLess(rug['pnl_lo'], rug['nominal']['pnl_sol'])             # the observed-sample fill was NOT the worst case

    def test_several_ladder_rungs_hit_inside_one_gap(self):
        # +5m -> +15m the pool price triples: the engine sells ONE rung per observed mark, so a coarse gap hides several.
        quotes = [80, 260, 260, 260, 260, 260]
        narrow = assumptions(excursion_fraction='0.10')                       # keeps the stop out of reach: only rungs matter
        t = self.bracket(quotes, a=narrow)
        self.assertTrue(t['ambiguous'])
        self.assertGreater(t['pnl_hi'], t['nominal']['pnl_sol'])
        self.assertLess(t['pnl_lo'], t['nominal']['pnl_sol'])
        # The scenario itself: three marks at the three rung levels inside the first gap sell three rungs at 1.4x/2x/3x.
        cand = candidate(quotes)
        oracle = IndependentOracle(cand, BASE, narrow)
        index, state, _ = sh._find_entry(cand, BASE, narrow, None)
        base = cand['samples'][0]['base_raw']
        explorer = sh._Explorer(cand, BASE, narrow)
        st = copy.deepcopy(state)
        marks = []
        for k, trigger in enumerate(sh.LADDER_TRIGGERS):
            q = sh._quote_raw_for(st['positions']['M1'], base, trigger * (1 + sh.MARGIN), BASE, narrow, 'ROUND_CEILING')
            explorer.feed(st, 1000301 + k, base, q)
            marks.append((1000301 + k, q))
        self.assertEqual(st['positions']['M1']['stage'], 3)                     # all three rungs fired inside ONE gap
        pnl = oracle.run({0: marks})
        self.assertTrue(t['pnl_lo'] <= pnl <= t['pnl_hi'])                      # ... and the bracket covers that history
        self.assertNotEqual(pnl, t['nominal']['pnl_sol'])
        self.assertTight(quotes, t, a=narrow)
        # With no extra marks only one rung per sample can be sold.
        collapsed = self.bracket(quotes, a=assumptions(excursion_fraction='0.10', max_intra_marks=0))
        self.assertFalse(collapsed['ambiguous'])

    def test_unknown_peak_moves_the_trailing_level(self):
        t = self.bracket(PATHS['pump'])
        kinds = [m['mark'] for m in t['best_path']]
        self.assertIn('TRAIL', kinds)
        self.assertLess(kinds.index('HI'), kinds.index('TRAIL'))              # best: the unseen peak is raised, then the level is hit
        self.assertGreater(t['pnl_hi'], t['nominal']['pnl_sol'])
        self.assertTight(PATHS['pump'], t)

    def test_a_deadline_inside_a_gap_makes_the_exit_price_ambiguous(self):
        t = self.bracket(PATHS['flat'])
        deadline = 1000300 + BASE['time_stop_seconds']                        # opened at +5m, time stop 45 min later: inside (+30m, +1h)
        self.assertTrue(any(m['ts'] == deadline for m in t['best_path']), t['best_path'])
        self.assertEqual(t['nominal']['exit_at'], 1003600)                    # nominal: the first sample after the deadline
        self.assertTrue(t['ambiguous'])

    def test_terminal_pool_death_brackets_the_unknown_death_time(self):
        quotes = [80, 80, 80]
        t = self.bracket(quotes, died=3600, horizons=HORIZONS[:3])
        self.assertEqual(t['nominal']['return'], Decimal(-1))                 # nominal: the whole position is lost
        self.assertEqual(t['ret_lo'], Decimal(-1))
        self.assertGreater(t['ret_hi'], Decimal('-0.35'))                     # the engine could have stopped out on the way down
        self.assertTrue(t['ambiguous'])
        self.assertTight(quotes, t, died=3600, horizons=HORIZONS[:3])
        # The deadline (+50 min) falls before the death: the best case is a time-stop fill at the highest price it still fires at.
        self.assertTrue(any(m['mark'] == 'TIMECAP' and m['ts'] == 1000300 + BASE['time_stop_seconds'] for m in t['best_path']), t['best_path'])

    def test_transient_dead_sample_opens_the_gap_down_to_zero(self):
        quotes = [80, 80, 80, 80]
        zero = assumptions(excursion_fraction='0')
        cfg = sh.build_config(BASE, {'time_stop_seconds': 21600})
        plain = self.bracket(quotes, cfg, zero, horizons=(300, 900, 3600, 7200))
        self.assertFalse(plain['ambiguous'])                                    # no excursion, no dead sample: nothing is hidden
        revived = self.bracket(quotes, cfg, zero, horizons=(300, 900, 3600, 7200), transient=(1800,))
        self.assertTrue(revived['ambiguous'])                                   # a closed vault at +30m: the price may have hit zero
        self.assertLess(revived['pnl_lo'], plain['pnl_lo'])
        self.assertEqual(revived['pnl_hi'], plain['pnl_hi'])                    # a revival never makes the best case better

    def test_event_budget_reports_truncation_instead_of_guessing(self):
        with patch.object(sh, 'MAX_ENGINE_EVENTS', 5):
            t = sh.bracket_trade(candidate(PATHS['pump']), BASE, A)
        self.assertEqual((t['entered'], t['truncated']), (True, True))
        self.assertNotIn('pnl_lo', t)
        trades, rejects, truncated = [], {}, 0
        with patch.object(sh, 'MAX_ENGINE_EVENTS', 5):
            trades, rejects, truncated = sh.evaluate_variant([candidate(PATHS['pump'])], {'name': 'live', 'overrides': {}}, BASE, A)
        self.assertEqual((trades, truncated), ([], 1))

    def test_unentered_candidates_return_the_engine_reason(self):
        r = self.bracket([0.5] * 6)
        self.assertEqual((r['entered'], r['reject']), (False, 'MARKET_CAP'))


class BracketSoundnessTests(unittest.TestCase):
    """Throw random intra-gap schedules at the real engine: no outcome may leave the bracket."""

    SCENARIOS = {
        'rug': PATHS['rug'], 'pump': PATHS['pump'], 'flat': PATHS['flat'],
        'zigzag': [80, 140, 70, 160, 90, 60], 'crash_recover': [80, 30, 90, 200, 100, 150],
        'moon': [80, 400, 900, 1500, 800, 400],
    }

    def random_schedule(self, oracle, entry_index, rng, marks):
        schedule = {}
        for gap in range(entry_index, len(oracle.points)):
            lo, hi = oracle.interval(gap)
            t_a = oracle.points[gap][0]
            t_b = oracle.points[gap + 1][0] if gap + 1 < len(oracle.points) else oracle.points[gap][0] + 3600
            base = oracle.points[gap][1]
            k = rng.randint(0, marks)
            times = sorted(rng.sample(range(t_a + 1, t_b), k)) if t_b - t_a - 1 >= k else []
            if times and rng.random() < 0.4:                                   # land exactly on a timer deadline now and then
                times[0] = min(t_b - 1, max(t_a + 1, oracle.points[entry_index][0] + oracle.cfg['time_stop_seconds']))
                times = sorted(set(times))
            quotes = []
            for _ in times:
                pick = rng.random()
                price = lo if pick < 0.15 else hi if pick < 0.30 else lo + (hi - lo) * D(str(rng.random()))
                quotes.append(max(1, int(price * D(base))))
            schedule[gap] = list(zip(times, quotes))
        return schedule

    def entry_index(self, cand, cfg):
        index, _, _ = sh._find_entry(cand, cfg, A, None)
        return index

    def test_random_schedules_never_leave_the_bracket(self):
        rng = random.Random(20261011)
        checked = 0
        configs = [BASE, sh.build_config(BASE, {'stop_fraction': '0.30', 'time_stop_seconds': 1500}),
                   sh.build_config(BASE, {'trailing_fraction': '0.15', 'max_hold_seconds': 3000})]
        for name, quotes in self.SCENARIOS.items():
            for cfg_index, cfg in enumerate(configs):
                for died in (None, 7200):
                    cand = candidate(quotes[:4], horizons=HORIZONS[:4], died=died) if died else candidate(quotes)
                    trade = sh.bracket_trade(cand, cfg, A)
                    if not trade['entered']:
                        continue
                    oracle = IndependentOracle(cand, cfg, A)
                    index = self.entry_index(cand, cfg)
                    for _ in range(40):
                        pnl = oracle.run(self.random_schedule(oracle, index, rng, A['max_intra_marks']))
                        checked += 1
                        self.assertTrue(trade['pnl_lo'] - Decimal('1e-15') <= pnl <= trade['pnl_hi'] + Decimal('1e-15'),
                                        (name, cfg_index, died, float(pnl), float(trade['pnl_lo']), float(trade['pnl_hi'])))
        self.assertGreater(checked, 1000)

    def test_bounds_are_attained_by_their_own_mark_sequences_on_every_scenario(self):
        for name, quotes in self.SCENARIOS.items():
            with self.subTest(path=name):
                cand = candidate(quotes)
                trade = sh.bracket_trade(cand, BASE, A)
                if not trade['entered']:
                    continue
                oracle = IndependentOracle(cand, BASE, A)
                self.assertTrue(close(oracle.run(oracle.schedule_of(trade['worst_path'])), trade['pnl_lo']))
                self.assertTrue(close(oracle.run(oracle.schedule_of(trade['best_path'])), trade['pnl_hi']))

    def test_marks_land_on_the_engine_thresholds_they_claim(self):
        # The inverse of the engine's mark ratio: a RUNG mark sells exactly one more rung, a STOP mark closes the position.
        cand = candidate(PATHS['flat'])
        index, state, _ = sh._find_entry(cand, BASE, A, None)
        explorer = sh._Explorer(cand, BASE, A)
        base = cand['samples'][0]['base_raw']
        position = state['positions']['M1']
        for label, ratio_target in (('RUNG', sh.LADDER_TRIGGERS[0]), ('TOUCH', sh.TOUCH_RATIO)):
            below = sh._quote_raw_for(position, base, ratio_target * (1 - Decimal('1e-6')), BASE, A, 'ROUND_FLOOR')
            above = sh._quote_raw_for(position, base, ratio_target * (1 + Decimal('1e-6')), BASE, A, 'ROUND_CEILING')
            st_below, st_above = copy.deepcopy(state), copy.deepcopy(state)
            explorer.feed(st_below, 1000301, base, below)
            explorer.feed(st_above, 1000301, base, above)
            if label == 'RUNG':
                self.assertEqual((st_below['positions']['M1']['stage'], st_above['positions']['M1']['stage']), (0, 1))
            else:
                self.assertEqual((st_below['positions']['M1']['touched_15'], st_above['positions']['M1']['touched_15']), (False, True))
        stop = D(position['stop_ratio'])
        at_stop = sh._quote_raw_for(position, base, stop * (1 - Decimal('1e-9')), BASE, A, 'ROUND_FLOOR')
        st = copy.deepcopy(state)
        explorer.feed(st, 1000301, base, at_stop)
        self.assertNotIn('M1', st['positions'])
        self.assertAlmostEqual(float(Decimal(st['realized_pnl']) / D(position['initial_cost'])), -0.18, delta=0.01)


class StoreFixture(unittest.TestCase):
    """A real T26 counterfactual store with hand-written sample rows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / 'cf.sqlite'
        cf.init(self.store, now=T0)
        self.n = 0

    def add(self, mint, rows, migrated=None, outcome=None):
        """rows: [(horizon, status, quote_sol)] ; quote_sol only for OK rows."""
        self.n += 1
        cf.add_candidate(self.store, mint=mint, pool='P-' + mint, signature=f's{self.n}', slot=1,
                         migrated_at=T0 + self.n if migrated is None else migrated, seq=self.n, now=T0)
        with closing(sqlite3.connect(self.store)) as c:
            for h, status, quote in rows:
                ok = status == 'OK'
                c.execute('INSERT INTO samples(mint,horizon,due_at,sampled_at,base_raw,quote_raw,price,status,code) VALUES(?,?,?,?,?,?,?,?,?)',
                          (mint, h, T0 + h, T0 + h, str(2 * 10 ** 14) if ok else None, str(int(quote * 1e9)) if ok else None,
                           str(quote / 2e14) if ok else None, status, None if ok else 'X'))
            if outcome is not None:
                c.execute('INSERT INTO outcomes(mint,at,payload) VALUES(?,?,?)', (mint, T0, json.dumps(outcome)))
            c.commit()

    def full(self, quotes, status='OK'):
        return [(h, status, q) for h, q in zip(HORIZONS, quotes)]


class SelectionAndOutcomeTests(StoreFixture):
    def test_every_exclusion_is_counted_and_the_stages_add_up(self):
        self.add('none', [])                                                                           # no sample rows at all
        self.add('trunc', [(300, 'OK', 80), (900, 'OK', 80)])                                          # path not complete yet
        self.add('nothing', self.full([0] * 6, 'FAILED'))                                              # complete, never priced
        self.add('dead5m', [(300, 'POOL_DEAD', 0)] + [(h, 'MISSED', 0) for h in HORIZONS[1:]])         # dead before the baseline
        self.add('nobase', [(300, 'MISSED', 0)] + [(h, 'OK', 80) for h in HORIZONS[1:]])               # +5m not priced: cannot enter
        self.add('good', self.full(PATHS['flat']))
        self.add('hole', [(300, 'OK', 80), (900, 'MISSED', 0)] + [(h, 'OK', 80) for h in HORIZONS[2:]])  # FAILED/MISSED gap, still evaluated
        self.add('transient', [(300, 'OK', 80), (900, 'POOL_DEAD', 0)] + [(h, 'OK', 80) for h in HORIZONS[2:]])
        self.add('dies', [(300, 'OK', 80), (900, 'OK', 80), (1800, 'OK', 80), (3600, 'POOL_DEAD', 0), (7200, 'MISSED', 0), (21600, 'MISSED', 0)])
        selection = {}
        found = sh.load_candidates(self.store, selection)
        self.assertEqual([c['mint'] for c in found], ['good', 'hole', 'transient', 'dies'])
        s = selection
        self.assertEqual((s['candidates'], s['no_sample_rows'], s['truncated_paths'], s['dead_before_baseline'], s['no_priced_sample'],
                          s['baseline_not_priced'], s['evaluated']), (9, 1, 1, 1, 1, 1, 4))
        self.assertEqual(s['candidates'], sum(s[k] for k in sh.SELECTION_STAGES[1:]))             # nothing silently dropped
        self.assertEqual([(f['stage'], f['excluded'], f['remaining']) for f in s['funnel']],
                         [('no_sample_rows', 1, 8), ('truncated_paths', 1, 7), ('dead_before_baseline', 1, 6),
                          ('no_priced_sample', 1, 5), ('baseline_not_priced', 1, 4)])              # totals at each exclusion stage
        self.assertEqual((s['failed_or_missed_gaps'], s['transient_death'], s['terminal_death']), (2, 1, 1))   # 'hole' and 'dies' have MISSED rows
        died = {c['mint']: c for c in found}
        self.assertEqual((died['dies']['pool_died'], died['dies']['died_at'], died['transient']['pool_died'], died['transient']['transient_dead']),
                         (True, 3600, False, [900]))
        # An incomplete path can be evaluated on request, flagged and counted.
        partial = {}
        found = sh.load_candidates(self.store, partial, include_truncated=True)
        self.assertEqual((partial['truncated_paths'], partial['truncated_included'], partial['evaluated']), (0, 1, 5))
        self.assertIn('trunc', [c['mint'] for c in found])

    def test_run_reports_selection_totals_and_entry_rejects_per_stage(self):
        self.add('good', self.full(PATHS['flat'])); self.add('tiny', self.full([0.5] * 6)); self.add('none', [])
        selection = {}
        found = sh.load_candidates(self.store, selection)
        grid = {'name': 'g', 'base': BASE, 'assumptions': assumptions(max_intra_marks=0), 'variants': [{'name': 'live', 'overrides': {}}]}
        result = sh.run(found, grid, holdout_from=0, selection=selection)
        self.assertEqual(result['selection']['candidates'], 3)
        self.assertEqual(result['selection']['no_sample_rows'], 1)
        part = result['leaderboard'][0]['holdout']
        self.assertEqual((part['trades'], part['entry_rejects']), (1, {'MARKET_CAP': 1}))
        self.assertIn('HINT RECEIPT', result['age_basis'])

    def test_outcome_classification_join_and_split(self):
        rejected = {'v': 2, 'class': 'REJECTED', 'stage': 'ENGINE', 'codes': ['COST_BUDGET', 'MAX_POSITIONS'], 'detail': None}
        self.add('bought', self.full(PATHS['flat']), outcome={'v': 2, 'class': 'BOUGHT', 'stage': None, 'codes': [], 'detail': None})
        self.add('rej1', self.full(PATHS['flat']), outcome=rejected)
        self.add('rej2', self.full(PATHS['flat']), outcome=rejected)
        self.add('nd', self.full(PATHS['flat']), outcome={'v': 2, 'class': 'NOT_DISPATCHED', 'stage': None, 'codes': [], 'detail': None})
        self.add('legacy', self.full(PATHS['flat']), outcome={'dispatched': True, 'status': 'COMPLETE', 'codes': []})
        self.add('unrecorded', self.full(PATHS['flat']))
        groups = sh.load_outcomes(self.store)
        self.assertEqual(groups, {'bought': 'BOUGHT', 'rej1': 'REJECTED:ENGINE:COST_BUDGET', 'rej2': 'REJECTED:ENGINE:COST_BUDGET',
                                  'nd': 'NOT_DISPATCHED', 'legacy': 'UNKNOWN', 'unrecorded': 'UNKNOWN'})   # primary code only: one group per trade
        found = sh.load_candidates(self.store)
        grid = {'name': 'g', 'base': BASE, 'assumptions': assumptions(max_intra_marks=0), 'variants': [{'name': 'live', 'overrides': {}}]}
        result = sh.run(found, grid, holdout_from=0, outcomes=groups)
        split = result['leaderboard'][0]['holdout']['by_outcome']
        self.assertEqual({k: v['trades'] for k, v in split.items()},
                         {'BOUGHT': 1, 'REJECTED:ENGINE:COST_BUDGET': 2, 'NOT_DISPATCHED': 1, 'UNKNOWN': 2})
        self.assertEqual(sum(v['trades'] for v in split.values()), result['leaderboard'][0]['holdout']['trades'])

    def test_store_is_opened_by_the_t02f_rule_and_symlinks_are_refused(self):
        self.add('good', self.full(PATHS['flat']))
        wal = self.root / 'wal.sqlite'
        import shutil
        shutil.copyfile(self.store, wal)
        with closing(sqlite3.connect(wal, isolation_level=None)) as c:
            c.execute('PRAGMA journal_mode=WAL')
        self.assertEqual(sorted(p.name for p in self.root.glob('wal.sqlite*')), ['wal.sqlite'])
        self.assertEqual(cf.open_mode(wal), 'immutable')
        self.assertEqual([c['mint'] for c in sh.load_candidates(wal)], ['good'])
        self.assertEqual(sorted(p.name for p in self.root.glob('wal.sqlite*')), ['wal.sqlite'], 'no -wal/-shm created beside a quiet WAL store')
        self.assertEqual(cf.open_mode(self.store), 'ro')
        link = self.root / 'link.sqlite'; link.symlink_to(self.store)
        with self.assertRaises(cf.CounterfactualError):
            sh.load_candidates(link)
        before = self.store.read_bytes()
        sh.load_candidates(self.store)
        self.assertEqual(self.store.read_bytes(), before)


class FeatureTests(unittest.TestCase):
    def write(self, spec):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        path = Path(d.name) / 'f.json'; path.write_text(json.dumps(spec)); return path

    def test_features_must_say_when_they_became_known_and_never_after_the_path(self):
        cand = candidate(PATHS['flat'])
        for bad in ({'M1': {'danger': True}},                                                  # no as_of
                    {'M1': {'as_of': 'later', 'danger': True}},
                    {'M1': {'as_of': True, 'danger': True}},
                    {'M1': {'as_of': 1, 'surprise': 1}},                                        # unknown field
                    {'M1': {'as_of': T0 + 21600 + 1, 'danger': True}},                          # observed after the whole path: look-ahead
                    ['M1']):
            with self.subTest(bad=bad), self.assertRaises(sh.ShadowError):
                sh.load_features(self.write(bad), [cand])
        ok = sh.load_features(self.write({'M1': {'as_of': T0 + 300, 'danger': False, 'flow': '50'}}), [cand])
        self.assertEqual(ok, {'M1': {'as_of': T0 + 300, 'fields': {'danger': False, 'flow': '50'}}})

    def test_a_feature_known_later_never_influences_an_earlier_decision(self):
        cand = candidate(PATHS['flat'])
        features = {'M1': {'as_of': T0 + 900, 'fields': {'danger': True}}}     # danger is only known from the +15m sample on
        seen = []
        real = engine.transition
        def spy(state, e, cfg, **kw):
            seen.append((e['ts'], e['danger']))
            return real(state, e, cfg, **kw)
        with patch.object(engine, 'transition', spy):
            r = sh.simulate(cand, BASE, A, 'samples', features)
        self.assertEqual((r['entered'], r['reject']), (False, 'FEATURES_NOT_YET_KNOWN'))
        self.assertNotIn(T0 + 300, [ts for ts, _ in seen])                      # the +5m decision never ran with unknown features
        self.assertTrue(all(danger for _, danger in seen))                      # every decision that did run had the features
        # The same candidate with features known at +5m is judged on them from the first decision.
        early = sh.simulate(cand, BASE, A, 'samples', {'M1': {'as_of': T0 + 300, 'fields': {'danger': True}}})
        self.assertEqual((early['entered'], early['reject']), (False, 'DANGER'))
        # Candidates without a features entry use the explicit neutral defaults.
        self.assertTrue(sh.simulate(cand, BASE, A, 'samples', {'OTHER': {'as_of': 0.0, 'fields': {'danger': True}}})['entered'])
        # The bracket honours the same rule.
        self.assertEqual(sh.bracket_trade(cand, BASE, A, features)['reject'], 'FEATURES_NOT_YET_KNOWN')

    def test_neutral_defaults_and_assumptions_are_explicit_in_the_output(self):
        grid = {'name': 'g', 'base': BASE, 'assumptions': assumptions(max_intra_marks=0), 'variants': [{'name': 'live', 'overrides': {}}]}
        result = sh.run([candidate(PATHS['flat'])], grid, holdout_from=0)
        self.assertEqual(result['neutral_features'], sh.NEUTRAL_FEATURES)
        self.assertEqual((result['assumptions']['sol_usd'], result['assumptions']['token_supply_tokens']), ('150', '1000000000'))
        self.assertIn('max_intra_marks=0', result['bracket_assumption'])


class RankingTests(unittest.TestCase):
    def grid(self, names_overrides):
        return {'name': 'g', 'base': BASE, 'assumptions': assumptions(max_intra_marks=1),
                'variants': [{'name': n, 'overrides': o} for n, o in names_overrides]}

    def population(self, holdout_n, train_paths):
        out = []
        for i in range(10):
            out.append(candidate(train_paths[i % len(train_paths)], mint=f'T{i}', migrated=T0 + i))
        for i in range(holdout_n):
            out.append(candidate(PATHS['rug'] if i % 3 == 0 else PATHS['flat'], mint=f'H{i}', migrated=T0 + 1000 + i))
        return out

    def test_minimum_trades_gate_and_holdout_only_ranking(self):
        variants = [('live', {}), ('tight', {'stop_fraction': '0.05'}), ('loose', {'stop_fraction': '0.60'})]
        result = sh.run(self.population(34, [PATHS['flat']]), self.grid(variants), holdout_from=T0 + 500)
        self.assertEqual((result['candidates'], result['variants_tried']), ({'train': 10, 'holdout': 34}, 3))
        rows = result['leaderboard']
        self.assertEqual([r['rank'] for r in rows], [1, 2, 3])
        self.assertEqual({r['rank_status'] for r in rows}, {'RANKED'})
        lows = [r['holdout']['bootstrap_ci'][0] for r in rows]
        self.assertEqual(lows, sorted(lows, reverse=True))                            # ranked by the CI LOWER bound
        for r in rows:
            self.assertEqual((r['holdout']['alpha'], r['holdout']['trades']), (0.05 / 3, 34))
        # Changing only the TRAIN candidates cannot change the ranks.
        again = sh.run(self.population(34, [PATHS['rug'], PATHS['pump']]), self.grid(variants), holdout_from=T0 + 500)
        self.assertEqual([(r['variant'], r['rank']) for r in rows], [(r['variant'], r['rank']) for r in again['leaderboard']])
        # Too few holdout trades: listed, never ranked, with the reason; ranked variants come first.
        few = sh.run(self.population(12, [PATHS['flat']]), self.grid(variants), holdout_from=T0 + 500)
        self.assertEqual({r['rank'] for r in few['leaderboard']}, {None})
        self.assertTrue(all(r['rank_status'] == 'UNRANKED_FEWER_THAN_30_HOLDOUT_TRADES' for r in few['leaderboard']))
        lenient = sh.run(self.population(12, [PATHS['flat']]), self.grid(variants), holdout_from=T0 + 500, min_trades=10)
        self.assertEqual([r['rank'] for r in lenient['leaderboard']], [1, 2, 3])
        self.assertIn('minimum 30', result['ranking'])
        self.assertIn('Bonferroni', result['ranking'])

    def test_many_variants_do_not_collapse_the_confidence_interval(self):
        trades = [{'mint': f'm{i}', 'entry_at': i, 'cost_sol': Decimal('0.1'), 'pnl_lo': Decimal(str(-0.01 + 0.0004 * i)),
                   'pnl_hi': Decimal(str(0.01 + 0.0004 * i)), 'ret_lo': Decimal(str(-0.1 + 0.004 * i)), 'ret_hi': Decimal(str(0.1 + 0.004 * i)),
                   'ambiguous': True, 'exit_reasons': ['STOP']} for i in range(40)]
        s = sh.summarize(trades, name='v', n_variants=2000)
        low, high = s['bootstrap_ci']
        values = sorted(float(t['ret_lo']) for t in trades)
        self.assertGreater(low, values[0]); self.assertLess(low, sum(values) / len(values))
        self.assertEqual(s['bootstrap_resamples'], sh.bootstrap_size(0.05 / 2000))
        self.assertGreater(s['bootstrap_resamples'], sh.BOOTSTRAP)


class ParityTests(unittest.TestCase):
    """Replay RECORDED events of a ledger written by the real Ledger.apply and compare with the recorded decisions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.sqlite'
        self.cfg = helpers.config()
        ledger = Ledger(self.path)
        T = helpers.T
        events = [helpers.event(T), helpers.event(T + 5, reserve_sol='160'), helpers.event(T + 10, reserve_sol='260'),
                  helpers.event(T + 15, reserve_sol='40'), helpers.control(T + 20, 'PAUSE_ENTRY'), helpers.event(T + 25, mint='OTHER')]
        for e in events:
            ledger.apply(e, self.cfg, engine.transition, engine.initial_state)
        ledger.close()

    def recorded(self):
        with closing(sqlite3.connect(self.path)) as c:
            return [json.loads(p) for (p,) in c.execute('SELECT payload FROM outcomes ORDER BY seq')]

    def test_recorded_events_replay_to_the_recorded_decisions(self):
        outcomes = self.recorded()
        self.assertTrue(any(o.get('type') == 'fill' and o.get('side') == 'buy' for o in outcomes))      # the fixture is not trivial
        self.assertTrue(any(o.get('type') == 'fill' and o.get('side') == 'sell' for o in outcomes))
        self.assertTrue(any(o.get('type') == 'reject' for o in outcomes))
        result = sh.replay_ledger(self.path)
        self.assertEqual((result['events'], result['matched'], result['mismatches']), (6, 6, []))

    def test_a_changed_recorded_decision_is_a_mismatch(self):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('UPDATE outcomes SET payload=REPLACE(payload,?,?) WHERE payload LIKE ?',
                      ('"reason":"ENTRY"', '"reason":"FORGED"', '%"reason":"ENTRY"%'))
            c.commit()
        result = sh.replay_ledger(self.path)
        self.assertEqual(len(result['mismatches']), 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(sh.main(['--parity-ledger', str(self.path)]), 3)
        self.assertEqual(json.loads(out.getvalue())['matched'], 5)

    def test_a_different_config_changes_the_replayed_decisions(self):
        # The replay uses the ledger's SAVED config; a shadow variant is a different config and must not match.
        cfg = sh.build_config(self.cfg, {'stop_fraction': '0.60', 'min_market_cap_usd': '99999999'})
        state = engine.initial_state(cfg)
        decisions = [engine.transition(state, helpers.event(helpers.T), cfg)[1]]
        self.assertNotEqual([x.get('type') for x in decisions[0]], [o.get('type') for o in self.recorded()[:len(decisions[0])]])

    def test_cli_exit_codes_and_quote_mode_ledgers_are_refused(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(sh.main(['--parity-ledger', str(self.path)]), 0)
        self.assertEqual(json.loads(out.getvalue())['matched'], 6)
        with closing(sqlite3.connect(self.path)) as c:
            quote_cfg = {**self.cfg, 'paper_signal_policy_version': 3, 'paper_quote_execution_version': 1}
            c.execute("UPDATE metadata SET value=? WHERE key='config'", (json.dumps(quote_cfg),)); c.commit()
        with self.assertRaises(sh.ShadowError):
            sh.replay_ledger(self.path)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sh.main(['--parity-ledger', str(self.path)]), 2)
            self.assertEqual(sh.main(['--parity-ledger', str(Path(self.tmp.name) / 'missing.sqlite')]), 2)


class CliTests(StoreFixture):
    def test_cli_runs_end_to_end_and_prints_the_assumptions(self):
        for i in range(4):
            self.add(f'c{i}', self.full(PATHS['flat'] if i % 2 else PATHS['rug']))
        features = self.root / 'features.json'
        features.write_text(json.dumps({'c0': {'as_of': T0 + 300, 'flow': '55'}}))
        grid = self.root / 'grid.json'
        grid.write_text(json.dumps({'version': 'shadow-grid-1', 'base_config': 'config/paper.json', 'assumptions': {'max_intra_marks': 1},
                                    'variants': [{'name': 'live'}, {'name': 'tight', 'overrides': {'stop_fraction': '0.05'}}]}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(sh.main(['--store', str(self.store), '--grid', str(grid), '--features', str(features), '--json']), 0)
        parsed = json.loads(out.getvalue())
        self.assertEqual((parsed['label'], parsed['variants_tried'], parsed['selection']['evaluated'], parsed['features_supplied']),
                         ('SIMULATED_SHADOW', 2, 4, ['c0']))
        self.assertIn('bracket_assumption', parsed)
        text = io.StringIO()
        with contextlib.redirect_stdout(text):
            self.assertEqual(sh.main(['--store', str(self.store), '--grid', str(grid), '--no-outcomes']), 0)
        self.assertIn('bracket assumption:', text.getvalue())
        self.assertIn('selection:', text.getvalue())
        self.add('growing', [(300, 'OK', 80), (900, 'OK', 80)])                  # a path that is still being sampled
        incl = io.StringIO()
        with contextlib.redirect_stdout(incl):
            self.assertEqual(sh.main(['--store', str(self.store), '--grid', str(grid), '--include-truncated', '--json']), 0)
        self.assertEqual(json.loads(incl.getvalue())['selection']['truncated_included'], 1)
        excl = io.StringIO()
        with contextlib.redirect_stdout(excl):
            self.assertEqual(sh.main(['--store', str(self.store), '--grid', str(grid), '--json']), 0)
        self.assertEqual((json.loads(excl.getvalue())['selection']['truncated_paths'], json.loads(excl.getvalue())['selection']['evaluated']), (1, 4))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sh.main(['--store', str(self.root / 'missing.sqlite'), '--grid', str(grid)]), 2)
            self.assertEqual(sh.main(['--grid', str(grid)]), 2)
            bad_features = self.root / 'bad.json'; bad_features.write_text(json.dumps({'c0': {'flow': '1'}}))
            self.assertEqual(sh.main(['--store', str(self.store), '--grid', str(grid), '--features', str(bad_features)]), 2)


if __name__ == '__main__':
    unittest.main()
