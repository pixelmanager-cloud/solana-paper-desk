"""L20: sellability evidence for paper SELLs. PAPER ONLY: read-only RPC methods on the shared LOW lane, no signing, no sending.

A Jupiter quote proves a ROUTE exists, not that a real trader could sell: a honeypot (a Token-2022 transfer hook, a freeze, a
non-pump program) can quote fine and still refuse every sell. Before a paper SELL is booked, this module looks for evidence that real
traders DID sell recently: at least ``min_organic_sells`` (2) successful sells by DISTINCT signers into the pool in the last
``sell_window_s`` (300 s). A sell = a transaction in which the pool's token vault rose and its WSOL vault fell, and the signer's token
balance went down while its SOL + WSOL went up (fee added back). The paper taker and the coin creator (``coin_creator`` of the pool
account the screen retained) never count.

The probe: ONE ``getSignaturesForAddress(pool)`` + at most ``max_tx_samples`` (8) ``getTransaction`` (one per equal time stratum of the window, aperiodic pick),
stopping as soon as enough distinct sellers are found. Results are cached per pool for ``cache_s`` (60 s); a hard
``max_calls_per_hour`` (400) caps the module. Verdicts:

  VERIFIED    enough distinct organic sellers were seen                              -> the SELL is booked normally
  UNVERIFIED  the probe COMPLETED (every planned sample read, nothing shed) and found fewer -> the SELL is booked as SELL_UNVERIFIED
  UNKNOWN     the probe was shed / failed / cut / not finished in time                -> the SELL is booked normally, flagged UNKNOWN

The probe NEVER delays an exit: ``start`` returns at once (a daemon thread does the calls); the exit waits only inside the L10 delay it
pays anyway (``Execution.run_exits`` waits for the probes at most until the same deadline its sleep would end at, then sleeps the rest);
whatever is not finished then is UNKNOWN (a late result still fills the cache for the next sell of the pool). Because the probe has
its own thread it never sleeps on the runner's clock: a ``no_token`` shed is paced with a real-time wait, up to the probe's own deadline.
"""
import json
import logging
from decimal import Decimal
import threading
import time

from lean.flow import HourBudget, LowCalls, Stop, is_organic_sell, parse_trade, stratified
from lean.providers import PAPER_TAKER, ProviderError, MAX_TX_VERSION
from lean.wallet_signals import SignalError, parse_signatures

log = logging.getLogger('lean.sell_evidence')

VERIFIED, UNVERIFIED, UNKNOWN = 'VERIFIED', 'UNVERIFIED', 'UNKNOWN'
SELL_UNVERIFIED = 'SELL_UNVERIFIED'
POLICIES = ('zero', 'flag_only')
DEFAULTS = {
    'enabled': False,
    'min_organic_sells': 2,        # distinct organic sellers needed
    'sell_window_s': 300,          # ... in this many seconds before the probe
    'unverified_sell_policy': 'flag_only',   # flag_only (default): booked at the quote, flagged; zero: valued at 0 proceeds
    'max_unverified_window': 1000,  # more successful window txs than this with too few sellers found = UNKNOWN (sample too small); 1000 = off
    'max_tx_samples': 8,           # getTransaction per probe (+ 1 getSignaturesForAddress)
    'sig_limit': 1000,
    'cache_s': 60,                 # a conclusive result is reused for this long per pool
    'max_calls_per_hour': 400,     # HARD cap for the module (~44 full probes / h)
    'pace_s': 0.25,                # real-time wait on a no_token shed (the probe thread only)
    'probe_timeout_s': 20.0,       # the probe thread gives up after this long (real time)
}


class ConfigError(ValueError):
    pass


