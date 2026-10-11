"""Synthetic captured decoder gaps: discard-only, never semantic acceptance."""
import copy,json,sqlite3,unittest
from contextlib import closing
from unittest.mock import patch
from desk import paper_migration_no_entry as r,paper_terminal_reconciliation as terminal
from desk.model import canonical,digest
from desk.security import base58
from tests import test_migration_no_entry as fixture
from tests import test_paper_entry_dispatcher as transport_fixture
from tests.test_paper_read_sources import Response
from tools import paper_entry_dispatcher as dispatcher


def multisig_close(raw,*,malformed=False):
    value=copy.deepcopy(raw);value['transaction']['signatures']=[base58(bytes([87])*64)]
    value['transaction']['message']['instructions']=[{'programId':'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA','parsed':{'type':'closeAccount','info':{'account':base58(bytes([61])*32),'destination':base58(bytes([62])*32),'multisigOwner':base58(bytes([63])*32),'signers':[base58(bytes([64])*32)]}}}]
    if malformed:value['transaction']['message']['instructions'][0]['parsed']['info']['account']='invalid-address'
    value['meta']['innerInstructions']=[]
    return value

class CapturedGapTests(unittest.TestCase):
    def setUp(self):
        self.h=fixture.MigrationNoEntryTests();self.addCleanup(self.h.doCleanups);self.h.setUp()
        self.f=self.h.f
    def reject(self,*,incomplete=False):
        pages=[0]
        def wire(data):
            envelope=json.loads(data);pages[0]+=1
            envelope['result']['data']=[self.f.raw,multisig_close(self.f.raw,malformed=True)] if pages[0]==1 else []
            envelope['result']['paginationToken']='remaining' if pages[0]==1 or incomplete else None
            return Response(canonical(envelope).encode())
        with patch.object(transport_fixture,'Response',side_effect=wire):return self.f.live()
    def test_complete_two_pages_publish_preserve_charges_and_ledger_no_retry(self):
        ledger=self.f.ledger.read_bytes();result=self.reject()
        self.assertEqual(result['paper_status'],'NO_ENTRY');self.assertFalse(result['entry_authorized'])
        self.assertEqual(self.f.ledger.read_bytes(),ledger)
        self.assertEqual(self.f.f.progress.admission(result['scan_id'])['requests_used'],6)
        with self.f.f.progress.store.connect() as c:v=r.rows(c)[0]
        self.assertEqual(v['association'],r.DECODE_GAP);self.assertEqual(len(v['wire_attempt_refs']),2)
        self.assertEqual(v['migration'],{'status':'NOT_EVALUATED','witnesses':[],'blockers':['HISTORY_DECODE_GAP']})
        self.assertEqual(terminal.gate(self.f.f.progress.store,self.f.f.jobs.path,(result['scan_id'],)),'REJECTED_SCAN_RETIRED')
        with dispatcher._journal(self.f.journal) as c:dispatcher._validate(c,self.f.ctx)
        calls=list(self.f.calls);self.assertEqual(self.f.invoke()['status'],'NO_CANDIDATE');self.assertEqual(self.f.calls,calls)
    def test_repaired_decoder_cannot_restore_candidate_or_change_original_coverage(self):
        result=self.reject();store=self.f.f.progress.store
        with store.connect() as c:v=r.rows(c)[0]
        original=self.f.f.progress.snapshot(v['history_id'])['coverage']
        with patch('desk.history.decode',side_effect=AssertionError('discard proof never reinterprets failed rows')):
            r.proof(store,v)
            self.assertEqual(terminal.gate(store,self.f.f.jobs.path,(result['scan_id'],)),'REJECTED_SCAN_RETIRED')
        self.assertEqual(self.f.f.progress.snapshot(v['history_id'])['coverage'],original)
    def test_nonexhausted_gap_stays_unresolved(self):
        with self.assertRaises(ValueError):self.reject(incomplete=True)
        self.assertEqual(self.f.count('results'),0)
    def test_later_charge_and_missing_capture_fail_closed(self):
        result=self.reject();store=self.f.f.progress.store
        with store.connect() as c:v=r.rows(c)[0]
        self.f.f.progress.reserve(result['scan_id'])
        with self.assertRaises(ValueError):r.proof(store,v)
        with store.connect() as c:c.execute('DELETE FROM pages WHERE hash=?',(v['wire_attempt_refs'][0],))
        with self.assertRaises(ValueError):r.proof(store,v)
    def test_dropped_receipt_and_publication_crash_stay_blocked(self):
        self.reject();store=self.f.f.progress.store
        with store.connect() as c:c.execute('DROP TABLE '+r.TABLE)
        with self.assertRaises(ValueError):terminal.gate(store,self.f.f.jobs.path,())
    def test_any_candidate_preparation_intent_refuses_discard(self):
        result=self.reject();store=self.f.f.progress.store
        with store.connect() as c:v=r.rows(c)[0]
        key=store.save({'kind':'paper_cycle_intent_v1','admissions':{result['scan_id']:{}}})
        with store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)',('f'*32,key))
        with self.assertRaisesRegex(ValueError,'already entered'):r.proof(store,v)

