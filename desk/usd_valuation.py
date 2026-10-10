"""SOL/USD valuation v2: Jupiter PriceV3 primary, Kraken fallback and cross-check (`paper_usd_valuation_version: 2`).

Valuation only, never execution or safety evidence. Pure functions over RETAINED attempt records (the same
`paper_read_attempt_v1` records the transport saves for every request, successful or not): no network, no
filesystem, no clock of its own. Everything here is a deterministic function of (records, decision time, config),
so saved events replay byte-identically.

Source selection (see `choose`):
  * the primary (Jupiter PriceV3) is used when it is fresh and strictly valid;
  * otherwise the fallback (Kraken SOLUSD recent trade) when that is fresh and valid;
  * when both are fresh and differ by more than `usd_divergence_max_fraction` (default 0.01) NEW ENTRIES get the
    normal no-entry blocker USD_SOURCE_DIVERGENCE (never a latch); exits are never blocked by divergence and use
    the primary (or the fallback when the primary is unavailable);
  * neither fresh: SOL_USD_UNAVAILABLE (entries: normal no-entry; exits: the held pass reports it as usual).

PriceV3 carries a Solana `blockId` but this valuation does not date the price by chain time (that needs two extra
RPC reads, the reason the Kraken-only version 1 exists): freshness is the acquisition time of the retained
attempt against the price TTL, and `blockId` is only required to be present and sane. A price therefore proves
"the provider said X when we asked", not "the chain was at slot Y", and the Kraken cross-check is what bounds a
wrong primary on entries.
"""
import base64
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext
import hashlib
import json
from urllib.parse import urlencode

from . import kraken_usd_observation as kraken
from .model import digest
from .sol_usd_observation import (SOL_MINT, MAX_RESPONSE_BYTES, _json_decimal, _json_integer, _reject_constant,
                                  _unique_object)

VERSION = 2
SOURCE = 'usd-valuation-v2'
JUPITER_SOURCE_ID = 'jupiter-price-v3-sol-paper-v1'     # PaperReadSources.price_source_id
JUPITER_METHOD = 'jupiter_price_v3'
JUPITER_PARAMS = {'ids': SOL_MINT}
KEY_DIVERGENCE = 'usd_divergence_max_fraction'
DEFAULT_DIVERGENCE = Decimal('0.01')
MAX_DIVERGENCE = Decimal('0.25')
PRICE_TTL_SECONDS = 10                                    # the same TTL as the other observations
# Conservative sanity range for a SOL/USD price. A provider value outside it is treated as malformed (fail closed);
# it is not a market bound and never clamps or replaces a value.
PRICE_RANGE = (Decimal('0.01'), Decimal('1000000'))
PRE_CHECK_CODES = frozenset({'PERSISTED_ADMISSION_REQUIRED', 'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED',
                             'CYCLE_REQUEST_BUDGET_EXHAUSTED', 'CYCLE_DEADLINE_UNAVAILABLE', 'WALL_CLOCK_UNAVAILABLE',
                             'SHARED_BUDGET_CHARGE_OR_IDENTITY_MISMATCH', 'MONITORING_CHARGE_OR_ADMISSION_MISMATCH',
                             'MONITORING_REQUEST_BUDGET_EXHAUSTED'})
BLOCKER_DIVERGENCE = 'USD_SOURCE_DIVERGENCE'
BLOCKER_UNAVAILABLE = 'SOL_USD_UNAVAILABLE'
REQUESTS = {'jupiter': 1, 'kraken': 1}


def selected(cfg):
    """0 when the key is absent, 1 for the Kraken-only version, 2 for this one; anything else fails closed."""
    if 'paper_usd_valuation_version' not in cfg:
        return 0
    version = cfg['paper_usd_valuation_version']
    if type(version) is int and version == 1:
        return kraken.selected(cfg)
    if (type(version) is not int or version != VERSION or cfg.get('mode') != 'paper'
            or type(cfg.get('paper_signal_policy_version')) is not int or cfg['paper_signal_policy_version'] != 3
            or type(cfg.get('paper_quote_execution_version')) is not int or cfg['paper_quote_execution_version'] != 1):
        raise ValueError('USD valuation 2 requires explicit paper USD2/signal3/quote1')
    max_divergence(cfg)
    return VERSION


