"""L13: bundle / sniper detection and a paper-observed smart-wallet signal. FEATURES ONLY: nothing here is read by
``lean.strategy`` and nothing changes an entry decision. PAPER ONLY: read-only RPC methods, no signing, no sending.

What is computed for a candidate (the first ``window_s`` seconds of swaps after its graduation / pool creation)
  same_slot_buyers   distinct buyers whose first buy is in the pool-creation slot
  sniper_count       distinct buyers whose first buy is within ``sniper_slots`` slots of it
  bundle_score       share of the examined window buyers that are "bundled": a same-slot buyer, or one of >= ``min_cluster``
                     examined buyers whose first funding transfer came from the same source wallet (1 hop)
  bundled_supply_pct last seen balance of the bundled wallets as a percentage of the token supply
  smart_buyers_count window buyers whom the desk's OWN paper record currently rates as smart (see below)

Shared funding is a SIGNAL, not proof of common ownership: the funding source may be an exchange hot wallet or a
faucet-like service. ``ignore_funders`` removes known hubs; the caveat travels with every row. The score is a lower bound:
only the first ``max_swap_txs`` window transactions and the first ``funding_wallets`` buyers are examined.

Smart wallets (``wallet_table``) are built ONLY from this desk's own records: every examined early buyer of a candidate is
written as a ``wallet_buy`` event, and a token's outcome (``wallet_outcome`` event: WIN = reached ``win_multiple`` x its
first observed price before it fell to ``rug_price_frac`` x; RUG = the reverse) comes from the L07R2 path data in
``paths.sqlite`` and its archived ``paths-*.sqlite`` (read-only, through ONE reader: ``read_path``) when it exists,
otherwise from this desk's own closed trade on the token. A wallet is smart with at least ``min_wins`` wins
and a Laplace score (wins + 1) / (wins + rugs + 2) >= ``smart_threshold``. A candidate's ``smart_buyers_count`` only uses
outcomes recorded BEFORE its row is written (the row stores the event id it was computed as of), and the candidate's own
buys are written after it, so a wallet is never rated by the token it is being scored on.

Budget: every provider call goes through the shared LOW lane (``lean.providers.low``, L07R): a call goes out at once or fails with
``LANE_SHED`` and sends nothing, and any 429 on any lane sheds the low lane for a while. A ``no_token`` shed is paced (``pace_s``
sleeps, up to ``shed_wait_s`` per request) because this collector has its own thread; a shed window (429) or lost patience ends
the candidate with what it learned (``unavailable['stopped'] = 'LANE_SHED'``), or retries it later when nothing was learned yet.
A hard per-candidate cap ``max_calls`` counts the requests actually SENT; ``shed()`` (credit shedding, L15) refuses before a request.
A missing field is null with a reason in ``unavailable``, never an error. A failure here never touches entries, exits or the accounting.

LINT2b (pre-deploy fixes):
* No lookahead. Every row records ``decision_ts`` (the candidate's first PASS/REJECT screen decision), ``collected_at`` and
  ``late_s``. The swap window ends at ``min(graduation + window_s, decision_ts)`` by BLOCK time (a window signature without a
  block time is skipped); ``window_cut_by_decision`` / ``window_s_effective`` say how much was left, and
  ``window_after_decision_s`` is how far the examined window runs past the decision (always 0 when the decision is known; null
  when it is not). The supply is the one the screen read (``supply_source``); ``smart_buyers_count`` uses only outcomes
  resolved, and buys recorded, BEFORE ``decision_ts``. The backfill cutoff is ``migrated_at``, not the row time.
* Bundle poisoning. The row stores the buyer -> funder map (``funders``). A funder that funded buyers of at least
  ``hub_min_mints`` distinct mints within ``hub_window_s`` before the decision is a HUB (an exchange or a service, detected,
  never hard-coded) and never forms a cluster; every lookup is persisted as a ``wallet_funding`` event and a newly seen hub as
  a ``wallet_hub`` event. ``bundle_score_basis`` is ``same_slot_only`` unless at least one funding source was found.
* Sharing the low lane: a call is only made while at least ``reserve_tokens`` more tokens would remain for the other low-lane
  users (path recorder, L11 checks, L12F), paced at ``pace_s`` (0.5 s).
* Shutdown: ``stop`` is checked before every request; a stopped collection writes NOTHING (it is collected again later).
* Outcomes: candidates that can never resolve (hazard rejects with no path and no fill) are closed at once as UNKNOWN, and the
  unresolved mints are visited round-robin, so new WIN/RUG outcomes are not stuck behind a 12 h backlog.

Storage: one ``observations(kind='wallet_signals')`` row per candidate (canonical JSON bytes + the same figures in ``meta``),
append-only ``events`` rows of kind ``wallet_buy`` / ``wallet_outcome`` / ``wallet_funding`` / ``wallet_hub``. The store schema is untouched (a lean store is
never migrated), so ``lean_wallets`` is a view over those events, not a table.
"""
import json
import logging
import re
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from lean.providers import LANE_SHED, ProviderError
from lean.store import StoreError

log = logging.getLogger('lean.wallet_signals')

VERSION = 'wallet_signals_v2'
SLOT_SECONDS = 0.4
CAVEAT = 'shared funding source is a signal, not proof of common ownership'
OBSERVATION_KIND = 'wallet_signals'
_BASE58 = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')
_SIGNATURE = re.compile(r'[1-9A-HJ-NP-Za-km-z]{64,90}')
SYSTEM_PROGRAM = '11111111111111111111111111111111'

DEFAULTS = {
    'window_s': 120,               # swaps examined after the graduation slot
    'sniper_slots': 3,             # first buy within this many slots of the pool-creation slot
    'max_swap_txs': 6,             # earliest window transactions fetched (getTransaction each)
    'sig_limit': 1000,             # signatures per page (RPC maximum)
    'sig_pages': 2,                # pages walked back looking for the graduation slot
    'funding_wallets': 3,          # earliest buyers whose funding source is looked up (2 calls each)
    'funding_history': 10,         # prior signatures read per wallet; a full page means the history is cut
    'min_funding_lamports': 1_000_000,
    'min_cluster': 2,
    'ignore_funders': [],          # known hubs (exchanges, airdrop services) that must not form a cluster
    'max_calls': 16,               # HARD cap per candidate: 2 pages + supply + 6 txs + 3 x 2 funding = 15 (+1 spare)
    'pace_s': 0.5,                 # wait between tries when the shared low lane has no token right now (LINT2b: 0.5)
    'reserve_tokens': 1,           # LINT2b: only call while this many MORE tokens would remain for the other low-lane users
    'shed_wait_s': 30.0,           # total patience per request for a token; then the candidate stops (or is retried later)
    'max_per_pass': 2,             # candidates per signals pass
    'pass_interval_s': 5.0,
    'max_retries': 2,              # transient failure before anything was learned
    'retry_delay_s': 60.0,
    'backfill_max_age_s': 21600,   # candidates older than this are never collected (the window may be unreachable)
    'backfill_limit': 50,
    'retain_raw': 'signatures',    # none | signatures | all  (getTransaction bodies are large: off by default)
    'win_multiple': 2.0,
    'rug_price_frac': 0.3,
    'rug_loss_frac': 0.5,          # a closed trade losing at least this fraction of its cost counts as a rug
    'min_wins': 2,
    'smart_threshold': 0.7,
    'resolve_interval_s': 300.0,
    'resolve_batch': 200,
    'outcome_max_age_s': 43200,    # unresolved after this long -> outcome UNKNOWN (never scored)
    'hub_min_mints': 5,            # LINT2b: a funder of buyers of >= this many distinct mints ...
    'hub_window_s': 86400.0,       # ... within this window before the decision is a hub (never a cluster)
}
SOFT_SCREEN_REASONS = frozenset({'MARKET_CAP_BELOW_MIN', 'MARKET_CAP_ABOVE_MAX', 'LIQUIDITY_BELOW_MIN'})


