"""lean.execution: the model in isolation (pure functions + the real Store for accounting). SYNTHETIC_TEST_ONLY, no network."""
import base64
import dataclasses
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from lean import adapters as A, execution as X, paper
from lean.store import Store

def model_pcfg(execution, fixed_fee_sol='0.00005', slippage='50'):
    """The paper config the runner derives: strategy fixed fee + priority fee + tip (A.paper_config on the effective strategy config)."""
    base = SimpleNamespace(fixed_fee_sol=Decimal(fixed_fee_sol), adverse_slippage_bps=Decimal(slippage))
    effective = SimpleNamespace(fixed_fee_sol=base.fixed_fee_sol + Decimal(execution.cfg.tx_extra_lamports) / paper.LAMPORTS,
                                adverse_slippage_bps=base.adverse_slippage_bps)
    return A.paper_config(effective)


MINT = 'MintMintMintMintMintMintMintMintMintMint11'
TOKEN_2022 = X.TOKEN_2022
LEGACY = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'


def mint_data(older_bps=None, newer_bps=None, extra=b''):
    base = bytearray(82)
    if older_bps is None and not extra:
        return bytes(base)
    tlv = b''
    if older_bps is not None:
        cfg = bytearray(108)
        cfg[88:90] = older_bps.to_bytes(2, 'little')
        cfg[106:108] = (older_bps if newer_bps is None else newer_bps).to_bytes(2, 'little')
        tlv += (1).to_bytes(2, 'little') + (108).to_bytes(2, 'little') + bytes(cfg)
    tlv += extra
    return bytes(base) + bytes(83) + b'\x01' + tlv


def account(data, owner=TOKEN_2022):
    return {'data': [base64.b64encode(data).decode(), 'base64'], 'owner': owner, 'executable': False, 'lamports': 1}


class ConfigTests(unittest.TestCase):
    def test_defaults_are_the_task_defaults_in_lamports(self):
        c = X.ExecConfig.from_dict({})
        self.assertEqual((c.exec_delay_s, c.priority_fee_lamports, c.jito_tip_lamports, c.ata_rent_lamports),
                         (3.0, 100_000, 100_000, 2_039_280))
        self.assertEqual(c.tx_extra_lamports, 200_000)
        self.assertFalse({'exit_requote_attempts', 'exit_requote_gap_s'} & set(X.KEYS))     # exits retry on the next pass instead

    def test_sol_values_become_exact_lamports_and_zero_switches_a_cost_off(self):
        c = X.ExecConfig.from_dict({'exec_delay_s': 5, 'priority_fee_sol': '0.0002', 'jito_tip_sol': 0, 'ata_rent_sol': 0.002})
        self.assertEqual((c.exec_delay_s, c.priority_fee_lamports, c.jito_tip_lamports, c.ata_rent_lamports), (5, 200_000, 0, 2_000_000))

    def test_bad_config_is_refused(self):
        bad = [{'exec_delay': 3}, {'priority_fee_sol': '-1'}, {'jito_tip_sol': '0.0000000001'}, {'ata_rent_sol': 'x'},
               {'exec_delay_s': -1}, {'exec_delay_s': 61}, {'exec_delay_s': True}, {'exec_delay_s': float('nan')},
               {'exit_requote_attempts': 5}, {'exit_requote_gap_s': 1}, [], 'x', {'priority_fee_sol': True}]
        for raw in bad:
            with self.assertRaises(X.ExecutionError, msg=raw):
                X.ExecConfig.from_dict(raw)

    def test_delay_must_fit_the_strategy_freshness_window(self):
        ttl10 = SimpleNamespace(price_ttl_seconds=10)
        X.Execution(X.ExecConfig(exec_delay_s=8)).validate_against(ttl10)
        with self.assertRaises(X.ExecutionError):
            X.Execution(X.ExecConfig(exec_delay_s=9)).validate_against(ttl10)

    def test_fee_enters_through_the_strategy_config(self):
        """One source of truth: the strategy's fixed fee grows by priority fee + tip, so the fill fee, the net marks and the
        entry/exit cost checks all see 0.00025 SOL per transaction."""
        from lean import strategy
        cfg = strategy.StrategyConfig.load(Path(__file__).resolve().parents[2] / 'config' / 'lean' / 'strategy-default.json')
        out = X.Execution(X.ExecConfig()).strategy_cfg(cfg)
        self.assertEqual((cfg.fixed_fee_sol, out.fixed_fee_sol), (Decimal('0.00005'), Decimal('0.00025')))
        self.assertEqual(out.config_hash, cfg.config_hash)
        self.assertEqual(A.paper_config(out).fee_lamports, 250_000)
        self.assertEqual(X.Execution(X.ExecConfig(priority_fee_lamports=0, jito_tip_lamports=0)).strategy_cfg(cfg).fixed_fee_sol, cfg.fixed_fee_sol)


