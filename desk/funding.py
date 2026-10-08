"""Observed native funding before a buy; unknown ordering is never attribution."""
from collections import defaultdict
from .model import digest


def observed_funding(observations,wallet,buy,*,floor_lamports=1000000,ordering=None):
    if type(floor_lamports) is not int or floor_lamports<=0:raise ValueError('Positive funding floor required')
    bought=buy['block_time'];buy_slot=buy['slot']
    if type(bought) is not int or bought<0 or type(buy_slot) is not int or buy_slot<0:raise ValueError('Chain buy time and slot required')
    by_signature={};conflicts=set();reasons=[];grouped=defaultdict(list);ambiguous=[]
    positions=None;proof_hash=None;proof_conflict=False
    for proof in (ordering or {}).get('proofs',[]):
        if proof.get('slot')!=buy_slot:continue
        indexes=proof.get('positions');evidence=proof.get('evidence_hash')
        valid=(proof.get('verified') is True and proof.get('block_time')==bought
            and isinstance(evidence,str) and len(evidence)==64 and all(c in '0123456789abcdef' for c in evidence)
            and isinstance(indexes,dict) and bool(indexes)
            and all(isinstance(k,str) and type(v) is int and 0<=v<20000 for k,v in indexes.items())
            and len(set(indexes.values()))==len(indexes))
        if not valid or (positions is not None and (positions!=indexes or proof_hash!=evidence)):
            proof_conflict=True;reasons.append('FUNDING_BLOCK_PROOF_INVALID_OR_CONFLICTING')
        else:positions=indexes;proof_hash=evidence
    if proof_conflict:positions=None
    if buy.get('commitment')!='finalized_provider_response':
        return {'edges':[],'ambiguous_transfers':[],'reasons':['FUNDING_BUY_FINALITY_UNVERIFIED'],'funding_history_complete':False}

    for obs in observations:
        signature=obs['signature']
        if signature in by_signature and digest(obs)!=digest(by_signature[signature]):conflicts.add(signature)
        else:by_signature[signature]=obs
    if conflicts:reasons.append('FUNDING_CONFLICTING_OBSERVATIONS')
    for signature,obs in sorted(by_signature.items()):
        if signature in conflicts or obs.get('status')!='OBSERVED':continue
        if obs.get('commitment')!='finalized_provider_response':
            reasons.append('FUNDING_OBSERVATION_FINALITY_UNVERIFIED');continue
        at=obs.get('block_time');slot=obs.get('slot')
        if type(at) is not int or type(slot) is not int:
            reasons.append('FUNDING_CHAIN_TIME_UNKNOWN');continue
        if not max(0,bought-3600)<=at<=bought or slot>buy_slot:continue
        for transfer in obs.get('transfers',[]):
            if transfer.get('asset')!='SOL' or transfer.get('destination')!=wallet or transfer.get('source')==wallet:continue
            n=transfer.get('amount_raw')
            if not isinstance(n,str) or not n.isascii() or not n.isdigit() or not 0<int(n)<2**64:
                reasons.append('FUNDING_AMOUNT_INVALID');continue
            evidence={**transfer,'signature':signature,'slot':slot,'block_time':at,'source_kind':'unknown'}
            if slot==buy_slot:
                buy_signatures=buy.get('same_slot_buy_signatures',[buy.get('signature')])
                valid_buys=isinstance(buy_signatures,list) and bool(buy_signatures) and all(isinstance(x,str) for x in buy_signatures)
                if (positions is None or signature not in positions or not valid_buys
                        or any(s not in positions for s in buy_signatures)
                        or signature in buy_signatures or at!=bought):
                    ambiguous.append(evidence);reasons.append('FUNDING_SAME_SLOT_ORDER_UNVERIFIED');continue
                if positions[signature]>=min(positions[s] for s in buy_signatures):continue
                evidence['ordering_evidence_hash']=proof_hash
            grouped[transfer['source']].append(evidence)
    edges=[]
    for source,transfers in sorted(grouped.items()):
        # Aggregate fragments within this observed pre-buy window. This remains
        # a transfer relationship, never a private-controller classification.
        total=sum(int(t['amount_raw']) for t in transfers)
        if total<floor_lamports:continue
        edges.append({'source':source,'destination':wallet,'asset':'SOL','amount_raw':str(total),
            'source_kind':'unknown','ordering':'FINALIZED_BLOCK_POSITION' if any(t['slot']==buy_slot for t in transfers) else 'STRICTLY_EARLIER_SLOT','transfers':transfers,
            'fragmented':len(transfers)>1 and all(int(t['amount_raw'])<floor_lamports for t in transfers)})
    return {'edges':edges,'ambiguous_transfers':ambiguous,'reasons':sorted(set(reasons)),
        'funding_history_complete':False,'notice':'Bounded observed native transfers only. Same-slot ordering, service identity, indirect funding and earlier balances remain unresolved.'}
