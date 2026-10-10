"""Batched reserve-implied PORTFOLIO marks (opt-in ``paper_portfolio_mark_source_version: 1``). Paper only.

One ``getMultipleAccounts`` over the PumpSwap pool and both vaults of EVERY open position (one request, one slot
context) refreshes all portfolio marks at once, so the 10 s held-mark freshness rule can stay unchanged with N
positions. The mark is a constant-product value from same-slot base/quote vault balances, net of the pool fee
hypothesis, the PumpSwap creator fee and a Token-2022 transfer fee where known, the cfg adverse slippage and the
fixed fee: the same model as ``tools/ops/held_watcher.implied_ratio`` (parity is tested).

It is used ONLY for portfolio valuation, exposure and daily equity checks. Stop, trailing and take-profit decisions
and every SELL fill still need the executable Jupiter quote path; the engine keeps these marks in separate position
keys (``portfolio_mark_*``) and never lets them satisfy an exit-side freshness check.

Pure functions only: no clock, store or provider access. The cycle owns the charged read and the evidence.
"""
import base64
import binascii
import json
from decimal import Decimal, localcontext

from .model import digest

KEY = 'paper_portfolio_mark_source_version'
FEE_KEY = 'paper_portfolio_mark_pool_fee_bps'
VERSION = 1
GUARDED_VERSION = 2
DIVERGENCE_KEY = 'paper_portfolio_mark_max_divergence'
DEFAULT_DIVERGENCE = '0.03'
SOURCE = 'reserve_implied_vault_marks_v1'
KIND = 'portfolio_marks'
MAX_POSITIONS = 8
MAX_MARK_AGE_SECONDS = 10

TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN_2022 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
WSOL = 'So11111111111111111111111111111111111111112'
ATA_PROGRAM = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'
ZERO, ONE = Decimal(0), Decimal(1)
OPTIONS = {'encoding': 'base64', 'commitment': 'confirmed', 'minContextSlot': 0}


class MarkError(ValueError):
    pass


def selected(cfg):
    """0 when absent; 1 or 2 when valid; otherwise fail closed. Requires the concurrent-entries experiment.

    Version 1: the model marks feed every portfolio valuation (equity, peak, daily limits, sizing).
    Version 2 (T35F): model marks feed exposure, position limits and the freshness gates, but `peak_equity`,
    `day_start_equity` and the daily drawdown/liquidation use the executable mark unless the model mark is within
    `paper_portfolio_mark_max_divergence` (default 3 %) of it."""
    if KEY not in cfg:
        if DIVERGENCE_KEY in cfg:
            raise ValueError('Mark divergence bound requires the guarded portfolio mark source')
        return 0
    value = cfg[KEY]
    if type(value) is not int or value not in (VERSION, GUARDED_VERSION):
        raise ValueError('Unsupported portfolio mark source version')
    flag = cfg.get('paper_concurrent_entries_version')
    quote = cfg.get('paper_quote_execution_version')
    if (cfg.get('mode') != 'paper' or type(flag) is not int or flag != 1
            or type(quote) is not int or quote != 1):
        raise ValueError('Portfolio mark source requires paper mode, quote execution 1 and the concurrent entries experiment')
    pool_fee_bps(cfg)
    if value == VERSION and DIVERGENCE_KEY in cfg:
        raise ValueError('Mark divergence bound requires the guarded portfolio mark source')
    max_divergence(cfg)
    return value


def max_divergence(cfg):
    """The guard's bound as a Decimal fraction (version 2 only; None otherwise)."""
    if cfg.get(KEY) != GUARDED_VERSION:
        return None
    raw = cfg.get(DIVERGENCE_KEY, DEFAULT_DIVERGENCE)
    if type(raw) is not str or not 1 <= len(raw) <= 12:
        raise ValueError('Mark divergence bound must be a decimal string')
    try:
        bound = Decimal(raw)
    except ArithmeticError:
        raise ValueError('Mark divergence bound invalid') from None
    if not bound.is_finite() or not ZERO < bound < ONE:
        raise ValueError('Mark divergence bound out of range')
    return bound


def pool_fee_bps(cfg):
    """The operator's pool fee hypothesis for reserve-implied marks (decimal string, config-hash bound)."""
    value = cfg.get(FEE_KEY)
    if type(value) is not str or not 1 <= len(value) <= 12:
        raise ValueError('Portfolio mark pool fee hypothesis required (decimal string)')
    try:
        fee = Decimal(value)
    except ArithmeticError:
        raise ValueError('Portfolio mark pool fee hypothesis invalid') from None
    if not fee.is_finite() or not 0 <= fee < 10000:
        raise ValueError('Portfolio mark pool fee hypothesis out of range')
    return fee


