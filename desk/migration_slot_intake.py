"""Explicit coordinator-only single-slot finalized history intake; hints grant no authority.

Run as separate reviewed tooling against existing research/evidence. No paper
ledger, admission/schema creation, provider primitive or source rewrite. Existing
b84 exporter/cycle can consume the ordinary history_request_v1 manifests.
"""
import argparse
import base64
from contextlib import closing
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from .graduation_witness import extract_graduation, PROVENANCES, _pda, PUMP, AMM, SOL
from solders.pubkey import Pubkey
from .history_progress import canonical_ownership_path, HistoryProgress
from .job_persistence import canonical_job_path
from .model import canonical, digest
from .ownership_acquisition import _Setup, _policy, _validate_binding
from .paper_history_source import PaperHistorySource
from .paper_observation_collector import ObservationTarget, _admission, _Blocked
from .paper_observe_cli import _worker_lock
from .paper_cycle import _lock
from .paper_read_sources import PaperReadSources, PaperReadError, RPC_ID, MAX_REQUEST_BYTES
from .paper_target_export import _context, _bounded, _Store
from .programs import address, unbase58
from .replay_history import replay_history
from .security import base58

MAX_ATTEMPTS = 2  # Failed/ambiguous attempts remain charged and cannot be retried here.


class IntakeBlocked(ValueError):
    pass


class _ExistingStore(_Store):
    def connect(self):
        # rw cannot silently create/reinitialize an absent database.
        mode = 'ro' if self.read_only else 'rw'
        return sqlite3.connect(self.path.as_uri()+'?mode='+mode, uri=True,
                               timeout=2, isolation_level=None)


