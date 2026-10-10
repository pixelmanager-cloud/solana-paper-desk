"""Synthetic real retained-lineage proof of an interrupted, unreserved intake."""
import copy
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

from desk.model import canonical,digest
from desk import paper_migration_no_entry as r,runtime_compatibility as runtime,runtime_extensions as ext
from desk import monitoring_budget as monitoring,paper_terminal_reconciliation as terminal
from tools import paper_entry_dispatcher as dispatcher

class PreEntryAbandonmentTests(unittest.TestCase):
    def test_preserved_pending_intake_four_charges_and_terminal_lineage(self):
        from tests import test_intake_uncaptured_retirement as ancestor
        from tests.test_captured_intake_decode_no_entry import HistoricalCapturedGapTests
        from tests.test_migration_recovery_lineage import mint_fixture
        from desk import ownership_acquisition as acquisition
        from desk.history_progress import HistoryProgress
        h=ancestor.IntakeRetirementTests()
        helper=HistoricalCapturedGapTests()
        self.addCleanup(helper.doCleanups)
        with patch.object(ancestor,'IntakeRetirementTests',return_value=h):
            helper.test_actual_lineage_review_extension_append_preserves_originals_and_restart()
        with h.store.connect() as c:
            parent=[v for v in r.rows(c) if v['association']==r.REVIEWED_DECODE_GAP][0]
            key=c.execute('SELECT mint_hash FROM ownership_acquisition_setup WHERE scan_id=?',(h.scan,)).fetchone()[0]
        producer=parent['successor_context'];mint_value=h.store.load(key)['result']
        raw,mint,pool=mint_fixture(31);seed=[0]
        def rpc(method,params):
            if method=='getAccountInfo':return copy.deepcopy(mint_value)
            if method=='getSlot':return 120
            seed[0]+=1;return {'data':[],'paginationToken':'seed' if seed[0]==1 else None}
        acquired=acquisition.acquire(h.ctx['research_db'],h.ctx['evidence_db'],rpc,mint=mint)
        scan=acquired['scan_id'];identity='e'*32
        hint={**h.intent['hint'],'mint':mint,'pool':pool,'signature':raw['transaction']['signatures'][0],'slot':raw['slot'],'seq':6}
        intent={'version':1,'context_hash':digest(producer),'at':h.intent['at'],'hint':hint}
        with dispatcher._journal(h.journal) as c:dispatcher._write(c,'intents',identity,intent,hint)
        progress=HistoryProgress(h.store)
        history=progress.create(scan,pool,0,1,slot_range={'gte':hint['slot'],'lt':hint['slot']+1})
        before=progress.snapshot(history)
        backup=h.root/'unreserved-before.sqlite'
        with closing(sqlite3.connect(h.ctx['ledger_db'])) as a,closing(sqlite3.connect(backup)) as b:a.backup(b)
        stopped=h.store.save({'kind':'migration_dispatch_stopped_review_v1','dispatch_id':identity,'dispatch_intent_hash':digest(intent),'producer_source_hash':producer['source_hash'],'exit_status':15,'service_active':False,'timer_active':False,'observed_at':int(intent['at'])+900})
        kwargs={'ledger_backup':str(backup),'stopped_witness_hash':stopped,'pre_entry_abandonment':True}
        with patch.object(runtime,'implementation_hash',return_value='8'*64),patch.object(monitoring,'_implementation',return_value='8'*64):
            proposed=r.review_plan(producer,identity,scan,review_source=producer['source_hash'],**kwargs)
            self.assertEqual(proposed['admission']['requests_used'],4)
            self.assertEqual(proposed['wire_attempt_refs'],[])
            self.assertEqual(proposed['migration']['status'],'NOT_EVALUATED')
            with sqlite3.connect(h.ctx['ledger_db']) as c:
                _,fh,_,ch=ext._base(c,extended=True);snapshot=ext._snapshot(c,h.cfg)
                prefix=ext._bounded_rows(c);self.assertEqual(len(prefix),3)
                schema=c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()
            pin={k:snapshot[k] for k in ('checkpoint_hash','metadata_hash','events_count','events_hash','outcomes_count','outcomes_hash')}
            pin.update(sequence=4,first_receipt_hash=fh,continuation_receipt_hash=ch,parent_receipt_hash=prefix[-1][2],predecessor=producer['source_hash'],successor='8'*64,config_hash=digest(h.cfg),context={k:h.ctx[k] for k in ('research_db','evidence_db','ledger_db')})
            policy=runtime._parse(ext.POLICY.read_text());policy['extensions'].append(pin);ext.POLICY.write_text(canonical(policy))
            ext.append_runtime(*(h.ctx[k] for k in ('research_db','evidence_db','ledger_db')),h.cfg,**{k:pin[k] for k in ('sequence','first_receipt_hash','continuation_receipt_hash','parent_receipt_hash','predecessor','successor')})
            with sqlite3.connect(h.ctx['ledger_db']) as c:
                self.assertEqual(ext._bounded_rows(c)[:3],prefix)
                self.assertEqual(c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall(),schema)
                ledger=list(c.iterdump())
            actual=r.review_plan(producer,identity,scan,**kwargs);self.assertEqual(actual,proposed)
            policy=runtime._parse(r.POLICY.read_text());policy['recoveries'].append(actual);r.POLICY.write_text(canonical(policy))
            journal=h.journal.read_bytes() if hasattr(h.journal,'read_bytes') else __import__('pathlib').Path(h.journal).read_bytes()
            self.assertEqual(r.review_plan(producer,identity,scan,apply=True,**kwargs)['status'],'RECORDED')
            self.assertEqual(r.review_plan(producer,identity,scan,apply=True,**kwargs)['status'],'ALREADY_RECORDED')
            self.assertEqual(progress.snapshot(history),before)
            self.assertEqual(progress.admission(scan)['requests_used'],4)
            self.assertEqual(__import__('pathlib').Path(h.journal).read_bytes(),journal)
            with sqlite3.connect(h.ctx['ledger_db']) as c:self.assertEqual(list(c.iterdump()),ledger)
            self.assertEqual(terminal.gate(h.store,h.ctx['research_db'],(scan,)),'REJECTED_SCAN_RETIRED')
            with dispatcher._journal(h.journal) as c:
                values=dispatcher._validate(c,actual['successor_context'])
                self.assertEqual((len(values['intents']),len(values['results'])),(6,1))
                self.assertNotIn(identity,values['results'])
            orphan=h.store.save({'kind':'paper_cycle_intent_v1','scan_id':scan})
            with self.assertRaisesRegex(ValueError,'candidate evidence'):r.proof(h.store,actual)
