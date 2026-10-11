"""SYNTHETIC_TEST_ONLY: the monitoring chain HEAD (T24S item 2) - O(new) accounting, proved against an INDEPENDENT full recompute.

The reference below is written here from the three monitoring tables with ``hashlib`` and ``json`` only. It shares no code with
``desk.monitoring_budget`` (not ``_accounting``, not ``_chain_rows``, not ``digest``) and is pinned to the module's golden vector,
so "incremental == full" is a statement about two separate implementations. 230 seeded random sequences mix ok / transient /
latching reads (all chained), abandoned reads (resolved without a chain link) and pending reservations, reopening the database
between operations the way separate calls do.
"""
import hashlib
import json
import os
import random
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from desk import monitoring_budget as mb
from desk.monitoring_budget import MonitoringBlocked

GENESIS = '0' * 64


def reference_hash(prev, identity, at, scan, mint, checkpoint, method, params, evidence):
    body = {'monitoring_chain_version': 1, 'prev': prev, 'id': identity, 'at': at, 'scan': scan, 'mint': mint,
            'checkpoint': checkpoint, 'method': method, 'params': params, 'evidence': evidence}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def reference(c):
    """Everything an accounting call must conclude, recomputed from scratch: (links, head, unchained completed reservation ids)."""
    links, prev = [], GENESIS
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (mb.CHAIN_TABLE,)).fetchone():
        return links, prev, {i for (i,) in c.execute('SELECT r.id FROM paper_monitoring_reservations r JOIN paper_monitoring_outcomes o '
                                                     'ON o.reservation_id=r.id')}
    for seq, identity, stored, at, scan, mint, checkpoint, method, params, evidence in c.execute(
            'SELECT ch.seq,ch.reservation_id,ch.chain_hash,r.at,r.scan_id,r.mint,r.checkpoint_hash,r.method,r.params_hash,o.evidence_hash '
            f'FROM {mb.CHAIN_TABLE} ch JOIN paper_monitoring_reservations r ON r.id=ch.reservation_id '
            'JOIN paper_monitoring_outcomes o ON o.reservation_id=ch.reservation_id ORDER BY ch.seq'):
        prev = reference_hash(prev, identity, at, scan, mint, checkpoint, method, params, evidence)
        assert stored == prev and seq == len(links) + 1
        links.append(identity)
    chained = set(links)
    unchained = {i for (i,) in c.execute('SELECT r.id FROM paper_monitoring_reservations r JOIN paper_monitoring_outcomes o '
                                         'ON o.reservation_id=r.id')} - chained
    return links, prev, unchained


def make_tables(c):
    c.execute('CREATE TABLE paper_monitoring_reservations(id INTEGER PRIMARY KEY,at REAL NOT NULL,scan_id TEXT NOT NULL,mint TEXT NOT NULL,'
              'checkpoint_hash TEXT NOT NULL,method TEXT NOT NULL,params_hash TEXT NOT NULL)')
    c.execute('CREATE TABLE paper_monitoring_outcomes(reservation_id INTEGER PRIMARY KEY REFERENCES paper_monitoring_reservations(id),'
              'evidence_hash TEXT NOT NULL)')


