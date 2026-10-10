"""One explicit local continuation of an immutable first runtime receipt.

Coordinator authorization only: no provider, execution or ownership attestation.
A second continuation, changed configuration or implicit adoption is unsupported.
"""
from contextlib import closing
from pathlib import Path
import sqlite3

from . import runtime_compatibility as runtime
from .model import canonical,digest

POLICY=Path(__file__).resolve().parents[1]/'config/runtime-continuation.json'
TABLE='paper_runtime_continuation'
FIELDS={'version','first_receipt_hash','predecessor','successor','config_hash',
        'context','original_metadata','original_checkpoint','events_count',
        'events_hash','outcomes_count','outcomes_hash'}


def _approved(first_hash,predecessor,successor,config_hash):
    with POLICY.open('rb') as stream:raw=stream.read(65537)
    if len(raw)>65536:raise ValueError('Continuation policy bound')
    policy=runtime._parse(raw.decode())
    if (type(policy) is not dict or set(policy)!={'version','continuations'}
            or type(policy['version']) is not int or policy['version']!=1
            or type(policy['continuations']) is not list or len(policy['continuations'])>8):
        raise ValueError('Invalid continuation policy')
    seen=set();matches=[]
    for entry in policy['continuations']:
        if (type(entry) is not dict or set(entry)!={'first_receipt_hash','predecessor','successor','config_hash','context'}
                or not all(runtime._hash(entry[k]) for k in ('first_receipt_hash','predecessor','successor','config_hash'))
                or entry['predecessor']==entry['successor'] or not runtime._context_shape(entry['context'])):
            raise ValueError('Invalid continuation edge')
        key=canonical(entry)
        if key in seen:raise ValueError('Duplicate continuation edge')
        seen.add(key)
        if tuple(entry[k] for k in ('first_receipt_hash','predecessor','successor','config_hash'))==(first_hash,predecessor,successor,config_hash):
            matches.append(entry)
    if len(matches)!=1:raise ValueError('Continuation is not reviewed or ambiguous')
    return matches[0]['context']


def _schema():
    return f'CREATE TABLE {TABLE}(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL,payload_hash TEXT NOT NULL)'


def _guards():
    result={TABLE+'_insert':f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT,'Runtime continuation immutable'); END"}
    for action in ('UPDATE','DELETE'):
        result[TABLE+'_'+action.lower()]=f"CREATE TRIGGER {TABLE}_{action.lower()} BEFORE {action} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Runtime continuation immutable'); END"
    return result


def _first(c,*,_extensions=False):
    """Validate SQL contract and bounds BEFORE loading any first-receipt bytes."""
    names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'paper_runtime_%'")}
    from .runtime_performance_continuation import namespace
    allowed=(namespace(c,{runtime.TABLE,TABLE,'paper_runtime_extensions'}),) if _extensions else ({runtime.TABLE},{runtime.TABLE,TABLE})
    if names not in allowed:
        raise ValueError('Partial runtime transition schema')
    if c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(runtime.TABLE,)).fetchone()!=(runtime._schema(),):
        raise ValueError('Malformed first runtime table')
    columns=c.execute(f'PRAGMA table_info({runtime.TABLE})').fetchall()
    if [(r[1],r[2],r[5]) for r in columns]!=[('id','INTEGER',1),('payload','TEXT',0),('payload_hash','TEXT',0)]:
        raise ValueError('Malformed first runtime columns')
    if dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(runtime.TABLE,)))!=runtime._guards():
        raise ValueError('Malformed first runtime guards')
    if c.execute(f'SELECT COUNT(*) FROM {runtime.TABLE}').fetchone()[0]!=1:
        raise ValueError('Partial first runtime receipt')
    # These expressions return scalars, not untrusted payload/hash contents.
    shape=c.execute(f'SELECT id,typeof(payload),typeof(payload_hash),length(CAST(payload AS BLOB)),length(CAST(payload_hash AS BLOB)) FROM {runtime.TABLE}').fetchone()
    if shape is None or shape[0]!=1 or shape[1:3]!=('text','text') or not 0<shape[3]<=runtime.MAX_BYTES or shape[4]!=64:
        raise ValueError('First runtime receipt type or byte bound')
    row=c.execute(f'SELECT payload,payload_hash FROM {runtime.TABLE} WHERE id=1').fetchone()
    receipt=runtime._parse(row[0])
    if type(receipt) is not dict or not runtime._hash(row[1]) or digest(receipt)!=row[1]:
        raise ValueError('First receipt hash invalid')
    return receipt,row[1]


