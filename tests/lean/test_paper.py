"""SYNTHETIC_TEST_ONLY: lean paper fills and accounting. Known answers, refusals and exact integer identities. No network."""
import unittest
from decimal import Decimal

from lean import paper
from lean.paper import AccountingHalt, Fill, PaperConfig, PaperError, Position, Quote, apply_fill, buy, sell, to_lamports

MINT = 'M' * 32
CFG = PaperConfig()            # fee 0.00005 SOL, slippage 50 bps


def bq(sol_lamports=20_000_000, out=1_000_000_000, ts=100.0, mint=MINT, decimals=6):
    return Quote(mint, 'buy', sol_lamports, out, decimals, ts, ref='obs:1')


def sq(qty, out, ts=200.0, mint=MINT, decimals=6):
    return Quote(mint, 'sell', qty, out, decimals, ts, ref='obs:2')


class SizeTests(unittest.TestCase):
    def test_exact_lamports(self):
        for value, lamports in ((1, 10 ** 9), ('0.02', 20_000_000), (Decimal('0.000000001'), 1), (0.02, 20_000_000), ('1e-9', 1)):
            self.assertEqual(to_lamports(value), lamports, value)

    def test_refused_sizes(self):
        for bad in (0, -1, '0', '0.0000000001', 0.1 + 0.2, float('nan'), float('inf'), 'x', None, True, [], Decimal('NaN')):
            with self.subTest(bad=bad), self.assertRaises(PaperError):
                to_lamports(bad)


