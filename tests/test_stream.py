import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desk.ledger import Ledger
from desk.providers import record_stream, SubscriptionRejected


class FakeSocket:
    def __init__(self, rejected=False):
        self.messages = [{"id": 1, "error": {"code": -1}} if rejected else {"id": 1, "result": 7},
                         {"method": "transactionNotification", "params": {"result": {"signature": "sig", "slot": 10}}}]

    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def send(self, data): self.sent = json.loads(data)
    async def recv(self): return json.dumps(self.messages.pop(0))


@unittest.skipUnless(importlib.util.find_spec("websockets"), "optional streaming dependency not installed")
class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_stream_and_no_secret_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "raw.sqlite")
            with patch("desk.providers.api_key", return_value="test-secret-never-log"), \
                 patch("websockets.asyncio.client.connect", return_value=FakeSocket()):
                result = await record_stream(ledger, ["pool"], 10, 1, 100000)
            self.assertEqual(result["status"], "CAPTURED")
            self.assertEqual(result["new_records"], 1)
            self.assertFalse(result["history_complete"])
            self.assertNotIn("test-secret", json.dumps(ledger.report()))
            ledger.close()

    async def test_rejected_subscription_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "raw.sqlite")
            with patch("desk.providers.api_key", return_value="test-secret"), \
                 patch("websockets.asyncio.client.connect", return_value=FakeSocket(True)):
                with self.assertRaises(SubscriptionRejected):
                    await record_stream(ledger, ["pool"], 10, 1, 100000)
            self.assertEqual(ledger.report()["health"][-1]["code"], "SUBSCRIPTION_REJECTED")
            ledger.close()
