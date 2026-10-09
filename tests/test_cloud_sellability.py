"""Issue #5 audit reproductions: offline captures and explicitly synthetic attacks.

Passing gap-characterization tests preserve evidence of incomplete policy; they do
not endorse effects_passed or caller-supplied sellability flags as authorization.
"""
import base64
import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from solders.hash import Hash
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from desk.compile import compile_unsigned
from desk.effects import check_effects
from desk.engine import position_sellability
from desk.envelope import check_sell_envelope
from desk.instructions import JUPITER, SYSTEM, inventory
from desk.model import D
from desk.roundtrip import simulate_roundtrip
from desk.router import check_sell_route
from desk.security import TOKEN_2022, TOKEN_PROGRAM, base58, mint_policy, sellability_gate
from desk.sequence import simulate_sequence
from tests.helpers import T, event

ROOT = Path(__file__).resolve().parents[1]


def capture(name):
    return json.loads((ROOT / 'fixtures' / name).read_text())


def instruction(program, raw, accounts=()):
    return {'programId': program, 'data': base64.b64encode(raw).decode(),
            'accounts': [{'pubkey': key, 'isSigner': signer, 'isWritable': True}
                         for key, signer in accounts]}


class CloudRoundtripAudit(unittest.TestCase):
    """Real effect/control checkers; compiler and sequence transport are stubbed.

    This harness does not validate compiled transaction bytes or execute altered
    instructions. It tests the orchestration's handling of supplied evidence.
    """
    def setUp(self):
        self.saved = capture('mainnet-roundtrip-simulation.json')
        self.result = self.saved['result']
        self.sequence = copy.deepcopy(self.result['sequence'])
        self.rows = self.sequence['result']['value']['transactionResults']
        self.quotes = copy.deepcopy(self.saved['quote_responses'])
        self.legs = copy.deepcopy(self.saved['legs'])
        self.calls = []

    def rpc(self, method, params):
        self.calls.append(method)
        if method == 'getLatestBlockhash':
            return {'value': {'blockhash': str(Hash.default())}}
        if method == 'getMultipleAccounts':
            watch = self.sequence['watch']
            return {'context': {'slot': self.result['slot'] - 1},
                    'value': [self.rows[0]['preExecutionAccounts'][watch.index(k)]
                              for k in params[0]]}
        self.fail(f'Forbidden/unexpected RPC method: {method}')

    def run_diagnostic(self):
        quotes = iter(self.quotes)
        legs = [{**leg, 'raw': b'audit-transport-stub'} for leg in self.legs]
        # Bind only these explicit synthetic wire stand-ins, not the original
        # capture's unavailable bytes. Instruction-policy gaps remain asserted.
        sequence = {**self.sequence, 'transaction_hashes':
                    [hashlib.sha256(leg['raw']).hexdigest() for leg in legs]}
        with patch('desk.roundtrip.time.time', return_value=T), \
                patch('desk.roundtrip.time.monotonic', return_value=0), \
                patch('desk.roundtrip.compile_unsigned', side_effect=legs), \
                patch('desk.roundtrip.simulate_sequence', return_value=sequence):
            return simulate_roundtrip(
                self.result['mint'], self.result['wallet'],
                int(self.result['spend_lamports']), rpc=self.rpc,
                quote=lambda *args: {'observed_at': T, 'response': next(quotes)})

    def assert_unapproved(self, result):
        for key in ('signed', 'submitted', 'eligible_for_trading', 'transaction_policy_ok'):
            self.assertIs(result[key], False, key)

    def test_buy_minimum_exit_leaves_exact_unsold_surplus(self):
        result = self.run_diagnostic()
        self.assertTrue(result['effects_passed'])
        self.assertEqual(result['sell_quantity_raw'], '2523452')
        self.assertEqual(result['residual_token_raw'], '25489')
        self.assertEqual(result['net_native_wealth_delta_lamports'], '-143010')
        self.assert_unapproved(result)
        self.assertEqual(self.calls, ['getMultipleAccounts', 'getLatestBlockhash'])

    def test_malicious_outer_approval_is_not_checked_by_roundtrip(self):
        leg = self.legs[1]
        holding = next(leg['keys'][r['accountIndex']]
                       for r in self.rows[1]['preTokenBalances']
                       if r['owner'] == self.result['wallet'] and r['mint'] == self.result['mint'])
        leg['outer'].append(instruction(TOKEN_PROGRAM, b'\x04' + bytes(8),
                                       [(holding, False), (self.result['mint'], False),
                                        (self.result['wallet'], True)]))
        inspected = inventory(leg['outer'], self.rows[1], leg['keys'], self.result['wallet'])
        self.assertIn('TOKEN_APPROVE_NOT_ALLOWED', inspected['reasons'])
        result = self.run_diagnostic()
        self.assertTrue(result['effects_passed'])  # Gap: instruction policy is never called.
        self.assert_unapproved(result)

    def test_missing_inner_inventory_does_not_change_roundtrip_effects(self):
        for row in self.rows:
            row.pop('innerInstructions', None)
        result = self.run_diagnostic()
        self.assertTrue(result['effects_passed'])
        self.assert_unapproved(result)

    def test_quote_exit_size_must_equal_conservative_buy_minimum(self):
        self.quotes[1]['inAmount'] = str(int(self.quotes[0]['otherAmountThreshold']) + 1)
        with self.assertRaisesRegex(ValueError, 'Sell route identity mismatch'):
            self.run_diagnostic()
        self.assertEqual(self.calls, ['getMultipleAccounts'])

    def test_each_leg_rejects_output_below_required_minimum(self):
        # One raw unit above the witnessed receipt/proceeds must fail separately.
        baseline = self.run_diagnostic()
        for leg, field, reason in (
                (0, 'received_raw', 'BOUGHT_TOKENS_BELOW_ROUTE_MINIMUM'),
                (1, 'native_wealth_delta_lamports', 'PROCEEDS_BELOW_ROUTE_MINIMUM')):
            with self.subTest(leg=leg):
                keys = self.legs[leg]['keys']
                row = self.rows[leg]
                minimum = int(baseline['leg_effects'][leg][field]) + 1
                if leg == 1:
                    minimum += int(baseline['leg_effects'][leg]['fee_lamports'])
                checked = check_effects(row, keys, self.result['wallet'], self.result['mint'],
                                        int(self.result['spend_lamports']) if leg == 0 else 2523452,
                                        minimum, direction='buy' if leg == 0 else 'sell')
                self.assertIn(reason, checked['reasons'])

    def test_one_unit_input_mismatch_rejected_in_both_directions(self):
        for leg, amount, reason in (
                (0, int(self.result['spend_lamports']) + 1, 'EXACT_NATIVE_INPUT_DEBIT_MISMATCH'),
                (1, 2523453, 'EXACT_INPUT_DEBIT_MISMATCH')):
            with self.subTest(leg=leg):
                checked = check_effects(self.rows[leg], self.legs[leg]['keys'],
                                        self.result['wallet'], self.result['mint'], amount, 1,
                                        direction='buy' if leg == 0 else 'sell')
                self.assertIn(reason, checked['reasons'])

    def test_post_sell_delegate_rejected_even_without_changed_net_amount(self):
        wallet = self.result['wallet']
        owned = next(r for r in self.rows[1]['postTokenBalances']
                     if r['owner'] == wallet and r['mint'] == self.result['mint'])
        key = self.legs[1]['keys'][owned['accountIndex']]
        account = self.rows[1]['postExecutionAccounts'][self.sequence['watch'].index(key)]
        raw = bytearray(base64.b64decode(account['data'][0]))
        raw[72:76] = (1).to_bytes(4, 'little')
        raw[76:108] = bytes([9]) * 32
        account['data'][0] = base64.b64encode(raw).decode()
        result = self.run_diagnostic()
        self.assertIn('TOKEN_ACCOUNT_DELEGATE', result['reasons'])
        self.assertFalse(result['effects_passed'])
        self.assert_unapproved(result)

    def test_unsupported_mint_never_reaches_quote_or_compilation(self):
        at = self.sequence['watch'].index(self.result['mint'])
        self.rows[0]['preExecutionAccounts'][at]['owner'] = TOKEN_2022
        with self.assertRaisesRegex(ValueError, 'Mint policy excludes roundtrip'):
            self.run_diagnostic()
        self.assertEqual(self.calls, ['getMultipleAccounts'])


