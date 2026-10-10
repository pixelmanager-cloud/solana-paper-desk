"""Explicit reviewed updates of an existing two-receipt paper experiment.

At most four append-only edges, each independently pinned and explicitly applied.
Local coordinator authorization, never provider/deployed-program authentication.
"""
from contextlib import closing
from pathlib import Path
import sqlite3

from . import runtime_compatibility as runtime,runtime_continuation as continuation
from .model import canonical,digest

POLICY=Path(__file__).resolve().parents[1]/'config/runtime-extensions.json'
TABLE='paper_runtime_extensions'
MAX_EDGES=4
HASH_FIELDS={'first_receipt_hash','continuation_receipt_hash','parent_receipt_hash',
             'predecessor','successor','config_hash','checkpoint_hash','metadata_hash','events_hash','outcomes_hash'}
PIN_FIELDS=HASH_FIELDS|{'sequence','context','events_count','outcomes_count'}
LOOKUP_FIELDS=('first_receipt_hash','continuation_receipt_hash','sequence','parent_receipt_hash',
               'predecessor','successor','config_hash','context')
FIELDS=PIN_FIELDS|{'version','original_metadata','original_checkpoint'}


def _pin_shape(pin):
    return (type(pin) is dict and set(pin)==PIN_FIELDS
            and all(runtime._hash(pin[k]) for k in HASH_FIELDS)
            and type(pin['sequence']) is int and 1<=pin['sequence']<=MAX_EDGES
            and all(type(pin[k]) is int and 0<=pin[k]<=10000 for k in ('events_count','outcomes_count'))
            and runtime._context_shape(pin['context']) and pin['predecessor']!=pin['successor'])


def _approved(key):
    if (type(key) is not dict or not set(LOOKUP_FIELDS)<=set(key)
            or type(key['sequence']) is not int or not 1<=key['sequence']<=MAX_EDGES
            or not all(runtime._hash(key[k]) for k in LOOKUP_FIELDS if k not in ('sequence','context'))
            or not runtime._context_shape(key['context'])):
        raise ValueError('Invalid explicit extension identity')
    with POLICY.open('rb') as stream:raw=stream.read(65537)
    if len(raw)>65536:raise ValueError('Runtime extension policy bound')
    policy=runtime._parse(raw.decode())
    if (type(policy) is not dict or set(policy)!={'version','extensions'}
            or type(policy['version']) is not int or policy['version']!=1
            or type(policy['extensions']) is not list or len(policy['extensions'])>5):
        raise ValueError('Invalid extension policy')
    seen=set();matches=[]
    for pin in policy['extensions']:
        if not _pin_shape(pin):raise ValueError('Invalid extension pin')
        identity=canonical({k:pin[k] for k in LOOKUP_FIELDS if k!='successor'})
        if identity in seen:raise ValueError('Duplicate or conflicting extension pin')
        seen.add(identity)
        if all(pin[k]==key[k] for k in LOOKUP_FIELDS):matches.append(pin)
    if len(matches)!=1:raise ValueError('Runtime extension not reviewed')
    return matches[0]


def _schema():
    return f'CREATE TABLE {TABLE}(seq INTEGER PRIMARY KEY CHECK(seq BETWEEN 1 AND {MAX_EDGES}),payload TEXT NOT NULL,payload_hash TEXT NOT NULL)'


