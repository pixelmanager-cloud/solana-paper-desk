"""Actual cross-process canonical locks; synthetic fixture, no provider I/O."""
from contextlib import contextmanager
import subprocess
import sys
import unittest
from unittest.mock import patch
from tests import test_paper_entry_dispatcher as fixture
from tools import paper_entry_dispatcher as tool

_LOCKER = '''import fcntl,sys
with open(sys.argv[1],'a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 print('LOCKED',flush=True)
 sys.stdin.readline()
'''

class DispatchContentionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.DispatcherTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.processes=[];self.addCleanup(self.release)
    def hold(self,path):
        child=subprocess.Popen([sys.executable,'-c',_LOCKER,str(path)],stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.processes.append(child)
        self.assertEqual(child.stdout.readline().strip(),'LOCKED')
    def release(self):
        for child in self.processes:
            if child.poll() is None:
                child.communicate('\n',timeout=5)
            else:
                child.communicate(timeout=5)
            self.assertEqual(child.returncode,0)
        self.processes=[]
    def invoke(self):return self.f.invoke(execute=True,systemd_credentials=True)
    def assert_clean(self):
        self.assertEqual(self.f.count('intents'),0)
        self.assertEqual(self.f.count('results'),0)
        with self.f.f.jobs.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM scans').fetchone()[0],0)
    def test_competing_process_at_final_admission_lock_is_zero_intent_and_retryable(self):
        paths=(str(self.f.f.jobs.path)+'.jobs-worker.lock',
               str(self.f.f.progress.store.path)+'.ownership-invocation.lock',
               str(self.f.ledger)+'.paper-cycle.lock')
        original=tool.monitor._context
        for path in paths:
            with self.subTest(path=path):
                calls=[0]
                @contextmanager
                def contend(*args,**kw):
                    calls[0]+=1
                    if calls[0]==3:self.hold(path)
                    with original(*args,**kw) as value:yield value
                with patch.object(tool.monitor,'_context',side_effect=contend),\
                     patch.object(tool.cli,'_credentials',side_effect=AssertionError('credentials before final locks')),\
                     patch('desk.providers.helius_rpc',side_effect=AssertionError('provider')):
                    with self.assertRaisesRegex(ValueError,'busy'):self.invoke()
                self.assert_clean();self.release()
                self.assertEqual(self.f.invoke()['status'],'DRY_RUN')
                self.assert_clean()
    def test_busy_initial_preflight_never_reads_credentials_or_creates_intent(self):
        self.hold(str(self.f.f.jobs.path)+'.jobs-worker.lock')
        with patch.object(tool.cli,'_credentials',side_effect=AssertionError('credentials')),\
             patch('desk.providers.helius_rpc',side_effect=AssertionError('provider')):
            with self.assertRaisesRegex(ValueError,'Research worker busy'):self.invoke()
        self.assert_clean()
    def test_credential_failure_before_publication_is_zero_intent(self):
        with patch.object(tool.cli,'_credentials',side_effect=ValueError('credential unavailable')),\
             patch('desk.providers.helius_rpc',side_effect=AssertionError('provider')):
            with self.assertRaisesRegex(ValueError,'credential unavailable'):self.invoke()
        self.assert_clean()
    def test_post_admission_handoff_contention_stays_unresolved_no_automatic_retry(self):
        original=tool.monitor._context;calls=[0]
        @contextmanager
        def contend(*args,**kw):
            calls[0]+=1
            with original(*args,**kw) as value:yield value
            if calls[0]==3:self.hold(str(self.f.f.jobs.path)+'.jobs-worker.lock')
        with patch.object(tool.monitor,'_context',side_effect=contend),\
             patch.object(tool.cli,'_credentials'),\
             patch('desk.providers.helius_rpc',side_effect=AssertionError('provider')):
            with self.assertRaisesRegex(ValueError,'Acquisition incomplete'):self.invoke()
        self.assertEqual(self.f.count('intents'),1);self.assertEqual(self.f.count('results'),0)
        with self.f.f.jobs.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM scans').fetchone()[0],1)
        self.release()
        with patch.object(tool.cli,'_credentials',side_effect=AssertionError('retry')):
            with self.assertRaisesRegex(ValueError,'Unresolved dispatch'):self.invoke()
        self.assertEqual(self.f.count('intents'),1)