class CloudUnsignedPolicyAudit(unittest.TestCase):
    def setUp(self):
        self.saved = capture('mainnet-sell-simulation.json')
        self.wallet = self.saved['wallet']
        self.other = base58(bytes([9]) * 32)

    def no_rpc(self, *args):
        self.fail('Network is forbidden in this audit')

    def test_compiler_serializes_extra_native_transfer_but_envelope_rejects_it(self):
        transfer = instruction(SYSTEM, (2).to_bytes(4, 'little') + (1).to_bytes(8, 'little'),
                               [(self.wallet, True), (self.other, False)])
        response = {'swapInstruction': transfer, 'otherInstructions': [copy.deepcopy(transfer)]}
        compiled = compile_unsigned(response, self.wallet, str(Hash.default()), self.no_rpc)
        tx = VersionedTransaction.from_bytes(compiled['raw'])
        self.assertEqual(tx.signatures, [Signature.default()])
        self.assertEqual(len(tx.message.instructions), 2)
        self.assertIn('ENVELOPE_UNAPPROVED_OUTER_PROGRAM',
                      check_sell_envelope(compiled['outer'], self.wallet)['reasons'])

    def test_injected_cleanup_refund_recipient_rejected(self):
        outer = copy.deepcopy(self.saved['outer'])
        outer[-1]['accounts'][1]['pubkey'] = self.other
        self.assertIn('ENVELOPE_UNAPPROVED_TOKEN_OPERATION_OR_RECIPIENT',
                      check_sell_envelope(outer, self.wallet)['reasons'])

    def test_zero_net_approval_and_revoke_cpis_are_still_forbidden(self):
        outer = [instruction(JUPITER, bytes(8), [(self.wallet, True)])]
        keys = [self.wallet, JUPITER, TOKEN_PROGRAM]
        inner = [{'index': 0, 'instructions': [
            {'programId': TOKEN_PROGRAM, 'stackHeight': 2,
             'parsed': {'type': kind, 'info': {}}} for kind in ('approve', 'revoke')]}]
        checked = inventory(outer, {'innerInstructions': inner}, keys, self.wallet)
        self.assertIn('TOKEN_APPROVE_NOT_ALLOWED', checked['reasons'])
        self.assertIn('TOKEN_REVOKE_NOT_ALLOWED', checked['reasons'])
        self.assertFalse(checked['full_route_policy_passed'])

    def test_public_multihop_route_cannot_be_promoted_by_passing_balance_checks(self):
        saved = self.saved
        router = next(ix for ix in saved['outer'] if ix['programId'] == JUPITER)
        holding = router['accounts'][2]['pubkey']
        effects = check_effects(saved['simulation'], saved['keys'], self.wallet,
                                saved['mint'], int(saved['amount_raw']), 1)
        self.assertTrue(effects['passed'])
        route = check_sell_route(router, saved['mint'], self.wallet, holding,
                                 int(saved['amount_raw']), 1)
        self.assertIn('ROUTE_OUTSIDE_DIRECT_PUMPSWAP_SELL_PROFILE', route['reasons'])
        self.assertFalse(route['full_route_policy_passed'])

    def test_amm_external_recipient_and_output_fee_alias_are_rejected(self):
        from desk.providers import SOL
        from desk.recipients import check_sell_recipients
        fields = ('user', 'pool', 'base_mint', 'user_base_token_account',
                  'pool_base_token_account', 'pool_quote_token_account',
                  'user_quote_token_account', 'protocol_fee_recipient_token_account',
                  'coin_creator_vault_ata')
        names = {name: base58(bytes([i + 1]) * 32) for i, name in enumerate(fields)}
        bindings = {'passed': True, 'instruction': '2.0', 'account_bindings': names}

        def transfer(source, mint, destination, authority, amount):
            return {'program': TOKEN_PROGRAM, 'instruction': '2.1',
                    'parent_instruction': '2.0', 'stack_height': 3,
                    'accounts': [names[source], mint, names[destination], names[authority]],
                    'data_base64': base64.b64encode(
                        b'\x0c' + amount.to_bytes(8, 'little') + b'\x06').decode()}

        rows = [transfer('user_base_token_account', names['base_mint'],
                         'pool_base_token_account', 'user', 100),
                transfer('pool_quote_token_account', SOL,
                         'user_quote_token_account', 'pool', 90)]
        evidence = {'stack_metadata_verified': True, 'instructions': rows}
        self.assertTrue(check_sell_recipients(evidence, bindings, 100, 90)['passed'])
        rows[1]['accounts'][2] = base58(bytes([99]) * 32)
        self.assertIn('AMM_TOKEN_RECIPIENT_OR_SOURCE_UNAPPROVED',
                      check_sell_recipients(evidence, bindings, 100, 90)['reasons'])
        rows[1]['accounts'][2] = names['user_quote_token_account']
        names['protocol_fee_recipient_token_account'] = names['user_quote_token_account']
        self.assertIn('AMM_RECIPIENT_ROLES_OVERLAP',
                      check_sell_recipients(evidence, bindings, 100, 90)['reasons'])

    def test_router_unknown_suffix_is_not_ignored(self):
        saved = self.saved
        router = next(ix for ix in saved['outer'] if ix['programId'] == JUPITER)
        router['data'] = base64.b64encode(base64.b64decode(router['data']) + b'unknown').decode()
        checked = check_sell_route(router, saved['mint'], self.wallet,
                                   router['accounts'][2]['pubkey'], int(saved['amount_raw']), 1)
        self.assertIn('ROUTE_TRAILING_BYTES', checked['reasons'])

    def test_unknown_mint_layout_and_token2022_fail_closed(self):
        clean = event()['token_evidence']['account']
        unknown = copy.deepcopy(clean)
        unknown['data'][0] = base64.b64encode(base64.b64decode(clean['data'][0]) + b'\x01').decode()
        token2022 = copy.deepcopy(clean)
        token2022['owner'] = TOKEN_2022
        for account in (unknown, token2022):
            with self.subTest(owner=account['owner']):
                self.assertEqual(mint_policy(account)['decision'], 'SKIP')

    def test_nonzero_signature_bytes_rejected_before_transport(self):
        transfer = instruction(SYSTEM, (2).to_bytes(4, 'little') + bytes(8),
                               [(self.wallet, True), (self.other, False)])
        compiled = compile_unsigned({'swapInstruction': transfer}, self.wallet,
                                    str(Hash.default()), self.no_rpc)
        tx = VersionedTransaction.from_bytes(compiled['raw'])
        # Fabricated bytes, no signer/keypair used.
        forged = bytes(VersionedTransaction.populate(tx.message,
                                                    [Signature.from_bytes(bytes([1]) * 64)]))
        with self.assertRaisesRegex(ValueError, 'null signatures only'):
            simulate_sequence([compiled['raw'], forged], [self.wallet], self.no_rpc)


