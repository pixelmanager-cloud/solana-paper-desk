"""Actual guarded journal -> semantic replay, original synthetic bytes only."""
import base64
import copy
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from desk import common_bank_journal as journal
from desk import common_bank_journal_replay as replay
from desk.common_bank_journal import CommonBankJournal,JournalBlocked
from desk.common_bank_view import CommonBankError
from desk.model import canonical,digest
from desk.history import collect_history
from desk.history_progress import HistoryProgress
from desk.evidence import EvidenceStore
from desk.pool_vault_admission import GENESIS
from desk.pool_receipt_ledger import ApprovedSource
from tests import test_common_bank_journal as fixture
from tests import test_common_bank_union_stage as union_fixture


def _reader_death(research,evidence):
    j=CommonBankJournal(research,evidence,ApprovedSource('fixture','synthetic_fixture'))
    def die(*args,**kwargs):os._exit(119)
    replay._validate_account_semantics=die
    with j.locked() as s:s.replay_semantics('capture',observed_at=130)


@unittest.skipUnless(Path('/proc/self/mountinfo').is_file(),'Linux guarded journal contract')
class JournalSemanticReplayTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.CommonBankJournalTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        f=self.f;original=f.store;f.path=f.root/'semantic-evidence.sqlite';f.research=f.root/'semantic-research.sqlite'
        f.store=EvidenceStore(f.path)
        with original.connect() as c:hashes=[r[0] for r in c.execute('SELECT hash FROM pages')]
        for key in hashes:self.assertEqual(f.store.save(original.load(key)),key)
        # Fresh original bounded discovery, never rewrite an already SEALED row.
        _,coverage=collect_history(f.scan['mint'],90,120,f.fixture.h.transport,max_pages=2,
                    capture=f.store.save,token_accounts='none',slot_range={'gte':0,'lt':21})
        report=json.loads(f.scan['result']);report.pop('report_hash');report['history_queries']=[coverage]
        report['report_hash']=digest(report);f.scan['result']=canonical(report)
        with sqlite3.connect(f.research) as c:
            c.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
            c.execute('INSERT INTO scans VALUES(?,?,?,?,?)',tuple(f.scan.values()))
        f.progress=HistoryProgress(f.store);a=f.progress.admit('multi',f.descriptor)
        for _ in range(3):self.assertTrue(f.progress.reserve('multi'))
        f.progress.prepare_source('multi',a['descriptor_hash'],f.scan);f.progress.seal_source('multi',a['descriptor_hash'],digest(f.scan))
        f.journal=CommonBankJournal(f.research,f.path,f.source);f.freeze()
        with f.journal.locked() as s:
            s.install_genesis_attachments();t=s.begin_genesis('capture',fence=f.fence,reserved_at=121)
            s.attach_genesis_response(t,union_fixture.wire(s.genesis_request(t),GENESIS),completed_at=122)
            s.install_slot_stage();t=s.begin_slot('capture',fence=s.resume_capture('multi')['slot_fence'],reserved_at=123)
            s.attach_slot_response(t,union_fixture.wire(s.slot_request(t),20),completed_at=124)
        self.j=f.journal;self.values=union_fixture.values(f)

    def complete(self,values=None,slot=21,time=110,clock=True):
        with self.j.locked() as s:
            s.install_union_stage();token=s.begin_union('capture',fence=s.resume_capture('multi')['union_fence'],reserved_at=125)
            event=s.attach_union_response(token,union_fixture.wire(s.union_request(token),
                    {'context':{'slot':slot},'value':self.values if values is None else values}),completed_at=126)
            self.assertEqual(event['state'],'DONE')
            if clock:
                s.install_clock_stage();token=s.begin_clock('capture',fence=s.resume_capture('multi')['clock_fence'],reserved_at=127)
                event=s.attach_clock_response(token,union_fixture.wire(s.clock_request(token),time),completed_at=128)
                self.assertEqual(event['state'],'DONE')

    def result(self,observed_at=130):
        with self.j.locked() as s:return s.replay_semantics('capture',observed_at=observed_at)

    def changed(self,index,offset,data):
        values=copy.deepcopy(self.values);raw=bytearray(base64.b64decode(values[index]['data'][0]));raw[offset:offset+len(data)]=data
        values[index]['data'][0]=base64.b64encode(raw).decode();return values

    def bytes(self):
        return {p.name:p.read_bytes() for p in self.f.root.iterdir() if p.is_file() and not p.name.endswith('.lock')}

    def sql_update(self,table,column,value,trigger_schema):
        name=table+'_update'
        with self.f.store.connect() as c:
            c.execute('DROP TRIGGER '+name);c.execute('UPDATE '+table+' SET '+column+'=?',(value,));c.execute(trigger_schema[name])

    def test_actual_persisted_bytes_full_semantics_no_mutation_no_authority(self):
        self.complete();before=self.bytes();out=self.result();self.assertEqual(before,self.bytes())
        self.assertEqual((out['snapshot_slot'],out['request_floor'],out['snapshot_time']),(21,20,110))
        self.assertEqual((out['bank_captured_at'],out['clock_completed_at'],out['local_capture_age_seconds']),(125,128,5))
        self.assertEqual(out['plan_hash'],digest(self.f.resume()['plan']));self.assertEqual(out['requests_used'],7)
        with self.f.store.connect() as c:
            for stage,table in replay.ATTACHMENTS.items():
                req,res,key=c.execute('SELECT request_bytes,response_bytes,event_hash FROM '+table).fetchone()
                self.assertEqual(out['original_bindings'][stage]['request_sha256'],hashlib.sha256(req).hexdigest())
                self.assertEqual(out['original_bindings'][stage]['response_sha256'],hashlib.sha256(res).hexdigest())
                self.assertEqual(out['original_bindings'][stage]['completion_hash'],key)
            self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name IN ('ownership_banks','ownership_heads')").fetchone())
        self.assertEqual(out['decision'],'REJECT');self.assertTrue(out['semantic_binding_valid'])
        self.assertEqual(out['provider_calls'],0)
        for key in ('source_authenticated','finality_authenticated','history_complete','ownership_approved','eligible_for_trading','authoritative_capture_complete','private_control_proven'):
            self.assertIs(out[key],False)
        self.assertNotIn('result',out);self.assertNotIn('method',out);self.assertNotIn('manifest_hash',out)
        self.assertEqual(out['completed_source_hash'],digest(self.f.scan))
        self.assertIn('POST_CAPTURE_HISTORY_AND_LIFETIMES_UNRESOLVED',out['remaining_dependencies'])

    def test_restart_replay_identical_without_handle_reservation_or_refresh(self):
        self.complete();before=self.bytes();out=self.result()
        self.j=CommonBankJournal(self.f.research,self.f.path,ApprovedSource('fixture','synthetic_fixture'))
        self.assertEqual(self.result(),out);self.assertEqual(before,self.bytes());self.assertEqual(self.f.used(),7)
        later=self.result(1000);self.assertEqual(later['local_capture_age_seconds'],875);self.assertFalse(later['eligible_for_trading'])
        out['holder_states'].clear();self.assertTrue(self.result()['holder_states'])

    def test_syntax_done_wrong_vault_authority_is_semantic_rejection(self):
        self.complete(self.changed(4,32,b'\x02'*32))
        with self.assertRaisesRegex(CommonBankError,'VAULT_MINT_OR_AUTHORITY'):self.result()
        self.assertEqual(self.f.resume()['state'],'CLOCK_DONE')

    def test_syntax_done_wrong_holder_mint_is_semantic_rejection(self):
        self.complete(self.changed(7,0,b'\x03'*32))
        with self.assertRaisesRegex(CommonBankError,'HOLDER_BINDING_INVALID'):self.result()

    def test_syntax_done_wrong_holder_authority_is_semantic_rejection(self):
        self.complete(self.changed(7,32,b'\x03'*32))
        with self.assertRaisesRegex(CommonBankError,'HOLDER_BINDING_INVALID'):self.result()

    def test_syntax_done_swapped_mint_lp_identity_is_rejected(self):
        values=copy.deepcopy(self.values);values[1],values[3]=values[3],values[1];self.complete(values)
        with self.assertRaisesRegex(CommonBankError,'ACTIVE_MINT_AUTHORITY'):self.result()

    def test_syntax_done_wrong_pool_pda_bump_is_rejected(self):
        self.complete(self.changed(0,8,b'\x00'))
        with self.assertRaisesRegex(CommonBankError,'POOL_IDENTITY_INVALID'):self.result()

    def test_syntax_done_wrong_pool_mint_binding_is_rejected(self):
        # Pool base mint starts after discriminator,bump,index,creator.
        self.complete(self.changed(0,43,b'\x02'*32))
        with self.assertRaisesRegex(CommonBankError,'POOL_IDENTITY_INVALID'):self.result()

    def test_syntax_done_native_lamport_reserve_contradiction_is_rejected(self):
        values=copy.deepcopy(self.values);values[5]['lamports']+=1;self.complete(values)
        with self.assertRaisesRegex(CommonBankError,'NATIVE_VAULT_RESERVE'):self.result()

    def test_null_holder_explicit_unknown_no_zero_closed_or_private_label(self):
        values=copy.deepcopy(self.values);values[7]=None;self.complete(values);out=self.result()
        address=out['keys'][7];self.assertEqual(out['holder_states'][address],'ABSENT_AT_BANK_UNVERIFIED_LIFETIME')
        self.assertIn('ABSENT_HOLDER_LIFETIME_UNRESOLVED',out['remaining_dependencies'])
        self.assertFalse(out['closed_lifetimes_verified']);self.assertFalse(out['private_control_proven'])

    def test_missing_or_failed_clock_and_missing_union_refuse_without_writes(self):
        before=self.bytes()
        with self.assertRaises(JournalBlocked):self.result()
        self.assertEqual(before,self.bytes());self.complete(clock=False)
        with self.assertRaisesRegex(JournalBlocked,'CLOCK_PROFILE_REQUIRED'):self.result()
        with self.j.locked() as s:
            s.install_clock_stage();token=s.begin_clock('capture',fence=s.resume_capture('multi')['clock_fence'],reserved_at=127)
            before=self.bytes()
            with self.assertRaisesRegex(JournalBlocked,'ALL_STAGES_DONE_REQUIRED'):s.replay_semantics('capture',observed_at=130)
            self.assertEqual(before,self.bytes());s.attach_clock_failure(token,'TRANSPORT_ERROR',completed_at=128)
        before=self.bytes()
        with self.assertRaisesRegex(JournalBlocked,'ALL_STAGES_DONE_REQUIRED'):self.result()
        self.assertEqual(before,self.bytes())

    def test_original_request_slot_substitution_fails_before_semantics(self):
        self.complete()
        with self.f.store.connect() as c:request=c.execute('SELECT request_bytes FROM common_bank_clock_attachments').fetchone()[0]
        parsed=json.loads(request);parsed['params']=[20]
        self.sql_update('common_bank_clock_attachments','request_bytes',canonical(parsed).encode(),journal.CLOCK_SCHEMA)
        with patch.object(replay,'_validate_account_semantics',side_effect=AssertionError('must not run')):
            with self.assertRaisesRegex(JournalBlocked,'ORIGINAL_CLOCK_REQUEST_MISMATCH'):self.result()

    def test_original_response_bytes_corruption_cannot_hide_behind_summaries(self):
        self.complete()
        with self.f.store.connect() as c:body=c.execute('SELECT response_bytes FROM common_bank_union_attachments').fetchone()[0]
        self.sql_update('common_bank_union_attachments','response_bytes',body+b' ',journal.UNION_SCHEMA)
        with self.assertRaisesRegex(JournalBlocked,'HASH_OR_BINDING'):self.result()

    def test_rehashed_plan_source_or_past_observation_never_approves(self):
        self.complete()
        for at in (True,129.5,-1,2**63,127):
            with self.subTest(at=at):
                with self.assertRaises(JournalBlocked):self.result(at)
        with self.f.store.connect() as c:plan=json.loads(c.execute('SELECT plan_json FROM common_bank_runs').fetchone()[0])
        plan['F'].reverse();self.sql_update('common_bank_runs','plan_json',canonical(plan),journal.SCHEMA)
        with self.assertRaisesRegex(JournalBlocked,'PLAN_OR_SEED'):self.result()

    def test_changed_sealed_source_or_planned_source_rejects_without_mutation(self):
        self.complete();other=CommonBankJournal(self.f.research,self.f.path,ApprovedSource('other','synthetic_fixture'))
        with other.locked() as s:
            with self.assertRaisesRegex(JournalBlocked,'METADATA'):s.replay_semantics('capture',observed_at=130)
        with sqlite3.connect(self.f.research) as c:c.execute("UPDATE scans SET status='FAILED'")
        before=self.bytes()
        with self.assertRaisesRegex(JournalBlocked,'EXACT_SEALED_SOURCE'):self.result()
        self.assertEqual(before,self.bytes())

    def test_shared_aggregate_reference_and_byte_limits_reject_entire_result(self):
        self.complete();out=self.result();before=self.bytes();size=out['resource_usage']['serialized_bytes_charged']
        with patch.object(replay,'MAX_BYTES',size-1):
            with self.assertRaisesRegex(JournalBlocked,'BYTE_CEILING'):self.result()
        with patch.object(replay,'MAX_REFERENCES',out['resource_usage']['distinct_references']-1):
            with self.assertRaisesRegex(JournalBlocked,'REFERENCE_CEILING'):self.result()
        with patch.object(replay,'MAX_LOADS',0):
            with self.assertRaisesRegex(JournalBlocked,'LOAD_CEILING'):self.result()
        self.assertEqual(before,self.bytes())
        with patch.object(replay,'MAX_BYTES',size):self.assertEqual(self.result()['resource_usage']['serialized_bytes_charged'],size)

    def test_all_selected_second_pass_bytes_reserved_before_any_body_fetch(self):
        self.complete();required=self.result()['resource_usage']['serialized_bytes_charged']
        before=self.bytes();real_connect=sqlite3.connect;real_charge=replay._Budget.charge
        trace=[];charged=[None]
        class Cursor(sqlite3.Cursor):
            def execute(cursor,sql,parameters=()):
                cursor.query=sql
                return super().execute(sql,parameters)
            def fetchone(cursor):
                if cursor.query.startswith('SELECT length(request_bytes),length(response_bytes),typeof('):
                    trace.append(('length',id(cursor.connection)))
                elif cursor.query.startswith('SELECT request_bytes,response_bytes,event_hash FROM '):
                    trace.append(('body',id(cursor.connection),charged[0]))
                return super().fetchone()
        class Connection(sqlite3.Connection):
            def cursor(connection,*args,**kwargs):return super().cursor(*args,**{**kwargs,'factory':Cursor})
            def execute(connection,sql,parameters=()):return connection.cursor().execute(sql,parameters)
        def connect(*args,**kwargs):return real_connect(*args,**{**kwargs,'factory':Connection})
        def charge(budget,size):
            real_charge(budget,size);charged[0]=budget.bytes
        with patch.object(replay,'MAX_BYTES',required-1),patch.object(sqlite3,'connect',side_effect=connect),patch.object(replay._Budget,'charge',charge):
            with self.assertRaisesRegex(JournalBlocked,'BYTE_CEILING'):self.result()
        self.assertEqual([row[0] for row in trace],['length']*4,trace)
        self.assertEqual(len({row[1] for row in trace}),1)
        trace.clear()
        with patch.object(replay,'MAX_BYTES',required),patch.object(sqlite3,'connect',side_effect=connect),patch.object(replay._Budget,'charge',charge):
            out=self.result()
        self.assertEqual([row[0] for row in trace],['length']*4+['body']*4,trace)
        self.assertEqual(len({row[1] for row in trace}),1)
        self.assertTrue(all(row[2]==required for row in trace if row[0]=='body'),trace)
        self.assertEqual(out['resource_usage']['serialized_bytes_charged'],required)
        self.assertEqual(before,self.bytes());self.assertEqual(self.f.used(),7)

    def test_preflight_oversized_original_before_any_decoding(self):
        self.complete();self.sql_update('common_bank_clock_attachments','response_bytes',b' '* (replay.MAX_BYTES+1),journal.CLOCK_SCHEMA)
        with patch.object(journal._Session,'_audit',side_effect=AssertionError('must not traverse')):
            with self.assertRaisesRegex(JournalBlocked,'BYTE_CEILING'):self.result()

    def test_candidate_manifest_summary_or_expired_session_cannot_supply_input(self):
        self.complete()
        with self.j.locked() as s:
            with self.assertRaises(TypeError):s.replay_semantics('capture',observed_at=130,summary={'complete':True})
        with self.assertRaisesRegex(JournalBlocked,'SESSION_EXPIRED'):s.replay_semantics('capture',observed_at=130)
        with self.assertRaisesRegex(JournalBlocked,'SESSION_REQUIRED'):replay.replay_semantics(object(),'capture',observed_at=130)

    def test_evidence_read_guard_prevents_sql_writer_and_replay_never_enters_write_transaction(self):
        self.complete();before=self.bytes();real=replay._validate_account_semantics
        def probe(*args,**kwargs):
            with sqlite3.connect(self.f.path,timeout=0) as c:
                with self.assertRaises(sqlite3.OperationalError):c.execute("UPDATE ownership_budgets SET used=used WHERE id='multi'")
            return real(*args,**kwargs)
        with patch.object(replay,'_validate_account_semantics',side_effect=probe),patch.object(journal._Session,'_transaction',side_effect=AssertionError('write API forbidden')):
            out=self.result()
        self.assertTrue(out['semantic_binding_valid']);self.assertEqual(before,self.bytes())

    def test_actual_process_death_during_semantic_read_releases_guard_without_mutation(self):
        self.complete();before=self.bytes();ctx=multiprocessing.get_context('spawn')
        child=ctx.Process(target=_reader_death,args=(self.f.research,self.f.path))
        self.addCleanup(lambda:child.kill() if child.is_alive() else None);child.start();child.join(20)
        self.assertEqual(child.exitcode,119);self.assertEqual(before,self.bytes())
        self.assertTrue(self.result()['semantic_binding_valid']);self.assertEqual(self.f.used(),7)

    def test_prior_unbounded_discovery_stays_rejected_without_silent_rewriting(self):
        f=fixture.CommonBankJournalTests();union_fixture.setup(f);self.addCleanup(f.doCleanups)
        values=union_fixture.values(f)
        with f.journal.locked() as s:
            s.install_union_stage();t=s.begin_union('capture',fence=s.resume_capture('multi')['union_fence'],reserved_at=125)
            s.attach_union_response(t,union_fixture.wire(s.union_request(t),{'context':{'slot':21},'value':values}),completed_at=126)
            s.install_clock_stage();t=s.begin_clock('capture',fence=s.resume_capture('multi')['clock_fence'],reserved_at=127)
            s.attach_clock_response(t,union_fixture.wire(s.clock_request(t),110),completed_at=128)
        before=f.path.read_bytes()
        with f.journal.locked() as s:
            with self.assertRaisesRegex(CommonBankError,'DISCOVERY_RANGE_INVALID'):s.replay_semantics('capture',observed_at=130)
        self.assertEqual(before,f.path.read_bytes());self.assertEqual(f.used(),7)

    def test_rehashed_wrong_t_cannot_rebind_clock_predecessor(self):
        self.complete()
        with self.f.store.connect() as c:row=c.execute('SELECT * FROM common_bank_union_attachments').fetchone()
        capture,event_json,prior,key,request,body,failure=row
        event=json.loads(event_json);response=json.loads(body);response['result']['context']['slot']=22
        body=canonical(response).encode();event['slot']=22;event['response_sha256']=hashlib.sha256(body).hexdigest()
        key=digest({'previous_hash':prior,'event':event})
        with self.f.store.connect() as c:
            c.execute('DROP TRIGGER common_bank_union_attachments_update')
            c.execute('UPDATE common_bank_union_attachments SET event_json=?,event_hash=?,response_bytes=?',(canonical(event),key,body))
            c.execute(journal.UNION_SCHEMA['common_bank_union_attachments_update'])
        with self.assertRaisesRegex(JournalBlocked,'CLOCK_FENCE_OR_HASH'):self.result()

    def test_clock_timestamp_or_capture_age_summary_substitution_cannot_pass(self):
        self.complete()
        with self.f.store.connect() as c:row=c.execute('SELECT response_bytes,event_json FROM common_bank_clock_attachments').fetchone()
        response=json.loads(row[0]);response['result']=111
        self.sql_update('common_bank_clock_attachments','response_bytes',canonical(response).encode(),journal.CLOCK_SCHEMA)
        with self.assertRaisesRegex(JournalBlocked,'HASH_OR_BINDING'):self.result()
        self.sql_update('common_bank_clock_attachments','response_bytes',row[0],journal.CLOCK_SCHEMA)
        event=json.loads(row[1]);event['bank_captured_at']=128
        self.sql_update('common_bank_clock_attachments','event_json',canonical(event),journal.CLOCK_SCHEMA)
        with self.assertRaisesRegex(JournalBlocked,'HASH_OR_BINDING'):self.result()

    def test_missing_original_discovery_page_cannot_use_saved_verified_plan(self):
        self.complete()
        with self.f.store.connect() as c:
            report=json.loads(self.f.scan['result']);key=report['history_queries'][0]['pages'][0]['payload_hash']
            c.execute('DELETE FROM pages WHERE hash=?',(key,))
        before=self.bytes()
        with self.assertRaises(JournalBlocked):self.result()
        self.assertEqual(before,self.bytes())

    def test_blob_in_nominally_numeric_source_column_is_bounded_before_audit(self):
        self.complete()
        with sqlite3.connect(self.f.research) as c:c.execute('UPDATE scans SET created=?',(b'x'*(2*1024*1024+1),))
        before=self.bytes()
        with patch.object(journal._Session,'_audit',side_effect=AssertionError('source body must not load')):
            with self.assertRaisesRegex(JournalBlocked,'SOURCE_BOUND'):self.result()
        self.assertEqual(before,self.bytes())

    def test_oversized_counter_and_journal_ordinal_refuse_before_decode(self):
        self.complete()
        with self.f.store.connect() as c:c.execute('UPDATE ownership_budgets SET used=?',(b'x'*513,))
        before=self.bytes()
        with patch.object(journal._Session,'_audit',side_effect=AssertionError('counter must not load')):
            with self.assertRaisesRegex(JournalBlocked,'COUNTER_BOUND'):self.result()
        self.assertEqual(before,self.bytes())
        with self.f.store.connect() as c:
            c.execute('UPDATE ownership_budgets SET used=7')
            c.execute('DROP TRIGGER common_bank_events_update')
            c.execute('UPDATE common_bank_events SET ordinal=?',(b'x'*(replay.MAX_BYTES+1),))
            c.execute(journal.SCHEMA['common_bank_events_update'])
        with patch.object(journal._Session,'_audit',side_effect=AssertionError('ordinal must not load')):
            with self.assertRaisesRegex(JournalBlocked,'BYTE_CEILING'):self.result()


