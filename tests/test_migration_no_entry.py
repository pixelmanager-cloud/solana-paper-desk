"""Synthetic actual acquisition/intake wires; no providers or approval mocks."""
import copy
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch
from tests import test_paper_entry_dispatcher as fixture
from tools import paper_entry_dispatcher as tool
from desk import paper_migration_no_entry as rejection, paper_terminal_reconciliation as terminal
from desk.model import canonical,digest
from desk.programs import unbase58
from desk.security import base58

class MigrationNoEntryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.DispatcherTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.seeds=0
        original=self.f.setup_rpc
        def rpc(method,params):
            if method!='getTransactionsForAddress':return original(method,params)
            self.f.calls.append(method);self.seeds+=1
            return {'data':[],'paginationToken':'seed-next' if self.seeds==1 else None}
        self.f.setup_rpc=rpc
    def quote(self,key):
        event=self.f.raw['meta']['innerInstructions'][0]['instructions'][0]
        event['data']=base58(unbase58(event['data'])[:-32]+unbase58(key))
    def reject(self):
        self.quote(base58(bytes([33])*32))
        return self.f.live()
    def test_exact_unsupported_quote_records_skips_and_replays_without_more_calls(self):
        before=self.f.ledger.read_bytes();result=self.reject()
        self.assertEqual(result['paper_status'],'NO_ENTRY')
        self.assertEqual(self.f.count('intents'),1);self.assertEqual(self.f.count('results'),1)
        self.assertEqual(self.f.ledger.read_bytes(),before)
        self.assertEqual(self.f.f.progress.admission(result['scan_id'])['requests_used'],5)
        store=self.f.f.progress.store
        with closing(store.connect()) as c:
            records=rejection.rows(c)
        self.assertEqual(records[0]['reason'],'UNSUPPORTED_MIGRATION_EVENT_QUOTE')
        self.assertEqual(records[0]['canonical_acquisition']['evidence_class'],'CANONICAL_METHOD_PARAMS_RESULTS_NOT_ORIGINAL_WIRE')
        self.assertEqual(len(records[0]['wire_attempt_refs']),1)
        with tool._journal(self.f.journal) as c:tool._validate(c,self.f.ctx)
        self.assertEqual(terminal.gate(store,self.f.f.jobs.path,(result['scan_id'],)),'REJECTED_SCAN_RETIRED')
        calls=len(self.f.calls);self.assertEqual(self.f.invoke()['status'],'NO_CANDIDATE');self.assertEqual(len(self.f.calls),calls)
    def test_witness_absence_alone_remains_unresolved(self):
        self.f.raw['meta']['innerInstructions']=[]
        with self.assertRaises(ValueError):self.f.live()
        self.assertEqual(self.f.count('intents'),1);self.assertEqual(self.f.count('results'),0)
        with self.assertRaises(ValueError):self.f.invoke()
    def test_generic_non_quote_binding_mismatch_stays_unresolved(self):
        self.quote(base58(bytes([33])*32))
        event=self.f.raw['meta']['innerInstructions'][0]['instructions'][0]
        data=bytearray(unbase58(event['data']));data[16:48]=bytes([42])*32;event['data']=base58(data)
        with self.assertRaises(ValueError):self.f.live()
        self.assertEqual(self.f.count('results'),0)
    def test_legacy_sentinel_is_not_an_automatic_recovery(self):
        self.quote('11111111111111111111111111111111')
        actual_intake=tool.migration.intake
        def captured_then_rejection(*args,**kwargs):
            # Capture with the current parser, then explicitly exercise the
            # rejection publisher. Corrected legacy intake is not a rejection.
            actual_intake(*args,**kwargs)
            with closing(sqlite3.connect(self.f.journal)) as c:
                identity=c.execute('SELECT id FROM intents').fetchone()[0]
                return rejection.publish(c,self.f.ctx,identity,kwargs['scan_id'])
        with patch.object(tool.migration,'intake',side_effect=captured_then_rejection):
            with self.assertRaises(ValueError):self.f.live()
        self.assertEqual(self.f.count('intents'),1)
        self.assertEqual(self.f.count('results'),0)
        with closing(self.f.f.progress.store.connect()) as c:
            self.assertIsNone(c.execute('SELECT name FROM sqlite_master WHERE name=?',(rejection.TABLE,)).fetchone())
    def test_dropped_table_and_partial_publication_block(self):
        result=self.reject();store=self.f.f.progress.store
        with closing(store.connect()) as c:c.execute('DROP TABLE '+rejection.TABLE)
        with self.assertRaisesRegex(ValueError,'table missing'):terminal.gate(store,self.f.f.jobs.path,())
        with self.assertRaises(ValueError):self.f.invoke()
    def test_raw_http_failure_and_uncaptured_attempt_cannot_publish(self):
        self.reject();store=self.f.f.progress.store
        with closing(store.connect()) as c:v=rejection.rows(c)[0]
        key=v['wire_attempt_refs'][0];original=store.load(key)
        with closing(store.connect()) as c:c.execute('DELETE FROM pages WHERE hash=?',(key,))
        store.save({**original,'failure_code':'HTTP_STATUS_403','http_status':403,'response_bytes_base64':None})
        with self.assertRaises(ValueError):rejection.proof(store,v)
    def test_rehashed_summary_and_changed_charge_are_not_trusted(self):
        self.reject();store=self.f.f.progress.store
        with closing(store.connect()) as c:v=rejection.rows(c)[0]
        changed=copy.deepcopy(v);changed['reason']='NO_HAZARDS'
        with self.assertRaises(ValueError):rejection.proof(store,changed)
        self.f.f.progress.reserve(v['scan_id'])
        with self.assertRaises(ValueError):rejection.proof(store,v)
    def test_immutable_disposition_no_replace_update_delete(self):
        self.reject();store=self.f.f.progress.store
        with closing(store.connect()) as c:
            row=c.execute('SELECT * FROM '+rejection.TABLE).fetchone()
            for sql in ('UPDATE '+rejection.TABLE+' SET payload=payload','DELETE FROM '+rejection.TABLE):
                with self.assertRaises(sqlite3.Error):c.execute(sql)
            with self.assertRaises(sqlite3.Error):c.execute('INSERT OR REPLACE INTO '+rejection.TABLE+' VALUES(?,?,?,?)',row)

    def test_ordinary_capacity_matches_journal_not_32_skips(self):
        self.reject()
        with self.f.f.progress.store.connect() as c:
            original=rejection.rows(c)[0]
            for n in range(1,33):
                value=copy.deepcopy(original)
                value.update(dispatch_id=f'{n:032x}',scan_id=f'{n+100:032x}')
                c.execute('INSERT INTO '+rejection.TABLE+' VALUES(?,?,?,?)',
                    (value['dispatch_id'],value['scan_id'],canonical(value),digest(value)))
            self.assertEqual(len(rejection.rows(c)),33)
        self.assertEqual(rejection.MAX,tool.MAX_DISPATCHES)
        # This exercises bounded row inventory, not replay authority for invented rows.
        self.assertIsNone(original['journal_prefix'])

    def test_crash_after_disposition_before_result_stays_no_retry(self):
        self.quote(base58(bytes([33])*32))
        original=tool._write
        def interrupted(c,table,*args,**kw):
            if table=='results':raise ValueError('simulated result-publication crash')
            return original(c,table,*args,**kw)
        with patch.object(tool,'_write',side_effect=interrupted):
            with self.assertRaisesRegex(ValueError,'publication crash'):self.f.live()
        self.assertEqual(self.f.count('intents'),1)
        self.assertEqual(self.f.count('results'),0)
        calls=list(self.f.calls)
        with patch.object(tool.cli,'_credentials',side_effect=AssertionError('retry')):
            with self.assertRaises(ValueError):self.f.invoke(execute=True,systemd_credentials=True)
        self.assertEqual(self.f.calls,calls)
        self.assertEqual(self.f.count('intents'),1)

    def test_corrupt_publication_marker_scalar_bound_precedes_load(self):
        self.reject()
        store=self.f.f.progress.store
        with store.connect() as c:
            value=copy.deepcopy(rejection.rows(c)[0])
            value.update(dispatch_id='d'*32,scan_id='e'*32)
            c.execute('UPDATE pages SET payload=? WHERE hash=?',(b'x'*(20*1024*1024),digest(rejection.INSTALL_MARKER)))
        with patch.object(terminal,'_load',side_effect=AssertionError('must not load corrupt payload')):
            with self.assertRaisesRegex(ValueError,'marker scalar bound'):rejection._insert(store,value)
        with store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM '+rejection.TABLE).fetchone(),(1,))

    def test_absence_gate_uses_exact_marker_lookup_without_page_loads(self):
        store=self.f.f.progress.store
        for n in range(100):store.save({'kind':'unrelated_fixture','n':n})
        with patch.object(terminal,'_load',side_effect=AssertionError('absence must not load pages')):
            self.assertIsNone(rejection.gate(store,self.f.f.jobs.path,()))

    def test_prospective_decline_after_actual_buy_and_sell_preserves_trade_history(self):
        from dataclasses import replace
        from desk import paper_cycle as cycle,quote_execution as qe,provider_pacing as pace
        from desk.monitoring_budget import MonitoringBudget
        from tests import test_kraken_lifecycle as lifecycle
        h=lifecycle.KrakenLifecycleTests();h.setUp()
        try:
            self.assertEqual(h.h.cfg,self.f.cfg)
            with patch.dict('os.environ',{pace.ENV:str(h.path)}):
                bought=lifecycle.actual_cycle(h.h)
                self.assertEqual(bought['status'],'COMPLETE',bought)
                position=cycle._state(h.h.path,h.h.cfg)['positions'][h.h.target.mint]
                MonitoringBudget(h.h.f.progress.store,h.h.path,h.h.cfg).provision()
                item=replace(h.h.item,target=replace(h.h.target,amount_raw=qe.raw_quantity(position['qty'],position['quote_execution']['mint_decimals'])),known_hazards=('MAYHEM_POOL',))
                sold=lifecycle.actual_cycle(h.h,positions=(item,),candidates=(),monitoring=True)
                self.assertEqual(sold['status'],'COMPLETE',sold)
                self.assertEqual(cycle._state(h.h.path,h.h.cfg)['positions'],{})
            # Fixture-only transplant of a genuine completed experiment into the
            # separate dispatcher fixture; keep its recorded file inode intact.
            with closing(sqlite3.connect(h.h.path)) as source,closing(sqlite3.connect(self.f.ledger)) as target:source.backup(target)
        finally:h.doCleanups()
        with sqlite3.connect(self.f.ledger) as c:
            before=c.execute('SELECT seq,event_id,payload,payload_hash FROM events ORDER BY seq').fetchall()
            fills=c.execute('SELECT * FROM outcomes ORDER BY seq').fetchall()
        result=self.reject();self.assertEqual(result['paper_status'],'NO_ENTRY')
        with sqlite3.connect(self.f.ledger) as c:
            self.assertEqual(c.execute('SELECT seq,event_id,payload,payload_hash FROM events ORDER BY seq').fetchall(),before)
            self.assertEqual(c.execute('SELECT * FROM outcomes ORDER BY seq').fetchall(),fills)
        self.assertGreater(len(before),1)
