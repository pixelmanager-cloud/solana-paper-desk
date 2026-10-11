"""SYNTHETIC_TEST_ONLY: L24 end to end with the REAL lean modules (runner, candidates, screen, strategy, paper, store, providers,
wallet_signals, clusters). Only the HTTP layer is faked (tests/lean/fakeworld.py with planted holder token accounts + the
getSignaturesForAddress / getTransaction / getTokenSupply chain of tests/lean/test_wallet_signals.Chain). No network, no credentials.

Scenario (ticks of 60 s; each tick: position pass, candidate pass, wallet-signals pass, clusters pass):
  filler0..3  one 5 % holder each, every one funded by the same exchange-like wallet HUB (a hub after four mints)
  planted     5 wallets funded by one non-hub funder F (5.6 % each), 3 wallets bought in one Jito bundle together with one of them
              (4 % each): 40 % of the supply in ONE cluster; the creator was funded by F too; 4 organic 2 % holders
  hubbed      5 wallets funded by HUB (5.6 % each) at different times: NOT a cluster
and the whole trading result (screens, entries, exits, fills, cash) is IDENTICAL with the collector switched off.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lean import clusters as C
from lean.__main__ import build_runner, load_config
from tests.lean.fakeworld import FakeTime, T0, Token, World
from tests.lean.test_wallet_signals import Chain, addr, block_time
from tests.lean.test_clusters import tip_transfer

ROOT = Path(__file__).resolve().parents[2]
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}
END = T0 + 1500
SUPPLY = 10 ** 15
PCT = SUPPLY // 1000
CREATOR_TAG = 120
CREATOR, F_, HUB = addr(CREATOR_TAG), addr(90), addr(91)
A = [addr(20 + i) for i in range(5)]
B = [addr(30 + i) for i in range(3)]
O = [addr(40 + i) for i in range(4)]
H = [addr(50 + i) for i in range(5)]
Y = [addr(70 + i) for i in range(4)]


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def _account(tag, i):
    """A distinct token-account address per (token, holder)."""
    from desk.security import base58
    return base58(bytes([tag, i + 1]) * 16)


ORGANIC = [(w, 20 * PCT) for w in O]
SCENARIO = [                                       # (role, Token kwargs, ready offset)
    ('filler0', dict(holder_rows=[(Y[0], 50 * PCT)] + ORGANIC[:3]), 0),
    ('filler1', dict(holder_rows=[(Y[1], 50 * PCT)] + ORGANIC[:3]), 60),
    ('filler2', dict(holder_rows=[(Y[2], 50 * PCT)] + ORGANIC[:3]), 120),
    ('filler3', dict(holder_rows=[(Y[3], 50 * PCT)] + ORGANIC[:3]), 180),
    ('planted', dict(holder_rows=[(w, 56 * PCT) for w in A] + [(w, 40 * PCT) for w in B] + ORGANIC, coin_creator_tag=CREATOR_TAG), 600),
    ('hubbed', dict(holder_rows=[(w, 56 * PCT) for w in H] + ORGANIC), 900),
]


def grad_slot(world):
    import sqlite3
    with sqlite3.connect(world.discovery_db) as c:
        payload = json.loads(c.execute('SELECT payload FROM raw_events ORDER BY seq LIMIT 1').fetchone()[0])
    return payload['params']['result']['slot']


def build_chain(by_role, slot):
    chain = Chain()
    # funding, one wallet per slot (no two examined wallets share a slot), all long before every decision
    for i, wallet in enumerate([CREATOR] + A):
        chain.fund(wallet, F_, slot=i)
    for i, wallet in enumerate(B):
        chain.fund(wallet, addr(60 + i), slot=6 + i)
    for i, wallet in enumerate(H + Y):
        chain.fund(wallet, HUB, slot=2000 + 7 * i)
    assert block_time(2100) < T0
    # the planted bundle: four adjacent swaps in one slot of the first pool window, the last one pays a Jito tip
    token = by_role['planted']
    for wallet in [A[0]] + B:
        last = chain.swap(slot + 5, {wallet: 10 ** 12}, mint=token.mint, pool=token.pool)
    chain.bodies[last]['transaction']['message']['instructions'].append(tip_transfer(B[-1]))
    return chain


class ClustersEndToEnd(unittest.TestCase):
    maxDiff = None

    def run_world(self, clusters, overrides=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(os.path.realpath(tmp.name))
        clock = FakeTime(T0)
        tokens = []
        for i, (_, kwargs, _) in enumerate(SCENARIO):
            kwargs = dict(kwargs)
            kwargs['holder_rows'] = [(_account(61 + 2 * i, k), owner, amount) for k, (owner, amount) in enumerate(kwargs['holder_rows'])]
            tokens.append(Token(61 + 2 * i, **kwargs))
        role = {t.mint: name for t, (name, _, _) in zip(tokens, SCENARIO)}
        world = World(root, tokens, clock)
        world.add_frames([T0 + offset for _, _, offset in SCENARIO])
        chain = build_chain({role[t.mint]: t for t in tokens}, grad_slot(world))
        world.extra_rpc = chain.rpc_handlers()
        cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        from lean import providers
        cfg['lanes'] = {'shares': providers.validate_lane_shares({'main': 0.3, 'exit': 0.2, 'low': 0.5}), 'low_shed_s': 1}
        cfg['wallet_signals'] = {'pace_s': 0.25, 'retry_delay_s': 60, 'max_per_pass': 3}
        cfg['clusters'] = {**cfg['clusters'], 'max_per_pass': 3, **(overrides or {})} if clusters else None
        r = build_runner(cfg, state_dir=root / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='e2e-clusters',
                         clock=clock.time, transport_kwargs={'opener': world.opener, 'clock': clock.time, 'monotonic': clock.monotonic,
                                                             'sleep': clock.sleep, 'rng': lambda: 0.5})
        self.addCleanup(r.store.close)
        tick = T0
        while tick <= END:
            if clock.time() < tick:
                clock.t = tick
            r.position_pass()
            r.candidate_pass()
            r.wallet_signals.signals_pass()
            if r.clusters is not None:
                r.clusters.clusters_pass()
            r.write_health()
            self.assertIsNone(r.halted, r.halted)
            self.assertTrue(r.store.check_invariants())
            tick += 60
        return r, role, world, chain

    def trading_result(self, r, role):
        decisions = [(d['kind'], role[d['mint']], d['action'], tuple(json.loads(d['reasons']))) for d in r.store.rows('decisions', limit=100000)
                     if d['mint'] in role]
        fills = [(role[f['mint']], f['side'], f['qty_raw'], f['sol_lamports'], f['fee_lamports']) for f in r.store.rows('fills', limit=100000)]
        closed = sorted((role[c['mint']], c['state']['reason']) for c in r.store.closed_positions())
        return sorted(decisions), fills, closed, r.store.cash()

    def clusters_of(self, r, role):
        out = {}
        for row in r.store.rows('observations', kind='clusters', limit=1000):
            self.assertNotIn(role[row['mint']], out)                          # one row per candidate
            out[role[row['mint']]] = json.loads(row['raw'])
        return out

    def test_end_to_end(self):
        r, role, world, chain = self.run_world(True)
        rows = self.clusters_of(r, role)
        screened = {role[d['mint']] for d in r.store.rows('decisions', kind='screen', limit=1000) if d['action'] in ('PASS', 'REJECT')}
        self.assertEqual(set(rows), screened)
        self.assertEqual(set(rows), {name for name, _, _ in SCENARIO})

        p = rows['planted']
        self.assertEqual((p['cluster_count'], p['largest_cluster_supply_pct'], p['linked_supply_pct_total']), (1, 40.0, 40.0))
        self.assertEqual((p['top_cluster_holders'], p['creator_cluster_supply_pct'], p['jito_bundled_buys']), (8, 40.0, 4))
        self.assertEqual(sorted(p['largest_cluster_wallets']), sorted(A + B))
        self.assertTrue({'shared_funder', 'jito_bundle'} <= set(p['clusters'][0]['edge_types']))
        self.assertEqual((p['holders_source'], p['supply_source'], p['flags'], p['signals_row']), ('screen', 'screen', {}, True))
        self.assertEqual((p['fresh_wallet_share'], p['fresh_checked']), (1.0, 5))
        self.assertEqual(p['window_after_decision_s'], 0.0)

        h = rows['hubbed']
        self.assertEqual((h['cluster_count'], h['linked_supply_pct_total'], h['largest_cluster_supply_pct']), (0, 0.0, 0.0))
        self.assertEqual(h['hub_funders'], [HUB])
        for name in ('filler0', 'filler1', 'filler2', 'filler3'):
            self.assertEqual((rows[name]['cluster_count'], rows[name]['linked_supply_pct_total']), (0, 0.0), name)
        self.assertEqual(rows['filler0']['hub_funders'], [])                     # not yet a hub on the first mint

        # no lookahead: every row is as of its candidate's first screen decision; every fetched transaction is before it
        first_screen = {}
        for d in r.store.rows('decisions', kind='screen', limit=1000):
            if d['action'] in ('PASS', 'REJECT'):
                first_screen.setdefault(role[d['mint']], d['ts'])
        for name, row in rows.items():
            self.assertEqual(row['decision_ts'], first_screen[name])
            self.assertEqual(row['late_s'], round(row['collected_at'] - row['decision_ts'], 3))
            self.assertLessEqual(row['calls'], r.clusters.cfg['max_calls'])
        for method, signature in chain.calls:
            if method == 'getTransaction':
                self.assertLess(chain.bodies[signature]['blockTime'], min(first_screen.values()) + 1)

        # health: the collector is visible; its calls are counted; the store stays append-only and reconciles
        health = json.loads((Path(r.state_dir) / 'health.json').read_text())['clusters']
        self.assertEqual((health['enabled'], health['alive'], health['collected']), (True, False, len(SCENARIO)))
        self.assertEqual(health['calls'], sum(row['calls'] for row in rows.values()))
        self.assertIn('sheds', health)
        self.assertTrue(r.store.check_invariants())

    def test_the_hub_rule_is_what_keeps_the_hub_funded_set_apart(self):
        r, role, *_ = self.run_world(True, {'hub_min_mints': 50})                  # sensitivity: no hub can be detected
        h = self.clusters_of(r, role)['hubbed']
        self.assertEqual((h['cluster_count'], h['top_cluster_holders'], h['linked_supply_pct_total'], h['hub_funders']), (1, 5, 28.0, []))

    def test_the_collector_never_changes_what_the_desk_does(self):
        with_clusters, role_a, *_ = self.run_world(True)
        without, role_b, *_ = self.run_world(False)
        a, b = self.trading_result(with_clusters, role_a), self.trading_result(without, role_b)
        self.assertGreater(len(a[1]), 0)                                           # it traded
        self.assertEqual(a, b)
        self.assertIsNone(without.clusters)
        self.assertEqual(without.store.rows('observations', kind='clusters'), [])
        self.assertEqual(json.loads((Path(without.state_dir) / 'health.json').read_text())['clusters'], {'enabled': False, 'alive': False})

    def test_run_starts_the_collector_on_its_own_thread_and_stops_it_with_the_runner(self):
        r, *_ = self.run_world(True)
        seen = {}

        def run_loop():
            import threading
            seen['thread'] = threading.current_thread().name
            seen['stop'] = r.clusters.stop
            r.stop.set()
        r.clusters.run_loop = run_loop
        r.run(candidate_interval=0.01, position_interval=0.01, install_signals=False)
        self.assertEqual(seen['thread'], 'clusters')
        self.assertIs(seen['stop'], r.stop)
        self.assertIsInstance(r.clusters, C.Clusters)
        self.assertEqual(r.clusters.helius.transport.lane, 'low')


if __name__ == '__main__':
    unittest.main()
