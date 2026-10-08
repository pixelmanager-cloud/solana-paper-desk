"""Guarded diagnostic replay of original journal bytes; never an RPC publisher.

Trusted callers use an existing locked journal session. No candidate loader,
manifest, source hash pin, admission flag or normalized response is accepted.
"""
from contextlib import closing
import os
import sqlite3
import threading
import struct
import zlib

from .common_bank_journal import (ATTACHMENT_SCHEMA, CLOCK_SCHEMA, CLOCK_VERSION,
    SCHEMA, SLOT_SCHEMA, UNION_SCHEMA, JournalBlocked, _Session, _identity, _need, _sha,
    _strict_json)
from .common_bank_view import CommonBankError, _validate_account_semantics
from .control_obligations import ReplayView, read_guard
from .evidence import EvidenceStore
from .model import digest
from .pool_vault_admission import _Reject

MAX_REFERENCES = 128
MAX_LOADS = 128
MAX_SCHEMA_OBJECTS = 128
MAX_BYTES = 32 * 1024 * 1024
# Existing schemas are preserved. No migration or new journal profile exists.
TABLES = tuple(name for name,sql in (SCHEMA|ATTACHMENT_SCHEMA|SLOT_SCHEMA|UNION_SCHEMA|CLOCK_SCHEMA).items()
               if sql.startswith('CREATE TABLE'))
ATTACHMENTS = {'genesis':'common_bank_attachments','slot':'common_bank_slot_attachments',
               'union':'common_bank_union_attachments','clock':'common_bank_clock_attachments'}
TEXT_COLUMNS = {
    'common_bank_runs':('capture_id','budget_id','descriptor_json','plan_json','plan_hash','seed_hash'),
    'common_bank_events':('capture_id','ordinal','event_json','previous_hash','event_hash'),
}
for _table in TABLES:
    if _table.endswith('_meta'):TEXT_COLUMNS[_table]=('descriptor',)
    elif _table.endswith('_intents'):TEXT_COLUMNS[_table]=('capture_id','event_json','previous_hash','event_hash')
    elif _table in ATTACHMENTS.values():TEXT_COLUMNS[_table]=('capture_id','event_json','previous_hash','event_hash','request_bytes','response_bytes','failure_json')


class _Budget:
    def __init__(self):self.references=set();self.bytes=0;self.loads=0;self.blocked=None

    def require(self,condition,reason):
        if not condition:
            self.blocked=self.blocked or reason
            raise JournalBlocked(reason)

    def check(self):
        _need(self.blocked is None,self.blocked or 'COMMON_BANK_SEMANTIC_RESOURCE_BLOCKED')

    def reference(self,key):
        self.references.add(key)
        self.require(len(self.references)<=MAX_REFERENCES,'COMMON_BANK_SEMANTIC_REFERENCE_CEILING')

    def charge(self,size):
        self.require(type(size) is int and size>=0,'COMMON_BANK_SEMANTIC_SIZE_INVALID')
        self.bytes+=size
        self.require(self.bytes<=MAX_BYTES,'COMMON_BANK_SEMANTIC_BYTE_CEILING')

    def diagnostic(self):
        self.check()
        return {'distinct_references':len(self.references),'physical_page_loads':self.loads,
                'serialized_bytes_charged':self.bytes,'reference_ceiling':MAX_REFERENCES,
                'physical_page_load_ceiling':MAX_LOADS,'serialized_byte_ceiling':MAX_BYTES}


class _Store(EvidenceStore):
    def __init__(self,path,connection,budget):
        super().__init__(path,read_only=True);self.connection=connection;self.budget=budget

    def load(self,key):
        self.budget.reference('page:'+key)
        self.budget.loads+=1
        self.budget.require(self.budget.loads<=MAX_LOADS,'COMMON_BANK_SEMANTIC_LOAD_CEILING')
        row=self.connection.execute('SELECT raw_bytes,length(payload) FROM pages WHERE hash=?',(key,)).fetchone()
        self.budget.require(row is not None and type(row[0]) is int and 0<=row[0]<=16*1024*1024
              and type(row[1]) is int and row[1]>=0,'COMMON_BANK_SEMANTIC_PAGE_UNAVAILABLE')
        # Reserve decompressed and compressed input before fetching either body.
        self.budget.charge(row[0]+row[1])
        return super().load(key)


