"""Deep and unbounded candidate evidence must block, never abort classification."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from desk.account_classification import classify_account, MAX_RECORD_DEPTH, MAX_RECORD_NODES, MAX_RECORD_BYTES
from desk.model import digest


class ClassificationDepthBoundTests(unittest.TestCase):
    def setUp(self):
        self.record = json.loads((Path(__file__).resolve().parents[1]/
                                  'fixtures/account-classification/service.json').read_text())
        self.record['scope']['purpose'] = 'holder_exclusion'

    def classify(self, records, hashes=None):
        hashes = hashes or [digest(record) for record in records]
        pages = dict(zip(hashes, records))
        return classify_account(self.record['account'], self.record['program'],
            mint=self.record['scope']['mint'], purpose='holder_exclusion', now=150, slot=15,
            evidence_hashes=hashes, trusted_hashes=frozenset(hashes), load=pages.__getitem__)

    def blocked(self, result):
        self.assertEqual(result['label'], 'UNKNOWN')
        for flag in ('classification_resolved', 'exclusion_allowed', 'private_control_proven', 'ownership_approval'):
            self.assertIs(result[flag], False)
        self.assertIn('CLASSIFICATION_EVIDENCE_UNREADABLE', result['reasons'])

    def nested(self, depth):
        record = copy.deepcopy(self.record)
        node = {};record['annotation'] = node
        for _ in range(depth):
            node['next'] = {};node = node['next']
        return record

    def test_pinned_600_level_json_reproduction_blocks_before_encoding_or_copy(self):
        record = self.nested(600)
        key = digest(record)  # Valid, small JSON: original failure was deepcopy.
        with (patch('desk.account_classification.deepcopy', side_effect=AssertionError('No unbounded copy')),
              patch('desk.account_classification.digest', side_effect=AssertionError('No unbounded digest'))):
            self.blocked(self.classify([record], [key]))
        self.assertEqual(record['account'], self.record['account'])

    def test_depth_boundary_preserves_existing_positive_and_detached_evidence(self):
        record = self.nested(MAX_RECORD_DEPTH-1)
        result = self.classify([record])
        self.assertTrue(result['classification_resolved'])
        self.assertTrue(result['exclusion_allowed'])
        self.assertFalse(result['private_control_proven'])
        self.assertFalse(result['ownership_approval'])
        result['evidence'][0]['record']['annotation']['next'] = 'changed'
        self.assertIsInstance(record['annotation']['next'], dict)
        self.blocked(self.classify([self.nested(MAX_RECORD_DEPTH)]))

    def test_failed_candidate_blocks_agreeing_valid_candidate_in_both_orders(self):
        deep = self.nested(600)
        for records in ([self.record,deep], [deep,self.record]):
            result = self.classify(records)
            self.blocked(result)
            self.assertEqual(len(result['evidence']), 1)

    def test_cycles_non_json_and_excessive_width_or_scalar_are_unresolved(self):
        cyclic = copy.deepcopy(self.record);cyclic['annotation'] = cyclic
        candidates = [cyclic]
        for annotation in ([0]*MAX_RECORD_NODES, 'x'*(MAX_RECORD_BYTES+1),
                           1<<257, object(), {1:'nonstring key'}, float('nan')):
            candidate = copy.deepcopy(self.record);candidate['annotation'] = annotation;candidates.append(candidate)
        for record in candidates:
            with self.subTest(type=type(record.get('annotation')).__name__):
                self.blocked(self.classify([record], ['a'*64]))

    def test_copy_failure_inside_boundary_cannot_abort_or_omit_blocking_reason(self):
        key = digest(self.record)
        with patch('desk.account_classification.deepcopy', side_effect=RecursionError):
            result = self.classify([self.record], [key])
        self.blocked(result)
        self.assertEqual(result['evidence'], [])

    def test_nonobject_root_candidates_cannot_reach_postcopy_label_iteration(self):
        for record in (None, [], 'text', 123, True):
            with self.subTest(record=record):
                self.blocked(self.classify([record]))
