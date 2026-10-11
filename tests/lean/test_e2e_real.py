"""SYNTHETIC_TEST_ONLY: the lean desk end to end with the REAL L01-L06 modules. Only the HTTP layer is faked
(tests/lean/fakeworld.py answers Helius / Jupiter / Kraken requests with realistic JSON); discovery is a real discovery
database holding the desk's migration transaction re-keyed per token. No network, no credentials.

Scenario (20 candidates, ~75 minutes of fake time, ticks of 60 s; the position pass runs before the candidate pass):
  hazard rejects   mint authority, freeze authority, outstanding LP, holder concentration, market cap above max,
                   liquidity below min
  entry skips      COST_BUDGET (an expensive route), MAX_POSITIONS (4 open), a Jupiter no-route (400, body kept)
  exits            STOP, TAKE_PROFIT rungs 1-2-3 then TRAILING_STOP, TP1 then STOP at the raised stop, TIME_STOP
  outage           Helius answers 503 for ~35 s: a screen fails (transient, retried next pass and entered) and one
                   marks read fails (exits wait, nothing breaks)
  restart          mid-run, with 4 open positions (two of them past TP rungs): a fresh process on the same state dir
                   resumes them; no TP rung fires twice
  invariants       store.check_invariants() + check_position_states() after every tick; never halted
  report           lean.report on the final store: funnel, exit reasons and PnL equal the store's own accounting
"""
import json
import os
import tempfile
import unittest
from unittest import mock
from collections import Counter
from decimal import Decimal
from pathlib import Path

from lean import report
from lean.__main__ import build_runner, load_config
from tests.lean.fakeworld import FakeTime, T0, Token, World

ROOT = Path(__file__).resolve().parents[2]
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}


def steps(*points):
    """Piecewise-constant multiplier path: ((from_age, m), ...)."""
    def path(age):
        value = Decimal(1)
        for start, m in points:
            if age >= start:
                value = Decimal(str(m))
        return value
    return path


STOP = steps((600, 0.7))
QUICK_STOP = steps((240, 0.7))
LADDER = steps((300, 1.5), (900, 2.2), (1500, 3.2), (2100, 3.6), (2700, 2.3))
TP1_THEN_STOP = steps((300, 1.5), (1800, 0.95))
FLAT_105 = steps((0, 1.05))

# (role, Token kwargs, ready_at offset): ready = when the frame is old enough to be scanned (min age 300 s)
SCENARIO = [
    ('stop', dict(path=STOP), 0),
    ('mint_authority', dict(hazard='mint_authority'), 120),
    ('cost', dict(route_fee_bps=600), 240),
    ('ladder', dict(path=LADDER), 360),
    ('no_route', dict(no_route=True), 480),
    ('freeze_authority', dict(hazard='freeze_authority'), 600),
    ('time', dict(path=FLAT_105), 720),
    ('lp_outstanding', dict(hazard='lp_outstanding'), 840),
    ('tp1_stop', dict(path=TP1_THEN_STOP), 960),
    ('mcap_high', dict(quote_sol=3000), 1080),
    ('quick_stop', dict(path=QUICK_STOP), 1200),
    ('concentrated', dict(hazard='concentrated'), 1320),
    ('outage', dict(path=FLAT_105), 1440),
    ('full_1', dict(), 1560),
    ('full_2', dict(), 1680),
    ('liquidity_low', dict(quote_sol=20, base_raw=2 * 10 ** 13), 1800),
    ('full_3', dict(), 1920),
    ('full_4', dict(), 2040),
    ('full_5', dict(), 2160),
    ('late_stop', dict(path=STOP), 3000),
]
OUTAGE = (T0 + 1435, T0 + 1470)
RESTART_AT = T0 + 1530
END = T0 + 4500



