"""Explicit shared provider cadence; no request budgets, retries or credentials.

Provision one protected DB and DESK_PROVIDER_PACING_DB in every participating
process. Wall clock is a trusted host input; rollback refuses. A granted slot is
never refunded after process death. Waiters expire within five seconds; held
requests precede investigations that have not already received a slot.

An unfinished grant whose owning PROCESS is gone (kernel holder lock free) is reclaimed after
max(60 s, 30 x cadence) by Pacer.reclaim_orphans()/acquire(), recorded append-only in `pacing_reclaims`.
That table is added only by the explicit, idempotent `upgrade()` (`--upgrade DB`); every process using the
shared database must run the new code first (old code rejects the extra table). Without the upgrade nothing is
ever reclaimed.

Reviewed cadence changes (paid provider plans) are recorded append-only in `pacing_policy_changes`, added only by
the explicit, idempotent `apply_policy()` (`python -m tools.ops.pacing_policy apply`). The original `policy` rows
are NEVER edited: the Kraken migration receipt pins them, and the effective cadence/backoff of helius and jupiter is
the newest change row, itself validated against the release's reviewed `config/provider-pacing-policy.json`. Kraken
is not changeable here. Every process using the shared database must run code that knows the table before it is
added (older code rejects the extra table, i.e. fails closed).

Holder lock files (``<db>.holder-<provider>.lock``) are the owner proof: NEVER delete one. A lock file that is
removed and recreated no longer names the inode a live owner holds, so a second process could take "its" lock and
the live owner's slot would look orphaned. A forked child inherits the open lock descriptor (flock belongs to the
open file description), so the slot stays protected until the LAST copy closes; an owner that forks and exits
leaves the child as the owner. The lock is released only after the commit that records the outcome has succeeded
(see ``_commit_release``): if the commit keeps failing the lock is kept until process exit, so a live owner is
never reclaimed and a throttle's Retry-After is never lost.
"""
import argparse
from contextlib import closing
import fcntl
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid

PROVIDERS = ('helius', 'jupiter')
MAX_WAIT = 5.0
MAX_WAITERS = 32
MAX_DB_BYTES = 2 * 1024 * 1024
ENV = 'DESK_PROVIDER_PACING_DB'
# A granted slot whose owner process is gone is released only after BOTH this much time has
# passed since the grant AND the owner's kernel-held holder lock is provably free.
COMMIT_ATTEMPTS = 6                  # bounded retries of a busy COMMIT (finish/throttle) before giving up
COMMIT_BACKOFF = (0.05, 0.5)         # first delay and cap, doubling
HOLD_ATTEMPTS = 20                   # a new grantee waits briefly for the previous owner's post-commit release
HOLD_DELAY = 0.005
RECLAIM_INTERVALS = 30
RECLAIM_MIN_SECONDS = 60.0
RECLAIM_TABLE = 'pacing_reclaims'
RECLAIM_SQL = ('CREATE TABLE pacing_reclaims(id INTEGER PRIMARY KEY AUTOINCREMENT,provider TEXT NOT NULL,'
               'ticket TEXT NOT NULL UNIQUE,granted_at REAL NOT NULL,reclaimed_at REAL NOT NULL,'
               'backoff_until REAL NOT NULL,reason TEXT NOT NULL)')
RECLAIM_GUARDS = {
    f'pacing_reclaims_no_{op.lower()}': (f'CREATE TRIGGER pacing_reclaims_no_{op.lower()} BEFORE {op} ON pacing_reclaims '
                                         "BEGIN SELECT RAISE(ABORT,'Immutable pacing reclaim record'); END")
    for op in ('UPDATE', 'DELETE')}
MAX_RECLAIMS = 10000
POLICY_TABLE = 'pacing_policy_changes'
POLICY_FILE = Path(__file__).resolve().parents[1] / 'config' / 'provider-pacing-policy.json'
POLICY_FILE_BOUND = 65536
MAX_POLICY_CHANGES = 64
POLICY_SQL = ('CREATE TABLE pacing_policy_changes(id INTEGER PRIMARY KEY,'
              "provider TEXT NOT NULL CHECK(provider IN ('helius','jupiter')),"
              'old_cadence REAL NOT NULL,new_cadence REAL NOT NULL,old_backoff REAL NOT NULL,new_backoff REAL NOT NULL,'
              'reason TEXT NOT NULL,config_sha256 TEXT NOT NULL,policy_digest TEXT NOT NULL,applied_at REAL NOT NULL)')
POLICY_GUARDS = {
    **{f'pacing_policy_changes_no_{op.lower()}': (f'CREATE TRIGGER pacing_policy_changes_no_{op.lower()} BEFORE {op} ON '
                                                  "pacing_policy_changes BEGIN SELECT RAISE(ABORT,'Immutable pacing policy record'); END")
       for op in ('UPDATE', 'DELETE')},
    'pacing_policy_changes_order': ('CREATE TRIGGER pacing_policy_changes_order BEFORE INSERT ON pacing_policy_changes '
                                    'WHEN NEW.id IS NOT (SELECT COALESCE(MAX(id),0)+1 FROM pacing_policy_changes) '
                                    "BEGIN SELECT RAISE(ABORT,'Pacing policy record order'); END")}
