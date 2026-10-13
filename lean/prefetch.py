"""W2b #8: features READY AT DECISION TIME. RECORDING ONLY (nothing here is read by ``lean.strategy``), PAPER ONLY, read-only requests on
the shared LOW lane (never the main or the exit lane), one daemon thread, default OFF (``prefetch`` absent / null in lean.json).

The problem. Every candidate sits idle for ``screen.min_age_seconds`` (60 s in production) before it is screened, and every collector
(features L12F, flow L20, wallet signals L13, RugCheck L21) only started AFTER the screen. At the decision only flow 48 %, RugCheck 72 %
and bundle 70 % of the labelled candidates had a usable value (TOOL_AUDIT B03). This module uses the idle minute:

  discovery  the frame is seen as soon as it is written (its own read-only scan, a cursor of its own; the runner's scan is untouched)
  probe      +2 s: a SHADOW SCREEN (``lean.candidates.screen`` with the real screen settings but the market bands opened and the age
             check off: 3 low-lane calls) tells hazard from hazard-free. A hazard (MAYHEM_POOL, HOLDER_CONCENTRATION_ABOVE_MAX,
             HOLDER_REWARD_POOL ...: ~58 % of the candidates) is NOT prefetched (that is the spend saved); an unreadable probe prefetches
             anyway. The probe also gives the dev wallet, the supply and the first holder list the collectors would read later.
  jobs       each collector runs its own code for the candidate ``lead_s`` seconds before the earliest screen (``lead_s`` is how long the
             job needs, so the window it measures is as late as it can be and the value still finishes before the decision):
               features        dev balance + pool history (+ the probe's holder shares for a soft reject)   lean.features.prefetch_enrich
               risk_external   the RugCheck report, creator history                                          lean.risk_external (asof=)
               wallet_signals  early buyers, funders, bundle                                                  lean.wallet_signals (sink=)
               clusters        linked-wallet clusters (the probe's holders + the wallet job's funders)       lean.clusters (sink=)
               flow            the pool's order flow                                                          lean.flow (sink=)
             Nothing is written: the result waits in memory (``Prefetch.entries``), stamped with its REAL times.
  commit     when the screen has decided, the collector's own pass finds the finished job (``Prefetch.take``) and writes its row from
             it with no request: the row carries the REAL decision time (``decision_ts``), the time the last value was OBSERVED
             (``collected_at``: before the decision, so ``late_s`` <= 0) and ``prefetch = {asof, started_at, finished_at, staleness_s}``.

NO LOOKAHEAD. The decision stays where it was: the screen. A prefetch has an AS-OF time ``asof`` = its start less ``settle_s`` (the
confirmation lag); every window it measures ENDS at ``asof`` (flow, wallet signals, the first-10-minute count: the collectors' own cut at
``decision_ts``, fed ``asof``), RugCheck's creator history counts tokens created before ``asof``, and a snapshot (dev balance, holder
list, RugCheck) is read before the job finishes. ``take`` refuses a result that finished after the decision it is asked for, or whose
``asof`` is later than it, or that is older than ``max_stale_s`` (it then falls back to the collector's normal post-screen path, which is
exactly what ran before this module existed). The learner's guards (``lean.learn``) therefore see ``late_s`` <= 0 and a window that ends
before the decision, and keep working unchanged.

BUDGET. Low lane only, never waiting longer than ``patience_s`` per request on this thread, ``reserve_tokens`` left for the other
low-lane users, the collectors' own hourly caps (flow ``max_calls_per_hour``) shared with their normal path. The cost per hazard-free
candidate is the probe (3 calls) plus the calls its collectors would have made after the screen anyway; a hazard costs the probe only;
a mis-gated candidate (the probe said hazard-free, the screen says hazard, or the reverse) wastes or loses its prefetch, never its trade.
The screen, the entry and the exits never wait for, call or read anything here: ``on_decision`` is a dict lookup.

OVERLOAD. One prefetch thread, in arrival order. A job that cannot finish before the earliest screen (it would start later than
``min_age + 3 s - a quarter of its lead``) is not started (``LATE_START``): under a launch burst or a slow provider the prefetch sheds
ITSELF, candidates fall back to the collectors' own post-screen pass, and the low lane is not spent on results that would be refused.
Measured (tests/lean/w2b_measure.py): production-like load (a launch every 30-70 s) 100 % of the hazard-free candidates ready; a stressed
world (every 6-30 s, 1.5x latencies, 5 % failed calls, a 30 s 429 shed window) 92-100 %; far beyond production (every 3-9 s, 3x latencies)
the prefetch cannot keep up (4-17 % ready) and, the low lane being the bottleneck, any extra user of it can cost the collectors' own pass
a few points of learner-usable rows: raise ``reserve_tokens`` to leave more of the lane to them.
"""
import copy
import json
import logging
import threading
import time
from collections import OrderedDict
from decimal import Decimal
from types import SimpleNamespace

from lean import candidates as C
from lean.paths import eligible, is_hazard_reject
from lean.providers import LANE_SHED, ProviderError

log = logging.getLogger('lean.prefetch')