def _preflight(session,c,budget):
    """Whole-set SQL length/count admission before schema/body traversal."""
    placeholders=','.join('?' for _ in TABLES)
    where="name LIKE 'common_bank_%' OR (type='trigger' AND tbl_name IN ("+placeholders+"))"
    _need(not c.execute('SELECT 1 FROM sqlite_master WHERE ('+where+') AND length(CAST(name AS BLOB))>128 LIMIT 1',TABLES).fetchone(),
          'COMMON_BANK_SEMANTIC_SCHEMA_NAME_BOUND')
    records=c.execute('SELECT name,length(CAST(sql AS BLOB)) FROM sqlite_master WHERE '+where+' LIMIT ?',TABLES+(MAX_SCHEMA_OBJECTS+1,)).fetchall()
    _need(len(records)<=MAX_SCHEMA_OBJECTS,'COMMON_BANK_SEMANTIC_SCHEMA_CEILING')
    for _,size in records:budget.charge(size)
    present={r[0] for r in records}
    for table in TABLES:
        if table not in present:continue  # Existing strict schema audit refuses gaps.
        columns=TEXT_COLUMNS[table]
        expression='+'.join('COALESCE(length(CAST('+col+' AS BLOB)),0)' for col in columns)
        count,size=c.execute('SELECT count(*),COALESCE(sum('+expression+'),0) FROM '+table).fetchone()
        _need(count<=MAX_REFERENCES,'COMMON_BANK_SEMANTIC_ROW_CEILING')
        budget.charge(size)
        key='id' if table.endswith('_meta') else 'capture_id'
        _need(not c.execute('SELECT 1 FROM '+table+' WHERE length(CAST('+key+' AS BLOB))>128 LIMIT 1').fetchone(),
              'COMMON_BANK_SEMANTIC_ID_BOUND')
        for row, in c.execute('SELECT '+key+' FROM '+table):
            budget.reference('journal:'+table+':'+str(row))
            if table in ATTACHMENTS.values():
                budget.reference('request:'+table+':'+str(row));budget.reference('response:'+table+':'+str(row))
    if 'common_bank_runs' not in present:return
    rows=c.execute('SELECT capture_id,budget_id FROM common_bank_runs LIMIT ?', (MAX_REFERENCES+1,)).fetchall()
    with closing(sqlite3.connect(session.journal.research.as_uri()+'?mode=ro',uri=True,timeout=0)) as source:
        for capture,run_id in rows:
            _need(_identity(capture) and _identity(run_id),'COMMON_BANK_RUN_ID_INVALID')
            budget.reference('source:'+run_id)
            columns=('id','descriptor','state','prepared_source','prepared_used','completed_source_hash')
            row=c.execute('SELECT '+','.join('COALESCE(length(CAST('+name+' AS BLOB)),0)' for name in columns)
                          +' FROM ownership_admissions WHERE id=?',(run_id,)).fetchone()
            _need(row is not None and all(type(n) is int and n>=0 for n in row)
                  and row[1]<=8192 and row[3]<=2*1024*1024 and sum(row)<=2*1024*1024+8192+512,
                  'BOUNDED_EXISTING_SEALED_SOURCE_REQUIRED')
            # Admission inspector, exact source comparison and continuation
            # validation each consume sealed source; reserve those passes.
            budget.charge(3*sum(row))
            counters=c.execute('SELECT COALESCE(length(CAST(source_hash AS BLOB)),0),COALESCE(length(CAST(used AS BLOB)),0),COALESCE(length(CAST(ceiling AS BLOB)),0) FROM ownership_budgets WHERE id=?',(run_id,)).fetchone()
            _need(counters is not None and sum(counters)<=512,'COMMON_BANK_SEMANTIC_COUNTER_BOUND')
            budget.charge(3*sum(counters))
            # SQLite affinity does not prevent BLOBs in nominally numeric
            # columns. Include created before any exact-source row fetch.
            sizes=source.execute('SELECT length(CAST(id AS BLOB)),length(CAST(mint AS BLOB)),length(CAST(created AS BLOB)),length(CAST(status AS BLOB)),length(CAST(result AS BLOB)) FROM scans WHERE id=?',(run_id,)).fetchone()
            _need(sizes is not None and all(type(n) is int and n>=0 for n in sizes)
                  and sum(sizes)<=2*1024*1024,'COMMON_BANK_SEMANTIC_SOURCE_BOUND')
            budget.charge(sum(sizes))


