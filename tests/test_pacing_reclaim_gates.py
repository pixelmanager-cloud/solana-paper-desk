"""SYNTHETIC_TEST_ONLY: T23F end to end. The entry gates refuse on `pending` WITHOUT calling acquire(), so an orphaned
pacing slot (owner SIGKILLed) used to block entry forever on a flat ledger. They now call Pacer.reclaim_orphans() first;
a LIVE owner must still block. Real fresh store set (fresh_start.apply), real flock, real SIGKILL; no network."""
import inspect
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
import unittest

from desk import paper_terminal_reconciliation as terminal, provider_pacing as p
from tests.test_ops_fresh_start import FreshStartBase
from tools import history_first_paper_entry as history_first, paper_entry_dispatcher as dispatcher
from tools.ops import fresh_start as fs

ROOT = Path(__file__).resolve().parents[1]


class OrphanedSlotGateTests(FreshStartBase):
    def setUp(self):
        super().setUp()
        self.manifest = self.apply('exp-1')
        s = {k: Path(v['path']) for k, v in self.manifest['stores'].items()}
        self.ctx = dispatcher.plan(
            config=str(self.config), research_db=str(s['research_db']), evidence_db=str(s['evidence_db']),
            ledger_db=str(s['ledger_db']), discovery_db=str(self.discovery), pacing_db=str(self.pacer),
            journal=str(s['journal']), taker=fs.PRODUCTION_TAKER, amount_raw=100_000_000, pool_fee_bps='25')
        self.assertEqual(p.upgrade(self.pacer), 'UPGRADED')
        self.children = []
        self.addCleanup(self.reap)

    def reap(self):
        for child in self.children:
            if child.poll() is None: child.kill()
            child.wait(); child.stdout.close()

    def holder(self):
        """Another process that really holds a helius slot (real clock) until killed."""
        code = textwrap.dedent(f'''
            import time
            from pathlib import Path
            from desk import kraken_pacing_migration as k, provider_pacing as p
            k.POLICY = Path({str(self.base / 'migration.json')!r})     # the fixture's reviewed pin (as in FreshStartBase)
            p.Pacer({str(self.pacer)!r}).acquire("helius", timeout_seconds=1)
            print("HELD", flush=True); time.sleep(600)
            ''')
        proc = subprocess.Popen([sys.executable, '-c', code], cwd=ROOT, stdout=subprocess.PIPE, text=True,
                                env={**os.environ, 'PYTHONPATH': str(ROOT)})
        self.children.append(proc)
        self.assertEqual(proc.stdout.readline().strip(), 'HELD')
        # Pretend two hours passed since the grant (the slot is then older than the reclaim threshold).
        with sqlite3.connect(self.pacer) as c:
            c.execute('UPDATE state SET next_at=next_at-7200 WHERE provider="helius"')
        return proc

    def kill(self, proc):
        proc.send_signal(signal.SIGKILL); proc.wait()

    def pending(self):
        with sqlite3.connect(self.pacer) as c:
            return c.execute('SELECT pending FROM state WHERE provider="helius"').fetchone()[0]

    def test_dispatcher_preflight_passes_only_after_the_owner_process_is_gone(self):
        proc = self.holder()
        with self.assertRaisesRegex(ValueError, 'Provider pacing pending'):
            dispatcher._preflight(self.ctx)                                  # owner alive: must NOT pass
        self.assertIsNotNone(self.pending())
        self.kill(proc)
        dispatcher._preflight(self.ctx)                                      # owner gone + old enough: passes
        self.assertIsNone(self.pending())
        with sqlite3.connect(self.pacer) as c:
            self.assertEqual(c.execute('SELECT provider,reason FROM pacing_reclaims').fetchall(), [('helius', 'OWNER_GONE')])

    def test_dispatcher_preflight_still_refuses_a_young_orphan(self):
        proc = self.holder()
        with sqlite3.connect(self.pacer) as c:
            c.execute('UPDATE state SET next_at=next_at+7200 WHERE provider="helius"')   # undo the aging
        self.kill(proc)
        with self.assertRaisesRegex(ValueError, 'Provider pacing pending'):
            dispatcher._preflight(self.ctx)
        self.assertIsNotNone(self.pending())

    def test_terminal_gate_pacing_check(self):
        proc = self.holder()
        with self.assertRaisesRegex(ValueError, 'Provider outcome pending'):
            terminal._pacing(str(self.pacer))
        self.kill(proc)
        terminal._pacing(str(self.pacer))
        self.assertIsNone(self.pending())

    def test_history_first_pacer_check(self):
        proc = self.holder()
        with self.assertRaisesRegex(ValueError, 'Pacer recovery required'):
            history_first._pacer()
        self.kill(proc)
        self.assertEqual(history_first._pacer()[0], self.pacer)
        self.assertIsNone(self.pending())

    def test_the_rejection_gate_reclaims_before_it_tests_pending(self):
        source = inspect.getsource(dispatcher._rejection)
        self.assertLess(source.index('reclaim_orphans()'), source.index("'Provider pacing pending'"))
        source = inspect.getsource(dispatcher._preflight)
        self.assertLess(source.index('reclaim_orphans()'), source.index("'Provider pacing pending'"))

    def test_a_pacing_database_that_was_never_upgraded_still_blocks(self):
        # Fail closed: without the explicit upgrade no reclaim happens, whatever the age.
        other = self.base / 'old-schema.sqlite'
        p.initialize(other)
        proc = subprocess.Popen([sys.executable, '-c', textwrap.dedent(f'''
            import time
            from desk import provider_pacing as p
            p.Pacer({str(other)!r}).acquire("helius", timeout_seconds=1)
            print("HELD", flush=True); time.sleep(600)
            ''')], cwd=ROOT, stdout=subprocess.PIPE, text=True, env={**os.environ, 'PYTHONPATH': str(ROOT)})
        self.children.append(proc)
        self.assertEqual(proc.stdout.readline().strip(), 'HELD')
        with sqlite3.connect(other) as c:
            c.execute('UPDATE state SET next_at=next_at-7200 WHERE provider="helius"')
        self.kill(proc)
        self.assertEqual(p.Pacer(other).reclaim_orphans(), [])


if __name__ == '__main__':
    unittest.main()
