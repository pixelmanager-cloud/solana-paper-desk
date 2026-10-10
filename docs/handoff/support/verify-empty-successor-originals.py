"""Coordinator-only quiesced preservation check; never authorizes restart.

Run from the reviewed release with PYTHONPATH set to it. Receipt authenticity,
policy/proof validation and stopped writers are separate coordinator gates.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from desk import runtime_compatibility as runtime
from desk import runtime_empty_history_successor as continuation
from desk import paper_empty_history_reconciliation as recovery
from desk import paper_terminal_reconciliation as terminal
from desk.model import canonical, digest

DATABASES = ('research.sqlite', 'evidence.sqlite', 'active-paper.sqlite',
             'token2022-paper.sqlite', 'token2022-boost-00b7c302.sqlite',
             'paper-decisions.sqlite', 'launches.sqlite', 'raw.sqlite',
             'provider-pacing.sqlite', 'discovery/continuous.sqlite', 'paper-kraken-77de75a2.sqlite', 'entry-dispatch/dispatch.sqlite')
MAX_CELL_BYTES = 16 * 1024 * 1024


def quote(value):
    return '"' + value.replace('"', '""') + '"'


def existing(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_file():
        raise ValueError('Canonical existing regular database required')
    if path.stat().st_nlink != 1:
        raise ValueError('Hard-linked database refused')
    return path


def inventory(c):
    # Include SQLite autoindexes and internal tables; never collapse object names.
    bounds = c.execute('SELECT count(*),COALESCE(sum(length(CAST(name AS BLOB))+length(CAST(tbl_name AS BLOB))+COALESCE(length(CAST(sql AS BLOB)),0)),0) FROM sqlite_master').fetchone()
    if bounds[0] > 512 or bounds[1] > 512 * 1024:
        raise ValueError('Schema inventory bound')
    rows = c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master').fetchall()
    result = {(r[0], r[1]): r for r in rows}
    if len(result) != len(rows):
        raise ValueError('Ambiguous schema identities')
    return result


def compare_rows(old, new, table, *, exclude_pass=None):
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
    sql = 'SELECT ' + fields + ',' + types + ' FROM ' + quote(table) + ' ORDER BY ' + order
    left = old.execute(sql)
    right = new.execute(sql if exclude_pass is None else sql.replace(' ORDER BY ', ' WHERE pass_id IS NOT ? ORDER BY ', 1), () if exclude_pass is None else (exclude_pass,))
    while True:
        # One bounded row at a time; no retained history prefix or full fetch.
        x, y = left.fetchone(), right.fetchone()
        if x != y:
            raise ValueError('Original rows changed: ' + table)
        if x is None:
            return



def verify(backup, live, *, source):
    if runtime.implementation_hash()!=source:raise ValueError('Exact source required')
    backup,live=Path(backup),Path(live)
    if not backup.is_absolute() or not live.is_absolute() or backup.resolve(strict=True)!=backup or live.resolve(strict=True)!=live or backup==live:raise ValueError('Distinct canonical roots required')
    policy=json.loads(recovery.POLICY.read_text())
    if len(policy['associations'])!=1:raise ValueError('One exact recovery policy required')
    pin=policy['associations'][0];recovery.approved(pin)
    count=0
    for name in DATABASES:
        op,np=existing(backup/name),existing(live/name)
        if (op.stat().st_dev,op.stat().st_ino)==(np.stat().st_dev,np.stat().st_ino):raise ValueError('Backup aliases live')
        with closing(sqlite3.connect(op.as_uri()+'?mode=ro',uri=True)) as old,closing(sqlite3.connect(np.as_uri()+'?mode=ro',uri=True)) as new:
            for c in (old,new):c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
            before,after=inventory(old),inventory(new)
            if name=='evidence.sqlite' and (('table',terminal.TABLE) not in before or ('table',terminal.TABLE) not in after):raise ValueError('Original terminal table required')
            if any(after.get(k)!=v for k,v in before.items()):raise ValueError('Original schema changed: '+name)
            expected={}
            if name=='paper-kraken-77de75a2.sqlite':
                t=continuation.TABLE
                expected={('table',t):('table',t,t,continuation.schema())}
                expected.update({('trigger',k):('trigger',k,t,v) for k,v in continuation.guards().items()})
                if any(k in before for k in expected):raise ValueError('Continuation already in baseline')
                from desk import runtime_performance_continuation as parent
                if parent.require(new)!=source:raise ValueError('Continuation authority mismatch')
            if {k:v for k,v in after.items() if k not in before}!=expected:raise ValueError('Unexpected schema addition: '+name)
            for kind,table in before:
                if kind=='table':
                    excluded=None
                    if name=='evidence.sqlite' and table==terminal.TABLE:
                        excluded=pin['pass_id']
                        if new.execute('SELECT COUNT(*) FROM '+quote(table)).fetchone()[0]!=old.execute('SELECT COUNT(*) FROM '+quote(table)).fetchone()[0]+1:raise ValueError('Exactly one recovery addition required')
                        if old.execute('SELECT 1 FROM '+quote(table)+' WHERE pass_id=?',(excluded,)).fetchone():raise ValueError('Recovery already present in baseline')
                        row=new.execute('SELECT rowid,pass_id,scan_id,payload,payload_hash FROM '+quote(table)+' WHERE pass_id=?',(excluded,)).fetchall()
                        next_id=old.execute('SELECT COALESCE(MAX(rowid),0)+1 FROM '+quote(table)).fetchone()[0]
                        if row!=[(next_id,excluded,pin['scan_id'],canonical(pin),digest(pin))]:raise ValueError('Exact appended recovery row required')
                    compare_rows(old,new,table,exclude_pass=excluded);count+=1
    return {'status':'ORIGINALS_PRESERVED','databases_checked':len(DATABASES),'original_tables_verified':count,'source':source,'scope':'PRESERVATION_ONLY_NOT_RESTART_APPROVAL'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--backup',required=True);p.add_argument('--source',required=True);a=p.parse_args()
    print(json.dumps(verify(a.backup,'/var/lib/solana-desk',source=a.source),sort_keys=True))
