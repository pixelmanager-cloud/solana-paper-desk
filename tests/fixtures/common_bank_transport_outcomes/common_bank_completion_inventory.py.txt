"""Disconnected physical completion inventory; never operational quiescence.

No writer/session/transport constructors are called. Original rows are retained
as tuples, including raw attachment bytes. Semantic replay belongs elsewhere.
"""
from contextlib import ExitStack, closing
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import time

from . import common_bank_journal as journal
from . import pool_capture_bridge as capture
from . import pool_receipt_ledger as ledger
from .common_bank_receipt_chain import ChainSnapshot, validate_receipt_chain
from .common_bank_receipt_format import EXPECTED_INVENTORY, FORMAT_SQL, validate_header
from .common_bank_receipt_reader import _stat_ok, _stamp, _stable
from .control_obligations import read_guard, read_platform_available
from .model import canonical, digest
from .history_progress import HistoryProgress

MAX_ROWS = 10000
MAX_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_SECONDS = 10

# Trusted constants only. No SQL, table name or column selection is taken from
# candidate sqlite_master rows, even after validating that inventory.
BASE_TABLE_SQL = {
    'pages':'CREATE TABLE pages(hash TEXT PRIMARY KEY,payload BLOB NOT NULL,raw_bytes INTEGER NOT NULL)',
    'ownership_budgets':'CREATE TABLE ownership_budgets(id TEXT PRIMARY KEY,source_hash TEXT NOT NULL,used INTEGER NOT NULL,ceiling INTEGER NOT NULL)',
    'ownership_admissions':'CREATE TABLE ownership_admissions(id TEXT PRIMARY KEY,descriptor TEXT NOT NULL,state TEXT NOT NULL,prepared_source TEXT,prepared_used INTEGER,completed_source_hash TEXT)',
    'scans':'CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)',
}
TRUSTED_SQL = (BASE_TABLE_SQL | ledger.SCHEMA | dict(FORMAT_SQL) | capture.SCHEMA | journal.SCHEMA
               | journal.ATTACHMENT_SCHEMA | journal.SLOT_SCHEMA | journal.UNION_SCHEMA | journal.CLOCK_SCHEMA)
COLUMNS = {name:tuple(re.findall(r'(?:\(|,)([a-z_]+) (TEXT|INTEGER|BLOB)\b',sql))
           for name,sql in TRUSTED_SQL.items() if sql.startswith('CREATE TABLE ')}
COMMON_AUTOINDEXES = {'common_bank_runs':(2,3),'common_bank_events':(1,),
    **{name:(2,) for name in ('common_bank_attachments','common_bank_slot_intents',
       'common_bank_slot_attachments','common_bank_union_intents','common_bank_union_attachments',
       'common_bank_clock_intents','common_bank_clock_attachments')}}


def _objects(sqls, indexes):
    """Parse literal accepted SQL, never candidate SQL; include autoindexes."""
    objects=[]
    for name,sql in sqls.items():
        table=re.match(r'CREATE TABLE ([a-z_]+)\(',sql)
        if table:objects.append(('table',name,table[1],sql))
        else:
            trigger=re.match(r'CREATE TRIGGER ([a-z_]+) BEFORE (?:INSERT|UPDATE|DELETE) ON ([a-z_]+)\b',sql)
            need(trigger is not None and trigger[1]==name,'INVENTORY_CONSTANT_SCHEMA')
            objects.append(('trigger',name,trigger[2],sql))
    for table,ordinals in indexes.items():
        if table in sqls:
            objects.extend(('index',f'sqlite_autoindex_{table}_{i}',table,None) for i in ordinals)
    return tuple(sorted(objects))


class InventoryUnavailable(ValueError):
    """No partial inventory or previous-success fallback is returned."""


def need(value, code):
    if not value: raise InventoryUnavailable(code)


@dataclass(frozen=True)
class InventoryPins:
    # Trusted local operator arguments, never populated from the candidate DB.
    ledger_descriptor: str
    ledger_head: tuple
    research_path: Path
    research_identity: tuple
    journal_descriptor: str
    capture_head: tuple | None
    chain_anchor: object = None


@dataclass(frozen=True)
class CompletionInventory:
    # Every selected row, without source/freshness/slot/completion filtering.
    tables: tuple
    schemas: tuple
    file_identities: tuple
    snapshot_hash: str
    blockers: tuple

    def diagnostic(self):
        return {'status': 'REJECT', 'blockers': self.blockers, 'snapshot_hash': self.snapshot_hash,
                'provider_calls': 0, 'operational_quiescence': False,
                'completion_authenticated': False, 'migration_allowed': False,
                'source_authenticated': False, 'entry_allowed': False}


def _json(raw):
    need(type(raw) is str and len(raw.encode('utf-8')) <= 2*1024*1024, 'INVENTORY_JSON_BOUND')
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, 'INVENTORY_DUPLICATE_JSON_KEY'); result[key] = value
        return result
    def constant(value): raise InventoryUnavailable('INVENTORY_NONFINITE_JSON')
    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    need(canonical(result) == raw, 'INVENTORY_NONCANONICAL_JSON')
    return result


def _held(stack, path, identity, *, directory=False):
    path = Path(path)
    need(path.is_absolute() and str(path) == str(path.resolve(strict=True)), 'INVENTORY_CANONICAL_PATH')
    info = path.lstat(); _stat_ok(info, directory=directory, private=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_DIRECTORY if directory else 0))
    stack.callback(os.close, fd); opened = os.fstat(fd)
    _stat_ok(opened, directory=directory, private=True)
    need(tuple(identity) == (opened.st_dev, opened.st_ino), 'INVENTORY_IDENTITY_PIN')
    need((info.st_dev, info.st_ino) == tuple(identity), 'INVENTORY_IDENTITY_RACE')
    if not directory: need(100 <= opened.st_size <= MAX_FILE_BYTES, 'INVENTORY_FILE_BOUND')
    return path, fd, directory, True, _stamp(opened)


