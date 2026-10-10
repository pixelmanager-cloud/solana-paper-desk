"""tools.ops.status: fixtures built with the repo's own schema creators; no network."""
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

import shutil
import subprocess
import sys
from unittest import mock

from desk.engine import initial_state, transition
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.ledger import Ledger
from desk.model import canonical
from desk.monitoring_budget import MonitoringBudget
from desk import provider_pacing
from tests.helpers import T, config, event
from tests.test_quote_execution import QuoteExecutionTests
from tools import paper_entry_dispatcher as dispatcher
from tools.ops import status

NOW = 1791417600 + 100


def tree_state(root):
    out = {}
    for p in sorted(Path(root).rglob('*')):
        if p.is_file() and not p.is_symlink():
            # A WAL reader legitimately touches the -shm index mtime (contents stay
            # identical); the database and -wal bytes and mtimes must never change.
            out[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest(),
                           None if p.name.endswith('-shm') else p.stat().st_mtime_ns)
    return out


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd=None):
        self.calls.append((argv, cwd))
        if argv[0] == 'systemctl' and argv[1] == 'list-unit-files':
            return 'desk-paper-entry.timer enabled\ndesk-dashboard.service enabled\n'
        if argv[0] == 'systemctl' and argv[1] == 'show':
            return (f'ActiveState=active\nUnitFileState=enabled\nResult=success\n'
                    f'WorkingDirectory=/opt/rel/{argv[2]}\nDropInPaths=/etc/x/60-reviewed-release.conf\n')
        if argv[0] == 'ss':
            return 'LISTEN 0 128 127.0.0.1:8765 0.0.0.0:*\n'
        if '-I' in argv:
            return 'ab' * 32 + '\n'
        raise AssertionError(argv)


