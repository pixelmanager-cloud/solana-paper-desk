import base64
import json
import tempfile
import unittest
from pathlib import Path
from desk.programs import schemas,instruction,address
from desk.security import base58,TOKEN_PROGRAM
from desk.screen import screen
from desk.dashboard import Jobs

MINT=base58(bytes([7])*32)

class ProgramTests(unittest.TestCase):
    def test_pinned_program_accounts_and_discriminator(self):
        for program, instructions in schemas().items():
            spec=next(x for x in instructions.values() if x['name']=='buy')
            ix={'programId':program,'data':base58(bytes(spec['discriminator'])+bytes(17)),
                'accounts':[x.get('address',MINT) for x in spec['accounts']]}
            self.assertEqual(instruction(ix)['kind'],'BUY_INTENT')
            ix['accounts']=[]
            self.assertEqual(instruction(ix)['status'],'SCHEMA_ACCOUNT_MISMATCH')
    def test_invalid_addresses(self):
        for v in ['https://evil.test',None,'1'*31,'0'*44,123]:
            with self.assertRaises(ValueError):address(v)
    def test_unknown_discriminator(self):
        program=next(iter(schemas()))
        self.assertEqual(instruction({'programId':program,'data':base58(bytes([200])*8)})['status'],'UNKNOWN_DISCRIMINATOR')

class ScanTests(unittest.TestCase):
    def rpc(self,method,params):
        if method=='getAccountInfo':
            d=bytearray(82);d[36:44]=(1000000).to_bytes(8,'little');d[44]=6;d[45]=1
            return {'context':{'slot':100},'value':{'owner':TOKEN_PROGRAM,'executable':False,'data':[base64.b64encode(d).decode(),'base64']}}
        if method=='getTokenLargestAccounts':return {'value':[]}
        if method=='getTransactionsForAddress':return {'data':[]}
        if method=='getTokenAccounts':raise ValueError('DAS unavailable')
        raise AssertionError(method)
    def test_clean_mint_and_routes_never_imply_safe(self):
        r=screen(MINT,rpc=self.rpc,quote=lambda *args:{'response':{'outAmount':'100000'}},clock=lambda:100000)
        self.assertEqual(r['decision'],'SKIP')
        self.assertFalse(r['eligible_for_trading'])
        self.assertFalse(r['history']['launch_history_complete'])
        self.assertFalse(r['roundtrip_quote']['simulation_performed'])
    def test_quote_failure_keeps_report(self):
        def quote(*args):raise ValueError('secret should not be echoed')
        r=screen(MINT,rpc=self.rpc,quote=quote,clock=lambda:100000)
        self.assertIn('BUY_ROUTE_UNAVAILABLE',r['unknowns'])
        self.assertNotIn('secret',json.dumps(r))
    def test_malformed_input_no_network(self):
        with self.assertRaises(ValueError):screen('bad',rpc=lambda *x:self.fail('network'))

    def holder_scan(self,capture=None):
        from unittest.mock import patch
        p=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-holder-snapshot.json').read_text())
        response=p['rpc']['response']
        def rpc(method,params):
            if method=='getAccountInfo':return {'context':response['context'],'value':response['value'][0]}
            if method=='getMultipleAccounts':return response
            return self.rpc(method,params)
        def quote(*args):raise ValueError('No quote in holder replay')
        with patch('desk.screen.enumerate_holders',return_value=p['enumeration']):
            return screen(p['enumeration']['mint'],rpc=rpc,quote=quote,clock=lambda:1791402595,history_capture=capture)
    def test_live_holder_evidence_requires_persistence(self):
        r=self.holder_scan()
        self.assertFalse(r['holder_evidence']['verified']);self.assertIn('FULL_HOLDER_COVERAGE',r['unknowns'])
        self.assertIn('HOLDER_SNAPSHOT_EVIDENCE_NOT_PERSISTED',r['unknowns'])
    def test_persisted_atomic_holder_scope_still_cannot_approve_token(self):
        from desk.evidence import EvidenceStore
        with tempfile.TemporaryDirectory() as d:
            store=EvidenceStore(Path(d)/'evidence.sqlite');r=self.holder_scan(store.save)
            self.assertTrue(r['holder_evidence']['verified']);self.assertNotIn('FULL_HOLDER_COVERAGE',r['unknowns'])
            raw=store.load(r['holder_evidence']['evidence_hash'])
            self.assertEqual(raw['method'],'getMultipleAccounts');self.assertEqual(raw['result']['context']['slot'],r['holder_evidence']['slot'])
            self.assertFalse(r['eligible_for_trading']);self.assertLessEqual(r['calls'],18)