# Holder locks of grants this PROCESS owns: (database path, st_dev, st_ino, provider) -> (ticket, fd, pid, committed). Only process exit (any
# death, including SIGKILL) or finish()/throttle() releases them. Never a Pacer object's lifetime: a caller may
# deliberately keep a ticket pending (pacing_release=False) while its Pacer is garbage-collected, and that owner is
# still alive. Any Pacer in the process may acknowledge a ticket another Pacer in the process took.
_HELD = {}


def _purge_stale_holds():
    """Drop (and close) entries that can no longer belong to a live database in this process.

    An entry is trusted only while its database path still names the SAME file (device and inode) and it was
    taken by this process (a forked child must not treat an inherited descriptor as its own grant). A deleted or
    recreated database, or inode reuse after a temp directory vanished, therefore never gives another database
    a phantom live owner.
    """
    for key in list(_HELD):
        path, dev, ino, _provider = key
        ticket, fd, pid, _committed = _HELD[key]
        try:
            info = os.stat(path)
            same = (info.st_dev, info.st_ino) == (dev, ino) and pid == os.getpid()
        except OSError:
            same = False
        if not same:
            del _HELD[key]
            try: os.close(fd)
            except OSError: pass


class PacingError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value < 2**53


def _path(path):
    p = Path(path).absolute()
    try:
        info = p.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_DB_BYTES):
            raise ValueError()
    except (OSError, ValueError):
        raise PacingError('PACING_DATABASE_INVALID') from None
    return p.parent.resolve() / p.name, (info.st_dev, info.st_ino)


def initialize(path, *, helius_seconds=2.0, jupiter_seconds=2.0, backoff_seconds=30.0):
    for value in (helius_seconds, jupiter_seconds):
        if not _number(value) or not .05 <= value <= 60:
            raise PacingError('PACING_POLICY_INVALID')
    if not _number(backoff_seconds) or not 1 <= backoff_seconds <= 3600:
        raise PacingError('PACING_POLICY_INVALID')
    p = Path(path).absolute()
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(p)) as c:
        c.executescript('''
        PRAGMA journal_mode=DELETE;
        CREATE TABLE policy(provider TEXT PRIMARY KEY,version INTEGER NOT NULL,
                            cadence REAL NOT NULL,backoff REAL NOT NULL);
        CREATE TABLE state(provider TEXT PRIMARY KEY,next_at REAL NOT NULL,
                           blocked_until REAL NOT NULL,high_water REAL NOT NULL,pending TEXT);
        CREATE TABLE waiters(ticket TEXT PRIMARY KEY,provider TEXT NOT NULL,
                             priority TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL);
        ''')
        for provider, cadence in zip(PROVIDERS, (helius_seconds, jupiter_seconds)):
            c.execute('INSERT INTO policy VALUES(?,2,?,?)', (provider, cadence, backoff_seconds))
            c.execute('INSERT INTO state VALUES(?,0,0,0,NULL)', (provider,))
        c.commit()


def _effective(c, provider):
    """(cadence, backoff) in force for a provider: the newest reviewed change row, else the original policy row.
    Kraken is pinned and never changes. The change rows are validated by Pacer._validate before this is trusted."""
    row = c.execute('SELECT cadence,backoff FROM policy WHERE provider=?', (provider,)).fetchone()
    if provider != 'kraken' and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (POLICY_TABLE,)).fetchone():
        later = c.execute(f'SELECT new_cadence,new_backoff FROM {POLICY_TABLE} WHERE provider=? ORDER BY id DESC LIMIT 1',
                          (provider,)).fetchone()
        if later is not None:
            return later
    return row


