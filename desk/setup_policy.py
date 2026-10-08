"""Bind inner account setup to the wallet's single legacy WSOL ATA."""
import base64
from .instructions import ATA,SYSTEM
from .security import TOKEN_PROGRAM
from .providers import SOL
from .programs import unbase58


def check_sell_setup(inventory,wallet):
    from solders.pubkey import Pubkey
    target=str(Pubkey.find_program_address([unbase58(wallet),unbase58(TOKEN_PROGRAM),unbase58(SOL)],Pubkey.from_string(ATA))[0])
    reasons=[];counts={};rows=inventory.get('instructions',[])
    if inventory.get('stack_metadata_verified') is not True:return {'passed':False,'full_route_policy_passed':False,'reasons':['SETUP_STACK_UNVERIFIED']}
    setup={r['instruction'] for r in rows if r['program']==ATA and r.get('stack_height')==1}
    if len(setup)>1:reasons.append('SETUP_MULTIPLE_ASSOCIATED_ACCOUNTS')
    for row in rows:
        program=row['program'];raw=base64.b64decode(row['data_base64'],validate=True);accounts=row['accounts'];height=row.get('stack_height')
        if program not in (SYSTEM,TOKEN_PROGRAM):continue
        if program==TOKEN_PROGRAM and raw and raw[0] in (3,12):continue  # Recipient policy validates every transfer.
        if program==TOKEN_PROGRAM and raw==b'\x09' and height==1:
            if accounts!=[target,wallet,wallet]:reasons.append('SETUP_OUTER_CLOSE_MISMATCH')
            continue
        if height!=2 or row.get('parent_instruction') not in setup or row.get('parent_program')!=ATA:
            reasons.append('SETUP_OPERATION_OUTSIDE_WALLET_ATA');continue
        kind=None
        if program==SYSTEM:
            kind='create'
            if len(raw)!=52 or raw[:4]!=bytes(4) or accounts!=[wallet,target] or int.from_bytes(raw[12:20],'little')!=165 or raw[20:]!=unbase58(TOKEN_PROGRAM):
                reasons.append('SETUP_CREATE_BINDING_MISMATCH')
        elif raw in (b'\x15',b'\x15\x07\x00'):
            kind='size'
            if accounts!=[SOL]:reasons.append('SETUP_SIZE_MINT_MISMATCH')
        elif raw==b'\x16':
            kind='immutable'
            if accounts!=[target]:reasons.append('SETUP_IMMUTABLE_ACCOUNT_MISMATCH')
        elif len(raw)==33 and raw[0]==18:
            kind='initialize'
            if accounts!=[target,SOL] or raw[1:]!=unbase58(wallet):reasons.append('SETUP_INITIALIZE_OWNER_OR_MINT_MISMATCH')
        else:reasons.append('SETUP_TOKEN_OPERATION_UNSUPPORTED')
        if kind:
            counts[kind]=counts.get(kind,0)+1
            if counts[kind]>1:reasons.append('SETUP_REPEATED_OPERATION')
    if counts.get('create') and counts.get('initialize')!=1:reasons.append('SETUP_CREATED_ACCOUNT_NOT_INITIALIZED')
    if counts.get('initialize') and counts.get('create')!=1:reasons.append('SETUP_INITIALIZATION_WITHOUT_CREATION')
    return {'passed':not reasons,'full_route_policy_passed':False,'reasons':sorted(set(reasons)),
        'wallet_wsol_account':target,'operations':counts,'notice':'Setup bindings only; exact rent, recipient movements, effects and complete route policy remain separate.'}
