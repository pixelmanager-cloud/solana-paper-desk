"""Explicit manual one-cycle operator interface; no service or auto-enable.

Target JSON selects reads, never authorizes entry or replaces persisted evidence.
Credentials are optionally loaded only from systemd's CREDENTIALS_DIRECTORY.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from .model import load_config
from .programs import address
from .paper_observation_collector import ObservationTarget
from . import paper_cycle as cycle



def _object(pairs):
    result = {}
    for key,value in pairs:
        if key in result:raise ValueError('Duplicate JSON key')
        result[key]=value
    return result


def _json(path, limit=65536):
    with open(path,'rb') as stream:raw=stream.read(limit+1)
    if len(raw)>limit:raise ValueError('Operator input exceeds bound')
    return json.loads(raw,object_pairs_hook=_object,
                      parse_constant=lambda value:(_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def _hashes(values, maximum):
    if type(values) is not list or len(values)>maximum or any(
        type(x) is not str or re.fullmatch('[0-9a-f]{64}',x) is None for x in values):
        raise ValueError('Bounded retained evidence hashes required')
    return tuple(values)


def load_targets(path):
    value=_json(path)
    if type(value) is not dict or set(value)!={'position_targets','candidates','usd_evidence_refs'}:
        raise ValueError('Explicit cycle target lists required')
    fields={'scan_id','mint','pool','taker','amount_raw','provenance','pool_fee_bps','graduation_refs','known_hazards'}
    result=[];count=0
    for name in ('position_targets','candidates'):
        rows=value[name]
        if type(rows) is not list:raise ValueError('Target lists required')
        count+=len(rows)
        if count>18:raise ValueError('Target bound exceeded')
        items=[]
        for row in rows:
            if type(row) is not dict or set(row)!=fields:raise ValueError('Exact operator target fields required')
            if type(row['scan_id']) is not str or not 1<=len(row['scan_id'])<=256:raise ValueError('Scan ID required')
            for key in ('mint','pool','taker'):address(row[key])
            if type(row['amount_raw']) is not int or not 0<row['amount_raw']<2**64:raise ValueError('Exact raw units required')
            if row['provenance'] not in ('SYNTHETIC_TEST_ONLY','PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE'):
                raise ValueError('Explicit source provenance required')
            fee=row['pool_fee_bps']
            if fee is not None and (type(fee) is not str or re.fullmatch(r'[0-9]{1,4}(?:\.[0-9]{1,8})?',fee) is None):
                raise ValueError('Explicit bounded fee hypothesis required')
            hazards=row['known_hazards']
            if type(hazards) is not list or len(hazards)>32 or any(type(x) is not str or re.fullmatch('[A-Z0-9_]{1,128}',x) is None for x in hazards):
                raise ValueError('Explicit bounded adverse hazard labels required')
            target=ObservationTarget(*(row[key] for key in ('scan_id','mint','pool','taker','amount_raw')))
            items.append(cycle.CycleTarget(target,row['provenance'],None,None,fee,known_hazards=tuple(hazards),
                         graduation_refs=_hashes(row['graduation_refs'],8)))
        result.append(tuple(items))
    usd=_hashes(value['usd_evidence_refs'],3)
    if len(usd) not in (0,3):raise ValueError('Exact USD source triple required')
    return (*result,usd)


def _credentials():
    directory=os.environ.get('CREDENTIALS_DIRECTORY')
    if not directory:raise ValueError('systemd credential directory required')
    path=Path(directory)/'provider-keys.json'
    with path.open('rb') as stream:
        mode=stat.S_IMODE(os.fstat(stream.fileno()).st_mode)
        if mode not in (0o400,0o440,0o600):raise ValueError('Managed credential permissions required')
        raw=stream.read(16385)
    if len(raw)>16384:raise ValueError('Credential size bound')
    value=json.loads(raw,object_pairs_hook=_object)
    if type(value) is not dict:raise ValueError('Managed credential object required')
    keys={name:value.get(name) for name in ('HELIUS_API_KEY','JUPITER_API_KEY')}
    if any(type(v) is not str or not v.strip() for v in keys.values()):raise ValueError('Both runtime credentials required')
    for name,value in keys.items():os.environ[name]=value.strip()


def main(argv=None):
    parser=argparse.ArgumentParser(description='Manual bounded paper cycle; EXECUTION_UNVERIFIED, no auto-enable')
    parser.add_argument('--config',required=True)
    commands=parser.add_subparsers(dest='command',required=True)
    init=commands.add_parser('init',help='Create exclusively a NEW quote-paper experiment')
    init.add_argument('--ledger-db',required=True)
    once=commands.add_parser('once',help='One supervised pass; existing admissions and ledger required')
    for name in ('research-db','evidence-db','ledger-db','targets'):once.add_argument('--'+name,required=True)
    once.add_argument('--systemd-credentials',action='store_true',help='Load only %d/provider-keys.json')
    once.add_argument('--dependency-blocker',action='append',default=[])
    once.add_argument('--control',choices=('PAUSE_ENTRY','EXIT_ONLY','LIQUIDATE','RESUME'))
    args=parser.parse_args(argv)
    try:
        cfg=load_config(args.config);cycle._config(cfg)
        if args.command=='init':
            cycle.initialize(args.ledger_db,cfg)
            result={'kind':'paper_cycle_init_v1','status':'INITIALIZED','execution_status':'EXECUTION_UNVERIFIED',
                    'live_readiness':False,'automatic_entry_enabled':False}
        else:
            positions,candidates,usd=load_targets(args.targets)
            blockers=tuple(args.dependency_blocker)
            if len(blockers)>16 or any(not 1<=len(x)<=128 for x in blockers):raise ValueError('Blocker bound')
            # Pending integration/review refuses without credential reads.
            if args.systemd_credentials and not blockers:_credentials()
            controls=()
            if args.control:
                controls=({'schema_version':1,'kind':'control','event_id':'operator-cycle:'+uuid.uuid4().hex,
                           'ts':int(time.time()),'actor':'operator','command':args.control},)
            result=cycle.run_once(args.research_db,args.evidence_db,args.ledger_db,cfg,
                    position_targets=positions,candidates=candidates,dependency_blockers=blockers,
                    controls=controls,usd_evidence_refs=usd)
        # Print no raw events/source payloads, targets, credential values or error strings.
        summary={key:result[key] for key in ('kind','status','execution_status','live_readiness',
                 'automatic_entry_enabled','attempted_requests','events','budget','evidence_hash') if key in result}
        summary['blockers']=result.get('blockers',[])
        summary['outcomes']=[{k:row[k] for k in ('type','mint','side','reason','risk_flags','execution_status') if k in row}
                             for row in result.get('outcomes',[])]
        summary['diagnostics']=[]
        for row in result.get('diagnostics',[]):
            diagnostic={k:row[k] for k in ('scan_id','blockers') if k in row}
            if 'graduation' in row:
                diagnostic['graduation']={k:row['graduation'][k] for k in
                    ('status','graduated_at','blockers','source_hashes','scope') if k in row['graduation']}
            summary['diagnostics'].append(diagnostic)
        print(json.dumps(summary,sort_keys=True,allow_nan=False))
        return 0 if result['status'] in ('COMPLETE','INITIALIZED') else 2
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError,OverflowError,RecursionError):
        print(json.dumps({'kind':'paper_cycle_cli_v1','status':'UNAVAILABLE',
            'blockers':['OPERATOR_INPUT_CONFIG_CREDENTIAL_OR_CHECKPOINT_UNAVAILABLE'],
            'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False}))
        return 2


if __name__=='__main__':raise SystemExit(main())
