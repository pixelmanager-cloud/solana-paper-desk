"""SYNTHETIC_TEST_ONLY: SOL/USD valuation v2 (Jupiter PriceV3 primary, Kraken fallback), pure core.

Fixture bytes only; no network, no credentials. The Jupiter body has the shape the paid PriceV3 endpoint returns
(`usdPrice`, `blockId`, `decimals`, plus fields we ignore)."""
import base64
import copy
import json
import unittest
from decimal import Decimal as D
from urllib.parse import urlencode

from desk import kraken_usd_observation as kraken, usd_valuation as uv
from desk.model import canonical, digest
from desk.sol_usd_observation import SOL_MINT

NOW = 1_791_700_000
SCAN = 'scan-1'
CFG = {'mode': 'paper', 'paper_usd_valuation_version': 2, 'paper_signal_policy_version': 3,
       'paper_quote_execution_version': 1}


def jupiter_body(price='84.12', block=376191212, **item):
    entry = {'usdPrice': D(price), 'blockId': block, 'decimals': 9, 'priceChange24h': D('-1.25'),
             'createdAt': '2023-03-21T09:43:27Z', **item}
    entry = {k: v for k, v in entry.items() if v is not None}
    body = json.dumps({SOL_MINT: entry}, default=lambda d: float(d) if False else str(d))
    # PriceV3 sends JSON numbers, not strings: re-render the Decimals as numbers
    for key in ('usdPrice', 'priceChange24h'):
        if key in entry:
            body = body.replace('"%s": "%s"' % (key, entry[key]), '"%s": %s' % (key, entry[key]))
    return body.encode()


def jupiter_record(raw=None, *, observed_at=NOW - 2, status=200, failure=None, scan=SCAN, **overrides):
    record = {'kind': 'paper_read_attempt_v1', 'scan_id': scan, 'requests_used': 3, 'source_id': uv.JUPITER_SOURCE_ID,
              'method': uv.JUPITER_METHOD, 'params': {'ids': SOL_MINT},
              'request_bytes_base64': base64.b64encode(urlencode({'ids': SOL_MINT}).encode()).decode(),
              'response_bytes_base64': None if raw is None else base64.b64encode(raw).decode(),
              'observed_at': observed_at, 'http_status': status, 'failure_code': failure}
    record.update(overrides)
    return record


def kraken_body(price='84.20', trade_at=NOW - 3):
    return ('{"error":[],"result":{"SOLUSD":[["%s","1.00000",%d.25,"s","l","",123]],"last":"123000000000"}}'
            % (price, trade_at)).encode()


def kraken_record(raw=None, *, acquired=NOW - 1, failure=None, scan=SCAN, **overrides):
    record = {'kind': 'paper_read_attempt_v1', 'scan_id': scan, 'requests_used': 4, 'source_id': kraken.SOURCE,
              'method': kraken.METHOD, 'params': dict(kraken.PARAMS),
              'request_bytes_base64': base64.b64encode(urlencode(kraken.PARAMS).encode()).decode(),
              'response_bytes_base64': None if raw is None else base64.b64encode(raw).decode(),
              'observed_at': acquired, 'http_status': 200, 'failure_code': failure,
              'acquired_at_decimal': '%d.5' % acquired}
    record['observed_at'] = int(record['acquired_at_decimal'].split('.')[0])
    record.update(overrides)
    return record


def attempts(primary='ok', fallback=None):
    p = {'ok': lambda: jupiter_record(jupiter_body()), 'none': lambda: None}[primary]() if isinstance(primary, str) else primary
    return {'primary': p, 'fallback': fallback}


