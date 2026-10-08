"""Disconnected nonmutating exact-format reader, diagnostic REJECT only.

Trusted local callers provide CoordinatorBoundary AND independent ChainAnchor.
No pin discovery, migration, issuance, evidence loading, transport or v1 policy.
Same-UID whole rewrites/rollback/omission remain outside coordinator guarantees.
"""
from contextlib import ExitStack, closing
from dataclasses import asdict
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import time

from .common_bank_receipt_chain import (ChainAnchor, ChainSnapshot, ChainUnavailable,
    MAX_BYTES, MAX_RECORDS, _hash, _json, _preflight_rows)
from .common_bank_receipt_format import (SCHEMA_FINGERPRINT, CERTIFICATE_TABLE,
    MAX_SCHEMA_OBJECTS, MAX_SCHEMA_SQL_BYTES, MAX_SCHEMA_BYTES, PREFLIGHT_SQL, FormatMetrics, validate_header,
    validate_inventory, validate_preflight, validate_format_snapshot)
from .control_obligations import read_guard, read_platform_available
from .pool_receipt_ledger import (CoordinatorBoundary, ApprovedSource, GENESIS,
    NETWORK, PROFILE, _identity, _local_fs)

MAX_READ_SECONDS = 10
INVENTORY_SQL = 'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type COLLATE BINARY,name COLLATE BINARY LIMIT 18'
ROWS_SQL = 'SELECT seq,publication_id,pool,mint,slot,source_id,payload,payload_hash,previous_hash,row_hash FROM coordinator_receipts ORDER BY seq LIMIT 10001'


class ReaderUnavailable(ChainUnavailable):
    """No result returned, no recovery, creation or cached-prefix fallback."""


def _need(condition, code):
    if not condition: raise ReaderUnavailable(code)


def _pins(boundary, anchor):
    _need(type(boundary) is CoordinatorBoundary and type(anchor) is ChainAnchor,'RECEIPT_READER_PINS_REQUIRED')
    _need(_identity(boundary.ledger_id) and type(boundary.sources) is frozenset
          and 1<=len(boundary.sources)<=256 and all(type(s) is ApprovedSource for s in boundary.sources),
          'RECEIPT_READER_BOUNDARY_INVALID')
    _need(type(boundary.allow_synthetic_fixtures) is bool and type(boundary.max_age_seconds) is int
          and 1<=boundary.max_age_seconds<=300,'RECEIPT_READER_BOUNDARY_INVALID')
    _need(len({s.source_id for s in boundary.sources})==len(boundary.sources),'RECEIPT_READER_SOURCE_INVALID')
    for source in boundary.sources:
        _need(_identity(source.source_id) and source.profile==PROFILE and source.network==NETWORK
              and source.genesis_hash==GENESIS and source.source_kind in ('coordinator_capture','synthetic_fixture')
              and (source.source_kind!='synthetic_fixture' or boundary.allow_synthetic_fixtures),
              'RECEIPT_READER_SOURCE_INVALID')
    _need(anchor.schema_hash==SCHEMA_FINGERPRINT and _hash(anchor.certificate_hash)
          and type(anchor.head) is tuple and len(anchor.head)==2 and type(anchor.head[0]) is int
          and 1<=anchor.head[0]<=MAX_RECORDS and _hash(anchor.head[1]),'RECEIPT_READER_ANCHOR_INVALID')
    _preflight_rows(anchor.legacy_rows)
    desc=_json(anchor.descriptor_body)
    # Boundary paths/options come from local arguments, never the candidate DB.
    paths=[]
    for value in (boundary.root,boundary.ledger_path,boundary.evidence_path):
        _need(type(value) in (str,type(Path('/'))) and len(str(value))<=4096,'RECEIPT_READER_PATH_INVALID')
        path=Path(value)
        _need(path.is_absolute() and str(path)==str(path.resolve(strict=True)),'RECEIPT_READER_PATH_INVALID')
        paths.append(path)
    root,ledger,evidence=paths;lock=ledger.with_name(ledger.name+'.coordinator.lock')
    _need(ledger.parent==root and len({ledger,evidence,lock})==3,'RECEIPT_READER_PATH_INVALID')
    expected={**desc,'version':1,'ledger_id':boundary.ledger_id,'profile':PROFILE,
        'root':str(root),'ledger_path':str(ledger),'lock_path':str(lock),'evidence_path':str(evidence),
        'sources':sorted((asdict(s) for s in boundary.sources),key=lambda s:s['source_id']),
        'allow_synthetic_fixtures':boundary.allow_synthetic_fixtures,'max_age_seconds':boundary.max_age_seconds}
    _need(desc==expected,'RECEIPT_READER_BOUNDARY_PIN_MISMATCH')
    for path in (root,ledger,evidence,lock):_local_fs(path)
    return desc,(root,ledger,evidence,lock)


