"""Real separate-process scheduling lease; synthetic protected files only."""
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
