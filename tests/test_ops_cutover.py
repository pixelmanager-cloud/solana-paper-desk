"""Release staging/cutover/rollback tool: fake systemctl, temp filesystem, no network."""
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from tools.ops import cutover

STUB = ("import hashlib, pathlib\n"
        "def implementation_hash():\n"
        "    root = pathlib.Path(__file__).parent\n"
        "    h = hashlib.sha256()\n"
        "    for p in sorted(root.rglob('*.py')):\n"
        "        h.update(p.read_bytes())\n"
        "    return h.hexdigest()\n")
SVC = ['desk-dashboard.service', 'desk-decisions.service']
ENTRY_SVC, ENTRY_TIMER = 'desk-paper-entry-dispatcher.service', 'desk-paper-entry-dispatcher.timer'


def make_tar(path, members):
    """members: list of (name, bytes|('symlink',target)|('hardlink',target)|'dir')."""
    with tarfile.open(path, 'w:gz') as tar:
        for name, body in members:
            info = tarfile.TarInfo(name)
            if body == 'dir':
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif isinstance(body, tuple):
                info.type = tarfile.SYMTYPE if body[0] == 'symlink' else tarfile.LNKTYPE
                info.linkname = body[1]
                tar.addfile(info)
            else:
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FakeSystemd:
    """Simulates systemctl; python/other commands run for real. Effective config is
    only updated by daemon-reload, like systemd."""

    def __init__(self, unit_dir):
        self.unit_dir = Path(unit_dir)
        self.state = {}
        self.effective = {}
        self.calls = []
        self.fail_start = set()
        self.unhealthy = set()
        self.stale_reload = False

    def __call__(self, argv, cwd=None):
        if argv[0] != 'systemctl':
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
        self.calls.append(list(argv))
        cmd = argv[1]
        if cmd == 'daemon-reload':
            if not self.stale_reload:
                for conf in self.unit_dir.glob('*.service.d/60-reviewed-release.conf'):
                    unit = conf.parent.name[:-2]
                    text = conf.read_text()
                    wd = re.search(r'^WorkingDirectory=(.*)$', text, re.M)
                    ex = re.findall(r'^ExecStart=(.+)$', text, re.M)
                    self.effective[unit] = {'WorkingDirectory': wd.group(1) if wd else '',
                                            'ExecStart': ex[-1] if ex else ''}
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if cmd == 'start':
            if argv[2] in self.fail_start:
                return SimpleNamespace(returncode=1, stdout='', stderr='boom')
            self.state[argv[2]] = 'active'
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if cmd == 'stop':
            self.state[argv[2]] = 'inactive'
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if cmd == 'show':
            unit = argv[2]
            eff = self.effective.get(unit, {})
            out = {'ActiveState': 'failed' if unit in self.unhealthy else self.state.get(unit, 'inactive'),
                   'SubState': 'x', 'Result': 'success', 'Type': 'simple',
                   'WorkingDirectory': eff.get('WorkingDirectory', '/opt/old'),
                   'ExecStart': eff.get('ExecStart', '')}
            lines = ['%s=%s' % (a[2:], out[a[2:]]) for a in argv[3:]]
            return SimpleNamespace(returncode=0, stdout='\n'.join(lines) + '\n', stderr='')
        raise AssertionError(argv)

    def mutating(self):
        return [c for c in self.calls if c[1] != 'show']

    def started(self):
        return [c[2] for c in self.calls if c[1] == 'start']


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.roots = self.root / 'releases'
        self.units = self.root / 'units'
        self.journal = self.root / 'state' / 'journal.jsonl'
        for d in (self.roots, self.units):
            d.mkdir(mode=0o755)
        self.systemd = FakeSystemd(self.units)

    def run_cli(self, *argv, apply=False):
        head = ['--journal', str(self.journal), '--python', 'python3']
        if apply:
            head.append('--apply')
        out = io.StringIO()
        with redirect_stdout(out):
            code = cutover.main(head + list(argv), runner=self.systemd)
        return code, json.loads(out.getvalue())

    def release(self, name='abc1234'):
        rel = self.roots / name
        (rel / 'desk').mkdir(parents=True, exist_ok=True, mode=0o755)
        (rel / 'desk' / '__init__.py').write_text('')
        (rel / 'desk' / 'runtime_compatibility.py').write_text(STUB)
        return rel

    def base_unit(self, unit, exec_start='/opt/x/python -m tools.paper_scheduler --mode held'):
        (self.units / unit).write_text('[Service]\nExecStart=%s\n' % exec_start)

    def cut(self, *extra, units=SVC, apply=True, release=None):
        release = release or self.release()
        for u in units + [e for e in extra if isinstance(e, str) and e.endswith(('.service', '.timer'))]:
            if u.endswith('.service') and not (self.units / u).exists():
                self.base_unit(u)
        args = ['cutover', '--release', str(release), '--release-root', str(self.roots),
                '--unit-dir', str(self.units), '--units', *units]
        return self.run_cli(*args, *[e for e in extra if e != ''], apply=apply)


