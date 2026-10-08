"""SYNTHETIC_TEST_ONLY: consumer diagnostics, no providers, repair or fills."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from desk.dashboard import handler
from desk.engine import initial_state, transition
from desk.ledger import Ledger
from desk.monitor import tick
from desk.paper_view import paper_status
from tests.helpers import ROOT, T, config, event
from tests.test_cloud_decision_renderer import render


class PaperConsumerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'synthetic-paper.sqlite'
        self.cfg = config()
        guard = patch('socket.socket', side_effect=AssertionError('Synthetic consumer tests only'))
        guard.start()
        self.addCleanup(guard.stop)

    def seed(self, *events):
        ledger = Ledger(self.path)
        try:
            for observation in events:
                ledger.apply(observation, self.cfg, transition, initial_state)
        finally:
            ledger.close()

    def connection(self):
        return sqlite3.connect(self.path)

    def records(self):
        c = sqlite3.connect(self.path)
        try:
            return {table:c.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                    for table in ('metadata','events','outcomes','state','raw_events','health','sqlite_sequence')}
        finally:
            c.close()

    def assert_recovery(self, reason):
        before = self.records()
        saved_bytes = self.path.read_bytes()
        with patch('desk.monitor.Ledger', side_effect=AssertionError('No writable ledger for corruption')):
            for now in (T-1, T+1, T+86400):
                view = paper_status(self.path, now=now)
                monitor = tick(self.path, self.cfg, now=now)
                for result in (view, monitor):
                    self.assertEqual(result['status'], 'RECOVERY_REQUIRED')
                    self.assertEqual(result['recovery_reason'], reason)
                    self.assertFalse(result['automatic_entry_enabled'])
                    self.assertIn('Recovery is required', result['notice'])
                for key in ('cash_sol','realized_pnl_sol','estimated_equity_sol'):
                    self.assertIsNone(view[key])
                self.assertEqual(view['positions'], [])
                self.assertEqual(view['recent_outcomes'], [])
                self.assertNotIn('outcomes', monitor)
                self.assertEqual(self.records(), before)
                self.assertEqual(self.path.read_bytes(), saved_bytes)
        return view

    def test_missing_checkpoint_with_retained_fills_is_recovery_not_empty(self):
        self.seed(event(), event(T+5, reserve_sol='160'))
        with self.connection() as c:
            c.execute('DELETE FROM state')
        view = self.assert_recovery('CHECKPOINT_MISSING')
        dom = render(paper_data=view, now_ms=(T+20)*1000)['paper_html']
        self.assertIn('recovery required', dom)
        self.assertNotIn('No simulated positions open', dom)
        self.assertNotIn('Simulated cash:', dom)

    def test_each_surviving_identity_blocks_missing_checkpoint_empty_success(self):
        for survivor in ('events','outcomes','metadata','sequence'):
            with self.subTest(survivor=survivor):
                self.path = Path(self.tmp.name) / f'{survivor}.sqlite'
                self.seed(event())
                with self.connection() as c:
                    for table in ('state','events','outcomes','metadata'):
                        if table != survivor:
                            c.execute(f'DELETE FROM {table}')
                    if survivor != 'sequence':
                        c.execute("DELETE FROM sqlite_sequence WHERE name IN ('events','outcomes')")
                self.assert_recovery('CHECKPOINT_MISSING')

    def test_corrupt_checkpoint_never_becomes_no_change_or_clock_behind(self):
        self.seed(event())
        good = paper_status(self.path, now=T)
        self.assertEqual(good['status'], 'LEDGER_PRESENT')
        for payload in ('null', '{}', '{', '[]'):
            with self.subTest(payload=payload):
                with self.connection() as c:
                    c.execute('UPDATE state SET payload=?', (payload,))
                self.assert_recovery('CHECKPOINT_INVALID')

    def test_partial_journal_or_identity_loss_is_not_valid_portfolio(self):
        for missing, reason in (('events','EVENT_JOURNAL_INCOMPLETE'),
                                ('config_hash','EXPERIMENT_IDENTITY_MISSING')):
            with self.subTest(missing=missing):
                self.path = Path(self.tmp.name) / f'{missing}.sqlite'
                self.seed(event())
                with self.connection() as c:
                    if missing == 'events':
                        c.execute('DELETE FROM events')
                    else:
                        c.execute('DELETE FROM metadata WHERE key=?', (missing,))
                self.assert_recovery(reason)

    def test_normal_missing_new_raw_only_and_closed_ledgers_keep_existing_states(self):
        self.assertEqual(paper_status(self.path, now=T)['status'], 'NOT_CONFIGURED')
        self.assertEqual(tick(self.path, self.cfg, now=T)['status'], 'NOT_CONFIGURED')
        self.assertFalse(self.path.exists())
        self.seed()
        for raw in (False, True):
            if raw:
                ledger = Ledger(self.path)
                ledger.record_raw('synthetic-original', T, 1, {'fixture':'original'})
                ledger.health(T, 'SYNTHETIC_TEST_ONLY', {'provider_calls':0})
                ledger.close()
            before = self.records()
            self.assertEqual(paper_status(self.path, now=T)['status'], 'EMPTY_LEDGER')
            self.assertEqual(tick(self.path, self.cfg, now=T)['status'], 'EMPTY_LEDGER')
            self.assertEqual(self.records(), before)
        self.seed(event(), event(T+1, danger=True))
        before = self.records()
        view = paper_status(self.path, now=T+2)
        self.assertEqual(view['status'], 'LEDGER_PRESENT')
        self.assertEqual(view['positions'], [])
        self.assertEqual(tick(self.path, self.cfg, now=T+2)['status'], 'NO_CHANGE')
        self.assertEqual(self.records(), before)

    def test_actual_dashboard_get_returns_recovery_and_preserves_records(self):
        self.path = Path(self.tmp.name) / 'active-paper.sqlite'
        self.seed(event())
        with self.connection() as c:
            c.execute('DELETE FROM state')
        before = self.records()
        endpoint = handler(SimpleNamespace(db=Path(self.tmp.name)/'jobs.sqlite'), 8080)
        request = object.__new__(endpoint)
        request.path = '/api/paper'
        request.headers = {'Host':'127.0.0.1:8080'}
        request.respond = Mock()
        request.do_GET()
        status, response = request.respond.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(response['status'], 'RECOVERY_REQUIRED')
        self.assertFalse(response['automatic_entry_enabled'])
        self.assertIsNone(response['cash_sol'])
        self.assertEqual(self.records(), before)

    def test_actual_monitor_cli_outputs_recovery_without_fills_or_writes(self):
        self.seed(event())
        with self.connection() as c:
            c.execute('DELETE FROM state')
        before = self.records()
        result = subprocess.run([sys.executable,'-m','desk','paper-monitor','--db',str(self.path),
            '--config',str(ROOT/'config/paper.json')], cwd=ROOT,
            env={'PATH':os.defpath,'PYTHONPATH':str(ROOT)}, capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status['status'], 'RECOVERY_REQUIRED')
        self.assertFalse(status['automatic_entry_enabled'])
        self.assertNotIn('outcomes', status)
        self.assertEqual(self.records(), before)

    def test_missing_schema_is_unavailable_without_automatic_schema_repair(self):
        self.seed(event())
        with self.connection() as c:
            c.execute('DROP TABLE state')
        before = self.path.read_bytes()
        for result in (paper_status(self.path,now=T),tick(self.path,self.cfg,now=T)):
            self.assertEqual(result['status'],'LEDGER_UNAVAILABLE')
            self.assertFalse(result['automatic_entry_enabled'])
        self.assertEqual(self.path.read_bytes(),before)
        with self.connection() as c:
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='state'").fetchone())
