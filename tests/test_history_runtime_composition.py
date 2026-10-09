"""SYNTHETIC_TEST_ONLY: real HTTPResponse, pacing, receipts and accounting.

Only HTTPS acquisition and credential loading are injected. Temporary fixture
policy pins and inert predecessor marker do not authenticate provider evidence.
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
from tests import test_monitoring_runtime_upgrade as runtime_fixture
from tests import test_paper_cycle as wire_fixture
from tests.test_paper_read_sources import Response
from tests.test_runtime_compatibility import dump


# New synthetic raw account captures, never edits to retained evidence rows.
PROTOCOL_FEES_RAW = 123_456_789
CREATOR_FEES_RAW = 987_654_321
GROSS_QUOTE_RAW = 100_000_000_000
NET_QUOTE_RAW = GROSS_QUOTE_RAW - PROTOCOL_FEES_RAW - CREATOR_FEES_RAW


def nonboosted_fee_response(method, params, response, pool):
    from desk.security import TOKEN_2022
    response = copy.deepcopy(response)
    account = (response['value'][3] if method == 'getMultipleAccounts' else
               response['value'] if method == 'getAccountInfo' and params[0] == pool else None)
    if account is not None:
        raw = bytearray(base64.b64decode(account['data'][0]))
        raw.extend(bytes(max(0, 300-len(raw))))
        raw[245:261] = (-PROTOCOL_FEES_RAW-CREATOR_FEES_RAW).to_bytes(16, 'little', signed=True)
        raw[271:279] = PROTOCOL_FEES_RAW.to_bytes(8, 'little')
        raw[279:287] = CREATOR_FEES_RAW.to_bytes(8, 'little')
        account['data'][0] = base64.b64encode(raw).decode()
    if method == 'getMultipleAccounts':
        lp = response['value'][2]
        raw = bytearray(base64.b64decode(lp['data'][0]))
        if len(raw) != 82:
            raise AssertionError('Synthetic LP fixture must start at exact canonical layout')
        raw[44] = 9
        lp['owner'] = TOKEN_2022
        lp['data'][0] = base64.b64encode(raw).decode()
    return response


def distinct_protocol(seed=9):
    from solders.pubkey import Pubkey
    from tests.test_pools import PoolTests
    from desk.pools import ATA
    from desk.providers import PUMP, PUMPSWAP
    from desk.security import TOKEN_PROGRAM
    p=PoolTests();p.setUp();p.mint=Pubkey.from_bytes(bytes([seed])*32)
    p.creator=Pubkey.find_program_address([b'pool-authority',bytes(p.mint)],Pubkey.from_string(PUMP))[0]
    p.pool,p.bump=Pubkey.find_program_address([b'pool',bytes(2),bytes(p.creator),bytes(p.mint),bytes(Pubkey.from_string(SOL))],Pubkey.from_string(PUMPSWAP))
    p.lp=Pubkey.find_program_address([b'pool_lp_mint',bytes(p.pool)],Pubkey.from_string(PUMPSWAP))[0]
    p.vaults=[Pubkey.find_program_address([bytes(p.pool),bytes(Pubkey.from_string(TOKEN_PROGRAM)),bytes(mint)],Pubkey.from_string(ATA))[0] for mint in (p.mint,Pubkey.from_string(SOL))]
    p.raw=p.raw[:8]+bytes([p.bump])+bytes(2)+b''.join(bytes(v) for v in (p.creator,p.mint,Pubkey.from_string(SOL),p.lp,*p.vaults))+p.raw[203:]
    return p


def migration_for(protocol,at):
    from tests.test_graduation_witness import fixture
    from desk import graduation_witness as g
    from desk.providers import PUMP
    raw,mint,pool=fixture('migrate_v2')
    replacements={mint:str(protocol.mint),pool:str(protocol.pool),
        g._pda([b'pool-authority',unbase58(mint)],PUMP):str(protocol.creator),
        g._pda([b'bonding-curve',unbase58(mint)],PUMP):g._pda([b'bonding-curve',bytes(protocol.mint)],PUMP)}
    outer=raw['transaction']['message']['instructions'][0]
    outer['accounts']=[replacements.get(v,v) for v in outer['accounts']]
    ix=raw['meta']['innerInstructions'][0]['instructions'][0];d=bytearray(unbase58(ix['data']))
    d[48:80]=bytes(protocol.mint);d[104:136]=unbase58(replacements[g._pda([b'bonding-curve',unbase58(mint)],PUMP)])
    d[136:144]=at.to_bytes(8,'little',signed=True);d[144:176]=bytes(protocol.pool)
    ix['data']=base58(d);raw['blockTime']=at
    raw['transaction']['signatures']=['SYNTHETIC_BOOSTED_CANDIDATE_A']
    return raw


def boosted_response(protocol,method,params):
    result=copy.deepcopy(protocol.rpc(method,params))
    account=result['value'][3] if method=='getMultipleAccounts' else result['value']
    d=bytearray(base64.b64decode(account['data'][0]));d.extend(bytes(max(0,300-len(d))))
    d[245:261]=(1).to_bytes(16,'little',signed=True)
    account['data'][0]=base64.b64encode(d).decode()
    return result


class HistoryRuntimeCompositionTests(unittest.TestCase):
    def test_reviewed_terminal_a_unblocks_distinct_b_wrapper_sell_restart(self):
        self._scenario(continuation=True, event_quote_bytes=bytes(32),
                       fee_profile=True, extension=True, terminal=True)

    def test_raw_fee_lp_entry_sell_restart_preserves_runtime_receipts(self):
        self._scenario(continuation=True, event_quote_bytes=bytes(32),
                       fee_profile=True, extension=True)

    def test_synthetic_sentinel_entry_exit_continuation_preserves_originals(self):
        # Retained public event stays unchanged. Its representation is reused
        # only in a separately constructed synthetic current-time transaction.
        path = Path(__file__).parents[1]/'fixtures/migration_quote_sentinel_excerpt.json'
        original_bytes = path.read_bytes()
        excerpt = json.loads(original_bytes)
        self.assertEqual(digest(excerpt['event']),
            '0fdaf2a531590ae451229d99345d546713f06ec7f1d043b9534e0bf89697ba46')
        self._scenario(continuation=True,
            event_quote_bytes=unbase58(excerpt['event']['data'])[-32:])
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_chunked_history_guarded_entry_runtime_receipt_and_monitoring_restart(self):
        self._scenario()

    def test_invalid_chunked_history_retains_charge_and_blocks_restart_without_retry(self):
        self._scenario(invalid_history=True)

    def _scenario(self, invalid_history=False, continuation=False, event_quote_bytes=None, fee_profile=False, extension=False, terminal=False):
        if extension:
            from tests import test_runtime_extensions
            u = test_runtime_extensions.RuntimeExtensionTests()
        elif continuation:
            from tests import test_runtime_continuation
            u = test_runtime_continuation.RuntimeContinuationTests()
        else:
            u = runtime_fixture.MonitoringRuntimeUpgradeTests()
        u.setUp()
        self.addCleanup(u.doCleanups)
        op = operator_fixture.HistoryFirstTests()
        op.setUp()
        self.addCleanup(op.doCleanups)
        h, f = u.f, op.f
        old_store = f.f.progress.store
        now = int(time.time())
        manifest = old_store.load(f.item.graduation_refs[0])
        response = copy.deepcopy(old_store.load(manifest['response_hash']))
        if continuation:
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
        if continuation:
            data[-32:] = event_quote_bytes
        ix['data'] = base58(data)
        graduated_at = raw['blockTime']
        manifest = {**manifest, 'response_hash': h.store.save(response)}
        ref = h.store.save(manifest)
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
        retired = dump(h.old)
        with h.store.connect() as c:
            original_reservations = c.execute('SELECT * FROM paper_monitoring_reservations').fetchall()
            original_outcomes = c.execute('SELECT * FROM paper_monitoring_outcomes').fetchall()
        if continuation:
            with sqlite3.connect(h.new) as c:
                first_receipt = c.execute('SELECT payload,payload_hash FROM paper_runtime_transition').fetchone()
                if extension:
                    second_receipt = c.execute('SELECT payload,payload_hash FROM paper_runtime_continuation').fetchone()
            preserved = tuple(dump(path) for path in (h.old, h.evidence, h.research, h.pacing))
        with patch.dict('os.environ', environment):
            if terminal:
                rejected = self._legacy_rejection(u, h)
                preserved = tuple(dump(path) for path in (h.old, h.evidence, h.research, h.pacing))
            self.assertEqual((u.append() if extension else u.upgrade())['status'], 'RECORDED')
            if extension:
                from desk import runtime_extensions
                with sqlite3.connect(h.new) as c:
                    journal_row = c.execute(f'SELECT * FROM {runtime_extensions.TABLE}').fetchall()
            if continuation:
                self.assertEqual(tuple(dump(path) for path in (h.old, h.evidence, h.research, h.pacing)), preserved)
            code = textwrap.dedent(inspect.getsource(f.actual_cycle))
            start, end = code.index('    class Opener:'), code.index('    with ExitStack()')
            namespace = {**vars(wire_fixture), 'outer': f}
            exec(textwrap.dedent(code[start:end]), namespace)
            delegate = namespace['Opener']()
            history_bodies = []

            class Socket:
                def __init__(self, data): self.data = data
                def makefile(self, *args): return io.BytesIO(self.data)

            class HTTP:
                price_at = None
                def open(self, request, *, timeout):
                    if '/price/v3?' in request.full_url:
                        self.price_at = int(time.time()) - 1
                        return Response(canonical({SOL: {'usdPrice': 100, 'blockId': 100, 'decimals': 9}}).encode())
                    call = json.loads(request.data) if request.method == 'POST' else None
                    if call and call['method'] == 'getBlockTime':
                        return Response(canonical({'jsonrpc': '2.0', 'id': transport.RPC_ID,
                                                   'result': self.price_at}).encode())
                    if terminal and call and call['method']=='getTransactionsForAddress':
                        # Generate NEW synthetic B trades at this request's time;
                        # every retained A transaction/response stays unchanged.
                        f.f.at = int(time.time())-1
                    result = delegate.open(request, timeout=timeout)
                    if fee_profile and call and call['method'] in ('getAccountInfo', 'getMultipleAccounts'):
                        wire = json.loads(result.body)
                        wire['result'] = nonboosted_fee_response(call['method'], call['params'], wire['result'], h.target.pool)
                        result = Response(canonical(wire).encode())
                    if call and call['method'] == 'getTransactionsForAddress':
                        body = b'{' if invalid_history else result.body
                        history_bodies.append(body)
                        split = len(body)//2
                        chunks = b''.join(f'{len(part):x}\r\n'.encode()+part+b'\r\n'
                                          for part in (body[:split], body[split:]) if part)
                        result = HTTPResponse(Socket(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
                                                     +chunks+b'0\r\n\r\n'))
                        result.begin()
                    return result

            http = HTTP()
            with patch.object(operator_fixture.tool.cli, '_credentials'), \
                    patch.object(transport, 'build_opener', return_value=http):
                if invalid_history:
                    with self.assertRaises(ValueError):
                        op.invoke(live=True, systemd_credentials=True)
                    admission = f.f.progress.admission(h.target.scan_id)
                    self.assertEqual(admission['requests_used'], 1)
                    calls = len(f.http_calls)
                    with self.assertRaises(ValueError):
                        op.invoke(live=True, systemd_credentials=True)
                    self.assertEqual(len(f.http_calls), calls)
                    self.assertEqual(f.f.progress.admission(h.target.scan_id), admission)
                    self.assertEqual(paper_cycle._state(h.new, h.cfg)['positions'], {})
                    self.assertEqual(dump(h.old), retired)
                    if terminal:
                        self._assert_retired_originals(h, rejected)
                    with h.store.connect() as c:
                        pages = [h.store.load(key) for (key,) in c.execute('SELECT hash FROM pages')]
                        self.assertEqual(c.execute('SELECT count(*) FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()[0], 1)
                    self.assertTrue(any(isinstance(page, dict) and page.get('method') == 'getTransactionsForAddress'
                        and page.get('response_bytes_base64') == base64.b64encode(b'{').decode()
                        for page in pages))
                    return
                if terminal:
                    self._resolve_terminal(u, h, op, rejected)
                entry = op.invoke(live=True, systemd_credentials=True)
                self.assertEqual(entry['status'], 'COMPLETE', entry)
                self.assertTrue(any(row.get('side') == 'buy' for row in entry['outcomes']), entry)
                self.assertEqual(len(history_bodies), 1)
                self.assertEqual(f.f.progress.admission(h.target.scan_id)['requests_used'], 15 if continuation else 9)
                with h.store.connect() as c:
                    pages = [h.store.load(key) for (key,) in c.execute('SELECT hash FROM pages')]
                self.assertTrue(any(isinstance(page, dict) and page.get('method') == 'getTransactionsForAddress'
                    and page.get('response_bytes_base64') == base64.b64encode(history_bodies[0]).decode()
                    and page.get('source_id') == transport.PaperReadSources.rpc_source_id for page in pages))
                if fee_profile:
                    self._assert_nonboosted_originals(h.store, h.target)
                admission = f.f.progress.admission(h.target.scan_id)
                held = paper_cycle._state(h.new, h.cfg)['positions'][h.target.mint]
                if continuation:
                    item = replace(f.item, graduation_refs=(), known_hazards=('SYNTHETIC_KNOWN_HAZARD',),
                        target=replace(h.target, amount_raw=quote_execution.raw_quantity(held['qty'], 6)))
                    result = paper_cycle.run_once(h.research, h.evidence, h.new, h.cfg,
                        position_targets=(item,), candidates=(), dependency_blockers=(), monitoring=True)
                    self.assertEqual(result['status'], 'COMPLETE', result)
                    self.assertEqual(result['monitoring_attempted_requests'], 4)
                    self.assertTrue(any(row.get('side') == 'sell' for row in result['outcomes']))
                    if fee_profile:
                        self._assert_nonboosted_originals(h.store, h.target)
                        buy = next(row for row in entry['outcomes'] if row.get('side') == 'buy')
                        sell = next(row for row in result['outcomes'] if row.get('side') == 'sell')
                        self.assertEqual(sell['quote_execution']['input_raw'], item.target.amount_raw)
                        self.assertEqual(quote_execution.raw_quantity(held['qty'], 6), item.target.amount_raw)
                        for outcome, estimated, decimals in ((buy, 1_000_000, 6), (sell, 10_000_000, 9)):
                            quoted = outcome['quote_execution']
                            minimum = estimated*99//100
                            simulated = minimum*(10_000-int(h.cfg['adverse_slippage_bps']))//10_000
                            self.assertEqual(quoted['estimated_output_raw'], estimated)
                            self.assertEqual(quoted['provider_minimum_output_raw'], minimum)
                            self.assertEqual(quoted['simulated_output_raw'], simulated)
                            self.assertEqual(quoted['status'], 'EXECUTION_UNVERIFIED')
                            self.assertFalse(quoted['transaction_verified'])
                            self.assertEqual(Decimal(outcome['fee_sol']), Decimal(h.cfg['fixed_fee_sol']))
                            if outcome is buy:
                                self.assertEqual(quote_execution.raw_quantity(held['qty'], decimals), simulated)
                            else:
                                self.assertEqual(Decimal(sell['proceeds_sol']), Decimal(simulated)/10**decimals-Decimal(h.cfg['fixed_fee_sol']))
                        final = paper_cycle._state(h.new, h.cfg)
                        self.assertEqual(Decimal(final['cash']), Decimal(h.cfg['initial_equity_sol'])
                            - Decimal(buy['amount_sol']) - Decimal(buy['fee_sol']) + Decimal(sell['proceeds_sol']))
                    self.assertEqual(paper_cycle._state(h.new, h.cfg)['positions'], {})
                    self.assertEqual(f.f.progress.admission(h.target.scan_id), admission)
                    self.assertEqual(MonitoringBudget(h.store, h.new, h.cfg).snapshot()['total_used'], 5)
                    restart = paper_cycle.run_once(h.research, h.evidence, h.new, h.cfg,
                        candidates=(), dependency_blockers=(), monitoring=True)
                    self.assertEqual(restart['status'], 'COMPLETE', restart)
                    self.assertEqual(restart['attempted_requests'], 0)
                    with sqlite3.connect(h.new) as c:
                        self.assertEqual(c.execute('SELECT payload,payload_hash FROM paper_runtime_transition').fetchone(), first_receipt)
                        if extension:
                            self.assertEqual(c.execute('SELECT payload,payload_hash FROM paper_runtime_continuation').fetchone(), second_receipt)
                            from desk import runtime_extensions, runtime_compatibility
                            self.assertEqual(runtime_compatibility.require_runtime(c), u.source)
                            self.assertEqual(len(journal_row), 1)
                            self.assertEqual(c.execute(f'SELECT * FROM {runtime_extensions.TABLE}').fetchall(), journal_row)
                    self.assertEqual(h.store.load(ref)['response_hash'], digest(response))
                    self.assertEqual(h.store.load(digest(response))['data'][0], raw)
                    self.assertEqual(dump(h.old), retired)
                    if terminal:
                        self._assert_retired_originals(h, rejected)
                    with h.store.connect() as c:
                        self.assertEqual(c.execute('SELECT * FROM paper_monitoring_reservations WHERE id=1').fetchall(), original_reservations)
                        self.assertEqual(c.execute('SELECT * FROM paper_monitoring_outcomes WHERE reservation_id=1').fetchall(), original_outcomes)
                    return
                budget = MonitoringBudget(h.store, h.new, h.cfg)
                source = transport.PaperReadSources(f.f.progress, h.target.scan_id,
                                                   monitoring_budget=budget)
                value, key = source.rpc_with_evidence('getSlot', [{'commitment': 'finalized'}],
                                                     timeout_seconds=15)
                self.assertEqual(value, 110)
                retained = h.store.load(key)
                self.assertEqual(retained['source_id'], source.rpc_source_id)
                self.assertIsNone(retained['failure_code'])
                self.assertEqual(retained['monitoring_reservation']['id'], 2)
                self.assertEqual(f.f.progress.admission(h.target.scan_id), admission)
                self.assertEqual(paper_cycle._state(h.new, h.cfg)['positions'][h.target.mint], held)
                snapshot = MonitoringBudget(h.store, h.new, h.cfg).snapshot()
                self.assertEqual(snapshot['total_used'], 2)
                self.assertEqual(snapshot['blockers'], [])
                before = dump(h.evidence), len(f.http_calls)
                with self.assertRaises(ValueError):
                    op.invoke(live=True, systemd_credentials=True)
                self.assertEqual((dump(h.evidence), len(f.http_calls)), before)
                self.assertEqual(MonitoringBudget(h.store, h.new, h.cfg).snapshot()['total_used'], 2)
        self.assertEqual(dump(h.old), retired)
        with h.store.connect() as c:
            self.assertEqual(c.execute('SELECT * FROM paper_monitoring_reservations WHERE id=1').fetchall(), original_reservations)
            self.assertEqual(c.execute('SELECT * FROM paper_monitoring_outcomes WHERE reservation_id=1').fetchall(), original_outcomes)

    def _assert_nonboosted_originals(self, store, target):
        from desk.live_observation import ProviderObservation, ingest_pool
        with store.connect() as c:
            captures = [store.load(key) for (key,) in c.execute('SELECT hash FROM pages')]
        captures = [raw for raw in captures if isinstance(raw, dict)
            and raw.get('kind') == 'pool_snapshot' and target.pool in raw['params'][0]]
        self.assertTrue(captures)
        for raw in captures:
            original = copy.deepcopy(raw)
            observed = ingest_pool(lambda: ProviderObservation('SYNTHETIC_REPLAY_ONLY', 1, raw),
                mint=target.mint, pool=target.pool, now=1, token_profile_version=1)
            self.assertEqual(observed.reserve_lamports, NET_QUOTE_RAW)
            self.assertEqual(observed.gross_reserve_lamports, GROSS_QUOTE_RAW)
            self.assertEqual(observed.accrued_protocol_fees_lamports, PROTOCOL_FEES_RAW)
            self.assertEqual(observed.accrued_creator_fees_lamports, CREATOR_FEES_RAW)
            self.assertEqual(observed.virtual_quote_reserves_lamports, -PROTOCOL_FEES_RAW-CREATOR_FEES_RAW)
            self.assertEqual(observed.reserve_sol, Decimal(NET_QUOTE_RAW)/10**9)
            self.assertNotEqual(observed.reserve_sol, Decimal(GROSS_QUOTE_RAW)/10**9)
            self.assertEqual(observed.spot_sol_per_token, observed.reserve_sol/observed.reserve_tokens)
            self.assertEqual(observed.source.raw_hash, digest(original))
            self.assertEqual(raw, original)

    def _legacy_rejection(self, u, h):
        from desk.ownership_acquisition import _Setup
        from tests.test_runtime_extensions import BASE
        protocol = distinct_protocol()
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
""", u.base_root, BASE, target=vars(target), mint_response=mint,
            discovery=boosted_response(protocol,'getAccountInfo',[]),
            atomic=boosted_response(protocol,'getMultipleAccounts',[]),
            ref=ref, graduated_at=now-600, pacing=str(h.pacing))
        failed = json.loads(result.stdout)
        with h.store.connect() as c:
            row = c.execute('SELECT id,intent_hash,outcome_hash FROM paper_observation_passes').fetchone()
            pages = [(key,h.store.load(key)) for (key,) in c.execute('SELECT hash FROM pages')]
        refs = tuple(key for key,value in sorted(pages,key=lambda x:x[1].get('requests_used',0))
            if value.get('kind')=='paper_read_attempt_v1' and value.get('scan_id')==target.scan_id)
        self.assertEqual(len(refs),3);self.assertIsNone(row[2])
        return {'target':target,'row':row,'failed':failed,'refs':refs,
            'admission':h.f.context.progress.admission(target.scan_id),
            'originals':{key:h.store.load(key) for key in (*refs,row[1],failed['evidence_hash'])}}

    def _resolve_terminal(self, u, h, op, rejected):
        from desk import paper_terminal_reconciliation as r
        calls = len(op.f.http_calls)
        with self.assertRaises(ValueError):op.invoke(live=True,systemd_credentials=True)
        self.assertEqual(len(op.f.http_calls),calls)
        target = rejected['target'];records = [h.store.load(key) for key in rejected['refs']]
        unit='synthetic-composition.service';invocation='b'*32
        start=f'Started {unit} - /synthetic/python tools/history_first_paper_entry.py --research-db {h.research} --evidence-db {h.evidence} --ledger-db {h.new} --targets /synthetic/targets --execute'
        messages=[start,unit+': Main process exited, code=exited, status=2/INVALIDARGUMENT',unit+": Failed with result 'exit-code'.",unit+': Consumed synthetic CPU time.']
        times=[records[0]['observed_at']-1,records[-1]['observed_at']+1,records[-1]['observed_at']+1,records[-1]['observed_at']+1]
        invocation_rows=[{'MESSAGE':m,'__REALTIME_TIMESTAMP':str(times[i]*1_000_000+i),'_SYSTEMD_UNIT':'init.scope','UNIT':unit,'INVOCATION_ID':invocation} for i,m in enumerate(messages)]
        invocation_rows[0]['JOB_RESULT']='done'
        proof={'pass_id':rejected['row'][0],'outcome_hash':rejected['failed']['evidence_hash'],
            'attempt_refs':rejected['refs'],'invocation_hash':h.store.save(invocation_rows),'pacing_db':h.pacing}
        policy=u.root/'synthetic-terminal-policy.json'
        policy.write_text(canonical({'version':1,'associations':[]}))
        pin=r.review_plan(h.research,h.evidence,h.new,h.cfg,**proof)
        policy.write_text(canonical({'version':1,'associations':[pin]}))
        before=dump(h.new),dump(h.research)
        with patch.object(r,'POLICY',policy):
            self.assertEqual(r.reconcile(h.research,h.evidence,h.new,h.cfg,**proof)['status'],'RECORDED')
            self.assertEqual((dump(h.new),dump(h.research)),before)
            self.assertEqual(r.gate(h.store,h.research,(target.scan_id,)),'REJECTED_SCAN_RETIRED')
        # Keep the policy alive for the subsequent actual wrapper and held cycle.
        p=patch.object(r,'POLICY',policy);p.start();self.addCleanup(p.stop)
        retired=paper_cycle.run_once(h.research,h.evidence,h.new,h.cfg,
            candidates=(replace(op.f.item,target=target),),dependency_blockers=())
        self.assertEqual(retired['blockers'],['REJECTED_SCAN_RETIRED'])
        self.assertEqual(retired['attempted_requests'],0)
        with patch.object(operator_fixture.tool.cli,'_credentials',side_effect=AssertionError('retired scan credentials')):
            prior=copy.deepcopy(op.row);op.row.update(vars(target));op.save()
            try:
                with self.assertRaises(ValueError):op.invoke(live=True,systemd_credentials=True)
            finally:op.row=prior;op.save()
        self.assertEqual(len(op.f.http_calls),calls)
        self._assert_retired_originals(h,rejected)

    def _assert_retired_originals(self, h, rejected):
        with h.store.connect() as c:
            self.assertEqual(c.execute('SELECT id,intent_hash,outcome_hash FROM paper_observation_passes WHERE id=?',(rejected['row'][0],)).fetchone(),rejected['row'])
        self.assertEqual(h.f.context.progress.admission(rejected['target'].scan_id),rejected['admission'])
        self.assertEqual(rejected['admission']['requests_used'],3)
        for key,original in rejected['originals'].items():self.assertEqual(h.store.load(key),original)
