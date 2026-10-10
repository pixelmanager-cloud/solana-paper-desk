"""SYNTHETIC_TEST_ONLY: orphaned provider-pacing slot reclaim (T23/F6). Real flock, real SIGKILL."""
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

    def rows(self, sql):
        with sqlite3.connect(self.path) as c: return c.execute(sql).fetchall()

    def spawn_holder(self, provider='helius'):
        """A separate process that acquires a slot (fake clock T0) and then waits to be killed."""
        code = textwrap.dedent(f'''
            import sys, time
            from desk import provider_pacing as p
            c = type("C", (), {{"time": lambda s: {T0}, "sleep": lambda s, d: None}})()
            pacer = p.Pacer({str(self.path)!r}, clock=c.time, monotonic=c.time, sleep=c.sleep)
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
        ticket = self.make().acquire('helius', timeout_seconds=1)
        self.assertEqual(len(ticket), 32)
        (record,) = self.rows('SELECT provider,ticket,granted_at,reclaimed_at,backoff_until,reason FROM pacing_reclaims')
        self.assertEqual((record[0], record[5]), ('helius', 'OWNER_GONE'))
        self.assertAlmostEqual(record[2], T0, delta=3)          # derived grant time
        self.assertAlmostEqual(record[4], record[2] + 30.0, delta=1e-6)   # unknown outcome = fixed-backoff throttle at grant
        self.assertEqual(record[3], T0 + 3600)
        self.make()                                   # a DB carrying reclaim rows still validates
        # cadence state never rewound: next_at only moved forward by the NEW grant
        after = self.rows('SELECT next_at,blocked_until,high_water FROM state WHERE provider="helius"')[0]
        self.assertGreater(after[0], before[0]); self.assertGreaterEqual(after[1], record[4]); self.assertGreaterEqual(after[2], before[2])
        with sqlite3.connect(self.path) as c:
            for sql in ('UPDATE pacing_reclaims SET reason="x"', 'DELETE FROM pacing_reclaims'):
                with self.assertRaisesRegex(sqlite3.DatabaseError, 'Immutable pacing reclaim'): c.execute(sql)

    def test_live_owner_is_never_reclaimed_however_old_the_slot(self):
        self.spawn_holder()
        self.clock.wall = T0 + 10 * 86400
        with self.assertRaisesRegex(p.PacingError, 'OUTCOME_PENDING'):
            self.make().acquire('helius', timeout_seconds=1)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM sqlite_master WHERE name="pacing_reclaims"'), [(0,)])

    def test_owner_gone_but_slot_not_yet_stale_is_not_reclaimed(self):
        proc = self.spawn_holder(); proc.send_signal(signal.SIGKILL); proc.wait()
        self.clock.wall = T0 + p.RECLAIM_MIN_SECONDS - 1
        with self.assertRaisesRegex(p.PacingError, 'OUTCOME_PENDING'):
            self.make().acquire('helius', timeout_seconds=1)
        self.clock.wall = T0 + p.RECLAIM_MIN_SECONDS + 1
        self.make().acquire('helius', timeout_seconds=1)

    def test_normal_finish_releases_holder_and_never_writes_a_reclaim_row(self):
        pacer = self.make()
        ticket = pacer.acquire('helius', timeout_seconds=1)
        self.assertIn('helius', pacer._held)
        pacer.finish('helius', ticket)
        self.assertNotIn('helius', pacer._held)
        t2 = pacer.acquire('helius', timeout_seconds=5)
        pacer.throttle('helius', None, ticket=t2)
        self.assertNotIn('helius', pacer._held)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM sqlite_master WHERE name="pacing_reclaims"'), [(0,)])

    def test_reclaim_is_per_provider_and_exactly_once_per_ticket(self):
        orphan = self.make()
        orphan.acquire('jupiter', timeout_seconds=1)
        del orphan                                                  # instance gone => holder lock closed
        self.clock.wall = T0 + 600
        other = self.make()
        ticket = other.acquire('helius', timeout_seconds=1)         # unrelated provider unaffected
        other.finish('helius', ticket)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM sqlite_master WHERE name="pacing_reclaims"'), [(0,)])
        ticket = other.acquire('jupiter', timeout_seconds=1)        # reclaims the orphan, then grants
        other.finish('jupiter', ticket)
        self.clock.wall += 10
        other.finish('jupiter', other.acquire('jupiter', timeout_seconds=1))
        self.assertEqual(self.rows('SELECT provider FROM pacing_reclaims'), [('jupiter',)])

    def test_tampered_reclaim_table_fails_closed(self):
        proc = self.spawn_holder(); proc.send_signal(signal.SIGKILL); proc.wait()
        self.clock.wall = T0 + 3600
        self.make().acquire('helius', timeout_seconds=1)
        with sqlite3.connect(self.path) as c:
            c.execute('DROP TRIGGER pacing_reclaims_no_delete')
        with self.assertRaisesRegex(p.PacingError, 'DATABASE_INVALID'):
            self.make()

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


if __name__ == '__main__':
    unittest.main()
