"""T12 review of T01: bounds that turn a healthy fresh store into a permanent latch.

``expectedFailure`` = RED demonstration of a real defect (remove the decorator when fixed).
"""
import unittest
from contextlib import closing

from desk import paper_cycle_no_entry as no_entry, paper_terminal_reconciliation as terminal
from tests.test_cycle_no_entry import fixture

no_entry_limit = 4096


def add_unrelated_pages(store, count):
    """Pages of other kinds (other scans, histories, quotes): same table, nothing to do with the retired scan."""
    for i in range(count):
        store.save({'kind': 'unrelated_filler', 'i': i})


def page_count(store):
    with closing(store.connect()) as c:
        return c.execute('SELECT count(*) FROM pages').fetchone()[0]


class PageInventoryBoundTests(unittest.TestCase):
    def test_one_rejected_candidate_costs_about_twenty_two_pages(self):
        """Measurement behind the finding: 4096 pages is only ~186 rejected candidates (T09 F7 estimate 70-200)."""
        t = fixture()
        self.addCleanup(t.doCleanups)
        pages = page_count(t.store)
        self.assertTrue(15 <= pages <= 40, pages)

    @unittest.expectedFailure   # DEFECT R2 (HIGH): gate re-indexes EVERY page of the store and raises above 4096
    def test_unrelated_pages_must_not_latch_the_gate_for_a_retired_rejection(self):
        t = fixture()
        self.addCleanup(t.doCleanups)
        add_unrelated_pages(t.store, no_entry_limit + 10)
        self.assertGreater(page_count(t.store), no_entry_limit)
        self.assertIsNone(terminal.gate(t.store, t.ctx['research_db'], ()))

    @unittest.expectedFailure   # DEFECT R2: publication itself refuses, so the NEXT rejection also latches the store
    def test_unrelated_pages_must_not_prevent_publishing_a_normal_rejection(self):
        t = fixture(legacy=True)
        self.addCleanup(t.doCleanups)
        with closing(t.store.connect()) as c:
            pending = c.execute('SELECT id,intent_hash FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()
        ledger = no_entry.ledger_snapshot(t.ctx['ledger_db'])
        forged = {k: v for k, v in t.result.items() if k != 'evidence_hash'}
        add_unrelated_pages(t.store, no_entry_limit + 10)
        no_entry.publish(t.store, t.progress, pass_id=pending[0], intent_hash=pending[1], result=forged, ledger=ledger)


if __name__ == '__main__':
    unittest.main()
