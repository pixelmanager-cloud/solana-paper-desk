"""T22F: producer vocabulary (R1 extras), visible publish refusals (R3), rotation capacity (R4), witness constraint (R5).

Fixtures only (synthetic bytes of tests.test_empty_history_no_entry); no network, no credentials.
"""
import copy
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from desk import paper_cycle_no_entry as no_entry, paper_terminal_reconciliation as terminal
from tests.test_cycle_no_entry import fixture
from tests.test_review_t01_integrity import with_producer_blockers


class ProducerVocabularyTests(unittest.TestCase):
    def test_window_blockers_only_for_known_measurements(self):
        ok = no_entry.producer_blocker_is_normal
        self.assertTrue(ok('MISSING_WINDOW_MEASUREMENT:net_buy_ratio'))
        self.assertTrue(ok('STALE_WINDOW_MEASUREMENT:directional_flow_proxy_v1'))
        self.assertFalse(ok('MISSING_WINDOW_MEASUREMENT:made_up_measurement'))
        self.assertFalse(ok('STALE_WINDOW_MEASUREMENT:'))
        self.assertFalse(ok(None))

    def test_declared_hazards_are_normal_but_only_when_declared(self):
        ok = no_entry.producer_blocker_is_normal
        self.assertTrue(ok('FREEZE_AUTHORITY_PRESENT', ('FREEZE_AUTHORITY_PRESENT',)))
        self.assertFalse(ok('FREEZE_AUTHORITY_PRESENT'))
        self.assertFalse(ok('FREEZE_AUTHORITY_PRESENT', ('SOMETHING_ELSE',)))

    def test_an_integrity_code_declared_as_a_hazard_stays_integrity(self):
        ok = no_entry.producer_blocker_is_normal
        for code in no_entry.PRODUCER_INTEGRITY | {'SOL_USD:ANYTHING', 'ORIGINAL_MINT_MISSING_OR_INVALID',
                                                    'COLLECTOR_ANYTHING', 'MISSING_WINDOW_MEASUREMENT:bogus'}:
            with self.subTest(code):
                self.assertFalse(ok(code, (code,)))


class CheckResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = fixture(legacy=True)
        cls.addClassCleanup(cls.t.doCleanups)
        cls.scan = cls.t.h.f.target.scan_id
        cls.after = cls.t.result['budget'][cls.scan]['used']
        cls.before = cls.after - cls.t.result['attempted_requests']

    def check(self, result, blocker='MARKET_PRODUCER_BLOCKED', hazards=()):
        no_entry._check_result(result, blocker, self.scan, self.before, self.after, hazards)

    def test_declared_hazard_accepted_only_with_the_declaration(self):
        forged = with_producer_blockers(self.t.result, ('FREEZE_AUTHORITY_PRESENT',))
        self.check(forged, hazards=('FREEZE_AUTHORITY_PRESENT',))
        with self.assertRaises(ValueError):
            self.check(forged)

    def test_one_bad_code_among_normal_ones_rejects_the_whole_result(self):
        forged = with_producer_blockers(self.t.result, ('MISSING_WINDOW_MEASUREMENT:net_buy_ratio',
                                                        'COORDINATOR_TARGET_BINDING_MISMATCH'))
        with self.assertRaises(ValueError):
            self.check(forged)

    def test_blockers_must_be_a_list_of_strings(self):
        forged = with_producer_blockers(self.t.result, ())
        for diagnostic in forged['diagnostics']:
            if 'blockers' in diagnostic:
                diagnostic['blockers'] = [['MISSING_WINDOW_MEASUREMENT:net_buy_ratio']]
        with self.assertRaises(ValueError):
            self.check(forged)

    def test_migration_witness_blocker_is_only_for_an_absent_witness(self):
        """R5: contradictory graduation evidence (conflicting timestamps, binding mismatch...) is category (b)."""
        base = copy.deepcopy({k: v for k, v in self.t.result.items() if k != 'evidence_hash'})
        graduation = [d for d in base['diagnostics'] if 'graduation' in d][0]
        base['blockers'] = ['RETAINED_MIGRATION_WITNESS_REQUIRED']
        for blockers, accepted in ((['MIGRATION_WITNESS_ABSENT'], True),
                                   (['CONFLICTING_MIGRATION_TIMESTAMPS'], False),
                                   (['MIGRATION_ACCOUNT_BINDING_MISMATCH', 'MIGRATION_WITNESS_ABSENT'], False),
                                   (['MALFORMED_RAW_TRANSACTION_OR_TIME'], False), ([], False)):
            with self.subTest(blockers):
                graduation['graduation'] = {'status': 'NOT_OBSERVED', 'blockers': blockers}
                if accepted:
                    self.check(base, 'RETAINED_MIGRATION_WITNESS_REQUIRED')
                else:
                    with self.assertRaises(ValueError):
                        self.check(base, 'RETAINED_MIGRATION_WITNESS_REQUIRED')