VERSION = 'prefetch_v1'
KINDS = ('features', 'risk_external', 'wallet_signals', 'clusters', 'flow')      # (clusters after wallet_signals: it uses that row's funders)
ORDER = ('probe',) + KINDS
DEFAULTS = {
    'enabled': False,
    'kinds': list(KINDS),               # which collectors are prefetched (a subset of KINDS)
    'probe': True,                      # the shadow screen: hazard gate + dev wallet / supply / holders (3 low-lane calls per candidate)
    'skip_hazards': True,               # do not prefetch a candidate the probe shows to be a hazard
    # seconds BEFORE the earliest screen (discovery + min_age_seconds) at which a job starts: about how long it needs, with slack
    'lead_s': {'risk_external': 25.0, 'features': 16.0, 'wallet_signals': 16.0, 'clusters': 14.0, 'flow': 15.0},       # W2b2 #1: closer to the decision
    'probe_at_s': 2.0,                  # the probe runs this long after discovery
    'min_start_s': 2.0,                 # no job starts earlier than this after discovery
    'settle_s': 2.0,                    # as-of = start - settle_s (a transaction needs a moment to be visible at "confirmed")
    'max_stale_s': 120.0,               # a result whose as-of is older than this at the decision is not used
    'pass_interval_s': 1.0,
    'discover_interval_s': 2.0,
    'max_jobs_per_pass': 3,
    'max_entries': 300,
    'ttl_s': 900.0,                     # an entry is forgotten this long after discovery
    'max_attempts': 3,
    'retry_delay_s': 3.0,
    'patience_s': 3.0,                  # the longest a prefetch waits for a low-lane token per request (own thread)
    'reserve_tokens': 1,                # only call while this many MORE low-lane tokens would remain for the other users
    'scan_limit': 200,
    'flow_calls_per_hour': 450,         # W2b2 #3: the prefetch's OWN hourly flow budget (flow.max_calls_per_hour stays the post-screen pass's)
}
_FLOATS = {'probe_at_s': (0, 600), 'min_start_s': (0, 600), 'settle_s': (0, 60), 'max_stale_s': (1, 3600), 'pass_interval_s': (0.1, 60),
           'discover_interval_s': (0.1, 60), 'ttl_s': (60, 86400), 'retry_delay_s': (0, 600), 'patience_s': (0, 60)}
_INTS = {'max_jobs_per_pass': (1, 50), 'max_entries': (1, 10000), 'max_attempts': (1, 10), 'reserve_tokens': (0, 10), 'scan_limit': (1, 2000),
         'flow_calls_per_hour': (1, 100_000)}
SCREEN_LAG_S = 3.0              # the screen decides about this long after discovery + min_age (p50 2.6 s in production: TOOL_AUDIT B03)
WINDOWED = frozenset({'flow', 'wallet_signals', 'features', 'clusters'})        # their as-of is start - settle_s; RugCheck's is the start itself


class ConfigError(ValueError):
    pass


