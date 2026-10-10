"""SYNTHETIC_TEST_ONLY: orphaned provider-pacing slot reclaim (T23/F6, T23F). Real flock, real SIGKILL.

T23F: "owner gone" means the PROCESS is gone. A discarded Pacer object (or `del`) is still a live owner, so orphans
are created by a real subprocess that is SIGKILLed; the reclaim table exists only after the explicit upgrade().
"""
import gc
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from desk import provider_pacing as p

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_000_000.0


class Clock:
    def __init__(self, wall=T0): self.wall = wall
    def time(self): return self.wall
    def sleep(self, d): self.wall += d


class PacingReclaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve() / 'pace.sqlite'
        p.initialize(self.path)
        self.assertEqual(p.upgrade(self.path), 'UPGRADED')          # explicit, never a side effect of a reclaim
        self.clock = Clock()
        self.children = []
        self.addCleanup(self.reap)

    def reap(self):
        for c in self.children:
            if c.poll() is None: c.kill()
            c.wait(); c.stdout.close()

    def make(self, clock=None):
        clock = clock or self.clock
        return p.Pacer(self.path, clock=clock.time, monotonic=clock.time, sleep=clock.sleep)

    def after_embargo(self, pacer, provider='helius'):
        """A reclaim embargoes the provider for the fixed backoff: first attempt times out, then it works."""
        with self.assertRaisesRegex(p.PacingError, 'DEADLINE_EXCEEDED'):
            pacer.acquire(provider, timeout_seconds=1)
        self.clock.wall += 31
        return pacer.acquire(provider, timeout_seconds=1)

    def rows(self, sql):
        with sqlite3.connect(self.path) as c: return c.execute(sql).fetchall()

    def killed_orphan(self, provider='helius'):
        proc = self.spawn_holder(provider)
        proc.send_signal(signal.SIGKILL); proc.wait()
        return proc

    def spawn_holder(self, provider='helius', path=None):
        """A separate process that acquires a slot (fake clock T0) and then waits to be killed."""
        code = textwrap.dedent(f'''
            import sys, time
            from desk import provider_pacing as p
            c = type("C", (), {{"time": lambda s: {T0}, "sleep": lambda s, d: None}})()
            pacer = p.Pacer({str(path or self.path)!r}, clock=c.time, monotonic=c.time, sleep=c.sleep)
            pacer.acquire({provider!r}, timeout_seconds=1)
            print("HELD", flush=True); time.sleep(600)
            ''')
        proc = subprocess.Popen([sys.executable, '-c', code], cwd=ROOT, stdout=subprocess.PIPE, text=True,
                                env={**os.environ, 'PYTHONPATH': str(ROOT)})
        self.children.append(proc)
        self.assertEqual(proc.stdout.readline().strip(), 'HELD')
        return proc

    def test_sigkilled_owner_slot_is_reclaimed_once_stale_with_append_only_record(self):
        proc = self.spawn_holder()
        proc.send_signal(signal.SIGKILL); proc.wait()
        before = self.rows('SELECT next_at,blocked_until,high_water,pending FROM state WHERE provider="helius"')[0]
        self.assertIsNotNone(before[3])
        self.clock.wall = T0 + 3600
        ticket = self.after_embargo(self.make())
        self.assertEqual(len(ticket), 32)
        (record,) = self.rows('SELECT provider,ticket,granted_at,reclaimed_at,backoff_until,reason FROM pacing_reclaims')
        self.assertEqual((record[0], record[5]), ('helius', 'OWNER_GONE'))
        self.assertAlmostEqual(record[2], T0, delta=3)          # derived grant time
        self.assertEqual(record[4], record[3] + 30.0)   # unknown outcome = fixed-backoff throttle from the reclaim
        self.assertEqual(record[3], T0 + 3600)
        self.assertGreaterEqual(self.rows('SELECT blocked_until FROM state WHERE provider="helius"')[0][0], record[4])
        self.make()                                   # a DB carrying reclaim rows still validates
        # cadence state never rewound: next_at only moved forward by the NEW grant
        after = self.rows('SELECT next_at,blocked_until,high_water FROM state WHERE provider="helius"')[0]
        self.assertGreater(after[0], before[0]); self.assertGreaterEqual(after[2], before[2])
        with sqlite3.connect(self.path) as c:
            for sql in ('UPDATE pacing_reclaims SET reason="x"', 'DELETE FROM pacing_reclaims'):
                with self.assertRaisesRegex(sqlite3.DatabaseError, 'Immutable pacing reclaim'): c.execute(sql)

    def test_live_owner_is_never_reclaimed_however_old_the_slot(self):
        self.spawn_holder()
        self.clock.wall = T0 + 10 * 86400
        with self.assertRaisesRegex(p.PacingError, 'OUTCOME_PENDING'):
            self.make().acquire('helius', timeout_seconds=1)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM pacing_reclaims'), [(0,)])

    def test_owner_gone_but_slot_not_yet_stale_is_not_reclaimed(self):
        proc = self.spawn_holder(); proc.send_signal(signal.SIGKILL); proc.wait()
        self.clock.wall = T0 + p.RECLAIM_MIN_SECONDS - 1
        with self.assertRaisesRegex(p.PacingError, 'OUTCOME_PENDING'):
            self.make().acquire('helius', timeout_seconds=1)
        self.clock.wall = T0 + p.RECLAIM_MIN_SECONDS + 1
        self.after_embargo(self.make())

    def test_normal_finish_releases_holder_and_never_writes_a_reclaim_row(self):
        pacer = self.make()
        ticket = pacer.acquire('helius', timeout_seconds=1)
        self.assertIn('helius', pacer._held)
        pacer.finish('helius', ticket)
        self.assertNotIn('helius', pacer._held)
        t2 = pacer.acquire('helius', timeout_seconds=5)
        pacer.throttle('helius', None, ticket=t2)
        self.assertNotIn('helius', pacer._held)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM pacing_reclaims'), [(0,)])

    def test_reclaim_is_per_provider_and_exactly_once_per_ticket(self):
        self.killed_orphan('jupiter')
        self.clock.wall = T0 + 600
        other = self.make()
        ticket = other.acquire('helius', timeout_seconds=1)         # unrelated provider unaffected
        other.finish('helius', ticket)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM pacing_reclaims'), [(0,)])
        ticket = self.after_embargo(other, 'jupiter')               # reclaims the orphan, embargo, then grants
        other.finish('jupiter', ticket)
        self.clock.wall += 10
        other.finish('jupiter', other.acquire('jupiter', timeout_seconds=1))
        self.assertEqual(self.rows('SELECT provider FROM pacing_reclaims'), [('jupiter',)])

    def test_tampered_reclaim_table_fails_closed(self):
        proc = self.spawn_holder(); proc.send_signal(signal.SIGKILL); proc.wait()
        self.clock.wall = T0 + 3600
        self.after_embargo(self.make())
        with sqlite3.connect(self.path) as c:
            c.execute('DROP TRIGGER pacing_reclaims_no_delete')
        with self.assertRaisesRegex(p.PacingError, 'DATABASE_INVALID'):
            self.make()

    def test_slot_is_released_only_after_the_commit(self):
        # T23G item 1 REVERSES the T23F ordering ("release before commit"): that order let a busy COMMIT fail after
        # the flock was gone, so a live owner's slot looked orphaned. The lock is now released after the outcome is durable.
        pacer = self.make()
        ticket = pacer.acquire('helius', timeout_seconds=1)
        original, seen = pacer._release, []
        def spy(provider, tkt=None):
            seen.append(self.rows('SELECT pending FROM state WHERE provider="helius"')[0][0])   # already committed
            original(provider, tkt)
            fd = os.open(pacer._lock_path(provider), os.O_RDWR)
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); seen.append('lock free')
            finally: os.close(fd)
        pacer._release = spy
        pacer.finish('helius', ticket)
        self.assertEqual(seen, [None, 'lock free'])

    # ---- T23G items 1 and 2: busy commits and contention --------------------------------------------------------
    def reader(self):
        """A connection holding a SHARED lock (DELETE journal mode): a writer's COMMIT then fails as busy."""
        c = sqlite3.connect(self.path, isolation_level=None)
        c.execute('BEGIN'); c.execute('SELECT * FROM state').fetchall()
        self.addCleanup(c.close)
        return c

    def lock_is_held(self, pacer, provider='helius'):
        import fcntl
        fd = os.open(pacer._lock_path(provider), os.O_RDWR)
        try:
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError: return True
            return False
        finally: os.close(fd)

    def test_finish_with_a_busy_commit_keeps_the_owner_lock_and_succeeds_when_the_reader_leaves(self):
        pacer = self.make()
        ticket = pacer.acquire('helius', timeout_seconds=1)
        reader, held_during, pending_during = self.reader(), [], []
        sleeps = []
        def sleep(d):
            sleeps.append(d)
            held_during.append(self.lock_is_held(pacer))
            # the writer holds PENDING after a busy COMMIT, so only the existing reader can still look
            pending_during.append(reader.execute('SELECT pending FROM state WHERE provider="helius"').fetchone()[0] == ticket)
            if len(sleeps) == 2: reader.execute('ROLLBACK')             # the contention ends
        pacer.sleep = sleep
        pacer.finish('helius', ticket)
        self.assertEqual(len(sleeps), 2)
        self.assertEqual((held_during, pending_during), ([True, True], [True, True]))   # never reclaimable meanwhile
        self.assertTrue(sleeps[1] > sleeps[0])                           # bounded, growing backoff
        self.assertIsNone(self.rows('SELECT pending FROM state WHERE provider="helius"')[0][0])
        self.assertFalse(self.lock_is_held(pacer))                       # released only after the commit

    def test_a_commit_that_never_succeeds_raises_busy_but_the_live_owner_is_never_reclaimed(self):
        pacer = self.make()
        ticket = pacer.acquire('helius', timeout_seconds=1)
        reader = self.reader()
        pacer.sleep = lambda d: None
        with self.assertRaisesRegex(p.PacingError, 'DATABASE_BUSY'):
            pacer.finish('helius', ticket)
        self.assertTrue(self.lock_is_held(pacer))                        # kept until process exit
        self.assertIn('helius', pacer._held)
        reader.execute('ROLLBACK')
        self.clock.wall = T0 + 10 * 86400                                # however old the slot is...
        self.assertEqual(self.make().reclaim_orphans(), [])              # ...a live owner is not reclaimed
        self.assertEqual(self.rows('SELECT COUNT(*) FROM pacing_reclaims'), [(0,)])
        pacer.finish('helius', ticket)                                   # and the owner can still finish later
        self.assertIsNone(self.rows('SELECT pending FROM state WHERE provider="helius"')[0][0])
        self.assertFalse(self.lock_is_held(pacer))

    def test_throttle_retry_after_survives_a_busy_commit(self):
        from email.message import Message
        headers = Message(); headers['Retry-After'] = '600'
        pacer = self.make()
        ticket = pacer.acquire('helius', timeout_seconds=1)
        reader, sleeps = self.reader(), []
        def sleep(d):
            sleeps.append(d)
            self.assertTrue(self.lock_is_held(pacer))
            if len(sleeps) == 3: reader.execute('ROLLBACK')
        pacer.sleep = sleep
        pacer.throttle('helius', headers, ticket=ticket)
        blocked, high = self.rows('SELECT blocked_until,high_water FROM state WHERE provider="helius"')[0]
        self.assertGreaterEqual(blocked - high, 600)                     # the full Retry-After, not the 30 s fallback
        self.assertIsNone(self.rows('SELECT pending FROM state WHERE provider="helius"')[0][0])

    def test_a_new_grantee_waits_for_the_previous_owners_post_commit_release(self):
        first = self.make()
        t1 = first.acquire('helius', timeout_seconds=1)
        second = self.make()
        released = []
        real_flock = p.fcntl.flock
        calls = []
        def flock(fd, op):
            calls.append(op)
            if len(calls) == 3 and not released: first._release('helius', t1); released.append(True)
            return real_flock(fd, op)
        first.finish('helius', t1) if False else None
        with sqlite3.connect(self.path) as c:                            # the owner committed its outcome, release is next
            c.execute('UPDATE state SET pending=NULL WHERE provider="helius"')
        self.clock.wall = T0 + 10
        with patch.object(p.fcntl, 'flock', flock):
            ticket = second.acquire('helius', timeout_seconds=5)
        self.assertEqual(len(ticket), 32)
        self.assertTrue(released)

    def test_reclaim_orphans_never_takes_the_write_lock_unless_an_old_pending_row_exists(self):
        writer = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute('BEGIN IMMEDIATE')                                # a held write lock: any write attempt is busy
        pacer = self.make()
        self.assertEqual(pacer.reclaim_orphans(), [])                    # nothing pending: pure read, no error
        writer.execute('ROLLBACK')
        ticket = pacer.acquire('helius', timeout_seconds=1)              # a young live grant
        writer.execute('BEGIN IMMEDIATE')
        self.assertEqual(self.make().reclaim_orphans(), [])              # young: still read-only, no error
        writer.execute('ROLLBACK')
        pacer.finish('helius', ticket)

    def statements(self, pacer, action):
        """SQL issued by reclaim_orphans: a wrapper over the real connection records every execute()."""
        seen = []
        real = pacer._connect

        class Spy:
            def __init__(self, c): self.c = c
            def execute(self, sql, *a): seen.append(sql.split()[0] + ' ' + (sql.split()[1] if len(sql.split()) > 1 else '')); return self.c.execute(sql, *a)
            def __getattr__(self, name): return getattr(self.c, name)
        with patch.object(pacer, '_connect', lambda: Spy(real())):
            result = action()
        return result, seen

    def test_reclaim_orphans_issues_no_write_transaction_for_nothing_pending_or_a_young_grant(self):
        pacer = self.make()
        result, seen = self.statements(pacer, pacer.reclaim_orphans)                  # nothing pending
        self.assertEqual(result, [])
        self.assertFalse([x for x in seen if x.startswith('BEGIN IMMEDIATE')], seen)
        ticket = pacer.acquire('helius', timeout_seconds=1)                           # pending but young
        result, seen = self.statements(pacer, pacer.reclaim_orphans)
        self.assertEqual(result, [])
        self.assertFalse([x for x in seen if x.startswith('BEGIN IMMEDIATE')], seen)
        pacer.finish('helius', ticket)

    def test_reclaim_orphans_takes_the_write_lock_once_an_old_pending_row_exists(self):
        self.killed_orphan()
        self.clock.wall = T0 + 3600                                                    # old and owner gone: now it writes
        other = self.make()
        result, seen = self.statements(other, other.reclaim_orphans)
        self.assertEqual(result, ['helius'])
        self.assertTrue([x for x in seen if x.startswith('BEGIN IMMEDIATE')], seen)

    def test_reclaim_orphans_returns_empty_instead_of_raising_when_the_database_is_busy(self):
        self.killed_orphan()
        self.clock.wall = T0 + 3600                                      # old, owner gone: a write IS needed
        writer = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute('BEGIN IMMEDIATE')
        self.assertEqual(self.make().reclaim_orphans(), [])              # busy: no reclaim this time, never an exception
        self.assertIsNotNone(self.rows('SELECT pending FROM state WHERE provider="helius"')[0][0])
        writer.execute('ROLLBACK')
        self.assertEqual(self.make().reclaim_orphans(), ['helius'])      # the next call reclaims

    def test_holder_lock_failure_fails_closed_without_leaving_an_unprotected_ticket(self):
        pacer = self.make()
        real = p.fcntl.flock
        def flock(fd, op):
            if op & p.fcntl.LOCK_EX: raise OSError('simulated')
            return real(fd, op)
        with patch.object(p.fcntl, 'flock', side_effect=flock):
            with self.assertRaisesRegex(p.PacingError, 'HOLDER_LOCK_UNAVAILABLE'):
                pacer.acquire('helius', timeout_seconds=1)
        self.assertIsNone(self.rows('SELECT pending FROM state WHERE provider="helius"')[0][0])
        self.assertEqual(pacer._held, {})

    def test_reclaim_count_cap_is_enforced_on_insert_and_fails_closed(self):
        for provider in ('helius', 'jupiter'):
            self.killed_orphan(provider)
        self.clock.wall = T0 + 600
        with patch.object(p, 'MAX_RECLAIMS', 1):
            self.after_embargo(self.make(), 'helius')
            with self.assertRaisesRegex(p.PacingError, 'OUTCOME_PENDING'):
                self.make().acquire('jupiter', timeout_seconds=1)
        self.make()      # DB still validates

    def test_unprovable_owner_keeps_the_old_fail_closed_behavior(self):
        proc = self.spawn_holder(); proc.send_signal(signal.SIGKILL); proc.wait()
        self.clock.wall = T0 + 3600
        with patch.object(p.os, 'open', side_effect=OSError('no lock file')):
            with self.assertRaisesRegex(p.PacingError, 'OUTCOME_PENDING'):
                self.make().acquire('helius', timeout_seconds=1)

    def test_two_holders_never_share_a_slot(self):
        a = self.make()
        a.acquire('helius', timeout_seconds=1)
        b = self.make()
        self.clock.wall = T0 + 3600
        with self.assertRaisesRegex(p.PacingError, 'OUTCOME_PENDING'):   # a is alive in this process
            b.acquire('helius', timeout_seconds=1)

    # ---- T23F ---------------------------------------------------------------------------------------------
    def plain_db(self):
        path = Path(self.tmp.name).resolve() / 'plain.sqlite'
        p.initialize(path)
        return path

    def test_without_the_explicit_upgrade_nothing_is_reclaimed_and_no_table_is_created(self):
        plain = self.plain_db()
        proc = self.spawn_holder(path=plain); proc.send_signal(signal.SIGKILL); proc.wait()
        self.clock.wall = T0 + 3600
        pacer = p.Pacer(plain, clock=self.clock.time, monotonic=self.clock.time, sleep=self.clock.sleep)
        with self.assertRaisesRegex(p.PacingError, 'OUTCOME_PENDING'):
            pacer.acquire('helius', timeout_seconds=1)
        self.assertEqual(pacer.reclaim_orphans(), [])
        with sqlite3.connect(plain) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM sqlite_master WHERE name="pacing_reclaims"').fetchone(), (0,))
            self.assertIsNotNone(c.execute('SELECT pending FROM state WHERE provider="helius"').fetchone()[0])
        self.assertEqual(p.upgrade(plain), 'UPGRADED')
        self.assertEqual(pacer.reclaim_orphans() if False else p.Pacer(plain, clock=self.clock.time).reclaim_orphans(), ['helius'])

    def test_upgrade_is_idempotent_touches_no_existing_row_and_refuses_a_tampered_database(self):
        plain = self.plain_db()
        def dump():
            with sqlite3.connect(plain) as c:
                return [c.execute(f'SELECT * FROM {t} ORDER BY 1').fetchall() for t in ('policy', 'state', 'waiters')]
        before = dump()
        self.assertEqual(p.upgrade(plain), 'UPGRADED')
        self.assertEqual(dump(), before)                                  # no row of any existing table changed
        self.assertEqual(p.upgrade(plain), 'ALREADY_UPGRADED')
        self.assertEqual(dump(), before)
        p.Pacer(plain)                                                    # strict validation accepts the new schema
        other = Path(self.tmp.name).resolve() / 'tampered.sqlite'
        p.initialize(other)
        with sqlite3.connect(other) as c: c.execute('CREATE TABLE sneaky(x)')
        with self.assertRaisesRegex(p.PacingError, 'DATABASE_INVALID'):
            p.upgrade(other)
        with sqlite3.connect(other) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM sqlite_master WHERE name="pacing_reclaims"').fetchone(), (0,))
        with self.assertRaisesRegex(p.PacingError, 'DATABASE_INVALID'):
            p.upgrade(Path(self.tmp.name) / 'missing.sqlite')

    def test_cli_upgrade(self):
        plain = self.plain_db()
        with patch('builtins.print') as out:
            self.assertEqual(p.main(['--upgrade', str(plain)]), 0)
            self.assertEqual(p.main(['--upgrade', str(plain)]), 0)
            self.assertEqual(p.main(['--upgrade', str(plain) + '.missing']), 2)
        self.assertEqual([c.args[0] for c in out.call_args_list],
                         ['PACING_UPGRADED', 'PACING_ALREADY_UPGRADED', 'PACING_UPGRADE_FAILED'])

    def test_public_reclaim_orphans_needs_age_and_a_dead_owner_and_is_idempotent(self):
        self.killed_orphan()
        pacer = self.make()
        self.clock.wall = T0 + p.RECLAIM_MIN_SECONDS - 1
        self.assertEqual(pacer.reclaim_orphans(), [])                      # dead owner, too young
        self.clock.wall = T0 + 3600
        self.assertEqual(pacer.reclaim_orphans(), ['helius'])
        self.assertEqual(pacer.reclaim_orphans(), [])                      # exactly once per ticket
        self.assertEqual(self.rows('SELECT provider,reason FROM pacing_reclaims'), [('helius', 'OWNER_GONE')])
        self.assertIsNone(self.rows('SELECT pending FROM state WHERE provider="helius"')[0][0])
        self.after_embargo(pacer)                                          # the unknown-outcome embargo still applies

    def test_public_reclaim_orphans_never_touches_a_live_owner(self):
        self.spawn_holder()
        self.clock.wall = T0 + 10 * 86400
        self.assertEqual(self.make().reclaim_orphans(), [])
        self.assertIsNotNone(self.rows('SELECT pending FROM state WHERE provider="helius"')[0][0])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM pacing_reclaims'), [(0,)])

    def test_a_garbage_collected_pacer_in_a_live_process_is_still_the_owner(self):
        # paper_read_sources keeps a ticket pending (pacing_release=False) while its Pacer is discarded.
        pacer = self.make()
        ticket = pacer.acquire('helius', timeout_seconds=1)
        del pacer; gc.collect()
        self.clock.wall = T0 + 86400
        self.assertEqual(self.make().reclaim_orphans(), [])
        with self.assertRaisesRegex(p.PacingError, 'OUTCOME_PENDING'):
            self.make().acquire('helius', timeout_seconds=1)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM pacing_reclaims'), [(0,)])
        self.make().finish('helius', ticket)                               # any Pacer in the process may acknowledge it
        self.assertEqual([k for k in p._HELD if k[0] == str(self.path)], [])   # the registry is process-wide; mine only
        self.make().acquire('helius', timeout_seconds=1)

    def test_reclaim_never_lowers_a_stored_embargo_or_moves_the_cadence(self):
        self.killed_orphan()
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE state SET blocked_until=? WHERE provider="helius"', (T0 + 100000,))
        before = self.rows('SELECT next_at FROM state WHERE provider="helius"')[0][0]
        policy = self.rows('SELECT * FROM policy ORDER BY provider')
        self.clock.wall = T0 + 3600
        self.assertEqual(self.make().reclaim_orphans(), ['helius'])
        (blocked, next_at) = self.rows('SELECT blocked_until,next_at FROM state WHERE provider="helius"')[0]
        self.assertEqual(blocked, T0 + 100000)                              # the longer Retry-After wins over the fixed 30 s
        self.assertEqual(next_at, before)
        self.assertEqual(self.rows('SELECT * FROM policy ORDER BY provider'), policy)
        self.clock.wall = T0 + 50000
        with self.assertRaisesRegex(p.PacingError, 'DEADLINE_EXCEEDED'):
            self.make().acquire('helius', timeout_seconds=1)

    def test_the_grant_is_not_visible_until_the_owner_lock_is_held(self):
        pacer = self.make()
        seen = []
        real = pacer._hold
        def spy(provider, ticket):
            seen.append(self.rows('SELECT pending FROM state WHERE provider=?'.replace('?', repr(provider)))[0][0])
            real(provider, ticket)
            seen.append(provider in pacer._held)
        pacer._hold = spy
        ticket = pacer.acquire('helius', timeout_seconds=1)
        self.assertEqual(seen, [None, True])                                # nothing committed before the lock existed
        self.assertEqual(self.rows('SELECT pending FROM state WHERE provider="helius"')[0][0], ticket)