# --------------------------------------------------------------------------- account decoding

def _account_bytes(account, what):
    if not isinstance(account, dict) or account.get('owner') not in (TOKEN_PROGRAM, TOKEN_2022):
        raise MarkError(what + '_NOT_TOKEN_ACCOUNT')
    data = account.get('data')
    if not isinstance(data, list) or len(data) != 2 or data[1] != 'base64' or not isinstance(data[0], str):
        raise MarkError(what + '_DATA_ENCODING')
    try:
        return base64.b64decode(data[0], validate=True)
    except (ValueError, binascii.Error):
        raise MarkError(what + '_DATA_ENCODING') from None


def decode_token_account(account, expected_mint, expected_owner):
    """Raw amount of an initialized, undelegated-agnostic SPL / Token-2022 token account of ``expected_mint`` owned by the pool."""
    raw = _account_bytes(account, 'VAULT')
    if len(raw) < 165 or raw[108] != 1:
        raise MarkError('VAULT_NOT_INITIALIZED')
    from .security import base58
    if base58(raw[:32]) != expected_mint:
        raise MarkError('VAULT_MINT_MISMATCH')
    if base58(raw[32:64]) != expected_owner:
        raise MarkError('VAULT_OWNER_MISMATCH')
    return int.from_bytes(raw[64:72], 'little')


def mint_facts(account):
    """(decimals, transfer_fee_bps): decimals of the mint, and the Token-2022 TransferFeeConfig's NEWER schedule
    (its epoch is not checked; the per-transfer maximum is ignored, the conservative direction) or 0."""
    raw = _account_bytes(account, 'MINT')
    if len(raw) < 82 or raw[45] != 1:
        raise MarkError('MINT_NOT_INITIALIZED')
    decimals, fee_bps = raw[44], 0
    if account['owner'] == TOKEN_2022 and len(raw) > 165:
        if raw[165] != 1:
            raise MarkError('MINT_ACCOUNT_TYPE')
        offset = 166
        while offset + 4 <= len(raw):
            kind, length = int.from_bytes(raw[offset:offset + 2], 'little'), int.from_bytes(raw[offset + 2:offset + 4], 'little')
            body = raw[offset + 4:offset + 4 + length]
            if len(body) != length:
                raise MarkError('MINT_EXTENSION_MALFORMED')
            if kind == 1:
                if length != 108:
                    raise MarkError('MINT_EXTENSION_MALFORMED')
                fee_bps = int.from_bytes(body[106:108], 'little')
            offset += 4 + length
    return decimals, fee_bps


# --------------------------------------------------------------------------- pricing

def implied_value(position, base_raw, quote_raw, cfg, pool_fee_bps, creator_fee_bps=0, transfer_fee_bps=0,
                  virtual_quote_raw=0):
    """Net SOL value of the position's remaining quantity from one same-slot reserve pair."""
    decimals = position['quote_execution']['mint_decimals']
    qty, cost = Decimal(position['qty']), Decimal(position['cost_left'])
    if cost <= ZERO or qty <= ZERO:
        raise MarkError('POSITION_NOT_PRICEABLE')
    for value in (base_raw, quote_raw, virtual_quote_raw):
        if type(value) is not int or not 0 <= value < 2 ** 64:
            raise MarkError('RESERVE_RANGE')
    with localcontext() as context:
        context.prec = 60
        token_reserve = Decimal(base_raw) / (Decimal(10) ** decimals)
        sol_reserve = Decimal(quote_raw + virtual_quote_raw) / Decimal(10 ** 9)
        if sol_reserve <= ZERO or token_reserve < ZERO:
            return ZERO                                   # drained pool: worth nothing, the worst case
        effective = (qty * (ONE - Decimal(transfer_fee_bps) / 10000)
                     * (ONE - (Decimal(pool_fee_bps) + Decimal(creator_fee_bps)) / 10000))
        out = sol_reserve * effective / (token_reserve + effective)
        out *= ONE - Decimal(cfg['adverse_slippage_bps']) / 10000
        return max(ZERO, out - Decimal(cfg['fixed_fee_sol']))