def config(raw):
    """lean.json ``prefetch`` -> validated settings. Absent / null = disabled. Unknown keys and wrong types are refused."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ConfigError('prefetch must be an object')
    unknown = set(raw) - set(DEFAULTS) - {'_comment'}
    if unknown:
        raise ConfigError('prefetch: unknown keys %s' % sorted(unknown))
    out = {k: copy.deepcopy(raw.get(k, v)) for k, v in DEFAULTS.items()}
    for key in ('enabled', 'probe', 'skip_hazards'):
        if type(out[key]) is not bool:
            raise ConfigError('prefetch.%s must be true or false' % key)
    kinds = out['kinds']
    if not isinstance(kinds, list) or not kinds or any(k not in KINDS for k in kinds) or len(set(kinds)) != len(kinds):
        raise ConfigError('prefetch.kinds must be a non-empty list of distinct names from %s' % (KINDS,))
    lead = out['lead_s']
    if not isinstance(lead, dict) or set(lead) - set(KINDS):
        raise ConfigError('prefetch.lead_s must be an object keyed by %s' % (KINDS,))
    merged = dict(DEFAULTS['lead_s'])
    merged.update(lead)
    for name, value in merged.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 3600 or value != value:
            raise ConfigError('prefetch.lead_s.%s out of range' % name)
        merged[name] = float(value)
    out['lead_s'] = merged
    for key, (low, high) in _FLOATS.items():
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or not low <= value <= high:
            raise ConfigError('prefetch.%s out of range' % key)
        out[key] = float(value)
    for key, (low, high) in _INTS.items():
        if type(out[key]) is not int or not low <= out[key] <= high:
            raise ConfigError('prefetch.%s out of range' % key)
    if out['skip_hazards'] and not out['probe']:
        raise ConfigError('prefetch.skip_hazards needs prefetch.probe')
    if 'features' in out['kinds'] and not out['probe']:
        raise ConfigError('prefetch.kinds features needs prefetch.probe (the dev wallet comes from the probe)')
    return out


class Job:
    __slots__ = ('kind', 'state', 'attempts', 'next_at', 'asof', 'started_at', 'finished_at', 'payload', 'why', 'used', 'cancelled')

    def __init__(self, kind):
        self.kind, self.state, self.attempts, self.next_at = kind, 'pending', 0, 0.0
        self.asof = self.started_at = self.finished_at = self.payload = self.why = None
        self.used = False
        self.cancelled = False            # W2b2 #6: the decision came while the job was running: it ends 'cancelled', never 'done'


class Entry:
    __slots__ = ('candidate', 'received', 'seen_at', 'probe', 'probe_at', 'gate', 'soft', 'jobs', 'decided_at', 'real')

    def __init__(self, candidate, seen_at):
        self.candidate, self.received, self.seen_at = candidate, float(candidate.migrated_at), seen_at
        self.probe = self.probe_at = None
        self.gate = None                  # None (unknown) | 'FREE' | 'HAZARD' (the probe's verdict)
        self.soft = None                  # W2b2 #3: None (unknown) | True / False: the probe's market figures are outside / inside the screen's bands
        self.jobs = OrderedDict((kind, Job(kind)) for kind in ORDER)
        self.decided_at = None
        self.real = None                  # 'FREE' | 'HAZARD' | 'FAILED' by the real screen


def _fresh_counts():
    return {kind: 0 for kind in KINDS}


class Prefetch:
    """The prefetcher. ``prefetch_pass()`` does a bounded amount of work (tests drive it); ``run_loop()`` is its thread."""

    def __init__(self, *, cfg, store, helius, discovery_db, screen_cfg, min_age_s, clock=time.time, sleep=time.sleep, stop=None,
                 code_version='', strategy_version=''):
        self.cfg = config(cfg)
        self.store, self.helius, self.discovery_db = store, helius, discovery_db
        self.clock, self.sleep = clock, sleep
        self.stop = stop or threading.Event()
        self.min_age_s = float(min_age_s)
        self.screen_cfg = dict(screen_cfg)
        # the shadow screen: the real settings, but the age check off and the market bands opened, so it always reaches the holder stage
        self.probe_cfg = {**self.screen_cfg, 'min_age_seconds': 0, 'max_age_seconds': 10 ** 9, 'min_market_cap_usd': 0,
                          'max_market_cap_usd': 10 ** 18, 'min_liquidity_usd': 0}
        self.code_version, self.strategy_version = code_version, strategy_version
        self.sol_usd = None                                # W2b2 #3: callable -> Decimal | None, the runner's cached SOL/USD (None = bands cannot be judged)
        self.gate = None                                   # callable -> bool: False pauses the prefetch (the runner takes no new entries: halted / kill switch)
        self.flow = self.wallet_signals = self.features = self.risk_external = self.clusters = None
        self.entries = OrderedDict()                       # mint -> Entry
        self._lock = threading.RLock()
        self._cursor = None
        self._last_discover = -1e18
        self.counts = {'discovered': 0, 'already_handled': 0, 'probes': 0, 'probe_calls': 0, 'probe_failed': 0, 'hazard_skipped': 0,
                       'jobs_run': 0, 'jobs_done': 0, 'jobs_failed': 0, 'retries': 0, 'lane_waits': 0, 'cancelled': 0, 'late_start': 0, 'errors': 0,
                       'expired': 0, 'dropped': 0, 'scan_errors': 0}
        self.used = _fresh_counts()
        self.misses = {kind: {} for kind in KINDS}
        # readiness AT THE DECISION (on_decision): over hazard-free decisions (PASS or soft reject) and, for flow, PASS decisions
        self.flow_gate = {'skipped_soft': 0, 'skipped_soft_but_passed': 0, 'ran_but_not_passed': 0}      # W2b2 #3: the soft-reject gate vs the screen
        self.ready = {'decisions': 0, 'hazard_free': 0, 'passed': 0, 'kinds': _fresh_counts(), 'flow_passed': 0,
                      'probe': {'tp': 0, 'fp': 0, 'fn': 0, 'tn': 0, 'unknown': 0}, 'staleness_s': {'n': 0, 'sum': 0.0, 'max': 0.0}}
        self.last_error = None

    # ------------------------------------------------------------------------------------------------- wiring
    def attach(self, **collectors):
        """Wire the collectors this prefetcher drives (``features``, ``risk_external``, ``wallet_signals``, ``clusters``, ``flow``)."""
        for name, worker in collectors.items():
            if name not in KINDS:
                raise ValueError('unknown collector ' + name)
            setattr(self, name, worker)
            if worker is not None:
                worker.prefetch = self
                if name == 'flow':                       # W2b2 #3: the prefetch spends its own hourly budget, the post-screen fallback keeps its
                    from lean import flow as _flow
                    worker.prefetch_budget = _flow.HourBudget(self.cfg['flow_calls_per_hour'], self.clock)
        return self

    def _worker(self, kind):
        return getattr(self, kind) if kind in self.cfg['kinds'] else None

    # ------------------------------------------------------------------------------------------------- the pass
    def prefetch_pass(self):
        """Discover new frames, run the due jobs (at most ``max_jobs_per_pass``), forget old entries. Never raises."""
        done = 0
        try:
            if self.gate is not None and not self.gate():
                return 0                                         # the runner screens nothing now: spend nothing on candidates it will not screen
            now = self.clock()
            if now - self._last_discover >= self.cfg['discover_interval_s']:
                self._last_discover = now
                self._discover(now)
            self._prune(now)
            for entry in list(self.entries.values()):
                if self.stop.is_set() or done >= self.cfg['max_jobs_per_pass']:
                    break
                if entry.decided_at is not None:
                    continue
                for kind in ORDER:
                    if self.stop.is_set() or done >= self.cfg['max_jobs_per_pass']:
                        break
                    job = entry.jobs[kind]
                    if job.state != 'pending':
                        continue
                    state = self._ready(entry, job, self.clock())
                    if state == 'run':
                        self._run(entry, job)
                        done += 1
                    elif state == 'block':
                        break
        except Exception as error:                               # noqa: BLE001 - a prefetch bug never reaches the trader
            self.counts['errors'] += 1
            self.last_error = type(error).__name__
            log.debug('prefetch pass failed', exc_info=True)
        return done

    def run_loop(self):
        while not self.stop.is_set():
            self.prefetch_pass()
            self.stop.wait(self.cfg['pass_interval_s'])

    # ------------------------------------------------------------------------------------------------- discovery
    def _discover(self, now):
        """New migration frames (their own cursor, the runner's is untouched), a cheap read-only scan with no age gate."""
        window = self.min_age_s + self.cfg['ttl_s']
        try:
            if self._cursor is None:
                self._cursor = C.initial_cursor(self.discovery_db, now=now, max_age_seconds=self.min_age_s + 30)
            scan = C.scan_new(self.discovery_db, self._cursor, now=now, limit=self.cfg['scan_limit'], min_age_seconds=0,
                              max_age_seconds=window, cfg=self.screen_cfg)
        except (C.DiscoveryUnavailable, OSError, ValueError):
            self.counts['scan_errors'] += 1
            return
        self._cursor = max(self._cursor, scan.next_cursor)
        with self._lock:
            fresh = [c for c in scan.candidates if c.mint not in self.entries]
        # W2b2 #7: the store read happens OUTSIDE the lock, so the candidate thread's ``on_decision`` never waits behind it
        handled = {c.mint for c in fresh if self._handled(c.mint)}
        with self._lock:
            for candidate in fresh:
                if candidate.mint in self.entries:
                    continue
                if candidate.mint in handled:
                    self.counts['already_handled'] += 1
                    continue
                while len(self.entries) >= self.cfg['max_entries']:
                    self.entries.popitem(last=False)
                    self.counts['dropped'] += 1
                self.entries[candidate.mint] = Entry(candidate, now)
                self.counts['discovered'] += 1

    def _handled(self, mint):
        """Already screened by the runner (e.g. before a restart): nothing left to prefetch for."""
        try:
            return self.store.candidate_id(mint) is not None
        except Exception:                                        # noqa: BLE001
            return False

    def _prune(self, now):
        with self._lock:
            for mint in [m for m, e in self.entries.items() if now - e.received > self.cfg['ttl_s']]:
                del self.entries[mint]
                self.counts['expired'] += 1

    # ------------------------------------------------------------------------------------------------- scheduling
    def due(self, entry, kind):
        """The time a job may start: the probe a fixed time after discovery, a collector ``lead_s`` before the earliest screen."""
        if kind == 'probe':
            return entry.received + self.cfg['probe_at_s']
        return entry.received + max(self.cfg['min_start_s'], self.min_age_s - self.cfg['lead_s'][kind])

    def needs(self, kind):
        """About how long a job takes: a quarter of its ``lead_s`` (the lead is the duration plus slack); the probe's three calls ~3 s."""
        return 3.0 if kind == 'probe' else 0.25 * self.cfg['lead_s'][kind]

    def _lane_tokens(self):
        limiter = getattr(getattr(self.helius, 'transport', None), 'limiter', None)
        available = getattr(limiter, 'available', None)
        return None if available is None else available()

    def _ready(self, entry, job, now):
        """'run' | 'wait' (not yet; the pass goes on to the next kind) | 'block' (the rest of this entry waits for this job)
        | 'skip' (the job was finished off without running)."""
        kind = job.kind
        if kind == 'probe':
            if not self.cfg['probe'] or job.attempts >= self.cfg['max_attempts']:
                if job.state == 'pending':
                    job.state, job.why = 'skipped', 'PROBE_OFF' if not self.cfg['probe'] else 'PROBE_ATTEMPTS'
                    entry.gate = entry.gate or None
                return 'skip'
        else:
            worker = self._worker(kind)
            if worker is None or (kind == 'features' and (not getattr(worker, 'enrich_enabled', True) or getattr(worker, 'helius', None) is None)):
                job.state, job.why = 'skipped', 'NOT_ENABLED'
                return 'skip'
            if self.cfg['probe']:
                probe = entry.jobs['probe']
                if probe.state == 'pending':
                    return 'wait'                                  # the verdict first (it is retried a few times, then given up)
                if entry.gate == 'HAZARD' and self.cfg['skip_hazards']:
                    job.state, job.why = 'skipped', 'HAZARD'
                    self.counts['hazard_skipped'] += 1
                    return 'skip'
                if kind == 'features' and probe.state != 'done':
                    job.state, job.why = 'skipped', 'NO_PROBE'      # the dev wallet comes from the probe
                    return 'skip'
                if kind == 'flow' and entry.soft is True:           # W2b2 #3: flow is collected for PASS decisions only: a soft reject would waste it
                    job.state, job.why = 'skipped', 'SOFT_REJECT'
                    self.flow_gate['skipped_soft'] += 1
                    return 'skip'
        if kind == 'clusters' and self.wallet_signals is not None and 'wallet_signals' in self.cfg['kinds'] \
                and entry.jobs['wallet_signals'].state == 'pending':
            return 'wait'                                          # its funders / early buyers come from the wallet job
        if now < self.due(entry, kind) or now < job.next_at:
            return 'wait'
        # too late to finish before the earliest screen (+ the screen's usual lag): a result would be refused at the decision, and the calls
        # would only crowd the low lane the collectors' own post-screen pass needs. Under overload the prefetch sheds ITSELF.
        if now > entry.received + self.min_age_s + SCREEN_LAG_S - self.needs(kind):
            job.state, job.why = 'skipped', 'LATE_START'
            self.counts['late_start'] += 1
            return 'skip'
        need = {'probe': 3, 'features': 2, 'wallet_signals': 2, 'clusters': 2, 'flow': 2, 'risk_external': 0}[kind]
        tokens = self._lane_tokens() if need else None
        if tokens is not None and tokens < need + self.cfg['reserve_tokens']:
            self.counts['lane_waits'] += 1                        # the lane is busy with the others: this pass skips it, nothing was sent
            return 'wait'
        return 'run'

    # ------------------------------------------------------------------------------------------------- one job
    def _run(self, entry, job):
        kind, candidate = job.kind, entry.candidate
        job.attempts += 1
        job.state = 'running'                                # W2b2 #6: not 'pending': the decision must be able to tell a job in flight
        job.started_at = self.clock()
        job.asof = job.started_at - (self.cfg['settle_s'] if kind in WINDOWED else 0.0)
        self.counts['jobs_run'] += 1
        sink = {'patience_s': self.cfg['patience_s']}
        try:
            if kind == 'probe':
                done = self._probe(entry, job)
            elif kind == 'features':
                payload = self.features.prefetch_run(candidate, entry.probe, job.asof, patience_s=self.cfg['patience_s'], stop=self.stop,
                                                     need_holders=entry.soft is True)
                shed = [why for why in payload['missing'].values() if why in (LANE_SHED, 'STOPPED')]
                done = self._retry(job, shed[0]) if shed and payload['calls'] == 0 else self._finish(job, payload)       # a shed lane: ask again
            elif kind == 'risk_external':
                payload = self.risk_external.collect(candidate.mint, None, True, job.started_at, asof=job.asof)
                why = payload['missing'].get('rc_score')
                # a report that could not be read for a transient reason (429 / back-off / timeout) is asked for again, not committed as a gap;
                # "RugCheck does not know this mint" (400) is an answer
                done = (self._retry(job, why) if payload['fields'].get('rc_score') is None and why not in (None, 'RC_UNKNOWN_MINT')
                        else self._finish(job, payload))
            elif kind == 'wallet_signals':
                supply = (entry.probe.features or {}).get('supply_raw') if entry.probe is not None else None
                self.wallet_signals.collect(None, candidate.mint, candidate.pool, candidate.slot, decision_ts=job.asof, sink=sink,
                                            prefetched_supply=int(supply) if supply is not None and str(supply).isdecimal() and int(supply) > 0 else None)
                done = self._finish(job, sink) if 'features' in sink else self._retry(job, 'NO_RESULT')
            elif kind == 'clusters':
                probe = entry.probe
                bodies = dict(probe.raw) if probe is not None else {}
                wallet = entry.jobs['wallet_signals']
                # W2b2 #2: no holder body: the job READS the holder list itself, now (the probe's is ~60 s old at the decision)
                self.clusters.collect(None, candidate.mint, candidate.pool, candidate.slot, decision_ts=job.asof, sink=sink,
                                      screen_data=(dict(probe.features or {}), None, bodies.get('accounts_mint_pool')),
                                      signals=wallet.payload['features'] if wallet.state == 'done' else None)
                done = self._finish(job, sink) if 'row' in sink else self._retry(job, 'NO_RESULT')
            else:                                                  # flow
                from lean import flow as _flow
                try:
                    self.flow.collect(None, job.asof, candidate.mint, None, candidate.pool, candidate.migrated_at, sink=sink)
                except _flow._Retry as retry:
                    done = self._retry(job, retry.code)
                else:
                    done = self._finish(job, sink)
        except ProviderError as error:
            done = self._retry(job, error.code)
        except Exception as error:                                 # noqa: BLE001 - a collector bug is a missing prefetch, never an error of the trader
            self.counts['errors'] += 1
            self.last_error = type(error).__name__
            log.debug('prefetch job %s failed', kind, exc_info=True)
            job.state, job.why = 'failed', 'ERROR:' + type(error).__name__
            self.counts['jobs_failed'] += 1
            done = False
        return done

    def _finish(self, job, payload):
        job.finished_at = self.clock()
        if job.cancelled:                                    # W2b2 #6: decided while running: its result is dropped, it is not counted as done
            job.state, job.why = 'cancelled', 'DECIDED_WHILE_RUNNING'
            return False
        job.payload, job.state = payload, 'done'
        self.counts['jobs_done'] += 1
        return True

    def _retry(self, job, why):
        """Nothing usable came of this try (a stop, a shed lane, a transient failure before anything was learned): try again soon."""
        job.why = str(why)
        if job.cancelled:
            job.state, job.why = 'cancelled', 'DECIDED_WHILE_RUNNING'
        elif self.stop.is_set() or job.attempts >= self.cfg['max_attempts']:
            job.state = 'failed'
            self.counts['jobs_failed'] += 1
        else:
            job.state, job.next_at = 'pending', self.clock() + self.cfg['retry_delay_s']
            self.counts['retries'] += 1
        return False

    def _probe(self, entry, job):
        """The shadow screen. A transient failure retries; the verdict is ``FREE`` (passed or soft-rejected only) or ``HAZARD``."""
        candidate = entry.candidate
        self.counts['probes'] += 1
        providers = SimpleNamespace(helius=_Patient(self.helius, self), kraken=None)
        result = C.screen(candidate, providers, self.probe_cfg, now=self.clock(), sol_usd=Decimal(1))
        self.counts['probe_calls'] += len(result.raw)
        if result.error:
            self.counts['probe_failed'] += 1
            return self._retry(job, result.error.get('code'))
        if any(str(r).startswith('SCREEN_INTERNAL_ERROR') for r in result.reasons):
            job.state, job.why = 'failed', 'SCREEN_INTERNAL_ERROR'
            self.counts['jobs_failed'] += 1
            return False
        entry.probe, entry.probe_at = result, self.clock()
        entry.gate = 'HAZARD' if is_hazard_reject(result) else 'FREE'
        entry.soft = self._soft(result)
        return self._finish(job, {'gate': entry.gate, 'soft': entry.soft, 'reasons': list(result.reasons)})

    def _soft(self, result):
        """W2b2 #3: would the screen reject this pool for its MARKET figures (market cap / liquidity outside the strategy's bands)? The probe
        ran with SOL/USD = 1, so its USD figures are the SOL figures: x the runner's cached SOL/USD gives the screen's. None when that rate is
        unknown (then nothing is skipped on this ground)."""
        try:
            usd = self.sol_usd() if self.sol_usd is not None else None
            f = result.features or {}
            if usd is None or Decimal(str(usd)) <= 0 or f.get('market_cap_usd') is None or f.get('liquidity_usd') is None:
                return None
            usd = Decimal(str(usd))
            cap, liquidity = Decimal(str(f['market_cap_usd'])) * usd, Decimal(str(f['liquidity_usd'])) * usd
            cfg = self.screen_cfg
            return bool(cap < Decimal(str(cfg['min_market_cap_usd'])) or cap > Decimal(str(cfg['max_market_cap_usd']))
                        or liquidity < Decimal(str(cfg['min_liquidity_usd'])))
        except (ArithmeticError, ValueError, TypeError, KeyError):
            return None

    # ------------------------------------------------------------------------------------------------- results
    def take(self, kind, mint, decision_ts):
        """The finished prefetch of ``kind`` for ``mint`` usable at ``decision_ts``, else None (the collector then runs as it always did).
        Usable = done, finished and as-of at or before the decision, and not older than ``max_stale_s`` at it. Returns
        ``{'payload', 'asof', 'started_at', 'finished_at'}``."""
        with self._lock:
            entry = self.entries.get(mint)
            job = None if entry is None else entry.jobs.get(kind)
            why = None
            if job is None:
                why = 'NO_ENTRY'
            elif job.state != 'done':
                why = 'NOT_DONE:' + (job.why or job.state)
            elif job.used:                                   # W2b2 #6: ONE row per job: a second decision of the mint falls back to the collector
                why = 'ALREADY_USED'
            elif job.finished_at > decision_ts:
                why = 'FINISHED_AFTER_DECISION'
            elif job.asof > decision_ts:
                why = 'ASOF_AFTER_DECISION'
            elif decision_ts - job.asof > self.cfg['max_stale_s']:
                why = 'STALE'
            if why is not None:
                self.misses[kind][why] = self.misses[kind].get(why, 0) + 1
                return None
            job.used = True
            self.used[kind] += 1
            return {'payload': job.payload, 'asof': job.asof, 'started_at': job.started_at, 'finished_at': job.finished_at}

    def take_features(self, mint, decision_ts, need_holders):
        """``take('features')`` unless a soft reject needs holder shares the probe did not give (then the collector reads them itself)."""
        if need_holders:
            with self._lock:
                entry = self.entries.get(mint)
                job = None if entry is None else entry.jobs.get('features')
                if job is None or job.state != 'done' or not (job.payload or {}).get('holders'):
                    self.misses['features']['NO_HOLDERS'] = self.misses['features'].get('NO_HOLDERS', 0) + 1
                    return None
        return self.take('features', mint, decision_ts)

    @staticmethod
    def _stamp(pre, decision_ts):
        return {'asof': pre['asof'], 'started_at': pre['started_at'], 'finished_at': pre['finished_at'],
                'staleness_s': round(decision_ts - pre['asof'], 3), 'version': VERSION}

    def _note_staleness(self, decision_ts, pre):
        stale = decision_ts - pre['asof']
        with self._lock:
            s = self.ready['staleness_s']
            s['n'] += 1
            s['sum'] += stale
            s['max'] = max(s['max'], stale)

    def commit_flow(self, collector, decision_id, decision_ts, mint, cid, pool):
        """The flow row of a finished prefetch, written with the real decision (no request). None: collect as usual."""
        pre = self.take('flow', mint, decision_ts)
        if pre is None:
            return None
        sink = pre['payload']
        body = copy.deepcopy(sink['body'])
        body['started_at'] = pre['finished_at']                  # collected_at = when the last value was observed
        body['prefetch'] = self._stamp(pre, decision_ts)
        body['window_mode'] = 'prefetch'                         # W2b2 #1: version 'flow_v1p': the learner keeps it in its own columns
        body['window_end_ts'] = sink.get('window_end_ts')
        self._note_staleness(decision_ts, pre)
        return collector._write(decision_id, decision_ts, mint, cid, pool, body)

    def commit_wallet(self, collector, cid, mint, pool, grad_slot, decision_ts):
        """The wallet-signals row of a finished prefetch, written with the real decision (no request). None: collect as usual."""
        pre = self.take('wallet_signals', mint, decision_ts)
        if pre is None:
            return None
        sink = pre['payload']
        features = copy.deepcopy(sink['features'])
        events = copy.deepcopy(sink['events'])
        features.update(decision_ts=decision_ts, collected_at=pre['finished_at'], late_s=round(pre['finished_at'] - decision_ts, 3),
                        prefetch=self._stamp(pre, decision_ts), window_end_ts=sink.get('window_end_ts'),
                        version=features['version'] + 'p', window_mode='prefetch')      # W2b2 #1
        features.get('unavailable', {}).pop('decision_ts', None)
        features['partial'] = bool(features.get('unavailable'))
        collector._attempts.pop(mint, None)
        self._note_staleness(decision_ts, pre)
        collector._persist(features, events, cid, mint)
        collector._persist_raws(sink.get('raws'), cid, mint)                 # W2b2 #8: with the candidate id
        return features

    def commit_clusters(self, collector, cid, mint, pool, grad_slot, decision_ts):
        """The clusters row of a finished prefetch, written with the real decision (no request). None: collect as usual."""
        pre = self.take('clusters', mint, decision_ts)
        if pre is None:
            return None
        sink = pre['payload']
        row = copy.deepcopy(sink['row'])
        lookups = copy.deepcopy(sink['lookups'])
        row.update(decision_ts=decision_ts, collected_at=pre['finished_at'], late_s=round(pre['finished_at'] - decision_ts, 3),
                   prefetch=self._stamp(pre, decision_ts), window_end_ts=sink.get('window_end_ts'),
                   version=row['version'] + 'p', window_mode='prefetch')                # W2b2 #1
        self._note_staleness(decision_ts, pre)
        collector._persist(row, lookups, cid, mint)
        return row

    # ------------------------------------------------------------------------------------------------- the decision
    def on_decision(self, mint, decision_ts, screen):
        """The runner's hook right after a screen decision (``screen`` = the real ``Screen``): count what was READY, and compare the
        probe's verdict with the screen's. A dictionary lookup under a lock: no I/O, never raises, never blocks the screen."""
        try:
            if getattr(screen, 'error', None):
                return                                           # a failed screen is retried: its final attempt is the decision
            with self._lock:
                entry = self.entries.get(mint)
                if entry is None or entry.decided_at is not None:
                    return
                entry.decided_at = decision_ts
                for job in entry.jobs.values():
                    if job.state == 'pending':
                        job.state, job.why = 'cancelled', 'DECIDED'
                        self.counts['cancelled'] += 1
                    elif job.state == 'running':
                        job.cancelled = True                  # W2b2 #6: ends 'cancelled' when it returns (see ``_finish``), is not counted done
                        self.counts['cancelled'] += 1
                entry.real = 'FREE' if eligible(screen) else 'HAZARD'
                r = self.ready
                r['decisions'] += 1
                probe = r['probe']
                if entry.gate is None:
                    probe['unknown'] += 1
                elif entry.gate == 'HAZARD':
                    probe['tp' if entry.real == 'HAZARD' else 'fp'] += 1
                else:
                    probe['tn' if entry.real == 'FREE' else 'fn'] += 1
                if entry.real != 'FREE':
                    return
                r['hazard_free'] += 1
                passed = bool(getattr(screen, 'passed', False))
                r['passed'] += 1 if passed else 0
                flow_job = entry.jobs['flow']                  # W2b2 #3: was the soft-reject gate right?
                if passed and flow_job.why == 'SOFT_REJECT':
                    self.flow_gate['skipped_soft_but_passed'] += 1
                elif not passed and flow_job.state in ('done', 'running', 'cancelled') and flow_job.why != 'DECIDED':
                    self.flow_gate['ran_but_not_passed'] += 1
                for kind in KINDS:
                    if kind == 'flow' and not passed:
                        continue
                    job = entry.jobs[kind]
                    if job.state == 'done' and job.finished_at is not None and job.finished_at <= decision_ts and job.asof <= decision_ts:
                        r['kinds'][kind] += 1
                        if kind == 'flow':
                            r['flow_passed'] += 1
        except Exception:                                        # noqa: BLE001
            self.counts['errors'] += 1

    # ------------------------------------------------------------------------------------------------- health
    def health(self):
        with self._lock:
            r = self.ready
            n_free, n_pass = r['hazard_free'], r['passed']
            rate = {kind: (None if not (n_pass if kind == 'flow' else n_free) else
                           round(r['kinds'][kind] / (n_pass if kind == 'flow' else n_free), 4)) for kind in KINDS}
            s = r['staleness_s']
            states = {}
            for entry in self.entries.values():
                for job in entry.jobs.values():
                    states[job.kind + ':' + job.state] = states.get(job.kind + ':' + job.state, 0) + 1
            return {'enabled': True, 'version': VERSION, 'entries': len(self.entries), **self.counts, 'used': dict(self.used),
                    'misses': {k: dict(v) for k, v in self.misses.items() if v}, 'job_states': dict(sorted(states.items())),
                    'ready_at_decision': {'decisions': r['decisions'], 'hazard_free': n_free, 'passed': n_pass,
                                          'ready': dict(r['kinds']), 'rate': rate,
                                          'staleness_s_mean': None if not s['n'] else round(s['sum'] / s['n'], 3),
                                          'staleness_s_max': round(s['max'], 3) if s['n'] else None},
                    'probe_vs_screen': dict(r['probe']), 'probe_fp_rate': self._fp_rate(r['probe']), 'flow_gate': dict(self.flow_gate),
                    'last_error': self.last_error, 'kinds': list(self.cfg['kinds'])}

    @staticmethod
    def _fp_rate(probe):
        """W2b2 #5: fp / (fp + tn): of the candidates the screen found hazard-FREE, the share the probe wrongly called hazard (their prefetch was
        skipped). The rollback criterion: above 10 % the gate costs more than it saves. None before any such decision."""
        n = probe['fp'] + probe['tn']
        return None if not n else round(probe['fp'] / n, 4)


class _Patient:
    """The low-lane Helius client as the SHADOW SCREEN and the collectors' prefetch jobs use it: a ``no_token`` shed is waited out for at
    most ``patience_s`` (this thread's own time, the lane never waits) before the typed ``LANE_SHED`` goes on to the caller."""

    def __init__(self, helius, owner):
        self._helius, self._owner = helius, owner

    def _call(self, name, *args):
        waited, step = 0.0, 0.5
        while True:
            try:
                return getattr(self._helius, name)(*args)
            except ProviderError as error:
                if error.code != LANE_SHED or error.meta.get('why') != 'no_token' or waited >= self._owner.cfg['patience_s'] \
                        or self._owner.stop.is_set():
                    raise
                self._owner.sleep(step)
                waited += step

    def get_multiple_accounts(self, pubkeys):
        return self._call('get_multiple_accounts', pubkeys)

    def rpc(self, method, params):
        return self._call('rpc', method, params)


def build(cfg, *, store, keys, discovery_db, screen_cfg, min_age_s, code_version, strategy_version, clock=time.time, transport_kwargs=None):
    """The prefetcher of a validated ``prefetch`` config on the shared LOW lane (not yet attached to its collectors); None when disabled."""
    if not cfg['enabled']:
        return None
    from lean import providers
    transport_kwargs = dict(transport_kwargs or {})
    helius = providers.low(keys, **transport_kwargs).helius
    return Prefetch(cfg=cfg, store=store, helius=helius, discovery_db=discovery_db, screen_cfg=screen_cfg, min_age_s=min_age_s, clock=clock,
                    sleep=transport_kwargs.get('sleep', time.sleep), code_version=code_version, strategy_version=strategy_version)


# ======================================================================================================== readiness (read-only)
READINESS_FIELDS = (            # (name, observation kind, field, population)
    ('flow.buy_sell_ratio', 'flow', 'buy_sell_ratio', 'passed'),
    ('flow.tx_rate_per_s', 'flow', 'tx_rate_per_s', 'passed'),
    ('features.dev_holding_pct', 'features', 'dev_holding_pct', 'free'),
    ('features.tx_count_first_10m', 'features', 'tx_count_first_10m', 'free'),
    ('features.seconds_graduation_to_screen', 'features', 'seconds_graduation_to_screen', 'free'),
    ('risk.rc_score', 'risk_external', 'rc_score', 'free'),
    ('risk.same_image_count', 'risk_external', 'same_image_count', 'free'),
    ('wallet.bundle_score', 'wallet_signals', 'bundle_score', 'free'),
    ('clusters.cluster_count', 'clusters', 'cluster_count', 'free'),
)


def _meta_rows(connection, kind):
    """{mint: [row dicts]} of one observation kind from the small ``meta`` column (never the ``raw`` blob), each with ``_candidate_id``."""
    out = {}
    for mint, cid, meta in connection.execute('SELECT mint, candidate_id, meta FROM observations WHERE kind=? ORDER BY id', (kind,)):
        try:
            row = json.loads(meta)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict) and isinstance(mint, str):
            row['_candidate_id'] = cid
            out.setdefault(mint, []).append(row)
    return out


