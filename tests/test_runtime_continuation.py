"""Genuine compiled 0fe/31 desk archives; synthetic state/config/receipts only."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
import zipfile
from unittest.mock import patch

from desk import runtime_compatibility as runtime,runtime_continuation as continuation
from desk.model import canonical,digest
from desk.monitoring_budget import MonitoringBudget,MonitoringBlocked
from desk.paper_checkpoint import read_checkpoint
from tests import test_monitoring_handoff as fixtures
from tests.test_runtime_compatibility import dump
from tests.helpers import T

ORIGIN='0fe08c215d591fb972b59749329abcea5c30fbbf5483ea313695ffd175aa433e'
PREDECESSOR='31b249e1dfbb16c12ec89235a7137afab4de989fd05ded52a0231ebdfb9667ae'
ROOT=Path(__file__).resolve().parents[1]


class RuntimeContinuationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.MonitoringHandoffTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.root=self.f.root;self.source=runtime.implementation_hash()
        self.origroot=self.root/'origin';self.oldroot=self.root/'frozen31'
        for path,name in ((self.origroot,'origin'),(self.oldroot,'predecessor')):
            path.mkdir()
            with zipfile.ZipFile(ROOT/f'fixtures/runtime-continuation-{name}-desk.zip') as z:z.extractall(path)
        # Replace only disposable synthetic pre-handoff native initialization.
        self.f.new.unlink();self.f.pins['new_source']=ORIGIN
        self.f.policy.write_text(canonical({'version':1,'handoffs':[self.f.pins]}))
        self.f.charge_original()
        self.run_old('''from desk import paper_cycle,monitoring_handoff as handoff
handoff.POLICY=Path(a['handoff_policy'])
paper_cycle.initialize(a['ledger'],a['cfg'])
c=a['pins']['context']
handoff.activate(c['research_db'],c['evidence_db'],c['old_ledger_db'],c['new_ledger_db'],c['pacing_db'],a['cfg'],pins=a['pins'],at=a['at'])
''',root=self.origroot,source=ORIGIN)
        self.edge={'predecessor':ORIGIN,'successor':PREDECESSOR,'config_hash':digest(self.f.cfg),
                   'context':{'research_db':str(self.f.research),'evidence_db':str(self.f.evidence),'ledger_db':str(self.f.new)}}
        self.f.f.policy.write_text(canonical({'version':1,'transitions':[self.f.edge,self.edge]}))
        self.run_old('''runtime.transition(a['research'],a['evidence'],a['ledger'],a['cfg'],predecessor=a['origin'],successor=a['source'])
''')
        with sqlite3.connect(self.f.new) as c:
            self.first=c.execute('SELECT payload,payload_hash FROM paper_runtime_transition').fetchone()
        self.pin={'first_receipt_hash':self.first[1],'predecessor':PREDECESSOR,'successor':self.source,
                  'config_hash':digest(self.f.cfg),'context':self.edge['context']}
        self.policy=self.root/'continuation-policy.json';self.policy.write_text(canonical({'version':1,'continuations':[self.pin]}))
        p=patch.object(continuation,'POLICY',self.policy);p.start();self.addCleanup(p.stop)

    def run_old(self,body,*,root=None,source=PREDECESSOR,**extras):
        args={'source':source,'origin':ORIGIN,'runtime_policy':str(self.f.f.policy),'handoff_policy':str(self.f.policy),
              'ledger':str(self.f.new),'research':str(self.f.research),'evidence':str(self.f.evidence),
              'cfg':self.f.cfg,'pins':self.f.pins,'at':T,'scan':self.f.target.scan_id,**extras}
        script='''import json,sys
from pathlib import Path
from desk import runtime_compatibility as runtime
a=json.loads(sys.argv[1]);runtime.POLICY=Path(a['runtime_policy'])
assert runtime.implementation_hash()==a['source']
'''+body
        env=os.environ.copy();env['PYTHONPATH']=str(ROOT)
        result=subprocess.run([sys.executable,'-c',script,canonical(args)],cwd=root or self.oldroot,
                              env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def buy_before(self,*,pending=False):
        self.run_old("""from desk import engine,quote_execution as qe,monitoring_handoff as handoff
