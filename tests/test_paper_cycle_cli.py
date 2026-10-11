"""SYNTHETIC_TEST_ONLY manual invocation; no provider or real credentials."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from desk import paper_cycle_cli as cli
from tests import test_paper_cycle as cycle_fixtures
from tests.helpers import config
from desk.providers import SOL


class PaperCycleCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.cfg={**config(),'paper_signal_policy_version':3,'paper_quote_execution_version':1}
        self.config_path=self.root/'config.json';self.config_path.write_text(json.dumps(self.cfg))
        self.target_path=self.root/'targets.json'
        self.value={'position_targets':[],'candidates':[],'usd_evidence_refs':[]}
        self.save()
    def save(self):self.target_path.write_text(json.dumps(self.value))
    def invoke(self,*args):
        out=io.StringIO()
        with contextlib.redirect_stdout(out):code=cli.main(['--config',str(self.config_path),*args])
        return code,json.loads(out.getvalue())
    def args(self):return ['once','--research-db',str(self.root/'research.sqlite'),
        '--evidence-db',str(self.root/'evidence.sqlite'),'--ledger-db',str(self.root/'paper.sqlite'),
        '--targets',str(self.target_path)]
    def row(self):return {'scan_id':'existing-scan','mint':SOL,'pool':SOL,'taker':SOL,'amount_raw':1,
                         'provenance':'SYNTHETIC_TEST_ONLY','pool_fee_bps':'25','graduation_refs':[],'known_hazards':[]}

    def test_actual_initialize_is_exclusive_and_never_loads_credentials(self):
        ledger=self.root/'new.sqlite'
        with patch.object(cli,'_credentials',side_effect=AssertionError('credential read')):
            code,result=self.invoke('init','--ledger-db',str(ledger))
            self.assertEqual(code,0);self.assertEqual(result['status'],'INITIALIZED')
            original=ledger.read_bytes()
            code,result=self.invoke('init','--ledger-db',str(ledger))
            self.assertEqual(code,2);self.assertEqual(ledger.read_bytes(),original)

    def test_actual_pending_dependency_refuses_before_credentials_databases_network(self):
        with (patch.object(cli,'_credentials',side_effect=AssertionError('credential read')),
              patch('socket.socket',side_effect=AssertionError('network'))):
            code,result=self.invoke(*self.args(),'--systemd-credentials','--dependency-blocker','PENDING_REVIEW')
        self.assertEqual(code,2);self.assertEqual(result['blockers'],['PENDING_REVIEW'])
        self.assertEqual(result['attempted_requests'],0)
        self.assertFalse(list(self.root.glob('*.sqlite')))

    def test_raw_authorization_flags_times_overrides_and_malformed_targets_refuse(self):
        for key in ('graduated_at','holder_at','safe','entry_authorized','history_as_of'):
            with self.subTest(key=key):
                row=self.row();row[key]=0;self.value['candidates']=[row];self.save()
                with patch.object(cli.cycle,'run_once',side_effect=AssertionError('called')):
                    code,_=self.invoke(*self.args())
                self.assertEqual(code,2)
        for amount in (True,0,-1,1.5,2**64,'1'):
            row=self.row();row['amount_raw']=amount;self.value['candidates']=[row];self.save()
            with self.assertRaises(ValueError):cli.load_targets(self.target_path)
        self.value['candidates']=[self.row()]*19;self.save()
        with self.assertRaises(ValueError):cli.load_targets(self.target_path)
        self.target_path.write_text('{"candidates":[],"candidates":[]}')
        with self.assertRaises(ValueError):cli.load_targets(self.target_path)
        self.target_path.write_text(' '*65537)
        with self.assertRaises(ValueError):cli.load_targets(self.target_path)

    def test_actual_cycle_call_retains_historical_refs_and_unknown_holder(self):
        f=cycle_fixtures.PaperCycleTests();self.addCleanup(f.doCleanups);f.setUp()
        item=f.item;target=item.target
        row={key:getattr(target,key) for key in ('scan_id','mint','pool','taker','amount_raw')}
        row.update(provenance=item.provenance,pool_fee_bps=item.pool_fee_bps,graduation_refs=list(item.graduation_refs),known_hazards=list(item.known_hazards))
        self.value['candidates']=[row];self.save()
        seen=[];actual=cli.cycle.run_once;f.http_calls=[];f.sell_output=10_000_000
        def bound(*args,**kwargs):
            seen.append(kwargs['candidates'][0])
            f.run_cycle=lambda **injected:actual(*args,**{**kwargs,**injected,'wall_clock':lambda:f.f.at,'monotonic':lambda:1})
            return f.actual_cycle(positions=kwargs['position_targets'],candidates=kwargs['candidates'],
                                  usd_refs=kwargs['usd_evidence_refs'])
        with patch.object(cli.cycle,'run_once',side_effect=bound):
            code,result=self.invoke('once','--research-db',str(f.f.jobs.path),
              '--evidence-db',str(f.f.progress.store.path),'--ledger-db',str(f.path),'--targets',str(self.target_path))
        self.assertEqual(code,0);self.assertEqual(result['status'],'COMPLETE');self.assertEqual(result['execution_status'],'EXECUTION_UNVERIFIED')
        self.assertFalse(result['live_readiness']);self.assertIsNone(seen[0].graduated_at)
        self.assertIsNone(seen[0].holder_at);self.assertEqual(seen[0].graduation_refs,item.graduation_refs)
        self.assertTrue(any(row.get('side')=='buy' for row in result['outcomes']))
        self.assertNotIn('original_json',json.dumps(result));self.assertNotIn('token_evidence',json.dumps(result))
        self.assertLessEqual(result['attempted_requests'],18)

    def test_systemd_credentials_permissions_bounds_and_no_error_leak(self):
        credential=self.root/'provider-keys.json'
        credential.write_text(json.dumps({'HELIUS_API_KEY':'synthetic-one','JUPITER_API_KEY':'synthetic-two'}))
        credential.chmod(0o440)
        with patch.dict(os.environ,{'CREDENTIALS_DIRECTORY':str(self.root)}):
            cli._credentials()
            self.assertEqual(os.environ['HELIUS_API_KEY'],'synthetic-one')
            credential.chmod(0o644)
            with self.assertRaises(ValueError):cli._credentials()
            credential.write_text('synthetic-secret-bad-json');credential.chmod(0o400)
            code,result=self.invoke(*self.args(),'--systemd-credentials')
            self.assertEqual(code,2);self.assertNotIn('synthetic-secret',json.dumps(result))

    def test_adverse_labels_and_control_are_additive_not_event_overrides(self):
        row=self.row();row['known_hazards']=['KNOWN_MINT_HAZARD'];self.value['candidates']=[row];self.save()
        result={'kind':'paper_cycle_v1','status':'BLOCKED','execution_status':'EXECUTION_UNVERIFIED',
                'live_readiness':False,'blockers':['KNOWN_MINT_HAZARD']}
        with patch.object(cli.cycle,'run_once',return_value=result) as call,patch.object(cli.time,'time',return_value=1000):
            code,_=self.invoke(*self.args(),'--control','EXIT_ONLY')
        self.assertEqual(code,2)
        self.assertEqual(call.call_args.kwargs['candidates'][0].known_hazards,('KNOWN_MINT_HAZARD',))
        control=call.call_args.kwargs['controls'][0]
        self.assertEqual((control['command'],control['ts'],control['actor']),('EXIT_ONLY',1000,'operator'))
        self.assertEqual(set(control),{'schema_version','kind','event_id','ts','actor','command'})

    def test_usable_module_pending_review_exit(self):
        result=subprocess.run([os.sys.executable,'-m','desk.paper_cycle_cli','--config',str(self.config_path),
            *self.args(),'--dependency-blocker','PENDING_REVIEW'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2);self.assertEqual(json.loads(result.stdout)['blockers'],['PENDING_REVIEW'])
        self.assertEqual(result.stderr,'')


if __name__=='__main__':unittest.main()
