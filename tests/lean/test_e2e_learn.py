"""SYNTHETIC_TEST_ONLY: the selection learner (L19, LINT3) end to end on the REAL runner, the REAL L12F feature recorder (v3 rows with
decision_ts / late_s / flags) and the REAL L07R2 path recorder (lean/paths.py, its own paths.sqlite). Only HTTP is faked
(tests/lean/fakeworld.py; the dev wallet balances and the pool histories are served through its ``extra_rpc`` hook).

Worlds of 100 launches, one per 240 s, each with a dev wallet holding a different share of the supply:
  PLANTED  a launch whose dev holds more than 15 % usually rugs (the price collapses a minute after the entry); the others are winners
           (ladder to 3.2x) or small losers (-25 % stop). The tx count of the pool's first 10 minutes is independent noise.
  NOISE    the same dev shares and tx counts, but the outcome is drawn independently of every feature.
  LOSING   NOISE with a losing baseline (like the live run: every window loses on average): mostly rugs and stop losses, rare winners,
           independent of every feature. The pre-M2 learner (rejected PnL t-tested against 0) CONFIRMED a rule here; it must not.
  LATE     the PLANTED world, but the feature recorder reads the dev balance a minute AFTER the decision (an AT_COLLECTION snapshot,
           late_s ~ 60 s): the planted signal exists only after the decision, so the learner must NOT find it (and does, if the
           lookahead guard is relaxed: the test is sensitive).
The runner trades them all (the daily stops are widened so that the rugs do not halt it), the recorders write ``lean.sqlite`` and
``paths.sqlite``, and ``lean.learn`` reads those two files (mode=ro) and nothing else.
"""
import contextlib
import hashlib
import io
import json
import os
import random
import shutil
import socket
import sqlite3
import tempfile
import types
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from lean import adapters as A, features as F, labels as LB, learn as LN, paths as L, providers, strategy as S
from lean.__main__ import build_runner, load_config
from lean.store import Store
from tests.lean.fakeworld import FakeTime, SUPPLY, T0, Token, World
from tests.lean.test_e2e_features import BLOCK_BEFORE_RECEIPT, grad_slot
from tests.lean.test_e2e_real import steps

ROOT = Path(__file__).resolve().parents[2]
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}
N = 100
SPACING = 240
END = T0 + N * SPACING + 1500
PATHS = {'RUG': steps((60, 0.15)), 'WIN': steps((120, 1.5), (240, 2.2), (360, 3.2), (480, 1.8)), 'LOSS': steps((120, 0.75))}
NAMES = ('dev_holding_pct', 'tx_count_first_10m', 'market_cap_usd')
MIN = dict(min_n=10, min_confirm=6, min_bucket=5)


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def plan(planted, seed, losing=False):
    rng = random.Random(seed)
    out = []
    for i in range(N):
        dev = round(rng.uniform(0.5, 30), 1)
        tx = rng.randint(3, 60)
        if planted:
            kind = 'RUG' if (dev > 15 and rng.random() < 0.97) or (dev <= 15 and rng.random() < 0.03) else rng.choice(('WIN', 'WIN', 'LOSS'))
        elif losing:
            kind = rng.choices(('RUG', 'LOSS', 'WIN'), weights=(0.45, 0.5, 0.05))[0]
        else:
            kind = rng.choice(('RUG', 'WIN', 'LOSS'))
        out.append({'dev': dev, 'tx': tx, 'kind': kind})
    return out


