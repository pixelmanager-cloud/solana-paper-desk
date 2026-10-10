"""Synthetic ledger/evidence; real compiled source hashes and runtime validators.

Installed acceptance runs in subprocesses with real compiled source hashes and
unpatched runtime/checkpoint/gate/monitoring/dispatcher validators. The reused
ancestral helpers construct synthetic historical source identities and evidence;
no synthetic identity patch enters these acceptance subprocesses.
"""
import json, os, shutil, sqlite3, subprocess, sys, time, unittest
from pathlib import Path
from unittest.mock import patch
from desk.model import canonical, digest
from desk import runtime_compatibility as runtime, runtime_extensions as extensions

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
for name in ('paper_preparation_retirement','paper_dispatch_preparation_retirement','paper_migration_no_entry','paper_intake_uncaptured_retirement'):
 module=__import__('desk.'+name,fromlist=[name]);module.POLICY=Path(a[name+'_policy'])
from desk import paper_intake_uncaptured_retirement as intake_retirement
intake_retirement.CASE=a['intake_case']
os.environ['DESK_PROVIDER_PACING_DB']=a['pacing']
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
import copy,sqlite3,time
# Earlier synthetic wire fixtures can advance the durable wall-clock watermark
# slightly ahead of the host. Wait for the host; never reset pacing state.
with sqlite3.connect(a['pacing']) as paced:
 lag=paced.execute('SELECT MAX(high_water) FROM state').fetchone()[0]-time.time()
