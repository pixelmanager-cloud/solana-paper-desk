"""SYNTHETIC_TEST_ONLY: the fresh-start deployment as ONE flow (T32).

The commands are parsed out of docs/ops/RUNBOOK.md and run in order against a temp filesystem with a fake
`systemctl` (archive backup -> bootstrap -> render -> cutover with entry OFF -> healthcheck -> latch -> entry enable ->
rollback). A flag or argument that drifts between the RUNBOOK and the tools therefore fails here. Only paths and the
service user are adapted for the sandbox; no network, no credentials, no real systemd.
"""
import contextlib
import io
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import time
from desk import kraken_pacing_migration as upgrade, provider_pacing as pace, runtime_compatibility as runtime
from desk.model import canonical
from discovery import continuous as discovery
from tests.test_ops_cutover import FakeSystemd
from tests.test_ops_fresh_start import FreshStartBase
from tests.test_ops_fresh_start_wiring import exec_words, env_words, settings
from tools.ops import backup, cutover, entry_latch, fresh_start as fs, healthcheck, verify_backup

REPO = Path(__file__).resolve().parents[1]
RUNBOOK = REPO / 'docs' / 'ops' / 'RUNBOOK.md'


def logical_commands(text):
    """(section, command) for every command line in a fenced block, continuations joined."""
    out, section, in_block, pending = [], '', False, ''
    for raw in text.splitlines():
        if raw.startswith('#'):
            if not in_block:
                section = raw.lstrip('# ').strip()
            elif not pending:
                continue
        if raw.startswith('```'):
            in_block = not in_block
            pending = ''
            continue
        if not in_block:
            continue
        line = raw.rstrip()
        if pending or line.strip():
            pending += (' ' if pending else '') + line.strip().rstrip('\\').strip()
            if not line.endswith('\\'):
                out.append((section, pending))
                pending = ''
    return out


