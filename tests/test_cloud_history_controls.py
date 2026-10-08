"""Synthetic PR24 raw-history attacks; provider transport is fixture-only."""
import copy
import unittest

from desk.account_history import account_inventory
from desk.decode import decode, TOKEN_CONTROL_OPERATIONS
from desk.history import collect_history
from desk.reconcile import reconcile_movements
from desk.security import TOKEN_PROGRAM, TOKEN_2022
from tests import test_ownership_multihistory_integration as multi


class CloudHistoryControlsTests(unittest.TestCase):
    def setUp(self):
        self.history = multi.OwnershipMultiHistoryIntegrationTests()
        self.history.setUp()
        self.addCleanup(self.history.doCleanups)
        self.raw = copy.deepcopy(self.history.records[-1])
        self.mint = self.history.f['mint']
        self.account = self.history.f['accounts'][0]
        self.owner = self.history.f['owners'][self.account]

    def operation(self, kind, program=TOKEN_PROGRAM, info=None):
        return {'programId': program, 'parsed': {'type': kind, 'info': info if info is not None else {
            'account': self.account, 'source': self.account, 'mint': self.mint,
            'owner': self.owner, 'authority': self.owner, 'authorityType': 'CloseAccount',
            'newAuthority': self.history.f['owners'][self.history.f['accounts'][1]],
            'delegate': self.owner, 'amount': '0', 'tokenAmount': {'amount': '0', 'decimals': 0}}}}

    def add(self, raw, operations, inner=False):
        if inner:
            raw['meta']['innerInstructions'].append({'index': 0, 'instructions': operations})
        else:
            raw['transaction']['message']['instructions'].extend(operations)

    def observed(self, raw):
        result = decode(raw)
        result['commitment'] = 'finalized_provider_response'
        return result

    def assert_blocked(self, observation):
        self.assertIn('UNDECODED_TOKEN_INSTRUCTION', observation['limitations'])
        result = reconcile_movements(self.mint, observation)
        self.assertFalse(result['passed'])
        self.assertIn('MOVEMENT_DECODING_INCOMPLETE', result['reasons'])

    def test_each_control_operation_outer_and_cpi_never_silently_disappears(self):
        baseline = self.observed(self.raw)
        self.assertTrue(reconcile_movements(self.mint, baseline)['passed'])
        for program in (TOKEN_PROGRAM, TOKEN_2022):
            for kind in sorted(TOKEN_CONTROL_OPERATIONS):
                for inner in (False, True):
                    with self.subTest(program=program, kind=kind, inner=inner):
                        raw = copy.deepcopy(self.raw)
                        self.add(raw, [self.operation(kind, program)], inner)
                        obs = self.observed(raw)
                        self.assert_blocked(obs)
                        self.assertIn('UNSUPPORTED_TOKEN_CONTROL_OPERATION', obs['limitations'])
                        self.assertEqual(obs['transfers'], baseline['transfers'])
                        event = obs['token_control_operations'][-1]
                        self.assertEqual(event['program'], program)
                        self.assertEqual(event['type'], kind)
                        self.assertEqual('.' in event['instruction'], inner)
                        # The diagnostic event cannot attest new authority state.
                        self.assertNotIn('new_authority', event)

    def test_all_set_authority_roles_are_unsupported_even_revocation(self):
        for role in ('MintTokens', 'FreezeAccount', 'AccountOwner', 'CloseAccount', 'unknown'):
            for new in (None, self.owner):
                with self.subTest(role=role, new=new):
                    raw = copy.deepcopy(self.raw)
                    ix = self.operation('setAuthority')
                    ix['parsed']['info'].update(authorityType=role, newAuthority=new)
                    self.add(raw, [ix])
                    self.assert_blocked(self.observed(raw))

    def test_unknown_and_nonmovement_operations_block_outer_and_cpi(self):
        for kind in ('futureAuthority', 'syncNative', 'getAccountDataSize',
                     'amountToUiAmount', 'uiAmountToAmount', 'initializeMetadataPointer'):
            for inner in (False, True):
                with self.subTest(kind=kind, inner=inner):
                    raw = copy.deepcopy(self.raw)
                    self.add(raw, [self.operation(kind)], inner)
                    self.assert_blocked(self.observed(raw))

    def test_malformed_parsed_shapes_cannot_pass_accounting(self):
        for parsed in ({}, {'type': None}, {'type': ['transfer']},
                       {'type': 'transfer', 'info': None}, {'type': 'transfer', 'info': []},
                       {'type': 'setAuthority'}, {'type': 'setAuthority', 'info': 'bad'}):
            for inner in (False, True):
                with self.subTest(parsed=parsed, inner=inner):
                    raw = copy.deepcopy(self.raw)
                    self.add(raw, [{'programId': TOKEN_PROGRAM, 'parsed': parsed}], inner)
                    self.assert_blocked(self.observed(raw))

    def test_malformed_supported_operation_quarantines_query(self):
        for kind in ('transfer', 'transferChecked', 'mintTo', 'burn',
                     'initializeAccount', 'initializeMint', 'closeAccount'):
            with self.subTest(kind=kind):
                # Missing required fields cannot produce a complete query after replay.
                raw = copy.deepcopy(self.raw)
                self.add(raw, [self.operation(kind, info={})])
                self.history.records[-1] = raw
                _, coverage = collect_history(self.mint, 90, 120, self.history.transport,
                    max_pages=2, capture=self.history.store.save, token_accounts='none',
                    slot_range={'gte': 0, 'lt': 21})
                self.assertFalse(coverage['query_coverage_verified'])
                self.assertIn('HISTORY_DECODE_GAP', coverage['reasons'])

    def test_transient_control_then_restoration_same_transaction_blocks_snapshot(self):
        for first, restore in (('setAuthority', 'setAuthority'), ('approve', 'revoke'),
                               ('freezeAccount', 'thawAccount')):
            for inner in (False, True):
                with self.subTest(first=first, restore=restore, inner=inner):
                    raw = copy.deepcopy(self.raw)
                    ix = self.operation(restore)
                    ix['parsed']['info']['newAuthority'] = self.owner
                    self.add(raw, [self.operation(first), ix], inner)
                    self.history.records[-1] = raw
                    report = self.history.capture_report()
                    history, snapshot = self.history.replay(report)
                    self.assertTrue(history['query_coverage_verified'])
                    self.assertFalse(history['inventory']['initialization_inventory_verified'])
                    self.assertFalse(history['observed_movements']['passed'])
                    self.assertFalse(history['account_continuity']['passed'])
                    self.assertFalse(snapshot['reconciled'])
                    # Restored end balances and the unchanged captured bank cannot
                    # substitute for supported historical control normalization.
                    self.assertEqual(snapshot['observed_supply_raw'], '100')
                    self.assertFalse(snapshot['eligible_for_trading'])

    def test_authority_change_in_separate_transaction_then_restoration_stays_blocked(self):
        from desk.security import base58
        original = self.raw['meta']['postTokenBalances']
        for index in range(2):
            raw = copy.deepcopy(self.raw)
            raw['slot'] = 14 + index
            raw['blockTime'] = 104 + index
            signature = base58(bytes([60 + index]) * 64)
            raw['signature'] = signature
            raw['transaction']['signatures'] = [signature]
            raw['meta']['preTokenBalances'] = copy.deepcopy(original)
            raw['meta']['postTokenBalances'] = copy.deepcopy(original)
            raw['meta']['innerInstructions'] = []
            raw['transaction']['message']['instructions'] = []
            ix = self.operation('setAuthority')
            if index:
                ix['parsed']['info']['newAuthority'] = self.owner
            self.add(raw, [ix], inner=bool(index))
            self.history.records.append(raw)
        report = self.history.capture_report()
        history, snapshot = self.history.replay(report)
        self.assertEqual(history['observed_transaction_count'], 6)
        self.assertFalse(history['observed_movements']['passed'])
        self.assertFalse(history['account_continuity']['passed'])
        self.assertFalse(snapshot['reconciled'])
        self.assertIn('HISTORY_MOVEMENTS_OR_ORDER_UNVERIFIED', snapshot['reasons'])
        self.assertEqual(snapshot['observed_supply_raw'], '100')

    def test_control_inventory_cannot_be_approved_by_removing_limitation_flags(self):
        raw = copy.deepcopy(self.raw)
        self.add(raw, [self.operation('setAuthority')])
        obs = self.observed(raw)
        obs['limitations'] = []
        result = reconcile_movements(self.mint, obs)
        self.assertFalse(result['passed'])
        self.assertIn('UNSUPPORTED_TOKEN_CONTROL_OPERATION', result['reasons'])
        observations = [self.observed(r) for r in self.history.records[:-1]] + [obs]
        coverage = self.history.capture_report()['history_queries'][0]
        inventory = account_inventory(self.mint, observations, coverage)
        self.assertFalse(inventory['initialization_inventory_verified'])
        self.assertIn('ACCOUNT_INITIALIZATION_DECODING_INCOMPLETE', inventory['reasons'])

    def test_failed_control_transaction_has_no_committed_operations(self):
        raw = copy.deepcopy(self.raw)
        self.add(raw, [self.operation('setAuthority')])
        raw['meta']['err'] = {'InstructionError': [0, 'Custom']}
        obs = self.observed(raw)
        self.assertEqual(obs['token_control_operations'], [])
        self.assertEqual(obs['transfers'], [])
        self.assertTrue(reconcile_movements(self.mint, obs)['passed'])

    def test_valid_legacy_multiaccount_initialization_movements_and_closure_survive(self):
        history, snapshot = self.history.replay(self.history.capture_report())
        self.history.assert_matching(history, snapshot, 4)
        self.history.records += self.history.f['lifetime_records']
        self.history.f['snapshot'] = self.history.f['lifetime_snapshot']
        history, snapshot = self.history.replay(self.history.capture_report())
        self.history.assert_matching(history, snapshot, 8)