def _stat_ok(info, *, directory=False, private=False):
    _need(info.st_uid==os.getuid(),'RECEIPT_READER_OWNER_INVALID')
    if directory:
        _need(stat.S_ISDIR(info.st_mode) and (stat.S_IMODE(info.st_mode)==0o700 if private else not info.st_mode & 0o022),'RECEIPT_READER_ROOT_INVALID')
    else:
        _need(stat.S_ISREG(info.st_mode) and info.st_nlink==1,'RECEIPT_READER_FILE_INVALID')
        _need(stat.S_IMODE(info.st_mode)==0o600 if private else not info.st_mode & 0o022,
              'RECEIPT_READER_PERMISSIONS_INVALID')


def _stamp(info):
    return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_nlink,
            info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def _open_files(stack, desc, paths):
    held=[]
    for path,key,directory,private in zip(paths,
            ('root_identity','ledger_identity','evidence_identity','lock_identity'),
            (True,False,False,False),(True,True,False,True)):
        _need(str(path)==str(path.resolve(strict=True)),'RECEIPT_READER_PATH_INVALID')
        before=path.lstat();_stat_ok(before,directory=directory,private=private)
        flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC
        if directory:flags|=os.O_DIRECTORY
        fd=os.open(path,flags);stack.callback(os.close,fd)
        info=os.fstat(fd);_stat_ok(info,directory=directory,private=private)
        identity=[info.st_dev,info.st_ino]+([info.st_mtime_ns] if key=='lock_identity' else [])
        _need(identity==desc[key] and (info.st_dev,info.st_ino)==(before.st_dev,before.st_ino),
              'RECEIPT_READER_IDENTITY_MISMATCH')
        held.append((path,fd,directory,private,_stamp(info)))
    # An evidence file may live outside the private coordinator root. Its
    # direct parent must also be owned/nonwritable by other users and stable.
    if paths[2].parent!=paths[0]:
        parent=paths[2].parent;info=parent.lstat();_stat_ok(info,directory=True)
        fd=os.open(parent,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_DIRECTORY);stack.callback(os.close,fd)
        opened=os.fstat(fd);_stat_ok(opened,directory=True)
        _need((info.st_dev,info.st_ino)==(opened.st_dev,opened.st_ino),'RECEIPT_READER_PARENT_CHANGED')
        held.append((parent,fd,True,False,_stamp(opened)))
    return tuple(held)


def _stable(held):
    for path,fd,directory,private,stamp in held:
        _need(str(path)==str(path.resolve(strict=True)),'RECEIPT_READER_PATH_CHANGED')
        named=path.lstat();opened=os.fstat(fd)
        _stat_ok(named,directory=directory,private=private);_stat_ok(opened,directory=directory,private=private)
        if directory:
            _need((named.st_dev,named.st_ino,named.st_mode,named.st_uid)==stamp[:4]
                  and (opened.st_dev,opened.st_ino,opened.st_mode,opened.st_uid)==stamp[:4],
                  'RECEIPT_READER_ROOT_CHANGED')
        else:
            _need(_stamp(named)==stamp and _stamp(opened)==stamp,'RECEIPT_READER_FILE_CHANGED')


def _authorize(action, first, second, database, origin):
    if action==sqlite3.SQLITE_SELECT:return sqlite3.SQLITE_OK
    if action==sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if (database in ('main','temp') or (database is None and second=='')) and first in (
            'sqlite_master','sqlite_temp_master','ledger_descriptor','ledger_head',
            'coordinator_receipts',CERTIFICATE_TABLE) else sqlite3.SQLITE_DENY
    if action==sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK if type(second) is str and second.lower() in (
            'count','sum','max','min','length','typeof','coalesce') else sqlite3.SQLITE_DENY
    if action==sqlite3.SQLITE_TRANSACTION:
        return sqlite3.SQLITE_OK if first in ('BEGIN','COMMIT','ROLLBACK') else sqlite3.SQLITE_DENY
    if action==sqlite3.SQLITE_PRAGMA:
        allowed=(first=='query_only' and second in ('ON','1')) or (
            first=='trusted_schema' and second in ('OFF','0')) or (
            first in ('database_list','journal_mode') and second is None)
        return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_DENY


def _scalar(connection, sql):
    rows=connection.execute(sql).fetchmany(2)
    _need(len(rows)==1 and type(rows[0]) is tuple,'RECEIPT_READER_SCALAR_INVALID')
    return rows[0]