def max_divergence(cfg):
    value = cfg.get(KEY_DIVERGENCE, DEFAULT_DIVERGENCE)
    if type(value) is bool or type(value) not in (int, float, str, Decimal):
        raise ValueError('Divergence limit must be a number')
    try:
        number = Decimal(str(value))
    except DecimalException:
        raise ValueError('Divergence limit must be a number') from None
    if not number.is_finite() or not Decimal(0) < number <= MAX_DIVERGENCE:
        raise ValueError('Divergence limit outside (0, 0.25]')
    return number


def fresh_requests(version):
    """Requests a fresh entry reserves: 9 (version 0), 7 (Kraken only), 8 (Jupiter + one Kraken cross-check)."""
    return {1: 7, 2: 8}.get(version, 9) if type(version) is int else 9


@dataclass(frozen=True)
class Observation:
    source: str                  # 'JUPITER' | 'KRAKEN'
    status: str                  # 'MEASURED' | 'UNKNOWN'
    usd_price: Decimal | None
    blockers: tuple
    attempt_hash: str
    payload_sha256: str | None
    request_sha256: str
    price_at: int | None = None  # Jupiter: acquisition second; Kraken: the trade second (the event's freshness anchor)


@dataclass(frozen=True)
class Decision:
    status: str                  # PRIMARY | FALLBACK | DIVERGENCE | UNAVAILABLE
    source: str | None
    usd_price: Decimal | None
    divergence: Decimal | None
    blockers: tuple
    price_at: int | None = None


@dataclass(frozen=True)
class Measured:
    """What the market adapter needs from a decision: the price and its freshness anchor."""
    usd_price: Decimal
    price_at: int
    status: str = 'MEASURED'
    blockers: tuple = ()