def implied_ratio(position, *args, **kwargs):
    return implied_value(position, *args, **kwargs) / Decimal(position['cost_left'])


def pair_value(position, base, quote, cfg, pool_fee_bps, **facts):
    """``base`` and ``quote`` are (raw, slot). A pair read at different slots is never priced (a swap changes both
    vaults in one slot, so a lone or mismatched side is a half-applied swap)."""
    if base[1] != quote[1] or type(base[1]) is not int:
        raise MarkError('VAULT_PAIR_SLOT_MISMATCH')
    return implied_value(position, base[0], quote[0], cfg, pool_fee_bps, **facts)


# --------------------------------------------------------------------------- request

def _original_mint_account(position):
    try:
        original = json.loads(position['quote_execution']['original_mint_json'])
        return original['account']
    except (KeyError, TypeError, ValueError):
        raise MarkError('POSITION_MINT_EVIDENCE_MISSING') from None


def vault_addresses(position, mint):
    """(base_vault, quote_vault): associated token accounts of the pool, derived (never trusted from the response)."""
    try:
        from solders.pubkey import Pubkey
    except ImportError:
        raise MarkError('SOLDERS_REQUIRED') from None
    pool = position['pool']
    program = _original_mint_account(position).get('owner')
    if program not in (TOKEN_PROGRAM, TOKEN_2022):
        raise MarkError('POSITION_MINT_PROGRAM_UNSUPPORTED')
    ata = Pubkey.from_string(ATA_PROGRAM)
    def derive(token_program, token):
        return str(Pubkey.find_program_address([bytes(Pubkey.from_string(pool)),
            bytes(Pubkey.from_string(token_program)), bytes(Pubkey.from_string(token))], ata)[0])
    return derive(program, mint), derive(TOKEN_PROGRAM, WSOL)


def request_keys(positions):
    """Deterministic account list for ``getMultipleAccounts``: [pool, base vault, quote vault] per mint, sorted by mint."""
    if not positions or len(positions) > MAX_POSITIONS:
        raise MarkError('POSITION_COUNT')
    keys = []
    for mint in sorted(positions):
        base, quote = vault_addresses(positions[mint], mint)
        keys += [positions[mint]['pool'], base, quote]
    if len(set(keys)) != len(keys):
        raise MarkError('DUPLICATE_ACCOUNT')
    return keys


def request_params(positions):
    return [request_keys(positions), dict(OPTIONS)]


# --------------------------------------------------------------------------- response

CLOSED_REASONS = ('POOL_CLOSED', 'VAULT_CLOSED')
# A null account in ONE answer is only an observation (commitment lag, a transient provider gap). The engine counts
# consecutive null observations per position and values the position at zero, with the CLOSED reason, only after
# CLOSED_CONFIRMATIONS of them (distinct observations); any ordinary answer for the position resets the count.
NULL_REASONS = ('POOL_NULL', 'VAULT_NULL')
CLOSED_CONFIRMATIONS = 2
CONFIRMED = {'POOL_NULL': 'POOL_CLOSED', 'VAULT_NULL': 'VAULT_CLOSED'}


def NULL_REASON_BY_MISSING(pool_account, base_account, quote_account):
    if pool_account is None:
        return 'POOL_NULL'
    if base_account is None or quote_account is None:
        return 'VAULT_NULL'
    return None


