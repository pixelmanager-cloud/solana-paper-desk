"""Release staging/cutover/rollback tool: fake systemctl, temp filesystem, no network."""
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
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
        self.hang_start = set()        # start raises subprocess.TimeoutExpired
        self.noop_start = set()        # start succeeds but changes nothing (already running)
        self.unhealthy = set()
        self.substate = {}             # unit -> SubState override
        self.types = {}                # unit -> Type (default simple)
        self.pids = {}
        self.start_mono = {}
        self.nrestarts = {}
        self.enabled = {}              # unit -> is-enabled output
        self.stuck_enabled = set()     # disable does not take effect
        self.next_elapse = {}          # timer -> NextElapseUSecMonotonic override
        self.after_start = {}          # unit -> ActiveState right after start (default active)
        self.stale_reload = False
        self._pid = 1000

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
                    env = re.findall(r'^Environment=(.+)$', text, re.M)
                    self.effective[unit] = {'WorkingDirectory': wd.group(1) if wd else '',
                                            'ExecStart': ex[-1] if ex else '', 'Environment': env}
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if cmd == 'start':
            unit = argv[2]
            if unit in self.hang_start:
                raise subprocess.TimeoutExpired(argv, 120)
            if unit in self.fail_start:
                return SimpleNamespace(returncode=1, stdout='', stderr='boom')
            if unit not in self.noop_start:
                self.state[unit] = ('inactive' if self.types.get(unit) == 'oneshot'
                                    else self.after_start.get(unit, 'active'))
                self._pid += 1
                self.pids[unit] = self._pid
                self.start_mono[unit] = int(time.monotonic() * 1e6)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if cmd == 'stop':
            self.state[argv[2]] = 'inactive'
            self.pids[argv[2]] = 0
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if cmd == 'is-enabled':
            value = self.enabled.get(argv[2], 'disabled')
            return SimpleNamespace(returncode=0 if value.startswith('enabled') else 1, stdout=value + '\n', stderr='')
        if cmd == 'disable':
            if argv[2] not in self.stuck_enabled:
                self.enabled[argv[2]] = 'disabled'
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if cmd == 'show':
            unit = argv[2]
            eff = self.effective.get(unit, {})
            kind = self.types.get(unit, 'simple')
            timer = unit.endswith('.timer')
            out = {'ActiveState': 'failed' if unit in self.unhealthy else self.state.get(unit, 'inactive'),
                   'SubState': self.substate.get(unit, 'waiting' if timer else 'running'),
                   'Result': 'success', 'Type': kind,
                   'WorkingDirectory': eff.get('WorkingDirectory', '/opt/old'),
                   'MainPID': str(self.pids.get(unit, 0)),
                   'NRestarts': str(self.nrestarts.get(unit, 0)),
                   'ExecMainStartTimestampMonotonic': str(self.start_mono.get(unit, 0)),
                   'NextElapseUSecMonotonic': str(self.next_elapse.get(unit, 5_000_000 if timer else 0)),
                   'NextElapseUSecRealtime': '', 'LastTriggerUSec': '',
                   'Environment': ' '.join(eff.get('Environment', [])),
                   'ExecStartPre': '', 'ExecStartPost': '', 'ExecStop': '', 'ExecReload': ''}
            lines = []
            for prop in argv[3:]:
                key = prop[2:]
                if key == 'ExecStart':
                    values = eff.get('ExecStart', '')
                    for item in (values if isinstance(values, list) else [values]):
                        lines.append('ExecStart=' + item)
                else:
                    lines.append('%s=%s' % (key, out.get(key, '')))
            return SimpleNamespace(returncode=0, stdout='\n'.join(lines) + '\n', stderr='')
        raise AssertionError(argv)

    def mutating(self):
        return [c for c in self.calls if c[1] not in ('show', 'is-enabled')]

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
        self.sleeps = []
        self.sleep_hook = None

    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.sleep_hook:
            self.sleep_hook()

    def run_cli(self, *argv, apply=False):
        head = ['--journal', str(self.journal), '--python', 'python3']
        if apply:
            head.append('--apply')
        out = io.StringIO()
        with redirect_stdout(out):
            code = cutover.main(head + list(argv), runner=self.systemd, sleep=self._sleep)
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
            # T11F item 10: the fresh root lives OUTSIDE the archived /var/lib/solana-desk
            'ExecStart': '/opt/x/python -m tools.paper_scheduler --ledger-db /var/lib/solana-desk-fresh/exp-1/l.sqlite'}}}))
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
               'not json']
        # T11F item 8: a store-env naming a unit outside the cutover used to be refused; it is now
        # ignored (see test_store_env_units_outside_cutover_are_ignored_and_reported).
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
        # T11F item 1: a failed start is stopped too (it may be half-started), so the failing unit
        # is stopped first, then the earlier one. This replaces the former assertion that only
        # the already-started unit was stopped.
        self.assertEqual([c[2] for c in self.systemd.calls if c[1] == 'stop'], [SVC[1], SVC[0]])
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


# ---------------------------------------------------------------- T11F additions

