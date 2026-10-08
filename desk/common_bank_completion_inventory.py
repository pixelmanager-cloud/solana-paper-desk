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
from pathlib import Path
import sqlite3
import time

from . import common_bank_journal as journal
from . import pool_capture_bridge as capture
from . import pool_receipt_ledger as ledger
from .common_bank_receipt_chain import ChainSnapshot, validate_receipt_chain
from .common_bank_receipt_format import EXPECTED_INVENTORY, validate_header
from .common_bank_receipt_reader import _stat_ok, _stamp, _stable
from .control_obligations import read_guard, read_platform_available
from .model import canonical, digest
from .history_progress import HistoryProgress

MAX_ROWS = 10000
MAX_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_SECONDS = 10


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
    metrics = c.execute("SELECT count(*),coalesce(max(length(CAST(name AS BLOB))),0),coalesce(max(length(CAST(sql AS BLOB))),0),coalesce(sum(length(CAST(sql AS BLOB))),0) FROM sqlite_master").fetchone()
    need(all(type(x) is int for x in metrics) and metrics[0] <= 256 and metrics[1] <= 128
         and metrics[2] <= 8192 and metrics[3] <= 256*1024, 'INVENTORY_SCHEMA_BOUND')
    rows = tuple(c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchmany(257))
    need(all(type(r[0]) is str and type(r[1]) is str and type(r[2]) is str
             and (r[3] is None or type(r[3]) is str) for r in rows), 'INVENTORY_SCHEMA_TYPES')
    return rows


def _family(schema, expected, prefix):
    actual = {r[1]: r[3] for r in schema if r[1].startswith(prefix)
              or (r[0] in ('trigger','index') and r[2].startswith(prefix) and r[3] is not None)}
    need(actual == expected, 'INVENTORY_UNSUPPORTED_OR_PARTIAL_SCHEMA')


def _read_tables(c, schema, names, budget):
    # SQL identifiers originate only in fixed module constants, never JSON.
    specs = []
    for name in names:
        if name=='pages':
            count,bad=c.execute("SELECT count(*),coalesce(sum(CASE WHEN typeof(hash)!='text' OR length(CAST(hash AS BLOB))!=64 OR typeof(payload)!='blob' OR typeof(raw_bytes)!='integer' OR raw_bytes<0 OR raw_bytes>16777216 THEN 1 ELSE 0 END),0) FROM pages").fetchone()
            need(bad==0,'INVENTORY_PAGE_CATALOG_TYPES')
            budget[0]+=count;budget[1]+=count*96
            need(budget[0]<=MAX_ROWS and budget[1]<=MAX_BYTES,'INVENTORY_SHARED_BOUND')
            specs.append((name,'hash',count));continue
        columns = c.execute(f'PRAGMA table_info({name})').fetchall()
        need(columns and len(columns) <= 16, 'INVENTORY_COLUMNS')
        for col in columns:
            key=col[1]
            limit = (2*1024*1024 if key in ('result','prepared_source') else
                     65536 if key in ('plan_json','response_bytes') else
                     16384 if key=='event_json' and name=='common_bank_union_intents' else
                     8192 if key in ('body','payload','descriptor','descriptor_json','event_json','request_bytes') else
                     2048 if key=='failure_json' else 128)
            expected={'TEXT':'text','INTEGER':'integer','BLOB':'blob'}[col[2]]
            nullable=not col[3] and not col[5]
            allowed=f"'{expected}'"+(",'null'" if nullable else '')
            need(c.execute(f'SELECT count(*) FROM "{name}" WHERE typeof("{key}") NOT IN ({allowed}) OR length(CAST("{key}" AS BLOB))>{limit}').fetchone()==(0,),
                 'INVENTORY_FIELD_PREFLIGHT')
        size = '+'.join(f'coalesce(length(CAST("{r[1]}" AS BLOB)),0)' for r in columns)
        invalid = ' OR '.join(f'(typeof("{r[1]}") NOT IN (\'null\',\'text\',\'integer\',\'blob\'))' for r in columns)
        count, total, maximum, bad = c.execute(f'SELECT count(*),coalesce(sum({size}),0),coalesce(max({size}),0),coalesce(sum(CASE WHEN {invalid} THEN 1 ELSE 0 END),0) FROM "{name}"').fetchone()
        need(all(type(v) is int and v >= 0 for v in (count,total,maximum,bad))
             and bad == 0 and maximum <= 2*1024*1024, 'INVENTORY_SCALAR_BOUND')
        if name.startswith('common_bank_') and not name.endswith('_meta'):
            need(count<=journal.MAX_RUNS,'INVENTORY_STAGE_CAPACITY')
        budget[0] += count; budget[1] += total
        need(budget[0] <= MAX_ROWS and budget[1] <= MAX_BYTES, 'INVENTORY_SHARED_BOUND')
        order = ','.join(f'"{r[1]}"' for r in columns if r[5]) or 'rowid'
        specs.append((name, order, count))
    return specs


def _load(c, specs):
    result = {}
    for name, order, count in specs:
        select='hash,raw_bytes,length(payload)' if name=='pages' else '*'
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


