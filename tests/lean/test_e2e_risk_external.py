"""SYNTHETIC_TEST_ONLY: L21 end to end with the REAL modules (runner wiring and hook, screen, strategy, paper, store, the RugCheck client
and its Transport, the recorder, the L07R2 path recorder and the selection learner). Only HTTP is faked: tests/lean/fakeworld.py for
Helius / Jupiter / Kraken, and a fake ``api.rugcheck.xyz`` in front of it (synthetic report bodies; see tests/lean/test_risk_external.py).
No network, no credentials.

Trading scenario (9 launches, 60 s ticks; after each candidate pass the recorder's queue is drained, as its thread would):
  ok_1, ok_2     passed and entered (ok_2's creator launched 4 tokens in the last 30 days; 3 of them are sampled, 2 rugged)
  cost           passed, rejected after the quote
  no_route       passed, the entry quote has no route
  mcap_high      SOFT reject (a row: hazard-free)
  mint_authority HAZARD reject: no row, not one RugCheck request
  copy_of_ok_1   passed, same name as ok_1 and the same IPFS image: a copycat
  unknown        RugCheck has no report (HTTP 400): the row is all reasons
  throttled      RugCheck answers 429: the row is all reasons and the next candidate inside the back-off sends nothing
and the trading result is IDENTICAL with the recorder switched off.

Learner worlds (40 launches, one per 240 s): a launch with a high RugCheck score rugs, the others win or lose. ON TIME: the rows are
collected a few seconds after the decision and the learner reads rc_score. LATE: the same rows read a minute after the decision; the
learner's lookahead guard excludes every value (and relaxing only the guard brings them back).
"""
import json
import os
import random
import socket
import tempfile
import threading
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from lean import learn as LN, paths as L, providers, risk_external as R, strategy as S
from lean.__main__ import build_runner, load_config
from tests.lean.fakeworld import FakeTime, Resp, T0, Token, World
from tests.lean.test_e2e_real import steps
from tests.lean.test_risk_external import iso, report

ROOT = Path(__file__).resolve().parents[2]
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}
IMAGE = 'https://ipfs.io/ipfs/QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG'
SCENARIO = [('ok_1', dict(path=steps((300, 0.7))), 0), ('cost', dict(route_fee_bps=600), 50), ('mint_authority', dict(hazard='mint_authority'), 100),
            ('mcap_high', dict(quote_sol=3000), 150), ('no_route', dict(no_route=True), 200), ('ok_2', dict(path=steps((300, 0.7))), 250),
            ('copy_of_ok_1', dict(), 300), ('unknown', dict(), 400), ('throttled', dict(), 600), ('after_throttle', dict(), 650)]
END = T0 + 2000
DRAIN_STEP = 1.5


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class RugCheckWorld:
    """``api.rugcheck.xyz`` in front of the fake Solana world: everything else goes to ``world.opener``."""

    def __init__(self, world):
        self.world, self.reports, self.requests = world, {}, []

    def opener(self, request, timeout):
        if not request.full_url.startswith('https://api.rugcheck.xyz/'):
            return self.world.opener(request, timeout)
        assert request.get_method() == 'GET'
        mint = request.full_url.split('/v1/tokens/', 1)[1].rsplit('/report', 1)[0]
        self.requests.append((mint, self.world.clock.time()))
        answer = self.reports.get(mint, (400, b'{"error":"invalid token mint"}', {}))
        if isinstance(answer, tuple):
            resp = Resp(answer[1], answer[0])
            resp.headers = answer[2]
            return resp
        return Resp(answer)


def drain(r, clock, step=DRAIN_STEP):
    while True:
        clock.t += step                                       # the recorder's thread: one candidate after another
        if not r.risk_external.process_one():
            break