T13_UNITS = {
    'desk-paper-entry-dispatcher': {
        'environment': ['DESK_PROVIDER_PACING_DB=/var/lib/solana-desk/provider-pacing.sqlite',
                        'DESK_PAPER_SCHEDULER_IDENTITY=64768:1234'],
        'argv': ['-m', 'tools.paper_scheduler', '--research-db', '/var/lib/solana-desk-fresh/v1/research.sqlite',
                 '--mode', 'entry', '--', '--config', '/etc/solana-paper/fresh.json',
                 '--pacing-db', '/var/lib/solana-desk/provider-pacing.sqlite',
                 '--discovery-db', '/var/lib/solana-desk/discovery/continuous.sqlite',
                 '--journal', '/var/lib/solana-desk-fresh/v1/entry-dispatch/dispatch.sqlite',
                 '--taker', '6E2G75Z3uJEnPo9EvzmLTxp8KB78m3RDsFBjoCTVHZD2', '--amount-raw', '100000000',
                 '--pool-fee-bps', '25']},
    'desk-paper-held-cycle': {
        'environment': ['DESK_PAPER_SCHEDULER_IDENTITY=64768:1234'],
        'argv': ['-m', 'tools.paper_scheduler', '--mode', 'held', '--', '--systemd-credentials']},
    'desk-decisions': {
        'environment': ['DESK_PAPER_SCHEDULER_IDENTITY=64768:1234'],
        'argv': ['-m', 'tools.paper_scheduler', '--mode', 'decisions', '--', '--db',
                 '/var/lib/solana-desk-fresh/v1/research.sqlite']},
    'desk-dashboard': {
        'environment': ['DESK_PAPER_SCHEDULER_LOCK=/var/lib/solana-desk-fresh/v1/paper-scheduler.lock'],
        'argv': ['-m', 'desk', '--secrets-file', '%d/provider-keys.json', 'serve', '--db',
                 '/var/lib/solana-desk-fresh/v1/research.sqlite', '--port', '8765']},
}


def sd_decode(line):
    """Minimal systemd ExecStart word splitter (double quotes, backslash, $$ and %% escapes)."""
    words, cur, i, quoted, started = [], '', 0, False, False
    while i < len(line):
        c = line[i]
        if quoted:
            if c == '\\':
                i += 1
                cur += line[i]
            elif c == '"':
                quoted = False
            else:
                cur += c
        elif c == '"':
            quoted, started = True, True
        elif c == ' ':
            if started or cur:
                words.append(cur)
            cur, started = '', False
        else:
            cur += c
            started = True
        i += 1
    if started or cur:
        words.append(cur)
    assert not quoted
    return [w.replace('$$', '$').replace('%%', '%') for w in words]


class StageHardeningTests(Base):
    def test_hash_and_extract_come_from_the_same_open_file(self):
        good = self.root / 'good.tgz'
        sha = make_tar(good, [('desk/__init__.py', b'GOOD'), ('desk/runtime_compatibility.py', STUB.encode())])
        evil = self.root / 'evil.tgz'
        make_tar(evil, [('desk/__init__.py', b'EVIL'), ('desk/runtime_compatibility.py', STUB.encode())])
        real_open = tarfile.open

        def swap_then_open(*args, **kwargs):
            os.replace(evil, good)       # an attacker swaps the path after it was hashed
            return real_open(*args, **kwargs)
        tarfile.open = swap_then_open
        try:
            code, out = self.run_cli('stage', '--tar', str(good), '--sha256', sha, '--commit', 'abc1234',
                                     '--release-root', str(self.roots), apply=True)
        finally:
            tarfile.open = real_open
        self.assertEqual(code, 0, out)
        self.assertEqual((self.roots / 'abc1234' / 'desk' / '__init__.py').read_bytes(), b'GOOD')

    def test_colliding_archive_members_are_a_cutover_error_without_residue(self):
        path = self.root / 'c.tgz'
        sha = make_tar(path, [('desk/__init__.py', b''), ('desk/a', b'file'), ('desk/a/b', b'under a file')])
        code, out = self.run_cli('stage', '--tar', str(path), '--sha256', sha, '--commit', 'abc1234',
                                 '--release-root', str(self.roots), apply=True)
        self.assertEqual(code, 2, out)
        self.assertEqual(list(self.roots.iterdir()), [])

    def test_symlinked_tar_path_refused(self):
        real = self.root / 'real.tgz'
        sha = make_tar(real, [('desk/__init__.py', b''), ('desk/runtime_compatibility.py', STUB.encode())])
        link = self.root / 'link.tgz'
        link.symlink_to(real)
        code, out = self.run_cli('stage', '--tar', str(link), '--sha256', sha, '--commit', 'abc1234',
                                 '--release-root', str(self.roots), apply=True)
        self.assertEqual(code, 2, out)