class SignalError(Exception):
    """A malformed or unusable provider body for ONE step; the candidate records the reason and moves on."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ConfigError(ValueError):
    pass


def make_config(overrides=None):
    """Strict: unknown keys, wrong types and impossible numbers are refused (a typo must not fall back to a default)."""
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict):
        raise ConfigError('wallet_signals must be an object')
    unknown = set(overrides) - set(DEFAULTS) - {'_comment'}
    if unknown:
        raise ConfigError('unknown wallet_signals keys: %s' % sorted(unknown))
    cfg = {**DEFAULTS, **{k: v for k, v in overrides.items() if k != '_comment'}}
    for key, default in DEFAULTS.items():
        value = cfg[key]
        if key == 'retain_raw':
            if value not in ('none', 'signatures', 'all'):
                raise ConfigError('retain_raw must be none, signatures or all')
        elif key == 'ignore_funders':
            if not isinstance(value, list) or not all(isinstance(x, str) and _BASE58.fullmatch(x) for x in value):
                raise ConfigError('ignore_funders must be a list of addresses')
        elif isinstance(default, float) or key in ('win_multiple',):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float('inf'), float('-inf')):
                raise ConfigError('%s must be a number' % key)
        elif isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError('%s must be an integer' % key)
    positive = ('window_s', 'max_swap_txs', 'sig_limit', 'sig_pages', 'funding_history', 'min_cluster', 'max_calls', 'max_per_pass',
                'backfill_max_age_s', 'backfill_limit', 'resolve_batch', 'outcome_max_age_s', 'min_wins')
    for key in positive:
        if cfg[key] < 1:
            raise ConfigError('%s must be at least 1' % key)
    if not 1 <= cfg['sig_limit'] <= 1000 or cfg['funding_history'] > 1000 or cfg['max_calls'] > 200:
        raise ConfigError('call sizes out of range')
    if cfg['sniper_slots'] < 0 or cfg['funding_wallets'] < 0 or cfg['max_retries'] < 0 or cfg['min_funding_lamports'] < 0:
        raise ConfigError('negative setting')
    if not 0 < cfg['pace_s'] <= 60 or not 0 <= cfg['shed_wait_s'] <= 600 or cfg['pass_interval_s'] <= 0 or cfg['retry_delay_s'] < 0 \
            or cfg['resolve_interval_s'] <= 0:
        raise ConfigError('pace or interval out of range')
    if not 0 <= cfg['reserve_tokens'] <= 10 or cfg['hub_min_mints'] < 2 or not 0 < cfg['hub_window_s'] <= 30 * 86400:
        raise ConfigError('reserve_tokens, hub_min_mints or hub_window_s out of range')
    if cfg['win_multiple'] <= 1 or not 0 < cfg['rug_price_frac'] < 1 or not 0 < cfg['rug_loss_frac'] <= 1 or not 0 < cfg['smart_threshold'] < 1:
        raise ConfigError('outcome thresholds out of range')
    cfg['ignore_funders'] = list(cfg['ignore_funders'])
    return cfg


# ---------------------------------------------------------------------------------------------------- parsing (pure)

def _is_int(value):
    return type(value) is int


def parse_signatures(result):
    """getSignaturesForAddress result (newest first) -> [{'signature', 'slot', 'block_time', 'failed'}] in the SAME order."""
    if not isinstance(result, list):
        raise SignalError('SIGNATURES_MALFORMED')
    out = []
    for entry in result:
        if not isinstance(entry, dict):
            raise SignalError('SIGNATURES_MALFORMED')
        signature, slot, block_time = entry.get('signature'), entry.get('slot'), entry.get('blockTime')
        if not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature) or not _is_int(slot) or slot < 0 \
                or not (block_time is None or _is_int(block_time)):
            raise SignalError('SIGNATURES_MALFORMED')
        out.append({'signature': signature, 'slot': slot, 'block_time': block_time, 'failed': entry.get('err') is not None})
    return out


def _token_amounts(balances, mint):
    """{(accountIndex): (owner, amount)} of one mint from a pre/postTokenBalances list."""
    if balances is None:
        return {}
    if not isinstance(balances, list):
        raise SignalError('TOKEN_BALANCES_MALFORMED')
    out = {}
    for item in balances:
        if not isinstance(item, dict) or item.get('mint') != mint:
            continue
        owner, index, ui = item.get('owner'), item.get('accountIndex'), item.get('uiTokenAmount')
        amount = ui.get('amount') if isinstance(ui, dict) else None
        if not isinstance(owner, str) or not _BASE58.fullmatch(owner) or not _is_int(index) \
                or not isinstance(amount, str) or not amount.isdecimal() or index in out:
            raise SignalError('TOKEN_BALANCES_MALFORMED')
        out[index] = (owner, int(amount))
    return out


def parse_swap_tx(result, signature, mint, pool):
    """A jsonParsed getTransaction body -> ``{'signature', 'slot', 'block_time', 'failed', 'payer', 'buyers', 'pool_delta',
    'post'}`` or None when the node returned no transaction. ``buyers`` = {owner: tokens received} and is only filled when the
    pool's own balance of the mint FELL in the same transaction (a wallet-to-wallet transfer is not a buy). ``post`` = every
    non-pool owner's balance after the transaction. Raises SignalError on a malformed body."""
    if result is None:
        return None
    if not isinstance(result, dict) or not _is_int(result.get('slot')) or not isinstance(result.get('meta'), dict):
        raise SignalError('TRANSACTION_MALFORMED')
    meta, message = result['meta'], (result.get('transaction') or {}).get('message') if isinstance(result.get('transaction'), dict) else None
    if not isinstance(message, dict) or not isinstance(message.get('accountKeys'), list):
        raise SignalError('TRANSACTION_MALFORMED')
    block_time = result.get('blockTime')
    if not (block_time is None or _is_int(block_time)):
        raise SignalError('TRANSACTION_MALFORMED')
    signers = [k.get('pubkey') for k in message['accountKeys'] if isinstance(k, dict) and k.get('signer') is True]
    view = {'signature': signature, 'slot': result['slot'], 'block_time': block_time, 'failed': meta.get('err') is not None,
            'payer': signers[0] if signers else None, 'buyers': {}, 'pool_delta': 0, 'post': {}}
    if view['failed']:
        return view
    pre, post = _token_amounts(meta.get('preTokenBalances'), mint), _token_amounts(meta.get('postTokenBalances'), mint)
    delta, last = {}, {}
    for index, (owner, amount) in pre.items():
        delta[owner] = delta.get(owner, 0) - amount
    for index, (owner, amount) in post.items():
        delta[owner] = delta.get(owner, 0) + amount
        last[owner] = last.get(owner, 0) + amount
    view['pool_delta'] = delta.get(pool, 0)
    view['post'] = {o: a for o, a in last.items() if o != pool}
    if view['pool_delta'] < 0:
        view['buyers'] = {o: d for o, d in delta.items() if o != pool and d > 0}
    return view


