"""Price-path recorder: strategy-tuning data for every candidate that passed the HAZARD checks, entered or not.

Paper only. Read-only: ``getMultipleAccounts`` of the two pool vault accounts, nothing else. A recorder failure NEVER reaches the
trader: ``start`` and ``poll`` swallow everything, count it, and go on.

What is recorded (rows of ``observations``; append-only like everything in the store):
* ``path_start`` once: the screen features, whether the candidate was entered and the reason it was not, and the pool vault targets
  (so a restarted process can ``resume`` the path).
* ``path_mark`` every ``interval_s`` for ``path_hours``: ``meta`` = ``{price_sol, liquidity_sol, ts, slot, base_raw, quote_raw, status, n}``;
  ``raw`` = the same fields as canonical JSON. ``status`` is OK / ACCOUNT_MISSING (a closed vault: a rug signal worth keeping) /
  EMPTY_POOL / MALFORMED. The raw account bytes are NOT kept (a poll of 100 accounts is about 30 KB; the reserves are in the mark).
* ``path_end`` once: marks written, gaps, and why it ended.

Hazard filter: a candidate is tracked when its screen passed, or when it failed ONLY for the market-stage reasons (market cap or
liquidity outside the window) and the market features exist. Everything the screen rejects for a hazard (authorities, extensions, pool
or vault evidence, holders, provider errors) is not tracked. Entry-stage skips (cost cap, portfolio caps, cooldown) are tracked.

Pricing follows ``lean.candidates._market``: ``price = (quote_gross + virtual_quote) / 1e9 / (base_raw / 10**decimals)`` and
``liquidity_sol = 2 * (quote_gross - fees_at_screen) / 1e9``. The pool-account fees are taken from the screen (they sit in the pool
account, not in the vaults; re-reading it would cost a third account per pool). ``base_raw`` / ``quote_raw`` are stored so a later
analysis can re-price with any other definition.

Budget: an own token bucket (default 4 requests/s = 40 % of the 10 req/s Helius budget), at most 100 ACCOUNTS per call (the RPC limit),
that is 50 pools per call (two vaults each). The recorder yields first: a transient provider error (429, 5xx, timeout) pauses it for
30 s, doubling to 10 min, and the missed marks are counted as gaps, never retried at once. Give it a DEDICATED Helius client (see
``dedicated_helius``): the shared limiter of the trader must not be blocked by the recorder's 429.
"""
import json
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, localcontext

from desk.graduation_witness import _pda
from desk.pools import ATA
from desk.programs import unbase58
from desk.security import TOKEN_2022, TOKEN_PROGRAM, base58

SOL_MINT = 'So11111111111111111111111111111111111111112'
NON_HAZARD_REASONS = frozenset({'MARKET_CAP_BELOW_MIN', 'MARKET_CAP_ABOVE_MAX', 'LIQUIDITY_BELOW_MIN'})
KINDS = ('path_start', 'path_mark', 'path_end')
ACCOUNTS_PER_POOL = 2
LAMPORTS = 10 ** 9
TOKEN_ACCOUNT_MIN = 72          # mint (32) + owner (32) + amount (8)


def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _ata(pool, program, mint):
    return _pda([unbase58(pool), unbase58(program), unbase58(mint)], ATA)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, default=str).encode()


# ------------------------------------------------------------------------------------------------- targets
@dataclass(frozen=True)
class PathTarget:
    mint: str
    pool: str
    base_vault: str
    quote_vault: str
    token_program: str
    decimals: int
    virtual_quote_raw: int
    fee_raw: int

    def as_dict(self):
        return dict(self.__dict__)


def hazard_free(screen):
    """True when the screen passed, or failed only for market-stage reasons (cap / liquidity outside the window)."""
    if _get(screen, 'passed'):
        return True
    reasons = list(_get(screen, 'reasons', ()) or ())
    return bool(reasons) and all(r in NON_HAZARD_REASONS for r in reasons)


