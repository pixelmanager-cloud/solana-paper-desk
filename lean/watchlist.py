"""Watchlist: re-screen candidates that were rejected for SOFT reasons (L14).

A candidate whose latest outcome is a rejection and EVERY reason of that rejection is in the soft sets below goes on
the watchlist and is screened again every ``watch_interval_s`` for up to ``watch_hours`` after the first rejection.
A reason that is not listed is a hazard: the rule is an allow-list, so an unknown, new or malformed reason can only
make a candidate LESS likely to be re-screened. Hazard rejects are never re-screened, and a watched candidate whose
re-screen finds a hazard leaves the list at once (``HAZARD``). Provider failures (``FAILED`` screens, quote errors)
neither add nor remove: they belong to the runner's own retry path.

State is append-only events in the store (``watch_add``, ``watch_rescreen``, ``watch_end``) and is rebuilt from them
at start, so a restart keeps the list and a mint that ended is never watched again. Re-screens re-run the SAME
screen and entry decision as a new candidate; nothing about an entry is relaxed. Nothing here quotes, signs or sends.

Cost control (L14F)
  * Re-screens run AFTER the fresh scan of new frames, so new candidates are never delayed by old ones.
  * Before any re-screen, the ledger-only ``strategy.portfolio_blockers`` (NO I/O) is asked: a mint refused only by the
    portfolio (MAX_POSITIONS while the book is full, COOLDOWN, ENTRY_THROTTLE, a loss pause) is skipped this pass, stays
    on the list, and costs nothing. The portfolio is rebuilt only after a re-screen could have changed it.
  * Hard cap: ``watch_max_rescreens_per_hour`` (default 120, persisted: it counts the ``watch_rescreen`` events of the last
    hour, so a restart cannot reset it) and ``watch_max_per_pass`` per pass.
  * Provider calls per re-screen: the screen makes 2 Helius ``getMultipleAccounts`` and, with ``holder_check``, 1
    ``getTokenLargestAccounts`` (SOL/USD is the runner's 30 s cached Kraken value); a screen that passes and wants to
    buy adds 2 Jupiter quotes. At the default cap that is at most 120 x 3 = 360 Helius and 120 x 2 = 240 Jupiter calls per
    hour, whatever the list length.

Ending a watch: ``ENTERED`` on ANY fill for the mint (checked in the store before every re-screen, so a halt between a BUY
and the bookkeeping cannot buy twice), ``HAZARD`` when a re-screen finds a hazard, ``EXPIRED`` at the earlier of
``watch_hours`` after the first rejection and ``max_age_seconds`` after the migration (TOO_OLD is the screen's own age
limit, so it is an expiry, not a hazard). ``watch_hours`` may not exceed the screen's ``max_age_seconds`` (``check_window``).

Soft reasons (conditions that change with time or with the portfolio, not properties of the token):
  screen   TOO_YOUNG  MARKET_CAP_BELOW_MIN  MARKET_CAP_ABOVE_MAX  LIQUIDITY_BELOW_MIN
  entry    MARKET_CAP  LIQUIDITY  COST_BUDGET  BELOW_MINIMUM  and the portfolio states MAX_POSITIONS  COOLDOWN
           ENTRY_THROTTLE  LOSS_STREAK_PAUSE  DAILY_LOSS_STOP
NOT soft (examples): TOO_OLD (it only gets older), MARKET_CAP_UNKNOWN / LIQUIDITY_UNKNOWN (missing evidence),
ALREADY_HELD, every mint/pool/vault/LP/holder/fee-config reason, stale or contradictory quotes, any PROVIDER_ERROR.
"""
import json
from collections import deque
from dataclasses import dataclass

from lean import candidates as C, strategy as S
from lean.sources import iter_rows

SOFT_SCREEN = frozenset({'TOO_YOUNG', 'MARKET_CAP_BELOW_MIN', 'MARKET_CAP_ABOVE_MAX', 'LIQUIDITY_BELOW_MIN'})
SOFT_ENTRY = frozenset({'MARKET_CAP', 'LIQUIDITY', 'COST_BUDGET', 'BELOW_MINIMUM', 'MAX_POSITIONS', 'COOLDOWN',
                        'ENTRY_THROTTLE', 'LOSS_STREAK_PAUSE', 'DAILY_LOSS_STOP'})
RESCREEN_ATTEMPT = 1_000_000           # decision feature 'attempt' of a re-screen: > any retry count, so never re-queued
DEFAULT_SOURCE = 'pump_graduation'


class WatchConfigError(ValueError):
    pass