def transfers_to(result, wallet):
    """[(source, lamports)] of the System-program transfers into ``wallet`` in a jsonParsed transaction (top level and inner)."""
    if result is None:
        return []
    if not isinstance(result, dict) or not isinstance(result.get('transaction'), dict) or not isinstance(result.get('meta'), dict):
        raise SignalError('TRANSACTION_MALFORMED')
    message = result['transaction'].get('message')
    if not isinstance(message, dict) or not isinstance(message.get('instructions'), list):
        raise SignalError('TRANSACTION_MALFORMED')
    instructions = list(message['instructions'])
    inner = result['meta'].get('innerInstructions') or []
    if not isinstance(inner, list):
        raise SignalError('TRANSACTION_MALFORMED')
    for group in inner:
        if isinstance(group, dict) and isinstance(group.get('instructions'), list):
            instructions.extend(group['instructions'])
    out = []
    for ix in instructions:
        parsed = ix.get('parsed') if isinstance(ix, dict) else None
        if not isinstance(parsed, dict) or ix.get('program') != 'system' or parsed.get('type') != 'transfer':
            continue
        info = parsed.get('info')
        if isinstance(info, dict) and info.get('destination') == wallet and isinstance(info.get('source'), str) \
                and _is_int(info.get('lamports')) and info['lamports'] > 0 and info['source'] != wallet:
            out.append((info['source'], info['lamports']))
    return out


def parse_supply(result):
    value = result.get('value') if isinstance(result, dict) else None
    amount = value.get('amount') if isinstance(value, dict) else None
    if not isinstance(amount, str) or not amount.isdecimal() or int(amount) <= 0:
        raise SignalError('SUPPLY_MALFORMED')
    return int(amount)


def analyze(*, grad_slot, txs, funding, supply_raw, cfg, hubs=()):
    """The figures of one candidate from already parsed window transactions (oldest first). Pure.

    ``funding`` = {wallet: source or None} for the wallets whose funding was looked up. ``hubs`` are detected hub funders (LINT2b):
    like ``ignore_funders`` they never form a cluster. Returns a dict whose figures are null (with a reason in ``unavailable``) when
    they cannot be computed. ``funders`` is the buyer -> funder map as looked up (null = no source found)."""
    unavailable = {}
    first = {}                                   # buyer -> (slot, order)
    last_post = {}
    for order, tx in enumerate(txs):
        for owner, amount in tx['post'].items():
            last_post[owner] = amount
        for owner in tx['buyers']:
            first.setdefault(owner, (tx['slot'], order))
    buyers = sorted(first, key=lambda w: first[w])
    out = {'buyers': len(buyers), 'same_slot_buyers': None, 'sniper_count': None, 'bundle_score': None,
           'bundled_supply_pct': None, 'bundled_wallets': None, 'funding_checked': len(funding), 'funding_clusters': [],
           'buyer_wallets': buyers, 'funders': dict(sorted(funding.items())),
           'hub_funders': sorted({s for s in funding.values() if s is not None and s in set(hubs)})}
    if not buyers:
        out.update(same_slot_buyers=0, sniper_count=0, bundled_wallets=0)
        unavailable['bundle_score'] = unavailable['bundled_supply_pct'] = 'NO_BUYERS_IN_WINDOW'
        out['unavailable'] = unavailable
        return out
    same_slot = [w for w in buyers if first[w][0] == grad_slot]
    snipers = [w for w in buyers if first[w][0] - grad_slot <= cfg['sniper_slots']]
    ignore = set(cfg['ignore_funders']) | set(hubs)
    by_source = {}
    for wallet, source in funding.items():
        if source is not None and source not in ignore:
            by_source.setdefault(source, []).append(wallet)
    clusters = [{'source': s, 'wallets': sorted(w, key=lambda x: first[x])} for s, w in sorted(by_source.items())
                if len(w) >= cfg['min_cluster']]
    bundled = set(same_slot) | {w for c in clusters for w in c['wallets']}
    out.update(same_slot_buyers=len(same_slot), sniper_count=len(snipers), bundled_wallets=len(bundled),
               bundle_score=round(len(bundled) / len(buyers), 4), funding_clusters=clusters,
               bundle_score_basis='same_slot+funding' if any(s is not None for s in funding.values()) else 'same_slot_only')
    if cfg['funding_wallets'] and not funding:
        unavailable['funding'] = 'NOT_CHECKED'
    if supply_raw is None:
        unavailable['bundled_supply_pct'] = 'SUPPLY_UNAVAILABLE'
    else:
        held = sum(last_post.get(w, 0) for w in bundled)
        out['bundled_supply_pct'] = float((Decimal(held) * 100 / Decimal(supply_raw)).quantize(Decimal('0.0001')))
    out['unavailable'] = unavailable
    return out


# --------------------------------------------------------------------------------------------------------- collection

class _Cap(Exception):
    """The per-candidate call cap (or credit shedding) stopped this candidate; what was learned is kept."""

    def __init__(self, code):
        self.code = code


