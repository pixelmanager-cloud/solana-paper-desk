"""Actual four-edge fixture and one immutable performance-only continuation."""
import copy
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch
from desk import runtime_compatibility as runtime,runtime_extensions as ext,runtime_performance_continuation as perf
from desk.model import canonical,digest
from tests import test_runtime_extensions as fixtures

class PerformanceContinuationTests(unittest.TestCase):
    def setUp(self):
        self.h=fixtures.RuntimeExtensionTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        h=self.h;h.append()
        previous=h.pin['successor'];parent=None
        for seq,source in ((2,'a'*64),(3,'b'*64),(4,'c'*64)):
            with sqlite3.connect(h.f.new) as c:parent=ext._bounded_rows(c)[-1][2]
            pin=h.make_pin(seq,parent,previous,source);h.set_pins(h.pins+[pin])
            with patch.object(runtime,'implementation_hash',return_value=source):h.append(pin)
            previous=source
        self.predecessor=previous;self.successor='8'*64
        source_patch=patch.object(runtime,'implementation_hash',return_value=self.successor);source_patch.start();self.addCleanup(source_patch.stop)
        self.policy=h.root/'performance.json';self.policy.write_text(canonical({'version':1,'continuations':[]}))
        for p in (patch.object(perf,'POLICY',self.policy),patch.object(perf,'PREDECESSOR',previous)):
            p.start();self.addCleanup(p.stop)
        self.ctx=h.h.edge['context'];self.cfg=h.f.cfg
        self.old={'source_hash':previous,'config_hash':digest(self.cfg),'tool_hash':'d'*64,'entry_tool_hash':'e'*64,
                  'paths':{k:{'path':v,'device':1,'inode':i} for i,(k,v) in enumerate(self.ctx.items(),1)},'journal':str(h.root/'dispatch.sqlite')}
        self.new={**self.old,'source_hash':self.successor}
        self.pin=perf.plan(h.f.research,h.f.evidence,h.f.new,self.cfg,dispatch_predecessor=self.old,dispatch_successor=self.new)
        self.policy.write_text(canonical({'version':1,'continuations':[self.pin]}))
    def append(self,pin=None):
        h=self.h;return perf.append(h.f.research,h.f.evidence,h.f.new,self.cfg,pin=self.pin if pin is None else pin)
    def test_originals_preserved_restart_and_historical_proofs(self):
        h=self.h
        with sqlite3.connect(h.f.new) as c:
            schemas=c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()
            originals={t:c.execute('SELECT rowid,* FROM '+t).fetchall() for t in ('metadata','state','events','outcomes',runtime.TABLE,'paper_runtime_continuation',ext.TABLE)}
        self.assertEqual(self.append()['status'],'RECORDED')
        self.assertEqual(self.append()['status'],'ALREADY_RECORDED')
        with sqlite3.connect(h.f.new) as c:
            for t,rows in originals.items():self.assertEqual(c.execute('SELECT rowid,* FROM '+t).fetchall(),rows,t)
            for row in schemas:self.assertIn(row,c.execute('SELECT type,name,sql FROM sqlite_master').fetchall())
            self.assertEqual(runtime.require_runtime(c),self.successor)
            self.assertEqual(runtime.require_runtime(c,implementation=self.predecessor),self.predecessor)
            self.assertEqual(perf.dispatch_predecessor(c,self.new),self.old)
            with self.assertRaises(ValueError):perf.dispatch_predecessor(c,{**self.new,'journal':'/tmp/other.sqlite'})
        # Independent fresh SQLite connection: INSERT OR REPLACE cannot bypass guards.
        with sqlite3.connect(h.f.new) as c:
            payload,key=c.execute('SELECT payload,payload_hash FROM '+perf.TABLE).fetchone()
            for sql in ('INSERT OR REPLACE INTO '+perf.TABLE+' VALUES(1,?,?)','UPDATE '+perf.TABLE+' SET payload=?,payload_hash=?'):
                with self.assertRaises(sqlite3.IntegrityError):c.execute(sql,(payload,key))
            with self.assertRaises(sqlite3.IntegrityError):c.execute('DELETE FROM '+perf.TABLE)
    def test_unreviewed_and_changed_context_or_prefix_never_mutates(self):
        h=self.h
        for field,value in (('extension_prefix_hash','f'*64),('parent_receipt_hash','f'*64),('config_hash','f'*64),('predecessor','f'*64)):
            with self.subTest(field=field),self.assertRaises(ValueError):self.append({**self.pin,field:value})
        self.policy.write_text(canonical({'version':1,'continuations':[self.pin,self.pin]}))
        with self.assertRaises(ValueError):self.append()
        with sqlite3.connect(h.f.new) as c:self.assertIsNone(perf.read(c))
    def test_empty_partial_or_mutated_receipt_and_original_guard_fail_closed(self):
        h=self.h;self.append()
        with sqlite3.connect(h.f.new) as c:
            c.execute('DROP TRIGGER '+perf.TABLE+'_update')
            with self.assertRaises(ValueError):runtime.require_runtime(c)
            c.execute(perf.guards()[perf.TABLE+'_update'])
            c.execute('DROP TRIGGER '+ext.TABLE+'_update')
            with self.assertRaises(ValueError):runtime.require_runtime(c)
