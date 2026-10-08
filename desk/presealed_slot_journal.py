"""Disconnected fresh-job finalized-slot intent. No transport or publication.

Trusted local code supplies a live job worker and planned source configuration.
Neither these objects nor syntax-valid replies authenticate a remote provider.
One dedicated evidence database, one job, one charged intent; no retry/reset.
"""
from contextlib import closing, contextmanager
from dataclasses import asdict
import fcntl
import os
import re
from pathlib import Path
import sqlite3
import threading

from .common_bank_journal import _file, _identity, _sha, _strict_json
from .history_progress import HistoryProgress, canonical_ownership_path, ownership_lock_path
from .job_persistence import _Worker, Claim, canonical_job_path
from .model import canonical, digest
from .pool_receipt_ledger import ApprovedSource
from .pool_vault_admission import NETWORK, GENESIS, PROFILE

APPLICATION_ID = 0x50534A31
VERSION = 1
MAX_REQUEST_BYTES = 1024
MAX_RESPONSE_BYTES = 65536
MAX_FAILURE_BYTES = 2048
MAX_METADATA_BYTES = 8192
MAX_CATALOG_RECORDS = 64
MAX_CATALOG_NAME_BYTES = 128
MAX_CATALOG_SQL_BYTES = 4096
MAX_CATALOG_BYTES = 65536
BASE_SCHEMA = {
 'pages': 'CREATE TABLE pages(hash TEXT PRIMARY KEY,payload BLOB NOT NULL,raw_bytes INTEGER NOT NULL)',
 'ownership_budgets': 'CREATE TABLE ownership_budgets(id TEXT PRIMARY KEY,source_hash TEXT NOT NULL,used INTEGER NOT NULL,ceiling INTEGER NOT NULL)',
 'ownership_history': 'CREATE TABLE ownership_history(id TEXT PRIMARY KEY,budget TEXT NOT NULL,query TEXT NOT NULL,coverage TEXT,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0)',
 'ownership_admissions': 'CREATE TABLE ownership_admissions(id TEXT PRIMARY KEY,descriptor TEXT NOT NULL,state TEXT NOT NULL,prepared_source TEXT,prepared_used INTEGER,completed_source_hash TEXT)',
}
# Each operation audits at most one intent and one attachment, then writes one
# bounded attachment. No store/page decompression or selected second load.
MAX_AUDIT_BYTES = 2 * MAX_METADATA_BYTES + MAX_REQUEST_BYTES + MAX_RESPONSE_BYTES + MAX_FAILURE_BYTES
SCHEMA = {
 'presealed_slot_meta': 'CREATE TABLE presealed_slot_meta(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL) WITHOUT ROWID',
 'presealed_slot_intent': 'CREATE TABLE presealed_slot_intent(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL,hash TEXT UNIQUE NOT NULL,request BLOB NOT NULL) WITHOUT ROWID',
 'presealed_slot_attachment': 'CREATE TABLE presealed_slot_attachment(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL,hash TEXT UNIQUE NOT NULL,response BLOB,failure TEXT) WITHOUT ROWID',
}
for table in tuple(SCHEMA):
    for op in ('UPDATE','DELETE'):
        name=table+'_'+op.lower()
        SCHEMA[name]=f"CREATE TRIGGER {name} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'Immutable presealed slot'); END"
    name=table+'_replace'
    SCHEMA[name]=f"CREATE TRIGGER {name} BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table}) BEGIN SELECT RAISE(ABORT,'Presealed slot identity exists'); END"
# Protect old generic readers at SQL level, even recursive_triggers=0. The
# intent is inserted before the single charge in the SAME transaction.
SCHEMA['presealed_budget_update']="""CREATE TRIGGER presealed_budget_update BEFORE UPDATE ON ownership_budgets
 WHEN NEW.id IS NOT OLD.id OR NEW.rowid IS NOT OLD.rowid OR NEW.source_hash IS NOT OLD.source_hash OR NEW.ceiling IS NOT OLD.ceiling
 OR OLD.used!=0 OR NEW.used!=1 OR NOT EXISTS(SELECT 1 FROM presealed_slot_intent WHERE id=1)
 BEGIN SELECT RAISE(ABORT,'Presealed budget requires unique atomic intent'); END"""
