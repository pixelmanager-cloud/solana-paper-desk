"""Actual PR111 reproductions and pre-materialization read guards, fixture-only."""
import json
import sqlite3
import unittest
from unittest.mock import patch

from desk import paper_view
from desk.model import canonical,digest
from desk.paper_runner import FixtureAdapter,initialize,run_once
from tests.helpers import config,event,T
from tests import test_paper_loop_visibility as fixtures


class PaperLoopVisibilityRepairTests(unittest.TestCase):
    # Reuse fixture setup only; the prior behavior tests run once in their module.
    setUp=fixtures.PaperLoopVisibilityTests.setUp
    initialize=fixtures.PaperLoopVisibilityTests.initialize
    apply=fixtures.PaperLoopVisibilityTests.apply
    view=fixtures.PaperLoopVisibilityTests.view
    def replace_event(self,payload,key=None):
        decoded=json.loads(payload)
        self.ledger.db.execute('UPDATE events SET payload=?,payload_hash=? WHERE event_id=?',
                               (payload,digest(decoded) if key is None else key,event()['event_id']))
        self.ledger.db.commit()

    def test_rehashed_malformed_event_grammar_refuses_saved_activity(self):
        self.initialize();self.apply(event())
        for change in ({'kind':'bogus'},{'schema_version':True},{'danger':None},
                       {'price_at':T+1},{'reserve_sol':'NaN'},{'kind':'control','actor':'candidate'},
                       {'kind':'clock','actor':'operator'}):
            with self.subTest(change=change):
                self.replace_event(canonical(event()|change));before=list(self.ledger.db.iterdump())
                out=self.view()
                self.assertEqual(out['status'],'RECOVERY_REQUIRED')
                self.assertEqual(out['recovery_reason'],'RUNNER_EVENT_INVALID')
                self.assertIsNone(out['cash_sol']);self.assertEqual(out['positions'],[])
                self.assertEqual(out['runner_liveness'],'UNKNOWN')
                self.assertFalse(out['automatic_entry_enabled'])
                self.assertEqual(before,list(self.ledger.db.iterdump()))
        malformed={'event_id':event()['event_id'],'ts':T,'kind':'market','provenance':'SYNTHETIC_TEST_ONLY'}
        self.replace_event(canonical(malformed))
        self.assertEqual(self.view()['recovery_reason'],'RUNNER_EVENT_INVALID')

    def test_duplicate_json_field_is_not_laundered_by_rehashing(self):
        self.initialize();self.apply(event())
        payload=canonical(event())[:-1]+',"kind":"market"}'
        self.replace_event(payload)
        self.assertEqual(self.view()['recovery_reason'],'RUNNER_EVENT_INVALID')

    def test_valid_custom_config_actual_runner_projection_and_expected_config(self):
        path=self.path.parent/'custom.sqlite';cfg={**config(),'initial_equity_sol':'6','price_ttl_seconds':20}
        init=initialize(path,cfg)
        self.assertEqual(init['status'],'LEDGER_PRESENT');self.assertEqual(init['cash_sol'],'6')
        fixture=FixtureAdapter({'provenance':'SYNTHETIC_TEST_ONLY','events':[event()]})
        out=run_once(path,cfg,fixture,now=T)
        self.assertEqual(out['status'],'COMPLETE');self.assertEqual(out['paper']['status'],'LEDGER_PRESENT')
        self.assertEqual(out['paper']['runner_status'],'SYNTHETIC_CHECKPOINT_RECORDED')
        self.assertEqual(out['paper']['runner_liveness'],'UNKNOWN')
        self.assertFalse(out['paper']['automatic_entry_enabled'])
        self.assertEqual(paper_view.paper_status(path,now=T+15)['valuation_status'],'MODEL_ESTIMATE')
        self.assertEqual(paper_view.paper_status(path,now=T+21)['valuation_status'],'STALE_OR_EXIT_UNVERIFIED')
        self.assertEqual(paper_view.paper_status(path,now=T,expected_config=cfg)['status'],'LEDGER_PRESENT')
        self.assertEqual(paper_view.paper_status(path,now=T,expected_config=config())['status'],'RECOVERY_REQUIRED')

    def assert_history_rejected_before_load(self):
        real=sqlite3.connect;loads=[]
        class Guarded(sqlite3.Connection):
            def execute(connection,sql,*args):
                if sql.startswith('SELECT seq,event_id,ts,payload,payload_hash FROM events'):
                    loads.append(sql);raise AssertionError('Unbounded event payload materialized')
                return super().execute(sql,*args)
        before=list(self.ledger.db.iterdump())
        with patch.object(paper_view.sqlite3,'connect',side_effect=lambda *a,**k:real(*a,**{**k,'factory':Guarded})):
            out=self.view()
        self.assertEqual(out['status'],'RECOVERY_REQUIRED')
        self.assertEqual(out['recovery_reason'],'RUNNER_HISTORY_LIMIT')
        self.assertEqual(loads,[]);self.assertEqual(before,list(self.ledger.db.iterdump()))

    def test_large_utf8_or_wrong_sql_type_refuses_before_payload_load(self):
        self.initialize();self.apply(event())
        for payload in ('x'*(1024*1024),'é'*(128*1024+1),b'{}'):
            with self.subTest(type=type(payload).__name__):
                self.ledger.db.execute('UPDATE events SET payload=? WHERE event_id=?',(payload,event()['event_id']))
                self.ledger.db.commit();self.assert_history_rejected_before_load()

    def test_whole_history_aggregate_refuses_before_any_payload_load(self):
        self.initialize()
        rows=[(f'bulk:{i}',0,'x'*(130*1024),'0'*64) for i in range(127)]
        self.ledger.db.executemany('INSERT INTO events(event_id,ts,payload,payload_hash) VALUES(?,?,?,?)',rows)
        self.ledger.db.commit();self.assert_history_rejected_before_load()

    def test_whole_history_count_refuses_before_any_payload_load(self):
        self.initialize()
        rows=[(f'bulk:{i}',0,'{}','0'*64) for i in range(10000)]
        self.ledger.db.executemany('INSERT INTO events(event_id,ts,payload,payload_hash) VALUES(?,?,?,?)',rows)
        self.ledger.db.commit();self.assert_history_rejected_before_load()

    def test_exact_history_count_accepts_valid_saved_clocks_without_liveness(self):
        self.initialize()
        rows=[]
        for i in range(9999):
            e={'schema_version':1,'event_id':f'bounded-clock:{i}','ts':0,
               'kind':'clock','actor':'paper_monitor'}
            rows.append((e['event_id'],0,canonical(e),digest(e)))
        self.ledger.db.executemany('INSERT INTO events(event_id,ts,payload,payload_hash) VALUES(?,?,?,?)',rows)
        self.ledger.db.commit()
        out=self.view(0)
        self.assertEqual(out['status'],'LEDGER_PRESENT')
        self.assertEqual(out['runner_liveness'],'UNKNOWN')
        self.assertFalse(out['automatic_entry_enabled'])

    def test_exact_event_utf8_byte_cap_and_original_payload_preserved(self):
        self.initialize();self.apply(event())
        record=event(padding='');record['padding']='x'*(256*1024-len(canonical(record).encode()))
        raw=canonical(record);self.assertEqual(len(raw.encode()),256*1024)
        self.replace_event(raw);before=list(self.ledger.db.iterdump())
        self.assertEqual(self.view()['status'],'LEDGER_PRESENT')
        self.assertEqual(before,list(self.ledger.db.iterdump()))
        record['padding']+='x';self.replace_event(canonical(record))
        self.assert_history_rejected_before_load()

    def test_future_live_marker_does_not_select_a_profile_from_event_flags(self):
        self.initialize();self.apply(event(experimental_profile='LIVE_PAPER',eligible_for_trading=True))
        self.assertEqual(self.view()['runner_provenance'],'SYNTHETIC_TEST_ONLY')
        self.ledger.db.execute("UPDATE metadata SET value='LIVE_PAPER' WHERE key='paper_runner'")
        self.ledger.db.commit()
        self.assertEqual(self.view()['recovery_reason'],'RUNNER_MARKER_UNSUPPORTED')