def require_continuation(c,*,implementation=None,_extensions=False):
    current=runtime.implementation_hash() if implementation is None else implementation
    if not runtime._hash(current):raise ValueError('Invalid continuation runtime identity')
    if c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone()!=(_schema(),):
        raise ValueError('Malformed continuation table')
    columns=c.execute(f'PRAGMA table_info({TABLE})').fetchall()
    if [(r[1],r[2],r[5]) for r in columns]!=[('id','INTEGER',1),('payload','TEXT',0),('payload_hash','TEXT',0)]:
        raise ValueError('Malformed continuation columns')
    if dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(TABLE,)))!=_guards():
        raise ValueError('Malformed continuation guards')
    if c.execute(f'SELECT COUNT(*) FROM {TABLE}').fetchone()[0]!=1:raise ValueError('Partial continuation')
    shape=c.execute(f'SELECT id,typeof(payload),typeof(payload_hash),length(CAST(payload AS BLOB)),length(CAST(payload_hash AS BLOB)) FROM {TABLE}').fetchone()
    if shape is None or shape[0]!=1 or shape[1:3]!=('text','text') or not 0<shape[3]<=runtime.MAX_BYTES or shape[4]!=64:
        raise ValueError('Continuation receipt type or byte bound')
    row=c.execute(f'SELECT payload,payload_hash FROM {TABLE} WHERE id=1').fetchone()
    receipt=runtime._parse(row[0]);first,first_hash=_first(c,_extensions=_extensions)
    if (type(receipt) is not dict or set(receipt)!=FIELDS or type(receipt['version']) is not int
            or receipt['version']!=1 or not runtime._hash(row[1]) or digest(receipt)!=row[1]
            or receipt['first_receipt_hash']!=first_hash or receipt['predecessor']!=first.get('successor')
            or receipt['successor']!=current or receipt['successor']==first.get('predecessor')
            or receipt['config_hash']!=first.get('config_hash')):
        raise ValueError('Continuation receipt binding invalid')
    runtime._require_first(c,implementation=receipt['predecessor'],continuation=True,_extensions=_extensions)
    if (receipt['context']!=first['context'] or receipt['context']!=_approved(first_hash,receipt['predecessor'],current,receipt['config_hash'])):
        raise ValueError('Continuation context binding invalid')
    metadata=dict(c.execute('SELECT key,value FROM metadata'))
    if receipt['original_metadata']!=metadata or type(receipt['original_checkpoint']) is not dict:
        raise ValueError('Continuation original snapshot invalid')
    for table in ('events','outcomes'):
        if (type(receipt[table+'_count']) is not int or receipt[table+'_count']<first[table+'_count']
                or runtime._prefix(c,table,receipt[table+'_count'])!=receipt[table+'_hash']):
            raise ValueError('Continuation journal anchor invalid')
    runtime._baseline(c,receipt)
    return current


def continue_runtime(research_db,evidence_db,ledger_db,cfg,*,first_receipt_hash,predecessor,successor):
    """Explicit atomic append under research -> evidence -> ledger locks."""
    from .job_persistence import canonical_job_path
    from .history_progress import canonical_ownership_path
    from .paper_observe_cli import _worker_lock
    from .paper_cycle import _lock
    from .paper_checkpoint import validate_checkpoint
    research=canonical_job_path(research_db);evidence=canonical_ownership_path(evidence_db);ledger=canonical_job_path(ledger_db)
    context=_approved(first_receipt_hash,predecessor,successor,digest(cfg))
    actual={'research_db':str(research),'evidence_db':str(evidence),'ledger_db':str(ledger)}
    if (successor!=runtime.implementation_hash() or actual!=context or len({research,evidence,ledger})!=3
            or not all(p.is_file() for p in (research,evidence,ledger))):
        raise ValueError('Exact existing continuation context/source required')
    with _worker_lock(research) as worker:
        if worker is None:raise ValueError('Research worker busy')
        with _lock(str(evidence)+'.ownership-invocation.lock') as locked:
            if not locked:raise ValueError('Evidence invocation busy')
            with _lock(str(ledger)+'.paper-cycle.lock') as locked:
                if not locked:raise ValueError('Ledger cycle busy')
                runtime.require_transition_context(research,evidence,ledger,reviewed_context=context)
                with closing(sqlite3.connect(ledger.as_uri()+'?mode=rw',uri=True,isolation_level=None)) as c:
                    c.execute('BEGIN IMMEDIATE')
                    try:
                        first,key=_first(c)
                        if (key!=first_receipt_hash or first.get('successor')!=predecessor
                                or first.get('config_hash')!=digest(cfg) or first.get('context')!=context):
                            raise ValueError('Exact first runtime receipt required')
                        names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'paper_runtime_%'")}
                        if TABLE in names:
                            runtime.require_runtime(c)
                            row=c.execute('SELECT payload FROM state WHERE id=1').fetchone()
                            if row is None:raise ValueError('Continuation checkpoint missing')
                            validate_checkpoint(c,row[0]);c.commit()
                            return {'status':'ALREADY_RECORDED','effective_runtime_hash':successor}
                        runtime._require_first(c,implementation=predecessor)
                        metadata=dict(c.execute('SELECT key,value FROM metadata'))
                        if metadata.get('config')!=canonical(cfg):raise ValueError('Continuation config mismatch')
                        row=c.execute('SELECT payload FROM state WHERE id=1').fetchone()
                        if row is None:raise ValueError('Continuation checkpoint missing')
                        state=validate_checkpoint(c,row[0])
                        receipt={'version':1,'first_receipt_hash':key,'predecessor':predecessor,'successor':successor,
                                 'config_hash':digest(cfg),'context':context,'original_metadata':metadata,'original_checkpoint':state}
                        for table in ('events','outcomes'):
                            count=c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                            receipt[table+'_count']=count;receipt[table+'_hash']=runtime._prefix(c,table,count)
                        c.execute(_schema())
                        for sql in _guards().values():c.execute(sql)
                        c.execute(f'INSERT INTO {TABLE} VALUES(1,?,?)',(canonical(receipt),digest(receipt)))
                        runtime.require_runtime(c);c.commit()
                        return {'status':'RECORDED','effective_runtime_hash':successor}
                    except BaseException:
                        c.rollback();raise


def main(argv=None):
    import argparse,json
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('research-db','evidence-db','ledger-db','config','first-receipt-hash','predecessor','successor'):
        parser.add_argument('--'+name,required=True)
    args=parser.parse_args(argv)
    cfg=runtime._parse(Path(args.config).read_text())
    print(json.dumps(continue_runtime(args.research_db,args.evidence_db,args.ledger_db,cfg,
        first_receipt_hash=args.first_receipt_hash,predecessor=args.predecessor,successor=args.successor),sort_keys=True))


if __name__=='__main__':main()
