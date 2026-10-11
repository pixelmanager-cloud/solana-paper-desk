"""SYNTHETIC_TEST_ONLY: L13 end to end with the REAL lean modules (runner, candidates, screen, strategy, paper, store, providers,
wallet_signals). Only the HTTP layer is faked (tests/lean/fakeworld.py + the three extra RPC methods from
tests/lean/test_wallet_signals.Chain). No network, no credentials.

Scenario (8 launches, ticks of 60 s; each tick: position pass, candidate pass, signals pass):
  bundle      4 buyers in the graduation slot, three funded by one source, plus an organic buyer; and a 5th wallet after the window
  organic     4 spread-out buyers with different funders
  hazard      active mint authority (screen REJECT) with a bundled launch: collected all the same
  win1, win2  S1 and S2 buy early; the L07-style path marks then show 2.5x and 2.2x
  rug         R1 and S3 buy early; the path then collapses to 0.1x
  outage      the provider fails the very first request of one candidate: it is retried a minute later and collected
  late        S1, S2 and R1 buy early AFTER the outcomes are known: smart_buyers_count == 2
  and the whole trading result (screens, entries, exits, fills) is IDENTICAL with the feature switched off.
"""
import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from lean.__main__ import build_runner, load_config
from lean.providers import ProviderError
from tests.lean.fakeworld import FakeTime, T0, Token, World
from tests.lean.test_wallet_signals import Chain, addr, add_path

ROOT = Path(__file__).resolve().parents[2]
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}
END = T0 + 4500


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def steps(*points):
    from decimal import Decimal

    def path(age):
        value = Decimal(1)
        for start, m in points:
            if age >= start:
                value = Decimal(str(m))
        return value
    return path


# (role, Token kwargs, ready offset)
SCENARIO = [
    ('bundle', dict(), 0),
    ('organic', dict(path=steps((600, 1.4))), 120),
    ('hazard', dict(hazard='mint_authority'), 240),
    ('win1', dict(), 360),
    ('win2', dict(), 480),
    ('rug', dict(), 600),
    ('outage', dict(), 1380),
    ('late', dict(), 2700),
]
S1, S2, S3, R1 = addr(11), addr(12), addr(13), addr(14)


def grad_slot(world):
    import sqlite3
    with sqlite3.connect(world.discovery_db) as c:
        payload = json.loads(c.execute('SELECT payload FROM raw_events ORDER BY seq LIMIT 1').fetchone()[0])
    return payload['params']['result']['slot']


def build_chain(tokens, role, slot):
    chain = Chain()
    by_role = {role[t.mint]: t for t in tokens}

    def launch(name, buys, funders=None):
        token = by_role[name]
        for offset, wallets in buys:
            chain.swap(slot + offset, {w: 10 ** 12 for w in wallets}, mint=token.mint, pool=token.pool)
        for wallet, source in (funders or {}).items():
            chain.fund(wallet, source, slot=slot - 5)
    a, b, c, d, org = (addr(i) for i in (21, 22, 23, 24, 25))
    launch('bundle', [(0, [a]), (0, [b, c]), (0, [d]), (60, [org]), (400, [addr(26)])], {a: addr(90), b: addr(90), c: addr(90), d: addr(92), org: addr(93)})
    launch('organic', [(10 * i, [addr(30 + i)]) for i in range(1, 5)], {addr(30 + i): addr(60 + i) for i in range(1, 5)})
    launch('hazard', [(0, [addr(41)]), (0, [addr(42)])], {addr(41): addr(94), addr(42): addr(94)})
    launch('win1', [(5, [S1]), (9, [S2]), (30, [addr(51)])], {S1: addr(95), S2: addr(96), addr(51): addr(97)})
    launch('win2', [(4, [S1]), (8, [S2])], {})
    launch('rug', [(3, [R1]), (6, [S3])], {R1: addr(98)})
    launch('outage', [(2, [addr(61)]), (20, [addr(62)])], {})
    launch('late', [(7, [addr(71)]), (9, [S1]), (12, [R1]), (14, [S2])], {})
    return chain