class World_:
    """One simulated run: the runner + both recorders over the whole scenario, then the files closed."""

    def __init__(self, root, planted, seed, late=False, losing=False):
        self.root, self.plan = root, plan(planted, seed, losing)
        clock = FakeTime(T0)
        tokens = [Token(10 + i, coin_creator_tag=120 + i, path=PATHS[p['kind']]) for i, p in enumerate(self.plan)]
        self.role = {t.mint: i for i, t in enumerate(tokens)}
        world = World(root, tokens, clock)
        offsets = [i * SPACING for i in range(N)]
        world.add_frames([T0 + off for off in offsets])
        slot = grad_slot(world)
        pools = {t.pool: (i, T0 + off - 300 - BLOCK_BEFORE_RECEIPT) for i, (t, off) in enumerate(zip(tokens, offsets))}
        creators = {t.coin_creator: i for i, t in enumerate(tokens)}

        def signatures(params, t):
            pool, options = params
            i, born = pools[pool]
            born = int(born)
            sigs = [{'signature': 'create', 'slot': slot, 'blockTime': born, 'err': None}]
            sigs += [{'signature': 't%d' % k, 'slot': slot + 1 + k, 'blockTime': born + 1 + k, 'err': None} for k in range(self.plan[i]['tx'])]
            return list(reversed(sigs))

        def owner_accounts(params, t):
            units = int(D(str(self.plan[creators[params[0]]]['dev'])) / 100 * SUPPLY)
            return {'context': {'slot': 1}, 'value': [{'account': {'data': {'parsed': {'info': {'tokenAmount': {'amount': str(units)}}}}}}]}
        world.extra_rpc = {'getSignaturesForAddress': signatures, 'getTokenAccountsByOwner': owner_accounts}
        strategy = json.loads((ROOT / 'config' / 'lean' / 'strategy-default.json').read_text())
        strategy.update(daily_loss_stop_fraction='0.9', daily_liquidate_fraction='0.95', loss_streak_pause_at=100)
        (root / 'strategy.json').write_text(json.dumps(strategy))
        cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        cfg['strategy_config'] = str(root / 'strategy.json')
        cfg['execution'], cfg['held_risk'], cfg['wallet_signals'] = None, {'enabled': False}, None
        from lean import flow, sell_evidence                                                    # L20: the shipped example turns them ON
        cfg['sell_evidence'], cfg['flow'] = sell_evidence.config(None), flow.config(None)
        cfg['paths'] = L.config({'enabled': True, 'path_hours': 0.5, 'interval_s': 15})
        cfg['features'] = F.config({'enabled': True, 'max_late_s': 3600 if late else 30})     # LATE: the recorder still reads it
        cfg['lanes'] = {'shares': providers.validate_lane_shares({'main': 0.5, 'exit': 0.2, 'low': 0.3}), 'low_shed_s': 1}
        self.pool_fee_bps = cfg['pool_fee_bps']
        r = build_runner(cfg, state_dir=root / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='e2e-learn',
                         clock=clock.time, transport_kwargs={'opener': world.opener, 'clock': clock.time, 'monotonic': clock.monotonic,
                                                             'sleep': clock.sleep, 'rng': lambda: 0.5})
        r.l07r_start_paths()

        def drain():
            while True:
                clock.t += 2.0                                    # the feature recorder's thread: one candidate per low-lane refill
                if not r.features.process_one():
                    break
        tick = T0
        while tick <= END:
            for sub in (15, 30, 45):
                if clock.time() < tick - 60 + sub:
                    clock.t = tick - 60 + sub
                    r.paths.poll()
            if clock.time() < tick:
                clock.t = tick
            r.paths.poll()
            if late:
                drain()                                           # LATE: last tick's candidates, a minute after their decision
            r.position_pass()
            r.candidate_pass()
            if not late:
                drain()
            assert r.halted is None, r.halted
            tick += 60
        drain()
        assert r.store.check_invariants()
        self.fills = r.store.rows('fills', limit=100000)
        self.sells = {}
        for d in r.store.rows('decisions', kind='exit', limit=100000):
            if d['action'] == 'SELL':
                self.sells.setdefault(d['mint'], []).append(json.loads(d['reasons'])[0])
        self.db, self.paths_db = r.store.path, r.paths.db.path
        r.store.close()
        r.paths.db.close()
        self.strategy_path = root / 'strategy.json'