from desk.ledger import Ledger
from tests.test_quote_execution_v3_seam import QuoteV3SeamTests
handoff.POLICY=Path(a['handoff_policy'])
f=QuoteV3SeamTests();f.setUp()
try:
 f.fixture.mint=a['mint'];f.fixture.pool=a['pool'];f.fixture.wallet=a['taker']
 event=f.market(a['at']+1);event['paper_source_evidence']={'scan_id':a['scan'],'collector_refs':[]}
 buy=f.fixture.quote('buy',10_000_000,1_000_000,at=a['at']+1)
 sell=f.fixture.quote('sell',qe.output_raw(buy,a['cfg']),10_000_000,at=a['at']+1)
 ledger=Ledger(a['ledger'],must_exist=True)
 try:assert any(o.get('side')=='buy' for o in ledger.apply(event,a['cfg'],qe.bind_transition(event,(buy,sell)),engine.initial_state))
 finally:ledger.close()
 if a['pending']:
  from desk.evidence import EvidenceStore
  from desk.history_progress import HistoryProgress
  from desk.monitoring_budget import MonitoringBudget
  store=EvidenceStore(a['evidence'],read_only=True);store.read_only=False
  progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
  budget=MonitoringBudget(store,a['ledger'],a['cfg'],clock=lambda:a['at']+1)
  receipt=budget.reserve_read(progress,a['scan'],'getSlot',[{'commitment':'finalized'}])
  assert receipt['id']==2