class _Calls:
    def __init__(self, limit, shed, stop=None):
        self.limit, self.used, self.shed, self.learned, self.stop = limit, 0, shed, False, stop
        self.patience = None                       # W2b #8: a prefetch's own (shorter) patience per request; None = shed_wait_s

    def check(self):
        """Raise _Cap before a request that the stop (LINT2b), the cap or the credit shed forbids."""
        if self.stop is not None and self.stop.is_set():
            raise _Cap('STOPPED')
        if self.shed is not None and self.shed():
            raise _Cap('CREDIT_SHED')
        if self.used >= self.limit:
            raise _Cap('CALL_CAP')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, default=str).encode()


class WalletSignals:
    """The collector. ``helius`` is a low-lane client (``build_low_lane_helius``); ``store`` is the lean store.

    ``signals_pass()`` does a bounded amount of work and is meant for its own thread (``run_loop``). It finds candidates
    straight from the store (screened, no ``wallet_signals`` row yet), so a restart loses nothing and no queue can overflow."""

    def __init__(self, *, store, helius, cfg=None, code_version, strategy_version, clock=time.time, shed=None, stop=None, sleep=time.sleep,
                 paths_db=None):
        self.store, self.helius, self.clock, self.shed, self.sleep = store, helius, clock, shed, sleep
        self.paths_db = paths_db                   # LINT2: the L07R2 paths.sqlite (None = no path outcomes, trades only)
        self.cfg = make_config(cfg)
        self.code_version, self.strategy_version = code_version, strategy_version
        self.stop = stop or threading.Event()
        self._attempts = {}                        # mint -> (count, not_before)
        self._wallets = {}                         # wallet -> {mint: ts of its wallet_buy event}
        self._outcomes = {}                        # mint -> (outcome, ts of its wallet_outcome event)
        self._buy_mints = {}                       # mint -> first wallet_buy ts
        self._fundings = {}                        # LINT2b: funder -> {mint: first wallet_funding ts}
        self._hubs = set()                         # LINT2b: funders already recorded as hubs (wallet_hub events)
        self._watermark = 0
        self._wallet_lock = threading.RLock()
        self._last_resolve = 0.0
        self._resolve_cursor = 0
        self.last_pending = None                   # LINT2b: health (queue depth = candidates waiting for a row)
        self.prefetch = None                       # W2b #8: lean.prefetch.Prefetch (set by build_runner when configured)
        self.last_unresolved = None
        self.counts = {'collected': 0, 'partial': 0, 'transient_failures': 0, 'calls': 0, 'cap_stops': 0, 'shed_stops': 0,
                       'lane_sheds': 0, 'outcomes': 0, 'errors': 0, 'stopped': 0, 'yielded': 0, 'never_resolvable': 0}

    # -- plumbing -------------------------------------------------------------------------------------------------------
    def _query(self, sql, args=()):
        with self.store._lock:
            return self.store.db.execute(sql, args).fetchall()

    def _write(self, function, *args, **kwargs):
        return function(*args, code_version=self.code_version, strategy_version=self.strategy_version, **kwargs)

    def health(self):
        """LINT2b: counters + queue depth (``pending``: candidates waiting for their row at the last pass) + unresolved mints."""
        return {'enabled': True, **self.counts, 'pending': self.last_pending, 'unresolved': self.last_unresolved}

    def pending(self, now, limit):
        """[(candidate_id, mint, pool, slot, migrated_at, decision_ts)]: screened (PASS or REJECT), old enough that the window is
        complete, graduated within ``backfill_max_age_s`` (LINT2b: by ``migrated_at``, not the row time), with no
        ``wallet_signals`` row yet. ``decision_ts`` is the FIRST PASS/REJECT screen decision. Oldest first."""
        cutoff = now - self.cfg['backfill_max_age_s']
        rows = self._query(
            "SELECT c.id,c.mint,c.pool,c.slot,c.migrated_at,(SELECT MIN(d.ts) FROM decisions d WHERE d.mint=c.mint AND d.candidate_id=c.id "
            "AND d.kind='screen' AND d.action IN ('PASS','REJECT')) AS decision_ts FROM candidates c WHERE c.migrated_at>? "
            "AND c.pool IS NOT NULL AND c.slot IS NOT NULL AND c.migrated_at<=? "
            "AND NOT EXISTS (SELECT 1 FROM observations o WHERE o.mint=c.mint AND o.kind=?) AND decision_ts IS NOT NULL "
            'ORDER BY c.id LIMIT ?', (cutoff, now - self.cfg['window_s'] - 5, OBSERVATION_KIND, int(limit)))
        return rows

    def signals_pass(self):
        """One bounded pass: collect up to ``max_per_pass`` candidates, then (throttled) resolve token outcomes."""
        now = self.clock()
        done = 0
        rows = self.pending(now, self.cfg['backfill_limit'])
        self.last_pending = len(rows)
        for cid, mint, pool, slot, _migrated, decision_ts in rows:
            if self.stop.is_set() or done >= self.cfg['max_per_pass']:
                break
            attempts, not_before = self._attempts.get(mint, (0, 0.0))
            if now < not_before:
                continue
            try:
                self.collect(cid, mint, pool, slot, decision_ts=decision_ts)
            except Exception as error:               # recorded; a recorder failure never reaches entries or exits
                self.counts['errors'] += 1
                self._record_error('WALLET_SIGNALS_FAILED', mint, type(error).__name__)
                self._attempts[mint] = (attempts + 1, now + self.cfg['retry_delay_s'])
                if attempts + 1 > self.cfg['max_retries']:       # never retried forever: close it out with the reason
                    self._close_out(cid, mint, pool, slot, 'COLLECT_FAILED:' + type(error).__name__, decision_ts)
            done += 1
        if not self.stop.is_set() and now - self._last_resolve >= self.cfg['resolve_interval_s']:
            self._last_resolve = now
            try:
                self.resolve_outcomes(now)
            except Exception as error:
                self.counts['errors'] += 1
                self._record_error('WALLET_OUTCOMES_FAILED', None, type(error).__name__)
        return done

    def _close_out(self, cid, mint, pool, slot, reason, decision_ts=None):
        try:
            self._attempts.pop(mint, None)
            now = self.clock()
            features = {'version': VERSION, 'caveat': CAVEAT, 'mint': mint, 'pool': pool, 'grad_slot': slot, 'partial': True,
                        'unavailable': {'window': reason}, 'bundle_score': None, 'sniper_count': None, 'same_slot_buyers': None,
                        'bundled_supply_pct': None, 'smart_buyers_count': None, 'calls': 0, 'collected_at': now,
                        'decision_ts': decision_ts, 'late_s': None if decision_ts is None else round(now - decision_ts, 3)}
            self._write(self.store.add_observation, OBSERVATION_KIND, _canonical(features), mint=mint, candidate_id=cid,
                        meta=_meta_summary(features), ts=self.clock())
        except Exception:
            self.counts['errors'] += 1

    def _record_error(self, code, mint, message):
        try:
            self._write(self.store.add_error, code, transient=True, scope='wallet_signals', mint=mint, message=message, ts=self.clock())
        except Exception:
            pass

    def run_loop(self):
        while not self.stop.is_set():
            try:
                self.signals_pass()
            except Exception:
                self.counts['errors'] += 1
            self.stop.wait(self.cfg['pass_interval_s'])

    # -- one candidate --------------------------------------------------------------------------------------------------
    def _yield_lane(self):
        """LINT2b: True when taking a token now would leave fewer than ``reserve_tokens`` for the other low-lane users (path
        recorder, L11 account checks, L12F). Best effort: a peek at the shared bucket, nothing is taken."""
        reserve = self.cfg['reserve_tokens']
        limiter = getattr(getattr(self.helius, 'transport', None), 'limiter', None)
        available = getattr(limiter, 'available', None)
        if not reserve or available is None:
            return False
        return available() < 1 + reserve

    def _rpc(self, calls, method, params, label, retained, mint, cid):
        """One request on the shared low lane. ``LANE_SHED`` sent nothing and costs nothing against the cap: a ``no_token`` shed (or
        a yield to the other low-lane users) is paced (this collector has its own thread) up to ``shed_wait_s``; a shed window or
        lost patience stops the candidate (or, when nothing was learned yet, raises a transient error so it is retried later).
        ``stop`` is checked before every try (LINT2b)."""
        waited = 0.0
        while True:
            calls.check()                                              # stop, call cap, credit shed: before ANY request
            if self._yield_lane():
                self.counts['yielded'] += 1
                why, error = 'no_token', ProviderError(LANE_SHED, True, meta={'why': 'yield'})
            else:
                try:
                    result, raw, meta = self.helius.rpc(method, params)
                    break
                except ProviderError as failure:
                    if failure.code != LANE_SHED:
                        calls.used += 1
                        self.counts['calls'] += 1
                        raise
                    self.counts['lane_sheds'] += 1
                    why, error = failure.meta.get('why'), failure
            if why != 'no_token' or waited >= (self.cfg['shed_wait_s'] if calls.patience is None else calls.patience):
                if not calls.learned:
                    raise error
                raise _Cap(LANE_SHED) from None
            self.sleep(self.cfg['pace_s'])
            waited += self.cfg['pace_s']
        calls.used += 1
        self.counts['calls'] += 1
        keep = self.cfg['retain_raw']
        if keep == 'all' or (keep == 'signatures' and retained):
            self._write(self.store.add_observation, 'wallet_signals:raw:' + label, raw, mint=mint, candidate_id=cid,
                        meta={'method': method, 'attempts': meta.get('attempts')}, ts=self.clock())
        return result

    def _signatures(self, calls, address, mint, cid, label, *, before=None, limit):
        options = {'limit': limit, 'commitment': 'confirmed'}
        if before:
            options['before'] = before
        return parse_signatures(self._rpc(calls, 'getSignaturesForAddress', [address, options], label, True, mint, cid))

    def _screen_supply(self, cid, mint):
        """The supply the SCREEN read (pre-decision), from its first PASS/REJECT decision, or None."""
        for row in self.store.rows('decisions', mint=mint, kind='screen', limit=100):
            if row['candidate_id'] == cid and row['action'] in ('PASS', 'REJECT'):
                try:
                    value = json.loads(row['features']).get('supply_raw')
                    return int(value) if value is not None and int(value) > 0 else None
                except (ValueError, TypeError, AttributeError):
                    return None
        return None

    def collect(self, cid, mint, pool, grad_slot, *, decision_ts=None, sink=None, supply=None):
        """Collect, analyze and record one candidate AS OF ``decision_ts`` (LINT2b). A transient failure before anything was learned
        is retried later (up to ``max_retries``); a stop writes nothing; everything else is recorded with its reasons.

        W2b #8: with a ``sink`` (a dict) this is a PREFETCH: ``decision_ts`` is the AS-OF time the window ends at (never later than the
        decision the row will be committed to), the supply is ``supply`` (read before the decision) or one call, nothing is written,
        ``sink`` receives ``features`` / ``events`` / ``finished_at`` (``sink['patience_s']`` optionally shortens the token patience), and
        a failure leaves no trace in this collector's own retry bookkeeping. Without a sink, a finished prefetch of this candidate is
        committed instead of collecting (no request)."""
        # --- W2b hook ---
        if sink is None and self.prefetch is not None and decision_ts is not None:
            committed = self.prefetch.commit_wallet(self, cid, mint, pool, grad_slot, decision_ts)
            if committed is not None:
                return committed
        # --- end W2b ---
        cfg, started = self.cfg, self.clock()
        calls = _Calls(cfg['max_calls'], self.shed, self.stop)
        if sink is not None:
            calls.patience = sink.get('patience_s')
        unavailable, notes = {}, {}
        txs, funding, supply, window = [], {}, None, []
        supply_source = None
        try:
            # -- 1. the pool's signatures back to the graduation slot (oldest first afterwards)
            seen, before, reached = [], None, False
            for page in range(cfg['sig_pages']):
                try:
                    rows = self._signatures(calls, pool, mint, cid, 'signatures:%d' % page, before=before, limit=cfg['sig_limit'])
                except ProviderError as error:
                    if page == 0:
                        raise
                    unavailable['window'] = 'SIGNATURES_PAGE_%d:%s' % (page, error.code)
                    break
                calls.learned = True
                seen.extend(rows)
                if len(rows) < cfg['sig_limit']:
                    reached = True                    # the store of signatures ends here: nothing older exists
                    break
                if rows[-1]['slot'] <= grad_slot:
                    reached = True
                    break
                before = rows[-1]['signature']
            ordered = list(reversed(seen))
            window_slots = int(cfg['window_s'] / SLOT_SECONDS)
            if not reached:
                unavailable['window'] = unavailable.get('window') or 'WINDOW_START_NOT_REACHED'
                notes['oldest_slot_seen'] = ordered[0]['slot'] if ordered else None
            else:
                window = [s for s in ordered if not s['failed'] and grad_slot <= s['slot'] <= grad_slot + window_slots]
                notes.update(_window_cut(window, ordered, grad_slot, decision_ts, cfg['window_s']))
                if decision_ts is not None:           # LINT2b: nothing after the decision (unknown block time = unknown = out)
                    window = [s for s in window if s['block_time'] is not None and s['block_time'] <= decision_ts]
            notes.update(signatures_seen=len(seen), window_txs=len(window), examined_txs=0)
            if 'window' not in unavailable:
                # -- 2. the supply (for bundled_supply_pct): the one the screen read, else a call (flagged: read after the decision)
                # W2b #8: a prefetch has no screen yet: the supply read before the decision (``supply``) or one call, flagged rpc_prefetch
                supply = self._screen_supply(cid, mint) if sink is None else supply
                supply_source = ('screen' if sink is None else 'rpc_prefetch') if supply is not None else None
                if supply is None:
                    try:
                        supply = parse_supply(self._rpc(calls, 'getTokenSupply', [mint], 'supply', False, mint, cid))
                        supply_source = 'rpc_after_decision' if sink is None else 'rpc_prefetch'
                    except ProviderError as error:
                        unavailable['supply'] = 'SUPPLY_UNAVAILABLE:' + error.code
                    except SignalError as error:
                        unavailable['supply'] = error.code
                # -- 3. the earliest window transactions
                notes['tx_skipped'] = 0
                for entry in window[:cfg['max_swap_txs']]:
                    try:
                        body = self._rpc(calls, 'getTransaction', [entry['signature'], {'encoding': 'jsonParsed', 'commitment': 'confirmed',
                                                                                        'maxSupportedTransactionVersion': 0}],
                                         'tx', False, mint, cid)
                        view = parse_swap_tx(body, entry['signature'], mint, pool)
                    except (ProviderError, SignalError):
                        view = None
                    if view is not None and decision_ts is not None and (view['block_time'] is None or view['block_time'] > decision_ts):
                        view = None                   # LINT2b: defensive, the body disagrees with its signature's time
                    if view is None or view['failed']:
                        notes['tx_skipped'] += 1
                        continue
                    txs.append(view)
                notes['truncated_by_cap'] = len(window) > cfg['max_swap_txs']
                # -- 4. funding sources (1 hop) of the earliest buyers (their history BEFORE their buy: pre-decision)
                firsts = []
                for tx in txs:
                    for owner in tx['buyers']:
                        if owner not in dict(firsts):
                            firsts.append((owner, tx['signature']))
                for wallet, signature in firsts[:cfg['funding_wallets']]:
                    funding[wallet] = self._funder(calls, wallet, signature, mint, cid, unavailable)
        except _Cap as stop:
            if stop.code == 'STOPPED':                # LINT2b: the runner is stopping: no row, collected again next start
                self.counts['stopped'] += 1
                return None
            unavailable['stopped'] = stop.code
            self.counts['shed_stops' if stop.code in ('CREDIT_SHED', LANE_SHED) else 'cap_stops'] += 1
        except ProviderError as error:
            if sink is not None and not calls.learned and error.transient:      # W2b #8: the prefetch retries on its own schedule
                return None
            if not calls.learned and error.transient:     # the very first call failed: nothing learned, try again later
                attempts = self._attempts.get(mint, (0, 0.0))[0] + 1
                if attempts <= cfg['max_retries']:
                    self._attempts[mint] = (attempts, self.clock() + cfg['retry_delay_s'])
                    self.counts['transient_failures'] += 1
                    return None
            unavailable['window'] = 'SIGNATURES_UNAVAILABLE:' + error.code
        except SignalError as error:
            unavailable['window'] = error.code
        self._attempts.pop(mint, None)
        notes['examined_txs'] = len(txs)
        window_ok = 'window' not in unavailable
        wallets, watermark = self.wallet_table(asof=decision_ts)
        hubs = self.hubs_asof(decision_ts if decision_ts is not None else started, mint, funding.values())
        figures = analyze(grad_slot=grad_slot, txs=txs, funding=funding,
                          supply_raw=supply, cfg=cfg, hubs=hubs) if window_ok else {
            'buyers': None, 'same_slot_buyers': None, 'sniper_count': None, 'bundle_score': None, 'bundled_supply_pct': None,
            'bundled_wallets': None, 'funding_checked': 0, 'funding_clusters': [], 'buyer_wallets': [], 'funders': {},
            'hub_funders': [], 'unavailable': {}}
        unavailable = {**figures.pop('unavailable'), **unavailable}
        if not window_ok:
            for field in ('same_slot_buyers', 'sniper_count', 'bundle_score', 'bundled_supply_pct'):
                unavailable.setdefault(field, unavailable['window'])
        if decision_ts is None:
            unavailable['decision_ts'] = 'UNKNOWN'
        smart = [w for w in figures['buyer_wallets'] if self.is_smart(wallets.get(w))] if window_ok else None
        features = {'version': VERSION, 'caveat': CAVEAT, 'mint': mint, 'pool': pool, 'grad_slot': grad_slot,
                    'window_s': cfg['window_s'], **notes, **figures, 'smart_buyers_count': None if smart is None else len(smart),
                    'smart_wallets': smart, 'smart_asof_event_id': watermark, 'smart_asof_ts': decision_ts,
                    'calls': calls.used, 'call_cap': cfg['max_calls'], 'supply_source': supply_source,
                    'decision_ts': decision_ts, 'collected_at': started,
                    'late_s': None if decision_ts is None else round(started - decision_ts, 3),
                    'unavailable': unavailable, 'partial': bool(unavailable)}
        # the candidate's own buys are written AFTER its smart count: a wallet is never rated by the token being scored
        events = [('wallet_buy', {'wallet': wallet, 'mint': mint, 'slot_offset': next(tx for tx in txs if wallet in tx['buyers'])['slot'] - grad_slot})
                  for wallet in figures['buyer_wallets']]
        # LINT2b: the funding lookups persist (hub detection); a newly detected hub is recorded once
        events += [('wallet_funding', {'funder': source, 'wallet': wallet, 'mint': mint}) for wallet, source in sorted(figures['funders'].items())
                   if source is not None]
        events += [('wallet_hub', {'funder': hub, 'detected_on': mint, 'asof_ts': decision_ts, 'min_mints': cfg['hub_min_mints'],
                                   'window_s': cfg['hub_window_s']}) for hub in figures['hub_funders']]
        # --- W2b hook: a prefetch keeps the row and its events; the commit writes them stamped with the real decision ---
        if sink is not None:
            sink.update(features=features, events=events, finished_at=self.clock(), window_end_ts=decision_ts)
            return features
        # --- end W2b ---
        self._persist(features, events, cid, mint)
        return features

    def _persist(self, features, events, cid, mint):
        """Write one ``wallet_signals`` row and its events (``wallet_buy`` / ``wallet_funding`` / a newly detected ``wallet_hub``)."""
        row_id = self._write(self.store.add_observation, OBSERVATION_KIND, _canonical(features), mint=mint, candidate_id=cid,
                             meta=_meta_summary(features), ts=self.clock())
        for kind, payload in events:
            if kind == 'wallet_buy':
                self._write(self.store.record, kind, {**payload, 'candidate_id': cid, 'signals_observation': row_id}, ts=self.clock())
            elif kind == 'wallet_funding':
                self._write(self.store.record, kind, {**payload, 'candidate_id': cid}, ts=self.clock())
            elif payload['funder'] not in self._hubs:
                self._hubs.add(payload['funder'])
                self._write(self.store.record, kind, payload, ts=self.clock())
        self.counts['collected'] += 1
        self.counts['partial'] += 1 if features['partial'] else 0

    def _funder(self, calls, wallet, buy_signature, mint, cid, unavailable):
        """The source wallet of ``wallet``'s first funding transfer, or None (with the reason in ``unavailable``)."""
        cfg = self.cfg
        key = 'funding:' + wallet[:8]
        try:
            prior = self._signatures(calls, wallet, mint, cid, 'wallet_history', before=buy_signature, limit=cfg['funding_history'])
            if not prior:
                unavailable[key] = 'NO_PRIOR_HISTORY'
                return None
            if len(prior) >= cfg['funding_history']:
                unavailable[key] = 'FUNDING_HISTORY_TRUNCATED'
                return None
            oldest = prior[-1]
            body = self._rpc(calls, 'getTransaction', [oldest['signature'], {'encoding': 'jsonParsed', 'commitment': 'confirmed',
                                                                              'maxSupportedTransactionVersion': 0}], 'funding_tx', False, mint, cid)
            options = [t for t in transfers_to(body, wallet) if t[1] >= cfg['min_funding_lamports']]
        except ProviderError as error:
            unavailable[key] = 'FUNDING_UNAVAILABLE:' + error.code
            return None
        except SignalError as error:
            unavailable[key] = error.code
            return None
        if not options:
            unavailable[key] = 'NO_FUNDING_TRANSFER'
            return None
        return max(options, key=lambda t: t[1])[0]

    # -- the smart-wallet table ----------------------------------------------------------------------------------------------
    def _load_events(self):
        rows = self._query("SELECT id,kind,payload,ts FROM events WHERE kind IN ('wallet_buy','wallet_outcome','wallet_funding','wallet_hub') "
                           "AND id>? ORDER BY id", (self._watermark,))
        for event_id, kind, payload, ts in rows:
            self._watermark = event_id
            data = json.loads(payload)
            if kind == 'wallet_buy':
                self._wallets.setdefault(data['wallet'], {}).setdefault(data['mint'], ts)
                self._buy_mints.setdefault(data['mint'], ts)
            elif kind == 'wallet_outcome':
                self._outcomes.setdefault(data['mint'], (data['outcome'], ts))
            elif kind == 'wallet_funding':
                self._fundings.setdefault(data['funder'], {}).setdefault(data['mint'], ts)
            else:
                self._hubs.add(data['funder'])

    def wallet_table(self, asof=None):
        """({wallet: {'wins', 'rugs', 'flat', 'unresolved', 'buys'}}, watermark) from the append-only events (incremental).
        With ``asof`` (LINT2b) only buys and outcomes recorded BEFORE that time count: no outcome resolved after a decision."""
        with self._wallet_lock:
            self._load_events()
            table = {}
            for wallet, mints in self._wallets.items():
                bought = [m for m, ts in mints.items() if asof is None or ts < asof]
                if not bought:
                    continue
                record = {'wins': 0, 'rugs': 0, 'flat': 0, 'unresolved': 0, 'buys': len(bought)}
                for mint in bought:
                    outcome, at = self._outcomes.get(mint, (None, None))
                    if outcome is not None and asof is not None and not at < asof:
                        outcome = None
                    key = {'WIN': 'wins', 'RUG': 'rugs', 'FLAT': 'flat'}.get(outcome, 'unresolved')
                    record[key] += 1
                table[wallet] = record
            return table, self._watermark

    def hubs_asof(self, asof, mint, sources):
        """LINT2b: the funders among ``sources`` that funded buyers of >= ``hub_min_mints`` distinct mints (this one included)
        within ``hub_window_s`` before ``asof``, from the persisted ``wallet_funding`` events. Detected, never hard-coded."""
        with self._wallet_lock:
            self._load_events()
            out = set()
            for source in {s for s in sources if s is not None}:
                prior = {m for m, ts in self._fundings.get(source, {}).items()
                         if m != mint and asof - self.cfg['hub_window_s'] < ts <= asof}
                if len(prior) + 1 >= self.cfg['hub_min_mints']:
                    out.add(source)
            return out

    def score(self, record):
        return (record['wins'] + 1) / (record['wins'] + record['rugs'] + 2)

    def is_smart(self, record):
        return bool(record) and record['wins'] >= self.cfg['min_wins'] and self.score(record) >= self.cfg['smart_threshold']

    def never_resolvable(self, mint):
        """LINT2b: a hazard reject (never a PASS, a REJECT for a non-market reason) that the desk never bought: no path is ever
        recorded for it (L07R2 skips hazards) and no trade exists, so it can never get an outcome."""
        screens = [d for d in self.store.rows('decisions', mint=mint, kind='screen', limit=1000) if d['action'] in ('PASS', 'REJECT')]
        if not screens or any(d['action'] == 'PASS' for d in screens):
            return False
        if any(f['side'] == 'buy' for f in self.store.rows('fills', mint=mint, limit=1000)):
            return False
        return all(not set(json.loads(d['reasons'])) <= SOFT_SCREEN_REASONS for d in screens)

    def resolve_outcomes(self, now):
        """Write one ``wallet_outcome`` event per token whose outcome became known. Bounded per pass and ROUND-ROBIN over the
        unresolved mints (LINT2b), so a new WIN/RUG never waits behind a backlog; hazard rejects close at once as UNKNOWN."""
        self.wallet_table()
        todo = [m for m in self._buy_mints if m not in self._outcomes]
        self.last_unresolved = len(todo)
        if not todo:
            return
        start = self._resolve_cursor % len(todo)
        batch = (todo[start:] + todo[:start])[:self.cfg['resolve_batch']]
        self._resolve_cursor = start + len(batch)
        closed = self.store.closed_positions()                         # once per pass, not once per mint
        for mint in batch:
            if self.stop.is_set():
                break
            outcome, source, detail = path_outcome(self.paths_db, mint, self.cfg)
            if outcome is None:
                outcome, source, detail = trade_outcome(self.store, mint, self.cfg, closed=closed)
            if outcome is None and self.never_resolvable(mint):
                outcome, source, detail = 'UNKNOWN', 'NEVER_RESOLVABLE', {}
                self.counts['never_resolvable'] += 1
            if outcome is None:
                if now - self._buy_mints[mint] >= self.cfg['outcome_max_age_s']:
                    outcome, source, detail = 'UNKNOWN', 'AGE_LIMIT', {}
            if outcome is None:
                continue
            self._write(self.store.record, 'wallet_outcome', {'mint': mint, 'outcome': outcome, 'source': source, 'detail': detail}, ts=self.clock())
            self.counts['outcomes'] += 1
        self.wallet_table()

