"""Synthetic contract replay through OWN-2 labels and the current-holder core.

These raw token bytes and policy pins are test-only; no source authentication or
validated production snapshot is claimed. No positive interval adapter exists.
"""
import base64
from copy import deepcopy
from dataclasses import replace
import unittest

from desk.account_classification import classify_account
from desk.classification_exposure_adapter import ReplayScope, adapt_classification
from desk.distribution_exposure import (
    Balance, Kind, Position, Seed, Transfer, ValidatedSnapshot, trace_exposure,
)
from desk.model import digest
from desk.security import TOKEN_PROGRAM, base58


class ClassificationHolderContractTests(unittest.TestCase):
    def setUp(self):
        self.accounts = tuple(base58(bytes([n])*32) for n in (1, 2))
        self.owner = base58(bytes([3])*32)
        self.mint = base58(bytes([4])*32)
        self.genesis = base58(bytes([5])*32)
        self.scope = ReplayScope(self.genesis, self.mint, 'holder_exclusion',
                                 10, 19, 100, 180, 190)
        self.record = {
            'version': 1, 'account': self.accounts[0], 'program': TOKEN_PROGRAM,
            'token_authority': self.owner, 'label': 'POOL_VAULT',
            'genesis_hash': self.genesis,
            'scope': {'mint': self.mint, 'purpose': 'holder_exclusion'},
            'observed_at': 100, 'expires_at': 200,
            'observed_slot': 10, 'expires_slot': 20,
            'provenance': {'source': 'synthetic coordinator contract fixture',
                           'method': 'exact account attestation',
                           'verifier': 'fixture-only', 'source_hash': 'a'*64},
        }
        # Two token accounts share an authority and transfer 25 raw units.
        # Neither sharing authority nor a shared-funder annotation proves control.
        self.snapshot = ValidatedSnapshot(self.mint, 10, 19, 100,
            (Balance(self.accounts[0], self.owner, 100),
             Balance(self.accounts[1], self.owner, 0)),
            (Balance(self.accounts[0], self.owner, 75),
             Balance(self.accounts[1], self.owner, 25)),
            'synthetic-bank-fixture', 'synthetic-history-fixture',
            validated=True, history_complete=True)
        self.transfers = (Transfer('synthetic-transfer', Position(15, 0, 0),
                                  *self.accounts, 25, 'synthetic-raw-transfer'),)

    def raw(self, account, slot, amount):
        raw = bytearray(165)
        raw[:32] = bytes([4])*32
        raw[32:64] = bytes([3])*32
        raw[64:72] = amount.to_bytes(8, 'little')
        raw[108] = 1
        return {'genesis_hash': self.genesis, 'method': 'getAccountInfo',
            'params': [account, {'encoding': 'base64', 'commitment': 'finalized'}],
            'result': {'context': {'slot': slot}, 'value': {
                'owner': TOKEN_PROGRAM, 'executable': False,
                'data': [base64.b64encode(raw).decode(), 'base64']}}}

    def classify(self, records, **changes):
        pages = {digest(r): r for r in records}
        query = dict(account=self.accounts[0], program=TOKEN_PROGRAM,
            mint=self.mint, purpose='holder_exclusion', now=190, slot=19,
            evidence_hashes=list(pages), trusted_hashes=frozenset(pages),
            load=pages.__getitem__)
        query.update(changes)
        return classify_account(**query)

    def adapt(self, candidate, *, scope=None, candidate_hash=None, current_raw=None):
        return adapt_classification(self.accounts[0], scope=scope or self.scope,
            initial_raw=self.raw(self.accounts[0], 10, 100),
            current_raw=current_raw or self.raw(self.accounts[0], 19, 75),
            candidate=candidate,
            candidate_hash=digest(candidate) if candidate_hash is None else candidate_hash)

    def unresolved_exposure(self, *adapted):
        result = trace_exposure(self.snapshot, self.transfers,
            (Seed(self.accounts[0], 100),), tuple(a.to_core() for a in adapted))
        self.assertEqual((result.lower_bound, result.excluded, result.unresolved), (0, 0, 100))
        self.assertEqual(tuple(h.balance for h in result.holders), (75, 25))
        self.assertTrue(all(h.kind is Kind.UNRESOLVED for h in result.holders))
        self.assertIn('CLASSIFICATION_UNRESOLVED', result.reasons)
        return result

    def test_point_attestation_does_not_authorize_interval_or_other_holder(self):
        classified = self.classify([self.record])
        self.assertTrue(classified['exclusion_allowed'])
        self.assertFalse(classified['private_control_proven'])
        adapted = self.adapt(classified['evidence'][0]['record'])
        self.assertEqual(adapted.owner, self.owner)
        self.assertEqual(adapted.chain_program, TOKEN_PROGRAM)
        self.assertIn('INDEPENDENT_SOURCE_ADMISSION_UNAVAILABLE', adapted.reasons)
        self.unresolved_exposure(adapted)

    def test_pool_service_conflicts_survive_both_consumer_orders(self):
        service = {**self.record, 'label': 'SERVICE'}
        for records in ([self.record, service], [service, self.record]):
            with self.subTest(first=records[0]['label']):
                before = deepcopy(records)
                classified = self.classify(records)
                self.assertEqual(classified['label'], 'UNKNOWN')
                self.assertIn('CLASSIFICATION_CONFLICT', classified['reasons'])
                self.assertFalse(classified['exclusion_allowed'])
                result = self.unresolved_exposure(*(self.adapt(r) for r in records))
                self.assertIn('CLASSIFICATION_CONFLICT', result.reasons)
                self.assertEqual(records, before)

    def test_provenance_address_program_and_scope_gaps_cannot_exclude_balances(self):
        cases = [({'provenance': {}}, 'CLASSIFICATION_PROVENANCE_MISSING'),
                 ({'account': self.accounts[1]}, 'ACCOUNT_PROGRAM_BINDING_MISMATCH'),
                 ({'program': self.owner}, 'ACCOUNT_PROGRAM_BINDING_MISMATCH'),
                 ({'scope': {'mint': self.mint, 'purpose': 'funding_source'}},
                  'CLASSIFICATION_SCOPE_MISMATCH')]
        for change, reason in cases:
            with self.subTest(change=change):
                record = {**self.record, **change}
                classified = self.classify([record])
                self.assertEqual(classified['label'], 'UNKNOWN')
                self.assertIn(reason, classified['reasons'])
                adapted = self.adapt(record)
                self.assertIn(reason, adapted.reasons)
                self.unresolved_exposure(adapted)

    def test_expiry_boundaries_remain_unresolved_through_current_holder_replay(self):
        shorter = {**self.record, 'expires_at': 190, 'expires_slot': 19}
        classified = self.classify([self.record, shorter])
        self.assertFalse(classified['exclusion_allowed'])
        self.assertIn('CLASSIFICATION_STALE_OR_FUTURE', classified['reasons'])
        for scope in (self.scope, replace(self.scope, evaluated_at=200)):
            adapted = self.adapt(shorter if scope == self.scope else self.record, scope=scope)
            self.assertIn('CLASSIFICATION_STALE_OR_FUTURE', adapted.reasons)
            self.unresolved_exposure(adapted)
        self.assertEqual(self.adapt(shorter).valid_through_slot, 18)

    def test_shared_funding_system_and_private_assertions_never_propagate(self):
        for label in ('SHARED_FUNDER', 'SYSTEM_ACCOUNT', 'PRIVATE', 'UNKNOWN'):
            record = {**self.record, 'label': label, 'verified': True,
                      'shared_funder': self.owner, 'common_control': True}
            classified = self.classify([record])
            self.assertFalse(classified['classification_resolved'])
            self.assertFalse(classified['private_control_proven'])
            self.unresolved_exposure(self.adapt(record))

    def test_nested_candidate_fails_closed_in_consumer(self):
        record = deepcopy(self.record)
        nested = {}; record['annotation'] = nested
        for _ in range(2000):
            nested['next'] = {}; nested = nested['next']
        adapted = self.adapt(record, candidate_hash='a'*64)
        self.assertIn('CLASSIFICATION_CANDIDATE_MALFORMED', adapted.reasons)
        self.unresolved_exposure(adapted)
