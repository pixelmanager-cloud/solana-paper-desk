"""Disconnected fixture journal; actual process death/SQLite, never providers."""
import copy
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from desk import common_bank_journal as module
from desk.common_bank_journal import CommonBankJournal, JournalBlocked, SCHEMA, APPLICATION_ID
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.model import canonical, digest
from desk.pool_receipt_ledger import ApprovedSource
from tests import test_pool_classification_projection as projection


def _child(research,evidence,fence,mode,queue=None,go=None):
    if go is not None: go.wait(10)
    j = CommonBankJournal(research,evidence,ApprovedSource('fixture','synthetic_fixture'))
    if mode == 'before_commit':
        real_connect = sqlite3.connect
        class InterruptedConnection(sqlite3.Connection):
            def commit(self):
                try: pending = self.execute('SELECT count(*) FROM common_bank_events').fetchone()[0]
                except sqlite3.Error: pending = 0
                if pending: os._exit(78)
                return super().commit()
        def connect(*args,**kwargs):
            return real_connect(*args,**{**kwargs,'factory':InterruptedConnection})
        sqlite3.connect = connect
    try:
        with j.locked() as session:
            event = session.reserve_stage('capture','genesis','getGenesisHash',[],
                                          fence=fence,reserved_at=121)
            if mode == 'after_commit': os._exit(77)
        if queue is not None: queue.put(('OK',event['used_after']))
    except Exception as exc:
        if queue is not None: queue.put(('BLOCKED',str(exc)))
        else: raise