def build(cfg, *, store, keys, code_version, strategy_version, clock=time.time, transport_kwargs=None, shed=None, paths_db=None):
    """The collector of a validated config, on the shared LOW lane (``lean.providers.low``)."""
    from lean import providers
    transport_kwargs = dict(transport_kwargs or {})
    helius = providers.low(keys, **transport_kwargs).helius
    return WalletSignals(store=store, helius=helius, cfg=cfg, code_version=code_version, strategy_version=strategy_version, clock=clock,
                         shed=shed, sleep=transport_kwargs.get('sleep', time.sleep), paths_db=paths_db)


def _window_cut(window, ordered, grad_slot, decision_ts, window_s):
    """LINT2b notes: where the examined swap window ends relative to the decision (by block time)."""
    grad_time = next((s['block_time'] for s in ordered if s['slot'] == grad_slot and s['block_time'] is not None), None)
    if grad_time is None:
        times = [s['block_time'] for s in window if s['block_time'] is not None]
        grad_time = min(times) if times else None
    out = {'grad_block_time': grad_time, 'window_cut_by_decision': None, 'window_s_effective': None,
           'window_after_decision_s': None, 'window_unknown_time_skipped': 0}
    if decision_ts is None:
        return out                                     # unknown decision: the window may run past it (flag stays null)
    out['window_unknown_time_skipped'] = sum(1 for s in window if s['block_time'] is None)
    out['window_after_decision_s'] = 0.0               # the window is cut at the decision below
    if grad_time is not None:
        out['window_cut_by_decision'] = grad_time + window_s > decision_ts
        out['window_s_effective'] = round(max(0.0, min(float(window_s), decision_ts - grad_time)), 3)
    return out


