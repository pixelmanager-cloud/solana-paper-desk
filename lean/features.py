"""Entry feature capture (L12, reworked in L12F): one ``observations(kind='features')`` row per screened candidate, entered or not.

Paper only, read-only, features only (nothing here is read by the strategy). The row is the canonical JSON of
``{features_version, mint, entered, not_entered_reason, screen_passed, screen_at, collected_at, calls, fields, missing}``:
``fields[name]`` is the value or ``None``; every ``None`` has a reason in ``missing[name]``. A missing field is never an error.
``screen_at`` is when the screen decided; the enrichment calls run a little later (``collected_at``), so a consumer that must not
look ahead (L19) can tell the two apart. The dev and holder figures are therefore "as of a few seconds after the screen".

Free, from what the screen already loaded (no request): market cap, liquidity, price, SOL/USD, age at screen, token program,
mint/freeze authority state, the Token-2022 extension list and transfer-fee bps (the retained mint account bytes), the DEV WALLET
(``coin_creator`` of the retained PumpSwap pool account: no paging of the mint's signatures), and, when the screen reached the
holder stage, top-1 / top-10 holder share from the retained ``getTokenLargestAccounts`` body. Both shares are of the TOTAL supply
and exclude the pool vault AND the burn addresses (the incinerator and the system program, as token accounts of this mint).

At most ``MAX_EXTRA_CALLS`` (3) extra Helius calls per candidate, always on the shared LOW lane (``lean.providers.low``: a call
goes out at once or fails with ``LANE_SHED`` and sends nothing; ``LANE_SHED`` ends the enrichment and the rest is recorded as
missing with that reason):
1. ``getTokenAccountsByOwner(dev wallet, mint)``  -> ``dev_holding_pct`` (skipped when the dev wallet is unknown);
2. ``getSignaturesForAddress(pool, 1000)``        -> ``graduation_block_time`` (the BLOCK time of the pool's first transaction, only when
   the history is complete: fewer than 1000 signatures and the oldest one is the graduation slot), ``seconds_graduation_to_screen``
   and ``tx_count_first_10m`` (successful pool transactions in the first 10 minutes, the pool-creation transaction itself excluded);
3. ``getTokenLargestAccounts(mint)``              -> the holder shares for a SOFT reject (the screen stopped before its holder stage);
   an entered / passed candidate already has them.

Not captured, and why (the fields were removed from the row in version 2 instead of staying null forever):
* ``holder_count``: needs a full holder scan (DAS ``getTokenAccounts`` or ``getProgramAccounts``), not allowed by ``lean.providers``;
* buys / sells / unique buyers / buy volume in the first minutes: need parsed transactions over the 3-call budget. L13 (``wallet_signals``)
  records unique buyers, same-slot buyers and snipers from the pool's first swaps; ``tx_count_first_10m`` is the activity figure here;
* ``seconds_creation_to_graduation``: needs the mint's first transaction; a graduated token has thousands of signatures, so it would take
  paging the mint's history (it is ``HISTORY_TRUNCATED`` after one page);
* twitter / telegram / website: need the metadata JSON (DAS ``getAsset`` or an HTTP fetch of the off-chain URI), neither allowed.

The enrichment runs on its own thread (``FeatureRecorder.run``) fed by a bounded queue; ``submit`` never blocks and never raises, so a
slow or failing provider cannot touch an entry. A full queue writes the cheap row at once with reason ``QUEUE_FULL``.
"""
import base64
import json
import queue
import threading
import time
from decimal import Decimal, InvalidOperation, localcontext

from desk.security import TOKEN_2022, TOKEN_PROGRAM