def _schema(c):
    metrics = c.execute("SELECT count(*),coalesce(max(length(CAST(name AS BLOB))),0),coalesce(max(length(CAST(sql AS BLOB))),0),coalesce(sum(coalesce(length(CAST(sql AS BLOB)),0)+length(CAST(name AS BLOB))+length(CAST(tbl_name AS BLOB))+length(CAST(type AS BLOB))),0),coalesce(max(length(CAST(tbl_name AS BLOB))),0),coalesce(max(length(CAST(type AS BLOB))),0),coalesce(sum(CASE WHEN typeof(name)!='text' OR typeof(tbl_name)!='text' OR typeof(type)!='text' OR typeof(sql) NOT IN ('text','null') THEN 1 ELSE 0 END),0) FROM sqlite_master").fetchone()
    need(all(type(x) is int for x in metrics) and metrics[0] <= 256 and metrics[1] <= 128
         and metrics[2] <= 8192 and metrics[3] <= 256*1024 and metrics[4]<=128
         and metrics[5]<=16 and metrics[6]==0, 'INVENTORY_SCHEMA_BOUND')
    rows = tuple(c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchmany(257))
    need(all(type(r[0]) is str and type(r[1]) is str and type(r[2]) is str
             and (r[3] is None or type(r[3]) is str) for r in rows), 'INVENTORY_SCHEMA_TYPES')
    return rows


def _family(schema, expected, prefix):
    indexes=COMMON_AUTOINDEXES if prefix=='common_bank_' else {'pool_capture_events':(1,)}
    wanted=_objects(expected,indexes)
    names={row[1] for row in wanted};tables={row[1] for row in wanted if row[0]=='table'}
    if prefix=='pool_capture_':
        names.update(capture.SCHEMA)
        tables.update(name for name,sql in capture.SCHEMA.items() if sql.startswith('CREATE TABLE '))
    actual=tuple(row for row in schema if row[1] in names or row[2] in tables
                 or row[1].startswith(prefix) or row[2].startswith(prefix))
    # Cardinality, type, owning table and SQL all participate. A table/trigger
    # or index/trigger name collision must never overwrite another object.
    need(actual == wanted, 'INVENTORY_UNSUPPORTED_OR_PARTIAL_SCHEMA')


def _read_tables(c, schema, names, budget):
    # SQL identifiers originate only in fixed module constants, never JSON.
    specs = []
    for name in names:
        need(name in COLUMNS,'INVENTORY_CONSTANT_TABLE_REQUIRED')
        if name=='pages':
            count,bad=c.execute("SELECT count(*),coalesce(sum(CASE WHEN typeof(hash)!='text' OR length(CAST(hash AS BLOB))!=64 OR typeof(payload)!='blob' OR typeof(raw_bytes)!='integer' OR raw_bytes<0 OR raw_bytes>16777216 THEN 1 ELSE 0 END),0) FROM pages").fetchone()
            need(bad==0,'INVENTORY_PAGE_CATALOG_TYPES')
            budget[0]+=count;budget[1]+=count*96
            need(budget[0]<=MAX_ROWS and budget[1]<=MAX_BYTES,'INVENTORY_SHARED_BOUND')
            specs.append((name,'hash',count));continue
        columns = c.execute(f'PRAGMA table_info({name})').fetchall()
        need(columns and len(columns) <= 16, 'INVENTORY_COLUMNS')
        need(tuple((row[1],row[2]) for row in columns)==COLUMNS[name],'INVENTORY_CONSTANT_COLUMNS')
        for index,(key,declared) in enumerate(COLUMNS[name]):
            col=columns[index]
            limit = (2*1024*1024 if key in ('result','prepared_source') else
                     65536 if key in ('plan_json','response_bytes') else
                     16384 if key=='event_json' and name=='common_bank_union_intents' else
                     8192 if key in ('body','payload','descriptor','descriptor_json','event_json','request_bytes') else
                     2048 if key=='failure_json' else 128)
            expected={'TEXT':'text','INTEGER':'integer','BLOB':'blob'}[declared]
            nullable=not col[3] and not col[5]
            allowed=f"'{expected}'"+(",'null'" if nullable else '')
            need(c.execute(f'SELECT count(*) FROM "{name}" WHERE typeof("{key}") NOT IN ({allowed}) OR length(CAST("{key}" AS BLOB))>{limit}').fetchone()==(0,),
                 'INVENTORY_FIELD_PREFLIGHT')
        size = '+'.join(f'coalesce(length(CAST("{key}" AS BLOB)),0)' for key,_ in COLUMNS[name])
        invalid = ' OR '.join(f'(typeof("{key}") NOT IN (\'null\',\'text\',\'integer\',\'blob\'))' for key,_ in COLUMNS[name])
        count, total, maximum, bad = c.execute(f'SELECT count(*),coalesce(sum({size}),0),coalesce(max({size}),0),coalesce(sum(CASE WHEN {invalid} THEN 1 ELSE 0 END),0) FROM "{name}"').fetchone()
        need(all(type(v) is int and v >= 0 for v in (count,total,maximum,bad))
             and bad == 0 and maximum <= 2*1024*1024, 'INVENTORY_SCALAR_BOUND')
        if name.startswith('common_bank_') and not name.endswith('_meta'):
            need(count<=journal.MAX_RUNS,'INVENTORY_STAGE_CAPACITY')
        budget[0] += count; budget[1] += total
        need(budget[0] <= MAX_ROWS and budget[1] <= MAX_BYTES, 'INVENTORY_SHARED_BOUND')
        order = ','.join(f'"{key}"' for i,(key,_) in enumerate(COLUMNS[name]) if columns[i][5]) or 'rowid'
        specs.append((name, order, count))
    return specs


