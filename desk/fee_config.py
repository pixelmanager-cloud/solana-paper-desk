"""Pinned program-owned PumpSwap global fee configuration."""
import json
from pathlib import Path
from .programs import schemas,BorshReader
from .providers import PUMPSWAP
from .security import account_bytes


def config_address():
    from solders.pubkey import Pubkey
    create=next(s for s in schemas()[PUMPSWAP].values() if s['name']=='create_config')
    spec=next(a for a in create['accounts'] if a['name']=='global_config')
    return str(Pubkey.find_program_address([bytes(s['value']) for s in spec['pda']['seeds']],Pubkey.from_string(PUMPSWAP))[0])


def parse_config(account):
    if not account or account.get('owner')!=PUMPSWAP or account.get('executable') is not False:raise ValueError('Global config program mismatch')
    schemas()  # Validate the pinned file digest before reading its account schema.
    schema=json.loads((Path(__file__).parent/'schemas/pump_amm.json').read_text())
    spec=next(x for x in schema['accounts'] if x['name']=='GlobalConfig');raw=account_bytes(account)
    if raw[:8]!=bytes(spec['discriminator']):raise ValueError('Global config discriminator mismatch')
    types={t['name']:t['type'] for t in schema['types']};reader=BorshReader(raw[8:],types);fields={};reasons=[]
    try:
        for field in types['GlobalConfig']['fields']:fields[field['name']]=reader.read(field['type'])
    except ValueError:reasons.append('FEE_CONFIG_LAYOUT_INCOMPLETE')
    if reader.pos!=len(reader.data):reasons.append('FEE_CONFIG_UNKNOWN_BYTES')
    for field in ('lp_fee_basis_points','protocol_fee_basis_points','coin_creator_fee_basis_points','buyback_basis_points','max_configurable_creator_fee_bps'):
        if field in fields and not 0<=fields[field]<=10000:reasons.append('FEE_CONFIG_RATE_INVALID')
    disabled=fields.get('disable_flags')
    if disabled is not None and disabled&~31:reasons.append('FEE_CONFIG_DISABLE_FLAGS_UNKNOWN')
    return {'configuration_complete':not reasons,'fields':fields,'sell_disabled':bool(disabled&16) if disabled is not None else None,
        'reasons':sorted(set(reasons)),'notice':'Program-owned configuration parsing only. Membership and actual charged fees still require route checks; mutable admin configuration can change.'}