def replay_semantics(session,capture_id,*,observed_at):
    """Read-only exact journal replay, no partial result on any refusal.

    observed_at is local diagnostic time only, never a freshness attestation.
    Every run is audited with the same aggregate budget; selection cannot hide
    corrupt/oversized competing journal records or bypass source admission.
    """
    _need(type(session) is _Session,'COMMON_BANK_SEMANTIC_SESSION_REQUIRED')
    _need(_identity(capture_id),'COMMON_BANK_QUERY_ID_INVALID')
    _need(type(observed_at) is int and 0<=observed_at<2**63,'COMMON_BANK_SEMANTIC_OBSERVATION_TIME_INVALID')
    _need(session.active and session.owner==(os.getpid(),threading.get_ident()),
          'COMMON_BANK_SESSION_EXPIRED_OR_WRONG_OWNER')
    session.journal._guard()
    try:
        # Existing locks/guard already cover research. Acquire only evidence's
        # read guard here; never recursively enter a public journal/write API.
        with read_guard(session.journal.path), closing(sqlite3.connect(
                session.journal.path.as_uri()+'?mode=ro',uri=True,timeout=0,isolation_level=None)) as c:
            c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
            budget=_Budget();_preflight(session,c,budget);session._schema(c,False)
            _need(c.execute('PRAGMA user_version').fetchone()[0]==CLOCK_VERSION,'COMMON_BANK_SEMANTIC_CLOCK_PROFILE_REQUIRED')
            selected=c.execute('SELECT budget_id FROM common_bank_runs WHERE capture_id=?',(capture_id,)).fetchone()
            _need(selected is not None,'COMMON_BANK_CAPTURE_MISSING')
            details={};view=ReplayView(_Store(session.journal.path,c,budget))
            runs=session._audit(c,view=view,details=(selected[0],details));budget.check();run=runs[capture_id]
            stages={}
            for stage,table in ATTACHMENTS.items():
                completion=run[{'genesis':'completion','slot':'slot_completion','union':'union_completion','clock':'clock_completion'}[stage]]
                _need(completion is not None and completion['state']=='DONE','COMMON_BANK_SEMANTIC_ALL_STAGES_DONE_REQUIRED')
                row=c.execute('SELECT request_bytes,response_bytes,event_hash FROM '+table+' WHERE capture_id=?',(capture_id,)).fetchone()
                _need(row is not None and type(row[0]) is bytes and type(row[1]) is bytes,'COMMON_BANK_SEMANTIC_ORIGINAL_BYTES_REQUIRED')
                # Selected originals are fetched/parsed again after all-stage
                # audit; charge this extra bounded pass explicitly.
                budget.charge(len(row[0])+len(row[1]))
                stages[stage]={'request':_strict_json(row[0]),'response':_strict_json(row[1]),
                              'request_sha256':_sha(row[0]),'response_sha256':_sha(row[1]),
                              'intent_hash':completion['intent_hash'],'completion_hash':row[2]}
            plan=run['plan'];history=details['history'];floor=stages['slot']['response']['result']
            semantics=_validate_account_semantics(plan['mint'],history['inventory'],history['slot_range'],floor,
                plan['U'],plan['F'],plan,stages['union']['request']['params'],stages['union']['response']['result'],
                stages['clock']['request']['params'],stages['clock']['response']['result'])
            clock=run['clock_completion'];captured=clock['bank_captured_at']
            _need(semantics['slot']==clock['slot'] and semantics['block_time']==clock['block_time']
                  and captured==run['union_intent']['reserved_at'],'COMMON_BANK_SEMANTIC_CLOCK_OR_CAPTURE_BINDING_INVALID')
            _need(observed_at>=clock['completed_at'],'COMMON_BANK_SEMANTIC_OBSERVATION_BEFORE_COMPLETION')
            session.journal._guard()
            missing=['SOURCE_AND_FINALITY_UNAUTHENTICATED','FRONTIER_AT_ACTUAL_BANK_UNPROVEN',
                     'POST_CAPTURE_HISTORY_AND_LIFETIMES_UNRESOLVED','AUTHORITATIVE_CAPTURE_COMPLETION_ABSENT',
                     'PROTECTED_RECEIPT_AND_PUBLICATION_ABSENT','CURRENT_FRESHNESS_UNAUTHENTICATED']
            if any(value=='ABSENT_AT_BANK_UNVERIFIED_LIFETIME' for value in semantics['holder_states'].values()):
                missing.append('ABSENT_HOLDER_LIFETIME_UNRESOLVED')
            if view.requested-set(view.cache):missing.append('DECLARED_HISTORY_EVIDENCE_UNAVAILABLE')
            out={'schema':'common_bank_journal_semantic_diagnostic_v1','decision':'REJECT',
                'read_status':'AUDITED_LOCAL_SEMANTICS','local_binding_valid':True,'semantic_binding_valid':True,
                'capture_id':capture_id,'budget_id':run['budget'],'planned_source':run['descriptor']['planned_source'],
                'admission_descriptor_hash':run['descriptor']['admission_descriptor_hash'],
                'completed_source_hash':run['descriptor']['completed_source_hash'],'plan_hash':digest(plan),
                'seed_hash':run['seed'],'requests_used':run['used'],'request_ceiling':18,
                'snapshot_slot':semantics['slot'],'request_floor':floor,'snapshot_time':semantics['block_time'],
                'discovery_cutoff':semantics['discovery_cutoff'],'bank_captured_at':captured,
                'clock_completed_at':clock['completed_at'],'observed_at':observed_at,
                'local_capture_age_seconds':observed_at-captured,
                'keys':semantics['keys'],'frontier':semantics['frontier'],
                'ownership_indices':semantics['ownership_indices'],'pool_indices':semantics['pool_indices'],
                'holder_states':semantics['holder_states'],
                'original_bindings':{stage:{k:v for k,v in value.items() if k not in ('request','response')}
                                     for stage,value in stages.items()},
                'discovery_evidence_hashes':plan['discovery_evidence_hashes'],
                'audited_evidence_hashes':sorted(view.cache),
                'unavailable_evidence_hashes':sorted(view.requested-set(view.cache)),'resource_usage':budget.diagnostic(),
                'remaining_dependencies':missing,'provider_calls':0,
                'source_authenticated':False,'finality_authenticated':False,'frontier_at_bank_complete':False,
                'history_complete':False,'closed_lifetimes_verified':False,'cpi_success_verified':False,
                'historical_interval_exclusion_allowed':False,'ownership_approved':False,
                'common_control_verified':False,'private_control_proven':False,'lifecycle_verified':False,
                'historical_control_verified':False,'production_point_prerequisite':False,
                'authoritative_capture_complete':False,'eligible_for_trading':False}
            out['diagnostic_hash']=digest(out)
            return out
    except (JournalBlocked,CommonBankError):raise
    except _Reject as exc:raise CommonBankError(str(exc)) from None
    except (ValueError,TypeError,KeyError,IndexError,AttributeError,RecursionError,OverflowError,
            UnicodeError,sqlite3.Error,OSError,struct.error,zlib.error,ImportError):
        raise JournalBlocked('COMMON_BANK_SEMANTIC_REPLAY_INVALID') from None
