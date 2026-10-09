"""Versioned quote-based paper accounting; never transaction/fill verification.

Coordinator API: Ledger.apply(event, cfg, bind_transition(event, quotes),
engine.initial_state). Only trusted in-process coordinator code supplies quotes.
A new experiment must pin paper_quote_execution_version=1, fixed_fee_sol and
adverse_slippage_bps. Event JSON cannot opt in. Existing ledger fingerprints
refuse configuration changes/adoption and make event delivery idempotent.

Provider minimum output is additionally haircut by configured adverse slippage,
rounded DOWN in raw units. Quote-embedded pool fees/impact are already included;
fixed_fee_sol is an explicit per-swap network/priority-fee hypothesis. Rent,
transaction validity, actual fees and fills remain unverified. No network/I/O.
"""
from dataclasses import dataclass
from copy import deepcopy
from decimal import Decimal, localcontext
import re

from .live_observation import (QuoteObservation, SourceRecord, ProviderObservation,
                               ingest_mint, ingest_quote)
from .model import canonical, digest, decimal
from .programs import address
from .token2022_paper import selected
from .original_byte_slot_transport import _parse
from .original_byte_read_transport import _bounded_json

VERSION = 1
STATUS = 'EXECUTION_UNVERIFIED'
MAX_QUOTES = 8
MAX_SOURCE_BYTES = 256 * 1024


class QuoteExecutionError(ValueError):
    pass


def config(cfg):
    selected(cfg)
    version = cfg.get('paper_quote_execution_version')
    if version is None:
        return False
    if type(version) is not int or version != VERSION or cfg.get('mode') != 'paper':
        raise QuoteExecutionError('QUOTE_EXECUTION_CONFIG_INVALID')
    fee = decimal(cfg['fixed_fee_sol'])
    slip = decimal(cfg['adverse_slippage_bps'])
    with localcontext() as ctx:
        ctx.prec = 100
        lamports = fee * 10**9
        if fee < 0 or lamports != lamports.to_integral_value() or lamports >= 2**64:
            raise QuoteExecutionError('QUOTE_EXECUTION_FEE_INVALID')
    if slip != slip.to_integral_value() or not 0 <= slip < 10000:
        raise QuoteExecutionError('QUOTE_EXECUTION_SLIPPAGE_INVALID')
    if type(cfg.get('price_ttl_seconds')) is not int or not 0 < cfg['price_ttl_seconds'] <= 60:
        raise QuoteExecutionError('QUOTE_EXECUTION_TTL_INVALID')
    return True


def output_raw(quote, cfg):
    """Exact conservative raw output to request the next-leg quote against."""
    if not config(cfg) or type(quote) is not QuoteObservation:
        raise QuoteExecutionError('QUOTE_EXECUTION_REQUIRED')
    if type(quote.minimum_output_raw) is not int or not 0 < quote.minimum_output_raw < 2**64:
        raise QuoteExecutionError('QUOTE_OUTPUT_INVALID')
    return quote.minimum_output_raw * (10000 - int(decimal(cfg['adverse_slippage_bps']))) // 10000


def units(raw, decimals):
    with localcontext() as ctx:
        ctx.prec = 400
        return Decimal(raw).scaleb(-decimals)


def raw_quantity(value, decimals):
    with localcontext() as ctx:
        ctx.prec = 400
        scaled = decimal(value).scaleb(decimals)
        if scaled != scaled.to_integral_value() or not 0 < scaled < 2**64:
            raise QuoteExecutionError('QUOTE_QUANTITY_INVALID')
        return int(scaled)


def _original(source):
    if (type(source) is not SourceRecord or type(source.source_id) is not str
            or re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', source.source_id) is None
            or type(source.original_json) is not str
            or not 0 < len(source.original_json.encode()) <= MAX_SOURCE_BYTES):
        raise QuoteExecutionError('QUOTE_SOURCE_INVALID')
    value = _parse(source.original_json.encode())
    _bounded_json(value)
    if canonical(value) != source.original_json or digest(value) != source.raw_hash:
        raise QuoteExecutionError('QUOTE_SOURCE_BINDING_INVALID')
    return value


