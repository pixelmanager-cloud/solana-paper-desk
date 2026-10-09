"""Injected Jupiter V3 SOL/USD valuation, never execution or safety evidence.

Primary docs: https://developers.jup.ag/docs/price and
https://developers.jup.ag/docs/guides/how-to-get-token-price . The supplied
index.md/openapi YAML could not be read by the documentation tool; rendered
primary docs confirm this narrow contract. createdAt is token creation time.

Transport (owned by the coordinator) authenticates HTTPS with x-api-key and
supplies response bytes, status, acquisition time and the credential-free exact
request descriptor below. Keys/headers are deliberately outside this API.
TrustedSlotBounds comes from coordinator clock/chain reads, NEVER response JSON
or arbitrary submitted flags. block_times must associate exact slots with trusted
chain times; a fresh getSlot alone cannot date an older price. Hashes identify
records, not authenticate the provider. No network, filesystem or DB I/O.

Conservative limits: 64 KiB response, 32-slot range, 10s acquisition/slot-capture
age, 30s actual price-block age; numeric tokens <=128 characters and
explicit exponent magnitude <=308. Unsupported numeric syntax remains unknown. Consumers must re-parse with a current trusted
clock before reuse. No interpolation, stablecoin peg or createdAt fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException
import hashlib
import json
import re

SOL_MINT = "So11111111111111111111111111111111111111112"
PRICE_URL = "https://api.jup.ag/price/v3?ids=" + SOL_MINT
MAX_RESPONSE_BYTES = 65536
# Conservative JSON numeric syntax budgets, not price estimates or market bounds.
MAX_NUMBER_CHARS = 128
MAX_EXPONENT = 308


@dataclass(frozen=True)
class JupiterPriceResponse:
    method: str
    url: str
    raw_payload: bytes
    acquired_at: int
    http_status: int = 200


@dataclass(frozen=True)
class TrustedSlotBounds:
    now: int
    observed_at: int
    min_slot: int
    max_slot: int
    block_times: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class SolUsdObservation:
    status: str
    usd_price: Decimal | None
    block_id: int | None
    price_at: int | None
    acquired_at: int
    http_status: int
    blockers: tuple[str, ...]
    request: tuple[str, str]
    request_sha256: str
    raw_payload: bytes
    payload_sha256: str
    bounds: TrustedSlotBounds
    source: str = "jupiter_price_v3"
    purpose: str = "USD_VALUATION_ONLY"


def _integer(value):
    return type(value) is int and value >= 0


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _reject_constant(value):
    raise ValueError("nonfinite JSON number")



def _json_decimal(token):
    # Check the original numeric token before Decimal can overflow its exponent
    # representation. No rounding, float conversion, clamping or zero fallback.
    if len(token)>MAX_NUMBER_CHARS:
        raise ValueError("JSON numeric token exceeds syntax budget")
    match=re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE]([+-]?[0-9]+))?',token)
    if match is None or (match[1] is not None and abs(int(match[1]))>MAX_EXPONENT):
        raise ValueError("JSON numeric exponent exceeds syntax budget")
    return Decimal(token)


def _json_integer(token):
    if len(token)>MAX_NUMBER_CHARS:
        raise ValueError("JSON integer token exceeds syntax budget")
    return int(token)


def parse_sol_usd(observation: JupiterPriceResponse, *,
                  bounds: TrustedSlotBounds) -> SolUsdObservation:
    """Return MEASURED or UNKNOWN; invalid adapter/bounds raise ValueError.

    MEASURED is a historical, source-bound USD valuation observation, not a
    fill, route, entry authorization, guaranteed oracle truth or continuous
    certification. All unavailable response cases retain exact original bytes.
    Bounds are retained to make the trusted time/slot dependency reviewable.
    """
    if type(observation) is not JupiterPriceResponse or type(bounds) is not TrustedSlotBounds:
        raise ValueError("typed coordinator inputs required")
    if observation.method != "GET" or observation.url != PRICE_URL:
        raise ValueError("exact credential-free SOL price request required")
    if type(observation.raw_payload) is not bytes or len(observation.raw_payload) > MAX_RESPONSE_BYTES:
        raise ValueError("bounded immutable response bytes required")
    if not _integer(observation.acquired_at) or type(observation.http_status) is not int:
        raise ValueError("invalid transport metadata")
    if not all(_integer(v) for v in (bounds.now, bounds.observed_at, bounds.min_slot, bounds.max_slot)):
        raise ValueError("invalid trusted clock/slot bounds")
    if not 0 <= bounds.max_slot - bounds.min_slot <= 32:
        raise ValueError("trusted slot range exceeds 32 slots")
    if type(bounds.block_times) is not tuple or not 1 <= len(bounds.block_times) <= 33:
        raise ValueError("bounded immutable exact block times required")
    times = {}
    for pair in bounds.block_times:
        if (type(pair) is not tuple or len(pair) != 2 or not all(_integer(v) for v in pair)
                or not bounds.min_slot <= pair[0] <= bounds.max_slot or pair[0] in times):
            raise ValueError("invalid or duplicate trusted block time")
        times[pair[0]] = pair[1]
    if any(time > bounds.observed_at for time in times.values()):
        raise ValueError("block time exceeds trusted capture time")
    ordered = [times[slot] for slot in sorted(times)]
    if ordered != sorted(ordered):
        raise ValueError("contradictory trusted block ordering")

    blockers = []
    slot = price_at = price = None
    if not 0 <= bounds.now - bounds.observed_at <= 10:
        blockers.append("TRUSTED_SLOT_CAPTURE_STALE_OR_FUTURE")
    if not 0 <= bounds.now - observation.acquired_at <= 10:
        blockers.append("ACQUISITION_STALE_OR_FUTURE")
    if observation.http_status != 200:
        blockers.append("HTTP_NOT_200")
    try:
        payload = json.loads(observation.raw_payload.decode("utf-8"),
                             object_pairs_hook=_unique_object, parse_float=_json_decimal,
                             parse_int=_json_integer,
                             parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError, DecimalException):
        payload = None
        blockers.append("MALFORMED_JSON")
    if not isinstance(payload, dict):
        blockers.append("RESPONSE_OBJECT_REQUIRED")
    elif SOL_MINT not in payload or payload[SOL_MINT] is None:
        blockers.append("SOL_PRICE_MISSING")
    elif not isinstance(payload[SOL_MINT], dict):
        blockers.append("SOL_PRICE_OBJECT_REQUIRED")
    else:
        item = payload[SOL_MINT]
        value = item.get("usdPrice")
        if type(value) not in (int, Decimal) or not Decimal(value).is_finite() or value <= 0:
            blockers.append("USD_PRICE_FINITE_POSITIVE_NUMBER_REQUIRED")
        else:
            price = Decimal(value)
        if type(item.get("decimals")) is not int or item["decimals"] != 9:
            blockers.append("SOL_DECIMALS_9_REQUIRED")
        candidate = item.get("blockId")
        if not _integer(candidate) or candidate == 0:
            blockers.append("PRICE_BLOCK_ID_REQUIRED")
        else:
            slot = candidate
            if not bounds.min_slot <= slot <= bounds.max_slot:
                blockers.append("PRICE_BLOCK_OUTSIDE_TRUSTED_RANGE")
            elif slot not in times:
                blockers.append("PRICE_BLOCK_TIME_UNKNOWN")
            else:
                price_at = times[slot]
                if not 0 <= bounds.now - price_at <= 30:
                    blockers.append("PRICE_BLOCK_STALE_OR_FUTURE")
    request = (observation.method, observation.url)
    request_json = json.dumps({"method": request[0], "url": request[1]},
                              sort_keys=True, separators=(",", ":"))
    return SolUsdObservation(
        "UNKNOWN" if blockers else "MEASURED", None if blockers else price,
        slot, price_at, observation.acquired_at, observation.http_status, tuple(blockers), request,
        hashlib.sha256(request_json.encode()).hexdigest(), observation.raw_payload,
        hashlib.sha256(observation.raw_payload).hexdigest(), bounds)