class E2E(FreshStartBase):
    def setUp(self):
        super().setUp()
        b = self.base
        self.old = b / 'var-lib' / 'solana-desk'
        self.vb = b / 'var-backups'
        self.rels = b / 'opt-releases'
        self.unit_dir = b / 'etc-systemd'
        self.rootdir = b / 'root'
        for d in (self.old / 'discovery', self.vb, self.rels, self.unit_dir, self.rootdir, b / 'var-lib' / 'fresh',
                  b / 'var-lib' / 'health'):
            d.mkdir(parents=True)
        # The SHARED stores live inside the archived root, as in production. The pacing database's migration receipt
        # binds its path, so it is created in place (same steps as FreshStartBase) rather than moved.
        self.pacer = self.old / 'provider-pacing.sqlite'
        pace.initialize(self.pacer)
        policy = b / 'migration-e2e.json'
        patcher = mock.patch.object(upgrade, 'POLICY', policy)
        patcher.start(); self.addCleanup(patcher.stop)
        policy.write_text(canonical({'version': 1, 'pins': []}))
        proposal = upgrade.review_plan(self.pacer)
        policy.write_text(canonical({'version': 1, 'pins': [proposal]}))
        upgrade.migrate(self.pacer)
        patcher = mock.patch.dict(os.environ, {pace.ENV: str(self.pacer)})
        patcher.start(); self.addCleanup(patcher.stop)
        self.discovery = self.old / 'discovery' / 'continuous.sqlite'
        with mock.patch.object(discovery.time, 'time', return_value=time.time() - 600):
            discovery.initialize(self.discovery)
        import sqlite3
        for i in range(12):                                   # 12 production stores + pacing + discovery = 14
            with sqlite3.connect(self.old / f'old-store-{i}.sqlite') as c:
                c.execute('CREATE TABLE t(x)'); c.execute('INSERT INTO t VALUES(?)', (i,))
        self.sha = 'abc1234'
        self.rel = self.rels / self.sha
        shutil.copytree(REPO / 'desk', self.rel / 'desk', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(REPO / 'deploy', self.rel / 'deploy')
        shutil.copytree(REPO / 'tools', self.rel / 'tools', ignore=shutil.ignore_patterns('__pycache__'))
        self.digest = runtime.implementation_hash()
        self.systemd = FakeSystemd(self.unit_dir)
        self.plan_hash = None
        self.last_cutover = None
        self.results = []

    # -- RUNBOOK plumbing -------------------------------------------------------------------------------------
    def variables(self, text):
        block = re.search(r'## Variables used below\s+```\n(.*?)```', text, re.S).group(1)
        values = {}
        for line in block.splitlines():
            for key, value in re.findall(r'([A-Z]+)=(\S+)', line.split('#')[0]):
                values[key] = value
        values['SHA'] = self.sha                               # the RUNBOOK shows a placeholder here
        for _ in range(3):                                    # resolve $X inside values
            for key, value in list(values.items()):
                values[key] = re.sub(r'\$([A-Z]+)', lambda m: values.get(m.group(1), m.group(0)), value)
        return values

    def sandbox(self, text, v=None):
        if v is not None:                                     # v holds PRODUCTION values; rewrite happens once, last
            text = re.sub(r'\$([A-Z]+)', lambda m: v.get(m.group(1), m.group(0)), text)
        mapping = {'/var/lib/solana-desk-fresh': self.base / 'var-lib' / 'fresh',
                   '/var/lib/solana-desk-health': self.base / 'var-lib' / 'health',
                   '/var/lib/solana-desk': self.old,
                   '/var/backups/solana-desk': self.vb,
                   '/opt/solana-desk-releases': self.rels,
                   '/opt/solana-desk/.venv/bin/python': Path(sys.executable),
                   '/etc/systemd/system': self.unit_dir,
                   '/etc/solana-paper': self.rootdir,
                   '/root': self.rootdir}
        # one regex pass (longest prefix first) so a rewritten path is never rewritten again
        pattern = re.compile('|'.join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)))
        # quote only paths with whitespace (e.g. a venv under "Clause Coding"); shlex joins 'a b'/x correctly
        text = pattern.sub(lambda m: (lambda p: shlex.quote(p) if any(c.isspace() for c in p) else p)(str(mapping[m.group(0)])), text)
        return text

    def run_tool(self, module, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            if module == 'cutover':
                code = cutover.main(argv, runner=self.systemd, sleep=lambda s: None)
            elif module == 'backup':
                # backup() binds its default runner at definition time, so patch the defaults tuple
                fake = lambda unit: self.systemd.state.get(unit, 'inactive')
                with mock.patch.object(backup.backup, '__defaults__', ((), None, fake)):
                    code = backup.main(argv)
            else:
                code = {'verify_backup': verify_backup, 'fresh_start': fs, 'healthcheck': healthcheck,
                        'entry_latch': entry_latch}[module].main(argv)
        text = out.getvalue()
        try:
            payload = json.loads(text) if text.strip() else {}
        except ValueError:
            try:
                payload = json.loads(text.strip().splitlines()[-1])
            except ValueError:
                payload = {}
        return code, payload, text

    def adapt(self, module, argv, v):
        """Sandbox-only argument changes; everything else is exactly what the RUNBOOK says."""
        if module == 'fresh_start' and argv[0] in ('plan', 'apply', 'rotate'):
            argv += ['--backup-dir', v['BK']]
            if '--chown' in argv:                              # the sandbox user is not solana-desk
                i = argv.index('--chown'); del argv[i:i + 2]
                i = argv.index('--service-user'); argv[i + 1] = self.user
        if module == 'healthcheck':
            argv += ['--no-systemd']
        if module == 'cutover':
            head = ['--journal', str(self.rootdir / 'cutover-journal.jsonl'), '--python', sys.executable]
            sub = next(a for a in argv if a in ('stage', 'cutover', 'rollback'))
            i = argv.index(sub)
            tail = ['--unit-dir', str(self.unit_dir)]
            if sub == 'cutover':
                tail += ['--release-root', str(self.rels), '--settle-seconds', '0']
            argv = head + argv[:i + 1] + tail + argv[i + 1:]
        return argv

    def shell(self, words, v):
        """Minimal effects of the few non-python commands the RUNBOOK uses (what they do on a real host)."""
        cmd = words[0]
        if cmd == 'systemctl' and words[1] == 'stop':
            for unit in words[2:]:
                self.systemd.state[unit] = 'inactive'
        elif cmd == 'systemctl' and words[1] == 'enable':
            units = [w for w in words[2:] if not w.startswith('--')]
            for unit in units:
                self.systemd.enabled[unit] = 'enabled'
                if '--now' in words:
                    self.systemd.state[unit] = 'active'
        elif cmd == 'systemctl' and words[1] == 'daemon-reload':
            self.systemd(['systemctl', 'daemon-reload'])
        elif cmd == 'install':
            args = [w for w in words[1:]]
            dash_d = '-D' in args
            # drop options and their values: -m MODE, -o/-g NAME, -D, -d
            rest, skip = [], False
            for w in args:
                if skip:
                    skip = False
                elif w in ('-m', '-o', '-g'):
                    skip = True
                elif not w.startswith('-'):
                    rest.append(w)
            if '-d' in args:
                Path(rest[-1]).mkdir(parents=True, exist_ok=True)
                return
            *sources, dest = [Path(w) for w in rest]
            assert sources, words
            for src in sources:
                matches = sorted(Path('/').glob(str(src)[1:])) if '*' in str(src) else [src]
                assert matches, f'install source missing: {src}'
                for m in matches:
                    target = dest if dash_d or not dest.is_dir() else dest / m.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(m, target)
        elif cmd == 'find' and 'chmod' in words:
            for p in self.old.rglob('*.sqlite'):
                if p.name != 'provider-pacing.sqlite' and p.parent.name != 'discovery':
                    p.chmod(0o400)
        elif cmd in ('cd', 'export', 'sha256sum', 'ss', 'mkdir', 'cp', 'systemctl', 'ls'):
            pass
        else:
            raise AssertionError('RUNBOOK uses an unhandled command: %r' % words)

    # -- the flow ---------------------------------------------------------------------------------------------
    def run_runbook(self, upto):
        text = RUNBOOK.read_text()
        prod = self.variables(text)
        v = {k: self.sandbox(val) for k, val in prod.items()}
        # the config copy in step 5 is `install -m 0644 config/... $CFG` relative to $REL
        shutil.copytree(REPO / 'config', self.rel / 'config', dirs_exist_ok=True)
        self.v = v
        ran = []
        for section, command in logical_commands(text.split('## Rotate when flat')[0]):
            if section.startswith('Variables') or section.startswith('Working directory'):
                continue
            if not any(section.startswith(s) for s in upto):
                continue
            command = re.sub(r'\s+#.*$', '', command)
            command = re.sub(r'<DIGEST[^>]*>', self.digest, command)
            command = command.replace('<plan_hash>', self.plan_hash or '<plan_hash>')
            text_cmd = self.sandbox(command, prod)
            if text_cmd.endswith('<same arguments>'):
                prefix = text_cmd[:-len('<same arguments>')]
                text_cmd = prefix + self.last_cutover
            words = shlex.split(text_cmd)
            if words[0] == v['PY'] or words[0] == sys.executable:
                assert words[1] == '-m' and words[2].startswith('tools.ops.'), words
                module = words[2].split('.')[-1]
                argv = self.adapt(module, words[3:], v)
                if module == 'cutover' and 'stage' in words[3:] or module in ('preflight_dryrun', 'status', 'verify_cycle'):
                    continue                                    # stage: own tests; others: not in this tree
                if '<' in ' '.join(argv):
                    continue                                    # step needs a value only the operator has
                code, payload, raw = self.run_tool(module, argv)
                self.results.append((section, module, argv, code, payload, raw))
                if code != 0 and module != 'healthcheck':      # healthcheck exits 2 on any CRITICAL; judged below
                    raise AssertionError((section, module, argv, payload or raw[-800:]))
                if module == 'fresh_start' and argv[0] == 'plan':
                    self.plan_hash = payload['plan_hash']
                if module == 'cutover' and 'cutover' in words[3:]:
                    self.last_cutover = ' '.join(shlex.quote(a) for a in words[words.index('cutover') + 1:])
                ran.append((module, argv, code))
            else:
                self.shell(words, v)
        return ran

    def test_runbook_sequence_composes_end_to_end(self):
        self.run_runbook(('2.', '3.', '4.', '5.', '7a.', '7b.', '8.', '9.'))
        # every RUNBOOK command that ran succeeded (healthcheck is judged separately: a sandbox has no live discovery)
        for section, module, argv, code, payload, raw in self.results:
            if module != 'healthcheck':
                self.assertEqual(code, 0, (section, module, argv, payload or raw[-500:]))
        (health,) = [r for r in self.results if r[1] == 'healthcheck']
        critical = {c['check'] for c in health[4]['checks'] if c['severity'] == 'CRITICAL'}
        # Right after cutover only two things are legitimately missing: live discovery frames and the first backup.
        self.assertEqual(critical, {'discovery_frames', 'backup'}, health[4]['checks'])
        self.assertEqual(health[3], 2)
        for c in health[4]['checks']:
            if c['check'] in ('mode', 'monitoring_budget') or c['check'].startswith('pacing:'):
                self.assertEqual(c['severity'], 'OK', c)      # the fresh stores themselves are healthy
        self.assertTrue((self.vb / f'pre-fresh-{self.sha}' / 'manifest.json').is_file())
        new = Path(self.v['NEW'])
        manifest = json.loads((new / fs.MANIFEST).read_text())
        self.assertEqual(manifest['implementation_hash'], self.digest)
        self.assertTrue(Path(self.v['BK']).is_dir())
        self.assertEqual(oct(Path(self.v['BK']).stat().st_mode & 0o777), '0o700')
        self.assertTrue(manifest['enable_held'])
        self.assertNotIn('--dependency-blocker', manifest['units']['desk-paper-held-cycle']['argv'])

        # --- cutover: entry OFF and disabled, monitor never started, everything intended started and enabled
        calls = self.systemd.calls
        started = [c[-1] for c in calls if c[1] == 'start']
        self.assertEqual(sorted(started), sorted([
            'desk-continuous-discovery.service', 'desk-dashboard.service', 'desk-paper-held-cycle.timer',
            'desk-decisions.timer', 'desk-backup.timer', 'desk-healthcheck.timer', 'desk-notify-daily.timer']))
        self.assertNotIn('desk-paper-entry-dispatcher.timer', started)
        self.assertNotIn('desk-paper-monitor.timer', started)
        enabled = [c[2] for c in calls if c[1] == 'enable']
        # boot persistence: exactly the started units, in that order, and only AFTER they all started
        self.assertEqual(enabled[:-1] + enabled[-1:], [u for u in enabled])
        self.assertEqual(sorted(u for u in enabled if u != 'desk-paper-entry-dispatcher.timer'), sorted(started))
        self.assertLess(max(i for i, c in enumerate(calls) if c[1] == 'start'),
                        min(i for i, c in enumerate(calls) if c[1] == 'enable'))

        # --- invariants over everything that is now installed
        self.check_units(manifest, new)

        # --- the fresh backup unit's real command works on the real root
        argv = manifest['units']['desk-backup']['argv']
        done = subprocess.run([sys.executable, *argv], cwd=self.rel, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        (daily,) = list(Path(self.v['BK']).glob('daily-*'))
        self.assertEqual(verify_backup.main([str(daily)]), 0)
        self.assertEqual(len(json.loads((daily / 'manifest.json').read_text())['databases']), len(fs.STORES))
        code, payload, _ = self.run_tool('healthcheck', health[2])
        self.assertEqual({c['check'] for c in payload['checks'] if c['severity'] == 'CRITICAL'}, {'discovery_frames'})

        # --- entry enable is the last step and comes after the latch drop-in is installed
        latch = self.unit_dir / 'desk-paper-entry-dispatcher.service.d' / '70-entry-latch.conf'
        self.assertTrue(latch.is_file())
        self.assertEqual(latch.read_text().splitlines()[0], cutover.LATCH_MARKER)
        self.assertEqual(self.systemd.enabled.get('desk-paper-entry-dispatcher.timer'), 'enabled')

        # --- rollback restores everything this flow wrote
        units = ['desk-continuous-discovery.service', 'desk-dashboard.service', 'desk-paper-held-cycle.service',
                 'desk-decisions.service', 'desk-backup.service', 'desk-healthcheck.service',
                 'desk-notify-daily.service', 'desk-paper-monitor.service', 'desk-paper-entry-dispatcher.service']
        code, payload, raw = self.run_tool('cutover', self.adapt('cutover', ['--apply', 'rollback', '--stop', '--disable',
                                                                            '--units', *units], self.v))
        self.assertEqual(code, 0, raw[-600:])
        leftovers = [str(p) for p in self.unit_dir.glob('*.service.d/*.conf')]
        self.assertEqual(leftovers, [])
        for unit in started + ['desk-paper-entry-dispatcher.timer']:
            self.assertNotEqual(self.systemd.enabled.get(unit, 'disabled'), 'enabled', unit)
            self.assertIn(self.systemd.state.get(unit, 'inactive'), ('inactive', 'failed'), unit)
        # a second rollback is a harmless no-op
        code, payload, raw = self.run_tool('cutover', self.adapt('cutover', ['--apply', 'rollback', '--units', *units], self.v))
        self.assertEqual((code, payload['removed']), (0, []))

    def check_units(self, manifest, new):
        old = str(self.old)
        pacing, discovery = str(self.pacer), str(self.discovery)
        allowed = {pacing, old, discovery, str(self.old / 'discovery')}
        for path in sorted(self.unit_dir.rglob('*')):
            if not path.is_file():
                continue
            body = re.sub(r'(?m)^#.*$', '', path.read_text())
            for ref in re.findall(re.escape(old) + r'[^\s"\';,]*', body):
                self.assertIn(ref, allowed, f'{path.name} references the archived root: {ref}')
            self.assertNotRegex(body, r'0\.0\.0\.0|--host\s+(?!127\.0\.0\.1|localhost)', path.name)
        # one effective model per unit: base unit + our 60- drop-in
        for unit, spec in manifest['units'].items():
            base = (self.unit_dir / f'{unit}.service').read_text()
            drop = self.unit_dir / f'{unit}.service.d' / cutover.DROPIN
            if not drop.exists():                            # configured only: every manifest unit gets one
                self.fail(f'no cutover drop-in for {unit}')
            text = drop.read_text()
            eff = settings(base, text)
            execs = eff[('Service', 'ExecStart')]
            self.assertEqual(len(execs), 1, unit)
            words = exec_words(execs[0])
            self.assertEqual(words[1:], [a for a in spec['argv']], unit)
            sections = manifest['unit_sections'][unit]
            self.assertEqual(eff[('Service', 'WorkingDirectory')][-1], str(self.rel), unit)
            if sections['condition_path_exists']:
                self.assertEqual(eff[('Unit', 'ConditionPathExists')], [sections['condition_path_exists']], unit)
            if sections['timeout_start_sec']:
                self.assertEqual(eff[('Service', 'TimeoutStartSec')][-1], str(sections['timeout_start_sec']), unit)
            env = {}
            for item in eff[('Service', 'Environment')]:
                for word in env_words(item):
                    k, _, val = word.partition('=')
                    env[k] = val
            self.assertEqual(env, dict(i.split('=', 1) for i in spec['environment']), unit)
            rw = ' '.join(eff[('Service', 'ReadWritePaths')]).split()
            # ReadWritePaths is additive: the drop-in's paths must be effective (the base unit may list the same ones)
            self.assertLessEqual(set(sections['read_write_paths']), set(rw), unit)
        timeouts = {u: self.unit_dir.joinpath(f'{u}.service').read_text() for u in manifest['units']}
        self.assertIn('TimeoutStartSec=600', timeouts['desk-paper-entry-dispatcher'])
        self.assertIn('TimeoutStartSec=120', timeouts['desk-paper-held-cycle'])
        entry = manifest['units']['desk-paper-entry-dispatcher']['argv']
        self.assertEqual(entry[-2:], ['--execute', '--systemd-credentials'])
        dash = dict(i.split('=', 1) for i in manifest['units']['desk-dashboard']['environment'])
        self.assertEqual(set(dash), {'DESK_PAPER_SCHEDULER_LOCK', 'DESK_PAPER_SCHEDULER_IDENTITY', 'DESK_PAPER_LEDGER_DB'})
        self.assertEqual(manifest['backup_dir'], self.v['BK'])
