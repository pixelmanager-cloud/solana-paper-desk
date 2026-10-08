"""Bounded finalized-block ordering evidence; no lexical signature ordering."""
from collections import defaultdict
from .programs import address, unbase58
from .model import digest


def block_order(slot, block, observations):
    if type(slot) is not int or slot<0:raise ValueError('Invalid block slot')
    if not isinstance(block,dict):raise ValueError('Block unavailable')
    address(block['blockhash']);address(block['previousBlockhash'])
    parent=block['parentSlot'];at=block.get('blockTime');signatures=block.get('signatures')
    if type(parent) is not int or not 0<=parent<slot:raise ValueError('Invalid parent slot')
    if type(at) is not int or at<0:raise ValueError('Block time unavailable')
    if not isinstance(signatures,list) or not 1<=len(signatures)<=20000:raise ValueError('Block signature budget/schema')
    if any(not isinstance(s,str) or not 64<=len(s)<=88 or len(unbase58(s))!=64 for s in signatures):raise ValueError('Invalid block signature')
    if len(set(signatures))!=len(signatures):raise ValueError('Duplicate block signature')
    positions={s:i for i,s in enumerate(signatures)};selected={}
    for obs in observations:
        if obs['slot']!=slot:continue
        if obs.get('commitment')!='finalized_provider_response' or obs.get('block_time')!=at:
            raise ValueError('Block/history finality or timestamp mismatch')
        if obs['signature'] not in positions:raise ValueError('Historical signature missing from block')
        selected[obs['signature']]=positions[obs['signature']]
    if not selected:raise ValueError('No observations in block')
    return {'slot':slot,'blockhash':block['blockhash'],'block_time':at,'positions':selected,
            'response_hash':digest(block),'verified':True}


def collect_ordering(mint,observations,rpc,*,capture=None,max_blocks=2):
    if type(max_blocks) is not int or not 0<=max_blocks<=2:raise ValueError('Invalid block ordering budget')
    touched=defaultdict(set)
    for obs in observations:
        if obs.get('status')=='FAILED':continue
        keys={r['account'] for r in obs.get('token_deltas',[]) if r['mint']==mint}
        keys|={r['account'] for r in obs.get('token_account_initializations',[]) if r['mint']==mint}
        for key in keys:touched[(key,obs['slot'])].add(obs['signature'])
    slots=sorted({slot for (_,slot),signatures in touched.items() if len(signatures)>1})
    proofs=[];reasons=[]
    for slot in slots[:max_blocks]:
        try:
            config={'commitment':'finalized','transactionDetails':'signatures','rewards':False,'maxSupportedTransactionVersion':1}
            block=rpc('getBlock',[slot,config]);proof=block_order(slot,block,observations)
            envelope={'method':'getBlock','slot':slot,'config':config,'result':block}
            key=capture(envelope) if capture else None
            if key!=digest(envelope):raise ValueError('Ordering evidence not persisted')
            proof['evidence_hash']=key;proofs.append(proof)
        except (ValueError,KeyError,TypeError,IndexError,OSError):reasons.append('BLOCK_ORDERING_UNVERIFIED')
    if len(slots)>max_blocks:reasons.append('BLOCK_ORDERING_BUDGET_EXHAUSTED')
    return {'proofs':proofs,'required_slots':slots,'reasons':sorted(set(reasons)),'verified':not reasons}
