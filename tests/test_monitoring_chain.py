"""SYNTHETIC_TEST_ONLY: incremental, hash-chained monitoring accounting (T24R F14).

Every completed monitoring read extends an append-only chain in the same transaction that stores its outcome. Accounting then
recomputes the chain from SQL scalars (no evidence loads), re-proves only unchained rows and a bounded deterministic sample of
chained ones from their blobs. Corruption of an already-verified row must be caught by the chain even when it is not sampled.
Everything is seeded and deterministic.
"""
import os
import random
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from desk import monitoring_budget as mb
from desk import paper_terminal_reconciliation as terminal
from desk.evidence import EvidenceStore
from desk.monitoring_budget import MonitoringBlocked
from desk.model import digest
from tests import test_monitoring_budget as base


class _Fixture(base.MonitoringBudgetTests):
    pass


for _name in dir(base.MonitoringBudgetTests):      # reuse setUp/helpers only
    if _name.startswith('test_'):
        setattr(_Fixture, _name, None)

TABLES = ('paper_monitoring_budget', 'paper_monitoring_reservations', 'paper_monitoring_outcomes', mb.CHAIN_TABLE, mb.HEAD_TABLE)


def independent_chain(c):
    """The chain recomputed here from the three tables, in completion order, without using the module under test."""
    rows = c.execute(f'SELECT ch.reservation_id,r.at,r.scan_id,r.mint,r.checkpoint_hash,r.method,r.params_hash,o.evidence_hash '
                     f'FROM {mb.CHAIN_TABLE} ch JOIN paper_monitoring_reservations r ON r.id=ch.reservation_id '
                     'JOIN paper_monitoring_outcomes o ON o.reservation_id=ch.reservation_id ORDER BY ch.seq').fetchall()
    prev, out = '0' * 64, []
    for identity, at, scan, mint, checkpoint, method, params, evidence in rows:
        prev = digest({'monitoring_chain_version': 1, 'prev': prev, 'id': identity, 'at': at, 'scan': scan, 'mint': mint,
                       'checkpoint': checkpoint, 'method': method, 'params': params, 'evidence': evidence})
        out.append((identity, prev))
    return out


FULL = {'DESK_CHAIN_FULL_VERIFY': '1'}