def _meta_summary(features):
    keys = ('version', 'bundle_score', 'sniper_count', 'same_slot_buyers', 'bundled_supply_pct', 'bundled_wallets', 'buyers',
            'smart_buyers_count', 'smart_asof_event_id', 'calls', 'partial', 'unavailable', 'window_txs', 'examined_txs', 'funding_checked',
            'decision_ts', 'collected_at', 'late_s', 'bundle_score_basis', 'window_cut_by_decision', 'window_s_effective',
            'window_after_decision_s', 'hub_funders', 'supply_source')
    return {k: features.get(k) for k in keys}


# ------------------------------------------------------------------------------------------------------------ outcomes

def read_path(paths_db, mint):
    """LINT2: THE reader of L07R2 path data. ``([(status, net_sol)] oldest first, ended)`` of ``mint`` from ``paths_db`` and the
    archived ``paths-*.sqlite`` beside it, each opened READ-ONLY with its own short-lived connection (never lean.sqlite, never a
    store lock). A missing file is no data; nothing is created."""
    if paths_db is None:
        return [], False
    main = Path(paths_db)
    marks, ended = [], False
    for path in sorted(main.parent.glob('paths-*.sqlite')) + [main]:
        if not path.is_file():
            continue
        connection = sqlite3.connect('file:%s?mode=ro' % quote(str(path)), uri=True, timeout=5)
        try:
            # --- W2b #9: a sampled HAZARD reject's path is never an outcome here (a wallet is not rated by a token the desk rejected) ---
            try:
                hazard = connection.execute('SELECT 1 FROM paths WHERE mint=? AND stage=?', (mint, 'hazard_reject')).fetchone() is not None
            except sqlite3.OperationalError:                           # a file without a ``paths`` table / ``stage`` column: no hazard paths
                hazard = False
            if hazard:
                continue
            # --- end W2b ---
            marks += connection.execute('SELECT ts,status,net_sol FROM path_marks WHERE mint=? ORDER BY ts', (mint,)).fetchall()
            ended = ended or connection.execute('SELECT 1 FROM path_ends WHERE mint=?', (mint,)).fetchone() is not None
        finally:
            connection.close()
    return [(status, net) for _, status, net in sorted(marks, key=lambda m: m[0])], ended


