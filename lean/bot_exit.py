"""BOT-EXIT detection on held positions (L23). PAPER ONLY: nothing here signs, builds or sends a transaction.

Why: most realized losses are gap dumps (about entry -> -80 % between two 10 s marks, the stop cannot fill) and most winners ride
bot-pumped staircases. Hypothesis: the dump is preceded by observable on-chain signals (the bot's tx rate falls, the sell share rises,
large holders / the pumping wallets / the creator start selling, buy sizes shrink). This module watches every held position's pool
for those signals and, in SHADOW mode (the default), only RECORDS what it would have done.

Monitor (its OWN daemon thread, every ``poll_s``; never the exit thread, never a lock held across a request). Per held position, on the
shared non-blocking LOW lane (``lean.providers.low``: a call goes out at once or fails ``LANE_SHED`` and sends nothing):
  1. ``getSignaturesForAddress(pool, {limit: sig_limit, until: last seen})``: every pool transaction (count + block time). The tx
     rate needs no parsing, so it is exact while a page is not full; a full page leaves a gap, and a window that overlaps a gap (or
     starts before the first page) yields NO rate metric (never a false "the bot stopped").
  2. ``getTransaction`` (jsonParsed) of a bounded SAMPLE (``sample_txs`` per position per pass, spread over the new signatures, the
     newest included): side, SOL size, trader and the pool's quote reserve before/after, from the pool's own token balances.
  3. every ``holders_every`` passes, ``getTokenLargestAccounts(mint)``: the outflow of the top non-pool holders since the last check.
  At most ``max_calls_per_pass`` Helius calls per pass over ALL positions (round-robin start), paced ``pace_s`` apart on this thread,
  and only while ``reserve_tokens`` low-lane tokens stay for the other low-lane users (path recorder, L11 probes, L12F, L13). A shed
  (or a yield) ends the pass at once: nothing waits, the next pass starts over.

Metrics (``window_s`` = the "_30s" window, default 30 s; ``baseline_s`` = the window BEFORE it, default 180 s):
  tx_rate_30s, tx_rate_baseline, tx_rate_ratio, failed_share_30s       from the signatures (exact, gap-aware)
  sell_share_30s, net_sol_flow_30s, max_sell_reserve_frac, buy_size_ratio, pumper_net_sol_30s, pumper_sellers_30s, creator_sold,
  swaps_sampled_30s                                                    from the sampled swaps
  top_holders_sold_frac                                                from the holder checks
``pumpers`` = wallets with a sampled BUY in the first ``pumper_window_s`` after the entry. ``creator`` = the PumpSwap pool's
``coin_creator`` read from the pool account the screen retained (no request). A metric without enough evidence is None, and a
condition on a None metric is false.

Signal: ``rules`` (data-driven, from config) is a list of ``{"name", "all": [{"metric", "op", "value"}, ...]}``; a rule fires when all
of its conditions hold, a signal when any rule fires. One signal per position lifecycle ``(mint, open_fill_id)``, sticky.

SHADOW (``mode: "shadow"``): ``decisions(kind='shadow_exit', action='SIGNAL')`` with the metrics, the rules that fired, the quantity
held and the HYPOTHETICAL exit value (net of the paper slippage and fee, like a paper sell): a fresh D1 quote-mark when one exists,
else ONE Jupiter sell quote for the whole quantity on the low lane, else a fresh vault mark. Trading is NOT changed.
LIVE (``mode: "live"``): ``decisions(kind='bot_exit', action='SIGNAL')`` and the runner's ``_manage`` wants the position out with
reason ``BOT_EXIT`` through the ordinary (two-phase, L10) exit path on its next pass. Implemented and tested, OFF by default.

Off unless ``bot_exit.enabled`` is true (absent key = off). ``report_section`` compares each shadow signal's value with what the
position really fetched afterwards ("would it have saved money?").
"""
import json
import logging
import threading
import time
from collections import deque
from decimal import Decimal, InvalidOperation

from lean import adapters as A, providers
from lean.providers import ProviderError

log = logging.getLogger('lean.bot_exit')

SHADOW_KIND = 'shadow_exit'
LIVE_KIND = 'bot_exit'
EXIT_REASON = 'BOT_EXIT'
SOL_MINT = A.SOL_MINT
LAMPORTS = 10 ** 9
MODES = ('shadow', 'live')
OPS = {'<': lambda a, b: a < b, '<=': lambda a, b: a <= b, '>': lambda a, b: a > b, '>=': lambda a, b: a >= b}
METRICS = ('tx_rate_30s', 'tx_rate_baseline', 'tx_rate_ratio', 'failed_share_30s', 'sell_share_30s', 'net_sol_flow_30s',
           'max_sell_reserve_frac', 'buy_size_ratio', 'pumper_net_sol_30s', 'pumper_sellers_30s', 'creator_sold',
           'swaps_sampled_30s', 'top_holders_sold_frac')
