"""SYNTHETIC_TEST_ONLY: replay + entry-timing variants on a store produced by the REAL lean runner (L08B e2e).

Only the HTTP layer is faked (tests/lean/fakeworld.py). The real runner trades 7 fake tokens and writes the real store; the
price paths are then recorded exactly as L07 does (path_start raw = screen features, path_mark meta = price_sol / two-sided
liquidity_sol / status, from the pool's own reserves every 15 s) and the real lean.replay / lean.tune read that store.

What it proves
  calibration   replaying the default strategy on the paths of the tokens the runner actually traded reproduces the runner's
                ordered sell reasons (STOP, TP-TP-TP-TRAILING_STOP, TP-STOP, TIME_STOP, ...): the replay and the live loop agree
  hazard        the token the screen rejected for a hazard has no path and is never replayed
  timing        first_mark / pullback / momentum entries behave as specified on those real paths: the pullback variant enters
                the dump token at its dip and turns the live stop-out into a win; momentum enters only the ramp token; flat
                or stepped tokens never trigger a pullback / momentum and are recorded as ENTRY_TIMING_NO_TRIGGER
  tune          the grid is evaluated walk-forward and ranked out-of-sample, JSON-safe, without touching the store
"""
import hashlib
import json
import os
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from lean import replay as R, strategy as S, tune as T
from lean.__main__ import build_runner, load_config
from lean.store import Store
from tests.lean.fakeworld import FakeTime, T0, Token, World
from tests.lean.test_e2e_real import FLAT_105, KEYS, LADDER, STOP, TP1_THEN_STOP, steps

ROOT = Path(__file__).resolve().parents[2]
END = T0 + 5000
POOL_FEE_BPS = 25                       # fakeworld's pool fee; the runner config uses the same


def ramp(age):
    """+0.05% per second for 1000 s (a smooth climb to 1.5x), a plateau, then a drop to 0.9x."""
    return D(1) + D(min(age, 1000)) / D(2000) if age < 2400 else D('0.9')


DUMP_RALLY = steps((120, 0.8), (600, 1.2), (900, 1.7), (1200, 2.2), (1500, 2.8), (1800, 2.7), (2100, 1.2))

