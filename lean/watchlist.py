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

Soft reasons (conditions that change with time or with the portfolio, not properties of the token):
  screen   TOO_YOUNG  MARKET_CAP_BELOW_MIN  MARKET_CAP_ABOVE_MAX  LIQUIDITY_BELOW_MIN
  entry    MARKET_CAP  LIQUIDITY  COST_BUDGET  BELOW_MINIMUM  and the portfolio states MAX_POSITIONS  COOLDOWN
           ENTRY_THROTTLE  LOSS_STREAK_PAUSE  DAILY_LOSS_STOP
NOT soft (examples): TOO_OLD (it only gets older), MARKET_CAP_UNKNOWN / LIQUIDITY_UNKNOWN (missing evidence),
ALREADY_HELD, every mint/pool/vault/LP/holder/fee-config reason, stale or contradictory quotes, any PROVIDER_ERROR.
"""
import json
from dataclasses import dataclass

from lean import candidates as C

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
    watch_hours: float = 6.0
    watch_max_per_pass: int = 5

    KEYS = ('enabled', 'watch_interval_s', 'watch_hours', 'watch_max_per_pass')

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
        return cfg


def classify(kind, action, reasons):
    """'SOFT' (re-screen), 'HAZARD' (never), or None (not a rejection: PASS / BUY / FAILED)."""
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
    return 'SOFT' if reasons and all(r in allowed for r in reasons) else 'HAZARD'


class Watchlist:
    def __init__(self, store, cfg, *, clock, code_version, strategy_version):
        self.store, self.cfg, self.clock = store, cfg, clock
        self.code_version, self.strategy_version = code_version, strategy_version
        self.entries = {}              # mint -> {'first_at', 'next_at', 'source'}
        self.ended = set()             # mints that left the list for good
        self._load()

    # -- state ----------------------------------------------------------------------------------------------------
    def _event(self, kind, payload):
        self.store.record(kind, payload, code_version=self.code_version, strategy_version=self.strategy_version, ts=self.clock())

    def _load(self):
        now = self.clock()
        for row in self.store.rows('events', kind='watch_add', limit=100000):
            p = _payload(row)
            if isinstance(p.get('mint'), str):
                # next_at = now: a restart re-screens soon, bounded per pass by watch_max_per_pass
                self.entries[p['mint']] = {'first_at': float(p.get('first_at', row['ts'])), 'next_at': now,
                                           'source': p.get('source') or DEFAULT_SOURCE}
        for row in self.store.rows('events', kind='watch_end', limit=100000):
            mint = _payload(row).get('mint')
            self.entries.pop(mint, None)
            self.ended.add(mint)

    def _end(self, mint, why):
        if mint in self.entries:
            self._event('watch_end', {'mint': mint, 'why': why, 'at': self.clock()})
            del self.entries[mint]
            self.ended.add(mint)

    def _source(self, mint):
        rows = self.store.rows('candidates', mint=mint, limit=1)
        try:
            meta = json.loads(rows[0]['meta'])
            return meta['source'] if isinstance(meta.get('source'), str) else DEFAULT_SOURCE
        except (IndexError, TypeError, ValueError, AttributeError):
            return DEFAULT_SOURCE

    def candidate(self, mint):
        rows = self.store.rows('candidates', mint=mint, limit=1)
        if not rows:
            return None
        row = rows[0]
        try:
            meta = json.loads(row['meta'])
            payload_hash = meta.get('payload_hash') if isinstance(meta, dict) else None
            return C.Candidate(seq=int(row['hint_seq'] or 0), mint=mint, pool=row['pool'], signature=row['signature'],
                               slot=int(row['slot']), migrated_at=float(row['migrated_at']), payload_hash=payload_hash or '')
        except (TypeError, ValueError, KeyError):
            return None

    # -- hooks ----------------------------------------------------------------------------------------------------
    def after_handle(self, candidate):
        """Called after every screen/entry handling of ``candidate`` (new or re-screen): add, keep or drop."""
        if not self.cfg.enabled or candidate.mint in self.ended:
            return
        outcome = self._latest_outcome(candidate.mint)
        mint = candidate.mint
        if outcome is None:                                   # provider failure / nothing decided: state unchanged
            return
        kind, action, label = outcome[:3]
        if label == 'SOFT':
            if mint not in self.entries:
                now = self.clock()
                source = self._source(mint)
                self._event('watch_add', {'mint': mint, 'source': source, 'first_at': now, 'first_reasons': _reasons_of(outcome)})
                self.entries[mint] = {'first_at': now, 'next_at': now + self.cfg.watch_interval_s, 'source': source}
            return
        if mint in self.entries:
            self._end(mint, 'ENTERED' if action == 'BUY' else 'HAZARD')

    def _latest_outcome(self, mint):
        rows = [r for r in self.store.rows('decisions', mint=mint, limit=100000) if r['kind'] in ('screen', 'entry')]
        if not rows:
            return None
        last = rows[-1]
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

    def due(self):
        """Mints to re-screen now (oldest first, at most ``watch_max_per_pass``); expired ones leave the list here."""
        if not self.cfg.enabled:
            return []
        now, due = self.clock(), []
        for mint, entry in sorted(self.entries.items(), key=lambda kv: kv[1]['next_at']):
            if entry['next_at'] > now:
                continue
            if now - entry['first_at'] > self.cfg.watch_hours * 3600:
                self._end(mint, 'EXPIRED')
                continue
            due.append(mint)
        return due[:self.cfg.watch_max_per_pass]

    def rescreen(self, handle):
        """``handle(candidate, source)`` runs the normal screen/entry path for each due mint."""
        done = 0
        for mint in self.due():
            entry = self.entries.get(mint)
            if entry is None:
                continue
            candidate = self.candidate(mint)
            if candidate is None:
                self._end(mint, 'CANDIDATE_MISSING')
                continue
            now = self.clock()
            entry['next_at'] = now + self.cfg.watch_interval_s    # before the work: a failure never hot-loops
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
