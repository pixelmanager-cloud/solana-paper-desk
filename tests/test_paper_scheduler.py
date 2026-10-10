"""Real separate-process scheduling lease; synthetic protected files only."""
from contextlib import redirect_stdout
import io
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from tools import paper_scheduler as wrapper
from desk.paper_scheduler import lease

class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.research=self.root/'research.sqlite'
        self.research.touch();self.path=self.root/'paper-scheduler.lock';self.path.touch(mode=0o600)
        info=self.path.stat();env=patch.dict(os.environ,{'DESK_PAPER_SCHEDULER_IDENTITY':str(info.st_dev)+':'+str(info.st_ino)})
        env.start();self.addCleanup(env.stop)
    def contender(self):
        code="from desk.paper_scheduler import lease\nimport sys\nwith lease(sys.argv[1]) as fd:print('BUSY' if fd is None else 'ACQUIRED')"
        return subprocess.check_output([sys.executable,'-c',code,str(self.research)],text=True).strip()
    def test_handoffs_keep_outer_lease_then_restart_releases(self):
        with lease(self.research) as fd:
            self.assertIsNotNone(fd)
            for _ in range(4):self.assertEqual(self.contender(),'BUSY')
        self.assertEqual(self.contender(),'ACQUIRED')
    def test_in_process_command_keeps_lease_across_handoffs(self):
        def invoke(args):
            self.assertEqual(args,['--research-db',str(self.research)])
            for _ in range(3):self.assertEqual(self.contender(),'BUSY')
            return 0
        with patch('desk.paper_monitor_service.main',side_effect=invoke):
            self.assertEqual(wrapper.main(['--research-db',str(self.research),'--mode','held','--','--research-db',str(self.research)]),0)
        self.assertEqual(self.contender(),'ACQUIRED')
    def test_actual_wrapper_busy_has_no_command_or_credentials(self):
        output=io.StringIO()
        with lease(self.research),redirect_stdout(output),patch('desk.paper_cycle_cli._credentials',side_effect=AssertionError('credentials')),patch('desk.paper_monitor_service.main',side_effect=AssertionError('command')):
            self.assertEqual(wrapper.main(['--research-db',str(self.research),'--mode','held','--','--research-db',str(self.research)]),0)
        self.assertEqual(json.loads(output.getvalue())['status'],'SCHEDULER_BUSY')
    def test_command_exception_releases_outer_lease(self):
        with patch('desk.paper_monitor_service.main',side_effect=ValueError('synthetic command failure')):
            with self.assertRaisesRegex(ValueError,'synthetic command failure'):
                wrapper.main(['--research-db',str(self.research),'--mode','held','--','--research-db',str(self.research)])
        self.assertEqual(self.contender(),'ACQUIRED')
    def test_same_path_replacement_rejects_pinned_identity(self):
        replacement=self.root/'replacement';replacement.touch(mode=0o600)
        replacement.replace(self.path)
        with self.assertRaisesRegex(ValueError,'identity mismatch'):
            with lease(self.research):self.fail('replacement admitted')
    def test_dashboard_once_obeys_actual_shared_lease(self):
        from desk.dashboard import Jobs
        jobs=Jobs(self.research,scanner=lambda _:self.fail('provider scanner'))
        with patch.dict(os.environ,{'DESK_PAPER_SCHEDULER_LOCK':str(self.path)}):
            with lease(self.research),patch.object(jobs.persistence,'worker',side_effect=AssertionError('inner worker on contention')):
                self.assertFalse(jobs.once())
            # Empty queue after release reaches the real worker, without scan.
            self.assertFalse(jobs.once())
    def test_actual_held_checkpoint_refuses_entry_without_ledger_change(self):
        from tests import test_paper_entry_dispatcher as fixture
        from desk import paper_cycle
        f=fixture.DispatcherTests();f.setUp()
        try:
            result=f.live();self.assertEqual(result['paper_status'],'COMPLETE')
            self.assertTrue(paper_cycle._state(f.ledger,f.cfg)['positions'])
            lock=f.root/'paper-scheduler.lock';lock.touch(mode=0o600)
            info=lock.stat();identity=str(info.st_dev)+':'+str(info.st_ino)
            before=hashlib.sha256(f.ledger.read_bytes()).hexdigest()
            args=['--research-db',str(f.f.jobs.path),'--mode','entry','--',
                  '--research-db',str(f.f.jobs.path),'--config',str(f.config),'--ledger-db',str(f.ledger)]
            output=io.StringIO()
            with patch.dict(os.environ,{'DESK_PAPER_SCHEDULER_IDENTITY':identity}),redirect_stdout(output),patch('desk.paper_cycle_cli._credentials',side_effect=AssertionError('credentials')),patch('tools.paper_entry_dispatcher.main',side_effect=AssertionError('entry invoked')):
                self.assertEqual(wrapper.main(args),0)
            self.assertEqual(json.loads(output.getvalue())['status'],'HELD_POSITION_PRIORITY')
            self.assertEqual(hashlib.sha256(f.ledger.read_bytes()).hexdigest(),before)
        finally:f.doCleanups()
    def test_exec_retains_lease_without_parent_child_gap(self):
        code="from desk.paper_scheduler import lease\nimport os,sys\nwith lease(sys.argv[1]) as fd:\n os.set_inheritable(fd,True)\n os.execv(sys.executable,[sys.executable,'-c',\"import sys;print('READY',flush=True);sys.stdin.readline()\"])"
        child=subprocess.Popen([sys.executable,'-c',code,str(self.research)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(),'READY');self.assertEqual(self.contender(),'BUSY')
            child.kill();child.wait(timeout=5)
            self.assertEqual(self.contender(),'ACQUIRED')
        finally:
            if child.poll() is None:child.kill();child.wait()
            child.stdin.close();child.stdout.close()
    def test_missing_symlink_hardlink_and_permissions_rejected(self):
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            with lease(self.research):pass
        other=self.root/'other';other.touch(mode=0o600);self.path.symlink_to(other)
        with self.assertRaises(OSError):
            with lease(self.research):pass
        self.path.unlink();os.link(other,self.path)
        with self.assertRaises(ValueError):
            with lease(self.research):pass
        self.path.unlink();self.path.touch(mode=0o644);self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            with lease(self.research):pass
