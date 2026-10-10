"""Explicit reviewed additive pacing transition; never reset/provision on read."""
from contextlib import closing
from pathlib import Path
from .model import canonical,digest
from . import runtime_compatibility as runtime

POLICY=Path(__file__).resolve().parents[1]/'config/kraken-pacing-migration.json'
TABLE='kraken_pacing_migration'
SQL=f'CREATE TABLE {TABLE}(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL,payload_hash TEXT NOT NULL)'
GUARDS={TABLE+'_'+a.lower():f"CREATE TRIGGER {TABLE}_{a.lower()} BEFORE {a} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Pacing migration immutable'); END" for a in ('UPDATE','DELETE')}
GUARDS[TABLE+'_insert']=f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN EXISTS(SELECT 1 FROM {TABLE}) BEGIN SELECT RAISE(ABORT,'Pacing migration immutable'); END"


def _approved(body):
    with POLICY.open('rb') as f:raw=f.read(65537)
    if len(raw)>65536:raise ValueError('Pacing migration pin bound')
    p=runtime._parse(raw.decode())
    if type(p) is not dict or set(p)!={'version','pins'} or type(p['version']) is not int or p['version']!=1 or type(p['pins']) is not list or len(p['pins'])>16:raise ValueError('Pacing migration pin shape')
    if any(type(x) is not dict or set(x)!=set(body) for x in p['pins']) or len({x['path'] for x in p['pins']})!=len(p['pins']):raise ValueError('Pacing migration duplicate/malformed pin')
    if body not in p['pins']:raise ValueError('Pacing migration not reviewed')


def read(c,path):
    objects=list(c.execute("SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'kraken_pacing_%'"))
    if not objects:return None
    expected={('table',TABLE):SQL}|{('trigger',k):v for k,v in GUARDS.items()}
    if {(a,b):s for a,b,s in objects}!=expected or dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(TABLE,)))!=GUARDS:raise ValueError('Pacing migration schema/guards')
    shapes=c.execute(f'SELECT id,typeof(payload),length(CAST(payload AS BLOB)),typeof(payload_hash),length(CAST(payload_hash AS BLOB)) FROM {TABLE} LIMIT 2').fetchall()
    if len(shapes)!=1 or shapes[0][0]!=1 or shapes[0][1]!='text' or not 0<shapes[0][2]<=65536 or shapes[0][3:]!=('text',64):raise ValueError('Pacing migration scalar/partial bound')
    raw,key=c.execute(f'SELECT payload,payload_hash FROM {TABLE} WHERE id=1').fetchone();body=runtime._parse(raw)
    if (type(body) is not dict or set(body)!={'version','path','source_hash','old_policy','old_state','kraken_policy'}
            or type(body['version']) is not int or body['version']!=1 or body['path']!=str(path) or not runtime._hash(body['source_hash'])
            or body['kraken_policy']!=['kraken',2,2.0,30.0]
            or type(body['old_policy']) is not list or len(body['old_policy'])!=2
            or type(body['old_state']) is not list or len(body['old_state'])!=2 or digest(body)!=key):raise ValueError('Pacing migration binding')
    _approved(body)
    # Original policy cannot silently change. Existing cadence/high-water/backoff
    # values are never restored; ordinary subsequent reservations remain mutable.
    if [list(r) for r in c.execute("SELECT * FROM policy WHERE provider!='kraken' ORDER BY provider")]!=body['old_policy']:raise ValueError('Original pacing policy changed')
    for original in body['old_state']:
        row=c.execute('SELECT provider,next_at,blocked_until,high_water,pending FROM state WHERE provider=?',(original[0],)).fetchone()
        if row is None or any(row[i]<original[i] for i in (1,2,3)):raise ValueError('Original pacing state regressed')
    return body


def review_plan(path):
    from .provider_pacing import Pacer
    p=Pacer(path)
    with closing(p._connect()) as c:
        c.execute('BEGIN')
        if read(c,p.path):raise ValueError('Pacing already migrated')
        if c.execute('SELECT 1 FROM state WHERE pending IS NOT NULL').fetchone() or c.execute('SELECT 1 FROM waiters').fetchone():raise ValueError('Pacing not quiescent')
        return {'version':1,'path':str(p.path),'source_hash':runtime.implementation_hash(),
                'old_policy':[list(r) for r in c.execute('SELECT * FROM policy ORDER BY provider')],
                'old_state':[list(r) for r in c.execute('SELECT * FROM state ORDER BY provider')],
                'kraken_policy':['kraken',2,2.0,30.0]}


def migrate(path):
    from .provider_pacing import Pacer
    p=Pacer(path)
    with closing(p._connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            old=read(c,p.path)
            if old:c.rollback();return {'status':'ALREADY_MIGRATED','receipt_hash':digest(old)}
            body=review_plan(path);_approved(body)
            # Revalidate original scalar snapshot inside this same writer txn.
            if ([list(r) for r in c.execute('SELECT * FROM policy ORDER BY provider')]!=body['old_policy']
                    or [list(r) for r in c.execute('SELECT * FROM state ORDER BY provider')]!=body['old_state']
                    or c.execute('SELECT 1 FROM waiters').fetchone()):raise ValueError('Pacing changed before migration')
            c.execute(SQL)
            for sql in GUARDS.values():c.execute(sql)
            c.execute(f'INSERT INTO {TABLE} VALUES(1,?,?)',(canonical(body),digest(body)))
            c.execute('INSERT INTO policy VALUES(?,?,?,?)',body['kraken_policy'])
            c.execute('INSERT INTO state VALUES(?,0,0,?,NULL)',('kraken',max(r[3] for r in body['old_state'])))
            read(c,p.path);c.commit()
        except BaseException:c.rollback();raise
    return {'status':'MIGRATED','receipt_hash':digest(body)}


def main(argv=None):
    import argparse,json,sqlite3
    parser=argparse.ArgumentParser(description='Explicit reviewed additive Kraken pacing migration')
    parser.add_argument('--pacing-db',required=True);parser.add_argument('--plan',action='store_true');args=parser.parse_args(argv)
    try:print(json.dumps(review_plan(args.pacing_db) if args.plan else migrate(args.pacing_db),sort_keys=True));return 0
    except (ValueError,OSError,TypeError,KeyError,sqlite3.Error):print('KRAKEN_PACING_MIGRATION_REFUSED');return 2

if __name__=='__main__':raise SystemExit(main())