def _reviewed():
    """{policy_digest: entry}, file sha256 of this release's reviewed policy file; anything malformed raises."""
    from .model import digest
    with POLICY_FILE.open('rb') as stream:
        raw = stream.read(POLICY_FILE_BOUND + 1)
    if len(raw) > POLICY_FILE_BOUND:
        raise ValueError('Pacing policy file bound')
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out: raise ValueError('Duplicate key')
            out[key] = value
        return out
    doc = json.loads(raw, object_pairs_hook=unique)
    if type(doc) is not dict or set(doc) != {'version', 'policies'} or doc['version'] != 1 or type(doc['version']) is not int \
            or type(doc['policies']) is not list or not 1 <= len(doc['policies']) <= 16:
        raise ValueError('Pacing policy file shape')
    out, ids = {}, set()
    for entry in doc['policies']:
        if type(entry) is not dict or set(entry) != {'id', 'reason', 'providers'}:
            raise ValueError('Pacing policy entry shape')
        if (type(entry['id']) is not str or not 1 <= len(entry['id']) <= 128 or entry['id'] in ids
                or type(entry['reason']) is not str or not 1 <= len(entry['reason']) <= 512
                or type(entry['providers']) is not dict or not entry['providers']
                or not set(entry['providers']) <= set(PROVIDERS)):
            raise ValueError('Pacing policy entry invalid')
        ids.add(entry['id'])
        for values in entry['providers'].values():
            if type(values) is not dict or set(values) != {'cadence', 'backoff'}:
                raise ValueError('Pacing policy provider shape')
            cadence, backoff = values['cadence'], values['backoff']
            if (type(cadence) not in (int, float) or type(backoff) not in (int, float) or isinstance(cadence, bool)
                    or not _number(cadence) or not .05 <= cadence <= 60 or not _number(backoff) or not 1 <= backoff <= 3600):
                raise ValueError('Pacing policy values')
        out[digest(entry)] = entry
    return out, hashlib.sha256(raw).hexdigest()


# Lowest class: research/measurement processes (fill realism, counterfactual) queue behind trading quotes.
PRIORITIES = ('held', 'investigation', 'research')


