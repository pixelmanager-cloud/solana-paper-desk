"""Synthetic clocks/HTTP, actual SQLite and subprocesses; never provider calls."""
from contextlib import closing
from email.message import Message
import io
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from desk import provider_pacing as p
from desk import paper_read_sources as reads
from desk import providers
from tests import test_paper_read_sources as read_fixtures
from tests.test_paper_read_sources import Response, KEY
from tests import test_monitoring_budget as monitor_fixtures


class Clock:
    def __init__(self):self.wall=100.;self.tick=0.;self.hook=None
    def time(self):return self.wall
    def monotonic(self):return self.tick
    def sleep(self,d):
        self.wall+=d;self.tick+=d
        if self.hook:
            fn,self.hook=self.hook,None;fn()


class SharedClock:
    """Two real processes share deterministic grant time, not scheduler latency."""
    def __init__(self,value):self.value=value
    def time(self):
        with self.value.get_lock():return self.value.value
    def sleep(self,seconds):
        with self.value.get_lock():self.value.value+=seconds


def race(path,start,out,clock):
    try:
        pacer=p.Pacer(path,clock=clock.time,monotonic=clock.time,sleep=clock.sleep)
        if not start.wait(5):raise AssertionError('Fixture start was not released')
        ticket=pacer.acquire('helius',timeout_seconds=2)
        at=clock.time()
        with sqlite3.connect(path) as c:
            due=c.execute("SELECT next_at FROM state WHERE provider='helius'").fetchone()[0]
        pacer.finish('helius',ticket)
        out.send(('OK',at,due))
    except Exception as error:out.send(('FAIL',type(error).__name__,getattr(error,'code',None)))
    finally:out.close()


