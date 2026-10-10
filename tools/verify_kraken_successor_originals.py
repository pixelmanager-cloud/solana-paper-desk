"""Quiesced Kraken successor preservation only; never authorizes restart.

Use exact externally reviewed policies, stopped writers and accepted gate validators
separately. No database creation, migration, recovery or network operations.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from desk import monitoring_successor as successor, monitoring_handoff as handoff
from desk import kraken_pacing_migration as migration
from desk.model import canonical, digest
from desk.paper_cycle_cli import _config
from tools.verify_http403_originals import DATABASES, MAX_CELL_BYTES, quote, existing, inventory
from desk import runtime_compatibility as runtime

def compare_rows(old, new, table, *, new_where="", args=()):
    # Generated columns also shadow rowid aliases; table_info omits them.
    cols = [r[1] for r in old.execute('PRAGMA table_xinfo(' + quote(table) + ')')]
    if not 1 <= len(cols) <= 64:
        raise ValueError('Original column bound')
    # Bound a materialized row before fetching payloads, including BLOBs.
    sizes = '+'.join('COALESCE(length(CAST(' + quote(c) + ' AS BLOB)),0)' for c in cols)
    for conn in (old, new):
        if conn.execute('SELECT 1 FROM ' + quote(table) + ' WHERE ' + sizes + '>? LIMIT 1', (MAX_CELL_BYTES,)).fetchone():
            raise ValueError('Original row byte bound')
    fields = ','.join(quote(c) for c in cols)
    types = ','.join('typeof(' + quote(c) + ')' for c in cols)
    order = ','.join(quote(c) + ' COLLATE BINARY' for c in cols) + ',' + types
    layout = [r for r in old.execute('PRAGMA table_list') if r[0] == 'main' and r[1] == table]
    if len(layout) != 1:
        raise ValueError('Original table layout unavailable')
    if not layout[0][4]:  # WITHOUT ROWID tables use their preserved primary key.
        shadowed = {c.casefold() for c in cols}
        aliases = [c for c in ('rowid', '_rowid_', 'oid') if c not in shadowed]
        if not aliases:
            # Never mistake an ordinary shadow column for the hidden identity.
            raise ValueError('All original rowid aliases shadowed')
        identity = quote(aliases[0])
        fields = identity + ',' + fields
        types = 'typeof(' + identity + '),' + types
        order = identity + ',' + order
    select = 'SELECT ' + fields + ',' + types + ' FROM ' + quote(table)
    sql = select + ' ORDER BY ' + order
    left = old.execute(sql)
    new_sql = select + (' WHERE ' + new_where if new_where else '') + ' ORDER BY ' + order
    right = new.execute(new_sql, args)
    while True:
        # One bounded row at a time; no retained history prefix or full fetch.
        x, y = left.fetchone(), right.fetchone()
        if x != y:
            raise ValueError('Original rows changed: ' + table)
        if x is None:
            return


def verify(backup, live, *, source, successor_pin, pacing_pin, config):
    if runtime.implementation_hash() != source or successor_pin['new_source'] != source or pacing_pin['source_hash'] != source:
        raise ValueError('Reviewed source mismatch')
    successor.approved(successor_pin)
    migration._approved(pacing_pin)
    backup, live = Path(backup), Path(live)
    if any(not p.is_absolute() or p.resolve(strict=True) != p for p in (backup, live)) or backup == live:
        raise ValueError('Distinct canonical roots required')
    ctx = successor_pin['context']
    if any(ctx[k] != str(live / name) for k, name in
           (('research_db','research.sqlite'),('evidence_db','evidence.sqlite'),
            ('old_ledger_db','token2022-boost-00b7c302.sqlite'),('pacing_db','provider-pacing.sqlite'))):
        raise ValueError('Pin context mismatch')
    if pacing_pin['path'] != ctx['pacing_db']:
        raise ValueError('Pacing pin context mismatch')
    newpath = existing(ctx['new_ledger_db'])
    if newpath.parent != live or newpath.name in DATABASES or (backup/newpath.name).exists():
        raise ValueError('Separate newly initialized ledger required')
    checked = 0
    native_schema = None
    for name in DATABASES:
        oldpath, newfile = existing(backup/name), existing(live/name)
        if oldpath.samefile(newfile) or newpath.samefile(newfile) or newpath.samefile(oldpath):
            raise ValueError('Database inode alias refused')
        with closing(sqlite3.connect(oldpath.as_uri()+'?mode=ro',uri=True)) as old, closing(sqlite3.connect(newfile.as_uri()+'?mode=ro',uri=True)) as new:
            for c in (old,new):
                c.execute('PRAGMA query_only=ON'); c.execute('BEGIN')
            previous, current = inventory(old), inventory(new)
            if any(current.get(k) != v for k,v in previous.items()):
                raise ValueError('Original schema changed: '+name)
            if name=='token2022-boost-00b7c302.sqlite':
                native={'metadata','events','outcomes','state','raw_events','health','sqlite_sequence'}
                native_schema={k:v for k,v in previous.items() if v[2] in native}
            expected = {}
            filters = {}
            if name == 'evidence.sqlite':
                table=successor.TABLE
                if previous.get(('table',table)) != ('table',table,table,successor.schema()):
                    raise ValueError('Existing successor schema required')
                for key,sql in successor.guards().items():
                    if previous.get(('trigger',key)) != ('trigger',key,table,sql):
                        raise ValueError('Existing successor guards required')
                before=old.execute('SELECT count(*),max(seq) FROM '+table).fetchone()
                first=handoff.read(old)
                history=successor.rows(old,first) if first else []
                if not history or history[-1][1]!=successor_pin['parent_hash'] or first[1]!=successor_pin['first_hash']:
                    raise ValueError('Reviewed successor parent differs from original prefix')
                seq=successor_pin['sequence']
                if before != (seq-1,seq-1) or seq<2:
                    raise ValueError('Exact next successor prefix required')
                bounds=new.execute('SELECT seq,typeof(body),length(CAST(body AS BLOB)),typeof(body_hash),length(CAST(body_hash AS BLOB)) FROM '+table+' ORDER BY seq').fetchall()
                if len(bounds)!=seq or any(r[1]!='text' or not 0<r[2]<=16384 or r[3:]!=('text',64) for r in bounds):raise ValueError('Successor scalar bounds')
                row=new.execute('SELECT seq,body,body_hash FROM '+table+' WHERE seq=?',(seq,)).fetchall()
                if row != [(seq,canonical(successor_pin),digest(successor_pin))] or new.execute('SELECT count(*) FROM '+table).fetchone() != (seq,):
                    raise ValueError('Exact appended successor required')
                filters[table]=('seq<?',(seq,))
            elif name == 'provider-pacing.sqlite':
                expected={('table',migration.TABLE):('table',migration.TABLE,migration.TABLE,migration.SQL)}
                expected.update({('trigger',k):('trigger',k,migration.TABLE,sql) for k,sql in migration.GUARDS.items()})
                if any(k in previous for k in expected):raise ValueError('Pacing migration already present')
                migration.read(new,newfile)
                if new.execute('SELECT id,payload,payload_hash FROM '+migration.TABLE).fetchall() != [(1,canonical(pacing_pin),digest(pacing_pin))]:
                    raise ValueError('Exact pacing receipt required')
                if any(old.execute('SELECT count(*) FROM '+t).fetchone()!=(2,) for t in ('policy','state')):raise ValueError('Original pacing row count')
                if [list(r) for r in old.execute('SELECT * FROM policy ORDER BY provider')] != pacing_pin['old_policy'] or [list(r) for r in old.execute('SELECT * FROM state ORDER BY provider')] != pacing_pin['old_state']:
                    raise ValueError('Pacing original snapshot differs from pin')
                if old.execute('SELECT 1 FROM state WHERE pending IS NOT NULL').fetchone() or old.execute('SELECT 1 FROM waiters').fetchone():raise ValueError('Original pacing not quiescent')
                for table, row in (('policy',pacing_pin['kraken_policy']),('state',['kraken',0,0,max(r[3] for r in pacing_pin['old_state']),None])):
                    if new.execute('SELECT * FROM '+table+" WHERE provider='kraken'").fetchall() != [tuple(row)]:raise ValueError('Exact Kraken initial row required')
                    filters[table]=("provider!='kraken'",())
            if {k:v for k,v in current.items() if k not in previous} != expected:
                raise ValueError('Unexpected schema additions: '+name)
            for kind,table in previous:
                if kind=='table':
                    where,args=filters.get(table,('',()))
                    compare_rows(old,new,table,new_where=where,args=args);checked+=1
    with closing(sqlite3.connect(newpath.as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
        objects=inventory(c)
        tables={k[1] for k in objects if k[0]=='table'}
        if objects!=native_schema or tables!={'metadata','events','outcomes','state','raw_events','health','sqlite_sequence'}:raise ValueError('New ledger schema mismatch')
        if c.execute('SELECT rowid,name,seq FROM sqlite_sequence').fetchall()!=[(1,'events',1)]:raise ValueError('New ledger sequence mismatch')
        for table in ('raw_events','health'):
            if c.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone():raise ValueError('New ledger is not pristine')
    fresh=handoff._ledger(str(newpath),successor_pin['new_config_hash'],source)
    if digest(config)!=successor_pin['new_config_hash'] or fresh['config']!=config or fresh['state']['positions']:
        raise ValueError('New ledger configuration/state mismatch')
    if digest(fresh['metadata'])!=successor_pin['new_metadata_hash'] or fresh['checkpoint_hash']!=successor_pin['new_checkpoint_hash'] or any(fresh[k]!=successor_pin['new_'+k] for k in ('events_count','events_hash','outcomes_count','outcomes_hash')):
        raise ValueError('New ledger anchors differ from reviewed pin')
    return dict(status='ORIGINALS_PRESERVED',databases_checked=10,new_ledgers_checked=1,original_tables_verified=checked,source=source,successor_hash=digest(successor_pin),pacing_hash=digest(pacing_pin),scope='PRESERVATION_ONLY_NOT_RESTART_APPROVAL')


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for k in ('backup','source','successor-pin','successor-hash','pacing-pin','pacing-hash','config'):p.add_argument('--'+k,required=True)
    a=p.parse_args(argv)
    pins=[]
    for path,key in ((a.successor_pin,a.successor_hash),(a.pacing_pin,a.pacing_hash)):
        with Path(path).open('rb') as f:raw=f.read(65537)
        if len(raw)>65536:raise ValueError('Pin byte bound')
        pin=runtime._parse(raw.decode())
        if digest(pin)!=key:raise ValueError('Independent pin hash mismatch')
        pins.append(pin)
    print(json.dumps(verify(a.backup,'/var/lib/solana-desk',source=a.source,successor_pin=pins[0],pacing_pin=pins[1],config=_config(a.config)),sort_keys=True))

if __name__=='__main__':main()
