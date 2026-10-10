"""SYNTHETIC_TEST_ONLY: the regime producer's SOL/USD series under USD valuation 2 (T37F item 2).

Fixture attempt originals only (synthetic bytes in a temporary evidence store); no network, no credentials."""
import base64
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import urlencode

from desk import kraken_usd_observation as kraken, regime_producer as producer, usd_valuation as uv
from desk.evidence import EvidenceStore
from desk.sol_usd_observation import SOL_MINT

TS = 1_791_700_000
TTL = 900


def jupiter(price, at, **overrides):
    body = ('{"%s":{"usdPrice":%s,"blockId":100,"decimals":9}}' % (SOL_MINT, price)).encode()
    record = {'kind': 'paper_read_attempt_v1', 'scan_id': 's', 'requests_used': 3, 'source_id': uv.JUPITER_SOURCE_ID,
              'method': uv.JUPITER_METHOD, 'params': {'ids': SOL_MINT},
              'request_bytes_base64': base64.b64encode(urlencode({'ids': SOL_MINT}).encode()).decode(),
              'response_bytes_base64': base64.b64encode(body).decode(), 'observed_at': at, 'http_status': 200, 'failure_code': None}
    record.update(overrides)
    return record


def kraken_attempt(price, at):
    body = ('{"error":[],"result":{"SOLUSD":[["%s","1.00000",%d.25,"s","l","",123]],"last":"123000000000"}}' % (price, at - 1)).encode()
    return {'kind': 'paper_read_attempt_v1', 'scan_id': 's', 'requests_used': 4, 'source_id': kraken.SOURCE, 'method': kraken.METHOD,
            'params': dict(kraken.PARAMS), 'request_bytes_base64': base64.b64encode(urlencode(kraken.PARAMS).encode()).decode(),
            'response_bytes_base64': base64.b64encode(body).decode(), 'observed_at': at, 'http_status': 200, 'failure_code': None,
            'acquired_at_decimal': '%d.5' % at}


class RegimeSeriesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = EvidenceStore(Path(self.tmp.name) / 'evidence.sqlite')
        self.research = Path(self.tmp.name) / 'research.sqlite'
        with sqlite3.connect(self.research) as c:
            c.execute('CREATE TABLE scans(id TEXT PRIMARY KEY, created INTEGER)')
            for n in range(12):
                c.execute('INSERT INTO scans VALUES(?,?)', ('s%d' % n, TS - 60 * (n + 1)))

    def put_series(self, make, prices):
        for age, price in prices:
            self.store.save(make(price, TS - age))

    def change(self, version, **kw):
        return producer.sol_usd_change_pct(self.store.path, TS, TTL, version)

    def test_v2_reads_the_primary_series_even_when_kraken_is_sparse(self):
        self.put_series(jupiter, [(2400, '100'), (1500, '99'), (600, '98.5'), (5, '98')])
        self.put_series(kraken_attempt, [(300, '100.00000')])                       # one cross-check, no span of its own
        self.assertEqual(self.change(2), ('-2.0000', TS - 5))
        self.assertEqual(self.change(1), (None, None))                             # the Kraken-only series cannot answer
        evidence = producer.evidence(self.research, self.store.path, TS, TTL, valuation_version=2)
        self.assertEqual((evidence['sol_usd_change_pct'], evidence['as_of'], evidence['graduations_per_hour']), ('-2.0000', TS - 5, '12'))
        self.assertIsNone(producer.evidence(self.research, self.store.path, TS, TTL))   # flag-absent call: Kraken only, fail closed

    def test_a_kraken_cross_check_never_mixes_into_the_primary_series(self):
        self.put_series(jupiter, [(2400, '100'), (5, '98')])
        self.put_series(kraken_attempt, [(2300, '90.00000'), (4, '120.00000')])      # a wildly different Kraken series
        self.assertEqual(self.change(2), ('-2.0000', TS - 5))
        self.assertEqual(self.change(1), ('33.3333', TS - 4))                       # version 1 still sees Kraken alone

    def test_when_the_primary_series_is_too_short_the_regime_uses_kraken(self):
        self.put_series(jupiter, [(10, '98')])                                       # no span
        self.put_series(kraken_attempt, [(2000, '100.00000'), (8, '99.00000')])
        self.assertEqual(self.change(2), ('-1.0000', TS - 8))

    def test_a_failed_cross_check_leaves_the_regime_on_the_primary(self):
        self.put_series(jupiter, [(2400, '100'), (5, '97')])
        self.store.save({**kraken_attempt('100.00000', TS - 3), 'failure_code': 'TRANSPORT_ERROR', 'response_bytes_base64': None})
        self.assertEqual(self.change(2), ('-3.0000', TS - 5))

    def test_unusable_primary_records_are_ignored(self):
        self.put_series(jupiter, [(2400, '100'), (5, '98')])
        self.store.save(jupiter('50', TS - 3, failure_code='HTTP_REJECTED', http_status=503, response_bytes_base64=None))
        self.store.save(jupiter('50', TS - 2, http_status=500))
        self.store.save(jupiter('0', TS - 1))                                         # not a valid price
        extra = json.dumps({SOL_MINT: {'usdPrice': 50, 'blockId': 100, 'decimals': 9}, 'Other' + '1' * 38: {'usdPrice': 1}}).encode()
        self.store.save(jupiter('50', TS - 6, response_bytes_base64=base64.b64encode(extra).decode()))   # extra mint: refused whole
        self.assertEqual(self.change(2), ('-2.0000', TS - 5))

    def test_stale_future_and_empty_stores_fail_closed(self):
        self.assertEqual(self.change(2), (None, None))
        self.put_series(jupiter, [(7000, '100'), (3000, '98')])                       # newest older than the TTL
        self.assertEqual(self.change(2), (None, None))
        self.store.save(jupiter('90', TS + 50))                                       # future: outside the window
        self.assertEqual(self.change(2), (None, None))
        self.assertIsNone(producer.evidence(self.research, self.store.path, TS, TTL, valuation_version=2))
        self.assertIsNone(producer.evidence(self.research, '/nonexistent.sqlite', TS, TTL, valuation_version=2))

    def test_the_series_is_bounded_to_the_newest_pages(self):
        self.put_series(jupiter, [(2400, '100'), (5, '98')])
        for i in range(uv.SCAN_PAGES + 5):
            self.store.save({'kind': 'filler', 'i': i})
        self.assertEqual(self.change(2), (None, None))                                # read cost never grows with the store


if __name__ == '__main__':
    unittest.main()