class ParseTests(unittest.TestCase):
    def parse(self, raw, **kw):
        kw.setdefault('acquired_at', NOW - 2); kw.setdefault('http_status', 200); kw.setdefault('now', NOW)
        return uv.parse_jupiter(raw, **kw)

    def test_the_observed_shape_parses(self):
        price, blockers, block = self.parse(jupiter_body())
        self.assertEqual((price, blockers, block), (D('84.12'), (), 376191212))
        self.assertEqual(self.parse(b'{"%s":{"usdPrice":84,"blockId":5,"decimals":9}}' % SOL_MINT.encode())[0], D(84))

    def test_every_malformed_field_is_refused(self):
        cases = {
            'not json': (b'<html>', 'MALFORMED_JSON'),
            'array': (b'[]', 'RESPONSE_OBJECT_REQUIRED'),
            'missing sol': (b'{"other":{}}', 'SOL_PRICE_MISSING'),
            'null sol': (b'{"%s":null}' % SOL_MINT.encode(), 'SOL_PRICE_MISSING'),
            'sol not object': (b'{"%s":5}' % SOL_MINT.encode(), 'SOL_PRICE_OBJECT_REQUIRED'),
            'duplicate keys': (b'{"%s":{"usdPrice":84,"usdPrice":85,"blockId":5,"decimals":9}}' % SOL_MINT.encode(), 'MALFORMED_JSON'),
            'NaN': (b'{"%s":{"usdPrice":NaN,"blockId":5,"decimals":9}}' % SOL_MINT.encode(), 'MALFORMED_JSON'),
            'huge exponent': (b'{"%s":{"usdPrice":1e999,"blockId":5,"decimals":9}}' % SOL_MINT.encode(), 'MALFORMED_JSON'),
            'price string': (jupiter_body(usdPrice='84.12').replace(b'84.12,', b'"84.12",'), 'USD_PRICE_FINITE_POSITIVE_NUMBER_REQUIRED'),
            'price zero': (b'{"%s":{"usdPrice":0,"blockId":5,"decimals":9}}' % SOL_MINT.encode(), 'USD_PRICE_FINITE_POSITIVE_NUMBER_REQUIRED'),
            'price negative': (b'{"%s":{"usdPrice":-84,"blockId":5,"decimals":9}}' % SOL_MINT.encode(), 'USD_PRICE_FINITE_POSITIVE_NUMBER_REQUIRED'),
            'price absurd': (b'{"%s":{"usdPrice":10000000,"blockId":5,"decimals":9}}' % SOL_MINT.encode(), 'USD_PRICE_OUT_OF_SANITY_RANGE'),
            'price dust': (b'{"%s":{"usdPrice":0.0001,"blockId":5,"decimals":9}}' % SOL_MINT.encode(), 'USD_PRICE_OUT_OF_SANITY_RANGE'),
            'price missing': (b'{"%s":{"blockId":5,"decimals":9}}' % SOL_MINT.encode(), 'USD_PRICE_FINITE_POSITIVE_NUMBER_REQUIRED'),
            'block missing': (b'{"%s":{"usdPrice":84,"decimals":9}}' % SOL_MINT.encode(), 'PRICE_BLOCK_ID_REQUIRED'),
            'block zero': (b'{"%s":{"usdPrice":84,"blockId":0,"decimals":9}}' % SOL_MINT.encode(), 'PRICE_BLOCK_ID_REQUIRED'),
            'block negative': (b'{"%s":{"usdPrice":84,"blockId":-1,"decimals":9}}' % SOL_MINT.encode(), 'PRICE_BLOCK_ID_REQUIRED'),
            'block string': (b'{"%s":{"usdPrice":84,"blockId":"5","decimals":9}}' % SOL_MINT.encode(), 'PRICE_BLOCK_ID_REQUIRED'),
            'block bool': (b'{"%s":{"usdPrice":84,"blockId":true,"decimals":9}}' % SOL_MINT.encode(), 'PRICE_BLOCK_ID_REQUIRED'),
            'block huge': (b'{"%s":{"usdPrice":84,"blockId":99999999999999999999,"decimals":9}}' % SOL_MINT.encode(), 'PRICE_BLOCK_ID_REQUIRED'),
            'decimals 6': (b'{"%s":{"usdPrice":84,"blockId":5,"decimals":6}}' % SOL_MINT.encode(), 'SOL_DECIMALS_9_REQUIRED'),
            'decimals missing': (b'{"%s":{"usdPrice":84,"blockId":5}}' % SOL_MINT.encode(), 'SOL_DECIMALS_9_REQUIRED'),
            'decimals float': (b'{"%s":{"usdPrice":84,"blockId":5,"decimals":9.0}}' % SOL_MINT.encode(), 'SOL_DECIMALS_9_REQUIRED'),
        }
        for name, (raw, code) in cases.items():
            price, blockers, _ = self.parse(raw)
            self.assertIsNone(price, name)
            self.assertIn(code, blockers, name)
        self.assertEqual(self.parse(b'')[1][-1], 'MALFORMED_JSON')
        self.assertEqual(self.parse(b'x' * 70000)[1][-1], 'MALFORMED_JSON')

    def test_freshness_and_http_status(self):
        self.assertEqual(self.parse(jupiter_body(), acquired_at=NOW - uv.PRICE_TTL_SECONDS)[1], ())
        self.assertIn('ACQUISITION_STALE_OR_FUTURE', self.parse(jupiter_body(), acquired_at=NOW - 11)[1])
        self.assertIn('ACQUISITION_STALE_OR_FUTURE', self.parse(jupiter_body(), acquired_at=NOW + 1)[1])
        self.assertIn('HTTP_NOT_200', self.parse(jupiter_body(), http_status=429)[1])
        self.assertIsNone(self.parse(jupiter_body(), http_status=429)[0])
        for bad in ({'acquired_at': 1.5}, {'now': '5'}, {'acquired_at': -1}):
            with self.assertRaises(ValueError):
                self.parse(jupiter_body(), **bad)


