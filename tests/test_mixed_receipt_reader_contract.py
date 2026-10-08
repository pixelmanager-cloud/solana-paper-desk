"""Missing protected-format contract proofs; NOT a snapshot reader implementation.

All physical databases are fresh synthetic scratch fixtures. No accepted ledger
is migrated or trusted, and no certificate here provisions coordinator authority.
"""
from dataclasses import replace
import sqlite3
import unittest

from desk.common_bank_receipt_chain import ChainUnavailable, validate_receipt_chain
from desk.model import canonical, digest
from desk.pool_receipt_ledger import SCHEMA
from tests import test_common_bank_receipt_chain as fixtures


class MixedReceiptReaderContractTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ReceiptChainTests();self.f.setUp()
        self.c=sqlite3.connect(':memory:',isolation_level=None)
        self.addCleanup(self.c.close)
        for sql in SCHEMA.values():self.c.execute(sql)

    def schema(self):
        return tuple(self.c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))

    def prepare(self, table=None, remove_guard=False):
        if table is not None:
            self.c.execute('CREATE TABLE '+table+'(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL,hash TEXT NOT NULL)')
        if remove_guard:self.c.execute('DROP TRIGGER receipt_no_update')
        self.f.schema_hash=digest(self.schema())
        self.f.certificate['schema_hash']=self.f.schema_hash;self.f.build()
        snapshot=self.f.snapshot
        self.c.execute('INSERT INTO ledger_descriptor VALUES(1,?,?)',(snapshot.descriptor_body,snapshot.descriptor_hash))
        self.c.execute('INSERT INTO ledger_head VALUES(1,?,?)',snapshot.head)
        self.c.executemany('INSERT INTO coordinator_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',snapshot.rows)
        if table is not None:
            self.c.execute('INSERT INTO '+table+' VALUES(1,?,?)',(snapshot.certificate_body,snapshot.certificate_hash))
        return snapshot

    def assert_no_authority(self, result):
        scope=result.enumerate_scope(pool=self.f.pool,mint=self.f.mint,slot=self.f.slot)
        diagnostic=scope.diagnostic();self.assertEqual(diagnostic['decision'],'REJECT')
        for key,value in diagnostic.items():
            if type(value) is bool:self.assertIs(value,False,key)

    def test_matching_arbitrary_sql_hash_does_not_define_protected_schema(self):
        snapshot=self.prepare(table='unreviewed_certificate',remove_guard=True)
        self.assertEqual(digest(self.schema()),self.f.anchor.schema_hash)
        self.assertIsNone(self.c.execute("SELECT sql FROM sqlite_master WHERE name='receipt_no_update'").fetchone())
        self.assert_no_authority(validate_receipt_chain(snapshot,self.f.anchor))
        # An external accepted EXACT format is needed: schema hash matching alone
        # can bind a schema missing an immutable receipt guard without approving it.

    def test_candidate_certificate_storage_does_not_provision_trusted_anchor(self):
        snapshot=self.prepare(table='candidate_selected_certificate')
        candidate=self.c.execute('SELECT body,hash FROM candidate_selected_certificate').fetchone()
        self.assertEqual(candidate,(snapshot.certificate_body,snapshot.certificate_hash))
        self.assert_no_authority(validate_receipt_chain(snapshot,self.f.anchor))
        self.assertIsNone(self.c.execute("SELECT sql FROM sqlite_master WHERE name='format2_certificate'").fetchone())

    def test_metadata_validation_does_not_establish_physical_certificate_presence(self):
        snapshot=self.prepare()
        self.assertEqual(len([s for s in self.schema() if s[0]=='table']),3)
        self.assert_no_authority(validate_receipt_chain(snapshot,self.f.anchor))
        # The candidate has no certificate table at all. Pure snapshot pins are
        # not a protected read, and no default table can safely be inferred.

    def test_old_schema_can_contain_typed_rows_without_reviewed_old_binary_fence(self):
        snapshot=self.prepare(table='unreviewed_certificate')
        actual=dict(self.c.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"))
        self.assertTrue(all(actual[name]==sql for name,sql in SCHEMA.items()))
        self.assert_no_authority(validate_receipt_chain(snapshot,self.f.anchor))
        # Required v1 schema is unchanged. Metadata dispatch cannot certify the
        # REQUIRED-object fence even though an added certificate table exists.

    def test_two_candidate_certificate_layouts_are_not_a_defined_format(self):
        snapshot=self.prepare(table='candidate_certificate_a')
        first_hash=self.f.anchor.schema_hash
        self.c.execute('ALTER TABLE candidate_certificate_a RENAME TO candidate_certificate_b')
        second_hash=digest(self.schema());self.assertNotEqual(second_hash,first_hash)
        certificate=self.f.certificate.copy();certificate['schema_hash']=second_hash
        self.f.schema_hash=second_hash;self.f.certificate=certificate;self.f.build()
        # Two separately repinned synthetic layouts both pass metadata dispatch;
        # accepted schema/provisioning must choose explicitly, outside candidate DB.
        self.assert_no_authority(validate_receipt_chain(snapshot,replace(self.f.anchor,
            schema_hash=first_hash,certificate_hash=snapshot.certificate_hash,head=snapshot.head)))
        self.assert_no_authority(validate_receipt_chain(self.f.snapshot,self.f.anchor))

    def test_byte_preflight_must_use_blob_length_not_character_count(self):
        self.prepare()
        payload='é'*4097
        self.c.execute('INSERT INTO coordinator_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',
            (4,'oversized-synthetic',self.f.pool,self.f.mint,str(self.f.slot),'fixture-source',
             payload,'a'*64,'b'*64,'c'*64))
        chars,bytes_=self.c.execute('SELECT length(payload),length(CAST(payload AS BLOB)) FROM coordinator_receipts WHERE seq=4').fetchone()
        self.assertLessEqual(chars,8192);self.assertGreater(bytes_,8192)
        # This SQL-only preflight witness is not a reader or accepted schema.

    def test_interleaved_head_and_rows_cannot_be_salvaged_as_complete_snapshot(self):
        snapshot=self.prepare()
        saved_head=self.c.execute('SELECT count,hash FROM ledger_head WHERE id=1').fetchone()
        self.f.add_marker(status='FAILED');self.f.build();new=self.f.snapshot.rows[-1]
        self.c.execute('INSERT INTO coordinator_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',new)
        self.c.execute('UPDATE ledger_head SET count=?,hash=? WHERE id=1',self.f.snapshot.head)
        rows=tuple(self.c.execute('SELECT * FROM coordinator_receipts ORDER BY seq'))
        inconsistent=replace(snapshot,rows=rows,head=saved_head)
        old_anchor=replace(self.f.anchor,head=saved_head)
        with self.assertRaises(ChainUnavailable):validate_receipt_chain(inconsistent,old_anchor)
        # A future reader still needs one guarded transaction and atomic head/
        # certificate/schema consistency; separately read rows cannot replace it.
