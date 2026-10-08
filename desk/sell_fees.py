"""Integer standard-SOL sell fee diagnostics; never full transaction approval."""
from .dynamic_fees import standard_sol_fees
from .providers import SOL
from .security import TOKEN_PROGRAM


def standard_sell_amounts(amount, base_reserve, quote_reserve, fees, *, has_creator):
    """SDK 2.1.0 sellBaseInput + util.fee, without float slippage or special pools."""
    if any(type(n) is not int or not 0<n<2**64 for n in (amount,base_reserve,quote_reserve)):
        raise ValueError('Positive bounded raw amounts required')
    if type(has_creator) is not bool:raise ValueError('Creator status required')
    if set(fees)!= {'lp_fee_bps','protocol_fee_bps','creator_fee_bps'} or any(type(n) is not int or not 0<=n<=10000 for n in fees.values()) or sum(fees.values())>10000:
        raise ValueError('Invalid fee rates')
    gross=quote_reserve*amount//(base_reserve+amount)
    charged={name:(gross*rate+9999)//10000 for name,rate in fees.items()}
    if not has_creator:charged['creator_fee_bps']=0
    net=gross-sum(charged.values())
    if net<=0:raise ValueError('Fees consume output')
    return {'gross_quote_raw':str(gross),'lp_fee_raw':str(charged['lp_fee_bps']),
            'protocol_total_raw':str(charged['protocol_fee_bps']),
            'creator_fee_raw':str(charged['creator_fee_bps']),'user_output_raw':str(net)}


def check_sell_fee_totals(pool,bindings,recipients,simulation,keys,amount):
    result={'passed':False,'fee_amounts_verified':False,'full_route_policy_passed':False,'reasons':[]}
    if not pool or pool.get('identity_evidence_verified') is not True or pool.get('liquidity_control_verified') is not True or bindings.get('passed') is not True or recipients.get('passed') is not True:
        return {**result,'reasons':['SELL_FEE_EVIDENCE_UNVERIFIED']}
    reasons=result['reasons']
    try:
        from .fee_config import parse_config
        from .dynamic_fees import parse_fee_config
        from .security import mint_policy
        names=bindings['account_bindings'];slot=pool['slot'];dynamic=pool['dynamic_fee_config'];mint=pool['base_mint_policy'];global_config=pool['global_config']
        if (dynamic['slot']!=slot or mint['slot']!=slot or global_config['slot']!=slot
                or dynamic['address']!=names['fee_config'] or mint['address']!=names['base_mint']
                or global_config['address']!=names['global_config'] or mint['decision']!='PASS_TOKEN_POLICY'):
            raise ValueError('Snapshot identity mismatch')
        if any(pool.get(flag) is not False for flag in ('is_cashback_coin','is_holder_reward','is_mayhem_mode')) or pool.get('creator_fee_bps')!=0:
            raise ValueError('Unsupported pool profile')
        if any(int(pool[key])!=0 for key in ('accrued_protocol_fees_raw','accrued_creator_fees_raw')):
            raise ValueError('Accrued fee profile unsupported')
        if simulation.get('err','missing') is not None:raise ValueError('Simulation unsuccessful')
        if len(set(keys))!=len(keys):raise ValueError('Duplicate keys')
        rows=simulation['preTokenBalances'];seen={}
        for row in rows:
            index=row['accountIndex']
            if type(index) is not int or not 0<=index<len(keys) or index in seen:raise ValueError('Ambiguous pre-state')
            seen[index]=row
        for vault in pool['vaults']:
            row=seen[keys.index(vault['address'])]
            if (row['mint']!=vault['mint'] or row['owner']!=pool['pool'] or row.get('programId')!=TOKEN_PROGRAM
                    or row['uiTokenAmount']['amount']!=vault['amount_raw']):
                reasons.append('SELL_FEE_RESERVES_CHANGED_SINCE_SNAPSHOT')
        # Confirm simulation returned the same configuration and mint supply.
        # Complete CPI policy must still rule out transient changes and other side effects.
        accounts=simulation['accounts']
        if len(accounts)!=len(keys):raise ValueError('Post accounts incomplete')
        for address,parser,prior in ((dynamic['address'],parse_fee_config,dynamic),(global_config['address'],parse_config,global_config)):
            current=parser(accounts[keys.index(address)])
            if current.get('configuration_complete') is not True or current['fields']!=prior['fields']:
                reasons.append('SELL_FEE_CONFIGURATION_CHANGED')
        current_mint=mint_policy(accounts[keys.index(mint['address'])])
        if current_mint.get('decision')!='PASS_TOKEN_POLICY' or current_mint.get('supply_raw')!=mint['supply_raw']:
            reasons.append('SELL_FEE_MINT_CHANGED')
        rates=standard_sol_fees(dynamic,int(mint['supply_raw']),int(pool['base_reserve_raw']),int(pool['quote_reserve_raw']),canonical=pool['canonical_migration_pool'])
        expected=standard_sell_amounts(amount,int(pool['base_reserve_raw']),int(pool['quote_reserve_raw']),rates['fees_bps'],has_creator=pool['coin_creator']!='11111111111111111111111111111111')
        for key in ('user_output_raw','creator_fee_raw'):
            if recipients[key]!=expected[key]:reasons.append('SELL_FEE_'+key.upper()+'_MISMATCH')
        if int(recipients['protocol_fee_raw'])+int(recipients.get('buyback_fee_raw','0'))!=int(expected['protocol_total_raw']):
            reasons.append('SELL_FEE_PROTOCOL_TOTAL_MISMATCH')
        result.update(expected=expected,fee_schedule=rates)
    except (ValueError,KeyError,TypeError,IndexError,OverflowError):
        reasons.append('SELL_FEE_STATE_INCOMPLETE_OR_UNSUPPORTED')
    result.update(passed=not reasons,reasons=sorted(set(reasons)),
        notice='Exact standard-pool aggregate comparison only. Protocol/buyback split rounding and full CPI policy remain unverified; no trading approval.')
    return result
