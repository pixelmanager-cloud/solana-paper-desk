"""Synthetic contexts and transports; genuine first-handoff fixture, no live I/O."""
import copy
import sqlite3
import unittest
from unittest.mock import patch

from desk import monitoring_handoff as handoff, monitoring_successor as successor, paper_cycle, runtime_compatibility as runtime
from desk.model import canonical,digest
from desk.monitoring_budget import MonitoringBudget,MonitoringBlocked
from tests import test_monitoring_handoff as fixtures
from tests.test_runtime_compatibility import dump
from tests.helpers import T


class MonitoringSuccessorTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.MonitoringHandoffTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.charge_original();self.first=self.f.activate()
        self.policy=self.f.root/'successor-policy.json'
        self.policy.write_text(canonical({'version':1,'successors':[]}))
        p=patch.object(successor,'POLICY',self.policy);p.start();self.addCleanup(p.stop)
        self.next=self.f.root/'profile-successor.sqlite'
        # Opaque test config delta; no new token profile semantics implemented here.
        self.cfg=self.f.cfg|{'synthetic_successor_version':1}
        paper_cycle.initialize(self.next,self.cfg)
        self.pin=self.plan(self.f.new,self.next,self.cfg,T+2)
        self.policy.write_text(canonical({'version':1,'successors':[self.pin]}))

    def plan(self,old,new,cfg,at):
        return successor.plan(self.f.research,self.f.evidence,old,new,self.f.pacing,cfg,
                              old_source=runtime.implementation_hash(),at=at)

    def activate(self,old=None,new=None,cfg=None,pin=None):
        return handoff.activate_successor(self.f.research,self.f.evidence,old or self.f.new,
              new or self.next,self.f.pacing,cfg or self.cfg,pins=pin or self.pin)

    def test_two_successive_edges_preserve_original_grants_usage_and_ledgers(self):
        original={p:dump(p) for p in (self.f.old,self.f.new,self.next,self.f.research,self.f.pacing)}
        with self.f.store.connect() as c:
            receipt=c.execute('SELECT * FROM '+handoff.TABLE).fetchall()
            budget=c.execute('SELECT * FROM paper_monitoring_budget').fetchall()
            reservation=c.execute('SELECT * FROM paper_monitoring_reservations').fetchall()
            outcomes=c.execute('SELECT * FROM paper_monitoring_outcomes').fetchall()
        first=self.activate()
        self.assertEqual(first['status'],'BOUND')
        with self.f.store.connect() as c:
            self.assertEqual(c.execute('SELECT * FROM '+handoff.TABLE).fetchall(),receipt)
            self.assertEqual(c.execute('SELECT * FROM paper_monitoring_budget').fetchall(),budget)
            self.assertEqual([r[:8] for r in c.execute('SELECT * FROM paper_monitoring_reservations')],reservation)
            self.assertEqual(c.execute('SELECT * FROM paper_monitoring_outcomes').fetchall(),outcomes)
        for path,expected in original.items():self.assertEqual(dump(path),expected)
        before=dump(self.f.evidence)
        self.assertEqual(self.activate()['status'],'ALREADY_BOUND');self.assertEqual(dump(self.f.evidence),before)
        next2=self.f.root/'second-successor.sqlite';cfg2=self.cfg|{'synthetic_successor_version':2}
        paper_cycle.initialize(next2,cfg2);pin2=self.plan(self.next,next2,cfg2,T+3)
        self.policy.write_text(canonical({'version':1,'successors':[self.pin,pin2]}))
        self.activate(self.next,next2,cfg2,pin2)
        active=MonitoringBudget(self.f.store,next2,cfg2,clock=lambda:T+3)
        self.assertEqual((active.snapshot()['total_used'],active.snapshot()['remaining']),(1,3599))
        for path,cfg in ((self.f.new,self.f.cfg),(self.next,self.cfg)):
            with self.assertRaises(MonitoringBlocked):MonitoringBudget(self.f.store,path,cfg).snapshot()
        with self.assertRaises(ValueError):self.activate() # Retired replay is not current authority.

    def test_reservation_fence_rejects_missing_retired_replace_and_current_partial(self):
        result=self.activate();before=dump(self.f.evidence)
        values=(2,T+3,'s','m','a'*64,'getSlot','b'*64,self.first['binding_hash'])
        with self.f.store.connect() as c:
            for statement,args in (
                ('INSERT INTO paper_monitoring_reservations(id,at,scan_id,mint,checkpoint_hash,method,params_hash,context_hash) VALUES(?,?,?,?,?,?,?,?)',values),
                ('INSERT OR REPLACE INTO paper_monitoring_reservations VALUES(?,?,?,?,?,?,?,?,?)',values+(self.first['binding_hash'],)),
                ('INSERT OR REPLACE INTO paper_monitoring_reservations VALUES(?,?,?,?,?,?,?,?,?)',(1,)+values[1:]+(result['binding_hash'],))):
                with self.assertRaises(sqlite3.Error):c.execute(statement,args)
        self.assertEqual(dump(self.f.evidence),before)

    def test_pins_tamper_partial_guards_and_rollback_are_nonmutating(self):
        before=dump(self.f.evidence)
        for key in ('parent_hash','old_source','new_source','old_config_hash','new_config_hash','old_checkpoint_hash','reservations_hash'):
            pin=copy.deepcopy(self.pin);pin[key]='0'*64
            with self.assertRaises(ValueError):self.activate(pin=pin)
            self.assertEqual(dump(self.f.evidence),before)
        with patch.object(MonitoringBudget,'_accounting',side_effect=MonitoringBlocked('synthetic crash')):
            with self.assertRaises(MonitoringBlocked):self.activate()
        self.assertEqual(dump(self.f.evidence),before)
        self.activate()
        with self.f.store.connect() as c:c.execute('DROP TRIGGER '+successor.FENCE)
        before=dump(self.f.evidence)
        with self.assertRaises(MonitoringBlocked):MonitoringBudget(self.f.store,self.next,self.cfg).snapshot()
        self.assertEqual(dump(self.f.evidence),before)

    def test_pending_monitoring_refuses_without_mutation(self):
        with self.f.store.connect() as c:
            c.execute('INSERT INTO paper_monitoring_reservations VALUES(?,?,?,?,?,?,?,?)',
                      (2,T+1,'pending','m','a'*64,'getSlot','b'*64,self.first['binding_hash']))
            c.execute('UPDATE paper_monitoring_budget SET total=2,high_water=?',(T+1,))
        before=dump(self.f.evidence)
        with self.assertRaises(ValueError):self.activate()
        self.assertEqual(dump(self.f.evidence),before)

    def test_bound_before_payload_fetch(self):
        self.activate()
        with self.f.store.connect() as c:
            c.execute('DROP TRIGGER '+successor.TABLE+'_update')
            c.execute('UPDATE '+successor.TABLE+' SET body=?',('x'*20000,))
            c.execute(successor.guards()[successor.TABLE+'_update'])
        with self.f.store.connect() as c:
            with patch.object(runtime,'_parse',side_effect=AssertionError('must not materialize')):
                with self.assertRaisesRegex(ValueError,'payload bounds'):successor.rows(c,({'unused':True},'a'*64))

    def test_shared_pacing_pending_refuses_and_blocked_latch_survives(self):
        from desk import provider_pacing
        pacer=provider_pacing.Pacer(self.f.pacing,clock=lambda:T+1)
        ticket=pacer.acquire('helius',timeout_seconds=1)
        before=dump(self.f.evidence)
        with self.assertRaises(ValueError):self.activate()
        self.assertEqual(dump(self.f.evidence),before)
        pacer.finish('helius',ticket)
        with self.f.store.connect() as c:c.execute("UPDATE paper_monitoring_budget SET blocked='CLOCK_ROLLBACK'")
        pin=self.plan(self.f.new,self.next,self.cfg,T+2)
        self.policy.write_text(canonical({'version':1,'successors':[pin]}))
        self.activate(pin=pin)
        result=MonitoringBudget(self.f.store,self.next,self.cfg,clock=lambda:T+2).snapshot()
        self.assertIn('MONITORING_RECOVERY_REQUIRED',result['blockers'])
        with self.f.store.connect() as c:self.assertEqual(c.execute('SELECT total,blocked FROM paper_monitoring_budget').fetchone(),(1,'CLOCK_ROLLBACK'))

    def test_actual_successor_reservation_retention_and_rolling_accounting(self):
        result=self.activate()
        # Genuine engine/checkpoint held-position creation in the NEW experiment.
        saved=(self.f.new,self.f.cfg)
        self.f.new=self.next;self.f.cfg=self.cfg
        try:target=self.f.buy_new()
        finally:self.f.new,self.f.cfg=saved
        active=MonitoringBudget(self.f.store,self.next,self.cfg,clock=lambda:T+3)
        receipt=active.reserve_read(self.f.f.context.progress,target.scan_id,'getSlot',[{'commitment':'finalized'}])
        self.assertEqual((receipt['id'],receipt['context_hash'],receipt['successor_context_hash']),
                         (2,self.first['binding_hash'],result['binding_hash']))
        record={'monitoring_reservation':receipt,'scan_id':target.scan_id,'method':'getSlot',
                'params':[{'commitment':'finalized'}],'failure_code':None}
        active.retain_outcome(receipt,self.f.store.save(record))
        self.assertEqual((active.snapshot()['total_used'],active.snapshot()['remaining']),(2,3598))
        with self.assertRaises(MonitoringBlocked):self.f.active.retain_outcome(receipt,digest(record))

    def test_both_certified_null_passes_replay_original_context_after_successor(self):
        from contextlib import ExitStack
        from dataclasses import replace
        from desk import paper_terminal_reconciliation as terminal, paper_read_sources as transport, provider_pacing
        from desk.ownership_acquisition import _Setup
        from tests import test_paper_terminal_reconciliation as terminal_fixtures
        t=terminal_fixtures.TerminalReconciliationTests();t.setUp();self.addCleanup(t.doCleanups)
        for key in t.h.item.graduation_refs:
            manifest=t.store.load(key)
            self.f.store.save(t.store.load(manifest['response_hash']))
            self.f.store.save(manifest)
        t.f=self.f.f.context;t.store=self.f.store;t.progress=t.f.progress
        t.ledger=self.f.new;t.cfg=self.f.cfg;t.target=self.f.target;t.pacing=self.f.pacing
        t.h.f=t.f;t.h.path=t.ledger;t.h.cfg=t.cfg;t.h.target=t.target
        t.h.item=replace(t.h.item,target=t.target,history_as_of=T+1)
        t.f.at=T+1;t.f.protocol.lp_supply=100
        def run_cycle(*,legacy=False,candidates=None):
            outer=t
            class Legacy(transport.PaperReadSources):
                def __init__(self,*args,**kw):super().__init__(*args,**kw);outer.sources.append(self)
            def factory(*args,**kw):
                value=transport.PaperReadSources(*args,**kw);outer.sources.append(value);return value
            with ExitStack() as stack:
                stack.enter_context(patch.dict(transport.os.environ,{'HELIUS_API_KEY':'SYNTHETIC_TEST_ONLY',provider_pacing.ENV:str(t.pacing)},clear=True))
                stack.enter_context(patch.object(transport,'build_opener',return_value=t.opener))
                stack.enter_context(patch.object(transport.time,'time',return_value=t.f.at))
                stack.enter_context(patch.object(transport.time,'monotonic',return_value=1))
                return t.h.run_cycle(source_factory=Legacy if legacy else factory,candidates=(t.h.item,) if candidates is None else candidates)
        t.run_cycle=run_cycle
        # Explicit legacy proof plus an intrinsically associated second rejection.
        t.legacy();t.approve();t.reconcile();first_scan=t.target.scan_id
        source=t.f.jobs.source(first_scan)
        report={'mint':t.target.mint,'eligible_for_trading':False,'calls':10};report['report_hash']=digest(report)
        source.update(status='COMPLETE',result=canonical(report))
        admission=t.progress.admission(first_scan)
        t.progress.prepare_source(first_scan,admission['descriptor_hash'],source)
        t.progress.seal_source(first_scan,admission['descriptor_hash'],digest(source))
        with t.f.jobs.connect() as c:c.execute("UPDATE scans SET status='COMPLETE',result=? WHERE id=?",(source['result'],first_scan))
        other=t.f.target()
        _Setup(t.store,t.f.jobs.descriptor(other.scan_id),t.progress.admission(other.scan_id))
        t.target=other;t.h.target=other;t.h.item=replace(t.h.item,target=other)
        result=t.run_cycle()
        self.assertIn('terminal_receipt_hash',result,result)
        with t.store.connect() as c:
            before=c.execute('SELECT id,intent_hash,outcome_hash FROM paper_observation_passes ORDER BY id').fetchall()
            self.assertEqual(len(before),2);self.assertTrue(all(row[2] is None for row in before))
        self.activate()
        self.assertEqual(terminal.gate(t.store,t.f.jobs.path,(first_scan,)),'REJECTED_SCAN_RETIRED')
        self.assertEqual(terminal.gate(t.store,t.f.jobs.path,(other.scan_id,)),'REJECTED_SCAN_RETIRED')
        self.assertIsNone(terminal.gate(t.store,t.f.jobs.path,('unrelated-candidate',)))
        with t.store.connect() as c:self.assertEqual(c.execute('SELECT id,intent_hash,outcome_hash FROM paper_observation_passes ORDER BY id').fetchall(),before)
        # A new unknown NULL record is never grandfathered through the journal.
        with t.store.connect() as c:
            c.execute('INSERT INTO paper_observation_passes(id,intent_hash,outcome_hash) VALUES(?,?,NULL)',('f'*32,'a'*64))
        self.assertEqual(terminal.gate(t.store,t.f.jobs.path,()),'OBSERVATION_RECOVERY_REQUIRED')

    def test_every_affected_lock_and_fork_gap_cycle_refuses_without_publication(self):
        import fcntl
        before=dump(self.f.evidence)
        for path in (str(self.f.research)+'.jobs-worker.lock',str(self.f.evidence)+'.ownership-invocation.lock',
                     str(self.f.old)+'.paper-cycle.lock',str(self.f.new)+'.paper-cycle.lock',str(self.next)+'.paper-cycle.lock'):
            with open(path,'a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaises(ValueError):self.activate()
            self.assertEqual(dump(self.f.evidence),before)
        for changes in ({'sequence':2},{'parent_hash':'0'*64},
                        {'context':self.pin['context']|{'new_ledger_db':str(self.f.old)}}):
            pin=copy.deepcopy(self.pin);pin.update(changes)
            self.policy.write_text(canonical({'version':1,'successors':[pin]}))
            with self.assertRaises(ValueError):self.activate(pin=pin)
            self.assertEqual(dump(self.f.evidence),before)
        self.policy.write_text(canonical({'version':1,'successors':[self.pin,self.pin]}))
        with self.assertRaises(ValueError):self.activate()
        self.assertEqual(dump(self.f.evidence),before)

    def test_publication_crash_after_insert_rolls_back_all_additive_ddl(self):
        before=dump(self.f.evidence)
        original=MonitoringBudget._accounting
        def crash(budget,c):
            if c.execute('SELECT 1 FROM sqlite_master WHERE name=?',(successor.TABLE,)).fetchone():
                raise RuntimeError('synthetic interruption after receipt and fence')
            return original(budget,c)
        with patch.object(MonitoringBudget,'_accounting',new=crash):
            with self.assertRaisesRegex(RuntimeError,'synthetic interruption'):self.activate()
        self.assertEqual(dump(self.f.evidence),before)
        self.assertEqual(self.activate()['status'],'BOUND')
