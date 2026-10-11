"""SYNTHETIC_TEST_ONLY (L20) end to end with the REAL lean modules (runner, execution two-phase exits, sell_evidence, flow, strategy,
paper, store, providers, report, notify). Only HTTP is faked: tests/lean/fakeworld.py for the pools and quotes, tests/lean/tape.py for
each pool's swap history (getSignaturesForAddress / getTransaction on the shared low lane).

Every token takes TP1 (1.5x at +300 s after entry) and is then stopped out (0.7x at +900 s): one partial and one full SELL each.
  (i)   honeypot      buys only, every sell attempt fails on chain          -> both SELLs booked SELL_UNVERIFIED at 0 proceeds
        creator_only  the only successful sells are the coin creator's       -> SELL_UNVERIFIED too (the creator never counts)
  (ii)  normal        organic buys and sells by many wallets                  -> VERIFIED, fills IDENTICAL to the run without L20
  (iii) bot_pump      one bot buying the same size 5x a second (the FOMO staircase) vs an organic pool: the flow row records a high
                      tx rate, a near-zero trade-size CV, an all-up price path, wash-trading and path-shape figures, decision-time only
  (iv)  a shed low lane at exit: no exit delay (q1 exactly exec_delay_s after q0), fills identical, sell_evidence UNKNOWN; a probe still
        running at fill time is UNKNOWN (PENDING) and the exit is not later either.
"""
import json
import os
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from lean import flow as FL, notify as N, providers, report, sell_evidence as SE
from lean.__main__ import build_runner, load_config
from tests.lean import tape as TP
from tests.lean.fakeworld import FakeTime, T0, Token, World
from tests.lean.test_e2e_real import steps