@dataclass(frozen=True)
class _Book:
    event_hash: str
    quotes: tuple
    decimals: int | None

    def find(self, direction, raw):
        matches = [q for q in self.quotes if q.direction == direction and q.input_raw == raw]
        if len(matches) != 1:
            raise QuoteExecutionError('EXACT_QUOTE_MISSING')
        return matches[0]

    def buy(self):
        matches = [q for q in self.quotes if q.direction == 'buy']
        if len(matches) != 1:
            raise QuoteExecutionError('EXACT_BUY_QUOTE_MISSING')
        return matches[0]

    def record(self, quote, cfg):
        return {'status': STATUS, 'version': VERSION, 'direction': quote.direction,
                'input_raw': quote.input_raw, 'estimated_output_raw': quote.estimated_output_raw,
                'provider_minimum_output_raw': quote.minimum_output_raw,
                'simulated_output_raw': output_raw(quote, cfg), 'mint_decimals': self.decimals,
                'quote_source_id': quote.source.source_id, 'quote_hash': quote.source.raw_hash,
                'quote_observed_at': quote.source.observed_at,
                'mint_source_id': quote.mint_source.source_id, 'mint_hash': quote.mint_source.raw_hash,
                'mint_observed_at': quote.mint_source.observed_at,
                'original_quote_json': quote.source.original_json,
                'original_mint_json': quote.mint_source.original_json,
                'assumptions': {'network_fee_sol': str(decimal(cfg['fixed_fee_sol'])),
                    'additional_adverse_slippage_bps': int(decimal(cfg['adverse_slippage_bps'])),
                    'output_basis': 'PROVIDER_MINIMUM_PLUS_ADVERSE_HAIRCUT_RAW_FLOOR',
                    'embedded_pool_fees_and_impact': 'ALREADY_IN_QUOTE',
                    'rent_and_actual_transaction_fees': 'UNVERIFIED'},
                'source_authenticated': False, 'transaction_verified': False,
                'actual_fill_verified': False, 'risk_flags': list(quote.risk_flags)}


def _book(event, quotes, cfg):
    if not config(cfg):
        raise QuoteExecutionError('QUOTE_EXECUTION_CONFIG_REQUIRED')
    if type(quotes) is not tuple or len(quotes) > MAX_QUOTES:
        raise QuoteExecutionError('QUOTE_SET_INVALID')
    if event.get('kind') in ('market','quote_exit'):
        try:address(event.get('taker'))
        except ValueError:
            raise QuoteExecutionError('QUOTE_EVENT_WALLET_REQUIRED') from None
    validated = []; identities = set(); decimals = None; mint_hash = None
    for quote in quotes:
        if type(quote) is not QuoteObservation:
            raise QuoteExecutionError('TYPED_QUOTE_REQUIRED')
        for value in (quote.input_raw, quote.estimated_output_raw, quote.minimum_output_raw):
            if type(value) is not int or not 0 < value < 2**64:
                raise QuoteExecutionError('QUOTE_AMOUNT_INVALID')
        for value in (quote.input_units, quote.estimated_output_units, quote.minimum_output_units, quote.estimated_sol_per_token):
            if type(value) is not Decimal or not value.is_finite():
                raise QuoteExecutionError('QUOTE_UNITS_INVALID')
        token_payload = _original(quote.mint_source)
        original = _original(quote.source)
        token = ingest_mint(lambda: ProviderObservation(quote.mint_source.source_id,
            quote.mint_source.observed_at, token_payload), mint=event['mint'], now=event['ts'],
            max_age_seconds=cfg['price_ttl_seconds'],token_profile_version=selected(cfg))
        replayed = ingest_quote(lambda: ProviderObservation(quote.source.source_id,
            quote.source.observed_at, original), mint=token, direction=quote.direction,
            amount_raw=quote.input_raw, taker=event.get('taker'), now=event['ts'],
            expected_pool=event['pool'], max_age_seconds=cfg['price_ttl_seconds'])
        if replayed != quote or (mint_hash is not None and mint_hash != token.source.raw_hash):
            raise QuoteExecutionError('QUOTE_REPLAY_MISMATCH')
        if event.get('kind')=='quote_exit':
            evidence=event['source_evidence']
            if (quote.direction!='sell' or token.decimals!=event['mint_decimals']
                    or token.source.raw_hash!=evidence['mint_hash']
                    or token.source.source_id!=evidence['rpc_source_id']
                    or quote.source.source_id!=evidence['quote_source_id']
                    or (quote.input_raw==event['current_quantity_raw'] and
                        (quote.source.raw_hash!=evidence['quote_hash'] or
                         quote.source.observed_at!=evidence['quote_at']))):
                raise QuoteExecutionError('EXIT_QUOTE_SOURCE_BINDING_MISMATCH')
        identity = (quote.direction, quote.input_raw)
        if identity in identities:
            raise QuoteExecutionError('DUPLICATE_QUOTE')
        identities.add(identity); validated.append(replayed)
        decimals = token.decimals; mint_hash = token.source.raw_hash
    return _Book(digest(event), tuple(validated), decimals)