def _load(c, specs):
    result = {}
    for name, order, count in specs:
        select='hash,raw_bytes,length(payload)' if name=='pages' else ','.join(f'"{key}"' for key,_ in COLUMNS[name])
        rows = tuple(c.execute(f'SELECT {select} FROM "{name}" ORDER BY {order} LIMIT {MAX_ROWS+1}').fetchmany(MAX_ROWS+1))
        need(len(rows) == count, 'INVENTORY_COUNT_CHANGED')
        for row in rows:
            for value in row:
                if type(value) is str: value.encode('utf-8')
        result[name] = rows
    return result


def _connection(stack, path, deadline):
    c = stack.enter_context(closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0,isolation_level=None)))
    c.enable_load_extension(False)
    def authorize(action,first,second,database,origin):
        if action==sqlite3.SQLITE_SELECT:return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_READ:return sqlite3.SQLITE_OK if database in ('main',None) else sqlite3.SQLITE_DENY
        if action==sqlite3.SQLITE_FUNCTION:return sqlite3.SQLITE_OK if second.lower() in ('count','sum','coalesce','max','length','typeof') else sqlite3.SQLITE_DENY
        if action==sqlite3.SQLITE_TRANSACTION:return sqlite3.SQLITE_OK if first in ('BEGIN','ROLLBACK') else sqlite3.SQLITE_DENY
        if action==sqlite3.SQLITE_PRAGMA:
            allowed=(first=='query_only' and second=='ON') or (first=='trusted_schema' and second=='OFF') or (
                first in ('journal_mode','user_version','application_id') and second is None) or first=='table_info'
            return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY
    c.set_authorizer(authorize)
    c.execute('PRAGMA query_only=ON'); c.execute('PRAGMA trusted_schema=OFF')
    need(c.execute('PRAGMA journal_mode').fetchone() == ('delete',), 'INVENTORY_ROLLBACK_REQUIRED')
    c.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
    c.execute('BEGIN')
    return c


def _uint(value, ceiling=2**63):
    return type(value) is int and 0<=value<ceiling


def _plan_structure(plan, mint, source_hash):
    fields={'kind','mint','F','U','pool_indices','ownership_indices','discovery_revision_hash',
            'discovery_query_hashes','discovery_evidence_hashes'}
    need(type(plan) is dict and set(plan)==fields and plan['kind']=='common_bank_plan_source_discovery_v1'
         and plan['mint']==mint and plan['discovery_revision_hash']==source_hash,'INVENTORY_PLAN_VERSION_OR_BINDING')
    frontier=plan['F'];union=plan['U']
    need(type(frontier) is list and 1<=len(frontier)<=99 and all(type(a) is str for a in frontier)
         and frontier==sorted(set(frontier)) and type(union) is list and len(union)<=100,
         'INVENTORY_PLAN_COLLECTION')
    pool=capture.canonical_accounts(mint)
    for account in frontier:capture.address(account)
    expected=pool+sorted(set(frontier)-set(pool))
    need(pool[4] in frontier and not set(frontier).intersection(pool[:4]+pool[5:])
         and mint not in frontier and union==expected,'INVENTORY_PLAN_LAYOUT')
    need(canonical(plan['pool_indices'])==canonical(list(range(6)))
         and canonical(plan['ownership_indices'])==canonical([union.index(mint)]+[union.index(a) for a in frontier]),
         'INVENTORY_PLAN_INDICES')
    for field,limit in (('discovery_query_hashes',18),('discovery_evidence_hashes',128)):
        values=plan[field]
        need(type(values) is list and 1<=len(values)<=limit and all(ledger._hash(v) for v in values)
             and values==sorted(values),'INVENTORY_PLAN_REFERENCE_TYPES')
    need(len(plan['discovery_evidence_hashes'])==len(set(plan['discovery_evidence_hashes'])),
         'INVENTORY_PLAN_REFERENCE_DUPLICATE')


