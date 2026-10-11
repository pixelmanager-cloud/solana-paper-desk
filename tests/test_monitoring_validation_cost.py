"""Fixture-only validation reuse; no cache across calls, transactions or I/O."""
import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

from desk import paper_checkpoint as checkpoint, runtime_compatibility as runtime
from desk.model import canonical
from desk.monitoring_budget import MonitoringBudget,MonitoringBlocked
from tests import test_monitoring_successor as fixtures
from tests.test_runtime_compatibility import dump
from tests.helpers import T


class MonitoringValidationCostTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.MonitoringSuccessorTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.f.activate()
        saved=(self.f.f.new,self.f.f.cfg)
        self.f.f.new=self.f.next;self.f.f.cfg=self.f.cfg
        try:self.target=self.f.f.buy_new()
        finally:self.f.f.new,self.f.f.cfg=saved
        self.store=self.f.f.store
        self.budget=MonitoringBudget(self.store,self.f.next,self.f.cfg,clock=lambda:T+3)
        self.progress=self.f.f.f.context.progress

    def reserve(self):
        return self.budget.reserve_read(self.progress,self.target.scan_id,'getSlot',[{'commitment':'finalized'}])

    def record(self,receipt,**kw):
        return {'monitoring_reservation':receipt,'scan_id':self.target.scan_id,'method':'getSlot',
                'params':[{'commitment':'finalized'}],'failure_code':None,**kw}

    def test_one_current_checkpoint_and_fresh_source_per_validation_call(self):
        original=checkpoint.validate_checkpoint;current=[]
        def counted(c,payload):
            path=c.execute('PRAGMA database_list').fetchone()[2]
            if path==str(self.f.next):current.append(payload)
            return original(c,payload)
        with patch.object(checkpoint,'validate_checkpoint',side_effect=counted),patch.object(runtime,'implementation_hash',wraps=runtime.implementation_hash) as hashes:
            state,payload=self.budget._checkpoint()
            self.assertEqual(state,json.loads(payload));self.assertEqual(len(current),1);self.assertEqual(hashes.call_count,1)
            self.budget.snapshot()
            self.assertEqual(len(current),2);self.assertEqual(hashes.call_count,2)
        # A constructor hash is never evidence that later source bytes are current.
        before=dump(self.store.path)
        with patch.object(runtime,'implementation_hash',return_value='0'*64):
            for operation in (self.budget._checkpoint,self.budget.snapshot):
                with self.assertRaises(MonitoringBlocked):operation()
        self.assertEqual(dump(self.store.path),before)

    def test_every_new_receipt_field_is_checked_and_insertion_rolls_back(self):
        receipt=self.reserve()
        variants={'id':999,'total_used':999,'reserved_at':T,'checkpoint_hash':'0'*64,
                  'mint':'forged','kind':'open_paper_monitoring_reservation_v1','cap':60,
                  'window_seconds':1,'policy_hash':'0'*64,'context_hash':'0'*64,'successor_context_hash':'0'*64}
        for field,value in variants.items():
            with self.subTest(field=field):
                forged=copy.deepcopy(receipt);forged[field]=value
                key=self.store.save(self.record(forged));before=dump(self.store.path)
                with self.assertRaises(MonitoringBlocked):self.budget.retain_outcome(forged,key)
                self.assertEqual(dump(self.store.path),before)
        key=self.store.save(self.record(receipt));self.budget.retain_outcome(receipt,key)
        before=dump(self.store.path);self.budget.retain_outcome(receipt,key)
        self.assertEqual(dump(self.store.path),before)
        conflicting=self.store.save(self.record(receipt,extra='conflicting retained evidence'));before=dump(self.store.path)
        with self.assertRaises(MonitoringBlocked):self.budget.retain_outcome(receipt,conflicting)
        self.assertEqual(dump(self.store.path),before)

    def test_mutated_checkpoint_between_reservation_and_retention_refuses(self):
        receipt=self.reserve();key=self.store.save(self.record(receipt))
        with sqlite3.connect(self.f.next) as c:
            state=json.loads(c.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
            state['positions'][self.target.mint]['qty']='999'
            c.execute('UPDATE state SET payload=? WHERE id=1',(canonical(state),))
        before=dump(self.store.path)
        with self.assertRaises(MonitoringBlocked):self.budget.retain_outcome(receipt,key)
        self.assertEqual(dump(self.store.path),before)

    def test_mutated_retired_context_or_fence_is_not_reused_after_io(self):
        receipt=self.reserve();key=self.store.save(self.record(receipt))
        with sqlite3.connect(self.f.f.new) as c:c.execute("INSERT INTO metadata VALUES('synthetic-corruption','changed')")
        before=dump(self.store.path)
        with self.assertRaises(MonitoringBlocked):self.budget.retain_outcome(receipt,key)
        self.assertEqual(dump(self.store.path),before)
        with sqlite3.connect(self.f.f.new) as c:c.execute("DELETE FROM metadata WHERE key='synthetic-corruption'")
        from desk.monitoring_successor import FENCE
        with self.store.connect() as c:c.execute('DROP TRIGGER '+FENCE)
        before=dump(self.store.path)
        with self.assertRaises(MonitoringBlocked):self.budget.retain_outcome(receipt,key)
        self.assertEqual(dump(self.store.path),before)

    def test_changed_source_and_config_refuse_retention_pending_and_latch_preserved(self):
        receipt=self.reserve();key=self.store.save(self.record(receipt,failure_code='SOURCE_REQUEST_FAILED'))
        before=dump(self.store.path)
        with patch.object(runtime,'implementation_hash',return_value='0'*64):
            with self.assertRaises(MonitoringBlocked):self.budget.retain_outcome(receipt,key)
        self.assertEqual(dump(self.store.path),before)
        with sqlite3.connect(self.f.next) as c:
            cfg=c.execute("SELECT value FROM metadata WHERE key='config_hash'").fetchone()[0]
            c.execute("UPDATE metadata SET value=? WHERE key='config_hash'",('0'*64,))
        with self.assertRaises(MonitoringBlocked):self.budget.retain_outcome(receipt,key)
        self.assertEqual(dump(self.store.path),before)
        with sqlite3.connect(self.f.next) as c:c.execute("UPDATE metadata SET value=? WHERE key='config_hash'",(cfg,))
        # Known retained failure may resolve its own charged attempt, never clear
        # or replace an earlier latch and never authorize the next request.
        with self.store.connect() as c:c.execute("UPDATE paper_monitoring_budget SET blocked='CLOCK_ROLLBACK'")
        self.budget.retain_outcome(receipt,key)
        self.assertIn('MONITORING_RECOVERY_REQUIRED',self.budget.snapshot()['blockers'])
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT blocked FROM paper_monitoring_budget').fetchone(),('CLOCK_ROLLBACK',))
        before=dump(self.store.path)
        with self.assertRaises(MonitoringBlocked):self.reserve()
        self.assertEqual(dump(self.store.path),before)

    def test_runtime_receipt_mutation_between_reservation_and_retention_refuses(self):
        receipt=self.reserve();key=self.store.save(self.record(receipt))
        with sqlite3.connect(self.f.next) as c:c.execute('CREATE TABLE paper_runtime_transition(unreviewed TEXT)')
        before=dump(self.store.path)
        with self.assertRaises(MonitoringBlocked):self.budget.retain_outcome(receipt,key)
        self.assertEqual(dump(self.store.path),before)

    def test_unrelated_pending_charge_remains_blocked_after_own_retention(self):
        receipt=self.reserve();key=self.store.save(self.record(receipt))
        with self.store.connect() as c:
            c.execute('INSERT INTO paper_monitoring_reservations VALUES(?,?,?,?,?,?,?,?,?)',
                (receipt['id']+1,T+3,'unresolved-other',receipt['mint'],receipt['checkpoint_hash'],
                 'getSlot','a'*64,receipt['context_hash'],receipt['successor_context_hash']))
            c.execute('UPDATE paper_monitoring_budget SET total=total+1')
        self.budget.retain_outcome(receipt,key)
        snapshot=self.budget.snapshot()
        self.assertEqual(snapshot['total_used'],receipt['id']+1)
        self.assertIn('MONITORING_OUTCOME_PENDING',snapshot['blockers'])
        before=dump(self.store.path)
        with self.assertRaises(MonitoringBlocked):self.reserve()
        self.assertEqual(dump(self.store.path),before)