class HistoricalCapturedGapTests(unittest.TestCase):
    def test_actual_lineage_review_extension_append_preserves_originals_and_restart(self):
        from tests import test_intake_uncaptured_retirement as ancestor
        from tests.test_migration_recovery_lineage import mint_fixture
        from desk import ownership_acquisition as acquisition,migration_slot_intake as intake,paper_read_sources as transport
        from desk import runtime_compatibility as runtime,runtime_extensions as ext,monitoring_budget as monitoring
        h=ancestor.IntakeRetirementTests();self.addCleanup(h.doCleanups);h.setUp();h.apply()
        producer=h.pin['successor_context'];raw,mint,pool=mint_fixture(29);identity='9'*32
        with h.store.connect() as c:key=c.execute('SELECT mint_hash FROM ownership_acquisition_setup WHERE scan_id=?',(h.scan,)).fetchone()[0]
        mint_value=h.store.load(key)['result'];seed=[0]
        def rpc(method,params):
            if method=='getAccountInfo':return copy.deepcopy(mint_value)
            if method=='getSlot':return 120
            seed[0]+=1;return {'data':[],'paginationToken':'seed' if seed[0]==1 else None}
        acquired=acquisition.acquire(h.ctx['research_db'],h.ctx['evidence_db'],rpc,mint=mint);scan=acquired['scan_id']
        hint={**h.intent['hint'],'mint':mint,'pool':pool,'signature':raw['transaction']['signatures'][0],'slot':raw['slot'],'seq':5}
        intent={'version':1,'context_hash':digest(producer),'at':h.intent['at'],'hint':hint}
        with dispatcher._journal(h.journal) as c:dispatcher._write(c,'intents',identity,intent,hint)
        reads=[0]
        class HTTP:
            def open(self,request,*,timeout):
                reads[0]+=1
                return Response(canonical({'jsonrpc':'2.0','id':transport.RPC_ID,'result':{'data':[raw,multisig_close(raw)] if reads[0]==1 else [],'paginationToken':'last' if reads[0]==1 else None}}).encode())
        import hashlib,types
        from pathlib import Path
        original_decoder=(Path(__file__).parent/'fixtures/decode_4d42b13.py.txt').read_bytes()
        self.assertEqual(hashlib.sha256(original_decoder).hexdigest(),'d7a0a3c0c9a525b5c1e38a890540e715bf67cdff1f294ceead1503e6ef3ec93d')
        archived=types.ModuleType('desk._captured_original_decoder_fixture');archived.__package__='desk'
        exec(compile(original_decoder,'decode_4d42b13.py.txt','exec'),archived.__dict__)
        with patch.object(transport,'build_opener',return_value=HTTP()),patch.dict('os.environ',{'HELIUS_API_KEY':'SYNTHETIC_ONLY'}),patch('desk.history.decode',archived.decode):
            retained=intake.intake(h.ctx['research_db'],h.ctx['evidence_db'],scan_id=scan,mint=mint,pool=pool,signature=hint['signature'],slot=hint['slot'],provenance=dispatcher.PROVENANCE)
        self.assertNotEqual(retained['status'],'RETAINED_MIGRATION_WITNESS');self.assertEqual(retained['requests_used'],6)
        backup=h.root/'captured-gap-before.sqlite'
        with closing(sqlite3.connect(h.ctx['ledger_db'])) as a,closing(sqlite3.connect(backup)) as b:a.backup(b)
        stopped=h.store.save({'kind':'migration_dispatch_stopped_review_v1','dispatch_id':identity,'dispatch_intent_hash':digest(intent),'producer_source_hash':producer['source_hash'],'exit_status':2,'service_active':False,'timer_active':False,'observed_at':int(intent['at'])+600})
        kwargs={'ledger_backup':str(backup),'stopped_witness_hash':stopped,'captured_decode_gap':True}
        for p in (patch.object(runtime,'implementation_hash',return_value='9'*64),patch.object(monitoring,'_implementation',return_value='9'*64)):
            p.start();self.addCleanup(p.stop)
        proposed=r.review_plan(producer,identity,scan,review_source=producer['source_hash'],**kwargs)
        with sqlite3.connect(h.ctx['ledger_db']) as c:
            _,fh,_,ch=ext._base(c,extended=True);snapshot=ext._snapshot(c,h.cfg);parent=c.execute('SELECT payload_hash FROM '+ext.TABLE+' ORDER BY seq DESC LIMIT 1').fetchone()[0]
        pin={k:snapshot[k] for k in ('checkpoint_hash','metadata_hash','events_count','events_hash','outcomes_count','outcomes_hash')}
        pin.update(sequence=3,first_receipt_hash=fh,continuation_receipt_hash=ch,parent_receipt_hash=parent,predecessor=producer['source_hash'],successor='9'*64,config_hash=digest(h.cfg),context={k:h.ctx[k] for k in ('research_db','evidence_db','ledger_db')})
        policy=runtime._parse(ext.POLICY.read_text());policy['extensions'].append(pin);ext.POLICY.write_text(canonical(policy))
        ext.append_runtime(*(h.ctx[k] for k in ('research_db','evidence_db','ledger_db')),h.cfg,**{k:pin[k] for k in ('sequence','first_receipt_hash','continuation_receipt_hash','parent_receipt_hash','predecessor','successor')})
        actual=r.review_plan(producer,identity,scan,**kwargs);self.assertEqual(actual,proposed)
        policy=runtime._parse(r.POLICY.read_text());policy['recoveries'].append(actual);r.POLICY.write_text(canonical(policy))
        with sqlite3.connect(h.ctx['ledger_db']) as c:before=list(c.iterdump())
        journal=__import__('pathlib').Path(h.journal).read_bytes()
        self.assertEqual(r.review_plan(producer,identity,scan,apply=True,**kwargs)['status'],'RECORDED')
        self.assertEqual(r.review_plan(producer,identity,scan,apply=True,**kwargs)['status'],'ALREADY_RECORDED')
        self.assertEqual(__import__('pathlib').Path(h.journal).read_bytes(),journal)
        with sqlite3.connect(h.ctx['ledger_db']) as c:self.assertEqual(list(c.iterdump()),before)
        self.assertEqual(terminal.gate(h.store,h.ctx['research_db'],(scan,)),'REJECTED_SCAN_RETIRED')
        with dispatcher._journal(h.journal) as c:
            values=dispatcher._validate(c,actual['successor_context']);self.assertEqual(len(values['intents']),5);self.assertEqual(len(values['results']),1)
            self.assertNotIn(identity,values['results'])
