"""M1 #7: the PumpSwap mark model (lean.json ``marks``). PAPER ONLY, pure functions plus one small cache, no I/O.

The live position loop values a held position as the NET SOL a sell of the whole quantity would get from the pool now
(``lean.adapters.mark``: constant product, pool fee, the fixed paper fee). Two models:

  vault      (default, today's behaviour) the quote reserve is the RAW quote vault amount and the pool fee is lean.json
             ``pool_fee_bps``. On 712 live sells the Jupiter decision quote was 1.138x this mark (median): PumpSwap prices
             with VIRTUAL quote reserves on top of the vault, so the vault model under-values every position by ~12% and
             the -18% stop fired near -9%.
  effective  the quote reserve is what the program prices with:
                 effective = quote vault + virtual_quote_reserves
             decoded from the PumpSwap pool account (desk.pools.parse_pool, read-only; desk.pools prices the same way).
             The accrued protocol/creator fees in the pool are NOT subtracted: on the 486 live entries, whose screen read
             the pool and vaults seconds before the Jupiter quote, vault + virtual reproduces Jupiter within 1e-5 on 170
             quotes against 87 for vault - fees + virtual, and a pool with 3.76 SOL accrued is matched to 3e-6 by the
             former and 4% off by the latter. The accrued fees are recorded with every mark. The fee is the program's own
             schedule: the Pump fee program's FeeConfig account (desk.dynamic_fees.parse_fee_config) selects a market-cap
             tier (market cap = effective * supply / base reserve, as desk.dynamic_fees.standard_sol_fees) whose
             lp/protocol/creator bps are each charged rounded up on the gross output (pump-swap-sdk sellBaseInput, as
             desk.sell_fees.standard_sell_amounts). When the schedule is not decodable (missing account, unknown
             allocation, stale) the fee falls back to lean.json ``pool_fee_bps`` (one component), recorded as
             ``fee_source: config``.

Verified exactly on the captured mainnet SellEvent (fixtures/mainnet-sell-event.json): (pool_quote_token_reserves +
virtual_quote_reserves) * in // (base + in) is the event's quote_amount_out to the lamport and the tier-0 fees reproduce
user_quote_amount_out. Calibrated against Jupiter on the live run: reports/M1.md.

The marks call reads the pool account next to the two vaults (one more account in the SAME batched getMultipleAccounts,
no extra request) and the FeeConfig account once per call. When the pool account is missing or unreadable the pool fields
captured at the screen (position state ``pool_info``) are used instead, recorded as ``pool_source: screen``.
"""
import threading

from lean import adapters as A
from lean.adapters import AdapterError

MODELS = ('vault', 'effective')
FEE_SOURCES = ('dynamic', 'config')
DEFAULTS = {'model': 'vault', 'fee_source': 'dynamic', 'schedule_ttl_s': 3600}
MAX_BPS = 10_000
DEFAULT_PUBKEY = '11111111111111111111111111111111'
FEE_COMPONENTS = ('lp_fee_bps', 'protocol_fee_bps', 'creator_fee_bps')


class MarkError(AdapterError):
    """A pool / fee account that cannot be turned into a mark (per position; never a halt)."""


class MarkConfigError(ValueError):
    pass


