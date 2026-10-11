"""Disconnected finalized getSlot: production journal, raw bytes and real deaths."""
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from desk import common_bank_journal as module
from desk.common_bank_journal import CommonBankJournal, JournalBlocked, SCHEMA, ATTACHMENT_SCHEMA, SLOT_SCHEMA
from desk.model import canonical, digest
from desk.pool_receipt_ledger import ApprovedSource
from desk.pool_vault_admission import GENESIS
from tests import test_common_bank_journal as fixtures


def response(request,value):
    return ('\n '+json.dumps({'jsonrpc':'2.0','id':json.loads(request)['id'],'result':value})+' \n').encode()


def child(research,evidence,fence,mode,queue=None,go=None):
    if go is not None: go.wait(10)
    real_connect=sqlite3.connect
    if mode in ('intent_before','completion_before'):
        class DeathConnection(sqlite3.Connection):
            def commit(self):
                table='common_bank_slot_intents' if mode=='intent_before' else 'common_bank_slot_attachments'
                if self.execute('SELECT count(*) FROM '+table).fetchone()[0]: os._exit(91 if mode=='intent_before' else 93)
                return super().commit()
        def connect(*args,**kwargs): return real_connect(*args,**{**kwargs,'factory':DeathConnection})
        sqlite3.connect=connect
    journal=CommonBankJournal(research,evidence,ApprovedSource('fixture','synthetic_fixture'))
    try:
        with journal.locked() as session:
            token=session.begin_slot('capture',fence=fence,reserved_at=123)
            if mode=='intent_after': os._exit(92)
            request=session.slot_request(token)
            result=(session.attach_slot_failure(token,'TRANSPORT_ERROR',completed_at=124)
                    if mode=='failure_after' else session.attach_slot_response(token,response(request,42),completed_at=124))
            if mode=='completion_after': os._exit(94)
            if mode=='failure_after': os._exit(95)
        if queue is not None: queue.put(('OK',result['state']))
    except Exception as exc:
        if queue is not None: queue.put(('BLOCKED',str(exc)))
        else: raise