class StatusFixture(unittest.TestCase):
    """Fixtures and helpers only; the test classes below inherit them without re-running each other."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(os.path.realpath(self.tmp.name)) / 'data'
        (self.data / 'entry-dispatch').mkdir(parents=True)
        self.cfg = config()
        self.cfg['paper_quote_execution_version'] = 1
        self.config_path = self.data / 'config.json'
        self.config_path.write_text(json.dumps(self.cfg))
        self.ledger = self.data / 'ledger.sqlite'
        ledger = Ledger(self.ledger)
        ledger.apply(event(), self.cfg, transition, initial_state)
        ledger.close()
        # Plain paper config yields a synthetic BUY (quote-execution config does not).
        self.buy_ledger = self.data / 'buy-ledger.sqlite'
        self.buy_cfg = config()
        buy = Ledger(self.buy_ledger)
        buy.apply(event(), self.buy_cfg, transition, initial_state)
        buy.close()
        store = EvidenceStore(self.data / 'evidence.sqlite')
        HistoryProgress(store)
        MonitoringBudget(store, self.ledger, self.cfg).provision()
        with store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
            c.execute("INSERT INTO paper_observation_passes VALUES('p1','h',NULL)")
            c.execute("INSERT INTO paper_observation_passes VALUES('p2','h','o')")
        pacing = self.data / 'provider-pacing.sqlite'
        provider_pacing.initialize(pacing)
        journal = self.data / 'entry-dispatch' / 'dispatch.sqlite'
        with sqlite3.connect(journal) as c:
            for sql in (*dispatcher.SCHEMAS.values(), *dispatcher._guards().values()):
                c.execute(sql)
            c.execute("INSERT INTO intents VALUES('i1','M1','S1',?,?)", (canonical({'scan_id': 'scan1', 'at': 1.0}), 'h' * 64))
            c.execute("INSERT INTO results VALUES('r1',?,?)", (canonical({'scan_id': 'scan1', 'at': 2.0, 'result': {'status': 'REJECTED', 'blockers': ['X']}}), 'h' * 64))
        for p in self.data.rglob('*.sqlite'):
            p.chmod(0o600)

    def report(self, **kw):
        return status.build_report(str(self.data), str(kw.pop('ledger', self.ledger)), str(kw.pop('config', self.config_path)), now=NOW,
                                   runner=kw.pop('runner', FakeRunner()), **kw)

class StatusTests(StatusFixture):
    def test_full_report_contents(self):
        r = self.report(release_dir=str(self.data), systemd=True, check=True)
        self.assertEqual(r['status'], 'OK')
        self.assertEqual(r['release']['runtime_digest'], 'ab' * 32)
        led = r['ledger']
        self.assertEqual(led['events'], 1)
        self.assertEqual(led['state']['mode'], 'RUNNING')
        self.assertEqual(led['positions'], [])
        self.assertTrue(led['config']['file_equals_stored_config'])
        self.assertEqual(led['config']['file_sha256'], hashlib.sha256(self.config_path.read_bytes()).hexdigest())
        self.assertEqual(r['observation_passes']['null_ids'], ['p1'])
        self.assertEqual(r['observation_passes']['resolved'], 1)
        self.assertEqual(r['dispatcher_journal']['last_results'][0]['status'], 'REJECTED')
        self.assertEqual(r['budgets']['monitoring']['remaining_in_window'], 60)
        self.assertEqual({p['provider'] for p in r['provider_pacing']['providers']}, {'helius', 'jupiter'})
        self.assertTrue(all(s['quick_check'] == 'ok' for s in r['store_inventory']['stores']))
        self.assertEqual(r['systemd']['units']['desk-dashboard.service']['ActiveState'], 'active')
        self.assertEqual(r['dashboard']['non_loopback'], [])
        self.assertNotIn('HELD_POSITION', r['blockers'])
        self.assertIn('NULL_OBSERVATION_PASSES', r['blockers'])

    def test_open_position_and_fill_labels(self):
        cfg_path = self.data / 'buy-config.json'
        cfg_path.write_text(json.dumps(self.buy_cfg))
        r = self.report(ledger=self.buy_ledger, config=cfg_path)
        led = r['ledger']
        self.assertEqual([p['mint'] for p in led['positions']], ['SYNTHETIC_A'])
        self.assertEqual(led['positions'][0]['initial_cost_sol'], '0.083383333')
        self.assertEqual((led['fills'][0]['side'], led['fills'][0]['mint']), ('buy', 'SYNTHETIC_A'))
        self.assertEqual(led['fills'][0]['provenance'], 'SYNTHETIC_TEST_ONLY')
        self.assertIsNone(led['fills'][0]['execution_label'])  # synthetic fixture carries no label
        self.assertTrue(led['config']['file_equals_stored_config'])
        self.assertIn('HELD_POSITION', r['blockers'])

    def test_changed_config_file_is_reported(self):
        self.cfg['stop_loss'] = '0.5'
        self.config_path.write_text(json.dumps(self.cfg))
        r = self.report()
        self.assertFalse(r['ledger']['config']['file_equals_stored_config'])
        self.assertIn('CONFIG_FILE_DIFFERS_FROM_LEDGER', r['blockers'])

    def test_non_loopback_dashboard_flagged(self):
        runner = FakeRunner()
        base = runner.__call__
        runner = lambda argv, cwd=None: 'LISTEN 0 128 0.0.0.0:8765 0.0.0.0:*\n' if argv[0] == 'ss' else base(argv, cwd)
        r = self.report(runner=runner)
        self.assertEqual(r['dashboard']['non_loopback'], ['0.0.0.0'])
        self.assertIn('DASHBOARD_NON_LOOPBACK', r['blockers'])

    def test_exhausted_monitoring_budget_blocks(self):
        with sqlite3.connect(self.data / 'evidence.sqlite') as c:
            cap = c.execute('SELECT cap FROM paper_monitoring_budget').fetchone()[0]
            c.executescript('DROP TRIGGER paper_monitoring_reservations_insert;')
            for i in range(cap):
                c.execute("INSERT INTO paper_monitoring_reservations VALUES(?,?,?,?,?,?,?)", (i + 1, NOW - 10, 's', 'm', 'c', 'x', 'p'))
                c.execute("INSERT INTO paper_monitoring_outcomes VALUES(?,?)", (i + 1, 'e'))
        r = self.report()
        self.assertTrue(r['budgets']['monitoring']['exhausted'])
        self.assertIn('MONITORING_BUDGET_EXHAUSTED_OR_PENDING', r['blockers'])

    def test_pacing_blocked_until_blocks(self):
        with sqlite3.connect(self.data / 'provider-pacing.sqlite') as c:
            c.execute("UPDATE state SET blocked_until=? WHERE provider='jupiter'", (NOW + 500,))
        self.assertIn('PACING_BLOCKED_OR_PENDING', self.report()['blockers'])

    def test_symlinked_ledger_fails_closed_without_reading(self):
        link = self.data / 'ledger-link.sqlite'
        link.symlink_to(self.ledger)
        r = status.build_report(str(self.data), str(link), str(self.config_path), now=NOW, runner=FakeRunner())
        self.assertEqual(r['ledger']['status'], 'ERROR')
        self.assertEqual(r['ledger']['error'], 'PATH_NOT_CANONICAL')
        self.assertEqual(r['status'], 'ERROR')
        self.assertIn('UNREADABLE_LEDGER', r['blockers'])

    def test_hardlinked_and_symlinked_data_dir_refused(self):
        hard = self.data / 'hard.sqlite'
        os.link(self.ledger, hard)
        r = status.build_report(str(self.data), str(hard), str(self.config_path), now=NOW, runner=FakeRunner())
        self.assertEqual(r['ledger']['error'], 'PATH_NOT_SINGLE_REGULAR_FILE')
        alias = self.data.parent / 'alias'
        alias.symlink_to(self.data)
        r = status.build_report(str(alias), str(self.ledger), str(self.config_path), now=NOW, runner=FakeRunner())
        self.assertEqual(r['blockers'], ['DATA_DIR_INVALID'])

    def test_relative_path_refused(self):
        r = status.build_report(str(self.data), 'ledger.sqlite', str(self.config_path), now=NOW, runner=FakeRunner())
        self.assertEqual(r['ledger']['error'], 'PATH_NOT_ABSOLUTE')

    def test_missing_store_is_error_not_created(self):
        (self.data / 'evidence.sqlite').unlink()
        r = self.report()
        self.assertEqual(r['budgets']['status'], 'ERROR')
        self.assertFalse((self.data / 'evidence.sqlite').exists())

    def test_never_writes_bytes_or_mtimes_or_new_files(self):
        before = tree_state(self.data)
        self.report(release_dir=str(self.data), systemd=True, check=True)
        self.assertEqual(tree_state(self.data), before)

    def test_cli_exit_codes(self):
        out = io.StringIO()
        code = status.main(['--data', str(self.data), '--ledger', str(self.ledger), '--config', str(self.config_path)],
                           runner=FakeRunner(), out=out)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())['paper_only'], True)
        out = io.StringIO()
        code = status.main(['--data', str(self.data), '--ledger', str(self.data / 'missing.sqlite'), '--config', str(self.config_path)],
                           runner=FakeRunner(), out=out)
        self.assertEqual(code, 2)


REPO = Path(__file__).resolve().parents[1]


class ReleaseProbeWritesNothing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.release = Path(os.path.realpath(self.tmp.name)) / 'release'
        shutil.copytree(REPO / 'desk', self.release / 'desk', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

    def listing(self):
        return sorted(str(p.relative_to(self.release)) for p in self.release.rglob('*'))

    def test_real_probe_subprocess_leaves_release_tree_untouched(self):
        before = self.listing()
        report = status.release_section(str(self.release), status.default_runner)
        self.assertEqual(report['status'], 'OK')
        from desk.runtime_compatibility import implementation_hash
        self.assertEqual(report['runtime_digest'], implementation_hash())
        self.assertEqual(self.listing(), before)
        self.assertEqual([p for p in self.listing() if '__pycache__' in p or p.endswith('.pyc')], [])

    def test_probe_argv_and_environment_forbid_bytecode(self):
        calls = []
        status.release_section(str(self.release), lambda argv, cwd=None: calls.append((argv, cwd)) or 'ab' * 32)
        argv, cwd = calls[0]
        self.assertEqual(argv[1:4], ['-I', '-B', '-c'])
        self.assertIn('sys.dont_write_bytecode=True', argv[4])
        seen = {}

        def fake_run(argv, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 0, stdout='x', stderr='')
        with mock.patch.object(status.subprocess, 'run', fake_run):
            status.default_runner(['true'])
        self.assertEqual(seen['env']['PYTHONDONTWRITEBYTECODE'], '1')

    def test_tool_itself_disables_bytecode_before_importing_desk(self):
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONDONTWRITEBYTECODE'}
        code = ("import sys; assert not sys.dont_write_bytecode; import tools.ops.status; "
                "print(sys.dont_write_bytecode)")
        out = subprocess.run([sys.executable, '-c', code], cwd=REPO, env=env, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), 'True')


class OpenModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))

    def wal_db(self, name='wal.sqlite'):
        path = self.dir / name
        with closing_connect(path) as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE t(v INTEGER)')
            c.execute('INSERT INTO t VALUES(1)')
            c.commit()
        return path

    def uri_used(self, path):
        with mock.patch.object(status.sqlite3, 'connect', wraps=sqlite3.connect) as spy:
            status.connect_ro(path).close()
        return spy.call_args[0][0]

    def test_rollback_journal_database_is_never_opened_immutable(self):
        pacing = self.dir / 'provider-pacing.sqlite'
        provider_pacing.initialize(pacing)
        self.assertFalse(any(Path(str(pacing) + s).exists() for s in ('-wal', '-shm', '-journal')))
        self.assertEqual(status.open_mode(pacing), 'ro')
        uri = self.uri_used(pacing)
        self.assertIn('mode=ro', uri)
        self.assertNotIn('immutable', uri)

    def test_quiescent_wal_database_without_wal_file_is_read_immutable(self):
        path = self.wal_db()
        self.assertEqual(path.read_bytes()[18], 2)
        self.assertFalse(Path(str(path) + '-wal').exists())
        self.assertEqual(status.open_mode(path), 'immutable')
        self.assertIn('immutable=1', self.uri_used(path))

    def test_live_wal_with_open_writer_uses_mode_ro_and_sees_unflushed_pages(self):
        path = self.wal_db()
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('INSERT INTO t VALUES(2)')
        writer.commit()                      # committed, but only in the -wal file
        self.assertTrue(Path(str(path) + '-wal').stat().st_size > 0)
        main_before = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(status.open_mode(path), 'ro')
        with status.connect_ro(path) as c:
            self.assertEqual(sorted(r[0] for r in c.execute('SELECT v FROM t')), [1, 2])
            with self.assertRaises(sqlite3.OperationalError):
                c.execute('INSERT INTO t VALUES(3)')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), main_before)
        writer.execute('INSERT INTO t VALUES(4)')    # the writer is not blocked by the reader
        writer.commit()
        with status.connect_ro(path) as c:
            self.assertEqual(sorted(r[0] for r in c.execute('SELECT v FROM t')), [1, 2, 4])

    def test_header_not_filename_decides(self):
        path = self.dir / 'junk.sqlite'
        path.write_bytes(b'not a database' * 20)
        self.assertEqual(status.open_mode(path), 'ro')
        with self.assertRaises(sqlite3.DatabaseError):
            with status.connect_ro(path) as c:
                c.execute('SELECT 1 FROM sqlite_master').fetchall()

    def test_pacing_pending_is_a_warning_and_stuck_only_when_unchanged(self):
        pacing = self.dir / 'provider-pacing.sqlite'
        provider_pacing.initialize(pacing)
        with sqlite3.connect(pacing) as c:
            c.execute("UPDATE state SET pending='ticket-1' WHERE provider='jupiter'")
        one = status.pacing_section(pacing, NOW)
        self.assertEqual((one['pending_providers'], one['pending_stuck']), (['jupiter'], []))
        sleeps = []
        two = status.pacing_section(pacing, NOW, samples=2, interval=3.0, sleep=sleeps.append)
        self.assertEqual((sleeps, two['pending_stuck']), ([3.0], ['jupiter']))

        def change(_):
            with sqlite3.connect(pacing) as c:
                c.execute("UPDATE state SET pending='ticket-2' WHERE provider='jupiter'")
        moved = status.pacing_section(pacing, NOW, samples=2, sleep=change)
        self.assertEqual((moved['pending_providers'], moved['pending_stuck']), (['jupiter'], []))

        def clear(_):
            with sqlite3.connect(pacing) as c:
                c.execute("UPDATE state SET pending=NULL")
        cleared = status.pacing_section(pacing, NOW, samples=2, sleep=clear)
        self.assertEqual((cleared['pending_providers'], cleared['pending_stuck']), ([], []))


class closing_connect:
    def __init__(self, path):
        self.c = sqlite3.connect(path)

    def __enter__(self):
        return self.c

    def __exit__(self, *exc):
        self.c.close()


class FailClosedTests(StatusFixture):
    def blockers(self, **kw):
        return self.report(**kw)['blockers']

    # -- binding ---------------------------------------------------------------
    def test_monitoring_budget_binding_matches_for_the_provisioned_ledger_and_config(self):
        r = self.report()
        binding = r['budgets']['monitoring']['binding']
        self.assertEqual((binding['ledger_matches'], binding['config_hash_matches']), (True, True))
        self.assertNotIn('MONITORING_BINDING_MISMATCH', r['blockers'])
        self.assertNotIn('MONITORING_BINDING_UNVERIFIED', r['blockers'])

    def test_binding_mismatch_for_a_different_ledger_or_config_is_a_blocker(self):
        cfg_path = self.data / 'buy-config.json'
        cfg_path.write_text(json.dumps(self.buy_cfg))
        r = self.report(ledger=self.buy_ledger, config=cfg_path)
        binding = r['budgets']['monitoring']['binding']
        self.assertEqual((binding['ledger_matches'], binding['config_hash_matches']), (False, False))
        self.assertIn('MONITORING_BINDING_MISMATCH', r['blockers'])
        other = self.data / 'other-ledger.sqlite'
        shutil.copy(self.ledger, other)
        os.chmod(other, 0o600)
        r = self.report(ledger=other)
        self.assertEqual(r['budgets']['monitoring']['binding']['ledger_matches'], False)
        self.assertIn('MONITORING_BINDING_MISMATCH', r['blockers'])

    def test_unreadable_config_leaves_binding_unverified_which_blocks(self):
        r = status.build_report(str(self.data), str(self.ledger), str(self.data / 'missing.json'), now=NOW, runner=FakeRunner())
        self.assertIsNone(r['budgets']['monitoring']['binding']['config_hash_matches'])
        self.assertIn('MONITORING_BINDING_UNVERIFIED', r['blockers'])

    # -- ownership budgets -------------------------------------------------------
    def test_exhausted_ownership_budget_beyond_the_first_fifty_rows_is_found(self):
        with sqlite3.connect(self.data / 'evidence.sqlite') as c:
            for i in range(80):
                c.execute('INSERT INTO ownership_budgets VALUES(?,?,?,?)', ('b%03d' % i, 'h', 10 if i == 70 else 0, 10))
        r = self.report()
        self.assertEqual(len(r['budgets']['ownership']['budgets']), status.BOUND)
        self.assertTrue(r['budgets']['ownership']['budgets_truncated'])
        self.assertEqual(r['budgets']['ownership']['exhausted'], 1)
        self.assertIn('OWNERSHIP_BUDGET_EXHAUSTED', r['blockers'])

    def test_null_ownership_ceiling_is_a_blocker_not_a_crash(self):
        evidence = self.data / 'evidence.sqlite'
        evidence.unlink()
        with sqlite3.connect(evidence) as c:
            c.execute('CREATE TABLE ownership_budgets(id TEXT PRIMARY KEY,used INTEGER,ceiling INTEGER)')
            c.execute("INSERT INTO ownership_budgets VALUES('a',1,NULL)")
        os.chmod(evidence, 0o600)
        r = self.report()
        self.assertEqual(r['budgets']['status'], 'OK')
        self.assertEqual(r['budgets']['ownership']['missing_values'], 1)
        self.assertIn('OWNERSHIP_CEILING_MISSING', r['blockers'])

    def test_null_ledger_mode_is_a_blocker_not_a_crash(self):
        with sqlite3.connect(self.ledger) as c:
            state = json.loads(c.execute('SELECT payload FROM state').fetchone()[0])
            state['mode'] = None
            c.execute('UPDATE state SET payload=?', (json.dumps(state),))
        self.assertIn('LEDGER_MODE_MISSING', self.blockers())

    # -- inventory ---------------------------------------------------------------
    def test_walk_errors_raise_instead_of_hiding_stores(self):
        def failing_walk(top, followlinks=False, onerror=None):
            # Like the real os.walk: an unreadable directory is silently skipped unless onerror is given.
            if onerror is not None:
                onerror(PermissionError('denied'))
            return iter(())
        with mock.patch.object(status.os, 'walk', failing_walk):
            r = self.report()
        self.assertEqual(r['store_inventory']['status'], 'ERROR')
        self.assertIn('UNREADABLE_STORE_INVENTORY', r['blockers'])

    def test_symlinked_and_hardlinked_stores_and_dirs_become_blockers(self):
        (self.data / 'alias.sqlite').symlink_to(self.data / 'evidence.sqlite')
        elsewhere = self.data.parent / 'elsewhere'
        elsewhere.mkdir()
        (self.data / 'linkdir').symlink_to(elsewhere)
        inv = self.report()['store_inventory']
        self.assertEqual(inv['symlinked'], ['alias.sqlite', 'linkdir'])
        self.assertIn('STORE_SYMLINKED', self.blockers())
        (self.data / 'alias.sqlite').unlink()
        (self.data / 'linkdir').unlink()
        os.link(self.data / 'provider-pacing.sqlite', self.data / 'pacing-copy.sqlite')
        r = self.report()
        self.assertEqual(r['store_inventory']['hardlinked'], ['pacing-copy.sqlite', 'provider-pacing.sqlite'])
        self.assertIn('STORE_HARDLINKED', r['blockers'])

    # -- pacing ----------------------------------------------------------------
    def set_pending(self, value):
        with sqlite3.connect(self.data / 'provider-pacing.sqlite') as c:
            c.execute('UPDATE state SET pending=? WHERE provider=?', (value, 'jupiter'))

    def test_pacing_pending_is_only_a_warning_for_a_single_sample(self):
        self.set_pending('ticket-1')
        r = self.report()
        self.assertEqual(r['warnings'], ['PACING_PENDING'])
        self.assertNotIn('PACING_PENDING_STUCK', r['blockers'])
        self.assertNotIn('PACING_BLOCKED_OR_PENDING', r['blockers'])

    def test_pacing_pending_unchanged_across_two_samples_blocks(self):
        self.set_pending('ticket-1')
        sleeps = []
        r = self.report(samples=2, sample_interval=3.0, sleep=sleeps.append)
        self.assertEqual(sleeps, [3.0])
        self.assertIn('PACING_PENDING_STUCK', r['blockers'])

    def test_pacing_pending_that_moves_between_samples_does_not_block(self):
        self.set_pending('ticket-1')
        r = self.report(samples=2, sleep=lambda _: self.set_pending('ticket-2'))
        self.assertNotIn('PACING_PENDING_STUCK', r['blockers'])
        self.assertEqual(r['warnings'], ['PACING_PENDING_CHANGED'])
        self.set_pending('ticket-3')
        r = self.report(samples=2, sleep=lambda _: self.set_pending(None))
        self.assertEqual((r['warnings'], r['blockers'].count('PACING_PENDING_STUCK')), ([], 0))

    def test_cli_samples_flag(self):
        self.set_pending('ticket-1')
        out = io.StringIO()
        code = status.main(['--data', str(self.data), '--ledger', str(self.ledger), '--config', str(self.config_path),
                            '--samples', '2', '--sample-interval', '0'], runner=FakeRunner(), out=out)
        self.assertEqual(code, 0)
        self.assertIn('PACING_PENDING_STUCK', json.loads(out.getvalue())['blockers'])

    def test_reads_stay_write_free_with_new_checks(self):
        (self.data / 'x.sqlite').symlink_to(self.data / 'evidence.sqlite')
        before = tree_state(self.data)
        self.report(release_dir=str(self.data), systemd=True, check=True)
        self.assertEqual(tree_state(self.data), before)


class QuoteFillLabelTests(StatusFixture):
    def quote_ledger(self, sell):
        qe = QuoteExecutionTests('test_buy_full_exit_exact_cash_fees_and_assumptions')
        qe.setUp()
        self.addCleanup(qe.doCleanups)
        qe.buy_fill()
        if sell:
            qe.apply(qe.market(T + 1, danger=True), (qe.quote('sell', qe.raw, 9_500_000, at=T + 1),))
        target = self.data / ('quote-%s.sqlite' % ('closed' if sell else 'open'))
        with closing_connect(qe.path) as src, closing_connect(target) as dst:
            src.backup(dst)
        os.chmod(target, 0o600)
        cfg = self.data / 'quote-config.json'
        cfg.write_text(json.dumps(qe.cfg))
        return target, cfg

    def test_quote_execution_fills_report_the_execution_unverified_label(self):
        ledger, cfg = self.quote_ledger(sell=True)
        led = self.report(ledger=ledger, config=cfg)['ledger']
        self.assertEqual([f['side'] for f in led['fills']], ['buy', 'sell'])
        self.assertEqual([f['execution_label'] for f in led['fills']], ['EXECUTION_UNVERIFIED'] * 2)
        self.assertEqual(led['positions'], [])
        ledger, cfg = self.quote_ledger(sell=False)
        led = self.report(ledger=ledger, config=cfg)['ledger']
        self.assertEqual(len(led['positions']), 1)
        self.assertEqual(led['fills'][0]['execution_label'], 'EXECUTION_UNVERIFIED')


if __name__ == '__main__':
    unittest.main()