def config(raw):
    """lean.json ``sell_evidence`` -> validated settings. Absent / null = disabled. Unknown keys and wrong types are refused."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ConfigError('sell_evidence must be an object')
    unknown = set(raw) - set(DEFAULTS) - {'_comment'}
    if unknown:
        raise ConfigError('sell_evidence: unknown keys %s' % sorted(unknown))
    out = {k: raw.get(k, v) for k, v in DEFAULTS.items()}
    if type(out['enabled']) is not bool:
        raise ConfigError('sell_evidence.enabled must be true or false')
    if out['unverified_sell_policy'] not in POLICIES:
        raise ConfigError('sell_evidence.unverified_sell_policy must be zero or flag_only')
    for key, low, high in (('min_organic_sells', 1, 20), ('sell_window_s', 10, 86400), ('max_tx_samples', 1, 50), ('sig_limit', 1, 1000), ('max_unverified_window', 1, 100000),
                           ('cache_s', 0, 3600), ('max_calls_per_hour', 1, 100_000)):
        if type(out[key]) is not int or not low <= out[key] <= high:
            raise ConfigError('sell_evidence.%s must be an integer in [%d, %d]' % (key, low, high))
    for key, low, high in (('pace_s', 0.01, 10), ('probe_timeout_s', 0.5, 120)):
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or not low <= value <= high:
            raise ConfigError('sell_evidence.%s out of range' % key)
        out[key] = float(value)
    return out


def unknown(why):
    return {'verdict': UNKNOWN, 'why': str(why)[:60], 'sellers': None, 'sampled': None}


def probe(calls, pool, mint, *, now, cfg, exclude):
    """The evidence for ``pool`` at ``now``: a verdict dict. ``calls`` is a ``lean.flow.LowCalls``. Never raises."""
    sellers, sampled, failed = set(), 0, 0
    try:
        rows = parse_signatures(calls.rpc('getSignaturesForAddress', [pool, {'limit': cfg['sig_limit'], 'commitment': 'confirmed'}]))
        window = [r for r in rows if not r['failed'] and r['block_time'] is not None and now - cfg['sell_window_s'] <= r['block_time'] <= now + 5]
        plan = stratified(list(reversed(window)), cfg['max_tx_samples'])[::-1]   # one per stratum of the window, newest first
        for entry in plan:
            try:
                view = parse_trade(calls.rpc('getTransaction', [entry['signature'], {'encoding': 'jsonParsed', 'commitment': 'confirmed',
                                                                                   'maxSupportedTransactionVersion': MAX_TX_VERSION}]),
                                   entry['signature'], mint, pool)
            except (ProviderError, SignalError):
                failed += 1
                continue
            if view is None:
                failed += 1
                continue
            sampled += 1
            if is_organic_sell(view, exclude):
                sellers.add(view['signer'])
                if len(sellers) >= cfg['min_organic_sells']:
                    break
    except Stop as stop:                          # shed / cap / deadline / stopping: absence is not proven
        return {**unknown(stop.code), 'sellers': len(sellers), 'sampled': sampled, 'calls': calls.used}
    except ProviderError as error:
        return {**unknown('PROVIDER_' + error.code), 'calls': calls.used}
    except SignalError as error:
        return {**unknown(error.code), 'calls': calls.used}
    except Exception as error:                 # noqa: BLE001 - a probe failure is UNKNOWN, never an exit failure
        return {**unknown('PROBE_FAILED:' + type(error).__name__), 'calls': calls.used}
    out = {'sellers': len(sellers), 'sampled': sampled, 'window_txs': len(window), 'calls': calls.used}
    if len(sellers) >= cfg['min_organic_sells']:
        return {'verdict': VERIFIED, 'why': None, **out}
    if failed:                                  # the planned sample was not fully read: absence is not proven
        return {**unknown('SAMPLE_INCOMPLETE'), **out}
    oldest = min((r['block_time'] for r in rows if r['block_time'] is not None), default=None)
    if len(rows) >= cfg['sig_limit'] and (oldest is None or oldest > now - cfg['sell_window_s']):
        return {**unknown('WINDOW_NOT_COVERED'), **out}      # a full page that ends inside the window: absence is not proven
    if len(window) > cfg['max_unverified_window']:
        return {**unknown('SAMPLE_TOO_SMALL'), **out}        # a busy pool: a handful of samples cannot prove there are no sellers
    return {'verdict': UNVERIFIED, 'why': 'NO_SELLS_IN_WINDOW' if not window else 'TOO_FEW_ORGANIC_SELLERS', **out}


class Probe:
    """One probe's handle: ``event`` is set when ``result`` is final."""

    def __init__(self, pool, result=None, cached=False):
        self.pool, self.result, self.cached = pool, result, cached
        self.event = threading.Event()
        if result is not None:
            self.event.set()