class ExampleConfigTests(unittest.TestCase):
    def test_execution_example_loads_and_is_valid(self):
        from lean.__main__ import load_config
        root = Path(__file__).resolve().parents[2] / 'config' / 'lean'
        cfg = load_config(root / 'lean.execution.example.json')
        self.assertEqual(X.ExecConfig.from_dict(cfg['execution']), X.ExecConfig())
        self.assertIsNone(load_config(root / 'lean.example.json')['execution'])


class TransferFeeTests(unittest.TestCase):
    def test_legacy_and_plain_token_2022_have_no_fee(self):
        self.assertEqual(X.transfer_fee_bps(account(mint_data(), LEGACY)), 0)
        self.assertEqual(X.transfer_fee_bps(account(mint_data())), 0)

    def test_extension_other_than_transfer_fee_is_zero(self):
        other = (18).to_bytes(2, 'little') + (64).to_bytes(2, 'little') + bytes(64)
        self.assertEqual(X.transfer_fee_bps(account(mint_data(extra=other))), 0)

    def test_larger_of_older_and_newer_is_taken(self):
        self.assertEqual(X.transfer_fee_bps(account(mint_data(100, 250))), 250)
        self.assertEqual(X.transfer_fee_bps(account(mint_data(400, 25))), 400)

    def test_unreadable_evidence_raises(self):
        good = mint_data(100)
        cases = [account(good[:150]), account(good[:-5]), account(b'\x00' * 100), account(mint_data(10001)),
                 {'owner': TOKEN_2022, 'data': ['!!!', 'base64']}, {'owner': TOKEN_2022, 'data': 'x'}, None, 'x', {'owner': TOKEN_2022}]
        bad_len = bytearray(good)
        bad_len[166 + 2:166 + 4] = (50).to_bytes(2, 'little')
        cases.append(account(bytes(bad_len)))
        for case in cases:
            with self.assertRaises(X.ExecutionError, msg=str(case)[:60]):
                X.transfer_fee_bps(case)

    def test_read_from_the_raw_screen_response(self):
        import json
        raw = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'context': {'slot': 1}, 'value': [account(mint_data(120)), None]}}).encode()
        self.assertEqual(X.transfer_fee_bps_from_screen([('other', b'x'), ('accounts_mint_pool', raw)]), 120)
        for items in ([], [('accounts_mint_pool', b'{')], [('accounts_mint_pool', b'{"result":{"value":[]}}')]):
            with self.assertRaises(X.ExecutionError):
                X.transfer_fee_bps_from_screen(items)


class TaxTests(unittest.TestCase):
    def test_tax_is_on_the_output_of_the_same_input(self):
        self.assertEqual(X.tax_bps(1000, 990), '100.00')
        self.assertEqual(X.tax_bps(1000, 1000), '0.00')
        self.assertEqual(X.tax_bps(1000, 1010), '-100.00')            # a better exit fill is a negative tax
        self.assertEqual(X.tax_bps(3, 2), '3333.33')


def runner_stub(positions=None, base=None):
    pcfg = model_pcfg(X.Execution(X.ExecConfig()))
    return SimpleNamespace(store=SimpleNamespace(positions=lambda: dict(positions or {})), pcfg=pcfg)


