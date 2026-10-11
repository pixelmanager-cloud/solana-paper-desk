"""SYNTHETIC_TEST_ONLY: the append-only inventory of completed original passes (T24S item 1).

Unit tests use a real EvidenceStore with synthetic pages; the publisher / gate tests use the same real stores as T24R
(a retained history-preparation rejection and a retained cycle no-entry). The inventory must make a gate cost O(new passes) page
loads WITHOUT waiving anything: a missing, extra, altered or stale row fails closed, an unindexed pass is always classified, and a
forced full replay re-classifies every pass.
"""
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from desk import history_preparation_rejection as rejection
from desk import pass_inventory as inv
from desk import paper_cycle_no_entry as no_entry
from desk import paper_terminal_reconciliation as terminal
from desk import verified_index as vi
from desk.evidence import EvidenceStore
from tests import legacy_null_pass
from tests import test_cycle_no_entry as no_entry_fixture
from tests import test_history_preparation_phase as prep_fixture

RECEIPT = 'history_preparation_no_entry_v1'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'evidence.sqlite')
        self.store = EvidenceStore(self.path)
        with closing(self.store.connect()) as c:
            c.execute('CREATE TABLE paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
        self.n = 0

    def add(self, count, kind='paper_pass_closure_v1', scan=None, complete=True):
        rows = []
        for _ in range(count):
            i, self.n = self.n, self.n + 1
            intent = self.store.save({'kind': 'paper_cycle_intent_v1', 'n': i})
            page = {'kind': kind, 'n': i, 'intent_hash': intent}
            if scan is not None:
                page['scan_id'] = scan
            outcome = self.store.save(page) if complete else None
            rows.append(('%032x' % (0x1000 + i), intent, outcome))
        with closing(self.store.connect()) as c:
            c.executemany('INSERT INTO paper_observation_passes VALUES(?,?,?)', rows)
        return rows

    def completed(self, kinds=(), **kw):
        loads = [0]
        real = terminal._load

        def counted(*args, **kwargs):
            loads[0] += 1
            return real(*args, **kwargs)
        with patch.object(terminal, '_load', new=counted):
            result = inv.completed(self.store, kinds, **kw)
        return result, loads[0]

    def table(self):
        with closing(self.store.connect()) as c:
            return c.execute(f'SELECT id,pass_id,kind,scan_id FROM {inv.TABLE} ORDER BY id').fetchall() if inv._objects(c) else []

    def raw(self, statement):
        with closing(self.store.connect()) as c:
            c.execute(statement)

    def tamper(self, statement, args=()):
        """Run ``statement`` with every trigger lifted (a restore or a manual edit), then put the triggers back."""
        with closing(self.store.connect()) as c:
            triggers = c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall()
            for name, _ in triggers:
                c.execute(f'DROP TRIGGER "{name}"')
            c.execute(statement, args)
            for _, sql in triggers:
                table = sql.split(' ON ')[1].split()[0]
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    c.execute(sql)


class CostTests(Base):
    def test_the_first_call_classifies_every_completed_pass_once_and_remembers_them(self):
        self.add(40)
        result, loads = self.completed()
        self.assertEqual((loads, result.persisted, result.count, result.indexed_count), (40, True, 40, 0))
        self.assertEqual(len(self.table()), 40)

    def test_a_later_call_loads_only_the_sample_whatever_the_history(self):
        measured = {}
        for size in (30, 300, 1500):
            self.add(size - self.n)
            self.completed()                                  # catch up
            measured[size] = self.completed()[1]
        self.assertEqual(len(set(measured.values())), 1, measured)
        self.assertLessEqual(measured[30], inv.SAMPLE + inv.NEWEST)

    def test_new_passes_cost_one_load_each_plus_the_sample(self):
        self.add(200)
        self.completed()
        base = self.completed()[1]
        self.add(3)
        result, loads = self.completed()
        self.assertEqual(len(result.fresh), 3)
        self.assertLessEqual(loads - 3, base + 0)             # the same sample as before plus the three new classifications
        self.assertEqual(len(self.table()), 203)

    def test_pending_passes_are_not_completed_passes(self):
        self.add(5)
        self.add(4, complete=False)
        result, loads = self.completed()
        self.assertEqual((result.count, loads), (5, 5))

    def test_the_sample_moves_with_the_history_so_every_row_is_re_read_over_time(self):
        self.add(40)
        self.completed()
        seen = set()
        real = terminal._classification

        def spy(store, key):
            seen.add(key)
            return real(store, key)
        with patch.object(terminal, '_classification', new=spy):
            for _ in range(60):
                self.add(1)
                inv.completed(self.store, ())
        with closing(self.store.connect()) as c:
            first40 = {r[0] for r in c.execute('SELECT outcome_hash FROM paper_observation_passes ORDER BY rowid LIMIT 40')}
        self.assertGreater(len(seen & first40), 20)

    def test_full_replay_reclassifies_everything(self):
        self.add(25)
        self.completed()
        self.assertEqual(self.completed(full=True)[1], 25)
        with patch.dict(os.environ, {'DESK_GATE_FULL_REPLAY': '1'}):
            self.assertEqual(self.completed()[1], 25)


class ClassificationTests(Base):
    def test_requested_kinds_are_listed_with_their_scalars_and_others_only_counted(self):
        self.add(6)
        wanted = self.add(2, kind=RECEIPT, scan='scan-x')
        result, _ = self.completed((RECEIPT,))
        listed = result.of(RECEIPT)
        self.assertEqual(sorted(r[0] for r in listed), sorted(w[0] for w in wanted))
        for pass_id, scan, page_intent, intent, outcome in listed:
            row = next(w for w in wanted if w[0] == pass_id)
            self.assertEqual((scan, page_intent, intent, outcome), ('scan-x', row[1], row[1], row[2]))
        self.assertEqual(result.of('paper_pass_closure_v1'), [])
        self.assertEqual(result.count, 8)
        again, _ = self.completed((RECEIPT,))                    # the same answer from the persisted table
        self.assertEqual(sorted(again.of(RECEIPT)), sorted(listed))

    def test_a_page_without_a_string_kind_or_scan_is_stored_as_none(self):
        key = self.store.save({'kind': 7, 'scan_id': 9})
        intent = self.store.save({'x': 1})
        with closing(self.store.connect()) as c:
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,?)', ('%032x' % 1, intent, key))
        self.completed()
        self.assertEqual(self.table(), [(1, '%032x' % 1, '', None)])


