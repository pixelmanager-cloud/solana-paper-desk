"""W2a2: the pre-deploy review of the shadow arms, one fail-first test group per finding (see reports/W2a2.md).

 1  lean_config_hash never covers the arms; arms_config_hash rides next to it
 2  a low-lane entry gets the SAME L10 execution model as the main arm (q0, delay, q1, the worse of the two, the aborts); the sell leg is M1's
 3  MARK_FILL fills at the LATEST mark and never without a fresh one; the report leaves flagged fills out of the headline, EV by price source
 4  live_copy enters only when the main arm bought; main trades that ended by L11/L20/L23/D2 are not compared
 5  the entry filter's not-ready share is in the arm's health
 6  an arm position is marked with the model it was opened with
 7  one bad mint / position never costs the other arms' marks and exits
 8  the arms keep a reserve token on the low lane
"""
import dataclasses
import json
import os
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from lean import __main__ as entry, adapters as A, arms as W, paper, providers, report, strategy as S
from lean.paper import Fill
from lean.store import Store
from tests.lean import test_e2e_arms as E
from tests.lean.fakeworld import Resp

T0 = E.T0
ROOT = Path(__file__).resolve().parents[2]


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def ramp(sign):
    return lambda age: D(1) + sign * D('0.01') * min(D(age), D(10))


def refuse_main(run):
    """The main arm's gate refuses every candidate (its market-cap band is elsewhere), so the arms price them on the low lane."""
    run.r.cfg = dataclasses.replace(run.r.cfg, min_market_cap_usd=D('50000000'), max_market_cap_usd=D('90000000'))


def solo(run_test, path=None, **kw):
    run = E.Run(run_test, scenario=[('solo', dict(path=path) if path else dict(), 0)], **kw)
    refuse_main(run)
    return run


def execution_payload(run, arm, side):
    for row in run.rows('arm_states', arm):
        state = json.loads(row['state'])
        if row['fill_id'] is not None and (state.get('execution') or {}).get('side') == side:
            return state['execution']
    return None


# ------------------------------------------------------------------------------------------------------------- 1 hashes
class ConfigHashes(unittest.TestCase):
    def test_the_arms_never_change_the_main_runs_identity(self):
        base = entry.load_config(ROOT / 'tests' / 'lean' / 'lean.r1-prod.json')
        on = {**base, 'arms': E.shipped_arms()}
        self.assertEqual(entry.lean_config_hash(on), entry.lean_config_hash(base))
        self.assertEqual(entry.lean_config_hash({**on, 'arms': E.shipped_arms(max_scale=3.0)}), entry.lean_config_hash(base))
        self.assertEqual(entry.lean_config_hash({**base, 'stale_mark_s': 31}) == entry.lean_config_hash(base), False)   # still sensitive

    def test_the_arms_have_their_own_hash(self):
        base = entry.load_config(ROOT / 'tests' / 'lean' / 'lean.r1-prod.json')
        self.assertIsNone(entry.arms_config_hash(base))
        a, b = entry.arms_config_hash({'arms': E.shipped_arms()}), entry.arms_config_hash({'arms': E.shipped_arms(max_scale=3.0)})
        self.assertRegex(a, r'^[0-9a-f]{64}$')
        self.assertNotEqual(a, b)
        self.assertEqual(a, entry.arms_config_hash({'arms': E.shipped_arms()}))
        self.assertNotEqual(a, entry.lean_config_hash({'arms': E.shipped_arms()}))

    def test_the_start_event_and_health_carry_both(self):
        on, off = E.Run(self), E.Run(self, arms=False)
        starts = {}
        for name, run in (('on', on), ('off', off)):
            starts[name] = json.loads(run.r.store.rows('events', kind='start')[-1]['payload'])
            health = run.r.health()
            self.assertEqual((health['lean_config_hash'], health['arms_config_hash']), (starts[name]['lean_config_hash'], starts[name]['arms_config_hash']))
        self.assertEqual(starts['on']['lean_config_hash'], starts['off']['lean_config_hash'])         # the learner's window survives turning the arms on
        self.assertEqual(starts['on']['arms_config_hash'], entry.arms_config_hash(on.cfg))
        self.assertRegex(starts['on']['arms_config_hash'], r'^[0-9a-f]{64}$')
        self.assertIsNone(starts['off']['arms_config_hash'])

    def test_the_learner_default_window_is_the_same_run_with_and_without_arms(self):
        from lean import learn
        run = E.Run(self, arms=False)
        before = learn.latest_config_hash(run.r.store.path)
        self.assertRegex(before, r'^[0-9a-f]{64}$')
        run.r.store.close()
        run.cfg['arms'] = E.shipped_arms()                                                  # the arms are switched on in the same state dir
        run.r = run.build()
        self.assertIsNotNone(run.r.arms)
        self.assertEqual(learn.latest_config_hash(run.r.store.path), before)                # the learner's default window is the same run


