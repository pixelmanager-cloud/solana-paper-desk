"""Lean candidate intake and per-candidate screening.

``scan_new`` / ``iter_new_candidates`` read migration hints from ``discovery/continuous.sqlite`` strictly
read-only, using a persisted ``seq`` cursor and an age window. ``screen`` runs the cheap pool/vault/mint/market
checks for ONE candidate through an injected providers object and returns a ``Screen``; it never raises for a
bad candidate or a failing provider (the failure is recorded on the Screen and the caller moves on).

Only pure helpers from ``desk/`` are imported (frame decoder, PumpSwap pool parser, mint/holding policy). The
desk's gates, receipts, pins, monitoring budget and pacing database are deliberately NOT used.

Providers contract (``lean/providers.py``): ``providers.helius.get_multiple_accounts(pubkeys)`` and
``providers.helius.rpc(method, params)``; ``providers.kraken.sol_usd()``. Each returns ``(parsed, raw_bytes, meta)``
or raises an error carrying ``code`` and ``transient``. ``parsed`` of ``get_multiple_accounts`` is the RPC result
object ``{"context": {"slot": n}, "value": [account|null, ...]}`` with base64 account data.

Deliberately NOT checked, compared with the desk: ownership/funding history and common-ownership clustering;
bundle detection; developer launch history; the dynamic/global fee-config accounts (the fee model is the
strategy's assumption, not proof); a sell-route simulation (the strategy's exit quote is the only sellability
evidence); trade-flow/churn/momentum proxies; Token-2022 holder-account extension scans beyond the desk's
profile-2 mint rules; finality authentication of the discovery frame.
"""
from contextlib import closing
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time

from desk.decode import decode
from desk.graduation_witness import _pda
from desk.model import digest
from desk.pools import ATA, parse_pool
from desk.programs import address, unbase58
from desk.providers import PUMP, PUMPSWAP, SOL
from desk.security import TOKEN_2022, TOKEN_PROGRAM, account_bytes, base58, holding_policy, mint_policy

MAX_FRAME_BYTES = 200_000
MAX_SNAPSHOT_SLOT_DRIFT = 16
DEFAULTS = {
    'min_age_seconds': 300, 'max_age_seconds': 7200,
    'min_market_cap_usd': 50_000, 'max_market_cap_usd': 2_000_000, 'min_liquidity_usd': 8_000,
    'token_profile_version': 2, 'holder_check': True, 'max_top10_pct': 60,
}
NOT_CHECKED = (
    'OWNERSHIP_AND_FUNDING_HISTORY', 'BUNDLE_DETECTION', 'DEVELOPER_HISTORY', 'FEE_CONFIG_ACCOUNTS',
    'SELL_ROUTE_SIMULATION', 'FLOW_CHURN_MOMENTUM', 'FRAME_FINALITY_AUTHENTICATION',
)


# ----------------------------------------------------------------------------------------- candidates
@dataclass(frozen=True)
class Candidate:
    seq: int
    mint: str
    pool: str
    signature: str
    slot: int
    migrated_at: float
    payload_hash: str


@dataclass
class ScanResult:
    """The candidates of one scan plus the cursor to persist. Iterable/indexable like the candidate list, so a caller of
    ``iter_new_candidates`` can never lose ``next_cursor`` (it must persist it even when the batch had no candidate,
    e.g. 500 non-migration or too-old frames, or the scan never moves forward)."""
    candidates: list
    next_cursor: int
    stats: dict

    def __iter__(self):
        return iter(self.candidates)

    def __len__(self):
        return len(self.candidates)

    def __getitem__(self, index):
        return self.candidates[index]


class DiscoveryUnavailable(OSError):
    """The discovery database is missing, locked or unreadable right now: retry on the next pass (never a halt)."""
    code = 'DISCOVERY_UNAVAILABLE'
    transient = True


DISCOVERY_TIMEOUT = 2.0


def _cfg(cfg, name):
    default = DEFAULTS[name]
    if cfg is None:
        return default
    value = cfg.get(name, default) if isinstance(cfg, dict) else getattr(cfg, name, default)
    return default if value is None else value


