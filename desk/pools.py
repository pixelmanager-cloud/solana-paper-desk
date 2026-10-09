"""PumpSwap pool identity and vault verification from program-owned chain state."""
import hashlib,json
from pathlib import Path
from .programs import schemas,BorshReader,address
from .security import account_bytes,base58,TOKEN_PROGRAM,TOKEN_2022
from .providers import PUMPSWAP,PUMP,SOL

ATA='ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'


def parse_pool(account):
    if not account or account.get('owner')!=PUMPSWAP or account.get('executable') is not False:
        raise ValueError('Pool is not owned by PumpSwap')
    schemas()
    folder=Path(__file__).parent/'schemas'
    manifest=json.loads((folder/'fee_sdk_manifest.json').read_text())
    raw_schema=(folder/manifest['pool_schema_file']).read_bytes()
    if hashlib.sha256(raw_schema).hexdigest()!=manifest['pool_schema_sha256']:raise ValueError('Pool schema checksum mismatch')
    schema=json.loads(raw_schema)
    spec=next(x for x in schema['accounts'] if x['name']=='Pool')
    data=account_bytes(account)
    if data[:8]!=bytes(spec['discriminator']):raise ValueError('Pool discriminator mismatch')
    types={t['name']:t['type'] for t in schema['types']}
    reader=BorshReader(data[8:],types);fields={}
    # Official docs permit absent appended fields. Never silently default an identity field.
    appended={'is_mayhem_mode':False,'is_cashback_coin':False,'virtual_quote_reserves':0,
              'creator_fee_bps':0,'can_edit_creator_fee':False,'is_holder_reward':False,
              'protocol_fees':0,'creator_fees':0}
    for field in types['Pool']['fields']:
        name=field['name']
        if name in ('protocol_fees','creator_fees') and len(data)<287:fields[name]=0
        elif reader.pos==len(reader.data) and name in appended:fields[name]=appended[name]
        else:fields[name]=reader.read(field['type'])
    trailing=reader.data[reader.pos:]
    # SDK documents extended allocation >=300; limit acceptance to captured
    # 300/301-byte profiles with entirely zero reserved capacity.
    known_padding=len(data) in (300,301) and not any(trailing)
    fields['reserved_bytes']=len(trailing) if known_padding else 0
    fields['unknown_trailing_bytes']=0 if known_padding else len(trailing)
    return fields


