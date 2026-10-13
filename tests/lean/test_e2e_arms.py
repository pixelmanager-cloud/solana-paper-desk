"""SYNTHETIC_TEST_ONLY: W2a shadow arms end to end. Real lean modules (runner, strategy, store, providers, candidates, execution, marks,
arms, report); only HTTP is faked (tests/lean/fakeworld.py). 20 candidates, four arms from the SHIPPED example config
(hold2m, tp110, nocluster_hold2m, live_copy), the L10 latency model on.

Tokens (price paths are keyed on the seconds since the token's FIRST buy quote; ready = offset in seconds from T0):
  flat_*     flat price: the main arm holds until TIME_STOP (45 min); hold2m exits at MAX_HOLD 120 s
  pump_*     +20% at 40 s: tp110 takes profit at 1.10; the main arm holds (touched 1.15), hold2m exits at 120 s
  dump_*     -30% at 90 s: every arm stops out (STOP) before hold2m's 120 s
  hazards    mint authority, freeze authority, concentrated holders, market cap, liquidity: screen rejects; the arms never see them
  cost       an expensive route: COST_BUDGET for everybody
  no_route   a Jupiter no-route: the main arm's quote fails; the arms' own low-lane quote fails too

The main arm holds at most 4 positions (flat ones for 45 min), so later candidates are refused by its portfolio gate BEFORE any quote:
the arms (hold2m frees its slots after 2 minutes) then price them with ONE low-lane quote.
"""
import json
import os
import re
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from lean import arms as W, providers, report
from lean.__main__ import build_runner, load_config
from tests.lean.fakeworld import FakeTime, T0, Token, World

ROOT = Path(__file__).resolve().parents[2]
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}
EXEC = {'exec_delay_s': 3, 'priority_fee_sol': '0.0001', 'jito_tip_sol': '0.0001', 'ata_rent_sol': '0.00203928'}
D = Decimal
STEP = 15
WAKE = 4              # the arm thread wakes this long after the position/candidate passes of a tick
HAZARDS = {'mint_authority', 'freeze_authority', 'concentrated', 'mcap_high', 'liquidity_low'}


def steps(*points):
    def path(age):
        value = D(1)
        for start, m in points:
            if age >= start:
                value = D(str(m))
        return value
    return path


PUMP = steps((40, 1.20))
DUMP = steps((90, 0.7))

# (role, Token kwargs, ready offset)
SCENARIO = [
    ('flat_a', dict(), 0), ('pump_a', dict(path=PUMP), 120), ('dump_a', dict(path=DUMP), 240),
    ('mint_authority', dict(hazard='mint_authority'), 360), ('flat_b', dict(), 480), ('flat_c', dict(), 600),
    ('cost', dict(route_fee_bps=600), 720), ('flat_d', dict(), 840), ('pump_b', dict(path=PUMP), 960),
    ('freeze_authority', dict(hazard='freeze_authority'), 1080), ('flat_e', dict(), 1200), ('no_route', dict(no_route=True), 1320),
    ('dump_b', dict(path=DUMP), 1440), ('pump_c', dict(path=PUMP), 1560), ('concentrated', dict(hazard='concentrated'), 1680),
    ('flat_f', dict(), 1800), ('liquidity_low', dict(quote_sol=20, base_raw=2 * 10 ** 13), 1920), ('pump_d', dict(path=PUMP), 2040),
    ('mcap_high', dict(quote_sol=3000), 2160), ('dump_c', dict(path=DUMP), 2280),
]
END = T0 + 2280 + 3300
# the nocluster arm's feature, per role: (value, written seconds relative to the candidate's ready time, flags, unavailable)
CLUSTERS = {
    'flat_a': ('0.20', -1), 'pump_a': ('3.50', -1), 'dump_a': None,                     # none: no row at all
    'flat_b': ('0.10', +30),                                                             # written AFTER the decision
    'flat_c': (None, -1),                                                                # a null figure
    'flat_d': ('0.10', -1, {'largest_cluster_supply_pct': 'AT_COLLECTION'}),            # read after the decision
    'pump_b': ('0.71', -1), 'flat_e': ('0.7101', -1), 'dump_b': ('0.00', -1), 'pump_c': ('0.50', -1), 'flat_f': ('0.30', -1),
    'pump_d': ('0.30', -1), 'dump_c': ('9.00', -1),
}


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def fee_schedule():
    from tests.lean.fakeworld import mainnet_fee_schedule
    from lean import marks as M
    address, account, _fields = mainnet_fee_schedule()
    return address, account, M.decode_schedule(account)


