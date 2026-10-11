"""SYNTHETIC_TEST_ONLY: the selection learner (L19) end to end on the REAL runner, the REAL L12F feature recorder and the REAL L07R2
path recorder (lean/paths.py). Only HTTP is faked (tests/lean/fakeworld.py; the dev wallet balances and the pool histories are served
through its ``extra_rpc`` hook).

Two worlds of 100 launches, one per 240 s, each with a dev wallet holding a different share of the supply:
  PLANTED  a launch whose dev holds more than 15 % usually rugs (the price collapses a minute after the entry); the others are winners
           (ladder to 3.2x) or small losers (-25 % stop). The tx count of the pool's first 10 minutes is independent noise.
  NOISE    the same dev shares and tx counts, but the outcome is drawn independently of every feature.
The runner trades them all (the daily stops are widened so that the rugs do not halt it), the recorders write ``lean.sqlite`` (features
rows) and ``paths.sqlite`` (the marks of every candidate), and ``lean.learn`` reads those two files and nothing else.

What it proves
  labels     every path gets the label of the family that generated it (RUG / WIN / LOSS) from the recorded vault amounts
  no leak    a features row that appears later with a perfectly predictive value is not used (decision-time guard)
  recovery   the planted ``dev_holding_pct > T`` rule (T = 15) is mined, selected on the select window and confirmed on the confirm window;
             a proposal is written, never applied; the noise world yields NO confirmed rule
  read-only  both databases are byte-identical afterwards
"""
import contextlib
import hashlib
import io
import json
import os
import random
import socket
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from lean import features as F, labels as LB, learn as LN, paths as L, providers, strategy as S
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
TX_NAMES = ('dev_holding_pct', 'tx_count_first_10m')
MIN = dict(min_n=10, min_confirm=6, min_bucket=5)


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def plan(planted, seed):
    rng = random.Random(seed)
    out = []
    for i in range(N):
        dev = round(rng.uniform(0.5, 30), 1)
        tx = rng.randint(3, 60)
        if planted:
            kind = 'RUG' if (dev > 15 and rng.random() < 0.97) or (dev <= 15 and rng.random() < 0.03) else rng.choice(('WIN', 'WIN', 'LOSS'))
        else:
            kind = rng.choice(('RUG', 'WIN', 'LOSS'))
        out.append({'dev': dev, 'tx': tx, 'kind': kind})
    return out


