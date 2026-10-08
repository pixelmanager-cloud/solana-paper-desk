"""Actual local GET dispatch and guarded temporary-DB replay, never scan submission."""
from contextlib import ExitStack
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer

from desk.control_obligations import read_platform_available
from desk.dashboard import handler
from desk.dashboard_diagnostics import DashboardDiagnostics, MAX_RESPONSE_BYTES
from desk.model import digest
from tests import test_control_obligations as obligation_fixtures
from tests.test_cloud_decision_renderer import render

requires_reads = unittest.skipUnless(read_platform_available(), 'Approved Linux LP64 diagnostic read guard required')


class DiagnosticEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root/'research.sqlite'
        self.evidence = self.root/'configured-evidence.sqlite'
        self.scanner = Mock(side_effect=AssertionError('No scans or providers'))
        self.jobs = SimpleNamespace(db=str(self.db), submit=self.scanner, scanner=self.scanner)
        self.server = ThreadingHTTPServer(('127.0.0.1',0), handler(self.jobs,0,evidence_db=self.evidence))
        self.server.RequestHandlerClass = handler(self.jobs,self.server.server_port,evidence_db=self.evidence)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.addCleanup(self.scanner.assert_not_called)
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        self.guards.enter_context(patch('desk.providers.helius_rpc', side_effect=AssertionError('No provider calls')))

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def fixture(self):
        helper = obligation_fixtures.ControlObligationsTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.helper = helper
        self.db.write_bytes(helper.db.read_bytes())
        self.evidence.write_bytes(helper.h.path.read_bytes())
        return helper.head['evidence_hash']

    def get(self,path='/api/evidence-diagnostics?scan_id=multi',headers=None):
        connection = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        try:
            connection.request('GET',path,headers=headers or {})
            response = connection.getresponse()
            raw = response.read()
            self.assertLessEqual(len(raw),MAX_RESPONSE_BYTES)
            self.assertEqual(response.getheader('Cache-Control'),'no-store')
            self.assertEqual(response.getheader('X-Content-Type-Options'),'nosniff')
            return response.status,json.loads(raw)
        finally:
            connection.close()

    def assert_closed(self,result):
        self.assertEqual(result['decision'],'REJECT')
        self.assertIs(result['eligible_for_trading'],False)

    @requires_reads
    def test_actual_configured_head_replays_without_writes_or_new_requests(self):
        revision = self.fixture()
        before = {p.name:p.read_bytes() for p in self.root.iterdir()}
        calls = list(self.helper.h.calls)
        # A sibling default DB must never displace the server-selected source.
        (self.root/'evidence.sqlite').write_text('untrusted alternate fixture')
        before['evidence.sqlite'] = (self.root/'evidence.sqlite').read_bytes()
        with patch('desk.dashboard_diagnostics.time.time',return_value=1000):
            status,result = self.get()
            self.assertEqual((status,result['status']),(200,'AVAILABLE'))
            self.assertEqual(result,self.get()[1])
        self.assert_closed(result)
        self.assertEqual(result['provider_calls'],0)
        projection = result['diagnostics']
        self.assertEqual(projection['revision_hash'],revision)
        self.assertEqual((projection['observed_at'],projection['evaluated_at']),(120,1000))
        inventory = projection['control_obligations']
        self.assertEqual(inventory['obligations']['endpoint_controls']['status'],'OBSERVED_COMPONENT')
        self.assertEqual(inventory['obligations']['historical_accounting']['status'],'RECONCILED_COMPONENT')
        self.assertEqual(inventory['requests_used'],13)
        self.assertTrue(all(item['status']=='UNRESOLVED' for key,item in inventory['obligations'].items()
                            if key not in ('endpoint_controls','historical_accounting')))
        self.assertIn('LIVE_FEATURE_ADAPTER_NOT_READY',projection['reasons'])
        unsigned = dict(projection); unsigned.pop('manifest_hash')
        self.assertEqual(projection['manifest_hash'],digest(unsigned))
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir()})
        self.assertEqual(calls,self.helper.h.calls)
        self.assertFalse((self.root/'paper-decisions.sqlite').exists())

    def test_query_cannot_supply_paths_flags_payloads_revisions_or_duplicate_ids(self):
        attacks = ['', 'scan_id=', 'scan_id=multi&scan_id=other', 'scan_id=multi&revision_hash='+'a'*64,
                   'scan_id=multi&evidence_db=/tmp/evil', 'scan_id=multi&safe=true',
                   'scan_id=multi&source=%7B%7D','scan_id=%2Ftmp%2Fevil','scan_id=x%00',
                   'scan_id='+('x'*129),'scan_id='+('x'*600),'scan_id=multi#ignored']
        with patch('desk.dashboard_diagnostics.candidate_snapshot',side_effect=AssertionError('Invalid request must not replay')):
            for query in attacks:
                with self.subTest(query=query):
                    status,result = self.get('/api/evidence-diagnostics?'+query)
                    self.assertEqual(status,400)
                    self.assert_closed(result)
                    self.assertIsNone(result['diagnostics'])
        self.assertEqual(list(self.root.iterdir()),[])

    def test_foreign_host_and_origin_cannot_inspect_or_trigger_replay(self):
        with patch('desk.dashboard_diagnostics.candidate_snapshot',side_effect=AssertionError('Untrusted request must not replay')):
            for headers in ({'Host':'evil.test'},{'Origin':'https://evil.test'},{'Origin':'null'}):
                self.assertEqual(self.get(headers=headers),(403,{'error':'Local access only'}))
        self.assertEqual(list(self.root.iterdir()),[])

    def test_missing_databases_and_unsupported_platform_are_explicit_without_creation(self):
        status,result = self.get()
        self.assertEqual((status,result['status']),(503,'UNAVAILABLE'))
        self.assert_closed(result)
        self.assertIsNone(result['diagnostics'])
        self.assertNotIn('revision_hash',result)
        with patch('desk.control_obligations.sys.platform','darwin'), patch('sqlite3.connect',side_effect=AssertionError('No unsupported read')):
            status,result = self.get()
            self.assertIn('DIAGNOSTIC_READ_PLATFORM_UNAVAILABLE',result['reasons'])
        self.assertEqual(list(self.root.iterdir()),[])

    @requires_reads
    def test_missing_malformed_and_duplicate_heads_never_synthesize_revision(self):
        self.fixture()
        with sqlite3.connect(self.evidence) as c:
            c.execute('DELETE FROM ownership_heads')
        self.assertIn('OWNERSHIP_HEAD_MISSING',self.get()[1]['reasons'])
        for heads in (['invalid'],['a'*64,'b'*64]):
            with self.subTest(heads=heads):
                with sqlite3.connect(self.evidence) as c:
                    c.execute('DROP TABLE ownership_heads')
                    c.execute('CREATE TABLE ownership_heads(scan_id TEXT,evidence_hash TEXT)')
                    c.executemany('INSERT INTO ownership_heads VALUES(?,?)',[('multi',key) for key in heads])
                status,result = self.get()
                self.assertEqual(status,503)
                self.assertIn('OWNERSHIP_HEAD_INVALID',result['reasons'])
                self.assertNotIn('revision_hash',result)
        with sqlite3.connect(self.evidence) as c:
            c.execute('DROP TABLE ownership_heads')
        self.assertEqual(self.get()[0],503)
        self.assertIn('DIAGNOSTIC_READ_LAYOUT_REPLAY_OR_OUTPUT_UNAVAILABLE',self.get()[1]['reasons'])

    @requires_reads
    def test_writer_contention_and_wal_layout_are_unavailable_without_repair(self):
        self.fixture()
        writer = sqlite3.connect(self.evidence)
        try:
            writer.execute('BEGIN IMMEDIATE')
            status,result = self.get()
            self.assertEqual(status,503)
            self.assertIn('DIAGNOSTIC_READ_CONTENDED',result['reasons'])
        finally:
            writer.rollback(); writer.close()
        with sqlite3.connect(self.evidence) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
        before = {p.name:p.read_bytes() for p in self.root.iterdir()}
        status,result = self.get()
        self.assertEqual((status,result['status']),(503,'UNAVAILABLE'))
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir()})

    @requires_reads
    def test_output_ceiling_withholds_entire_projection_not_partial_hashes(self):
        revision = self.fixture()
        with patch('desk.dashboard_diagnostics.MAX_RESPONSE_BYTES',4096):
            status,result = self.get()
        self.assertEqual((status,result['status']),(503,'UNAVAILABLE'))
        self.assertEqual(result['revision_hash'],revision)
        self.assertIsNone(result['diagnostics'])
        self.assertNotIn('manifest_hash',result)

    @requires_reads
    def test_nonblocking_single_flight_returns_busy_and_releases_after_error(self):
        self.fixture()
        started,release = threading.Event(),threading.Event()
        first=[]
        def expensive(*args,**kwargs):
            started.set()
            if not release.wait(5):raise AssertionError('Fixture replay not released')
            raise ValueError('SENSITIVE_FIXTURE_SENTINEL')
        with patch('desk.dashboard_diagnostics.candidate_snapshot',side_effect=expensive):
            worker=threading.Thread(target=lambda:first.append(self.get()))
            worker.start()
            try:
                self.assertTrue(started.wait(3))
                status,result=self.get()
                self.assertEqual((status,result['status']),(409,'BUSY'))
                self.assertTrue(worker.is_alive())
                # The head lookup guard remains held through the replay call.
                with sqlite3.connect(self.evidence,timeout=0) as writer:
                    with self.assertRaises(sqlite3.OperationalError):
                        writer.execute('BEGIN IMMEDIATE')
                self.assert_closed(result)
                self.assertIsNone(result['diagnostics'])
            finally:
                release.set();worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(first[0][0],503)
        self.assertNotIn('SENSITIVE_FIXTURE_SENTINEL',json.dumps(first))
        self.assertEqual(self.get()[0],200)


class DiagnosticRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper=obligation_fixtures.ControlObligationsTests()
        cls.helper.setUp()
        cls.addClassCleanup(cls.helper.doCleanups)

    def fixture(self):
        h=self.helper
        _,body=DashboardDiagnostics(h.db,h.h.path).inspect('multi')
        return json.loads(body)

    def dom(self,data=None,**options):
        return render(scans_data=[{'id':'multi','mint':'SYNTHETIC_DIAGNOSTIC','created':120,'status':'COMPLETE',
                                  'result':{'observed_at':120,'findings':[],'unknowns':[]}}],
                      diagnostics_data=data,now_ms=1000000,**options)

    def test_inspection_is_user_triggered_no_periodic_replay_or_control_change(self):
        result=self.dom()
        self.assertIn('Inspect saved evidence diagnostics',result['scans_html'])
        self.assertFalse(any('/api/evidence-diagnostics' in call['url'] for call in result['calls']))
        self.assertEqual(result['controls'],{'submit_listeners':1,'refresh_listeners':1})
        self.assertEqual(sorted(result['intervals']),[5000,15000,15000,30000])

    @requires_reads
    def test_real_projection_separates_components_obligations_age_and_exact_hashes(self):
        with patch('desk.dashboard_diagnostics.time.time',return_value=990):
            data=self.fixture()
        result=self.dom(data,inspect_scan=True,double_inspect=True,refresh_after_inspection=True)
        html=result['scans_html']
        value=data['diagnostics']
        for text in ('Historical evidence inspection · entry remains REJECT',
                     'Observed endpoint controls · OBSERVED_COMPONENT',
                     'Reconciled historical accounting · RECONCILED_COMPONENT',
                     'Unresolved historical control and authentication obligations',
                     'historical_controls · UNRESOLVED','source_authentication · UNRESOLVED',
                     'Observation age at inspection display: 880 seconds',
                     'LIVE_FEATURE_ADAPTER_NOT_READY','Persisted investigation requests: 13/18',
                     'Hashes bind local records','does not continuously certify current evidence health'):
            self.assertIn(text,html)
        self.assertIn('Canonical ownership revision: '+value['revision_hash'],html)
        self.assertIn('Original source hash: '+value['source_hash'],html)
        self.assertIn('Diagnostic manifest hash: '+value['manifest_hash'],html)
        calls=[call for call in result['calls'] if '/api/evidence-diagnostics' in call['url']]
        self.assertEqual(calls,[{'url':'/api/evidence-diagnostics?scan_id=multi','method':'GET'}])

    def test_busy_missing_head_fetch_error_and_forged_promotion_fail_closed(self):
        for data,text in (({'status':'BUSY'},'replay is busy'),
                          ({'status':'UNAVAILABLE','reasons':['OWNERSHIP_HEAD_MISSING']},'OWNERSHIP_HEAD_MISSING'),
                          ({'status':'AVAILABLE','diagnostics':{'schema':'candidate_snapshot_diagnostic_v1',
                            'read_status':'AVAILABLE','decision':'APPROVE','eligible_for_trading':True}},'unavailable')):
            with self.subTest(data=data):
                html=self.dom(data,inspect_scan=True,diagnostic_http_ok=False)['scans_html']
                self.assertIn(text,html)
                self.assertIn('REJECT',html)
                self.assertNotIn('OBSERVED_COMPONENT',html)
        html=self.dom(inspect_scan=True,diagnostic_fetch_error=True)['scans_html']
        self.assertIn('Evidence diagnostics unavailable.',html)

    @requires_reads
    def test_untrusted_diagnostic_scope_accounts_and_reasons_remain_text(self):
        data=self.fixture()
        attack='<img src=x onerror="globalThis.executed=true">'
        data['diagnostics']['scope']=attack
        data['diagnostics']['reasons']=[attack]
        data['diagnostics']['control_obligations']['obligations']['endpoint_controls']['accounts'][0]['account']=attack
        html=self.dom(data,inspect_scan=True)['scans_html']
        self.assertNotIn('<img',html)
        self.assertIn('&lt;img',html)
        self.assertIn('entry remains REJECT',html)


if __name__=='__main__':
    unittest.main()
