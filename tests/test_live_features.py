"""Synthetic source-bound replay attacks; no provider or eligible-event fixture."""
import json
import sqlite3
import unittest
from unittest.mock import patch

from desk.live_features import candidate_snapshot
from desk.model import canonical, digest
from tests import test_sealed_continuation_entry_evidence as fixtures


class LiveFeatureDiagnosticTests(unittest.TestCase):
    rpc = fixtures.SealedContinuationEntryEvidenceTests.rpc
    run_worker = fixtures.SealedContinuationEntryEvidenceTests.run_worker

    def setUp(self):
        fixtures.SealedContinuationEntryEvidenceTests.setUp(self)

    def snapshot(self, now=110, revision=None):
        with patch('socket.socket', side_effect=AssertionError('Network forbidden')):
            result = candidate_snapshot(self.db, self.evidence, 'scan', now=now,
                                        revision_hash=revision or self.head['evidence_hash'])
        self.assertEqual(result['decision'], 'REJECT')
        self.assertFalse(result['eligible_for_trading'])
        self.assertIn('LIVE_FEATURE_ADAPTER_NOT_READY', result['reasons'])
        self.assertNotIn('event_id', result)
        self.assertNotIn('kind', result)
        return result

    def test_unmocked_raw_replay_is_historical_not_strategy_approval(self):
        result = self.snapshot()
        self.assertEqual(result['components']['history_snapshot']['status'], 'VERIFIED_COMPONENT')
        for name in ('top10_pct', 'reserve_sol', 'mint_revoked', 'route_available', 'pool'):
            self.assertEqual(result['fields'][name]['status'], 'UNKNOWN')
            self.assertIsNone(result['fields'][name]['value'])
            self.assertTrue(result['fields'][name]['unknown_reasons'])
        self.assertLess(len(canonical(result)), 65536)

    def test_raw_gross_concentration_never_populates_private_concentration(self):
        original = fixtures.fixtures.OwnershipIntegrationTests.setUp
        def with_holder(case):
            original(case)
            key = case.store.save({'method': 'getMultipleAccounts',
                'params': [[case.mint, case.account], {'encoding': 'base64',
                    'commitment': 'confirmed', 'minContextSlot': 10}],
                'result': {'context': {'slot': 12}, 'value': case.values}})
            case.report.pop('report_hash')
            case.report['holder_snapshot'] = {'evidence_hash': key}
            case.report['report_hash'] = digest(case.report)
            with sqlite3.connect(case.db) as c:
                c.execute('UPDATE scans SET result=?', (canonical(case.report),))
        with patch.object(fixtures.fixtures.OwnershipIntegrationTests, 'setUp', with_holder):
            fixtures.SealedContinuationEntryEvidenceTests.setUp(self)
        result = self.snapshot()
        gross = result['fields']['gross_top10_supply_pct']
        self.assertEqual(gross['value'], '100')
        self.assertEqual(gross['units'], 'percent_gross_supply')
        self.assertTrue(gross['evidence_hashes'])
        self.assertIsNone(result['fields']['top10_pct']['value'])
        self.assertEqual(result['fields']['top10_pct']['status'], 'UNKNOWN')

    def test_deterministic_manifest_and_hash(self):
        first = self.snapshot()
        self.assertEqual(first, self.snapshot())
        key = first.pop('manifest_hash')
        self.assertEqual(key, digest(first))
        self.assertEqual(first['evidence_hashes'], sorted(set(first['evidence_hashes'])))

    def test_historical_age_is_not_refreshed_by_replay_or_now(self):
        for now in (109, 121, 1000):
            result = self.snapshot(now)
            self.assertEqual(result['observed_at'], 110)
            self.assertIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', result['reasons'])
        self.assertNotIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', self.snapshot(120)['reasons'])

    def test_rebound_revision_suppresses_components(self):
        result = self.snapshot(revision='f' * 64)
        self.assertIn('SOURCE_REVISION_RAW_REPLAY_UNVERIFIED', result['reasons'])
        self.assertTrue(all(v['status'] == 'BLOCKED' for v in result['components'].values()))
        self.assertNotIn('gross_top10_supply_pct', result['fields'])

    def test_forged_summary_cannot_supply_strategy_fields_or_approval(self):
        report = dict(self.report, top10_pct=0, reserve_sol=999, eligible_for_trading=True,
                      mint_revoked=True, observed_at=1000)
        report.pop('report_hash'); report['report_hash'] = digest(report)
        with sqlite3.connect(self.db) as c:
            c.execute('UPDATE scans SET result=?', (canonical(report),))
        result = self.snapshot(now=1000)
        self.assertTrue(all(v['status'] == 'UNKNOWN' for v in result['fields'].values()))
        self.assertIn('SOURCE_REVISION_RAW_REPLAY_UNVERIFIED', result['reasons'])

    def test_seal_budget_and_raw_page_attacks_remain_blocked(self):
        with self.store.connect() as c:
            c.execute("UPDATE ownership_admissions SET state='PREPARED'")
        self.assertIn('SOURCE_REVISION_RAW_REPLAY_UNVERIFIED', self.snapshot()['reasons'])
        with self.store.connect() as c:
            c.execute("UPDATE ownership_admissions SET state='SEALED'")
            c.execute('UPDATE ownership_budgets SET ceiling=19')
        self.assertIn('SOURCE_REVISION_RAW_REPLAY_UNVERIFIED', self.snapshot()['reasons'])
        with self.store.connect() as c:
            c.execute('UPDATE ownership_budgets SET ceiling=18')
            key = self.head['history_queries'][0]['pages'][0]['payload_hash']
            c.execute('DELETE FROM pages WHERE hash=?', (key,))
        self.assertIn('SOURCE_REVISION_RAW_REPLAY_UNVERIFIED', self.snapshot()['reasons'])

    def test_no_storage_writes(self):
        def dump(path):
            with sqlite3.connect(path) as c: return list(c.iterdump())
        before = [dump(self.db), dump(self.evidence)]
        self.snapshot()
        self.assertEqual(before, [dump(self.db), dump(self.evidence)])

    def test_missing_malformed_and_oversized_source_fail_closed(self):
        for source in ('null', '[1]', '{', 'x' * (2 * 1024 * 1024 + 1)):
            with sqlite3.connect(self.db) as c:
                c.execute('UPDATE scans SET result=?', (source,))
            result = self.snapshot()
            self.assertIn('DIAGNOSTIC_SOURCE_OR_REPLAY_UNAVAILABLE', result['reasons'])
            self.assertEqual(result['components'], {})

    def test_explicit_identity_and_time_types(self):
        for revision, now in (('F' * 64, 110), ('a' * 63, 110), ('a' * 64, True),
                              ('a' * 64, -1), ('a' * 64, 2**63)):
            with self.assertRaises(ValueError):
                candidate_snapshot(self.db, self.evidence, 'scan', revision_hash=revision, now=now)

    def test_replay_resource_ceiling_fails_closed(self):
        with patch('desk.live_features.MAX_LOADS', 0):
            result = self.snapshot()
        self.assertTrue(all(v['status'] == 'BLOCKED' for v in result['components'].values()))
        self.assertNotIn('gross_top10_supply_pct', result['fields'])
