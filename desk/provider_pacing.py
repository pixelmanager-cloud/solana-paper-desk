"""Explicit shared provider cadence; no request budgets, retries or credentials.

Provision one protected DB and DESK_PROVIDER_PACING_DB in every participating
process. Wall clock is a trusted host input; rollback refuses. A granted slot is
never refunded after process death. Waiters expire within five seconds; held
requests precede investigations that have not already received a slot.
"""
import argparse
from contextlib import closing
from email.utils import parsedate_to_datetime
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
                           blocked_until REAL NOT NULL,high_water REAL NOT NULL);
        CREATE TABLE waiters(ticket TEXT PRIMARY KEY,provider TEXT NOT NULL,
                             priority TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL);
        ''')
        for provider, cadence in zip(PROVIDERS, (helius_seconds, jupiter_seconds)):
            c.execute('INSERT INTO policy VALUES(?,1,?,?)', (provider, cadence, backoff_seconds))
            c.execute('INSERT INTO state VALUES(?,0,0,0)', (provider,))
        c.commit()


class Pacer:
    def __init__(self, path, *, priority='investigation', clock=time.time,
                 monotonic=time.monotonic, sleep=time.sleep):
        if priority not in ('held', 'investigation'):
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
                if tables != {'policy', 'state', 'waiters'}: raise ValueError()
                rows = c.execute('SELECT provider,version,cadence,backoff FROM policy').fetchall()
                if len(rows) != 2 or {r[0] for r in rows} != set(PROVIDERS): raise ValueError()
                for provider, version, cadence, backoff in rows:
                    if version != 1 or not _number(cadence) or not .05 <= cadence <= 60 or not _number(backoff) or not 1 <= backoff <= 3600: raise ValueError()
                rows = c.execute('SELECT provider,next_at,blocked_until,high_water FROM state').fetchall()
                if len(rows) != 2 or {r[0] for r in rows} != set(PROVIDERS): raise ValueError()
                if any(not _number(v) for r in rows for v in r[1:]): raise ValueError()
                waiters = c.execute('SELECT ticket,provider,priority,created,expires FROM waiters LIMIT 65').fetchall()
                if len(waiters) > 2*MAX_WAITERS: raise ValueError()
                for ticket, provider, priority, created, expires in waiters:
                    if (type(ticket) is not str or len(ticket) != 32 or provider not in PROVIDERS
                            or priority not in ('held','investigation') or not _number(created)
                            or not _number(expires) or not 0 < expires-created <= MAX_WAIT): raise ValueError()
        except (sqlite3.Error, ValueError, TypeError):
            raise PacingError('PACING_DATABASE_INVALID') from None

    def _now(self, c):
        now = self.clock()
        if not _number(now) or now >= 2**40 or now < c.execute('SELECT MAX(high_water) FROM state').fetchone()[0]:
            raise PacingError('PACING_CLOCK_INVALID')
        return now

    def acquire(self, provider, *, timeout_seconds):
        if provider not in PROVIDERS or not _number(timeout_seconds) or timeout_seconds <= 0:
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
                    now = self._now(c)
                    c.execute('DELETE FROM waiters WHERE expires<=?', (now,))
                    if not registered:
                        if c.execute('SELECT COUNT(*) FROM waiters WHERE provider=?',(provider,)).fetchone()[0] >= MAX_WAITERS:
                            raise PacingError('PACING_QUEUE_FULL')
                        c.execute('INSERT INTO waiters VALUES(?,?,?,?,?)',(ticket,provider,self.priority,now,now+duration))
                        registered = True
                    row = c.execute('SELECT next_at,blocked_until FROM state WHERE provider=?',(provider,)).fetchone()
                    first = c.execute("SELECT ticket FROM waiters WHERE provider=? ORDER BY CASE priority WHEN 'held' THEN 0 ELSE 1 END,created,ticket LIMIT 1",(provider,)).fetchone()
                    due = max(row)
                    c.execute('UPDATE state SET high_water=? WHERE provider=?',(now,provider))
                    if first and first[0] == ticket and now >= due:
                        cadence = c.execute('SELECT cadence FROM policy WHERE provider=?',(provider,)).fetchone()[0]
                        c.execute('UPDATE state SET next_at=? WHERE provider=?',(math.nextafter(now+cadence, math.inf),provider))
                        c.execute('DELETE FROM waiters WHERE ticket=?',(ticket,));c.commit()
                        return
                    c.commit()
                self.sleep(min(.05, max(0,deadline-tick)))
        except sqlite3.Error:
            raise PacingError('PACING_DATABASE_BUSY') from None
        finally:
            if registered:
                try:
                    with closing(self._connect()) as c: c.execute('DELETE FROM waiters WHERE ticket=?',(ticket,))
                except (sqlite3.Error, PacingError): pass  # Durable expiry still bounds abandoned waiters.

    def throttle(self, provider, headers):
        """Shared429 embargo; missing/ambiguous Retry-After uses fixed backoff.

        Retry-After is bounded input, never logged. A valid long delay is honored,
        not clamped down; an unrepresentable delay fails closed indefinitely.
        """
        if provider not in PROVIDERS: raise PacingError('PACING_REQUEST_INVALID')
        try:
            with closing(self._connect()) as c:
                c.execute('BEGIN IMMEDIATE');now=self._now(c)
                fallback=c.execute('SELECT backoff FROM policy WHERE provider=?',(provider,)).fetchone()[0]
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
                c.execute('UPDATE state SET blocked_until=MAX(blocked_until,?),high_water=? WHERE provider=?',(until,now,provider));c.commit()
        except sqlite3.Error:
            raise PacingError('PACING_DATABASE_BUSY') from None


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
    p.add_argument('--initialize',required=True)
    p.add_argument('--helius-seconds',type=float,default=2)
    p.add_argument('--jupiter-seconds',type=float,default=2)
    p.add_argument('--backoff-seconds',type=float,default=30)
    a=p.parse_args(argv)
    try:initialize(a.initialize,helius_seconds=a.helius_seconds,jupiter_seconds=a.jupiter_seconds,backoff_seconds=a.backoff_seconds)
    except (PacingError,OSError,sqlite3.Error):
        print('PACING_INITIALIZATION_FAILED');return 2
    print('PACING_PROVISIONED_NOT_ACTIVATED');return 0


if __name__=='__main__':raise SystemExit(main())
