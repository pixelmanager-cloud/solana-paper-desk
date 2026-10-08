"""Token-2022 extension inventory; diagnostic only, never an allow decision."""
import json
from pathlib import Path
from .security import account_bytes,TOKEN_2022

SCHEMA=json.loads((Path(__file__).parent/'schemas/token2022_extensions.json').read_text())
NAMES={int(k):v for k,v in SCHEMA['extension_names'].items()}
CAPABILITIES={
    1:'TRANSFER_FEES_AND_FEE_AUTHORITIES',3:'MINT_CLOSE_CONTROL',
    4:'CONFIDENTIAL_TRANSFERS',6:'DEFAULT_ACCOUNT_STATE_CONTROL',
    9:'NON_TRANSFERABLE_TOKEN',10:'INTEREST_ADJUSTED_UI_AMOUNT',
    12:'PERMANENT_DELEGATE_TRANSFER_OR_BURN',14:'TRANSFER_HOOK_PROGRAM',
    16:'CONFIDENTIAL_TRANSFER_FEES',24:'CONFIDENTIAL_MINT_BURN',
    25:'SCALED_UI_AMOUNT',26:'PAUSABLE_MINT',28:'PERMISSIONED_BURN',
}
# Account-only extension IDs cannot occur on a valid mint.
ACCOUNT_ONLY={2,5,7,8,11,13,15,17,27}


def inspect_mint_extensions(account):
    if not isinstance(account,dict) or account.get('owner')!=TOKEN_2022:
        raise ValueError('Token-2022 mint account required')
    data=account_bytes(account)
    reasons=[];extensions=[]
    if account.get('executable') is not False or len(data)<82 or len(data)==355:
        reasons.append('INVALID_TOKEN_2022_MINT_LAYOUT')
    elif len(data)!=82:
        if len(data)<166 or any(data[82:165]) or data[165]!=1:
            reasons.append('INVALID_TOKEN_2022_MINT_PADDING_OR_TYPE')
        else:
            offset=166;seen=set()
            while offset<len(data):
                if not any(data[offset:]):break
                if len(data)-offset<4:
                    reasons.append('TRUNCATED_EXTENSION_HEADER');break
                kind=int.from_bytes(data[offset:offset+2],'little')
                size=int.from_bytes(data[offset+2:offset+4],'little');offset+=4
                if kind==0:
                    reasons.append('NONZERO_DATA_AFTER_EXTENSION_TERMINATOR');break
                if offset+size>len(data):
                    reasons.append('TRUNCATED_EXTENSION_VALUE');break
                if kind in seen:reasons.append('DUPLICATE_TOKEN_EXTENSION')
                seen.add(kind)
                if kind in ACCOUNT_ONLY:reasons.append('ACCOUNT_EXTENSION_ON_MINT')
                if kind not in NAMES:reasons.append('UNKNOWN_TOKEN_EXTENSION')
                extensions.append({'id':kind,'name':NAMES.get(kind,'Unknown'),
                                   'length':size,'capability':CAPABILITIES.get(kind)})
                offset+=size
                if len(extensions)>64:
                    reasons.append('EXTENSION_COUNT_LIMIT');break
    return {'layout_inventory_complete':not reasons,'extensions':extensions,'reasons':sorted(set(reasons)),
            'capabilities':sorted({x['capability'] for x in extensions if x['capability']}),
            'source_commit':SCHEMA['commit'],'eligible_for_trading':False,
            'notice':'Extension presence describes capability, not proof of abuse. Payload semantics and active authorities are not approved; Token-2022 remains excluded.'}
