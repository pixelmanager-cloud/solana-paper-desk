"""Disconnected SEALED-source plan and atomic shared-charge/PENDING primitive.

No provider/publication, canonical bank/head or receipt API exists.
Reopened PENDING is terminal; only a same-session opaque in-flight handle
can attach an observation. Trusted application configuration
supplies the planned source identity; its type/hash never authenticates RPC.
"""
from contextlib import closing, contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading

from .control_obligations import ReplayView, read_guard
from .common_bank_view import MAX_RECORD_BYTES
from .entry_evidence import _validate_continuation_source
from .history_progress import HistoryProgress, canonical_ownership_path, ownership_lock_path
from .job_persistence import canonical_job_path
from .live_features import _BoundedStore
from .model import canonical, digest
from .pool_capture_bridge import canonical_accounts
from .pool_receipt_ledger import ApprovedSource
from .pool_vault_admission import GENESIS, NETWORK, PROFILE, _raw_account, _Reject
from .providers import PUMPSWAP
from .programs import address
from .replay_history import reconstruct_launch_history
from .security import TOKEN_PROGRAM, mint_policy

APPLICATION_ID = 0x43424A31
ATTACHMENT_VERSION = 0x43424731
SLOT_VERSION = 0x43425331
UNION_VERSION = 0x43425531
CLOCK_VERSION = 0x43424331
MAX_UNION_REQUEST_BYTES = 8192
MAX_REQUEST_BYTES = 1024
MAX_RESPONSE_BYTES = 65536
MAX_FAILURE_BYTES = 2048
MAX_RUNS = 128
EXISTING_COLUMNS = {
    'ownership_budgets': [('id','TEXT',0,1),('source_hash','TEXT',1,0),('used','INTEGER',1,0),('ceiling','INTEGER',1,0)],
    'ownership_admissions': [('id','TEXT',0,1),('descriptor','TEXT',1,0),('state','TEXT',1,0),('prepared_source','TEXT',0,0),('prepared_used','INTEGER',0,0),('completed_source_hash','TEXT',0,0)],
    'pages': [('hash','TEXT',0,1),('payload','BLOB',1,0),('raw_bytes','INTEGER',1,0)],
}
SCHEMA = {
    'common_bank_meta': 'CREATE TABLE common_bank_meta(id INTEGER PRIMARY KEY CHECK(id=1),descriptor TEXT NOT NULL) WITHOUT ROWID',
    'common_bank_runs': 'CREATE TABLE common_bank_runs(capture_id TEXT PRIMARY KEY,budget_id TEXT UNIQUE NOT NULL,descriptor_json TEXT NOT NULL,plan_json TEXT NOT NULL,plan_hash TEXT NOT NULL,seed_hash TEXT UNIQUE NOT NULL) WITHOUT ROWID',
    'common_bank_events': 'CREATE TABLE common_bank_events(capture_id TEXT NOT NULL,ordinal INTEGER NOT NULL,event_json TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT UNIQUE NOT NULL,PRIMARY KEY(capture_id,ordinal),FOREIGN KEY(capture_id) REFERENCES common_bank_runs(capture_id)) WITHOUT ROWID',
}
for table in ('common_bank_meta', 'common_bank_runs', 'common_bank_events'):
    for operation in ('UPDATE', 'DELETE'):
        name = table + '_' + operation.lower()
        SCHEMA[name] = f"CREATE TRIGGER {name} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Immutable common bank journal'); END"
SCHEMA.update({
    'common_bank_meta_replace': "CREATE TRIGGER common_bank_meta_replace BEFORE INSERT ON common_bank_meta WHEN EXISTS(SELECT 1 FROM common_bank_meta) BEGIN SELECT RAISE(ABORT,'Immutable common bank metadata'); END",
    'common_bank_run_replace': "CREATE TRIGGER common_bank_run_replace BEFORE INSERT ON common_bank_runs WHEN EXISTS(SELECT 1 FROM common_bank_runs WHERE capture_id=NEW.capture_id OR budget_id=NEW.budget_id OR seed_hash=NEW.seed_hash) BEGIN SELECT RAISE(ABORT,'Common bank run already exists'); END",
    'common_bank_event_insert': "CREATE TRIGGER common_bank_event_insert BEFORE INSERT ON common_bank_events WHEN NEW.ordinal!=1 OR EXISTS(SELECT 1 FROM common_bank_events WHERE capture_id=NEW.capture_id OR event_hash=NEW.event_hash) OR NOT EXISTS(SELECT 1 FROM common_bank_runs r JOIN ownership_budgets b ON b.id=r.budget_id WHERE r.capture_id=NEW.capture_id AND r.seed_hash=NEW.previous_hash AND json_valid(NEW.event_json) AND json_extract(NEW.event_json,'$.used_after')=b.used AND json_extract(NEW.event_json,'$.state')='PENDING') BEGIN SELECT RAISE(ABORT,'Common bank intent fence or charge mismatch'); END",
})


# Explicit additive profile; the original v1 tables/triggers stay byte-for-byte.
ATTACHMENT_SCHEMA = {
    'common_bank_attachment_meta': 'CREATE TABLE common_bank_attachment_meta(id INTEGER PRIMARY KEY CHECK(id=1),descriptor TEXT NOT NULL) WITHOUT ROWID',
    'common_bank_attachments': 'CREATE TABLE common_bank_attachments(capture_id TEXT PRIMARY KEY,event_json TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT UNIQUE NOT NULL,request_bytes BLOB NOT NULL,response_bytes BLOB,failure_json TEXT,FOREIGN KEY(capture_id) REFERENCES common_bank_runs(capture_id)) WITHOUT ROWID',
}
for table in ('common_bank_attachment_meta', 'common_bank_attachments'):
    for operation in ('UPDATE', 'DELETE'):
        name = table + '_' + operation.lower()
        ATTACHMENT_SCHEMA[name] = f"CREATE TRIGGER {name} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Immutable common bank attachment'); END"
ATTACHMENT_SCHEMA.update({
    'common_bank_attachment_meta_replace': "CREATE TRIGGER common_bank_attachment_meta_replace BEFORE INSERT ON common_bank_attachment_meta WHEN EXISTS(SELECT 1 FROM common_bank_attachment_meta) BEGIN SELECT RAISE(ABORT,'Immutable attachment metadata'); END",
    'common_bank_attachment_insert': "CREATE TRIGGER common_bank_attachment_insert BEFORE INSERT ON common_bank_attachments WHEN EXISTS(SELECT 1 FROM common_bank_attachments WHERE capture_id=NEW.capture_id OR event_hash=NEW.event_hash) OR NOT EXISTS(SELECT 1 FROM common_bank_events e WHERE e.capture_id=NEW.capture_id AND e.ordinal=1 AND e.event_hash=NEW.previous_hash) BEGIN SELECT RAISE(ABORT,'Attachment predecessor or identity mismatch'); END",
})
# Additive slot profile: prior genesis schema and records are not rewritten.
SLOT_SCHEMA = {
    'common_bank_slot_meta': 'CREATE TABLE common_bank_slot_meta(id INTEGER PRIMARY KEY CHECK(id=1),descriptor TEXT NOT NULL) WITHOUT ROWID',
    'common_bank_slot_intents': 'CREATE TABLE common_bank_slot_intents(capture_id TEXT PRIMARY KEY,event_json TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT UNIQUE NOT NULL,FOREIGN KEY(capture_id) REFERENCES common_bank_runs(capture_id)) WITHOUT ROWID',
    'common_bank_slot_attachments': 'CREATE TABLE common_bank_slot_attachments(capture_id TEXT PRIMARY KEY,event_json TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT UNIQUE NOT NULL,request_bytes BLOB NOT NULL,response_bytes BLOB,failure_json TEXT,FOREIGN KEY(capture_id) REFERENCES common_bank_slot_intents(capture_id)) WITHOUT ROWID',
}
for table in ('common_bank_slot_meta','common_bank_slot_intents','common_bank_slot_attachments'):
    for operation in ('UPDATE','DELETE'):
        SLOT_SCHEMA[table+'_'+operation.lower()] = f"CREATE TRIGGER {table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Immutable common bank slot'); END"
SLOT_SCHEMA.update({
    'common_bank_slot_meta_replace': "CREATE TRIGGER common_bank_slot_meta_replace BEFORE INSERT ON common_bank_slot_meta WHEN EXISTS(SELECT 1 FROM common_bank_slot_meta) BEGIN SELECT RAISE(ABORT,'Immutable slot metadata'); END",
    'common_bank_slot_intent_insert': "CREATE TRIGGER common_bank_slot_intent_insert BEFORE INSERT ON common_bank_slot_intents WHEN EXISTS(SELECT 1 FROM common_bank_slot_intents WHERE capture_id=NEW.capture_id OR event_hash=NEW.event_hash) OR NOT EXISTS(SELECT 1 FROM common_bank_attachments a JOIN common_bank_runs r ON r.capture_id=a.capture_id JOIN ownership_budgets b ON b.id=r.budget_id WHERE a.capture_id=NEW.capture_id AND a.event_hash=NEW.previous_hash AND json_extract(a.event_json,'$.state')='DONE' AND json_valid(NEW.event_json) AND json_extract(NEW.event_json,'$.used_after')=b.used AND json_extract(NEW.event_json,'$.state')='PENDING') BEGIN SELECT RAISE(ABORT,'Slot predecessor or charge mismatch'); END",
    'common_bank_slot_attachment_insert': "CREATE TRIGGER common_bank_slot_attachment_insert BEFORE INSERT ON common_bank_slot_attachments WHEN EXISTS(SELECT 1 FROM common_bank_slot_attachments WHERE capture_id=NEW.capture_id OR event_hash=NEW.event_hash) OR NOT EXISTS(SELECT 1 FROM common_bank_slot_intents i WHERE i.capture_id=NEW.capture_id AND i.event_hash=NEW.previous_hash) BEGIN SELECT RAISE(ABORT,'Slot attachment predecessor mismatch'); END",
})
UNION_SCHEMA = {
    'common_bank_union_meta': 'CREATE TABLE common_bank_union_meta(id INTEGER PRIMARY KEY CHECK(id=1),descriptor TEXT NOT NULL) WITHOUT ROWID',
    'common_bank_union_intents': 'CREATE TABLE common_bank_union_intents(capture_id TEXT PRIMARY KEY,event_json TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT UNIQUE NOT NULL,FOREIGN KEY(capture_id) REFERENCES common_bank_runs(capture_id)) WITHOUT ROWID',
    'common_bank_union_attachments': 'CREATE TABLE common_bank_union_attachments(capture_id TEXT PRIMARY KEY,event_json TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT UNIQUE NOT NULL,request_bytes BLOB NOT NULL,response_bytes BLOB,failure_json TEXT,FOREIGN KEY(capture_id) REFERENCES common_bank_union_intents(capture_id)) WITHOUT ROWID',
}
for table in ('common_bank_union_meta','common_bank_union_intents','common_bank_union_attachments'):
    for operation in ('UPDATE','DELETE'):
        UNION_SCHEMA[table+'_'+operation.lower()] = f"CREATE TRIGGER {table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Immutable common bank union'); END"