class FillModelTests(unittest.TestCase):
    def setUp(self):
        self.x = X.Execution(X.ExecConfig())
        self.pcfg = model_pcfg(self.x)
        self.buy_q = paper.Quote(MINT, 'buy', 80_000_000, 1_000_000_000, 6, 1.0)
        self.meta = {'delay_s': 3.0, 'q0_ref': '1', 'q0_out': 1_000_000_000, 'q1_ref': '2', 'q1_out': 970_000_000,
                     'fill': 'q1', 'fill_out': 970_000_000, 'fill_ref': '2', 'transfer_fee_bps': 0}

    def buy(self, fee_bps=0, positions=None):
        q = dataclasses.replace(self.buy_q, out_amount=970_000_000)
        fill = paper.buy(q, '0.08', self.pcfg)
        return self.x.finish_buy(runner_stub(positions), fill, {**self.meta, 'transfer_fee_bps': fee_bps}), fill

    def test_first_buy_pays_ata_rent_in_the_fee_and_keeps_the_rest(self):
        (fill, extra), plain = self.buy()
        self.assertEqual(fill.fee_lamports, plain.fee_lamports + 2_039_280)
        self.assertEqual((fill.qty_raw, fill.sol_lamports), (plain.qty_raw, plain.sol_lamports))
        e = extra['execution']
        self.assertEqual((e['base_fee_lamports'], e['priority_fee_lamports'], e['jito_tip_lamports'], e['ata_rent_paid_lamports']),
                         (50_000, 100_000, 100_000, 2_039_280))
        self.assertEqual((extra['ata_rent_lamports'], extra['transfer_fee_bps']), (2_039_280, 0))
        self.assertEqual(e['latency_tax_bps'], '300.00')

    def test_an_add_to_an_existing_position_pays_no_second_rent(self):
        (fill, extra), plain = self.buy(positions={MINT: object()})
        self.assertEqual(fill.fee_lamports, plain.fee_lamports)
        self.assertEqual(extra['ata_rent_lamports'], 0)

    def test_transfer_fee_reduces_the_tokens_received(self):
        (fill, extra), plain = self.buy(fee_bps=200)
        self.assertEqual(fill.qty_raw, plain.qty_raw * 9800 // 10000)
        self.assertEqual(extra['execution']['transfer_fee_lost_tokens_raw'], plain.qty_raw - fill.qty_raw)
        self.assertEqual(extra['transfer_fee_bps'], 200)

    def test_nothing_left_after_the_transfer_fee_is_refused(self):
        fill = dataclasses.replace(paper.buy(self.buy_q, '0.08', self.pcfg), qty_raw=1)
        with self.assertRaises(X.ExecutionError):
            self.x.finish_buy(runner_stub(), fill, {**self.meta, 'transfer_fee_bps': 9999})

    def position(self, qty=1000, cost=90_000_000):
        return paper.Position(MINT, qty, cost, 1.0, 0, 6)

    def sell_fill(self, qty, out=90_000_000):
        pos = self.position()
        q = paper.Quote(MINT, 'sell', qty, out, 6, 5.0)
        return pos, paper.sell(pos, q, cfg=self.pcfg, qty_raw=qty)

    def smeta(self, out=90_000_000):
        return {'delay_s': 3.0, 'q0_ref': '5', 'q0_out': 91_000_000, 'q1_ref': '6', 'q1_out': out, 'fill': 'q1', 'fill_out': out, 'fill_ref': '6'}

    def test_full_close_refunds_the_rent_and_reconciles(self):
        pos, fill = self.sell_fill(1000)
        out, extra = self.x.finish_sell(fill, pos, {'ata_rent_lamports': 2_039_280, 'transfer_fee_bps': 0}, self.smeta())
        self.assertEqual(out.sol_lamports, fill.sol_lamports + 2_039_280)
        self.assertEqual(out.realized_lamports, out.sol_lamports - out.fee_lamports - out.cost_sold_lamports)
        e = extra['execution']
        self.assertEqual((e['ata_refund_lamports'], e['base_fee_lamports'], e['side']), (2_039_280, 50_000, 'sell'))
        self.assertEqual(e['latency_tax_bps'], X.tax_bps(91_000_000, 90_000_000))
        paper.apply_fill({MINT: pos}, 10 ** 10, out)                       # the accounting accepts it

    def test_partial_sell_refunds_nothing(self):
        pos, fill = self.sell_fill(400)
        out, extra = self.x.finish_sell(fill, pos, {'ata_rent_lamports': 2_039_280, 'transfer_fee_bps': 0}, self.smeta())
        self.assertEqual(out.sol_lamports, fill.sol_lamports)
        self.assertEqual(extra['execution']['ata_refund_lamports'], 0)

    def test_sell_transfer_fee_haircuts_the_proceeds(self):
        pos, fill = self.sell_fill(1000)
        out, extra = self.x.finish_sell(fill, pos, {'transfer_fee_bps': 500}, self.smeta())
        self.assertEqual(out.sol_lamports, fill.sol_lamports * 9500 // 10000)
        self.assertEqual(extra['execution']['transfer_fee_lost_lamports'], fill.sol_lamports - out.sol_lamports)

    def test_old_position_state_without_the_keys_sells_exactly_as_before(self):
        pos, fill = self.sell_fill(1000)
        out, _ = self.x.finish_sell(fill, pos, {}, self.smeta())
        self.assertEqual(out, fill)

    def test_without_execution_metadata_nothing_changes(self):
        pos, fill = self.sell_fill(1000)
        self.assertEqual(self.x.finish_sell(fill, pos, {}, None), (fill, {}))
        self.assertEqual(self.x.finish_buy(runner_stub(), fill, None), (fill, {}))


class EconomicTests(unittest.TestCase):
    """Rent is real cash while it is out, but prices, fees and per-sell returns shown to people must not contain it."""

    def test_economic_buy_and_sell_rows(self):
        buy = {'side': 'buy', 'sol_lamports': 80_000_000, 'fee_lamports': 250_000 + 2_039_280, 'realized_lamports': 0, 'cost_sold_lamports': 0}
        e = X.economic(buy, {'ata_rent_paid_lamports': 2_039_280})
        self.assertEqual((e['fee_lamports'], e['sol_lamports']), (250_000, 80_000_000))
        sell = {'side': 'sell', 'sol_lamports': 79_000_000 + 2_039_280, 'fee_lamports': 250_000,
                'cost_sold_lamports': 80_000_000 + 250_000 + 2_039_280, 'realized_lamports': 79_000_000 + 2_039_280 - 250_000 - 82_289_280}
        e = X.economic(sell, {'ata_refund_lamports': 2_039_280, 'ata_rent_basis_sold_lamports': 2_039_280})
        self.assertEqual(e['sol_lamports'], 79_000_000)
        self.assertEqual(e['cost_sold_lamports'], 80_250_000)
        self.assertEqual(e['realized_lamports'], 79_000_000 - 250_000 - 80_250_000)          # the same loss, rent netted out
        self.assertEqual(e['realized_lamports'], sell['realized_lamports'])
        self.assertIsNot(e, sell)

    def test_no_payload_and_write_off_rows_are_unchanged(self):
        row = {'side': 'sell', 'sol_lamports': 0, 'fee_lamports': 0, 'cost_sold_lamports': 82_000_000, 'realized_lamports': -82_000_000}
        self.assertEqual(X.economic(row, None), row)
        self.assertEqual(X.economic(row, {'kind': 'write_off', 'ata_rent_written_off_lamports': 2_039_280}), row)

    def test_partial_sells_telescope_to_the_full_rent_and_the_economic_realized_matches_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(os.path.realpath(tmp), 'lean.sqlite'), initial_cash_sol='10', code_version='t', strategy_version='s')
            x = X.Execution(X.ExecConfig())
            pcfg = model_pcfg(x)
            q = paper.Quote(MINT, 'buy', 80_000_000, 1_000_003, 6, 1.0)
            meta = {'delay_s': 3.0, 'q0_ref': '1', 'q0_out': 1_000_003, 'q1_ref': '1', 'q1_out': 1_000_003, 'fill': 'q0',
                    'fill_out': 1_000_003, 'fill_ref': '1', 'transfer_fee_bps': 0}
            buy, extra = x.finish_buy(SimpleNamespace(store=store, pcfg=pcfg), paper.buy(q, '0.08', pcfg), meta)
            store.add_fill(buy, state={'event': 'open', 'state': {'pool': 'p', 'initial_qty_raw': buy.qty_raw, **extra}})
            state = {'initial_qty_raw': buy.qty_raw, **extra}
            open_id, econ_realized, basis, refunds = 1, 0, 0, 0
            for fraction in (3, 3, None):                                    # 30%, 30%, then the rest
                pos = store.positions()[MINT]
                qty = pos.qty_raw if fraction is None else buy.qty_raw * fraction // 10
                sq = paper.Quote(MINT, 'sell', qty, 30_000_000, 6, 2.0)
                sell, sextra = x.finish_sell(paper.sell(pos, sq, cfg=pcfg, qty_raw=qty), pos, state,
                                             {**meta, 'q0_out': 30_000_000, 'q1_out': 30_000_000, 'fill_out': 30_000_000})
                event = 'closed' if qty == pos.qty_raw else 'rung'
                store.add_fill(sell, state={'event': event, 'open_fill_id': open_id, 'state': {'pool': 'p', **state, **sextra}})
                p = sextra['execution']
                row = dict(side='sell', sol_lamports=sell.sol_lamports, fee_lamports=sell.fee_lamports,
                           cost_sold_lamports=sell.cost_sold_lamports, realized_lamports=sell.realized_lamports)
                econ_realized += X.economic(row, p)['realized_lamports']
                basis += p['ata_rent_basis_sold_lamports']
                refunds += p['ata_refund_lamports']
            self.assertTrue(store.check_invariants())
            self.assertEqual((basis, refunds), (2_039_280, 2_039_280))            # nothing lost to the integer splits
            self.assertEqual(econ_realized, store.realized())                      # rent nets out of the trade as a whole
            store.close()


class StoreAccountingTests(unittest.TestCase):
    """A real Store replays every fill through paper.apply_fill: rent paid and refunded must net to zero."""

    def test_rent_nets_to_zero_and_cash_reconciles(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(os.path.realpath(tmp), 'lean.sqlite'), initial_cash_sol='10', code_version='t', strategy_version='s')
            x = X.Execution(X.ExecConfig())
            pcfg = model_pcfg(x)
            q = paper.Quote(MINT, 'buy', 80_000_000, 1_000_000_000, 6, 1.0)
            meta = {'delay_s': 3.0, 'q0_ref': '1', 'q0_out': 1_000_000_000, 'q1_ref': '1', 'q1_out': 1_000_000_000, 'fill': 'q0',
                    'fill_out': 1_000_000_000, 'fill_ref': '1', 'transfer_fee_bps': 0}
            buy, extra = x.finish_buy(SimpleNamespace(store=store, pcfg=pcfg), paper.buy(q, '0.08', pcfg), meta)
            store.add_fill(buy, state={'event': 'open', 'state': {'pool': 'p', **extra}})
            pos = store.positions()[MINT]
            sq = paper.Quote(MINT, 'sell', pos.qty_raw, 80_000_000, 6, 2.0)
            sell, sextra = x.finish_sell(paper.sell(pos, sq, cfg=pcfg, qty_raw=pos.qty_raw), pos, extra, {**meta, 'q0_out': 80_000_000, 'q1_out': 80_000_000, 'fill_out': 80_000_000})
            store.add_fill(sell, state={'event': 'closed', 'open_fill_id': 1, 'state': {'pool': 'p', **sextra}})
            self.assertTrue(store.check_invariants())
            self.assertEqual(store.positions(), {})
            # spent 0.08 + fees 0.00025 + rent 0.00203928; got 0.0796 (50 bps slippage) - fee 0.00025 + the rent back
            self.assertEqual(store.cash(), store.initial_cash - (80_000_000 + 250_000 + 2_039_280) + (79_600_000 + 2_039_280 - 250_000))
            self.assertEqual(store.initial_cash - store.cash(), 400_000 + 2 * 250_000)       # slippage + two transactions' fees, no rent
            self.assertEqual(store.realized(), store.cash() - store.initial_cash)
            store.close()


class AdapterTests(unittest.TestCase):
    def test_rent_is_not_part_of_the_strategys_cost_basis(self):
        pos = paper.Position(MINT, 1000, 80_000_000 + 250_000 + 2_039_280, 1.0, 0, 6)
        state = {'initial_qty_raw': 1000, 'stop_ratio': '0.82', 'stage': 0, 'peak_ratio': '1', 'touched_15': False}
        plain = A.strategy_position(pos, state)
        with_rent = A.strategy_position(pos, {**state, 'ata_rent_lamports': 2_039_280})
        self.assertEqual(plain.cost_left, A.sol(pos.cost_lamports))
        self.assertEqual(with_rent.cost_left, A.sol(pos.cost_lamports - 2_039_280))

    def test_rent_left_shrinks_with_the_remaining_quantity(self):
        pos = paper.Position(MINT, 500, 40_000_000 + 1_019_640, 1.0, 0, 6)
        state = {'initial_qty_raw': 1000, 'stop_ratio': '0.82', 'stage': 1, 'peak_ratio': '1', 'touched_15': False, 'ata_rent_lamports': 2_039_280}
        self.assertEqual(A.strategy_position(pos, state).cost_left, A.sol(40_000_000))


if __name__ == '__main__':
    unittest.main()