for table in ('ownership_budgets','ownership_admissions','ownership_history','pages'):
    for op in ('INSERT','DELETE'):
        name='presealed_'+table+'_'+op.lower()
        SCHEMA[name]=f"CREATE TRIGGER {name} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'Disconnected presealed profile'); END"
    if table!='ownership_budgets':
        name='presealed_'+table+'_update'
        SCHEMA[name]=f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'Disconnected presealed profile'); END"
SCHEMA['presealed_intent_admission']="""CREATE TRIGGER presealed_intent_admission BEFORE INSERT ON presealed_slot_intent
 WHEN NOT EXISTS(SELECT 1 FROM ownership_budgets WHERE used=0 AND ceiling=18)
 BEGIN SELECT RAISE(ABORT,'Fresh presealed budget required'); END"""
SCHEMA['presealed_attachment_predecessor']="""CREATE TRIGGER presealed_attachment_predecessor BEFORE INSERT ON presealed_slot_attachment
 WHEN NOT EXISTS(SELECT 1 FROM presealed_slot_intent i JOIN ownership_budgets b WHERE b.used=1 AND json_extract(NEW.body,'$.previous_hash')=i.hash)
 BEGIN SELECT RAISE(ABORT,'Presealed attachment predecessor'); END"""

def _expected_catalog(installed):
    # Type is part of identity: SQLite permits a trigger and table with the
    # same name. A dict keyed only by name would silently overwrite one.
    rows = [('table', name, name, sql) for name, sql in BASE_SCHEMA.items()]
    rows += [('index', 'sqlite_autoindex_'+name+'_1', name, None) for name in BASE_SCHEMA]
    if installed:
        for name, sql in SCHEMA.items():
            kind = 'table' if sql.startswith('CREATE TABLE ') else 'trigger'
            table = name if kind == 'table' else re.search(r'\bON ([a-z_]+)', sql).group(1)
            rows.append((kind, name, table, sql))
        rows += [('index', 'sqlite_autoindex_'+name+'_1', name, None)
                 for name in ('presealed_slot_intent', 'presealed_slot_attachment')]
    return sorted(rows)


def _catalog(c, *, installed):
    """Full typed set under the caller's guarded transaction, before body I/O.

    Only scalar count/byte statistics are fetched before aggregate reservation.
    No names or SQL bodies, including a contradictory suffix, are selected early.
    """
    prefix = 'PRESEALED_SCHEMA' if installed else 'FRESH_ADMITTED_DATABASE_ONLY:SCHEMA'
    expected = _expected_catalog(installed)
    stats = c.execute("""SELECT count(*),
        max(length(CAST(type AS BLOB))),max(length(CAST(name AS BLOB))),
        max(length(CAST(tbl_name AS BLOB))),max(length(CAST(sql AS BLOB))),
        coalesce(sum(length(CAST(type AS BLOB))+length(CAST(name AS BLOB))+
          length(CAST(tbl_name AS BLOB))+coalesce(length(CAST(sql AS BLOB)),0)),0)
        FROM sqlite_master""").fetchone()
    _need(stats[0] == len(expected) and stats[0] <= MAX_CATALOG_RECORDS, prefix+'_CATALOG_COUNT')
    _need(all(type(n) is int and 0 < n <= limit for n, limit in
              zip(stats[1:5], (16, MAX_CATALOG_NAME_BYTES, MAX_CATALOG_NAME_BYTES, MAX_CATALOG_SQL_BYTES))),
          prefix+'_CATALOG_RECORD_LIMIT')
    _need(type(stats[5]) is int and 0 < stats[5] <= MAX_CATALOG_BYTES, prefix+'_CATALOG_AGGREGATE_LIMIT')
    # A bounded list of complete typed records; never overwrite equal names.
    rows = c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master').fetchall()
    _need(len(rows) == len(expected) and sorted(rows) == expected, prefix+'_CATALOG_MISMATCH')


