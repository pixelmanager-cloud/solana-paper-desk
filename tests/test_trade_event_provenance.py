"""Offline research only: official TradeEvent layout, no evidence approval."""
import hashlib
import json
import unittest
from pathlib import Path
from desk.programs import BorshReader, instruction, unbase58

ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = '8cda1fa30ea658b20909d8aedf002047119388d2'
SOURCE_SHA256 = '38b8abcc5b279bda85cf473e7c6f67bd15eb89df658cf93434687a43c88ad937'
# Exact TradeEvent and referenced Shareholder types from official idl/pump.json.
# Immutable source and full-file digest are documented in the companion report.
LAYOUT = {'TradeEvent': {'kind': 'struct',
                'fields': [{'name': 'mint', 'type': 'pubkey'},
                           {'name': 'sol_amount', 'type': 'u64'},
                           {'name': 'token_amount', 'type': 'u64'},
                           {'name': 'is_buy', 'type': 'bool'},
                           {'name': 'user', 'type': 'pubkey'},
                           {'name': 'timestamp', 'type': 'i64'},
                           {'name': 'virtual_sol_reserves', 'type': 'u64'},
                           {'name': 'virtual_token_reserves', 'type': 'u64'},
                           {'name': 'real_sol_reserves', 'type': 'u64'},
                           {'name': 'real_token_reserves', 'type': 'u64'},
                           {'name': 'fee_recipient', 'type': 'pubkey'},
                           {'name': 'fee_basis_points', 'type': 'u64'},
                           {'name': 'fee', 'type': 'u64'},
                           {'name': 'creator', 'type': 'pubkey'},
                           {'name': 'creator_fee_basis_points', 'type': 'u64'},
                           {'name': 'creator_fee', 'type': 'u64'},
                           {'name': 'track_volume', 'type': 'bool'},
                           {'name': 'total_unclaimed_tokens', 'type': 'u64'},
                           {'name': 'total_claimed_tokens', 'type': 'u64'},
                           {'name': 'current_sol_volume', 'type': 'u64'},
                           {'name': 'last_update_timestamp', 'type': 'i64'},
                           {'name': 'ix_name', 'type': 'string'},
                           {'name': 'mayhem_mode', 'type': 'bool'},
                           {'name': 'cashback_fee_basis_points', 'type': 'u64'},
                           {'name': 'cashback', 'type': 'u64'},
                           {'name': 'buyback_fee_basis_points', 'type': 'u64'},
                           {'name': 'buyback_fee', 'type': 'u64'},
                           {'name': 'shareholders',
                            'type': {'vec': {'defined': {'name': 'Shareholder'}}}},
                           {'name': 'quote_mint', 'type': 'pubkey'},
                           {'name': 'quote_amount', 'type': 'u64'},
                           {'name': 'virtual_quote_reserves', 'type': 'u64'},
                           {'name': 'real_quote_reserves', 'type': 'u64'},
                           {'name': 'holder_rewards_bps', 'type': 'u64'},
                           {'name': 'holder_rewards', 'type': 'u64'},
                           {'name': 'creator_fee_unclaimed', 'type': 'u64'}]},
 'Shareholder': {'kind': 'struct',
                 'fields': [{'name': 'address', 'type': 'pubkey'},
                            {'name': 'share_bps', 'type': 'u16'}]}}
HEADERS = bytes.fromhex('e445a52e51cb9a1dbddb7fd34ee661ee')


def research_decode(raw):
    if raw[:16] != HEADERS:
        raise ValueError('Unexpected event discriminator')
    reader = BorshReader(raw[16:], LAYOUT)
    values = {f['name']: reader.read(f['type']) for f in LAYOUT['TradeEvent']['fields']}
    if reader.pos != len(reader.data):
        raise ValueError('Trailing event bytes')
    return values


class TradeEventProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = (ROOT / 'fixtures/mainnet-launch-raw.json').read_bytes()
        result = json.loads(self.fixture)['result']
        keys = result['transaction']['message']['accountKeys']
        loaded = result['meta'].get('loadedAddresses', {})
        keys = keys + loaded.get('writable', []) + loaded.get('readonly', [])
        group = next(g for g in result['meta']['innerInstructions'] if g['index'] == 4)
        ix = group['instructions'][8]
        self.ix = {'programId': keys[ix['programIdIndex']],
                   'accounts': [keys[i] for i in ix['accounts']], 'data': ix['data']}
        self.raw = unbase58(ix['data'])

    def test_original_fixture_and_event_bytes_unchanged(self):
        self.assertEqual(hashlib.sha256(self.fixture).hexdigest(),
                         '27d481575656d2b254a63bf699a32ce3dd599ce3b55fc8b04da2aa79dd356520')
        self.assertEqual(len(self.raw), 390)
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(),
                         '2b0a30b072ba6ea8d24e7ef60ab16affb307f37ffe16d825ec0a7c95784d2ca8')

    def test_official_layout_extends_pinned_prefix_by_one_u64(self):
        pinned = json.loads((ROOT / 'desk/schemas/pump.json').read_text())
        types = {t['name']: t['type'] for t in pinned['types']}
        self.assertEqual(LAYOUT['TradeEvent']['fields'][:-1], types['TradeEvent']['fields'])
        self.assertEqual(LAYOUT['Shareholder'], types['Shareholder'])
        self.assertEqual(LAYOUT['TradeEvent']['fields'][-1],
                         {'name': 'creator_fee_unclaimed', 'type': 'u64'})
        event = next(e for e in pinned['events'] if e['name'] == 'TradeEvent')
        self.assertEqual(bytes(event['discriminator']), HEADERS[8:])

    def test_exact_whole_event_and_appended_field_offset(self):
        values = research_decode(self.raw)
        self.assertEqual(len(values), 35)
        self.assertEqual(values['ix_name'], 'buy')
        self.assertEqual(values['shareholders'], [])
        self.assertEqual(values['creator_fee_unclaimed'], int.from_bytes(self.raw[382:390], 'little'))
        self.assertEqual(values['creator_fee_unclaimed'], 0)
        reader = BorshReader(self.raw[16:], LAYOUT)
        for field in LAYOUT['TradeEvent']['fields'][:-1]:
            reader.read(field['type'])
        self.assertEqual(reader.pos + 16, 382)

    def test_production_decodes_pinned_syntax_without_economic_approval(self):
        observed = instruction(self.ix)
        self.assertEqual(observed['status'], 'EVENT_DECODED')
        self.assertIs(observed['schema_complete'], True)
        self.assertEqual(observed['schema_file'], 'pump_events_8cda1fa.json')
        self.assertEqual(observed['fields']['creator_fee_unclaimed'], 0)

    def test_every_truncated_event_is_rejected(self):
        for end in range(len(self.raw)):
            with self.subTest(end=end), self.assertRaises(ValueError):
                research_decode(self.raw[:end])

    def test_suffix_and_wrong_discriminators_rejected(self):
        for mutated in (self.raw + b'\x00', b'\x00' + self.raw[1:],
                        self.raw[:8] + b'\x00' + self.raw[9:]):
            with self.subTest(raw=mutated[:16]), self.assertRaises(ValueError):
                research_decode(mutated)

    def test_invalid_boolean_and_vector_length_rejected(self):
        # Derive offsets from the authoritative layout; mutate only scratch bytes.
        reader = BorshReader(self.raw[16:], LAYOUT)
        offsets = {}
        for field in LAYOUT['TradeEvent']['fields']:
            offsets[field['name']] = reader.pos + 16
            reader.read(field['type'])
        for name, replacement in [('is_buy', b'\x02'),
                                  ('shareholders', (257).to_bytes(4, 'little'))]:
            at = offsets[name]
            mutated = self.raw[:at] + replacement + self.raw[at + len(replacement):]
            with self.subTest(field=name), self.assertRaises(ValueError):
                research_decode(mutated)
