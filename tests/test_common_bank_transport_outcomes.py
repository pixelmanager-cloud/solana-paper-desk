"""Original-byte outcomes against real guarded journal fixtures; no transport I/O."""
from contextlib import contextmanager
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import types
import unittest
from unittest.mock import patch

from desk import common_bank_journal as m
from desk import common_bank_completion_inventory as inventory
from desk.model import canonical, digest
from desk.original_byte_slot_transport import SlotByteExchange
from desk.pool_vault_admission import GENESIS
from tests import test_common_bank_completion_inventory as fixture
from tests import test_common_bank_union_stage as union_fixture
from tests import test_common_bank_journal_replay as replay_fixture

STAGES = ('genesis', 'slot', 'union', 'clock')
TABLES = ('common_bank_attachments', 'common_bank_slot_attachments',
          'common_bank_union_attachments', 'common_bank_clock_attachments')


def begin(s, stage):
    state=s.resume_capture('multi')
    fence=state['fence' if stage=='genesis' else stage+'_fence']
    return getattr(s,'begin_'+stage)('capture',fence=fence,reserved_at=121+2*STAGES.index(stage))


def wire(s, token, stage, values):
    request=getattr(s,stage+'_request')(token)
    result={'genesis':GENESIS,'slot':20,'union':{'context':{'slot':21},'value':values},'clock':110}[stage]
    return request,union_fixture.wire(request,result)


def death(research,evidence,stage,mode):
    j=m.CommonBankJournal(research,evidence,m.ApprovedSource('fixture','synthetic_fixture'))
    real=sqlite3.connect
    if mode=='before_commit':
        class DeathConnection(sqlite3.Connection):
            def commit(self):
                table=TABLES[STAGES.index(stage)]
                if self.execute('SELECT count(*) FROM '+table).fetchone()[0]:os._exit(141)
                return super().commit()
        def connect(*args,**kwargs):return real(*args,**{**kwargs,'factory':DeathConnection})
        sqlite3.connect=connect
    with j.locked() as s:
        token=begin(s,stage);request=getattr(s,stage+'_request')(token)
        # A valid-looking body must remain failed even after durable restart.
        raw=union_fixture.wire(request,GENESIS if stage=='genesis' else 110)
        getattr(s,'attach_'+stage+'_outcome')(token,SlotByteExchange(request,raw,'RESPONSE_TRUNCATED'),
                                          completed_at=122+2*STAGES.index(stage))
        os._exit(142)


