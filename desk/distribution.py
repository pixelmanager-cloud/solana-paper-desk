"""Observed token-account transfers resolved to owners; never ownership proof."""
from collections import defaultdict
from decimal import Decimal
from .programs import address
from .decode import integer
from .security import TOKEN_PROGRAM,TOKEN_2022
from .model import digest


def resolve_transfers(mint,observations):
    address(mint);edges=[];reasons=[];seen={}
    for obs in observations:
        if obs.get('status')=='FAILED':continue
        if obs.get('status')!='OBSERVED' or obs.get('commitment')!='finalized_provider_response':
            reasons.append('DISTRIBUTION_UNFINALIZED_OBSERVATION');continue
        signature=obs['signature'];fingerprint=digest(obs)
        if signature in seen:
            reasons.append('DISTRIBUTION_DUPLICATE_OBSERVATION' if seen[signature]==fingerprint else 'DISTRIBUTION_CONFLICTING_OBSERVATION');continue
        seen[signature]=fingerprint
        slot=obs.get('slot');at=obs.get('block_time')
        if type(slot) is not int or slot<0 or type(at) is not int or at<0:
            reasons.append('DISTRIBUTION_CHAIN_TIME_MISSING');continue
        identities=defaultdict(set)
        for row in obs.get('token_deltas',[]):
            # Retain conflicting identities instead of choosing pre/post arbitrarily.
            key=address(row['account']);owner=row.get('owner');program=row.get('program');asset=row.get('mint')
            if owner is not None:address(owner)
            if asset is not None:address(asset)
            identities[key].add((asset,owner,program))
        paths=set()
        for row in obs.get('transfers',[]):
            if row.get('asset')!='TOKEN':continue
            source=address(row['source']);destination=address(row['destination'])
            left,right=identities[source],identities[destination]
            potentially_target=row.get('mint')==mint or any(x[0]==mint for x in left|right)
            if not potentially_target:continue
            path=row.get('instruction')
            if not isinstance(path,str) or not path or any(not x.isdigit() for x in path.split('.')):
                reasons.append('DISTRIBUTION_INSTRUCTION_ORDER_MISSING');continue
            if path in paths:reasons.append('DISTRIBUTION_DUPLICATE_TRANSFER');continue
            paths.add(path)
            if len(left)!=1 or len(right)!=1:
                reasons.append('DISTRIBUTION_ENDPOINT_IDENTITY_AMBIGUOUS');continue
            a,b=next(iter(left)),next(iter(right))
            if (a[0]!=mint or b[0]!=mint or not a[1] or not b[1] or a[2]!=b[2] or a[2]!=row.get('program')
                    or a[2] not in (TOKEN_PROGRAM,TOKEN_2022) or row.get('mint') not in (None,mint)):
                reasons.append('DISTRIBUTION_ENDPOINT_IDENTITY_MISMATCH');continue
            amount=integer(row['amount_raw'])
            if amount>=2**64:raise ValueError('Transfer amount exceeds token layout')
            if not amount or a[1]==b[1]:continue
            edges.append({'source':a[1],'destination':b[1],'source_account':source,'destination_account':destination,
                'mint':mint,'amount_raw':str(amount),'slot':slot,'block_time':at,'signature':signature,'instruction':path,
                'source_kind':'unknown','destination_kind':'unknown','identity_source':'SAME_TRANSACTION_TOKEN_BALANCES'})
    return {'edges':edges,'reasons':sorted(set(reasons)),'history_complete':False,
            'notice':'Resolved token-account owners are not verified private-wallet or common-control labels.'}