def _guards():
    result={TABLE+'_insert':f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN NEW.seq!=COALESCE((SELECT MAX(seq) FROM {TABLE}),0)+1 OR NEW.seq>{MAX_EDGES} BEGIN SELECT RAISE(ABORT,'Runtime extension sequence immutable'); END"}
    for action in ('UPDATE','DELETE'):
        result[TABLE+'_'+action.lower()]=f"CREATE TRIGGER {TABLE}_{action.lower()} BEFORE {action} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Runtime extension immutable'); END"
    return result


def _bounded_rows(c):
    """All schema/cardinality/type/byte bounds precede any journal field fetch."""
    names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'paper_runtime_%'")}
    from .runtime_performance_continuation import namespace
    if names!=namespace(c,{runtime.TABLE,continuation.TABLE,TABLE}):raise ValueError('Partial runtime extension namespace')
    if c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone()!=(_schema(),):
        raise ValueError('Malformed extension table')
    columns=c.execute(f'PRAGMA table_info({TABLE})').fetchall()
    if [(r[1],r[2],r[5]) for r in columns]!=[('seq','INTEGER',1),('payload','TEXT',0),('payload_hash','TEXT',0)]:
        raise ValueError('Malformed extension columns')
    if dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(TABLE,)))!=_guards():
        raise ValueError('Malformed extension guards')
    count=c.execute(f'SELECT COUNT(*) FROM {TABLE}').fetchone()[0]
    if not 1<=count<=MAX_EDGES:raise ValueError('Partial or oversized extension journal')
    shapes=c.execute(f'SELECT seq,typeof(payload),typeof(payload_hash),length(CAST(payload AS BLOB)),length(CAST(payload_hash AS BLOB)) FROM {TABLE} ORDER BY seq').fetchall()
    if ([r[0] for r in shapes]!=list(range(1,count+1))
            or any(r[1:3]!=('text','text') or not 0<r[3]<=runtime.MAX_BYTES or r[4]!=64 for r in shapes)):
        raise ValueError('Extension sequence/type/byte bound')
    return c.execute(f'SELECT seq,payload,payload_hash FROM {TABLE} ORDER BY seq').fetchall()


def _metadata(c):
    size=c.execute('SELECT COUNT(*),COALESCE(SUM(length(CAST(key AS BLOB))+length(CAST(value AS BLOB))),0) FROM metadata').fetchone()
    if not 3<=size[0]<=64 or size[1]>runtime.MAX_BYTES or c.execute("SELECT 1 FROM metadata WHERE typeof(key)!='text' OR typeof(value)!='text' LIMIT 1").fetchone():
        raise ValueError('Extension metadata type/byte bound')
    return dict(c.execute('SELECT key,value FROM metadata'))


def _base(c,*,extended):
    _metadata(c)
    # The original continuation is scalar-preflighted before fetching either
    # contents field. Original receipt grammar/guards/policy remain authoritative.
    if c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(continuation.TABLE,)).fetchone()!=(continuation._schema(),):
        raise ValueError('Original continuation schema missing')
    columns=c.execute(f'PRAGMA table_info({continuation.TABLE})').fetchall()
    if [(r[1],r[2],r[5]) for r in columns]!=[('id','INTEGER',1),('payload','TEXT',0),('payload_hash','TEXT',0)]:
        raise ValueError('Original continuation columns invalid')
    if dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(continuation.TABLE,)))!=continuation._guards():
        raise ValueError('Original continuation guards invalid')
    if c.execute(f'SELECT COUNT(*) FROM {continuation.TABLE}').fetchone()[0]!=1:raise ValueError('Original continuation count invalid')
    shape=c.execute(f'SELECT id,typeof(payload),typeof(payload_hash),length(CAST(payload AS BLOB)),length(CAST(payload_hash AS BLOB)) FROM {continuation.TABLE}').fetchone()
    if shape is None or shape[0]!=1 or shape[1:3]!=('text','text') or not 0<shape[3]<=runtime.MAX_BYTES or shape[4]!=64:
        raise ValueError('Original continuation type/byte bound')
    first,first_hash=continuation._first(c,_extensions=extended)
    row=c.execute(f'SELECT payload,payload_hash FROM {continuation.TABLE} WHERE id=1').fetchone()
    base=runtime._parse(row[0])
    if type(base) is not dict or set(base)!=continuation.FIELDS or digest(base)!=row[1]:
        raise ValueError('Original continuation binding invalid')
    continuation.require_continuation(c,implementation=base['successor'],_extensions=extended)
    return first,first_hash,base,row[1]


