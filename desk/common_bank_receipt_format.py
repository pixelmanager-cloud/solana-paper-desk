"""Exact disconnected physical-format specification; no SQL execution or I/O.

Only synthetic fixtures currently provision this layout. Functions validate
supplied bounded scalars/bytes; they do not establish protected reads or pins.
Future reader/migration/issuer must share these literal constants and independent
anchors, and must obtain all inputs in one guarded transaction.
"""
from dataclasses import dataclass
from functools import wraps
from types import MappingProxyType

from .common_bank_receipt_chain import (ChainAnchor, ChainUnavailable, MAX_BYTES,
    MAX_RECORDS, MAX_TOTAL_BYTES, _preflight_rows, validate_receipt_chain)
from .model import digest
from .pool_receipt_ledger import SCHEMA

FORMAT_ID = 'pool_receipt_physical_format_v2'
CERTIFICATE_TABLE = 'receipt_transition_certificate'
FENCE_NAME = 'receipt_no_replace'
# Exact same WHEN predicate and ABORT operation: only the message changes.
FENCE_SQL = SCHEMA[FENCE_NAME].replace('Receipt identity already exists',
                                      'Format2 receipt identity already exists')
_sql = dict(SCHEMA)
_sql[FENCE_NAME] = FENCE_SQL
_sql.update({
    CERTIFICATE_TABLE: 'CREATE TABLE receipt_transition_certificate(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL,hash TEXT NOT NULL)',
    'certificate_no_update': "CREATE TRIGGER certificate_no_update BEFORE UPDATE ON receipt_transition_certificate BEGIN SELECT RAISE(ABORT,'Immutable format2 certificate'); END",
    'certificate_no_delete': "CREATE TRIGGER certificate_no_delete BEFORE DELETE ON receipt_transition_certificate BEGIN SELECT RAISE(ABORT,'Immutable format2 certificate'); END",
    'certificate_no_replace': "CREATE TRIGGER certificate_no_replace BEFORE INSERT ON receipt_transition_certificate WHEN EXISTS(SELECT 1 FROM receipt_transition_certificate) BEGIN SELECT RAISE(ABORT,'Immutable format2 certificate'); END",
})
FORMAT_SQL = MappingProxyType(_sql)
del _sql


def _table(name, sql):
    if sql.startswith('CREATE TABLE'): return name
    if name.startswith('certificate_'): return CERTIFICATE_TABLE
    if name.startswith('receipt_'): return 'coordinator_receipts'
    if name.startswith('descriptor_'): return 'ledger_descriptor'
    return 'ledger_head'


EXPECTED_INVENTORY = tuple(sorted(
    [('table' if sql.startswith('CREATE TABLE') else 'trigger', name, _table(name,sql), sql)
     for name,sql in FORMAT_SQL.items()]
    + [('index','sqlite_autoindex_coordinator_receipts_'+str(i),'coordinator_receipts',None)
       for i in (1,2)]))
# Canonical UTF-8 JSON uses sorted object keys and compact separators, tuples as
# arrays, autoindex SQL as null; no rootpage, SQL normalization or partial set.
SCHEMA_FINGERPRINT = digest({'format':FORMAT_ID,'inventory':EXPECTED_INVENTORY})
MAX_SCHEMA_OBJECTS = len(EXPECTED_INVENTORY)
MAX_SCHEMA_SQL_BYTES = 2048
MAX_SCHEMA_BYTES = 32 * 1024
PAGE_SIZE = 4096
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_PAGES = MAX_FILE_BYTES // PAGE_SIZE


def _need(condition, code):
    if not condition: raise ChainUnavailable(code)