def _load_snapshot(connection):
    """Called ONLY after all scalar byte/type/cardinality/aggregate preflight."""
    descriptor=_scalar(connection,'SELECT body,hash FROM ledger_descriptor WHERE id=1')
    certificate=_scalar(connection,'SELECT body,hash FROM receipt_transition_certificate WHERE id=1')
    head=_scalar(connection,'SELECT count,hash FROM ledger_head WHERE id=1')
    rows=tuple(connection.execute(ROWS_SQL).fetchmany(MAX_RECORDS+1))
    return ChainSnapshot(*descriptor,*certificate,head,rows)


def read_receipt_chain(boundary, anchor):
    """Fresh protected complete read; returns only after guards/connection close.

    No selector/default trust/cache. Returned TypedChain remains diagnostic,
    never a current admission token; every later use requires a new read/pin.
    """
    if not read_platform_available():raise ReaderUnavailable('RECEIPT_READER_PLATFORM_UNAVAILABLE')
    try:
        desc,paths=_pins(boundary,anchor);root,ledger,evidence,lock=paths
        deadline=time.monotonic()+MAX_READ_SECONDS
        with ExitStack() as files:
            held=_open_files(files,desc,paths)
            # Evidence guard -> existing coordinator shared lock -> ledger guard.
            with read_guard(evidence):
                _stable(held)
                fcntl.flock(held[3][1],fcntl.LOCK_SH|fcntl.LOCK_NB)
                try:
                    with read_guard(ledger):
                        _stable(held)
                        header=os.pread(held[1][1],100,0);file_size=os.fstat(held[1][1]).st_size
                        validate_header(header,file_size)
                        with closing(sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True,timeout=0,isolation_level=None)) as c:
                            c.enable_load_extension(False)
                            c.set_authorizer(_authorize)
                            c.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
                            c.execute('PRAGMA query_only=ON');c.execute('PRAGMA trusted_schema=OFF')
                            _need(c.execute('PRAGMA database_list').fetchall()==[(0,'main',str(ledger))],
                                  'RECEIPT_READER_CONNECTION_INVALID')
                            _need(_scalar(c,'PRAGMA journal_mode')==('delete',),'RECEIPT_READER_JOURNAL_INVALID')
                            c.execute('BEGIN')
                            _need(_scalar(c,'SELECT COUNT(*) FROM sqlite_temp_master')==(0,),
                                  'RECEIPT_READER_TEMP_INVALID')
                            # Schema scalar preflight BEFORE inventory SQL TEXT.
                            schema=_scalar(c,PREFLIGHT_SQL['schema'])
                            _need(len(schema)==5 and all(type(v) is int and v>=0 for v in schema)
                                and schema[0]==MAX_SCHEMA_OBJECTS and schema[1]==0 and schema[2]<=128
                                and schema[3]<=MAX_SCHEMA_SQL_BYTES and schema[4]<=MAX_SCHEMA_BYTES,'RECEIPT_READER_SCHEMA_PREFLIGHT')
                            inventory=tuple(c.execute(INVENTORY_SQL).fetchmany(MAX_SCHEMA_OBJECTS+1))
                            validate_inventory(inventory)
                            metrics=FormatMetrics(**{name:_scalar(c,sql) for name,sql in PREFLIGHT_SQL.items()})
                            validate_preflight(metrics,anchor)
                            snapshot=_load_snapshot(c)
                            result=validate_format_snapshot(snapshot,anchor,inventory=inventory,metrics=metrics,
                                header=header,file_size=file_size)
                            _need(time.monotonic()<=deadline,'RECEIPT_READER_DEADLINE')
                            _stable(held)
                            _need(tuple(c.execute(INVENTORY_SQL).fetchmany(MAX_SCHEMA_OBJECTS+1))==inventory
                                  and _scalar(c,'SELECT count,hash FROM ledger_head WHERE id=1')==snapshot.head,
                                  'RECEIPT_READER_IMAGE_CHANGED')
                            c.execute('ROLLBACK')
                        _stable(held)  # After SQLite close while OFD guards remain held.
                    _stable(held)
                finally:fcntl.flock(held[3][1],fcntl.LOCK_UN)
                _stable(held)
            _stable(held)
        return result
    except ReaderUnavailable:raise
    except (ChainUnavailable,ValueError,TypeError,KeyError,IndexError,AttributeError,UnicodeError,
            OverflowError,RecursionError,sqlite3.Error,OSError):
        raise ReaderUnavailable('RECEIPT_READER_UNAVAILABLE') from None
