"""One explicit separate-experiment binding of an EXISTING monitoring allowance.

Reviewed local policy only; never provider authenticity, entry permission, capital
continuation, provisioning, counter reset or alteration of an original receipt.
"""
from contextlib import ExitStack, closing
import os
import math
from pathlib import Path
import sqlite3
import time

from . import allowance_policy as grants, runtime_compatibility as runtime
from .model import canonical, digest

POLICY = Path(__file__).resolve().parents[1]/'config/monitoring-handoff.json'
TABLE = 'paper_monitoring_context_handoff'
CONTEXT = {'research_db','evidence_db','old_ledger_db','new_ledger_db','pacing_db'}
PINS = {'context','old_source','new_source','old_config_hash','new_config_hash',
        'old_runtime_hash','old_grant_hash','research_grant_hash'}
FIELDS = PINS | {'kind','at','old_checkpoint_hash','new_initial_checkpoint_hash',
                  'old_metadata_hash','old_events_count','old_events_hash',
                  'old_outcomes_count','old_outcomes_hash','original_budget',
                  'reservation_cutoff','reservations_hash','outcomes_hash'}
FENCE = 'paper_monitoring_reservation_context_fence'
VERSION_FENCE = 'paper_monitoring_active_version_fence'


def _context(value):
    return (type(value) is dict and set(value)==CONTEXT and len(set(value.values()))==5
            and all(type(p) is str and 0<len(p.encode())<=4096 and Path(p).is_absolute()
                    and str(Path(p).resolve())==p for p in value.values()))


def approved(pins):
    with POLICY.open('rb') as stream:raw=stream.read(65537)
    if len(raw)>65536:raise ValueError('Handoff policy bound')
    policy=runtime._parse(raw.decode())
    if (type(policy) is not dict or set(policy)!={'version','handoffs'}
            or type(policy['version']) is not int or policy['version']!=1
            or type(policy['handoffs']) is not list or len(policy['handoffs'])>8):
        raise ValueError('Invalid reviewed handoff policy')
    seen=set()
    for entry in policy['handoffs']:
        if (type(entry) is not dict or set(entry)!=PINS or not _context(entry['context'])
                or any(not grants.hash_value(entry[k]) for k in PINS-{'context'})
                or entry['old_source']==entry['new_source']
                or entry['old_config_hash']==entry['new_config_hash']):
            raise ValueError('Invalid reviewed handoff edge')
        key=canonical(entry)
        if key in seen:raise ValueError('Duplicate reviewed handoff edge')
        seen.add(key)
    if pins not in policy['handoffs']:raise ValueError('Explicit reviewed handoff required')


def _fence():
    return (f'CREATE TRIGGER {FENCE} BEFORE INSERT ON paper_monitoring_reservations '
            f'WHEN NEW.id<=(SELECT json_extract(body,\'$.reservation_cutoff\') FROM {TABLE} WHERE id=1) '
            f'OR NEW.context_hash IS NULL OR NEW.context_hash!=(SELECT body_hash FROM {TABLE} WHERE id=1) '
            "BEGIN SELECT RAISE(ABORT,'Active monitoring context required'); END")


def _version_fence():
    return (f'CREATE TRIGGER {VERSION_FENCE} BEFORE UPDATE ON paper_monitoring_budget '
            "WHEN NEW.version!=3 BEGIN SELECT RAISE(ABORT,'Active monitoring version required'); END")


def _prefix(c,table,cutoff):
    if type(cutoff) is not int or not 0<=cutoff<=100000:raise ValueError('Monitoring prefix bound')
    columns=('id,at,scan_id,mint,checkpoint_hash,method,params_hash' if table=='paper_monitoring_reservations'
             else 'reservation_id,evidence_hash')
    primary=columns.split(',')[0]
    byte_fields=('length(CAST(scan_id AS BLOB))+length(CAST(mint AS BLOB))+'
                 'length(CAST(checkpoint_hash AS BLOB))+length(CAST(method AS BLOB))+'
                 'length(CAST(params_hash AS BLOB))' if table=='paper_monitoring_reservations'
                 else 'length(CAST(evidence_hash AS BLOB))')
    size=c.execute(f'SELECT COUNT(*),COALESCE(SUM({byte_fields}),0) FROM {table} WHERE {primary}<=?',
                   (cutoff,)).fetchone()
    if size[0]>100000 or size[1]>32*1024*1024:raise ValueError('Monitoring prefix oversized')
    rows=list(c.execute(f'SELECT {columns} FROM {table} WHERE {primary}<=? ORDER BY {primary}',(cutoff,)))
    if table=='paper_monitoring_reservations' and [r[0] for r in rows]!=list(range(1,cutoff+1)):
        raise ValueError('Monitoring prefix incomplete')
    return digest(rows)


