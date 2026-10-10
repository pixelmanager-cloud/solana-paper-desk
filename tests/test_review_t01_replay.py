"""T12 review of T01: properties the review confirmed hold (green regression guards, not defects)."""
import unittest
from contextlib import closing
from pathlib import Path

from desk import paper_cycle_no_entry as no_entry, paper_terminal_reconciliation as terminal
from tests.test_cycle_no_entry import fixture


class ReceiptReplayTests(unittest.TestCase):
    def setUp(self):
        self.t = fixture()
        self.addCleanup(self.t.doCleanups)
        self.store, self.research = self.t.store, self.t.ctx['research_db']
        with closing(self.store.connect()) as c:
            rows = c.execute('SELECT id,intent_hash,outcome_hash FROM paper_observation_passes WHERE outcome_hash IS NOT NULL').fetchall()
        self.identity, self.intent, self.outcome = [r for r in rows if terminal._load(self.store, r[2]).get('kind') == no_entry.KIND][0]

    def latched(self):
        with self.assertRaises(ValueError):
            terminal.gate(self.store, self.research, ())

    def test_baseline_gate_is_clear(self):
        self.assertIsNone(terminal.gate(self.store, self.research, ()))

    def test_receipt_replayed_onto_a_second_pass_with_the_same_intent_is_refused(self):
        with closing(self.store.connect()) as c:
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,?)', ('b' * 32, self.intent, self.outcome))
        self.latched()

    def test_replayed_receipt_with_a_matching_table_row_is_refused(self):
        with closing(self.store.connect()) as c:
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,?)', ('b' * 32, self.intent, self.outcome))
            c.execute(f'INSERT INTO {no_entry.TABLE} VALUES(?,?,?,?)', ('b' * 32, 'another-scan', self.intent, self.outcome))
        self.latched()

    def test_receipt_moved_to_a_consistently_renamed_pass_is_refused(self):
        """Pass row and receipt row both renamed (guards recreated): only the record's own pass_id binds it."""
        with closing(self.store.connect()) as c:
            for (name,) in list(c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (no_entry.TABLE,))):
                c.execute(f'DROP TRIGGER {name}')
            c.execute('UPDATE paper_observation_passes SET id=? WHERE id=?', ('b' * 32, self.identity))
            c.execute(f'UPDATE {no_entry.TABLE} SET pass_id=? WHERE pass_id=?', ('b' * 32, self.identity))
            for sql in no_entry.GUARDS.values():
                c.execute(sql)
        self.latched()

    def test_original_pass_is_only_ever_filled_never_rewritten(self):
        """The one UPDATE publish performs is NULL -> value, guarded by rowcount == 1."""
        source = Path(no_entry.__file__).read_text()
        self.assertIn("outcome_hash=? WHERE id=? AND intent_hash=? AND outcome_hash IS NULL", source)
        self.assertNotIn('DELETE FROM', source.replace("BEFORE DELETE", ''))
        self.assertNotIn('requests_used=', source.replace("'requests_used'", ''))   # never writes a charge back

    def test_admission_charge_is_untouched_by_publication_and_gate(self):
        scan = self.t.h.f.target.scan_id
        before = self.t.progress.admission(scan)
        terminal.gate(self.store, self.research, (scan,))
        self.assertEqual(self.t.progress.admission(scan), before)
        self.assertGreater(before['requests_used'], 0)


if __name__ == '__main__':
    unittest.main()
