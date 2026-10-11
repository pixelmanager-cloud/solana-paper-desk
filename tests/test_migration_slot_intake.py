"""Genuine persisted synthetic BIRTH setup; actual wire transport is mocked only."""
import base64
from contextlib import redirect_stdout
import copy
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from desk import migration_slot_intake as m
from desk import paper_read_sources as transport
from desk.ownership_acquisition import acquire
from desk.paper_target_export import export_targets
from desk.paper_cycle_cli import load_targets
from desk.model import canonical
from desk.security import base58
from tests.test_paper_read_sources import Response, KEY
from tests import test_paper_cycle as cycle_fixtures


class MigrationSlotTests(unittest.TestCase):
    def setUp(self):
        self.c=cycle_fixtures.PaperCycleTests();self.addCleanup(self.c.doCleanups);self.c.setUp()
        self.f=self.c.f;self.target=self.c.target
        original=self.f.progress.store.load(self.c.item.graduation_refs[0])
        self.raw=self.f.progress.store.load(original['response_hash'])['data'][0]
        self.signature=base58(bytes([9])*64);self.raw['transaction']['signatures']=[self.signature]
        def setup_rpc(method,params):
            if method=='getAccountInfo':return {'value':self.f.protocol.rpc('getMultipleAccounts',[])['value'][6]}
            if method=='getSlot':return 120
            self.assertEqual(method,'getTransactionsForAddress');return {'data':[]}
        result=acquire(self.f.jobs.path,self.f.progress.store.path,setup_rpc,scan_id=self.target.scan_id)
        self.assertEqual(result['requests_used'],3);self.assertEqual(self.f.progress.admission(self.target.scan_id)['state'],'SEALED')
        self.source=self.f.jobs.source(self.target.scan_id);self.opened=[]
        self.responses=[{'data':[self.raw]}]
        self.output=Path(self.f.tmp.name)/'targets.json'
    def arguments(self,**kwargs):
        return dict(scan_id=self.target.scan_id,mint=self.target.mint,pool=self.target.pool,
                    signature=self.signature,slot=10,provenance='SYNTHETIC_TEST_ONLY',now=self.f.at)|kwargs
    def opener(self):
        outer=self
        class Opener:
            def open(inner,request,*,timeout):
                body=json.loads(request.data);outer.opened.append(body)
                outer.assertEqual(body['params'][0],outer.target.pool)
                outer.assertEqual(body['params'][1]['filters'],{'slot':{'gte':10,'lt':11},'status':'any','tokenAccounts':'none'})
                outer.assertEqual(outer.f.progress.admission(outer.target.scan_id)['requests_used'],3+len(outer.opened))
                outer.assertGreater(timeout,0);outer.assertLessEqual(timeout,10)
                response=outer.responses.pop(0)
                if isinstance(response,BaseException) and not isinstance(response,IncompleteRead):raise response
                return Response(response if type(response) is bytes or isinstance(response,IncompleteRead) else
                    b' '+canonical({'jsonrpc':'2.0','id':'paper-read-v1','result':response}).encode()+b'\n')
        return Opener()
    def intake(self,**kwargs):
        with patch.object(transport.os.environ,'get',return_value=KEY),patch.object(transport,'build_opener',return_value=self.opener()):
            return m.intake(self.f.jobs.path,self.f.progress.store.path,**self.arguments(**kwargs))
    def test_single_slot_originals_existing_export_and_actual_cycle(self):
        result=self.intake();self.assertEqual(result['status'],'RETAINED_MIGRATION_WITNESS')
        self.assertEqual(result['requests_used'],4);self.assertEqual(result['provider_calls'],1)
        self.assertEqual(self.f.jobs.source(self.target.scan_id),self.source)
        manifest=self.f.progress.store.load(result['request_evidence_refs'][0])
        self.assertEqual(manifest['params'][1]['filters']['slot'],{'gte':10,'lt':11})
        response=self.f.progress.store.load(manifest['response_hash']);self.assertEqual(response,{'data':[self.raw]})
        wire=self.f.progress.store.load(result['transport_evidence_refs'][0])
        self.assertEqual(json.loads(base64.b64decode(wire['response_bytes_base64']))['result'],response)
        self.assertTrue(base64.b64decode(wire['response_bytes_base64']).startswith(b' '))
        self.assertIs(result['entry_authorized'],False);self.assertIs(result['source_authenticated'],False)
        repeat=self.intake();self.assertEqual(repeat['provider_calls'],0);self.assertEqual(len(self.opened),1)
        export_targets(self.f.jobs.path,self.f.progress.store.path,self.output,pool_fee_bps='25',now=self.f.at,
            candidate={'scan_id':self.target.scan_id,'pool':self.target.pool,'taker':self.target.taker,
                       'amount_raw':self.target.amount_raw,'provenance':'SYNTHETIC_TEST_ONLY','known_hazards':[]})
        _,targets,_=load_targets(self.output)
        self.assertIn(result['request_evidence_refs'][0],targets[0].graduation_refs)
        self.c.http_calls=[];self.c.sell_output=10_000_000
        cycle=self.c.actual_cycle(candidates=targets)
        self.assertEqual(cycle['status'],'COMPLETE');self.assertFalse(cycle['live_readiness'])
        self.assertEqual(self.f.jobs.source(self.target.scan_id),self.source)
    def test_both_supported_migrations(self):
        from tests.test_graduation_witness import fixture
        raw,mint,pool=fixture('migrate_v2');raw['transaction']['signatures']=[self.signature]
        self.assertEqual((mint,pool),(self.target.mint,self.target.pool));self.responses=[{'data':[raw]}]
        self.assertEqual(self.intake()['status'],'RETAINED_MIGRATION_WITNESS')
    def test_strict_hints_cutoff_binding_before_credentials_or_io(self):
        cases=[{'slot':True},{'slot':-1},{'slot':121},{'slot':2**63},
               {'mint':self.target.pool},{'scan_id':'missing'},{'signature':'bad'},
               {'provenance':'AUTHENTICATED'}]
        for values in cases:
            with self.subTest(values=values),patch.object(transport,'build_opener') as opener:
                loader=lambda: self.fail('No credential loading before hint/admission/cutoff checks')
                with self.assertRaises((ValueError,KeyError)):
                    m.intake(self.f.jobs.path,self.f.progress.store.path,**self.arguments(**values),credentials_loader=loader)
                opener.assert_not_called()
        self.assertEqual(self.f.progress.admission(self.target.scan_id)['requests_used'],3)
    def test_signature_and_pool_hints_reject_without_hiding_charge(self):
        result=self.intake(signature=base58(bytes([10])*64))
        self.assertEqual(result['blockers'],['OBSERVED_MIGRATION_SIGNATURE_MISMATCH'])
        again=self.intake(signature=base58(bytes([10])*64))
        self.assertEqual(again['status'],'BLOCKED');self.assertEqual(again['provider_calls'],0)
        with self.assertRaises(m.IntakeBlocked):self.intake(pool=self.target.mint)
        self.assertEqual(self.f.progress.admission(self.target.scan_id)['requests_used'],4)
    def test_partial_truncation_failure_retained_and_restart_latched(self):
        body=canonical({'jsonrpc':'2.0','id':'paper-read-v1','result':{'data':[self.raw]}}).encode()
        self.responses=[IncompleteRead(body,10)]
        first=self.intake();self.assertEqual(first['blockers'],['CHARGED_OR_AMBIGUOUS_HISTORY_ATTEMPT'])
        wire=self.f.progress.store.load(first['transport_evidence_refs'][0])
        self.assertEqual(base64.b64decode(wire['response_bytes_base64']),body)
        self.assertEqual(wire['failure_code'],'RESPONSE_TRUNCATED');self.assertEqual(first['requests_used'],4)
        retry=self.intake();self.assertEqual(retry['provider_calls'],0);self.assertEqual(len(self.opened),1)
    def test_crash_after_reservation_before_io_cannot_retry(self):
        self.responses=[KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):self.intake()
        retry=self.intake();self.assertEqual(retry['blockers'],['CHARGED_OR_AMBIGUOUS_HISTORY_ATTEMPT'])
        self.assertEqual(retry['requests_used'],4);self.assertEqual(len(self.opened),1)
    def test_two_pages_cursor_preserved_and_total_bound(self):
        self.responses=[{'data':[self.raw],'paginationToken':'next'},{'data':[],'paginationToken':'more'}]
        first=self.intake();self.assertEqual(first['blockers'],['MIGRATION_SLOT_PAGE_LIMIT'])
        self.assertEqual(first['requests_used'],5);self.assertEqual(len(first['request_evidence_refs']),2)
        self.assertEqual(self.opened[1]['params'][1]['paginationToken'],'next')
        self.assertEqual(self.intake()['provider_calls'],0);self.assertEqual(len(self.opened),2)
    def test_two_page_exhaustion_validates_all_returned_rows(self):
        self.responses=[{'data':[self.raw],'paginationToken':'next'},{'data':[]}]
        self.assertEqual(self.intake()['status'],'RETAINED_MIGRATION_WITNESS')
        self.assertEqual(self.f.progress.admission(self.target.scan_id)['requests_used'],5)
    def test_exact_budget_exhaustion_never_calls_opener(self):
        for _ in range(15):self.assertTrue(self.f.progress.reserve(self.target.scan_id))
        result=self.intake();self.assertEqual(result['blockers'],['INVESTIGATION_REQUEST_BUDGET_EXHAUSTED'])
        self.assertEqual(result['requests_used'],18);self.assertEqual(self.opened,[])
    def test_rebound_completed_history_rejects_without_new_io(self):
        result=self.intake()
        with self.f.progress.store.connect() as c:
            c.execute('UPDATE ownership_history SET budget=? WHERE id=?',('forged-other-scan',result['history_id']))
        with self.assertRaises(ValueError):self.intake()
        self.assertEqual(len(self.opened),1)
    def test_missing_schema_never_initialized(self):
        with self.f.progress.store.connect() as c:c.execute('DROP TABLE ownership_history')
        with self.assertRaises(sqlite3.Error):self.intake()
        with self.f.progress.store.connect() as c:
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='ownership_history'").fetchone())
    def test_failed_out_of_slot_missing_time_unknown_scope_and_conflicts(self):
        # Every case uses its own genuine admission; no counter reset for retries.
        for attack in ('failed','slot','time','nested','conflict','malformed'):
            with self.subTest(attack=attack):
                case=MigrationSlotTests();case.setUp()
                try:
                    raw=copy.deepcopy(case.raw)
                    if attack=='failed':raw['meta']['err']={'InstructionError':[0,'custom']}
                    elif attack=='slot':raw['slot']=11
                    elif attack=='time':raw['blockTime']=None
                    elif attack=='nested':raw['meta']['innerInstructions'][0]['instructions'][0]['stackHeight']=3
                    elif attack=='malformed':del raw['meta']
                    rows=[raw]
                    if attack=='conflict':
                        altered=copy.deepcopy(raw);altered['blockTime']-=1;rows.append(altered)
                    case.responses=[{'data':rows}]
                    result=case.intake()
                    self.assertEqual(result['status'],'BLOCKED');self.assertEqual(result['requests_used'],4)
                    self.assertFalse(result['entry_authorized'])
                    manifest=case.f.progress.store.load(result['request_evidence_refs'][0])
                    self.assertEqual(case.f.progress.store.load(manifest['response_hash'])['data'],rows)
                    self.assertEqual(case.f.jobs.source(case.target.scan_id),case.source)
                finally:case.doCleanups()
    def test_crash_after_wire_persistence_retains_outcome_and_latches(self):
        original=m._ExistingStore.save;records=[]
        def save(store,payload):
            if 'data' in payload:raise KeyboardInterrupt()
            key=original(store,payload)
            if payload.get('kind')=='paper_read_attempt_v1':records.append(key)
            return key
        with patch.object(m._ExistingStore,'save',save):
            with self.assertRaises(KeyboardInterrupt):self.intake()
        self.assertEqual(len(records),1)
        wire=self.f.progress.store.load(records[0])
        self.assertIsNone(wire['failure_code']);self.assertIn(self.signature,base64.b64decode(wire['response_bytes_base64']).decode())
        retry=self.intake();self.assertEqual(retry['blockers'],['CHARGED_OR_AMBIGUOUS_HISTORY_ATTEMPT'])
        self.assertEqual(retry['provider_calls'],0);self.assertEqual(retry['requests_used'],4)
        self.assertEqual(self.f.progress.store.load(records[0]),wire)
    def test_wrong_evidence_database_and_corrupt_cutoff_no_io(self):
        import shutil,zlib
        copy_path=Path(self.f.tmp.name)/'other-evidence.sqlite'
        shutil.copyfile(self.f.progress.store.path,copy_path)
        with self.assertRaises(m.IntakeBlocked):
            m.intake(self.f.jobs.path,copy_path,**self.arguments())
        key=json.loads(self.source['result'])['acquisition']['cutoff_hash']
        with self.f.progress.store.connect() as c:
            c.execute('UPDATE pages SET payload=?,raw_bytes=? WHERE hash=?',(zlib.compress(b'{}'),2,key))
        with self.assertRaises(ValueError):self.intake()
        self.assertEqual(self.opened,[])
        self.assertEqual(self.f.progress.admission(self.target.scan_id)['requests_used'],3)
    def test_corruption_after_success_never_reuses_cached_success(self):
        import zlib
        result=self.intake();manifest=self.f.progress.store.load(result['request_evidence_refs'][0])
        with self.f.progress.store.connect() as c:
            c.execute('UPDATE pages SET payload=?,raw_bytes=? WHERE hash=?',
                      (zlib.compress(b'{}'),2,manifest['response_hash']))
        with self.assertRaises(ValueError):self.intake()
        self.assertEqual(len(self.opened),1)
    def test_reservation_adapter_unreserved_and_cross_budget_substitution_no_io(self):
        key=self.f.progress.create(self.target.scan_id,self.target.pool,0,1,{'gte':10,'lt':11})
        adapter=m._SlotSource(self.f.progress,self.target.scan_id,key,slot=10,pool=self.target.pool,timeout_seconds=3)
        params=[self.target.pool,{'transactionDetails':'full','sortOrder':'asc','limit':100,'commitment':'finalized',
            'encoding':'jsonParsed','maxSupportedTransactionVersion':1,
            'filters':{'slot':{'gte':10,'lt':11},'status':'any','tokenAccounts':'none'}}]
        with patch.object(transport,'build_opener') as opened,patch.object(transport.os.environ,'get') as credential:
            with self.assertRaises(ValueError):adapter('getTransactionsForAddress',params)
            with self.f.progress.store.connect() as c:
                c.execute('UPDATE ownership_history SET budget=? WHERE id=?',('other',key))
            with self.assertRaises(ValueError):adapter('getTransactionsForAddress',params)
        opened.assert_not_called();credential.assert_not_called()
        self.assertEqual(self.f.progress.admission(self.target.scan_id)['requests_used'],3)
    def test_unknown_status_and_preexisting_overlimit_complete_query_no_io(self):
        key=self.f.progress.create(self.target.scan_id,self.target.pool,0,1,{'gte':10,'lt':11})
        with self.f.progress.store.connect() as c:c.execute('UPDATE ownership_history SET status=? WHERE id=?',('UNKNOWN',key))
        self.assertEqual(self.intake()['blockers'],['HISTORY_STATUS_INVALID'])
        self.assertEqual(self.f.progress.admission(self.target.scan_id)['requests_used'],3)
        # Existing ordinary history may have been collected by another trusted
        # caller. Intake still refuses an unsupported >2-page record, even DONE.
        with self.f.progress.store.connect() as c:c.execute('UPDATE ownership_history SET status=? WHERE id=?',('PENDING',key))
        pages=[{'data':[self.raw],'paginationToken':'first'},
               {'data':[self.raw],'paginationToken':'second'},{'data':[]}]
        for page in pages:self.f.progress.advance(key,lambda *args:page)
        self.assertEqual(self.intake()['blockers'],['MIGRATION_SLOT_PAGE_LIMIT'])
        self.assertEqual(self.opened,[]);self.assertEqual(self.f.progress.admission(self.target.scan_id)['requests_used'],6)
    def test_cli_actual_command_and_redacted_blockers(self):
        argv=['--coordinator-intake','--research-db',str(self.f.jobs.path),'--evidence-db',str(self.f.progress.store.path)]
        for key,value in self.arguments().items():
            if key!='now':argv.extend(['--'+key.replace('_','-'),str(value)])
        with patch.object(transport.os.environ,'get',return_value=KEY),patch.object(transport,'build_opener',return_value=self.opener()),redirect_stdout(io.StringIO()) as out:
            self.assertEqual(m.main(argv),0)
        self.assertEqual(json.loads(out.getvalue())['status'],'RETAINED_MIGRATION_WITNESS')
        with redirect_stdout(io.StringIO()) as out:self.assertEqual(m.main(argv+['--slot','121']),2)
        self.assertNotIn('Traceback',out.getvalue());self.assertFalse(json.loads(out.getvalue())['live_readiness'])
