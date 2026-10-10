"""T24/F7: the preparation-rejection gate indexes evidence pages incrementally.

Fixtures only. The index is derived state: everything it skips must still fail closed.
"""
import unittest
from contextlib import closing
from unittest.mock import patch

from desk import history_preparation_rejection as rejection
from desk import paper_terminal_reconciliation as terminal
from tests import test_history_preparation_phase as prep_fixture


class RejectionIndex(unittest.TestCase):
    def setUp(self):
        self.h = prep_fixture.PreparationTests('test_global_gate_retirement_and_unrelated_scan')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.rejected()
        self.research = self.h.context['research_db']
        self.store = self.h.store
        with closing(self.store.connect()) as c:
            self.pass_id, self.scan, self.intent, self.outcome = rejection.rows(c)[0]
        self.gate()

    def gate(self):
        return terminal.gate(self.store, self.research, ('b' * 32,))

    def loads(self):
        count = [0]
        real = terminal._load

        def counting(*a, **k):
            count[0] += 1
            return real(*a, **k)
        with patch.object(terminal, '_load', side_effect=counting):
            self.assertIsNone(self.gate())
        return count[0]

    def attempt_record(self):
        value = terminal._load(self.store, self.outcome)
        return terminal._load(self.store, value['attempt_refs'][0])

    def test_conflicting_outcome_added_after_the_index_was_built_is_refused(self):
        self.store.save({'kind': 'history_preparation_no_entry_v1', 'intent_hash': self.intent, 'pad': 'conflict'})
        with self.assertRaises(ValueError):
            self.gate()

    def test_partial_first_outcome_for_the_intent_is_refused(self):
        self.store.save({'kind': 'history_first_paper_preparation_outcome_v1', 'intent_hash': self.intent, 'pad': 'partial'})
        with self.assertRaises(ValueError):
            self.gate()

    def test_ambiguous_duplicate_charge_added_later_is_refused(self):
        self.store.save({**self.attempt_record(), 'pad': 'second record claiming the same charge'})
        with self.assertRaises(ValueError):
            self.gate()

    def test_unrelated_outcome_for_another_intent_is_not_a_conflict(self):
        self.store.save({'kind': 'history_preparation_no_entry_v1', 'intent_hash': '0' * 64, 'pad': 'other'})
        self.assertIsNone(self.gate())

    def test_corrupt_new_page_fails_every_call_and_is_not_skipped(self):
        key = self.store.save({'filler': 1})
        with closing(self.store.connect()) as c:
            c.execute('UPDATE pages SET payload=? WHERE hash=?', (b'corrupt', key))
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.gate()

    def test_corrupt_used_attempt_page_is_detected_even_when_cached(self):
        value = terminal._load(self.store, self.outcome)
        key = value['attempt_refs'][0]
        with closing(self.store.connect()) as c:
            c.execute('UPDATE pages SET payload=? WHERE hash=?', (b'corrupt', key))
        with self.assertRaises(ValueError):
            self.gate()

    def test_deleted_page_invalidates_the_cached_prefix(self):
        for i in range(5):
            self.store.save({'filler': i})
        self.gate()
        steady = self.loads()
        with closing(self.store.connect()) as c:
            c.execute('DELETE FROM pages WHERE hash=?', (self.store.save({'filler': 0}),))
        self.assertGreater(self.loads(), steady)

    def test_steady_state_decodes_only_new_pages(self):
        steady = self.loads()
        for i in range(25):
            self.store.save({'filler': i, 'pad': 'z' * 100})
        self.assertEqual(self.loads(), steady + 25)
        self.assertEqual(self.loads(), steady)

    def test_page_count_warns_at_eighty_percent_and_bounds_at_the_maximum(self):
        total = len(self.store.connect().execute('SELECT hash FROM pages').fetchall())
        with patch.object(rejection, 'MAX_INDEXED_PAGES', total + 10), \
                patch.object(rejection, 'WARN_INDEXED_PAGES', total + 2):
            rejection._INDEX.clear()
            self.gate()                                   # below warning: silent
            for i in range(3):
                self.store.save({'filler': i})
            with self.assertLogs(rejection.__name__, 'WARNING'):
                self.gate()
            for i in range(10, 20):
                self.store.save({'filler': i})
            with self.assertRaisesRegex(ValueError, 'inventory bound'):
                self.gate()


if __name__ == '__main__':
    unittest.main()