def _attachment_structure(event, intent, request, response, failure, stage, records, source):
    common={'kind','capture_id','budget_id','stage','ordinal','state','reason','intent_hash','fence',
            'descriptor_hash','plan_hash','planned_source','used_after','reserved_at','completed_at',
            'request_sha256','response_sha256','failure_hash','eligible_for_trading','ownership_approval','chain_authenticated'}
    extra={'genesis':set(),'slot':{'slot'},'union':{'request_floor','slot'},
           'clock':{'slot','block_time','bank_captured_at'}}[stage]
    need(set(event)==common|extra and _uint(event['completed_at'])
         and event['completed_at']>=intent['reserved_at']
         and type(event['reserved_at']) is int and event['reserved_at']==intent['reserved_at']
         and event['fence']==intent['fence'] and event['planned_source']==source
         and event['intent_hash']==digest({'previous_hash':intent['fence'],'event':intent})
         and all(event[name] is False for name in ('eligible_for_trading','ownership_approval','chain_authenticated')),
         'INVENTORY_ATTACHMENT_LOCAL_FIELDS')
    expected_request=canonical({'jsonrpc':'2.0','id':digest(intent),'method':intent['method'],'params':intent['params']}).encode()
    limit=journal.MAX_UNION_REQUEST_BYTES if stage=='union' else journal.MAX_REQUEST_BYTES
    need(type(request) is bytes and request==expected_request and len(request)<=limit,
         'INVENTORY_ATTACHMENT_ORIGINAL_REQUEST')
    need((type(response) is bytes and len(response)<=journal.MAX_RESPONSE_BYTES and failure is None)
         or (response is None and type(failure) is str and len(failure.encode())<=journal.MAX_FAILURE_BYTES),
         'INVENTORY_ATTACHMENT_EXCLUSIVE_OUTCOME')
    success={'genesis':'EXACT_GENESIS','slot':'FINALIZED_SLOT_DECLARED','union':'UNION_RAW_SHAPE_VALID','clock':'BLOCK_TIME_DECLARED'}
    errors={'RESPONSE_INVALID','RPC_ERROR','RESPONSE_OVERSIZED'}|set(journal.FAILURE_CATEGORIES)|{
        'genesis':{'GENESIS_MISMATCH'},'slot':{'SLOT_INVALID'},'clock':{'CLOCK_TIME_INVALID'},
        'union':{'UNION_RESULT_INVALID','UNION_CONTEXT_INVALID','UNION_CARDINALITY_INVALID','UNION_PARENT_BOUND',
                 'UNION_REQUIRED_ACCOUNT_ABSENT','UNION_ACCOUNT_OR_RECORD_INVALID'}}[stage]
    need(event['reason']==success[stage] if event['state']=='DONE' else event['reason'] in errors,
         'INVENTORY_ATTACHMENT_REASON')
    if failure is not None:
        f=_json(failure);category=f.get('category')
        need(category in journal.FAILURE_CATEGORIES or category=='RESPONSE_OVERSIZED','INVENTORY_FAILURE_CATEGORY')
        expected={'kind':f'common_bank_{stage}_redacted_failure_v1' if stage in ('union','clock') else 'common_bank_redacted_failure_v1',
                  'category':category,'method':intent['method']}
        if stage in ('union','clock'):expected['request_sha256']=hashlib.sha256(request).hexdigest()
        else:expected['params']=intent['params']
        if category=='RESPONSE_OVERSIZED':
            need(_uint(f.get('observed_bytes')) and f['observed_bytes']>journal.MAX_RESPONSE_BYTES,'INVENTORY_FAILURE_SIZE')
            expected['observed_bytes']=f['observed_bytes']
        need(f==expected and event['state']=='FAILED' and event['reason']==category,'INVENTORY_FAILURE_LOCAL_FIELDS')
    if stage in ('slot','union'):
        need((_uint(event['slot'],2**64 if stage=='slot' else 2**63) if event['state']=='DONE' else event['slot'] is None),
             'INVENTORY_ATTACHMENT_SLOT_TYPE')
    if stage=='union':
        floor=records['slot']['slot']
        need(type(event['request_floor']) is int and event['request_floor']==floor
             and (event['state']!='DONE' or event['slot']>=floor),'INVENTORY_ATTACHMENT_FLOOR')
    if stage=='clock':
        union=records['union']
        need(type(event['slot']) is int and event['slot']==union['slot']
             and type(event['bank_captured_at']) is int and event['bank_captured_at']==union['reserved_at']
             and (_uint(event['block_time']) if event['state']=='DONE' else event['block_time'] is None),
             'INVENTORY_ATTACHMENT_CLOCK_FIELDS')


