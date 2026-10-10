"""SYNTHETIC_TEST_ONLY: T13F unit wiring, service-user ownership and locked rotation for tools.ops.fresh_start.

Temp directories and fixture stores only: no network, no credentials, no systemd. The drop-in checks use an
independent mini model of systemd's rules (word splitting, `%%`/`$$`, empty-assignment reset of list settings)
applied to the real deploy/*.service templates, so a drop-in that leaves an old store path in force fails here.
"""
from collections import defaultdict
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch

from desk import dashboard
from desk.backup import selected_paper_ledger
from desk.model import canonical, digest
from tests.test_ops_fresh_start import FreshStartBase
from tools.ops import fresh_start as fs

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / 'deploy'
UNITS = ('desk-paper-entry-dispatcher', 'desk-paper-held-cycle', 'desk-paper-monitor', 'desk-decisions',
         'desk-dashboard', 'desk-backup')


# ---- independent model of the systemd rules the drop-ins rely on ---------------------------------
def split_words(text):
    words, current, quote, started, i = [], '', None, False, 0
    while i < len(text):
        c = text[i]
        if c == '\\' and i + 1 < len(text):
            current += text[i + 1]; i += 2; started = True; continue
        if quote:
            if c == quote:
                quote = None
            else:
                current += c
        elif c in '"\'':
            quote = c; started = True
        elif c.isspace():
            if started:
                words.append(current); current = ''; started = False
        else:
            current += c; started = True
        i += 1
    assert quote is None, 'unterminated quote'
    if started:
        words.append(current)
    return words


def exec_words(line):
    out = []
    for word in split_words(line):
        word = re.sub(r'%(.)', lambda m: '%' if m.group(1) == '%' else m.group(0), word)
        out.append(word.replace('$$', '$'))
    return out


def env_words(line):
    return [re.sub(r'%(.)', lambda m: '%' if m.group(1) == '%' else m.group(0), w) for w in split_words(line)]


def settings(*texts):
    """Effective list-valued settings after applying the files in order; an empty assignment resets."""
    result, section = defaultdict(list), None
    for text in texts:
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith(('#', ';')):
                continue
            if line.startswith('['):
                section = line.strip('[]'); continue
            key, _, value = line.partition('=')
            if value == '':
                result[(section, key)] = []
            else:
                result[(section, key)].append(value)
    return result


def base_unit(unit):
    return (DEPLOY / f'{unit}.service').read_text()


class WiringBase(FreshStartBase):
    def applied(self, name='exp-1', **extra):
        manifest = fs.apply(fs.plan(**self.args(name, **extra)), service_user=self.user)
        return manifest, self.base / name

    def env(self, manifest, unit):
        return dict(item.split('=', 1) for item in manifest['units'][unit]['environment'])


class DashboardWiringTests(WiringBase):
    def test_dashboard_emits_scheduler_identity_and_ledger_selection(self):
        manifest, root = self.applied()
        env = self.env(manifest, 'desk-dashboard')
        self.assertEqual(env['DESK_PAPER_SCHEDULER_LOCK'], str(root / 'paper-scheduler.lock'))
        self.assertEqual(env['DESK_PAPER_SCHEDULER_IDENTITY'], manifest['scheduler_identity'])
        self.assertEqual(env['DESK_PAPER_LEDGER_DB'], str(root / 'paper-ledger.sqlite'))

    def test_jobs_once_runs_against_the_new_root_with_the_emitted_env_and_dies_without_identity(self):
        old_umask = os.umask(0o022)         # an operator shell: the files must still end up protected
        self.addCleanup(os.umask, old_umask)
        manifest, root = self.applied()
        env = self.env(manifest, 'desk-dashboard')
        jobs = dashboard.Jobs(root / 'research.sqlite', scanner=lambda mint: {})
        with patch.dict(os.environ, env):
            self.assertFalse(jobs.once())                      # leases the scheduler lock, empty queue
            self.assertEqual(selected_paper_ledger(root)[0], root / 'paper-ledger.sqlite')
        broken = {k: v for k, v in env.items() if k != 'DESK_PAPER_SCHEDULER_IDENTITY'}
        with patch.dict(os.environ, broken), self.assertRaisesRegex(ValueError, 'identity required'):
            jobs.once()                                        # the failure the old wiring caused

    def test_every_created_entry_is_protected_regardless_of_umask(self):
        os.umask(0o000); self.addCleanup(os.umask, 0o022)
        manifest, root = self.applied()
        for path in [root, *root.rglob('*')]:
            mode = stat.S_IMODE(path.lstat().st_mode)
            self.assertEqual(mode, 0o700 if path.is_dir() else 0o600, path)