class StageTests(Base):
    def stage(self, tar, sha, *extra, apply=True, commit='abc1234'):
        return self.run_cli('stage', '--tar', str(tar), '--sha256', sha, '--commit', commit,
                            '--release-root', str(self.roots), *extra, apply=apply)

    def good_tar(self, name='r.tgz', members=None):
        path = self.root / name
        sha = make_tar(path, members or [('desk/__init__.py', b''), ('desk/runtime_compatibility.py', STUB.encode())])
        return path, sha

    def test_stage_extracts_and_prints_digest(self):
        tar, sha = self.good_tar()
        code, out = self.stage(tar, sha)
        self.assertEqual(code, 0, out)
        self.assertTrue((self.roots / 'abc1234' / 'desk' / '__init__.py').is_file())
        self.assertRegex(out['runtime_digest'], '^[0-9a-f]{64}$')
        self.assertEqual(out['runtime_digest'], cutover.runtime_digest(self.roots / 'abc1234', cutover.Ops(runner=self.systemd), 'python3'))
        self.assertEqual(list(self.roots.glob('.stage-*')), [])

    def test_stage_dry_run_extracts_nothing(self):
        tar, sha = self.good_tar()
        code, out = self.stage(tar, sha, apply=False)
        self.assertEqual((code, out['status']), (0, 'DRY_RUN'))
        self.assertEqual(list(self.roots.iterdir()), [])
        self.assertFalse(self.journal.exists())

    def test_sha_mismatch_refused_before_reading_members(self):
        tar, sha = self.good_tar()
        code, out = self.stage(tar, '0' * 64)
        self.assertEqual(code, 2)
        self.assertIn('sha256 mismatch', out['error'])
        self.assertEqual(list(self.roots.iterdir()), [])

    def test_bad_sha_and_commit_format_refused(self):
        tar, sha = self.good_tar()
        self.assertEqual(self.stage(tar, sha.upper())[0], 2)
        self.assertEqual(self.stage(tar, sha, commit='../x')[0], 2)
        self.assertEqual(list(self.roots.iterdir()), [])

    def test_path_traversal_and_absolute_names_refused(self):
        for name in ('../evil', 'desk/../../evil', '/etc/evil', 'desk\\..\\evil'):
            with self.subTest(name=name):
                tar, sha = self.good_tar('t.tgz', [('desk/__init__.py', b''), (name, b'x')])
                code, out = self.stage(tar, sha)
                self.assertEqual(code, 2, out)
                self.assertEqual(list(self.roots.iterdir()), [])
                self.assertFalse((self.root / 'evil').exists())

    def test_links_refused(self):
        for kind in ('symlink', 'hardlink'):
            with self.subTest(kind=kind):
                tar, sha = self.good_tar('l.tgz', [('desk/__init__.py', b''), ('desk/link', (kind, '/etc/passwd'))])
                code, out = self.stage(tar, sha)
                self.assertEqual(code, 2, out)
                self.assertIn('links', out['error'])
                self.assertEqual(list(self.roots.iterdir()), [])

    def test_duplicate_member_refused(self):
        tar, sha = self.good_tar('d.tgz', [('desk/a.py', b'1'), ('desk/./a.py', b'2')])
        code, out = self.stage(tar, sha)
        self.assertEqual(code, 2)
        self.assertIn('Duplicate', out['error'])

    def test_existing_destination_refused_and_untouched(self):
        tar, sha = self.good_tar()
        (self.roots / 'abc1234').mkdir()
        (self.roots / 'abc1234' / 'keep').write_text('x')
        code, out = self.stage(tar, sha)
        self.assertEqual(code, 2)
        self.assertEqual((self.roots / 'abc1234' / 'keep').read_text(), 'x')

    def test_missing_desk_dir_leaves_no_residue(self):
        tar, sha = self.good_tar('n.tgz', [('other/a.py', b'1')])
        code, out = self.stage(tar, sha)
        self.assertEqual(code, 2)
        self.assertEqual(list(self.roots.iterdir()), [])

    def test_strip_components_for_wrapped_archives(self):
        tar, sha = self.good_tar('w.tgz', [('proj-abc', 'dir'), ('proj-abc/desk/__init__.py', b''),
                                           ('proj-abc/desk/runtime_compatibility.py', STUB.encode())])
        code, out = self.stage(tar, sha, '--strip-components', '1')
        self.assertEqual(code, 0, out)
        self.assertTrue((self.roots / 'abc1234' / 'desk').is_dir())

    def test_size_bound_and_expect_digest(self):
        tar, sha = self.good_tar('s.tgz', [('desk/big.bin', b'x' * 5000)])
        code, out = self.stage(tar, sha, '--max-bytes', '100')
        self.assertEqual(code, 2)
        self.assertIn('size bound', out['error'])
        tar, sha = self.good_tar()
        code, out = self.stage(tar, sha, '--expect-digest', '1' * 64)
        self.assertEqual(code, 2)
        self.assertEqual(list(self.roots.iterdir()), [])

    def test_setuid_and_world_writable_modes_are_dropped(self):
        path = self.root / 'm.tgz'
        with tarfile.open(path, 'w:gz') as tar:
            for name, body, mode in (('desk/__init__.py', b'', 0o4777), ('desk/runtime_compatibility.py', STUB.encode(), 0o666)):
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(body), mode
                tar.addfile(info, io.BytesIO(body))
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(self.stage(path, sha)[0], 0)
        for name in ('__init__.py', 'runtime_compatibility.py'):
            self.assertEqual(os.stat(self.roots / 'abc1234' / 'desk' / name).st_mode & 0o7022, 0)

    def test_release_root_must_be_canonical_non_world_writable(self):
        tar, sha = self.good_tar()
        link = self.root / 'link'
        link.symlink_to(self.roots)
        out = io.StringIO()
        with redirect_stdout(out):
            code = cutover.main(['--journal', str(self.journal), 'stage', '--tar', str(tar), '--sha256', sha,
                                 '--commit', 'abc1234', '--release-root', str(link)], runner=self.systemd)
        self.assertEqual(code, 2)
        os.chmod(self.roots, 0o777)
        self.assertEqual(self.stage(tar, sha)[0], 2)