def _snapshot(c,cfg):
    from .paper_checkpoint import validate_checkpoint
    metadata=_metadata(c)
    if metadata.get('config_hash')!=digest(cfg) or metadata.get('config')!=canonical(cfg):
        raise ValueError('Extension original config mismatch')
    if c.execute('SELECT COUNT(*) FROM state').fetchone()[0]!=1:raise ValueError('Extension checkpoint count invalid')
    shape=c.execute('SELECT id,typeof(payload),length(CAST(payload AS BLOB)) FROM state').fetchone()
    if shape is None or shape[0]!=1 or shape[1]!='text' or not 0<shape[2]<=runtime.MAX_BYTES:
        raise ValueError('Extension checkpoint type/byte bound')
    row=c.execute('SELECT payload FROM state WHERE id=1').fetchone()
    state=validate_checkpoint(c,row[0]);runtime._history(c,cfg)
    value={'original_metadata':metadata,'original_checkpoint':state,
           'metadata_hash':digest(metadata),'checkpoint_hash':digest(state)}
    for table in ('events','outcomes'):
        count=c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        value[table+'_count']=count;value[table+'_hash']=runtime._prefix(c,table,count)
    return value


def _validate(c,rows,first,first_hash,base,base_hash,current):
    if not runtime._hash(current):raise ValueError('Extension runtime identity invalid')
    used={first['predecessor'],first['successor'],base['successor']}
    parent=base_hash;predecessor=base['successor'];previous=base
    metadata=_metadata(c)
    for sequence,payload,key in rows:
        value=runtime._parse(payload)
        if (type(value) is not dict or set(value)!=FIELDS or type(value['version']) is not int
                or value['version']!=1 or not runtime._hash(key) or digest(value)!=key
                or not _pin_shape({k:value[k] for k in PIN_FIELDS})
                or value['sequence']!=sequence or value['first_receipt_hash']!=first_hash
                or value['continuation_receipt_hash']!=base_hash or value['parent_receipt_hash']!=parent
                or value['predecessor']!=predecessor or value['successor'] in used
                or value['config_hash']!=base['config_hash'] or value['context']!=base['context']):
            raise ValueError('Extension linkage/source/config binding invalid')
        pin={k:value[k] for k in PIN_FIELDS}
        if _approved(pin)!=pin:raise ValueError('Extension anchor pin mismatch')
        if (value['original_metadata']!=metadata or digest(metadata)!=value['metadata_hash']
                or type(value['original_checkpoint']) is not dict or digest(value['original_checkpoint'])!=value['checkpoint_hash']):
            raise ValueError('Extension original snapshot changed')
        for table in ('events','outcomes'):
            if (value[table+'_count']<previous[table+'_count']
                    or runtime._prefix(c,table,value[table+'_count'])!=value[table+'_hash']):
                raise ValueError('Extension prefix anchor changed')
        runtime._baseline(c,value)
        parent=key;predecessor=value['successor'];used.add(predecessor);previous=value
    if predecessor!=current:raise ValueError('Runtime extension tail is not running source')
    return previous,parent


def require_extensions(c,*,implementation=None):
    from .runtime_performance_continuation import read,require
    if read(c) is not None:return require(c,implementation=implementation)
    rows=_bounded_rows(c)
    first,first_hash,base,base_hash=_base(c,extended=True)
    current=runtime.implementation_hash() if implementation is None else implementation
    _validate(c,rows,first,first_hash,base,base_hash,current)
    return current


