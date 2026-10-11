"""L06F: attribution by the ENTRY strategy_version on a REAL lean store (report AND notifier). A position opened under
``lean-1-collect`` and sold after a restart under ``lean-1-collect-b`` belongs to the first, completely.
SYNTHETIC_TEST_ONLY, no network."""
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from lean import notify as N, paper, report
from lean.store import Store

A, B = 'lean-1-collect', 'lean-1-collect-b'
M1, M2, M3 = 'M' * 32, 'N' * 32, 'O' * 32
PCFG = paper.PaperConfig(fee_lamports=50_000, slippage_bps=50)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(os.path.realpath(self.tmp.name)) / 'lean.sqlite'
        self.t = [1_800_000_000.0]
        self.store = Store(self.path, initial_cash_sol='10', code_version='c', strategy_version=A, clock=lambda: self.t[0])
        self.addCleanup(self.store.close)

    def tick(self):
        self.t[0] += 60
        return self.t[0]

    def buy(self, mint, version):
        q = paper.Quote(mint, 'buy', 80_000_000, 1_000_000_000, 6, self.tick())
        fill = paper.buy(q, '0.08', PCFG)
        self.store.add_fill(fill, strategy_version=version, state={'event': 'open', 'state': {'initial_qty_raw': fill.qty_raw, 'stage': 0}})
        return self.store.position_states()[mint]['open_fill_id']

    def sell(self, mint, version, open_id, fraction_of_held, out, reason):
        pos = self.store.positions()[mint]
        qty = pos.qty_raw if fraction_of_held is None else pos.qty_raw * fraction_of_held // 10
        fill = paper.sell(pos, paper.Quote(mint, 'sell', qty, out, 6, self.tick()), cfg=PCFG, qty_raw=qty)
        closing = qty == pos.qty_raw
        state = {'initial_qty_raw': 1, 'reason' if closing else 'last_reason': reason}
        if closing:
            state['trade_pnl_lamports'] = pos.realized_lamports + fill.realized_lamports
        self.store.add_fill(fill, strategy_version=version, state={'event': 'closed' if closing else 'rung', 'open_fill_id': open_id, 'state': state})

    def scenario(self):
        o1 = self.buy(M1, A)
        self.sell(M1, A, o1, 3, 30_000_000, 'TAKE_PROFIT')           # a rung under A
        self.sell(M1, B, o1, None, 50_000_000, 'STOP')               # after the restart: the rest under B
        o2 = self.buy(M2, B)                                          # a clean B trade
        self.sell(M2, B, o2, None, 120_000_000, 'TAKE_PROFIT')
        o3 = self.buy(M3, A)                                          # a clean A trade
        self.sell(M3, A, o3, None, 70_000_000, 'STOP')
        self.store.check_invariants()
        return o1, o2, o3


class ReportOnARealStore(Case):
    def test_trades_and_totals_by_entry_version(self):
        self.scenario()
        summary = report.build(self.path, now=self.t[0] + 100)
        by = summary['pnl_by_strategy']['data']
        self.assertEqual(sorted(by), [A, B])
        self.assertEqual((by[A]['trades'], by[A]['mixed_version_trades'], by[B]['trades']), (2, 1, 1))
        rows = {r['mint']: r for r in summary['trades']['data']['rows']}
        self.assertEqual((rows[M1]['strategy_version'], rows[M1]['mixed_version'], rows[M1]['fill_versions']), (A, 1, [A, B]))
        self.assertEqual((rows[M2]['mixed_version'], rows[M3]['mixed_version']), (0, 0))
        closed = {c['mint']: int(c['state']['trade_pnl_lamports']) for c in self.store.closed_positions()}
        self.assertAlmostEqual(by[A]['pnl_sol'], (closed[M1] + closed[M3]) / 1e9, places=6)
        self.assertAlmostEqual(by[B]['pnl_sol'], closed[M2] / 1e9, places=6)


