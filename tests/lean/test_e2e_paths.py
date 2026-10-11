"""SYNTHETIC_TEST_ONLY: L07R/L07R2 path recorder end to end on fakeworld with the REAL lean modules (only HTTP is faked).

The test_e2e_real scenario (20 candidates, a Helius outage, a mid-run restart) runs twice: once with the recorder off
and once with it on, polling every tick on the LOW lane into its own ``paths.sqlite``. With it on:
  * a path is recorded for every hazard-free candidate: the 7 entered ones AND the soft rejects (market cap / liquidity
    out of band, the cost cap, portfolio limits, a no-route quote); never for the 4 hazard rejects;
  * path marks are ``adapters.mark`` of the reference quantity: for an entered position they EQUAL the live position
    loop's vault marks at the same instant;
  * the restart resumes exactly the live paths; the outage is counted in the recorder's health, never raised;
  * the trading is untouched (same fills, decisions, exits, cash) AND ``lean.sqlite`` is identical table by table:
    no path row ever lands in the trader's store, so the notifier and the report never walk one.
"""
import json
import os
import socket
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from collections import Counter
from decimal import Decimal
from pathlib import Path
from unittest import mock

from lean import __main__ as M, adapters as A, paths as L
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


def rows(db, sql, *args):
    return db.db.execute(sql, args).fetchall()


