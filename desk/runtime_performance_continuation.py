"""One pinned performance-only successor after the four immutable runtime edges.

Local coordinator authority only. No counters, journals or original receipts change.
"""
from contextlib import closing,contextmanager
from pathlib import Path
import sqlite3
from . import runtime_compatibility as runtime,runtime_extensions as extensions
from .model import canonical,digest

TABLE='paper_runtime_performance_continuation'
POLICY=Path(__file__).resolve().parents[1]/'config/runtime-performance-continuation.json'
PREDECESSOR='6dd1ec3f12fe74c3fe94fab5c4be5736e94ba062b6766b9bba3cab3daf319b83'
HASHES={'first_receipt_hash','continuation_receipt_hash','extension_prefix_hash','parent_receipt_hash',
        'predecessor','successor','config_hash','metadata_hash','checkpoint_hash','events_hash','outcomes_hash'}
PINS=HASHES|{'context','events_count','outcomes_count','dispatch_predecessor','dispatch_successor','dispatch_journal_prefix'}
FIELDS=PINS|{'version','kind','original_metadata','original_checkpoint'}
KIND='PERFORMANCE_ONLY_FOUR_EDGE_SUCCESSOR_V1'


def schema():return f'CREATE TABLE {TABLE}(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL,payload_hash TEXT NOT NULL)'
def guards():
    result={TABLE+'_insert':f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN EXISTS(SELECT 1 FROM {TABLE}) BEGIN SELECT RAISE(ABORT,'Performance continuation immutable'); END"}
    return result|{TABLE+'_'+a.lower():f"CREATE TRIGGER {TABLE}_{a.lower()} BEFORE {a} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Performance continuation immutable'); END" for a in ('UPDATE','DELETE')}


def _pin_shape(pin):
    if (type(pin) is not dict or set(pin)!=PINS or not all(runtime._hash(pin[k]) for k in HASHES)
            or pin['predecessor']!=PREDECESSOR or pin['predecessor']==pin['successor']
            or not runtime._context_shape(pin['context'])
            or any(type(pin[k]) is not int or not 0<=pin[k]<=10000 for k in ('events_count','outcomes_count'))):
        raise ValueError('Exact performance continuation pin required')
    old,new=pin['dispatch_predecessor'],pin['dispatch_successor']
    if (type(old) is not dict or type(new) is not dict or set(old)!=set(new)
            or old.get('source_hash')!=pin['predecessor'] or new.get('source_hash')!=pin['successor']
            or type(old.get('paths')) is not dict
            or any(type(old['paths'].get(k)) is not dict for k in pin['context'])
            or old.get('config_hash')!=pin['config_hash'] or new.get('config_hash')!=pin['config_hash']
            or any(not runtime._hash(x.get(k)) for x in (old,new) for k in ('source_hash','tool_hash','entry_tool_hash'))
            or new!={**old,**{k:new[k] for k in ('source_hash','tool_hash','entry_tool_hash')}}
            or any(old.get('paths',{}).get(k,{}).get('path')!=v for k,v in pin['context'].items())):
        raise ValueError('Only exact source/tool dispatcher rollover permitted')
    prefix=pin['dispatch_journal_prefix']
    if (type(prefix) is not dict or set(prefix)!={'context','intents','results'}
            or any(type(prefix[k]) is not list for k in prefix) or len(prefix['context'])!=1
            or any(len(prefix[k])>4096 for k in ('intents','results'))):
        raise ValueError('Bounded complete dispatcher prefix required')
    return pin


def approved(pin):
    _pin_shape(pin)
    with POLICY.open('rb') as f:raw=f.read(runtime.MAX_BYTES+1)
    if len(raw)>runtime.MAX_BYTES:raise ValueError('Performance policy byte bound')
    policy=runtime._parse(raw.decode())
    if (type(policy) is not dict or set(policy)!={'version','continuations'} or type(policy['version']) is not int
            or policy['version']!=1 or type(policy['continuations']) is not list or len(policy['continuations'])!=1):
        raise ValueError('One independently reviewed performance continuation required')
    wanted=_pin_shape(policy['continuations'][0])
    if pin!=wanted:raise ValueError('Performance continuation not reviewed')
    return pin


def read(c):
    found=c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name=? OR substr(name,1,?)=? OR tbl_name=?',(TABLE,len(TABLE)+1,TABLE+'_',TABLE)).fetchall()
    if not found:return None
    expected={('table',TABLE,TABLE,schema())}|{('trigger',k,TABLE,s) for k,s in guards().items()}
    if len(found)!=len(expected) or set(found)!=expected:raise ValueError('Performance receipt schema/guards incomplete')
    if c.execute('SELECT count(*) FROM '+TABLE).fetchone()!=(1,):raise ValueError('One performance receipt required')
    shape=c.execute('SELECT id,typeof(payload),typeof(payload_hash),length(CAST(payload AS BLOB)),length(CAST(payload_hash AS BLOB)) FROM '+TABLE).fetchone()
    if shape is None or shape[0]!=1 or shape[1:3]!=('text','text') or not 0<shape[3]<=runtime.MAX_BYTES or shape[4]!=64:
        raise ValueError('Performance receipt scalar bound')
    raw,key=c.execute('SELECT payload,payload_hash FROM '+TABLE+' WHERE id=1').fetchone();v=runtime._parse(raw)
    if (type(v) is not dict or set(v)!=FIELDS or type(v['version']) is not int or v['version']!=1
            or v['kind']!=KIND or not runtime._hash(key) or canonical(v)!=raw or digest(v)!=key):
        raise ValueError('Performance receipt hash/shape invalid')
    approved({k:v[k] for k in PINS})
    return v,key


def namespace(c,expected):
    """Only a scalar-bounded, exactly pinned receipt permits the extra table."""
    if read(c) is not None:
        from . import runtime_empty_history_successor as empty
        return expected|{TABLE}|({empty.TABLE} if empty.read(c) is not None else set())
    return expected


def _prefix(c,pin):
    rows=extensions._bounded_rows(c)
    if len(rows)!=4 or digest(rows)!=pin['extension_prefix_hash'] or rows[-1][2]!=pin['parent_receipt_hash']:
        raise ValueError('Exact four-edge immutable prefix required')
    first,fh,base,bh=extensions._base(c,extended=True)
    previous,_=extensions._validate(c,rows,first,fh,base,bh,pin['predecessor'])
    if (fh!=pin['first_receipt_hash'] or bh!=pin['continuation_receipt_hash']
            or base['context']!=pin['context'] or base['config_hash']!=pin['config_hash']):
        raise ValueError('Original receipt/context binding changed')
    if pin['successor'] in {first['predecessor'],first['successor'],base['successor']}|{runtime._parse(r[1])['successor'] for r in rows}:
        raise ValueError('Performance successor reuses historical source')
    return previous


def require(c,*,implementation=None):
    record=read(c)
    if record is None:raise ValueError('Performance receipt missing')
    v,key=record;pin={k:v[k] for k in PINS};previous=_prefix(c,pin)
    metadata=extensions._metadata(c)
    if (v['original_metadata']!=metadata or digest(metadata)!=v['metadata_hash']
            or type(v['original_checkpoint']) is not dict or digest(v['original_checkpoint'])!=v['checkpoint_hash']):
        raise ValueError('Performance original snapshot changed')
    for table in ('events','outcomes'):
        if v[table+'_count']<previous[table+'_count'] or runtime._prefix(c,table,v[table+'_count'])!=v[table+'_hash']:
            raise ValueError('Performance journal prefix changed')
    runtime._baseline(c,v)
    _verify_journal(v)
    current=runtime.implementation_hash() if implementation is None else implementation
    # Historical proof closures may request the original tail; they still replay
    # this installed pinned receipt and every original edge before returning it.
    from . import runtime_empty_history_successor as empty
    if empty.read(c) is not None:return empty.effective(c,v,current)
    if current not in (v['predecessor'],v['successor']):raise ValueError('Performance runtime is not reviewed')
    return current


def dispatch_binding(c,expected,journal):
    from . import runtime_empty_history_successor as empty
    expected,extra_bindings,extra_contexts=empty.dispatch_binding(c,expected,journal)
    record=read(c)
    if record is None:return expected,{},{}
    v,_=record;require(c,implementation=v['successor']);_verify_journal(v,c=journal)
    if expected not in (v['dispatch_predecessor'],v['dispatch_successor']):
        raise ValueError('Unreviewed performance dispatcher context')
    from . import paper_migration_no_entry as migration
    bindings={row[1]:runtime._parse(row[-2])['context_hash'] for row in v['dispatch_journal_prefix']['intents']}
    old=v['dispatch_predecessor']
    return old,{**bindings,**extra_bindings},{digest(old):old,**extra_contexts}


def _verify_journal(v,*,c=None,initial=False):
    from . import paper_migration_no_entry as migration
    if c is None:
        from tools.paper_entry_dispatcher import _path
        path=_path(v['dispatch_predecessor']['journal'],private=True)
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as journal:
            journal.execute('BEGIN')
            return _verify_journal(v,c=journal,initial=initial)
    _,actual=migration._journal(c)
    for table,old in v['dispatch_journal_prefix'].items():
        if actual[table][:len(old)]!=old or (initial and actual[table]!=old):
            raise ValueError('Reviewed dispatcher journal prefix changed')
    return actual


def _validate_predecessor_journal(c,expected):
    # Read-only planning while the new binary is not yet authorized for this
    # ledger. Reuse the existing exact predecessor proof selector, never replace
    # the running implementation hash or let an entry caller supply authority.
    from tools import paper_entry_dispatcher as dispatcher
    from . import paper_migration_no_entry as migration
    from .evidence import EvidenceStore
    if expected['source_hash']!=PREDECESSOR:raise ValueError('Exact deployed predecessor required')
    values,_=migration._journal(c)
    if values['context']=={1:expected}:
        return dispatcher._check_records(c,expected,values,{}, {},review_source=PREDECESSOR)
    if set(values['context'])!={1}:raise ValueError('Original dispatcher context missing')
    store=EvidenceStore(expected['paths']['evidence_db']['path'],read_only=True)
    with closing(store.connect()) as evidence:
        matches=[v for v in migration.rows(evidence) if v['association']==migration.PREWIRE and v['successor_context']==expected]
    if len(matches)!=1:raise ValueError('Exact installed predecessor dispatcher certificate required')
    bindings,retired,contexts=migration._prewire_lineage(c,matches[0],values['context'][1],review_source=PREDECESSOR)
    return dispatcher._check_records(c,expected,values,bindings,retired,historical_contexts=contexts,review_source=PREDECESSOR)


@contextmanager
def _journal_lock(ctx):
    from tools import paper_entry_dispatcher as dispatcher
    from .paper_cycle import _lock
    path=dispatcher._path(ctx['journal'],private=True)
    with _lock(str(path)+'.dispatcher.lock') as locked:
        if not locked:raise ValueError('Dispatcher busy')
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
            c.execute('BEGIN');yield c


def append(research_db,evidence_db,ledger_db,cfg,*,pin):
    """One locked atomic append; no historical schema or row is rewritten."""
    from .job_persistence import canonical_job_path
    from .history_progress import canonical_ownership_path
    from .paper_observe_cli import _worker_lock
    from .paper_cycle import _lock
    research=canonical_job_path(research_db);evidence=canonical_ownership_path(evidence_db);ledger=canonical_job_path(ledger_db)
    approved(pin)
    actual={'research_db':str(research),'evidence_db':str(evidence),'ledger_db':str(ledger)}
    if type(cfg) is not dict or actual!=pin['context'] or digest(cfg)!=pin['config_hash'] or runtime.implementation_hash()!=pin['successor']:
        raise ValueError('Exact performance source/config/context required')
    with _journal_lock(pin['dispatch_predecessor']) as journal:
        _verify_journal(pin,c=journal)
        with _worker_lock(research) as locked:
            if locked is None:raise ValueError('Research busy')
            with _lock(str(evidence)+'.ownership-invocation.lock') as locked:
                if not locked:raise ValueError('Evidence busy')
                with _lock(str(ledger)+'.paper-cycle.lock') as locked:
                    if not locked:raise ValueError('Ledger busy')
                    runtime.require_transition_context(research,evidence,ledger,reviewed_context=pin['context'])
                    with closing(sqlite3.connect(ledger.as_uri()+'?mode=rw',uri=True,isolation_level=None)) as c:
                        c.execute('BEGIN IMMEDIATE')
                        try:
                            existing=read(c)
                            if existing is not None:
                                require(c)
                                if {k:existing[0][k] for k in PINS}!=pin:raise ValueError('Conflicting performance replay')
                                c.commit();return {'status':'ALREADY_RECORDED','receipt_hash':existing[1],'effective_runtime_hash':pin['successor']}
                            _verify_journal(pin,c=journal,initial=True)
                            _prefix(c,pin);snapshot=extensions._snapshot(c,cfg)
                            if any(pin[k]!=snapshot[k] for k in snapshot if k in PINS):raise ValueError('Reviewed performance snapshot changed')
                            v=pin|snapshot|{'version':1,'kind':KIND};raw=canonical(v)
                            if len(raw.encode())>runtime.MAX_BYTES:raise ValueError('Performance publication bound')
                            c.execute(schema())
                            for sql in guards().values():c.execute(sql)
                            c.execute('INSERT INTO '+TABLE+' VALUES(1,?,?)',(raw,digest(v)))
                            require(c);c.commit()
                            return {'status':'RECORDED','receipt_hash':digest(v),'effective_runtime_hash':pin['successor']}
                        except BaseException:c.rollback();raise


def plan(research_db,evidence_db,ledger_db,cfg,*,dispatch_predecessor,dispatch_successor):
    """Locked read-only proposal. Returned data is not approval or activation."""
    from .job_persistence import canonical_job_path
    from .history_progress import canonical_ownership_path
    from .paper_observe_cli import _worker_lock
    from .paper_cycle import _lock
    research=canonical_job_path(research_db);evidence=canonical_ownership_path(evidence_db);ledger=canonical_job_path(ledger_db)
    context={'research_db':str(research),'evidence_db':str(evidence),'ledger_db':str(ledger)}
    if type(cfg) is not dict:raise ValueError('Performance config object required')
    with _journal_lock(dispatch_predecessor) as journal:
        from tools import paper_entry_dispatcher as dispatcher
        from .paper_migration_no_entry import _journal
        _validate_predecessor_journal(journal,dispatch_predecessor)
        _,dispatch_prefix=_journal(journal)
        with _worker_lock(research) as locked:
            if locked is None:raise ValueError('Research busy')
            with _lock(str(evidence)+'.ownership-invocation.lock') as locked:
                if not locked:raise ValueError('Evidence busy')
                with _lock(str(ledger)+'.paper-cycle.lock') as locked:
                    if not locked:raise ValueError('Ledger busy')
                    runtime.require_transition_context(research,evidence,ledger,reviewed_context=context)
                    with closing(sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True)) as c:
                        c.execute('BEGIN')
                        if read(c) is not None:raise ValueError('One performance successor already installed')
                        rows=extensions._bounded_rows(c)
                        if len(rows)!=4:raise ValueError('Four original edges required')
                        first,fh,base,bh=extensions._base(c,extended=True)
                        snapshot=extensions._snapshot(c,cfg)
                        pin={k:snapshot[k] for k in PINS if k in snapshot}
                        pin.update(first_receipt_hash=fh,continuation_receipt_hash=bh,extension_prefix_hash=digest(rows),
                                   parent_receipt_hash=rows[-1][2],predecessor=PREDECESSOR,successor=runtime.implementation_hash(),
                                   config_hash=digest(cfg),context=context,dispatch_predecessor=dispatch_predecessor,
                                   dispatch_successor=dispatch_successor,dispatch_journal_prefix=dispatch_prefix)
                        _pin_shape(pin);_prefix(c,pin)
                        return pin
