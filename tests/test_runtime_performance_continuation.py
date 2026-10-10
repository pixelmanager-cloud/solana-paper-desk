"""Actual four-edge fixture and one immutable performance-only continuation."""
import copy
from contextlib import closing
import sqlite3
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from desk import runtime_compatibility as runtime,runtime_extensions as ext,runtime_performance_continuation as perf
from desk.model import canonical,digest
from tests import test_runtime_extensions as fixtures

class PerformanceContinuationTests(unittest.TestCase):
    def setUp(self):
        self.h=fixtures.RuntimeExtensionTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        h=self.h;h.append()
        previous=h.pin['successor'];parent=None
        for seq,source in ((2,'a'*64),(3,'b'*64),(4,'c'*64)):
            with sqlite3.connect(h.f.new) as c:parent=ext._bounded_rows(c)[-1][2]
            pin=h.make_pin(seq,parent,previous,source);h.set_pins(h.pins+[pin])
            with patch.object(runtime,'implementation_hash',return_value=source):h.append(pin)
            previous=source
        self.predecessor=previous;self.successor='8'*64
        source_patch=patch.object(runtime,'implementation_hash',return_value=self.successor);source_patch.start();self.addCleanup(source_patch.stop)
        self.policy=h.root/'performance.json';self.policy.write_text(canonical({'version':1,'continuations':[]}))
        for p in (patch.object(perf,'POLICY',self.policy),patch.object(perf,'PREDECESSOR',previous)):
            p.start();self.addCleanup(p.stop)
        self.ctx=h.h.edge['context'];self.cfg=h.f.cfg
        self.old={'source_hash':previous,'config_hash':digest(self.cfg),'tool_hash':'d'*64,'entry_tool_hash':'e'*64,
                  'paths':{k:{'path':v,'device':1,'inode':i} for i,(k,v) in enumerate(self.ctx.items(),1)},'journal':str(h.root/'dispatch.sqlite')}
        self.new={**self.old,'source_hash':self.successor}
        from tools import paper_entry_dispatcher as dispatcher
        journal=Path(self.old['journal'])
        with sqlite3.connect(journal) as c:
            for sql in dispatcher.SCHEMAS.values():c.execute(sql)
            for sql in dispatcher._guards().values():c.execute(sql)
            c.execute('INSERT INTO context VALUES(1,?,?)',(canonical(self.old),digest(self.old)))
        journal.chmod(0o600)
        self.pin=perf.plan(h.f.research,h.f.evidence,h.f.new,self.cfg,dispatch_predecessor=self.old,dispatch_successor=self.new)
        self.policy.write_text(canonical({'version':1,'continuations':[self.pin]}))
    def append(self,pin=None):
        h=self.h;return perf.append(h.f.research,h.f.evidence,h.f.new,self.cfg,pin=self.pin if pin is None else pin)
    def test_originals_preserved_restart_and_historical_proofs(self):
        h=self.h
        with sqlite3.connect(h.f.new) as c:
            schemas=c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()
            originals={t:c.execute('SELECT rowid,* FROM '+t).fetchall() for t in ('metadata','state','events','outcomes',runtime.TABLE,'paper_runtime_continuation',ext.TABLE)}
        self.assertEqual(self.append()['status'],'RECORDED')
        self.assertEqual(self.append()['status'],'ALREADY_RECORDED')
        with sqlite3.connect(h.f.new) as c:
            for t,rows in originals.items():self.assertEqual(c.execute('SELECT rowid,* FROM '+t).fetchall(),rows,t)
            for row in schemas:self.assertIn(row,c.execute('SELECT type,name,sql FROM sqlite_master').fetchall())
            self.assertEqual(runtime.require_runtime(c),self.successor)
            self.assertEqual(runtime.require_runtime(c,implementation=self.predecessor),self.predecessor)
            with sqlite3.connect(self.old['journal']) as journal:
                self.assertEqual(perf.dispatch_binding(c,self.new,journal)[0],self.old)
            with sqlite3.connect(self.old['journal']) as journal,self.assertRaises(ValueError):
                perf.dispatch_binding(c,{**self.new,'journal':'/tmp/other.sqlite'},journal)
        # Independent fresh SQLite connection: INSERT OR REPLACE cannot bypass guards.
        with sqlite3.connect(h.f.new) as c:
            payload,key=c.execute('SELECT payload,payload_hash FROM '+perf.TABLE).fetchone()
            for sql in ('INSERT OR REPLACE INTO '+perf.TABLE+' VALUES(1,?,?)','UPDATE '+perf.TABLE+' SET payload=?,payload_hash=?'):
                with self.assertRaises(sqlite3.IntegrityError):c.execute(sql,(payload,key))
            with self.assertRaises(sqlite3.IntegrityError):c.execute('DELETE FROM '+perf.TABLE)
    def test_unreviewed_and_changed_context_or_prefix_never_mutates(self):
        h=self.h
        for field,value in (('extension_prefix_hash','f'*64),('parent_receipt_hash','f'*64),('config_hash','f'*64),('predecessor','f'*64)):
            bad={**self.pin,field:value}
            self.policy.write_text(canonical({'version':1,'continuations':[bad]}))
            with self.subTest(field=field),self.assertRaises(ValueError):self.append(bad)
        self.policy.write_text(canonical({'version':1,'continuations':[self.pin]}))
        for malformed in (None,[],{'ledger_db':[]},{'ledger_db':None}):
            bad=copy.deepcopy(self.pin);bad['dispatch_predecessor']['paths']=malformed
            with self.subTest(paths=malformed),self.assertRaises(ValueError):perf._pin_shape(bad)
        self.policy.write_text(canonical({'version':1,'continuations':[self.pin,self.pin]}))
        with self.assertRaises(ValueError):self.append()
        with sqlite3.connect(h.f.new) as c:self.assertIsNone(perf.read(c))
    def test_empty_partial_or_mutated_receipt_and_original_guard_fail_closed(self):
        h=self.h;self.append()
        with sqlite3.connect(h.f.new) as c:
            c.execute('DROP TRIGGER '+perf.TABLE+'_update')
            with self.assertRaises(ValueError):runtime.require_runtime(c)
            c.execute(perf.guards()[perf.TABLE+'_update'])
            c.execute('DROP TRIGGER '+ext.TABLE+'_update')
            with self.assertRaises(ValueError):runtime.require_runtime(c)


