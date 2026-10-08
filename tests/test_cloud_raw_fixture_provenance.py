"""Public saved-record correspondence only; no syntax or lifecycle acceptance."""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
RAW_DIGEST = '2b49cd4c6fe24a121cb59894c8a6fe0e82e4cf51db28e88151612984ccc9a0c7'

def load():
    raw = json.loads((ROOT/'fixtures/mainnet-launch-raw.json').read_bytes())
    notification = json.loads((ROOT/'fixtures/mainnet-launch.json').read_bytes())['payload']['params']['result']
    provenance = json.loads((ROOT/'fixtures/mainnet-launch-raw.provenance.json').read_bytes())
    return raw, notification, provenance

def inventory(record):
    rows = [(str(i), row) for i, row in enumerate(record['transaction']['message']['instructions'])]
    for group in record['meta']['innerInstructions']:
        rows.extend((f"{group['index']}.{i}", row) for i, row in enumerate(group['instructions']))
    return rows

def check_correspondence(raw, notification, provenance):
    record = raw['result']; parsed = notification['transaction']
    canonical = json.dumps(raw, sort_keys=True, separators=(',', ':')).encode()
    assert hashlib.sha256(canonical).hexdigest() == RAW_DIGEST == provenance['record_sha256']
    assert record['slot'] == notification['slot'] == provenance['slot']
    assert record['transaction']['signatures'] == parsed['transaction']['signatures']
    assert record['transaction']['signatures'][0] == notification['signature'] == provenance['signature'] == raw['params'][0]
    message = record['transaction']['message']; other = parsed['transaction']['message']
    loaded = record['meta']['loadedAddresses']
    keys = message['accountKeys'] + loaded['writable'] + loaded['readonly']
    assert keys == [key['pubkey'] for key in other['accountKeys']]
    assert message['addressTableLookups'] == other['addressTableLookups']
    assert message['recentBlockhash'] == other['recentBlockhash']
    assert keys[message['instructions'][2]['accounts'][0]] == provenance['mint'] == other['instructions'][2]['accounts'][0]
    left, right = inventory(record), inventory(parsed)
    assert [p for p, _ in left] == [p for p, _ in right]
    for (path, compiled), (_, expanded) in zip(left, right):
        assert keys[compiled['programIdIndex']] == expanded['programId'], path
        assert compiled['stackHeight'] == expanded['stackHeight'], path
        assert isinstance(compiled['data'], str) and 'parsed' not in compiled, path
        assert all(type(i) is int and 0 <= i < len(keys) for i in compiled['accounts']), path
        if 'data' in expanded:
            assert compiled['data'] == expanded['data'], path
            assert [keys[i] for i in compiled['accounts']] == expanded['accounts'], path
    return left, right

class RawFixtureProvenanceTests(unittest.TestCase):
    def test_exact_canonical_digest_and_identity_inventory(self):
        self.assertEqual(hashlib.sha256((ROOT/'fixtures/mainnet-launch.json').read_bytes()).hexdigest(),
                         'd80cf9876fdb3e9b465e4103fa14bf2baaf09483dad9a27a57ff7339624b3bf2')
        self.assertEqual(hashlib.sha256((ROOT/'fixtures/mainnet-launch-raw.json').read_bytes()).hexdigest(),
                         '27d481575656d2b254a63bf699a32ce3dd599ce3b55fc8b04da2aa79dd356520')
        raw, notification, provenance = load()
        left, right = check_correspondence(raw, notification, provenance)
        self.assertEqual(len(left), 33)
        self.assertEqual(sum('parsed' in row for _, row in right), 26)
        self.assertEqual(sum('data' in row for _, row in right), 7)
        self.assertEqual([len(g['instructions']) for g in raw['result']['meta']['innerInstructions']], [15, 4, 9])

    def test_loaded_key_segments_and_declared_header_flags(self):
        raw, notification, _ = load()
        record = raw['result']; message = record['transaction']['message']
        keys = notification['transaction']['transaction']['message']['accountKeys']
        self.assertEqual((len(message['accountKeys']),len(record['meta']['loadedAddresses']['writable']),len(record['meta']['loadedAddresses']['readonly'])), (18,5,11))
        for i, key in enumerate(keys):
            self.assertEqual(key['source'], 'transaction' if i < 18 else 'lookupTable')
            self.assertEqual(key['signer'], i < 2)
            self.assertEqual(key['writable'], i < 14 or 18 <= i < 23)
        self.assertEqual(message['header'], {'numRequiredSignatures':2,'numReadonlySignedAccounts':0,'numReadonlyUnsignedAccounts':4})

    def test_representation_differences_are_explicit(self):
        raw, notification, _ = load(); r = raw['result']; p = notification['transaction']
        self.assertNotIn('header',p['transaction']['message'])
        self.assertNotIn('loadedAddresses',p['meta'])
        self.assertEqual(r['meta']['rewards'], [])
        self.assertIsNone(p['meta']['rewards'])
        self.assertEqual({k for k in r['meta'].keys() & p['meta'].keys() if r['meta'][k] != p['meta'][k]}, {'innerInstructions','rewards'})
        self.assertEqual(r['transactionIndex'],notification['transactionIndex'])

    def test_correspondence_rejects_identity_key_inventory_and_byte_mutations(self):
        for mutate in (
            lambda n: n.update(slot=n['slot']+1),
            lambda n: n.update(signature='foreign'),
            lambda n: n['transaction']['transaction']['message']['accountKeys'].reverse(),
            lambda n: n['transaction']['meta']['innerInstructions'][0]['instructions'].pop(),
            lambda n: n['transaction']['meta']['innerInstructions'][0]['instructions'][0].update(programId='foreign'),
            lambda n: n['transaction']['transaction']['message']['instructions'][2].update(data='foreign'),
        ):
            raw, notification, provenance = load(); mutate(notification)
            with self.assertRaises(AssertionError): check_correspondence(raw, notification, provenance)
        raw, notification, provenance = load();raw['result']['slot']+=1
        with self.assertRaises(AssertionError):check_correspondence(raw, notification, provenance)

    def test_request_provenance_is_only_a_saved_declaration(self):
        raw, _, provenance = load()
        self.assertEqual(raw['method'],'getTransaction')
        self.assertEqual(raw['params'][1],{'commitment':'finalized','encoding':'json','maxSupportedTransactionVersion':0})
        self.assertEqual(provenance['provider_calls'],1)
        self.assertEqual(provenance['replay_provider_calls'],0)
        self.assertIn('no independent finality',provenance['scope'])
        self.assertIn('Endpoint state not captured',provenance['scope'])
