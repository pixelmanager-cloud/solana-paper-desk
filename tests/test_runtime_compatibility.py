"""Genuine pinned old desk sources; all ledger/config/accounting data synthetic."""
import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from desk import runtime_compatibility as rc, engine, paper_cycle
from desk.ledger import Ledger
from desk.model import canonical,digest
from desk.paper_checkpoint import read_checkpoint,RecoveryRequired
from desk.paper_view import paper_status
from tests.helpers import config, T

OLD='15054558d09320369b427a2900028472bf46a870d30e23159b235351df3a72a8'
SOURCE=Path(__file__).resolve().parents[1]/'fixtures/runtime-predecessor-desk.zip'


def dump(path):
    with sqlite3.connect(path) as c:return list(c.iterdump())


class RuntimeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.oldroot=self.root/'old';self.oldroot.mkdir()
        with zipfile.ZipFile(SOURCE) as z:z.extractall(self.oldroot)
        self.cfg=config()|{'paper_signal_policy_version':3,'paper_quote_execution_version':1}
        (self.oldroot/'config.json').write_text(canonical(self.cfg))
        self.ledger=self.root/'ledger.sqlite';self.research=self.root/'research.sqlite';self.evidence=self.root/'evidence.sqlite'
        script='''import json,sys
from desk import paper_cycle
from desk.model import digest
from pathlib import Path
r=Path('desk');actual=digest({str(p.relative_to(r)):p.read_text() for p in sorted(r.rglob('*')) if p.suffix in ('.py','.json')})
assert actual==sys.argv[2]
paper_cycle.initialize(sys.argv[1],json.loads(Path('config.json').read_text()))
'''
        result=subprocess.run([sys.executable,'-c',script,str(self.ledger),OLD],cwd=self.oldroot,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        from tests.test_paper_observation_collector import PaperObservationCollectorTests
        self.context=PaperObservationCollectorTests();self.context.setUp();self.addCleanup(self.context.doCleanups)
        target=self.context.target()
        from desk.ownership_acquisition import _Setup
        _Setup(self.context.progress.store,self.context.jobs.descriptor(target.scan_id),
               self.context.progress.admission(target.scan_id))
        self.research=self.context.jobs.path;self.evidence=self.context.progress.store.path
        self.current=rc.implementation_hash();self.policy=self.root/'policy.json'
        self.edge={'predecessor':OLD,'successor':self.current,'config_hash':digest(self.cfg),'context':{'research_db':str(self.research.resolve()),
            'evidence_db':str(self.evidence.resolve()),'ledger_db':str(self.ledger.resolve())}}
        self.policy.write_text(canonical({'version':1,'transitions':[self.edge]}))
        self.patch=patch.object(rc,'POLICY',self.policy);self.patch.start();self.addCleanup(self.patch.stop)

    def migrate(self,**kw):
        return rc.transition(self.research,self.evidence,self.ledger,self.cfg,
                            **({'predecessor':OLD,'successor':self.current}|kw))

    def test_unrelated_context_cannot_bypass_genuine_held_locks(self):
        import fcntl
        before=dump(self.ledger)
        fake_research=self.root/'text-research';fake_evidence=self.root/'text-evidence'
        for p in (fake_research,fake_evidence):p.write_text('not a database')
        with open(str(self.research)+'.jobs-worker.lock','a') as r, open(str(self.evidence)+'.ownership-invocation.lock','a') as e:
            fcntl.flock(r,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(e,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(ValueError):self.migrate()
            with self.assertRaisesRegex(ValueError,'context'):
                rc.transition(fake_research,fake_evidence,self.ledger,self.cfg,predecessor=OLD,successor=self.current)
        self.assertEqual(dump(self.ledger),before)
        self.assertEqual(fake_research.read_text(),'not a database')

    def test_different_valid_context_and_missing_empty_ledger_pin_refused(self):
        from tests.test_paper_observation_collector import PaperObservationCollectorTests
        other=PaperObservationCollectorTests();other.setUp();self.addCleanup(other.doCleanups);other.target()
        before=dump(self.ledger)
        with self.assertRaisesRegex(ValueError,'context'):
            rc.transition(other.jobs.path,other.progress.store.path,self.ledger,self.cfg,predecessor=OLD,successor=self.current)
        edge=copy.deepcopy(self.edge);edge.pop('context')
        self.policy.write_text(canonical({'version':1,'transitions':[edge]}))
        with self.assertRaises(ValueError):self.migrate()
        self.assertEqual(dump(self.ledger),before)

    def test_explicit_pin_does_not_replace_existing_schema_validation(self):
        before=dump(self.ledger)
        fake=self.root/'fake';fake.write_text('not a database')
        edge=copy.deepcopy(self.edge);edge['context']['research_db']=str(fake)
        self.policy.write_text(canonical({'version':1,'transitions':[edge]}))
        with self.assertRaises(sqlite3.Error):
            rc.transition(fake,self.evidence,self.ledger,self.cfg,predecessor=OLD,successor=self.current)
        self.assertEqual(dump(self.ledger),before)
        self.assertEqual(fake.read_text(),'not a database')

    def test_shared_dispatch_validator_rejects_missing_and_conflicting_binding(self):
        before=dump(self.ledger)
        with sqlite3.connect(self.evidence) as c:
            c.execute('DROP TRIGGER acquisition_setup_update')
            c.execute("UPDATE ownership_acquisition_setup SET job_descriptor_hash=?",('0'*64,))
        evidence_before=dump(self.evidence)
        with self.assertRaises(ValueError):self.migrate()
        self.assertEqual(dump(self.ledger),before)
        self.assertEqual(dump(self.evidence),evidence_before)

    def test_copied_ledger_receipt_does_not_authorize_other_path(self):
        import shutil
        self.migrate();other=self.root/'copied-ledger.sqlite';shutil.copyfile(self.ledger,other)
        before=dump(other)
        with sqlite3.connect(other) as c:
            with self.assertRaisesRegex(ValueError,'path binding'):rc.require_runtime(c)
            with self.assertRaises(RecoveryRequired):read_checkpoint(c)
        self.assertEqual(dump(other),before)

    def test_real_pinned_source_explicit_transition_preserves_records_and_readers(self):
        original=dump(self.ledger);budgets=[dump(p) for p in (self.research,self.evidence)]
        with sqlite3.connect(self.ledger) as c:
            with self.assertRaises(RecoveryRequired):read_checkpoint(c)
        self.assertEqual(dump(self.ledger),original)
        self.assertEqual(self.migrate()['status'],'RECORDED')
        with sqlite3.connect(self.ledger) as c:
            state=read_checkpoint(c)
            self.assertEqual(c.execute("SELECT value FROM metadata WHERE key='implementation_hash'").fetchone()[0],OLD)
            receipt=json.loads(c.execute('SELECT payload FROM paper_runtime_transition').fetchone()[0])
            self.assertEqual(receipt['original_checkpoint'],state)
        # Every original SQL line is preserved, apart from inserted transition DDL/data.
        self.assertTrue(set(original)<=set(dump(self.ledger)))
        self.assertEqual(paper_cycle._state(self.ledger,self.cfg),state)
        self.assertEqual(paper_status(self.ledger,now=0,expected_config=self.cfg)['status'],'LEDGER_PRESENT')
        before=dump(self.ledger);self.assertEqual(self.migrate()['status'],'ALREADY_RECORDED');self.assertEqual(dump(self.ledger),before)
        ledger=Ledger(self.ledger,must_exist=True)
        try:
            event={'schema_version':1,'kind':'clock','event_id':'successor-clock','ts':T,'actor':'paper_monitor'}
            ledger.apply(event,self.cfg,engine.transition,engine.initial_state)
            self.assertEqual(read_checkpoint(ledger.db)['last_ts'],T)
            self.assertEqual(rc.require_runtime(ledger.db),self.current)
        finally:ledger.close()
        self.assertEqual([dump(p) for p in (self.research,self.evidence)],budgets)

    def test_no_allowlist_wrong_source_config_and_malformed_policy_do_not_mutate(self):
        original=dump(self.ledger)
        for body in ('{"version":1,"transitions":[]}','[]','{"version":true,"transitions":[]}',
                     '{"version":1,"version":1,"transitions":[]}',canonical({'version':1,'transitions':[self.edge,self.edge]})):
            self.policy.write_text(body)
            with self.assertRaises(ValueError):self.migrate()
            self.assertEqual(dump(self.ledger),original)
        self.policy.write_text(canonical({'version':1,'transitions':[self.edge]}))
        with self.assertRaises(ValueError):self.migrate(successor='0'*64)
        with sqlite3.connect(self.ledger) as c:c.execute("UPDATE metadata SET value='changed' WHERE key='config'")
        original=dump(self.ledger)
        with self.assertRaises(ValueError):self.migrate()
        self.assertEqual(dump(self.ledger),original)

    def test_corrupt_checkpoint_or_history_not_adopted_even_zero_fills(self):
        with sqlite3.connect(self.ledger) as c:c.execute("UPDATE events SET payload_hash=?",('0'*64,))
        before=dump(self.ledger)
        with self.assertRaises(ValueError):self.migrate()
        self.assertEqual(before,dump(self.ledger))

    def test_atomic_failure_leaves_no_partial_schema(self):
        before=dump(self.ledger)
        with patch.object(rc,'require_runtime',side_effect=ValueError('interrupted validation')):
            with self.assertRaises(ValueError):self.migrate()
        self.assertEqual(before,dump(self.ledger))

    def test_partial_or_tampered_receipt_fail_closed_and_originals_immutable(self):
        self.migrate()
        with sqlite3.connect(self.ledger) as c:
            before=list(c.iterdump())
            for sql in ('DELETE FROM paper_runtime_transition','UPDATE paper_runtime_transition SET payload=\'{}\'',
                        'INSERT OR REPLACE INTO paper_runtime_transition SELECT * FROM paper_runtime_transition'):
                with self.assertRaises(sqlite3.IntegrityError):c.execute(sql)
                self.assertEqual(list(c.iterdump()),before)
            c.execute('DROP TRIGGER paper_runtime_transition_update')
            with self.assertRaises(ValueError):rc.require_runtime(c)
            with self.assertRaises(RecoveryRequired):read_checkpoint(c)

    def test_canonical_lock_contention_refuses_without_mutation(self):
        import fcntl
        before=dump(self.ledger)
        for path in (str(self.research)+'.jobs-worker.lock',str(self.evidence)+'.ownership-invocation.lock',str(self.ledger)+'.paper-cycle.lock'):
            with open(path,'a') as stream:
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaises(ValueError):self.migrate()
            self.assertEqual(dump(self.ledger),before)

    def test_empty_partial_transition_never_becomes_fresh(self):
        fresh=self.root/'partial.sqlite';ledger=Ledger(fresh);self.addCleanup(ledger.close)
        ledger.db.execute('CREATE TABLE paper_runtime_transition(id INTEGER PRIMARY KEY)')
        before=list(ledger.db.iterdump())
        with self.assertRaises(RecoveryRequired):read_checkpoint(ledger.db)
        event={'schema_version':1,'kind':'clock','event_id':'forbidden-adoption','ts':T,'actor':'paper_monitor'}
        with self.assertRaises(ValueError):ledger.apply(event,self.cfg,engine.transition,engine.initial_state)
        self.assertEqual(list(ledger.db.iterdump()),before)

    def test_fake_guard_and_changed_prefix_or_revoked_policy_fail_closed(self):
        self.migrate()
        with sqlite3.connect(self.ledger) as c:
            c.execute('DROP TRIGGER paper_runtime_transition_update')
            c.execute('CREATE TRIGGER paper_runtime_transition_update BEFORE UPDATE ON paper_runtime_transition BEGIN SELECT 1; END')
            with self.assertRaises(ValueError):rc.require_runtime(c)
            c.execute('DROP TRIGGER paper_runtime_transition_update')
            c.execute(rc._guards()['paper_runtime_transition_update'])
            c.execute("UPDATE events SET payload_hash=?",('0'*64,))
            before=list(c.iterdump())
            with self.assertRaises(ValueError):rc.require_runtime(c)
            self.assertEqual(list(c.iterdump()),before)
        self.policy.write_text('{"version":1,"transitions":[]}')
        with sqlite3.connect(self.ledger) as c:
            with self.assertRaises(RecoveryRequired):read_checkpoint(c)

    def test_real_old_source_partial_position_continues_without_rewriting_basis(self):
        import os
        script='''import shutil,sys
from tests.test_quote_execution_v3_seam import QuoteV3SeamTests
from tests.helpers import T
s=QuoteV3SeamTests();s.setUp();f=s.fixture;f.cfg=s.cfg
try:
 f.apply(s.market(),(f.buy,f.exit))
 f.apply(s.market(T+1),(f.quote('sell',f.raw,20000000,at=T+1),f.quote('sell',f.raw*3//10,6000000,at=T+1)))
 f.ledger.close();shutil.copyfile(f.path,sys.argv[1])
finally:s.doCleanups()
'''
        result=subprocess.run([sys.executable,'-c',script,str(self.ledger)],cwd=self.oldroot,
                              env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])},
                              capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        original=dump(self.ledger)
        self.migrate()
        with sqlite3.connect(self.ledger) as c:
            state=read_checkpoint(c);position=next(iter(state['positions'].values()))
            self.assertEqual(position['qty'],'0.689535')
            self.assertGreater(float(position['cost_left']),0)
            self.assertEqual(c.execute("SELECT value FROM metadata WHERE key='implementation_hash'").fetchone()[0],OLD)
        self.assertTrue(set(original)<=set(dump(self.ledger)))

    def test_unsupported_suffix_rejected_consistently_before_duplicate_or_reporting(self):
        self.migrate()
        event={'schema_version':99,'kind':'future_unreviewed','event_id':'future','ts':T,'actor':'paper_monitor'}
        with sqlite3.connect(self.ledger) as c:
            state=json.loads(c.execute('SELECT payload FROM state WHERE id=1').fetchone()[0]);state['last_ts']=T
            c.execute('INSERT INTO events(event_id,ts,payload,payload_hash) VALUES(?,?,?,?)',('future',T,canonical(event),digest(event)))
            c.execute('UPDATE state SET payload=? WHERE id=1',(canonical(state),))
        before=dump(self.ledger)
        with sqlite3.connect(self.ledger) as c:
            with self.assertRaises(ValueError):rc.require_runtime(c)
            with self.assertRaises(RecoveryRequired):read_checkpoint(c)
        with self.assertRaises(RecoveryRequired):paper_cycle._state(self.ledger,self.cfg)
        ledger=Ledger(self.ledger,must_exist=True)
        try:
            with self.assertRaises(ValueError):ledger.apply(event,self.cfg,engine.transition,engine.initial_state)
            with self.assertRaises(ValueError):ledger.report()
        finally:ledger.close()
        self.assertEqual(paper_status(self.ledger,now=T)['status'],'RECOVERY_REQUIRED')
        self.assertEqual(before,dump(self.ledger))

    def test_recomputed_baseline_with_correct_guards_still_requires_valid_checkpoint(self):
        self.migrate()
        with sqlite3.connect(self.ledger) as c:
            row=json.loads(c.execute('SELECT payload FROM paper_runtime_transition').fetchone()[0]);row['original_checkpoint']={}
            c.execute('DROP TRIGGER paper_runtime_transition_update')
            c.execute('UPDATE paper_runtime_transition SET payload=?,payload_hash=?',(canonical(row),digest(row)))
            c.execute(rc._guards()['paper_runtime_transition_update'])
            with self.assertRaises(ValueError):rc.require_runtime(c)
            with self.assertRaises(RecoveryRequired):read_checkpoint(c)

    def test_suffix_preflight_bounds_before_body_materialization(self):
        self.migrate()
        with sqlite3.connect(self.ledger) as c:
            huge=' '*262145
            c.execute('INSERT INTO events(event_id,ts,payload,payload_hash) VALUES(?,?,?,?)',('oversize',T,huge,'0'*64))
            queries=[];c.set_trace_callback(queries.append)
            with self.assertRaises(ValueError):rc.require_runtime(c)
            c.set_trace_callback(None)
            self.assertFalse(any('SELECT event_id,ts,payload,payload_hash FROM events' in q for q in queries))

    def test_review_noop_guard_and_rehashed_empty_snapshot_reproduction(self):
        self.migrate()
        with sqlite3.connect(self.ledger) as c:
            for action in ('INSERT','UPDATE','DELETE'):
                name='paper_runtime_transition_'+action.lower()
                c.execute('DROP TRIGGER '+name)
                c.execute(f'CREATE TRIGGER {name} BEFORE {action} ON paper_runtime_transition BEGIN SELECT 1; END')
            receipt=json.loads(c.execute('SELECT payload FROM paper_runtime_transition').fetchone()[0])
            receipt['original_checkpoint']={}
            c.execute('UPDATE paper_runtime_transition SET payload=?,payload_hash=?',(canonical(receipt),digest(receipt)))
            before=list(c.iterdump())
            with self.assertRaises(ValueError):rc.require_runtime(c)
            with self.assertRaises(RecoveryRequired):read_checkpoint(c)
            self.assertEqual(list(c.iterdump()),before)