def dies_after_grant(path,clock):
    p.Pacer(path,clock=clock.time,monotonic=clock.time,sleep=clock.sleep).acquire('helius',timeout_seconds=1)
    os._exit(19)


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'pace.sqlite';p.initialize(self.path,helius_seconds=.1,jupiter_seconds=.2,backoff_seconds=1)
        self.clock=Clock();self.pacer=self.make()
    def make(self,priority='investigation'):
        return p.Pacer(self.path,priority=priority,clock=self.clock.time,monotonic=self.clock.monotonic,sleep=self.clock.sleep)
    def slot(self,provider,*,timeout_seconds,priority='investigation'):
        pacer=self.make(priority);ticket=pacer.acquire(provider,timeout_seconds=timeout_seconds);pacer.finish(provider,ticket)
    def embargo(self,pacer,provider,headers):
        ticket=pacer.acquire(provider,timeout_seconds=1);pacer.throttle(provider,headers,ticket=ticket)
    def state(self,provider='helius'):
        with sqlite3.connect(self.path) as c:return c.execute('SELECT next_at,blocked_until,high_water FROM state WHERE provider=?',(provider,)).fetchone()
    def test_default_disabled_explicit_missing_and_no_implicit_init(self):
        with patch.dict(os.environ,{},clear=True):self.assertIsNone(p.configured())
        missing=Path(self.tmp.name)/'missing.sqlite'
        with patch.dict(os.environ,{p.ENV:str(missing)},clear=True):
            with self.assertRaises(p.PacingError):p.configured()
        self.assertFalse(missing.exists())
        with patch.dict(os.environ,{p.ENV:str(self.path)},clear=True):self.assertIsInstance(p.configured(priority='held'),p.Pacer)
        with self.assertRaises(FileExistsError):p.initialize(self.path)
    def test_shared_cadence_restart_no_refund_provider_independence(self):
        self.slot('helius',timeout_seconds=1)
        self.slot('jupiter',timeout_seconds=1)
        self.assertEqual(self.clock.tick,0)
        self.slot('helius',timeout_seconds=1)
        self.assertGreaterEqual(self.clock.tick,.1)
        self.assertAlmostEqual(self.state()[0],self.clock.wall+.1)
    def test_deadline_no_grant_and_monotonic_or_wall_rollback_refuse(self):
        self.slot('helius',timeout_seconds=1);before=self.state()[0]
        with self.assertRaisesRegex(p.PacingError,'DEADLINE'):self.slot('helius',timeout_seconds=.01)
        self.assertEqual(self.state()[0],before)
        self.clock.wall=99
        with self.assertRaisesRegex(p.PacingError,'CLOCK'):self.slot('jupiter',timeout_seconds=1)
        self.clock.wall=101;self.clock.tick=float('nan')
        with self.assertRaisesRegex(p.PacingError,'CLOCK'):self.slot('helius',timeout_seconds=1)
    def test_held_preempts_waiting_investigation_not_granted_slot(self):
        self.slot('helius',timeout_seconds=1)
        # Held request arrives after investigation began waiting. The already
        # granted first slot cannot be revoked; held gets the next due slot.
        def held():self.slot('helius',timeout_seconds=1,priority='held')
        self.clock.hook=held
        self.slot('helius',timeout_seconds=1)
        self.assertGreaterEqual(self.clock.tick,.2)
    def test_shared_backoff_retry_after_seconds_date_and_failures(self):
        for value in ('4','Thu, 01 Jan 1970 00:02:00 GMT'):
            headers=Message();headers['Retry-After']=value
            self.embargo(self.pacer,'helius',headers)
            self.clock.wall=104
        self.assertEqual(self.state()[1],120)
        with self.assertRaisesRegex(p.PacingError,'DEADLINE'):self.slot('helius',timeout_seconds=1,priority='held')
        self.slot('jupiter',timeout_seconds=1)
        self.clock.wall=120;self.slot('helius',timeout_seconds=1)
    def test_missing_malformed_duplicate_and_huge_retry_after(self):
        for i,values in enumerate(([],['nonsense'],['1','2'],['9'*100])):
            path=Path(self.tmp.name)/('independent-'+str(i)+'.sqlite');p.initialize(path,backoff_seconds=1)
            pacer=p.Pacer(path,clock=self.clock.time,monotonic=self.clock.monotonic,sleep=self.clock.sleep)
            headers=Message()
            for v in values:headers['Retry-After']=v
            self.embargo(pacer,'helius',headers)
            with sqlite3.connect(path) as c:until=c.execute("SELECT blocked_until FROM state WHERE provider='helius'").fetchone()[0]
            self.assertGreaterEqual(until,101)
        self.assertEqual(until,2**53-1)
    def test_queue_bounded_expired_crash_waiters_reclaimed(self):
        with sqlite3.connect(self.path) as c:
            for i in range(p.MAX_WAITERS):c.execute('INSERT INTO waiters VALUES(?,?,?,?,?)',(str(i).zfill(32),'helius','held',100.,105.))
        with self.assertRaisesRegex(p.PacingError,'QUEUE_FULL'):self.slot('helius',timeout_seconds=1)
        self.clock.wall=105;self.slot('helius',timeout_seconds=1)
        with sqlite3.connect(self.path) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM waiters').fetchone()[0],0)
    def test_corrupt_schema_alias_rotation_permissions_and_wal_refuse(self):
        alias=Path(self.tmp.name)/'alias';alias.symlink_to(self.path)
        with self.assertRaises(p.PacingError):p.Pacer(alias)
        os.chmod(self.path,0o644)
        with self.assertRaises(p.PacingError):self.make()
        os.chmod(self.path,0o600)
        old=self.path.with_suffix('.old');self.path.rename(old);p.initialize(self.path)
        with self.assertRaisesRegex(p.PacingError,'CHANGED'):self.pacer.acquire('helius',timeout_seconds=1)
        with sqlite3.connect(self.path) as c:c.execute('DROP TABLE waiters')
        with self.assertRaises(p.PacingError):self.make()
    def test_sqlite_contention_refuses_without_network_or_long_wait(self):
        with sqlite3.connect(self.path) as lock:
            lock.execute('BEGIN EXCLUSIVE')
            at=time.monotonic()
            with self.assertRaises(p.PacingError):self.slot('helius',timeout_seconds=1)
            self.assertLess(time.monotonic()-at,.5)
    def child_slot(self,ctx,clock):
        start=ctx.Event();reader,writer=ctx.Pipe(False)
        child=ctx.Process(target=race,args=(str(self.path),start,writer,clock))
        child.start();writer.close();start.set()
        try:
            self.assertTrue(reader.poll(5),'Fixture child produced no result')
            result=reader.recv();child.join(5);self.assertEqual(child.exitcode,0)
            return result
        finally:
            reader.close()
            if child.is_alive():child.kill();child.join(2)

    def test_multiprocess_overlap_refuses_exact_pending_code_without_changing_grant(self):
        ctx=multiprocessing.get_context('spawn');clock=SharedClock(ctx.Value('d',100))
        owner=p.Pacer(self.path,clock=clock.time,monotonic=clock.time,sleep=clock.sleep)
        ticket=owner.acquire('helius',timeout_seconds=1)
        def state():
            with sqlite3.connect(self.path) as c:
                return c.execute("SELECT next_at,blocked_until,high_water,pending FROM state WHERE provider='helius'").fetchone()
        before=state();self.assertEqual(before[3],ticket)
        # First owner cannot acknowledge until child has actually refused. This
        # is the schedule that the old 'both OK' assertion incorrectly rejected.
        result=self.child_slot(ctx,clock)
        self.assertEqual(result,('FAIL','PacingError','PACING_OUTCOME_PENDING'))
        self.assertEqual(state(),before)
        owner.finish('helius',ticket)
        self.assertIsNone(state()[3])
        # A NEW explicit invocation after matching acknowledgment may acquire;
        # the helper never retries a refused invocation or clears another guard.
        following=self.child_slot(ctx,clock)
        self.assertEqual(following[0],'OK',following)
        self.assertGreaterEqual(following[1],before[0])
        self.assertGreaterEqual(following[2]-before[0],.1)

    def test_multiprocess_acknowledged_slot_spacing_and_restart_death(self):
        ctx=multiprocessing.get_context('spawn');clock=SharedClock(ctx.Value('d',100))
        # Serial acknowledgments distinguish cadence from unresolved-outcome
        # refusal; no assumption that simultaneous requests both succeed.
        first=self.child_slot(ctx,clock);second=self.child_slot(ctx,clock)
        self.assertEqual(first[0],'OK',first);self.assertEqual(second[0],'OK',second)
        self.assertGreaterEqual(second[1],first[2])
        self.assertGreaterEqual(second[2]-first[2],.1)
        child=ctx.Process(target=dies_after_grant,args=(str(self.path),clock))
        child.start()
        try:
            child.join(5);self.assertEqual(child.exitcode,19)
            with sqlite3.connect(self.path) as c:
                pending=c.execute("SELECT pending FROM state WHERE provider='helius'").fetchone()[0]
            self.assertIsNotNone(pending)
            with self.assertRaisesRegex(p.PacingError,'OUTCOME_PENDING'):
                p.Pacer(self.path,clock=clock.time,monotonic=clock.time,sleep=clock.sleep).acquire('helius',timeout_seconds=1)
            with sqlite3.connect(self.path) as c:
                self.assertEqual(c.execute("SELECT pending FROM state WHERE provider='helius'").fetchone()[0],pending)
        finally:
            if child.is_alive():child.kill();child.join(2)


