"""Disconnected getBlockTime(T), production persistence with synthetic bytes."""
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from desk import common_bank_journal as module
from desk.common_bank_journal import CommonBankJournal,JournalBlocked,SCHEMA,ATTACHMENT_SCHEMA,SLOT_SCHEMA,UNION_SCHEMA,CLOCK_SCHEMA
from desk.model import canonical,digest
from desk.pool_receipt_ledger import ApprovedSource
from tests import test_common_bank_journal as journal_fixture
from tests import test_common_bank_union_stage as union_fixture


def setup(f,outcome='DONE'):
    union_fixture.setup(f)
    body={'context':{'slot':21},'value':union_fixture.values(f)}
    with f.journal.locked() as s:
        s.install_union_stage();t=s.begin_union('capture',fence=s.resume_capture('multi')['union_fence'],reserved_at=125)
        if outcome=='DONE':s.attach_union_response(t,union_fixture.wire(s.union_request(t),body),completed_at=126)
        elif outcome=='FAILED':s.attach_union_failure(t,'TRANSPORT_ERROR',completed_at=126)


def child(research,evidence,fence,mode,q=None,go=None):
    if go is not None:go.wait(10)
    real=sqlite3.connect
    if mode in ('intent_before','completion_before'):
        class DeathConnection(sqlite3.Connection):
            def commit(self):
                table='common_bank_clock_intents' if mode=='intent_before' else 'common_bank_clock_attachments'
                if self.execute('SELECT count(*) FROM '+table).fetchone()[0]:os._exit(111 if mode=='intent_before' else 113)
                return super().commit()
        def connect(*a,**kw):return real(*a,**{**kw,'factory':DeathConnection})
        sqlite3.connect=connect
    j=CommonBankJournal(research,evidence,ApprovedSource('fixture','synthetic_fixture'))
    try:
        with j.locked() as s:
            t=s.begin_clock('capture',fence=fence,reserved_at=127)
            if mode=='intent_after':os._exit(112)
            request=s.clock_request(t)
            result=(s.attach_clock_failure(t,'TRANSPORT_ERROR',completed_at=128) if mode=='failure_after'
                    else s.attach_clock_response(t,union_fixture.wire(request,110),completed_at=128))
            if mode=='completion_after':os._exit(114)
            if mode=='failure_after':os._exit(115)
        if q is not None:q.put(('OK',result['state']))
    except Exception as exc:
        if q is not None:q.put(('BLOCKED',str(exc)))
        else:raise


