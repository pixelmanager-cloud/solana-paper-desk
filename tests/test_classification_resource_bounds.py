"""Synthetic resource attacks against the public offline classification API."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from desk import account_classification as classification
from desk.model import canonical, digest


class ClassificationResourceBoundsTests(unittest.TestCase):
    def setUp(self):
        self.record = json.loads((Path(__file__).resolve().parents[1] /
            'fixtures/account-classification/service.json').read_text())
        self.calls = []

    def evaluate(self, records, references=None, policy=None):
        pages = {digest(r): r for r in records}
        def load(key):
            self.calls.append(key)
            return pages[key]
        return classification.classify_account(
            self.record['account'], self.record['program'],
            mint=self.record['scope']['mint'], purpose='funding_source',
            now=150, slot=15,
            evidence_hashes=list(pages) if references is None else references,
            trusted_hashes=frozenset(pages) if policy is None else policy,
            load=load)

    def unresolved(self, result, reason):
        self.assertEqual(result['label'], 'UNKNOWN')
        for flag in ('classification_resolved', 'exclusion_allowed',
                     'private_control_proven', 'ownership_approval'):
            self.assertIs(result[flag], False)
        self.assertIn(reason, result['reasons'])
        self.assertEqual(result['evidence'], [])
        self.assertNotIn('expires_at', result)

    def test_duplicates_and_malformed_oversized_lists_reject_before_load(self):
        key = digest(self.record)
        for refs in ([key] * 129, [None] * 129, [key] * 128 + [None]):
            with self.subTest(last=refs[-1]):
                self.unresolved(self.evaluate([self.record], refs),
                                'CLASSIFICATION_EVIDENCE_REFERENCE_LIMIT')
                self.assertEqual(self.calls, [])
        result = self.evaluate([self.record], [key] * 128)
        self.assertTrue(result['classification_resolved'])
        self.assertEqual(self.calls, [key])
        self.assertEqual(len(result['evidence']), 1)

    def test_unique_reference_boundary_and_oversized_policy(self):
        records = [{**self.record, 'synthetic_case': i} for i in range(129)]
        result = self.evaluate(records[:128])
        self.assertTrue(result['classification_resolved'])
        self.assertEqual(len(self.calls), 128)
        self.calls.clear()
        self.unresolved(self.evaluate(records), 'CLASSIFICATION_POLICY_REFERENCE_LIMIT')
        self.assertEqual(self.calls, [])

    def test_policy_overflow_and_invalid_policy_are_distinct(self):
        policy = frozenset(f'{i:064x}' for i in range(129))
        self.unresolved(self.evaluate([self.record], policy=policy),
                        'CLASSIFICATION_POLICY_REFERENCE_LIMIT')
        for invalid in (set(), frozenset({'not-a-hash'})):
            with self.assertRaisesRegex(ValueError, 'immutable trusted'):
                self.evaluate([self.record], policy=invalid)
        self.assertEqual(self.calls, [])

    def test_nonplain_candidate_collection_is_not_traversed(self):
        class HostileList(list):
            def __iter__(self):
                raise AssertionError('candidate traversal')
        for refs in (HostileList([digest(self.record)]), tuple([digest(self.record)])):
            self.unresolved(self.evaluate([self.record], refs),
                            'CLASSIFICATION_EVIDENCE_LIST_MALFORMED')
        self.assertEqual(self.calls, [])

    def test_malformed_hash_subclass_cannot_override_bounded_validation(self):
        class HostileHash(str):
            def __iter__(self):
                raise AssertionError('hash traversal')
        key = HostileHash(digest(self.record))
        self.unresolved(self.evaluate([self.record], [key]), 'CLASSIFICATION_HASH_MALFORMED')
        with self.assertRaisesRegex(ValueError, 'immutable trusted'):
            self.evaluate([self.record], policy=frozenset({key}))
        self.assertEqual(self.calls, [])

    def test_real_aggregate_overflow_discards_prefix_without_copying_overflow(self):
        records = [{**self.record, 'synthetic_case': i, 'padding': 'x' * 60000}
                   for i in range(70)]
        records[-1]['label'] = 'POOL'  # An unvisited contradiction cannot yield SERVICE.
        source_hashes = [digest(r) for r in records]
        with patch.object(classification, 'deepcopy', wraps=copy.deepcopy) as copies:
            result = self.evaluate(records)
        self.unresolved(result, 'CLASSIFICATION_RETAINED_EVIDENCE_LIMIT')
        self.assertLess(len(self.calls), 71)
        self.assertEqual(copies.call_count, len(self.calls) - 1)
        self.assertLess(copies.call_count, len(records))
        self.assertEqual([digest(r) for r in records], source_hashes)

    def test_exact_aggregate_boundary_and_one_byte_over(self):
        other = {**self.record, 'synthetic_case': 1}
        records = [self.record, other]
        total = sum(len(canonical(r).encode('utf-8')) + 64 for r in records)
        with patch.object(classification, 'MAX_RETAINED_EVIDENCE_BYTES', total):
            result = self.evaluate(records)
        self.assertTrue(result['classification_resolved'])
        self.assertEqual(len(result['evidence']), 2)
        with patch.object(classification, 'MAX_RETAINED_EVIDENCE_BYTES', total - 1):
            self.unresolved(self.evaluate(records), 'CLASSIFICATION_RETAINED_EVIDENCE_LIMIT')

    def test_duplicate_bytes_charge_once_and_conflicting_suffix_is_preserved(self):
        key = digest(self.record)
        size = len(canonical(self.record).encode('utf-8')) + 64
        with patch.object(classification, 'MAX_RETAINED_EVIDENCE_BYTES', size):
            result = self.evaluate([self.record], [key] * 128)
        self.assertTrue(result['classification_resolved'])
        conflict = {**self.record, 'label': 'POOL'}
        result = self.evaluate([self.record, conflict], [key] * 127 + [digest(conflict)])
        self.assertFalse(result['classification_resolved'])
        self.assertEqual(result['label'], 'UNKNOWN')
        self.assertIn('CLASSIFICATION_CONFLICT', result['reasons'])
        self.assertEqual(len(result['evidence']), 2)
