"""One reviewed successor for empty-history rejection and reconciliation.

Preserves the installed performance receipt and four immutable extension rows.
No further successor, counter reset, or automatic policy selection is supported.
"""
from contextlib import closing, ExitStack
from pathlib import Path
import sqlite3
from . import runtime_compatibility as runtime
from . import runtime_performance_continuation as parent
from .model import canonical, digest

TABLE='paper_runtime_empty_history_successor'
POLICY=Path(__file__).resolve().parents[1]/'config/runtime-empty-history-successor.json'
FIELDS={'version','kind','parent_hash','predecessor','successor','config_hash','context',
        'recovery_hash','dispatch_predecessor','dispatch_successor','dispatch_journal_prefix'}
KIND='EMPTY_HISTORY_NO_ENTRY_SUCCESSOR_V1'


def schema():return f'CREATE TABLE {TABLE}(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL,payload_hash TEXT NOT NULL)'
def guards():
    return {TABLE+'_insert':f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN EXISTS(SELECT 1 FROM {TABLE}) BEGIN SELECT RAISE(ABORT,'Empty history successor immutable'); END"}|{
        TABLE+'_'+a.lower():f"CREATE TRIGGER {TABLE}_{a.lower()} BEFORE {a} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Empty history successor immutable'); END" for a in ('UPDATE','DELETE')}


def shape(v):
    if (type(v) is not dict or set(v)!=FIELDS or type(v['version']) is not int or v['version']!=1
            or v['kind']!=KIND or any(not runtime._hash(v[k]) for k in ('parent_hash','predecessor','successor','config_hash','recovery_hash'))
            or v['predecessor']==v['successor'] or not runtime._context_shape(v['context'])):
        raise ValueError('Exact empty history successor shape required')
    old,new=v['dispatch_predecessor'],v['dispatch_successor']
    if (type(old) is not dict or type(new) is not dict or set(old)!=set(new)
            or old.get('source_hash')!=v['predecessor'] or new.get('source_hash')!=v['successor']
            or any(x.get('config_hash')!=v['config_hash'] for x in (old,new))
            or any(not runtime._hash(x.get(k)) for x in (old,new) for k in ('source_hash','tool_hash','entry_tool_hash'))
            or new!={**old,**{k:new[k] for k in ('source_hash','tool_hash','entry_tool_hash')}}
            or type(old.get('paths')) is not dict
            or any(type(old['paths'].get(k)) is not dict or old['paths'][k].get('path')!=p for k,p in v['context'].items())):
        raise ValueError('Exact same ledger dispatcher rollover required')
    prefix=v['dispatch_journal_prefix']
    if (type(prefix) is not dict or set(prefix)!={'context','intents','results'}
            or any(type(prefix[k]) is not list for k in prefix) or len(prefix['context'])!=1
            or any(len(prefix[k])>4096 for k in ('intents','results'))):
        raise ValueError('Bounded complete dispatcher prefix required')
    return v


def approved(v):
    shape(v)
    with POLICY.open('rb') as f:raw=f.read(runtime.MAX_BYTES+1)
    if len(raw)>runtime.MAX_BYTES:raise ValueError('Empty successor policy bound')
    p=runtime._parse(raw.decode())
    if type(p) is not dict or set(p)!={'version','successors'} or type(p['version']) is not int or p['version']!=1 or p['successors']!=[v]:
        raise ValueError('One independently reviewed empty successor required')
    return v


def read(c):
    found=c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name=? OR substr(name,1,?)=? OR tbl_name=?',(TABLE,len(TABLE)+1,TABLE+'_',TABLE)).fetchall()
    if not found:return None
    expected={('table',TABLE,TABLE,schema())}|{('trigger',k,TABLE,s) for k,s in guards().items()}
    if len(found)!=len(expected) or set(found)!=expected or c.execute('SELECT count(*) FROM '+TABLE).fetchone()!=(1,):
        raise ValueError('Empty successor schema/guards/count invalid')
    scalars=c.execute('SELECT id,typeof(payload),typeof(payload_hash),length(CAST(payload AS BLOB)),length(CAST(payload_hash AS BLOB)) FROM '+TABLE).fetchone()
    if scalars[0]!=1 or scalars[1:3]!=('text','text') or not 0<scalars[3]<=runtime.MAX_BYTES or scalars[4]!=64:
        raise ValueError('Empty successor scalar bound')
    raw,key=c.execute('SELECT payload,payload_hash FROM '+TABLE).fetchone();v=runtime._parse(raw)
    if canonical(v)!=raw or not runtime._hash(key) or digest(v)!=key:raise ValueError('Empty successor bytes changed')
    approved(v);return v,key


