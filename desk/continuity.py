"""Conservative account lifetime/balance continuity across finalized observations."""
from collections import defaultdict
from .decode import integer
from .programs import address
from .reconcile import reconcile_movements


def reconcile_history(mint, observations, *, ordering=None):
    address(mint)
    reasons=set(); timelines=defaultdict(list); seen=set(); transactions=0
    for obs in observations:
        if obs.get('status')=='FAILED':continue
        signature=obs['signature'];slot=obs['slot']
        if signature in seen:
            reasons.add('HISTORY_DUPLICATE_TRANSACTION');continue
        seen.add(signature)
        if type(slot) is not int or slot<0:raise ValueError('Invalid history slot')
        movement=reconcile_movements(mint,obs)
        if not movement['passed']:reasons.add('HISTORY_MOVEMENTS_UNRECONCILED')
        rows=defaultdict(list)
        for row in obs.get('token_deltas',[]):
            if row['mint']==mint:rows[address(row['account'])].append(row)
        initialized=defaultdict(list)
        for row in obs.get('token_account_initializations',[]):
            if row['mint']==mint:initialized[address(row['account'])].append(row)
        closed={x['account'] for x in obs.get('token_account_closures',[])}
        if rows or initialized:transactions+=1
        for key in rows.keys()|initialized.keys():
            if len(rows[key])>1 or len(initialized[key])>1:
                reasons.add('HISTORY_ACCOUNT_IDENTITY_AMBIGUOUS');continue
            row=rows[key][0] if rows[key] else None
            init=initialized[key][0] if initialized[key] else None
            # A transient initialized-and-closed account can be absent from both
            # balance arrays. Its lifetime is still relevant to completeness.
            if row is None and (init is None or key not in closed):
                reasons.add('HISTORY_ACCOUNT_BALANCE_MISSING');continue
            owner=(row or init).get('owner');program=(row or init).get('program')
            if not owner:reasons.add('HISTORY_OWNER_UNKNOWN')
            else:address(owner)
            if init and (init.get('owner')!=owner or init.get('program')!=program):
                reasons.add('HISTORY_INITIALIZATION_IDENTITY_MISMATCH')
            pre=row.get('pre_raw') if row else None;post=row.get('post_raw') if row else None
            if init and pre is not None:reasons.add('HISTORY_REINITIALIZATION_AMBIGUOUS')
            if key in closed and post is not None:reasons.add('HISTORY_CLOSURE_AMBIGUOUS')
            timelines[key].append({'slot':slot,'signature':signature,'owner':owner,'program':program,
                'pre_raw':integer(pre) if pre is not None else None,
                'post_raw':integer(post) if post is not None else None,
                'initialized':bool(init),'closed':key in closed})
    proofs={p['slot']:p['positions'] for p in (ordering or {}).get('proofs',[]) if p.get('verified') is True and p.get('evidence_hash')}
    ends=[]
    for key,events in sorted(timelines.items()):
        events.sort(key=lambda e:e['slot'])
        # Slot numbers cannot order separate transactions within a block. Never
        # use signature order or matching balances to manufacture that proof.
        grouped=defaultdict(list)
        for event in events:grouped[event['slot']].append(event)
        ambiguous=False
        for slot,group in grouped.items():
            if len(group)>1 and any(e['signature'] not in proofs.get(slot,{}) for e in group):ambiguous=True
        if ambiguous:
            reasons.add('HISTORY_SAME_SLOT_ORDER_UNKNOWN');continue
        events.sort(key=lambda e:(e['slot'],proofs.get(e['slot'],{}).get(e['signature'],0)))
        previous=None
        for event in events:
            if previous is None:
                if not event['initialized'] or event['pre_raw'] is not None:
                    reasons.add('HISTORY_INITIAL_STATE_UNKNOWN')
            elif previous['closed']:
                if not event['initialized'] or event['pre_raw'] is not None:
                    reasons.add('HISTORY_ACCOUNT_REOPEN_UNWITNESSED')
            else:
                if event['initialized']:reasons.add('HISTORY_REINITIALIZED_OPEN_ACCOUNT')
                if event['pre_raw']!=previous['post_raw']:
                    reasons.add('HISTORY_BALANCE_DISCONTINUITY')
                if (event['owner'],event['program'])!=(previous['owner'],previous['program']):
                    reasons.add('HISTORY_ACCOUNT_CONTROL_CHANGED')
            previous=event
        last=events[-1]
        ends.append({'account':key,'slot':last['slot'],'signature':last['signature'],
            'owner':last['owner'],'program':last['program'],'closed':last['closed'],
            'amount_raw':str(last['post_raw']) if last['post_raw'] is not None else None})
    if not timelines:reasons.add('HISTORY_NO_ACCOUNT_LIFETIMES')
    return {'passed':not reasons,'reasons':sorted(reasons),'accounts':len(timelines),
        'transactions':transactions,'end_states':ends,'transfer_history_complete':False,
        'eligible_for_trading':False,
        'notice':'Observed account lifetimes only. Exhausted query coverage, canonical transaction order, current snapshot reconciliation and authority/funding classification remain separate requirements.'}