FEATURES_VERSION = 2
KIND = 'features'
MAX_EXTRA_CALLS = 3
EARLY_WINDOW_S = 600
SIGNATURE_LIMIT = 1000
LANE_SHED = 'LANE_SHED'
SYSTEM_PROGRAM = '11111111111111111111111111111111'
INCINERATOR = '1nc1nerator11111111111111111111111111111111'
BURN_OWNERS = (INCINERATOR, SYSTEM_PROGRAM)
ATA_PROGRAM = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'
TOKEN_2022_EXTENSIONS = {1: 'TransferFeeConfig', 2: 'TransferFeeAmount', 3: 'MintCloseAuthority', 4: 'ConfidentialTransferMint',
                         5: 'ConfidentialTransferAccount', 6: 'DefaultAccountState', 7: 'ImmutableOwner', 8: 'MemoTransfer',
                         9: 'NonTransferable', 10: 'InterestBearingConfig', 11: 'CpiGuard', 12: 'PermanentDelegate',
                         13: 'NonTransferableAccount', 14: 'TransferHook', 15: 'TransferHookAccount', 16: 'ConfidentialTransferFeeConfig',
                         17: 'ConfidentialTransferFeeAmount', 18: 'MetadataPointer', 19: 'TokenMetadata', 20: 'GroupPointer',
                         21: 'TokenGroup', 22: 'GroupMemberPointer', 23: 'TokenGroupMember'}
FIELDS = ('top10_pct', 'top1_pct', 'creator_wallet', 'dev_holding_pct', 'graduation_block_time', 'seconds_graduation_to_screen',
          'tx_count_first_10m', 'market_cap_usd', 'liquidity_usd', 'price_sol_per_token', 'age_seconds_at_screen',
          'mint_authority_active', 'freeze_authority_active', 'token_program', 'token2022_extensions', 'transfer_fee_bps', 'sol_usd')
# What the v1 row listed and v2 does not carry (documentation, and a guard for the tests: none of these may come back as a null column)
REMOVED_FIELDS = {'holder_count': 'REQUIRES_FULL_HOLDER_SCAN', 'buys_first_10m': 'NEEDS_PARSED_TRANSACTIONS_OVER_BUDGET',
                  'sells_first_10m': 'NEEDS_PARSED_TRANSACTIONS_OVER_BUDGET', 'unique_buyers_first_10m': 'NEEDS_PARSED_TRANSACTIONS_OVER_BUDGET',
                  'buy_volume_sol_first_10m': 'NEEDS_PARSED_TRANSACTIONS_OVER_BUDGET', 'has_twitter': 'METADATA_RPC_NOT_ALLOWED',
                  'has_telegram': 'METADATA_RPC_NOT_ALLOWED', 'has_website': 'METADATA_RPC_NOT_ALLOWED',
                  'seconds_creation_to_graduation': 'MINT_HISTORY_TOO_LONG_TO_PAGE'}
SOFT_REJECTS = frozenset({'MARKET_CAP_BELOW_MIN', 'MARKET_CAP_ABOVE_MAX', 'LIQUIDITY_BELOW_MIN'})
DEFAULTS = {'enabled': False, 'enrich': True, 'queue_size': 500}


def config(raw):
    """lean.json ``features`` -> validated settings. Absent / null = disabled. Unknown keys are refused."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError('features must be an object')
    unknown = set(raw) - set(DEFAULTS) - {'_comment'}
    if unknown:
        raise ValueError('features: unknown keys %s' % sorted(unknown))
    out = {k: raw.get(k, v) for k, v in DEFAULTS.items()}
    for key in ('enabled', 'enrich'):
        if type(out[key]) is not bool:
            raise ValueError('features.%s must be true or false' % key)
    if type(out['queue_size']) is not int or not 1 <= out['queue_size'] <= 100_000:
        raise ValueError('features.queue_size out of range')
    return out


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, default=str).encode()


def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _num(value):
    """A finite Decimal from a screen string/number, or None."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _pct(part, supply):
    with localcontext() as ctx:
        ctx.prec = 40
        return format(round(Decimal(part) * 100 / Decimal(supply), 4), 'f')


