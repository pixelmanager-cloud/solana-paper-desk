"""Exercise the real provider boundary without network or credentials."""
import unittest
from unittest.mock import patch
from desk.providers import helius_rpc


class SnapshotProviderBoundaryTests(unittest.TestCase):
    def test_snapshot_clock_reaches_read_only_transport(self):
        with patch('desk.providers.api_key', return_value='fixture-only'), patch(
            'desk.providers.fetch_json', return_value={'result': 1234}
        ) as transport:
            self.assertEqual(helius_rpc('getBlockTime', [42]), 1234)
            payload = transport.call_args.args[1]
            self.assertEqual(payload['method'], 'getBlockTime')
            self.assertEqual(payload['params'], [42])

    def test_write_methods_still_rejected_before_transport(self):
        with patch('desk.providers.fetch_json') as transport:
            for method in ('sendTransaction', 'sendBundle', 'requestAirdrop'):
                with self.assertRaises(ValueError):
                    helius_rpc(method, [])
            transport.assert_not_called()