class EndToEndPathsTest(unittest.TestCase):
    maxDiff = None

    def world(self, name, scenario=E.SCENARIO, outage=E.OUTAGE):
        root = Path(os.path.realpath(self.tmp.name)) / name
        root.mkdir()
        clock = FakeTime(T0)
        tokens = [Token(21 + 2 * i, **kwargs) for i, (_, kwargs, _) in enumerate(scenario)]
        world = World(root, tokens, clock)
        world.add_frames([T0 + offset for _, _, offset in scenario])
        if outage:
            world.outages['helius'] = outage
        role = {t.mint: r for t, (r, _, _) in zip(tokens, scenario)}
        return root, clock, world, role

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(E.ROOT / 'config' / 'lean' / 'lean.example.json')
        self.assertTrue(self.cfg['paths']['enabled'])                   # the example config turns it on
        self.assertIsNone(self.cfg['paths']['db'])                      # = <state-dir>/paths.sqlite

    def runner(self, root, clock, world, enabled, start_paths=True):
        cfg = dict(self.cfg, paths=L.config(dict(self.cfg['paths'], enabled=enabled)))
        r = build_runner(cfg, state_dir=root / 'state', discovery_db=world.discovery_db, keys=E.KEYS, code_version='e2e-test',
                         clock=clock.time, transport_kwargs={'opener': world.opener, 'clock': clock.time,
                                                             'monotonic': clock.monotonic, 'sleep': clock.sleep,
                                                             'rng': lambda: 0.5})
        self.assertIsNone(r.paths)                                     # build_runner never opens paths.sqlite
        if start_paths:
            r.l07r_start_paths()                                       # what run() does before its loops start
        return r

    def run_scenario(self, name, enabled):
        root, clock, world, role = self.world(name)
        r = self.runner(root, clock, world, enabled)
        out = {'role': role, 'pairs': [], 'restart': None, 'health_before_restart': None, 'root': root}
        restarted, tick = False, T0
        while tick <= E.END:
            if clock.time() < tick:
                clock.t = tick
            if not restarted and tick >= E.RESTART_AT:
                before = r.paths.active() if r.paths else None
                out['health_before_restart'] = r.health()['paths']
                r.store.close()
                if r.paths:
                    r.paths.db.close()
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
        targets = {m: json.loads(t) for m, t in rows(r.paths.db, 'SELECT mint, target FROM paths')}
        for mint, (value, mark_at, source) in r.marks.items():
            if source != 'vaults' or mint not in targets or positions[mint].qty_raw != targets[mint]['qty_raw']:
                continue                                               # after a TP rung the live mark is of fewer tokens
            found = rows(r.paths.db, 'SELECT net_sol FROM path_marks WHERE mint=? AND ts=?', mint, float(mark_at))
            if found:
                pairs.append((mint, value, Decimal(found[-1][0])))

    @staticmethod
    def trading(r):
        fills = [(f['ts'], f['mint'], f['side'], f['qty_raw'], f['sol_lamports'], f['fee_lamports'])
                 for f in r.store.rows('fills', limit=10000)]
        closed = {c['mint']: c['state']['reason'] for c in r.store.closed_positions()}
        decisions = [(d['kind'], d['mint'], d['action'], d['reasons']) for d in r.store.rows('decisions', limit=10000)]
        return fills, closed, decisions, r.store.cash()

    @staticmethod
    def trader_store_shape(r):
        kinds = Counter(o['kind'] for o in r.store.rows('observations', limit=100000))
        events = Counter(e['kind'] for e in r.store.rows('events', limit=100000))
        return r.store.counts(), kinds, events

    # ------------------------------------------------------------------------------------------------------------
    def test_paths_recorded_for_entered_and_soft_rejected_and_exits_unaffected(self):
        off = self.run_scenario('off', enabled=False)
        on = self.run_scenario('on', enabled=True)
        role, r = on['role'], on['runner']
        db = r.paths.db

        # 1. trading is untouched, and lean.sqlite is IDENTICAL table by table: no path row in the trader's store
        self.assertIsNone(off['runner'].paths)
        self.assertEqual(off['runner'].health()['paths'], {'enabled': False, 'alive': False})
        self.assertEqual(self.trading(off['runner']), self.trading(r))
        self.assertEqual({role[m]: reason for m, reason in self.trading(r)[1].items()}, EXPECTED_EXITS)
        self.assertEqual(self.trader_store_shape(off['runner']), self.trader_store_shape(r))
        _counts, kinds, events = self.trader_store_shape(r)
        self.assertFalse({k for k in list(kinds) + list(events) if 'path' in k})
        self.assertFalse((off['root'] / 'state' / 'paths.sqlite').exists())
        self.assertEqual(db.path, on['root'] / 'state' / 'paths.sqlite')

        # 2. which paths: entered + soft rejects, never hazards; one row per mint (the outage retry decided once)
        cols = ('mint', 'entered', 'stage', 'reason', 'holders_checked', 'target', 'start_ts')
        starts = {role[x[0]]: dict(zip(cols, x)) for x in rows(db, 'SELECT %s FROM paths' % ','.join(cols))}
        self.assertEqual(set(starts), set(role.values()) - HAZARDS)
        self.assertEqual({k for k, v in starts.items() if v['entered']}, ENTERED)
        for k in ENTERED:
            self.assertEqual((json.loads(starts[k]['reason']), json.loads(starts[k]['target'])['basis']), ([], 'fill'))
        ne = {k: (v['stage'], json.loads(v['reason'])) for k, v in starts.items() if not v['entered']}
        self.assertEqual(ne['mcap_high'], ('screen', ['MARKET_CAP_ABOVE_MAX']))
        self.assertEqual(ne['liquidity_low'][0], 'screen')
        self.assertIn('LIQUIDITY_BELOW_MIN', ne['liquidity_low'][1])
        self.assertEqual(starts['mcap_high']['holders_checked'], 0)     # a band reject stops before the holder check
        self.assertEqual(ne['cost'], ('entry', ['COST_BUDGET']))
        for k in ('full_1', 'full_2', 'full_3', 'full_4', 'full_5'):
            self.assertEqual(ne[k][0], 'entry', k)
            self.assertIn('MAX_POSITIONS', ne[k][1], k)
        self.assertEqual(ne['no_route'], ('quote', ['ENTRY_FAILED:HTTP_400']))
        fills = r.store.rows('fills', limit=10000)
        for k in ENTERED:
            mint = next(m for m, x in role.items() if x == k)
            buy = next(f for f in fills if f['mint'] == mint and f['side'] == 'buy')
            target = json.loads(starts[k]['target'])
            self.assertEqual((target['qty_raw'], target['cost_lamports']), (buy['qty_raw'], buy['sol_lamports'] + buy['fee_lamports']))

        # 3. marks: every path is marked, after its start, with adapters.mark of its reference quantity
        marks = rows(db, 'SELECT mint, ts, base_raw, quote_raw, net_sol, status FROM path_marks')
        self.assertEqual({role[m] for m, *_ in marks}, set(starts))
        pcfg = A.paper_config(r.cfg)
        for mint, ts, base, quote, net, status in marks:
            target = json.loads(starts[role[mint]]['target'])
            self.assertGreaterEqual(ts, starts[role[mint]]['start_ts'])
            self.assertEqual(status, 'OK')
            self.assertEqual(Decimal(net), A.mark(target['qty_raw'], base, quote, pool_fee_bps=25, pcfg=pcfg))

        # 4. the SAME function as live: the path mark of a held position equals the position loop's vault mark
        self.assertGreaterEqual(len(on['pairs']), 10)
        self.assertEqual({role[m] for m, _, _ in on['pairs']} - ENTERED, set())
        for mint, live, path in on['pairs']:
            self.assertEqual(live, path, role[mint])

        # 5. the outage is a counted error with gap rows; the restart resumed exactly the live paths
        before, after = on['restart']
        self.assertEqual(after, before)
        self.assertGreater(len(before), 5)
        self.assertGreaterEqual(on['health_before_restart']['errors_by_code'].get('HTTP_503', 0), 1)
        self.assertGreaterEqual(rows(db, "SELECT COUNT(*) FROM path_gaps WHERE cause='HTTP_503'")[0][0], 1)
        health = json.loads((Path(r.state_dir) / 'health.json').read_text())['paths']
        self.assertEqual((health['enabled'], health['alive'], health['resumed'], health['active_paths']),
                         (True, False, len(before), len(starts)))

    def test_a_transient_quote_failure_is_decided_on_the_retry(self):
        """Jupiter 503 during the first attempt's buy quote: the candidate is retried next pass and entered. Its path is
        started ONCE, after the final attempt, as entered (not as 'ENTRY_FAILED' from the first attempt)."""
        root, clock, world, _ = self.world('retry', scenario=[('a', {}, 0)], outage=None)
        world.outages['jupiter'] = (T0, T0 + 30)
        r = self.runner(root, clock, world, True)
        for tick in (T0, T0 + 60):
            clock.t = max(clock.t, tick)
            r.candidate_pass()
            if tick == T0:
                self.assertEqual(len(r.retry), 1)
                self.assertEqual(r.paths.active(), {})                  # undecided: nothing recorded yet
        self.assertEqual([(e, s) for e, s in rows(r.paths.db, 'SELECT entered, stage FROM paths')], [(1, None)])
        self.assertEqual(r.paths.health()['duplicates'], 0)
        r.store.close()

    def test_recorder_lives_only_in_run_mode(self):
        """build_runner and the --once passes never open paths.sqlite; run() builds, resumes and runs it in its own
        thread (health ``alive``); main() --once / --clear-halt never call run()."""
        root, clock, world, _ = self.world('modes', scenario=[('a', {}, 0)], outage=None)
        r = self.runner(root, clock, world, True, start_paths=False)
        r.position_pass(), r.candidate_pass(), r.write_health()
        self.assertFalse((root / 'state' / 'paths.sqlite').exists())
        self.assertEqual(r.health()['paths'], {'enabled': True, 'alive': False})
        done = threading.Thread(target=r.run, kwargs={'install_signals': False, 'candidate_interval': 0.05,
                                                     'position_interval': 0.05})
        done.start()
        try:
            for _ in range(200):
                if r._l07r_thread is not None and r._l07r_thread.is_alive():
                    break
                time.sleep(0.01)
            health = r.health()['paths']
            self.assertTrue((health['enabled'], health['alive']) == (True, True), health)
            self.assertTrue((root / 'state' / 'paths.sqlite').exists())
        finally:
            r.stop.set()
            done.join(10)
        self.assertFalse(done.is_alive())
        self.assertFalse(r.health()['paths']['alive'])
        r.store.close()

        # main(): --once and --clear-halt never reach run() (the only place the recorder is built)
        cfg_path = root / 'lean.json'
        cfg = json.loads((E.ROOT / 'config' / 'lean' / 'lean.example.json').read_text())
        cfg['strategy_config'] = str(E.ROOT / 'config' / 'lean' / 'strategy-default.json')
        cfg_path.write_text(json.dumps(cfg))
        keys = root / 'keys.json'
        keys.write_text(json.dumps({'HELIUS_API_KEY': 'TEST-H', 'JUPITER_API_KEY': 'TEST-J'}))
        os.chmod(keys, stat.S_IRUSR | stat.S_IWUSR)
        for flag in ('--once', '--clear-halt'):
            fake = mock.MagicMock(halted=None)
            with mock.patch.object(M, 'build_runner', return_value=fake), mock.patch('builtins.print'), \
                    mock.patch.object(M.logging, 'basicConfig'):
                M.main(['--config', str(cfg_path), '--state-dir', str(root / 's2'), '--discovery-db', str(world.discovery_db),
                        '--keys-file', str(keys), flag])
            self.assertFalse(fake.run.called, flag)
            self.assertFalse(fake.l07r_start_paths.called, flag)

    def test_absent_paths_key_disables_the_recorder(self):
        root, clock, world, _ = self.world('absent', scenario=[('a', {}, 0)], outage=None)
        cfg = dict(self.cfg)
        del cfg['paths']
        r = build_runner(cfg, state_dir=root / 'state', discovery_db=world.discovery_db, keys=E.KEYS, code_version='e2e-test',
                         clock=clock.time, transport_kwargs={'opener': world.opener, 'clock': clock.time,
                                                             'monotonic': clock.monotonic, 'sleep': clock.sleep})
        self.addCleanup(r.store.close)
        self.assertIsNone(r.l07r_start_paths())
        self.assertEqual(r.health()['paths'], {'enabled': False, 'alive': False})


if __name__ == '__main__':
    unittest.main()
