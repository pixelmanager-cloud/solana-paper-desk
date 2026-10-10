"""Synthetic invocation reuse; no provider I/O or cached gate decisions."""
from contextlib import closing
from contextvars import Context
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from desk.evidence import EvidenceStore
from desk import paper_terminal_reconciliation as terminal
from tools import paper_entry_dispatcher as dispatcher


class DispatchByteScopeTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=EvidenceStore(Path(tmp.name)/'evidence.sqlite')
        self.value={'kind':'SYNTHETIC_ONLY','items':[1],'padding':'x'*10000}
        self.key=self.store.save(self.value)

    def test_dispatch_validation_and_repeated_gates_share_bytes_not_decisions(self):
        def body(expected,**kwargs):
            first=terminal._load(self.store,self.key)  # journal validation path
            first['items'].append(2)
            for _ in range(3):
                self.assertEqual(terminal.gate(self.store,'unused',()),self.value)
            return 'done'
        with patch.object(dispatcher,'_dispatch',side_effect=body),patch.object(terminal,'_gate',side_effect=lambda *a,**k:terminal._load(self.store,self.key)) as gate,patch.object(terminal,'digest',wraps=terminal.digest) as digest:
            self.assertEqual(dispatcher.dispatch({}),'done')
            self.assertEqual(gate.call_count,3)  # every gate still executes
            self.assertEqual(digest.call_count,1)
            dispatcher.dispatch({})
            self.assertEqual(digest.call_count,2)  # no cross-invocation reuse
        self.assertIsNone(terminal._GATE_BYTES.get())

    def test_payload_scalar_and_missing_row_changes_refuse_across_gates(self):
        for n,sql in enumerate(("UPDATE pages SET payload=x'00' WHERE hash=?",'UPDATE pages SET raw_bytes=raw_bytes+1 WHERE hash=?','DELETE FROM pages WHERE hash=?')):
            key=self.store.save({**self.value,'case':n})
            with terminal.verified_bytes_scope():
                terminal._load(self.store,key)
                with closing(self.store.connect()) as c:c.execute(sql,(key,))
                with patch.object(terminal,'_gate',side_effect=lambda *a,**k:terminal._load(self.store,key)):
                    with self.assertRaises(ValueError):terminal.gate(self.store,'unused',())

    def test_nested_exception_and_independent_context_isolation(self):
        with terminal.verified_bytes_scope():
            parent=terminal._GATE_BYTES.get()
            with patch.object(dispatcher,'_dispatch',side_effect=ValueError('stop')):
                with self.assertRaisesRegex(ValueError,'stop'):dispatcher.dispatch({})
            self.assertIs(terminal._GATE_BYTES.get(),parent)
            def independent():
                self.assertIsNone(terminal._GATE_BYTES.get())
                with terminal.verified_bytes_scope():self.assertIsNot(terminal._GATE_BYTES.get(),parent)
                self.assertIsNone(terminal._GATE_BYTES.get())
            Context().run(independent)
        with patch.object(dispatcher,'_dispatch',side_effect=ValueError('stop')):
            with self.assertRaises(ValueError):dispatcher.dispatch({})
        self.assertIsNone(terminal._GATE_BYTES.get())

    def test_page_and_byte_caps_apply_across_nested_gates(self):
        self.assertEqual(terminal.MAX_GATE_CACHE_BYTES,8*1024*1024)
        self.assertEqual(terminal.MAX_GATE_CLASSIFICATION_BYTES,8*1024*1024)
        self.assertEqual(terminal.MAX_GATE_CACHE_PAGES,512)
        for field in ('MAX_GATE_CACHE_BYTES','MAX_GATE_CACHE_PAGES'):
            with patch.object(terminal,field,0),terminal.verified_bytes_scope(),patch.object(terminal,'digest',wraps=terminal.digest) as digest:
                for _ in range(2):
                    with terminal.verified_bytes_scope():terminal._load(self.store,self.key)
                self.assertEqual(digest.call_count,2)
                self.assertEqual(terminal._GATE_BYTES.get()['pages'],{})
