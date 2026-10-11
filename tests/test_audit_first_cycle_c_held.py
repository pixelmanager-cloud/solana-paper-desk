"""Audit C (fresh-start first cycle): held-pass latch, EXIT_ONLY stickiness,
fresh-store bootstrap gaps, scheduler lease bootstrap, live-latency deadline.

Fixtures only; no network, no provider keys. Tests marked expectedFailure are RED
demonstrations of defects/gaps; each docstring states the scenario.
"""
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from desk import paper_cycle as cycle, quote_execution as qe
from tests.test_kraken_lifecycle import KrakenLifecycleTests, actual_cycle


class _Base(KrakenLifecycleTests):
    # keep setUp, neutralise inherited tests
    test_entry_held_exit_restart_accounting_and_originals = None
    test_entry_saved_valuation_tampering_and_missing_version_refuse_read_and_duplicate = None
    test_partial_full_exit_preserves_investigation_basis_and_monitoring_charges = None

    def _enter_and_provision(self):
        result = actual_cycle(self.h)
        self.assertEqual(result['status'], 'COMPLETE', result)
        from desk.monitoring_budget import MonitoringBudget
        MonitoringBudget(self.h.f.progress.store, self.h.path, self.h.cfg,
                         clock=lambda: self.h.f.at).provision()
        position = cycle._state(self.h.path, self.h.cfg)['positions'][self.h.target.mint]
        return replace(self.h.item, target=replace(self.h.target,
                       amount_raw=qe.raw_quantity(position['qty'], 6)))

    def _null_passes(self):
        with sqlite3.connect(self.h.f.progress.store.path) as c:
            return c.execute('SELECT COUNT(*) FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()[0]


class HeldPassLatchTests(_Base):
    @unittest.expectedFailure
    def test_dust_sell_quote_must_not_strand_position(self):
        """Rug scenario: held pass sees a sell quote worth less than the fixed fee.
        The pass spends 5 monitoring requests, ends BLOCKED (UNRESOLVED_QUOTE_DEMAND),
        so run_once leaves paper_observation_passes.outcome_hash NULL. The next held
        pass (healthy quote) is refused with OBSERVATION_RECOVERY_REQUIRED forever:
        the position can never be exited without a code-level reviewed receipt."""
        item = self._enter_and_provision()
        self.h.sell_output = 40_000
        first = actual_cycle(self.h, positions=(item,), candidates=(), monitoring=True)
        self.assertEqual(first['status'], 'BLOCKED')
        self.assertGreater(first['monitoring_attempted_requests'], 0)
        self.h.sell_output = 10_000_000
        second = actual_cycle(self.h, positions=(item,), candidates=(), monitoring=True)
        self.assertNotEqual(second['blockers'], ['OBSERVATION_RECOVERY_REQUIRED'], second)

    @unittest.expectedFailure
    def test_zero_sell_quote_must_not_strand_position(self):
        """Same latch via ObservationError (sell quote outAmount 0 -> HELD_OBSERVATION_CONTENT_REJECTED)."""
        item = self._enter_and_provision()
        self.h.sell_output = 0
        first = actual_cycle(self.h, positions=(item,), candidates=(), monitoring=True)
        self.assertEqual(first['blockers'], ['HELD_OBSERVATION_CONTENT_REJECTED'])
        self.h.sell_output = 10_000_000
        second = actual_cycle(self.h, positions=(item,), candidates=(), monitoring=True)
        self.assertNotEqual(second['blockers'], ['OBSERVATION_RECOVERY_REQUIRED'], second)

    def test_latch_is_global_not_per_scan_and_per_position(self):
        """Documents (passes): the NULL pass count is 1 after the failed held pass."""
        item = self._enter_and_provision()
        self.h.sell_output = 0
        actual_cycle(self.h, positions=(item,), candidates=(), monitoring=True)
        self.assertEqual(self._null_passes(), 1)


class ExitOnlyStickinessTests(_Base):
    def setUp(self):
        # T23 (F5): recovery is an explicit versioned experiment option, so this scenario runs on a
        # ledger initialised with paper_exit_only_recovery_version=1 (default-off stays sticky; see
        # tests.test_exit_only_recovery.test_default_off_is_byte_identical_and_sticky).
        super().setUp()
        self.h.cfg = self.h.cfg | {'paper_exit_only_recovery_version': 1}
        self.h.path = Path(self.h.f.tmp.name) / 'kraken-recovery.sqlite'
        cycle.initialize(self.h.path, self.h.cfg)

    def test_mode_returns_to_running_after_stale_mark_exit(self):
        """Entry, then the 5-second stale-mark watchdog (monitor.tick) fires >10s later
        (guaranteed with a 5-minute held timer). That sets exit_blocked and mode=EXIT_ONLY.
        The held pass then exits cleanly (STOP) but mode stays EXIT_ONLY with zero positions:
        no code path other than an operator --control RESUME (held service passes none)
        restores RUNNING, so tools.paper_scheduler entry mode reports HELD_POSITION_PRIORITY
        forever and no second entry ever occurs."""
        item = self._enter_and_provision()
        from desk.monitor import tick
        self.assertEqual(tick(self.h.path, self.h.cfg, now=self.h.f.at + 11)['status'], 'MARKS_EXPIRED')
        self.h.f.at += 20
        self.h.sell_output = 1_000_000
        done = actual_cycle(self.h, positions=(item,), candidates=(), monitoring=True)
        self.assertEqual(done['status'], 'COMPLETE', done)
        state = cycle._state(self.h.path, self.h.cfg)
        self.assertEqual(state['positions'], {})
        self.assertEqual(state['mode'], 'RUNNING')


class FreshStoreBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name).resolve()
        os.chmod(self.root, 0o700)

    @unittest.expectedFailure
    def test_monitoring_3600_upgrade_available_on_fresh_stores(self):
        """Fresh research+evidence+ledger (init + provision-monitoring succeed) leave the
        monitoring cap at 60/hour; desk.monitoring_budget.upgrade_existing raises
        'no such table: ownership_acquisition_setup' until some acquisition has already
        run. A 5-request held pass every 5 minutes is exactly 60/hour (zero slack; a
        partial exit pass costs 6)."""
        from desk.job_persistence import JobPersistence
        from desk.evidence import EvidenceStore
        from desk.history_progress import HistoryProgress
        from desk.monitoring_budget import upgrade_existing
        from desk.monitoring_budget import MonitoringBudget
        from tests.test_paper_cycle import PaperCycleTests
        t = PaperCycleTests(); self.addCleanup(t.doCleanups); t.setUp()
        cfg = t.cfg | {'paper_usd_valuation_version': 1}
        research, evidence, ledger = (self.root / n for n in ('r.sqlite', 'e.sqlite', 'l.sqlite'))
        JobPersistence(research); HistoryProgress(EvidenceStore(evidence))
        cycle.initialize(ledger, cfg)
        from desk.paper_monitor_operator import provision
        provision(research, evidence, ledger, cfg)
        upgrade_existing(research, evidence, ledger, cfg,
                         provenance='USER_AUTHORIZED_CONTINUOUS_SCANNING_V1')

    @unittest.expectedFailure
    def test_fresh_pacing_db_can_gain_kraken_lane(self):
        """provider_pacing.initialize() makes a DB with no kraken lane; kraken_pacing_migration.migrate
        refuses ('Pacing migration not reviewed') because config/kraken-pacing-migration.json only
        pins the old production path/state/source hash. Dispatcher plan() requires the kraken lane.
        A fresh pacing DB needs a hand-added pin carrying the FINAL post-fix implementation hash."""
        from desk import provider_pacing as pace, kraken_pacing_migration as mig
        path = self.root / 'pacing.sqlite'
        pace.initialize(path)
        self.assertEqual(mig.migrate(path)['status'], 'MIGRATED')

    @unittest.expectedFailure
    def test_scheduler_lease_fails_closed_without_traceback_on_fresh_dir(self):
        """tools.paper_scheduler on a fresh store directory: paper-scheduler.lock is never
        created by any tool (lease() opens without O_CREAT) and no unit sets
        DESK_PAPER_SCHEDULER_IDENTITY. main() has no handler, so the 60s entry timer and
        5s expire timer die with an uncaught exception instead of a structured status."""
        from desk.job_persistence import JobPersistence
        from tools import paper_scheduler
        research = self.root / 'research.sqlite'
        JobPersistence(research)
        env = {k: v for k, v in os.environ.items() if k != 'DESK_PAPER_SCHEDULER_IDENTITY'}
        old = dict(os.environ)
        os.environ.clear(); os.environ.update(env)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        code = paper_scheduler.main(['--research-db', str(research), '--mode', 'entry', '--',
                                     '--research-db', str(research)])
        self.assertIn(code, (0, 2))


class LiveLatencyDeadlineTests(_Base):
    @unittest.expectedFailure
    def test_entry_survives_realistic_provider_latency(self):
        """Pass deadline is a hard 10s (_Budget.remaining). The reviewed native-clock VPS
        fixtures already need 7.0s (BUY) / 7.8s (SELL) with ZERO network latency. Here each
        provider response costs 1.6s of monotonic time (about 7 requests, still only 11s): the entry must still
        complete, but the cycle aborts CYCLE_DEADLINE_UNAVAILABLE and leaves a NULL pass."""
        outer = self.h

        class Slow(list):
            def append(self, item):
                outer.f.tick += 1.6
                super().append(item)
        outer.http_calls = Slow()
        outer.f.tick = 1
        result = actual_cycle(self.h)
        self.assertEqual(result['status'], 'COMPLETE', result['blockers'])


if __name__ == '__main__':
    unittest.main()
