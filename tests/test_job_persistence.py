"""Synthetic local processes only; no acquisition execution or provider I/O."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from desk.dashboard import Jobs
from desk.job_persistence import BIRTH_ACQUISITION_V1, SCREEN, JobPersistence
from desk.model import canonical, digest
from desk.security import base58
from tests.helpers import ROOT

MINT = base58(bytes([7])*32)
OTHER = base58(bytes([8])*32)
THIRD = base58(bytes([9])*32)
CHILD = '''
import sys
from pathlib import Path
from desk.dashboard import Jobs
path, ready, mode = sys.argv[1:]
def scanner(mint):
    Path(ready).write_text(mint)
    sys.stdin.readline()
    return {'mint':mint,'eligible_for_trading':False}
jobs=Jobs(path, scanner=scanner)
if mode == 'work':
    print(jobs.once(), flush=True)
else:
    try:
        mint=sys.stdin.readline().strip()
        uid=jobs.submit_acquisition(mint, str(Path(path).parent/'evidence.sqlite')) if mode == 'acquire' else jobs.submit(mint)
        print(uid, flush=True)
    except ValueError as exc:
        print(str(exc), flush=True)
'''


class JobPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'research.sqlite'
        self.jobs = Jobs(self.path, scanner=lambda m: {'mint': m, 'eligible_for_trading': False})

    def child(self, path=None, mode='work'):
        ready = self.root/f'ready-{time.monotonic_ns()}'
        process = subprocess.Popen([sys.executable, '-c', CHILD, str(path or self.path), str(ready), mode],
            cwd=ROOT, env={'PATH': os.defpath, 'PYTHONPATH': str(ROOT)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        return process, ready

    def wait_ready(self, process, ready):
        deadline = time.monotonic()+10
        while not ready.exists():
            if process.poll() is not None:
                self.fail(f'Fixture worker stopped: {process.communicate(timeout=1)}')
            if time.monotonic() >= deadline:
                self.fail('Fixture worker did not reach scanner')
            time.sleep(.01)

    def scans(self):
        with self.jobs.connect() as c:
            return [tuple(r) for r in c.execute('SELECT * FROM scans ORDER BY rowid')]

    def test_migration_defaults_legacy_to_screen_preserving_completed_bytes(self):
        legacy = self.root/'legacy.sqlite'
        payload = '{ "fixture" : true, "eligible_for_trading": false }'
        with sqlite3.connect(legacy) as c:
            c.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
            c.execute('INSERT INTO scans VALUES(?,?,?,?,?)', ('old', MINT, 1, 'COMPLETE', payload))
        jobs = Jobs(legacy)
        with jobs.connect() as c:
            self.assertEqual(tuple(c.execute('SELECT * FROM scans').fetchone()), ('old', MINT, 1, 'COMPLETE', payload))
        value = jobs.descriptor('old')
        self.assertEqual(value['schema_version'], 1)
        self.assertEqual(value['kind'], SCREEN)
        self.assertEqual(value['admitted_at'], 1)
        self.assertFalse(jobs.once())
        self.assertEqual(jobs.list()[0]['result']['fixture'], True)

    def test_acquisition_admission_is_immutable_and_ordinary_scanner_skips_it(self):
        uid = self.jobs.submit_acquisition(MINT, self.root/'evidence.sqlite')
        descriptor = self.jobs.descriptor(uid)
        self.assertEqual(descriptor['kind'], BIRTH_ACQUISITION_V1)
        self.assertEqual(descriptor['scan_id'], uid)
        self.assertEqual(descriptor['request_ceiling'], 18)
        self.assertEqual(descriptor['research_db'], str(self.path.resolve()))
        self.assertEqual(descriptor['evidence_db'], str((self.root/'evidence.sqlite').resolve()))
        self.assertFalse((self.root/'evidence.sqlite').exists())
        with self.jobs.connect() as c:
            for sql in ("UPDATE scan_jobs SET kind='SCREEN'", "UPDATE scan_jobs SET descriptor='{}'",
                        'DELETE FROM scan_jobs'):
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'Immutable'):
                    c.execute(sql)
        self.assertFalse(self.jobs.once())
        screen = self.jobs.submit(OTHER)
        self.assertTrue(self.jobs.once())
        self.assertEqual({r['id']: r['status'] for r in self.jobs.list()}, {uid:'QUEUED', screen:'COMPLETE'})
        self.assertEqual(Jobs(self.path).descriptor(uid), descriptor)
        self.assertEqual(set(self.jobs.list()[0]), {'id','mint','created','status','result'})

    def test_plain_connection_replace_cannot_rebind_admitted_identity(self):
        uid = self.jobs.submit_acquisition(MINT, self.root/'evidence.sqlite')
        original = self.jobs.descriptor(uid)
        rebound = dict(original, kind=SCREEN, source_version=SCREEN)
        rebound.pop('evidence_db')
        with self.jobs.connect() as c:
            before = tuple(c.execute('SELECT rowid,* FROM scan_jobs WHERE scan_id=?', (uid,)).fetchone())
        columns = 'scan_id,kind,descriptor_version,descriptor,descriptor_hash,generation,claim_token'
        values = (uid, SCREEN, 1, canonical(rebound), digest(rebound), 77, 'replacement-token')
        variants = [
            (f'INSERT OR REPLACE INTO scan_jobs({columns}) VALUES(?,?,?,?,?,?,?)', values),
            (f'REPLACE INTO scan_jobs({columns}) VALUES(?,?,?,?,?,?,?)', values),
            (f'INSERT OR REPLACE INTO scan_jobs({columns}) SELECT ?,?,?,?,?,?,?', values),
            (f'INSERT OR REPLACE INTO scan_jobs(rowid,{columns}) VALUES(?,?,?,?,?,?,?,?)', (before[0],)+values),
            # Plain INSERT and IGNORE must not silently preserve or rebind a
            # duplicate descriptor under a different conflict policy either.
            (f'INSERT INTO scan_jobs({columns}) VALUES(?,?,?,?,?,?,?)', values),
            (f'INSERT OR IGNORE INTO scan_jobs({columns}) VALUES(?,?,?,?,?,?,?)', values),
        ]
        for sql, params in variants:
            with self.subTest(sql=sql):
                # Fresh plain connection, deliberately not helper.connect().
                with sqlite3.connect(self.path) as c:
                    self.assertEqual(c.execute('PRAGMA recursive_triggers').fetchone()[0], 0)
                    with self.assertRaisesRegex(sqlite3.IntegrityError, 'Immutable job descriptor identity'):
                        c.execute(sql, params)
                    self.assertEqual(tuple(c.execute('SELECT rowid,* FROM scan_jobs WHERE scan_id=?', (uid,)).fetchone()), before)
                self.assertEqual(self.jobs.descriptor(uid), original)
                self.assertFalse(self.jobs.once())
        later = self.jobs.submit(OTHER)
        self.assertTrue(self.jobs.once())
        self.assertEqual({r['id']:r['status'] for r in self.jobs.list()}, {uid:'QUEUED', later:'COMPLETE'})
        self.assertEqual(self.jobs.descriptor(uid), original)

    def test_plain_connection_replace_by_rowid_cannot_remove_another_identity(self):
        uid = self.jobs.submit_acquisition(MINT, self.root/'evidence.sqlite')
        other = self.jobs.submit(OTHER)
        with self.jobs.connect() as c:
            before = [tuple(r) for r in c.execute('SELECT rowid,* FROM scan_jobs ORDER BY rowid')]
            descriptor = self.jobs.descriptor(other)
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('PRAGMA recursive_triggers').fetchone()[0], 0)
            # Different scan ID but colliding storage rowid must not delete the
            # admitted acquisition's protected descriptor implicitly.
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'Immutable'):
                c.execute('''REPLACE INTO scan_jobs(rowid,scan_id,kind,descriptor_version,descriptor,descriptor_hash)
                             VALUES(?,?,?,?,?,?)''',
                          (before[0][0], 'fixture-new-id', SCREEN, 1, canonical(descriptor), digest(descriptor)))
            self.assertEqual([tuple(r) for r in c.execute('SELECT rowid,* FROM scan_jobs ORDER BY rowid')], before)
        self.assertEqual(self.jobs.descriptor(uid)['kind'], BIRTH_ACQUISITION_V1)

    def test_non_object_descriptors_block_only_corrupt_jobs_not_later_valid_work(self):
        for value in ([], [1], None, 'fixture-scalar', 0, 1.5, True, False):
            with self.subTest(value=value):
                path = self.root/f'shape-{type(value).__name__}-{canonical(value)}.sqlite'
                seen = []
                jobs = Jobs(path, scanner=lambda mint: seen.append(mint) or {'fixture':True})
                with sqlite3.connect(path) as c:
                    c.execute('INSERT INTO scans VALUES(?,?,?,?,NULL)', ('bad',MINT,int(time.time()),'QUEUED'))
                    c.execute('''INSERT INTO scan_jobs(scan_id,kind,descriptor_version,descriptor,descriptor_hash)
                                 VALUES(?,?,?,?,?)''', ('bad', SCREEN, 1, canonical(value), digest(value)))
                    before = tuple(c.execute('SELECT * FROM scan_jobs WHERE scan_id=?', ('bad',)).fetchone())
                with self.assertRaisesRegex(ValueError, 'JSON object'):
                    jobs.descriptor('bad')
                good = jobs.submit(OTHER)
                # Exercise the actual worker loop: without shape validation its
                # first once() raises AttributeError and never dispatches OTHER.
                jobs.scanner = lambda mint: seen.append(mint) or jobs.stopping.set() or {'fixture':True}
                jobs.run()
                self.assertEqual(seen, [OTHER])
                self.assertEqual({r['id']:r['status'] for r in jobs.list()}, {'bad':'QUEUED', good:'COMPLETE'})
                self.assertFalse(jobs.once())
                with jobs.connect() as c:
                    self.assertEqual(tuple(c.execute('SELECT * FROM scan_jobs WHERE scan_id=?', ('bad',)).fetchone()), before)
                with self.assertRaisesRegex(ValueError, 'pending'):
                    jobs.submit(MINT)
                jobs.submit(THIRD)
                jobs.submit(base58(bytes([10])*32))
                with self.assertRaisesRegex(ValueError, 'Queue full'):
                    jobs.submit(base58(bytes([11])*32))

    def test_rowid_update_aliases_reject_collision_and_identity_moves_across_reopen(self):
        uid = self.jobs.submit_acquisition(MINT, self.root/'evidence.sqlite')
        other = self.jobs.submit(OTHER)
        with self.jobs.connect() as c:
            # Mutable worker checkpoints remain permitted by the row identity guard.
            c.execute('UPDATE scan_jobs SET generation=3,claim_token=? WHERE scan_id=?', ('fixture-token', other))
            before = [tuple(r) for r in c.execute('SELECT rowid,* FROM scan_jobs ORDER BY rowid')]
        scans = self.scans()
        for alias in ('rowid', '_rowid_', 'oid'):
            for operation in ('UPDATE', 'UPDATE OR REPLACE', 'UPDATE OR IGNORE'):
                for target in (before[0][0], max(r[0] for r in before)+100):
                    with self.subTest(alias=alias, operation=operation, target=target):
                        with sqlite3.connect(self.path) as c:
                            self.assertEqual(c.execute('PRAGMA recursive_triggers').fetchone()[0], 0)
                            with self.assertRaisesRegex(sqlite3.IntegrityError, 'Immutable job descriptor row identity'):
                                c.execute(f'{operation} scan_jobs SET {alias}=? WHERE scan_id=?', (target, other))
                            self.assertEqual([tuple(r) for r in c.execute('SELECT rowid,* FROM scan_jobs ORDER BY rowid')], before)
                        reopened = Jobs(self.path)
                        self.assertEqual(reopened.descriptor(uid)['kind'], BIRTH_ACQUISITION_V1)
                        with reopened.connect() as c:
                            self.assertEqual([tuple(r) for r in c.execute('SELECT rowid,* FROM scan_jobs ORDER BY rowid')], before)
                        self.assertEqual(self.scans(), scans)
        seen = []
        reopened.scanner = lambda mint: seen.append(mint) or {'fixture':True}
        self.assertTrue(reopened.once())
        self.assertEqual(seen, [OTHER])
        self.assertFalse(reopened.once())
        self.assertEqual(reopened.descriptor(uid)['kind'], BIRTH_ACQUISITION_V1)
        with reopened.connect() as c:
            self.assertEqual(c.execute('SELECT generation,claim_token FROM scan_jobs WHERE scan_id=?', (other,)).fetchone()[:], (4,None))

    def test_one_time_migration_does_not_infer_screen_for_later_missing_metadata(self):
        with self.jobs.connect() as c:
            c.execute('INSERT INTO scans VALUES(?,?,?,?,NULL)', ('missing-new-acquisition',MINT,int(time.time()),'QUEUED'))
            marker = tuple(c.execute('SELECT * FROM scan_job_migrations').fetchone())
        before = self.scans()
        for _ in range(2):
            reopened = Jobs(self.path)
            with self.assertRaisesRegex(ValueError, 'missing'):
                reopened.descriptor('missing-new-acquisition')
            self.assertFalse(reopened.once())
            self.assertEqual(self.scans(), before)
            with reopened.connect() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM scan_jobs').fetchone()[0], 0)
                self.assertEqual(tuple(c.execute('SELECT * FROM scan_job_migrations').fetchone()), marker)
        with self.assertRaisesRegex(ValueError, 'pending'):
            reopened.submit(MINT)
        reopened.submit(OTHER)
        self.assertTrue(reopened.once())
        self.assertEqual({r['id']:r['status'] for r in reopened.list()}['missing-new-acquisition'], 'QUEUED')

    def test_upgrade_without_marker_does_not_backfill_existing_dispatch_database(self):
        path = self.root/'prior-dispatch.sqlite'
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
            c.execute('''CREATE TABLE scan_jobs(scan_id TEXT PRIMARY KEY,kind TEXT NOT NULL,
                descriptor_version INTEGER NOT NULL,descriptor TEXT NOT NULL,descriptor_hash TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0,claim_token TEXT)''')
            c.execute('INSERT INTO scans VALUES(?,?,?,?,NULL)', ('lost-acquisition',MINT,int(time.time()),'QUEUED'))
        for _ in range(2):
            jobs = Jobs(path, scanner=lambda mint: self.fail('Missing acquisition was reclassified'))
            with self.assertRaisesRegex(ValueError, 'missing'):
                jobs.descriptor('lost-acquisition')
            self.assertFalse(jobs.once())
            with jobs.connect() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM scan_jobs').fetchone()[0], 0)
                self.assertEqual(tuple(c.execute('SELECT * FROM scan_job_migrations').fetchone()), ('legacy_screen_descriptors',1))
        jobs.scanner = lambda mint: {'fixture':True}
        jobs.submit(OTHER)
        self.assertTrue(jobs.once())
        self.assertEqual({r['id']:r['status'] for r in jobs.list()}['lost-acquisition'], 'QUEUED')

    def test_migration_marker_plain_sql_changes_are_rejected(self):
        uid = self.jobs.submit_acquisition(MINT, self.root/'evidence.sqlite')
        original = self.jobs.descriptor(uid)
        for sql in ("UPDATE scan_job_migrations SET version=2",
                    "DELETE FROM scan_job_migrations",
                    "INSERT OR REPLACE INTO scan_job_migrations VALUES('legacy_screen_descriptors',2)"):
            with sqlite3.connect(self.path) as c:
                self.assertEqual(c.execute('PRAGMA recursive_triggers').fetchone()[0], 0)
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'Immutable job migration marker'):
                    c.execute(sql)
                self.assertEqual(c.execute('SELECT * FROM scan_job_migrations').fetchall(), [('legacy_screen_descriptors',1)])
        reopened = Jobs(self.path)
        self.assertEqual(reopened.descriptor(uid), original)
        self.assertFalse(reopened.once())

    def test_legacy_migration_and_marker_roll_back_together(self):
        path = self.root/'failed-migration.sqlite'
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
            c.execute('INSERT INTO scans VALUES(?,?,?,?,?)', ('legacy',MINT,1,'COMPLETE','{ "original" : true }'))
            before = c.execute('SELECT * FROM scans').fetchall()
        with patch.object(JobPersistence, '_insert_descriptor', side_effect=RuntimeError('fixture crash')):
            with self.assertRaises(RuntimeError):
                Jobs(path)
        with sqlite3.connect(path) as c:
            self.assertEqual(c.execute('SELECT * FROM scans').fetchall(), before)
            self.assertEqual(c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('scans',)])
        jobs = Jobs(path)
        self.assertEqual(jobs.descriptor('legacy')['kind'], SCREEN)
        with jobs.connect() as c:
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM scans')], before)

    def test_corrupt_or_exhausted_generation_blocks_only_bad_job(self):
        for generation in ('broken', b'fixture-blob', -1, 1.5, (1 << 63)-1):
            with self.subTest(generation=generation):
                path = self.root/f'generation-{type(generation).__name__}-{len(list(self.root.iterdir()))}.sqlite'
                jobs = Jobs(path)
                bad = jobs.submit(MINT)
                good = jobs.submit(OTHER)
                with sqlite3.connect(path) as c:
                    c.execute('UPDATE scan_jobs SET generation=? WHERE scan_id=?', (generation,bad))
                    before = c.execute('SELECT rowid,* FROM scan_jobs WHERE scan_id=?', (bad,)).fetchone()
                seen = []
                jobs.scanner = lambda mint: seen.append(mint) or jobs.stopping.set() or {'fixture':True}
                jobs.run()
                self.assertEqual(seen, [OTHER])
                self.assertEqual({r['id']:r['status'] for r in jobs.list()}, {bad:'QUEUED',good:'COMPLETE'})
                self.assertFalse(jobs.once())
                with sqlite3.connect(path) as c:
                    self.assertEqual(c.execute('SELECT rowid,* FROM scan_jobs WHERE scan_id=?', (bad,)).fetchone(), before)
                with self.assertRaisesRegex(ValueError, 'pending'):
                    jobs.submit(MINT)
                jobs.submit(THIRD)
                jobs.submit(base58(bytes([10])*32))
                with self.assertRaisesRegex(ValueError, 'Queue full'):
                    jobs.submit(base58(bytes([11])*32))

    def test_descriptor_insert_failure_rolls_back_admission(self):
        with patch.object(JobPersistence, '_insert_descriptor', side_effect=RuntimeError('fixture crash')):
            with self.assertRaises(RuntimeError):
                self.jobs.submit(MINT)
        self.assertEqual(self.scans(), [])
        with self.jobs.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM scan_jobs').fetchone()[0], 0)
        self.jobs.submit(MINT)

    def test_shared_duplicate_and_queue_limits_include_acquisitions(self):
        self.jobs.submit_acquisition(MINT, self.root/'evidence.sqlite')
        with self.assertRaisesRegex(ValueError, 'pending'):
            self.jobs.submit(MINT)
        self.jobs.submit(OTHER)
        with self.assertRaisesRegex(ValueError, 'pending'):
            self.jobs.submit_acquisition(OTHER, self.root/'evidence.sqlite')
        self.jobs.submit_acquisition(THIRD, self.root/'evidence.sqlite')
        with self.assertRaisesRegex(ValueError, 'Queue full'):
            self.jobs.submit(base58(bytes([10])*32))

    def test_day_budget_keeps_failed_and_interrupted_both_kinds(self):
        now = 200000
        with patch('desk.job_persistence.time.time', return_value=now):
            for i in range(10):
                mint = base58(bytes([i+1])*32)
                uid = (self.jobs.submit(mint) if i % 2 else
                       self.jobs.submit_acquisition(mint, self.root/'evidence.sqlite'))
                with self.jobs.connect() as c:
                    c.execute('UPDATE scans SET status=? WHERE id=?', ('INTERRUPTED' if i % 2 else 'FAILED', uid))
            for acquisition in (False, True):
                with self.assertRaisesRegex(ValueError, 'Daily budget'):
                    if acquisition:
                        self.jobs.submit_acquisition(MINT, self.root/'evidence.sqlite')
                    else:
                        self.jobs.submit(MINT)
        with patch('desk.job_persistence.time.time', return_value=now+86400):
            self.jobs.submit(MINT)  # Preserve the original exclusive rolling cutoff.

    def test_unknown_kind_and_version_block_dispatch_and_consume_capacity(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            self.jobs.persistence.admit(MINT, kind='UNKNOWN')
        with self.jobs.connect() as c:
            for uid, kind, version, mint in [('unknown', 'UNKNOWN', 1, MINT), ('future', SCREEN, 2, OTHER)]:
                c.execute('INSERT INTO scans VALUES(?,?,?,?,NULL)', (uid,mint,int(time.time()),'QUEUED'))
                descriptor = {'schema_version':version, 'kind':kind}
                c.execute('INSERT INTO scan_jobs(scan_id,kind,descriptor_version,descriptor,descriptor_hash) VALUES(?,?,?,?,?)',
                          (uid,kind,version,canonical(descriptor),digest(descriptor)))
        before = self.scans()
        self.assertFalse(self.jobs.once())
        self.assertEqual(before, self.scans())
        with self.assertRaises(ValueError):
            self.jobs.descriptor('unknown')
        with self.assertRaisesRegex(ValueError, 'pending'):
            self.jobs.submit(MINT)
        self.jobs.submit(THIRD)
        with self.assertRaisesRegex(ValueError, 'Queue full'):
            self.jobs.submit(base58(bytes([10])*32))

    def test_corrupt_screen_descriptor_does_not_reach_scanner(self):
        with self.jobs.connect() as c:
            c.execute('INSERT INTO scans VALUES(?,?,?,?,NULL)', ('bad',MINT,int(time.time()),'QUEUED'))
            c.execute('INSERT INTO scan_jobs(scan_id,kind,descriptor_version,descriptor,descriptor_hash) VALUES(?,?,?,?,?)',
                      ('bad',SCREEN,1,'{}','0'*64))
        self.jobs.scanner = lambda m: self.fail('Corrupt descriptor reached scanner')
        self.assertFalse(self.jobs.once())
        self.assertEqual(self.jobs.list()[0]['status'], 'QUEUED')

    def test_live_process_constructor_duplicate_and_competing_claim_are_safe(self):
        uid = self.jobs.submit(MINT)
        process, ready = self.child()
        self.wait_ready(process, ready)
        other = Jobs(self.path, scanner=lambda m: self.fail('Concurrent scanner'))
        self.assertEqual(other.list()[0]['status'], 'RUNNING')
        self.assertFalse(other.once())
        with self.assertRaisesRegex(ValueError, 'pending'):
            other.submit_acquisition(MINT, self.root/'evidence.sqlite')
        self.assertEqual(other.descriptor(uid)['kind'], SCREEN)
        stdout, stderr = process.communicate('\n', timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), 'True')
        self.assertEqual(Jobs(self.path).list()[0]['status'], 'COMPLETE')

    def test_killed_process_is_interrupted_once_without_replaying_scan(self):
        uid = self.jobs.submit(MINT)
        process, ready = self.child()
        self.wait_ready(process, ready)
        before = self.jobs.descriptor(uid)
        process.kill()
        process.communicate(timeout=10)
        restarted = Jobs(self.path, scanner=lambda m: self.fail('Interrupted job executed'))
        self.assertEqual(restarted.list()[0]['status'], 'INTERRUPTED')
        self.assertEqual(restarted.descriptor(uid), before)
        self.assertFalse(restarted.once())
        records = self.scans()
        Jobs(self.path)
        self.assertEqual(records, self.scans())

    def test_symlink_and_relative_aliases_share_worker_lock_and_sqlite_identity(self):
        alias = self.root/'alias.sqlite'
        alias.symlink_to(self.path)
        relative = self.root/'sub'/ '..'/'research.sqlite'
        (self.root/'sub').mkdir()
        self.jobs.submit(MINT)
        process, ready = self.child(alias)
        self.wait_ready(process, ready)
        for path in (alias, relative, self.path):
            jobs = Jobs(path)
            self.assertEqual(jobs.db, str(self.path.resolve()))
            self.assertEqual(jobs.list()[0]['status'], 'RUNNING')
            self.assertFalse(jobs.once())
        self.assertFalse(Path(str(alias)+'.jobs-worker.lock').exists())
        process.communicate('\n', timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(self.jobs.list()[0]['status'], 'COMPLETE')

    def test_hardlinked_database_and_lock_and_evidence_are_rejected(self):
        self.jobs.submit(MINT)
        original = self.scans()
        alias = self.root/'hard.sqlite'
        os.link(self.path, alias)
        for path in (self.path, alias):
            with self.assertRaisesRegex(ValueError, 'one hard link'):
                Jobs(path)
        with self.assertRaises(ValueError):
            self.jobs.once()
        alias.unlink()
        self.assertEqual(original, self.scans())
        evidence = self.root/'evidence.sqlite'
        evidence.write_bytes(b'fixture')
        os.link(evidence, self.root/'hard-evidence.sqlite')
        with self.assertRaises(ValueError):
            self.jobs.submit_acquisition(OTHER, evidence)
        lock = Path(str(self.path)+'.jobs-worker.lock')
        os.link(lock, self.root/'hard-lock')
        with self.assertRaises(ValueError):
            self.jobs.once()

    def test_stale_token_generation_and_terminal_publication_cannot_overwrite(self):
        uid = self.jobs.submit(MINT)
        with self.jobs.persistence.worker() as worker:
            claim = worker.claim_screen()
            self.assertEqual(claim.scan_id, uid)
            for bad in (replace(claim, token='old-worker'), replace(claim, generation=claim.generation-1)):
                with self.assertRaises(ValueError):
                    worker.publish(bad, 'COMPLETE', {'forged':True})
            worker.publish(claim, 'COMPLETE', {'original':True})
            before = self.scans()
            with self.assertRaisesRegex(ValueError, 'Stale'):
                worker.publish(claim, 'FAILED', {'forged':True})
            self.assertEqual(before, self.scans())
        with self.assertRaisesRegex(ValueError, 'no longer active'):
            worker.publish(claim, 'FAILED', {'late':True})
        with self.jobs.persistence.worker() as next_worker:
            with self.assertRaisesRegex(ValueError, 'Invalid'):
                next_worker.publish(claim, 'FAILED', {'late':True})
        self.assertEqual(before, self.scans())

    def test_fork_cleanup_cannot_release_parent_worker_lock(self):
        self.jobs.submit(MINT)
        context = self.jobs.persistence.worker()
        worker = context.__enter__()
        try:
            claim = worker.claim_screen()
            pid = os.fork()
            if pid == 0:
                try:
                    try:
                        worker.publish(claim, 'COMPLETE', {'child':True})
                    except ValueError:
                        pass
                    else:
                        os._exit(2)
                    context.__exit__(None, None, None)
                    os._exit(0)
                except BaseException:
                    os._exit(3)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            rival = Jobs(self.path)
            self.assertEqual(rival.list()[0]['status'], 'RUNNING')
            self.assertFalse(rival.once())
            worker.publish(claim, 'COMPLETE', {'parent':True})
        finally:
            context.__exit__(None, None, None)
        self.assertEqual(self.jobs.list()[0]['result'], {'parent':True})

    def test_interrupted_claim_generation_fences_old_publisher(self):
        uid = self.jobs.submit(MINT)
        with self.jobs.persistence.worker() as old:
            old_claim = old.claim_screen()
        with self.jobs.persistence.worker() as new:
            self.assertEqual(self.jobs.list()[0]['status'], 'INTERRUPTED')
            with self.assertRaises(ValueError):
                new.publish(old_claim, 'COMPLETE', {'late':True})
            # Fixture-only requeue to exercise the same scan's generation fence.
            with self.jobs.connect() as c:
                c.execute("UPDATE scans SET status='QUEUED' WHERE id=?", (uid,))
            claim = new.claim_screen()
            self.assertGreater(claim.generation, old_claim.generation)
            with self.assertRaisesRegex(ValueError, 'Stale'):
                new.publish(replace(old_claim, token=claim.token), 'COMPLETE', {'late':True})
            new.publish(claim, 'COMPLETE', {'recovered':True})
        self.assertEqual(self.jobs.list()[0]['result'], {'recovered':True})

    def test_concurrent_screen_and_acquisition_share_last_daily_admission(self):
        for i in range(9):
            uid = self.jobs.submit(base58(bytes([i+20])*32))
            with self.jobs.connect() as c:
                c.execute("UPDATE scans SET status='FAILED' WHERE id=?", (uid,))
        first, _ = self.child(mode='submit')
        second, _ = self.child(mode='acquire')
        first.stdin.write(MINT+'\n'); first.stdin.flush()
        second.stdin.write(OTHER+'\n'); second.stdin.flush()
        results = [first.communicate(timeout=10), second.communicate(timeout=10)]
        self.assertEqual(first.returncode, 0, results[0][1])
        self.assertEqual(second.returncode, 0, results[1][1])
        self.assertEqual(sum('Daily budget' in stdout for stdout, _ in results), 1)
        self.assertEqual(len(self.jobs.list()), 10)

    def test_concurrent_screen_and_acquisition_share_last_queue_slot(self):
        self.jobs.submit(base58(bytes([20])*32))
        self.jobs.submit(base58(bytes([21])*32))
        first, _ = self.child(mode='submit')
        second, _ = self.child(mode='acquire')
        first.stdin.write(MINT+'\n'); first.stdin.flush()
        second.stdin.write(OTHER+'\n'); second.stdin.flush()
        results = [first.communicate(timeout=10), second.communicate(timeout=10)]
        self.assertEqual(first.returncode, 0, results[0][1])
        self.assertEqual(second.returncode, 0, results[1][1])
        self.assertEqual(sum('Queue full' in stdout for stdout, _ in results), 1)
        self.assertEqual(len(self.jobs.list()), 3)

    def test_concurrent_process_admission_shares_pending_identity(self):
        first, _ = self.child(mode='submit')
        second, _ = self.child(mode='submit')
        first.stdin.write(MINT+'\n'); first.stdin.flush()
        second.stdin.write(MINT+'\n'); second.stdin.flush()
        results = [first.communicate(timeout=10), second.communicate(timeout=10)]
        self.assertEqual(first.returncode, 0, results[0][1])
        self.assertEqual(second.returncode, 0, results[1][1])
        self.assertEqual(sum('pending' in stdout for stdout, _ in results), 1)
        self.assertEqual(len(self.jobs.list()), 1)


if __name__ == '__main__':
    unittest.main()
