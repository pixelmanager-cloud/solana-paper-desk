import tempfile,sqlite3,unittest
from pathlib import Path
from desk.storage import snapshot,verify
class StorageTests(unittest.TestCase):
    def test_wal_snapshot_and_restore(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);c=sqlite3.connect(p/'live.sqlite');c.execute('PRAGMA journal_mode=WAL')
            c.execute('create table decisions(id integer,reason text)');c.execute("insert into decisions values(1,'SKIP')");c.commit()
            try:
                report=snapshot(p/'live.sqlite',p/'backup.sqlite');self.assertTrue(verify(p/'backup.sqlite',report['sha256']))
                restored=snapshot(p/'backup.sqlite',p/'restored.sqlite')
                with sqlite3.connect(p/'restored.sqlite') as r:self.assertEqual(r.execute('select * from decisions').fetchall(),[(1,'SKIP')])
                self.assertEqual((p/'backup.sqlite').stat().st_mode&0o777,0o600)
                with self.assertRaises(ValueError):snapshot(p/'live.sqlite',p/'backup.sqlite')
            finally:c.close()
    def test_corrupt_or_wrong_checksum_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bad';p.write_bytes(b'not a database')
            with self.assertRaises(ValueError):verify(p,'0'*64)
            with self.assertRaises(sqlite3.DatabaseError):snapshot(p,p.parent/'out.sqlite')
            self.assertFalse((p.parent/'out.sqlite').exists())
    def test_backup_deadline_removes_unpublished_snapshot(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            with sqlite3.connect(p/'live.sqlite') as c:c.execute('create table marker(value)')
            with patch('desk.storage.time.monotonic',side_effect=[0,31]):
                with self.assertRaises(TimeoutError):snapshot(p/'live.sqlite',p/'backup.sqlite')
            self.assertEqual({x.name for x in p.iterdir()},{'live.sqlite'})
