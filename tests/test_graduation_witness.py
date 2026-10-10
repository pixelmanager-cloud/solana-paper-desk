"""SYNTHETIC_TEST_ONLY pinned migration encodings; no provider authentication."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from solders.pubkey import Pubkey
from desk import graduation_witness as g
from desk.programs import instruction, unbase58
from desk.security import base58


def fixture(name='migrate'):
    mint = str(Pubkey.from_bytes(bytes([7])*32)); user = str(Pubkey.from_bytes(bytes([8])*32))
    authority = g._pda([b'pool-authority', bytes(Pubkey.from_string(mint))], g.PUMP)
    curve = g._pda([b'bonding-curve', bytes(Pubkey.from_string(mint))], g.PUMP)
    pool = g._pda([b'pool', b'\0\0', bytes(Pubkey.from_string(authority)),
                   bytes(Pubkey.from_string(mint)), bytes(Pubkey.from_string(g.SOL))], g.AMM)
    schema = json.loads((Path(__file__).resolve().parents[1]/'desk/schemas/pump.json').read_text())
    spec = next(x for x in schema['instructions'] if x['name']==name)
    bindings = {'mint':mint,'base_mint':mint,'quote_mint':g.SOL,'wsol_mint':g.SOL,
                'bonding_curve':curve,'pool':pool,'pool_authority':authority,'user':user,
                'program':g.PUMP}
    accounts = [a.get('address') or bindings.get(a['name'],user) for a in spec['accounts']]
    event = (bytes.fromhex('e445a52e51cb9a1dbde95db95c94ea94') + unbase58(user)+unbase58(mint)
             + (1).to_bytes(8,'little')*3+unbase58(curve)+(1000).to_bytes(8,'little',signed=True)
             + unbase58(pool)+unbase58(g.SOL))
    raw = {'slot':10,'blockTime':1000,'transaction':{'signatures':['synthetic-migration'],
        'message':{'accountKeys':[{'pubkey':user}], 'instructions':[{'programId':g.PUMP,
        'accounts':accounts,'data':base58(bytes(spec['discriminator']))}]}},
        'meta':{'err':None,'preTokenBalances':[],'postTokenBalances':[],
        'innerInstructions':[{'index':0,'instructions':[{'programId':g.PUMP,
        'accounts':[g._pda([b'__event_authority'],g.PUMP)],'data':base58(event),'stackHeight':2}]}]}}
    return raw,mint,pool


class GraduationWitnessTests(unittest.TestCase):
    def extract(self, raw, mint, pool, **kwargs):
        return g.extract_graduation(raw,mint=mint,pool=pool,now=kwargs.get('now',2000),
                                   provenance='SYNTHETIC_TEST_ONLY')

    def sentinel_fixture(self, name='migrate_v2'):
        # Synthetic reconstruction of the supplied excerpt's quote distinction;
        # synthetic identities/time, not the full retained mainnet transaction.
        raw,mint,pool=fixture(name)
        ix=raw['meta']['innerInstructions'][0]['instructions'][0]
        ix['data']=base58(unbase58(ix['data'])[:-32]+bytes(32))
        return raw,mint,pool

    def test_migrate_v2_native_sentinel_preserves_raw_quote_and_effective_pool_quote(self):
        raw,mint,pool=self.sentinel_fixture();original=copy.deepcopy(raw)
        with patch('socket.socket',side_effect=AssertionError('network forbidden')):
            result=self.extract([raw],mint,pool)
        self.assertEqual(result['status'],'OBSERVED_MIGRATION')
        self.assertEqual(result['graduated_at'],1000)
        self.assertEqual(result['witnesses'][0]['event_quote_mint'],g.NATIVE_SOL_SENTINEL)
        self.assertEqual(result['witnesses'][0]['quote_mint'],g.SOL)
        self.assertEqual(result['witnesses'][0]['payload_hash'],g.digest(original))
        self.assertEqual(result['source_hashes'],[g.digest(original)])
        self.assertEqual(result['provenance'],'SYNTHETIC_TEST_ONLY')
        self.assertFalse(result['entry_authorized']);self.assertEqual(raw,original)

    def test_supplied_public_event_original_hash_layout_and_wsol_pool_binding(self):
        excerpt=json.loads((Path(__file__).resolve().parents[1]/'fixtures/migration_quote_sentinel_excerpt.json').read_text())
        event=excerpt['event'];original=copy.deepcopy(event)
        self.assertEqual(g.digest(event),'0fdaf2a531590ae451229d99345d546713f06ec7f1d043b9534e0bf89697ba46')
        self.assertEqual(len(unbase58(event['data'])),208)
        decoded=instruction(event);f=decoded['fields']
        self.assertEqual(decoded['status'],'EVENT_DECODED');self.assertTrue(decoded['schema_complete'])
        self.assertEqual(decoded['schema_file'],'pump.json')
        self.assertEqual(f['quote_mint'],g.NATIVE_SOL_SENTINEL)
        self.assertEqual(f['timestamp'],excerpt['blockTime'])
        self.assertEqual(f['bonding_curve'],g._pda([b'bonding-curve',unbase58(f['mint'])],g.PUMP))
        authority=g._pda([b'pool-authority',unbase58(f['mint'])],g.PUMP)
        self.assertEqual(f['pool'],g._pda([b'pool',b'\0\0',unbase58(authority),unbase58(f['mint']),unbase58(g.SOL)],g.AMM))
        self.assertEqual(event,original)

    def test_legacy_sentinel_preserves_quote_and_arbitrary_quotes_still_reject(self):
        raw,mint,pool=self.sentinel_fixture('migrate')
        original=copy.deepcopy(raw);result=self.extract([raw],mint,pool)
        self.assertEqual(result['status'],'OBSERVED_MIGRATION')
        self.assertEqual(result['witnesses'][0]['event_quote_mint'],g.NATIVE_SOL_SENTINEL)
        self.assertEqual(result['witnesses'][0]['quote_mint'],g.SOL)
        self.assertFalse(result['entry_authorized']);self.assertEqual(raw,original)
        for quote in (g.NATIVE_SOL_SENTINEL,mint):
            raw,mint,pool=self.sentinel_fixture();outer=raw['transaction']['message']['instructions'][0]
            spec=json.loads((Path(__file__).resolve().parents[1]/'desk/schemas/pump.json').read_text())['instructions']
            spec=next(x for x in spec if x['name']=='migrate_v2')
            outer['accounts'][next(i for i,a in enumerate(spec['accounts']) if a['name']=='quote_mint')]=quote
            self.assertIsNone(self.extract([raw],mint,pool)['graduated_at'])
        raw,mint,pool=self.sentinel_fixture();ix=raw['meta']['innerInstructions'][0]['instructions'][0]
        ix['data']=base58(unbase58(ix['data'])[:-32]+unbase58(mint))
        self.assertIsNone(self.extract([raw],mint,pool)['graduated_at'])

    def test_sentinel_requires_success_complete_direct_event_and_every_binding(self):
        changes=('failed','nested','noheight','parent','authority','outer_args','outer_program',
                 'outer_mint','outer_curve','outer_pool','outer_authority','outer_user',
                 'event_mint','event_curve','event_pool','event_user','time','suffix','truncated','noncanonical_pool')
        spec=next(x for x in json.loads((Path(__file__).resolve().parents[1]/'desk/schemas/pump.json').read_text())['instructions'] if x['name']=='migrate_v2')
        for change in changes:
            with self.subTest(change=change):
                raw,mint,pool=self.sentinel_fixture();ix=raw['meta']['innerInstructions'][0]['instructions'][0]
                outer=raw['transaction']['message']['instructions'][0];data=unbase58(ix['data'])
                if change=='failed':raw['meta']['err']='fixture failure'
                elif change=='nested':ix['stackHeight']=3
                elif change=='noheight':ix.pop('stackHeight')
                elif change=='parent':raw['meta']['innerInstructions'][0]['index']=1
                elif change=='authority':ix['accounts']=[mint]
                elif change=='outer_args':outer['data']=base58(unbase58(outer['data'])+b'\0')
                elif change=='outer_program':outer['programId']=g.AMM
                elif change.startswith('outer_') or change=='noncanonical_pool':
                    field={'mint':'base_mint','curve':'bonding_curve','authority':'pool_authority'}.get(change[6:],change[6:]) if change!='noncanonical_pool' else 'pool'
                    outer['accounts'][next(i for i,a in enumerate(spec['accounts']) if a['name']==field)]=g.SOL
                    if change=='noncanonical_pool':
                        pool=g.SOL;ix['data']=base58(data[:144]+unbase58(pool)+data[176:])
                elif change.startswith('event_'):
                    offset={'user':16,'mint':48,'curve':104,'pool':144}[change[6:]]
                    ix['data']=base58(data[:offset]+unbase58(g.SOL)+data[offset+32:])
                elif change=='time':raw['blockTime']=999
                elif change=='suffix':ix['data']=base58(data+b'\0')
                elif change=='truncated':ix['data']=base58(data[:-1])
                original=copy.deepcopy(raw);result=self.extract([raw],mint,pool)
                self.assertEqual(result['status'],'UNKNOWN');self.assertIsNone(result['graduated_at'])
                self.assertFalse(result['entry_authorized']);self.assertEqual(raw,original)

    def test_both_pinned_migrations_preserve_original_historical_time_and_source(self):
        for name in ('migrate','migrate_v2'):
            with self.subTest(name=name):
                raw,mint,pool=fixture(name); original=copy.deepcopy(raw)
                with patch('socket.socket',side_effect=AssertionError('network forbidden')):
                    result=self.extract([raw],mint,pool)
                self.assertEqual(result['graduated_at'],1000)
                self.assertEqual(result['status'],'OBSERVED_MIGRATION')
                self.assertEqual(result['witnesses'][0]['payload_hash'],g.digest(raw))
                self.assertEqual(raw,original);self.assertFalse(result['entry_authorized'])
                self.assertEqual(self.extract([raw,copy.deepcopy(raw)],mint,pool)['graduated_at'],1000)

    def test_failed_missing_future_and_wrong_block_times_never_guess(self):
        raw,mint,pool=fixture()
        for change in ('failed','missing','future','different'):
            with self.subTest(change=change):
                r=copy.deepcopy(raw)
                if change=='failed':r['meta']['err']={'InstructionError':[0,'custom']}
                else:r['blockTime']={'missing':None,'future':2001,'different':999}[change]
                self.assertIsNone(self.extract([r],mint,pool)['graduated_at'])
        self.assertEqual(self.extract([],mint,pool)['blockers'],['MIGRATION_WITNESS_ABSENT'])

    def test_complete_event_is_not_migration_and_unknown_suffix_is_not_complete(self):
        raw,mint,pool=fixture();ix=raw['meta']['innerInstructions'][0]['instructions'][0]
        original=unbase58(ix['data'])
        for data in (original+b'\0', original[:8]+bytes.fromhex('5f72619cd42e9808')+original[16:],original[:30]):
            ix['data']=base58(data)
            self.assertIsNone(self.extract([raw],mint,pool)['graduated_at'])

    def test_event_scope_authority_and_instruction_bindings(self):
        for change in ('nested','noheight','parent','program','curve','pool','mint','quote','user','authority','args','pool_authority'):
            with self.subTest(change=change):
                raw,mint,pool=fixture('migrate_v2');ix=raw['meta']['innerInstructions'][0]['instructions'][0]
                outer=raw['transaction']['message']['instructions'][0]
                if change=='nested':ix['stackHeight']=3
                elif change=='noheight':ix.pop('stackHeight')
                elif change=='parent':raw['meta']['innerInstructions'][0]['index']=1
                elif change=='program':outer['programId']=g.AMM
                elif change=='authority':ix['accounts']=[mint]
                elif change=='args':outer['data']=base58(unbase58(outer['data'])+b'\0')
                else:
                    spec=next(x for x in json.loads((Path(__file__).resolve().parents[1]/'desk/schemas/pump.json').read_text())['instructions'] if x['name']=='migrate_v2')
                    field={'curve':'bonding_curve','mint':'base_mint','quote':'quote_mint'}.get(change,change)
                    idx=next(i for i,a in enumerate(spec['accounts']) if a['name']==field)
                    outer['accounts'][idx]=mint if change=='quote' else g.SOL
                self.assertIsNone(self.extract([raw],mint,pool)['graduated_at'])

    def test_conflicting_identity_and_failed_metadata_are_not_waived(self):
        raw,mint,pool=fixture();other=copy.deepcopy(raw);other['blockTime']=999
        self.assertIn('CONFLICTING_TRANSACTION_IDENTITY',self.extract([raw,other],mint,pool)['blockers'])
        for slot,at,blocker in ((10,999,'CONFLICTING_SLOT_TIME'),(11,999,'REGRESSING_SLOT_TIME')):
            other=copy.deepcopy(raw);other['transaction']['signatures']=['failed-other']
            other.update(slot=slot,blockTime=at);other['meta']['err']='failed'
            result=self.extract([raw,other],mint,pool)
            self.assertIn(blocker,result['blockers']);self.assertIsNone(result['graduated_at'])

    def test_wrong_mint_sibling_under_target_parent_blocks_both_migrations(self):
        for name in ('migrate','migrate_v2'):
            for wrong_first in (False,True):
                with self.subTest(name=name,wrong_first=wrong_first):
                    raw,mint,pool=fixture(name)
                    group=raw['meta']['innerInstructions'][0]['instructions']
                    wrong=copy.deepcopy(group[0]);data=unbase58(wrong['data'])
                    wrong['data']=base58(data[:48]+unbase58(g.SOL)+data[80:])
                    group.insert(0 if wrong_first else 1,wrong)
                    original=copy.deepcopy(raw)
                    result=self.extract([raw],mint,pool)
                    self.assertEqual(result['status'],'UNKNOWN')
                    self.assertIsNone(result['graduated_at'])
                    self.assertIn('MIGRATION_ACCOUNT_BINDING_MISMATCH',result['blockers'])
                    self.assertEqual(result['source_hashes'],[g.digest(raw)])
                    self.assertFalse(result['entry_authorized']);self.assertEqual(raw,original)

    def test_independently_bound_other_mint_migrations_remain_irrelevant(self):
        for name in ('migrate','migrate_v2'):
            with self.subTest(name=name):
                raw,mint,pool=fixture(name);other=copy.deepcopy(raw)
                other_mint=str(Pubkey.from_bytes(bytes([10])*32))
                curve=g._pda([b'bonding-curve',unbase58(other_mint)],g.PUMP)
                authority=g._pda([b'pool-authority',unbase58(other_mint)],g.PUMP)
                other_pool=g._pda([b'pool',b'\0\0',unbase58(authority),unbase58(other_mint),unbase58(g.SOL)],g.AMM)
                spec=next(x for x in json.loads((Path(__file__).resolve().parents[1]/'desk/schemas/pump.json').read_text())['instructions'] if x['name']==name)
                outer=other['transaction']['message']['instructions'][0]
                changes={'mint':other_mint,'base_mint':other_mint,'bonding_curve':curve,'pool_authority':authority,'pool':other_pool}
                for i,a in enumerate(spec['accounts']):
                    if a['name'] in changes:outer['accounts'][i]=changes[a['name']]
                ix=other['meta']['innerInstructions'][0]['instructions'][0];data=unbase58(ix['data'])
                ix['data']=base58(data[:48]+unbase58(other_mint)+data[80:104]+unbase58(curve)+data[136:144]+unbase58(other_pool)+data[176:])
                other['transaction']['signatures']=['other-mint-migration']
                self.assertEqual(self.extract([raw,other],mint,pool)['graduated_at'],1000)
                # Same transaction, different independently bound outer parent.
                combined=copy.deepcopy(raw)
                combined['transaction']['message']['instructions'].append(outer)
                group=copy.deepcopy(other['meta']['innerInstructions'][0]);group['index']=1
                combined['meta']['innerInstructions'].append(group)
                result=self.extract([combined],mint,pool)
                self.assertEqual(result['graduated_at'],1000);self.assertEqual(result['blockers'],[])

    def test_compiled_keys_and_notification_use_existing_decoder(self):
        raw,mint,pool=fixture(); message=raw['transaction']['message']
        keys=[message['accountKeys'][0]['pubkey']]
        instructions=message['instructions']+raw['meta']['innerInstructions'][0]['instructions']
        for ix in instructions:
            for key in [ix['programId']]+ix['accounts']:
                if key not in keys:keys.append(key)
        message['accountKeys']=keys
        message['header']={'numRequiredSignatures':1,'numReadonlySignedAccounts':0,'numReadonlyUnsignedAccounts':0}
        raw['version']='legacy'
        for ix in instructions:
            ix['programIdIndex']=keys.index(ix.pop('programId'))
            ix['accounts']=[keys.index(a) for a in ix['accounts']]
        self.assertEqual(self.extract([raw],mint,pool)['graduated_at'],1000)
        notification={'method':'transactionNotification','params':{'result':{
            'signature':'synthetic-migration','slot':10,'blockTime':1000,
            'transaction':{k:raw[k] for k in ('transaction','meta','version')}}}}
        self.assertEqual(self.extract([notification],mint,pool)['graduated_at'],1000)

    def test_bounded_inputs_and_explicit_provenance(self):
        raw,mint,pool=fixture()
        with self.assertRaises(ValueError):self.extract([raw]*257,mint,pool)
        with self.assertRaises(ValueError):self.extract([raw],mint,pool,now=True)
        large=copy.deepcopy(raw);large['unused']='x'*(128*1024)
        self.assertIn('RAW_TRANSACTION_BYTE_BOUND',self.extract([raw,large],mint,pool)['blockers'])
        with self.assertRaises(ValueError):g.extract_graduation([raw],mint=mint,pool=pool,now=2000,provenance='LIVE_AUTHENTICATED')


if __name__=='__main__':unittest.main()