class AttemptIndexTests(unittest.TestCase):
    """R2: the per-scan index is built from explicit refs; wrong refs fail closed."""

    @classmethod
    def setUpClass(cls):
        cls.t = fixture(legacy=True)
        cls.addClassCleanup(cls.t.doCleanups)
        cls.scan = cls.t.h.f.target.scan_id
        cls.refs = list(cls.t.result['attempt_refs'])

    def test_real_refs_index_by_charge_number(self):
        found = no_entry._index(self.t.store, self.scan, self.refs)[self.scan]
        self.assertEqual(sorted(found), list(range(min(found), min(found) + len(found))))
        self.assertEqual({key for key, _ in found.values()}, set(self.refs))

    def test_foreign_scan_unrelated_page_duplicates_and_bad_refs_are_refused(self):
        store = self.t.store
        filler = store.save({'kind': 'unrelated_filler', 'i': 1})
        cases = {'other scan': (self.scan + 'x', self.refs), 'not an attempt': (self.scan, self.refs + [filler]),
                 'duplicate ref': (self.scan, self.refs + self.refs[:1]), 'not a hash': (self.scan, ['zz']),
                 'too many': (self.scan, [('%064x' % i) for i in range(19)]), 'not a list': (self.scan, tuple(self.refs))}
        for name, (scan, refs) in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                no_entry._index(store, scan, refs)

    def test_two_pages_claiming_the_same_charge_are_ambiguous(self):
        store = self.t.store
        record = copy.deepcopy(store.load(self.refs[0]))
        record['observed_at'] = record['observed_at'] + 1          # same scan and requests_used, different page
        twin = store.save(record)
        with self.assertRaises(ValueError):
            no_entry._index(store, self.scan, self.refs + [twin])