UNION_SCHEMA.update({
    'common_bank_union_meta_replace': "CREATE TRIGGER common_bank_union_meta_replace BEFORE INSERT ON common_bank_union_meta WHEN EXISTS(SELECT 1 FROM common_bank_union_meta) BEGIN SELECT RAISE(ABORT,'Immutable union metadata'); END",
    'common_bank_union_intent_insert': "CREATE TRIGGER common_bank_union_intent_insert BEFORE INSERT ON common_bank_union_intents WHEN EXISTS(SELECT 1 FROM common_bank_union_intents WHERE capture_id=NEW.capture_id OR event_hash=NEW.event_hash) OR NOT EXISTS(SELECT 1 FROM common_bank_slot_attachments a JOIN common_bank_runs r ON r.capture_id=a.capture_id JOIN ownership_budgets b ON b.id=r.budget_id WHERE a.capture_id=NEW.capture_id AND a.event_hash=NEW.previous_hash AND json_extract(a.event_json,'$.state')='DONE' AND json_valid(NEW.event_json) AND json_extract(NEW.event_json,'$.used_after')=b.used AND json_extract(NEW.event_json,'$.state')='PENDING') BEGIN SELECT RAISE(ABORT,'Union predecessor or charge mismatch'); END",
    'common_bank_union_attachment_insert': "CREATE TRIGGER common_bank_union_attachment_insert BEFORE INSERT ON common_bank_union_attachments WHEN EXISTS(SELECT 1 FROM common_bank_union_attachments WHERE capture_id=NEW.capture_id OR event_hash=NEW.event_hash) OR NOT EXISTS(SELECT 1 FROM common_bank_union_intents i WHERE i.capture_id=NEW.capture_id AND i.event_hash=NEW.previous_hash) BEGIN SELECT RAISE(ABORT,'Union attachment predecessor mismatch'); END",
})
CLOCK_SCHEMA = {
    'common_bank_clock_meta': 'CREATE TABLE common_bank_clock_meta(id INTEGER PRIMARY KEY CHECK(id=1),descriptor TEXT NOT NULL) WITHOUT ROWID',
    'common_bank_clock_intents': 'CREATE TABLE common_bank_clock_intents(capture_id TEXT PRIMARY KEY,event_json TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT UNIQUE NOT NULL,FOREIGN KEY(capture_id) REFERENCES common_bank_runs(capture_id)) WITHOUT ROWID',
    'common_bank_clock_attachments': 'CREATE TABLE common_bank_clock_attachments(capture_id TEXT PRIMARY KEY,event_json TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT UNIQUE NOT NULL,request_bytes BLOB NOT NULL,response_bytes BLOB,failure_json TEXT,FOREIGN KEY(capture_id) REFERENCES common_bank_clock_intents(capture_id)) WITHOUT ROWID',
}
for table in ('common_bank_clock_meta','common_bank_clock_intents','common_bank_clock_attachments'):
    for operation in ('UPDATE','DELETE'):
        CLOCK_SCHEMA[table+'_'+operation.lower()] = f"CREATE TRIGGER {table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Immutable common bank clock'); END"
CLOCK_SCHEMA.update({
    'common_bank_clock_meta_replace': "CREATE TRIGGER common_bank_clock_meta_replace BEFORE INSERT ON common_bank_clock_meta WHEN EXISTS(SELECT 1 FROM common_bank_clock_meta) BEGIN SELECT RAISE(ABORT,'Immutable clock metadata'); END",
    'common_bank_clock_intent_insert': "CREATE TRIGGER common_bank_clock_intent_insert BEFORE INSERT ON common_bank_clock_intents WHEN EXISTS(SELECT 1 FROM common_bank_clock_intents WHERE capture_id=NEW.capture_id OR event_hash=NEW.event_hash) OR NOT EXISTS(SELECT 1 FROM common_bank_union_attachments a JOIN common_bank_runs r ON r.capture_id=a.capture_id JOIN ownership_budgets b ON b.id=r.budget_id WHERE a.capture_id=NEW.capture_id AND a.event_hash=NEW.previous_hash AND json_extract(a.event_json,'$.state')='DONE' AND json_valid(NEW.event_json) AND json_extract(NEW.event_json,'$.used_after')=b.used AND json_extract(NEW.event_json,'$.state')='PENDING') BEGIN SELECT RAISE(ABORT,'Clock predecessor or charge mismatch'); END",
    'common_bank_clock_attachment_insert': "CREATE TRIGGER common_bank_clock_attachment_insert BEFORE INSERT ON common_bank_clock_attachments WHEN EXISTS(SELECT 1 FROM common_bank_clock_attachments WHERE capture_id=NEW.capture_id OR event_hash=NEW.event_hash) OR NOT EXISTS(SELECT 1 FROM common_bank_clock_intents i WHERE i.capture_id=NEW.capture_id AND i.event_hash=NEW.previous_hash) BEGIN SELECT RAISE(ABORT,'Clock attachment predecessor mismatch'); END",
})
FAILURE_CATEGORIES = frozenset(('TRANSPORT_TIMEOUT', 'TRANSPORT_ERROR', 'CANCELLED'))


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    def constant(value): raise ValueError('Nonfinite JSON')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)


class JournalBlocked(ValueError):
    pass


def _need(condition, reason):
    if not condition:
        raise JournalBlocked(reason)


def _identity(value):
    return (type(value) is str and 1 <= len(value) <= 128
            and all(c.isascii() and (c.isalnum() or c in '_.:-') for c in value))


def _file(path):
    info = path.stat()
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
          and not info.st_mode & 0o022, 'UNSAFE_COMMON_BANK_FILE_IDENTITY')
    return [info.st_dev, info.st_ino]


class CommonBankJournal:
    def __init__(self, research_db, evidence_db, planned_source):
        self.research = canonical_job_path(research_db)
        self.path = canonical_ownership_path(evidence_db)
        _need(self.research.exists() and self.path.exists() and not self.research.samefile(self.path),
              'EXISTING_DISTINCT_COMMON_BANK_DATABASES_REQUIRED')
        _need(type(planned_source) is ApprovedSource and _identity(planned_source.source_id)
              and planned_source.source_kind in ('coordinator_capture', 'synthetic_fixture')
              and (planned_source.network, planned_source.genesis_hash, planned_source.profile)
                  == (NETWORK, GENESIS, PROFILE), 'PLANNED_SOURCE_CONFIGURATION_INVALID')
        self.source = asdict(planned_source)
        self.descriptor = {'version': 1, 'kind': 'common_bank_journal_v1',
            'planned_source':self.source,
            'research_path': str(self.research), 'research_identity': _file(self.research),
            'evidence_path': str(self.path), 'evidence_identity': _file(self.path)}

    def _guard(self):
        _need(canonical_job_path(self.research) == self.research
              and canonical_ownership_path(self.path) == self.path
              and _file(self.research) == self.descriptor['research_identity']
              and _file(self.path) == self.descriptor['evidence_identity'],
              'COMMON_BANK_DATABASE_IDENTITY_CHANGED')

    @contextmanager
    def locked(self):
        """Research worker -> invocation -> request; no nested public lock APIs.

        Session capability expires on exit and is bound to this PID/thread.
        All contention is nonblocking. No transaction spans external I/O.
        """
        self._guard(); locks = []; session = None
        try:
            for name in (str(self.research)+'.jobs-worker.lock',
                         ownership_lock_path(self,invocation=True), ownership_lock_path(self)):
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                locks.append(fd); info = os.fstat(fd)
                _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
                      and not info.st_mode & 0o022, 'UNSAFE_COMMON_BANK_LOCK')
                try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise JournalBlocked('COMMON_BANK_BUSY') from None
            with read_guard(self.research):
                self._guard(); session = _Session(self)
                yield session
                self._guard()
        finally:
            if session is not None:
                session.active = False
                session._inflight.clear()
            for fd in reversed(locks): os.close(fd)


