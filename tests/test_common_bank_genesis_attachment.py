"""Exact in-flight genesis observation attachment: fixtures, no transport I/O."""
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from desk import common_bank_journal as module
from desk.common_bank_journal import CommonBankJournal, JournalBlocked, ATTACHMENT_SCHEMA, SCHEMA
from desk.model import canonical
from desk.pool_receipt_ledger import ApprovedSource
from desk.pool_vault_admission import GENESIS
from tests import test_common_bank_journal as journal_fixture


def _response(request,result=GENESIS):
    # Preserve whitespace deliberately: wire-byte identity is not JSON identity.
    return (' \n'+json.dumps({'jsonrpc':'2.0','id':json.loads(request)['id'],'result':result})+'\n ').encode()


def _child(research,evidence,fence,mode,queue=None,go=None):
    if go is not None: go.wait(10)
    journal = CommonBankJournal(research,evidence,ApprovedSource('fixture','synthetic_fixture'))
    if mode == 'before_completion_commit':
        real_connect = sqlite3.connect
        class DeathConnection(sqlite3.Connection):
            def commit(self):
                try: completed = self.execute('SELECT count(*) FROM common_bank_attachments').fetchone()[0]
                except sqlite3.Error: completed = 0
                if completed: os._exit(83)
                return super().commit()
        def connect(*args,**kwargs):
            return real_connect(*args,**{**kwargs,'factory':DeathConnection})
        sqlite3.connect = connect
    try:
        with journal.locked() as session:
            token = session.begin_genesis('capture',fence=fence,reserved_at=121)
            if mode == 'lost_response': os._exit(82)
            request = session.genesis_request(token)
            result = (session.attach_genesis_failure(token,'TRANSPORT_ERROR',completed_at=122)
                      if mode == 'after_failure_commit' else
                      session.attach_genesis_response(token,_response(request),completed_at=122))
            if mode == 'after_failure_commit': os._exit(85)
            if mode == 'after_completion_commit': os._exit(84)
        if queue is not None: queue.put(('OK',result['state']))
    except Exception as exc:
        if queue is not None: queue.put(('BLOCKED',str(exc)))
        else: raise


