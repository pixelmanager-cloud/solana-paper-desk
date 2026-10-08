"""Actual guarded SQLite fixtures; no provider or operational completion."""
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from desk import common_bank_completion_inventory as m
from desk.model import canonical,digest
from desk.pool_receipt_ledger import CoordinatorBoundary,PoolReceiptLedger
from tests import test_common_bank_journal as jf
from tests import test_common_bank_clock_stage as cf
from tests import test_common_bank_union_stage as uf
from tests.test_pool_capture_bridge import PoolCaptureBridge,CoordinatorTransport,Investigation,FixtureRPC


@unittest.skipUnless(m.read_platform_available(),'Linux LP64 OFD guarded inventory')
class CompletionInventoryTests(unittest.TestCase):
    def setUp(self):
        self.f=jf.CommonBankJournalTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.bind()

    def bind(self):
        f=self.f;self.root=f.root;os.chmod(f.research,0o600);os.chmod(f.path,0o600)
        boundary=CoordinatorBoundary('completion-fixture',self.root,self.root/'inventory-receipts.sqlite',f.path,
                     frozenset({f.source}),allow_synthetic_fixtures=True)
        self.writer=PoolReceiptLedger.open_writer(boundary)
        self.writer.publish('original',f.fixture.receipt)
        self.pins=m.InventoryPins(canonical(self.writer.descriptor),self.head(),f.research,
            tuple(f.journal.descriptor['research_identity']),canonical(f.journal.descriptor),None)

    def head(self):
        with sqlite3.connect(self.writer.path) as c:return c.execute('SELECT count,hash FROM ledger_head').fetchone()
    def read(self):return m.read_completion_inventory(self.pins)
    def files(self):
        return {str(p):(p.stat().st_ino,p.stat().st_size,p.stat().st_mtime_ns,p.stat().st_ctime_ns,
            hashlib.sha256(p.read_bytes()).hexdigest()) for p in self.root.iterdir() if p.is_file()}
    def refused(self):
        with self.assertRaises(m.InventoryUnavailable):self.read()
    def table(self,result,name):
        return next(rows for _,key,rows in result.tables if key==name)

    def test_absent_journal_no_creation_and_all_original_publications_counters(self):
        before=self.files();v=self.read()
        self.assertEqual(before,self.files())
        self.assertIn('COMMON_BANK_JOURNAL_NOT_INSTALLED',v.blockers)
        self.assertEqual(self.table(v,'ownership_budgets')[0][2],3)
        self.assertEqual(len(self.table(v,'coordinator_receipts')),1)
        self.assertEqual(self.read(),v)
        for key,value in v.diagnostic().items():
            if type(value) is bool:self.assertFalse(value,key)

    def test_admitted_run_and_terminal_pending_retained_no_refund(self):
        self.f.freeze();self.f.reserve();before=self.files();v=self.read()
        self.assertEqual(before,self.files());self.assertEqual(self.f.used(),4)
        self.assertIn('ORIGINAL_PENDING_RECORD_RETAINED',v.blockers)
        self.assertEqual(len(self.table(v,'common_bank_events')),1)
        self.assertIn('PENDING_OR_AMBIGUOUS_ATTEMPT',v.blockers)
        self.assertEqual(json.loads(self.table(v,'common_bank_events')[0][2])['used_after'],4)

    def test_all_four_profiles_exact_original_raw_completion_and_pending_survive(self):
        cf.setup(self.f);self.bind()
        with self.f.journal.locked() as s:
            s.install_clock_stage();t=s.begin_clock('capture',fence=s.resume_capture('multi')['clock_fence'],reserved_at=127)
            raw=uf.wire(s.clock_request(t),110);s.attach_clock_response(t,raw,completed_at=128)
        before=self.files();v=self.read();self.assertEqual(before,self.files())
        self.assertEqual(self.f.used(),7)
        self.assertEqual(self.table(v,'common_bank_clock_attachments')[0][5],raw)
        self.assertIn('COMPLETION_NOT_AUTHENTICATED',v.blockers)
        self.assertIn('ORIGINAL_PENDING_RECORD_RETAINED',v.blockers)
        self.assertNotIn('PENDING_OR_AMBIGUOUS_ATTEMPT',v.blockers)
        self.assertEqual(len([key for _,key,_ in v.tables if key.startswith('common_bank_')]),14)

    def test_failed_raw_attachment_remains_and_budget_survives(self):
        self.f.create()
        with self.f.journal.locked() as s:
            s.install_genesis_attachments();t=s.begin_genesis('capture',fence=s.resume_capture('multi')['fence'],reserved_at=121)
            s.attach_genesis_failure(t,'TRANSPORT_ERROR',completed_at=122)
        v=self.read();self.assertIn('FAILED_ATTEMPT_RETAINED',v.blockers)
        self.assertEqual(self.f.used(),4);self.assertIsNone(self.table(v,'common_bank_attachments')[0][5])

    def test_legacy_capture_all_original_rows_and_publications_not_filtered(self):
        fixture=json.loads((Path(__file__).resolve().parents[1]/'fixtures/pool-vault-admission-legacy.json').read_text())
        bridge=PoolCaptureBridge(self.writer,CoordinatorTransport(self.f.source,FixtureRPC(fixture)),clock=lambda:fixture['query']['snapshot_time']+2)
        d=self.f.progress.admission('multi')['descriptor_hash']
        bridge.capture(capture_id='legacy',investigation=Investigation('multi',d,self.f.scan['mint']),pool=fixture['query']['pool'])
        with sqlite3.connect(self.f.path) as c:head=c.execute('SELECT count,hash FROM pool_capture_head').fetchone()
        self.pins=replace(self.pins,capture_head=head,ledger_head=self.head())
        before=self.files();v=self.read();self.assertEqual(before,self.files())
        self.assertEqual(len(self.table(v,'pool_capture_events')),9)
        self.assertEqual(len(self.table(v,'coordinator_receipts')),2)

    def test_unknown_and_partial_journal_profiles_refuse_complete_output(self):
        self.f.create()
        with sqlite3.connect(self.f.path) as c:c.execute('PRAGMA user_version=99')
        self.refused()
        with sqlite3.connect(self.f.path) as c:c.execute('PRAGMA user_version=0');c.execute('DROP TRIGGER common_bank_run_replace')
        self.refused()

    def test_extra_index_on_owned_journal_table_cannot_hide_under_other_name(self):
        self.f.create()
        with sqlite3.connect(self.f.path) as c:c.execute('CREATE INDEX unrelated_name ON common_bank_runs(plan_hash)')
        with patch.object(m,'_load',side_effect=AssertionError('materialized')) as load:self.refused();load.assert_not_called()

    def test_wrong_external_identity_descriptor_head_and_missing_pins(self):
        for pins in (replace(self.pins,research_identity=(1,2)),replace(self.pins,ledger_head=(0,'a'*64)),
                     replace(self.pins,journal_descriptor=canonical({'version':2})),None):
            with self.subTest(pins=pins),self.assertRaises(m.InventoryUnavailable):m.read_completion_inventory(pins)

    def test_boolean_numeric_pin_is_not_an_exact_integer_head(self):
        with patch.object(m,'_held',side_effect=AssertionError('file open')):
            for head in ((True,self.pins.ledger_head[1]),None,('1',self.pins.ledger_head[1])):
                with self.assertRaises(m.InventoryUnavailable):m.read_completion_inventory(replace(self.pins,ledger_head=head))

    def test_oversized_scalar_preflight_before_any_values_are_loaded(self):
        with sqlite3.connect(self.f.research) as c:c.execute('UPDATE scans SET result=?',('x'*(2*1024*1024+1),))
        with patch.object(m,'_load',side_effect=AssertionError('materialized')) as load:self.refused();load.assert_not_called()

    def test_shared_byte_and_row_limits_before_materialization(self):
        for limit in ('MAX_ROWS','MAX_BYTES'):
            with patch.object(m,limit,1),patch.object(m,'_load',side_effect=AssertionError('materialized')) as load:
                self.refused();load.assert_not_called()

    def test_new_unmatched_stage_or_hash_corruption_refuses_after_success(self):
        self.f.freeze();self.f.reserve();self.read()
        with sqlite3.connect(self.f.path) as c:
            c.execute('DROP TRIGGER common_bank_events_update')
            c.execute("UPDATE common_bank_events SET event_hash=?",('a'*64,))
            c.execute(m.journal.SCHEMA['common_bank_events_update'])
        self.refused()

    def test_writer_contention_all_three_databases_fail_without_mutation(self):
        for path in (self.f.path,self.writer.path,self.f.research):
            before=self.files()
            with sqlite3.connect(path,isolation_level=None) as c:
                c.execute('BEGIN IMMEDIATE');self.refused();c.execute('ROLLBACK')
            self.assertEqual(before,self.files())

    def test_existing_coordinator_contention_and_no_missing_lock_creation(self):
        lock=Path(self.writer.descriptor['lock_path']);fd=os.open(lock,os.O_RDONLY)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);self.refused()
        finally:os.close(fd)
        lock.unlink();self.refused();self.assertFalse(lock.exists())

    def test_symlink_hardlink_modes_wal_and_hot_journal_rejected(self):
        path=self.f.research;real=path.with_name('original-research');path.rename(real);path.symlink_to(real)
        self.refused();path.unlink();real.rename(path)
        alias=path.with_name('alias');os.link(path,alias);self.refused();alias.unlink()
        os.chmod(path,0o644);self.refused();os.chmod(path,0o600)
        journal=Path(str(path)+'-journal');journal.write_bytes(b'hot');self.refused();journal.unlink()
        with sqlite3.connect(path) as c:c.execute('PRAGMA journal_mode=WAL')
        self.refused()

    def test_inode_replacement_during_load_refuses_no_partial_result(self):
        original=m._load;path=self.f.research;old=path.with_name('old-research')
        def changed(*args):
            result=original(*args)
            if not old.exists():path.rename(old);path.write_bytes(old.read_bytes());os.chmod(path,0o600)
            return result
        with patch.object(m,'_load',side_effect=changed):self.refused()

    def test_deadline_and_unsafe_dynamic_research_columns_refuse(self):
        with patch.object(m,'MAX_SECONDS',-1):self.refused()
        with sqlite3.connect(self.f.research) as c:c.execute('ALTER TABLE scans ADD COLUMN arbitrary TEXT')
        self.refused()

    def test_original_page_catalog_is_complete_without_blob_materialization(self):
        v=self.read()
        with sqlite3.connect(self.f.path) as c:
            expected=tuple(c.execute('SELECT hash,raw_bytes,length(payload) FROM pages ORDER BY hash'))
        self.assertEqual(self.table(v,'pages'),expected)
        self.assertTrue(expected)

    def test_stage_future_version_refuses_even_with_rehashed_local_bytes(self):
        self.f.freeze();self.f.reserve()
        with sqlite3.connect(self.f.path) as c:
            row=c.execute('SELECT * FROM common_bank_events').fetchone();event=json.loads(row[2]);event['kind']='common_bank_pending_v2'
            c.execute('DROP TRIGGER common_bank_events_update')
            c.execute('UPDATE common_bank_events SET event_json=?,event_hash=?',(canonical(event),digest({'previous_hash':row[3],'event':event})))
            c.execute(m.journal.SCHEMA['common_bank_events_update'])
        self.refused()

    def test_unsealed_and_missing_research_original_are_explicit_blockers(self):
        self.f.progress.admit('unfinished',{'kind':'ownership_admission_v1','scan_id':'unfinished','mint':self.f.scan['mint'],'created':121})
        with sqlite3.connect(self.f.research) as c:c.execute('DELETE FROM scans')
        v=self.read();self.assertIn('UNSEALED_ADMISSION',v.blockers)
        self.assertIn('ORIGINAL_SCAN_SOURCE_MISMATCH_OR_MISSING',v.blockers)

    def test_reader_blocks_standard_writers_inside_materialization_window(self):
        original=m._load
        def probe(*args):
            for path in (self.f.path,self.writer.path,self.f.research):
                with sqlite3.connect(path,timeout=0,isolation_level=None) as c:
                    with self.assertRaises(sqlite3.OperationalError):c.execute('BEGIN IMMEDIATE')
            return original(*args)
        with patch.object(m,'_load',side_effect=probe):self.read()

    def test_reader_sql_authorizer_denies_mutation_and_attach(self):
        original=m._load
        def probe(c,specs):
            for statement in ('CREATE TEMP TABLE x(y)','PRAGMA query_only=OFF',"ATTACH ':memory:' AS other",'DELETE FROM scans'):
                with self.assertRaises(sqlite3.DatabaseError):c.execute(statement)
            return original(c,specs)
        before=self.files()
        with patch.object(m,'_load',side_effect=probe):self.read()
        self.assertEqual(before,self.files())

    def test_all_typed_profiles_expired_disabled_observations_and_markers_retained(self):
        from tests import test_common_bank_receipt_chain as typed
        from desk import common_bank_receipt_format as fmt
        f=typed.ReceiptChainTests();f.setUp()
        with sqlite3.connect(self.writer.path) as c:prefix=tuple(c.execute('SELECT * FROM coordinator_receipts ORDER BY seq'))
        d=json.loads(self.pins.ledger_descriptor);f.descriptor=d;f.prefix=prefix;f.schema_hash=fmt.SCHEMA_FINGERPRINT
        f.capabilities.append({**d['sources'][0],'version':1,'issuance_enabled':False})
        f.certificate.update(legacy_descriptor_hash=digest(d),legacy_head_hash=prefix[-1][-1],schema_hash=fmt.SCHEMA_FINGERPRINT)
        f.add_marker(slot=None);f.add_marker(pool=None,status='FAILED');f.extra.append(('legacy_observation',m.ledger.PROFILE,{**f.receipt,'captured_at':0}))
        f.build()
        with sqlite3.connect(self.writer.path) as c:
            c.execute('DROP TRIGGER receipt_no_replace');c.execute(fmt.FENCE_SQL)
            for name in (fmt.CERTIFICATE_TABLE,'certificate_no_update','certificate_no_delete','certificate_no_replace'):c.execute(fmt.FORMAT_SQL[name])
            c.execute('INSERT INTO receipt_transition_certificate VALUES(1,?,?)',(f.snapshot.certificate_body,f.snapshot.certificate_hash))
            c.executemany('INSERT INTO coordinator_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',f.snapshot.rows[len(prefix):])
            c.execute('UPDATE ledger_head SET count=?,hash=? WHERE id=1',f.snapshot.head)
        self.pins=replace(self.pins,ledger_head=f.snapshot.head,chain_anchor=f.anchor)
        before=self.files();v=self.read();self.assertEqual(before,self.files())
        self.assertEqual(self.table(v,'coordinator_receipts'),f.snapshot.rows)
        self.pins=replace(self.pins,chain_anchor=None);self.refused()


class PortableInventoryTests(unittest.TestCase):
    def test_unsupported_platform_before_pins_files_or_sqlite(self):
        with patch.object(m,'read_platform_available',return_value=False),patch.object(m,'_json',side_effect=AssertionError),patch.object(m.sqlite3,'connect',side_effect=AssertionError):
            with self.assertRaisesRegex(m.InventoryUnavailable,'PLATFORM'):m.read_completion_inventory(None)