DEFAULT_RULES = (
    {'name': 'BOT_STOPPED', 'all': [{'metric': 'tx_rate_ratio', 'op': '<', 'value': '0.3'},
                                    {'metric': 'sell_share_30s', 'op': '>', 'value': '0.6'}]},
    {'name': 'WHALE_SELL', 'all': [{'metric': 'max_sell_reserve_frac', 'op': '>', 'value': '0.15'}]},
    {'name': 'CREATOR_SELL', 'all': [{'metric': 'creator_sold', 'op': '>=', 'value': '1'}]},
    {'name': 'PUMPERS_OUT', 'all': [{'metric': 'pumper_net_sol_30s', 'op': '<', 'value': '0'},
                                    {'metric': 'pumper_sellers_30s', 'op': '>=', 'value': '2'},
                                    {'metric': 'tx_rate_ratio', 'op': '<', 'value': '0.6'}]},
)
DEFAULTS = {
    'enabled': False,
    'mode': 'shadow',
    'poll_s': 10,
    'window_s': 30,
    'baseline_s': 180,
    'min_baseline_tx': 30,          # a quiet pool has no "bot rate" to lose: the rate metric needs this many baseline txs
    'min_sample': 4,                # sampled swaps in the window needed for sell_share_30s
    'pumper_window_s': 120,
    'sig_limit': 300,
    'sample_txs': 2,
    'holders_every': 3,
    'max_calls_per_pass': 6,
    'reserve_tokens': 1,
    'pace_s': 0.5,
    'quote_ttl_s': 10,
    'rules': None,                  # None = DEFAULT_RULES
}
INT_KEYS = {'poll_s': (1, 3600), 'window_s': (5, 600), 'baseline_s': (10, 3600), 'min_baseline_tx': (1, 100000), 'min_sample': (1, 1000),
            'pumper_window_s': (1, 3600), 'sig_limit': (10, 1000), 'sample_txs': (0, 20), 'holders_every': (0, 1000),
            'max_calls_per_pass': (1, 100), 'reserve_tokens': (0, 10), 'quote_ttl_s': (1, 600)}


class BotExitConfigError(ValueError):
    pass


class BotExitError(ValueError):
    """A malformed provider body (isolated: counted, the position is skipped this pass)."""


# ------------------------------------------------------------------------------------------------------------------- config
def _decimal(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
        raise BotExitConfigError('%s must be a number (int or decimal string)' % name)
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise BotExitConfigError('%s must be a number' % name) from None
    if not number.is_finite():
        raise BotExitConfigError('%s must be finite' % name)
    return number


def _rules(raw):
    rules = DEFAULT_RULES if raw is None else raw
    if not isinstance(rules, (list, tuple)) or not 1 <= len(rules) <= 32:
        raise BotExitConfigError('rules must be a list of 1 to 32 rules')
    out, names = [], set()
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict) or set(rule) - {'name', 'all', '_comment'} or not isinstance(rule.get('name'), str) \
                or not rule['name'] or len(rule['name']) > 40 or rule['name'] in names:
            raise BotExitConfigError('rules[%d] needs a unique name (<= 40 chars) and "all"' % i)
        conditions = rule.get('all')
        if not isinstance(conditions, list) or not 1 <= len(conditions) <= 8:
            raise BotExitConfigError('rules[%d].all must list 1 to 8 conditions' % i)
        checked = []
        for j, c in enumerate(conditions):
            if not isinstance(c, dict) or set(c) != {'metric', 'op', 'value'}:
                raise BotExitConfigError('rules[%d].all[%d] needs exactly metric, op, value' % (i, j))
            if c['metric'] not in METRICS:
                raise BotExitConfigError('rules[%d].all[%d]: unknown metric %r' % (i, j, c['metric']))
            if c['op'] not in OPS:
                raise BotExitConfigError('rules[%d].all[%d]: op must be one of %s' % (i, j, sorted(OPS)))
            checked.append((c['metric'], c['op'], _decimal(c['value'], 'rules[%d].all[%d].value' % (i, j))))
        names.add(rule['name'])
        out.append((rule['name'], tuple(checked)))
    return tuple(out)


class Config:
    def __init__(self, **values):
        unknown = set(values) - set(DEFAULTS) - {'_comment'}
        if unknown:
            raise BotExitConfigError('unknown bot_exit keys: %s' % sorted(unknown))
        merged = {**DEFAULTS, **{k: v for k, v in values.items() if k != '_comment'}}
        if type(merged['enabled']) is not bool:
            raise BotExitConfigError('enabled must be true or false')
        if merged['mode'] not in MODES:
            raise BotExitConfigError('mode must be "shadow" or "live"')
        for name, (lo, hi) in INT_KEYS.items():
            if type(merged[name]) is not int or not lo <= merged[name] <= hi:
                raise BotExitConfigError('%s must be an integer from %d to %d' % (name, lo, hi))
        pace = merged['pace_s']
        if isinstance(pace, bool) or not isinstance(pace, (int, float)) or not 0 <= pace <= 5:
            raise BotExitConfigError('pace_s must be a number from 0 to 5')
        if merged['sample_txs'] > merged['max_calls_per_pass']:
            raise BotExitConfigError('sample_txs must be <= max_calls_per_pass')
        self.rules = _rules(merged['rules'])
        for name in DEFAULTS:
            if name != 'rules':
                setattr(self, name, merged[name])
        self.pace_s = float(pace)
        self.raw_rules = merged['rules']

    @classmethod
    def from_dict(cls, value):
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise BotExitConfigError('bot_exit must be an object')
        return cls(**value)


# ------------------------------------------------------------------------------------------------------------- parsing (pure)
def _is_int(value):
    return type(value) is int


