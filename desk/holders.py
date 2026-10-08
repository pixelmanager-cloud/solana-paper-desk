"""Bounded indexed holder enumeration with explicit coverage and snapshot checks."""
from collections import defaultdict
from decimal import Decimal
from .programs import address
from .decode import integer


def enumerate_holders(mint, supply, mint_slot, rpc, max_pages=3, page_size=1000):
    address(mint)
    if type(supply) is not int or not 0<supply<2**64 or type(mint_slot) is not int or mint_slot<0:
        raise ValueError('Valid mint supply and context slot required')
    if type(max_pages) is not int or type(page_size) is not int or not 1<=max_pages<=10 or not 1<=page_size<=1000:raise ValueError('Invalid holder request budget')
    owners=defaultdict(int);accounts={};slots=[];reasons=[];finished=False
    for page in range(1,max_pages+1):
        response=rpc('getTokenAccounts',{'mint':mint,'page':page,'limit':page_size,'options':{'showZeroBalance':False}})
        rows=response.get('token_accounts');slot=response.get('last_indexed_slot')
        if not isinstance(rows,list) or len(rows)>page_size:raise ValueError('Malformed holder page')
        if type(slot) is not int or slot<0:reasons.append('INDEXED_SLOT_MISSING')
        else:slots.append(slot)
        for row in rows:
            key=address(row['address']);owner=address(row['owner'])
            if row.get('mint')!=mint:raise ValueError('Holder mint mismatch')
            amount=integer(row['amount'])
            if amount>=2**64:raise ValueError('Invalid SPL balance')
            if key in accounts:
                reasons.append('DUPLICATE_HOLDER_ACCOUNT');continue
            if type(row.get('frozen')) is not bool:reasons.append('HOLDER_STATE_MISSING')
            delegated=None
            if row.get('delegated_amount') is None:reasons.append('HOLDER_DELEGATION_STATE_MISSING')
            else:
                delegated=integer(row['delegated_amount'])
                if delegated>=2**64:raise ValueError('Invalid delegated SPL amount')
            accounts[key]={'address':key,'wallet':owner,'amount_raw':str(amount),
                           'frozen':row.get('frozen'),'delegated_raw':str(delegated) if delegated is not None else None}
            owners[owner]+=amount
        if len(rows)<page_size:
            finished=True;break
    if not finished:reasons.append('HOLDER_PAGE_BUDGET_EXHAUSTED')
    total=sum(owners.values())
    if total!=supply:reasons.append('HOLDER_SUPPLY_NOT_RECONCILED')
    if slots and (max(slots)-min(slots)>16 or max(abs(s-mint_slot) for s in slots)>32):
        reasons.append('HOLDER_SNAPSHOT_SLOT_DRIFT')
    complete=finished and not reasons
    return {'mint':mint,'enumeration_complete':finished,'coverage_verified':complete,
            'snapshot_atomic':False,'coverage_pct':str(Decimal(total)*100/supply),'supply_raw':str(supply),
            'observed_supply_raw':str(total),'account_count':len(accounts),'owner_count':len(owners),
            'indexed_slot_min':min(slots) if slots else None,'indexed_slot_max':max(slots) if slots else None,
            'reasons':sorted(set(reasons)),'accounts':list(accounts.values()),
            'holders':[{'wallet':w,'amount_raw':str(n),'gross_supply_pct':str(Decimal(n)*100/supply)}
                       for w,n in sorted(owners.items(),key=lambda x:(-x[1],x[0]))],
            'notice':'Indexed enumeration, not an atomic chain snapshot. Verified coverage requires supply reconciliation and bounded slot drift.'}


