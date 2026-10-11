"""SYNTHETIC_TEST_ONLY. Actual persisted jobs and mocked HTTPS; no live reads."""
import base64
import copy
from dataclasses import asdict
import io
import json
import fcntl
from pathlib import Path
import subprocess
import sys
import sqlite3
from unittest.mock import patch
import unittest

from desk import paper_observe_cli as cli
from desk import paper_read_sources as transport
from desk.model import canonical
from desk.providers import SOL
from tests import test_paper_observation_collector as fixtures
from tests.test_paper_read_sources import Response


class ObservationCliTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.PaperObservationCollectorTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.position=self.f.target(1234567);self.candidate=self.f.second_target(self.position)
        self.requests=[];self.price_raw=('{ "'+SOL+'": {"usdPrice":100.00,"decimals":9,"blockId":100}}\n').encode()
        self.block_time=self.f.at-5;self.transport_fail=False;self.clock_values=None
    def invoke(self, *, positions=(), candidates=None, sol_usd=False, via_cli=False):
        outer=self;f=self.f
        class Opener:
            def open(self,request,*,timeout):
                outer.requests.append(request)
                if outer.transport_fail == 'interrupt':raise KeyboardInterrupt()
                if outer.transport_fail:raise OSError('SECRET_do_not_print')
                outer.assertGreater(timeout,0);outer.assertLessEqual(timeout,10)
                if request.method=='POST':
                    call=json.loads(request.data);method=call['method'];params=call['params']
                    if method=='getSlot':result=110
                    elif method=='getBlockTime':result=outer.block_time
                    elif method=='getAccountInfo' and params[0]==outer.position.mint:
                        result={'context':{'slot':100},'value':copy.deepcopy(f.protocol.rpc('getMultipleAccounts',[])['value'][6])}
                    else:result=copy.deepcopy(f.protocol.rpc(method,params))
                    raw=canonical({'jsonrpc':'2.0','id':transport.RPC_ID,'result':result}).encode()
                elif '/price/v3?' in request.full_url:raw=outer.price_raw
                else:
                    from urllib.parse import parse_qs,urlsplit
                    q={k:v[0] for k,v in parse_qs(urlsplit(request.full_url).query).items()}
                    raw=canonical({'inputMint':q['inputMint'],'outputMint':q['outputMint'],'inAmount':q['amount'],
                        'outAmount':'2000000','otherAmountThreshold':'1999999','swapMode':'ExactIn','slippageBps':100,
                        'routePlan':[{'percent':100,'swapInfo':{'ammKey':outer.position.pool,'inputMint':q['inputMint'],
                            'outputMint':q['outputMint'],'inAmount':q['amount'],'outAmount':'2000000'}}]}).encode()
                return Response(raw)
        candidates=(self.candidate,) if candidates is None else candidates
        with patch.object(transport.os.environ,'get',return_value='SYNTHETIC_TEST_ONLY'),patch.object(transport,'build_opener',return_value=Opener()),patch.object(cli.time,'time',return_value=f.at),patch.object(cli.time,'monotonic',return_value=1):
            if not via_cli:return cli.observe(f.jobs.path,f.progress.store.path,open_positions=positions,candidates=candidates,sol_usd=sol_usd)
            path=Path(f.tmp.name)/'targets.json'
            path.write_text(json.dumps({'open_positions':[asdict(t) for t in positions],'candidates':[asdict(t) for t in candidates]}))
            args=['--research-db',str(f.jobs.path),'--evidence-db',str(f.progress.store.path),'--targets',str(path)]
            if sol_usd:args.append('--sol-usd')
            stdout=io.StringIO()
            with patch('sys.stdout',stdout):code=cli.main(args)
            return code,json.loads(stdout.getvalue())
    def test_actual_cli_collects_seven_charged_reads_original_bytes_no_entries(self):
        original=self.f.jobs.source(self.position.scan_id)
        code,result=self.invoke(candidates=(self.position,),sol_usd=True,via_cli=True)
        self.assertEqual(code,0);self.assertEqual(result['status'],'OBSERVED')
        self.assertEqual(result['attempted_requests'],7);self.assertEqual(len(self.requests),7)
        self.assertEqual(self.f.progress.admission(self.position.scan_id)['requests_used'],7)
        self.assertEqual(result['sol_usd']['usd_price'],'100.00')
        self.assertEqual(result['sol_usd']['price_at'],self.block_time)
        self.assertFalse(result['entries_enabled']);self.assertFalse(result['eligible_for_trading']);self.assertFalse(result['quote_is_fill'])
        self.assertEqual(self.f.jobs.source(self.position.scan_id),original)
        raw=self.f.progress.store.load(result['sol_usd']['evidence_refs'][0])
        self.assertEqual(base64.b64decode(raw['response_bytes_base64']),self.price_raw)
        saved=self.f.progress.store.load(result['evidence_hash'])
        self.assertEqual(saved,{k:v for k,v in result.items() if k!='evidence_hash'})
    def test_position_sell_precedes_buy_and_usd(self):
        r=self.invoke(positions=(self.position,),sol_usd=True)
        self.assertEqual(r['attempted_requests'],11)
        self.assertEqual([o['direction'] for o in r['observations']],['sell','buy'])
        self.assertIn('inputMint='+self.position.mint,self.requests[3].full_url)
        self.assertIn('amount=1234567',self.requests[3].full_url)
        self.assertIn('inputMint='+SOL,self.requests[7].full_url)
        self.assertIn('/price/v3?',self.requests[8].full_url)
    def test_exhausted_position_blocks_candidate_and_usd_without_reset(self):
        for _ in range(18):self.f.progress.reserve(self.position.scan_id)
        r=self.invoke(positions=(self.position,),sol_usd=True)
        self.assertEqual(r['status'],'UNKNOWN');self.assertEqual(r['attempted_requests'],0)
        self.assertEqual(r['stopped_reason'],'SHARED_REQUEST_BUDGET_EXHAUSTED')
        self.assertEqual(self.requests,[]);self.assertEqual(self.f.progress.admission(self.position.scan_id)['requests_used'],18)
        self.assertEqual(self.f.progress.admission(self.candidate.scan_id)['requests_used'],0)
    def test_quote_failure_stops_and_retains_charged_original_without_usd(self):
        self.transport_fail=True
        code,r=self.invoke(positions=(self.position,),sol_usd=True,via_cli=True)
        self.assertEqual(code,2);self.assertEqual(r['stopped_reason'],'SOURCE_REQUEST_FAILED')
        self.assertEqual(len(self.requests),1);self.assertEqual(r['attempted_requests'],1)
        self.assertNotIn('SECRET',json.dumps(r))
        self.assertEqual(self.f.progress.admission(self.position.scan_id)['requests_used'],1)
    def test_failed_read_latches_restart_and_different_targets_without_new_charge(self):
        self.transport_fail=True
        first=self.invoke()
        self.assertEqual(first['stopped_reason'],'SOURCE_REQUEST_FAILED')
        original=self.f.progress.store.load(first['evidence_hash'])
        self.transport_fail=False;self.requests.clear()
        for target in (self.candidate,self.position):
            code,result=self.invoke(candidates=(target,),via_cli=True)
            self.assertEqual(code,2)
            self.assertEqual(result['stopped_reason'],'OBSERVATION_RECOVERY_REQUIRED')
            self.assertEqual(result['attempted_requests'],0)
        self.assertEqual(self.requests,[])
        self.assertEqual(self.f.progress.admission(self.candidate.scan_id)['requests_used'],1)
        self.assertEqual(self.f.progress.store.load(first['evidence_hash']),original)

    def test_interrupted_charged_read_latches_without_published_outcome(self):
        self.transport_fail='interrupt'
        with self.assertRaises(KeyboardInterrupt):self.invoke()
        self.assertEqual(self.f.progress.admission(self.candidate.scan_id)['requests_used'],1)
        self.transport_fail=False;self.requests.clear()
        result=self.invoke()
        self.assertEqual(result['stopped_reason'],'OBSERVATION_RECOVERY_REQUIRED')
        self.assertEqual(self.requests,[])
        self.assertEqual(self.f.progress.admission(self.candidate.scan_id)['requests_used'],1)

    def test_invalid_existing_databases_unchanged_before_admission_rejection(self):
        root=Path(self.f.tmp.name)
        paths=[root/'invalid-research.sqlite',root/'invalid-evidence.sqlite']
        for path in paths:
            with sqlite3.connect(path) as c:c.execute('CREATE TABLE original(value TEXT)')
        before=[path.read_bytes() for path in paths]
        result=cli.observe(*paths,candidates=(self.candidate,))
        self.assertEqual(result['stopped_reason'],'PERSISTED_ADMISSION_REQUIRED')
        self.assertEqual([path.read_bytes() for path in paths],before)
        self.assertEqual(self.requests,[])

    def test_missing_admission_keeps_all_original_tables_and_pages_unchanged(self):
        with self.f.progress.store.connect() as c:
            c.execute('DELETE FROM ownership_admissions WHERE id=?',(self.candidate.scan_id,))
            before=list(c.iterdump())
        result=self.invoke()
        with self.f.progress.store.connect() as c:self.assertEqual(list(c.iterdump()),before)
        self.assertEqual(result['stopped_reason'],'PERSISTED_ADMISSION_REQUIRED')
        self.assertEqual(self.requests,[])

    def test_output_persistence_interruption_remains_latched(self):
        original=cli.EvidenceStore.save
        def save(store,payload):
            if payload.get('kind')=='paper_observation_pass_v1':raise OSError('fixture disk failure')
            return original(store,payload)
        with patch.object(cli.EvidenceStore,'save',save):
            with self.assertRaises(OSError):self.invoke()
        self.requests.clear()
        result=self.invoke()
        self.assertEqual(result['stopped_reason'],'OBSERVATION_RECOVERY_REQUIRED')
        self.assertEqual(self.requests,[])
        self.assertEqual(self.f.progress.admission(self.candidate.scan_id)['requests_used'],4)

    def test_usd_stale_never_healthy_or_numeric_default(self):
        self.block_time=self.f.at-31
        r=self.invoke(sol_usd=True)
        self.assertEqual(r['status'],'UNKNOWN');self.assertIsNone(r['sol_usd']['usd_price'])
        self.assertIn('PRICE_BLOCK_STALE_OR_FUTURE',r['sol_usd']['blockers'])
    def test_usd_missing_never_pegged(self):
        self.price_raw=b'{}';r=self.invoke(sol_usd=True)
        self.assertEqual(r['status'],'UNKNOWN');self.assertIsNone(r['sol_usd']['usd_price'])
        self.assertIn('SOL_PRICE_MISSING',r['sol_usd']['blockers'])
    def test_usd_null_never_numeric_default(self):
        self.price_raw=('{"'+SOL+'":null}').encode();r=self.invoke(sol_usd=True)
        self.assertEqual(r['status'],'UNKNOWN');self.assertIsNone(r['sol_usd']['usd_price'])
        self.assertIn('SOL_PRICE_MISSING',r['sol_usd']['blockers'])
    def test_price_64k_limit_failure_account_quote_2m_limit_explicit(self):
        self.price_raw=b' ' * 65537
        r=self.invoke(sol_usd=True)
        self.assertEqual(r['attempted_requests'],5);self.assertEqual(len(self.requests),5)
        self.assertEqual(r['sol_usd']['blockers'],['RESPONSE_OVERSIZED'])
        self.assertEqual(r['limits']['price_bytes'],65536)
        self.assertEqual(r['limits']['account_quote_bytes'],2097152)
    def test_null_exact_block_time_unknown_and_all_three_witnesses_retained(self):
        self.block_time=None;r=self.invoke(sol_usd=True)
        self.assertEqual(r['status'],'UNKNOWN');self.assertEqual(len(r['sol_usd']['evidence_refs']),3)
        self.assertEqual(r['attempted_requests'],7)
    def test_missing_admission_never_recreates_counter_or_reads(self):
        with self.f.progress.store.connect() as c:c.execute('DELETE FROM ownership_admissions WHERE id=?',(self.candidate.scan_id,))
        r=self.invoke(sol_usd=True)
        self.assertEqual(r['stopped_reason'],'PERSISTED_ADMISSION_REQUIRED');self.assertEqual(self.requests,[])
        self.assertIsNone(self.f.progress.admission(self.candidate.scan_id))
    def test_empty_and_missing_databases_no_creation(self):
        r=self.invoke(candidates=())
        self.assertEqual(r['status'],'UNKNOWN');self.assertEqual(self.requests,[])
        path=Path(self.f.tmp.name)/'absent.sqlite'
        with self.assertRaises(ValueError):cli.observe(path,self.f.progress.store.path)
        self.assertFalse(path.exists())
    def test_worker_busy_zero_reads_and_no_admission_mutation(self):
        with self.f.jobs.worker() as worker:
            r=self.invoke()
        self.assertEqual(r['stopped_reason'],'RESEARCH_WORKER_BUSY');self.assertEqual(self.requests,[])
        self.assertEqual(self.f.progress.admission(self.candidate.scan_id)['requests_used'],0)
    def test_evidence_invocation_busy_zero_reads(self):
        path=cli.ownership_lock_path(self.f.progress.store,invocation=True)
        with open(path,'a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            r=self.invoke()
        self.assertEqual(r['stopped_reason'],'EVIDENCE_INVOCATION_BUSY');self.assertEqual(self.requests,[])
    def test_total_pass_limit_also_bounds_usd_witness_reads(self):
        r=self.invoke(candidates=(self.candidate,)*4,sol_usd=True)
        self.assertEqual(r['attempted_requests'],18);self.assertEqual(len(self.requests),18)
        self.assertEqual(self.f.progress.admission(self.candidate.scan_id)['requests_used'],18)
        self.assertEqual(r['status'],'UNKNOWN');self.assertEqual(len(r['sol_usd']['evidence_refs']),2)
        self.assertFalse(r['eligible_for_trading'])
    def test_restart_uses_remaining_counter_and_preserves_completed_source(self):
        original=self.f.jobs.source(self.position.scan_id)
        first=self.invoke(candidates=(self.position,),sol_usd=True)
        second=self.invoke(candidates=(self.position,),sol_usd=True)
        self.assertEqual(first['status'],'OBSERVED');self.assertEqual(second['status'],'OBSERVED')
        self.assertEqual(self.f.progress.admission(self.position.scan_id)['requests_used'],14)
        self.assertEqual(self.f.jobs.source(self.position.scan_id),original)
        third=self.invoke(candidates=(self.position,),sol_usd=True)
        self.assertEqual(third['status'],'UNKNOWN');self.assertEqual(third['attempted_requests'],4)
        self.assertEqual(self.f.progress.admission(self.position.scan_id)['requests_used'],18)
    def test_module_cli_missing_db_reports_unknown_without_creating_files(self):
        root=Path(self.f.tmp.name);targets=root/'empty-targets.json'
        targets.write_text('{"open_positions":[],"candidates":[]}')
        absent=root/'absent.sqlite'
        result=subprocess.run([sys.executable,'-m','desk.paper_observe_cli','--research-db',str(absent),
            '--evidence-db',str(self.f.progress.store.path),'--targets',str(targets)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,2);self.assertEqual(json.loads(result.stdout)['status'],'UNKNOWN')
        self.assertEqual(result.stderr,'');self.assertFalse(absent.exists())
    def test_target_file_bounds_unknown_keys_and_no_permission_flags(self):
        path=Path(self.f.tmp.name)/'targets.json'
        for body in (b'x'*65537,b'null',b'{"open_positions":[],"candidates":[],"entries_enabled":true}',b'{"open_positions":[],"candidates":[{}]}'):
            path.write_bytes(body)
            with self.assertRaises(ValueError):cli.load_targets(path)


if __name__=='__main__':unittest.main()
