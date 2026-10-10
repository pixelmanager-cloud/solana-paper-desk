"""Synthetic ledger/evidence; real compiled source hashes and runtime validators.

No implementation_hash, checkpoint, gate, monitoring or dispatcher acceptance
validator is patched. Historical producer semantics are compiled fixture bytes;
policy paths and fixture construction are the only injected local interfaces.
"""
import json, os, shutil, sqlite3, subprocess, sys, unittest
from pathlib import Path
from unittest.mock import patch
from desk.model import canonical, digest
from desk import runtime_compatibility as runtime, runtime_extensions as extensions
from tests import test_runtime_extensions as chain, test_runtime_compatibility as origin

ROOT=Path(__file__).resolve().parents[1]

COMMON='''
import json,sys,os
from pathlib import Path
from desk import runtime_compatibility as runtime,monitoring_handoff as handoff
from desk import runtime_continuation as continuation,runtime_extensions as extensions
from desk import runtime_performance_continuation as performance
from desk import runtime_empty_history_successor as successor,paper_empty_history_reconciliation as recovery
from desk.model import canonical,digest
a=json.loads(sys.argv[1])
assert runtime.implementation_hash()==a['source']
for module,key in ((runtime,'runtime_policy'),(handoff,'handoff_policy'),(continuation,'continuation_policy'),(extensions,'extension_policy'),(performance,'performance_policy'),(successor,'successor_policy'),(recovery,'recovery_policy')):
 module.POLICY=Path(a[key])
from desk import kraken_pacing_migration
kraken_pacing_migration.POLICY=Path(a['kraken_policy'])
os.environ['DESK_PROVIDER_PACING_DB']=a['pacing']
'''

CREATE_DISPATCH='''
from discovery import continuous as discovery
from desk import kraken_pacing_migration as kraken
from tools import paper_entry_dispatcher as dispatcher
kraken.POLICY=Path(a['kraken_policy'])
pin=kraken.review_plan(a['pacing']);kraken.POLICY.write_text(canonical({'version':1,'pins':[pin]}));kraken.migrate(a['pacing'])
discovery.initialize(a['discovery'])
ctx=dispatcher.plan(config=a['config'],research_db=a['research'],evidence_db=a['evidence'],ledger_db=a['ledger'],discovery_db=a['discovery'],pacing_db=a['pacing'],journal=a['journal'],taker=a['taker'],amount_raw=100000000,pool_fee_bps='25')
dispatcher.initialize(ctx,approved_context_hash=digest(ctx))
Path(a['old_context']).write_text(canonical(ctx))
'''

INSTALL_PERFORMANCE='''
import hashlib
from tools import paper_entry_dispatcher as dispatcher,history_first_paper_entry as entry
old=json.loads(Path(a['old_context']).read_text())
new={**old,'source_hash':a['source'],'tool_hash':hashlib.sha256(Path(dispatcher.__file__).read_bytes()).hexdigest(),'entry_tool_hash':hashlib.sha256(Path(entry.__file__).read_bytes()).hexdigest()}
pin=performance.plan(a['research'],a['evidence'],a['ledger'],a['cfg'],dispatch_predecessor=old,dispatch_successor=new)
performance.POLICY.write_text(canonical({'version':1,'continuations':[pin]}))
assert performance.append(a['research'],a['evidence'],a['ledger'],a['cfg'],pin=pin)['status']=='RECORDED'
Path(a['performance_context']).write_text(canonical(new))
'''

