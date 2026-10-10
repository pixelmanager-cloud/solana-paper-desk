"""Actual paper checkpoint and durable reservations; transport is synthetic only."""
from dataclasses import replace
import json
import multiprocessing
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from desk import engine
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.job_persistence import JobPersistence, BIRTH_ACQUISITION_V1
from desk.model import canonical,digest
from desk.monitoring_budget import MonitoringBudget,MonitoringBlocked
from desk.paper_read_sources import PaperReadSources,PaperReadError,RPC_ID
from desk.providers import SOL
from tests.test_paper_read_sources import Response,KEY
from tests import test_quote_execution as fixtures
from tests.helpers import T


def _compete(evidence,ledger,cfg,scan,connection):
    store=EvidenceStore(evidence)
    progress=HistoryProgress(store)
    budget=MonitoringBudget(store,ledger,cfg,clock=lambda:T)
    try:
        receipt=budget.reserve_read(progress,scan,'getSlot',[{'commitment':'finalized'}])
        connection.send(('reserved',receipt['total_used']))
    except MonitoringBlocked as error:
        connection.send(('blocked',error.code))
    finally:connection.close()


class MonitoringBudgetTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.QuoteExecutionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.root=Path(self.f.tmp.name)
        self.jobs=JobPersistence(self.root/'research.sqlite')
        self.store=EvidenceStore(self.root/'evidence.sqlite')
        self.progress=HistoryProgress(self.store)
        from tests.test_pools import PoolTests
        from desk.pools import verify_pool
        from desk.quote_execution import output_raw
        self.protocol=PoolTests();self.protocol.setUp()
        self.f.mint=str(self.protocol.mint);self.f.pool=str(self.protocol.pool)
        self.f.buy=self.f.quote('buy',10_000_000,1_000_000)
        self.f.raw=output_raw(self.f.buy,self.f.cfg)
        self.f.exit=self.f.quote('sell',self.f.raw,10_000_000)
        refs=[]
        def capture(payload):
            key=self.store.save(payload);refs.append(key);return key
        verify_pool(self.f.pool,self.f.mint,self.protocol.rpc,capture=capture)
        self.pool_refs=refs
        self.scan=self.admit(self.f.mint)
        e=self.f.market(paper_source_evidence={'scan_id':self.scan,'collector_refs':refs,'pool_hash':refs[0]})
        self.f.apply(e,(self.f.buy,self.f.exit))
        self.now=T
        self.budget=MonitoringBudget(self.store,self.f.path,self.f.cfg,clock=lambda:self.now)
        self.budget.provision()

    def admit(self,mint):
        with patch('desk.job_persistence.time.time',return_value=T):
            scan=self.jobs.admit(mint,kind=BIRTH_ACQUISITION_V1,evidence_db=self.store.path)
        descriptor=self.jobs.descriptor(scan)
        self.progress.admit(scan,{'kind':'ownership_admission_v1','scan_id':scan,'mint':mint,'created':descriptor['admitted_at']})
        return scan

    def read(self,*,scan=None,budget=None,body=None,fail=False):
        scan=scan or self.scan;source=PaperReadSources(self.progress,scan,monitoring_budget=budget or self.budget)
        outer=self
        class Opener:
            def open(self,request,*,timeout):
                with outer.store.connect() as c:
                    total=c.execute('SELECT total FROM paper_monitoring_budget').fetchone()[0]
                    outer.assertEqual(c.execute('SELECT count(*) FROM paper_monitoring_reservations').fetchone()[0],total)
                outer.assertEqual(outer.progress.admission(scan)['requests_used'],0)
                if fail:raise ConnectionResetError('SYNTHETIC_SECRET_SENTINEL')  # a real transient failure; a bare OSError is unclassified and latches (T25)
                return Response(body or canonical({'jsonrpc':'2.0','id':RPC_ID,'result':100}).encode())
        with patch('desk.paper_read_sources.os.environ.get',return_value=KEY),patch('desk.paper_read_sources.build_opener',return_value=Opener()):
            return source.rpc_with_evidence('getSlot',[{'commitment':'finalized'}],timeout_seconds=3)

    def accounting(self):
        with self.store.connect() as c:return c.execute('SELECT high_water,total,blocked FROM paper_monitoring_budget').fetchone()

    def test_sixty_shared_original_receipts_then_zero_io_and_no_investigation_reset(self):
        original=self.progress.admission(self.scan)
        refs=[]
        for count in range(1,61):
            _,key=self.read();refs.append(key);r=self.store.load(key)
            self.assertEqual(r['monitoring_reservation']['total_used'],count)
            self.assertEqual(r['monitoring_reservation']['window_used'],count)
            self.assertEqual(r['requests_used'],0)
        with patch('desk.paper_read_sources.os.environ.get') as credentials,patch('desk.paper_read_sources.build_opener') as opener:
            with self.assertRaises(PaperReadError) as caught:self.read_unmocked()
        self.assertEqual(caught.exception.code,'MONITORING_REQUEST_BUDGET_EXHAUSTED');credentials.assert_not_called();opener.assert_not_called()
        self.assertEqual(self.progress.admission(self.scan),original)
        for ref in refs:self.assertEqual(self.store.load(ref)['failure_code'],None)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM paper_monitoring_outcomes').fetchone()[0],60)

    def read_unmocked(self):
        return PaperReadSources(self.progress,self.scan,monitoring_budget=self.budget).rpc('getSlot',[{'commitment':'finalized'}],timeout_seconds=3)

    def test_multiple_held_positions_share_global_cap(self):
        old_mint=self.f.mint
        self.f.apply(self.f.market(T+61),(self.f.quote('sell',self.f.raw,10_000_000,at=T+61),))
        from desk.security import base58
        other=base58(bytes([31])*32);self.f.mint=other
        scan=self.admit(other)
        buy=self.f.quote('buy',10_000_000,1_000_000,at=T+62);raw=self.f.raw
        sell=self.f.quote('sell',raw,10_000_000,at=T+62)
        out=self.f.apply(self.f.market(T+62,paper_source_evidence={'scan_id':scan,'collector_refs':[]}),(buy,sell))
        self.assertTrue(any(row['type']=='fill' for row in out));self.now=T+62
        self.f.mint=old_mint
        for i in range(60):self.read(scan=self.scan if i%2 else scan)
        with self.assertRaises(PaperReadError) as caught:self.read(scan=scan)
        self.assertEqual(caught.exception.code,'MONITORING_REQUEST_BUDGET_EXHAUSTED')
        self.assertEqual(self.accounting()[1],60)
        self.assertEqual(self.progress.admission(scan)['requests_used'],0)

    def test_exact_rolling_boundary_preserves_all_records_and_restart_counter(self):
        for _ in range(60):self.read()
        self.now=T+3599
        with self.assertRaises(PaperReadError):self.read()
        self.now=T+3600
        reopened=MonitoringBudget(self.store,self.f.path,self.f.cfg,clock=lambda:self.now)
        _,key=self.read(budget=reopened)
        self.assertEqual(self.store.load(key)['monitoring_reservation']['window_used'],1)
        self.assertEqual(self.accounting()[1],61)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM paper_monitoring_reservations').fetchone()[0],61)

    def test_failure_charged_original_retained_and_restart_latched(self):
        # T08: this test used a transient TRANSPORT_ERROR (OSError) as its sample failure.
        # Transient provider-availability failures no longer latch the shared allowance
        # (see the next test), so the latch is now asserted with a non-transient failure:
        # a well-formed HTTP 200 whose body is not the expected JSON-RPC envelope.
        # The latch assertions themselves are unchanged.
        with self.assertRaises(PaperReadError) as caught:self.read(body=b'{"unexpected":"envelope"}')
        row=self.store.load(caught.exception.evidence_hash)
        self.assertEqual(row['failure_code'],'RESPONSE_INVALID')
        self.assertEqual(self.accounting()[1:],(1,'SOURCE_FAILURE'))
        self.budget=MonitoringBudget(self.store,self.f.path,self.f.cfg,clock=lambda:T+3600)
        with self.assertRaises(PaperReadError) as caught:self.read_unmocked()
        self.assertEqual(caught.exception.code,'MONITORING_RECOVERY_REQUIRED');self.assertEqual(self.accounting()[1],1)

    def test_transient_failure_charged_original_retained_and_not_latched(self):
        with self.assertRaises(PaperReadError) as caught:self.read(fail=True)
        row=self.store.load(caught.exception.evidence_hash)
        self.assertEqual(row['failure_code'],'TRANSPORT_ERROR');self.assertNotIn('SYNTHETIC_SECRET_SENTINEL',canonical(row))
        self.assertEqual(self.accounting()[1:],(1,None))
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM paper_monitoring_outcomes').fetchone()[0],1)
        self.budget=MonitoringBudget(self.store,self.f.path,self.f.cfg,clock=lambda:T+3600)
        _,key=self.read()                                    # the next read is allowed and charged
        self.assertEqual(self.store.load(key)['monitoring_reservation']['total_used'],2)
        self.assertEqual(self.accounting()[1:],(2,None))

    def test_lost_completion_pending_across_restart(self):
        with patch('desk.paper_read_sources.os.environ.get',return_value=KEY),patch('desk.paper_read_sources.build_opener') as opener:
            opener.return_value.open.side_effect=KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):self.read_unmocked()
        # T25: an orphan older than ABANDON_AFTER_SECONDS is resolved as ABANDONED_CHARGED (see
        # tests/test_monitoring_classification.py); within the deadline it must still fail closed.
        self.budget=MonitoringBudget(self.store,self.f.path,self.f.cfg,clock=lambda:T+60)
        with patch('desk.paper_read_sources.os.environ.get') as credentials:
            with self.assertRaises(PaperReadError) as caught:self.read_unmocked()
        credentials.assert_not_called();self.assertEqual(caught.exception.code,'MONITORING_OUTCOME_PENDING');self.assertEqual(self.accounting()[1],1)

    def test_clock_rollback_persists_recovery_even_after_clock_recovers(self):
        self.read();self.now=T-1
        with self.assertRaises(PaperReadError) as caught:self.read_unmocked()
        self.assertEqual(caught.exception.code,'MONITORING_CLOCK_ROLLBACK')
        self.budget=MonitoringBudget(self.store,self.f.path,self.f.cfg,clock=lambda:T+3600)
        with self.assertRaises(PaperReadError) as caught:self.read_unmocked()
        self.assertEqual(caught.exception.code,'MONITORING_RECOVERY_REQUIRED');self.assertEqual(self.accounting()[1],1)

    def test_subsecond_rollback_survives_restart_without_rounding(self):
        self.now=T+0.75;self.read()
        self.assertEqual(self.accounting()[0],T+0.75)
        self.budget=MonitoringBudget(self.store,self.f.path,self.f.cfg,clock=lambda:T+0.5)
        with patch('desk.paper_read_sources.os.environ.get') as credentials:
            with self.assertRaises(PaperReadError) as caught:self.read_unmocked()
        credentials.assert_not_called();self.assertEqual(caught.exception.code,'MONITORING_CLOCK_ROLLBACK')
        self.assertEqual(self.accounting()[1],1)

    def test_candidate_buy_history_other_identity_and_oversize_refused_before_io(self):
        from desk.security import base58
        candidate=self.admit(base58(bytes([32])*32))
        source=PaperReadSources(self.progress,self.scan,monitoring_budget=self.budget)
        cases=[lambda:self.read(scan=candidate),lambda:source.quote(SOL,self.f.mint,10,self.f.wallet,timeout_seconds=3),
               lambda:source.quote(self.f.mint,SOL,self.f.raw+1,self.f.wallet,timeout_seconds=3),
               lambda:source.quote(self.f.mint,SOL,1,SOL,timeout_seconds=3),
               lambda:source.rpc('getAccountInfo',[SOL,{'encoding':'base64','commitment':'confirmed'}],timeout_seconds=3),
               lambda:self.budget.reserve_read(self.progress,self.scan,'getTransactionsForAddress',[])]
        with patch('desk.paper_read_sources.os.environ.get') as credentials,patch('desk.paper_read_sources.build_opener') as opener:
            for invoke in cases:
                with self.subTest(invoke=invoke),self.assertRaises(ValueError):invoke()
        credentials.assert_not_called();opener.assert_not_called();self.assertEqual(self.accounting()[1],0)

    def test_closed_position_and_corrupt_checkpoint_refuse_no_mutation(self):
        state=self.f.state();state['positions'][self.f.mint]['qty']='999999'
        self.f.ledger.db.execute('UPDATE state SET payload=? WHERE id=1',(canonical(state),))
        before=list(self.f.ledger.db.iterdump())
        with self.assertRaises(PaperReadError):self.read_unmocked()
        self.assertEqual(list(self.f.ledger.db.iterdump()),before);self.assertEqual(self.accounting()[1],0)

    def test_actual_full_exit_removes_monitoring_authority(self):
        self.f.apply(self.f.market(T+1,danger=True),(self.f.quote('sell',self.f.raw,9_500_000,at=T+1),))
        self.assertEqual(self.f.state()['positions'],{})
        with self.assertRaises(PaperReadError) as caught:self.read_unmocked()
        self.assertEqual(caught.exception.code,'MONITORING_OPEN_POSITION_REQUIRED');self.assertEqual(self.accounting()[1],0)

    def reentered_v3(self):
        from tests.test_quote_execution_v3_seam import QuoteV3SeamTests
        case=QuoteV3SeamTests();case.setUp();self.addCleanup(case.doCleanups)
        f=case.fixture;f.cfg=case.cfg
        root=Path(f.tmp.name);jobs=JobPersistence(root/'research.sqlite')
        store=EvidenceStore(root/'evidence.sqlite');progress=HistoryProgress(store)
        with patch('desk.job_persistence.time.time',return_value=T):
            scan=jobs.admit(f.mint,kind=BIRTH_ACQUISITION_V1,evidence_db=store.path)
        progress.admit(scan,{'kind':'ownership_admission_v1','scan_id':scan,'mint':f.mint,'created':T})
        first=case.market();first['paper_source_evidence']={'scan_id':scan,'collector_refs':[]}
        self.assertTrue(any(r['type']=='fill' for r in f.apply(first,(f.buy,f.exit))))
        budget=MonitoringBudget(store,f.path,f.cfg,clock=lambda:T);budget.provision()
        source=PaperReadSources(progress,scan,monitoring_budget=budget)
        with patch('desk.paper_read_sources.os.environ.get',return_value=KEY),patch('desk.paper_read_sources.build_opener') as opener:
            opener.return_value.open.return_value=Response(canonical({'jsonrpc':'2.0','id':RPC_ID,'result':100}).encode())
            source.rpc('getSlot',[{'commitment':'finalized'}],timeout_seconds=3)
        close=case.market(T+1);close['danger']=True
        self.assertTrue(any(r['type']=='fill' and r['side']=='sell' for r in f.apply(close,(f.quote('sell',f.raw,10_000_000,at=T+1),))))
        self.assertEqual(f.state()['positions'],{})
        at=f.state()['cooldowns'][f.mint]+1
        second=case.market(at);second['paper_source_evidence']={'scan_id':scan,'collector_refs':[]}
        self.assertTrue(any(r['type']=='fill' for r in f.apply(second,(f.quote('buy',10_000_000,1_000_000,at=at),f.quote('sell',f.raw,10_000_000,at=at)))))
        return f,progress,scan,first,second,at

    def test_actual_v3_buy_full_close_cooldown_rebuy_monitoring_binds_current_entry(self):
        f,progress,scan,first,second,at=self.reentered_v3()
        self.assertEqual(f.state()['positions'][f.mint]['entry_event_id'],second['event_id'])
        self.assertNotEqual(first['event_id'],second['event_id'])
        original=list(f.ledger.db.iterdump());admission=progress.admission(scan)
        budget=MonitoringBudget(progress.store,f.path,f.cfg,clock=lambda:at)
        with patch('desk.paper_read_sources.os.environ.get',return_value=KEY),patch('desk.paper_read_sources.build_opener') as opener:
            opener.return_value.open.return_value=Response(canonical({'jsonrpc':'2.0','id':RPC_ID,'result':100}).encode())
            PaperReadSources(progress,scan,monitoring_budget=budget).rpc('getSlot',[{'commitment':'finalized'}],timeout_seconds=3)
        self.assertEqual(budget.snapshot()['total_used'],2)
        self.assertEqual(progress.admission(scan),admission);self.assertEqual(list(f.ledger.db.iterdump()),original)

    def test_reentry_cannot_select_closed_entry_or_waive_historical_corruption(self):
        f,progress,scan,first,second,at=self.reentered_v3()
        state=f.state();state['positions'][f.mint]['entry_event_id']=first['event_id']
        f.ledger.db.execute('UPDATE state SET payload=? WHERE id=1',(canonical(state),))
        source=PaperReadSources(progress,scan,monitoring_budget=MonitoringBudget(progress.store,f.path,f.cfg,clock=lambda:at))
        with patch('desk.paper_read_sources.os.environ.get') as credential:
            with self.assertRaises(PaperReadError):source.rpc('getSlot',[{'commitment':'finalized'}],timeout_seconds=3)
        credential.assert_not_called()
        state['positions'][f.mint]['entry_event_id']=second['event_id']
        f.ledger.db.execute('UPDATE state SET payload=? WHERE id=1',(canonical(state),))
        row=f.ledger.db.execute("SELECT seq,payload FROM outcomes WHERE event_id=? AND json_extract(payload,'$.side')='buy'",(first['event_id'],)).fetchone()
        corrupted=json.loads(row[1]);corrupted['quote_execution']['quote_hash']='0'*64
        f.ledger.db.execute('UPDATE outcomes SET payload=? WHERE seq=?',(canonical(corrupted),row[0]))
        before=list(f.ledger.db.iterdump())
        with patch('desk.paper_read_sources.os.environ.get') as credential:
            with self.assertRaises(PaperReadError):source.rpc('getSlot',[{'commitment':'finalized'}],timeout_seconds=3)
        credential.assert_not_called();self.assertEqual(list(f.ledger.db.iterdump()),before)

    def test_exhausted_investigation_still_unchanged_and_default_transport_rejects(self):
        for _ in range(18):self.assertTrue(self.progress.reserve(self.scan))
        original=self.progress.admission(self.scan)
        with patch('desk.paper_read_sources.os.environ.get',return_value=KEY),patch('desk.paper_read_sources.build_opener') as opener:
            opener.return_value.open.return_value=Response(canonical({'jsonrpc':'2.0','id':RPC_ID,'result':100}).encode())
            source=PaperReadSources(self.progress,self.scan,monitoring_budget=self.budget)
            source.rpc('getSlot',[{'commitment':'finalized'}],timeout_seconds=3)
        self.assertEqual(self.progress.admission(self.scan),original)
        with self.assertRaises(PaperReadError) as caught:PaperReadSources(self.progress,self.scan).rpc('getSlot',[{'commitment':'finalized'}],timeout_seconds=3)
        self.assertEqual(caught.exception.code,'BUDGET_EXHAUSTED')

    def test_unprovisioned_or_rebound_database_not_adopted(self):
        with self.store.connect() as c:c.execute('UPDATE paper_monitoring_budget SET cap=61')
        before=self.accounting()
        with self.assertRaises(PaperReadError):self.read_unmocked()
        self.assertEqual(self.accounting(),before)
        with self.assertRaises(MonitoringBlocked):self.budget.provision()

    def test_original_reservations_and_outcomes_immutable(self):
        self.read()
        with self.store.connect() as c:
            for sql in ('DELETE FROM paper_monitoring_reservations','UPDATE paper_monitoring_reservations SET at=0','DELETE FROM paper_monitoring_outcomes'):
                with self.assertRaises(sqlite3.IntegrityError):c.execute(sql)

    def test_fresh_connection_replace_and_rowid_aliases_preserve_full_budget(self):
        for _ in range(60):self.read()
        with sqlite3.connect(self.store.path) as c:
            self.assertEqual(c.execute('PRAGMA recursive_triggers').fetchone()[0],0)
            before=list(c.iterdump())
            statements=[
                'INSERT OR REPLACE INTO paper_monitoring_reservations SELECT id,at-3600,scan_id,mint,checkpoint_hash,method,params_hash FROM paper_monitoring_reservations',
                "INSERT OR REPLACE INTO paper_monitoring_outcomes SELECT reservation_id,'changed' FROM paper_monitoring_outcomes"]
            for alias in ('id','rowid','oid','_rowid_'):
                statements.append(f'INSERT OR REPLACE INTO paper_monitoring_reservations({alias},at,scan_id,mint,checkpoint_hash,method,params_hash) SELECT id,at-3600,scan_id,mint,checkpoint_hash,method,params_hash FROM paper_monitoring_reservations WHERE id=1')
            for alias in ('reservation_id','rowid','oid','_rowid_'):
                statements.append(f"INSERT OR REPLACE INTO paper_monitoring_outcomes({alias},evidence_hash) VALUES(1,'changed')")
            statements.extend([
                'INSERT INTO paper_monitoring_reservations SELECT * FROM paper_monitoring_reservations WHERE 1 ON CONFLICT(id) DO UPDATE SET at=0',
                "INSERT INTO paper_monitoring_outcomes VALUES(1,'changed') ON CONFLICT(reservation_id) DO UPDATE SET evidence_hash='changed'",
                'UPDATE OR REPLACE paper_monitoring_reservations SET id=1 WHERE id=2'])
            for statement in statements:
                with self.subTest(statement=statement),self.assertRaises(sqlite3.IntegrityError):c.execute(statement)
                self.assertEqual(list(c.iterdump()),before)
        self.assertEqual(self.budget.snapshot()['window_used'],60)
        with patch('desk.paper_read_sources.os.environ.get') as credentials:
            with self.assertRaises(PaperReadError) as caught:self.read_unmocked()
        credentials.assert_not_called();self.assertEqual(caught.exception.code,'MONITORING_REQUEST_BUDGET_EXHAUSTED')
        self.assertEqual(self.accounting()[1],60)

    def test_corrupted_restored_time_and_outcome_refuse_without_repair(self):
        self.read()
        # Simulate an offline corrupted restore, bypassing the live connection's
        # mutation guard only in this fixture. Source pages remain original.
        with sqlite3.connect(self.store.path) as c:
            c.execute('DROP TRIGGER paper_monitoring_reservations_update')
            c.execute('UPDATE paper_monitoring_reservations SET at=at-3600')
        before=self.accounting()
        with patch('desk.paper_read_sources.os.environ.get') as credentials:
            with self.assertRaises(PaperReadError) as caught:self.read_unmocked()
        credentials.assert_not_called();self.assertEqual(caught.exception.code,'MONITORING_ACCOUNTING_INVALID');self.assertEqual(self.accounting(),before)
        with self.assertRaises(MonitoringBlocked):self.budget.provision()

    def test_completion_replay_idempotent_without_replace(self):
        _,key=self.read();receipt=self.store.load(key)['monitoring_reservation']
        with self.store.connect() as c:before=list(c.iterdump())
        self.budget.retain_outcome(receipt,key)
        with self.store.connect() as c:self.assertEqual(list(c.iterdump()),before)

    def test_held_atomic_pool_and_sell_quantities_use_actual_transport(self):
        from desk.live_observation import ingest_mint,ingest_quote,ProviderObservation
        source=PaperReadSources(self.progress,self.scan,monitoring_budget=self.budget)
        pool=self.store.load(self.pool_refs[0]);params=pool['params']
        calls=[]
        class Opener:
            def open(inner,request,*,timeout):
                from urllib.parse import urlsplit,parse_qs
                calls.append(request)
                if request.method=='POST':
                    call=json.loads(request.data);result=self.protocol.rpc(call['method'],call['params'])
                    raw=canonical({'jsonrpc':'2.0','id':RPC_ID,'result':result}).encode()
                else:
                    query={k:v[0] for k,v in parse_qs(urlsplit(request.full_url).query).items()}
                    raw=canonical({'inputMint':query['inputMint'],'outputMint':query['outputMint'],'inAmount':query['amount'],
                        'outAmount':'10000000','otherAmountThreshold':'9900000','swapMode':'ExactIn','slippageBps':100,
                        'routePlan':[{'percent':100,'swapInfo':{'ammKey':self.f.pool,'inputMint':self.f.mint,'outputMint':SOL,
                            'inAmount':query['amount'],'outAmount':'10000000'}}]}).encode()
                return Response(raw)
        with patch('desk.paper_read_sources.os.environ.get',return_value=KEY),patch('desk.paper_read_sources.build_opener',return_value=Opener()):
            source.rpc('getAccountInfo',[self.f.pool,{'encoding':'base64','commitment':'confirmed'}],timeout_seconds=3)
            source.rpc('getMultipleAccounts',params,timeout_seconds=3)
            source.quote(self.f.mint,SOL,self.f.raw,self.f.wallet,timeout_seconds=3)
            source.quote(self.f.mint,SOL,self.f.raw*3//10,self.f.wallet,timeout_seconds=3)
        self.assertEqual(len(calls),4);self.assertEqual(self.budget.snapshot()['total_used'],4)
        self.assertEqual(self.progress.admission(self.scan)['requests_used'],0)
        invalid=[list(params[0]),dict(params[1])];invalid[0][0]=SOL
        with self.assertRaises(PaperReadError):source.rpc('getMultipleAccounts',invalid,timeout_seconds=3)
        self.assertEqual(self.budget.snapshot()['total_used'],4)

    def test_snapshot_readonly_and_expiration_uses_sequence_not_window_delta(self):
        self.read();before=list(self.store.connect().iterdump());first=self.budget.snapshot()
        self.assertEqual(first['total_used'],1);self.assertFalse(first['entries_enabled'])
        self.assertEqual(list(self.store.connect().iterdump()),before)
        self.now=T+3600;self.read();second=self.budget.snapshot()
        self.assertEqual(second['window_used'],first['window_used']);self.assertEqual(second['total_used'],2)

    def test_concurrent_processes_at_last_slot_cannot_overspend(self):
        for _ in range(59):self.read()
        context=multiprocessing.get_context('fork');children=[];readers=[]
        for _ in range(4):
            reader,writer=context.Pipe(duplex=False)
            process=context.Process(target=_compete,args=(str(self.store.path),str(self.f.path),self.f.cfg,self.scan,writer))
            process.start();writer.close();children.append(process);readers.append(reader)
        results=[]
        try:
            for reader in readers:
                self.assertTrue(reader.poll(15));results.append(reader.recv());reader.close()
        finally:
            for child in children:
                child.join(15)
                if child.is_alive():child.terminate();child.join()
                self.assertEqual(child.exitcode,0)
        self.assertEqual(sum(r[0]=='reserved' for r in results),1)
        self.assertEqual(self.accounting()[1],60)
        self.assertTrue(all(r[0]=='reserved' or r[1] in ('MONITORING_OUTCOME_PENDING','MONITORING_REQUEST_BUDGET_EXHAUSTED') for r in results))
