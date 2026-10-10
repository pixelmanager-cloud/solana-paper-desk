"""tools.research.forward_eval: synthetic ledgers with known answers (no network, no providers)."""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from desk.engine import initial_state, transition
from desk.ledger import Ledger
from desk import quote_execution as qe
from tests import test_quote_experiment_report as quote_fixtures
from unittest import mock
from tests.helpers import T, config, event
from tools.research import forward_eval as F

DAY = 100000
# (exit-event overrides, exit delay seconds) -> DANGER loss, STOP loss, DANGER win, TIME_STOP loss
SCRIPT = [(dict(danger=True), 3000), (dict(reserve_sol='40'), 3000),
          (dict(reserve_sol='160', danger=True), 3000), ({}, 3000)]


def build(path, cfg=None, script=SCRIPT, start=T, close_last=True, step=DAY):
    """Apply the script; return per-trade (cost, pnl, reason, hold, closed_at) taken from raw outcomes."""
    cfg = cfg or config()
    ledger = Ledger(path)
    expected, t = [], start
    for i, (changes, delay) in enumerate(script):
        mint = 'SYNTHETIC_%d' % i
        buy = [o for o in ledger.apply(event(t, mint=mint, graduated_at=t - 600), cfg, transition, initial_state)
               if o['type'] == 'fill'][0]
        if not close_last and i == len(script) - 1:
            break
        sell = [o for o in ledger.apply(event(t + delay, mint=mint, graduated_at=t - 600, **changes),
                                        cfg, transition, initial_state) if o['type'] == 'fill'][0]
        expected.append({'cost': D(buy['amount_sol']) + D(buy['fee_sol']), 'pnl': D(sell['realized_pnl_sol']),
                         'reason': sell['reason'], 'hold': delay, 'closed_at': t + delay, 'opened_at': t,
                         'qty': D(buy['quantity']), 'exit_event': event(t + delay, mint=mint, graduated_at=t - 600, **changes)})
        t += step
    ledger.close()
    return expected


