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


from desk import kraken_usd_observation as usd, kraken_pacing_migration as migration, provider_pacing as pace
from tests import test_paper_cycle as fixtures

# Synthetic adapter reuses legacy fixture account/quote/history shapes; only the
# new Kraken public recent-trade response differs. No provider requests.
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
            elif 'api.kraken.com/0/public/Trades?' in request.full_url:
                wire=('{"error":[],"result":{"SOLUSD":[["100.00000","1.00000",'+str(outer.f.at-1)+'.25,"s","l","",123]],"last":"123000000000"}}').encode()
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


class KrakenLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.h=fixtures.PaperCycleTests();self.addCleanup(self.h.doCleanups);self.h.setUp()
        self.old_path=self.h.path;self.old_dump=dump(self.old_path)
        self.h.cfg=self.h.cfg|{'paper_usd_valuation_version':1}
        self.h.path=Path(self.h.f.tmp.name)/'kraken-experiment.sqlite';cycle.initialize(self.h.path,self.h.cfg)
        self.h.http_calls=[];self.h.sell_output=10_000_000
        self.path=Path(self.h.f.tmp.name).resolve()/'kraken-pacing.sqlite';pace.initialize(self.path)
        self.policy=self.path.parent/'migration.json'
        p=patch.object(migration,'POLICY',self.policy);p.start();self.addCleanup(p.stop)
        self.policy.write_text(canonical({'version':1,'pins':[]}))
        pin=migration.review_plan(self.path);self.policy.write_text(canonical({'version':1,'pins':[pin]}));migration.migrate(self.path)
        self.clock=[float(self.h.f.at)]
        def configured(**kw):
            return pace.Pacer(self.path,clock=lambda:self.clock[0],monotonic=lambda:self.clock[0],sleep=lambda n:self.clock.__setitem__(0,self.clock[0]+n),**kw)
        p=patch.object(pace,'configured',side_effect=configured);p.start();self.addCleanup(p.stop)
    def test_entry_held_exit_restart_accounting_and_originals(self):
        result=actual_cycle(self.h);self.assertEqual(result['status'],'COMPLETE',result)
        self.assertEqual(result['attempted_requests'],7,result)
        self.assertEqual(len(result['usd_evidence_refs']),1)
        with sqlite3.connect(self.h.path) as ledger:
            state=cycle._state(self.h.path,self.h.cfg);self.assertEqual(len(state['positions']),1)
            event=json.loads(ledger.execute("SELECT payload FROM events WHERE event_id=?",(result['events'][0],)).fetchone()[0])
            self.assertEqual(event['paper_usd_valuation']['source'],usd.SOURCE)
            self.assertIsNone(event['paper_usd_valuation']['solana_slot_witness'])
            self.assertNotIn('trusted_slot_bounds',event['paper_source_evidence']['usd'])
        from desk.monitoring_budget import MonitoringBudget
        monitoring=MonitoringBudget(self.h.f.progress.store,self.h.path,self.h.cfg,clock=lambda:self.h.f.at);monitoring.provision()
        position=cycle._state(self.h.path,self.h.cfg)['positions'][self.h.target.mint]
        item=replace(self.h.item,target=replace(self.h.target,amount_raw=qe.raw_quantity(position['qty'],position['quote_execution']['mint_decimals'])))
        result=actual_cycle(self.h,positions=(item,),candidates=(),monitoring=True)
        self.assertEqual(result['status'],'COMPLETE',result)
        self.assertEqual(result['monitoring_attempted_requests'],5,result)
        self.assertEqual(len(result['usd_evidence_refs']),1)
        self.assertEqual(monitoring.snapshot()['total_used'],5)
        self.assertEqual(dump(self.old_path),self.old_dump)
        # Host restart does not expire historical evidence: replay uses original
        # event decision time, while a fresh cycle must acquire a fresh price.
        self.h.f.at+=100
        self.assertEqual(actual_cycle(self.h,candidates=())['attempted_requests'],0)
        self.assertEqual(dump(self.old_path),self.old_dump)
    def test_entry_saved_valuation_tampering_and_missing_version_refuse_read_and_duplicate(self):
        result=actual_cycle(self.h);self.assertEqual(result['status'],'COMPLETE',result)
        with sqlite3.connect(self.h.path) as c:
            event=json.loads(c.execute('SELECT payload FROM events WHERE event_id=?',(result['events'][0],)).fetchone()[0])
            event['paper_usd_valuation']['trade_at']='0'
            c.execute('UPDATE events SET payload=?,payload_hash=? WHERE event_id=?',(canonical(event),digest(event),event['event_id']))
        before=dump(self.h.path)
        from desk.paper_checkpoint import RecoveryRequired
        with self.assertRaises(RecoveryRequired):cycle._state(self.h.path,self.h.cfg)
        self.assertEqual(dump(self.h.path),before)

    def test_partial_full_exit_preserves_investigation_basis_and_monitoring_charges(self):
        result=actual_cycle(self.h);self.assertEqual(result['status'],'COMPLETE',result)
        from desk.monitoring_budget import MonitoringBudget
        allowance=MonitoringBudget(self.h.f.progress.store,self.h.path,self.h.cfg,clock=lambda:self.h.f.at);allowance.provision()
        original=self.h.f.progress.admission(self.h.target.scan_id)
        position=cycle._state(self.h.path,self.h.cfg)['positions'][self.h.target.mint]
        item=replace(self.h.item,target=replace(self.h.target,amount_raw=qe.raw_quantity(position['qty'],6)))
        self.h.sell_output=30_000_000
        partial=actual_cycle(self.h,positions=(item,),candidates=(),monitoring=True)
        self.assertEqual(partial['status'],'COMPLETE',partial);self.assertEqual(partial['monitoring_attempted_requests'],6)
        sold=next(x for x in partial['outcomes'] if x.get('side')=='sell')
        self.assertEqual(sold['quote_execution']['input_raw'],item.target.amount_raw*3//10)
        remaining=cycle._state(self.h.path,self.h.cfg)['positions'][self.h.target.mint]
        self.assertGreater(Decimal(remaining['cost_left']),0);self.assertLess(Decimal(remaining['cost_left']),Decimal(remaining['initial_cost']))
        item=replace(item,target=replace(item.target,amount_raw=qe.raw_quantity(remaining['qty'],6)))
        self.h.sell_output=6_000_000
        full=actual_cycle(self.h,positions=(item,),candidates=(),monitoring=True)
        self.assertEqual(full['status'],'COMPLETE',full);self.assertEqual(full['monitoring_attempted_requests'],5)
        self.assertEqual(cycle._state(self.h.path,self.h.cfg)['positions'],{})
        self.assertEqual(allowance.snapshot()['total_used'],11);self.assertEqual(self.h.f.progress.admission(self.h.target.scan_id),original)
        self.assertEqual(dump(self.old_path),self.old_dump)