@unittest.skipUnless(Path('/proc/self/mountinfo').is_file(),'Linux guarded source contract')
class SlotStageTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.CommonBankJournalTests(); self.addCleanup(self.f.doCleanups); self.f.setUp()
        self.f.freeze(); self.journal=self.f.journal
        with self.journal.locked() as session:
            session.install_genesis_attachments()
            token=session.begin_genesis('capture',fence=self.f.fence,reserved_at=121)
            self.genesis=session.attach_genesis_response(token,response(session.genesis_request(token),GENESIS),completed_at=122)
        self.fence=digest({'previous_hash':self.genesis['intent_hash'],'event':self.genesis})

    def install(self):
        with self.journal.locked() as session: session.install_slot_stage()

    def begin(self,session): return session.begin_slot('capture',fence=self.fence,reserved_at=123)

    def rows(self,table='common_bank_slot_attachments'):
        with self.f.store.connect() as c: return c.execute('SELECT * FROM '+table).fetchall()

    def original(self):
        with self.f.store.connect() as c:
            return {t:c.execute('SELECT * FROM '+t).fetchall() for t in
                    ('common_bank_meta','common_bank_runs','common_bank_events','common_bank_attachment_meta','common_bank_attachments','pages','ownership_admissions','ownership_history')}

    def test_explicit_upgrade_preserves_all_genesis_sql_and_original_rows(self):
        before=self.original()
        with self.journal.locked() as session:
            with self.assertRaisesRegex(JournalBlocked,'EXPLICIT'): self.begin(session)
            session.install_slot_stage(); session.install_slot_stage(); session.install_genesis_attachments()
        self.assertEqual(self.original(),before); self.assertEqual(self.f.used(),4)
        with self.f.store.connect() as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],module.SLOT_VERSION)
            for name,sql in (SCHEMA|ATTACHMENT_SCHEMA).items():
                self.assertEqual(c.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()[0],sql)
        self.assertEqual(self.f.resume()['state'],'GENESIS_DONE')

    def test_old_v1_requires_explicit_genesis_profile_and_known_genesis_can_finish_after_upgrade(self):
        f=fixtures.CommonBankJournalTests();f.setUp()
        try:
            f.freeze()
            with f.journal.locked() as s:
                with self.assertRaisesRegex(JournalBlocked,'GENESIS_ATTACHMENT_PROFILE'):s.install_slot_stage()
                s.install_genesis_attachments();g=s.begin_genesis('capture',fence=f.fence,reserved_at=121)
                s.install_slot_stage()
                done=s.attach_genesis_response(g,response(s.genesis_request(g),GENESIS),completed_at=122)
                slot=s.begin_slot('capture',fence=digest({'previous_hash':done['intent_hash'],'event':done}),reserved_at=123)
                s.attach_slot_response(slot,response(s.slot_request(slot),42),completed_at=124)
            self.assertEqual(f.used(),5);self.assertEqual(f.resume()['state'],'SLOT_DONE')
        finally:f.doCleanups()

    def test_oversized_slot_response_never_validates_or_truncates_positive_prefix(self):
        self.install()
        with self.journal.locked() as s:
            t=self.begin(s);raw=response(s.slot_request(t),42)
            raw+=b' '*(module.MAX_RESPONSE_BYTES+1-len(raw))
            event=s.attach_slot_response(t,raw,completed_at=124)
            self.assertEqual(event['state'],'FAILED');self.assertIsNone(event['slot'])
            self.assertEqual(event['reason'],'RESPONSE_OVERSIZED')
        row=self.rows()[0];self.assertIsNone(row[5]);failure=json.loads(row[6])
        self.assertEqual(failure['observed_bytes'],len(raw));self.assertEqual(failure['method'],'getSlot')
        self.assertEqual(self.f.used(),5)

    def test_exact_finalized_request_atomic_charge_raw_completion_and_idempotence(self):
        self.install(); before=self.original()
        with self.journal.locked() as session:
            token=self.begin(session); request=session.slot_request(token); raw=response(request,42)
            self.assertEqual(json.loads(request)['method'],'getSlot')
            self.assertEqual(json.loads(request)['params'],[{'commitment':'finalized'}])
            self.assertEqual(session.resume_capture('multi')['state'],'SLOT_IN_FLIGHT')
            result=session.attach_slot_response(token,raw,completed_at=124)
            self.assertEqual((result['state'],result['slot']),('DONE',42))
            self.assertEqual(result['fence'],self.fence); self.assertEqual(result['ordinal'],4)
            self.assertEqual(session.attach_slot_response(token,raw,completed_at=124),result)
            for body,at in ((raw+b' ',124),(raw,125),(response(request,43),124)):
                with self.assertRaisesRegex(JournalBlocked,'CONFLICTING'):
                    session.attach_slot_response(token,body,completed_at=at)
        self.assertEqual(self.f.used(),5); self.assertEqual(self.original(),before)
        row=self.rows()[0]; self.assertEqual(row[4:6],(request,raw))
        state=self.f.resume(); self.assertEqual(state['state'],'SLOT_DONE')
        for k in ('eligible_for_trading','ownership_approval','chain_authenticated'): self.assertIs(state[k],False)
        self.assertEqual(state['provider_calls'],0)
        with self.f.store.connect() as c:
            self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name IN ('ownership_banks','ownership_heads')").fetchone())
        with self.journal.locked() as session:
            with self.assertRaisesRegex(JournalBlocked,'ALREADY_RESERVED'): self.begin(session)
            with self.assertRaisesRegex(JournalBlocked,'BUDGET_ALREADY_BOUND'): session.create_run('multi','replacement')

    def test_actual_slot_strict_u64_and_malformed_responses_retained_without_summary(self):
        cases=[0,2**64-1,True,False,-1,2**64,1.0,'42',None,{},[]]
        for value in cases:
            with self.subTest(value=value):
                f=fixtures.CommonBankJournalTests(); f.setUp()
                try:
                    f.freeze()
                    with f.journal.locked() as s:
                        s.install_genesis_attachments(); g=s.begin_genesis('capture',fence=f.fence,reserved_at=121)
                        done=s.attach_genesis_response(g,response(s.genesis_request(g),GENESIS),completed_at=122)
                        s.install_slot_stage(); token=s.begin_slot('capture',fence=digest({'previous_hash':done['intent_hash'],'event':done}),reserved_at=123)
                        raw=response(s.slot_request(token),value); event=s.attach_slot_response(token,raw,completed_at=124)
                        valid=type(value) is int and 0<=value<2**64
                        self.assertEqual(event['state'],'DONE' if valid else 'FAILED')
                        self.assertEqual(event['slot'],value if valid else None)
                    with f.store.connect() as c: self.assertEqual(c.execute('SELECT response_bytes FROM common_bank_slot_attachments').fetchone()[0],raw)
                    self.assertEqual(f.used(),5)
                finally: f.doCleanups()

    def test_failed_or_ambiguous_genesis_never_transitions(self):
        for failed in (False,True):
            with self.subTest(failed=failed):
                f=fixtures.CommonBankJournalTests(); f.setUp()
                try:
                    f.freeze()
                    with f.journal.locked() as s:
                        s.install_genesis_attachments(); token=s.begin_genesis('capture',fence=f.fence,reserved_at=121)
                        if failed: s.attach_genesis_failure(token,'TRANSPORT_TIMEOUT',completed_at=122)
                        s.install_slot_stage()
                        with self.assertRaisesRegex(JournalBlocked,'GENESIS_DONE'):
                            s.begin_slot('capture',fence=f.fence,reserved_at=123)
                    self.assertEqual(f.used(),4)
                    with f.store.connect() as c: self.assertEqual(c.execute('SELECT count(*) FROM common_bank_slot_intents').fetchone()[0],0)
                finally: f.doCleanups()

    def test_stale_fence_bad_time_forged_cross_stage_and_expired_handles(self):
        self.install()
        with self.journal.locked() as s:
            for fence,at in ((self.f.fence,123),(self.fence,121),(self.fence,True)):
                with self.assertRaises(JournalBlocked): s.begin_slot('capture',fence=fence,reserved_at=at)
            token=self.begin(s); request=s.slot_request(token)
            for body in ({'result':42},42,bytearray(response(request,42))):
                with self.assertRaisesRegex(JournalBlocked,'RAW_RESPONSE'): s.attach_slot_response(token,body,completed_at=124)
            with self.assertRaisesRegex(JournalBlocked,'UNKNOWN_INFLIGHT'): s.attach_slot_response(object(),b'{}',completed_at=124)
            with self.assertRaisesRegex(JournalBlocked,'STAGE_MISMATCH'): s.genesis_request(token)
            with self.assertRaisesRegex(JournalBlocked,'STAGE_MISMATCH'): s.attach_genesis_response(token,b'{}',completed_at=124)
        with self.assertRaisesRegex(JournalBlocked,'EXPIRED'): s.slot_request(token)
        with self.journal.locked() as other:
            with self.assertRaisesRegex(JournalBlocked,'UNKNOWN_INFLIGHT'): other.attach_slot_response(token,response(request,42),completed_at=124)
        self.assertEqual(self.f.resume()['state'],'TERMINAL_UNCERTAINTY'); self.assertEqual(self.f.used(),5)

    def test_shared_spending_and_lower_bound_never_refund_or_reset(self):
        self.install()
        for _ in range(8): self.assertTrue(self.f.progress.reserve('multi'))
        self.assertEqual(self.f.used(),12)
        with self.journal.locked() as s:
            with self.assertRaisesRegex(JournalBlocked,'LOWER_BOUND'): self.begin(s)
        self.assertEqual(self.f.used(),12); self.assertEqual(self.rows('common_bank_slot_intents'),[])

    def test_slot_failure_retained_redacted_and_terminal_without_refund(self):
        self.install()
        with self.journal.locked() as s:
            t=self.begin(s); result=s.attach_slot_failure(t,'TRANSPORT_TIMEOUT',completed_at=124)
            self.assertEqual(result['state'],'FAILED'); self.assertIsNone(result['slot'])
            self.assertEqual(s.attach_slot_failure(t,'TRANSPORT_TIMEOUT',completed_at=124),result)
        failure=json.loads(self.rows()[0][6]); self.assertEqual(failure['method'],'getSlot')
        self.assertEqual(failure['params'],[{'commitment':'finalized'}])
        self.assertEqual(self.f.used(),5); self.assertEqual(self.f.resume()['state'],'SLOT_FAILED')

    def death(self,mode,code):
        self.install(); ctx=multiprocessing.get_context('spawn')
        p=ctx.Process(target=child,args=(self.f.research,self.f.path,self.fence,mode));p.start()
        self.addCleanup(lambda:p.kill() if p.is_alive() else None);p.join(20);self.assertEqual(p.exitcode,code)

    def test_real_death_before_intent_commit_rolls_back_charge_and_intent(self):
        self.death('intent_before',91); self.assertEqual(self.f.used(),4)
        self.assertEqual(self.rows('common_bank_slot_intents'),[]);self.assertEqual(self.f.resume()['state'],'GENESIS_DONE')
        with self.journal.locked() as s: self.begin(s)
        self.assertEqual(self.f.used(),5)

    def test_real_death_after_intent_is_charged_ambiguous_no_new_handle(self):
        self.death('intent_after',92);self.assertEqual(self.f.used(),5)
        self.assertEqual(self.rows(),[]);self.assertEqual(self.f.resume()['state'],'TERMINAL_UNCERTAINTY')
        with self.journal.locked() as s:
            with self.assertRaisesRegex(JournalBlocked,'ALREADY_RESERVED'): self.begin(s)

    def test_real_death_before_completion_commit_preserves_terminal_pending(self):
        self.death('completion_before',93);self.assertEqual(self.f.used(),5)
        self.assertEqual(self.rows(),[]);self.assertEqual(self.f.resume()['state'],'TERMINAL_UNCERTAINTY')

    def test_real_death_after_done_commit_preserves_exact_slot(self):
        self.death('completion_after',94);self.assertEqual(self.f.used(),5)
        self.assertEqual(self.f.resume()['slot_completion']['slot'],42)
        self.assertEqual(self.f.resume()['state'],'SLOT_DONE')

    def test_real_death_after_failure_commit_preserves_failed_attempt(self):
        self.death('failure_after',95);self.assertEqual(self.f.used(),5)
        self.assertEqual(self.f.resume()['state'],'SLOT_FAILED')
        self.assertEqual(json.loads(self.rows()[0][6])['category'],'TRANSPORT_ERROR')

    def test_two_processes_race_exactly_one_slot_charge_and_completion(self):
        self.install();ctx=multiprocessing.get_context('spawn');q=ctx.Queue();go=ctx.Event()
        ps=[ctx.Process(target=child,args=(self.f.research,self.f.path,self.fence,'race',q,go)) for _ in range(2)]
        for p in ps:p.start();self.addCleanup(lambda p=p:p.kill() if p.is_alive() else None)
        go.set()
        for p in ps:p.join(20);self.assertEqual(p.exitcode,0)
        results=[q.get(timeout=3) for _ in ps];q.close();q.join_thread()
        self.assertEqual(sum(r[0]=='OK' for r in results),1,results)
        self.assertEqual(self.f.used(),5);self.assertEqual(len(self.rows()),1)

    def test_completion_commit_errors_only_identical_known_observation_can_persist(self):
        self.install()
        # First failed commit leaves charged intent and freezes the observation;
        # second failure occurs after the actual completion transaction commits.
        with self.journal.locked() as s:
            t=self.begin(s);raw=response(s.slot_request(t),42);real=sqlite3.connect
            for after in (False,True):
                class ErrorConnection(sqlite3.Connection):
                    def commit(self):
                        if after:super().commit()
                        raise sqlite3.OperationalError('injected slot completion commit')
                def connect(*a,**kw):return real(*a,**{**kw,'factory':ErrorConnection})
                with patch.object(module.sqlite3,'connect',side_effect=connect):
                    with self.assertRaisesRegex(sqlite3.OperationalError,'injected'):s.attach_slot_response(t,raw,completed_at=124)
                with self.assertRaisesRegex(JournalBlocked,'CONFLICTING'):s.attach_slot_response(t,raw+b' ',completed_at=124)
            self.assertEqual(s.attach_slot_response(t,raw,completed_at=124)['slot'],42)
        self.assertEqual(self.f.used(),5);self.assertEqual(len(self.rows()),1)

    def test_slot_reservation_commit_error_before_and_after_durability_mints_no_handle(self):
        self.install();real=sqlite3.connect
        with self.journal.locked() as s:
            for after in (False,True):
                class ErrorConnection(sqlite3.Connection):
                    def commit(self):
                        if after:super().commit()
                        raise sqlite3.OperationalError('injected slot reservation commit')
                def connect(*a,**kw):return real(*a,**{**kw,'factory':ErrorConnection})
                with patch.object(module.sqlite3,'connect',side_effect=connect):
                    with self.assertRaisesRegex(sqlite3.OperationalError,'injected'):self.begin(s)
                self.assertEqual(self.f.used(),5 if after else 4)
                self.assertEqual(s.resume_capture('multi')['state'],'TERMINAL_UNCERTAINTY' if after else 'GENESIS_DONE')
                self.assertFalse(s._inflight)
            with self.assertRaisesRegex(JournalBlocked,'ALREADY_RESERVED'):self.begin(s)
        self.assertEqual(len(self.rows('common_bank_slot_intents')),1)

    def test_genesis_predecessor_cannot_be_rebased_even_with_recomputed_parent_hash(self):
        self.install()
        with self.journal.locked() as s:self.begin(s)
        with self.f.store.connect() as c:
            c.execute('DROP TRIGGER common_bank_attachments_update')
            event=json.loads(c.execute('SELECT event_json FROM common_bank_attachments').fetchone()[0])
            event['completed_at']=123
            key=digest({'previous_hash':event['intent_hash'],'event':event})
            c.execute('UPDATE common_bank_attachments SET event_json=?,event_hash=?',(canonical(event),key))
            c.execute(ATTACHMENT_SCHEMA['common_bank_attachments_update'])
        with self.assertRaisesRegex(JournalBlocked,'FENCE_OR_HASH'):self.f.resume()
        self.assertEqual(self.f.used(),5)

    def test_stale_source_and_counter_rewind_do_not_acknowledge_slot(self):
        self.install()
        with self.journal.locked() as s:
            t=self.begin(s);raw=response(s.slot_request(t),42)
            with self.f.store.connect() as c:c.execute("UPDATE ownership_admissions SET completed_source_hash=? WHERE id='multi'",('0'*64,))
            with self.assertRaises(ValueError):s.attach_slot_response(t,raw,completed_at=124)
            self.assertEqual(self.rows(),[])
            with self.f.store.connect() as c:
                c.execute("UPDATE ownership_admissions SET completed_source_hash=? WHERE id='multi'",(digest(self.f.scan),))
            s.attach_slot_response(t,raw,completed_at=124)
            with self.f.store.connect() as c:c.execute("UPDATE ownership_budgets SET used=4 WHERE id='multi'")
            with self.assertRaisesRegex(JournalBlocked,'SLOT_INTENT_INVALID'):s.resume_capture('multi')

    def test_fresh_connections_cannot_replace_delete_update_or_alias_slot_records(self):
        self.install()
        with self.journal.locked() as s:
            t=self.begin(s);s.attach_slot_response(t,response(s.slot_request(t),42),completed_at=124)
        for table in ('common_bank_slot_meta','common_bank_slot_intents','common_bank_slot_attachments'):
            with sqlite3.connect(self.f.path) as c:
                before=c.execute('SELECT * FROM '+table).fetchall()
                self.assertEqual(c.execute('PRAGMA recursive_triggers').fetchone()[0],0)
                for sql in ('DELETE FROM '+table,'INSERT OR REPLACE INTO '+table+' SELECT * FROM '+table,
                            'UPDATE '+table+' SET '+('id=id' if table.endswith('meta') else 'capture_id=capture_id')):
                    with self.assertRaises(sqlite3.DatabaseError):c.execute(sql)
                for alias in ('rowid','oid','_rowid_'):
                    with self.assertRaises(sqlite3.DatabaseError):c.execute('UPDATE OR REPLACE '+table+' SET '+alias+'=2')
                self.assertEqual(c.execute('SELECT * FROM '+table).fetchall(),before)
        self.assertEqual(self.f.resume()['state'],'SLOT_DONE')

    def test_corrupt_slot_raw_bytes_detected_and_missing_schema_never_remigrates(self):
        self.install()
        with self.journal.locked() as s:
            t=self.begin(s);raw=response(s.slot_request(t),42);s.attach_slot_response(t,raw,completed_at=124)
        with self.f.store.connect() as c:
            c.execute('DROP TRIGGER common_bank_slot_attachments_update')
            c.execute('UPDATE common_bank_slot_attachments SET response_bytes=?',(raw+b' ',))
            c.execute(SLOT_SCHEMA['common_bank_slot_attachments_update'])
        with self.assertRaisesRegex(JournalBlocked,'HASH_OR_BINDING'):self.f.resume()
        with self.f.store.connect() as c:c.execute('DROP TABLE common_bank_slot_meta')
        with self.journal.locked() as s:
            with self.assertRaisesRegex(JournalBlocked,'SCHEMA'):s.install_slot_stage()
        self.assertEqual(self.f.used(),5)
