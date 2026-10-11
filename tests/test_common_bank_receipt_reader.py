"""Actual guarded reads on synthetic private fixtures, no production provisioning."""
from contextlib import closing
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import time
import unittest
from unittest.mock import patch

from desk import common_bank_receipt_reader as reader
from desk import common_bank_receipt_format as fmt
from desk.control_obligations import read_platform_available
from desk.model import digest
from tests import test_pool_receipt_ledger as legacy_fixture
from tests import test_common_bank_receipt_chain as typed_fixture


@unittest.skipUnless(read_platform_available(),'Linux LP64 protected reader contract')
class ProtectedReceiptReaderTests(unittest.TestCase):
    def setUp(self):
        self.old=legacy_fixture.PoolReceiptLedgerTests();self.addCleanup(self.old.doCleanups);self.old.setUp()
        self.old.writer.publish('legacy-original',self.old.receipt)
        self.path=self.old.writer.path;self.root=self.old.root;self.boundary=self.old.boundary
        with sqlite3.connect(self.path) as c:
            descriptor=json.loads(c.execute('SELECT body FROM ledger_descriptor').fetchone()[0])
            prefix=tuple(c.execute('SELECT * FROM coordinator_receipts ORDER BY seq'))
        self.f=typed_fixture.ReceiptChainTests();self.f.setUp();self.f.descriptor=descriptor;self.f.prefix=prefix
        self.f.schema_hash=fmt.SCHEMA_FINGERPRINT
        self.f.certificate.update(legacy_descriptor_hash=digest(descriptor),legacy_head_hash=prefix[-1][-1],schema_hash=fmt.SCHEMA_FINGERPRINT)
        self.f.build();self.anchor=self.f.anchor
        # Synthetic fixture construction ONLY; reader never installs anything.
        with sqlite3.connect(self.path) as c:
            c.execute('BEGIN IMMEDIATE');c.execute('DROP TRIGGER receipt_no_replace');c.execute(fmt.FENCE_SQL)
            for name in (fmt.CERTIFICATE_TABLE,'certificate_no_update','certificate_no_delete','certificate_no_replace'):
                c.execute(fmt.FORMAT_SQL[name])
            c.execute('INSERT INTO receipt_transition_certificate VALUES(1,?,?)',
                      (self.f.snapshot.certificate_body,self.f.snapshot.certificate_hash))
            c.executemany('INSERT INTO coordinator_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',self.f.snapshot.rows[len(prefix):])
            c.execute('UPDATE ledger_head SET count=?,hash=? WHERE id=1',self.f.snapshot.head)

    def read(self):return reader.read_receipt_chain(self.boundary,self.anchor)
    def files(self):
        return {p.name:(p.stat().st_ino,p.stat().st_size,p.stat().st_mtime_ns,p.stat().st_ctime_ns,
                        hashlib.sha256(p.read_bytes()).hexdigest()) for p in self.root.iterdir() if p.is_file()}
    def refused(self):
        with self.assertRaises(reader.ReaderUnavailable):self.read()
    def append(self):
        self.f.build();self.anchor=self.f.anchor
        with sqlite3.connect(self.path) as c:
            count=c.execute('SELECT count FROM ledger_head').fetchone()[0]
            c.executemany('INSERT INTO coordinator_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',self.f.snapshot.rows[count:])
            c.execute('UPDATE ledger_head SET count=?,hash=? WHERE id=1',self.f.snapshot.head)

    def test_positive_actual_complete_read_keeps_exact_bytes_no_sidecars_or_permissions(self):
        before=self.files();result=self.read()
        self.assertEqual(result.records[0].row,self.f.prefix[0])
        self.assertEqual(tuple(r.row for r in result.records),self.f.snapshot.rows[:1]+self.f.snapshot.rows[2:])
        self.assertEqual(result.head,self.anchor.head)
        scope=result.enumerate_scope(pool=self.f.pool,mint=self.f.mint,slot=self.f.slot)
        self.assertEqual(len(scope.observations),2)
        for key,value in scope.diagnostic().items():
            if type(value) is bool:self.assertFalse(value,key)
        self.assertEqual(before,self.files())
        self.assertEqual(result,self.read())

    def test_all_profiles_markers_expired_disabled_sources_survive_actual_read(self):
        self.f.add_marker(status='FAILED');self.f.add_marker(slot=None)
        self.f.extra.append(('legacy_observation',reader.PROFILE,{**self.f.receipt,'captured_at':0}))
        self.append();result=self.read()
        scope=result.enumerate_scope(pool=self.f.pool,mint=self.f.mint,slot=self.f.slot)
        self.assertEqual((len(scope.observations),len(scope.markers)),(3,2))
        self.assertEqual(scope.observations[0].issuance_enabled,False)
        self.assertIn('CHAIN_UNRESOLVED_FAILED',scope.reasons)
        self.assertIn('CHAIN_UNKNOWN_SLOT_MARKER',scope.reasons)
        self.assertFalse(scope.diagnostic()['raw_conflicts_checked'])

    def test_unknown_identity_actual_marker_blocks_other_scope(self):
        self.f.add_marker(pool=None,slot=None);self.append()
        scope=self.read().enumerate_scope(pool=self.f.mint,mint=self.f.pool,slot=self.f.slot+1)
        self.assertIn('CHAIN_UNKNOWN_IDENTITY_MARKER',scope.reasons)

    def test_no_candidate_defaults_or_wrong_boundary_pins(self):
        self.anchor=replace(self.anchor,head=(3,'e'*64));self.refused()
        self.anchor=self.f.anchor;self.boundary=replace(self.boundary,ledger_id='other');self.refused()
        with self.assertRaises(reader.ReaderUnavailable):reader.read_receipt_chain(None,None)

    def test_scalar_preflight_precedes_any_snapshot_materialization_and_one_transaction(self):
        trace=[];connect=sqlite3.connect
        def observed(*args,**kwargs):
            c=connect(*args,**kwargs);c.set_trace_callback(trace.append);return c
        with patch.object(reader.sqlite3,'connect',side_effect=observed):self.read()
        scalar=max(i for i,s in enumerate(trace) if s in fmt.PREFLIGHT_SQL.values())
        load=min(i for i,s in enumerate(trace) if s=='SELECT body,hash FROM ledger_descriptor WHERE id=1')
        self.assertLess(scalar,load)
        self.assertEqual(trace.count('BEGIN'),1);self.assertEqual(trace.count('ROLLBACK'),1)
        self.assertTrue(trace.index('BEGIN')<scalar<load<trace.index('ROLLBACK'))
        self.assertFalse(any(s.upper().startswith(('INSERT','UPDATE','DELETE','CREATE','DROP','ATTACH','COMMIT')) for s in trace))

    def test_oversized_utf8_scalar_refuses_before_any_values_load(self):
        with sqlite3.connect(self.path) as c:
            c.execute('DROP TRIGGER certificate_no_update');c.execute('UPDATE receipt_transition_certificate SET body=?',('é'*4097,))
            c.execute(fmt.FORMAT_SQL['certificate_no_update'])
        with patch.object(reader,'_load_snapshot',side_effect=AssertionError('must preflight')):self.refused()

    def test_ten_thousand_one_rows_refuse_before_load(self):
        with sqlite3.connect(self.path) as c:
            c.executemany('INSERT INTO coordinator_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',
                ((i,'synthetic:'+str(i),self.f.pool,self.f.mint,str(self.f.slot),'fixture-source','{}',
                  format(i,'064x'),'a'*64,'b'*64) for i in range(4,10002)))
            c.execute('UPDATE ledger_head SET count=10001')
        with patch.object(reader,'_load_snapshot',side_effect=AssertionError('must preflight')):self.refused()

    def test_missing_extra_altered_schema_refuse_before_values_load(self):
        for action in ('DROP TRIGGER certificate_no_update','CREATE TABLE extra(x)'):
            with self.subTest(action=action):
                with sqlite3.connect(self.path) as c:c.execute(action)
                with patch.object(reader,'_load_snapshot',side_effect=AssertionError('must schema audit')):self.refused()
                with sqlite3.connect(self.path) as c:
                    if action.startswith('DROP'):c.execute(fmt.FORMAT_SQL['certificate_no_update'])
                    else:c.execute('DROP TABLE extra')

    def test_oversized_schema_preflight_precedes_full_sql_text_inventory(self):
        with sqlite3.connect(self.path) as c:
            c.execute('DROP TRIGGER certificate_no_update')
            c.execute("CREATE TRIGGER certificate_no_update BEFORE UPDATE ON receipt_transition_certificate BEGIN SELECT '"+'x'*3000+"'; END")
        with patch.object(reader,'validate_inventory',side_effect=AssertionError('must schema preflight')):self.refused()

    def test_partial_singleton_or_wrong_storage_type_refuse_before_load(self):
        with sqlite3.connect(self.path) as c:
            c.execute('DROP TRIGGER certificate_no_delete');c.execute('DELETE FROM receipt_transition_certificate');c.execute(fmt.FORMAT_SQL['certificate_no_delete'])
        with patch.object(reader,'_load_snapshot',side_effect=AssertionError('must singleton preflight')):self.refused()

    def test_invalid_utf8_sql_text_refuses_without_mutation(self):
        with sqlite3.connect(self.path) as c:
            c.execute('DROP TRIGGER certificate_no_update')
            c.execute("UPDATE receipt_transition_certificate SET body=CAST(x'ff' AS TEXT)")
            c.execute(fmt.FORMAT_SQL['certificate_no_update'])
        before=self.files();self.refused();self.assertEqual(before,self.files())

    def test_corruption_after_prior_success_never_returns_cached_prefix(self):
        self.read()
        with sqlite3.connect(self.path) as c:
            c.execute('DROP TRIGGER receipt_no_update')
            c.execute("UPDATE coordinator_receipts SET row_hash=? WHERE seq=3",('e'*64,))
            c.execute(fmt.FORMAT_SQL['receipt_no_update'])
        before=self.files();self.refused();self.assertEqual(before,self.files())

    def test_hot_stale_wal_shm_sidecars_refuse_before_sqlite_open(self):
        for suffix in ('-journal','-wal','-shm'):
            path=Path(str(self.path)+suffix);path.write_bytes(b'fixture only');before=self.files()
            with patch.object(reader.sqlite3,'connect',side_effect=AssertionError('no recovery open')):self.refused()
            self.assertEqual(before,self.files());path.unlink()

    def test_header_physical_overflow_refuses_before_sqlite_open(self):
        with self.path.open('r+b') as f:f.truncate(fmt.MAX_FILE_BYTES+fmt.PAGE_SIZE)
        with patch.object(reader.sqlite3,'connect',side_effect=AssertionError('must file/header preflight')):self.refused()

    def test_wal_header_refuses_before_sqlite_open(self):
        with self.path.open('r+b') as f:f.seek(18);f.write(b'\x02\x02')
        with patch.object(reader.sqlite3,'connect',side_effect=AssertionError('must header preflight')):self.refused()

    def test_ledger_evidence_aliases_and_hardlinks_refuse_before_sqlite(self):
        original=self.boundary
        for field,path in (('ledger_path',self.path),('evidence_path',self.old.store.path)):
            alias=self.root/('alias-'+field);alias.symlink_to(path)
            self.boundary=replace(original,**{field:alias})
            with patch.object(reader.sqlite3,'connect',side_effect=AssertionError('must alias preflight')):self.refused()
            alias.unlink()
        self.boundary=original;link=self.root/'hardlink';os.link(self.path,link)
        with patch.object(reader.sqlite3,'connect',side_effect=AssertionError('must link preflight')):self.refused()
        link.unlink()

    def test_unsafe_root_ledger_lock_and_evidence_permissions_refuse(self):
        for path,mode in ((self.root,0o755),(self.path,0o640),(self.old.writer.lock_path,0o644),(self.old.store.path,0o666)):
            previous=path.stat().st_mode&0o777;path.chmod(mode)
            try:
                with patch.object(reader.sqlite3,'connect',side_effect=AssertionError('must permission preflight')):self.refused()
            finally:path.chmod(previous)

    def test_missing_lock_cannot_be_recreated(self):
        self.old.writer.lock_path.unlink()
        with patch.object(reader.sqlite3,'connect',side_effect=AssertionError('must lock preflight')):self.refused()
        self.assertFalse(self.old.writer.lock_path.exists())

    def test_coordinator_lock_contention_fails_immediately_and_never_opens_db(self):
        with self.old.writer.lock_path.open('rb') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB);start=time.monotonic()
            with patch.object(reader.sqlite3,'connect',side_effect=AssertionError('must lock preflight')):self.refused()
            self.assertLess(time.monotonic()-start,1)

    def test_uncooperative_sqlite_writer_on_evidence_or_ledger_blocks_reader(self):
        for path in (self.path,self.old.store.path):
            with closing(sqlite3.connect(path,isolation_level=None,timeout=0)) as c:
                c.execute('BEGIN IMMEDIATE')
                with patch.object(reader.sqlite3,'connect',side_effect=AssertionError('must OFD guard preflight')):self.refused()
                c.execute('ROLLBACK')

    def test_guard_blocks_noncooperating_writer_during_snapshot_loading(self):
        original=reader._load_snapshot;blocked=[]
        def probe(c):
            for path in (self.path,self.old.store.path):
                with closing(sqlite3.connect(path,isolation_level=None,timeout=0)) as writer:
                    with self.assertRaises(sqlite3.OperationalError):writer.execute('BEGIN IMMEDIATE')
                    blocked.append(path)
            return original(c)
        with patch.object(reader,'_load_snapshot',side_effect=probe):self.read()
        self.assertEqual(len(blocked),2)

    def test_ledger_evidence_or_lock_replacement_during_read_has_no_result(self):
        original=reader._load_snapshot;swapped=[]
        for path in (self.path,self.old.store.path,self.old.writer.lock_path):
            saved=Path(str(path)+'.saved')
            def replace_path(c):
                data=path.read_bytes();path.rename(saved);path.write_bytes(data);path.chmod(0o600)
                swapped.append(path)
                return original(c)
            try:
                with patch.object(reader,'_load_snapshot',side_effect=replace_path):self.refused()
            finally:
                if saved.exists():path.unlink();saved.rename(path)
        self.assertEqual(len(swapped),3)

    def test_root_permissions_changed_at_guard_exit_refuse_complete_result(self):
        original=reader._load_snapshot
        def changed(c):
            result=original(c);self.root.chmod(0o755);return result
        try:
            with patch.object(reader,'_load_snapshot',side_effect=changed):self.refused()
        finally:self.root.chmod(0o700)

    def test_sql_authorizer_rejects_writes_attach_and_unknown_functions(self):
        original=reader._load_snapshot;attempted=[]
        def probe(c):
            for sql in ('CREATE TEMP TABLE x(v)', 'UPDATE ledger_head SET count=0', "ATTACH ':memory:' AS other", 'SELECT randomblob(1000000)'):
                with self.assertRaises(sqlite3.DatabaseError):c.execute(sql)
                attempted.append(sql)
            return original(c)
        with patch.object(reader,'_load_snapshot',side_effect=probe):self.read()
        self.assertEqual(len(attempted),4)

    def test_connection_is_pristine_readonly_and_cannot_disable_query_only(self):
        original=reader._load_snapshot
        def probe(c):
            self.assertEqual(c.in_transaction,True)
            with self.assertRaises(sqlite3.DatabaseError):c.execute('PRAGMA query_only=OFF')
            return original(c)
        with patch.object(reader,'_load_snapshot',side_effect=probe):self.read()

    def test_current_pin_never_refreshes_after_append(self):
        previous=self.anchor;self.f.add_marker();self.append();current=self.anchor;self.anchor=previous
        self.refused();self.anchor=current;self.assertEqual(len(self.read().records),3)

    def test_new_call_after_locked_failure_revalidates_without_sticky_or_cached_success(self):
        with self.old.writer.lock_path.open('rb') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB);self.refused()
        self.assertEqual(self.read().head,self.anchor.head)


    def test_shared_32m_sql_byte_limit_fails_before_load_with_other_limits_valid(self):
        payload='{"x":"'+'x'*3990+'"}'
        with sqlite3.connect(self.path) as c:
            c.executemany('INSERT INTO coordinator_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',
                ((i,'aggregate:'+str(i),self.f.pool,self.f.mint,str(self.f.slot),'fixture-source',payload,
                  format(i,'064x'),'a'*64,'b'*64) for i in range(4,9001)))
            c.execute('UPDATE ledger_head SET count=9000')
        self.anchor=replace(self.anchor,head=(9000,'e'*64))
        original=reader.validate_preflight;reasons=[]
        def observed(metrics,anchor):
            try:return original(metrics,anchor)
            except Exception as exc:reasons.append(str(exc));raise
        with patch.object(reader,'validate_preflight',side_effect=observed),\
             patch.object(reader,'_load_snapshot',side_effect=AssertionError('must aggregate preflight')):self.refused()
        self.assertEqual(reasons,['FORMAT_SHARED_BYTE_BOUND'])

    def test_evidence_hot_journal_also_refuses_before_any_database_open(self):
        sidecar=Path(str(self.old.store.path)+'-journal');sidecar.write_bytes(b'fixture only')
        before=self.files()
        with patch.object(reader.sqlite3,'connect',side_effect=AssertionError('must evidence guard')):self.refused()
        self.assertEqual(before,self.files())

    def test_root_directory_identity_replacement_at_load_refuses_complete_result(self):
        saved=Path(str(self.root)+'.saved');original=reader._load_snapshot;swapped=[]
        def changed(c):
            result=original(c);self.root.rename(saved);self.root.mkdir(mode=0o700)
            for path in saved.iterdir():
                if path.is_file():shutil.copyfile(path,self.root/path.name);(self.root/path.name).chmod(path.stat().st_mode&0o777)
            swapped.append(True);return result
        try:
            with patch.object(reader,'_load_snapshot',side_effect=changed):self.refused()
            self.assertEqual(swapped,[True])
        finally:
            if saved.exists():shutil.rmtree(self.root);saved.rename(self.root)

    def test_sql_progress_deadline_fails_without_partial_result(self):
        with patch.object(reader,'MAX_READ_SECONDS',0):self.refused()

    def test_current_head_and_certificate_pin_are_checked_against_actual_values(self):
        self.anchor=replace(self.anchor,certificate_hash='e'*64);self.refused()



    def test_actual_reader_accepts_complete_ten_thousand_row_image_without_filtering(self):
        for i in range(9997):self.f.add_parent(capture_id='bounded:'+str(i))
        self.append()
        before=self.files();result=self.read()
        self.assertEqual((result.head[0],len(result.records)),(10000,9999))
        self.assertEqual(tuple(r.row for r in result.records),self.f.snapshot.rows[:1]+self.f.snapshot.rows[2:])
        self.assertEqual(before,self.files())



class PortableReaderPlatformTests(unittest.TestCase):
    def test_unsupported_platform_fails_before_pins_paths_or_database_access(self):
        with patch.object(reader,'read_platform_available',return_value=False),\
             patch.object(reader,'_pins',side_effect=AssertionError('no filesystem')),\
             patch.object(reader.sqlite3,'connect',side_effect=AssertionError('no database')):
            with self.assertRaisesRegex(reader.ReaderUnavailable,'PLATFORM_UNAVAILABLE'):reader.read_receipt_chain(None,None)