def verify_pool(pool, mint, rpc,*,capture=None,token_profile_version=0):
    from .token2022_paper import check_version
    check_version(token_profile_version)
    from solders.pubkey import Pubkey
    address(pool);address(mint)
    response=rpc('getAccountInfo',[pool,{'encoding':'base64','commitment':'confirmed'}])
    initial_slot=response.get('context',{}).get('slot')
    if type(initial_slot) is not int or initial_slot<0:raise ValueError('Pool context slot missing')
    fields=parse_pool(response.get('value'))
    program=Pubkey.from_string(PUMPSWAP)
    expected,bump=Pubkey.find_program_address([b'pool',fields['index'].to_bytes(2,'little'),
        bytes(Pubkey.from_string(fields['creator'])),bytes(Pubkey.from_string(fields['base_mint'])),
        bytes(Pubkey.from_string(fields['quote_mint']))],program)
    if str(expected)!=pool or bump!=fields['pool_bump']:raise ValueError('Pool PDA mismatch')
    if fields['base_mint']!=mint:raise ValueError('Pool base mint mismatch')
    expected_lp=str(Pubkey.find_program_address([b'pool_lp_mint',bytes(expected)],program)[0])
    if fields['lp_mint']!=expected_lp:raise ValueError('Pool LP mint PDA mismatch')
    from .fee_config import config_address,parse_config
    from .dynamic_fees import fee_address,parse_fee_config
    from .security import mint_policy
    global_key=config_address();dynamic_key=str(fee_address()[0])
    snapshot_keys=[fields['pool_base_token_account'],fields['pool_quote_token_account'],fields['lp_mint'],pool,global_key,dynamic_key,mint]
    if len(set(snapshot_keys))!=7 or fields['base_mint']==fields['quote_mint']:raise ValueError('Pool account identities overlap')
    params=[snapshot_keys,{'encoding':'base64','commitment':'confirmed','minContextSlot':initial_slot}]
    values=rpc('getMultipleAccounts',params)
    if not isinstance(values.get('value'),list) or len(values['value'])!=7:raise ValueError('Pool snapshot incomplete')
    same_bank_pool=values['value'][3]
    if parse_pool(same_bank_pool)!=fields or account_bytes(same_bank_pool)!=account_bytes(response['value']):
        raise ValueError('Pool state changed between discovery and atomic snapshot')
    global_config=parse_config(values['value'][4])
    dynamic_config=parse_fee_config(values['value'][5]);base_mint_policy=mint_policy(values['value'][6],mint=mint,token_profile_version=token_profile_version)
    vaults=[];reasons=list(global_config['reasons'])+list(dynamic_config['reasons'])+list(base_mint_policy['reasons']);evidence_hash=None
    if global_config['sell_disabled']:reasons.append('POOL_SELL_DISABLED_BY_CONFIG')
    if capture is not None:
        from .model import digest
        envelope={'kind':'pool_snapshot','discovery':response,'method':'getMultipleAccounts','params':params,'result':values}
        evidence_hash=capture(envelope)
        if evidence_hash!=digest(envelope):raise ValueError('Pool snapshot evidence hash mismatch')
    else:reasons.append('POOL_SNAPSHOT_EVIDENCE_NOT_PERSISTED')
    for key,mint_key,value in zip(('pool_base_token_account','pool_quote_token_account'),('base_mint','quote_mint'),values['value'][:2]):
        if not value or value.get('owner') not in (TOKEN_PROGRAM,TOKEN_2022):raise ValueError('Unsupported pool vault program')
        if value.get('executable') is not False:raise ValueError('Executable pool vault')
        data=account_bytes(value)
        if value['owner']==TOKEN_PROGRAM and len(data)!=165:raise ValueError('Invalid legacy vault layout')
        if value['owner']==TOKEN_2022:
            if token_profile_version==1:
                from .security import holding_policy
                checked=holding_policy(value,fields[mint_key],pool,token_profile_version=token_profile_version)
                reasons.extend(checked['reasons'])
            elif len(data)>165:reasons.append('VAULT_EXTENSIONS_REQUIRE_VALIDATION')
        if len(data)<165 or base58(data[:32])!=fields[mint_key] or base58(data[32:64])!=pool:
            raise ValueError('Pool vault identity mismatch')
        expected_ata=str(Pubkey.find_program_address([bytes(expected),bytes(Pubkey.from_string(value['owner'])),
                         bytes(Pubkey.from_string(fields[mint_key]))],Pubkey.from_string(ATA))[0])
        if expected_ata!=fields[key]:raise ValueError('Pool vault ATA mismatch')
        if data[108]!=1 or int.from_bytes(data[72:76],'little')!=0:raise ValueError('Frozen or delegated pool vault')
        close=int.from_bytes(data[129:133],'little')
        if close not in (0,1) or (close and base58(data[133:165])!=pool):raise ValueError('External vault close authority')
        vaults.append({'address':fields[key],'wallet':pool,'mint':fields[mint_key],
                       'amount_raw':str(int.from_bytes(data[64:72],'little')),'classification':'VERIFIED_POOL_VAULT'})
    if token_profile_version==1:
        if values['value'][0]['owner']!=values['value'][6]['owner']:reasons.append('BASE_MINT_VAULT_PROGRAM_MISMATCH')
        if values['value'][1]['owner']!=TOKEN_PROGRAM:reasons.append('QUOTE_VAULT_PROGRAM_UNSUPPORTED')
        if values['value'][2].get('owner')!=TOKEN_PROGRAM:reasons.append('LP_TOKEN_PROGRAM_UNSUPPORTED')
    lp=values['value'][2];lp_supply=None
    if lp and lp.get('owner') in (TOKEN_PROGRAM,TOKEN_2022):
        raw=account_bytes(lp)
        valid_layout=lp.get('executable') is False and (len(raw)==82 if lp['owner']==TOKEN_PROGRAM else len(raw)>=82)
        if valid_layout and raw[45]==1:lp_supply=int.from_bytes(raw[36:44],'little')
        else:reasons.append('INVALID_LP_MINT_LAYOUT')
    canonical_creator=str(Pubkey.find_program_address([b'pool-authority',bytes(Pubkey.from_string(mint))],Pubkey.from_string(PUMP))[0])
    canonical=fields['index']==0 and fields['creator']==canonical_creator and fields['quote_mint']==SOL
    if not canonical:reasons.append('NONCANONICAL_MIGRATION_POOL')
    if lp and lp.get('owner') in (TOKEN_PROGRAM,TOKEN_2022):
        raw=account_bytes(lp)
        if len(raw)>=82:
            authority=int.from_bytes(raw[:4],'little')
            if authority!=1 or base58(raw[4:36])!=pool:reasons.append('UNEXPECTED_LP_MINT_AUTHORITY')
            if int.from_bytes(raw[46:50],'little')!=0:reasons.append('LP_FREEZE_AUTHORITY')
            if lp['owner']==TOKEN_2022 and len(raw)>82:reasons.append('LP_EXTENSIONS_REQUIRE_VALIDATION')

    if fields['protocol_fees'] or fields['creator_fees']:reasons.append('ACCRUED_POOL_FEES_REQUIRE_RESERVE_ADJUSTMENT')
    if fields['unknown_trailing_bytes']:reasons.append('POOL_LAYOUT_HAS_UNKNOWN_EXTENSION')
    if fields['quote_mint']!=SOL:reasons.append('NON_SOL_QUOTE_POOL')
    if fields['is_mayhem_mode']:reasons.append('MAYHEM_POOL')
    if fields['is_cashback_coin']:reasons.append('CASHBACK_POOL_REQUIRES_FEE_POLICY')
    if fields['is_holder_reward']:reasons.append('HOLDER_REWARD_POOL_REQUIRES_FEE_POLICY')
    if fields['creator_fee_bps']:reasons.append('POOL_CREATOR_FEE_OVERRIDE_REQUIRES_POLICY')
    if fields['can_edit_creator_fee']:reasons.append('MUTABLE_CREATOR_FEE')
    if fields['virtual_quote_reserves']!=0:reasons.append('VIRTUAL_RESERVES_REQUIRE_SPECIAL_PRICING')
    if lp_supply is None:reasons.append('LP_SUPPLY_UNKNOWN')
    elif lp_supply>0:reasons.append('OUTSTANDING_WITHDRAWABLE_LP_SUPPLY')
    slot=values.get('context',{}).get('slot')
    if type(slot) is not int or not 0<=slot-initial_slot<=16:
        reasons.append('POOL_SNAPSHOT_SLOT_DRIFT')
    return {'pool':pool,'identity_verified':True,'identity_evidence_verified':evidence_hash is not None,'snapshot_atomic':True,'evidence_hash':evidence_hash,'canonical_migration_pool':canonical,'slot':values['context']['slot'],'vaults':vaults,
            'accrued_protocol_fees_raw':str(fields['protocol_fees']),'accrued_creator_fees_raw':str(fields['creator_fees']),
            'base_reserve_raw':vaults[0]['amount_raw'],'quote_reserve_raw':vaults[1]['amount_raw'],
            'dynamic_fee_config':{'address':dynamic_key,'slot':slot,**dynamic_config},
            'base_mint_policy':{'address':mint,'slot':slot,**base_mint_policy},
            'global_config':{'address':global_key,'slot':slot,**global_config},'is_cashback_coin':fields['is_cashback_coin'],'is_holder_reward':fields['is_holder_reward'],'creator_fee_bps':fields['creator_fee_bps'],'is_mayhem_mode':fields['is_mayhem_mode'],'coin_creator':fields['coin_creator'],'lp_mint':fields['lp_mint'],'outstanding_lp_supply_raw':str(lp_supply) if lp_supply is not None else None,
            'liquidity_control_verified':lp_supply==0 and not reasons,'reasons':reasons,
            'notice':'PDA and vault verification is not a complete liquidity safety guarantee; program upgrades and later deposits remain possible.'}