class SlotJournalBlocked(ValueError):
    pass


def _need(ok, reason):
    if not ok: raise SlotJournalBlocked(reason)


def _bounded(raw, maximum):
    _need(type(raw) is bytes and 0<len(raw)<=maximum,'ORIGINAL_BYTES_REQUIRED_OR_OVERSIZED')
    return raw


def _result(request, response):
    try:
        value=_strict_json(response);req=_strict_json(request)
        _need(type(value) is dict and set(value)=={'jsonrpc','id','result'}
              and value['jsonrpc']=='2.0' and type(value['id']) is str and value['id']==req['id'], 'WIRE_FRAME_INVALID')
        cutoff=value['result']
        _need(type(cutoff) is int and 0<=cutoff<(1<<63)-1,'CUTOFF_NOT_OVERFLOW_SAFE_INTEGER')
        return 'DONE',cutoff,None
    except (ValueError,TypeError,UnicodeError,RecursionError):
        return 'FAILED',None,'INVALID_ORIGINAL_SLOT_RESPONSE'


class PresealedSlotJournal:
    def __init__(self,research_db,evidence_db,planned_source):
        self.research=canonical_job_path(research_db);self.path=canonical_ownership_path(evidence_db)
        _need(self.research.exists() and self.path.exists() and not self.research.samefile(self.path),'DISTINCT_EXISTING_DATABASES_REQUIRED')
        _need(type(planned_source) is ApprovedSource and _identity(planned_source.source_id)
              and planned_source.source_kind in ('coordinator_capture','synthetic_fixture')
              and (planned_source.network,planned_source.genesis_hash,planned_source.profile)==(NETWORK,GENESIS,PROFILE),'PLANNED_SOURCE_INVALID')
        self.pins={'research_path':str(self.research),'research_identity':_file(self.research),
                   'evidence_path':str(self.path),'evidence_identity':_file(self.path),'planned_source':asdict(planned_source)}

    def _guard(self):
        _need(canonical_job_path(self.research)==self.research and canonical_ownership_path(self.path)==self.path
              and _file(self.research)==self.pins['research_identity'] and _file(self.path)==self.pins['evidence_identity'],'DATABASE_IDENTITY_CHANGED')

    @contextmanager
    def locked(self,worker,claim):
        # Caller already owns jobs-worker. Never recursively acquire its flock.
        _need(type(worker) is _Worker and type(claim) is Claim and worker.store.path==self.research,'LIVE_EXISTING_WORKER_REQUIRED')
        descriptor=worker._acquisition_claim(claim)
        _need(descriptor.get('evidence_db')==str(self.path),'JOB_EVIDENCE_BINDING_MISMATCH')
        self._guard();fds=[];session=None
        try:
            for path in (ownership_lock_path(self,True),ownership_lock_path(self)):
                fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600);fds.append(fd)
                _need(_file(Path(path))==[os.fstat(fd).st_dev,os.fstat(fd).st_ino],'LOCK_IDENTITY_CHANGED')
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise SlotJournalBlocked('PRESEALED_BUSY') from None
            session=_Session(self,worker,claim,descriptor)
            yield session
        finally:
            if session is not None:session.active=False;session.handles.clear()
            for fd in reversed(fds):os.close(fd)


