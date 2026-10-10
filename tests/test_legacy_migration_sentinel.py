"""Synthetic legacy migration mutations; real pinned decoder, no providers."""
import copy
import json
from pathlib import Path
import unittest
from desk import graduation_witness as g
from desk.programs import unbase58, instruction
from desk.model import digest
from desk.security import base58
from tests.test_graduation_witness import fixture

class LegacyMigrationSentinelTests(unittest.TestCase):
    def test_unchanged_retained_event_excerpt_matches_pinned_schema(self):
        x=json.loads((Path(__file__).resolve().parents[1]/'fixtures/legacy_migration_sentinel_excerpt.json').read_text())
        self.assertEqual(digest(x['event']),'4bd73ced897b6b0132043a553acbcde15078ee168e1ca8b544e3bb6ef8bac34d')
        event=instruction(x['event']);self.assertEqual(event['status'],'EVENT_DECODED')
        self.assertTrue(event['schema_complete']);self.assertEqual(event['schema_file'],'pump.json')
        f=event['fields'];self.assertEqual(f['quote_mint'],g.NATIVE_SOL_SENTINEL)
        self.assertEqual(f['timestamp'],x['blockTime'])
        authority=g._pda([b'pool-authority',unbase58(f['mint'])],g.PUMP)
        self.assertEqual(f['bonding_curve'],g._pda([b'bonding-curve',unbase58(f['mint'])],g.PUMP))
        self.assertEqual(f['pool'],g._pda([b'pool',b'\0\0',unbase58(authority),unbase58(f['mint']),unbase58(g.SOL)],g.AMM))
        self.assertEqual(x['outer_wsol_mint'],g.SOL)
    def test_every_outer_binding_and_event_quote_remains_mandatory(self):
        schema=json.loads((Path(__file__).resolve().parents[1]/'desk/schemas/pump.json').read_text())
        spec=next(x for x in schema['instructions'] if x['name']=='migrate')
        changes=('wsol_mint','mint','bonding_curve','pool','pool_authority','user','program',
                 'event_quote','failed','nested','suffix','event_user','time')
        for change in changes:
            with self.subTest(change=change):
                raw,mint,pool=fixture('migrate');outer=raw['transaction']['message']['instructions'][0]
                event=raw['meta']['innerInstructions'][0]['instructions'][0]
                data=unbase58(event['data'])[:-32]+bytes(32);event['data']=base58(data)
                if change in {a['name'] for a in spec['accounts']}:
                    outer['accounts'][next(i for i,a in enumerate(spec['accounts']) if a['name']==change)]=g.NATIVE_SOL_SENTINEL
                elif change=='event_quote':event['data']=base58(data[:-32]+unbase58(mint))
                elif change=='failed':raw['meta']['err']={'InstructionError':[0,'Custom']}
                elif change=='nested':event['stackHeight']=3
                elif change=='suffix':event['data']=base58(data+b'\0')
                elif change=='event_user':event['data']=base58(data[:16]+bytes(32)+data[48:])
                elif change=='time':raw['blockTime']=999
                original=copy.deepcopy(raw)
                result=g.extract_graduation([raw],mint=mint,pool=pool,now=2000,provenance='SYNTHETIC_TEST_ONLY')
                self.assertEqual(result['status'],'UNKNOWN');self.assertIsNone(result['graduated_at'])
                self.assertFalse(result['entry_authorized']);self.assertEqual(raw,original)
