"""Original sequence identity binding; public accounting fixture, mock-only wire."""
import copy
import hashlib
import unittest
from unittest.mock import patch

from desk.roundtrip import simulate_roundtrip
from tests import test_roundtrip as fixtures


class RoundtripIdentityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RoundtripTests()
        self.fixture.setUp()
        # The public capture omits original transaction bytes. These distinct
        # stand-ins test the connector, never real route/transaction validation.
        self.legs = [{**leg, 'raw': b'explicit-synthetic-leg-' + bytes([i])}
                     for i, leg in enumerate(self.fixture.p['legs'])]
        self.sequence = copy.deepcopy(self.fixture.sequence)
        self.sequence['transaction_hashes'] = [hashlib.sha256(leg['raw']).hexdigest()
                                               for leg in self.legs]

    def run_with(self, sequence):
        fixture = self.fixture
        with patch('desk.roundtrip.compile_unsigned', side_effect=self.legs), \
             patch('desk.roundtrip.simulate_sequence', return_value=sequence), \
             patch('socket.socket', side_effect=AssertionError('No network')):
            return simulate_roundtrip(fixture.r['mint'], fixture.r['wallet'],
                                      int(fixture.r['spend_lamports']),
                                      rpc=fixture.rpc, quote=fixture.quote)

    def assert_unbound(self, result, reason):
        self.assertIn(reason, result['reasons'])
        self.assertFalse(result['effects_passed'])
        self.assertEqual(result['leg_effects'], [])
        self.assertEqual(result['leg_controls'], [])
        self.assertIsNone(result['residual_token_raw'])
        self.assertIsNone(result['net_native_wealth_delta_lamports'])
        self.assertEqual(result['sell_quantity_witness']['status'], 'UNKNOWN')
        self.assertFalse(result['transaction_policy_ok'])
        self.assertFalse(result['eligible_for_trading'])
        self.assertEqual(self.fixture.calls, ['getMultipleAccounts', 'getLatestBlockhash'])

    def test_both_exact_ordered_hashes_required_even_when_rows_look_successful(self):
        hashes = self.sequence['transaction_hashes']
        for supplied in (None, [], hashes[:1], hashes + ['0'*64], tuple(hashes),
                         list(reversed(hashes)), ['0'*64, hashes[1]],
                         [hashes[0], '0'*64]):
            with self.subTest(hashes=supplied):
                self.fixture.setUp()
                sequence = copy.deepcopy(self.sequence)
                sequence['transaction_hashes'] = supplied
                self.assert_unbound(self.run_with(sequence),
                                    'ROUNDTRIP_TRANSACTION_IDENTITY_MISMATCH')

    def test_exact_snapshot_watch_order_required_before_effects_or_quantity(self):
        watch = self.sequence['watch']
        for supplied in (None, [], watch[:-1], list(reversed(watch)), tuple(watch),
                         watch + [watch[0]]):
            with self.subTest(watch=supplied):
                self.fixture.setUp()
                sequence = copy.deepcopy(self.sequence)
                sequence['watch'] = supplied
                self.assert_unbound(self.run_with(sequence),
                                    'ROUNDTRIP_WATCHLIST_IDENTITY_MISMATCH')

    def test_bound_accounting_remains_diagnostic_and_does_not_add_requests(self):
        result = self.run_with(self.sequence)
        self.assertTrue(result['effects_passed'])
        self.assertEqual(result['residual_token_raw'], '25489')
        self.assertEqual(result['net_native_wealth_delta_lamports'], '-143010')
        self.assertEqual(len(result['leg_effects']), 2)
        self.assertFalse(result['transaction_policy_ok'])
        self.assertFalse(result['eligible_for_trading'])
        self.assertEqual(self.fixture.calls, ['getMultipleAccounts', 'getLatestBlockhash'])
