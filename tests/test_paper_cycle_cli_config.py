"""Actual manual CLI config rejection before credentials/DB/network effects."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from desk import paper_cycle_cli as cli
from desk.model import load_config
from tests.helpers import config


class ConfigBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'config.json'
        self.cfg={**config(),'paper_signal_policy_version':3,'paper_quote_execution_version':1}
        self.path.write_text(json.dumps(self.cfg))
        self.target=self.root/'targets.json'
        self.target.write_text(json.dumps({'position_targets':[],'candidates':[],'usd_evidence_refs':[]}))
        self.credentials=self.root/'credentials';self.credentials.mkdir()
        (self.credentials/'provider-keys.json').write_text('SYNTHETIC_NEVER_READ_CREDENTIAL')
        self.paths=[self.root/x for x in ('research.sqlite','evidence.sqlite','ledger.sqlite')]
        for path in self.paths:path.write_bytes(b'ORIGINAL_RECORDS_NOT_A_DATABASE')
        self.before={str(p):p.read_bytes() for p in self.paths}

    def args(self):
        return ['--config',str(self.path),'once','--research-db',str(self.paths[0]),
            '--evidence-db',str(self.paths[1]),'--ledger-db',str(self.paths[2]),
            '--targets',str(self.target),'--systemd-credentials']

    def invalid(self):
        duplicate='{"paper_signal_policy_version":2,'+json.dumps(self.cfg)[1:]
        same='{"paper_signal_policy_version":3,'+json.dumps(self.cfg)[1:]
        return [b'[]',b'null',b'"SYNTHETIC_CONFIG_SECRET"',b'true',b'1',
            duplicate.encode(),same.encode(),b'{"x":{"policy":1,"policy":2}}',
            b'{"x":NaN}',b'{"x":Infinity}',b'\xff',b'['*2000+b']'*2000,
            (json.dumps(self.cfg)+' '*(65537-len(json.dumps(self.cfg)))).encode()]

    def test_main_shapes_duplicates_and_bound_refuse_before_credentials_or_cycle(self):
        for raw in self.invalid():
            self.path.write_bytes(raw);out=io.StringIO()
            with (self.subTest(prefix=raw[:30]),contextlib.redirect_stdout(out),
                patch.object(cli,'_credentials',side_effect=AssertionError('credential read')),
                patch.object(cli.cycle,'run_once',side_effect=AssertionError('database cycle')),
                patch.object(cli.cycle,'initialize',side_effect=AssertionError('database initialization'))):
                self.assertEqual(cli.main(self.args()),2)
            result=json.loads(out.getvalue());self.assertEqual(result['status'],'UNAVAILABLE')
            self.assertFalse(result['live_readiness'])
            self.assertNotIn('SYNTHETIC_CONFIG_SECRET',out.getvalue())
            self.assertEqual({str(p):p.read_bytes() for p in self.paths},self.before)

    def test_actual_module_invalid_input_has_no_traceback_or_credential_db_network_read(self):
        # Audit the real module in a subprocess: no substituted CLI, validator,
        # cycle or credentials function. Forbidden access becomes an uncaught
        # assertion, distinguishing rejection from a swallowed read failure.
        program='''import os,runpy,sys
def audit(event,args):
    if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
        path=os.fsdecode(args[0])
        if path.startswith(os.environ['CREDENTIALS_DIRECTORY']) or path.endswith('.sqlite'):
            raise AssertionError('forbidden credential or database read')
    if event in ('sqlite3.connect','socket.connect'):
        raise AssertionError('forbidden database or network action')
sys.addaudithook(audit)
sys.argv=['desk.paper_cycle_cli']+sys.argv[1:]
runpy.run_module('desk.paper_cycle_cli',run_name='__main__')
'''
        for raw in self.invalid():
            self.path.write_bytes(raw)
            with self.subTest(prefix=raw[:30]):
                result=subprocess.run([sys.executable,'-c',program,*self.args()],
                    env={**os.environ,'CREDENTIALS_DIRECTORY':str(self.credentials)},
                    capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,2,result.stderr)
                self.assertEqual(result.stderr,'')
                self.assertEqual(json.loads(result.stdout)['status'],'UNAVAILABLE')
                self.assertNotIn('SYNTHETIC_',result.stdout)
                self.assertEqual({str(p):p.read_bytes() for p in self.paths},self.before)

    def test_exact_byte_limit_accepts_valid_config_next_byte_refuses_without_creation(self):
        text=json.dumps(self.cfg);self.path.write_text(text+' '*(65536-len(text)))
        ledger=self.root/'new.sqlite'
        result=subprocess.run([sys.executable,'-m','desk.paper_cycle_cli','--config',str(self.path),
            'init','--ledger-db',str(ledger)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'],'INITIALIZED')
        self.assertTrue(ledger.exists())
        self.path.write_bytes(self.path.read_bytes()+b' ')
        other=self.root/'beyond.sqlite'
        result=subprocess.run([sys.executable,'-m','desk.paper_cycle_cli','--config',str(self.path),
            'init','--ledger-db',str(other)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertEqual(result.stderr,'');self.assertFalse(other.exists())

    def test_existing_semantic_validator_and_single_captured_input_are_preserved(self):
        # Mutating the caller path after preflight cannot replace its validated
        # snapshot with a last-wins duplicate policy document.
        def semantic(snapshot):
            self.assertNotEqual(Path(snapshot),self.path)
            self.path.write_text('{"paper_signal_policy_version":2,'+json.dumps(self.cfg)[1:])
            return load_config(snapshot)
        with patch.object(cli,'load_config',side_effect=semantic) as validator:
            self.assertEqual(cli._config(self.path),self.cfg)
        self.assertEqual(validator.call_count,1)
        for change in ({'mode':'live'},{'fee_reserve_sol':'100'},
                       {'daily_pause_fraction':'.9'},{'initial_equity_sol':'0'}):
            value=copy.deepcopy(self.cfg);value.update(change);self.path.write_text(json.dumps(value))
            with self.subTest(change=change),self.assertRaises(ValueError):cli._config(self.path)


if __name__=='__main__':unittest.main()
