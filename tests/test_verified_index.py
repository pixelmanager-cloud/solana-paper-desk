"""SYNTHETIC_TEST_ONLY: append-only verified-digest index for retained terminal receipts (T24R F7).

Real stores: a retained history-preparation rejection (tests.test_history_preparation_phase) and a retained cycle no-entry
(tests.test_cycle_no_entry), both published by the production code. The index must make a gate cheap WITHOUT waiving anything:
unindexed receipts are always replayed, an index that disagrees with a receipt fails closed, the sample is deterministic and
bounded, and a full replay is always available.
"""
import sqlite3
import unittest
import zlib
from contextlib import closing
from unittest.mock import patch

from desk import history_preparation_rejection as rejection
from desk import paper_cycle_no_entry as no_entry
from desk import paper_terminal_reconciliation as terminal
from desk import verified_index as vi
from desk.model import digest
from tests import legacy_null_pass
from tests import test_cycle_no_entry as no_entry_fixture
from tests import test_history_preparation_phase as prep_fixture

PASS = 'a' * 32


def memory():
    c = sqlite3.connect(':memory:')
    return c


def item(n, outcome=None):
    return ('%032x' % n, 'scan-%d' % n, '%064x' % (n + 1000), outcome or '%064x' % (n + 2000))


def indexed(c, kind, items):
    for pass_id, scan, intent, outcome in items:
        vi.record(c, kind, pass_id, outcome, vi.proof_digest(kind, outcome, pass_id, scan, intent))


