"""SYNTHETIC_TEST_ONLY: in-memory paths through the REAL desk.engine; no provider, ledger or live store."""
import contextlib
import copy
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from desk import engine
from tools.research import counterfactual as cf
from tools.research import shadow_strategies as sh

ROOT = Path(__file__).resolve().parents[1]
BASE = json.loads((ROOT / 'config/paper.json').read_text())
A = dict(sh.DEFAULT_ASSUMPTIONS)
T0 = 1_000_000.0
HORIZONS = (300, 900, 1800, 3600, 7200, 21600)
# 2e8 tokens (raw 2e14 at 6 decimals) and 80 SOL: mcap ~ $60k, liquidity ~ $24k -> passes the live windows.
PATHS = {'rug': [80, 80, 40, 20, 10, 5], 'pump': [80, 100, 160, 200, 150, 120], 'flat': [80] * 6}


def candidate(quotes, mint='M1', migrated=T0, died=False, horizons=HORIZONS):
    return {'mint': mint, 'pool': 'P-' + mint, 'migrated_at': migrated, 'pool_died': died,
            'samples': [{'horizon': h, 'base_raw': 2 * 10 ** 14, 'quote_raw': int(q * 1e9)} for h, q in zip(horizons, quotes)]}


class ShadowTests(unittest.TestCase):
    def trade(self, quotes, model, cfg=None, **kw):
        return sh.simulate(candidate(quotes, **kw), cfg or BASE, A, model)

    # ------------------------------------------------------------ known answers
    def test_stop_known_answer_and_bracket(self):
        s = self.trade(PATHS['rug'], 'samples'); c = self.trade(PATHS['rug'], 'carry')
        for t in (s, c):
            self.assertEqual((t['exit_reason'], t['exit_at'], t['entry_at']), ('STOP', 1001800, 1000300))
        worst, best = s['pnl_sol'], s['pnl_best_sol']
        self.assertLess(worst, best); self.assertLess(best, 0)
        # Best case = a fill exactly at the tested stop level: -18% of the position cost (fee included in the mark).
        self.assertAlmostEqual(float(best / s['cost_sol']), -0.18, delta=0.01)
        bracket = sh.combine({'samples': [s], 'carry': [c]})[0]
        self.assertTrue(bracket['ambiguous']); self.assertEqual((bracket['pnl_lo'], bracket['pnl_hi']), (worst, best))

    def test_time_stop_only_the_carry_model_sees_the_deadline(self):
        s = self.trade(PATHS['flat'], 'samples'); c = self.trade(PATHS['flat'], 'carry')
        self.assertEqual((s['exit_reason'], s['exit_at']), ('TIME_STOP', 1003600))   # first sample after the deadline
        self.assertEqual((c['exit_reason'], c['exit_at']), ('TIME_STOP', 1000300 + BASE['time_stop_seconds']))
        self.assertEqual(s['pnl_sol'], c['pnl_sol'])                                  # flat price: same reserves, same cost
        self.assertTrue(Decimal('-0.0016') < s['pnl_sol'] < Decimal('-0.0013'))        # round-trip fee + slippage + pool fee only
        self.assertFalse(sh.combine({'samples': [s], 'carry': [c]})[0]['ambiguous'])

    def test_profitable_path_runs_to_horizon_end_through_engine_liquidate(self):
        t = self.trade(PATHS['pump'], 'samples')
        self.assertEqual((t['exit_reason'], t['horizon_end']), ('LIQUIDATE', True))
        self.assertGreater(t['pnl_sol'], 0)

    def test_pool_death_while_holding_loses_remaining_cost(self):
        t = self.trade([80, 80, 80], 'samples', died=True, horizons=HORIZONS[:3])   # flat: no ladder sale, no timer reached
        self.assertEqual((t['exit_reason'], t['horizon_end']), ('POOL_DEAD', True))
        self.assertEqual(t['return'], Decimal(-1))                                   # the whole position is lost
        partial = self.trade([80, 100, 160], 'samples', died=True, horizons=HORIZONS[:3])
        self.assertEqual(partial['exit_reason'], 'POOL_DEAD')
        self.assertTrue(Decimal(-1) < partial['return'] < 0)                          # the +40% ladder sale is kept, the rest is lost

    def test_rejected_candidates_report_the_engine_reason(self):
        r = sh.simulate(candidate([0.5] * 6), BASE, A, 'samples')   # liquidity far below the floor
        self.assertEqual((r['entered'], r['reject']), (False, 'MARKET_CAP'))
        cfg = sh.build_config(BASE, {'min_age_seconds': 900})
        t = sh.simulate(candidate(PATHS['flat']), cfg, A, 'samples')
        self.assertEqual(t['entry_at'], 1000900)                    # the age window moves the entry to the +15m sample
        danger = sh.simulate(candidate(PATHS['flat']), BASE, A, 'samples', {'danger': True})
        self.assertEqual((danger['entered'], danger['reject']), (False, 'DANGER'))

    # ---------------------------------------------------------- engine parity
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
                same = sh.simulate(c, sh.build_config(BASE, copy.deepcopy({k: BASE[k] for k in ('stop_fraction', 'trailing_fraction', 'time_stop_seconds')})), A, 'samples')
                self.assertEqual(same['pnl_sol'], trade['pnl_sol'])

    def test_every_decision_is_made_by_the_real_engine(self):
        calls = []
        real = engine.transition
        def spy(state, e, cfg, **kw):
            calls.append((e['kind'], e['ts'], e.get('provenance')))
            return real(state, e, cfg, **kw)
        with patch.object(engine, 'transition', spy):
            self.trade(PATHS['rug'], 'samples')
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual({p for k, _, p in calls if k == 'market'}, {'SYNTHETIC_TEST_ONLY'})

    def test_changed_parameters_change_the_engine_outcome(self):
        tight = self.trade(PATHS['rug'], 'samples', sh.build_config(BASE, {'stop_fraction': '0.05'}))
        loose = self.trade(PATHS['rug'], 'samples', sh.build_config(BASE, {'stop_fraction': '0.60'}))
        self.assertEqual(tight['exit_at'], 1001800)
        self.assertGreater(loose['exit_at'], tight['exit_at'])       # a 60% stop needs the later, deeper sample
        self.assertLess(loose['pnl_sol'], tight['pnl_sol'])

    # --------------------------------------------------------- no look-ahead
    def test_no_decision_uses_a_sample_taken_after_it(self):
        for model in sh.MODELS:
            for name, quotes in PATHS.items():
                with self.subTest(model=model, path=name):
                    c = candidate(quotes); points = sh._path_points(c)
                    seen = []
                    real = engine.transition
                    def spy(state, e, cfg, **kw):
                        if e['kind'] == 'market':
                            seen.append((e['ts'], e['reserve_sol']))
                        return real(state, e, cfg, **kw)
                    with patch.object(engine, 'transition', spy):
                        sh.simulate(c, BASE, A, model)
                    for ts, reserve in seen:
                        known = [p for p in points if p[0] <= ts]
                        if not known:
                            self.fail('event before any sample')
                        latest = known[-1]
                        if ts <= points[-1][0]:  # the horizon-end events repeat the final sample
                            self.assertEqual(Decimal(reserve), Decimal(latest[2]) / 10 ** 9, (ts, latest[0]))
                        else:
                            self.assertEqual(Decimal(reserve), Decimal(points[-1][2]) / 10 ** 9)

    def test_changing_the_future_cannot_change_an_earlier_exit(self):
        base = candidate(PATHS['rug'])
        tampered = copy.deepcopy(base)
        for s in tampered['samples'][3:]:
            s['quote_raw'] = 10 ** 15; s['base_raw'] = 1                  # absurd future after the +30m exit
        for model in sh.MODELS:
            a = sh.simulate(base, BASE, A, model); b = sh.simulate(tampered, BASE, A, model)
            for key in ('entry_at', 'exit_at', 'exit_reason', 'pnl_sol', 'pnl_best_sol'):
                self.assertEqual(a[key], b[key], (model, key))
        late = copy.deepcopy(base)
        late['samples'][1]['quote_raw'] = 10 ** 9                          # a change at +15m cannot move the +5m entry
        self.assertEqual(sh.simulate(base, BASE, A, 'samples')['entry_at'], sh.simulate(late, BASE, A, 'samples')['entry_at'])

    def test_timer_events_never_open_positions(self):
        cfg = sh.build_config(BASE, {'min_age_seconds': 21600})
        samples = sh.simulate(candidate(PATHS['flat']), cfg, A, 'samples'); carry = sh.simulate(candidate(PATHS['flat']), cfg, A, 'carry')
        self.assertEqual((samples['entered'], samples['entry_at']), (True, 1021600))    # only the +6h sample is old enough
        self.assertEqual(carry['entry_at'], samples['entry_at'])                        # the timer model never changes entries

    # ------------------------------------------------------------- statistics
    def fake(self, mint, entry_at, lo, hi, cost='0.1'):
        cost = Decimal(cost)
        return {'mint': mint, 'entry_at': entry_at, 'cost_sol': cost, 'pnl_lo': Decimal(lo), 'pnl_hi': Decimal(hi),
                'ret_lo': Decimal(lo) / cost, 'ret_hi': Decimal(hi) / cost, 'exit_reasons': ['STOP'], 'ambiguous': lo != hi}

    def test_summary_known_answers(self):
        trades = [self.fake('a', 1, '0.01', '0.01'), self.fake('b', 2, '-0.03', '-0.01'), self.fake('c', 3, '0.02', '0.02'),
                  self.fake('d', 4, '-0.02', '0.005')]
        s = sh.summarize(trades, name='x', n_variants=10)
        self.assertEqual(s['trades'], 4); self.assertEqual(s['ambiguous_trades'], 2)
        self.assertEqual(s['net_pnl_sol'], (Decimal('-0.02'), Decimal('0.025')))
        self.assertEqual(s['win_rate'], (Decimal('0.5'), Decimal('0.75')))
        self.assertEqual(s['mean_return'], (Decimal('-0.05'), Decimal('0.0625')))
        # worst-case equity path: +.01, -.02, 0, -.02 with running peak .01 -> deepest drop is -.03
        lo_series = [Decimal(x) for x in ('0.01', '-0.03', '0.02', '-0.02')]
        self.assertEqual(sh._max_drawdown(lo_series), Decimal('-0.03'))
        self.assertEqual(min(s['max_drawdown_sol']), Decimal('-0.03'))
        self.assertEqual(s['alpha'], 0.005)                                       # Bonferroni over ten variants
        self.assertEqual(s, sh.summarize(trades, name='x', n_variants=10))        # deterministic bootstrap
        low, high = s['bootstrap_ci']; self.assertLessEqual(low, high)
        self.assertEqual(sh.summarize([], name='x')['trades'], 0)

    def test_run_ranks_on_holdout_only_and_counts_comparisons(self):
        candidates = []
        for i in range(10):   # alternating crash / flat paths over time
            candidates.append(candidate(PATHS['rug'] if i % 2 else PATHS['flat'], mint=f'M{i}', migrated=T0 + i * 1000))
        grid = {'name': 'g', 'base': BASE, 'assumptions': A, 'variants': [
            {'name': 'live', 'overrides': {}}, {'name': 'tight', 'overrides': {'stop_fraction': '0.05'}},
            {'name': 'loose', 'overrides': {'stop_fraction': '0.60'}}]}
        result = sh.run(candidates, grid)
        self.assertEqual(result['label'], 'SIMULATED_SHADOW'); self.assertEqual(result['variants_tried'], 3)
        self.assertEqual(result['candidates'], {'train': 7, 'holdout': 3})
        self.assertIn('3 variants tried', result['multiple_comparisons']); self.assertIn('holdout-only', result['ranking'])
        lows = [r['holdout']['mean_return'][0] for r in result['leaderboard']]
        self.assertEqual(lows, sorted(lows, reverse=True))
        for r in result['leaderboard']:
            self.assertEqual(r['holdout']['alpha'], 0.05 / 3)
        # An explicit cut moves candidates between the parts; nothing is silently dropped.
        explicit = sh.run(candidates, grid, holdout_from=T0 + 5000)
        self.assertEqual(explicit['candidates'], {'train': 5, 'holdout': 5})
        self.assertIn('SIMULATED_SHADOW', sh.render(result))

    # ------------------------------------------------------------ grids/inputs
    def test_grid_expansion_and_fail_closed_validation(self):
        grid = sh.load_grid(ROOT / 'config/experiments/shadow/exit-grid.json')
        self.assertEqual(len(grid['variants']), 1 + 3 * 2 * 2)
        self.assertEqual(sh.load_grid(ROOT / 'config/experiments/shadow/live-baseline.json')['variants'][0]['overrides'], {})
        with tempfile.TemporaryDirectory() as d:
            def write(spec):
                p = Path(d) / 'g.json'; p.write_text(json.dumps({'version': 'shadow-grid-1', 'base_config': 'config/paper.json', **spec})); return p
            for bad in ({'variants': [{'name': 'x', 'overrides': {'no_such_key': 1}}]},
                        {'variants': [{'name': 'x', 'overrides': {'stop_fraction': 0.18}}]},                 # wrong type
                        {'variants': [{'name': 'x'}, {'name': 'x'}]},                                        # duplicate name
                        {'variants': []},
                        {'variants': [{'name': 'x'}], 'assumptions': {'surprise': 1}}):
                with self.subTest(bad=bad), self.assertRaises(sh.ShadowError):
                    sh.load_grid(write(bad))

    def test_loader_reads_only_priced_samples_and_never_writes(self):
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / 'cf.sqlite'; cf.init(store, now=T0)
            cf.add_candidate(store, mint='M1', pool='P1', signature='s', slot=1, migrated_at=T0, seq=1, now=T0)
            with closing(sqlite3.connect(store)) as c:
                rows = [(300, 'OK', '5', '10', '2'), (900, 'POOL_DEAD', None, None, None), (1800, 'MISSED', None, None, None),
                        (3600, 'FAILED', None, None, None)]
                for h, status, price, quote, base in rows:
                    c.execute('INSERT INTO samples(mint,horizon,due_at,sampled_at,base_raw,quote_raw,price,status) VALUES(?,?,?,?,?,?,?,?)',
                              ('M1', h, T0 + h, T0 + h, base, quote, price, status))
                c.commit()
            before = store.read_bytes()
            found = sh.load_candidates(store)
            self.assertEqual(store.read_bytes(), before)
            self.assertEqual([(x['mint'], x['pool_died'], [s['horizon'] for s in x['samples']]) for x in found], [('M1', True, [300])])

    def test_module_has_no_provider_ledger_or_write_path(self):
        source = Path(sh.__file__).read_text()
        for forbidden in ('urllib', 'provider_pacing', 'HELIUS', 'Ledger(', 'paper_cycle', 'INSERT ', 'UPDATE ', 'DELETE ', 'socket'):
            self.assertNotIn(forbidden, source)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / 'cf.sqlite'; cf.init(store, now=T0)
            for i, quotes in enumerate((PATHS['rug'], PATHS['flat'], PATHS['pump'])):
                cf.add_candidate(store, mint=f'M{i}', pool=f'P{i}', signature='s', slot=1, migrated_at=T0 + i * 100, seq=i + 1, now=T0)
                with closing(sqlite3.connect(store)) as c:
                    for h, q in zip(HORIZONS, quotes):
                        c.execute("INSERT INTO samples(mint,horizon,due_at,sampled_at,base_raw,quote_raw,price,status) VALUES(?,?,?,?,?,?,?,'OK')",
                                  (f'M{i}', h, T0 + h, T0 + h, str(2 * 10 ** 14), str(int(q * 1e9)), str(q / 2e14)))
                    c.commit()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(sh.main(['--store', str(store), '--grid', str(ROOT / 'config/experiments/shadow/live-baseline.json'), '--json']), 0)
            parsed = json.loads(out.getvalue())
            self.assertEqual((parsed['label'], parsed['variants_tried']), ('SIMULATED_SHADOW', 1))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sh.main(['--store', str(Path(d) / 'missing.sqlite'), '--grid', str(ROOT / 'config/experiments/shadow/live-baseline.json')]), 2)


if __name__ == '__main__':
    unittest.main()