def parse_signatures(result):
    """getSignaturesForAddress -> [(signature, block_time|None, failed)] newest first (the node's order)."""
    if not isinstance(result, list):
        raise BotExitError('SIGNATURES_MALFORMED')
    out = []
    for entry in result:
        if not isinstance(entry, dict) or not isinstance(entry.get('signature'), str) or not 32 <= len(entry['signature']) <= 100:
            raise BotExitError('SIGNATURES_MALFORMED')
        block_time = entry.get('blockTime')
        if not (block_time is None or _is_int(block_time)):
            raise BotExitError('SIGNATURES_MALFORMED')
        out.append((entry['signature'], block_time, entry.get('err') is not None))
    return out


def _balances(items, pool, mint):
    """{owner: {mint: raw}} of one pre/postTokenBalances list (only ``mint`` and wSOL)."""
    if items is None:
        return {}
    if not isinstance(items, list):
        raise BotExitError('TOKEN_BALANCES_MALFORMED')
    out = {}
    for item in items:
        if not isinstance(item, dict) or item.get('mint') not in (mint, SOL_MINT):
            continue
        owner, ui = item.get('owner'), item.get('uiTokenAmount')
        amount = ui.get('amount') if isinstance(ui, dict) else None
        if not isinstance(owner, str) or not isinstance(amount, str) or not amount.isdecimal():
            raise BotExitError('TOKEN_BALANCES_MALFORMED')
        held = out.setdefault(owner, {})
        held[item['mint']] = held.get(item['mint'], 0) + int(amount)
    return out


def parse_swap(result, mint, pool):
    """A jsonParsed getTransaction body -> ``{'t', 'side', 'sol', 'trader', 'reserve_pre', 'reserve_post', 'tokens'}`` read from
    the POOL's own balances (its token vault and its wSOL vault are owned by the pool account), or None when it is not a swap of
    this pool (or failed, or the node returned nothing). ``sol`` is lamports in/out of the pool's wSOL vault."""
    if result is None:
        return None
    if not isinstance(result, dict) or not isinstance(result.get('meta'), dict):
        raise BotExitError('TRANSACTION_MALFORMED')
    meta = result['meta']
    if meta.get('err') is not None:
        return None
    block_time = result.get('blockTime')
    if not (block_time is None or _is_int(block_time)):
        raise BotExitError('TRANSACTION_MALFORMED')
    pre, post = _balances(meta.get('preTokenBalances'), pool, mint), _balances(meta.get('postTokenBalances'), pool, mint)
    p0, p1 = pre.get(pool, {}), post.get(pool, {})
    base_delta = p1.get(mint, 0) - p0.get(mint, 0)
    quote_pre, quote_post = p0.get(SOL_MINT), p1.get(SOL_MINT)
    if base_delta == 0 or quote_pre is None or quote_post is None or quote_post == quote_pre:
        return None
    side = 'buy' if base_delta < 0 else 'sell'
    if (side == 'buy') != (quote_post > quote_pre):
        return None                                            # tokens and SOL moved the same way: not a plain swap
    trader, best = None, 0
    for owner in set(pre) | set(post):
        if owner == pool:
            continue
        delta = abs(post.get(owner, {}).get(mint, 0) - pre.get(owner, {}).get(mint, 0))
        if delta > best:
            trader, best = owner, delta
    if trader is None:
        message = (result.get('transaction') or {}).get('message') if isinstance(result.get('transaction'), dict) else None
        keys = message.get('accountKeys') if isinstance(message, dict) else None
        signers = [k.get('pubkey') for k in keys or [] if isinstance(k, dict) and k.get('signer') is True]
        trader = signers[0] if signers else None
    return {'t': block_time, 'side': side, 'sol': abs(quote_post - quote_pre), 'trader': trader,
            'reserve_pre': quote_pre, 'reserve_post': quote_post, 'tokens': abs(base_delta)}


def parse_largest(result, exclude):
    """getTokenLargestAccounts -> {token account: raw amount} without the pool vault."""
    value = result.get('value') if isinstance(result, dict) else None
    if not isinstance(value, list):
        raise BotExitError('LARGEST_MALFORMED')
    out = {}
    for item in value:
        amount = item.get('amount') if isinstance(item, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get('address'), str) or not isinstance(amount, str) or not amount.isdecimal():
            raise BotExitError('LARGEST_MALFORMED')
        if item['address'] not in exclude:
            out[item['address']] = int(amount)
    return out


def holders_outflow(previous, current):
    """Fraction of the previous top holders' balance that left them (an account that dropped out of the list is assumed to hold at
    most the smallest balance still listed: a lower bound on its outflow). None without a previous check."""
    if not previous:
        return None
    floor = min(current.values()) if current else 0
    before = sum(previous.values())
    if before <= 0:
        return None
    out = 0
    for account, amount in previous.items():
        now = current.get(account, min(floor, amount))
        out += max(0, amount - now)
    return Decimal(out) / Decimal(before)