# ------------------------------------------------------------------------------------------------ Token-2022 mint bytes
def mint_extensions(data):
    """``(names, transfer_fee_bps)`` from raw mint account bytes. Legacy mints (82 bytes) have none. Raises ValueError if malformed."""
    if len(data) == 82:
        return [], None
    if len(data) < 166 or data[165] != 1:
        raise ValueError('not a Token-2022 mint')
    names, fee_bps, offset = [], None, 166
    while offset + 4 <= len(data):
        kind, length = int.from_bytes(data[offset:offset + 2], 'little'), int.from_bytes(data[offset + 2:offset + 4], 'little')
        body = data[offset + 4:offset + 4 + length]
        if kind == 0 and length == 0:
            break
        if len(body) != length:
            raise ValueError('truncated extension')
        names.append(TOKEN_2022_EXTENSIONS.get(kind, 'UNKNOWN_%d' % kind))
        if kind == 1 and length >= 108:                  # TransferFeeConfig: ... older (epoch,max,bps) then newer (epoch,max,bps)
            fee_bps = int.from_bytes(body[106:108], 'little')
        offset += 4 + length
    return names, fee_bps


def _raw_body(screen, label):
    for name, body in _get(screen, 'raw', ()) or ():
        if name == label:
            return body
    return None


def _mint_bytes(screen):
    """The mint account bytes the screen retained (label ``accounts_mint_pool``), or None."""
    body = _raw_body(screen, 'accounts_mint_pool')
    if body is None:
        return None
    try:
        value = json.loads(body)['result']['value'][0]
        return base64.b64decode(value['data'][0], validate=True)
    except (ValueError, KeyError, IndexError, TypeError):
        return None


# ------------------------------------------------------------------------------------------------ dev wallet and holders
def dev_wallet(screen):
    """``(coin_creator, None)`` from the PumpSwap pool account the screen already loaded, or ``(None, reason)``."""
    body = _raw_body(screen, 'accounts_mint_pool')
    if body is None:
        return None, 'POOL_ACCOUNT_NOT_RETAINED'
    try:
        account = json.loads(body)['result']['value'][1]
        if account is None:
            return None, 'POOL_ACCOUNT_MISSING'
        from desk.pools import parse_pool
        creator = parse_pool(account)['coin_creator']
    except (ValueError, KeyError, IndexError, TypeError):
        return None, 'POOL_ACCOUNT_UNREADABLE'
    if not isinstance(creator, str) or creator in BURN_OWNERS:
        return None, 'CREATOR_UNKNOWN'
    return creator, None


def burn_accounts(mint, token_program):
    """The token accounts of ``mint`` owned by the burn addresses (derived, no request): excluded from the holder shares."""
    from solders.pubkey import Pubkey
    program = Pubkey.from_string(token_program)
    ata = Pubkey.from_string(ATA_PROGRAM)
    return {str(Pubkey.find_program_address([bytes(Pubkey.from_string(owner)), bytes(program), bytes(Pubkey.from_string(mint))], ata)[0])
            for owner in BURN_OWNERS}


def holder_shares(rows, supply_raw, exclude):
    """``(top1_pct, top10_pct)`` of the TOTAL supply over the largest-account rows without the excluded token accounts.
    Raises ValueError on malformed rows or amounts above the supply."""
    supply = int(supply_raw)
    amounts = []
    if supply <= 0 or not isinstance(rows, list):
        raise ValueError('supply')
    for row in rows:
        amount = int(row['amount'])
        if amount < 0 or not isinstance(row['address'], str):
            raise ValueError('row')
        if row['address'] not in exclude:
            amounts.append(amount)
    if sum(amounts) > supply:
        raise ValueError('holders exceed supply')
    amounts.sort(reverse=True)
    return _pct(amounts[0] if amounts else 0, supply), _pct(sum(amounts[:10]), supply)


def _screen_rows(screen):
    """The ``getTokenLargestAccounts`` rows the screen retained (label ``holders``), or None."""
    body = _raw_body(screen, 'holders')
    if body is None:
        return None
    try:
        return json.loads(body)['result']['value']
    except (ValueError, KeyError, TypeError):
        return 'MALFORMED'