class _SlotSource(PaperHistorySource):
    """Exact one-slot adapter using the existing owning-reservation snapshot.

    Advance owns the sole charge. PaperReadSources retains the exact original
    wire outcome through the same fixed endpoint, deadline and redacted errors.
    The old rolling-window adapter/transport grammar is not broadened.
    """
    def __init__(self, progress, scan_id, history_id, *, slot, pool, timeout_seconds):
        if (type(timeout_seconds) not in (int,float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 10 or not isinstance(progress,HistoryProgress)):
            raise PaperReadError('HISTORY_BINDING_INVALID')
        self.started = time.monotonic(); self.deadline = self.started+timeout_seconds
        self.progress,self.scan_id,self.history_id = progress,scan_id,history_id
        self.before,self.admission_before = self._bound_snapshot()
        expected = {'address':pool,'start':0,'end':1,'token_accounts_filter':'none',
                    'slot_range':{'gte':slot,'lt':slot+1}}
        if (type(slot) is not int or not 0 <= slot < 2**63
                or self.before['query'] != expected):
            raise PaperReadError('HISTORY_BINDING_INVALID')
        self.called=False;self.evidence_hash=None

    def __call__(self, method, params):
        before=self.before;query=before['query'];coverage=before['coverage']
        options={'transactionDetails':'full','sortOrder':'asc','limit':100,'commitment':'finalized',
                 'encoding':'jsonParsed','maxSupportedTransactionVersion':1,
                 'filters':{'slot':query['slot_range'],'status':'any','tokenAccounts':'none'}}
        if coverage and coverage['next_cursor']:options['paginationToken']=coverage['next_cursor']
        if (self.called or method!='getTransactionsForAddress' or type(params) is not list
                or canonical(params)!=canonical([query['address'],options])):
            raise PaperReadError('HISTORY_BINDING_INVALID')
        current,admission=self._bound_snapshot()
        expected={**self.admission_before,'requests_used':before['requests_used']+1}
        if (admission!=expected or current['query']!=query or current['coverage']!=coverage
                or current['status']!='PENDING' or current['attempts']!=before['attempts']+1):
            raise PaperReadError('HISTORY_RESERVATION_REQUIRED')
        self.called=True;owner=self
        class Reserved:
            store=owner.progress.store
            def admission(self,identity):
                state,binding=owner._bound_snapshot()
                if identity!=owner.scan_id or state!=current or binding!=expected:
                    raise PaperReadError('HISTORY_RESERVATION_REQUIRED')
                return binding
            def reserve(self,identity):
                self.admission(identity);return True  # Exact existing advance charge, never a second charge.
        adapter=PaperReadSources(self.progress,self.scan_id);adapter.progress=Reserved()
        body=canonical({'jsonrpc':'2.0','id':RPC_ID,'method':method,'params':params}).encode()
        if len(body)>MAX_REQUEST_BYTES:raise PaperReadError('REQUEST_INVALID')
        now=time.monotonic()
        if not math.isfinite(now) or now<self.started:
            raise PaperReadError('DEADLINE_EXCEEDED')
        if now>=self.deadline:
            # This is a local, known pre-transport outcome, NOT a wire response.
            # Advance already owns the charge. Preserve it and do not retry.
            observed=int(time.time())
            if not 0<=observed<2**63:raise PaperReadError('DEADLINE_EXCEEDED')
            record={'kind':'paper_read_attempt_v1','scan_id':self.scan_id,
                    'requests_used':expected['requests_used'],
                    'source_id':adapter.rpc_source_id,'method':method,'params':params,
                    'request_bytes_base64':base64.b64encode(body).decode(),
                    'response_bytes_base64':None,'observed_at':observed,
                    'http_status':None,'failure_code':'INTAKE_DEADLINE_BEFORE_TRANSPORT'}
            self.evidence_hash=self.progress.store.save(record)
            if self.evidence_hash!=digest(record):raise PaperReadError('OUTCOME_PERSISTENCE_FAILED')
            raise PaperReadError('INTAKE_DEADLINE_BEFORE_TRANSPORT',evidence_hash=self.evidence_hash)
        try:
            result,self.evidence_hash=adapter._attempt(method,params,body,self.deadline-now)
            return result
        except PaperReadError as error:
            self.evidence_hash=error.evidence_hash;raise


def _hints(scan_id,mint,pool,signature,slot,provenance):
    if (type(scan_id) is not str or re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',scan_id) is None
            or type(slot) is not int or not 0<=slot<2**63 or provenance not in PROVENANCES):
        raise IntakeBlocked('OBSERVED_HINT_INVALID')
    address(mint);address(pool)
    authority=_pda([b'pool-authority',bytes(Pubkey.from_string(mint))],PUMP)
    expected_pool=_pda([b'pool',b'\0\0',bytes(Pubkey.from_string(authority)),
                        bytes(Pubkey.from_string(mint)),bytes(Pubkey.from_string(SOL))],AMM)
    if pool!=expected_pool:raise IntakeBlocked('OBSERVED_POOL_BINDING_INVALID')
    if type(signature) is not str or not 64<=len(signature)<=88:
        raise IntakeBlocked('OBSERVED_SIGNATURE_INVALID')
    raw=unbase58(signature)
    if len(raw)!=64 or base58(raw)!=signature:raise IntakeBlocked('OBSERVED_SIGNATURE_INVALID')


def intake(research_db,evidence_db,*,scan_id,mint,pool,signature,slot,provenance,
           now=None,credentials_loader=None,paper_token_profile_version=0):
    from .token2022_paper import check_version
    check_version(paper_token_profile_version)
    _hints(scan_id,mint,pool,signature,slot,provenance)
    now=int(time.time()) if now is None else now
    if type(now) is not int or not 0<=now<2**63:raise IntakeBlocked('CLOCK_INVALID')
    research=canonical_job_path(research_db);evidence=canonical_ownership_path(evidence_db)
    if research==evidence or any(not p.is_file() or p.stat().st_size>256*1024*1024 for p in (research,evidence)):
        raise IntakeBlocked('EXISTING_DISTINCT_DATABASES_REQUIRED')
    result={'kind':'migration_slot_intake_v1','status':'BLOCKED','scan_id':scan_id,
            'request_evidence_refs':[],'transport_evidence_refs':[],'provider_calls':0,'blockers':[],
            'eligible_for_trading':False,'entry_authorized':False,'source_authenticated':False,
            'ownership_verified':False,'finality_authenticated':False,
            'risk_flags':['UNRESOLVED_OWNERSHIP_HISTORY'],
            'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False}
    with _worker_lock(research) as locked:
        if locked is None:raise IntakeBlocked('RESEARCH_WORKER_BUSY')
        with _lock(str(evidence)+'.ownership-invocation.lock') as locked:
            if not locked:raise IntakeBlocked('EVIDENCE_INVOCATION_BUSY')
            jobs,_,_= _context(research,evidence)
            store=_ExistingStore(evidence,read_only=True)
            progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
            with closing(jobs.connect()) as c:
                _bounded(c,'scan_jobs','descriptor','WHERE scan_id=?',(scan_id,),1,8192)
                _bounded(c,'scans','result','WHERE id=?',(scan_id,),1,2*1024*1024)
            # Admission checks do not initialize/recover/migrate either database.
            try:_admission(jobs,progress,ObservationTarget(scan_id,mint,pool,mint,1))
            except _Blocked:raise IntakeBlocked('PERSISTED_BIRTH_ADMISSION_REQUIRED') from None
            descriptor=jobs.descriptor(scan_id);source=jobs.source(scan_id)
            if descriptor.get('paper_token_profile_version',0) not in (0,paper_token_profile_version):
                raise IntakeBlocked('TOKEN_PROFILE_DESCRIPTOR_MISMATCH')
            setup=_Setup.__new__(_Setup);setup.store=store;setup.descriptor=descriptor;setup.identity=scan_id
            state=setup.read()
            if _policy(state,mint=mint,token_profile_version=paper_token_profile_version)['decision']!='PASS_TOKEN_POLICY':raise IntakeBlocked('UNSUPPORTED_TOKEN')
            cutoff=state.get('cutoff')
            if type(cutoff) is not int or not 0<=cutoff<2**63 or slot>cutoff:
                raise IntakeBlocked('SLOT_BEYOND_IMMUTABLE_FINALIZED_CUTOFF')
            if source['result']:
                report=json.loads(source['result'])
                if report.get('report_hash')!=digest({k:v for k,v in report.items() if k!='report_hash'}):
                    raise IntakeBlocked('ORIGINAL_SOURCE_INVALID')
                _validate_binding(report,descriptor,state)
            with closing(store.connect()) as c:
                for table,columns in (('pages','hash,payload,raw_bytes'),
                                      ('ownership_history','id,budget,query,coverage,status,attempts')):
                    c.execute(f'SELECT {columns} FROM {table} LIMIT 0')
                _bounded(c,'ownership_history','coverage','WHERE budget=?',(scan_id,),18,2*1024*1024)
                _bounded(c,'ownership_history','query','WHERE budget=?',(scan_id,),18,1024*1024)
                key=digest({'budget':scan_id,'query':{'address':pool,'start':0,'end':1,
                    'token_accounts_filter':'none','slot_range':{'gte':slot,'lt':slot+1}}})
                count=c.execute('SELECT COUNT(*) FROM ownership_history WHERE budget=?',(scan_id,)).fetchone()[0]
                if count==18 and not c.execute('SELECT 1 FROM ownership_history WHERE id=?',(key,)).fetchone():
                    raise IntakeBlocked('HISTORY_QUERY_BOUND_EXCEEDED')
            store.read_only=False
            key=progress.create(scan_id,pool,0,1,{'gte':slot,'lt':slot+1})
            started=None
            while True:
                adapter=_SlotSource(progress,scan_id,key,slot=slot,pool=pool,timeout_seconds=10)
                current=adapter.before;coverage=current['coverage']
                pages=coverage['pages'] if coverage else []
                if current['status'] not in ('PENDING','DONE','RETRYABLE_ERROR'):
                    result['blockers']=['HISTORY_STATUS_INVALID'];break
                # A crash after reservation or a failed I/O cannot be hidden by retry.
                if current['attempts']!=len(pages) or current['status']=='RETRYABLE_ERROR':
                    result['blockers']=['CHARGED_OR_AMBIGUOUS_HISTORY_ATTEMPT'];break
                if current['attempts']>MAX_ATTEMPTS:
                    result['blockers']=['MIGRATION_SLOT_PAGE_LIMIT'];break
                if current['status']=='DONE':break
                if current['attempts']>=MAX_ATTEMPTS:
                    result['blockers']=['MIGRATION_SLOT_PAGE_LIMIT'];break
                if started is None:
                    # Local authorization/recovery replay precedes the bounded
                    # provider phase; it cannot consume the I/O deadline.
                    if credentials_loader is not None:
                        credentials_loader();credentials_loader=None
                    started=time.monotonic()
                remaining=10-(time.monotonic()-started)
                if not math.isfinite(remaining) or remaining>10:
                    result['blockers']=['INTAKE_DEADLINE_EXCEEDED'];break
                adapter=_SlotSource(progress,scan_id,key,slot=slot,pool=pool,timeout_seconds=remaining if remaining>0 else 10)
                if remaining<=0:
                    # Conservative owning reservation remains charged. This
                    # adapter records local expiry and never opens transport.
                    adapter.deadline=adapter.started
                before=current['requests_used'];current=progress.advance(key,adapter)
                result['provider_calls']+=current['requests_used']-before
                if adapter.evidence_hash:result['transport_evidence_refs'].append(adapter.evidence_hash)
                if current.get('blocked') or current.get('busy'):
                    result['blockers']=[current.get('blocked') or 'HISTORY_BUSY'];break
            result['history_id']=key;result['requests_used']=progress.admission(scan_id)['requests_used']
            current=_SlotSource(progress,scan_id,key,slot=slot,pool=pool,timeout_seconds=10).before
            coverage=current['coverage']
            if coverage:
                replay_history(coverage,store)
                result['request_evidence_refs']=[p['request_evidence_hash'] for p in coverage['pages']]
            if not result['blockers']:
                if not coverage or not coverage['query_coverage_verified']:
                    result['blockers']=['FINALIZED_SLOT_COVERAGE_UNVERIFIED']
                else:
                    rows=[]
                    for page in coverage['pages']:rows.extend(store.load(page['payload_hash'])['data'])
                    witness=extract_graduation(rows,mint=mint,pool=pool,now=now,provenance=provenance)
                    result['migration']=witness
                    if witness['status']!='OBSERVED_MIGRATION':result['blockers']=witness['blockers']
                    elif not any(w['signature']==signature and w['slot']==slot for w in witness['witnesses']):
                        result['blockers']=['OBSERVED_MIGRATION_SIGNATURE_MISMATCH']
                    else:result['status']='RETAINED_MIGRATION_WITNESS'
            return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    p.add_argument('--coordinator-intake',action='store_true',required=True)
    for name in ('research-db','evidence-db','scan-id','mint','pool','signature','slot','provenance'):
        p.add_argument('--'+name,required=True,type=int if name=='slot' else str)
    p.add_argument('--systemd-credentials',action='store_true')
    p.add_argument('--paper-token-profile-version',type=int,choices=(1,2),default=0)
    a=p.parse_args(argv)
    try:
        loader=None
        if a.systemd_credentials:
            from .paper_cycle_cli import _credentials
            loader=_credentials
        result=intake(a.research_db,a.evidence_db,scan_id=a.scan_id,mint=a.mint,pool=a.pool,
                      signature=a.signature,slot=a.slot,provenance=a.provenance,credentials_loader=loader,paper_token_profile_version=a.paper_token_profile_version)
        code=0 if result['status']=='RETAINED_MIGRATION_WITNESS' else 2
    except (ValueError,TypeError,KeyError,IndexError,AttributeError,OSError,sqlite3.Error,OverflowError,RecursionError) as error:
        result={'status':'BLOCKED','blockers':[str(error) if isinstance(error,IntakeBlocked) else 'PERSISTED_INPUT_UNAVAILABLE'],
                'entry_authorized':False,'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False};code=2
    print(json.dumps(result,sort_keys=True));return code


if __name__=='__main__':raise SystemExit(main())