def _connect_ro(path):
    p = Path(path)
    if os.path.islink(p):
        raise ValueError('SYMLINKED_DISCOVERY')
    info = p.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('DISCOVERY_NOT_A_REGULAR_FILE')
    p = p.resolve()
    # Always plain mode=ro, never immutable=1: discovery is a live DELETE-journal database. A mode=ro reader takes the
    # normal SHARED lock (so it never reads a half-written page), waits up to DISCOVERY_TIMEOUT while the writer holds
    # its lock, and never creates or writes a file (a rollback-journal store has no -wal/-shm).
    c = sqlite3.connect(p.as_uri() + '?mode=ro', uri=True, timeout=DISCOVERY_TIMEOUT)
    c.execute('PRAGMA query_only=1')
    return c


def read_cursor(path):
    """Persisted cursor (last fully processed discovery ``seq``); 0 when absent. A corrupt file raises."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return 0
    value = json.loads(text)
    if type(value) is not dict or type(value.get('seq')) is not int or value['seq'] < 0:
        raise ValueError('CURSOR_FILE_INVALID')
    return value['seq']


def write_cursor(path, seq):
    """Atomic replace (tmp + fsync + rename). The cursor only ever moves forward."""
    if type(seq) is not int or seq < 0:
        raise ValueError('CURSOR_INVALID')
    path = Path(path)
    if seq < read_cursor(path):
        raise ValueError('CURSOR_MUST_NOT_MOVE_BACKWARD')
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, json.dumps({'seq': seq}).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def expected_pool(mint):
    """The canonical PumpSwap migration pool for ``mint`` (index 0, creator = pool-authority PDA, SOL quote)."""
    authority = _pda([b'pool-authority', unbase58(mint)], PUMP)
    return _pda([b'pool', b'\0\0', unbase58(authority), unbase58(mint), unbase58(SOL)], PUMPSWAP)


def _event_hint(raw, decoded, parent):
    """Exactly one decoded CompletePumpAmmMigrationEvent as the depth-2 child of the migrate instruction."""
    path = parent['instruction']
    if parent.get('status') != 'IDENTIFIED' or not path.isdecimal() or '.' in path:
        return False
    container = raw['params']['result']['transaction'] if raw.get('method') == 'transactionNotification' else raw
    groups = [g for g in (container['meta'].get('innerInstructions') or [])
              if type(g.get('index')) is int and str(g['index']) == path]
    if len(groups) != 1:
        return False
    events = []
    for event in decoded['program_observations']:
        if (event.get('name') != 'CompletePumpAmmMigrationEvent' or event.get('status') != 'EVENT_DECODED'
                or event.get('schema_complete') is not True or event.get('program') != parent['program']):
            continue
        parts = event['instruction'].split('.')
        if len(parts) != 2 or parts[0] != path or not parts[1].isdecimal():
            continue
        i = int(parts[1])
        instructions = groups[0]['instructions']
        if i >= len(instructions) or type(instructions[i].get('stackHeight')) is not int or instructions[i]['stackHeight'] != 2:
            continue
        fields = event.get('fields', {})
        if fields.get('mint') != parent.get('mint') or fields.get('pool') != parent.get('pool'):
            continue
        events.append(event)
    return len(events) == 1


def _frame_candidate(seq, received, slot, stored_hash, payload, stats):
    stats['frames'] += 1
    try:
        raw = json.loads(payload, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite')))
        if digest(raw) != stored_hash:
            stats['altered'] += 1
            return None
        decoded = decode(raw)
        if decoded['status'] != 'OBSERVED':
            stats['not_migration'] += 1
            return None
        found = [o for o in decoded['program_observations']
                 if o.get('name') in ('migrate', 'migrate_v2') and o.get('status') == 'IDENTIFIED'
                 and _event_hint(raw, decoded, o)]
        if not found:
            stats['not_migration'] += 1
            return None
        if len(found) > 1:
            stats['ambiguous'] += 1
            return None
        mint, pool = found[0]['mint'], found[0]['pool']
        address(mint), address(pool)
        if pool != expected_pool(mint):
            stats['unsupported_pool'] += 1
            return None
        signature = decoded['signature']
        if type(signature) is not str or len(unbase58(signature)) != 64:
            stats['undecodable'] += 1
            return None
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError):
        stats['undecodable'] += 1
        return None
    return Candidate(seq, mint, pool, signature, slot, float(received), stored_hash)


def scan_new(discovery_db, cursor=0, *, now=None, limit=500, min_age_seconds=None, max_age_seconds=None, cfg=None):
    """New migration candidates after ``cursor`` inside the age window; also returns the cursor to persist.

    Frames are visited in ``seq`` order. A frame younger than ``min_age_seconds`` stops the scan WITHOUT advancing
    past it (it is not ready yet; later frames are younger still). A frame older than ``max_age_seconds`` is
    counted and skipped. A frame whose bytes do not match its stored hash is counted and never used, never trusted.
    The same mint is returned once per batch.
    """
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError('SCAN_ARGUMENTS_INVALID')
    now = time.time() if now is None else now
    low = _cfg(cfg, 'min_age_seconds') if min_age_seconds is None else min_age_seconds
    high = _cfg(cfg, 'max_age_seconds') if max_age_seconds is None else max_age_seconds
    stats = {'frames': 0, 'altered': 0, 'undecodable': 0, 'not_migration': 0, 'ambiguous': 0,
             'unsupported_pool': 0, 'too_old': 0, 'duplicate_mint': 0, 'waiting_for_age': False, 'truncated': False}
    out, seen, next_cursor = [], set(), cursor
    try:
        return _scan(discovery_db, cursor, now, limit, low, high, stats, out, seen, next_cursor)
    except DiscoveryUnavailable:
        raise
    except (FileNotFoundError, PermissionError, sqlite3.Error) as error:
        raise DiscoveryUnavailable('DISCOVERY_UNAVAILABLE: %s' % type(error).__name__) from None


def _scan(discovery_db, cursor, now, limit, low, high, stats, out, seen, next_cursor):
    with closing(_connect_ro(discovery_db)) as d:
        d.execute('BEGIN')
        rows = d.execute('SELECT seq,received_at,slot,payload_hash,typeof(payload),length(CAST(payload AS BLOB)) '
                         'FROM raw_events WHERE seq>? ORDER BY seq LIMIT ?', (cursor, limit + 1)).fetchall()
        if len(rows) > limit:
            stats['truncated'], rows = True, rows[:limit]
        for seq, received, slot, stored_hash, kind, size in rows:
            if type(received) not in (int, float) or not math.isfinite(received) or type(slot) is not int:
                stats['altered'] += 1
                next_cursor = seq
                continue
            age = now - received
            if age < low:
                stats['waiting_for_age'] = True
                break
            next_cursor = seq
            if age > high:
                stats['too_old'] += 1
                continue
            if kind != 'text' or not 0 < size <= MAX_FRAME_BYTES:
                stats['altered'] += 1
                continue
            payload = d.execute('SELECT payload FROM raw_events WHERE seq=?', (seq,)).fetchone()[0]
            candidate = _frame_candidate(seq, received, slot, stored_hash, payload, stats)
            if candidate is None:
                continue
            if candidate.mint in seen:
                stats['duplicate_mint'] += 1
                continue
            seen.add(candidate.mint)
            out.append(candidate)
    return ScanResult(out, next_cursor, stats)


def initial_cursor(discovery_db, *, now, max_age_seconds):
    """The cursor for a FIRST start (no cursor file yet): just before the first frame newer than ``now -
    max_age_seconds``, so a new desk does not wade through the whole history. 0 for an empty database."""
    cutoff = now - max_age_seconds
    try:
        with closing(_connect_ro(discovery_db)) as d:
            first = d.execute('SELECT MIN(seq) FROM raw_events WHERE received_at>=?', (cutoff,)).fetchone()[0]
            if first is None:
                return d.execute('SELECT COALESCE(MAX(seq),0) FROM raw_events').fetchone()[0]
            return max(0, first - 1)
    except (FileNotFoundError, PermissionError, sqlite3.Error) as error:
        raise DiscoveryUnavailable('DISCOVERY_UNAVAILABLE: %s' % type(error).__name__) from None


def iter_new_candidates(discovery_db_path, cursor=0, **kwargs):
    """The ``ScanResult`` (iterable like the candidate list); persist ``.next_cursor`` after handling it."""
    return scan_new(discovery_db_path, cursor, **kwargs)


# --------------------------------------------------------------------------------------------- screening
@dataclass(frozen=True)
class Screen:
    passed: bool
    reasons: tuple
    features: dict
    unknowns: tuple = ()
    raw: tuple = ()          # ((label, raw provider bytes), ...) for the store's observations
    error: dict | None = None   # {'stage', 'code', 'transient'} when a provider failed; the candidate is failed, not rejected for cause


class _Stop(Exception):
    """Internal: stop screening this candidate (reasons already recorded)."""


class _Unavailable(Exception):
    """Internal: evidence that is required could not be fetched now (typed transient reject, never a pass)."""

    def __init__(self, code, provider_code, raw=None):
        super().__init__(code)
        self.code, self.provider_code, self.raw = code, provider_code, raw


def _dec(value):
    if isinstance(value, bool):
        raise ValueError('boolean')
    if type(value) is dict:
        for key in ('price', 'usd', 'sol_usd', 'value'):
            if key in value:
                return _dec(value[key])
        raise ValueError('no price key')
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError('non-positive')
    return result


def _unwrap(parsed):
    if type(parsed) is dict and 'value' not in parsed and type(parsed.get('result')) is dict:
        parsed = parsed['result']
    if type(parsed) is not dict or type(parsed.get('value')) is not list:
        raise ValueError('MALFORMED_PROVIDER_RESPONSE')
    slot = (parsed.get('context') or {}).get('slot')
    if type(slot) is not int or slot < 0:
        raise ValueError('MALFORMED_PROVIDER_RESPONSE')
    return slot, parsed['value']


def _ata(pool, program, mint):
    return _pda([unbase58(pool), unbase58(program), unbase58(mint)], ATA)


def _fmt(value):
    return format(value, 'f') if isinstance(value, Decimal) else value


def screen(candidate, providers, cfg=None, *, now=None, sol_usd=None):
    """Screen one candidate. Never raises for the candidate or its providers; see ``Screen``."""
    reasons, unknowns, raw, features = [], [], [], {}
    try:
        return _screen(candidate, providers, cfg, now, sol_usd, reasons, unknowns, raw, features)
    except _Stop:
        pass
    except _Unavailable as error:
        if isinstance(error.raw, (bytes, bytearray)):
            raw.append((features.get('stage') or 'unavailable', bytes(error.raw)))
        return Screen(False, tuple(dict.fromkeys(reasons)), features, tuple(unknowns), tuple(raw),
                      {'code': error.code, 'transient': True, 'stage': features.get('stage'), 'provider_code': error.provider_code})
    except Exception as error:   # per-candidate isolation: record, never halt the desk
        code = getattr(error, 'code', None)
        transient = getattr(error, 'transient', None)
        if type(code) is str and type(transient) is bool:
            if isinstance(getattr(error, 'raw', None), (bytes, bytearray)):
                raw.append((features.get('stage') or 'error', bytes(error.raw)))
            return Screen(False, tuple(reasons) + ('PROVIDER_ERROR:' + code,), features, tuple(unknowns), tuple(raw),
                          {'code': code, 'transient': transient, 'stage': features.get('stage')})
        reasons.append('SCREEN_INTERNAL_ERROR:' + type(error).__name__)
    return Screen(False, tuple(dict.fromkeys(reasons)), features, tuple(unknowns), tuple(raw))


def _call(label, features, raw, function, *args):
    features['stage'] = label
    parsed, body, _meta = function(*args)
    if isinstance(body, (bytes, bytearray)):
        raw.append((label, bytes(body)))
    return parsed


def _screen(candidate, providers, cfg, now, sol_usd, reasons, unknowns, raw, features):
    now = time.time() if now is None else now
    profile = _cfg(cfg, 'token_profile_version')
    age = now - candidate.migrated_at
    features['age_seconds'] = round(age, 3)
    if age < _cfg(cfg, 'min_age_seconds'):
        reasons.append('TOO_YOUNG')
    if age > _cfg(cfg, 'max_age_seconds'):
        reasons.append('TOO_OLD')
    try:
        address(candidate.mint), address(candidate.pool)
        if candidate.pool != expected_pool(candidate.mint):
            reasons.append('POOL_BINDING_INVALID')
    except ValueError:
        reasons.append('CANDIDATE_ADDRESS_INVALID')
    if reasons:
        raise _Stop
    # SOL/USD first: without it no USD bound can be evaluated.
    if sol_usd is None:
        parsed = _call('sol_usd', features, raw, providers.kraken.sol_usd)
        try:
            sol_usd = _dec(parsed)
        except (ValueError, InvalidOperation):
            reasons.append('SOL_USD_INVALID')
            raise _Stop
    else:
        sol_usd = _dec(sol_usd)
    features['sol_usd'] = _fmt(sol_usd)
    # Snapshot A: mint + pool.
    parsed = _call('accounts_mint_pool', features, raw, providers.helius.get_multiple_accounts, [candidate.mint, candidate.pool])
    try:
        slot_a, values = _unwrap(parsed)
        if len(values) != 2:
            raise ValueError('MALFORMED_PROVIDER_RESPONSE')
    except ValueError:
        reasons.append('MALFORMED_PROVIDER_RESPONSE')
        raise _Stop
    mint_account, pool_account = values
    policy = _mint(candidate, mint_account, profile, reasons)
    fields = _pool(candidate, pool_account, reasons)
    if reasons or policy is None or fields is None:
        raise _Stop
    features.update(mint=candidate.mint, pool=candidate.pool, token_program=policy['program'], decimals=policy['decimals'],
                    supply_raw=policy['supply_raw'], mint_snapshot_slot=slot_a,
                    pool_base_token_account=fields['pool_base_token_account'],
                    pool_quote_token_account=fields['pool_quote_token_account'])
    # Snapshot B: vaults + LP mint.
    parsed = _call('accounts_vaults_lp', features, raw, providers.helius.get_multiple_accounts,
                   [fields['pool_base_token_account'], fields['pool_quote_token_account'], fields['lp_mint']])
    try:
        slot_b, values = _unwrap(parsed)
        if len(values) != 3:
            raise ValueError('MALFORMED_PROVIDER_RESPONSE')
    except ValueError:
        reasons.append('MALFORMED_PROVIDER_RESPONSE')
        raise _Stop
    features['vault_snapshot_slot'] = slot_b
    if abs(slot_b - slot_a) > MAX_SNAPSHOT_SLOT_DRIFT:
        reasons.append('SNAPSHOT_SLOT_DRIFT')
    base, quote = _vaults(candidate, fields, mint_account, values[:2], profile, reasons)
    _lp(candidate, fields, values[2], features, reasons)
    if reasons or base is None or quote is None:
        raise _Stop
    _market(candidate, policy, fields, base, quote, sol_usd, cfg, features, reasons)
    if reasons:
        raise _Stop
    if _cfg(cfg, 'holder_check'):
        _holders(candidate, providers, policy, fields, cfg, features, raw, reasons, unknowns)
    if reasons:
        raise _Stop
    unknowns.extend(NOT_CHECKED)
    return Screen(True, (), features, tuple(dict.fromkeys(unknowns)), tuple(raw))


def _safe(reasons, code, function, *args):
    try:
        return function(*args)
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError):
        reasons.append(code)
        return None


def _mint(candidate, account, profile, reasons):
    if account is None:
        reasons.append('MINT_ACCOUNT_MISSING')
        return None
    policy = _safe(reasons, 'MINT_EVIDENCE_MALFORMED', lambda: mint_policy(account, mint=candidate.mint, token_profile_version=profile))
    if policy is None:
        return None
    if policy['decision'] != 'PASS_TOKEN_POLICY':
        reasons.extend(policy['reasons'] or ['MINT_POLICY_REJECTED'])
        return None
    return policy


def _pool(candidate, account, reasons):
    if account is None:
        reasons.append('POOL_ACCOUNT_MISSING')
        return None
    fields = _safe(reasons, 'POOL_EVIDENCE_MALFORMED', parse_pool, account)
    if fields is None:
        return None
    from solders.pubkey import Pubkey
    program = Pubkey.from_string(PUMPSWAP)
    expected, bump = Pubkey.find_program_address(
        [b'pool', fields['index'].to_bytes(2, 'little'), bytes(Pubkey.from_string(fields['creator'])),
         bytes(Pubkey.from_string(fields['base_mint'])), bytes(Pubkey.from_string(fields['quote_mint']))], program)
    if str(expected) != candidate.pool or bump != fields['pool_bump']:
        reasons.append('POOL_PDA_MISMATCH')
    if fields['base_mint'] != candidate.mint:
        reasons.append('POOL_BASE_MINT_MISMATCH')
    if fields['quote_mint'] != SOL:
        reasons.append('NON_SOL_QUOTE_POOL')
    if fields['index'] != 0 or fields['creator'] != _pda([b'pool-authority', unbase58(candidate.mint)], PUMP):
        reasons.append('NONCANONICAL_MIGRATION_POOL')
    if fields['lp_mint'] != _pda([b'pool_lp_mint', unbase58(candidate.pool)], PUMPSWAP):
        reasons.append('LP_MINT_PDA_MISMATCH')
    for flag, reason in (('is_mayhem_mode', 'MAYHEM_POOL'), ('is_cashback_coin', 'CASHBACK_POOL'),
                         ('is_holder_reward', 'HOLDER_REWARD_POOL'), ('can_edit_creator_fee', 'MUTABLE_CREATOR_FEE')):
        if fields[flag]:
            reasons.append(reason)
    if fields['creator_fee_bps']:
        reasons.append('POOL_CREATOR_FEE_OVERRIDE')
    if fields['unknown_trailing_bytes']:
        reasons.append('POOL_LAYOUT_HAS_UNKNOWN_EXTENSION')
    if len({fields['pool_base_token_account'], fields['pool_quote_token_account'], fields['lp_mint'], candidate.pool, candidate.mint}) != 5:
        reasons.append('POOL_ACCOUNT_IDENTITIES_OVERLAP')
    return fields


def _vaults(candidate, fields, mint_account, accounts, profile, reasons):
    result = []
    for key, mint_key, account in zip(('pool_base_token_account', 'pool_quote_token_account'), ('base_mint', 'quote_mint'), accounts):
        if account is None:
            reasons.append('VAULT_ACCOUNT_MISSING')
            result.append(None)
            continue
        program = account.get('owner') if type(account) is dict else None
        if program not in (TOKEN_PROGRAM, TOKEN_2022):
            reasons.append('UNSUPPORTED_VAULT_PROGRAM')
            result.append(None)
            continue
        if _safe(reasons, 'VAULT_EVIDENCE_MALFORMED', lambda: _ata(candidate.pool, program, fields[mint_key])) != fields[key]:
            reasons.append('VAULT_ATA_MISMATCH')
            result.append(None)
            continue
        checked = _safe(reasons, 'VAULT_EVIDENCE_MALFORMED',
                        lambda: holding_policy(account, fields[mint_key], candidate.pool, token_profile_version=profile))
        if checked is None:
            result.append(None)
            continue
        if checked['decision'] != 'PASS_HOLDING_POLICY':
            reasons.extend('VAULT_' + r for r in checked['reasons'])
            result.append(None)
            continue
        result.append({'program': program, 'amount_raw': int(checked['amount_raw'])})
    base, quote = result
    if base and base['program'] != mint_account.get('owner'):
        reasons.append('BASE_MINT_VAULT_PROGRAM_MISMATCH')
    if quote and quote['program'] != TOKEN_PROGRAM:
        reasons.append('QUOTE_VAULT_PROGRAM_UNSUPPORTED')
    return base, quote


def _lp(candidate, fields, account, features, reasons):
    if account is None or type(account) is not dict or account.get('owner') not in (TOKEN_PROGRAM, TOKEN_2022):
        reasons.append('LP_MINT_UNAVAILABLE')
        return
    data = _safe(reasons, 'LP_EVIDENCE_MALFORMED', account_bytes, account)
    if data is None:
        return
    if len(data) != 82 or account.get('executable') is not False or data[45] != 1:
        reasons.append('INVALID_LP_MINT_LAYOUT')
        return
    supply = int.from_bytes(data[36:44], 'little')
    features['lp_supply_raw'] = str(supply)
    if supply > 0:
        reasons.append('OUTSTANDING_WITHDRAWABLE_LP_SUPPLY')
    if int.from_bytes(data[:4], 'little') != 1 or base58(data[4:36]) != candidate.pool:
        reasons.append('UNEXPECTED_LP_MINT_AUTHORITY')
    if int.from_bytes(data[46:50], 'little') != 0:
        reasons.append('LP_FREEZE_AUTHORITY')


def _market(candidate, policy, fields, base, quote, sol_usd, cfg, features, reasons):
    gross = quote['amount_raw']
    fees = fields['protocol_fees'] + fields['creator_fees']
    spendable = gross - fees
    effective = gross + fields['virtual_quote_reserves']
    features.update(base_reserve_raw=str(base['amount_raw']), quote_gross_raw=str(gross), quote_spendable_raw=str(spendable),
                    quote_effective_raw=str(effective), virtual_quote_reserves_raw=str(fields['virtual_quote_reserves']),
                    boosted=bool(fields['virtual_quote_reserves'] or fees))
    if spendable <= 0 or not 0 < effective < 2**64 or base['amount_raw'] <= 0:
        reasons.append('POOL_RESERVES_INVALID')
        return
    with localcontext() as ctx:
        ctx.prec = 60
        supply_ui = Decimal(policy['supply_raw']) / (Decimal(10) ** policy['decimals'])
        base_ui = Decimal(base['amount_raw']) / (Decimal(10) ** policy['decimals'])
        # Pricing follows the pool's effective quote reserve; liquidity counts only physical, spendable SOL.
        price = (Decimal(effective) / Decimal(10) ** 9) / base_ui
        market_cap = supply_ui * price * sol_usd
        liquidity = 2 * (Decimal(spendable) / Decimal(10) ** 9) * sol_usd
    features.update(price_sol_per_token=_fmt(price), market_cap_usd=_fmt(market_cap), liquidity_usd=_fmt(liquidity))
    if market_cap < Decimal(str(_cfg(cfg, 'min_market_cap_usd'))):
        reasons.append('MARKET_CAP_BELOW_MIN')
    if market_cap > Decimal(str(_cfg(cfg, 'max_market_cap_usd'))):
        reasons.append('MARKET_CAP_ABOVE_MAX')
    if liquidity < Decimal(str(_cfg(cfg, 'min_liquidity_usd'))):
        reasons.append('LIQUIDITY_BELOW_MIN')


def _holders(candidate, providers, policy, fields, cfg, features, raw, reasons, unknowns):
    """One getTokenLargestAccounts call. A provider failure is a typed transient reject (HOLDERS_UNAVAILABLE, retried
    by the runner); a malformed or contradictory answer rejects the candidate."""
    features['stage'] = 'holders'
    try:
        parsed, body, _meta = providers.helius.rpc('getTokenLargestAccounts', [candidate.mint, {'commitment': 'confirmed'}])
    except Exception as error:
        if type(getattr(error, 'code', None)) is str and type(getattr(error, 'transient', None)) is bool:
            # Not a pass with concentration UNKNOWN: a typed transient reject, so the runner retries the candidate.
            features['holder_check'] = 'UNAVAILABLE:' + error.code
            reasons.append('HOLDERS_UNAVAILABLE')
            raise _Unavailable('HOLDERS_UNAVAILABLE', error.code, getattr(error, 'raw', None)) from None
        raise
    if isinstance(body, (bytes, bytearray)):
        raw.append(('holders', bytes(body)))
    try:
        _slot, rows = _unwrap(parsed)
        supply = int(policy['supply_raw'])
        amounts = []
        for row in rows:
            amount = int(row['amount'])
            if amount < 0:
                raise ValueError('negative')
            address(row['address'])
            if row['address'] != fields['pool_base_token_account']:
                amounts.append(amount)
        total = sum(amounts)
        if total > supply or supply <= 0:
            raise ValueError('holders exceed supply')
    except (ValueError, KeyError, TypeError):
        reasons.append('HOLDER_EVIDENCE_MALFORMED')
        return
    amounts.sort(reverse=True)
    top1 = Decimal(amounts[0] if amounts else 0) * 100 / Decimal(supply)
    top10 = Decimal(sum(amounts[:10])) * 100 / Decimal(supply)
    features.update(holder_check='OK', top1_pct_excluding_pool=_fmt(round(top1, 4)), top10_pct_excluding_pool=_fmt(round(top10, 4)))
    if top10 > Decimal(str(_cfg(cfg, 'max_top10_pct'))):
        reasons.append('HOLDER_CONCENTRATION_ABOVE_MAX')
