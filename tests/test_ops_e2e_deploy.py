"""SYNTHETIC_TEST_ONLY: the fresh-start deployment as ONE flow (T32).

The commands are parsed out of docs/ops/RUNBOOK.md and run in order against a temp filesystem with a fake
`systemctl` (archive backup -> bootstrap -> render -> cutover with entry OFF -> healthcheck -> latch -> entry enable ->
rollback). A flag or argument that drifts between the RUNBOOK and the tools therefore fails here. Only paths and the
service user are adapted for the sandbox; no network, no credentials, no real systemd.
"""
import contextlib
import fnmatch
import io
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import time
import types
from desk import kraken_pacing_migration as upgrade, provider_pacing as pace, runtime_compatibility as runtime
from desk.model import canonical
from discovery import continuous as discovery
from tests.test_ops_cutover import FakeSystemd, tree
from tests.test_ops_fresh_start import FreshStartBase
from tests.test_ops_fresh_start_wiring import exec_words, env_words, settings
from tools.ops import backup, cutover, entry_latch, fresh_start as fs, healthcheck, preflight_dryrun, status, verify_backup, verify_cycle

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
        # T32I item 4: the archived root also holds ~40 non-sqlite evidence files; they must be sealed too.
        self.evidence_files = [self.old / n for n in (
            *('acquisition-%02d.json' % i for i in range(5)), *('paper-target-SYNTH%02d.json' % i for i in range(5)),
            'migration-slot-intake.json', 'wallet-intake-2.json', 'events.jsonl', 'discovery/frames.jsonl')]
        for path in self.evidence_files:
            path.write_text('{"synthetic": true}\n')
            path.chmod(0o644)
        # Live lock files of the SHARED inputs and of the archived stores (T32G item 1): seal must never touch them.
        # discovery/continuous.py:61 opens `<db>.discovery.lock` read-write with O_CREAT; the pacing holders, paper-cycle,
        # invocation, scheduler and dispatcher locks are owned by the service user and flock'ed by running processes.
        self.locks = [self.old / n for n in (
            'discovery/continuous.sqlite.discovery.lock', 'provider-pacing.sqlite.holder-helius.lock',
            'provider-pacing.sqlite.holder-jupiter.lock', 'old-store-0.sqlite.paper-cycle.lock',
            'old-store-1.sqlite.ownership-invocation.lock', 'paper-scheduler.lock', '.daily.lock')]
        for lock in self.locks:
            lock.write_bytes(b'')
            lock.chmod(0o600)
        self.lock_stats = {l: (os.lstat(l).st_mode, os.lstat(l).st_uid, os.lstat(l).st_gid, os.lstat(l).st_mtime_ns) for l in self.locks}
        patcher = mock.patch.object(cutover, 'ROOT_UID', -1)       # the suite may run as uid 0; seal's "root-only" rule is tested elsewhere
        patcher.start(); self.addCleanup(patcher.stop)
        self.sha = 'abc1234'
        self.rel = self.rels / self.sha
        shutil.copytree(REPO / 'desk', self.rel / 'desk', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(REPO / 'deploy', self.rel / 'deploy')
        shutil.copytree(REPO / 'tools', self.rel / 'tools', ignore=shutil.ignore_patterns('__pycache__'))
        self.digest = runtime.implementation_hash()
        self.seed_production_units()
        self.prod_tree = tree(self.unit_dir)
        self.chowns = []
        patcher = mock.patch.object(cutover.Ops, 'chown', side_effect=lambda path, owner, group: self.chowns.append((Path(path), owner, group)))
        patcher.start(); self.addCleanup(patcher.stop)
        self.systemd = FakeSystemd(self.unit_dir)
        # Real VPS state (read-only inventory, 2026-10-11): two units are `failed`, most old units are `static`.
        self.systemd.state['desk-paper-entry-dispatcher.service'] = 'failed'
        self.systemd.state['desk-decisions.service'] = 'failed'
        for unit in ('desk-recorder.service', 'desk-discovery.service', 'desk-paper-monitor.service'):
            self.systemd.enabled[unit] = 'static'
        self.plan_hash = None
        self.last_cutover = None
        self.results = []
        self.research_runs = []
        self.runuser = []
        self.shell_log = []

    # -- the REAL production systemd state (read-only inventory by the coordinator, 2026-10-11) ---------------
    CODEX_STACK = ['60-reviewed-release', '90-reviewed-73edf43', '95-reviewed-e4badab', '96-reviewed-b414dbd',
                   '97-reviewed-continuation', '98-reviewed-extension', '99-profile2-reviewed']
    PRODUCTION_DROPINS = {
        'desk-backup.service': CODEX_STACK,
        'desk-dashboard.service': CODEX_STACK + ['zz-paper-scheduler-reviewed'],
        'desk-decisions.service': CODEX_STACK + ['zz-paper-scheduler-reviewed'],
        'desk-discovery.service': ['60-reviewed-release', '70-migration-sampling'],
        'desk-paper-entry-dispatcher.service': ['zz-paper-scheduler-reviewed', 'zzz-local-validation-deadline'],
        'desk-paper-entry-dispatcher.timer': ['90-reviewed-cadence'],
        'desk-paper-held-cycle.service': ['70-reviewed-runtime'] + CODEX_STACK[1:] + [
            'zz-paper-scheduler-reviewed', 'zzz-local-validation-deadline', 'zzzz-full-cycle-wall-deadline'],
        'desk-paper-held-cycle.timer': ['70-reviewed-enable', '90-reviewed-cadence'],
        'desk-paper-monitor.service': CODEX_STACK + ['zz-paper-scheduler-reviewed'],
    }
    CODEX_MARK = 'codex-era-override'

    def seed_production_units(self):
        """Stock deploy/ units plus the exact Codex-era *.d stacks of the production VPS, with realistic overrides."""
        for source in sorted((REPO / 'deploy').glob('desk-*.service')) + sorted((REPO / 'deploy').glob('desk-*.timer')):
            shutil.copy(source, self.unit_dir / source.name)
        for unit, names in self.PRODUCTION_DROPINS.items():
            directory = self.unit_dir / (unit + '.d')
            directory.mkdir(mode=0o755)
            for i, name in enumerate(names):
                if unit.endswith('.timer'):
                    body = ('[Install]\nWantedBy=timers.target\n' if 'enable' in name else
                            '[Timer]\nOnUnitInactiveSec=\nOnUnitInactiveSec=%ds\n' % (120 + i))
                else:
                    body = ('[Unit]\nConditionPathExists=\nConditionPathExists=/var/lib/solana-desk/active-paper.sqlite\n'
                            '[Service]\nWorkingDirectory=/opt/solana-desk-releases/%s\n'
                            'Environment=DESK_%s=%s\nExecStart=\nExecStart=/opt/solana-desk/.venv/bin/python -m desk.%s %s\n'
                            % (name.split('-')[-1], self.CODEX_MARK.upper().replace('-', '_'), name, unit[5:-8].replace('-', '_'), name))
                path = directory / (name + '.conf')
                path.write_text(body)
                path.chmod(0o644 if i % 3 else 0o640)
        (self.unit_dir / 'desk-paper-entry-dispatcher.service.d' / 'zzz-local-validation-deadline.conf.bak').write_text('[Service]\nTimeoutStartSec=17\n')

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
            elif module == 'preflight_dryrun':
                # The real tool needs unshare/mount and a service-user owner; here only the parsed arguments are judged
                # (T32H/T34: the shared pacing and discovery stores must be named, they are not under --data).
                self.preflight_args = []
                with mock.patch.object(preflight_dryrun, 'run', lambda args: self.preflight_args.append(args) or {'status': 'PASS'}):
                    code = preflight_dryrun.main(argv)
            else:
                code = {'verify_backup': verify_backup, 'fresh_start': fs, 'healthcheck': healthcheck,
                        'entry_latch': entry_latch, 'status': status, 'verify_cycle': verify_cycle}[module].main(argv)
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
        if module == 'status':                                  # the sandbox has no systemd and no git checkout to describe
            argv = [a for i, a in enumerate(argv) if a != '--systemd' and a != '--release-dir' and (i == 0 or argv[i - 1] != '--release-dir')]
        if module == 'cutover':
            head = ['--journal', str(self.rootdir / 'cutover-journal.jsonl'), '--python', sys.executable]
            sub = next(a for a in argv if a in ('stage', 'cutover', 'rollback', 'inventory', 'archive-dropins', 'seal-archive', 'unseal'))
            i = argv.index(sub)
            tail = ['--unit-dir', str(self.unit_dir)] if sub not in ('seal-archive', 'unseal') else []
            if sub == 'archive-dropins':
                tail += ['--archive-root', str(self.vb)]
            if sub == 'cutover':
                tail += ['--release-root', str(self.rels), '--settle-seconds', '0']
            argv = head + argv[:i + 1] + tail + argv[i + 1:]
        return argv

    def shell(self, words, v):
        """Minimal effects of the few non-python commands the RUNBOOK uses (what they do on a real host)."""
        cmd = words[0]
        self.shell_log.append(words)
        if cmd == 'systemctl' and words[1] == 'stop':
            for unit in words[2:]:
                if self.systemd.state.get(unit) != 'failed':             # stopping a failed unit leaves it `failed`
                    self.systemd.state[unit] = 'inactive'
        elif cmd == 'systemctl' and words[1] == 'reset-failed':
            patterns = words[2:] or ['*']
            for unit, state in list(self.systemd.state.items()):
                if state == 'failed' and any(fnmatch.fnmatch(unit, pattern) for pattern in patterns):
                    self.systemd.state[unit] = 'inactive'
        elif cmd == 'systemctl' and words[1] == 'enable':
            units = [w for w in words[2:] if not w.startswith('--')]
            for unit in units:
                self.systemd.enabled[unit] = 'enabled'
                if '--now' in words:
                    self.systemd.state[unit] = 'active'
        elif cmd == 'systemctl' and words[1] == 'daemon-reload':
            self.systemd(['systemctl', 'daemon-reload'])
        elif cmd == 'systemctl' and words[1] == 'disable':
            for unit in words[2:]:
                self.systemd(['systemctl', 'disable', unit])
        elif cmd == 'systemctl' and words[1] == 'is-enabled':   # the RUNBOOK says "expect disabled or static": make it so
            for unit in words[2:]:
                self.assertIn(self.systemd(['systemctl', 'is-enabled', unit]).stdout.strip(), ('disabled', 'static'), unit)
        elif cmd == 'systemctl' and words[1] == 'is-active':
            for unit in words[2:]:
                self.assertIn(self.systemd.state.get(unit, 'inactive'), ('inactive', 'failed'), unit)
        elif cmd == 'systemd-analyze':          # not available in the sandbox: assert the units it would verify exist
            self.assertEqual(words[1], 'verify')
            self.assertTrue(any(self.unit_dir.glob('desk-*.service')) and any(self.unit_dir.glob('desk-*.timer')))
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
        elif cmd in ('cd', 'export', 'sha256sum', 'ss'):    # no filesystem effect worth simulating (cwd, env, a missing tarball, a socket)
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
            if words[:4] == ['runuser', '-u', 'solana-desk', '--']:
                self.runuser.append(' '.join(words[4:7]))      # the sandbox user is not solana-desk: run it directly
                words = words[4:]
            if words[0] == v['PY'] or words[0] == sys.executable:
                if words[1] == '-m' and words[2].startswith('tools.research.'):
                    done = subprocess.run([sys.executable, *words[1:]], cwd=self.rel, capture_output=True, text=True)
                    self.assertEqual(done.returncode, 0, (words, done.stderr[-400:]))
                    self.research_runs.append(words[1:])
                    continue
                assert words[1] == '-m' and words[2].startswith('tools.ops.'), words
                module = words[2].split('.')[-1]
                argv = self.adapt(module, words[3:], v)
                if module == 'cutover' and 'stage' in words[3:]:
                    continue                                    # stage: own tests
                if module == 'pacing_policy':
                    continue                                    # T36's tool lives on cloud/T36 until the coordinator merges it
                if '<' in ' '.join(argv):
                    continue                                    # step needs a value only the operator has
                code, payload, raw = self.run_tool(module, argv)
                self.results.append((section, module, argv, code, payload, raw))
                if code != 0 and module not in ('healthcheck', 'status'):      # healthcheck/status exit 2 on any blocker; judged below
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
        self.presealed = {p: (os.lstat(p).st_mode & 0o7777, os.lstat(p).st_uid, os.lstat(p).st_gid)
                          for p in [self.old, self.old / 'discovery', *self.old.glob('old-store-*.sqlite')]}
        self.run_runbook(('2.', '3.', '4.', '5.', '6.', '7a.', '7b.', '8.', '9.'))
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
            'desk-decisions.timer', 'desk-backup.timer', 'desk-healthcheck.timer', 'desk-notify-daily.timer',
            'desk-notify-watchdog.timer']))
        self.assertNotIn('desk-paper-entry-dispatcher.timer', started)
        self.assertNotIn('desk-paper-monitor.timer', started)
        enabled = [c[2] for c in calls if c[1] == 'enable']
        # boot persistence: exactly the started units, in that order, and only AFTER they all started
        self.assertEqual(enabled[:-1] + enabled[-1:], [u for u in enabled])
        self.assertEqual(sorted(u for u in enabled if u != 'desk-paper-entry-dispatcher.timer'), sorted(started))
        self.assertLess(max(i for i, c in enumerate(calls) if c[1] == 'start'),
                        min(i for i, c in enumerate(calls) if c[1] == 'enable'))

        # --- T32F: the Codex-era stack was MOVED, never layered on
        archive = self.vb / f'systemd-archive-{self.sha}'
        amanifest = json.loads((archive / 'manifest.json').read_text())
        self.assertEqual({r['path'] for r in amanifest['records'] if r['type'] == 'file' and '.d/' in r['path']},
                         {f'{unit}.d/{name}.conf' for unit, names in self.PRODUCTION_DROPINS.items() for name in names}
                         | {'desk-paper-entry-dispatcher.service.d/zzz-local-validation-deadline.conf.bak'})
        for path in self.unit_dir.rglob('*'):
            if path.is_file() and path.parent.name.endswith('.d'):
                first = path.read_text().splitlines()[0]
                self.assertIn(first, (cutover.MARKER, cutover.FRESH_MARKER, cutover.LATCH_MARKER),
                              f'{path.relative_to(self.unit_dir)} is not one of ours')
        for unit in self.systemd_units():
            shown = self.systemd(['systemctl', 'cat', unit]).stdout
            self.assertNotIn(self.CODEX_MARK.upper().replace('-', '_'), shown, unit)       # no Codex-era Environment
            for name in self.CODEX_STACK[1:] + ['zz-paper-scheduler-reviewed', 'zzz-local-validation-deadline']:
                self.assertNotIn(name, shown, unit)
            headers = re.findall(r'(?m)^# (/\S+\.conf)$', shown)           # every drop-in `systemctl cat` lists is ours
            self.assertTrue(all(Path(h).read_text().splitlines()[0] in (cutover.MARKER, cutover.FRESH_MARKER, cutover.LATCH_MARKER)
                                for h in headers), (unit, headers))
        # old units not in the fresh set stay put but disabled (RUNBOOK step 2)
        for unit in ('desk-discovery.timer', 'desk-discovery.service', 'desk-paper-monitor.timer'):
            self.assertIn(self.systemd.enabled.get(unit, 'disabled'), ('disabled', 'static'), unit)    # static: no [Install], nothing to disable
        self.assertTrue((self.unit_dir / 'desk-discovery.service').is_file())            # not replaced: stays, disabled
        self.assertFalse((self.unit_dir / 'desk-discovery.service.d').exists())          # its stack was archived too
        self.assertTrue((archive / 'desk-discovery.service.d' / '70-migration-sampling.conf').is_file())
        # the inventory was saved BEFORE anything changed and includes the drop-in directories
        inventory = json.loads((self.vb / f'systemd-inventory-{self.sha}' / 'inventory.json').read_text())
        self.assertTrue(any(r['path'] == 'desk-paper-held-cycle.service.d/zzzz-full-cycle-wall-deadline.conf'
                            for r in inventory['records']))
        self.assertIn('zzzz-full-cycle-wall-deadline', (self.vb / f'systemd-inventory-{self.sha}' /
                      'systemctl-cat-desk-paper-held-cycle.service.txt').read_text())
        # archived stores: root-owned read-only; shared inputs untouched (fake chown layer records the intended owner)
        owners = {p: (o, g) for p, o, g in self.chowns}
        self.assertEqual(owners[self.old / 'old-store-3.sqlite'], ('root', 'solana-desk'))
        self.assertEqual(oct((self.old / 'old-store-3.sqlite').stat().st_mode & 0o777), '0o440')
        self.assertNotIn(self.pacer, owners)
        self.assertNotIn(self.discovery, owners)
        self.assertEqual(owners[self.old], ('root', 'solana-desk'))
        self.assertEqual(oct(self.old.stat().st_mode & 0o7777), '0o1770')            # group rwx + sticky, nothing for others
        self.assertEqual(oct((self.old / 'discovery').stat().st_mode & 0o7777), '0o1770')
        # T32G item 1: every live lock file is exactly as it was (owner, mode, mtime) and was never a chown target
        for lock in self.locks:
            now = os.lstat(lock)
            self.assertEqual((now.st_mode, now.st_uid, now.st_gid, now.st_mtime_ns), self.lock_stats[lock], lock.name)
            self.assertNotIn(lock, owners, lock.name)
        with discovery.worker(self.discovery):                                        # continuous discovery still gets its lock
            pass
        # T32I item 4: the JSON/JSONL evidence files are sealed like the stores (root:service-group 0440), locks still never
        for path in self.evidence_files:
            self.assertEqual(oct(path.stat().st_mode & 0o777), '0o440', path.name)
            self.assertEqual(owners[path], ('root', 'solana-desk'), path.name)
        # the seal manifest exists (root-only) and lists what was changed
        manifest_path = Path(self.v['SM'])
        self.assertEqual(oct(manifest_path.stat().st_mode & 0o777), '0o600')
        sealed = {e['path'] for e in json.loads(manifest_path.read_text())['sealed']}
        self.assertIn('old-store-3.sqlite', sealed)
        self.assertFalse(any(path.endswith('.lock') for path in sealed))
        self.assertTrue({f.relative_to(self.old).as_posix() for f in self.evidence_files} <= sealed)
        # T32G items 4/5: the backup parent was never chowned or re-moded; failed units were reset; static is accepted
        self.assertNotIn(self.vb, owners)
        self.assertEqual(self.systemd.state['desk-paper-entry-dispatcher.service'], 'inactive')
        self.assertEqual(self.systemd.state['desk-decisions.service'], 'inactive')
        self.assertIn(['systemctl', 'reset-failed', 'desk-*'], self.shell_log)
        # read-only tools run as the service user
        self.assertEqual([r.split()[-1] for r in self.runuser], ['tools.ops.healthcheck', 'tools.ops.status', 'tools.ops.verify_cycle', 'tools.ops.entry_latch'])
        # T32H/T34: the read-only tools were really run against the fresh stores and named the SHARED pacing/discovery stores
        (status_run,) = [r for r in self.results if r[1] == 'status']
        self.assertEqual((status_run[3], status_run[4]['status'], status_run[4]['blockers']), (0, 'OK', []))
        self.assertEqual(status_run[4]['discovery']['path'], str(self.discovery))
        external = {store['name']: store['role'] for store in status_run[4]['store_inventory']['stores'] if store.get('external')}
        self.assertEqual(external, {str(self.discovery): 'discovery', str(self.pacer): 'provider_pacing'})
        self.assertTrue(status_run[4]['provider_pacing']['providers'])               # the shared pacing database was really read
        (snapshot_run,) = [r for r in self.results if r[1] == 'verify_cycle']
        self.assertEqual((snapshot_run[3], snapshot_run[4]['status']), (0, 'NO_FILLS'))
        snapshot_doc = json.loads(Path(snapshot_run[4]['out']).read_text())
        self.assertEqual((snapshot_doc['pacing']['present'], snapshot_doc['discovery']['present']), (True, True))   # the SHARED stores were found
        self.assertEqual([name for name, _ in snapshot_doc['pacing']['providers']], ['helius', 'jupiter', 'kraken'])
        (preflight,) = self.preflight_args
        self.assertEqual((preflight.pacing_db, preflight.discovery_db, preflight.data, preflight.ledger),
                         (str(self.pacer), str(self.discovery), self.v['NEW'], self.v['LEDGER']))
        # ...and so does every other read-only tool the RUNBOOK mentions (inline code spans are not executed here)
        for line in RUNBOOK.read_text().splitlines():
            for tool_name in ('tools.ops.status', 'tools.ops.verify_cycle', 'tools.ops.healthcheck', 'tools.ops.entry_latch'):
                for hit in re.finditer(r'\$PY -m ' + re.escape(tool_name), line):
                    if line.startswith('Read-only tools'):
                        continue
                    self.assertTrue(line[:hit.start()].rstrip().endswith('runuser -u solana-desk --'), line)

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

        # --- rollback restores everything this flow wrote AND puts the production tree back byte-for-byte
        units = ['desk-continuous-discovery.service', 'desk-dashboard.service', 'desk-paper-held-cycle.service',
                 'desk-decisions.service', 'desk-backup.service', 'desk-healthcheck.service',
                 'desk-notify-daily.service', 'desk-notify-watchdog.service', 'desk-paper-monitor.service',
                 'desk-paper-entry-dispatcher.service']
        self.chowns.clear()
        self.run_runbook(('Rollback',))                                                 # the RUNBOOK's own commands, unseal included
        self.assertEqual(tree(self.unit_dir), self.prod_tree)
        modes = {p: (os.lstat(p).st_mode & 0o7777) for p in self.presealed}
        self.assertEqual(modes, {p: v[0] for p, v in self.presealed.items()})            # unseal restored every mode...
        restored = {p: (u, g) for p, u, g in self.chowns}
        for path, (mode, uid, gid) in self.presealed.items():
            self.assertEqual(restored[path], (uid, gid), path)                          # ...and the exact numeric owner
        for lock in self.locks:
            self.assertNotIn(lock, restored)
        for unit in started + ['desk-paper-entry-dispatcher.timer']:
            self.assertNotEqual(self.systemd.enabled.get(unit, 'disabled'), 'enabled', unit)
            self.assertIn(self.systemd.state.get(unit, 'inactive'), ('inactive', 'failed'), unit)
        # a second rollback is a harmless no-op and leaves the restored tree alone
        code, payload, raw = self.run_tool('cutover', self.adapt('cutover', ['--apply', 'rollback', '--units', *units], self.v))
        self.assertEqual((code, payload['removed']), (0, []))
        self.assertEqual(tree(self.unit_dir), self.prod_tree)

    def runbook_blocks(self):
        return logical_commands(RUNBOOK.read_text().split('## Rotate when flat')[0])

    def sealed_stat_overlay(self):
        """The sandbox cannot chown: show the owner/group the (mocked) seal recorded, so the REAL permission rules see a sealed root."""
        real = os.lstat
        sealed = {Path(p) for p, owner, group in self.chowns if owner == 'root'}
        service_gid = real(self.v['NEW']).st_gid

        def stat(path, *a, **k):
            info = real(path, *a, **k)
            if Path(path) in sealed:
                return types.SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink, st_uid=0, st_gid=service_gid,
                                             st_size=info.st_size, st_mtime_ns=info.st_mtime_ns)
            return info
        return mock.patch.object(preflight_dryrun, '_stat', side_effect=stat)

    def test_the_real_preflight_accepts_the_sealed_archive_it_is_run_against_after_a_real_seal(self):
        """T32I item 1 (BLOCKER): step 6 failed with LIVE_external_*_dir:MODE_1770 right after the step-4 seal. Nothing is stubbed here:
        the RUNBOOK's seal ran for real, then the REAL preflight tool (copies, child, `_live_findings`) is run with the RUNBOOK's arguments."""
        if preflight_dryrun.namespace_prefix() is None:
            self.skipTest('REQUIRES a private mount namespace (euid 0 or unprivileged user namespaces) - unavailable on this host')
        self.run_runbook(('2.', '3.', '4.', '5.'))
        self.assertEqual(oct(self.old.stat().st_mode & 0o7777), '0o1770')
        shutil.copyfile(self.base / 'migration-e2e.json', self.rel / 'config' / 'kraken-pacing-migration.json')   # the child reads the RELEASE's pin
        (step6,) = [c for sec, c in self.runbook_blocks() if sec.startswith('6.') and 'preflight_dryrun' in c]
        prod = self.variables(RUNBOOK.read_text())
        words = shlex.split(self.sandbox(step6, prod))
        argv = words[3:]
        workdir = argv.index('--workdir')
        argv[workdir + 1] = str(self.base / 'preflight-work')
        out = io.StringIO()
        with self.sealed_stat_overlay(), contextlib.redirect_stdout(out):
            code = preflight_dryrun.main(argv)
        report = json.loads(out.getvalue())
        self.assertEqual((code, report.get('status'), report.get('blockers'), report.get('live_permission_findings')), (0, 'PASS', [], []), report)
        self.assertEqual(report['isolation'], 'MOUNT_NAMESPACE')

    def test_there_is_exactly_one_pacing_policy_step_and_no_6b(self):
        """T32I item 3: T36's `## 6b` hunk is dropped; T32H's 7a2 is the one place the policy is applied."""
        text = RUNBOOK.read_text()
        self.assertNotIn('## 6b', text)
        self.assertEqual(len(re.findall(r'^#+ .*[Pp]acing policy', text, re.M)), 1)
        steps = [sec for sec, c in self.runbook_blocks() if 'tools.ops.pacing_policy' in c]
        self.assertEqual(steps, ['7a2. Pacing policy for the paid Helius / Jupiter plans (T36; optional, reviewed, append-only)'] * 3)

    def test_the_pacing_schema_upgrade_step_sits_next_to_7a2_and_says_what_it_is(self):
        text = RUNBOOK.read_text()
        a, p2, p3, b = (text.index(h) for h in ('### 7a. Render', '### 7a2.', '### 7a3.', '### 7b. Dry run'))
        self.assertTrue(a < p2 < p3 < b)
        commands = [c for sec, c in self.runbook_blocks() if sec.startswith('7a3.')]
        self.assertEqual(len(commands), 3, commands)
        cd, quiet, upgrade = commands
        self.assertEqual(cd, 'cd $REL')
        self.assertTrue(quiet.startswith('systemctl is-active '))
        for unit in ('desk-paper-entry-dispatcher.service', 'desk-paper-held-cycle.path', 'desk-paper-monitor.timer', 'desk-decisions.service',
                     'desk-backup.service', 'desk-healthcheck.service', 'desk-dashboard.service', 'desk-continuous-discovery.service'):
            self.assertIn(unit, quiet)
        self.assertEqual(upgrade, 'runuser -u solana-desk -- $PY -m desk.provider_pacing --upgrade $PACING')
        section = ' '.join(text[p3:b].split())
        for phrase in ('One-way door', 'PACING_DATABASE_INVALID', 'INERT', 'every writer stopped', 'service user', 'never reset'):
            self.assertIn(phrase, section, phrase)
        self.assertIn('If step 7a2 or 7a3 was applied, the OLD stack cannot be started again', text)

    def test_the_pacing_schema_upgrade_command_runs_as_written_against_a_production_shaped_pacing_database(self):
        """T32I item 2: the production database has the Kraken receipt and NO pacing_reclaims table."""
        import sqlite3
        prod = self.variables(RUNBOOK.read_text())
        commands = [c for sec, c in self.runbook_blocks() if sec.startswith('7a3.')]

        def tables():
            with sqlite3.connect(self.pacer) as c:
                return sorted(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'"))

        def rows():
            with sqlite3.connect(self.pacer) as c:
                return {t: c.execute('SELECT * FROM "%s"' % t).fetchall() for t in tables() if t not in ('pacing_reclaims', 'sqlite_sequence')}
        self.assertNotIn('pacing_reclaims', tables())
        self.assertEqual(len(rows()['kraken_pacing_migration']), 1, 'production-shaped: the Kraken migration receipt is there')
        before = rows()
        outputs = []
        for command in commands:
            words = shlex.split(self.sandbox(command, prod))
            if words[:4] == ['runuser', '-u', 'solana-desk', '--']:
                words = words[4:]
            if words[0] == sys.executable:
                self.assertEqual(words[1:3], ['-m', 'desk.provider_pacing'])
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = pace.main(words[3:])
                outputs.append((code, out.getvalue().strip()))
            else:
                self.shell(words, prod)
        self.assertEqual(outputs, [(0, 'PACING_UPGRADED')])
        self.assertIn('pacing_reclaims', tables())
        self.assertEqual(rows(), before, 'no existing row, counter, cadence or the Kraken receipt changed')
        with sqlite3.connect(self.pacer) as c:                      # append-only guards came with it
            c.execute("INSERT INTO pacing_reclaims(provider,ticket,granted_at,reclaimed_at,backoff_until,reason) VALUES('helius','t',1,2,3,'OWNER_GONE')")
            with self.assertRaises(sqlite3.DatabaseError):
                c.execute('UPDATE pacing_reclaims SET reason=\'x\'')
            c.rollback()
        with contextlib.redirect_stdout(io.StringIO()) as again:
            self.assertEqual(pace.main(['--upgrade', str(self.pacer)]), 0)
        self.assertEqual(again.getvalue().strip(), 'PACING_ALREADY_UPGRADED')
        pace.Pacer(self.pacer)                                      # the strict schema check still accepts the database
        from tools.ops import pacing_policy
        with mock.patch.object(pace, 'POLICY_FILE', self.rel / 'config' / 'provider-pacing-policy.json'):
            shutil.copytree(REPO / 'config', self.rel / 'config', dirs_exist_ok=True)
            plan = pacing_policy.run(types.SimpleNamespace(command='plan', db=str(self.pacer), policy=None, policy_id=None, execute=False,
                                                          require_quiesced=[]), runner=lambda unit: 'inactive')
        self.assertTrue(plan['ready_to_apply'], plan)

    def test_the_rotate_seal_uses_a_new_manifest_and_no_shared_path_so_the_new_root_is_0550(self):
        """T32I items 5/6: run the rotate section's two seal commands as written."""
        text = RUNBOOK.read_text()
        rotate = text[text.index('## Rotate when flat'):text.index('## If a position is open')]
        seals = [c for c in (l.strip() for l in rotate.splitlines()) if 'cutover' in c and 'seal-archive' in c]
        self.assertEqual(len(seals), 2, seals)
        for command in seals:
            self.assertNotIn('--shared-path', command)
            self.assertIn('--manifest /var/backups/solana-desk/seal-manifest-rotate-<next version>.json', command)
            self.assertNotIn('$SM', command)
        self.assertNotIn('--apply', seals[0])
        self.assertIn('--apply', seals[1])
        prod = self.variables(text)
        root = Path(self.v['NEW'] if hasattr(self, 'v') else self.base / 'var-lib' / 'fresh' / 'v1')
        root = self.base / 'var-lib' / 'fresh' / 'rotated-from'
        (root / 'sub').mkdir(parents=True)
        for rel in ('a.sqlite', 'sub/b.sqlite', 'run.json'):
            (root / rel).write_text('x')
            (root / rel).chmod(0o644)
        for command in seals:
            command = command.replace('$NEW', str(root)).replace('<next version>', 'v2')
            words = shlex.split(self.sandbox(command, prod))
            argv = self.adapt('cutover', words[3:], {})
            code, payload, raw = self.run_tool('cutover', argv)
            self.assertEqual(code, 0, (command, payload or raw[-300:]))
        self.assertEqual(oct(root.stat().st_mode & 0o7777), '0o550')
        self.assertEqual(oct((root / 'sub').stat().st_mode & 0o7777), '0o550')
        self.assertEqual(payload['shared_dirs'], [])
        self.assertTrue((self.vb / 'seal-manifest-rotate-v2.json').is_file())
        code, payload, raw = self.run_tool('cutover', self.adapt('cutover', words[3:], {}))      # the same manifest path again: refused
        self.assertNotEqual(code, 0)
        self.assertIn('already exists', payload.get('error', raw))

    def test_every_status_preflight_and_snapshot_command_names_the_shared_pacing_and_discovery_stores(self):
        """T32H/T34: the shared stores live outside $NEW; the tools' defaults would look for them under $NEW."""
        hits = [(section, c) for section, c in self.runbook_blocks()
                if re.search(r'tools\.ops\.(status|preflight_dryrun|verify_cycle snapshot)\b', c)]
        self.assertEqual(len(hits), 4, hits)             # preflight (6), status (8), snapshot before (8), snapshot after (10)
        for section, command in hits:
            self.assertIn('--pacing-db $PACING', command, (section, command))
            self.assertIn('--discovery-db $DISCOVERY', command, (section, command))

    def test_pacing_policy_step_sits_between_unit_installation_and_the_first_start(self):
        """T32H: apply ONLY after the new release is on every unit, as the service user, from $REL, with an append-only file."""
        text = RUNBOOK.read_text()
        seven_a, policy, seven_b = (text.index(h) for h in ('### 7a. Render and install the units', '### 7a2. Pacing policy', '### 7b. Dry run, then apply'))
        self.assertLess(seven_a, policy)
        self.assertLess(policy, seven_b)
        self.assertLess(text.index('install -m 0644 $UNITS/*.service $UNITS/*.timer /etc/systemd/system/'), policy)   # units installed first
        self.assertLess(text.index('## 2. Inventory, then stop writers'), policy)                                     # writers stopped first
        section = text[policy:seven_b]
        commands = [c for sec, c in self.runbook_blocks() if sec.startswith('7a2.')]
        self.assertEqual(len(commands), 4, commands)                        # cd + plan + dry-run apply + apply --execute
        self.assertEqual(commands[0], 'cd $REL')
        plan, dry, real = commands[1:]
        for command in (plan, dry, real):
            self.assertTrue(command.startswith('runuser -u solana-desk -- $PY -m tools.ops.pacing_policy '), command)
            self.assertIn('--db $PACING', command)
            self.assertIn('--require-quiesced', command)
        self.assertNotIn('--execute', plan + dry)
        self.assertIn('--execute', real)
        for command in (dry, real):
            self.assertIn('--policy $REL/config/provider-pacing-policy.json', command)
        for phrase in ('ONLY after the new release is on every unit', 'BEFORE the first unit starts', '**The policy file is append-only.**', 'Kraken stays at exactly 2.0 s',
                       'Never edit, reorder or delete an entry that was applied', 'ALREADY_APPLIED', 'One-way door', 'PACING_DATABASE_INVALID', 'shared database must not be reset'):
            self.assertIn(phrase, ' '.join(section.split()), phrase)
        # T32I: the rollback sentence now names both one-way doors (7a2 policy table, 7a3 reclaim table); the 7a2 half is unchanged
        self.assertIn('If step 7a2 or 7a3 was applied, the OLD stack cannot be started again', text)         # the rollback section says so

    @unittest.skipUnless((REPO / 'tools' / 'ops' / 'pacing_policy.py').is_file(), 'T36 (tools.ops.pacing_policy) is not merged into this tree yet')
    def test_the_pacing_policy_commands_run_as_written_against_the_sandbox_pacing_database(self):
        """T32H: once T36 is merged, the RUNBOOK's 7a2 commands are executed, not only read."""
        import sqlite3
        from tools.ops import pacing_policy
        shutil.copytree(REPO / 'config', self.rel / 'config', dirs_exist_ok=True)
        prod = self.variables(RUNBOOK.read_text())
        commands = [c for sec, c in self.runbook_blocks() if sec.startswith('7a2.')][1:]
        self.assertEqual(len(commands), 3)

        def kraken_lane():
            with sqlite3.connect(self.pacer) as c:
                return (c.execute("SELECT * FROM policy WHERE provider='kraken'").fetchall(),
                        c.execute("SELECT * FROM state WHERE provider='kraken'").fetchall(),
                        c.execute('SELECT * FROM kraken_pacing_migration').fetchall())
        before_kraken = kraken_lane()
        results = []
        with mock.patch.object(pace, 'POLICY_FILE', self.rel / 'config' / 'provider-pacing-policy.json'):   # the tool accepts only THIS release's file
            for command in commands + commands[2:]:                        # ...and the real apply a second time: a no-op
                words = shlex.split(self.sandbox(command, prod))
                self.assertEqual(words[:4], ['runuser', '-u', 'solana-desk', '--'])
                self.assertEqual(words[4:7], [sys.executable, '-m', 'tools.ops.pacing_policy'])
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = pacing_policy.main(words[7:], runner=lambda unit: 'inactive')
                results.append((code, json.loads(out.getvalue())))
        self.assertEqual([r[0] for r in results], [0, 0, 0, 0], results)
        plan, dry, real, again = (r[1] for r in results)
        self.assertEqual((plan['status'], dry['status']), ('PLAN', 'DRY_RUN'))
        self.assertTrue(plan['ready_to_apply'] and dry['ready_to_apply'])
        self.assertEqual({c['provider'] for c in plan['changes']}, {'helius', 'jupiter'})   # never kraken
        with sqlite3.connect(self.pacer) as c:
            rows = c.execute('SELECT provider FROM pacing_policy_changes ORDER BY id').fetchall()
        self.assertEqual(sorted(r[0] for r in rows), ['helius', 'jupiter'])               # appended exactly once
        self.assertIn('ALREADY_APPLIED', json.dumps(again))
        self.assertEqual(kraken_lane(), before_kraken)                                      # the Kraken lane is byte-identical

    def test_runbook_never_chowns_or_installs_into_the_backup_parent(self):
        """T32G item 4: /var/backups/solana-desk is root:root 0755 on the VPS and stays so; fresh_start creates $BK in it."""
        text = RUNBOOK.read_text()
        for section, command in logical_commands(text):
            if re.search(r'\b(chown|install|chmod)\b', command):
                self.assertNotRegex(command, r'(?<![\w/-])/var/backups/solana-desk(?![\w/-])', command)
        step0 = text.split('## 0. Preconditions')[1].split('## 1.')[0]
        self.assertRegex(step0, r'(?is)/var/backups/solana-desk[^.]*root')
        self.assertRegex(step0, r'(?i)never chown')

    def test_the_flow_leaves_the_backup_parent_owner_and_mode_alone(self):
        """T32G item 4: apply creates $BK inside the root-owned parent; the parent itself is never chowned or re-moded."""
        self.vb.chmod(0o755)
        before = (self.vb.stat().st_uid, self.vb.stat().st_gid, self.vb.stat().st_mode & 0o7777)
        self.run_runbook(('2.', '3.', '4.', '5.', '6.', '7a.', '7b.', '8.', '9.'))
        after = (self.vb.stat().st_uid, self.vb.stat().st_gid, self.vb.stat().st_mode & 0o7777)
        self.assertEqual(after, before)
        self.assertEqual(oct(Path(self.v['BK']).stat().st_mode & 0o777), '0o700')
        self.assertEqual(Path(self.v['BK']).parent, self.vb)

    def test_failed_units_are_reset_after_the_stops_and_static_old_units_pass_the_disabled_check(self):
        self.run_runbook(('2.',))
        self.assertEqual({u: s for u, s in self.systemd.state.items() if s == 'failed'}, {})
        commands = [w for w in self.shell_log if w[:2] == ['systemctl', 'reset-failed']]
        self.assertEqual(commands, [['systemctl', 'reset-failed', 'desk-*']])
        index = {tuple(w): i for i, w in enumerate(self.shell_log)}
        stops = [i for i, w in enumerate(self.shell_log) if w[:2] == ['systemctl', 'stop']]
        reset = next(i for i, w in enumerate(self.shell_log) if w[:2] == ['systemctl', 'reset-failed'])
        self.assertGreater(reset, max(stops))                                       # only after every stop
        for unit in ('desk-recorder.service', 'desk-discovery.service', 'desk-paper-monitor.service'):
            self.assertEqual(self.systemd(['systemctl', 'is-enabled', unit]).stdout.strip(), 'static')

    def test_runbook_expects_disabled_or_static_not_only_disabled(self):
        step2 = RUNBOOK.read_text().split('## 2.')[1].split('## 3.')[0]
        self.assertRegex(step2, r'expect disabled(?: or |\|)static')

    def test_runbook_pins_the_working_directory_and_documents_the_stale_package_fix(self):
        text = RUNBOOK.read_text()
        self.assertRegex(text, r'cwd = \$REL')
        self.assertRegex(text, r'(?i)site-packages')
        self.assertIn('pip uninstall -y solana-desk', text)

    def test_seal_step_uses_a_manifest_and_the_rollback_unseals(self):
        text = RUNBOOK.read_text()
        self.assertRegex(text, r'(?m)^SM=\S*seal-manifest\S*')
        seal = [c for s, c in logical_commands(text) if 'seal-archive' in c and 'apply' in c]
        self.assertTrue(seal and all('--manifest $SM' in c for c in seal), seal)
        rollback = [c for s, c in logical_commands(text) if s == 'Rollback']
        self.assertTrue(any(' unseal ' in c and '--manifest $SM' in c for c in rollback), rollback)

    def test_research_units_are_never_installed_or_enabled_by_the_default_flow(self):
        self.run_runbook(('2.', '3.', '4.', '5.', '6.', '7a.', '7b.', '8.', '9.'))
        research = {'desk-counterfactual.service', 'desk-counterfactual.timer', 'desk-held-watcher.service',
                    'desk-paper-held-cycle.path'}
        self.assertFalse(research & {p.name for p in self.unit_dir.iterdir()})
        touched = {c[-1] for c in self.systemd.calls if c[1] in ('start', 'enable')}
        self.assertFalse(research & touched)
        self.assertEqual({p.name for p in (Path(self.v['UNITS']) / 'research').iterdir()}, research)

    def test_the_explicit_research_step_renders_installs_initialises_and_enables_them(self):
        self.run_runbook(('2.', '3.', '4.', '5.', '7a.', '7b.', '8.', '9.', '11.'))
        for name in ('desk-counterfactual.service', 'desk-counterfactual.timer', 'desk-held-watcher.service',
                     'desk-paper-held-cycle.path'):
            self.assertTrue((self.unit_dir / name).is_file(), name)
        new = Path(self.v['NEW'])
        self.assertTrue((new / 'counterfactual' / 'counterfactual.sqlite').is_file())     # init ran for real
        self.assertTrue((Path(self.v['STATE']) / 'held-watcher').is_dir())
        self.assertEqual(self.systemd.enabled.get('desk-counterfactual.timer'), 'enabled')
        installed = (self.unit_dir / 'desk-counterfactual.service').read_text()
        self.assertIn(f'{new}/counterfactual/counterfactual.sqlite', installed)
        self.assertNotIn('exp-FRESH', installed)
        self.assertTrue(any(r[:3] == ['-m', 'tools.research.counterfactual', 'init'] for r in self.research_runs))

    def systemd_units(self):
        return sorted(p.name for p in self.unit_dir.glob('desk-*') if p.is_file() and p.suffix in ('.service', '.timer'))

    def test_backup_unit_execstart_is_systemd_quoted_and_runs(self):
        """T32F item 11: the backup unit's lines survive systemd's own quoting rules (base unit and python -c override)."""
        self.run_runbook(('2.', '3.', '4.', '5.', '7a.', '7b.'))
        base = (self.unit_dir / 'desk-backup.service').read_text()
        (base_line,) = [l for l in base.splitlines() if l.startswith('ExecStart=')]
        self.assertNotIn('%', base_line.replace('%%', ''))                    # every `%` (a systemd specifier) is doubled
        self.assertNotRegex(re.sub(r'\$\$', '', base_line), r'\$')           # `$` only as the intended `$$` for the shell
        self.assertIn('%%Y%%m%%dT%%H%%M%%SZ', base_line)
        drop = (self.unit_dir / 'desk-backup.service.d' / cutover.DROPIN).read_text()
        override = [l for l in drop.splitlines() if l.startswith('ExecStart=') and l != 'ExecStart=']
        self.assertEqual(len(override), 1)                                    # the reset line plus exactly one command
        line = override[0]
        self.assertIn('%%Y%%m%%dT%%H%%M%%SZ', line)
        self.assertNotIn('%', line.replace('%%', ''))
        self.assertNotRegex(line, r'\$[^$]')
        words = exec_words(line.partition('=')[2])
        self.assertEqual(words[1], '-c')
        code = words[2]                                                       # what python receives after systemd unquoting
        self.assertIn('%Y%m%dT%H%M%SZ', code)
        compile(code, 'desk-backup-execstart', 'exec')                        # still valid Python after unquoting
        manifest = json.loads((Path(self.v['NEW']) / fs.MANIFEST).read_text())
        self.assertEqual(words[1:], manifest['units']['desk-backup']['argv'])

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
        self.assertEqual(set(dash), {'DESK_PAPER_SCHEDULER_LOCK', 'DESK_PAPER_SCHEDULER_IDENTITY', 'DESK_PAPER_LEDGER_DB',
                                     'DESK_PROVIDER_PACING_DB'})          # T32F item 6: dashboard scans use the shared 2 s pacing
        self.assertEqual(dash['DESK_PROVIDER_PACING_DB'], str(self.pacer))
        self.assertEqual(manifest['backup_dir'], self.v['BK'])
