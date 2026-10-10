"""Synthetic local deadline/guard boundaries; no provider or credential I/O."""
from contextlib import closing
import copy
import unittest
from unittest.mock import patch
from tests import test_migration_slot_intake as intake_fixture
from tests import test_migration_no_entry as dispatch_fixture
from desk import migration_slot_intake as intake, paper_read_sources as transport
from desk import paper_migration_no_entry as disposition, paper_terminal_reconciliation as terminal
from tools import paper_entry_dispatcher as dispatcher

class IntakeDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.f=intake_fixture.MigrationSlotTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
    def test_delayed_local_guard_does_not_consume_provider_window(self):
        clock=[100.0];calls=[]
        def guard():calls.append('guard');clock[0]+=11.0
        with patch.object(intake.time,'monotonic',side_effect=lambda:clock[0]):
            result=self.f.intake(credentials_loader=guard)
        self.assertEqual(result['status'],'RETAINED_MIGRATION_WITNESS')
        self.assertEqual(calls,['guard']);self.assertEqual(len(self.f.opened),1)
        self.assertEqual(result['requests_used'],4)
    def test_guard_failure_has_no_reservation_or_transport(self):
        with self.assertRaisesRegex(ValueError,'local guard'):
            self.f.intake(credentials_loader=lambda:(_ for _ in ()).throw(ValueError('local guard')))
        self.assertEqual(self.f.opened,[])
        self.assertEqual(self.f.f.progress.admission(self.f.target.scan_id)['requests_used'],3)
    def test_expired_reserved_adapter_records_local_failure_not_response(self):
        original=intake._SlotSource.__call__
        def expire(source,*args):source.deadline=source.started;return original(source,*args)
        with patch.object(intake._SlotSource,'__call__',expire):result=self.f.intake()
        self.assertEqual(self.f.opened,[]);self.assertEqual(result['requests_used'],4)
        self.assertEqual(len(result['transport_evidence_refs']),1)
        record=self.f.f.progress.store.load(result['transport_evidence_refs'][0])
        self.assertEqual(record['failure_code'],'INTAKE_DEADLINE_BEFORE_TRANSPORT')
        self.assertIsNone(record['response_bytes_base64']);self.assertIsNone(record['http_status'])
        retry=self.f.intake();self.assertEqual(retry['provider_calls'],0)
        self.assertEqual(retry['requests_used'],4)

class DispatchDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.f=dispatch_fixture.MigrationNoEntryTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
    def expired(self):
        original=intake._SlotSource.__call__
        def expire(source,*args):source.deadline=source.started;return original(source,*args)
        with patch.object(intake._SlotSource,'__call__',expire):return self.f.f.live()
    def test_known_local_failure_records_no_entry_and_no_retry(self):
        f=self.f.f;ledger=f.ledger.read_bytes();result=self.expired()
        self.assertEqual(result['paper_status'],'NO_ENTRY')
        self.assertEqual(f.count('intents'),1);self.assertEqual(f.count('results'),1)
        self.assertEqual(f.ledger.read_bytes(),ledger)
        self.assertEqual(f.f.progress.admission(result['scan_id'])['requests_used'],5)
        self.assertEqual(f.calls.count('getTransactionsForAddress'),2) # acquisition only
        store=f.f.progress.store
        with closing(store.connect()) as c:record=disposition.rows(c)[0]
        self.assertEqual(record['association'],'CAPTURED_INTAKE_LOCAL_DEADLINE')
        self.assertEqual(record['migration']['status'],'NOT_EVALUATED')
        self.assertFalse(record['entry_authorized'])
        with dispatcher._journal(f.journal) as c:dispatcher._validate(c,f.ctx)
        self.assertEqual(terminal.gate(store,f.f.jobs.path,(result['scan_id'],)),'REJECTED_SCAN_RETIRED')
        calls=list(f.calls);self.assertEqual(f.invoke()['status'],'NO_CANDIDATE');self.assertEqual(f.calls,calls)
    def test_missing_or_rehashed_unknown_outcome_cannot_replay(self):
        f=self.f.f;self.expired();store=f.f.progress.store
        with closing(store.connect()) as c:record=disposition.rows(c)[0]
        key=record['wire_attempt_refs'][0];attempt=store.load(key)
        with closing(store.connect()) as c:c.execute('DELETE FROM pages WHERE hash=?',(key,))
        store.save({**attempt,'failure_code':'DEADLINE_EXCEEDED'})
        with self.assertRaises(ValueError):disposition.proof(store,record)
        with self.assertRaises(ValueError):f.invoke()
    def test_wrong_evidence_class_is_not_a_local_disposition(self):
        f=self.f.f;self.expired()
        with closing(f.f.progress.store.connect()) as c:record=disposition.rows(c)[0]
        wrong=copy.deepcopy(record);wrong['association']='CAPTURED_UNSUPPORTED_QUOTE'
        with self.assertRaises(ValueError):disposition.shape(wrong)
