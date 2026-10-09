"""Persisted PR103-compatible initialization, not a runner-process heartbeat."""
import json
import http.client
import threading
from http.server import ThreadingHTTPServer
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desk.dashboard import Jobs, handler
from desk.engine import initial_state, transition
from desk.ledger import Ledger
from desk.model import canonical, digest
from desk.paper_view import paper_status
from tests.helpers import config, event, T
from tests.test_cloud_decision_renderer import render

INIT={'schema_version':1,'event_id':'paper-runner:init','ts':0,'kind':'clock','actor':'paper_monitor'}


class PaperLoopVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'active-paper.sqlite';self.cfg=config()
        self.ledger=Ledger(self.path);self.addCleanup(lambda:self.ledger.close())

    def initialize(self):
        def bootstrap(state,e,cfg):
            self.ledger.db.execute('INSERT INTO metadata VALUES(?,?)',('paper_runner','SYNTHETIC_TEST_ONLY'))
            return transition(state,e,cfg)
        self.ledger.apply(INIT,self.cfg,bootstrap,initial_state)

    def apply(self,e):self.ledger.apply(e,self.cfg,transition,initial_state)

    def view(self,now=T):
        with patch('socket.socket',side_effect=AssertionError('No providers')):
            return paper_status(self.path,now=now)

    def test_initialized_synthetic_cash_is_saved_not_alive_or_live(self):
        self.initialize();r=self.view(0)
        self.assertEqual(r['status'],'LEDGER_PRESENT')
        self.assertEqual(r['runner_status'],'SYNTHETIC_INITIALIZED')
        self.assertEqual(r['runner_provenance'],'SYNTHETIC_TEST_ONLY')
        self.assertEqual(r['cash_sol'],self.cfg['initial_equity_sol'])
        self.assertIsNone(r['last_run_evidence']);self.assertIsNone(r['last_market_at'])
        self.assertEqual(r['runner_liveness'],'UNKNOWN')
        self.assertFalse(r['automatic_entry_enabled'])

    def test_saved_market_activity_age_hash_positions_outcomes_and_restart(self):
        self.initialize();self.apply(event())
        self.ledger.close();self.ledger=Ledger(self.path,must_exist=True)
        r=self.view(T+1)
        self.assertEqual(r['runner_status'],'SYNTHETIC_CHECKPOINT_RECORDED')
        self.assertEqual(r['last_event_age_seconds'],1)
        self.assertEqual(r['last_market_age_seconds'],1)
        self.assertEqual(r['last_market_currentness'],'RECENT_SAVED_OBSERVATION')
        self.assertEqual(r['last_run_evidence']['payload_hash'],digest(event()))
        self.assertEqual(len(r['positions']),1);self.assertTrue(r['recent_outcomes'])
        self.assertEqual(r['runner_liveness'],'UNKNOWN')
        stale=self.view(T+11);self.assertEqual(stale['last_market_currentness'],'STALE')
        self.assertIsNone(stale['estimated_equity_sol'])
        future=self.view(T-1);self.assertEqual(future['last_market_currentness'],'FUTURE')
        self.assertIsNone(future['estimated_equity_sol'])

    def test_plain_ledger_is_not_adopted_as_runner(self):
        self.apply(event());r=self.view()
        self.assertEqual(r['runner_status'],'NOT_CONNECTED')
        self.assertIsNone(r['runner_provenance']);self.assertIsNone(r['last_run_evidence'])
        self.assertEqual(r['last_market_at'],T)

    def test_marker_only_unknown_live_and_conflicting_market_fail_closed(self):
        self.apply(event())
        self.ledger.db.execute('INSERT INTO metadata VALUES(?,?)',('paper_runner','SYNTHETIC_TEST_ONLY'))
        r=self.view();self.assertEqual(r['status'],'RECOVERY_REQUIRED');self.assertIsNone(r['cash_sol'])
        self.ledger.db.execute("UPDATE metadata SET value='LIVE_PAPER' WHERE key='paper_runner'")
        r=self.view();self.assertEqual(r['status'],'RECOVERY_REQUIRED');self.assertFalse(r['automatic_entry_enabled'])
        self.ledger.close()
        self.path=Path(self.tmp.name)/'proper.sqlite';self.ledger=Ledger(self.path)
        self.initialize();self.apply(event(provenance='MAINNET_OBSERVATION'))
        self.assertEqual(self.view()['status'],'RECOVERY_REQUIRED')

    def test_mismatched_code_config_missing_checkpoint_and_substituted_event_redact(self):
        self.initialize();self.apply(event());original=list(self.ledger.db.iterdump())
        attacks=[("UPDATE metadata SET value=? WHERE key='implementation_hash'",('0'*64,)),
                 ("UPDATE events SET payload_hash=? WHERE event_id<>'paper-runner:init'",('0'*64,)),
                 ('DELETE FROM state',())]
        for index,(sql,args) in enumerate(attacks):
            target=Path(self.tmp.name)/('attack'+str(index)+'.sqlite')
            with sqlite3.connect(target) as c:self.ledger.db.backup(c);c.execute(sql,args);c.commit()
            r=paper_status(target,now=T)
            self.assertEqual(r['status'],'RECOVERY_REQUIRED');self.assertIsNone(r['cash_sol'])
            self.assertEqual(r['positions'],[]);self.assertEqual(r['recent_outcomes'],[])
        wrong={**self.cfg,'initial_equity_sol':'6'}
        self.assertEqual(paper_status(self.path,now=T,expected_config=wrong)['status'],'RECOVERY_REQUIRED')
        self.assertEqual(original,list(self.ledger.db.iterdump()))

    def test_bounded_history_and_read_only_original_records(self):
        self.initialize();self.apply(event());before=list(self.ledger.db.iterdump())
        r=self.view()
        self.assertEqual(before,list(self.ledger.db.iterdump()))
        oversized={**INIT,'padding':'x'*(256*1024)}
        self.ledger.db.execute('UPDATE events SET payload=?,payload_hash=? WHERE event_id=?',
                               (canonical(oversized),digest(oversized),INIT['event_id']))
        self.ledger.db.commit()
        blocked=self.view()
        self.assertEqual(blocked['status'],'RECOVERY_REQUIRED')
        self.assertEqual(blocked['recovery_reason'],'RUNNER_HISTORY_LIMIT')
        self.assertIsNone(blocked['cash_sol'])
        self.assertEqual(r['status'],'LEDGER_PRESENT')

    def test_synthetic_renderer_ages_liveness_and_event_identity_escape(self):
        self.initialize();malicious=event();malicious['event_id']='<img src=x onerror=attack()>'
        self.apply(malicious);r=self.view(T+1)
        html=render(paper_data=r,now_ms=(T+1)*1000)['paper_html']
        self.assertIn('SYNTHETIC_TEST_ONLY runner experiment',html)
        self.assertIn('Runner process liveness: unknown',html)
        self.assertIn('Last saved event age: 1 seconds',html)
        self.assertIn('Last saved market observation:',html)
        self.assertIn('Automatic live paper entries remain disabled.',html)
        self.assertIn('&lt;img',html);self.assertNotIn('<img',html)
        bad={**r,'status':'RECOVERY_REQUIRED'}
        html=render(paper_data=bad,now_ms=T*1000)['paper_html']
        self.assertNotIn('Simulated cash:',html)
        self.assertNotIn('SYNTHETIC_TEST_ONLY runner experiment',html)

    def test_existing_api_presents_saved_synthetic_activity_without_writes(self):
        self.initialize();self.apply(event());before=list(self.ledger.db.iterdump())
        jobs=Jobs(Path(self.tmp.name)/'research.sqlite',scanner=lambda *a,**kw:self.fail('No scans'))
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(jobs,0))
        server.RequestHandlerClass=handler(jobs,server.server_port)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
        try:
            connection.request('GET','/api/paper');response=connection.getresponse()
            r=json.loads(response.read())
            self.assertEqual(response.status,200)
            self.assertEqual(response.getheader('Cache-Control'),'no-store')
            self.assertEqual(r['runner_status'],'SYNTHETIC_CHECKPOINT_RECORDED')
            self.assertEqual(r['runner_liveness'],'UNKNOWN')
            self.assertFalse(r['automatic_entry_enabled'])
            self.assertEqual(r['positions'][0]['mint'],event()['mint'])
            self.assertEqual(r['last_market_currentness'],'STALE')
            self.assertEqual(before,list(self.ledger.db.iterdump()))
        finally:
            connection.close();server.shutdown();server.server_close();thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
