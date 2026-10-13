"""L20: order-flow / bot-pump features AS OF the decision. FEATURES ONLY: nothing here is read by ``lean.strategy`` and nothing
changes an entry decision. PAPER ONLY: read-only RPC methods on the shared LOW lane, no signing, no sending.

Why. A token can print a perfectly linear all-green 1-minute staircase because one bot trades it every ~80 ms (the FOMO case: ~14k
pool transactions in 18 minutes). Such a chart should be recognisable from data the desk had AT ENTRY TIME. One
``observations(kind='flow')`` row is written per screen PASS decision (re-screens included), computed from the pool's swaps BEFORE that
decision only:

  tx_rate_per_s      successful pool transactions per second over the pre-decision window (from the signature list, every one counted)
  buy_sell_ratio     buys / max(1, sells) in the sampled swaps
  unique_buyers      distinct buying signers in the sample          (a lower bound: sample only)
  unique_sellers     distinct selling signers in the sample         (a lower bound: sample only)
  trade_size_cv      coefficient of variation (population sd / mean) of the sampled swaps' SOL sizes; LOW = bot-uniform sizes
  monotonic_up_frac  share of up-moves among the consecutive price changes of the sampled swaps (oldest first); 1.0 = a staircase
  max_drawdown_pre   the largest pullback from a running peak over the same price sequence (0.0 = never pulled back)
Wash trading (addendum #1; the FOMO case had ~14k pool txs in 18 min and only 74 holders), over the sampled swaps:
  txs_per_unique_wallet  sampled swaps / distinct signers (1.0 = every swap a different wallet; high = a few wallets churn)
  roundtrip_wallets      signers that both bought and sold in the sample
  roundtrip_share        roundtrip_wallets / distinct signers
  top5_signer_tx_share   share of the sampled swaps made by the 5 most active signers
Price-path shape (addendum #7), over the sampled price sequence:
  return_vol             population sd of the per-swap log returns: ln(p_j / p_i) / (transactions from i to j) of sampled pairs
  max_runup_pct          the largest rise from a running low, in percent
  longest_up_streak      the longest run of consecutive up-moves (an unchanged price ends a run)

The price of a swap is the pool's own spot price right AFTER it (post WSOL vault / post token vault, from the transaction's
post-token-balances), so the sequence is the pool's price path, not a mix of buy and sell execution prices.

No lookahead. A block time is whole seconds, so the window is counted in whole seconds: from ``ceil(decision_ts - window_s)`` (or
the graduation time when it is later) up to ``floor(decision_ts)`` EXCLUSIVE: a transaction is used only when its whole second ended
at or before the decision (``block_time + 1 <= floor(decision_ts)``), and the rate's span is the same whole-second interval. Every row
carries ``decision_ts``, ``collected_at``, ``late_s`` and ``window_after_decision_s`` (always 0.0: the window ends at the decision), and
``flags`` like the L12F v3 rows (``SAMPLE`` for figures read off the sampled swaps, ``TRUNCATED_SPAN`` when the signature pages did not
reach the window start and the rate is over the covered span only).

Budget. At most ``sig_pages`` (3) ``getSignaturesForAddress`` pages + ``max_txs`` (12) ``getTransaction`` per decision, a hard
``max_calls_per_hour`` (900) across all decisions, only while ``reserve_tokens`` more low-lane tokens would remain for the other low-lane
users, paced at ``pace_s`` on a ``no_token`` shed (its own thread). A shed window, the hourly cap or a stop ends a decision: it is retried
later when nothing was learned yet (up to ``max_retries``), otherwise the row is written partial with the reason in ``unavailable``.
A failure here never touches entries, exits or the accounting.
"""
import json
import logging
import hashlib
import math
import threading
import time
from collections import deque
from decimal import Decimal

from lean.providers import LANE_SHED, ProviderError, MAX_TX_VERSION
from lean.wallet_signals import SignalError, _token_amounts, parse_signatures

log = logging.getLogger('lean.flow')

