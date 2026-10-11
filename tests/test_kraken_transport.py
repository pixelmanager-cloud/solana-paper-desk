"""Real persisted admissions/pacing with synthetic HTTP only; no credentials."""
import base64
from contextlib import ExitStack
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from desk import paper_read_sources as reads, kraken_usd_observation as usd, provider_pacing as pace, kraken_pacing_migration as migration
from desk.model import canonical
from tests import test_paper_read_sources as fixtures
from tests.test_kraken_usd import RAW
from tests.test_provider_pacing import Clock

class KrakenTransportTests(unittest.TestCase):
    def setUp(self):
        self.h=fixtures.PaperReadTests();self.addCleanup(self.h.doCleanups);self.h.setUp()
        self.clock=Clock();self.clock.wall=1002.220369
        self.path=Path(self.h.temp.name).resolve()/'pacing.sqlite';pace.initialize(self.path)
        self.policy=self.path.parent/'migration.json';self.policy.write_text(canonical({'version':1,'pins':[]}))
        p=patch.object(migration,'POLICY',self.policy);p.start();self.addCleanup(p.stop)
        self.policy.write_text(canonical({'version':1,'pins':[migration.review_plan(self.path)]}));migration.migrate(self.path)
    def call(self,raw=RAW,exception=None,status=200):
        outer=self
        class Opener:
            def open(self,request,*,timeout):
                outer.assertEqual(outer.h.progress.admission('scan')['requests_used'],outer.before+1)
                outer.assertEqual(request.full_url,usd.URL);outer.assertEqual(request.method,'GET')
                outer.assertIsNone(request.data);outer.assertIsNone(request.get_header('X-api-key'))
                with sqlite3.connect(outer.path) as c:outer.assertIsNotNone(c.execute("SELECT pending FROM state WHERE provider='kraken'").fetchone()[0])
                if exception:raise exception
                response=fixtures.Response(raw);response.status=status;return response
        self.before=self.h.progress.admission('scan')['requests_used']
        with ExitStack() as stack:
            stack.enter_context(patch.object(reads.os.environ,'get',side_effect=AssertionError('no credential lookup')))
            stack.enter_context(patch.object(reads,'build_opener',return_value=Opener()))
            stack.enter_context(patch.object(pace,'configured',side_effect=lambda **kw:pace.Pacer(self.path,clock=self.clock.time,monotonic=self.clock.monotonic,sleep=self.clock.sleep,**kw)))
            stack.enter_context(patch.object(reads.time,'time',side_effect=self.clock.time))
            stack.enter_context(patch.object(reads.time,'monotonic',side_effect=self.clock.monotonic))
            return self.h.source.kraken_sol_price(timeout_seconds=5)
    def test_raw_timestamp_fraction_no_key_and_consecutive_shared_cadence(self):
        first=self.call();record=self.h.store.load(first['evidence_hash']);self.assertEqual(base64.b64decode(record['response_bytes_base64']),RAW)
        self.assertEqual(record['acquired_at_decimal'],'1002.220369');self.assertEqual(record['source_id'],usd.SOURCE)
        second=self.call();self.assertGreaterEqual(self.clock.wall,1004.220369)
        self.assertEqual(self.h.progress.admission('scan')['requests_used'],2)
        self.assertNotEqual(first['evidence_hash'],second['evidence_hash'])
    def test_http429_and_provider_error_retain_charge_raw_and_backoff(self):
        for raw,status,code in [(RAW,429,'HTTP_REJECTED'),(b'{"error":["EAPI:Rate limit exceeded"]}',200,'KRAKEN_PROVIDER_ERROR')]:
            with self.subTest(status=status):
                with self.assertRaises(reads.PaperReadError) as caught:self.call(raw,status=status)
                self.assertEqual(caught.exception.code,code)
                record=self.h.store.load(caught.exception.evidence_hash);self.assertEqual(record['requests_used'],self.before+1)
                with sqlite3.connect(self.path) as c:
                    row=c.execute("SELECT blocked_until,pending FROM state WHERE provider='kraken'").fetchone();self.assertIsNone(row[1]);self.assertGreaterEqual(row[0],self.clock.wall+30)
                if status==200:self.assertEqual(base64.b64decode(record['response_bytes_base64']),raw)
                self.clock.wall+=31;self.clock.tick+=31
    def test_ambiguous_missing_pacer_and_bodyless_http_error_preserve_failure(self):
        with self.assertRaises(reads.PaperReadError) as caught:self.call(exception=HTTPError(usd.URL,403,'synthetic',{},None))
        record=self.h.store.load(caught.exception.evidence_hash);self.assertEqual(record['http_status'],403);self.assertIsNone(record['response_bytes_base64']);self.assertIsNone(record['acquired_at_decimal'])
        with patch.object(pace,'configured',return_value=None),patch.object(reads,'build_opener',side_effect=AssertionError('no HTTP without pacing')):
            with self.assertRaises(reads.PaperReadError) as caught:self.h.source.kraken_sol_price(timeout_seconds=5)
        self.assertEqual(caught.exception.code,'KRAKEN_PACING_NOT_CONFIGURED');self.assertEqual(self.h.progress.admission('scan')['requests_used'],2)
    def test_invalid_provider_shapes_nonpositive_future_stale_and_oversize_retain_failure(self):
        for raw in [RAW.replace(b'109.08000',b'0.00000'),RAW.replace(b'1000.2493582',b'900.2493582'),RAW.replace(b'1000.2493582',b'9999.2493582'),b'x'*65537,RAW.replace(b'"error":[]',b'"error":[],"error":[]')]:
            with self.subTest(raw=raw[:70]),self.assertRaises(reads.PaperReadError) as caught:self.call(raw)
            record=self.h.store.load(caught.exception.evidence_hash);self.assertIsNotNone(record['failure_code']);self.assertEqual(record['requests_used'],self.before+1)

    def test_retained_kraken_ref_reparse_without_io_and_legacy_triple_unchanged(self):
        from types import SimpleNamespace
        from desk.paper_cycle import _usd,CycleBlocked
        fresh=self.call();refs=(fresh['evidence_hash'],)
        budget=SimpleNamespace(wall_clock=lambda:self.clock.wall,call=lambda *_:(_ for _ in ()).throw(AssertionError('retained ref must not call HTTP')))
        result=_usd(self.h.progress,self.h.source,'scan',budget,refs,valuation_version=1)
        self.assertEqual(result[-1],refs)
        self.clock.wall+=31
        with self.assertRaises(ValueError):_usd(self.h.progress,self.h.source,'scan',budget,refs,valuation_version=1)
        with self.assertRaises(CycleBlocked):_usd(self.h.progress,self.h.source,'scan',budget,refs)