class TradingE2E(unittest.TestCase):
    maxDiff = None

    def build(self, *, enabled):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(os.path.realpath(tmp.name))
        clock = FakeTime(T0)
        tokens = [Token(21 + 2 * i, **kw) for i, (_, kw, _) in enumerate(SCENARIO)]
        role = {t.mint: name for t, (name, _, _) in zip(tokens, SCENARIO)}
        by = {name: t for t, (name, _, _) in zip(tokens, SCENARIO)}
        world = World(root, tokens, clock)
        world.add_frames([T0 + off for _, _, off in SCENARIO])
        rc = RugCheckWorld(world)
        for name, token in by.items():
            if name not in ('unknown', 'throttled'):
                rc.reports[token.mint] = report(token.mint, score=100 + len(name), name=name, symbol=name[:4].upper())
        rc.reports[by['copy_of_ok_1'].mint] = report(by['copy_of_ok_1'].mint, name='OK 1!', symbol='COPY', image=IMAGE)
        rc.reports[by['ok_1'].mint] = report(by['ok_1'].mint, score=104, name='ok_1', symbol='OK_1', image=IMAGE)
        rc.reports[by['throttled'].mint] = (429, b'{"error":"Too Many Requests"}', {})
        prior = [(base58_mint(90 + i), T0 - (i + 1) * 2 * 86400) for i in range(4)] + [(base58_mint(99), T0 - 60 * 86400)]
        rc.reports[by['ok_2'].mint] = report(by['ok_2'].mint, name='ok_2', symbol='OK_2',
                                             creator_tokens=[{'mint': m, 'marketCap': 3000, 'createdAt': iso(ts)} for m, ts in prior])
        for i, (m, _) in enumerate(prior):
            rc.reports[m] = report(m, rugged=i in (0, 2))
        cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        self.assertTrue(cfg['risk_external']['enabled'])                      # the shipped example turns it on
        cfg['paths'] = L.config({'enabled': False})
        cfg['held_risk'], cfg['wallet_signals'] = {'enabled': False}, None
        from lean import features as F
        cfg['features'] = F.config(None)                                      # isolate this recorder
        cfg['risk_external'] = R.config({**cfg['risk_external'], 'enabled': enabled, 'backoff_s': 300.0})   # after_throttle is inside it
        r = build_runner(cfg, state_dir=root / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='e2e-risk',
                         clock=clock.time, transport_kwargs={'opener': rc.opener, 'clock': clock.time, 'monotonic': clock.monotonic,
                                                             'sleep': clock.sleep, 'rng': lambda: 0.5})
        self.addCleanup(r.store.close)
        return r, role, by, rc, clock

    def drive(self, r, clock):
        tick = T0
        while tick <= END:
            if clock.time() < tick:
                clock.t = tick
            r.position_pass()
            r.candidate_pass()
            if r.risk_external is not None:
                drain(r, clock)
            self.assertIsNone(r.halted, r.halted)
            tick += 60

    def rows(self, r, role):
        out = {}
        for obs in r.store.rows('observations', kind=R.KIND, limit=10000):
            payload = json.loads(obs['raw'])
            self.assertNotIn(role[payload['mint']], out)
            out[role[payload['mint']]] = payload
        return out

    def test_trading_is_identical_with_the_recorder_on_and_off(self):
        on, role_a, *rest_a = self.build(enabled=True)
        self.drive(on, rest_a[-1])
        off, role_b, *rest_b = self.build(enabled=False)
        self.drive(off, rest_b[-1])

        def result(r, role):
            decisions = sorted((d['kind'], role[d['mint']], d['action'], tuple(json.loads(d['reasons']))) for d in r.store.rows('decisions', limit=100000)
                               if d['mint'] in role)
            fills = [(role[f['mint']], f['side'], f['qty_raw'], f['sol_lamports'], f['fee_lamports']) for f in r.store.rows('fills', limit=100000)]
            return decisions, fills, r.store.cash()
        a, b = result(on, role_a), result(off, role_b)
        self.assertTrue(a[1])
        self.assertEqual(a, b)
        self.assertTrue(self.rows(on, role_a))
        self.assertIsNone(off.risk_external)
        self.assertEqual(off.store.rows('observations', kind=R.KIND), [])
        self.assertEqual(rest_b[1].requests, [])                               # off: not one RugCheck request
        off.write_health()
        self.assertEqual(json.loads((Path(off.state_dir) / 'health.json').read_text())['risk_external'], {'enabled': False, 'alive': False})

    def test_one_row_per_hazard_free_candidate_with_the_report_as_collected(self):
        r, role, by, rc, clock = self.build(enabled=True)
        self.drive(r, clock)
        rows = self.rows(r, role)
        self.assertEqual(set(rows), {name for name, _, _ in SCENARIO} - {'mint_authority'})
        self.assertNotIn(by['mint_authority'].mint, [m for m, _ in rc.requests])          # a hazard costs nothing
        screens = {}
        for d in r.store.rows('decisions', kind='screen', limit=10000):
            screens.setdefault(role[d['mint']], []).append(d['ts'])
        for name, row in rows.items():
            self.assertEqual(row['decision_ts'], screens[name][0], name)                  # THE screen decision, from the store
            # (a watchlist re-screen in the same state buys no second row: one collection per mint and screen state)
            self.assertTrue(0 < row['late_s'] <= 10, (name, row['late_s']))
            self.assertEqual(row['flags'], {n: 'AT_COLLECTION' for n in R.FIELDS if row['fields'][n] is not None}, name)
            self.assertTrue(all((row['fields'][n] is None) == (n in row['missing']) for n in R.FIELDS), name)
        ok = rows['ok_1']['fields']
        self.assertEqual((ok['rc_score'], ok['rc_rugged'], ok['rc_top10_pct'], ok['rc_total_holders']), (104, False, '25.5000', 1200))
        self.assertEqual((ok['creator_tokens_30d'], ok['copycat_of_recent'], ok['same_image_count']), (0, 0, 0))
        copy = rows['copy_of_ok_1']['fields']
        self.assertEqual((copy['copycat_of_recent'], copy['copycat_name_count'], copy['same_image_count']), (1, 1, 1))
        two = rows['ok_2']
        self.assertEqual((two['fields']['creator_tokens_7d'], two['fields']['creator_tokens_30d'], two['fields']['creator_dead_30d']), (3, 4, 4))
        self.assertEqual((two['fields']['creator_sampled'], two['fields']['creator_rugged_sampled'], two['calls']), (3, 2, 4))
        self.assertEqual(rows['unknown']['missing']['rc_score'], 'RC_UNKNOWN_MINT')
        self.assertEqual(rows['throttled']['missing']['rc_score'], 'RC_HTTP_429')
        self.assertEqual((rows['after_throttle']['missing']['rc_score'], rows['after_throttle']['calls']), ('RC_BACKOFF', 0))
        self.assertNotIn(by['after_throttle'].mint, [m for m, _ in rc.requests])
        self.assertEqual(rows['mcap_high']['screen_passed'], False)
        # the client paced itself at its own 1 req/s bucket: no two requests inside one second
        times = [t for _, t in rc.requests]
        self.assertTrue(all(b - a >= 1.0 - 1e-9 for a, b in zip(times, times[1:])), times)
        r.write_health()
        health = json.loads((Path(r.state_dir) / 'health.json').read_text())['risk_external']
        self.assertEqual((health['enabled'], health['alive'], health['rows'], health['calls'], health['http_429']),
                         (True, False, len(rows), len(rc.requests), 1))
        self.assertEqual(r.ops.credits.calls.get('rugcheck', {}).get('report'), len(rc.requests))   # counted by ops ...
        self.assertEqual(r.ops.credits.cost('rugcheck', 'report'), 0)                               # ... and free

    def test_a_broken_or_stuck_recorder_changes_no_entry(self):
        r, role, by, rc, clock = self.build(enabled=True)

        class Boom:
            def submit(self, *a, **k):
                raise RuntimeError('recorder bug')

            def health(self):
                return {'enabled': True}
        r.risk_external = Boom()
        tick = T0
        while tick <= END:
            clock.t = max(clock.t, tick)
            r.position_pass()
            r.candidate_pass()
            tick += 60
        self.assertTrue(r.store.rows('fills', limit=10))
        self.assertNotIn('CANDIDATE_FAILED', r.errors_by_code)

    def test_run_starts_the_thread_reports_alive_and_stops_it_with_the_runner(self):
        r, role, by, rc, clock = self.build(enabled=True)
        ran = threading.Event()
        real_run = r.risk_external.run

        def run(stop, tick=0.5):
            ran.set()
            real_run(stop, tick=0.01)
        r.risk_external.run = run
        thread = threading.Thread(target=lambda: r.run(candidate_interval=0.01, position_interval=0.01, install_signals=False), daemon=True)
        thread.start()
        try:
            self.assertTrue(ran.wait(5))
            self.assertTrue(r.health()['risk_external']['alive'])
        finally:
            r.stop.set()
            thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertFalse(r.health()['risk_external']['alive'])