class InstalledHistoricalClosureTests(unittest.TestCase):
    def test_full_retired_chain_and_original_dispatch_journal_under_successor(self):
        from tests import test_intake_uncaptured_retirement as ancestor
        from tests.test_pre_entry_abandonment import PreEntryAbandonmentTests
        from desk import paper_migration_no_entry as migration,monitoring_budget as monitoring,paper_terminal_reconciliation as terminal
        from tools import paper_entry_dispatcher as dispatcher
        h=ancestor.IntakeRetirementTests();helper=PreEntryAbandonmentTests();self.addCleanup(helper.doCleanups)
        with patch.object(ancestor,'IntakeRetirementTests',return_value=h):
            helper.test_preserved_pending_intake_four_charges_and_terminal_lineage()
        with h.store.connect() as c:
            v=[x for x in migration.rows(c) if x['association']==migration.PREWIRE][0]
            # Remove only the intentionally orphaned synthetic negative probe.
            marker=digest({'kind':'paper_cycle_intent_v1','scan_id':v['scan_id']})
            c.execute('DELETE FROM pages WHERE hash=?',(marker,))
        old=v['successor_context']
        # Use the already accepted deployed 1000/day allowance, preserving prior
        # admissions. The ancestral fixture otherwise retains legacy daily10.
        from desk.job_persistence import JobPersistence
        from desk import allowance_policy
        import time
        jobs=JobPersistence(h.ctx['research_db'])
        with jobs.connect() as c:admissions_before=c.execute('SELECT id,mint,created FROM scans ORDER BY id').fetchall()
        jobs.upgrade_allowance(at=int(time.time()),provenance=allowance_policy.PROVENANCE)
        with jobs.connect() as c:self.assertEqual(c.execute('SELECT id,mint,created FROM scans ORDER BY id').fetchall(),admissions_before)
        import base64
        from tests.test_migration_recovery_lineage import mint_fixture
        from desk import ownership_acquisition as acquisition
        with h.store.connect() as c:key=c.execute('SELECT mint_hash FROM ownership_acquisition_setup WHERE scan_id=?',(h.scan,)).fetchone()[0]
        mint_value=h.store.load(key)['result']
        safe_mint_value=copy.deepcopy(mint_value)
        raw_mint=bytearray(base64.b64decode(mint_value['value']['data'][0]))
        raw_mint[:4]=(1).to_bytes(4,'little');raw_mint[4:36]=bytes([7])*32
        mint_value['value']['data'][0]=base64.b64encode(raw_mint).decode()
        with patch.object(runtime,'implementation_hash',return_value=old['source_hash']),patch.object(monitoring,'_implementation',return_value=old['source_hash']):
            for n in range(3):
                raw,mint,pool=mint_fixture(40+n)
                acquired=acquisition.acquire(h.ctx['research_db'],h.ctx['evidence_db'],lambda method,params:copy.deepcopy(mint_value),mint=mint)
                self.assertEqual(acquired['requests_used'],1)
                hint={**h.intent['hint'],'mint':mint,'pool':pool,'signature':raw['transaction']['signatures'][0],'slot':raw['slot'],'seq':7+n}
                intent={'version':1,'context_hash':digest(old),'at':h.intent['at'],'hint':hint};identity=('%032x'%(100+n))
                result=dispatcher._rejection_evidence(old,intent,acquired['scan_id'])
                with dispatcher._journal(h.journal) as journal:
                    dispatcher._write(journal,'intents',identity,intent,hint)
                    dispatcher._write(journal,'results',identity,{'version':1,'intent_hash':digest(intent),'at':h.intent['at'],'scan_id':acquired['scan_id'],'result':result})
                    dispatcher._validate(journal,old)
            # Two genuine historical aggregate-byte preparation NO_ENTRY results,
            # not five interchangeable token rejections. Keep all captured wire
            # bytes, charges and old-source monitoring/checkpoint validation.
            from tests.test_history_preparation_phase import PreparationTests
            from desk import paper_cycle as cycle,history_preparation_rejection as rejection
            from desk.history_progress import HistoryProgress
            from desk.paper_observation_collector import ObservationTarget
            from tools import history_first_paper_entry as entry
            for n in range(3,5):
                raw,mint,pool=mint_fixture(40+n);seed=[0]
                def rpc(method,params):
                    if method=='getAccountInfo':return copy.deepcopy(safe_mint_value)
                    if method=='getSlot':return 120
                    seed[0]+=1;return {'data':[],'paginationToken':'seed' if seed[0]==1 else None}
                acquired=acquisition.acquire(h.ctx['research_db'],h.ctx['evidence_db'],rpc,mint=mint)
                self.assertEqual(acquired['requests_used'],4)
                hint={**h.intent['hint'],'mint':mint,'pool':pool,'signature':raw['transaction']['signatures'][0],'slot':raw['slot'],'seq':7+n}
                intent={'version':1,'context_hash':digest(old),'at':h.intent['at'],'hint':hint};identity='%032x'%(100+n)
                with dispatcher._journal(h.journal) as journal:dispatcher._write(journal,'intents',identity,intent,hint)
                prep=PreparationTests();prep.store=h.store;prep.progress=HistoryProgress(h.store)
                prep.target=ObservationTarget(acquired['scan_id'],mint,pool,old['taker'],old['amount_raw'])
                prep.as_of=int(time.time());prep.cfg=h.cfg;prep.ledger=Path(h.ctx['ledger_db']);prep.pacer=Path(h.ctx['pacing_db']);prep.context=h.ctx
                prep.item=cycle.CycleTarget(prep.target,provenance='SYNTHETIC_TEST_ONLY',graduated_at=None,holder_at=None,pool_fee_bps='25',history_as_of=prep.as_of)
                for _ in range(2):self.assertTrue(prep.progress.reserve(prep.target.scan_id))
                prep.budget=entry._PreparationBudget(prep.progress,prep.item,h.cfg);prep.identity='%032x'%(200+n)
                historical_intent=rejection.intent(h.store,prep.ledger,h.cfg,prep.item,prep.progress.admission(prep.target.scan_id),h.ctx)
                historical_intent.pop('feature_semantics_version')
                historical_intent['kind']='history_first_paper_preparation_v2'
                prep.intent=h.store.save(historical_intent)
                with h.store.connect() as c:c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)',(prep.identity,prep.intent))
                # Reproduce the historical producer only. Replay below and
                # after installation uses unpatched current validators.
                bounds,reason=rejection.bounds,rejection.reason_for
                def old_bounds(*args,**kwargs):return bounds(*args,**{**kwargs,'semantics_version':1})
                def old_reason(*args,**kwargs):return reason(*args,**{**kwargs,'semantics_version':1})
                with patch.object(rejection,'bounds',side_effect=old_bounds),patch.object(rejection,'reason_for',side_effect=old_reason):
                    prep.advance({'data':prep.rows(50,padding=13000),'paginationToken':'p2'})
                    prep.advance({'data':prep.rows(50,start=50,padding=13000),'paginationToken':'p3'})
                    with self.assertRaisesRegex(entry.PreparationRejected,'HISTORY_FEATURE_AGGREGATE_BYTES_EXCEEDED'):
                        prep.advance({'data':prep.rows(50,start=100,padding=13000),'paginationToken':'p4'})
                    result=prep.publish('HISTORY_FEATURE_AGGREGATE_BYTES_EXCEEDED')
                self.assertEqual(result['admission_after']['requests_used'],9)
                with dispatcher._journal(h.journal) as journal:
                    dispatcher._write(journal,'results',identity,{'version':1,'intent_hash':digest(intent),'at':h.intent['at'],'scan_id':acquired['scan_id'],'result':result})
                    dispatcher._validate(journal,old)
        new={**old,'source_hash':'7'*64};policy=h.root/'performance-full-chain.json'
        policy.write_text(canonical({'version':1,'continuations':[]}))
        with patch.object(perf,'POLICY',policy),patch.object(perf,'PREDECESSOR',old['source_hash']),patch.object(runtime,'implementation_hash',return_value=new['source_hash']),patch.object(monitoring,'_implementation',return_value=new['source_hash']):
            pin=perf.plan(*(h.ctx[k] for k in ('research_db','evidence_db','ledger_db')),h.cfg,dispatch_predecessor=old,dispatch_successor=new)
            policy.write_text(canonical({'version':1,'continuations':[pin]}))
            self.assertEqual(perf.append(*(h.ctx[k] for k in ('research_db','evidence_db','ledger_db')),h.cfg,pin=pin)['status'],'RECORDED')
            self.assertIsNone(terminal.gate(h.store,h.ctx['research_db'],()))
            self.assertEqual(terminal.gate(h.store,h.ctx['research_db'],(v['scan_id'],)),'REJECTED_SCAN_RETIRED')
            with dispatcher._journal(h.journal) as c:
                values=dispatcher._validate(c,new)
                self.assertEqual((len(values['intents']),len(values['results'])),(11,6))
                self.assertNotIn(v['dispatch_id'],values['results'])
                raw,mint,pool=mint_fixture(55)
                late_hint={**h.intent['hint'],'mint':mint,'pool':pool,'signature':raw['transaction']['signatures'][0],'slot':raw['slot'],'seq':20}
                late={'version':1,'context_hash':digest(old),'at':h.intent['at'],'hint':late_hint}
                dispatcher._write(c,'intents','%032x'%999,late,late_hint)
                with self.assertRaisesRegex(ValueError,'Dispatch intent binding invalid'):dispatcher._validate(c,new)


