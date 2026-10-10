"""Standalone synthetic native-clock three-page BUY/SELL probe; no real HTTP."""
import inspect,textwrap,time,json,unittest,contextlib
from unittest.mock import patch
from tests import test_history_first_paper_entry as fixture
from desk import paper_cycle as cycle, quote_execution as qe, kraken_pacing_migration as migration
from desk import paper_migration_no_entry as recovery,paper_terminal_reconciliation as terminal
from desk import paper_dispatch_preparation_retirement as parent
from desk.model import canonical
records=[];cycles=[];phase='setup'
def measured(name,fn):
 def run(*a,**k):
  at=time.perf_counter();p=phase;error=None
  try:return fn(*a,**k)
  except BaseException as e:error=repr(e);raise
  finally:records.append(dict(name=name,phase=p,start=at,elapsed=time.perf_counter()-at,error=error))
 return run
original=cycle.run_once
def run_cycle(*a,**k):
 global phase
 phase='held' if k.get('position_targets') else 'buy';at=time.perf_counter()
 result=original(*a,**k)
 cycles.append(dict(phase=phase,elapsed=time.perf_counter()-at,status=result['status'],attempted=result['attempted_requests'],monitoring=result.get('monitoring_attempted_requests'),outcomes=[{q:v for q,v in x.items() if q in ('side','type','reason')} for x in result['outcomes']],diagnostics=result.get('diagnostics'),budget=result.get('budget')))
 return result
src=textwrap.dedent(inspect.getsource(fixture.HistoryFirstTests.test_normal_clock_mock_http_history_then_usd_late_buy))
src=src.replace("f=self.f;f.f.at=int(time.time());f.http_calls=[];f.sell_output=10_000_000", """f=self.f;f.f.at=int(time.time());f.http_calls=[];f.buy_output_raw=100_000_000;f.sell_output_per_unit_raw=99_000
    f.cfg=f.cfg|{'paper_usd_valuation_version':1}
    f.path=self.root/'native-kraken.sqlite';cycle.initialize(f.path,f.cfg)
    self.config.write_text(json.dumps(f.cfg))
    policy=self.root/'kraken-policy.json'
    policy.write_text(canonical({'version':1,'pins':[]}))
    pin=migration.review_plan(self.pace);policy.write_text(canonical({'version':1,'pins':[pin]}))
    with patch.object(migration,'POLICY',policy):migration.migrate(self.pace)
    policy_patch=patch.object(migration,'POLICY',policy);policy_patch.start();self.addCleanup(policy_patch.stop)""")
src=src.replace("if '/price/v3?' in request.full_url:","""if request.method=='POST' and json.loads(request.data)['method']=='getTransactionsForAddress':
                call=json.loads(request.data);cursor=call['params'][1].get('paginationToken');start=int(cursor) if cursor else 0
                # NEW synthetic, complete three-page query, never relabel retained data.
                decoded=json.loads(delegate.open(request,timeout=timeout).body)
                templates=decoded['result']['data'];rows=[]
                for i in range(start,start+50):
                    row=copy.deepcopy(templates[i%len(templates)]);row['transaction']['signatures']=['verbose-native-'+str(i)];row['slot']=10+i
                    row['meta']['logMessages']=['Program log: SYNTHETIC_VERBOSE '+('x'*19000)]
                    rows.append(row)
                return Response(canonical({'jsonrpc':'2.0','id':transport.RPC_ID,'result':{'data':rows,'paginationToken':str(start+50) if start+50<150 else None}}).encode())
            if '/price/v3?' in request.full_url:""")
src=src.replace("if '/price/v3?' in request.full_url:","if 'api.kraken.com/0/public/Trades?' in request.full_url:\n                return Response(canonical({'error':[],'result':{'SOLUSD':[['100','1',time.time()-0.1,'b','m','',1]],'last':'1'}}).encode())\n            if '/price/v3?' in request.full_url:\n                raise AssertionError('PriceV3 forbidden')\n            if False:")
src=src.replace("result=self.invoke(live=True,systemd_credentials=True)","""result=self.invoke(live=True,systemd_credentials=True)
        self.assertTrue(any(r.get('side')=='buy' for r in result['outcomes']),result)
        state=cycle._state(f.path,f.cfg);p=state['positions'][f.target.mint]
        item=replace(f.item,provenance='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE',target=replace(f.target,amount_raw=qe.raw_quantity(p['qty'],p['quote_execution']['mint_decimals'])),graduation_refs=(),known_hazards=('SYNTHETIC_KNOWN_HAZARD',))
        allowance=cycle.MonitoringBudget(f.f.progress.store,f.path,f.cfg);allowance.provision()
        exited=cycle.run_once(f.f.jobs.path,f.f.progress.store.path,f.path,f.cfg,position_targets=(item,),candidates=(),dependency_blockers=(),monitoring=True)
        self.assertEqual(exited['status'],'COMPLETE',exited)
        self.assertTrue(any(r.get('side')=='sell' for r in exited['outcomes']),exited)
        self.assertEqual(cycle._state(f.path,f.cfg)['positions'],{})
        self.assertEqual(allowance.snapshot()['total_used'],5)""")
src=src.replace("['requests_used'],9)","['requests_used'],9)").replace("result['usd_evidence_refs'].__len__(),3)","result['usd_evidence_refs'].__len__(),1)")
ns={**vars(fixture),'cycle':cycle,'qe':qe,'migration':migration};exec(src,ns)
test=fixture.HistoryFirstTests();at=time.perf_counter()
try:
 profile=patch.object(fixture.vertical.VerticalProfileTests,'profile',2,create=True);profile.start()
 test.addCleanup(profile.stop)
 test.setUp()
 with contextlib.ExitStack() as stack:
  for mod,name in ((terminal,'gate'),(recovery,'gate'),(recovery,'proof'),(parent,'proof'),(cycle,'_history'),(cycle,'_usd'),(cycle,'fulfill_quotes'),(cycle,'_collect_held')):
   stack.enter_context(patch.object(mod,name,measured(mod.__name__+'.'+name,getattr(mod,name))))
  stack.enter_context(patch.object(cycle,'run_once',run_cycle))
  ns['test_normal_clock_mock_http_history_then_usd_late_buy'](test)
 print(json.dumps(dict(status='PASS',total=time.perf_counter()-at,cycles=cycles,timings=records)))
except BaseException as e:
 print(json.dumps(dict(status='FAIL',error=repr(e),total=time.perf_counter()-at,cycles=cycles,timings=records)));raise
finally:test.doCleanups()
