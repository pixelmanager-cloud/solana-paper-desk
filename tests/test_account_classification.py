import copy
import json
from pathlib import Path
import unittest

from desk.account_classification import classify_account
from desk.model import digest
from desk.security import base58


class AccountClassificationTests(unittest.TestCase):
    def setUp(self):
        self.record = json.loads((Path(__file__).resolve().parents[1] /
            'fixtures/account-classification/service.json').read_text())

    def check(self, records=None, **kwargs):
        records = [self.record] if records is None else records
        pages = {digest(r): r for r in records}
        query = dict(account=self.record['account'], program=self.record['program'],
                     mint=self.record['scope']['mint'], purpose='funding_source',
                     now=150, slot=15, evidence_hashes=list(pages),
                     trusted_hashes=frozenset(pages), load=pages.__getitem__)
        query.update(kwargs)
        return classify_account(**query)

    def blocked(self, result):
        self.assertEqual(result['label'], 'UNKNOWN')
        self.assertFalse(result['classification_resolved'])
        self.assertFalse(result['exclusion_allowed'])
        self.assertFalse(result['private_control_proven'])
        self.assertFalse(result['ownership_approval'])
        self.assertTrue(result['reasons'])

    def test_exact_service_attestation_is_scoped_and_never_private_control(self):
        before = copy.deepcopy(self.record)
        r = self.check()
        self.assertEqual(r['label'], 'SERVICE')
        self.assertTrue(r['classification_resolved'])
        self.assertFalse(r['exclusion_allowed'])
        self.assertFalse(r['private_control_proven'])
        self.assertFalse(r['ownership_approval'])
        self.assertEqual(r['evidence'][0]['record']['provenance'], before['provenance'])
        self.assertEqual(r['expires_at'], 200)
        self.assertEqual(r['expires_slot'], 20)
        r['evidence'][0]['record']['label'] = 'changed'
        self.assertEqual(self.record, before)

    def test_all_supported_exclusions_need_exact_evidence(self):
        for label in ('POOL', 'POOL_VAULT', 'SERVICE'):
            record = copy.deepcopy(self.record)
            record['label'] = label
            record['scope']['purpose'] = 'holder_exclusion'
            r = self.check([record], purpose='holder_exclusion')
            self.assertTrue(r['exclusion_allowed'])
            self.assertFalse(r['ownership_approval'])

    def test_system_account_and_shared_funding_without_attestation_stay_unknown(self):
        self.blocked(self.check([]))
        for label in ('PRIVATE', 'SYSTEM_ACCOUNT', 'SHARED_FUNDER', 'UNKNOWN'):
            record = {**self.record, 'label': label}
            self.blocked(self.check([record]))

    def test_rebinding_address_owner_mint_or_purpose_is_blocked(self):
        other = base58(bytes([3]) * 32)
        for field, value in [('account', other), ('program', other), ('mint', other),
                             ('purpose', 'distribution_endpoint')]:
            with self.subTest(field=field):
                self.blocked(self.check(**{field: value}))

    def test_both_expirations_and_future_boundaries_are_enforced(self):
        for field, values in [('now', [99, 200, 201]), ('slot', [9, 20, 21])]:
            for value in values:
                self.blocked(self.check(**{field: value}))
        self.assertTrue(self.check(now=100, slot=10)['classification_resolved'])

    def test_missing_provenance_scope_expiry_or_binding_blocks(self):
        for field in self.record:
            record = copy.deepcopy(self.record)
            del record[field]
            self.blocked(self.check([record]))
        for field in self.record['provenance']:
            record = copy.deepcopy(self.record)
            del record['provenance'][field]
            self.blocked(self.check([record]))

    def test_malformed_fields_and_flags_cannot_pass(self):
        for field, value in [('version', True), ('observed_at', True), ('expires_at', 100),
                             ('expires_slot', 10), ('scope', {'mint': '*'}),
                             ('label', []), ('provenance', {'verified': True})]:
            record = {**self.record, field: value, 'verified': True}
            self.blocked(self.check([record]))

    def test_conflicting_stale_or_untrusted_candidate_blocks_valid_label(self):
        conflict = {**self.record, 'label': 'POOL'}
        r = self.check([self.record, conflict])
        self.blocked(r)
        self.assertIn('CLASSIFICATION_CONFLICT', r['reasons'])
        stale = {**self.record, 'expires_at': 120}
        self.blocked(self.check([self.record, stale]))
        self.blocked(self.check(trusted_hashes=frozenset()))
        self.blocked(self.check(evidence_hashes=[digest(self.record), 'b'*64]))

    def test_checksum_missing_reader_and_malformed_hash_fail_closed(self):
        self.blocked(self.check(load=lambda _: {**self.record, 'label': 'POOL'}))
        def missing(_):
            raise ValueError('missing')
        self.blocked(self.check(load=missing))
        self.blocked(self.check(evidence_hashes=[None]))

    def test_duplicates_do_not_inflate_and_earliest_expiry_wins(self):
        key = digest(self.record)
        self.assertEqual(len(self.check(evidence_hashes=[key, key])['evidence']), 1)
        shorter = {**self.record, 'expires_at': 180, 'expires_slot': 18}
        r = self.check([self.record, shorter])
        self.assertEqual((r['expires_at'], r['expires_slot']), (180, 18))

    def test_invalid_queries_and_mutable_policy_raise(self):
        for kwargs in [{'now': True}, {'slot': -1}, {'purpose': '*'},
                       {'account': 'bad'}, {'trusted_hashes': set()}]:
            with self.assertRaises(ValueError):
                self.check(**kwargs)

    def test_offline_content_addressed_store_replay_and_absent_record(self):
        import tempfile
        from desk.evidence import EvidenceStore
        record = json.loads((Path(__file__).resolve().parents[1] /
            'fixtures/account-classification/pool-vault.json').read_text())
        with tempfile.TemporaryDirectory() as folder:
            store = EvidenceStore(Path(folder) / 'evidence.sqlite')
            key = store.save(record)
            reader = EvidenceStore(store.path, read_only=True)
            result = self.check([record], program=record['program'],
                                purpose='holder_exclusion', load=reader.load)
            self.assertEqual(result['label'], 'POOL_VAULT')
            self.assertTrue(result['exclusion_allowed'])
            self.assertEqual(result['evidence'][0]['hash'], key)
            self.blocked(self.check(evidence_hashes=['b'*64],
                trusted_hashes=frozenset({'b'*64}), load=reader.load))
