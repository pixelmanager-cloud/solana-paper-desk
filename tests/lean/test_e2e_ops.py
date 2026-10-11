"""SYNTHETIC_TEST_ONLY: lean ops end to end with the REAL runner, providers, store, regime log and credit tracker.
Only the HTTP layer is faked (tests/lean/fakeworld.py); discovery is a real discovery database. No network, no credentials.

Scenario (8 tokens, ~95 minutes of fake time, ticks of 60 s; each tick runs position pass, candidate pass and one ops
housekeeping step, exactly what the runner's threads do):
  counting       every HTTP attempt of every provider (including the 503 outage window) is counted per provider/method
  credits        a small monthly budget makes the projection exceed it: low-priority lanes are shed (one CREDIT_SHED row per lane),
                 screening and exits are never shed; a huge budget never sheds
  regime         a regime row every 5 minutes with SOL/USD (the fake Kraken price moves), the 1h change after an hour, candidates/hour
  watchdog       a real AF_UNIX NOTIFY_SOCKET receives WATCHDOG=1 while both loops are fresh; a position loop that stops beating
                 withholds the ping, is reported once (WATCHDOG_STALL) and recovers
  restart        a new process on the same state continues the month's credit usage and the regime history
  non-intrusive  the paper fills are IDENTICAL with ops disabled
"""
import json
import os
import socket
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from lean import ops, report
from lean.__main__ import build_runner, load_config
from tests.lean.fakeworld import FakeTime, Resp, T0, Token, World

ROOT = Path(__file__).resolve().parents[2]
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}
END = T0 + 95 * 60


def steps(*points):
    def path(age):
        value = Decimal(1)
        for start, m in points:
            if age >= start:
                value = Decimal(str(m))
        return value
    return path


SCENARIO = [
    ('stop', dict(path=steps((600, 0.7))), 0),
    ('mint_authority', dict(hazard='mint_authority'), 60),
    ('flat', dict(path=steps((0, 1.05))), 120),
    ('ladder', dict(path=steps((300, 1.5), (900, 2.2), (1500, 3.2), (2100, 3.6), (2700, 2.3))), 180),
    ('concentrated', dict(hazard='concentrated'), 240),
    ('flat2', dict(path=steps((0, 1.05))), 300),
    ('late', dict(path=steps((600, 0.7))), 2400),
    ('no_route', dict(no_route=True), 2460),
]
OUTAGE = (T0 + 600, T0 + 640)


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=lambda *a, **k: (_ for _ in ()).throw(AssertionError('network used'))
                                if False else None)
    # the AF_UNIX notify socket needs connect(); only forbid INET connects
    real_connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError('network used')
        return real_connect(self, address)
    patcher = mock.patch.object(socket.socket, 'connect', guarded)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class MovingKraken:
    """Same world, but api.kraken.com answers a SOL/USD price that rises 10% per hour from 150."""

    def __init__(self, world, clock):
        self.world, self.clock = world, clock

    def __call__(self, request, timeout):
        if 'api.kraken.com' not in request.full_url:
            return self.world.opener(request, timeout)
        t = self.clock.time()
        self.world.calls.append(('kraken', 'Trades', t))
        price = Decimal(150) * (1 + Decimal('0.1') * Decimal(repr(t - T0)) / 3600)
        trade = format(Decimal(repr(t)) - 2, 'f')
        return Resp(('{"error":[],"result":{"SOLUSD":[["%s","0.5",%s,"b","l","",123456]],"last":"1"}}' % (format(price, '.5f'), trade)).encode())