def read(c):
    """Exact immutable receipt/schema; partial publication never means absent."""
    value=grants.read(c,TABLE)
    columns=c.execute('PRAGMA table_info(paper_monitoring_reservations)').fetchall()
    fence=c.execute('SELECT sql FROM sqlite_master WHERE name=?',(FENCE,)).fetchall()
    version_fence=c.execute('SELECT sql FROM sqlite_master WHERE name=?',(VERSION_FENCE,)).fetchall()
    if value is None:
        if fence or version_fence or any(r[1]=='context_hash' for r in columns):raise ValueError('Partial handoff publication')
        return None
    if ([r[1:3] for r in columns]!=[('id','INTEGER'),('at','REAL'),('scan_id','TEXT'),('mint','TEXT'),
            ('checkpoint_hash','TEXT'),('method','TEXT'),('params_hash','TEXT'),('context_hash','TEXT')]
            or fence!=[(_fence(),)] or version_fence!=[(_version_fence(),)]):raise ValueError('Handoff reservation fence invalid')
    body,key=value
    if (set(body)!=FIELDS or body['kind']!='separate_paper_monitoring_handoff_v1'
            or type(body['at']) is not int or not 0<=body['at']<2**53
            or type(body['reservation_cutoff']) is not int or not 0<=body['reservation_cutoff']<=100000
            or type(body['original_budget']) is not list or len(body['original_budget'])!=9
            or any(not grants.hash_value(body[k]) for k in ('old_checkpoint_hash','new_initial_checkpoint_hash',
                'old_metadata_hash','old_events_hash','old_outcomes_hash','reservations_hash','outcomes_hash'))):
        raise ValueError('Handoff binding invalid')
    original=body['original_budget']
    if (original[:6]!=[2,body['context']['old_ledger_db'],body['old_config_hash'],original[3],3600,3600]
            or not grants.hash_value(original[3]) or type(original[0]) is not int
            or type(original[4]) is not int or type(original[5]) is not int
            or type(original[6]) not in (int,float) or not math.isfinite(original[6])
            or not 0<=original[6]<=body['at'] or type(original[7]) is not int
            or original[7]!=body['reservation_cutoff']
            or original[8] not in (None,'CLOCK_ROLLBACK','SOURCE_FAILURE')):
        raise ValueError('Original monitoring baseline invalid')
    approved({k:body[k] for k in PINS})
    for table,name in (('paper_monitoring_reservations','reservations_hash'),('paper_monitoring_outcomes','outcomes_hash')):
        if _prefix(c,table,body['reservation_cutoff'])!=body[name]:raise ValueError('Original monitoring prefix changed')
    if c.execute('SELECT 1 FROM paper_monitoring_reservations WHERE (id<=? AND context_hash IS NOT NULL) OR (id>? AND (context_hash IS NULL OR context_hash!=? OR at<?)) LIMIT 1',
                 (body['reservation_cutoff'],body['reservation_cutoff'],key,body['at'])).fetchone():
        raise ValueError('Monitoring context suffix invalid')
    return body,key


