"""Offline reconstruction of saved unsigned sell diagnostics; never fresh approval."""
import base64
import hashlib
from .model import digest
from .quantity import raw_u64
from .compile import compile_unsigned
from .pools import verify_pool
from .effects import check_effects,check_account_controls
from .instructions import inventory
from .debits import check_sell_debits
from .router import check_sell_route
from .envelope import check_sell_envelope
from .amm import check_sell_bindings
from .recipients import check_sell_recipients
from .sell_fees import check_sell_fee_totals
from .fee_call import check_fee_query
from .sell_event import check_sell_event
from .fee_split import check_protocol_split
from .setup_policy import check_sell_setup


def replay_sell(store,key):
    record=store.load(key)
    if record.get('schema_version')!=2 or record.get('provenance')!='UNSIGNED_UNSUBMITTED_MAINNET_SIMULATION':
        raise ValueError('Saved sell evidence lacks complete replay inputs')
    def lookup_rpc(method,params):
        saved=record.get('compiler_lookup_snapshot')
        if not saved or method!=saved['method'] or params!=saved['params']:raise ValueError('Lookup-table evidence missing or mismatched')
        return saved['result']
    route=record['route'];response=route['response'];wallet=record['wallet'];mint=record['mint'];holding=record['holding']
    amount=raw_u64(record['amount_raw']);minimum=int(record['minimum_out_raw']);slot=record['slot']
    from .providers import SOL
    if (response['inputMint']!=mint or response['outputMint']!=SOL or int(response['inAmount'])!=amount
            or int(response['otherAmountThreshold'])!=minimum or type(slot) is not int or slot<0):raise ValueError('Replay route identity mismatch')
    compiled=compile_unsigned(response,wallet,record['blockhash'],lookup_rpc)
    if (compiled['raw']!=base64.b64decode(record['unsigned_transaction'],validate=True)
            or compiled['keys']!=record['keys'] or compiled['outer']!=record['outer']):raise ValueError('Replay transaction does not match saved route and keys')
    pool=None;pool_hash=record.get('pool_evidence_hash')
    if pool_hash:
        saved=store.load(pool_hash)
        if saved.get('kind')!='pool_snapshot':raise ValueError('Pool evidence kind mismatch')
        pool_key=saved['params'][0][3]
        def pool_rpc(method,params):
            if method=='getAccountInfo' and params==[pool_key,{'encoding':'base64','commitment':'confirmed'}]:return saved['discovery']
            if method==saved['method'] and params==saved['params']:return saved['result']
            raise ValueError('Pool replay request mismatch')
        pool=verify_pool(pool_key,mint,pool_rpc,capture=digest)
        if pool['evidence_hash']!=pool_hash:raise ValueError('Reconstructed pool hash mismatch')
    value=record['simulation'];keys=compiled['keys'];outer=compiled['outer']
    effects=check_effects(value,keys,wallet,mint,amount,minimum)
    controls=check_account_controls(value,keys,wallet,effects.get('owned_token_account_indices',[]),mint=mint)
    instructions=inventory(outer,value,keys,wallet,compiled=compiled)
    debits=check_sell_debits(instructions,value,keys,wallet,mint,holding,amount,rent_exempt_lamports=record['rent_lookup']['lamports'])
    bindings=check_sell_bindings(instructions,pool,mint,wallet,holding,amount,minimum,slot)
    recipients=check_sell_recipients(instructions,bindings,amount,minimum)
    fees=check_sell_fee_totals(pool,bindings,recipients,value,keys,amount)
    from .quantity import diagnostic_quantity
    quantity = diagnostic_quantity(mint, wallet, amount, mint_source=record.get('mint_lookup'),
        simulation=value, keys=keys, transaction_hash=hashlib.sha256(compiled['raw']).hexdigest())
    if 'quantity_witness' in record and record['quantity_witness'] != quantity:
        raise ValueError('Saved quantity witness differs from original raw inputs')
    result={'quantity_witness':quantity,'kind':'historical_sell_replay','evidence_hash':key,'fresh':False,'eligible_for_trading':False,'transaction_policy_ok':False,
        'balance_effects':effects,'account_controls':controls,'instruction_inventory':instructions,'wallet_debit_checks':debits,
        'router_checks':check_sell_route(response['swapInstruction'],mint,wallet,holding,amount,minimum),
        'envelope_checks':check_sell_envelope(outer,wallet),'amm_bindings':bindings,'recipient_checks':recipients,'setup_checks':check_sell_setup(instructions,wallet),'fee_split_checks':check_protocol_split(pool,bindings,fees,recipients),'sell_event_checks':check_sell_event(instructions,bindings,fees,recipients,pool),'fee_query':check_fee_query(instructions,bindings,fees),'fee_checks':fees,
        'notice':'Reconstructed from saved raw inputs without network access. Historical replay is never fresh sellability or trading approval.'}

    from .route_coverage import check_route_coverage
    names={'effects':'balance_effects','controls':'account_controls','debits':'wallet_debit_checks','router':'router_checks',
        'envelope':'envelope_checks','amm':'amm_bindings','recipients':'recipient_checks','fees':'fee_checks',
        'fee_query':'fee_query','fee_split':'fee_split_checks','event':'sell_event_checks','setup':'setup_checks'}
    result['route_coverage']=check_route_coverage(instructions,{name:result[key] for name,key in names.items()})
    return result
