import base64
import copy
from dataclasses import replace
import importlib.util
from pathlib import Path
import unittest

from desk.classification_exposure_adapter import ReplayScope, adapt_classification
from desk.model import digest
from desk.security import TOKEN_PROGRAM, base58


class ClassificationExposureAdapterTests(unittest.TestCase):
    def setUp(self):
        self.account = base58(bytes([1]) * 32)
        self.authority = base58(bytes([2]) * 32)
        self.mint = base58(bytes([3]) * 32)
        self.chain = base58(bytes([4]) * 32)
        self.scope = ReplayScope(self.chain, self.mint, 'holder_exclusion', 10, 19, 100, 180, 190)
        self.initial = self.raw(10)
        self.current = self.raw(19)
        self.candidate = {'version': 1, 'label': 'POOL_VAULT', 'account': self.account,
            'program': TOKEN_PROGRAM, 'token_authority': self.authority,
            'genesis_hash': self.chain, 'scope': {'mint': self.mint, 'purpose': 'holder_exclusion'},
            'observed_slot': 10, 'expires_slot': 20, 'observed_at': 100, 'expires_at': 200,
            'provenance': {'source': 'invented synthetic source', 'method': 'invented verifier',
                           'verifier': 'not authenticated', 'source_hash': 'a' * 64},
            'verified': True, 'validated': True, 'history_complete': True}

    def raw(self, slot, authority=None):
        data = bytearray(165)
        data[:32] = bytes([3]) * 32
        data[32:64] = bytes([2 if authority is None else authority]) * 32
        data[64:72] = (100).to_bytes(8, 'little')
        data[108] = 1
        return {'genesis_hash': self.chain, 'method': 'getAccountInfo',
            'params': [self.account, {'encoding': 'base64', 'commitment': 'finalized'}],
            'result': {'context': {'slot': slot}, 'value': {'owner': TOKEN_PROGRAM,
                'executable': False, 'data': [base64.b64encode(data).decode(), 'base64']}}}

    def adapt(self, **changes):
        args = dict(scope=self.scope, initial_raw=self.initial, current_raw=self.current,
                    candidate=self.candidate, candidate_hash=digest(self.candidate))
        args.update(changes)
        return adapt_classification(self.account, **args)

    def unresolved(self, r):
        self.assertEqual(r.kind, 'unresolved')
        self.assertFalse(r.ownership_approval)
        self.assertFalse(r.verified)
        self.assertFalse(r.private_control_proven)
        self.assertIn('INDEPENDENT_SOURCE_ADMISSION_UNAVAILABLE', r.reasons)

    def test_self_hashed_forged_candidate_never_admitted(self):
        before = copy.deepcopy(self.candidate)
        r = self.adapt()
        self.unresolved(r)
        self.assertEqual(self.candidate, before)
        self.assertEqual(r.candidate_label, 'POOL_VAULT')
        # Inspection can retain the assertion without endorsing it.
        self.assertEqual(r.chain_program, TOKEN_PROGRAM)
        self.assertEqual(r.owner, self.authority)
        self.assertNotEqual(r.chain_program, r.owner)

    def test_claimed_program_cannot_be_token_authority(self):
        c = {**self.candidate, 'program': self.authority}
        r = self.adapt(candidate=c, candidate_hash=digest(c))
        self.unresolved(r)
        self.assertIn('ACCOUNT_PROGRAM_BINDING_MISMATCH', r.reasons)
        c = {**self.candidate, 'token_authority': TOKEN_PROGRAM}
        self.assertIn('CLASSIFICATION_TOKEN_AUTHORITY_MISMATCH',
            self.adapt(candidate=c, candidate_hash=digest(c)).reasons)

    def test_exclusive_expiry_maps_to_inclusive_through(self):
        r = self.adapt()
        self.assertEqual((r.valid_from_slot, r.valid_through_slot), (10, 19))
        self.assertNotIn('CLASSIFICATION_STALE_OR_FUTURE', r.reasons)
        r = self.adapt(scope=replace(self.scope, cutoff_slot=20), current_raw=self.raw(20))
        self.assertEqual(r.valid_through_slot, 19)
        self.assertIn('CLASSIFICATION_STALE_OR_FUTURE', r.reasons)
        self.unresolved(r)

    def test_actual_time_expiry_not_just_chain_time(self):
        for now in (200, 201):
            r = self.adapt(scope=replace(self.scope, evaluated_at=now))
            self.assertIn('CLASSIFICATION_STALE_OR_FUTURE', r.reasons)
            self.unresolved(r)
        self.assertNotIn('CLASSIFICATION_STALE_OR_FUTURE',
                         self.adapt(scope=replace(self.scope, evaluated_at=199)).reasons)

    def test_whole_interval_must_be_covered(self):
        for field, value in [('observed_at', 101), ('observed_slot', 11),
                             ('expires_at', 180), ('expires_slot', 19)]:
            c = {**self.candidate, field: value}
            r = self.adapt(candidate=c, candidate_hash=digest(c))
            self.assertIn('CLASSIFICATION_STALE_OR_FUTURE', r.reasons)
            self.unresolved(r)

    def test_wrong_purpose_chain_or_authority_cannot_exclude(self):
        c = copy.deepcopy(self.candidate)
        c['scope']['purpose'] = 'funding_source'
        self.assertIn('CLASSIFICATION_SCOPE_MISMATCH',
            self.adapt(candidate=c, candidate_hash=digest(c)).reasons)
        c = {**self.candidate, 'genesis_hash': self.mint}
        self.assertIn('CLASSIFICATION_CHAIN_SCOPE_MISMATCH',
            self.adapt(candidate=c, candidate_hash=digest(c)).reasons)
        self.assertIn('RAW_BOUNDARY_IDENTITY_CONFLICT',
            self.adapt(initial_raw=self.raw(10, authority=5)).reasons)

    def test_unverified_normalized_input_flags_are_ignored(self):
        r = self.adapt(current_raw={'validated': True, 'owner': self.authority,
                                  'program': TOKEN_PROGRAM, 'amount': 100})
        self.unresolved(r)
        self.assertIsNone(r.owner)
        self.assertIsNone(r.chain_program)
        self.assertIn('RAW_ACCOUNT_BINDING_UNVERIFIED', r.reasons)
        self.unresolved(self.adapt(candidate=None, candidate_hash=None))
        c = {**self.candidate, 'label': 'PRIVATE'}
        r = self.adapt(candidate=c, candidate_hash=digest(c))
        self.assertIn('UNKNOWN_OR_UNSUPPORTED_LABEL', r.reasons)
        self.unresolved(r)

    def test_tampered_hash_and_raw_bindings_fail_closed(self):
        self.assertIn('CLASSIFICATION_CANDIDATE_HASH_MISMATCH',
            self.adapt(candidate_hash='b'*64).reasons)
        for change in ('params', 'genesis_hash', 'slot', 'owner', 'executable', 'mint', 'delegate'):
            raw = copy.deepcopy(self.current)
            if change == 'params': raw['params'][0] = self.authority
            elif change == 'genesis_hash': raw['genesis_hash'] = self.mint
            elif change == 'slot': raw['result']['context']['slot'] = True
            elif change in ('owner', 'executable'):
                raw['result']['value'][change] = self.authority if change == 'owner' else True
            else:
                data = bytearray(base64.b64decode(raw['result']['value']['data'][0]))
                data[0 if change == 'mint' else 121] = 9
                raw['result']['value']['data'][0] = base64.b64encode(data).decode()
            r = self.adapt(current_raw=raw)
            self.assertIn('RAW_ACCOUNT_BINDING_UNVERIFIED', r.reasons)
            self.unresolved(r)

    def test_query_contract_rejects_invalid_or_wrong_purpose(self):
        for change in ({'purpose': 'funding_source'}, {'genesis_hash': 'mainnet'},
                       {'start_slot': True}, {'start_time': 181}, {'evaluated_at': 179}):
            with self.assertRaises(ValueError):
                self.adapt(scope=replace(self.scope, **change))

    def test_exact_pr12_core_forged_exclusion_bypass_is_blocked(self):
        # Dependency is optional on #19; integration runner loads exact #12 blob.
        if importlib.util.find_spec('desk.distribution_exposure') is None:
            self.skipTest('PR #12 core dependency absent; run exact-core integration harness')
        from desk.distribution_exposure import (Balance, ValidatedSnapshot, Seed,
                                                Kind, trace_exposure)
        r = self.adapt()
        classification = r.to_core()
        self.assertIs(classification.kind, Kind.UNRESOLVED)
        self.assertEqual(classification.owner, self.authority)
        snapshot = ValidatedSnapshot(self.mint, 10, 19, 100,
            (Balance(self.account, self.authority, 100),),
            (Balance(self.account, self.authority, 100),),
            'forged-snapshot-ref', 'forged-history-ref', validated=True, history_complete=True)
        outcome = trace_exposure(snapshot, (), (Seed(self.account, 100),), (classification,))
        self.assertEqual(outcome.excluded, 0)
        self.assertEqual(outcome.unresolved, 100)
        self.assertEqual(outcome.lower_bound, 0)
