"""SYNTHETIC_TEST_ONLY: preflight dry-run harness against copies of fixture stores.

Store sets come from the dispatcher fixtures (actual schema creators). No provider
credentials, live RPC or production data; the harness child runs offline.

CI note: tests that run the whole tool need the private mount namespace
(``unshare --mount`` + ``mount --bind``). That needs EITHER euid 0 OR unprivileged user
namespaces (``unshare --mount --map-root-user``); no sudo is used. When the probe in
``tool.namespace_prefix()`` fails they SKIP with an explicit reason, while the guard tests
listed in ``NO_NAMESPACE_TESTS`` always run (also on macOS, where there is no ``unshare``).
GitHub Actions ``ubuntu-latest`` runs the namespace tests only if its image permits
unprivileged user namespaces (newer Ubuntu images restrict them through AppArmor; then they
skip unless the job relaxes ``kernel.apparmor_restrict_unprivileged_userns``). Unverified here.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import types
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
    'test_release_or_python_under_data_is_refused',
    'test_copy_of_quiet_wal_store_is_immutable_and_creates_no_sidecars',
    'test_copy_with_pending_wal_keeps_uncheckpointed_rows', 'test_fd_path_helper_per_platform',
    'test_child_uses_the_portable_fd_path_helper', 'test_parent_never_writes_bytecode',
    'test_docstring_states_what_is_compared', 'test_pending_unreadable_vs_absent_table_unit',
    'test_dir_fd_mutation_is_resolved_and_judged_without_proc_assumption',
    'test_unresolvable_or_unsupported_dir_fd_fails_closed'}


class PreflightDryrunTests(unittest.TestCase):
    def setUp(self):
        if not NAMESPACE and self._testMethodName not in NO_NAMESPACE_TESTS:
            self.skipTest('REQUIRES a private mount namespace (euid 0 or unprivileged user namespaces; '
                          'unshare --mount) - unavailable on this host')
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

    # --- T05F: owner is the expected service user, never the euid of this (root) process.
    def fake_owner(self, uid):
        real = os.lstat
        def stat(path, *a, **k):
            info = real(path, *a, **k)
            return types.SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink, st_uid=uid,
                                         st_size=info.st_size, st_mtime_ns=info.st_mtime_ns)
        return patch.object(tool, '_stat', side_effect=stat)

    def test_default_expected_owner_is_the_data_owner_not_the_running_uid(self):
        with self.fake_owner(os.geteuid() + 4242):   # stores belong to a service user, tool runs as another
            code, report = self.run_tool()
        self.assertEqual((code, report['status'], report['blockers']), (0, 'PASS', []), report)
        self.assertEqual(report['expected_owner'], {'uid': os.geteuid() + 4242, 'source': 'DATA_OWNER'})

    def test_explicit_expect_owner_mismatch_blocks_by_name_and_uid(self):
        with self.fake_owner(4242):
            code, report = self.run_tool(expect_owner='1111')
            self.assertEqual((code, report['status']), (2, 'BLOCKED'), report)
            self.assertTrue(any(b.startswith('LIVE_journal:OWNER_4242_EXPECTED_1111') for b in report['blockers']), report['blockers'])
            with patch.object(tool, '_getpwnam', return_value=types.SimpleNamespace(pw_uid=4242)):
                code, report = self.run_tool(expect_owner='solana-desk')
            self.assertEqual((code, report['status'], report['expected_owner']['source']), (0, 'PASS', 'FLAG_USER'), report)
            with patch.object(tool, '_getpwnam', side_effect=KeyError('x')):
                code, report = self.run_tool(expect_owner='no-such-user')
            self.assertEqual((code, report['status']), (3, 'REFUSED'))

    # --- T05F: a fresh store set must say exactly what is missing and which bootstrap step creates it.
    def test_missing_lock_and_journal_are_reported_precisely(self):
        (self.data / 'paper-scheduler.lock').unlink()
        os.rename(self.d.journal, self.d.journal.with_suffix('.db'))
        code, report = self.run_tool()   # no --taker: the child cannot even plan, the parent still names the journal
        self.assertEqual((code, report['status']), (2, 'BLOCKED'), report)
        self.assertIn('JOURNAL_ABSENT', report['blockers'])
        by = {d['blocker']: d for d in report['blocker_details']}
        self.assertEqual(by['LIVE_scheduler_lock:MISSING']['missing'], 'paper-scheduler.lock')
        self.assertIn('fresh_start', by['LIVE_scheduler_lock:MISSING']['created_by'])
        self.assertIn('T13F', by['LIVE_scheduler_lock:MISSING']['created_by'])
        self.assertEqual(by['JOURNAL_ABSENT']['missing'], self.rel(self.d.journal))
        self.assertIn('no bypass', by['JOURNAL_ABSENT']['created_by'])
        self.assertFalse((self.data / 'paper-scheduler.lock').exists(), 'the live tree is never repaired by this tool')

    # --- T05F: unreadable pending data is a blocker; a fresh store without the table is not.
    def test_pending_unreadable_vs_absent_table_unit(self):
        scratch = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, scratch, True)
        empty = scratch / 'empty.sqlite'
        sqlite3.connect(empty).close()
        self.assertEqual(tool._pending(empty), ([], False))
        bad = scratch / 'bad.sqlite'
        with contextlib.closing(sqlite3.connect(bad)) as c, c:
            c.execute('CREATE TABLE paper_observation_passes(id TEXT)')   # no outcome_hash column
        with self.assertRaises(tool.PendingUnreadable):
            tool._pending(bad)
        garbage = scratch / 'garbage.sqlite'
        garbage.write_bytes(b'not a database' * 100)
        with self.assertRaises(tool.PendingUnreadable):
            tool._pending(garbage)

    def test_unreadable_pending_blocks_the_run(self):
        with sqlite3.connect(self.d.f.progress.store.path) as c:
            c.execute('DROP TABLE IF EXISTS paper_observation_passes')
            c.execute('CREATE TABLE paper_observation_passes(id TEXT)')
        code, report = self.run_tool()
        self.assertEqual((code, report['status']), (2, 'BLOCKED'), report)
        self.assertIn('PENDING_UNREADABLE', report['blockers'])
        self.assertFalse(report['pending_null_pass_ids_readable'])

    def test_fresh_evidence_store_without_pass_table_is_not_a_blocker(self):
        with sqlite3.connect(self.d.f.progress.store.path) as c:
            c.execute('DROP TABLE IF EXISTS paper_observation_passes')
        code, report = self.run_tool()
        self.assertNotIn('PENDING_UNREADABLE', report['blockers'])
        self.assertTrue(report['pending_null_pass_ids_readable'])

    # --- T05F: no sidecars beside live stores; sources are re-hashed, not just stat-ed.
    def wal_store(self, directory, hold_writer):
        path = directory / 'wal.sqlite'
        writer = sqlite3.connect(path, isolation_level=None)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE t(x)')
        writer.execute('INSERT INTO t VALUES(1)')
        if hold_writer:
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute('INSERT INTO t VALUES(2)')
            self.addCleanup(writer.close)
        else:
            writer.close()
        return path

    def test_copy_of_quiet_wal_store_is_immutable_and_creates_no_sidecars(self):
        data = Path(tempfile.mkdtemp()).resolve(); work = Path(tempfile.mkdtemp()).resolve()
        for d in (data, work):
            self.addCleanup(shutil.rmtree, d, True)
        src = self.wal_store(data, hold_writer=False)
        self.assertEqual(sorted(p.name for p in data.iterdir()), ['wal.sqlite'])
        (info,) = tool._copy(data, work, [src])
        self.assertEqual(sorted(p.name for p in data.iterdir()), ['wal.sqlite'], 'reading must create no -wal/-shm')
        self.assertEqual(info['read_mode'], 'IMMUTABLE_NO_SIDECARS')
        with contextlib.closing(sqlite3.connect(work / 'wal.sqlite')) as c:
            self.assertEqual(c.execute('SELECT x FROM t ORDER BY x').fetchall(), [(1,)])

    def test_copy_with_pending_wal_keeps_uncheckpointed_rows(self):
        data = Path(tempfile.mkdtemp()).resolve(); work = Path(tempfile.mkdtemp()).resolve()
        for d in (data, work):
            self.addCleanup(shutil.rmtree, d, True)
        src = self.wal_store(data, hold_writer=True)
        (info,) = tool._copy(data, work, [src])
        self.assertEqual(info['read_mode'], 'READ_ONLY_SIDECAR_PRESENT')   # immutable would silently lose row 2
        with contextlib.closing(sqlite3.connect(work / 'wal.sqlite')) as c:
            self.assertEqual(c.execute('SELECT x FROM t ORDER BY x').fetchall(), [(1,), (2,)])

    def test_sidecar_or_file_created_beside_a_live_store_is_a_blocker(self):
        original = tool._copy
        def copy_and_litter(data, work, sources):
            result = original(data, work, sources)
            (data / 'research.sqlite-wal').write_bytes(b'')      # what a plain read-only open leaves behind
            (data / 'stray.txt').write_text('x')
            return result
        with patch.object(tool, '_copy', side_effect=copy_and_litter):
            code, report = self.run_tool()
        self.assertEqual((code, report['status']), (2, 'BLOCKED'), report)
        self.assertIn('LIVE_SIDECAR_CREATED:research.sqlite-wal', report['blockers'])
        self.assertIn('LIVE_FILE_CREATED:stray.txt', report['blockers'])
        self.assertEqual(report['live_sidecars_created'], ['research.sqlite-wal'])

    def test_same_size_same_mtime_modification_of_a_live_source_is_detected_by_sha256(self):
        original = tool._copy
        def copy_then_tamper(data, work, sources):
            result = original(data, work, sources)
            target = sources[0]
            info = target.stat()
            blob = bytearray(target.read_bytes()); blob[-1] ^= 0xFF
            target.write_bytes(bytes(blob))
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))   # size and mtime look untouched
            self.assertEqual((target.stat().st_size, target.stat().st_mtime_ns), (info.st_size, info.st_mtime_ns))
            return result
        with patch.object(tool, '_copy', side_effect=copy_then_tamper):
            code, report = self.run_tool()
        self.assertEqual((code, report['status']), (2, 'BLOCKED'), report)
        self.assertFalse(report['live_sources_unchanged'])
        self.assertIn('LIVE_SOURCE_CHANGED_DURING_RUN', report['blockers'])

    # --- T05F: portable dir_fd resolution (no /proc assumption off Linux).
    def test_fd_path_helper_per_platform(self):
        ns = {'os': os, 'sys': sys}
        exec(tool.FD_PATH_HELPER, ns)
        fd_path = ns['fd_path']
        directory = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, directory, True)
        fd = os.open(directory, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        self.assertEqual(Path(fd_path(fd, platform='linux')).resolve(), directory)
        calls = []
        fake = types.SimpleNamespace(F_GETPATH=50, fcntl=lambda f, cmd, buf: (calls.append((f, cmd)), b'/private/tmp/x\x00' + b'\x00' * 9)[1])
        self.assertEqual(fd_path(7, platform='darwin', fcntl_module=fake), '/private/tmp/x')
        self.assertEqual(calls, [(7, 50)])
        for platform, module in (('darwin', types.SimpleNamespace()), ('win32', None), ('freebsd13', None)):
            with self.subTest(platform), self.assertRaises(OSError):   # unresolvable -> the hook refuses (fail closed)
                fd_path(fd, platform=platform, fcntl_module=module)

    def test_child_uses_the_portable_fd_path_helper(self):
        outside_helper = tool.CHILD.replace(tool.FD_PATH_HELPER, '')
        self.assertNotIn('/proc/self/fd', outside_helper)
        self.assertIn('fd_path(fd)', tool.CHILD)
        self.assertNotIn('#FD_PATH_HELPER#', tool.CHILD)

    def test_dir_fd_mutation_is_resolved_and_judged_without_proc_assumption(self):
        # Runs the real child hook (Linux here): dir_fd inside the workdir passes, outside is a violation.
        inside = self.work
        outside = self.data
        code, report = self.run_fake(f'''
            import os
            fd = os.open({str(inside)!r}, os.O_RDONLY)
            os.mkdir("inside-ok", dir_fd=fd)
            os.rmdir("inside-ok", dir_fd=fd)
            fd2 = os.open({str(outside)!r}, os.O_RDONLY)
            try:
                os.mkdir("outside-bad", dir_fd=fd2)
            except OSError:
                pass
            return None
            ''')
        self.assertEqual((code, report['status']), (3, 'GUARD_VIOLATION'), report)
        details = [v['detail'] for v in report['guard_violations'] if v['kind'] == 'MUTATION_OUTSIDE_WORKDIR']
        self.assertEqual(len(details), 1, report['guard_violations'])
        self.assertTrue(details[0].endswith('outside-bad'), details)
        self.assertFalse((self.data / 'outside-bad').exists())

    def test_unresolvable_or_unsupported_dir_fd_fails_closed(self):
        code, report = self.run_fake(f'''
            import os, sys
            fd = os.open({str(self.work)!r}, os.O_RDONLY)
            os.close(fd)                        # a dir_fd that no longer resolves to any path
            try:
                os.mkdir("closed-fd", dir_fd=fd)
            except OSError:
                pass
            good = os.open({str(self.work)!r}, os.O_RDONLY)
            sys.platform = "freebsd13"          # no /proc and no F_GETPATH: cannot be judged -> refuse
            try:
                os.mkdir("unsupported-platform", dir_fd=good)
            except OSError:
                pass
            return None
            ''')
        self.assertEqual((code, report['status']), (3, 'GUARD_VIOLATION'), report)
        details = [v['detail'] for v in report['guard_violations'] if v['kind'] == 'MUTATION_OUTSIDE_WORKDIR']
        self.assertEqual(details, ['unresolvable dir_fd', 'unresolvable dir_fd'], report['guard_violations'])
        self.assertFalse((self.work / 'closed-fd').exists() or (self.work / 'unsupported-platform').exists())

    # --- T05F minor items.
    def test_parent_never_writes_bytecode(self):
        # The flag cannot stop the interpreter caching the tool module itself when it is imported, but
        # it must stop everything imported after it, and `python -m` must leave no cache at all.
        package = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, package, True)
        (package / 'tools' / 'ops').mkdir(parents=True)
        shutil.copyfile(REPO / 'tools' / 'ops' / 'preflight_dryrun.py', package / 'tools' / 'ops' / 'preflight_dryrun.py')
        (package / 'tools' / 'ops' / 'sibling.py').write_text('VALUE = 1\n')
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONDONTWRITEBYTECODE'}
        done = subprocess.run([sys.executable, '-c', 'import sys, tools.ops.preflight_dryrun, tools.ops.sibling; '
                               'print(sys.dont_write_bytecode)'], cwd=package, env=env, capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'True'), done.stderr)
        self.assertEqual([p.name for p in package.rglob('*.pyc') if 'sibling' in p.name], [],
                         'modules imported after the flag must not be cached')
        shutil.rmtree(package / 'tools' / 'ops' / '__pycache__', ignore_errors=True)
        done = subprocess.run([sys.executable, '-m', 'tools.ops.preflight_dryrun', '--help'],
                              cwd=package, env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        # The loader caches the module it runs (before the flag executes); nothing else may be cached.
        self.assertEqual({p.name.split('.')[0] for p in package.rglob('*.pyc')} - {'preflight_dryrun'}, set())

    def test_docstring_states_what_is_compared(self):
        self.assertNotIn('``paths``/``journal`` are excluded', tool.__doc__)
        self.assertIn('ARE compared', tool.__doc__)
        for text in ('paths.*.device', 'paths.*.inode', "config copy's path", 'excluded_context_fields'):
            self.assertIn(text, tool.__doc__)
        self.assertEqual(tool.SKIPPED_CONTEXT_FIELDS, ('paths.*.device', 'paths.*.inode', 'paths.config.path'))

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