def readiness(db, *, max_late_s=15.0, use_raw=True):
    """How many of the hazard-free (PASS or soft-rejected) candidates had each feature AT THE DECISION. Read-only, from a lean.sqlite.

    For every candidate with a screen decision: ``decision`` = its first PASS / REJECT screen decision. Per feature:
      ``row``      a collector row of the candidate exists with a value;
      ``ready``    ... and the value was OBSERVED strictly before the screen decision (``collected_at`` < decision, ``late_s`` < 0): the
                   strategy could have used it at the decision;
      ``usable``   ... and the selection learner's lookahead guards accept it (``lean.learn``, ``max_late_s``): a value collected after the
                   decision can still be usable (a window cut at the decision), but only a READY one was available to decide with.
    Returns ``{'population': {...}, 'features': {name: {'n', 'row', 'ready', 'usable', 'ready_rate', 'usable_rate'}}}``. ``use_raw=False``
    reads the small ``meta`` column only (the lean report never selects an observation blob) and then ``usable`` is None."""
    from lean import learn as LN
    from lean.report import open_ro
    policy = LN.LookaheadPolicy(max_late_s=max_late_s)
    connection = open_ro(db)
    try:
        counts = LN._Counts()
        decisions = connection.execute("SELECT id, ts, mint, candidate_id, action, reasons FROM decisions WHERE kind='screen' "
                                       "AND action IN ('PASS','REJECT') ORDER BY id").fetchall()
        first = {}
        for _id, ts, mint, cid, action, reasons in decisions:
            first.setdefault((mint, cid), (ts, action, reasons))
        learner_decisions = LN.read_decisions(connection)
        kinds = ('features', 'flow', 'wallet_signals', 'risk_external', 'clusters')
        by_kind = ({k: LN._observations(connection, k, counts, k.upper()) for k in kinds} if use_raw
                   else {k: _meta_rows(connection, k) for k in kinds})
    finally:
        connection.close()
    soft = {'MARKET_CAP_BELOW_MIN', 'MARKET_CAP_ABOVE_MAX', 'LIQUIDITY_BELOW_MIN'}
    population = {'screened': 0, 'free': 0, 'passed': 0}
    members = {'free': [], 'passed': []}
    for (mint, cid), (ts, action, reasons) in first.items():
        population['screened'] += 1
        try:
            names = set(json.loads(reasons))
        except (ValueError, TypeError):
            names = set()
        free = action == 'PASS' or bool(names) and names <= soft
        if free:
            population['free'] += 1
            members['free'].append((mint, cid, ts))
        if action == 'PASS':
            population['passed'] += 1
            members['passed'].append((mint, cid, ts))
    guards = {'features': LN.feature_values, 'flow': LN.flow_values, 'wallet_signals': LN.wallet_values, 'risk_external': LN.risk_values,
              'clusters': LN.cluster_values}
    out = {}
    for name, kind, field, pop in READINESS_FIELDS:
        n = row = ready = usable = 0
        for mint, cid, screen_ts in members[pop]:
            n += 1
            rows = [r for r in by_kind[kind].get(mint, []) if r.get('_candidate_id') in (None, cid)]
            if not rows:
                continue
            # the learner's decision of this candidate (the entry decision that followed its screen, else the screen)
            decision = LN.decision_time(learner_decisions.get(mint, []), cid, float('inf')) or screen_ts
            picked = LN._pick(rows, decision, cid, policy, LN._Counts(), kind) if use_raw else None
            observed = [r for r in rows if _value(kind, r, field) is not None]
            if observed:
                row += 1
                if any(_collected(r) is not None and _collected(r) < screen_ts - 1e-6 for r in observed):         # strictly BEFORE the decision
                    ready += 1
            if picked is not None and guards[kind](picked, decision, policy, LN._Counts(), (field,)).get(field) is not None:
                usable += 1
        out[name] = {'n': n, 'row': row, 'ready': ready, 'usable': usable if use_raw else None,
                     'ready_rate': None if not n else round(ready / n, 4), 'usable_rate': None if not n or not use_raw else round(usable / n, 4)}
    return {'population': population, 'features': out}


def _collected(row):
    value = row.get('collected_at')
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _value(kind, row, field):
    holder = row.get('fields') if kind in ('features', 'risk_external') else row
    return holder.get(field) if isinstance(holder, dict) else None