ROOT = Path(__file__).resolve().parents[2]
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}
EXEC = {'exec_delay_s': 3, 'priority_fee_sol': '0.0001', 'jito_tip_sol': '0.0001', 'ata_rent_sol': '0.00203928'}
PATH = steps((300, 1.5), (900, 0.7))
CREATOR_TAG = 222
SELL = {'enabled': True, 'max_tx_samples': 4}            # 1 + 4 calls fit the test lane's burst (5): no real-time pacing in a test


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class L20E2E(unittest.TestCase):
    maxDiff = None

    def world(self, scenario, end):
        """scenario: [(role, ready offset, tape builder(start, end, token))]."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.clock = FakeTime(T0)
        self.tokens = [Token(61 + 2 * i, path=PATH, coin_creator_tag=CREATOR_TAG + i) for i in range(len(scenario))]
        self.role = {t.mint: name for t, (name, _, _) in zip(self.tokens, scenario)}
        self.by_role = {name: t for t, (name, _, _) in zip(self.tokens, scenario)}
        self.world_ = World(self.root, self.tokens, self.clock)
        self.world_.add_frames([T0 + off for _, off, _ in scenario])
        self.tape = TP.Tape(None)
        for t, (_name, off, build) in zip(self.tokens, scenario):
            self.tape.add(t, build(T0 + off - 300, end, t))                         # from the migration on
        self.world_.extra_rpc = self.tape.handlers()
        self.end = end

    def runner(self, state, *, sell=None, flow=None, execution=EXEC):
        cfg = load_config(ROOT / 'tests' / 'lean' / 'lean.baseline.json')
        cfg['execution'] = execution
        cfg['lanes'] = {'shares': providers.validate_lane_shares({'main': 0.25, 'exit': 0.25, 'low': 0.5}), 'low_shed_s': 1}
        cfg['sell_evidence'] = SE.config(sell)
        cfg['flow'] = FL.config(flow)
        r = build_runner(cfg, state_dir=state, discovery_db=self.world_.discovery_db, keys=KEYS, code_version='e2e-l20', clock=self.clock.time,
                         transport_kwargs={'opener': self.world_.opener, 'clock': self.clock.time, 'monotonic': self.clock.monotonic,
                                           'sleep': self.clock.sleep, 'rng': lambda: 0.5})
        self.addCleanup(r.store.close)
        return r

    def drive(self, r):
        tick = T0
        while tick <= self.end:
            if self.clock.time() < tick:
                self.clock.t = tick
            r.position_pass()
            r.candidate_pass()
            if r.flow is not None:
                r.flow.flow_pass()
            self.assertIsNone(r.halted, r.halted)
            self.assertTrue(r.store.check_invariants())
            self.assertTrue(r.store.check_position_states())
            tick += 60
        return r

    def run_once(self, name, after_build=None, **kw):
        self.clock.t = T0
        for t in self.tokens:
            t.anchor = None
        self.tape.calls.clear()
        r = self.runner(self.root / name, **kw)
        if after_build is not None:
            after_build()                                     # build_runner re-creates the lane limiters: act on them after it
        return self.drive(r)

    def sells(self, r):
        """[(role, fill row, execution payload, position_state event, state)] of every SELL."""
        fills = {f['id']: f for f in r.store.rows('fills', limit=10000)}
        out = []
        for row in r.store.rows('position_state', limit=10000):
            if row['fill_id'] is not None and fills[row['fill_id']]['side'] == 'sell':
                state = json.loads(row['state'])
                out.append((self.role[row['mint']], fills[row['fill_id']], state.get('execution') or {}, row['event'], state))
        return out

    @staticmethod
    def shape(r):
        return [(f['mint'], f['side'], f['qty_raw'], f['sol_lamports'], f['fee_lamports'], f['realized_lamports'])
                for f in r.store.rows('fills', limit=10000)]

    # -------------------------------------------------------------------------------------------- (i) + (ii)
    def test_honeypots_are_booked_unverified_at_zero_and_a_normal_pool_is_untouched(self):
        def normal(start, end, t):
            return TP.organic(start, end, seed=11, sell_share=0.9)

        def honeypot(start, end, t):
            return TP.honeypot(start, end)

        def creator_only(start, end, t):
            return TP.only_by(start, end, t.coin_creator)
        self.world([('normal', 0, normal), ('honeypot', 420, honeypot), ('creator_only', 840, creator_only)], T0 + 3000)
        off = self.run_once('off')
        on = self.run_once('on', sell=SELL)
        sells = self.sells(on)
        by = {}
        for role, fill, payload, event, state in sells:
            by.setdefault(role, []).append((fill, payload, event, state))
        self.assertEqual(sorted(by), ['creator_only', 'honeypot', 'normal'])
        for role in by:
            self.assertEqual([e for _, _, e, _ in by[role]], ['rung', 'closed'], role)          # TP1 then the stop, for every token

        # (ii) normal: VERIFIED by >= 2 distinct sellers, and every fill identical to the run without the feature
        for fill, payload, _, _ in by['normal']:
            self.assertIsInstance(payload['sell_evidence'], dict, payload)
            self.assertEqual(payload['sell_evidence']['verdict'], 'VERIFIED')
            self.assertGreaterEqual(payload['sell_evidence']['sellers'], 2)
            self.assertNotIn('sell_status', payload)
        normal = self.by_role['normal'].mint
        self.assertEqual([s for s in self.shape(on) if s[0] == normal], [s for s in self.shape(off) if s[0] == normal])

        # (i) honeypot + creator-only: SELL_UNVERIFIED, booked by paper.write_off of the same qty (0 proceeds, 0 fee)
        for role in ('honeypot', 'creator_only'):
            quoted = [f for f in off.store.rows('fills', limit=10000) if f['mint'] == self.by_role[role].mint and f['side'] == 'sell']
            for (fill, payload, event, state), plain in zip(by[role], quoted):
                self.assertEqual((fill['sol_lamports'], fill['fee_lamports']), (0, 0), role)
                self.assertEqual(fill['realized_lamports'], -fill['cost_sold_lamports'])
                decided = [json.loads(d['features'])['qty_raw'] for d in on.store.rows('decisions', kind='exit', mint=fill['mint'], limit=1000)
                           if d['action'] == 'SELL']
                self.assertIn(fill['qty_raw'], decided, role)                                   # the decided quantity, not more, not less
                if role == 'honeypot':                                                          # entered before any write-off: same size
                    self.assertEqual(fill['qty_raw'], plain['qty_raw'])
                self.assertGreater(plain['sol_lamports'], 0)                                    # the quote WOULD have paid
                self.assertEqual((payload['kind'], payload['write_off_reason'], payload['sell_status'], payload['unverified_sell_policy']),
                                 ('write_off', 'SELL_UNVERIFIED', 'SELL_UNVERIFIED', 'zero'))
                self.assertEqual((payload['sell_evidence']['verdict'], payload['sell_evidence']['sellers']), ('UNVERIFIED', 0))
                self.assertEqual(payload['unverified_quote_sol_lamports'], payload['q1_out'] * (10000 - 50) // 10000)   # the quote at q1
                if role == 'honeypot':
                    self.assertEqual(payload['unverified_quote_sol_lamports'] + 2_039_280 * (event == 'closed'), plain['sol_lamports'])
            self.assertEqual(by[role][1][1]['ata_rent_written_off_lamports'], 2_039_280)           # the full close loses the rent
        # the creator DID sell in its pool: it was seen and excluded (its sells are in the tape and in the sampled bodies)
        creator_sells = [s for m, s, _ in self.tape.calls if m == 'getTransaction' and self.tape.by_sig[s][0] == self.by_role['creator_only'].pool
                         and self.tape.pools[self.by_role['creator_only'].pool]['trades'][self.tape.by_sig[s][1]][2] == 'sell']
        self.assertTrue(creator_sells, [p['sell_evidence'] for _, p, _, _ in by['creator_only']])

        # accounting: one fill per exit, the books close, the outstanding rent is zero, cash = initial + realized
        self.assertEqual(len(self.shape(on)), len(self.shape(off)))
        self.assertEqual(on.store.positions(), {})
        self.assertEqual(on.store.cash(), on.store.initial_cash + on.store.realized())
        self.assertLess(on.store.cash(), off.store.cash())
        x = report.build(on.store.path, now=self.clock.time())
        self.assertEqual(Decimal(x['execution']['data']['fees_sol']['ata_rent_outstanding']), 0)

        # report: the unverified sells are a section of their own and their own exit reason
        se = x['sell_evidence']['data']
        self.assertEqual(se['verdicts'], {'VERIFIED': 2, 'UNVERIFIED': 4, 'UNKNOWN': 0, 'NOT_CHECKED': 0})
        self.assertEqual(sorted({self.role[u['mint']] for u in se['unverified']}), ['creator_only', 'honeypot'])
        self.assertGreater(Decimal(se['forgone_sol']), 0)
        self.assertTrue(any(k.startswith('SELL_UNVERIFIED:') for k in x['exit_reasons']['data']))
        html = report.render_html(x)
        self.assertIn('Sell evidence', html)
        self.assertIn('SELL_UNVERIFIED', html)
        off_html = report.render_html(report.build(off.store.path, now=self.clock.time()))
        self.assertNotIn('Sell evidence', off_html)                                          # nothing checked: no section

        # notify: the SELL message of an unverified sell says so
        view = N.StoreView(N.open_store(on.store.path))
        try:
            texts = {}
            for row in view.fills_after(0, limit=1000):
                if row['side'] == 'sell':
                    texts.setdefault(self.role[row['mint']], []).append(N.render_fill(N.fill_event(view, row, sol_usd_max_age=3600), 'X'))
        finally:
            view.close()
        self.assertTrue(all('⚠️ 매도 검증 안됨' in t for t in texts['honeypot'] + texts['creator_only']))
        self.assertFalse(any('매도 검증' in t for t in texts['normal']))

        # health: the prober's counters
        health = on.health()['sell_evidence']
        self.assertEqual((health['enabled'], health['UNVERIFIED'], health['policy']), (True, 4, 'zero'))
        self.assertLessEqual(health['calls'], 6 * 5)

    # -------------------------------------------------------------------------------------------- (iv)
    def test_a_shed_low_lane_never_delays_an_exit_and_books_unknown(self):
        def normal(start, end, t):
            return TP.organic(start, end, seed=11, sell_share=0.9)
        self.world([('normal', 0, normal)], T0 + 1500)
        off = self.run_once('off')
        started = time.monotonic()
        on = self.run_once('on', sell=SELL, after_build=lambda: providers.shed_low('helius', 10 ** 6, clock=self.clock.monotonic,
                                                                                  sleep=self.clock.sleep))
        self.assertLess(time.monotonic() - started, 2.5)                                   # no wait on a probe that cannot run
        self.assertEqual(self.shape(on), self.shape(off))
        sells = self.sells(on)
        self.assertEqual(len(sells), 2)
        obs = {o['id']: o for o in on.store.rows('observations', limit=100000)}
        for _, fill, payload, _, _ in sells:
            self.assertEqual((payload['sell_evidence'], payload['sell_evidence_why']), ('UNKNOWN', 'LANE_SHED'))
            self.assertEqual(obs[payload['q1_ref']]['ts'] - obs[payload['q0_ref']]['ts'], 3.0)  # the same delay as without the probe
        self.assertFalse([c for c in self.tape.calls if c[0] == 'getTransaction'])
        se = report.build(on.store.path, now=self.clock.time())['sell_evidence']['data']
        self.assertEqual((se['verdicts']['UNKNOWN'], se['unknown_why']), (2, {'LANE_SHED': 2}))

    def test_a_probe_still_running_at_fill_time_is_unknown_and_the_exit_is_not_later(self):
        def honeypot(start, end, t):
            return TP.honeypot(start, end)
        self.world([('honeypot', 0, honeypot)], T0 + 1500)
        fast = dict(EXEC, exec_delay_s=1)
        off = self.run_once('off', execution=fast)
        gate, plain = threading.Event(), self.tape.transaction

        def slow(params, t):                                     # the node answers only after the fill (real time)
            gate.wait(10)
            return plain(params, t)
        self.world_.extra_rpc = {**self.tape.handlers(), 'getTransaction': slow}
        self.addCleanup(gate.set)
        started = time.monotonic()
        on = self.run_once('on', sell=SELL, execution=fast)
        elapsed = time.monotonic() - started
        gate.set()
        self.assertLess(elapsed, 2 * 1.0 + 1.5)                  # two exits: each waited at most its own 1 s delay
        self.assertEqual(self.shape(on), self.shape(off))        # booked at the quote: absence was never proven
        for _, _, payload, _, _ in self.sells(on):
            self.assertEqual((payload['sell_evidence'], payload['sell_evidence_why']), ('UNKNOWN', 'PENDING'))
        obs = {o['id']: o for o in on.store.rows('observations', limit=100000)}
        for _, _, payload, _, _ in self.sells(on):
            self.assertEqual(obs[payload['q1_ref']]['ts'] - obs[payload['q0_ref']]['ts'], 1.0)

    # -------------------------------------------------------------------------------------------- (iii)
    def test_a_bot_pumped_staircase_is_recognisable_at_decision_time(self):
        def bot(start, end, t):
            return TP.bot_pump(start, end)

        def organic(start, end, t):
            return TP.organic(start, end, seed=4)
        self.world([('bot_pump', 0, bot), ('organic', 120, organic)], T0 + 900)
        r = self.run_once('flow', flow={'enabled': True})
        rows = {self.role[x['mint']]: x for x in FL.flow_rows(r.store)}
        self.assertEqual(sorted(rows), ['bot_pump', 'organic'])
        passes = {self.role[d['mint']]: d['ts'] for d in r.store.rows('decisions', kind='screen', limit=1000) if d['action'] == 'PASS'}
        b, o = rows['bot_pump'], rows['organic']
        for name, row in rows.items():
            self.assertEqual((row['version'], row['decision_ts'], row['window_after_decision_s']), ('flow_v1', passes[name], 0.0))
            self.assertEqual(row['late_s'], round(row['collected_at'] - row['decision_ts'], 3))
            self.assertFalse(row['partial'], row['unavailable'])
            self.assertLessEqual(row['calls'], 3 + 12)
            pool = self.by_role[name].pool
            fetched = [self.tape.pools[pool]['trades'][self.tape.by_sig[s][1]][0] for m, s, _ in self.tape.calls
                       if m == 'getTransaction' and self.tape.by_sig[s][0] == pool]
            self.assertTrue(fetched and all(int(t) + 1 <= row['decision_ts'] for t in fetched), name)
        # the staircase: ~5 tx/s, identical sizes, every sampled move up; wash trading (#1) and path shape (#7)
        self.assertEqual(b['tx_rate_per_s'], 5.0)
        self.assertEqual((b['trade_size_cv'], b['monotonic_up_frac'], b['max_drawdown_pre'], b['unique_sellers']), (0.0, 1.0, 0.0, 0))
        self.assertGreaterEqual(b['txs_per_unique_wallet'], 4.0)
        self.assertEqual((b['top5_signer_tx_share'], b['roundtrip_wallets'], b['longest_up_streak']), (1.0, 0, 11))
        self.assertGreater(b['max_runup_pct'], 0)
        self.assertLess(o['tx_rate_per_s'], 0.3)
        self.assertGreater(o['trade_size_cv'], 0.3)
        self.assertLess(o['monotonic_up_frac'], 0.9)
        self.assertLess(o['top5_signer_tx_share'], 0.8)
        self.assertGreater(o['return_vol'], 5 * b['return_vol'])
        self.assertEqual(r.health()['flow']['rows'], 2)


class ConfigCompatibility(unittest.TestCase):
    def test_the_live_r1_config_loads_unchanged_with_l20_off(self):
        """tests/lean/lean.r1-live.json is a byte copy of the r1 example the VPS was configured from (no L20 keys)."""
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copy(ROOT / 'tests' / 'lean' / 'lean.r1-live.json', root / 'lean.json')
            shutil.copy(ROOT / 'config' / 'lean' / 'strategy-default.json', root / 'strategy-default.json')
            raw = json.loads((root / 'lean.json').read_text())
            self.assertNotIn('sell_evidence', raw)
            self.assertNotIn('flow', raw)
            cfg = load_config(root / 'lean.json')
        self.assertFalse(cfg['sell_evidence']['enabled'])
        self.assertFalse(cfg['flow']['enabled'])

    def test_the_example_turns_both_on_and_sell_evidence_needs_execution(self):
        cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        self.assertTrue(cfg['sell_evidence']['enabled'] and cfg['flow']['enabled'])
        # LINT4 (coordinator): the example ships flag_only; under 'zero' a quiet pool would value a legitimate stop at 0
        self.assertEqual(cfg['sell_evidence']['unverified_sell_policy'], 'flag_only')
        with tempfile.TemporaryDirectory() as tmp:
            raw = json.loads((ROOT / 'config' / 'lean' / 'lean.example.json').read_text())
            raw['execution'] = None
            raw['strategy_config'] = str(ROOT / 'config' / 'lean' / 'strategy-default.json')
            bad = Path(tmp) / 'bad.json'
            bad.write_text(json.dumps(raw))
            with self.assertRaises(Exception) as error:
                load_config(bad)
            self.assertIn('sell_evidence needs execution', str(error.exception))
            raw['sell_evidence'] = {'enabled': True, 'typo_key': 1}
            raw['execution'] = {}
            bad.write_text(json.dumps(raw))
            with self.assertRaises(Exception):
                load_config(bad)


if __name__ == '__main__':
    unittest.main()
