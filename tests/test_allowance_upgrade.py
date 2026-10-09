"""Explicit policy transitions preserve original charged work and receipts."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from desk import allowance_policy as policy
from desk.job_persistence import JobPersistence
from desk.monitoring_budget import MonitoringBudget, MonitoringBlocked
from desk.paper_read_sources import PaperReadError
from tests import test_monitoring_budget as fixtures
from tests.helpers import T


class ResearchUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.jobs=JobPersistence(Path(self.tmp.name)/'jobs.sqlite')
        self.mint=fixtures.SOL

    def admit(self):
        with patch('desk.job_persistence.time.time',return_value=T):
            return self.jobs.admit(self.mint)

    def upgrade(self):
        return self.jobs.upgrade_allowance(at=T,provenance=policy.PROVENANCE)

    def test_used_day_preserved_and_explicit_upgrade_only(self):
        for _ in range(10):
            scan=self.admit()
            with self.jobs.connect() as c:c.execute("UPDATE scans SET status='FAILED' WHERE id=?",(scan,))
        with self.assertRaises(ValueError):self.admit()
        with self.jobs.connect() as c:original=[tuple(r) for r in c.execute('SELECT * FROM scans ORDER BY id')]
        first=self.upgrade()
        self.assertEqual(first,self.upgrade())
        with self.jobs.connect() as c:
            self.assertEqual(original,[tuple(r) for r in c.execute('SELECT * FROM scans ORDER BY id')])
            self.assertEqual(policy.research_limits(c,self.jobs.path),(1000,25))
        self.admit()
        with self.jobs.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM scans').fetchone()[0],11)

    def test_atomic_concurrent_activation_and_immutable_aliases(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            results=list(workers.map(lambda _:self.upgrade(),range(2)))
        self.assertEqual(results[0],results[1])
        for alias in ('rowid','_rowid_','oid'):
            with self.jobs.connect() as c:
                with self.assertRaises(sqlite3.IntegrityError):
                    c.execute(f'UPDATE OR REPLACE {policy.RESEARCH} SET {alias}=2')
        with self.jobs.connect() as c:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute(f'INSERT OR REPLACE INTO {policy.RESEARCH} SELECT * FROM {policy.RESEARCH}')

    def test_partial_schema_and_clock_rollback_refuse(self):
        self.admit()
        with self.assertRaises(ValueError):self.jobs.upgrade_allowance(at=T-1,provenance=policy.PROVENANCE)
        self.upgrade()
        with self.jobs.connect() as c:c.execute(f'DROP TRIGGER {policy.RESEARCH}_update')
        with self.assertRaises(ValueError):self.upgrade()
        with self.assertRaises(ValueError):self.admit()


class MonitoringUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.MonitoringBudgetTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def upgrade(self):
        with self.f.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prepared=self.f.budget.prepare_upgrade(c,at=T,provenance=policy.PROVENANCE)
            self.f.budget.activate_upgrade(c,prepared)
            c.commit()
        return prepared

    def test_sixty_original_receipts_count_against_new_cap_next_id_unchanged(self):
        refs=[self.f.read()[1] for _ in range(60)]
        originals=[self.f.store.load(key) for key in refs]
        self.assertEqual(self.f.budget.snapshot()['remaining'],0)
        prepared=self.upgrade()
        self.assertEqual(prepared,self.upgrade())
        self.assertEqual(self.f.budget.snapshot()['remaining'],3540)
        self.assertEqual(originals,[self.f.store.load(key) for key in refs])
        _,key=self.f.read();receipt=self.f.store.load(key)['monitoring_reservation']
        self.assertEqual((receipt['id'],receipt['cap'],receipt['policy_hash']),(61,3600,prepared[1]))
        restarted=MonitoringBudget(self.f.store,self.f.f.path,self.f.f.cfg,clock=lambda:T)
        self.assertEqual(restarted.snapshot()['remaining'],3539)
        self.assertEqual(self.f.progress.admission(self.f.scan)['requests_used'],0)

    def test_pending_and_failure_never_reset(self):
        pending=self.f.budget.reserve_read(self.f.progress,self.f.scan,'getSlot',[{'commitment':'finalized'}])
        old=self.f.accounting();self.upgrade()
        self.assertEqual(old,self.f.accounting())
        self.assertIn('MONITORING_OUTCOME_PENDING',self.f.budget.snapshot()['blockers'])
        with self.assertRaises(PaperReadError):self.f.read()
        self.assertEqual(pending['id'],1)

    def test_failure_latch_and_clock_highwater_survive(self):
        with self.assertRaises(Exception):self.f.read(fail=True)
        old=self.f.accounting();self.upgrade()
        self.assertEqual(old,self.f.accounting())
        self.assertIn('MONITORING_RECOVERY_REQUIRED',self.f.budget.snapshot()['blockers'])

    def test_prepare_reservation_race_refuses_stale_cutoff(self):
        with self.f.store.connect() as c:prepared=self.f.budget.prepare_upgrade(c,at=T,provenance=policy.PROVENANCE)
        self.f.read()
        with self.f.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            with self.assertRaises(MonitoringBlocked):self.f.budget.activate_upgrade(c,prepared)
        self.assertEqual(self.f.budget.snapshot()['cap'],60)

    def test_partial_damage_and_transaction_rollback_never_escalate(self):
        with self.f.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prepared=self.f.budget.prepare_upgrade(c,at=T,provenance=policy.PROVENANCE)
            self.f.budget.activate_upgrade(c,prepared);c.rollback()
        self.assertEqual(self.f.budget.snapshot()['cap'],60)
        self.upgrade()
        with self.f.store.connect() as c:c.execute(f'DROP TABLE {policy.MONITORING}')
        with self.assertRaises(ValueError):self.f.budget.snapshot()