class IndexUnitTests(unittest.TestCase):
    def test_schema_is_exact_append_only_and_contiguous(self):
        c = memory()
        self.assertEqual(vi.rows(c), {})
        items = [item(i) for i in range(3)]
        indexed(c, 'k', items)
        self.assertEqual(sorted(i for (_, i) in vi.rows(c)), sorted(i[0] for i in items))
        for statement in (f'UPDATE {vi.TABLE} SET proof_digest=proof_digest', f'DELETE FROM {vi.TABLE}'):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
                c.execute(statement)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):       # a gap in the ids
            c.execute(f"INSERT INTO {vi.TABLE} VALUES(9,'k','{'b' * 32}','{'c' * 64}','{'d' * 64}')")
        with self.assertRaises(sqlite3.IntegrityError):                            # same (kind, pass) twice
            vi.record(c, 'k', items[0][0], items[0][3], 'e' * 64)

    def test_any_schema_deviation_is_refused(self):
        for tamper in (f'ALTER TABLE {vi.TABLE} ADD COLUMN extra TEXT', f'DROP TRIGGER {vi.TABLE}_update',
                       f'CREATE INDEX stray ON {vi.TABLE}(kind)', f'CREATE TABLE {vi.TABLE}_shadow(x)'):
            with self.subTest(tamper=tamper):
                c = memory()
                indexed(c, 'k', [item(1)])
                c.execute(tamper)
                with self.assertRaisesRegex(ValueError, 'schema malformed'):
                    vi.rows(c)

    def test_scalar_and_contiguity_tampering_is_refused(self):
        c = memory()
        indexed(c, 'k', [item(1), item(2)])
        for name in vi.GUARDS:
            c.execute(f'DROP TRIGGER {name}')
        c.execute(f'UPDATE {vi.TABLE} SET id=7 WHERE id=2')
        with self.assertRaisesRegex(ValueError, 'schema malformed|not contiguous'):
            vi.rows(c)
        c = memory()
        indexed(c, 'k', [item(1)])
        for name in vi.GUARDS:
            c.execute(f'DROP TRIGGER {name}')
        c.execute(f"UPDATE {vi.TABLE} SET proof_digest='short'")
        with self.assertRaisesRegex(ValueError, 'schema malformed|scalar malformed'):
            vi.rows(c)

    def test_proof_digest_binds_every_identity_field(self):
        base = vi.proof_digest('k', 'o' * 64, 'p' * 32, 's', 'i' * 64)
        for other in (('x', 'o' * 64, 'p' * 32, 's', 'i' * 64), ('k', 'x' * 64, 'p' * 32, 's', 'i' * 64),
                      ('k', 'o' * 64, 'x' * 32, 's', 'i' * 64), ('k', 'o' * 64, 'p' * 32, 'x', 'i' * 64),
                      ('k', 'o' * 64, 'p' * 32, 's', 'x' * 64)):
            self.assertNotEqual(vi.proof_digest(*other), base)
        self.assertEqual(vi.proof_digest('k', 'o' * 64, 'p' * 32, 's', 'i' * 64), base)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.c = memory()
        self.items = [item(i) for i in range(40)]
        indexed(self.c, 'k', self.items[:30])           # 30 indexed, 10 never indexed
        self.index = vi.rows(self.c)

    def test_unindexed_receipts_are_always_replayed_and_the_sample_is_bounded(self):
        replay, quick = vi.plan(self.index, 'k', self.items, seed='s')
        replayed = {i[0] for i in replay}
        self.assertTrue({i[0] for i in self.items[30:]} <= replayed)
        self.assertEqual(len(replay), 10 + vi.SAMPLE)
        self.assertEqual(len(quick), 30 - vi.SAMPLE)
        self.assertEqual(sorted(i[0] for i in replay + quick), sorted(i[0] for i in self.items))

    def test_the_sample_is_deterministic_and_moves_with_the_seed(self):
        a = vi.plan(self.index, 'k', self.items, seed='s')
        self.assertEqual(a, vi.plan(self.index, 'k', self.items, seed='s'))
        self.assertEqual(vi.plan(self.index, 'k', list(reversed(self.items)), seed='s')[0][-vi.SAMPLE:].__len__(), vi.SAMPLE)
        seen = set()
        for seed in range(60):
            seen |= {i[0] for i in vi.plan(self.index, 'k', self.items, seed=str(seed))[0]} - {i[0] for i in self.items[30:]}
        self.assertEqual(len(seen), 30, 'over time every indexed receipt is re-proved')

    def test_sample_size_and_full_override(self):
        replay, quick = vi.plan(self.index, 'k', self.items, seed='s', sample=0)
        self.assertEqual((len(replay), len(quick)), (10, 30))
        replay, quick = vi.plan(self.index, 'k', self.items, seed='s', full=True)
        self.assertEqual((len(replay), len(quick)), (40, 0))
        with patch.object(vi, 'SAMPLE', 3):
            self.assertEqual(len(vi.plan(self.index, 'k', self.items, seed='s')[0]), 13)

    def test_kinds_do_not_leak_into_each_other(self):
        replay, quick = vi.plan(self.index, 'other', self.items[:5], seed='s')
        self.assertEqual((len(replay), len(quick)), (5, 0))

    def test_an_index_row_without_a_retained_receipt_or_with_another_outcome_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'without a retained receipt'):
            vi.plan(self.index, 'k', self.items[1:30], seed='s')
        altered = list(self.items)
        altered[3] = item(3, outcome='f' * 64)
        with self.assertRaisesRegex(ValueError, 'disagrees'):
            vi.plan(self.index, 'k', altered, seed='s')
        other_intent = list(self.items)
        other_intent[4] = (other_intent[4][0], other_intent[4][1], 'e' * 64, other_intent[4][3])
        with self.assertRaisesRegex(ValueError, 'disagrees'):
            vi.plan(self.index, 'k', other_intent, seed='s')

    def test_forced_full_replay_environment_switch(self):
        self.assertFalse(vi.full_replay_forced())
        with patch.dict('os.environ', {'DESK_GATE_FULL_REPLAY': '1'}):
            self.assertTrue(vi.full_replay_forced())
        with patch.dict('os.environ', {'DESK_GATE_FULL_REPLAY': 'yes'}):
            self.assertFalse(vi.full_replay_forced())


