"""One-successor schema bounds; real SQLite and existing ancestor validators."""
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
from desk import runtime_empty_history_successor as edge
from desk import runtime_compatibility as runtime

class ScalarTests(unittest.TestCase):
    def test_partial_table_never_enters_namespace(self):
        with sqlite3.connect(':memory:') as c:
            self.assertIsNone(edge.read(c))
            c.execute(edge.schema())
            with self.assertRaisesRegex(ValueError,'schema/guards/count'):edge.read(c)
            for sql in edge.guards().values():c.execute(sql)
            with self.assertRaisesRegex(ValueError,'schema/guards/count'):edge.read(c)

    def test_oversized_or_wrong_storage_types_refused_before_parse(self):
        for payload,key in (('x'*(runtime.MAX_BYTES+1),'a'*64),(b'{}','a'*64),('{}',b'a'*64)):
            with self.subTest(payload_type=type(payload)),sqlite3.connect(':memory:') as c:
                c.execute(edge.schema())
                for sql in edge.guards().values():c.execute(sql)
                c.execute('INSERT INTO '+edge.TABLE+' VALUES(1,?,?)',(payload,key))
                with patch.object(runtime,'_parse',side_effect=AssertionError('must not parse')),self.assertRaisesRegex(ValueError,'scalar bound'):
                    edge.read(c)

    def test_replace_rowid_update_delete_and_extra_receipt_refused(self):
        with sqlite3.connect(':memory:') as c:
            c.execute(edge.schema())
            for sql in edge.guards().values():c.execute(sql)
            c.execute('INSERT INTO '+edge.TABLE+' VALUES(1,?,?)',('{}','a'*64))
            for sql in ('INSERT OR REPLACE INTO '+edge.TABLE+' VALUES(1,\'{}\',\''+'b'*64+'\')',
                        'UPDATE '+edge.TABLE+' SET rowid=2','DELETE FROM '+edge.TABLE,
                        'INSERT INTO '+edge.TABLE+' VALUES(2,\'{}\',\''+'b'*64+'\')'):
                with self.subTest(sql=sql),self.assertRaises(sqlite3.IntegrityError):c.execute(sql)
            self.assertEqual(c.execute('SELECT * FROM '+edge.TABLE).fetchall(),[(1,'{}','a'*64)])

    def test_no_policy_no_shape_no_implicit_successor(self):
        for malformed in (None,[],{}, {'version':True}):
            with self.assertRaises(ValueError):edge.shape(malformed)
