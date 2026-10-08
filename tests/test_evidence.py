import sqlite3,tempfile,unittest
from pathlib import Path
from desk.evidence import EvidenceStore
class EvidenceTests(unittest.TestCase):
    def test_roundtrip_and_content_deduplication(self):
        with tempfile.TemporaryDirectory() as d:
            store=EvidenceStore(Path(d)/'e.sqlite');p={'data':[{'slot':123}],'paginationToken':'opaque'}
            key=store.save(p);self.assertEqual(store.save(p),key);self.assertEqual(store.load(key),p)
            with sqlite3.connect(store.path) as c:self.assertEqual(c.execute('select count(*) from pages').fetchone()[0],1)
    def test_storage_budget_failure_preserves_existing_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            store=EvidenceStore(Path(d)/'e.sqlite');key=store.save({'data':[]});store.max_bytes=1
            with self.assertRaises(ValueError):store.save({'data':['new']})
            self.assertEqual(store.load(key),{'data':[]})
    def test_tampered_raw_length_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            store=EvidenceStore(Path(d)/'e.sqlite');key=store.save({'data':[]})
            with sqlite3.connect(store.path) as c:c.execute('update pages set raw_bytes=raw_bytes-1')
            with self.assertRaises(ValueError):store.load(key)
