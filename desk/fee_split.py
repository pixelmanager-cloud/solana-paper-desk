"""Conservative split diagnostic for the captured 50% protocol buyback profile."""

def check_protocol_split(pool,bindings,totals,recipients):
    result={'passed':False,'fee_amounts_verified':False,'full_route_policy_passed':False,'reasons':[]}
    if (not pool or pool.get('identity_evidence_verified') is not True or pool.get('liquidity_control_verified') is not True
            or bindings.get('passed') is not True or totals.get('passed') is not True or recipients.get('passed') is not True):
        return {**result,'reasons':['FEE_SPLIT_CONTEXT_UNVERIFIED']}
    config=pool.get('global_config',{})
    if config.get('configuration_complete') is not True or config.get('slot')!=pool.get('slot'):
        return {**result,'reasons':['FEE_SPLIT_CONFIGURATION_UNVERIFIED']}
    bps=config.get('fields',{}).get('buyback_basis_points')
    # A candidate inferred from 284 captured events, including 144 fractional
    # cases. Restrict to the observed rate; this is not full policy approval.
    if type(bps) is not int or bps!=5000 or bindings.get('account_profile')!='STANDARD_WITH_BUYBACK':
        return {**result,'reasons':['FEE_SPLIT_PROFILE_UNSUPPORTED']}
    try:
        total=int(totals['expected']['protocol_total_raw'])
        if not 0<=total<2**64:raise ValueError('Invalid protocol total')
        expected_buyback=total*bps//10000;expected_protocol=total-expected_buyback
        if recipients['buyback_fee_raw']!=str(expected_buyback) or recipients['protocol_fee_raw']!=str(expected_protocol):
            result['reasons'].append('FEE_SPLIT_RECIPIENT_AMOUNTS_MISMATCH')
        result.update(expected_buyback_raw=str(expected_buyback),expected_protocol_raw=str(expected_protocol),buyback_basis_points=bps)
    except (KeyError,ValueError,TypeError):result['reasons'].append('FEE_SPLIT_AMOUNTS_UNKNOWN')
    return {**result,'passed':not result['reasons'],
        'notice':'Observed-profile diagnostic: floor(protocol fee * 5000 / 10000), remainder to protocol. Historical event agreement is not independent program verification or full transaction approval.'}
