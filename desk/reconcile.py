"""Explain observed token balance changes from explicit SPL movements."""
from collections import defaultdict
from .decode import integer
from .programs import address
from .security import TOKEN_PROGRAM,TOKEN_2022


def reconcile_movements(mint,observation):
    address(mint)
    from .providers import SOL
    if mint==SOL:return {'passed':False,'accounts':0,'reasons':['WRAPPED_SOL_RECONCILIATION_UNSUPPORTED'],'notice':'Native wrapping requires lamport/rent accounting; this checker covers token movements only.'}
    if observation.get('status')=='FAILED':return {'passed':True,'accounts':0,'reasons':[],'notice':'Failed transaction has no committed effects.'}
    reasons=[];identities=defaultdict(set);programs=defaultdict(set);states={};flow=defaultdict(int)
    if observation.get('status')!='OBSERVED' or observation.get('commitment')!='finalized_provider_response':reasons.append('MOVEMENTS_NOT_FINALIZED')
    if {'INNER_INSTRUCTIONS_UNAVAILABLE','UNDECODED_TOKEN_INSTRUCTION','TOKEN_BALANCES_MISSING'}&set(observation.get('limitations',[])):
        reasons.append('MOVEMENT_DECODING_INCOMPLETE')
    if observation.get('token_control_operations'):
        reasons.append('UNSUPPORTED_TOKEN_CONTROL_OPERATION')
    initialized={x['account'] for x in observation.get('token_account_initializations',[]) if x['mint']==mint}
    closed={x['account'] for x in observation.get('token_account_closures',[])}
    for row in observation.get('token_deltas',[]):
        key=address(row['account']);identities[key].add(row['mint']);programs[key].add(row.get('program'))
        if row['mint']!=mint:continue
        if key in states:reasons.append('MOVEMENT_ACCOUNT_IDENTITY_AMBIGUOUS');continue
        if row.get('program') not in (TOKEN_PROGRAM,TOKEN_2022):reasons.append('MOVEMENT_TOKEN_PROGRAM_UNKNOWN')
        pre,post=row.get('pre_raw'),row.get('post_raw')
        if pre is None and key not in initialized:reasons.append('MOVEMENT_PRE_BALANCE_UNKNOWN')
        if post is None and key not in closed:reasons.append('MOVEMENT_POST_BALANCE_UNKNOWN')
        states[key]=(integer(pre) if pre is not None else 0,integer(post) if post is not None else 0)
    for row in observation.get('token_account_initializations',[]):
        if row['mint']==mint:identities[row['account']].add(mint);programs[row['account']].add(row['program'])
    seen=set()
    for row in observation.get('transfers',[]):
        if row.get('asset')!='TOKEN':continue
        src,dst=row['source'],row['destination'];involved=identities[src]|identities[dst]
        if mint not in involved and row.get('mint')!=mint:continue
        path=row.get('instruction')
        if path in seen:reasons.append('MOVEMENT_DUPLICATE_INSTRUCTION');continue
        seen.add(path)
        if identities[src]!={mint} or identities[dst]!={mint} or row.get('mint') not in (None,mint):
            reasons.append('MOVEMENT_TRANSFER_MINT_UNRESOLVED');continue
        if programs[src]!={row.get('program')} or programs[dst]!={row.get('program')}:reasons.append('MOVEMENT_TRANSFER_PROGRAM_MISMATCH')
        amount=integer(row['amount_raw']);flow[src]-=amount;flow[dst]+=amount
    for row in observation.get('token_supply_changes',[]):
        if row['mint']!=mint:continue
        path=row.get('instruction')
        if path in seen:reasons.append('MOVEMENT_DUPLICATE_INSTRUCTION');continue
        seen.add(path);amount=integer(row['amount_raw'])
        if row['direction'] not in ('mint','burn'):raise ValueError('Unknown supply movement')
        flow[row['account']]+=amount if row['direction']=='mint' else -amount
    for key in states.keys()|flow.keys()|initialized:
        if key in states:pre,post=states[key]
        elif key in initialized and key in closed:pre,post=0,0
        else:reasons.append('MOVEMENT_ACCOUNT_BALANCE_MISSING');continue
        if post-pre!=flow[key]:reasons.append('TOKEN_MOVEMENT_BALANCE_MISMATCH')
    return {'passed':not reasons,'accounts':len(states.keys()|flow.keys()|initialized),
        'reasons':sorted(set(reasons)),'notice':'Per-transaction raw balance reconciliation only; does not establish complete history, authority safety or common ownership.'}
