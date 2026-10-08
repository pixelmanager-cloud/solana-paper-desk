"""Explicitly synthetic paired simulations: mechanism tests, not route approval."""
import copy
import unittest
import tempfile
from pathlib import Path
from decimal import Decimal as D

from desk.engine import initial_state, transition, sell
from desk.model import digest
from desk.ledger import Ledger
from tests.helpers import T, config, control, event


class PartialActionProofTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.state, fills = transition(initial_state(self.cfg), event(taker="fixture-wallet"), self.cfg)
        self.buy = fills[0]
        self.initial = copy.deepcopy(self.state['positions']['SYNTHETIC_A'])

    def paired(self, ts=T+5, reserve='160', fraction='.3', **changes):
        p = self.state['positions']['SYNTHETIC_A']
        qty = D(p['qty'])
        action_qty = min(qty, D(p['initial_qty']) * D(fraction))
        def proof(amount, tx, route, proceeds):
            return {'kind': 'sell_simulation', 'mint': 'SYNTHETIC_A', 'wallet': 'fixture-wallet',
                    'quantity_tokens': str(amount), 'observed_at': ts, 'slot': 123,
                    'transaction_hash': tx * 64, 'route_hash': route * 64,
                    'simulation_ok': True, 'transaction_policy_ok': True, 'wallet_account_ok': True,
                    'net_proceeds_sol': proceeds, 'provenance': 'SYNTHETIC_TEST_ONLY',
                    'source_hash': 'e' * 64, 'revision_hash': 'f' * 64}
        valuation = proof(qty, 'a', 'b', '10')
        action = proof(action_qty, 'c', 'd', '.01')
        action['valuation_proof_hash'] = digest(valuation)
        return event(ts, reserve_sol=reserve, taker='fixture-wallet', sellability=valuation,
                     action_sellability=action, exit_source_hash='e' * 64,
                     exit_revision_hash='f' * 64, **changes)

    def apply(self, e):
        self.state, output = transition(self.state, e, self.cfg)
        return output

    def assert_no_fill(self, e, expected):
        before = copy.deepcopy(self.state)
        output = self.apply(e)
        self.assertFalse(any(o['type'] == 'fill' for o in output))
        reasons = [r for o in output for r in o.get('reasons', [])]
        self.assertIn(expected, reasons)
        p = self.state['positions']['SYNTHETIC_A']
        old = before['positions']['SYNTHETIC_A']
        for key in ('qty', 'cost_left', 'trade_pnl', 'stage'):
            self.assertEqual(p[key], old[key])
        for key in ('cash', 'realized_pnl', 'day_gross_losses'):
            self.assertEqual(self.state[key], before[key])

    def test_distinct_paired_proofs_fill_exact_partial_and_use_action_net_proceeds(self):
        e = self.paired()
        self.assertNotEqual(e['sellability']['quantity_tokens'], e['action_sellability']['quantity_tokens'])
        self.assertNotEqual(e['sellability']['transaction_hash'], e['action_sellability']['transaction_hash'])
        fills = [o for o in self.apply(e) if o['type'] == 'fill']
        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertEqual(fill['reason'], 'TAKE_PROFIT')
        self.assertEqual(D(fill['quantity']), D(self.initial['qty']) * D('.3'))
        self.assertEqual(D(fill['proceeds_sol']), D('.01'))
        p = self.state['positions']['SYNTHETIC_A']
        self.assertEqual(p['stage'], 1)
        self.assertEqual(p['exit_blocked'], 'REMAINING_POSITION_VALUATION_REQUIRED')
        self.assertEqual(p['mark_status'], 'UNVERIFIED_EXIT')

    def test_missing_action_does_not_resize_full_proof(self):
        e = self.paired(); del e['action_sellability']
        self.assert_no_fill(e, 'EXIT_ACTION_PROOF_MISSING')

    def test_full_proof_substitution_for_action_still_fails_exact_size(self):
        e = self.paired(); e['action_sellability'] = copy.deepcopy(e['sellability'])
        e['action_sellability']['valuation_proof_hash'] = digest(e['sellability'])
        self.assert_no_fill(e, 'SELL_PROOF_SIZE_MISMATCH')

    def test_quantity_wallet_mint_and_source_substitutions_both_proofs(self):
        for selected in ('sellability', 'action_sellability'):
            for field, value, reason in (
                    ('quantity_tokens', '1', 'SELL_PROOF_SIZE_MISMATCH'),
                    ('wallet', 'other-wallet', 'SELL_IDENTITY_MISMATCH'),
                    ('mint', 'other-mint', 'SELL_IDENTITY_MISMATCH'),
                    ('source_hash', '0'*64, 'EXIT_SOURCE_HASH_MISMATCH'),
                    ('revision_hash', '0'*64, 'EXIT_REVISION_HASH_MISMATCH'),
                    ('provenance', 'helius', 'EXIT_PROOF_PROVENANCE_MISMATCH')):
                with self.subTest(selected=selected, field=field):
                    self.setUp(); e = self.paired(); e[selected][field] = value
                    self.assert_no_fill(e, reason)

    def test_one_raw_unit_size_change_is_rejected_and_ttl_boundary_is_valid(self):
        e = self.paired()
        e['action_sellability']['quantity_tokens'] = str(
            D(e['action_sellability']['quantity_tokens']) + D('.000001'))
        self.assert_no_fill(e, 'SELL_PROOF_SIZE_MISMATCH')
        self.setUp(); e = self.paired()
        e['sellability']['observed_at'] = T-5
        e['action_sellability']['observed_at'] = T-5
        e['action_sellability']['valuation_proof_hash'] = digest(e['sellability'])
        self.assertTrue(any(o['type'] == 'fill' for o in self.apply(e)))

    def test_event_source_revision_and_taker_substitution(self):
        for field, value, reason in (
                ('exit_source_hash', '0'*64, 'EXIT_SOURCE_HASH_MISMATCH'),
                ('exit_revision_hash', '0'*64, 'EXIT_REVISION_HASH_MISMATCH'),
                ('taker', 'other-wallet', 'SELL_IDENTITY_MISMATCH'),
                ('exit_source_hash', None, 'EXIT_SOURCE_HASH_MISMATCH'),
                ('exit_revision_hash', 'F'*64, 'EXIT_REVISION_HASH_MISMATCH')):
            with self.subTest(field=field):
                self.setUp(); e = self.paired(); e[field] = value
                self.assert_no_fill(e, reason)

    def test_position_wallet_cannot_be_substituted_even_with_matching_event_and_proofs(self):
        for wallet in (None, 'other-wallet'):
            with self.subTest(wallet=wallet):
                self.setUp()
                self.state['positions']['SYNTHETIC_A']['taker'] = wallet
                self.assert_no_fill(self.paired(), 'POSITION_WALLET_MISMATCH')

    def test_partial_cannot_reuse_valuation_transaction_hash(self):
        e = self.paired()
        e['action_sellability']['transaction_hash'] = e['sellability']['transaction_hash']
        self.assert_no_fill(e, 'EXIT_ACTION_TRANSACTION_REUSED')

    def test_direct_partial_sale_cannot_bypass_full_valuation_gate(self):
        e = self.paired(); e['sellability']['transaction_policy_ok'] = False
        e['action_sellability']['valuation_proof_hash'] = digest(e['sellability'])
        before = copy.deepcopy(self.state)
        output = []
        self.assertFalse(sell(self.state, e, self.cfg, D('.3'), 'TAKE_PROFIT', output))
        self.assertIn('SELL_TRANSACTION_POLICY_OK', output[-1]['reasons'])
        self.assertEqual(self.state['cash'], before['cash'])
        self.assertEqual(self.state['positions']['SYNTHETIC_A']['qty'],
                         before['positions']['SYNTHETIC_A']['qty'])

    def test_action_cannot_rebind_to_changed_valuation_record(self):
        e = self.paired(); e['sellability']['transaction_hash'] = '0'*64
        self.assert_no_fill(e, 'EXIT_VALUATION_PROOF_BINDING_MISMATCH')

    def test_stale_future_and_malformed_quantity_fail_closed(self):
        for selected in ('sellability', 'action_sellability'):
            for at in (T-6, T+6, True, None):
                with self.subTest(selected=selected, at=at):
                    self.setUp(); e = self.paired(); e[selected]['observed_at'] = at
                    self.assert_no_fill(e, 'SELL_PROOF_STALE')
            for amount in ('NaN', 'not-a-number', True):
                with self.subTest(selected=selected, amount=amount):
                    self.setUp(); e = self.paired(); e[selected]['quantity_tokens'] = amount
                    self.assert_no_fill(e, 'SELL_PROOF_MALFORMED')

    def test_existing_transaction_route_and_control_gates_still_block(self):
        for selected in ('sellability', 'action_sellability'):
            for field, value in (('transaction_policy_ok', False), ('wallet_account_ok', False),
                                 ('simulation_ok', False), ('route_hash', ''), ('transaction_hash', '')):
                with self.subTest(selected=selected, field=field):
                    self.setUp(); e = self.paired(); e[selected][field] = value
                    expected = 'SELL_' + field.upper() + ('_MISSING' if field.endswith('_hash') else '')
                    self.assert_no_fill(e, expected)

    def test_valuation_net_cap_can_prevent_take_profit(self):
        e = self.paired()
        cap = D(self.initial['cost_left']) * D('1.1')
        e['sellability']['net_proceeds_sol'] = str(cap)
        e['action_sellability']['valuation_proof_hash'] = digest(e['sellability'])
        self.assertFalse(any(o['type'] == 'fill' for o in self.apply(e)))
        self.assertEqual(D(self.state['positions']['SYNTHETIC_A']['mark_value']), cap)

    def test_whole_stop_time_and_manual_exits_use_full_proof_not_partial(self):
        for cause in ('STOP', 'TIME_STOP', 'MAX_HOLD', 'LIQUIDATE', 'DANGER', 'TRAILING_STOP'):
            with self.subTest(cause=cause):
                self.setUp()
                ts = T + (2700 if cause == 'TIME_STOP' else 21600 if cause == 'MAX_HOLD' else 5)
                e = self.paired(ts, reserve='70' if cause == 'STOP' else '100',
                                danger=cause == 'DANGER')
                # An unusable partial proof cannot authorize or prevent an
                # independently proven full exit, whose quantity exactly matches.
                e['action_sellability']['observed_at'] = 0
                if cause == 'LIQUIDATE': self.apply(control(T+1, 'LIQUIDATE'))
                if cause == 'TRAILING_STOP':
                    self.state['positions']['SYNTHETIC_A'].update(stage=3, peak_ratio='4')
                fills = [o for o in self.apply(e) if o['type'] == 'fill']
                self.assertEqual(len(fills), 1)
                self.assertEqual(fills[0]['reason'], cause)
                self.assertEqual(D(fills[0]['quantity']), D(self.initial['qty']))
                self.assertFalse(self.state['positions'])

    def test_three_rungs_then_full_exit_conserve_inventory_cash_basis_and_pnl(self):
        total_qty = D(0); proceeds = D(0); basis = D(self.initial['cost_left'])
        for dt, reserve, fraction in ((5, '160', '.3'), (10, '240', '.3'), (15, '350', '.2')):
            fill = next(o for o in self.apply(self.paired(T+dt, reserve, fraction)) if o['type'] == 'fill')
            total_qty += D(fill['quantity']); proceeds += D(fill['proceeds_sol'])
            p = self.state['positions']['SYNTHETIC_A']
            self.assertAlmostEqual(D(p['qty']) + total_qty, D(self.initial['qty']), places=24)
            self.assertAlmostEqual(D(p['cost_left']), basis * D(p['qty']) / D(self.initial['qty']), places=26)
        fill = next(o for o in self.apply(self.paired(T+20, '350', danger=True)) if o['type'] == 'fill')
        total_qty += D(fill['quantity']); proceeds += D(fill['proceeds_sol'])
        self.assertAlmostEqual(total_qty, D(self.initial['qty']), places=24)
        self.assertAlmostEqual(D(self.state['cash']), D(self.cfg['initial_equity_sol']) - basis + proceeds, places=26)
        self.assertAlmostEqual(D(self.state['realized_pnl']), proceeds - basis, places=26)
        self.assertFalse(self.state['positions'])

    def test_paired_partial_restart_redelivery_and_final_exit_preserve_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'paired.sqlite'
            ledger = Ledger(path)
            try:
                ledger.apply(event(taker='fixture-wallet'), self.cfg, transition, initial_state)
                partial = self.paired()
                expected = self.apply(partial)
                self.assertEqual(ledger.apply(partial, self.cfg, transition, initial_state), expected)
                committed = ledger.report()
                self.assertEqual(committed['state'], self.state)
                ledger.close(); ledger = Ledger(path, must_exist=True)
                self.assertEqual(ledger.report(), committed)
                self.assertEqual(ledger.apply(partial, self.cfg, transition, initial_state), [])
                collision = copy.deepcopy(partial)
                collision['action_sellability']['quantity_tokens'] = '1'
                with self.assertRaisesRegex(ValueError, 'event_id collision'):
                    ledger.apply(collision, self.cfg, transition, initial_state)
                self.assertEqual(ledger.report(), committed)
                final = self.paired(T+10, '160', danger=True)
                expected = self.apply(final)
                self.assertEqual(ledger.apply(final, self.cfg, transition, initial_state), expected)
                self.assertEqual(ledger.report()['state'], self.state)
                self.assertFalse(self.state['positions'])
            finally:
                ledger.close()

    def test_live_admission_guard_unchanged(self):
        state, output = transition(initial_state(self.cfg), event(provenance='helius'), self.cfg)
        self.assertFalse(state['positions'])
        self.assertIn('LIVE_FEATURE_ADAPTER_NOT_READY', output[-1]['reasons'])

    def test_legacy_synthetic_model_partial_behavior_remains_valid(self):
        fills = [o for o in self.apply(event(T+5, reserve_sol='160')) if o['type'] == 'fill']
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]['reason'], 'TAKE_PROFIT')