class SellEvidence:
    """The probes of one runner. ``start`` never blocks on the network; ``verdict`` never blocks at all."""

    def __init__(self, cfg, *, helius, store, clock=time.time, taker=PAPER_TAKER, stop=None, monotonic=time.monotonic):
        self.cfg = config(cfg)
        self.helius, self.store, self.clock, self.taker = helius, store, clock, taker
        self.stop = stop or threading.Event()
        self.monotonic = monotonic
        self.budget = HourBudget(self.cfg['max_calls_per_hour'], clock)
        self._cache, self._inflight, self._creators = {}, {}, {}
        self._lock = threading.Lock()
        self._pace = threading.Event()             # never set: a real-time wait that a stop does not need to interrupt
        self.counts = {'probes': 0, 'cached': 0, VERIFIED: 0, UNVERIFIED: 0, UNKNOWN: 0, 'pending_at_fill': 0, 'calls': 0}

    @property
    def policy(self):
        return self.cfg['unverified_sell_policy']

    def health(self):
        return {'enabled': True, 'policy': self.policy, **self.counts, 'calls_last_hour': self.budget.used(),
                'max_calls_per_hour': self.cfg['max_calls_per_hour'], 'inflight': len(self._inflight)}

    # -- the coin creator (excluded): the pool account the screen retained --------------------------------------------------
    def creator(self, mint):
        if mint in self._creators:
            return self._creators[mint]
        creator = None
        try:
            from lean.features import dev_wallet
            rows = self.store.rows('observations', mint=mint, kind='screen:accounts_mint_pool', limit=10)
            if rows:
                creator, _why = dev_wallet({'raw': [('accounts_mint_pool', bytes(rows[-1]['raw']))]})
        except Exception:                           # noqa: BLE001 - unknown creator: nothing excluded but the taker
            creator = None
        self._creators[mint] = creator
        return creator

    # -- probes ------------------------------------------------------------------------------------------------------
    def start(self, mint, pool):
        """A probe handle for ``pool`` at once: a cached conclusive result, the probe already in flight, or a new daemon-thread probe."""
        now = self.clock()
        with self._lock:
            hit = self._cache.get(pool)
            if hit is not None and 0 <= now - hit[1] <= self.cfg['cache_s']:
                self.counts['cached'] += 1
                return Probe(pool, dict(hit[0], cached=True), cached=True)
            running = self._inflight.get(pool)
            if running is not None:
                return running
            handle = self._inflight[pool] = Probe(pool)
        exclude = {self.taker} | ({self.creator(mint)} - {None})
        thread = threading.Thread(target=self._run, args=(handle, mint, pool, now, exclude), name='sell_evidence', daemon=True)
        thread.start()
        return handle

    def _run(self, handle, mint, pool, now, exclude):
        cfg = self.cfg
        calls = LowCalls(self.helius, limit=1 + cfg['max_tx_samples'], budget=self.budget, wait=self._pace.wait, pace_s=cfg['pace_s'],
                         patience_s=cfg['probe_timeout_s'], stop=self.stop, deadline=self.monotonic() + cfg['probe_timeout_s'],
                         monotonic=self.monotonic)
        try:
            result = probe(calls, pool, mint, now=now, cfg=cfg, exclude=exclude)
        except Exception as error:                  # noqa: BLE001
            result = unknown('PROBE_FAILED:' + type(error).__name__)
        result = {**result, 'probed_at': now, 'min_organic_sells': cfg['min_organic_sells'], 'sell_window_s': cfg['sell_window_s']}
        with self._lock:
            self.counts['probes'] += 1
            self.counts['calls'] += int(result.get('calls') or 0)
            self.counts[result['verdict']] += 1
            if result['verdict'] != UNKNOWN:
                self._cache[pool] = (result, now)
            self._inflight.pop(pool, None)
            handle.result = result
        handle.event.set()

    def wait(self, handles, timeout):
        """Wait (real time) until every handle is final or ``timeout`` seconds have passed. Returns the seconds waited."""
        start = self.monotonic()
        for handle in handles:
            left = timeout - (self.monotonic() - start)
            if left <= 0:
                break
            handle.event.wait(left)
        return self.monotonic() - start

    def verdict(self, handle):
        """The result of ``handle`` NOW (never waits): UNKNOWN/PENDING when it is not final."""
        if handle is None:
            return unknown('NOT_PROBED')
        if not handle.event.is_set():
            with self._lock:
                self.counts['pending_at_fill'] += 1
            return unknown('PENDING')
        return dict(handle.result)


