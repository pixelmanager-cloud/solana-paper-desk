"""SYNTHETIC_TEST_ONLY: T32G item 3 - the optional research units render with the fresh-root layout.

`desk-counterfactual.*`, `desk-held-watcher.service` and `desk-paper-held-cycle.path` are research/fast-exit helpers:
they are rendered by `fresh_start render-units` into a separate `research/` directory, so the RUNBOOK's default install
(`install $UNITS/*.service $UNITS/*.timer ...`) can never pick them up, and every path in them is the applied fresh root,
the staged release and the shared pacing/discovery inputs. Temp directories only; nothing is installed or started.
"""
import re
import shutil
import unittest
from pathlib import Path

from tests.test_ops_fresh_start_wiring import REPO, WiringBase
from tools.ops import fresh_start as fs

RESEARCH = {'desk-counterfactual.service', 'desk-counterfactual.timer', 'desk-held-watcher.service',
            'desk-paper-held-cycle.path'}
RELEASE = '/opt/solana-desk-releases/abc1234'
STATE = '/var/lib/solana-desk-health'


def active(text):
    return re.sub(r'(?m)^\s*#.*$', '', text)


class ResearchUnitTests(WiringBase):
    def rendered(self, name='exp-1'):
        manifest, root = self.applied(name, enable_held=True)
        out = self.base / f'units-{name}'
        result = fs.render_units(root, out, release_dir=RELEASE, state_dir=STATE)
        return manifest, root, out, result

    def test_research_units_render_into_their_own_directory_not_the_default_install_set(self):
        manifest, root, out, result = self.rendered()
        self.assertEqual(fs.RESEARCH_UNITS, RESEARCH)
        self.assertEqual({p.name for p in (out / 'research').iterdir()}, RESEARCH)
        top = {p.name for p in out.iterdir() if p.is_file()}
        self.assertFalse(top & RESEARCH)                                     # nothing optional at the top level
        self.assertFalse(any(p.suffix == '.path' for p in out.iterdir()))
        # exactly what the RUNBOOK installs by default (`$UNITS/*.service $UNITS/*.timer`)
        default = {p.name for p in [*out.glob('*.service'), *out.glob('*.timer')]}
        self.assertFalse(default & RESEARCH)
        self.assertEqual(sorted(result['research']), sorted(f'research/{n}' for n in RESEARCH))
        self.assertTrue(set(result['research']) <= set(result['files']))

    def test_every_research_placeholder_is_filled_and_comments_are_not_scanned(self):
        manifest, root, out, result = self.rendered()
        for path in (out / 'research').iterdir():
            self.assertNotRegex(active(path.read_text()), r'<[A-Z_]+>', path.name)
            self.assertNotRegex(path.read_text(), r'<[A-Z_]+>', path.name)

    def test_counterfactual_service_uses_the_fresh_root_release_and_shared_inputs(self):
        manifest, root, out, _ = self.rendered()
        text = (out / 'research' / 'desk-counterfactual.service').read_text()
        body = active(text)
        for stale in ('exp-FRESH', '/FRESH', 'LEDGER', '/opt/solana-desk\n', '/var/lib/solana-desk/exp-'):
            self.assertNotIn(stale, text)
        self.assertIn(f'WorkingDirectory={RELEASE}', body)
        store = f'{root}/counterfactual/counterfactual.sqlite'
        self.assertIn(f'ingest --store {store} --discovery-db {self.discovery}', body)
        self.assertIn(f'--journal {root}/entry-dispatch/dispatch.sqlite', body)
        self.assertIn(f'--ledger {root}/paper-ledger.sqlite', body)
        self.assertIn(f'--decisions-db {root}/paper-decisions.sqlite', body)
        self.assertIn(f'sample --store {store} --systemd-credentials', body)
        self.assertIn(f'Environment=DESK_PROVIDER_PACING_DB={self.pacer}', body)
        # write scope: its own store directory and the pacing DIRECTORY (a rollback journal is created beside the db)
        self.assertIn(f'ReadWritePaths={self.pacer.parent} {root}/counterfactual', body)
        self.assertIn(f'ReadOnlyPaths={root} {self.discovery.parent}', body)
        (line,) = [l for l in body.splitlines() if l.startswith('ExecStart=')]
        self.assertTrue(line.startswith('ExecStart=/opt/solana-desk/.venv/bin/python -m tools.research.counterfactual sample'))

    def test_counterfactual_timer_is_inert_until_the_operator_enables_it(self):
        manifest, root, out, _ = self.rendered()
        body = active((out / 'research' / 'desk-counterfactual.timer').read_text())
        self.assertIn('Unit=desk-counterfactual.service', body)
        self.assertIn('OnUnitInactiveSec=60s', body)
        self.assertNotIn('OnBootSec', body)                                 # nothing starts it on its own at install

    def test_held_watcher_writes_only_its_own_state_directory_and_the_path_unit_watches_its_trigger(self):
        manifest, root, out, _ = self.rendered()
        service = active((out / 'research' / 'desk-held-watcher.service').read_text())
        self.assertIn(f'WorkingDirectory={RELEASE}', service)
        self.assertIn(f'--ledger {root}/paper-ledger.sqlite --config {manifest["config"]} --state-dir {STATE}/held-watcher', service)
        self.assertIn(f'ReadWritePaths={STATE}/held-watcher', service)
        self.assertNotIn('DESK_PROVIDER_PACING_DB', service)               # its own allowance, never the shared pacing store
        path = active((out / 'research' / 'desk-paper-held-cycle.path').read_text())
        self.assertIn(f'PathChanged={STATE}/held-watcher/trigger/request', path)
        self.assertIn('Unit=desk-paper-held-cycle.service', path)

    def test_no_research_unit_references_the_archived_root_except_the_shared_inputs(self):
        manifest, root, out, _ = self.rendered()
        old = str(self.pacer.parent)
        allowed = {str(self.pacer), old, str(self.discovery), str(self.discovery.parent)}
        for path in (out / 'research').iterdir():
            for ref in re.findall(re.escape(old) + r'[^\s"\';,]*', active(path.read_text())):
                if ref.startswith(str(root)) or ref == manifest['config']:   # sandbox only: the fresh root and the config sit under the same parent
                    continue
                self.assertIn(ref, allowed, f'{path.name}: {ref}')

    def test_an_unfilled_placeholder_in_a_research_template_is_refused_and_nothing_is_written(self):
        manifest, root = self.applied('exp-2', enable_held=True)
        for name in sorted(RESEARCH):
            with self.subTest(name):
                templates = self.base / f'tpl-{name}'
                shutil.copytree(REPO / 'deploy' / 'fresh', templates)
                path = templates / name
                path.write_text(path.read_text() + '\n# ' + 'x' + '\nDescription=<NOT_A_KNOWN_PLACEHOLDER>\n')
                out = self.base / f'out-{name}'
                with self.assertRaisesRegex(fs.FreshStartError, 'unfilled'):
                    fs.render_units(root, out, release_dir=RELEASE, state_dir=STATE, templates=templates)
                self.assertFalse(out.exists())

    def test_a_template_directory_without_a_research_unit_is_refused(self):
        manifest, root = self.applied('exp-3', enable_held=True)
        templates = self.base / 'tpl-missing'
        shutil.copytree(REPO / 'deploy' / 'fresh', templates)
        (templates / 'desk-counterfactual.timer').unlink()
        with self.assertRaisesRegex(fs.FreshStartError, 'desk-counterfactual.timer'):
            fs.render_units(root, self.base / 'out-missing', release_dir=RELEASE, state_dir=STATE, templates=templates)

    def test_cli_reports_the_research_files(self):
        manifest, root = self.applied('exp-4', enable_held=True)
        code, out = self.cli('render-units', '--root', str(root), '--out', str(self.base / 'cli-units'),
                             '--release-dir', RELEASE)
        self.assertEqual((code, out['status']), (0, 'RENDERED'))
        self.assertEqual(sorted(out['research']), sorted(f'research/{n}' for n in RESEARCH))


if __name__ == '__main__':
    unittest.main()