class ChainTests(_Fixture):
    def reads(self, n):
        """n completed reads; the monitoring allowance is 60 per rolling hour, so the clock moves on between batches."""
        for _ in range(n):
            with closing(self.store.connect()) as c:
                used = c.execute('SELECT count(*) FROM paper_monitoring_reservations WHERE at>?', (self.now - 3600,)).fetchone()[0]
            if used >= 55:
                self.now += 3601
            self.read()

    def stored_chain(self):
        with closing(self.store.connect()) as c:
            return c.execute(f'SELECT reservation_id,chain_hash FROM {mb.CHAIN_TABLE} ORDER BY seq').fetchall()

    def loads_during(self, function):
        calls = []
        real = EvidenceStore.load

        def counting(store, key, *a, **k):
            calls.append(key)
            return real(store, key, *a, **k)
        with patch.object(EvidenceStore, 'load', counting):
            function()
        return len(calls)

    def corrupt(self, statement, args=()):
        """Run ``statement`` with the immutability triggers of the monitoring tables lifted, then put them back unchanged."""
        with closing(self.store.connect()) as c:
            triggers = c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN (%s)" % ','.join('?' * len(TABLES)), TABLES).fetchall()
            for name, _ in triggers:
                c.execute(f'DROP TRIGGER "{name}"')
            c.execute(statement, args)
            for name, sql in triggers:
                table = sql.split(' ON ')[1].split()[0]
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    c.execute(sql)

    # -- shape and known answers -------------------------------------------------------------------------------------
    def test_golden_vector_pins_the_chain_formula(self):
        self.assertEqual(mb._chain_hash('0' * 64, 1, 1791417600.0, 'scan', 'mint', 'c' * 64, 'getSlot', 'p' * 64, 'e' * 64),
                         '2fa3478daef4e9672251d4949d49365f687f5909fed28f55f7fd8d7689793b5a')

    def test_chain_follows_completions_and_matches_an_independent_recomputation(self):
        for _ in range(12):
            self.read()
        chain = self.stored_chain()
        self.assertEqual([i for i, _ in chain], list(range(1, 13)))
        with closing(self.store.connect()) as c:
            self.assertEqual(independent_chain(c), chain)
            covered, head = mb._chain_rows(c)
        self.assertEqual((sorted(covered), head), (list(range(1, 13)), chain[-1][1]))

    def test_the_chain_table_is_outside_the_terminal_gates_monitoring_prefix(self):
        self.read()
        self.assertFalse(mb.CHAIN_TABLE.startswith('paper_monitoring_'))
        with closing(self.store.connect()) as c:
            terminal._monitoring(c)          # raises 'Monitoring schema partial/unknown' for any unlisted paper_monitoring_* table

    def test_chain_is_append_only(self):
        self.read()
        self.read()
        for statement in (f'UPDATE {mb.CHAIN_TABLE} SET chain_hash=chain_hash', f'DELETE FROM {mb.CHAIN_TABLE}'):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'), closing(self.store.connect()) as c:
                c.execute(statement)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'), closing(self.store.connect()) as c:
            c.execute(f"INSERT INTO {mb.CHAIN_TABLE} VALUES(9,1,'{'a' * 64}')")             # gap in seq
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'), closing(self.store.connect()) as c:
            c.execute(f"INSERT INTO {mb.CHAIN_TABLE} VALUES(3,99,'{'a' * 64}')")            # reservation without an outcome

    # -- cost ----------------------------------------------------------------------------------------------------------
    def test_snapshot_blob_loads_do_not_grow_with_history(self):
        measured = {}
        done = 0
        for size in (3, 30, 90):                     # 1x / 10x / 30x
            self.reads(size - done)
            done = size
            measured[size] = self.loads_during(self.budget.snapshot)
        self.assertEqual(set(measured.values()), {mb.CHAIN_SAMPLE}, measured)

    def test_a_read_costs_a_constant_number_of_blob_loads_too(self):
        self.reads(20)
        few = self.loads_during(self.read)
        self.reads(40)
        self.assertEqual(self.loads_during(self.read), few)

    # -- property: incremental == full recompute -----------------------------------------------------------------------------------
    def test_incremental_accounting_equals_a_full_recompute_after_random_sequences(self):
        for seed in range(4):
            with self.subTest(seed=seed):
                rnd = random.Random(seed)
                for step in range(14):
                    op = rnd.choice(('ok', 'ok', 'ok', 'transient'))
                    if op == 'ok':
                        self.read()
                    else:
                        with self.assertRaises(Exception):
                            self.read(fail=True)
                    with closing(self.store.connect()) as c:
                        c.execute('BEGIN')
                        incremental = self.budget._accounting(c, checkpoint=True)
                        chain = mb._chain_rows(c)[0]
                        self.assertEqual(sorted(chain), sorted(i for (i,) in c.execute('SELECT reservation_id FROM paper_monitoring_outcomes')))
                    with patch.object(mb, 'CHAIN_SAMPLE', 10 ** 6), closing(self.store.connect()) as c:
                        c.execute('BEGIN')
                        self.assertEqual(self.budget._accounting(c, checkpoint=True), incremental)

    # -- forced corruption of an already verified row is caught by the chain, not by luck -----------------------------------------------
    def test_corrupting_a_verified_row_is_detected_even_when_it_is_not_sampled(self):
        for _ in range(6):
            self.read()
        with patch.object(mb, 'CHAIN_SAMPLE', 0), patch.object(mb, 'CHAIN_HASH_SAMPLE', 0):
            self.assertEqual(self.budget.snapshot()['blockers'], [])
            self.corrupt("UPDATE paper_monitoring_reservations SET params_hash=? WHERE id=3", ('f' * 64,))
            self.assertEqual(self.budget.snapshot()['blockers'], [])           # T24S: verified once, not re-hashed every call ...
            with patch.dict(os.environ, FULL), self.assertRaises(MonitoringBlocked) as caught:      # ... until a full verification
                self.budget.snapshot()
        self.assertEqual(caught.exception.code, 'MONITORING_ACCOUNTING_INVALID')

    def test_every_chained_field_is_covered(self):
        for column, value in (('at', 5.0), ('scan_id', 'x'), ('mint', 'x'), ('checkpoint_hash', 'x'), ('method', 'x'), ('params_hash', 'x')):
            with self.subTest(column=column):
                self.doCleanups()          # a re-run of setUp inside a subTest must release the previous fixture first
                self.setUp()
                for _ in range(4):
                    self.read()
                self.corrupt(f'UPDATE paper_monitoring_reservations SET {column}=? WHERE id=2', (value,))
                with patch.object(mb, 'CHAIN_SAMPLE', 0), patch.dict(os.environ, FULL), self.assertRaises(MonitoringBlocked):
                    self.budget.snapshot()

    def test_swapped_outcome_evidence_is_detected(self):
        for _ in range(4):
            self.read()
        with closing(self.store.connect()) as c:
            first, second = (r[0] for r in c.execute('SELECT evidence_hash FROM paper_monitoring_outcomes WHERE reservation_id IN (1,2) ORDER BY reservation_id'))
        self.corrupt('UPDATE paper_monitoring_outcomes SET evidence_hash=? WHERE reservation_id=1', (second,))
        with patch.object(mb, 'CHAIN_SAMPLE', 0), patch.dict(os.environ, FULL), self.assertRaises(MonitoringBlocked):
            self.budget.snapshot()

    def test_chain_row_tampering_is_detected(self):
        for _ in range(5):
            self.read()
        for statement, args in ((f'UPDATE {mb.CHAIN_TABLE} SET chain_hash=? WHERE seq=2', ('0' * 64,)),
                                (f'DELETE FROM {mb.CHAIN_TABLE} WHERE seq=2', ()),
                                (f'UPDATE {mb.CHAIN_TABLE} SET reservation_id=99 WHERE seq=2', ()),
                                (f'UPDATE {mb.CHAIN_TABLE} SET reservation_id=99 WHERE seq=5', ())):      # the LAST link too
            with self.subTest(statement=statement):
                self.doCleanups()          # a re-run of setUp inside a subTest must release the previous fixture first
                self.setUp()
                for _ in range(5):
                    self.read()
                self.corrupt(statement, args)
                with patch.object(mb, 'CHAIN_SAMPLE', 0), patch.dict(os.environ, FULL), self.assertRaises(MonitoringBlocked):
                    self.budget.snapshot()

    def test_chain_schema_deviation_is_refused(self):
        self.read()
        self.corrupt(f'ALTER TABLE {mb.CHAIN_TABLE} ADD COLUMN extra TEXT')
        with self.assertRaises(MonitoringBlocked):
            self.budget.snapshot()

    def test_dropping_the_last_chain_row_but_not_the_head_is_refused(self):
        for _ in range(4):
            self.read()
        self.corrupt(f'DELETE FROM {mb.CHAIN_TABLE} WHERE seq=4')                 # the head still vouches for link 4
        with patch.object(mb, 'CHAIN_SAMPLE', 0), self.assertRaises(MonitoringBlocked):
            self.budget.snapshot()

    def test_dropping_the_last_chain_row_and_rewinding_the_head_is_harmless_because_that_row_is_then_proved_from_its_blob(self):
        for _ in range(4):
            self.read()
        self.corrupt(f'DELETE FROM {mb.CHAIN_TABLE} WHERE seq=4')
        self.corrupt(f'UPDATE {mb.HEAD_TABLE} SET seq=3,chain_hash=(SELECT chain_hash FROM {mb.CHAIN_TABLE} WHERE seq=3)')
        with patch.object(mb, 'CHAIN_SAMPLE', 0):
            self.assertEqual(self.loads_during(self.budget.snapshot), 1)       # row 4 now unchained: one full proof
            self.assertEqual(self.budget.snapshot()['blockers'], [])

    # -- nothing is waived for rows without a chain entry ------------------------------------------------------------------------------
    def test_a_store_without_a_chain_is_fully_verified_and_then_adopts_the_chain_for_new_rows_only(self):
        for _ in range(5):
            self.read()
        self.corrupt(f'DROP TABLE {mb.CHAIN_TABLE}')
        with patch.object(mb, 'CHAIN_SAMPLE', 0), self.assertRaises(MonitoringBlocked):       # a surviving head without its chain is tampering
            self.budget.snapshot()
        self.corrupt(f'DROP TABLE {mb.HEAD_TABLE}')                                              # an older store has neither
        with patch.object(mb, 'CHAIN_SAMPLE', 0):
            self.assertEqual(self.loads_during(self.budget.snapshot), 5)        # every old row is proved from its blob
            self.read()
            self.assertEqual([i for i, _ in self.stored_chain()], [6])
            self.assertEqual(self.loads_during(self.budget.snapshot), 5)        # the five legacy rows are still proved each time
        self.corrupt("UPDATE paper_monitoring_reservations SET params_hash=? WHERE id=1", ('f' * 64,))
        with self.assertRaises(MonitoringBlocked):
            self.budget.snapshot()

    def test_a_failed_chain_append_rolls_the_outcome_back(self):
        self.read()
        with patch.object(mb, '_chain_append', side_effect=MonitoringBlocked('MONITORING_ACCOUNTING_INVALID')), self.assertRaises(Exception):
            self.read()
        with closing(self.store.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM paper_monitoring_outcomes').fetchone()[0], 1)
            self.assertEqual(c.execute(f'SELECT count(*) FROM {mb.CHAIN_TABLE}').fetchone()[0], 1)

    def test_sample_is_deterministic_bounded_and_moves_with_the_head(self):
        for _ in range(10):
            self.read()
        with closing(self.store.connect()) as c:
            count, head, _ = mb._chain_state(c)
        self.assertEqual(count, 10)
        first = mb._chain_sample(count, head)
        self.assertEqual(first, mb._chain_sample(count, head))
        self.assertEqual(len(first), mb.CHAIN_SAMPLE)
        self.assertTrue(first <= set(range(1, count + 1)))
        seen = set()
        for i in range(80):
            seen |= mb._chain_sample(count, str(i))
        self.assertEqual(seen, set(range(1, count + 1)), 'over time every chained row is re-proved from its blob')


if __name__ == '__main__':
    unittest.main()
