"""Narrow sell setup/cleanup policy; does not approve inner AMM behavior."""
from .route_coverage import outer_receipt
import base64
from .instructions import ATA,COMPUTE,JUPITER,SYSTEM
from .providers import SOL
from .security import TOKEN_PROGRAM
from .programs import address


def check_sell_envelope(outer,wallet):
    from solders.pubkey import Pubkey
    address(wallet)
    if not isinstance(outer,list) or not 1<=len(outer)<=64:raise ValueError('Invalid instruction count')
    destination=str(Pubkey.find_program_address([bytes(Pubkey.from_string(wallet)),bytes(Pubkey.from_string(TOKEN_PROGRAM)),bytes(Pubkey.from_string(SOL))],Pubkey.from_string(ATA))[0])
    reasons=[];seen=set();phase=0;routes=0;setups=0;closes=0
    # Use our maximum supported compute allowance if no explicit limit is supplied.
    units=1400000;price=0
    for ix in outer:
        program=address(ix['programId']);data=base64.b64decode(ix['data'],validate=True);accounts=ix['accounts']
        for a in accounts:
            address(a['pubkey'])
            if type(a['isSigner']) is not bool or type(a['isWritable']) is not bool:raise ValueError('Invalid instruction flags')
            if a['isSigner'] and a['pubkey']!=wallet:reasons.append('ENVELOPE_UNEXPECTED_SIGNER')
        keys=[a['pubkey'] for a in accounts]
        if program==COMPUTE:
            tag=data[0] if data else None
            if phase!=0 or accounts or tag not in (2,3) or len(data)!={2:5,3:9}.get(tag):
                reasons.append('ENVELOPE_COMPUTE_LAYOUT_OR_ORDER');continue
            if tag in seen:reasons.append('ENVELOPE_DUPLICATE_COMPUTE_SETTING')
            seen.add(tag);value=int.from_bytes(data[1:],'little')
            if tag==2:
                units=value
                if not 1<=units<=1400000:reasons.append('ENVELOPE_COMPUTE_LIMIT_OUTSIDE_BUDGET')
            else:price=value
        elif program==ATA:
            if phase>1:reasons.append('ENVELOPE_SETUP_AFTER_SWAP')
            phase=max(phase,1);setups+=1
            if (data!=b'\x01' or keys!=[wallet,destination,wallet,SOL,SYSTEM,TOKEN_PROGRAM]
                    or not accounts[0]['isSigner'] or not accounts[0]['isWritable'] or not accounts[1]['isWritable']):
                reasons.append('ENVELOPE_UNAPPROVED_ACCOUNT_SETUP')
        elif program==JUPITER:
            if phase>1:reasons.append('ENVELOPE_MULTIPLE_OR_MISORDERED_ROUTES')
            phase=2;routes+=1
        elif program==TOKEN_PROGRAM:
            if phase!=2:reasons.append('ENVELOPE_CLEANUP_WITHOUT_SWAP')
            phase=3;closes+=1
            if (data!=b'\x09' or keys!=[destination,wallet,wallet]
                    or not accounts[0]['isWritable'] or not accounts[1]['isWritable'] or not accounts[2]['isSigner']):
                reasons.append('ENVELOPE_UNAPPROVED_TOKEN_OPERATION_OR_RECIPIENT')
        else:reasons.append('ENVELOPE_UNAPPROVED_OUTER_PROGRAM')
    if routes!=1:reasons.append('ENVELOPE_REQUIRES_ONE_ROUTE')
    if setups>1:reasons.append('ENVELOPE_DUPLICATE_ACCOUNT_SETUP')
    if closes!=1:reasons.append('ENVELOPE_REQUIRES_NATIVE_SOL_CLEANUP')
    fee_bound=5000+(units*price+999999)//1000000
    if fee_bound>50000:reasons.append('ENVELOPE_NETWORK_FEE_EXCEEDS_BUDGET')
    return {'passed':not reasons,'full_route_policy_passed':False,'reasons':sorted(set(reasons)),
            'checked_instructions':[outer_receipt(ix,i) for i,ix in enumerate(outer)] if not reasons else [],'output_account':destination,'network_fee_bound_lamports':fee_bound,
            'notice':'Outer sell setup, cleanup recipients and compute budget only. Inner transfers, pool bindings and actual rent costs remain required.'}