def _journal_structure(tables, budgets, jd):
    runs = {}
    admissions={row[0]:row for row in tables['ownership_admissions']}
    for cap,budget,raw,plan,plan_hash,seed in tables.get('common_bank_runs',()):
        d = _json(raw); p = _json(plan)
        need(type(p) is dict and p.get('kind')=='common_bank_plan_source_discovery_v1',
             'INVENTORY_PLAN_VERSION')
        need(budget in budgets and budget in admissions and d.get('kind')=='common_bank_run_v1'
             and d.get('capture_id')==cap and d.get('budget_id')==budget
             and d.get('admission_descriptor_hash')==budgets[budget][1]
             and admissions[budget][2]=='SEALED' and d.get('completed_source_hash')==admissions[budget][5]
             and d.get('planned_source')==jd['planned_source']
             and d.get('database_binding')==jd and digest(p)==plan_hash
             and digest({'descriptor':d,'plan_hash':plan_hash})==seed, 'INVENTORY_RUN_BINDING')
        need(type(d.get('initial_used')) is int and admissions[budget][4]<=d['initial_used']<=budgets[budget][2],
             'INVENTORY_RUN_COUNTER')
        runs[cap]=(budget,seed,d,plan_hash)
    need(len(runs)<=journal.MAX_RUNS,'INVENTORY_RUN_BOUND')
    predecessors={cap:run[1] for cap,run in runs.items()}
    charges={cap:run[2]['initial_used'] for cap,run in runs.items()}
    ordinals={cap:0 for cap in runs};states={cap:None for cap in runs}
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
                 and event.get('kind')==kind and event.get('stage')==stage and event.get('ordinal')==ordinal
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
            predecessors[cap]=key
            ordinals[cap]=ordinal;states[cap]=event['state']
    return states


def _legacy_structure(tables, budgets, desc):
    runs={};admissions={row[0]:row for row in tables['ownership_admissions']}
    for seq,cap,body,prior,key in tables.get('pool_capture_events',()):
        event=_json(body)
        if cap not in runs:
            need(set(event)=={'descriptor'},'INVENTORY_LEGACY_DESCRIPTOR')
            d=event['descriptor'];inv=d.get('investigation')
            need(set(d)=={'investigation','pool','mint','source','profile'} and d['profile']==ledger.PROFILE
                 and d['source'] in desc['sources'] and type(inv) is dict
                 and set(inv)=={'scan_id','descriptor_hash','mint'},'INVENTORY_LEGACY_VERSION_OR_SOURCE')
            identity=inv['scan_id']
            need(identity in budgets and identity in admissions and inv['descriptor_hash']==budgets[identity][1]
                 and inv['mint']==d['mint'] and _json(admissions[identity][1])['mint']==d['mint'],
                 'INVENTORY_LEGACY_INVESTIGATION_BINDING')
            runs[cap]=[0,None]
        else:
            stage,state=runs[cap]
            need(set(event)=={'stage','state','record','captured_at'} and stage<4
                 and event['stage']==('genesis','slot','snapshot','time')[stage]
                 and type(event['captured_at']) is int,'INVENTORY_LEGACY_STAGE')
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
    need(desc.get('version') == 1 and desc.get('profile') == ledger.PROFILE, 'INVENTORY_LEDGER_VERSION')
    need(jd.get('kind') == 'common_bank_journal_v1' and jd.get('version') == 1,
         'INVENTORY_JOURNAL_VERSION')
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
        need({x[1]:x[3] for x in ls if x[3] is not None} == ledger.SCHEMA
             and {x[1] for x in ls if x[3] is None} == {'sqlite_autoindex_coordinator_receipts_1','sqlite_autoindex_coordinator_receipts_2'},
             'INVENTORY_LEDGER_SCHEMA')
    need([(x[1],x[2]) for x in r.execute('PRAGMA table_info(scans)')] ==
         [('id','TEXT'),('mint','TEXT'),('created','INTEGER'),('status','TEXT'),('result','TEXT')],
         'INVENTORY_RESEARCH_SCHEMA')
    need(any(x[:3]==('table','scans','scans') for x in rs),'INVENTORY_RESEARCH_TABLE_REQUIRED')
    for name, expected in journal.EXISTING_COLUMNS.items():
        columns = e.execute(f'PRAGMA table_info({name})').fetchall()
        need([(x[1],x[2],x[3],x[5]) for x in columns] == expected and all(x[4] is None for x in columns),
             'INVENTORY_ADMISSION_SCHEMA')
    need(not any(x[0]=='trigger' and x[2] in journal.EXISTING_COLUMNS for x in es),
         'INVENTORY_UNSUPPORTED_ADMISSION_TRIGGER')
    names = [x[1] for x in es if x[0] == 'table' and (x[1].startswith(('common_bank_','pool_capture_'))
              or x[1] in ('ownership_budgets','ownership_admissions','pages'))]
    budget = [0,0]
    # All three preflights finish before any row body is loaded.
    specs = (_read_tables(e,es,names,budget), _read_tables(l,ls,[x[1] for x in ls if x[0]=='table'],budget),
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