assert lag<5,lag
if lag>0:time.sleep(lag+.05)
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
 from tests import test_paper_cycle as protocol_fixture
 from tests.test_pools import PoolTests
 import inspect,textwrap
 construction=textwrap.dedent(inspect.getsource(PoolTests.setUp)).replace('bytes([7])*32','bytes([51])*32')
 pool_namespace=dict(vars(__import__('tests.test_pools',fromlist=['PoolTests'])));exec(construction,pool_namespace)
 self.f=protocol_fixture.PaperCycleTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
 fresh_protocol=PoolTests();pool_namespace['setUp'](fresh_protocol);self.f.f.protocol=fresh_protocol
 self.root=Path(self.f.f.tmp.name);self.targets=self.root/'targets.json'
 f=self.f;f.http_calls=[];f.sell_output=100_000_000;old_store=f.f.progress.store
 manifest=old_store.load(f.item.graduation_refs[0]);response=old_store.load(manifest['response_hash'])
 f.f.jobs=JobPersistence(a['research']);f.f.progress=HistoryProgress(EvidenceStore(a['evidence']))
 f.f.at=int(__import__('time').time())
 admit=JobPersistence.admit
 def profiled(jobs,*args,**kw):return admit(jobs,*args,**kw,paper_token_profile_version=a['cfg'].get('paper_token_profile_version',0))
 with patch.object(JobPersistence,'admit',profiled):target=f.f.target(quantity=100000000)
 f.target=target;f.path=Path(a['ledger']);f.cfg=a['cfg']
 from tests.test_migration_recovery_lineage import mint_fixture
 from desk.security import base58
 from desk.programs import unbase58
 from desk.providers import SOL
 raw,_,_=mint_fixture(51);raw['blockTime']=f.f.at-600
 ix=raw['meta']['innerInstructions'][0]['instructions'][0];data=bytearray(unbase58(ix['data']));data[136:144]=raw['blockTime'].to_bytes(8,'little',signed=True);data[-32:]=unbase58(SOL);ix['data']=base58(data)
 response={'data':[raw],'paginationToken':None}
 key=f.f.progress.store.save(response);ref=f.f.progress.store.save({**manifest,'params':[target.pool,manifest['params'][1]],'response_hash':key})
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
setup=setup.replace("history=c.execute('SELECT id FROM ownership_history').fetchone()[0]","history=c.execute('SELECT id FROM ownership_history WHERE budget=?',(f.target.scan_id,)).fetchone()[0]")
namespace={**vars(fixture),'fixture_args':a};exec(setup,namespace)
r=fixture.RetainedEmptyTests()
try:
 with patch.object(first.HistoryFirstTests,'setUp',same_context):namespace['setUp'](r)
 recovery.POLICY=Path(a['recovery_policy'])
 r.pending=(r.result['pass_id'],r.result['intent_hash'])
 with r.store.connect() as evidence:
  prep=[]
  for identity,ih,oh in evidence.execute('SELECT id,intent_hash,outcome_hash FROM paper_observation_passes WHERE outcome_hash IS NOT NULL'):
   original=r.store.load(ih)
   if original.get('kind')=='history_first_paper_preparation_v3' and original['target']['target']['scan_id']==r.h.f.target.scan_id:prep.append((identity,ih,oh))
  histories=evidence.execute('SELECT id FROM ownership_history WHERE budget=?',(r.h.f.target.scan_id,)).fetchall()
 assert len(prep)==len(histories)==1,(prep,histories)
 prior=prep[0];history=histories[0][0];cov=r.progress.snapshot(history)['coverage']
 r.details.update(preparation_pass_id=prior[0],preparation_intent_hash=prior[1],preparation_outcome_hash=prior[2],history_id=history,coverage_hash=cov['evidence_hash'])
 a.update(scan=r.h.f.target.scan_id,mint=r.h.f.target.mint,pool=r.h.f.target.pool)
 Path(a['case_file']).write_text(canonical({k:a[k] for k in ('scan','mint','pool')}))
 dc=json.loads(Path(a['performance_context']).read_text());hint={'seq':1,'payload_hash':'1'*64,'raw_hash':'2'*64,'received_at':__import__('time').time()-600,'mint':a['mint'],'pool':a['pool'],'signature':r.store.load(r.store.load(r.h.f.item.graduation_refs[0])['response_hash'])['data'][0]['transaction']['signatures'][0],'slot':10}
 di={'version':1,'context_hash':digest(dc),'at':hint['received_at']+600,'hint':hint}
 dr={'version':1,'intent_hash':digest(di),'at':di['at'],'scan_id':a['scan'],'result':r.result}
 with dispatcher._journal(a['journal']) as c:
  dispatcher._write(c,'intents','fa'*16,di,hint);dispatcher._write(c,'results','fa'*16,dr);dispatcher._validate(c,dc)
 r.details.update(dispatcher_context=dc,dispatch_id='fa'*16,dispatcher_result_hash=digest(dr))
 pin=recovery.plan(a['research'],a['evidence'],a['ledger'],a['cfg'],pass_id=r.pending[0],outcome_hash=r.result['evidence_hash'],empty_history=r.details,pacing_db=a['pacing'])
 recovery.POLICY.write_text(canonical({'version':1,'associations':[pin]}))
 with sqlite3.connect(a['ledger']) as original:ledger_before=list(original.iterdump())
 with dispatcher._journal(a['journal']) as original:journal_before=dispatcher._read_journal(original)
 charges_before=r.progress.admission(a['scan'])
 assert recovery.reconcile(a['research'],a['evidence'],a['ledger'],a['cfg'],pin=pin)['status']=='RECORDED'
 with sqlite3.connect(a['ledger']) as original:assert list(original.iterdump())==ledger_before
 with dispatcher._journal(a['journal']) as original:assert dispatcher._read_journal(original)==journal_before
 assert r.progress.admission(a['scan'])==charges_before and charges_before['requests_used']==12
 with r.store.connect() as original:assert original.execute('SELECT outcome_hash FROM paper_observation_passes WHERE id=?',(r.pending[0],)).fetchone()==(None,)
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
from tests.test_migration_recovery_lineage import mint_fixture
raw,mint,pool=mint_fixture(54)
hint={'seq':54,'payload_hash':'3'*64,'raw_hash':'4'*64,'received_at':__import__('time').time()-600,'mint':mint,'pool':pool,'signature':raw['transaction']['signatures'][0],'slot':raw['slot']}
unresolved={'version':1,'context_hash':digest(old),'at':hint['received_at']+600,'hint':hint}
# Publish a real fixture-only unresolved row. Restore using a SQLite snapshot
# after proving rejection; no immutable guard or validator is bypassed.
with dispatcher._journal(a['journal']) as c:
 checkpoint=sqlite3.connect(':memory:');c.backup(checkpoint)
 dispatcher._write(c,'intents','fd'*16,unresolved,hint)
 try:
  with sqlite3.connect(a['ledger']) as ledger:
   try:successor._validate_predecessor_journal(ledger,c,old)
   except ValueError as exc:assert 'Unresolved dispatch' in str(exc),str(exc)
   else:raise AssertionError('New unresolved predecessor intent accepted')
 finally:checkpoint.backup(c);checkpoint.close()
