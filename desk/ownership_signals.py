"""Observed pre-buy funding groups reconstructed without private-owner assertions."""
from collections import defaultdict
from .replay_history import replay_history
from .funding import observed_funding


def funding_groups(mint,observations,queries,store,ordering,launch_slot):
    first={}
    for obs in sorted(observations,key=lambda o:o['slot']):
        for ix in obs.get('program_observations',[]):
            if (ix.get('mint')!=mint or ix.get('kind')!='BUY_INTENT' or ix.get('status')!='IDENTIFIED'
                    or not ix.get('wallet') or not 0<=obs['slot']-launch_slot<=3):continue
            wallet=ix['wallet']
            buy=first.setdefault(wallet,{'slot':obs['slot'],'block_time':obs['block_time'],'signature':obs['signature'],
                                         'commitment':obs.get('commitment'),'same_slot_buy_signatures':[]})
            if obs['slot']==buy['slot'] and obs['signature'] not in buy['same_slot_buy_signatures']:
                buy['same_slot_buy_signatures'].append(obs['signature'])
    reasons=[];edges=[];verified=[];hashes=[];ambiguities=[]
    for wallet,buy in sorted(first.items()):
        at=buy['block_time']
        if type(at) is not int:reasons.append('EARLY_BUY_TIME_UNVERIFIED');continue
        matches=[q for q in queries if q.get('address')==wallet and q.get('start')==max(0,at-3600)
                 and q.get('end')==at+1 and q.get('token_accounts_filter')=='balanceChanged']
        if len(matches)!=1:reasons.append('EARLY_BUYER_FUNDING_QUERY_MISSING_OR_AMBIGUOUS');continue
        try:rows,coverage=replay_history(matches[0],store)
        except (ValueError,KeyError,TypeError,IndexError):reasons.append('EARLY_BUYER_FUNDING_RAW_REPLAY_FAILED');continue
        hashes.append(coverage['evidence_hash'])
        if coverage['query_coverage_verified']:verified.append(wallet)
        else:reasons.append('EARLY_BUYER_FUNDING_QUERY_INCOMPLETE')
        result=observed_funding(rows,wallet,buy,ordering=ordering)
        reasons.extend(result['reasons']);ambiguities.extend(result['ambiguous_transfers'])
        edges.extend({**edge,'query_evidence_hash':coverage['evidence_hash']} for edge in result['edges'])
    groups=defaultdict(set)
    for edge in edges:groups[edge['source']].add(edge['destination'])
    if edges:reasons.append('FUNDING_SOURCE_SERVICE_CLASSIFICATION_REQUIRED')
    if not first:reasons.append('EARLY_BUY_COHORT_NOT_OBSERVED')
    return {'early_wallets':sorted(first),'verified_window_wallets':verified,'query_hashes':hashes,
            'edges':edges,'ambiguous_transfers':ambiguities,
            'shared_sources':[{'source':source,'wallets':sorted(wallets),'classification':'UNKNOWN_SERVICE_OR_PRIVATE_SOURCE'}
                              for source,wallets in sorted(groups.items()) if len(wallets)>1],
            'reasons':sorted(set(reasons)),'funding_history_complete':False,'common_control_verified':False,
            'notice':'One-hour observed funding windows for witnessed launch-cohort buyers. Shared sources are risk candidates, not proven controllers.'}
