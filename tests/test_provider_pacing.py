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


def race(path,barrier,out):
    try:
        pacer=p.Pacer(path);barrier.wait(5)
        pacer.acquire('helius',timeout_seconds=2)
        out.send(('OK',time.time()))
    except Exception as error:out.send(('FAIL',type(error).__name__))
    finally:out.close()


def dies_after_grant(path):
    p.Pacer(path).acquire('helius',timeout_seconds=1)
    os._exit(19)


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'pace.sqlite';p.initialize(self.path,helius_seconds=.1,jupiter_seconds=.2,backoff_seconds=1)
        self.clock=Clock();self.pacer=self.make()
    def make(self,priority='investigation'):
        return p.Pacer(self.path,priority=priority,clock=self.clock.time,monotonic=self.clock.monotonic,sleep=self.clock.sleep)
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
        self.pacer.acquire('helius',timeout_seconds=1)
        self.make().acquire('jupiter',timeout_seconds=1)
        self.assertEqual(self.clock.tick,0)
        self.make().acquire('helius',timeout_seconds=1)
        self.assertGreaterEqual(self.clock.tick,.1)
        self.assertAlmostEqual(self.state()[0],self.clock.wall+.1)
    def test_deadline_no_grant_and_monotonic_or_wall_rollback_refuse(self):
        self.pacer.acquire('helius',timeout_seconds=1);before=self.state()[0]
        with self.assertRaisesRegex(p.PacingError,'DEADLINE'):self.make().acquire('helius',timeout_seconds=.01)
        self.assertEqual(self.state()[0],before)
        self.clock.wall=99
        with self.assertRaisesRegex(p.PacingError,'CLOCK'):self.make().acquire('jupiter',timeout_seconds=1)
        self.clock.wall=101;self.clock.tick=float('nan')
        with self.assertRaisesRegex(p.PacingError,'CLOCK'):self.make().acquire('helius',timeout_seconds=1)
    def test_held_preempts_waiting_investigation_not_granted_slot(self):
        self.pacer.acquire('helius',timeout_seconds=1)
        # Held request arrives after investigation began waiting. The already
        # granted first slot cannot be revoked; held gets the next due slot.
        def held():self.make('held').acquire('helius',timeout_seconds=1)
        self.clock.hook=held
        self.make().acquire('helius',timeout_seconds=1)
        self.assertGreaterEqual(self.clock.tick,.2)
    def test_shared_backoff_retry_after_seconds_date_and_failures(self):
        for value in ('4','Thu, 01 Jan 1970 00:02:00 GMT'):
            headers=Message();headers['Retry-After']=value
            self.pacer.throttle('helius',headers)
        self.assertEqual(self.state()[1],120)
        with self.assertRaisesRegex(p.PacingError,'DEADLINE'):self.make('held').acquire('helius',timeout_seconds=1)
        self.make().acquire('jupiter',timeout_seconds=1)
        self.clock.wall=120;self.make().acquire('helius',timeout_seconds=1)
    def test_missing_malformed_duplicate_and_huge_retry_after(self):
        for values in ([],['nonsense'],['1','2'],['9'*100]):
            headers=Message()
            for v in values:headers['Retry-After']=v
            self.pacer.throttle('helius',headers)
            self.assertGreaterEqual(self.state()[1],101)
        self.assertEqual(self.state()[1],2**53-1)
    def test_queue_bounded_expired_crash_waiters_reclaimed(self):
        with sqlite3.connect(self.path) as c:
            for i in range(p.MAX_WAITERS):c.execute('INSERT INTO waiters VALUES(?,?,?,?,?)',(str(i).zfill(32),'helius','held',100.,105.))
        with self.assertRaisesRegex(p.PacingError,'QUEUE_FULL'):self.make().acquire('helius',timeout_seconds=1)
        self.clock.wall=105;self.make().acquire('helius',timeout_seconds=1)
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
            with self.assertRaises(p.PacingError):self.pacer.acquire('helius',timeout_seconds=1)
            self.assertLess(time.monotonic()-at,.5)
    def test_multiprocess_slot_race_actual_spacing_and_restart_death(self):
        ctx=multiprocessing.get_context('spawn');barrier=ctx.Barrier(2);children=[];readers=[]
        for _ in range(2):
            reader,writer=ctx.Pipe(False);child=ctx.Process(target=race,args=(str(self.path),barrier,writer));child.start();writer.close();readers.append(reader);children.append(child)
        try:
            results=[]
            for r in readers:self.assertTrue(r.poll(5));results.append(r.recv());r.close()
            for child in children:child.join(5);self.assertEqual(child.exitcode,0)
            self.assertTrue(all(r[0]=='OK' for r in results),results)
            times=sorted(r[1] for r in results);self.assertGreaterEqual(times[1]-times[0],.095)
            child=ctx.Process(target=dies_after_grant,args=(str(self.path),));children.append(child);child.start();child.join(5);self.assertEqual(child.exitcode,19)
            with sqlite3.connect(self.path) as c:due=c.execute("SELECT next_at FROM state WHERE provider='helius'").fetchone()[0]
            p.Pacer(self.path).acquire('helius',timeout_seconds=1)
            self.assertGreaterEqual(time.time(),due)
        finally:
            for child in children:
                if child.is_alive():child.kill();child.join(2)


class ConnectorTests(unittest.TestCase):
    setUp=PacingTests.setUp
    make=PacingTests.make
    state=PacingTests.state
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
            headers=Message();headers['Retry-After']='20';self.pacer.throttle('helius',headers)
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
