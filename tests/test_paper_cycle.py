"""SYNTHETIC_TEST_ONLY: persisted admissions and actual engine quote planning.

No live acceptance; injected protocol/quote bytes and clocks are explicit fixtures.
Pending checkpoint/producer seams are surfaced, never replaced by passing mocks.
"""
import base64
import copy
from dataclasses import replace
from decimal import Decimal
import fcntl
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from desk import engine, paper_cycle as cycle, quote_execution as qe, paper_read_sources as transport
from desk.job_persistence import BIRTH_ACQUISITION_V1
from desk.live_observation import ProviderObservation, ingest_mint
from desk.model import digest, canonical
from desk.providers import SOL
from desk.ledger import Ledger
from tests.test_paper_read_sources import Response
from tests.test_live_strategy_features import transaction, POOL as TRADE_POOL
from contextlib import ExitStack
from desk.paper_observation_collector import ObservationTarget, TargetObservation
from desk.paper_read_sources import PaperReadError
from tests import test_paper_observation_collector as collector_fixtures
from tests import test_quote_execution_v3_seam as execution_fixtures
from tests.helpers import config, T
from tests.test_graduation_witness import fixture as migration_fixture
from desk.programs import unbase58
from desk.security import base58


def dump(path):
    with sqlite3.connect(path) as connection:
        return list(connection.iterdump())


