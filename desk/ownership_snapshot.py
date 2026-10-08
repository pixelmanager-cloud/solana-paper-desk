"""Match fully replayed historical account endings to one finalized bank snapshot."""
from .security import account_bytes,base58,mint_policy,holding_policy


def validate_bank(mint,accounts,snapshot):
    keys,options=snapshot['params'];result=snapshot['result'];slot=result['context']['slot']
    if (snapshot['method']!='getMultipleAccounts' or options!={'encoding':'base64','commitment':'finalized'}
            or not keys or keys[0]!=mint or len(keys)>100 or len(keys)!=len(set(keys))
            or set(keys[1:])!=set(accounts) or len(result['value'])!=len(keys)
            or type(slot) is not int or slot<0 or not isinstance(result['value'],list)):
        raise ValueError('Complete finalized historical-account snapshot required')
    if mint_policy(result['value'][0])['decision']!='PASS_TOKEN_POLICY':
        raise ValueError('Snapshot token policy failed')
    return slot


def reconcile_snapshot(mint,history,snapshot,block_time):
    reasons=[];observed=0
    if not history.get('query_coverage_verified'):reasons.append('MINT_HISTORY_NOT_EXHAUSTED')
    inventory=history['inventory'];queries=history['account_queries'];continuity=history['account_continuity']
    if not inventory.get('initialization_inventory_verified'):reasons.append('ACCOUNT_INVENTORY_UNVERIFIED')
    if queries['reasons'] or queries['verified']!=queries['required']:reasons.append('ACCOUNT_QUERIES_INCOMPLETE')
    if not continuity['passed'] or not history['observed_movements']['passed'] or history['block_ordering']['reasons']:
        reasons.append('HISTORY_MOVEMENTS_OR_ORDER_UNVERIFIED')
    frontier={a['address'] for a in inventory['accounts']}
    params=snapshot['params'];keys,options=params;values=snapshot['result']['value'];slot=snapshot['result']['context']['slot']
    if (snapshot['method']!='getMultipleAccounts' or options.get('encoding')!='base64'
            or options.get('commitment')!='finalized' or not keys or keys[0]!=mint
            or len(keys)!=len(set(keys)) or set(keys[1:])!=frontier or len(values)!=len(keys)
            or type(slot) is not int or slot<0):raise ValueError('Complete finalized historical-account snapshot required')
    if block_time['method']!='getBlockTime' or block_time['params']!=[slot]:raise ValueError('Snapshot block time binding mismatch')
    at=block_time['result']
    if type(at) is not int or at<0:raise ValueError('Snapshot block time unavailable')
    if history.get('slot_range')!={'gte':0,'lt':slot+1}:reasons.append('HISTORY_SNAPSHOT_SLOT_BOUNDARY_UNVERIFIED')
    endings={row['account']:row for row in continuity['end_states']}
    if set(endings)!=frontier or len(endings)!=len(continuity['end_states']):reasons.append('ACCOUNT_ENDING_COVERAGE_MISMATCH')
    policy=mint_policy(values[0]);supply=int(policy.get('supply_raw',0))
    if policy['decision']!='PASS_TOKEN_POLICY':reasons.append('SNAPSHOT_TOKEN_POLICY_FAILED')
    owners={}
    for key,value in zip(keys[1:],values[1:]):
        end=endings.get(key)
        if end is None:continue
        if end['slot']>slot:reasons.append('HISTORY_ENDS_AFTER_SNAPSHOT')
        if end['closed']:
            if value is not None:reasons.append('CLOSED_ACCOUNT_REAPPEARED')
            if end['amount_raw'] is not None:reasons.append('CLOSED_ACCOUNT_END_BALANCE_INVALID')
            continue
        if value is None:reasons.append('OPEN_ACCOUNT_MISSING');continue
        checked=holding_policy(value,mint,end['owner'])
        if checked['decision']!='PASS_HOLDING_POLICY':reasons.extend(checked['reasons']);continue
        amount=int(checked['amount_raw']);observed+=amount
        if end['program']!=value['owner'] or str(amount)!=end['amount_raw']:reasons.append('HISTORY_SNAPSHOT_BALANCE_OR_CONTROL_MISMATCH')
        owner=base58(account_bytes(value)[32:64]);owners[owner]=owners.get(owner,0)+amount
    if observed!=supply or supply<=0:reasons.append('SNAPSHOT_SUPPLY_NOT_RECONCILED')
    return {'reconciled':not reasons,'reasons':sorted(set(reasons)),'slot':slot,'block_time':at,
            'supply_raw':str(supply),'observed_supply_raw':str(observed),'holder_totals_raw':{w:str(n) for w,n in sorted(owners.items())},
            'common_control_verified':False,'eligible_for_trading':False,
            'notice':'Historical accounting at a finalized snapshot, not proof of common ownership or current entry freshness.'}


def replay_snapshot(report,store,snapshot_hash,block_time_hash):
    from .replay_history import reconstruct_launch_history
    history=reconstruct_launch_history(report,store)
    result=reconcile_snapshot(report['mint'],history,store.load(snapshot_hash),store.load(block_time_hash))
    return {**result,'snapshot_hash':snapshot_hash,'block_time_hash':block_time_hash}
