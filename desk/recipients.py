"""Token movement recipients for the narrow direct PumpSwap sell profile."""
import base64
from .security import TOKEN_PROGRAM,TOKEN_2022
from .providers import SOL


def check_sell_recipients(inventory,bindings,amount,minimum_out):
    result={'passed':False,'full_route_policy_passed':False,'fee_amounts_verified':False,'reasons':[]}
    if bindings.get('passed') is not True or inventory.get('stack_metadata_verified') is not True:
        return {**result,'reasons':['RECIPIENT_CALL_BINDINGS_UNVERIFIED']}
    if type(amount) is not int or type(minimum_out) is not int or not 0<amount<2**64 or not 0<minimum_out<2**64:raise ValueError('Invalid transfer policy amounts')
    names=bindings['account_bindings'];parent=bindings['instruction'];reasons=result['reasons'];movements=[]
    incoming=0;outgoing=0;fees={'protocol':0,'creator':0,'buyback':0}
    destinations={names['user_quote_token_account']:'user',names['protocol_fee_recipient_token_account']:'protocol',names['coin_creator_vault_ata']:'creator'}
    expected_roles=3
    if 'buyback_fee_recipient_token_account' in names:
        destinations[names['buyback_fee_recipient_token_account']]='buyback';expected_roles+=1
    if len(destinations)!=expected_roles:return {**result,'reasons':['AMM_RECIPIENT_ROLES_OVERLAP']}
    for row in inventory['instructions']:
        program=row['program']
        if program==TOKEN_2022:reasons.append('RECIPIENT_TOKEN_2022_UNSUPPORTED');continue
        if program!=TOKEN_PROGRAM:continue
        raw=base64.b64decode(row['data_base64'],validate=True)
        if not raw or raw[0] not in (3,12):continue
        checked=raw[0]==12;accounts=row['accounts']
        if len(raw)!=(10 if checked else 9) or len(accounts)!=(4 if checked else 3):
            reasons.append('RECIPIENT_TRANSFER_LAYOUT_UNSUPPORTED');continue
        if row.get('parent_instruction')!=parent or row.get('stack_height')!=3:
            reasons.append('TOKEN_TRANSFER_OUTSIDE_APPROVED_AMM_CALL');continue
        source=accounts[0];destination=accounts[2] if checked else accounts[1];authority=accounts[-1]
        mint=accounts[1] if checked else None;n=int.from_bytes(raw[1:9],'little');role=None
        if source==names['user_base_token_account'] and destination==names['pool_base_token_account']:
            if authority!=names['user'] or (checked and mint!=names['base_mint']):reasons.append('AMM_INPUT_TRANSFER_AUTHORITY_OR_MINT_MISMATCH')
            incoming+=n;role='input'
        elif source==names['pool_quote_token_account'] and destination in destinations:
            if authority!=names['pool'] or (checked and mint!=SOL):reasons.append('AMM_OUTPUT_TRANSFER_AUTHORITY_OR_MINT_MISMATCH')
            role=destinations[destination]
            if role=='user':outgoing+=n
            else:fees[role]+=n
        else:reasons.append('AMM_TOKEN_RECIPIENT_OR_SOURCE_UNAPPROVED')
        movements.append({'instruction':row['instruction'],'source':source,'destination':destination,'amount_raw':str(n),'role':role})
    if incoming!=amount:reasons.append('AMM_TRANSFER_INPUT_SUM_MISMATCH')
    if outgoing<minimum_out:reasons.append('AMM_TRANSFER_OUTPUT_BELOW_MINIMUM')
    result.update(passed=not reasons,reasons=sorted(set(reasons)),input_raw=str(incoming),user_output_raw=str(outgoing),
        protocol_fee_raw=str(fees['protocol']),creator_fee_raw=str(fees['creator']),buyback_fee_raw=str(fees['buyback']),transfers=movements,
        notice='Transfer sources, recipients, caller context and user amounts only. Exact fee schedule and full instruction policy remain required.')
    return result