def target_from_screen(candidate, screen):
    """The pool vault targets for a tracked candidate, or None (with nothing recorded) when the screen lacks the market features.

    No request is made: the base vault is the pool's ATA for the token program the screen found, the quote vault the pool's WSOL ATA."""
    if not hazard_free(screen):
        return None
    features = _get(screen, 'features', {}) or {}
    try:
        mint, pool = _get(candidate, 'mint'), _get(candidate, 'pool')
        program, decimals = features['token_program'], features['decimals']
        virtual, gross, spendable = (int(features[k]) for k in ('virtual_quote_reserves_raw', 'quote_gross_raw', 'quote_spendable_raw'))
        int(features['base_reserve_raw'])
        if (program not in (TOKEN_PROGRAM, TOKEN_2022) or type(decimals) is not int or not 0 <= decimals <= 30
                or not isinstance(mint, str) or not isinstance(pool, str) or virtual < 0 or gross < 0 or spendable > gross):
            return None
        return PathTarget(mint, pool, _ata(pool, program, mint), _ata(pool, TOKEN_PROGRAM, SOL_MINT), program, decimals, virtual, gross - spendable)
    except (KeyError, TypeError, ValueError):
        return None


def _amount(data, expected_mint, expected_owner):
    if base58(bytes(data[0:32])) != expected_mint or base58(bytes(data[32:64])) != expected_owner:
        return None, 'MALFORMED'
    return int.from_bytes(data[64:72], 'little'), None


def compute_mark(target, base_raw, quote_raw):
    """``(price_sol, liquidity_sol)`` as plain-decimal strings, or None when the pool is empty."""
    if base_raw <= 0 or quote_raw <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 60
        base_ui = Decimal(base_raw) / (Decimal(10) ** target.decimals)
        price = (Decimal(quote_raw + target.virtual_quote_raw) / LAMPORTS) / base_ui
        liquidity = 2 * (Decimal(max(0, quote_raw - target.fee_raw)) / LAMPORTS)
    return format(price, 'f'), format(liquidity, 'f')


# ------------------------------------------------------------------------------------------------- budget
class ShareBucket:
    """A token bucket that never sleeps: ``take()`` is True when a request may go now. Thread safe."""

    def __init__(self, rate_per_second, burst=None, *, clock=time.monotonic):
        if not isinstance(rate_per_second, (int, float)) or not 0 < rate_per_second <= 1000:
            raise ValueError('rate out of range')
        self.rate, self.burst, self._clock = float(rate_per_second), float(burst if burst is not None else max(1.0, rate_per_second)), clock
        self._tokens, self._stamp, self._lock = self.burst, clock(), threading.Lock()

    def take(self):
        with self._lock:
            now = self._clock()
            self._tokens = min(self.burst, self._tokens + (now - self._stamp) * self.rate)
            self._stamp = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


def dedicated_helius(key, *, rate=4.0, **kwargs):
    """A Helius client with its OWN rate limiter, for the recorder only (40 % of 10 req/s by default), so a 429 on recorder traffic
    can never block the trader's limiter."""
    from lean.providers import Helius, RateLimiter, Transport
    kwargs.setdefault('max_attempts', 1)          # the recorder never retries: a failed poll is a gap, the next one comes in 15 s
    return Helius(key, transport=Transport('helius', RateLimiter(rate, max(1, int(rate))), **kwargs))


# ------------------------------------------------------------------------------------------------- recorder
@dataclass
class _Path:
    target: PathTarget
    started: float
    ends: float
    next_due: float
    candidate_id: int = None
    marks: int = 0
    gaps: int = 0


