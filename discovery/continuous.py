"""Bounded listening only; never admits candidates or authorizes paper entries.

One fixed database and exclusive worker. Original frames/reservations are append
only. Interrupted receives are conservatively charged at exclusive restart time;
no traffic counters are reconstructed from transaction count or reset on restart.
Application payload limits do not establish provider billing or TCP byte limits.
"""
import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
from urllib.parse import urlencode

from desk.model import canonical, digest
from desk.programs import address
from desk.providers import subscription

AUTHORITY = '39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg'
MESSAGE = 200000
HOUR_BYTES, HOUR_RECORDS = 100000000, 5000
RECONNECTS = 12
STORAGE = 5 * 1024**3
HEADROOM = 4 * 1024 * 1024  # rollback journal/transaction headroom


def _policy(hour_bytes,hour_records,storage_bytes):
    if (type(hour_bytes) is not int or not MESSAGE<=hour_bytes<=HOUR_BYTES
            or type(hour_records) is not int or not 1<=hour_records<=HOUR_RECORDS
            or type(storage_bytes) is not int or not 8*1024**2<=storage_bytes<=STORAGE):
        raise Blocked('DISCOVERY_POLICY_INVALID')
    return hour_bytes,hour_records,storage_bytes


class Blocked(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _path(path, *, existing=True):
    path = Path(path).absolute()
    if path.is_symlink(): raise Blocked('DISCOVERY_PATH_ALIAS')
    path=path.parent.resolve()/path.name
    if existing and (not path.is_file() or path.stat().st_nlink != 1):
        raise Blocked('DISCOVERY_DATABASE_REQUIRED')
    return path


@contextmanager
def worker(path):
    path = _path(path)
    lock = Path(str(path)+'.discovery.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_nlink != 1: raise Blocked('DISCOVERY_PATH_ALIAS')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise Blocked('DISCOVERY_WORKER_BUSY') from None
        yield
    finally:
        os.close(fd)


def initialize(path, filter_address=AUTHORITY, *, hour_bytes=HOUR_BYTES, hour_records=HOUR_RECORDS, storage_bytes=STORAGE):
    policy = _policy(hour_bytes,hour_records,storage_bytes)
    address(filter_address)
    path = _path(path, existing=False)
    # Exclusive init; never adopt the old launch/paper/research databases.
    with path.open('xb'): pass
    os.chmod(path, 0o600)
    with sqlite3.connect(path) as c:
        c.execute('PRAGMA journal_mode=DELETE')
        c.executescript('''
        CREATE TABLE settings(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,filter TEXT NOT NULL,high_water REAL NOT NULL,last_reservation INTEGER NOT NULL);
        CREATE TABLE policies(id INTEGER PRIMARY KEY,at REAL NOT NULL,hour_bytes INTEGER NOT NULL,hour_records INTEGER NOT NULL,storage_bytes INTEGER NOT NULL);
        CREATE TABLE reservations(id INTEGER PRIMARY KEY,at REAL NOT NULL,kind TEXT NOT NULL);
        CREATE TABLE completions(id INTEGER PRIMARY KEY REFERENCES reservations(id),at REAL NOT NULL,bytes INTEGER NOT NULL CHECK(bytes BETWEEN 0 AND 200000),records INTEGER NOT NULL CHECK(records IN (0,1)),code TEXT NOT NULL,frame_hash TEXT);
        CREATE TABLE frames(id INTEGER PRIMARY KEY REFERENCES reservations(id),payload BLOB NOT NULL,sha256 TEXT NOT NULL);
        CREATE TABLE raw_events(seq INTEGER PRIMARY KEY,source_id TEXT UNIQUE NOT NULL,received_at REAL NOT NULL,slot INTEGER NOT NULL,payload TEXT NOT NULL,payload_hash TEXT NOT NULL);
        CREATE INDEX completions_time ON completions(at);
        CREATE INDEX reservations_time_kind ON reservations(kind,at);
        CREATE INDEX frames_hash ON frames(sha256);
        ''')
        c.execute('INSERT INTO settings VALUES(1,2,?,0,0)', (filter_address,))
        c.execute('INSERT INTO policies VALUES(1,0,?,?,?)',policy)
        for table in ('reservations','completions','frames','raw_events','policies'):
            for action in ('UPDATE','DELETE'):
                c.execute(f"CREATE TRIGGER {table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'Original discovery record is immutable'); END")
            c.execute(f"CREATE TRIGGER {table}_insert BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'Duplicate discovery identity'); END")
        c.execute("CREATE TRIGGER raw_identity BEFORE INSERT ON raw_events WHEN EXISTS(SELECT 1 FROM raw_events WHERE source_id=NEW.source_id) BEGIN SELECT RAISE(ABORT,'Duplicate discovery source'); END")
        c.execute("CREATE TRIGGER settings_identity BEFORE UPDATE ON settings WHEN NEW.id!=OLD.id OR NEW.version!=OLD.version OR NEW.filter!=OLD.filter BEGIN SELECT RAISE(ABORT,'Fixed discovery identity'); END")
        c.execute("CREATE TRIGGER settings_monotonic BEFORE UPDATE ON settings WHEN NEW.high_water<OLD.high_water OR NEW.last_reservation NOT IN (OLD.last_reservation,OLD.last_reservation+1) BEGIN SELECT RAISE(ABORT,'Discovery counters are monotonic'); END")
        c.execute("CREATE TRIGGER settings_delete BEFORE DELETE ON settings BEGIN SELECT RAISE(ABORT,'Fixed discovery identity'); END")
        c.execute("CREATE TRIGGER settings_insert BEFORE INSERT ON settings WHEN EXISTS(SELECT 1 FROM settings) BEGIN SELECT RAISE(ABORT,'Fixed discovery identity'); END")


class Store:
    def __init__(self, path, *, clock=time.time):
        self.path = _path(path)
        self.clock = clock
        self.c = sqlite3.connect(self.path.as_uri()+'?mode=rw', uri=True, isolation_level=None, timeout=2)
        self.c.execute('PRAGMA synchronous=FULL')
        if self.c.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
            self.c.close(); raise Blocked('DISCOVERY_STORAGE_MODE_INVALID')
        try:
            self._validate()
        except BaseException:
            self.c.close();raise

    def close(self): self.c.close()

    def _validate(self):
        row = self.c.execute('SELECT version,filter,high_water,last_reservation FROM settings WHERE id=1').fetchone()
        if row is None or row[0] != 2: raise Blocked('DISCOVERY_IDENTITY_INVALID')
        address(row[1]); self.filter = row[1]
        count,smallest,largest=self.c.execute('SELECT count(*),COALESCE(min(id),0),COALESCE(max(id),0) FROM reservations').fetchone()
        if type(row[3]) is not int or row[3]!=count or row[3]!=largest or (count and smallest!=1):
            raise Blocked('DISCOVERY_ACCOUNTING_INVALID')
        policies=self.c.execute('SELECT id,at,hour_bytes,hour_records,storage_bytes FROM policies ORDER BY id').fetchall()
        if not policies or [r[0] for r in policies]!=list(range(1,len(policies)+1)):
            raise Blocked('DISCOVERY_POLICY_INVALID')
        for _,at,b,r,storage in policies:
            _policy(b,r,storage)
            if not math.isfinite(at) or not 0<=at<=row[2]: raise Blocked('DISCOVERY_POLICY_INVALID')
        if not math.isfinite(row[2]) or row[2] < 0: raise Blocked('DISCOVERY_CLOCK_INVALID')
        if self.c.execute('SELECT 1 FROM completions o LEFT JOIN reservations r ON r.id=o.id WHERE r.id IS NULL LIMIT 1').fetchone():
            raise Blocked('DISCOVERY_ACCOUNTING_INVALID')
        if self.c.execute('SELECT 1 FROM frames f LEFT JOIN completions o ON o.id=f.id WHERE o.id IS NULL OR o.frame_hash!=f.sha256 LIMIT 1').fetchone():
            raise Blocked('DISCOVERY_ORIGINAL_INVALID')
        for payload,h,source,slot,received in self.c.execute('SELECT payload,payload_hash,source_id,slot,received_at FROM raw_events'):
            raw=json.loads(payload)
            receipt=self.c.execute('SELECT 1 FROM frames f JOIN completions o ON o.id=f.id WHERE f.sha256=? AND o.frame_hash=f.sha256 AND o.records=1 AND o.code="RECEIVED" AND o.at=? LIMIT 1',(hashlib.sha256(payload.encode()).hexdigest(),received)).fetchone()
            if (receipt is None or digest(raw)!=h or raw.get('params',{}).get('result',{}).get('signature')!=source.removeprefix('confirmed:')
                    or raw.get('params',{}).get('result',{}).get('slot')!=slot):
                raise Blocked('DISCOVERY_ORIGINAL_INVALID')
        for identity, at, kind, completed, used, records, code, frame_hash in self.c.execute('SELECT r.id,r.at,r.kind,o.at,o.bytes,o.records,o.code,o.frame_hash FROM reservations r LEFT JOIN completions o ON o.id=r.id'):
            if (kind not in ('RECEIVE','CONNECT') or not math.isfinite(at) or not 0<=at<=row[2]
                    or (completed is not None and (not math.isfinite(completed) or not at<=completed<=row[2]
                    or type(used) is not int or not 0<=used<=MESSAGE or records not in (0,1)
                    or (kind=='CONNECT' and (used or records))))):
                raise Blocked('DISCOVERY_ACCOUNTING_INVALID')
            if frame_hash is not None:
                frame = self.c.execute('SELECT payload,sha256 FROM frames WHERE id=?',(identity,)).fetchone()
                if (not frame or hashlib.sha256(frame[0]).hexdigest()!=frame_hash or frame[1]!=frame_hash
                        or used!=len(frame[0])): raise Blocked('DISCOVERY_ORIGINAL_INVALID')
                if records==1 or code in ('DUPLICATE','CONFLICT'):
                    original=json.loads(frame[0])
                    event=original.get('params',{}).get('result',{})
                    saved=self.c.execute('SELECT slot,payload_hash FROM raw_events WHERE source_id=?',('confirmed:'+str(event.get('signature')),)).fetchone()
                    expected=(event.get('slot'),digest(original))
                    if (saved is None or (code=='CONFLICT' and saved==expected)
                            or (code!='CONFLICT' and saved!=expected)):
                        raise Blocked('DISCOVERY_ORIGINAL_INVALID')

    @property
    def policy(self):
        row=self.c.execute('SELECT hour_bytes,hour_records,storage_bytes FROM policies ORDER BY id DESC LIMIT 1').fetchone()
        if row is None: raise Blocked('DISCOVERY_POLICY_INVALID')
        return _policy(*row)

    def _begin(self):
        self.storage();self.c.execute('BEGIN IMMEDIATE')
        self.c.execute('PRAGMA max_page_count='+str((self.policy[2]-HEADROOM)//self.c.execute('PRAGMA page_size').fetchone()[0]))

    def configure(self, *, hour_bytes, hour_records, storage_bytes):
        policy=_policy(hour_bytes,hour_records,storage_bytes)
        if self.storage()+HEADROOM>storage_bytes: raise Blocked('DISCOVERY_STORAGE_LIMIT')
        self._begin()
        try:
            now=self._now()
            self.c.execute('INSERT INTO policies(at,hour_bytes,hour_records,storage_bytes) VALUES(?,?,?,?)',(now,*policy))
            self.c.execute('UPDATE settings SET high_water=? WHERE id=1',(now,));self.c.commit()
        except BaseException:self.c.rollback();raise

    def _now(self):
        now = self.clock()
        if type(now) not in (int,float) or not math.isfinite(now) or not 0<=now<2**53:
            raise Blocked('DISCOVERY_CLOCK_INVALID')
        if now < self.c.execute('SELECT high_water FROM settings WHERE id=1').fetchone()[0]:
            raise Blocked('DISCOVERY_CLOCK_ROLLBACK')
        return now

    def storage(self):
        total = 0
        for suffix in ('','-journal','-wal','-shm'):
            p=Path(str(self.path)+suffix)
            if p.exists():
                if p.is_symlink() or p.stat().st_nlink!=1: raise Blocked('DISCOVERY_PATH_ALIAS')
                total+=p.stat().st_size
        if total>=self.policy[2]-HEADROOM: raise Blocked('DISCOVERY_STORAGE_LIMIT')
        return total

    def usage(self, now):
        def window(seconds):
            b,r=self.c.execute('SELECT COALESCE(sum(bytes),0),COALESCE(sum(records),0) FROM completions WHERE at>?',(now-seconds,)).fetchone()
            pending=self.c.execute("SELECT count(*) FROM reservations r LEFT JOIN completions o ON o.id=r.id WHERE o.id IS NULL AND r.kind='RECEIVE'").fetchone()[0]
            connects=self.c.execute("SELECT count(*) FROM reservations r LEFT JOIN completions o ON o.id=r.id WHERE r.kind='CONNECT' AND (o.id IS NULL OR o.at>?)",(now-seconds,)).fetchone()[0]
            return b+pending*MESSAGE,r+pending,connects
        return window(3600),window(86400)

    def recover(self):
        """Called only under exclusive worker lock; charge unknown old reads fully."""
        self._begin()
        try:
            self._validate(); now=self._now()
            for identity,kind in self.c.execute('SELECT r.id,r.kind FROM reservations r LEFT JOIN completions o ON o.id=r.id WHERE o.id IS NULL').fetchall():
                self.c.execute('INSERT INTO completions VALUES(?,?,?,?,?,NULL)',(identity,now,MESSAGE if kind=='RECEIVE' else 0,1 if kind=='RECEIVE' else 0,'INTERRUPTED'))
            self.c.execute('UPDATE settings SET high_water=? WHERE id=1',(now,));self.c.commit()
        except BaseException:self.c.rollback();raise

    def touch(self):
        self._begin()
        try:
            now=self._now();self.c.execute('UPDATE settings SET high_water=? WHERE id=1',(now,));self.c.commit()
        except BaseException:self.c.rollback();raise

    def capacity(self):
        self.storage();now=self._now();hour,day=self.usage(now)
        if hour[0]+MESSAGE>self.policy[0] or hour[1]+1>self.policy[1] or day[0]+MESSAGE>24*self.policy[0] or day[1]+1>24*self.policy[1]:
            raise Blocked('DISCOVERY_TRAFFIC_LIMIT')

    def reserve(self, kind):
        if kind not in ('RECEIVE','CONNECT'): raise Blocked('DISCOVERY_REQUEST_INVALID')
        self._begin()
        try:
            now=self._now();hour,day=self.usage(now)
            if self.c.execute('SELECT 1 FROM reservations r LEFT JOIN completions o ON o.id=r.id WHERE o.id IS NULL LIMIT 1').fetchone():
                raise Blocked('DISCOVERY_PENDING_RESERVATION')
            if kind=='RECEIVE' and (hour[0]+MESSAGE>self.policy[0] or hour[1]+1>self.policy[1] or day[0]+MESSAGE>24*self.policy[0] or day[1]+1>24*self.policy[1]):
                raise Blocked('DISCOVERY_TRAFFIC_LIMIT')
            if kind=='CONNECT' and hour[2]>=RECONNECTS: raise Blocked('DISCOVERY_RECONNECT_LIMIT')
            identity=self.c.execute('SELECT last_reservation+1 FROM settings WHERE id=1').fetchone()[0]
            self.c.execute('UPDATE settings SET high_water=?,last_reservation=? WHERE id=1',(now,identity))
            self.c.execute('INSERT INTO reservations(id,at,kind) VALUES(?,?,?)',(identity,now,kind))
            self.c.commit();return identity
        except BaseException:self.c.rollback();raise

    def complete(self, identity, *, payload=None, code='RECEIVED', allow_notification=True):
        self._begin()
        try:
            now=self._now();row=self.c.execute('SELECT kind FROM reservations WHERE id=?',(identity,)).fetchone()
            if row is None or self.c.execute('SELECT 1 FROM completions WHERE id=?',(identity,)).fetchone():
                raise Blocked('DISCOVERY_RESERVATION_INVALID')
            kind=row[0];used=records=0;frame_hash=None
            if kind=='RECEIVE':
                used,records=MESSAGE,1
                if payload is not None:
                    if type(payload) is str: payload=payload.encode('utf-8')
                    if type(payload) is not bytes or len(payload)>MESSAGE:
                        code='OVERSIZE'
                    else:
                        used=len(payload);records=0;frame_hash=hashlib.sha256(payload).hexdigest()
                        self.c.execute('INSERT INTO frames VALUES(?,?,?)',(identity,payload,frame_hash))
                        try:
                            text=payload.decode('utf-8')
                            def pairs(values):
                                d={}
                                for k,v in values:
                                    if k in d: raise ValueError()
                                    d[k]=v
                                return d
                            parsed=json.loads(text,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
                            if type(parsed) is not dict: raise ValueError()
                            if parsed.get('method')=='transactionNotification' and not allow_notification:
                                code='EARLY_NOTIFICATION'
                            elif parsed.get('method')=='transactionNotification':
                                event=parsed['params']['result'];signature=event['signature'];slot=event['slot']
                                if type(signature) is not str or not 1<=len(signature)<=128 or type(slot) is not int or not 0<=slot<2**63: raise ValueError()
                                source='confirmed:'+signature;h=digest(parsed)
                                old=self.c.execute('SELECT slot,payload_hash FROM raw_events WHERE source_id=?',(source,)).fetchone()
                                if old is not None:
                                    code='DUPLICATE' if old==(slot,h) else 'CONFLICT'
                                else:
                                    self.c.execute('INSERT INTO raw_events(source_id,received_at,slot,payload,payload_hash) VALUES(?,?,?,?,?)',(source,now,slot,text,h));records=1
                            else:code='CONTROL_FRAME'
                        except (ValueError,KeyError,TypeError,UnicodeError,RecursionError):
                            code='MALFORMED'
            self.c.execute('INSERT INTO completions VALUES(?,?,?,?,?,?)',(identity,now,used,records,code,frame_hash))
            self.c.execute('UPDATE settings SET high_water=? WHERE id=1',(now,));self.c.commit()
            return code
        except BaseException:self.c.rollback();raise

    def status(self):
        now=self._now();hour,day=self.usage(now)
        return {'kind':'bounded_continuous_discovery_v1','hour_bytes':hour[0],'hour_records':hour[1],
                'day_bytes':day[0],'day_records':day[1],'hour_connect_attempts':hour[2],
                'filter':self.filter,'hour_byte_cap':self.policy[0],'hour_record_cap':self.policy[1],
                'day_byte_cap':24*self.policy[0],'day_record_cap':24*self.policy[1],
                'storage_byte_cap':self.policy[2],'entries_authorized':False,'automatic_investigations':False,
                'history_complete':False,'connected':'UNKNOWN',
                'last_connected_at':self.c.execute("SELECT max(at) FROM completions WHERE code='CONNECTED'").fetchone()[0],
                'last_payload_at':self.c.execute('SELECT max(at) FROM completions WHERE frame_hash IS NOT NULL').fetchone()[0],
                'last_stored_at':self.c.execute('SELECT max(received_at) FROM raw_events').fetchone()[0],
                'interrupted_receives':self.c.execute("SELECT count(*) FROM completions WHERE code IN ('INTERRUPTED','SOURCE_FAILURE','OVERSIZE')").fetchone()[0],
                'coverage':'BOUNDED_WITH_UNVERIFIED_GAPS'}


def credential():
    directory=os.environ.get('CREDENTIALS_DIRECTORY')
    if not directory: raise Blocked('DISCOVERY_CREDENTIAL_UNAVAILABLE')
    path=Path(directory)/'provider-keys.json'
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in (0o400,0o440,0o600) or info.st_size>16384:
        raise Blocked('DISCOVERY_CREDENTIAL_UNAVAILABLE')
    value=json.loads(path.read_text()).get('HELIUS_API_KEY')
    if type(value) is not str or not 1<=len(value)<=512 or any(not 33<=ord(c)<=126 for c in value):
        raise Blocked('DISCOVERY_CREDENTIAL_UNAVAILABLE')
    return value


async def _listen(store, *, seconds=86400, connector=None, key=credential, monotonic=time.monotonic, sleep=asyncio.sleep):
    if type(seconds) is not int or not 1<=seconds<=86400: raise Blocked('DISCOVERY_DURATION_INVALID')
    if connector is None:
        from websockets.asyncio.client import connect
        connector=connect
    store.recover()  # caller owns exclusive lock throughout
    started=last=monotonic(); failures=0
    if type(started) not in (int,float) or not math.isfinite(started):
        raise Blocked('DISCOVERY_MONOTONIC_INVALID')
    def remaining():
        nonlocal last
        now=monotonic()
        if type(now) not in (int,float) or not math.isfinite(now) or now<last:
            raise Blocked('DISCOVERY_MONOTONIC_INVALID')
        last=now
        return max(0,started+seconds-now)
    while remaining():
        try:
            store.capacity()  # no fabricated response or receive reservation
            connection=store.reserve('CONNECT')
        except Blocked as error:
            if error.code not in ('DISCOVERY_TRAFFIC_LIMIT','DISCOVERY_RECONNECT_LIMIT'): raise
            await sleep(min(60,remaining()));continue
        receive=None
        try:
            url='wss://mainnet.helius-rpc.com/?'+urlencode({'api-key':key()})
            async with connector(url,ping_interval=20,ping_timeout=20,open_timeout=10,max_size=MESSAGE,max_queue=1,compression=None) as ws:
                store.complete(connection,code='CONNECTED')
                receive=store.reserve('RECEIVE')  # before subscription and ack read
                await ws.send(canonical(subscription([store.filter])))
                acknowledged=False
                while remaining():
                    try:frame=await asyncio.wait_for(ws.recv(),timeout=min(30,max(.01,remaining())))
                    except asyncio.TimeoutError:
                        store.touch()  # retain high-water clock; do not reconnect or claim gap
                        continue
                    outcome=store.complete(receive,payload=frame,allow_notification=acknowledged);receive=None
                    if outcome in ('OVERSIZE','MALFORMED','CONFLICT'): raise Blocked('DISCOVERY_'+outcome)
                    if not acknowledged:
                        ack=json.loads(frame)
                        if (type(ack) is not dict or ack.get('id')!=1 or 'error' in ack
                                or type(ack.get('result')) is not int or ack['result']<0):
                            raise Blocked('DISCOVERY_SUBSCRIPTION_REJECTED')
                        acknowledged=True
                    receive=store.reserve('RECEIVE')
                if receive is not None:
                    store.complete(receive,code='INTERRUPTED');receive=None
                return {**store.status(),'connected':False,'subscription_acknowledged':acknowledged}
        except Blocked as error:
            if receive is not None: store.complete(receive,code='INTERRUPTED')
            if error.code in ('DISCOVERY_TRAFFIC_LIMIT','DISCOVERY_RECONNECT_LIMIT'):
                await sleep(min(60,remaining()));continue
            raise
        except Exception:
            # No URLs, provider exception strings or credentials are emitted.
            if receive is not None:store.complete(receive,code='SOURCE_FAILURE')
            if not store.c.execute('SELECT 1 FROM completions WHERE id=?',(connection,)).fetchone():
                store.complete(connection,code='CONNECT_FAILURE')
            failures+=1
            await sleep(min(60,2**min(failures,6),remaining()))
    return {**store.status(),'connected':False}


async def listen(store, **kwargs):
    with worker(store.path):
        return await _listen(store, **kwargs)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Opt-in bounded discovery; no investigations or entries')
    commands=parser.add_subparsers(dest='command',required=True)
    init=commands.add_parser('init');init.add_argument('--db',required=True);init.add_argument('--address',default=AUTHORITY)
    run=commands.add_parser('listen');run.add_argument('--db',required=True);run.add_argument('--seconds',type=int,default=86400)
    status=commands.add_parser('status');status.add_argument('--db',required=True)
    configure=commands.add_parser('configure');configure.add_argument('--db',required=True)
    for command in (init,configure):
        command.add_argument('--hour-bytes',type=int,default=HOUR_BYTES)
        command.add_argument('--hour-records',type=int,default=HOUR_RECORDS)
        command.add_argument('--storage-bytes',type=int,default=STORAGE)
    args=parser.parse_args(argv)
    try:
        if args.command=='init':
            initialize(args.db,args.address,hour_bytes=args.hour_bytes,hour_records=args.hour_records,storage_bytes=args.storage_bytes)
            print('{"status":"INITIALIZED","entries_authorized":false}');return 0
        store=Store(args.db)
        try:
            if args.command=='listen':result=asyncio.run(listen(store,seconds=args.seconds))
            elif args.command=='status':
                store.c.execute('BEGIN');result=store.status();store.c.rollback()
            else:
                with worker(args.db):
                    if args.command=='configure':
                        store.configure(hour_bytes=args.hour_bytes,hour_records=args.hour_records,storage_bytes=args.storage_bytes)
                    result=store.status()
            print(canonical(result));return 0
        finally:store.close()
    except (Blocked,sqlite3.Error,OSError,ValueError,ImportError):
        print('{"status":"BLOCKED","entries_authorized":false}');return 2


if __name__=='__main__':raise SystemExit(main())