VERSION = 'flow_v1'
KIND = 'flow'
SOL_MINT = 'So11111111111111111111111111111111111111112'
SAMPLE_FIELDS = ('buy_sell_ratio', 'unique_buyers', 'unique_sellers', 'trade_size_cv', 'monotonic_up_frac', 'max_drawdown_pre',
                 'txs_per_unique_wallet', 'roundtrip_wallets', 'roundtrip_share', 'top5_signer_tx_share',
                 'return_vol', 'max_runup_pct', 'longest_up_streak')
FEATURES = ('tx_rate_per_s',) + SAMPLE_FIELDS
DEFAULTS = {
    'enabled': False,
    'window_s': 180,               # pre-decision window
    'sig_limit': 1000,             # signatures per page (RPC maximum)
    'sig_pages': 3,                # pages walked back from "now" past the decision to the window start
    'max_txs': 12,                 # swaps fetched (getTransaction each), one per equal stratum of the window
    'min_span_s': 5,               # a rate over a shorter covered span is null (SPAN_TOO_SHORT)
    'max_per_pass': 2,
    'pass_interval_s': 5.0,
    'pace_s': 0.5,                 # wait between tries when the low lane has no token right now (own thread)
    'shed_wait_s': 20.0,           # total patience per request
    'reserve_tokens': 1,           # only call while this many MORE low-lane tokens would remain for the other users
    'max_age_s': 900,              # a PASS decision older than this is never collected
    'max_retries': 3,
    'retry_delay_s': 30.0,
    'max_calls_per_hour': 900,     # HARD cap across all decisions
}


class ConfigError(ValueError):
    pass


def config(raw):
    """lean.json ``flow`` -> validated settings. Absent / null = disabled. Unknown keys and wrong types are refused."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ConfigError('flow must be an object')
    unknown = set(raw) - set(DEFAULTS) - {'_comment'}
    if unknown:
        raise ConfigError('flow: unknown keys %s' % sorted(unknown))
    out = {k: raw.get(k, v) for k, v in DEFAULTS.items()}
    if type(out['enabled']) is not bool:
        raise ConfigError('flow.enabled must be true or false')
    for key, low, high in (('window_s', 10, 3600), ('sig_limit', 1, 1000), ('sig_pages', 1, 10), ('max_txs', 2, 50), ('min_span_s', 1, 600),
                           ('max_per_pass', 1, 50), ('reserve_tokens', 0, 10), ('max_age_s', 60, 86400), ('max_retries', 0, 20),
                           ('max_calls_per_hour', 1, 100_000)):
        if type(out[key]) is not int or not low <= out[key] <= high:
            raise ConfigError('flow.%s must be an integer in [%d, %d]' % (key, low, high))
    for key, low, high in (('pass_interval_s', 0.1, 600), ('pace_s', 0.05, 60), ('shed_wait_s', 0, 600), ('retry_delay_s', 0, 3600)):
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or not low <= value <= high:
            raise ConfigError('flow.%s out of range' % key)
        out[key] = float(value)
    return out


# ------------------------------------------------------------------------------------------------------- shared plumbing
class HourBudget:
    """A sliding one-hour cap on requests SENT (thread-safe). ``take`` before a request, ``refund`` when nothing was sent."""

    def __init__(self, limit, clock):
        self.limit, self.clock = int(limit), clock
        self._sent = deque()
        self._lock = threading.Lock()

    def _trim(self, now):
        while self._sent and self._sent[0] <= now - 3600:
            self._sent.popleft()

    def take(self):
        with self._lock:
            now = self.clock()
            self._trim(now)
            if len(self._sent) >= self.limit:
                return False
            self._sent.append(now)
            return True

    def refund(self):
        with self._lock:
            if self._sent:
                self._sent.pop()

    def used(self):
        with self._lock:
            self._trim(self.clock())
            return len(self._sent)


class Stop(Exception):
    """A unit of low-lane work ended before it was done: ``code`` = STOPPED | CALL_CAP | HOURLY_BUDGET | LANE_SHED."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class LowCalls:
    """Bounded, paced requests on the shared low lane for ONE unit of work (a decision, a probe). ``LANE_SHED`` sent nothing: it is
    refunded to the hourly budget and not counted against ``limit``; a ``no_token`` shed (or a yield) is retried every ``pace_s``
    through ``wait`` for up to ``patience_s``; a shed WINDOW (429 / credit shed) or lost patience raises ``Stop('LANE_SHED')``."""

    def __init__(self, helius, *, limit, budget, wait, pace_s, patience_s, stop=None, reserve=0, deadline=None, monotonic=time.monotonic):
        self.helius, self.limit, self.budget, self.wait = helius, limit, budget, wait
        self.pace_s, self.patience_s, self.stop, self.reserve = pace_s, patience_s, stop, reserve
        self.deadline, self.monotonic = deadline, monotonic
        self.used = 0

    def _yield(self):
        if not self.reserve:
            return False
        limiter = getattr(getattr(self.helius, 'transport', None), 'limiter', None)
        available = getattr(limiter, 'available', None)
        return available is not None and available() < 1 + self.reserve

    def rpc(self, method, params):
        waited = 0.0
        while True:
            if self.stop is not None and self.stop.is_set():
                raise Stop('STOPPED')
            if self.deadline is not None and self.monotonic() >= self.deadline:
                raise Stop('DEADLINE')
            if self.used >= self.limit:
                raise Stop('CALL_CAP')
            why = None
            if self._yield():
                why = 'no_token'
            elif not self.budget.take():
                raise Stop('HOURLY_BUDGET')
            else:
                try:
                    result = self.helius.rpc(method, params)[0]
                except ProviderError as error:
                    if error.code != LANE_SHED:
                        self.used += 1
                        raise
                    self.budget.refund()
                    why = error.meta.get('why')
                else:
                    self.used += 1
                    return result
            if why != 'no_token' or waited >= self.patience_s:
                raise Stop(LANE_SHED)
            self.wait(self.pace_s)
            waited += self.pace_s