CAPTURE_AND_RECOVER='''
import copy,sqlite3
from unittest.mock import patch
from tests import test_empty_history_no_entry as fixture,test_history_first_paper_entry as first
from desk import paper_cycle as cycle
from desk.job_persistence import JobPersistence
from desk.history_progress import HistoryProgress
from desk.evidence import EvidenceStore
from desk.paper_observation_collector import ObservationTarget
from tools import paper_entry_dispatcher as dispatcher
original_setup=first.HistoryFirstTests.setUp
owner=[None]
def same_context(self):
 original_setup(self)
 f=self.f;old_store=f.f.progress.store
 manifest=old_store.load(f.item.graduation_refs[0]);response=old_store.load(manifest['response_hash'])
 f.f.jobs=JobPersistence(a['research']);f.f.progress=HistoryProgress(EvidenceStore(a['evidence']))
 target=ObservationTarget(a['scan'],a['mint'],a['pool'],a['taker'],100000000)
 f.target=target;f.path=Path(a['ledger']);f.cfg=a['cfg']
 key=f.f.progress.store.save(response);ref=f.f.progress.store.save({**manifest,'response_hash':key})
 from dataclasses import replace
 f.item=replace(f.item,target=target,graduation_refs=(ref,))
 self.pace=Path(a['pacing']);self.config=Path(a['config']);os.environ['DESK_PROVIDER_PACING_DB']=a['pacing']
 self.row={**vars(target),'provenance':f.item.provenance,'pool_fee_bps':'25','graduation_refs':[ref],'known_hazards':[]};self.save()
 owner[0]=self
# Reuse only synthetic producer construction; bind its providers and ledger to
# the already authenticated chain rather than initializing another experiment.
import inspect,textwrap
setup=textwrap.dedent(inspect.getsource(fixture.RetainedEmptyTests.setUp))
setup=setup.replace("f.cfg=f.cfg|{'paper_usd_valuation_version':1};f.path=Path(h.root)/'empty-kraken.sqlite';cycle.initialize(f.path,f.cfg)","assert f.path==Path(fixture_args['ledger'])")
start=setup.index('    from desk import kraken_pacing_migration')
end=setup.index('    h.config.write_text',start)
setup=setup[:start]+setup[end:]
namespace={**vars(fixture),'fixture_args':a};exec(setup,namespace)
r=fixture.RetainedEmptyTests()
try:
 with patch.object(first.HistoryFirstTests,'setUp',same_context):namespace['setUp'](r)
 recovery.POLICY=Path(a['recovery_policy'])
 dc=json.loads(Path(a['performance_context']).read_text());hint={'seq':1,'payload_hash':'1'*64,'raw_hash':'2'*64,'received_at':__import__('time').time()-600,'mint':a['mint'],'pool':a['pool'],'signature':'synthetic-empty-migration','slot':10}
 di={'version':1,'context_hash':digest(dc),'at':hint['received_at']+600,'hint':hint}
 dr={'version':1,'intent_hash':digest(di),'at':di['at'],'scan_id':a['scan'],'result':r.result}
 with dispatcher._journal(a['journal']) as c:
  dispatcher._write(c,'intents','d'*32,di,hint);dispatcher._write(c,'results','d'*32,dr);dispatcher._validate(c,dc)
 r.details.update(dispatcher_context=dc,dispatcher_result_hash=digest(dr))
 pin=recovery.plan(a['research'],a['evidence'],a['ledger'],a['cfg'],pass_id=r.pending[0],outcome_hash=r.result['evidence_hash'],empty_history=r.details,pacing_db=a['pacing'])
 recovery.POLICY.write_text(canonical({'version':1,'associations':[pin]}))
 assert recovery.reconcile(a['research'],a['evidence'],a['ledger'],a['cfg'],pin=pin)['status']=='RECORDED'
 Path(a['recovery_pin']).write_text(canonical(pin))
finally:r.doCleanups()
'''

INSTALL_SUCCESSOR='''
import sqlite3,hashlib
from tools import paper_entry_dispatcher as dispatcher,history_first_paper_entry as entry
from desk import paper_terminal_reconciliation as terminal
from desk.paper_checkpoint import read_checkpoint
from desk.evidence import EvidenceStore
from desk.monitoring_budget import MonitoringBudget
old=json.loads(Path(a['performance_context']).read_text())
new={**old,'source_hash':a['source'],'tool_hash':hashlib.sha256(Path(dispatcher.__file__).read_bytes()).hexdigest(),'entry_tool_hash':hashlib.sha256(Path(entry.__file__).read_bytes()).hexdigest()}
pin=successor.plan(a['ledger'],dispatch_predecessor=old,dispatch_successor=new,recovery_pin=json.loads(Path(a['recovery_pin']).read_text()))
successor.POLICY.write_text(canonical({'version':1,'successors':[pin]}))
assert successor.append(a['ledger'],pin=pin)['status']=='RECORDED'
with sqlite3.connect(a['ledger']) as c:
 assert runtime.require_runtime(c)==a['source'];assert read_checkpoint(c)['positions']=={}
store=EvidenceStore(a['evidence'],read_only=True);store.read_only=False
assert terminal.gate(store,a['research'],()) is None
assert terminal.gate(store,a['research'],(a['scan'],))=='REJECTED_SCAN_RETIRED'
assert MonitoringBudget(store,a['ledger'],a['cfg']).snapshot()['status']=='AVAILABLE'
with dispatcher._journal(a['journal']) as c:
 assert len(dispatcher._validate(c,new)['results'])==1
Path(a['successor_context']).write_text(canonical(new))
'''