SCENARIO = [   # (role, Token kwargs, ready_at offset)
    ('stop', dict(path=STOP), 0),
    ('ladder', dict(path=LADDER), 120),
    ('tp1_stop', dict(path=TP1_THEN_STOP), 240),
    ('time', dict(path=FLAT_105), 360),
    ('hazard', dict(hazard='mint_authority'), 480),
    ('dump', dict(path=DUMP_RALLY), 720),     # the stop token has closed by now, so a slot is free
    ('ramp', dict(path=ramp), 1500),
]
EXPECTED_LIVE = {'stop': ['STOP'], 'ladder': ['TAKE_PROFIT'] * 3 + ['TRAILING_STOP'], 'tp1_stop': ['TAKE_PROFIT', 'STOP'],
                 'time': ['TIME_STOP'], 'ramp': ['TAKE_PROFIT', 'STOP'], 'dump': ['STOP']}


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class ReplayOnRealRunnerStoreTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(os.path.realpath(cls.tmp.name))
        clock = FakeTime(T0)
        cls.tokens = [Token(31 + 2 * i, **kw) for i, (_, kw, _) in enumerate(SCENARIO)]
        cls.role = {t.mint: role for t, (role, _, _) in zip(cls.tokens, SCENARIO)}
        world = World(cls.root, cls.tokens, clock)
        world.add_frames([T0 + off for _, _, off in SCENARIO])
        cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        runner = build_runner(cfg, state_dir=cls.root / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='e2e-replay',
                              clock=clock.time, transport_kwargs={'opener': world.opener, 'clock': clock.time, 'monotonic': clock.monotonic,
                                                                  'sleep': clock.sleep, 'rng': lambda: 0.5})
        tick = T0
        while tick <= END:
            if clock.time() < tick:
                clock.t = tick
            runner.position_pass()
            runner.candidate_pass()
            tick += 60
        assert runner.halted is None and runner.store.check_invariants() and not runner.store.positions()
        cls.strategy_cfg = runner.cfg
        cls.db = runner.store.path
        cls.live = {}
        for d in runner.store.rows('decisions', limit=10000):
            if d['kind'] == 'exit':
                cls.live.setdefault(cls.role[d['mint']], []).append(json.loads(d['reasons'])[0])
        screens = [d for d in runner.store.rows('decisions', limit=10000) if d['kind'] == 'screen' and d['action'] == 'PASS']
        runner.store.close()
        cls.record_paths(screens)
        cls.sha_before = hashlib.sha256(cls.db.read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def record_paths(cls, screens):
        """What L07 would have written for every candidate whose screen passed: path_start + a path_mark every 15 s."""
        store = Store(cls.db, code_version='e2e-replay', strategy_version='lean-1')
        by_mint = {t.mint: t for t in cls.tokens}
        for d in screens:
            token, features = by_mint[d['mint']], json.loads(d['features'])
            start = d['ts']
            store.add_observation('path_start', json.dumps({'mint': d['mint'], 'features': features, 'screen_passed': True, 'reason': None}).encode(),
                                  mint=d['mint'], ts=start, meta={'entered': True, 'reason': None, 'started': start})
            virtual, fee = int(features['virtual_quote_reserves_raw']), int(features['quote_gross_raw']) - int(features['quote_spendable_raw'])
            t = start
            while t <= END:
                base, quote = token.reserves(t)
                price = (D(quote + virtual) / 10**9) / (D(base) / D(10) ** int(features['decimals']))
                liquidity = 2 * D(max(0, quote - fee)) / 10**9
                body = {'ts': t, 'price_sol': format(price, 'f'), 'liquidity_sol': format(liquidity, 'f'), 'status': 'OK', 'n': 1}
                store.add_observation('path_mark', json.dumps(body).encode(), mint=d['mint'], ts=t, meta=body)
                t += 15
        store.close()

    # ------------------------------------------------------------------------------------------------------------
    def conn(self):
        import sqlite3
        c = sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True)
        self.addCleanup(c.close)
        return c

    def paths(self):
        paths, skipped = R.load_paths(self.conn())
        return {self.role[p.mint]: p for p in paths}, skipped

    def rcfg(self, **kw):
        return R.ReplayConfig(initial_cash_sol=R.store_initial_cash_sol(self.conn()), pool_fee_bps=POOL_FEE_BPS, **kw)

    def run_one(self, role, timing):
        paths, _ = self.paths()
        return R.replay([paths[role]], self.strategy_cfg, self.rcfg(entry_timing=timing))

    # ------------------------------------------------------------------------------------------------------------
    def test_the_live_runner_did_what_the_scenario_says(self):
        self.assertEqual(self.live, EXPECTED_LIVE)

    def test_paths_cover_exactly_the_candidates_that_passed_the_hazard_checks(self):
        paths, skipped = self.paths()
        self.assertEqual(set(paths), {'stop', 'ladder', 'tp1_stop', 'time', 'ramp', 'dump'})  # no path for the mint-authority hazard
        self.assertEqual(skipped, {})
        p = paths['dump']
        self.assertEqual((p.decimals, p.features['mint'] in self.role, p.features['reserve_sol']), (6, True, '80'))
        self.assertEqual(p.points[0].liquidity_sol, D(80))                 # recorded two-sided (160), replayed one-sided
        self.assertEqual(p.points[1].ts - p.points[0].ts, 15)

    def test_calibration_replay_reproduces_the_live_exit_reasons(self):
        cal = R.calibrate(self.conn(), self.strategy_cfg, self.rcfg())
        self.assertEqual((cal['status'], cal['tokens'], cal['matched']), ('OK', 6, 6), [r for r in cal['rows'] if not r['match']])
        self.assertEqual({self.role[r['mint']]: r['replay_reasons'] for r in cal['rows']}, EXPECTED_LIVE)
        self.assertEqual(cal['replay_assumptions']['initial_cash_sol'], '10')   # taken from the live store, not a default

    def test_defaults_start_from_the_cash_of_the_live_store(self):
        cal = R.calibrate(self.conn(), self.strategy_cfg)               # no replay config given
        self.assertEqual(cal['replay_assumptions']['initial_cash_sol'], '10')
        result = T.tune(self.db, {'stop_fraction': ['0.25']}, 0.7, base=self.strategy_cfg, min_oos=1, now=T0 + 10**6)
        self.assertEqual(result['replay_assumptions']['initial_cash_sol'], '10')

    def test_calibration_reports_a_mismatch_for_a_different_strategy(self):
        raw = T.config_to_dict(self.strategy_cfg)
        raw['tp_ladder'] = [{'trigger': t, 'fraction': f, 'stop_ratio': s} for t, f, s in (('5', '0.3', '1'), ('6', '0.3', '2'), ('7', '0.2', '3'))]
        other = S.StrategyConfig.from_dict(raw)               # a ladder nothing here reaches: ladder/ramp/tp1_stop must now differ
        cal = R.calibrate(self.conn(), other, self.rcfg())
        self.assertEqual(cal['status'], 'MISMATCH')
        self.assertGreaterEqual(cal['mismatched'], 3)
        self.assertEqual({self.role[r['mint']] for r in cal['rows'] if not r['match']} >= {'ladder', 'ramp', 'tp1_stop'}, True)
        self.assertIn('EXIT_REASONS_DIFFER', {r['note'] for r in cal['rows']})

    def test_pullback_enters_the_dump_at_its_dip_and_beats_the_live_stop_out(self):
        paths, _ = self.paths()
        p = paths['dump']
        first = self.run_one('dump', R.EntryTiming())
        pull = self.run_one('dump', R.EntryTiming('pullback', pullback_pct=D('0.15')))
        dip = next(pt.ts for pt in p.points if (max(q.price_sol for q in p.points if q.ts <= pt.ts) - pt.price_sol) / max(q.price_sol for q in p.points if q.ts <= pt.ts) >= D('0.15'))
        self.assertEqual(first.trades[0].entry_ts, p.start_ts)
        self.assertEqual(first.trades[0].exit_reason, 'STOP')
        self.assertEqual(pull.trades[0].entry_ts, dip)
        self.assertGreater(dip, p.start_ts)
        self.assertLess(first.trades[0].realized_lamports, 0)
        self.assertGreater(pull.trades[0].realized_lamports, 0)
        self.assertIn('TAKE_PROFIT', pull.trades[0].reasons)

    def test_momentum_enters_only_the_ramp(self):
        for role in ('stop', 'ladder', 'tp1_stop', 'time', 'dump'):
            r = self.run_one(role, R.EntryTiming('momentum', momentum_minutes=2, max_wait_s=900))
            self.assertEqual(r.trades, [], role)
            self.assertEqual([s['reasons'] for s in r.skipped_entries], [['ENTRY_TIMING_NO_TRIGGER']], role)
        paths, _ = self.paths()
        ramp_run = self.run_one('ramp', R.EntryTiming('momentum', momentum_minutes=2, max_wait_s=900))
        self.assertEqual(ramp_run.trades[0].entry_ts, paths['ramp'].start_ts + 120)

    def test_flat_and_stepped_tokens_never_pull_back(self):
        r = self.run_one('time', R.EntryTiming('pullback', pullback_pct=D('0.05')))
        self.assertEqual((r.trades, [s['reasons'] for s in r.skipped_entries]), ([], [['ENTRY_TIMING_NO_TRIGGER']]))

    def test_tune_grid_over_entry_timing_is_walk_forward_json_safe_and_read_only(self):
        grid = {'replay.entry_timing': ['first_mark', {'kind': 'pullback', 'pullback_pct': '0.15'}, {'kind': 'momentum', 'momentum_minutes': 2}]}
        result = T.tune(self.db, grid, 0.7, base=self.strategy_cfg, rcfg=self.rcfg(), min_oos=1, now=T0 + 10**6)
        json.dumps(result, allow_nan=False)
        self.assertEqual(result['split']['paths_total'], 6)
        self.assertEqual((result['split']['in_sample_paths'], result['split']['out_of_sample_paths']), (4, 2))
        self.assertEqual(sorted(c['label'] for c in result['configs']), ['base', 'entry_timing=momentum(2m)', 'entry_timing=pullback(0.15)'])
        self.assertEqual([c['rank'] for c in result['configs']], [1, 2, 3])
        self.assertEqual(result['replay_assumptions']['initial_cash_sol'], '10')
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), self.sha_before)   # the store was only read


if __name__ == '__main__':
    unittest.main()
