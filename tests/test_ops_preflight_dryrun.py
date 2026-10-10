"""SYNTHETIC_TEST_ONLY: preflight dry-run harness against copies of fixture stores.

Store sets come from the dispatcher fixtures (actual schema creators). No provider
credentials, live RPC or production data; the harness child runs offline.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from tools.ops import preflight_dryrun as tool
from tests import test_paper_entry_dispatcher as dispatcher_fixtures

REPO = Path(__file__).resolve().parents[1]


def tree(root):
    """Bytes + mtimes + names of everything under root (detects any write).

    A read-only SQLite connection to a WAL database refreshes the mtime of its ``-shm`` wal-index
    (contents unchanged; any reader does this, including the backup API), so only its bytes are
    compared. Every other file is compared by bytes and mtime.
    """
    out = {}
    for p in sorted(Path(root).rglob('*')):
        if p.is_file():
            mtime = None if p.name.endswith('-shm') else p.stat().st_mtime_ns
            out[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest(), mtime)
        else:
            out[str(p)] = None
    return out


NAMESPACE = tool.namespace_prefix() is not None


# Guard tests use --no-namespace and must run everywhere.
NO_NAMESPACE_TESTS = {
    'test_network_attempt_fails_the_run_even_if_release_code_swallows_it',
    'test_raw_socket_connect_is_blocked_by_audit_hook',
    'test_write_or_sqlite_outside_workdir_is_blocked_and_leaves_no_file',
    'test_process_creation_is_blocked', 'test_child_environment_carries_no_provider_secrets',
    'test_provider_credentials_refuse_to_run', 'test_setup_refusals', 'test_aliased_source_stores_are_refused',
    'test_without_namespace_absolute_path_bindings_hit_the_guard_not_live_data',
    'test_release_or_python_under_data_is_refused'}


class PreflightDryrunTests(unittest.TestCase):
    def setUp(self):
        if not NAMESPACE and self._testMethodName not in NO_NAMESPACE_TESTS:
            self.skipTest('private mount namespace (unshare --mount) unavailable on this host')
        self.d = dispatcher_fixtures.DispatcherTests('test_dry_run_no_admission_credentials_or_io_and_context_activation')
        self.d.setUp()
        self.addCleanup(self.d.doCleanups)
        self.data = self.d.root
        lock = self.data / 'paper-scheduler.lock'   # live scheduler lock (mode 0600) as in production
        lock.touch(mode=0o600)
        os.chmod(lock, 0o600)
        self.scratch = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.work = self.scratch / 'work'
        # The Kraken pacing receipt is approved by the release's own policy file; the fixture pin
        # (path + source hash of this exact tree) goes into a release COPY, never into the repo.
        self.release = self.scratch / 'release'
        for name in ('desk', 'tools', 'discovery', 'config'):
            shutil.copytree(REPO / name, self.release / name, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copyfile(self.data / 'migration.json', self.release / 'config' / 'kraken-pacing-migration.json')

    def rel(self, path):
        return str(Path(path).resolve().relative_to(self.data))

    def argv(self, **extra):
        a = ['--data', str(self.data), '--ledger', self.rel(self.d.ledger), '--config', str(self.d.config),
             '--release-dir', str(self.release), '--workdir', str(self.work),
             '--research-db', self.rel(self.d.f.jobs.path), '--evidence-db', self.rel(self.d.f.progress.store.path),
             '--pacing-db', self.rel(self.d.pacer), '--discovery-db', self.rel(self.d.discovery),
             '--journal', self.rel(self.d.journal)]
        for k, v in extra.items():
            a += ['--' + k.replace('_', '-'), str(v)] if v is not True else ['--' + k.replace('_', '-')]
        return a

    def run_tool(self, **extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tool.main(self.argv(**extra))
        return code, json.loads(out.getvalue())

    def test_clean_store_set_passes_and_live_files_are_untouched(self):
        before = tree(self.data)
        code, report = self.run_tool()
        self.assertEqual((code, report['status'], report['blockers']), (0, 'PASS', []), report)
        self.assertEqual(tree(self.data), before, 'live fixture files/mtimes/names must be unchanged')
        self.assertTrue(report['live_sources_unchanged'])
        self.assertIsNone(report['gate']['gate_result'])
        self.assertEqual(report['scheduler']['status'], 'PLAN')
        self.assertIsNone(report['preflight']['refusal_reason'])
        self.assertEqual(report['preflight']['journal_context'], 'MATCH_EXCLUDING_IDENTITIES')
        self.assertFalse(report['preflight']['context_mismatch'])
        self.assertEqual(report['pending_null_pass_ids'], [])
        self.assertFalse(report['live_readiness'])
        for key in ('copy_seconds', 'child_seconds', 'total_seconds'):
            self.assertIn(key, report['timing'])
        self.assertFalse(self.work.exists(), 'workdir deleted unless --keep')

    def test_keep_retains_private_workdir_copies(self):
        code, report = self.run_tool(keep=True)
        self.assertEqual(code, 0, report)
        self.assertEqual(self.work.stat().st_mode & 0o077, 0)
        copy = self.work / self.rel(self.d.ledger)
        self.assertTrue(copy.is_file())
        self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(copy.stat().st_ino, self.d.ledger.stat().st_ino)

    def test_retained_null_pass_reports_recovery_required_and_ids(self):
        ident, intent = 'a' * 32, 'b' * 64
        with sqlite3.connect(self.d.f.progress.store.path) as c:
            c.execute('CREATE TABLE IF NOT EXISTS paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)', (ident, intent))
        before = tree(self.data)
        code, report = self.run_tool()
        self.assertEqual(code, 2, report)
        self.assertEqual(report['status'], 'BLOCKED')
        self.assertEqual(report['gate']['gate_result'], 'OBSERVATION_RECOVERY_REQUIRED')
        self.assertIn('GATE_OBSERVATION_RECOVERY_REQUIRED', report['blockers'])
        self.assertEqual(report['pending_null_pass_ids'], [ident])
        self.assertEqual(report['preflight']['refusal_reason'], 'Observation recovery or retired scan')
        self.assertEqual(tree(self.data), before)

    def test_held_position_refuses_entry(self):
        result = self.d.live()
        self.assertEqual(result['status'], 'DISPATCHED', result)
        code, report = self.run_tool()
        self.assertEqual(code, 2, report)
        self.assertEqual(report['gate']['positions'], 1)
        self.assertEqual(report['preflight']['refusal_reason'], 'Held-position priority or paused ledger')
        self.assertEqual(report['scheduler']['status'], 'HELD_POSITION_PRIORITY')
        self.assertIn('PREFLIGHT_REFUSED', report['blockers'])

    def test_changed_dispatcher_source_is_reported_as_journal_context_mismatch(self):
        # A release whose dispatcher tool bytes differ from the activated context.
        release = self.release
        with (release / 'tools' / 'paper_entry_dispatcher.py').open('a') as stream:
            stream.write('\n# harmless reviewed-context drift\n')
        before = tree(self.data)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tool.main(self.argv())
        report = json.loads(out.getvalue())
        self.assertEqual(code, 2, report)
        self.assertTrue(report['preflight']['context_mismatch'])
        self.assertIn('tool_hash', report['preflight']['journal_context_diff'])
        self.assertIn('CONTEXT_MISMATCH', report['blockers'])
        self.assertEqual(tree(self.data), before)

    def test_provider_credentials_refuse_to_run(self):
        for name in ('HELIUS_API_KEY', 'JUPITER_API_KEY', 'KRAKEN_API_KEY', 'KRAKEN_API_SECRET', 'CREDENTIALS_DIRECTORY'):
            with self.subTest(name=name), patch.dict(os.environ, {name: 'x'}):
                code, report = self.run_tool()
                self.assertEqual((code, report['status']), (3, 'REFUSED'))
                self.assertIn(name, report['reason'])
                self.assertFalse(self.work.exists())

    def test_setup_refusals(self):
        link = self.scratch / 'link'
        link.symlink_to(self.data)
        for label, extra in (('symlinked data', {'data': link}), ('existing workdir', {'workdir': self.scratch}),
                             ('workdir inside data', {'workdir': self.data / 'work'}),
                             ('absolute store name', {'research_db': '/etc/passwd'}),
                             ('traversal store name', {'evidence_db': '../x.sqlite'}),
                             ('missing store', {'ledger': 'nope.sqlite'})):
            with self.subTest(label):
                argv = self.argv()
                for k, v in extra.items():
                    argv[argv.index('--' + k.replace('_', '-')) + 1] = str(v)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = tool.main(argv)
                self.assertEqual((code, json.loads(out.getvalue())['status']), (3, 'REFUSED'), label)
                self.assertFalse(self.work.exists() and label != 'existing workdir', label)

    def test_aliased_source_stores_are_refused(self):
        alias = self.data / 'alias.sqlite'
        os.link(self.d.discovery, alias)
        self.addCleanup(alias.unlink)
        code, report = self.run_tool()
        self.assertEqual((code, report['status']), (3, 'REFUSED'))
        alias.unlink()
        alias.symlink_to(self.d.discovery)
        code, report = self.run_tool()
        self.assertEqual((code, report['status']), (3, 'REFUSED'))

    def test_pending_ids_come_from_the_copy_not_the_live_evidence_store(self):
        seen = []
        original = tool._pending
        with patch.object(tool, '_pending', side_effect=lambda p: (seen.append(p), original(p))[1]):
            code, report = self.run_tool()
        self.assertEqual(code, 0, report)
        self.assertEqual(seen, [self.work / self.rel(self.d.f.progress.store.path)])

    def test_live_modes_and_lock_masked_by_the_copy_still_block(self):
        lock = self.data / 'paper-scheduler.lock'
        for label, action, code_name in (
                ('missing lock', lambda: lock.unlink(), 'LIVE_scheduler_lock:MISSING'),
                ('open journal mode', lambda: os.chmod(self.d.journal, 0o644), 'LIVE_journal:MODE_644'),
                ('pacing mode', lambda: os.chmod(self.d.pacer, 0o640), 'LIVE_pacing:MODE_640')):
            with self.subTest(label):
                action()
                code, report = self.run_tool()
                self.assertEqual((code, report['status']), (2, 'BLOCKED'), report)
                self.assertTrue(any(b.startswith(code_name) for b in report['blockers']), report['blockers'])
                lock.touch(mode=0o600); os.chmod(lock, 0o600)
                os.chmod(self.d.journal, 0o600); os.chmod(self.d.pacer, 0o600)

    def test_missing_activated_journal_blocks_instead_of_passing(self):
        os.rename(self.d.journal, self.d.journal.with_suffix('.db'))
        code, report = self.run_tool(taker=self.d.f.taker, amount_raw=100_000_000, pool_fee_bps='25')
        self.assertEqual((code, report['status']), (2, 'BLOCKED'), report)
        self.assertIn('JOURNAL_ABSENT', report['blockers'])

    def test_context_diff_catches_a_different_store_path_not_only_hashes(self):
        # Same journal, but the caller points --discovery-db at another valid store: path strings differ.
        other = self.data / 'other-discovery.sqlite'
        shutil.copyfile(self.d.discovery, other)
        os.chmod(other, 0o600)
        argv = self.argv()
        argv[argv.index('--discovery-db') + 1] = 'other-discovery.sqlite'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tool.main(argv)
        report = json.loads(out.getvalue())
        self.assertEqual(code, 2, report)
        self.assertIn('paths', report['preflight']['journal_context_diff'])
        self.assertIn('CONTEXT_MISMATCH', report['blockers'])

    def test_missing_stage_is_a_blocker(self):
        self.assertIn('GATE_MISSING', tool._blockers({'import': {'ok': True}}, []))

    # --- guard proof: the release code under test may not reach the network or live data.
    def fake_release(self, gate_body):
        release = self.scratch / 'fake-release'
        (release / 'desk').mkdir(parents=True)
        files = {
            'desk/__init__.py': '',
            'desk/runtime_compatibility.py': 'def implementation_hash():\n    return "f" * 64\n',
            'desk/paper_cycle_cli.py': 'def _config(path):\n    return {}\n',
            'desk/paper_monitor_operator.py': textwrap.dedent('''
                from contextlib import contextmanager
                @contextmanager
                def _context(research, evidence, ledger, cfg):
                    yield object(), ledger, {"positions": {}, "mode": "RUNNING"}
                '''),
            'desk/paper_terminal_reconciliation.py': 'def gate(store, research, scans, ledger_locked=None):\n' + textwrap.indent(textwrap.dedent(gate_body), '    '),
        }
        for name, text in files.items():
            (release / name).write_text(text)
        return release

    def run_fake(self, gate_body, namespace=False):
        release = self.fake_release(gate_body)
        out = io.StringIO()
        extra = {} if namespace else {'no_namespace': True}
        with contextlib.redirect_stdout(out):
            code = tool.main([a if a != str(self.release) else str(release) for a in self.argv(**extra)])
        return code, json.loads(out.getvalue())

    def test_network_attempt_fails_the_run_even_if_release_code_swallows_it(self):
        code, report = self.run_fake('''
            import socket
            try:
                socket.create_connection(("127.0.0.1", 9), timeout=1)
            except OSError:
                pass
            return None
            ''')
        self.assertEqual(code, 3, report)
        self.assertEqual(report['status'], 'GUARD_VIOLATION')
        self.assertTrue(any(v['kind'] == 'NETWORK' for v in report['guard_violations']), report)
        self.assertIn('GUARD_VIOLATION', report['blockers'])

    def test_raw_socket_connect_is_blocked_by_audit_hook(self):
        code, report = self.run_fake('''
            import _socket
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            try:
                s.connect(("127.0.0.1", 9))
            except OSError:
                pass
            return None
            ''')
        self.assertEqual((code, report['status']), (3, 'GUARD_VIOLATION'), report)

    def test_write_or_sqlite_outside_workdir_is_blocked_and_leaves_no_file(self):
        target = self.data / 'escaped.txt'
        db = self.data / 'escaped.sqlite'
        code, report = self.run_fake(f'''
            import sqlite3
            for action in (lambda: open({str(target)!r}, "w").write("x"), lambda: sqlite3.connect({str(db)!r})):
                try:
                    action()
                except OSError:
                    pass
            return None
            ''')
        self.assertEqual((code, report['status']), (3, 'GUARD_VIOLATION'), report)
        kinds = {v['kind'] for v in report['guard_violations']}
        self.assertTrue({'WRITE_OUTSIDE_WORKDIR', 'SQLITE_OUTSIDE_WORKDIR'} <= kinds, kinds)
        self.assertFalse(target.exists() or db.exists())

    def test_process_creation_is_blocked(self):
        code, report = self.run_fake('''
            import subprocess
            try:
                subprocess.run(["true"])
            except OSError:
                pass
            return None
            ''')
        self.assertEqual((code, report['status']), (3, 'GUARD_VIOLATION'), report)

    def test_live_data_directory_is_invisible_inside_the_namespace(self):
        (self.data / 'live-only-marker.txt').write_text('live')
        code, report = self.run_fake(f'''
            import os
            return ",".join(sorted(os.listdir({str(self.data)!r})))
            ''', namespace=True)
        listing = report['gate']['gate_result'].split(',')
        self.assertIn(self.rel(self.d.ledger), listing, report)
        self.assertNotIn('live-only-marker.txt', listing, 'live files must not be visible to the child')
        self.assertEqual(report['isolation'], 'MOUNT_NAMESPACE')

    def test_without_namespace_absolute_path_bindings_hit_the_guard_not_live_data(self):
        # The evidence store binds the live ledger path; on a plain copy the gate would open
        # it. The audit guard must refuse and the run must fail rather than read live data.
        code, report = self.run_tool(no_namespace=True)
        self.assertEqual((code, report['status'], report['isolation']), (3, 'GUARD_VIOLATION', 'NONE'), report)
        self.assertTrue(any(v['kind'] == 'SQLITE_OUTSIDE_WORKDIR' and str(self.d.ledger) in v['detail']
                            for v in report['guard_violations']), report['guard_violations'])

    def test_release_or_python_under_data_is_refused(self):
        release = self.data / 'rel'
        (release / 'desk').mkdir(parents=True)
        argv = [a if a != str(self.release) else str(release) for a in self.argv()]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tool.main(argv)
        self.assertEqual((code, json.loads(out.getvalue())['status']), (3, 'REFUSED'))

    def test_child_environment_carries_no_provider_secrets(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'unrelated-secret', 'SECRET_TOKEN': 'x'}):
            code, report = self.run_fake('''
                import os
                leaked = sorted(k for k in os.environ if "KEY" in k or "SECRET" in k or "TOKEN" in k or "CREDENTIAL" in k)
                return "LEAK:" + ",".join(leaked) if leaked else None
                ''')
        self.assertIsNone(report['gate']['gate_result'], report)


if __name__ == '__main__':
    unittest.main()
