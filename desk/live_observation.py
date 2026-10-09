"""Read-only ingestion of original provider observations, not market-event approval.

The coordinator supplies the reader, source identity and acquisition time. Hashes
identify content, not provider authenticity. No default reader or network calls.
Unknown strategy inputs remain absent; quote amounts are estimates, never fills.
The returned records are deliberately not compatible with validate_event: the
runner/policy must handle unknown flow, concentration and creator history itself.
ProviderObservation.payload is the original parsed JSON object from inspect_mint,
jupiter_probe or verify_pool's capture callback. raw_hash covers canonical JSON
(as existing digest does), not HTTP bytes. Acquisition time/source are assertions
of the injected coordinator reader, not independently authenticated chain facts.
The MintObservation argument to ingest_quote is an in-process ingest_mint result,
not a deserialization contract for candidate-supplied normalized policy records.
"""
from dataclasses import dataclass
from decimal import Decimal, localcontext
import json
from typing import Callable, Literal

from .decode import integer
from .model import canonical, digest
from .pools import verify_pool
from .programs import address
from .providers import SOL
from .security import mint_policy


class ObservationError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderObservation:
    source_id: str
    observed_at: int
    payload: dict


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    observed_at: int
    raw_hash: str
    original_json: str


@dataclass(frozen=True)
class MintObservation:
    mint: str
    slot: int
    decimals: int
    supply_raw: int
    mint_authority: None
    freeze_authority: None
    source: SourceRecord


@dataclass(frozen=True)
class PoolObservation:
    mint: str
    pool: str
    slot: int
    decimals: int
    reserve_tokens_raw: int
    reserve_lamports: int
    reserve_tokens: Decimal
    reserve_sol: Decimal
    spot_sol_per_token: Decimal
    source: SourceRecord
    risk_flags: tuple[str, ...] = ('RESERVE_SPOT_NOT_EXECUTION_PRICE', 'FEE_POLICY_REQUIRED',
                                   'OWNERSHIP_HISTORY_UNKNOWN', 'STRATEGY_FEATURES_UNKNOWN')
    executable_fill_proof: None = None


@dataclass(frozen=True)
class QuoteObservation:
    mint: str
    direction: Literal['buy', 'sell']
    input_raw: int
    estimated_output_raw: int
    minimum_output_raw: int
    input_units: Decimal
    estimated_output_units: Decimal
    minimum_output_units: Decimal
    estimated_sol_per_token: Decimal
    route_pools: tuple[str, ...]
    source: SourceRecord
    mint_source: SourceRecord
    risk_flags: tuple[str, ...] = ('QUOTE_NOT_FILL', 'ROUTE_POLICY_UNKNOWN',
                                   'OWNERSHIP_HISTORY_UNKNOWN', 'STRATEGY_FEATURES_UNKNOWN')
    executable_fill_proof: None = None


def _whole(value, *, positive=False):
    result = integer(value)
    if result >= 2**64 or (positive and result == 0):
        raise ObservationError('Invalid base-unit amount')
    return result


def _fresh(at, now, max_age):
    if (type(now) is not int or type(at) is not int or type(max_age) is not int
            or max_age < 0 or now < 0 or not 0 <= now - at <= max_age):
        raise ObservationError('Missing, future or stale observation time')


def _read(reader, now, max_age):
    observation = reader()
    if not isinstance(observation, ProviderObservation):
        raise ObservationError('Timestamped provider observation required')
    if not isinstance(observation.source_id, str) or not observation.source_id.strip():
        raise ObservationError('Source identity required')
    _fresh(observation.observed_at, now, max_age)
    if not isinstance(observation.payload, dict):
        raise ObservationError('Original JSON object required')
    raw = canonical(observation.payload)
    payload = json.loads(raw)  # retain an immutable original, detach caller mutation
    if 'observed_at' in payload and (type(payload['observed_at']) is not int or payload['observed_at'] != observation.observed_at):
        raise ObservationError('Original and acquisition times mismatch')
    return payload, SourceRecord(observation.source_id, observation.observed_at,
                                 digest(payload), raw)


def _units(amount, decimals):
    if type(decimals) is not int or not 0 <= decimals <= 255:
        raise ObservationError('Invalid mint decimals')
    with localcontext() as ctx:
        ctx.prec = 100
        return Decimal(amount).scaleb(-decimals)


def _ratio(numerator, denominator):
    with localcontext() as ctx:
        ctx.prec = 100
        return numerator / denominator


def ingest_mint(reader: Callable[[], ProviderObservation], *, mint: str,
                now: int, max_age_seconds: int = 10) -> MintObservation:
    """Read existing inspect_mint output; recompute controls from its raw account."""
    address(mint)
    payload, source = _read(reader, now, max_age_seconds)
    if payload.get('mint') != mint:
        raise ObservationError('Mint identity mismatch')
    slot = payload.get('slot')
    if type(slot) is not int or slot < 0:
        raise ObservationError('Mint bank missing')
    policy = mint_policy(payload.get('account'))
    if policy['decision'] != 'PASS_TOKEN_POLICY':
        raise ObservationError('Unsafe mint: ' + ','.join(policy['reasons']))
    return MintObservation(mint, slot, policy['decimals'], _whole(policy['supply_raw'], positive=True),
                           None, None, source)


