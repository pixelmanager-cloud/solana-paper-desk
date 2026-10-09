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
from desk import paper_http403_retirement as retirement

DATABASES = ('research.sqlite', 'evidence.sqlite', 'active-paper.sqlite',
             'token2022-paper.sqlite', 'token2022-boost-00b7c302.sqlite',
             'paper-decisions.sqlite', 'launches.sqlite', 'raw.sqlite',
             'provider-pacing.sqlite', 'discovery/continuous.sqlite')
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


def additions(module):
    table = module.TABLE
    result = {('table', table): ('table', table, table, module._schema())}
    result.update({('trigger', key): ('trigger', key, table, sql)
                   for key, sql in module._guards().items()})
    if module is retirement:
        for i in (1, 2):
            name = f'sqlite_autoindex_{table}_{i}'
            result[('index', name)] = ('index', name, table, None)
    return result


def compare_rows(old, new, table):
    cols = [r[1] for r in old.execute('PRAGMA table_info(' + quote(table) + ')')]
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
    left, right = old.execute(sql), new.execute(sql)
    while True:
        # One bounded row at a time; no retained history prefix or full fetch.
        x, y = left.fetchone(), right.fetchone()
        if x != y:
            raise ValueError('Original rows changed: ' + table)
        if x is None:
            return


def verify(backup, live, *, source):
    if runtime.implementation_hash() != source:
        raise ValueError('Reviewed source mismatch')
    backup, live = Path(backup), Path(live)
    if (not backup.is_absolute() or not live.is_absolute()
            or backup.resolve(strict=True) != backup or live.resolve(strict=True) != live
            or backup == live):
        raise ValueError('Distinct canonical roots required')
    checked = 0
    for name in DATABASES:
        oldpath, newpath = existing(backup / name), existing(live / name)
        if oldpath.stat().st_dev == newpath.stat().st_dev and oldpath.stat().st_ino == newpath.stat().st_ino:
            raise ValueError('Backup aliases live database')
        with closing(sqlite3.connect(oldpath.as_uri() + '?mode=ro', uri=True)) as old, closing(sqlite3.connect(newpath.as_uri() + '?mode=ro', uri=True)) as new:
            for c in (old, new):
                c.execute('PRAGMA query_only=ON')
                c.execute('BEGIN')
            previous, current = inventory(old), inventory(new)
            if any(current.get(k) != v for k, v in previous.items()):
                raise ValueError(name + ': original schema changed')
            module = runtime if name == 'token2022-boost-00b7c302.sqlite' else retirement if name == 'evidence.sqlite' else None
            expected = additions(module) if module else {}
            if any(k in previous for k in expected):
                raise ValueError('Expected additions already present in backup')
            if {k: v for k, v in current.items() if k not in previous} != expected:
                raise ValueError(name + ': unexpected schema additions')
            if module and new.execute('SELECT count(*) FROM ' + quote(module.TABLE)).fetchone() != (1,):
                raise ValueError('Exactly one new receipt required')
            for kind, table in previous:
                if kind == 'table':
                    compare_rows(old, new, table)
                    checked += 1
    return {'status': 'ORIGINALS_PRESERVED', 'databases_checked': len(DATABASES),
            'original_tables_verified': checked, 'source': source,
            'scope': 'PRESERVATION_ONLY_NOT_RECEIPT_AUTHORITY_OR_RESTART_APPROVAL'}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backup', required=True)
    p.add_argument('--source', required=True)
    a = p.parse_args(argv)
    print(json.dumps(verify(a.backup, '/var/lib/solana-desk', source=a.source), sort_keys=True))


if __name__ == '__main__':
    main()
