"""Audit B (budgets, pacing, timing, locks) for a fresh-store first 48h.

Fixture-only: no network, no provider keys. Tests marked expectedFailure
document a concrete operational hazard that is RED against current code.
"""
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from desk import provider_pacing as p

ROOT = Path(__file__).resolve().parents[1]


def unit(name):
    out = {}
    for line in (ROOT / 'deploy' / name).read_text().splitlines():
        m = re.match(r'([A-Za-z]+)=(.*)', line)
        if m:
            out.setdefault(m.group(1), m.group(2))
    return out


class Clock:
    def __init__(self, wall=1_000_000.):
        self.wall = wall
    def time(self): return self.wall
    def sleep(self, d): self.wall += d


class FreshPacingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'pace.sqlite'
        p.initialize(self.path)
        self.clock = Clock()

    def make(self):
        return p.Pacer(self.path, clock=self.clock.time, monotonic=self.clock.time, sleep=self.clock.sleep)

    @unittest.expectedFailure
    def test_fresh_pacing_db_can_receive_reviewed_kraken_migration(self):
        """A freshly initialize()d pacing DB must be able to gain the kraken
        row (dispatcher plan() and Kraken reads REQUIRE it). The reviewed pin
        in config/kraken-pacing-migration.json hardcodes the production path
        /var/lib/solana-desk/provider-pacing.sqlite, the exact production
        old_state floats and source_hash 77de75a2..., so a fresh DB (state
        0,0,0,0) is refused with 'not reviewed' => KRAKEN_PACING_NOT_CONFIGURED
        / dispatcher never plans. Any desk/ source change (e.g. T01) also
        changes review_plan()['source_hash'] for a not-yet-migrated DB."""
        from desk import kraken_pacing_migration as k
        self.assertEqual(k.migrate(self.path)['status'], 'MIGRATED')

    def test_slot_granted_to_killed_process_is_reclaimed_eventually(self):
        """Process acquires a slot (pending=ticket) then is SIGKILLed by
        systemd TimeoutStartSec before finish()/throttle() (most likely
        exactly when a provider is slow, i.e. during the HTTP call). pending
        has no TTL and no clear path: 24h later every acquire() still raises
        PACING_OUTCOME_PENDING and dispatcher _preflight() refuses forever."""
        self.make().acquire('helius', timeout_seconds=1)   # never finished
        self.clock.wall += 86400
        pacer = self.make()
        with self.assertRaisesRegex(p.PacingError, 'DEADLINE_EXCEEDED'):   # T23: reclaim embargoes the fixed backoff
            pacer.acquire('helius', timeout_seconds=1)
        self.clock.wall += 31
        pacer.acquire('helius', timeout_seconds=1)

    def test_orphaned_pending_blocks_every_provider_call_today(self):
        """GREEN characterisation: the block is real while the owner is provably alive (T23 reclaims
        only owner-gone slots; the grant is held by `pacer` so its lock stays open)."""
        pacer = self.make(); pacer.acquire('helius', timeout_seconds=1)
        self.clock.wall += 86400
        with self.assertRaisesRegex(p.PacingError, 'OUTCOME_PENDING'):
            self.make().acquire('helius', timeout_seconds=1)
        with sqlite3.connect(self.path) as c:   # dispatcher _preflight predicate
            self.assertIsNotNone(c.execute('SELECT 1 FROM state WHERE pending IS NOT NULL').fetchone())

    @unittest.expectedFailure
    def test_ambiguous_retry_after_does_not_embargo_provider_forever(self):
        """One 429/503 carrying two Retry-After headers (or an absurd value)
        sets blocked_until=2**53-1; no code path ever lowers it, so helius
        (or kraken/jupiter) is unusable until the coordinator hand-edits the
        DB, which the docs forbid ('never clear unresolved pacing')."""
        pacer = self.make()
        ticket = pacer.acquire('helius', timeout_seconds=1)
        h = Message(); h['Retry-After'] = '1'; h['Retry-After'] = '2'
        pacer.throttle('helius', h, ticket=ticket)
        self.clock.wall += 86400
        self.make().acquire('helius', timeout_seconds=1)


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); os.chmod(self.root, 0o700)
        self.research = self.root / 'research.sqlite'; self.research.touch()
        self.lock = self.root / 'paper-scheduler.lock'; self.lock.touch(mode=0o600); self.lock.chmod(0o600)
        i = self.lock.stat(); self.identity = f'{i.st_dev}:{i.st_ino}'

    def test_sigkilled_holder_releases_lease(self):
        """GREEN: flock is released by the kernel on SIGKILL; next tick runs."""
        code = textwrap.dedent('''
            import sys,time
            from desk.paper_scheduler import lease
            with lease(sys.argv[1]) as fd:
                print("held" if fd is not None else "busy",flush=True)
                time.sleep(60)
        ''')
        env = dict(os.environ, DESK_PAPER_SCHEDULER_IDENTITY=self.identity, PYTHONPATH=str(ROOT))
        child = subprocess.Popen([sys.executable, '-c', code, str(self.research)], stdout=subprocess.PIPE, env=env, cwd=ROOT)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), b'held')
        from desk.paper_scheduler import lease
        with patch.dict(os.environ, {'DESK_PAPER_SCHEDULER_IDENTITY': self.identity}):
            with lease(self.research) as fd:
                self.assertIsNone(fd)            # busy while child alive
            child.send_signal(signal.SIGKILL); child.wait()
            with lease(self.research) as fd:
                self.assertIsNotNone(fd)

    def test_fresh_store_lease_requires_preexisting_lock_and_identity_env(self):
        """GREEN: on a fresh data dir the lease raises (not SCHEDULER_BUSY)
        unless the 0600 lock file was pre-created AND its dev:ino exported."""
        from desk.paper_scheduler import lease
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                with lease(self.research): pass
        self.lock.unlink()
        with patch.dict(os.environ, {'DESK_PAPER_SCHEDULER_IDENTITY': '1:1'}):
            with self.assertRaises(FileNotFoundError):
                with lease(self.research): pass

    @unittest.expectedFailure
    def test_scheduler_units_provide_lease_identity(self):
        """Every unit that runs tools.paper_scheduler must export
        DESK_PAPER_SCHEDULER_IDENTITY (dev:ino of the pre-created lock) or
        every tick dies with ValueError before doing anything. No repo unit
        sets it (only the dashboard sets the unrelated _LOCK variable)."""
        for name in ('desk-paper-entry-dispatcher.service', 'desk-paper-held-cycle.service',
                     'desk-paper-monitor.service', 'desk-decisions.service'):
            self.assertIn('DESK_PAPER_SCHEDULER_IDENTITY', (ROOT / 'deploy' / name).read_text(), name)


