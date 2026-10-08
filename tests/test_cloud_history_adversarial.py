"""Offline attacks on request-bound replay, snapshot matching and reservations."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from desk.evidence import EvidenceStore
from desk.continuity import reconcile_history
from desk.history import collect_history
from desk.history_progress import HistoryProgress
from desk.model import digest
from desk.ownership_snapshot import reconcile_snapshot
from desk.replay_history import replay_history, reconstruct_launch_history


class CloudHistoryAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.f = json.loads((Path(__file__).resolve().parents[1] /
                             'fixtures/cloud_history/synthetic.json').read_text())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'e.sqlite'
        self.store = EvidenceStore(self.path)

    def snapshot(self):
        return reconcile_snapshot(self.f['mint'], self.f['history'],
                                  self.f['snapshot'], self.f['clock'])

    def capture(self, pages, cutoff=21):
        iterator = iter(pages)
        return collect_history(self.f['mint'], 1, 2, lambda *a: next(iterator),
                               max_pages=len(pages), capture=self.store.save,
                               token_accounts='none', slot_range={'gte': 0, 'lt': cutoff})

    @staticmethod
    def rehash(coverage):
        coverage.pop('evidence_hash')
        coverage['evidence_hash'] = digest(coverage)

    def test_same_slot_cutoff_includes_all_records_at_snapshot_slot(self):
        rows = copy.deepcopy(self.f['records'][:2])
        rows[0]['slot'] = 20
        observations, coverage = self.capture([{'data': rows}])
        self.assertEqual(len(observations), 2)
        self.assertTrue(coverage['query_coverage_verified'])
        self.assertEqual(replay_history(coverage, self.store), (observations, coverage))
        self.assertTrue(self.snapshot()['reconciled'])
        self.assertFalse(self.snapshot()['eligible_for_trading'])

    def test_exclusive_cutoff_rejects_record_exactly_at_lt(self):
        observations, coverage = self.capture([{'data': self.f['records']}])
        self.assertEqual(len(observations), 2)
        self.assertIn('HISTORY_SLOT_OUTSIDE_QUERY', coverage['reasons'])
        self.assertFalse(replay_history(coverage, self.store)[1]['query_coverage_verified'])

    def test_same_slot_lifetime_cannot_use_page_or_signature_order(self):
        rows = self.f['continuity_records']
        for ordered in [rows, list(reversed(rows))]:
            with self.subTest(signatures=[r['signature'] for r in ordered]):
                result = reconcile_history(self.f['mint'], ordered)
                self.assertIn('HISTORY_SAME_SLOT_ORDER_UNKNOWN', result['reasons'])
                self.assertFalse(result['passed'])
                self.assertFalse(result['eligible_for_trading'])

    def test_individually_balanced_records_cannot_bridge_missing_activity(self):
        rows = self.f['continuity_records']
        rows[1]['slot'] = 21
        rows[1]['token_deltas'][0]['pre_raw'] = '99'
        rows[1]['token_supply_changes'][0]['amount_raw'] = '9'
        result = reconcile_history(self.f['mint'], rows)
        self.assertIn('HISTORY_BALANCE_DISCONTINUITY', result['reasons'])
        self.assertFalse(result['passed'])

    def test_cutoff_cannot_be_rebound_with_recomputed_summary_hash(self):
        _, coverage = self.capture([{'data': self.f['records'][:2]}])
        coverage['slot_range']['lt'] = 22
        self.rehash(coverage)
        with self.assertRaisesRegex(ValueError, 'binding'):
            replay_history(coverage, self.store)

    def test_reordered_page_chain_rejected_after_rehash(self):
        _, coverage = self.capture([{'data': [self.f['records'][0]], 'paginationToken': 'p2'},
                                    {'data': [self.f['records'][1]]}])
        coverage['pages'].reverse()
        self.rehash(coverage)
        with self.assertRaises(ValueError):
            replay_history(coverage, self.store)

    def test_provider_reordered_records_preserve_order_gap(self):
        _, coverage = self.capture([{'data': self.f['records'][1::-1]}])
        self.assertIn('HISTORY_ORDER_REGRESSION', coverage['reasons'])
        self.assertFalse(replay_history(coverage, self.store)[1]['query_coverage_verified'])

    def test_cross_page_duplicate_and_conflict_are_distinct_gaps(self):
        for changed, reason in [(False, 'HISTORY_DUPLICATE_RECORD'),
                                (True, 'HISTORY_CONFLICTING_RECORD')]:
            with self.subTest(changed=changed):
                second = copy.deepcopy(self.f['records'][0])
                if changed:
                    second['blockTime'] += 1
                observations, coverage = self.capture([
                    {'data': [self.f['records'][0]], 'paginationToken': 'p2'}, {'data': [second]}])
                self.assertEqual(len(observations), 1)
                self.assertIn(reason, coverage['reasons'])
                self.assertFalse(replay_history(coverage, self.store)[1]['query_coverage_verified'])

    def test_missing_raw_page_cannot_be_replaced_by_coverage_flags(self):
        _, coverage = self.capture([{'data': self.f['records'][:2]}])
        with self.store.connect() as db:
            db.execute('DELETE FROM pages WHERE hash=?', (coverage['pages'][0]['payload_hash'],))
        with self.assertRaises(ValueError):
            replay_history(coverage, self.store)

    def test_missing_open_account_fails_even_with_complete_summary(self):
        self.f['snapshot']['result']['value'][1] = None
        self.assertIn('OPEN_ACCOUNT_MISSING', self.snapshot()['reasons'])
        self.assertFalse(self.snapshot()['reconciled'])

    def test_contradictory_ending_balance_cannot_hide_under_matching_supply(self):
        self.f['history']['account_continuity']['end_states'][0]['amount_raw'] = '99'
        result = self.snapshot()
        self.assertEqual(result['observed_supply_raw'], result['supply_raw'])
        self.assertIn('HISTORY_SNAPSHOT_BALANCE_OR_CONTROL_MISMATCH', result['reasons'])
        self.assertFalse(result['reconciled'])

    def test_missing_and_duplicate_endings_fail_closed(self):
        endings = self.f['history']['account_continuity']['end_states']
        for replacement in [[], endings + copy.deepcopy(endings)]:
            with self.subTest(replacement=replacement):
                self.f['history']['account_continuity']['end_states'] = replacement
                self.assertIn('ACCOUNT_ENDING_COVERAGE_MISMATCH', self.snapshot()['reasons'])

    def test_timestamp_match_cannot_substitute_for_exact_slot_cutoff(self):
        for cutoff in [None, {'gte': 0, 'lt': 20}, {'gte': 0, 'lt': 22}]:
            with self.subTest(cutoff=cutoff):
                self.f['history']['slot_range'] = cutoff
                self.assertIn('HISTORY_SNAPSHOT_SLOT_BOUNDARY_UNVERIFIED', self.snapshot()['reasons'])

    def test_closed_account_reappearance_is_not_current_ownership(self):
        ending = self.f['history']['account_continuity']['end_states'][0]
        ending.update(closed=True, amount_raw=None)
        self.assertIn('CLOSED_ACCOUNT_REAPPEARED', self.snapshot()['reasons'])

    def test_restart_after_process_interruption_keeps_reservation(self):
        progress = HistoryProgress(self.store)
        progress.budget('scan', digest({'scan': 3}), 16)
        key = progress.create('scan', self.f['mint'], 1, 2, {'gte': 0, 'lt': 21})
        def interrupted(*args):
            raise SystemExit('synthetic process death after reservation')
        with self.assertRaises(SystemExit):
            progress.advance(key, interrupted)
        reopened = HistoryProgress(EvidenceStore(self.path))
        reopened.budget('scan', digest({'scan': 3}), 0)
        self.assertEqual(reopened.snapshot(key)['requests_used'], 17)
        result = reopened.advance(key, lambda *a: {'data': []})
        self.assertEqual(result['requests_used'], 18)
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(result['query']['slot_range'], {'gte': 0, 'lt': 21})

    def test_exhausted_shared_budget_prevents_new_account_request_after_restart(self):
        progress = HistoryProgress(self.store)
        progress.budget('scan', digest({'scan': 4}), 17)
        first = progress.create('scan', self.f['mint'], 1, 2)
        second = progress.create('scan', self.f['account'], 1, 2)
        def failure(*args):
            raise OSError('synthetic provider failure')
        progress.advance(first, failure)
        reopened = HistoryProgress(EvidenceStore(self.path))
        result = reopened.advance(second, lambda *a: self.fail('Exhausted budget made a request'))
        self.assertEqual(result['requests_used'], 18)
        self.assertEqual(result['attempts'], 0)
        self.assertEqual(result['blocked'], 'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED')
        self.assertIsNone(result['coverage'])

    def test_replay_page_budget_cannot_be_bypassed_by_many_queries(self):
        _, coverage = self.capture([{'data': []}])
        with self.assertRaisesRegex(ValueError, 'budget'):
            reconstruct_launch_history({'mint': self.f['mint'], 'history_queries': [coverage] * 19}, self.store)
