"""Discover historical token accounts, including accounts no longer held/open."""
from .programs import address
from .launch import launch_anchor
from .security import TOKEN_PROGRAM,TOKEN_2022


def account_inventory(mint,observations,coverage):
    address(mint);reasons=[];accounts={};seen_balances=set();anchors=[]
    if (coverage.get('address')!=mint or coverage.get('token_accounts_filter')!='none'
            or coverage.get('query_coverage_verified') is not True or coverage.get('raw_pages_persisted') is not True):
        reasons.append('ACCOUNT_DISCOVERY_QUERY_NOT_VERIFIED')
    signatures=set()
    for obs in observations:
        if obs.get('status')=='FAILED':continue
        if obs.get('commitment')!='finalized_provider_response':reasons.append('ACCOUNT_DISCOVERY_NOT_FINALIZED')
        if obs['signature'] in signatures:reasons.append('ACCOUNT_DISCOVERY_DUPLICATE_TRANSACTION')
        signatures.add(obs['signature'])
        if {'INNER_INSTRUCTIONS_UNAVAILABLE','UNDECODED_TOKEN_INSTRUCTION'}&set(obs.get('limitations',[])):
            reasons.append('ACCOUNT_INITIALIZATION_DECODING_INCOMPLETE')
        if any(x.get('mint')==mint for x in obs.get('mint_initializations',[])):
            anchor=launch_anchor(obs,mint)
            if anchor['verified']:anchors.append(anchor)
        for row in obs.get('token_account_initializations',[]):
            if row['mint']!=mint:continue
            key=address(row['account']);address(row['owner'])
            if row['program'] not in (TOKEN_PROGRAM,TOKEN_2022):raise ValueError('Unknown token initialization program')
            accounts.setdefault(key,[]).append({'owner_at_initialization':row['owner'],'program':row['program'],
                'signature':obs['signature'],'slot':obs['slot'],'instruction':row['instruction']})
        for row in obs.get('token_deltas',[]):
            if row['mint']==mint:seen_balances.add(address(row['account']))
    if len(anchors)!=1:reasons.append('ACCOUNT_DISCOVERY_LAUNCH_ANCHOR_REQUIRED')
    else:
        at=anchors[0]['block_time'];start,end=coverage.get('start'),coverage.get('end')
        slot_range=coverage.get('slot_range')
        in_range=(slot_range['gte']<=anchors[0]['slot']<slot_range['lt']) if slot_range is not None else (type(start) is int and type(end) is int and start<=at<end)
        if not in_range:
            reasons.append('ACCOUNT_DISCOVERY_RANGE_EXCLUDES_BIRTH')
    if seen_balances-set(accounts):reasons.append('TOKEN_ACCOUNTS_WITHOUT_INITIALIZATION_WITNESS')
    # Every initialized account needs its own bounded history query; querying the
    # mint cannot see unchecked SPL transfers that omit the mint account key.
    frontier=sorted(set(accounts)|seen_balances)
    return {'mint':mint,'initialization_inventory_verified':not reasons,'transfer_history_complete':False,
        'eligible_for_trading':False,'reasons':sorted(set(reasons)),
        'accounts':[{'address':key,'initializations':accounts.get(key,[]),'history_required':True} for key in frontier],
        'account_count':len(frontier),'history_start':coverage.get('start'),'history_end':coverage.get('end'),'source_coverage_hash':coverage.get('evidence_hash'),
        'notice':'Historical initialization inventory includes closed accounts. Per-account history, ordering and snapshot reconciliation are still required.'}


def collect_account_histories(inventory,seed_observations,rpc,*,capture=None,max_accounts=2):
    from .history import collect_history
    if type(max_accounts) is not int or not 1<=max_accounts<=8:raise ValueError('Invalid account history budget')
    result={'all_required_account_queries_verified':False,'transfer_history_complete':False,
            'eligible_for_trading':False,'queries':[],'reasons':[],'observations':[]}
    if inventory.get('initialization_inventory_verified') is not True:
        result['reasons']=['HISTORICAL_ACCOUNT_INVENTORY_UNVERIFIED'];return result
    mint=address(inventory['mint']);start,end=inventory['history_start'],inventory['history_end']
    if type(start) is not int or type(end) is not int or not 0<=start<end:raise ValueError('Invalid inventory history range')
    frontier=[address(x['address']) for x in inventory['accounts']]
    if not frontier or len(frontier)!=len(set(frontier)):raise ValueError('Empty or duplicate historical accounts')
    seen={};conflicts=set();reasons=result['reasons']
    def merge(observations):
        for obs in observations:
            key=obs['signature']
            if key in conflicts:continue
            if key in seen and seen[key]['payload_hash']!=obs['payload_hash']:
                reasons.append('ACCOUNT_HISTORY_CONFLICTING_TRANSACTION');conflicts.add(key);seen.pop(key);continue
            seen[key]=obs
            if any(row['mint']==mint and row['account'] not in frontier for row in obs.get('token_deltas',[])):
                reasons.append('HISTORICAL_ACCOUNT_FRONTIER_NOT_CLOSED')
            if {'INNER_INSTRUCTIONS_UNAVAILABLE','UNDECODED_TOKEN_INSTRUCTION'}&set(obs.get('limitations',[])):
                reasons.append('ACCOUNT_HISTORY_TOKEN_DECODING_INCOMPLETE')
    merge(seed_observations)
    for key in frontier[:max_accounts]:
        try:
            observations,coverage=collect_history(key,start,end,rpc,max_pages=1,capture=capture,token_accounts='none')
            result['queries'].append(coverage);merge(observations)
            if not coverage['query_coverage_verified'] or not coverage['raw_pages_persisted']:
                reasons.append('ACCOUNT_HISTORY_QUERY_UNVERIFIED')
            reasons.extend(coverage['reasons'])
        except (ValueError,KeyError,TypeError,IndexError,OSError):
            reasons.append('ACCOUNT_HISTORY_QUERY_FAILED')
            result['queries'].append({'address':key,'query_coverage_verified':False})
    result['unqueried_accounts']=frontier[max_accounts:]
    if result['unqueried_accounts']:reasons.append('ACCOUNT_HISTORY_REQUEST_BUDGET_EXHAUSTED')
    result['all_required_account_queries_verified']=not reasons
    result['reasons']=sorted(set(reasons))
    result['observations']=sorted(seen.values(),key=lambda o:(o['slot'],o['signature']))
    from .reconcile import reconcile_movements
    reconciled=[{'signature':o['signature'],**reconcile_movements(mint,o)} for o in result['observations']]
    relevant=[r for r in reconciled if r['accounts'] or r['reasons']]
    result['movement_reconciliation']={'passed':bool(relevant) and all(r['passed'] for r in relevant),'transactions':len(relevant),
        'failures':[r for r in relevant if not r['passed']]}
    from .continuity import reconcile_history
    from .ordering import collect_ordering
    result['block_ordering']=collect_ordering(mint,result['observations'],rpc,capture=capture,max_blocks=2)
    result['account_continuity']=reconcile_history(mint,result['observations'],ordering=result['block_ordering'])
    result['notice']='One page per account within the existing request budget. Query coverage does not yet prove balance/supply reconciliation or bundle safety.'
    return result