def payload_fields(result):
    """The ``sell_evidence`` fields of a fill's execution payload: ``{'sellers', 'sampled', ...}`` when conclusive, ``'UNKNOWN'``
    (with ``sell_evidence_why``) otherwise; ``sell_status`` = SELL_UNVERIFIED on an unverified sell."""
    if result['verdict'] == UNKNOWN:
        return {'sell_evidence': UNKNOWN, 'sell_evidence_why': result.get('why')}
    evidence = {k: result.get(k) for k in ('verdict', 'sellers', 'sampled', 'window_txs', 'why', 'probed_at', 'min_organic_sells',
                                            'sell_window_s', 'cached') if result.get(k) is not None}
    out = {'sell_evidence': evidence}
    if result['verdict'] == UNVERIFIED:
        out['sell_status'] = SELL_UNVERIFIED
    return out


def build(cfg, *, store, keys, clock=time.time, transport_kwargs=None):
    """The prober of a validated ``sell_evidence`` config, on the shared LOW lane; None when disabled."""
    if not cfg['enabled']:
        return None
    from lean import providers
    transport_kwargs = dict(transport_kwargs or {})
    return SellEvidence(cfg, helius=providers.low(keys, **transport_kwargs).helius, store=store, clock=clock)


# ----------------------------------------------------------------------------------------------------------- report
def report_section(connection):
    """Sells by evidence verdict and every UNVERIFIED sell (booked proceeds vs the quote), from an open READ-ONLY connection."""
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out = {'sells': 0, 'verdicts': {VERIFIED: 0, UNVERIFIED: 0, UNKNOWN: 0, 'NOT_CHECKED': 0}, 'unknown_why': {}, 'unverified': [],
           'forgone_sol': '0'}
    if not {'position_state', 'fills'} <= tables:
        return out
    forgone = 0
    rows = connection.execute("SELECT f.id, f.ts, f.mint, f.qty_raw, f.sol_lamports, f.realized_lamports, p.event, p.state FROM position_state p "
                              "JOIN fills f ON f.id = p.fill_id WHERE f.side='sell' ORDER BY f.id").fetchall()
    for fill_id, ts, mint, qty, sol, realized, event, text in rows:
        try:
            state = json.loads(text) or {}
        except ValueError:
            continue
        payload = state.get('execution') if isinstance(state.get('execution'), dict) else {}
        out['sells'] += 1
        evidence = payload.get('sell_evidence')
        if evidence is None:
            out['verdicts']['NOT_CHECKED'] += 1
            continue
        if evidence == UNKNOWN:
            out['verdicts'][UNKNOWN] += 1
            why = str(payload.get('sell_evidence_why') or 'UNSPECIFIED')
            out['unknown_why'][why] = out['unknown_why'].get(why, 0) + 1
            continue
        verdict = evidence.get('verdict') if isinstance(evidence, dict) else None
        if verdict not in (VERIFIED, UNVERIFIED):
            continue
        out['verdicts'][verdict] += 1
        if verdict == UNVERIFIED:
            quoted = int(payload.get('unverified_quote_sol_lamports') or 0)
            forgone += max(0, quoted - sol)
            out['unverified'].append({'fill_id': fill_id, 'ts': ts, 'mint': mint, 'event': event,
                                      'reason': state.get('reason') if event == 'closed' else state.get('last_reason'),
                                      'policy': payload.get('unverified_sell_policy'), 'booked_sol': _sol(sol),
                                      'quoted_sol': _sol(quoted), 'realized_sol': _sol(realized),
                                      'sellers': evidence.get('sellers'), 'sampled': evidence.get('sampled'), 'why': evidence.get('why')})
    out['forgone_sol'] = _sol(forgone)
    return out


def _sol(lamports):
    return str(Decimal(int(lamports)) / 10 ** 9)