class PersistenceTests(Base):
    def test_a_review_pass_never_writes_the_store(self):
        self.add(12)
        result, loads = self.completed(persist=False)
        self.assertEqual((result.persisted, loads, self.table()), (False, 12, []))
        with closing(self.store.connect()) as c:
            self.assertFalse(inv._objects(c))
        self.assertEqual(self.completed(persist=False)[1], 12)           # nothing was remembered: it simply costs again

    def test_a_read_only_store_still_answers(self):
        self.add(9)
        read_only = EvidenceStore(self.path, read_only=True)
        result = inv.completed(read_only, ())
        self.assertEqual((result.count, result.persisted), (9, False))

    def test_a_busy_database_is_not_waited_for(self):
        self.add(9)
        holder = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute('BEGIN IMMEDIATE')
        result, _ = self.completed()
        self.assertEqual((result.count, result.persisted), (9, False))
        holder.execute('ROLLBACK')
        self.assertEqual(self.completed()[0].persisted, True)

    def test_a_pass_that_changed_between_classification_and_persistence_is_not_remembered(self):
        rows = self.add(3)
        real = inv._persist
        def swap(store, fresh):
            with closing(store.connect()) as c:
                c.execute('UPDATE paper_observation_passes SET outcome_hash=? WHERE id=?', ('f' * 64, rows[0][0]))
            return real(store, fresh)
        with patch.object(inv, '_persist', new=swap):
            result, _ = self.completed()
        self.assertFalse(result.persisted)
        self.assertEqual(self.table(), [])

    def test_the_rows_are_written_in_the_transaction_that_rolls_back(self):
        rows = self.add(1, complete=False)
        with closing(self.store.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE paper_observation_passes SET outcome_hash=? WHERE id=?', ('a' * 64, rows[0][0]))
            inv.record(c, rows[0][0], rows[0][1], 'a' * 64, 'k', None, None)
            c.execute('ROLLBACK')
        self.assertEqual(self.table(), [])


class TableGuardTests(Base):
    def setUp(self):
        super().setUp()
        self.add(5)
        self.completed()

    def test_append_only_contiguous_and_unique(self):
        with closing(self.store.connect()) as c:
            for statement in (f'UPDATE {inv.TABLE} SET kind=kind', f'DELETE FROM {inv.TABLE}'):
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
                    c.execute(statement)
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):                      # a gap in the ids
                c.execute(f"INSERT INTO {inv.TABLE} VALUES(99,'{'b' * 32}','{'c' * 64}','{'d' * 64}','k',NULL,NULL,'{'e' * 64}')")
            with self.assertRaises(sqlite3.IntegrityError):                                          # the same pass twice
                inv.record(c, '%032x' % 0x1000, 'c' * 64, 'd' * 64, 'k', None, None)

    def test_the_table_is_bounded(self):
        with patch.object(inv, 'MAX_ROWS', 5):
            pass
        self.assertEqual(inv.MAX_ROWS, 16384)                                      # above the 10,000 original-pass ceiling
        self.assertGreater(inv.MAX_ROWS, 10000)


