"""SYNTHETIC_TEST_ONLY: no provider access, credentials or live acceptance."""
import asyncio
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from discovery import continuous as d
from desk.model import canonical
from tests.test_graduation_witness import fixture


def notification(signature='fixture',slot=10):
    return canonical({'method':'transactionNotification','params':{'result':{'signature':signature,'slot':slot}}}).encode()


def killed_receive(path):
    with d.worker(path):
        store=d.Store(path,clock=lambda:100)
        store.reserve('RECEIVE')
        os._exit(17)


def killed_committed(path):
    with d.worker(path):
        store=d.Store(path,clock=lambda:100)
        identity=store.reserve('RECEIVE');store.complete(identity,payload=notification())
        os._exit(19)


def competing(path,queue):
    try:
        with d.worker(path):queue.put('UNEXPECTED')
    except d.Blocked as error:queue.put(error.code)


@unittest.skipUnless(os.name=='posix','exclusive Linux/Unix worker lock')
class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'discovery.sqlite';self.now=100.0
        d.initialize(self.path,hour_bytes=800000,hour_records=20,storage_bytes=64*1024**2)
        self.store=d.Store(self.path,clock=lambda:self.now);self.addCleanup(self.store.close)

    def receive(self,raw):
        identity=self.store.reserve('RECEIVE')
        return self.store.complete(identity,payload=raw)

    def test_actual_original_dedup_conflict_bytes_and_decoder_intake(self):
        raw,mint,pool=fixture();signature=raw['transaction']['signatures'][0]
        payload={'method':'transactionNotification','params':{'result':{'signature':signature,'slot':raw['slot'],'blockTime':raw['blockTime'],
                 'transaction':{'transaction':raw['transaction'],'meta':raw['meta']}}}}
        wire=canonical(payload).encode()
        self.assertEqual(self.receive(wire),'RECEIVED');self.assertEqual(self.receive(wire),'DUPLICATE')
        altered=json.loads(wire);altered['params']['result']['slot']+=1
        self.assertEqual(self.receive(canonical(altered).encode()),'CONFLICT')
        self.assertEqual(self.store.status()['hour_records'],1)
        self.assertEqual(self.store.c.execute('SELECT payload FROM frames WHERE id=1').fetchone()[0],wire)
        # Existing raw decoder consumes this schema without migration or repair.
        from desk.decode import decode_capture
        output=Path(self.tmp.name)/'decoded.jsonl'
        result=decode_capture(self.path,output)
        self.assertEqual(result['counts']['raw_records'],1)
        from desk.decode import decode
        saved=self.store.c.execute('SELECT payload FROM raw_events').fetchone()[0]
        decoded=decode(json.loads(saved))
        self.assertEqual(decoded['slot'],raw['slot'])
        self.assertTrue(any(ix.get('name')=='migrate' for ix in decoded['program_observations']))

    def test_twenty_stored_or_byte_cap_survive_restart(self):
        for i in range(20):self.receive(notification(str(i)))
        with self.assertRaisesRegex(d.Blocked,'TRAFFIC'):self.store.reserve('RECEIVE')
        reopened=d.Store(self.path,clock=lambda:self.now)
        try:
            reopened.recover()
            with self.assertRaisesRegex(d.Blocked,'TRAFFIC'):reopened.reserve('RECEIVE')
            self.assertEqual(reopened.status()['hour_records'],20)
        finally:reopened.close()

    def test_failed_receive_full_charges_byte_limit_and_no_reset(self):
        for _ in range(4):
            identity=self.store.reserve('RECEIVE');self.store.complete(identity,code='SOURCE_FAILURE')
        self.assertEqual(self.store.status()['hour_bytes'],800000)
        with self.assertRaisesRegex(d.Blocked,'TRAFFIC'):self.store.reserve('RECEIVE')
        self.now+=3599
        with self.assertRaisesRegex(d.Blocked,'TRAFFIC'):self.store.reserve('RECEIVE')
        self.now+=1
        self.assertIsInstance(self.store.reserve('RECEIVE'),int)

    def test_process_death_before_receive_charged_at_restart(self):
        ctx=multiprocessing.get_context('fork');p=ctx.Process(target=killed_receive,args=(str(self.path),));p.start();p.join(5)
        self.assertEqual(p.exitcode,17);self.now=200
        with d.worker(self.path):self.store.recover()
        self.assertEqual(self.store.status()['hour_bytes'],200000)
        self.assertEqual(self.store.status()['hour_records'],1)
        self.assertEqual(self.store.c.execute('SELECT code,at FROM completions').fetchone(),('INTERRUPTED',200.0))
        with d.worker(self.path):self.store.recover()
        self.assertEqual(self.store.status()['hour_bytes'],200000)

    def test_plain_connection_replace_identity_aliases_rejected(self):
        self.receive(notification())
        with sqlite3.connect(self.path) as c:
            original=list(c.iterdump())
            for alias in ('id','rowid','oid','_rowid_'):
                with self.assertRaises(sqlite3.IntegrityError):
                    c.execute(f'INSERT OR REPLACE INTO reservations({alias},at,kind) VALUES(1,0,"RECEIVE")')
                c.rollback();self.assertEqual(list(c.iterdump()),original)
            with self.assertRaises(sqlite3.IntegrityError):c.execute('UPDATE OR REPLACE reservations SET rowid=2')
            c.rollback()
            with self.assertRaises(sqlite3.IntegrityError):c.execute('INSERT OR REPLACE INTO policies VALUES(1,0,100000000,5000,5368709120)')

    def test_immutable_policy_change_preserves_used_and_originals(self):
        self.receive(notification());before=self.store.status()['hour_bytes']
        original=self.store.c.execute('SELECT payload FROM frames').fetchone()[0]
        with d.worker(self.path):self.store.configure(hour_bytes=100000000,hour_records=5000,storage_bytes=5368709120)
        status=self.store.status();self.assertEqual(status['hour_bytes'],before)
        self.assertEqual(status['day_byte_cap'],2400000000)
        self.assertEqual(status['day_record_cap'],120000)
        self.assertEqual(self.store.c.execute('SELECT count(*) FROM policies').fetchone()[0],2)
        self.assertEqual(self.store.c.execute('SELECT payload FROM frames').fetchone()[0],original)
        with self.assertRaises(d.Blocked):self.store.configure(hour_bytes=100000001,hour_records=5000,storage_bytes=5368709120)

    def test_malformed_duplicate_keys_and_oversize_no_prefix_acceptance(self):
        for raw in (b'null',b'{"id":1,"id":2}',b'NaN',b'\xff'):
            self.assertEqual(self.receive(raw),'MALFORMED')
        identity=self.store.reserve('RECEIVE');self.assertEqual(self.store.complete(identity,payload=b'x'*(d.MESSAGE+1)),'OVERSIZE')
        self.assertEqual(self.store.c.execute('SELECT count(*) FROM raw_events').fetchone()[0],0)
        self.assertEqual(self.store.c.execute('SELECT count(*) FROM frames').fetchone()[0],4)
        self.assertGreaterEqual(self.store.status()['hour_bytes'],d.MESSAGE)

    def test_clock_nan_subsecond_rollback_block_without_writes(self):
        self.now=100.9;self.receive(notification());before=list(self.store.c.iterdump())
        for invalid in (100.8,float('nan'),float('inf'),True):
            self.now=invalid
            with self.assertRaises(d.Blocked):self.store.reserve('CONNECT')
            self.assertEqual(list(self.store.c.iterdump()),before)

    def test_single_worker_and_aliases(self):
        ctx=multiprocessing.get_context('fork');q=ctx.Queue()
        with d.worker(self.path):
            p=ctx.Process(target=competing,args=(str(self.path),q));p.start();p.join(5)
            self.assertEqual(q.get(timeout=2),'DISCOVERY_WORKER_BUSY')
        alias=Path(self.tmp.name)/'alias';alias.symlink_to(self.path)
        with self.assertRaises(d.Blocked):d.Store(alias)
        hard=Path(self.tmp.name)/'hard';os.link(self.path,hard)
        with self.assertRaises(d.Blocked):d.Store(hard)

    def test_storage_sidecars_and_wal_mode_stop_no_pruning(self):
        self.receive(notification());before=list(self.store.c.iterdump())
        sidecar=Path(str(self.path)+'-wal')
        with sidecar.open('wb') as stream:stream.truncate(64*1024**2)
        with self.assertRaisesRegex(d.Blocked,'STORAGE'):self.store.reserve('CONNECT')
        self.assertEqual(list(self.store.c.iterdump()),before);sidecar.unlink()
        other=Path(self.tmp.name)/'wal.sqlite';d.initialize(other)
        with sqlite3.connect(other) as c:c.execute('PRAGMA journal_mode=WAL')
        with self.assertRaisesRegex(d.Blocked,'STORAGE_MODE'):d.Store(other)

    def test_missing_invalid_existing_database_never_initialized(self):
        missing=Path(self.tmp.name)/'missing'
        with self.assertRaises(d.Blocked):d.Store(missing)
        self.assertFalse(missing.exists())
        invalid=Path(self.tmp.name)/'invalid'
        with sqlite3.connect(invalid) as c:c.execute('CREATE TABLE original(value TEXT)')
        before=invalid.read_bytes()
        with self.assertRaises(sqlite3.Error):d.Store(invalid)
        self.assertEqual(invalid.read_bytes(),before)

    def test_reconnect_attempt_limit_survives_restart(self):
        for _ in range(12):
            identity=self.store.reserve('CONNECT');self.store.complete(identity,code='CONNECT_FAILURE')
        with self.assertRaisesRegex(d.Blocked,'RECONNECT'):self.store.reserve('CONNECT')
        reopened=d.Store(self.path,clock=lambda:self.now)
        try:
            with self.assertRaisesRegex(d.Blocked,'RECONNECT'):reopened.reserve('CONNECT')
        finally:reopened.close()

    def test_commit_failure_keeps_reservation_and_rolls_back_raw(self):
        identity=self.store.reserve('RECEIVE');actual=self.store.c
        class FailedCommit:
            def __getattr__(self,name):return getattr(actual,name)
            def commit(self):raise sqlite3.OperationalError('SYNTHETIC_TEST_ONLY disk failure')
        self.store.c=FailedCommit()
        with self.assertRaises(sqlite3.Error):self.store.complete(identity,payload=notification())
        self.store.c=actual
        self.assertEqual(actual.execute('SELECT count(*) FROM raw_events').fetchone()[0],0)
        self.assertEqual(self.store.status()['hour_bytes'],d.MESSAGE)
        with d.worker(self.path):self.store.recover()
        self.assertEqual(self.store.status()['hour_bytes'],d.MESSAGE)

    def test_process_death_after_commit_preserves_original_without_double_charge(self):
        ctx=multiprocessing.get_context('fork');p=ctx.Process(target=killed_committed,args=(str(self.path),));p.start();p.join(5)
        self.assertEqual(p.exitcode,19)
        before=self.store.status()['hour_bytes']
        with d.worker(self.path):self.store.recover()
        self.assertEqual(self.store.status()['hour_bytes'],before)
        self.assertEqual(self.store.c.execute('SELECT count(*) FROM raw_events').fetchone()[0],1)

    def test_partial_restore_retained_raw_cannot_reset_charged_accounting(self):
        self.receive(notification())
        original=list(self.store.c.iterdump())
        restored=Path(self.tmp.name)/'partial.sqlite'
        # Simulated incomplete restore, not automatic repair or production deletion.
        script='\n'.join(line for line in original if not line.startswith(('INSERT INTO "reservations"','INSERT INTO "completions"','INSERT INTO "frames"')))
        with sqlite3.connect(restored) as c:c.executescript(script)
        before=restored.read_bytes()
        with self.assertRaisesRegex(d.Blocked,'ORIGINAL|ACCOUNTING'):d.Store(restored,clock=lambda:101)
        self.assertEqual(restored.read_bytes(),before)
        self.assertEqual(self.store.status()['hour_bytes'],len(notification()))

    def test_restored_raw_timestamp_cannot_claim_freshness_against_original_receipt(self):
        self.receive(notification());original=list(self.store.c.iterdump())
        restored=Path(self.tmp.name)/'partial-time.sqlite'
        script='\n'.join(line.replace(',100.0,',',101.0,') if line.startswith('INSERT INTO "raw_events"') else line for line in original)
        with sqlite3.connect(restored) as c:c.executescript(script)
        before=restored.read_bytes()
        with self.assertRaisesRegex(d.Blocked,'ORIGINAL|ACCOUNTING'):d.Store(restored,clock=lambda:101)
        self.assertEqual(restored.read_bytes(),before)

    def test_failed_attempt_partial_restore_cannot_erase_charge_with_metadata_retained(self):
        identity=self.store.reserve('RECEIVE');self.store.complete(identity,code='SOURCE_FAILURE')
        original=list(self.store.c.iterdump());restored=Path(self.tmp.name)/'partial-failure.sqlite'
        script='\n'.join(line for line in original if not line.startswith(('INSERT INTO "reservations"','INSERT INTO "completions"')))
        with sqlite3.connect(restored) as c:c.executescript(script)
        before=restored.read_bytes()
        with self.assertRaisesRegex(d.Blocked,'ACCOUNTING'):d.Store(restored,clock=lambda:101)
        self.assertEqual(restored.read_bytes(),before)

    def test_completed_duplicate_cannot_survive_missing_original_as_healthy(self):
        self.receive(notification());self.receive(notification())
        original=list(self.store.c.iterdump());restored=Path(self.tmp.name)/'partial-duplicate.sqlite'
        script='\n'.join(line for line in original if not line.startswith('INSERT INTO "raw_events"'))
        with sqlite3.connect(restored) as c:c.executescript(script)
        before=restored.read_bytes()
        with self.assertRaisesRegex(d.Blocked,'ORIGINAL'):d.Store(restored,clock=lambda:101)
        self.assertEqual(restored.read_bytes(),before)

    def test_partial_restore_missing_raw_never_reconstructs_from_frame(self):
        self.receive(notification());original=list(self.store.c.iterdump())
        restored=Path(self.tmp.name)/'partial-raw.sqlite'
        script='\n'.join(line for line in original if not line.startswith('INSERT INTO "raw_events"'))
        with sqlite3.connect(restored) as c:c.executescript(script)
        before=restored.read_bytes()
        with self.assertRaisesRegex(d.Blocked,'ORIGINAL|ACCOUNTING'):d.Store(restored,clock=lambda:101)
        self.assertEqual(restored.read_bytes(),before)

    def test_ambiguous_old_connect_recovered_attempt_remains_charged_current_hour(self):
        identity=self.store.reserve('CONNECT');self.now+=3601
        with d.worker(self.path):self.store.recover()
        self.assertEqual(self.store.status()['hour_connect_attempts'],1)
        self.assertEqual(self.store.c.execute('SELECT code FROM completions WHERE id=?',(identity,)).fetchone()[0],'INTERRUPTED')

    def test_actual_module_cli_new_policy_and_missing_listen_no_creation(self):
        import subprocess,sys
        target=Path(self.tmp.name)/'cli.sqlite'
        created=subprocess.run([sys.executable,'-m','discovery.continuous','init','--db',str(target)],capture_output=True,text=True)
        self.assertEqual(created.returncode,0,created.stderr)
        status=subprocess.run([sys.executable,'-m','discovery.continuous','status','--db',str(target)],capture_output=True,text=True)
        value=json.loads(status.stdout)
        self.assertEqual(value['hour_byte_cap'],100000000)
        self.assertEqual(value['hour_record_cap'],5000)
        self.assertEqual(value['storage_byte_cap'],5368709120)
        missing=Path(self.tmp.name)/'missing-cli'
        result=subprocess.run([sys.executable,'-m','discovery.continuous','listen','--db',str(missing),'--seconds','1'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2);self.assertFalse(missing.exists())
        self.assertFalse(json.loads(result.stdout)['entries_authorized'])

    def test_existing_paper_ledger_reopens_without_fingerprint_adoption(self):
        from desk.ledger import Ledger
        from desk import engine
        from tests.helpers import config
        cfg=config();path=Path(self.tmp.name)/'paper.sqlite'
        ledger=Ledger(path)
        try:
            ledger.apply({'schema_version':1,'kind':'clock','event_id':'before-discovery','ts':100,'actor':'paper_monitor'},cfg,engine.transition,engine.initial_state)
            fingerprint=ledger.db.execute("SELECT value FROM metadata WHERE key='implementation_hash'").fetchone()[0]
        finally:ledger.close()
        self.receive(notification())
        ledger=Ledger(path,must_exist=True)
        try:
            ledger.apply({'schema_version':1,'kind':'clock','event_id':'after-discovery','ts':101,'actor':'paper_monitor'},cfg,engine.transition,engine.initial_state)
            self.assertEqual(ledger.db.execute("SELECT value FROM metadata WHERE key='implementation_hash'").fetchone()[0],fingerprint)
        finally:ledger.close()



class ListeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'db';d.initialize(self.path,hour_bytes=800000,hour_records=20,storage_bytes=64*1024**2)
        self.now=100.;self.tick=0.;self.store=d.Store(self.path,clock=lambda:self.now);self.addCleanup(self.store.close)
        self.calls=[];self.delays=[]
    async def sleep(self,seconds):self.tick+=seconds;self.now+=seconds;self.delays.append(seconds)
    def connector(self,*args,**kwargs):
        outer=self;self.calls.append(kwargs)
        class Socket:
            def __init__(self):self.n=0
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def send(self,data):outer.sent=json.loads(data)
            async def recv(self):
                self.n+=1;outer.tick+=1;outer.now+=1
                if self.n==1:return '{"id":1,"result":42}'
                if self.n==2:raise asyncio.TimeoutError()
                if self.n==3:return notification()
                raise asyncio.TimeoutError()
        return Socket()
    async def test_idle_preserves_connection_backpressure_and_original_delivery(self):
        result=await d.listen(self.store,seconds=5,connector=self.connector,key=lambda:'SYNTHETIC_TEST_ONLY',monotonic=lambda:self.tick,sleep=self.sleep)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0]['max_size'],200000);self.assertEqual(self.calls[0]['max_queue'],1)
        self.assertIsNone(self.calls[0]['compression'])
        self.assertEqual(self.sent['params'][0]['accountInclude'],[d.AUTHORITY])
        self.assertEqual(result['hour_connect_attempts'],1);self.assertEqual(self.store.c.execute('SELECT count(*) FROM raw_events').fetchone()[0],1)
        self.assertEqual(result['last_payload_at'],103.);self.assertEqual(result['last_stored_at'],103.)
        self.assertFalse(result['history_complete']);self.assertFalse(result['automatic_investigations'])
    async def test_failing_connector_backoff_budget_durable_no_secret_output(self):
        def failure(*args,**kwargs):raise OSError('secret=SYNTHETIC_TEST_ONLY')
        result=await d.listen(self.store,seconds=100,connector=failure,key=lambda:'SYNTHETIC_TEST_ONLY',monotonic=lambda:self.tick,sleep=self.sleep)
        self.assertTrue(all(0<=n<=60 for n in self.delays));self.assertEqual(self.delays[:3],[2,4,8])
        self.assertNotIn('SYNTHETIC_TEST_ONLY',canonical(result))
        self.assertGreater(result['hour_connect_attempts'],0)

    async def test_real_websocket_oversize_stops_and_keeps_charge_on_restart(self):
        await self.real_websocket_oversize()

    async def test_real_websocket_non_oversize_failure_still_retries_bounded(self):
        await self.real_websocket_oversize(fail_first=True)

    async def real_websocket_oversize(self,fail_first=False):
        from websockets.asyncio.client import connect
        from websockets.asyncio.server import serve
        connections=[]
        async def server(socket):
            connections.append(socket)
            await socket.recv()
            if fail_first and len(connections)==1:
                await socket.close(code=1011,reason='SYNTHETIC_TEST_ONLY')
                return
            await socket.send('{"id":1,"result":42}')
            await socket.send(b'x'*(d.MESSAGE+1))
            await socket.wait_closed()
        async with serve(server,'127.0.0.1',0,compression=None) as local:
            port=local.sockets[0].getsockname()[1]
            def connector(url,**kwargs):
                self.calls.append(kwargs)
                return connect(f'ws://127.0.0.1:{port}',**kwargs)
            with self.assertRaises(d.Blocked) as error:
                await d.listen(self.store,seconds=5,connector=connector,
                               key=lambda:'SYNTHETIC_TEST_ONLY',sleep=self.sleep)
        self.assertEqual(error.exception.code,'DISCOVERY_OVERSIZE')
        self.assertEqual(len(connections),1+fail_first);self.assertEqual(len(self.calls),1+fail_first)
        self.assertEqual(self.delays,[2] if fail_first else [])
        self.assertEqual(self.store.c.execute('SELECT count(*) FROM completions WHERE code="SOURCE_FAILURE"').fetchone()[0],int(fail_first))
        self.assertEqual(self.store.c.execute('SELECT bytes,records FROM completions WHERE code="OVERSIZE"').fetchall(),[(d.MESSAGE,1)])
        self.assertEqual(self.store.c.execute('SELECT count(*) FROM raw_events').fetchone()[0],0)
        before=self.store.status()
        self.store.close()
        self.store=d.Store(self.path,clock=lambda:self.now);self.addCleanup(self.store.close)
        self.store.recover()
        self.assertEqual(self.store.status()['hour_bytes'],before['hour_bytes'])
        self.assertEqual(self.store.status()['hour_records'],before['hour_records'])


if __name__=='__main__':unittest.main()
