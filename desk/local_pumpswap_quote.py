"""Disconnected SDK2.1.0 exact-input estimate from original PumpSwap accounts.

No network, transactions, signer, capture writes or production imports. Callers
must retain the original capture and bind its source/hash/time before invocation.
This is conditional local math, not provider authenticity or execution evidence.
"""
from .dynamic_fees import standard_sol_fees
from .live_observation import ProviderObservation, ingest_pool
from .model import canonical, digest
from .pools import verify_pool
from .programs import address
from .providers import SOL, PUMPSWAP

VERSION = 'pumpswap-sdk-2.1.0-exact-input-v1'
MAX_CAPTURE_BYTES = 256 * 1024


def _u64(value, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value < 2**64:
        raise ValueError('Bounded raw integer required')
    return value


def quote_exact_input(observation, *, capture_hash, expected_source_id, mint, pool,
                      taker, direction, amount_raw, slippage_bps, now,
                      token_profile_version):
    """Return a distinct local estimate envelope; never forge a Jupiter response.

    Only direct canonical WSOL PumpSwap pools accepted by current profile1/2
    ingestion qualify. Transfer-fee/other Token2022 extensions remain rejected.
    Buy follows SDK buyQuoteInput (including its one-unit buffer); sell follows
    sellBaseInput/sellAmounts and checks gross outflow net of LP against real
    reserves, not merely the smaller seller payout.
    """
    if type(observation) is not ProviderObservation or type(expected_source_id) is not str or not expected_source_id or observation.source_id != expected_source_id:
        raise ValueError('Exact source-bound original observation required')
    for key in (mint, pool, taker):
        address(key)
    if direction not in ('buy', 'sell') or type(token_profile_version) is not int or token_profile_version not in (1, 2):
        raise ValueError('Explicit supported direction/profile required')
    amount = _u64(amount_raw, positive=True)
    if type(slippage_bps) is not int or not 0 <= slippage_bps < 10000:
        raise ValueError('Bounded integer slippage required')
    payload = observation.payload
    if len(canonical(payload).encode()) > MAX_CAPTURE_BYTES or digest(payload) != capture_hash:
        raise ValueError('Bounded original capture hash mismatch')
    verified = ingest_pool(lambda: observation, mint=mint, pool=pool, now=now,
                           token_profile_version=token_profile_version)

    def replay(method, params):
        if method == 'getAccountInfo' and params == [pool, {'encoding': 'base64', 'commitment': 'confirmed'}]:
            return payload['discovery']
        if method == payload['method'] and params == payload['params']:
            return payload['result']
        raise ValueError('Original request replay mismatch')

    def captured(value):
        if any(payload.get(k) != v for k, v in value.items()):
            raise ValueError('Original account capture changed')
        return digest(value)

    state = verify_pool(pool, mint, replay, capture=captured,
                        token_profile_version=token_profile_version)
    flags = state['global_config']['fields']['disable_flags']
    if flags & (8 if direction == 'buy' else 16):
        raise ValueError('Requested operation disabled')
    base = verified.reserve_tokens_raw
    gross_reserve = verified.gross_reserve_lamports
    effective = gross_reserve + verified.virtual_quote_reserves_lamports
    spendable = gross_reserve - verified.accrued_protocol_fees_lamports - verified.accrued_creator_fees_lamports
    for value in (base, gross_reserve, effective, spendable):
        _u64(value, positive=True)
    schedule = standard_sol_fees(state['dynamic_fee_config'], int(state['base_mint_policy']['supply_raw']),
                                 base, effective, canonical=True,
                                 # Already applied signed virtual reserves once;
                                 # profile1 also permits proven nonboost fee netting.
                                 virtual_quote_reserves=0,
                                 token_profile_version=token_profile_version)
    rates = dict(schedule['fees_bps'])
    if state['coin_creator'] == '11111111111111111111111111111111':
        rates['creator_fee_bps'] = 0
    if set(rates) != {'lp_fee_bps', 'protocol_fee_bps', 'creator_fee_bps'} or any(type(x) is not int or not 0 <= x <= 10000 for x in rates.values()) or sum(rates.values()) > 10000:
        raise ValueError('Unsupported fee rates')
    if direction == 'buy':
        net = amount * 10000 // (10000 + sum(rates.values()))
        fees = {k: (net * v + 9999) // 10000 for k, v in rates.items()}
        net -= max(0, net + sum(fees.values()) - amount)
        swap = net - 1  # SDK buyQuoteInput's explicit conservative buffer.
        if swap <= 0:
            raise ValueError('Input cannot cover fees and buffer')
        output = base * swap // (effective + swap)
        if base <= output or gross_reserve + amount >= 2**64:
            raise ValueError('Reserve capacity exceeded')
        impact_num, impact_den = base * swap - output * effective, base * swap
        internal = swap
    else:
        if base + amount >= 2**64:
            raise ValueError('Base reserve overflow')
        internal = effective * amount // (base + amount)
        fees = {k: (internal * v + 9999) // 10000 for k, v in rates.items()}
        if internal - fees['lp_fee_bps'] > spendable:
            raise ValueError('Physical quote reserves cannot cover gross sell outflow')
        output = internal - sum(fees.values())
        impact_num, impact_den = effective * amount - internal * base, effective * amount
    minimum = output * (10000 - slippage_bps) // 10000
    _u64(output, positive=True); _u64(minimum, positive=True)
    request = {'mint': mint, 'pool': pool, 'taker': taker, 'direction': direction,
               'amount_raw': str(amount), 'slippage_bps': slippage_bps,
               'token_profile_version': token_profile_version}
    return {'kind': 'local_pumpswap_quote_v1', 'math_version': VERSION,
            'source_id': 'local:' + VERSION, 'rpc_source_id': expected_source_id,
            'capture_hash': capture_hash, 'observed_at': observation.observed_at,
            'slot': verified.slot, 'request': request, 'request_hash': digest(request),
            'input_mint': SOL if direction == 'buy' else mint,
            'output_mint': mint if direction == 'buy' else SOL,
            'input_raw': str(amount), 'estimated_output_raw': str(output),
            'minimum_output_raw': str(minimum), 'internal_swap_raw': str(internal),
            'fees_raw': {k: str(v) for k, v in fees.items()}, 'fees_bps': rates,
            'fee_schedule': schedule, 'price_impact_fraction': [str(impact_num), str(impact_den)],
            'program': PUMPSWAP, 'execution_status': 'EXECUTION_UNVERIFIED',
            'entry_authorized': False, 'execution_verified': False,
            'assumptions': ['SDK_QUOTE_ESTIMATE_NOT_EXECUTED', 'CONFIRMED_ACCOUNT_STATE_NOT_FINALITY',
                            'NO_FUTURE_STATE_OR_TRANSACTION_COST_GUARANTEE', 'BUY_INPUT_IS_CONSERVATIVE_BUDGET']}