class BindCheckTests(unittest.TestCase):
    def test_flags_and_wildcards(self):
        bad = ['/x/python -m desk serve --host 0.0.0.0', '/x/python serve --bind=10.0.0.5', '/x/p -H ::',
               '/x/p serve --listen 192.168.1.2:8765', '/x/p 0.0.0.0:8765', '/x/p --addr [::]:8765',
               '/x/p serve 203.0.113.9:8765']
        for text in bad:
            with self.subTest(text=text):
                self.assertTrue(cutover.non_loopback_bind(text), text)

    def test_loopback_and_plain_commands_pass(self):
        good = ['/x/p -m desk serve --db /var/lib/x/r.sqlite --port 8765', '/x/p serve --host 127.0.0.1',
                '/x/p serve --bind=localhost:8765', '/x/p --host ::1', '/x/p serve 127.0.0.1:8765', '']
        for text in good:
            with self.subTest(text=text):
                self.assertIsNone(cutover.non_loopback_bind(text), text)


class CutoverTests(Base):
    def test_dry_run_mutates_nothing_and_prints_commands(self):
        code, out = self.cut(apply=False)
        self.assertEqual((code, out['status']), (0, 'DRY_RUN'))
        self.assertEqual(self.systemd.mutating(), [])
        self.assertEqual(list(self.units.glob('*.d')), [])
        self.assertFalse(self.journal.exists())
        self.assertIn('systemctl daemon-reload', out['commands'])
        self.assertIn('systemctl start desk-dashboard.service', out['commands'])

    def test_apply_pins_working_directory_then_reloads_then_starts_in_order(self):
        rel = self.release()
        code, out = self.cut(release=rel)
        self.assertEqual(code, 0, out)
        for unit in SVC:
            text = (self.units / (unit + '.d') / cutover.DROPIN).read_text()
            self.assertIn(cutover.MARKER, text)
            self.assertIn('WorkingDirectory=%s\n' % rel, text)
        kinds = [c[1] for c in self.systemd.mutating()]
        self.assertEqual(kinds, ['daemon-reload', 'start', 'start'])
        self.assertEqual(self.systemd.started(), SVC)
        self.assertEqual([s['unit'] for s in out['started']], SVC)
        events = [json.loads(l)['event'] for l in self.journal.read_text().splitlines()]
        self.assertIn('cutover-done', events)
        self.assertEqual(oct(self.journal.stat().st_mode & 0o777), '0o600')

    def test_keep_off_unit_is_never_started_even_if_service_is_cut(self):
        self.base_unit(ENTRY_SVC)
        code, out = self.cut('--configure-only', ENTRY_SVC, '--keep-off', ENTRY_TIMER)
        self.assertEqual(code, 0, out)
        self.assertNotIn(ENTRY_TIMER, self.systemd.started())
        self.assertNotIn(ENTRY_SVC, self.systemd.started())
        self.assertTrue((self.units / (ENTRY_SVC + '.d') / cutover.DROPIN).is_file())

    def test_keep_off_unit_listed_as_start_target_refused(self):
        code, out = self.cut('--keep-off', SVC[0])
        self.assertEqual(code, 2)
        self.assertEqual(self.systemd.mutating(), [])

    def test_keep_off_unit_already_active_refused_before_any_mutation(self):
        self.base_unit(ENTRY_SVC)
        self.systemd.state[ENTRY_TIMER] = 'active'
        code, out = self.cut('--configure-only', ENTRY_SVC, '--keep-off', ENTRY_TIMER)
        self.assertEqual(code, 2)
        self.assertIn('already active', out['error'])
        self.assertEqual(self.systemd.mutating(), [])
        self.assertEqual(list(self.units.glob('*.d')), [])

    def test_timer_without_its_service_refused(self):
        code, out = self.cut('--keep-off', ENTRY_TIMER)
        self.assertEqual(code, 2)
        self.assertIn('needs its service', out['error'])

    def test_entry_dispatcher_start_needs_explicit_flag(self):
        code, out = self.cut(units=[ENTRY_SVC])
        self.assertEqual(code, 2)
        self.assertEqual(self.systemd.started(), [])
        code, out = self.cut('--allow-entry', units=[ENTRY_SVC])
        self.assertEqual(code, 0, out)

    def test_invalid_unit_names_refused(self):
        for name in ('ssh.service', 'desk-x.service/../y', 'desk-x.socket', 'desk-X.service', 'desk-a b.service'):
            with self.subTest(name=name):
                code, out = self.cut(units=[name])
                self.assertEqual(code, 2)
        self.assertEqual(self.systemd.mutating(), [])

    def test_non_loopback_exec_start_from_store_env_refused(self):
        env = self.root / 'env.json'
        env.write_text(json.dumps({'version': 1, 'units': {
            SVC[0]: {'ExecStart': '/opt/x/python -m desk serve --host 0.0.0.0 --port 8765'}}}))
        code, out = self.cut('--store-env', str(env))
        self.assertEqual(code, 2)
        self.assertIn('loopback', out['error'])
        self.assertEqual(self.systemd.mutating(), [])
        self.assertEqual(list(self.units.glob('*.d')), [])

    def test_non_loopback_exec_start_in_existing_unit_refused(self):
        self.base_unit(SVC[0], '/opt/x/python serve --bind 0.0.0.0')
        code, out = self.cut()
        self.assertEqual(code, 2)
        self.assertEqual(self.systemd.mutating(), [])

    def test_effective_exec_start_off_loopback_after_reload_stops_everything(self):
        # An unseen drop-in changes ExecStart after reload: caught on the effective value pre-start.
        orig = self.systemd.__call__

        def sneaky(argv, cwd=None):
            result = orig(argv, cwd)
            if argv[:2] == ['systemctl', 'daemon-reload']:
                self.systemd.effective[SVC[0]]['ExecStart'] = '{ path=/x ; argv[]=/x/python serve --host 0.0.0.0 ; }'
            return result
        self.systemd.__call__ = sneaky
        old = self.run_cli
        out = io.StringIO()
        rel = self.release()
        self.base_unit(SVC[0])
        with redirect_stdout(out):
            code = cutover.main(['--journal', str(self.journal), '--python', 'python3', '--apply', 'cutover',
                                 '--release', str(rel), '--release-root', str(self.roots),
                                 '--unit-dir', str(self.units), '--units', SVC[0]], runner=sneaky)
        self.assertEqual(code, 2)
        self.assertEqual(self.systemd.started(), [])
        self.assertIn('loopback', json.loads(out.getvalue())['error'])

    def test_store_env_exec_start_and_environment_are_rendered(self):
        env = self.root / 'env.json'
        env.write_text(json.dumps({'version': 1, 'units': {SVC[1]: {
            'Environment': ['DESK_PROVIDER_PACING_DB=/var/lib/solana-desk/provider-pacing.sqlite'],
            'ExecStart': '/opt/x/python -m tools.paper_scheduler --ledger-db /var/lib/solana-desk/exp-1/l.sqlite'}}}))
        code, out = self.cut('--store-env', str(env))
        self.assertEqual(code, 0, out)
        text = (self.units / (SVC[1] + '.d') / cutover.DROPIN).read_text()
        self.assertIn('\nExecStart=\nExecStart=/opt/x/python', text)
        self.assertIn("Environment=DESK_PROVIDER_PACING_DB=/var/lib/solana-desk/provider-pacing.sqlite", text)
        self.assertNotIn('ExecStart', (self.units / (SVC[0] + '.d') / cutover.DROPIN).read_text())

    def test_store_env_malformed_inputs_refused(self):
        bad = ['{"version":1,"units":{"desk-a.service":{"ExecStart":"/x"},"desk-a.service":{}}}',
               '{"version":2,"units":{}}', '{"version":1,"units":{"ssh.service":{}}}',
               '{"version":1,"units":{"%s":{"Environment":["A=b\\nExecStart=/evil"]}}}' % SVC[0],
               '{"version":1,"units":{"%s":{"ExecStart":"relative cmd"}}}' % SVC[0],
               '{"version":1,"units":{"%s":{"WorkingDirectory":"/tmp"}}}' % SVC[0],
               '{"version":1,"units":{"desk-other.service":{}}}', 'not json']
        for i, text in enumerate(bad):
            with self.subTest(i=i):
                env = self.root / ('env%d.json' % i)
                env.write_text(text)
                code, out = self.cut('--store-env', str(env))
                self.assertEqual(code, 2, out)
        self.assertEqual(self.systemd.mutating(), [])

    def test_failed_start_stops_every_started_unit_and_keeps_dropins_for_rollback(self):
        self.systemd.fail_start.add(SVC[1])
        code, out = self.cut()
        self.assertEqual(code, 2)
        self.assertIn('stopped=', out['error'])
        # Only the unit that was actually started is stopped; nothing remains active.
        self.assertEqual([c[2] for c in self.systemd.calls if c[1] == 'stop'], [SVC[0]])
        self.assertTrue(all(v != 'active' for v in self.systemd.state.values()))
        for unit in SVC:
            self.assertTrue((self.units / (unit + '.d') / cutover.DROPIN).is_file())
        last = json.loads(self.journal.read_text().splitlines()[-1])
        self.assertEqual(last['event'], 'cutover-failed')
        self.assertEqual(len(last['dropins_left_for_rollback']), 2)

    def test_unhealthy_after_start_stops_all_started_in_reverse(self):
        self.systemd.unhealthy.add(SVC[1])
        code, out = self.cut()
        self.assertEqual(code, 2)
        self.assertEqual([c[2] for c in self.systemd.calls if c[1] == 'stop'], [SVC[1], SVC[0]])
        self.assertTrue(all(v != 'active' for v in self.systemd.state.values()))

    def test_stale_reload_working_directory_mismatch_blocks_start(self):
        self.systemd.stale_reload = True
        code, out = self.cut()
        self.assertEqual(code, 2)
        self.assertIn('WorkingDirectory', out['error'])
        self.assertEqual(self.systemd.started(), [])

    def test_release_must_be_canonical_direct_child_of_root_with_desk(self):
        rel = self.release()
        link = self.roots / 'link'
        link.symlink_to(rel)
        self.assertEqual(self.cut(release=link)[0], 2)
        outside = self.root / 'outside'
        (outside / 'desk').mkdir(parents=True)
        self.assertEqual(self.cut(release=outside)[0], 2)
        empty = self.roots / 'empty'
        empty.mkdir()
        self.assertEqual(self.cut(release=empty)[0], 2)
        self.assertEqual(self.systemd.mutating(), [])

    def test_expect_digest_mismatch_refused(self):
        code, out = self.cut('--expect-digest', '2' * 64)
        self.assertEqual(code, 2)
        self.assertEqual(self.systemd.mutating(), [])

    def test_foreign_dropin_refused_then_backed_up_and_restored_by_rollback(self):
        foreign = self.units / (SVC[0] + '.d') / cutover.DROPIN
        foreign.parent.mkdir()
        foreign.write_text('[Service]\nWorkingDirectory=/opt/solana-desk-releases/old\n')
        self.assertEqual(self.cut()[0], 2)
        self.assertEqual(foreign.read_text(), '[Service]\nWorkingDirectory=/opt/solana-desk-releases/old\n')
        code, out = self.cut('--replace-existing')
        self.assertEqual(code, 0, out)
        self.assertIn(cutover.MARKER, foreign.read_text())
        backups = list(foreign.parent.glob(cutover.DROPIN + '.pre-cutover-*'))
        self.assertEqual(len(backups), 1)
        code, out = self.run_cli('rollback', '--units', *SVC, '--unit-dir', str(self.units), apply=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(foreign.read_text(), '[Service]\nWorkingDirectory=/opt/solana-desk-releases/old\n')
        self.assertFalse((self.units / (SVC[1] + '.d') / cutover.DROPIN).exists())

    def test_rerun_on_own_dropin_keeps_original_backup_reference(self):
        foreign = self.units / (SVC[0] + '.d') / cutover.DROPIN
        foreign.parent.mkdir()
        foreign.write_text('[Service]\nWorkingDirectory=/old\n')
        self.assertEqual(self.cut('--replace-existing')[0], 0)
        self.assertEqual(self.cut()[0], 0)
        self.assertEqual(len(list(foreign.parent.glob(cutover.DROPIN + '.pre-cutover-*'))), 1)
        self.assertEqual(self.run_cli('rollback', '--units', SVC[0], '--unit-dir', str(self.units), apply=True)[0], 0)
        self.assertEqual(foreign.read_text(), '[Service]\nWorkingDirectory=/old\n')


class RollbackTests(Base):
    def test_removes_only_marked_dropins_and_reloads(self):
        self.assertEqual(self.cut()[0], 0)
        other = self.units / 'desk-backup.service.d' / cutover.DROPIN
        other.parent.mkdir()
        other.write_text('[Service]\nWorkingDirectory=/opt/handwritten\n')
        sibling = self.units / (SVC[0] + '.d') / '10-hardening.conf'
        sibling.write_text('[Service]\nMemoryMax=384M\n')
        self.systemd.calls.clear()
        code, out = self.run_cli('rollback', '--units', *SVC, 'desk-backup.service', 'desk-ghost.service',
                                 '--unit-dir', str(self.units), apply=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(out['removed']), 2)
        reasons = {s['unit']: s['reason'] for s in out['skipped']}
        self.assertEqual(reasons['desk-backup.service'], 'not written by cutover')
        self.assertEqual(reasons['desk-ghost.service'], 'no drop-in')
        self.assertTrue(other.is_file())
        self.assertTrue(sibling.is_file())
        self.assertEqual([c[1] for c in self.systemd.mutating()], ['daemon-reload'])

    def test_rollback_dry_run_removes_nothing(self):
        self.assertEqual(self.cut()[0], 0)
        self.systemd.calls.clear()
        code, out = self.run_cli('rollback', '--units', *SVC, '--unit-dir', str(self.units))
        self.assertEqual((code, out['status']), (0, 'DRY_RUN'))
        self.assertTrue((self.units / (SVC[0] + '.d') / cutover.DROPIN).is_file())
        self.assertEqual(self.systemd.mutating(), [])

    def test_symlinked_dropin_is_not_followed_or_removed(self):
        target = self.root / 'victim.conf'
        target.write_text(cutover.MARKER + '\n[Service]\n')
        link = self.units / (SVC[0] + '.d') / cutover.DROPIN
        link.parent.mkdir()
        link.symlink_to(target)
        code, out = self.run_cli('rollback', '--units', SVC[0], '--unit-dir', str(self.units), apply=True)
        self.assertEqual(code, 0)
        self.assertTrue(target.is_file())
        self.assertEqual(out['removed'], [])


class RealRuntimeDigestTests(unittest.TestCase):
    def test_subprocess_digest_matches_in_process_implementation_hash(self):
        from desk.runtime_compatibility import implementation_hash
        repo = Path(__file__).resolve().parents[1]
        got = cutover.runtime_digest(repo, cutover.Ops(), 'python3')
        self.assertEqual(got, implementation_hash())


if __name__ == '__main__':
    unittest.main()