class UnitBudgetTests(unittest.TestCase):
    @unittest.expectedFailure
    def test_entry_wall_timeout_covers_code_time_budgets(self):
        """Code budgets: up to 18 investigation requests, each allowed up to
        15s transport (paper_read_sources._attempt: 0<timeout<=15) plus pacing
        (<=5s wait each, 2s cadence), then time.sleep(2.0) and a fresh 10s
        cycle. Worst case >= 18*15+2+10 = 282s. Repo unit says 180s, so a slow
        provider => SIGTERM/SIGKILL after intent written (permanent
        'Unresolved dispatch') and possibly a pacing pending row. Coordinator
        says prod is 600s; the repo template must not be shipped as-is."""
        worst = 18 * 15 + 2 + 10
        self.assertGreaterEqual(int(unit('desk-paper-entry-dispatcher.service')['TimeoutStartSec']), worst)

    @unittest.expectedFailure
    def test_held_wall_timeout_covers_cycle_plus_accounting(self):
        """Held cycle has a hard 10s monotonic budget (_Budget.remaining) plus
        export_targets + terminal.gate + accounting verification outside it.
        Repo unit TimeoutStartSec=20 leaves 10s slack, while accounting is
        O(total reservations) (see MonitoringScalingTests). Coordinator says
        prod is 120s; repo template is 20s."""
        self.assertGreaterEqual(int(unit('desk-paper-held-cycle.service')['TimeoutStartSec']), 120)

    def test_timer_cadences_never_self_overlap_but_share_one_lease(self):
        """GREEN: entry/held use OnUnitInactiveSec so a slow run cannot stack;
        Persistent=false so no catch-up burst after downtime. All four modes
        contend on one non-blocking flock, so SCHEDULER_BUSY (exit 0, no
        retry) silently skips a tick: a skipped held tick = 10 min gap."""
        for n in ('desk-paper-entry-dispatcher.timer', 'desk-paper-held-cycle.timer'):
            t = (ROOT / 'deploy' / n).read_text()
            self.assertIn('OnUnitInactiveSec', t); self.assertIn('Persistent=false', t)
            self.assertNotIn('[Install]', t)   # inactive templates: someone must enable them
        self.assertIn('0/5', (ROOT / 'deploy' / 'desk-paper-monitor.timer').read_text())


if __name__ == '__main__':
    unittest.main()