# ------------------------------------------------------------------------------------- 2 the L10 model on a low-lane entry
class LowLaneEntryL10(unittest.TestCase):
    def buys(self, run, arm='hold2m'):
        return [f for f in run.rows('arm_fills', arm) if f['side'] == 'buy']

    def test_q1_not_worse_fills_at_q0_one_delay_after_the_decision(self):
        run = solo(self, ramp(-1))                                                       # the price falls 1%/s after the first quote
        run.drive(until=T0 + 30)
        for name in ('hold2m', 'tp110'):
            d = run.decisions(name, 'solo')
            self.assertEqual([(x['action'], x['reasons']) for x in d], [('BUY', ['ENTRY'])], name)
            self.assertEqual(d[0]['features']['price_source'], 'low_lane_entry_quote:q0', name)
            self.assertNotIn('LATENCY_MODEL_ABSENT', d[0]['features']['flags'])
            self.assertIn('SELL_LEG_MODELED', d[0]['features']['flags'])
        fill = self.buys(run)[0]
        self.assertGreaterEqual(fill['ts'] - T0, 3.0)                                    # not before exec_delay_s
        payload = execution_payload(run, 'hold2m', 'buy')
        self.assertEqual((payload['fill'], payload['latency_tax_bps']), ('q0', '0.00'))
        self.assertGreater(payload['q1_out'], payload['q0_out'])                         # q1 was the better one: the worse (q0) is the fill
        self.assertEqual(fill['qty_raw'], payload['q0_out'] * (10000 - 50) // 10000)
        calls = run.r.arms.calls
        self.assertEqual((calls['jupiter_entry_quote'], calls['jupiter_entry_requote']), (1, 1))     # q0 and q1: one call each for all the arms
        self.assertIn('SHARED_REQUOTE', run.decisions('tp110', 'solo')[0]['features']['flags'])

    def test_the_entry_waits_for_the_execution_delay(self):
        run = solo(self, ramp(-1))
        r, clock, arms = run.r, run.clock, run.r.arms
        r.position_pass()
        r.candidate_pass()
        pf = arms.arms[0].pending_fills[0]
        self.assertAlmostEqual(pf.due_at - pf.q0_t, 3.0)                                 # exec_delay_s
        self.assertEqual(arms.calls['jupiter_entry_requote'], 0)
        clock.advance(1.5)
        arms.process()
        self.assertEqual((run.rows('arm_fills'), arms.calls['jupiter_entry_requote']), ([], 0))      # no re-quote and no fill before the delay
        clock.advance(2.0)
        arms.process()
        self.assertEqual(len(self.buys(run)), 1)
        self.assertAlmostEqual(self.buys(run)[0]['ts'] - pf.q0_t, 3.5)

    def test_q1_worse_fills_at_q1_and_pays_the_latency_tax(self):
        run = solo(self, ramp(1))                                                        # the price rises 1%/s: q1 gets fewer tokens
        run.drive(until=T0 + 30)
        d = run.decisions('hold2m', 'solo')[0]
        self.assertEqual((d['action'], d['features']['price_source']), ('BUY', 'low_lane_entry_quote:q1'))
        payload = execution_payload(run, 'hold2m', 'buy')
        self.assertEqual(payload['fill'], 'q1')
        self.assertLess(payload['q1_out'], payload['q0_out'])
        self.assertTrue(D(350) < D(payload['latency_tax_bps']) < D(420), payload['latency_tax_bps'])     # about 4 s of 1%/s: q1 is taken when the arm thread wakes
        self.assertEqual(self.buys(run)[0]['qty_raw'], payload['q1_out'] * (10000 - 50) // 10000)
        self.assertEqual(payload['delay_s'], 3)
        fee = self.buys(run)[0]['fee_lamports']
        self.assertEqual(fee, 50_000 + 100_000 + 100_000 + 2_039_280)                    # the cost model: fixed + priority + tip + ATA rent

    def test_q1_without_a_route_drops_the_entry(self):
        def wrap(opener):
            seen = {}

            def wrapped(request, timeout):
                from urllib.parse import parse_qs, urlsplit
                url = urlsplit(request.full_url)
                if url.hostname == 'api.jup.ag':
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    if q['inputMint'] == 'So11111111111111111111111111111111111111112':
                        seen[q['outputMint']] = seen.get(q['outputMint'], 0) + 1        # q0 is the first buy quote of the mint, q1 the second
                        if seen[q['outputMint']] >= 2:
                            return Resp(b'{"error":"Could not find any route","errorCode":"COULD_NOT_FIND_ANY_ROUTE"}', 400)
                return opener(request, timeout)
            return wrapped
        run = solo(self, wrap_opener=wrap)
        run.drive(until=T0 + 30)
        for name in ('hold2m', 'tp110'):
            d = run.decisions(name, 'solo')
            self.assertEqual([(x['action'], x['reasons']) for x in d], [(W.X.ABORT, ['Q1_UNAVAILABLE:HTTP_400'])], name)
        self.assertEqual(run.rows('arm_fills'), [])
        self.assertEqual(run.r.arms.calls['jupiter_entry_requote'], 1)                   # the failure is shared, not retried per arm
        self.assertEqual(run.r.arms.arms[0].counts['entry_aborted_latency'], 1)

    def test_a_worse_q1_that_breaks_the_cost_cap_is_dropped_a_q0_fill_is_only_skipped(self):
        for sign, expect in ((1, (W.X.ABORT, ['LATENCY_COST_BUDGET'])), (-1, ('SKIP', ['COST_BUDGET']))):
            run = solo(self, ramp(sign))
            for arm in run.r.arms.arms:
                arm.cfg = dataclasses.replace(arm.cfg, max_roundtrip_cost_fraction=D('0.02'))        # the real cost is about 3%
            run.drive(until=T0 + 30)
            d = run.decisions('hold2m', 'solo')
            self.assertEqual([(x['action'], x['reasons']) for x in d], [expect], sign)
            self.assertEqual(run.rows('arm_fills'), [], sign)

    def test_a_shed_between_q0_and_q1_skips_with_the_shed_reason(self):
        run = solo(self)
        r, clock, arms = run.r, run.clock, run.r.arms
        r.position_pass()
        r.candidate_pass()                                                              # q0 taken, the entry waits for its delay
        self.assertTrue(arms.arms[0].pending_fills)
        providers.shed_low('jupiter', 600, clock=clock.monotonic, sleep=clock.sleep)
        clock.advance(4)
        arms.process()
        d = run.decisions('hold2m', 'solo')
        self.assertEqual([(x['action'], x['reasons']) for x in d], [('SKIP', ['ARM_SKIPPED_SHED'])])
        self.assertEqual(d[0]['features']['stage'], 'q1')
        self.assertEqual((run.rows('arm_fills'), arms.arms[0].pending_fills), ([], []))

    def test_without_an_execution_model_the_main_arm_has_none_either_so_one_quote(self):
        run = solo(self, execution=None)
        run.drive(until=T0 + 30)
        d = run.decisions('hold2m', 'solo')[0]
        self.assertEqual((d['action'], d['features']['price_source']), ('BUY', 'low_lane_entry_quote:single'))
        self.assertEqual(run.r.arms.calls['jupiter_entry_requote'], 0)
        self.assertNotIn('LATENCY_MODEL_ABSENT', d['features']['flags'])               # nothing to apply: nothing is "absent"

    def test_a_pending_candidate_is_not_decided_twice(self):
        run = solo(self)
        r, arms = run.r, run.r.arms
        r.position_pass()
        r.candidate_pass()
        ev = arms.arms[0].pending_fills[0].ev
        arms.submit(ev)
        arms.submit(ev)
        self.assertEqual(len(arms.arms[0].pending_fills), 1)


# ------------------------------------------------------------------------------------------------ 3 MARK_FILL and the report
def write_trade(store, arm, mint, ret, flags=(), entry_source='main_entry_quote:q0', exit_source='main_exit_quote', ts=100.0):
    """One closed trade of 1 SOL cost and return ``ret`` in the log, the buy flagged with the first flag, the sell with the rest."""
    cost = 1_000_000_000
    back = int(cost * (1 + ret))
    held = paper.Position(mint, 1000, cost, ts, 0, 6)
    buy = Fill(ts=ts, mint=mint, side='buy', qty_raw=1000, sol_lamports=cost - 50_000, fee_lamports=50_000, slippage_bps=50, decimals=6)
    store.fill(arm, buy, cash_after=1, held=held, candidate_id=hash(mint) % 10 ** 6, price_source=entry_source, flags=list(flags[:1]), event='open',
               state={'stage': 0}, open_fill_id=None)
    opened = store.rows('arm_fills')[-1]['id']
    sell = Fill(ts=ts + 120, mint=mint, side='sell', qty_raw=1000, sol_lamports=back + 50_000, fee_lamports=50_000, slippage_bps=50, decimals=6,
                cost_sold_lamports=cost, realized_lamports=back - cost)
    store.fill(arm, sell, cash_after=1, held=None, candidate_id=None, price_source=exit_source, flags=list(flags[1:]), event='closed',
               state={'reason': 'MAX_HOLD'}, open_fill_id=opened)


def identity(name='x'):
    import types
    return types.SimpleNamespace(name=name, strategy_version='v1', config_hash='h' * 64, code_version='test')


class MarkFill(unittest.TestCase):
    def setUp(self):
        self.run = E.Run(self)
        self.arms = self.run.r.arms
        self.arm = self.arms.arms[0]

    def pend(self, mark='0.2'):
        return W.PendingExit(mint='M', open_id=1, decided_at=T0, due_at=T0 + 3, decided_int=T0, qty=1000, reason='MAX_HOLD', mark=D(mark),
                             mark_at=T0, mark_source='x', liquidate=False)

    def test_it_fills_at_the_latest_mark_not_the_decision_time_mark(self):
        pend = self.pend('0.2')
        self.arm.marks['M'] = (D('0.123'), T0 + 8, 'main_marks')
        got = self.arms._mark_fill(self.arm, pend, None, T0 + 10.0)
        quote, _ref, _at, _meta, source, flags = got
        self.assertEqual(quote.out_amount, int(D('0.123') * 10 ** 9) + self.arm.pcfg.fee_lamports)
        self.assertEqual((source, flags), ('mark_model', ['MARK_FILL', 'LATENCY_MODEL_ABSENT']))
        self.assertEqual(pend.fill_mark, (D('0.123'), T0 + 8))

    def test_without_a_fresh_mark_nothing_is_filled_and_the_exit_keeps_retrying(self):
        pend = self.pend('0.2')
        self.assertIsNone(self.arms._mark_fill(self.arm, pend, None, T0 + 10.0))              # no mark at all
        self.arm.marks['M'] = (D('0.123'), T0 - 5, 'main_marks')
        self.assertIsNone(self.arms._mark_fill(self.arm, pend, None, T0 + 10.0))              # 15 s old: older than the price ttl
        self.arm.marks['M'] = (D(0), T0 + 9, 'main_marks')
        self.assertIsNone(self.arms._mark_fill(self.arm, pend, None, T0 + 10.0))              # a zero mark is not a price
        self.assertEqual(self.arms.counts['mark_fill_waiting_for_a_mark'], 3)
        self.assertIsNone(pend.fill_mark)

    def test_a_shed_exit_is_filled_at_the_mark_of_the_moment_it_fills(self):
        run = E.Run(self, scenario=[('flat_a', dict(path=E.steps((140, 0.9))), 0)], shed=(T0 + 100, T0 + 100_000))
        run.r.arms.cfg['exit_wait_s'] = 10.0
        run.drive(until=T0 + 220)
        trade = run.trades('hold2m')['flat_a']
        self.assertEqual(trade['reason'], 'MAX_HOLD')
        sell = [f for f in run.rows('arm_fills', 'hold2m') if f['side'] == 'sell'][0]
        self.assertEqual((sell['price_source'], set(json.loads(sell['flags']))), ('mark_model', {'MARK_FILL', 'LATENCY_MODEL_ABSENT'}))
        detail = [d for d in run.decisions('hold2m', 'flat_a', kind='exit')][-1]['features']
        self.assertLess(D(detail['fill_mark_sol']), D(detail['mark_sol']) * D('0.95'))        # the price fell while the arm waited: the later mark
        self.assertLessEqual(detail['fill_mark_age_s'], 10)
        self.assertLess(trade['ret'], D('-0.08'))                                             # at the decision-time mark it would be about -2%


class ReportExcludesFlaggedFills(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = W.ArmsStore(Path(os.path.realpath(self.tmp.name)) / 'arms.sqlite')
        self.addCleanup(self.store.close)
        arm = identity('x')
        self.store.event(arm, 'arm_init', {'initial_cash_lamports': 10 ** 10}, ts=1.0)
        write_trade(self.store, arm, 'A', 0.10)
        write_trade(self.store, arm, 'B', -0.60, flags=('LATENCY_MODEL_ABSENT',), entry_source='low_lane_entry_quote:single')
        write_trade(self.store, arm, 'C', 0.50, flags=('', 'MARK_FILL'), exit_source='mark_model')
        write_trade(self.store, arm, 'D', 0.30, entry_source='low_lane_entry_quote:q1')
        self.connection = W.open_ro(self.store.path)
        self.addCleanup(self.connection.close)
        self.main = [{'mint': m, 'ret': D(r), 'entry_at': 100.0, 'exit_at': 220.0, 'pnl_lamports': 0, 'hold_s': 120.0}
                     for m, r in (('A', '0.0'), ('B', '0.0'), ('C', '0.0'), ('D', '0.1'))]

    def section(self, **kw):
        return W.report_section(self.connection, self.main, **kw)['arms']['x']

    def test_the_default_headline_leaves_out_mark_fills_and_missing_latency_models(self):
        v = self.section()
        self.assertEqual(v['trades'], 2)                                                      # A and D
        self.assertAlmostEqual(v['ev_per_trade'], (0.10 + 0.30) / 2, places=6)
        self.assertEqual(v['excluded']['trades'], 2)
        self.assertEqual(v['excluded']['by_flag'], {'LATENCY_MODEL_ABSENT': 1, 'MARK_FILL': 1})
        self.assertAlmostEqual(v['excluded']['stats']['ev_per_trade'], (-0.60 + 0.50) / 2, places=6)             # still shown, separately
        self.assertEqual(v['paired_vs_main']['pairs'], 2)                                      # the comparison is on the headline trades only
        self.assertEqual(self.section()['excluded']['flags'], ['LATENCY_MODEL_ABSENT', 'MARK_FILL'])

    def test_exclude_flags_is_configurable(self):
        self.assertEqual(self.section(exclude_flags=())['trades'], 4)
        self.assertEqual(self.section(exclude_flags=())['excluded']['trades'], 0)
        v = self.section(exclude_flags=('MARK_FILL',))
        self.assertEqual((v['trades'], v['excluded']['by_flag']), (3, {'MARK_FILL': 1}))

    def test_ev_by_price_source_covers_every_trade(self):
        v = self.section()
        self.assertEqual({k: b['trades'] for k, b in v['by_entry_source'].items()},
                         {'main_entry_quote:q0': 2, 'low_lane_entry_quote:single': 1, 'low_lane_entry_quote:q1': 1})
        self.assertAlmostEqual(v['by_entry_source']['low_lane_entry_quote:q1']['ev_per_trade'], 0.30, places=6)
        self.assertAlmostEqual(v['by_entry_source']['low_lane_entry_quote:single']['ev_per_trade'], -0.60, places=6)
        self.assertEqual({k: b['trades'] for k, b in v['by_exit_source'].items()}, {'main_exit_quote': 3, 'mark_model': 1})
        self.assertAlmostEqual(v['by_exit_source']['mark_model']['ev_per_trade'], 0.50, places=6)

    def test_the_html_and_the_summary_show_both(self):
        run = E.Run(self)
        run.drive(until=T0 + 400)
        summary = report.build(run.r.store.path, now=run.clock.time(), arms_db=run.r.arms.store.path)
        data = summary['arms']['data']
        self.assertEqual(data['exclude_flags'], ['LATENCY_MODEL_ABSENT', 'MARK_FILL'])
        self.assertIn('by_entry_source', data['arms']['hold2m'])
        self.assertIn('Headline: trades with a fill flagged', report.render_html(summary))
        none = report.build(run.r.store.path, now=run.clock.time(), arms_db=run.r.arms.store.path, arms_exclude_flags=[])
        self.assertEqual(none['arms']['data']['exclude_flags'], [])
        self.assertEqual(none['arms']['data']['arms']['hold2m']['excluded']['trades'], 0)


# ---------------------------------------------------------------------------------------------------------- 4 live_copy
class MirrorMain(unittest.TestCase):
    def test_the_shipped_control_follows_the_main_arm(self):
        cfg = E.shipped_arms()
        flags = {a['name']: a['mirror_main'] for a in cfg['arms']}
        self.assertEqual(flags, {'hold2m': False, 'tp110': False, 'nocluster_hold2m': False, 'live_copy': True})
        with self.assertRaises(W.ArmsConfigError):
            W.config({'arms': [{'name': 'x', 'strategy_config': 'strategy-default.json', 'mirror_main': 'yes'}]}, base_dir=ROOT / 'config' / 'lean')

    def test_it_enters_only_when_the_main_arm_bought(self):
        run = E.Run(self)
        run.seed_clusters()
        run.drive()
        main_bought = {run.role[f['mint']] for f in run.r.store.rows('fills', limit=10000) if f['side'] == 'buy'}
        copied = {run.role[f['mint']] for f in run.rows('arm_fills', 'live_copy') if f['side'] == 'buy'}
        self.assertEqual(copied, main_bought)
        for d in run.decisions('live_copy'):
            if run.role[d['mint']] not in main_bought:
                self.assertEqual((d['action'], d['reasons']), ('SKIP', ['MAIN_DID_NOT_BUY']), run.role[d['mint']])
        self.assertEqual(run.r.arms.arms[3].counts['entries'], len(main_bought))

    def test_when_the_main_gate_refuses_it_does_not_trade_and_costs_no_call(self):
        run = solo(self)
        run.drive(until=T0 + 60)
        d = run.decisions('live_copy', 'solo')
        self.assertEqual([(x['action'], x['reasons']) for x in d], [('SKIP', ['MAIN_DID_NOT_BUY'])])
        self.assertEqual(run.rows('arm_fills', 'live_copy'), [])
        self.assertEqual(run.decisions('hold2m', 'solo')[0]['action'], 'BUY')                  # the other arms still trade it


class ForcedMainExits(unittest.TestCase):
    def build_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(os.path.realpath(tmp.name)) / 'lean.sqlite', initial_cash_sol='100', code_version='t', strategy_version='t')
        self.addCleanup(store.close)
        cfg = paper.PaperConfig(fee_lamports=50_000, slippage_bps=50)
        ts = 1000.0
        exits = {'CLEAN': ({'reason': 'STOP'}, 'closed'), 'BOT': ({'reason': 'BOT_EXIT'}, 'closed'), 'RUG': ({'reason': 'RUG_WRITEOFF'}, 'closed'),
                 'DANGER': ({'reason': 'DANGER', 'rug_trigger': {'why': 'x'}}, 'closed'), 'D2': ({'reason': 'UNEXITABLE'}, 'closed'),
                 'UNVERIFIED': ({'reason': 'STOP', 'execution': {'side': 'sell', 'sell_status': 'SELL_UNVERIFIED'}}, 'closed'),
                 'WRITTEN_OFF': ({'reason': 'STOP', 'execution': {'kind': 'write_off', 'write_off_reason': 'SELL_UNVERIFIED'}}, 'closed'),
                 'RUNG_ONLY': ({'last_reason': 'TAKE_PROFIT'}, 'rung')}
        for mint, (state, event) in exits.items():
            buy = paper.buy(paper.Quote(mint, 'buy', 100_000_000, 1000, 6, ts), D('0.1'), cfg)
            store.add_fill(buy, state={'event': 'open', 'state': {'stage': 0}})
            opened = store.rows('fills', mint=mint)[-1]['id']
            position = store.positions()[mint]
            sell = paper.sell(position, paper.Quote(mint, 'sell', buy.qty_raw, 90_000_000, 6, ts + 100), cfg=cfg, qty_raw=buy.qty_raw)
            store.add_fill(sell, state={'event': event, 'open_fill_id': opened, 'state': state})
            ts += 200
        return store

    def test_trades_that_ended_by_an_exit_the_arms_do_not_model_are_flagged(self):
        store = self.build_store()
        forced = W.forced_main_exits(report.open_ro(store.path))
        self.assertEqual(forced, {'BOT': ['L23:BOT_EXIT'], 'RUG': ['L11:RUG_WRITEOFF'], 'DANGER': ['L11:DANGER', 'L11:rug_trigger'],
                                  'D2': ['D2:UNEXITABLE'], 'UNVERIFIED': ['L20:SELL_UNVERIFIED'], 'WRITTEN_OFF': ['L20:SELL_UNVERIFIED']})
        self.assertEqual(W.forced_main_exits(sqlite3_memory()), {})                           # not a lean store: nothing

    def test_a_flagged_main_trade_is_not_compared(self):
        mine = [{'mint': m, 'ret': D('0.1'), 'entry_at': 1.0} for m in ('A', 'B', 'C')]
        main = [{'mint': 'A', 'ret': D('0.0'), 'entry_at': 1.0, 'forced': []},
                {'mint': 'B', 'ret': D('-0.9'), 'entry_at': 1.0, 'forced': ['L11:RUG_WRITEOFF']},
                {'mint': 'C', 'ret': D('0.0'), 'entry_at': 1.0}]
        p = W.paired(mine, main)
        self.assertEqual((p['pairs'], p['arm_only'], p['main_only']), (2, 0, 0))
        self.assertAlmostEqual(p['mean_diff'], 0.1)
        self.assertEqual(p['main_forced_excluded'], [{'mint': 'B', 'labels': ['L11:RUG_WRITEOFF']}])

    def test_the_report_lists_them_and_leaves_them_out_of_the_comparison(self):
        run = E.Run(self)
        run.seed_clusters()
        run.drive()
        plain = report.build(run.r.store.path, now=run.clock.time(), arms_db=run.r.arms.store.path)['arms']['data']
        flagged = run.by_role['flat_a'].mint
        with mock.patch.object(W, 'forced_main_exits', return_value={flagged: ['L23:BOT_EXIT']}):
            summary = report.build(run.r.store.path, now=run.clock.time(), arms_db=run.r.arms.store.path)
        data = summary['arms']['data']
        before, after = plain['arms']['live_copy']['paired_vs_main'], data['arms']['live_copy']['paired_vs_main']
        self.assertEqual(before['main_forced_excluded'], [])
        self.assertEqual(after['pairs'], before['pairs'] - 1)                                 # flat_a is no longer compared ...
        self.assertEqual(after['main_forced_excluded'], [{'mint': flagged, 'labels': ['L23:BOT_EXIT']}])    # ... and is named
        self.assertEqual(data['main']['forced_exits'], {flagged: ['L23:BOT_EXIT']})
        self.assertIn('forced exits', report.render_html(summary))


def sqlite3_memory():
    import sqlite3
    return sqlite3.connect(':memory:')


# ---------------------------------------------------------------------------------------------------------- 5 filter health
class FilterHealth(unittest.TestCase):
    def test_the_not_ready_share_is_in_the_arms_health(self):
        run = E.Run(self)
        run.seed_clusters()
        run.drive()
        health = json.loads((run.state / 'health.json').read_text())['arms']['arms']
        f = health['nocluster_hold2m']['filter']
        decisions = [d for d in run.decisions('nocluster_hold2m') if 'entry_filter' in d['features']]
        not_ready = [d for d in decisions if d['reasons'] == ['FEATURE_NOT_READY']]
        self.assertEqual((f['evaluated'], f['not_ready']), (len(decisions), len(not_ready)))
        self.assertEqual(f['not_ready_share'], round(len(not_ready) / len(decisions), 4))
        self.assertEqual(f['pass'] + f['reject'] + f['not_ready'], f['evaluated'])
        why = {}
        for d in not_ready:
            why[d['features']['entry_filter']['why']] = why.get(d['features']['entry_filter']['why'], 0) + 1
        self.assertEqual(f['not_ready_why'], why)
        self.assertEqual(set(why), {'NO_ROW', 'WRITTEN_AFTER_DECISION', 'VALUE_NULL', 'AT_COLLECTION'})
        for name in ('hold2m', 'tp110', 'live_copy'):
            self.assertNotIn('filter', health[name])                                           # an arm without a filter has no share

    def test_a_pipeline_that_never_has_the_figure_reads_100_percent(self):
        run = E.Run(self, clusters={})
        run.drive()
        f = run.r.arms.health()['arms']['nocluster_hold2m']['filter']
        self.assertEqual(f['not_ready_share'], 1.0)
        self.assertEqual(f['pass'] + f['reject'], 0)

    def test_a_deferred_candidate_is_counted_once(self):
        run = E.Run(self)
        run.seed_clusters()
        run.r.arms.arms[2].filter['wait_s'] = 200.0
        run.drive(until=T0 + 1200)
        f = run.r.arms.health()['arms']['nocluster_hold2m']['filter']
        candidates = {d['candidate_id'] for d in run.decisions('nocluster_hold2m') if 'entry_filter' in d['features']}
        self.assertEqual(f['evaluated'], len(candidates))                                      # a DEFER and its later verdict are ONE candidate


# --------------------------------------------------------------------------------------------------------------- 6 the pin
class PinnedMarks(unittest.TestCase):
    def test_an_arm_position_records_the_model_it_was_opened_with(self):
        run = E.Run(self)
        run.drive(until=T0 + 30)
        opened = [json.loads(s['state']) for s in run.rows('arm_states', 'hold2m') if s['event'] == 'open']
        self.assertTrue(opened)
        for state in opened:
            self.assertEqual((state['pool_info']['model'], state['pool_info']['pool_fee_bps']), ('vault', 25))
            self.assertIn('supply_raw', state['pool_info'])
        main_states = [json.loads(s['state']) for s in run.r.store.rows('position_state', limit=100) if s['event'] == 'open']
        self.assertEqual(opened[0]['pool_info'], main_states[0]['pool_info'])                  # the same pin the main arm writes

    def test_a_config_switch_does_not_re_mark_an_open_arm_position(self):
        run = E.Run(self)
        run.drive(until=T0 + 30)
        arms, arm = run.r.arms, run.r.arms.arms[0]
        mint = next(iter(arm.book.positions))
        position = arm.book.positions[mint]
        arm.book.states[mint]['state']['pool_info']['pool_fee_bps'] = 200                       # the position's pinned vault fee
        arms.model.model = 'effective'                                                          # the arms' configured model changes under it
        arms.model.cfg = dict(arms.model.cfg, model='effective')
        run.clock.t = T0 + 40
        arm.marks.clear()
        with arms._lock:
            arms._accounts.clear()
        arms._refresh_marks(run.clock.time(), {mint})
        mark, mark_at, source = arm.marks[mint]
        base, quote = run.by_role['solo' if 'solo' in run.by_role else 'flat_a'].reserves(mark_at)
        expected = A.mark(position.qty_raw, base, quote, pool_fee_bps=200, pcfg=arm.pcfg)
        self.assertEqual(mark, expected)

    def test_an_unpinned_old_position_is_marked_as_the_runner_does(self):
        run = E.Run(self)
        run.drive(until=T0 + 30)
        arms, arm = run.r.arms, run.r.arms.arms[0]
        mint = next(iter(arm.book.positions))
        arm.book.states[mint]['state'].pop('pool_info')
        self.assertEqual(arms._pin_of(arm.book.states[mint]['state']), arms.model.pinned(None))


# ------------------------------------------------------------------------------------------------------------ 7 the guard
class PerMintGuard(unittest.TestCase):
    def two_positions(self):
        run = E.Run(self, scenario=[('solo1', dict(), 0), ('solo2', dict(), 120)])
        refuse_main(run)
        r, clock, hold = run.r, run.clock, run.r.arms.arms[0]
        tick = T0
        while tick <= T0 + 60 and not hold.book.positions:
            if clock.time() < tick:
                clock.t = tick
            r.position_pass()
            r.candidate_pass()
            clock.advance(E.WAKE)
            r.arms.process()
            tick += 5
        self.assertEqual([run.role[m] for m in hold.book.positions], ['solo1'])
        return run, hold, next(iter(hold.book.positions)), tick

    def test_one_position_that_cannot_be_laid_out_does_not_cost_the_others_their_marks_and_exits(self):
        run, hold, bad, tick = self.two_positions()
        hold.book.states[bad]['state'].pop('base_vault')                                      # a corrupt state: the low-lane read cannot be laid out
        run.drive(start=tick, until=T0 + 400, step=5)
        trades = run.trades('hold2m')
        self.assertEqual(trades['solo2']['reason'], 'MAX_HOLD')
        self.assertLessEqual(trades['solo2']['hold_s'], 120 + 12)
        self.assertGreater(run.r.arms.counts['marks_layout_errors'], 0)                       # counted, not raised
        bad_exit = [d for d in run.decisions('hold2m', 'solo1', kind='exit')][-1]['features']
        self.assertEqual(bad_exit['mark_source'], 'quote')                                    # the bad one is valued by a sell quote (D1) instead
        self.assertIsNone(hold.halted)

    def test_one_position_that_fails_its_exit_decision_does_not_stop_the_others(self):
        run, hold, bad, tick = self.two_positions()
        hold.book.states[bad]['state'].pop('initial_qty_raw')                                 # strategy_position raises for this one
        run.drive(start=tick, until=T0 + 400, step=5)
        self.assertEqual(run.trades('hold2m')['solo2']['reason'], 'MAX_HOLD')
        self.assertGreater(hold.counts['failures'], 0)

    def test_one_mark_that_cannot_be_computed_does_not_stop_the_other_arms(self):
        run, hold, bad, tick = self.two_positions()
        real = run.r.arms.model.value_accounts

        def broken(qty, accounts, **kw):
            if kw.get('pool_info', {}).get('boom'):
                raise TypeError('boom')
            return real(qty, accounts, **kw)
        hold.book.states[bad]['state']['pool_info']['boom'] = True
        with mock.patch.object(run.r.arms.model, 'value_accounts', broken):
            run.drive(start=tick, until=T0 + 400, step=5)
        self.assertEqual(run.trades('hold2m')['solo2']['reason'], 'MAX_HOLD')
        self.assertGreater(hold.counts['failures'], 0)


# ---------------------------------------------------------------------------------------------------------- 8 the reserve
class LaneReserve(unittest.TestCase):
    def test_a_call_leaves_the_lane_one_token_for_its_other_users(self):
        run = E.Run(self)
        arms = run.r.arms
        limiter = arms.low.helius.transport.limiter
        self.assertGreaterEqual(limiter.available(), 2.0)
        self.assertTrue(arms._lane_open(arms.low.helius))                                     # full bucket: a call may go
        limiter.acquire()                                                                     # someone else took one: 1 token left < 1 + reserve
        self.assertFalse(arms._lane_open(arms.low.helius))
        self.assertEqual(arms.counts['lane_yielded'], 1)
        run.clock.advance(2)                                                                  # refilled
        self.assertTrue(arms._lane_open(arms.low.helius))
        limiter.acquire()
        arms.cfg['reserve_tokens'] = 0                                                        # reserve off: any token will do
        self.assertTrue(arms._lane_open(arms.low.helius))

    def test_a_bucket_that_cannot_hold_the_reserve_needs_to_be_full(self):
        run = E.Run(self)
        arms = run.r.arms
        limiter = arms.low.jupiter.transport.limiter
        self.assertLess(limiter.burst, 2.0)                                                   # the Jupiter low lane holds about 1 token
        self.assertTrue(arms._lane_open(arms.low.jupiter))
        limiter.acquire()
        self.assertFalse(arms._lane_open(arms.low.jupiter))
        run.clock.advance(2)
        self.assertTrue(arms._lane_open(arms.low.jupiter))

    def test_an_entry_yields_to_the_lane_and_is_recorded_as_a_shed(self):
        run = solo(self)
        run.r.arms.low.jupiter.transport.limiter.acquire()                                    # the lane is busy this second
        run.r.position_pass()
        run.r.candidate_pass()
        d = run.decisions('hold2m', 'solo')
        self.assertEqual([(x['action'], x['reasons']) for x in d], [('SKIP', ['ARM_SKIPPED_SHED'])])
        self.assertEqual(d[0]['features']['shed'], 'reserve')
        self.assertEqual(run.r.arms.calls['jupiter_entry_quote'], 0)

    def test_a_marks_read_yields_too(self):
        run = solo(self)
        run.drive(until=T0 + 30)
        arms, arm = run.r.arms, run.r.arms.arms[0]
        mint = next(iter(arm.book.positions))
        helius = arms.low.helius.transport.limiter
        helius.acquire()
        calls = arms.calls['helius_marks']
        self.assertEqual(arms._read_low([mint], {mint: 'vault'}, run.clock.time()), {})
        self.assertEqual(arms.calls['helius_marks'], calls)
        self.assertGreater(arms.counts['marks_shed'], 0)
        run.clock.advance(2)
        self.assertIn(mint, arms._read_low([mint], {mint: 'vault'}, run.clock.time()))

    def test_an_exit_waits_while_the_lane_keeps_its_reserve(self):
        run = E.Run(self)
        arms, arm = run.r.arms, run.r.arms.arms[0]
        pend = W.PendingExit(mint='M', open_id=1, decided_at=T0, due_at=T0, decided_int=T0, qty=1000, reason='STOP', mark=D(1), mark_at=T0,
                             mark_source='x', liquidate=False)
        run.clock.t = T0 + 5
        arms.low.jupiter.transport.limiter.acquire()
        with mock.patch.object(arms.low.jupiter, 'quote', side_effect=AssertionError('called')):
            self.assertIsNone(arms._price_exit(arm, pend, None, run.clock.time()))
        self.assertEqual(pend.retry_at, run.clock.time() + 1.0)
        self.assertEqual(arms.counts['exit_shed_waits'], 1)


if __name__ == '__main__':
    unittest.main()