class PaperCycleTests(unittest.TestCase):
    def setUp(self):
        self.f=collector_fixtures.PaperObservationCollectorTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.target=self.f.target()
        raw,mint,pool=migration_fixture()
        self.assertEqual((mint,pool),(self.target.mint,self.target.pool))
        raw['blockTime']=self.f.at-600
        ix=raw['meta']['innerInstructions'][0]['instructions'][0]
        data=bytearray(unbase58(ix['data']));data[136:144]=raw['blockTime'].to_bytes(8,'little',signed=True);ix['data']=base58(data)
        response={'data':[raw],'paginationToken':None}
        response_hash=self.f.progress.store.save(response)
        manifest={'kind':'history_request_v1','method':'getTransactionsForAddress',
                  'params':[pool,{'transactionDetails':'full','commitment':'finalized','encoding':'jsonParsed'}],
                  'response_hash':response_hash}
        ref=self.f.progress.store.save(manifest)
        self.item=cycle.CycleTarget(self.target,'SYNTHETIC_TEST_ONLY',self.f.at-600,None,'25',
                                   history_as_of=self.f.at,graduation_refs=(ref,))
        self.cfg={**config(),'paper_signal_policy_version':3,'paper_quote_execution_version':1}
        self.path=Path(self.f.tmp.name)/'cycle.sqlite'
        cycle.initialize(self.path,self.cfg)
        outer=self
        class Source:
            rpc_source_id='fixture-protocol-rpc';quote_source_id='fixture-protocol-quote'
            def __init__(self,progress,scan):self.source=outer.f.sources[scan]
            def rpc(self,*args,**kwargs):return self.source.rpc(*args,**kwargs)
            def quote(self,*args,**kwargs):return self.source.quote(*args,**kwargs)
        self.source_factory=Source
        guard=patch('socket.socket',side_effect=AssertionError('synthetic sources only'))
        guard.start();self.addCleanup(guard.stop)

    def run_cycle(self,**kwargs):
        args=dict(candidates=(self.item,),dependency_blockers=(),source_factory=self.source_factory,
                  wall_clock=lambda:self.f.at,monotonic=lambda:self.f.tick)
        args.update(kwargs)
        return cycle.run_once(self.f.jobs.path,self.f.progress.store.path,self.path,self.cfg,**args)

    def actual_cycle(self, *, positions=(), candidates=None, usd_refs=(), monitoring=False):
        # Actual transport/collector/history/parser/builder/engine/checkpoint;
        # HTTPS open is replaced by explicitly synthetic original wire bytes.
        outer=self
        class Opener:
            def open(self,request,*,timeout):
                outer.http_calls.append(request)
                if request.method=='POST':
                    call=json.loads(request.data);method=call['method'];params=call['params']
                    if method=='getSlot':result=110
                    elif method=='getBlockTime':result=outer.f.at-1
                    elif method=='getTransactionsForAddress':
                        rows=[]
                        for i in range(40):
                            row=transaction('cycle-'+str(i),10+i,outer.f.at-1,quote=200,base=100,
                                            wallet=base58((i+1).to_bytes(32,'big')))
                            ix=row['transaction']['message']['instructions'][0]
                            ix['data']=base58(unbase58(ix['data']).replace(unbase58(TRADE_POOL),unbase58(outer.target.pool)))
                            rows.append(row)
                        result={'data':rows,'paginationToken':None}
                    else:
                        result=copy.deepcopy(outer.f.protocol.rpc(method,params))
                        if method=='getAccountInfo' and params[0]==outer.target.mint:
                            result={'context':{'slot':100},'value':copy.deepcopy(outer.f.protocol.rpc('getMultipleAccounts',[])['value'][6])}
                        # Valid pinned account layouts, explicit synthetic bank
                        # with100SOL/1M tokens and10M-token supply: defaults pass.
                        if method=='getMultipleAccounts':
                            for index,amount in ((0,10**12),(1,10**11)):
                                raw=bytearray(base64.b64decode(result['value'][index]['data'][0]));raw[64:72]=amount.to_bytes(8,'little')
                                result['value'][index]['data'][0]=base64.b64encode(raw).decode()
                            accounts=[result['value'][6]]
                        elif params[0]==outer.target.mint:accounts=[result['value']]
                        else:accounts=[]
                        for account in accounts:
                            raw=bytearray(base64.b64decode(account['data'][0]));raw[36:44]=(10**13).to_bytes(8,'little')
                            account['data'][0]=base64.b64encode(raw).decode()
                    wire=canonical({'jsonrpc':'2.0','id':transport.RPC_ID,'result':result}).encode()
                elif '/price/v3?' in request.full_url:
                    wire=canonical({SOL:{'usdPrice':100,'blockId':100,'decimals':9}}).encode()
                else:
                    from urllib.parse import parse_qs,urlsplit
                    q={k:v[0] for k,v in parse_qs(urlsplit(request.full_url).query).items()}
                    output=getattr(outer,'buy_output_raw',1_000_000) if q['inputMint']==SOL else (int(q['amount'])*outer.sell_output_per_unit_raw//1_000_000 if hasattr(outer,'sell_output_per_unit_raw') else outer.sell_output)
                    wire=canonical({'inputMint':q['inputMint'],'outputMint':q['outputMint'],'swapMode':'ExactIn',
                        'inAmount':q['amount'],'outAmount':str(output),'otherAmountThreshold':str(output*99//100),
                        'slippageBps':100,'routePlan':[{'percent':100,'swapInfo':{'inputMint':q['inputMint'],
                        'outputMint':q['outputMint'],'inAmount':q['amount'],'outAmount':str(output),'ammKey':outer.target.pool}}]}).encode()
                return Response(wire)
        with ExitStack() as stack:
            stack.enter_context(patch.object(transport.os.environ,'get',return_value='SYNTHETIC_TEST_ONLY'))
            stack.enter_context(patch.object(transport,'build_opener',return_value=Opener()))
            stack.enter_context(patch.object(cycle.time,'time',return_value=self.f.at))
            stack.enter_context(patch.object(cycle.time,'monotonic',return_value=1))
            return self.run_cycle(position_targets=positions,candidates=(self.item,) if candidates is None else candidates,
                                  source_factory=transport.PaperReadSources,usd_evidence_refs=usd_refs,monitoring=monitoring)

    def test_actual_entry_mark_full_exit_restart_originals_and_costs(self):
        self.http_calls=[];self.sell_output=10_000_000
        original_source=self.f.jobs.source(self.target.scan_id)
        entry=self.actual_cycle()
        self.assertEqual(entry['status'],'COMPLETE',entry)
        buy=next(x for x in entry['outcomes'] if x.get('side')=='buy')
        self.assertEqual(entry['attempted_requests'],9)
        self.assertEqual(buy['execution_status'],'EXECUTION_UNVERIFIED')
        state=cycle._state(self.path,self.cfg);position=state['positions'][self.target.mint]
        target=replace(self.target,amount_raw=qe.raw_quantity(position['qty'],6));item=replace(self.item,target=target)
        # Fresh retained originals are replayed, not refreshed/restamped/reset.
        refs=tuple(entry['usd_evidence_refs'])
        mark=self.actual_cycle(positions=(item,),candidates=(),usd_refs=refs)
        self.assertEqual(mark['status'],'COMPLETE',mark);self.assertEqual(mark['attempted_requests'],4)
        self.assertFalse(any(x['type']=='fill' for x in mark['outcomes']))
        self.sell_output=7_000_000
        exit_result=self.actual_cycle(positions=(item,),candidates=(),usd_refs=refs)
        self.assertEqual(exit_result['status'],'COMPLETE',exit_result)
        self.assertEqual(exit_result['budget'][self.target.scan_id]['used'],17)
        sell=next(x for x in exit_result['outcomes'] if x.get('side')=='sell')
        self.assertEqual(sell['reason'],'STOP')
        self.assertEqual(self.f.jobs.source(self.target.scan_id),original_source)
        final=cycle._state(self.path,self.cfg)
        self.assertEqual(final['positions'],{})
        self.assertEqual(Decimal(final['cash']),Decimal(self.cfg['initial_equity_sol'])-
                         Decimal(buy['amount_sol'])-Decimal(buy['fee_sol'])+Decimal(sell['proceeds_sol']))
        original=self.f.progress.store.load(entry['evidence_hash'])
        restart=self.actual_cycle(candidates=(),usd_refs=refs)
        self.assertEqual(restart['status'],'COMPLETE');self.assertEqual(restart['attempted_requests'],0)
        self.assertEqual(self.f.progress.admission(self.target.scan_id)['requests_used'],17)
        self.assertEqual(self.f.progress.store.load(entry['evidence_hash']),original)

    def test_actual_partial_then_full_exit_exact_sizes_within18(self):
        self.http_calls=[];self.sell_output=10_000_000
        entry=self.actual_cycle();self.assertEqual(entry['status'],'COMPLETE',entry)
        refs=tuple(entry['usd_evidence_refs']);state=cycle._state(self.path,self.cfg)
        raw=qe.raw_quantity(state['positions'][self.target.mint]['qty'],6)
        item=replace(self.item,target=replace(self.target,amount_raw=raw))
        self.sell_output=30_000_000
        partial=self.actual_cycle(positions=(item,),candidates=(),usd_refs=refs)
        self.assertEqual(partial['status'],'COMPLETE',partial)
        self.assertEqual(partial['attempted_requests'],5)
        sold=next(x for x in partial['outcomes'] if x.get('side')=='sell')
        self.assertEqual(sold['reason'],'TAKE_PROFIT');self.assertEqual(sold['quote_execution']['input_raw'],raw*3//10)
        remaining=cycle._state(self.path,self.cfg)['positions'][self.target.mint]
        self.assertEqual(qe.raw_quantity(remaining['qty'],6),raw-raw*3//10)
        self.assertEqual(remaining['exit_blocked'],'REMAINING_POSITION_VALUATION_REQUIRED')
        self.sell_output=6_000_000
        remaining_item=replace(item,target=replace(item.target,amount_raw=qe.raw_quantity(remaining['qty'],6)))
        full=self.actual_cycle(positions=(remaining_item,),candidates=(),usd_refs=refs)
        self.assertEqual(full['status'],'COMPLETE',full)
        self.assertEqual(full['attempted_requests'],4)
        self.assertEqual(full['budget'][self.target.scan_id],{'used':18,'ceiling':18})
        self.assertEqual(cycle._state(self.path,self.cfg)['positions'],{})
        self.assertEqual(self.actual_cycle(candidates=())['attempted_requests'],0)
        self.assertEqual(self.f.progress.admission(self.target.scan_id)['requests_used'],18)

    def test_actual_known_hazard_held_position_exits_without_entry_profile(self):
        self.http_calls=[];self.sell_output=10_000_000
        self.actual_cycle();state=cycle._state(self.path,self.cfg)
        item=replace(self.item,target=replace(self.target,amount_raw=qe.raw_quantity(state['positions'][self.target.mint]['qty'],6)),
                     known_hazards=('SYNTHETIC_KNOWN_HAZARD',),graduation_refs=())
        result=self.actual_cycle(positions=(item,),candidates=())
        self.assertEqual(result['status'],'COMPLETE',result)
        sale=next(x for x in result['outcomes'] if x.get('side')=='sell')
        self.assertEqual(sale['reason'],'DANGER')
        self.assertEqual(cycle._state(self.path,self.cfg)['positions'],{})
        self.assertEqual(result['attempted_requests'],4)

    def test_unprovisioned_legacy_allowance_still_exhausts_investigation18(self):
        self.http_calls=[];self.sell_output=10_000_000
        self.actual_cycle();p=cycle._state(self.path,self.cfg)['positions'][self.target.mint]
        item=replace(self.item,target=replace(self.target,amount_raw=qe.raw_quantity(p['qty'],6)))
        for used in (13,17):
            mark=self.actual_cycle(positions=(item,),candidates=())
            self.assertEqual(mark['attempted_requests'],4)
            self.assertEqual(mark['budget'][self.target.scan_id]['used'],used)
        stopped=self.actual_cycle(positions=(item,),candidates=())
        self.assertEqual(stopped['attempted_requests'],1)
        self.assertEqual(stopped['budget'][self.target.scan_id]['used'],18)
        self.assertIn('SHARED_REQUEST_BUDGET_EXHAUSTED',stopped['blockers'])
        self.assertFalse(any(x['type']=='fill' for x in stopped['outcomes']))
        count=len(self.http_calls)
        retry=self.actual_cycle(positions=(item,),candidates=())
        self.assertEqual(retry['status'],'RECOVERY_REQUIRED');self.assertEqual(len(self.http_calls),count)

    def test_held_exit_does_not_restamp_or_require_old_entry_usd_history(self):
        self.http_calls=[];self.sell_output=10_000_000
        entry=self.actual_cycle();p=cycle._state(self.path,self.cfg)['positions'][self.target.mint]
        item=replace(self.item,target=replace(self.target,amount_raw=qe.raw_quantity(p['qty'],6)),graduation_refs=())
        self.f.at+=31;self.sell_output=7_000_000
        result=self.actual_cycle(positions=(item,),candidates=(),usd_refs=tuple(entry['usd_evidence_refs']))
        self.assertEqual(result['status'],'COMPLETE',result)
        original=self.f.progress.store.load(entry['usd_evidence_refs'][0])
        self.assertEqual(original['observed_at'],self.f.at-31)
        self.assertEqual(result['usd_evidence_refs'],[])
        self.assertEqual(next(x for x in result['outcomes'] if x.get('side')=='sell')['reason'],'STOP')

    def test_candidate_usd_original_ref_substitution_is_controlled_and_no_fill(self):
        self.http_calls=[];self.sell_output=10_000_000
        result=self.actual_cycle(usd_refs=('0'*64,'1'*64,'2'*64))
        self.assertEqual(result['blockers'],['USD_ORIGINAL_BINDING_INVALID'])
        self.assertEqual(result['attempted_requests'],5)
        self.assertFalse(any(x['type']=='fill' for x in result['outcomes']))

    def monitoring_fixture(self):
        self.http_calls=[];self.sell_output=10_000_000
        entry=self.actual_cycle();self.assertEqual(entry['status'],'COMPLETE',entry)
        p=cycle._state(self.path,self.cfg)['positions'][self.target.mint]
        item=replace(self.item,target=replace(self.target,amount_raw=qe.raw_quantity(p['qty'],6)),graduation_refs=())
        allowance=cycle.MonitoringBudget(self.f.progress.store,self.path,self.cfg,clock=lambda:self.f.at)
        allowance.provision()  # Explicit synthetic coordinator operation, never run_once.
        return item,allowance

    def test_monitoring_actual_partial_full_restart_preserves_investigation_and_costs(self):
        item,allowance=self.monitoring_fixture()
        original=self.f.progress.admission(self.target.scan_id)
        self.sell_output=30_000_000
        partial=self.actual_cycle(positions=(item,),candidates=(),monitoring=True)
        self.assertEqual(partial['status'],'COMPLETE',partial)
        self.assertEqual(partial['monitoring_attempted_requests'],5)
        self.assertEqual(partial['investigation_attempted_requests'],0)
        sold=next(x for x in partial['outcomes'] if x.get('side')=='sell')
        self.assertEqual(sold['quote_execution']['input_raw'],item.target.amount_raw*3//10)
        # Three RPC reads, full-size valuation, ONE exact partial quote.
        self.assertEqual(len(self.http_calls),14)
        p=cycle._state(self.path,self.cfg)['positions'][self.target.mint]
        item=replace(item,target=replace(item.target,amount_raw=qe.raw_quantity(p['qty'],6)))
        self.sell_output=6_000_000
        complete=self.actual_cycle(positions=(item,),candidates=(),monitoring=True)
        self.assertEqual(complete['status'],'COMPLETE',complete)
        self.assertEqual(complete['monitoring_attempted_requests'],4)
        self.assertEqual(self.f.progress.admission(self.target.scan_id),original)
        self.assertEqual(allowance.snapshot()['total_used'],9)
        final=cycle._state(self.path,self.cfg)
        self.assertEqual(final['positions'],{})
        before=len(self.http_calls)
        restart=self.actual_cycle(candidates=(),monitoring=True)
        self.assertEqual(restart['status'],'COMPLETE',restart)
        self.assertEqual(restart['attempted_requests'],0);self.assertEqual(len(self.http_calls),before)
        self.assertEqual(allowance.snapshot()['total_used'],9)
        from desk.experiment_report import experiment_report
        # Actual read-only report must accept exit journals; costs are retained.
        actual=experiment_report(self.path,now=self.f.at)
        self.assertIsNotNone(actual)

    def test_monitoring_requires_explicit_provision_without_schema_creation(self):
        self.http_calls=[];self.sell_output=10_000_000
        self.actual_cycle();p=cycle._state(self.path,self.cfg)['positions'][self.target.mint]
        item=replace(self.item,target=replace(self.target,amount_raw=qe.raw_quantity(p['qty'],6)))
        before=len(self.http_calls);ledger=dump(self.path);evidence=dump(self.f.progress.store.path)
        result=self.actual_cycle(positions=(item,),candidates=(),monitoring=True)
        self.assertEqual(result['status'],'BLOCKED');self.assertEqual(len(self.http_calls),before)
        self.assertEqual(dump(self.path),ledger);self.assertEqual(dump(self.f.progress.store.path),evidence)

    def test_monitoring_can_refresh_held_after18_but_never_candidate(self):
        item,allowance=self.monitoring_fixture()
        while self.f.progress.admission(self.target.scan_id)['requests_used']<18:
            self.assertTrue(self.f.progress.reserve(self.target.scan_id))
        result=self.actual_cycle(positions=(item,),candidates=(),monitoring=True)
        self.assertEqual(result['status'],'COMPLETE',result)
        self.assertEqual(result['monitoring_attempted_requests'],4)
        self.assertEqual(result['budget'][self.target.scan_id]['used'],18)
        self.assertEqual(allowance.snapshot()['total_used'],4)

    def test_monitoring_cap_exhaustion_no_retry_or_investigation_fallback(self):
        item,allowance=self.monitoring_fixture()
        for _ in range(15):
            result=self.actual_cycle(positions=(item,),candidates=(),monitoring=True)
            self.assertEqual(result['status'],'COMPLETE',result)
        self.assertEqual(allowance.snapshot()['total_used'],60)
        before=len(self.http_calls);ledger=dump(self.path)
        result=self.actual_cycle(positions=(item,),candidates=(),monitoring=True)
        self.assertIn('MONITORING_REQUEST_BUDGET_EXHAUSTED',result['blockers'])
        self.assertEqual(result['attempted_requests'],0);self.assertEqual(len(self.http_calls),before)
        self.assertEqual(dump(self.path),ledger)
        self.assertEqual(self.f.progress.admission(self.target.scan_id)['requests_used'],9)

    def test_monitoring_charged_source_failure_latched_before_restart_or_candidates(self):
        item,allowance=self.monitoring_fixture()
        def failure(*args,**kwargs):raise OSError('SYNTHETIC_TEST_ONLY')
        before=self.f.progress.admission(self.target.scan_id)
        with patch.object(transport,'build_opener',side_effect=failure),patch.object(transport.os.environ,'get',return_value='SYNTHETIC_TEST_ONLY'),patch.object(transport.time,'time',return_value=self.f.at):
            result=self.run_cycle(position_targets=(item,),candidates=(),monitoring=True,source_factory=transport.PaperReadSources)
        self.assertEqual(result['status'],'BLOCKED');self.assertEqual(result['monitoring_attempted_requests'],1)
        self.assertEqual(allowance.snapshot()['total_used'],1)
        self.assertEqual(self.f.progress.admission(self.target.scan_id),before)
        restart=self.actual_cycle(positions=(item,),candidates=(),monitoring=True)
        self.assertIn(restart['status'],('RECOVERY_REQUIRED','BLOCKED'))
        self.assertEqual(restart['attempted_requests'],0)

    def test_monitoring_positions_first_candidate_still_uses_exhausted18(self):
        item,allowance=self.monitoring_fixture()
        while self.f.progress.admission(self.target.scan_id)['requests_used']<18:
            self.assertTrue(self.f.progress.reserve(self.target.scan_id))
        self.sell_output=7_000_000
        result=self.actual_cycle(positions=(item,),candidates=(self.item,),monitoring=True)
        self.assertEqual(result['monitoring_attempted_requests'],4)
        self.assertEqual(result['investigation_attempted_requests'],0)
        self.assertTrue(any(x.get('side')=='sell' for x in result['outcomes']))
        self.assertIn('SHARED_REQUEST_BUDGET_EXHAUSTED',result['blockers'])
        self.assertEqual(allowance.snapshot()['total_used'],4)
        self.assertEqual(result['budget'][self.target.scan_id]['used'],18)
        self.assertEqual(cycle._state(self.path,self.cfg)['positions'],{})

    def test_monitoring_source_without_charge_rejected_and_restart_latched(self):
        item,allowance=self.monitoring_fixture()
        outer=self
        class Uncharged:
            rpc_source_id=transport.PaperReadSources.rpc_source_id
            quote_source_id=transport.PaperReadSources.quote_source_id
            def __init__(self,*args,**kwargs):pass
            def rpc(self,*args,**kwargs):return outer.f.protocol.rpc(*args)
        result=self.run_cycle(position_targets=(item,),candidates=(),monitoring=True,source_factory=Uncharged)
        self.assertIn('MONITORING_CHARGE_OR_ADMISSION_MISMATCH',result['blockers'])
        self.assertFalse(any(x['type']=='fill' for x in result['outcomes']))
        self.assertEqual(allowance.snapshot()['total_used'],0)
        restart=self.actual_cycle(positions=(item,),candidates=(),monitoring=True)
        self.assertEqual(restart['status'],'RECOVERY_REQUIRED')
        self.assertEqual(restart['attempted_requests'],0)

    def test_monitoring_process_interruption_after_reservation_retains_whole_cycle_intent(self):
        item,allowance=self.monitoring_fixture()
        outer=self
        class Interrupted(transport.PaperReadSources):
            def rpc(self,method,params,*,timeout_seconds):
                self.monitoring_budget.reserve_read(self.progress,self.scan_id,method,params)
                raise SystemExit('SYNTHETIC_TEST_ONLY crash boundary')
        before=dump(self.path)
        with self.assertRaises(SystemExit):
            self.run_cycle(position_targets=(item,),candidates=(),monitoring=True,source_factory=Interrupted)
        self.assertEqual(dump(self.path),before)
        self.assertEqual(allowance.snapshot()['total_used'],1)
        self.assertIn('MONITORING_OUTCOME_PENDING',allowance.snapshot()['blockers'])
        restart=self.actual_cycle(positions=(item,),candidates=(),monitoring=True)
        self.assertEqual(restart['status'],'RECOVERY_REQUIRED')
        self.assertEqual(restart['attempted_requests'],0)
        self.assertEqual(self.f.progress.admission(self.target.scan_id)['requests_used'],9)

    def test_monitoring_unsellable_original_keeps_position_and_latches_restart(self):
        item,allowance=self.monitoring_fixture()
        original=cycle._state(self.path,self.cfg)['positions'][self.target.mint]
        self.sell_output=0  # SYNTHETIC unusable exact-size route, never a fabricated price.
        result=self.actual_cycle(positions=(item,),candidates=(),monitoring=True)
        self.assertIn('HELD_OBSERVATION_CONTENT_REJECTED',result['blockers'])
        self.assertFalse(any(x['type']=='fill' for x in result['outcomes']))
        self.assertEqual(cycle._state(self.path,self.cfg)['positions'][self.target.mint],original)
        self.assertEqual(allowance.snapshot()['total_used'],4)
        count=len(self.http_calls)
        restart=self.actual_cycle(positions=(item,),candidates=(),monitoring=True)
        self.assertEqual(restart['status'],'RECOVERY_REQUIRED')
        self.assertEqual(len(self.http_calls),count)

    def test_monitoring_quote_return_clock_boundary_keeps_original_source_time(self):
        item,allowance=self.monitoring_fixture()
        original=transport.PaperReadSources.quote;at=self.f.at
        def boundary(source,*args,**kwargs):
            result=original(source,*args,**kwargs)
            self.f.at+=1
            return result
        with patch.object(transport.PaperReadSources,'quote',boundary):
            result=self.actual_cycle(positions=(item,),candidates=(),monitoring=True)
        self.assertEqual(result['status'],'COMPLETE',result)
        self.assertEqual(allowance.snapshot()['total_used'],4)
        self.assertIn(self.target.mint,cycle._state(self.path,self.cfg)['positions'])
        with sqlite3.connect(self.path) as c:
            event=json.loads(c.execute("SELECT payload FROM events WHERE json_extract(payload,'$.kind')='quote_exit'").fetchone()[0])
        self.assertEqual(event['source_evidence']['quote_at'],at)
        self.assertEqual(event['ts'],at+1)

    def test_monitoring_late_local_completion_is_not_hidden_by_original_quote_time(self):
        item,allowance=self.monitoring_fixture()
        original=transport.PaperReadSources.quote
        def boundary(source,*args,**kwargs):
            result=original(source,*args,**kwargs)
            self.f.at+=11
            return result
        with patch.object(transport.PaperReadSources,'quote',boundary):
            result=self.actual_cycle(positions=(item,),candidates=(),monitoring=True)
        self.assertIn('SOURCE_RESPONSE_STALE',result['blockers'])
        self.assertFalse(any(x['type']=='fill' for x in result['outcomes']))
        self.assertEqual(allowance.snapshot()['total_used'],4)
        retry=self.actual_cycle(positions=(item,),candidates=(),monitoring=True)
        self.assertEqual(retry['status'],'RECOVERY_REQUIRED')
        self.assertEqual(retry['attempted_requests'],0)

    def test_explicit_pending_dependency_blocks_before_paths_and_sources(self):
        result=cycle.run_once('missing-research','missing-evidence','missing-ledger',self.cfg,
                              dependency_blockers=('CHECKPOINT_QUOTE_PROFILE3_PENDING',))
        self.assertEqual(result['status'],'BLOCKED');self.assertEqual(result['attempted_requests'],0)
        self.assertEqual(result['blockers'],['CHECKPOINT_QUOTE_PROFILE3_PENDING'])
        self.assertFalse(result['live_readiness'])

    def test_empty_cycle_and_restart_preserve_new_experiment(self):
        for _ in range(2):
            result=self.run_cycle(candidates=())
            self.assertEqual(result['status'],'COMPLETE');self.assertEqual(result['attempted_requests'],0)
        self.assertEqual(self.f.calls,[])
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM outcomes').fetchone()[0],0)

    def test_cfg_defaults_conflicts_and_legacy_field_never_opt_in(self):
        for changes in ({'paper_signal_policy_version':None},{'paper_signal_policy_version':True},
                        {'experimental_policy_version':3},{'experimental_policy_version':None},
                        {'paper_quote_execution_version':None},{'mode':'live'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                cycle.run_once('absent','absent','absent',self.cfg|changes,dependency_blockers=())
        self.assertEqual(self.f.calls,[])

    def test_initialization_exclusive_and_changed_cfg_cannot_adopt(self):
        with self.assertRaises(FileExistsError):cycle.initialize(self.path,self.cfg)
        before=self.path.read_bytes()
        changed=self.cfg|{'fixed_fee_sol':'.00006'}
        result=cycle.run_once(self.f.jobs.path,self.f.progress.store.path,self.path,changed,
                              dependency_blockers=(),candidates=(self.item,))
        self.assertEqual(result['blockers'],['SAVED_CONFIG_MISMATCH']);self.assertEqual(self.f.calls,[])
        self.assertEqual(self.path.read_bytes(),before)

    def test_invalid_existing_databases_no_mutation_or_admission_creation(self):
        paths=[Path(self.f.tmp.name)/'invalid-research',Path(self.f.tmp.name)/'invalid-evidence']
        for path in paths:
            with sqlite3.connect(path) as c:c.execute('CREATE TABLE original(value TEXT)')
        before=[x.read_bytes() for x in paths]
        result=cycle.run_once(*paths,self.path,self.cfg,candidates=(self.item,),dependency_blockers=())
        self.assertEqual(result['blockers'],['PERSISTED_ADMISSION_OR_SCHEMA_REQUIRED'])
        self.assertEqual([x.read_bytes() for x in paths],before)

    def test_deleted_admission_is_not_recreated_and_evidence_unchanged(self):
        with self.f.progress.store.connect() as c:
            c.execute('DELETE FROM ownership_admissions WHERE id=?',(self.target.scan_id,))
            before=list(c.iterdump())
        result=self.run_cycle()
        self.assertEqual(result['blockers'],['PERSISTED_ADMISSION_OR_SCHEMA_REQUIRED'])
        with self.f.progress.store.connect() as c:self.assertEqual(list(c.iterdump()),before)
        self.assertEqual(self.f.calls,[])

    def test_canonical_locks_block_without_reads(self):
        for path,reason in ((str(self.f.jobs.path)+'.jobs-worker.lock','RESEARCH_WORKER_BUSY'),
                            (str(self.f.progress.store.path)+'.ownership-invocation.lock','EVIDENCE_INVOCATION_BUSY'),
                            (str(self.path)+'.paper-cycle.lock','PAPER_CYCLE_BUSY')):
            with self.subTest(reason=reason),open(path,'a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertEqual(self.run_cycle()['blockers'],[reason])
        self.assertEqual(self.f.calls,[])

    def test_exhausted_lifetime_budget_never_reset_or_new_read(self):
        for _ in range(18):self.assertTrue(self.f.progress.reserve(self.target.scan_id))
        result=self.run_cycle()
        self.assertEqual(result['attempted_requests'],0)
        self.assertIn('SHARED_REQUEST_BUDGET_EXHAUSTED',result['blockers'])
        self.assertEqual(result['budget'][self.target.scan_id],{'used':18,'ceiling':18})
        self.assertEqual(self.f.calls,[])

    def test_failed_positionless_pass_stays_latched_across_restart(self):
        self.f.fail='getAccountInfo'
        first=self.run_cycle()
        self.assertEqual(first['attempted_requests'],1);self.assertIn('SOURCE_REQUEST_FAILED',first['blockers'])
        self.f.fail=None;self.f.calls.clear()
        second=self.run_cycle()
        self.assertEqual(second['status'],'RECOVERY_REQUIRED')
        self.assertEqual(second['blockers'],['OBSERVATION_RECOVERY_REQUIRED'])
        self.assertEqual(self.f.calls,[]);self.assertEqual(self.f.progress.admission(self.target.scan_id)['requests_used'],1)
        saved=self.f.progress.store.load(first['evidence_hash']);self.assertEqual(saved['blockers'],first['blockers'])

    def test_interrupted_charged_pass_no_fresh_restart(self):
        original=self.f.sources[self.target.scan_id]
        def interrupted(method,params,*,timeout_seconds):
            self.f.progress.reserve(self.target.scan_id)
            raise KeyboardInterrupt()
        self.f.sources[self.target.scan_id]=replace(original,rpc=interrupted)
        with self.assertRaises(KeyboardInterrupt):self.run_cycle()
        self.f.sources[self.target.scan_id]=original
        result=self.run_cycle()
        self.assertEqual(result['status'],'RECOVERY_REQUIRED');self.assertEqual(self.f.calls,[])
        self.assertEqual(self.f.progress.admission(self.target.scan_id)['requests_used'],1)

    def test_missing_checkpoint_no_reads_or_ledger_writes(self):
        with sqlite3.connect(self.path) as c:
            c.execute('DELETE FROM state');c.commit();before=list(c.iterdump())
        result=self.run_cycle()
        self.assertEqual(result['status'],'RECOVERY_REQUIRED');self.assertEqual(result['blockers'],['CHECKPOINT_MISSING'])
        with sqlite3.connect(self.path) as c:self.assertEqual(list(c.iterdump()),before)
        self.assertEqual(self.f.calls,[])

    def test_missing_migration_never_substitutes_supplied_numeric_date(self):
        item=replace(self.item,graduation_refs=())
        result=self.run_cycle(candidates=(item,))
        self.assertEqual(result['blockers'],['RETAINED_MIGRATION_WITNESS_REQUIRED'])
        self.assertEqual(result['diagnostics'][0]['graduation']['blockers'],['MIGRATION_WITNESS_ABSENT'])
        self.assertIsNone(result['diagnostics'][0]['graduation']['graduated_at'])
        self.assertEqual(self.f.calls,[])

    def test_graduation_original_time_conflict_is_blocked(self):
        result=self.run_cycle(candidates=(replace(self.item,graduated_at=self.f.at),))
        self.assertEqual(result['blockers'],['GRADUATION_TIMESTAMP_CONFLICT'])
        self.assertEqual(self.f.calls,[])

    def test_history_failure_enclosed_by_durable_intent_blocks_restart(self):
        class HistoryFailure:
            def __init__(self,progress,scan,key,*,timeout_seconds):pass
            def __call__(self,method,params):raise OSError('SYNTHETIC interrupted history response')
        result=self.run_cycle(history_source_factory=HistoryFailure)
        self.assertEqual(result['attempted_requests'],5)
        self.assertEqual(result['blockers'],['HISTORY_RECOVERY_REQUIRED'])
        self.assertEqual(result['budget'][self.target.scan_id]['used'],5)
        self.f.calls.clear()
        repeated=self.run_cycle()
        self.assertEqual(repeated['blockers'],['OBSERVATION_RECOVERY_REQUIRED'])
        self.assertEqual(self.f.calls,[])
        self.assertEqual(self.f.progress.admission(self.target.scan_id)['requests_used'],5)

    def test_unrelated_source_defect_remains_visible_and_restart_is_latched(self):
        def bad(progress,scan):raise RuntimeError('fixture programmer defect')
        with self.assertRaisesRegex(RuntimeError,'programmer defect'):self.run_cycle(source_factory=bad)
        result=self.run_cycle()
        self.assertEqual(result['status'],'RECOVERY_REQUIRED');self.assertEqual(self.f.calls,[])


class QuoteOnlyCycleTests(unittest.TestCase):
    def setUp(self):
        self.fx=execution_fixtures.QuoteV3SeamTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.f=self.fx.fixture;self.cfg=self.fx.cfg
        self.admissions=collector_fixtures.PaperObservationCollectorTests();self.admissions.setUp();self.addCleanup(self.admissions.doCleanups)
        jobs=self.admissions.jobs;progress=self.admissions.progress
        scan=jobs.admit(self.f.mint,kind=BIRTH_ACQUISITION_V1,evidence_db=progress.store.path)
        d=jobs.descriptor(scan)
        progress.admit(scan,{'kind':'ownership_admission_v1','scan_id':scan,'mint':self.f.mint,'created':d['admitted_at']})
        self.target=ObservationTarget(scan,self.f.mint,self.f.pool,self.f.wallet,self.f.buy.input_raw)
        self.budget=cycle._Budget(progress,lambda:T,lambda:1)
        self.calls=[]
        outer=self
        class Source:
            quote_source_id='fixture:synthetic-quote'
            def quote(self,input_mint,output_mint,amount,taker,*,timeout_seconds):
                outer.assertTrue(progress.reserve(scan));outer.calls.append((input_mint,output_mint,amount,taker))
                direction='buy' if input_mint!=outer.f.mint else 'sell'
                q=outer.f.quote(direction,amount,10_000_000 if direction=='sell' else 1_000_000)
                payload=json.loads(q.source.original_json)
                progress.store.save({'kind':'synthetic_cycle_quote_original_v1','payload':payload})
                return payload
        self.source=Source()
        self.token=self.token_from(self.f.buy)
        self.collected=TargetObservation(self.target,'buy',self.token,None,self.f.buy,None,())

    def token_from(self,quote):
        return ingest_mint(lambda:ProviderObservation(quote.mint_source.source_id,quote.mint_source.observed_at,
            json.loads(quote.mint_source.original_json)),mint=self.f.mint,now=T)

    def test_entry_reuses_existing_buy_and_only_reads_exact_reverse_once(self):
        state=engine.initial_state(self.cfg);before=copy.deepcopy(state)
        event,quotes,planned=cycle.fulfill_quotes(state,self.cfg,self.fx.market,self.collected,self.source,self.budget)
        self.assertEqual(len(self.calls),1);self.assertEqual(self.calls[0][2],qe.output_raw(self.f.buy,self.cfg))
        self.assertEqual([q.direction for q in quotes],['buy','sell'])
        self.assertEqual(self.budget.attempted,1);self.assertEqual(state,before)
        self.assertEqual(planned['quote_demands'],[])
        committed,out=qe.bind_transition(event,quotes)(state,event,self.cfg)
        self.assertEqual(next(x for x in out if x['type']=='fill')['execution_status'],'EXECUTION_UNVERIFIED')
        self.assertIn(self.f.mint,committed['positions'])

    def test_partial_exit_demands_exact_raw_floor_without_four_read_recollection(self):
        e=self.fx.market()
        state,_=qe.bind_transition(e,(self.f.buy,self.f.exit))(engine.initial_state(self.cfg),e,self.cfg)
        mark=self.f.quote('sell',self.f.raw,30_000_000)
        collected=replace(self.collected,target=replace(self.target,amount_raw=self.f.raw),direction='sell',quote=mark)
        event,quotes,planned=cycle.fulfill_quotes(state,self.cfg,self.fx.market,collected,self.source,self.budget)
        self.assertEqual(len(self.calls),1);self.assertEqual(self.calls[0][2],self.f.raw*3//10)
        self.assertEqual([q.input_raw for q in quotes],[self.f.raw,self.f.raw*3//10])
        self.assertEqual(planned['quote_demands'],[])
        new,out=qe.bind_transition(event,quotes)(state,event,self.cfg)
        sold=next(x for x in out if x.get('side')=='sell')
        self.assertEqual(sold['reason'],'TAKE_PROFIT')
        self.assertEqual(qe.raw_quantity(new['positions'][self.f.mint]['qty'],6),self.f.raw-self.f.raw*3//10)
        self.assertEqual(new['positions'][self.f.mint]['exit_blocked'],'REMAINING_POSITION_VALUATION_REQUIRED')

    def test_missing_reverse_budget_refuses_without_reset_or_fill(self):
        for _ in range(18):self.admissions.progress.reserve(self.target.scan_id)
        state=engine.initial_state(self.cfg);before=copy.deepcopy(state)
        with self.assertRaisesRegex(cycle.CycleBlocked,'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED'):
            cycle.fulfill_quotes(state,self.cfg,self.fx.market,self.collected,self.source,self.budget)
        self.assertEqual(state,before);self.assertEqual(self.calls,[])
        self.assertEqual(self.admissions.progress.admission(self.target.scan_id)['requests_used'],18)

    def test_source_without_one_durable_charge_cannot_supply_quote(self):
        def uncharged(*args,**kwargs):return json.loads(self.f.exit.source.original_json)
        with patch.object(self.source,'quote',uncharged),self.assertRaisesRegex(cycle.CycleBlocked,'SHARED_BUDGET_CHARGE_OR_IDENTITY_MISMATCH'):
            cycle.fulfill_quotes(engine.initial_state(self.cfg),self.cfg,self.fx.market,self.collected,self.source,self.budget)

    def test_event_builder_blocker_never_quotes_or_substitutes_event(self):
        with self.assertRaisesRegex(cycle.CycleBlocked,'MARKET_PRODUCER_BLOCKED'):
            cycle.fulfill_quotes(engine.initial_state(self.cfg),self.cfg,lambda:None,self.collected,self.source,self.budget)
        self.assertEqual(self.calls,[])

    def test_oversized_delivered_event_refused_before_quote_or_ledger_work(self):
        event=self.fx.market();event['synthetic_original_padding']='x'*cycle.MAX_EVENT_BYTES
        before=list(self.f.ledger.db.iterdump())
        with self.assertRaisesRegex(cycle.CycleBlocked,'EVENT_READER_SIZE_LIMIT'):
            cycle.fulfill_quotes(engine.initial_state(self.cfg),self.cfg,lambda:event,self.collected,self.source,self.budget)
        self.assertEqual(self.calls,[]);self.assertEqual(list(self.f.ledger.db.iterdump()),before)

    def test_global_deadline_refuses_before_source(self):
        self.budget.monotonic=lambda:11
        with self.assertRaisesRegex(cycle.CycleBlocked,'CYCLE_DEADLINE_UNAVAILABLE'):
            cycle.fulfill_quotes(engine.initial_state(self.cfg),self.cfg,self.fx.market,self.collected,self.source,self.budget)
        self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
