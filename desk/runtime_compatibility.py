"""Explicit reviewed local runtime transitions; never replace original identity.

The repository allowlist is outside desk/ (the implementation hash domain).
A receipt is local coordinator authorization, not authenticated provider evidence.
"""
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .model import canonical, digest

POLICY = Path(__file__).resolve().parents[1]/'config/runtime-compatibility.json'
TABLE = 'paper_runtime_transition'
MAX_BYTES = 2*1024*1024


def implementation_hash():
    root=Path(__file__).parent
    return digest({str(p.relative_to(root)):p.read_text() for p in sorted(root.rglob('*'))
                   if p.is_file() and p.suffix in ('.py','.json')})


def _hash(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Duplicate runtime contract field')
        result[key]=value
    return result


def _parse(raw):
    if type(raw) is not str or len(raw.encode())>MAX_BYTES:raise ValueError('Runtime contract bound')
    return json.loads(raw,object_pairs_hook=_unique,
        parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite runtime contract')))


def _approved(predecessor,successor,config_hash):
    with POLICY.open('rb') as stream:raw=stream.read(65537)
    if len(raw)>65536:raise ValueError('Runtime allowlist bound')
    policy=_parse(raw.decode())
    if (type(policy) is not dict or set(policy)!={'version','transitions'}
            or type(policy['version']) is not int or policy['version']!=1
            or type(policy['transitions']) is not list or len(policy['transitions'])>8):
        raise ValueError('Invalid runtime allowlist')
    entries=policy['transitions'];seen=set()
    for entry in entries:
        if (type(entry) is not dict or set(entry)!={'predecessor','successor','config_hash'}
                or not all(_hash(v) for v in entry.values()) or entry['predecessor']==entry['successor']):
            raise ValueError('Invalid runtime edge')
        key=canonical(entry)
        if key in seen:raise ValueError('Duplicate runtime edge')
        seen.add(key)
    if {'predecessor':predecessor,'successor':successor,'config_hash':config_hash} not in entries:
        raise ValueError('Runtime transition is not reviewed')


def _schema():
    return f'CREATE TABLE {TABLE}(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL,payload_hash TEXT NOT NULL)'


def _guards():
    result={TABLE+'_insert':f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT,'Runtime transition immutable'); END"}
    for action in ('UPDATE','DELETE'):
        result[TABLE+'_'+action.lower()]=f"CREATE TRIGGER {TABLE}_{action.lower()} BEFORE {action} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Runtime transition immutable'); END"
    return result


def _prefix(c,table,limit):
    if type(limit) is not int or not 0<=limit<=10000:raise ValueError('Runtime journal bound')
    columns='seq,event_id,ts,payload,payload_hash' if table=='events' else 'seq,event_id,payload'
    size=c.execute(f'SELECT COUNT(*),COALESCE(SUM(length(CAST(payload AS BLOB))),0),COALESCE(MAX(length(CAST(payload AS BLOB))),0) FROM {table} WHERE seq<=?',(limit,)).fetchone()
    if size[0]!=limit or size[1]>16*1024*1024 or size[2]>MAX_BYTES:raise ValueError('Runtime journal incomplete or oversized')
    rows=list(c.execute(f'SELECT {columns} FROM {table} WHERE seq<=? ORDER BY seq',(limit,)))
    if [r[0] for r in rows]!=list(range(1,limit+1)):raise ValueError('Runtime journal sequence invalid')
    return digest(rows)


def _history(c,cfg):
    from .paper_view import _event_json, _history_preflight
    _history_preflight(c)
    for table in ('events','outcomes'):
        count=c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        _prefix(c,table,count)
    for identity,at,payload,key in c.execute('SELECT event_id,ts,payload,payload_hash FROM events'):
        event=_event_json(payload,cfg)
        if event.get('event_id')!=identity or event.get('ts')!=at or digest(event)!=key:
            raise ValueError('Runtime original event binding invalid')
    for payload in c.execute('SELECT payload FROM outcomes'):
        if type(_parse(payload[0])) is not dict:raise ValueError('Runtime outcome malformed')
    if c.execute('SELECT 1 FROM outcomes o LEFT JOIN events e ON o.event_id=e.event_id WHERE e.event_id IS NULL LIMIT 1').fetchone():
        raise ValueError('Runtime orphan outcome')


def require_runtime(c,*,implementation=None):
    """Shared writer/reader hook; accepts one exact approved edge, no chains."""
    current=implementation_hash() if implementation is None else implementation
    metadata=dict(c.execute("SELECT key,value FROM metadata WHERE key IN ('implementation_hash','config_hash','config')"))
    original=metadata.get('implementation_hash');cfg_hash=metadata.get('config_hash')
    if not _hash(original) or not _hash(current) or not _hash(cfg_hash):raise ValueError('Invalid runtime identity')
    cfg=_parse(metadata.get('config'))
    if type(cfg) is not dict or digest(cfg)!=cfg_hash:raise ValueError('Runtime config binding invalid')
    names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'paper_runtime_%'")}
    if not names:
        if original!=current:raise ValueError('implementation changed: explicit reviewed transition required')
        return current
    if names!={TABLE}:raise ValueError('Partial runtime transition schema')
    if c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone() != (_schema(),):
        raise ValueError('Malformed runtime table semantics')
    columns=c.execute(f'PRAGMA table_info({TABLE})').fetchall()
    if [(r[1],r[2],r[5]) for r in columns]!=[('id','INTEGER',1),('payload','TEXT',0),('payload_hash','TEXT',0)]:
        raise ValueError('Malformed runtime transition schema')
    triggers=dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(TABLE,)))
    if triggers!=_guards():raise ValueError('Partial or malformed runtime guards')
    if c.execute(f'SELECT COUNT(*) FROM {TABLE}').fetchone()[0]!=1:raise ValueError('Partial runtime transition')
    row=c.execute(f'SELECT id,payload,payload_hash,length(CAST(payload AS BLOB)) FROM {TABLE}').fetchone()
    if row[0]!=1 or not 0<row[3]<=MAX_BYTES:raise ValueError('Runtime receipt bound')
    receipt=_parse(row[1])
    fields={'version','predecessor','successor','config_hash','original_checkpoint','original_metadata','events_count','events_hash','outcomes_count','outcomes_hash'}
    if (type(receipt) is not dict or set(receipt)!=fields or type(receipt['version']) is not int
            or receipt['version']!=1 or digest(receipt)!=row[2]
            or receipt['predecessor']!=original or receipt['successor']!=current or receipt['config_hash']!=cfg_hash):
        raise ValueError('Runtime receipt binding invalid')
    _approved(original,current,cfg_hash)
    baseline=receipt['original_metadata']
    if (type(baseline) is not dict or any(baseline.get(k)!=metadata.get(k) for k in metadata)
            or type(receipt['original_checkpoint']) is not dict):raise ValueError('Runtime original snapshot invalid')
    for table in ('events','outcomes'):
        if _prefix(c,table,receipt[table+'_count'])!=receipt[table+'_hash']:
            raise ValueError('Runtime original journal changed')
    _history(c,cfg)
    _baseline(c,receipt)
    return current