def marks_from_result(positions, keys, result, cfg, *, pool_fee_bps):
    """``({mint: mark}, slot, {mint: error})`` from one ``getMultipleAccounts`` result.

    Every account of the response shares ONE context slot, so a vault pair is same-slot by construction; a missing
    or malformed slot rejects the whole response. A position whose pool or vaults fail verification is dropped
    (its mark stays stale and the engine rejects with STALE_PORTFOLIO) while the others are still refreshed."""
    if not isinstance(result, dict) or not isinstance(result.get('context'), dict):
        raise MarkError('CONTEXT_MISSING')
    slot = result['context'].get('slot')
    if type(slot) is not int or not 0 <= slot < 2 ** 63:
        raise MarkError('SLOT_INVALID')
    values = result.get('value')
    if not isinstance(values, list) or len(values) != len(keys):
        raise MarkError('VALUE_COUNT')
    from .pools import parse_pool
    marks, errors = {}, {}
    for index, mint in enumerate(sorted(positions)):
        position = positions[mint]
        pool_account, base_account, quote_account = values[3 * index:3 * index + 3]
        try:
            if keys[3 * index] != position['pool']:
                raise MarkError('KEY_ORDER')
            closed = NULL_REASON_BY_MISSING(pool_account, base_account, quote_account)
            if closed:
                # A null pool/vault account is an OBSERVATION of closure, not yet proof: the engine confirms it over
                # CLOSED_CONFIRMATIONS consecutive observations before valuing the position at zero (valuation only).
                marks[mint] = {'value_sol': '0', 'pool': position['pool'], 'base_raw': '0', 'quote_raw': '0', 'reason': closed}
                continue
            fields = parse_pool(pool_account)
            base_vault, quote_vault = keys[3 * index + 1], keys[3 * index + 2]
            if (fields['base_mint'] != mint or fields['quote_mint'] != WSOL
                    or fields['pool_base_token_account'] != base_vault
                    or fields['pool_quote_token_account'] != quote_vault):
                raise MarkError('POOL_BINDING_MISMATCH')
            decimals, transfer_fee = mint_facts(_original_mint_account(position))
            if decimals != position['quote_execution']['mint_decimals']:
                raise MarkError('DECIMALS_MISMATCH')
            base_raw = decode_token_account(base_account, mint, position['pool'])
            quote_raw = decode_token_account(quote_account, WSOL, position['pool'])
            value = pair_value(position, (base_raw, slot), (quote_raw, slot), cfg, pool_fee_bps,
                               creator_fee_bps=int(fields.get('creator_fee_bps') or 0), transfer_fee_bps=transfer_fee,
                               virtual_quote_raw=int(fields.get('virtual_quote_reserves') or 0))
            marks[mint] = {'value_sol': format(value, 'f'), 'pool': position['pool'],
                           'base_raw': str(base_raw), 'quote_raw': str(quote_raw)}
        except (MarkError, ValueError, KeyError, TypeError) as error:
            errors[mint] = str(error)[:64]
    return marks, slot, errors


def build_event(ts, observed_at, slot, evidence_hash, marks):
    """The ledger event that carries the marks. ``evidence_hash`` is the retained original attempt record."""
    body = {'schema_version': 1, 'kind': KIND, 'ts': ts, 'source': SOURCE, 'observed_at': observed_at, 'slot': slot,
            'evidence_hash': evidence_hash, 'marks': marks}
    return {**body, 'event_id': 'paper-marks:' + digest(body)}


def validate_event(e):
    """Strict shape; raises ValueError. Binding of event_id to content makes replays exact."""
    keys = {'schema_version', 'kind', 'ts', 'source', 'observed_at', 'slot', 'evidence_hash', 'marks', 'event_id'}
    if type(e) is not dict or set(e) != keys or e['schema_version'] != 1 or e['kind'] != KIND or e['source'] != SOURCE:
        raise ValueError('Portfolio marks event shape')
    for name in ('ts', 'observed_at', 'slot'):
        if type(e[name]) is not int or not 0 <= e[name] < 2 ** 63:
            raise ValueError('Portfolio marks event integer')
    if not 0 <= e['ts'] - e['observed_at'] <= MAX_MARK_AGE_SECONDS:
        raise ValueError('Portfolio marks observation not current')
    if type(e['evidence_hash']) is not str or len(e['evidence_hash']) != 64:
        raise ValueError('Portfolio marks evidence hash')
    marks = e['marks']
    if type(marks) is not dict or not 1 <= len(marks) <= MAX_POSITIONS:
        raise ValueError('Portfolio marks mapping')
    for mint, mark in marks.items():
        base_keys = {'value_sol', 'pool', 'base_raw', 'quote_raw'}
        if (type(mint) is not str or type(mark) is not dict or set(mark) not in (base_keys, base_keys | {'reason'})
                or any(type(v) is not str or not 1 <= len(v) <= 64 for v in mark.values())):
            raise ValueError('Portfolio mark entry')
        if 'reason' in mark and (mark['reason'] not in NULL_REASONS or mark['value_sol'] != '0'):
            raise ValueError('Portfolio mark reason')
        try:
            value = Decimal(mark['value_sol'])
        except ArithmeticError:
            raise ValueError('Portfolio mark value') from None
        if not value.is_finite() or value < 0 or value > Decimal(10) ** 12:
            raise ValueError('Portfolio mark value range')
    body = {k: v for k, v in e.items() if k != 'event_id'}
    if e['event_id'] != 'paper-marks:' + digest(body):
        raise ValueError('Portfolio marks event hash mismatch')