class _Session:
    def __init__(self,journal,worker,claim,descriptor):
        self.journal=journal;self.worker=worker;self.claim=claim;self.descriptor=descriptor
        self.active=True;self.owner=(os.getpid(),threading.get_ident());self.handles={}

    def _check(self):
        _need(self.active and self.owner==(os.getpid(),threading.get_ident()),'SESSION_EXPIRED')
        self.journal._guard();_need(self.worker._acquisition_claim(self.claim)==self.descriptor,'STALE_JOB_FENCE')

    @contextmanager
    def _tx(self):
        self._check()
        with closing(sqlite3.connect(self.journal.path,timeout=0,isolation_level=None)) as c:
            _need(c.execute('PRAGMA journal_mode').fetchone()[0]=='delete','ROLLBACK_JOURNAL_REQUIRED')
            c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
            try:
                yield c
                self._check();c.commit()
            except BaseException:
                if c.in_transaction:c.rollback()
                raise

    def _fresh(self,c):
        _need(c.execute('PRAGMA application_id').fetchone()[0]==0 and c.execute('PRAGMA user_version').fetchone()[0]==0,'UNKNOWN_OR_EXISTING_PROFILE')
        _catalog(c, installed=False)
        _need(c.execute('SELECT count(*) FROM ownership_budgets').fetchone()[0]==1
              and c.execute('SELECT count(*) FROM ownership_admissions').fetchone()[0]==1
              and c.execute('SELECT count(*) FROM pages').fetchone()[0]==0
              and c.execute('SELECT count(*) FROM ownership_history').fetchone()[0]==0,'NO_LEGACY_RECORD_RETROFIT')
        self._admission_bound(c)
        a=HistoryProgress.inspect_admission(c,self.claim.scan_id)
        expected={'kind':'ownership_admission_v1','scan_id':self.claim.scan_id,'mint':self.claim.mint,'created':self.descriptor['admitted_at']}
        _need(a is not None and a['descriptor']==expected and a['state']=='ADMITTED'
              and a['requests_used']==0 and a['request_ceiling']==18,'FRESH_ADMISSION_REQUIRED')
        _need(self.claim.generation==1,'FRESH_JOB_CLAIM_REQUIRED')
        with closing(self.worker.store.connect()) as r:
            row=r.execute('SELECT result FROM scans WHERE id=?',(self.claim.scan_id,)).fetchone()
        _need(row is not None and row[0] is None,'UNPUBLISHED_FRESH_JOB_REQUIRED')

    def _admission_bound(self,c):
        row=c.execute('SELECT length(CAST(descriptor AS BLOB)),length(CAST(prepared_source AS BLOB)) FROM ownership_admissions WHERE id=?',(self.claim.scan_id,)).fetchone()
        _need(row is not None and type(row[0]) is int and 0<row[0]<=MAX_METADATA_BYTES and row[1] is None,'BOUNDED_FRESH_ADMISSION_REQUIRED')

    def _audit(self,c):
        _need((c.execute('PRAGMA application_id').fetchone()[0],c.execute('PRAGMA user_version').fetchone()[0])==(APPLICATION_ID,VERSION),'UNKNOWN_PRESEALED_VERSION')
        _catalog(c, installed=True)
        # Length/count preflight occurs before ANY journal body fetch.
        total=0
        for table in ('presealed_slot_meta','presealed_slot_intent','presealed_slot_attachment'):
            columns={'presealed_slot_meta':['body'],'presealed_slot_intent':['body','request'],'presealed_slot_attachment':['body','response','failure']}[table]
            rows=c.execute('SELECT '+','.join('length(CAST('+x+' AS BLOB))' for x in columns)+' FROM '+table).fetchall()
            _need(len(rows)<=1,'PRESEALED_ROW_LIMIT')
            for row in rows:
                limits=[MAX_METADATA_BYTES]+([MAX_REQUEST_BYTES] if table.endswith('intent') else [MAX_RESPONSE_BYTES,MAX_FAILURE_BYTES] if table.endswith('attachment') else [])
                for n,limit in zip(row,limits):
                    _need(n is None or 0<n<=limit,'PRESEALED_RECORD_LIMIT');total+=n or 0
        _need(total<=MAX_AUDIT_BYTES,'PRESEALED_AGGREGATE_LIMIT')
        meta=c.execute('SELECT body FROM presealed_slot_meta').fetchone()
        _need(meta is not None,'PRESEALED_METADATA_MISSING')
        binding=_strict_json(meta[0].encode())
        _need(type(binding) is dict and set(binding)=={'version','pins','job_descriptor','admission_hash','fence'}
              and type(binding['version']) is int and binding['version']==VERSION
              and binding['pins']==self.journal.pins and binding['job_descriptor']==self.descriptor
              and type(binding['fence']) is dict and set(binding['fence'])=={'generation','token'}
              and type(binding['fence']['generation']) is int and binding['fence']['generation']==1
              and _identity(binding['fence']['token']) and canonical(binding)==meta[0],'PRESEALED_SOURCE_OR_JOB_MISMATCH')
        self._admission_bound(c)
        a=HistoryProgress.inspect_admission(c,self.claim.scan_id)
        _need(a is not None and a['state']=='ADMITTED' and a['descriptor_hash']==binding['admission_hash'] and a['requests_used']==1 and a['request_ceiling']==18,'PRESEALED_COUNTER_CORRUPT')
        _need(c.execute('SELECT count(*) FROM ownership_budgets').fetchone()[0]==1 and c.execute('SELECT count(*) FROM ownership_admissions').fetchone()[0]==1
              and c.execute('SELECT count(*) FROM pages').fetchone()[0]==0 and c.execute('SELECT count(*) FROM ownership_history').fetchone()[0]==0,'PRESEALED_LEGACY_MUTATION')
        row=c.execute('SELECT body,hash,request FROM presealed_slot_intent').fetchone()
        _need(row is not None,'CHARGED_INTENT_MISSING')
        intent=_strict_json(row[0].encode());request=row[2]
        _need(canonical(intent)==row[0] and digest(intent)==row[1] and intent=={'version':1,'state':'PENDING','binding_hash':digest(binding),'fence':binding['fence'],'used_after':1,'request_hash':_sha(request)}
              and type(intent.get('version')) is int and type(intent.get('used_after')) is int
              and request==self._request(binding),'ORIGINAL_INTENT_CORRUPT')
        attachment=c.execute('SELECT body,hash,response,failure FROM presealed_slot_attachment').fetchone()
        if attachment:
            event=_strict_json(attachment[0].encode())
            expected=self._event(row[1],request,attachment[2],attachment[3])
            _need(canonical(event)==attachment[0] and digest(event)==attachment[1] and canonical(event)==canonical(expected),'ORIGINAL_ATTACHMENT_CORRUPT')
        return binding,row,attachment

    @staticmethod
    def _request(binding):
        return canonical({'jsonrpc':'2.0','id':'presealed-slot:'+digest(binding),'method':'getSlot','params':[{'commitment':'finalized'}]}).encode()

    def begin_slot(self):
        with self._tx() as c:
            self._fresh(c)
            a=HistoryProgress.inspect_admission(c,self.claim.scan_id)
            binding={'version':VERSION,'pins':self.journal.pins,'job_descriptor':self.descriptor,'admission_hash':a['descriptor_hash'],
                     'fence':{'generation':self.claim.generation,'token':self.claim.token}}
            raw=canonical(binding);_need(len(raw.encode())<=MAX_METADATA_BYTES,'METADATA_LIMIT')
            for sql in SCHEMA.values():c.execute(sql)
            c.execute('PRAGMA application_id='+str(APPLICATION_ID));c.execute('PRAGMA user_version='+str(VERSION))
            c.execute('INSERT INTO presealed_slot_meta VALUES(1,?)',(raw,))
            request=_bounded(self._request(binding),MAX_REQUEST_BYTES)
            intent={'version':1,'state':'PENDING','binding_hash':digest(binding),'fence':binding['fence'],'used_after':1,'request_hash':_sha(request)}
            c.execute('INSERT INTO presealed_slot_intent VALUES(1,?,?,?)',(canonical(intent),digest(intent),request))
            changed=c.execute('UPDATE ownership_budgets SET used=used+1 WHERE id=? AND used=0 AND used<ceiling',(self.claim.scan_id,)).rowcount
            _need(changed==1,'SHARED18_RESERVATION_FAILED');self._audit(c)
        # A commit exception/lost acknowledgment cannot mint a handle.
        handle=object();self.handles[handle]={'request':request,'intent_hash':digest(intent),'binding':binding,'chosen':None}
        return handle

    def _handle(self,handle):
        self._check();_need(type(handle) is object and handle in self.handles,'UNKNOWN_OR_REOPENED_PENDING_TERMINAL')
        state=self.handles[handle]
        _need(state['binding']['fence']=={'generation':self.claim.generation,'token':self.claim.token},'STALE_COMPLETION_FENCE')
        return state

    def request_bytes(self,handle):
        state=self._handle(handle)
        with self._tx() as c:self._audit(c)
        return state['request']

    @staticmethod
    def _event(previous,request,response,failure):
        if response is not None:
            _bounded(response,MAX_RESPONSE_BYTES);_need(failure is None,'CONFLICTING_ATTACHMENT')
            status,cutoff,reason=_result(request,response)
        else:
            _need(type(failure) is str and 0<len(failure.encode())<=MAX_FAILURE_BYTES,'BOUNDED_FAILURE_REQUIRED')
            value=_strict_json(failure.encode())
            _need(value in ({'category':'TRANSPORT_TIMEOUT'},{'category':'TRANSPORT_ERROR'},{'category':'CANCELLED'}),'REDACTED_FAILURE_CATEGORY_REQUIRED')
            status,cutoff,reason='FAILED',None,value['category']
        return {'version':1,'previous_hash':previous,'state':status,'request_hash':_sha(request),
                'response_hash':_sha(response) if response is not None else None,'failure_hash':_sha(failure.encode()) if failure is not None else None,
                'cutoff':cutoff,'exclusive_cutoff':cutoff+1 if cutoff is not None else None,'reason':reason,
                'source_authenticated':False,'ownership_complete':False,'eligible_for_trading':False}

    def _attach(self,handle,request,response,failure):
        state=self._handle(handle)
        _need(type(request) is bytes and request==state['request'],'EXACT_GENERATED_REQUEST_REQUIRED')
        event=self._event(state['intent_hash'],request,response,failure)
        chosen=(canonical(event),response,failure)
        if state['chosen'] is None:state['chosen']=chosen
        _need(state['chosen']==chosen,'CONFLICTING_KNOWN_RESULT')
        with self._tx() as c:
            _,_,old=self._audit(c)
            if old:
                _need((old[0],old[2],old[3])==chosen,'CONFLICTING_PERSISTED_RESULT')
            else:
                c.execute('INSERT INTO presealed_slot_attachment VALUES(1,?,?,?,?)',(chosen[0],digest(event),response,failure))
                _,_,persisted=self._audit(c)
                _need(persisted is not None and (persisted[0],persisted[1],persisted[2],persisted[3])
                      == (chosen[0],digest(event),response,failure),'EXPECTED_ATTACHMENT_NOT_PERSISTED')
        return event

    def attach_response(self,handle,request_bytes,response_bytes):
        _bounded(response_bytes,MAX_RESPONSE_BYTES)
        return self._attach(handle,request_bytes,response_bytes,None)

    def attach_failure(self,handle,request_bytes,category):
        _need(category in ('TRANSPORT_TIMEOUT','TRANSPORT_ERROR','CANCELLED'),'REDACTED_FAILURE_CATEGORY_REQUIRED')
        return self._attach(handle,request_bytes,None,canonical({'category':category}))

    def inspect(self):
        with self._tx() as c:
            _,_,attachment=self._audit(c)
            return _strict_json(attachment[0].encode()) if attachment else {'state':'PENDING_TERMINAL','source_authenticated':False,'ownership_complete':False,'eligible_for_trading':False}