def _baseline(c,receipt):
    """Validate preserved checkpoint against its bounded ORIGINAL prefix only."""
    from .paper_checkpoint import validate_checkpoint
    metadata=receipt['original_metadata']
    if len(metadata)>64 or any(type(k) is not str or type(v) is not str for k,v in metadata.items()):
        raise ValueError('Malformed original metadata snapshot')
    with closing(sqlite3.connect(':memory:')) as baseline:
        baseline.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        baseline.execute('CREATE TABLE events(seq INTEGER PRIMARY KEY,event_id TEXT,ts INTEGER,payload TEXT,payload_hash TEXT)')
        baseline.execute('CREATE TABLE outcomes(seq INTEGER PRIMARY KEY,event_id TEXT,payload TEXT)')
        baseline.executemany('INSERT INTO metadata VALUES(?,?)',metadata.items())
        for table,columns in (('events','seq,event_id,ts,payload,payload_hash'),('outcomes','seq,event_id,payload')):
            rows=c.execute(f'SELECT {columns} FROM {table} WHERE seq<=? ORDER BY seq',(receipt[table+'_count'],))
            marks=','.join('?' for _ in columns.split(','))
            baseline.executemany(f'INSERT INTO {table} VALUES({marks})',rows)
        validate_checkpoint(baseline,canonical(receipt['original_checkpoint']))