class ConnectorTests(unittest.TestCase):
    setUp=PacingTests.setUp
    make=PacingTests.make
    state=PacingTests.state
    embargo=PacingTests.embargo
    def factory(self,*,priority='investigation'):
        self.priorities.append(priority);return self.make(priority)
    def test_actual_collector_429_shared_backoff_no_retry_and_exact_charges(self):
        f=read_fixtures.PaperReadTests();f.setUp();self.addCleanup(f.doCleanups);self.priorities=[]
        headers=Message();headers['Retry-After']='4'
        error=HTTPError('https://SYNTHETIC_SECRET_INVALID',429,'SYNTHETIC_SECRET',headers,io.BytesIO(b'private'))
        with patch.object(p,'configured',side_effect=self.factory):
            with self.assertRaises(reads.PaperReadError) as caught:f.call(opened_error=error)
            record=f.outcome(caught.exception)
            self.assertEqual(record['http_status'],429);self.assertEqual(record['requests_used'],1)
            self.assertNotIn('SECRET',str(caught.exception));self.assertEqual(self.state()[1],104)
            with patch.object(reads,'build_opener') as opened,patch.object(reads.os.environ,'get') as credentials:
                with self.assertRaises(reads.PaperReadError) as blocked:
                    f.source.rpc('getAccountInfo',f.params,timeout_seconds=.01)
            opened.assert_not_called();credentials.assert_not_called()
            self.assertEqual(blocked.exception.code,'PACING_DEADLINE_EXCEEDED')
            record=f.outcome(blocked.exception);self.assertEqual(record['requests_used'],2)
            self.assertEqual(f.progress.admission('scan')['requests_used'],2)
        self.assertEqual(self.priorities,['investigation','investigation'])
    def test_actual_jupiter_quote_and_price_share_cadence(self):
        f=read_fixtures.PaperReadTests();f.setUp();self.addCleanup(f.doCleanups);self.priorities=[]
        with patch.object(p,'configured',side_effect=self.factory):
            f.call(Response(reads.canonical(f.quote).encode()),kind='quote')
            first=self.state('jupiter')[0]
            f.call(Response(reads.canonical({providers.SOL:{'usdPrice':100,'blockId':1,'decimals':9}}).encode()),kind='price')
        self.assertGreaterEqual(self.clock.wall,first);self.assertEqual(f.progress.admission('scan')['requests_used'],2)
    def test_actual_monitoring_uses_held_priority_failures_charged_no_investigation_change(self):
        f=monitor_fixtures.MonitoringBudgetTests();f.setUp();self.addCleanup(f.doCleanups);self.priorities=[]
        original=f.progress.admission(f.scan);ledger=list(f.f.ledger.db.iterdump())
        with patch.object(p,'configured',side_effect=self.factory):
            f.read()
            headers=Message();headers['Retry-After']='20';self.embargo(self.pacer,'helius',headers)
            with patch.object(reads,'build_opener') as opened,patch.object(reads.os.environ,'get') as credentials:
                with self.assertRaises(reads.PaperReadError) as blocked:f.read_unmocked()
            opened.assert_not_called();credentials.assert_not_called()
        self.assertEqual(self.priorities,['held','held']);self.assertEqual(f.budget.snapshot()['total_used'],2)
        self.assertEqual(f.progress.admission(f.scan),original);self.assertEqual(list(f.f.ledger.db.iterdump()),ledger)
        record=f.store.load(blocked.exception.evidence_hash);self.assertEqual(record['http_status'],None)
        self.assertEqual(record['monitoring_reservation']['total_used'],2)
    def test_actual_ownership_acquisition_legacy_transport_three_charges(self):
        from tests.test_paper_cycle import PaperCycleTests
        from desk.ownership_acquisition import acquire
        from desk.model import canonical
        c=PaperCycleTests();c.setUp();self.addCleanup(c.doCleanups);self.priorities=[];opened=[]
        class Body(io.BytesIO):
            status=200;headers=Message()
        class Opener:
            def open(inner,request,*,timeout):
                self.assertLessEqual(timeout,15);call=__import__('json').loads(request.data);opened.append(call)
                self.assertEqual(c.f.progress.admission(c.target.scan_id)['requests_used'],len(opened))
                if call['method']=='getAccountInfo':result={'value':c.f.protocol.rpc('getMultipleAccounts',[])['value'][6]}
                elif call['method']=='getSlot':result=120
                else:self.assertEqual(call['method'],'getTransactionsForAddress');result={'data':[]}
                return Body(canonical({'jsonrpc':'2.0','id':1,'result':result}).encode())
        with patch.object(p,'configured',side_effect=self.factory),patch.object(providers,'api_key',return_value=KEY),patch.object(providers,'build_opener',return_value=Opener()):
            result=acquire(c.f.jobs.path,c.f.progress.store.path,providers.helius_rpc,scan_id=c.target.scan_id)
        self.assertEqual(result['requests_used'],3);self.assertEqual(len(opened),3)
        self.assertEqual(self.priorities,['investigation']*3)
    def test_legacy429_shares_backoff_and_no_redirect_or_retry(self):
        headers=Message();headers['Retry-After']='3';self.priorities=[]
        error=HTTPError('https://SYNTHETIC_SECRET_INVALID',429,'SYNTHETIC_SECRET',headers,io.BytesIO())
        with patch.object(p,'configured',side_effect=self.factory),patch.object(providers,'build_opener') as opened:
            opened.return_value.open.side_effect=error
            with self.assertRaises(ValueError) as caught:providers.fetch_json('https://mainnet.helius-rpc.com/?api-key=SYNTHETIC')
            self.assertEqual(opened.return_value.open.call_count,1)
            self.assertNotIn('SECRET',str(caught.exception))
            from desk.coordinator_rpc import _NoRedirect
            self.assertIsInstance(opened.call_args.args[0],_NoRedirect)
            self.assertEqual(self.state()[1],103)
    def test_returned_429_and503_retry_after_without_hidden_requests(self):
        for status in (429,503):
            f=read_fixtures.PaperReadTests();f.setUp();self.addCleanup(f.doCleanups);self.priorities=[]
            response=Response(b'SYNTHETIC_IGNORED_ERROR_BODY',[('Retry-After','7')]);response.status=status
            with patch.object(p,'configured',side_effect=self.factory):
                with self.assertRaises(reads.PaperReadError) as caught:f.call(response)
            self.assertEqual(f.outcome(caught.exception)['http_status'],status)
            self.assertGreaterEqual(self.state()[1],self.clock.wall+7)
            self.assertEqual(f.progress.admission('scan')['requests_used'],1)
            self.clock.wall+=8
    def test_received429_sqlite_contention_leaves_shared_guard_and_exact_charges(self):
        f=read_fixtures.PaperReadTests();f.setUp();self.addCleanup(f.doCleanups);self.priorities=[]
        headers=Message();headers['Retry-After']='120'
        lock=sqlite3.connect(self.path);self.addCleanup(lock.close)
        class Opener:
            def open(inner,request,*,timeout):
                lock.execute('BEGIN IMMEDIATE')
                raise HTTPError('https://SYNTHETIC_INVALID',429,'fixture',headers,io.BytesIO())
        with patch.object(p,'configured',side_effect=self.factory),patch.object(reads,'build_opener',return_value=Opener()) as opened,patch.object(reads.os.environ,'get',return_value=KEY):
            with self.assertRaises(reads.PaperReadError) as failed:f.source.rpc('getAccountInfo',f.params,timeout_seconds=1)
        self.assertEqual(opened.call_count,1)
        record=f.outcome(failed.exception);self.assertEqual(record['http_status'],429)
        self.assertEqual(record['failure_code'],'PACING_DATABASE_BUSY');self.assertEqual(record['requests_used'],1)
        lock.rollback();self.clock.wall=100.15
        with sqlite3.connect(self.path) as c:
            until,pending=c.execute("SELECT blocked_until,pending FROM state WHERE provider='helius'").fetchone()
        self.assertEqual(until,0);self.assertIsNotNone(pending)
        # New process-equivalent constructor, even long after unknown embargo,
        # cannot erase an unresolved outcome or issue another HTTP request.
        for now in (100.15,230):
            self.clock.wall=now
            with patch.object(p,'configured',side_effect=self.factory),patch.object(reads,'build_opener') as reopened,patch.object(reads.os.environ,'get') as credentials:
                with self.assertRaises(reads.PaperReadError) as blocked:f.source.rpc('getAccountInfo',f.params,timeout_seconds=1)
            self.assertEqual(blocked.exception.code,'PACING_OUTCOME_PENDING')
            reopened.assert_not_called();credentials.assert_not_called()
        self.assertEqual(f.progress.admission('scan')['requests_used'],3)
        # Other provider still operates; exact held/investigation guards are per provider.
        other=self.make();ticket=other.acquire('jupiter',timeout_seconds=1);other.finish('jupiter',ticket)
    def test_pacer_failed_throttle_and_failed_ack_restart_never_clear_unknown_outcome(self):
        ticket=self.pacer.acquire('helius',timeout_seconds=1)
        headers=Message();headers['Retry-After']='120'
        with sqlite3.connect(self.path) as lock:
            lock.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(p.PacingError,'DATABASE_BUSY'):self.pacer.throttle('helius',headers,ticket=ticket)
        self.clock.wall=100.15
        with self.assertRaisesRegex(p.PacingError,'OUTCOME_PENDING'):self.make().acquire('helius',timeout_seconds=1)
        with self.assertRaisesRegex(p.PacingError,'GRANT_MISMATCH'):self.make().finish('helius','0'*32)
        # Matching grant can complete backoff atomically; a lost normal ACK must
        # also retain pending rather than imply the response was non-throttled.
        self.make().throttle('helius',headers,ticket=ticket)
        self.assertGreaterEqual(self.state()[1],220.15)
        self.clock.wall=230;next_ticket=self.make().acquire('helius',timeout_seconds=1)
        with sqlite3.connect(self.path) as lock:
            lock.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(p.PacingError,'DATABASE_BUSY'):self.make().finish('helius',next_ticket)
        with self.assertRaisesRegex(p.PacingError,'OUTCOME_PENDING'):self.make().acquire('helius',timeout_seconds=1)
    def test_legacy_http429_contention_guard_no_unpaced_followup(self):
        self.priorities=[];headers=Message();headers['Retry-After']='120'
        lock=sqlite3.connect(self.path);self.addCleanup(lock.close)
        class Opener:
            def open(inner,request,*,timeout):
                lock.execute('BEGIN IMMEDIATE')
                raise HTTPError('https://SYNTHETIC_INVALID',429,'fixture',headers,io.BytesIO())
        with patch.object(p,'configured',side_effect=self.factory),patch.object(providers,'build_opener',return_value=Opener()) as opened:
            with self.assertRaisesRegex(p.PacingError,'DATABASE_BUSY'):providers.fetch_json('https://mainnet.helius-rpc.com/')
        self.assertEqual(opened.call_count,1);lock.rollback();self.clock.wall=100.15
        with patch.object(p,'configured',side_effect=self.factory),patch.object(providers,'build_opener') as reopened:
            with self.assertRaisesRegex(p.PacingError,'OUTCOME_PENDING'):providers.fetch_json('https://mainnet.helius-rpc.com/')
        reopened.assert_not_called()
    def test_old_schema_refused_without_reset_or_migration(self):
        with sqlite3.connect(self.path) as c:c.execute('UPDATE policy SET version=1')
        before=self.path.read_bytes()
        with self.assertRaisesRegex(p.PacingError,'DATABASE_INVALID'):self.make()
        self.assertEqual(self.path.read_bytes(),before)
    def test_legacy_interrupted_outcome_does_not_acknowledge_guard(self):
        self.priorities=[]
        with patch.object(p,'configured',side_effect=self.factory),patch.object(providers,'build_opener') as opened:
            opened.return_value.open.side_effect=KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):providers.fetch_json('https://mainnet.helius-rpc.com/')
        self.clock.wall=200
        with self.assertRaisesRegex(p.PacingError,'OUTCOME_PENDING'):self.make().acquire('helius',timeout_seconds=1)