def exclusions(screen):
    """Token accounts that are not holders: the pool vault and the burn accounts. None when the screen has no mint / program."""
    f = _get(screen, 'features', {}) or {}
    mint, program = f.get('mint'), f.get('token_program')
    if not isinstance(mint, str) or program not in (TOKEN_PROGRAM, TOKEN_2022):
        return None
    out = set(burn_accounts(mint, program))
    if isinstance(f.get('pool_base_token_account'), str):
        out.add(f['pool_base_token_account'])
    return out


# ------------------------------------------------------------------------------------------------ the cheap part
def base_fields(candidate, screen):
    """``(fields, missing)`` from what the screen already loaded. Pure, no request."""
    f = _get(screen, 'features', {}) or {}
    reasons = [str(r) for r in (_get(screen, 'reasons', ()) or ())]
    fields, missing = {name: None for name in FIELDS}, {}

    def put(name, value, why):
        if value is None:
            missing[name] = why
        else:
            fields[name] = value
    for name, key in (('market_cap_usd', 'market_cap_usd'), ('liquidity_usd', 'liquidity_usd'), ('price_sol_per_token', 'price_sol_per_token'),
                      ('sol_usd', 'sol_usd')):
        value = _num(f.get(key))
        put(name, None if value is None else format(value, 'f'), 'SCREEN_STOPPED_BEFORE_MARKET')
    age = _num(f.get('age_seconds'))
    put('age_seconds_at_screen', None if age is None else format(age, 'f'), 'SCREEN_STOPPED_BEFORE_AGE')
    program = f.get('token_program')
    put('token_program', program if isinstance(program, str) else None, 'MINT_STAGE_NOT_REACHED')
    if isinstance(program, str):                         # the mint stage passed: both authorities were absent
        fields['mint_authority_active'] = fields['freeze_authority_active'] = False
    else:
        for name, why in (('mint_authority_active', 'ACTIVE_MINT_AUTHORITY'), ('freeze_authority_active', 'ACTIVE_FREEZE_AUTHORITY')):
            if why in reasons:
                fields[name] = True
            else:
                missing[name] = 'MINT_STAGE_NOT_REACHED'
    data = _mint_bytes(screen)
    if data is None:
        missing['token2022_extensions'] = missing['transfer_fee_bps'] = 'MINT_BYTES_NOT_RETAINED'
    else:
        try:
            names, fee = mint_extensions(data)
            fields['token2022_extensions'] = names
            if 'TransferFeeConfig' in names:
                put('transfer_fee_bps', fee, 'TRANSFER_FEE_UNREADABLE')
            else:
                missing['transfer_fee_bps'] = 'NO_TRANSFER_FEE_EXTENSION'
        except ValueError:
            missing['token2022_extensions'] = missing['transfer_fee_bps'] = 'MINT_EXTENSIONS_MALFORMED'
    creator, why = dev_wallet(screen)
    put('creator_wallet', creator, why)
    rows = _screen_rows(screen)
    skip = exclusions(screen)
    if rows is None or skip is None or f.get('supply_raw') is None:
        check = f.get('holder_check')
        why = 'HOLDER_CHECK_' + check if isinstance(check, str) and check.startswith('UNAVAILABLE') else 'HOLDERS_NOT_LOADED'
        missing['top10_pct'] = missing['top1_pct'] = why
    else:
        try:
            if rows == 'MALFORMED':
                raise ValueError('rows')
            fields['top1_pct'], fields['top10_pct'] = holder_shares(rows, f['supply_raw'], skip)
        except (ValueError, KeyError, TypeError):
            missing['top10_pct'] = missing['top1_pct'] = 'HOLDER_ROWS_MALFORMED'
    return fields, missing


# ------------------------------------------------------------------------------------------------ the extra calls
class _Budget(Exception):
    pass