def _fail_closed(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:return function(*args, **kwargs)
        except ChainUnavailable:raise
        except (ValueError,TypeError,KeyError,IndexError,AttributeError,UnicodeError,OverflowError):
            raise ChainUnavailable('FORMAT_MALFORMED_INPUT') from None
    return checked


@_fail_closed
def validate_inventory(inventory):
    """Exact ordered sqlite_master type/name/table/SQL inventory, no DB access."""
    _need(type(inventory) is tuple and len(inventory)==MAX_SCHEMA_OBJECTS,'FORMAT_OBJECT_COUNT_INVALID')
    total=0
    for item in inventory:
        _need(type(item) is tuple and len(item)==4,'FORMAT_OBJECT_INVALID')
        kind,name,table,sql=item
        for value in (kind,name,table):
            _need(type(value) is str and len(value)<=128 and len(value.encode())<=128,'FORMAT_OBJECT_BOUND')
            total+=len(value.encode())
        _need(sql is None or (type(sql) is str and len(sql)<=MAX_SCHEMA_SQL_BYTES
              and len(sql.encode())<=MAX_SCHEMA_SQL_BYTES),'FORMAT_SQL_BOUND')
        total+=0 if sql is None else len(sql.encode())
        _need(total<=MAX_SCHEMA_BYTES,'FORMAT_INVENTORY_BOUND')
    _need(inventory==EXPECTED_INVENTORY,'FORMAT_SCHEMA_MISMATCH')


@_fail_closed
def validate_header(header, file_size):
    """Validate already-read 100-byte SQLite header + no-follow fstat size.

    No path/stat/open, journal recovery or assertion of file authenticity here.
    """
    _need(type(header) is bytes and len(header)==100,'FORMAT_HEADER_INVALID')
    _need(type(file_size) is int and PAGE_SIZE<=file_size<=MAX_FILE_BYTES
          and file_size%PAGE_SIZE==0,'FORMAT_FILE_BOUND')
    number=lambda start: int.from_bytes(header[start:start+4],'big')
    _need(header[:16]==b'SQLite format 3\0' and int.from_bytes(header[16:18],'big')==PAGE_SIZE
          and header[18:24]==b'\x01\x01\x00\x40\x20\x20','FORMAT_HEADER_INVALID')
    pages=number(28)
    _need(1<=pages<=MAX_PAGES and pages*PAGE_SIZE==file_size
          and number(24)==number(92),'FORMAT_HEADER_COUNT_INVALID')
    _need(number(44)==4 and number(56)==1 and number(60)==0 and number(68)==0
          and header[72:92]==bytes(20),'FORMAT_HEADER_PROFILE_INVALID')
    free=number(36);trunk=number(32)
    _need(free<pages and ((free==0 and trunk==0) or (free>0 and 1<trunk<=pages)),
          'FORMAT_FREELIST_INVALID')


def _singleton_sql(table):
    return ('SELECT COUNT(*),COALESCE(SUM(CASE WHEN typeof(id)<>\'integer\' OR '
        'typeof(body)<>\'text\' OR typeof(hash)<>\'text\' THEN 1 ELSE 0 END),0),'
        'COALESCE(MIN(id),0),COALESCE(MAX(id),0),'
        'COALESCE(MAX(length(CAST(body AS BLOB))),0),'
        'COALESCE(MAX(length(CAST(hash AS BLOB))),0),'
        'COALESCE(SUM(length(CAST(body AS BLOB))+length(CAST(hash AS BLOB))),0) FROM '+table)


_columns = ('publication_id','pool','mint','slot','source_id','payload','payload_hash','previous_hash','row_hash')
_bytes = tuple('length(CAST('+key+' AS BLOB))' for key in _columns)
_bad = ' OR '.join("typeof("+key+")<>'text'" for key in _columns)
PREFLIGHT_SQL = MappingProxyType({
    'descriptor':_singleton_sql('ledger_descriptor'),
    'certificate':_singleton_sql(CERTIFICATE_TABLE),
    'head': "SELECT COUNT(*),COALESCE(SUM(CASE WHEN typeof(id)<>'integer' OR typeof(count)<>'integer' OR typeof(hash)<>'text' THEN 1 ELSE 0 END),0),COALESCE(MIN(id),0),COALESCE(MAX(id),0),COALESCE(MAX(count),0),COALESCE(MAX(length(CAST(hash AS BLOB))),0),COALESCE(SUM(length(CAST(hash AS BLOB))),0) FROM ledger_head",
    'rows':("SELECT COUNT(*),COALESCE(SUM(CASE WHEN typeof(seq)<>'integer' OR "+_bad+
        " THEN 1 ELSE 0 END),0),COALESCE(MAX("+_bytes[5]+"),0),COALESCE(MAX(MAX("+
        ','.join(b for i,b in enumerate(_bytes) if i!=5)+")),0),COALESCE(SUM("+
        '+'.join(_bytes)+"),0),COALESCE(MIN(seq),0),COALESCE(MAX(seq),0) FROM coordinator_receipts"),
    'schema': "SELECT COUNT(*),COALESCE(SUM(CASE WHEN typeof(type)<>'text' OR typeof(name)<>'text' OR typeof(tbl_name)<>'text' OR (sql IS NOT NULL AND typeof(sql)<>'text') THEN 1 ELSE 0 END),0),COALESCE(MAX(MAX(length(CAST(type AS BLOB)),length(CAST(name AS BLOB)),length(CAST(tbl_name AS BLOB)))),0),COALESCE(MAX(length(CAST(sql AS BLOB))),0),COALESCE(SUM(length(CAST(type AS BLOB))+length(CAST(name AS BLOB))+length(CAST(tbl_name AS BLOB))+COALESCE(length(CAST(sql AS BLOB)),0)),0) FROM sqlite_master",
})
del _columns, _bytes, _bad


@dataclass(frozen=True)
class FormatMetrics:
    """Numeric SQL-only preflight; not a protected read or authority token."""
    descriptor: tuple
    certificate: tuple
    head: tuple
    rows: tuple
    schema: tuple


@_fail_closed
def validate_preflight(metrics, anchor):
    """Bound scalars BEFORE full metadata/row materialization by future reader."""
    _need(type(metrics) is FormatMetrics and type(anchor) is ChainAnchor,'FORMAT_TYPED_PREFLIGHT_REQUIRED')
    for key,size in (('descriptor',7),('certificate',7),('head',7),('rows',7),('schema',5)):
        values=getattr(metrics,key)
        _need(type(values) is tuple and len(values)==size and all(type(v) is int and 0<=v<=MAX_FILE_BYTES for v in values),
              'FORMAT_METRIC_INVALID')
    for values in (metrics.descriptor,metrics.certificate):
        count,bad,min_id,max_id,body,hash_bytes,total=values
        _need((count,bad,min_id,max_id)==(1,0,1,1) and 1<=body<=MAX_BYTES
              and hash_bytes==64 and total==body+64,'FORMAT_SINGLETON_INVALID')
    count,bad,min_id,max_id,records,hash_bytes,total=metrics.head
    _need((count,bad,min_id,max_id)==(1,0,1,1) and 1<=records<=MAX_RECORDS
          and hash_bytes==total==64,'FORMAT_HEAD_INVALID')
    count,bad,payload,other,total,min_seq,max_seq=metrics.rows
    _need(1<=count<=MAX_RECORDS and count==records and bad==0 and 1<=payload<=MAX_BYTES
          and 1<=other<=128 and min_seq==1 and max_seq==count,'FORMAT_ROWS_INVALID')
    objects,bad,names,sql,size=metrics.schema
    _need(objects==MAX_SCHEMA_OBJECTS and bad==0 and 1<=names<=128
          and 1<=sql<=MAX_SCHEMA_SQL_BYTES and size<=MAX_SCHEMA_BYTES,'FORMAT_SCHEMA_BOUND')
    _need(anchor.schema_hash==SCHEMA_FINGERPRINT,'FORMAT_UNREVIEWED_SCHEMA_PIN')
    _need(type(anchor.head) is tuple and len(anchor.head)==2 and type(anchor.head[0]) is int
          and anchor.head[0]==records,'FORMAT_CURRENT_COUNT_MISMATCH')
    _need(type(anchor.descriptor_body) is str and len(anchor.descriptor_body)<=MAX_BYTES
          and len(anchor.descriptor_body.encode())<=MAX_BYTES,'FORMAT_ANCHOR_BOUND')
    prefix=_preflight_rows(anchor.legacy_rows)
    aggregate=total+prefix+metrics.descriptor[6]-64+metrics.certificate[6]-64+len(anchor.descriptor_body.encode())
    _need(aggregate<=MAX_TOTAL_BYTES,'FORMAT_SHARED_BYTE_BOUND')


def validate_format_snapshot(snapshot, anchor, *, inventory, metrics, header, file_size):
    """Disconnected consistency proof only: supplied inputs are not authenticated."""
    try:
        validate_header(header,file_size)
        validate_preflight(metrics,anchor)
        validate_inventory(inventory)
        # Pin concrete fingerprint independently of candidate certificate choice.
        result=validate_receipt_chain(snapshot,anchor)
        _need(_loaded_metrics(snapshot,inventory)==metrics,'FORMAT_PREFLIGHT_SNAPSHOT_MISMATCH')
        return result
    except ChainUnavailable:raise
    except (ValueError,TypeError,KeyError,IndexError,AttributeError,UnicodeError,OverflowError):
        raise ChainUnavailable('FORMAT_MALFORMED_INPUT') from None


def _loaded_metrics(snapshot, inventory):
    """After bounded dispatch, require preflight scalars and loaded image agree."""
    singleton=lambda body,key: (1,0,1,1,len(body.encode()),len(key.encode()),len(body.encode())+len(key.encode()))
    rows=snapshot.rows
    other=(1,2,3,4,5,7,8,9)
    row_metrics=(len(rows),0,max(len(r[6].encode()) for r in rows),
        max(len(r[i].encode()) for r in rows for i in other),
        sum(len(v.encode()) for r in rows for v in r[1:]),rows[0][0],rows[-1][0])
    schema_metrics=(len(inventory),0,max(len(v.encode()) for row in inventory for v in row[:3]),
        max(len(row[3].encode()) for row in inventory if row[3] is not None),
        sum(len(v.encode()) for row in inventory for v in row if v is not None))
    return FormatMetrics(singleton(snapshot.descriptor_body,snapshot.descriptor_hash),
        singleton(snapshot.certificate_body,snapshot.certificate_hash),
        (1,0,1,1,snapshot.head[0],len(snapshot.head[1].encode()),len(snapshot.head[1].encode())),
        row_metrics,schema_metrics)