def stratified(items, k):
    """``k`` of ``items`` (signature rows), in their order: the list is cut into ``k`` equal strata (time coverage) and each stratum
    gives the row whose signature has the smallest sha256 (a deterministic, APERIODIC pick: an even spread aliases with a bot that
    trades in a fixed pattern, e.g. buy-sell-sell, and could sample only its buys). All of them when there are at most ``k``."""
    n = len(items)
    if n <= k:
        return list(items)
    out = []
    for j in range(k):
        stratum = items[j * n // k:(j + 1) * n // k]
        if stratum:
            out.append(min(stratum, key=lambda r: hashlib.sha256(str(r['signature']).encode()).digest()))
    return out


def _ints(values, name):
    if values is None:
        return None
    if not isinstance(values, list) or not all(type(v) is int and v >= 0 for v in values):
        raise SignalError(name)
    return values


# ------------------------------------------------------------------------------------------------------- parsing (pure)
def parse_trade(result, signature, mint, pool):
    """A jsonParsed getTransaction body -> a swap view against ``pool``, or None when the node returned no transaction.

    ``side`` is ``buy`` when the pool's token vault FELL and its WSOL vault ROSE, ``sell`` for the reverse, else None (not a swap of this
    pool). ``sol_lamports`` / ``token_raw`` are the pool vault changes; ``spot`` the pool's price right after (post WSOL / post token).
    ``signer`` is the fee payer; ``signer_token_delta`` its change of ``mint`` and ``signer_sol_delta`` its lamports change plus the fee it
    paid plus its WSOL change (so a sell's proceeds are positive). Raises SignalError on a malformed body."""
    if result is None:
        return None
    if not isinstance(result, dict) or type(result.get('slot')) is not int or not isinstance(result.get('meta'), dict):
        raise SignalError('TRANSACTION_MALFORMED')
    transaction = result.get('transaction')
    message = transaction.get('message') if isinstance(transaction, dict) else None
    if not isinstance(message, dict) or not isinstance(message.get('accountKeys'), list):
        raise SignalError('TRANSACTION_MALFORMED')
    block_time = result.get('blockTime')
    if not (block_time is None or type(block_time) is int):
        raise SignalError('TRANSACTION_MALFORMED')
    meta = result['meta']
    keys = [k.get('pubkey') if isinstance(k, dict) else k for k in message['accountKeys']]
    signers = [k.get('pubkey') for k in message['accountKeys'] if isinstance(k, dict) and k.get('signer') is True]
    view = {'signature': signature, 'slot': result['slot'], 'block_time': block_time, 'failed': meta.get('err') is not None,
            'signer': signers[0] if signers else None, 'side': None, 'sol_lamports': 0, 'token_raw': 0, 'spot': None,
            'signer_token_delta': 0, 'signer_sol_delta': 0}
    if view['failed']:
        return view
    token_pre, token_post = _token_amounts(meta.get('preTokenBalances'), mint), _token_amounts(meta.get('postTokenBalances'), mint)
    wsol_pre, wsol_post = _token_amounts(meta.get('preTokenBalances'), SOL_MINT), _token_amounts(meta.get('postTokenBalances'), SOL_MINT)

    def total(balances, owner):
        return sum(a for o, a in balances.values() if o == owner)

    def held(balances, owner):
        return any(o == owner for o, _ in balances.values())
    pool_token = total(token_post, pool) - total(token_pre, pool)
    pool_sol = total(wsol_post, pool) - total(wsol_pre, pool)
    if pool_token < 0 < pool_sol:
        view['side'] = 'buy'
    elif pool_sol < 0 < pool_token:
        view['side'] = 'sell'
    view['sol_lamports'], view['token_raw'] = abs(pool_sol), abs(pool_token)
    if held(token_post, pool) and held(wsol_post, pool) and total(token_post, pool) > 0:
        view['spot'] = Decimal(total(wsol_post, pool)) / Decimal(total(token_post, pool))
    signer = view['signer']
    if signer is not None:
        view['signer_token_delta'] = total(token_post, signer) - total(token_pre, signer)
        lamports = 0
        pre, post = _ints(meta.get('preBalances'), 'BALANCES_MALFORMED'), _ints(meta.get('postBalances'), 'BALANCES_MALFORMED')
        if pre is not None and post is not None and signer in keys:
            index = keys.index(signer)
            if index < len(pre) and index < len(post):
                lamports = post[index] - pre[index]
                fee = meta.get('fee')
                if index == 0 and type(fee) is int and fee >= 0:
                    lamports += fee
        view['signer_sol_delta'] = lamports + total(wsol_post, signer) - total(wsol_pre, signer)
    return view


def is_organic_sell(view, exclude=()):
    """A successful sell INTO the pool by a signer whose token balance went down and whose SOL/WSOL went up, not in ``exclude``."""
    return (view is not None and not view['failed'] and view['side'] == 'sell' and view['signer'] is not None
            and view['signer'] not in set(exclude) and view['signer_token_delta'] < 0 and view['signer_sol_delta'] > 0)


def figures(trades):
    """The sample figures of parsed swaps (oldest first). Pure; a figure that cannot be computed is None with a reason."""
    out, missing = {name: None for name in SAMPLE_FIELDS}, {}
    swaps = [t for t in trades if t['side'] in ('buy', 'sell')]
    buys = [t for t in swaps if t['side'] == 'buy']
    sells = [t for t in swaps if t['side'] == 'sell']
    out.update(buys=len(buys), sells=len(sells), trades=len(swaps))
    if not swaps:
        for name in SAMPLE_FIELDS:
            missing[name] = 'NO_SWAPS_IN_SAMPLE'
        return out, missing
    out['buy_sell_ratio'] = round(len(buys) / max(1, len(sells)), 4)
    out['unique_buyers'] = len({t['signer'] for t in buys if t['signer']})
    out['unique_sellers'] = len({t['signer'] for t in sells if t['signer']})
    sizes = [t['sol_lamports'] for t in swaps]
    if len(sizes) >= 2 and sum(sizes) > 0:
        mean = sum(sizes) / len(sizes)
        sd = math.sqrt(sum((s - mean) ** 2 for s in sizes) / len(sizes))
        out['trade_size_cv'] = round(sd / mean, 4)
    else:
        missing['trade_size_cv'] = 'FEWER_THAN_2_SWAPS'
    prices = [t['spot'] for t in swaps if t['spot'] is not None and t['spot'] > 0]
    moves = [(b > a) for a, b in zip(prices, prices[1:]) if b != a]
    if moves:
        out['monotonic_up_frac'] = round(sum(moves) / len(moves), 4)
    else:
        missing['monotonic_up_frac'] = 'NO_PRICE_CHANGE' if len(prices) >= 2 else 'FEWER_THAN_2_PRICES'
    if prices:
        peak, worst, low, runup = prices[0], Decimal(0), prices[0], Decimal(0)
        for p in prices:
            peak = max(peak, p)
            worst = max(worst, (peak - p) / peak)
            low = min(low, p)
            runup = max(runup, (p - low) / low)
        out['max_drawdown_pre'] = float(round(worst, 6))
        out['max_runup_pct'] = float(round(runup * 100, 4))
    else:
        missing['max_drawdown_pre'] = missing['max_runup_pct'] = 'NO_PRICES'
    # addendum #7: path shape
    if len(prices) >= 2:
        priced = [t for t in swaps if t['spot'] is not None and t['spot'] > 0]
        # per SWAP: a sampled pair's log return over the number of pool transactions between them (1 when not known)
        logs = [math.log(b['spot'] / a['spot']) / max(1, (b.get('position') or 0) - (a.get('position') or 0))
                for a, b in zip(priced, priced[1:])]
        mean = sum(logs) / len(logs)
        out['return_vol'] = round(math.sqrt(sum((x - mean) ** 2 for x in logs) / len(logs)), 8)
        streak = best = 0
        for a, b in zip(prices, prices[1:]):
            streak = streak + 1 if b > a else 0
            best = max(best, streak)
        out['longest_up_streak'] = best
    else:
        missing['return_vol'] = missing['longest_up_streak'] = 'FEWER_THAN_2_PRICES'
    # addendum #1: wash trading
    by_signer = {}
    for t in swaps:
        if t['signer']:
            by_signer.setdefault(t['signer'], set()).add(t['side'])
    counts = {}
    for t in swaps:
        if t['signer']:
            counts[t['signer']] = counts.get(t['signer'], 0) + 1
    if by_signer:
        signed = sum(counts.values())
        out['txs_per_unique_wallet'] = round(signed / len(by_signer), 4)
        out['roundtrip_wallets'] = sum(1 for sides in by_signer.values() if sides == {'buy', 'sell'})
        out['roundtrip_share'] = round(out['roundtrip_wallets'] / len(by_signer), 4)
        out['top5_signer_tx_share'] = round(sum(sorted(counts.values(), reverse=True)[:5]) / signed, 4)
    else:
        for name in ('txs_per_unique_wallet', 'roundtrip_wallets', 'roundtrip_share', 'top5_signer_tx_share'):
            missing[name] = 'NO_SIGNERS'
    return out, missing


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, default=str).encode()