class RenderUnitsTests(WiringBase):
    """T32: the deploy/fresh templates, the manifest argv and the drop-ins must say the same thing."""

    def rendered(self, name='exp-1', **extra):
        manifest, root = self.applied(name, enable_held=True, **extra)
        out = self.base / f'units-{name}'
        fs.render_units(root, out, release_dir='/opt/solana-desk-releases/abc1234', state_dir='/var/lib/solana-desk-health')
        return manifest, root, out

    def test_template_exec_start_equals_the_manifest_argv_for_every_scheduler_unit(self):
        manifest, root, out = self.rendered()
        for unit in ('desk-paper-entry-dispatcher', 'desk-paper-held-cycle', 'desk-paper-monitor', 'desk-decisions',
                     'desk-dashboard'):
            text = (out / f'{unit}.service').read_text()
            (line,) = [l for l in text.splitlines() if l.startswith('ExecStart=')]
            self.assertEqual(exec_words(line[len('ExecStart='):]), [fs.PYTHON, *manifest['units'][unit]['argv']], unit)
            envs = [w for l in text.splitlines() if l.startswith('Environment=') for w in env_words(l[len('Environment='):])]
            self.assertEqual(sorted(envs), sorted(manifest['units'][unit]['environment']), unit)

    def test_backup_template_and_manifest_use_the_same_tool_root_and_single_destination(self):
        manifest, root, out = self.rendered()
        template = (out / 'desk-backup.service').read_text()
        spec = ' '.join(manifest['units']['desk-backup']['argv'])
        self.assertIn('tools.ops.backup', template)
        self.assertIn('from tools.ops import backup', spec)          # same tool, imported by the -c form
        for text in (template, spec):
            self.assertIn(str(root), text)
            self.assertIn(manifest['backup_dir'], text)
        self.assertIn(f'ReadWritePaths={manifest["backup_dir"]} {root}', template)

    def test_entry_unit_runs_execute_with_credentials_and_a_600s_timeout_everywhere(self):
        manifest, root, out = self.rendered()
        argv = manifest['units']['desk-paper-entry-dispatcher']['argv']
        self.assertEqual(argv[-2:], ['--execute', '--systemd-credentials'])
        self.assertIn('TimeoutStartSec=600', (out / 'desk-paper-entry-dispatcher.service').read_text())
        self.assertIn('TimeoutStartSec=120', (out / 'desk-paper-held-cycle.service').read_text())
        sections = manifest['unit_sections']
        self.assertEqual({u: s['timeout_start_sec'] for u, s in sections.items()},
                         {'desk-paper-entry-dispatcher': 600, 'desk-paper-held-cycle': 120, 'desk-paper-monitor': 60,
                          'desk-decisions': 60, 'desk-dashboard': None, 'desk-backup': 300})
        # the drop-in resets ExecStart AND carries the timeout, so a stock base unit (180 s) cannot win
        entry = fs.render_dropin('desk-paper-entry-dispatcher', manifest)
        self.assertIn('TimeoutStartSec=600', entry)
        self.assertNotIn('TimeoutStartSec', fs.render_dropin('desk-dashboard', manifest))

    def test_dashboard_template_has_scheduler_identity_and_ledger_selection(self):
        manifest, root, out = self.rendered()
        text = (out / 'desk-dashboard.service').read_text()
        self.assertIn('Environment=DESK_PAPER_SCHEDULER_IDENTITY=' + manifest['scheduler_identity'], text)
        self.assertIn(f'Environment=DESK_PAPER_LEDGER_DB={root}/paper-ledger.sqlite', text)
        self.assertIn(f'Environment=DESK_PAPER_SCHEDULER_LOCK={root}/paper-scheduler.lock', text)

    def test_latch_dropin_is_rendered_with_its_marker_first_and_the_fresh_paths(self):
        manifest, root, out = self.rendered()
        latch = (out / fs.LATCH_OUT).read_text().splitlines()
        self.assertEqual(latch[0], '# desk-entry-latch-managed v1')
        body = '\n'.join(latch)
        self.assertIn(f'--ledger {root}/paper-ledger.sqlite', body)
        self.assertIn(f'--config {self.config}', body)
        self.assertNotIn('<', re.sub(r'(?m)^#.*$', '', body))

    def test_every_unit_and_timer_template_is_filled_and_no_archived_path_appears(self):
        manifest, root, out = self.rendered()
        names = sorted(p.name for p in out.iterdir())
        self.assertIn('desk-healthcheck.timer', names)
        for path in out.rglob('*'):
            if path.is_file():
                self.assertNotRegex(re.sub(r'(?m)^#.*$', '', path.read_text()), r'<[A-Z_]+>', path.name)
        self.assertEqual(manifest['backup_dir'].rsplit('/', 1)[1], 'fresh-exp-1')

    def test_render_units_refuses_existing_out_unknown_placeholder_unsafe_value_and_identity_drift(self):
        manifest, root, out = self.rendered()
        kwargs = dict(release_dir='/opt/solana-desk-releases/abc1234')
        with self.assertRaisesRegex(fs.FreshStartError, 'must not exist'):
            fs.render_units(root, out, **kwargs)
        with self.assertRaisesRegex(fs.FreshStartError, 'quoting'):
            fs.render_units(root, self.base / 'o2', release_dir='/opt/has space/rel')
        templates = self.base / 'tpl'
        shutil.copytree(REPO / 'deploy' / 'fresh', templates)
        (templates / 'desk-dashboard.service').write_text('[Service]\nExecStart=<NOT_A_PLACEHOLDER_WE_KNOW>\n')
        with self.assertRaisesRegex(fs.FreshStartError, 'unfilled'):
            fs.render_units(root, self.base / 'o3', templates=templates, **kwargs)
        self.assertFalse((self.base / 'o3').exists())
        lock = root / fs.SCHEDULER_LOCK
        keep = root / 'inode-keeper'
        os.link(lock, keep)                                  # hold the old inode so the replacement cannot reuse it
        lock.unlink(); lock.write_text('')
        with self.assertRaisesRegex(fs.FreshStartError, 'identity'):
            fs.render_units(root, self.base / 'o4', **kwargs)

    def test_apply_creates_the_backup_dir_owned_0700_and_refuses_a_missing_parent_before_creating_anything(self):
        manifest, root = self.applied('exp-1')
        backup = Path(manifest['backup_dir'])
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        self.assertEqual(backup.stat().st_uid, os.geteuid())
        missing = self.base / 'no-such-parent' / 'fresh-x'
        with self.assertRaisesRegex(fs.FreshStartError, 'must exist'):
            fs.apply(fs.plan(**self.args('exp-2', backup_dir=str(missing))), service_user=self.user)
        self.assertFalse((self.base / 'exp-2').exists())
        self.assertFalse(missing.exists())

    def test_existing_backup_dir_with_loose_mode_is_refused(self):
        loose = self.base / 'backups' / 'fresh-exp-3'
        (self.base / 'backups').mkdir(exist_ok=True)
        loose.mkdir(mode=0o755); os.chmod(loose, 0o755)
        with self.assertRaisesRegex(fs.FreshStartError, '0700'):
            fs.apply(fs.plan(**self.args('exp-3', backup_dir=str(loose))), service_user=self.user)

    def test_cli_render_units(self):
        manifest, root = self.applied('exp-1', enable_held=True)
        code, out = self.cli('render-units', '--root', str(root), '--out', str(self.base / 'cli-units'),
                             '--release-dir', '/opt/solana-desk-releases/abc1234')
        self.assertEqual((code, out['status']), (0, 'RENDERED'))
        self.assertIn(fs.LATCH_OUT, out['files'])
        code, out = self.cli('render-units', '--root', str(root), '--out', str(self.base / 'cli-units'),
                             '--release-dir', '/opt/solana-desk-releases/abc1234')
        self.assertEqual(code, 2)


