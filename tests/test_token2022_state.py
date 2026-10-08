"""Synthetic source-layout tests, NOT captured mint/account state or evidence.

Fixtures below are constructed from the pinned Rust Pack/Borsh/TLV layouts.
No signer, broadcast, transport or URI lookup. mainnet-launch.json is unchanged
and is used only to demonstrate that its notification supplies no raw state.
"""
import ast
import copy
import hashlib
import json
from pathlib import Path
import struct
import unittest

from solders.pubkey import Pubkey

from research.token2022_state import (decode_token2022_state, TOKEN_2022,
                                     MAX_STATE_BYTES, STRING_LIMITS, SOURCES)

ROOT = Path(__file__).resolve().parents[1]
MINT = str(Pubkey.from_bytes(bytes([17]) * 32))
OWNER = str(Pubkey.from_bytes(bytes([29]) * 32))
ACCOUNT = str(Pubkey.from_bytes(bytes([41]) * 32))
OTHER = str(Pubkey.from_bytes(bytes([53]) * 32))
TEXT = {'name': 'Monolith DEX', 'symbol': 'MONOLITH', 'uri': 'x' * 80}


def key(value):
    return bytes(Pubkey.from_string(value))


def option(value=None, stale=None, numeric=False):
    width = 8 if numeric else 32
    body = ((int(value).to_bytes(8, 'little') if numeric else key(value))
            if value is not None else (stale or bytes(width)))
    return struct.pack('<I', int(value is not None)) + body


def string(value):
    data = value.encode('utf-8') if isinstance(value, str) else value
    return struct.pack('<I', len(data)) + data


def tlv(kind, data=b''):
    return struct.pack('<HH', kind, len(data)) + data


def metadata(*, authority=None, mint=MINT, text=None, pairs=()):
    text = TEXT if text is None else text
    return ((key(authority) if authority is not None else bytes(32)) + key(mint)
            + b''.join(string(text[f]) for f in ('name', 'symbol', 'uri'))
            + struct.pack('<I', len(pairs))
            + b''.join(string(k) + string(v) for k, v in pairs))


def mint_base(*, authority=None, freeze=None, initialized=1, decimals=6,
              supply=10**15, stale=None):
    return (option(authority, stale) + struct.pack('<QBB', supply, decimals, initialized)
            + option(freeze, stale))


def mint_bytes(*, base=None, pointer_authority=None, pointer=MINT,
               metadata_data=None, entries=None):
    pointer_data = ((key(pointer_authority) if pointer_authority is not None else bytes(32))
                    + (key(pointer) if pointer is not None else bytes(32)))
    if entries is None:
        entries = tlv(18, pointer_data) + tlv(19, metadata() if metadata_data is None else metadata_data)
    return (mint_base() if base is None else base) + bytes(83) + b'\x01' + entries


def account_bytes(*, mint=MINT, owner=OWNER, amount=100, delegate=None,
                  allowance=0, state=1, native=None, close=None, stale=None,
                  entries=None):
    return (key(mint) + key(owner) + struct.pack('<Q', amount)
            + option(delegate, stale) + bytes([state]) + option(native, numeric=True)
            + struct.pack('<Q', allowance) + option(close, stale) + b'\x02'
            + (tlv(7) if entries is None else entries))


def decode_mint(raw=None, **kwargs):
    arguments = dict(kind='mint', address=MINT, program_owner=TOKEN_2022, executable=False)
    arguments.update(kwargs)
    return decode_token2022_state(raw, **arguments)


def decode_account(raw, **kwargs):
    arguments = dict(kind='account', address=ACCOUNT, program_owner=TOKEN_2022,
                     executable=False, expected_mint=MINT, expected_token_owner=OWNER)
    arguments.update(kwargs)
    return decode_token2022_state(raw, **arguments)


def codes(result):
    return {d['code'] for d in result['diagnostics']}