class StartLifecycleTests(Base):
    def test_hung_start_is_a_cutover_error_and_is_stopped(self):
        self.systemd.hang_start.add(SVC[1])
        code, out = self.cut()
        self.assertEqual(code, 2, out)
        self.assertIn('imed out', out['error'])
        self.assertEqual([c[2] for c in self.systemd.calls if c[1] == 'stop'], [SVC[1], SVC[0]])
        self.assertTrue(all(v != 'active' for v in self.systemd.state.values()))

    def test_runner_exception_during_read_is_a_cutover_error(self):
        def boom(argv, cwd=None):
            raise OSError('systemctl vanished')
        out = io.StringIO()
        rel = self.release()
        self.base_unit(SVC[0])
        with redirect_stdout(out):
            code = cutover.main(['--journal', str(self.journal), '--python', 'python3', '--apply', 'cutover',
                                 '--release', str(rel), '--release-root', str(self.roots),
                                 '--unit-dir', str(self.units), '--units', SVC[0]], runner=boom, sleep=lambda s: None)
        self.assertEqual(code, 2)
        self.assertIn('vanished', json.loads(out.getvalue())['error'])

    def test_already_running_unit_refused_before_any_mutation(self):
        self.systemd.state[SVC[0]] = 'active'
        code, out = self.cut()
        self.assertEqual(code, 2, out)
        self.assertIn('not inactive', out['error'])
        self.assertEqual(self.systemd.mutating(), [])
        self.assertEqual(list(self.units.glob('*.d')), [])

    def test_failed_unit_may_be_restarted(self):
        self.systemd.state[SVC[0]] = 'failed'
        self.assertEqual(self.cut()[0], 0)

    def _concurrent_old_process(self, unit, pid, start_mono):
        orig = self.systemd.__call__

        def hook(argv, cwd=None):
            result = orig(argv, cwd)
            if argv[:2] == ['systemctl', 'daemon-reload']:
                self.systemd.state[unit] = 'active'           # an old instance appears after the pre-check
                self.systemd.pids[unit] = pid
                self.systemd.start_mono[unit] = start_mono
                self.systemd.noop_start.add(unit)             # so `start` is a silent no-op
            return result
        return hook

    def _cut_with(self, runner, *units):
        out = io.StringIO()
        rel = self.release()
        for u in units:
            self.base_unit(u)
        with redirect_stdout(out):
            code = cutover.main(['--journal', str(self.journal), '--python', 'python3', '--apply', 'cutover',
                                 '--release', str(rel), '--release-root', str(self.roots),
                                 '--unit-dir', str(self.units), '--units', *units], runner=runner,
                                sleep=lambda s: None)
        return code, json.loads(out.getvalue())

    def test_start_that_left_an_old_process_running_is_detected(self):
        runner = self._concurrent_old_process(SVC[0], 555, 1)
        code, out = self._cut_with(runner, SVC[0])
        self.assertEqual(code, 2, out)
        self.assertRegex(out['error'], 'MainPID|ExecMainStart|not newer')
        self.assertEqual([c[2] for c in self.systemd.calls if c[1] == 'stop'], [SVC[0]])

    def test_oneshot_that_never_ran_is_not_healthy(self):
        self.systemd.types[SVC[0]] = 'oneshot'
        self.systemd.noop_start.add(SVC[0])
        code, out = self.cut(units=[SVC[0]])
        self.assertEqual(code, 2, out)

    def test_oneshot_that_ran_and_succeeded_is_healthy(self):
        self.systemd.types[SVC[0]] = 'oneshot'
        code, out = self.cut(units=[SVC[0]])
        self.assertEqual(code, 0, out)

    def test_timer_without_scheduled_trigger_is_not_healthy(self):
        unit = 'desk-decisions.timer'
        self.systemd.next_elapse[unit] = 0
        code, out = self.cut(units=['desk-decisions.service', unit])
        self.assertEqual(code, 2, out)
        self.assertIn('trigger', out['error'])

    def test_timer_with_scheduled_trigger_is_healthy(self):
        code, out = self.cut(units=['desk-decisions.service', 'desk-decisions.timer'])
        self.assertEqual(code, 0, out)

    def test_activating_or_auto_restart_is_not_healthy(self):
        for state in ('activating', 'reloading'):
            with self.subTest(state=state):
                self.systemd.after_start[SVC[0]] = state
                self.systemd.substate[SVC[0]] = 'auto-restart'
                code, out = self.cut(units=[SVC[0]])
                self.assertEqual(code, 2, out)
                self.systemd.state.clear()
                self.systemd.substate.clear()
                self.systemd.after_start.clear()

    def test_active_service_in_auto_restart_substate_is_not_healthy(self):
        self.systemd.substate[SVC[0]] = 'auto-restart'
        code, out = self.cut(units=[SVC[0]])
        self.assertEqual(code, 2, out)

    def test_settle_delay_default_and_override(self):
        self.assertEqual(self.cut()[0], 0)
        self.assertEqual(self.sleeps, [10.0])
        self.sleeps.clear()
        self.systemd.state.clear()
        self.assertEqual(self.cut('--settle-seconds', '3')[0], 0)
        self.assertEqual(self.sleeps, [3.0])

    def test_dry_run_does_not_sleep(self):
        self.assertEqual(self.cut(apply=False)[0], 0)
        self.assertEqual(self.sleeps, [])

    def test_restart_during_settle_window_fails_and_stops_everything(self):
        def crash_loop():
            self.systemd.nrestarts[SVC[1]] = 1
        self.sleep_hook = crash_loop
        code, out = self.cut()
        self.assertEqual(code, 2, out)
        self.assertIn('NRestarts', out['error'])
        self.assertEqual([c[2] for c in self.systemd.calls if c[1] == 'stop'], [SVC[1], SVC[0]])

    def test_unit_that_dies_during_settle_window_fails(self):
        def die():
            self.systemd.state[SVC[0]] = 'failed'
        self.sleep_hook = die
        code, out = self.cut()
        self.assertEqual(code, 2, out)
        self.assertTrue(all(v != 'active' for v in self.systemd.state.values()))