class Pacer:
    def __init__(self, path, *, priority='investigation', clock=time.time,
                 monotonic=time.monotonic, sleep=time.sleep):
        if priority not in PRIORITIES:
            raise PacingError('PACING_PRIORITY_INVALID')
        self.path, self.identity = _path(path)
        self.priority, self.clock, self.monotonic, self.sleep = priority, clock, monotonic, sleep
        self._validate()

    def _connect(self):
        if _path(self.path)[1] != self.identity:
            raise PacingError('PACING_DATABASE_CHANGED')
        c = sqlite3.connect(self.path.as_uri()+'?mode=rw', uri=True, isolation_level=None, timeout=.05)
        c.execute('PRAGMA synchronous=FULL')
        if c.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
            c.close(); raise PacingError('PACING_DATABASE_INVALID')
        return c

    def _validate(self):
        try:
            with closing(self._connect()) as c:
                tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                from .kraken_pacing_migration import read as migration
                receipt=migration(c,self.path)
                self.providers=PROVIDERS+('kraken',) if receipt else PROVIDERS
                reclaims = tables & {RECLAIM_TABLE, 'sqlite_sequence', POLICY_TABLE}   # AUTOINCREMENT owns sqlite_sequence
                if ('sqlite_sequence' in tables) != (RECLAIM_TABLE in tables): raise ValueError()
                if tables - reclaims != {'policy','state','waiters'}|({'kraken_pacing_migration'} if receipt else set()): raise ValueError()
                if RECLAIM_TABLE in tables: self._validate_reclaims(c)
                rows = c.execute('SELECT provider,version,cadence,backoff FROM policy').fetchall()
                if len(rows) != len(self.providers) or {r[0] for r in rows} != set(self.providers): raise ValueError()
                for provider, version, cadence, backoff in rows:
                    if provider=='kraken' and (cadence<2 or cadence!=2 or backoff!=30):raise ValueError()
                    if version != 2 or not _number(cadence) or not .05 <= cadence <= 60 or not _number(backoff) or not 1 <= backoff <= 3600: raise ValueError()
                if POLICY_TABLE in tables:
                    self._validate_policy_changes(c, {r[0]: (r[2], r[3]) for r in rows if r[0] != 'kraken'})
                rows = c.execute('SELECT provider,next_at,blocked_until,high_water,pending FROM state').fetchall()
                if len(rows) != len(self.providers) or {r[0] for r in rows} != set(self.providers): raise ValueError()
                if any(not _number(v) for r in rows for v in r[1:4]): raise ValueError()
                if any(r[4] is not None and (type(r[4]) is not str or len(r[4]) != 32
                       or any(ch not in '0123456789abcdef' for ch in r[4])) for r in rows): raise ValueError()
                waiters = c.execute('SELECT ticket,provider,priority,created,expires FROM waiters LIMIT 97').fetchall()
                if len(waiters) > len(self.providers)*MAX_WAITERS: raise ValueError()
                for ticket, provider, priority, created, expires in waiters:
                    if (type(ticket) is not str or len(ticket) != 32 or provider not in self.providers
                            or priority not in PRIORITIES or not _number(created)
                            or not _number(expires) or not 0 < expires-created <= MAX_WAIT): raise ValueError()
        except (sqlite3.Error, ValueError, TypeError, OSError):      # OSError: the reviewed policy file is unreadable
            raise PacingError('PACING_DATABASE_INVALID') from None

    @staticmethod
    def _validate_policy_changes(c, original):
        """The change rows must be exactly the chain of reviewed policy entries: schema and guards pinned, ids 1..n,
        every row starting from the previous effective value (the original policy row for the first), digest and values
        equal to an entry of this release's reviewed file, reason equal to that entry's, times non-decreasing."""
        if (dict(c.execute("SELECT name,sql FROM sqlite_master WHERE tbl_name=? AND type='table'", (POLICY_TABLE,))) != {POLICY_TABLE: POLICY_SQL}
                or dict(c.execute("SELECT name,sql FROM sqlite_master WHERE tbl_name=? AND type='trigger'", (POLICY_TABLE,))) != POLICY_GUARDS):
            raise ValueError()
        if c.execute(f'SELECT COUNT(*) FROM {POLICY_TABLE}').fetchone()[0] > MAX_POLICY_CHANGES: raise ValueError()
        reviewed, _ = _reviewed()
        current, last = dict(original), 0.0
        rows = c.execute(f'SELECT id,provider,old_cadence,new_cadence,old_backoff,new_backoff,reason,config_sha256,policy_digest,applied_at,'
                         f'typeof(old_cadence),typeof(new_cadence),typeof(old_backoff),typeof(new_backoff),typeof(applied_at),'
                         f'typeof(reason),typeof(config_sha256),typeof(policy_digest) FROM {POLICY_TABLE} ORDER BY id').fetchall()
        for index, row in enumerate(rows, 1):
            (ident, provider, old_c, new_c, old_b, new_b, reason, sha, policy_digest, at) = row[:10]
            if (ident != index or provider not in PROVIDERS or any(t != 'real' for t in row[10:15])
                    or any(t != 'text' for t in row[15:])): raise ValueError()
            if (current[provider] != (old_c, old_b) or not all(_number(v) for v in (old_c, new_c, old_b, new_b, at)) or at < last
                    or type(sha) is not str or len(sha) != 64 or any(ch not in '0123456789abcdef' for ch in sha)
                    or type(policy_digest) is not str): raise ValueError()
            entry = reviewed.get(policy_digest)
            if (entry is None or provider not in entry['providers'] or reason != entry['reason']
                    or (new_c, new_b) != (float(entry['providers'][provider]['cadence']), float(entry['providers'][provider]['backoff']))):
                raise ValueError()
            current[provider], last = (new_c, new_b), at

    @staticmethod
    def _validate_reclaims(c):
        if (dict(c.execute("SELECT name,sql FROM sqlite_master WHERE tbl_name=? AND type='table'", (RECLAIM_TABLE,))) != {RECLAIM_TABLE: RECLAIM_SQL}
                or dict(c.execute("SELECT name,sql FROM sqlite_master WHERE tbl_name=? AND type='trigger'", (RECLAIM_TABLE,))) != RECLAIM_GUARDS):
            raise ValueError()
        count, bad = c.execute("SELECT COUNT(*),COALESCE(SUM(typeof(provider)!='text' OR typeof(ticket)!='text' OR length(ticket)!=32 "
                               "OR typeof(granted_at)!='real' OR typeof(reclaimed_at)!='real' OR typeof(backoff_until)!='real' "
                               "OR reason!='OWNER_GONE'),0) FROM pacing_reclaims").fetchone()
        if count > MAX_RECLAIMS or bad: raise ValueError()

    @property
    def _held(self):
        """provider -> (ticket, fd) for the grants this process holds on this database."""
        _purge_stale_holds()
        identity = (str(self.path),) + tuple(self.identity)
        mine = {key: held for key, held in _HELD.items() if key[:3] == identity}
        if mine:
            # A held ticket is only meaningful while this database still records it as pending. Inode numbers are
            # reused after a delete, so a recreated database must not inherit a phantom owner from the old file.
            try:
                with closing(self._connect()) as c:
                    pending = dict(c.execute('SELECT provider,pending FROM state'))
            except (sqlite3.Error, PacingError):
                return {key[3]: held[:2] for key, held in mine.items()}      # cannot verify: report, never purge
            for key, held in list(mine.items()):
                if held[3] and pending.get(key[3]) != held[0]:   # only committed grants can be contradicted by the file
                    del mine[key]
                    if _HELD.get(key) is held:
                        del _HELD[key]
                        try: os.close(held[1])
                        except OSError: pass
        return {key[3]: held[:2] for key, held in mine.items()}

    def _hold_key(self, provider):
        return (str(self.path),) + tuple(self.identity) + (provider,)

    def _lock_path(self, provider):
        return str(self.path) + '.holder-' + provider + '.lock'

    def _hold(self, provider, ticket):
        """Take the kernel lock that proves this process owns the slot; call BEFORE committing the grant.

        flock is released by the kernel on any process death (including SIGKILL), which is the only
        owner-gone proof available without extra state. If it cannot be taken nothing has been committed
        yet (the caller's transaction is rolled back), so no unprotected ticket can ever exist.
        """
        _purge_stale_holds()
        # acquire() grants only while `pending` is NULL, so an entry still registered for this very database and
        # provider is a leftover (finish() was never called, or the file was deleted and its inode reused): it
        # protects nothing and must not hold the lock the new grant needs.
        previous = _HELD.pop(self._hold_key(provider), None)
        if previous is not None:
            try: os.close(previous[1])
            except OSError: pass
        fd = None
        try:
            fd = os.open(self._lock_path(provider), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            for attempt in range(HOLD_ATTEMPTS):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    # The previous owner committed its outcome and is about to release (release follows the commit).
                    if attempt + 1 == HOLD_ATTEMPTS: raise
                    time.sleep(HOLD_DELAY)
        except OSError:
            if fd is not None: os.close(fd)
            raise PacingError('PACING_HOLDER_LOCK_UNAVAILABLE') from None
        _HELD[self._hold_key(provider)] = (ticket, fd, os.getpid(), False)   # becomes committed after the grant commits

    def _confirm(self, provider, ticket):
        key = self._hold_key(provider)
        held = _HELD.get(key)
        if held and held[0] == ticket:
            _HELD[key] = held[:3] + (True,)

    def _release(self, provider, ticket=None):
        """Close this process's holder lock for (provider, ticket), whichever Pacer instance took it."""
        key = self._hold_key(provider)
        held = _HELD.get(key)
        if held and (ticket is None or held[0] == ticket):
            del _HELD[key]
            try: os.close(held[1])
            except OSError: pass

    def reclaim_orphans(self):
        """Reclaim every provider's orphaned grant whose owner PROCESS is provably gone; returns the providers.

        Same proofs and the same append-only record as acquire() (age >= max(60 s, 30 x cadence), holder lock
        free, row cap, table present). Called by the entry gates before they test `pending`, because those
        gates refuse on a pending grant without ever calling acquire(). A live owner is never reclaimed;
        `blocked_until` is only ever raised.

        Read first: the write lock is taken only when an old enough pending row exists. A busy database
        returns [] (no reclaim this time) and never raises, so the gate cannot create a new unresolved path.
        """
        try:
            with closing(self._connect()) as c:
                c.execute('BEGIN')                                    # deferred: a plain read, no write lock
                now = self._reclaim_now(c)
                if now is None: return []
                if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (RECLAIM_TABLE,)).fetchone():
                    return []
                old = False
                for provider, next_at, pending in c.execute('SELECT provider,next_at,pending FROM state').fetchall():
                    if pending is None or provider not in self.providers: continue
                    cadence = _effective(c, provider)[0]
                    if now - (next_at - cadence) >= max(RECLAIM_MIN_SECONDS, RECLAIM_INTERVALS * cadence): old = True
                c.rollback()
                if not old: return []
                done = []
                c.execute('BEGIN IMMEDIATE')                          # now (and only now) the write lock; re-proved inside
                now = self._reclaim_now(c)
                if now is None:
                    c.rollback()
                    return []
                for provider in self.providers:
                    row = c.execute('SELECT next_at,pending FROM state WHERE provider=?', (provider,)).fetchone()
                    if row and row[1] is not None and self._reclaim(c, provider, row[1], row[0], now):
                        done.append(provider)
                c.commit()
                return done
        except sqlite3.Error:
            return []

    def _reclaim(self, c, provider, ticket, next_at, now):
        """Release an orphaned grant inside the caller's write transaction; True when released.

        Cadence, next_at and the provider embargo are never lowered. The unknown outcome is treated
        like a throttle without Retry-After starting now (fixed backoff), and an append-only row
        records the release. At MAX_RECLAIMS rows nothing is reclaimed (fail closed).
        """
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (RECLAIM_TABLE,)).fetchone():
            return False                        # not upgraded: the schema change is never a side effect of a reclaim
        cadence, backoff = _effective(c, provider)
        granted = next_at - cadence
        if now - granted < max(RECLAIM_MIN_SECONDS, RECLAIM_INTERVALS * cadence): return False
        if c.execute('SELECT COUNT(*) FROM pacing_reclaims').fetchone()[0] >= MAX_RECLAIMS: return False
        try:
            fd = os.open(self._lock_path(provider), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError: return False
        try:
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError: return False        # owner alive (or unprovable): never reclaim
            until = now + backoff
            if not (_number(until) and _number(granted)): return False
            c.execute('INSERT INTO pacing_reclaims(provider,ticket,granted_at,reclaimed_at,backoff_until,reason) VALUES(?,?,?,?,?,?)',
                      (provider, ticket, float(granted), float(now), float(until), 'OWNER_GONE'))
            c.execute('UPDATE state SET pending=NULL,blocked_until=MAX(blocked_until,?),high_water=? WHERE provider=? AND pending=?',
                      (until, now, provider, ticket))
            return True
        finally:
            os.close(fd)

    def _reclaim_now(self, c):
        """The same clock and the same validity rule as acquire(), but a bad clock is "do nothing", never an exception.

        Reclaim runs on the entry gates' read-first path, which refuse on a pending grant without calling acquire(). A
        clock behind `high_water` or not a usable number is acquire()'s PACING_CLOCK_INVALID to report; here it only means
        "prove nothing this time", so the gate cannot be turned into a crash and nothing is reclaimed on an untrusted clock.
        """
        try:
            return self._now(c)
        except PacingError as error:
            if error.code == 'PACING_CLOCK_INVALID': return None
            raise

    def _now(self, c):
        now = self.clock()
        if not _number(now) or now >= 2**40 or now < c.execute('SELECT MAX(high_water) FROM state').fetchone()[0]:
            raise PacingError('PACING_CLOCK_INVALID')
        return now

    def acquire(self, provider, *, timeout_seconds):
        if provider not in self.providers or not _number(timeout_seconds) or timeout_seconds <= 0:
            raise PacingError('PACING_REQUEST_INVALID')
        duration = min(timeout_seconds, MAX_WAIT)
        started = self.monotonic()
        if not _number(started): raise PacingError('PACING_CLOCK_INVALID')
        deadline = started + duration
        ticket = uuid.uuid4().hex
        registered = False
        try:
            while True:
                tick = self.monotonic()
                if not _number(tick) or tick < started: raise PacingError('PACING_CLOCK_INVALID')
                if tick >= deadline: raise PacingError('PACING_DEADLINE_EXCEEDED')
                with closing(self._connect()) as c:
                    c.execute('BEGIN IMMEDIATE')
                    if provider=='kraken':
                        from .kraken_pacing_migration import read as migration
                        if migration(c,self.path) is None or c.execute("SELECT version,cadence,backoff FROM policy WHERE provider='kraken'").fetchone()!=(2,2.0,30.0):
                            raise PacingError('PACING_DATABASE_INVALID')
                    now = self._now(c)
                    c.execute('DELETE FROM waiters WHERE expires<=?', (now,))
                    if not registered:
                        if c.execute('SELECT COUNT(*) FROM waiters WHERE provider=?',(provider,)).fetchone()[0] >= MAX_WAITERS:
                            raise PacingError('PACING_QUEUE_FULL')
                        c.execute('INSERT INTO waiters VALUES(?,?,?,?,?)',(ticket,provider,self.priority,now,now+duration))
                        registered = True
                    row = c.execute('SELECT next_at,blocked_until,pending FROM state WHERE provider=?',(provider,)).fetchone()
                    if row[2] is not None:
                        if self._reclaim(c, provider, row[2], row[0], now): c.commit(); continue
                        raise PacingError('PACING_OUTCOME_PENDING')
                    first = c.execute("SELECT ticket FROM waiters WHERE provider=? ORDER BY CASE priority WHEN 'held' THEN 0 WHEN 'investigation' THEN 1 ELSE 2 END,created,ticket LIMIT 1",(provider,)).fetchone()
                    due = max(row[:2])
                    c.execute('UPDATE state SET high_water=? WHERE provider=?',(now,provider))
                    if first and first[0] == ticket and now >= due:
                        cadence = _effective(c, provider)[0]
                        c.execute('UPDATE state SET next_at=?,pending=? WHERE provider=?',(math.nextafter(now+cadence, math.inf),ticket,provider))
                        c.execute('DELETE FROM waiters WHERE ticket=?',(ticket,))
                        self._hold(provider, ticket)       # owner proof BEFORE the grant becomes visible
                        try: c.commit()
                        except BaseException:
                            self._release(provider, ticket); raise
                        self._confirm(provider, ticket)
                        return ticket
                    c.commit()
                self.sleep(min(.05, max(0,deadline-tick)))
        except sqlite3.Error:
            raise PacingError('PACING_DATABASE_BUSY') from None
        finally:
            if registered:
                try:
                    with closing(self._connect()) as c: c.execute('DELETE FROM waiters WHERE ticket=?',(ticket,))
                except (sqlite3.Error, PacingError): pass  # Durable expiry still bounds abandoned waiters.

    def _commit_release(self, c, provider, ticket):
        """Commit, THEN release the holder lock. A busy COMMIT (a reader's shared lock) is retried with bounded
        backoff while the lock is still held; if it never succeeds the error propagates and the lock is kept until
        process exit, so a live owner's slot cannot be reclaimed and a throttle's Retry-After cannot be lost."""
        delay = COMMIT_BACKOFF[0]
        for attempt in range(COMMIT_ATTEMPTS):
            try:
                c.commit(); break
            except sqlite3.OperationalError:
                if attempt + 1 == COMMIT_ATTEMPTS: raise
                self.sleep(delay); delay = min(delay * 2, COMMIT_BACKOFF[1])
        self._release(provider, ticket)

    def finish(self, provider, ticket):
        """Acknowledge an outcome with no unrecorded backoff; never clear another grant."""
        try:
            with closing(self._connect()) as c:
                c.execute('BEGIN IMMEDIATE');now=self._now(c)
                self._pending(c,provider,ticket)
                c.execute('UPDATE state SET pending=NULL,high_water=? WHERE provider=?',(now,provider))
                self._commit_release(c, provider, ticket)   # release only AFTER the outcome is durable
        except sqlite3.Error:
            raise PacingError('PACING_DATABASE_BUSY') from None

    def _pending(self,c,provider,ticket):
        if provider not in self.providers or type(ticket) is not str or len(ticket)!=32:
            raise PacingError('PACING_REQUEST_INVALID')
        row=c.execute('SELECT pending FROM state WHERE provider=?',(provider,)).fetchone()
        if not row or row[0]!=ticket: raise PacingError('PACING_GRANT_MISMATCH')

    def throttle(self, provider, headers, *, ticket):
        """Shared429 embargo; missing/ambiguous Retry-After uses fixed backoff.

        Retry-After is bounded input, never logged. A valid long delay is honored,
        not clamped down; an unrepresentable delay fails closed indefinitely.
        """
        if provider not in self.providers: raise PacingError('PACING_REQUEST_INVALID')
        try:
            with closing(self._connect()) as c:
                c.execute('BEGIN IMMEDIATE');now=self._now(c)
                self._pending(c,provider,ticket)
                fallback=_effective(c, provider)[1]
                until=now+fallback
                values=headers.get_all('Retry-After') if hasattr(headers,'get_all') else None
                if values is None:
                    value=headers.get('Retry-After') if headers is not None else None
                    values=[] if value is None else [value]
                if len(values)==1 and type(values[0]) is str and len(values[0])<=128:
                    value=values[0].strip()
                    try:
                        if value.isascii() and value.isdecimal(): delay=int(value)
                        else:
                            date=parsedate_to_datetime(value)
                            if date.tzinfo is None: raise ValueError()
                            delay=max(0,date.timestamp()-now)
                        until=now+max(fallback,delay)
                        if not _number(until):until=2**53-1
                    except (ValueError,TypeError,OverflowError): pass
                elif values: until=2**53-1  # Ambiguous/oversize instructions cannot permit an early retry.
                c.execute('UPDATE state SET blocked_until=MAX(blocked_until,?),high_water=?,pending=NULL WHERE provider=?',(until,now,provider))
                self._commit_release(c, provider, ticket)
        except sqlite3.Error:
            raise PacingError('PACING_DATABASE_BUSY') from None


def upgrade(path):
    """Explicitly add the append-only `pacing_reclaims` table (and its guards) to an existing pacing database.

    Idempotent: returns 'UPGRADED' the first time and 'ALREADY_UPGRADED' afterwards. It validates the whole
    database first (so a tampered or unexpected schema is refused), touches no existing row, resets no counter or
    cadence, and is the only thing that ever creates the table. Run it only after every process that uses the
    shared database runs this code: older code raises PACING_DATABASE_INVALID on the extra table (and
    tools/verify_*_originals.py compare the schema strictly).
    """
    pacer = Pacer(path)
    try:
        with closing(pacer._connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (RECLAIM_TABLE,)).fetchone():
                c.rollback()
                return 'ALREADY_UPGRADED'
            c.execute(RECLAIM_SQL)
            for sql in RECLAIM_GUARDS.values(): c.execute(sql)
            c.commit()
    except sqlite3.Error:
        raise PacingError('PACING_DATABASE_BUSY') from None
    Pacer(path)                                   # the result must validate under the strict schema check
    return 'UPGRADED'


def _policy_entry(policy_id):
    reviewed, sha = _reviewed()
    entries = list(reviewed.values())                      # file order: the last entry is the newest
    if policy_id is None:
        return entries[-1], sha
    for entry in entries:
        if entry['id'] == policy_id: return entry, sha
    raise PacingError('PACING_POLICY_UNKNOWN')


def _policy_diff(c, entry):
    changes, unchanged = [], []
    for provider, values in entry['providers'].items():
        old, new = tuple(_effective(c, provider)), (float(values['cadence']), float(values['backoff']))
        item = {'provider': provider, 'old_cadence': old[0], 'new_cadence': new[0],
                'old_backoff': old[1], 'new_backoff': new[1]}
        (unchanged if old == new else changes).append(item)
    return changes, unchanged


def policy_plan(path, policy_id=None):
    """Read-only: what apply_policy would record. Validates the database and the reviewed policy file first."""
    from .model import digest
    pacer = Pacer(path)
    entry, sha = _policy_entry(policy_id)
    try:
        with closing(pacer._connect()) as c:
            c.execute('BEGIN')
            now = pacer.clock()
            changes, unchanged = _policy_diff(c, entry)
            pending = [r[0] for r in c.execute('SELECT provider FROM state WHERE pending IS NOT NULL ORDER BY provider')]
            waiters = c.execute('SELECT COUNT(*) FROM waiters WHERE expires>?', (now,)).fetchone()[0]
            present = bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (POLICY_TABLE,)).fetchone())
            return {'status': 'PLAN', 'policy_id': entry['id'], 'reason': entry['reason'], 'config_sha256': sha,
                    'policy_digest': digest(entry), 'table_present': present, 'changes': changes, 'unchanged': unchanged,
                    'kraken': 'UNTOUCHED', 'pending_providers': pending, 'live_waiters': waiters,
                    'quiescent_database': not pending and not waiters,
                    'effective_now': {r: list(_effective(c, r)) for r in pacer.providers if r != 'kraken'}}
    except sqlite3.Error:
        raise PacingError('PACING_DATABASE_BUSY') from None