def base58_mint(n):
    from desk.security import base58
    return base58(bytes([n]) * 32)


# ---------------------------------------------------------------------------------------------------------------- the learner
N = 40
SPACING = 240
LEARN_END = T0 + N * SPACING + 1500
PATHS = {'RUG': steps((60, 0.15)), 'WIN': steps((120, 1.5), (240, 2.2), (360, 3.2), (480, 1.8)), 'LOSS': steps((120, 0.75))}


def plan(seed):
    rng = random.Random(seed)
    out = []
    for _ in range(N):
        score = rng.choice((rng.randint(100, 3000), rng.randint(6000, 30000)))
        kind = 'RUG' if score > 5000 else rng.choice(('WIN', 'WIN', 'LOSS'))
        out.append({'score': score, 'kind': kind})
    return out


class LearnWorld:
    def __init__(self, root, *, late):
        self.plan = plan(5)
        clock = FakeTime(T0)
        tokens = [Token(10 + i, coin_creator_tag=120 + i, path=PATHS[p['kind']]) for i, p in enumerate(self.plan)]
        self.role = {t.mint: i for i, t in enumerate(tokens)}
        world = World(root, tokens, clock)
        world.add_frames([T0 + i * SPACING for i in range(N)])
        rc = RugCheckWorld(world)
        for t, p in zip(tokens, self.plan):
            rc.reports[t.mint] = report(t.mint, score=p['score'], name='t%d' % t.tag, symbol='T%d' % t.tag)
        strategy = json.loads((ROOT / 'config' / 'lean' / 'strategy-default.json').read_text())
        strategy.update(daily_loss_stop_fraction='0.9', daily_liquidate_fraction='0.95', loss_streak_pause_at=100)
        (root / 'strategy.json').write_text(json.dumps(strategy))
        cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        cfg['strategy_config'] = str(root / 'strategy.json')
        cfg['execution'], cfg['held_risk'], cfg['wallet_signals'] = None, {'enabled': False}, None
        from lean import features as F
        cfg['features'] = F.config(None)
        cfg['paths'] = L.config({'enabled': True, 'path_hours': 0.5, 'interval_s': 15})
        cfg['risk_external'] = R.config({'enabled': True, 'max_late_s': 3600 if late else 30})    # LATE: the recorder still reads it
        cfg['lanes'] = {'shares': providers.validate_lane_shares({'main': 0.5, 'exit': 0.2, 'low': 0.3}), 'low_shed_s': 1}
        self.pool_fee_bps = cfg['pool_fee_bps']
        r = build_runner(cfg, state_dir=root / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='e2e-risk-learn',
                         clock=clock.time, transport_kwargs={'opener': rc.opener, 'clock': clock.time, 'monotonic': clock.monotonic,
                                                             'sleep': clock.sleep, 'rng': lambda: 0.5})
        r.l07r_start_paths()
        tick = T0
        while tick <= LEARN_END:
            for sub in (15, 30, 45):
                if clock.time() < tick - 60 + sub:
                    clock.t = tick - 60 + sub
                    r.paths.poll()
            if clock.time() < tick:
                clock.t = tick
            r.paths.poll()
            if late:
                drain(r, clock)                                   # LATE: last tick's candidates, a minute after their decision
            r.position_pass()
            r.candidate_pass()
            if not late:
                drain(r, clock)
            assert r.halted is None, r.halted
            tick += 60
        drain(r, clock)
        self.db, self.paths_db = r.store.path, r.paths.db.path
        self.strategy_path = root / 'strategy.json'
        r.store.close()
        r.paths.db.close()


