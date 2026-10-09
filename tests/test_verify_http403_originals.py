"""Synthetic stopped-database fixtures; no provider or production access."""
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from tools import verify_http403_originals as helper


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = Path(self.tmp.name) / 'pre'
        self.live = Path(self.tmp.name) / 'live'
        for root in (self.old, self.live):
            root.mkdir()
        for name in helper.DATABASES:
            path = self.old / name
            path.parent.mkdir(exist_ok=True)
            with closing(sqlite3.connect(path)) as c:
                c.execute('CREATE TABLE originals(id INTEGER PRIMARY KEY, payload BLOB, amount, pending TEXT)')
                c.execute('CREATE INDEX original_index ON originals(pending)')
                c.execute('INSERT INTO originals VALUES(1,?,?,NULL)', (b'original\x00bytes', 6))
                c.execute('CREATE TABLE paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT,outcome_hash TEXT)')
                c.execute("INSERT INTO paper_observation_passes VALUES('original','hash',NULL)")
                c.commit()
            target = self.live / name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(path, target)
        for name, module in (('evidence.sqlite', helper.retirement),
                             ('token2022-boost-00b7c302.sqlite', helper.runtime)):
            with closing(sqlite3.connect(self.live / name)) as c:
                c.execute(module._schema())
                for sql in module._guards().values():
                    c.execute(sql)
                if module is helper.runtime:
                    c.execute('INSERT INTO ' + module.TABLE + " VALUES(1,'fixture','hash')")
                else:
                    c.execute('INSERT INTO ' + module.TABLE + " VALUES('fixture','scan','fixture','hash')")
                c.commit()
        self.source = helper.runtime.implementation_hash()

    def verify(self):
        return helper.verify(self.old, self.live, source=self.source)

    def mutate(self, name, sql):
        with closing(sqlite3.connect(self.live / name)) as c:
            c.execute(sql)
            c.commit()

    def test_exact_additions_and_originals_pass_without_mutation(self):
        before = {str(p): p.read_bytes() for root in (self.old, self.live) for p in root.rglob('*.sqlite')}
        result = self.verify()
        self.assertEqual(result['databases_checked'], 10)
        self.assertEqual(result['original_tables_verified'], 20)
        self.assertEqual(before, {p: Path(p).read_bytes() for p in before})

    def test_changes_to_every_original_database_reject(self):
        for name in helper.DATABASES:
            with self.subTest(name=name):
                self.mutate(name, 'UPDATE originals SET amount=7')
                with self.assertRaisesRegex(ValueError, 'Original rows changed'):
                    self.verify()
                self.mutate(name, 'UPDATE originals SET amount=6')

    def test_original_null_completion_blob_and_storage_type_reject(self):
        for sql in ("UPDATE paper_observation_passes SET outcome_hash='completed'",
                    "UPDATE originals SET payload=X'00'", 'UPDATE originals SET amount=6.0'):
            with self.subTest(sql=sql):
                self.mutate('evidence.sqlite', sql)
                with self.assertRaisesRegex(ValueError, 'Original rows changed'):
                    self.verify()
                self.mutate('evidence.sqlite', 'UPDATE paper_observation_passes SET outcome_hash=NULL')
                with closing(sqlite3.connect(self.live / 'evidence.sqlite')) as c:
                    c.execute('UPDATE originals SET payload=?,amount=6', (b'original\x00bytes',))
                    c.commit()

    def test_same_name_trigger_cannot_hide_in_inventory(self):
        self.mutate('raw.sqlite', 'CREATE TRIGGER originals AFTER INSERT ON originals BEGIN SELECT 1; END')
        with self.assertRaisesRegex(ValueError, 'unexpected schema additions'):
            self.verify()

    def test_extra_autoindex_is_not_ignored(self):
        self.mutate('raw.sqlite', 'CREATE UNIQUE INDEX extra ON originals(amount)')
        with self.assertRaises(ValueError):
            self.verify()

    def test_changed_guard_and_original_schema_reject(self):
        self.mutate('evidence.sqlite', 'DROP TRIGGER ' + helper.retirement.TABLE + '_update')
        with self.assertRaises(ValueError):
            self.verify()

    def test_two_retirements_reject(self):
        self.mutate('evidence.sqlite', 'INSERT INTO ' + helper.retirement.TABLE + " VALUES('other','other','x','x')")
        with self.assertRaisesRegex(ValueError, 'Exactly one'):
            self.verify()

    def test_missing_file_source_mismatch_and_alias_reject(self):
        with self.assertRaisesRegex(ValueError, 'source mismatch'):
            helper.verify(self.old, self.live, source='0' * 64)
        with self.assertRaises(ValueError):
            helper.verify(self.live, self.live, source=self.source)
        path = self.live / 'raw.sqlite'
        path.unlink()
        path.symlink_to(self.old / 'raw.sqlite')
        with self.assertRaisesRegex(ValueError, 'Canonical'):
            self.verify()

    def test_payload_bound_before_materialization(self):
        self.mutate('research.sqlite', 'UPDATE originals SET payload=zeroblob(16777217)')
        with self.assertRaisesRegex(ValueError, 'row byte bound'):
            self.verify()

    def test_hidden_original_rowid_change_rejects(self):
        self.mutate('evidence.sqlite', 'UPDATE paper_observation_passes SET rowid=99')
        with self.assertRaisesRegex(ValueError, 'Original rows changed'):
            self.verify()

    def test_without_rowid_preserved_primary_key_and_change(self):
        for root in (self.old, self.live):
            with closing(sqlite3.connect(root / 'raw.sqlite')) as c:
                c.execute('CREATE TABLE keyed(k TEXT PRIMARY KEY,v BLOB) WITHOUT ROWID')
                c.execute("INSERT INTO keyed VALUES('key',X'0001')")
                c.commit()
        self.verify()
        self.mutate('raw.sqlite', "UPDATE keyed SET k='changed'")
        with self.assertRaisesRegex(ValueError, 'Original rows changed'):
            self.verify()

    def test_shadowed_rowid_uses_unshadowed_alias(self):
        for root in (self.old, self.live):
            with closing(sqlite3.connect(root / 'raw.sqlite')) as c:
                c.execute('CREATE TABLE shadow(rowid TEXT,_rowid_ TEXT)')
                c.execute("INSERT INTO shadow VALUES('ordinary','ordinary')")
                c.commit()
        self.verify()
        self.mutate('raw.sqlite', 'UPDATE shadow SET oid=99')
        with self.assertRaisesRegex(ValueError, 'Original rows changed'):
            self.verify()

    def test_all_rowid_aliases_shadowed_refuses(self):
        for root in (self.old, self.live):
            with closing(sqlite3.connect(root / 'raw.sqlite')) as c:
                c.execute('CREATE TABLE shadow(ROWID TEXT,_rowid_ TEXT,oid TEXT)')
                c.commit()
        with self.assertRaisesRegex(ValueError, 'aliases shadowed'):
            self.verify()

    def test_generated_rowid_alias_does_not_hide_original_identity(self):
        for root in (self.old, self.live):
            with closing(sqlite3.connect(root / 'raw.sqlite')) as c:
                c.execute('CREATE TABLE generated(value TEXT, rowid TEXT GENERATED ALWAYS AS (value) VIRTUAL)')
                c.execute("INSERT INTO generated(value) VALUES('original')")
                c.commit()
        self.verify()
        self.mutate('raw.sqlite', 'UPDATE generated SET _rowid_=99')
        with self.assertRaisesRegex(ValueError, 'Original rows changed'):
            self.verify()

    def test_generated_aliases_all_shadowed_refuse(self):
        for root in (self.old, self.live):
            with closing(sqlite3.connect(root / 'raw.sqlite')) as c:
                c.execute('CREATE TABLE generated(value TEXT, rowid TEXT GENERATED ALWAYS AS (value) STORED, _rowid_ TEXT GENERATED ALWAYS AS (value) VIRTUAL, oid TEXT GENERATED ALWAYS AS (value) VIRTUAL)')
                c.commit()
        with self.assertRaisesRegex(ValueError, 'aliases shadowed'):
            self.verify()


if __name__ == '__main__':
    unittest.main()