class RefusalLogTests(unittest.TestCase):
    def test_a_swallowed_publish_refusal_is_recorded_and_the_pass_is_not_retired_as_no_entry(self):
        """R3 end to end through the real run_once: publish raises, the cycle still fails closed, the refusal is typed."""
        def refusing(*args, **kwargs):
            raise ValueError('forced refusal SYNTHETIC_TEST_ONLY')
        with patch.object(no_entry, 'publish', refusing):
            t = fixture()
        self.addCleanup(t.doCleanups)
        rows = no_entry.read_refusals(t.store.path)
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual((row['kind'], row['reason'], row['blocker']), ('publish_refused_v1', 'ValueError', 'MARKET_PRODUCER_BLOCKED'))
        self.assertEqual((row['scan_id'], row['pass_id'] is not None), (t.h.f.target.scan_id, True))
        self.assertIn('forced refusal', row['detail'])
        with closing(t.store.connect()) as c:                       # nothing was retired as a normal no-entry
            self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (no_entry.TABLE,)).fetchone())

    def test_record_and_read_round_trip_cap_and_malformed_lines(self):
        class Store:
            path = None
        with tempfile.TemporaryDirectory() as directory:
            store = Store(); store.path = os.path.join(directory, 'evidence.db')
            self.assertEqual(no_entry.read_refusals(store.path), [])
            no_entry.record_refusal(store, pass_id='p', scan_id='s', blocker='B', error=OSError('x' * 500), at=1.0)
            rows = no_entry.read_refusals(store.path)
            self.assertEqual((len(rows), len(rows[0]['detail'])), (1, 200))       # message is bounded
            with open(no_entry.refusal_path(store), 'ab') as handle:
                handle.write(b'not json\n')
            self.assertEqual(no_entry.read_refusals(store.path)[1]['reason'], 'MALFORMED_LINE')
            with patch.object(no_entry, 'MAX_REFUSAL_BYTES', os.path.getsize(no_entry.refusal_path(store))):
                no_entry.record_refusal(store, pass_id='p2', scan_id='s', blocker='B', error=ValueError('y'), at=2.0)
                self.assertEqual(len(no_entry.read_refusals(store.path)), 2)       # full log stops growing
            no_entry.record_refusal(store, pass_id='p3', scan_id='s', blocker='B', error=ValueError('z'), at=3.0)
            self.assertEqual(len(no_entry.read_refusals(store.path)), 3)

    def test_unwritable_log_never_changes_the_cycle_outcome(self):
        class Store:
            path = '/nonexistent-directory/evidence.db'
        row = no_entry.record_refusal(Store(), pass_id='p', scan_id='s', blocker='B', error=ValueError('q'), at=1.0)
        self.assertEqual(row['reason'], 'ValueError')


class RotationCapacityTests(unittest.TestCase):
    def test_warning_at_eighty_percent_and_hard_stop_that_keeps_the_store_replayable(self):
        """R4: at 80% of the limit the publication warns (visibly) and the retained row still replays through the gate."""
        with patch.object(no_entry, 'MAX_ROWS', 1), patch.object(no_entry, 'GUARDS', no_entry._guards(1)):
            t = fixture()
            self.addCleanup(t.doCleanups)
            # limit 1 -> 80% is reached by the first publication
            warned = [r for r in no_entry.read_refusals(t.store.path) if r['kind'] == no_entry.WARNING_KIND]
            self.assertEqual([(w['rows'], w['limit']) for w in warned], [(1, 1)])
            self.assertIsNone(terminal.gate(t.store, t.ctx['research_db'], ()))
            self.assertEqual(terminal.gate(t.store, t.ctx['research_db'], (t.h.f.target.scan_id,)), 'REJECTED_SCAN_RETIRED')

    def test_hard_stop_refuses_with_a_typed_error_and_writes_nothing(self):
        t = fixture(legacy=True)                    # a NULL pass that would otherwise publish
        self.addCleanup(t.doCleanups)
        with closing(t.store.connect()) as c:
            pending = c.execute('SELECT id,intent_hash FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()
        result = {k: v for k, v in t.result.items() if k != 'evidence_hash'}
        with patch.object(no_entry, 'MAX_ROWS', 0), patch.object(no_entry, 'GUARDS', no_entry._guards(0)):
            with self.assertRaises(no_entry.RotationRequired):
                no_entry.publish(t.store, t.progress, pass_id=pending[0], intent_hash=pending[1], result=result,
                                 ledger=no_entry.ledger_snapshot(t.ctx['ledger_db']))
        with closing(t.store.connect()) as c:
            self.assertFalse(c.execute('SELECT 1 FROM sqlite_master WHERE name=?', (no_entry.TABLE,)).fetchone())
            self.assertIsNone(c.execute('SELECT outcome_hash FROM paper_observation_passes WHERE id=?', (pending[0],)).fetchone()[0])
        self.assertTrue(issubclass(no_entry.RotationRequired, ValueError))    # run_once swallows ValueError -> recorded

    def test_default_limit_and_guard_text_are_unchanged(self):
        self.assertEqual(no_entry.MAX_ROWS, 8192)
        self.assertEqual(no_entry.GUARDS, no_entry._guards(8192))
        self.assertIn('>=8192', no_entry.GUARDS[no_entry.TABLE + '_insert'])


if __name__ == '__main__':
    unittest.main()