class PathRecorder:
    def __init__(self, *, store, helius, code_version, strategy_version, path_hours=6.0, interval_s=15.0, accounts_per_call=100,
                 bucket=None, clock=time.time, max_paths=2000, shed_base_s=30.0, shed_max_s=600.0, error_backoff_s=5.0):
        if not 0 < path_hours <= 72 or not 1 <= interval_s <= 3600 or not 2 <= accounts_per_call <= 100:
            raise ValueError('recorder settings out of range')
        self.store, self.helius, self.clock = store, helius, clock
        self.code_version, self.strategy_version = code_version, strategy_version
        self.path_s, self.interval_s, self.accounts_per_call = float(path_hours) * 3600, float(interval_s), int(accounts_per_call)
        self.bucket = bucket or ShareBucket(4.0, 4, clock=time.monotonic)
        self.max_paths, self.shed_base_s, self.shed_max_s, self.error_backoff_s = max_paths, shed_base_s, shed_max_s, error_backoff_s
        self.paths = {}                       # mint -> _Path
        self.pause_until, self._shed_level = 0.0, 0
        self.lock = threading.RLock()
        self.stats = {'started': 0, 'marks': 0, 'calls': 0, 'ended': 0, 'gaps': 0, 'shed': 0, 'dropped_capacity': 0, 'errors': 0,
                      'bucket_skips': 0}
        self.last_error = None

    # -- writing ---------------------------------------------------------------------------------------------------------
    def _write(self, kind, mint, payload, meta, candidate_id=None, ts=None):
        return self.store.add_observation(kind, _canonical(payload), mint=mint, candidate_id=candidate_id, meta=meta,
                                          code_version=self.code_version, strategy_version=self.strategy_version, ts=ts)

    def _count_error(self, error):
        self.stats['errors'] += 1
        self.last_error = type(error).__name__

    # -- starting a path ------------------------------------------------------------------------------------------------
    def start(self, candidate, screen, *, entered, reason=None, candidate_id=None):
        """Begin recording ``candidate`` if its screen shows no hazard. Never raises; True when a path was started."""
        try:
            target = target_from_screen(candidate, screen)
            if target is None:
                return False
            now = self.clock()
            with self.lock:
                if target.mint in self.paths:
                    return False
                if len(self.paths) >= self.max_paths:
                    self.stats['dropped_capacity'] += 1
                    return False
                reasons = list(_get(screen, 'reasons', ()) or ())
                note = reason if reason is not None else (';'.join(reasons) if reasons else None)
                payload = {'mint': target.mint, 'pool': target.pool, 'features': _get(screen, 'features', {}), 'screen_passed': bool(_get(screen, 'passed')),
                           'screen_reasons': reasons, 'entered': bool(entered), 'reason': note, 'path_hours': self.path_s / 3600,
                           'interval_s': self.interval_s, 'target': target.as_dict(), 'started': now}
                self._write('path_start', target.mint, payload, {'entered': bool(entered), 'reason': note, 'started': now,
                                                                  'ends': now + self.path_s, 'target': target.as_dict()}, candidate_id, now)
                self.paths[target.mint] = _Path(target, now, now + self.path_s, now, candidate_id)
                self.stats['started'] += 1
            return True
        except Exception as error:                 # noqa: BLE001 - a recorder failure never reaches the trader
            self._count_error(error)
            return False

    def resume(self, now=None):
        """Rebuild the live paths after a restart from ``path_start`` rows that have no ``path_end`` and have not expired."""
        try:
            now = self.clock() if now is None else now
            horizon = now - self.path_s
            starts = self.store.rows('observations', where="WHERE kind='path_start' AND ts>?", args=(horizon,), limit=100000)
            ended = {r['mint'] for r in self.store.rows('observations', where="WHERE kind='path_end' AND ts>?", args=(horizon,), limit=100000)}
            restored = 0
            with self.lock:
                for row in starts:
                    meta = json.loads(row['meta'])
                    mint = row['mint']
                    if mint in ended or mint in self.paths or meta.get('ends', 0) <= now:
                        continue
                    target = PathTarget(**meta['target'])
                    done = self.store.rows('observations', where="WHERE kind='path_mark' AND mint=? AND ts>=?", args=(mint, meta['started']), limit=100000)
                    self.paths[mint] = _Path(target, meta['started'], meta['ends'], now, row['candidate_id'], marks=len(done))
                    restored += 1
            return restored
        except Exception as error:                 # noqa: BLE001
            self._count_error(error)
            return 0

    # -- one polling round --------------------------------------------------------------------------------------------------
    def poll(self, now=None):
        """Mark every due path (batched), close expired ones. Never raises; returns the number of marks written."""
        try:
            return self._poll(self.clock() if now is None else now)
        except Exception as error:                 # noqa: BLE001
            self._count_error(error)
            return 0

    def _poll(self, now):
        written = 0
        with self.lock:
            pools_per_call = max(1, self.accounts_per_call // ACCOUNTS_PER_POOL)
            if now < self.pause_until:
                self.stats['shed'] += 1
                due = []
            else:
                due = sorted((p for p in self.paths.values() if p.next_due <= now < p.ends), key=lambda p: p.next_due)
            for offset in range(0, len(due), pools_per_call):
                chunk = due[offset:offset + pools_per_call]
                if not self.bucket.take():
                    self.stats['bucket_skips'] += 1                     # over our own share: skip, do not queue
                    for p in chunk:
                        p.gaps += 1
                        self.stats['gaps'] += 1
                    continue
                written += self._call(chunk, now)
                if now < self.pause_until:
                    for p in due[offset + pools_per_call:]:             # the provider pushed back: the rest wait, counted as gaps
                        p.gaps += 1
                        self.stats['gaps'] += 1
                    break
            for mint in [m for m, p in self.paths.items() if now >= p.ends]:
                self._end(mint, now, 'COMPLETE')
        return written

    def _call(self, chunk, now):
        keys = []
        for p in chunk:
            keys += [p.target.base_vault, p.target.quote_vault]
        self.stats['calls'] += 1
        try:
            parsed, raw, meta = self.helius.get_multiple_accounts(keys)
        except Exception as error:                 # noqa: BLE001
            transient = bool(getattr(error, 'transient', False))
            self._count_error(error)
            self.last_error = str(getattr(error, 'code', type(error).__name__))[:64]
            if transient:
                self.pause_until = now + min(self.shed_max_s, self.shed_base_s * 2 ** self._shed_level)
                self._shed_level = min(self._shed_level + 1, 10)
            else:
                self.pause_until = now + self.error_backoff_s
            for p in chunk:
                p.gaps += 1
                self.stats['gaps'] += 1
                p.next_due = now + self.interval_s
            return 0
        self._shed_level = 0
        accounts = parsed.get('accounts') if isinstance(parsed, dict) else None
        slot = parsed.get('slot') if isinstance(parsed, dict) else None
        if not isinstance(accounts, list) or len(accounts) != len(keys):
            self._count_error(ValueError('RESPONSE_SHAPE'))
            for p in chunk:
                p.gaps += 1
                self.stats['gaps'] += 1
                p.next_due = now + self.interval_s
            return 0
        written = 0
        for i, p in enumerate(chunk):
            t = p.target
            base, quote = accounts[2 * i], accounts[2 * i + 1]
            status, base_raw, quote_raw, price, liquidity = 'OK', None, None, None, None
            b_amount, b_error = _vault(base, t.mint, t.pool, t.token_program)
            q_amount, q_error = _vault(quote, SOL_MINT, t.pool, TOKEN_PROGRAM)
            if b_error or q_error:
                status = 'ACCOUNT_MISSING' if 'ACCOUNT_MISSING' in (b_error, q_error) else 'MALFORMED'
            else:
                base_raw, quote_raw = b_amount, q_amount
                computed = compute_mark(t, base_raw, quote_raw)
                if computed is None:
                    status = 'EMPTY_POOL'
                else:
                    price, liquidity = computed
            p.marks += 1
            body = {'ts': now, 'price_sol': price, 'liquidity_sol': liquidity, 'slot': slot, 'base_raw': base_raw, 'quote_raw': quote_raw,
                    'status': status, 'n': p.marks}
            try:
                self._write('path_mark', t.mint, body, body, p.candidate_id, now)
                written += 1
                self.stats['marks'] += 1
            except Exception as error:             # noqa: BLE001 - a failed write is a gap, never an exception
                p.marks -= 1
                p.gaps += 1
                self.stats['gaps'] += 1
                self._count_error(error)
            p.next_due = now + self.interval_s
        return written

    def _end(self, mint, now, why):
        p = self.paths.pop(mint, None)
        if p is None:
            return
        try:
            body = {'mint': mint, 'ended': now, 'why': why, 'marks': p.marks, 'gaps': p.gaps, 'started': p.started}
            self._write('path_end', mint, body, body, p.candidate_id, now)
            self.stats['ended'] += 1
        except Exception as error:                 # noqa: BLE001
            self._count_error(error)

    def stop_all(self, now=None):
        """On shutdown: leave the paths open (``resume`` continues them); nothing is written."""
        return len(self.paths)

    def run(self, stop, tick=1.0):
        """Poll until ``stop`` (a threading.Event) is set. Meant for its own thread."""
        self.resume()
        while not stop.is_set():
            self.poll()
            stop.wait(tick)

    def health(self):
        with self.lock:
            return {'active_paths': len(self.paths), 'paused_until': self.pause_until or None, 'last_error': self.last_error, **self.stats}


def _vault(account, mint, owner, program):
    if account is None:
        return None, 'ACCOUNT_MISSING'
    data = account.get('data') if isinstance(account, dict) else None
    if not isinstance(data, (bytes, bytearray)) or len(data) < TOKEN_ACCOUNT_MIN or account.get('owner') != program:
        return None, 'MALFORMED'
    return _amount(data, mint, owner)