class LearnerE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(cls.tmp.name))
        cls.worlds = {}
        for name, late in (('on_time', False), ('late', True)):
            (base / name).mkdir()
            cls.worlds[name] = LearnWorld(base / name, late=late)
        cls.strategy = S.StrategyConfig.load(cls.worlds['on_time'].strategy_path)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def dataset(self, name, **kw):
        w = self.worlds[name]
        return LN.build_dataset(w.db, w.paths_db, self.strategy, pool_fee_bps=w.pool_fee_bps, **kw)

    def test_the_learner_reads_what_the_recorder_wrote_at_the_decision(self):
        w, ds = self.worlds['on_time'], self.dataset('on_time')
        self.assertEqual(ds.stats['with_features'], N, ds.stats['skipped'])
        for row in ds.rows:
            p = w.plan[w.role[row.mint]]
            self.assertEqual(row.features['rc_score'], D(p['score']), row.mint)
            self.assertEqual(row.features['rc_rugged'], D(0))
            self.assertEqual(row.features['copycat_of_recent'], D(0))
            self.assertIsNone(row.features['dev_holding_pct'])                   # L12F is off in this world
        self.assertEqual(ds.stats['excluded'], {})
        r = LN.learn(ds.rows, strategy=self.strategy, now=T0 + 10 ** 6, proposals_dir=Path(self.tmp.name) / 'p1', dataset_stats=ds.stats,
                     feature_names=('rc_score', 'rc_total_holders'), min_n=4, min_confirm=3, min_bucket=3)
        self.assertEqual(r['proposal_status'], 'CONFIRMED', r['candidates'][:3])
        self.assertEqual((r['selected'][0]['if'][0]['feature'], r['selected'][0]['if'][0]['op']), ('rc_score', '>'))
        self.assertIn('wait_for_risk_external:rc_score', json.loads(Path(r['proposal']).read_text())['requires_runner_change'])

    def test_a_snapshot_read_after_the_decision_is_never_learned(self):
        """LATE world: the same reports, read a minute after each decision. Every value is excluded (counted); only relaxing the guard
        brings them back, so the guard is what stops the leak."""
        w = self.worlds['late']
        ds = self.dataset('late')
        self.assertEqual(ds.stats['with_features'], N)
        self.assertTrue(all(r.features[n] is None for r in ds.rows for n in LN.RISK_FEATURES))
        self.assertEqual(ds.stats['excluded']['rc_score']['AT_COLLECTION_LATE'], N)
        import sqlite3
        c = sqlite3.connect(w.db.as_uri() + '?mode=ro', uri=True)
        self.addCleanup(c.close)
        lates = [json.loads(raw)['late_s'] for (raw,) in c.execute("SELECT raw FROM observations WHERE kind='risk_external'")]
        self.assertEqual(len(lates), N)
        self.assertTrue(all(55 <= x <= 400 for x in lates), lates[:5])
        lax = self.dataset('late', policy=LN.LookaheadPolicy(max_late_s=3600))
        self.assertEqual({r.mint: r.features['rc_score'] for r in lax.rows},
                         {m: D(w.plan[i]['score']) for m, i in w.role.items()})

    def test_a_row_stamped_after_the_decision_is_never_used(self):
        """A row whose own decision_ts is after the learner's decision (built by the REAL row builder) is skipped and counted."""
        import shutil
        from lean.store import Store
        w = self.worlds['on_time']
        clean = {r.mint: r for r in self.dataset('on_time').rows}
        target = next(m for m, i in w.role.items() if i == 3)
        copy = Path(self.tmp.name) / 'leak.sqlite'
        shutil.copy(w.db, copy)
        store = Store(copy, code_version='e2e', strategy_version='lean-1')
        cid = store.db.execute('SELECT candidate_id FROM decisions WHERE mint=? AND candidate_id IS NOT NULL', (target,)).fetchone()[0]
        at = clean[target].ts + 30
        payload = R.build_row(target, screen_passed=True, decision_ts=at, collected_at=at + 1, calls=1, fields={'rc_score': 777777}, missing={})
        store.add_observation(R.KIND, R.canonical(payload), mint=target, candidate_id=cid, meta=payload, ts=at + 1)
        store.close()
        ds = LN.build_dataset(copy, w.paths_db, self.strategy, pool_fee_bps=w.pool_fee_bps)
        self.assertEqual({r.mint: r for r in ds.rows}[target].features['rc_score'], clean[target].features['rc_score'])
        self.assertEqual(ds.stats['skipped'].get('RISK_EXTERNAL_AFTER_DECISION'), 1)


if __name__ == '__main__':
    unittest.main()