class SequenceClock:
    """A wall clock that replays a list of readings (the last one repeats)."""
    def __init__(self, *readings): self.readings = list(readings); self.calls = 0
    def time(self):
        value = self.readings[min(self.calls, len(self.readings) - 1)]; self.calls += 1
        return value
    def sleep(self, d): pass


class ReclaimClockTests(PacingReclaimTests):
    """T23H: a clock problem inside reclaim must never raise on the gate's read-first path.

    `reclaim_orphans` is called by the entry gates before they test `pending`; they refuse on a pending grant without ever
    calling acquire(). A clock behind `high_water` (or not a usable number) is already refused by acquire() with
    PACING_CLOCK_INVALID; reclaim just does nothing, so a gate cannot be turned into a crash by a clock that disagrees with the
    one that advanced the database (this crashed tests.test_empty_history_successor_lifecycle on integration/r1).
    """

    def snapshot(self):
        return (self.rows('SELECT provider,next_at,blocked_until,high_water,pending FROM state ORDER BY provider'),
                self.rows('SELECT * FROM pacing_reclaims'), self.path.read_bytes())

    BAD_CLOCKS = {'behind high_water': T0 - 50, 'nan': float('nan'), 'inf': float('inf'), 'too large': 2.0 ** 41, 'none': None,
                  'bool': True, 'string': '1000000'}

    def test_a_clock_that_disagrees_with_the_database_returns_empty_and_changes_nothing(self):
        self.killed_orphan()
        before = self.snapshot()
        for label, reading in self.BAD_CLOCKS.items():
            with self.subTest(label):
                pacer = self.make(Clock(reading))
                self.assertEqual(pacer.reclaim_orphans(), [])
                self.assertEqual(self.snapshot(), before)             # no reclaim row, orphan intact, file bytes identical

    def test_acquire_still_fails_closed_with_the_same_clock(self):
        self.killed_orphan()
        for label, reading in self.BAD_CLOCKS.items():
            with self.subTest(label), self.assertRaisesRegex(p.PacingError, 'PACING_CLOCK_INVALID|DEADLINE'):
                self.make(Clock(reading)).acquire('helius', timeout_seconds=1)

    def test_a_valid_clock_reclaims_afterwards_so_the_failed_attempt_damaged_nothing(self):
        self.killed_orphan()
        self.assertEqual(self.make(Clock(T0 - 50)).reclaim_orphans(), [])
        self.assertEqual(self.make(Clock(T0 + 100)).reclaim_orphans(), ['helius'])
        self.assertEqual(len(self.rows('SELECT * FROM pacing_reclaims')), 1)

    def test_a_clock_that_goes_bad_between_the_read_and_the_write_pass_also_returns_empty(self):
        self.killed_orphan()
        before = self.snapshot()
        clock = SequenceClock(T0 + 100, T0 - 5)                       # valid for the read-first pass, behind for the write pass
        self.assertEqual(self.make(clock).reclaim_orphans(), [])
        self.assertEqual(clock.calls, 2)
        self.assertEqual(self.snapshot(), before)

    def test_reclaim_without_an_orphan_and_a_bad_clock_is_also_quiet(self):
        self.assertEqual(self.make(Clock(T0 - 50)).reclaim_orphans(), [])
        self.assertEqual(self.make(Clock(float('nan'))).reclaim_orphans(), [])

    def test_only_a_clock_problem_is_swallowed_an_integrity_error_still_raises(self):
        self.killed_orphan()
        pacer = self.make(Clock(T0 + 100))
        for code in ('PACING_DATABASE_INVALID', 'PACING_DATABASE_CHANGED', 'PACING_POLICY_INVALID'):
            with self.subTest(code), patch.object(p.Pacer, '_now', side_effect=p.PacingError(code)):
                with self.assertRaisesRegex(p.PacingError, code):
                    pacer.reclaim_orphans()                        # fail closed: never hidden as "nothing to reclaim"

    def test_the_gate_refuses_on_the_pending_grant_not_on_the_clock(self):
        from desk import paper_terminal_reconciliation as terminal
        self.killed_orphan()
        behind, real = Clock(T0 - 50), p.Pacer
        with patch.object(terminal.provider_pacing, 'Pacer', lambda path: real(path, clock=behind.time)):
            with self.assertRaisesRegex(ValueError, 'Provider outcome pending'):
                terminal._pacing(self.path)                            # the pre-existing fail-closed refusal, not PacingError

    def test_the_gate_passes_a_clean_database_even_when_the_clock_disagrees(self):
        from desk import paper_terminal_reconciliation as terminal
        real = p.Pacer
        with patch.object(terminal.provider_pacing, 'Pacer', lambda path: real(path, clock=Clock(T0 - 50).time)):
            terminal._pacing(self.path)                                # nothing pending: acquire() will refuse the bad clock later


for _name in dir(PacingReclaimTests):                      # reuse setUp/helpers only; do not re-run the parent's tests
    if _name.startswith('test_'):
        setattr(ReclaimClockTests, _name, None)


if __name__ == '__main__':
    unittest.main()
