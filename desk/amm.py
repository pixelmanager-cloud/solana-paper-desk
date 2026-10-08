"""Pinned PumpSwap sell bindings; fee authorization is a separate requirement."""
from .route_coverage import instruction_receipt
import base64
from .programs import schemas,address,unbase58
from .providers import PUMPSWAP,SOL
from .security import TOKEN_PROGRAM
from .instructions import ATA,JUPITER


def check_sell_bindings(inventory,pool,mint,wallet,holding,amount,minimum_out,slot):
    from solders.pubkey import Pubkey
    result={'passed':False,'full_route_policy_passed':False,'fee_recipient_authorization_verified':False,'reasons':[]}
    if not pool or pool.get('identity_evidence_verified') is not True:
        return {**result,'reasons':['AMM_POOL_EVIDENCE_UNVERIFIED']}
    reasons=result['reasons']
    if type(slot) is not int or type(pool.get('slot')) is not int or not 0<=slot-pool['slot']<=32:
        reasons.append('AMM_POOL_EVIDENCE_STALE')
    if pool.get('liquidity_control_verified') is not True:reasons.append('AMM_LIQUIDITY_CONTROL_UNVERIFIED')
    for key in (mint,wallet,holding,pool['pool']):address(key)
    if type(amount) is not int or type(minimum_out) is not int or not 0<amount<2**64 or not 0<minimum_out<2**64:raise ValueError('Invalid AMM amounts')
    spec=next(x for x in schemas()[PUMPSWAP].values() if x['name']=='sell')
    candidates=[]
    for row in inventory.get('instructions',[]):
        if row['program']!=PUMPSWAP:continue
        raw=base64.b64decode(row['data_base64'],validate=True)
        if raw[:8]==bytes(spec['discriminator']):candidates.append((row,raw))
    if len(candidates)!=1:return {**result,'reasons':sorted(set(reasons+['AMM_REQUIRES_ONE_SELL_INSTRUCTION']))}
    row,raw=candidates[0];accounts=row['accounts']
    if (inventory.get('stack_metadata_verified') is not True or row.get('stack_height')!=2 or row.get('parent_program')!=JUPITER):
        reasons.append('AMM_SELL_CALLER_UNVERIFIED')
    creator=pool.get('coin_creator');base_count=len(spec['accounts'])
    has_creator=creator not in (None,'11111111111111111111111111111111')
    modern_count=base_count+2+int(has_creator)
    modern=len(accounts)==modern_count
    if len(raw)!=24 or len(accounts) not in (base_count,modern_count):return {**result,'reasons':sorted(set(reasons+['AMM_SELL_LAYOUT_UNSUPPORTED']))}
    named={entry['name']:address(accounts[i]) for i,entry in enumerate(spec['accounts'])}
    def pda(seeds,program):return str(Pubkey.find_program_address(seeds,Pubkey.from_string(program))[0])
    def ata(owner,asset):return pda([unbase58(owner),unbase58(TOKEN_PROGRAM),unbase58(asset)],ATA)
    if modern:
        if pool.get('is_cashback_coin') is not False or pool.get('is_holder_reward') is not False:
            reasons.append('AMM_REWARD_OR_UNKNOWN_PROFILE_UNSUPPORTED')
        if has_creator:
            named['pool_v2']=address(accounts[base_count])
            if named['pool_v2']!=pda([b'pool-v2',unbase58(mint)],PUMPSWAP):reasons.append('AMM_POOL_V2_PDA_MISMATCH')
        named['buyback_fee_recipient']=address(accounts[-2])
        named['buyback_fee_recipient_token_account']=address(accounts[-1])
        if named['buyback_fee_recipient_token_account']!=ata(named['buyback_fee_recipient'],SOL):reasons.append('AMM_BUYBACK_FEE_ATA_MISMATCH')
    vaults={v['mint']:v['address'] for v in pool['vaults']}
    if len(vaults)!=2 or mint not in vaults or SOL not in vaults:raise ValueError('AMM pool vault assets mismatch')
    from .fee_config import config_address
    expected={'global_config':config_address(),'pool':pool['pool'],'user':wallet,'base_mint':mint,'quote_mint':SOL,
        'user_base_token_account':holding,'user_quote_token_account':ata(wallet,SOL),
        'pool_base_token_account':vaults[mint],'pool_quote_token_account':vaults[SOL],
        'base_token_program':TOKEN_PROGRAM,'quote_token_program':TOKEN_PROGRAM}
    expected.update({entry['name']:entry['address'] for entry in spec['accounts'] if entry.get('address')})
    if any(named[k]!=v for k,v in expected.items()):reasons.append('AMM_ACCOUNT_BINDING_MISMATCH')
    if int.from_bytes(raw[8:16],'little')!=amount:reasons.append('AMM_INPUT_AMOUNT_MISMATCH')
    if int.from_bytes(raw[16:24],'little')<minimum_out:reasons.append('AMM_MINIMUM_OUTPUT_TOO_LOW')
    if named['protocol_fee_recipient_token_account']!=ata(named['protocol_fee_recipient'],SOL):reasons.append('AMM_PROTOCOL_FEE_ATA_MISMATCH')
    creator=pool.get('coin_creator')
    if not creator:reasons.append('AMM_CREATOR_IDENTITY_MISSING')
    else:
        authority=pda([b'creator_vault',unbase58(address(creator))],PUMPSWAP)
        if named['coin_creator_vault_authority']!=authority or named['coin_creator_vault_ata']!=ata(authority,SOL):reasons.append('AMM_CREATOR_VAULT_MISMATCH')
    if named['event_authority']!=pda([b'__event_authority'],PUMPSWAP):reasons.append('AMM_EVENT_AUTHORITY_MISMATCH')
    fee_spec=next(x for x in spec['accounts'] if x['name']=='fee_config')
    if named['fee_config']!=pda([bytes(x['value']) for x in fee_spec['pda']['seeds']],named['fee_program']):reasons.append('AMM_FEE_CONFIG_PDA_MISMATCH')
    config=pool.get('global_config',{})
    authorized=(config.get('configuration_complete') is True and config.get('address')==config_address()
        and named['global_config']==config_address() and config.get('slot')==pool['slot'] and config.get('sell_disabled') is False
        and named['protocol_fee_recipient'] in config.get('fields',{}).get('protocol_fee_recipients',[])
        and pool.get('is_mayhem_mode') is False)
    if not authorized:reasons.append('AMM_PROTOCOL_FEE_RECIPIENT_UNAUTHORIZED_OR_UNKNOWN')
    buyback_authorized=not modern or (authorized and named['buyback_fee_recipient'] in config.get('fields',{}).get('buyback_fee_recipients',[]))
    if not buyback_authorized:reasons.append('AMM_BUYBACK_RECIPIENT_UNAUTHORIZED_OR_UNKNOWN')
    result['fee_recipient_authorization_verified']=authorized and buyback_authorized
    result['account_profile']='STANDARD_WITH_BUYBACK' if modern else 'LEGACY_BASE_ACCOUNTS'
    result.update(passed=not reasons,reasons=sorted(set(reasons)),instruction=row['instruction'],
        checked_instructions=[instruction_receipt(row)] if not reasons else [],account_bindings=named,pool=pool['pool'],protocol_fee_recipient=named['protocol_fee_recipient'],
        notice='Account bindings only. Standard protocol-fee membership is checked against the same-bank global configuration. Actual fee amounts and complete CPI policy remain unverified.')
    return result