def apply_policy(path, policy_id=None, *, clock=time.time):
    """Record the reviewed cadence change(s) append-only; idempotent; the original policy rows are never edited.

    Refuses unless no grant is pending and no live waiter exists (the caller must have stopped every writer; see
    tools.ops.pacing_policy). Adds the `pacing_policy_changes` table and its guards the first time, then one row per
    provider whose effective value differs from the reviewed policy. The Kraken row, state and migration receipt are
    not touched. The change chain is validated inside the transaction before the commit (a failure rolls back), and the whole
    database is validated again afterwards.
    """
    from .model import digest
    pacer = Pacer(path, clock=clock)
    entry, sha = _policy_entry(policy_id)
    try:
        with closing(pacer._connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                now = pacer._now(c)
                if c.execute('SELECT 1 FROM state WHERE pending IS NOT NULL').fetchone() \
                        or c.execute('SELECT 1 FROM waiters WHERE expires>?', (now,)).fetchone():
                    raise PacingError('PACING_NOT_QUIESCENT')
                present = bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (POLICY_TABLE,)).fetchone())
                changes, unchanged = _policy_diff(c, entry)
                if not changes:
                    c.rollback()
                    return {'status': 'ALREADY_APPLIED' if present else 'NOTHING_TO_DO', 'policy_id': entry['id'],
                            'changes': [], 'unchanged': unchanged}
                if not present:
                    c.execute(POLICY_SQL)
                    for sql in POLICY_GUARDS.values(): c.execute(sql)
                last = c.execute(f'SELECT COALESCE(MAX(id),0) FROM {POLICY_TABLE}').fetchone()[0]
                for offset, item in enumerate(changes, 1):
                    c.execute(f'INSERT INTO {POLICY_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?)',
                              (last + offset, item['provider'], item['old_cadence'], item['new_cadence'], item['old_backoff'],
                               item['new_backoff'], entry['reason'], sha, digest(entry), float(now)))
                original = {r[0]: (r[1], r[2]) for r in c.execute("SELECT provider,cadence,backoff FROM policy WHERE provider!='kraken'")}
                try: Pacer._validate_policy_changes(c, original)       # the chain must validate BEFORE it is committed
                except ValueError: raise PacingError('PACING_POLICY_INVALID') from None
                c.commit()
            except BaseException:
                if c.in_transaction: c.rollback()
                raise
    except sqlite3.Error:
        raise PacingError('PACING_DATABASE_BUSY') from None
    Pacer(path, clock=clock)                      # ... and the whole database must still validate afterwards
    return {'status': 'APPLIED', 'policy_id': entry['id'], 'config_sha256': sha, 'policy_digest': digest(entry),
            'changes': changes, 'unchanged': unchanged}