class WalletSignalsEndToEnd(unittest.TestCase):
    maxDiff = None

    def run_world(self, signals):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(os.path.realpath(tmp.name))
        clock = FakeTime(T0)
        tokens = [Token(61 + 2 * i, **kwargs) for i, (_, kwargs, _) in enumerate(SCENARIO)]
        role = {t.mint: name for t, (name, _, _) in zip(tokens, SCENARIO)}
        world = World(root, tokens, clock)
        world.add_frames([T0 + offset for _, _, offset in SCENARIO])
        slot = grad_slot(world)
        chain = build_chain(tokens, role, slot)
        outage_pool = next(t.pool for t in tokens if role[t.mint] == 'outage')
        failures = {'n': 0}

        def provider_down(params):                                       # the first collection of 'outage' fails (one attempt per call)
            if params[0] == outage_pool and failures['n'] < 1:
                failures['n'] += 1
                return ProviderError('HTTP_503', True)
        chain.fail['getSignaturesForAddress'] = provider_down
        self.failures = failures
        world.extra_rpc = chain.rpc_handlers()
        cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        from lean import paths, providers
        cfg['paths'] = paths.config({'enabled': True})       # LINT2: paths.sqlite is the outcome source; the recorder thread is
                                                              # built by Runner.run() only, so it never runs in this manual drive
        cfg['lanes'] = {'shares': providers.validate_lane_shares({'main': 0.3, 'exit': 0.2, 'low': 0.5}), 'low_shed_s': 1}
        if signals:
            cfg['wallet_signals'] = {'pace_s': 0.25, 'retry_delay_s': 60, 'max_per_pass': 3}
        else:
            cfg['wallet_signals'] = None                                      # LINT2: the shipped example turns it ON
        r = build_runner(cfg, state_dir=root / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='e2e-wallets',
                         clock=clock.time, transport_kwargs={'opener': world.opener, 'clock': clock.time, 'monotonic': clock.monotonic,
                                                             'sleep': clock.sleep, 'rng': lambda: 0.5})
        self.addCleanup(r.store.close)
        paths_injected = False
        tick = T0
        while tick <= END:
            if clock.time() < tick:
                clock.t = tick
            r.position_pass()
            r.candidate_pass()
            if r.wallet_signals is not None:
                r.wallet_signals.signals_pass()
            r.write_health()
            self.assertIsNone(r.halted, r.halted)
            self.assertTrue(r.store.check_invariants())
            if not paths_injected and tick >= T0 + 1500:                 # L07-style path marks, once the early launches are collected
                by_role = {name: t.mint for t, (name, _, _) in zip(tokens, SCENARIO)}
                add_path(r.store, by_role['win1'], [1, 1.5, 2.5], ts=clock.time())
                add_path(r.store, by_role['win2'], [1, 2.2], ts=clock.time())
                add_path(r.store, by_role['rug'], [1, 0.8, 0.1], ts=clock.time())
                paths_injected = True
            tick += 60
        return r, role, world, chain

    def trading_result(self, r, role):
        decisions = [(d['kind'], role[d['mint']], d['action'], tuple(json.loads(d['reasons']))) for d in r.store.rows('decisions', limit=100000)
                     if d['mint'] in role]
        fills = [(role[f['mint']], f['side'], f['qty_raw'], f['sol_lamports'], f['fee_lamports']) for f in r.store.rows('fills', limit=100000)]
        closed = sorted((role[c['mint']], c['state']['reason']) for c in r.store.closed_positions())
        return sorted(decisions), fills, closed, r.store.cash()

    def signals_of(self, r, role):
        out = {}
        for row in r.store.rows('observations', kind='wallet_signals', limit=1000):
            out[role[row['mint']]] = json.loads(row['raw'])
        return out

    def test_end_to_end(self):
        r, role, world, chain = self.run_world(True)
        rows = self.signals_of(r, role)

        # every screened (PASS or REJECT) candidate has exactly one row; the rejected one too
        screened = {role[d['mint']]: d['action'] for d in r.store.rows('decisions', kind='screen', limit=1000) if d['action'] in ('PASS', 'REJECT')}
        self.assertEqual(set(rows), set(screened))
        self.assertEqual(screened['hazard'], 'REJECT')
        self.assertEqual(len(r.store.rows('observations', kind='wallet_signals', limit=1000)), len(SCENARIO))

        # bundle: 4 same-slot buyers, one 3-wallet funding cluster, a 5th buyer outside the window is never counted
        b = rows['bundle']
        self.assertEqual((b['buyers'], b['same_slot_buyers'], b['sniper_count'], b['bundled_wallets']), (5, 4, 4, 4))
        self.assertEqual(b['bundle_score'], 0.8)
        self.assertEqual(b['bundled_supply_pct'], 0.4)                       # 4 x 1e12 of 1e15
        self.assertEqual([c['source'] for c in b['funding_clusters']], [addr(90)])
        self.assertEqual(b['funding_checked'], 3)
        self.assertNotIn(addr(26), b['buyer_wallets'])
        self.assertFalse(b['partial'], b['unavailable'])
        # organic and hazard
        self.assertEqual((rows['organic']['bundle_score'], rows['organic']['sniper_count']), (0.0, 0))
        self.assertEqual((rows['hazard']['bundle_score'], rows['hazard']['bundled_wallets']), (1.0, 2))
        self.assertEqual(rows['hazard']['funding_clusters'][0]['source'], addr(94))

        # hard call cap: no candidate used more than max_calls, and the world saw exactly the rows' calls
        cap = r.wallet_signals.cfg['max_calls']
        self.assertTrue(all(row['calls'] <= cap for row in rows.values()))
        signal_calls = [c for c in chain.calls]
        self.assertEqual(len(signal_calls), sum(row['calls'] for row in rows.values()) + self.failures['n'])

        # outage: the 503 hit a collection, which was retried later and then recorded
        self.assertEqual(self.failures['n'], 1)
        self.assertEqual(set(rows['outage']['unavailable'].values()), {'NO_PRIOR_HISTORY'})    # unfunded fixture wallets, nothing else
        self.assertEqual(rows['outage']['buyers'], 2)
        self.assertGreaterEqual(r.wallet_signals.counts['transient_failures'], 1)

        # smart wallets: outcomes became known, and only the LATE launch (after them) sees the smart buyers
        outcomes = {json.loads(e['payload'])['mint']: json.loads(e['payload'])['outcome'] for e in r.store.rows('events', kind='wallet_outcome', limit=1000)}
        by_mint = {m: n for m, n in role.items()}
        self.assertEqual({by_mint[m]: o for m, o in outcomes.items() if by_mint[m] in ('win1', 'win2', 'rug')},
                         {'win1': 'WIN', 'win2': 'WIN', 'rug': 'RUG'})
        for name in ('bundle', 'organic', 'hazard', 'win1', 'win2', 'rug', 'outage'):
            self.assertEqual(rows[name]['smart_buyers_count'], 0, name)
        late = rows['late']
        self.assertEqual((late['smart_buyers_count'], late['smart_wallets']), (2, [S1, S2]))
        table = r.wallet_signals.wallet_table()[0]
        self.assertEqual({w: (t['wins'], t['rugs']) for w, t in table.items() if w in (S1, S2, S3, R1)},
                         {S1: (2, 0), S2: (2, 0), S3: (0, 1), R1: (0, 1)})
        self.assertFalse(r.wallet_signals.is_smart(table[R1]))

        # health shows the collector; the store stays append-only and reconciles
        health = json.loads((r.state_dir and Path(r.state_dir) / 'health.json').read_text())
        self.assertEqual(health['wallet_signals']['collected'], len(SCENARIO))
        self.assertTrue(r.store.check_invariants())
        # the feature rows reference the signature pages as research evidence (retain_raw = signatures)
        kinds = Counter(o['kind'] for o in r.store.rows('observations', limit=100000))
        self.assertGreaterEqual(kinds['wallet_signals:raw:signatures:0'], len(SCENARIO))
        self.assertEqual(kinds['wallet_signals:raw:tx'], 0)

    def test_the_features_never_change_what_the_desk_does(self):
        with_signals, role_a, *_ = self.run_world(True)
        without, role_b, *_ = self.run_world(False)
        a, b = self.trading_result(with_signals, role_a), self.trading_result(without, role_b)
        self.assertGreater(len(a[1]), 0)                                      # it traded
        self.assertEqual(a, b)
        self.assertIsNone(without.wallet_signals)
        self.assertEqual(without.store.rows('observations', kind='wallet_signals'), [])
        self.assertEqual(json.loads((Path(without.state_dir) / 'health.json').read_text())['wallet_signals'], {'enabled': False})

    def test_run_starts_the_collector_on_its_own_thread_and_stops_it_with_the_runner(self):
        r, *_ = self.run_world(True)
        seen = {}

        def run_loop():
            import threading
            seen['thread'] = threading.current_thread().name
            seen['stop'] = r.wallet_signals.stop
            r.stop.set()
        r.wallet_signals.run_loop = run_loop
        r.run(candidate_interval=0.01, position_interval=0.01, install_signals=False)
        self.assertEqual(seen['thread'], 'wallet_signals')
        self.assertIs(seen['stop'], r.stop)


if __name__ == '__main__':
    unittest.main()