class ChooseTests(unittest.TestCase):
    def obs(self, source, price, status='MEASURED'):
        return uv.Observation(source, status, None if price is None else D(price), (), 'h', 'p', 'r')

    def test_selection_matrix(self):
        J, K = self.obs('JUPITER', '84'), self.obs('KRAKEN', '84.2')
        limit = D('0.01')
        for purpose in ('entry', 'exit'):
            self.assertEqual(uv.choose(J, K, purpose=purpose, limit=limit).status, 'PRIMARY')
            self.assertEqual(uv.choose(J, None, purpose=purpose, limit=limit).status, 'PRIMARY')
            fallback = uv.choose(None, K, purpose=purpose, limit=limit)
            self.assertEqual((fallback.status, fallback.source, fallback.usd_price), ('FALLBACK', 'KRAKEN', D('84.2')))
            self.assertEqual(uv.choose(self.obs('JUPITER', None, 'UNKNOWN'), K, purpose=purpose, limit=limit).status, 'FALLBACK')
            self.assertEqual(uv.choose(None, None, purpose=purpose, limit=limit).blockers, ('SOL_USD_UNAVAILABLE',))
            self.assertEqual(uv.choose(J, self.obs('KRAKEN', None, 'UNKNOWN'), purpose=purpose, limit=limit).status, 'PRIMARY')

    def test_divergence_blocks_entries_only_and_exits_use_the_primary(self):
        J = self.obs('JUPITER', '100')
        for price, blocked in (('100.99', False), ('101', False), ('101.01', True), ('99', False), ('98.99', True), ('50', True)):
            K = self.obs('KRAKEN', price)
            entry = uv.choose(J, K, purpose='entry', limit=D('0.01'))
            self.assertEqual(entry.status == 'DIVERGENCE', blocked, price)
            if blocked:
                self.assertEqual((entry.usd_price, entry.source, entry.blockers), (None, None, ('USD_SOURCE_DIVERGENCE',)))
            exit_ = uv.choose(J, K, purpose='exit', limit=D('0.01'))
            self.assertEqual((exit_.status, exit_.source, exit_.usd_price), ('PRIMARY', 'JUPITER', D('100')), price)
        with self.assertRaises(ValueError):
            uv.choose(J, J, purpose='held', limit=D('0.01'))