# ------------------------------------------------------------------------------------------------------- the collector
class _Retry(Exception):
    def __init__(self, code):
        self.code = code


class FlowCollector:
    """Writes one ``flow`` row per screen PASS decision. ``flow_pass()`` does a bounded amount of work; ``run_loop`` is its thread.
    Pending decisions come straight from the store (a restart loses nothing; already written decisions are skipped by ``decision_id``)."""

    def __init__(self, *, store, helius, cfg=None, code_version, strategy_version, clock=time.time, sleep=time.sleep, stop=None):
        self.store, self.helius, self.clock, self.sleep = store, helius, clock, sleep
        self.cfg = config(cfg)                    # idempotent on an already validated config
        self.code_version, self.strategy_version = code_version, strategy_version
        self.stop = stop or threading.Event()
        self.budget = HourBudget(self.cfg['max_calls_per_hour'], clock)
        self._cursor = None                       # every PASS decision id <= this one has its row (or expired)
        self._done = set()                        # decided above the cursor
        self._attempts = {}                       # decision id -> (count, not_before)
        self.counts = {'rows': 0, 'partial': 0, 'calls': 0, 'retries': 0, 'stopped': 0, 'errors': 0, 'budget_stops': 0, 'shed_stops': 0}
        self.last_pending = None
        self.prefetch = None                      # W2b #8: lean.prefetch.Prefetch (set by build_runner when configured)

    def health(self):
        return {'enabled': True, **self.counts, 'pending': self.last_pending, 'calls_last_hour': self.budget.used(),
                'max_calls_per_hour': self.cfg['max_calls_per_hour']}

    def _query(self, sql, args=()):
        with self.store._lock:
            return self.store.db.execute(sql, args).fetchall()

    def _load_cursor(self):
        """The highest decision id that already has a row (restart): from the stored rows' ``decision_id``."""
        best, after = 0, 0
        while True:
            rows = self.store.rows_after('observations', after, kind=KIND, limit=1000)
            for row in rows:
                after = row['id']
                try:
                    best = max(best, int(json.loads(row['meta']).get('decision_id') or 0))
                except (ValueError, TypeError, AttributeError):
                    continue
            if len(rows) < 1000:
                return best

    def pending(self, now, limit=200):
        """[(decision_id, decision_ts, mint, candidate_id, pool, migrated_at)]: screen PASS decisions above the cursor, younger than
        ``max_age_s``, oldest first."""
        if self._cursor is None:
            self._cursor = self._load_cursor()
        rows = self._query("SELECT d.id, d.ts, d.mint, d.candidate_id, c.pool, c.migrated_at FROM decisions d JOIN candidates c ON c.id = d.candidate_id "
                           "WHERE d.kind='screen' AND d.action='PASS' AND d.id>? AND d.ts>=? AND c.pool IS NOT NULL ORDER BY d.id LIMIT ?",
                           (self._cursor, now - self.cfg['max_age_s'], int(limit)))
        return [r for r in rows if r[0] not in self._done]

    def flow_pass(self):
        now = self.clock()
        rows = self.pending(now)
        self.last_pending = len(rows)
        done, contiguous = 0, True
        for decision_id, decision_ts, mint, cid, pool, migrated_at in rows:
            if self.stop.is_set() or done >= self.cfg['max_per_pass']:
                break
            attempts, not_before = self._attempts.get(decision_id, (0, 0.0))
            finished = False
            if now >= not_before:
                done += 1
                try:
                    self.collect(decision_id, decision_ts, mint, cid, pool, migrated_at)
                    finished = True
                except _Retry as retry:
                    if retry.code == 'STOPPED':
                        self.counts['stopped'] += 1
                        break
                    self.counts['retries'] += 1
                    self._attempts[decision_id] = (attempts + 1, now + self.cfg['retry_delay_s'])
                    if attempts + 1 > self.cfg['max_retries']:
                        self._write(decision_id, decision_ts, mint, cid, pool, {'unavailable': {'window': 'RETRIES_EXHAUSTED:' + retry.code}})
                        finished = True
                except Exception as error:                 # recorded; a collector failure never reaches entries or exits
                    self.counts['errors'] += 1
                    log.debug('flow collection failed', exc_info=True)
                    try:
                        self._write(decision_id, decision_ts, mint, cid, pool, {'unavailable': {'window': 'COLLECT_FAILED:' + type(error).__name__}})
                        finished = True
                    except Exception:
                        pass
            if finished:
                self._attempts.pop(decision_id, None)
                if contiguous:
                    self._cursor = decision_id
                else:
                    self._done.add(decision_id)
            else:
                contiguous = False
        self._done = {d for d in self._done if d > (self._cursor or 0)}
        return done

    def run_loop(self):
        while not self.stop.is_set():
            try:
                self.flow_pass()
            except Exception:
                self.counts['errors'] += 1
            self.stop.wait(self.cfg['pass_interval_s'])

    # -- one decision ------------------------------------------------------------------------------------------------------
    def collect(self, decision_id, decision_ts, mint, cid, pool, migrated_at=None, sink=None):
        """Collect and write the row of one PASS decision; raises _Retry when nothing was learned and it should be tried later.

        W2b #8: with a ``sink`` (a dict) this is a PREFETCH: ``decision_ts`` is the AS-OF time the window ends at (never later than the
        decision it will be committed to), nothing is written, the row body is left in ``sink['body']`` with ``sink['finished_at']``,
        and ``sink['patience_s']`` (optional) replaces ``shed_wait_s``. Without a sink, a finished prefetch of this decision is committed
        instead of collecting (no request)."""
        # --- W2b hook ---
        if sink is None and self.prefetch is not None:
            committed = self.prefetch.commit_flow(self, decision_id, decision_ts, mint, cid, pool)
            if committed is not None:
                return committed
        # --- end W2b ---
        cfg, started = self.cfg, self.clock()
        calls = LowCalls(self.helius, limit=cfg['sig_pages'] + cfg['max_txs'], budget=self.budget, wait=self.sleep, pace_s=cfg['pace_s'],
                         patience_s=cfg['shed_wait_s'] if sink is None or sink.get('patience_s') is None else sink['patience_s'],
                         stop=self.stop, reserve=cfg['reserve_tokens'])
        unavailable, flags, notes = {}, {}, {}
        window_start = decision_ts - cfg['window_s']
        if isinstance(migrated_at, (int, float)) and not isinstance(migrated_at, bool) and migrated_at > window_start:
            window_start = float(migrated_at)
            notes['window_clipped_at_graduation'] = True
        end_s = math.floor(decision_ts)          # whole block-time seconds: [start_s, end_s), every one ended by the decision
        cut = end_s - 1
        start_s = math.ceil(window_start)
        seen, exhausted, reached_start = [], False, False
        learned = False
        out = {'tx_rate_per_s': None}
        try:
            before = None
            for page in range(cfg['sig_pages']):
                options = {'limit': cfg['sig_limit'], 'commitment': 'confirmed'}
                if before:
                    options['before'] = before
                try:
                    rows = parse_signatures(calls.rpc('getSignaturesForAddress', [pool, options]))
                except ProviderError as error:
                    if not learned:
                        raise _Retry(error.code) from None
                    unavailable['signatures'] = 'PAGE_%d:%s' % (page, error.code)
                    break
                learned = True
                seen.extend(rows)
                if len(rows) < cfg['sig_limit']:
                    exhausted = True
                    break
                times = [r['block_time'] for r in rows if r['block_time'] is not None]
                if times and min(times) < window_start:
                    reached_start = True
                    break
                before = rows[-1]['signature']
            times = [r['block_time'] for r in seen if r['block_time'] is not None]
            pre = [r for r in seen if r['block_time'] is not None and r['block_time'] <= cut]
            notes.update(signatures_seen=len(seen), unknown_time_skipped=len(seen) - len(times))
            if not pre and not exhausted:
                unavailable['window'] = 'DECISION_NOT_REACHED'
            else:
                if reached_start or (exhausted and (not times or min(times) <= start_s)):
                    covered = start_s
                elif exhausted:                       # the whole history is younger than the window: its first second is complete
                    covered = max(start_s, min(times))
                    notes['pool_younger_than_window'] = True
                else:                                 # the pages ran out: the oldest second seen may be partial, so it is dropped
                    covered = max(start_s, min(times) + 1)
                    flags['tx_rate_per_s'] = 'TRUNCATED_SPAN'
                window = [r for r in reversed(pre) if r['block_time'] >= covered and not r['failed']]     # oldest first
                notes.update(window_txs=len(window), failed_txs=sum(1 for r in pre if r['failed'] and r['block_time'] >= covered),
                             window_start=window_start, covered_from=covered, window_end=end_s, window_s_effective=float(end_s - covered))
                span = end_s - covered
                if span >= cfg['min_span_s']:
                    out['tx_rate_per_s'] = round(len(window) / span, 4)
                else:
                    unavailable['tx_rate_per_s'] = 'SPAN_TOO_SHORT'
                trades, skipped = [], 0
                sample = stratified(window, cfg['max_txs'])
                notes['sampled'] = len(sample)
                position = {r['signature']: i for i, r in enumerate(window)}
                for entry in sample:
                    try:
                        view = parse_trade(calls.rpc('getTransaction', [entry['signature'], {'encoding': 'jsonParsed', 'commitment': 'confirmed',
                                                                                           'maxSupportedTransactionVersion': MAX_TX_VERSION}]),
                                           entry['signature'], mint, pool)
                    except (ProviderError, SignalError):
                        view = None
                    if view is not None and (view['block_time'] is None or view['block_time'] > cut):
                        view = None                      # defensive: the body disagrees with its signature's time
                    if view is None or view['failed'] or view['side'] is None:
                        skipped += 1
                        continue
                    view['position'] = position[entry['signature']]          # its place among the window's transactions
                    trades.append(view)
                notes['sample_skipped'] = skipped
                sample_out, sample_missing = figures(trades)
                out.update(sample_out)
                unavailable.update(sample_missing)
                for name in SAMPLE_FIELDS:
                    if out.get(name) is not None:
                        flags[name] = 'SAMPLE'
                if len(sample) < len(window):
                    notes['sample_of'] = len(window)
        except Stop as stop:
            if stop.code != 'STOPPED':
                self.counts['budget_stops' if stop.code == 'HOURLY_BUDGET' else 'shed_stops'] += 1
            if stop.code == 'STOPPED' or not learned:          # stopping, or nothing learned yet: no row now, tried again later
                raise _Retry(stop.code) from None
            unavailable['stopped'] = stop.code
        except SignalError as error:
            unavailable['window'] = error.code
        finally:
            self.counts['calls'] += calls.used
        for name in FEATURES:
            if out.get(name) is None and name not in unavailable:
                unavailable[name] = unavailable.get('window') or unavailable.get('stopped') or 'UNKNOWN'
        body = {**notes, **out, 'unavailable': unavailable, 'flags': flags, 'calls': calls.used, 'started_at': started}
        # --- W2b hook: a prefetch keeps the body (the commit writes it, stamped with the real decision) ---
        if sink is not None:
            sink.update(body=body, finished_at=self.clock(), window_end_ts=float(end_s))
            return body
        # --- end W2b ---
        return self._write(decision_id, decision_ts, mint, cid, pool, body)

    def _write(self, decision_id, decision_ts, mint, cid, pool, body):
        now = self.clock()
        collected = body.pop('started_at', now)
        unavailable = body.pop('unavailable', {})
        flags = body.pop('flags', {})
        row = {name: None for name in FEATURES}
        row.update(body)
        for name in FEATURES:
            if row.get(name) is None:
                unavailable.setdefault(name, unavailable.get('window') or 'UNKNOWN')
        row.update({'version': VERSION, 'mint': mint, 'pool': pool, 'decision_id': decision_id, 'decision_ts': decision_ts,
                    'collected_at': collected, 'late_s': round(collected - decision_ts, 3), 'window_s': self.cfg['window_s'],
                    'window_after_decision_s': 0.0, 'unavailable': unavailable,
                    'flags': {k: v for k, v in flags.items() if row.get(k) is not None}, 'partial': bool(unavailable)})
        row.setdefault('calls', 0)
        meta = {k: row.get(k) for k in ('version', 'decision_id', 'decision_ts', 'collected_at', 'late_s', 'calls', 'partial', 'window_txs',
                                        'sampled', 'trades') + FEATURES}
        self.store.add_observation(KIND, _canonical(row), mint=mint, candidate_id=cid, meta=meta, code_version=self.code_version,
                                   strategy_version=self.strategy_version, ts=now)
        self.counts['rows'] += 1
        self.counts['partial'] += 1 if row['partial'] else 0
        return row


def build(cfg, *, store, keys, code_version, strategy_version, clock=time.time, transport_kwargs=None):
    """The collector of a validated ``flow`` config, on the shared LOW lane; None when disabled."""
    if not cfg['enabled']:
        return None
    from lean import providers
    transport_kwargs = dict(transport_kwargs or {})
    helius = providers.low(keys, **transport_kwargs).helius
    return FlowCollector(store=store, helius=helius, cfg=cfg, code_version=code_version, strategy_version=strategy_version, clock=clock,
                         sleep=transport_kwargs.get('sleep', time.sleep))


def flow_rows(store):
    """Stored ``flow`` rows as dicts, oldest first."""
    out = []
    for row in store.rows('observations', kind=KIND, limit=100000):
        try:
            out.append(json.loads(row['raw']))
        except ValueError:
            continue
    return out
