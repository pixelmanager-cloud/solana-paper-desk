"""Entry feature capture (L12): one ``observations(kind='features')`` row per screened candidate, entered or not.

Paper only, read-only. The row is the canonical JSON of ``{features_version, mint, entered, not_entered_reason, fields, missing}``:
``fields[name]`` is the value or ``None``; every ``None`` has a reason in ``missing[name]``. A missing field is never an error.

Cheap first. Everything the screen already loaded costs nothing (market cap, liquidity, SOL/USD, age, top-10 holder share, token
program, mint/freeze authority state from the screen verdict, the Token-2022 extension list from the retained mint account bytes).
At most ``MAX_EXTRA_CALLS`` (3) extra Helius calls per candidate, on a LOW-PRIORITY lane (own non-sleeping token bucket, default
2 req/s; a candidate that finds the bucket empty is recorded with reason ``LOW_PRIORITY_SHED``, never queued behind the trader):

1. ``getSignaturesForAddress(mint, limit=1000)``  -> creation time (oldest signature, only when the history is complete),
   seconds creation -> graduation, successful transaction count in the first 10 minutes.
2. ``getTransaction(oldest signature)``           -> the fee payer of the mint's first transaction (``creator_wallet``: the
   fee payer, not a proven creator).
3. ``getTokenAccountsByOwner(creator, mint)``     -> ``dev_holding_pct`` of the supply.

Not available inside that budget, recorded as missing with the reason: holder count (needs a full holder scan), buys / sells /
unique buyers / buy volume (needs parsed transactions; L13 owns that), metadata socials (needs the DAS ``getAsset`` or the off-chain
JSON, neither is allowed by ``lean.providers``). The top-10 share excludes the pool vault only (the screen's definition), not the burn address.

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

FEATURES_VERSION = 1
KIND = 'features'
MAX_EXTRA_CALLS = 3
EARLY_WINDOW_S = 600
SIGNATURE_LIMIT = 1000
TOKEN_2022_EXTENSIONS = {1: 'TransferFeeConfig', 2: 'TransferFeeAmount', 3: 'MintCloseAuthority', 4: 'ConfidentialTransferMint',
                         5: 'ConfidentialTransferAccount', 6: 'DefaultAccountState', 7: 'ImmutableOwner', 8: 'MemoTransfer',
                         9: 'NonTransferable', 10: 'InterestBearingConfig', 11: 'CpiGuard', 12: 'PermanentDelegate',
                         13: 'NonTransferableAccount', 14: 'TransferHook', 15: 'TransferHookAccount', 16: 'ConfidentialTransferFeeConfig',
                         17: 'ConfidentialTransferFeeAmount', 18: 'MetadataPointer', 19: 'TokenMetadata', 20: 'GroupPointer',
                         21: 'TokenGroup', 22: 'GroupMemberPointer', 23: 'TokenGroupMember'}
FIELDS = ('holder_count', 'top10_pct_excl_pool', 'top1_pct_excl_pool', 'creator_wallet', 'dev_holding_pct', 'seconds_creation_to_graduation',
          'tx_count_first_10m', 'buys_first_10m', 'sells_first_10m', 'unique_buyers_first_10m', 'buy_volume_sol_first_10m',
          'market_cap_usd', 'liquidity_usd', 'price_sol_per_token', 'age_seconds_at_screen', 'has_twitter', 'has_telegram', 'has_website',
          'mint_authority_active', 'freeze_authority_active', 'token_program', 'token2022_extensions', 'transfer_fee_bps', 'sol_usd')
NEEDS_PARSED = 'NEEDS_PARSED_TRANSACTIONS_OVER_BUDGET'
NEEDS_METADATA = 'METADATA_RPC_NOT_ALLOWED'


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


def _mint_bytes(screen):
    """The mint account bytes the screen retained (label ``accounts_mint_pool``), or None."""
    for label, body in _get(screen, 'raw', ()) or ():
        if label != 'accounts_mint_pool':
            continue
        try:
            value = json.loads(body)['result']['value'][0]
            return base64.b64decode(value['data'][0], validate=True)
        except (ValueError, KeyError, IndexError, TypeError):
            return None
    return None


# ------------------------------------------------------------------------------------------------ the cheap part
def base_fields(candidate, screen):
    """``(fields, missing)`` from what the screen already loaded. Pure."""
    f = _get(screen, 'features', {}) or {}
    reasons = [str(r) for r in (_get(screen, 'reasons', ()) or ())]
    fields, missing = {name: None for name in FIELDS}, {}

    def put(name, value, why):
        if value is None:
            missing[name] = why
        else:
            fields[name] = value
    for name, key in (('market_cap_usd', 'market_cap_usd'), ('liquidity_usd', 'liquidity_usd'), ('price_sol_per_token', 'price_sol_per_token'),
                      ('sol_usd', 'sol_usd'), ('top10_pct_excl_pool', 'top10_pct_excluding_pool'), ('top1_pct_excl_pool', 'top1_pct_excluding_pool')):
        value = _num(f.get(key))
        if name.startswith('top'):
            why = 'HOLDER_CHECK_' + str(f['holder_check']) if f.get('holder_check') else 'SCREEN_STOPPED_BEFORE_HOLDERS'
        else:
            why = 'SCREEN_STOPPED_BEFORE_MARKET'
        put(name, None if value is None else format(value, 'f'), why)
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
    missing['holder_count'] = 'REQUIRES_FULL_HOLDER_SCAN'
    for name in ('buys_first_10m', 'sells_first_10m', 'unique_buyers_first_10m', 'buy_volume_sol_first_10m'):
        missing[name] = NEEDS_PARSED
    for name in ('has_twitter', 'has_telegram', 'has_website'):
        missing[name] = NEEDS_METADATA
    return fields, missing


# ------------------------------------------------------------------------------------------------ the extra calls
class LowPriorityBucket:
    """Non-sleeping token bucket: ``take(n)`` is True when ``n`` requests may go now (all or nothing). Thread safe."""

    def __init__(self, rate_per_second=2.0, burst=3, *, clock=time.monotonic):
        if not isinstance(rate_per_second, (int, float)) or not 0 < rate_per_second <= 1000:
            raise ValueError('rate out of range')
        self.rate, self.burst, self._clock = float(rate_per_second), float(max(burst, MAX_EXTRA_CALLS)), clock
        self._tokens, self._stamp, self._lock = self.burst, clock(), threading.Lock()

    def take(self, n=1):
        with self._lock:
            now = self._clock()
            self._tokens = min(self.burst, self._tokens + (now - self._stamp) * self.rate)
            self._stamp = now
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False


def _sol_fields(missing, names, why):
    for name in names:
        missing[name] = why


def enrich(candidate, helius, supply_raw, *, migrated_at=None):
    """Up to three read-only calls. Returns ``(fields, missing, calls)``; never raises (provider errors become reasons)."""
    fields, missing, calls = {}, {}, 0
    mint = _get(candidate, 'mint')
    migrated_at = _get(candidate, 'migrated_at') if migrated_at is None else migrated_at
    group = ('creator_wallet', 'dev_holding_pct', 'seconds_creation_to_graduation', 'tx_count_first_10m')

    def fail(names, error):
        _sol_fields(missing, [n for n in names if n not in fields], 'PROVIDER_' + str(getattr(error, 'code', type(error).__name__))[:40])
    try:
        calls += 1
        sigs, _raw, _meta = helius.rpc('getSignaturesForAddress', [mint, {'limit': SIGNATURE_LIMIT, 'commitment': 'confirmed'}])
    except Exception as error:                           # noqa: BLE001 - a provider failure is a missing field
        fail(group, error)
        return fields, missing, calls
    if not isinstance(sigs, list) or not sigs or not all(isinstance(s, dict) for s in sigs):
        _sol_fields(missing, group, 'SIGNATURES_UNAVAILABLE')
        return fields, missing, calls
    if len(sigs) >= SIGNATURE_LIMIT:
        _sol_fields(missing, group, 'HISTORY_TRUNCATED')
        return fields, missing, calls
    oldest = min(sigs, key=lambda s: s.get('slot') if type(s.get('slot')) is int else 2 ** 63)
    created = oldest.get('blockTime')
    if type(created) is not int or isinstance(created, bool):
        _sol_fields(missing, ('seconds_creation_to_graduation', 'tx_count_first_10m'), 'CREATION_TIME_UNKNOWN')
    else:
        if type(migrated_at) in (int, float) and migrated_at >= created:
            fields['seconds_creation_to_graduation'] = round(float(migrated_at) - created, 3)
        else:
            missing['seconds_creation_to_graduation'] = 'GRADUATION_TIME_UNKNOWN'
        fields['tx_count_first_10m'] = sum(1 for s in sigs if s.get('err') is None and type(s.get('blockTime')) is int
                                           and 0 <= s['blockTime'] - created <= EARLY_WINDOW_S)
    first = oldest.get('signature')
    if not isinstance(first, str):
        _sol_fields(missing, ('creator_wallet', 'dev_holding_pct'), 'CREATION_SIGNATURE_UNKNOWN')
        return fields, missing, calls
    try:
        calls += 1
        tx, _raw, _meta = helius.rpc('getTransaction', [first, {'encoding': 'jsonParsed', 'maxSupportedTransactionVersion': 0,
                                                                'commitment': 'confirmed'}])
        keys = tx['transaction']['message']['accountKeys']
        payer = keys[0]['pubkey'] if isinstance(keys[0], dict) else keys[0]
        if not isinstance(payer, str) or not payer:
            raise ValueError('payer')
    except Exception as error:                           # noqa: BLE001
        if isinstance(error, (KeyError, IndexError, TypeError, ValueError)):
            _sol_fields(missing, ('creator_wallet', 'dev_holding_pct'), 'CREATION_TX_UNREADABLE')
        else:
            fail(('creator_wallet', 'dev_holding_pct'), error)
        return fields, missing, calls
    fields['creator_wallet'] = payer
    try:
        calls += 1
        owned, _raw, _meta = helius.rpc('getTokenAccountsByOwner', [payer, {'mint': mint}, {'encoding': 'jsonParsed', 'commitment': 'confirmed'}])
        total = sum(int(a['account']['data']['parsed']['info']['tokenAmount']['amount']) for a in owned['value'])
        supply = int(supply_raw)
        if supply <= 0 or total < 0 or total > supply:
            raise ValueError('amount')
        with localcontext() as ctx:
            ctx.prec = 40
            fields['dev_holding_pct'] = format(round(Decimal(total) * 100 / Decimal(supply), 4), 'f')
    except Exception as error:                           # noqa: BLE001
        if isinstance(error, (KeyError, IndexError, TypeError, ValueError)):
            missing['dev_holding_pct'] = 'DEV_BALANCE_UNREADABLE'
        else:
            fail(('dev_holding_pct',), error)
    return fields, missing, calls


# ------------------------------------------------------------------------------------------------ the row
def build_row(candidate, screen, *, entered, reason=None, extra=None, extra_missing=None, default_why=None):
    """The features row payload. ``extra`` / ``extra_missing`` come from ``enrich``; fields it did not cover get ``default_why``."""
    fields, missing = base_fields(candidate, screen)
    covered = ('creator_wallet', 'dev_holding_pct', 'seconds_creation_to_graduation', 'tx_count_first_10m')
    for name, value in (extra or {}).items():
        fields[name] = value
        missing.pop(name, None)
    for name, why in (extra_missing or {}).items():
        missing[name] = why
    for name in covered:
        if fields[name] is None and name not in missing:
            missing[name] = default_why or 'NOT_ENRICHED'
    for name in FIELDS:                                  # the invariant of the schema: None <=> a reason
        if fields[name] is None and name not in missing:
            missing[name] = 'UNKNOWN'
        if fields[name] is not None:
            missing.pop(name, None)
    return {'features_version': FEATURES_VERSION, 'mint': _get(candidate, 'mint'), 'entered': bool(entered),
            'not_entered_reason': None if entered else reason, 'screen_passed': bool(_get(screen, 'passed')), 'fields': fields, 'missing': missing}


class FeatureRecorder:
    """Writes one ``features`` row per candidate. ``submit`` is non-blocking and never raises."""

    def __init__(self, *, store, helius, code_version, strategy_version, bucket=None, clock=time.time, queue_size=500, enrich_enabled=True):
        self.store, self.helius, self.clock = store, helius, clock
        self.code_version, self.strategy_version = code_version, strategy_version
        self.bucket = bucket or LowPriorityBucket()
        self.enrich_enabled = enrich_enabled
        self.queue = queue.Queue(maxsize=queue_size)
        self.stats = {'rows': 0, 'enriched': 0, 'shed': 0, 'queue_full': 0, 'errors': 0, 'calls': 0, 'dropped': 0}
        self.last_error = None
        self._written = set()
        self._lock = threading.Lock()

    def _count_error(self, error):
        self.stats['errors'] += 1
        self.last_error = type(error).__name__

    def _write(self, candidate, candidate_id, screen, entered, reason, extra=None, extra_missing=None, default_why=None):
        payload = build_row(candidate, screen, entered=entered, reason=reason, extra=extra, extra_missing=extra_missing, default_why=default_why)
        mint = payload['mint']
        self.store.add_observation(KIND, canonical(payload), mint=mint, candidate_id=candidate_id,
                                   meta={'features_version': FEATURES_VERSION, 'entered': bool(entered), 'missing': len(payload['missing'])},
                                   code_version=self.code_version, strategy_version=self.strategy_version, ts=self.clock())
        self.stats['rows'] += 1

    def submit(self, candidate, screen, *, entered, reason=None, candidate_id=None):
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
                self._write(candidate, candidate_id, screen, entered, reason, default_why='ENRICHMENT_DISABLED')
                return True
            try:
                self.queue.put_nowait((candidate, screen, entered, reason, candidate_id))
            except queue.Full:
                self.stats['queue_full'] += 1
                self._write(candidate, candidate_id, screen, entered, reason, default_why='QUEUE_FULL')
            return True
        except Exception as error:                       # noqa: BLE001 - a recorder failure never reaches the trader
            self._count_error(error)
            self.stats['dropped'] += 1
            return False

    def process_one(self, timeout=0.0):
        """Take one queued candidate and write its row. Returns True when one was handled."""
        try:
            candidate, screen, entered, reason, candidate_id = self.queue.get(timeout=timeout) if timeout else self.queue.get_nowait()
        except queue.Empty:
            return False
        try:
            extra, extra_missing, why = None, None, None
            supply = (_get(screen, 'features', {}) or {}).get('supply_raw')
            if not _get(screen, 'passed') and not _hazard_free(screen):
                why = 'NOT_ENRICHED_HAZARD_REJECT'       # a hazard reject keeps its cheap row; no spending on it
            elif supply is None:
                why = 'MINT_STAGE_NOT_REACHED'
            elif not self.bucket.take(MAX_EXTRA_CALLS):
                self.stats['shed'] += 1
                why = 'LOW_PRIORITY_SHED'
            else:
                extra, extra_missing, calls = enrich(candidate, self.helius, supply)
                self.stats['calls'] += calls
                self.stats['enriched'] += 1
            self._write(candidate, candidate_id, screen, entered, reason, extra, extra_missing, why)
        except Exception as error:                       # noqa: BLE001
            self._count_error(error)
            try:
                self._write(candidate, candidate_id, screen, entered, reason, default_why='ENRICHMENT_FAILED')
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
        return {'queued': self.queue.qsize(), 'last_error': self.last_error, **self.stats}


NON_HAZARD_REASONS = frozenset({'MARKET_CAP_BELOW_MIN', 'MARKET_CAP_ABOVE_MAX', 'LIQUIDITY_BELOW_MIN'})


def _hazard_free(screen):
    reasons = list(_get(screen, 'reasons', ()) or ())
    return bool(reasons) and all(r in NON_HAZARD_REASONS for r in reasons)


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
    """``{mint: realized lamports}`` of the positions that are CLOSED now (sum of the sell fills' realized pnl)."""
    held = set(store.positions())
    realized = {}
    for fill in store.rows('fills', limit=100000):
        if fill['side'] == 'sell' and fill['mint'] not in held:
            realized[fill['mint']] = realized.get(fill['mint'], 0) + fill['realized_lamports']
    return realized


def _numeric(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    return _num(value)


def feature_outcome_table(rows, realized, *, buckets=4, min_total=30, min_bucket=10):
    """Per numeric feature: quantile buckets x (n, win rate, mean pnl in lamports). Entered and closed candidates only.

    ``insufficient`` is True for a feature with fewer than ``min_total`` non-null samples and for each bucket with fewer than
    ``min_bucket`` (the numbers are then shown but must not be read as a finding). Bucket edges are rank based, so equal values
    can straddle buckets only at the cut; ties go to the lower bucket."""
    samples = [(r['fields'], realized[r['mint']]) for r in rows if r.get('entered') and r.get('mint') in realized]
    table = {}
    names = sorted({k for fields, _ in samples for k, v in fields.items() if _numeric(v) is not None and k != 'sol_usd'})
    for name in names:
        pts = sorted(((_numeric(f.get(name)), pnl) for f, pnl in samples if _numeric(f.get(name)) is not None), key=lambda p: p[0])
        n = len(pts)
        k = max(1, min(buckets, n))
        groups = [pts[i * n // k:(i + 1) * n // k] for i in range(k)]
        table[name] = {'n': n, 'insufficient': n < min_total, 'buckets': [
            {'lo': str(g[0][0]), 'hi': str(g[-1][0]), 'n': len(g), 'win_rate': round(sum(1 for _, p in g if p > 0) / len(g), 4),
             'mean_pnl_lamports': sum(p for _, p in g) // len(g), 'insufficient': len(g) < min_bucket} for g in groups if g]}
    return table
