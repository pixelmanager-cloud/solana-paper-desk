"""SYNTHETIC_TEST_ONLY: replay + entry-timing variants on a store produced by the REAL lean runner (L08B e2e).

Only the HTTP layer is faked (tests/lean/fakeworld.py). The real runner trades 8 fake tokens and writes the real store while the
REAL L07R2 recorder (lean.paths) writes its own paths.sqlite from the pool vaults every 15 s; the real lean.replay / lean.tune then
read that recorder output, not hand-built rows.

What it proves
  calibration   replaying the default strategy on the paths of the tokens the runner actually traded reproduces the runner's
                ordered sell reasons (STOP, TP-TP-TP-TRAILING_STOP, TP-STOP, TIME_STOP, ...): the replay and the live loop agree
  hazard        the token the screen rejected for a hazard has no path and is never replayed
  timing        first_mark / pullback / momentum entries behave as specified on those real paths: the pullback variant enters
                the dump token at its dip and turns the live stop-out into a win; momentum enters only the ramp token; flat
                or stepped tokens never trigger a pullback / momentum and are recorded as ENTRY_TIMING_NO_TRIGGER
  marks         replay marks exactly like the live position loop (adapters.mark on the stored vault amounts, no extra haircut): the
                `edge` token sits 0.2% above the stop line, so a mark model with a 50 bps haircut flips its calibration row
  fills         pullback / momentum entries fill at the NEXT recorded mark, never at the trigger mark
  tune          train / select / confirm, ranked on select only, JSON-safe, without touching either database
"""
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from lean import adapters as A, paths as L, replay as R, strategy as S, tune as T
from lean.__main__ import build_runner, load_config
from lean.store import Store
from tests.lean.fakeworld import FakeTime, T0, Token, World
from tests.lean.test_e2e_real import FLAT_105, KEYS, LADDER, STOP, TP1_THEN_STOP, steps

ROOT = Path(__file__).resolve().parents[2]
END = T0 + 6500
POOL_FEE_BPS = 25                       # fakeworld's pool fee; the runner config uses the same


def ramp(age):
    """+0.05% per second for 1000 s (a smooth climb to 1.5x), a plateau, then a drop to 0.9x."""
    return D(1) + D(min(age, 1000)) / D(2000) if age < 2400 else D('0.9')


DUMP_RALLY = steps((120, 0.8), (600, 1.2), (900, 1.7), (1200, 2.2), (1500, 2.8), (1800, 2.7), (2100, 1.2))