class CloudExitEvidenceAudit(unittest.TestCase):
    def proof(self, quantity='100'):
        # Synthetic caller assertion: explicitly NOT a trusted simulation artifact.
        return {'kind': 'sell_simulation', 'mint': 'SYNTHETIC_A', 'wallet': 'wallet',
                'observed_at': T, 'quantity_tokens': quantity, 'simulation_ok': True,
                'transaction_policy_ok': True, 'wallet_account_ok': True,
                'net_proceeds_sol': '.01', 'slot': 100,
                'transaction_hash': 'a' * 64, 'route_hash': 'b' * 64}

    def test_roundtrip_diagnostic_cannot_be_used_as_exact_exit_proof(self):
        saved = capture('mainnet-roundtrip-simulation.json')['result']
        reasons = sellability_gate(event(sellability=saved), D(100))
        self.assertEqual(reasons, ['SELL_SIMULATION_REQUIRED'])

    def test_smaller_larger_and_partial_exit_sizes_require_distinct_evidence(self):
        for proof_size, requested in (('99', '100'), ('101', '100'), ('100', '30')):
            with self.subTest(proof_size=proof_size, requested=requested):
                self.assertIn('SELL_PROOF_SIZE_MISMATCH', sellability_gate(
                    event(sellability=self.proof(proof_size), taker='wallet'), D(requested)))

    def test_stale_future_wrong_wallet_and_mint_proofs_fail_closed(self):
        for field, value, reason in (
                ('observed_at', T - 11, 'SELL_PROOF_STALE'),
                ('observed_at', T + 1, 'SELL_PROOF_STALE'),
                ('wallet', 'attacker', 'SELL_IDENTITY_MISMATCH'),
                ('mint', 'other', 'SELL_IDENTITY_MISMATCH')):
            with self.subTest(field=field, value=value):
                proof = self.proof()
                proof[field] = value
                self.assertIn(reason, sellability_gate(event(sellability=proof, taker='wallet'), D(100)))

    def test_real_position_rejects_synthetic_model_even_for_matching_wallet(self):
        position = {'provenance': 'MAINNET_OBSERVATION', 'taker': 'wallet'}
        self.assertIn('SYNTHETIC_EXIT_ON_REAL_POSITION',
                      position_sellability(position, event(taker='wallet'), D(100)))

    def test_formatted_hashes_and_asserted_flags_are_not_reconstructed_by_gate(self):
        # Characterizes a future adapter trust boundary, not proof of a real exit.
        market = event(provenance='MAINNET_OBSERVATION', sellability=self.proof(), taker='wallet')
        self.assertEqual(sellability_gate(market, D(100)), [])
        self.assertEqual(position_sellability(
            {'provenance': 'MAINNET_OBSERVATION', 'taker': 'wallet'}, market, D(100)), [])


if __name__ == '__main__':
    unittest.main()