@unittest.skipUnless(Path('/proc/self/mountinfo').is_file(),'Linux stable guarded source contract')
class GenesisAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.f = journal_fixture.CommonBankJournalTests()
        self.addCleanup(self.f.doCleanups); self.f.setUp()
        self.journal = self.f.journal; self.f.freeze()

    def install(self):
        with self.journal.locked() as session: session.install_genesis_attachments()

    def rows(self):
        with self.f.store.connect() as c:
            return c.execute('SELECT * FROM common_bank_attachments').fetchall()

    def begin(self,session):
        return session.begin_genesis('capture',fence=self.f.fence,reserved_at=121)

    def test_explicit_install_preserves_v1_schema_records_and_is_idempotent(self):
        before = self.f.originals()
        with self.f.store.connect() as c:
            parent_rows = {name:c.execute('SELECT * FROM '+name).fetchall()
                           for name in ('common_bank_meta','common_bank_runs','common_bank_events')}
        with self.journal.locked() as session:
            with self.assertRaisesRegex(JournalBlocked,'EXPLICIT'): self.begin(session)
            session.install_genesis_attachments(); session.install_genesis_attachments()
        self.assertEqual(self.f.used(),3); self.assertEqual(self.f.originals(),before)
        with self.f.store.connect() as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],module.ATTACHMENT_VERSION)
            for name,sql in SCHEMA.items():
                self.assertEqual(c.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()[0],sql)
            for name,rows in parent_rows.items(): self.assertEqual(c.execute('SELECT * FROM '+name).fetchall(),rows)

    def test_original_response_and_request_exact_bytes_once_no_approval(self):
        self.install(); before = self.f.originals()
        with self.journal.locked() as session:
            token = self.begin(session); request = session.genesis_request(token); raw = _response(request)
            self.assertEqual(session.resume_capture('multi')['state'],'GENESIS_IN_FLIGHT')
            event = session.attach_genesis_response(token,raw,completed_at=122)
            self.assertEqual(event['state'],'DONE'); self.assertEqual(event['reason'],'EXACT_GENESIS')
            for key in ('eligible_for_trading','ownership_approval','chain_authenticated'): self.assertIs(event[key],False)
            self.assertEqual(event['request_sha256'],hashlib.sha256(request).hexdigest())
            self.assertEqual(event['response_sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual(session.attach_genesis_response(token,raw,completed_at=122),event)
            for changed,time in [(raw+b' ',122),(raw,123),(_response(request,'wrong'),122)]:
                with self.assertRaisesRegex(JournalBlocked,'CONFLICTING'):
                    session.attach_genesis_response(token,changed,completed_at=time)
            with self.assertRaisesRegex(JournalBlocked,'CONFLICTING'):
                session.attach_genesis_failure(token,'TRANSPORT_TIMEOUT',completed_at=122)
        rows = self.rows(); self.assertEqual(len(rows),1); self.assertEqual(rows[0][4:6],(request,raw))
        state = self.f.resume(); self.assertEqual(state['state'],'GENESIS_DONE')
        for key in ('eligible_for_trading','ownership_approval','chain_authenticated'): self.assertIs(state[key],False)
        self.assertEqual(state['provider_calls'],0); self.assertEqual(self.f.used(),4)
        self.assertEqual(self.f.originals(),before)
        with self.f.store.connect() as c:
            self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name IN ('ownership_banks','ownership_heads')").fetchone())

    def test_legacy_or_reopened_pending_never_gains_completion_authority(self):
        intent = self.f.reserve()
        with self.f.store.connect() as c: original = c.execute('SELECT * FROM common_bank_events').fetchall()
        self.install()
        with self.f.store.connect() as c: self.assertEqual(c.execute('SELECT * FROM common_bank_events').fetchall(),original)
        with self.journal.locked() as session:
            self.assertEqual(session.resume_capture('multi')['state'],'TERMINAL_UNCERTAINTY')
            for forged in (object(),intent,{'fence':self.f.fence}):
                with self.assertRaisesRegex(JournalBlocked,'UNKNOWN_INFLIGHT'):
                    session.attach_genesis_response(forged,b'{}',completed_at=122)
            with self.assertRaisesRegex(JournalBlocked,'TERMINAL'): self.begin(session)
        self.assertEqual(self.f.used(),4); self.assertEqual(self.rows(),[])

    def test_handle_dies_with_session_and_cannot_be_used_in_another_session(self):
        self.install()
        with self.journal.locked() as first:
            token = self.begin(first); raw = _response(first.genesis_request(token))
        with self.assertRaisesRegex(JournalBlocked,'EXPIRED'):
            first.attach_genesis_response(token,raw,completed_at=122)
        with self.journal.locked() as second:
            with self.assertRaisesRegex(JournalBlocked,'UNKNOWN_INFLIGHT'):
                second.attach_genesis_response(token,raw,completed_at=122)
            self.assertEqual(second.resume_capture('multi')['state'],'TERMINAL_UNCERTAINTY')
        self.assertEqual(self.rows(),[]); self.assertEqual(self.f.used(),4)

    def test_summaries_mutable_bytes_forged_handle_and_bad_time_rejected(self):
        self.install()
        with self.journal.locked() as session:
            token = self.begin(session); raw = _response(session.genesis_request(token))
            for body in ({'result':GENESIS},GENESIS,bytearray(raw)):
                with self.assertRaisesRegex(JournalBlocked,'RAW_RESPONSE'):
                    session.attach_genesis_response(token,body,completed_at=122)
            with self.assertRaisesRegex(JournalBlocked,'UNKNOWN_INFLIGHT'):
                session.attach_genesis_response(object(),raw,completed_at=122)
            for time in (True,120,2**63):
                with self.assertRaisesRegex(JournalBlocked,'TIME'):
                    session.attach_genesis_response(token,raw,completed_at=time)
            for category in ('timeout: secret=https://example.invalid/?key=x',{'category':'TRANSPORT_ERROR'},'RESPONSE_OVERSIZED'):
                with self.assertRaisesRegex(JournalBlocked,'CATEGORY'):
                    session.attach_genesis_failure(token,category,completed_at=122)
            self.assertEqual(self.rows(),[])
            self.assertEqual(session.attach_genesis_response(token,raw,completed_at=122)['state'],'DONE')
        self.assertEqual(self.f.used(),4)

    def test_every_malformed_conflicting_or_error_response_is_retained_failed(self):
        cases = [lambda id:b'not json',lambda id:b'null',lambda id:b'[]',
                 lambda id:json.dumps({'jsonrpc':'2.0','id':id,'result':'other-chain'}).encode(),
                 lambda id:json.dumps({'jsonrpc':'2.0','id':'substituted','result':GENESIS}).encode(),
                 lambda id:json.dumps({'jsonrpc':'2.0','id':id,'error':{'code':-1,'message':'fixture'}}).encode(),
                 lambda id:json.dumps({'jsonrpc':'2.0','id':id,'result':GENESIS,'error':None}).encode(),
                 lambda id:('{"jsonrpc":"2.0","id":'+json.dumps(id)+',"result":NaN}').encode(),
                 lambda id:('{"jsonrpc":"2.0","id":'+json.dumps(id)+',"result":"wrong","result":'+json.dumps(GENESIS)+'}').encode(),
                 lambda id:b'\xff',lambda id:b'['*2000+b']'*2000]
        for i,make in enumerate(cases):
            with self.subTest(case=i):
                f = journal_fixture.CommonBankJournalTests(); f.setUp()
                try:
                    f.freeze()
                    with f.journal.locked() as session:
                        session.install_genesis_attachments()
                        token = session.begin_genesis('capture',fence=f.fence,reserved_at=121)
                        request = session.genesis_request(token); raw = make(json.loads(request)['id'])
                        event = session.attach_genesis_response(token,raw,completed_at=122)
                        self.assertEqual(event['state'],'FAILED')
                    with f.store.connect() as c:
                        self.assertEqual(c.execute('SELECT response_bytes FROM common_bank_attachments').fetchone()[0],raw)
                    self.assertEqual(f.used(),4); self.assertEqual(f.resume()['state'],'GENESIS_FAILED')
                finally: f.doCleanups()

    def test_redacted_failure_is_fixed_bound_terminal_and_not_refunded(self):
        self.install()
        with self.journal.locked() as session:
            token = self.begin(session)
            event = session.attach_genesis_failure(token,'TRANSPORT_TIMEOUT',completed_at=122)
            self.assertEqual((event['state'],event['reason']),('FAILED','TRANSPORT_TIMEOUT'))
            self.assertEqual(session.attach_genesis_failure(token,'TRANSPORT_TIMEOUT',completed_at=122),event)
        row = self.rows()[0]; self.assertIsNone(row[5])
        self.assertEqual(json.loads(row[6]),{'kind':'common_bank_redacted_failure_v1','category':'TRANSPORT_TIMEOUT','method':'getGenesisHash','params':[]})
        self.assertEqual(self.f.used(),4); self.assertEqual(self.f.resume()['state'],'GENESIS_FAILED')
        with self.assertRaisesRegex(JournalBlocked,'TERMINAL'): self.f.reserve()
        with self.journal.locked() as session:
            with self.assertRaisesRegex(JournalBlocked,'BUDGET_ALREADY_BOUND'): session.create_run('multi','replacement')

    def test_exact_response_cap_and_oversize_failure_preserve_no_validated_prefix(self):
        for extra in (0,1):
            with self.subTest(extra=extra):
                f = journal_fixture.CommonBankJournalTests(); f.setUp()
                try:
                    f.freeze()
                    with f.journal.locked() as session:
                        session.install_genesis_attachments()
                        token = session.begin_genesis('capture',fence=f.fence,reserved_at=121)
                        raw = _response(session.genesis_request(token))
                        raw += b' '*(module.MAX_RESPONSE_BYTES-len(raw)+extra)
                        event = session.attach_genesis_response(token,raw,completed_at=122)
                        self.assertEqual(event['state'],'FAILED' if extra else 'DONE')
                    with f.store.connect() as c:
                        body,failure = c.execute('SELECT response_bytes,failure_json FROM common_bank_attachments').fetchone()
                    if extra:
                        self.assertIsNone(body); self.assertEqual(event['reason'],'RESPONSE_OVERSIZED')
                        self.assertEqual(json.loads(failure)['observed_bytes'],len(raw))
                    else: self.assertEqual(body,raw)
                    self.assertEqual(f.used(),4)
                finally: f.doCleanups()

    def test_actual_fork_cannot_use_inherited_completion_handle(self):
        self.install()
        with self.journal.locked() as session:
            token = self.begin(session); raw = _response(session.genesis_request(token))
            pid = os.fork()
            if pid == 0:
                try: session.attach_genesis_response(token,raw,completed_at=122)
                except JournalBlocked as exc:
                    os._exit(86 if 'WRONG_OWNER' in str(exc) else 87)
                os._exit(88)
            _,status = os.waitpid(pid,0)
            self.assertTrue(os.WIFEXITED(status)); self.assertEqual(os.WEXITSTATUS(status),86)
            self.assertEqual(self.rows(),[])
            self.assertEqual(session.attach_genesis_response(token,raw,completed_at=122)['state'],'DONE')
        self.assertEqual(self.f.used(),4)

    def test_stale_source_descriptor_and_cross_thread_handle_fail_before_attachment(self):
        self.install()
        with self.journal.locked() as session:
            token = self.begin(session); raw = _response(session.genesis_request(token))
            import threading
            errors = []
            def other():
                try: session.attach_genesis_response(token,raw,completed_at=122)
                except JournalBlocked as exc: errors.append(str(exc))
            thread = threading.Thread(target=other); thread.start(); thread.join(3)
            self.assertEqual(errors,['COMMON_BANK_SESSION_EXPIRED_OR_WRONG_OWNER'])
            with self.f.store.connect() as c:
                c.execute("UPDATE ownership_admissions SET completed_source_hash=? WHERE id='multi'",('0'*64,))
            with self.assertRaises(ValueError): session.attach_genesis_response(token,raw,completed_at=122)
            self.assertEqual(self.rows(),[])
        with self.f.store.connect() as c: self.assertEqual(c.execute('SELECT used FROM ownership_budgets').fetchone()[0],4)

    def death(self,mode,code):
        self.install(); ctx = multiprocessing.get_context('spawn')
        child = ctx.Process(target=_child,args=(self.f.research,self.f.path,self.f.fence,mode))
        child.start(); self.addCleanup(lambda: child.kill() if child.is_alive() else None); child.join(20)
        self.assertEqual(child.exitcode,code)

    def test_real_death_after_intent_before_result_stays_ambiguous(self):
        self.death('lost_response',82)
        self.assertEqual(self.f.used(),4); self.assertEqual(self.rows(),[])
        self.assertEqual(self.f.resume()['state'],'TERMINAL_UNCERTAINTY')
        with self.assertRaisesRegex(JournalBlocked,'TERMINAL'): self.f.reserve()

    def test_real_death_before_completion_commit_rolls_back_attachment_only(self):
        self.death('before_completion_commit',83)
        self.assertEqual(self.f.used(),4); self.assertEqual(self.rows(),[])
        self.assertEqual(self.f.resume()['state'],'TERMINAL_UNCERTAINTY')

    def test_real_death_after_completion_commit_recovers_exact_done_without_retry(self):
        self.death('after_completion_commit',84)
        self.assertEqual(self.f.used(),4); self.assertEqual(len(self.rows()),1)
        self.assertEqual(self.f.resume()['state'],'GENESIS_DONE')
        with self.journal.locked() as session:
            with self.assertRaisesRegex(JournalBlocked,'TERMINAL'): self.begin(session)
            with self.assertRaisesRegex(JournalBlocked,'UNKNOWN_INFLIGHT'):
                session.attach_genesis_response(object(),self.rows()[0][5],completed_at=122)

    def test_real_death_after_failure_commit_preserves_redacted_terminal_record(self):
        self.death('after_failure_commit',85)
        self.assertEqual(self.f.used(),4); self.assertEqual(len(self.rows()),1)
        self.assertEqual(self.f.resume()['state'],'GENESIS_FAILED')
        self.assertEqual(json.loads(self.rows()[0][6])['category'],'TRANSPORT_ERROR')
        with self.assertRaisesRegex(JournalBlocked,'TERMINAL'): self.f.reserve()

    def test_two_real_processes_only_one_can_begin_and_attach(self):
        self.install(); ctx = multiprocessing.get_context('spawn'); queue = ctx.Queue(); go = ctx.Event()
        children = [ctx.Process(target=_child,args=(self.f.research,self.f.path,self.f.fence,'race',queue,go)) for _ in range(2)]
        for child in children:
            child.start(); self.addCleanup(lambda child=child: child.kill() if child.is_alive() else None)
        go.set()
        for child in children: child.join(20); self.assertEqual(child.exitcode,0)
        results = [queue.get(timeout=3) for _ in children]; queue.close(); queue.join_thread()
        self.assertEqual(sum(r[0]=='OK' for r in results),1,results)
        self.assertEqual(self.f.used(),4); self.assertEqual(len(self.rows()),1)
        self.assertEqual(self.f.resume()['state'],'GENESIS_DONE')

    def test_commit_errors_allow_only_identical_known_observation_persistence(self):
        for after in (False,True):
            with self.subTest(after=after):
                f = journal_fixture.CommonBankJournalTests(); f.setUp()
                try:
                    f.freeze()
                    with f.journal.locked() as session:
                        session.install_genesis_attachments()
                        token = session.begin_genesis('capture',fence=f.fence,reserved_at=121)
                        raw = _response(session.genesis_request(token)); real_connect = sqlite3.connect
                        class ErrorConnection(sqlite3.Connection):
                            def commit(self):
                                if after: super().commit()
                                raise sqlite3.OperationalError('injected completion commit error')
                        def connect(*args,**kwargs): return real_connect(*args,**{**kwargs,'factory':ErrorConnection})
                        with patch.object(module.sqlite3,'connect',side_effect=connect):
                            with self.assertRaisesRegex(sqlite3.OperationalError,'injected'):
                                session.attach_genesis_response(token,raw,completed_at=122)
                        with self.assertRaisesRegex(JournalBlocked,'CONFLICTING'):
                            session.attach_genesis_response(token,raw+b' ',completed_at=122)
                        self.assertEqual(session.attach_genesis_response(token,raw,completed_at=122)['state'],'DONE')
                    self.assertEqual(f.used(),4)
                    with f.store.connect() as c: self.assertEqual(c.execute('SELECT count(*) FROM common_bank_attachments').fetchone()[0],1)
                finally: f.doCleanups()

    def test_fresh_sqlite_attachment_replace_delete_update_rowid_aliases_are_blocked(self):
        self.install()
        with self.journal.locked() as session:
            token = self.begin(session); session.attach_genesis_response(token,_response(session.genesis_request(token)),completed_at=122)
        for table in ('common_bank_attachment_meta','common_bank_attachments'):
            with sqlite3.connect(self.f.path) as c:
                self.assertEqual(c.execute('PRAGMA recursive_triggers').fetchone()[0],0)
                rows = c.execute('SELECT * FROM '+table).fetchall()
                for sql in ('DELETE FROM '+table,'UPDATE '+table+' SET '+('id=id' if table.endswith('meta') else 'capture_id=capture_id'),
                            'INSERT OR REPLACE INTO '+table+' SELECT * FROM '+table):
                    with self.assertRaises(sqlite3.DatabaseError): c.execute(sql)
                for alias in ('rowid','oid','_rowid_'):
                    with self.assertRaises(sqlite3.DatabaseError): c.execute('UPDATE OR REPLACE '+table+' SET '+alias+'=2')
                self.assertEqual(c.execute('SELECT * FROM '+table).fetchall(),rows)
        self.assertEqual(self.f.resume()['state'],'GENESIS_DONE')

    def test_altered_original_bytes_are_detected_after_restored_trigger(self):
        self.install()
        with self.journal.locked() as session:
            token = self.begin(session); raw = _response(session.genesis_request(token))
            session.attach_genesis_response(token,raw,completed_at=122)
        with self.f.store.connect() as c:
            c.execute('DROP TRIGGER common_bank_attachments_update')
            c.execute('UPDATE common_bank_attachments SET response_bytes=?',(raw+b' ',))
            c.execute(ATTACHMENT_SCHEMA['common_bank_attachments_update'])
        with self.assertRaisesRegex(JournalBlocked,'HASH_OR_BINDING'): self.f.resume()
        self.assertEqual(self.f.used(),4)

    def test_missing_attachment_schema_or_metadata_and_version_cannot_remigrate(self):
        self.install()
        with self.f.store.connect() as c:
            c.execute('DROP TRIGGER common_bank_attachment_meta_delete'); c.execute('DELETE FROM common_bank_attachment_meta')
            c.execute(ATTACHMENT_SCHEMA['common_bank_attachment_meta_delete'])
        with self.journal.locked() as session:
            with self.assertRaisesRegex(JournalBlocked,'METADATA'): session.install_genesis_attachments()
        self.assertEqual(self.f.used(),3)
        with self.f.store.connect() as c:
            for name in ('common_bank_attachments','common_bank_attachment_meta'): c.execute('DROP TABLE '+name)
        with self.journal.locked() as session:
            with self.assertRaisesRegex(JournalBlocked,'SCHEMA'): session.install_genesis_attachments()
        with self.f.store.connect() as c: c.execute('PRAGMA user_version=17')
        with self.journal.locked() as session:
            with self.assertRaisesRegex(JournalBlocked,'VERSION_UNKNOWN'): session.install_genesis_attachments()
        self.assertEqual(self.f.used(),3)