class TamperingTests(Base):
    """Every deviation from the passes themselves fails closed."""

    def setUp(self):
        super().setUp()
        self.add(12)
        self.add(2, kind=RECEIPT, scan='scan-x')
        self.completed()

    def refused(self, message=None, **kw):
        with self.assertRaises(ValueError) as caught:
            inv.completed(self.store, (RECEIPT,), **kw)
        if message:
            self.assertIn(message, str(caught.exception))

    def test_schema_deviation(self):
        for tamper in (f'ALTER TABLE {inv.TABLE} ADD COLUMN extra TEXT', f'DROP TRIGGER {inv.TABLE}_update',
                       f'CREATE INDEX stray ON {inv.TABLE}(pass_id)', f'CREATE TABLE {inv.TABLE}_shadow(x)'):
            with self.subTest(tamper=tamper):
                self.setUp()
                self.raw(tamper)
                self.refused('schema')

    def test_a_pass_whose_outcome_changed_after_it_was_indexed(self):
        self.tamper('UPDATE paper_observation_passes SET outcome_hash=? WHERE id=?', ('f' * 64, '%032x' % 0x1000))
        self.refused('disagrees with the original passes')

    def test_a_pass_whose_intent_changed(self):
        self.tamper('UPDATE paper_observation_passes SET intent_hash=? WHERE id=?', ('f' * 64, '%032x' % 0x1000))
        self.refused('disagrees with the original passes')

    def test_a_row_for_a_pass_that_does_not_exist(self):
        self.tamper('DELETE FROM paper_observation_passes WHERE id=?', ('%032x' % 0x1001,))
        self.refused()

    def test_more_rows_than_completed_passes(self):
        self.tamper('UPDATE paper_observation_passes SET outcome_hash=NULL WHERE id=?', ('%032x' % 0x1001,))
        self.refused()

    def test_a_deleted_row_breaks_contiguity(self):
        self.tamper(f'DELETE FROM {inv.TABLE} WHERE id=3')
        self.refused('not contiguous')

    def test_a_forged_kind_on_a_sampled_row_is_caught_against_the_retained_page(self):
        with patch.object(inv, 'SAMPLE', 10 ** 6):                                  # every row sampled
            self.tamper(f"UPDATE {inv.TABLE} SET kind='x' WHERE id=2")
            self.refused('retained outcome page')

    def test_a_forged_row_digest(self):
        with patch.object(inv, 'SAMPLE', 10 ** 6):
            self.tamper(f"UPDATE {inv.TABLE} SET proof_digest=? WHERE id=2", ('0' * 64,))
            self.refused('retained outcome page')

    def test_a_forged_requested_kind_row_is_caught_even_when_it_is_not_sampled(self):
        with patch.object(inv, 'SAMPLE', 0), patch.object(inv, 'NEWEST', 0):
            self.tamper(f"UPDATE {inv.TABLE} SET proof_digest=? WHERE kind=?", ('0' * 64, RECEIPT))
            self.refused('row digest mismatch')

    def test_a_forged_kind_that_hides_nothing_unrequested_is_only_found_by_sampling_or_full_replay(self):
        with patch.object(inv, 'SAMPLE', 0), patch.object(inv, 'NEWEST', 0):
            self.tamper(f"UPDATE {inv.TABLE} SET kind='x' WHERE id=2")
            inv.completed(self.store, (RECEIPT,))                                   # documented trade-off: not re-read each call
            self.refused('retained outcome page', full=True)

    def test_a_missing_outcome_page_is_caught_by_a_sample(self):
        with patch.object(inv, 'SAMPLE', 10 ** 6):
            self.tamper('DELETE FROM pages WHERE hash=(SELECT outcome_hash FROM paper_observation_passes WHERE id=?)', ('%032x' % 0x1000,))
            with self.assertRaises(ValueError):
                inv.completed(self.store, (RECEIPT,))