def enrich(candidate, screen, helius, *, creator, need_holders, screen_time=None):
    """Up to ``MAX_EXTRA_CALLS`` read-only calls on the low lane. Returns ``(fields, missing, calls)``; never raises. A provider
    failure becomes the reason of its fields; ``LANE_SHED`` ends the enrichment (nothing was sent) with that reason on the rest."""
    fields, missing, calls = {}, {}, 0
    f = _get(screen, 'features', {}) or {}
    mint, pool = _get(candidate, 'mint'), _get(candidate, 'pool')
    pending = {'dev': ('dev_holding_pct',), 'pool': ('graduation_block_time', 'seconds_graduation_to_screen', 'tx_count_first_10m')}
    if need_holders:
        pending['holders'] = ('top10_pct', 'top1_pct')
    if creator is None:
        missing['dev_holding_pct'] = 'CREATOR_UNKNOWN'
        del pending['dev']
    supply = f.get('supply_raw')
    shed = [None]

    def finish(group, why):
        for name in pending.pop(group, ()):
            missing.setdefault(name, why)

    def rpc(method, params):
        nonlocal calls
        if calls >= MAX_EXTRA_CALLS:
            raise _Budget
        calls += 1
        return helius.rpc(method, params)[0]

    def failed(group, error):
        code = str(getattr(error, 'code', type(error).__name__))[:40]
        if code == LANE_SHED:
            shed[0] = LANE_SHED
        finish(group, 'PROVIDER_' + code if code != LANE_SHED else LANE_SHED)

    # 1. the dev wallet's balance
    if 'dev' in pending:
        try:
            owned = rpc('getTokenAccountsByOwner', [creator, {'mint': mint}, {'encoding': 'jsonParsed', 'commitment': 'confirmed'}])
            total = sum(int(a['account']['data']['parsed']['info']['tokenAmount']['amount']) for a in owned['value'])
            if supply is None or int(supply) <= 0 or total < 0 or total > int(supply):
                raise ValueError('amount')
            fields['dev_holding_pct'] = _pct(total, supply)
            pending.pop('dev')
        except (KeyError, IndexError, TypeError, ValueError):
            finish('dev', 'DEV_BALANCE_UNREADABLE')
        except Exception as error:                       # noqa: BLE001 - a provider failure is a missing field
            failed('dev', error)
    # 2. the pool's own history: graduation block time and the activity of the first 10 minutes
    if 'pool' in pending and shed[0] is None:
        try:
            sigs = rpc('getSignaturesForAddress', [pool, {'limit': SIGNATURE_LIMIT, 'commitment': 'confirmed'}])
            if not isinstance(sigs, list) or not sigs or not all(isinstance(s, dict) for s in sigs):
                finish('pool', 'SIGNATURES_UNAVAILABLE')
            elif len(sigs) >= SIGNATURE_LIMIT:
                finish('pool', 'HISTORY_TRUNCATED')
            else:
                oldest = min(sigs, key=lambda s: s['slot'] if type(s.get('slot')) is int else 2 ** 63)
                born = oldest.get('blockTime')
                if oldest.get('slot') != _get(candidate, 'slot'):
                    finish('pool', 'POOL_HISTORY_NOT_FROM_GRADUATION')
                elif type(born) is not int or isinstance(born, bool):
                    finish('pool', 'GRADUATION_BLOCK_TIME_UNKNOWN')
                else:
                    fields['graduation_block_time'] = born
                    fields['tx_count_first_10m'] = sum(1 for s in sigs if s is not oldest and s.get('err') is None and type(s.get('blockTime')) is int
                                                       and 0 <= s['blockTime'] - born <= EARLY_WINDOW_S)      # the pool-creation tx itself is not a trade
                    if screen_time is not None:
                        fields['seconds_graduation_to_screen'] = round(float(screen_time) - born, 3)
                    else:
                        missing['seconds_graduation_to_screen'] = 'SCREEN_TIME_UNKNOWN'
                    pending.pop('pool')
        except (KeyError, IndexError, TypeError, ValueError):
            finish('pool', 'SIGNATURES_UNREADABLE')
        except Exception as error:                       # noqa: BLE001
            failed('pool', error)
    # 3. holders of a soft reject
    if 'holders' in pending and shed[0] is None:
        skip = exclusions(screen)
        try:
            rows = rpc('getTokenLargestAccounts', [mint, {'commitment': 'confirmed'}])['value']
            if skip is None or supply is None:
                raise ValueError('mint')
            fields['top1_pct'], fields['top10_pct'] = holder_shares(rows, supply, skip)
            pending.pop('holders')
        except (KeyError, IndexError, TypeError, ValueError):
            finish('holders', 'HOLDER_ROWS_MALFORMED')
        except Exception as error:                       # noqa: BLE001
            failed('holders', error)
    for group in list(pending):
        finish(group, shed[0] or 'NOT_ENRICHED')
    return fields, missing, calls