def append_runtime(research_db,evidence_db,ledger_db,cfg,*,sequence,first_receipt_hash,
                   continuation_receipt_hash,parent_receipt_hash,predecessor,successor):
    """One explicitly pinned append; original receipt/grant/state bytes unchanged."""
    from .job_persistence import canonical_job_path
    from .history_progress import canonical_ownership_path
    from .paper_observe_cli import _worker_lock
    from .paper_cycle import _lock
    research=canonical_job_path(research_db);evidence=canonical_ownership_path(evidence_db);ledger=canonical_job_path(ledger_db)
    if type(cfg) is not dict:raise ValueError('Extension config object required')
    context={'research_db':str(research),'evidence_db':str(evidence),'ledger_db':str(ledger)}
    lookup={'sequence':sequence,'first_receipt_hash':first_receipt_hash,'continuation_receipt_hash':continuation_receipt_hash,
            'parent_receipt_hash':parent_receipt_hash,'predecessor':predecessor,'successor':successor,
            'config_hash':digest(cfg),'context':context}
    pin=_approved(lookup)
    if (successor!=runtime.implementation_hash() or not runtime._context_shape(context)
            or not all(p.is_file() for p in (research,evidence,ledger))):
        raise ValueError('Exact existing extension context/source required')
    with _worker_lock(research) as worker:
        if worker is None:raise ValueError('Research worker busy')
        with _lock(str(evidence)+'.ownership-invocation.lock') as locked:
            if not locked:raise ValueError('Evidence invocation busy')
            with _lock(str(ledger)+'.paper-cycle.lock') as locked:
                if not locked:raise ValueError('Ledger cycle busy')
                runtime.require_transition_context(research,evidence,ledger,reviewed_context=pin['context'])
                with closing(sqlite3.connect(ledger.as_uri()+'?mode=rw',uri=True,isolation_level=None)) as c:
                    c.execute('BEGIN IMMEDIATE')
                    try:
                        names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'paper_runtime_%'")}
                        extended=TABLE in names
                        rows=_bounded_rows(c) if extended else []
                        first,first_hash,base,base_hash=_base(c,extended=extended)
                        if (first_hash!=first_receipt_hash or base_hash!=continuation_receipt_hash
                                or base['config_hash']!=digest(cfg) or base['context']!=context):
                            raise ValueError('Exact original receipt pair required')
                        snapshot=_snapshot(c,cfg)
                        if extended and sequence==len(rows):
                            value,_=_validate(c,rows,first,first_hash,base,base_hash,successor)
                            if {k:value[k] for k in PIN_FIELDS}!=pin:raise ValueError('Conflicting extension replay')
                            c.commit();return {'status':'ALREADY_RECORDED','effective_runtime_hash':successor}
                        if sequence!=len(rows)+1 or sequence>MAX_EDGES:raise ValueError('Extension append sequence limit')
                        if rows:
                            previous,parent=_validate(c,rows,first,first_hash,base,base_hash,predecessor)
                        else:previous,parent=base,base_hash
                        if predecessor!=previous['successor'] or parent_receipt_hash!=parent:
                            raise ValueError('Extension predecessor/parent mismatch')
                        if any(pin[k]!=snapshot[k] for k in snapshot if k in PIN_FIELDS):
                            raise ValueError('Reviewed extension checkpoint/journal changed')
                        receipt=pin|snapshot|{'version':1}
                        payload=canonical(receipt)
                        if len(payload.encode())>runtime.MAX_BYTES:raise ValueError('Extension publication byte bound')
                        if not extended:
                            c.execute(_schema())
                            for sql in _guards().values():c.execute(sql)
                        c.execute(f'INSERT INTO {TABLE} VALUES(?,?,?)',(sequence,payload,digest(receipt)))
                        require_extensions(c);c.commit()
                        return {'status':'RECORDED','effective_runtime_hash':successor}
                    except BaseException:
                        c.rollback();raise


def main(argv=None):
    import argparse,json
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('research-db','evidence-db','ledger-db','config','first-receipt-hash',
                 'continuation-receipt-hash','parent-receipt-hash','predecessor','successor'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--sequence',required=True,type=int)
    args=parser.parse_args(argv)
    with Path(args.config).open('rb') as stream:raw=stream.read(runtime.MAX_BYTES+1)
    cfg=runtime._parse(raw.decode())
    print(json.dumps(append_runtime(args.research_db,args.evidence_db,args.ledger_db,cfg,sequence=args.sequence,
        first_receipt_hash=args.first_receipt_hash,continuation_receipt_hash=args.continuation_receipt_hash,
        parent_receipt_hash=args.parent_receipt_hash,predecessor=args.predecessor,successor=args.successor)))


if __name__=='__main__':main()