class EvidenceTests(unittest.TestCase):
    def build(self, primary, fallback, purpose='entry', cfg=CFG):
        return uv.evidence({'primary': primary, 'fallback': fallback}, now=str(NOW) + '.5', scan=SCAN, purpose=purpose, cfg=cfg)

    def test_primary_only_primary_plus_crosscheck_and_fallback_after_each_primary_failure(self):
        J = jupiter_record(jupiter_body())
        K = kraken_record(kraken_body())
        only = self.build(J, None)
        self.assertEqual((only['selection'], only['selected_source'], only['usd_price'], only['fallback']),
                         ('PRIMARY', 'JUPITER', '84.12', None))
        both = self.build(J, K)
        self.assertEqual((both['selection'], both['divergence']), ('PRIMARY', str(uv.divergence(D('84.12'), D('84.20')))))
        self.assertEqual(both['fallback']['status'], 'MEASURED')
        failures = {
            'transport failure': jupiter_record(None, failure='TRANSPORT_ERROR', observed_at=None),
            'http 429': jupiter_record(jupiter_body(), status=429, failure='HTTP_REJECTED'),
            'stale': jupiter_record(jupiter_body(), observed_at=NOW - 30),
            'malformed': jupiter_record(b'{"nope":1}'),
            'no blockId': jupiter_record(b'{"%s":{"usdPrice":84,"decimals":9}}' % SOL_MINT.encode()),
        }
        for name, bad in failures.items():
            evidence = self.build(bad, K)
            self.assertEqual((evidence['selection'], evidence['selected_source'], evidence['usd_price']),
                             ('FALLBACK', 'KRAKEN', '84.20'), name)
            self.assertEqual(evidence['primary']['status'], 'UNKNOWN', name)
            self.assertTrue(evidence['primary']['blockers'], name)

    def test_nothing_usable_raises_and_divergence_raises_for_entries_but_not_exits(self):
        for primary, fallback in ((None, None), (jupiter_record(None, failure='TRANSPORT_ERROR', observed_at=None), None),
                                  (jupiter_record(None, failure='X', observed_at=None), kraken_record(None, failure='Y'))):
            with self.assertRaisesRegex(ValueError, 'SOL_USD_UNAVAILABLE'):
                self.build(primary, fallback)
        J, K = jupiter_record(jupiter_body('100')), kraken_record(kraken_body('103'))
        with self.assertRaisesRegex(ValueError, 'USD_SOURCE_DIVERGENCE'):
            self.build(J, K)
        evidence = self.build(J, K, purpose='exit')
        self.assertEqual((evidence['selected_source'], evidence['usd_price'], evidence['divergence']),
                         ('JUPITER', '100', '0.03'))

    def test_the_configured_limit_applies_and_the_evidence_is_deterministic(self):
        J, K = jupiter_record(jupiter_body('100')), kraken_record(kraken_body('103'))
        wide = {**CFG, uv.KEY_DIVERGENCE: 0.05}
        first = self.build(J, K, cfg=wide)
        self.assertEqual((first['selection'], first['divergence_max_fraction']), ('PRIMARY', '0.05'))
        self.assertEqual(canonical(first), canonical(self.build(copy.deepcopy(J), copy.deepcopy(K), cfg=wide)))
        self.assertEqual(digest(first), digest(self.build(J, K, cfg=wide)))

    def test_records_that_do_not_belong_are_integrity_errors_not_fallbacks(self):
        K = kraken_record(kraken_body())
        bad = {
            'wrong scan': jupiter_record(jupiter_body(), scan='other'),
            'wrong source': jupiter_record(jupiter_body(), source_id='helius-mainnet-paper-confirmed-v1'),
            'wrong method': jupiter_record(jupiter_body(), method='getSlot'),
            'wrong params': jupiter_record(jupiter_body(), params={'ids': 'X'}),
            'wrong request bytes': jupiter_record(jupiter_body(), request_bytes_base64=base64.b64encode(b'ids=X').decode()),
            'extra key': {**jupiter_record(jupiter_body()), 'surprise': 1},
            'non canonical wire': jupiter_record(jupiter_body(), response_bytes_base64=base64.b64encode(jupiter_body()).decode() + '\n'),
            'oversize wire': jupiter_record(b'x' * 70000),
            'not a dict': [],
        }
        for name, record in bad.items():
            with self.assertRaises(ValueError, msg=name):
                self.build(record, K)
        for name, record in {'k scan': kraken_record(kraken_body(), scan='other'),
                             'k params': kraken_record(kraken_body(), params={'pair': 'ETHUSD', 'count': '1'}),
                             'k missing decimal': {k: v for k, v in kraken_record(kraken_body()).items() if k != 'acquired_at_decimal'}}.items():
            with self.assertRaises(ValueError, msg=name):
                self.build(jupiter_record(jupiter_body()), record)
        with self.assertRaises(ValueError):
            uv.decide({'primary': None}, now='1.5', scan=SCAN, purpose='entry', cfg=CFG)

    def event(self, evidence, kind='market'):
        source = {'scan_id': SCAN, 'usd': uv.summary_for_source(evidence)} if kind == 'market' else {'scan_id': SCAN}
        event = {'kind': kind, 'ts': NOW, 'paper_usd_valuation': evidence}
        event['paper_source_evidence' if kind == 'market' else 'source_evidence'] = source
        if kind == 'market':
            event['sol_usd'] = evidence['usd_price']
        return event

    def test_a_saved_valuation_is_rebuilt_and_every_tamper_is_refused(self):
        J, K = jupiter_record(jupiter_body()), kraken_record(kraken_body())
        evidence = self.build(J, K)
        uv.validate_event(self.event(evidence), CFG)
        uv.validate_event(self.event(self.build(J, K, 'exit'), 'quote_exit'), CFG)
        def tampered(mutate):
            event = copy.deepcopy(self.event(evidence)); mutate(event); return event
        edits = {
            'price': lambda e: e['paper_usd_valuation'].__setitem__('usd_price', '90'),
            'sol_usd': lambda e: e.__setitem__('sol_usd', '90'),
            'selected source': lambda e: e['paper_usd_valuation'].__setitem__('selected_source', 'KRAKEN'),
            'selection': lambda e: e['paper_usd_valuation'].__setitem__('selection', 'FALLBACK'),
            'primary bytes': lambda e: e['paper_usd_valuation']['primary']['attempt'].__setitem__(
                'response_bytes_base64', base64.b64encode(jupiter_body('99')).decode()),
            'drop primary': lambda e: e['paper_usd_valuation'].__setitem__('primary', None),
            'drop fallback evidence': lambda e: e['paper_usd_valuation'].__setitem__('fallback', None),
            'decision time': lambda e: e['paper_usd_valuation'].__setitem__('decision_at', str(NOW - 5) + '.5'),
            'purpose': lambda e: e['paper_usd_valuation'].__setitem__('decision_purpose', 'exit'),
            'divergence': lambda e: e['paper_usd_valuation'].__setitem__('divergence', '0'),
            'limit': lambda e: e['paper_usd_valuation'].__setitem__('divergence_max_fraction', '0.2'),
            'source label': lambda e: e['paper_usd_valuation'].__setitem__('source', kraken.SOURCE),
            'version': lambda e: e['paper_usd_valuation'].__setitem__('version', 1),
            'scan': lambda e: e['paper_source_evidence'].__setitem__('scan_id', 'other'),
            'usd summary': lambda e: e['paper_source_evidence']['usd'].__setitem__('usd_price', '1'),
            'missing valuation': lambda e: e.pop('paper_usd_valuation'),
        }
        for name, mutate in edits.items():
            with self.assertRaises(ValueError, msg=name):
                uv.validate_event(tampered(mutate), CFG)
        with self.assertRaises(ValueError):
            uv.validate_event(self.event(evidence), {**CFG, uv.KEY_DIVERGENCE: 0.5})   # rebuilt with another limit
        uv.validate_event({'kind': 'clock', 'ts': NOW}, CFG)
        with self.assertRaises(ValueError):
            uv.validate_event({'kind': 'clock', 'ts': NOW, 'paper_usd_valuation': evidence}, CFG)