class KeepOffTests(Base):
    def _cut_entry(self, *extra):
        self.base_unit(ENTRY_SVC)
        return self.cut('--configure-only', ENTRY_SVC, '--keep-off', ENTRY_TIMER, *extra)

    def test_enabled_keep_off_timer_is_disabled_and_reported(self):
        for state in ('enabled', 'enabled-runtime', 'alias'):
            with self.subTest(state=state):
                self.systemd.calls.clear()
                self.systemd.enabled[ENTRY_TIMER] = state
                code, out = self._cut_entry()
                self.assertEqual(code, 0, out)
                self.assertIn(['systemctl', 'disable', ENTRY_TIMER], self.systemd.calls)
                self.assertEqual(self.systemd.enabled[ENTRY_TIMER], 'disabled')
                self.assertEqual(out['keep_off'], [{'unit': ENTRY_TIMER, 'enabled_before': state,
                                                    'enabled_after': 'disabled'}])
                self.assertNotIn(ENTRY_TIMER, self.systemd.started())

    def test_disabled_keep_off_timer_needs_no_disable_but_is_reported(self):
        code, out = self._cut_entry()
        self.assertEqual(code, 0, out)
        self.assertNotIn('disable', [c[1] for c in self.systemd.calls])
        self.assertEqual(out['keep_off'][0]['enabled_before'], 'disabled')

    def test_keep_off_that_stays_enabled_refuses_before_writes_and_starts(self):
        self.systemd.enabled[ENTRY_TIMER] = 'enabled'
        self.systemd.stuck_enabled.add(ENTRY_TIMER)
        code, out = self._cut_entry()
        self.assertEqual(code, 2, out)
        self.assertIn('still enabled', out['error'])
        self.assertEqual(self.systemd.started(), [])
        self.assertEqual(list(self.units.glob('*.d')), [])

    def test_dry_run_plans_disable_without_executing_it(self):
        self.systemd.enabled[ENTRY_TIMER] = 'enabled'
        self.base_unit(ENTRY_SVC)
        code, out = self.cut('--configure-only', ENTRY_SVC, '--keep-off', ENTRY_TIMER, apply=False)
        self.assertEqual(code, 0, out)
        self.assertIn('systemctl disable ' + ENTRY_TIMER, out['commands'])
        self.assertEqual(self.systemd.enabled[ENTRY_TIMER], 'enabled')
        self.assertEqual(self.systemd.mutating(), [])


class RollbackSafetyTests(Base):
    def setUp(self):
        super().setUp()
        self.managed = ['desk-decisions.service', 'desk-decisions.timer']
        self.assertEqual(self.cut(units=self.managed)[0], 0)
        self.systemd.calls.clear()

    def rollback(self, *extra, apply=True):
        return self.run_cli('rollback', '--units', 'desk-decisions.service', '--unit-dir', str(self.units),
                            *extra, apply=apply)

    def test_refused_while_unit_or_its_timer_is_active(self):
        code, out = self.rollback()
        self.assertEqual(code, 2, out)
        self.assertIn('active', out['error'])
        self.assertIn('desk-decisions.timer', out['error'])         # the partner timer counts
        self.assertTrue((self.units / 'desk-decisions.service.d' / cutover.DROPIN).is_file())
        self.assertEqual(self.systemd.mutating(), [])

    def test_refused_when_only_the_partner_timer_is_active(self):
        self.systemd.state['desk-decisions.service'] = 'inactive'
        code, out = self.rollback()
        self.assertEqual(code, 2, out)
        self.assertTrue((self.units / 'desk-decisions.service.d' / cutover.DROPIN).is_file())

    def test_stop_flag_stops_timer_first_then_service_then_removes(self):
        code, out = self.rollback('--stop')
        self.assertEqual(code, 0, out)
        stops = [c[2] for c in self.systemd.calls if c[1] == 'stop']
        self.assertEqual(stops, ['desk-decisions.timer', 'desk-decisions.service'])
        kinds = [c[1] for c in self.systemd.mutating()]
        self.assertEqual(kinds, ['stop', 'stop', 'daemon-reload'])
        self.assertFalse((self.units / 'desk-decisions.service.d' / cutover.DROPIN).exists())

    def test_stop_that_does_not_take_effect_aborts_before_removal(self):
        orig = self.systemd.__call__

        def sticky(argv, cwd=None):
            if argv[:2] == ['systemctl', 'stop']:
                return SimpleNamespace(returncode=0, stdout='', stderr='')   # reports success, unit stays active
            return orig(argv, cwd)
        out = io.StringIO()
        with redirect_stdout(out):
            code = cutover.main(['--journal', str(self.journal), '--python', 'python3', '--apply', 'rollback',
                                 '--units', 'desk-decisions.service', '--unit-dir', str(self.units), '--stop'],
                                runner=sticky, sleep=lambda s: None)
        self.assertEqual(code, 2)
        self.assertTrue((self.units / 'desk-decisions.service.d' / cutover.DROPIN).is_file())

    def test_inactive_units_roll_back_without_stop_flag(self):
        self.systemd.state.clear()
        code, out = self.rollback()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(out['removed']), 1)

    def test_unmanaged_active_unit_does_not_block_rollback(self):
        self.systemd.state.clear()
        self.systemd.state['desk-backup.service'] = 'active'
        code, out = self.run_cli('rollback', '--units', 'desk-decisions.service', 'desk-backup.service',
                                 '--unit-dir', str(self.units), apply=True)
        self.assertEqual(code, 0, out)

    def test_dry_run_with_active_units_reports_refusal_without_mutation(self):
        code, out = self.rollback(apply=False)
        self.assertEqual(code, 2, out)
        self.assertEqual(self.systemd.mutating(), [])


