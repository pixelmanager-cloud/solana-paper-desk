"""Unsigned diagnostic simulation. No private keys, signatures or broadcast methods."""
import base64
import hashlib
import time
from .providers import helius_rpc, jupiter_probe, SOL
from .programs import address
from .security import account_bytes, holding_policy, mint_policy, TOKEN_PROGRAM
from .model import digest


def simulate_sell(mint, wallet, holding, amount, rpc=helius_rpc, quote=jupiter_probe, capture=None, pool_snapshot=None):
    started_at=int(time.time())
    from solders.pubkey import Pubkey
    for value in (mint,wallet,holding):address(value)
    if type(amount) is not int or not 0<amount<2**64:raise ValueError('Invalid raw token amount')
    if not Pubkey.from_string(wallet).is_on_curve():raise ValueError('Simulation wallet must be an ordinary public key')
    before=rpc('getMultipleAccounts',[[wallet,holding,mint],{'encoding':'base64','commitment':'confirmed'}])
    payer,token,mint_account=before['value']
    if not payer or payer.get('owner')!='11111111111111111111111111111111' or payer.get('executable') is not False:
        raise ValueError('Simulation payer must be a funded system account')
    if mint_policy(mint_account)['decision']!='PASS_TOKEN_POLICY':raise ValueError('Mint policy does not permit this simulation')
    checked=holding_policy(token,mint,wallet)
    if checked['decision']!='PASS_HOLDING_POLICY' or int(checked['amount_raw'])<amount:
        raise ValueError('Holding unavailable, insufficient or unsafe')
    route=quote(mint,SOL,amount,wallet);response=route['response']
    if response.get('inputMint')!=mint or response.get('outputMint')!=SOL or int(response['inAmount'])!=amount:
        raise ValueError('Route identity mismatch')
    minimum_out=response.get('otherAmountThreshold')
    from .decode import integer
    minimum_out=integer(minimum_out)
    if not 0<minimum_out<2**64:raise ValueError('Route minimum output missing or invalid')
    from .compile import compile_unsigned
    blockhash=rpc('getLatestBlockhash',[{'commitment':'confirmed'}])['value']['blockhash']
    compiled=compile_unsigned(response,wallet,blockhash,rpc)
    raw,raw_instructions,resolved_keys=compiled['raw'],compiled['outer'],compiled['keys']
    outcome=rpc('simulateTransaction',[base64.b64encode(raw).decode(),{'encoding':'base64','sigVerify':False,
        'replaceRecentBlockhash':True,'commitment':'confirmed','innerInstructions':True,'minContextSlot':before['context']['slot'],
        'accounts':{'encoding':'base64','addresses':resolved_keys}}])
    value=outcome['value']
    if 'err' not in value:raise ValueError('Simulation result missing execution status')
    ok=value['err'] is None
    after=value.get('accounts') or []
    holding_index=resolved_keys.index(holding) if holding in resolved_keys else None
    post_policy=holding_policy(after[holding_index],mint,wallet) if holding_index is not None and len(after)>holding_index and after[holding_index] else None
    from .effects import check_effects, check_account_controls
    effects=check_effects(value,resolved_keys,wallet,mint,amount,minimum_out)
    controls=check_account_controls(value,resolved_keys,wallet,effects.get('owned_token_account_indices',[]),mint=mint)
    from .instructions import inventory
    instruction_inventory=inventory(raw_instructions,value,resolved_keys,wallet)
    from .debits import check_sell_debits
    debit_checks=check_sell_debits(instruction_inventory,value,resolved_keys,wallet,mint,holding,amount)
    rent_quote=None
    if debit_checks.get('rent_creations'):
        try:
            rent_quote=rpc('getMinimumBalanceForRentExemption',[165,{'commitment':'confirmed'}])
        except (ValueError,KeyError,TypeError,OSError):pass
        debit_checks=check_sell_debits(instruction_inventory,value,resolved_keys,wallet,mint,holding,amount,rent_exempt_lamports=rent_quote)
    from .quantity import diagnostic_quantity
    mint_lookup = {'method': 'getMultipleAccounts',
                   'params': [[wallet, holding, mint], {'encoding': 'base64', 'commitment': 'confirmed'}],
                   'result': before}
    quantity = diagnostic_quantity(mint, wallet, amount, mint_source=mint_lookup,
        simulation=value, keys=resolved_keys, transaction_hash=hashlib.sha256(raw).hexdigest())
    evidence_hash=None
    if capture:
        evidence={'schema_version':2,'holding':holding,'route':route,'blockhash':blockhash,
                 'mint_lookup':mint_lookup,'quantity_witness':quantity,
                 'compiler_lookup_snapshot':compiled['lookup_snapshot'],
                 'pool_evidence_hash':pool_snapshot.get('evidence_hash') if pool_snapshot else None,
                 'provenance':'UNSIGNED_UNSUBMITTED_MAINNET_SIMULATION','outer':raw_instructions,
                 'simulation':value,'keys':resolved_keys,'wallet':wallet,'mint':mint,'amount_raw':str(amount),
                 'minimum_out_raw':str(minimum_out),'slot':outcome['context']['slot'],
                 'unsigned_transaction':base64.b64encode(raw).decode(),
                 'rent_lookup':{'data_size':165,'commitment':'confirmed','lamports':rent_quote}}
        evidence_hash=capture(evidence)
        if evidence_hash!=digest(evidence):raise ValueError('Simulation evidence not persisted with matching hash')

    from .router import check_sell_route
    router_checks=check_sell_route(response['swapInstruction'],mint,wallet,holding,amount,minimum_out)
    from .envelope import check_sell_envelope
    envelope_checks=check_sell_envelope(raw_instructions,wallet)
    from .amm import check_sell_bindings
    amm_checks=check_sell_bindings(instruction_inventory,pool_snapshot,mint,wallet,holding,amount,minimum_out,outcome['context']['slot'])
    from .recipients import check_sell_recipients
    recipient_checks=check_sell_recipients(instruction_inventory,amm_checks,amount,minimum_out)
    from .sell_fees import check_sell_fee_totals
    fee_checks=check_sell_fee_totals(pool_snapshot,amm_checks,recipient_checks,value,resolved_keys,amount)
    from .fee_call import check_fee_query
    fee_query=check_fee_query(instruction_inventory,amm_checks,fee_checks)
    from .fee_split import check_protocol_split
    split_checks=check_protocol_split(pool_snapshot,amm_checks,fee_checks,recipient_checks)
    from .setup_policy import check_sell_setup
    setup_checks=check_sell_setup(instruction_inventory,wallet)
    from .sell_event import check_sell_event
    event_checks=check_sell_event(instruction_inventory,amm_checks,fee_checks,recipient_checks,pool_snapshot)
    from .route_coverage import check_route_coverage
    coverage_checks=check_route_coverage(instruction_inventory,dict(effects=effects,controls=controls,debits=debit_checks,
        router=router_checks,envelope=envelope_checks,amm=amm_checks,recipients=recipient_checks,fees=fee_checks,
        fee_query=fee_query,fee_split=split_checks,event=event_checks,setup=setup_checks))
    completed_at=int(time.time());route_at=route.get('observed_at')
    fresh=(0<=completed_at-started_at<=10 and type(route_at) is int and 0<=completed_at-route_at<=10
           and 0<=outcome['context']['slot']-before['context']['slot']<=32)
    return {'kind':'diagnostic_sell_simulation','mint':mint,'wallet':wallet,'holding':holding,
        'amount_raw':str(amount),'quantity_witness':quantity,'observed_at':completed_at,'started_at':started_at,'fresh':fresh,'slot':outcome['context']['slot'],
        'simulation_ok':ok,'simulation_error':value.get('err'),'units_consumed':value.get('unitsConsumed'),
        'holding_after':post_policy,'balance_effects':effects,'account_controls':controls,'instruction_inventory':instruction_inventory,'route_coverage':coverage_checks,'setup_checks':setup_checks,'fee_split_checks':split_checks,'sell_event_checks':event_checks,'fee_query':fee_query,'fee_checks':fee_checks,'recipient_checks':recipient_checks,'amm_bindings':amm_checks,'wallet_debit_checks':debit_checks,'router_checks':router_checks,'envelope_checks':envelope_checks,'before_slot':before['context']['slot'],
        'raw_evidence_persisted':evidence_hash is not None,'evidence_hash':evidence_hash,'transaction_hash':hashlib.sha256(raw).hexdigest(),'route_hash':digest(route),
        'signed':False,'submitted':False,'eligible_for_trading':False,'transaction_policy_ok':False,
        'notice':'Public-holder diagnostic only. Balance and authority checks are diagnostics; full instruction policy and wallet-specific evidence remain required; not a wallet-specific trading approval.'}
