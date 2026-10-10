"""tools.ops.backup / verify_backup: fixture databases only, fake systemctl, no network."""
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from tools.ops import backup as B
from tools.ops import verify_backup as V


def make_db(path, rows=3, wal=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as c:
        if wal:
            c.execute('PRAGMA journal_mode=WAL')
        c.execute('CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)')
        c.executemany('INSERT INTO t(v) VALUES(?)', [('r%d' % i,) for i in range(rows)])
        c.commit()


def tree_digest(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob('*')) if p.is_file()}


class BackupCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.data, self.out = base / 'data', base / 'backups'
        self.data.mkdir()
        self.out.mkdir()
        make_db(self.data / 'a.sqlite')
        make_db(self.data / 'discovery' / 'continuous.sqlite', rows=5)
        make_db(self.data / 'entry-dispatch' / 'dispatch.sqlite', rows=1)
        (self.data / 'a.sqlite-wal').write_bytes(b'')  # sidecars are skipped
        (self.data / 'x.lock').write_text('')
        self.dest = self.out / 'b1'

    def run_backup(self, **kw):
        return B.backup(self.data, kw.pop('dest', self.dest), kw.pop('label', 'unit-test'), **kw)

    def test_roundtrip_manifest_and_verify(self):
        before = tree_digest(self.data)
        result = self.run_backup(expect_count=3)
        self.assertEqual(result['databases'], 3)
        self.assertEqual(tree_digest(self.data), before, 'source bytes unchanged')
        manifest = json.loads((self.dest / 'manifest.json').read_text())
        self.assertEqual(sorted(manifest['databases']),
                         ['a.sqlite', 'discovery/continuous.sqlite', 'entry-dispatch/dispatch.sqlite'])
        self.assertEqual(manifest['databases']['discovery/continuous.sqlite']['tables'], {'t': 5})
        self.assertRegex(manifest['implementation_hash'], '^[0-9a-f]{64}$')
        self.assertEqual(manifest['label'], 'unit-test')
        self.assertEqual(oct(self.dest.stat().st_mode & 0o777), '0o700')
        self.assertEqual(oct((self.dest / 'a.sqlite').stat().st_mode & 0o777), '0o600')
        self.assertEqual(V.verify(self.dest)['databases'], 3)
        self.assertEqual(V.main([str(self.dest)]), 0)

    def test_expect_count_mismatch_fails_and_leaves_nothing(self):
        with self.assertRaises(B.BackupError):
            self.run_backup(expect_count=4)
        self.assertFalse(self.dest.exists())

    def test_symlink_database_refused(self):
        os.symlink(self.data / 'a.sqlite', self.data / 'alias.sqlite')
        with self.assertRaises(B.BackupError):
            self.run_backup()
        self.assertFalse(self.dest.exists())

    def test_symlinked_directory_refused(self):
        other = self.data.parent / 'other'
        make_db(other / 'z.sqlite')
        os.symlink(other, self.data / 'linked')
        with self.assertRaises(B.BackupError):
            self.run_backup()

    def test_hardlink_alias_refused(self):
        os.link(self.data / 'a.sqlite', self.data / 'hard.sqlite')
        with self.assertRaises(B.BackupError):
            self.run_backup()
        self.assertFalse(self.dest.exists())

    def test_externally_hardlinked_database_refused(self):
        # nlink>1 with the other name outside data: only the single-link rule catches it
        os.link(self.data / 'a.sqlite', self.out / 'elsewhere.sqlite')
        with self.assertRaises(B.BackupError):
            self.run_backup()
        self.assertFalse(self.dest.exists())

    def test_non_quiesced_unit_refused(self):
        states = {'desk-a.service': 'inactive', 'desk-b.service': 'active'}
        with self.assertRaises(B.BackupError):
            self.run_backup(require_quiesced=list(states), runner=states.get)
        self.assertFalse(self.dest.exists())
        states['desk-b.service'] = 'failed'
        self.run_backup(require_quiesced=list(states), runner=states.get)

    def test_unknown_state_is_not_quiet(self):
        with self.assertRaises(B.BackupError):
            self.run_backup(require_quiesced=['u'], runner=lambda unit: '')

    def test_existing_destination_refused_and_untouched(self):
        self.dest.mkdir()
        (self.dest / 'keep').write_text('x')
        with self.assertRaises(B.BackupError):
            self.run_backup()
        self.assertEqual((self.dest / 'keep').read_text(), 'x')

    def test_symlink_destination_and_parent_refused(self):
        os.symlink(self.out, self.out.parent / 'alias')
        with self.assertRaises(B.BackupError):
            self.run_backup(dest=self.out.parent / 'alias' / 'b2')
        os.symlink(self.out / 'nowhere', self.out / 'dangling')
        with self.assertRaises(B.BackupError):
            self.run_backup(dest=self.out / 'dangling')

    def test_corrupted_copy_detected_by_verifier(self):
        self.run_backup()
        target = self.dest / 'a.sqlite'
        with target.open('r+b') as f:
            f.seek(-1, 2)
            last = f.read(1)
            f.seek(-1, 2)
            f.write(bytes([last[0] ^ 0xFF]))
        with self.assertRaises(B.BackupError):
            V.verify(self.dest)
        self.assertEqual(V.main([str(self.dest)]), 1)

    def test_verifier_detects_extra_missing_and_symlink(self):
        self.run_backup()
        (self.dest / 'extra.sqlite').write_bytes(b'')
        with self.assertRaises(B.BackupError):
            V.verify(self.dest)
        (self.dest / 'extra.sqlite').unlink()
        (self.dest / 'a.sqlite').unlink()
        with self.assertRaises(B.BackupError):
            V.verify(self.dest)

    def test_verifier_detects_tampered_manifest(self):
        self.run_backup()
        path = self.dest / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['databases']['a.sqlite']['tables']['t'] = 99
        path.write_text(json.dumps(manifest))
        with self.assertRaises(B.BackupError):
            V.verify(self.dest)

    def test_valid_but_modified_database_detected(self):
        # still integrity-ok, but content changed after the manifest was written
        self.run_backup()
        with closing(sqlite3.connect(self.dest / 'a.sqlite')) as c:
            c.execute("INSERT INTO t(v) VALUES('late')")
            c.commit()
        with self.assertRaises(B.BackupError):
            V.verify(self.dest)

    def test_wal_source_consistent_while_writer_holds_transaction(self):
        make_db(self.data / 'wal.sqlite', rows=4, wal=True)
        writer = sqlite3.connect(self.data / 'wal.sqlite', isolation_level=None)
        try:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("INSERT INTO t(v) VALUES('uncommitted')")
            self.run_backup()
        finally:
            writer.execute('ROLLBACK')
            writer.close()
        manifest = json.loads((self.dest / 'manifest.json').read_text())
        self.assertEqual(manifest['databases']['wal.sqlite']['tables'], {'t': 4})
        self.assertEqual(V.verify(self.dest)['databases'], 4)
        with closing(sqlite3.connect(self.dest / 'wal.sqlite')) as c:
            self.assertEqual(c.execute('PRAGMA journal_mode').fetchone(), ('delete',))

    def test_cli_failure_exit_code(self):
        self.dest.mkdir()
        argv = ['--data', str(self.data), '--destination', str(self.dest), '--label', 'x']
        self.assertEqual(B.main(argv), 1)
        argv[3] = str(self.out / 'fresh')
        self.assertEqual(B.main(argv), 0)


if __name__ == '__main__':
    unittest.main()