def config(raw):
    """lean.json ``marks`` -> validated settings. Absent / null = ``{"model": "vault"}`` (today's marks). Unknown keys refused."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise MarkConfigError('marks must be an object')
    unknown = set(raw) - set(DEFAULTS) - {'_comment'}
    if unknown:
        raise MarkConfigError('marks: unknown keys %s' % sorted(unknown))
    out = {k: raw.get(k, v) for k, v in DEFAULTS.items()}
    if out['model'] not in MODELS:
        raise MarkConfigError('marks.model must be one of %s' % (MODELS,))
    if out['fee_source'] not in FEE_SOURCES:
        raise MarkConfigError('marks.fee_source must be one of %s' % (FEE_SOURCES,))
    ttl = out['schedule_ttl_s']
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 1 <= ttl <= 86400:
        raise MarkConfigError('marks.schedule_ttl_s must be between 1 and 86400')
    out['schedule_ttl_s'] = float(ttl)
    return out


_FEE_ADDRESS = []


def fee_config_address():
    """The Pump fee program's FeeConfig PDA (the PumpSwap sell instruction's ``fee_config`` account)."""
    if not _FEE_ADDRESS:
        from desk.dynamic_fees import fee_address
        _FEE_ADDRESS.append(str(fee_address()[0]))
    return _FEE_ADDRESS[0]


# --------------------------------------------------------------------------------------------------------- decoding
def pool_fields(account):
    """The pricing fields of a PumpSwap pool account (RPC base64 shape): ints, plus the coin creator."""
    if not isinstance(account, dict):
        raise MarkError('pool account missing')
    data = account.get('data')
    key = (account.get('owner'), account.get('executable'), data[0] if isinstance(data, list) and data and isinstance(data[0], str) else None)
    if key[2] is not None:
        with _POOL_CACHE_LOCK:
            cached = _POOL_CACHE.get(key)
        if cached is not None:
            return dict(cached)
    from desk.pools import parse_pool
    try:
        fields = parse_pool(account)
    except (ValueError, KeyError, TypeError, IndexError, StopIteration):
        raise MarkError('pool account malformed') from None
    out = _pool_out(fields)
    if key[2] is not None:                 # parse_pool re-reads and re-hashes its pinned schema on every call: memoize by bytes
        with _POOL_CACHE_LOCK:
            if len(_POOL_CACHE) >= POOL_CACHE_SIZE:
                _POOL_CACHE.pop(next(iter(_POOL_CACHE)))
            _POOL_CACHE[key] = out
    return dict(out)


POOL_CACHE_SIZE = 4096
_POOL_CACHE = {}
_POOL_CACHE_LOCK = threading.Lock()


def _pool_out(fields):
    return {'virtual_quote_reserves_raw': int(fields['virtual_quote_reserves']), 'protocol_fees_raw': int(fields['protocol_fees']),
            'creator_fees_raw': int(fields['creator_fees']), 'coin_creator': str(fields['coin_creator']),
            'creator_fee_bps': int(fields.get('creator_fee_bps') or 0)}


def screen_pool_info(features):
    """The pool fields the screen decoded (candidates._market), kept in the open position state as ``pool_info``: the
    fallback when a mark cannot read the pool account, and the supply that sets the fee tier. None when absent."""
    try:
        gross, spendable = int(features['quote_gross_raw']), int(features['quote_spendable_raw'])
        return {'virtual_quote_reserves_raw': int(features.get('virtual_quote_reserves_raw') or 0),
                'accrued_fees_raw': gross - spendable, 'supply_raw': int(features['supply_raw'])}
    except (KeyError, TypeError, ValueError):
        return None


def effective_quote(vault_quote, virtual_raw):
    """The quote reserve PumpSwap prices with: the quote vault + virtual_quote_reserves (the accrued fees are NOT taken out:
    see the module docstring for the evidence)."""
    quote = int(vault_quote) + int(virtual_raw)
    if not 0 < quote < 2 ** 64:
        raise MarkError('effective quote reserve invalid')
    return quote


def decode_schedule(account):
    """The FeeConfig fields when the account decodes completely (desk.dynamic_fees.parse_fee_config), else None."""
    if not isinstance(account, dict):
        return None
    from desk.dynamic_fees import parse_fee_config
    try:
        parsed = parse_fee_config(account)
    except (ValueError, KeyError, TypeError, IndexError, StopIteration):
        return None
    fields = parsed.get('fields') or {}
    if parsed.get('configuration_complete') is not True or not fields.get('fee_tiers'):
        return None
    return fields


def tier_fees(schedule, quote_effective, base_reserve, supply):
    """(fees {lp, protocol, creator bps}, market cap lamports) of a canonical pool: the highest tier whose threshold is <= the
    market cap (desk.dynamic_fees.standard_sol_fees)."""
    if supply <= 0 or base_reserve <= 0:
        raise MarkError('supply / base reserve required for the fee tier')
    cap = quote_effective * supply // base_reserve
    tiers = schedule['fee_tiers']
    selected = tiers[0]
    for tier in tiers:
        if tier['market_cap_lamports_threshold'] <= cap:
            selected = tier
        else:
            break
    return {k: int(selected['fees'][k]) for k in FEE_COMPONENTS}, cap


def sell_output(qty, base, quote, fees, *, has_creator=True):
    """Lamports the user receives for selling ``qty`` raw tokens (pump-swap-sdk sellBaseInput): each fee component rounded
    up on the gross output; no creator fee without a coin creator. Never negative."""
    if qty <= 0 or base <= 0 or quote <= 0:
        raise MarkError('empty pool or position')
    gross = quote * qty // (base + qty)
    charged = sum((gross * int(bps) + MAX_BPS - 1) // MAX_BPS for name, bps in fees.items()
                  if has_creator or name != 'creator_fee_bps')
    return max(0, gross - charged)


def buy_output(spend, base, quote, fee_bps):
    """Raw tokens ``spend`` lamports buy (pump-swap-sdk buyQuoteInput): the fees come off the input."""
    if spend <= 0 or base <= 0 or quote <= 0:
        raise MarkError('empty pool or spend')
    net = spend * MAX_BPS // (MAX_BPS + int(fee_bps))
    return base * net // (quote + net)


def config_fees(pool_fee_bps):
    return {'lp_fee_bps': int(pool_fee_bps), 'protocol_fee_bps': 0, 'creator_fee_bps': 0}


# ------------------------------------------------------------------------------------------------------------ model
class MarkModel:
    """The runner's / path recorder's mark: which accounts to read, and their value. Thread-safe (one small cache)."""

    def __init__(self, cfg=None, *, pool_fee_bps=25):
        self.cfg = config(cfg)
        self.model = self.cfg['model']
        self.pool_fee_bps = int(pool_fee_bps)
        self._lock = threading.Lock()
        self._schedule = None                    # (fields, decoded_at)
        self._schedule_raw = None                # (owner, base64 data) the schedule was decoded from
        self.stats = {'schedule_reads': 0, 'schedule_undecodable': 0, 'fee_dynamic': 0, 'fee_config': 0, 'pool_screen': 0}

    @property
    def effective(self):
        return self.model == 'effective'

    @property
    def keys_per_position(self):
        return 3 if self.effective else 2

    def position_keys(self, base_vault, quote_vault, pool):
        return [base_vault, quote_vault, pool] if self.effective else [base_vault, quote_vault]

    def call_keys(self):
        """Accounts read once per batched call (the fee schedule)."""
        return [fee_config_address()] if self.effective and self.cfg['fee_source'] == 'dynamic' else []

    def observe_call(self, accounts, at):
        """The once-per-call accounts of a marks call (in ``call_keys`` order)."""
        if not accounts:
            return
        account = accounts[0]
        data = account.get('data') if isinstance(account, dict) else None
        raw = data[0] if isinstance(data, list) and data and isinstance(data[0], str) else None
        with self._lock:
            same = raw is not None and self._schedule is not None and self._schedule_raw == (account.get('owner'), raw)
            if same:                                         # unchanged bytes: the decoded schedule, refreshed
                self.stats['schedule_reads'] += 1
                self._schedule = (self._schedule[0], float(at))
                return
        fields = decode_schedule(account)
        with self._lock:
            self._schedule_raw = (account.get('owner'), raw) if fields is not None else None
        with self._lock:
            self.stats['schedule_reads'] += 1
            if fields is None:
                self.stats['schedule_undecodable'] += 1
            else:
                self._schedule = (fields, float(at))

    def schedule(self, now):
        with self._lock:
            held = self._schedule
        if held is None or not 0 <= float(now) - held[1] <= self.cfg['schedule_ttl_s']:
            return None
        return held[0]

    def health(self):
        with self._lock:
            out = {'model': self.model, 'fee_source': self.cfg['fee_source'], 'pool_fee_bps': self.pool_fee_bps, **self.stats,
                   'schedule_at': None if self._schedule is None else self._schedule[1]}
        return out

    def _bump(self, name):
        with self._lock:
            self.stats[name] += 1

    def value(self, qty, base, quote, *, pool=None, pool_info=None, now, fee_lamports):
        """(net lamports, info) of selling ``qty`` with the vault amounts ``base`` / ``quote`` (ints). ``pool`` is the decoded
        live pool (``pool_fields``) or None; ``pool_info`` the screen snapshot (``screen_pool_info``) or None."""
        if not self.effective:
            if base <= 0 or quote <= 0 or qty <= 0:
                raise MarkError('empty pool or position')
            gross = quote * qty // (base + qty)
            out = gross * (MAX_BPS - self.pool_fee_bps) // MAX_BPS
            return max(0, out - fee_lamports), {'model': 'vault', 'base_raw': base, 'quote_raw': quote, 'fee_bps': self.pool_fee_bps}
        info = pool_info or {}
        if pool is not None:
            virtual = pool['virtual_quote_reserves_raw']
            accrued = pool['protocol_fees_raw'] + pool['creator_fees_raw']
            has_creator = pool.get('coin_creator') not in (None, DEFAULT_PUBKEY)
            creator_override = int(pool.get('creator_fee_bps') or 0)
            pool_source = 'live'
        elif pool_info:
            virtual, accrued = int(info['virtual_quote_reserves_raw']), int(info.get('accrued_fees_raw') or 0)
            has_creator, creator_override, pool_source = True, int(info.get('creator_fee_bps') or 0), 'screen'
            self._bump('pool_screen')
        else:
            raise MarkError('pool fields unavailable')
        quote_eff = effective_quote(quote, virtual)
        cap, fee_source = None, 'config'
        schedule = self.schedule(now)
        supply = int(info.get('supply_raw') or 0)
        if schedule is not None and supply > 0:
            fees, cap = tier_fees(schedule, quote_eff, base, supply)
            fee_source = 'dynamic'
            if creator_override:                 # a per-coin creator fee (newer IDLs) replaces the tier's creator component
                fees = dict(fees, creator_fee_bps=creator_override)
        else:
            fees = config_fees(self.pool_fee_bps)          # FALLBACK (flagged): the schedule did not decode, or no supply
        self._bump('fee_' + fee_source)
        out = sell_output(qty, base, quote_eff, fees, has_creator=has_creator)
        charged = {k: (v if has_creator or k != 'creator_fee_bps' else 0) for k, v in fees.items()}
        return max(0, out - fee_lamports), {'model': 'effective', 'base_raw': base, 'quote_raw': quote, 'virtual_raw': virtual,
                                            'accrued_fees_raw': accrued, 'quote_effective_raw': quote_eff,
                                            'fee_bps': sum(charged.values()), 'fees_bps': charged, 'fee_source': fee_source,
                                            'fee_fallback': fee_source == 'config', 'pool_source': pool_source,
                                            **({'market_cap_lamports': cap} if cap is not None else {})}

    def value_accounts(self, qty, accounts, *, pool_info=None, now, fee_lamports):
        """``value`` from the raw accounts of one position, in ``position_keys`` order. AdapterError when unusable."""
        base, quote = A.vault_amount(accounts[0]), A.vault_amount(accounts[1])
        pool = None
        if self.effective and len(accounts) > 2 and accounts[2] is not None:
            try:
                pool = pool_fields(accounts[2])
            except MarkError:
                pool = None                     # an unreadable pool account: the screen snapshot instead (recorded)
        return self.value(qty, base, quote, pool=pool, pool_info=pool_info, now=now, fee_lamports=fee_lamports)
