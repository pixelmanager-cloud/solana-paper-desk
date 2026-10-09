"""SYNTHETIC_TEST_ONLY: successor profile2 lifecycle, native clocks/pacing.

Original fixture records stay immutable. Injected HTTPS is synthetic acquisition,
not authenticated chain evidence, executable fill proof or profitability evidence.
"""
import base64
import copy
from dataclasses import replace
from decimal import Decimal
from http.client import HTTPResponse
import inspect
import io
import json
from pathlib import Path
import sqlite3
import textwrap
import time
import unittest
from unittest.mock import patch

from desk import paper_cycle, paper_read_sources as transport, quote_execution
from desk.model import canonical, digest
from desk.monitoring_budget import MonitoringBudget
from desk.programs import unbase58
from desk.providers import SOL
from desk.security import base58
from tests import test_history_first_paper_entry as operator_fixture
from tests import test_paper_cycle as wire_fixture
from tests.test_paper_read_sources import Response
from tests.test_runtime_compatibility import dump


from contextlib import ExitStack
from desk import monitoring_handoff, monitoring_successor, runtime_compatibility as runtime
from desk.model import BOOST_VOLUME_FEATURE, BOOST_SIGNAL_PROFILE
from desk.monitoring_budget import MonitoringBlocked
from tests import test_history_runtime_composition as prior
from tests.test_history_runtime_composition import distinct_protocol, migration_for, boosted_response
from tests import test_runtime_extensions