def model_ratio(e, qty, cost, cfg=None):
    """Independent constant-product mark / cost (same formula as desk.strategy.swap_quote, written out)."""
    cfg = cfg or config()
    eff = qty * (1 - D(e['pool_fee_bps']) / 10000)
    out = D(e['reserve_sol']) * eff / (D(e['reserve_tokens']) + eff) * (1 - D(cfg['adverse_slippage_bps']) / 10000)
    return max(D(0), out - D(cfg['fixed_fee_sol'])) / cost


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = F.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ForwardEvalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def ledger(self, name='a.sqlite', **kw):
        path = self.dir / name
        return path, build(path, **kw)

    def test_known_answers_single_experiment(self):
        path, exp = self.ledger()
        report = F.evaluate([path], now=T + 10 * DAY)
        self.assertEqual(report['execution_status'], 'EXECUTION_UNVERIFIED')
        self.assertTrue(report['paper_only'])
        self.assertEqual(report['profitability_verdict'], 'NOT_ASSESSED')
        (row,) = report['experiments']
        self.assertEqual(row['execution_status'], 'EXECUTION_UNVERIFIED')
        a = row['all']
        returns = sorted(t['pnl'] / t['cost'] for t in exp)
        self.assertEqual(a['closed_trades'], 4)
        self.assertEqual((a['wins'], a['losses'], a['flat']), (1, 3, 0))
        self.assertEqual(D(a['net_pnl_sol']), sum(t['pnl'] for t in exp))
        self.assertAlmostEqual(D(a['mean_return']), sum(returns) / 4, places=25)
        self.assertAlmostEqual(D(a['median_return']), (returns[1] + returns[2]) / 2, places=25)
        self.assertEqual(a['exit_reasons'], {'DANGER': 2, 'STOP': 1, 'TIME_STOP': 1})
        self.assertEqual(D(a['mean_hold_seconds']), D(3000))
        self.assertIn('INSUFFICIENT_SAMPLE', a['warning'])
        # one held event per trade (the exit event itself): MFE == MAE == model ratio - 1 on that event
        self.assertEqual(a['mfe_mae']['status'], 'OK')
        self.assertEqual(a['mfe_mae']['trades_with_marks'], 4)
        ratios = [model_ratio(t['exit_event'], t['qty'], t['cost']) - 1 for t in exp]
        self.assertAlmostEqual(D(a['mfe_mae']['mean_mfe']), sum(ratios) / 4, places=20)
        self.assertAlmostEqual(D(a['mfe_mae']['min_mae']), min(ratios), places=20)
        self.assertAlmostEqual(D(a['mfe_mae']['max_mfe']), max(ratios), places=20)
        # drawdown from the closed-trade equity curve, computed independently
        equity = peak = D(config()['initial_equity_sol']); worst = D(0)
        for t in exp:
            equity += t['pnl']; peak = max(peak, equity); worst = max(worst, (peak - equity) / peak)
        self.assertAlmostEqual(D(a['max_drawdown_fraction']), worst, places=25)
        self.assertGreater(worst, 0)

    def test_partial_take_profit_then_final_exit_is_one_trade_with_final_reason(self):
        path = self.dir / 'tp.sqlite'
        cfg, ledger = config(), Ledger(path)
        apply = lambda e: ledger.apply(e, cfg, transition, initial_state)
        buy = apply(event(T))[0]
        e5, e10 = event(T + 5, reserve_sol='160'), event(T + 10, reserve_sol='120', danger=True)
        fills = [o for e in (e5, e10) for o in apply(e) if o['type'] == 'fill']
        ledger.close()
        self.assertEqual([f['reason'] for f in fills], ['TAKE_PROFIT', 'DANGER'])
        (row,) = F.evaluate([path], now=T + 10)['experiments']
        a = row['all']
        self.assertEqual(a['closed_trades'], 1)
        self.assertEqual(a['exit_reasons'], {'DANGER': 1})
        self.assertEqual(a['partial_take_profit_fills'], 1)
        self.assertEqual(D(a['mean_hold_seconds']), 10)
        cost = D(buy['amount_sol']) + D(buy['fee_sol'])
        self.assertAlmostEqual(D(a['mean_return']), sum(D(f['realized_pnl_sol']) for f in fills) / cost, places=25)
        # marks: e5 sees the full position; e10 sees what is left after the partial sell (qty AND cost shrink)
        qty, sold = D(buy['quantity']), D(fills[0]['quantity'])
        r5 = model_ratio(e5, qty, cost)
        r10 = model_ratio(e10, qty - sold, cost * (1 - sold / qty))
        self.assertNotAlmostEqual(r5, r10, places=3)
        trade = row['trades'][0]
        self.assertAlmostEqual(D(trade['mfe']), max(r5, r10) - 1, places=15)
        self.assertAlmostEqual(D(trade['mae']), min(r5, r10) - 1, places=15)

    def test_flat_trade_counts_as_neither_win_nor_loss(self):
        trades = [dict(pnl=D(0), cost=D(1), **{'return': D(0)}, hold_seconds=5, exit_reason='TIME_STOP',
                       reasons=['TIME_STOP'], closed_seq=1, closed_at=1, ledger='x',
                       marks={'status': 'NO_HELD_MARKS', 'mfe': None, 'mae': None, 'samples': 0})]
        a = F.block(trades, D(5), 50, 0)
        self.assertEqual((a['wins'], a['losses'], a['flat']), (0, 0, 1))

    def hold_ledger(self, ratios_reserve):
        """One trade held through several market events (no fills) then a DANGER exit."""
        path = self.dir / 'hold.sqlite'
        cfg, ledger = config(), Ledger(path)
        apply = lambda e: ledger.apply(e, cfg, transition, initial_state)
        buy = [o for o in apply(event(T)) if o['type'] == 'fill'][0]
        events = [event(T + 60 * (i + 1), reserve_sol=r) for i, r in enumerate(ratios_reserve)]
        events.append(event(T + 60 * (len(events) + 1), reserve_sol='100', danger=True))
        fills = []
        for e in events:
            fills += [o for o in apply(e) if o['type'] == 'fill']
        ledger.close()
        self.assertEqual([f['reason'] for f in fills], ['DANGER'], 'held events must not trade')
        return path, buy, events

    def test_mfe_mae_from_journaled_held_events_known_answer(self):
        path, buy, events = self.hold_ledger(['115', '90'])
        cost = D(buy['amount_sol']) + D(buy['fee_sol'])
        ratios = [model_ratio(e, D(buy['quantity']), cost) for e in events]
        self.assertGreater(max(ratios), 1)
        self.assertLess(min(ratios), 1)
        row = F.evaluate([path], now=T + 1000)['experiments'][0]
        marks = row['all']['mfe_mae']
        self.assertEqual((marks['status'], marks['trades_with_marks']), ('OK', 1))
        self.assertAlmostEqual(D(marks['max_mfe']), max(ratios) - 1, places=20)
        self.assertAlmostEqual(D(marks['min_mae']), min(ratios) - 1, places=20)
        self.assertNotEqual(marks['max_mfe'], marks['min_mae'])
        (trade,) = row['trades']
        self.assertAlmostEqual(D(trade['mfe']), max(ratios) - 1, places=20)
        self.assertAlmostEqual(D(trade['mae']), min(ratios) - 1, places=20)

    def test_mfe_mae_ignores_stale_events_and_events_after_the_exit(self):
        path = self.dir / 'win.sqlite'
        cfg, ledger = config(), Ledger(path)
        apply = lambda e: ledger.apply(e, cfg, transition, initial_state)
        before = apply(event(T - 60, reserve_sol='5000', danger=True))  # rejected pre-entry event for the same mint
        self.assertEqual([o['type'] for o in before], ['reject'])
        buy = [o for o in apply(event(T)) if o['type'] == 'fill'][0]
        stale = event(T + 90, reserve_sol='1000', price_at=T + 90 - cfg['price_ttl_seconds'] - 20)
        held = [event(T + 60, reserve_sol='115'), event(T + 120, reserve_sol='90'),
                event(T + 180, reserve_sol='100', danger=True)]
        fills = []
        for e in (held[0], stale, held[1], held[2], event(T + 240, reserve_sol='5000')):
            fills += [o for o in apply(e) if o['type'] == 'fill']
        ledger.close()
        self.assertEqual([f['reason'] for f in fills], ['DANGER'])
        cost = D(buy['amount_sol']) + D(buy['fee_sol'])
        ratios = [model_ratio(e, D(buy['quantity']), cost) - 1 for e in held]
        self.assertGreater(model_ratio(stale, D(buy['quantity']), cost), 3)  # would dominate if wrongly counted
        (row,) = F.evaluate([path], now=T + 300)['experiments']
        self.assertAlmostEqual(D(row['trades'][0]['mfe']), max(ratios), places=20)
        self.assertAlmostEqual(D(row['trades'][0]['mae']), min(ratios), places=20)

    def test_mfe_mae_unavailable_only_in_quote_mode_and_detected(self):
        case = quote_fixtures.QuoteReportTests()
        c, raw, _ = case.lifecycle.__func__(_Adapter(self))
        out = c.apply(c.market(T + 2, danger=True), (c.quote('sell', raw - raw * 3 // 10, 12_000_000, at=T + 2),))
        self.assertEqual([o['reason'] for o in out if o['type'] == 'fill'], ['DANGER'])
        row = F.evaluate([c.path], now=T + 2)['experiments'][0]
        self.assertTrue(row['quote_mode'])
        self.assertEqual(row['all']['closed_trades'], 1)
        self.assertEqual(row['all']['mfe_mae']['status'], 'UNAVAILABLE_QUOTE_MODE')
        self.assertEqual(row['trades'][0]['mfe_mae_status'], 'UNAVAILABLE_QUOTE_MODE')
        # a constant-product ledger with no held events between entry and exit is a different, named case
        path, _ = self.ledger(script=[({}, 3000)])
        self.assertEqual(F.evaluate([path], now=T + 10 * DAY)['experiments'][0]['all']['mfe_mae']['status'], 'OK')

    def test_concurrent_positions_buy_a_buy_b_sell_b_sell_a(self):
        path = self.dir / 'conc.sqlite'
        cfg, ledger = config(), Ledger(path)
        fills = []
        def apply(e):
            out = [o for o in ledger.apply(e, cfg, transition, initial_state) if o['type'] == 'fill']
            fills.extend(out)
            return out
        buy_a = apply(event(T, mint='SYNTHETIC_A', graduated_at=T - 600))[0]
        # entries are rejected (STALE_PORTFOLIO) unless every open position has a fresh mark
        a_early = event(T + 118, mint='SYNTHETIC_A', graduated_at=T - 600)
        self.assertEqual(apply(a_early), [])
        buy_b = apply(event(T + 120, mint='SYNTHETIC_B', graduated_at=T - 600))[0]
        self.assertEqual((buy_a['side'], buy_b['side']), ('buy', 'buy'))
        a_mid = event(T + 180, mint='SYNTHETIC_A', graduated_at=T - 600, reserve_sol='112')
        self.assertEqual(apply(a_mid), [])
        sell_b = apply(event(T + 240, mint='SYNTHETIC_B', graduated_at=T - 600, danger=True, reserve_sol='95'))[0]
        a_exit = event(T + 360, mint='SYNTHETIC_A', graduated_at=T - 600, danger=True, reserve_sol='105')
        sell_a = apply(a_exit)[0]
        ledger.close()
        self.assertEqual([(f['side'], f['mint']) for f in fills], [('buy', 'SYNTHETIC_A'), ('buy', 'SYNTHETIC_B'),
                         ('sell', 'SYNTHETIC_B'), ('sell', 'SYNTHETIC_A')])
        cost_a = D(buy_a['amount_sol']) + D(buy_a['fee_sol'])
        cost_b = D(buy_b['amount_sol']) + D(buy_b['fee_sol'])
        row = F.evaluate([path], now=T + 400)['experiments'][0]
        by_mint = {t['mint']: t for t in row['trades']}
        self.assertEqual(row['all']['closed_trades'], 2)
        self.assertEqual((by_mint['SYNTHETIC_A']['closed_at'] - by_mint['SYNTHETIC_A']['opened_at'],
                          by_mint['SYNTHETIC_B']['closed_at'] - by_mint['SYNTHETIC_B']['opened_at']), (360, 120))
        self.assertEqual(D(by_mint['SYNTHETIC_A']['pnl_sol']), D(sell_a['realized_pnl_sol']))
        self.assertEqual(D(by_mint['SYNTHETIC_B']['pnl_sol']), D(sell_b['realized_pnl_sol']))
        self.assertEqual([t['mint'] for t in row['trades']], ['SYNTHETIC_B', 'SYNTHETIC_A'])  # close order
        self.assertEqual(D(row['all']['net_pnl_sol']), D(sell_a['realized_pnl_sol']) + D(sell_b['realized_pnl_sol']))
        # A's marks come only from A's own held events (default, 112, 105 reserves), never from B's
        qty_a = D(buy_a['quantity'])
        ratios = [model_ratio(e, qty_a, cost_a) - 1 for e in (a_early, a_mid, a_exit)]
        self.assertAlmostEqual(D(by_mint['SYNTHETIC_A']['mfe']), max(ratios), places=20)
        self.assertAlmostEqual(D(by_mint['SYNTHETIC_A']['mae']), min(ratios), places=20)
        self.assertAlmostEqual(D(by_mint['SYNTHETIC_B']['mfe']),
                               model_ratio(event(T + 240, mint='SYNTHETIC_B', reserve_sol='95'), D(buy_b['quantity']), cost_b) - 1,
                               places=20)

    def test_pooled_ordering_is_by_close_time_and_independent_of_argument_order(self):
        # a: loss at T, big win ~10 days later (outcome seqs 2, 4). b: three losses on days 1-3 (seqs 2, 4, 6).
        # Close-time order a0,b0,b1,b2,a1 differs from per-ledger-seq order, and so does the drawdown.
        a, ea = self.ledger('a.sqlite', script=[(dict(reserve_sol='40'), 3000), (dict(reserve_sol='160', danger=True), 3000)],
                            step=10 * DAY)
        b, eb = self.ledger('b.sqlite', script=[(dict(reserve_sol='40'), 3000)] * 3, start=T + DAY)
        forward = F.evaluate([a, b], now=T + 20 * DAY)
        backward = F.evaluate([b, a], now=T + 20 * DAY)
        self.assertEqual(json.dumps(forward, sort_keys=True), json.dumps(backward, sort_keys=True))
        (row,) = forward['experiments']
        closed = [t['closed_at'] for t in row['trades']]
        self.assertEqual(closed, sorted(closed))
        self.assertEqual([t['ledger'].endswith('a.sqlite') for t in row['trades']], [True, False, False, False, True])
        # independent drawdown over the pooled close-time order, one baseline
        initial = D(config()['initial_equity_sol'])
        def drawdown(trades):
            equity = peak = initial; worst = D(0)
            for t in trades:
                equity += t['pnl']; peak = max(peak, equity); worst = max(worst, (peak - equity) / peak)
            return worst
        by_time = drawdown(sorted(ea + eb, key=lambda t: t['closed_at']))
        by_seq = drawdown([ea[0], eb[0], ea[1], eb[1], eb[2]])
        self.assertNotAlmostEqual(by_time, by_seq, places=6)
        self.assertAlmostEqual(D(row['all']['max_drawdown_fraction']), by_time, places=25)
        self.assertEqual(row['equity_baseline_sol'], str(initial))
        self.assertIn('SEQUENTIAL_SINGLE_BASELINE', row['pooled_equity_model'])

    def test_same_config_different_implementation_hashes_are_separate_rows_unless_pooled(self):
        a, ea = self.ledger('a.sqlite')
        b, eb = self.ledger('b.sqlite', script=SCRIPT[:2], start=T + 50 * DAY)
        # desk.experiment_report pins a ledger to the running code's hash (RUNTIME_IDENTITY_INVALID otherwise),
        # so relabel b after the real validated read to model a ledger written by another code version
        real = F.read_ledger

        def relabel(path, now=None):
            ledger = real(path, now)
            return {**ledger, 'implementation_hash': 'ab' * 32} if str(path) == str(b) else ledger

        patcher = mock.patch.object(F, 'read_ledger', relabel)
        patcher.start(); self.addCleanup(patcher.stop)
        rows = F.evaluate([a, b], now=T + 60 * DAY)['experiments']
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r['all']['closed_trades'] for r in rows), [2, 4])
        self.assertEqual(len({r['config_hash'] for r in rows}), 1)
        self.assertEqual(len({tuple(r['implementation_hashes']) for r in rows}), 2)
        pooled = F.evaluate([a, b], now=T + 60 * DAY, pool_implementations=True)
        (row,) = pooled['experiments']
        self.assertEqual(row['all']['closed_trades'], 6)
        self.assertEqual(len(row['implementation_hashes']), 2)
        self.assertIn('POOLED_IMPLEMENTATIONS', row['warnings'][0])
        code, out, err = run_main('--ledger', str(a), '--ledger', str(b), '--now', str(T + 60 * DAY),
                                  '--pool-implementations')
        self.assertEqual(code, 0)
        self.assertIn('POOLED_IMPLEMENTATIONS', err)
        self.assertEqual(len(json.loads(run_main('--ledger', str(a), '--ledger', str(b),
                                                 '--now', str(T + 60 * DAY))[1])['experiments']), 2)

    def test_ledger_changing_between_validation_and_read_is_refused(self):
        path, _ = self.ledger()
        real = F.experiment_report

        def validate_then_append(p, now=None):
            report = real(p, now=now)
            ledger = Ledger(p, must_exist=True)
            ledger.apply(event(T + 90 * DAY, mint='SYNTHETIC_LATE', graduated_at=T + 90 * DAY - 600),
                         config(), transition, initial_state)
            ledger.close()
            return report

        with mock.patch.object(F, 'experiment_report', validate_then_append):
            with self.assertRaises(F.EvalError) as caught:
                F.evaluate([path], now=T + 100 * DAY)
        self.assertIn('LEDGER_CHANGED_DURING_EVAL', str(caught.exception))
        # sanity: unchanged ledger evaluates
        self.assertEqual(F.evaluate([path], now=T + 100 * DAY)['experiments'][0]['all']['closed_trades'], 4)

    def test_ledger_changing_after_the_read_is_refused(self):
        path, _ = self.ledger()
        real, calls = F._connect, []

        def connect(p):
            calls.append(1)
            if len(calls) == 3:  # 1 pre-fingerprint, 2 snapshot read, 3 post-read check
                ledger = Ledger(p, must_exist=True)
                ledger.apply(event(T + 90 * DAY, mint='SYNTHETIC_LATE', graduated_at=T + 90 * DAY - 600),
                             config(), transition, initial_state)
                ledger.close()
            return real(p)

        with mock.patch.object(F, '_connect', connect):
            with self.assertRaises(F.EvalError) as caught:
                F.evaluate([path], now=T + 100 * DAY)
        self.assertIn('after read', str(caught.exception))

    def test_insufficient_sample_is_printed_not_hidden(self):
        path, _ = self.ledger()
        code, out, err = run_main('--ledger', str(path), '--now', str(T + 10 * DAY))
        self.assertEqual(code, 0)
        self.assertIn('WARNING', err)
        self.assertIn('INSUFFICIENT_SAMPLE', err)
        self.assertIn('INSUFFICIENT_SAMPLE', json.loads(out)['experiments'][0]['all']['warning'])

    def test_thirty_two_trades_drop_the_warning(self):
        path, exp = self.ledger(script=([(dict(reserve_sol='160', danger=True), 3000)] * 3 + [(dict(danger=True), 3000)]) * 8)
        row = F.evaluate([path], now=T + 40 * DAY, resamples=200)['experiments'][0]['all']
        self.assertEqual(row['closed_trades'], 32)
        self.assertNotIn('warning', row)
        self.assertAlmostEqual(D(row['net_pnl_sol']), sum(t['pnl'] for t in exp), places=25)

    def test_different_config_hashes_never_share_a_row(self):
        a, ea = self.ledger('a.sqlite')
        other = dict(config(), version='2026-10-11.other.1')
        b, eb = self.ledger('b.sqlite', cfg=other, script=SCRIPT[:2])
        report = F.evaluate([a, b], now=T + 10 * DAY)
        rows = {r['version']: r for r in report['experiments']}
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(*[r['config_hash'] for r in rows.values()])
        self.assertEqual(rows[config()['version']]['all']['closed_trades'], 4)
        self.assertEqual(rows['2026-10-11.other.1']['all']['closed_trades'], 2)
        self.assertEqual(D(rows['2026-10-11.other.1']['all']['net_pnl_sol']), sum(t['pnl'] for t in eb))

    def test_single_experiment_flag_refuses_mixing(self):
        a, _ = self.ledger('a.sqlite')
        b, _ = self.ledger('b.sqlite', cfg=dict(config(), version='x.1'))
        with self.assertRaises(F.EvalError):
            F.evaluate([a, b], now=T + 10 * DAY, single=True)
        code, out, err = run_main('--ledger', str(a), '--ledger', str(b), '--single-experiment')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('REFUSED', err)

    def test_same_config_ledgers_pool_and_duplicate_ledger_refused(self):
        a, ea = self.ledger('a.sqlite')
        b, eb = self.ledger('b.sqlite', script=SCRIPT[:1], start=T + 50 * DAY)
        (row,) = F.evaluate([a, b], now=T + 60 * DAY)['experiments']
        self.assertEqual(row['all']['closed_trades'], 5)
        self.assertEqual(len(row['ledgers']), 2)
        with self.assertRaises(F.EvalError):
            F.evaluate([a, a], now=T + 60 * DAY)
        os.link(a, self.dir / 'hard.sqlite')
        with self.assertRaises(F.EvalError):
            F.evaluate([a, self.dir / 'hard.sqlite'], now=T + 60 * DAY)

    def test_holdout_splits_on_entry_time_and_starts_from_equity_at_the_cut(self):
        path, exp = self.ledger()
        initial = D(config()['initial_equity_sol'])
        # cut between trade 1's entry and its exit: trade 1 was ENTERED before the cut, so it is train
        # even though it closes after the cut; trade 0 closed before the cut.
        cut = exp[1]['opened_at'] + 100
        row = F.evaluate([path], holdout_from=cut, now=T + 10 * DAY)['experiments'][0]
        self.assertEqual(row['split_basis'], 'ENTRY_TIME')
        self.assertEqual((row['train']['closed_trades'], row['holdout']['closed_trades']), (2, 2))
        self.assertEqual(D(row['train']['net_pnl_sol']), exp[0]['pnl'] + exp[1]['pnl'])
        self.assertEqual(D(row['holdout']['net_pnl_sol']), exp[2]['pnl'] + exp[3]['pnl'])
        self.assertEqual(D(row['train']['start_equity_sol']), initial)
        self.assertEqual(D(row['holdout']['start_equity_sol']), initial + exp[0]['pnl'])
        equity = peak = initial + exp[0]['pnl']; worst = D(0)
        for t in exp[2:]:
            equity += t['pnl']; peak = max(peak, equity); worst = max(worst, (peak - equity) / peak)
        self.assertAlmostEqual(D(row['holdout']['max_drawdown_fraction']), worst, places=25)
        self.assertIn('INSUFFICIENT_SAMPLE', row['holdout']['warning'])
        # a trade entered exactly at the cut is holdout
        exact = F.evaluate([path], holdout_from=exp[2]['opened_at'], now=T + 10 * DAY)['experiments'][0]
        self.assertEqual((exact['train']['closed_trades'], exact['holdout']['closed_trades']), (2, 2))
        self.assertEqual(F.parse_time('2026-10-10T00:00:00Z'), 1791590400)
        with self.assertRaises(F.EvalError):
            F.parse_time('2026-10-10T00:00:00')

    def test_empty_holdout_side_is_reported_not_crashing(self):
        path, _ = self.ledger()
        row = F.evaluate([path], holdout_from=T + 99 * DAY, now=T + 10 * DAY)['experiments'][0]
        self.assertEqual(row['holdout']['closed_trades'], 0)
        self.assertIn('INSUFFICIENT_SAMPLE', row['holdout']['warning'])
        self.assertEqual(row['train']['closed_trades'], 4)

    def test_only_open_positions(self):
        path, exp = self.ledger(script=[({}, 3000)], close_last=False)
        (row,) = F.evaluate([path], now=T + 10)['experiments']
        self.assertEqual(exp, [])
        self.assertEqual(row['all']['closed_trades'], 0)
        self.assertIn('INSUFFICIENT_SAMPLE', row['all']['warning'])
        self.assertEqual(row['open_positions_excluded'], 1)
        self.assertNotIn('net_pnl_sol', row['all'])

    def test_ledger_without_trades_is_zero_not_error(self):
        path = self.dir / 'rej.sqlite'
        ledger = Ledger(path)
        out = ledger.apply(event(T, danger=True, graduated_at=T - 99999), config(), transition, initial_state)
        ledger.close()
        self.assertTrue(all(o['type'] != 'fill' for o in out))
        (row,) = F.evaluate([path], now=T + 10)['experiments']
        self.assertEqual(row['all']['closed_trades'], 0)

    def test_empty_ledger_fails_closed(self):
        path = self.dir / 'empty.sqlite'
        Ledger(path).close()
        with self.assertRaises(F.EvalError):
            F.evaluate([path], now=T)
        self.assertEqual(run_main('--ledger', str(path))[0], 2)

    def test_bootstrap_ci_is_deterministic_and_bounded(self):
        values = [D(x) for x in ('-0.1', '0.05', '0.2', '-0.02', '0.07')]
        ci = F.bootstrap_ci(values, 500, 7)
        self.assertEqual(ci, F.bootstrap_ci(values, 500, 7))
        self.assertNotEqual(ci, F.bootstrap_ci(values, 500, 8))
        self.assertTrue(float(min(values)) <= ci[0] <= ci[1] <= float(max(values)))
        self.assertEqual(F.bootstrap_ci([D('0.1'), D('0.1'), D('0.1')], 100, 0), [0.1, 0.1])
        self.assertIsNone(F.bootstrap_ci([D('0.1')], 100, 0))

    def test_corrupt_or_tampered_ledger_refused(self):
        path, _ = self.ledger()
        with sqlite3.connect(path) as c:  # inflate a recorded realized PnL: replay must catch it
            seq, payload = c.execute("SELECT seq,payload FROM outcomes WHERE payload LIKE '%realized_pnl_sol%'").fetchone()
            fill = json.loads(payload); fill['realized_pnl_sol'] = '99'
            c.execute('UPDATE outcomes SET payload=? WHERE seq=?', (json.dumps(fill), seq))
        with self.assertRaises(F.EvalError):
            F.evaluate([path], now=T + 10 * DAY)

    def test_symlinked_ledger_refused_and_read_is_byte_identical(self):
        path, _ = self.ledger()
        link = self.dir / 'link.sqlite'
        os.symlink(path, link)
        with self.assertRaises(F.EvalError):
            F.evaluate([link], now=T + 10 * DAY)
        before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        run_main('--ledger', str(path), '--now', str(T + 10 * DAY))
        self.assertEqual((hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns), before)
        # a WAL-mode ledger opened mode=ro may get -shm/-wal sidecars from SQLite itself; the db file is untouched


class _Adapter:
    """Lets the existing quote-mode fixture helper run inside this TestCase (it only needs addCleanup)."""
    def __init__(self, case):
        self.addCleanup = case.addCleanup


if __name__ == '__main__':
    unittest.main()
