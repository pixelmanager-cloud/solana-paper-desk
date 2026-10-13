"""SYNTHETIC_TEST_ONLY: C3 end to end with the REAL lean modules (runner, candidates, screen, strategy, paper, store, providers
low lane, census). Only the HTTP layer is faked (tests/lean/fakeworld.py + the two RPC methods of tests/lean/census_chain.py).
No network, no credentials.

Scenario (3 tokens traded as in the other e2e tests; ticks of 60 s; the census polls every 600 s like its run_loop would):
  LaunchLab  baseline history, then creates / swaps / a failed create spread over six intervals
  DBC        a quiet launchpad (one create, matched by its own, different, log pattern)
  and the trading result (screens, entries, exits, fills, cash) is IDENTICAL with the census switched off.
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from lean import census as K
from lean.__main__ import build_runner, load_config
from tests.lean.census_chain import DBC, LAUNCHLAB, LaunchChain
from tests.lean.fakeworld import FakeTime, T0, Token, World

ROOT = Path(__file__).resolve().parents[2]
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}
END = T0 + 3700
INTERVAL = 600


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


SCENARIO = [('stop', dict(path=steps((600, 0.7))), 0), ('hazard', dict(hazard='mint_authority'), 60), ('flat', dict(), 120)]


def build_chain():
    chain = LaunchChain()
    chain.add(LAUNCHLAB, T0 - 500, 'create')                                  # history before the first poll: baseline only
    chain.add(LAUNCHLAB, T0 - 400, 'swap')
    chain.add(DBC, T0 - 300, 'swap')
    plan = [(T0 + 100, 'create'), (T0 + 200, 'swap'), (T0 + 300, 'swap'),                       # interval 1: 1 create of 3
            (T0 + 700, 'create'), (T0 + 710, 'create'), (T0 + 720, 'swap'),                     # interval 2: 2 of 3
            (T0 + 1300, 'swap'),                                                                # interval 3: 0 of 1 (+ a failed one below)
            (T0 + 2500, 'create'), (T0 + 2510, 'swap'), (T0 + 2520, 'swap'), (T0 + 2530, 'create')]   # interval 5: 2 of 4
    for t, kind in plan:
        chain.add(LAUNCHLAB, t, kind)
    chain.add(LAUNCHLAB, T0 + 1310, 'create', err={'InstructionError': [0, {'Custom': 6001}]})
    chain.add(DBC, T0 + 1900, 'create')
    return chain


class CensusEndToEnd(unittest.TestCase):
    maxDiff = None

    def run_world(self, census):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(os.path.realpath(tmp.name))
        clock = FakeTime(T0)
        tokens = [Token(81 + 2 * i, **kw) for i, (_, kw, _) in enumerate(SCENARIO)]
        role = {t.mint: name for t, (name, _, _) in zip(tokens, SCENARIO)}
        world = World(root, tokens, clock)
        world.add_frames([T0 + off for _, _, off in SCENARIO])
        chain = build_chain()
        world.extra_rpc = chain.rpc_handlers()
        cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        from lean import flow, sell_evidence
        cfg['wallet_signals'], cfg['clusters'] = None, None
        cfg['sell_evidence'], cfg['flow'] = sell_evidence.config(None), flow.config(None)
        cfg['census'] = K.config({'enabled': True, 'pump_graduations': False, 'interval_s': INTERVAL}) if census else None
        r = build_runner(cfg, state_dir=root / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='e2e-c3',
                         clock=clock.time, transport_kwargs={'opener': world.opener, 'clock': clock.time, 'monotonic': clock.monotonic,
                                                             'sleep': clock.sleep, 'rng': lambda: 0.5})
        self.addCleanup(r.store.close)
        tick, next_poll = T0, T0
        while tick <= END:
            if clock.time() < tick:
                clock.t = tick
            r.position_pass()
            r.candidate_pass()
            if r.census is not None and tick >= next_poll:
                r.census.poll_once()
                next_poll += INTERVAL
            r.write_health()
            self.assertIsNone(r.halted, r.halted)
            self.assertTrue(r.store.check_invariants())
            tick += 60
        return r, role, world, chain, root

    def trading_result(self, r, role):
        decisions = sorted((d['kind'], role[d['mint']], d['action'], tuple(json.loads(d['reasons']))) for d in r.store.rows('decisions', limit=100000)
                           if d['mint'] in role)
        fills = [(role[f['mint']], f['side'], f['qty_raw'], f['sol_lamports'], f['fee_lamports']) for f in r.store.rows('fills', limit=100000)]
        return decisions, fills, r.store.cash()

    def test_counts_per_interval_and_no_effect_on_trading(self):
        on, role, world, chain, root = self.run_world(True)
        rows = on.census.db.rows('raydium_launchlab')
        self.assertEqual([r['note'] for r in rows][:1], ['BASELINE'])
        by_interval = [(r['signatures'], r['failed'], r['new_count'], r['exact']) for r in rows[1:]]
        # polls at T0 (baseline), T0+600 (sees +100..+300), +1200 (+700..+720), +1800 (+1300, a failed create at +1310), +2400 (none),
        # +3000 (+2500..+2530), +3600 (none)
        self.assertEqual(by_interval, [(3, 0, 1, 1), (3, 0, 2, 1), (2, 1, 0, 1), (0, 0, 0, 1), (4, 0, 2, 1), (0, 0, 0, 1)])
        self.assertEqual(sum(r['new_count'] for r in rows[1:]), 5)
        dbc = on.census.db.rows('meteora_dbc')
        self.assertEqual([r['new_count'] for r in dbc], [None, 0, 0, 0, 1, 0, 0])             # one create at +1900, seen by the +2400 poll
        self.assertEqual([r['signatures'] for r in dbc][4], 1)
        self.assertEqual(len(json.loads(rows[1]['sample_mints'])), 1)
        # the census wrote only to its own database
        self.assertTrue((root / 'state' / 'census.sqlite').exists())
        self.assertEqual(on.store.rows('observations', kind='census', limit=10), [])
        # it ran on the low lane, within its credit budget, and the cursors followed the chain
        census_calls = [c for c in world.calls if c[0] == 'helius' and c[1] in ('getSignaturesForAddress', 'getTransaction')]
        self.assertGreater(len(census_calls), 14)
        self.assertLessEqual(len(census_calls), on.census.budget.limit * 2)
        health = on.health()['census']
        self.assertTrue(health['enabled'])
        self.assertEqual(health['worst_case_credits_per_month'], K.worst_case_credits_per_month(on.census.cfg))
        self.assertIn('census', [name for name, _, _ in on._aux_specs()])

        off, role_off, *_ = self.run_world(False)
        self.assertIsNone(off.census)
        self.assertFalse((_[-1] / 'state' / 'census.sqlite').exists())
        self.assertFalse(off.health()['census']['enabled'])
        self.assertEqual(self.trading_result(on, role), self.trading_result(off, role_off))
        self.assertGreater(len(self.trading_result(on, role)[1]), 0)                         # something was actually traded

    def test_run_loop_polls_at_once_and_stops_on_the_runners_stop_event(self):
        r, role, world, chain, root = self.run_world(True)
        c = r.census
        before = c.counts['polls']
        c.stop = threading.Event()
        thread = threading.Thread(target=c.run_loop, daemon=True)
        thread.start()
        deadline = 200
        while c.counts['polls'] == before and deadline:
            c.stop.wait(0.01)
            deadline -= 1
        c.stop.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(c.counts['polls'], before + 1)


if __name__ == '__main__':
    unittest.main()