def _validate(c,v,original,*,journal=None,initial=False):
    """Parent.require has already validated its complete historical closure."""
    if (v['parent_hash']!=digest(original) or v['predecessor']!=original['successor']
            or v['context']!=original['context'] or v['config_hash']!=original['config_hash']
            or v['dispatch_predecessor']!=original['dispatch_successor']):
        raise ValueError('Empty successor parent/context changed')
    # Reuse exact prefix identity/storage-type validation, never accept old
    # context records inserted after the independently reviewed prefix.
    parent._verify_journal(v,c=journal,initial=initial)
    from . import paper_empty_history_reconciliation as recovery
    path=Path(v['context']['evidence_db'])
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as evidence:
        from . import paper_terminal_reconciliation as terminal
        candidates=[x for x in terminal._rows(evidence) if x.get('association')==recovery.ASSOCIATION]
    if len(candidates)!=1 or digest(candidates[0])!=v['recovery_hash']:
        raise ValueError('Reviewed empty recovery receipt missing')
    recovery.approved(candidates[0])
    r=candidates[0]
    if r['source_hash']!=v['predecessor'] or r['config_hash']!=v['config_hash'] or {k:r['context'][k] for k in v['context']}!=v['context']:
        raise ValueError('Empty recovery source/context changed')
    return v


def effective(c,original,current):
    record=read(c)
    if record is None:return current
    v,_=record;_validate(c,v,original)
    if current not in (original['predecessor'],original['successor'],v['successor']):
        raise ValueError('Empty successor source not reviewed')
    return current


def dispatch_binding(c,expected,journal):
    record=read(c)
    if record is None:return expected,{},{}
    v,_=record;parent.require(c,implementation=v['successor']);original,_=parent.read(c)
    _validate(c,v,original,journal=journal)
    if expected not in (v['dispatch_predecessor'],v['dispatch_successor']):
        raise ValueError('Unreviewed empty successor dispatcher context')
    bindings={row[1]:runtime._parse(row[-2])['context_hash'] for row in v['dispatch_journal_prefix']['intents']}
    old=v['dispatch_predecessor'];return old,bindings,{digest(old):old}


def _validate_predecessor_journal(ledger,journal,expected):
    """Authenticate completions before giving the new prefix any authority."""
    from tools import paper_entry_dispatcher as dispatcher
    from . import paper_migration_no_entry as migration
    from .evidence import EvidenceStore
    values=dispatcher._read_journal(journal)
    if values['context']=={1:expected}:
        return dispatcher._check_records(journal,expected,values,{}, {},review_source=expected['source_hash'])
    if set(values['context'])!={1}:raise ValueError('Original dispatcher context missing')
    mapped,bindings,contexts=parent.dispatch_binding(ledger,expected,journal)
    retired={}
    if values['context']!={1:mapped}:
        store=EvidenceStore(expected['paths']['evidence_db']['path'],read_only=True)
        with closing(store.connect()) as evidence:
            matches=[v for v in migration.rows(evidence) if v['association']==migration.PREWIRE and v['successor_context']==mapped]
        if len(matches)!=1:raise ValueError('Exact original dispatcher lineage required')
        original_bindings,retired,original_contexts=migration._prewire_lineage(
            journal,matches[0],values['context'][1],review_source=expected['source_hash'])
        bindings=original_bindings|bindings;contexts=original_contexts|contexts
    return dispatcher._check_records(journal,expected,values,bindings,retired,
        historical_contexts=contexts,review_source=expected['source_hash'])