def _journal_structure(tables, budgets, jd):
    runs = {}
    admissions={row[0]:row for row in tables['ownership_admissions']}
    for cap,budget,raw,plan,plan_hash,seed in tables.get('common_bank_runs',()):
        d = _json(raw); p = _json(plan)
        need(type(d) is dict and set(d)=={'kind','capture_id','budget_id','admission_descriptor_hash',
             'completed_source_hash','initial_used','planned_source','database_binding'}
             and ledger._identity(cap) and ledger._identity(budget) and ledger._hash(plan_hash) and ledger._hash(seed),
             'INVENTORY_RUN_LOCAL_FIELDS')
        need(budget in budgets and budget in admissions and d.get('kind')=='common_bank_run_v1'
             and d.get('capture_id')==cap and d.get('budget_id')==budget
             and d.get('admission_descriptor_hash')==budgets[budget][1]
             and admissions[budget][2]=='SEALED' and d.get('completed_source_hash')==admissions[budget][5]
             and d.get('planned_source')==jd['planned_source']
             and d.get('database_binding')==jd and digest(p)==plan_hash
             and digest({'descriptor':d,'plan_hash':plan_hash})==seed, 'INVENTORY_RUN_BINDING')
        need(type(d.get('initial_used')) is int and admissions[budget][4]<=d['initial_used']<=budgets[budget][2],
             'INVENTORY_RUN_COUNTER')
        _plan_structure(p,_json(admissions[budget][1])['mint'],admissions[budget][5])
        runs[cap]=(budget,seed,d,plan_hash)
    need(len(runs)<=journal.MAX_RUNS,'INVENTORY_RUN_BOUND')
    predecessors={cap:run[1] for cap,run in runs.items()}
    charges={cap:run[2]['initial_used'] for cap,run in runs.items()}
    ordinals={cap:0 for cap in runs};states={cap:None for cap in runs}
    records={cap:{} for cap in runs};intents={cap:None for cap in runs}
    names=('common_bank_events','common_bank_attachments','common_bank_slot_intents',
           'common_bank_slot_attachments','common_bank_union_intents','common_bank_union_attachments',
           'common_bank_clock_intents','common_bank_clock_attachments')
    for ordinal,name in enumerate(names,1):
        seen=set()
        for row in tables.get(name,()):
            cap=row[0]; need(cap in runs and cap not in seen,'INVENTORY_STAGE_IDENTITY');seen.add(cap)
            if name=='common_bank_events':
                need(row[1]==1,'INVENTORY_STAGE_ORDINAL'); raw,prior,key=row[2:]
            else:raw,prior,key=row[1:4]
            event=_json(raw); budget,seed,d,plan_hash=runs[cap]
            stage=('genesis','slot','union','clock')[(ordinal-1)//2]
            kind=(('common_bank_pending_v1' if ordinal==1 else f'common_bank_{stage}_pending_v1')
                  if ordinal%2 else f'common_bank_{stage}_attachment_v1')
            need(type(event) is dict and event.get('capture_id')==cap and event.get('budget_id')==budget
                 and event.get('kind')==kind and event.get('stage')==stage and type(event.get('ordinal')) is int
                 and event['ordinal']==ordinal
                 and ordinals[cap]==ordinal-1
                 and event.get('descriptor_hash')==digest(d) and event.get('plan_hash')==plan_hash
                 and prior==predecessors[cap] and key==digest({'previous_hash':prior,'event':event}),
                 'INVENTORY_STAGE_CHAIN')
            used=event.get('used_after')
            need(type(used) is int and charges[cap]<=used<=budgets[budget][2],'INVENTORY_STAGE_COUNTER')
            if name.endswith('intents') or name=='common_bank_events':
                need(event.get('state')=='PENDING' and used>charges[cap],'INVENTORY_INTENT_CHARGE')
                need(event.get('method')==('getGenesisHash','getSlot','getMultipleAccounts','getBlockTime')[(ordinal-1)//2],
                     'INVENTORY_STAGE_METHOD')
                need(ordinal==1 or states[cap]=='DONE','INVENTORY_STAGE_PREDECESSOR')
                at=event.get('reserved_at')
                need(_uint(at) and (ordinal==1 or at>=records[cap][('genesis','slot','union','clock')[(ordinal-3)//2]]['completed_at']),
                     'INVENTORY_INTENT_TIME')
                params=[] if stage=='genesis' else [{'commitment':'finalized'}]
                if stage=='union':
                    floor=records[cap]['slot']['slot'];need(_uint(floor),'INVENTORY_UNION_FLOOR_TYPE')
                    plan=_json(next(row[3] for row in tables['common_bank_runs'] if row[0]==cap))
                    params=[plan['U'],{'encoding':'base64','commitment':'finalized','minContextSlot':floor}]
                elif stage=='clock':params=[records[cap]['union']['slot']]
                expected={'kind':kind,'capture_id':cap,'budget_id':budget,'stage':stage,'state':'PENDING',
                          'method':event['method'],'params':params,'descriptor_hash':digest(d),'plan_hash':plan_hash,
                          'fence':prior,'ordinal':ordinal,'used_after':used,'reserved_at':at}
                need(set(event)==set(expected) and canonical(event)==canonical(expected),'INVENTORY_INTENT_LOCAL_FIELDS')
                intents[cap]=event
                charges[cap]=used
            else:
                need(used==charges[cap] and event.get('state') in ('DONE','FAILED'),'INVENTORY_ATTACHMENT_STATE')
                request,response,failure=row[4:]
                need(type(request) is bytes and len(request)<=journal.MAX_UNION_REQUEST_BYTES
                     and (response is None or type(response) is bytes and len(response)<=journal.MAX_RESPONSE_BYTES)
                     and (failure is None or type(failure) is str and len(failure.encode())<=journal.MAX_FAILURE_BYTES),
                     'INVENTORY_ATTACHMENT_BOUND')
                need(event.get('request_sha256')==hashlib.sha256(request).hexdigest()
                     and event.get('response_sha256')==(hashlib.sha256(response).hexdigest() if response is not None else None)
                     and event.get('failure_hash')==(digest(_json(failure)) if failure is not None else None),
                     'INVENTORY_ATTACHMENT_HASH')
                _attachment_structure(event,intents[cap],request,response,failure,stage,records[cap],d['planned_source'])
                records[cap][stage]=event
            predecessors[cap]=key
            ordinals[cap]=ordinal;states[cap]=event['state']
    return states


def _legacy_structure(tables, budgets, desc):
    runs={};admissions={row[0]:row for row in tables['ownership_admissions']}
    for seq,cap,body,prior,key in tables.get('pool_capture_events',()):
        event=_json(body)
        need(capture.identity(cap),'INVENTORY_LEGACY_CAPTURE_ID')
        if cap not in runs:
            need(set(event)=={'descriptor'},'INVENTORY_LEGACY_DESCRIPTOR')
            d=event['descriptor'];inv=d.get('investigation')
            need(set(d)=={'investigation','pool','mint','source','profile'} and d['profile']==ledger.PROFILE
                 and d['source'] in desc['sources'] and type(inv) is dict
                 and set(inv)=={'scan_id','descriptor_hash','mint'},'INVENTORY_LEGACY_VERSION_OR_SOURCE')
            identity=inv['scan_id']
            need(identity in budgets and identity in admissions and inv['descriptor_hash']==budgets[identity][1]
                 and inv['mint']==d['mint'] and _json(admissions[identity][1])['mint']==d['mint']
                 and d['pool']==capture.canonical_accounts(d['mint'])[0],
                 'INVENTORY_LEGACY_INVESTIGATION_BINDING')
            runs[cap]=[0,None]
        else:
            stage,state=runs[cap]
            need(set(event)=={'stage','state','record','captured_at'} and stage<4
                 and event['stage']==('genesis','slot','snapshot','time')[stage]
                 and _uint(event['captured_at']),'INVENTORY_LEGACY_STAGE')
            if event['state']=='PENDING':
                need(state is None and event['record'] is None,'INVENTORY_LEGACY_PENDING')
                runs[cap][1]='PENDING'
            else:
                need(state=='PENDING' and event['state'] in ('DONE','FAILED') and ledger._hash(event['record']),
                     'INVENTORY_LEGACY_COMPLETION')
                runs[cap]=[stage+1,None] if event['state']=='DONE' else [stage,'FAILED']
    return runs


def _inspect(pins, connections, schemas):
    e, l, r = connections; es, ls, rs = schemas
    desc = _json(pins.ledger_descriptor); jd = _json(pins.journal_descriptor)
    need(type(desc) is dict and set(desc)=={'version','ledger_id','profile','root','root_identity','ledger_path',
         'ledger_identity','lock_path','lock_identity','evidence_path','evidence_identity','sources',
         'allow_synthetic_fixtures','max_age_seconds'} and type(desc.get('version')) is int
         and desc['version']==1 and desc.get('profile') == ledger.PROFILE, 'INVENTORY_LEDGER_VERSION')
    need(ledger._identity(desc['ledger_id']) and type(desc['allow_synthetic_fixtures']) is bool
         and type(desc['max_age_seconds']) is int and 1<=desc['max_age_seconds']<=300
         and type(desc['sources']) is list and 1<=len(desc['sources'])<=256,'INVENTORY_LEDGER_LOCAL_FIELDS')
    ids=[]
    for source in desc['sources']:
        need(type(source) is dict and set(source)==set(ledger.ApprovedSource.__dataclass_fields__)
             and ledger._identity(source['source_id']) and source['source_kind'] in ('coordinator_capture','synthetic_fixture')
             and (source['network'],source['genesis_hash'],source['profile'])==(ledger.NETWORK,ledger.GENESIS,ledger.PROFILE)
             and (source['source_kind']!='synthetic_fixture' or desc['allow_synthetic_fixtures']),
             'INVENTORY_LEDGER_SOURCE_FIELDS')
        ids.append(source['source_id'])
    need(ids==sorted(set(ids)),'INVENTORY_LEDGER_SOURCE_IDENTITIES')
    for field in ('root_identity','ledger_identity','evidence_identity','lock_identity'):
        identity=desc[field]
        need(type(identity) is list and len(identity)==(3 if field=='lock_identity' else 2)
             and all(_uint(value,2**64) for value in identity),'INVENTORY_DESCRIPTOR_IDENTITY_TYPES')
    need(jd.get('kind') == 'common_bank_journal_v1' and jd.get('version') == 1,
         'INVENTORY_JOURNAL_VERSION')
    need(set(jd)=={'kind','version','planned_source','research_path','research_identity','evidence_path','evidence_identity'}
         and type(jd['version']) is int and type(jd['planned_source']) is dict
         and set(jd['planned_source'])==set(ledger.ApprovedSource.__dataclass_fields__), 'INVENTORY_JOURNAL_LOCAL_FIELDS')
    for key in ('research_identity','evidence_identity'):
        need(type(jd[key]) is list and len(jd[key])==2 and all(_uint(value,2**64) for value in jd[key]),
             'INVENTORY_JOURNAL_IDENTITY_TYPES')
    source=jd['planned_source']
    need(ledger._identity(source['source_id']) and source['source_kind'] in ('coordinator_capture','synthetic_fixture')
         and (source['network'],source['genesis_hash'],source['profile'])==(ledger.NETWORK,ledger.GENESIS,ledger.PROFILE),
         'INVENTORY_JOURNAL_SOURCE_FIELDS')
    version = e.execute('PRAGMA user_version').fetchone()[0]
    families = {0: journal.SCHEMA,
        journal.ATTACHMENT_VERSION: journal.SCHEMA | journal.ATTACHMENT_SCHEMA,
        journal.SLOT_VERSION: journal.SCHEMA | journal.ATTACHMENT_SCHEMA | journal.SLOT_SCHEMA,
        journal.UNION_VERSION: journal.SCHEMA | journal.ATTACHMENT_SCHEMA | journal.SLOT_SCHEMA | journal.UNION_SCHEMA,
        journal.CLOCK_VERSION: journal.SCHEMA | journal.ATTACHMENT_SCHEMA | journal.SLOT_SCHEMA | journal.UNION_SCHEMA | journal.CLOCK_SCHEMA}
    actual_common = any(x[1].startswith('common_bank_') for x in es)
    if actual_common:
        need(version in families and e.execute('PRAGMA application_id').fetchone()[0] == journal.APPLICATION_ID,
             'INVENTORY_JOURNAL_VERSION')
        _family(es, families[version], 'common_bank_')
    else:
        need(version == 0 and e.execute('PRAGMA application_id').fetchone()[0] == 0,
             'INVENTORY_ABSENT_JOURNAL_MARKER')
    actual_capture = any(x[1].startswith('pool_capture_') for x in es)
    _family(es, capture.SCHEMA if actual_capture else {}, 'pool_capture_')
    format2 = any(x[1] == 'receipt_transition_certificate' for x in ls)
    if format2:
        need(ls == EXPECTED_INVENTORY and pins.chain_anchor is not None, 'INVENTORY_FORMAT2_PIN')
    else:
        need(ls==_objects(ledger.SCHEMA,{'coordinator_receipts':(1,2)}),'INVENTORY_LEDGER_SCHEMA')
    for name in BASE_TABLE_SQL:
        schema=rs if name=='scans' else es
        wanted=_objects({name:BASE_TABLE_SQL[name]},{name:(1,)})
        need(tuple(x for x in schema if x[1] in {y[1] for y in wanted} or x[2]==name)==wanted,
             'INVENTORY_BASE_TYPED_SCHEMA')
    for name, expected in journal.EXISTING_COLUMNS.items():
        columns = e.execute(f'PRAGMA table_info({name})').fetchall()
        need([(x[1],x[2],x[3],x[5]) for x in columns] == expected and all(x[4] is None for x in columns),
             'INVENTORY_ADMISSION_SCHEMA')
    need(not any(x[0]=='trigger' and x[2] in journal.EXISTING_COLUMNS for x in es),
         'INVENTORY_UNSUPPORTED_ADMISSION_TRIGGER')
    selected=(families[version] if actual_common else {}) | (capture.SCHEMA if actual_capture else {})
    names=['pages','ownership_budgets','ownership_admissions']+[name for name,sql in selected.items() if sql.startswith('CREATE TABLE ')]
    budget = [0,0]
    # All three preflights finish before any row body is loaded.
    ledger_sql=FORMAT_SQL if format2 else ledger.SCHEMA
    specs = (_read_tables(e,es,names,budget), _read_tables(l,ls,[name for name,sql in ledger_sql.items() if sql.startswith('CREATE TABLE ')],budget),
             _read_tables(r,rs,['scans'],budget))
    tables = tuple(_load(c,s) for c,s in zip(connections,specs))
    et,lt,rt = tables
    need(lt['ledger_descriptor'] == ((1,pins.ledger_descriptor,digest(desc)),)
         and lt['ledger_head'] == ((1,*pins.ledger_head),), 'INVENTORY_LEDGER_PIN_MISMATCH')
    if actual_common: need(et['common_bank_meta'] == ((1,pins.journal_descriptor),), 'INVENTORY_JOURNAL_PIN_MISMATCH')
    parent=journal.SCHEMA
    for table,kind,addition in (
        ('common_bank_attachment_meta','common_bank_genesis_attachment_profile_v1',journal.ATTACHMENT_SCHEMA),
        ('common_bank_slot_meta','common_bank_slot_profile_v1',journal.SLOT_SCHEMA),
        ('common_bank_union_meta','common_bank_union_profile_v1',journal.UNION_SCHEMA),
        ('common_bank_clock_meta','common_bank_clock_profile_v1',journal.CLOCK_SCHEMA)):
        if table in et:
            expected={'kind':kind,'database_binding':jd,'parent_schema_hash':digest(parent)}
            if table in ('common_bank_union_meta','common_bank_clock_meta'):
                expected.update(request_limit=journal.MAX_UNION_REQUEST_BYTES if table=='common_bank_union_meta' else journal.MAX_REQUEST_BYTES,
                                response_limit=journal.MAX_RECORD_BYTES)
            need(et[table]==((1,canonical(expected)),),'INVENTORY_PROFILE_METADATA')
            parent=parent|addition
    if actual_capture:
        config = {'version':1,'ledger':desc,'evidence_identity':desc['evidence_identity']}
        need(et['pool_capture_config'] == ((1,canonical(config)),)
             and et['pool_capture_head'] == ((1,*pins.capture_head),), 'INVENTORY_CAPTURE_PIN_MISMATCH')
        prior = digest(config)
        for i,(seq,cap,body,previous,key) in enumerate(et['pool_capture_events'],1):
            event = _json(body)
            need(seq == i and previous == prior and key == digest({'seq':seq,'capture':cap,'event':event,'previous':prior}), 'INVENTORY_CAPTURE_CHAIN')
            prior = key
        need(pins.capture_head == (len(et['pool_capture_events']),prior), 'INVENTORY_CAPTURE_HEAD')
    else: need(pins.capture_head is None, 'INVENTORY_CAPTURE_ABSENCE_PIN')
    if format2:
        cert = lt['receipt_transition_certificate']; need(len(cert)==1 and cert[0][0]==1,'INVENTORY_CERTIFICATE')
        validate_receipt_chain(ChainSnapshot(pins.ledger_descriptor,digest(desc),cert[0][1],cert[0][2],pins.ledger_head,lt['coordinator_receipts']),pins.chain_anchor)
    else:
        previous = digest(desc)
        for i,row in enumerate(lt['coordinator_receipts'],1):
            seq,pub,pool,mint,slot,source,body,key,prior,h = row
            receipt = ledger._decode(body, _boundary_from_desc(desc))
            need(seq == i and prior == previous and digest(_json(body)) == key
                 and (pool,mint,slot,source)==(receipt.pool,receipt.mint,str(receipt.slot),receipt.source_id)
                 and h==digest({'seq':seq,'publication_id':pub,'payload_hash':key,'previous_hash':prior}), 'INVENTORY_PUBLICATION_CHAIN')
            previous=h
        need(pins.ledger_head==(len(lt['coordinator_receipts']),previous),'INVENTORY_PUBLICATION_HEAD')
    blockers = {'OFFLINE_EXCLUSIVE_CONTROL_REQUIRED','RAW_SEMANTIC_REPLAY_REQUIRED','COMPLETION_NOT_AUTHENTICATED'}
    if not actual_common: blockers.add('COMMON_BANK_JOURNAL_NOT_INSTALLED')
    budgets = {row[0]:row for row in et['ownership_budgets']}
    for identity,source,used,ceiling in budgets.values():
        need(type(used) is int and type(ceiling) is int and 0<=used<=ceiling<=18,'INVENTORY_COUNTER_INVALID')
    for row in et['ownership_admissions']:
        need(row[0] in budgets, 'INVENTORY_ORPHAN_ADMISSION')
        need(row[2] in ('ADMITTED','PREPARED','SEALED'),'INVENTORY_ADMISSION_STATE')
        HistoryProgress.inspect_admission(e,row[0])
        if row[2] != 'SEALED': blockers.add('UNSEALED_ADMISSION')
        else:
            saved=next((scan for scan in rt['scans'] if scan[0]==row[0]),None)
            prepared=_json(row[3])
            if saved is None or dict(zip(('id','mint','created','status','result'),saved))!=prepared:
                blockers.add('ORIGINAL_SCAN_SOURCE_MISMATCH_OR_MISSING')
    states=_journal_structure(et,budgets,jd)
    legacy_states=_legacy_structure(et,budgets,desc)
    if any(stage!=4 or state is not None for stage,state in legacy_states.values()):
        blockers.add('UNRESOLVED_LEGACY_CAPTURE')
    if any(state=='PENDING' for state in states.values()):blockers.add('PENDING_OR_AMBIGUOUS_ATTEMPT')
    if any(state is None for state in states.values()):blockers.add('UNSTARTED_JOURNAL_RUN')
    page_keys={row[0] for row in et['pages']}
    for row in et.get('pool_capture_events',()):
        event=_json(row[2]);ref=event.get('record')
        if ref is not None and ref not in page_keys:blockers.add('LEGACY_RAW_RECORD_MISSING')
        if event.get('state')=='PENDING':blockers.add('LEGACY_CAPTURE_REQUIRES_COMPLETION_AUDIT')
    for name,rows in et.items():
        if name.endswith(('events','intents','attachments')):
            index = 2 if name in ('common_bank_events','pool_capture_events') else 1
            for row in rows:
                body = _json(row[index]); state = body.get('state')
                if state == 'PENDING': blockers.add('ORIGINAL_PENDING_RECORD_RETAINED')
                elif state == 'FAILED': blockers.add('FAILED_ATTEMPT_RETAINED')
                elif state not in ('DONE',None): blockers.add('UNKNOWN_STAGE_STATE')
        if name == 'common_bank_runs':
            for row in rows:
                need(row[1] in budgets,'INVENTORY_ORPHAN_RUN')
                blockers.add('JOURNAL_RUN_REQUIRES_COMPLETION_AUDIT')
    return tables, tuple(sorted(blockers))


def _boundary_from_desc(desc):
    return ledger.CoordinatorBoundary(desc['ledger_id'],Path(desc['root']),Path(desc['ledger_path']),
        Path(desc['evidence_path']),frozenset(ledger.ApprovedSource(**s) for s in desc['sources']),
        desc['allow_synthetic_fixtures'],desc['max_age_seconds'])


def read_completion_inventory(pins):
    """One complete guarded multi-file window, no source/profile filtering.

    Pins identify local originals, not remote truth. All output is diagnostic.
    An unsupported inventory raises; no subset is returned.
    """
    if not read_platform_available(): raise InventoryUnavailable('INVENTORY_PLATFORM_UNAVAILABLE')
    try:
        need(type(pins) is InventoryPins, 'INVENTORY_EXTERNAL_PINS_REQUIRED')
        for head,nullable in ((pins.ledger_head,False),(pins.capture_head,True)):
            need(nullable and head is None or type(head) is tuple and len(head)==2
                 and type(head[0]) is int and 0<=head[0]<=MAX_ROWS and ledger._hash(head[1]),
                 'INVENTORY_HEAD_PIN_TYPE')
        desc = _json(pins.ledger_descriptor); jd = _json(pins.journal_descriptor)
        root = Path(desc['root']); evidence = Path(desc['evidence_path']); path = Path(desc['ledger_path'])
        research = Path(pins.research_path); lock = Path(desc['lock_path'])
        need(path.parent==root and lock==path.with_name(path.name+'.coordinator.lock')
             and len({path,evidence,research,lock})==4,'INVENTORY_DISTINCT_PATHS')
        need(jd['research_path']==str(research) and jd['research_identity']==list(pins.research_identity)
             and jd['evidence_path']==str(evidence) and jd['evidence_identity']==desc['evidence_identity'], 'INVENTORY_DATABASE_BINDING')
        deadline=time.monotonic()+MAX_SECONDS
        with ExitStack() as files:
            held=[]
            for p,identity,directory in ((root,desc['root_identity'],True),(evidence,desc['evidence_identity'],False),
                    (path,desc['ledger_identity'],False),(research,pins.research_identity,False)):
                held.append(_held(files,p,identity,directory=directory))
            # Existing coordinator lock only; never create/recover sidecars.
            info=lock.lstat(); _stat_ok(info,private=True)
            fd=os.open(lock,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC);files.callback(os.close,fd)
            need([info.st_dev,info.st_ino,info.st_mtime_ns]==desc['lock_identity'],'INVENTORY_LOCK_PIN')
            held.append((lock,fd,False,True,_stamp(info)))
            for parent in {evidence.parent,research.parent}-{root}:
                info=parent.lstat();held.append(_held(files,parent,(info.st_dev,info.st_ino),directory=True))
            # Consistent with writer research-worker -> ownership -> ledger order:
            # OFD research guard first (no worker sidecar creation), then evidence,
            # coordinator shared lock, then ledger OFD. No nested public sessions.
            with read_guard(research), read_guard(evidence):
                _stable(held); fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
                try:
                    with read_guard(path), ExitStack() as connections:
                        cs=tuple(_connection(connections,p,deadline) for p in (evidence,path,research))
                        schemas=tuple(_schema(c) for c in cs)
                        if any(x[1]=='receipt_transition_certificate' for x in schemas[1]):
                            validate_header(os.pread(held[2][1],100,0),os.fstat(held[2][1]).st_size)
                        tables,blockers=_inspect(pins,cs,schemas)
                        _stable(held)
                        need(time.monotonic()<=deadline,'INVENTORY_DEADLINE')
                        for c in cs:c.execute('ROLLBACK')
                    _stable(held)
                finally:fcntl.flock(fd,fcntl.LOCK_UN)
            _stable(held)
        # Hash exact bytes (attachments are not converted into synthetic RPC).
        image=tuple((i,name,rows) for i,t in enumerate(tables) for name,rows in sorted(t.items()))
        h=hashlib.sha256()
        h.update(canonical(schemas).encode())
        identities=tuple((str(p),stamp) for p,_,_,_,stamp in held)
        h.update(canonical([pins.ledger_descriptor,pins.journal_descriptor,pins.ledger_head,
                            pins.capture_head,identities]).encode())
        for db,name,rows in image:
            h.update(canonical([db,name]).encode())
            for row in rows:
                for value in row:
                    raw=value if type(value) is bytes else canonical(value).encode()
                    h.update((b'B' if type(value) is bytes else b'J')+len(raw).to_bytes(8,'big')+raw)
        need(time.monotonic()<=deadline,'INVENTORY_DEADLINE')
        return CompletionInventory(image,schemas,identities,h.hexdigest(),blockers)
    except InventoryUnavailable:raise
    except (ValueError,TypeError,KeyError,IndexError,AttributeError,UnicodeError,RecursionError,OverflowError,sqlite3.Error,OSError):
        raise InventoryUnavailable('INVENTORY_UNAVAILABLE') from None