# ------------------------------------------------------------------------------------------------ the row
def build_row(candidate, screen, *, entered, reason=None, extra=None, extra_missing=None, default_why=None, screen_at=None,
              collected_at=None, calls=0):
    """The features row payload. ``extra`` / ``extra_missing`` come from ``enrich``; fields it did not cover get ``default_why``."""
    fields, missing = base_fields(candidate, screen)
    for name, value in (extra or {}).items():
        fields[name] = value
        missing.pop(name, None)
    for name, why in (extra_missing or {}).items():
        if fields[name] is None:
            missing[name] = why
    for name in ('dev_holding_pct', 'graduation_block_time', 'seconds_graduation_to_screen', 'tx_count_first_10m'):
        if fields[name] is None and name not in missing:
            missing[name] = default_why or 'NOT_ENRICHED'
    for name in FIELDS:                                  # the invariant of the schema: None <=> a reason
        if fields[name] is None and name not in missing:
            missing[name] = default_why or 'UNKNOWN'
        if fields[name] is not None:
            missing.pop(name, None)
    return {'features_version': FEATURES_VERSION, 'mint': _get(candidate, 'mint'), 'entered': bool(entered),
            'not_entered_reason': None if entered else reason, 'screen_passed': bool(_get(screen, 'passed')),
            'screen_at': screen_at, 'collected_at': collected_at, 'calls': calls, 'fields': fields, 'missing': missing}


def hazard_free(screen):
    """True for a PASS or a screen rejected ONLY for the soft market bands (no hazard): worth the enrichment calls."""
    if _get(screen, 'passed'):
        return True
    reasons = list(_get(screen, 'reasons', ()) or ())
    return bool(reasons) and all(r in SOFT_REJECTS for r in reasons)


