"""tools.ops.status: fixtures built with the repo's own schema creators; no network."""
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from desk.engine import initial_state, transition
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.ledger import Ledger
from desk.model import canonical
from desk.monitoring_budget import MonitoringBudget
from desk import provider_pacing
from tests.helpers import config, event
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


class StatusTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
