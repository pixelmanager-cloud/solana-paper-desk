"""Bind the observed read-only fee-query CPI; not authorization for the whole program."""
import base64
from .dynamic_fees import fee_schema,fee_address
from .providers import PUMPSWAP,SOL
from .security import base58


def decode_quote_fee_call(raw,accounts):
    schema=fee_schema();spec=next(x for x in schema['instructions'] if x['name']=='get_fees_with_quote_mint')
    if len(raw)!=57 or raw[:8]!=bytes(spec['discriminator']) or raw[8] not in (0,1):raise ValueError('Unsupported fee query layout')
    if accounts!=[str(fee_address()[0]),PUMPSWAP]:raise ValueError('Fee query account binding mismatch')
    return {'canonical':bool(raw[8]),'market_cap_lamports':str(int.from_bytes(raw[9:25],'little')),'quote_mint':base58(raw[25:57])}


def check_fee_query(inventory,bindings,fee_totals):
    result={'passed':False,'full_route_policy_passed':False,'reasons':[]}
    if bindings.get('passed') is not True or fee_totals.get('passed') is not True or inventory.get('stack_metadata_verified') is not True:
        return {**result,'reasons':['FEE_QUERY_CONTEXT_UNVERIFIED']}
    program=fee_schema()['address'];rows=[r for r in inventory['instructions'] if r['program']==program]
    if len(rows)!=1:return {**result,'reasons':['FEE_QUERY_REQUIRES_ONE_CALL']}
    row=rows[0];reasons=result['reasons']
    if row.get('parent_instruction')!=bindings['instruction'] or row.get('parent_program')!=PUMPSWAP or row.get('stack_height')!=3:
        reasons.append('FEE_QUERY_CALLER_MISMATCH')
    try:
        fields=decode_quote_fee_call(base64.b64decode(row['data_base64'],validate=True),row['accounts'])
        expected=fee_totals['fee_schedule']
        if fields['canonical']!=expected['canonical'] or fields['market_cap_lamports']!=expected['market_cap_lamports'] or fields['quote_mint']!=SOL:
            reasons.append('FEE_QUERY_ARGUMENT_MISMATCH')
        if row['accounts'][0]!=bindings['account_bindings']['fee_config']:reasons.append('FEE_QUERY_CONFIG_MISMATCH')
        result['arguments']=fields
    except (ValueError,KeyError,TypeError):reasons.append('FEE_QUERY_LAYOUT_OR_ACCOUNTS_UNSUPPORTED')
    return {**result,'passed':not reasons,'reasons':sorted(set(reasons)),
        'notice':'One exact fee query under the bound AMM sell only. Other fee-program instructions remain unsupported; full transaction policy is still required.'}
