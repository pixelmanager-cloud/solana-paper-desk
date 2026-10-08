"""Unchanged historical compiled reference: semantic facts, no finality claim."""
import hashlib
import json
import struct
import unittest
from pathlib import Path

from desk.decode import decode
from desk.programs import instruction, unbase58
from desk.providers import PUMP
from desk.security import TOKEN_PROGRAM


ROOT = Path(__file__).resolve().parents[1] / 'fixtures/legacy_pump_reference'
SOURCE_SHA = '89dcc14ecad1432598e4937da2dfa9955af8637a5ae373418e9631c174991749'
MINT = 'B9Z9mKUoVy5k8KuL2HauUD1mhmfF3PPNnJoK83S1pump'
SIGNATURE = '4Cod1cNGv6RboJ7rSB79yeVCR4Lfd25rFgLY3eiPJfTJjTGyYP1r2i1upAYZHQsWDqUbGd1bhTRm1bpSQcpWMnEz'


class LegacyPumpReferenceTests(unittest.TestCase):
    def setUp(self):
        self.bytes = (ROOT / 'create_buy.json').read_bytes()
        self.raw = json.loads(self.bytes)
        self.manifest = json.loads((ROOT / 'manifest.json').read_text())
        self.message = self.raw['transaction']['message']
        self.keys = self.message['accountKeys']

    def resolved_instruction(self, raw):
        # Resolve existing indices only to inspect a single instruction. Never
        # synthesize jsonParsed metadata, commitment or a production observation.
        return {'programId': self.keys[raw['programIdIndex']],
                'accounts': [self.keys[i] for i in raw['accounts']], 'data': raw['data']}

    def test_original_source_bytes_and_pinned_provenance(self):
        self.assertEqual(hashlib.sha256(self.bytes).hexdigest(), SOURCE_SHA)
        self.assertEqual(len(self.bytes), 12523)
        self.assertEqual(self.manifest['artifact']['sha256'], SOURCE_SHA)
        self.assertEqual(self.manifest['artifact']['bytes'], len(self.bytes))
        source = self.manifest['source']
        self.assertEqual(source['commit'], '57ce4f643deec96b363d66778f220658df461497')
        self.assertEqual(source['path'], 'testdata/example/' + SIGNATURE + '.json')
        self.assertEqual(source['url'], source['repository'] + '/blob/' + source['commit'] + '/' + source['path'])
        self.assertEqual(source['license'], 'MIT')
        self.assertIn('solana-dex-parser-go contributors', (ROOT / 'LICENSE').read_text())

    def test_finality_and_acceptance_are_explicitly_unverified(self):
        self.assertEqual(self.manifest['finality_status'], 'CONFIRMED_SOURCE_NOT_FINALIZED')
        self.assertTrue(self.manifest['reference_only'])
        for field in ('finality_verified', 'authenticity_verified', 'eligible_for_trading'):
            self.assertIs(self.manifest[field], False)
        self.assertEqual(self.manifest['source']['documented_fetch_commitment'], 'confirmed')
        self.assertEqual(self.manifest['source']['fixture_acquisition_commitment'], 'unknown')
        self.assertFalse(self.manifest['source']['original_request_binding_available'])
        self.assertNotIn('commitment', self.raw)
        self.assertNotIn('request_evidence_hash', self.manifest)
        self.assertNotIn('history_queries', self.manifest)

    def test_identity_complete_saved_metadata_and_original_stack_heights(self):
        self.assertEqual(self.raw['transaction']['signatures'][0], SIGNATURE)
        self.assertEqual(len(unbase58(SIGNATURE)), 64)
        self.assertEqual(self.raw['slot'], 282653703)
        self.assertEqual(self.raw['blockTime'], 1723261303)
        self.assertEqual(self.raw['version'], 0)
        self.assertIsNone(self.raw['meta']['err'])
        self.assertEqual([g['index'] for g in self.raw['meta']['innerInstructions']], [3, 4, 5])
        self.assertEqual([len(g['instructions']) for g in self.raw['meta']['innerInstructions']], [15, 4, 4])
        self.assertEqual(self.raw['meta']['innerInstructions'][0]['instructions'][1]['stackHeight'], 2)
        self.assertEqual(self.raw['meta']['innerInstructions'][0]['instructions'][7]['stackHeight'], 3)
        self.assertEqual(len(self.raw['meta']['preBalances']), len(self.keys))
        self.assertEqual(len(self.raw['meta']['postBalances']), len(self.keys))

    def test_independent_legacy_create_discriminator_account_positions_and_arguments(self):
        create = self.message['instructions'][3]
        data = unbase58(create['data'])
        self.assertEqual(self.keys[create['programIdIndex']], PUMP)
        self.assertEqual(data[:8], bytes([24, 30, 200, 40, 5, 28, 7, 119]))
        self.assertNotEqual(data[:8], bytes([214, 144, 76, 236, 95, 139, 49, 180]))
        self.assertEqual(len(create['accounts']), 14)
        accounts = [self.keys[i] for i in create['accounts']]
        self.assertEqual(accounts[0], MINT)
        self.assertEqual(accounts[1], 'TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM')
        self.assertEqual(accounts[9], TOKEN_PROGRAM)
        self.assertEqual(accounts[10], 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL')
        self.assertEqual(accounts[11], 'SysvarRent111111111111111111111111111111111')
        self.assertEqual(accounts[12], 'Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1')
        self.assertEqual(accounts[13], PUMP)
        pos = 8
        strings = []
        for _ in range(3):
            size = struct.unpack_from('<I', data, pos)[0]
            pos += 4
            strings.append(data[pos:pos + size].decode())
            pos += size
        self.assertEqual(strings[:2], ['joke', 'joke'])
        self.assertTrue(strings[2].startswith('https://cf-ipfs.com/ipfs/'))
        self.assertEqual(pos, len(data))  # Old create carries three strings only.
        result = instruction(self.resolved_instruction(create))
        self.assertEqual(result['status'], 'IDENTIFIED')
        self.assertEqual(result['name'], 'create')
        self.assertEqual(result['accounts']['token_program'], TOKEN_PROGRAM)
        self.assertEqual(result['mint'], MINT)

    def test_saved_tokenkeg_execution_correlates_raw_init_mint_revoke_and_balances(self):
        group = self.raw['meta']['innerInstructions'][0]
        init = self.resolved_instruction(group['instructions'][1])
        mint_to = self.resolved_instruction(group['instructions'][12])
        revoke = self.resolved_instruction(group['instructions'][13])
        for ix in (init, mint_to, revoke):
            self.assertEqual(ix['programId'], TOKEN_PROGRAM)
            self.assertEqual(ix['accounts'][0], MINT)
        init_bytes = unbase58(init['data'])
        self.assertEqual(init_bytes[:2], bytes([20, 6]))  # InitializeMint2, decimals.
        self.assertEqual(init_bytes[2:34], unbase58('TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM'))
        self.assertEqual(init_bytes[34:], b'\x00')  # No freeze authority.
        mint_bytes = unbase58(mint_to['data'])
        self.assertEqual(mint_bytes[0], 7)
        self.assertEqual(int.from_bytes(mint_bytes[1:], 'little'), 1_000_000_000_000_000)
        self.assertEqual(unbase58(revoke['data']), bytes([6, 0, 0]))  # SetAuthority, MintTokens, None.
        balances = self.raw['meta']['postTokenBalances']
        self.assertEqual({r['mint'] for r in balances}, {MINT})
        self.assertEqual({r['programId'] for r in balances}, {TOKEN_PROGRAM})
        self.assertEqual(sum(int(r['uiTokenAmount']['amount']) for r in balances), 1_000_000_000_000_000)
        self.assertIn('Program ' + TOKEN_PROGRAM + ' success', self.raw['meta']['logMessages'])
        self.assertIn('Program ' + PUMP + ' success', self.raw['meta']['logMessages'])

    def test_production_compiled_resolution_keeps_downstream_evidence_unverified(self):
        from desk.account_history import account_inventory
        observation=decode(self.raw)
        self.assertEqual(observation['commitment'],'unverified')
        self.assertIn('UNDECODED_TOKEN_INSTRUCTION',observation['limitations'])
        self.assertTrue(any(p['status']=='EVENT_SCHEMA_MISMATCH' for p in observation['program_observations']))
        self.assertEqual(observation['mint_initializations'],[])
        inventory=account_inventory(MINT,[observation],{'address':MINT,'token_accounts_filter':'none'})
        self.assertFalse(inventory['initialization_inventory_verified'])
        self.assertFalse(inventory['eligible_for_trading'])
        self.assertIn('ACCOUNT_DISCOVERY_NOT_FINALIZED',inventory['reasons'])
        self.assertIn('ACCOUNT_INITIALIZATION_DECODING_INCOMPLETE',inventory['reasons'])
        self.assertIn('ACCOUNT_DISCOVERY_LAUNCH_ANCHOR_REQUIRED',inventory['reasons'])
        self.assertEqual(hashlib.sha256(self.bytes).hexdigest(), SOURCE_SHA)

    def test_original_create_event_remains_schema_mismatch(self):
        event = self.raw['meta']['innerInstructions'][0]['instructions'][14]
        view = self.resolved_instruction(event)
        raw = unbase58(view['data'])
        self.assertEqual(view['programId'], PUMP)
        self.assertEqual(raw[:8], bytes.fromhex('e445a52e51cb9a1d'))
        self.assertEqual(raw[8:16], bytes.fromhex('1b72a94ddeeb6376'))
        result = instruction(view)
        self.assertEqual(result['status'], 'EVENT_SCHEMA_MISMATCH')
        self.assertIsNot(result.get('schema_complete'), True)
        self.assertNotIn('fields', result)