@unittest.skipUnless(inventory.read_platform_available(),'Linux LP64 guarded original outcome contract')
class TransportOutcomeTests(unittest.TestCase):
    @contextmanager
    def prepared(self, stage, *, install=True, prior_v2=True):
        case=fixture.CompletionInventoryTests();case.setUp()
        try:
            f=case.f;f.freeze()
            with f.journal.locked() as s:
                s.install_genesis_attachments();s.install_slot_stage();s.install_union_stage();s.install_clock_stage()
                if install:s.install_transport_outcomes()
            values=union_fixture.values(f)
            with f.journal.locked() as s:
                for previous in STAGES[:STAGES.index(stage)]:
                    t=begin(s,previous);request,raw=wire(s,t,previous,values)
                    if install and prior_v2:
                        event=getattr(s,'attach_'+previous+'_outcome')(t,SlotByteExchange(request,raw,None),
                                                           completed_at=122+2*STAGES.index(previous))
                    else:
                        event=getattr(s,'attach_'+previous+'_response')(t,raw,
                                                           completed_at=122+2*STAGES.index(previous))
                    self.assertEqual(event['state'],'DONE')
            yield case,values
        finally:case.doCleanups()

    def row(self,f,stage):
        with f.store.connect() as c:return c.execute('SELECT * FROM '+TABLES[STAGES.index(stage)]).fetchone()

    def complete(self,s,t,stage,request,body,code):
        return getattr(s,'attach_'+stage+'_outcome')(t,SlotByteExchange(request,body,code),
                                            completed_at=122+2*STAGES.index(stage))

    def test_valid_looking_truncated_body_all_stages_failed_exact_bytes_and_no_values(self):
        for stage in STAGES:
            with self.subTest(stage=stage),self.prepared(stage) as (case,values):
                f=case.f;original=f.originals()
                with f.journal.locked() as s:
                    t=begin(s,stage);request,raw=wire(s,t,stage,values)
                    event=self.complete(s,t,stage,request,raw,'RESPONSE_TRUNCATED')
                    self.assertEqual(event['kind'],f'common_bank_{stage}_attachment_v2')
                    self.assertEqual((event['state'],event['reason']),('FAILED','RESPONSE_TRUNCATED'))
                    for flag in ('eligible_for_trading','ownership_approval','chain_authenticated'):
                        self.assertIs(event[flag],False)
                    if stage in ('slot','union'):self.assertIsNone(event['slot'])
                    if stage=='clock':self.assertIsNone(event['block_time']);self.assertEqual(event['slot'],21)
                    self.assertEqual(self.complete(s,t,stage,request,raw,'RESPONSE_TRUNCATED'),event)
                    for body,code in ((raw+b' ','RESPONSE_TRUNCATED'),(raw,'TRANSPORT_ERROR'),(raw,None)):
                        with self.assertRaisesRegex(m.JournalBlocked,'CONFLICTING'):
                            self.complete(s,t,stage,request,body,code)
                row=self.row(f,stage)
                self.assertEqual(row[4:6],(request,raw))
                failure=json.loads(row[6])
                self.assertEqual(failure,{'kind':'common_bank_transport_failure_v2','stage':stage,
                    'method':json.loads(request)['method'],'category':'RESPONSE_TRUNCATED',
                    'request_sha256':hashlib.sha256(request).hexdigest()})
                self.assertEqual(f.originals(),original);self.assertEqual(f.used(),4+STAGES.index(stage))
                self.assertEqual(f.resume()['state'],stage.upper()+'_FAILED')
                read=case.read();self.assertIn('FAILED_ATTEMPT_RETAINED',read.blockers)
                stored=case.table(read,TABLES[STAGES.index(stage)])[0]
                self.assertEqual(stored[4:6],(request,raw))
                with f.journal.locked() as s:
                    with self.assertRaisesRegex(m.JournalBlocked,'UNKNOWN_INFLIGHT'):
                        self.complete(s,t,stage,request,raw,'RESPONSE_TRUNCATED')
                    with self.assertRaises(m.JournalBlocked):begin(s,stage)
                    with self.assertRaisesRegex(m.JournalBlocked,'ALL_STAGES_DONE'):
                        s.replay_semantics('capture',observed_at=130)

    def test_missing_empty_and_unobserved_oversized_bodies_stay_distinct(self):
        for stage in STAGES:
            for body,code,reason in ((None,'TRANSPORT_ERROR','TRANSPORT_ERROR'),
                                     (b'','RESPONSE_TRUNCATED','RESPONSE_TRUNCATED'),
                                     (b'',None,'RESPONSE_INVALID'),
                                     (None,'RESPONSE_OVERSIZED','RESPONSE_OVERSIZED')):
                with self.subTest(stage=stage,body=body,code=code),self.prepared(stage) as (case,values):
                    f=case.f
                    with f.journal.locked() as s:
                        t=begin(s,stage);request,_=wire(s,t,stage,values)
                        event=self.complete(s,t,stage,request,body,code)
                        self.assertEqual((event['state'],event['reason']),('FAILED',reason))
                        self.assertEqual(event['response_sha256'],None if body is None else hashlib.sha256(body).hexdigest())
                    self.assertEqual(self.row(f,stage)[5],body)
                    failure=self.row(f,stage)[6]
                    self.assertEqual(failure is None,code is None)
                    if failure:self.assertNotIn('observed_bytes',json.loads(failure))
                    self.assertIn('FAILED_ATTEMPT_RETAINED',case.read().blockers)

    def test_all_fixed_transport_codes_fail_even_on_valid_bodies(self):
        for stage in STAGES:
            with self.prepared(stage) as (case,values),case.f.journal.locked() as s:
                t=begin(s,stage);request,raw=wire(s,t,stage,values)
                for code in m.TRANSPORT_FAILURE_CODES:
                    with self.subTest(stage=stage,code=code):
                        failure=m._Session._transport_failure(request,code,stage)
                        result=(m._Session._union_outcome({},request,raw,failure,transport=True) if stage=='union'
                                else m._Session._clock_outcome({},request,raw,failure,transport=True) if stage=='clock'
                                else m._Session._outcome(request,raw,failure,stage,transport=True))
                        self.assertEqual(result[:2],('FAILED',code))
                        if len(result)==3:self.assertIsNone(result[2])

    def test_invalid_outcome_type_binding_code_or_overcap_body_never_persists(self):
        for stage in STAGES:
            with self.prepared(stage) as (case,values),case.f.journal.locked() as s:
                t=begin(s,stage);request,raw=wire(s,t,stage,values)
                for bad in ({'request_bytes':request,'response_bytes':raw,'failure_code':None},
                            SlotByteExchange(request+b' ',raw,None),SlotByteExchange(request,None,None),
                            SlotByteExchange(request,bytearray(raw),'TRANSPORT_ERROR'),
                            SlotByteExchange(request,b'x'*(m.MAX_RESPONSE_BYTES+1),'RESPONSE_OVERSIZED'),
                            SlotByteExchange(request,raw,'provider URL secret'),SlotByteExchange(request,raw,True)):
                    with self.subTest(stage=stage,bad_type=type(bad).__name__):
                        with self.assertRaises(m.JournalBlocked):
                            getattr(s,'attach_'+stage+'_outcome')(t,bad,completed_at=122+2*STAGES.index(stage))
                self.assertIsNone(self.row(case.f,stage))
                self.assertEqual(case.f.used(),4+STAGES.index(stage))
                self.assertEqual(self.complete(s,t,stage,request,raw,'TRANSPORT_ERROR')['state'],'FAILED')

    def test_explicit_profile_preserves_all_v1_rows_sql_and_counters_no_downgrade(self):
        with self.prepared('clock',install=False) as (case,values):
            f=case.f
            with f.store.connect() as c:
                sql=c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
                names=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                originals={n:c.execute('SELECT * FROM '+n).fetchall() for n in names}
            with f.journal.locked() as s:
                t=begin(s,'clock');request,raw=wire(s,t,'clock',values)
                with self.assertRaisesRegex(m.JournalBlocked,'EXPLICIT_TRANSPORT'):
                    self.complete(s,t,'clock',request,raw,'RESPONSE_TRUNCATED')
                with f.store.connect() as c:
                    # Newly charged intent is original too; freeze after reservation.
                    originals['ownership_budgets']=c.execute('SELECT * FROM ownership_budgets').fetchall()
                    originals['common_bank_clock_intents']=c.execute('SELECT * FROM common_bank_clock_intents').fetchall()
                s.install_transport_outcomes();s.install_transport_outcomes()
                s.install_clock_stage();s.install_union_stage();s.install_slot_stage();s.install_genesis_attachments()
                with f.store.connect() as c:
                    self.assertEqual(c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall(),sql)
                    for n,rows in originals.items():self.assertEqual(c.execute('SELECT * FROM '+n).fetchall(),rows,n)
                    self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],m.TRANSPORT_VERSION)
                self.assertEqual(self.complete(s,t,'clock',request,raw,'RESPONSE_TRUNCATED')['state'],'FAILED')
            for stage in STAGES[:3]:self.assertTrue(json.loads(self.row(f,stage)[1])['kind'].endswith('_v1'))
            self.assertTrue(json.loads(self.row(f,'clock')[1])['kind'].endswith('_v2'))
            self.assertEqual(f.used(),7);self.assertIn('FAILED_ATTEMPT_RETAINED',case.read().blockers)

    def test_install_refuses_incomplete_profiles_without_initialization_or_charge(self):
        case=fixture.CompletionInventoryTests();self.addCleanup(case.doCleanups);case.setUp()
        case.f.freeze()
        with case.f.journal.locked() as s:
            with self.assertRaisesRegex(m.JournalBlocked,'FULL_CLOCK'):s.install_transport_outcomes()
        self.assertEqual(case.f.used(),3)
        with case.f.store.connect() as c:self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],0)

    def test_old_binary_refuses_new_marker_and_downgraded_v2_records(self):
        # Execute exact accepted base reader code; no network fetch or PR97 source.
        # Pinned source snapshots work in shallow CI checkouts with no Git/network dependency.
        hashes={'common_bank_journal': '978c8503073c5acc393f7a5e0bba8743826bb1de31769907164de36952f1de95', 'common_bank_completion_inventory': 'ccfcab3ac6b584307fcc24ed827c01d1842e1c9662c4b2ac0cc864072082baa1'}
        def old_module(filename):
            path=Path(__file__).parent/'fixtures/common_bank_transport_outcomes'/(filename+'.py.txt')
            raw=path.read_bytes();self.assertLessEqual(len(raw),128*1024)
            self.assertEqual(hashlib.sha256(raw).hexdigest(),hashes[filename])
            mod=types.ModuleType('desk._old_'+filename);mod.__package__='desk'
            exec(compile(raw,'accepted-base/'+filename+'.py','exec'),mod.__dict__)
            return mod
        old=old_module('common_bank_journal');old_inventory=old_module('common_bank_completion_inventory')
        # Freeze old inventory's journal constants against the accepted old module.
        old_inventory.journal=old
        for code in ('RESPONSE_TRUNCATED',None):
            with self.prepared('genesis') as (case,values):
                f=case.f
                with f.journal.locked() as s:
                    t=begin(s,'genesis');request,raw=wire(s,t,'genesis',values)
                    self.complete(s,t,'genesis',request,raw,code)
                old_pins=old_inventory.InventoryPins(**vars(case.pins))
                old_j=old.CommonBankJournal(f.research,f.path,f.source)
                with old_j.locked() as s:
                    with self.assertRaisesRegex(old.JournalBlocked,'VERSION_UNKNOWN'):s.resume_capture('multi')
                with self.assertRaisesRegex(old_inventory.InventoryUnavailable,'JOURNAL_VERSION'):
                    old_inventory.read_completion_inventory(old_pins)
                with f.store.connect() as c:c.execute(f'PRAGMA user_version={m.CLOCK_VERSION}')
                with f.journal.locked() as s:
                    with self.assertRaisesRegex(m.JournalBlocked,'PROFILE_MISMATCH'):s.resume_capture('multi')
                with old_j.locked() as s:
                    with self.assertRaises(old.JournalBlocked):s.resume_capture('multi')
                with self.assertRaises(inventory.InventoryUnavailable):case.read()
                with self.assertRaises(old_inventory.InventoryUnavailable):old_inventory.read_completion_inventory(old_pins)
                self.assertEqual(f.used(),4)

    def test_before_and_after_commit_errors_only_identical_outcome_can_persist(self):
        real=sqlite3.connect
        for stage in STAGES:
            with self.subTest(stage=stage),self.prepared(stage) as (case,values),case.f.journal.locked() as s:
                t=begin(s,stage);request,raw=wire(s,t,stage,values)
                for after in (False,True):
                    class ErrorConnection(sqlite3.Connection):
                        def commit(self):
                            if after:super().commit()
                            raise sqlite3.OperationalError('fixture completion acknowledgment loss')
                    def connect(*a,**kw):return real(*a,**{**kw,'factory':ErrorConnection})
                    with patch.object(m.sqlite3,'connect',side_effect=connect):
                        with self.assertRaisesRegex(sqlite3.OperationalError,'fixture completion'):
                            self.complete(s,t,stage,request,raw,'RESPONSE_TRUNCATED')
                    with self.assertRaisesRegex(m.JournalBlocked,'CONFLICTING'):
                        self.complete(s,t,stage,request,raw,None)
                self.assertEqual(self.complete(s,t,stage,request,raw,'RESPONSE_TRUNCATED')['state'],'FAILED')
                self.assertEqual(case.f.used(),4+STAGES.index(stage))

    def test_process_death_retains_terminal_charged_pending_or_failed_original(self):
        for stage in ('genesis','clock'):
            for mode,exitcode in (('before_commit',141),('after_commit',142)):
                with self.subTest(stage=stage,mode=mode),self.prepared(stage) as (case,values):
                    f=case.f
                    p=multiprocessing.get_context('fork').Process(target=death,args=(f.research,f.path,stage,mode))
                    p.start();p.join(20);self.assertFalse(p.is_alive());self.assertEqual(p.exitcode,exitcode)
                    self.assertEqual(f.used(),4+STAGES.index(stage))
                    self.assertEqual(f.resume()['state'],'TERMINAL_UNCERTAINTY' if mode=='before_commit' else stage.upper()+'_FAILED')
                    with f.journal.locked() as s:
                        with self.assertRaises(m.JournalBlocked):begin(s,stage)
                    row=self.row(f,stage)
                    if mode=='before_commit':self.assertIsNone(row)
                    else:self.assertEqual(json.loads(row[6])['category'],'RESPONSE_TRUNCATED');self.assertIsInstance(row[5],bytes)

    def test_successful_mixed_chain_replay_remains_diagnostic_and_read_only(self):
        case=replay_fixture.JournalSemanticReplayTests();self.addCleanup(case.doCleanups);case.setUp()
        case.complete(clock=False)
        f=case.f
        with f.journal.locked() as s:
            s.install_clock_stage();s.install_transport_outcomes()
            t=begin(s,'clock');request=s.clock_request(t);raw=union_fixture.wire(request,110)
            event=self.complete(s,t,'clock',request,raw,None);self.assertEqual(event['state'],'DONE')
            before=hashlib.sha256(f.path.read_bytes()).hexdigest()
            out=s.replay_semantics('capture',observed_at=130)
            self.assertEqual(hashlib.sha256(f.path.read_bytes()).hexdigest(),before)
            self.assertEqual(out['decision'],'REJECT');self.assertTrue(out['semantic_binding_valid'])
            for key in ('source_authenticated','finality_authenticated','eligible_for_trading','authoritative_capture_complete'):
                self.assertIs(out[key],False)
            self.assertEqual(out['requests_used'],7)

    def test_shared18_exhaustion_does_not_refund_or_drop_reserved_completion(self):
        with self.prepared('genesis') as (case,values),case.f.journal.locked() as s:
            f=case.f;t=begin(s,'genesis');request,raw=wire(s,t,'genesis',values)
            while f.progress.reserve('multi'):pass
            self.assertEqual(f.used(),18)
            self.assertEqual(self.complete(s,t,'genesis',request,raw,'RESPONSE_TRUNCATED')['state'],'FAILED')
            self.assertEqual(self.complete(s,t,'genesis',request,raw,'RESPONSE_TRUNCATED')['state'],'FAILED')
            self.assertEqual(f.used(),18);self.assertFalse(f.progress.reserve('multi'))
            with self.assertRaises(m.JournalBlocked):begin(s,'genesis')
            with self.assertRaises(m.JournalBlocked):begin(s,'slot')
            self.assertEqual(f.used(),18)

    def test_profile_transition_commit_failure_is_atomic_and_does_not_charge(self):
        real=sqlite3.connect
        for after in (False,True):
            with self.subTest(after=after),self.prepared('genesis',install=False) as (case,values):
                f=case.f;original=f.originals()
                class ErrorConnection(sqlite3.Connection):
                    def commit(self):
                        if after:super().commit()
                        raise sqlite3.OperationalError('fixture marker acknowledgment loss')
                def connect(*args,**kwargs):return real(*args,**{**kwargs,'factory':ErrorConnection})
                with f.journal.locked() as s:
                    with patch.object(m.sqlite3,'connect',side_effect=connect):
                        with self.assertRaisesRegex(sqlite3.OperationalError,'fixture marker'):
                            s.install_transport_outcomes()
                    with f.store.connect() as c:
                        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],
                                         m.TRANSPORT_VERSION if after else m.CLOCK_VERSION)
                    s.install_transport_outcomes()
                self.assertEqual(f.originals(),original);self.assertEqual(f.used(),3)

    def test_upgrade_cannot_recover_an_old_terminal_pending_capability(self):
        with self.prepared('genesis',install=False) as (case,values):
            f=case.f
            with f.journal.locked() as s:t=begin(s,'genesis');request,raw=wire(s,t,'genesis',values)
            self.assertEqual(f.resume()['state'],'TERMINAL_UNCERTAINTY')
            with f.journal.locked() as s:
                s.install_transport_outcomes()
                with self.assertRaisesRegex(m.JournalBlocked,'UNKNOWN_INFLIGHT'):
                    self.complete(s,t,'genesis',request,raw,'RESPONSE_TRUNCATED')
                with self.assertRaisesRegex(m.JournalBlocked,'TERMINAL_UNCERTAINTY'):begin(s,'genesis')
            self.assertEqual(f.used(),4);self.assertIsNone(self.row(f,'genesis'))

    def test_rehashed_done_forgery_with_transport_failure_refuses_all_stage_audits(self):
        schemas=m.ATTACHMENT_SCHEMA|m.SLOT_SCHEMA|m.UNION_SCHEMA|m.CLOCK_SCHEMA
        reasons=('EXACT_GENESIS','FINALIZED_SLOT_DECLARED','UNION_RAW_SHAPE_VALID','BLOCK_TIME_DECLARED')
        for stage in STAGES:
            with self.subTest(stage=stage),self.prepared(stage) as (case,values):
                f=case.f
                with f.journal.locked() as s:
                    t=begin(s,stage);request,raw=wire(s,t,stage,values)
                    self.complete(s,t,stage,request,raw,'RESPONSE_TRUNCATED')
                case.read()  # Previously valid structural inventory cannot be reused after corruption.
                table=TABLES[STAGES.index(stage)];row=self.row(f,stage);event=json.loads(row[1])
                event.update(state='DONE',reason=reasons[STAGES.index(stage)])
                if stage in ('slot','union'):event['slot']=20 if stage=='slot' else 21
                if stage=='clock':event['block_time']=110
                key=digest({'previous_hash':row[2],'event':event})
                # Self-consistent hashes and valid-looking original body: retain the failure witness.
                with f.store.connect() as c:
                    c.execute('DROP TRIGGER '+table+'_update')
                    c.execute('UPDATE '+table+' SET event_json=?,event_hash=?',(canonical(event),key))
                    c.execute(schemas[table+'_update'])
                with f.journal.locked() as s:
                    with self.assertRaisesRegex(m.JournalBlocked,'HASH_OR_BINDING'):s.resume_capture('multi')
                    with self.assertRaises(m.JournalBlocked):s.replay_semantics('capture',observed_at=130)
                with self.assertRaisesRegex(inventory.InventoryUnavailable,'FAILURE_LOCAL_FIELDS'):case.read()
                self.assertEqual(self.row(f,stage)[4:7],row[4:7]);self.assertEqual(f.used(),4+STAGES.index(stage))