class FeatureRecorder:
    """Writes one ``features`` row per candidate. ``submit`` / ``on_candidate`` are non-blocking and never raise."""

    def __init__(self, *, store, helius, code_version, strategy_version, clock=time.time, queue_size=500, enrich_enabled=True):
        self.store, self.helius, self.clock = store, helius, clock
        self.code_version, self.strategy_version = code_version, strategy_version
        self.enrich_enabled = enrich_enabled
        self.queue = queue.Queue(maxsize=queue_size)
        self.stats = {'rows': 0, 'enriched': 0, 'shed': 0, 'queue_full': 0, 'errors': 0, 'calls': 0, 'dropped': 0}
        self.last_error = None
        self._written = set()
        self._lock = threading.Lock()

    def _count_error(self, error):
        self.stats['errors'] += 1
        self.last_error = type(error).__name__

    def _write(self, candidate, candidate_id, screen, entered, reason, screen_at, **kwargs):
        payload = build_row(candidate, screen, entered=entered, reason=reason, screen_at=screen_at, collected_at=self.clock(), **kwargs)
        # the same payload in ``meta`` (queryable; the report never selects the ``raw`` blob) and as canonical bytes in ``raw``
        self.store.add_observation(KIND, canonical(payload), mint=payload['mint'], candidate_id=candidate_id, meta=payload,
                                   code_version=self.code_version, strategy_version=self.strategy_version, ts=self.clock())
        self.stats['rows'] += 1

    # -- what became of the candidate (read from the store the runner just wrote) --------------------------------------
    def outcome(self, candidate_id, mint, screen):
        """``(entered, reason)``: a BUY fill of this candidate, else its last entry decision, else the screen's own reasons."""
        if candidate_id is not None:
            if any(f['side'] == 'buy' and f['candidate_id'] == candidate_id for f in self.store.rows('fills', mint=mint, limit=1000)):
                return True, None
            entry = [d for d in self.store.rows('decisions', mint=mint, kind='entry', limit=1000) if d['candidate_id'] == candidate_id]
            if entry:
                reasons = json.loads(entry[-1]['reasons'])
                return False, ','.join(str(r) for r in reasons) or entry[-1]['action']
        if not _get(screen, 'passed'):
            return False, ','.join(str(r) for r in (_get(screen, 'reasons', ()) or ())) or None
        return False, 'ABORTED_BY_ERROR'

    def on_candidate(self, candidate, candidate_id, screen):
        """Runner hook: the candidate has been handled; record what became of it. Never raises."""
        try:
            entered, reason = self.outcome(candidate_id, _get(candidate, 'mint'), screen)
            return self.submit(candidate, screen, entered=entered, reason=reason, candidate_id=candidate_id, screen_at=self.clock())
        except Exception as error:                       # noqa: BLE001 - a recorder failure never reaches the trader
            self._count_error(error)
            self.stats['dropped'] += 1
            return False

    def submit(self, candidate, screen, *, entered, reason=None, candidate_id=None, screen_at=None):
        """Queue the candidate for its row. Never raises, never blocks; True when accepted (queued or written)."""
        try:
            mint = _get(candidate, 'mint')
            with self._lock:
                key = (mint, candidate_id)
                if key in self._written:
                    return False
                self._written.add(key)
                if len(self._written) > 100000:
                    self._written.clear()
            if not self.enrich_enabled or self.helius is None:
                self._write(candidate, candidate_id, screen, entered, reason, screen_at, default_why='ENRICHMENT_DISABLED')
                return True
            try:
                self.queue.put_nowait((candidate, screen, entered, reason, candidate_id, screen_at))
            except queue.Full:
                self.stats['queue_full'] += 1
                self._write(candidate, candidate_id, screen, entered, reason, screen_at, default_why='QUEUE_FULL')
            return True
        except Exception as error:                       # noqa: BLE001
            self._count_error(error)
            self.stats['dropped'] += 1
            return False

    def process_one(self, timeout=0.0):
        """Take one queued candidate and write its row. Returns True when one was handled."""
        try:
            candidate, screen, entered, reason, candidate_id, screen_at = self.queue.get(timeout=timeout) if timeout else self.queue.get_nowait()
        except queue.Empty:
            return False
        try:
            extra, extra_missing, why, calls = None, None, None, 0
            f = _get(screen, 'features', {}) or {}
            if not hazard_free(screen):
                why = 'NOT_ENRICHED_HAZARD_REJECT'       # a hazard reject keeps its cheap row; no spending on it
            elif f.get('supply_raw') is None:
                why = 'MINT_STAGE_NOT_REACHED'
            else:
                creator, _ = dev_wallet(screen)
                need_holders = _screen_rows(screen) is None
                age = _num(f.get('age_seconds'))
                screen_time = None if age is None or not isinstance(_get(candidate, 'migrated_at'), (int, float)) else float(candidate.migrated_at) + float(age)
                extra, extra_missing, calls = enrich(candidate, screen, self.helius, creator=creator, need_holders=need_holders,
                                                     screen_time=screen_time)
                self.stats['calls'] += calls
                self.stats['enriched'] += 1
                if extra_missing and LANE_SHED in extra_missing.values():
                    self.stats['shed'] += 1
            self._write(candidate, candidate_id, screen, entered, reason, screen_at, extra=extra, extra_missing=extra_missing,
                        default_why=why, calls=calls)
        except Exception as error:                       # noqa: BLE001
            self._count_error(error)
            try:
                self._write(candidate, candidate_id, screen, entered, reason, screen_at, default_why='ENRICHMENT_FAILED')
            except Exception as again:                   # noqa: BLE001
                self._count_error(again)
                self.stats['dropped'] += 1
        return True

    def run(self, stop, tick=0.5):
        while not stop.is_set():
            if not self.process_one(timeout=tick):
                continue
        while self.process_one():                         # drain on shutdown: every accepted candidate gets its row
            pass

    def health(self):
        return {'enabled': True, 'queued': self.queue.qsize(), 'last_error': self.last_error, **self.stats}


