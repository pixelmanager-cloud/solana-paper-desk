"""SYNTHETIC_TEST_ONLY: C2 bonding-curve recorder end to end on fakeworld with the REAL lean modules (only HTTP is faked).

The runner is built by ``build_runner`` from the baseline config plus a ``curves`` block (Helius V2, 2 curves per page so every
poll pages). A fake Helius answers ``getProgramAccountsV2`` the way the documented API does (discriminator filter, dataSlice,
limit/paginationKey, changedSinceSlot, a context slot); everything else is the fake world of tests/lean/fakeworld.py.

Scenario (40 minutes of fake time, ticks of 60 s: position pass, curve poll, path poll, candidate pass):
  graduating   two curves that rise through 0.55..0.97 and complete (their migration frames arrive 300 s later, are screened,
               entered and recorded on PumpSwap by lean.paths) -> a GRADUATED end with a final complete mark, and ONE
               continuous series per mint through ``join_by_mint``
  ignored      a curve at progress 0.2, a non-SOL-quoted curve and a malformed account: counted, never recorded
  abandoned    a curve at 0.7 that never changes: STALE after stale_s
  outage       the curve RPC answers 503 for two ticks: gap rows, no marks, the missed states are recorded afterwards
  restart      a new process on the same state resumes the live curves and the changedSinceSlot cursor, no duplicate marks
  non-intrusive  lean.sqlite is IDENTICAL table by table with the recorder off
"""
import base64
import json
import os
import socket
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from lean import curves as C
from lean.__main__ import build_runner, load_config
from tests.lean.fakeworld import FakeTime, Resp, T0, Token, World
from tests.lean.test_curves import curve_bytes, entry, key

ROOT = Path(__file__).resolve().parents[2]
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}
INITIAL = C.INITIAL_REAL_TOKEN_RESERVES
END = T0 + 40 * 60
RESTART_AT = T0 + 360
OUTAGE = (T0 + 235, T0 + 305)
FLAT = lambda age: Decimal('1.05')
SCEN = [('grad_a', dict(path=FLAT), 720), ('grad_b', dict(path=FLAT), 960), ('hazard', dict(hazard='mint_authority'), 1100),
        ('late', dict(path=FLAT), 1500)]


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def steps(*points):
    """((from_age, progress or 'done'), ...) -> function of seconds since T0."""
    def state(age):
        value = points[0][1]
        for start, v in points:
            if age >= start:
                value = v
        return value
    return state


def account(progress):
    if progress == 'done':
        return curve_bytes(vtr=10 ** 14, vsr=115_000_000_000, rtr=0, rsr=85_000_000_000, complete=True)
    rtr = int(Decimal(INITIAL) * (Decimal(1) - Decimal(str(progress))))
    return curve_bytes(vtr=1_073_000_000_000_000 - rtr // 2, vsr=30_000_000_000 + int(Decimal(str(progress)) * 55_000_000_000),
                       rtr=rtr, rsr=int(Decimal(str(progress)) * 85_000_000_000))


class CurveWorld:
    """The program-account side of the fake chain: curve states over time, the V2 request semantics, an outage window."""

    def __init__(self, tokens, clock):
        self.clock, self.slot = clock, 1000
        by_role = {role: token for token, (role, _, _) in zip(tokens, SCEN)}
        self.states = {
            C.curve_address(by_role['grad_a'].mint): steps((0, .55), (60, .7), (180, .85), (300, .97), (420, 'done')),
            C.curve_address(by_role['grad_b'].mint): steps((0, .6), (300, .8), (480, .95), (660, 'done')),
            key(90): steps((0, .7)),                       # abandoned: never changes
            key(91): steps((0, .2)),                       # below min_progress
        }
        self.bytes_override = {key(92): curve_bytes(quote=key(77)), key(93): b'\x01' * 125}   # non-SOL quote; malformed
        self.last, self.changed = {}, {}
        self.calls, self.outage_calls = [], 0

    def current(self):
        age = self.clock.time() - T0
        out = {pubkey: account(state(age)) for pubkey, state in self.states.items()}
        out.update(self.bytes_override)
        return out

    def answer(self, body):
        t = self.clock.time()
        method, (program, opts) = body['method'], body['params']
        assert method == 'getProgramAccountsV2' and program == C.PUMP
        assert opts['filters'] == [{'memcmp': {'offset': 0, 'bytes': C.DISCRIMINATOR_B58}}] and opts['encoding'] == 'base64'
        assert opts['dataSlice'] == {'offset': 0, 'length': 125} and 'withContext' not in opts
        self.calls.append((t, opts.get('changedSinceSlot'), opts.get('paginationKey')))
        if OUTAGE[0] <= t < OUTAGE[1]:
            self.outage_calls += 1
            return Resp(b'{"error":"unavailable"}', 503)
        self.slot += 1
        now = self.current()
        for pubkey, data in now.items():
            if self.last.get(pubkey) != data:
                self.last[pubkey], self.changed[pubkey] = data, self.slot
        since = opts.get('changedSinceSlot')
        pubkeys = sorted(p for p in now if since is None or self.changed[p] >= since)
        start = int(opts['paginationKey']) if opts.get('paginationKey') else 0
        page = pubkeys[start:start + opts['limit']]
        result = {'accounts': [entry(p, now[p]) for p in page], 'context': {'slot': self.slot}}
        if start + opts['limit'] < len(pubkeys):
            result['paginationKey'] = str(start + opts['limit'])
        return Resp({'jsonrpc': '2.0', 'id': body['id'], 'result': result})


class EndToEndCurvesTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))

    def build_world(self, name):
        root = self.root / name
        root.mkdir()
        clock = FakeTime(T0)
        tokens = [Token(21 + 2 * i, **kw) for i, (_, kw, _) in enumerate(SCEN)]
        world = World(root, tokens, clock)
        world.add_frames([T0 + off for _, _, off in SCEN])
        curves = CurveWorld(tokens, clock)

        def opener(request, timeout):
            body = json.loads(request.data) if request.data else {}
            if body.get('method') == 'getProgramAccountsV2':
                return curves.answer(body)
            return world.opener(request, timeout)
        return root, clock, world, curves, opener, {t.mint: role for t, (role, _, _) in zip(tokens, SCEN)}, tokens

    def runner(self, root, clock, world, opener, enabled):
        cfg = load_config(ROOT / 'tests' / 'lean' / 'lean.baseline.json')
        cfg['lanes'] = {'helius_rps': 50, 'main': 0.35, 'exit': 0.2, 'low': 0.45, 'low_shed_s': 30}     # a paged poll needs several low tokens at once
        cfg['curves'] = C.config({'enabled': enabled, 'method': 'getProgramAccountsV2', 'page_limit': 2, 'max_pages': 5, 'stale_s': 600,
                                  'max_credits_month': 5_000_000, 'min_free_gb': 0})
        r = build_runner(cfg, state_dir=root / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='e2e-curves',
                         clock=clock.time, transport_kwargs={'opener': opener, 'clock': clock.time, 'monotonic': clock.monotonic,
                                                             'sleep': clock.sleep, 'rng': lambda: 0.5})
        r.l07r_start_paths()
        return r

    def run_scenario(self, name, enabled, restart=True):
        root, clock, world, curves, opener, role, tokens = self.build_world(name)
        r = self.runner(root, clock, world, opener, enabled)
        out = {'root': root, 'role': role, 'curves': curves, 'tokens': tokens, 'clock': clock, 'restart': None, 'health': []}
        tick, restarted = T0, False
        while tick <= END:
            if clock.time() < tick:
                clock.t = tick
            if restart and not restarted and tick >= RESTART_AT:
                before = r.curves.live.copy() if r.curves else None
                cursor = r.curves.cursor if r.curves else None
                out['stats_before_restart'] = dict(r.curves.stats) if r.curves else None
                r.store.close()
                r.paths.db.close()
                if r.curves:
                    r.curves.db.close()
                r = self.runner(root, clock, world, opener, enabled)
                restarted = True
                out['restart'] = (sorted(before) if before is not None else None, sorted(r.curves.live) if r.curves else None, cursor,
                                  r.curves.cursor if r.curves else None)
            r.position_pass()
            if r.curves is not None:
                r.curves.poll()
            r.paths.poll()
            r.candidate_pass()
            r.write_health()
            self.assertIsNone(r.halted, r.halted)
            self.assertTrue(r.store.check_invariants())
            tick += 60
        out['runner'] = r
        return out

    @staticmethod
    def dump(path):
        with sqlite3.connect(path) as c:
            names = [n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {n: c.execute('SELECT * FROM %s ORDER BY rowid' % n).fetchall() for n in names}

    def test_end_to_end(self):
        out = self.run_scenario('on', True)
        r, root, role, curves = out['runner'], out['root'], out['role'], out['curves']
        mint = {v: k for k, v in role.items()}
        curve_db = root / 'state' / 'curves.sqlite'
        db = sqlite3.connect(curve_db)
        a, b = C.curve_address(mint['grad_a']), C.curve_address(mint['grad_b'])

        # --- what was recorded and what was not
        started = {row[0] for row in db.execute('SELECT curve FROM curve_paths')}
        self.assertEqual(started, {a, b, key(90)})                     # below min_progress, non-SOL and malformed: never recorded
        stats = out['stats_before_restart']                            # the second process only saw "changed since" nothing
        self.assertEqual((stats['below_min_progress'] >= 1, stats['non_sol'] >= 1, stats['decode_errors'] >= 1), (True, True, True), stats)
        ends = {(c, why) for c, why in db.execute('SELECT curve, why FROM curve_ends')}
        self.assertEqual(ends, {(a, 'GRADUATED'), (b, 'GRADUATED'), (key(90), 'STALE')})
        for curve in (a, b):
            marks = db.execute('SELECT progress, complete, ts FROM curve_marks WHERE curve=? ORDER BY ts', (curve,)).fetchall()
            progress = [Decimal(m[0]) for m in marks]
            self.assertEqual(progress, sorted(progress))               # progress only goes up
            self.assertEqual([m[1] for m in marks], [0] * (len(marks) - 1) + [1])      # exactly the last mark is the complete one
            self.assertEqual(progress[-1], Decimal(1))
            end_ts = db.execute('SELECT ts FROM curve_ends WHERE curve=?', (curve,)).fetchone()[0]
            self.assertEqual(end_ts, marks[-1][2])
        self.assertEqual(db.execute('SELECT COUNT(*) FROM curve_marks WHERE curve=?', (key(90),)).fetchone()[0], 1)   # unchanged: one mark
        stale_end = db.execute('SELECT ts FROM curve_ends WHERE curve=?', (key(90),)).fetchone()[0]
        first_ts = db.execute('SELECT ts FROM curve_marks WHERE curve=?', (key(90),)).fetchone()[0]
        self.assertGreaterEqual(stale_end - first_ts, 600)
        self.assertLess(stale_end - first_ts, 700)

        # --- the outage: gap rows, no marks inside the window, the states missed are recorded afterwards
        gaps = db.execute('SELECT ts, cause FROM curve_poll_gaps').fetchall()
        self.assertEqual({cause for _ts, cause in gaps if OUTAGE[0] <= _ts < OUTAGE[1]}, {'HTTP_503'})
        self.assertEqual(curves.outage_calls, 2)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM curve_marks WHERE ts>=? AND ts<?', OUTAGE).fetchone()[0], 0)
        a_progress = [m[0] for m in db.execute('SELECT progress FROM curve_marks WHERE curve=? ORDER BY ts', (a,))]
        self.assertIn('0.970000', a_progress)                         # the 0.97 state began inside the outage and was caught afterwards

        # --- V2 requests: paged, cursor advanced only by complete polls, always the LOW lane
        self.assertTrue(any(page is not None for _t, _since, page in curves.calls))
        self.assertTrue(any(since is not None for _t, since, _page in curves.calls))
        self.assertEqual(r.curves.helius.transport.lane, 'low')
        self.assertEqual(r.curves.cursor, int(r.curves.db.meta('changed_since_slot')))
        spent = db.execute('SELECT SUM(credits), SUM(calls) FROM curve_polls').fetchone()
        self.assertEqual(spent[0], 10.0 * spent[1])

        # --- restart: live curves and the cursor came from the file; nothing was duplicated
        before, after, cursor_before, cursor_after = out['restart']
        self.assertEqual(before, after)
        self.assertEqual(sorted(before), sorted([a, b, key(90)]))      # all three were live at the restart (tick 360)
        self.assertEqual(cursor_before, cursor_after)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM (SELECT curve, ts FROM curve_marks GROUP BY curve, ts HAVING COUNT(*)>1)').fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM curve_paths').fetchone()[0], 3)

        # --- one continuous series per graduated mint: curve marks, then the PumpSwap pool marks of lean.paths
        for role_name in ('grad_a', 'grad_b'):
            joined = C.join_by_mint(curve_db, root / 'state' / 'paths.sqlite', mint[role_name])
            sources = [s['source'] for s in joined['series']]
            self.assertGreater(joined['curve_marks'], 3, role_name)
            self.assertGreater(joined['pool_marks'], 3, role_name)
            first_pool = sources.index('pool')
            self.assertEqual(sources, ['curve'] * first_pool + ['pool'] * (len(sources) - first_pool))
            self.assertEqual(joined['ends'][0]['why'], 'GRADUATED')
            self.assertLessEqual(joined['ends'][0]['ts'], joined['series'][first_pool]['ts'])
            times = [s['ts'] for s in joined['series']]
            self.assertEqual(times, sorted(times))
            self.assertTrue(all(Decimal(s['price_sol']) > 0 for s in joined['series']))
        late = C.join_by_mint(curve_db, root / 'state' / 'paths.sqlite', mint['late'])
        self.assertEqual(late['curve_marks'], 0)                       # a mint whose curve was never seen has no curve phase

        # --- health: one entry, plain JSON, with the call/credit estimate
        health = json.loads((root / 'state' / 'health.json').read_text())['curves']
        self.assertTrue(health['enabled'] and health['alive'] is False)    # no thread in this test (the loop drives poll())
        self.assertEqual(health['graduated'], r.curves.stats['graduated'])
        self.assertIn('credits_per_month', health['estimate'])

        # --- no key anywhere in the file
        raw = curve_db.read_bytes()
        for secret in KEYS.values():
            self.assertNotIn(secret.encode(), raw)

    def test_the_trader_is_untouched_by_the_recorder(self):
        on = self.run_scenario('on', True)
        off = self.run_scenario('off', False)
        self.assertIsNone(off['runner'].curves)
        a, b = self.dump(on['root'] / 'state' / 'lean.sqlite'), self.dump(off['root'] / 'state' / 'lean.sqlite')
        self.assertEqual(set(a), set(b))
        for table in a:
            self.assertEqual(a[table], b[table], table)
        self.assertGreaterEqual(len(a['fills']), 3)                     # it did trade
        self.assertFalse((off['root'] / 'state' / 'curves.sqlite').exists())
        self.assertNotIn('curves', {name for (name,) in sqlite3.connect(on['root'] / 'state' / 'lean.sqlite').execute(
            "SELECT name FROM sqlite_master WHERE type='table'")})

    def test_config_is_strict_and_off_by_default(self):
        cfg = load_config(ROOT / 'tests' / 'lean' / 'lean.baseline.json')
        self.assertFalse(cfg['curves']['enabled'])
        raw = json.loads((ROOT / 'tests' / 'lean' / 'lean.baseline.json').read_text())
        raw['curves'] = {'enabled': True, 'methd': 'getProgramAccounts'}
        bad = self.root / 'bad.json'
        bad.write_text(json.dumps(raw).replace('"../../config', '"' + str(ROOT / 'config')))
        from lean.__main__ import ConfigError
        with self.assertRaises(ConfigError):
            load_config(bad)
        raw['curves'] = {'enabled': True, 'method': 'getProgramAccountsV2'}
        bad.write_text(json.dumps(raw).replace('"../../config', '"' + str(ROOT / 'config')))
        self.assertTrue(load_config(bad)['curves']['enabled'])

    def test_runner_thread_wiring(self):
        """run() starts the recorder as an auxiliary daemon wired to the runner's stop event."""
        root, clock, world, curves, opener, role, tokens = self.build_world('wired')
        r = self.runner(root, clock, world, opener, True)
        specs = {name: (target, args) for name, target, args in r._aux_specs()}
        self.assertIn('curves', specs)
        self.assertIs(r.curves.stop, r.stop)
        self.assertEqual(specs['curves'][0].__func__, C.CurveRecorder.run_loop)
        off_root = self.build_world('wired-off')
        off = self.runner(off_root[0], off_root[1], off_root[2], off_root[4], False)
        self.assertNotIn('curves', {name for name, _t, _a in off._aux_specs()})
        self.assertEqual(off.health()['curves'], {'enabled': False, 'alive': False})


if __name__ == '__main__':
    unittest.main()
