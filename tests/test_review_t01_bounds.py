"""T12 review of T01: bounds that turn a healthy fresh store into a permanent latch.

``expectedFailure`` = RED demonstration of a real defect (remove the decorator when fixed).
"""
import unittest
import unittest.mock
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

    def test_unrelated_pages_must_not_latch_the_gate_for_a_retired_rejection(self):
        t = fixture()
        self.addCleanup(t.doCleanups)
        add_unrelated_pages(t.store, no_entry_limit + 10)
        self.assertGreater(page_count(t.store), no_entry_limit)
        self.assertIsNone(terminal.gate(t.store, t.ctx['research_db'], ()))

    def test_unrelated_pages_must_not_prevent_publishing_a_normal_rejection(self):
        t = fixture(legacy=True)
        self.addCleanup(t.doCleanups)
        with closing(t.store.connect()) as c:
            pending = c.execute('SELECT id,intent_hash FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()
        ledger = no_entry.ledger_snapshot(t.ctx['ledger_db'])
        forged = {k: v for k, v in t.result.items() if k != 'evidence_hash'}
        add_unrelated_pages(t.store, no_entry_limit + 10)
        no_entry.publish(t.store, t.progress, pass_id=pending[0], intent_hash=pending[1], result=forged, ledger=ledger)

    def test_gate_cost_is_flat_in_unrelated_store_size(self):
        """T22F R2: the gate loads only the bound refs of each retained rejection, never every page.

        Generous bound: with ~40x more unrelated pages the gate must not take 10x longer (it used to be linear in
        pages and raised outright above 4096). Page loads are counted too, which is the deterministic signal.
        """
        import time
        t = fixture()
        self.addCleanup(t.doCleanups)
        args = (t.store, t.ctx['research_db'], ())

        def measure():
            loads = []
            real = terminal._load

            def counting(store, key):
                loads.append(key)
                return real(store, key)
            with unittest.mock.patch.object(terminal, '_load', counting):
                started = time.perf_counter()
                self.assertIsNone(terminal.gate(*args))
                return time.perf_counter() - started, len(loads)
        small_time, small_loads = measure()
        add_unrelated_pages(t.store, 1500)
        big_time, big_loads = measure()
        self.assertEqual(big_loads, small_loads)
        self.assertLess(big_time, 10 * max(small_time, 0.05))


if __name__ == '__main__':
    unittest.main()