@unittest.skipUnless(Path('/proc/self/mountinfo').is_file(),'Linux guarded source contract')
class ClockStageTests(unittest.TestCase):
    def setUp(self):
        self.f=journal_fixture.CommonBankJournalTests();setup(self.f);self.addCleanup(self.f.doCleanups)
        self.j=self.f.journal;self.fence=self.f.resume()['clock_fence']

    def install(self):
        with self.j.locked() as s:s.install_clock_stage()

    def begin(self,s):return s.begin_clock('capture',fence=self.fence,reserved_at=127)

    def rows(self,table='common_bank_clock_attachments'):
        with self.f.store.connect() as c:return c.execute('SELECT * FROM '+table).fetchall()

    def originals(self):
        with self.f.store.connect() as c:
            names=c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'common_bank_clock_%' AND name!='ownership_budgets' ORDER BY name").fetchall()
            return {t:c.execute('SELECT * FROM '+t).fetchall() for t, in names}

    def test_explicit_profile_preserves_all_prior_sql_and_raw_rows_no_downgrade(self):
        before=self.originals()
        with self.j.locked() as s:
            with self.assertRaisesRegex(JournalBlocked,'EXPLICIT'):self.begin(s)
            s.install_clock_stage();s.install_clock_stage();s.install_union_stage();s.install_slot_stage();s.install_genesis_attachments()
        self.assertEqual(self.originals(),before);self.assertEqual(self.f.used(),6)
        with self.f.store.connect() as c:
            for name,sql in (SCHEMA|ATTACHMENT_SCHEMA|SLOT_SCHEMA|UNION_SCHEMA).items():self.assertEqual(c.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()[0],sql)
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],module.CLOCK_VERSION)

    def test_actual_t_above_floor_original_clock_wire_and_time_conservation(self):
        self.install();before=self.originals()
        with self.j.locked() as s:
            t=self.begin(s);request=s.clock_request(t);raw=union_fixture.wire(request,110)
            self.assertEqual(json.loads(request)['method'],'getBlockTime');self.assertEqual(json.loads(request)['params'],[21])
            self.assertEqual(s.resume_capture('multi')['union_completion']['request_floor'],20)
            self.assertEqual(s.resume_capture('multi')['state'],'CLOCK_IN_FLIGHT')
            event=s.attach_clock_response(t,raw,completed_at=128)
            self.assertEqual((event['state'],event['slot'],event['block_time']),('DONE',21,110))
            self.assertEqual(event['bank_captured_at'],125);self.assertEqual(event['reserved_at'],127)
            self.assertEqual(event['ordinal'],8);self.assertEqual(s.attach_clock_response(t,raw,completed_at=128),event)
            for body,at in ((raw+b' ',128),(raw,129),(union_fixture.wire(request,111),128)):
                with self.assertRaisesRegex(JournalBlocked,'CONFLICTING'):s.attach_clock_response(t,body,completed_at=at)
        self.assertEqual(self.rows()[0][4:6],(request,raw));self.assertEqual(self.originals(),before);self.assertEqual(self.f.used(),7)
        out=self.f.resume();self.assertEqual(out['state'],'CLOCK_DONE')
        for key in ('eligible_for_trading','ownership_approval','chain_authenticated'):self.assertIs(out[key],False)
        self.assertEqual(out['provider_calls'],0)
        with self.f.store.connect() as c:self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name IN ('ownership_banks','ownership_heads')").fetchone())
        with self.j.locked() as s:
            with self.assertRaisesRegex(JournalBlocked,'ALREADY_RESERVED'):self.begin(s)
            with self.assertRaisesRegex(JournalBlocked,'BUDGET_ALREADY_BOUND'):s.create_run('multi','new-id')

    def test_strict_time_int63_bounds_null_bool_float_unknown_and_overflow(self):
        for value in (0,2**63-1,None,True,False,-1,2**63,110.0,'110',{},[],float('nan')):
            with self.subTest(value=value):
                f=journal_fixture.CommonBankJournalTests();setup(f)
                try:
                    with f.journal.locked() as s:
                        s.install_clock_stage();t=s.begin_clock('capture',fence=s.resume_capture('multi')['clock_fence'],reserved_at=127)
                        raw=union_fixture.wire(s.clock_request(t),value);event=s.attach_clock_response(t,raw,completed_at=128)
                        valid=type(value) is int and 0<=value<2**63
                        self.assertEqual(event['state'],'DONE' if valid else 'FAILED');self.assertEqual(event['block_time'],value if valid else None)
                    with f.store.connect() as c:self.assertEqual(c.execute('SELECT response_bytes FROM common_bank_clock_attachments').fetchone()[0],raw)
                    self.assertEqual(f.used(),7)
                finally:f.doCleanups()

    def test_failed_or_ambiguous_union_cannot_start_clock(self):
        for outcome in ('FAILED','PENDING'):
            with self.subTest(outcome=outcome):
                f=journal_fixture.CommonBankJournalTests();setup(f,outcome)
                try:
                    with f.journal.locked() as s:
                        s.install_clock_stage()
                        with self.assertRaisesRegex(JournalBlocked,'UNION_DONE'):s.begin_clock('capture',fence=f.fence,reserved_at=127)
                    self.assertEqual(f.used(),6)
                    with f.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM common_bank_clock_intents').fetchone()[0],0)
                finally:f.doCleanups()

    def test_caller_wrong_slot_stale_fence_summary_stage_and_expired_handle_fail(self):
        self.install()
        with self.j.locked() as s:
            with self.assertRaises(TypeError):s.begin_clock('capture',fence=self.fence,reserved_at=127,slot=20)
            with self.assertRaisesRegex(JournalBlocked,'STALE_CLOCK'):s.begin_clock('capture',fence=self.f.fence,reserved_at=127)
            for at in (True,125,2**63):
                with self.assertRaisesRegex(JournalBlocked,'TIME'):s.begin_clock('capture',fence=self.fence,reserved_at=at)
            t=self.begin(s);request=s.clock_request(t)
            with self.assertRaisesRegex(JournalBlocked,'RAW_RESPONSE'):s.attach_clock_response(t,{'result':110},completed_at=128)
            with self.assertRaisesRegex(JournalBlocked,'UNKNOWN_INFLIGHT'):s.attach_clock_response(object(),b'{}',completed_at=128)
            with self.assertRaisesRegex(JournalBlocked,'STAGE_MISMATCH'):s.union_request(t)
            with self.assertRaisesRegex(JournalBlocked,'STAGE_MISMATCH'):s.attach_slot_response(t,b'{}',completed_at=128)
        with self.assertRaisesRegex(JournalBlocked,'EXPIRED'):s.clock_request(t)
        with self.j.locked() as other:
            with self.assertRaisesRegex(JournalBlocked,'UNKNOWN_INFLIGHT'):other.attach_clock_response(t,union_fixture.wire(request,110),completed_at=128)
        self.assertEqual(self.f.resume()['state'],'TERMINAL_UNCERTAINTY');self.assertEqual(self.f.used(),7)

    def test_wrong_id_duplicate_error_extra_slot_and_invalid_json_retained_failed(self):
        for kind in ('wrong_id','duplicate','error','slot','invalid'):
            with self.subTest(kind=kind):
                f=journal_fixture.CommonBankJournalTests();setup(f)
                try:
                    with f.journal.locked() as s:
                        s.install_clock_stage();t=s.begin_clock('capture',fence=s.resume_capture('multi')['clock_fence'],reserved_at=127)
                        id=json.loads(s.clock_request(t))['id']
                        if kind=='wrong_id':raw=json.dumps({'jsonrpc':'2.0','id':'forged','result':110}).encode()
                        elif kind=='duplicate':raw=('{"jsonrpc":"2.0","id":'+json.dumps(id)+',"result":null,"result":110}').encode()
                        elif kind=='error':raw=json.dumps({'jsonrpc':'2.0','id':id,'error':{'code':-1,'message':'fixture'}}).encode()
                        elif kind=='slot':raw=json.dumps({'jsonrpc':'2.0','id':id,'result':110,'slot':20}).encode()
                        else:raw=b'\xff'
                        self.assertEqual(s.attach_clock_response(t,raw,completed_at=128)['state'],'FAILED')
                    with f.store.connect() as c:self.assertEqual(c.execute('SELECT response_bytes FROM common_bank_clock_attachments').fetchone()[0],raw)
                finally:f.doCleanups()

    def test_clock_positive_prefix_over_cap_and_redacted_failure_cannot_approve(self):
        self.install()
        with self.j.locked() as s:
            t=self.begin(s);raw=union_fixture.wire(s.clock_request(t),110);raw+=b' '*(module.MAX_RECORD_BYTES+1-len(raw))
            result=s.attach_clock_response(t,raw,completed_at=128)
            self.assertEqual((result['state'],result['reason']),('FAILED','RESPONSE_OVERSIZED'));self.assertIsNone(result['block_time'])
        row=self.rows()[0];self.assertIsNone(row[5]);failure=json.loads(row[6]);self.assertEqual(failure['observed_bytes'],len(raw))
        self.assertEqual(failure['method'],'getBlockTime');self.assertEqual(failure['request_sha256'],module._sha(row[4]))
        self.assertEqual(self.f.used(),7)

    def test_clock_transport_failure_is_bound_and_terminal_no_refund(self):
        self.install()
        with self.j.locked() as s:
            t=self.begin(s)
            with self.assertRaisesRegex(JournalBlocked,'CATEGORY'):s.attach_clock_failure(t,'URL=fixture-secret',completed_at=128)
            event=s.attach_clock_failure(t,'TRANSPORT_TIMEOUT',completed_at=128)
            self.assertEqual(event['state'],'FAILED');self.assertIsNone(event['block_time'])
            self.assertEqual(s.attach_clock_failure(t,'TRANSPORT_TIMEOUT',completed_at=128),event)
        self.assertEqual(self.f.used(),7);self.assertEqual(self.f.resume()['state'],'CLOCK_FAILED')

    def death(self,mode,code):
        self.install();ctx=multiprocessing.get_context('spawn');p=ctx.Process(target=child,args=(self.f.research,self.f.path,self.fence,mode))
        p.start();self.addCleanup(lambda:p.kill() if p.is_alive() else None);p.join(20);self.assertEqual(p.exitcode,code)

    def test_real_death_before_intent_commit_rolls_back_charge_and_pending(self):
        self.death('intent_before',111);self.assertEqual(self.f.used(),6);self.assertEqual(self.rows('common_bank_clock_intents'),[])
        self.assertEqual(self.f.resume()['state'],'UNION_DONE')
        with self.j.locked() as s:self.begin(s)
        self.assertEqual(self.f.used(),7)

    def test_real_death_after_intent_is_charged_ambiguous_without_retry(self):
        self.death('intent_after',112);self.assertEqual(self.f.used(),7);self.assertEqual(self.rows(),[])
        self.assertEqual(self.f.resume()['state'],'TERMINAL_UNCERTAINTY')
        with self.j.locked() as s:
            with self.assertRaisesRegex(JournalBlocked,'ALREADY_RESERVED'):self.begin(s)

    def test_real_death_before_completion_commit_stays_terminal_pending(self):
        self.death('completion_before',113);self.assertEqual(self.f.used(),7);self.assertEqual(self.rows(),[])
        self.assertEqual(self.f.resume()['state'],'TERMINAL_UNCERTAINTY')

    def test_real_death_after_done_commit_retains_actual_t_and_block_time(self):
        self.death('completion_after',114);self.assertEqual(self.f.used(),7)
        event=self.f.resume()['clock_completion'];self.assertEqual((event['slot'],event['block_time'],event['bank_captured_at']),(21,110,125))

    def test_real_death_after_failure_commit_preserves_failed_provenance(self):
        self.death('failure_after',115);self.assertEqual(self.f.used(),7);self.assertEqual(self.f.resume()['state'],'CLOCK_FAILED')

    def test_two_processes_race_one_clock_intent_charge_and_attachment(self):
        self.install();ctx=multiprocessing.get_context('spawn');q=ctx.Queue();go=ctx.Event()
        ps=[ctx.Process(target=child,args=(self.f.research,self.f.path,self.fence,'race',q,go)) for _ in range(2)]
        for p in ps:p.start();self.addCleanup(lambda p=p:p.kill() if p.is_alive() else None)
        go.set()
        for p in ps:p.join(20);self.assertEqual(p.exitcode,0)
        results=[q.get(timeout=3) for _ in ps];q.close();q.join_thread();self.assertEqual(sum(r[0]=='OK' for r in results),1,results)
        self.assertEqual(self.f.used(),7);self.assertEqual(len(self.rows()),1)

    def test_reservation_commit_errors_before_after_durability_mint_no_handle(self):
        self.install();real=sqlite3.connect
        with self.j.locked() as s:
            for after in (False,True):
                class ErrorConnection(sqlite3.Connection):
                    def commit(self):
                        if after:super().commit()
                        raise sqlite3.OperationalError('injected clock reservation commit')
                def connect(*a,**kw):return real(*a,**{**kw,'factory':ErrorConnection})
                with patch.object(module.sqlite3,'connect',side_effect=connect):
                    with self.assertRaisesRegex(sqlite3.OperationalError,'injected'):self.begin(s)
                self.assertEqual(self.f.used(),7 if after else 6);self.assertFalse(s._inflight)
            self.assertEqual(s.resume_capture('multi')['state'],'TERMINAL_UNCERTAINTY')

    def test_completion_commit_errors_only_identical_known_observation_can_persist(self):
        self.install();real=sqlite3.connect
        with self.j.locked() as s:
            t=self.begin(s);raw=union_fixture.wire(s.clock_request(t),110)
            for after in (False,True):
                class ErrorConnection(sqlite3.Connection):
                    def commit(self):
                        if after:super().commit()
                        raise sqlite3.OperationalError('injected clock completion commit')
                def connect(*a,**kw):return real(*a,**{**kw,'factory':ErrorConnection})
                with patch.object(module.sqlite3,'connect',side_effect=connect):
                    with self.assertRaisesRegex(sqlite3.OperationalError,'injected'):s.attach_clock_response(t,raw,completed_at=128)
                with self.assertRaisesRegex(JournalBlocked,'CONFLICTING'):s.attach_clock_response(t,raw+b' ',completed_at=128)
            self.assertEqual(s.attach_clock_response(t,raw,completed_at=128)['state'],'DONE')
        self.assertEqual(self.f.used(),7);self.assertEqual(len(self.rows()),1)

    def test_fresh_sqlite_cannot_replace_alias_delete_or_update_clock_rows(self):
        self.install()
        with self.j.locked() as s:
            t=self.begin(s);s.attach_clock_response(t,union_fixture.wire(s.clock_request(t),110),completed_at=128)
        for table in ('common_bank_clock_meta','common_bank_clock_intents','common_bank_clock_attachments'):
            with sqlite3.connect(self.f.path) as c:
                before=c.execute('SELECT * FROM '+table).fetchall();self.assertEqual(c.execute('PRAGMA recursive_triggers').fetchone()[0],0)
                for sql in ('DELETE FROM '+table,'INSERT OR REPLACE INTO '+table+' SELECT * FROM '+table,
                            'UPDATE '+table+' SET '+('id=id' if table.endswith('meta') else 'capture_id=capture_id')):
                    with self.assertRaises(sqlite3.DatabaseError):c.execute(sql)
                for alias in ('rowid','oid','_rowid_'):
                    with self.assertRaises(sqlite3.DatabaseError):c.execute('UPDATE OR REPLACE '+table+' SET '+alias+'=2')
                self.assertEqual(c.execute('SELECT * FROM '+table).fetchall(),before)
        self.assertEqual(self.f.resume()['state'],'CLOCK_DONE')

    def test_wrong_saved_request_slot_even_rehashed_and_missing_schema_refuse(self):
        self.install()
        with self.j.locked() as s:self.begin(s)
        with self.f.store.connect() as c:
            c.execute('DROP TRIGGER common_bank_clock_intents_update');raw,previous=c.execute('SELECT event_json,previous_hash FROM common_bank_clock_intents').fetchone()
            event=json.loads(raw);event['params']=[20];key=digest({'previous_hash':previous,'event':event})
            c.execute('UPDATE common_bank_clock_intents SET event_json=?,event_hash=?',(canonical(event),key));c.execute(CLOCK_SCHEMA['common_bank_clock_intents_update'])
        with self.assertRaisesRegex(JournalBlocked,'FENCE_OR_HASH'):self.f.resume()
        with self.f.store.connect() as c:c.execute('DROP TABLE common_bank_clock_meta')
        with self.j.locked() as s:
            with self.assertRaisesRegex(JournalBlocked,'SCHEMA'):s.install_clock_stage()
        self.assertEqual(self.f.used(),7)

    def test_raw_byte_corruption_after_restored_trigger_fails_audit(self):
        self.install()
        with self.j.locked() as s:
            t=self.begin(s);raw=union_fixture.wire(s.clock_request(t),110);s.attach_clock_response(t,raw,completed_at=128)
        with self.f.store.connect() as c:
            c.execute('DROP TRIGGER common_bank_clock_attachments_update');c.execute('UPDATE common_bank_clock_attachments SET response_bytes=?',(raw+b' ',));c.execute(CLOCK_SCHEMA['common_bank_clock_attachments_update'])
        with self.assertRaisesRegex(JournalBlocked,'HASH_OR_BINDING'):self.f.resume()
        self.assertEqual(self.f.used(),7)

    def test_stale_source_counter_rewind_and_rehashed_union_t_never_ack_clock(self):
        self.install()
        with self.j.locked() as s:
            t=self.begin(s);raw=union_fixture.wire(s.clock_request(t),110)
            with self.f.store.connect() as c:c.execute("UPDATE ownership_admissions SET completed_source_hash=? WHERE id='multi'",('0'*64,))
            with self.assertRaises(ValueError):s.attach_clock_response(t,raw,completed_at=128)
            self.assertEqual(self.rows(),[])
            with self.f.store.connect() as c:c.execute("UPDATE ownership_admissions SET completed_source_hash=? WHERE id='multi'",(digest(self.f.scan),))
            s.attach_clock_response(t,raw,completed_at=128)
            with self.f.store.connect() as c:c.execute("UPDATE ownership_budgets SET used=6 WHERE id='multi'")
            with self.assertRaisesRegex(JournalBlocked,'CLOCK_INTENT_INVALID'):s.resume_capture('multi')
        with self.f.store.connect() as c:
            c.execute("UPDATE ownership_budgets SET used=7 WHERE id='multi'");c.execute('DROP TRIGGER common_bank_union_attachments_update')
            event=json.loads(c.execute('SELECT event_json FROM common_bank_union_attachments').fetchone()[0]);event['slot']=22
            request,body=c.execute('SELECT request_bytes,response_bytes FROM common_bank_union_attachments').fetchone();value=json.loads(body)['result'];value['context']['slot']=22
            body=union_fixture.wire(request,value);event['response_sha256']=module._sha(body);key=digest({'previous_hash':event['intent_hash'],'event':event})
            c.execute('UPDATE common_bank_union_attachments SET event_json=?,event_hash=?,response_bytes=?',(canonical(event),key,body));c.execute(UNION_SCHEMA['common_bank_union_attachments_update'])
        with self.assertRaisesRegex(JournalBlocked,'FENCE_OR_HASH'):self.f.resume()