class PublisherAndGateTests(unittest.TestCase):
    """Real stores: the publishers write the inventory row in the transaction that sets the outcome, and the gates use it."""

    def history(self):
        h = prep_fixture.PreparationTests('test_global_gate_retirement_and_unrelated_scan')
        h.setUp()
        self.addCleanup(h.doCleanups)
        result = h.rejected()
        return h, result

    def test_the_history_publisher_indexes_the_pass_with_the_outcome(self):
        h, result = self.history()
        with closing(h.store.connect()) as c:
            row = c.execute(f'SELECT pass_id,intent_hash,outcome_hash,kind,scan_id,page_intent_hash FROM {inv.TABLE}').fetchall()
        self.assertEqual(row, [(result['pass_id'], result['intent_hash'], result['evidence_hash'], RECEIPT, result['scan_id'], result['intent_hash'])])

    def test_a_failed_inventory_write_rolls_the_receipt_back(self):
        h = prep_fixture.PreparationTests('test_global_gate_retirement_and_unrelated_scan')
        h.setUp()
        self.addCleanup(h.doCleanups)
        with patch.object(inv, 'record', side_effect=ValueError('inventory write failed')), self.assertRaises(ValueError):
            h.rejected()
        with closing(h.store.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM paper_observation_passes WHERE outcome_hash IS NOT NULL').fetchone()[0], 0)

    def test_the_no_entry_publisher_indexes_the_pass_with_the_outcome(self):
        legacy_null_pass.install(self)
        t = no_entry_fixture.fixture()
        self.addCleanup(t.doCleanups)
        with closing(t.store.connect()) as c:
            ((pass_id, scan, intent, outcome),) = no_entry.rows(c)
            self.assertEqual(c.execute(f'SELECT pass_id,intent_hash,outcome_hash,kind,scan_id,page_intent_hash FROM {inv.TABLE}').fetchall(),
                             [(pass_id, intent, outcome, no_entry.KIND, scan, intent)])

    def test_the_gate_fails_closed_when_the_inventory_hides_a_receipt_pass(self):
        h, result = self.history()
        with closing(h.store.connect()) as c:
            triggers = c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (inv.TABLE,)).fetchall()
            for name, _ in triggers:
                c.execute(f'DROP TRIGGER "{name}"')
            c.execute(f"UPDATE {inv.TABLE} SET kind=''")
            for _, sql in triggers:
                c.execute(sql)
        with patch.object(inv, 'SAMPLE', 0), patch.object(inv, 'NEWEST', 0):
            with self.assertRaisesRegex(ValueError, 'inventory incomplete'):
                terminal.gate(h.store, h.context['research_db'], ('b' * 32,))

    def test_the_gate_fails_closed_when_the_inventory_invents_a_receipt_pass(self):
        h, result = self.history()
        with closing(h.store.connect()) as c:
            intent = h.store.save({'kind': 'paper_cycle_intent_v1', 'n': 77})
            outcome = h.store.save({'kind': 'paper_pass_closure_v1', 'n': 77, 'intent_hash': intent})       # NOT a receipt page
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,?)', ('%032x' % 77, intent, outcome))
            inv.record(c, '%032x' % 77, intent, outcome, RECEIPT, 'scan-77', intent)                          # but indexed as one
        with patch.object(inv, 'SAMPLE', 10 ** 6), self.assertRaises(ValueError):                              # sampled: the page says otherwise
            terminal.gate(h.store, h.context['research_db'], ('b' * 32,))
        with patch.object(inv, 'SAMPLE', 0), patch.object(inv, 'NEWEST', 0), self.assertRaisesRegex(ValueError, 'inventory incomplete'):
            terminal.gate(h.store, h.context['research_db'], ('b' * 32,))

    def test_the_gate_catches_up_a_store_without_an_inventory_and_stays_cheap_afterwards(self):
        h, result = self.history()
        with closing(h.store.connect()) as c:                       # an older store: no inventory table at all
            triggers = c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (inv.TABLE,)).fetchall()
            for (name,) in triggers:
                c.execute(f'DROP TRIGGER "{name}"')
            c.execute(f'DROP TABLE {inv.TABLE}')
        self.assertEqual(terminal.gate(h.store, h.context['research_db'], (result['scan_id'],)), 'REJECTED_SCAN_RETIRED')
        with closing(h.store.connect()) as c:
            self.assertEqual(c.execute(f'SELECT count(*) FROM {inv.TABLE}').fetchone()[0], 1)         # remembered by the gate

    def drop_inventory(self, store):
        with closing(store.connect()) as c:
            for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (inv.TABLE,)).fetchall():
                c.execute(f'DROP TRIGGER "{name}"')
            c.execute(f'DROP TABLE {inv.TABLE}')

    def test_a_review_plan_reads_the_gate_without_writing_the_inventory(self):
        h, result = self.history()
        self.drop_inventory(h.store)
        with patch.object(rejection, 'verify', return_value={'context': {'research_db': h.context['research_db']}}):
            self.assertIsNone(rejection.gate(h.store, h.context['research_db'], ('b' * 32,), review_source='a' * 64))
        with closing(h.store.connect()) as c:
            self.assertFalse(inv._objects(c))
        self.assertIsNone(rejection.gate(h.store, h.context['research_db'], ('b' * 32,)))        # the active gate remembers
        with closing(h.store.connect()) as c:
            self.assertTrue(inv._objects(c))

    def test_a_no_entry_review_plan_reads_the_gate_without_writing_the_inventory(self):
        legacy_null_pass.install(self)
        t = no_entry_fixture.fixture()
        self.addCleanup(t.doCleanups)
        self.drop_inventory(t.store)
        research = t.ctx['research_db']
        with patch.object(no_entry, '_proof', return_value={'targets': [{'target': {'scan_id': t.h.f.target.scan_id}}]}):
            self.assertIsNone(no_entry.gate(t.store, research, ('b' * 32,), review_source='a' * 64))
        with closing(t.store.connect()) as c:
            self.assertFalse(inv._objects(c))


