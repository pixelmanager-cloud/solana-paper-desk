"""Offline only. Public documented example + SYNTHETIC_TEST_ONLY mutations.

Example from https://developers.jup.ag/docs/guides/how-to-get-token-price,
read 2026-10-09. Its price/block are historical documentation, not live data.
All time/slot associations below are SYNTHETIC_TEST_ONLY, not chain captures.
"""
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import unittest
from unittest.mock import patch

from desk.sol_usd_observation import (
    JupiterPriceResponse, TrustedSlotBounds, SOL_MINT, PRICE_URL,
    MAX_RESPONSE_BYTES, parse_sol_usd,
)


class SolUsdTests(unittest.TestCase):
    def setUp(self):
        self.item = {"createdAt": "2024-06-05T08:55:25.527Z",
                     "liquidity": 621679197.67, "usdPrice": 147.48,
                     "blockId": 348004023, "decimals": 9, "priceChange24h": 1.29}
        self.bounds = TrustedSlotBounds(1000, 999, 348004020, 348004030,
                                       ((348004023, 995), (348004030, 999)))
        self.obs = JupiterPriceResponse("GET", PRICE_URL,
                                       json.dumps({SOL_MINT: self.item}).encode(), 998)

    def parse(self, item=None, payload=None, **changes):
        raw = json.dumps(payload if payload is not None else
                         {SOL_MINT: self.item if item is None else item}).encode()
        return parse_sol_usd(replace(self.obs, raw_payload=raw, **changes), bounds=self.bounds)

    def unknown(self, result, blocker):
        self.assertEqual(result.status, "UNKNOWN")
        self.assertIsNone(result.usd_price)
        self.assertIn(blocker, result.blockers)

    def test_public_documented_example_with_synthetic_clock_not_live_price(self):
        with patch("socket.socket", side_effect=AssertionError("no transport")):
            result = parse_sol_usd(self.obs, bounds=self.bounds)
        self.assertEqual(result.status, "MEASURED")
        self.assertEqual(result.usd_price, Decimal("147.48"))
        self.assertEqual((result.block_id, result.price_at, result.acquired_at),
                         (348004023, 995, 998))
        self.assertEqual(result.purpose, "USD_VALUATION_ONLY")
        self.assertEqual(result.blockers, ())

    def test_exact_original_bytes_request_hash_unknown_fields_and_bounds_retained(self):
        raw = b'{ "' + SOL_MINT.encode() + b'": {"usdPrice":147.4800,"decimals":9,"blockId":348004023,"futureField":42}}\n'
        result = parse_sol_usd(replace(self.obs, raw_payload=raw), bounds=self.bounds)
        self.assertEqual(result.raw_payload, raw)
        self.assertEqual(result.payload_sha256, hashlib.sha256(raw).hexdigest())
        request = json.dumps({"method":"GET", "url":PRICE_URL}, sort_keys=True, separators=(",", ":"))
        self.assertEqual(result.request_sha256, hashlib.sha256(request.encode()).hexdigest())
        self.assertEqual(result.request, ("GET", PRICE_URL))
        self.assertEqual(result.bounds, self.bounds)
        self.assertNotEqual(result.payload_sha256, self.parse().payload_sha256)

    def test_created_at_never_dates_or_refreshes_price(self):
        for created in (None, "future", "2099-01-01T00:00:00Z", 1000):
            with self.subTest(created=created):
                result = self.parse(item={**self.item, "createdAt": created})
                self.assertEqual(result.price_at, 995)
                self.assertEqual(result.status, "MEASURED")
        self.bounds = replace(self.bounds, block_times=((348004023, 950),))
        self.unknown(self.parse(item={**self.item, "createdAt":1000}), "PRICE_BLOCK_STALE_OR_FUTURE")

    def test_no_sol_no_peg_and_null_missing_fields(self):
        for payload in ({}, {"USDC":{"usdPrice":1}}, {SOL_MINT:None}):
            with self.subTest(payload=payload):
                self.unknown(self.parse(payload=payload), "SOL_PRICE_MISSING")
        for value in (None, True, False, 0, -1, "147.48", [], {}):
            self.unknown(self.parse(item={**self.item, "usdPrice":value}),
                         "USD_PRICE_FINITE_POSITIVE_NUMBER_REQUIRED")
        self.unknown(self.parse(item={"blockId":348004023,"decimals":9}),
                     "USD_PRICE_FINITE_POSITIVE_NUMBER_REQUIRED")

    def test_decimals_and_block_id_strict_schema(self):
        for decimals in (None, True, "9", 9.0, 6):
            self.unknown(self.parse(item={**self.item,"decimals":decimals}), "SOL_DECIMALS_9_REQUIRED")
        for slot in (None, True, "348004023", 348004023.0, 0, -1):
            self.unknown(self.parse(item={**self.item,"blockId":slot}), "PRICE_BLOCK_ID_REQUIRED")
        for slot in (348004019, 348004031):
            self.unknown(self.parse(item={**self.item,"blockId":slot}), "PRICE_BLOCK_OUTSIDE_TRUSTED_RANGE")
        self.unknown(self.parse(item={**self.item,"blockId":348004024}), "PRICE_BLOCK_TIME_UNKNOWN")

    def test_malformed_duplicate_and_nonfinite_preserve_original(self):
        for raw in (b'not-json', b'\xff', b'[]', b'null',
                    b'{"x":1,"x":2}',
                    ('{"'+SOL_MINT+'":{"usdPrice":NaN}}').encode(),
                    ('{"'+SOL_MINT+'":{"usdPrice":Infinity}}').encode(),
                    b'[' * 2000 + b']' * 2000):
            with self.subTest(raw=raw[:50]):
                result = parse_sol_usd(replace(self.obs, raw_payload=raw), bounds=self.bounds)
                self.assertEqual(result.status, "UNKNOWN")
                self.assertIsNone(result.usd_price)
                self.assertEqual(result.raw_payload, raw)
                self.assertEqual(result.payload_sha256, hashlib.sha256(raw).hexdigest())
        self.unknown(self.parse(payload={SOL_MINT: []}), "SOL_PRICE_OBJECT_REQUIRED")

    def test_acquisition_and_trusted_capture_age_boundaries_and_reuse(self):
        self.assertEqual(self.parse(acquired_at=990).status, "MEASURED")
        for time in (989, 1001):
            self.unknown(self.parse(acquired_at=time), "ACQUISITION_STALE_OR_FUTURE")
        for time in (989, 1001):
            bounds = replace(self.bounds, observed_at=time, block_times=((348004023, 980),))
            result = parse_sol_usd(self.obs, bounds=bounds)
            self.unknown(result, "TRUSTED_SLOT_CAPTURE_STALE_OR_FUTURE")
        self.unknown(parse_sol_usd(self.obs, bounds=replace(self.bounds, now=1011)),
                     "ACQUISITION_STALE_OR_FUTURE")

    def test_actual_block_age_boundary_no_slot_time_interpolation(self):
        for block_time, expected in ((970,"MEASURED"),(969,"UNKNOWN")):
            result = parse_sol_usd(self.obs, bounds=replace(self.bounds, block_times=((348004023,block_time),)))
            self.assertEqual(result.status, expected)
        missing = replace(self.bounds, block_times=((348004030,999),))
        self.unknown(parse_sol_usd(self.obs, bounds=missing), "PRICE_BLOCK_TIME_UNKNOWN")

    def test_http_error_does_not_certify_even_plausible_payload(self):
        for status in (401,429,500,201):
            result = self.parse(http_status=status)
            self.unknown(result,"HTTP_NOT_200")
            self.assertEqual(result.http_status,status)
            self.assertEqual(result.raw_payload,self.obs.raw_payload)

    def test_exact_request_only_no_credentials_multi_mints_or_override(self):
        for url in (PRICE_URL+",USDC",PRICE_URL+"&api-key=NOT_A_REAL_KEY",
                    PRICE_URL.replace("https:","http:"),PRICE_URL.replace("api.jup.ag","example.test"),
                    PRICE_URL.replace(SOL_MINT,"USDC"),PRICE_URL+"#fragment"):
            with self.assertRaisesRegex(ValueError,"exact credential-free"):
                parse_sol_usd(replace(self.obs,url=url),bounds=self.bounds)
        with self.assertRaises(ValueError):
            parse_sol_usd(replace(self.obs,method="POST"),bounds=self.bounds)
        with self.assertRaisesRegex(ValueError,"typed coordinator"):
            parse_sol_usd({},bounds=self.bounds)

    def test_invalid_bounded_adapter_metadata(self):
        for changes in ({"raw_payload":b'x'*(MAX_RESPONSE_BYTES+1)},
                        {"raw_payload":bytearray(b'{}')},{"acquired_at":True},
                        {"acquired_at":-1},{"http_status":True}):
            with self.assertRaises(ValueError):
                parse_sol_usd(replace(self.obs,**changes),bounds=self.bounds)
        for changes in ({"now":True},{"observed_at":-1},{"max_slot":348004053},
                        {"max_slot":348004019},{"block_times":()},
                        {"block_times":((348004023,995),(348004023,995))},
                        {"block_times":((348004023,1000),)},
                        {"block_times":((348004023,998),(348004030,997))},
                        {"block_times":((348004019,995),)},
                        {"block_times":[(348004023,995)]},
                        {"block_times":((348004023,True),)}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                parse_sol_usd(self.obs,bounds=replace(self.bounds,**changes))


if __name__ == "__main__":
    unittest.main()
