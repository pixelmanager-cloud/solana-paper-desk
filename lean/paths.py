"""L07R price-path recorder: strategy-tuning data for every candidate that passed the HAZARD checks, entered or not.

PAPER ONLY and read-only: one batched ``getMultipleAccounts`` of pool vault accounts on the shared LOW provider lane
(``lean.providers.low``), nothing else. A recorder failure NEVER reaches the trader: every public method swallows its
own failures, counts them in ``health()`` and returns.

Which candidates (``eligible``): a screen that PASSED, or one rejected ONLY for the soft market reasons (market cap or
liquidity outside the band). Hazard rejects (authorities, extensions, pool/vault/LP evidence, holder concentration) and
failed screens (provider errors) are never recorded. Every soft entry outcome after a passed screen is recorded too:
cost cap, portfolio limits, entry throttle, cooldown, a quote that failed, entries stopped. A candidate queued for a
retry is decided on its final attempt. NOTE: a market-band reject stops the screen before the holder check, so those
paths carry ``holders_checked: false``.

Rows (``observations``, append-only like everything in the store):
* ``path_start`` once: the screen features, ``entered``, the stage and reasons it was not entered, and the ``target``
  (vault keys, decimals, the reference quantity and its cost) so a restarted process can resume the path.
* ``path_mark`` every ``interval_s`` for ``path_hours``: ``meta`` = ``{ts, slot, base_raw, quote_raw, net_sol,
  liquidity_sol, status}``; ``raw`` = the vault amounts and slot as canonical JSON (the account bytes are not kept).
  ``net_sol`` is ``lean.adapters.mark`` (THE function the live position loop marks with) of the reference quantity:
  the filled quantity for an entered candidate, else the tokens ``ref_size_sol`` buys at the path-start reserves.
  ``liquidity_sol`` = 2 x (quote vault - the pool fees seen at the screen) in SOL. ``status`` is OK / ACCOUNT_MISSING
  (a closed vault) / MALFORMED / EMPTY_POOL.
* ``path_end`` once: COMPLETE after ``path_hours`` (or EXPIRED_WHILE_DOWN when the process was down at the end).
* the event ``paths_active`` (``store.record``): the ``path_start`` ids of the live paths, rewritten whenever the set
  changes. ``resume`` reads the latest one (``store.latest_event`` + ``store.observation``) after a restart.

Budget: at most 100 ACCOUNTS per call (the RPC limit) = 50 pools (base + quote vault each). The low lane never waits.
When it has no token right now, THIS recorder thread paces itself (sleeps one token interval, no lock held) as long as
the poll stays within 80% of ``interval_s``; a shed window (a 429 on any lane) or an exhausted budget ends the poll and
the missed marks count as gaps. No provider I/O runs while any lock is held; the marks of one poll are written in ONE
store transaction.
"""
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from decimal import Decimal

from lean import adapters as A
from lean.providers import LANE_SHED, MAX_MULTIPLE_ACCOUNTS, ProviderError, _pubkey

log = logging.getLogger('lean.paths')

SOFT_SCREEN_REASONS = frozenset({'MARKET_CAP_BELOW_MIN', 'MARKET_CAP_ABOVE_MAX', 'LIQUIDITY_BELOW_MIN'})
ACCOUNTS_PER_POOL = 2
POOLS_PER_CALL = MAX_MULTIPLE_ACCOUNTS // ACCOUNTS_PER_POOL          # 50 pools = 100 accounts
CHECKPOINT = 'paths_active'
PACE_BUDGET = 0.8            # a poll may pace its calls over at most 80% of the interval
DEFAULTS = {'enabled': False, 'path_hours': 6.0, 'interval_s': 15.0, 'ref_size_sol': '0.2', 'max_paths': 2000}


