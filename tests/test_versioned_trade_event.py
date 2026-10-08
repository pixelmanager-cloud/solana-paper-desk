"""Versioned official syntax must not expand instruction or economic trust."""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from desk.programs import schemas, event_schemas, instruction, unbase58
from desk.security import base58
from tests.test_create_v2_raw_support import raw_fixture, instruction_at, report, data

ROOT = Path(__file__).resolve().parents[1]


class VersionedTradeEventTests(unittest.TestCase):
    def resolved(self, raw):
        result = raw_fixture()
        keys = result['transaction']['message']['accountKeys']
        loaded = result['meta'].get('loadedAddresses', {})
        keys = keys + loaded.get('writable', []) + loaded.get('readonly', [])
        ix = instruction_at(result, '4.8')
        return {'programId': keys[ix['programIdIndex']],
                'accounts': [keys[i] for i in ix['accounts']], 'data': base58(raw)}

    def event_bytes(self):
        return unbase58(instruction_at(raw_fixture(), '4.8')['data'])

    def test_literal_source_digest_and_events_only_instruction_isolation(self):
        folder = ROOT / 'desk/schemas'
        manifest = json.loads((folder / 'manifest.json').read_text())
        record = manifest['files']['pump_events_8cda1fa']
        raw = (folder / 'pump_events_8cda1fa.json').read_bytes()
        self.assertTrue(record['events_only'])
        self.assertEqual(record['commit'], '8cda1fa30ea658b20909d8aedf002047119388d2')
        self.assertEqual(hashlib.sha256(raw).hexdigest(), record['sha256'])
        self.assertEqual(record['sha256'], '38b8abcc5b279bda85cf473e7c6f67bd15eb89df658cf93434687a43c88ad937')
        old = json.loads((folder / 'pump.json').read_text())
        new = json.loads(raw)
        expected = {bytes(i['discriminator']): i for i in old['instructions']}
        self.assertEqual(schemas()[old['address']], expected)
        added = [i for i in new['instructions'] if bytes(i['discriminator']) not in expected]
        self.assertTrue(added)
        for spec in added:
            ix = {'programId': old['address'], 'accounts': [], 'data': base58(bytes(spec['discriminator']))}
            self.assertEqual(instruction(ix)['status'], 'UNKNOWN_DISCRIMINATOR')

    def test_historical_exact_layouts_and_unknown_suffix_fail_closed(self):
        raw = self.event_bytes()
        for removed, schema in [(0, 'pump_events_8cda1fa.json'), (8, 'pump.json'),
                                (24, 'pump_previous1.json')]:
            value = instruction(self.resolved(raw[:len(raw)-removed]))
            self.assertEqual(value['status'], 'EVENT_DECODED')
            self.assertEqual(value['schema_file'], schema)
            self.assertIs(value['schema_complete'], True)
        for suffix in (b'\0', b'\xff'*8):
            value = instruction(self.resolved(raw+suffix))
            self.assertEqual(value['status'], 'EVENT_PREFIX_DECODED')
            self.assertIs(value['schema_complete'], False)
            self.assertEqual(value['unknown_trailing_bytes'], len(suffix))

    def test_pinned_digest_corruption_fails_before_instruction_and_event_loading(self):
        original = Path.read_bytes
        def corrupted(path):
            raw = original(path)
            return raw+b' ' if path.name == 'pump_events_8cda1fa.json' else raw
        try:
            with patch.object(Path, 'read_bytes', corrupted):
                for loader in (schemas, event_schemas):
                    schemas.cache_clear()
                    event_schemas.cache_clear()
                    with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                        loader()
        finally:
            schemas.cache_clear()
            event_schemas.cache_clear()

    def test_complete_syntax_and_changed_declared_fee_never_resolve_effects(self):
        for tail in (b'\0'*8, b'\xff'*8):
            record = raw_fixture()
            ix = instruction_at(record, '4.8')
            data(ix, self.event_bytes()[:-8]+tail)
            out = report(record)
            self.assertEqual(out['errors'], [])
            self.assertEqual(len(out['inventory']), 33)
            self.assertEqual(out['inventory_summary']['total_observed'], 33)
            self.assertEqual(out['endpoint_states'], [])
            self.assertEqual(out['partial_event_witnesses'], [])
            event = next(w for w in out['unresolved_effect_witnesses']
                         if w['instruction']['instruction_path'] == '4.8')
            self.assertIs(event['schema_witness']['schema_complete'], True)
            self.assertFalse(event['structural_role_complete'])
            self.assertIn('TRADE_EVENT_ECONOMIC_EFFECTS_UNVERIFIED:4.8', out['unknowns'])
            for key in ('observed_inventory_agreement', 'supported_sequence_agreement'):
                self.assertIs(out[key], False)
            # Existing common assertion checks every lifecycle/trust/eligibility flag.
            from tests.test_create_v2_raw_support import CreateV2RawSupportTests
            CreateV2RawSupportTests().unapproved(out)

    def test_registration_exposes_only_trade_event_with_shared_types_intact(self):
        folder = ROOT / 'desk/schemas'
        old = json.loads((folder / 'pump.json').read_text())
        new = json.loads((folder / 'pump_events_8cda1fa.json').read_text())
        registered, types, _ = next(row for row in event_schemas()[new['address']]
                                    if row[2] == 'pump_events_8cda1fa.json')
        self.assertEqual(set(registered.values()), {'TradeEvent'})
        self.assertIn('Shareholder', types)
        old_names = {event['name'] for event in old['events']}
        added = [event for event in new['events'] if event['name'] not in old_names]
        self.assertEqual(len(added), 4)
        for event in added:
            raw = bytes.fromhex('e445a52e51cb9a1d') + bytes(event['discriminator'])
            self.assertEqual(instruction(self.resolved(raw))['status'], 'EVENT_SCHEMA_MISMATCH')

    def test_allowlist_malformed_missing_unknown_or_ambiguous_fails_closed(self):
        folder = ROOT / 'desk/schemas'
        baseline = json.loads((folder / 'manifest.json').read_text())
        original = Path.read_text
        import copy
        cases = [None, [], 'TradeEvent', [''], [123], ['TradeEvent', 'TradeEvent'],
                 ['DoesNotExist'], {'TradeEvent': True}]
        for allowed in cases + ['MISSING']:
            modified = copy.deepcopy(baseline)
            record = modified['files']['pump_events_8cda1fa']
            if allowed == 'MISSING':
                record.pop('event_allowlist')
            else:
                record['event_allowlist'] = allowed
            def manifest(path, *args, **kwargs):
                return json.dumps(modified) if path == folder / 'manifest.json' else original(path, *args, **kwargs)
            try:
                with patch.object(Path, 'read_text', manifest):
                    schemas.cache_clear()
                    event_schemas.cache_clear()
                    with self.subTest(allowed=allowed), self.assertRaisesRegex(ValueError, 'allowlist'):
                        event_schemas()
            finally:
                schemas.cache_clear()
                event_schemas.cache_clear()

    def test_appended_create_depth_cannot_verify_production_launch_anchor(self):
        from desk.decode import decode
        from desk.launch import launch_anchor
        payload = json.loads((ROOT / 'fixtures/mainnet-launch.json').read_text())['payload']
        baseline = decode(payload)
        create = next(event for event in baseline['program_observations'] if event.get('name') == 'CreateEvent')
        mint = create['fields']['mint']
        baseline['commitment'] = 'finalized_provider_response'
        baseline['block_time'] = create['fields']['timestamp']
        self.assertTrue(launch_anchor(baseline, mint)['verified'])
        transaction = payload['params']['result']['transaction']
        candidates = [ix for group in transaction['meta']['innerInstructions'] for ix in group['instructions']
                      if ix.get('data') and unbase58(ix['data'])[:16] == bytes.fromhex('e445a52e51cb9a1d1b72a94ddeeb6376')]
        self.assertEqual(len(candidates), 1)
        ix = candidates[0]
        original = unbase58(ix['data'])
        for depth in (0, 1, 255):
            ix['data'] = base58(original + bytes([depth]))
            observed = decode(payload)
            observed['commitment'] = 'finalized_provider_response'
            observed['block_time'] = create['fields']['timestamp']
            event = next(event for event in observed['program_observations'] if event.get('name') == 'CreateEvent')
            self.assertEqual(event['status'], 'EVENT_PREFIX_DECODED')
            self.assertIs(event['schema_complete'], False)
            self.assertEqual(event['unknown_trailing_bytes'], 1)
            self.assertNotIn('depth', event['fields'])
            self.assertFalse(launch_anchor(observed, mint)['verified'])
