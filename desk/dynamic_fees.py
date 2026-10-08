"""Pinned Pump fee-program layout and integer-only standard SOL fee tiers."""
import hashlib,json
from functools import lru_cache
from pathlib import Path
from .programs import BorshReader,schemas
from .providers import PUMPSWAP
from .security import account_bytes

@lru_cache(maxsize=1)
def fee_schema():
    folder=Path(__file__).parent/'schemas';manifest=json.loads((folder/'fee_manifest.json').read_text())
    raw=(folder/manifest['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=manifest['sha256']:raise ValueError('Fee schema checksum mismatch')
    parsed=json.loads(raw)
    if parsed['address']!=manifest['program']:raise ValueError('Fee schema program mismatch')
    return parsed


def fee_address():
    from solders.pubkey import Pubkey
    sell=next(x for x in schemas()[PUMPSWAP].values() if x['name']=='sell')
    entry=next(x for x in sell['accounts'] if x['name']=='fee_config')
    return Pubkey.find_program_address([bytes(x['value']) for x in entry['pda']['seeds']],Pubkey.from_string(fee_schema()['address']))


def parse_fee_config(account):
    schema=fee_schema()
    if not account or account.get('owner')!=schema['address'] or account.get('executable') is not False:raise ValueError('Fee config owner mismatch')
    raw=account_bytes(account);spec=next(x for x in schema['accounts'] if x['name']=='FeeConfig')
    if raw[:8]!=bytes(spec['discriminator']):raise ValueError('Fee config discriminator mismatch')
    # Official pump-swap-sdk 2.1.0 versionedFeeConfigData gates fields by
    # allocated length, not vector end. Provenance: schemas/fee_sdk_manifest.json.
    # Restrict to documented allocations; never interpret old reserved bytes as
    # newly introduced fields, even if they happen to be valid Borsh.
    supported_sizes=(2512,4073,4097)
    reader=BorshReader(raw[8:],{x['name']:x['type'] for x in schema['types']});fields={};reasons=[]
    if len(raw) not in supported_sizes:reasons.append('DYNAMIC_FEE_ALLOCATION_UNSUPPORTED')
    else:
        try:
            layout=next(x['type']['fields'] for x in schema['types'] if x['name']=='FeeConfig')
            for field in layout:
                name=field['name']
                if name=='stable_fee_tiers' and len(raw)<4073:fields[name]=[]
                elif name=='exotic_flat_fees' and len(raw)<4097:
                    fields[name]={'lp_fee_bps':0,'protocol_fee_bps':0,'creator_fee_bps':0}
                else:fields[name]=reader.read(field['type'])
            if any(reader.data[reader.pos:]):reasons.append('DYNAMIC_FEE_NONZERO_RESERVED_BYTES')
        except ValueError:
            fields={};reasons.append('DYNAMIC_FEE_LAYOUT_INCOMPLETE')
    if fields:
        if fields['bump']!=fee_address()[1]:reasons.append('DYNAMIC_FEE_BUMP_MISMATCH')
        rates=[fields['flat_fees'],fields['exotic_flat_fees']]
        for key in ('fee_tiers','stable_fee_tiers'):
            tiers=fields[key];thresholds=[t['market_cap_lamports_threshold'] for t in tiers]
            if key=='fee_tiers' and not tiers:reasons.append('DYNAMIC_FEE_TIERS_EMPTY')
            if any(a>=b for a,b in zip(thresholds,thresholds[1:])):reasons.append('DYNAMIC_FEE_TIERS_NOT_STRICTLY_SORTED')
            rates.extend(t['fees'] for t in tiers)
        if any(any(type(n) is not int or not 0<=n<=10000 for n in fee.values()) or sum(fee.values())>10000 for fee in rates):
            reasons.append('DYNAMIC_FEE_RATES_INVALID')
    return {'configuration_complete':not reasons,'fields':fields,'reasons':sorted(set(reasons)),
        'allocated_bytes':len(raw),'decoded_bytes':8+reader.pos,'reserved_bytes':max(0,len(raw)-8-reader.pos),
        'notice':'Decoded fee schedule only. A same-bank reserve/mint snapshot and supported pool profile are required to apply it.'}


def standard_sol_fees(config,supply,base_reserve,quote_reserve,*,canonical,virtual_quote_reserves=0):
    if config.get('configuration_complete') is not True:raise ValueError('Dynamic fee configuration unverified')
    if type(canonical) is not bool:raise ValueError('Canonical pool identity required')
    if any(type(x) is not int or not 0<x<2**64 for x in (supply,base_reserve,quote_reserve)):raise ValueError('Positive bounded raw pool values required')
    if type(virtual_quote_reserves) is not int or virtual_quote_reserves!=0:raise ValueError('Virtual reserve profile unsupported')
    cap=quote_reserve*supply//base_reserve;fields=config['fields'];threshold=None
    if canonical:
        tiers=fields['fee_tiers']
        if not tiers:raise ValueError('Fee tiers empty')
        selected=tiers[0]
        for tier in tiers:
            if tier['market_cap_lamports_threshold']<=cap:selected=tier
            else:break
        fees=selected['fees'];threshold=selected['market_cap_lamports_threshold']
    else:fees=fields['flat_fees']
    return {'market_cap_lamports':str(cap),'tier_threshold_lamports':str(threshold) if threshold is not None else None,
        'fees_bps':dict(fees),'canonical':canonical,'fee_amounts_verified':False,
        'notice':'Standard SOL tier selection only; token-specific creator/reward/boost behavior and exact transfer rounding remain separate.'}
