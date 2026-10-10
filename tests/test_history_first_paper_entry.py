"""SYNTHETIC_TEST_ONLY; normal clocks, fixture HTTP, no secrets/network."""
import copy
import contextlib
import io
from dataclasses import replace
import inspect
import json
from pathlib import Path
import sqlite3
import textwrap
import time
import unittest
from unittest.mock import patch
from tools import history_first_paper_entry as tool
from desk import provider_pacing as pacing, paper_read_sources as transport
from desk.model import canonical
from desk.security import base58
from desk.programs import unbase58
from desk.providers import SOL
from tests import test_token2022_paper as vertical, test_paper_cycle as fixtures
from tests.test_paper_read_sources import Response


class HistoryFirstTests(unittest.TestCase):
    def setUp(self):
        self.v=vertical.VerticalProfileTests();self.v.setUp();self.addCleanup(self.v.doCleanups)
        self.f=self.v.f;self.root=Path(self.f.f.tmp.name)
        self.config=self.root/'config.json';self.config.write_text(json.dumps(self.f.cfg))
        self.targets=self.root/'targets.json'
        row=vars(self.f.target).copy()
        row.update(provenance=self.f.item.provenance,pool_fee_bps='25',
                   graduation_refs=list(self.f.item.graduation_refs),known_hazards=[])
        self.row=row;self.save()
        self.pace=self.root/'pacing.sqlite';pacing.initialize(self.pace)
        self.env=patch.dict('os.environ',{pacing.ENV:str(self.pace)});self.env.start();self.addCleanup(self.env.stop)
    def save(self):self.targets.write_text(json.dumps({'position_targets':[],'candidates':[self.row],'usd_evidence_refs':[]}))
    def invoke(self,**kw):
        return tool.execute(self.config,self.f.f.jobs.path,self.f.f.progress.store.path,self.f.path,self.targets,**kw)
    def test_dry_run_no_credentials_no_history_or_provider_spend(self):
        before=self.f.f.progress.admission(self.f.target.scan_id)
        with (patch.object(tool.cli,'_credentials',side_effect=AssertionError('secret')),
              patch.object(tool.cycle,'_history',side_effect=AssertionError('history'))):
            self.assertEqual(self.invoke()['status'],'DRY_RUN')
        self.assertEqual(before,self.f.f.progress.admission(self.f.target.scan_id))
    def test_wrong_config_runtime_checkpoint_pending_refuse_before_credentials_history(self):
        for kind in ('config','runtime','checkpoint','pending'):
            with self.subTest(kind=kind):
                with sqlite3.connect(self.f.path) as c: backup='\n'.join(c.iterdump())
                if kind=='config':
                    cfg=copy.deepcopy(self.f.cfg);cfg['price_ttl_seconds']=9;self.config.write_text(json.dumps(cfg))
                elif kind=='runtime':
                    with sqlite3.connect(self.f.path) as c:c.execute("UPDATE metadata SET value='bad' WHERE key='implementation_hash'")
                elif kind=='checkpoint':
                    with sqlite3.connect(self.f.path) as c:c.execute('DELETE FROM state')
                else:
                    with self.f.f.progress.store.connect() as c:
                        c.execute('CREATE TABLE paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
                        c.execute("INSERT INTO paper_observation_passes VALUES('pending','original',NULL)")
                with patch.object(tool.cli,'_credentials',side_effect=AssertionError('secret')), patch.object(tool.cycle,'_history',side_effect=AssertionError('history')):
                    with self.assertRaises(ValueError):self.invoke()
                self.config.write_text(json.dumps(self.f.cfg))
                if kind!='pending':
                    self.f.path.unlink()
                    with sqlite3.connect(self.f.path) as c:c.executescript(backup)
    def test_pacer_required_and_live_synthetic_refused_before_credentials(self):
        with patch.dict('os.environ',{},clear=True),patch.object(tool.cli,'_credentials',side_effect=AssertionError('secret')):
            with self.assertRaises(ValueError):self.invoke()
        with patch.object(tool.cli,'_credentials',side_effect=AssertionError('secret')):
            with self.assertRaises(ValueError):self.invoke(live=True,systemd_credentials=True)
    def test_wrong_ledger_and_open_positions_refuse_before_provider(self):
        with patch.object(tool.cli,'_credentials',side_effect=AssertionError('secret')), patch.object(tool.cycle,'_history',side_effect=AssertionError('history')):
            with self.assertRaises(ValueError):
                tool.execute(self.config,self.f.f.jobs.path,self.f.f.progress.store.path,self.f.f.jobs.path,self.targets)
            original=tool.cycle._state
            def held(*args):
                state=original(*args);return {**state,'positions':{'already-held':{}}}
            with patch.object(tool.cycle,'_state',side_effect=held):
                with self.assertRaises(ValueError):self.invoke()
    def test_charged_preparation_failure_is_closed_and_the_candidate_is_not_retried_across_restart(self):
        # T22: was ..._latched_across_restart (a NULL pass blocked EVERY later candidate). The failed pass is now
        # closed FAILED_CHARGED: the charge stays, the same scan is retired (refused before credentials or history
        # reads), and the store is no longer latched for other candidates.
        self.row['provenance']='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE';self.save()
        def failed(progress,item,*args):
            progress.reserve(item.target.scan_id)
            # T22G: a classified transient fault. The earlier free-text ValueError is not on the allow-list and now
            # holds the pass (tests/test_pass_closure_allowlist.py pins that).
            raise TimeoutError('ambiguous synthetic failure')
        with patch.object(tool.cli,'_credentials'),patch.object(tool.cycle,'_history',side_effect=failed):
            with self.assertRaises(TimeoutError):self.invoke(live=True,systemd_credentials=True)
        self.assertEqual(self.f.f.progress.admission(self.f.target.scan_id)['requests_used'],1)
        with patch.object(tool.cli,'_credentials',side_effect=AssertionError('secret')),patch.object(tool.cycle,'_history',side_effect=AssertionError('retry')):
            with self.assertRaises(ValueError):self.invoke(live=True,systemd_credentials=True)
        self.assertEqual(self.f.f.progress.admission(self.f.target.scan_id)['requests_used'],1)
        with self.f.f.progress.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM paper_pass_closures WHERE retired_scan IS NOT NULL').fetchone()[0],1)

    def test_live_known_hazard_no_credentials_history_charge_or_pending(self):
        self.row.update(provenance='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE',known_hazards=['KNOWN_TOKEN_HAZARD']);self.save()
        before=self.f.f.progress.admission(self.f.target.scan_id)
        with patch.object(tool.cli,'_credentials',side_effect=AssertionError('secret')),patch.object(tool.cycle,'_history',side_effect=AssertionError('history')):
            with self.assertRaises(ValueError):self.invoke(live=True,systemd_credentials=True)
        self.assertEqual(before,self.f.f.progress.admission(self.f.target.scan_id))
        with self.f.f.progress.store.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='paper_observation_passes'").fetchone())
    def test_actual_main_admission_mismatch_is_redacted_before_io(self):
        self.row.update(provenance='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE',mint=self.row['taker']);self.save()
        before=self.f.f.progress.admission(self.f.target.scan_id)
        args=['--config',str(self.config),'--research-db',str(self.f.f.jobs.path),
              '--evidence-db',str(self.f.f.progress.store.path),'--ledger-db',str(self.f.path),
              '--targets',str(self.targets),'--execute','--systemd-credentials']
        out=io.StringIO()
        with contextlib.redirect_stdout(out),patch.object(tool.cli,'_credentials',side_effect=AssertionError('secret')),patch.object(tool.cycle,'_history',side_effect=AssertionError('history')):
            self.assertEqual(tool.main(args),2)
        result=json.loads(out.getvalue());self.assertEqual(result['status'],'BLOCKED')
        self.assertEqual(result['blockers'],['HISTORY_FIRST_PREFLIGHT_OR_ACQUISITION_UNAVAILABLE'])
        self.assertEqual(before,self.f.f.progress.admission(self.f.target.scan_id))
        with self.f.f.progress.store.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='paper_observation_passes'").fetchone())

    def test_normal_clock_mock_http_history_then_usd_late_buy(self):
        f=self.f;f.f.at=int(time.time());f.http_calls=[];f.sell_output=10_000_000
        # Construct NEW synthetic migration evidence at current fixture time;
        # no existing evidence row is rewritten or relabelled.
        manifest=f.f.progress.store.load(f.item.graduation_refs[0]);response=copy.deepcopy(f.f.progress.store.load(manifest['response_hash']))
        raw=response['data'][0];raw['blockTime']=f.f.at-600
        ix=raw['meta']['innerInstructions'][0]['instructions'][0]
        b=bytearray(unbase58(ix['data']));b[136:144]=raw['blockTime'].to_bytes(8,'little',signed=True);ix['data']=base58(b)
        key=f.f.progress.store.save(response);manifest={**manifest,'response_hash':key};ref=f.f.progress.store.save(manifest)
        # PUBLIC label exercises operator grammar only; all raw bytes remain synthetic.
        self.row.update(provenance='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE',graduation_refs=[ref]);self.save()
        code=textwrap.dedent(inspect.getsource(f.actual_cycle));a=code.index('    class Opener:');b=code.index('    with ExitStack()')
        ns={**vars(fixtures),'outer':f};exec(textwrap.dedent(code[a:b]),ns);delegate=ns['Opener']()
        class HTTP:
            price_at = None
            def open(self,request,*,timeout):
                if '/price/v3?' in request.full_url:
                    self.price_at=int(time.time())-1
                    return Response(canonical({SOL:{'usdPrice':100,'blockId':100,'decimals':9}}).encode())
                if request.method=='POST' and json.loads(request.data)['method']=='getBlockTime':
                    return Response(canonical({'jsonrpc':'2.0','id':transport.RPC_ID,'result':self.price_at}).encode())
                return delegate.open(request,timeout=timeout)
        with patch.object(tool.cli,'_credentials'),patch.dict('os.environ',{'HELIUS_API_KEY':'SYNTHETIC_TEST_ONLY','JUPITER_API_KEY':'SYNTHETIC_TEST_ONLY'}),patch.object(transport,'build_opener',return_value=HTTP()):
            result=self.invoke(live=True,systemd_credentials=True)
        self.assertEqual(result['status'],'COMPLETE',result.get('diagnostics'))
        self.assertTrue(any(r.get('side')=='buy' for r in result['outcomes']))
        self.assertEqual(self.f.f.progress.admission(f.target.scan_id)['requests_used'],9)
        self.assertEqual(result['usd_evidence_refs'].__len__(),3)


if __name__=='__main__':unittest.main()
