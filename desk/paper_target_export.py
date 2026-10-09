"""Offline new-only operator targets; no admission, provider I/O or entry approval.

Candidates require explicit operator size/taker/fee and actual retained migration.
Positions derive identities/remaining raw inventory from the verified checkpoint.
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from .evidence import EvidenceStore
from .history_progress import HistoryProgress, canonical_ownership_path
from .job_persistence import JobPersistence, canonical_job_path
from .model import canonical, digest, load_config
from .replay_history import replay_history
from .paper_cycle import _config, _state, _lock, _graduation, CycleTarget
from .paper_observe_cli import _worker_lock
from .paper_observation_collector import ObservationTarget, _admission, _Blocked
from .paper_view import _history_preflight
from .paper_cycle_cli import load_targets, _json
from . import quote_execution as qe


class ExportBlocked(ValueError):
    pass


def _bounded(connection, table, column, where='', args=(), count_limit=18, byte_limit=2*1024*1024):
    count,size,largest=connection.execute(
        f'SELECT COUNT(*),COALESCE(SUM(length(CAST({column} AS BLOB))),0),COALESCE(MAX(length(CAST({column} AS BLOB))),0) '
        f'FROM (SELECT {column} FROM {table} {where} LIMIT {count_limit+1})',args).fetchone()
    if count>count_limit or size>byte_limit or largest>byte_limit:
        raise ExportBlocked('RETAINED_INPUT_BOUND_EXCEEDED')


class _Store(EvidenceStore):
    def connect(self):
        return sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=2,isolation_level=None)

    def load(self,key):
        if type(key) is not str or re.fullmatch('[0-9a-f]{64}',key) is None:
            raise ExportBlocked('ORIGINAL_REFERENCE_INVALID')
        with closing(self.connect()) as c:
            row=c.execute('SELECT raw_bytes,length(payload) FROM pages WHERE hash=?',(key,)).fetchone()
        if not row or type(row[0]) is not int or not 0<row[0]<=2*1024*1024 or row[1]>2*1024*1024+65536:
            raise ExportBlocked('ORIGINAL_MISSING_OR_OVERSIZED')
        return super().load(key)


def _context(research,evidence):
    jobs=JobPersistence.__new__(JobPersistence);jobs.path=research
    def connect():
        c=sqlite3.connect(research.as_uri()+'?mode=ro',uri=True,timeout=2)
        c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');return c
    jobs.connect=connect
    with closing(jobs.connect()) as c:
        marker=c.execute("SELECT version FROM scan_job_migrations WHERE name='legacy_screen_descriptors'").fetchone()
        if marker is None or type(marker[0]) is not int or marker[0]!=1:
            raise ExportBlocked('EXISTING_DISPATCH_SCHEMA_REQUIRED')
    store=_Store(evidence,read_only=True)
    progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
    return jobs,store,progress


def _refs(jobs,progress,target):
    with closing(jobs.connect()) as c:
        for table,column,field,limit in (('scans','result','id',2*1024*1024),('scan_jobs','descriptor','scan_id',8192)):
            _bounded(c,table,column,f'WHERE {field}=?',(target.scan_id,),1,limit)
    try:_admission(jobs,progress,target)
    except _Blocked:raise ExportBlocked('PERSISTED_ADMISSION_REQUIRED') from None
    source=jobs.source(target.scan_id)
    queries=[]
    if source['result']:
        report=json.loads(source['result']);claimed=report.get('report_hash')
        if report.get('mint')!=target.mint or claimed!=digest({k:v for k,v in report.items() if k!='report_hash'}):
            raise ExportBlocked('SAVED_SOURCE_BINDING_INVALID')
        queries.extend(report.get('history_queries',[]))
    with closing(progress.store.connect()) as c:
        _bounded(c,'ownership_history','coverage','WHERE budget=?',(target.scan_id,),18,1024*1024)
        _bounded(c,'ownership_history','query','WHERE budget=?',(target.scan_id,),18,1024*1024)
        for identity,query,coverage in c.execute('SELECT id,query,coverage FROM ownership_history WHERE budget=?',(target.scan_id,)):
            query=json.loads(query)
            if identity!=digest({'budget':target.scan_id,'query':query}):raise ExportBlocked('HISTORY_IDENTITY_INVALID')
            if coverage:
                coverage=json.loads(coverage)
                if any(coverage.get(key)!=value for key,value in query.items()):raise ExportBlocked('HISTORY_QUERY_BINDING_INVALID')
                queries.append(coverage)
    if len(queries)>36:raise ExportBlocked('HISTORY_QUERY_BOUND_EXCEEDED')
    refs=[]
    for query in queries:
        if query.get('address') not in (target.mint,target.pool):continue
        replay_history(query,progress.store)
        for page in query['pages']:
            ref=page['request_evidence_hash'];manifest=progress.store.load(ref)
            if (manifest.get('kind')!='history_request_v1' or manifest.get('response_hash')!=page['payload_hash']
                    or manifest['params'][0]!=query['address']):raise ExportBlocked('HISTORY_MANIFEST_BINDING_INVALID')
            if ref not in refs:refs.append(ref)
    if len(refs)>8:raise ExportBlocked('GRADUATION_REFERENCE_BOUND_EXCEEDED')
    return tuple(refs)


def _publish(output,payload):
    output=Path(output)
    if output.exists() or output.is_symlink():raise ExportBlocked('OUTPUT_ALREADY_EXISTS')
    raw=(canonical(payload)+'\n').encode()
    if len(raw)>65536:raise ExportBlocked('TARGET_OUTPUT_BOUND_EXCEEDED')
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent,delete=False) as stream:
            temporary=stream.name;os.chmod(temporary,0o600)
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        load_targets(temporary)  # Actual CLI140 grammar; no duplicated caller authority.
        os.link(temporary,output)  # Atomic no-overwrite publication.
    finally:
        if temporary is not None:os.unlink(temporary)


def export_targets(research_db,evidence_db,output,*,pool_fee_bps,candidate=None,ledger_db=None,cfg=None,now=None):
    if (candidate is None)==(ledger_db is None):raise ExportBlocked('SELECT_CANDIDATE_OR_POSITIONS')
    if type(pool_fee_bps) is not str or re.fullmatch(r'[0-9]{1,4}(?:\.[0-9]{1,8})?',pool_fee_bps) is None:
        raise ExportBlocked('EXPLICIT_FEE_HYPOTHESIS_REQUIRED')
    research=canonical_job_path(research_db);evidence=canonical_ownership_path(evidence_db)
    paths=[research,evidence]+([canonical_job_path(ledger_db)] if ledger_db is not None else [])
    if len(set(paths))!=len(paths) or not all(p.is_file() and p.stat().st_size<=256*1024*1024 for p in paths):
        raise ExportBlocked('DISTINCT_EXISTING_DATABASES_REQUIRED')
    if Path(output).exists() or Path(output).is_symlink():raise ExportBlocked('OUTPUT_ALREADY_EXISTS')
    now=int(time.time()) if now is None else now
    if type(now) is not int or now<0:raise ExportBlocked('TRUSTED_CLOCK_REQUIRED')
    result={'position_targets':[],'candidates':[],'usd_evidence_refs':[]}
    with _worker_lock(research) as worker:
        if worker is None:raise ExportBlocked('RESEARCH_WORKER_BUSY')
        with _lock(str(evidence)+'.ownership-invocation.lock') as locked:
            if not locked:raise ExportBlocked('EVIDENCE_INVOCATION_BUSY')
            jobs,store,progress=_context(research,evidence)
            if candidate is not None:
                required={'scan_id','pool','taker','amount_raw','provenance','known_hazards'}
                if type(candidate) is not dict or set(candidate)!=required:raise ExportBlocked('EXPLICIT_CANDIDATE_INPUTS_REQUIRED')
                # Descriptor first preflight avoids loading unbounded caller-bound dispatch.
                with closing(jobs.connect()) as c:_bounded(c,'scan_jobs','descriptor','WHERE scan_id=?',(candidate['scan_id'],),1,8192)
                mint=jobs.descriptor(candidate['scan_id'])['mint']
                row={**candidate,'mint':mint,'pool_fee_bps':pool_fee_bps,'graduation_refs':[]}
                # Validate explicit scalar/address fields before source access.
                probe=CycleTarget(ObservationTarget(*(row[k] for k in ('scan_id','mint','pool','taker','amount_raw'))),row['provenance'],None,None,pool_fee_bps)
                with tempfile.TemporaryDirectory() as temp:
                    p=Path(temp)/'input.json';p.write_text(canonical({'position_targets':[],'candidates':[row],'usd_evidence_refs':[]}));load_targets(p)
                refs=_refs(jobs,progress,probe.target)
                observed=_graduation(store,CycleTarget(probe.target,probe.provenance,None,None,pool_fee_bps,graduation_refs=refs),now)
                if observed['status']!='OBSERVED_MIGRATION':raise ExportBlocked('RETAINED_MIGRATION_WITNESS_REQUIRED')
                row['graduation_refs']=list(refs);result['candidates'].append(row)
            else:
                _config(cfg);ledger=paths[2]
                with _lock(str(ledger)+'.paper-cycle.lock') as locked:
                    if not locked:raise ExportBlocked('PAPER_CYCLE_BUSY')
                    with closing(sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True)) as c:
                        _history_preflight(c)
                        for table,column in (('state','payload'),('outcomes','payload')):_bounded(c,table,column,count_limit=10000,byte_limit=32*1024*1024)
                    state=_state(ledger,cfg)
                    if len(state['positions'])>18:raise ExportBlocked('TARGET_COUNT_BOUND_EXCEEDED')
                    with closing(sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True)) as c:
                        for mint,p in state['positions'].items():
                            event=c.execute('SELECT payload,payload_hash FROM events WHERE event_id=?',(p['entry_event_id'],)).fetchone()
                            original=json.loads(event[0]) if event else None
                            if original is None or digest(original)!=event[1]:raise ExportBlocked('ORIGINAL_ENTRY_REQUIRED')
                            scan=original['paper_source_evidence']['scan_id']
                            target=ObservationTarget(scan,mint,p['pool'],p['taker'],qe.raw_quantity(p['qty'],p['quote_execution']['mint_decimals']))
                            refs=_refs(jobs,progress,target)
                            result['position_targets'].append({'scan_id':scan,'mint':mint,'pool':p['pool'],'taker':p['taker'],
                                'amount_raw':target.amount_raw,'provenance':p['provenance'],'pool_fee_bps':pool_fee_bps,
                                'graduation_refs':list(refs),'known_hazards':[]})
            _publish(output,result)
    return {'status':'EXPORTED','execution_status':'EXECUTION_UNVERIFIED','live_readiness':False,
            'risk_flags':['UNRESOLVED_OWNERSHIP_HISTORY'],'candidates':len(result['candidates']),
            'positions':len(result['position_targets']),'notice':'Targets only; held exits require reviewed exit-only cycle dispatch. No source authentication or entry permission.'}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('research-db','evidence-db','output','pool-fee-bps'):p.add_argument('--'+name,required=True)
    sub=p.add_subparsers(dest='mode',required=True)
    entry=sub.add_parser('candidate')
    for name in ('scan-id','pool','taker','amount-raw','provenance'):entry.add_argument('--'+name,required=True,type=int if name=='amount-raw' else str)
    entry.add_argument('--known-hazard',action='append',default=[])
    positions=sub.add_parser('positions');positions.add_argument('--ledger-db',required=True);positions.add_argument('--config',required=True)
    a=p.parse_args(argv)
    try:
        kwargs={'pool_fee_bps':a.pool_fee_bps}
        if a.mode=='candidate':kwargs['candidate']={'scan_id':a.scan_id,'pool':a.pool,'taker':a.taker,'amount_raw':a.amount_raw,'provenance':a.provenance,'known_hazards':a.known_hazard}
        else:
            config=_json(a.config)
            if type(config) is not dict:raise ExportBlocked('BOUNDED_OBJECT_CONFIG_REQUIRED')
            # Validate the bounded, duplicate-free bytes through existing semantics.
            with tempfile.TemporaryDirectory() as temp:
                path=Path(temp)/'config.json';path.write_text(canonical(config))
                kwargs.update(ledger_db=a.ledger_db,cfg=load_config(path))
        result=export_targets(a.research_db,a.evidence_db,a.output,**kwargs)
        code=0
    except (ValueError,KeyError,TypeError,OSError,sqlite3.Error,OverflowError,RecursionError):
        import sys
        error=sys.exc_info()[1]
        result={'status':'BLOCKED','blockers':[str(error) if isinstance(error,ExportBlocked) else 'PERSISTED_INPUT_UNAVAILABLE'],'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False};code=2
    print(json.dumps(result,sort_keys=True));return code


if __name__=='__main__':raise SystemExit(main())
