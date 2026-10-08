"""Production union journal fixture tests; no network/provider transport."""
import base64
import copy
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from desk import common_bank_journal as module
from desk.common_bank_journal import CommonBankJournal,JournalBlocked,SCHEMA,ATTACHMENT_SCHEMA,SLOT_SCHEMA,UNION_SCHEMA
from desk.model import canonical,digest
from desk.pool_receipt_ledger import ApprovedSource
from desk.pool_vault_admission import GENESIS
from desk.security import TOKEN_2022
from tests import test_common_bank_journal as fixtures


def wire(request,result):
    return ('\n '+json.dumps({'jsonrpc':'2.0','id':json.loads(request)['id'],'result':result})+' \n').encode()


def setup(f,floor=20,slot_failure=False):
    f.setUp();f.freeze()
    with f.journal.locked() as s:
        s.install_genesis_attachments();g=s.begin_genesis('capture',fence=f.fence,reserved_at=121)
        s.attach_genesis_response(g,wire(s.genesis_request(g),GENESIS),completed_at=122)
        s.install_slot_stage();slot=s.begin_slot('capture',fence=s.resume_capture('multi')['slot_fence'],reserved_at=123)
        if slot_failure is True:s.attach_slot_failure(slot,'TRANSPORT_TIMEOUT',completed_at=124)
        elif slot_failure is not None:s.attach_slot_response(slot,wire(s.slot_request(slot),floor),completed_at=124)


def values(f):
    fixture=f.fixture
    bank=fixture.h.store.load(fixture.head['snapshot_evidence']['snapshot_hash'])
    point=fixture.h.store.load(fixture.refs.snapshot)
    states=dict(zip(point['params'][0],point['result']['value']));states.update(zip(bank['params'][0],bank['result']['value']))
    for value in states.values():value.setdefault('lamports',2039280)
    return [copy.deepcopy(states[k]) for k in f.resume()['plan']['U']]


def child(research,evidence,fence,body,mode,q=None,go=None):
    if go is not None:go.wait(10)
    real=sqlite3.connect
    if mode in ('intent_before','completion_before'):
        class DeathConnection(sqlite3.Connection):
            def commit(self):
                table='common_bank_union_intents' if mode=='intent_before' else 'common_bank_union_attachments'
                if self.execute('SELECT count(*) FROM '+table).fetchone()[0]:os._exit(101 if mode=='intent_before' else 103)
                return super().commit()
        def connect(*a,**kw):return real(*a,**{**kw,'factory':DeathConnection})
        sqlite3.connect=connect
    j=CommonBankJournal(research,evidence,ApprovedSource('fixture','synthetic_fixture'))
    try:
        with j.locked() as s:
            t=s.begin_union('capture',fence=fence,reserved_at=125)
            if mode=='intent_after':os._exit(102)
            result=(s.attach_union_failure(t,'TRANSPORT_ERROR',completed_at=126) if mode=='failure_after'
                    else s.attach_union_response(t,wire(s.union_request(t),body),completed_at=126))
            if mode=='completion_after':os._exit(104)
            if mode=='failure_after':os._exit(105)
        if q is not None:q.put(('OK',result['state']))
    except Exception as exc:
        if q is not None:q.put(('BLOCKED',str(exc)))
        else:raise