EDGE = steps((300, 0.833))   # net mark 0.2% above the 18% stop line: only a mark with an extra 50 bps haircut would stop it out
SCENARIO = [   # (role, Token kwargs, ready_at offset)
    ('stop', dict(path=STOP), 0),
    ('ladder', dict(path=LADDER), 120),
    ('tp1_stop', dict(path=TP1_THEN_STOP), 240),
    ('time', dict(path=FLAT_105), 360),
    ('hazard', dict(hazard='mint_authority'), 480),
    ('dump', dict(path=DUMP_RALLY, quote_sol=100), 720),   # a deeper pool: market cap 75k at the start, 60k at the dip     # the stop token has closed by now, so a slot is free
    ('ramp', dict(path=ramp), 1500),
    ('edge', dict(path=EDGE), 2800),
]
EXPECTED_LIVE = {'edge': ['TIME_STOP'], 'stop': ['STOP'], 'ladder': ['TAKE_PROFIT'] * 3 + ['TRAILING_STOP'], 'tp1_stop': ['TAKE_PROFIT', 'STOP'],
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
        cls.token = {role: t for t, (role, _, _) in zip(cls.tokens, SCENARIO)}
        world = World(cls.root, cls.tokens, clock)
        world.add_frames([T0 + off for _, _, off in SCENARIO])
        cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        assert cfg['paths']['enabled'] and cfg['paths']['interval_s'] == 15
        # replay models instant paper fills at the recorded vault amounts: the LINT1 example's latency-aware fills (L10) and rug handling (L11)
        # are separate cost/exit models that replay does not reproduce, so they are off here (a documented replay limitation)
        cfg['execution'], cfg['held_risk'] = None, {'enabled': False}
        runner = build_runner(cfg, state_dir=cls.root / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='e2e-replay',
                              clock=clock.time, transport_kwargs={'opener': world.opener, 'clock': clock.time, 'monotonic': clock.monotonic,
                                                                  'sleep': clock.sleep, 'rng': lambda: 0.5})
        runner.l07r_start_paths()                      # what run() does before its loops start
        tick = T0
        while tick <= END:
            for sub in (15, 30, 45):                   # the recorder thread polls on the low lane between the trader's 60 s passes
                if clock.time() < tick - 60 + sub:
                    clock.t = tick - 60 + sub
                    runner.paths.poll()
            if clock.time() < tick:
                clock.t = tick
            runner.paths.poll()
            runner.position_pass()
            runner.candidate_pass()
            tick += 60
        assert runner.halted is None and runner.store.check_invariants() and not runner.store.positions()
        cls.strategy_cfg = runner.cfg
        cls.db = runner.store.path
        cls.paths_db = runner.paths.db.path
        cls.live = {}
        for d in runner.store.rows('decisions', limit=10000):
            if d['kind'] == 'exit':
                cls.live.setdefault(cls.role[d['mint']], []).append(json.loads(d['reasons'])[0])
        runner.store.close()
        runner.paths.db.close()
        cls.sha_before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (cls.db, cls.paths_db)}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # ------------------------------------------------------------------------------------------------------------
    def conn(self, path=None):
        c = sqlite3.connect((path or self.db).as_uri() + '?mode=ro', uri=True)
        self.addCleanup(c.close)
        return c

    def paths(self):
        paths, skipped = R.load_paths(self.conn(self.paths_db))
        return {self.role[p.mint]: p for p in paths}, skipped

    def rcfg(self, **kw):
        return R.ReplayConfig(initial_cash_sol=R.store_initial_cash_sol(self.conn()), pool_fee_bps=POOL_FEE_BPS, **kw)

    def run_one(self, role, timing):
        paths, _ = self.paths()
        return R.replay([paths[role]], self.strategy_cfg, self.rcfg(entry_timing=timing))

    def calibrate(self, cfg=None, rcfg=None):
        return R.calibrate(self.conn(), [self.conn(self.paths_db)], cfg or self.strategy_cfg, rcfg or self.rcfg())

    # ------------------------------------------------------------------------------------------------------------
    def test_the_live_runner_did_what_the_scenario_says(self):
        self.assertEqual(self.live, EXPECTED_LIVE)

    def test_the_trader_store_holds_no_path_rows_and_the_recorder_file_is_separate(self):
        self.assertNotEqual(self.db, self.paths_db)
        kinds = {r[0] for r in self.conn().execute('SELECT DISTINCT kind FROM observations')}
        self.assertFalse({k for k in kinds if k.startswith('path_')})

    def test_paths_cover_exactly_the_candidates_that_passed_the_hazard_checks(self):
        paths, skipped = self.paths()
        self.assertEqual(set(paths), {'stop', 'ladder', 'tp1_stop', 'time', 'ramp', 'dump', 'edge'})  # no path for the mint-authority hazard
        self.assertEqual(skipped, {})
        p = paths['dump']
        token = self.token['dump']
        self.assertEqual((p.decimals, p.features['mint'] in self.role, p.features['reserve_sol']), (6, True, '100'))
        self.assertEqual((p.points[0].base_raw, p.points[0].quote_raw), (token.base_raw, token.quote_lamports))   # the pool's own vaults
        self.assertEqual(p.points[1].ts - p.points[0].ts, 15)
        later = next(q for q in p.points if q.ts - p.start_ts >= 120)
        self.assertEqual(later.quote_raw, int(token.quote_lamports * D('0.8')))
        self.assertGreater(p.supply_raw, 0)

    def test_calibration_replay_reproduces_the_live_exit_reasons_on_the_recorder_output(self):
        cal = self.calibrate()
        self.assertEqual((cal['status'], cal['tokens'], cal['matched']), ('OK', 7, 7), [r for r in cal['rows'] if not r['match']])
        self.assertEqual({self.role[r['mint']]: r['replay_reasons'] for r in cal['rows']}, EXPECTED_LIVE)
        self.assertEqual(cal['replay_assumptions']['initial_cash_sol'], '10')   # taken from the live store, not a default

    def test_calibration_is_sensitive_to_the_mark_model(self):
        """The edge token's net mark is 0.2% above the stop line. Replay with an extra haircut on the mark (the old divergence) stops
        it out; the live runner did not, so the calibration must report exactly that token."""
        real = A.mark

        def haircut(*args, **kwargs):
            return real(*args, **kwargs) * D('0.995')
        with mock.patch.object(R.A, 'mark', haircut):
            cal = self.calibrate()
        bad = [r for r in cal['rows'] if not r['match']]
        self.assertEqual((cal['status'], [self.role[r['mint']] for r in bad]), ('MISMATCH', ['edge']))
        self.assertEqual((bad[0]['live_reasons'], bad[0]['replay_reasons'], bad[0]['note']), (['TIME_STOP'], ['STOP'], 'EXIT_REASONS_DIFFER'))

    def test_defaults_start_from_the_cash_of_the_live_store(self):
        cal = R.calibrate(self.conn(), [self.conn(self.paths_db)], self.strategy_cfg)               # no replay config given
        self.assertEqual(cal['replay_assumptions']['initial_cash_sol'], '10')
        result = T.tune(self.db, self.paths_db, {'stop_fraction': ['0.25']}, base=self.strategy_cfg, min_select=1, min_confirm=1, now=T0 + 10**6)
        self.assertEqual(result['replay_assumptions']['initial_cash_sol'], '10')

    def test_calibration_reports_a_mismatch_for_a_different_strategy(self):
        raw = T.config_to_dict(self.strategy_cfg)
        raw['tp_ladder'] = [{'trigger': t, 'fraction': f, 'stop_ratio': s} for t, f, s in (('5', '0.3', '1'), ('6', '0.3', '2'), ('7', '0.2', '3'))]
        other = S.StrategyConfig.from_dict(raw)               # a ladder nothing here reaches: ladder/ramp/tp1_stop must now differ
        cal = self.calibrate(other)
        self.assertEqual(cal['status'], 'MISMATCH')
        self.assertGreaterEqual(cal['mismatched'], 3)
        self.assertEqual({self.role[r['mint']] for r in cal['rows'] if not r['match']} >= {'ladder', 'ramp', 'tp1_stop'}, True)
        self.assertIn('EXIT_REASONS_DIFFER', {r['note'] for r in cal['rows']})

    def test_pullback_fills_at_the_next_mark_after_the_dip_and_beats_the_live_stop_out(self):
        paths, _ = self.paths()
        p = paths['dump']
        first = self.run_one('dump', R.EntryTiming())
        pull = self.run_one('dump', R.EntryTiming('pullback', pullback_pct=D('0.15')))
        high = lambda ts: max(q.price for q in p.points if q.ts <= ts)
        dip = next(i for i, pt in enumerate(p.points) if (high(pt.ts) - pt.price) / high(pt.ts) >= D('0.15'))
        self.assertEqual(first.trades[0].entry_ts, p.points[0].ts)   # the first recorded mark
        self.assertEqual(first.trades[0].exit_reason, 'STOP')
        self.assertEqual(pull.trades[0].entry_ts, p.points[dip + 1].ts)      # the trigger mark is never the fill mark
        self.assertGreater(p.points[dip + 1].ts, p.points[dip].ts)
        self.assertLess(first.trades[0].realized_lamports, 0)
        self.assertGreater(pull.trades[0].realized_lamports, 0)
        self.assertIn('TAKE_PROFIT', pull.trades[0].reasons)

    def test_the_market_cap_band_is_judged_at_the_entry_mark_not_at_the_screen(self):
        paths, _ = self.paths()
        raw = T.config_to_dict(self.strategy_cfg)
        raw['min_market_cap_usd'] = '70000'        # 75k at the screen, 60k at the pullback entry
        strict = S.StrategyConfig.from_dict(raw)
        rc = self.rcfg(entry_timing=R.EntryTiming('pullback', pullback_pct=D('0.15')))
        early = R.replay([paths['dump']], strict, self.rcfg())
        late = R.replay([paths['dump']], strict, rc)
        self.assertEqual(len(early.trades), 1)
        self.assertEqual((late.trades, [s['reasons'] for s in late.skipped_entries]), ([], [['MARKET_CAP']]))

    def test_momentum_enters_only_the_ramp_and_fills_at_the_next_mark(self):
        for role in ('stop', 'ladder', 'tp1_stop', 'time', 'dump', 'edge'):
            r = self.run_one(role, R.EntryTiming('momentum', momentum_minutes=2, max_wait_s=900))
            self.assertEqual(r.trades, [], role)
            self.assertEqual([s['reasons'] for s in r.skipped_entries], [['ENTRY_TIMING_NO_TRIGGER']], role)
        paths, _ = self.paths()
        ramp_run = self.run_one('ramp', R.EntryTiming('momentum', momentum_minutes=2, max_wait_s=900))
        self.assertEqual(ramp_run.trades[0].entry_ts, paths['ramp'].points[0].ts + 120 + 15)   # triggered at +120 s, filled at the mark after it

    def test_flat_and_stepped_tokens_never_pull_back(self):
        r = self.run_one('time', R.EntryTiming('pullback', pullback_pct=D('0.05')))
        self.assertEqual((r.trades, [s['reasons'] for s in r.skipped_entries]), ([], [['ENTRY_TIMING_NO_TRIGGER']]))

    def test_tune_is_three_way_selects_on_select_only_json_safe_and_read_only(self):
        grid = {'replay.entry_timing': ['first_mark', {'kind': 'pullback', 'pullback_pct': '0.15'}, {'kind': 'momentum', 'momentum_minutes': 2}]}
        result = T.tune(self.db, self.paths_db, grid, base=self.strategy_cfg, rcfg=self.rcfg(), min_select=1, min_confirm=1, now=T0 + 10**6)
        json.dumps(result, allow_nan=False)
        self.assertEqual(result['split']['paths_total'], 7)
        self.assertEqual((result['split']['train_paths'], result['split']['select_paths'], result['split']['confirm_paths']), (3, 2, 2))
        self.assertEqual(sorted(c['label'] for c in result['configs']), ['base', 'entry_timing=momentum(2m)', 'entry_timing=pullback(0.15)'])
        self.assertEqual([c['rank'] for c in result['configs']], [1, 2, 3])
        self.assertTrue(all(c['confirm_used_for_selection'] is False for c in result['configs']))
        self.assertEqual(result['replay_assumptions']['initial_cash_sol'], '10')
        for path, digest in self.sha_before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)   # both databases were only read


if __name__ == '__main__':
    unittest.main()