def config(raw):
    """lean.json ``paths`` -> validated settings. Absent = disabled. Unknown keys are refused."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError('paths must be an object')
    if set(raw) - set(DEFAULTS) - {'_comment'}:
        raise ValueError('paths: unknown keys %s' % sorted(set(raw) - set(DEFAULTS) - {'_comment'}))
    out = {k: raw.get(k, v) for k, v in DEFAULTS.items()}
    if type(out['enabled']) is not bool:
        raise ValueError('paths.enabled must be true or false')
    for key, low, high in (('path_hours', 0.01, 72), ('interval_s', 1, 3600)):
        if isinstance(out[key], bool) or not isinstance(out[key], (int, float)) or not low <= out[key] <= high:
            raise ValueError('paths.%s out of range' % key)
        out[key] = float(out[key])
    if type(out['max_paths']) is not int or not 1 <= out['max_paths'] <= 100_000:
        raise ValueError('paths.max_paths out of range')
    try:
        size = Decimal(str(out['ref_size_sol']))
    except ArithmeticError:
        raise ValueError('paths.ref_size_sol must be a decimal') from None
    if isinstance(out['ref_size_sol'], bool) or not size.is_finite() or not Decimal('0.001') <= size <= 1000:
        raise ValueError('paths.ref_size_sol out of range')
    out['ref_size_sol'] = str(size)
    return out


def eligible(screen):
    """True when the screen shows no hazard: it passed, or it was rejected ONLY for soft market-band reasons."""
    if screen is None or getattr(screen, 'error', None):
        return False
    if screen.passed:
        return True
    reasons = tuple(screen.reasons or ())
    return bool(reasons) and all(r in SOFT_SCREEN_REASONS for r in reasons)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, default=str).encode()


@dataclass(frozen=True)
class Target:
    mint: str
    pool: str
    base_vault: str
    quote_vault: str
    decimals: int
    fee_raw: int            # pool fees in the quote vault at the screen (excluded from liquidity_sol)
    qty_raw: int            # the reference quantity marked by adapters.mark
    cost_lamports: int      # what that quantity cost (net_sol / cost = the strategy's mark ratio)
    basis: str              # 'fill' (entered) or 'ref_size'


@dataclass
class _Path:
    target: Target
    started: float
    ends: float
    candidate_id: object
    start_id: object = None
    marks: int = 0
    gaps: int = 0


class PathRecorder:
    def __init__(self, *, store, helius, pcfg, code_version, strategy_version, pool_fee_bps=25, path_hours=6.0,
                 interval_s=15.0, ref_size_sol='0.2', max_paths=2000, clock=time.time, sleep=time.sleep):
        self.store, self.helius, self.pcfg, self.clock, self.sleep = store, helius, pcfg, clock, sleep
        self.code_version, self.strategy_version = code_version, strategy_version
        self.pool_fee_bps = int(pool_fee_bps)
        self.path_s, self.interval_s = float(path_hours) * 3600, float(interval_s)
        self.ref_lamports = A.lamports(Decimal(str(ref_size_sol)))
        self.max_paths = int(max_paths)
        self._paths = {}                           # mint -> _Path
        self._lock = threading.Lock()              # in-memory state only: never held across I/O
        self._checkpoint_lock = threading.Lock()   # serializes checkpoint writes (never taken by the trader)
        self.stats = {'started': 0, 'resumed': 0, 'ended': 0, 'polls': 0, 'calls': 0, 'marks': 0, 'gaps': 0, 'shed': 0,
                      'paced': 0, 'errors': 0, 'write_failures': 0, 'hazard_skipped': 0, 'duplicates': 0,
                      'capacity_dropped': 0}
        self.errors_by_code = {}
        self.last_error = None
        self.last_poll_at = None

    # -- bookkeeping ----------------------------------------------------------------------------------------------
    def _bump(self, name, n=1):
        with self._lock:
            self.stats[name] += n

    def _fail(self, code, error=None):
        code = str(getattr(error, 'code', None) or code)[:64]
        with self._lock:
            self.stats['errors'] += 1
            self.errors_by_code[code] = self.errors_by_code.get(code, 0) + 1
            self.last_error = code
        log.debug('path recorder: %s (%s)', code, type(error).__name__ if error is not None else '-')

    def active(self):
        with self._lock:
            return {m: p.start_id for m, p in self._paths.items() if p.start_id is not None}

    def health(self):
        with self._lock:
            out = {'enabled': True, 'active_paths': sum(1 for p in self._paths.values() if p.start_id is not None),
                   'last_poll_at': self.last_poll_at, 'last_error': self.last_error,
                   'errors_by_code': dict(self.errors_by_code), **self.stats}
        limiter = getattr(getattr(self.helius, 'transport', None), 'limiter', None)
        if isinstance(getattr(limiter, 'stats', None), dict):
            out['low_lane'] = dict(limiter.stats)
        return out

    # -- starting a path ------------------------------------------------------------------------------------------
    def on_candidate(self, mint, candidate_id, screen):
        """Runner hook, after a candidate was handled (its final attempt). Never raises; True when a path started."""
        try:
            if not eligible(screen):
                self._bump('hazard_skipped')
                return False
            stage, reasons, entered = self._outcome(mint, screen)
            position = self.store.positions().get(mint) if entered else None
            if entered and position is None:
                entered, reasons = False, ['NOT_FILLED']
            return self.start(mint, candidate_id, screen.features, entered=entered, stage=stage, reasons=reasons,
                              screen_reasons=list(screen.reasons or ()), position=position)
        except Exception as error:                                     # noqa: BLE001 - never reaches the trader
            self._fail('START_FAILED', error)
            return False

    def _outcome(self, mint, screen):
        """(stage, reasons, entered) from what the runner recorded for this attempt (store reads only)."""
        if not screen.passed:
            return 'screen', list(screen.reasons), False
        decisions = self.store.rows('decisions', mint=mint, limit=10000)
        screens = [d for d in decisions if d['kind'] == 'screen']
        after_id = screens[-1]['id'] if screens else 0
        after_ts = screens[-1]['ts'] if screens else 0
        entries = [d for d in decisions if d['kind'] == 'entry' and d['id'] > after_id]
        if entries:
            last = entries[-1]
            if last['action'] == 'BUY':
                return 'entry', [], True
            return 'entry', list(json.loads(last['reasons'])) or [last['action']], False
        errors = [e for e in self.store.rows('errors', mint=mint, limit=10000) if e['ts'] >= after_ts]
        return 'quote', ['ENTRY_FAILED:' + errors[-1]['code'] if errors else 'NOT_ENTERED'], False

    def _target(self, mint, features, position):
        base_vault, quote_vault = _pubkey(features['pool_base_token_account']), _pubkey(features['pool_quote_token_account'])
        base0, gross0, spendable0 = (int(features[k]) for k in ('base_reserve_raw', 'quote_gross_raw', 'quote_spendable_raw'))
        if base0 <= 0 or gross0 <= 0 or not 0 <= spendable0 <= gross0:
            raise ValueError('pool reserves at the screen are unusable')
        if position is not None:
            qty, cost, basis = int(position.qty_raw), int(position.cost_lamports), 'fill'
        else:
            spent = self.ref_lamports * (10_000 - self.pool_fee_bps) // 10_000
            qty, cost, basis = base0 * spent // (gross0 + spent), self.ref_lamports, 'ref_size'
        if qty <= 0:
            raise ValueError('reference quantity rounds to nothing')
        return Target(mint=_pubkey(mint), pool=str(features.get('pool')), base_vault=base_vault, quote_vault=quote_vault,
                      decimals=int(features['decimals']), fee_raw=gross0 - spendable0, qty_raw=qty, cost_lamports=cost,
                      basis=basis)

    def start(self, mint, candidate_id, features, *, entered, stage, reasons, screen_reasons=(), position=None):
        """Begin recording ``mint`` (no provider I/O: the vault keys come from the screen). Never raises."""
        try:
            target = self._target(mint, features, position)
            now = self.clock()
            with self._lock:
                if mint in self._paths:
                    self.stats['duplicates'] += 1
                    return False
                if len(self._paths) >= self.max_paths:
                    self.stats['capacity_dropped'] += 1
                    return False
                path = self._paths[mint] = _Path(target, now, now + self.path_s, candidate_id)
            meta = {'mint': mint, 'candidate_id': candidate_id, 'entered': bool(entered),
                    'not_entered': None if entered else {'stage': stage, 'reasons': list(reasons)},
                    'screen_reasons': list(screen_reasons), 'holders_checked': features.get('holder_check') == 'OK',
                    'started': now, 'ends': now + self.path_s, 'path_hours': self.path_s / 3600,
                    'interval_s': self.interval_s, 'target': asdict(target)}
            try:
                start_id = self.store.add_observation(
                    'path_start', _canonical({**meta, 'features': _jsonable(features)}), mint=mint,
                    candidate_id=candidate_id, meta=meta, code_version=self.code_version,
                    strategy_version=self.strategy_version, ts=now)
            except BaseException:
                with self._lock:
                    self._paths.pop(mint, None)
                raise
            with self._lock:
                path.start_id = start_id
                self.stats['started'] += 1
            self._checkpoint(now)
            return True
        except Exception as error:                                     # noqa: BLE001
            self._fail('START_FAILED', error)
            return False

    # -- persistence of the live set ----------------------------------------------------------------------------------
    def _checkpoint(self, now=None):
        try:
            with self._checkpoint_lock:
                ids = sorted(self.active().values())
                self.store.record(CHECKPOINT, {'active': ids, 'at': self.clock() if now is None else now},
                                  code_version=self.code_version, strategy_version=self.strategy_version,
                                  ts=self.clock() if now is None else now)
        except Exception as error:                                     # noqa: BLE001
            self._fail('CHECKPOINT_FAILED', error)

    def resume(self):
        """After a restart: the live paths of the latest ``paths_active`` checkpoint; ended ones get their path_end.
        Call it before the candidate loop starts. Never raises; returns the number of paths resumed."""
        try:
            last = self.store.latest_event(CHECKPOINT)
            if last is None:
                return 0
            now, restored, expired = self.clock(), 0, []
            for start_id in last[1].get('active', []):
                obs = self.store.observation(int(start_id))
                if obs is None or obs['kind'] != 'path_start':
                    self._fail('RESUME_ROW_INVALID')
                    continue
                meta = obs['meta']
                path = _Path(Target(**meta['target']), float(meta['started']), float(meta['ends']), meta.get('candidate_id'),
                             int(start_id))
                if path.ends <= now:
                    expired.append(path)
                    continue
                with self._lock:
                    if path.target.mint not in self._paths:
                        self._paths[path.target.mint] = path
                        restored += 1
            self._bump('resumed', restored)
            if expired:
                self._end(expired, now, 'EXPIRED_WHILE_DOWN')
            self._checkpoint(now)
            return restored
        except Exception as error:                                     # noqa: BLE001
            self._fail('RESUME_FAILED', error)
            return 0

    # -- polling --------------------------------------------------------------------------------------------------
    def poll(self):
        """One round: end expired paths, then ONE getMultipleAccounts per <=50 pools (low lane) and one store write.
        Never raises; returns the number of marks written."""
        try:
            return self._poll()
        except Exception as error:                                     # noqa: BLE001
            self._fail('POLL_FAILED', error)
            return 0

    def _poll(self):
        now = self.clock()
        with self._lock:
            self.stats['polls'] += 1
            self.last_poll_at = now
            expired = [p for p in self._paths.values() if p.start_id is not None and now >= p.ends]
        if expired:
            self._end(expired, now, 'COMPLETE')
        with self._lock:
            due = sorted((p for p in self._paths.values() if p.start_id is not None), key=lambda p: p.start_id)
        rows, marked, gaps = [], [], []
        deadline = now + self.interval_s * PACE_BUDGET
        for offset in range(0, len(due), POOLS_PER_CALL):
            chunk = due[offset:offset + POOLS_PER_CALL]
            keys = [k for p in chunk for k in (p.target.base_vault, p.target.quote_vault)]
            try:
                result = self._call(keys, deadline)
            except ProviderError as error:
                if error.code == LANE_SHED:                            # nothing was sent: yield, the rest are gaps
                    self._bump('shed')
                    gaps += due[offset:]
                    break
                self._bump('calls')
                self._fail('PROVIDER', error)
                gaps += chunk
                continue
            except Exception as error:                                 # noqa: BLE001
                self._fail('POLL_CALL_FAILED', error)
                gaps += chunk
                continue
            self._bump('calls')
            at, slot = self.clock(), result['context']['slot']
            for i, path in enumerate(chunk):
                rows.append(self._mark_row(path, result['value'][2 * i], result['value'][2 * i + 1], at, slot))
                marked.append(path)
        written = 0
        if rows:
            try:
                written = self.store.add_observations(rows, code_version=self.code_version,
                                                      strategy_version=self.strategy_version)
            except Exception as error:                                 # noqa: BLE001 - a failed write is a gap
                self._bump('write_failures')
                self._fail('WRITE_FAILED', error)
                gaps, marked = gaps + marked, []
        with self._lock:
            for path in marked:
                path.marks += 1
            for path in gaps:
                path.gaps += 1
            self.stats['marks'] += written
            self.stats['gaps'] += len(gaps)
        return written

    def _call(self, keys, deadline):
        """One low-lane getMultipleAccounts. The lane never waits; when it has no token RIGHT NOW (not a 429 shed),
        this recorder thread paces itself (sleeps one token interval, holding no lock) while the poll is inside its
        time budget, so a large batch spreads over the interval instead of bursting. A shed window is never waited."""
        while True:
            try:
                return self.helius.get_multiple_accounts(keys)[0]
            except ProviderError as error:
                pace = self._pace_s()
                if error.code != LANE_SHED or error.meta.get('why') != 'no_token' or self.clock() + pace > deadline:
                    raise
                self._bump('paced')
                self.sleep(pace)

    def _pace_s(self):
        limiter = getattr(getattr(self.helius, 'transport', None), 'limiter', None)
        rate = getattr(limiter, 'rate', None)
        return 1.0 / rate if isinstance(rate, float) and rate > 0 else 0.5

    def _mark_row(self, path, base_account, quote_account, at, slot):
        t = path.target
        status, base, quote, net, liquidity = 'OK', None, None, None, None
        if base_account is None or quote_account is None:
            status = 'ACCOUNT_MISSING'
        else:
            try:
                base, quote = A.vault_amount(base_account), A.vault_amount(quote_account)
            except A.AdapterError:
                status, base, quote = 'MALFORMED', None, None
        if status == 'OK':
            liquidity = format(A.sol(2 * max(0, quote - t.fee_raw)), 'f')
            try:
                net = format(A.mark(t.qty_raw, base, quote, pool_fee_bps=self.pool_fee_bps, pcfg=self.pcfg), 'f')
            except A.AdapterError:
                status = 'EMPTY_POOL'
        evidence = {'base_raw': base, 'quote_raw': quote, 'slot': slot}
        return {'kind': 'path_mark', 'mint': t.mint, 'candidate_id': path.candidate_id, 'ts': at, 'raw': _canonical(evidence),
                'meta': {**evidence, 'ts': at, 'net_sol': net, 'liquidity_sol': liquidity, 'status': status}}

    def _end(self, paths, now, why):
        with self._lock:
            for path in paths:
                if self._paths.get(path.target.mint) is path:
                    del self._paths[path.target.mint]
        rows = [{'kind': 'path_end', 'mint': p.target.mint, 'candidate_id': p.candidate_id, 'ts': now,
                 'raw': _canonical(body), 'meta': body}
                for p in paths
                for body in [{'mint': p.target.mint, 'why': why, 'started': p.started, 'ends': p.ends, 'ended': now,
                              'start_id': p.start_id, 'marks_this_process': p.marks, 'gaps_this_process': p.gaps}]]
        try:
            self.store.add_observations(rows, code_version=self.code_version, strategy_version=self.strategy_version)
            self._bump('ended', len(rows))
        except Exception as error:                                     # noqa: BLE001
            self._bump('write_failures')
            self._fail('WRITE_FAILED', error)
        self._checkpoint(now)

    # -- the thread -----------------------------------------------------------------------------------------------
    def run(self, stop, *, monotonic=time.monotonic):
        """Poll every ``interval_s`` until ``stop`` (a threading.Event) is set. For its own thread; never raises."""
        while not stop.is_set():
            began = monotonic()
            try:
                self.poll()
            except BaseException as error:                             # noqa: B902 - the trader must never notice
                self._fail('POLL_FAILED', error)
            stop.wait(max(0.0, self.interval_s - (monotonic() - began)))


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def build(cfg, *, store, keys, pcfg, code_version, strategy_version, pool_fee_bps, clock=time.time, transport_kwargs=None):
    """The recorder of a lean.json ``paths`` config (already validated by ``config``), on the LOW lane; None if disabled."""
    if not cfg['enabled']:
        return None
    from lean import providers
    transport_kwargs = dict(transport_kwargs or {})
    helius = providers.low(keys, **transport_kwargs).helius
    return PathRecorder(store=store, helius=helius, pcfg=pcfg, code_version=code_version, strategy_version=strategy_version,
                        pool_fee_bps=pool_fee_bps, path_hours=cfg['path_hours'], interval_s=cfg['interval_s'],
                        ref_size_sol=cfg['ref_size_sol'], max_paths=cfg['max_paths'], clock=clock,
                        sleep=transport_kwargs.get('sleep', time.sleep))
