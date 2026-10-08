"""Original-parent persisted fixture validation, no provider or journal API."""
import base64
import copy
from dataclasses import FrozenInstanceError
from pathlib import Path
import unittest
from unittest.mock import patch

from desk.common_bank_view import validate_common_bank, CommonBankError
from desk.control_obligations import ReplayView, read_guard
from desk.evidence import EvidenceStore
from desk.model import digest
from desk.pool_capture_bridge import canonical_accounts
from desk.pool_vault_admission import GENESIS, NETWORK
from tests import test_pool_classification_projection as fixtures


@unittest.skipUnless(Path('/proc/self/mountinfo').is_file(),'Existing fixture ledger setup requires Linux')
class CommonBankViewTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.PoolClassificationProjectionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.store=self.f.h.store; self.mint=self.f.q['mint'];self.pool=canonical_accounts(self.mint)
        bank=self.store.load(self.f.head['snapshot_evidence']['snapshot_hash'])
        point=self.store.load(self.f.refs.snapshot)
        states=dict(zip(point['params'][0],point['result']['value']));states.update(zip(bank['params'][0],bank['result']['value']))
        # Original multi-holder fixture omits lamports; this new synthetic bank
        # explicitly provides them without rewriting any original saved record.
        for value in states.values():value.setdefault('lamports',2039280)
        self.frontier=sorted(bank['params'][0][1:]);self.keys=self.pool+sorted(set(self.frontier)-set(self.pool))
        self.bank={'kind':'rpc_response_v1','method':'getMultipleAccounts',
                   'params':[self.keys,{'encoding':'base64','commitment':'finalized','minContextSlot':20}],
                   'result':{'context':{'slot':20},'value':[copy.deepcopy(states[k]) for k in self.keys]}}
        coverage=next(q for q in self.f.head['history_queries'] if q['address']==self.mint)
        self.manifest={'kind':'common_bank_manifest_v1','mint':self.mint,'discovery_hash':self.store.save(coverage),
                       'genesis_response_hash':self.store.save({'kind':'rpc_response_v1','method':'getGenesisHash','params':[],'result':GENESIS}),
                       'slot_response_hash':self.store.save({'kind':'rpc_response_v1','method':'getSlot','params':[{'commitment':'finalized'}],'result':20}),
                       'keys':self.keys,'frontier':self.frontier,'ownership_indices':[self.keys.index(self.mint)]+[self.keys.index(a) for a in self.frontier],
                       'pool_indices':list(range(6))}
        self.clock={'kind':'rpc_response_v1','method':'getBlockTime','params':[20],'result':110}
        self.bind()
    def request(self,record,key):
        return {'kind':'common_bank_request_v1','network':NETWORK,'genesis_hash':GENESIS,
                'method':record['method'],'params':copy.deepcopy(record['params']),'response_hash':key}
    def bind(self):
        for role,record in [('bank',self.bank),('clock',self.clock)]:
            key=self.store.save(record);self.manifest[role+'_response_hash']=key
            self.manifest[role+'_request_hash']=self.store.save(self.request(record,key))
        self.key=self.store.save(self.manifest)
    def validate(self):
        with read_guard(self.store.path):
            return validate_common_bank(self.key,ReplayView(EvidenceStore(self.store.path,read_only=True)))
    def rejected(self):
        with self.assertRaises(CommonBankError):self.validate()
    def mutate_bytes(self,index,offset,data):
        value=self.bank['result']['value'][index];raw=bytearray(base64.b64decode(value['data'][0]));raw[offset:offset+len(data)]=data
        value['data'][0]=base64.b64encode(raw).decode();self.bind()

    def test_positive_original_parent_indices_raw_identity_no_writes_and_false_permissions(self):
        before={p.name:p.read_bytes() for p in self.f.root.iterdir() if p.is_file()}
        view=self.validate();out=view.diagnostic()
        self.assertEqual((view.slot,view.request_floor,view.block_time,view.discovery_cutoff),(20,20,110,20))
        self.assertEqual(view.parent_hash,self.manifest['bank_response_hash']);self.assertEqual(view.account(self.mint),self.bank['result']['value'][1])
        self.assertEqual(view.account(self.pool[4]),self.bank['result']['value'][4]);self.assertEqual(len(view.keys),8)
        self.assertEqual(out['decision'],'REJECT')
        self.assertTrue(all(out[k] is False for k in ('source_authenticated','finality_authenticated','frontier_at_bank_complete','history_complete','closed_lifetimes_verified','cpi_success_verified','historical_interval_exclusion_allowed','ownership_approved','eligible_for_trading')))
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.f.root.iterdir() if p.is_file()})
        self.assertEqual(self.store.load(view.parent_hash),self.bank)
        self.assertNotIn('method',out);self.assertNotIn('result',out)
    def test_account_copy_cannot_mutate_original_or_cached_view(self):
        view=self.validate();value=view.account(self.mint);value['owner']='forged';self.assertNotEqual(view.account(self.mint)['owner'],'forged')
        with self.assertRaises(FrozenInstanceError):view.slot=999
    def test_actual_later_bank_not_relabelled_as_floor_or_complete_frontier(self):
        self.bank['result']['context']['slot']=21;self.clock['params']=[21];self.clock['result']=111;self.bind()
        view=self.validate();self.assertEqual(view.slot,21);self.assertEqual(view.request_floor,20);self.assertFalse(view.diagnostic()['frontier_at_bank_complete'])
    def test_null_holder_retains_frontier_and_does_not_assert_closed_or_zero(self):
        index=self.manifest['ownership_indices'][2];key=self.keys[index];self.bank['result']['value'][index]=None;self.bind()
        view=self.validate();self.assertIn(key,view.frontier);self.assertIsNone(view.account(key));self.assertEqual(view.diagnostic()['holder_states'][key],'ABSENT_AT_BANK_UNVERIFIED_LIFETIME')
        self.assertFalse(view.diagnostic()['closed_lifetimes_verified'])
    def test_null_required_pool_account_rejected(self):
        for index in range(6):
            with self.subTest(index=index):
                old=self.bank['result']['value'][index];self.bank['result']['value'][index]=None;self.bind();self.rejected();self.bank['result']['value'][index]=old
    def test_forged_frontier_completeness_summary_is_not_input(self):
        self.manifest['frontier']=self.frontier[:-1];self.bind();self.rejected()
        self.manifest['frontier']=self.frontier;self.manifest['complete']=True;self.bind();self.rejected()
    def test_omitted_duplicated_and_bool_role_indices_reject(self):
        original=copy.deepcopy(self.manifest['ownership_indices'])
        for roles in (original[:-1],original+[original[-1]],[True]+original[1:]):
            self.manifest['ownership_indices']=roles;self.bind();self.rejected()
    def test_changed_order_extra_key_and_missing_raw_value_reject(self):
        original=copy.deepcopy(self.bank)
        self.bank['params'][0]=list(reversed(self.keys));self.bind();self.rejected();self.bank=copy.deepcopy(original)
        self.bank['params'][0]=self.keys+[self.mint];self.bind();self.rejected();self.bank=copy.deepcopy(original)
        self.bank['result']['value'].pop();self.bind();self.rejected()
    def test_floor_slot_clock_commitment_and_method_types_reject(self):
        original=copy.deepcopy(self.bank)
        for change in ('slot_bool','below_floor','confirmed','method','floor_bool'):
            self.bank=copy.deepcopy(original)
            if change=='slot_bool':self.bank['result']['context']['slot']=True
            if change=='below_floor':self.bank['result']['context']['slot']=19
            if change=='confirmed':self.bank['params'][1]['commitment']='confirmed'
            if change=='method':self.bank['method']='getAccountInfo'
            if change=='floor_bool':self.bank['params'][1]['minContextSlot']=True
            self.bind();self.rejected()
        self.bank=original;self.clock['params']=[21];self.bind();self.rejected()
    def test_genesis_and_source_request_manifest_not_invented(self):
        self.manifest['genesis_response_hash']=self.store.save({'kind':'rpc_response_v1','method':'getGenesisHash','params':[],'result':'foreign'});self.bind();self.rejected()
    def test_clock_bool_and_manifest_slot_bool_equality_reject(self):
        self.clock['result']=True;self.bind();self.rejected()
        self.clock['result']=110;self.bind()
        request=self.store.load(self.manifest['clock_request_hash']);request['params']=[True];self.manifest['clock_request_hash']=self.store.save(request);self.key=self.store.save(self.manifest);self.rejected()
    def test_discovery_summary_cannot_override_raw_request_and_pages(self):
        coverage=self.store.load(self.manifest['discovery_hash']);coverage['query_coverage_verified']=False
        coverage.pop('evidence_hash');coverage['evidence_hash']=digest(coverage);self.manifest['discovery_hash']=self.store.save(coverage);self.bind();self.rejected()
    def test_missing_raw_discovery_page_is_unavailable(self):
        coverage=self.store.load(self.manifest['discovery_hash'])
        with self.store.connect() as c:c.execute('DELETE FROM pages WHERE hash=?',(coverage['pages'][0]['payload_hash'],))
        self.rejected()
    def test_token2022_holder_and_mint_rejected(self):
        from desk.security import TOKEN_2022
        for i in (1,7):
            original=self.bank['result']['value'][i]['owner'];self.bank['result']['value'][i]['owner']=TOKEN_2022;self.bind();self.rejected();self.bank['result']['value'][i]['owner']=original
    def test_mint_authority_and_vault_delegate_rejected(self):
        self.mutate_bytes(1,0,(1).to_bytes(4,'little'));self.rejected()
    def test_wrong_vault_authority_rejected(self):
        self.mutate_bytes(4,32,bytes([2])*32);self.rejected()
    def test_wrong_holder_mint_and_owner_rejected(self):
        original=copy.deepcopy(self.bank)
        for offset in (0,32):
            self.bank=copy.deepcopy(original);self.mutate_bytes(7,offset,bytes([3])*32);self.rejected()
    def test_unknown_pool_layout_and_nonzero_reserved_capacity_reject(self):
        original=copy.deepcopy(self.bank)
        self.mutate_bytes(0,0,bytes(8));self.rejected();self.bank=original
        value=self.bank['result']['value'][0];raw=base64.b64decode(value['data'][0]);raw=raw+bytes(max(300,len(raw))-len(raw));raw=raw[:-1]+b'\x01'
        value['data'][0]=base64.b64encode(raw).decode();self.bind();self.rejected()
    def test_raw_discovery_100_and_101_union_boundary(self):
        from desk.history import collect_history
        from desk.security import base58
        raw=self.f.h.records[0]
        instructions=raw['meta']['innerInstructions'][0]['instructions']
        template=next(ix for ix in instructions if ix.get('parsed',{}).get('type')=='initializeAccount3')
        extras=[]
        for i in range(1000,1092):
            key=base58(i.to_bytes(32,'little'));extras.append(key)
            ix=copy.deepcopy(template);ix['parsed']['info']['account']=key;instructions.append(ix)
        _,coverage=collect_history(self.mint,90,120,self.f.h.transport,max_pages=2,capture=self.store.save,token_accounts='none',slot_range={'gte':0,'lt':21})
        self.manifest['discovery_hash']=self.store.save(coverage)
        states=dict(zip(self.keys,self.bank['result']['value']));states.update({key:None for key in extras})
        frontier=sorted(self.frontier+extras);keys=self.pool+sorted(set(frontier)-set(self.pool))
        self.assertEqual(len(keys),100)
        self.manifest.update(frontier=frontier,keys=keys,ownership_indices=[keys.index(self.mint)]+[keys.index(k) for k in frontier])
        self.bank['params'][0]=keys;self.bank['result']['value']=[states[k] for k in keys];self.bind()
        self.assertEqual(len(self.validate().keys),100)
        ix=copy.deepcopy(template);ix['parsed']['info']['account']=base58((2000).to_bytes(32,'little'));instructions.append(ix)
        _,coverage=collect_history(self.mint,90,120,self.f.h.transport,max_pages=2,capture=self.store.save,token_accounts='none',slot_range={'gte':0,'lt':21})
        self.manifest['discovery_hash']=self.store.save(coverage);self.bind()
        with self.assertRaisesRegex(CommonBankError,'COMMON_BANK_ACCOUNT_CEILING'):self.validate()
    def test_delegate_close_authority_and_holder_owner_rejected(self):
        self.mutate_bytes(4,72,(1).to_bytes(4,'little'));self.rejected()
    def test_mismatched_raw_request_hash_is_rejected(self):
        request=self.store.load(self.manifest['bank_request_hash']);request['response_hash']='f'*64
        self.manifest['bank_request_hash']=self.store.save(request);self.key=self.store.save(self.manifest);self.rejected()
    def test_shared_cache_limits_and_manifest_resource_refuse_without_partial_view(self):
        with patch('desk.common_bank_view.MAX_RECORD_BYTES',1):self.rejected()
        with patch('desk.control_obligations.MAX_HASHES',1):self.rejected()
        with patch('desk.control_obligations.MAX_REPLAY_BYTES',1):self.rejected()
    def test_existing_v1_reader_stays_strict_no_sliced_response_saved(self):
        from desk.ownership_snapshot import validate_bank
        with self.assertRaises(ValueError):validate_bank(self.mint,self.frontier,self.bank)
        with self.store.connect() as c:before=c.execute('SELECT count(*) FROM pages').fetchone()[0]
        self.validate()
        with self.store.connect() as c:self.assertEqual(before,c.execute('SELECT count(*) FROM pages').fetchone()[0])


class PortableCommonBankBoundaryTests(unittest.TestCase):
    def test_requires_shared_readonly_view_before_any_load(self):
        with self.assertRaises(CommonBankError):validate_common_bank('a'*64,object())

    def test_boolean_request_slots_are_not_integer_aliases(self):
        from desk.common_bank_view import _request
        request={'kind':'common_bank_request_v1','network':NETWORK,'genesis_hash':GENESIS,'method':'getBlockTime','params':[True],'response_hash':'a'*64}
        with self.assertRaises(CommonBankError):_request(request,'getBlockTime',[1],'a'*64)