def validate_exit_valuation(event, outcome, cfg):
    """Replay the original full-position mark independently of a partial action.

    Full-size actions already persist their primary source. Partial quote_exit
    fills must additionally retain it; absent historical evidence fails closed,
    without synthesizing a full quote from an action or reconstructing originals.
    This establishes local binding, never provider authenticity or actual fills.
    """
    from .model import validate_event
    validate_event(event)
    if event.get('kind') != 'quote_exit' or outcome.get('side') != 'sell':
        raise QuoteExecutionError('EXIT_VALUATION_CONTEXT_REQUIRED')
    action = outcome.get('quote_execution')
    if type(action) is not dict:
        raise QuoteExecutionError('EXIT_ACTION_RECORD_REQUIRED')
    if (action.get('input_raw') == event['current_quantity_raw']
            and 'valuation_quote_execution' in outcome):
        raise QuoteExecutionError('EXIT_REDUNDANT_VALUATION_RECORD')
    record = (action if action.get('input_raw') == event['current_quantity_raw']
              else outcome.get('valuation_quote_execution'))
    if (type(record) is not dict or record.get('direction') != 'sell'
            or type(record.get('input_raw')) is not int
            or record['input_raw'] != event['current_quantity_raw']):
        raise QuoteExecutionError('EXIT_FULL_VALUATION_RECORD_REQUIRED')
    ms = SourceRecord(record['mint_source_id'], record['mint_observed_at'],
                      record['mint_hash'], record['original_mint_json'])
    qs = SourceRecord(record['quote_source_id'], record['quote_observed_at'],
                      record['quote_hash'], record['original_quote_json'])
    token = ingest_mint(lambda: ProviderObservation(ms.source_id, ms.observed_at, _original(ms)),
        mint=event['mint'], now=event['ts'], max_age_seconds=cfg['price_ttl_seconds'],token_profile_version=selected(cfg))
    observation = ingest_quote(lambda: ProviderObservation(qs.source_id, qs.observed_at, _original(qs)),
        mint=token, direction='sell', amount_raw=record['input_raw'], taker=event['taker'],
        expected_pool=event['pool'], now=event['ts'], max_age_seconds=cfg['price_ttl_seconds'])
    book = _book(event, (observation,), cfg)
    source = event['source_evidence']
    if (token.slot != source['mint_slot'] or token.source.observed_at != source['mint_at']
            or canonical(book.record(observation,cfg)) != canonical(record)):
        raise QuoteExecutionError('EXIT_FULL_VALUATION_BINDING_INVALID')