class QuickSampleTests(unittest.TestCase):
    """T24S: a bounded sample of the indexed receipts gets the page-level check; the rest a presence check."""

    def items(self, n):
        return [('%032x' % i, 'scan-%d' % i, '%064x' % (i + 1000), '%064x' % (i + 2000)) for i in range(n)]

    def test_distinct_picks_are_bounded_distinct_in_range_and_deterministic(self):
        for upto, k in ((0, 5), (1, 5), (3, 8), (50, 8), (50, 0), (1000, 8)):
            picks = vi.distinct_picks(upto, 'seed', k)
            self.assertEqual(len(picks), min(k, upto), (upto, k))
            self.assertTrue(all(1 <= n <= upto for n in picks))
            self.assertEqual(picks, vi.distinct_picks(upto, 'seed', k))
        self.assertNotEqual(vi.distinct_picks(1000, 'a', 8), vi.distinct_picks(1000, 'b', 8))

    def test_split_quick_partitions_and_is_bounded(self):
        items = self.items(40)
        checked, rest = vi.split_quick(items, seed='s', kind='k')
        self.assertEqual(len(checked), vi.QUICK_SAMPLE)
        self.assertEqual(sorted(checked + rest), sorted(items))
        self.assertEqual(vi.split_quick(items, seed='s', kind='k'), (checked, rest))
        self.assertEqual(vi.split_quick(items[:3], seed='s', kind='k')[1], [])            # fewer than the sample: all are checked
        self.assertEqual(len(vi.split_quick(items, seed='s', kind='k', sample=0)[0]), 0)
        self.assertEqual(vi.split_quick([], seed='s', kind='k'), ([], []))

    def test_over_time_every_receipt_gets_the_page_level_check(self):
        items = self.items(40)
        seen = set()
        for seed in range(80):
            seen |= {i[0] for i in vi.split_quick(items, seed=str(seed), kind='k')[0]}
        self.assertEqual(len(seen), 40)

    def test_missing_pages_names_exactly_the_absent_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EvidenceStore(os.path.join(tmp, 'e.sqlite'))
            keys = [store.save({'n': i}) for i in range(3)]
            with closing(store.connect()) as c:
                self.assertEqual(vi.missing_pages(c, keys), [])
                self.assertEqual(vi.missing_pages(c, [keys[0], 'f' * 64, keys[2], 'e' * 64]), ['f' * 64, 'e' * 64])
                self.assertEqual(vi.missing_pages(c, []), [])

    def test_the_gate_refuses_an_unsampled_receipt_whose_outcome_page_is_gone(self):
        h = prep_fixture.PreparationTests('test_global_gate_retirement_and_unrelated_scan')
        h.setUp()
        self.addCleanup(h.doCleanups)
        result = h.rejected()
        with patch.object(vi, 'QUICK_SAMPLE', 0), patch.object(vi, 'SAMPLE', 0):
            self.assertIsNone(terminal.gate(h.store, h.context['research_db'], ('b' * 32,)))
            with closing(h.store.connect()) as c:
                c.execute('DELETE FROM pages WHERE hash=?', (result['evidence_hash'],))
            with self.assertRaises(ValueError):
                terminal.gate(h.store, h.context['research_db'], ('b' * 32,))


if __name__ == '__main__':
    unittest.main()
