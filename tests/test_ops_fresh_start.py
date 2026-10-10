"""SYNTHETIC_TEST_ONLY: fresh-start bootstrap on temp dirs; fixtures only, no network, no credentials."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from desk import kraken_pacing_migration as upgrade, provider_pacing as pace, paper_cycle as cycle
from desk import paper_terminal_reconciliation as terminal, runtime_compatibility as runtime
from desk.evidence import EvidenceStore
from desk.model import canonical, digest
from desk.monitoring_budget import MonitoringBudget
from discovery import continuous as discovery
from tools import paper_entry_dispatcher as dispatcher, paper_scheduler as scheduler
from tools.ops import fresh_start as fs

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'config/experiments/paper-kraken-fresh.example.json'


class FreshStartBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.config = self.base / 'config.json'
        shutil.copy(EXAMPLE, self.config)
        # Shared pacing fixture copy (never reset by the tool) and discovery fixture.
        self.pacer = self.base / 'provider-pacing.sqlite'
        pace.initialize(self.pacer)
        policy = self.base / 'migration.json'
        p = patch.object(upgrade, 'POLICY', policy); p.start(); self.addCleanup(p.stop)
        policy.write_text(canonical({'version': 1, 'pins': []}))
        proposal = upgrade.review_plan(self.pacer)
        policy.write_text(canonical({'version': 1, 'pins': [proposal]}))
        upgrade.migrate(self.pacer)
        p = patch.dict(os.environ, {pace.ENV: str(self.pacer)}); p.start(); self.addCleanup(p.stop)
        self.discovery = self.base / 'discovery.sqlite'
        with patch.object(discovery.time, 'time', return_value=time.time() - 600):
            discovery.initialize(self.discovery)
        self.shared_before = self.shared_digest()

    def shared_digest(self):
        return [__import__('hashlib').sha256(p.read_bytes()).hexdigest() for p in (self.pacer, self.discovery)]

    def args(self, root, **extra):
        return dict(root=str(self.base / root), config=str(self.config), pacing_db=str(self.pacer),
                    discovery_db=str(self.discovery), **extra)

    def apply(self, name='exp-1'):
        return fs.apply(fs.plan(**self.args(name)))

    def cli(self, *argv):
        with patch('builtins.print') as out:
            code = fs.main(list(argv))
        return code, json.loads(out.call_args[0][0])


class FreshStartTests(FreshStartBase):
    def test_plan_creates_nothing_and_names_every_store_and_unit(self):
        value = fs.plan(**self.args('exp-1'))
        self.assertFalse((self.base / 'exp-1').exists())
        self.assertEqual(set(value['stores']), {'research_db', 'evidence_db', 'ledger_db', 'decisions_db',
                                                'journal', 'scheduler_lock', 'manifest'})
        self.assertEqual(set(value['units']), {'desk-paper-entry-dispatcher', 'desk-paper-held-cycle',
                                               'desk-paper-monitor', 'desk-decisions', 'desk-dashboard'})
        entry = value['units']['desk-paper-entry-dispatcher']['argv']
        self.assertIn(value['stores']['journal'], entry)
        self.assertIn(f"DESK_PROVIDER_PACING_DB={self.pacer}", value['units']['desk-paper-entry-dispatcher']['environment'])
        self.assertEqual(value['budgets']['monitoring_requests_per_rolling_hour'], 3600)
        self.assertFalse(value['entry_authorized'])
        self.assertEqual(self.shared_digest(), self.shared_before)

    def test_apply_passes_real_dispatcher_gate_and_scheduler_with_no_receipts_or_pins(self):
        manifest = self.apply()
        root = self.base / 'exp-1'
        self.assertEqual(stat_mode(root), 0o700)
        self.assertEqual(manifest['config_hash'], digest(json.loads(EXAMPLE.read_text())))
        self.assertEqual(manifest['implementation_hash'], runtime.implementation_hash())
        self.assertEqual(manifest['config_version'], '2026-10-11.paper-quote-kraken.fresh.1')
        self.assertEqual(json.loads((root / fs.MANIFEST).read_text())['kind'], 'fresh_start_manifest_v1')
        s = {k: Path(v['path']) for k, v in manifest['stores'].items()}
        # Real dispatcher --plan through the real scheduler entry pre-check.
        lock = root / fs.SCHEDULER_LOCK
        env = {'DESK_PAPER_SCHEDULER_IDENTITY': manifest['scheduler_identity']}
        argv = manifest['units']['desk-paper-entry-dispatcher']['argv']
        with patch.dict(os.environ, env), patch('builtins.print') as out:
            self.assertEqual(scheduler.main(argv[2:] + ['--plan']), 0)
        self.assertEqual(json.loads(out.call_args[0][0])['status'], 'PLAN')
        # Dispatcher preflight and terminal gate directly.
        ctx = dispatcher.plan(config=str(self.config), research_db=str(s['research_db']),
                              evidence_db=str(s['evidence_db']), ledger_db=str(s['ledger_db']),
                              discovery_db=str(self.discovery), pacing_db=str(self.pacer),
                              journal=str(s['journal']), taker=fs.PRODUCTION_TAKER, amount_raw=100_000_000,
                              pool_fee_bps='25')
        self.assertEqual(digest(ctx), manifest['dispatcher_context_hash'])
        dispatcher._preflight(ctx)
        self.assertFalse(terminal.gate(EvidenceStore(s['evidence_db'], read_only=True), s['research_db'], ()))
        with dispatcher._journal(s['journal']) as journal:
            values = dispatcher._validate(journal, ctx)
        self.assertEqual({k: len(v) for k, v in values.items()}, {'context': 1, 'intents': 0, 'results': 0})
        # No successor pins / reconciliation receipts / retained history rows anywhere.
        with sqlite3.connect(s['ledger_db']) as c:
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertFalse({n for n in names if 'successor' in n or 'extension' in n or 'receipt' in n})
            self.assertEqual(c.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0], 0)
        with sqlite3.connect(s['evidence_db']) as c:
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertFalse({n for n in names if 'receipt' in n or 'successor' in n or 'handoff' in n})
            self.assertEqual(c.execute('SELECT COUNT(*) FROM ownership_admissions').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT cap,window_seconds,total,blocked FROM paper_monitoring_budget').fetchone(),
                             (3600, 3600, 0, None))
        with sqlite3.connect(s['research_db']) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM scans').fetchone()[0], 0)
            self.assertEqual(fresh_limits(c, s['research_db']), (1000, 25))
        self.assertEqual(self.shared_digest(), self.shared_before)

    def test_monitoring_snapshot_and_budget_values(self):
        manifest = self.apply()
        s = {k: Path(v['path']) for k, v in manifest['stores'].items()}
        cfg = json.loads(EXAMPLE.read_text())
        snap = MonitoringBudget(EvidenceStore(s['evidence_db']), s['ledger_db'], cfg).snapshot()
        self.assertEqual(snap['status'], 'AVAILABLE')

    def test_refuses_existing_root_and_never_touches_it(self):
        root = self.base / 'exp-1'; root.mkdir(); (root / 'keep').write_text('x')
        with self.assertRaisesRegex(fs.FreshStartError, 'must not exist'):
            fs.plan(**self.args('exp-1'))
        self.assertEqual(sorted(p.name for p in root.iterdir()), ['keep'])

    def test_refuses_symlinked_root_and_symlinked_parent(self):
        target = self.base / 'elsewhere'; target.mkdir()
        (self.base / 'link').symlink_to(target)
        with self.assertRaisesRegex(fs.FreshStartError, 'symlink|must not exist'):
            fs.plan(**self.args('link'))
        with self.assertRaisesRegex(fs.FreshStartError, 'symlink'):
            fs.plan(**self.args('link/exp-1'))
        self.assertEqual(list(target.iterdir()), [])

    def test_refuses_relative_dotdot_and_missing_inputs(self):
        for root in ('exp', str(self.base / 'a' / '..' / 'exp')):
            with self.assertRaises(fs.FreshStartError):
                fs.plan(**{**self.args('x'), 'root': root})
        with self.assertRaises(fs.FreshStartError):
            fs.plan(**{**self.args('x'), 'pacing_db': str(self.base / 'missing.sqlite')})

    def test_refuses_config_without_kraken_valuation_or_inside_shared_conflict(self):
        cfg = json.loads(EXAMPLE.read_text()); del cfg['paper_usd_valuation_version']
        bad = self.base / 'bad.json'; bad.write_text(json.dumps(cfg))
        with self.assertRaises(fs.FreshStartError):
            fs.plan(**{**self.args('x'), 'config': str(bad)})

    def test_pacing_environment_must_match_and_failure_leaves_no_valid_root_marker(self):
        value = fs.plan(**self.args('exp-1'))
        with patch.dict(os.environ, {pace.ENV: str(self.base / 'other.sqlite')}):
            with self.assertRaisesRegex(fs.FreshStartError, 'must equal'):
                fs.apply(value)
        self.assertFalse((self.base / 'exp-1').exists())

    def test_plan_hash_and_drift_bound_apply(self):
        value = fs.plan(**self.args('exp-1'))
        self.config.write_text(self.config.read_text().replace('"50"', '"51"'))
        with self.assertRaisesRegex(fs.FreshStartError, 'changed since'):
            fs.apply(value)
        self.assertFalse((self.base / 'exp-1').exists())

    def test_cli_default_is_dry_run_and_execute_needs_exact_hash(self):
        base = ['apply', '--root', str(self.base / 'exp-1'), '--config', str(self.config),
                '--pacing-db', str(self.pacer), '--discovery-db', str(self.discovery)]
        code, out = self.cli(*base)
        self.assertEqual((code, out['status']), (0, 'DRY_RUN'))
        self.assertFalse((self.base / 'exp-1').exists())
        code, out = self.cli(*base, '--execute', '--approved-plan-hash', '0' * 64)
        self.assertEqual((code, out['status']), (2, 'BLOCKED'))
        self.assertFalse((self.base / 'exp-1').exists())
        code, out = self.cli(*base, '--execute', '--approved-plan-hash', out_hash(self, base))
        self.assertEqual((code, out['status']), (0, 'APPLIED'))
        self.assertTrue((self.base / 'exp-1' / fs.MANIFEST).is_file())


class RotationTests(FreshStartBase):
    def rotate_args(self, old, new):
        return dict(self.args(new), rotate_from=str(self.base / old))

    def test_rotate_from_flat_ledger_leaves_old_root_byte_identical(self):
        self.apply('exp-1')
        before = fs.tree_digest(self.base / 'exp-1')
        flat = fs.verify_flat(self.base / 'exp-1')
        self.assertEqual((flat['mode'], flat['positions']), ('RUNNING', 0))
        manifest = fs.apply(fs.plan(**self.rotate_args('exp-1', 'exp-2')))
        self.assertEqual(manifest['rotated_from'], str(self.base / 'exp-1'))
        self.assertEqual(fs.tree_digest(self.base / 'exp-1'), before)
        self.assertNotEqual(manifest['stores']['ledger_db']['path'], str(self.base / 'exp-1' / 'paper-ledger.sqlite'))
        self.assertEqual(self.shared_digest(), self.shared_before)

    def test_cli_rotate_dry_run_then_execute(self):
        self.apply('exp-1'); before = fs.tree_digest(self.base / 'exp-1')
        base = ['rotate', '--from', str(self.base / 'exp-1'), '--to', str(self.base / 'exp-2'),
                '--config', str(self.config), '--pacing-db', str(self.pacer), '--discovery-db', str(self.discovery)]
        code, out = self.cli(*base)
        self.assertEqual((code, out['status']), (0, 'DRY_RUN'))
        self.assertFalse((self.base / 'exp-2').exists())
        code, out = self.cli(*base, '--execute', '--approved-plan-hash', out['plan_hash'])
        self.assertEqual((code, out['status']), (0, 'APPLIED'))
        self.assertEqual(fs.tree_digest(self.base / 'exp-1'), before)

    def test_rotation_refused_with_open_position(self):
        self.apply('exp-1'); before = fs.tree_digest(self.base / 'exp-1')
        state = {'positions': {'MINT': {}}, 'mode': 'RUNNING'}
        with patch.object(fs, 'validate_checkpoint', return_value=state):
            with self.assertRaisesRegex(fs.FreshStartError, 'open position'):
                fs.verify_flat(self.base / 'exp-1')
            code, out = self.cli('rotate', '--from', str(self.base / 'exp-1'), '--to', str(self.base / 'exp-2'),
                                 '--config', str(self.config), '--pacing-db', str(self.pacer),
                                 '--discovery-db', str(self.discovery), '--execute', '--approved-plan-hash', '0' * 64)
        self.assertEqual(code, 2)
        self.assertFalse((self.base / 'exp-2').exists())
        self.assertEqual(fs.tree_digest(self.base / 'exp-1'), before)

    def test_rotation_refused_for_non_flat_modes(self):
        self.apply('exp-1')
        for mode in ('EXIT_ONLY', 'LIQUIDATING', 'STOPPED'):
            with patch.object(fs, 'validate_checkpoint', return_value={'positions': {}, 'mode': mode}):
                with self.assertRaisesRegex(fs.FreshStartError, 'mode'):
                    fs.verify_flat(self.base / 'exp-1')

    def test_rotation_refused_for_corrupt_checkpoint_missing_manifest_wal_and_nesting(self):
        self.apply('exp-1')
        ledger = self.base / 'exp-1' / 'paper-ledger.sqlite'
        # un-checkpointed WAL: not quiesced
        Path(str(ledger) + '-wal').write_bytes(b'x')
        with self.assertRaisesRegex(fs.FreshStartError, 'WAL'):
            fs.verify_flat(self.base / 'exp-1')
        Path(str(ledger) + '-wal').unlink()
        with self.assertRaisesRegex(fs.FreshStartError, 'manifest'):
            fs.verify_flat(self.base)
        with self.assertRaisesRegex(fs.FreshStartError, 'inside or around'):
            fs.plan(**self.rotate_args('exp-1', 'exp-1/nested'))
        with closing(sqlite3.connect(ledger)) as c:
            c.execute("UPDATE state SET payload='{}' WHERE id=1"); c.commit()
        with self.assertRaisesRegex(fs.FreshStartError, 'invalid'):
            fs.verify_flat(self.base / 'exp-1')

    def test_rotate_requires_from_and_apply_rejects_from(self):
        base = ['--config', str(self.config), '--pacing-db', str(self.pacer), '--discovery-db', str(self.discovery)]
        self.assertEqual(self.cli('rotate', '--to', str(self.base / 'x'), *base)[0], 2)
        self.assertEqual(self.cli('apply', '--root', str(self.base / 'x'), '--from', str(self.base), *base)[0], 2)


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


def fresh_limits(c, path):
    from desk import allowance_policy
    c.row_factory = sqlite3.Row
    return allowance_policy.research_limits(c, path)


def out_hash(case, base):
    code, out = case.cli(*base)
    return out['plan_hash']


if __name__ == '__main__':
    unittest.main()