def validate_position(mint, position, cfg):
    """Preserve quote provenance/assumptions on restart; never reconstruct state."""
    record=position.get('quote_execution')
    if type(record) is not dict or record.get('direction')!='buy':
        raise QuoteExecutionError('QUOTE_POSITION_RECORD_REQUIRED')
    quote_source=SourceRecord(record['quote_source_id'],record['quote_observed_at'],
                             record['quote_hash'],record['original_quote_json'])
    mint_source=SourceRecord(record['mint_source_id'],record['mint_observed_at'],
                            record['mint_hash'],record['original_mint_json'])
    at=position['opened_at']
    token=ingest_mint(lambda:ProviderObservation(mint_source.source_id,mint_source.observed_at,
        _original(mint_source)),mint=mint,now=at,max_age_seconds=cfg['price_ttl_seconds'],token_profile_version=selected(cfg))
    quote=ingest_quote(lambda:ProviderObservation(quote_source.source_id,quote_source.observed_at,
        _original(quote_source)),mint=token,direction='buy',amount_raw=record['input_raw'],
        taker=position['taker'],expected_pool=position['pool'],now=at,
        max_age_seconds=cfg['price_ttl_seconds'])
    book=_Book('',(quote,),token.decimals)
    with localcontext() as ctx:
        ctx.prec=400
        if (canonical(book.record(quote,cfg))!=canonical(record)
                or raw_quantity(position['initial_qty'],token.decimals)!=output_raw(quote,cfg)
                or raw_quantity(position['qty'],token.decimals)>output_raw(quote,cfg)
                or decimal(position['initial_cost'])!=quote.input_units+decimal(cfg['fixed_fee_sol'])
                or not 0<=decimal(position['cost_left'])<=decimal(position['initial_cost'])):
            raise QuoteExecutionError('QUOTE_POSITION_BINDING_INVALID')


def bind_transition(event, quotes):
    """Bind trusted observations to one exact event; never deserialize permission.

    Missing quotes are an empty tuple, not model fallback. Revalidation happens
    within Ledger.apply's transition. Bounds and cloning freeze caller arguments.
    The caller remains responsible for admission, source provisioning and clocks.
    """
    if type(event) is not dict or type(quotes) is not tuple or len(quotes) > MAX_QUOTES:
        raise QuoteExecutionError('QUOTE_SET_INVALID')
    event_hash = digest(event)
    frozen_quotes = tuple(quotes)
    def apply(state, delivered, cfg):
        if digest(delivered) != event_hash:
            raise QuoteExecutionError('QUOTE_EVENT_BINDING_MISMATCH')
        from .engine import transition
        if delivered.get('kind') not in ('market','quote_exit'):
            if frozen_quotes:
                raise QuoteExecutionError('NONMARKET_QUOTE_FORBIDDEN')
            config(cfg)
            return transition(state, delivered, cfg)
        with localcontext() as ctx:
            ctx.prec=400
            return transition(state, delivered, cfg, _quote_book=_book(delivered, frozen_quotes, cfg))
    return apply


def plan(state,event,cfg,quotes=()):
    """Read-only quote demands from the same engine gates/sizing/exit rules.

    Initial buy demand gives a risk-bounded maximum and minimum lamport input;
    coordinator chooses an exact input in that range. Supplying its buy quote
    then yields the exact conservatively simulated token quantity for the
    required reverse sell quote. An open position first needs its exact full
    quantity sell quote; supplying that mark quote may demand a separate partial
    sell quote. No quote is resized, no I/O/fill is committed by this planner.
    Invalid context raises; rejected gates are returned without quote demands.
    """
    if not config(cfg) or type(state) is not dict or type(event) is not dict:
        raise QuoteExecutionError('QUOTE_EXECUTION_CONFIG_REQUIRED')
    book=_book(event,quotes,cfg)
    from .engine import transition
    with localcontext() as ctx:
        ctx.prec=400
        _,outcomes=transition(deepcopy(state),deepcopy(event),cfg,_quote_book=book)
    demands=[]
    for outcome in outcomes:
        for demand in outcome.get('quote_demands',[]):
            if demand not in demands:demands.append(demand)
    return {'event_hash':digest(event),'quote_demands':demands,'outcomes':outcomes}