class World_:
    """One simulated run: the runner + both recorders over the whole scenario, then the files closed."""

    def __init__(self, root, planted, seed):
        self.root, self.plan = root, plan(planted, seed)
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
        cfg['execution'], cfg['held_risk'] = None, {'enabled': False}
        cfg['paths'] = L.config({'enabled': True, 'path_hours': 0.5, 'interval_s': 15})
        cfg['features'] = F.config({'enabled': True})
        cfg['lanes'] = {'shares': providers.validate_lane_shares({'main': 0.5, 'exit': 0.2, 'low': 0.3}), 'low_shed_s': 1}
        r = build_runner(cfg, state_dir=root / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='e2e-learn',
                         clock=clock.time, transport_kwargs={'opener': world.opener, 'clock': clock.time, 'monotonic': clock.monotonic,
                                                             'sleep': clock.sleep, 'rng': lambda: 0.5})
        r.l07r_start_paths()
        tick = T0
        while tick <= END:
            for sub in (15, 30, 45):
                if clock.time() < tick - 60 + sub:
                    clock.t = tick - 60 + sub
                    r.paths.poll()
            if clock.time() < tick:
                clock.t = tick
            r.paths.poll()
            r.position_pass()
            r.candidate_pass()
            while True:
                clock.t += 2.0                                    # the feature recorder's thread: one candidate per low-lane refill
                if not r.features.process_one():
                    break
            assert r.halted is None, r.halted
            tick += 60
        assert r.store.check_invariants()
        self.fills = r.store.rows('fills', limit=100000)
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
        for name, planted, seed in (('planted', True, 7), ('noise', False, 11)):
            root = base / name
            root.mkdir()
            cls.worlds[name] = World_(root, planted, seed)
        cls.strategy = S.StrategyConfig.load(cls.worlds['planted'].strategy_path)
        cls.sha = {n: {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (w.db, w.paths_db)} for n, w in cls.worlds.items()}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def dataset(self, name, **kw):
        w = self.worlds[name]
        return LN.build_dataset(w.db, [w.paths_db], self.strategy, **kw)

    def mint_of(self, name, i):
        w = self.worlds[name]
        return next(m for m, k in w.role.items() if k == i)

    # ------------------------------------------------------------------------------------------------------------
    def test_every_candidate_has_a_path_and_a_label_of_the_family_that_made_it(self):
        for name in ('planted', 'noise'):
            w, ds = self.worlds[name], self.dataset(name)
            self.assertEqual(ds.stats['paths'], N, ds.stats)
            self.assertEqual(ds.stats['with_features'], N, ds.stats['skipped'])
            self.assertEqual(ds.stats['enterable'], N)
            self.assertEqual(sum(1 for f in w.fills if f['side'] == 'buy'), N)         # the runner really traded every one of them
            for row in ds.rows:
                self.assertEqual(row.label.label, w.plan[w.role[row.mint]]['kind'], (name, row.mint))
                self.assertEqual(row.label.label_version, LB.LABEL_VERSION)

    def test_the_rug_family_loses_and_the_winner_family_wins_under_the_live_exit_rules(self):
        ds = self.dataset('planted')
        by = {}
        for row in ds.rows:
            by.setdefault(row.label.label, []).append(row.pnl_lamports)
        self.assertLess(max(by['RUG']), 0)
        self.assertGreater(min(by['WIN']), 0)
        self.assertLess(max(by['LOSS']), 0)
        self.assertLess(sum(by['RUG']) / len(by['RUG']), sum(by['LOSS']) / len(by['LOSS']))     # a rug costs more than an ordinary stop

    def test_features_are_what_the_recorder_saw_at_the_screen(self):
        w, ds = self.worlds['planted'], self.dataset('planted')
        for row in ds.rows:
            p = w.plan[w.role[row.mint]]
            self.assertEqual(row.features['dev_holding_pct'], D(str(p['dev'])).quantize(D('0.0001')), row.mint)
            self.assertEqual(row.features['tx_count_first_10m'], D(p['tx']), row.mint)
            self.assertIsNotNone(row.features['market_cap_usd'])
        self.assertEqual(ds.stats['skipped'], {})

    def test_a_feature_row_that_appears_after_the_decision_is_never_used(self):
        w = self.worlds['planted']
        target = self.mint_of('planted', 5)
        clean = {r.mint: r for r in self.dataset('planted').rows}[target]
        # work on a copy: append a perfectly predictive row stamped long after the decision, and one screened after it
        import shutil
        copy = Path(w.db.parent / 'leak.sqlite')
        shutil.copy(w.db, copy)
        shutil.copy(w.paths_db, copy.with_name('leak-paths.sqlite'))
        store = Store(copy, code_version='e2e-learn', strategy_version='lean-1')
        start = clean.ts
        for screen_at, collected_at, dev in ((start + 30, start + 31, '99'), (start - 5, start + 4000, '98')):
            payload = {'features_version': F.FEATURES_VERSION, 'mint': target, 'entered': True, 'not_entered_reason': None, 'screen_passed': True,
                       'screen_at': screen_at, 'collected_at': collected_at, 'calls': 0, 'fields': {'dev_holding_pct': dev}, 'missing': {}}
            store.add_observation('features', json.dumps(payload).encode(), mint=target, ts=collected_at, meta=payload)
        store.close()
        ds = LN.build_dataset(copy, [copy.with_name('leak-paths.sqlite')], self.strategy)
        leaked = {r.mint: r for r in ds.rows}[target]
        self.assertEqual(leaked.features, clean.features)
        self.assertEqual(ds.stats['skipped'].get('FEATURES_AFTER_DECISION'), 2)
        # the lag is the only knob: with an absurd lag the late-collected row (screened before the decision) is accepted
        lax = LN.build_dataset(copy, [copy.with_name('leak-paths.sqlite')], self.strategy, max_lag_s=10 ** 6)
        self.assertEqual({r.mint: r for r in lax.rows}[target].features['dev_holding_pct'], D(98))

    def test_the_planted_rule_is_recovered_selected_and_confirmed(self):
        ds = self.dataset('planted')
        props = Path(self.worlds['planted'].root) / 'proposals'
        r = LN.learn(ds.rows, strategy=self.strategy, now=T0 + 10 ** 6, proposals_dir=props, dataset_stats=ds.stats, feature_names=ds.stats['feature_names'], **MIN)
        self.assertEqual(r['proposal_status'], 'WRITTEN', r['candidates'][:3])
        first = r['selected'][0]['if'][0]
        self.assertEqual((first['feature'], first['op']), ('dev_holding_pct', '>'))
        self.assertTrue(8 <= D(first['value']) <= 20, first)
        conf = r['union']['confirm']
        self.assertGreater(conf['gain_per_candidate_sol'], 0)
        self.assertGreater(conf['rug_rate_rejected'], 0.6)
        self.assertLess(conf['rug_rate_kept'], 0.2)
        self.assertGreater(r['union']['confirm']['policy_mean_pnl_sol'], r['baseline']['confirm']['baseline_mean_pnl_sol'])
        doc = json.loads(Path(r['proposal']).read_text())
        self.assertEqual((doc['status'], doc['never_applied_automatically']), ('SELECTION_PROPOSAL_NOT_APPLIED', True))
        self.assertEqual(doc['current']['strategy_version'], self.strategy.strategy_version)
        self.assertIn('wait_for_enrichment:dev_holding_pct', doc['requires_runner_change'])
        # the tables show the planted structure: the highest dev bucket is mostly rugs, the lowest hardly any
        buckets = r['tables']['dev_holding_pct']['buckets']
        self.assertGreater(buckets[-1]['rug_rate'], 0.6)
        self.assertLess(buckets[0]['rug_rate'], 0.25)
        self.assertGreater(D(r['winners_vs_rugs']['RUG']['features']['dev_holding_pct']['median']), D(r['winners_vs_rugs']['WIN']['features']['dev_holding_pct']['median']))
        # the tx-count noise is not what the winning rule is about
        self.assertNotIn('tx_count_first_10m', [c['feature'] for rule in r['selected'] for c in rule['if']][:1])

    def test_a_world_whose_outcomes_ignore_every_feature_yields_no_confirmed_rule(self):
        ds = self.dataset('noise')
        props = Path(self.worlds['noise'].root) / 'proposals'
        r = LN.learn(ds.rows, strategy=self.strategy, now=T0 + 10 ** 6, proposals_dir=props, dataset_stats=ds.stats, feature_names=ds.stats['feature_names'], **MIN)
        self.assertIn(r['proposal_status'], ('NO_RULE_FOUND', 'NO_SELECTED_RULE', 'NOT_CONFIRMED'))
        self.assertIsNone(r['proposal'])
        self.assertFalse(props.exists())

    def test_the_html_section_and_the_cli(self):
        w = self.worlds['planted']
        out, props = w.root / 'out', w.root / 'cli-proposals'
        out.mkdir()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = LN.main(['--db', str(w.db), '--paths-db', str(w.paths_db), '--strategy', str(w.strategy_path), '--out-dir', str(out),
                            '--proposals-dir', str(props), '--min-n', '10', '--min-confirm', '6', '--now', str(T0 + 10 ** 6)])
        self.assertEqual(code, 0, buf.getvalue())
        summary = json.loads(buf.getvalue())
        self.assertEqual((summary['status'], summary['proposal_status']), ('OK', 'WRITTEN'))
        page = Path(summary['html']).read_text()
        for needle in ('What winners vs rugs looked like at entry', 'dev_holding_pct', 'Never applied automatically', 'CHOSEN'):
            self.assertIn(needle, page)
        bad = io.StringIO()
        with contextlib.redirect_stdout(bad):
            self.assertEqual(LN.main(['--db', str(w.db), '--paths-db', str(w.root / 'missing.sqlite'), '--out-dir', str(out), '--now', '1']), 2)
        self.assertEqual(json.loads(bad.getvalue())['status'], 'ERROR')

    def test_both_databases_are_only_read(self):
        for name, w in self.worlds.items():
            self.dataset(name)
            for path, digest in self.sha[name].items():
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest, path.name)
        # the trader's store never receives a learner row, and the recorder file has no learner table
        c = sqlite3.connect(self.worlds['planted'].db.as_uri() + '?mode=ro', uri=True)
        self.addCleanup(c.close)
        self.assertFalse({k for (k,) in c.execute('SELECT DISTINCT kind FROM observations') if 'label' in k or 'learn' in k})


if __name__ == '__main__':
    unittest.main()
