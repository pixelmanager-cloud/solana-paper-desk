"""Atomic simulation balance checks. Success is separate from instruction approval."""
from collections import defaultdict
from .decode import integer
from .providers import SOL


def check_effects(value, keys, wallet, mint, amount, minimum_out, max_fee_lamports=50000, *, direction="sell"):
    if direction not in ("buy","sell"):raise ValueError("Unknown swap direction")
    if type(amount) is not int or type(minimum_out) is not int or not 0 < amount < 2**64 or not 0 < minimum_out < 2**64:
        raise ValueError("Positive bounded exact input and minimum output required")
    reasons=[]
    if 'err' not in value or value['err'] is not None:
        return {'passed':False,'reasons':['SIMULATION_FAILED_OR_UNKNOWN']}
    pre_native,post_native=value.get('preBalances'),value.get('postBalances')
    pre_tokens,post_tokens=value.get('preTokenBalances'),value.get('postTokenBalances')
    if not all(isinstance(x,list) for x in (pre_native,post_native,pre_tokens,post_tokens)):
        return {'passed':False,'reasons':['ATOMIC_BALANCE_METADATA_UNAVAILABLE']}
    if len(pre_native)!=len(keys) or len(post_native)!=len(keys) or len(set(keys))!=len(keys):
        return {'passed':False,'reasons':['BALANCE_ACCOUNT_INDEX_MISMATCH']}
    pre_native=[integer(x) for x in pre_native];post_native=[integer(x) for x in post_native]
    if wallet not in keys:raise ValueError('Wallet missing from simulation keys')
    pre_map,post_map={},{}
    for rows,dest in ((pre_tokens,pre_map),(post_tokens,post_map)):
        for row in rows:
            index=integer(row['accountIndex'])
            if index>=len(keys) or index in dest:raise ValueError('Invalid simulation token index')
            owner=row.get('owner');token_mint=row.get('mint')
            if not isinstance(owner,str) or not isinstance(token_mint,str):
                reasons.append('TOKEN_OWNER_METADATA_MISSING');continue
            dest[index]={'owner':owner,'mint':token_mint,'amount':integer(row['uiTokenAmount']['amount'])}
    pre_total,post_total=defaultdict(int),defaultdict(int)
    owned=set()
    for index in pre_map.keys()|post_map.keys():
        before,after=pre_map.get(index),post_map.get(index)
        if (before and before['owner']==wallet) or (after and after['owner']==wallet):
            owned.add(index)
            if before and after and (before['owner']!=after['owner'] or before['mint']!=after['mint']):
                reasons.append('TOKEN_ACCOUNT_IDENTITY_CHANGED')
            if not before and pre_native[index]!=0:reasons.append('MISSING_EXISTING_PRE_TOKEN_BALANCE')
            if not after and post_native[index]!=0:reasons.append('MISSING_EXISTING_POST_TOKEN_BALANCE')
            if before and before['owner']==wallet:pre_total[before['mint']]+=before['amount']
            if after and after['owner']==wallet:post_total[after['mint']]+=after['amount']
    sold=pre_total[mint]-post_total[mint]
    if direction=='sell' and sold!=amount:reasons.append('EXACT_INPUT_DEBIT_MISMATCH')
    if direction=='buy' and -sold<minimum_out:reasons.append('BOUGHT_TOKENS_BELOW_ROUTE_MINIMUM')
    for asset in pre_total.keys()|post_total.keys():
        if asset not in (mint,SOL) and post_total[asset]<pre_total[asset]:
            reasons.append('UNRELATED_TOKEN_LOSS')
    # Native balance of a WSOL account includes its wrapped SOL. Summing account
    # lamports (rather than adding WSOL token amounts again) also neutralizes rent.
    native_accounts=owned|{keys.index(wallet)}
    native_delta=sum(post_native[i]-pre_native[i] for i in native_accounts)
    fee=integer(value['fee']) if value.get('fee') is not None else None
    if type(max_fee_lamports) is not int or max_fee_lamports<=0:raise ValueError('Positive fee cap required')
    if fee is None:reasons.append('SIMULATION_FEE_UNKNOWN')
    elif fee>max_fee_lamports:reasons.append('SIMULATION_FEE_EXCEEDS_BUDGET')
    if direction=='sell':
        if native_delta<=0:reasons.append('NO_NET_NATIVE_PROCEEDS')
        if fee is not None and native_delta+fee<minimum_out:reasons.append('PROCEEDS_BELOW_ROUTE_MINIMUM')
    elif fee is not None and -native_delta-fee!=amount:reasons.append('EXACT_NATIVE_INPUT_DEBIT_MISMATCH')
    return {'passed':not reasons,'reasons':sorted(set(reasons)),'sold_raw':str(sold) if direction=='sell' else None,'received_raw':str(-sold) if direction=='buy' else None,'direction':direction,
            'native_wealth_delta_lamports':str(native_delta),'fee_lamports':str(fee) if fee is not None else None,
            'minimum_route_out_lamports':str(minimum_out) if direction=='sell' else None,'minimum_route_out_token_raw':str(minimum_out) if direction=='buy' else None,'owned_token_account_indices':sorted(owned),
            'notice':'Atomic balance effects only. Delegate/authority changes and full instruction policy require separate checks.'}


