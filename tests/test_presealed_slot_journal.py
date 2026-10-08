"""Synthetic disconnected journal adversaries; never invoke any transport."""
from contextlib import contextmanager
from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.job_persistence import JobPersistence, BIRTH_ACQUISITION_V1
from desk.model import canonical
from desk.pool_receipt_ledger import ApprovedSource
from desk.presealed_slot_journal import PresealedSlotJournal, SlotJournalBlocked, MAX_RESPONSE_BYTES
from tests.test_ownership_acquisition import synthetic_launch


def _journal(research,evidence):
    return PresealedSlotJournal(research,evidence,ApprovedSource('fixture','synthetic_fixture'))


def _reply(request,value=20):
    return b' \n'+canonical({'jsonrpc':'2.0','id':json.loads(request)['id'],'result':value}).encode()+b'\n'


def _die(research,evidence,uid,where):
    jobs=JobPersistence(research)
    with jobs.worker() as worker:
        claim=worker.claim_acquisition(uid)
        with _journal(research,evidence).locked(worker,claim) as s:
            if where=='before_reserve':os._exit(121)
            if where in ('before_charge','after_charge_before_commit'):
                original=sqlite3.connect
                class ReserveDeathConnection(sqlite3.Connection):
                    def execute(self,sql,*args):
                        if sql.startswith('UPDATE ownership_budgets'):
                            if where=='before_charge':os._exit(125)
                            result=super().execute(sql,*args);os._exit(126)
                        return super().execute(sql,*args)
                def connect(*a,**kw):
                    if Path(a[0])==Path(evidence):kw['factory']=ReserveDeathConnection
                    return original(*a,**kw)
                with patch('desk.presealed_slot_journal.sqlite3.connect',side_effect=connect):s.begin_slot()
            h=s.begin_slot()
            if where=='after_reserve':os._exit(122)
            request=s.request_bytes(h)
            original=sqlite3.connect
            class DieConnection(sqlite3.Connection):
                def commit(self):
                    if self.execute('SELECT count(*) FROM presealed_slot_attachment').fetchone()[0]:
                        os._exit(123)
                    return super().commit()
            def connect(*a,**kw):
                if Path(a[0])==Path(evidence):kw['factory']=DieConnection
                return original(*a,**kw)
            with patch('desk.presealed_slot_journal.sqlite3.connect',side_effect=connect):
                s.attach_response(h,request,_reply(request))
    os._exit(124)


class PresealedSlotJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.research=self.root/'research.sqlite';self.evidence=self.root/'evidence.sqlite'
        self.jobs=JobPersistence(self.research);self.mint=synthetic_launch()[0]
        self.uid=self.jobs.admit(self.mint,kind=BIRTH_ACQUISITION_V1,evidence_db=self.evidence)
        self.store=EvidenceStore(self.evidence);self.progress=HistoryProgress(self.store)
        d=self.jobs.descriptor(self.uid)
        self.progress.admit(self.uid,{'kind':'ownership_admission_v1','scan_id':self.uid,'mint':self.mint,'created':d['admitted_at']})

    @contextmanager
    def session(self):
        with self.jobs.worker() as worker:
            claim=worker.claim_acquisition(self.uid)
            with _journal(self.research,self.evidence).locked(worker,claim) as s:yield s

    def count(self):
        with sqlite3.connect(self.evidence) as c:return c.execute('SELECT used,ceiling FROM ownership_budgets').fetchone()

    def test_original_generated_wire_exact_bytes_and_bound_integer_cutoff(self):
        with self.session() as s:
            h=s.begin_slot();request=s.request_bytes(h)
            self.assertEqual(self.count(),(1,18))
            self.assertEqual(json.loads(request)['method'],'getSlot')
            self.assertEqual(json.loads(request)['params'],[{'commitment':'finalized'}])
            self.assertIs(type(json.loads(request)['id']),str)
            response=_reply(request,(1<<63)-2)
            event=s.attach_response(h,request,response)
            self.assertEqual(event['exclusive_cutoff'],(1<<63)-1)
            self.assertFalse(event['source_authenticated']);self.assertFalse(event['eligible_for_trading'])
            before=self.evidence.read_bytes()
            self.assertEqual(s.attach_response(h,request,response),event)
            self.assertEqual(before,self.evidence.read_bytes())
            with sqlite3.connect(self.evidence) as c:
                self.assertEqual(c.execute('SELECT request FROM presealed_slot_intent').fetchone()[0],request)
                self.assertEqual(c.execute('SELECT response FROM presealed_slot_attachment').fetchone()[0],response)
            with self.assertRaises(ValueError):s.attach_response(h,request,_reply(request,21))
            with self.assertRaises(ValueError):s.begin_slot()
        with self.session() as s:
            self.assertEqual(s.inspect(),event)
            with self.assertRaises(ValueError):s.attach_response(h,request,response)
            with self.assertRaises(ValueError):s.begin_slot()
        self.assertEqual(self.count(),(1,18))

    def test_invalid_actual_bytes_are_retained_failed_never_cutoff(self):
        cases=[True,False,None,20.0,-1,(1<<63)-1,1<<64,'20',{},[]]
        for value in cases:
            with self.subTest(value=value):
                # Each independent fixture is a fresh job/database, never a retry.
                other=PresealedSlotJournalTests('test_original_generated_wire_exact_bytes_and_bound_integer_cutoff');other.setUp()
                try:
                    with other.session() as s:
                        h=s.begin_slot();q=s.request_bytes(h);raw=_reply(q,value);e=s.attach_response(h,q,raw)
                        self.assertEqual(e['state'],'FAILED');self.assertIsNone(e['cutoff'])
                        with sqlite3.connect(other.evidence) as c:self.assertEqual(c.execute('SELECT response FROM presealed_slot_attachment').fetchone()[0],raw)
                finally:other.doCleanups()

    def test_forged_wrong_id_missing_frame_duplicate_and_nonfinite_fail(self):
        cases=[b'20',b'{}',b'{"jsonrpc":"2.0","id":1,"result":20}',b'{"result":20,"result":21}',b'{"result":NaN}',b'\xff',b'['*2000]
        for raw in cases:
            other=PresealedSlotJournalTests();other.setUp()
            try:
                with other.session() as s:
                    h=s.begin_slot();q=s.request_bytes(h)
                    self.assertEqual(s.attach_response(h,q,raw)['state'],'FAILED')
            finally:other.doCleanups()

    def test_request_substitution_unknown_capability_and_response_limits(self):
        with self.session() as s:
            h=s.begin_slot();q=s.request_bytes(h)
            for fake in (object(),None):
                with self.assertRaises(ValueError):s.attach_response(fake,q,_reply(q))
            for wrong in (q+b' ',q.replace(b'finalized',b'confirmed'),q.decode()):
                with self.assertRaises(ValueError):s.attach_response(h,wrong,_reply(q))
            for wrong in (b'x'*(MAX_RESPONSE_BYTES+1),{},b''):
                with self.assertRaises(ValueError):s.attach_response(h,q,wrong)
            self.assertEqual(s.inspect()['state'],'PENDING_TERMINAL')
            self.assertEqual(s.attach_failure(h,q,'TRANSPORT_TIMEOUT')['state'],'FAILED')
            with self.assertRaises(ValueError):s.attach_failure(h,q,'SECRET_DETAIL')
            with self.assertRaises(ValueError):s.attach_response(h,q,_reply(q))

    def test_old_generic_reader_and_existing_object_cannot_charge_or_prepare(self):
        with self.session() as s:
            s.begin_slot();before=self.evidence.read_bytes()
            with self.assertRaisesRegex(ValueError,'dedicated reader'):HistoryProgress(self.store)
            with self.assertRaises(sqlite3.IntegrityError):self.progress.reserve(self.uid)
            self.assertEqual(self.count(),(1,18));self.assertEqual(before,self.evidence.read_bytes())
            with self.assertRaises(ValueError):self.jobs.admit(self.mint,kind=BIRTH_ACQUISITION_V1,evidence_db=self.root/'escape.sqlite')

    def test_spent_counters_and_legacy_setup_not_retrofitted(self):
        self.progress.reserve(self.uid)
        with self.session() as s:
            before=self.evidence.read_bytes()
            with self.assertRaises(ValueError):s.begin_slot()
            self.assertEqual(before,self.evidence.read_bytes())
        self.assertEqual(self.count(),(1,18))

    def test_legacy_setup_even_empty_rejected_without_mutation(self):
        with sqlite3.connect(self.evidence) as c:c.execute('CREATE TABLE ownership_acquisition_setup(scan_id TEXT PRIMARY KEY)')
        with self.session() as s:
            before=self.evidence.read_bytes()
            with self.assertRaisesRegex(ValueError,'FRESH_ADMITTED_DATABASE_ONLY'):s.begin_slot()
            self.assertEqual(before,self.evidence.read_bytes())

    def test_source_mismatch_stale_claim_expired_session_and_lock_contention(self):
        with self.jobs.worker() as worker:
            claim=worker.claim_acquisition(self.uid);j=_journal(self.research,self.evidence)
            with j.locked(worker,claim) as s:
                h=s.begin_slot()
                with self.assertRaisesRegex(ValueError,'BUSY'):
                    with j.locked(worker,claim):pass
                with self.assertRaises(ValueError):
                    with j.locked(worker,replace(claim,generation=2)):pass
                wrong=PresealedSlotJournal(self.research,self.evidence,ApprovedSource('other','synthetic_fixture'))
                with self.assertRaisesRegex(ValueError,'MISMATCH'):
                    # Reuse locks only after the current context below.
                    s.journal=wrong;s.inspect()
                s.journal=j
            with self.assertRaises(ValueError):s.request_bytes(h)

    def test_plain_sql_replace_delete_rowid_and_counter_bypasses(self):
        with self.session() as s:
            h=s.begin_slot();q=s.request_bytes(h);s.attach_response(h,q,_reply(q))
            attacks=["DELETE FROM presealed_slot_intent","UPDATE presealed_slot_meta SET body='{}'",
                "INSERT OR REPLACE INTO presealed_slot_intent SELECT * FROM presealed_slot_intent",
                "UPDATE OR REPLACE ownership_budgets SET rowid=2","UPDATE ownership_budgets SET used=0",
                "UPDATE ownership_budgets SET used=2","UPDATE ownership_admissions SET state='SEALED'",
                "INSERT OR REPLACE INTO ownership_budgets SELECT * FROM ownership_budgets"]
            for sql in attacks:
                with sqlite3.connect(self.evidence) as c:
                    self.assertEqual(c.execute('PRAGMA recursive_triggers').fetchone()[0],0)
                    with self.assertRaises(sqlite3.DatabaseError):c.execute(sql)
            self.assertEqual(s.inspect()['state'],'DONE')

    def test_schema_corruption_and_length_preflight(self):
        with self.session() as s:
            s.begin_slot()
            with sqlite3.connect(self.evidence) as c:c.execute('DROP TRIGGER presealed_slot_meta_update')
            with self.assertRaisesRegex(ValueError,'SCHEMA'):s.inspect()

    def test_actual_process_death_boundaries_and_restart_no_new_handle(self):
        for where,exitcode,used in [('before_reserve',121,0),('before_charge',125,0),('after_charge_before_commit',126,0),('after_reserve',122,1),('before_completion_commit',123,1)]:
            other=PresealedSlotJournalTests();other.setUp()
            try:
                p=multiprocessing.get_context('spawn').Process(target=_die,args=(other.research,other.evidence,other.uid,where))
                p.start();p.join(15)
                if p.is_alive():p.kill();p.join();self.fail('Fixture child timeout')
                self.assertEqual(p.exitcode,exitcode);self.assertEqual(other.count(),(used,18))
                with other.session() as s:
                    before=other.evidence.read_bytes()
                    with self.assertRaises(ValueError):s.begin_slot()
                    if used:self.assertEqual(s.inspect()['state'],'PENDING_TERMINAL')
                    self.assertEqual(before,other.evidence.read_bytes())
            finally:other.doCleanups()

    def test_completion_commit_failure_and_lost_ack_identical_persistence_only(self):
        with self.session() as s:
            h=s.begin_slot();q=s.request_bytes(h);raw=_reply(q)
            original=sqlite3.connect
            for after in (False,True):
                class FailConnection(sqlite3.Connection):
                    def commit(self):
                        if self.execute('SELECT count(*) FROM presealed_slot_attachment').fetchone()[0]:
                            if after:super().commit()
                            raise OSError('fixture completion commit ambiguity')
                        return super().commit()
                def connect(*a,**kw):
                    if Path(a[0])==self.evidence:kw['factory']=FailConnection
                    return original(*a,**kw)
                with patch('desk.presealed_slot_journal.sqlite3.connect',side_effect=connect):
                    with self.assertRaises(OSError):s.attach_response(h,q,raw)
                with self.assertRaises(ValueError):s.attach_response(h,q,_reply(q,21))
            self.assertEqual(s.attach_response(h,q,raw)['cutoff'],20);self.assertEqual(self.count(),(1,18))

    def test_reservation_lost_ack_never_yields_capability(self):
        with self.session() as s:
            original=sqlite3.connect
            class FailConnection(sqlite3.Connection):
                def commit(self):
                    super().commit();raise OSError('fixture reserve ack lost')
            def connect(*a,**kw):
                if Path(a[0])==self.evidence:kw['factory']=FailConnection
                return original(*a,**kw)
            with patch('desk.presealed_slot_journal.sqlite3.connect',side_effect=connect):
                with self.assertRaises(OSError):s.begin_slot()
            self.assertFalse(s.handles);self.assertEqual(self.count(),(1,18))
            self.assertEqual(s.inspect()['state'],'PENDING_TERMINAL')
            with self.assertRaises(ValueError):s.begin_slot()

    def test_budget_exhaustion_cannot_infer_or_reset_original_cutoff(self):
        for _ in range(18):self.assertTrue(self.progress.reserve(self.uid))
        with self.session() as s:
            before=self.evidence.read_bytes()
            with self.assertRaises(ValueError):s.begin_slot()
            self.assertEqual(before,self.evidence.read_bytes())
        self.assertEqual(self.count(),(18,18))

    def test_preflight_oversized_admission_before_body_load(self):
        with sqlite3.connect(self.evidence) as c:c.execute('UPDATE ownership_admissions SET descriptor=?',('x'*8193,))
        with self.session() as s:
            with self.assertRaisesRegex(ValueError,'BOUNDED'):s.begin_slot()
        self.assertEqual(self.count(),(0,18))

    def test_attachment_preflight_before_body_fetch_and_unknown_version(self):
        from desk.presealed_slot_journal import SCHEMA
        with self.session() as s:
            h=s.begin_slot();q=s.request_bytes(h);s.attach_response(h,q,_reply(q))
            with sqlite3.connect(self.evidence) as c:
                c.execute('DROP TRIGGER presealed_slot_attachment_update')
                c.execute('UPDATE presealed_slot_attachment SET response=?',(b'x'*(MAX_RESPONSE_BYTES+1),))
                c.execute(SCHEMA['presealed_slot_attachment_update'])
            seen=[];original=sqlite3.connect
            def connect(*a,**kw):
                c=original(*a,**kw)
                if Path(a[0])==self.evidence:c.set_trace_callback(seen.append)
                return c
            with patch('desk.presealed_slot_journal.sqlite3.connect',side_effect=connect):
                with self.assertRaisesRegex(ValueError,'RECORD_LIMIT'):s.inspect()
            self.assertFalse(any(sql.startswith('SELECT body') for sql in seen),seen)
            with sqlite3.connect(self.evidence) as c:c.execute('PRAGMA user_version=999')
            with self.assertRaisesRegex(ValueError,'VERSION'):s.inspect()

    def test_alias_hardlink_and_replaced_database_pins(self):
        alias=self.root/'alias.sqlite';alias.symlink_to(self.evidence)
        self.assertEqual(_journal(self.research,alias).path,self.evidence.resolve())
        j=_journal(self.research,self.evidence)
        link=self.root/'hard.sqlite';os.link(self.evidence,link)
        with self.assertRaises(ValueError):j._guard()
        link.unlink()
        saved=self.root/'saved.sqlite';self.evidence.rename(saved)
        self.evidence.write_bytes(saved.read_bytes())
        with self.assertRaises(ValueError):j._guard()

    def test_another_actual_process_cannot_claim_while_live_worker_holds_lock(self):
        with self.session() as s:
            h=s.begin_slot()
            # Fork receives parent's open descriptions; a fresh worker opens its
            # own lock and cannot obtain it. Never unlock inherited descriptions.
            ctx=multiprocessing.get_context('fork');readfd,writefd=os.pipe()
            def contender():
                os.close(readfd)
                with JobPersistence(self.research).worker() as worker:
                    os.write(writefd,b'blocked' if worker is None else b'claimed')
                os.close(writefd)
            p=ctx.Process(target=contender);p.start();os.close(writefd);p.join(10)
            try:
                if p.is_alive():p.kill();p.join();self.fail('Race child timeout')
                self.assertEqual(p.exitcode,0);self.assertEqual(os.read(readfd,32),b'blocked')
            finally:os.close(readfd)
            self.assertEqual(s.attach_failure(h,s.request_bytes(h),'CANCELLED')['state'],'FAILED')
        self.assertEqual(self.count(),(1,18))
