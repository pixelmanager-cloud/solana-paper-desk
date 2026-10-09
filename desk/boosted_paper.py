"""Profile2 replay and physical sell-capacity proof, never execution approval.

Pinned official pump-swap-sdk2.1.0 sellBaseInput/sellAmounts: effective reserve
prices the output; gross output less LP fee must fit vault less accrued fees.
Original atomic source is mandatory, including on restart; quote net output is
never substituted for that pre-protocol/creator-fee capacity obligation.
"""
from .live_observation import SourceRecord,ProviderObservation,ingest_pool
from .pools import verify_pool
from .model import decimal
from .security import mint_policy
from .dynamic_fees import standard_sol_fees
from .sell_fees import standard_sell_amounts


def replay_pool(event,cfg):
    from .quote_execution import _original
    source=event['paper_pool_evidence']
    if type(source) is not dict or set(source)!={'source_id','observed_at','raw_hash','original_json'}:
        raise ValueError('Original pool evidence required')
    record=SourceRecord(**source);raw=_original(record)
    observed=ingest_pool(lambda:ProviderObservation(record.source_id,record.observed_at,raw),
        mint=event['mint'],pool=event['pool'],now=event['ts'],
        max_age_seconds=cfg['price_ttl_seconds'],token_profile_version=2)
    if event['kind']=='market' and 'reserve_sol' in event:
        if event.get('paper_signal_profile',{}).get('volume_reserve_basis')!='EFFECTIVE_PRICING_NOT_PHYSICAL_LIQUIDITY':
            raise ValueError('Versioned historical pricing denominator required')
        original=event['paper_source_evidence']
        if (record.raw_hash!=original['pool_hash'] or record.observed_at!=original['pool_at']
                or observed.slot!=original['pool_slot']):raise ValueError('Entry pool source mismatch')
        if decimal(event['reserve_sol'])!=observed.reserve_sol or decimal(event['reserve_tokens'])!=observed.reserve_tokens:
            raise ValueError('Physical sizing reserve mismatch')
    if event['kind']=='quote_exit':
        e=event['source_evidence']
        if (record.raw_hash!=e['pool_hash'] or record.observed_at!=e['pool_at']
                or record.source_id!=e['rpc_source_id'] or observed.slot!=e['pool_slot']):
            raise ValueError('Exit pool source mismatch')
    def rpc(method,params):
        if method=='getAccountInfo' and params==[event['pool'],{'encoding':'base64','commitment':'confirmed'}]:return raw['discovery']
        if method==raw['method'] and params==raw['params']:return raw['result']
        raise ValueError('Pool request mismatch')
    from .model import digest
    checked=verify_pool(event['pool'],event['mint'],rpc,capture=digest,token_profile_version=2)
    return raw,checked


def validate_quote(event,quote,token,proof):
    if proof is None:raise ValueError('Bound pool proof required')
    raw,pool=proof
    atomic=raw['result']['value'][6]
    policy=mint_policy(atomic,mint=event['mint'],token_profile_version=2)
    from .quote_execution import _original
    separate=_original(token.source)['account']
    if token.source.source_id!=event['paper_pool_evidence']['source_id']:
        raise ValueError('Mint/pool source roster mismatch')
    if event['kind']=='market' and 'reserve_sol' in event:
        e=event['paper_source_evidence']
        if (token.source.raw_hash!=e['mint_hash'] or token.source.observed_at!=e['mint_at']
                or token.slot!=e['mint_slot'] or str(policy['supply_raw'])!=e['valuation_supply_raw']
                or pool['slot']!=e['valuation_supply_slot']
                or event['paper_pool_evidence']['observed_at']!=e['valuation_supply_at']):
            raise ValueError('Entry mint/supply source mismatch')
    if (separate['owner']!=atomic['owner'] or policy['decimals']!=token.decimals
            or pool['slot']<token.slot or pool['slot']-token.slot>16):
        raise ValueError('Mint/bank source mismatch')
    wire=_original(quote.source)['response']
    if len(wire['routePlan'])!=1 or quote.route_pools!=(event['pool'],):
        raise ValueError('Profile2 requires exact single canonical pool route')
    if quote.direction!='sell':return
    rates=standard_sol_fees(pool['dynamic_fee_config'],int(policy['supply_raw']),
        int(pool['base_reserve_raw']),int(pool['gross_quote_reserve_raw']),
        canonical=pool['canonical_migration_pool'],virtual_quote_reserves=int(pool['virtual_quote_reserves_raw']),
        token_profile_version=2)
    amounts=standard_sell_amounts(quote.input_raw,int(pool['base_reserve_raw']),
        int(pool['effective_quote_reserve_raw']),rates['fees_bps'],
        has_creator=pool['coin_creator']!='11111111111111111111111111111111')
    require_capacity(amounts,int(pool['spendable_quote_reserve_raw']))
    # A better provider quote can reflect a changed bank; do not accept that
    # output as supported by this retained snapshot. Embedded fees not doubled.
    if quote.estimated_output_raw>int(amounts['user_output_raw']):
        raise ValueError('Quote output exceeds supported snapshot amount')


def require_capacity(amounts,spendable):
    payout=int(amounts['gross_quote_raw'])-int(amounts['lp_fee_raw'])
    if type(spendable) is not int or not 0<spendable<2**64 or not 0<payout<=spendable:
        raise ValueError('Sell exceeds physical quote capacity')
    return payout