def trace_distribution(mint,supply,observations,early_wallets,holder_accounts,vault_accounts=(),*,ordering=None):
    if type(supply) is not int or supply<=0:raise ValueError('Positive raw supply required')
    resolved=resolve_transfers(mint,observations);reasons=list(resolved['reasons']);vaults=set(vault_accounts)
    for key in vaults:address(key)
    if not isinstance(early_wallets,dict):raise ValueError('Early intent positions required')
    roots=set(early_wallets)
    for key,position in early_wallets.items():
        address(key)
        if type(position.get('slot')) is not int or not isinstance(position.get('signature'),str) or not isinstance(position.get('instruction'),str):raise ValueError('Invalid early intent position')
    totals=defaultdict(int);seen=set()
    for h in holder_accounts:
        key=address(h['address']);wallet=address(h['wallet'])
        if key in seen:raise ValueError('Duplicate current holder account')
        seen.add(key)
        if key not in vaults:totals[wallet]+=integer(h['amount_raw'])
    if sum(totals.values())>supply:raise ValueError('Holder amount exceeds mint supply')
    grouped=defaultdict(list);excluded=0
    for edge in resolved['edges']:
        if edge['source_account'] in vaults or edge['destination_account'] in vaults:
            excluded+=1;continue
        grouped[(edge['source'],edge['destination'])].append(edge)
    block_positions={p['slot']:p['positions'] for p in (ordering or {}).get('proofs',[]) if p.get('verified') is True and p.get('evidence_hash')}
    def earlier(left,right):
        if left['slot']!=right['slot']:return left['slot']<right['slot']
        if left['signature']==right['signature'] and left['signature'] is not None:
            return tuple(map(int,left['instruction'].split('.')))<tuple(map(int,right['instruction'].split('.')))
        indexes=block_positions.get(left['slot'],{})
        return left['signature'] in indexes and right['signature'] in indexes and indexes[left['signature']]<indexes[right['signature']]
    reached={w:early_wallets[w] for w in roots};frontier=dict(reached);links=[]
    for depth in range(1,4):
        next_frontier={}
        for (source,destination),edges in sorted(grouped.items()):
            if source not in frontier:continue
            prior=frontier[source];eligible=[]
            for edge in edges:
                if prior is not None:
                    if edge['slot']<prior['slot']:continue
                    if edge['slot']==prior['slot']:
                        if edge['signature']!=prior['signature']:
                            indexes=block_positions.get(edge['slot'],{})
                            if edge['signature'] not in indexes or prior['signature'] not in indexes:
                                reasons.append('DISTRIBUTION_SAME_SLOT_ORDER_UNKNOWN');continue
                            if indexes[edge['signature']]<=indexes[prior['signature']]:continue
                        else:
                            order=lambda e:tuple(map(int,e['instruction'].split('.')))
                            if order(edge)<=order(prior):continue
                eligible.append(edge)
            amount=sum(int(e['amount_raw']) for e in eligible)
            if amount*200<supply:continue  # aggregate split sends before the 0.5% floor
            # Advance only when cumulative transfers reach the materiality floor.
            cumulative=0;first=None
            for candidate in sorted(eligible,key=lambda e:(e['slot'],block_positions.get(e['slot'],{}).get(e['signature'],0),tuple(map(int,e['instruction'].split('.'))))):
                cumulative+=int(candidate['amount_raw'])
                if cumulative*200>=supply:
                    first=dict(candidate);break
            same_slot={e['signature'] for e in eligible if e['slot']==first['slot']}
            if len(same_slot)>1 and not same_slot<=block_positions.get(first['slot'],{}).keys():first['signature']=None
            links.append({'source':source,'destination':destination,'depth':depth,'amount_raw':str(amount),
                'gross_supply_pct':str(Decimal(amount)*100/supply),'evidence':[{'signature':e['signature'],'instruction':e['instruction']} for e in eligible],
                'classification':'OBSERVED_TRANSFER_PATH_NOT_COMMON_CONTROL'})
            # A later-discovered path may reach the same wallet earlier. Keep
            # that arrival for the next depth; never mutate this depth's seeds.
            if destination not in reached or earlier(first,reached[destination]):
                reached[destination]=first
                if destination not in next_frontier or earlier(first,next_frontier[destination]):next_frontier[destination]=first
        frontier=next_frontier
    if any(a in frontier and b not in reached for a,b in grouped):reasons.append('DISTRIBUTION_DEPTH_LIMIT')
    # Pair-wise materiality alone misses many sub-threshold recipients. Flag the
    # combined outflow without claiming that recipients share private control.
    fragments=defaultdict(list)
    for (source,destination),edges in grouped.items():
        if source not in reached:continue
        after=[]
        for edge in edges:
            prior=reached[source]
            if earlier(prior,edge):after.append(edge)
            elif edge['slot']==prior['slot'] and edge['signature']!=prior['signature']:
                indexes=block_positions.get(edge['slot'],{})
                if edge['signature'] not in indexes or prior['signature'] not in indexes:
                    reasons.append('DISTRIBUTION_SAME_SLOT_ORDER_UNKNOWN')
        amount=sum(int(e['amount_raw']) for e in after)
        if 0<amount*200<supply:
            fragments[source].append({'destination':destination,'amount_raw':str(amount),
                'evidence':[{'signature':e['signature'],'instruction':e['instruction']} for e in after]})
    fanouts=[]
    for source,recipients in sorted(fragments.items()):
        amount=sum(int(r['amount_raw']) for r in recipients)
        if len(recipients)>=2 and amount*200>=supply:
            reasons.append('FRAGMENTED_DISTRIBUTION_REQUIRES_REVIEW')
            fanouts.append({'source':source,'recipients':recipients,'amount_raw':str(amount),
                'gross_supply_pct':str(Decimal(amount)*100/supply),
                'classification':'SPLIT_OUTFLOW_NOT_COMMON_CONTROL_PROOF'})
    linked=sum(totals.get(w,0) for w in reached)
    if links:reasons.append('DISTRIBUTION_WALLET_CLASSIFICATION_REQUIRED')
    return {'history_complete':False,'eligible_for_trading':False,'reasons':sorted(set(reasons)),
        'fragmented_outflows':fanouts,'resolved_transfer_count':len(resolved['edges']),'excluded_verified_vault_transfers':excluded,'links':links,
        'reached_wallets':sorted(reached),'observed_reached_current_gross_supply_pct':str(Decimal(linked)*100/supply),
        'notice':'Potential distribution paths from observed early-buy intents. Partial history and unknown service labels prevent a bundle verdict or safety pass.'}