class ConfigTests(unittest.TestCase):
    def test_selected(self):
        self.assertEqual(uv.selected({'mode': 'paper'}), 0)
        self.assertEqual(uv.selected(CFG), 2)
        self.assertEqual(uv.selected({**CFG, uv.KEY_DIVERGENCE: 0.02}), 2)
        self.assertEqual(uv.fresh_requests(0), 9); self.assertEqual(uv.fresh_requests(1), 7); self.assertEqual(uv.fresh_requests(2), 8)
        legacy = {**CFG, 'paper_usd_valuation_version': 1}
        self.assertEqual(uv.selected(legacy), 1)
        self.assertEqual(kraken.selected(CFG), 2)               # the shared interface: version 2 is a valid selection
        self.assertEqual(kraken.selected(legacy), 1)
        for bad in ({'paper_usd_valuation_version': 3}, {'paper_usd_valuation_version': True}, {'paper_usd_valuation_version': '2'},
                    {'paper_usd_valuation_version': 0}, {'mode': 'live'}, {'paper_signal_policy_version': 2},
                    {'paper_quote_execution_version': 0}, {uv.KEY_DIVERGENCE: 0}, {uv.KEY_DIVERGENCE: -0.01},
                    {uv.KEY_DIVERGENCE: 0.26}, {uv.KEY_DIVERGENCE: True}, {uv.KEY_DIVERGENCE: 'abc'},
                    {uv.KEY_DIVERGENCE: 'NaN'}, {uv.KEY_DIVERGENCE: None}):
            with self.assertRaises(ValueError, msg=bad):
                uv.selected({**CFG, **bad})
            with self.assertRaises(ValueError, msg=bad):
                kraken.selected({**CFG, **bad})