def check_account_controls(value, keys, wallet, owned_indices, *, mint=None):
    """Inspect returned account state; balances alone cannot reveal approvals."""
    from .security import TOKEN_PROGRAM, TOKEN_2022, account_bytes, base58, holding_policy
    accounts=value.get('accounts')
    if not isinstance(accounts,list) or len(accounts)!=len(keys):
        return {'passed':False,'reasons':['POST_ACCOUNT_STATE_INCOMPLETE']}
    reasons=[]
    if mint is not None:
        from .security import mint_policy
        if mint not in keys:reasons.append('POST_MINT_STATE_MISSING')
        else:
            policy=mint_policy(accounts[keys.index(mint)])
            reasons.extend('POST_'+r for r in policy['reasons'])
    balances=value.get('postBalances');tokens=value.get('postTokenBalances')
    if not isinstance(balances,list) or len(balances)!=len(keys):
        return {'passed':False,'reasons':['POST_BALANCE_STATE_INCOMPLETE']}
    # Bind economic metadata to the actual returned account bytes. A correct
    # metadata amount cannot excuse a different owner/amount in account state.
    for index,account in enumerate(accounts):
        reported=integer(balances[index])
        actual=integer(account['lamports']) if account is not None else 0
        if actual>=2**64 or reported>=2**64 or actual!=reported:
            reasons.append('POST_LAMPORT_STATE_MISMATCH')
    token_rows={}
    if not isinstance(tokens,list):reasons.append('POST_TOKEN_METADATA_INCOMPLETE')
    else:
        for row in tokens:
            index=integer(row['accountIndex'])
            if index>=len(keys) or index in token_rows:raise ValueError('Invalid post token index')
            token_rows[index]=row
            account=accounts[index]
            if not account or account.get('owner') not in (TOKEN_PROGRAM,TOKEN_2022):
                reasons.append('POST_TOKEN_STATE_MISSING');continue
            raw=account_bytes(account)
            if (len(raw)<165 or account.get('executable') is not False
                    or row.get('programId')!=account['owner'] or row.get('mint')!=base58(raw[:32])
                    or row.get('owner')!=base58(raw[32:64])
                    or integer(row['uiTokenAmount']['amount'])!=int.from_bytes(raw[64:72],'little')):
                reasons.append('POST_TOKEN_STATE_MISMATCH')
    wallet_index=keys.index(wallet)
    payer=accounts[wallet_index]
    if not payer or payer.get('owner')!='11111111111111111111111111111111' or payer.get('executable') is not False or account_bytes(payer):
        reasons.append('PAYER_ACCOUNT_CONTROL_CHANGED')
    checked=set()
    for index,account in enumerate(accounts):
        if not account:continue
        owner=account.get('owner')
        if owner not in (TOKEN_PROGRAM,TOKEN_2022):continue
        raw=account_bytes(account)
        if len(raw)<165 or base58(raw[32:64])!=wallet:continue
        checked.add(index)
        if index not in token_rows:reasons.append('OWNED_POST_TOKEN_METADATA_MISSING')
        policy=holding_policy(account,base58(raw[:32]),wallet)
        reasons.extend(policy['reasons'])
    balances=value.get('postBalances')
    for index in owned_indices:
        if type(index) is not int or not 0<=index<len(keys):
            reasons.append('INVALID_OWNED_ACCOUNT_INDEX');continue
        if index in checked:continue
        # A closed account is permitted only when the atomic balance report agrees.
        if accounts[index] is None and isinstance(balances,list) and len(balances)==len(keys) and balances[index]==0:continue
        reasons.append('OWNED_ACCOUNT_CONTROL_UNVERIFIED')
    return {'passed':not reasons,'reasons':sorted(set(reasons)),
            'checked_token_accounts':len(checked),
            'notice':'Post-state authority checks do not establish program or route safety.'}