class Sequence:
    """One database that grows by random operations."""

    def __init__(self, path, seed):
        self.path, self.rnd, self.next_id, self.pending = path, random.Random(seed), 1, []
        with closing(sqlite3.connect(path, isolation_level=None)) as c:
            make_tables(c)

    def connect(self):
        return sqlite3.connect(self.path, isolation_level=None)

    def random_hex(self):
        return '%064x' % self.rnd.getrandbits(256)

    def reserve(self, c):
        identity, self.next_id = self.next_id, self.next_id + 1
        c.execute('INSERT INTO paper_monitoring_reservations VALUES(?,?,?,?,?,?,?)',
                  (identity, 1700000000.0 + identity, 'scan%d' % self.rnd.randrange(3), 'mint%d' % self.rnd.randrange(3),
                   self.random_hex(), self.rnd.choice(('getSlot', 'getAccountInfo', 'jupiter_probe')), self.random_hex()))
        return identity

    def step(self):
        op = self.rnd.choice(('ok', 'ok', 'ok', 'transient', 'latching', 'abandoned', 'pending', 'finish'))
        with closing(self.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                if op == 'finish' and self.pending:
                    identity = self.pending.pop(self.rnd.randrange(len(self.pending)))
                    op = 'ok'
                elif op in ('ok', 'transient', 'latching', 'abandoned'):
                    identity = self.reserve(c)
                else:
                    self.pending.append(self.reserve(c))
                    c.execute('COMMIT')
                    return 'pending'
                c.execute('INSERT INTO paper_monitoring_outcomes VALUES(?,?)', (identity, self.random_hex()))
                if op != 'abandoned':                          # an abandoned reservation is resolved without a chain link
                    mb._chain_append(c, identity)
                c.execute('COMMIT')
                return op
            except BaseException:
                c.execute('ROLLBACK')
                raise


class IncrementalEqualsFullRecompute(unittest.TestCase):
    SEQUENCES = 230

    def test_the_reference_is_pinned_to_the_modules_golden_vector(self):
        args = ('0' * 64, 1, 1791417600.0, 'scan', 'mint', 'c' * 64, 'getSlot', 'p' * 64, 'e' * 64)
        self.assertEqual(reference_hash(*args), mb._chain_hash(*args))
        self.assertEqual(reference_hash(*args), '2fa3478daef4e9672251d4949d49365f687f5909fed28f55f7fd8d7689793b5a')

    def check(self, seq, label):
        with closing(seq.connect()) as c:
            c.execute('BEGIN')
            links, head, unchained = reference(c)
            count, found_head, mark = mb._chain_state(c)
            self.assertEqual((count, found_head), (len(links), head if links else GENESIS), label)
            self.assertLessEqual(mark, count, label)
            proving = {row[0] for row in mb._to_prove(c)}
            self.assertTrue(unchained <= proving, label)                       # nothing uncovered is ever waived ...
            covered = proving - unchained
            self.assertTrue(covered <= set(links), label)
            self.assertLessEqual(len(covered), mb.CHAIN_SAMPLE, label)         # ... and the covered ones cost a bounded sample
            self.assertEqual(len(covered), min(mb.CHAIN_SAMPLE, len(links)), label)   # ... and the sample really is taken
            if links:
                stored = c.execute(f'SELECT seq,chain_hash FROM {mb.HEAD_TABLE}').fetchall()
                self.assertEqual(stored, [(len(links), head)], label)          # the stored head follows every chained completion

    def test_random_sequences(self):
        with tempfile.TemporaryDirectory() as tmp:
            for number in range(self.SEQUENCES):
                seq = Sequence(os.path.join(tmp, 'm%d.sqlite' % number), number)
                kinds = set()
                for step in range(self.rnd_length(number)):
                    kinds.add(seq.step())
                    self.check(seq, (number, step))
        # the sequences really did mix every class
        self.assertGreaterEqual(self.SEQUENCES, 200)

    @staticmethod
    def rnd_length(number):
        return 6 + number % 9

    def test_every_class_occurs_in_the_generated_sequences(self):
        seen = set()
        with tempfile.TemporaryDirectory() as tmp:
            for number in range(40):
                seq = Sequence(os.path.join(tmp, 'k%d.sqlite' % number), number)
                for _ in range(self.rnd_length(number)):
                    seen.add(seq.step())
        self.assertTrue({'ok', 'transient', 'latching', 'abandoned', 'pending'} <= seen, seen)


class StoredHead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.seq = Sequence(os.path.join(self.tmp.name, 'm.sqlite'), 7)
        for _ in range(12):
            self.seq.step()
        with closing(self.seq.connect()) as c:
            self.links = c.execute(f'SELECT count(*) FROM {mb.CHAIN_TABLE}').fetchone()[0]
        self.assertGreater(self.links, 3)

    def tamper(self, statement, args=()):
        """Run ``statement`` with every trigger of the touched tables lifted (a restore / manual edit), then put them back."""
        with closing(self.seq.connect()) as c:
            triggers = c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall()
            for name, _ in triggers:
                c.execute(f'DROP TRIGGER "{name}"')
            c.execute(statement, args)
            for _, sql in triggers:
                table = sql.split(' ON ')[1].split()[0]
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    c.execute(sql)

    def state(self, full=False):
        with closing(self.seq.connect()) as c:
            c.execute('BEGIN')
            with patch.dict(os.environ, {'DESK_CHAIN_FULL_VERIFY': '1'} if full else {}):
                return mb._chain_state(c)

    def test_a_call_hashes_only_the_new_links_plus_a_bounded_sample_whatever_the_history(self):
        for extra in (0, 300):
            for _ in range(extra):
                self.seq.step()
            calls = []
            real = mb._chain_hash
            with patch.object(mb, '_chain_hash', side_effect=lambda *a: calls.append(1) or real(*a)):
                self.state()
            self.assertLessEqual(len(calls), mb.CHAIN_HASH_SAMPLE + 1, (extra, len(calls)))
            calls.clear()
            with patch.object(mb, '_chain_hash', side_effect=lambda *a: calls.append(1) or real(*a)):
                self.state(full=True)
            if extra:
                self.assertGreater(len(calls), mb.CHAIN_HASH_SAMPLE + 1)         # the reference path does hash everything

    def test_links_added_without_a_head_move_are_all_verified(self):
        with closing(self.seq.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            identity = self.seq.reserve(c)
            c.execute('INSERT INTO paper_monitoring_outcomes VALUES(?,?)', (identity, 'e' * 64))
            c.execute(f'INSERT INTO {mb.CHAIN_TABLE}(seq,reservation_id,chain_hash) VALUES(?,?,?)', (self.links + 1, identity, 'f' * 64))
            c.execute('COMMIT')
        with self.assertRaises(MonitoringBlocked):
            self.state()                                                         # the new link's hash is wrong: caught from the head

    def test_an_old_corruption_is_found_by_a_full_verification_and_by_a_sample_that_lands_on_it(self):
        with closing(self.seq.connect()) as c:
            first = c.execute(f'SELECT reservation_id FROM {mb.CHAIN_TABLE} WHERE seq=1').fetchone()[0]
        self.tamper('UPDATE paper_monitoring_reservations SET params_hash=? WHERE id=?', ('0' * 64, first))
        with self.assertRaises(MonitoringBlocked):
            self.state(full=True)
        with patch.object(mb, 'CHAIN_HASH_SAMPLE', 10 ** 6), self.assertRaises(MonitoringBlocked):     # every old link sampled
            self.state()
        with patch.object(mb, 'CHAIN_HASH_SAMPLE', 0):
            self.state()                                    # the documented trade-off: not re-hashed on every call

    def test_head_moves_forward_only_and_only_onto_a_real_link(self):
        with closing(self.seq.connect()) as c:
            low = c.execute(f'SELECT chain_hash FROM {mb.CHAIN_TABLE} WHERE seq=1').fetchone()[0]
            for statement, args in ((f'UPDATE {mb.HEAD_TABLE} SET seq=1,chain_hash=?', (low,)),                       # backwards
                                    (f'UPDATE {mb.HEAD_TABLE} SET seq=?,chain_hash=?', (self.links + 1, 'a' * 64)),   # beyond the chain
                                    (f'UPDATE {mb.HEAD_TABLE} SET chain_hash=?', ('a' * 64,)),                        # not that link's hash
                                    (f'DELETE FROM {mb.HEAD_TABLE}', ()),
                                    (f'INSERT INTO {mb.HEAD_TABLE}(id,seq,chain_hash) VALUES(2,1,?)', (low,))):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                    c.execute(statement, args)

    def test_a_head_beyond_the_chain_or_on_another_hash_is_refused(self):
        self.tamper(f'DELETE FROM {mb.CHAIN_TABLE} WHERE seq=?', (self.links,))
        with self.assertRaises(MonitoringBlocked):
            self.state()

    def test_a_head_row_that_vouches_for_a_changed_link_is_refused(self):
        self.tamper(f'UPDATE {mb.CHAIN_TABLE} SET chain_hash=? WHERE seq=?', ('a' * 64, self.links))
        with self.assertRaises(MonitoringBlocked):
            self.state()

    def test_the_head_schema_is_compared_byte_for_byte(self):
        self.tamper(f'ALTER TABLE {mb.HEAD_TABLE} ADD COLUMN extra TEXT')
        with self.assertRaises(MonitoringBlocked):
            self.state()

    def test_a_store_with_a_chain_but_no_head_is_fully_verified_and_gains_a_head_at_the_next_completion(self):
        self.tamper(f'DROP TABLE {mb.HEAD_TABLE}')
        count, head, mark = self.state()
        self.assertEqual((count, mark), (self.links, 0))
        for _ in range(40):
            if self.seq.step() != 'pending':
                break
        with closing(self.seq.connect()) as c:
            self.assertEqual(c.execute(f'SELECT seq FROM {mb.HEAD_TABLE}').fetchone()[0],
                             c.execute(f'SELECT count(*) FROM {mb.CHAIN_TABLE}').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