class ExtraMintTests(unittest.TestCase):
    """T37F: the request names one id; an answer that carries any other mint is refused whole."""

    def parse(self, payload):
        return uv.parse_jupiter(payload, acquired_at=NOW - 2, http_status=200, now=NOW)

    def test_an_extra_mint_is_refused_even_with_a_valid_sol_entry(self):
        good = json.loads(jupiter_body())
        extra = {**good, 'So11111111111111111111111111111111111111113': good[SOL_MINT]}
        price, blockers, block = self.parse(json.dumps(extra).encode())
        self.assertEqual((price, blockers, block), (None, ('UNEXPECTED_EXTRA_MINTS',), None))
        self.assertEqual(self.parse(jupiter_body())[0], D('84.12'))            # control: the exact shape parses

    def test_an_extra_mint_without_sol_is_still_a_missing_price(self):
        price, blockers, _ = self.parse(json.dumps({'OtherMint1111111111111111111111111111111111': {'usdPrice': 1}}).encode())
        self.assertEqual((price, blockers), (None, ('SOL_PRICE_MISSING',)))

    def test_an_extra_mint_makes_the_observation_unknown_and_falls_back(self):
        good = json.loads(jupiter_body())
        raw = json.dumps({**good, 'X' * 43: good[SOL_MINT]}).encode()
        decision, _ = uv.decide({'primary': jupiter_record(raw), 'fallback': kraken_record(kraken_body())}, now=str(NOW) + '.5', scan=SCAN,
                                purpose='entry', cfg=CFG)
        self.assertEqual((decision.status, decision.source), ('FALLBACK', 'KRAKEN'))


class UnavailableTests(unittest.TestCase):
    def test_no_usable_price_raises_a_typed_error_with_ordinary_blockers(self):
        both_failed = {'primary': jupiter_record(None, failure='TRANSPORT_ERROR'), 'fallback': kraken_record(None, failure='TRANSPORT_ERROR')}
        with self.assertRaises(uv.ValuationUnavailable) as caught:
            uv.evidence(both_failed, now=str(NOW) + '.5', scan=SCAN, purpose='exit', cfg=CFG)
        self.assertEqual(caught.exception.blockers, (uv.BLOCKER_UNAVAILABLE,))
        self.assertIsInstance(caught.exception, ValueError)                    # callers that catch ValueError keep working

    def test_ordinary_blockers_are_exactly_divergence_and_unavailable(self):
        self.assertEqual(uv.NORMAL_BLOCKERS, frozenset({'USD_SOURCE_DIVERGENCE', 'SOL_USD_UNAVAILABLE'}))

    def test_the_no_entry_vocabulary_accepts_them_and_nothing_near_them(self):
        from desk.paper_cycle_no_entry import producer_blocker_is_normal as normal
        self.assertTrue(normal('USD_SOURCE_DIVERGENCE'))
        self.assertTrue(normal('SOL_USD_UNAVAILABLE'))
        for code in ('SOL_USD_TRUSTED_INPUT_INVALID', 'SOL_USD_SOURCE_OR_EXACT_BLOCK_TIME_MISSING', 'SOL_USD:SOL_USD_UNAVAILABLE',
                     'SOL_USD_UNAVAILABLE ', 'USD_SOURCE_DIVERGENCE:x', 'sol_usd_unavailable'):
            with self.subTest(code):
                self.assertFalse(normal(code))


class CrossCheckConfigTests(unittest.TestCase):
    def test_default_range_and_loader_validation(self):
        self.assertEqual(uv.cross_check_seconds(CFG), 300)
        for good in (1, 60, 300, 3600):
            self.assertEqual(uv.selected({**CFG, uv.KEY_CROSS_CHECK: good}), 2)
        for bad in (0, -1, 3601, True, False, '300', 1.5, None, [300]):
            with self.subTest(bad), self.assertRaises(ValueError):
                uv.selected({**CFG, uv.KEY_CROSS_CHECK: bad})


