"""Synthetic SQLite format proofs only; no production provisioning/migration."""
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from desk import common_bank_receipt_format as fmt
from desk.common_bank_receipt_chain import ChainUnavailable
from desk.model import digest
from desk.pool_receipt_ledger import SCHEMA, LedgerError
from tests import test_common_bank_receipt_chain as fixture
from tests import test_pool_receipt_ledger as legacy_fixture

ROOT=Path(__file__).resolve().parents[1]


class ReceiptFormatTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'work').mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'work');self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'synthetic-format.sqlite'
        self.c=sqlite3.connect(self.path,isolation_level=None);self.addCleanup(self.c.close)
        self.c.execute('PRAGMA page_size=4096')
        self.c.execute('PRAGMA journal_mode=DELETE')
        self.c.execute('PRAGMA synchronous=FULL')
        # Tests alone construct the proposed layout. Module executes no SQL.
        for sql in fmt.FORMAT_SQL.values():self.c.execute(sql)
        self.f=fixture.ReceiptChainTests();self.f.setUp()
        self.f.schema_hash=fmt.SCHEMA_FINGERPRINT
        self.f.certificate['schema_hash']=fmt.SCHEMA_FINGERPRINT;self.f.build()
        self.c.execute('BEGIN IMMEDIATE')
        self.c.execute('INSERT INTO ledger_descriptor VALUES(1,?,?)',
            (self.f.snapshot.descriptor_body,self.f.snapshot.descriptor_hash))
        self.c.execute('INSERT INTO receipt_transition_certificate VALUES(1,?,?)',
            (self.f.snapshot.certificate_body,self.f.snapshot.certificate_hash))
        self.c.execute('INSERT INTO ledger_head VALUES(1,?,?)',self.f.snapshot.head)
        self.c.executemany('INSERT INTO coordinator_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',self.f.snapshot.rows)
        self.c.execute('COMMIT')

    def inventory(self):
        return tuple(self.c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type COLLATE BINARY,name COLLATE BINARY'))
    def metrics(self):
        return fmt.FormatMetrics(**{name:self.c.execute(sql).fetchone() for name,sql in fmt.PREFLIGHT_SQL.items()})
    def header(self):
        with self.path.open('rb') as f:return f.read(100)
    def validate(self, **changes):
        inputs=dict(inventory=self.inventory(),metrics=self.metrics(),header=self.header(),file_size=self.path.stat().st_size)
        inputs.update(changes)
        return fmt.validate_format_snapshot(self.f.snapshot,self.f.anchor,**inputs)
    def refused(self, **changes):
        with self.assertRaises(ChainUnavailable):self.validate(**changes)

    def test_exact_inventory_fingerprint_header_preflight_and_typed_snapshot(self):
        self.assertEqual(self.inventory(),fmt.EXPECTED_INVENTORY)
        self.assertEqual(fmt.SCHEMA_FINGERPRINT,'186fde57edcf2d53007f6972c66484f64a156d319d4814cec3db062397e687e8')
        self.assertEqual(fmt.SCHEMA_FINGERPRINT,digest({'format':fmt.FORMAT_ID,'inventory':self.inventory()}))
        result=self.validate();scope=result.enumerate_scope(pool=self.f.pool,mint=self.f.mint,slot=self.f.slot)
        self.assertEqual(len(scope.observations),2)
        self.assertEqual(result.records[0].row,self.f.prefix[0])
        for key,value in scope.diagnostic().items():
            if type(value) is bool:self.assertFalse(value,key)
        self.assertEqual([i[3] for i in self.inventory() if i[0]=='index'],[None,None])

    def test_immutable_constants_and_all_legacy_sql_unchanged_except_fence(self):
        with self.assertRaises(TypeError):fmt.FORMAT_SQL['other']='unsafe'
        for name,sql in SCHEMA.items():
            self.assertEqual(fmt.FORMAT_SQL[name],fmt.FENCE_SQL if name==fmt.FENCE_NAME else sql)
        self.assertNotEqual(fmt.FENCE_SQL,SCHEMA[fmt.FENCE_NAME])
        self.assertEqual(fmt.FENCE_SQL.replace('Format2 receipt identity already exists','Receipt identity already exists'),SCHEMA[fmt.FENCE_NAME])

    def test_certificate_update_delete_replace_and_every_rowid_alias_reject(self):
        before=self.c.execute('SELECT rowid,* FROM receipt_transition_certificate').fetchall()
        commands=["UPDATE receipt_transition_certificate SET body='forged'",
                  'DELETE FROM receipt_transition_certificate',
                  "INSERT OR REPLACE INTO receipt_transition_certificate VALUES(1,'forged','forged')"]
        commands += ['UPDATE OR REPLACE receipt_transition_certificate SET '+alias+'=2' for alias in ('rowid','_rowid_','oid','id')]
        for sql in commands:
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError):self.c.execute(sql)
                self.assertEqual(self.c.execute('SELECT rowid,* FROM receipt_transition_certificate').fetchall(),before)

    def test_certificate_collision_does_not_delete_another_row(self):
        # Corrupt-CHECK fixture probes guard even against an impossible second
        # singleton row; exact-format inventory/reader never enables this pragma.
        self.c.execute('PRAGMA ignore_check_constraints=ON')
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("INSERT OR REPLACE INTO receipt_transition_certificate VALUES(2,'x','y')")
        self.assertEqual(self.c.execute('SELECT id FROM receipt_transition_certificate').fetchall(),[(1,)])
        self.c.execute('PRAGMA ignore_check_constraints=OFF')

    def test_fence_retains_seq_publication_payload_and_rowid_collision_guards(self):
        before=self.c.execute('SELECT * FROM coordinator_receipts ORDER BY seq').fetchall()
        first=list(before[0])
        for mode in ('seq','publication','payload','rowid','_rowid_','oid'):
            values=first.copy();values[0]=4;values[1]='new-pub';values[7]='f'*64
            if mode in ('seq','rowid','_rowid_','oid'):values[0]=first[0]
            if mode=='publication':values[1]=first[1]
            if mode=='payload':values[7]=first[7]
            columns=('seq' if mode not in ('rowid','_rowid_','oid') else mode)+',publication_id,pool,mint,slot,source_id,payload,payload_hash,previous_hash,row_hash'
            with self.subTest(mode=mode):
                with self.assertRaises(sqlite3.IntegrityError):self.c.execute('INSERT OR REPLACE INTO coordinator_receipts('+columns+') VALUES(?,?,?,?,?,?,?,?,?,?)',values)
                self.assertEqual(self.c.execute('SELECT * FROM coordinator_receipts ORDER BY seq').fetchall(),before)

    def test_all_missing_changed_or_reordered_inventory_entries_refuse(self):
        inventory=self.inventory()
        for index in range(len(inventory)):
            with self.subTest(index=index):
                with self.assertRaises(ChainUnavailable):fmt.validate_inventory(inventory[:index]+inventory[index+1:])
                entry=list(inventory[index]);entry[3]=('altered' if entry[3] is None else entry[3]+' ')
                with self.assertRaises(ChainUnavailable):fmt.validate_inventory(inventory[:index]+(tuple(entry),)+inventory[index+1:])
        with self.assertRaises(ChainUnavailable):fmt.validate_inventory(tuple(reversed(inventory)))

    def test_extra_table_index_view_trigger_and_temp_object_cannot_enter_inventory(self):
        statements=[('TABLE','CREATE TABLE surprise(x)'),('INDEX','CREATE INDEX surprise ON coordinator_receipts(mint)'),
                    ('VIEW','CREATE VIEW surprise AS SELECT * FROM coordinator_receipts'),
                    ('TRIGGER','CREATE TRIGGER surprise AFTER INSERT ON coordinator_receipts BEGIN SELECT 1; END')]
        for kind,sql in statements:
            self.c.execute(sql)
            with self.assertRaises(ChainUnavailable):fmt.validate_inventory(self.inventory())
            self.c.execute('DROP '+kind+' surprise')
        # Future reader must reject nonempty TEMP schema too, not hide objects by
        # inspecting only main. Contract explicitly disallows ATTACH/TEMP SQL.
        self.c.execute('CREATE TEMP TABLE surprise(x)')
        temp=tuple(self.c.execute('SELECT type,name,tbl_name,sql FROM sqlite_temp_master'))
        with self.assertRaises(ChainUnavailable):fmt.validate_inventory(self.inventory()+temp)

    def test_original_v1_or_weakened_collision_fence_sql_refuses(self):
        self.c.execute('DROP TRIGGER receipt_no_replace');self.c.execute(SCHEMA['receipt_no_replace'])
        with self.assertRaises(ChainUnavailable):fmt.validate_inventory(self.inventory())
        self.c.execute('DROP TRIGGER receipt_no_replace')
        self.c.execute(fmt.FENCE_SQL.replace(' OR payload_hash=NEW.payload_hash',''))
        with self.assertRaises(ChainUnavailable):fmt.validate_inventory(self.inventory())

    def test_unknown_missing_or_partial_atomic_discriminator_refuses(self):
        self.c.execute('DROP TRIGGER certificate_no_delete');self.c.execute('DELETE FROM receipt_transition_certificate')
        with self.assertRaises(ChainUnavailable):fmt.validate_preflight(self.metrics(),self.f.anchor)
        with self.assertRaises(ChainUnavailable):fmt.validate_inventory(self.inventory())
        # No creation/repair or prefix policy returned after this partial state.

    def test_wrong_certificate_pin_or_unreviewed_schema_hash_cannot_be_provisioned(self):
        self.f.anchor=replace(self.f.anchor,certificate_hash='f'*64);self.refused()
        self.f.anchor=replace(self.f.anchor,schema_hash='f'*64);self.refused()

    def test_header_profile_file_page_bounds_and_current_header_count(self):
        original=self.header();size=self.path.stat().st_size
        for offset,value in ((16,b'\x20\x00'),(18,b'\x02'),(19,b'\x02'),(20,b'\x01'),
                            (44,(1).to_bytes(4,'big')),(56,(2).to_bytes(4,'big')),
                            (60,(2).to_bytes(4,'big')),(68,(2).to_bytes(4,'big')),(72,b'\x01'),
                            (92,(0).to_bytes(4,'big'))):
            header=bytearray(original);header[offset:offset+len(value)]=value
            with self.subTest(offset=offset):self.refused(header=bytes(header))
        for value in (True,0,size+1,fmt.MAX_FILE_BYTES+fmt.PAGE_SIZE):self.refused(file_size=value)
        self.refused(header=original[:-1])

    def test_upper_header_page_limit_accepts_only_exact_matching_size(self):
        header=bytearray(self.header());header[28:32]=fmt.MAX_PAGES.to_bytes(4,'big')
        fmt.validate_header(bytes(header),fmt.MAX_FILE_BYTES)
        header[28:32]=(fmt.MAX_PAGES+1).to_bytes(4,'big')
        with self.assertRaises(ChainUnavailable):fmt.validate_header(bytes(header),fmt.MAX_FILE_BYTES)

    def test_invalid_freelist_and_invalid_header_bytes_refuse(self):
        header=bytearray(self.header());header[36:40]=(1).to_bytes(4,'big')
        self.refused(header=bytes(header))
        header=bytearray(self.header());header[32:36]=(2).to_bytes(4,'big')
        self.refused(header=bytes(header))
        header=bytearray(self.header());header[0]=0;self.refused(header=bytes(header))

    def test_utf8_body_overflow_detected_by_sql_scalars_before_json_materialization(self):
        self.c.execute('DROP TRIGGER certificate_no_update')
        self.c.execute('UPDATE receipt_transition_certificate SET body=?',('é'*4097,))
        metrics=self.metrics();self.assertEqual(metrics.certificate[4],8194)
        with patch.object(fmt,'validate_receipt_chain',side_effect=AssertionError('must preflight')):
            self.refused(metrics=metrics)

    def test_blob_metadata_or_wrong_hash_byte_count_rejects_preflight(self):
        self.c.execute('DROP TRIGGER certificate_no_update')
        self.c.execute('UPDATE receipt_transition_certificate SET body=?',(b'blob',))
        with self.assertRaises(ChainUnavailable):fmt.validate_preflight(self.metrics(),self.f.anchor)
        self.c.execute("UPDATE receipt_transition_certificate SET body='text',hash=?",('h'*65,))
        with self.assertRaises(ChainUnavailable):fmt.validate_preflight(self.metrics(),self.f.anchor)

    def test_row_byte_or_type_overflow_refuses_before_typed_chain(self):
        metrics=self.metrics()
        for row in ((3,0,8193,64,100,1,3),(3,1,500,64,100,1,3),(3,0,500,129,100,1,3),
                    (10001,0,500,64,100,1,10001),(3,0,500,64,100,2,4)):
            with self.subTest(row=row),patch.object(fmt,'validate_receipt_chain',side_effect=AssertionError('must preflight')):
                self.refused(metrics=replace(metrics,rows=row))

    def test_cardinality_ids_count_types_and_corrupted_scalar_shapes_refuse(self):
        metrics=self.metrics()
        for field in ('descriptor','certificate','head'):
            for index,value in ((0,0),(0,2),(1,1),(2,0),(3,2),(0,True)):
                values=list(getattr(metrics,field));values[index]=value
                with self.subTest(field=field,index=index):
                    with self.assertRaises(ChainUnavailable):fmt.validate_preflight(replace(metrics,**{field:tuple(values)}),self.f.anchor)
        with self.assertRaises(ChainUnavailable):fmt.validate_preflight(replace(metrics,rows=list(metrics.rows)),self.f.anchor)

    def test_shared_32m_byte_envelope_charges_original_prefix_and_metadata(self):
        metrics=self.metrics();prefix=fmt._preflight_rows(self.f.anchor.legacy_rows)
        metadata=metrics.descriptor[6]-64+metrics.certificate[6]-64+len(self.f.anchor.descriptor_body.encode())
        maximum=fmt.MAX_TOTAL_BYTES-prefix-metadata
        fmt.validate_preflight(replace(metrics,rows=metrics.rows[:4]+(maximum,)+metrics.rows[5:]),self.f.anchor)
        with self.assertRaises(ChainUnavailable):fmt.validate_preflight(replace(metrics,rows=metrics.rows[:4]+(maximum+1,)+metrics.rows[5:]),self.f.anchor)

    def test_damage_after_success_cannot_use_cached_format_success(self):
        self.validate();self.c.execute('DROP TRIGGER certificate_no_update')
        self.refused()
        self.assertNotIn('certificate_no_update',[x[1] for x in self.inventory()])

    def test_real_10000_records_fit_resource_envelope_then_10001_preflight_refuses(self):
        # Dense valid syntax only; manifest refs are synthetic/unavailable.
        for i in range(9997):self.f.add_parent(capture_id='bounded:'+str(i))
        self.f.build()
        self.c.execute('BEGIN IMMEDIATE')
        self.c.executemany('INSERT INTO coordinator_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',self.f.snapshot.rows[3:])
        self.c.execute('UPDATE ledger_head SET count=?,hash=? WHERE id=1',self.f.snapshot.head)
        self.c.execute('COMMIT')
        self.assertEqual(tuple(self.c.execute('SELECT * FROM coordinator_receipts ORDER BY seq')),self.f.snapshot.rows)
        self.assertEqual(self.metrics().rows[0],10000)
        self.assertLess(self.path.stat().st_size,fmt.MAX_FILE_BYTES)
        self.assertLess(self.metrics().rows[4],fmt.MAX_TOTAL_BYTES)
        self.assertEqual(len(self.validate().records),9999)
        row=list(self.f.snapshot.rows[-1]);row[0]=10001;row[1]='overflow';row[7]='e'*64
        self.c.execute('INSERT INTO coordinator_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',row)
        self.c.execute('UPDATE ledger_head SET count=10001 WHERE id=1')
        with patch.object(fmt,'validate_receipt_chain',side_effect=AssertionError('must preflight')):self.refused()

    @unittest.skipUnless(Path('/proc/self/mountinfo').is_file(),'Protected v1 ledger requires Linux')
    def test_actual_v1_read_and_write_refuse_required_fence_without_row_rewrite(self):
        f=legacy_fixture.PoolReceiptLedgerTests();f.setUp();self.addCleanup(f.doCleanups)
        f.writer.publish('legacy-original',f.receipt)
        with sqlite3.connect(f.writer.path) as c:
            before=tuple(c.execute('SELECT * FROM coordinator_receipts ORDER BY seq'))
            old=dict(c.execute('SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL'))
            descriptor=tuple(c.execute('SELECT * FROM ledger_descriptor'))
            head=tuple(c.execute('SELECT * FROM ledger_head'))
            c.execute('DROP TRIGGER receipt_no_replace');c.execute(fmt.FENCE_SQL)
        with self.assertRaisesRegex(LedgerError,'schema/triggers'):f.writer.policy(**f.scope())
        with self.assertRaisesRegex(LedgerError,'schema/triggers'):f.writer.publish('new',f.receipt)
        with sqlite3.connect(f.writer.path) as c:
            self.assertEqual(before,tuple(c.execute('SELECT * FROM coordinator_receipts ORDER BY seq')))
            self.assertEqual(descriptor,tuple(c.execute('SELECT * FROM ledger_descriptor')))
            self.assertEqual(head,tuple(c.execute('SELECT * FROM ledger_head')))
            actual=dict(c.execute('SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL'))
            self.assertEqual({k:v for k,v in actual.items() if k!='receipt_no_replace'},
                             {k:v for k,v in old.items() if k!='receipt_no_replace'})

    def test_loaded_image_must_match_preflight_scalars_after_bounded_dispatch(self):
        metrics=self.metrics()
        # Still within preflight bounds, but not the exact loaded image's bytes.
        changed=metrics.rows[:4]+(metrics.rows[4]+1,)+metrics.rows[5:]
        with self.assertRaisesRegex(ChainUnavailable,'FORMAT_PREFLIGHT_SNAPSHOT_MISMATCH'):
            self.validate(metrics=replace(metrics,rows=changed))

    def test_numeric_schema_preflight_rejects_extra_or_oversized_objects_before_decode(self):
        metrics=self.metrics()
        for schema in ((18,0,64,100,1000),(17,0,129,100,1000),(17,0,64,2049,1000),(17,0,64,100,32769)):
            with patch.object(fmt,'validate_receipt_chain',side_effect=AssertionError('must preflight')):
                self.refused(metrics=replace(metrics,schema=schema))

    def test_direct_validation_helpers_refuse_invalid_utf8_without_uncaught_decode_errors(self):
        inventory=list(self.inventory());entry=list(inventory[-1]);entry[3]='\ud800';inventory[-1]=tuple(entry)
        with self.assertRaises(ChainUnavailable):fmt.validate_inventory(tuple(inventory))
        with self.assertRaises(ChainUnavailable):fmt.validate_preflight(self.metrics(),replace(self.f.anchor,descriptor_body='\ud800'))