class OpsEndToEnd(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.state = self.root / 'state'
        self.sock_path = str(self.root / 'notify.sock')
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(self.server.close)
        self.server.bind(self.sock_path)
        self.server.setblocking(False)
        self.messages, self.consumed = [], 0

    def fresh_root(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.state = self.root / 'state'

    def world(self, moving=False):
        self.clock = FakeTime(T0)
        tokens = [Token(21 + 2 * i, **kw) for i, (_, kw, _) in enumerate(SCENARIO)]
        self.role = {t.mint: role for t, (role, _, _) in zip(tokens, SCENARIO)}
        world = World(self.root, tokens, self.clock)
        world.add_frames([T0 + off for _, _, off in SCENARIO])
        world.outages['helius'] = OUTAGE
        self.fake = world
        self.opener = MovingKraken(world, self.clock) if moving else world.opener
        return world

    def config(self, **ops_overrides):
        cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        if ops_overrides.pop('_off', False):
            cfg['ops'] = None
        elif cfg['ops'] is not None:
            cfg['ops'] = ops.load_ops_config({'watchdog_stale_s': 180, 'regime_interval_s': 300, 'credit_flush_s': 60, **ops_overrides})
        return cfg

    def runner(self, cfg, state=None, systemd=True):
        env = {'NOTIFY_SOCKET': self.sock_path, 'WATCHDOG_USEC': '20000000'} if systemd else {}
        with mock.patch.dict(os.environ, env):
            if not systemd:
                os.environ.pop('WATCHDOG_USEC', None)
                os.environ.pop('NOTIFY_SOCKET', None)
            return build_runner(cfg, state_dir=state or self.state, discovery_db=self.fake.discovery_db, keys=KEYS,
                                code_version='e2e-ops', clock=self.clock.time,
                                transport_kwargs={'opener': self.opener, 'clock': self.clock.time, 'monotonic': self.clock.monotonic,
                                                  'sleep': self.clock.sleep, 'rng': lambda: 0.5})

    def drain(self):
        """Read every datagram now queued (systemd always drains its socket; so must the test, or sendall blocks)."""
        while True:
            try:
                self.messages.append(self.server.recv(64))
            except BlockingIOError:
                return

    def pings(self):
        """Datagrams received since the previous call."""
        self.drain()
        out, self.messages, self.consumed = self.messages[self.consumed:], self.messages, len(self.messages)
        return out

    def drive(self, r, until, *, positions=True, tick=60, watchdog=True):
        while self.clock.time() < until:
            self.clock.t = self.clock.time() + tick
            if positions:
                r.position_pass()
            r.candidate_pass()
            if r.ops is not None:
                r.ops.housekeeping_once()
                if watchdog:
                    r.ops.watchdog.check()
                    self.drain()
            r.write_health()
            self.assertIsNone(r.halted, r.halted)
            self.assertTrue(r.store.check_invariants())
            self.assertTrue(r.store.check_position_states())

    # ---------------------------------------------------------------------------------------------------------
    def test_ops_scenario(self):
        self.world(moving=True)
        r = self.runner(self.config(credit_budget_month=3000))
        self.assertIsNotNone(r.ops)
        self.drive(r, END)
        store = r.store

        # --- counting: every HTTP attempt, per provider and method, equals what the fake world served
        served = {}
        for provider, method, _t in self.fake.calls:
            served.setdefault(provider, {}).setdefault({'Trades': 'sol_usd', 'OUTAGE': None}.get(method, method), 0)
            if method != 'OUTAGE':
                served[provider][{'Trades': 'sol_usd'}.get(method, method)] += 1
        counted = r.ops.credits.health()['calls']
        outage_hits = sum(1 for p, m, _ in self.fake.calls if m == 'OUTAGE')
        self.assertGreater(outage_hits, 0)
        for provider in ('jupiter', 'kraken'):
            self.assertEqual(counted[provider], {m: n for m, n in served[provider].items() if m}, provider)
        helius_served = {m: n for m, n in served['helius'].items() if m}
        self.assertEqual(sum(counted['helius'].values()), sum(helius_served.values()) + outage_hits)   # failed attempts cost credits too
        for method, n in helius_served.items():
            self.assertGreaterEqual(counted['helius'][method], n)
        self.assertEqual(r.ops.credits.health()['credits_used'], float(sum(counted['helius'].values())))   # 1 credit each, others free

        # --- credits: the projection exceeds the small budget, low-priority lanes are shed, the trader is not
        health = r.ops.credits.health()
        self.assertEqual(health['status'], 'SHEDDING')
        self.assertGreater(health['projected_month'], 3000)
        self.assertEqual(health['shed_lanes'], ['features', 'paths', 'wallet_signals'])
        self.assertFalse(r.ops.credits.allow('paths'))
        self.assertFalse(r.ops.credits.allow('wallet_signals'))
        for lane in ('screening', 'exit', 'marks'):
            self.assertTrue(r.ops.credits.allow(lane))
        shed_rows = [e for e in store.rows('errors', limit=10000) if e['code'] == 'CREDIT_SHED']
        self.assertEqual(sorted(e['message'] for e in shed_rows), ['paths', 'wallet_signals'])        # once per lane that asked
        fills = store.rows('fills', limit=10000)
        entered = {self.role[f['mint']] for f in fills if f['side'] == 'buy'}
        self.assertEqual(entered, {'stop', 'flat', 'ladder', 'flat2', 'late'})                         # trading went on while shedding
        self.assertEqual(store.positions().keys() & {m for m, role in self.role.items() if role in ('stop', 'ladder')}, set())

        # --- regime: a row every 5 minutes, SOL/USD moves, 1h change appears after an hour
        rows = store.rows('observations', kind='regime', limit=10000)
        records = [json.loads(row['raw']) for row in rows]
        self.assertTrue(18 <= len(records) <= 20, len(records))
        gaps = [b['at'] - a['at'] for a, b in zip(records, records[1:])]
        self.assertTrue(all(300 <= g <= 360 for g in gaps), gaps)
        self.assertTrue(records[0]['sol_usd'].startswith('150.2'), records[0]['sol_usd'])      # first sample one tick (60 s) in
        self.assertTrue(all(Decimal(b['sol_usd']) > Decimal(a['sol_usd']) for a, b in zip(records, records[1:])))
        early = [x for x in records if x['at'] < T0 + 3000]        # < 50 min: no sample within 10 min of t-1h
        late = [x for x in records if x['at'] >= T0 + 3900]
        self.assertTrue(early and all(x['sol_usd_change_1h'] is None for x in early))
        self.assertTrue(late and all(x['sol_usd_change_1h'] is not None for x in late))
        self.assertAlmostEqual(float(late[-1]['sol_usd_change_1h']), 0.0945, delta=0.012)             # 150*(1+0.1h): +9.45% over the last hour at ~1.5 h
        self.assertTrue(all(x['sol_usd_change_24h'] is None for x in records))
        self.assertEqual(max(x['candidates_1h'] for x in records), 8)                # all eight were handled within one hour
        self.assertEqual(records[-1]['candidates_1h'], 2)                           # only 'late' and 'no_route' fall in the last hour
        self.assertIsNone(records[0]['candidates_per_hour'])                                           # < 10 min of observation
        self.assertIsNotNone(records[-1]['candidates_per_hour'])
        decisions_before = store.counts()['decisions']
        self.assertFalse([d for d in store.rows('decisions', limit=10000) if 'regime' in json.dumps(d)])   # never part of a decision

        # --- watchdog (both loops beat every tick): pings flowed; nothing stalled
        messages = self.pings()
        self.assertGreaterEqual(messages.count(b'WATCHDOG=1'), 90)
        self.assertEqual(r.ops.watchdog.health()['status'], 'OK')
        self.assertEqual([e for e in store.rows('errors', limit=10000) if e['code'] == 'WATCHDOG_STALL'], [])

        # --- health.json carries the ops section and is plain JSON
        written = json.loads((self.state / 'health.json').read_text())
        self.assertEqual(set(written['ops']), {'credits', 'watchdog', 'regime'})
        self.assertEqual(written['ops']['credits']['status'], 'SHEDDING')
        self.assertEqual(written['ops']['regime']['kind'], 'lean_regime_v1')

        # --- restart: the month's usage and the regime history continue
        r.ops.flush(force=True)
        used = r.ops.credits.credits
        history = len(r.ops.regime.history)
        self.assertGreater(history, 10)
        r.store.close()
        r2 = self.runner(self.config(credit_budget_month=3000))
        self.assertEqual(r2.ops.credits.credits, used)
        self.assertEqual(r2.ops.credits.calls, counted)
        self.assertEqual(len(r2.ops.regime.history), history)
        self.assertEqual(r2.ops.credits.status()[1], 'SHEDDING')
        self.assertFalse(r2.ops.credits.allow('paths'))
        self.assertGreater(decisions_before, 0)

        # --- the morning report reads the same store
        out = report.build(r2.store.path, now=self.clock.time())
        self.assertEqual(out['funnel']['data']['candidates'], 8)
        result = ops.morning_report(r2.store.path, self.root / 'reports', now=self.clock.time())
        self.assertEqual(result['status'], 'OK')
        self.assertIn('Lean desk report', Path(result['html']).read_text())

    def test_huge_budget_never_sheds_and_no_budget_only_counts(self):
        self.world()
        r = self.runner(self.config(credit_budget_month=10 ** 12))
        self.drive(r, T0 + 40 * 60)
        self.assertEqual(r.ops.credits.health()['status'], 'OK')
        self.assertTrue(all(r.ops.credits.allow(l) for l in ('paths', 'features', 'wallet_signals')))
        self.assertEqual([e for e in r.store.rows('errors', limit=10000) if e['code'] == 'CREDIT_SHED'], [])
        self.assertGreater(r.ops.credits.health()['projected_month'], 0)

    def test_a_stalled_position_loop_withholds_the_watchdog_ping_and_recovers(self):
        self.world()
        r = self.runner(self.config(watchdog_stale_s=120))
        self.drive(r, T0 + 30 * 60)
        self.assertGreater(self.pings().count(b'WATCHDOG=1'), 20)
        # the position loop hangs (never beats); the candidate loop keeps going
        self.drive(r, self.clock.time() + 4 * 60, positions=False)
        self.pings()
        self.drive(r, self.clock.time() + 3 * 60, positions=False)
        self.assertEqual(self.pings().count(b'WATCHDOG=1'), 0)                 # no ping while a loop is stalled
        self.assertEqual(r.ops.watchdog.health()['status'], 'STALLED')
        self.assertEqual(r.ops.watchdog.health()['stalled_loops'], ['positions'])
        stalls = [e for e in r.store.rows('errors', limit=10000) if e['code'] == 'WATCHDOG_STALL']
        self.assertEqual([e['message'] for e in stalls], ['positions'])         # reported once
        health = json.loads((self.state / 'health.json').read_text())
        self.assertEqual(health['ops']['watchdog']['status'], 'STALLED')
        # the loop comes back
        self.drive(r, self.clock.time() + 2 * 60)
        self.assertGreaterEqual(self.pings().count(b'WATCHDOG=1'), 1)
        self.assertEqual(r.ops.watchdog.health()['status'], 'OK')
        self.assertEqual(len([e for e in r.store.rows('errors', limit=10000) if e['code'] == 'WATCHDOG_STALL']), 1)

    def test_a_slow_but_progressing_candidate_pass_keeps_the_watchdog_fed(self):
        """A pass over many candidates can outlast the stale limit; per-candidate progress is the heartbeat, not the pass start."""
        self.world()
        r = self.runner(self.config(watchdog_stale_s=120))
        self.clock.t = T0 + 1000
        original = r._handle_candidate
        seen = []

        def slow(candidate, attempt):
            self.clock.t += 100                              # every candidate takes 100 s (slow providers)
            r.ops.beat('positions')                          # the position loop is healthy
            original(candidate, attempt)
            seen.append(r.ops.watchdog.check())
        with mock.patch.object(r, '_handle_candidate', slow):
            r.candidate_pass()
        self.assertGreaterEqual(len(seen), 4)
        self.assertEqual(seen, [True] * len(seen))
        self.assertGreater(self.clock.time() - (T0 + 1000), 120)           # the pass really outlasted the stale limit

    def test_ops_do_not_change_the_trading_result(self):
        fills = []
        for off in (False, True):
            self.fresh_root()
            self.world()
            r = self.runner(self.config(_off=off), systemd=False)
            self.assertEqual(r.ops is None, off)
            self.drive(r, END)
            fills.append([(f['ts'], f['mint'], f['side'], f['qty_raw'], f['sol_lamports'], f['fee_lamports'])
                          for f in r.store.rows('fills', limit=10000)])
            self.assertEqual(r.store.cash(), r.store.initial_cash + r.store.realized())
        self.assertEqual(fills[0], fills[1])
        self.assertGreater(len(fills[0]), 8)

    def test_without_an_ops_block_nothing_is_added_and_a_systemd_watchdog_still_gets_pings(self):
        self.world()
        cfg = self.config(_off=True)
        self.assertIsNone(cfg['ops'])
        r = self.runner(cfg)                                                   # WATCHDOG_USEC is set: pinger only
        self.assertTrue(r.ops.cfg.get('watchdog_only'))
        self.drive(r, T0 + 20 * 60)
        self.assertGreater(self.pings().count(b'WATCHDOG=1'), 10)
        self.assertEqual(r.store.rows('observations', kind='regime'), [])
        self.assertEqual(r.store.rows('events', kind='credit_usage'), [])
        health = json.loads((self.state / 'health.json').read_text())
        self.assertEqual(health['ops']['regime'], None)
        # and with no watchdog in the environment there is no ops object at all
        self.fresh_root()
        self.world()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('WATCHDOG_USEC', None)
            r = build_runner(cfg, state_dir=self.state, discovery_db=self.fake.discovery_db, keys=KEYS, code_version='x',
                             clock=self.clock.time, transport_kwargs={'opener': self.opener, 'clock': self.clock.time,
                                                                      'monotonic': self.clock.monotonic, 'sleep': self.clock.sleep})
        self.assertIsNone(r.ops)
        self.assertNotIn('ops', r.health())


if __name__ == '__main__':
    unittest.main()