def shipped_arms(**overrides):
    """The ``arms`` block of config/lean/lean.example.json, normalised exactly as lean.__main__.load_config does."""
    raw = json.loads((ROOT / 'config' / 'lean' / 'lean.example.json').read_text())['arms']
    raw = {**raw, **overrides}
    return W.config(raw, base_dir=ROOT / 'config' / 'lean')


class Run:
    """One run of the 20-candidate world."""

    def __init__(self, test, *, arms=True, arms_cfg=None, execution=EXEC, shed=None, clusters=CLUSTERS, opener=None, marks=None, scenario=None, wrap_opener=None):
        self.test = test
        scenario = SCENARIO if scenario is None else scenario
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.clock = FakeTime(T0)
        self.tokens = [Token(31 + 2 * i, **kw) for i, (_, kw, _) in enumerate(scenario)]
        self.role = {t.mint: name for t, (name, _, _) in zip(self.tokens, scenario)}
        self.by_role = {name: t for name, t in zip(self.role.values(), self.tokens)}
        self.ready = {name: T0 + off for name, _, off in scenario}
        self.world = World(self.root, self.tokens, self.clock)
        self.world.add_frames([T0 + off for _, _, off in scenario])
        self.cfg = load_config(ROOT / 'tests' / 'lean' / 'lean.baseline.json')
        self.cfg['execution'] = execution
        if marks:
            self.cfg['marks'] = marks
        self.cfg['arms'] = (arms_cfg or shipped_arms()) if arms else None
        self.shed = shed                                      # (start, end): the shared LOW lane of both providers is shed in this window
        self.clusters = clusters
        self.opener = opener or (wrap_opener(self.world.opener) if wrap_opener else self.world.opener)
        self.state = self.root / 'state'
        self.r = self.build()
        self.shed_done = False

    def build(self):
        return build_runner(self.cfg, state_dir=self.state, discovery_db=self.world.discovery_db, keys=KEYS, code_version='e2e-arms',
                            clock=self.clock.time,
                            transport_kwargs={'opener': self.opener, 'clock': self.clock.time, 'monotonic': self.clock.monotonic,
                                              'sleep': self.clock.sleep, 'rng': lambda: 0.5})

    def seed_clusters(self):
        for role, spec in (self.clusters or {}).items():
            if spec is None:
                continue
            value, offset = spec[0], spec[1]
            flags = spec[2] if len(spec) > 2 else {}
            meta = {'version': 'clusters_v1', 'largest_cluster_supply_pct': None if value is None else float(value), 'flags': flags,
                    'unavailable': {} if value is not None else {'largest_cluster_supply_pct': 'NO_HOLDERS'}, 'decision_ts': self.ready[role]}
            self.r.store.add_observation('clusters', b'{}', mint=self.by_role[role].mint, meta=meta, ts=self.ready[role] + offset)

    def drive(self, until=END, start=None, process_arms=True, step=STEP):
        r, clock = self.r, self.clock
        tick = start if start is not None else T0
        while tick <= until:
            if clock.time() < tick:
                clock.t = tick
            if self.shed and not self.shed_done and clock.time() >= self.shed[0]:
                self.shed_done = True
                for provider in ('jupiter', 'helius'):
                    providers.shed_low(provider, self.shed[1] - self.shed[0], clock=clock.monotonic, sleep=clock.sleep)
            r.position_pass()
            r.candidate_pass()
            clock.advance(WAKE)
            if r.arms is not None and process_arms:
                r.arms.process()                               # the arm thread's wake-up
            r.write_health()
            self.test.assertIsNone(r.halted, r.halted)
            self.test.assertTrue(r.store.check_invariants())
            self.test.assertTrue(r.store.check_position_states())
            tick += step
        return r

    # -- views ---------------------------------------------------------------------------------------------------------------
    def main_rows(self, table):
        """The main store's rows with the fake world's chain slot taken out: the fake chain's slot counter ticks on EVERY Helius call
        (the arms' low-lane marks reads included), a real chain's slot does not depend on who asks."""
        out = []
        for row in self.r.store.rows(table, limit=100000):
            out.append({k: re.sub(r'"([a-z_]*slot[a-z_]*)": ?\d+', r'"\1":0', v) if isinstance(v, str) else v
                        for k, v in row.items()})
        return out

    def rows(self, table, arm=None):
        store = self.r.arms.store
        return store.rows(table, arm)

    def decisions(self, arm, role=None, kind='entry'):
        out = []
        for d in self.rows('arm_decisions', arm):
            if d['kind'] == kind and (role is None or self.role[d['mint']] == role):
                out.append({**d, 'reasons': json.loads(d['reasons']), 'features': json.loads(d['features'])})
        return out

    def trades(self, arm):
        connection = W.open_ro(self.r.arms.store.path)
        try:
            return {self.role[t['mint']]: t for t in W.closed_trades(connection, arm)}
        finally:
            connection.close()