@dataclass(frozen=True)
class WatchConfig:
    enabled: bool = False              # off unless configured: absent config keeps today's behaviour
    watch_interval_s: float = 300.0
    watch_hours: float = 2.0           # = the screen's default max_age_seconds (7200): a watch cannot outlive the age limit
    watch_max_per_pass: int = 5
    watch_max_rescreens_per_hour: int = 120

    KEYS = ('enabled', 'watch_interval_s', 'watch_hours', 'watch_max_per_pass', 'watch_max_rescreens_per_hour')

    @classmethod
    def from_dict(cls, value):
        if value is None:
            return cls()
        if not isinstance(value, dict) or set(value) - set(cls.KEYS):
            raise WatchConfigError('watchlist keys: %s' % (cls.KEYS,))
        cfg = cls(**value)
        if type(cfg.enabled) is not bool:
            raise WatchConfigError('enabled must be a boolean')
        for name, low, high in (('watch_interval_s', 30, 86400), ('watch_hours', 0, 168)):
            v = getattr(cfg, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not low <= v <= high:
                raise WatchConfigError('%s must be a number in [%s, %s]' % (name, low, high))
        if type(cfg.watch_max_per_pass) is not int or not 1 <= cfg.watch_max_per_pass <= 100:
            raise WatchConfigError('watch_max_per_pass must be an int in [1, 100]')
        if type(cfg.watch_max_rescreens_per_hour) is not int or not 1 <= cfg.watch_max_rescreens_per_hour <= 100000:
            raise WatchConfigError('watch_max_rescreens_per_hour must be an int in [1, 100000]')
        return cfg


def check_window(cfg, max_age_seconds):
    """An enabled watchlist must not promise longer than the screen allows: past ``max_age_seconds`` every re-screen is TOO_OLD."""
    if cfg.enabled and cfg.watch_hours * 3600 > max_age_seconds:
        raise WatchConfigError('watch_hours (%s h) exceeds the screen max_age_seconds (%s s): lower watch_hours or raise '
                               'screen.max_age_seconds' % (cfg.watch_hours, max_age_seconds))


def classify(kind, action, reasons):
    """'SOFT' (re-screen), 'EXPIRED' (the age limit alone), 'HAZARD' (never), or None (not a rejection: PASS / BUY / FAILED)."""
    if kind == 'screen':
        if action == 'PASS' or action == 'FAILED':
            return None
        allowed = SOFT_SCREEN
    elif kind == 'entry':
        if action == 'BUY':
            return None
        allowed = SOFT_ENTRY
    else:
        return None
    reasons = [str(r) for r in reasons or ()]
    if kind == 'screen' and 'TOO_OLD' in reasons and all(r == 'TOO_OLD' or r in allowed for r in reasons):
        return 'EXPIRED'                      # only the age limit: the watch ran out, it is not a token hazard
    return 'SOFT' if reasons and all(r in allowed for r in reasons) else 'HAZARD'


class Watchlist:
    def __init__(self, store, cfg, *, clock, code_version, strategy_version, max_age_seconds=None):
        self.store, self.cfg, self.clock = store, cfg, clock
        self.code_version, self.strategy_version = code_version, strategy_version
        self.max_age_seconds = max_age_seconds     # the screen's age limit (None = not enforced here)
        self.entries = {}              # mint -> {'first_at', 'next_at', 'source', 'migrated_at'}
        self.ended = set()             # mints that left the list for good
        self.recent = deque()          # timestamps of the re-screens of the last hour (the hard cap)
        self.stats = {'portfolio_skipped': 0, 'capped': 0}
        self._load()

    # -- state ----------------------------------------------------------------------------------------------------
    def _event(self, kind, payload):
        self.store.record(kind, payload, code_version=self.code_version, strategy_version=self.strategy_version, ts=self.clock())

    def _load(self):
        now = self.clock()
        for row in iter_rows(self.store, 'events', kind='watch_add'):
            p = _payload(row)
            if isinstance(p.get('mint'), str):
                # next_at = now: a restart re-screens soon, bounded per pass by watch_max_per_pass and per hour by the cap
                self.entries[p['mint']] = {'first_at': float(p.get('first_at', row['ts'])), 'next_at': now,
                                           'source': p.get('source') or DEFAULT_SOURCE, 'migrated_at': p.get('migrated_at')}
        for row in iter_rows(self.store, 'events', kind='watch_end'):
            mint = _payload(row).get('mint')
            self.entries.pop(mint, None)
            self.ended.add(mint)
        for row in iter_rows(self.store, 'events', kind='watch_rescreen', since_ts=now - 3600):
            self.recent.append(float(row['ts']))     # the hourly cap survives a restart

    def _end(self, mint, why):
        if mint in self.entries:
            self._event('watch_end', {'mint': mint, 'why': why, 'at': self.clock()})
            del self.entries[mint]
            self.ended.add(mint)

    def _candidate_row(self, mint):
        rows = self.store.rows('candidates', mint=mint, limit=1)
        return rows[0] if rows else None

    def _source(self, mint):
        row = self._candidate_row(mint)
        try:
            meta = json.loads(row['meta'])
            return meta['source'] if isinstance(meta.get('source'), str) else DEFAULT_SOURCE
        except (TypeError, ValueError, AttributeError, KeyError):
            return DEFAULT_SOURCE

    def candidate(self, mint):
        row = self._candidate_row(mint)
        if row is None:
            return None
        try:
            meta = json.loads(row['meta'])
            payload_hash = meta.get('payload_hash') if isinstance(meta, dict) else None
            return C.Candidate(seq=int(row['hint_seq'] or 0), mint=mint, pool=row['pool'], signature=row['signature'],
                               slot=int(row['slot']), migrated_at=float(row['migrated_at']), payload_hash=payload_hash or '')
        except (TypeError, ValueError, KeyError):
            return None

    def has_fill(self, mint):
        """ANY fill (buy or sell) for the mint: it has been traded, so its watch is over (no second entry through the list)."""
        return bool(self.store.rows('fills', mint=mint, limit=1))

    # -- hooks ----------------------------------------------------------------------------------------------------
    def after_handle(self, candidate):
        """Called after every screen/entry handling of ``candidate`` (new or re-screen): add, keep or drop."""
        if not self.cfg.enabled or candidate.mint in self.ended:
            return
        mint = candidate.mint
        if self.has_fill(mint):                               # entered by ANY path, even if the bookkeeping never ran
            self._end(mint, 'ENTERED')
            self.ended.add(mint)
            return
        outcome = self._latest_outcome(mint)
        if outcome is None:                                   # provider failure / nothing decided: state unchanged
            return
        kind, action, label = outcome[:3]
        if label == 'SOFT':
            if mint not in self.entries:
                now = self.clock()
                row = self._candidate_row(mint)
                source = self._source(mint)
                migrated = float(row['migrated_at']) if row is not None and row['migrated_at'] is not None else None
                self._event('watch_add', {'mint': mint, 'source': source, 'first_at': now, 'migrated_at': migrated,
                                          'first_reasons': _reasons_of(outcome)})
                self.entries[mint] = {'first_at': now, 'next_at': now + self.cfg.watch_interval_s, 'source': source,
                                      'migrated_at': migrated}
            return
        if mint in self.entries:
            self._end(mint, 'ENTERED' if action == 'BUY' else 'EXPIRED' if label == 'EXPIRED' else 'HAZARD')

    def _latest_outcome(self, mint):
        last = None
        for row in iter_rows(self.store, 'decisions', mint=mint):      # newest wins; paged, so no row cap
            if row['kind'] in ('screen', 'entry'):
                last = row
        if last is None:
            return None
        try:
            reasons = json.loads(last['reasons'])
        except (TypeError, ValueError):
            reasons = ['UNREADABLE']
        if last['kind'] == 'screen' and last['action'] == 'PASS':
            return None                                       # the entry decision (or its failure) has not landed
        label = classify(last['kind'], last['action'], reasons)
        if last['kind'] == 'entry' and last['action'] == 'BUY':
            return ('entry', 'BUY', 'ENTERED')
        if label is None:
            return None
        return (last['kind'], last['action'], label, tuple(str(r) for r in reasons))

    def deadline(self, entry):
        """The earlier of watch_hours after the first rejection and max_age_seconds after the migration."""
        end = entry['first_at'] + self.cfg.watch_hours * 3600
        if self.max_age_seconds is not None and isinstance(entry.get('migrated_at'), (int, float)):
            end = min(end, entry['migrated_at'] + self.max_age_seconds)
        return end

    def due(self):
        """Mints whose time has come (oldest first). Expired ones leave the list here, before any I/O. Not capped: the cap
        applies to the re-screens actually done (``rescreen``), so mints skipped for free never starve the others."""
        if not self.cfg.enabled:
            return []
        now, due = self.clock(), []
        for mint, entry in sorted(self.entries.items(), key=lambda kv: kv[1]['next_at']):
            if now > self.deadline(entry):
                self._end(mint, 'EXPIRED')
                continue
            if entry['next_at'] <= now:
                due.append(mint)
        return due

    def _prune(self, now):
        while self.recent and self.recent[0] < now - 3600:
            self.recent.popleft()

    def rescreen(self, handle, *, blocked=None):
        """``handle(candidate, source)`` runs the normal screen/entry path for each re-screened mint.

        ``blocked(mint) -> list`` is the ledger-only portfolio check (no I/O): a mint it refuses is skipped for free and stays
        listed. Stops at ``watch_max_per_pass`` re-screens, and at ``watch_max_rescreens_per_hour`` over the last hour."""
        done = 0
        for mint in self.due():
            if done >= self.cfg.watch_max_per_pass:
                break
            entry = self.entries.get(mint)
            if entry is None:
                continue
            if self.has_fill(mint):
                self._end(mint, 'ENTERED')
                continue
            if blocked is not None and blocked(mint):
                self.stats['portfolio_skipped'] += 1
                continue
            now = self.clock()
            self._prune(now)
            if len(self.recent) >= self.cfg.watch_max_rescreens_per_hour:
                self.stats['capped'] += 1
                break
            candidate = self.candidate(mint)
            if candidate is None:
                self._end(mint, 'CANDIDATE_MISSING')
                continue
            entry['next_at'] = now + self.cfg.watch_interval_s    # before the work: a failure never hot-loops
            self.recent.append(now)
            self._event('watch_rescreen', {'mint': mint, 'at': now, 'source': entry['source']})
            handle(candidate, entry['source'])
            done += 1
        return done


def _reasons_of(outcome):
    return list(outcome[3]) if len(outcome) > 3 else []


def _payload(row):
    try:
        value = json.loads(row['payload'])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------------------------------- runner wiring
# The runner carries three delimited statements; everything else lives here (no existing runner line is edited).
def install(runner):
    """Wire a Runner (called from its ``# --- L14 hook ---`` block): tag each candidate row with its source and, with a
    watchlist, update it after every handling. Both are instance-level wrappers around the runner's own functions."""
    plain_add = runner.store.add_candidate

    def add_candidate(mint, **kwargs):
        meta = dict(kwargs.get('meta') or {})
        meta.setdefault('source', runner._source_of.get(mint, DEFAULT_SOURCE))
        return plain_add(mint, **{**kwargs, 'meta': meta})
    runner.store.add_candidate = add_candidate
    if runner.watch is not None:
        plain_isolated = runner._isolated

        def isolated(candidate, attempt):
            plain_isolated(candidate, attempt)                # records its own errors; re-raises only the halt
            runner.watch.after_handle(candidate)
        runner._isolated = isolated


def _bump(runner, name, n=1):
    with runner._counts_lock:
        runner.counts[name] = runner.counts.get(name, 0) + n


def after_scan(runner):
    """After the pump scan of this pass: extra sources (own cursors), then the watchlist's re-screens. Bounded, isolated from
    the pump scan, never raises."""
    from lean.runner import _Halt
    done = 0
    try:
        if runner.extra_sources is not None:
            def handle(source, candidate):
                runner._source_of[candidate.mint] = source
                try:
                    runner._isolated(candidate, 1)
                finally:
                    runner._source_of.pop(candidate.mint, None)
            done += runner.extra_sources.poll(
                handle, exists=lambda mint: runner.store.candidate_id(mint) is not None,
                stopped=lambda: runner.stop.is_set() or not runner.entries_allowed(),
                on_error=lambda code, transient, source, message: runner._error(
                    code, transient=transient, scope='source', message='%s: %s' % (source, message)))
        watch = runner.watch
        if watch is not None and not runner.stop.is_set() and runner.entries_allowed():
            cache = {}

            def blocked(mint):                                # ledger only; rebuilt after a re-screen could have changed it
                if 'portfolio' not in cache:
                    cache['portfolio'] = runner.portfolio(runner._now())
                return S.portfolio_blockers(cache['portfolio'], runner.cfg, mint)

            def handle(candidate, source):
                runner._source_of[candidate.mint] = source
                try:
                    runner._isolated(candidate, RESCREEN_ATTEMPT)
                finally:
                    runner._source_of.pop(candidate.mint, None)
                    cache.clear()
            before = dict(watch.stats)
            n = watch.rescreen(handle, blocked=blocked)
            done += n
            _bump(runner, 'watch_rescreens', n)
            for key, counter in (('portfolio_skipped', 'watch_skipped_portfolio'), ('capped', 'watch_capped')):
                if watch.stats[key] != before[key]:
                    _bump(runner, counter, watch.stats[key] - before[key])
    except _Halt:
        pass
    return done