class E2ELearn(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(cls.tmp.name))
        cls.worlds = {}
        for name, planted, seed, late, losing in (('planted', True, 7, False, False), ('noise', False, 11, False, False),
                                                  ('late', True, 7, True, False), ('losing', False, 13, False, True)):
            root = base / name
            root.mkdir()
            cls.worlds[name] = World_(root, planted, seed, late, losing)
        cls.strategy = S.StrategyConfig.load(cls.worlds['planted'].strategy_path)
        cls.sha = {n: {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (w.db, w.paths_db)} for n, w in cls.worlds.items()}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def dataset(self, name, **kw):
        w = self.worlds[name]
        kw.setdefault('pool_fee_bps', w.pool_fee_bps)
        return LN.build_dataset(w.db, w.paths_db, self.strategy, **kw)

    def run_learn(self, name, ds, **kw):
        props = Path(self.worlds[name].root) / ('proposals-%d' % len(list(Path(self.worlds[name].root).glob('proposals-*'))))
        return LN.learn(ds.rows, strategy=self.strategy, now=T0 + 10 ** 6, proposals_dir=props, dataset_stats=ds.stats,
                        feature_names=NAMES, **dict(MIN, **kw))

    def mint_of(self, name, i):
        w = self.worlds[name]
        return next(m for m, k in w.role.items() if k == i)

    # ------------------------------------------------------------------------------------------------------------ labels
    def test_every_candidate_has_a_path_and_the_label_of_the_family_that_made_it(self):
        for name in ('planted', 'noise'):
            w, ds = self.worlds[name], self.dataset(name)
            self.assertEqual(ds.stats['paths'], N, ds.stats)
            self.assertEqual(ds.stats['with_features'], N, ds.stats['skipped'])
            self.assertEqual(ds.stats['enterable'], N)
            self.assertEqual(sum(1 for f in w.fills if f['side'] == 'buy'), N)         # the runner really traded every one of them
            for row in ds.rows:
                self.assertEqual(row.label.label, w.plan[w.role[row.mint]]['kind'], (name, row.mint))
                self.assertEqual(row.label.label_version, LB.LABEL_VERSION)
                self.assertLessEqual(row.label.max_hole_s, 60, row.label)               # 15 s marks: the path is fully observed
                self.assertEqual(row.label.gap_fraction, 0.0)

    def test_labels_start_at_the_trader_decision_read_from_the_store(self):
        w, ds = self.worlds['planted'], self.dataset('planted')
        c = sqlite3.connect(w.db.as_uri() + '?mode=ro', uri=True)
        self.addCleanup(c.close)
        p = sqlite3.connect(w.paths_db.as_uri() + '?mode=ro', uri=True)
        self.addCleanup(p.close)
        starts = dict(p.execute('SELECT mint, start_ts FROM paths'))
        for row in ds.rows:
            entries = [ts for (ts,) in c.execute("SELECT ts FROM decisions WHERE mint=? AND kind='entry' ORDER BY id", (row.mint,))]
            self.assertEqual(row.ts, entries[-1])                                        # entered: the entry decision
            self.assertLessEqual(row.ts, starts[row.mint])                               # the path starts after it
            self.assertEqual(row.label.decision_ts, row.ts)

    def test_live_exit_pnl_reproduces_the_live_trader(self):
        """The minimal replay (strategy.exit_decision on every recorded mark) gives the SAME ordered sell reasons as the live runner did
        on the same tokens, and the families lose / win as built."""
        w, ds = self.worlds['planted'], self.dataset('planted')
        by = {}
        for row in ds.rows:
            by.setdefault(row.label.label, []).append(row.pnl_lamports)
        self.assertLess(max(by['RUG']), 0)
        self.assertGreater(min(by['WIN']), 0)
        self.assertLess(max(by['LOSS']), 0)
        self.assertLess(sum(by['RUG']) / len(by['RUG']), sum(by['LOSS']) / len(by['LOSS']))
        pcfg = A.paper_config(self.strategy)
        c = sqlite3.connect(w.paths_db.as_uri() + '?mode=ro', uri=True)
        self.addCleanup(c.close)
        paths = {p.mint: p for p in LB.load_paths(c)[0]}
        matched = 0
        for row in ds.rows:
            path = paths[row.mint]
            trade = LB.simulate_exits(path, row.ts, LB.reference(path, pcfg), self.strategy, pool_fee_bps=w.pool_fee_bps, pcfg=pcfg)
            self.assertEqual(trade.reasons, w.sells[row.mint], (row.mint, row.label.label))
            matched += 1
        self.assertEqual(matched, N)

    # ------------------------------------------------------------------------------------------------------------ features
    def test_features_are_what_the_recorder_saw_at_the_decision(self):
        w, ds = self.worlds['planted'], self.dataset('planted')
        for row in ds.rows:
            p = w.plan[w.role[row.mint]]
            self.assertEqual(row.features['dev_holding_pct'], D(str(p['dev'])).quantize(D('0.0001')), row.mint)
            self.assertEqual(row.features['tx_count_first_10m'], D(p['tx']), row.mint)
            self.assertIsNotNone(row.features['market_cap_usd'])
            for name in LN.WALLET_FEATURES:                                              # the collector is off in this world
                self.assertIsNone(row.features[name])
        self.assertEqual(ds.stats['skipped'], {})
        self.assertEqual(ds.stats['excluded'], {})

    def test_a_feature_row_written_after_the_decision_is_never_used(self):
        """Rows built by the REAL row builder (lean.features.build_row) and written exactly as FeatureRecorder._write writes them: one
        with a later decision_ts, one with a perfectly predictive dev share read 120 s after the decision (AT_COLLECTION). Neither
        changes anything; with the guard relaxed the snapshot IS read (so this test would see a leak)."""
        w = self.worlds['planted']
        target = self.mint_of('planted', 5)
        clean = {r.mint: r for r in self.dataset('planted').rows}[target]
        copy = Path(w.root / 'leak.sqlite')
        shutil.copy(w.db, copy)
        shutil.copy(w.paths_db, w.root / 'leak-paths.sqlite')
        store = Store(copy, code_version='e2e-learn', strategy_version='lean-1')
        cid = store.db.execute('SELECT candidate_id FROM decisions WHERE mint=? AND candidate_id IS NOT NULL', (target,)).fetchone()[0]
        candidate = types.SimpleNamespace(mint=target)
        screen = types.SimpleNamespace(passed=True, reasons=(), features={'market_cap_usd': '1', 'liquidity_usd': '1'})
        decision = clean.ts
        for decision_ts, collected_at, dev in ((decision + 30, decision + 31, '99'), (decision, decision + 120, '98')):
            payload = F.build_row(candidate, screen, entered=True, extra={'dev_holding_pct': dev}, screen_at=decision_ts,
                                  collected_at=collected_at, decision_ts=decision_ts, flags={'dev_holding_pct': 'AT_COLLECTION'})
            store.add_observation(F.KIND, F.canonical(payload), mint=target, candidate_id=cid, meta=payload, ts=collected_at)
        store.close()
        ds = LN.build_dataset(copy, w.root / 'leak-paths.sqlite', self.strategy, pool_fee_bps=w.pool_fee_bps)
        leaked = {r.mint: r for r in ds.rows}[target]
        self.assertNotIn(leaked.features['dev_holding_pct'], (D(99), D(98)))
        self.assertIsNone(leaked.features['dev_holding_pct'])         # the LAST row at the decision is the late one: its snapshot is out
        self.assertEqual(ds.stats['skipped'].get('FEATURES_AFTER_DECISION'), 1)
        self.assertEqual(ds.stats['excluded'], {'dev_holding_pct': {'AT_COLLECTION_LATE': 1}})
        lax = LN.build_dataset(copy, w.root / 'leak-paths.sqlite', self.strategy, pool_fee_bps=w.pool_fee_bps,
                               policy=LN.LookaheadPolicy(max_late_s=10 ** 6))
        self.assertEqual({r.mint: r for r in lax.rows}[target].features['dev_holding_pct'], D(98))

    # ------------------------------------------------------------------------------------------------------------ learning
    def test_the_planted_rule_is_recovered_selected_and_confirmed(self):
        ds = self.dataset('planted')
        r = self.run_learn('planted', ds)
        self.assertEqual(r['proposal_status'], 'CONFIRMED', r['candidates'][:3])
        first = r['selected'][0]['if'][0]
        self.assertEqual((first['feature'], first['op']), ('dev_holding_pct', '>'))
        self.assertTrue(8 <= D(first['value']) <= 20, first)
        conf = r['union']['confirm']
        self.assertGreater(conf['gain_per_candidate_sol'], 0)
        self.assertGreater(conf['kept_mean_pnl_sol'], 0)                             # the kept candidates make money after costs
        self.assertTrue(conf['beats_null'])
        self.assertGreater(conf['diff_kept_minus_rejected_sol'], conf['null_p95_diff_sol'])
        self.assertLessEqual(conf['welch_p'], LN.ALPHA)
        self.assertLessEqual(conf['reject_fraction'], LN.MAX_REJECT_FRACTION)
        self.assertGreater(r['tested']['rules'], 1)
        self.assertGreater(conf['rug_rate_rejected'], 0.6)
        self.assertLess(conf['rug_rate_kept'], 0.2)
        self.assertGreater(r['union']['confirm']['policy_mean_pnl_sol'], r['baseline']['confirm']['baseline_mean_pnl_sol'])
        doc = json.loads(Path(r['proposal']).read_text())
        self.assertEqual((doc['status'], doc['never_applied_automatically']), ('SELECTION_PROPOSAL_NOT_APPLIED', True))
        self.assertEqual(doc['current']['strategy_version'], self.strategy.strategy_version)
        self.assertIn('wait_for_enrichment:dev_holding_pct', doc['requires_runner_change'])
        buckets = r['tables']['dev_holding_pct']['buckets']
        self.assertGreater(buckets[-1]['rug_rate'], 0.6)
        self.assertLess(buckets[0]['rug_rate'], 0.25)
        self.assertGreater(D(r['winners_vs_rugs']['RUG']['features']['dev_holding_pct']['median']), D(r['winners_vs_rugs']['WIN']['features']['dev_holding_pct']['median']))
        self.assertNotIn('tx_count_first_10m', [c['feature'] for rule in r['selected'] for c in rule['if']][:1])

    def test_a_losing_world_whose_outcomes_ignore_every_feature_is_never_confirmed(self):
        """THE regression test for the pre-M2 bug (a): every window loses on average and the outcome ignores every feature. A t-test of
        the rejected PnL against 0 "confirms" any rule here (rejecting losers always "saves"); kept vs rejected against a random rule of
        the same size does not. (Only the API both versions share is used, so this test also runs against the old learner.)"""
        ds = self.dataset('losing')
        pnl = [r.pnl_lamports for r in ds.rows if r.enterable]
        self.assertEqual(len(pnl), N)
        self.assertLess(sum(pnl) / len(pnl), -0.02 * 10 ** 9)                       # a losing baseline, like the live run
        self.assertGreater(sum(1 for x in pnl if x > 0), 0)                        # a few winners: the kept set COULD make money
        r = self.run_learn('losing', ds)
        self.assertFalse(r['confirmed'], (r['proposal_status'], r['selected']))
        self.assertNotEqual(r['proposal_status'], 'CONFIRMED')
        doc = json.loads(Path(r['proposal']).read_text())
        self.assertEqual((doc['status'], doc['proposed']['selection_rules']), ('NO_PROPOSAL', []))

    def test_a_world_whose_outcomes_ignore_every_feature_yields_no_confirmed_rule(self):
        r = self.run_learn('noise', self.dataset('noise'))
        self.assertIn(r['proposal_status'], ('NO_RULE_FOUND', 'NO_SELECTED_RULE', 'NOT_CONFIRMED'))
        self.assertFalse(r['confirmed'])
        doc = json.loads(Path(r['proposal']).read_text())
        self.assertEqual((doc['status'], doc['proposed']['selection_rules'], doc['requires_runner_change']), ('NO_PROPOSAL', [], []))

    def test_a_signal_that_exists_only_after_the_decision_is_never_learned(self):
        """LATE world: the same planted dev shares, read 60+ s after each decision. The learner excludes every one of them (counted) and
        confirms nothing; relaxing ONLY the late threshold brings the leaked rule back, so the guard is what stops it."""
        w = self.worlds['late']
        ds = self.dataset('late')
        self.assertEqual(ds.stats['with_features'], N)
        self.assertTrue(all(r.features['dev_holding_pct'] is None for r in ds.rows))
        self.assertEqual(ds.stats['excluded']['dev_holding_pct']['AT_COLLECTION_LATE'], N)
        c = sqlite3.connect(w.db.as_uri() + '?mode=ro', uri=True)
        self.addCleanup(c.close)
        lates = [json.loads(raw)['late_s'] for (raw,) in c.execute("SELECT raw FROM observations WHERE kind='features'")]
        self.assertTrue(all(55 <= x <= 400 for x in lates), lates[:5])
        r = self.run_learn('late', ds)
        self.assertFalse(r['confirmed'], r['selected'])
        self.assertNotIn('dev_holding_pct', [c['feature'] for e in r['candidates'] for c in e['rule']['if']])
        leaky = self.run_learn('late', self.dataset('late', policy=LN.LookaheadPolicy(max_late_s=3600)))
        self.assertTrue(leaky['confirmed'])
        self.assertEqual(leaky['selected'][0]['if'][0]['feature'], 'dev_holding_pct')

    def test_the_html_section_and_the_cli(self):
        w = self.worlds['planted']
        out = w.root / 'out'
        out.mkdir()
        cfg = json.loads((ROOT / 'config' / 'lean' / 'lean.example.json').read_text())
        cfg['strategy_config'] = str(w.strategy_path)
        cfg['paths']['path_hours'] = 0.5                                       # what the world recorded: the embargo is 30 min
        (w.root / 'lean.json').write_text(json.dumps(cfg))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = LN.main(['--lean-db', str(w.db), '--paths-db', str(w.paths_db), '--config', str(w.root / 'lean.json'), '--out-dir', str(out),
                            '--min-n', '10', '--min-confirm', '6', '--min-bucket', '5', '--now', str(T0 + 10 ** 6)])
        self.assertEqual(code, 0, buf.getvalue())
        summary = json.loads(buf.getvalue())
        self.assertEqual((summary['status'], summary['proposal_status'], summary['confirmed']), ('OK', 'CONFIRMED', True))
        self.assertEqual((summary['embargo_s'], summary['eta_hours']), (1800.0, 0.0))     # embargo = paths.path_hours of the config
        self.assertGreater(summary['rules_tested'], 1)
        report = json.loads(Path(summary['json']).read_text())
        self.assertGreater(report['split']['purged'], 0)                              # the 30 min embargo purged dev rows
        self.assertEqual(Path(summary['proposal']).parent, out / 'proposals')
        self.assertTrue(Path(summary['proposal']).name.endswith('-selection.json'))
        page = Path(summary['html']).read_text()
        for needle in ('What winners vs rugs looked like at entry', 'dev_holding_pct', 'Never applied automatically', 'CHOSEN',
                       'no lookahead'):
            self.assertIn(needle, page)
        bad = io.StringIO()
        with contextlib.redirect_stdout(bad):
            self.assertEqual(LN.main(['--lean-db', str(w.db), '--paths-db', str(w.root / 'missing.sqlite'), '--out-dir', str(out), '--now', '1']), 2)
        self.assertEqual(json.loads(bad.getvalue())['status'], 'ERROR')
        again = io.StringIO()                                    # the same second again: nothing is overwritten
        with contextlib.redirect_stdout(again):
            self.assertEqual(LN.main(['--lean-db', str(w.db), '--paths-db', str(w.paths_db), '--config', str(w.root / 'lean.json'),
                                      '--out-dir', str(out), '--now', str(T0 + 10 ** 6)]), 2)

    def test_both_databases_are_only_read(self):
        for name, w in self.worlds.items():
            self.dataset(name)
            for path, digest in self.sha[name].items():
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest, path.name)
        c = sqlite3.connect(self.worlds['planted'].db.as_uri() + '?mode=ro', uri=True)
        self.addCleanup(c.close)
        self.assertFalse({k for (k,) in c.execute('SELECT DISTINCT kind FROM observations') if 'label' in k or 'learn' in k})

    def test_it_reads_while_the_files_are_held_open_for_writing(self):
        """The trader keeps both files open and writes while the learner runs: plain mode=ro reads a consistent snapshot."""
        w = self.worlds['noise']
        copy = w.root / 'busy'
        copy.mkdir()
        db, pdb = copy / 'lean.sqlite', copy / 'paths.sqlite'
        shutil.copy(w.db, db)
        shutil.copy(w.paths_db, pdb)
        writer = sqlite3.connect(db, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        pw = L.PathStore(pdb)
        self.addCleanup(pw.close)
        pw.add(marks=[('busy-mint', T0, 1, 1, 1, '1', '1', 'OK')])
        writer.execute('BEGIN IMMEDIATE')                       # a write transaction in progress on lean.sqlite
        writer.execute("INSERT INTO meta VALUES('lint3-busy', 'x')")
        ds = LN.build_dataset(db, pdb, self.strategy, pool_fee_bps=w.pool_fee_bps)
        writer.execute('ROLLBACK')
        self.assertEqual(ds.stats['with_features'], N)
        self.assertEqual(ds.stats['skipped'], {'MARKS_WITHOUT_PATH': 1})


if __name__ == '__main__':
    unittest.main()
