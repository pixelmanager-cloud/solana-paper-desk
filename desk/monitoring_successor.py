"""Explicit bounded monitoring experiment successors; no grants or capital copy."""
from contextlib import ExitStack, closing
from pathlib import Path
import sqlite3
import math
import time

from . import allowance_policy as grants, runtime_compatibility as runtime
from .model import canonical, digest

POLICY=Path(__file__).resolve().parents[1]/'config/monitoring-successors.json'
TABLE='paper_monitoring_context_successors'
FENCE=TABLE+'_reservation_fence'
MAX_EDGES=4
HASHES={'first_hash','parent_hash','old_source','new_source','old_config_hash','new_config_hash',
        'old_origin','new_origin','old_metadata_hash','old_checkpoint_hash','old_events_hash',
        'old_outcomes_hash','new_metadata_hash','new_checkpoint_hash','new_events_hash','new_outcomes_hash',
        'reservations_hash','outcomes_hash','old_grant_hash','research_grant_hash'}
FIELDS=HASHES|{'sequence','context','at','reservation_cutoff','budget',
               'old_events_count','old_outcomes_count','new_events_count','new_outcomes_count'}


def shape(v):
    from .monitoring_handoff import _context
    return (type(v) is dict and set(v)==FIELDS and _context(v['context'])
            and all(grants.hash_value(v[k]) for k in HASHES)
            and type(v['sequence']) is int and 1<=v['sequence']<=MAX_EDGES
            and type(v['at']) is int and 0<=v['at']<2**53
            and all(type(v[k]) is int and 0<=v[k]<=100000 for k in
                    ('reservation_cutoff','old_events_count','old_outcomes_count','new_events_count','new_outcomes_count'))
            and v['new_events_count']==1 and v['new_outcomes_count']==0
            and type(v['budget']) is list and len(v['budget'])==9
            and type(v['budget'][0]) is int and v['budget'][0]==3
            and all(type(v['budget'][i]) is int and v['budget'][i]==3600 for i in (4,5))
            and type(v['budget'][6]) in (int,float) and math.isfinite(v['budget'][6]) and 0<=v['budget'][6]<=v['at']
            and type(v['budget'][7]) is int and v['budget'][7]==v['reservation_cutoff']
            and v['budget'][8] in (None,'CLOCK_ROLLBACK','SOURCE_FAILURE')
            and v['old_config_hash']!=v['new_config_hash'])


def approved(pin):
    with POLICY.open('rb') as f:raw=f.read(65537)
    if len(raw)>65536:raise ValueError('Successor policy bound')
    policy=runtime._parse(raw.decode())
    if (type(policy) is not dict or set(policy)!={'version','successors'} or type(policy['version']) is not int
            or policy['version']!=1 or type(policy['successors']) is not list or len(policy['successors'])>MAX_EDGES):
        raise ValueError('Successor policy malformed')
    seen=set()
    for v in policy['successors']:
        if not shape(v) or v['sequence'] in seen:raise ValueError('Successor pin malformed/forked')
        seen.add(v['sequence'])
    if pin not in policy['successors']:raise ValueError('Exact reviewed successor required')


def schema():
    return f'CREATE TABLE {TABLE}(seq INTEGER PRIMARY KEY CHECK(seq BETWEEN 1 AND {MAX_EDGES}),body TEXT NOT NULL,body_hash TEXT NOT NULL)'