class ArchivedStoreGuardTests(Base):
    ARCH = '/var/lib/solana-desk'

    def env_file(self, units, name='env.json'):
        path = self.root / name
        path.write_text(json.dumps({'version': 1, 'units': units}))
        return str(path)

    def test_exec_start_into_archived_root_refused(self):
        env = self.env_file({SVC[0]: {'ExecStart': '/opt/x/python -m tools.paper_scheduler --ledger-db '
                                                   + self.ARCH + '/paper-kraken-77de75a2.sqlite'}})
        code, out = self.cut('--store-env', env)
        self.assertEqual(code, 2, out)
        self.assertIn('archived', out['error'])
        self.assertEqual(self.systemd.mutating(), [])
        self.assertEqual(list(self.units.glob('*.d')), [])

    def test_environment_into_archived_root_refused(self):
        env = self.env_file({SVC[0]: {'Environment': ['DESK_LEDGER=' + self.ARCH + '/active-paper.sqlite']}})
        code, out = self.cut('--store-env', env)
        self.assertEqual(code, 2, out)
        self.assertIn('archived', out['error'])

    def test_existing_base_unit_pointing_at_archived_root_refused(self):
        self.base_unit(SVC[0], '/opt/x/python -m desk serve --db ' + self.ARCH + '/research.sqlite --port 8765')
        code, out = self.cut()
        self.assertEqual(code, 2, out)
        self.assertIn('archived', out['error'])

    def test_configure_only_and_keep_off_services_are_checked_too(self):
        self.base_unit(ENTRY_SVC, '/opt/x/python --journal ' + self.ARCH + '/entry-dispatch/dispatch.sqlite')
        code, out = self.cut('--configure-only', ENTRY_SVC, '--keep-off', ENTRY_TIMER)
        self.assertEqual(code, 2, out)
        self.assertIn(ENTRY_SVC, out['error'])

    def test_shared_pacing_and_discovery_inputs_are_allowed(self):
        env = self.env_file({SVC[0]: {
            'Environment': ['DESK_PROVIDER_PACING_DB=' + self.ARCH + '/provider-pacing.sqlite'],
            'ExecStart': '/opt/x/python --discovery-db ' + self.ARCH + '/discovery/continuous.sqlite '
                         '--pacing-db ' + self.ARCH + '/provider-pacing.sqlite-wal'}})
        code, out = self.cut('--store-env', env)
        self.assertEqual(code, 0, out)

    def test_pacing_lookalike_inside_archived_root_is_not_shared(self):
        env = self.env_file({SVC[0]: {'ExecStart': '/opt/x/python --pacing-db ' + self.ARCH
                                                   + '/exp-1/provider-pacing.sqlite'}})
        self.assertEqual(self.cut('--store-env', env)[0], 2)

    def test_sibling_directory_with_same_prefix_is_not_archived(self):
        env = self.env_file({SVC[0]: {'ExecStart': '/opt/x/python --ledger-db /var/lib/solana-desk-fresh/v1/l.sqlite',
                                      'Environment': ['A=/var/lib/solana-desk2/x']}})
        self.assertEqual(self.cut('--store-env', env)[0], 0)

    def test_allow_archived_lists_exact_units_only(self):
        env = self.env_file({SVC[0]: {'ExecStart': '/opt/x/python --db ' + self.ARCH + '/research.sqlite'}})
        code, out = self.cut('--store-env', env, '--allow-archived', SVC[0])
        self.assertEqual(code, 0, out)
        env = self.env_file({SVC[1]: {'ExecStart': '/opt/x/python --db ' + self.ARCH + '/research.sqlite'}}, 'e2.json')
        self.systemd.state.clear()
        self.assertEqual(self.cut('--store-env', env, '--allow-archived', SVC[0])[0], 2)

    def test_custom_archived_root_and_extra_shared_path(self):
        env = self.env_file({SVC[0]: {'ExecStart': '/opt/x/python --db /srv/old/research.sqlite'}})
        self.assertEqual(self.cut('--store-env', env)[0], 0)           # default root does not match
        self.systemd.state.clear()
        self.assertEqual(self.cut('--store-env', env, '--archived-root', '/srv/old')[0], 2)
        self.systemd.state.clear()
        self.assertEqual(self.cut('--store-env', env, '--archived-root', '/srv/old',
                                  '--shared-path', '/srv/old/research.sqlite')[0], 0)

    def test_effective_exec_start_after_reload_into_archived_root_stops_before_start(self):
        orig = self.systemd.__call__

        def sneaky(argv, cwd=None):
            result = orig(argv, cwd)
            if argv[:2] == ['systemctl', 'daemon-reload']:
                self.systemd.effective[SVC[0]]['ExecStart'] = (
                    '{ path=/x ; argv[]=/x/python --db ' + self.ARCH + '/research.sqlite ; ignore_errors=no ; }')
            return result
        code, out = self._cut_with_runner(sneaky, SVC[0])
        self.assertEqual(code, 2, out)
        self.assertIn('archived', out['error'])
        self.assertEqual(self.systemd.started(), [])

    def test_effective_environment_after_reload_into_archived_root_stops_before_start(self):
        orig = self.systemd.__call__

        def sneaky(argv, cwd=None):
            result = orig(argv, cwd)
            if argv[:2] == ['systemctl', 'daemon-reload']:
                self.systemd.effective[SVC[0]]['Environment'] = ['LEDGER=' + self.ARCH + '/active-paper.sqlite']
            return result
        code, out = self._cut_with_runner(sneaky, SVC[0])
        self.assertEqual(code, 2, out)
        self.assertEqual(self.systemd.started(), [])

    def _cut_with_runner(self, runner, unit):
        out = io.StringIO()
        rel = self.release()
        self.base_unit(unit)
        with redirect_stdout(out):
            code = cutover.main(['--journal', str(self.journal), '--python', 'python3', '--apply', 'cutover',
                                 '--release', str(rel), '--release-root', str(self.roots),
                                 '--unit-dir', str(self.units), '--units', unit], runner=runner,
                                sleep=lambda s: None)
        return code, json.loads(out.getvalue())


