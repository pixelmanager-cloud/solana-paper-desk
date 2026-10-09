"""Constrain PumpSwap self-CPI sell events to the verified sell and its effects."""
from .route_coverage import instruction_receipt
import base64
from .providers import PUMPSWAP
from .programs import event_instruction


def check_sell_event(inventory,bindings,fees,recipients,pool):
    result={'passed':False,'full_route_policy_passed':False,'reasons':[]}
    if bindings.get('passed') is not True or fees.get('passed') is not True or recipients.get('passed') is not True or inventory.get('stack_metadata_verified') is not True:
        return {**result,'reasons':['SELL_EVENT_CONTEXT_UNVERIFIED']}
    rows=[r for r in inventory['instructions'] if r['program']==PUMPSWAP and r['instruction']!=bindings['instruction']]
    if len(rows)!=1:return {**result,'reasons':['SELL_EVENT_REQUIRES_ONE_CALLBACK']}
    row=rows[0];names=bindings['account_bindings'];reasons=result['reasons']
    if row.get('parent_instruction')!=bindings['instruction'] or row.get('parent_program')!=PUMPSWAP or row.get('stack_height')!=3:
        reasons.append('SELL_EVENT_CALLER_MISMATCH')
    if row.get('accounts')!=[names['event_authority']]:reasons.append('SELL_EVENT_ACCOUNT_MISMATCH')
    try:
        raw=base64.b64decode(row['data_base64'],validate=True)
        if raw[:8]!=bytes.fromhex('e445a52e51cb9a1d'):raise ValueError('Not an event callback')
        decoded=event_instruction({'programId':PUMPSWAP,'accounts':row['accounts']},raw)
        if decoded.get('name')!='SellEvent' or decoded.get('schema_complete') is not True:raise ValueError('Unknown sell event layout')
        fields=decoded['fields'];expected=fees['expected']
        identities={'pool':names['pool'],'user':names['user'],'user_base_token_account':names['user_base_token_account'],
            'user_quote_token_account':names['user_quote_token_account'],'protocol_fee_recipient':names['protocol_fee_recipient'],
            'protocol_fee_recipient_token_account':names['protocol_fee_recipient_token_account'],'coin_creator':pool['coin_creator']}
        if any(fields[k]!=v for k,v in identities.items()):reasons.append('SELL_EVENT_IDENTITY_MISMATCH')
        amounts={'base_amount_in':recipients['input_raw'],'pool_base_token_reserves':pool['base_reserve_raw'],
            'pool_quote_token_reserves':pool.get('gross_quote_reserve_raw',pool['quote_reserve_raw']),'quote_amount_out':expected['gross_quote_raw'],
            'lp_fee':expected['lp_fee_raw'],'protocol_fee':expected['protocol_total_raw'],
            'coin_creator_fee':expected['creator_fee_raw'],'user_quote_amount_out':expected['user_output_raw'],
            'buyback_fee':recipients.get('buyback_fee_raw','0')}
        if any(fields[k]!=int(v) for k,v in amounts.items()):reasons.append('SELL_EVENT_AMOUNT_MISMATCH')
        sell=next(r for r in inventory['instructions'] if r['instruction']==bindings['instruction'])
        data=base64.b64decode(sell['data_base64'],validate=True)
        if len(data)!=24 or fields['min_quote_amount_out']!=int.from_bytes(data[16:24],'little'):reasons.append('SELL_EVENT_MINIMUM_MISMATCH')
        if any(fields[k]!=0 for k in ('cashback','holder_rewards','virtual_quote_reserves','creator_fee_unclaimed')) or fields['can_boost'] is not False:
            reasons.append('SELL_EVENT_SPECIAL_FEE_PROFILE_UNSUPPORTED')
        result['schema_file']=decoded['schema_file']
    except (ValueError,KeyError,TypeError,StopIteration):reasons.append('SELL_EVENT_LAYOUT_UNSUPPORTED')
    return {**result,'passed':not reasons,'reasons':sorted(set(reasons)),
        'checked_instructions':[instruction_receipt(row)] if not reasons else [],
        'notice':'Callback identity and event/effect consistency only. Program event contents cannot substitute for balance evidence or complete fee policy.'}
