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
            had_job_table = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scan_jobs'").fetchone() is not None
            c.execute('''CREATE TABLE IF NOT EXISTS scan_jobs(
                scan_id TEXT PRIMARY KEY REFERENCES scans(id), kind TEXT NOT NULL,
                descriptor_version INTEGER NOT NULL, descriptor TEXT NOT NULL,
                descriptor_hash TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
                claim_token TEXT)''')
            # REPLACE's implicit DELETE ignores delete triggers when a plain
            # connection has recursive_triggers=0. Reject conflicts before insert,
            # including explicit rowid conflicts, without relying on that pragma.
            c.execute('''CREATE TRIGGER IF NOT EXISTS reject_scan_job_reinsertion
                BEFORE INSERT ON scan_jobs
                WHEN EXISTS(SELECT 1 FROM scan_jobs
                            WHERE scan_id=NEW.scan_id OR rowid=NEW.rowid)
                BEGIN SELECT RAISE(ABORT,'Immutable job descriptor identity'); END''')
            c.execute('''CREATE TRIGGER IF NOT EXISTS immutable_scan_job_row_identity
                BEFORE UPDATE ON scan_jobs WHEN NEW.rowid IS NOT OLD.rowid
                BEGIN SELECT RAISE(ABORT,'Immutable job descriptor row identity'); END''')
            # Backfill only a genuinely pre-dispatch database. Existing dispatch
            # tables without metadata may have lost an acquisition descriptor;
            # neither upgrade nor reopen may infer SCREEN for that admission.
            c.execute('''CREATE TABLE IF NOT EXISTS scan_job_migrations(
                name TEXT PRIMARY KEY,version INTEGER NOT NULL) WITHOUT ROWID''')
            marker = c.execute("SELECT version FROM scan_job_migrations WHERE name='legacy_screen_descriptors'").fetchone()
            if marker is not None and marker['version'] != 1:
                raise ValueError('Unknown job descriptor migration version')
            if marker is None:
                if not had_job_table:
                    for row in c.execute('SELECT id,mint,created FROM scans').fetchall():
                        self._insert_descriptor(c, self._descriptor(row['id'], row['mint'], row['created'], SCREEN))
                c.execute("INSERT INTO scan_job_migrations VALUES('legacy_screen_descriptors',1)")
            c.execute('''CREATE TRIGGER IF NOT EXISTS immutable_scan_job_migration_update
                BEFORE UPDATE ON scan_job_migrations
                BEGIN SELECT RAISE(ABORT,'Immutable job migration marker'); END''')
            c.execute('''CREATE TRIGGER IF NOT EXISTS immutable_scan_job_migration_delete
                BEFORE DELETE ON scan_job_migrations
                BEGIN SELECT RAISE(ABORT,'Immutable job migration marker'); END''')
            c.execute('''CREATE TRIGGER IF NOT EXISTS immutable_scan_job_migration_insert
                BEFORE INSERT ON scan_job_migrations
                WHEN EXISTS(SELECT 1 FROM scan_job_migrations WHERE name=NEW.name)
                BEGIN SELECT RAISE(ABORT,'Immutable job migration marker'); END''')
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

    def upgrade_allowance(self, *, at, provenance):
        """Explicit atomic policy activation; retained admissions never reset."""
        from . import allowance_policy as policy
        if type(at) is not int or not 0 <= at < 2**63 or provenance != policy.PROVENANCE:
            raise ValueError('ALLOWANCE_UPGRADE_INVALID')
        canonical_job_path(self.path)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                limits = policy.research_limits(c, self.path)
                if limits == (policy.NEW_DAILY, policy.NEW_QUEUE):
                    return policy.read(c, policy.RESEARCH)
                latest = c.execute('SELECT max(created) FROM scans').fetchone()[0]
                if latest is not None and at < latest:
                    raise ValueError('ALLOWANCE_CLOCK_ROLLBACK')
                body = {'kind':'research_allowance_upgrade_v1','research_db':str(self.path),
                        'daily':policy.NEW_DAILY,'queued':policy.NEW_QUEUE,
                        'previous_daily':10,'previous_queued':3,'at':at,'provenance':provenance}
                key = policy.install(c, policy.RESEARCH, body)
                c.execute('INSERT INTO scan_job_migrations VALUES(?,1)', (policy.MARKER,))
                c.commit()
                return body, key
            except BaseException:
                c.rollback()
                raise

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
            pending="status IN ('QUEUED','RUNNING') OR (status='INTERRUPTED' AND EXISTS(SELECT 1 FROM scan_jobs j WHERE j.scan_id=scans.id AND j.kind='BIRTH_ACQUISITION_V1'))"
            from .allowance_policy import research_limits
            daily,queued=research_limits(c,self.path)
            if c.execute('SELECT count(*) FROM scans WHERE '+pending).fetchone()[0] >= queued:
                raise ValueError('Queue full. Wait for the current scans to finish.')
            if c.execute('SELECT count(*) FROM scans WHERE created>?', (created-86400,)).fetchone()[0] >= daily:
                raise ValueError(f'Daily budget reached: {daily} scans per rolling 24 hours.')
            if c.execute('SELECT 1 FROM scans WHERE mint=? AND ('+pending+')', (mint,)).fetchone():
                raise ValueError('This token already has a pending scan.')
            c.execute('INSERT INTO scans VALUES(?,?,?,?,?)', (uid, mint, created, 'QUEUED', None))
            self._insert_descriptor(c, descriptor)
        return uid

    def source(self, scan_id):
        """Exact five-column scan projection used by immutable budget sealing."""
        self.descriptor(scan_id)
        with self.connect() as c:
            row=c.execute('SELECT id,mint,created,status,result FROM scans WHERE id=?',(scan_id,)).fetchone()
        return dict(row)

    def descriptor(self, scan_id):
        with self.connect() as c:
            row = c.execute('''SELECT j.*,s.mint,s.created FROM scan_jobs j
                               JOIN scans s ON s.id=j.scan_id WHERE j.scan_id=?''', (scan_id,)).fetchone()
        if not row:
            raise ValueError('Job descriptor missing')
        value = json.loads(row['descriptor'])
        if not isinstance(value, dict):
            raise ValueError('Job descriptor must be a JSON object')
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
                # Existing corrupt mutable checkpoints must not stop the worker
                # or overflow SQLite's signed 64-bit integer on increment.
                if type(row['generation']) is not int or not 0 <= row['generation'] < (1 << 63)-1:
                    continue
                generation = row['generation']+1
                c.execute('UPDATE scan_jobs SET claim_token=?,generation=? WHERE scan_id=?',
                          (self.token, generation, row['id']))
                c.execute("UPDATE scans SET status='RUNNING' WHERE id=? AND status='QUEUED'", (row['id'],))
                return Claim(row['id'], row['mint'], self.token, generation)
        return None

    def claim_acquisition(self, scan_id):
        self._check()
        descriptor=self.store.descriptor(scan_id)
        if descriptor['kind']!=BIRTH_ACQUISITION_V1:raise ValueError('Acquisition job required')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT s.status,j.generation FROM scans s JOIN scan_jobs j ON j.scan_id=s.id WHERE s.id=?',(scan_id,)).fetchone()
            if row['status'] not in ('QUEUED','INTERRUPTED'):raise ValueError('Acquisition is not resumable')
            if type(row['generation']) is not int or not 0<=row['generation']<(1<<63)-1:raise ValueError('Invalid claim generation')
            generation=row['generation']+1
            c.execute('UPDATE scan_jobs SET claim_token=?,generation=? WHERE scan_id=?',(self.token,generation,scan_id))
            c.execute("UPDATE scans SET status='RUNNING' WHERE id=?",(scan_id,))
        return Claim(scan_id,descriptor['mint'],self.token,generation)

    def _acquisition_claim(self,claim):
        self._check()
        descriptor=self.store.descriptor(claim.scan_id)
        if descriptor['kind']!=BIRTH_ACQUISITION_V1 or descriptor['mint']!=claim.mint or claim.token!=self.token:
            raise ValueError('Invalid acquisition claim')
        with self.store.connect() as c:
            live=c.execute("SELECT 1 FROM scans s JOIN scan_jobs j ON j.scan_id=s.id WHERE s.id=? AND s.status='RUNNING' AND j.claim_token=? AND j.generation=?",(claim.scan_id,claim.token,claim.generation)).fetchone()
        if not live:raise ValueError('Stale acquisition claim')
        return descriptor

    def interrupt_acquisition(self,claim):
        self._acquisition_claim(claim)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            changed=c.execute("""UPDATE scans SET status='INTERRUPTED' WHERE id=? AND status='RUNNING'
                AND EXISTS(SELECT 1 FROM scan_jobs j WHERE j.scan_id=scans.id AND j.claim_token=? AND j.generation=?)""",
                (claim.scan_id,claim.token,claim.generation)).rowcount
            if changed!=1:raise ValueError('Stale worker interruption')
            c.execute('UPDATE scan_jobs SET claim_token=NULL WHERE scan_id=?',(claim.scan_id,))

    def publish_acquisition(self,claim,source):
        descriptor=self._acquisition_claim(claim)
        if (set(source)!={'id','mint','created','status','result'} or source['id']!=claim.scan_id
                or source['mint']!=claim.mint or source['created']!=descriptor['admitted_at']
                or source['status']!='COMPLETE' or not isinstance(source['result'],str)
                or canonical(json.loads(source['result']))!=source['result']):
            raise ValueError('Exact canonical completed source required')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            changed=c.execute("""UPDATE scans SET status='COMPLETE',result=? WHERE id=? AND status='RUNNING'
                AND EXISTS(SELECT 1 FROM scan_jobs j WHERE j.scan_id=scans.id AND j.claim_token=? AND j.generation=?)""",
                (source['result'],claim.scan_id,claim.token,claim.generation)).rowcount
            if changed!=1:raise ValueError('Stale acquisition publication')
            c.execute('UPDATE scan_jobs SET claim_token=NULL WHERE scan_id=?',(claim.scan_id,))

    def publish(self, claim, status, result):
        self._check()
        if status not in ('COMPLETE', 'FAILED') or claim.token != self.token:
            raise ValueError('Invalid terminal publication')
        payload = canonical(result)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if self.store.descriptor(claim.scan_id)['kind']!=SCREEN:raise ValueError('Use exact acquisition publication')
            changed = c.execute('''UPDATE scans SET status=?,result=? WHERE id=? AND status='RUNNING'
                AND EXISTS(SELECT 1 FROM scan_jobs j WHERE j.scan_id=scans.id
                           AND j.claim_token=? AND j.generation=?)''',
                (status, payload, claim.scan_id, claim.token, claim.generation)).rowcount
            if changed != 1:
                raise ValueError('Stale worker publication')
            c.execute('UPDATE scan_jobs SET claim_token=NULL WHERE scan_id=?', (claim.scan_id,))

def main(argv=None):
    import argparse
    from .paper_observe_cli import _worker_lock
    parser=argparse.ArgumentParser(description='Explicit research allowance upgrade; existing database only')
    parser.add_argument('--research-db',required=True)
    parser.add_argument('--provenance',required=True)
    args=parser.parse_args(argv)
    try:
        path=canonical_job_path(args.research_db)
        if not path.is_file():raise ValueError('Existing research database required')
        with _worker_lock(path) as worker:
            if worker is None:raise ValueError('Research worker busy')
            jobs=JobPersistence.__new__(JobPersistence);jobs.path=path
            def connect():
                c=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=15)
                c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c
            jobs.connect=connect
            body,key=jobs.upgrade_allowance(at=int(time.time()),provenance=args.provenance)
        print(json.dumps({'status':'EXPLICITLY_ACTIVATED','policy_hash':key,'daily':body['daily'],'queued':body['queued']}));return 0
    except (ValueError,OSError,sqlite3.Error,TypeError,KeyError):
        print(json.dumps({'status':'BLOCKED','blockers':['EXPLICIT_ALLOWANCE_UPGRADE_UNAVAILABLE']}));return 2


if __name__=='__main__':raise SystemExit(main())