class EffectiveExecLinesTests(Base):
    def _cut_with_runner(self, runner, unit):
        out = io.StringIO()
        rel = self.release()
        self.base_unit(unit)
        with redirect_stdout(out):
            code = cutover.main(['--journal', str(self.journal), '--python', 'python3', '--apply', 'cutover',
                                 '--release', str(rel), '--release-root', str(self.roots),
                                 '--unit-dir', str(self.units), '--units', unit], runner=runner,
                                sleep=lambda s: None)
        return code, json.loads(out.getvalue())

    def test_every_effective_exec_start_line_is_checked_not_just_the_last(self):
        orig = self.systemd.__call__

        def sneaky(argv, cwd=None):
            result = orig(argv, cwd)
            if argv[:2] == ['systemctl', 'daemon-reload']:
                self.systemd.effective[SVC[0]]['ExecStart'] = [
                    '{ path=/x ; argv[]=/x/python serve --host 0.0.0.0 ; ignore_errors=no ; }',
                    '{ path=/x ; argv[]=/x/python serve --host 127.0.0.1 ; ignore_errors=no ; }']
            return result
        code, out = self._cut_with_runner(sneaky, SVC[0])
        self.assertEqual(code, 2, out)
        self.assertIn('loopback', out['error'])
        self.assertEqual(self.systemd.started(), [])

    def test_semicolon_argument_cannot_hide_a_later_bind_flag(self):
        orig = self.systemd.__call__

        def sneaky(argv, cwd=None):
            result = orig(argv, cwd)
            if argv[:2] == ['systemctl', 'daemon-reload']:
                self.systemd.effective[SVC[0]]['ExecStart'] = (
                    '{ path=/x ; argv[]=/x/python serve ; --host 0.0.0.0 ; ignore_errors=no ; }')
            return result
        code, out = self._cut_with_runner(sneaky, SVC[0])
        self.assertEqual(code, 2, out)

    def test_every_base_unit_exec_start_line_is_checked(self):
        (self.units / SVC[0]).write_text('[Service]\nExecStart=/x/python ok\nExecStart=/x/python serve --host 0.0.0.0\n')
        (self.units / SVC[1]).write_text('[Service]\nExecStart=/x/python ok\n')
        code, out = self.cut()
        self.assertEqual(code, 2, out)