class DropinTests(WiringBase):
    def render(self, name='exp-1', **extra):
        manifest, root = self.applied(name, **extra)
        out = self.base / (name + '-dropins')
        result = fs.render_dropins(root, out)
        return manifest, root, out, result

    def test_one_conf_per_unit_marker_first_and_t13_unit_keys_unchanged(self):
        manifest, root, out, result = self.render()
        self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(f'{u}.conf' for u in UNITS))
        self.assertEqual(set(manifest['units']), set(UNITS))                 # no ".service" suffix
        for unit in UNITS:
            text = (out / f'{unit}.conf').read_text()
            self.assertEqual(text.splitlines()[0], fs.DROPIN_MARKER)
            self.assertEqual(set(manifest['units'][unit]), {'environment', 'argv'})
            self.assertEqual(stat.S_IMODE((out / f'{unit}.conf').stat().st_mode), 0o644)

    def test_old_conditions_and_exec_start_are_replaced_not_appended(self):
        manifest, root, out, _ = self.render()
        store = {'desk-paper-entry-dispatcher': 'entry-dispatch/dispatch.sqlite', 'desk-paper-held-cycle': 'paper-ledger.sqlite',
                 'desk-paper-monitor': 'paper-ledger.sqlite', 'desk-decisions': 'research.sqlite'}
        for unit in UNITS:
            base = base_unit(unit)
            effective = settings(base, (out / f'{unit}.conf').read_text())
            if unit in store:
                self.assertEqual(effective[('Unit', 'ConditionPathExists')], [str(root / store[unit])], unit)
                self.assertIn('ConditionPathExists=/var/lib/solana-desk', base)   # the template really had one
            self.assertEqual(len(effective[('Service', 'ExecStart')]), 1, unit)
            self.assertTrue(effective[('Service', 'ExecStart')][0].startswith(fs.PYTHON + ' '))
            self.assertNotIn('/var/lib/solana-desk/', ' '.join(sum((v for k, v in effective.items() if k[1] in
                              ('ExecStart', 'Environment', 'ConditionPathExists')), [])), unit)
            self.assertIn(str(root), effective[('Service', 'ReadWritePaths')][-1].split())

    def test_exec_start_round_trips_through_systemd_word_rules(self):
        manifest, root, out, _ = self.render()
        for unit in UNITS:
            effective = settings(base_unit(unit), (out / f'{unit}.conf').read_text())
            self.assertEqual(exec_words(effective[('Service', 'ExecStart')][0]), [fs.PYTHON, *manifest['units'][unit]['argv']], unit)
            env = [w for line in effective[('Service', 'Environment')] for w in env_words(line)]
            self.assertEqual(env, manifest['units'][unit]['environment'], unit)

    def test_hostile_paths_are_quoted_and_percent_escaped_and_credentials_specifier_survives(self):
        weird = self.base / 'exp %d "q" $HOME x'
        value = fs.plan(**self.args('placeholder'), )
        value = fs.plan(**{**self.args('placeholder'), 'root': str(weird)})
        manifest = {'units': fs.unit_arguments(value, scheduler_identity='1:2'), 'unit_sections': fs.unit_sections(value),
                    'plan_hash': 'h', 'config_version': 'v'}
        for unit in UNITS:
            text = fs.render_dropin(unit, manifest)
            effective = settings(base_unit(unit), text)
            self.assertEqual(exec_words(effective[('Service', 'ExecStart')][0]), [fs.PYTHON, *manifest['units'][unit]['argv']], unit)
            self.assertEqual([w for line in effective[('Service', 'Environment')] for w in env_words(line)],
                             manifest['units'][unit]['environment'], unit)
            self.assertNotIn('\n', text.split('ExecStart=\n', 1)[1].rstrip('\n'))
        dash = fs.render_dropin('desk-dashboard', manifest)
        self.assertIn(' %d/provider-keys.json ', dash)                   # intentional credentials specifier
        self.assertNotIn('%%d/provider-keys', dash)
        self.assertIn('%%d', fs.render_dropin('desk-paper-monitor', manifest))   # a literal % in a path is escaped
        with self.assertRaises(fs.FreshStartError):
            fs._word('bad\nword')

    def test_backup_unit_runs_the_real_tool_against_the_new_root_with_a_dated_destination(self):
        backups = self.base / 'backups'; backups.mkdir(mode=0o700)
        manifest, root = self.applied(backup_dir=str(backups))
        argv = manifest['units']['desk-backup']['argv']
        self.assertEqual(argv[0], '-c')
        done = subprocess.run([sys.executable, *argv], cwd=REPO, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        made = [p for p in backups.iterdir()]
        self.assertEqual(len(made), 1)
        self.assertRegex(made[0].name, r'^daily-\d{8}T\d{6}Z$')
        recorded = json.loads((made[0] / 'manifest.json').read_text())
        self.assertEqual(sorted(recorded['databases']),
                         ['entry-dispatch/dispatch.sqlite', 'evidence.sqlite', 'paper-decisions.sqlite',
                          'paper-ledger.sqlite', 'research.sqlite'])
        # the backup unit may write the root (sqlite lock files) and the destination, nothing else
        self.assertEqual(manifest['unit_sections']['desk-backup']['read_write_paths'], [str(root), str(backups)])

    def test_backup_dir_inside_root_is_refused(self):
        with self.assertRaisesRegex(fs.FreshStartError, 'outside'):
            fs.plan(**self.args('exp-1', backup_dir=str(self.base / 'exp-1' / 'b')))

    def test_render_refuses_existing_out_symlink_out_missing_manifest_and_identity_drift(self):
        manifest, root = self.applied()
        out = self.base / 'out'; out.mkdir()
        with self.assertRaisesRegex(fs.FreshStartError, 'must not exist'):
            fs.render_dropins(root, out)
        (self.base / 'link').symlink_to(out)
        with self.assertRaisesRegex(fs.FreshStartError, 'symlink|must not exist'):
            fs.render_dropins(root, self.base / 'link')
        with self.assertRaisesRegex(fs.FreshStartError, 'manifest'):
            fs.render_dropins(self.base, self.base / 'o2')
        lock = root / fs.SCHEDULER_LOCK
        replacement = root / 'replacement.lock'            # created while the old lock exists: guaranteed new inode
        replacement.write_text(''); os.chmod(replacement, 0o600)
        os.replace(replacement, lock)                      # units pinned to the old identity would die at the lease
        with self.assertRaisesRegex(fs.FreshStartError, 'identity'):
            fs.render_dropins(root, self.base / 'o3')
        self.assertFalse((self.base / 'o3').exists())

    def test_cli_render_dropins(self):
        manifest, root = self.applied()
        code, out = self.cli('render-dropins', '--root', str(root), '--out', str(self.base / 'cli-out'), '--marker', '# custom marker')
        self.assertEqual((code, out['status']), (0, 'RENDERED'))
        self.assertEqual((self.base / 'cli-out' / 'desk-decisions.conf').read_text().splitlines()[0], '# custom marker')
        code, out = self.cli('render-dropins', '--root', str(root), '--out', str(self.base / 'cli-out2'), '--marker', 'no hash')
        self.assertEqual((code, out['status']), (2, 'BLOCKED'))


class HeldCycleTests(WiringBase):
    def test_held_unit_keeps_its_dependency_blocker_unless_enabled_explicitly(self):
        default = fs.plan(**self.args('exp-1'))
        argv = default['units']['desk-paper-held-cycle']['argv']
        self.assertEqual(argv[-2:], ['--dependency-blocker', fs.HELD_BLOCKER])
        self.assertIn('does NOT monitor', ' '.join(default['notes']))
        enabled = fs.plan(**self.args('exp-1', enable_held=True))
        self.assertNotIn('--dependency-blocker', enabled['units']['desk-paper-held-cycle']['argv'])
        self.assertNotEqual(digest(default), digest(enabled))              # the approved hash binds the choice
        self.assertIn('WITHOUT', ' '.join(enabled['notes']))

    def test_cli_flag_and_non_bool_rejected(self):
        base = ['plan', '--root', str(self.base / 'exp-1'), '--config', str(self.config), '--pacing-db', str(self.pacer),
                '--discovery-db', str(self.discovery)]
        self.assertIn('--dependency-blocker', json.dumps(self.cli(*base)[1]['plan']['units']['desk-paper-held-cycle']['argv']))
        self.assertNotIn('--dependency-blocker', json.dumps(self.cli(*base, '--enable-held')[1]['plan']['units']['desk-paper-held-cycle']['argv']))
        with self.assertRaises(fs.FreshStartError):
            fs.plan(**self.args('exp-1', enable_held='yes'))


class ServiceUserTests(WiringBase):
    def test_refuses_when_not_the_service_user_and_creates_nothing(self):
        other = 'nobody' if pwd.getpwnam('nobody').pw_uid != os.geteuid() else 'root'
        value = fs.plan(**self.args('exp-1'))
        with self.assertRaisesRegex(fs.FreshStartError, '(?i)service user'):
            fs.apply(value, service_user=other)
        with self.assertRaisesRegex(fs.FreshStartError, '(?i)service user'):
            fs.apply(value)                                   # default solana-desk does not exist / is not us
        self.assertFalse((self.base / 'exp-1').exists())

    def test_chown_must_name_the_service_user_and_user_must_exist(self):
        value = fs.plan(**self.args('exp-1'))
        with self.assertRaisesRegex(fs.FreshStartError, 'chown'):
            fs.apply(value, service_user=self.user, chown='someone-else')
        with self.assertRaisesRegex(fs.FreshStartError, 'does not exist'):
            fs.apply(value, service_user='no-such-user-t13f')
        self.assertFalse((self.base / 'exp-1').exists())

    @unittest.skipUnless(os.geteuid() == 0 and 'nobody' in {p.pw_name for p in pwd.getpwall()}, 'needs root and a nobody user')
    def test_root_with_chown_hands_every_created_entry_to_the_service_user_and_verifies(self):
        nobody = pwd.getpwnam('nobody')
        # nobody must be able to traverse to the root only if it later runs the units; ownership is what is checked
        manifest = fs.apply(fs.plan(**self.args('exp-1')), service_user='nobody', chown='nobody')
        root = self.base / 'exp-1'
        entries = [root, *root.rglob('*')]
        self.assertGreaterEqual(len(entries), 12)
        for path in entries:
            info = path.lstat()
            self.assertEqual((info.st_uid, info.st_gid), (nobody.pw_uid, nobody.pw_gid), path)
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o700 if path.is_dir() else 0o600, path)
        names = {p.name for p in entries}
        self.assertTrue({'paper-scheduler.lock', 'paper-ledger.sqlite.paper-cycle.lock', 'fresh-start-manifest.json'} <= names)
        lock = root / fs.SCHEDULER_LOCK
        self.assertEqual(manifest['scheduler_identity'], '%d:%d' % (lock.stat().st_dev, lock.stat().st_ino))

    @unittest.skipUnless(os.geteuid() == 0 and 'nobody' in {p.pw_name for p in pwd.getpwall()}, 'needs root and a nobody user')
    def test_failed_ownership_verification_is_reported_and_the_root_is_retained(self):
        with patch.object(fs.os, 'lchown'), self.assertRaisesRegex(fs.FreshStartError, 'verification failed'):
            fs.apply(fs.plan(**self.args('exp-1')), service_user='nobody', chown='nobody')
        self.assertTrue((self.base / 'exp-1').exists())      # never reused: a second apply refuses the existing root
        with self.assertRaisesRegex(fs.FreshStartError, 'must not exist'):
            fs.plan(**self.args('exp-1'))