def setUpModule():
    """No network: every socket connect fails for this module (the HTTP layer is the fake world's opener)."""
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class EndToEndRealModulesTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.state = self.root / 'state'
        self.clock = FakeTime(T0)
        self.tokens = [Token(21 + 2 * i, **kwargs) for i, (_, kwargs, _) in enumerate(SCENARIO)]
        self.role = {t.mint: role for t, (role, _, _) in zip(self.tokens, SCENARIO)}
        self.world = World(self.root, self.tokens, self.clock)
        self.world.add_frames([T0 + offset for _, _, offset in SCENARIO])
        self.world.outages['helius'] = OUTAGE
        self.cfg = load_config(ROOT / 'tests' / 'lean' / 'lean.baseline.json')

    def runner(self):
        return build_runner(self.cfg, state_dir=self.state, discovery_db=self.world.discovery_db, keys=KEYS,
                            code_version='e2e-test', clock=self.clock.time,
                            transport_kwargs={'opener': self.world.opener, 'clock': self.clock.time,
                                              'monotonic': self.clock.monotonic, 'sleep': self.clock.sleep, 'rng': lambda: 0.5})

    def run_scenario(self):
        r = self.runner()
        snapshots, restarted, tick = {}, False, T0
        while tick <= END:
            if self.clock.time() < tick:
                self.clock.t = tick
            if not restarted and tick >= RESTART_AT:
                snapshots['before_restart'] = self.snapshot(r)
                r.store.close()                                    # the process dies; a new one starts on the same state
                r = self.runner()
                restarted = True
                snapshots['after_restart'] = self.snapshot(r)
            r.position_pass()
            r.candidate_pass()
            r.write_health()
            self.assertIsNone(r.halted, r.halted)
            self.assertTrue(r.store.check_invariants())
            self.assertTrue(r.store.check_position_states())
            tick += 60
        return r, snapshots

    def snapshot(self, r):
        return {'positions': {self.role[m]: p.qty_raw for m, p in r.store.positions().items()},
                'states': {self.role[m]: s['state']['stage'] for m, s in r.store.position_states().items()},
                'cursor': r.cursor, 'cash': r.store.cash()}

    # ------------------------------------------------------------------------------------------------------------
    def test_twenty_candidates_end_to_end(self):
        r, snaps = self.run_scenario()
        store = r.store
        fills = store.rows('fills', limit=10000)
        decisions = store.rows('decisions', limit=10000)
        closed = {self.role[c['mint']]: c['state'] for c in store.closed_positions()}
        sells = [f for f in fills if f['side'] == 'sell']
        exit_reasons = Counter()
        for row in store.rows('position_state', limit=10000):
            if row['event'] in ('rung', 'closed'):
                state = json.loads(row['state'])
                exit_reasons[(self.role[row['mint']], state.get('reason') or state.get('last_reason'))] += 1
        screens = {}
        for d in decisions:
            if d['kind'] == 'screen':
                screens[self.role[d['mint']]] = (d['action'], tuple(json.loads(d['reasons'])))
        entries = {self.role[d['mint']]: (d['action'], tuple(json.loads(d['reasons']))) for d in decisions if d['kind'] == 'entry'}
        errors = Counter((e['code'], self.role.get(e['mint'], e['scope'])) for e in store.rows('errors', limit=10000))

        # every candidate was seen once, the cursor reached the last frame
        self.assertEqual(store.counts()['candidates'], 20)
        self.assertEqual(r.cursor, 20)

        # hazard rejects (never an entry, never a halt)
        expected_rejects = {'mint_authority': 'ACTIVE_MINT_AUTHORITY', 'freeze_authority': 'ACTIVE_FREEZE_AUTHORITY',
                            'lp_outstanding': 'OUTSTANDING_WITHDRAWABLE_LP_SUPPLY', 'concentrated': 'HOLDER_CONCENTRATION_ABOVE_MAX',
                            'mcap_high': 'MARKET_CAP_ABOVE_MAX', 'liquidity_low': 'LIQUIDITY_BELOW_MIN'}
        for role, reason in expected_rejects.items():
            self.assertEqual(screens[role][0], 'REJECT', role)
            self.assertIn(reason, screens[role][1], role)
            self.assertNotIn(role, entries)

        # entry skips: cost cap (needs the exact sell leg), portfolio cap, a no-route quote kept as evidence
        self.assertEqual(entries['cost'], ('SKIP', ('COST_BUDGET',)))
        for role in ('full_1', 'full_2', 'full_3', 'full_4', 'full_5'):
            self.assertEqual(entries[role][0], 'SKIP', role)
            self.assertIn('MAX_POSITIONS', entries[role][1], role)
        self.assertEqual(errors[('HTTP_400', 'no_route')], 1)
        no_route_body = [o for o in store.rows('observations', limit=10000) if o['kind'] == 'error:HTTP_400']
        self.assertEqual(len(no_route_body), 1)
        self.assertIn(b'COULD_NOT_FIND_ANY_ROUTE', no_route_body[0]['raw'])

        # entries and their exits, by reason
        entered = {self.role[f['mint']] for f in fills if f['side'] == 'buy'}
        self.assertEqual(entered, {'stop', 'ladder', 'time', 'tp1_stop', 'quick_stop', 'outage', 'late_stop'})
        self.assertEqual({role: s['reason'] for role, s in closed.items()},
                         {'stop': 'STOP', 'ladder': 'TRAILING_STOP', 'time': 'TIME_STOP', 'tp1_stop': 'STOP',
                          'quick_stop': 'STOP', 'outage': 'TIME_STOP', 'late_stop': 'STOP'})
        self.assertEqual(exit_reasons[('ladder', 'TAKE_PROFIT')], 3)          # TP1, TP2, TP3: each exactly once
        self.assertEqual(exit_reasons[('tp1_stop', 'TAKE_PROFIT')], 1)        # no re-fire after the restart
        self.assertEqual(store.positions(), {})

        # ladder sizes: fractions of the INITIAL quantity, floored to raw units, never of the current position
        ladder = next(t for t in self.tokens if self.role[t.mint] == 'ladder')
        ladder_fills = [f for f in fills if f['mint'] == ladder.mint]
        initial = ladder_fills[0]['qty_raw']
        self.assertEqual([f['qty_raw'] for f in ladder_fills[1:4]],
                         [int(Decimal(initial) * Decimal(x)) for x in ('0.3', '0.3', '0.2')])
        self.assertEqual(sum(f['qty_raw'] for f in ladder_fills[1:]), initial)

        # provider outage: the screen failed transiently, was retried and entered; a marks read failed; no halt
        outage_screens = [d for d in decisions if d['kind'] == 'screen' and self.role[d['mint']] == 'outage']
        self.assertEqual([d['action'] for d in outage_screens], ['FAILED', 'PASS'])
        self.assertGreaterEqual(errors[('HTTP_503', 'outage')], 1)
        self.assertGreaterEqual(errors[('HTTP_503', 'marks')], 1)
        self.assertIn(('helius', 'OUTAGE'), {(p, m) for p, m, _ in self.world.calls})

        # restart: 4 open positions survived with their stages; nothing was lost or duplicated
        self.assertEqual(snaps['before_restart'], snaps['after_restart'])
        self.assertEqual(set(snaps['after_restart']['positions']), {'ladder', 'time', 'tp1_stop', 'outage'})
        self.assertEqual(snaps['after_restart']['states'], {'ladder': 2, 'time': 0, 'tp1_stop': 1, 'outage': 0})

        # accounting: cash = initial + realized; every closed trade's pnl adds up to it
        realized = store.realized()
        self.assertEqual(store.cash(), store.initial_cash + realized)
        self.assertEqual(sum(int(s['trade_pnl_lamports']) for s in closed.values()), realized)
        self.assertEqual(len(sells), sum(exit_reasons.values()))

        # the L06 report on the result agrees with the store
        summary = report.build(store.path, now=self.clock.time())
        funnel = summary['funnel']['data']
        self.assertEqual((funnel['candidates'], funnel['screened'], funnel['passed'], funnel['entered'], funnel['exited']),
                         (20, 20, 14, 7, 7))
        trades = summary['trades']['data']
        self.assertEqual((trades['closed_scored'], trades['open'], trades['flagged'], trades['anomaly_count']), (7, 0, 0, 0))
        self.assertAlmostEqual(trades['overall_pnl_sol'], realized / 1e9, places=6)      # the report rounds to 1e-6 SOL
        pnl_by_mint = {r['mint']: r['pnl_sol'] for r in trades['rows']}
        for c in store.closed_positions():
            self.assertAlmostEqual(pnl_by_mint[c['mint']], int(c['state']['trade_pnl_lamports']) / 1e9, places=6)
        self.assertEqual({k: v['trades'] for k, v in summary['exit_reasons']['data'].items()},
                         {'STOP': 4, 'TRAILING_STOP': 1, 'TIME_STOP': 2})
        self.assertEqual(summary['pnl_by_strategy']['data']['lean-1']['trades'], 7)
        self.assertIn('HTTP_503', summary['errors']['data'])
        health = json.loads((self.state / 'health.json').read_text())
        self.assertEqual((health['halted'], health['open_positions']), (None, []))


if __name__ == '__main__':
    unittest.main()