pin=successor.plan(a['ledger'],dispatch_predecessor=old,dispatch_successor=new,recovery_pin=json.loads(Path(a['recovery_pin']).read_text()))
successor.POLICY.write_text(canonical({'version':1,'successors':[pin]}))
with sqlite3.connect(a['ledger']) as original:
 tables=[v[0] for v in original.execute("SELECT name FROM sqlite_master WHERE type='table'")]
 rows_before={t:original.execute('SELECT rowid,* FROM '+t+' ORDER BY rowid').fetchall() for t in tables}
 schema_before=original.execute('SELECT type,name,tbl_name,sql FROM sqlite_master').fetchall()
assert successor.append(a['ledger'],pin=pin)['status']=='RECORDED'
with sqlite3.connect(a['ledger']) as original:
 for t,rows in rows_before.items():assert original.execute('SELECT rowid,* FROM '+t+' ORDER BY rowid').fetchall()==rows,t
 for row in schema_before:assert row in original.execute('SELECT type,name,tbl_name,sql FROM sqlite_master').fetchall()
with sqlite3.connect(a['ledger']) as c:
 assert runtime.require_runtime(c)==a['source'];assert read_checkpoint(c)['positions']=={}
store=EvidenceStore(a['evidence'],read_only=True);store.read_only=False
assert terminal.gate(store,a['research'],()) is None
assert terminal.gate(store,a['research'],(a['scan'],))=='REJECTED_SCAN_RETIRED'
assert MonitoringBudget(store,a['ledger'],a['cfg']).snapshot()['status']=='AVAILABLE'
with dispatcher._journal(a['journal']) as c:
 assert len(dispatcher._validate(c,new)['results'])==2
 values=dispatcher._read_journal(c);retained=values['context']
 late={**next(iter(values['intents'].values())),'context_hash':digest(old)}
 from tests.test_migration_recovery_lineage import mint_fixture
 raw,mint,pool=mint_fixture(53)
 late['hint']={**late['hint'],'mint':mint,'pool':pool,'signature':raw['transaction']['signatures'][0],'slot':raw['slot']}
 checkpoint=sqlite3.connect(':memory:');c.backup(checkpoint)
 try:
  dispatcher._write(c,'intents','fc'*16,late,late['hint'])
  try:dispatcher._validate(c,new)
  except ValueError:pass
  else:raise AssertionError('Late predecessor context accepted')
 finally:checkpoint.backup(c);checkpoint.close()
 assert dispatcher._read_journal(c)['context']==retained
 assert len(dispatcher._validate(c,new)['results'])==2