def _request_hash(url):
    return hashlib.sha256(json.dumps({'method': 'GET', 'url': url}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _record_shape(record, *, scan, source_id, method, params):
    expected = {'kind', 'scan_id', 'requests_used', 'source_id', 'method', 'params', 'request_bytes_base64',
                'response_bytes_base64', 'observed_at', 'http_status', 'failure_code'}
    if source_id == kraken.SOURCE:
        expected = expected | {'acquired_at_decimal'}
    if type(record) is not dict or set(record) not in (expected, expected | {'monitoring_reservation'}):
        raise ValueError('USD attempt exact shape')
    if (record['kind'] != 'paper_read_attempt_v1' or record['scan_id'] != scan or record['source_id'] != source_id
            or record['method'] != method or record['params'] != params
            or type(record['requests_used']) is not int or not 0 <= record['requests_used'] <= 18
            or record['request_bytes_base64'] != base64.b64encode(urlencode(params).encode('ascii')).decode()):
        raise ValueError('USD attempt binding')
    if record['failure_code'] is not None and type(record['failure_code']) is not str:
        raise ValueError('USD attempt failure code')


def _wire(record, limit):
    wire = record['response_bytes_base64']
    if wire is None:
        return None
    if type(wire) is not str or len(wire) > 4 * ((limit + 2) // 3):
        raise ValueError('USD wire byte bound')
    raw = base64.b64decode(wire, validate=True)
    if base64.b64encode(raw).decode() != wire:
        raise ValueError('USD wire canonical encoding')
    return raw


def parse_jupiter(raw, *, acquired_at, http_status, now, ttl=PRICE_TTL_SECONDS):
    """(price | None, blockers, block_id). Strict: finite positive usdPrice, present blockId, 9 decimals, fresh."""
    blockers = []
    price = block = None
    if type(acquired_at) is not int or type(now) is not int or not 0 <= acquired_at < 2**63 or not 0 <= now < 2**63:
        raise ValueError('Integer acquisition and decision times required')
    if not 0 <= now - acquired_at <= ttl:
        blockers.append('ACQUISITION_STALE_OR_FUTURE')
    if http_status != 200:
        blockers.append('HTTP_NOT_200')
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_RESPONSE_BYTES:
            raise ValueError('bytes')
        with localcontext() as ctx:
            ctx.prec = 512
            payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object, parse_float=_json_decimal,
                                 parse_int=_json_integer, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError, DecimalException):
        return None, tuple(blockers + ['MALFORMED_JSON']), None
    if type(payload) is not dict:
        return None, tuple(blockers + ['RESPONSE_OBJECT_REQUIRED']), None
    item = payload.get(SOL_MINT)
    if item is None:
        return None, tuple(blockers + ['SOL_PRICE_MISSING']), None
    if type(item) is not dict:
        return None, tuple(blockers + ['SOL_PRICE_OBJECT_REQUIRED']), None
    value = item.get('usdPrice')
    if type(value) not in (int, Decimal) or not Decimal(value).is_finite() or value <= 0:
        blockers.append('USD_PRICE_FINITE_POSITIVE_NUMBER_REQUIRED')
    elif not PRICE_RANGE[0] <= Decimal(value) <= PRICE_RANGE[1]:
        blockers.append('USD_PRICE_OUT_OF_SANITY_RANGE')
    else:
        price = Decimal(value)
    if type(item.get('decimals')) is not int or item['decimals'] != 9:
        blockers.append('SOL_DECIMALS_9_REQUIRED')
    candidate = item.get('blockId')
    if type(candidate) is not int or not 0 < candidate < 2**63:
        blockers.append('PRICE_BLOCK_ID_REQUIRED')
    else:
        block = candidate
    if blockers:
        price = None
    return price, tuple(blockers), block


def observe_jupiter(record, *, now, scan):
    """Observation of a retained PriceV3 attempt (failed attempts are UNKNOWN with their transport code)."""
    _record_shape(record, scan=scan, source_id=JUPITER_SOURCE_ID, method=JUPITER_METHOD, params=JUPITER_PARAMS)
    key = digest(record)
    request = _request_hash('https://api.jup.ag/price/v3?' + urlencode(JUPITER_PARAMS))
    raw = _wire(record, MAX_RESPONSE_BYTES)
    if record['failure_code'] is not None or raw is None:
        return Observation('JUPITER', 'UNKNOWN', None, (record['failure_code'] or 'NO_RESPONSE',), key,
                           None if raw is None else hashlib.sha256(raw).hexdigest(), request)
    if type(record['observed_at']) is not int or type(record['http_status']) is not int:
        raise ValueError('USD attempt transport metadata')
    price, blockers, _block = parse_jupiter(raw, acquired_at=record['observed_at'], http_status=record['http_status'], now=now)
    return Observation('JUPITER', 'UNKNOWN' if blockers else 'MEASURED', price, blockers, key,
                       hashlib.sha256(raw).hexdigest(), request, None if blockers else record['observed_at'])


def observe_kraken(record, *, now, scan):
    """Observation of a retained Kraken attempt through the unchanged Kraken parser."""
    _record_shape(record, scan=scan, source_id=kraken.SOURCE, method=kraken.METHOD, params=kraken.PARAMS)
    key = digest(record)
    raw = _wire(record, kraken.MAX_BYTES)
    request = _request_hash(kraken.URL)
    if record['failure_code'] is not None or raw is None:
        return Observation('KRAKEN', 'UNKNOWN', None, (record['failure_code'] or 'NO_RESPONSE',), key,
                           None if raw is None else hashlib.sha256(raw).hexdigest(), request)
    if (type(record['observed_at']) is not int or record['observed_at'] != int(kraken.timestamp(record['acquired_at_decimal']))
            or type(record['http_status']) is not int):
        raise ValueError('USD attempt transport metadata')
    parsed = kraken.parse_kraken_usd(kraken.KrakenTradesResponse('GET', kraken.URL, raw, record['acquired_at_decimal'],
                                                                 record['http_status']),
                                     bounds=kraken.TrustedTimeBounds(now))
    return Observation('KRAKEN', parsed.status, parsed.usd_price, parsed.blockers, key, parsed.payload_sha256, request,
                       parsed.price_at)


def divergence(primary_price, fallback_price):
    """|primary - fallback| / primary, exact."""
    with localcontext() as ctx:
        ctx.prec = 60
        return abs(primary_price - fallback_price) / primary_price


def choose(primary, fallback, *, purpose, limit):
    """Pure source selection. `purpose` is 'entry' or 'exit'; `limit` the divergence fraction (Decimal)."""
    if purpose not in ('entry', 'exit'):
        raise ValueError('USD purpose entry/exit required')
    good_primary = primary is not None and primary.status == 'MEASURED'
    good_fallback = fallback is not None and fallback.status == 'MEASURED'
    if good_primary and good_fallback:
        gap = divergence(primary.usd_price, fallback.usd_price)
        if gap > limit and purpose == 'entry':
            return Decision('DIVERGENCE', None, None, gap, (BLOCKER_DIVERGENCE,))
        return Decision('PRIMARY', 'JUPITER', primary.usd_price, gap, (), primary.price_at)
    if good_primary:
        return Decision('PRIMARY', 'JUPITER', primary.usd_price, None, (), primary.price_at)
    if good_fallback:
        return Decision('FALLBACK', 'KRAKEN', fallback.usd_price, None, (), fallback.price_at)
    return Decision('UNAVAILABLE', None, None, None, (BLOCKER_UNAVAILABLE,))


def _summary(observation, record):
    return None if observation is None else {
        'attempt_hash': observation.attempt_hash, 'attempt': record, 'status': observation.status,
        'blockers': list(observation.blockers),
        'usd_price': None if observation.usd_price is None else str(observation.usd_price),
        'price_at': observation.price_at, 'payload_sha256': observation.payload_sha256, 'request_sha256': observation.request_sha256}


def decide(attempts, *, now, scan, purpose, cfg):
    """(Decision, evidence-without-decision-fields) from {'primary': record|None, 'fallback': record|None}."""
    if type(attempts) is not dict or set(attempts) != {'primary', 'fallback'}:
        raise ValueError('USD attempts exact shape')
    now_int = int(kraken.timestamp(now))
    primary = None if attempts['primary'] is None else observe_jupiter(attempts['primary'], now=now_int, scan=scan)
    fallback = None if attempts['fallback'] is None else observe_kraken(attempts['fallback'], now=now, scan=scan)
    limit = max_divergence(cfg)
    decision = choose(primary, fallback, purpose=purpose, limit=limit)
    return decision, {'primary': _summary(primary, attempts['primary']), 'fallback': _summary(fallback, attempts['fallback']),
                      'divergence': None if decision.divergence is None else str(decision.divergence),
                      'divergence_max_fraction': str(limit)}


def evidence(attempts, *, now, scan, purpose, cfg):
    """The saved event valuation. Raises when no price may be used (the caller turns that into a blocker)."""
    decision, body = decide(attempts, now=now, scan=scan, purpose=purpose, cfg=cfg)
    if decision.usd_price is None:
        raise ValueError('USD valuation unavailable: ' + ','.join(decision.blockers))
    return {'version': VERSION, 'source': SOURCE, 'purpose': 'USD_VALUATION_ONLY', 'scan_id': scan,
            'decision_at': now, 'decision_purpose': purpose, 'selection': decision.status,
            'selected_source': decision.source, 'usd_price': str(decision.usd_price),
            'price_at': decision.price_at, **body,
            'source_authentication': 'PROVIDER_OBSERVATION_NOT_CRYPTOGRAPHIC', 'solana_slot_witness': None}


def purpose_of(event):
    return 'entry' if event.get('kind') == 'market' else 'exit'


def validate_event(event, cfg):
    """Rebuild the saved valuation from its retained originals; any difference refuses the event."""
    value = event.get('paper_usd_valuation')
    if event.get('kind') not in ('market', 'quote_exit'):
        if value is not None:
            raise ValueError('USD valuation on unsupported event')
        return
    if type(value) is not dict or type(value.get('decision_at')) is not str or int(kraken.timestamp(value['decision_at'])) != event['ts']:
        raise ValueError('USD event clock/config binding')
    if value.get('version') != VERSION or value.get('source') != SOURCE:
        raise ValueError('USD valuation version binding')
    attempts = {'primary': (value.get('primary') or {}).get('attempt'), 'fallback': (value.get('fallback') or {}).get('attempt')}
    rebuilt = evidence(attempts, now=value['decision_at'], scan=value['scan_id'], purpose=purpose_of(event), cfg=cfg)
    if rebuilt != value:
        raise ValueError('USD saved valuation modified')
    source = event['paper_source_evidence'] if event['kind'] == 'market' else event['source_evidence']
    if value['scan_id'] != source.get('scan_id'):
        raise ValueError('USD event scan binding')
    if event['kind'] == 'market':
        if event.get('sol_usd') != value['usd_price'] or source.get('usd') != summary_for_source(value):
            raise ValueError('USD market valuation binding')


def summary_for_source(value):
    """The attempt-free projection stored beside the event's other source evidence."""
    return {k: v for k, v in value.items() if k not in ('primary', 'fallback')} | {
        'primary': None if value['primary'] is None else {k: v for k, v in value['primary'].items() if k != 'attempt'},
        'fallback': None if value['fallback'] is None else {k: v for k, v in value['fallback'].items() if k != 'attempt'}}