def build(cfg, *, store, keys, code_version, strategy_version, clock=time.time, transport_kwargs=None):
    """The recorder of a validated ``features`` config, on the shared LOW lane; None when disabled."""
    if not cfg['enabled']:
        return None
    from lean import providers
    helius = providers.low(keys, **dict(transport_kwargs or {})).helius
    return FeatureRecorder(store=store, helius=helius, code_version=code_version, strategy_version=strategy_version, clock=clock,
                           queue_size=cfg['queue_size'], enrich_enabled=cfg['enrich'])


# ------------------------------------------------------------------------------------------------ feature vs outcome
def feature_rows(store):
    """Stored ``features`` rows as dicts (payload JSON parsed), oldest first."""
    out = []
    for row in store.rows('observations', kind='features', limit=100000):
        try:
            out.append(json.loads(row['raw']))
        except ValueError:
            continue
    return out


def outcomes(store):
    """``{mint: realized lamports}`` of the positions that are CLOSED (the last closed lifecycle's total trade pnl)."""
    return {c['mint']: int(c['state']['trade_pnl_lamports']) for c in store.closed_positions() if c['state'].get('trade_pnl_lamports') is not None}


def _numeric(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    return _num(value)


def quantile_groups(points, buckets):
    """Split ``points`` (sorted by value) into at most ``buckets`` rank-based groups WITHOUT splitting equal values: a cut that falls
    inside a run of ties moves to the end of the run, so the same value is never in two buckets (fewer buckets may result)."""
    n = len(points)
    k = max(1, min(buckets, n))
    cuts = [0]
    for i in range(1, k):
        cut = max(i * n // k, cuts[-1])
        while 0 < cut < n and points[cut][0] == points[cut - 1][0]:
            cut += 1
        cuts.append(cut)
    cuts.append(n)
    return [points[a:b] for a, b in zip(cuts, cuts[1:]) if b > a]


def feature_outcome_table(rows, realized, *, buckets=4, min_total=30, min_bucket=10):
    """Per numeric feature: quantile buckets x (n, win rate, mean pnl in lamports). Entered and closed candidates only.

    ``insufficient`` is True for a feature with fewer than ``min_total`` non-null samples and for each bucket with fewer than
    ``min_bucket`` (the numbers are then shown but must not be read as a finding). Equal values always share a bucket."""
    samples = [(r['fields'], realized[r['mint']]) for r in rows if r.get('entered') and r.get('mint') in realized]
    table = {}
    names = sorted({k for fields, _ in samples for k, v in fields.items() if _numeric(v) is not None and k != 'sol_usd'})
    for name in names:
        pts = sorted(((_numeric(f.get(name)), pnl) for f, pnl in samples if _numeric(f.get(name)) is not None), key=lambda p: p[0])
        n = len(pts)
        groups = quantile_groups(pts, buckets)
        table[name] = {'n': n, 'insufficient': n < min_total, 'buckets': [
            {'lo': str(g[0][0]), 'hi': str(g[-1][0]), 'n': len(g), 'win_rate': round(sum(1 for _, p in g if p > 0) / len(g), 4),
             'mean_pnl_lamports': sum(p for _, p in g) // len(g), 'insufficient': len(g) < min_bucket} for g in groups]}
    return table