def ingest_pool(reader: Callable[[], ProviderObservation], *, mint: str, pool: str,
                now: int, max_age_seconds: int = 10) -> PoolObservation:
    """Replay exact saved verify_pool capture, without trusting normalized results."""
    payload, source = _read(reader, now, max_age_seconds)
    if payload.get('kind') != 'pool_snapshot' or payload.get('method') != 'getMultipleAccounts':
        raise ObservationError('Original atomic pool capture required')

    def replay(method, params):
        if method == 'getAccountInfo' and params == [pool, {'encoding': 'base64', 'commitment': 'confirmed'}]:
            return payload['discovery']
        if method == payload['method'] and params == payload.get('params'):
            return payload['result']
        raise ObservationError('Pool request identity mismatch')

    def captured(envelope):
        # Do not silently normalize or rewrite any original request/response.
        if any(payload.get(key) != value for key, value in envelope.items()):
            raise ObservationError('Pool capture mismatch')
        return digest(envelope)

    try:
        result = verify_pool(pool, mint, replay, capture=captured)
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise ObservationError('Malformed atomic pool capture') from error
    if result['reasons'] or not result['liquidity_control_verified']:
        raise ObservationError('Unsafe pool: ' + ','.join(result['reasons']))
    decimals = result['base_mint_policy']['decimals']
    tokens = _whole(result['base_reserve_raw'], positive=True)
    lamports = _whole(result['quote_reserve_raw'], positive=True)
    token_units, sol_units = _units(tokens, decimals), _units(lamports, 9)
    return PoolObservation(mint, pool, result['slot'], decimals, tokens, lamports,
                           token_units, sol_units, _ratio(sol_units, token_units), source)


def ingest_quote(reader: Callable[[], ProviderObservation], *, mint: MintObservation,
                 direction: Literal['buy', 'sell'], amount_raw: int, taker: str,
                 now: int, expected_pool: str | None = None,
                 max_age_seconds: int = 10) -> QuoteObservation:
    """Read existing jupiter_probe output for an exact intended input quantity.

    expected_pool binds route membership only; it does not validate route programs,
    account controls, balance effects or execution fees. Those remain runner gates.
    """
    if not isinstance(mint, MintObservation) or direction not in ('buy', 'sell'):
        raise ObservationError('Validated mint and explicit quote direction required')
    address(mint.mint); address(taker)
    _fresh(mint.source.observed_at, now, max_age_seconds)
    amount = _whole(amount_raw, positive=True)
    payload, source = _read(reader, now, max_age_seconds)
    if payload.get('kind') != 'unsigned_route_probe':
        raise ObservationError('Original unsigned route probe required')
    input_mint, output_mint = (SOL, mint.mint) if direction == 'buy' else (mint.mint, SOL)
    request, response = payload.get('request', {}), payload.get('response', {})
    for obj in (request, response):
        if not isinstance(obj, dict):
            raise ObservationError('Quote request/response object missing')
        if obj.get('inputMint') != input_mint or obj.get('outputMint') != output_mint:
            raise ObservationError('Quote mint mismatch')
    if request.get('taker') != taker or _whole(request.get('amount'), positive=True) != amount:
        raise ObservationError('Quote request quantity/taker mismatch')
    if response.get('swapMode') != 'ExactIn' or _whole(response.get('inAmount'), positive=True) != amount:
        raise ObservationError('Quote input amount/mode mismatch')
    slippage = _whole(request.get('slippageBps'))
    if slippage > 10000 or type(response.get('slippageBps')) is not int or response['slippageBps'] != slippage:
        raise ObservationError('Quote slippage mismatch')
    output = _whole(response.get('outAmount'), positive=True)
    minimum = _whole(response.get('otherAmountThreshold'), positive=True)
    if minimum > output:
        raise ObservationError('Quote minimum exceeds estimate')
    route = response.get('routePlan')
    if not isinstance(route, list) or not route:
        raise ObservationError('Quote route missing')
    pools = []
    previous_mint, previous_amount = input_mint, amount
    for hop in route:
        if not isinstance(hop, dict) or not isinstance(hop.get('swapInfo'), dict):
            raise ObservationError('Malformed quote route')
        info = hop['swapInfo']
        if type(hop.get('percent')) is not int or hop['percent'] != 100:
            raise ObservationError('Only complete sequential routes supported')
        if 'bps' in hop and (type(hop['bps']) is not int or hop['bps'] != 10000):
            raise ObservationError('Split route requires separate accounting')
        if info.get('inputMint') != previous_mint or _whole(info.get('inAmount'), positive=True) != previous_amount:
            raise ObservationError('Quote route input mismatch')
        previous_mint = info.get('outputMint'); address(previous_mint)
        previous_amount = _whole(info.get('outAmount'), positive=True)
        key = info.get('ammKey'); address(key)
        pools.append(key)
    if previous_mint != output_mint or previous_amount != output:
        raise ObservationError('Quote route output mismatch')
    if expected_pool is not None:
        address(expected_pool)
        if not any(hop['swapInfo']['ammKey'] == expected_pool and
                   mint.mint in (hop['swapInfo']['inputMint'], hop['swapInfo']['outputMint'])
                   for hop in route):
            raise ObservationError('Quote mint/pool mismatch')
    input_decimals, output_decimals = (9, mint.decimals) if direction == 'buy' else (mint.decimals, 9)
    input_units, output_units = _units(amount, input_decimals), _units(output, output_decimals)
    sol, tokens = (input_units, output_units) if direction == 'buy' else (output_units, input_units)
    return QuoteObservation(mint.mint, direction, amount, output, minimum, input_units,
                            output_units, _units(minimum, output_decimals), _ratio(sol, tokens),
                            tuple(pools), source, mint.source)
