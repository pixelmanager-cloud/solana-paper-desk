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


class PredecessorJournalTests(unittest.TestCase):
    def test_unresolved_intent_after_original_prefix_cannot_gain_authority(self):
        from tests.test_runtime_performance_continuation import PerformanceContinuationTests
        from tools import paper_entry_dispatcher as dispatcher
        from desk.model import digest
        from tests.test_migration_recovery_lineage import mint_fixture
        h=PerformanceContinuationTests();self.addCleanup(h.doCleanups);h.setUp()
        h.append()
        with sqlite3.connect(h.h.f.new) as ledger,sqlite3.connect(h.old['journal']) as journal:
            self.assertEqual(edge._validate_predecessor_journal(ledger,journal,h.new)['intents'],{})
            raw,mint,pool=mint_fixture(77)
            hint={'seq':1,'payload_hash':'1'*64,'raw_hash':'2'*64,'received_at':1000,
                  'mint':mint,'pool':pool,'signature':raw['transaction']['signatures'][0],'slot':raw['slot']}
            intent={'version':1,'context_hash':digest(h.new),'at':1600,'hint':hint}
            dispatcher._write(journal,'intents','1'*32,intent,hint);journal.commit()
            # Byte-prefix verification alone still passes: this is genuinely
            # a newly appended, syntactically valid pending predecessor intent.
            from desk import runtime_performance_continuation as parent
            parent._verify_journal(h.pin,c=journal)
            with self.assertRaisesRegex(ValueError,'Unresolved dispatch'):
                edge._validate_predecessor_journal(ledger,journal,h.new)