class GateBehaviour:
    """Shared adversarial scenarios; subclasses bind a real store holding exactly ONE retained receipt of their kind."""
    KIND = None

    def calls(self):
        raise NotImplementedError

    def run_gate(self, **kw):
        raise NotImplementedError

    def index_rows(self):
        with closing(self.store.connect()) as c:
            return vi.rows(c)

    def raw(self, statement, args=(), drop_guards=False, keep_schema=True):
        """Run SQL on the evidence store; with drop_guards the index triggers are lifted for the statement and (by default)
        restored afterwards, so the test tampers with ROWS while the schema stays exactly as published."""
        with closing(self.store.connect()) as c:
            if drop_guards:
                for name in vi.GUARDS:
                    c.execute(f'DROP TRIGGER IF EXISTS {name}')
            c.execute(statement, args)
            if drop_guards and keep_schema and c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (vi.TABLE,)).fetchone():
                for sql in vi.GUARDS.values():
                    c.execute(sql)

    def tamper_attempt_page(self, key):
        raw = b'{"tampered":1}'
        self.raw('UPDATE pages SET payload=?,raw_bytes=? WHERE hash=?', (zlib.compress(raw), len(raw), key))

    # -- the receipt was indexed at publication -------------------------------------------------------------------------
    def test_publication_recorded_the_digest_of_what_was_proved(self):
        (((kind, pass_id), (outcome, stored)),) = self.index_rows().items()
        self.assertEqual((kind, pass_id, outcome), (self.KIND, self.pass_id, self.outcome))
        self.assertEqual(stored, vi.proof_digest(self.KIND, self.outcome, self.pass_id, self.scan, self.intent))

    def test_default_gate_replays_a_small_store_completely(self):
        self.assertIsNone(self.run_gate())
        self.assertEqual(self.calls(), 1, 'one receipt is inside the sample, so it is proved in full as before')

    # -- the cheap path ---------------------------------------------------------------------------------------------------
    def test_indexed_receipt_outside_the_sample_is_only_bound_not_replayed(self):
        with patch.object(vi, 'SAMPLE', 0):
            self.assertIsNone(self.run_gate())
            self.assertEqual(self.calls(), 0)
            self.assertEqual(self.run_gate(scans=(self.scan,)), 'REJECTED_SCAN_RETIRED')

    def test_quick_path_loads_far_fewer_pages_than_a_replay(self):
        def loads(**kw):
            count = [0]
            real = terminal._load

            def counting(*a, **k):
                count[0] += 1
                return real(*a, **k)
            with patch.object(terminal, '_load', side_effect=counting):
                self.run_gate(**kw)
            return count[0]
        with patch.object(vi, 'SAMPLE', 0):
            quick = loads(direct=True)
        self.assertLess(quick, loads(full=True) / 2)

    def test_the_boundary_a_tampered_original_is_caught_by_sample_or_full_replay_only(self):
        self.tamper_attempt_page(self.attempt)
        with patch.object(vi, 'SAMPLE', 0):
            self.assertIsNone(self.run_gate(), 'outside the sample the original attempt pages are not re-read (documented limit)')
            with self.assertRaises(ValueError):
                self.run_gate(full=True)
            with patch.dict('os.environ', {'DESK_GATE_FULL_REPLAY': '1'}), self.assertRaises(ValueError):
                self.run_gate()
        with self.assertRaises(ValueError):                  # inside the default sample (1 receipt <= SAMPLE) it is caught
            self.run_gate()

    # -- nothing is waived ------------------------------------------------------------------------------------------------
    def test_receipt_without_an_index_row_is_replayed_every_time(self):
        self.raw(f'DROP TABLE {vi.TABLE}', drop_guards=True)   # table gone: nothing to restore
        with patch.object(vi, 'SAMPLE', 0):
            self.assertIsNone(self.run_gate())
            self.assertEqual(self.calls(), 1)
            self.tamper_attempt_page(self.attempt)
            with self.assertRaises(ValueError):
                self.run_gate()

    def test_index_rows_that_disagree_with_the_receipt_fail_closed(self):
        with patch.object(vi, 'SAMPLE', 0):
            self.raw(f"UPDATE {vi.TABLE} SET outcome_hash=?", ('f' * 64,), drop_guards=True)
            with self.assertRaisesRegex(ValueError, 'disagrees'):
                self.run_gate()

    def test_index_row_without_a_receipt_fails_closed(self):
        with patch.object(vi, 'SAMPLE', 0):
            self.raw(f"UPDATE {vi.TABLE} SET pass_id=?", ('c' * 32,), drop_guards=True)
            with self.assertRaisesRegex(ValueError, 'without a retained receipt'):
                self.run_gate()

    def test_forged_proof_digest_fails_closed(self):
        with patch.object(vi, 'SAMPLE', 0):
            self.raw(f"UPDATE {vi.TABLE} SET proof_digest=?", ('0' * 64,), drop_guards=True)
            with self.assertRaisesRegex(ValueError, 'disagrees'):
                self.run_gate()

    def test_deleting_the_index_row_only_costs_a_full_replay(self):
        self.raw(f'DELETE FROM {vi.TABLE}', drop_guards=True)
        with patch.object(vi, 'SAMPLE', 0):
            self.assertIsNone(self.run_gate())
            self.assertEqual(self.calls(), 1)

    def test_replaced_outcome_page_is_caught_even_on_the_quick_path(self):
        raw = b'{"tampered":1}'
        self.raw('UPDATE pages SET payload=?,raw_bytes=? WHERE hash=?', (zlib.compress(raw), len(raw), self.outcome))
        with patch.object(vi, 'SAMPLE', 0), self.assertRaises(ValueError):
            self.run_gate()