class NotifierOnARealStore(Case):
    def setUp(self):
        super().setUp()
        self.o1, self.o2, self.o3 = self.scenario()
        self.store.close()
        self.view = N.StoreView(N.open_store(self.path))
        self.addCleanup(self.view.close)

    def test_closed_trades_carry_the_opening_version_and_the_mixed_flag(self):
        closed = {c['mint']: c for c in self.view.closed()}
        self.assertEqual((closed[M1]['strategy_version'], closed[M1]['mixed_version']), (A, True))
        self.assertEqual((closed[M2]['strategy_version'], closed[M2]['mixed_version']), (B, False))
        self.assertEqual((closed[M3]['strategy_version'], closed[M3]['mixed_version']), (A, False))

    def test_realized_per_version_is_by_opening_buy(self):
        realized = self.view.realized_by_opening_version()
        rows = self.view._fills()
        by_mint = lambda mint: sum(f['realized_lamports'] for f in rows if f['mint'] == mint and f['side'] == 'sell')
        self.assertEqual(realized, {A: by_mint(M1) + by_mint(M3), B: by_mint(M2)})
        self.assertEqual(sum(realized.values()), sum(f['realized_lamports'] for f in rows if f['side'] == 'sell'))

    def test_pnl_command_never_shows_the_sells_version_as_a_strategy(self):
        class Clock:
            def time(self): return 1_800_100_000.0
        n = N.Notifier.__new__(N.Notifier)
        text = N.Notifier.cmd_pnl(n, self.view, 1_800_100_000.0)
        lines = {l.split(':')[0]: l for l in text.split('\n')[1:]}
        self.assertEqual(sorted(lines), [A, B])
        self.assertIn('2건', lines[A])                  # M1 (mixed) + M3
        self.assertIn('1건', lines[B])
        realized = self.view.realized_by_opening_version()
        self.assertIn(N.fmt_sol(N.sol_of(realized[A]), sign=True), lines[A])
        self.assertIn(N.fmt_sol(N.sol_of(realized[B]), sign=True), lines[B])

    def test_sell_message_shows_the_trades_strategy(self):
        rows = {(f['mint'], f['side'], f['qty_after']): f for f in self.view._fills()}
        last_m1 = rows[(M1, 'sell', 0)]
        self.assertEqual(last_m1['strategy_version'], B)                    # the fill itself carries the new version...
        event = N.fill_event(self.view, last_m1, sol_usd_max_age=3600)
        self.assertEqual(event['strategy_version'], A)                       # ...the message is about the trade opened under A
        self.assertTrue(event['mixed_version'])
        self.assertEqual(N.fill_event(self.view, rows[(M2, 'sell', 0)], sol_usd_max_age=3600)['strategy_version'], B)
        self.assertFalse(N.fill_event(self.view, rows[(M2, 'sell', 0)], sol_usd_max_age=3600)['mixed_version'])



class LegacyStoreWithoutLifecycleRows(Case):
    def test_a_sell_without_a_lifecycle_row_keeps_its_own_version(self):
        q = paper.Quote(M1, 'buy', 80_000_000, 1_000_000_000, 6, self.tick())
        fill = paper.buy(q, '0.08', PCFG)
        self.store.add_fill(fill, strategy_version=A)                                  # no position_state at all (legacy rows)
        pos = self.store.positions()[M1]
        self.store.add_fill(paper.sell(pos, paper.Quote(M1, 'sell', pos.qty_raw, 90_000_000, 6, self.tick()), cfg=PCFG, qty_raw=pos.qty_raw),
                            strategy_version=B)
        self.store.close()
        view = N.StoreView(N.open_store(self.path))
        self.addCleanup(view.close)
        realized = view.realized_by_opening_version()
        self.assertEqual(list(realized), [B])                                           # nothing is dropped, nothing is invented
        self.assertEqual(realized[B], view._fills("WHERE side='sell'")[0]['realized_lamports'])


if __name__ == '__main__':
    unittest.main()
