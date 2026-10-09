"""Real CLI composition against existing provisioned fixtures, no live calls."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from desk import coordinator_rpc
from desk.pool_point_capture_cli import main
from tests.test_pool_evidence_integration import Harness, KEY


@unittest.skipUnless(Path('/proc/self/mountinfo').is_file(), 'Linux protected coordinator contract')
class PoolPointCaptureCLITests(unittest.TestCase):
    def harness(self, mutate=None):
        h = Harness(self, mutate)
        h.store.path.chmod(0o600)
        return h

    def argv(self, h):
        return ['--coordinator-diagnostic', '--evidence-db', str(h.store.path),
                '--ledger-db', str(h.boundary.ledger_path), '--scan-id', h.binding.scan_id,
                '--capture-id', 'cli-point']

    def call(self, h, argv=None):
        output = io.StringIO()
        with patch.dict(os.environ, {'HELIUS_API_KEY': KEY}), \
             patch.object(coordinator_rpc, 'build_opener', return_value=h.opener) as opener, \
             patch('desk.pool_point_capture_cli.time.time', return_value=h.wall), \
             patch('socket.create_connection', side_effect=AssertionError('No network')), redirect_stdout(output):
            code = main(self.argv(h) if argv is None else argv)
        body = output.getvalue()
        self.assertNotIn(KEY, body)
        self.assertNotIn(str(h.store.path), body)
        self.assertNotIn(h.query['mint'], body)
        result = json.loads(body)
        for flag in ('chain_authenticated','historical_interval_exclusion_allowed','continuity_verified',
                     'private_control_proven','ownership_approval','eligible_for_trading'):
            self.assertIs(result[flag], False)
        return code, result, opener.call_count

    def bytes(self, h):
        return {str(p):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in h.store.path.parent.iterdir() if p.is_file()}

    def test_real_command_four_charged_calls_and_zero_call_replay_at_exhausted_budget(self):
        h = self.harness();code, result, calls = self.call(h)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'CAPTURED_POINT')
        self.assertEqual((result['provider_calls'], result['requests_used'], calls), (4,4,4))
        self.assertEqual(h.receipt().slot, h.bank)
        for _ in range(14):self.assertTrue(h.progress.reserve(h.binding.scan_id))
        before = h.records();code,result,calls = self.call(h)
        self.assertEqual((code,result['provider_calls'],result['requests_used'],calls),(0,0,18,0))
        self.assertEqual(h.records(), before)

    def test_invalid_admission_paths_and_no_policy_arguments_precede_mutation_or_transport(self):
        for mode in ('scan','missing','alias','policy'):
            h=self.harness();argv=self.argv(h);before=self.bytes(h)
            if mode=='scan':argv[argv.index('--scan-id')+1]='unknown'
            elif mode=='missing':argv[argv.index('--ledger-db')+1]=str(h.store.path.parent/'missing.sqlite')
            elif mode=='alias':
                alias=h.store.path.parent/'alias.sqlite';alias.symlink_to(h.store.path)
                argv[argv.index('--evidence-db')+1]=str(alias);before=self.bytes(h)
            else:argv += ['--source-id','candidate-assertion']
            code,result,calls=self.call(h,argv)
            self.assertNotEqual(code,0);self.assertEqual(calls,0);self.assertEqual(self.bytes(h),before)

    def test_insufficient_budget_rejects_without_initializing_capture_journal(self):
        h=self.harness()
        for _ in range(15):self.assertTrue(h.progress.reserve(h.binding.scan_id))
        before=self.bytes(h);code,result,calls=self.call(h)
        self.assertEqual((code,calls),(1,0));self.assertEqual(self.bytes(h),before)
        self.assertEqual(h.progress.admission(h.binding.scan_id)['requests_used'],15)

    def test_changed_provisioned_config_rejects_before_writer_or_transport(self):
        h=self.harness()
        # Existing immutable descriptor remains intact; fixed CLI refuses this
        # separately provisioned nondefault policy rather than rebinding it.
        import sqlite3
        from desk.model import canonical,digest
        with sqlite3.connect(h.boundary.ledger_path) as c:
            c.execute('DROP TRIGGER descriptor_no_update')
            body=json.loads(c.execute('SELECT body FROM ledger_descriptor').fetchone()[0]);body['max_age_seconds']=61
            c.execute('UPDATE ledger_descriptor SET body=?,hash=?',(canonical(body),digest(body)))
        before=self.bytes(h);code,result,calls=self.call(h)
        self.assertEqual((code,calls),(1,0));self.assertEqual(self.bytes(h),before)

    def test_transport_failure_is_terminal_and_redacted_on_same_or_new_capture_id(self):
        def failure(method,result):raise OSError(KEY)
        h=self.harness(failure);code,result,calls=self.call(h)
        self.assertEqual((code,result['provider_calls'],calls),(1,1,1))
        before=h.records()
        for new in (False,True):
            argv=self.argv(h)
            if new:argv[-1]='escape-attempt'
            code,result,calls=self.call(h,argv)
            self.assertEqual((code,calls),(1,0));self.assertEqual(h.records(),before)

    def test_wal_preflight_rejects_without_creating_sidecars(self):
        import sqlite3
        h=self.harness()
        with sqlite3.connect(h.store.path) as c:c.execute('PRAGMA journal_mode=WAL')
        before=self.bytes(h);code,result,calls=self.call(h)
        self.assertEqual((code,calls),(1,0));self.assertEqual(self.bytes(h),before)

    def test_config_rejection_never_reads_credentials(self):
        h=self.harness();argv=self.argv(h);argv[argv.index('--scan-id')+1]='missing'
        before=self.bytes(h);output=io.StringIO()
        original_get=coordinator_rpc.os.environ.get
        def guard_get(key,*args):
            if key=='HELIUS_API_KEY':raise AssertionError('Credential lookup')
            return original_get(key,*args)
        with patch.object(coordinator_rpc.os.environ,'get',side_effect=guard_get) as secret, \
             patch.object(coordinator_rpc,'build_opener',side_effect=AssertionError('Transport')) as opener, \
             redirect_stdout(output):
            self.assertEqual(main(argv),1)
        self.assertFalse(any(call.args[0]=='HELIUS_API_KEY' for call in secret.call_args_list))
        opener.assert_not_called();self.assertEqual(self.bytes(h),before)

    def test_lost_transport_completion_pending_stays_terminal_without_new_calls(self):
        def interrupted(method,result):raise SystemExit(77)
        h=self.harness(interrupted)
        with self.assertRaises(SystemExit):self.call(h)
        self.assertEqual(h.progress.admission(h.binding.scan_id)['requests_used'],1)
        before=h.records()
        for capture_id in ('cli-point','pending-escape'):
            argv=self.argv(h);argv[-1]=capture_id
            code,result,calls=self.call(h,argv)
            self.assertEqual((code,calls),(1,0));self.assertEqual(h.records(),before)
