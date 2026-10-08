import unittest
from unittest.mock import patch
from urllib.parse import urlparse,parse_qs
from desk.providers import jupiter_sequence_probe,jupiter_probe
class RouteLimitTests(unittest.TestCase):
    def test_sequence_requests_bounded_bundle_compatible_routes(self):
        with patch('desk.providers.api_key',return_value='test-only'),patch('desk.providers.fetch_json',return_value={'inAmount':'1','outAmount':'1','routePlan':[]}) as fetch:
            jupiter_sequence_probe('input','output',1,'wallet')
            query=parse_qs(urlparse(fetch.call_args.args[0]).query)
            self.assertEqual(query['maxAccounts'],['32']);self.assertEqual(query['forJitoBundle'],['true']);self.assertEqual(query['transactionVersion'],['0'])
    def test_invalid_budget_rejected_before_network(self):
        with patch('desk.providers.fetch_json') as fetch:
            with self.assertRaises(ValueError):jupiter_probe('input','output',1,'wallet',max_accounts=65)
            fetch.assert_not_called()