@unittest.skipUnless(Path('/proc/self/mountinfo').is_file(),'Linux stable guarded source contract')
class CommonBankJournalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = projection.PoolClassificationProjectionTests()
        self.addCleanup(self.fixture.doCleanups); self.fixture.setUp()
        f = self.fixture; self.root = f.root
        self.path = self.root/'journal-evidence.sqlite'; self.research = self.root/'journal-research.sqlite'
        self.store = EvidenceStore(self.path)
        # Preserve fixture originals; copy immutable pages into a fresh SEALED
        # pre-bank fixture rather than deleting an existing canonical bank/head.
        with f.h.store.connect() as c: hashes = [r[0] for r in c.execute('SELECT hash FROM pages')]
        for key in hashes: self.assertEqual(self.store.save(f.h.store.load(key)),key)
        self.scan = copy.deepcopy(f.scan); report = json.loads(self.scan['result'])
        report.pop('report_hash'); report['eligible_for_trading'] = False
        report['report_hash'] = digest(report); self.scan['result'] = canonical(report)
        with sqlite3.connect(self.research) as c:
            c.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
            c.execute('INSERT INTO scans VALUES(?,?,?,?,?)',tuple(self.scan.values()))
        self.progress = HistoryProgress(self.store)
        self.descriptor = {'kind':'ownership_admission_v1','scan_id':'multi',
                           'mint':self.scan['mint'],'created':self.scan['created']}
        a = self.progress.admit('multi',self.descriptor)
        for _ in range(3): self.assertTrue(self.progress.reserve('multi'))
        self.progress.prepare_source('multi',a['descriptor_hash'],self.scan)
        self.progress.seal_source('multi',a['descriptor_hash'],digest(self.scan))
        self.source = ApprovedSource('fixture','synthetic_fixture')
        self.journal = CommonBankJournal(self.research,self.path,self.source)

    def create(self):
        with self.journal.locked() as session: return session.create_run('multi','capture')

    def used(self): return self.progress.admission('multi')['requests_used']

    def reserve(self,fence=None):
        with self.journal.locked() as session:
            return session.reserve_stage('capture','genesis','getGenesisHash',[],
                fence=self.fence if fence is None else fence,reserved_at=121)

    def freeze(self):
        result = self.create(); self.fence = result['fence']; return result

    def resume(self):
        with self.journal.locked() as session: return session.resume_capture('multi')

    def originals(self):
        with self.store.connect() as c:
            return dict(pages=c.execute('SELECT * FROM pages ORDER BY hash').fetchall(),
                admissions=c.execute('SELECT * FROM ownership_admissions ORDER BY id').fetchall(),
                history=c.execute('SELECT * FROM ownership_history ORDER BY id').fetchall())

    def test_raw_discovery_plan_sealed_source_identity_no_original_mutation(self):
        before = self.originals(); scan_bytes = self.research.read_bytes()
        result = self.freeze(); plan = result['plan']
        self.assertEqual(plan['discovery_revision_hash'],digest(self.scan))
        self.assertEqual(plan['F'],sorted(self.fixture.h.f['accounts']))
        self.assertEqual(plan['U'][:6],module.canonical_accounts(self.scan['mint']))
        self.assertEqual([plan['U'][i] for i in plan['ownership_indices']],
                         [self.scan['mint']]+plan['F'])
        self.assertEqual(result['state'],'READY_GENESIS'); self.assertEqual(self.used(),3)
        self.assertEqual(self.originals(),before); self.assertEqual(self.research.read_bytes(),scan_bytes)
        with self.store.connect() as c:
            self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name IN ('ownership_banks','ownership_heads')").fetchone())
        self.assertEqual(self.create(),result)
        for key in ('eligible_for_trading','ownership_approval','chain_authenticated'):
            self.assertIs(result[key],False)

    def test_atomic_charge_intent_and_terminal_no_retry_new_id_or_refund(self):
        self.freeze(); original = self.originals(); event = self.reserve()
        self.assertEqual(event['used_after'],4); self.assertEqual(self.used(),4)
        self.assertEqual(self.resume()['state'],'TERMINAL_UNCERTAINTY')
        self.assertEqual(self.resume()['intent'],event)
        with self.assertRaisesRegex(JournalBlocked,'TERMINAL_UNCERTAINTY'): self.reserve()
        with self.journal.locked() as session:
            with self.assertRaisesRegex(JournalBlocked,'BUDGET_ALREADY_BOUND'):
                session.create_run('multi','replacement')
        self.assertEqual(self.used(),4); self.assertEqual(self.originals(),original)

    def test_insert_failure_rolls_back_both_charge_and_intent(self):
        self.freeze()
        def fail(*args): raise JournalBlocked('injected failure after increment')
        with patch.object(module._Session,'_intent',side_effect=fail):
            with self.assertRaisesRegex(JournalBlocked,'injected'): self.reserve()
        self.assertEqual(self.used(),3); self.assertEqual(self.resume()['state'],'READY_GENESIS')

    def test_real_process_death_before_commit_rolls_back_both(self):
        self.freeze(); ctx = multiprocessing.get_context('spawn')
        child = ctx.Process(target=_child,args=(self.research,self.path,self.fence,'before_commit'))
        child.start(); child.join(15); self.addCleanup(lambda: child.kill() if child.is_alive() else None)
        self.assertEqual(child.exitcode,78)
        self.assertEqual(self.used(),3); self.assertEqual(self.resume()['state'],'READY_GENESIS')
        self.assertEqual(self.reserve()['used_after'],4)

    def test_real_process_death_after_commit_remains_charged_terminal(self):
        self.freeze(); ctx = multiprocessing.get_context('spawn')
        child = ctx.Process(target=_child,args=(self.research,self.path,self.fence,'after_commit'))
        child.start(); child.join(15); self.addCleanup(lambda: child.kill() if child.is_alive() else None)
        self.assertEqual(child.exitcode,77)
        self.assertEqual(self.used(),4); self.assertEqual(self.resume()['state'],'TERMINAL_UNCERTAINTY')
        with self.assertRaisesRegex(JournalBlocked,'TERMINAL_UNCERTAINTY'): self.reserve()

    def test_process_concurrency_charges_exactly_one_intent(self):
        self.freeze(); ctx = multiprocessing.get_context('spawn'); queue = ctx.Queue(); go = ctx.Event()
        children = [ctx.Process(target=_child,args=(self.research,self.path,self.fence,'race',queue,go)) for _ in range(2)]
        for child in children: child.start()
        go.set()
        for child in children:
            child.join(15); self.addCleanup(lambda child=child: child.kill() if child.is_alive() else None)
            self.assertEqual(child.exitcode,0)
        results = [queue.get(timeout=3) for _ in children]; queue.close(); queue.join_thread()
        self.assertEqual(sum(r[0]=='OK' for r in results),1,results)
        self.assertEqual(self.used(),4)
        with self.store.connect() as c: self.assertEqual(c.execute('SELECT count(*) FROM common_bank_events').fetchone()[0],1)

    def test_stale_fence_wrong_stage_request_and_bad_time_never_charge(self):
        self.freeze()
        with self.assertRaisesRegex(JournalBlocked,'STALE_FENCE'): self.reserve('f'*64)
        with self.journal.locked() as session:
            for stage,method,params,at in [('slot','getSlot',[],121),('genesis','getGenesisHash',[1],121),
                                         ('genesis','getGenesisHash',[],True)]:
                with self.assertRaises(JournalBlocked):
                    session.reserve_stage('capture',stage,method,params,fence=self.fence,reserved_at=at)
        self.assertEqual(self.used(),3)

    def test_existing_counter_exhaustion_and_lower_bound_no_new_budget(self):
        self.freeze()
        for _ in range(15): self.assertTrue(self.progress.reserve('multi'))
        self.assertFalse(self.progress.reserve('multi'))
        with self.assertRaisesRegex(JournalBlocked,'LOWER_BOUND_EXCEEDS_18'): self.reserve()
        self.assertEqual(self.used(),18); self.assertEqual(self.resume()['state'],'READY_GENESIS')
        with self.store.connect() as c: self.assertEqual(c.execute('SELECT count(*) FROM ownership_budgets').fetchone()[0],1)

    def test_prepared_legacy_changed_source_and_fixed_bank_block_without_charge(self):
        with self.store.connect() as c: c.execute("UPDATE ownership_admissions SET state='PREPARED' WHERE id='multi'")
        with self.assertRaisesRegex(JournalBlocked,'SEALED_18'): self.create()
        with self.store.connect() as c: c.execute("UPDATE ownership_admissions SET state='SEALED' WHERE id='multi'")
        self.freeze()
        with sqlite3.connect(self.research) as c: c.execute("UPDATE scans SET status='FAILED'")
        with self.assertRaisesRegex(JournalBlocked,'EXACT_SEALED_SOURCE'): self.reserve()
        with sqlite3.connect(self.research) as c: c.execute("UPDATE scans SET status='COMPLETE'")
        with self.store.connect() as c:
            c.execute('CREATE TABLE ownership_banks(budget TEXT PRIMARY KEY,snapshot_hash TEXT NOT NULL,clock_hash TEXT)')
            c.execute("INSERT INTO ownership_banks VALUES('multi',?,NULL)",('a'*64,))
        with self.assertRaisesRegex(JournalBlocked,'BANK_ALREADY_FIXED'): self.reserve()
        self.assertEqual(self.used(),3)

    def test_immutable_replace_update_delete_and_rowid_aliases_fresh_connection(self):
        self.freeze(); self.reserve()
        with sqlite3.connect(self.path) as c:
            for table in ('common_bank_meta','common_bank_runs','common_bank_events'):
                with self.assertRaises(sqlite3.IntegrityError): c.execute(f'DELETE FROM {table}')
                column = dict(common_bank_meta='descriptor',common_bank_runs='plan_json',common_bank_events='event_json')[table]
                with self.assertRaises(sqlite3.IntegrityError): c.execute(f'UPDATE OR REPLACE {table} SET {column}={column}')
                for alias in ('rowid','oid','_rowid_'):
                    with self.assertRaises(sqlite3.OperationalError): c.execute(f'UPDATE OR REPLACE {table} SET {alias}=2')
                with self.assertRaises(sqlite3.IntegrityError): c.execute(f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}')
        self.assertEqual(self.used(),4); self.assertEqual(self.resume()['state'],'TERMINAL_UNCERTAINTY')

    def test_missing_schema_metadata_triggers_cannot_remigrate_or_charge(self):
        self.freeze()
        with self.store.connect() as c: c.execute('DROP TRIGGER common_bank_events_update')
        with self.assertRaisesRegex(JournalBlocked,'SCHEMA_OR_TRIGGER'): self.reserve()
        with self.store.connect() as c:
            c.execute(SCHEMA['common_bank_events_update']); c.execute('DROP TABLE common_bank_meta')
        with self.assertRaisesRegex(JournalBlocked,'SCHEMA_OR_TRIGGER'): self.create()
        with self.store.connect() as c:
            c.execute('DROP TABLE common_bank_events'); c.execute('DROP TABLE common_bank_runs')
            self.assertEqual(c.execute('PRAGMA application_id').fetchone()[0],APPLICATION_ID)
        with self.assertRaisesRegex(JournalBlocked,'SCHEMA_OR_TRIGGER'): self.create()
        self.assertEqual(self.used(),3)

    def test_expired_session_recursive_contention_and_hardlink_alias(self):
        with self.journal.locked() as session:
            with self.assertRaisesRegex(JournalBlocked,'BUSY'):
                with self.journal.locked(): pass
        with self.assertRaisesRegex(JournalBlocked,'SESSION_EXPIRED'): session.create_run('multi','capture')
        alias = self.root/'linked.sqlite'; os.link(self.path,alias)
        with self.assertRaises(ValueError): CommonBankJournal(self.research,alias,self.source)
        alias.unlink()

    def test_changed_plan_or_source_registry_reopen_has_no_fallback(self):
        self.freeze()
        changed = CommonBankJournal(self.research,self.path,ApprovedSource('other','synthetic_fixture'))
        with changed.locked() as session:
            with self.assertRaisesRegex(JournalBlocked,'METADATA'): session.resume_capture('multi')
        with self.store.connect() as c:
            c.execute('DROP TRIGGER common_bank_runs_update')
            plan = json.loads(c.execute('SELECT plan_json FROM common_bank_runs').fetchone()[0]); plan['U'].reverse()
            c.execute('UPDATE common_bank_runs SET plan_json=?',(canonical(plan),))
            c.execute(SCHEMA['common_bank_runs_update'])
        with self.assertRaisesRegex(JournalBlocked,'PLAN_OR_SEED'): self.reserve()
        self.assertEqual(self.used(),3)

    def test_unknown_triggers_on_counter_and_journal_are_rejected_before_charge(self):
        self.freeze()
        for sql,name in [
            ("CREATE TRIGGER external_counter AFTER UPDATE ON ownership_budgets BEGIN UPDATE ownership_budgets SET used=OLD.used WHERE id=NEW.id; END",'external_counter'),
            ("CREATE TRIGGER external_intent BEFORE INSERT ON common_bank_events BEGIN SELECT RAISE(ABORT,'hidden trigger'); END",'external_intent')]:
            with self.store.connect() as c: c.execute(sql)
            with self.assertRaises(JournalBlocked): self.reserve()
            self.assertEqual(self.used(),3)
            with self.store.connect() as c: c.execute('DROP TRIGGER '+name)
        self.assertEqual(self.reserve()['used_after'],4)

    def test_counter_rewind_below_committed_intent_is_not_accepted(self):
        self.freeze(); self.reserve()
        with self.store.connect() as c: c.execute("UPDATE ownership_budgets SET used=3 WHERE id='multi'")
        with self.assertRaisesRegex(JournalBlocked,'PENDING_INTENT_INVALID'): self.resume()

    def test_legacy_budget_is_never_converted_into_a_sealed_run(self):
        with self.store.connect() as c:
            c.execute("DELETE FROM ownership_admissions WHERE id='multi'")
            c.execute("UPDATE ownership_budgets SET source_hash=? WHERE id='multi'",(digest(self.scan),))
        with self.assertRaisesRegex(JournalBlocked,'SEALED_SOURCE_REQUIRED'): self.create()
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM ownership_admissions').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT used FROM ownership_budgets').fetchone()[0],3)