def guards():
    result={TABLE+'_insert':f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN NEW.seq!=COALESCE((SELECT MAX(seq) FROM {TABLE}),0)+1 BEGIN SELECT RAISE(ABORT,'Successor sequence immutable'); END"}
    for action in ('UPDATE','DELETE'):
        result[TABLE+'_'+action.lower()]=f"CREATE TRIGGER {TABLE}_{action.lower()} BEFORE {action} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Monitoring successor immutable'); END"
    return result


def fence():
    return (f'CREATE TRIGGER {FENCE} BEFORE INSERT ON paper_monitoring_reservations '
            f'WHEN NEW.id<=(SELECT json_extract(body,\'$.reservation_cutoff\') FROM {TABLE} ORDER BY seq DESC LIMIT 1) '
            f'OR NEW.successor_context_hash IS NULL OR NEW.successor_context_hash!=(SELECT body_hash FROM {TABLE} ORDER BY seq DESC LIMIT 1) '
            "BEGIN SELECT RAISE(ABORT,'Current successor context required'); END")


def rows(c,first):
    """One snapshot; exact guards and scalar bounds BEFORE receipt materialization."""
    names=c.execute('SELECT name,type,tbl_name,sql FROM sqlite_master WHERE name=? OR substr(name,1,?)=?',
                    (TABLE,len(TABLE)+1,TABLE+'_')).fetchall()
    column=any(r[1]=='successor_context_hash' for r in c.execute('PRAGMA table_info(paper_monitoring_reservations)'))
    if not names:
        if column:raise ValueError('Partial successor column')
        return []
    expected={(TABLE,'table',TABLE,schema()),(FENCE,'trigger','paper_monitoring_reservations',fence())}
    expected|={(name,'trigger',TABLE,sql) for name,sql in guards().items()}
    if len(names)!=len(expected) or set(names)!=expected or not column:raise ValueError('Successor schema/guards partial')
    count=c.execute(f'SELECT count(*) FROM {TABLE}').fetchone()[0]
    if not 1<=count<=MAX_EDGES:raise ValueError('Successor count bound')
    bounds=c.execute(f'SELECT seq,typeof(body),length(CAST(body AS BLOB)),typeof(body_hash),length(CAST(body_hash AS BLOB)) FROM {TABLE} ORDER BY seq').fetchall()
    if ([r[0] for r in bounds]!=list(range(1,count+1)) or any(r[1]!='text' or not 0<r[2]<=16384 or r[3:]!=('text',64) for r in bounds)):
        raise ValueError('Successor payload bounds')
    result=[];previous,key=first;used={previous['context']['old_ledger_db'],previous['context']['new_ledger_db']}
    configs={previous['old_config_hash'],previous['new_config_hash']}
    for sequence,raw,new_key in c.execute(f'SELECT seq,body,body_hash FROM {TABLE} ORDER BY seq'):
        v=runtime._parse(raw)
        if not shape(v) or canonical(v)!=raw or digest(v)!=new_key:raise ValueError('Successor record malformed')
        approved(v)
        if (v['sequence']!=sequence or v['first_hash']!=first[1] or v['parent_hash']!=key
                or v['context']['old_ledger_db']!=previous['context']['new_ledger_db']
                or v['old_config_hash']!=previous['new_config_hash']
                or v['context']['new_ledger_db'] in used or v['new_config_hash'] in configs
                or any(v['context'][k]!=first[0]['context'][k] for k in ('research_db','evidence_db','pacing_db'))
                or v['at']<previous['at'] or v['reservation_cutoff']<previous['reservation_cutoff']
                or v['budget'][:6]!=[3,first[0]['context']['old_ledger_db'],first[0]['old_config_hash'],first[0]['original_budget'][3],3600,3600]
                or v['old_grant_hash']!=first[0]['old_grant_hash'] or v['research_grant_hash']!=first[0]['research_grant_hash']):
            raise ValueError('Successor chain conflict/cycle')
        from .monitoring_handoff import _prefix
        for table,name in (('paper_monitoring_reservations','reservations_hash'),('paper_monitoring_outcomes','outcomes_hash')):
            if _prefix(c,table,v['reservation_cutoff'])!=v[name]:raise ValueError('Successor accounting prefix changed')
        if c.execute('SELECT 1 FROM paper_monitoring_reservations WHERE id>? AND id<=? AND (successor_context_hash IS NOT ? OR at<?) LIMIT 1',
                     (previous['reservation_cutoff'],v['reservation_cutoff'],None if not result else key,previous['at'])).fetchone():
            raise ValueError('Historical successor reservation interval changed')
        used.add(v['context']['new_ledger_db']);configs.add(v['new_config_hash']);result.append((v,new_key));previous,key=v,new_key
    if c.execute('SELECT 1 FROM paper_monitoring_reservations WHERE (id<=? AND successor_context_hash IS NOT NULL) OR (id>? AND (successor_context_hash IS NOT ? OR at<?)) LIMIT 1',
                 (result[0][0]['reservation_cutoff'],previous['reservation_cutoff'],key,previous['at'])).fetchone():
        raise ValueError('Successor current reservation interval changed')
    return result


def active(c,first):
    history=rows(c,first)
    return history[-1] if history else first


def validate_retired(v):
    from .monitoring_handoff import _ledger
    old=_ledger(v['context']['old_ledger_db'],v['old_config_hash'],v['old_source'],
                original_source=v['old_origin'],historical=True)
    if (old['state']['positions'] or digest(old['metadata'])!=v['old_metadata_hash']
            or old['checkpoint_hash']!=v['old_checkpoint_hash']
            or any(old[k]!=v['old_'+k] for k in ('events_count','events_hash','outcomes_count','outcomes_hash'))):
        raise ValueError('Retired successor experiment changed')
    return old


def plan(research_db,evidence_db,old_ledger_db,new_ledger_db,pacing_db,new_cfg,*,old_source,at=None):
    """Read-only pin construction; caller holds canonical locks. Never authorizes."""
    from . import monitoring_handoff as handoff
    from .monitoring_budget import MonitoringBudget
    from .evidence import EvidenceStore
    from .job_persistence import canonical_job_path
    from .history_progress import canonical_ownership_path
    context={k:str(canonical_job_path(p)) for k,p in zip(('research_db','evidence_db','old_ledger_db','new_ledger_db','pacing_db'),
              (research_db,evidence_db,old_ledger_db,new_ledger_db,pacing_db))}
    if not handoff._context(context) or canonical_ownership_path(evidence_db)!=Path(context['evidence_db']):raise ValueError('Successor paths invalid')
    handoff._pacing(context)
    at=int(time.time()) if at is None else at
    if type(at) is not int or not 0<=at<2**53:raise ValueError('Successor clock invalid')
    store=EvidenceStore(evidence_db,read_only=True);store.read_only=False
    with closing(store.connect()) as c:
        c.execute('BEGIN')
        first=handoff.read(c)
        if first is None:raise ValueError('Original handoff required')
        history=rows(c,first);parent=history[-1] if history else first
        if len(history)>=MAX_EDGES:raise ValueError('Successor bound exhausted')
        if parent[0]['context']['new_ledger_db']!=context['old_ledger_db']:raise ValueError('Active predecessor required')
        with closing(sqlite3.connect(Path(old_ledger_db).as_uri()+'?mode=ro',uri=True)) as oldc:
            from .runtime_extensions import _metadata
            metadata=_metadata(oldc)
        old=handoff._ledger(old_ledger_db,parent[0]['new_config_hash'],old_source,original_source=metadata['implementation_hash'],historical=True)
        previous=MonitoringBudget(store,old_ledger_db,old['config']);previous.code_hash=old_source
        budget=previous._accounting(c)
        if at<max(budget[6],parent[0]['at']):raise ValueError('Successor clock regression')
        if c.execute('SELECT 1 FROM paper_monitoring_reservations r LEFT JOIN paper_monitoring_outcomes o ON o.reservation_id=r.id WHERE o.reservation_id IS NULL LIMIT 1').fetchone():raise ValueError('Pending monitoring transport')
        new=handoff._ledger(new_ledger_db,digest(new_cfg),runtime.implementation_hash())
        from .engine import initial_state
        if old['state']['positions'] or new['state']!=initial_state(new_cfg) or new['events_count']!=1 or new['outcomes_count']!=0:
            raise ValueError('Zero retired positions and independent initialized experiment required')
        from .paper_terminal_reconciliation import _pacing
        _pacing(pacing_db)
        v={'first_hash':first[1],'parent_hash':parent[1],'sequence':len(history)+1,'context':context,
           'old_source':old_source,'new_source':runtime.implementation_hash(),'old_origin':metadata['implementation_hash'],
           'new_origin':new['metadata']['implementation_hash'],'old_config_hash':digest(old['config']),
           'new_config_hash':digest(new_cfg),'at':at,'reservation_cutoff':budget[7],'budget':list(budget),
           'old_grant_hash':first[0]['old_grant_hash'],'research_grant_hash':first[0]['research_grant_hash'],
           'reservations_hash':handoff._prefix(c,'paper_monitoring_reservations',budget[7]),
           'outcomes_hash':handoff._prefix(c,'paper_monitoring_outcomes',budget[7])}
        for prefix,snapshot in (('old',old),('new',new)):
            v[prefix+'_metadata_hash']=digest(snapshot['metadata']);v[prefix+'_checkpoint_hash']=snapshot['checkpoint_hash']
            for k in ('events_count','events_hash','outcomes_count','outcomes_hash'):v[prefix+'_'+k]=snapshot[k]
        if not shape(v):raise ValueError('Successor plan malformed')
        return v


def activate(research_db,evidence_db,old_ledger_db,new_ledger_db,pacing_db,new_cfg,*,pins,at=None):
    from . import monitoring_handoff as handoff
    from .paper_observe_cli import _worker_lock
    from .paper_cycle import _lock
    from .monitoring_budget import MonitoringBudget,_research_binding
    from .evidence import EvidenceStore
    approved(pins)
    if pins['new_source']!=runtime.implementation_hash() or pins['new_config_hash']!=digest(new_cfg):raise ValueError('Reviewed successor source/config required')
    from .job_persistence import canonical_job_path
    supplied=[str(canonical_job_path(p)) for p in (research_db,evidence_db,old_ledger_db,new_ledger_db,pacing_db)]
    if any(str(p)!=resolved or not Path(resolved).is_file() for p,resolved in zip((research_db,evidence_db,old_ledger_db,new_ledger_db,pacing_db),supplied)):
        raise ValueError('Existing canonical successor paths required')
    if supplied!=[pins['context'][k] for k in ('research_db','evidence_db','old_ledger_db','new_ledger_db','pacing_db')]:raise ValueError('Successor context mismatch')
    if at is not None and at!=pins['at']:raise ValueError('Exact reviewed time required')
    # CLI arguments are strings; internal binding/ledger helpers require Path.
    # Convert only AFTER canonical spelling and exact reviewed pin validation.
    research_db,evidence_db,old_ledger_db,new_ledger_db,pacing_db=map(Path,supplied)
    with ExitStack() as locks:
        if locks.enter_context(_worker_lock(research_db)) is None:raise ValueError('Research busy')
        if not locks.enter_context(_lock(str(evidence_db)+'.ownership-invocation.lock')):raise ValueError('Evidence busy')
        store=EvidenceStore(evidence_db,read_only=True);store.read_only=False
        with closing(store.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                first=handoff.read(c)
                if first is None:raise ValueError('Original binding missing')
                history=rows(c,first)
                ledgers={first[0]['context']['old_ledger_db'],first[0]['context']['new_ledger_db'],str(new_ledger_db)}
                ledgers|={v['context']['new_ledger_db'] for v,_ in history}
                for path in sorted(ledgers):
                    if not locks.enter_context(_lock(path+'.paper-cycle.lock')):raise ValueError('Ledger busy')
                _research_binding(research_db,evidence_db)
                active_budget=MonitoringBudget(store,new_ledger_db,new_cfg,clock=lambda:pins['at'])
                if pins['sequence']<=len(history):
                    if history[pins['sequence']-1][0]!=pins:raise ValueError('Successor replay conflict')
                    if pins['sequence']!=len(history):raise ValueError('Retired successor replay refused')
                    active_budget._accounting(c);c.commit();return {'status':'ALREADY_BOUND','binding_hash':history[-1][1],'separate_experiment':True}
                # Stable DB snapshot while all participating contexts are quiescent.
                candidate=plan(research_db,evidence_db,old_ledger_db,new_ledger_db,pacing_db,new_cfg,old_source=pins['old_source'],at=pins['at'])
                if candidate!=pins:raise ValueError('Reviewed successor anchors changed')
                if not history:
                    c.execute('ALTER TABLE paper_monitoring_reservations ADD COLUMN successor_context_hash TEXT')
                    c.execute(schema())
                    for sql in guards().values():c.execute(sql)
                key=digest(pins)
                c.execute(f'INSERT INTO {TABLE} VALUES(?,?,?)',(pins['sequence'],canonical(pins),key))
                if not history:c.execute(fence())
                active_budget._accounting(c)
                c.commit();return {'status':'BOUND','binding_hash':key,'separate_experiment':True}
            except BaseException:c.rollback();raise
