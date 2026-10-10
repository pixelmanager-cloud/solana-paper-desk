"""Audit B: monitoring-budget latches and O(N) re-verification. Fixture only."""
import unittest
from unittest.mock import patch

from desk.evidence import EvidenceStore
from desk.paper_read_sources import PaperReadError
from tests import test_monitoring_budget as base


class _Fixture(base.MonitoringBudgetTests):
    pass


for _name in dir(base.MonitoringBudgetTests):      # reuse setUp/helpers only
    if _name.startswith('test_'):
        setattr(_Fixture, _name, None)


class MonitoringLatchTests(_Fixture):
    def test_one_transient_failed_read_must_not_block_entries_for_good(self):
        """A single failed held read (timeout/5xx/429, ~every provider has
        one over 48h) sets blocked='SOURCE_FAILURE'. Nothing ever clears it:
        reserve_read raises MONITORING_RECOVERY_REQUIRED and snapshot() keeps
        a blocker, which tools/paper_entry_dispatcher._preflight treats as
        'Monitoring pending or blocked' => NO new paper entry, ever (even
        after the position closes), until a reviewed coordinator handoff."""
        with self.assertRaises(PaperReadError):
            self.read(fail=True)
        self.budget.clock = lambda: base.T + 7 * 86400
        self.assertEqual(self.budget.snapshot()['blockers'], [])

    @unittest.expectedFailure
    def test_process_killed_after_reservation_must_not_block_forever(self):
        """Reservation is committed before the HTTP call; SIGKILL/SIGTERM at
        the systemd timeout leaves a reservation without an outcome.
        snapshot() then reports MONITORING_OUTCOME_PENDING forever (no TTL)."""
        with patch('desk.paper_read_sources.os.environ.get', return_value=base.KEY), \
                patch('desk.paper_read_sources.build_opener') as opener:
            opener.return_value.open.side_effect = KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                self.read_unmocked()
        self.budget.clock = lambda: base.T + 7 * 86400
        self.assertEqual(self.budget.snapshot()['blockers'], [])


class MonitoringScalingTests(_Fixture):
    @unittest.expectedFailure
    def test_snapshot_cost_is_independent_of_total_history(self):
        """snapshot() -> _accounting() re-loads and re-hashes EVERY past
        reservation's evidence blob (store.load per row). Each held read calls
        reserve_read + snapshot x2 + retain_outcome x2 (~5 full passes), all
        inside the 10s _Budget. Measured (tiny getSlot blobs): 0.07s at 50
        rows, 0.136s at 400 rows (~0.18ms/row); real Jupiter/pool blobs are
        larger. ~6 reads/pass x 12 passes/h => ~72 rows/h while a position is
        held: ~1800 rows (~25h) puts one pass over the 10s budget."""
        for _ in range(30):
            self.read()
        calls = []
        real = EvidenceStore.load
        def counting(store, key, *a, **k):
            calls.append(key); return real(store, key, *a, **k)
        with patch.object(EvidenceStore, 'load', counting):
            self.budget.snapshot()
        self.assertLess(len(calls), 10, f'{len(calls)} blob loads for one snapshot at N=30')


if __name__ == '__main__':
    unittest.main()
