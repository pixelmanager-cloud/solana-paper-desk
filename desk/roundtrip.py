"""Conservative buy then partial sell simulation against one sequential bank state."""
import time
from .providers import SOL,helius_rpc,jupiter_sequence_probe
from .programs import address
from .decode import integer
from .security import mint_policy,account_bytes
from .compile import compile_unsigned
from .sequence import simulate_sequence
from .effects import check_effects,check_account_controls


def simulate_roundtrip(mint,wallet,spend,rpc=helius_rpc,quote=jupiter_sequence_probe,capture=None):
    from solders.pubkey import Pubkey
    for key in (mint,wallet):address(key)
    if type(spend) is not int or not 0<spend<=100000000:raise ValueError('Diagnostic spend must be at most 0.1 SOL')
    if not Pubkey.from_string(wallet).is_on_curve():raise ValueError('Ordinary public wallet required')
    started=time.monotonic();before=rpc('getMultipleAccounts',[[wallet,mint],{'encoding':'base64','commitment':'confirmed'}])
    payer,mint_account=before['value']
    if not payer or payer.get('owner')!='11111111111111111111111111111111' or payer.get('executable') is not False or account_bytes(payer):raise ValueError('Invalid simulation payer')
    if integer(payer['lamports'])<spend+10000000:raise ValueError('Insufficient diagnostic payer headroom')
    if mint_policy(mint_account)['decision']!='PASS_TOKEN_POLICY':raise ValueError('Mint policy excludes roundtrip')
    buy=quote(SOL,mint,spend,wallet);b=buy['response']
    if b.get('inputMint')!=SOL or b.get('outputMint')!=mint or integer(b['inAmount'])!=spend:raise ValueError('Buy route identity mismatch')
    quantity=integer(b['otherAmountThreshold'])
    if not 0<quantity<2**64:raise ValueError('Buy minimum missing or invalid')
    sell=quote(mint,SOL,quantity,wallet);s=sell['response']
    if s.get('inputMint')!=mint or s.get('outputMint')!=SOL or integer(s['inAmount'])!=quantity:raise ValueError('Sell route identity mismatch')
    minimum=integer(s['otherAmountThreshold'])
    if not 0<minimum<2**64:raise ValueError('Sell minimum missing or invalid')
    blockhash=rpc('getLatestBlockhash',[{'commitment':'confirmed'}])['value']['blockhash']
    legs=[compile_unsigned(r,wallet,blockhash,rpc) for r in (b,s)]
    watch=list(dict.fromkeys(k for leg in legs for k in leg['keys']))
    sequence=simulate_sequence([leg['raw'] for leg in legs],watch,rpc)
    rows=sequence['result']['value'].get('transactionResults',[]);effects=[];controls=[];reasons=list(sequence['reasons'])
    if len(rows)==2:
        for i,(leg,row) in enumerate(zip(legs,rows)):
            # Balance metadata uses each transaction's resolved-key order; requested
            # account snapshots use the shared watchlist order. Map explicitly.
            post=row.get('postExecutionAccounts')
            if not isinstance(post,list) or len(post)!=len(watch):reasons.append('ROUNDTRIP_POST_STATES_MISSING');continue
            normalized={**row,'accounts':[post[watch.index(k)] for k in leg['keys']]}
            effect=check_effects(normalized,leg['keys'],wallet,mint,spend if i==0 else quantity,quantity if i==0 else minimum,direction='buy' if i==0 else 'sell')
            control=check_account_controls(normalized,leg['keys'],wallet,effect.get('owned_token_account_indices',[]),mint=mint)
            effects.append(effect);controls.append(control);reasons.extend(effect['reasons']);reasons.extend(control['reasons'])
    else:reasons.append('ROUNDTRIP_LEGS_MISSING')
    now=int(time.time());slot=sequence.get('slot')
    if (time.monotonic()-started>10 or any(type(x.get('observed_at')) is not int or not 0<=now-x['observed_at']<=10 for x in (buy,sell))
            or type(slot) is not int or not 0<=slot-before['context']['slot']<=32):reasons.append('ROUNDTRIP_EVIDENCE_STALE')
    residual=str(int(effects[0]['received_raw'])-int(effects[1]['sold_raw'])) if len(effects)==2 and all(x['passed'] for x in effects) else None
    native_delta=str(sum(int(x['native_wealth_delta_lamports']) for x in effects)) if residual is not None else None
    result={'residual_token_raw':residual,'net_native_wealth_delta_lamports':native_delta,'kind':'unsigned_buy_partial_sell_diagnostic','mint':mint,'wallet':wallet,'spend_lamports':str(spend),
        'sell_quantity_raw':str(quantity),'observed_at':now,'slot':slot,'effects_passed':not reasons,
        'reasons':sorted(set(reasons)),'leg_effects':effects,'leg_controls':controls,'sequence':sequence,
        'signed':False,'submitted':False,'eligible_for_trading':False,'transaction_policy_ok':False,
        'notice':'Sells only the buy minimum; surplus tokens may remain in simulated inventory. Complete instruction policy and wallet-specific evidence remain required. Not a trading approval.'}
    if capture:capture({'provenance':'UNSIGNED_UNSUBMITTED_MAINNET_BUY_PARTIAL_SELL','result':result,
        'legs':[{'keys':x['keys'],'outer':x['outer']} for x in legs],'quote_responses':[b,s]})
    return result