def net_lamports(gross_lamports, pcfg):
    """A paper sell of ``gross`` lamports: the slippage haircut, less the fixed fee (lean.paper's arithmetic). Never negative."""
    from lean import paper
    return max(0, int(gross_lamports) * (paper.MAX_BPS - pcfg.slippage_bps) // paper.MAX_BPS - pcfg.fee_lamports)


def evaluate(rules, metrics):
    """Names of the rules whose every condition holds (a None metric never holds)."""
    fired = []
    for name, conditions in rules:
        ok = True
        for metric, op, value in conditions:
            got = metrics.get(metric)
            if got is None or not OPS[op](Decimal(str(got)), value):
                ok = False
                break
        if ok:
            fired.append(name)
    return fired


# ------------------------------------------------------------------------------------------------------------- per lifecycle
class Watch:
    """What one position lifecycle's pool has shown so far."""

    def __init__(self, mint, open_id, pool, base_vault, entered_at, creator):
        self.mint, self.open_id, self.pool, self.base_vault = mint, open_id, pool, base_vault
        self.entered_at, self.creator = entered_at, creator
        self.last_sig = None
        self.sigs = deque()                   # (t, failed) oldest first
        self.swaps = deque()                  # parsed sampled swaps, oldest first (only NEW signatures are ever sampled: ``until``)
        self.covered_since = None             # the oldest time the signature record is complete from
        self.gaps = []                        # [(lo, hi)]: a full page left these intervals incomplete
        self.pumpers = set()
        self.creator_sold = False
        self.holders = None                   # {token account: amount} at the last check
        self.holders_frac = None
        self.passes = 0
        self.polled_at = None
        self.signaled = False
        self.last_metrics = None

    def complete(self, lo, hi):
        if self.covered_since is None or self.covered_since > lo:
            return False
        return not any(g_lo < hi and g_hi > lo for g_lo, g_hi in self.gaps)

    def trim(self, keep_from):
        while self.sigs and self.sigs[0][0] < keep_from:
            self.sigs.popleft()
        while self.swaps and (self.swaps[0]['t'] or 0) < keep_from:
            self.swaps.popleft()
        self.gaps = [g for g in self.gaps if g[1] > keep_from]


# ------------------------------------------------------------------------------------------------------------------ monitor
class BotExit:
    def __init__(self, cfg, *, store, low, code_version, strategy_version, clock=time.time, sleep=time.sleep):
        self.cfg, self.store, self.low = cfg, store, low
        self.code_version, self.strategy_version = code_version, strategy_version
        self.clock, self.sleep = clock, sleep
        self.stop = None
        self.watches = {}                     # (mint, open_id) -> Watch      (this thread only)
        self._lock = threading.Lock()         # guards _live and counts only; never held across I/O
        self._live = {}                       # (mint, open_id) -> 'BOT_EXIT:<rules>' (live mode triggers)
        self._signaled = set()                # lifecycles that already signalled (replayed on start)
        self._rr = 0
        self.counts = {'passes': 0, 'polls': 0, 'calls': 0, 'sheds': 0, 'yields': 0, 'signals': 0, 'shadow_signals': 0,
                       'live_signals': 0, 'errors': 0, 'quotes': 0, 'quote_sheds': 0, 'swaps_parsed': 0, 'tx_not_ready': 0}
        self.tx_errors = {}
        self._replay()

    # -- persistence ------------------------------------------------------------------------------------------------------------
    def _replay(self):
        """Signals already recorded (shadow or live) are not recorded again; a LIVE signal of an open lifecycle stays an exit."""
        for kind in (SHADOW_KIND, LIVE_KIND):
            for row in self.store.rows('decisions', kind=kind, limit=100000):
                try:
                    features = json.loads(row['features'])
                    key = (row['mint'], int(features['open_fill_id']))
                except (ValueError, KeyError, TypeError):
                    continue
                self._signaled.add(key)
                if kind == LIVE_KIND and self.cfg.mode == 'live':
                    self._live[key] = '%s:%s' % (EXIT_REASON, ','.join(json.loads(row['reasons']) or []))

    # -- the runner's view (exit thread: a dict read under a tiny lock, no I/O) -------------------------------------------------
    def trigger(self, mint, open_fill_id):
        """'BOT_EXIT:<rules>' when LIVE mode wants this lifecycle out, else None. Never raises."""
        if self.cfg.mode != 'live':
            return None
        try:
            with self._lock:
                return self._live.get((mint, int(open_fill_id)))
        except (TypeError, ValueError):
            return None

    def _count(self, name, n=1):
        with self._lock:
            self.counts[name] += n

    def health(self):
        with self._lock:
            counts = dict(self.counts)
            live = len(self._live)
            tx_errors = dict(self.tx_errors)
        return {'enabled': True, 'mode': self.cfg.mode, 'watching': len(self.watches), 'live_triggers': live, 'tx_errors': tx_errors, **counts}

    # -- thread ---------------------------------------------------------------------------------------------------------------------
    def run_loop(self, runner):
        """Own daemon thread: one pass every ``poll_s`` until ``stop``. A failing pass is counted and the loop goes on."""
        stop = self.stop = self.stop or runner.stop
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.poll_pass(runner)
            except Exception:                                     # the trader never notices
                self._count('errors')
                log.debug('bot_exit pass failed', exc_info=True)
            stop.wait(max(0.0, self.cfg.poll_s - (time.monotonic() - started)))

    def _stopped(self, runner):
        return (self.stop is not None and self.stop.is_set()) or runner.stop.is_set()

    # -- one pass -------------------------------------------------------------------------------------------------------------
    def poll_pass(self, runner):
        """Signatures for every held position (round-robin), then the samples, then the holder checks, all within the call budget;
        then the rules. Returns the number of calls made."""
        self._count('passes')
        now = self.clock()
        positions, states = self.store.positions(), self.store.position_states()
        live_keys = set()
        for mint, position in positions.items():
            row = states.get(mint)
            if row is None:
                continue
            key = (mint, int(row['open_fill_id']))
            live_keys.add(key)
            if key not in self.watches:
                state = row['state']
                self.watches[key] = Watch(mint, key[1], state['pool'], state.get('base_vault'), float(position.opened_at),
                                          self._creator(mint))
        for key in [k for k in self.watches if k not in live_keys]:          # closed: forget
            del self.watches[key]
        with self._lock:
            for key in [k for k in self._live if k not in live_keys]:
                del self._live[key]
        order = sorted(self.watches)
        if not order:
            return 0
        self._rr = (self._rr + 1) % len(order)
        order = order[self._rr:] + order[:self._rr]
        budget = [self.cfg.max_calls_per_pass]
        picks, polled = {}, []
        try:
            for key in order:                                     # 1. signatures: the cheap, exact tx count
                if self._stopped(runner):
                    break
                fresh = self._signatures(runner, self.watches[key], budget, now)
                if fresh is not None:                             # None: that position's read failed (counted); no stale verdict
                    picks[key] = fresh
                    polled.append(key)
            for key in sorted(polled, key=self._priority):        # 2. sampled swaps: falling tx rates first
                for signature in picks.get(key) or ():
                    if self._stopped(runner):
                        break
                    self._sample(runner, self.watches[key], signature, budget)
            for key in polled:                                    # 3. holder checks
                watch = self.watches[key]
                if self.cfg.holders_every and watch.passes % self.cfg.holders_every == 1 % self.cfg.holders_every:
                    if self._stopped(runner):
                        break
                    self._holders(runner, watch, budget)
        except _Skip:
            pass                                                  # shed / yield / budget: the rest waits for the next pass
        for key in polled:
            watch = self.watches[key]
            if watch.polled_at is None:
                continue
            now = watch.polled_at                                 # the windows end where the signature record ends
            watch.trim(now - self.cfg.window_s - self.cfg.baseline_s - 5)
            metrics = self.metrics(watch, now)
            watch.last_metrics = metrics
            fired = evaluate(self.cfg.rules, metrics)
            if fired and key not in self._signaled and not self._stopped(runner):
                self._signal(runner, watch, positions.get(key[0]), fired, metrics, now)
        return self.cfg.max_calls_per_pass - budget[0]

    def _priority(self, key):
        """Sampling order when the budget is short (several positions): the lowest tx-rate ratio first (a bot that just slowed
        down is where the sell share matters), unknown ratios last."""
        watch = self.watches[key]
        ratio = self.metrics(watch, watch.polled_at)['tx_rate_ratio'] if watch.polled_at is not None else None
        return (ratio is None, ratio if ratio is not None else Decimal(0), key)

    # -- requests (low lane only) ---------------------------------------------------------------------------------------------------
    def _call(self, runner, budget, method, params):
        """One Helius call: within the pass budget, paced, never when it would leave fewer than ``reserve_tokens`` low-lane tokens.
        LANE_SHED (nothing sent) ends the pass. Any other provider error propagates to the caller (that position only)."""
        if budget[0] <= 0:
            raise _Skip()
        if self.cfg.max_calls_per_pass - budget[0] > 0 and self.cfg.pace_s:
            self._pace()
        if self._stopped(runner):
            raise _Skip()
        limiter = getattr(getattr(self.low.helius, 'transport', None), 'limiter', None)
        available = getattr(limiter, 'available', None)
        if self.cfg.reserve_tokens and callable(available) and available() < 1 + self.cfg.reserve_tokens:
            self._count('sheds')                                   # the lane cannot spare a token now (or is shed): skip
            self._count('yields')
            raise _Skip()
        try:
            result, _raw, _meta = self.low.helius.rpc(method, params)
        except ProviderError as error:
            if providers.is_shed(error):
                self._count('sheds')
                raise _Skip() from None
            budget[0] -= 1
            self._count('calls')
            raise
        budget[0] -= 1
        self._count('calls')
        return result

    def _pace(self):
        if self.stop is not None and self.sleep is time.sleep:
            self.stop.wait(self.cfg.pace_s)
        else:
            self.sleep(self.cfg.pace_s)

    def _error(self, runner, code, mint, message=''):
        self._count('errors')
        try:
            runner._error(code, transient=True, mint=mint, scope='bot_exit', message=message)
        except Exception:
            pass

    def _signatures(self, runner, watch, budget, now):
        """New pool signatures since the last one seen; returns the signatures to sample (newest first), or None on a failure."""
        options = {'limit': self.cfg.sig_limit, 'commitment': 'confirmed'}
        if watch.last_sig:
            options['until'] = watch.last_sig
        try:
            entries = parse_signatures(self._call(runner, budget, 'getSignaturesForAddress', [watch.pool, options]))
        except _Skip:
            raise
        except (ProviderError, BotExitError) as error:
            self._error(runner, getattr(error, 'code', str(error)), watch.mint, 'signatures')
            return None
        watch.passes += 1
        watch.polled_at = now = self.clock()                      # what the node had seen when it answered
        self._count('polls')
        stamped = [(sig, now if t is None else float(t), failed) for sig, t, failed in entries]
        full = len(entries) >= self.cfg.sig_limit
        if stamped:
            oldest = min(t for _s, t, _f in stamped)
            if watch.last_sig is None:
                watch.covered_since = oldest if full else float('-inf')
            elif full:
                previous = watch.sigs[-1][0] if watch.sigs else oldest
                watch.gaps.append((previous, oldest))
            watch.last_sig = stamped[0][0]
            for _sig, t, failed in reversed(stamped):
                watch.sigs.append((t, failed))
        elif watch.last_sig is None:
            watch.covered_since = float('-inf')                  # a pool with no history at all: complete
        fresh = [sig for sig, _t, failed in stamped if not failed]
        return _spread(fresh, self.cfg.sample_txs)

    def _sample(self, runner, watch, signature, budget):
        try:
            body = self._call(runner, budget, 'getTransaction',
                              [signature, {'encoding': 'jsonParsed', 'commitment': 'confirmed', 'maxSupportedTransactionVersion': providers.MAX_TX_VERSION}])
            swap = parse_swap(body, watch.mint, watch.pool)
        except _Skip:
            raise
        except ProviderError as error:
            # a sampled swap that cannot be read is a lost sample, not a desk error: count it by RPC code in health only
            # (live: ~120/h RPC_ERROR on just-landed signatures were flooding the errors table)
            self._count('tx_not_ready')
            key = error.code
            try:
                rpc = json.loads(error.raw or b'{}').get('error', {}).get('code')
                if isinstance(rpc, int):
                    key = '%s:%d' % (error.code, rpc)
            except (ValueError, AttributeError, TypeError):
                pass
            with self._lock:
                self.tx_errors[key] = self.tx_errors.get(key, 0) + 1
            return
        except BotExitError as error:
            self._error(runner, getattr(error, 'code', str(error)), watch.mint, 'transaction')
            return
        if swap is None:
            return
        self._count('swaps_parsed')
        swap['sig'] = signature
        if swap['t'] is None:
            swap['t'] = self.clock()
        if swap['side'] == 'buy' and swap['trader'] and watch.entered_at <= swap['t'] <= watch.entered_at + self.cfg.pumper_window_s:
            watch.pumpers.add(swap['trader'])
        if swap['side'] == 'sell' and watch.creator and swap['trader'] == watch.creator and swap['t'] >= watch.entered_at:
            watch.creator_sold = True
        watch.swaps.append(swap)
        ordered = sorted(watch.swaps, key=lambda s: s['t'])
        watch.swaps = deque(ordered)

    def _holders(self, runner, watch, budget):
        try:
            result = self._call(runner, budget, 'getTokenLargestAccounts', [watch.mint, {'commitment': 'confirmed'}])
            current = parse_largest(result, {watch.base_vault})
        except _Skip:
            raise
        except (ProviderError, BotExitError) as error:
            self._error(runner, getattr(error, 'code', str(error)), watch.mint, 'holders')
            return
        watch.holders_frac = holders_outflow(watch.holders, current)
        watch.holders = current

    # -- metrics ----------------------------------------------------------------------------------------------------------------
    def metrics(self, watch, now):
        w, b = self.cfg.window_s, self.cfg.baseline_s
        recent_lo, base_lo = now - w, now - w - b
        recent = [failed for t, failed in watch.sigs if recent_lo < t <= now]
        base = [failed for t, failed in watch.sigs if base_lo < t <= recent_lo]
        ok_recent, ok_base = sum(1 for f in recent if not f), sum(1 for f in base if not f)
        m = dict.fromkeys(METRICS)
        if watch.complete(recent_lo, now):
            m['tx_rate_30s'] = _q(Decimal(ok_recent) / w)
            m['failed_share_30s'] = _q(Decimal(len(recent) - ok_recent) / len(recent)) if recent else None
        if watch.complete(base_lo, recent_lo) and ok_base >= self.cfg.min_baseline_tx:
            m['tx_rate_baseline'] = _q(Decimal(ok_base) / b)
            if m['tx_rate_30s'] is not None:
                m['tx_rate_ratio'] = _q(Decimal(ok_recent) / w / (Decimal(ok_base) / b))
        swaps = [s for s in watch.swaps if recent_lo < s['t'] <= now]
        older = [s for s in watch.swaps if base_lo < s['t'] <= recent_lo]
        m['swaps_sampled_30s'] = len(swaps)
        sells = [s for s in swaps if s['side'] == 'sell']
        if len(swaps) >= self.cfg.min_sample:
            m['sell_share_30s'] = _q(Decimal(len(sells)) / len(swaps))
        if swaps:
            m['net_sol_flow_30s'] = _q(Decimal(swaps[-1]['reserve_post'] - swaps[0]['reserve_pre']) / LAMPORTS)
            m['max_sell_reserve_frac'] = _q(max((Decimal(s['sol']) / s['reserve_pre'] for s in sells if s['reserve_pre'] > 0),
                                                default=Decimal(0)))
        buys_now, buys_before = [s['sol'] for s in swaps if s['side'] == 'buy'], [s['sol'] for s in older if s['side'] == 'buy']
        if buys_now and buys_before:
            m['buy_size_ratio'] = _q((Decimal(sum(buys_now)) / len(buys_now)) / (Decimal(sum(buys_before)) / len(buys_before)))
        pumper_swaps = [s for s in swaps if s['trader'] in watch.pumpers]
        m['pumper_net_sol_30s'] = _q(Decimal(sum(s['sol'] if s['side'] == 'buy' else -s['sol'] for s in pumper_swaps)) / LAMPORTS)
        m['pumper_sellers_30s'] = len({s['trader'] for s in pumper_swaps if s['side'] == 'sell'})
        m['creator_sold'] = 1 if watch.creator_sold else 0
        m['top_holders_sold_frac'] = None if watch.holders_frac is None else _q(watch.holders_frac)
        return m

    # -- the signal ---------------------------------------------------------------------------------------------------------------
    def _signal(self, runner, watch, position, fired, metrics, now):
        key = (watch.mint, watch.open_id)
        live = self.cfg.mode == 'live'
        qty = position.qty_raw if position is not None else None
        value, source = self._value(runner, watch.mint, qty, now, quote=not live) if qty else (None, None)
        features = {'mode': self.cfg.mode, 'open_fill_id': watch.open_id, 'signal_at': now, 'rules': list(fired),
                    'metrics': {k: (None if v is None else str(v)) for k, v in metrics.items()}, 'qty_raw': qty,
                    'value_lamports': value, 'value_source': source, 'pool': watch.pool, 'entered_at': watch.entered_at,
                    'pumpers': len(watch.pumpers), 'creator_known': watch.creator is not None,
                    'held_s': round(now - watch.entered_at, 3)}
        kind = LIVE_KIND if live else SHADOW_KIND
        self.store.add_decision(kind, 'SIGNAL', mint=watch.mint, reasons=list(fired), features=features,
                                code_version=self.code_version, strategy_version=self.strategy_version, ts=now)
        self._signaled.add(key)
        watch.signaled = True
        with self._lock:
            self.counts['signals'] += 1
            self.counts['live_signals' if live else 'shadow_signals'] += 1
            if live:
                self._live[key] = '%s:%s' % (EXIT_REASON, ','.join(fired))
        log.info('bot_exit %s signal %s %s', self.cfg.mode, watch.mint[:8], ','.join(fired))

    def _value(self, runner, mint, qty, now, *, quote):
        """Hypothetical net exit value (lamports) of the whole quantity: a fresh D1 quote mark, else one Jupiter quote on the low
        lane (shadow only), else a fresh vault mark. Marks are net of the fixed fee; the paper slippage is applied on top."""
        pcfg = runner.pcfg
        mark = dict(runner.marks).get(mint)
        fresh = mark is not None and len(mark) >= 3 and 0 <= now - mark[1] <= self.cfg.quote_ttl_s
        if fresh and mark[2] == 'quote':
            return net_lamports(A.lamports(mark[0]) + pcfg.fee_lamports, pcfg), 'd1_quote_mark'
        if quote:
            try:
                q, _raw, _meta = self.low.jupiter.quote(mint, SOL_MINT, int(qty), runner.taker)
                self._count('quotes')
                return net_lamports(q.out_amount, pcfg), 'jupiter_low'
            except ProviderError as error:
                self._count('quote_sheds' if providers.is_shed(error) else 'errors')
        if fresh and mark[2] == 'vaults':
            return net_lamports(A.lamports(mark[0]) + pcfg.fee_lamports, pcfg), 'vault_mark'
        return None, None

    def _creator(self, mint):
        """``coin_creator`` of the PumpSwap pool account the screen retained (no request), or None."""
        try:
            with self.store._lock:
                row = self.store.db.execute("SELECT raw FROM observations WHERE mint=? AND kind='screen:accounts_mint_pool' "
                                            "ORDER BY id DESC LIMIT 1", (mint,)).fetchone()
            if row is None:
                return None
            account = json.loads(bytes(row[0]))['result']['value'][1]
            from desk.pools import parse_pool
            creator = parse_pool(account)['coin_creator']
            return creator if isinstance(creator, str) and creator != '11111111111111111111111111111111' else None
        except Exception:
            return None


class _Skip(Exception):
    """Internal: the pass ends here (shed, yield, budget spent, stop)."""


def _spread(items, n):
    """``n`` items spread evenly over ``items`` (newest first), the newest always included."""
    if n <= 0 or not items:
        return []
    if len(items) <= n:
        return list(items)
    step = Decimal(len(items) - 1) / (n - 1) if n > 1 else 0
    return [items[int(Decimal(i) * step)] for i in range(n)]


def _q(value):
    return Decimal(value).quantize(Decimal('0.000001'))


# ------------------------------------------------------------------------------------------------------------------- build
def build(cfg_dict, *, store, keys, code_version, strategy_version, clock=time.time, transport_kwargs=None):
    """None when disabled or absent (the runner then behaves exactly as before)."""
    cfg = Config.from_dict(cfg_dict)
    if not cfg.enabled:
        return None
    transport_kwargs = dict(transport_kwargs or {})
    low = providers.low(keys, **transport_kwargs)
    return BotExit(cfg, store=store, low=low, code_version=code_version, strategy_version=strategy_version, clock=clock,
                   sleep=transport_kwargs.get('sleep', time.sleep))


# ------------------------------------------------------------------------------------------------------------------ report
def report_section(connection, *, big_loss_frac=Decimal('0.5')):
    """Would the signals have saved money? Per signal of a CLOSED lifecycle: the hypothetical exit value at the signal vs the net
    proceeds (sol - fee, refundable ATA rent taken out) of every sell of that lifecycle AFTER the signal. ``saved`` = shadow - actual;
    a FALSE POSITIVE is a signal on a trade that later fetched more than the signal value (holding paid). Read-only."""
    from lean import execution
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out = {'signals': 0, 'by_mode': {}, 'by_rule': {}, 'open': 0, 'unvalued': 0, 'scored': 0, 'saved_count': 0, 'cost_count': 0,
           'shadow_sum_sol': '0', 'actual_sum_sol': '0', 'saved_sum_sol': '0', 'false_positive_rate': None,
           'closed_trades': 0, 'closed_without_signal': 0, 'big_losses': 0, 'big_losses_signalled': 0, 'rows': []}
    if not {'decisions', 'fills', 'position_state'} <= tables:
        return out
    signals = connection.execute('SELECT ts,kind,mint,reasons,features FROM decisions WHERE kind IN (?,?) ORDER BY id',
                                 (SHADOW_KIND, LIVE_KIND)).fetchall()
    closed = {}
    for mint, open_id, state in connection.execute("SELECT mint,open_fill_id,state FROM position_state WHERE event='closed' ORDER BY id"):
        try:
            closed[(mint, int(open_id))] = json.loads(state)
        except (ValueError, TypeError):
            continue
    fills = connection.execute('SELECT id,ts,mint,side,sol_lamports,fee_lamports,realized_lamports,cost_sold_lamports FROM fills ORDER BY id').fetchall()
    payloads = execution.payloads_by_fill(connection)
    lifecycle_of = dict(connection.execute('SELECT fill_id,open_fill_id FROM position_state WHERE fill_id IS NOT NULL').fetchall())
    cost = {}
    for fid, _ts, _mint, side, sol, fee, _realized, _cost_sold in fills:
        if side == 'buy' and fid in lifecycle_of:
            cost[lifecycle_of[fid]] = cost.get(lifecycle_of[fid], 0) + sol + fee
    signalled = set()
    shadow_sum = actual_sum = 0
    for ts, kind, mint, reasons, features in signals:
        try:
            f = json.loads(features)
            open_id = int(f['open_fill_id'])
            rules = json.loads(reasons) or []
        except (ValueError, KeyError, TypeError):
            continue
        out['signals'] += 1
        out['by_mode'][kind] = out['by_mode'].get(kind, 0) + 1
        for rule in rules:
            out['by_rule'][rule] = out['by_rule'].get(rule, 0) + 1
        signalled.add((mint, open_id))
        row = {'mint': mint, 'kind': kind, 'rules': rules, 'signal_at': ts, 'value_source': f.get('value_source'),
               'shadow_sol': None, 'actual_sol': None, 'saved_sol': None, 'status': 'OPEN'}
        out['rows'].append(row)
        if (mint, open_id) not in closed:
            out['open'] += 1
            continue
        actual = 0
        for fid, fts, fmint, side, sol, fee, realized, cost_sold in fills:
            if fmint != mint or side != 'sell' or lifecycle_of.get(fid) != open_id or fts < ts:
                continue
            econ = execution.economic({'side': side, 'sol_lamports': sol, 'fee_lamports': fee, 'realized_lamports': realized,
                                       'cost_sold_lamports': cost_sold}, payloads.get(fid))
            actual += econ['sol_lamports'] - econ['fee_lamports']
        row['actual_sol'] = str(Decimal(actual) / LAMPORTS)
        value = f.get('value_lamports')
        if value is None:
            out['unvalued'] += 1
            row['status'] = 'UNVALUED'
            continue
        value = int(value)
        out['scored'] += 1
        shadow_sum += value
        actual_sum += actual
        saved = value - actual
        row.update(shadow_sol=str(Decimal(value) / LAMPORTS), saved_sol=str(Decimal(saved) / LAMPORTS),
                   status='SAVED' if saved > 0 else 'COST' if saved < 0 else 'EVEN')
        out['saved_count' if saved > 0 else 'cost_count'] += 1 if saved != 0 else 0
    out['shadow_sum_sol'] = str(Decimal(shadow_sum) / LAMPORTS)
    out['actual_sum_sol'] = str(Decimal(actual_sum) / LAMPORTS)
    out['saved_sum_sol'] = str(Decimal(shadow_sum - actual_sum) / LAMPORTS)
    if out['scored']:
        out['false_positive_rate'] = str((Decimal(out['cost_count']) / out['scored']).quantize(Decimal('0.0001')))
    for (mint, open_id), state in closed.items():
        out['closed_trades'] += 1
        hit = (mint, open_id) in signalled
        out['closed_without_signal'] += 0 if hit else 1
        try:
            pnl, basis = int(state['trade_pnl_lamports']), cost.get(open_id, 0)
        except (KeyError, TypeError, ValueError):
            continue
        if basis > 0 and Decimal(pnl) <= -big_loss_frac * basis:
            out['big_losses'] += 1
            out['big_losses_signalled'] += 1 if hit else 0
    out['rows'] = out['rows'][-500:]
    return out