class HistoryRejectionGateTests(GateBehaviour, unittest.TestCase):
    KIND = rejection.KIND

    def setUp(self):
        self.h = prep_fixture.PreparationTests('test_global_gate_retirement_and_unrelated_scan')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        result = self.h.rejected()
        self.store, self.research = self.h.store, self.h.context['research_db']
        self.pass_id, self.outcome, self.scan = result['pass_id'], result['evidence_hash'], result['scan_id']
        self.intent, self.attempt = result['intent_hash'], result['attempt_refs'][0]

    def calls(self):
        return self.count[0]

    def run_gate(self, scans=('b' * 32,), full=False, direct=False):
        self.count = getattr(self, 'count', [0])
        self.count[0] = 0
        real = rejection.verify

        def counting(*a, **k):
            self.count[0] += 1
            return real(*a, **k)
        with patch.object(rejection, 'verify', side_effect=counting):
            if full or direct:
                return rejection.gate(self.store, self.research, scans, full=full)
            return terminal.gate(self.store, self.research, scans)


class NoEntryGateTests(GateBehaviour, unittest.TestCase):
    KIND = no_entry.INDEX_KIND

    def setUp(self):
        legacy_null_pass.install(self)
        self.t = no_entry_fixture.fixture()
        self.addCleanup(self.t.doCleanups)
        self.store, self.research = self.t.store, self.t.ctx['research_db']
        self.scan = self.t.h.f.target.scan_id
        with closing(self.store.connect()) as c:
            rows = no_entry.rows(c)
        (self.pass_id, scan, self.intent, self.outcome), = rows
        self.attempt = terminal._load(self.store, self.outcome)['attempt_refs'][0]
        self.count = [0]

    def calls(self):
        return self.count[0]

    def run_gate(self, scans=('b' * 32,), full=False, direct=False):
        self.count[0] = 0
        real = no_entry._proof

        def counting(*a, **k):
            self.count[0] += 1
            return real(*a, **k)
        with patch.object(no_entry, '_proof', side_effect=counting):
            if full or direct:
                return no_entry.gate(self.store, self.research, scans, full=full)
            return terminal.gate(self.store, self.research, scans)


class PublicationAtomicityTests(unittest.TestCase):
    def test_a_failed_index_write_rolls_back_the_receipt_and_the_pass_stays_unretired(self):
        h = prep_fixture.PreparationTests('test_global_gate_retirement_and_unrelated_scan')
        h.setUp()
        self.addCleanup(h.doCleanups)
        with patch.object(vi, 'record', side_effect=ValueError('index write failed')), \
                self.assertRaises(ValueError):
            h.rejected()
        with closing(h.store.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM paper_observation_passes WHERE outcome_hash IS NOT NULL').fetchone()[0], 0)
            self.assertEqual(rejection.rows(c) if c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (rejection.TABLE,)).fetchone() else [], [])