finally:f.doCleanups()
""",mint=self.f.target.mint,pool=self.f.target.pool,taker=self.f.target.taker,pending=pending)

    def upgrade(self,**extras):
        return continuation.continue_runtime(self.f.research,self.f.evidence,self.f.new,self.f.cfg,
            **({'first_receipt_hash':self.first[1],'predecessor':PREDECESSOR,'successor':self.source}|extras))

    def snapshot(self):return [dump(p) for p in (self.f.new,self.f.old,self.f.evidence,self.f.research,self.f.pacing)]

    def test_append_replay_restart_preserves_originals_and_pending_charge(self):
        before=self.snapshot()
        self.assertEqual(self.upgrade()['status'],'RECORDED')
        after=self.snapshot();self.assertEqual(after[1:],before[1:]);self.assertTrue(set(before[0])<=set(after[0]))
        with sqlite3.connect(self.f.new) as c:
            self.assertEqual(c.execute('SELECT payload,payload_hash FROM paper_runtime_transition').fetchone(),self.first)
            self.assertEqual(runtime.require_runtime(c),self.source);self.assertIsNotNone(read_checkpoint(c))
        self.assertEqual(self.upgrade()['status'],'ALREADY_RECORDED');self.assertEqual(self.snapshot(),after)
        active=MonitoringBudget(self.f.store,self.f.new,self.f.cfg,clock=lambda:T+1)
        self.assertEqual(active.snapshot()['remaining'],3599)
        target=self.f.buy_new()
        receipt=active.reserve_read(self.f.f.context.progress,target.scan_id,'getSlot',[{'commitment':'finalized'}])
        pending=self.snapshot()
        self.assertEqual(self.upgrade()['status'],'ALREADY_RECORDED');self.assertEqual(self.snapshot(),pending)
        self.assertEqual(receipt['id'],2)
        with self.assertRaises(MonitoringBlocked):
            MonitoringBudget(self.f.store,self.f.new,self.f.cfg,clock=lambda:T+1).reserve_read(self.f.f.context.progress,target.scan_id,'getSlot',[{'commitment':'finalized'}])

    def test_archived31_read_write_reservation_transport_fence(self):
        self.upgrade();self.f.buy_new();before=self.snapshot()
        self.run_old('''import sqlite3
from desk.paper_checkpoint import read_checkpoint,RecoveryRequired
from desk.ledger import Ledger
from desk import engine,monitoring_handoff as handoff
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.monitoring_budget import MonitoringBudget,MonitoringBlocked
from desk.paper_read_sources import PaperReadSources,PaperReadError
from unittest.mock import patch
handoff.POLICY=Path(a['handoff_policy'])
with sqlite3.connect(a['ledger']) as c:
 try:read_checkpoint(c)
 except RecoveryRequired:pass
 else:raise AssertionError('Old reader accepted continuation')
 event=json.loads(c.execute('SELECT payload FROM events ORDER BY seq LIMIT 1').fetchone()[0])
 before=list(c.iterdump())
ledger=Ledger(a['ledger'],must_exist=True)
try:
 try:ledger.apply(event,a['cfg'],engine.transition,engine.initial_state)
 except (ValueError,RecoveryRequired):pass
 else:raise AssertionError('Old duplicate writer accepted continuation')
finally:ledger.close()
with sqlite3.connect(a['ledger']) as c:assert list(c.iterdump())==before
store=EvidenceStore(a['evidence'],read_only=True);store.read_only=False
progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
budget=MonitoringBudget(store,a['ledger'],a['cfg'],clock=lambda:a['at']+1)
with store.connect() as c:original=list(c.iterdump())
try:budget.reserve_read(progress,a['scan'],'getSlot',[{'commitment':'finalized'}])
except MonitoringBlocked:pass
else:raise AssertionError('Old reservation accepted continuation')
import os
with patch('desk.paper_read_sources.os.environ.get',wraps=os.environ.get) as credentials,patch('desk.paper_read_sources.build_opener') as opener:
 try:PaperReadSources(progress,a['scan'],monitoring_budget=budget).rpc('getSlot',[{'commitment':'finalized'}],timeout_seconds=1)
 except PaperReadError:pass
 else:raise AssertionError('Old transport accepted continuation')
 assert all(call.args[0]=='DESK_PROVIDER_PACING_DB' for call in credentials.call_args_list),credentials.call_args_list
 opener.assert_not_called()
with store.connect() as c:assert list(c.iterdump())==original
''')
        self.assertEqual(self.snapshot(),before)

    def test_unreviewed_wrong_first_source_config_context_nonmutating(self):
        before=self.snapshot()
        for field in ('first_receipt_hash','predecessor','successor'):
            with self.assertRaises(ValueError):self.upgrade(**{field:'0'*64})
            self.assertEqual(self.snapshot(),before)
        for field in ('first_receipt_hash','predecessor','successor','config_hash','context'):
            pin=copy.deepcopy(self.pin);pin[field]='0'*64 if field!='context' else dict(pin[field],ledger_db=str(self.root/'other'))
            self.policy.write_text(canonical({'version':1,'continuations':[pin]}))
            with self.assertRaises(ValueError):self.upgrade()
            self.assertEqual(self.snapshot(),before)

    def test_atomic_post_ddl_failure_and_all_locks_nonmutating(self):
        import fcntl
        before=self.snapshot()
        with patch.object(continuation,'require_continuation',side_effect=ValueError('synthetic post-DDL validation failure')):
            with self.assertRaises(ValueError):self.upgrade()
        self.assertEqual(self.snapshot(),before)
        for path in (str(self.f.research)+'.jobs-worker.lock',str(self.f.evidence)+'.ownership-invocation.lock',str(self.f.new)+'.paper-cycle.lock'):
            with open(path,'a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaises(ValueError):self.upgrade()
            self.assertEqual(self.snapshot(),before)

    def test_partial_schema_noop_guards_and_third_edge_rejected(self):
        self.upgrade()
        with sqlite3.connect(self.f.new) as c:c.execute('CREATE TABLE paper_runtime_third(id INTEGER)')
        before=self.snapshot()
        with self.assertRaises(ValueError):self.upgrade()
        self.assertEqual(self.snapshot(),before)
        with sqlite3.connect(self.f.new) as c:
            c.execute('DROP TABLE paper_runtime_third')
            name=continuation.TABLE+'_update';c.execute('DROP TRIGGER '+name)
            c.execute(f'CREATE TRIGGER {name} BEFORE UPDATE ON {continuation.TABLE} BEGIN SELECT 1; END')
        before=self.snapshot()
        with self.assertRaises(ValueError):self.upgrade()
        self.assertEqual(self.snapshot(),before)

    def test_receipt_rehash_tamper_and_original_metadata_rejected(self):
        self.upgrade()
        with sqlite3.connect(self.f.new) as c:
            original=json.loads(c.execute(f'SELECT payload FROM {continuation.TABLE}').fetchone()[0])
        for field in ('first_receipt_hash','predecessor','successor','config_hash','events_hash','outcomes_hash','events_count','context'):
            value=copy.deepcopy(original);value[field]=('0'*64 if field.endswith('hash') or field in ('predecessor','successor') else -1 if field=='events_count' else dict(value[field],ledger_db=str(self.root/'other')))
            with sqlite3.connect(self.f.new) as c:
                c.execute('DROP TRIGGER '+continuation.TABLE+'_update')
                c.execute(f'UPDATE {continuation.TABLE} SET payload=?,payload_hash=?',(canonical(value),digest(value)))
                c.execute(continuation._guards()[continuation.TABLE+'_update'])
            before=self.snapshot()
            with self.assertRaises(ValueError):self.upgrade()
            self.assertEqual(self.snapshot(),before)
        with sqlite3.connect(self.f.new) as c:
            c.execute('DROP TRIGGER '+continuation.TABLE+'_update')
            c.execute(f'UPDATE {continuation.TABLE} SET payload=?,payload_hash=?',(canonical(original),digest(original)))
            c.execute(continuation._guards()[continuation.TABLE+'_update'])
            c.execute("UPDATE metadata SET value=? WHERE key='implementation_hash'",(self.source,))
        before=self.snapshot()
        with self.assertRaises(ValueError):self.upgrade()
        self.assertEqual(self.snapshot(),before)

    def test_first_prefix_suffix_gaps_and_corrupt_checkpoint_rejected(self):
        self.upgrade();self.f.buy_new()
        # Both first and continuation anchor INIT; full suffix grammar also checked.
        with sqlite3.connect(self.f.new) as c:c.execute("UPDATE events SET payload_hash=? WHERE seq=1",('0'*64,))
        before=self.snapshot()
        with self.assertRaises(ValueError):self.upgrade()
        self.assertEqual(self.snapshot(),before)

    def test_partial_receipt_duplicate_policy_and_conflicting_replay_refuse(self):
        before=self.snapshot()
        self.policy.write_text(canonical({'version':1,'continuations':[self.pin,self.pin]}))
        with self.assertRaises(ValueError):self.upgrade()
        self.assertEqual(self.snapshot(),before)
        self.policy.write_text(canonical({'version':1,'continuations':[self.pin]}));self.upgrade()
        with sqlite3.connect(self.f.new) as c:
            c.execute('DROP TRIGGER '+continuation.TABLE+'_delete');c.execute(f'DELETE FROM {continuation.TABLE}')
            c.execute(continuation._guards()[continuation.TABLE+'_delete'])
        before=self.snapshot()
        with self.assertRaises(ValueError):self.upgrade()
        self.assertEqual(self.snapshot(),before)

    def test_independent_continuation_prefix_and_preexisting_pending_preserved(self):
        self.buy_before(pending=True);before=self.snapshot()
        self.upgrade();after=self.snapshot();self.assertEqual(after[1:],before[1:])
        self.assertTrue(set(before[0])<=set(after[0]))
        self.assertEqual(self.upgrade()['status'],'ALREADY_RECORDED');self.assertEqual(self.snapshot(),after)
        with sqlite3.connect(self.f.new) as c:
            first=json.loads(self.first[0]);second=json.loads(c.execute(f'SELECT payload FROM {continuation.TABLE}').fetchone()[0])
            self.assertEqual(first['events_count'],1);self.assertEqual(second['events_count'],2)
            c.execute("UPDATE events SET payload_hash=? WHERE seq=2",('0'*64,))
            # First anchor remains valid; continuation anchor independently rejects.
            self.assertEqual(runtime._prefix(c,'events',first['events_count']),first['events_hash'])
        before=self.snapshot()
        with self.assertRaises(ValueError):self.upgrade()
        self.assertEqual(self.snapshot(),before)

    def test_suffix_gap_and_current_checkpoint_corruption_refuse(self):
        self.upgrade();self.f.buy_new()
        with sqlite3.connect(self.f.new) as c:
            c.execute("UPDATE state SET payload='{}'")
        before=self.snapshot()
        with self.assertRaises(ValueError):self.upgrade()
        self.assertEqual(self.snapshot(),before)
        with sqlite3.connect(self.f.new) as c:c.execute('DELETE FROM events WHERE seq=2')
        before=self.snapshot()
        with self.assertRaises(ValueError):self.upgrade()
        self.assertEqual(self.snapshot(),before)

    def test_continuation_guards_forbid_update_delete_replace(self):
        self.upgrade();before=self.snapshot()
        with sqlite3.connect(self.f.new) as c:
            row=c.execute(f'SELECT * FROM {continuation.TABLE}').fetchone()
            for query,params in ((f'UPDATE {continuation.TABLE} SET payload=payload',()),
                    (f'DELETE FROM {continuation.TABLE}',()),
                    (f'INSERT OR REPLACE INTO {continuation.TABLE} VALUES(?,?,?)',row)):
                with self.assertRaises(sqlite3.Error):c.execute(query,params)
        self.assertEqual(self.snapshot(),before)

    def test_first_receipt_preflight_never_loads_oversize_or_malformed_payload(self):
        class Probe:
            def __init__(self,connection):self.connection=connection;self.payload_reads=0
            def execute(self,sql,*args):
                if sql.startswith('SELECT payload,payload_hash FROM paper_runtime_transition'):
                    self.payload_reads+=1
                    raise AssertionError('First payload loaded before SQL preflight rejected it')
                return self.connection.execute(sql,*args)
        # Full SQL dump includes first bytes; comparing it proves no mutation but
        # never substitutes for the instrumented production reader below.
        cases=('oversize','oversize_utf8','blob_payload','oversize_hash','blob_hash','bad_id','empty','schema','guards')
        for case in cases:
            with self.subTest(case=case):
                database=self.root/('first-preflight-'+case+'.sqlite')
                with sqlite3.connect(self.f.new) as origin,sqlite3.connect(database) as c:
                    origin.backup(c)
                    if case=='guards':
                        c.execute('DROP TRIGGER paper_runtime_transition_update')
                        c.execute('CREATE TRIGGER paper_runtime_transition_update BEFORE UPDATE ON paper_runtime_transition BEGIN SELECT 1; END')
                    elif case=='schema':
                        c.execute('DROP TABLE paper_runtime_transition')
                        c.execute('CREATE TABLE paper_runtime_transition(id INTEGER,payload TEXT,payload_hash TEXT)')
                        c.execute('INSERT INTO paper_runtime_transition VALUES(1,?,?)',self.first)
                    else:
                        c.execute('DROP TRIGGER paper_runtime_transition_update')
                        if case=='oversize':c.execute('UPDATE paper_runtime_transition SET payload=?',('x'*(runtime.MAX_BYTES+1),))
                        if case=='oversize_utf8':c.execute('UPDATE paper_runtime_transition SET payload=?',('😀'*(runtime.MAX_BYTES//4+1),))
                        if case=='blob_payload':c.execute('UPDATE paper_runtime_transition SET payload=?',(b'{}',))
                        if case=='oversize_hash':c.execute('UPDATE paper_runtime_transition SET payload_hash=?',('0'*(runtime.MAX_BYTES+1),))
                        if case=='blob_hash':c.execute('UPDATE paper_runtime_transition SET payload_hash=?',(b'0'*64,))
                        if case=='bad_id':
                            c.execute('PRAGMA ignore_check_constraints=ON');c.execute('UPDATE paper_runtime_transition SET id=2')
                        if case=='empty':
                            c.execute('DROP TRIGGER paper_runtime_transition_delete');c.execute('DELETE FROM paper_runtime_transition')
                            c.execute(runtime._guards()['paper_runtime_transition_delete'])
                        c.execute(runtime._guards()['paper_runtime_transition_update'])
                    c.commit();before=list(c.iterdump());probe=Probe(c)
                    with patch.object(runtime,'_parse',side_effect=AssertionError('Parser called before first SQL preflight')) as parser:
                        with self.assertRaises(ValueError):continuation._first(probe)
                        parser.assert_not_called()
                    self.assertEqual(probe.payload_reads,0);self.assertEqual(list(c.iterdump()),before)

    def test_second_receipt_preflight_never_loads_oversize_or_malformed_payload(self):
        self.upgrade()
        class Probe:
            def __init__(self,connection):self.connection=connection;self.payload_reads=0
            def execute(self,sql,*args):
                if sql.startswith('SELECT payload,payload_hash FROM paper_runtime_'):
                    self.payload_reads+=1
                    raise AssertionError('Receipt payload loaded before second SQL preflight rejected it')
                return self.connection.execute(sql,*args)
        cases=('oversize','oversize_utf8','blob_payload','oversize_hash','blob_hash','bad_id','empty','schema','guards')
        for case in cases:
            with self.subTest(case=case):
                database=self.root/('second-preflight-'+case+'.sqlite')
                with sqlite3.connect(self.f.new) as origin,sqlite3.connect(database) as c:
                    origin.backup(c)
                    if case=='guards':
                        c.execute('DROP TRIGGER '+continuation.TABLE+'_update')
                        c.execute(f'CREATE TRIGGER {continuation.TABLE}_update BEFORE UPDATE ON {continuation.TABLE} BEGIN SELECT 1; END')
                    elif case=='schema':
                        row=c.execute(f'SELECT * FROM {continuation.TABLE}').fetchone()
                        c.execute('DROP TABLE '+continuation.TABLE)
                        c.execute(f'CREATE TABLE {continuation.TABLE}(id INTEGER,payload TEXT,payload_hash TEXT)')
                        c.execute(f'INSERT INTO {continuation.TABLE} VALUES(?,?,?)',row)
                    else:
                        c.execute('DROP TRIGGER '+continuation.TABLE+'_update')
                        if case=='oversize':c.execute(f'UPDATE {continuation.TABLE} SET payload=?',('x'*(runtime.MAX_BYTES+1),))
                        if case=='oversize_utf8':c.execute(f'UPDATE {continuation.TABLE} SET payload=?',('😀'*(runtime.MAX_BYTES//4+1),))
                        if case=='blob_payload':c.execute(f'UPDATE {continuation.TABLE} SET payload=?',(b'{}',))
                        if case=='oversize_hash':c.execute(f'UPDATE {continuation.TABLE} SET payload_hash=?',('0'*(runtime.MAX_BYTES+1),))
                        if case=='blob_hash':c.execute(f'UPDATE {continuation.TABLE} SET payload_hash=?',(b'0'*64,))
                        if case=='bad_id':
                            c.execute('PRAGMA ignore_check_constraints=ON');c.execute(f'UPDATE {continuation.TABLE} SET id=2')
                        if case=='empty':
                            c.execute('DROP TRIGGER '+continuation.TABLE+'_delete');c.execute(f'DELETE FROM {continuation.TABLE}')
                            c.execute(continuation._guards()[continuation.TABLE+'_delete'])
                        c.execute(continuation._guards()[continuation.TABLE+'_update'])
                    c.commit();before=list(c.iterdump());probe=Probe(c)
                    with patch.object(runtime,'_parse',side_effect=AssertionError('Parser called before second SQL preflight')) as parser:
                        with self.assertRaises(ValueError):continuation.require_continuation(probe)
                        parser.assert_not_called()
                    self.assertEqual(probe.payload_reads,0);self.assertEqual(list(c.iterdump()),before)