class BuyTests(unittest.TestCase):
    def test_known_answer(self):
        fill = buy(bq(), '0.02', CFG)
        # 1,000,000,000 raw out, 50 bps haircut -> 995,000,000; spend 0.02 SOL; fixed fee 50,000 lamports
        self.assertEqual((fill.side, fill.qty_raw, fill.sol_lamports, fill.fee_lamports, fill.slippage_bps),
                         ('buy', 995_000_000, 20_000_000, 50_000, 50))
        self.assertEqual((fill.label, fill.quote_ref, fill.cost_sold_lamports, fill.realized_lamports), ('EXECUTION_UNVERIFIED', 'obs:1', 0, 0))
        self.assertEqual(fill.price_sol_per_token, Decimal('0.02') / Decimal('995'))

    def test_rounds_down_never_in_the_traders_favour(self):
        fill = buy(bq(out=1999), '0.02', PaperConfig(slippage_bps=1))
        self.assertEqual(fill.qty_raw, 1999 * 9999 // 10000)

    def test_the_quote_must_match_the_request(self):
        with self.assertRaises(PaperError):
            buy(bq(sol_lamports=10_000_000), '0.02', CFG)             # quote for another size
        with self.assertRaises(PaperError):
            buy(sq(1000, 1000), '0.02', CFG)                          # a sell quote
        with self.assertRaises(PaperError):
            buy(bq(out=1), '0.02', PaperConfig(slippage_bps=9999))    # nothing left after slippage

    def test_bad_quotes_and_configs_are_refused(self):
        for kwargs in ({'in_amount': 0}, {'out_amount': -5}, {'decimals': 99}):
            with self.assertRaises(PaperError):
                Quote(**{'mint': MINT, 'side': 'buy', 'in_amount': 1, 'out_amount': 1, 'decimals': 6, 'ts': 1.0, **kwargs})
        with self.assertRaises(PaperError):
            Quote(MINT, 'hold', 1, 1, 6, 1.0)
        with self.assertRaises(PaperError):
            Quote('', 'buy', 1, 1, 6, 1.0)
        for bad in ({'fee_lamports': -1}, {'slippage_bps': 10_000}, {'slippage_bps': 1.5}, {'fee_lamports': True and 1.0}):
            with self.assertRaises(PaperError):
                PaperConfig(**bad)
        with self.assertRaises(PaperError):
            PaperConfig.from_dict({'fee_lamports': 1, 'bogus': 2})
        self.assertEqual(PaperConfig.from_dict({'slippage_bps': 25}).slippage_bps, 25)


class SellTests(unittest.TestCase):
    def position(self, qty=995_000_000, cost=20_050_000):
        return Position(MINT, qty, cost, 100.0, 0, 6)

    def test_full_sell_known_answer(self):
        pos = self.position()
        fill = sell(pos, sq(995_000_000, 40_000_000), 1, CFG)
        # proceeds 40,000,000 * 9950 / 10000 = 39,800,000; fee 50,000; basis 20,050,000
        self.assertEqual((fill.qty_raw, fill.sol_lamports, fill.fee_lamports, fill.cost_sold_lamports, fill.realized_lamports),
                         (995_000_000, 39_800_000, 50_000, 20_050_000, 39_800_000 - 50_000 - 20_050_000))

    def test_partial_sell_uses_average_cost_and_floors(self):
        pos = self.position(qty=1000, cost=10_001)
        fill = sell(pos, sq(300, 5_000_000), Decimal('0.3'), CFG)
        self.assertEqual((fill.qty_raw, fill.cost_sold_lamports), (300, 10_001 * 300 // 1000))
        self.assertEqual(sell(pos, sq(333, 5_000_000), 0.333, CFG).qty_raw, 333)       # int(1000 * 0.333)

    def test_refusals(self):
        pos = self.position()
        for fraction in (0, -0.1, 1.0000001, 'x', None, True, float('nan')):
            with self.subTest(fraction=fraction), self.assertRaises(PaperError):
                sell(pos, sq(1, 1), fraction, CFG)
        with self.assertRaises(PaperError):
            sell(pos, sq(1, 1), 0.0000000001, CFG)                      # sells nothing
        with self.assertRaises(PaperError):
            sell(pos, sq(994_999_999, 1), 1, CFG)                       # quote for another quantity
        with self.assertRaises(PaperError):
            sell(pos, sq(995_000_000, 1, mint='X' * 32), 1, CFG)        # another mint
        with self.assertRaises(PaperError):
            sell(pos, bq(), 1, CFG)                                     # a buy quote

    def test_a_loss_is_negative_realized(self):
        pos = self.position()
        fill = sell(pos, sq(995_000_000, 10_000_000), 1, CFG)
        self.assertLess(fill.realized_lamports, 0)


class ApplyFillTests(unittest.TestCase):
    def test_buy_then_partial_then_full_sell(self):
        cash = 5 * paper.LAMPORTS
        positions, cash = apply_fill({}, cash, buy(bq(), '0.02', CFG))
        self.assertEqual(cash, 5 * paper.LAMPORTS - 20_050_000)
        pos = positions[MINT]
        self.assertEqual((pos.qty_raw, pos.cost_lamports, pos.realized_lamports), (995_000_000, 20_050_000, 0))
        part = sell(pos, sq(500_000_000, 25_000_000), Decimal(500_000_000) / Decimal(995_000_000), CFG)
        positions, cash = apply_fill(positions, cash, part)
        left = positions[MINT]
        self.assertEqual(left.qty_raw, 995_000_000 - part.qty_raw)
        self.assertEqual(left.cost_lamports, 20_050_000 - part.cost_sold_lamports)
        self.assertEqual(left.realized_lamports, part.realized_lamports)
        final = sell(left, sq(left.qty_raw, 30_000_000), 1, CFG)
        positions, cash = apply_fill(positions, cash, final)
        self.assertEqual(positions, {})
        realized = part.realized_lamports + final.realized_lamports
        self.assertEqual(cash, 5 * paper.LAMPORTS + realized)            # nothing open: cash = initial + realized

    def test_buy_more_adds_to_the_same_position(self):
        positions, cash = apply_fill({}, 10 ** 10, buy(bq(), '0.02', CFG))
        positions, cash = apply_fill(positions, cash, buy(bq(ts=150.0), '0.02', CFG))
        self.assertEqual((positions[MINT].qty_raw, positions[MINT].cost_lamports, positions[MINT].opened_at),
                         (2 * 995_000_000, 2 * 20_050_000, 100.0))

    def test_every_violation_halts(self):
        cash = 10 ** 10
        held = {MINT: Position(MINT, 1000, 100, 1.0, 0, 6)}
        good = sell(held[MINT], sq(1000, 5_000_000), 1, CFG)
        cases = {
            'buy exceeds cash': lambda: apply_fill({}, 1_000_000, buy(bq(), '0.02', CFG)),
            'sell without a position': lambda: apply_fill({}, cash, good),
            'sell exceeds the position': lambda: apply_fill({MINT: Position(MINT, 999, 100, 1.0, 0, 6)}, cash, good),
            'sell pnl does not reconcile': lambda: apply_fill(held, cash, Fill(**{**good.__dict__, 'realized_lamports': good.realized_lamports + 1})),
            'basis wrong': lambda: apply_fill(held, cash, Fill(**{**good.__dict__, 'cost_sold_lamports': 1, 'realized_lamports': good.sol_lamports - good.fee_lamports - 1})),
            'buy cannot carry pnl': lambda: apply_fill({}, cash, Fill(**{**buy(bq(), '0.02', CFG).__dict__, 'realized_lamports': 1})),
            'decimals changed': lambda: apply_fill({MINT: Position(MINT, 5, 5, 1.0, 0, 9)}, cash, buy(bq(), '0.02', CFG)),
            'malformed': lambda: apply_fill({}, cash, Fill(**{**buy(bq(), '0.02', CFG).__dict__, 'qty_raw': 0})),
            'label': lambda: apply_fill({}, cash, Fill(**{**buy(bq(), '0.02', CFG).__dict__, 'label': 'LIVE'})),
            'negative fee': lambda: apply_fill({}, cash, Fill(**{**buy(bq(), '0.02', CFG).__dict__, 'fee_lamports': -1})),
        }
        for name, call in cases.items():
            with self.subTest(name), self.assertRaises(AccountingHalt):
                call()

    def test_spending_exactly_all_the_cash_is_allowed_and_one_lamport_more_is_not(self):
        fill = buy(bq(), '0.02', CFG)
        spend = fill.sol_lamports + fill.fee_lamports
        positions, cash = apply_fill({}, spend, fill)
        self.assertEqual((cash, positions[MINT].qty_raw), (0, fill.qty_raw))
        with self.assertRaises(AccountingHalt):
            apply_fill({}, spend - 1, fill)

    def test_a_sell_that_would_make_cash_negative_halts(self):
        pos = Position(MINT, 1000, 100, 1.0, 0, 6)
        fill = sell(pos, sq(1000, 10), 1, PaperConfig(fee_lamports=50_000))
        with self.assertRaises(AccountingHalt):
            apply_fill({MINT: pos}, 10, fill)

    def test_input_positions_are_not_mutated(self):
        original = {MINT: Position(MINT, 1000, 100, 1.0, 0, 6)}
        snapshot = dict(original)
        apply_fill(original, 10 ** 10, sell(original[MINT], sq(500, 5_000_000), 0.5, CFG))
        self.assertEqual(original, snapshot)


if __name__ == '__main__':
    unittest.main()
