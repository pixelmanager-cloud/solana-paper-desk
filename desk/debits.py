"""Gross wallet debit checks; net balance agreement is not recipient approval."""
import base64
from .programs import address
from .security import TOKEN_PROGRAM
from .instructions import SYSTEM,ATA
from .providers import SOL
from .decode import integer


def check_sell_debits(inventory,value,keys,wallet,mint,holding,amount,*,rent_exempt_lamports=None):
    from solders.pubkey import Pubkey
    for key in (wallet,mint,holding):address(key)
    if type(amount) is not int or not 0<amount<2**64:raise ValueError('Invalid sell debit amount')
    wsol=str(Pubkey.find_program_address([bytes(Pubkey.from_string(wallet)),bytes(Pubkey.from_string(TOKEN_PROGRAM)),bytes(Pubkey.from_string(SOL))],Pubkey.from_string(ATA))[0])
    reasons=[];owned={};gross=0;rent=0;rent_creations=0;movements=[]
    before=value.get('preTokenBalances');balances=value.get('preBalances')
    if not isinstance(before,list) or not isinstance(balances,list) or len(balances)!=len(keys):
        return {'passed':False,'full_route_policy_passed':False,'reasons':['GROSS_DEBIT_PRE_STATE_MISSING']}
    for row in before:
        idx=integer(row['accountIndex'])
        if idx>=len(keys):raise ValueError('Invalid debit account index')
        if row.get('owner')==wallet:
            if keys[idx] in owned:raise ValueError('Duplicate debit account identity')
            owned[keys[idx]]=row['mint']
    if owned.get(holding)!=mint:reasons.append('SELL_SOURCE_PRE_IDENTITY_MISSING')
    rows=inventory.get('instructions')
    if not isinstance(rows,list) or not rows:reasons.append('GROSS_DEBIT_INVENTORY_MISSING');rows=[]
    if inventory.get('inventory_checks_passed') is not True:reasons.append('GROSS_DEBIT_INVENTORY_UNVERIFIED')
    for row in rows:
        program=row['program'];raw=base64.b64decode(row['data_base64'],validate=True);accounts=row['accounts']
        if program==TOKEN_PROGRAM and raw and raw[0] in (3,12):
            checked=raw[0]==12;count=4 if checked else 3
            if len(raw)!=(10 if checked else 9) or len(accounts)!=count:
                reasons.append('GROSS_DEBIT_TOKEN_LAYOUT_UNSUPPORTED');continue
            source=accounts[0];destination=accounts[2] if checked else accounts[1];authority=accounts[-1]
            if source not in owned:continue
            n=int.from_bytes(raw[1:9],'little')
            movements.append({'source':source,'destination':destination,'amount_raw':str(n),'instruction':row['instruction']})
            if source!=holding or owned[source]!=mint:reasons.append('UNRELATED_GROSS_TOKEN_DEBIT')
            else:gross+=n
            if authority!=wallet or (checked and accounts[1]!=mint):reasons.append('GROSS_DEBIT_AUTHORITY_OR_MINT_MISMATCH')
        elif program==SYSTEM and len(raw)>=4 and accounts and accounts[0]==wallet:
            tag=int.from_bytes(raw[:4],'little')
            if tag!=0 or len(raw)!=52 or len(accounts)!=2:
                reasons.append('UNAPPROVED_GROSS_NATIVE_DEBIT');continue
            n=int.from_bytes(raw[4:12],'little');space=int.from_bytes(raw[12:20],'little')
            from .security import base58
            target=accounts[1]
            if (target!=wsol or target not in keys or integer(balances[keys.index(target)])!=0
                    or space!=165 or base58(raw[20:52])!=TOKEN_PROGRAM):
                reasons.append('UNAPPROVED_RENT_RECIPIENT_OR_LAYOUT')
            rent+=n;rent_creations+=1
    if gross!=amount:reasons.append('GROSS_SELL_DEBIT_MISMATCH')
    if rent>5000000:reasons.append('SELL_RENT_BUDGET_EXCEEDED')
    if rent_creations>1:reasons.append('MULTIPLE_SELL_RENT_CREATIONS')
    if rent_creations:
        if type(rent_exempt_lamports) is not int or not 0<rent_exempt_lamports<=5000000:
            reasons.append('EXACT_SELL_RENT_UNVERIFIED')
        elif rent!=rent_exempt_lamports:reasons.append('SELL_RENT_AMOUNT_MISMATCH')
    return {'passed':not reasons,'full_route_policy_passed':False,'reasons':sorted(set(reasons)),
        'rent_creations':rent_creations,'rent_exempt_lamports':rent_exempt_lamports,'gross_token_debit_raw':str(gross),'setup_lamports':str(rent),'wallet_token_transfers':movements,
        'notice':'Gross wallet debits and narrow WSOL setup only. AMM recipients and complete route policy remain required; rent lookups must also meet the overall simulation freshness window.'}