def _ledger(path, cfg_hash, source, *, old_runtime_hash=None):
    """Trusted exact-source contract; validate saved state/history, never rebuild."""
    from .paper_checkpoint import validate_checkpoint, read_checkpoint
    with closing(sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN')
        size=c.execute('SELECT COUNT(*),COALESCE(SUM(length(CAST(value AS BLOB))),0) FROM metadata').fetchone()
        if size[0]>64 or size[1]>runtime.MAX_BYTES:raise ValueError('Handoff metadata bound')
        metadata=dict(c.execute('SELECT key,value FROM metadata'))
        cfg=runtime._parse(metadata.get('config'))
        if type(cfg) is not dict or digest(cfg)!=cfg_hash or metadata.get('config_hash')!=cfg_hash:
            raise ValueError('Handoff ledger config mismatch')
        if metadata.get('paper_cycle')!='explicit-quote-cycle-v1':raise ValueError('Explicit experiment ledger required')
        runtime.require_runtime(c,implementation=source)
        runtime._history(c,cfg)
        size=c.execute('SELECT length(CAST(payload AS BLOB)) FROM state WHERE id=1').fetchone()
        if size is None or not 0<size[0]<=runtime.MAX_BYTES:raise ValueError('Handoff checkpoint bound')
        row=c.execute('SELECT payload FROM state WHERE id=1').fetchone()
        if row is None:raise ValueError('Handoff checkpoint missing')
        state=validate_checkpoint(c,row[0])
        if old_runtime_hash is not None:
            receipt=c.execute(f'SELECT payload_hash FROM {runtime.TABLE} WHERE id=1').fetchone()
            if receipt!=(old_runtime_hash,):raise ValueError('Old runtime receipt mismatch')
        else:
            if source!=runtime.implementation_hash() or metadata.get('implementation_hash')!=source:
                raise ValueError('New experiment source mismatch')
            read_checkpoint(c)
        result={'state':state,'metadata':metadata,'checkpoint_hash':digest(state),'config':cfg}
        for table in ('events','outcomes'):
            count=c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            result[table+'_count']=count;result[table+'_hash']=runtime._prefix(c,table,count)
        return result


def _pacing(context):
    from . import provider_pacing
    path=context['pacing_db']
    if os.environ.get(provider_pacing.ENV)!=path:raise ValueError('Existing shared pacing context required')
    provider_pacing.Pacer(path,priority='held')  # Validation only; no slots or resets.


def validate_active(c,budget):
    """Current readers must use only the reviewed successor experiment."""
    value=read(c)
    if value is None:return None
    body,key=value;ctx=body['context']
    if (str(budget.path)!=ctx['evidence_db'] or str(budget.ledger)!=ctx['new_ledger_db']
            or budget.config_hash!=body['new_config_hash'] or budget.code_hash!=body['new_source']):
        raise ValueError('Retired or mismatched monitoring context')
    _pacing(ctx)
    old=_ledger(ctx['old_ledger_db'],body['old_config_hash'],body['old_source'],old_runtime_hash=body['old_runtime_hash'])
    if (old['state']['positions'] or old['checkpoint_hash']!=body['old_checkpoint_hash']
            or digest(old['metadata'])!=body['old_metadata_hash']
            or any(old[k]!=body['old_'+k] for k in ('events_count','events_hash','outcomes_count','outcomes_hash'))):
        raise ValueError('Retired experiment changed')
    new=_ledger(ctx['new_ledger_db'],body['new_config_hash'],body['new_source'])
    from .engine import initial_state
    if digest(initial_state(new['config']))!=body['new_initial_checkpoint_hash']:
        raise ValueError('Separate experiment initialization changed')
    research=Path(ctx['research_db'])
    from .job_persistence import canonical_job_path
    from .history_progress import canonical_ownership_path
    if canonical_job_path(research)!=research or canonical_ownership_path(ctx['evidence_db'])!=budget.path:
        raise ValueError('Handoff context changed')
    with closing(sqlite3.connect(research.as_uri()+'?mode=ro',uri=True)) as jobs:
        if grants.research_limits(jobs,research)!=(1000,25) or grants.read(jobs,grants.RESEARCH)[1]!=body['research_grant_hash']:
            raise ValueError('Research allowance binding changed')
    grant=grants.monitoring_policy(c)
    if (grant is None or grant[1]!=body['old_grant_hash']
            or old['metadata'].get('implementation_hash')!=body['original_budget'][3]):
        raise ValueError('Original allowance grant changed')
    return body,key


def activate(research_db,evidence_db,old_ledger_db,new_ledger_db,pacing_db,new_cfg,*,pins,at=None):
    """Existing-only explicit operator action. One binding; no cross-DB writes."""
    from .job_persistence import canonical_job_path
    from .history_progress import canonical_ownership_path
    from .paper_observe_cli import _worker_lock
    from .paper_cycle import _lock, INIT
    from .evidence import EvidenceStore
    from .monitoring_budget import MonitoringBudget, _research_binding
    paths={'research_db':canonical_job_path(research_db),'evidence_db':canonical_ownership_path(evidence_db),
           'old_ledger_db':canonical_job_path(old_ledger_db),'new_ledger_db':canonical_job_path(new_ledger_db),
           'pacing_db':canonical_job_path(pacing_db)}
    context={k:str(v) for k,v in paths.items()}
    approved(pins)
    if context!=pins['context'] or not all(p.is_file() for p in paths.values()):raise ValueError('Reviewed existing handoff paths required')
    if pins['new_source']!=runtime.implementation_hash() or digest(new_cfg)!=pins['new_config_hash']:
        raise ValueError('Reviewed successor/config mismatch')
    if at is None:at=int(time.time())
    if type(at) is not int or not 0<=at<2**53:raise ValueError('Handoff clock invalid')
    with ExitStack() as locks:
        if locks.enter_context(_worker_lock(paths['research_db'])) is None:raise ValueError('Research worker busy')
        if not locks.enter_context(_lock(str(paths['evidence_db'])+'.ownership-invocation.lock')):raise ValueError('Evidence invocation busy')
        for p in sorted((paths['old_ledger_db'],paths['new_ledger_db'])):
            if not locks.enter_context(_lock(str(p)+'.paper-cycle.lock')):raise ValueError('Experiment cycle busy')
        _research_binding(paths['research_db'],paths['evidence_db']);_pacing(context)
        store=EvidenceStore(paths['evidence_db'],read_only=True);store.read_only=False
        store.connect=lambda:sqlite3.connect(paths['evidence_db'].as_uri()+'?mode=rw',uri=True,isolation_level=None)
        active=MonitoringBudget(store,paths['new_ledger_db'],new_cfg,clock=lambda:at)
        with closing(store.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                existing=read(c)
                if existing:
                    if {k:existing[0][k] for k in PINS}!=pins:raise ValueError('Handoff already bound differently')
                    active._checkpoint();active._accounting(c)
                    c.commit();return {'status':'ALREADY_BOUND','binding_hash':existing[1],'separate_experiment':True}
                old=_ledger(context['old_ledger_db'],pins['old_config_hash'],pins['old_source'],old_runtime_hash=pins['old_runtime_hash'])
                new=_ledger(context['new_ledger_db'],pins['new_config_hash'],pins['new_source'])
                if old['state']['positions'] or new['state']['positions'] or new['events_count']!=1 or new['outcomes_count']!=0:
                    raise ValueError('Zero old positions and explicitly initialized untraded new experiment required')
                from .engine import initial_state
                if new['state']!=initial_state(new_cfg):raise ValueError('Independent configured initial capital required')
                with closing(sqlite3.connect(paths['new_ledger_db'].as_uri()+'?mode=ro',uri=True)) as ledger:
                    if ledger.execute('SELECT event_id,payload FROM events').fetchone()!=(INIT['event_id'],canonical(INIT)):
                        raise ValueError('New experiment initial journal required')
                previous=MonitoringBudget(store,paths['old_ledger_db'],old['config'],clock=lambda:at)
                previous.code_hash=pins['old_source']  # Exact reviewed old-source accounting preflight only.
                row=previous._accounting(c)
                grant=grants.monitoring_policy(c)
                if grant is None or grant[1]!=pins['old_grant_hash']:raise ValueError('Reviewed original grant required')
                if row[0]!=2 or row[4]!=3600 or at<max(row[6],grant[0]['at']):raise ValueError('Handoff allowance/clock mismatch')
                if c.execute('SELECT 1 FROM paper_monitoring_reservations r LEFT JOIN paper_monitoring_outcomes o ON o.reservation_id=r.id WHERE o.reservation_id IS NULL LIMIT 1').fetchone():
                    raise ValueError('Unresolved monitoring attempt prevents handoff')
                with closing(sqlite3.connect(paths['research_db'].as_uri()+'?mode=ro',uri=True)) as jobs:
                    if grants.research_limits(jobs,paths['research_db'])!=(1000,25) or grants.read(jobs,grants.RESEARCH)[1]!=pins['research_grant_hash']:
                        raise ValueError('Reviewed research grant required')
                body={**pins,'kind':'separate_paper_monitoring_handoff_v1','at':at,
                      'old_checkpoint_hash':old['checkpoint_hash'],'new_initial_checkpoint_hash':new['checkpoint_hash'],
                      'old_metadata_hash':digest(old['metadata']),'original_budget':list(row),'reservation_cutoff':row[7],
                      'reservations_hash':_prefix(c,'paper_monitoring_reservations',row[7]),
                      'outcomes_hash':_prefix(c,'paper_monitoring_outcomes',row[7])}
                for k in ('events_count','events_hash','outcomes_count','outcomes_hash'):body['old_'+k]=old[k]
                # Additive SQL fence also blocks the legacy seven-column INSERT.
                c.execute('ALTER TABLE paper_monitoring_reservations ADD COLUMN context_hash TEXT')
                key=grants.install(c,TABLE,body)
                c.execute(_fence())
                c.execute('UPDATE paper_monitoring_budget SET version=3 WHERE id=1')
                c.execute(_version_fence())
                active._checkpoint();active._accounting(c)
                c.commit()
                return {'status':'BOUND','binding_hash':key,'separate_experiment':True}
            except BaseException:
                c.rollback();raise


def main(argv=None):
    import argparse,json
    from .paper_cycle_cli import _config,_json
    parser=argparse.ArgumentParser(description='Explicit existing monitoring allowance handoff; separate experiment')
    for name in ('research-db','evidence-db','old-ledger-db','new-ledger-db','pacing-db','new-config','reviewed-pins'):
        parser.add_argument('--'+name,required=True)
    args=parser.parse_args(argv)
    try:
        result=activate(args.research_db,args.evidence_db,args.old_ledger_db,args.new_ledger_db,args.pacing_db,
                        _config(args.new_config),pins=_json(args.reviewed_pins))
        print(json.dumps(result,sort_keys=True));return 0
    except (ValueError,OSError,sqlite3.Error,TypeError,KeyError,RecursionError):
        print(json.dumps({'status':'UNAVAILABLE','blockers':['REVIEWED_EXISTING_MONITORING_HANDOFF_REQUIRED']}));return 2


if __name__=='__main__':raise SystemExit(main())