Path(a['successor_context']).write_text(canonical(new))
'''

POST_LIFECYCLE = CAPTURE_AND_RECOVER[:CAPTURE_AND_RECOVER.index('r=fixture.RetainedEmptyTests()')].replace('bytes([51])*32','bytes([52])*32').replace('mint_fixture(51)','mint_fixture(52)') + '''
from contextlib import closing
from desk import paper_read_sources as transport,paper_terminal_reconciliation as terminal
from tests import test_paper_cycle as protocol
r=fixture.RetainedEmptyTests()
try:
 with patch.object(first.HistoryFirstTests,'setUp',same_context):
  r.h=first.HistoryFirstTests();r.h.setUp()
 r.addCleanup(r.h.doCleanups);r.store=r.h.f.f.progress.store;r.progress=r.h.f.f.progress
 r.ctx={k:a[v] for k,v in (('research_db','research'),('evidence_db','evidence'),('ledger_db','ledger'),('pacing_db','pacing'))}
 # Reuse the existing synthetic HTTP wire construction and native-clock lifecycle.
 construction=textwrap.dedent(inspect.getsource(fixture.RetainedEmptyTests.setUp))
 construction=construction[construction.index('    code=textwrap.dedent'):construction.index('    original_intent=')]
 namespace={**vars(fixture),'inspect':inspect,'textwrap':textwrap,'protocol':protocol,'transport':transport,'time':time,'json':json,'canonical':canonical,'digest':digest,'Path':Path,'Response':__import__('tests.test_paper_read_sources',fromlist=['Response']).Response,'h':r.h,'f':r.h.f,'self':r}
 exec(textwrap.dedent(construction),namespace)
 fixture_http=r.http
 class TraceHTTP:
  def open(self,request,*,timeout):
   try:return fixture_http.open(request,timeout=timeout)
   except Exception as exc:
    print('SYNTHETIC_HTTP_ERROR',request.full_url,repr(exc),flush=True);raise
 r.http=TraceHTTP()
 r.empty_history=False
 method=textwrap.dedent(inspect.getsource(fixture.RetainedEmptyTests.test_post_recovery_real_buy_monitor_full_exit))
 start=method.index('    pin=recovery.plan(');end=method.index('    retired=',start)
 method=method[:start]+method[end:]
 method=method.replace('f.sell_output=10_000_000','f.sell_output=100_000_000')
 method=method.replace('retired=f.target.scan_id','retired=fixture_args["scan"]').replace('f.target=f.f.target()','f.target=f.target')
 method=method.replace("self.assertEqual(result['status'],'COMPLETE',result)","self.assertEqual(result['status'],'COMPLETE',result);print('NATIVE_BUY',result['attempted_requests'],[(v.get('side'),v.get('reason')) for v in result['outcomes']],flush=True)")
 method=method.replace("self.assertEqual(mark['status'],'COMPLETE',mark)","self.assertEqual(mark['status'],'COMPLETE',mark);print('NATIVE_HELD',mark['monitoring_attempted_requests'],flush=True)")
 method=method.replace("self.assertEqual(exited['status'],'COMPLETE',exited)","self.assertEqual(exited['status'],'COMPLETE',exited);print('NATIVE_EXIT',exited['monitoring_attempted_requests'],[(v.get('side'),v.get('reason')) for v in exited['outcomes']],flush=True)")
 namespace={**vars(fixture),'fixture_args':a};exec(method,namespace)
 namespace['test_post_recovery_real_buy_monitor_full_exit'](r)
 with sqlite3.connect(a['ledger']) as c:assert runtime.require_runtime(c)==a['source']
 assert terminal.gate(r.store,a['research'],()) is None
 dc=json.loads(Path(a['successor_context']).read_text())
 with dispatcher._journal(a['journal']) as c:assert len(dispatcher._validate(c,dc)['results'])==2
 print('BUY_HELD_EXIT_RESTART_VALIDATORS_PASS',flush=True)
