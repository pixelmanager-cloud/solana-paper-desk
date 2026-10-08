import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CredentialTests(unittest.TestCase):
    def test_systemd_credential_mode_and_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "provider-keys.json"
            p.write_text(json.dumps({"HELIUS_API_KEY": "secret-must-not-print"}))
            p.chmod(0o440)
            command = [sys.executable, "-m", "desk", "--secrets-file", str(p), "doctor"]
            env = dict(os.environ, CREDENTIALS_DIRECTORY=tmp)
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["helius_key_configured"])
            self.assertNotIn("secret-must-not-print", result.stdout + result.stderr)
            env.pop("CREDENTIALS_DIRECTORY")
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)

    def test_world_readable_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "key.json"
            p.write_text('{}')
            p.chmod(0o644)
            result = subprocess.run([sys.executable, "-m", "desk", "--secrets-file", str(p), "doctor"],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
    def test_deployed_decision_and_monitor_entrypoints_execute(self):
        import sqlite3,shlex
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);source=p/'research.sqlite';journal=p/'decisions.sqlite'
            with sqlite3.connect(source) as c:
                c.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
                c.execute('INSERT INTO scans VALUES(?,?,?,?,?)',('a','mint',1,'COMPLETE',json.dumps({'mint':'mint','observed_at':1,'unknowns':[]})))
            for service,extra in [('desk-decisions.service',['--db',str(source),'--journal',str(journal)]),('desk-paper-monitor.service',['--db',str(p/'missing.sqlite'),'--config',str(root/'config/paper.json')])]:
                line=next(x for x in (root/'deploy'/service).read_text().splitlines() if x.startswith('ExecStart='))
                command=shlex.split(line.split('=',1)[1]);result=subprocess.run([sys.executable,*command[1:4],*extra],capture_output=True,text=True,cwd=root)
                self.assertEqual(result.returncode,0,result.stderr);data=json.loads(result.stdout)
                self.assertFalse(data['automatic_entry_enabled'])
            self.assertTrue(journal.is_file());self.assertFalse((p/'missing.sqlite').exists())