class JobTests(unittest.TestCase):
    def test_job_persistence_failure_and_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            jobs=Jobs(Path(d)/'db',scanner=lambda m:{'mint':m,'eligible_for_trading':False})
            jobs.submit(MINT)
            with self.assertRaises(ValueError):jobs.submit(MINT)
            self.assertTrue(jobs.once());self.assertFalse(jobs.once())
            self.assertEqual(Jobs(Path(d)/'db').list()[0]['status'],'COMPLETE')
    def test_failure_redacts_exception(self):
        def fail(m):raise ValueError('api-key=secret')
        with tempfile.TemporaryDirectory() as d:
            jobs=Jobs(Path(d)/'db',scanner=fail);jobs.submit(MINT);jobs.once()
            self.assertNotIn('api-key',json.dumps(jobs.list()))
            self.assertEqual(jobs.list()[0]['status'],'FAILED')
    def test_restart_marks_inflight_interrupted(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'db';jobs=Jobs(p);jobs.submit(MINT)
            with jobs.connect() as c:c.execute("UPDATE scans SET status='RUNNING'")
            self.assertEqual(Jobs(p).list()[0]['status'],'INTERRUPTED')

class DashboardHTTPTests(unittest.TestCase):
    def test_rebinding_csrf_and_readonly_status(self):
        import threading
        from http.server import ThreadingHTTPServer
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        from desk.dashboard import handler
        with tempfile.TemporaryDirectory() as d:
            jobs=Jobs(Path(d)/'db')
            server=ThreadingHTTPServer(('127.0.0.1',0),handler(jobs,0))
            server.RequestHandlerClass=handler(jobs,server.server_port)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            url=f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(url+'/api/status') as r:self.assertFalse(json.load(r)['live_trading'])
                for headers in [{'Host':'attacker.test'},{'Origin':'https://attacker.test'}]:
                    with self.assertRaises(HTTPError) as e:urlopen(Request(url+'/api/scans',headers=headers))
                    self.assertEqual(e.exception.code,403)
                with self.assertRaises(HTTPError) as e:urlopen(Request(url+'/api/scans',data=b'{}'))
                self.assertEqual(e.exception.code,403)
                with urlopen(Request(url+'/api/scans',data=json.dumps({'mint':MINT}).encode(),headers={'X-Desk-Request':'1'})) as r:self.assertEqual(r.status,202)
                self.assertEqual(len(jobs.list()),1)
            finally:server.shutdown();server.server_close();thread.join()

class EventTests(unittest.TestCase):
    def test_event_schemas_only_load_manifest_entries(self):
        from desk.programs import event_schemas,schemas
        event_schemas.cache_clear()
        self.assertEqual(set(event_schemas()),set(schemas()))
        from desk.providers import PUMP,PUMPSWAP
        self.assertEqual(len(event_schemas()[PUMP]),3)
        self.assertEqual(len(event_schemas()[PUMPSWAP]),4)
        self.assertNotIn('sell_v2',{s['name'] for s in schemas()[PUMPSWAP].values()})

    def test_borsh_rejects_truncation_and_invalid_boolean(self):
        from desk.programs import BorshReader
        for raw,t in [(b'\x02','bool'),(b'\x00','u64'),(b'\xff\xff\xff\xff','string')]:
            with self.assertRaises(ValueError):BorshReader(raw,{}).read(t)
    def test_schema_tampering_rejected(self):
        import unittest.mock as mock
        from desk.programs import schemas
        schemas.cache_clear()
        try:
            with mock.patch.object(Path,'read_bytes',return_value=b'{}'):
                with self.assertRaises(ValueError):schemas()
        finally:schemas.cache_clear()
        self.assertEqual(len(schemas()),2)

class DistributionTests(unittest.TestCase):
    def evidence(self):
        from tests.helpers import clean_evidence,T
        e=clean_evidence();e['early_buys']=[{'wallet':'w000','ts':T-100,'slot':e['launch_slot']}]
        e['transfers']=[{'source':a,'destination':b,'supply_pct':'.6','ts':T-90+i,
                        'source_kind':'private_verified','destination_kind':'private_verified'}
            for i,(a,b) in enumerate([('w000','intermediate'),('intermediate','w050')])]
        return e
    def test_material_supply_moves_through_intermediate(self):
        from desk.bundles import audit
        from tests.helpers import T
        r=audit(self.evidence(),T)
        self.assertTrue(any('w050' in c['wallets'] and 'w000' in c['wallets'] for c in r['clusters']))
    def test_service_hop_never_proves_common_ownership(self):
        from desk.bundles import audit
        from tests.helpers import T
        e=self.evidence();e['transfers'][0]['destination_kind']='pool'
        self.assertEqual(audit(e,T)['clusters'],[])
    def test_unknown_material_path_skips(self):
        from desk.bundles import audit
        from tests.helpers import T
        e=self.evidence();e['transfers'][0]['destination_kind']='unknown'
        self.assertIn('UNCLASSIFIED_MATERIAL_TRANSFER',audit(e,T)['reasons'])
    def test_fragmented_recipient_fanout_blocks_normalized_screen(self):
        from desk.bundles import audit
        from tests.helpers import T
        e=self.evidence();e['transfers']=[{'source':'w000','destination':w,'supply_pct':'.3','ts':T-90,
            'source_kind':'private_verified','destination_kind':'private_verified'} for w in ('w050','w051')]
        r=audit(e,T);self.assertEqual(r['decision'],'SKIP')
        self.assertIn('FRAGMENTED_DISTRIBUTION_REQUIRES_REVIEW',r['reasons'])
        self.assertEqual(r['fragmented_outflows'][0]['gross_supply_pct'],'0.6')
    def test_service_recipient_not_counted_as_private_fanout(self):
        from desk.bundles import audit
        from tests.helpers import T
        e=self.evidence();e['transfers']=[{'source':'w000','destination':w,'supply_pct':'.3','ts':T-90,
            'source_kind':'private_verified','destination_kind':kind} for w,kind in [('w050','private_verified'),('exchange','exchange')]]
        self.assertFalse(audit(e,T)['fragmented_outflows'])
    def test_indirect_earlier_arrival_is_revisited_within_depth_budget(self):
        from desk.bundles import audit
        from tests.helpers import T
        e=self.evidence();e['transfers']=[{'source':a,'destination':b,'supply_pct':'.6','ts':at,
            'source_kind':'private_verified','destination_kind':'private_verified'} for a,b,at in [
                ('w000','intermediate',T-80),('w000','alternate',T-90),('alternate','intermediate',T-89),('intermediate','w050',T-88)]]
        r=audit(e,T)
        self.assertEqual(next(x['depth'] for x in r['links'] if x.get('destination')=='w050'),3)
    def test_split_transfers_aggregate(self):
        from desk.bundles import audit
        from tests.helpers import T
        e=self.evidence();first=e['transfers'][0];first['supply_pct']='.3';e['transfers'].append(dict(first))
        self.assertTrue(audit(e,T)['clusters'])
    def test_transfer_before_acquisition_not_traced(self):
        from desk.bundles import audit
        from tests.helpers import T
        e=self.evidence();e['transfers'][1]['ts']=T-200
        self.assertFalse(any('w050' in c['wallets'] for c in audit(e,T)['clusters']))
    def test_split_funding_arrival_waits_for_material_quantity(self):
        from desk.bundles import audit
        from tests.helpers import T
        e=self.evidence();first=e['transfers'][0];first['supply_pct']='.2';first['ts']=T-90
        e['transfers'].append({**first,'supply_pct':'.4','ts':T-70})
        e['transfers'][1]['ts']=T-80
        self.assertFalse(any('w050' in c['wallets'] for c in audit(e,T)['clusters']))
