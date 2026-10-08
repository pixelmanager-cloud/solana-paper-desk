"""One bounded research queue with immutable dispatch and fenced local workers.

Linux single-host contract: stable canonical database/lock paths, no hard links,
renames or replacement while in use. This module performs no provider I/O.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import sqlite3
from stat import S_ISREG
import time
import uuid

from .model import canonical, digest
from .programs import address

SCREEN = 'SCREEN'
BIRTH_ACQUISITION_V1 = 'BIRTH_ACQUISITION_V1'
DESCRIPTOR_VERSION = 1


def canonical_job_path(path):
    resolved = Path(path).resolve()
    try:
        info = resolved.stat()
    except FileNotFoundError:
        return resolved
    if not S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Job database must be a regular file with one hard link')
    return resolved


@dataclass(frozen=True)
class Claim:
    scan_id: str
    mint: str
    token: str
    generation: int


class JobPersistence:
    def __init__(self, path):
        self.path = canonical_job_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
            c.execute('''CREATE TABLE IF NOT EXISTS scan_jobs(
                scan_id TEXT PRIMARY KEY REFERENCES scans(id), kind TEXT NOT NULL,
                descriptor_version INTEGER NOT NULL, descriptor TEXT NOT NULL,
                descriptor_hash TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
                claim_token TEXT)''')
            # Separate table preserves the original scans schema and completed bytes.
            for row in c.execute('SELECT id,mint,created FROM scans WHERE id NOT IN (SELECT scan_id FROM scan_jobs)').fetchall():
                descriptor = self._descriptor(row['id'], row['mint'], row['created'], SCREEN)
                self._insert_descriptor(c, descriptor)
            c.execute('''CREATE TRIGGER IF NOT EXISTS immutable_scan_job_descriptor
                BEFORE UPDATE OF scan_id,kind,descriptor_version,descriptor,descriptor_hash ON scan_jobs
                BEGIN SELECT RAISE(ABORT,'Immutable job descriptor'); END''')
            c.execute('''CREATE TRIGGER IF NOT EXISTS preserve_scan_job_descriptor
                BEFORE DELETE ON scan_jobs
                BEGIN SELECT RAISE(ABORT,'Immutable job descriptor'); END''')

    def connect(self):
        if canonical_job_path(self.path) != self.path:
            raise ValueError('Job database identity changed')
        c = sqlite3.connect(str(self.path), timeout=15)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        return c

    def _descriptor(self, uid, mint, created, kind, evidence_db=None):
        value = {'schema_version': DESCRIPTOR_VERSION, 'kind': kind, 'scan_id': uid,
                 'mint': mint, 'admitted_at': created, 'research_db': str(self.path),
                 'source_version': kind, 'request_ceiling': 18}
        if evidence_db is not None:
            value['evidence_db'] = str(canonical_job_path(evidence_db))
        return value

    @staticmethod
    def _insert_descriptor(c, descriptor):
        c.execute('''INSERT INTO scan_jobs(scan_id,kind,descriptor_version,descriptor,descriptor_hash)
                     VALUES(?,?,?,?,?)''', (descriptor['scan_id'], descriptor['kind'],
                     DESCRIPTOR_VERSION, canonical(descriptor), digest(descriptor)))

    def admit(self, mint, *, kind=SCREEN, evidence_db=None):
        address(mint)
        if kind not in (SCREEN, BIRTH_ACQUISITION_V1):
            raise ValueError('Unsupported job kind')
        if (kind == BIRTH_ACQUISITION_V1) != (evidence_db is not None):
            raise ValueError('Acquisition admission requires an evidence database')
        uid, created = uuid.uuid4().hex, int(time.time())
        descriptor = self._descriptor(uid, mint, created, kind, evidence_db)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute("SELECT count(*) FROM scans WHERE status IN ('QUEUED','RUNNING')").fetchone()[0] >= 3:
                raise ValueError('Queue full. Wait for the current scans to finish.')
            if c.execute('SELECT count(*) FROM scans WHERE created>?', (created-86400,)).fetchone()[0] >= 10:
                raise ValueError('Daily budget reached: 10 scans per rolling 24 hours.')
            if c.execute("SELECT 1 FROM scans WHERE mint=? AND status IN ('QUEUED','RUNNING')", (mint,)).fetchone():
                raise ValueError('This token already has a pending scan.')
            c.execute('INSERT INTO scans VALUES(?,?,?,?,?)', (uid, mint, created, 'QUEUED', None))
            self._insert_descriptor(c, descriptor)
        return uid

    def descriptor(self, scan_id):
        with self.connect() as c:
            row = c.execute('''SELECT j.*,s.mint,s.created FROM scan_jobs j
                               JOIN scans s ON s.id=j.scan_id WHERE j.scan_id=?''', (scan_id,)).fetchone()
        if not row:
            raise ValueError('Job descriptor missing')
        value = json.loads(row['descriptor'])
        if (row['kind'] not in (SCREEN, BIRTH_ACQUISITION_V1)
                or row['descriptor_version'] != DESCRIPTOR_VERSION
                or digest(value) != row['descriptor_hash']
                or type(value.get('schema_version')) is not int
                or value.get('schema_version') != DESCRIPTOR_VERSION
                or value.get('kind') != row['kind'] or value.get('source_version') != row['kind']
                or value.get('scan_id') != scan_id or value.get('mint') != row['mint']
                or value.get('admitted_at') != row['created']
                or value.get('research_db') != str(self.path) or value.get('request_ceiling') != 18):
            raise ValueError('Unknown or inconsistent job descriptor')
        expected = self._descriptor(scan_id, row['mint'], row['created'], row['kind'],
                                    value.get('evidence_db'))
        if value != expected or (row['kind'] == BIRTH_ACQUISITION_V1) != ('evidence_db' in value):
            raise ValueError('Unknown or inconsistent job descriptor')
        return value

    @contextmanager
    def worker(self):
        # Never unlink a lock file: replacing its inode would split exclusivity.
        canonical_job_path(self.path)
        lock_path = canonical_job_path(str(self.path)+'.jobs-worker.lock')
        with open(lock_path, 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield None
                return
            worker = _Worker(self)
            try:
                worker._recover()
                yield worker
            finally:
                worker.active = False
                # A fork inherits the open description; its cleanup must not
                # explicitly unlock the parent process's live invocation.
                if worker.pid == os.getpid():
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def recover(self):
        # Constructor callers may recover only when no invocation holds the lock.
        with self.worker() as worker:
            return worker is not None


class _Worker:
    """Capability valid only while its process owns the whole-invocation lock."""
    def __init__(self, store):
        self.store = store
        self.active = True
        self.pid = os.getpid()
        self.token = uuid.uuid4().hex

    def _check(self):
        if not self.active or self.pid != os.getpid():
            raise ValueError('Worker claim is no longer active')

    def _recover(self):
        self._check()
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE scan_jobs SET claim_token=NULL WHERE scan_id IN (SELECT id FROM scans WHERE status='RUNNING')")
            c.execute("UPDATE scans SET status='INTERRUPTED' WHERE status='RUNNING'")

    def claim_screen(self):
        self._check()
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            rows = c.execute('''SELECT s.id,s.mint,j.generation FROM scans s JOIN scan_jobs j ON j.scan_id=s.id
                                WHERE s.status='QUEUED' AND j.kind=? AND j.descriptor_version=?
                                ORDER BY s.rowid''', (SCREEN, DESCRIPTOR_VERSION)).fetchall()
            for row in rows:
                try:
                    self.store.descriptor(row['id'])
                except (ValueError, TypeError, KeyError):
                    # Corrupt/unknown jobs remain pending and consume shared limits.
                    continue
                generation = row['generation']+1
                c.execute('UPDATE scan_jobs SET claim_token=?,generation=? WHERE scan_id=?',
                          (self.token, generation, row['id']))
                c.execute("UPDATE scans SET status='RUNNING' WHERE id=? AND status='QUEUED'", (row['id'],))
                return Claim(row['id'], row['mint'], self.token, generation)
        return None

    def publish(self, claim, status, result):
        self._check()
        if status not in ('COMPLETE', 'FAILED') or claim.token != self.token:
            raise ValueError('Invalid terminal publication')
        payload = canonical(result)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self.store.descriptor(claim.scan_id)
            changed = c.execute('''UPDATE scans SET status=?,result=? WHERE id=? AND status='RUNNING'
                AND EXISTS(SELECT 1 FROM scan_jobs j WHERE j.scan_id=scans.id
                           AND j.claim_token=? AND j.generation=?)''',
                (status, payload, claim.scan_id, claim.token, claim.generation)).rowcount
            if changed != 1:
                raise ValueError('Stale worker publication')
            c.execute('UPDATE scan_jobs SET claim_token=NULL WHERE scan_id=?', (claim.scan_id,))
