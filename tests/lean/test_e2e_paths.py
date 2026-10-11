"""SYNTHETIC_TEST_ONLY: L07R path recorder end to end on fakeworld with the REAL lean modules (only HTTP is faked).

The test_e2e_real scenario (20 candidates, a Helius outage, a mid-run restart) runs twice: once with the recorder off
and once with it on, polling every tick on the LOW lane. With it on:
  * a path is recorded for every hazard-free candidate: the 7 entered ones AND the soft rejects (market cap / liquidity
    out of band, the cost cap, portfolio limits, a no-route quote); never for the 4 hazard rejects;
  * path marks are ``adapters.mark`` of the reference quantity: for an entered position they EQUAL the live position
    loop's vault marks at the same instant;
  * the restart resumes exactly the live paths; the outage is counted in the recorder's health, never raised;
  * the trading is untouched: the same fills, in the same order, at the same prices, and the same exit reasons.
"""
import json
import os
import socket
import tempfile
import unittest
from collections import Counter
from decimal import Decimal
from pathlib import Path
from unittest import mock

from lean import adapters as A, paths as L
from lean.__main__ import build_runner, load_config
from tests.lean import test_e2e_real as E
from tests.lean.fakeworld import FakeTime, T0, Token, World

HAZARDS = {'mint_authority', 'freeze_authority', 'lp_outstanding', 'concentrated'}
ENTERED = {'stop', 'ladder', 'time', 'tp1_stop', 'quick_stop', 'outage', 'late_stop'}
EXPECTED_EXITS = {'stop': 'STOP', 'ladder': 'TRAILING_STOP', 'time': 'TIME_STOP', 'tp1_stop': 'STOP', 'quick_stop': 'STOP',
                  'outage': 'TIME_STOP', 'late_stop': 'STOP'}


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class EndToEndPathsTest(unittest.TestCase):
    maxDiff = None

    def world(self, name):
        root = Path(os.path.realpath(self.tmp.name)) / name
        root.mkdir()
        clock = FakeTime(T0)
        tokens = [Token(21 + 2 * i, **kwargs) for i, (_, kwargs, _) in enumerate(E.SCENARIO)]
        world = World(root, tokens, clock)
        world.add_frames([T0 + offset for _, _, offset in E.SCENARIO])
        world.outages['helius'] = E.OUTAGE
        role = {t.mint: r for t, (r, _, _) in zip(tokens, E.SCENARIO)}
        return root, clock, world, role

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(E.ROOT / 'config' / 'lean' / 'lean.example.json')
        self.assertTrue(self.cfg['paths']['enabled'])                   # the example config turns it on

    def runner(self, root, clock, world, enabled):
        cfg = dict(self.cfg, paths=L.config(dict(self.cfg['paths'], enabled=enabled)))
        return build_runner(cfg, state_dir=root / 'state', discovery_db=world.discovery_db, keys=E.KEYS, code_version='e2e-test',
                            clock=clock.time, transport_kwargs={'opener': world.opener, 'clock': clock.time,
                                                                'monotonic': clock.monotonic, 'sleep': clock.sleep,
                                                                'rng': lambda: 0.5})

    def run_scenario(self, name, enabled):
        root, clock, world, role = self.world(name)
        r = self.runner(root, clock, world, enabled)
        out = {'role': role, 'pairs': [], 'restart': None, 'health_before_restart': None}
        restarted, tick = False, T0
        while tick <= E.END:
            if clock.time() < tick:
                clock.t = tick
            if not restarted and tick >= E.RESTART_AT:
                before = r.paths.active() if r.paths else None
                out['health_before_restart'] = r.health()['paths']
                r.store.close()
                r = self.runner(root, clock, world, enabled)
                restarted = True
                out['restart'] = (before, r.paths.active() if r.paths else None)
            if r.paths is not None:
                at = clock.time()
                r.paths.poll()                                         # the recorder thread's poll, at the tick
                self.assertEqual(clock.time(), at)                     # the low lane never slept the trader's clock
            r.position_pass()
            if r.paths is not None:
                self.collect_pairs(r, out['pairs'])
            r.candidate_pass()
            r.write_health()
            self.assertIsNone(r.halted, r.halted)
            self.assertTrue(r.store.check_invariants())
            tick += 60
        out['runner'] = r
        return out

    @staticmethod
    def collect_pairs(r, pairs):
        """(live vault mark, path mark) for each held position marked by vaults at this tick, at its initial quantity."""
        positions = r.store.positions()
        latest = {}
        for row in r.store.rows('observations', kind='path_mark', limit=100000)[-50:]:
            latest[row['mint']] = row
        starts = {row['mint']: json.loads(row['meta']) for row in r.store.rows('observations', kind='path_start', limit=1000)}
        for mint, (value, mark_at, source) in r.marks.items():
            row = latest.get(mint)
            if source != 'vaults' or row is None or int(row['ts']) != mark_at or mint not in starts:
                continue
            if positions[mint].qty_raw != starts[mint]['target']['qty_raw']:
                continue                                               # after a TP rung the live mark is of fewer tokens
            pairs.append((mint, value, Decimal(json.loads(row['meta'])['net_sol'])))

    @staticmethod
    def trading(r):
        fills = [(f['ts'], f['mint'], f['side'], f['qty_raw'], f['sol_lamports'], f['fee_lamports'])
                 for f in r.store.rows('fills', limit=10000)]
        closed = {c['mint']: c['state']['reason'] for c in r.store.closed_positions()}
        decisions = [(d['kind'], d['mint'], d['action'], d['reasons']) for d in r.store.rows('decisions', limit=10000)]
        return fills, closed, decisions, r.store.cash()

    # ------------------------------------------------------------------------------------------------------------
    def test_paths_recorded_for_entered_and_soft_rejected_and_exits_unaffected(self):
        off = self.run_scenario('off', enabled=False)
        on = self.run_scenario('on', enabled=True)
        role, r = on['role'], on['runner']
        store = r.store

        # 1. trading is untouched: the same fills (time, size, price), decisions and exits as without the recorder
        self.assertIsNone(off['runner'].paths)
        self.assertEqual(off['runner'].health()['paths'], {'enabled': False})
        self.assertEqual(self.trading(off['runner']), self.trading(r))
        closed = {role[m]: reason for m, reason in self.trading(r)[1].items()}
        self.assertEqual(closed, EXPECTED_EXITS)                         # the test_e2e_real exits

        # 2. which paths: entered + soft rejects, never hazards; one path_start per mint (the outage retry decided once)
        starts = {}
        for row in store.rows('observations', kind='path_start', limit=1000):
            self.assertNotIn(role[row['mint']], starts)
            starts[role[row['mint']]] = json.loads(row['meta'])
        self.assertEqual(set(starts), set(role.values()) - HAZARDS)
        self.assertEqual({k for k, v in starts.items() if v['entered']}, ENTERED)
        for k in ENTERED:
            self.assertEqual((starts[k]['not_entered'], starts[k]['target']['basis']), (None, 'fill'))
        ne = {k: v['not_entered'] for k, v in starts.items() if not v['entered']}
        self.assertEqual(ne['mcap_high'], {'stage': 'screen', 'reasons': ['MARKET_CAP_ABOVE_MAX']})
        self.assertEqual(ne['liquidity_low']['stage'], 'screen')
        self.assertIn('LIQUIDITY_BELOW_MIN', ne['liquidity_low']['reasons'])
        self.assertFalse(starts['mcap_high']['holders_checked'])        # a band reject stops before the holder check
        self.assertEqual(ne['cost'], {'stage': 'entry', 'reasons': ['COST_BUDGET']})
        for k in ('full_1', 'full_2', 'full_3', 'full_4', 'full_5'):
            self.assertEqual(ne[k]['stage'], 'entry', k)
            self.assertIn('MAX_POSITIONS', ne[k]['reasons'], k)
        self.assertEqual(ne['no_route'], {'stage': 'quote', 'reasons': ['ENTRY_FAILED:HTTP_400']})
        fills = store.rows('fills', limit=10000)
        for k in ENTERED:
            mint = next(m for m, x in role.items() if x == k)
            first_buy = next(f for f in fills if f['mint'] == mint and f['side'] == 'buy')
            self.assertEqual(starts[k]['target']['qty_raw'], first_buy['qty_raw'])

        # 3. marks: every path is marked, after its start, with adapters.mark of its reference quantity
        marks = store.rows('observations', kind='path_mark', limit=100000)
        by_mint = Counter(role[m['mint']] for m in marks)
        self.assertEqual(set(by_mint), set(starts))
        pcfg = A.paper_config(r.cfg)
        for row in marks:
            meta, start = json.loads(row['meta']), starts[role[row['mint']]]
            self.assertGreaterEqual(row['ts'], start['started'])
            if meta['status'] == 'OK':
                self.assertEqual(Decimal(meta['net_sol']), A.mark(start['target']['qty_raw'], meta['base_raw'], meta['quote_raw'],
                                                                  pool_fee_bps=25, pcfg=pcfg))
        self.assertEqual({row['mint'] for row in marks} - {row['mint'] for row in marks if json.loads(row['meta'])['status'] == 'OK'}, set())

        # 4. the SAME function as live: the path mark of a held position equals the position loop's vault mark
        self.assertGreaterEqual(len(on['pairs']), 10)
        self.assertEqual({role[m] for m, _, _ in on['pairs']} - ENTERED, set())
        for mint, live, path in on['pairs']:
            self.assertEqual(live, path, role[mint])

        # 5. the outage is a counted recorder error; the restart resumed exactly the live paths
        before, after = on['restart']
        self.assertEqual(after, before)
        self.assertGreater(len(before), 5)
        self.assertGreaterEqual(on['health_before_restart']['errors_by_code'].get('HTTP_503', 0), 1)
        health = json.loads((Path(r.state_dir) / 'health.json').read_text())['paths']
        self.assertEqual((health['enabled'], health['resumed'], health['active_paths']), (True, len(before), len(starts)))
        self.assertEqual(health['low_lane']['shed_backoff'], 0)

    def test_a_transient_quote_failure_is_decided_on_the_retry(self):
        """Jupiter 503 during the first attempt's buy quote: the candidate is retried next pass and entered. Its path is
        started ONCE, after the final attempt, as entered (not as 'ENTRY_FAILED' from the first attempt)."""
        root = Path(os.path.realpath(self.tmp.name)) / 'retry'
        root.mkdir()
        clock = FakeTime(T0)
        token = Token(21)
        world = World(root, [token], clock)
        world.add_frames([T0])
        world.outages['jupiter'] = (T0, T0 + 30)
        r = self.runner(root, clock, world, True)
        for tick in (T0, T0 + 60):
            clock.t = max(clock.t, tick)
            r.candidate_pass()
            if tick == T0:
                self.assertEqual(len(r.retry), 1)
                self.assertEqual(r.paths.active(), {})                  # undecided: nothing recorded yet
        starts = r.store.rows('observations', kind='path_start', limit=10)
        self.assertEqual(len(starts), 1)
        meta = json.loads(starts[0]['meta'])
        self.assertEqual((meta['entered'], meta['not_entered'], meta['target']['basis']), (True, None, 'fill'))
        self.assertEqual(r.paths.health()['duplicates'], 0)
        r.store.close()

    def test_run_starts_the_recorder_thread_and_absent_config_disables_it(self):
        root, clock, world, _ = self.world('thread')
        r = self.runner(root, clock, world, True)
        with mock.patch.object(r.paths, 'run') as run:
            r.stop.set()
            self.assertTrue(r.run(install_signals=False))
        run.assert_called_once_with(r.stop)
        r.store.close()
        cfg = dict(self.cfg)
        del cfg['paths']
        r2 = build_runner(cfg, state_dir=root / 'state', discovery_db=world.discovery_db, keys=E.KEYS, code_version='e2e-test',
                          clock=clock.time, transport_kwargs={'opener': world.opener, 'clock': clock.time,
                                                              'monotonic': clock.monotonic, 'sleep': clock.sleep})
        self.addCleanup(r2.store.close)
        self.assertIsNone(r2.paths)


if __name__ == '__main__':
    unittest.main()
