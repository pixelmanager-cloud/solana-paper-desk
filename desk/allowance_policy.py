"""Explicit immutable allowance records; readers never install or infer upgrades."""
import json
import re
from .model import canonical, digest

PROVENANCE = 'USER_AUTHORIZED_CONTINUOUS_SCANNING_V1'
RESEARCH = 'research_allowance_upgrade'
MONITORING = 'paper_monitoring_allowance_upgrade'
MARKER = 'continuous_scanning_allowance_v1'
NEW_DAILY = 1000
NEW_QUEUE = 25
NEW_MONITORING = 3600


def schema(prefix):
    table=f'CREATE TABLE {prefix}(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL,body_hash TEXT NOT NULL)'
    result={prefix:('table',prefix,table)}
    for action in ('UPDATE','DELETE'):
        name=prefix+'_'+action.lower()
        result[name]=('trigger',prefix,f"CREATE TRIGGER {name} BEFORE {action} ON {prefix} BEGIN SELECT RAISE(ABORT,'Immutable allowance provenance'); END")
    name=prefix+'_insert'
    result[name]=('trigger',prefix,f"CREATE TRIGGER {name} BEFORE INSERT ON {prefix} WHEN EXISTS(SELECT 1 FROM {prefix}) BEGIN SELECT RAISE(ABORT,'Allowance already activated'); END")
    return result


def read(c,prefix):
    expected=schema(prefix)
    # Full typed identities/cardinality; a table/trigger name collision cannot
    # overwrite another object in a name-keyed dictionary.
    rows=c.execute("SELECT name,type,tbl_name,sql FROM sqlite_master WHERE name=? OR substr(name,1,?)=?",
                   (prefix,len(prefix)+1,prefix+'_')).fetchall()
    if not rows:return None
    wanted={(name,*item) for name,item in expected.items()}
    if len(rows)!=len(wanted) or {tuple(row) for row in rows}!=wanted:
        raise ValueError('ALLOWANCE_SCHEMA_INVALID')
    row=c.execute(f'SELECT id,typeof(body),length(CAST(body AS BLOB)),typeof(body_hash),length(body_hash) FROM {prefix}').fetchall()
    if len(row)!=1 or tuple(row[0])[:2]!=(1,'text') or not 1<=row[0][2]<=8192 or tuple(row[0])[3:]!=('text',64):
        raise ValueError('ALLOWANCE_RECORD_INVALID')
    raw,key=c.execute(f'SELECT body,body_hash FROM {prefix} WHERE id=1').fetchone()
    body=json.loads(raw)
    if type(body) is not dict or canonical(body)!=raw or digest(body)!=key:
        raise ValueError('ALLOWANCE_RECORD_INVALID')
    return body,key


def install(c,prefix,body):
    old=read(c,prefix)
    if old:
        if old[0]!=body:raise ValueError('ALLOWANCE_ALREADY_BOUND')
        return old[1]
    for _,_,sql in schema(prefix).values():c.execute(sql)
    key=digest(body);c.execute(f'INSERT INTO {prefix} VALUES(1,?,?)',(canonical(body),key))
    return key


def hash_value(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None


def research_limits(c,path):
    record=read(c,RESEARCH)
    marker=c.execute('SELECT version FROM scan_job_migrations WHERE name=?',(MARKER,)).fetchone()
    if record is None and marker is None:return 10,3
    if record is None or marker is None or tuple(marker)!=(1,):raise ValueError('ALLOWANCE_PUBLICATION_INCOMPLETE')
    body=record[0]
    if (set(body)!={'kind','research_db','daily','queued','previous_daily','previous_queued','at','provenance'}
            or body['kind']!='research_allowance_upgrade_v1' or body['research_db']!=str(path)
            or type(body['daily']) is not int or body['daily']!=NEW_DAILY
            or type(body['queued']) is not int or body['queued']!=NEW_QUEUE
            or type(body['previous_daily']) is not int or body['previous_daily']!=10
            or type(body['previous_queued']) is not int or body['previous_queued']!=3
            or type(body['at']) is not int or not 0<=body['at']<2**63 or body['provenance']!=PROVENANCE):
        raise ValueError('ALLOWANCE_POLICY_INVALID')
    return NEW_DAILY,NEW_QUEUE


def monitoring_policy(c):
    value=read(c,MONITORING)
    if value is None:return None
    body,key=value
    fields={'kind','ledger','evidence','config_hash','predecessor_code','successor_code','old_budget',
            'reservation_cutoff','cap','at','provenance'}
    if (set(body)!=fields or body['kind']!='monitoring_allowance_upgrade_v1'
            or body['provenance']!=PROVENANCE or type(body['cap']) is not int or body['cap']!=NEW_MONITORING
            or type(body['reservation_cutoff']) is not int or body['reservation_cutoff']<0
            or type(body['at']) not in (int,float) or not 0<=body['at']<2**63
            or any(not hash_value(body[k]) for k in ('config_hash','predecessor_code','successor_code'))
            or type(body['ledger']) is not str or type(body['evidence']) is not str
            or type(body['old_budget']) is not list or len(body['old_budget'])!=9):
        raise ValueError('ALLOWANCE_POLICY_INVALID')
    old=body['old_budget']
    if (old[:6]!=[1,body['ledger'],body['config_hash'],body['predecessor_code'],60,3600]
            or any(type(old[i]) is not int for i in (0,4,5))
            or type(old[6]) not in (int,float) or not 0<=old[6]<=body['at']
            or type(old[7]) is not int or old[7]!=body['reservation_cutoff']
            or old[8] not in (None,'SOURCE_FAILURE','CLOCK_ROLLBACK')):
        raise ValueError('ALLOWANCE_POLICY_INVALID')
    return body,key