class T13FormatTests(Base):
    def write(self, payload, name='t13.json'):
        path = self.root / name
        path.write_text(json.dumps(payload))
        return str(path)

    def manifest(self, units=None):
        return {'version': 1, 'kind': 'fresh_start_manifest_v1', 'root': '/var/lib/solana-desk-fresh/v1',
                'units': units if units is not None else T13_UNITS}

    def all_units(self):
        return ['desk-paper-held-cycle.service', 'desk-decisions.service', 'desk-dashboard.service']

    def test_manifest_units_render_to_exec_start_with_interpreter(self):
        code, out = self.cut('--store-env', self.write(self.manifest()), '--interpreter', '/opt/solana-desk/.venv/bin/python',
                             units=self.all_units())
        self.assertEqual(code, 0, out)
        text = (self.units / 'desk-dashboard.service.d' / cutover.DROPIN).read_text()
        lines = text.splitlines()
        i = lines.index('ExecStart=')
        words = sd_decode(lines[i + 1][len('ExecStart='):])
        self.assertEqual(words[0], '/opt/solana-desk/.venv/bin/python')
        expected = ['-m', 'desk', '--secrets-file', '%d/provider-keys.json', 'serve', '--db',
                    '/var/lib/solana-desk-fresh/v1/research.sqlite', '--port', '8765']
        self.assertEqual(lines[i + 1][len('ExecStart='):].count('%d/provider-keys.json'), 1)
        self.assertEqual(words[1:], [w if not w.startswith('%d') else w for w in expected])
        self.assertIn('Environment=DESK_PAPER_SCHEDULER_LOCK=/var/lib/solana-desk-fresh/v1/paper-scheduler.lock', text)
        held = (self.units / 'desk-paper-held-cycle.service.d' / cutover.DROPIN).read_text()
        self.assertIn('Environment=DESK_PAPER_SCHEDULER_IDENTITY=64768:1234', held)
        self.assertIn(' --systemd-credentials', held)

    def test_default_interpreter_is_the_production_venv(self):
        self.assertEqual(self.cut('--store-env', self.write(self.manifest()), units=self.all_units())[0], 0)
        text = (self.units / 'desk-decisions.service.d' / cutover.DROPIN).read_text()
        self.assertIn('\nExecStart=/opt/solana-desk/.venv/bin/python -m tools.paper_scheduler ', text)

    def test_bare_map_is_accepted_too(self):
        self.assertEqual(self.cut('--store-env', self.write(T13_UNITS), units=self.all_units())[0], 0)

    def test_only_the_credentials_specifier_survives_percent_escaping(self):
        units = {'desk-decisions': {'environment': ['A=50%'], 'argv': ['-m', 'x', '100%', '%d/k.json', '%h/x', '--p=%d/y',
                                                                    '%%']}}
        self.assertEqual(self.cut('--store-env', self.write(self.manifest(units)), units=['desk-decisions.service'])[0], 0)
        text = (self.units / 'desk-decisions.service.d' / cutover.DROPIN).read_text()
        line = [l for l in text.splitlines() if l.startswith('ExecStart=/')][0]
        words = line[len('ExecStart='):]
        self.assertIn('100%%', words)
        self.assertIn('%d/k.json', words)
        self.assertNotIn('%%d/k.json', words)
        self.assertIn('%%h/x', words)
        self.assertIn('--p=%%d/y', words)          # only a whole-argument %d/ prefix is a credentials path
        self.assertIn('Environment=A=50%%', text)

    def test_hostile_arguments_round_trip_through_systemd_quoting(self):
        hostile = ['a b', 'it\'s', 'say "hi"', 'back\\slash', 'dollar$HOME', '$$', ';', 'semi;colon', 'tab-free', '',
                   'ünï', '--x=a b', '#hash', 'a\\"b']
        units = {'desk-decisions': {'environment': ['K=v w "q" \\ $ %'], 'argv': ['-m', 'x'] + hostile}}
        self.assertEqual(self.cut('--store-env', self.write(self.manifest(units)), units=['desk-decisions.service'])[0], 0)
        text = (self.units / 'desk-decisions.service.d' / cutover.DROPIN).read_text()
        line = [l for l in text.splitlines() if l.startswith('ExecStart=/')][0]
        words = sd_decode(line[len('ExecStart='):])
        self.assertEqual(words[1:], ['-m', 'x'] + hostile)
        self.assertEqual(len([l for l in text.splitlines() if l.startswith('ExecStart')]), 2)
        envs = [l for l in text.splitlines() if l.startswith('Environment=')]
        self.assertEqual(len(envs), 1)
        self.assertEqual(sd_decode(envs[0][len('Environment='):]), ['K=v w "q" \\ $ %'])

    def test_malformed_t13_inputs_refused_without_mutation(self):
        good = T13_UNITS['desk-decisions']
        bad = {
            'timer key': {'desk-decisions.timer': good},
            'bad name': {'ssh': good},
            'extra key': {'desk-decisions': dict(good, extra=1)},
            'argv not list': {'desk-decisions': {'environment': [], 'argv': '-m x'}},
            'argv non-string': {'desk-decisions': {'environment': [], 'argv': ['-m', 5]}},
            'empty argv': {'desk-decisions': {'environment': [], 'argv': []}},
            'newline in argv': {'desk-decisions': {'environment': [], 'argv': ['-m', 'x\nExecStart=/evil']}},
            'nul in argv': {'desk-decisions': {'environment': [], 'argv': ['-m', 'x\x00']}},
            'env injection': {'desk-decisions': {'environment': ['A=b\nExecStart=/evil'], 'argv': ['-m', 'x']}},
            'env not assignment': {'desk-decisions': {'environment': ['no-equals'], 'argv': ['-m', 'x']}},
            'missing argv': {'desk-decisions': {'environment': []}},
        }
        for label, units in bad.items():
            with self.subTest(label):
                code, out = self.cut('--store-env', self.write(self.manifest(units)), units=['desk-decisions.service'])
                self.assertEqual(code, 2, out)
        self.assertEqual(self.systemd.mutating(), [])
        self.assertEqual(list(self.units.glob('*.d')), [])

    def test_plan_output_with_placeholder_identity_is_refused(self):
        plan = self.manifest()
        plan['kind'] = 'fresh_start_plan_v1'
        code, out = self.cut('--store-env', self.write(plan), units=self.all_units())
        self.assertEqual(code, 2, out)
        self.assertIn('plan', out['error'])

    def test_unknown_interpreter_forms_refused(self):
        for interp in ('python', 'bin/python', '/opt/x y/python\n'):
            with self.subTest(interp=interp):
                code, out = self.cut('--store-env', self.write(self.manifest()), '--interpreter', interp,
                                     units=self.all_units())
                self.assertEqual(code, 2, out)

    def test_rendered_non_loopback_dashboard_is_still_refused(self):
        units = dict(T13_UNITS)
        units['desk-dashboard'] = {'environment': [], 'argv': ['-m', 'desk', 'serve', '--host', '0.0.0.0']}
        code, out = self.cut('--store-env', self.write(self.manifest(units)), units=self.all_units())
        self.assertEqual(code, 2, out)
        self.assertIn('loopback', out['error'])

    def test_store_env_units_outside_cutover_are_ignored_and_reported(self):
        code, out = self.cut('--store-env', self.write(self.manifest()),
                             units=['desk-decisions.service', 'desk-dashboard.service'])
        self.assertEqual(code, 0, out)
        self.assertEqual(out['store_env_ignored'], ['desk-paper-entry-dispatcher.service', 'desk-paper-held-cycle.service'])
        self.assertFalse((self.units / 'desk-paper-held-cycle.service.d').exists())
        self.assertNotIn('desk-paper-held-cycle.service', self.systemd.started())

    def test_subset_store_env_leaves_other_units_with_plain_working_directory_dropins(self):
        units = {'desk-decisions': T13_UNITS['desk-decisions']}
        code, out = self.cut('--store-env', self.write(self.manifest(units)), units=['desk-decisions.service', 'desk-dashboard.service'])
        self.assertEqual(code, 0, out)
        plain = (self.units / 'desk-dashboard.service.d' / cutover.DROPIN).read_text()
        self.assertNotIn('ExecStart', plain)
        self.assertIn('WorkingDirectory=', plain)