class _Session:
    def __init__(self, journal):
        self.journal = journal; self.active = True
        self.owner = (os.getpid(), threading.get_ident())
        self._inflight = {}

    @contextmanager
    def _transaction(self, *, initialize=False):
        _need(self.active and self.owner == (os.getpid(), threading.get_ident()),
              'COMMON_BANK_SESSION_EXPIRED_OR_WRONG_OWNER')
        j = self.journal; j._guard()
        with closing(sqlite3.connect(j.path.as_uri()+'?mode=rw',uri=True,timeout=0,isolation_level=None)) as c:
            _need(c.execute('PRAGMA journal_mode').fetchone()[0] == 'delete', 'COMMON_BANK_ROLLBACK_JOURNAL_REQUIRED')
            c.execute('PRAGMA synchronous=FULL'); c.execute('PRAGMA foreign_keys=ON')
            c.execute('BEGIN IMMEDIATE')
            try:
                self._schema(c,initialize)
                yield c
                j._guard(); c.commit()
            except BaseException: c.rollback(); raise

    def _schema(self, c, initialize):
        for table, expected in EXISTING_COLUMNS.items():
            columns = c.execute(f'PRAGMA table_info({table})').fetchall()
            _need([(r[1],r[2],r[3],r[5]) for r in columns] == expected
                  and all(r[4] is None for r in columns), 'EXISTING_ADMISSION_SCHEMA_MISMATCH')
        _need(not c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('ownership_budgets','ownership_admissions','pages') LIMIT 1").fetchone(),
              'UNREVIEWED_EXISTING_ADMISSION_TRIGGER')
        marker = c.execute('PRAGMA application_id').fetchone()[0]
        sql = "SELECT name,sql FROM sqlite_master WHERE name LIKE 'common_bank_%' OR (type='trigger' AND tbl_name IN ('common_bank_meta','common_bank_runs','common_bank_events','common_bank_attachment_meta','common_bank_attachments','common_bank_slot_meta','common_bank_slot_intents','common_bank_slot_attachments','common_bank_union_meta','common_bank_union_intents','common_bank_union_attachments','common_bank_clock_meta','common_bank_clock_intents','common_bank_clock_attachments'))"
        actual = dict(c.execute(sql))
        if not actual and marker == 0 and initialize:
            _need(c.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('ownership_budgets','ownership_admissions','pages')").fetchone()[0] == 3,
                  'EXISTING_ADMISSION_SCHEMA_REQUIRED')
            for statement in SCHEMA.values(): c.execute(statement)
            c.execute('INSERT INTO common_bank_meta VALUES(1,?)',(canonical(self.journal.descriptor),))
            c.execute(f'PRAGMA application_id={APPLICATION_ID}')
            actual = dict(c.execute(sql))
        version = c.execute('PRAGMA user_version').fetchone()[0]
        _need(version in (0, ATTACHMENT_VERSION, SLOT_VERSION, UNION_VERSION, CLOCK_VERSION), 'COMMON_BANK_ATTACHMENT_VERSION_UNKNOWN')
        expected = SCHEMA | ATTACHMENT_SCHEMA if version != 0 else SCHEMA
        if version in (SLOT_VERSION,UNION_VERSION,CLOCK_VERSION): expected = expected | SLOT_SCHEMA
        if version in (UNION_VERSION,CLOCK_VERSION): expected = expected | UNION_SCHEMA
        if version == CLOCK_VERSION: expected = expected | CLOCK_SCHEMA
        _need(marker in (0, APPLICATION_ID) and actual == expected, 'COMMON_BANK_SCHEMA_OR_TRIGGER_MISMATCH')
        _need(c.execute('PRAGMA application_id').fetchone()[0] == APPLICATION_ID,
              'COMMON_BANK_INSTALLATION_MARKER_MISMATCH')
        _need(c.execute('SELECT id,descriptor FROM common_bank_meta').fetchall()
              == [(1,canonical(self.journal.descriptor))], 'COMMON_BANK_METADATA_MISMATCH')
        if version != 0:
            _need(c.execute('SELECT id,descriptor FROM common_bank_attachment_meta').fetchall()
                  == [(1,canonical(self._attachment_descriptor()))], 'COMMON_BANK_ATTACHMENT_METADATA_MISMATCH')
        if version in (SLOT_VERSION,UNION_VERSION,CLOCK_VERSION):
            _need(c.execute('SELECT id,descriptor FROM common_bank_slot_meta').fetchall()
                  == [(1,canonical(self._slot_descriptor()))], 'COMMON_BANK_SLOT_METADATA_MISMATCH')
        if version in (UNION_VERSION,CLOCK_VERSION):
            _need(c.execute('SELECT id,descriptor FROM common_bank_union_meta').fetchall()
                  == [(1,canonical(self._union_descriptor()))], 'COMMON_BANK_UNION_METADATA_MISMATCH')
        if version == CLOCK_VERSION:
            _need(c.execute('SELECT id,descriptor FROM common_bank_clock_meta').fetchall()
                  == [(1,canonical(self._clock_descriptor()))], 'COMMON_BANK_CLOCK_METADATA_MISMATCH')

    def _source(self, c, budget):
        size = c.execute('SELECT length(descriptor),length(prepared_source) FROM ownership_admissions WHERE id=?',(budget,)).fetchone()
        _need(size is not None and type(size[0]) is int and size[0] <= 8192
              and type(size[1]) is int and size[1] <= 2*1024*1024,
              'BOUNDED_EXISTING_SEALED_SOURCE_REQUIRED')
        admission = HistoryProgress.inspect_admission(c,budget)
        _need(admission is not None and admission['state'] == 'SEALED'
              and admission['request_ceiling'] == 18, 'EXISTING_SEALED_18_BUDGET_REQUIRED')
        with closing(sqlite3.connect(self.journal.research.as_uri()+'?mode=ro',uri=True)) as source:
            source.row_factory = sqlite3.Row
            row = source.execute('SELECT id,mint,created,status,result FROM scans WHERE id=?',(budget,)).fetchone()
        _need(row is not None and dict(row) == admission['prepared_source'], 'EXACT_SEALED_SOURCE_REQUIRED')
        scan = dict(row); report = json.loads(scan['result'])
        raw = c.execute('SELECT descriptor,state,prepared_source,prepared_used,completed_source_hash FROM ownership_admissions WHERE id=?',(budget,)).fetchone()
        counters = c.execute('SELECT source_hash,used,ceiling FROM ownership_budgets WHERE id=?',(budget,)).fetchone()
        _validate_continuation_source(scan,report,counters,raw)
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='ownership_banks'").fetchone():
            _need(not c.execute('SELECT 1 FROM ownership_banks WHERE budget=?',(budget,)).fetchone(),
                  'CANONICAL_BANK_ALREADY_FIXED')
        return admission, scan, report

    def _plan(self, c, budget, view):
        admission, scan, report = self._source(c,budget)
        # Track this plan's refs separately while sharing the aggregate bounded
        # physical cache across every audited run.
        class Reads:
            def __init__(self): self.requested = set()
            def load(self,key):
                self.requested.add(key)
                return view.load(key)
        reads = Reads()
        mint = scan['mint']; original = reads.load(report['mint_evidence_hash'])
        _need(original.get('method') == 'getAccountInfo' and original.get('params') ==
              [mint,{'encoding':'base64','commitment':'confirmed'}]
              and mint_policy(original['result']['value'])['decision'] == 'PASS_TOKEN_POLICY',
              'LEGACY_SOURCE_MINT_POLICY_REQUIRED')
        history = reconstruct_launch_history(report,reads)
        _need(history['inventory']['initialization_inventory_verified'], 'RAW_DISCOVERY_FRONTIER_UNVERIFIED')
        frontier = sorted(a['address'] for a in history['inventory']['accounts'])
        _need(frontier and len(frontier) <= 99 and len(frontier) == len(set(frontier))
              and mint not in frontier, 'RAW_FRONTIER_INVALID_OR_OVERSIZED')
        for row in history['inventory']['accounts']:
            _need(row['initializations'] and all(x['program'] == TOKEN_PROGRAM for x in row['initializations']),
                  'FRONTIER_LEGACY_PROGRAM_REQUIRED')
        for account in frontier: address(account)
        pool = canonical_accounts(mint)
        _need(pool[4] in frontier and not set(frontier).intersection(pool[:4]+pool[5:]),
              'BASE_VAULT_FRONTIER_MEMBERSHIP_REQUIRED')
        union = pool + sorted(set(frontier)-set(pool))
        _need(len(union) <= 100, 'COMMON_BANK_UNION_TOO_LARGE')
        plan = {'kind':'common_bank_plan_source_discovery_v1','mint':mint,'F':frontier,'U':union,
            'pool_indices':list(range(6)), 'ownership_indices':[union.index(mint)]+[union.index(a) for a in frontier],
            'discovery_revision_hash':admission['completed_source_hash'],
            'discovery_query_hashes':sorted(digest(q) for q in report['history_queries']),
            'discovery_evidence_hashes':sorted(reads.requested)}
        return admission, plan

    def _audit(self,c):
        _need(not c.execute('SELECT 1 FROM common_bank_runs WHERE length(descriptor_json)>8192 OR length(plan_json)>65536 LIMIT 1').fetchone()
              and not c.execute('SELECT 1 FROM common_bank_events WHERE length(event_json)>8192 LIMIT 1').fetchone(),
              'COMMON_BANK_JOURNAL_RECORD_OVERSIZED')
        rows = c.execute('SELECT capture_id,budget_id,descriptor_json,plan_json,plan_hash,seed_hash FROM common_bank_runs LIMIT ?',
                         (MAX_RUNS+1,)).fetchall()
        _need(len(rows) <= MAX_RUNS, 'COMMON_BANK_RUN_CAPACITY')
        events = c.execute('SELECT capture_id,ordinal,event_json,previous_hash,event_hash FROM common_bank_events LIMIT ?',
                           (MAX_RUNS+1,)).fetchall()
        _need(len(events) <= MAX_RUNS, 'COMMON_BANK_EVENT_CAPACITY')
        view = ReplayView(_BoundedStore(self.journal.path)); runs = {}
        for capture, budget, desc_json, plan_json, plan_hash, seed in rows:
            _need(_identity(capture) and _identity(budget), 'COMMON_BANK_RUN_ID_INVALID')
            admission, plan = self._plan(c,budget,view)
            descriptor = json.loads(desc_json)
            _need(type(descriptor) is dict and set(descriptor) == {'kind','capture_id','budget_id','admission_descriptor_hash','completed_source_hash','initial_used','planned_source','database_binding'},
                  'COMMON_BANK_DESCRIPTOR_INVALID')
            _need(descriptor['kind'] == 'common_bank_run_v1' and descriptor['capture_id'] == capture
                  and descriptor['budget_id'] == budget and descriptor['admission_descriptor_hash'] == admission['descriptor_hash']
                  and descriptor['completed_source_hash'] == admission['completed_source_hash']
                  and canonical(descriptor['database_binding']) == canonical(self.journal.descriptor)
                  and descriptor['planned_source'] == self.journal.source
                  and type(descriptor['initial_used']) is int
                  and admission['prepared_requests_used'] <= descriptor['initial_used'] <= admission['requests_used'] <= 18,
                  'COMMON_BANK_DESCRIPTOR_SOURCE_MISMATCH')
            _need(canonical(descriptor) == desc_json and canonical(plan) == plan_json and digest(plan) == plan_hash
                  and digest({'descriptor':descriptor,'plan_hash':plan_hash}) == seed,
                  'COMMON_BANK_PLAN_OR_SEED_MISMATCH')
            runs[capture] = dict(budget=budget,descriptor=descriptor,plan=plan,seed=seed,
                                 used=admission['requests_used'],event=None,completion=None,slot_intent=None,slot_completion=None,union_intent=None,union_completion=None,clock_intent=None,clock_completion=None)
        for capture, ordinal, event_json, previous, event_hash in events:
            _need(capture in runs and ordinal == 1 and runs[capture]['event'] is None,
                  'COMMON_BANK_EVENT_IDENTITY_INVALID')
            run = runs[capture]; event = json.loads(event_json)
            _need(type(event) is dict, 'COMMON_BANK_PENDING_INTENT_INVALID')
            expected = self._intent(run,capture,event.get('used_after') if type(event) is dict else None,
                                    event.get('reserved_at') if type(event) is dict else None)
            _need(type(event.get('used_after')) is int and run['descriptor']['initial_used'] < event['used_after'] <= run['used']
                  and type(event.get('reserved_at')) is int and 0 <= event['reserved_at'] < 2**63
                  and canonical(expected) == event_json and previous == run['seed']
                  and digest({'previous_hash':previous,'event':event}) == event_hash,
                  'COMMON_BANK_PENDING_INTENT_INVALID')
            run['event'] = event
        self._audit_attachments(c,runs)
        self._audit_slots(c,runs)
        self._audit_unions(c,runs)
        self._audit_clocks(c,runs)
        return runs

    @staticmethod
    def _intent(run,capture,used,at):
        return {'kind':'common_bank_pending_v1','capture_id':capture,'budget_id':run['budget'],
                'stage':'genesis','state':'PENDING','method':'getGenesisHash','params':[],
                'descriptor_hash':digest(run['descriptor']),'plan_hash':digest(run['plan']),
                'fence':run['seed'],'ordinal':1,'used_after':used,'reserved_at':at}

    def create_run(self,budget_id,capture_id):
        _need(_identity(budget_id) and _identity(capture_id), 'COMMON_BANK_QUERY_ID_INVALID')
        with self._transaction(initialize=True) as c:
            runs = self._audit(c)
            view = ReplayView(_BoundedStore(self.journal.path))
            admission, plan = self._plan(c,budget_id,view)
            existing = next((key for key,r in runs.items() if r['budget'] == budget_id),None)
            if existing is not None:
                _need(existing == capture_id, 'COMMON_BANK_BUDGET_ALREADY_BOUND')
                return self._status(runs[capture_id],capture_id)
            _need(capture_id not in runs and len(runs) < MAX_RUNS, 'COMMON_BANK_CAPTURE_ID_ALREADY_BOUND')
            # Four setup stages plus at least one mint page and one per holder.
            _need(admission['requests_used']+5+len(plan['F']) <= 18, 'COMMON_BANK_LOWER_BOUND_EXCEEDS_18')
            descriptor = {'kind':'common_bank_run_v1','capture_id':capture_id,'budget_id':budget_id,
                'admission_descriptor_hash':admission['descriptor_hash'],
                'completed_source_hash':admission['completed_source_hash'],
                'initial_used':admission['requests_used'],'planned_source':self.journal.source,
                'database_binding':self.journal.descriptor}
            plan_hash = digest(plan); seed = digest({'descriptor':descriptor,'plan_hash':plan_hash})
            c.execute('INSERT INTO common_bank_runs VALUES(?,?,?,?,?,?)',
                      (capture_id,budget_id,canonical(descriptor),canonical(plan),plan_hash,seed))
            run = dict(budget=budget_id,descriptor=descriptor,plan=plan,seed=seed,
                       used=admission['requests_used'],event=None,completion=None,slot_intent=None,slot_completion=None,union_intent=None,union_completion=None,clock_intent=None,clock_completion=None)
            return self._status(run,capture_id)

    def reserve_stage(self,capture_id,stage,method,params,*,fence,reserved_at):
        """Atomic charge+PENDING only. DONE/next-stage/provider APIs are deferred."""
        with self._transaction() as c:
            runs = self._audit(c)
            _need(capture_id in runs, 'COMMON_BANK_CAPTURE_MISSING'); run = runs[capture_id]
            _need(run['event'] is None, 'COMMON_BANK_PENDING_TERMINAL_UNCERTAINTY')
            _need(type(fence) is str and fence == run['seed'], 'COMMON_BANK_STALE_FENCE')
            _need(stage == 'genesis' and method == 'getGenesisHash' and type(params) is list and params == [],
                  'COMMON_BANK_NEXT_STAGE_OR_REQUEST_MISMATCH')
            _need(type(reserved_at) is int and 0 <= reserved_at < 2**63, 'COMMON_BANK_RESERVATION_TIME_INVALID')
            _need(run['used']+5+len(run['plan']['F']) <= 18, 'COMMON_BANK_LOWER_BOUND_EXCEEDS_18')
            changed = c.execute('UPDATE ownership_budgets SET used=used+1 WHERE id=? AND source_hash=? AND ceiling=18 AND used<18',
                (run['budget'],run['descriptor']['admission_descriptor_hash'])).rowcount
            _need(changed == 1, 'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED')
            used = c.execute('SELECT used FROM ownership_budgets WHERE id=?',(run['budget'],)).fetchone()[0]
            event = self._intent(run,capture_id,used,reserved_at)
            c.execute('INSERT INTO common_bank_events VALUES(?,?,?,?,?)',
                      (capture_id,1,canonical(event),run['seed'],digest({'previous_hash':run['seed'],'event':event})))
            return event

    def _status(self,run,capture):
        return {'capture_id':capture,'budget_id':run['budget'],'fence':run['seed'],
                'plan':run['plan'],'requests_used':run['used'],
                'state': ('CLOCK_'+run['clock_completion']['state'] if run['clock_completion'] else
                          'CLOCK_IN_FLIGHT' if run['clock_intent'] and any(v['capture']==capture and v['stage']=='clock' for v in self._inflight.values()) else
                          'TERMINAL_UNCERTAINTY' if run['clock_intent'] else
                          'UNION_'+run['union_completion']['state'] if run['union_completion'] else
                          'UNION_IN_FLIGHT' if run['union_intent'] and any(v['capture']==capture and v['stage']=='union' for v in self._inflight.values()) else
                          'TERMINAL_UNCERTAINTY' if run['union_intent'] else
                          'SLOT_'+run['slot_completion']['state'] if run['slot_completion'] else
                          'SLOT_IN_FLIGHT' if run['slot_intent'] and any(v['capture']==capture and v['stage']=='slot' for v in self._inflight.values()) else
                          'TERMINAL_UNCERTAINTY' if run['slot_intent'] else
                          'GENESIS_'+run['completion']['state'] if run['completion'] else
                          'GENESIS_IN_FLIGHT' if any(v['capture']==capture for v in self._inflight.values()) else
                          'TERMINAL_UNCERTAINTY' if run['event'] else 'READY_GENESIS'),
                'completion':run['completion'],'slot_intent':run['slot_intent'],
                'slot_completion':run['slot_completion'],'union_intent':run['union_intent'],
                'union_completion':run['union_completion'],'clock_intent':run['clock_intent'],
                'clock_completion':run['clock_completion'],
                'clock_fence':self._union_hash(run) if run['union_completion'] and run['union_completion']['state']=='DONE' else None,
                'union_fence':self._slot_hash(run) if run['slot_completion'] and run['slot_completion']['state']=='DONE' else None,
                'slot_fence':self._genesis_hash(run) if run['completion'] and run['completion']['state']=='DONE' else None,
                'intent':run['event'],'provider_calls':0,'eligible_for_trading':False,
                'ownership_approval':False,'chain_authenticated':False}

    def resume_capture(self,budget_id):
        with self._transaction() as c:
            runs = self._audit(c)
            matches = [(key,r) for key,r in runs.items() if r['budget'] == budget_id]
            _need(len(matches) == 1, 'COMMON_BANK_CAPTURE_MISSING')
            key, run = matches[0]
            return self._status(run,key)


    def _attachment_descriptor(self):
        return {'kind':'common_bank_genesis_attachment_profile_v1',
                'database_binding':self.journal.descriptor,'parent_schema_hash':digest(SCHEMA)}

    def install_genesis_attachments(self):
        """Explicit additive installation; never retrofits v1 PENDING authority."""
        with self._transaction() as c:
            self._audit(c)
            if c.execute('PRAGMA user_version').fetchone()[0] in (ATTACHMENT_VERSION,SLOT_VERSION,UNION_VERSION,CLOCK_VERSION):
                return
            for statement in ATTACHMENT_SCHEMA.values(): c.execute(statement)
            c.execute('INSERT INTO common_bank_attachment_meta VALUES(1,?)',
                      (canonical(self._attachment_descriptor()),))
            c.execute(f'PRAGMA user_version={ATTACHMENT_VERSION}')
            self._schema(c,False)

    def begin_genesis(self,capture_id,*,fence,reserved_at):
        """Mint an opaque handle only for this session's newly committed intent.

        The returned object is not serializable recovery authority. Its request
        must be sent unchanged by a future reviewed transport, not this module.
        """
        with self._transaction() as c:
            _need(c.execute('PRAGMA user_version').fetchone()[0] in (ATTACHMENT_VERSION,SLOT_VERSION,UNION_VERSION,CLOCK_VERSION),
                  'EXPLICIT_GENESIS_ATTACHMENT_INSTALL_REQUIRED')
        intent = self.reserve_stage(capture_id,'genesis','getGenesisHash',[],
                                    fence=fence,reserved_at=reserved_at)
        token = object()
        request = self._request(intent)
        self._inflight[token] = {'capture':capture_id,'stage':'genesis','intent':canonical(intent),
                                 'request':request,'chosen':None}
        return token

    @staticmethod
    def _request(intent):
        return canonical({'jsonrpc':'2.0','id':digest(intent),
                          'method':'getGenesisHash','params':[]}).encode('utf-8')

    def _flight(self,token,stage='genesis'):
        _need(self.active and self.owner == (os.getpid(),threading.get_ident()),
              'COMMON_BANK_SESSION_EXPIRED_OR_WRONG_OWNER')
        _need(type(token) is object and token in self._inflight,
              'COMMON_BANK_UNKNOWN_INFLIGHT_CAPABILITY')
        self.journal._guard()
        _need(self._inflight[token]['stage'] == stage, 'COMMON_BANK_INFLIGHT_STAGE_MISMATCH')
        return self._inflight[token]

    def genesis_request(self,token):
        """Original generated wire request, not a caller-provided summary."""
        return self._flight(token)['request']

    @staticmethod
    def _failure(category,*,observed_bytes=None,stage='genesis'):
        failure = {'kind':'common_bank_redacted_failure_v1','category':category,
                   'method':'getGenesisHash' if stage=='genesis' else 'getSlot',
                   'params':[] if stage=='genesis' else [{'commitment':'finalized'}]}
        if observed_bytes is not None: failure['observed_bytes'] = observed_bytes
        return canonical(failure)

    @staticmethod
    def _outcome(request,response,failure,stage='genesis'):
        if failure is not None:
            _need(type(failure) is str and len(failure.encode()) <= MAX_FAILURE_BYTES,
                  'COMMON_BANK_FAILURE_PROVENANCE_INVALID')
            decoded = json.loads(failure)
            _need(type(decoded) is dict, 'COMMON_BANK_FAILURE_PROVENANCE_INVALID')
            category = decoded.get('category')
            if category == 'RESPONSE_OVERSIZED':
                size = decoded.get('observed_bytes')
                _need(type(size) is int and MAX_RESPONSE_BYTES < size < 2**63
                      and failure == _Session._failure(category,observed_bytes=size,stage=stage),
                      'COMMON_BANK_FAILURE_PROVENANCE_INVALID')
            else:
                _need(category in FAILURE_CATEGORIES and failure == _Session._failure(category,stage=stage),
                      'COMMON_BANK_FAILURE_PROVENANCE_INVALID')
            _need(response is None, 'COMMON_BANK_ATTACHMENT_PROVENANCE_AMBIGUOUS')
            return 'FAILED',category
        _need(type(response) is bytes and len(response) <= MAX_RESPONSE_BYTES,
              'COMMON_BANK_RAW_RESPONSE_REQUIRED')
        try:
            body = _strict_json(response)
            expected_id = _strict_json(request)['id']
            if (type(body) is not dict or body.get('jsonrpc') != '2.0'
                    or type(body.get('id')) is not str or body['id'] != expected_id):
                return 'FAILED','RESPONSE_INVALID'
            if set(body) == {'jsonrpc','id','error'}:
                return 'FAILED','RPC_ERROR'
            if set(body) != {'jsonrpc','id','result'}:
                return 'FAILED','RESPONSE_INVALID'
            if stage == 'union': return 'DONE','UNION_FRAMING_VALID'
            if stage == 'clock':
                return ('DONE','BLOCK_TIME_DECLARED') if type(body['result']) is int and 0<=body['result']<2**63 else ('FAILED','CLOCK_TIME_INVALID')
            if stage == 'slot':
                return ('DONE','FINALIZED_SLOT_DECLARED') if type(body['result']) is int and 0 <= body['result'] < 2**64 else ('FAILED','SLOT_INVALID')
            return ('DONE','EXACT_GENESIS') if type(body['result']) is str and body['result'] == GENESIS else ('FAILED','GENESIS_MISMATCH')
        except (ValueError,RecursionError):
            return 'FAILED','RESPONSE_INVALID'

    @staticmethod
    def _attachment(run,capture,request,response,failure,completed_at):
        intent = run['event']
        _need(intent is not None and type(completed_at) is int
              and intent['reserved_at'] <= completed_at < 2**63,
              'COMMON_BANK_COMPLETION_TIME_OR_PREDECESSOR_INVALID')
        _need(type(request) is bytes and len(request) <= MAX_REQUEST_BYTES
              and request == _Session._request(intent), 'COMMON_BANK_ORIGINAL_REQUEST_MISMATCH')
        state,reason = _Session._outcome(request,response,failure)
        intent_hash = digest({'previous_hash':run['seed'],'event':intent})
        event = {'kind':'common_bank_genesis_attachment_v1','capture_id':capture,
                 'budget_id':run['budget'],'stage':'genesis','ordinal':2,'state':state,'reason':reason,
                 'intent_hash':intent_hash,'fence':run['seed'],
                 'descriptor_hash':digest(run['descriptor']),'plan_hash':digest(run['plan']),
                 'planned_source':run['descriptor']['planned_source'],
                 'used_after':intent['used_after'],'reserved_at':intent['reserved_at'],
                 'completed_at':completed_at,'request_sha256':_sha(request),
                 'response_sha256':_sha(response) if response is not None else None,
                 'failure_hash':digest(json.loads(failure)) if failure is not None else None,
                 'eligible_for_trading':False,'ownership_approval':False,'chain_authenticated':False}
        return event,intent_hash,digest({'previous_hash':intent_hash,'event':event})

    def _audit_attachments(self,c,runs):
        if c.execute('PRAGMA user_version').fetchone()[0] == 0: return
        _need(not c.execute('SELECT 1 FROM common_bank_attachments WHERE length(event_json)>8192 OR length(request_bytes)>? OR length(response_bytes)>? OR length(failure_json)>? LIMIT 1',
                            (MAX_REQUEST_BYTES,MAX_RESPONSE_BYTES,MAX_FAILURE_BYTES)).fetchone(),
              'COMMON_BANK_ATTACHMENT_RECORD_OVERSIZED')
        rows = c.execute('SELECT capture_id,event_json,previous_hash,event_hash,request_bytes,response_bytes,failure_json FROM common_bank_attachments LIMIT ?',
                         (MAX_RUNS+1,)).fetchall()
        _need(len(rows) <= MAX_RUNS, 'COMMON_BANK_ATTACHMENT_CAPACITY')
        for capture,event_json,previous,key,request,response,failure in rows:
            _need(capture in runs, 'COMMON_BANK_ATTACHMENT_CAPTURE_MISSING')
            run = runs[capture]; recorded = json.loads(event_json)
            _need(type(recorded) is dict, 'COMMON_BANK_ATTACHMENT_INVALID')
            expected,prior,event_hash = self._attachment(run,capture,request,response,failure,recorded.get('completed_at'))
            _need(canonical(expected) == event_json and previous == prior and key == event_hash,
                  'COMMON_BANK_ATTACHMENT_HASH_OR_BINDING_MISMATCH')
            run['completion'] = expected

    def attach_genesis_response(self,token,response_bytes,*,completed_at):
        """Retain exact bounded bytes; derive DONE/FAILED, never accept summaries."""
        _need(type(response_bytes) is bytes, 'COMMON_BANK_RAW_RESPONSE_REQUIRED')
        if len(response_bytes) > MAX_RESPONSE_BYTES:
            return self._finish_genesis(token,None,self._failure('RESPONSE_OVERSIZED',observed_bytes=len(response_bytes)),completed_at)
        return self._finish_genesis(token,response_bytes,None,completed_at)

    def attach_genesis_failure(self,token,category,*,completed_at):
        """Fixed redacted categories only: no URL, exception text or secrets."""
        _need(type(category) is str and category in FAILURE_CATEGORIES,
              'COMMON_BANK_FAILURE_CATEGORY_INVALID')
        return self._finish_genesis(token,None,self._failure(category),completed_at)

    def _finish_genesis(self,token,response,failure,at):
        flight = self._flight(token)
        with self._transaction() as c:
            _need(c.execute('PRAGMA user_version').fetchone()[0] in (ATTACHMENT_VERSION,SLOT_VERSION,UNION_VERSION,CLOCK_VERSION),
                  'EXPLICIT_GENESIS_ATTACHMENT_INSTALL_REQUIRED')
            runs = self._audit(c); capture = flight['capture']
            _need(capture in runs and canonical(runs[capture]['event']) == flight['intent'],
                  'COMMON_BANK_STALE_INFLIGHT_INTENT')
            event,previous,key = self._attachment(runs[capture],capture,flight['request'],response,failure,at)
            chosen = (canonical(event),previous,key,flight['request'],response,failure)
            _need(flight['chosen'] is None or flight['chosen'] == chosen,
                  'COMMON_BANK_CONFLICTING_COMPLETION')
            # Freeze the known observation before attempting commit. Only this
            # same observation can be attached again after an SQL commit error;
            # this never repeats I/O, and reopening cannot mint its capability.
            flight['chosen'] = chosen
            existing = c.execute('SELECT event_json,previous_hash,event_hash,request_bytes,response_bytes,failure_json FROM common_bank_attachments WHERE capture_id=?',(capture,)).fetchone()
            if existing is not None:
                _need(existing == chosen, 'COMMON_BANK_CONFLICTING_COMPLETION')
                return event
            c.execute('INSERT INTO common_bank_attachments VALUES(?,?,?,?,?,?,?)',(capture,)+chosen)
            return event


    def _slot_descriptor(self):
        return {'kind':'common_bank_slot_profile_v1','database_binding':self.journal.descriptor,
                'parent_schema_hash':digest(SCHEMA | ATTACHMENT_SCHEMA)}

    def install_slot_stage(self):
        """Explicit additive upgrade after genesis attachment profile install."""
        with self._transaction() as c:
            _need(c.execute('PRAGMA user_version').fetchone()[0] in (ATTACHMENT_VERSION,SLOT_VERSION,UNION_VERSION,CLOCK_VERSION),
                  'GENESIS_ATTACHMENT_PROFILE_REQUIRED')
            self._audit(c)
            if c.execute('PRAGMA user_version').fetchone()[0] in (SLOT_VERSION,UNION_VERSION,CLOCK_VERSION): return
            for statement in SLOT_SCHEMA.values(): c.execute(statement)
            c.execute('INSERT INTO common_bank_slot_meta VALUES(1,?)',(canonical(self._slot_descriptor()),))
            c.execute(f'PRAGMA user_version={SLOT_VERSION}')
            self._schema(c,False)

    @staticmethod
    def _genesis_hash(run):
        return digest({'previous_hash':run['completion']['intent_hash'],'event':run['completion']})

    @staticmethod
    def _slot_intent(run,capture,used,at):
        return {'kind':'common_bank_slot_pending_v1','capture_id':capture,'budget_id':run['budget'],
                'stage':'slot','state':'PENDING','method':'getSlot','params':[{'commitment':'finalized'}],
                'descriptor_hash':digest(run['descriptor']),'plan_hash':digest(run['plan']),
                'fence':_Session._genesis_hash(run),'ordinal':3,'used_after':used,'reserved_at':at}

    def begin_slot(self,capture_id,*,fence,reserved_at):
        _need(_identity(capture_id), 'COMMON_BANK_QUERY_ID_INVALID')
        with self._transaction() as c:
            _need(c.execute('PRAGMA user_version').fetchone()[0] in (SLOT_VERSION,UNION_VERSION,CLOCK_VERSION),
                  'EXPLICIT_SLOT_INSTALL_REQUIRED')
            runs = self._audit(c)
            _need(capture_id in runs, 'COMMON_BANK_CAPTURE_MISSING'); run = runs[capture_id]
            _need(run['completion'] is not None and run['completion']['state']=='DONE',
                  'AUDITED_GENESIS_DONE_REQUIRED')
            _need(run['slot_intent'] is None, 'COMMON_BANK_SLOT_ALREADY_RESERVED_TERMINAL')
            previous = self._genesis_hash(run)
            _need(type(fence) is str and fence==previous, 'COMMON_BANK_STALE_SLOT_FENCE')
            _need(type(reserved_at) is int and run['completion']['completed_at'] <= reserved_at < 2**63,
                  'COMMON_BANK_SLOT_RESERVATION_TIME_INVALID')
            # Slot + union + clock, then at least one mint and one per F page.
            _need(run['used']+4+len(run['plan']['F']) <= 18, 'COMMON_BANK_LOWER_BOUND_EXCEEDS_18')
            changed = c.execute('UPDATE ownership_budgets SET used=used+1 WHERE id=? AND source_hash=? AND ceiling=18 AND used<18',
                               (run['budget'],run['descriptor']['admission_descriptor_hash'])).rowcount
            _need(changed==1, 'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED')
            used = c.execute('SELECT used FROM ownership_budgets WHERE id=?',(run['budget'],)).fetchone()[0]
            intent = self._slot_intent(run,capture_id,used,reserved_at)
            c.execute('INSERT INTO common_bank_slot_intents VALUES(?,?,?,?)',
                      (capture_id,canonical(intent),previous,digest({'previous_hash':previous,'event':intent})))
        token=object()
        request=canonical({'jsonrpc':'2.0','id':digest(intent),'method':'getSlot',
                           'params':[{'commitment':'finalized'}]}).encode()
        self._inflight[token]={'capture':capture_id,'stage':'slot','intent':canonical(intent),
                               'request':request,'chosen':None}
        return token

    def slot_request(self,token):
        return self._flight(token,'slot')['request']

    @staticmethod
    def _slot_attachment(run,capture,request,response,failure,at):
        intent=run['slot_intent']
        _need(intent is not None and type(at) is int and intent['reserved_at'] <= at < 2**63,
              'COMMON_BANK_SLOT_COMPLETION_TIME_INVALID')
        expected_request=canonical({'jsonrpc':'2.0','id':digest(intent),'method':'getSlot',
                                    'params':[{'commitment':'finalized'}]}).encode()
        _need(type(request) is bytes and len(request)<=MAX_REQUEST_BYTES and request==expected_request,
              'COMMON_BANK_ORIGINAL_SLOT_REQUEST_MISMATCH')
        state,reason=_Session._outcome(request,response,failure,'slot')
        previous=digest({'previous_hash':intent['fence'],'event':intent})
        event={'kind':'common_bank_slot_attachment_v1','capture_id':capture,'budget_id':run['budget'],
               'stage':'slot','ordinal':4,'state':state,'reason':reason,'intent_hash':previous,
               'fence':intent['fence'],'descriptor_hash':digest(run['descriptor']),
               'plan_hash':digest(run['plan']),'planned_source':run['descriptor']['planned_source'],
               'used_after':intent['used_after'],'reserved_at':intent['reserved_at'],'completed_at':at,
               'request_sha256':_sha(request),'response_sha256':_sha(response) if response is not None else None,
               'failure_hash':digest(json.loads(failure)) if failure is not None else None,
               'slot':_strict_json(response)['result'] if state=='DONE' else None,
               'eligible_for_trading':False,'ownership_approval':False,'chain_authenticated':False}
        return event,previous,digest({'previous_hash':previous,'event':event})

    def _audit_slots(self,c,runs):
        if c.execute('PRAGMA user_version').fetchone()[0] not in (SLOT_VERSION,UNION_VERSION,CLOCK_VERSION): return
        _need(not c.execute('SELECT 1 FROM common_bank_slot_intents WHERE length(event_json)>8192 LIMIT 1').fetchone(),
              'COMMON_BANK_SLOT_INTENT_OVERSIZED')
        intents=c.execute('SELECT capture_id,event_json,previous_hash,event_hash FROM common_bank_slot_intents LIMIT ?',
                          (MAX_RUNS+1,)).fetchall()
        _need(len(intents)<=MAX_RUNS, 'COMMON_BANK_SLOT_CAPACITY')
        for capture,raw,previous,key in intents:
            _need(capture in runs, 'COMMON_BANK_SLOT_CAPTURE_MISSING'); run=runs[capture]
            _need(run['completion'] is not None and run['completion']['state']=='DONE', 'AUDITED_GENESIS_DONE_REQUIRED')
            intent=json.loads(raw)
            _need(type(intent) is dict and type(intent.get('used_after')) is int
                  and run['completion']['used_after'] < intent['used_after'] <= run['used']
                  and type(intent.get('reserved_at')) is int
                  and run['completion']['completed_at'] <= intent['reserved_at'] < 2**63,
                  'COMMON_BANK_SLOT_INTENT_INVALID')
            expected=self._slot_intent(run,capture,intent['used_after'],intent['reserved_at'])
            _need(raw==canonical(expected) and previous==self._genesis_hash(run)
                  and key==digest({'previous_hash':previous,'event':intent}), 'COMMON_BANK_SLOT_FENCE_OR_HASH_MISMATCH')
            run['slot_intent']=intent
        _need(not c.execute('SELECT 1 FROM common_bank_slot_attachments WHERE length(event_json)>8192 OR length(request_bytes)>? OR length(response_bytes)>? OR length(failure_json)>? LIMIT 1',
                            (MAX_REQUEST_BYTES,MAX_RESPONSE_BYTES,MAX_FAILURE_BYTES)).fetchone(),
              'COMMON_BANK_SLOT_ATTACHMENT_OVERSIZED')
        rows=c.execute('SELECT capture_id,event_json,previous_hash,event_hash,request_bytes,response_bytes,failure_json FROM common_bank_slot_attachments LIMIT ?',
                       (MAX_RUNS+1,)).fetchall()
        _need(len(rows)<=MAX_RUNS, 'COMMON_BANK_SLOT_ATTACHMENT_CAPACITY')
        for capture,raw,previous,key,request,response,failure in rows:
            _need(capture in runs, 'COMMON_BANK_SLOT_CAPTURE_MISSING'); run=runs[capture]
            event=json.loads(raw); _need(type(event) is dict,'COMMON_BANK_SLOT_ATTACHMENT_INVALID')
            expected,prior,event_hash=self._slot_attachment(run,capture,request,response,failure,event.get('completed_at'))
            _need(raw==canonical(expected) and previous==prior and key==event_hash,
                  'COMMON_BANK_SLOT_ATTACHMENT_HASH_OR_BINDING_MISMATCH')
            run['slot_completion']=expected

    def attach_slot_response(self,token,response_bytes,*,completed_at):
        _need(type(response_bytes) is bytes, 'COMMON_BANK_RAW_RESPONSE_REQUIRED')
        if len(response_bytes)>MAX_RESPONSE_BYTES:
            return self._finish_slot(token,None,self._failure('RESPONSE_OVERSIZED',observed_bytes=len(response_bytes),stage='slot'),completed_at)
        return self._finish_slot(token,response_bytes,None,completed_at)

    def attach_slot_failure(self,token,category,*,completed_at):
        _need(type(category) is str and category in FAILURE_CATEGORIES,'COMMON_BANK_FAILURE_CATEGORY_INVALID')
        return self._finish_slot(token,None,self._failure(category,stage='slot'),completed_at)

    def _finish_slot(self,token,response,failure,at):
        flight=self._flight(token,'slot')
        with self._transaction() as c:
            runs=self._audit(c); capture=flight['capture']
            _need(capture in runs and canonical(runs[capture]['slot_intent'])==flight['intent'],
                  'COMMON_BANK_STALE_SLOT_INTENT')
            event,previous,key=self._slot_attachment(runs[capture],capture,flight['request'],response,failure,at)
            chosen=(canonical(event),previous,key,flight['request'],response,failure)
            _need(flight['chosen'] is None or flight['chosen']==chosen, 'COMMON_BANK_CONFLICTING_COMPLETION')
            flight['chosen']=chosen
            existing=c.execute('SELECT event_json,previous_hash,event_hash,request_bytes,response_bytes,failure_json FROM common_bank_slot_attachments WHERE capture_id=?',(capture,)).fetchone()
            if existing is not None:
                _need(existing==chosen,'COMMON_BANK_CONFLICTING_COMPLETION'); return event
            c.execute('INSERT INTO common_bank_slot_attachments VALUES(?,?,?,?,?,?,?)',(capture,)+chosen)
            return event


    def _union_descriptor(self):
        return {'kind':'common_bank_union_profile_v1','database_binding':self.journal.descriptor,
                'parent_schema_hash':digest(SCHEMA | ATTACHMENT_SCHEMA | SLOT_SCHEMA),
                'request_limit':MAX_UNION_REQUEST_BYTES,'response_limit':MAX_RECORD_BYTES}

    def install_union_stage(self):
        with self._transaction() as c:
            _need(c.execute('PRAGMA user_version').fetchone()[0] in (SLOT_VERSION,UNION_VERSION,CLOCK_VERSION),
                  'SLOT_PROFILE_REQUIRED')
            self._audit(c)
            if c.execute('PRAGMA user_version').fetchone()[0] in (UNION_VERSION,CLOCK_VERSION): return
            for statement in UNION_SCHEMA.values(): c.execute(statement)
            c.execute('INSERT INTO common_bank_union_meta VALUES(1,?)',(canonical(self._union_descriptor()),))
            c.execute(f'PRAGMA user_version={UNION_VERSION}')
            self._schema(c,False)

    @staticmethod
    def _slot_hash(run):
        return digest({'previous_hash':run['slot_completion']['intent_hash'],'event':run['slot_completion']})

    @staticmethod
    def _union_intent(run,capture,used,at):
        return {'kind':'common_bank_union_pending_v1','capture_id':capture,'budget_id':run['budget'],
                'stage':'union','state':'PENDING','method':'getMultipleAccounts',
                'params':[run['plan']['U'],{'encoding':'base64','commitment':'finalized',
                                          'minContextSlot':run['slot_completion']['slot']}],
                'descriptor_hash':digest(run['descriptor']),'plan_hash':digest(run['plan']),
                'fence':_Session._slot_hash(run),'ordinal':5,'used_after':used,'reserved_at':at}

    @staticmethod
    def _union_request(intent):
        return canonical({'jsonrpc':'2.0','id':digest(intent),'method':'getMultipleAccounts',
                          'params':intent['params']}).encode()

    def begin_union(self,capture_id,*,fence,reserved_at):
        _need(_identity(capture_id),'COMMON_BANK_QUERY_ID_INVALID')
        with self._transaction() as c:
            _need(c.execute('PRAGMA user_version').fetchone()[0] in (UNION_VERSION,CLOCK_VERSION),'EXPLICIT_UNION_INSTALL_REQUIRED')
            runs=self._audit(c);_need(capture_id in runs,'COMMON_BANK_CAPTURE_MISSING');run=runs[capture_id]
            _need(run['slot_completion'] is not None and run['slot_completion']['state']=='DONE','AUDITED_SLOT_DONE_REQUIRED')
            _need(run['union_intent'] is None,'COMMON_BANK_UNION_ALREADY_RESERVED_TERMINAL')
            _need(0 <= run['slot_completion']['slot'] < 2**63,'COMMON_BANK_UNION_FLOOR_UNSUPPORTED')
            previous=self._slot_hash(run)
            _need(type(fence) is str and fence==previous,'COMMON_BANK_STALE_UNION_FENCE')
            _need(type(reserved_at) is int and run['slot_completion']['completed_at'] <= reserved_at < 2**63,
                  'COMMON_BANK_UNION_RESERVATION_TIME_INVALID')
            _need(run['used']+3+len(run['plan']['F']) <= 18,'COMMON_BANK_LOWER_BOUND_EXCEEDS_18')
            used=run['used']+1;intent=self._union_intent(run,capture_id,used,reserved_at)
            request=self._union_request(intent)
            _need(len(request)<=MAX_UNION_REQUEST_BYTES,'COMMON_BANK_UNION_REQUEST_OVERSIZED')
            changed=c.execute('UPDATE ownership_budgets SET used=used+1 WHERE id=? AND source_hash=? AND ceiling=18 AND used<18',
                              (run['budget'],run['descriptor']['admission_descriptor_hash'])).rowcount
            _need(changed==1,'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED')
            c.execute('INSERT INTO common_bank_union_intents VALUES(?,?,?,?)',
                      (capture_id,canonical(intent),previous,digest({'previous_hash':previous,'event':intent})))
        token=object();self._inflight[token]={'capture':capture_id,'stage':'union','intent':canonical(intent),
                                            'request':request,'chosen':None}
        return token

    def union_request(self,token):
        return self._flight(token,'union')['request']

    @staticmethod
    def _union_failure(request,category,observed_bytes=None):
        record={'kind':'common_bank_union_redacted_failure_v1','method':'getMultipleAccounts',
                'request_sha256':_sha(request),'category':category}
        if observed_bytes is not None:record['observed_bytes']=observed_bytes
        return canonical(record)

    @staticmethod
    def _union_outcome(run,request,response,failure):
        if failure is not None:
            _need(type(failure) is str and len(failure.encode())<=MAX_FAILURE_BYTES,'COMMON_BANK_UNION_FAILURE_INVALID')
            record=json.loads(failure);_need(type(record) is dict,'COMMON_BANK_UNION_FAILURE_INVALID')
            category=record.get('category');size=record.get('observed_bytes')
            _need((category in FAILURE_CATEGORIES and size is None
                   and failure==_Session._union_failure(request,category)) or
                  (category=='RESPONSE_OVERSIZED' and type(size) is int and MAX_RECORD_BYTES<size<2**63
                   and failure==_Session._union_failure(request,category,size)), 'COMMON_BANK_UNION_FAILURE_INVALID')
            _need(response is None,'COMMON_BANK_ATTACHMENT_PROVENANCE_AMBIGUOUS')
            return 'FAILED',category,None
        state,reason=_Session._outcome(request,response,None,'union')
        if state!='DONE':return state,reason,None
        try:
            result=_strict_json(response)['result'];floor=run['slot_completion']['slot']
            if type(result) is not dict or set(result)!={'context','value'}:return 'FAILED','UNION_RESULT_INVALID',None
            context=result['context'];values=result['value']
            if (type(context) is not dict or not {'slot'}<=set(context)<= {'slot','apiVersion'}
                or type(context.get('slot')) is not int or not floor<=context['slot']<2**63
                or ('apiVersion' in context and (type(context['apiVersion']) is not str or not 1<=len(context['apiVersion'])<=128))):
                return 'FAILED','UNION_CONTEXT_INVALID',None
            if type(values) is not list or len(values)!=len(run['plan']['U']):return 'FAILED','UNION_CARDINALITY_INVALID',None
            # Match accepted full-parent canonical record bound without saving
            # a manufactured/sliced RPC record. Only original wire bytes persist.
            equivalent={'kind':'rpc_response_v1','method':'getMultipleAccounts',
                        'params':run['union_intent']['params'],'result':result}
            if len(canonical(equivalent).encode())>MAX_RECORD_BYTES:return 'FAILED','UNION_PARENT_BOUND',None
            for index,value in enumerate(values):
                if value is None:
                    if index<6:return 'FAILED','UNION_REQUIRED_ACCOUNT_ABSENT',None
                    continue  # Retained null does not establish closure or zero.
                program=PUMPSWAP if index==0 else TOKEN_PROGRAM
                lengths=(243,287,300,301) if index==0 else (82,) if index in (1,2,3) else (165,)
                _raw_account(value,program,lengths)
            return 'DONE','UNION_RAW_SHAPE_VALID',context['slot']
        except (_Reject,ValueError,TypeError,KeyError,RecursionError):
            return 'FAILED','UNION_ACCOUNT_OR_RECORD_INVALID',None

    @staticmethod
    def _union_attachment(run,capture,request,response,failure,at):
        intent=run['union_intent']
        _need(intent is not None and type(at) is int and intent['reserved_at']<=at<2**63,'COMMON_BANK_UNION_COMPLETION_TIME_INVALID')
        _need(type(request) is bytes and len(request)<=MAX_UNION_REQUEST_BYTES and request==_Session._union_request(intent),
              'COMMON_BANK_ORIGINAL_UNION_REQUEST_MISMATCH')
        state,reason,slot=_Session._union_outcome(run,request,response,failure)
        previous=digest({'previous_hash':intent['fence'],'event':intent})
        event={'kind':'common_bank_union_attachment_v1','capture_id':capture,'budget_id':run['budget'],
               'stage':'union','ordinal':6,'state':state,'reason':reason,'intent_hash':previous,
               'fence':intent['fence'],'descriptor_hash':digest(run['descriptor']),'plan_hash':digest(run['plan']),
               'planned_source':run['descriptor']['planned_source'],'used_after':intent['used_after'],
               'reserved_at':intent['reserved_at'],'completed_at':at,'request_sha256':_sha(request),
               'response_sha256':_sha(response) if response is not None else None,
               'failure_hash':digest(json.loads(failure)) if failure is not None else None,
               'request_floor':run['slot_completion']['slot'],'slot':slot,
               'eligible_for_trading':False,'ownership_approval':False,'chain_authenticated':False}
        return event,previous,digest({'previous_hash':previous,'event':event})

    def _audit_unions(self,c,runs):
        if c.execute('PRAGMA user_version').fetchone()[0]not in (UNION_VERSION,CLOCK_VERSION):return
        _need(not c.execute('SELECT 1 FROM common_bank_union_intents WHERE length(event_json)>16384 LIMIT 1').fetchone(),
              'COMMON_BANK_UNION_INTENT_OVERSIZED')
        intents=c.execute('SELECT capture_id,event_json,previous_hash,event_hash FROM common_bank_union_intents LIMIT ?',
                          (MAX_RUNS+1,)).fetchall();_need(len(intents)<=MAX_RUNS,'COMMON_BANK_UNION_CAPACITY')
        for capture,raw,previous,key in intents:
            _need(capture in runs,'COMMON_BANK_UNION_CAPTURE_MISSING');run=runs[capture]
            _need(run['slot_completion'] is not None and run['slot_completion']['state']=='DONE','AUDITED_SLOT_DONE_REQUIRED')
            _need(0<=run['slot_completion']['slot']<2**63,'COMMON_BANK_UNION_FLOOR_UNSUPPORTED')
            intent=json.loads(raw)
            _need(type(intent) is dict and type(intent.get('used_after')) is int
                  and run['slot_completion']['used_after']<intent['used_after']<=run['used']
                  and type(intent.get('reserved_at')) is int and run['slot_completion']['completed_at']<=intent['reserved_at']<2**63,
                  'COMMON_BANK_UNION_INTENT_INVALID')
            expected=self._union_intent(run,capture,intent['used_after'],intent['reserved_at'])
            _need(raw==canonical(expected) and previous==self._slot_hash(run)
                  and key==digest({'previous_hash':previous,'event':intent})
                  and len(self._union_request(intent))<=MAX_UNION_REQUEST_BYTES,'COMMON_BANK_UNION_FENCE_OR_HASH_MISMATCH')
            run['union_intent']=intent
        _need(not c.execute('SELECT 1 FROM common_bank_union_attachments WHERE length(event_json)>8192 OR length(request_bytes)>? OR length(response_bytes)>? OR length(failure_json)>? LIMIT 1',
                            (MAX_UNION_REQUEST_BYTES,MAX_RECORD_BYTES,MAX_FAILURE_BYTES)).fetchone(),'COMMON_BANK_UNION_ATTACHMENT_OVERSIZED')
        rows=c.execute('SELECT capture_id,event_json,previous_hash,event_hash,request_bytes,response_bytes,failure_json FROM common_bank_union_attachments LIMIT ?',
                       (MAX_RUNS+1,)).fetchall();_need(len(rows)<=MAX_RUNS,'COMMON_BANK_UNION_ATTACHMENT_CAPACITY')
        for capture,raw,previous,key,request,response,failure in rows:
            _need(capture in runs,'COMMON_BANK_UNION_CAPTURE_MISSING');run=runs[capture]
            event=json.loads(raw);_need(type(event) is dict,'COMMON_BANK_UNION_ATTACHMENT_INVALID')
            expected,prior,event_hash=self._union_attachment(run,capture,request,response,failure,event.get('completed_at'))
            _need(raw==canonical(expected) and previous==prior and key==event_hash,'COMMON_BANK_UNION_ATTACHMENT_HASH_OR_BINDING_MISMATCH')
            run['union_completion']=expected

    def attach_union_response(self,token,response_bytes,*,completed_at):
        _need(type(response_bytes) is bytes,'COMMON_BANK_RAW_RESPONSE_REQUIRED')
        flight=self._flight(token,'union')
        if len(response_bytes)>MAX_RECORD_BYTES:
            return self._finish_union(token,None,self._union_failure(flight['request'],'RESPONSE_OVERSIZED',len(response_bytes)),completed_at)
        return self._finish_union(token,response_bytes,None,completed_at)

    def attach_union_failure(self,token,category,*,completed_at):
        _need(type(category) is str and category in FAILURE_CATEGORIES,'COMMON_BANK_FAILURE_CATEGORY_INVALID')
        flight=self._flight(token,'union')
        return self._finish_union(token,None,self._union_failure(flight['request'],category),completed_at)

    def _finish_union(self,token,response,failure,at):
        flight=self._flight(token,'union')
        with self._transaction() as c:
            runs=self._audit(c);capture=flight['capture']
            _need(capture in runs and canonical(runs[capture]['union_intent'])==flight['intent'],'COMMON_BANK_STALE_UNION_INTENT')
            event,previous,key=self._union_attachment(runs[capture],capture,flight['request'],response,failure,at)
            chosen=(canonical(event),previous,key,flight['request'],response,failure)
            _need(flight['chosen'] is None or flight['chosen']==chosen,'COMMON_BANK_CONFLICTING_COMPLETION');flight['chosen']=chosen
            existing=c.execute('SELECT event_json,previous_hash,event_hash,request_bytes,response_bytes,failure_json FROM common_bank_union_attachments WHERE capture_id=?',(capture,)).fetchone()
            if existing is not None:
                _need(existing==chosen,'COMMON_BANK_CONFLICTING_COMPLETION');return event
            c.execute('INSERT INTO common_bank_union_attachments VALUES(?,?,?,?,?,?,?)',(capture,)+chosen)
            return event


    def _clock_descriptor(self):
        return {'kind':'common_bank_clock_profile_v1','database_binding':self.journal.descriptor,
                'parent_schema_hash':digest(SCHEMA | ATTACHMENT_SCHEMA | SLOT_SCHEMA | UNION_SCHEMA),
                'request_limit':MAX_REQUEST_BYTES,'response_limit':MAX_RECORD_BYTES}

    def install_clock_stage(self):
        with self._transaction() as c:
            _need(c.execute('PRAGMA user_version').fetchone()[0] in (UNION_VERSION,CLOCK_VERSION),
                  'UNION_PROFILE_REQUIRED')
            self._audit(c)
            if c.execute('PRAGMA user_version').fetchone()[0] == CLOCK_VERSION: return
            for statement in CLOCK_SCHEMA.values(): c.execute(statement)
            c.execute('INSERT INTO common_bank_clock_meta VALUES(1,?)',(canonical(self._clock_descriptor()),))
            c.execute(f'PRAGMA user_version={CLOCK_VERSION}')
            self._schema(c,False)

    @staticmethod
    def _union_hash(run):
        return digest({'previous_hash':run['union_completion']['intent_hash'],'event':run['union_completion']})

    @staticmethod
    def _clock_intent(run,capture,used,at):
        return {'kind':'common_bank_clock_pending_v1','capture_id':capture,'budget_id':run['budget'],
                'stage':'clock','state':'PENDING','method':'getBlockTime',
                'params':[run['union_completion']['slot']],
                'descriptor_hash':digest(run['descriptor']),'plan_hash':digest(run['plan']),
                'fence':_Session._union_hash(run),'ordinal':7,'used_after':used,'reserved_at':at}

    @staticmethod
    def _clock_request(intent):
        return canonical({'jsonrpc':'2.0','id':digest(intent),'method':'getBlockTime',
                          'params':intent['params']}).encode()

    def begin_clock(self,capture_id,*,fence,reserved_at):
        _need(_identity(capture_id),'COMMON_BANK_QUERY_ID_INVALID')
        with self._transaction() as c:
            _need(c.execute('PRAGMA user_version').fetchone()[0] == CLOCK_VERSION,'EXPLICIT_CLOCK_INSTALL_REQUIRED')
            runs=self._audit(c);_need(capture_id in runs,'COMMON_BANK_CAPTURE_MISSING');run=runs[capture_id]
            _need(run['union_completion'] is not None and run['union_completion']['state']=='DONE','AUDITED_UNION_DONE_REQUIRED')
            _need(run['clock_intent'] is None,'COMMON_BANK_CLOCK_ALREADY_RESERVED_TERMINAL')
            previous=self._union_hash(run)
            _need(type(fence) is str and fence==previous,'COMMON_BANK_STALE_CLOCK_FENCE')
            _need(type(reserved_at) is int and run['union_completion']['completed_at'] <= reserved_at < 2**63,
                  'COMMON_BANK_CLOCK_RESERVATION_TIME_INVALID')
            _need(run['used']+2+len(run['plan']['F']) <= 18,'COMMON_BANK_LOWER_BOUND_EXCEEDS_18')
            used=run['used']+1;intent=self._clock_intent(run,capture_id,used,reserved_at)
            request=self._clock_request(intent)
            _need(len(request)<=MAX_REQUEST_BYTES,'COMMON_BANK_CLOCK_REQUEST_OVERSIZED')
            changed=c.execute('UPDATE ownership_budgets SET used=used+1 WHERE id=? AND source_hash=? AND ceiling=18 AND used<18',
                              (run['budget'],run['descriptor']['admission_descriptor_hash'])).rowcount
            _need(changed==1,'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED')
            c.execute('INSERT INTO common_bank_clock_intents VALUES(?,?,?,?)',
                      (capture_id,canonical(intent),previous,digest({'previous_hash':previous,'event':intent})))
        token=object();self._inflight[token]={'capture':capture_id,'stage':'clock','intent':canonical(intent),
                                            'request':request,'chosen':None}
        return token

    def clock_request(self,token):
        return self._flight(token,'clock')['request']

    @staticmethod
    def _clock_failure(request,category,observed_bytes=None):
        record={'kind':'common_bank_clock_redacted_failure_v1','method':'getBlockTime',
                'request_sha256':_sha(request),'category':category}
        if observed_bytes is not None:record['observed_bytes']=observed_bytes
        return canonical(record)

    @staticmethod
    def _clock_outcome(run,request,response,failure):
        if failure is not None:
            _need(type(failure) is str and len(failure.encode())<=MAX_FAILURE_BYTES,'COMMON_BANK_CLOCK_FAILURE_INVALID')
            record=json.loads(failure);_need(type(record) is dict,'COMMON_BANK_CLOCK_FAILURE_INVALID')
            category=record.get('category');size=record.get('observed_bytes')
            _need((category in FAILURE_CATEGORIES and size is None
                   and failure==_Session._clock_failure(request,category)) or
                  (category=='RESPONSE_OVERSIZED' and type(size) is int and MAX_RECORD_BYTES<size<2**63
                   and failure==_Session._clock_failure(request,category,size)), 'COMMON_BANK_CLOCK_FAILURE_INVALID')
            _need(response is None,'COMMON_BANK_ATTACHMENT_PROVENANCE_AMBIGUOUS')
            return 'FAILED',category,None
        state,reason=_Session._outcome(request,response,None,'clock')
        return state,reason,_strict_json(response)['result'] if state=='DONE' else None

    @staticmethod
    def _clock_attachment(run,capture,request,response,failure,at):
        intent=run['clock_intent']
        _need(intent is not None and type(at) is int and intent['reserved_at']<=at<2**63,'COMMON_BANK_CLOCK_COMPLETION_TIME_INVALID')
        _need(type(request) is bytes and len(request)<=MAX_REQUEST_BYTES and request==_Session._clock_request(intent),
              'COMMON_BANK_ORIGINAL_CLOCK_REQUEST_MISMATCH')
        state,reason,block_time=_Session._clock_outcome(run,request,response,failure)
        previous=digest({'previous_hash':intent['fence'],'event':intent})
        event={'kind':'common_bank_clock_attachment_v1','capture_id':capture,'budget_id':run['budget'],
               'stage':'clock','ordinal':8,'state':state,'reason':reason,'intent_hash':previous,
               'fence':intent['fence'],'descriptor_hash':digest(run['descriptor']),'plan_hash':digest(run['plan']),
               'planned_source':run['descriptor']['planned_source'],'used_after':intent['used_after'],
               'reserved_at':intent['reserved_at'],'completed_at':at,'request_sha256':_sha(request),
               'response_sha256':_sha(response) if response is not None else None,
               'failure_hash':digest(json.loads(failure)) if failure is not None else None,
               'slot':run['union_completion']['slot'],'block_time':block_time,
               'bank_captured_at':run['union_completion']['reserved_at'],
               'eligible_for_trading':False,'ownership_approval':False,'chain_authenticated':False}
        return event,previous,digest({'previous_hash':previous,'event':event})

    def _audit_clocks(self,c,runs):
        if c.execute('PRAGMA user_version').fetchone()[0]!= CLOCK_VERSION:return
        _need(not c.execute('SELECT 1 FROM common_bank_clock_intents WHERE length(event_json)>8192 LIMIT 1').fetchone(),
              'COMMON_BANK_CLOCK_INTENT_OVERSIZED')
        intents=c.execute('SELECT capture_id,event_json,previous_hash,event_hash FROM common_bank_clock_intents LIMIT ?',
                          (MAX_RUNS+1,)).fetchall();_need(len(intents)<=MAX_RUNS,'COMMON_BANK_CLOCK_CAPACITY')
        for capture,raw,previous,key in intents:
            _need(capture in runs,'COMMON_BANK_CLOCK_CAPTURE_MISSING');run=runs[capture]
            _need(run['union_completion'] is not None and run['union_completion']['state']=='DONE','AUDITED_UNION_DONE_REQUIRED')
            intent=json.loads(raw)
            _need(type(intent) is dict and type(intent.get('used_after')) is int
                  and run['union_completion']['used_after']<intent['used_after']<=run['used']
                  and type(intent.get('reserved_at')) is int and run['union_completion']['completed_at']<=intent['reserved_at']<2**63,
                  'COMMON_BANK_CLOCK_INTENT_INVALID')
            expected=self._clock_intent(run,capture,intent['used_after'],intent['reserved_at'])
            _need(raw==canonical(expected) and previous==self._union_hash(run)
                  and key==digest({'previous_hash':previous,'event':intent})
                  and len(self._clock_request(intent))<=MAX_REQUEST_BYTES,'COMMON_BANK_CLOCK_FENCE_OR_HASH_MISMATCH')
            run['clock_intent']=intent
        _need(not c.execute('SELECT 1 FROM common_bank_clock_attachments WHERE length(event_json)>8192 OR length(request_bytes)>? OR length(response_bytes)>? OR length(failure_json)>? LIMIT 1',
                            (MAX_REQUEST_BYTES,MAX_RECORD_BYTES,MAX_FAILURE_BYTES)).fetchone(),'COMMON_BANK_CLOCK_ATTACHMENT_OVERSIZED')
        rows=c.execute('SELECT capture_id,event_json,previous_hash,event_hash,request_bytes,response_bytes,failure_json FROM common_bank_clock_attachments LIMIT ?',
                       (MAX_RUNS+1,)).fetchall();_need(len(rows)<=MAX_RUNS,'COMMON_BANK_CLOCK_ATTACHMENT_CAPACITY')
        for capture,raw,previous,key,request,response,failure in rows:
            _need(capture in runs,'COMMON_BANK_CLOCK_CAPTURE_MISSING');run=runs[capture]
            event=json.loads(raw);_need(type(event) is dict,'COMMON_BANK_CLOCK_ATTACHMENT_INVALID')
            expected,prior,event_hash=self._clock_attachment(run,capture,request,response,failure,event.get('completed_at'))
            _need(raw==canonical(expected) and previous==prior and key==event_hash,'COMMON_BANK_CLOCK_ATTACHMENT_HASH_OR_BINDING_MISMATCH')
            run['clock_completion']=expected

    def attach_clock_response(self,token,response_bytes,*,completed_at):
        _need(type(response_bytes) is bytes,'COMMON_BANK_RAW_RESPONSE_REQUIRED')
        flight=self._flight(token,'clock')
        if len(response_bytes)>MAX_RECORD_BYTES:
            return self._finish_clock(token,None,self._clock_failure(flight['request'],'RESPONSE_OVERSIZED',len(response_bytes)),completed_at)
        return self._finish_clock(token,response_bytes,None,completed_at)

    def attach_clock_failure(self,token,category,*,completed_at):
        _need(type(category) is str and category in FAILURE_CATEGORIES,'COMMON_BANK_FAILURE_CATEGORY_INVALID')
        flight=self._flight(token,'clock')
        return self._finish_clock(token,None,self._clock_failure(flight['request'],category),completed_at)

    def _finish_clock(self,token,response,failure,at):
        flight=self._flight(token,'clock')
        with self._transaction() as c:
            runs=self._audit(c);capture=flight['capture']
            _need(capture in runs and canonical(runs[capture]['clock_intent'])==flight['intent'],'COMMON_BANK_STALE_CLOCK_INTENT')
            event,previous,key=self._clock_attachment(runs[capture],capture,flight['request'],response,failure,at)
            chosen=(canonical(event),previous,key,flight['request'],response,failure)
            _need(flight['chosen'] is None or flight['chosen']==chosen,'COMMON_BANK_CONFLICTING_COMPLETION');flight['chosen']=chosen
            existing=c.execute('SELECT event_json,previous_hash,event_hash,request_bytes,response_bytes,failure_json FROM common_bank_clock_attachments WHERE capture_id=?',(capture,)).fetchone()
            if existing is not None:
                _need(existing==chosen,'COMMON_BANK_CONFLICTING_COMPLETION');return event
            c.execute('INSERT INTO common_bank_clock_attachments VALUES(?,?,?,?,?,?,?)',(capture,)+chosen)
            return event
