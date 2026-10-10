"""Synthetic gate-local reuse; every read still validates current DB bytes."""
from contextlib import closing
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from desk.evidence import EvidenceStore
from desk import paper_terminal_reconciliation as t

class GateReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=EvidenceStore(Path(self.tmp.name)/'evidence.sqlite')
        self.value={'kind':'SYNTHETIC_ONLY','nested':{'rows':[1,2,3]},'padding':'x'*10000}
        self.key=self.store.save(self.value)
    def run_gate(self,body):
        with patch.object(t,'_gate',side_effect=lambda *a,**kw:body()):return t.gate(self.store,'unused',())
    def test_equivalence_isolation_and_one_checksum_per_gate(self):
        original=t.digest
        with patch.object(t,'digest',wraps=original) as digest:
            def body():
                first=t._load(self.store,self.key);first['nested']['rows'].append(99)
                self.assertEqual(t._load(self.store,self.key),self.value)
            self.run_gate(body);self.assertEqual(digest.call_count,1)
            self.run_gate(body);self.assertEqual(digest.call_count,2)
        self.assertIsNone(t._GATE_BYTES.get())
    def test_current_payload_and_scalar_corruption_not_hidden_by_hit(self):
        for field,expression in [('raw_bytes','raw_bytes+1'),('payload',"x'00'")]:
            with self.subTest(field=field):
                store=EvidenceStore(Path(self.tmp.name)/(field+'.sqlite'));key=store.save(self.value)
                def body():
                    t._load(store,key)
                    with closing(store.connect()) as c:c.execute('UPDATE pages SET '+field+'='+expression+' WHERE hash=?',(key,))
                    with self.assertRaises(ValueError):t._load(store,key)
                self.run_gate(body)
    def test_strict_cache_bound_falls_back_without_waiving_validation(self):
        with patch.object(t,'MAX_GATE_CACHE_BYTES',1),patch.object(t,'digest',wraps=t.digest) as digest:
            self.run_gate(lambda:[t._load(self.store,self.key) for _ in range(2)])
            self.assertEqual(digest.call_count,2)
    def test_two_real_inventory_scans_decode_only_matching_attempt_on_repeat(self):
        from desk import paper_preparation_retirement as preparation
        from desk import paper_dispatch_preparation_retirement as dispatch
        scan='a'*32
        attempt={'kind':'paper_read_attempt_v1','scan_id':scan,'requests_used':1}
        self.store.save(attempt)
        with patch.object(t,'_load',wraps=t._load) as load:
            def body():
                first=preparation._attempts(self.store,scan)
                count=load.call_count
                second=dispatch._attempts(self.store,scan)
                self.assertEqual(first,second)
                self.assertEqual(load.call_count-count,1)
                self.assertEqual(first[0][2],attempt)
            self.run_gate(body)
    def test_classification_revalidates_changed_unrelated_page(self):
        def body():
            self.assertEqual(t._classification(self.store,self.key),('SYNTHETIC_ONLY',None,None))
            with closing(self.store.connect()) as c:c.execute("UPDATE pages SET payload=x'00' WHERE hash=?",(self.key,))
            with self.assertRaises(ValueError):t._classification(self.store,self.key)
        self.run_gate(body)
    def test_classification_budget_fallback_and_conflicting_outcome(self):
        from desk import paper_preparation_retirement as preparation
        self.store.save({'kind':'history_first_paper_preparation_outcome_v1','intent_hash':'b'*64})
        with patch.object(t,'MAX_GATE_CACHE_BYTES',1):
            def body():
                self.assertEqual(t._classification(self.store,self.key),('SYNTHETIC_ONLY',None,None))
                self.assertEqual(t._GATE_BYTES.get()['classification_bytes'],0)
                with self.assertRaisesRegex(ValueError,'Conflicting completed'):
                    preparation._attempts(self.store,'a'*32,intent_hash='b'*64)
            self.run_gate(body)
    def test_nested_gate_and_exception_restore_context(self):
        def outer():
            t._load(self.store,self.key);context=t._GATE_BYTES.get()
            with patch.object(t,'_gate',side_effect=lambda *a,**kw:t._load(self.store,self.key)):
                t.gate(self.store,'unused',())
            self.assertIs(t._GATE_BYTES.get(),context)
            raise ValueError('synthetic exception')
        with self.assertRaisesRegex(ValueError,'synthetic exception'):self.run_gate(outer)
        self.assertIsNone(t._GATE_BYTES.get())