class Profile2RuntimeCompositionTests(unittest.TestCase):
    _assert_retired_originals = prior.HistoryRuntimeCompositionTests._assert_retired_originals
    _resolve_terminal = prior.HistoryRuntimeCompositionTests._resolve_terminal

    def test_old_certificates_profile2_successor_buy_partial_full_restart(self):
        event_quote_bytes = bytes(32)
        u = test_runtime_extensions.RuntimeExtensionTests()
        u.setUp()
        self.addCleanup(u.doCleanups)
        op = operator_fixture.HistoryFirstTests()
        op.setUp()
        self.addCleanup(op.doCleanups)
        h, f = u.f, op.f
        self.assertEqual(h.cfg['paper_token_profile_version'], 1)
        old_store = f.f.progress.store
        now = int(time.time())
        manifest = old_store.load(f.item.graduation_refs[0])
        response = copy.deepcopy(old_store.load(manifest['response_hash']))
        from tests.test_graduation_witness import fixture
        raw, mint, pool = fixture('migrate_v2')
        self.assertEqual((mint, pool), (h.target.mint, h.target.pool))
        response['data'] = [raw]
        # Six already charged fixture reads remain part of this admission.
        for _ in range(6):
            self.assertTrue(h.f.context.progress.reserve(h.target.scan_id))
        raw = response['data'][0]
        raw['blockTime'] = now - 600
        ix = raw['meta']['innerInstructions'][0]['instructions'][0]
        data = bytearray(unbase58(ix['data']))
        data[136:144] = raw['blockTime'].to_bytes(8, 'little', signed=True)
        data[-32:] = event_quote_bytes
        ix['data'] = base58(data)
        graduated_at = raw['blockTime']
        manifest = {**manifest, 'response_hash': h.store.save(response)}
        ref = h.store.save(manifest)
        h.target = replace(h.target,amount_raw=10_000_001)
        h.f.context.protocol = f.f.protocol
        h.f.context.at = now
        f.f, f.target, f.path, f.cfg = h.f.context, h.target, h.new, h.cfg
        f.item = replace(f.item, target=h.target, graduated_at=graduated_at,
                         history_as_of=now, graduation_refs=(ref,),
                         provenance='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE')
        # Public operator grammar is exercised with explicitly synthetic bytes;
        # this fixture never claims real public acquisition provenance.
        f.http_calls, f.sell_output = [], 10_000_000
        op.row = vars(h.target).copy() | {
            'provenance': 'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE',
            'pool_fee_bps': '25', 'graduation_refs': [ref], 'known_hazards': []}
        op.save()
        op.config.write_text(canonical(h.cfg))
        # HistoryFirstTests has its own real pacer; bind the actual handoff
        # pacer instead. Never replace the pacer validator or source identity.
        from desk import provider_pacing
        environment = {provider_pacing.ENV: str(h.pacing),
                       'HELIUS_API_KEY': 'SYNTHETIC_TEST_ONLY',
                       'JUPITER_API_KEY': 'SYNTHETIC_TEST_ONLY'}
        with patch.dict('os.environ', environment):
            first = self._reject(u, h, seed=9)
            self.assertEqual(u.append()['status'], 'RECORDED')
            # The first exact synthetic legacy certificate is reviewed explicitly.
            self._resolve_terminal(u, h, op, first)
            second = self._reject(u, h, seed=10, intrinsic=True)
            self.assertIn('terminal_receipt_hash', second['failed'])
            self._assert_retired_originals(h, first)
            self._assert_retired_originals(h, second)
            self._successor_lifecycle(u, h, op, first, second)

    def _reject(self, u, h, *, seed, intrinsic=False):
        from desk.ownership_acquisition import _Setup
        from tests.test_runtime_extensions import BASE
        protocol = distinct_protocol(seed)
        target = h.f.context.target(protocol=protocol)
        self.assertNotEqual(target.mint, h.target.mint)
        _Setup(h.store, h.f.context.jobs.descriptor(target.scan_id), h.f.context.progress.admission(target.scan_id))
        now = int(time.time())
        raw = migration_for(protocol, now-600)
        response_hash = h.store.save({'data': [raw], 'paginationToken': None})
        ref = h.store.save({'kind':'history_request_v1','method':'getTransactionsForAddress',
            'params':[target.pool,{'transactionDetails':'full','commitment':'finalized','encoding':'jsonParsed'}],
            'response_hash':response_hash})
        mint = {'context':{'slot':100},'value':protocol.rpc('getMultipleAccounts',[])['value'][6]}
        result = u.successful_binary("""import os
from unittest.mock import patch
from desk import paper_cycle as cycle,paper_read_sources as transport
from desk.model import canonical
if a['terminal_policy']:
 from desk import runtime_extensions,paper_terminal_reconciliation as terminal
 runtime_extensions.POLICY=Path(a['extension_policy'])
 terminal.POLICY=Path(a['terminal_policy'])
class Response:
 status=200
 headers={}
 def __init__(self,body):self.body=body;self.pos=0
 def read(self,n=-1):
  b=self.body[self.pos:] if n<0 else self.body[self.pos:self.pos+n];self.pos+=len(b);return b
 def __enter__(self):return self
 def __exit__(self,*args):pass
class HTTP:
 def open(self,request,*,timeout):
  q=json.loads(request.data);m=q['method']
  result=a['mint_response'] if m=='getAccountInfo' and q['params'][0]==a['target']['mint'] else a['atomic'] if m=='getMultipleAccounts' else a['discovery']
  return Response(canonical({'jsonrpc':'2.0','id':transport.RPC_ID,'result':result}).encode())
item=cycle.CycleTarget(cycle.ObservationTarget(**a['target']),'SYNTHETIC_TEST_ONLY',a['graduated_at'],None,'25',graduation_refs=(a['ref'],))
with patch.dict(os.environ,{'DESK_PROVIDER_PACING_DB':a['pacing'],'HELIUS_API_KEY':'SYNTHETIC_TEST_ONLY'}),patch.object(transport,'build_opener',return_value=HTTP()):
 result=cycle.run_once(a['research'],a['evidence'],a['ledger'],a['cfg'],candidates=(item,),dependency_blockers=())
 assert result['blockers']==['SOURCE_CONTENT_REJECTED'] and result['attempted_requests']==3,result
 print(canonical(result))
""", Path(__file__).parents[1] if intrinsic else u.base_root,
            runtime.implementation_hash() if intrinsic else BASE, target=vars(target), mint_response=mint,
            discovery=boosted_response(protocol,'getAccountInfo',[]),
            atomic=boosted_response(protocol,'getMultipleAccounts',[]),
            ref=ref, graduated_at=now-600, pacing=str(h.pacing),
            terminal_policy=str(__import__('desk.paper_terminal_reconciliation',fromlist=['POLICY']).POLICY) if intrinsic else '')
        failed = json.loads(result.stdout)
        with h.store.connect() as c:
            row = c.execute('SELECT id,intent_hash,outcome_hash FROM paper_observation_passes ORDER BY rowid DESC LIMIT 1').fetchone()
            pages = [(key,h.store.load(key)) for (key,) in c.execute('SELECT hash FROM pages')]
        refs = tuple(key for key,value in sorted((p for p in pages if isinstance(p[1],dict)),key=lambda x:x[1].get('requests_used',0))
            if value.get('kind')=='paper_read_attempt_v1' and value.get('scan_id')==target.scan_id)
        self.assertEqual(len(refs),3);self.assertIsNone(row[2])
        return {'target':target,'row':row,'failed':failed,'refs':refs,
            'admission':h.f.context.progress.admission(target.scan_id),
            'originals':{key:h.store.load(key) for key in (*refs,row[1],failed['evidence_hash'])}}

    def _successor_lifecycle(self, u, h, op, first, second):
        from desk import paper_terminal_reconciliation as terminal
        old, old_cfg = h.new, h.cfg
        original_ledgers = {p: dump(p) for p in (h.old, old)}
        with h.store.connect() as c:
            prefixes = {t: c.execute('SELECT * FROM '+t).fetchall() for t in
                ('paper_monitoring_context_handoff', 'paper_monitoring_budget',
                 'paper_monitoring_reservations', 'paper_monitoring_outcomes',
                 terminal.TABLE)}
        new = u.root/'profile2-independent.sqlite'
        cfg = {**old_cfg, 'paper_token_profile_version': 2}
        paper_cycle.initialize(new, cfg)
        pin = monitoring_successor.plan(h.research, h.evidence, old, new, h.pacing,
            cfg, old_source=runtime.implementation_hash())
        policy = u.root/'synthetic-successor-policy.json'
        policy.write_text(canonical({'version': 1, 'successors': [pin]}))
        with patch.object(monitoring_successor, 'POLICY', policy):
            bound = monitoring_handoff.activate_successor(h.research, h.evidence,
                old, new, h.pacing, cfg, pins=pin)
            self.assertEqual(bound['status'], 'BOUND')
            for p, before in original_ledgers.items(): self.assertEqual(dump(p), before)
            with self.assertRaises(MonitoringBlocked):
                MonitoringBudget(h.store, old, old_cfg).snapshot()
            for rejected in (first, second):
                self.assertEqual(terminal.gate(h.store, h.research,
                    (rejected['target'].scan_id,)), 'REJECTED_SCAN_RETIRED')
                self._assert_retired_originals(h, rejected)
            h.new, h.cfg = new, cfg
            f = op.f
            f.path, f.cfg = new, cfg
            op.config.write_text(canonical(cfg))
            evidence_before = dump(h.evidence)
            with self.assertRaises(MonitoringBlocked):
                MonitoringBudget(h.store,old,old_cfg).reserve_read(f.f.progress,
                    h.target.scan_id,'getSlot',[{'commitment':'finalized'}])
            self.assertEqual(dump(h.evidence),evidence_before)
            for rejected in (first,second):
                original_row = copy.deepcopy(op.row)
                op.row.update(vars(rejected['target']));op.save()
                try:
                    with patch.object(operator_fixture.tool.cli,'_credentials',
                                      side_effect=AssertionError('retired credentials')):
                        with self.assertRaises(ValueError):op.invoke(live=True,systemd_credentials=True)
                finally:
                    op.row=original_row;op.save()
            f.buy_output_raw, f.sell_output_per_unit_raw = 60_000_000, 170_000
            code = textwrap.dedent(inspect.getsource(f.actual_cycle))
            namespace = {**vars(wire_fixture), 'outer': f}
            exec(textwrap.dedent(code[code.index('    class Opener:'):code.index('    with ExitStack()')]), namespace)
            delegate = namespace['Opener']()
            outer = self
            class HTTP:
                price_at = None
                virtual = 100_000_000_000
                def open(self, request, *, timeout):
                    if '/price/v3?' in request.full_url:
                        self.price_at = int(time.time())-1
                        return Response(canonical({SOL: {'usdPrice':100, 'blockId':100, 'decimals':9}}).encode())
                    call = json.loads(request.data) if request.method == 'POST' else None
                    if call and call['method'] == 'getBlockTime':
                        return Response(canonical({'jsonrpc':'2.0', 'id':transport.RPC_ID, 'result':self.price_at}).encode())
                    if call and call['method'] == 'getTransactionsForAddress':
                        # NEW synthetic rows at the exact requested window end.
                        # The delegate emits at f.at-1; no retained rows restamped.
                        f.f.at = call['params'][1]['filters']['blockTime']['lt']
                    response = delegate.open(request, timeout=timeout)
                    if call and call['method'] in ('getAccountInfo','getMultipleAccounts'):
                        wire = json.loads(response.body)
                        wire['result'] = prior.nonboosted_fee_response(call['method'],call['params'],wire['result'],h.target.pool)
                        result = wire['result']
                        account = result['value'][3] if call['method']=='getMultipleAccounts' else result['value'] if call['params'][0]==h.target.pool else None
                        if account is not None:
                            raw = bytearray(base64.b64decode(account['data'][0]))
                            raw[245:261] = self.virtual.to_bytes(16,'little',signed=True)
                            account['data'][0] = base64.b64encode(raw).decode()
                        response = Response(canonical(wire).encode())
                    if call and call['method']=='getTransactionsForAddress':
                        wire = json.loads(response.body)
                        wire['result']['data'] = [outer._parsed_boost_row(row,i) for i,row in enumerate(wire['result']['data'])]
                        response = Response(canonical(wire).encode())
                    return response
            http = HTTP()
            with patch.object(operator_fixture.tool.cli, '_credentials'), patch.object(transport, 'build_opener',return_value=http):
                started = time.monotonic()
                entry = op.invoke(live=True,systemd_credentials=True)
                self.assertEqual(entry['status'],'COMPLETE',(entry,time.monotonic()-started))
                buy = next((o for o in entry['outcomes'] if o.get('side')=='buy'), None)
                self.assertIsNotNone(buy, entry)
                self.assertEqual(buy['amount_sol'],'0.010000001')
                self.assertEqual(buy['entry_policy']['volume_feature'],BOOST_VOLUME_FEATURE)
                self.assertEqual(buy['entry_policy']['signal_profile']['name'],BOOST_SIGNAL_PROFILE)
                self.assertIn('UNRESOLVED_OWNERSHIP_HISTORY',buy['entry_policy']['risk_flags'])
                self.assertFalse(buy['entry_policy']['entry_authorized'])
                self.assertFalse(buy['entry_policy']['source_authenticated'])
                with sqlite3.connect(new) as c:
                    event = json.loads(c.execute('SELECT payload FROM events WHERE event_id=?',(entry['events'][0],)).fetchone()[0])
                self.assertIsNone(event['volume_vs_liq'])
                self._physical_capacity_negative(event,cfg)
                admission = f.f.progress.admission(h.target.scan_id)
                held = paper_cycle._state(new,cfg)['positions'][h.target.mint]
                initial_raw = quote_execution.raw_quantity(held['qty'],6)
                item = replace(f.item,graduation_refs=(),target=replace(h.target,amount_raw=initial_raw))
                http.virtual = 500_000_000_000
                f.sell_output_per_unit_raw = 500_000
                started = time.monotonic()
                partial = paper_cycle.run_once(h.research,h.evidence,new,cfg,
                    position_targets=(item,),candidates=(),dependency_blockers=(),monitoring=True)
                self.assertEqual(partial['status'],'COMPLETE',(partial,time.monotonic()-started))
                sold = next(o for o in partial['outcomes'] if o.get('side')=='sell')
                self.assertEqual(sold['quote_execution']['input_raw'],initial_raw*3//10)
                self.assertEqual(partial['monitoring_attempted_requests'],5)
                held = paper_cycle._state(new,cfg)['positions'][h.target.mint]
                self.assertEqual(quote_execution.raw_quantity(held['qty'],6),initial_raw-initial_raw*3//10)
                f.sell_output_per_unit_raw = 110_000
                item = replace(item,target=replace(item.target,amount_raw=quote_execution.raw_quantity(held['qty'],6)))
                started = time.monotonic()
                closed = paper_cycle.run_once(h.research,h.evidence,new,cfg,
                    position_targets=(item,),candidates=(),dependency_blockers=(),monitoring=True)
                self.assertEqual(closed['status'],'COMPLETE',(closed,time.monotonic()-started))
                full = next(o for o in closed['outcomes'] if o.get('side')=='sell')
                self.assertEqual(full['quote_execution']['input_raw'],item.target.amount_raw)
                self.assertEqual(closed['monitoring_attempted_requests'],4)
                state = paper_cycle._state(new,cfg)
                self.assertEqual(state['positions'],{})
                self.assertEqual(Decimal(state['cash']),Decimal(cfg['initial_equity_sol'])-
                    Decimal(buy['amount_sol'])-Decimal(buy['fee_sol'])+
                    Decimal(sold['proceeds_sol'])+Decimal(full['proceeds_sol']))
                before = dump(new),len(f.http_calls)
                restart = paper_cycle.run_once(h.research,h.evidence,new,cfg,
                    candidates=(),dependency_blockers=(),monitoring=True)
                self.assertEqual(restart['attempted_requests'],0)
                self.assertEqual((dump(new),len(f.http_calls)),before)
                self.assertEqual(f.f.progress.admission(h.target.scan_id),admission)
                self.assertEqual(MonitoringBudget(h.store,new,cfg).snapshot()['total_used'],10)
            for p,before in original_ledgers.items():self.assertEqual(dump(p),before)
            for rejected in (first,second):self._assert_retired_originals(h,rejected)
            with h.store.connect() as c:
                for table,rows in prefixes.items():
                    current = c.execute('SELECT * FROM '+table).fetchall()
                    if table=='paper_monitoring_budget':
                        self.assertEqual(current[0][:7],rows[0][:7])
                        self.assertGreaterEqual(current[0][7],rows[0][7])
                        self.assertEqual(current[0][8],10)
                        self.assertEqual(current[0][9],rows[0][9])
                        continue
                    if table=='paper_monitoring_reservations':current=[row[:len(rows[0])] for row in current[:len(rows)]]
                    else:current=current[:len(rows)]
                    self.assertEqual(current,rows)
                c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)',('f'*32,'a'*64))
            calls = len(f.http_calls)
            self.assertEqual(terminal.gate(h.store,h.research,()),'OBSERVATION_RECOVERY_REQUIRED')
            with patch.object(operator_fixture.tool.cli,'_credentials',side_effect=AssertionError('pending credentials')):
                with self.assertRaises(ValueError):op.invoke(live=True,systemd_credentials=True)
            self.assertEqual(len(f.http_calls),calls)

    def _parsed_boost_row(self,row,index):
        from tests.test_live_strategy_features import SCHEMA
        from desk.parsed_v1 import validate
        row = copy.deepcopy(row)
        ix = row['transaction']['message']['instructions'][0]
        fields = next(t['type']['fields'] for t in SCHEMA['types'] if t['name']=='BuyEvent')
        raw = bytearray(unbase58(ix['data']));offset=16
        for field in fields:
            kind=field['type']
            if field['name']=='virtual_quote_reserves':raw[offset:offset+16]=(1000).to_bytes(16,'little',signed=True)
            if field['name']=='can_boost':raw[offset]=1
            offset += 32 if kind=='pubkey' else 1 if kind=='bool' else 4+int.from_bytes(raw[offset:offset+4],'little') if kind=='string' else int(kind[1:])//8
        ix.update(data=base58(raw),stackHeight=1)
        payer=base58((index+1000).to_bytes(32,'big'))
        keys=list(dict.fromkeys([payer,ix['programId'],*ix['accounts']]))
        row['version']=1
        row['transaction']['signatures']=[base58((index+1).to_bytes(64,'big'))]
        row['transaction']['message'].update(accountKeys=[{'pubkey':key,'signer':i==0,'source':'transaction','writable':i==0} for i,key in enumerate(keys)],
            recentBlockhash=base58(bytes([42])*32),transactionConfig={k:None for k in ('priorityFee','computeUnitLimit','loadedAccountsDataSizeLimit','heapSize')})
        row['meta'].update(fee=0,preBalances=[0]*len(keys),postBalances=[0]*len(keys))
        validate(row)
        return row

    def _physical_capacity_negative(self,event,cfg):
        from desk import boosted_paper
        from desk.live_observation import ProviderObservation,ingest_pool
        from desk.sell_fees import standard_sell_amounts
        from desk.dynamic_fees import standard_sol_fees
        from desk.pools import verify_pool
        original = copy.deepcopy(event['paper_pool_evidence'])
        raw = json.loads(original['original_json'])
        positive = ingest_pool(lambda:ProviderObservation('SYNTHETIC_REPLAY_ONLY',1,raw),
            mint=event['mint'],pool=event['pool'],now=1,token_profile_version=2)
        self.assertEqual(positive.gross_reserve_lamports, prior.GROSS_QUOTE_RAW)
        self.assertEqual(positive.reserve_lamports, prior.NET_QUOTE_RAW)
        self.assertEqual(positive.virtual_quote_reserves_lamports,100_000_000_000)
        self.assertNotEqual(positive.reserve_sol,Decimal(prior.GROSS_QUOTE_RAW)/10**9)
        account = raw['result']['value'][1]
        data = bytearray(base64.b64decode(account['data'][0]))
        # New hypothetical capture, not an edit to any persisted original.
        data[64:72]=(prior.PROTOCOL_FEES_RAW+prior.CREATOR_FEES_RAW+1).to_bytes(8,'little')
        account['data'][0]=base64.b64encode(data).decode()
        obs = ingest_pool(lambda:ProviderObservation('SYNTHETIC_TINY_PHYSICAL_ONLY',1,raw),mint=event['mint'],pool=event['pool'],now=1,token_profile_version=2)
        self.assertEqual(obs.reserve_lamports,1)
        self.assertGreater(obs.spot_sol_per_token,0)
        def rpc(method,params):
            if method=='getAccountInfo' and params==[event['pool'],{'encoding':'base64','commitment':'confirmed'}]:
                return raw['discovery']
            self.assertEqual((method,params),(raw['method'],raw['params']))
            return raw['result']
        checked=verify_pool(event['pool'],event['mint'],rpc,capture=digest,token_profile_version=2)
        mint=base64.b64decode(raw['result']['value'][6]['data'][0])
        rates=standard_sol_fees(checked['dynamic_fee_config'],int.from_bytes(mint[36:44],'little'),
            int(checked['base_reserve_raw']),int(checked['gross_quote_reserve_raw']),
            canonical=checked['canonical_migration_pool'],
            virtual_quote_reserves=int(checked['virtual_quote_reserves_raw']),token_profile_version=2)
        amounts=standard_sell_amounts(60_000_000,int(checked['base_reserve_raw']),
            int(checked['effective_quote_reserve_raw']),rates['fees_bps'],
            has_creator=checked['coin_creator']!='11111111111111111111111111111111')
        with self.assertRaisesRegex(ValueError,'physical quote capacity'):
            boosted_paper.require_capacity(amounts,obs.reserve_lamports)
        self.assertEqual(event['paper_pool_evidence'],original)