@unittest.skipUnless(Path('/proc/self/mountinfo').is_file(),'Linux guarded source contract')
class UnionStageTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.CommonBankJournalTests();setup(self.f);self.addCleanup(self.f.doCleanups)
        self.j=self.f.journal;self.fence=self.f.resume()['union_fence'];self.body={'context':{'slot':21,'apiVersion':'fixture'},'value':values(self.f)}

    def install(self):
        with self.j.locked() as s:s.install_union_stage()

    def begin(self,s):return s.begin_union('capture',fence=self.fence,reserved_at=125)

    def rows(self,table='common_bank_union_attachments'):
        with self.f.store.connect() as c:return c.execute('SELECT * FROM '+table).fetchall()

    def originals(self):
        with self.f.store.connect() as c:return {t:c.execute('SELECT * FROM '+t).fetchall() for t in
             ('pages','ownership_admissions','ownership_history','common_bank_meta','common_bank_runs','common_bank_events',
              'common_bank_attachment_meta','common_bank_attachments','common_bank_slot_meta','common_bank_slot_intents','common_bank_slot_attachments')}

    def test_explicit_additive_install_preserves_original_sql_rows_and_bytes(self):
        before=self.originals()
        with self.j.locked() as s:
            with self.assertRaisesRegex(JournalBlocked,'EXPLICIT'):self.begin(s)
            s.install_union_stage();s.install_union_stage();s.install_slot_stage();s.install_genesis_attachments()
        self.assertEqual(self.originals(),before);self.assertEqual(self.f.used(),5)
        with self.f.store.connect() as c:
            for name,sql in (SCHEMA|ATTACHMENT_SCHEMA|SLOT_SCHEMA).items():self.assertEqual(c.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()[0],sql)
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],module.UNION_VERSION)

    def test_original_full_union_request_response_t_above_floor_no_slices_or_approval(self):
        self.install();before=self.originals()
        with self.j.locked() as s:
            t=self.begin(s);request=s.union_request(t);raw=wire(request,self.body);params=json.loads(request)['params']
            self.assertEqual(params,[s.resume_capture('multi')['plan']['U'],{'encoding':'base64','commitment':'finalized','minContextSlot':20}])
            self.assertEqual(s.resume_capture('multi')['state'],'UNION_IN_FLIGHT')
            result=s.attach_union_response(t,raw,completed_at=126)
            self.assertEqual((result['state'],result['request_floor'],result['slot']),('DONE',20,21))
            self.assertEqual(result['ordinal'],6);self.assertEqual(result['fence'],self.fence)
            self.assertEqual(s.attach_union_response(t,raw,completed_at=126),result)
            for changed,at in ((raw+b' ',126),(raw,127)):
                with self.assertRaisesRegex(JournalBlocked,'CONFLICTING'):s.attach_union_response(t,changed,completed_at=at)
        self.assertEqual(self.rows()[0][4:6],(request,raw));self.assertEqual(self.originals(),before)
        self.assertEqual(self.f.used(),6);out=self.f.resume();self.assertEqual(out['state'],'UNION_DONE')
        for k in ('eligible_for_trading','ownership_approval','chain_authenticated'):self.assertIs(out[k],False)
        self.assertEqual(out['provider_calls'],0)
        with self.f.store.connect() as c:self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name IN ('ownership_banks','ownership_heads')").fetchone())

    def test_null_extra_holder_retained_without_zero_or_closure_claim(self):
        self.install();self.body['value'][7]=None
        with self.j.locked() as s:
            t=self.begin(s);raw=wire(s.union_request(t),self.body);event=s.attach_union_response(t,raw,completed_at=126)
            self.assertEqual(event['state'],'DONE');self.assertFalse(event['ownership_approval'])
        self.assertIsNone(json.loads(self.rows()[0][5])['result']['value'][7])
        self.assertEqual(len(self.f.resume()['plan']['F']),3)

    def test_context_cardinality_encoding_owner_layout_and_metadata_adversarial(self):
        def bad(body,kind):
            if kind=='floor':body['context']['slot']=19
            elif kind=='bool':body['context']['slot']=True
            elif kind=='overflow':body['context']['slot']=2**63
            elif kind=='fraction':body['context']['slot']=21.0
            elif kind=='short':body['value'].pop()
            elif kind=='extra':body['value'].append(copy.deepcopy(body['value'][7]))
            elif kind=='required_null':body['value'][4]=None
            elif kind=='owner':body['value'][7]['owner']=TOKEN_2022
            elif kind=='encoding':body['value'][7]['data'][1]='jsonParsed'
            elif kind=='base64':body['value'][7]['data'][0]='!!!!'
            elif kind=='layout':body['value'][7]['data'][0]=base64.b64encode(bytes(164)).decode()
            elif kind=='lamports':body['value'][7]['lamports']=True
            elif kind=='space':body['value'][7]['space']=166
            elif kind=='executable':body['value'][7]['executable']=True
            elif kind=='reorder':body['value'][0],body['value'][1]=body['value'][1],body['value'][0]
            elif kind=='extra_result':body['trusted']=True
        for kind in ('floor','bool','overflow','fraction','short','extra','required_null','owner','encoding','base64','layout','lamports','space','executable','reorder','extra_result'):
            with self.subTest(kind=kind):
                f=fixtures.CommonBankJournalTests();setup(f)
                try:
                    b={'context':{'slot':21},'value':values(f)};bad(b,kind)
                    with f.journal.locked() as s:
                        s.install_union_stage();t=s.begin_union('capture',fence=s.resume_capture('multi')['union_fence'],reserved_at=125)
                        raw=wire(s.union_request(t),b);event=s.attach_union_response(t,raw,completed_at=126)
                        self.assertEqual(event['state'],'FAILED');self.assertIsNone(event['slot'])
                    with f.store.connect() as c:self.assertEqual(c.execute('SELECT response_bytes FROM common_bank_union_attachments').fetchone()[0],raw)
                    self.assertEqual(f.used(),6)
                finally:f.doCleanups()

    def test_failed_or_ambiguous_slot_and_unsupported_floor_cannot_transition(self):
        for failure,floor in ((True,20),(None,20),(False,2**63)):
            with self.subTest(failure=failure,floor=floor):
                f=fixtures.CommonBankJournalTests();setup(f,floor,failure)
                try:
                    with f.journal.locked() as s:
                        s.install_union_stage()
                        with self.assertRaises(JournalBlocked):s.begin_union('capture',fence=s.resume_capture('multi')['union_fence'],reserved_at=125)
                    self.assertEqual(f.used(),5)
                    with f.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM common_bank_union_intents').fetchone()[0],0)
                finally:f.doCleanups()

    def test_forged_summary_stale_fence_cross_stage_and_reopened_handle_rejected(self):
        self.install()
        with self.j.locked() as s:
            with self.assertRaisesRegex(JournalBlocked,'STALE_UNION'):s.begin_union('capture',fence=self.f.fence,reserved_at=125)
            t=self.begin(s);request=s.union_request(t)
            with self.assertRaisesRegex(JournalBlocked,'RAW_RESPONSE'):s.attach_union_response(t,self.body,completed_at=126)
            with self.assertRaisesRegex(JournalBlocked,'UNKNOWN_INFLIGHT'):s.attach_union_response(object(),wire(request,self.body),completed_at=126)
            with self.assertRaisesRegex(JournalBlocked,'STAGE_MISMATCH'):s.slot_request(t)
        with self.assertRaisesRegex(JournalBlocked,'EXPIRED'):s.union_request(t)
        with self.j.locked() as other:
            with self.assertRaisesRegex(JournalBlocked,'UNKNOWN_INFLIGHT'):other.attach_union_response(t,wire(request,self.body),completed_at=126)
            with self.assertRaisesRegex(JournalBlocked,'ALREADY_RESERVED'):self.begin(other)
        self.assertEqual(self.f.resume()['state'],'TERMINAL_UNCERTAINTY');self.assertEqual(self.f.used(),6)

    def test_positive_prefix_oversize_never_accepted(self):
        self.install()
        with self.j.locked() as s:
            t=self.begin(s);raw=wire(s.union_request(t),self.body);raw+=b' '*(module.MAX_RECORD_BYTES+1-len(raw))
            event=s.attach_union_response(t,raw,completed_at=126)
            self.assertEqual(event['state'],'FAILED');self.assertEqual(event['reason'],'RESPONSE_OVERSIZED')
        row=self.rows()[0];self.assertIsNone(row[5]);failure=json.loads(row[6]);self.assertEqual(failure['observed_bytes'],len(raw))
        self.assertEqual(failure['request_sha256'],module._sha(row[4]));self.assertEqual(self.f.used(),6)

    def test_raw_utf8_under_wire_cap_but_over_canonical_parent_cap_is_retained_failed(self):
        self.install();self.body['value'][7]['diagnostic']='é'*11000
        with self.j.locked() as s:
            t=self.begin(s);request=s.union_request(t)
            raw=json.dumps({'jsonrpc':'2.0','id':json.loads(request)['id'],'result':self.body},ensure_ascii=False).encode()
            self.assertLess(len(raw),module.MAX_RECORD_BYTES)
            event=s.attach_union_response(t,raw,completed_at=126)
            self.assertEqual((event['state'],event['reason']),('FAILED','UNION_PARENT_BOUND'))
        self.assertEqual(self.rows()[0][5],raw);self.assertEqual(self.f.used(),6)

    def test_wrong_id_duplicate_fields_error_and_nonobject_framing_preserved(self):
        for kind in ('wrong_id','duplicate','error','null'):
            with self.subTest(kind=kind):
                f=fixtures.CommonBankJournalTests();setup(f)
                try:
                    b={'context':{'slot':21},'value':values(f)}
                    with f.journal.locked() as s:
                        s.install_union_stage();t=s.begin_union('capture',fence=s.resume_capture('multi')['union_fence'],reserved_at=125)
                        request=s.union_request(t);id=json.loads(request)['id']
                        if kind=='wrong_id':raw=json.dumps({'jsonrpc':'2.0','id':'forged','result':b}).encode()
                        elif kind=='duplicate':raw=('{"jsonrpc":"2.0","id":'+json.dumps(id)+',"result":null,"result":'+json.dumps(b)+'}').encode()
                        elif kind=='error':raw=json.dumps({'jsonrpc':'2.0','id':id,'error':{'code':-1,'message':'fixture'}}).encode()
                        else:raw=b'null'
                        self.assertEqual(s.attach_union_response(t,raw,completed_at=126)['state'],'FAILED')
                    with f.store.connect() as c:self.assertEqual(c.execute('SELECT response_bytes FROM common_bank_union_attachments').fetchone()[0],raw)
                    self.assertEqual(f.used(),6)
                finally:f.doCleanups()

    def test_stale_source_and_counter_rewind_cannot_acknowledge_union(self):
        self.install()
        with self.j.locked() as s:
            t=self.begin(s);raw=wire(s.union_request(t),self.body)
            with self.f.store.connect() as c:c.execute("UPDATE ownership_admissions SET completed_source_hash=? WHERE id='multi'",('0'*64,))
            with self.assertRaises(ValueError):s.attach_union_response(t,raw,completed_at=126)
            self.assertEqual(self.rows(),[])
            with self.f.store.connect() as c:c.execute("UPDATE ownership_admissions SET completed_source_hash=? WHERE id='multi'",(digest(self.f.scan),))
            s.attach_union_response(t,raw,completed_at=126)
            with self.f.store.connect() as c:c.execute("UPDATE ownership_budgets SET used=5 WHERE id='multi'")
            with self.assertRaisesRegex(JournalBlocked,'UNION_INTENT_INVALID'):s.resume_capture('multi')

    def test_transport_failure_bound_to_full_original_request_no_refund(self):
        self.install()
        with self.j.locked() as s:
            t=self.begin(s);event=s.attach_union_failure(t,'TRANSPORT_TIMEOUT',completed_at=126)
            self.assertEqual(event['state'],'FAILED');self.assertIsNone(event['slot'])
            self.assertEqual(s.attach_union_failure(t,'TRANSPORT_TIMEOUT',completed_at=126),event)
            with self.assertRaisesRegex(JournalBlocked,'CONFLICTING'):s.attach_union_response(t,wire(s.union_request(t),self.body),completed_at=126)
        self.assertEqual(self.f.used(),6);self.assertEqual(self.f.resume()['state'],'UNION_FAILED')
        with self.j.locked() as s:
            with self.assertRaisesRegex(JournalBlocked,'BUDGET_ALREADY_BOUND'):s.create_run('multi','new-capture')

    def death(self,mode,code):
        self.install();ctx=multiprocessing.get_context('spawn');p=ctx.Process(target=child,args=(self.f.research,self.f.path,self.fence,self.body,mode))
        p.start();self.addCleanup(lambda:p.kill() if p.is_alive() else None);p.join(20);self.assertEqual(p.exitcode,code)

    def test_real_death_before_intent_commit_rolls_back_counter_and_intent(self):
        self.death('intent_before',101);self.assertEqual(self.f.used(),5);self.assertEqual(self.rows('common_bank_union_intents'),[])
        self.assertEqual(self.f.resume()['state'],'SLOT_DONE')
        with self.j.locked() as s:self.begin(s)
        self.assertEqual(self.f.used(),6)

    def test_real_death_after_intent_is_terminal_charged_no_retry(self):
        self.death('intent_after',102);self.assertEqual(self.f.used(),6);self.assertEqual(self.rows(),[])
        self.assertEqual(self.f.resume()['state'],'TERMINAL_UNCERTAINTY')

    def test_real_death_before_completion_commit_keeps_terminal_pending(self):
        self.death('completion_before',103);self.assertEqual(self.f.used(),6);self.assertEqual(self.rows(),[])
        self.assertEqual(self.f.resume()['state'],'TERMINAL_UNCERTAINTY')

    def test_real_death_after_completion_preserves_actual_t_and_floor(self):
        self.death('completion_after',104);self.assertEqual(self.f.used(),6)
        event=self.f.resume()['union_completion'];self.assertEqual((event['slot'],event['request_floor']),(21,20))

    def test_real_death_after_failure_preserves_redacted_observation(self):
        self.death('failure_after',105);self.assertEqual(self.f.used(),6);self.assertEqual(self.f.resume()['state'],'UNION_FAILED')

    def test_two_process_race_one_union_charge_and_attachment(self):
        self.install();ctx=multiprocessing.get_context('spawn');q=ctx.Queue();go=ctx.Event()
        ps=[ctx.Process(target=child,args=(self.f.research,self.f.path,self.fence,self.body,'race',q,go)) for _ in range(2)]
        for p in ps:p.start();self.addCleanup(lambda p=p:p.kill() if p.is_alive() else None)
        go.set()
        for p in ps:p.join(20);self.assertEqual(p.exitcode,0)
        results=[q.get(timeout=3) for _ in ps];q.close();q.join_thread();self.assertEqual(sum(r[0]=='OK' for r in results),1,results)
        self.assertEqual(self.f.used(),6);self.assertEqual(len(self.rows()),1)

    def test_completion_commit_errors_preserve_only_identical_known_observation(self):
        self.install();real=sqlite3.connect
        with self.j.locked() as s:
            t=self.begin(s);raw=wire(s.union_request(t),self.body)
            for after in (False,True):
                class ErrorConnection(sqlite3.Connection):
                    def commit(self):
                        if after:super().commit()
                        raise sqlite3.OperationalError('injected union completion commit')
                def connect(*a,**kw):return real(*a,**{**kw,'factory':ErrorConnection})
                with patch.object(module.sqlite3,'connect',side_effect=connect):
                    with self.assertRaisesRegex(sqlite3.OperationalError,'injected'):s.attach_union_response(t,raw,completed_at=126)
                with self.assertRaisesRegex(JournalBlocked,'CONFLICTING'):s.attach_union_response(t,raw+b' ',completed_at=126)
            self.assertEqual(s.attach_union_response(t,raw,completed_at=126)['state'],'DONE')
        self.assertEqual(self.f.used(),6);self.assertEqual(len(self.rows()),1)

    def test_fresh_sqlite_replace_rowid_deletion_and_corrupt_original_bytes_rejected(self):
        self.install()
        with self.j.locked() as s:
            t=self.begin(s);raw=wire(s.union_request(t),self.body);s.attach_union_response(t,raw,completed_at=126)
        for table in ('common_bank_union_meta','common_bank_union_intents','common_bank_union_attachments'):
            with sqlite3.connect(self.f.path) as c:
                before=c.execute('SELECT * FROM '+table).fetchall()
                for sql in ('DELETE FROM '+table,'INSERT OR REPLACE INTO '+table+' SELECT * FROM '+table,
                            'UPDATE '+table+' SET '+('id=id' if table.endswith('meta') else 'capture_id=capture_id')):
                    with self.assertRaises(sqlite3.DatabaseError):c.execute(sql)
                for alias in ('rowid','oid','_rowid_'):
                    with self.assertRaises(sqlite3.DatabaseError):c.execute('UPDATE OR REPLACE '+table+' SET '+alias+'=2')
                self.assertEqual(c.execute('SELECT * FROM '+table).fetchall(),before)
        with self.f.store.connect() as c:
            c.execute('DROP TRIGGER common_bank_union_attachments_update');c.execute('UPDATE common_bank_union_attachments SET response_bytes=?',(raw+b' ',))
            c.execute(UNION_SCHEMA['common_bank_union_attachments_update'])
        with self.assertRaisesRegex(JournalBlocked,'HASH_OR_BINDING'):self.f.resume()
        self.assertEqual(self.f.used(),6)

    def test_missing_profile_metadata_and_rehashed_slot_predecessor_never_rebind(self):
        self.install()
        with self.j.locked() as s:self.begin(s)
        with self.f.store.connect() as c:
            c.execute('DROP TRIGGER common_bank_slot_attachments_update');event=json.loads(c.execute('SELECT event_json FROM common_bank_slot_attachments').fetchone()[0])
            event['slot']=19;request=c.execute('SELECT request_bytes FROM common_bank_slot_attachments').fetchone()[0];raw=wire(request,19);event['response_sha256']=module._sha(raw)
            key=digest({'previous_hash':event['intent_hash'],'event':event})
            c.execute('UPDATE common_bank_slot_attachments SET event_json=?,event_hash=?,response_bytes=?',(canonical(event),key,raw));c.execute(SLOT_SCHEMA['common_bank_slot_attachments_update'])
        with self.assertRaisesRegex(JournalBlocked,'FENCE_OR_HASH'):self.f.resume()
        with self.f.store.connect() as c:c.execute('DROP TABLE common_bank_union_meta')
        with self.j.locked() as s:
            with self.assertRaisesRegex(JournalBlocked,'SCHEMA'):s.install_union_stage()
        self.assertEqual(self.f.used(),6)