class RetainedScanTests(unittest.TestCase):
    """The bounded read-only scans over retained attempt originals (schedule, health probe)."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from desk.evidence import EvidenceStore
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = EvidenceStore(Path(self.tmp.name) / 'evidence.sqlite')

    def test_last_kraken_attempt_counts_failures_and_respects_the_window(self):
        self.assertIsNone(uv.last_kraken_attempt_at(self.store.path, NOW, 300))
        self.store.save(kraken_record(None, acquired=NOW - 400, failure='TRANSPORT_ERROR'))
        self.assertIsNone(uv.last_kraken_attempt_at(self.store.path, NOW, 300))      # too old
        self.store.save(kraken_record(None, acquired=NOW - 200, failure='TRANSPORT_ERROR'))
        self.assertEqual(uv.last_kraken_attempt_at(self.store.path, NOW, 300), NOW - 200)   # a failed cross-check counts
        self.store.save(kraken_record(kraken_body(), acquired=NOW - 50))
        self.assertEqual(uv.last_kraken_attempt_at(self.store.path, NOW, 300), NOW - 50)
        self.store.save(jupiter_record(jupiter_body(), observed_at=NOW - 1))        # a Jupiter attempt is not a cross-check
        self.assertEqual(uv.last_kraken_attempt_at(self.store.path, NOW, 300), NOW - 50)
        self.assertIsNone(uv.last_kraken_attempt_at(self.store.path, NOW - 60, 5))   # future attempts are outside the window

    def test_unreadable_and_foreign_pages_are_skipped_and_the_scan_is_bounded(self):
        self.store.save({'kind': 'something_else', 'source_id': kraken.SOURCE, 'observed_at': NOW - 1})
        self.store.save({'kind': 'paper_read_attempt_v1', 'source_id': 'other-source', 'observed_at': NOW - 1})
        self.assertIsNone(uv.last_kraken_attempt_at(self.store.path, NOW, 300))
        self.store.save(kraken_record(None, acquired=NOW - 10, failure='TRANSPORT_ERROR'))
        for i in range(uv.SCAN_PAGES + 5):
            self.store.save({'kind': 'filler', 'i': i})
        self.assertIsNone(uv.last_kraken_attempt_at(self.store.path, NOW, 300))     # pushed out of the newest-pages window

    def test_persistent_auth_failure(self):
        probe = lambda: uv.persistent_auth_failure(self.store.path)
        self.assertIsNone(probe())
        for n, status in enumerate((401, 403, 401)):
            self.store.save(jupiter_record(None, status=status, failure='HTTP_REJECTED', observed_at=NOW - 100 + n, requests_used=3 + n))
        found = probe()
        self.assertEqual((found['consecutive'], found['last_status']), (3, 401))
        self.store.save(jupiter_record(jupiter_body(), observed_at=NOW - 1))        # the newest attempt succeeded: recovered
        self.assertIsNone(probe())

    def test_auth_failure_needs_the_newest_attempts_to_be_rejections(self):
        self.store.save(jupiter_record(None, status=403, failure='HTTP_REJECTED', observed_at=NOW - 30))
        self.store.save(jupiter_record(None, status=403, failure='HTTP_REJECTED', observed_at=NOW - 20, requests_used=4))
        self.assertIsNone(uv.persistent_auth_failure(self.store.path))               # two are not "persistent"
        self.assertEqual(uv.persistent_auth_failure(self.store.path, minimum=2)['consecutive'], 2)
        self.store.save(jupiter_record(None, status=503, failure='HTTP_REJECTED', observed_at=NOW - 10, requests_used=5))
        self.assertIsNone(uv.persistent_auth_failure(self.store.path, minimum=2))   # a 503 is not an authorization failure
        self.store.save(kraken_record(None, acquired=NOW - 5, failure='HTTP_REJECTED', http_status=401))
        self.assertIsNone(uv.persistent_auth_failure(self.store.path, minimum=2))   # Kraken attempts are not counted

    def test_the_probe_never_raises(self):
        self.assertIsNone(uv.persistent_auth_failure('/nonexistent/evidence.sqlite'))


class ExampleConfigTests(unittest.TestCase):
    ROOT = __import__('pathlib').Path(__file__).resolve().parents[1] / 'config' / 'experiments'

    def test_v2_example_loads_with_the_real_loader_and_v1_example_is_untouched(self):
        from desk.model import load_config
        v2 = load_config(self.ROOT / 'paper-jupiter-kraken-fresh.example.json')
        self.assertEqual((v2['paper_usd_valuation_version'], uv.max_divergence(v2)), (2, D("0.01")))
        self.assertEqual(uv.cross_check_seconds(v2), 300)
        v1 = load_config(self.ROOT / 'paper-kraken-fresh.example.json')
        self.assertEqual(v1['paper_usd_valuation_version'], 1)
        self.assertNotIn(uv.KEY_DIVERGENCE, v1)


if __name__ == '__main__':
    unittest.main()