finally:r.doCleanups()
'''

class EmptyHistorySuccessorLifecycleTests(unittest.TestCase):
 def binary(self,name,*,predecessor=None,historical=False):
  path=self.root/name;path.mkdir()
  shutil.copytree(ROOT/'desk',path/'desk',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
  (path/'desk/synthetic_runtime_marker.py').write_text('# Synthetic later binary '+name+'\n')
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
  started=time.monotonic()
  result=subprocess.run([sys.executable,'-c',COMMON+body,canonical(args)],cwd=path,env=env,capture_output=True,text=True,timeout=180)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  print('COMPILED_PHASE_PASS',round(time.monotonic()-started,3),result.stdout.strip(),flush=True)
 def test_real_four_edges_performance_recovery_and_successor(self):
  # Reuse the complete authenticated migration lineage, including its original
  # unresolved intents, full captured proofs, first/continuation and four edges.
  # Existing helper synthetic historical identities are fixture construction;
  # all later acceptance executes unpatched validators in compiled subprocesses.
  from tests import test_intake_uncaptured_retirement as ancestor
  from tests.test_pre_entry_abandonment import PreEntryAbandonmentTests
  from desk import paper_migration_no_entry as migration,kraken_pacing_migration as kraken
  from desk import monitoring_handoff as handoff,runtime_continuation as cont
  from desk import paper_intake_uncaptured_retirement as intake_retirement
  h=ancestor.IntakeRetirementTests();helper=PreEntryAbandonmentTests();self.addCleanup(helper.doCleanups)
  with patch.object(ancestor,'IntakeRetirementTests',return_value=h):
   helper.test_preserved_pending_intake_four_charges_and_terminal_lineage()
  self.root=h.root
  with h.store.connect() as c:
   v=[x for x in migration.rows(c) if x['association']==migration.PREWIRE][0]
   c.execute('DELETE FROM pages WHERE hash=?',(digest({'kind':'paper_cycle_intent_v1','scan_id':v['scan_id']}),))
  old=v['successor_context'];previous=old['source_hash']
  from desk.job_persistence import JobPersistence
  from desk import allowance_policy
  JobPersistence(h.ctx['research_db']).upgrade_allowance(at=int(__import__('time').time()),provenance=allowance_policy.PROVENANCE)
  self.args={'runtime_policy':str(runtime.POLICY),'handoff_policy':str(handoff.POLICY),'continuation_policy':str(cont.POLICY),'extension_policy':str(extensions.POLICY),
   'research':h.ctx['research_db'],'evidence':h.ctx['evidence_db'],'ledger':h.ctx['ledger_db'],'pacing':h.ctx['pacing_db'],'cfg':h.cfg,'taker':old['taker'],
   'config':old['paths']['config']['path'],'discovery':old['paths']['discovery_db']['path'],'journal':old['journal'],
   'kraken_policy':str(kraken.POLICY),'intake_case':intake_retirement.CASE}
  for name in ('paper_preparation_retirement','paper_dispatch_preparation_retirement','paper_migration_no_entry','paper_intake_uncaptured_retirement'):
   module=__import__('desk.'+name,fromlist=[name]);self.args[name+'_policy']=str(module.POLICY)
  for key in ('performance_policy','successor_policy','recovery_policy','old_context','performance_context','recovery_pin','successor_context','case_file'):
   self.args[key]=str(h.root/('lifecycle-'+key+'.json'))
  Path(self.args['old_context']).write_text(canonical(old))
  for key,field in (('performance_policy','continuations'),('successor_policy','successors'),('recovery_policy','associations')):Path(self.args[key]).write_text(canonical({'version':1,field:[]}))
  perfroot,perfsource=self.binary('synthetic-performance-producer',predecessor=previous,historical=True)
  self.run_binary(perfroot,perfsource,INSTALL_PERFORMANCE)
  self.run_binary(perfroot,perfsource,CAPTURE_AND_RECOVER)
  self.args.update(json.loads(Path(self.args['case_file']).read_text()))
  nextroot,nextsource=self.binary('synthetic-empty-successor',predecessor=previous)
  self.run_binary(nextroot,nextsource,INSTALL_SUCCESSOR)
  self.run_binary(nextroot,nextsource,POST_LIFECYCLE)
