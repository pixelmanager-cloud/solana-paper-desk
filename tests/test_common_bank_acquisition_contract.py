"""Design feasibility, not production common-bank capture or admission.

One synthetic provider returns an ORIGINAL union response. Indexed views below
are test-only address/value lookups, never stored/supplied as sliced RPC replies.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from desk.coordinator_rpc import _params
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.model import digest
from desk.ownership_snapshot import validate_bank
from desk.pool_capture_bridge import canonical_accounts
from desk.pool_vault_admission import _bound_capture, EvidenceRefs, GENESIS, _Reject
from desk.programs import address
from desk.security import base58

ROOT = Path(__file__).resolve().parents[1]


def plan(mint, frontier):
    """Test-only executable request-order proposal; no production API."""
    pool = canonical_accounts(mint)
    for key in frontier: address(key)
    if len(frontier) != len(set(frontier)) or mint in frontier or pool[4] not in frontier:
        raise ValueError('Invalid/incomplete holder frontier')
    # Preserve exact pool role order as a prefix; append other holders uniquely.
    keys = pool + sorted(set(frontier) - set(pool))
    if len(keys) > 100: raise ValueError('Atomic union exceeds 100')
    return keys, [keys.index(mint)] + [keys.index(k) for k in sorted(frontier)], list(range(6))


def indexed(record, mint, frontier, *, floor):
    """Semantic view of original response, deliberately NOT an RPC envelope."""
    keys, ownership, pool = plan(mint, frontier)
    expected = [keys, {'encoding':'base64','commitment':'finalized','minContextSlot':floor}]
    if record.get('kind') != 'rpc_response_v1' or record.get('method') != 'getMultipleAccounts' or record.get('params') != expected:
        raise ValueError('Original union request mismatch')
    result = record['result']; slot = result['context']['slot']
    if type(slot) is not int or slot < floor or type(result['value']) is not list or len(result['value']) != len(keys):
        raise ValueError('Original union bank mismatch')
    return {'parent_hash':digest(record), 'slot':slot,
            'ownership_indices':ownership,'pool_indices':pool,
            'accounts':dict(zip(keys,result['value']))}


class CommonBankRequestContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((ROOT/'fixtures/pool-vault-admission-legacy.json').read_text())
        self.q = self.fixture['query']; self.pool = canonical_accounts(self.q['mint'])
        self.frontier = [self.pool[4],base58(bytes([11])*32),base58(bytes([12])*32)]

    def test_deterministic_union_order_roles_and_no_duplicate_vault_or_mint(self):
        a = plan(self.q['mint'],self.frontier); b = plan(self.q['mint'],list(reversed(self.frontier)))
        self.assertEqual(a,b); keys,own,pool = a
        self.assertEqual(keys[:6],self.pool); self.assertEqual(len(keys),8)
        self.assertEqual([keys[i] for i in own],[self.q['mint']]+sorted(self.frontier))
        self.assertEqual([keys[i] for i in pool],self.pool)
        _params('getMultipleAccounts',[keys,{'encoding':'base64','commitment':'finalized','minContextSlot':20}])

    def test_100_account_boundary_rejects_101_without_io_or_chunking(self):
        # With only base vault shared, 95 holders + five other union accounts.
        holders = [self.pool[4]]+[base58(i.to_bytes(32,'little')) for i in range(1000,1094)]
        self.assertEqual(len(plan(self.q['mint'],holders)[0]),100)
        with self.assertRaises(ValueError):plan(self.q['mint'],holders+[base58((2000).to_bytes(32,'little'))])

    def test_missing_base_vault_duplicate_frontier_and_mint_as_holder_reject(self):
        for frontier in (self.frontier[1:],self.frontier+[self.frontier[0]],self.frontier+[self.q['mint']]):
            with self.subTest(frontier=frontier),self.assertRaises(ValueError):plan(self.q['mint'],frontier)

    def test_one_original_union_record_binds_both_views_without_sliced_responses(self):
        keys,_,_ = plan(self.q['mint'],self.frontier)
        original = copy.deepcopy(self.fixture['evidence'][self.fixture['refs']['snapshot']])
        states = dict(zip(original['params'][0],original['result']['value']))
        for key in self.frontier[1:]: states[key] = None  # Closed historical holders remain covered.
        params = [keys,{'encoding':'base64','commitment':'finalized','minContextSlot':20}]
        calls = []
        def fixture_rpc(method,supplied):
            calls.append((method,copy.deepcopy(supplied)));self.assertEqual(supplied,params)
            return {'context':{'slot':21},'value':[copy.deepcopy(states[k]) for k in keys]}
        record = {'kind':'rpc_response_v1','method':'getMultipleAccounts','params':params,
                  'result':fixture_rpc('getMultipleAccounts',params)}
        with tempfile.TemporaryDirectory() as root:
            store=EvidenceStore(Path(root)/'evidence.sqlite'); key=store.save(record)
            view=indexed(store.load(key),self.q['mint'],self.frontier,floor=20)
            self.assertEqual(view['parent_hash'],key);self.assertEqual(view['slot'],21)
            self.assertEqual(view['accounts'][self.q['mint']],original['result']['value'][1])
            self.assertEqual(view['accounts'][self.pool[4]],original['result']['value'][4])
            with store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM pages').fetchone()[0],1)
            self.assertEqual(store.load(key),record)
        self.assertEqual(len(calls),1)
        self.assertNotIn('method',view);self.assertNotIn('params',view)

    def test_slot_floor_is_not_historical_selector_and_changed_order_rejects(self):
        keys,_,_=plan(self.q['mint'],self.frontier)
        record={'kind':'rpc_response_v1','method':'getMultipleAccounts',
                'params':[keys,{'encoding':'base64','commitment':'finalized','minContextSlot':20}],
                'result':{'context':{'slot':23},'value':[None]*len(keys)}}
        self.assertEqual(indexed(record,self.q['mint'],self.frontier,floor=20)['slot'],23)
        for mutate in ('floor','slot','bool','order','omit'):
            bad=copy.deepcopy(record)
            if mutate=='floor':bad['params'][1]['minContextSlot']=23
            if mutate=='slot':bad['result']['context']['slot']=19
            if mutate=='bool':bad['result']['context']['slot']=True
            if mutate=='order':bad['params'][0][0],bad['params'][0][1]=bad['params'][0][1],bad['params'][0][0]
            if mutate=='omit':bad['result']['value'].pop()
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):indexed(bad,self.q['mint'],self.frontier,floor=20)

    def test_existing_exact_readers_reject_union_not_implicitly_accept_subsets(self):
        original=copy.deepcopy(self.fixture['evidence'][self.fixture['refs']['snapshot']])
        keys,_,_=plan(self.q['mint'],self.frontier)
        original['params'][0]=keys;original['result']['value'] += [None]*(len(keys)-6)
        with self.assertRaises(ValueError):validate_bank(self.q['mint'],self.frontier,original)
        evidence=[original,self.fixture['evidence'][self.fixture['refs']['snapshot_request']],
                  self.fixture['evidence'][self.fixture['refs']['block_time']],
                  self.fixture['evidence'][self.fixture['refs']['block_time_request']]]
        with self.assertRaises(_Reject):_bound_capture(evidence,EvidenceRefs(**self.fixture['refs']),self.pool,self.q['snapshot_slot'],self.q['snapshot_time'])

    def test_reservation_survives_crash_before_io_and_failure_never_refunds(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'evidence.sqlite';progress=HistoryProgress(EvidenceStore(path))
            descriptor={'kind':'ownership_admission_v1','scan_id':'contract','mint':self.q['mint'],'created':1}
            progress.admit('contract',descriptor)
            self.assertTrue(progress.reserve('contract'))  # Process may die before intent or I/O.
            resumed=HistoryProgress(EvidenceStore(path));self.assertEqual(resumed.admission('contract')['requests_used'],1)
            self.assertTrue(resumed.reserve('contract'))
            try:raise OSError('synthetic failed setup request')
            except OSError:pass
            self.assertEqual(HistoryProgress(EvidenceStore(path)).admission('contract')['requests_used'],2)
            for _ in range(16):self.assertTrue(resumed.reserve('contract'))
            self.assertFalse(resumed.reserve('contract'));self.assertEqual(resumed.admission('contract')['requests_used'],18)

    def test_real_shared_counter_charges_four_setup_calls_before_each_io(self):
        with tempfile.TemporaryDirectory() as root:
            progress=HistoryProgress(EvidenceStore(Path(root)/'evidence.sqlite'))
            progress.admit('contract',{'kind':'ownership_admission_v1','scan_id':'contract','mint':self.q['mint'],'created':1})
            # Preserve three original charged attempts, then capture before replay.
            for _ in range(3):self.assertTrue(progress.reserve('contract'))
            keys,_,_=plan(self.q['mint'],self.frontier);calls=[]
            point=self.fixture['evidence'][self.fixture['refs']['snapshot']]
            states=dict(zip(point['params'][0],point['result']['value']))
            states.update({k:None for k in self.frontier[1:]})
            stages=[('getGenesisHash',[]),('getSlot',[{'commitment':'finalized'}]),
                    ('getMultipleAccounts',[keys,{'encoding':'base64','commitment':'finalized','minContextSlot':20}]),('getBlockTime',[21])]
            def fixture_rpc(method,params):
                # Assert the durable charge is visible inside each actual fixture call.
                self.assertEqual(progress.admission('contract')['requests_used'],4+len(calls))
                calls.append((method,copy.deepcopy(params)))
                return {'getGenesisHash':GENESIS,'getSlot':20,'getMultipleAccounts':
                        {'context':{'slot':21},'value':[copy.deepcopy(states[k]) for k in keys]},
                        'getBlockTime':110}[method]
            records=[]
            for method,params in stages:
                self.assertTrue(progress.reserve('contract'));_params(method,params)
                record={'kind':'rpc_response_v1','method':method,'params':params,
                        'result':fixture_rpc(method,params)}
                records.append(progress.store.save(record))
            self.assertEqual(len(calls),4)
            union=progress.store.load(records[2])
            self.assertEqual(indexed(union,self.q['mint'],self.frontier,floor=20)['slot'],21)
            clock=progress.store.load(records[3]);self.assertEqual(clock['params'],[21])
            # Two mint pages and three two-page account queries after capture.
            for _ in range(8):self.assertTrue(progress.reserve('contract'))
            self.assertEqual(progress.admission('contract')['requests_used'],15)
            # A four-holder/two-page continuation at A=5 needs 5+4+2+8=19: impossible.
            self.assertGreater(5+4+2+2*4,18)


@unittest.skipUnless(Path('/proc/self/mountinfo').is_file(),'Existing Linux-only guarded point contract')
class ExistingPointFeasibilityTests(unittest.TestCase):
    def test_existing_matched_separate_fixture_path_works_but_advancement_cannot_retarget(self):
        from tests import test_pool_classification_projection as fixture
        h=fixture.PoolClassificationProjectionTests();self.addCleanup(h.doCleanups);h.setUp()
        result=h.project();self.assertTrue(result['point_binding_verified']);self.assertFalse(result['exclusion_allowed'])
        bank=HistoryProgress(h.h.store).bank('multi');before=copy.deepcopy(bank);calls=[]
        def forbidden(*args):calls.append(args);raise AssertionError('No recapture on restart')
        result=HistoryProgress(h.h.store).capture_bank('multi',h.q['mint'],h.h.f['accounts'],forbidden)
        self.assertFalse(result['attempted']);self.assertEqual(before,HistoryProgress(h.h.store).bank('multi'));self.assertEqual(calls,[])
        self.assertEqual(h.h.store.load(bank['snapshot_hash'])['result']['context']['slot'],20)

    def test_one_union_preserves_complete_real_fixture_holder_endings_and_pool_state(self):
        from tests import test_pool_classification_projection as fixture
        from desk.entry_evidence import continuation_snapshot
        from desk.security import holding_policy, mint_policy
        h=fixture.PoolClassificationProjectionTests();self.addCleanup(h.doCleanups);h.setUp()
        self.assertTrue(h.project()['point_binding_verified'])
        replay=continuation_snapshot(h.scan,json.loads(h.scan['result']),EvidenceStore(h.h.path,read_only=True),progress={'evidence_hash':h.head['evidence_hash']})
        self.assertTrue(replay['replay']['reconciled'])
        frontier=[r['address'] for r in replay['history']['inventory']['accounts']]
        bank=h.h.store.load(h.head['snapshot_evidence']['snapshot_hash'])
        point=h.h.store.load(h.refs.snapshot)
        states=dict(zip(point['params'][0],point['result']['value']))
        for key,value in zip(bank['params'][0],bank['result']['value']):
            if key in states:self.assertEqual(states[key],value)
            states[key]=copy.deepcopy(value)
        keys,_,_=plan(h.q['mint'],frontier);calls=[]
        params=[keys,{'encoding':'base64','commitment':'finalized','minContextSlot':19}]
        def synthetic_union_rpc(method,request):
            calls.append((method,copy.deepcopy(request)));self.assertEqual(request,params)
            return {'context':{'slot':20},'value':[copy.deepcopy(states[k]) for k in keys]}
        parent={'kind':'rpc_response_v1','method':'getMultipleAccounts','params':params,
                'result':synthetic_union_rpc('getMultipleAccounts',params)}
        parent_hash=h.h.store.save(parent);view=indexed(h.h.store.load(parent_hash),h.q['mint'],frontier,floor=19)
        self.assertEqual(view['slot'],replay['replay']['slot']);self.assertEqual(len(calls),1)
        observed=0
        for end in replay['history']['account_continuity']['end_states']:
            value=view['accounts'][end['account']]
            self.assertFalse(end['closed']);checked=holding_policy(value,h.q['mint'],end['owner'])
            self.assertEqual(checked['decision'],'PASS_HOLDING_POLICY');self.assertEqual(checked['amount_raw'],end['amount_raw'])
            observed+=int(checked['amount_raw'])
        self.assertEqual(observed,int(mint_policy(view['accounts'][h.q['mint']])['supply_raw']))
        for key,value in zip(point['params'][0],point['result']['value']):self.assertEqual(view['accounts'][key],value)
        self.assertEqual(h.h.store.load(parent_hash),parent)
        self.assertFalse(h.project()['exclusion_allowed'])