class ReceiptScalarTests(unittest.TestCase):
    def test_dispatcher_review_source_cannot_select_another_context(self):
        from tools import paper_entry_dispatcher as dispatcher
        with sqlite3.connect(':memory:') as c:
            for source in ('b'*64,None,False,[],'not-a-hash'):
                if source is None:continue  # Normal callers retain current-source checks.
                with self.assertRaisesRegex(ValueError,'Exact historical dispatcher review source required'):
                    dispatcher._check_records(c,{'source_hash':'a'*64},{}, {}, {},review_source=source)

    def test_empty_receipt_and_oversized_payload_fail_before_decode(self):
        with tempfile.TemporaryDirectory() as d,sqlite3.connect(Path(d)/'receipt.sqlite') as c:
            c.execute(perf.schema())
            for sql in perf.guards().values():c.execute(sql)
            with self.assertRaisesRegex(ValueError,'One performance receipt'):perf.read(c)
            c.execute('INSERT INTO '+perf.TABLE+' VALUES(1,?,?)',('x'*(runtime.MAX_BYTES+1),'a'*64))
            with patch.object(runtime,'_parse',side_effect=AssertionError('must preflight before decode')):
                with self.assertRaisesRegex(ValueError,'scalar bound'):perf.read(c)
    def test_insert_replace_rowid_alias_and_second_receipt_refused(self):
        with tempfile.TemporaryDirectory() as d,sqlite3.connect(Path(d)/'receipt.sqlite') as c:
            c.execute(perf.schema())
            for sql in perf.guards().values():c.execute(sql)
            c.execute('INSERT INTO '+perf.TABLE+' VALUES(1,?,?)',('original','a'*64))
            for alias in ('rowid','_rowid_','oid','id'):
                with self.subTest(alias=alias),self.assertRaises(sqlite3.IntegrityError):
                    c.execute('INSERT OR REPLACE INTO '+perf.TABLE+'('+alias+',payload,payload_hash) VALUES(1,?,?)',('replacement','b'*64))
            with self.assertRaises(sqlite3.IntegrityError):c.execute('INSERT INTO '+perf.TABLE+' VALUES(2,?,?)',('second','b'*64))
            self.assertEqual(c.execute('SELECT rowid,payload FROM '+perf.TABLE).fetchall(),[(1,'original')])