class RotationLockAndFlatnessTests(WiringBase):
    def setUp(self):
        super().setUp()
        self.manifest, self.old = self.applied('old')
        self.before = fs.tree_digest(self.old)

    def old_lock(self):
        return str(self.old / 'paper-ledger.sqlite') + '.paper-cycle.lock'

    def hold(self):
        fd = os.open(self.old_lock(), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        return fd

    def rotate_cli(self, *extra, execute=False, approved=None):
        argv = ['rotate', '--from', str(self.old), '--to', str(self.base / 'new'), '--config', str(self.config),
                '--pacing-db', str(self.pacer), '--discovery-db', str(self.discovery), '--service-user', self.user, *extra]
        if execute:
            argv += ['--execute', '--approved-plan-hash', approved or '0' * 64]
        return self.cli(*argv)

    def test_busy_old_ledger_lock_refuses_check_dry_run_and_rotation(self):
        self.hold()
        with self.assertRaisesRegex(fs.FreshStartError, 'busy'):
            fs.verify_flat(self.old)
        code, out = self.rotate_cli()
        self.assertEqual(code, 2); self.assertIn('busy', out['reason'])
        code, out = self.rotate_cli(execute=True)
        self.assertEqual(code, 2)
        self.assertFalse((self.base / 'new').exists())
        self.assertEqual(fs.tree_digest(self.old), self.before)

    def test_old_ledger_lock_is_held_across_the_whole_bootstrap(self):
        seen = []
        real_apply = fs.apply

        def spy(value, **kwargs):
            fd = os.open(self.old_lock(), os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append('held')
            finally:
                os.close(fd)
            return real_apply(value, **kwargs)
        code, dry = self.rotate_cli()
        self.assertEqual(code, 0)
        with patch.object(fs, 'apply', side_effect=spy):
            code, out = self.rotate_cli(execute=True, approved=dry['plan_hash'])
        self.assertEqual((code, out['status']), (0, 'APPLIED'))
        self.assertEqual(seen, ['held'])
        self.assertEqual(fs.tree_digest(self.old), self.before)
        # released afterwards
        fd = self.hold(); self.assertTrue(fd)

    def test_missing_lock_file_is_never_created_in_the_old_root(self):
        os.unlink(self.old_lock())
        before = fs.tree_digest(self.old)
        with self.assertRaisesRegex(fs.FreshStartError, 'lock file missing'):
            fs.verify_flat(self.old)
        self.assertFalse(os.path.exists(self.old_lock()))
        self.assertEqual(fs.tree_digest(self.old), before)

    def test_rotate_execute_binds_the_flat_proof_into_the_approved_hash(self):
        code, dry = self.rotate_cli()
        self.assertEqual(code, 0)
        self.assertIn('old_flat_proof', dry['plan'])
        code, out = self.rotate_cli(execute=True, approved='1' * 64)
        self.assertEqual(code, 2)
        self.assertFalse((self.base / 'new').exists())
        code, out = self.rotate_cli(execute=True, approved=dry['plan_hash'])
        self.assertEqual((code, out['status']), (0, 'APPLIED'))
        written = json.loads((self.base / 'new' / fs.MANIFEST).read_text())
        self.assertEqual(written['plan_hash'], dry['plan_hash'])

    def insert_pass(self, outcome):
        with closing(sqlite3.connect(self.old / 'evidence.sqlite')) as c:
            c.execute('CREATE TABLE IF NOT EXISTS paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,?)', ('p1', 'i1', outcome)); c.commit()

    def test_unresolved_observation_pass_blocks_rotation_but_a_resolved_one_does_not(self):
        self.insert_pass('a' * 64)
        self.assertEqual(fs.verify_flat(self.old)['positions'], 0)
        with closing(sqlite3.connect(self.old / 'evidence.sqlite')) as c:
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)', ('p2', 'i2')); c.commit()
        with self.assertRaisesRegex(fs.FreshStartError, 'unresolved observation pass'):
            fs.verify_flat(self.old)

    def test_dispatcher_intent_without_result_blocks_rotation(self):
        journal = self.old / 'entry-dispatch' / 'dispatch.sqlite'
        with closing(sqlite3.connect(journal)) as c:
            c.execute("INSERT INTO intents VALUES('i1','MINT','SIG','{}','h')"); c.commit()
        with self.assertRaisesRegex(fs.FreshStartError, 'intent without a result'):
            fs.verify_flat(self.old)
        with closing(sqlite3.connect(journal)) as c:
            c.execute("INSERT INTO results VALUES('i1','{}','h')"); c.commit()
        self.assertEqual(fs.verify_flat(self.old)['mode'], 'RUNNING')

    def test_monitoring_reservation_without_outcome_blocks_rotation(self):
        with closing(sqlite3.connect(self.old / 'evidence.sqlite')) as c:
            c.execute("INSERT INTO paper_monitoring_reservations VALUES(1,1.0,'s','m','c','getSlot','p')"); c.commit()
        with self.assertRaisesRegex(fs.FreshStartError, 'reservation without an outcome'):
            fs.verify_flat(self.old)

    def test_exit_blocked_position_is_named_in_the_refusal(self):
        state = {'positions': {'M': {'exit_blocked': 'EXACT_FRESH_SELL_QUOTE_REQUIRED'}}, 'mode': 'EXIT_ONLY'}
        with patch.object(fs, 'validate_checkpoint', return_value=state):
            with self.assertRaisesRegex(fs.FreshStartError, r'exit_blocked'):
                fs.verify_flat(self.old)

    def test_wal_on_any_store_is_refused(self):
        for name in ('evidence.sqlite', 'entry-dispatch/dispatch.sqlite'):
            wal = Path(str(self.old / name) + '-wal')
            wal.write_bytes(b'x')
            with self.assertRaisesRegex(fs.FreshStartError, 'WAL'):
                fs.verify_flat(self.old)
            wal.unlink()

    def test_roots_without_a_manifest_need_explicit_paths(self):
        foreign = self.base / 'foreign'
        shutil.copytree(self.old, foreign, ignore=shutil.ignore_patterns(fs.MANIFEST))
        with self.assertRaisesRegex(fs.FreshStartError, 'pass --old-ledger'):
            fs.verify_flat(foreign)
        explicit = dict(ledger=str(foreign / 'paper-ledger.sqlite'), evidence=str(foreign / 'evidence.sqlite'),
                        journal=str(foreign / 'entry-dispatch' / 'dispatch.sqlite'))
        self.assertEqual(fs.verify_flat(foreign, **explicit)['mode'], 'RUNNING')
        with self.assertRaises(fs.FreshStartError):
            fs.verify_flat(foreign, **{**explicit, 'ledger': 'relative.sqlite'})
        (self.base / 'ln.sqlite').symlink_to(foreign / 'paper-ledger.sqlite')
        with self.assertRaisesRegex(fs.FreshStartError, 'symlink'):
            fs.verify_flat(foreign, **{**explicit, 'ledger': str(self.base / 'ln.sqlite')})
        # the full CLI path with explicit stores
        code, out = self.cli('rotate', '--from', str(foreign), '--to', str(self.base / 'new2'), '--config', str(self.config),
                             '--pacing-db', str(self.pacer), '--discovery-db', str(self.discovery),
                             '--old-ledger', explicit['ledger'], '--old-evidence', explicit['evidence'],
                             '--old-journal', explicit['journal'])
        self.assertEqual((code, out['status']), (0, 'DRY_RUN'))


if __name__ == '__main__':
    unittest.main()