def plan(ledger_db,*,dispatch_predecessor,dispatch_successor,recovery_pin):
    """Read-only proposal. Coordinator quiescence and original backups required."""
    from . import paper_empty_history_reconciliation as recovery
    recovery.approved(recovery_pin)
    path=Path(ledger_db)
    if path.resolve(strict=True)!=path or str(path)!=recovery_pin['context']['ledger_db']:raise ValueError('Exact ledger required')
    from tools import paper_entry_dispatcher as dispatcher
    journal=dispatcher._path(dispatch_predecessor['journal'],private=True)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN');original,key=parent.read(c);parent.require(c,implementation=original['successor'])
        with closing(sqlite3.connect(journal.as_uri()+'?mode=ro',uri=True)) as j:
            j.execute('BEGIN')
            from .paper_migration_no_entry import _journal
            _,prefix=_journal(j)
            parent._verify_journal(original,c=j)
            _validate_predecessor_journal(c,j,dispatch_predecessor)
    v={'version':1,'kind':KIND,'parent_hash':key,'predecessor':original['successor'],
       'successor':runtime.implementation_hash(),'config_hash':original['config_hash'],'context':original['context'],
       'recovery_hash':digest(recovery_pin),'dispatch_predecessor':dispatch_predecessor,
       'dispatch_successor':dispatch_successor,'dispatch_journal_prefix':prefix}
    shape(v)
    if dispatch_predecessor!=original['dispatch_successor']:raise ValueError('Exact prior dispatch context required')
    if recovery_pin['source_hash']!=v['predecessor'] or {k:recovery_pin['context'][k] for k in v['context']}!=v['context']:
        raise ValueError('Recovery source/context changed')
    return v


def append(ledger_db,*,pin):
    """One append, after separately reviewed recovery; never activates services."""
    approved(pin)
    if runtime.implementation_hash()!=pin['successor']:raise ValueError('Reviewed successor source required')
    from .paper_cycle import _lock
    from .paper_observe_cli import _worker_lock
    ctx=pin['context'];path=Path(ledger_db)
    if str(path)!=ctx['ledger_db']:raise ValueError('Reviewed ledger mismatch')
    from tools.paper_entry_dispatcher import _path
    journal=_path(pin['dispatch_predecessor']['journal'],private=True)
    with ExitStack() as locks:
        if not locks.enter_context(_lock(str(journal)+'.dispatcher.lock')):raise ValueError('Journal busy')
        if locks.enter_context(_worker_lock(Path(ctx['research_db']))) is None:raise ValueError('Research busy')
        for p in (ctx['evidence_db']+'.ownership-invocation.lock',str(path)+'.paper-cycle.lock'):
            if not locks.enter_context(_lock(p)):raise ValueError('Recovery context busy')
        runtime.require_transition_context(*(ctx[k] for k in ('research_db','evidence_db','ledger_db')),reviewed_context=ctx)
        with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True)) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                original,_=parent.read(c);parent.require(c,implementation=original['successor'])
                previous=read(c)
                if previous is None:
                    with closing(sqlite3.connect(journal.as_uri()+'?mode=ro',uri=True)) as j:
                        j.execute('BEGIN');_validate_predecessor_journal(c,j,pin['dispatch_predecessor'])
                    from . import paper_empty_history_reconciliation as recovery
                    from .evidence import EvidenceStore
                    from .history_progress import HistoryProgress
                    store=EvidenceStore(ctx['evidence_db'],read_only=True)
                    progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
                    with closing(store.connect()) as evidence:
                        from . import paper_terminal_reconciliation as terminal
                        matches=[r for r in terminal._rows(evidence) if digest(r)==pin['recovery_hash']]
                    if len(matches)!=1:raise ValueError('Reviewed original recovery missing')
                    cfg=runtime._parse(parent.extensions._metadata(c)['config'])
                    recovery.proof(store,progress,matches[0],cfg,review_source=pin['predecessor'])
                    if terminal._gate(store,ctx['research_db'],(),ledger_locked=ctx['ledger_db'],review_source=pin['predecessor']) is not None:
                        raise ValueError('Other recovery remains unresolved')
                _validate(c,pin,original,initial=previous is None)
                if previous is not None:
                    if previous!=(pin,digest(pin)):raise ValueError('Conflicting successor receipt')
                    c.rollback();return {'status':'ALREADY_RECORDED','receipt_hash':digest(pin)}
                c.execute(schema())
                for sql in guards().values():c.execute(sql)
                c.execute('INSERT INTO '+TABLE+' VALUES(1,?,?)',(canonical(pin),digest(pin)))
                parent.require(c);c.commit()
            except BaseException:c.rollback();raise
    return {'status':'RECORDED','receipt_hash':digest(pin)}
