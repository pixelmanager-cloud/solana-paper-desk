import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from desk.decode import decode, decode_capture, SYSTEM, TOKENS


def transaction():
    return {'slot': 10, 'transaction': {'signatures': ['sig'], 'message': {
        'accountKeys': [{'pubkey': 'account'}], 'instructions': []}},
        'meta': {'err': None, 'preTokenBalances': [{'accountIndex': 0, 'mint': 'mint',
          'owner': 'owner', 'uiTokenAmount': {'amount': '9007199254740993', 'decimals': 6}}],
         'postTokenBalances': [{'accountIndex': 0, 'mint': 'mint', 'owner': 'owner',
          'uiTokenAmount': {'amount': '9007199254740994', 'decimals': 6}}],
         'innerInstructions': [{'index': 0, 'instructions': [{'programId': SYSTEM,
           'parsed': {'type': 'transfer', 'info': {'source': 'a', 'destination': 'b', 'lamports': 123}}}]}]}}


class DecoderTests(unittest.TestCase):
    def test_exact_integer_and_inner_transfer(self):
        r = decode(transaction())
        self.assertEqual(r['token_deltas'][0]['delta_raw'], '1')
        self.assertEqual(r['transfers'][0]['source_kind'], 'unknown')
        self.assertEqual(r['transfers'][0]['instruction'], '0.0')

    def test_failed_has_no_funding_edges(self):
        t = transaction(); t['meta']['err'] = {'InstructionError': [0, 'Custom']}
        self.assertEqual(decode(t)['transfers'], [])

    def test_missing_balance_is_not_zero(self):
        t = transaction(); t['meta']['preTokenBalances'] = []
        self.assertIsNone(decode(t)['token_deltas'][0]['delta_raw'])

    def test_owner_change_not_attributed_to_new_owner(self):
        t = transaction(); t['meta']['postTokenBalances'][0]['owner'] = 'new'
        self.assertTrue(all(x['delta_raw'] is None for x in decode(t)['token_deltas']))

    def test_spoofed_transfer_program_ignored(self):
        t = transaction(); t['meta']['innerInstructions'][0]['instructions'][0]['programId'] = 'fake'
        self.assertEqual(decode(t)['transfers'], [])

    def test_bad_index_quarantinable(self):
        t = transaction(); t['meta']['preTokenBalances'][0]['accountIndex'] = 2
        with self.assertRaises(ValueError): decode(t)

    def test_notification_wrapper(self):
        t = transaction(); slot = t.pop('slot')
        r = decode({'method': 'transactionNotification', 'params': {'result': {
            'signature': 'sig', 'slot': slot, 'transaction': t}}})
        self.assertEqual(r['commitment'], 'confirmed')

    def test_incomplete_metadata_rejected(self):
        t = transaction(); del t['meta']['err']
        with self.assertRaises(ValueError): decode(t)

    def test_readonly_export_quarantines_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder)/'raw.sqlite'; out = Path(folder)/'decoded.jsonl'
            c = sqlite3.connect(db)
            c.execute('CREATE TABLE raw_events(seq INTEGER,source_id TEXT,received_at INTEGER,slot INTEGER,payload TEXT)')
            for i, t in enumerate([transaction(), transaction(), {}]):
                c.execute('INSERT INTO raw_events VALUES(?,?,?,?,?)', (i, str(i), 100, 10, json.dumps(t)))
            c.commit(); c.close()
            before = db.read_bytes(); r = decode_capture(db, out)
            self.assertEqual(before, db.read_bytes())
            self.assertEqual(r['counts']['duplicates'], 1)
            self.assertEqual(r['counts']['quarantined'], 1)
            self.assertFalse(r['eligible_for_trading'])
            with self.assertRaises(FileExistsError): decode_capture(db, out)


class MainnetRegressionTests(unittest.TestCase):
    def test_captured_launch_and_cpi_event_decode_together(self):
        from desk.decode import decode
        from pathlib import Path
        import json
        fixture=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-launch.json').read_text())
        result=decode(fixture['payload'])
        self.assertEqual(result['signature'],fixture['expected_signature'])
        observations=result['program_observations']
        self.assertEqual(sum(x.get('kind')=='LAUNCH' for x in observations),fixture['expected_launch_count'])
        self.assertTrue(any(x.get('name')=='CreateEvent' or x.get('event')=='CreateEvent' for x in observations))