def verify_holder_snapshot(enumeration,rpc,max_accounts=99,*,capture=None):
    """Recheck every enumerated legacy account and mint in one RPC bank context."""
    from .security import TOKEN_PROGRAM,account_bytes,base58,mint_policy
    if type(max_accounts) is not int or not 1<=max_accounts<=99:raise ValueError('Invalid atomic holder budget')
    result={'verified':False,'snapshot_atomic':False,'raw_evidence_persisted':False,'reasons':[],'delegated_accounts':[]}
    if enumeration.get('coverage_verified') is not True:
        result['reasons']=['INDEXED_HOLDER_COVERAGE_UNVERIFIED'];return result
    mint=address(enumeration['mint']);rows=enumeration['accounts'];keys=[address(r['address']) for r in rows]
    if not keys or len(keys)>max_accounts:
        result['reasons']=['ONCHAIN_HOLDER_SNAPSHOT_ACCOUNT_BUDGET'];return result
    if len(keys)!=len(set(keys)) or mint in keys:raise ValueError('Duplicate holder snapshot keys')
    indexed_slot=enumeration['indexed_slot_max']
    if type(indexed_slot) is not int or indexed_slot<0:raise ValueError('Indexed slot required')
    params=[[mint,*keys],{'encoding':'base64','commitment':'confirmed','minContextSlot':indexed_slot}]
    response=rpc('getMultipleAccounts',params)
    from .model import digest
    envelope={'method':'getMultipleAccounts','params':params,'result':response}
    if capture is not None:
        evidence_hash=capture(envelope)
        if evidence_hash!=digest(envelope):raise ValueError('Holder snapshot evidence hash mismatch')
        result.update(raw_evidence_persisted=True,evidence_hash=evidence_hash)
    else:result['reasons'].append('HOLDER_SNAPSHOT_EVIDENCE_NOT_PERSISTED')
    values=response.get('value');slot=response.get('context',{}).get('slot');reasons=result['reasons']
    if not isinstance(values,list) or len(values)!=len(keys)+1:
        result['reasons']=['ONCHAIN_HOLDER_SNAPSHOT_INCOMPLETE'];return result
    if type(slot) is not int or not 0<=slot-indexed_slot<=32:reasons.append('ONCHAIN_HOLDER_SNAPSHOT_SLOT_DRIFT')
    policy=mint_policy(values[0]);supply=int(policy.get('supply_raw',0))
    if policy['decision']!='PASS_TOKEN_POLICY':reasons.append('SNAPSHOT_MINT_POLICY_FAILED')
    if supply!=integer(enumeration['supply_raw']):reasons.append('SNAPSHOT_MINT_SUPPLY_CHANGED')
    total=0
    for row,value in zip(rows,values[1:]):
        if not value or value.get('owner')!=TOKEN_PROGRAM or value.get('executable') is not False:
            reasons.append('SNAPSHOT_HOLDER_PROGRAM_OR_ACCOUNT_MISSING');continue
        raw=account_bytes(value)
        if len(raw)!=165:reasons.append('SNAPSHOT_HOLDER_LAYOUT_INVALID');continue
        if base58(raw[:32])!=mint or base58(raw[32:64])!=row['wallet']:reasons.append('SNAPSHOT_HOLDER_IDENTITY_CHANGED')
        amount=int.from_bytes(raw[64:72],'little');delegated=int.from_bytes(raw[121:129],'little');flag=int.from_bytes(raw[72:76],'little')
        if flag not in (0,1):reasons.append('SNAPSHOT_DELEGATE_OPTION_INVALID')
        if flag==1:result['delegated_accounts'].append(row['address'])
        if raw[108] not in (1,2):reasons.append('SNAPSHOT_HOLDER_UNINITIALIZED')
        if amount!=integer(row['amount_raw']) or (raw[108]==2)!=row['frozen'] or delegated!=integer(row['delegated_raw']):
            reasons.append('INDEXED_HOLDER_STATE_CHANGED')
        total+=amount
    if total!=supply:reasons.append('ATOMIC_HOLDER_SUPPLY_NOT_RECONCILED')
    result.update({'verified':not reasons,'snapshot_atomic':True,'slot':slot,'account_count':len(keys),
        'observed_supply_raw':str(total),'reasons':sorted(set(reasons)),
        'notice':'Mint and enumerated legacy accounts read in one bank context; positive supply reconciliation supports current coverage, not historical completeness or wallet classification.'})
    return result