class StoreGrowthTests(unittest.TestCase):
    """No store-wide page-count latch; warnings at 80% of the advisory limits, visible in the refusal log."""

    def setUp(self):
        self.h = prep_fixture.PreparationTests('test_global_gate_retirement_and_unrelated_scan')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.rejected()

    def test_headroom_reports_pages_and_bytes(self):
        h = no_entry.storage_headroom(self.h.store)
        with closing(self.h.store.connect()) as c:
            pages = c.execute('SELECT count(*) FROM pages').fetchone()[0]
        self.assertEqual((h['pages'], h['pages_limit'], h['pages_warn'], h['bytes_warn']), (pages, no_entry.PAGE_SOFT_LIMIT, False, False))

    def test_warning_rows_appear_at_eighty_percent_of_pages_and_of_bytes(self):
        with closing(self.h.store.connect()) as c:
            pages = c.execute('SELECT count(*) FROM pages').fetchone()[0]
        with patch.object(no_entry, 'PAGE_SOFT_LIMIT', int(pages / 0.8) + 1):
            self.assertTrue(no_entry.warn_if_crowded(self.h.store)['pages_warn'])
        with patch.object(self.h.store, 'max_bytes', 1000):
            self.assertTrue(no_entry.warn_if_crowded(self.h.store)['bytes_warn'])
        rows = no_entry.read_refusals(self.h.store.path)
        self.assertEqual({(r['kind'], r['table']) for r in rows}, {(no_entry.WARNING_KIND, 'evidence_pages'), (no_entry.WARNING_KIND, 'evidence_bytes')})
        self.assertEqual({r['limit'] for r in rows if r['table'] == 'evidence_pages'}, {int(pages / 0.8) + 1})

    def test_no_warning_below_the_threshold(self):
        no_entry.warn_if_crowded(self.h.store)
        self.assertEqual(no_entry.read_refusals(self.h.store.path), [])

    def test_a_measurement_failure_never_changes_the_outcome(self):
        with patch.object(self.h.store, 'connect', side_effect=sqlite3.OperationalError('locked')):
            self.assertIsNone(no_entry.warn_if_crowded(self.h.store))

    def test_gate_passes_with_far_more_pages_than_the_old_hard_limit(self):
        for i in range(4200):
            self.h.store.save({'filler': i})
        self.assertIsNone(terminal.gate(self.h.store, self.h.context['research_db'], ('b' * 32,)))


class AttemptWindowTests(unittest.TestCase):
    """The attempt inventory is read from the pages written while ONE preparation ran (intent .. outcome)."""

    def setUp(self):
        self.h = prep_fixture.PreparationTests('test_global_gate_retirement_and_unrelated_scan')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.result = self.h.rejected()
        self.store = self.h.store

    def loads(self, function):
        count = [0]
        real = terminal._load

        def counting(*a, **k):
            count[0] += 1
            return real(*a, **k)
        with patch.object(terminal, '_load', side_effect=counting):
            function()
        return count[0]

    def attempts(self):
        r = self.result
        original = terminal._load(self.store, r['intent_hash'])
        return rejection._attempts(self.store, r['scan_id'], original['admission']['requests_used'],
                                   r['admission_after']['requests_used'], intent_hash=r['intent_hash'], allowed_outcome=r['evidence_hash'])

    def test_the_inventory_is_found_inside_the_window_and_matches_the_published_refs(self):
        self.assertEqual([k for k, _ in self.attempts()], self.result['attempt_refs'])

    def test_pages_outside_the_window_are_never_decoded(self):
        base = self.loads(self.attempts)
        for i in range(500):
            self.store.save({'later': i})
        self.assertEqual(self.loads(self.attempts), base)

    def test_a_renumbered_store_fails_closed_instead_of_scanning_everything(self):
        with closing(self.store.connect()) as c:
            row = c.execute('SELECT hash,payload,raw_bytes FROM pages WHERE hash=?', (self.result['intent_hash'],)).fetchone()
            c.execute('DELETE FROM pages WHERE hash=?', (row[0],))
            c.execute('INSERT INTO pages VALUES(?,?,?)', row)          # same content, now the highest rowid
        with self.assertRaisesRegex(ValueError, 'charged attempt missing'):
            self.attempts()

    def test_legacy_callers_without_an_intent_keep_the_whole_store_scan(self):
        r = self.result
        original = terminal._load(self.store, r['intent_hash'])
        found = rejection._attempts(self.store, r['scan_id'], original['admission']['requests_used'], r['admission_after']['requests_used'])
        self.assertEqual([k for k, _ in found], r['attempt_refs'])

    def test_a_conflicting_second_outcome_inside_the_window_is_still_refused(self):
        r = self.result
        original = terminal._load(self.store, r['intent_hash'])
        with closing(self.store.connect()) as c:
            c.execute('DELETE FROM pages WHERE hash=?', (r['evidence_hash'],))
        self.store.save({'kind': 'history_preparation_no_entry_v1', 'intent_hash': r['intent_hash'], 'x': 1})
        with self.assertRaisesRegex(ValueError, 'partial or conflicting outcome'):
            rejection._attempts(self.store, r['scan_id'], original['admission']['requests_used'],
                                r['admission_after']['requests_used'], intent_hash=r['intent_hash'], allowed_outcome=r['evidence_hash'])


if __name__ == '__main__':
    unittest.main()