def path_outcome(paths_db, mint, cfg):
    """('WIN'|'RUG'|'FLAT'|None, 'PATH', detail) from the L07R2 marks (``net_sol`` of the path's reference quantity, via
    ``read_path``). First hit wins: >= win_multiple x the first mark is a WIN, <= rug_price_frac x (or an EMPTY_POOL mark) a RUG.
    A finished path (``path_ends``) with neither is FLAT. No marks, or a path still running with neither hit, is unresolved."""
    marks, ended = read_path(paths_db, mint)
    if not marks:
        return None, 'PATH', {}
    reference = None
    for status, net in marks:
        if status == 'EMPTY_POOL' and reference is not None:
            return 'RUG', 'PATH', {'ratio': 0.0}
        try:
            price = Decimal(str(net))
        except (ValueError, ArithmeticError, TypeError):
            continue
        if status != 'OK' or not price.is_finite() or price <= 0:
            continue
        if reference is None:
            reference = price
            continue
        ratio = price / reference
        if ratio >= Decimal(str(cfg['win_multiple'])):
            return 'WIN', 'PATH', {'ratio': float(ratio)}
        if ratio <= Decimal(str(cfg['rug_price_frac'])):
            return 'RUG', 'PATH', {'ratio': float(ratio)}
    if ended:
        return 'FLAT', 'PATH', {}
    return None, 'PATH', {}