class EmptyHistorySuccessorLifecycleTests(unittest.TestCase):
 def binary(self,name,*,predecessor=None,historical=False):
  path,source=self.chain.compiled(name)
  shutil.copytree(ROOT/'tools',path/'tools',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
  if predecessor:
   p=path/'desk/runtime_performance_continuation.py';p.write_text(p.read_text().replace("PREDECESSOR='6dd1ec3f12fe74c3fe94fab5c4be5736e94ba062b6766b9bba3cab3daf319b83'",'PREDECESSOR='+repr(predecessor)))
  if historical:
   p=path/'desk/history_preparation_rejection.py';p.write_text(p.read_text().replace("return {'kind':'history_first_paper_preparation_v4'","return {'kind':'history_first_paper_preparation_v3'"))
   p=path/'desk/paper_history_preparation.py';p.write_text(p.read_text().replace('required_measurements=True','required_measurements=False').replace('empty_window=True','empty_window=False'))
  source=digest({str(p.relative_to(path/'desk')):p.read_text() for p in sorted((path/'desk').rglob('*')) if p.is_file() and p.suffix in ('.py','.json')})
  return path,source
 def run_binary(self,path,source,body):
  args={**self.args,'source':source};env={**os.environ,'PYTHONPATH':str(ROOT)}
  result=subprocess.run([sys.executable,'-c',COMMON+body,canonical(args)],cwd=path,env=env,capture_output=True,text=True,timeout=180)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
 def test_real_four_edges_performance_recovery_and_successor(self):
  original=origin.config;self.chain=chain.RuntimeExtensionTests();self.addCleanup(self.chain.doCleanups)
  with patch.object(origin,'config',side_effect=lambda:original()|{'paper_usd_valuation_version':1}):self.chain.setUp()
  h=self.chain;h.append();previous=h.source
  for seq in (2,3,4):
   root,source=self.binary('synthetic-extension-'+str(seq))
   pin=h.make_pin(seq,h.parent(),previous,source);h.set_pins(h.pins+[pin]);h.binary_append(pin,root,source);previous=source
  f=h.f;self.args={'runtime_policy':str(f.f.policy),'handoff_policy':str(f.policy),'continuation_policy':str(h.h.policy),'extension_policy':str(h.policy),
   'research':str(f.research),'evidence':str(f.evidence),'ledger':str(f.new),'pacing':str(f.pacing),'cfg':f.cfg,'scan':f.target.scan_id,'mint':f.target.mint,'pool':f.target.pool,'taker':f.target.taker}
  for key in ('performance_policy','successor_policy','recovery_policy','kraken_policy','config','discovery','journal','old_context','performance_context','recovery_pin','successor_context'):
   self.args[key]=str(h.root/(key+'.json' if key.endswith('policy') or key in ('config','old_context','performance_context','recovery_pin','successor_context') else key+'.sqlite'))
  Path(self.args['config']).write_text(canonical(f.cfg))
  for key,field in (('performance_policy','continuations'),('successor_policy','successors'),('recovery_policy','associations'),('kraken_policy','pins')):Path(self.args[key]).write_text(canonical({'version':1,field:[]}))
  self.run_binary(root,previous,CREATE_DISPATCH)
  perfroot,perfsource=self.binary('synthetic-performance-producer',predecessor=previous,historical=True)
  self.run_binary(perfroot,perfsource,INSTALL_PERFORMANCE)
  self.run_binary(perfroot,perfsource,CAPTURE_AND_RECOVER)
  nextroot,nextsource=self.binary('synthetic-empty-successor',predecessor=previous)
  self.run_binary(nextroot,nextsource,INSTALL_SUCCESSOR)