def configured(*, priority='investigation'):
    # Presence is explicit activation. Empty/invalid/missing configured DB refuses;
    # no constructor initializes/replaces policy or resets existing cadence.
    if ENV not in os.environ: return None
    value=os.environ[ENV]
    if type(value) is not str or not 1<=len(value)<=4096:
        raise PacingError('PACING_DATABASE_INVALID')
    return Pacer(value,priority=priority)


def should_throttle(status, headers):
    return status == 429 or (type(status) is int and 400 <= status <= 599
                            and headers is not None and headers.get('Retry-After') is not None)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--initialize')
    g.add_argument('--upgrade',help='add the append-only pacing_reclaims table to an existing database (idempotent)')
    p.add_argument('--helius-seconds',type=float,default=2)
    p.add_argument('--jupiter-seconds',type=float,default=2)
    p.add_argument('--backoff-seconds',type=float,default=30)
    a=p.parse_args(argv)
    if a.upgrade:
        try:print('PACING_'+upgrade(a.upgrade));return 0
        except (PacingError,OSError,sqlite3.Error):
            print('PACING_UPGRADE_FAILED');return 2
    try:initialize(a.initialize,helius_seconds=a.helius_seconds,jupiter_seconds=a.jupiter_seconds,backoff_seconds=a.backoff_seconds)
    except (PacingError,OSError,sqlite3.Error):
        print('PACING_INITIALIZATION_FAILED');return 2
    print('PACING_PROVISIONED_NOT_ACTIVATED');return 0


if __name__=='__main__':raise SystemExit(main())