class ArmsE2E(unittest.TestCase):
    maxDiff = None

    def run_world(self, **kw):
        run = Run(self, **kw)
        run.seed_clusters()
        run.drive()
        return run

    # ------------------------------------------------------------------------------------------------------------------------
    def test_four_arms_twenty_candidates(self):
        run = self.run_world()
        r = run.r
        names = ['hold2m', 'tp110', 'nocluster_hold2m', 'live_copy']
        self.assertEqual([a.name for a in r.arms.arms], names)
        health = json.loads((run.state / 'health.json').read_text())['arms']
        self.assertEqual(health['failures'], 0, health)
        self.assertTrue(all(health['arms'][n]['halted'] is None for n in names))
        passed = {m for m in run.role if run.role[m] not in HAZARDS}
        # hazards: the arms never see a candidate the main screen rejected
        for name in names:
            seen = {d['mint'] for d in run.rows('arm_decisions', name)}
            self.assertFalse(seen & {m for m in run.role if run.role[m] in HAZARDS}, name)
            decided = {d['mint'] for d in run.decisions(name)}
            self.assertEqual(decided, passed, name)                          # every passed candidate is decided exactly once, by every arm
            self.assertEqual(len(run.decisions(name)), len(passed), name)
        # every row carries the arm identity
        for table in ('arm_decisions', 'arm_fills', 'arm_states', 'arm_events'):
            for row in run.rows(table):
                self.assertTrue(row['arm_name'] in names and row['strategy_version'] and len(row['config_hash']) == 64 and row['code_version'])
        versions = {a.name: a.strategy_version for a in r.arms.arms}
        self.assertEqual(versions, {'hold2m': 'lean-1-hold2m', 'tp110': 'lean-1-tp110', 'nocluster_hold2m': 'lean-1-nocluster-hold2m',
                                    'live_copy': 'lean-1'})
        self.assertEqual({a.name: a.config_hash for a in r.arms.arms}['live_copy'], r.cfg.config_hash)       # identical to the main config
        # the arms never write lean.sqlite: its tables hold no arm row (checked by the byte-identical test) and the file set is separate
        self.assertTrue((run.state / 'arms.sqlite').exists())
        # accounting of every arm reconciles with its fills
        for arm in r.arms.arms:
            self.assertEqual(arm.book.cash + sum(p.cost_lamports for p in arm.book.positions.values()), arm.book.initial + arm.book.realized)
        # everything is closed by the end (45 min time stop / 2 min hold / take profit) except what the MAIN arm still holds:
        # a pump the main strategy never sells (touched 1.15, no ladder rung, 6 h hold). live_copy mirrors that exactly.
        held = {run.role[m] for m in r.store.positions()}
        self.assertTrue(held and all(h.startswith('pump') for h in held), held)
        for arm in r.arms.arms:
            self.assertEqual({run.role[m] for m in arm.book.positions}, held if arm.name == 'live_copy' else set(), arm.name)

    def test_main_fills_are_byte_identical_with_arms_on_and_off(self):
        on = self.run_world(arms=True)
        off = self.run_world(arms=False)
        self.assertIsNone(off.r.arms)
        for table in ('fills', 'position_state', 'decisions', 'errors', 'candidates'):
            a, b = on.main_rows(table), off.main_rows(table)
            self.assertTrue(a or table == 'errors', table)
            self.assertEqual(a, b, table)
        self.assertEqual(on.r.store.cash(), off.r.store.cash())
        self.assertEqual(on.r.counts, {**off.r.counts})
        # the arms really did something while the main arm did not change
        self.assertTrue(on.rows('arm_fills'))
        # lean.sqlite carries no arm table or row
        tables = {r[0] for r in on.r.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse({t for t in tables if t.startswith('arm_')})

    def test_live_copy_tracks_the_main_arm(self):
        run = self.run_world()
        main = {}
        for f in run.r.store.rows('fills', limit=100000):
            main.setdefault(run.role[f['mint']], []).append(f)
        copy = run.trades('live_copy')
        main_closed = {run.role[c['mint']]: c for c in run.r.store.closed_positions()}
        self.assertTrue(main_closed)
        # the same candidates are traded, closed for the same reasons, with the same PnL
        self.assertEqual(set(copy), set(main_closed))
        for role, trade in copy.items():
            self.assertEqual(trade['reason'], main_closed[role]['state']['reason'], role)
            self.assertEqual(trade['pnl_lamports'], int(main_closed[role]['state']['trade_pnl_lamports']), role)
        arm_fills = {}
        for f in run.rows('arm_fills', 'live_copy'):
            arm_fills.setdefault(run.role[f['mint']], []).append(f)
        for role, fills in main.items():
            mine = arm_fills[role]
            self.assertEqual([(f['side'], f['qty_raw'], f['sol_lamports'], f['fee_lamports']) for f in fills],
                             [(f['side'], f['qty_raw'], f['sol_lamports'], f['fee_lamports']) for f in mine], role)
        # no price came from a provider call: every live_copy fill was priced from the main arm's own quotes
        sources = {f['price_source'].split(':')[0] for f in run.rows('arm_fills', 'live_copy')}
        self.assertEqual(sources, {'main_entry_quote', 'main_exit_quote'})
        main_pnl = run.r.store.realized()
        copy_pnl = run.r.arms.arms[3].book.realized
        self.assertEqual(copy_pnl, main_pnl)

    def test_hold2m_exits_at_about_120_seconds(self):
        run = self.run_world()
        trades = run.trades('hold2m')
        flat = [t for role, t in trades.items() if role.startswith(('flat', 'pump')) and t['reason'] == 'MAX_HOLD']
        self.assertGreaterEqual(len(flat), 5)
        for t in flat:
            # decided at the first tick at/after 120 s, filled one exec delay (3 s) + the wake after it
            self.assertGreaterEqual(t['hold_s'], 120, t)
            self.assertLessEqual(t['hold_s'], 120 + STEP + 3 + WAKE, t)
        # the main arm held the same flat candidates for 45 minutes (TIME_STOP)
        main = {run.role[c['mint']]: c['state'] for c in run.r.store.closed_positions()}
        self.assertEqual(main['flat_a']['reason'], 'TIME_STOP')
        self.assertEqual(trades['flat_a']['reason'], 'MAX_HOLD')
        # the stop of the main strategy stays: a dump stops out before 120 s, in every arm
        for arm in ('hold2m', 'tp110', 'live_copy'):
            self.assertEqual(run.trades(arm)['dump_a']['reason'], 'STOP', arm)
        self.assertLess(trades['dump_a']['hold_s'], 120)
        # the fill price follows the L10 model: q1 taken exec_delay_s after the decision, fees + ATA rent as live
        exit_decisions = run.decisions('hold2m', 'flat_a', kind='exit')
        self.assertEqual(exit_decisions[-1]['reasons'], ['MAX_HOLD'])
        detail = exit_decisions[-1]['features']
        self.assertGreaterEqual(detail['delay_s'], 3.0)
        self.assertNotIn('LATENCY_MODEL_ABSENT', detail['flags'])
        states = [json.loads(s['state']) for s in run.rows('arm_states', 'hold2m') if s['event'] == 'closed']
        self.assertTrue(all(s['execution']['ata_refund_lamports'] == 2_039_280 for s in states if s['reason'] != 'UNEXITABLE'))

    def test_tp110_takes_profit_at_one_ten(self):
        run = self.run_world()
        trades = run.trades('tp110')
        pumps = [t for role, t in trades.items() if role.startswith('pump')]
        self.assertTrue(pumps)
        for t in pumps:
            self.assertEqual(t['reason'], 'TAKE_PROFIT', t)
            self.assertGreaterEqual(t['ret'], D('0.08'), t)           # 1.20 gross, fees and the 3 s latency taken
            self.assertLess(t['hold_s'], 120, t)
        # the main arm did not take profit there (its ladder starts at 1.4)
        main = {run.role[c['mint']]: c['state'] for c in run.r.store.closed_positions()}
        for role in main:
            if role.startswith('pump'):
                self.assertNotEqual(main[role]['reason'], 'TAKE_PROFIT')

    def test_nocluster_filter_respects_the_timing_of_feature_readiness(self):
        run = self.run_world()
        by = {role: run.decisions('nocluster_hold2m', role) for role in CLUSTERS}
        def only(role):
            self.assertEqual(len(by[role]), 1, role)
            return by[role][0]
        self.assertEqual(only('flat_a')['action'], 'BUY')                                                    # 0.20 <= 0.71
        self.assertEqual(only('pump_a')['reasons'], ['ENTRY_FILTER'])                                         # 3.5 > 0.71
        self.assertEqual(only('pump_a')['features']['entry_filter']['value'], '3.5')
        # not ready: no row, a row written AFTER the decision, a null figure, a figure read after the decision
        expected = {'dump_a': 'NO_ROW', 'flat_b': 'WRITTEN_AFTER_DECISION', 'flat_c': 'VALUE_NULL', 'flat_d': 'AT_COLLECTION'}
        for role, why in expected.items():
            d = only(role)
            self.assertEqual((d['action'], d['reasons']), ('SKIP', ['FEATURE_NOT_READY']), role)
            self.assertEqual(d['features']['entry_filter']['why'], why, role)
        # the boundary: <= 0.71 passes, 0.7101 does not
        self.assertEqual(only('pump_b')['action'], 'BUY')
        self.assertEqual(only('flat_e')['reasons'], ['ENTRY_FILTER'])
        # the same candidates are entered by hold2m (no filter), so the filter is the only difference
        for role in ('flat_b', 'flat_c', 'dump_a'):
            self.assertEqual(run.decisions('hold2m', role)[0]['action'], 'BUY', role)
        # an entered candidate used the figure as of ITS decision (written before it)
        row = only('flat_a')['features']['entry_filter']['conditions'][0]
        self.assertLessEqual(row['row_written_at'], only('flat_a')['ts'])
        entered = run.trades('nocluster_hold2m')
        self.assertTrue(set(entered) <= {'flat_a', 'pump_b', 'dump_b', 'pump_c', 'flat_f', 'pump_d'})

    def test_entries_reuse_the_main_quote_and_pay_one_low_lane_call_otherwise(self):
        run = self.run_world()
        calls_on = [c for c in run.world.calls if c[0] == 'jupiter']
        off = self.run_world(arms=False)
        calls_off = [c for c in off.world.calls if c[0] == 'jupiter']
        arms = run.r.arms
        extra = len(calls_on) - len(calls_off)
        counted = (arms.calls['jupiter_entry_quote'] + arms.calls['jupiter_entry_requote'] + arms.calls['jupiter_exit_quote']
                   + arms.calls['jupiter_quote_mark'])
        self.assertEqual(extra, counted)                                                  # every extra call is an accounted low-lane call
        self.assertEqual(arms.calls['jupiter_quote_mark'], 0)
        # a candidate the main arm quoted costs NOTHING: its arm entries are priced from the main quote
        main_quoted = {run.role[d['mint']] for d in run.r.store.rows('decisions', limit=100000)
                       if d['kind'] == 'entry' and d['action'] == 'BUY'}
        self.assertTrue(main_quoted)
        for arm in ('hold2m', 'tp110', 'live_copy'):
            for role in main_quoted:
                d = run.decisions(arm, role)[0]
                if d['action'] == 'BUY':
                    self.assertTrue(d['features']['price_source'].startswith('main_entry_quote'), (arm, role, d['features']['price_source']))
                    self.assertIn('SCALED_FROM_MAIN', d['features']['flags'])
        # a candidate the main arm did not quote: one low-lane quote, flagged, the sell leg modeled
        low = [d for d in run.decisions('hold2m') if d['features'].get('price_source', '').startswith('low_lane_entry_quote')]
        self.assertTrue(low)
        for d in low:
            self.assertTrue(d['action'] == 'BUY' or d['reasons'] == ['COST_BUDGET'], d)
            self.assertIn('SELL_LEG_MODELED', d['features']['flags'])
            self.assertNotIn('LATENCY_MODEL_ABSENT', d['features']['flags'])        # the L10 model IS applied (q0, delay, q1)
        self.assertTrue([d for d in low if d['action'] == 'BUY'])
        # ONE call per candidate, shared by every arm that needs one (the others scale it: SHARED_QUOTE)
        entry_calls = len({d['candidate_id'] for arm in ('hold2m', 'tp110', 'nocluster_hold2m', 'live_copy') for d in run.decisions(arm)
                           if d['features'].get('price_source', '').startswith('low_lane_entry_quote')
                           or any(x.startswith('ARM_QUOTE_FAILED') for x in d['reasons'])})
        shared = [d for arm in ('tp110', 'live_copy') for d in run.decisions(arm) if 'SHARED_QUOTE' in d['features'].get('flags', [])]
        self.assertTrue(shared)
        self.assertEqual(arms.calls['jupiter_entry_quote'], entry_calls)
        # the no-route candidate: the main arm's quote failed and so did the arm's own
        reasons = run.decisions('hold2m', 'no_route')[0]['reasons']
        self.assertTrue(reasons[0].startswith('ARM_QUOTE_FAILED'), reasons)
        # the expensive route: hold2m has room (its slots free after 2 minutes), prices it with ONE low-lane quote and refuses on cost;
        # live_copy is refused exactly like the main arm (its portfolio gate, before any quote)
        self.assertEqual(run.decisions('hold2m', 'cost')[0]['reasons'], ['COST_BUDGET'])
        main_cost = [d for d in run.r.store.rows('decisions', limit=100000) if d['kind'] == 'entry' and run.role[d['mint']] == 'cost']
        self.assertEqual(json.loads(main_cost[0]['reasons']), ['MAX_POSITIONS'])
        self.assertEqual(run.decisions('live_copy', 'cost')[0]['reasons'], ['MAIN_DID_NOT_BUY'])           # the control follows the main arm

    def test_a_provider_shed_affects_only_the_arms(self):
        plain = self.run_world()
        shed_window = (T0 + 1000, T0 + 2400)
        shed = self.run_world(shed=shed_window)
        # the main arm is untouched, byte for byte
        for table in ('fills', 'position_state', 'decisions', 'errors'):
            self.assertEqual(plain.main_rows(table), shed.main_rows(table), table)
        # the arms recorded the shed on the candidates the main arm did not quote and skipped them
        skipped = [(arm, d) for arm in ('hold2m', 'tp110', 'nocluster_hold2m', 'live_copy') for d in shed.decisions(arm)
                   if W.SKIP_SHED in d['reasons']]
        self.assertTrue(skipped)
        self.assertGreater(shed.r.arms.counts['entry_shed'], 0)
        for arm, d in skipped:
            self.assertEqual(d['action'], 'SKIP')
        # no arm entered a candidate through the shed lane inside the window
        for arm in ('hold2m', 'tp110', 'live_copy'):
            for d in shed.decisions(arm):
                if d['features'].get('price_source', '').startswith('low_lane_entry_quote'):
                    self.assertFalse(shed_window[0] <= d['ts'] < shed_window[1], (arm, d['ts']))
        self.assertEqual(shed.r.arms.health()['failures'], 0)
        # the shed arms hold positions longer or exit by the flagged mark fill, never by a made-up price
        sources = {f['price_source'] for f in shed.rows('arm_fills')}
        self.assertFalse(sources - {'main_entry_quote:q0', 'main_entry_quote:q1', 'main_exit_quote', 'low_lane_entry_quote:q0',
                                    'low_lane_entry_quote:q1', 'low_lane_exit_quote', 'mark_model', 'write_off', 'main_quote_mark'}, sources)

    def test_an_arm_failure_never_reaches_the_main_arm_or_the_other_arms(self):
        plain = self.run_world()
        real = W.Arms._entry

        def broken(self, arm, ev, now_f, deferred):
            if arm.name == 'tp110':
                raise RuntimeError('boom')
            return real(self, arm, ev, now_f, deferred)
        with mock.patch.object(W.Arms, '_entry', broken):
            run = self.run_world()
        for table in ('fills', 'position_state', 'decisions', 'errors'):
            self.assertEqual(plain.main_rows(table), run.main_rows(table), table)
        health = run.r.arms.health()
        self.assertGreater(health['failures'], 0)
        self.assertGreater(health['arms']['tp110']['failures'], 0)
        self.assertEqual(health['arms']['hold2m'].get('failures', 0), 0)
        self.assertEqual(run.rows('arm_fills', 'tp110'), [])
        self.assertTrue(run.rows('arm_fills', 'hold2m'))
        self.assertTrue(run.rows('arm_fills', 'live_copy'))                    # an arm AFTER the broken one in the list still trades
        self.assertIsNone(run.r.halted)

    def test_arms_survive_a_restart_with_open_positions(self):
        run = Run(self)
        run.seed_clusters()
        run.drive(until=T0 + 1300)
        r = run.r
        before = {a.name: (dict(a.book.positions), a.book.cash, set(a.book.states)) for a in r.arms.arms}
        self.assertTrue(any(v[0] for v in before.values()))
        r.arms.close()
        r.store.close()
        run.r = run.build()
        after = {a.name: (dict(a.book.positions), a.book.cash, set(a.book.states)) for a in run.r.arms.arms}
        self.assertEqual(before, after)
        run.drive(start=T0 + 1300 + STEP)
        held = {run.role[m] for m in run.r.store.positions()}
        for a in run.r.arms.arms:
            self.assertIsNone(a.halted, a.name)
            self.assertEqual({run.role[m] for m in a.book.positions}, held if a.name == 'live_copy' else set(), a.name)
        self.assertEqual(run.r.arms.health()['failures'], 0)
        # no candidate was decided twice across the restart
        for name in ('hold2m', 'live_copy'):
            ids = [d['candidate_id'] for d in run.rows('arm_decisions', name) if d['kind'] == 'entry' and d['action'] != 'DEFER']
            self.assertEqual(len(ids), len(set(ids)), name)

    def test_report_section_and_paired_comparison(self):
        run = self.run_world()
        summary = report.build(run.r.store.path, now=run.clock.time(), arms_db=run.r.arms.store.path)
        section = summary['arms']
        self.assertEqual(section['status'], 'OK')
        arms = section['data']['arms']
        self.assertEqual(set(arms), {'hold2m', 'tp110', 'nocluster_hold2m', 'live_copy'})
        copy = arms['live_copy']
        main = summary['pnl_by_strategy']['data']['lean-1']
        self.assertEqual(copy['trades'], main['trades'])
        self.assertAlmostEqual(copy['ev_per_trade'], main['return']['mean'], places=5)
        self.assertEqual(copy['paired_vs_main']['pairs'], main['trades'])
        self.assertAlmostEqual(copy['paired_vs_main']['mean_diff'], 0.0, places=9)
        self.assertEqual(copy['paired_vs_main']['arm_only'], 0)
        hold = arms['hold2m']
        for key in ('trades', 'ev_per_trade', 'win_rate', 'p_le_minus_50', 'equity_curve', 'paired_vs_main', 'price_sources', 'skips'):
            self.assertIn(key, hold)
        self.assertTrue(hold['equity_curve'])
        self.assertGreater(hold['paired_vs_main']['pairs'], 0)
        self.assertIn('MAX_HOLD', hold['exit_reasons'])
        html = report.render_html(summary)
        self.assertIn('Shadow arms', html)
        self.assertIn('hold2m', html)
        json.dumps(summary, allow_nan=False)


class ArmsDegradeGracefully(unittest.TestCase):
    def test_a_broken_arms_store_never_stops_the_trader(self):
        run = Run(self, arms=False)
        (run.state).mkdir(parents=True, exist_ok=True)
        (run.state / 'arms.sqlite').write_bytes(b'this is not a database')
        run.cfg['arms'] = shipped_arms()
        run.r = run.build()
        self.assertIsNone(run.r.arms)
        events = [e['kind'] for e in run.r.store.rows('events', limit=1000)]
        self.assertIn('arms_unavailable', events)
        run.drive(until=T0 + 900)
        self.assertTrue(run.r.store.rows('fills', limit=10))

    def test_the_thread_mode_does_the_arms_work_off_the_trader_threads(self):
        run = Run(self)
        run.seed_clusters()
        arms = run.r.arms
        arms.threaded = True                                             # what Runner.run() sets: the hooks only enqueue
        run.drive(until=T0 + 400, process_arms=False)
        self.assertEqual(arms.health()['arms']['hold2m'].get('entries', 0), 0)      # nothing processed inline
        self.assertGreater(arms.health()['queued'], 0)
        arms.process()                                                   # the arm thread's pass
        self.assertEqual(arms.health()['queued'], 0)
        self.assertGreater(arms.health()['arms']['hold2m'].get('entries', 0), 0)


if __name__ == '__main__':
    unittest.main()