class Token2022StateTests(unittest.TestCase):
    def rejected(self, result, code):
        self.assertIn(code, codes(result))
        self.assertFalse(result['structural_profile_match'])
        self.unapproved(result)

    def unapproved(self, result):
        for flag in ('snapshot_authenticated', 'lifecycle_verified',
                     'authenticated_lifecycle_accepted', 'eligible'):
            self.assertIs(result[flag], False)
        self.assertIn('PREDECESSOR_STATE_AND_CONTROL_HISTORY_MISSING', result['unresolved_context'])

    def test_synthetic_mint_exact_payload_and_metadata_bindings(self):
        raw = mint_bytes()
        self.assertEqual(len(raw), 418)
        result = decode_mint(raw, expected_metadata=TEXT)
        self.assertTrue(result['decoding_complete'])
        self.assertTrue(result['structural_profile_match'])
        self.assertEqual(result['base']['supply'], 10**15)
        self.assertEqual(result['extensions'][1]['decoded']['mint'], MINT)
        self.assertFalse(result['extensions'][0]['decoded']['authority']['present'])
        self.assertFalse(result['extensions'][1]['decoded']['update_authority']['present'])
        self.assertEqual(result['raw_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result['raw_hex'], raw.hex())
        self.unapproved(result)

    def test_variable_metadata_lengths_not_fixed_to_418(self):
        for size in (0, 1, 93, 2048):
            with self.subTest(size=size):
                text = dict(TEXT, uri='x' * size)
                raw = mint_bytes(metadata_data=metadata(text=text))
                result = decode_mint(raw, expected_metadata=text)
                self.assertTrue(result['structural_profile_match'])
                self.assertEqual(len(raw), 338 + size)
                self.assertEqual(result['extensions'][1]['length'], 100 + size)
                self.unapproved(result)
        self.assertEqual(len(mint_bytes(metadata_data=metadata(text=dict(TEXT, uri='x'*93)))), 431)

    def test_synthetic_account_preserves_raw_amount_and_effective_closer(self):
        result = decode_account(account_bytes(amount=2**64 - 1))
        self.assertTrue(result['structural_profile_match'])
        self.assertEqual(result['base']['amount'], 2**64 - 1)
        self.assertEqual(result['base']['effective_close_authority'], OWNER)
        self.assertTrue(result['extensions'][0]['decoded']['immutable_owner_present'])
        self.unapproved(result)

    def test_none_coption_stale_payload_is_not_active_authority(self):
        stale = key(OTHER)
        for result in (decode_mint(mint_bytes(base=mint_base(stale=stale))),
                       decode_account(account_bytes(stale=stale))):
            self.assertTrue(result['structural_profile_match'])
            field = 'mint_authority' if result['kind'] == 'mint' else 'delegate'
            self.assertIsNone(result['base'][field]['value'])
            self.assertEqual(result['base'][field]['payload_hex'], stale.hex())
            self.unapproved(result)

    def test_some_zero_key_differs_from_nullable_none(self):
        zero = str(Pubkey.from_bytes(bytes(32)))
        result = decode_mint(mint_bytes(base=mint_base(authority=zero)))
        self.rejected(result, 'MINT_AUTHORITY_PRESENT')
        self.assertEqual(result['base']['mint_authority']['value'], zero)
        self.assertIsNone(result['extensions'][0]['decoded']['authority']['value'])

    def test_all_coption_tags_reject_noncanonical_values(self):
        for raw, decode, offsets in ((mint_bytes(), decode_mint, (0, 46)),
                                     (account_bytes(), decode_account, (72, 109, 129))):
            for offset in offsets:
                for tag in (2, 256, 0x1000000, 0xffffffff):
                    with self.subTest(offset=offset, tag=tag):
                        mutated = bytearray(raw)
                        mutated[offset:offset+4] = struct.pack('<I', tag)
                        result = decode(mutated)
                        self.rejected(result, 'COPTION_TAG_INVALID')
                        self.assertFalse(result['decoding_complete'])
                        self.assertEqual(result['diagnostics'][-1]['offset'], offset)

    def test_active_mint_and_freeze_roles_are_separately_retained(self):
        result = decode_mint(mint_bytes(base=mint_base(authority=OWNER, freeze=OTHER)))
        self.rejected(result, 'MINT_AUTHORITY_PRESENT')
        self.rejected(result, 'FREEZE_AUTHORITY_PRESENT')
        self.assertTrue(result['decoding_complete'])
        self.assertEqual(result['base']['freeze_authority']['value'], OTHER)

    def test_mint_initialization_byte_strict(self):
        for value in (2, 255):
            self.rejected(decode_mint(mint_bytes(base=mint_base(initialized=value))),
                          'MINT_INITIALIZED_BYTE_INVALID')
        result = decode_mint(mint_bytes(base=mint_base(initialized=0)))
        self.rejected(result, 'MINT_UNINITIALIZED')
        self.assertTrue(result['decoding_complete'])

    def test_supply_and_decimals_are_exact_profile_not_ui_amounts(self):
        for arguments in ({'supply': 10**15-1}, {'decimals': 9}, {'supply': 0}):
            result = decode_mint(mint_bytes(base=mint_base(**arguments)))
            self.rejected(result, 'MINT_SUPPLY_OR_DECIMALS_PROFILE_MISMATCH')
            self.assertTrue(result['decoding_complete'])

    def test_delegate_zero_allowance_still_adverse(self):
        for allowance in (0, 1, 2**64-1):
            result = decode_account(account_bytes(delegate=OTHER, allowance=allowance))
            self.rejected(result, 'DELEGATE_PRESENT')
            self.assertTrue(result['decoding_complete'])
            self.assertEqual(result['base']['delegated_amount'], allowance)

    def test_orphan_allowance_is_profile_contradiction(self):
        result = decode_account(account_bytes(allowance=1))
        self.rejected(result, 'DELEGATED_AMOUNT_WITHOUT_DELEGATE')
        self.assertTrue(result['decoding_complete'])

    def test_frozen_uninitialized_invalid_account_states(self):
        for value, code in ((0, 'ACCOUNT_UNINITIALIZED'), (2, 'ACCOUNT_FROZEN'),
                            (3, 'ACCOUNT_STATE_BYTE_INVALID'), (255, 'ACCOUNT_STATE_BYTE_INVALID')):
            result = decode_account(account_bytes(state=value))
            self.rejected(result, code)
            self.assertEqual(result['decoding_complete'], value in (0, 2))

    def test_close_authority_even_same_owner_rejected(self):
        for close in (OWNER, OTHER):
            result = decode_account(account_bytes(close=close))
            self.rejected(result, 'CLOSE_AUTHORITY_PRESENT')
            self.assertEqual(result['base']['effective_close_authority'], close)

    def test_native_reserve_zero_is_explicit_some_and_adverse(self):
        for reserve in (0, 123, 2**64-1):
            result = decode_account(account_bytes(native=reserve))
            self.rejected(result, 'IS_NATIVE_PRESENT')
            self.assertEqual(result['base']['is_native']['value'], reserve)

    def test_inactive_native_body_preserved(self):
        raw = bytearray(account_bytes())
        raw[113:121] = struct.pack('<Q', 999)
        result = decode_account(raw)
        self.assertTrue(result['structural_profile_match'])
        self.assertIsNone(result['base']['is_native']['value'])
        self.assertEqual(result['base']['is_native']['payload_hex'], raw[113:121].hex())

    def test_mint_and_token_owner_bindings_reject_redirection(self):
        self.rejected(decode_account(account_bytes(mint=OTHER)), 'ACCOUNT_MINT_BINDING_MISMATCH')
        self.rejected(decode_account(account_bytes(owner=OTHER)), 'ACCOUNT_OWNER_BINDING_MISMATCH')

    def test_pointer_and_metadata_authorities_independent(self):
        result = decode_mint(mint_bytes(pointer_authority=OWNER,
                                      metadata_data=metadata(authority=OTHER)))
        self.rejected(result, 'METADATA_POINTER_AUTHORITY_PRESENT')
        self.rejected(result, 'METADATA_UPDATE_AUTHORITY_PRESENT')
        self.assertEqual(result['extensions'][0]['decoded']['authority']['value'], OWNER)
        self.assertEqual(result['extensions'][1]['decoded']['update_authority']['value'], OTHER)

    def test_pointer_redirect_or_null_rejected(self):
        for pointer in (OTHER, None):
            self.rejected(decode_mint(mint_bytes(pointer=pointer)), 'METADATA_POINTER_NOT_SELF')

    def test_foreign_metadata_mint_rejected(self):
        self.rejected(decode_mint(mint_bytes(metadata_data=metadata(mint=OTHER))),
                      'METADATA_MINT_BINDING_MISMATCH')

    def test_expected_metadata_compares_exact_unicode_and_all_fields(self):
        text = {'name': '猫', 'symbol': 'é', 'uri': 'https://untrusted.invalid/a'}
        result = decode_mint(mint_bytes(metadata_data=metadata(text=text)), expected_metadata=text)
        self.assertTrue(result['structural_profile_match'])
        self.assertEqual(result['extensions'][1]['decoded']['name']['byte_length'], 3)
        self.rejected(decode_mint(mint_bytes(metadata_data=metadata(text=text)),
                                 expected_metadata=dict(text, symbol='e\u0301')),
                      'METADATA_STRING_BINDING_MISMATCH')

    def test_expected_metadata_shape_cannot_be_partial_or_summary_flags(self):
        for binding in ({}, {'name': 'x'}, dict(TEXT, safe=True), list(TEXT), dict(TEXT, uri=None)):
            self.rejected(decode_mint(mint_bytes(), expected_metadata=binding), 'EXPECTED_METADATA_INVALID')

    def test_string_bounds_are_utf8_byte_bounds(self):
        for field in ('name', 'symbol', 'uri'):
            limit = STRING_LIMITS[field]
            text = dict(TEXT, **{field: 'x' * limit})
            self.assertTrue(decode_mint(mint_bytes(metadata_data=metadata(text=text)))['structural_profile_match'])
            text[field] += 'x'
            self.rejected(decode_mint(mint_bytes(metadata_data=metadata(text=text))),
                          'METADATA_STRING_BOUND_EXCEEDED')
        self.rejected(decode_mint(mint_bytes(metadata_data=metadata(text=dict(TEXT, symbol='猫'*22)))),
                      'METADATA_STRING_BOUND_EXCEEDED')

    def test_huge_borsh_length_cannot_allocate_or_skip(self):
        data = bytes(32) + key(MINT) + struct.pack('<I', 0xffffffff)
        self.rejected(decode_mint(mint_bytes(metadata_data=data)), 'METADATA_STRING_BOUND_EXCEEDED')

    def test_invalid_utf8_and_truncated_borsh(self):
        self.rejected(decode_mint(mint_bytes(metadata_data=metadata(text=dict(TEXT, name=b'\xff')))),
                      'METADATA_UTF8_INVALID')
        valid = metadata()
        for length in (0, 31, 32, 63, 64, 66, 76, len(valid)-1):
            self.rejected(decode_mint(mint_bytes(metadata_data=valid[:length])), 'METADATA_TRUNCATED')

    def test_metadata_suffix_even_zero_rejected(self):
        for suffix in (b'\0', b'evil', tlv(12, bytes(32))):
            self.rejected(decode_mint(mint_bytes(metadata_data=metadata()+suffix)), 'METADATA_TRAILING_BYTES')

    def test_additional_metadata_nonempty_duplicate_and_bounded(self):
        for pairs in ((('x','y'),), (('x','y'),('x','z'))):
            result = decode_mint(mint_bytes(metadata_data=metadata(pairs=pairs)))
            self.rejected(result, 'ADDITIONAL_METADATA_UNSUPPORTED')
            self.assertEqual(len(result['extensions'][1]['decoded']['additional_metadata']), len(pairs))
        self.rejected(decode_mint(mint_bytes(metadata_data=metadata(pairs=[('a','b')]*17))),
                      'METADATA_PAIR_BOUND_EXCEEDED')
        for pairs in ([('a'*257,'b')], [('a','b'*2049)]):
            self.rejected(decode_mint(mint_bytes(metadata_data=metadata(pairs=pairs))),
                          'METADATA_STRING_BOUND_EXCEEDED')

    def test_metadata_partial_authority_witness_survives_failure(self):
        result = decode_mint(mint_bytes(metadata_data=key(OTHER)))
        self.rejected(result, 'METADATA_TRUNCATED')
        self.assertEqual(result['extensions'][1]['decoded']['update_authority']['value'], OTHER)

    def test_tlv_order_is_not_effect_order(self):
        entries = tlv(19, metadata()) + tlv(18, bytes(32)+key(MINT))
        result = decode_mint(mint_bytes(entries=entries))
        self.assertTrue(result['structural_profile_match'])
        self.assertEqual([r['type'] for r in result['extensions']], [19,18])
        self.unapproved(result)

    def test_duplicate_types_all_candidate_extensions(self):
        for raw, decode in ((mint_bytes()+tlv(18, bytes(64)), decode_mint),
                            (mint_bytes()+tlv(19, metadata()), decode_mint),
                            (account_bytes()+tlv(7), decode_account)):
            self.rejected(decode(raw), 'TLV_DUPLICATE_TYPE')

    def test_exact_set_rejects_missing_and_base_only(self):
        for raw, decode in ((mint_base(), decode_mint), (mint_bytes(entries=tlv(18, bytes(32)+key(MINT))), decode_mint),
                            (mint_bytes(entries=tlv(19, metadata())), decode_mint),
                            (account_bytes()[:165], decode_account),
                            (account_bytes(entries=b''), decode_account)):
            result = decode(raw)
            self.rejected(result, 'EXACT_EXTENSION_SET_MISMATCH')
            self.assertTrue(result['decoding_complete'])

    def test_all_other_official_ids_and_unknown_rejected_even_zero_inactive(self):
        for extension in list(range(1,29)) + [65535, 40000]:
            for kind, raw, decode, permitted in (
                    ('mint', mint_bytes(), decode_mint, {18,19}),
                    ('account', account_bytes(), decode_account, {7})):
                if extension in permitted:
                    continue
                with self.subTest(kind=kind, extension=extension):
                    result = decode(raw+tlv(extension, bytes(64)))
                    self.rejected(result, 'EXTENSION_TYPE_UNSUPPORTED')
                    self.assertFalse(result['decoding_complete'])
                    self.assertEqual(result['extensions'][-1]['raw_hex'], bytes(64).hex())

    def test_wrong_class_extensions_cannot_be_reinterpreted(self):
        self.rejected(decode_mint(mint_bytes(entries=tlv(7))), 'EXTENSION_TYPE_UNSUPPORTED')
        for extension in (18,19):
            self.rejected(decode_account(account_bytes(entries=tlv(extension, bytes(64)))),
                          'EXTENSION_TYPE_UNSUPPORTED')

    def test_known_payload_lengths_exact(self):
        for size in (0,63,65):
            self.rejected(decode_mint(mint_bytes(entries=tlv(18, bytes(size)))),
                          'METADATA_POINTER_LENGTH_INVALID')
        self.rejected(decode_account(account_bytes(entries=tlv(7,b'\0'))),
                      'IMMUTABLE_OWNER_LENGTH_INVALID')

    def test_tlv_truncated_header_value_and_reserved_padding(self):
        for suffix in (b'\0', b'\0\0', b'\0\0\0', b'\0\0\0\0', b'\0'*8,
                       b'\0\0hidden', b'\x12\0', b'\x12\0\xff\xff'):
            result = decode_mint(mint_bytes()+suffix)
            self.assertFalse(result['structural_profile_match'])
            self.assertFalse(result['decoding_complete'])
            self.assertTrue(any(d['category']=='layout' for d in result['diagnostics']))
            self.unapproved(result)
        self.rejected(decode_mint(mint_bytes()[:-1]), 'TLV_VALUE_TRUNCATED')

    def test_mint_padding_and_account_type_strict(self):
        for position in (82,100,164):
            raw = bytearray(mint_bytes()); raw[position] = 1
            self.rejected(decode_mint(raw), 'MINT_PADDING_NONZERO')
        for raw, decode, wrong_types in ((mint_bytes(), decode_mint, (0,2,255)),
                                         (account_bytes(), decode_account, (0,1,255))):
            for value in wrong_types:
                changed = bytearray(raw); changed[165] = value
                self.rejected(decode(changed), 'ACCOUNT_TYPE_MISMATCH')

    def test_base_prefix_truncation_and_multisig_collision(self):
        for raw, decode, minimum in ((mint_bytes(), decode_mint,82), (account_bytes(),decode_account,165)):
            for size in (0,1,minimum-1):
                self.rejected(decode(raw[:size]), 'BASE_STATE_TRUNCATED')
            self.rejected(decode(raw[:355].ljust(355,b'\0')), 'MULTISIG_LENGTH_COLLISION')
        for size in (83,100,165):
            self.rejected(decode_mint(mint_bytes()[:size]), 'EXTENDED_PREFIX_TRUNCATED')

    def test_only_exact_official_multisig_adjustment_padding_permitted(self):
        raw = mint_bytes(metadata_data=metadata(text=dict(TEXT,uri='x'*17)))
        self.assertEqual(len(raw),355)
        self.rejected(decode_mint(raw),'MULTISIG_LENGTH_COLLISION')
        result = decode_mint(raw+b'\0\0')
        self.assertTrue(result['structural_profile_match'])
        self.assertEqual(result['tlv_padding']['offset'],355)
        self.unapproved(result)
        for suffix in (b'\0\x01',b'\0\0\0',b'\0\0\0\0'):
            self.assertFalse(decode_mint(raw+suffix)['structural_profile_match'])
        self.assertFalse(decode_mint(mint_bytes()+b'\0\0')['structural_profile_match'])

    def test_envelope_presence_not_truthiness_and_legacy_distinction(self):
        for kwargs, code in (({'address':None},'ACCOUNT_ADDRESS_MISSING'),
                             ({'program_owner':None},'PROGRAM_OWNER_MISSING'),
                             ({'program_owner':'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'},'PROGRAM_OWNER_NOT_TOKEN2022'),
                             ({'executable':None},'EXECUTABLE_MISSING'),
                             ({'executable':0},'EXECUTABLE_NOT_EXPLICIT_FALSE'),
                             ({'executable':True},'EXECUTABLE_NOT_EXPLICIT_FALSE')):
            self.rejected(decode_mint(mint_bytes(), **kwargs), code)
        for value in ('bad', '', '0'*44, 5, {}, MINT+'1'):
            self.rejected(decode_mint(mint_bytes(),address=value), 'ACCOUNT_ADDRESS_INVALID')
        self.rejected(decode_account(account_bytes(), expected_mint=None),'EXPECTED_MINT_MISSING')
        self.rejected(decode_account(account_bytes(), expected_token_owner=None),'EXPECTED_TOKEN_OWNER_MISSING')

    def test_irrelevant_bindings_rejected(self):
        self.rejected(decode_mint(mint_bytes(),expected_mint=MINT),'ACCOUNT_BINDINGS_NOT_APPLICABLE')
        self.rejected(decode_account(account_bytes(),expected_metadata=TEXT),'EXPECTED_METADATA_NOT_APPLICABLE')

    def test_missing_raw_and_invalid_raw_types_never_synthesize(self):
        self.rejected(decode_mint(None),'RAW_STATE_MISSING')
        for raw in ('00', [], {}, 0):
            result = decode_mint(raw)
            self.rejected(result,'RAW_STATE_TYPE_INVALID')
            self.assertIsNone(result['raw_hex'])
        self.rejected(decode_mint(bytes(MAX_STATE_BYTES+1)),'RAW_STATE_BOUND_EXCEEDED')
        self.rejected(decode_token2022_state(mint_bytes(),kind='multisig'),'STATE_KIND_UNSUPPORTED')

    def test_mainnet_notification_remains_raw_state_missing_and_unchanged(self):
        raw = (ROOT/'fixtures/mainnet-launch.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         'd80cf9876fdb3e9b465e4103fa14bf2baaf09483dad9a27a57ff7339624b3bf2')
        notification = json.loads(raw)
        self.assertIsInstance(notification, dict)
        # This fixture has no getAccountInfo/state payload. Do not derive it from
        # parsed init/control instructions, allocations, events or token balances.
        result = decode_mint(None,address='AoPfwh6vExgSrfzX2ALPBEpWS2wSjhcG2ZxKdN1Vpump')
        self.rejected(result,'RAW_STATE_MISSING')
        self.assertEqual(result['extensions'],[])
        self.assertEqual(result['base'],{})
        self.assertIsNone(result['raw_sha256'])

    def test_transient_controls_restored_endpoint_is_not_lifecycle_proof(self):
        before = decode_account(account_bytes())
        adverse = decode_account(account_bytes(delegate=OTHER,allowance=1,close=OTHER))
        after = decode_account(account_bytes())
        self.assertTrue(before['structural_profile_match'])
        self.rejected(adverse,'DELEGATE_PRESENT')
        self.assertTrue(after['structural_profile_match'])
        self.unapproved(after)
        self.assertEqual(before['raw_sha256'],after['raw_sha256'])

    def test_input_and_results_independent_and_json_serializable(self):
        data = bytearray(mint_bytes()); text = dict(TEXT)
        result = decode_mint(data,expected_metadata=text)
        original = copy.deepcopy(result)
        data[0]=255; text['name']='mutated'
        self.assertEqual(result,original)
        result['extensions'][0]['decoded']['authority']['value']='mutated'
        self.assertIsNone(decode_mint(mint_bytes())['extensions'][0]['decoded']['authority']['value'])
        json.dumps(original)

    def test_official_pins_and_no_production_imports(self):
        self.assertEqual(len(SOURCES),5)
        self.assertTrue(all(len(row[1])==40 and len(row[3])==40 for row in SOURCES))
        for path in (ROOT/'desk').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node,ast.Import):
                    self.assertFalse(any('token2022_state' in n.name for n in node.names),str(path))
                if isinstance(node,ast.ImportFrom):
                    self.assertNotIn('token2022_state',node.module or '',str(path))
                    self.assertFalse(any('token2022_state' in n.name for n in node.names),str(path))


if __name__ == '__main__':
    unittest.main()