def trade_outcome(store, mint, cfg, *, closed=None):
    """The desk's own closed trade on the token: realized PnL / cost >= win_multiple - 1 is a WIN, a loss of at least
    ``rug_loss_frac`` of the cost a RUG, anything else FLAT. An open or never entered token is unresolved. ``closed`` = a
    ``store.closed_positions()`` list already read this pass (LINT2b: read once per pass, not once per mint)."""
    closed = [c for c in (store.closed_positions() if closed is None else closed) if c['mint'] == mint]
    if not closed:
        return None, 'TRADE', {}
    buys = [f for f in store.rows('fills', mint=mint, limit=100000) if f['side'] == 'buy']
    state = closed[-1]['state']
    cost = sum(f['sol_lamports'] + f['fee_lamports'] for f in buys if closed[-1]['open_fill_id'] <= f['id'] <= closed[-1]['fill_id'])
    pnl = state.get('trade_pnl_lamports')
    if not cost or pnl is None:
        return None, 'TRADE', {}
    ratio = Decimal(int(pnl)) / Decimal(cost)
    detail = {'pnl_over_cost': float(ratio)}
    if ratio >= Decimal(str(cfg['win_multiple'])) - 1:
        return 'WIN', 'TRADE', detail
    if ratio <= -Decimal(str(cfg['rug_loss_frac'])):
        return 'RUG', 'TRADE', detail
    return 'FLAT', 'TRADE', detail
