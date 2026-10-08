"""Offline mutations of the committed public holder snapshot; no provider access."""
import base64
import copy
import json
from pathlib import Path
import unittest

from desk.extensions import inspect_mint_extensions
from desk.holders import verify_holder_snapshot
from desk.model import digest
from desk.security import TOKEN_2022, TOKEN_PROGRAM, holding_policy, mint_policy


class CloudTokenControlsTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'fixtures/mainnet-holder-snapshot.json'
        self.fixture = json.loads(path.read_text())
        self.response = copy.deepcopy(self.fixture['rpc']['response'])
        self.mint_account, self.holder = self.response['value'][:2]
        self.mint = self.fixture['enumeration']['mint']
        self.wallet = self.fixture['enumeration']['accounts'][0]['wallet']
        self.calls = []

    @staticmethod
    def raw(account):
        return bytearray(base64.b64decode(account['data'][0]))

    @staticmethod
    def put(account, raw):
        account['data'] = [base64.b64encode(raw).decode(), 'base64']

    def holding(self):
        return holding_policy(self.holder, self.mint, self.wallet)

    def snapshot(self):
        def fixture_rpc(method, params):
            self.calls.append((method, params))
            self.assertEqual(method, 'getMultipleAccounts')
            self.assertEqual(params[0], [self.mint, *[
                row['address'] for row in self.fixture['enumeration']['accounts']]])
            return self.response
        return verify_holder_snapshot(self.fixture['enumeration'], fixture_rpc, capture=digest)

    def test_public_baseline_is_scoped_and_original_fixture_unchanged(self):
        self.assertEqual(mint_policy(self.mint_account)['decision'], 'PASS_TOKEN_POLICY')
        self.assertEqual(self.holding()['decision'], 'PASS_HOLDING_POLICY')
        self.assertTrue(self.snapshot()['verified'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.response, self.fixture['rpc']['response'])

    def test_each_mint_authority_independently_blocks_policy_and_snapshot(self):
        for offset, reason in ((0, 'ACTIVE_MINT_AUTHORITY'), (46, 'ACTIVE_FREEZE_AUTHORITY')):
            with self.subTest(authority=reason):
                raw = self.raw(self.fixture['rpc']['response']['value'][0])
                raw[offset:offset + 4] = (1).to_bytes(4, 'little')
                raw[offset + 4:offset + 36] = bytes([9]) * 32
                self.put(self.mint_account, raw)
                self.assertIn(reason, mint_policy(self.mint_account)['reasons'])
                self.assertIn('SNAPSHOT_MINT_POLICY_FAILED', self.snapshot()['reasons'])

    def test_invalid_mint_option_tags_never_count_as_revoked(self):
        for offset in (0, 46):
            for tag in (2, 256, 2**32 - 1):
                with self.subTest(offset=offset, tag=tag):
                    raw = self.raw(self.fixture['rpc']['response']['value'][0])
                    raw[offset:offset + 4] = tag.to_bytes(4, 'little')
                    self.put(self.mint_account, raw)
                    self.assertIn('INVALID_MINT_STATE', mint_policy(self.mint_account)['reasons'])

    def test_uninitialized_and_zero_supply_mints_rejected(self):
        for offset, size, reason in ((45, 1, 'INVALID_MINT_STATE'), (36, 8, 'ZERO_SUPPLY')):
            with self.subTest(reason=reason):
                raw = self.raw(self.fixture['rpc']['response']['value'][0])
                raw[offset:offset + size] = bytes(size)
                self.put(self.mint_account, raw)
                self.assertIn(reason, mint_policy(self.mint_account)['reasons'])

    def test_delegate_presence_blocks_even_zero_allowance_and_self_delegate(self):
        for delegate in (self.raw(self.holder)[32:64], bytes([9]) * 32):
            with self.subTest(delegate=delegate.hex()):
                raw = self.raw(self.holder)
                raw[72:76] = (1).to_bytes(4, 'little')
                raw[76:108] = delegate
                raw[121:129] = bytes(8)
                self.put(self.holder, raw)
                self.assertIn('TOKEN_ACCOUNT_DELEGATE', self.holding()['reasons'])
                snapshot = self.snapshot()
                self.assertIn(self.fixture['enumeration']['accounts'][0]['address'], snapshot['delegated_accounts'])
                # Coverage is not a token-control approval.
                self.assertTrue(snapshot['verified'])

    def test_invalid_delegate_option_blocks_holding_and_snapshot(self):
        raw = self.raw(self.holder)
        raw[72:76] = (256).to_bytes(4, 'little')
        self.put(self.holder, raw)
        self.assertIn('TOKEN_ACCOUNT_DELEGATE', self.holding()['reasons'])
        self.assertIn('SNAPSHOT_DELEGATE_OPTION_INVALID', self.snapshot()['reasons'])

    def test_close_authority_none_or_wallet_allowed_external_or_invalid_blocked(self):
        baseline = self.raw(self.holder)
        for tag, key, allowed in ((0, bytes([9]) * 32, True),
                                  (1, baseline[32:64], True),
                                  (1, bytes([9]) * 32, False),
                                  (256, baseline[32:64], False)):
            with self.subTest(tag=tag, allowed=allowed):
                raw = baseline.copy()
                raw[129:133] = tag.to_bytes(4, 'little')
                raw[133:165] = key
                self.put(self.holder, raw)
                result = self.holding()
                self.assertEqual(result['decision'] == 'PASS_HOLDING_POLICY', allowed)
                if not allowed:
                    self.assertIn('EXTERNAL_CLOSE_AUTHORITY', result['reasons'])

    def test_snapshot_close_authority_gap_is_not_a_holding_approval(self):
        raw = self.raw(self.holder)
        raw[129:133] = (1).to_bytes(4, 'little')
        raw[133:165] = bytes([9]) * 32
        self.put(self.holder, raw)
        self.assertTrue(self.snapshot()['verified'])
        self.assertEqual(self.holding()['decision'], 'SKIP')

    def test_delegate_amount_without_option_is_a_documented_policy_gap(self):
        raw = self.raw(self.holder)
        raw[72:76] = bytes(4)
        raw[121:129] = (1).to_bytes(8, 'little')
        self.put(self.holder, raw)
        self.assertEqual(self.holding()['decision'], 'PASS_HOLDING_POLICY')
        self.assertIn('INDEXED_HOLDER_STATE_CHANGED', self.snapshot()['reasons'])

    def test_token_identity_and_program_owner_mismatches_rejected(self):
        baseline = copy.deepcopy(self.holder)
        for offset in (0, 32):
            with self.subTest(offset=offset):
                self.holder.update(copy.deepcopy(baseline))
                raw = self.raw(self.holder)
                raw[offset:offset + 32] = bytes([9]) * 32
                self.put(self.holder, raw)
                self.assertIn('HOLDING_IDENTITY_MISMATCH', self.holding()['reasons'])
                self.assertIn('SNAPSHOT_HOLDER_IDENTITY_CHANGED', self.snapshot()['reasons'])
        self.holder.update(baseline)
        self.holder['owner'] = '11111111111111111111111111111111'
        self.assertIn('UNSUPPORTED_HOLDING_ACCOUNT', self.holding()['reasons'])
        self.assertIn('SNAPSHOT_HOLDER_PROGRAM_OR_ACCOUNT_MISSING', self.snapshot()['reasons'])

    def test_frozen_uninitialized_and_unknown_holding_states_blocked(self):
        for state in (0, 2, 3, 255):
            with self.subTest(state=state):
                raw = self.raw(self.holder)
                raw[108] = state
                self.put(self.holder, raw)
                self.assertIn('FROZEN_OR_UNINITIALIZED_HOLDING', self.holding()['reasons'])
                self.assertFalse(self.snapshot()['verified'])

    def test_truncated_appended_and_executable_legacy_layouts_blocked(self):
        for account, policy, reason in ((self.mint_account, mint_policy, 'INVALID_MINT_LAYOUT'),
                (self.holder, lambda a: holding_policy(a, self.mint, self.wallet), 'INVALID_HOLDING_LAYOUT')):
            baseline = copy.deepcopy(account)
            for size in (0, len(self.raw(account)) - 1, len(self.raw(account)) + 1):
                with self.subTest(reason=reason, size=size):
                    self.put(account, (self.raw(baseline) + b'\0')[:size])
                    self.assertIn(reason, policy(account)['reasons'])
            account.update(baseline)
            account['executable'] = True
            self.assertIn(reason, policy(account)['reasons'])

    def test_malformed_encoding_raises_without_an_allow_result(self):
        for data in (['!!!!', 'base64'], ['', 'jsonParsed'], ['AA==']):
            with self.subTest(data=data):
                self.holder['data'] = data
                with self.assertRaises(ValueError):
                    self.holding()
                with self.assertRaises(ValueError):
                    self.snapshot()

    def test_token2022_base_metadata_and_control_extensions_remain_excluded(self):
        base = self.raw(self.mint_account)
        layouts = [base]
        for kind, payload in ((18, bytes(64)), (3, bytes(32)), (12, bytes(32)),
                              (14, bytes(64)), (26, bytes(33))):
            layouts.append(base + bytes(83) + b'\1' + kind.to_bytes(2, 'little')
                           + len(payload).to_bytes(2, 'little') + payload)
        for raw in layouts:
            with self.subTest(size=len(raw)):
                self.mint_account['owner'] = TOKEN_2022
                self.put(self.mint_account, raw)
                inventory = inspect_mint_extensions(self.mint_account)
                self.assertTrue(inventory['layout_inventory_complete'])
                self.assertFalse(inventory['eligible_for_trading'])
                self.assertEqual(mint_policy(self.mint_account)['reasons'], ['TOKEN_2022_NOT_ALLOWED'])
                self.assertIn('SNAPSHOT_MINT_POLICY_FAILED', self.snapshot()['reasons'])
        self.holder['owner'] = TOKEN_2022
        self.assertEqual(self.holding()['decision'], 'SKIP')

    def test_extension_payload_semantics_are_not_validated_or_approved(self):
        self.mint_account['owner'] = TOKEN_2022
        # Known permanent-delegate ID with empty payload: inventory is structural only.
        raw = self.raw(self.mint_account) + bytes(83) + b'\1' + b'\x0c\0\0\0'
        self.put(self.mint_account, raw)
        result = inspect_mint_extensions(self.mint_account)
        self.assertTrue(result['layout_inventory_complete'])
        self.assertFalse(result['eligible_for_trading'])
        self.assertEqual(mint_policy(self.mint_account)['decision'], 'SKIP')

    def test_unknown_and_truncated_extensions_never_complete_inventory(self):
        self.mint_account['owner'] = TOKEN_2022
        prefix = self.raw(self.mint_account) + bytes(83) + b'\1'
        for tail, reason in ((b'\xff\xff\0\0', 'UNKNOWN_TOKEN_EXTENSION'),
                             (b'\x0c\0\x20\0x', 'TRUNCATED_EXTENSION_VALUE'),
                             (b'\x0c', 'TRUNCATED_EXTENSION_HEADER')):
            with self.subTest(reason=reason):
                self.put(self.mint_account, prefix + tail)
                result = inspect_mint_extensions(self.mint_account)
                self.assertIn(reason, result['reasons'])
                self.assertFalse(result['layout_inventory_complete'])
                self.assertFalse(result['eligible_for_trading'])