class MinorHardeningTests(Base):
    def test_marker_must_be_the_first_line(self):
        foreign = self.units / (SVC[0] + '.d') / cutover.DROPIN
        foreign.parent.mkdir()
        text = '[Service]\nWorkingDirectory=/opt/old\n# ' + cutover.MARKER + '\n'
        foreign.write_text(text)
        code, out = self.cut()
        self.assertEqual(code, 2, out)                      # unmanaged: needs --replace-existing
        code, out = self.run_cli('rollback', '--units', SVC[0], '--unit-dir', str(self.units), apply=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(out['removed'], [])
        self.assertEqual(foreign.read_text(), text)

    def test_dropins_and_their_directory_are_fsynced(self):
        if not os.path.isdir('/proc/self/fd'):
            self.skipTest('needs /proc')
        synced = []
        real = os.fsync

        def spy(fd):
            try:
                synced.append(os.readlink('/proc/self/fd/%d' % fd))
            except OSError:
                pass
            return real(fd)
        os.fsync = spy
        try:
            self.assertEqual(self.cut(units=[SVC[0]])[0], 0)
        finally:
            os.fsync = real
        self.assertTrue(any(p.endswith('60-reviewed-release.conf.tmp') or p.endswith(cutover.DROPIN) for p in synced), synced)
        self.assertIn(str(self.units / (SVC[0] + '.d')), synced)


class RealRuntimeDigestTests(unittest.TestCase):
    def test_subprocess_digest_matches_in_process_implementation_hash(self):
        from desk.runtime_compatibility import implementation_hash
        repo = Path(__file__).resolve().parents[1]
        got = cutover.runtime_digest(repo, cutover.Ops(), 'python3')
        self.assertEqual(got, implementation_hash())


if __name__ == '__main__':
    unittest.main()
