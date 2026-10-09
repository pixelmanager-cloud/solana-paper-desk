"""Genuine8c base binary; synthetic receipts/state and compiled later binaries."""
import copy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
import zipfile
from unittest.mock import patch

from desk import runtime_compatibility as runtime,runtime_continuation as continuation,runtime_extensions as extensions
from desk.model import canonical,digest
from desk.monitoring_budget import MonitoringBudget,MonitoringBlocked
from desk.paper_checkpoint import read_checkpoint
from tests import test_runtime_continuation as fixtures
from tests.test_runtime_compatibility import dump
from tests.helpers import T

BASE='8c675296c36cd6b088a25aae0c7fcf8d65bdc26673bfdffa49012368acb06427'
ROOT=Path(__file__).resolve().parents[1]


class RuntimeExtensionTests(unittest.TestCase):
    def setUp(self):
        self.h=fixtures.RuntimeContinuationTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.f=self.h.f;self.root=self.h.root;self.source=runtime.implementation_hash()
        self.base_root=self.root/'frozen8c';self.base_root.mkdir()
        with zipfile.ZipFile(ROOT/'fixtures/runtime-extension-predecessor-desk.zip') as z:z.extractall(self.base_root)
        self.h.pin['successor']=BASE
        self.h.policy.write_text(canonical({'version':1,'continuations':[self.h.pin]}))
        self.successful_binary('''from desk import runtime_continuation as continuation
continuation.POLICY=Path(a['continuation_policy'])
continuation.continue_runtime(a['research'],a['evidence'],a['ledger'],a['cfg'],first_receipt_hash=a['first_hash'],predecessor=a['prior'],successor=a['source'])
''',self.base_root,BASE)
        with sqlite3.connect(self.f.new) as c:
            self.original_receipts={table:c.execute(f'SELECT payload,payload_hash FROM {table}').fetchone() for table in (runtime.TABLE,continuation.TABLE)}
        self.first_hash=self.original_receipts[runtime.TABLE][1];self.base_hash=self.original_receipts[continuation.TABLE][1]
        self.policy=self.root/'extension-policy.json';self.policy.write_text(canonical({'version':1,'extensions':[]}))
        p=patch.object(extensions,'POLICY',self.policy);p.start();self.addCleanup(p.stop)
        self.pins=[];self.pin=self.make_pin(1,self.base_hash,BASE,self.source);self.set_pins([self.pin])
        self.successful_binary('''from desk.paper_checkpoint import read_checkpoint
import sqlite3
with sqlite3.connect(a['ledger']) as c:assert read_checkpoint(c) is not None
''',self.base_root,BASE)

    def set_pins(self,pins):
        self.pins=copy.deepcopy(pins);self.policy.write_text(canonical({'version':1,'extensions':pins}))

    def make_pin(self,sequence,parent,predecessor,successor):
        with sqlite3.connect(self.f.new) as c:
            meta=dict(c.execute('SELECT key,value FROM metadata'));state=json.loads(c.execute('SELECT payload FROM state').fetchone()[0])
            pin={'sequence':sequence,'first_receipt_hash':self.first_hash,'continuation_receipt_hash':self.base_hash,
                 'parent_receipt_hash':parent,'predecessor':predecessor,'successor':successor,'config_hash':digest(self.f.cfg),
                 'context':self.h.edge['context'],'checkpoint_hash':digest(state),'metadata_hash':digest(meta)}
            for table in ('events','outcomes'):
                count=c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                pin[table+'_count']=count;pin[table+'_hash']=runtime._prefix(c,table,count)
        return pin

    def append(self,pin=None):
        pin=self.pin if pin is None else pin
        return extensions.append_runtime(self.f.research,self.f.evidence,self.f.new,self.f.cfg,
             **{k:pin[k] for k in ('sequence','first_receipt_hash','continuation_receipt_hash','parent_receipt_hash','predecessor','successor')})

    def run_binary(self,body,root,source,**extras):
        args={'source':source,'runtime_policy':str(self.f.f.policy),'handoff_policy':str(self.f.policy),
              'continuation_policy':str(self.h.policy),'extension_policy':str(getattr(self,'policy','')),
              'research':str(self.f.research),'evidence':str(self.f.evidence),'ledger':str(self.f.new),
              'cfg':self.f.cfg,'at':T,'scan':self.f.target.scan_id,'mint':self.f.target.mint,
              'pool':self.f.target.pool,'taker':self.f.target.taker,'first_hash':self.h.first[1],
              'prior':fixtures.PREDECESSOR,**extras}
        script='''import json,sys
from pathlib import Path
from desk import runtime_compatibility as runtime,monitoring_handoff as handoff,runtime_continuation as continuation
a=json.loads(sys.argv[1]);runtime.POLICY=Path(a['runtime_policy']);handoff.POLICY=Path(a['handoff_policy']);continuation.POLICY=Path(a['continuation_policy'])
assert runtime.implementation_hash()==a['source']
'''+body
        env=os.environ.copy();env['PYTHONPATH']=str(ROOT)
        return subprocess.run([sys.executable,'-c',script,canonical(args)],cwd=root,env=env,capture_output=True,text=True,timeout=40)

    def successful_binary(self,body,root,source,**extras):
        result=self.run_binary(body,root,source,**extras)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr);return result

    def compiled(self,marker):
        root=self.root/marker;root.mkdir()
        shutil.copytree(Path(runtime.__file__).parent,root/'desk',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        (root/'desk/synthetic_runtime_marker.py').write_text('# Synthetic later binary '+marker+'\n')
        source=digest({str(p.relative_to(root/'desk')):p.read_text() for p in sorted((root/'desk').rglob('*')) if p.is_file() and p.suffix in ('.py','.json')})
        return root,source

    def binary_append(self,pin,root,source,*,crash=False):
        body='''from desk import runtime_extensions as extensions
extensions.POLICY=Path(a['extension_policy'])
'''
        if crash:body+='''import os
extensions.require_extensions=lambda *args,**kwargs:os._exit(69)
'''
        body+='''pin=a['pin']
print(extensions.append_runtime(a['research'],a['evidence'],a['ledger'],a['cfg'],**{k:pin[k] for k in ('sequence','first_receipt_hash','continuation_receipt_hash','parent_receipt_hash','predecessor','successor')}))
'''
        result=self.run_binary(body,root,source,pin=pin)
        if not crash:self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        return result

    def snapshot(self):return [dump(p) for p in (self.f.new,self.f.old,self.f.evidence,self.f.research,self.f.pacing)]

    def parent(self):
        with sqlite3.connect(self.f.new) as c:return c.execute(f'SELECT payload_hash FROM {extensions.TABLE} ORDER BY seq DESC LIMIT 1').fetchone()[0]

    def buy_and_pending(self,root,source):
        self.successful_binary('''from tests.test_quote_execution_v3_seam import QuoteV3SeamTests
from desk import engine,quote_execution as qe
from desk.ledger import Ledger
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.monitoring_budget import MonitoringBudget
try:
 from desk import runtime_extensions as extensions
 extensions.POLICY=Path(a['extension_policy'])
except ImportError:pass
f=QuoteV3SeamTests();f.setUp()
try:
 f.fixture.mint=a['mint'];f.fixture.pool=a['pool'];f.fixture.wallet=a['taker']
 event=f.market(a['at']+1);event['paper_source_evidence']={'scan_id':a['scan'],'collector_refs':[]}
 buy=f.fixture.quote('buy',10_000_000,1_000_000,at=a['at']+1);sell=f.fixture.quote('sell',qe.output_raw(buy,a['cfg']),10_000_000,at=a['at']+1)
 ledger=Ledger(a['ledger'],must_exist=True)
 try:assert any(o.get('side')=='buy' for o in ledger.apply(event,a['cfg'],qe.bind_transition(event,(buy,sell)),engine.initial_state))
 finally:ledger.close()
 store=EvidenceStore(a['evidence'],read_only=True);store.read_only=False
 progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
 receipt=MonitoringBudget(store,a['ledger'],a['cfg'],clock=lambda:a['at']+1).reserve_read(progress,a['scan'],'getSlot',[{'commitment':'finalized'}]);assert receipt['id']==2
finally:f.doCleanups()
''',root,source)

    def fence(self,root,source):
        self.successful_binary('''import sqlite3,os
from desk.paper_checkpoint import read_checkpoint,RecoveryRequired
from desk.ledger import Ledger
from desk import engine
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.monitoring_budget import MonitoringBudget,MonitoringBlocked
from desk.paper_read_sources import PaperReadSources,PaperReadError
from unittest.mock import patch
try:
 from desk import runtime_extensions as extensions
 extensions.POLICY=Path(a['extension_policy'])
except ImportError:pass
with sqlite3.connect(a['ledger']) as c:
 before=list(c.iterdump());event=json.loads(c.execute('SELECT payload FROM events ORDER BY seq LIMIT 1').fetchone()[0])
 try:read_checkpoint(c)
 except RecoveryRequired:pass
 else:raise AssertionError('Old reader accepted new journal tail')
ledger=Ledger(a['ledger'],must_exist=True)
try:
 try:ledger.apply(event,a['cfg'],engine.transition,engine.initial_state)
 except ValueError:pass
 else:raise AssertionError('Old duplicate writer accepted new journal tail')
finally:ledger.close()
with sqlite3.connect(a['ledger']) as c:assert list(c.iterdump())==before
store=EvidenceStore(a['evidence'],read_only=True);store.read_only=False
progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
budget=MonitoringBudget(store,a['ledger'],a['cfg'],clock=lambda:a['at']+1)
with store.connect() as c:before=list(c.iterdump())
try:budget.reserve_read(progress,a['scan'],'getSlot',[{'commitment':'finalized'}])
except MonitoringBlocked as exc:assert exc.code=='MONITORING_CONTEXT_BINDING_INVALID',exc.code
else:raise AssertionError('Old reservation spent budget')
with patch('desk.paper_read_sources.os.environ.get',wraps=os.environ.get) as credentials,patch('desk.paper_read_sources.build_opener') as opener:
 try:PaperReadSources(progress,a['scan'],monitoring_budget=budget).rpc('getSlot',[{'commitment':'finalized'}],timeout_seconds=1)
 except PaperReadError as exc:assert exc.code=='MONITORING_CONTEXT_BINDING_INVALID',exc.code
 else:raise AssertionError('Old transport called provider')
 assert all(call.args[0]=='DESK_PROVIDER_PACING_DB' for call in credentials.call_args_list)
 opener.assert_not_called()
with store.connect() as c:assert list(c.iterdump())==before
''',root,source)

    def test_bootstrap_preserves_originals_pending_usage_latch_and_exact_replay(self):
        self.buy_and_pending(self.base_root,BASE)
        for _ in range(18):self.assertTrue(self.f.f.context.progress.reserve(self.f.target.scan_id))
        with self.f.store.connect() as c:c.execute("UPDATE paper_monitoring_budget SET blocked='CLOCK_ROLLBACK'")
        self.pin=self.make_pin(1,self.base_hash,BASE,self.source);self.set_pins([self.pin])
        before=self.snapshot();self.assertEqual(self.append()['status'],'RECORDED');after=self.snapshot()
        self.assertEqual(after[1:],before[1:]);self.assertTrue(set(before[0])<=set(after[0]))
        with sqlite3.connect(self.f.new) as c:
            for table,row in self.original_receipts.items():self.assertEqual(c.execute(f'SELECT payload,payload_hash FROM {table}').fetchone(),row)
            self.assertEqual(runtime.require_runtime(c),self.source);self.assertIsNotNone(read_checkpoint(c))
        self.assertEqual(self.append()['status'],'ALREADY_RECORDED');self.assertEqual(self.snapshot(),after)
        self.fence(self.base_root,BASE);self.assertEqual(self.snapshot(),after)
        with self.f.store.connect() as c:self.assertEqual(c.execute('SELECT total,blocked FROM paper_monitoring_budget').fetchone(),(2,'CLOCK_ROLLBACK'))
        self.assertEqual(self.f.f.context.progress.admission(self.f.target.scan_id)['requests_used'],18)

    def test_intermediate_old_binary_fence_and_anchor_advance(self):
        root,source=self.compiled('intermediate')
        pin=self.make_pin(1,self.base_hash,BASE,source);self.set_pins([pin]);self.binary_append(pin,root,source)
        self.buy_and_pending(root,source)
        next_pin=self.make_pin(2,self.parent(),source,self.source);self.set_pins([pin,next_pin])
        before=self.snapshot();self.assertEqual(self.append(next_pin)['status'],'RECORDED');after=self.snapshot()
        self.assertEqual(after[1:],before[1:]);self.assertTrue(set(before[0])<=set(after[0]))
        self.fence(root,source);self.assertEqual(self.snapshot(),after)
        self.assertEqual(self.append(next_pin)['status'],'ALREADY_RECORDED');self.assertEqual(self.snapshot(),after)

    def test_four_distinct_pinned_edges_then_fifth_refuses(self):
        previous=BASE;parent=self.base_hash;pins=[]
        for sequence in range(1,5):
            root,source=self.compiled('edge'+str(sequence)) if sequence<4 else (None,self.source)
            pin=self.make_pin(sequence,parent,previous,source);pins.append(pin);self.set_pins(pins)
            if root:self.binary_append(pin,root,source)
            else:self.assertEqual(self.append(pin)['status'],'RECORDED')
            previous=source;parent=self.parent()
        with sqlite3.connect(self.f.new) as c:self.assertEqual(runtime.require_runtime(c),self.source)
        before=self.snapshot();fifth=dict(pin,sequence=5,predecessor=self.source,parent_receipt_hash=parent)
        with self.assertRaises(ValueError):self.append(fifth)
        with sqlite3.connect(self.f.new) as c:
            with self.assertRaises(sqlite3.Error):c.execute(f'INSERT INTO {extensions.TABLE} VALUES(5,?,?)',('x','0'*64))
        self.assertEqual(self.snapshot(),before)

    def test_unreviewed_conflicting_stale_anchors_and_identity_are_nonmutating(self):
        before=self.snapshot()
        for field in ('first_receipt_hash','continuation_receipt_hash','parent_receipt_hash','predecessor','successor'):
            with self.assertRaises(ValueError):self.append(dict(self.pin,**{field:'0'*64}))
            self.assertEqual(self.snapshot(),before)
        for field in ('checkpoint_hash','metadata_hash','events_hash','outcomes_hash'):
            pin=dict(self.pin,**{field:'0'*64});self.set_pins([pin])
            with self.assertRaises(ValueError):self.append()
            self.assertEqual(self.snapshot(),before)
        for pins in ([self.pin,self.pin],[self.pin,dict(self.pin,successor='0'*64)]):
            self.set_pins(pins)
            with self.assertRaises(ValueError):self.append()
            self.assertEqual(self.snapshot(),before)
        for sequence in (True,0,-1,5,1.0):
            with self.assertRaises(ValueError):self.append(dict(self.pin,sequence=sequence))
            self.assertEqual(self.snapshot(),before)

    def test_rollback_crash_and_canonical_locks(self):
        import fcntl
        before=self.snapshot()
        with patch.object(extensions,'require_extensions',side_effect=ValueError('post-DDL interruption')):
            with self.assertRaises(ValueError):self.append()
        self.assertEqual(self.snapshot(),before)
        root,source=self.compiled('crash')
        pin=self.make_pin(1,self.base_hash,BASE,source);self.set_pins([pin])
        result=self.binary_append(pin,root,source,crash=True);self.assertEqual(result.returncode,69)
        self.assertEqual(self.snapshot(),before)
        self.set_pins([self.pin])
        for path in (str(self.f.research)+'.jobs-worker.lock',str(self.f.evidence)+'.ownership-invocation.lock',str(self.f.new)+'.paper-cycle.lock'):
            with open(path,'a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaises(ValueError):self.append()
            self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.append()['status'],'RECORDED')

    def test_scalar_bounds_and_schema_before_journal_payload_load(self):
        self.append()
        class Probe:
            def __init__(self,c):self.c=c;self.reads=0
            def execute(self,sql,*args):
                if sql.startswith('SELECT seq,payload,payload_hash FROM paper_runtime_extensions'):
                    self.reads+=1;raise AssertionError('Unbounded payload read')
                return self.c.execute(sql,*args)
        for case in ('ascii','utf8','hash','blob_payload','blob_hash','gap','empty','schema','guards'):
            with self.subTest(case=case):
                with sqlite3.connect(self.f.new) as origin,sqlite3.connect(self.root/(case+'.sqlite')) as c:
                    origin.backup(c)
                    if case=='schema':
                        c.execute('DROP TABLE '+extensions.TABLE);c.execute(f'CREATE TABLE {extensions.TABLE}(seq INTEGER,payload TEXT,payload_hash TEXT)')
                    elif case=='guards':c.execute('DROP TRIGGER '+extensions.TABLE+'_update')
                    else:
                        c.execute('DROP TRIGGER '+extensions.TABLE+'_update')
                        if case in ('ascii','utf8','blob_payload'):
                            value='x'*(runtime.MAX_BYTES+1) if case=='ascii' else '😀'*(runtime.MAX_BYTES//4+1) if case=='utf8' else b'{}'
                            c.execute(f'UPDATE {extensions.TABLE} SET payload=?',(value,))
                        if case in ('hash','blob_hash'):c.execute(f'UPDATE {extensions.TABLE} SET payload_hash=?',('0'*(runtime.MAX_BYTES+1) if case=='hash' else b'0'*64,))
                        if case=='gap':c.execute(f'UPDATE {extensions.TABLE} SET seq=2')
                        if case=='empty':
                            c.execute('DROP TRIGGER '+extensions.TABLE+'_delete');c.execute('DELETE FROM '+extensions.TABLE)
                            c.execute(extensions._guards()[extensions.TABLE+'_delete'])
                        c.execute(extensions._guards()[extensions.TABLE+'_update'])
                    c.commit();before=list(c.iterdump());probe=Probe(c)
                    with patch.object(runtime,'_parse',side_effect=AssertionError('Parser called before scalar bound')) as parser:
                        with self.assertRaises(ValueError):extensions.require_extensions(probe)
                        parser.assert_not_called()
                    self.assertEqual(probe.reads,0);self.assertEqual(list(c.iterdump()),before)

    def test_rehashed_receipt_link_cycle_and_original_prefix_damage_refuse(self):
        self.append()
        with sqlite3.connect(self.f.new) as c:original=json.loads(c.execute(f'SELECT payload FROM {extensions.TABLE}').fetchone()[0])
        for field,value in (('parent_receipt_hash','0'*64),('successor',fixtures.ORIGIN),('predecessor',self.source),('events_hash','0'*64),('sequence',True)):
            altered=copy.deepcopy(original);altered[field]=value
            with sqlite3.connect(self.f.new) as c:
                c.execute('DROP TRIGGER '+extensions.TABLE+'_update')
                c.execute(f'UPDATE {extensions.TABLE} SET payload=?,payload_hash=?',(canonical(altered),digest(altered)))
                c.execute(extensions._guards()[extensions.TABLE+'_update'])
            before=self.snapshot()
            with self.assertRaises(ValueError):self.append()
            self.assertEqual(self.snapshot(),before)
        with sqlite3.connect(self.f.new) as c:
            c.execute('DROP TRIGGER '+extensions.TABLE+'_update')
            c.execute(f'UPDATE {extensions.TABLE} SET payload=?,payload_hash=?',(canonical(original),digest(original)))
            c.execute(extensions._guards()[extensions.TABLE+'_update'])
            c.execute("UPDATE events SET payload_hash=? WHERE seq=1",('0'*64,))
        before=self.snapshot()
        with self.assertRaises(ValueError):self.append()
        self.assertEqual(self.snapshot(),before)

    def test_guarded_replace_and_partial_publication_refuse(self):
        self.append();before=self.snapshot()
        with sqlite3.connect(self.f.new) as c:
            row=c.execute(f'SELECT * FROM {extensions.TABLE}').fetchone()
            for sql,args in ((f'INSERT OR REPLACE INTO {extensions.TABLE} VALUES(?,?,?)',row),
                             (f'UPDATE {extensions.TABLE} SET payload=payload',()),(f'DELETE FROM {extensions.TABLE}',())):
                with self.assertRaises(sqlite3.Error):c.execute(sql,args)
        self.assertEqual(self.snapshot(),before)
        with sqlite3.connect(self.f.new) as c:c.execute('CREATE TABLE paper_runtime_unknown(id INTEGER)')
        before=self.snapshot()
        with self.assertRaises(ValueError):self.append()
        self.assertEqual(self.snapshot(),before)
