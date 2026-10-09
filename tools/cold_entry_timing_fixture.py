"""SYNTHETIC_TEST_ONLY, real durable pacing/budget/source/engine; zero network."""
import sys,inspect,textwrap,json,sqlite3,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from contextlib import ExitStack
from unittest.mock import patch
from tests import test_token2022_paper as fixtures
from tests.test_paper_read_sources import Response
from desk import paper_cycle as cycle, paper_read_sources as transport, provider_pacing as pacing
from desk.providers import SOL
from desk.model import canonical,digest

class Clock:
    def __init__(self,wall):self.wall=wall;self.tick=0.
    def time(self):return self.wall
    def monotonic(self):return self.tick
    def sleep(self,d):self.wall+=d;self.tick+=d

def run(kind,fraction):
    test=fixtures.VerticalProfileTests();test.setUp();f=test.f
    clock=Clock(f.f.at+fraction);root=Path(f.f.tmp.name)
    pace=root/'pacing.sqlite';pacing.initialize(pace)
    code=textwrap.dedent(inspect.getsource(f.actual_cycle))
    start=code.index('    class Opener:');end=code.index('    with ExitStack()')
    ns={'outer':f,'json':json,'copy':copy,'base64':__import__('base64'),
        'canonical':canonical,'transport':transport,'transaction':__import__('tests.test_live_strategy_features',fromlist=['transaction']).transaction,
        'base58':__import__('desk.security',fromlist=['base58']).base58,
        'unbase58':__import__('desk.programs',fromlist=['unbase58']).unbase58,
        'TRADE_POOL':__import__('tests.test_live_strategy_features',fromlist=['POOL']).POOL,'SOL':SOL,'Response':Response}
    exec(textwrap.dedent(code[start:end]),ns)
    delegate=ns['Opener']();calls=[]
    class Opener:
        def open(self,request,*,timeout):
            assert timeout>0
            clock.sleep(.05)
            calls.append({'at':round(clock.time(),6),'method':json.loads(request.data)['method'] if request.method=='POST' else ('price' if '/price/v3?' in request.full_url else 'quote'),
                          'used':f.f.progress.admission(f.target.scan_id)['requests_used']})
            response=delegate.open(request,timeout=timeout)
            # Explicit synthetic node model: exact per-slot times fixed upfront.
            # Source records are saved once at real fixture acquisition time.
            block=100+int(clock.monotonic())
            if request.method=='POST':
                method=json.loads(request.data)['method']
                if method=='getSlot':value=100+int(clock.monotonic())
                elif method=='getBlockTime':value=block_times[json.loads(request.data)['params'][0]]
                else:return response
                return Response(canonical({'jsonrpc':'2.0','id':transport.RPC_ID,'result':value}).encode())
            if '/price/v3?' in request.full_url:
                return Response(canonical({SOL:{'usdPrice':100,'blockId':block,'decimals':9}}).encode())
            return response
    block_times={block:f.f.at+(block-100)-1 for block in range(100,200)}
    initial=f.f.progress.admission(f.target.scan_id)
    source=transport.PaperReadSources(f.f.progress,f.target.scan_id)
    def pacer(priority='investigation'):
        return pacing.Pacer(pace,priority=priority,clock=clock.time,monotonic=clock.monotonic,sleep=clock.sleep)
    with ExitStack() as stack:
        stack.enter_context(patch.object(transport,'build_opener',return_value=Opener()))
        stack.enter_context(patch.object(transport.os.environ,'get',return_value='SYNTHETIC_TEST_ONLY'))
        stack.enter_context(patch.object(pacing,'configured',side_effect=pacer))
        stack.enter_context(patch.object(cycle.time,'time',side_effect=clock.time))
        stack.enter_context(patch.object(cycle.time,'monotonic',side_effect=clock.monotonic))
        prior=[]
        if kind=='prehistory-spent':
            from dataclasses import replace
            for _ in range(4):
                _,key=source.rpc_with_evidence('getSlot',[{'commitment':'finalized'}],timeout_seconds=10)
                prior.append((key,f.f.progress.store.load(key)))
            # A newly captured query time, not a restamp of prior evidence.
            f.item=replace(f.item,history_as_of=int(clock.time()))
            f.f.at=int(clock.time())  # synthetic trades for this NEW requested window.
        phase=cycle._Budget(f.f.progress,clock.time,clock.monotonic)
        if kind=='naive':
            usd=cycle._usd(f.f.progress,source,f.target.scan_id,phase);refs=usd[-1]
        elif kind=='history-usd':
            from desk.paper_history_source import PaperHistorySource
            cycle._history(f.f.progress,f.item,phase,PaperHistorySource)
            phase=cycle._Budget(f.f.progress,clock.time,clock.monotonic)
            refs=cycle._usd(f.f.progress,source,f.target.scan_id,phase)[-1]
        elif kind in ('prehistory','prehistory-cli','prehistory-spent'):
            from desk.paper_history_source import PaperHistorySource
            cycle._history(f.f.progress,f.item,phase,PaperHistorySource)
            # Actual wait; no pacing state/counters are rewritten or reset.
            clock.sleep(2.0)
            refs=()
        else:refs=()
        usd_elapsed=clock.monotonic();used_usd=f.f.progress.admission(f.target.scan_id)['requests_used']
        originals={key:f.f.progress.store.load(key) for key in refs}
        from dataclasses import replace
        item=replace(f.item,history_as_of=None) if kind=='prehistory-cli' else f.item
        result=cycle.run_once(f.f.jobs.path,f.f.progress.store.path,f.path,f.cfg,candidates=(item,),
                             source_factory=transport.PaperReadSources,usd_evidence_refs=refs,dependency_blockers=(),
                             wall_clock=clock.time,monotonic=clock.monotonic)
        assert all(f.f.progress.store.load(k)==v and digest(v)==k for k,v in originals.items())
        assert all(f.f.progress.store.load(k)==v and digest(v)==k for k,v in prior)
        final=f.f.progress.admission(f.target.scan_id)
        assert len(calls)<=final['requests_used']<=18
        if result['status']=='COMPLETE':assert final['requests_used']==len(calls)
        assert {k:v for k,v in initial.items() if k!='requests_used'}=={k:v for k,v in final.items() if k!='requests_used'}
        output={'kind':kind,'fraction':fraction,'usd_elapsed':round(usd_elapsed,6),'entry_elapsed':round(clock.monotonic()-usd_elapsed,6),
                'usd_used':used_usd,'total_used':final['requests_used'],'status':result['status'],'blockers':result['blockers'],
                'outcomes':[o.get('side') for o in result['outcomes'] if o.get('type')=='fill'],
                'diagnostics':result['diagnostics'],'calls':calls,
                'refs_observed_at':[originals[k]['observed_at'] for k in refs],
                'result_usd_observed_at':[f.f.progress.store.load(k)['observed_at'] for k in result['usd_evidence_refs']],
                'result_usd_methods':[f.f.progress.store.load(k)['method'] for k in result['usd_evidence_refs']],
                'pacing':list(sqlite3.connect(pace).execute('SELECT provider,next_at,high_water FROM state'))}
        expected_buy = kind in ('prehistory', 'prehistory-spent')
        assert ('buy' in [str(x).lower() for x in output['outcomes']]) == expected_buy, output['kind']
        print(json.dumps(output,default=str,sort_keys=True))
    test.doCleanups()

for kind,fraction in [('cold',.9),('naive',.0),('naive',.9),('prehistory',.9),('prehistory-cli',.9),('prehistory-spent',.9),('history-usd',.9)]:run(kind,fraction)