class SemanticResourceLatchTests(unittest.TestCase):
    def test_caught_optional_history_resource_error_still_rejects_whole_evaluation(self):
        for field,value in (('MAX_REFERENCES',0),('MAX_BYTES',0)):
            b=replay._Budget()
            with patch.object(replay,field,value):
                try:
                    if field=='MAX_REFERENCES':b.reference('optional-history')
                    else:b.charge(1)
                except ValueError:pass  # Existing account-history replay records unknown.
                with self.assertRaises(JournalBlocked):b.check()
                with self.assertRaises(JournalBlocked):b.diagnostic()


@unittest.skipUnless(Path('/proc/self/mountinfo').is_file(),'Linux guarded journal contract')
class WholeSetReplayRefusalTests(unittest.TestCase):
    def test_malformed_suffix_row_count_is_rejected_before_any_audit(self):
        f=JournalSemanticReplayTests();f.setUp();self.addCleanup(f.doCleanups);f.complete()
        with f.f.store.connect() as c:
            c.execute('DROP TRIGGER common_bank_clock_intent_insert')
            for i in range(128):
                c.execute('INSERT INTO common_bank_clock_intents VALUES(?,?,?,?)',
                    ('malformed-'+str(i),'{}','0'*64,hashlib.sha256(str(i).encode()).hexdigest()))
            c.execute(journal.CLOCK_SCHEMA['common_bank_clock_intent_insert'])
        before=f.bytes()
        with patch.object(journal._Session,'_audit',side_effect=AssertionError('no validated prefix')):
            with self.assertRaisesRegex(JournalBlocked,'ROW_CEILING'):f.result()
        self.assertEqual(before,f.bytes())