def transition(research_db,evidence_db,ledger_db,cfg,*,predecessor,successor):
    """Explicit coordinator-only transaction under existing canonical lock order.

    No credentials, allowance provisioning, admission, config or metadata writes.
    The final source/config edge must already be in the reviewed allowlist.
    """
    from .job_persistence import canonical_job_path
    from .history_progress import canonical_ownership_path
    from .paper_observe_cli import _worker_lock
    from .paper_cycle import _lock
    from .paper_checkpoint import validate_checkpoint
    research=canonical_job_path(research_db);evidence=canonical_ownership_path(evidence_db);ledger=canonical_job_path(ledger_db)
    if len({research,evidence,ledger})!=3 or not all(p.is_file() for p in (research,evidence,ledger)):
        raise ValueError('Three stable distinct existing databases required')
    if successor!=implementation_hash():raise ValueError('Successor source mismatch')
    _approved(predecessor,successor,digest(cfg))
    with _worker_lock(research) as worker:
        if worker is None:raise ValueError('Research worker busy')
        with _lock(str(evidence)+'.ownership-invocation.lock') as locked:
            if not locked:raise ValueError('Evidence invocation busy')
            with _lock(str(ledger)+'.paper-cycle.lock') as locked:
                if not locked:raise ValueError('Ledger cycle busy')
                with closing(sqlite3.connect(ledger.as_uri()+'?mode=rw',uri=True,isolation_level=None)) as c:
                    c.execute('BEGIN IMMEDIATE')
                    try:
                        metadata=dict(c.execute('SELECT key,value FROM metadata'))
                        if (metadata.get('implementation_hash')!=predecessor or metadata.get('config_hash')!=digest(cfg)
                                or metadata.get('config')!=canonical(cfg)):
                            raise ValueError('Original runtime/config mismatch')
                        row=c.execute('SELECT payload FROM state WHERE id=1').fetchone()
                        if row is None:raise ValueError('Original checkpoint missing')
                        state=validate_checkpoint(c,row[0]);_history(c,cfg)
                        names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'paper_runtime_%'")}
                        if names:
                            require_runtime(c);c.commit();return {'status':'ALREADY_RECORDED','implementation_hash':predecessor,'effective_runtime_hash':successor}
                        receipt={'version':1,'predecessor':predecessor,'successor':successor,'config_hash':digest(cfg),
                                 'original_checkpoint':state,'original_metadata':metadata}
                        for table in ('events','outcomes'):
                            count=c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                            receipt[table+'_count']=count;receipt[table+'_hash']=_prefix(c,table,count)
                        c.execute(_schema())
                        for sql in _guards().values():c.execute(sql)
                        c.execute(f'INSERT INTO {TABLE} VALUES(1,?,?)',(canonical(receipt),digest(receipt)))
                        require_runtime(c);c.commit()
                        return {'status':'RECORDED','implementation_hash':predecessor,'effective_runtime_hash':successor}
                    except BaseException:
                        c.rollback();raise


def main(argv=None):
    import argparse
    from .paper_cycle_cli import _config
    parser=argparse.ArgumentParser(description='Explicit reviewed runtime identity transition; no budget change')
    for name in ('research-db','evidence-db','ledger-db','config','predecessor','successor'):
        parser.add_argument('--'+name,required=True)
    args=parser.parse_args(argv)
    try:
        result=transition(args.research_db,args.evidence_db,args.ledger_db,_config(args.config),
                          predecessor=args.predecessor,successor=args.successor)
        print(json.dumps(result,sort_keys=True));return 0
    except (ValueError,OSError,sqlite3.Error,TypeError,KeyError,RecursionError):
        print(json.dumps({'status':'UNAVAILABLE','blockers':['EXPLICIT_REVIEWED_RUNTIME_TRANSITION_REQUIRED']}));return 2


if __name__=='__main__':raise SystemExit(main())
