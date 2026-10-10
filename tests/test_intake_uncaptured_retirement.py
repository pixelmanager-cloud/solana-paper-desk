"""Synthetic archived-producer guard expiry; all runtime/pin validators are real."""
from contextlib import closing
import copy
import sqlite3
import unittest
from unittest.mock import patch
from pathlib import Path
from desk import paper_intake_uncaptured_retirement as r,paper_migration_no_entry as old
from desk import runtime_compatibility as runtime,runtime_extensions as ext,monitoring_budget as monitoring
from desk import ownership_acquisition as acquisition,migration_slot_intake as intake,paper_read_sources as transport
from desk import paper_terminal_reconciliation as terminal
from desk.evidence import EvidenceStore
from desk.model import canonical,digest
from tools import paper_entry_dispatcher as dispatch
from tests import test_migration_recovery_lineage as fixtures

class StopFixture(Exception):pass

class IntakeRetirementTests(unittest.TestCase):
    def setUp(self):
        ancestor=fixtures.MigrationRecoveryLineageTests();self.addCleanup(ancestor.doCleanups)
        original=old.review_plan;retained={}
        def capture(*args,**kw):
            result=original(*args,**kw)
            if kw.get('apply'):
                retained.update(producer=args[0],pin=old.runtime._parse(old.POLICY.read_text())['recoveries'][0]);raise StopFixture()
            return result
        with patch.object(old,'review_plan',side_effect=capture):
            with self.assertRaises(StopFixture):ancestor.test_real_prefix_prospective_pin_equals_post_extension_and_apply_strict()
        self.producer=retained['pin']['successor_context'];self.ctx={k:self.producer['paths'][k]['path'] for k in ('research_db','evidence_db','ledger_db','pacing_db')}
        self.store=EvidenceStore(self.ctx['evidence_db'],read_only=True);self.store.read_only=False
        self.root=Path(self.ctx['ledger_db']).parent;self.journal=self.producer['journal'];self.cfg=dispatch.cli._config(self.producer['paths']['config']['path'])
        self.identity='7'*32;self.raw,self.mint,self.pool=fixtures.mint_fixture(23)
        with self.store.connect() as c:key=c.execute('SELECT mint_hash FROM ownership_acquisition_setup WHERE scan_id=?',(retained['pin']['scan_id'],)).fetchone()[0]
        mint_value=self.store.load(key)['result'];seed=[0]
        def rpc(method,params):
            if method=='getAccountInfo':return copy.deepcopy(mint_value)
            if method=='getSlot':return 120
            seed[0]+=1;return {'data':[],'paginationToken':'seed' if seed[0]==1 else None}
        acquired=acquisition.acquire(self.ctx['research_db'],self.ctx['evidence_db'],rpc,mint=self.mint);self.scan=acquired['scan_id']
        with dispatch._journal(self.journal) as c:
            values=dispatch._read_journal(c);hint=copy.deepcopy(next(iter(values['intents'].values()))['hint']);at=next(iter(values['intents'].values()))['at']
            hint.update(mint=self.mint,pool=self.pool,signature=self.raw['transaction']['signatures'][0],slot=self.raw['slot'],seq=4)
            self.intent={'version':1,'context_hash':digest(self.producer),'at':at,'hint':hint};dispatch._write(c,'intents',self.identity,self.intent,hint)
        clock=[100.0]
        def guard():clock[0]+=11
        with patch.object(intake.time,'monotonic',side_effect=lambda:clock[0]),patch.object(transport,'build_opener',side_effect=AssertionError('no provider')):
            result=intake.intake(self.ctx['research_db'],self.ctx['evidence_db'],scan_id=self.scan,mint=self.mint,pool=self.pool,signature=hint['signature'],slot=hint['slot'],provenance=dispatch.PROVENANCE,credentials_loader=guard)
        self.assertEqual(result['requests_used'],5);self.assertEqual(result['transport_evidence_refs'],[])
        self.backup=self.root/'intake-before.sqlite'
        with closing(sqlite3.connect(self.ctx['ledger_db'])) as a,closing(sqlite3.connect(self.backup)) as b:a.backup(b)
        self.stopped=self.store.save({'kind':'migration_dispatch_stopped_review_v1','dispatch_id':self.identity,'dispatch_intent_hash':digest(self.intent),'producer_source_hash':self.producer['source_hash'],'exit_status':2,'service_active':False,'timer_active':False,'observed_at':int(at)+400})
        case={'dispatch_id':self.identity,'intent_hash':digest(self.intent),'scan_id':self.scan,'producer_context_hash':digest(self.producer),'history_id':result['history_id']}
        p=patch.object(r,'CASE',case);p.start();self.addCleanup(p.stop)
        self.policy=self.root/'intake-policy.json';self.policy.write_text(canonical({'version':1,'retirements':[]}))
        p=patch.object(r,'POLICY',self.policy);p.start();self.addCleanup(p.stop)
        self.kw=dict(ledger_backup=str(self.backup),stopped_witness_hash=self.stopped)
        self.source='c'*64
        for p in (patch.object(runtime,'implementation_hash',return_value=self.source),patch.object(monitoring,'_implementation',return_value=self.source)):
            p.start();self.addCleanup(p.stop)
        self.proposed=r.review_plan(self.producer,self.identity,self.scan,review_source=self.producer['source_hash'],**self.kw)
        with sqlite3.connect(self.ctx['ledger_db']) as c:
            first,fh,cont,ch=ext._base(c,extended=True);snapshot=ext._snapshot(c,self.cfg);parent=c.execute('SELECT payload_hash FROM '+ext.TABLE+' ORDER BY seq DESC LIMIT 1').fetchone()[0]
        pin={k:snapshot[k] for k in ('checkpoint_hash','metadata_hash','events_count','events_hash','outcomes_count','outcomes_hash')}
        pin.update(sequence=2,first_receipt_hash=fh,continuation_receipt_hash=ch,parent_receipt_hash=parent,predecessor=self.producer['source_hash'],successor=self.source,config_hash=digest(self.cfg),context={k:self.ctx[k] for k in ('research_db','evidence_db','ledger_db')})
        ep=ext.POLICY;policy=runtime._parse(ep.read_text());policy['extensions'].append(pin);ep.write_text(canonical(policy))
        ext.append_runtime(*(self.ctx[k] for k in ('research_db','evidence_db','ledger_db')),self.cfg,**{k:pin[k] for k in ('sequence','first_receipt_hash','continuation_receipt_hash','parent_receipt_hash','predecessor','successor')})
        self.pin=r.review_plan(self.producer,self.identity,self.scan,**self.kw)
        self.assertEqual(self.pin,self.proposed);self.policy.write_text(canonical({'version':1,'retirements':[self.pin]}))
    def apply(self):return r.review_plan(self.producer,self.identity,self.scan,apply=True,**self.kw)
    def gate(self,scans=()):return terminal.gate(self.store,Path(self.ctx['research_db']),scans)
    def test_real_equal_plan_append_preserves_originals_and_restart(self):
        journal=Path(self.journal).read_bytes()
        with closing(sqlite3.connect(Path(self.ctx['ledger_db']).as_uri()+'?mode=ro',uri=True)) as c:ledger=list(c.iterdump())
        self.assertEqual(self.apply()['status'],'RECORDED');self.assertEqual(self.apply()['status'],'ALREADY_RECORDED')
        self.assertEqual(Path(self.journal).read_bytes(),journal)
        with closing(sqlite3.connect(Path(self.ctx['ledger_db']).as_uri()+'?mode=ro',uri=True)) as c:self.assertEqual(list(c.iterdump()),ledger)
        self.assertIsNone(self.gate());self.assertEqual(self.gate((self.scan,)),'REJECTED_SCAN_RETIRED')
        with dispatch._journal(self.journal) as c:
            values=dispatch._validate(c,self.pin['successor_context']);self.assertEqual(len(values['intents']),4);self.assertEqual(len(values['results']),1)
    def test_empty_pin_refuses(self):
        self.policy.write_text(canonical({'version':1,'retirements':[]}))
        with self.assertRaises(ValueError):self.apply()
    def test_any_captured_fifth_blocks(self):
        self.store.save({'kind':'paper_read_attempt_v1','scan_id':self.scan,'requests_used':5})
        with self.assertRaisesRegex(ValueError,'retained transport'):self.apply()
    def test_unknown_reservation_cannot_be_refunded_or_advanced(self):
        with self.store.connect() as c:c.execute('UPDATE ownership_budgets SET used=4 WHERE id=?',(self.scan,))
        with self.assertRaises(ValueError):self.apply()
    def test_pending_pacer_refuses(self):
        with sqlite3.connect(self.ctx['pacing_db']) as c:c.execute("UPDATE state SET pending='pending' WHERE provider='helius'")
        with self.assertRaises(ValueError):self.apply()
    def test_other_unresolved_intent_refuses(self):
        with dispatch._journal(self.journal) as c:
            intent=copy.deepcopy(self.intent);intent['hint']['mint']=self.mint+'x';intent['hint']['signature']='x'
            dispatch._write(c,'intents','8'*32,intent,intent['hint'])
        with self.assertRaises(ValueError):self.apply()
    def test_guard_schema_and_oversize_before_parse(self):
        self.apply()
        with self.store.connect() as c:
            c.execute('DROP TRIGGER '+r.TABLE+'_update');c.execute('UPDATE '+r.TABLE+' SET payload=?',('x'*(2*1024*1024),));c.execute(r.guards()[r.TABLE+'_update'])
        with self.store.connect() as c,patch.object(runtime,'_parse',side_effect=AssertionError('not parsed')):
            with self.assertRaisesRegex(ValueError,'scalar bound'):r.rows(c)
    def test_rollback_partial_schema(self):
        original=r.rows
        def crash_after_insert(c):
            if c.execute('SELECT 1 FROM sqlite_master WHERE name=?',(r.TABLE,)).fetchone():raise ValueError('fault after receipt insert')
            return original(c)
        with patch.object(r,'rows',side_effect=crash_after_insert):
            with self.assertRaises(ValueError):self.apply()
        with self.store.connect() as c:self.assertIsNone(c.execute('SELECT 1 FROM sqlite_master WHERE name=?',(r.TABLE,)).fetchone())
    def test_predecessor_option_cannot_apply(self):
        with self.assertRaisesRegex(ValueError,'read-only'):r.review_plan(self.producer,self.identity,self.scan,apply=True,review_source=self.producer['source_hash'],**self.kw)
    def test_missing_installed_table_blocks(self):
        self.apply()
        with self.store.connect() as c:c.execute('DROP TABLE '+r.TABLE)
        with self.assertRaisesRegex(ValueError,'table missing'):self.gate()
