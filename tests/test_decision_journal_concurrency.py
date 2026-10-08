"""Synthetic fresh SQLite journals, real independent processes, no providers."""
import json
import multiprocessing
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from desk import decision_runner
from desk.entry_evidence import POLICY
from desk.model import digest


def consume_process(source, journal, barrier, output):
    """Align actual WAL switches after every process has opened the same DB."""
    connect = sqlite3.connect

    class AlignedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == 'PRAGMA journal_mode=WAL' and not getattr(self, 'aligned', False):
                self.aligned = True
                barrier.wait(timeout=10)
            return super().execute(sql, parameters)

    def aligned_connect(*args, **kwargs):
        kwargs['factory'] = AlignedConnection
        return connect(*args, **kwargs)

    try:
        with patch.object(sqlite3, 'connect', aligned_connect):
            result = decision_runner.consume(source, journal, now=100)
        output.put(('ok', result))
    except BaseException as error:
        output.put(('error', type(error).__name__, str(error)))


def hold_reader(journal, ready, release):
    """A rollback-journal reader prevents conversion to WAL until released."""
    with sqlite3.connect(journal, isolation_level=None) as c:
        c.execute('BEGIN')
        c.execute('SELECT * FROM sentinel').fetchall()
        ready.set()
        if not release.wait(10):
            raise AssertionError('Reader fixture timed out')
        c.execute('ROLLBACK')


class DecisionJournalConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'research.sqlite'
        self.journal = self.root / 'decisions.sqlite'
        self.ctx = multiprocessing.get_context('spawn')
        report = {'mint': 'synthetic-mint', 'observed_at': 100, 'findings': [], 'unknowns': [],
                  'eligible_for_trading': True}
        with sqlite3.connect(self.source) as c:
            c.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
            c.execute('INSERT INTO scans VALUES(?,?,?,?,?)', ('scan', 'synthetic-mint', 99, 'COMPLETE', json.dumps(report)))
        self.original_source = self.source.read_bytes()

    def processes(self, journal, count=4):
        barrier = self.ctx.Barrier(count)
        output = self.ctx.Queue()
        workers = [self.ctx.Process(target=consume_process,
                    args=(str(self.source), str(journal), barrier, output)) for _ in range(count)]
        try:
            for worker in workers: worker.start()
            results = [output.get(timeout=15) for _ in workers]
            for worker in workers:
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive(), 'Consumer did not stop within fixture deadline')
                self.assertEqual(worker.exitcode, 0)
            self.assertTrue(all(r[0] == 'ok' for r in results), results)
            return [r[1] for r in results]
        finally:
            for worker in workers:
                if worker.is_alive(): worker.terminate()
                worker.join(timeout=5)
            output.close()
            output.join_thread()

    def test_four_processes_initialize_fresh_journals_and_commit_once(self):
        for iteration in range(3):
            with self.subTest(iteration=iteration):
                journal = self.root / f'fresh-{iteration}.sqlite'
                self.assertFalse(journal.exists())
                results = self.processes(journal)
                self.assertEqual(sum(r['consumed'] for r in results), 1)
                for result in results:
                    self.assertFalse(result['automatic_entry_enabled'])
                    for d in result['decisions']:
                        self.assertEqual(d['decision'], 'REJECT')
                        self.assertFalse(d['eligible_for_trading'])
                        self.assertIn('CURRENT_HOLDER_BUNDLE_EXPOSURE_UNVERIFIED', d['reasons'])
                with sqlite3.connect(journal) as c:
                    self.assertEqual(c.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                    self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertEqual(c.execute('SELECT count(*) FROM decisions').fetchone()[0], 1)
                    self.assertEqual(c.execute('SELECT count(*) FROM decision_evaluations').fetchone()[0], 1)
                    hash_, payload = c.execute('SELECT source_hash,source_payload FROM decisions').fetchone()
                    self.assertEqual(hash_, digest(json.loads(payload)))
                self.assertEqual(self.source.read_bytes(), self.original_source)

    def test_concurrent_existing_journal_preserves_original_and_prior_revision(self):
        decision_runner.consume(self.source, self.journal, now=100)
        with sqlite3.connect(self.journal) as c:
            c.execute("UPDATE decisions SET decision='original historical rejection'")
            c.execute("UPDATE decision_evaluations SET policy='previous-policy'")
            before = c.execute('SELECT * FROM decisions').fetchall()
            prior = c.execute('SELECT * FROM decision_evaluations').fetchall()
        results = self.processes(self.journal)
        self.assertEqual(sum(r['consumed'] for r in results), 1)
        with sqlite3.connect(self.journal) as c:
            self.assertEqual(c.execute('SELECT * FROM decisions').fetchall(), before)
            self.assertEqual(c.execute("SELECT * FROM decision_evaluations WHERE policy='previous-policy'").fetchall(), prior)
            self.assertEqual(c.execute('SELECT count(*) FROM decision_evaluations WHERE policy=?', (POLICY,)).fetchone()[0], 1)
        self.assertEqual(sum(r['consumed'] for r in self.processes(self.journal)), 0)
        self.assertEqual(self.source.read_bytes(), self.original_source)

    def reader(self):
        with sqlite3.connect(self.journal) as c:
            c.execute('CREATE TABLE sentinel(value TEXT)')
            c.execute("INSERT INTO sentinel VALUES('preserved original')")
        ready, release = self.ctx.Event(), self.ctx.Event()
        worker = self.ctx.Process(target=hold_reader, args=(str(self.journal), ready, release))
        worker.start()
        self.assertTrue(ready.wait(10), 'Reader did not acquire its transaction')
        def cleanup():
            release.set()
            worker.join(timeout=10)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        self.addCleanup(cleanup)
        return worker, release

    def test_real_reader_contention_expires_and_writes_no_decisions_then_recovers(self):
        worker, release = self.reader()
        with patch.object(decision_runner, '_JOURNAL_INIT_TIMEOUT', 0.2):
            start = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError) as error:
                decision_runner.consume(self.source, self.journal, now=100)
            elapsed = time.monotonic() - start
        self.assertEqual(error.exception.sqlite_errorcode & 0xff, sqlite3.SQLITE_BUSY)
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertLess(elapsed, 1.5, 'Original 15-second busy timeout leaked into initialization')
        with sqlite3.connect(self.journal) as c:
            self.assertEqual(c.execute('SELECT value FROM sentinel').fetchone()[0], 'preserved original')
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('decisions','decision_evaluations')").fetchone()[0], 0)
        self.assertEqual(self.source.read_bytes(), self.original_source)
        release.set(); worker.join(timeout=10)
        self.assertEqual(worker.exitcode, 0)
        self.assertEqual(decision_runner.consume(self.source, self.journal, now=100)['consumed'], 1)
        self.assertEqual(decision_runner.consume(self.source, self.journal, now=100)['consumed'], 0)

    def test_read_only_source_attachment_and_full_sync_are_preserved(self):
        connect = sqlite3.connect
        attachments = []
        full_sync = []
        class CheckedConnection(sqlite3.Connection):
            def execute(connection, sql, parameters=()):
                result = super().execute(sql, parameters)
                if sql.startswith('ATTACH DATABASE'):
                    attachments.append(parameters[0])
                    self.assertTrue(parameters[0].endswith('?mode=ro'))
                    with self.assertRaises(sqlite3.OperationalError):
                        super(CheckedConnection, connection).execute("UPDATE research.scans SET status='FAILED'")
                if sql == 'BEGIN IMMEDIATE':
                    full_sync.append(super(CheckedConnection, connection).execute('PRAGMA synchronous').fetchone()[0])
                return result
        def checked_connect(*args, **kwargs):
            kwargs['factory'] = CheckedConnection
            return connect(*args, **kwargs)
        with patch.object(sqlite3, 'connect', checked_connect):
            self.assertEqual(decision_runner.consume(self.source, self.journal, now=100)['consumed'], 1)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(full_sync, [2, 2])
        self.assertEqual(self.source.read_bytes(), self.original_source)

    def test_nonbusy_sqlite_error_is_not_retried_and_restores_timeout(self):
        connect = sqlite3.connect
        calls = []
        class DeniedConnection(sqlite3.Connection):
            def execute(connection, sql, parameters=()):
                if sql == 'PRAGMA journal_mode=WAL':
                    calls.append(sql)
                    raise sqlite3.OperationalError('synthetic disk I/O error')
                return super().execute(sql, parameters)
        with connect(self.journal, factory=DeniedConnection, isolation_level=None) as c:
            with patch.object(decision_runner.time, 'sleep') as sleep:
                with self.assertRaisesRegex(sqlite3.OperationalError, 'disk I/O'):
                    decision_runner._initialize_journal(c)
                sleep.assert_not_called()
            self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0], 15000)
        self.assertEqual(len(calls), 1)

    def test_failed_batch_remains_atomic_and_original_rows_survive(self):
        decision_runner.consume(self.source, self.journal, now=100)
        with sqlite3.connect(self.journal) as c:
            original = c.execute('SELECT * FROM decisions').fetchall()
        with sqlite3.connect(self.source) as c:
            c.execute('INSERT INTO scans VALUES(?,?,?,?,?)', ('valid', 'synthetic-mint', 99, 'COMPLETE',
                       json.dumps({'mint': 'synthetic-mint', 'observed_at': 100})))
            c.execute('INSERT INTO scans VALUES(?,?,?,?,?)', ('malformed', 'synthetic-mint', 99, 'COMPLETE',
                       json.dumps({'findings': 'forged-list'})))
        with self.assertRaises(ValueError):
            decision_runner.consume(self.source, self.journal, now=100)
        with sqlite3.connect(self.journal) as c:
            self.assertEqual(c.execute('SELECT * FROM decisions').fetchall(), original)
            self.assertEqual(c.execute('SELECT count(*) FROM decision_evaluations').fetchone()[0], 1)

    def test_initialization_refuses_nonwal_result(self):
        class NonWalConnection(sqlite3.Connection):
            def execute(connection, sql, parameters=()):
                if sql == 'PRAGMA journal_mode=WAL':
                    return super().execute('PRAGMA journal_mode=DELETE')
                return super().execute(sql, parameters)
        with sqlite3.connect(self.journal, factory=NonWalConnection, isolation_level=None) as c:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'WAL unavailable'):
                decision_runner._initialize_journal(c)
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 0)

    def test_real_reader_release_allows_same_consumer_to_retry_successfully(self):
        worker, release = self.reader()
        busy = threading.Event()
        attempts = []
        connect = sqlite3.connect
        class ObservedConnection(sqlite3.Connection):
            def execute(connection, sql, parameters=()):
                if sql == 'PRAGMA journal_mode=WAL':
                    attempts.append(sql)
                    try:
                        return super().execute(sql, parameters)
                    except sqlite3.OperationalError as error:
                        if error.sqlite_errorcode & 0xff == sqlite3.SQLITE_BUSY:
                            busy.set()
                        raise
                return super().execute(sql, parameters)
        def observed_connect(*args, **kwargs):
            kwargs['factory'] = ObservedConnection
            return connect(*args, **kwargs)
        with patch.object(sqlite3, 'connect', observed_connect), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(decision_runner.consume, self.source, self.journal, now=100)
            try:
                self.assertTrue(busy.wait(5), 'Fixture did not exercise a real WAL BUSY')
            finally:
                release.set()
            self.assertEqual(future.result(timeout=6)['consumed'], 1)
        worker.join(timeout=10)
        self.assertEqual(worker.exitcode, 0)
        self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual(self.source.read_bytes(), self.original_source)

    def test_failed_second_schema_write_rolls_back_first_schema_write(self):
        class FailedSchemaConnection(sqlite3.Connection):
            def execute(connection, sql, parameters=()):
                if sql.startswith('CREATE TABLE IF NOT EXISTS decision_evaluations'):
                    raise sqlite3.IntegrityError('synthetic schema write failure')
                return super().execute(sql, parameters)
        with sqlite3.connect(self.journal, factory=FailedSchemaConnection, isolation_level=None) as c:
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'schema write failure'):
                decision_runner._initialize_journal(c)
            self.assertFalse(c.in_transaction)
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 0)
            self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0], 15000)
