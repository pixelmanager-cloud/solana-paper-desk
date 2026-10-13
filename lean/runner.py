"""Lean paper trader runner: one process, two concurrent loops, per-candidate failure isolation.

PAPER ONLY: no signing, no broadcasting, no real funds. Fills are quote-based and EXECUTION_UNVERIFIED.

candidate loop   discovery (lean.candidates.scan_new, cursor persisted; a first start begins at the age window)
                 -> screen -> entry decision (pre-quote gate) -> buy quote AT THE DECIDED SIZE + sell-leg quote for
                 exactly the tokens the fill will hold -> entry decision on the round trip -> paper BUY (fill + position
                 state in one transaction).
position loop    every <=10s: ONE batched getMultipleAccounts of all held vaults (exit lane) -> net marks. A position
                 whose mark is older than ``stale_mark_s`` (e.g. Helius down) is marked by a Jupiter sell quote for its
                 whole quantity on the exit lane instead (a "quote-mark"), so stop, time-stop and max-hold still fire.
                 -> exit decision -> (only when an exit triggers) sell quote for the exact raw qty -> exit decision on
                 the quote -> paper SELL (fill + rung/closed state in one transaction). Peak/touched are persisted.
                 An exit that stays wanted but un-quotable (no route / 4xx) for ``unexitable_after_s`` is written off
                 at zero proceeds (reason UNEXITABLE): the slot is freed and the loss is booked.

Failure policy (kept simple, no latches): a provider error, malformed response or bad evidence fails THAT candidate or
position check (an ``errors`` row, raw bytes kept as an observation when there are any) and the loop moves on; a
transient screening failure is retried on the next passes (in memory, up to ``max_retries``). An accounting invariant
violation (AccountingHalt) or a store failure (StoreError / sqlite3.Error, after the store's own busy retries) HALTS
NEW ENTRIES: the halt is logged at ERROR, written to the store and survives restarts; exits keep running. The runner
re-reads the store's halt state every pass, so ``python -m lean --clear-halt`` also resumes a RUNNING process. The
kill-switch file stops new entries only. If a loop thread dies, ``run()`` stops the other one and returns False
(the entry point exits non-zero; systemd restarts it).

All unit/type conversions live in lean.adapters.
"""
import json
import logging
import os
import signal
import sqlite3
import tempfile
import threading
import time
from decimal import Decimal

from lean import adapters as A, candidates as C, held_risk as H, paper, strategy as S
from lean.paper import AccountingHalt
from lean.providers import PAPER_TAKER, ProviderError
from lean.store import StoreError

log = logging.getLogger('lean.runner')
SOL_MINT = A.SOL_MINT
MAX_KEYS_PER_CALL = 100
AUX_JOIN_S = 10.0             # LINT2b: total wait for the auxiliary threads on stop (TimeoutStopSec is 30 s)
UNEXITABLE_BLOCKS = ('NO_ROUTE', 'STUCK_POSITION', 'SELL_QUOTE_INVALID', 'SELL_QUOTE_MISSING')
EXIT_BACKOFF_BASE_S = 10.0    # M1 #13: first back-off of a failing exit (one position pass)
# M1b #1: exits that are never backed off (unless the quote said worthless, or the mark is zero)
PRIORITY_EXITS = frozenset({'STOP', 'DANGER', 'LIQUIDATE', 'MAX_HOLD', 'TIME_STOP', 'BOT_EXIT'})
PEAK_PERSIST_STEP = Decimal('1.01')    # M1 #13: a peak rise of >= 1% is always persisted at once


class _Halt(Exception):
    """Internal: stop this pass, the runner is halted."""


class Runner:
    def __init__(self, *, store, providers, exit_providers, strategy_cfg, discovery_db, state_dir, code_version,
                 screen_overrides=None, pool_fee_bps=25, taker=PAPER_TAKER, kill_switch=None, clock=time.time,
                 scan_limit=500, max_retries=3, sol_usd_ttl_s=30, stale_mark_s=30, unexitable_after_s=7200, route_check=None,
                 execution=None, held_risk=None, extra_sources=None, watchlist=None, ops=None, mark_model=None,
                 blocked_log_s=300, exit_backoff_max_s=60, peak_persist_s=60, discovery_stale_s=600, lean_config_hash=None):
        # --- L10 hook: latency-aware fills + cost model (None = today's instant fills). The priority fee + Jito tip enter
        # through the strategy config, so the fill fee, the marks and the entry/exit cost checks share one number. ---
        self.execution = execution
        if execution is not None:
            execution.validate_against(strategy_cfg)
            strategy_cfg = execution.strategy_cfg(strategy_cfg)
        # --- end L10 ---
        self.store, self.providers, self.exit_providers = store, providers, exit_providers
        self.cfg = strategy_cfg
        self.pcfg = A.paper_config(strategy_cfg)
        self.screen_cfg = A.screen_config(strategy_cfg, screen_overrides)
        self.discovery_db, self.state_dir = discovery_db, os.fspath(state_dir)
        self.code_version, self.strategy_version = code_version, strategy_cfg.strategy_version
        self.pool_fee_bps, self.taker, self.clock = int(pool_fee_bps), taker, clock
        self.kill_switch = kill_switch or os.path.join(self.state_dir, 'KILL')
        self.scan_limit, self.max_retries, self.sol_usd_ttl_s = scan_limit, max_retries, sol_usd_ttl_s
        self.stale_mark_s, self.unexitable_after_s = int(stale_mark_s), int(unexitable_after_s)
        self.health_path = os.path.join(self.state_dir, 'health.json')
        self.cursor_path = os.path.join(self.state_dir, 'cursor.json')
        self.stop = threading.Event()
        self._trade_lock = threading.RLock()       # final re-check + fill, and the day-start baseline
        self._counts_lock = threading.Lock()       # shared counters
        self._health_lock = threading.Lock()       # one health writer at a time
        self.counts = {'candidates': 0, 'screened': 0, 'passed': 0, 'entered': 0, 'exits': 0, 'closed': 0,
                       'errors': 0, 'retries': 0, 'duplicates': 0, 'quote_marks': 0, 'write_offs': 0}
        self.errors_by_code = {}
        self.last = {'candidate_loop': None, 'position_loop': None}         # pass start
        self.heartbeat = {'candidates': None, 'positions': None}            # end of each loop iteration
        self.dead = {}                                                       # loop name -> exception type
        self.marks = {}                            # mint -> (Decimal net mark SOL, mark_at, 'vaults' | 'quote')
        self.cost_basis_mints = []                 # positions valued at cost in the last portfolio (no usable mark)
        self.retry = []                            # [(candidate, attempt)]
        self._sol_usd = None                       # (Decimal, fetched_at)
        self.halted = None
        self._halt_persisted = False
        self.cursor = 0
        # --- L16 hook ---
        from lean.route_check import RouteChecker
        self.route_check = RouteChecker(store, route_check, clock=clock)    # off unless route_check.enabled
        # --- end L16 ---
        self.cursor_initialized = False
        # --- L07R hook ---
        self.paths = None                          # lean.paths.PathRecorder, built by run() only (l07r_start_paths)
        self._l07r_build = None                    # factory set by build_runner when paths.enabled
        self._l07r_thread = None
        self._l07r = None                          # runner MEMORY of the candidate being handled (screen, entry, fill, error)
        # --- end L07R ---
        # --- L11 hook ---
        self.held_risk = H.build(held_risk, self.store, code_version=code_version, strategy_version=self.strategy_version, clock=clock)
        # --- end L11 ---
        # --- L14 hook: extra candidate sources + soft-reject watchlist (all logic in lean.watchlist / lean.sources) ---
        self.extra_sources, self.watch, self._source_of = extra_sources, watchlist, {}
        from lean import watchlist as _l14
        _l14.install(self)
        # --- end L14 ---
        # --- L15 hook ---
        self.ops = ops                              # lean.ops.Ops or None (no ops block in lean.json = today's behaviour)
        if self.ops is not None:
            self.ops.attach(self)
        # --- end L15 ---
        # --- L12 hook ---
        self.features = None                       # lean.features.FeatureRecorder (set by build_runner when features.enabled)
        self._l12_screened = None                  # (candidate_id, Screen) of the candidate being handled
        # --- end L12 ---
        # --- L13 hook ---
        self.wallet_signals = None                 # lean.wallet_signals.WalletSignals (set by build_runner when configured)
        # --- end L13 ---
        # --- L20 hook ---
        self.flow = None                           # lean.flow.FlowCollector (set by build_runner when flow.enabled)
        # --- end L20 ---
        # --- L24 hook ---
        self.clusters = None                       # lean.clusters.Clusters (set by build_runner when configured)
        # --- end L24 ---
        # --- LINT2b hook: the feature / wallet-signal threads (health ``alive``; one shared shutdown deadline) ---
        self._aux_threads = {}                     # name -> Thread (features, wallet_signals, flow, bot_exit, clusters, risk_external)
        # --- end LINT2b ---
        # --- L22 hook ---
        self.honeypot = None                       # lean.honeypot_sim.HoneypotSim (set by build_runner when honeypot_sim.enabled)
        # --- end L22 ---
        # --- L23 hook ---
        self.bot_exit = None                       # lean.bot_exit.BotExit (set by build_runner when bot_exit.enabled)
        # --- end L23 ---
        # --- L21 hook ---
        self.risk_external = None                  # lean.risk_external.RiskExternal (set by build_runner when risk_external.enabled)
        # --- end L21 ---
        # --- M1 hook ---
        from lean import marks as _marks
        self.mark_model = mark_model if mark_model is not None else _marks.MarkModel(None, pool_fee_bps=self.pool_fee_bps)   # #7
        self.mark_info = {}                        # mint -> the inputs of its latest vault mark (model, reserves, fee)
        self.lean_config_hash = lean_config_hash   # #10 sha256 of the whole lean.json (None when not built from a file)
        self.discovery_stale_s = float(discovery_stale_s)
        self._feed = {'newest_at': None, 'stale': False, 'stale_since': None, 'events': 0}
        self.blocked_log_s, self.exit_backoff_max_s = float(blocked_log_s), float(exit_backoff_max_s)          # #13
        self.peak_persist_s = float(peak_persist_s)
        self._blocked_log = {}                     # mint -> [open_id, key, last_written_at, suppressed]
        self._exit_backoff = {}                    # mint -> [open_id, failures, next_attempt_at]
        self._pending_state = {}                   # mint -> (open_id, state): the in-memory (authoritative) unpersisted peak
        self._persisted = {}                       # mint -> (open_id, written_at, peak_ratio Decimal)
        self._raw_cache = {}                       # observation id -> raw quote body (route_check under raw_policy errors_only)
        self.m1_counts = {'blocked_suppressed': 0, 'exit_backoff_skips': 0, 'peak_updates_deferred': 0}
        self.last_outage = None
        # --- end M1 ---
        self.startup()
        # --- L10 hook ---
        if execution is not None and self.halted is None:
            self.store.record('execution_config', {'exec_delay_s': execution.cfg.exec_delay_s, 'tx_extra_lamports': execution.cfg.tx_extra_lamports,
                                                   'ata_rent_lamports': execution.cfg.ata_rent_lamports, 'fixed_fee_sol': str(self.cfg.fixed_fee_sol)},
                              code_version=self.code_version, strategy_version=self.strategy_version, ts=self.clock())
        # --- end L10 ---

    # -- lifecycle ------------------------------------------------------------------------------------------------
    def startup(self):
        """Persisted halt, store invariants and the cursor: checked before any loop runs."""
        self.halted = self.store.halt_reason()
        self._halt_persisted = self.halted is not None
        if self.halted:
            log.error('lean trader starts HALTED (new entries stopped, exits continue): %s', self.halted)
        try:
            self.store.check_invariants()
            self.store.check_position_states()
        except AccountingHalt as error:
            self._halt('ACCOUNTING_INVARIANT: %s' % error)
        except (StoreError, sqlite3.Error) as error:
            self._halt('STORE_FAILURE: %s' % type(error).__name__)
        self.cursor_initialized = os.path.exists(self.cursor_path)
        self.cursor = C.read_cursor(self.cursor_path)
        self._record_outage()                                                  # M1 #10
        if self.halted is None:
            self.store.record('start', {'cursor': self.cursor, 'config_hash': self.cfg.config_hash,
                                        'strategy_config_hash': self.cfg.config_hash, 'lean_config_hash': self.lean_config_hash,
                                        'marks': self.mark_model.cfg, 'raw_policy': getattr(self.store, 'raw_policy', 'all'),
                                        'paper': {'fee_lamports': self.pcfg.fee_lamports, 'slippage_bps': self.pcfg.slippage_bps},
                                        'pool_fee_bps': self.pool_fee_bps, 'stale_mark_s': self.stale_mark_s,
                                        'unexitable_after_s': self.unexitable_after_s,
                                        'logging': {'blocked_log_s': self.blocked_log_s, 'exit_backoff_max_s': self.exit_backoff_max_s,
                                                    'peak_persist_s': self.peak_persist_s, 'discovery_stale_s': self.discovery_stale_s},
                                        'screen': {k: str(v) for k, v in self.screen_cfg.items()}},
                              code_version=self.code_version, strategy_version=self.strategy_version, ts=self.clock())

    # --- M1 #10 hook ---
    def _record_outage(self):
        """At startup: an ``outage`` event from the previous run's last sign of life (health.json ``at`` and the newest
        store row) to now. Data only; never raises (a failure to write it is logged)."""
        try:
            previous_health = None
            try:
                with open(self.health_path) as stream:
                    value = json.load(stream).get('at')
                previous_health = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
            except (OSError, ValueError, AttributeError):
                pass
            last_write = None
            for table in ('events', 'observations', 'decisions', 'errors', 'fills', 'position_state'):
                row = self.store.db.execute('SELECT ts FROM %s ORDER BY id DESC LIMIT 1' % table).fetchone()
                if row and (last_write is None or row[0] > last_write):
                    last_write = row[0]
            seen = [t for t in (previous_health, last_write) if t is not None]
            if not seen:
                return None                                                  # a new store: nothing was down
            now = self.clock()
            down_since = max(seen)
            payload = {'previous_health_at': previous_health, 'last_store_write_at': last_write, 'down_since': down_since,
                       'up_at': now, 'duration_s': round(max(0.0, now - down_since), 3)}
            self.store.record('outage', payload, code_version=self.code_version, strategy_version=self.strategy_version, ts=now)
            self.last_outage = payload
            return payload
        except Exception:                                                    # noqa: BLE001 - data only, never stops a start
            log.exception('outage event could not be written')
            return None

    def _m1_feed(self, newest_at):
        try:
            self._feed_check(newest_at)
        except (StoreError, sqlite3.Error) as error:
            self._halt('STORE_FAILURE: %s' % type(error).__name__)
            raise _Halt from None
        except Exception:                                                    # noqa: BLE001 - data only
            log.exception('discovery staleness check failed')

    def _feed_check(self, newest_at):
        """Discovery staleness transitions: ``discovery_stale`` when the newest frame is older than ``discovery_stale_s``,
        ``discovery_recovered`` when frames flow again. ``newest_at`` None = unknown this pass (keep the last known)."""
        feed = self._feed
        if newest_at is not None and (feed['newest_at'] is None or newest_at > feed['newest_at']):
            feed['newest_at'] = float(newest_at)
        if feed['newest_at'] is None:
            return None
        now = self.clock()
        silent = now - feed['newest_at']
        if not feed['stale'] and silent > self.discovery_stale_s:
            feed.update(stale=True, stale_since=feed['newest_at'])
            kind, payload = 'discovery_stale', {'newest_frame_at': feed['newest_at'], 'silent_s': round(silent, 3),
                                                'threshold_s': self.discovery_stale_s}
        elif feed['stale'] and silent <= self.discovery_stale_s:
            since = feed['stale_since']
            feed.update(stale=False, stale_since=None)
            kind, payload = 'discovery_recovered', {'stale_since': since, 'newest_frame_at': feed['newest_at'],
                                                    'duration_s': round(feed['newest_at'] - since, 3)}
        else:
            return None
        feed['events'] += 1
        self.store.record(kind, payload, code_version=self.code_version, strategy_version=self.strategy_version, ts=now)
        return kind
    # --- end M1 ---

    def _now(self):
        return int(self.clock())

    def _halt(self, reason):
        """Stop NEW ENTRIES (exits continue). Logged at ERROR and persisted; never raises."""
        if self.halted is None:
            self.halted = reason
            log.error('lean trader HALTED (new entries stopped, exits continue): %s', reason)
            try:
                self.store.record('halt', {'reason': reason, 'at': self.clock()}, code_version=self.code_version,
                                  strategy_version=self.strategy_version, ts=self.clock())
                self._halt_persisted = True
            except Exception:              # the store itself may be what failed; the in-memory halt still holds
                self._halt_persisted = False
        try:
            self.write_health()
        except Exception:
            log.exception('health write failed during a halt')

    def refresh_halt(self):
        """Every pass: the store's halt state wins, so ``--clear-halt`` resumes a running process. A halt that could not
        be persisted (the store itself failed) is kept in memory."""
        try:
            persisted = self.store.halt_reason()
        except Exception:
            return
        if persisted is not None:
            if self.halted is None:
                log.error('lean trader HALTED by the store (new entries stopped, exits continue): %s', persisted)
            self.halted, self._halt_persisted = persisted, True
        elif self.halted is not None and self._halt_persisted:
            log.warning('lean trader halt cleared by an operator: %s', self.halted)
            self.halted, self._halt_persisted = None, False

    def _check(self):
        """After every fill: the whole history must reconcile, and every position must have its state."""
        try:
            self.store.check_invariants()
            self.store.check_position_states()
        except AccountingHalt as error:
            self._halt('ACCOUNTING_INVARIANT: %s' % error)
            raise _Halt from None

    # --- L15 hook ---
    def _beat(self, name):
        if self.ops is not None:
            self.ops.beat(name)
    # --- end L15 ---

    def _count(self, name, n=1):
        with self._counts_lock:
            self.counts[name] += n

    def _error(self, code, *, transient, mint=None, scope='candidate', message='', raw=None, candidate_id=None):
        """Record one isolated failure. A failure to record it is a store failure: halt."""
        with self._counts_lock:
            self.counts['errors'] += 1
            self.errors_by_code[code] = self.errors_by_code.get(code, 0) + 1
        try:
            if isinstance(raw, (bytes, bytearray)) and raw:
                self.store.add_observation('error:' + str(code)[:40], bytes(raw), mint=mint, candidate_id=candidate_id,
                                           meta={'code': code, 'scope': scope}, ts=self.clock())
            self.store.add_error(code, transient=transient, scope=scope, mint=mint, message=message, ts=self.clock())
        except (StoreError, sqlite3.Error, OSError) as error:
            self._halt('STORE_FAILURE: %s' % type(error).__name__)
            raise _Halt from None

    def entries_allowed(self):
        return self.halted is None and not os.path.exists(self.kill_switch)

    # -- shared helpers -------------------------------------------------------------------------------------------
    def _quote(self, providers, input_mint, output_mint, amount, kind, mint, candidate_id=None):
        """A Jupiter quote with its raw response kept; returns (provider Quote, observation id, observed_at)."""
        try:
            quote, raw, meta = providers.jupiter.quote(input_mint, output_mint, amount, self.taker)
        except ProviderError as error:
            error.kind = kind
            raise
        observed_at = self._now()
        ref = self.store.add_observation(kind, raw, mint=mint, candidate_id=candidate_id, ts=self.clock(),
                                         meta={'amount': amount, 'attempts': meta.get('attempts'), 'route': list(quote.route_labels),
                                               'in_amount': quote.in_amount, 'out_amount': quote.out_amount})      # M1: parsed fields
        # --- M1 #12: route_check reads the filled quote's body from here (the store may keep only its sha256) ---
        with self._counts_lock:
            self._raw_cache[ref] = bytes(raw) if isinstance(raw, (bytes, bytearray)) else raw
            while len(self._raw_cache) > 64:
                self._raw_cache.pop(next(iter(self._raw_cache)))
        # --- end M1 ---
        return quote, ref, observed_at

    def _sol_usd_value(self):
        """SOL/USD cached for ``sol_usd_ttl_s`` (Kraken allows one request per 2 s). A failure raises ProviderError."""
        now = self._now()
        if self._sol_usd is not None and 0 <= now - self._sol_usd[1] <= self.sol_usd_ttl_s:
            return self._sol_usd[0]
        price, raw, meta = self.providers.kraken.sol_usd()
        self.store.add_observation('sol_usd', raw, meta={'trade_at': meta.get('trade_at')}, ts=self.clock())
        self._sol_usd = (Decimal(price), now)
        return self._sol_usd[0]

    def usable_marks(self, now):
        """Marks younger than ``stale_mark_s`` (vault or quote marks) only: a stale mark never values equity."""
        return {m: v for m, v in dict(self.marks).items() if 0 <= now - v[1] <= self.stale_mark_s}

    def day_start_equity(self, now):
        """The persisted UTC-day baseline, rolled over at midnight only with fresh marks (strategy.roll_day_start)."""
        with self._trade_lock:
            equity, fresh = A.equity_and_freshness(self.store, now, self.usable_marks(now), self.cfg.price_ttl_seconds)
            last = self.store.latest_event('day_start')
            previous = (int(last[1]['day']), Decimal(last[1]['equity'])) if last else None
            current = S.roll_day_start(previous, now, equity, marks_fresh=fresh)
            if current != previous:
                self.store.record('day_start', {'day': current[0], 'equity': str(current[1]), 'marks_fresh': fresh},
                                  code_version=self.code_version, strategy_version=self.strategy_version, ts=self.clock())
            return current[1]

    def portfolio(self, now):
        """Equity at usable marks; a position without one is valued at cost and flagged (``cost_basis_mints``)."""
        marks = self.usable_marks(now)
        portfolio = A.portfolio(self.store, self.cfg, now, marks=marks, day_start_equity=self.day_start_equity(now))
        self.cost_basis_mints = sorted(m for m in portfolio.open_mints if m not in marks)
        return portfolio

    # -- candidate loop -------------------------------------------------------------------------------------------
    def candidate_pass(self):
        """Retries first, then new discovery frames. One candidate failing never affects another."""
        self.last['candidate_loop'] = self.clock()
        self.refresh_halt()
        self._beat('candidates')
        if not self.entries_allowed():
            return 0
        retries, self.retry = self.retry, []
        done = 0
        try:
            for candidate, attempt in retries:
                if self.stop.is_set() or not self.entries_allowed():
                    self.retry.append((candidate, attempt))
                    continue
                self._count('retries')
                self._isolated(candidate, attempt)
                done += 1
            try:
                if not self.cursor_initialized:
                    # first start: begin at the first frame inside the age window, not at the start of history
                    first = C.initial_cursor(self.discovery_db, now=self.clock(), max_age_seconds=self.screen_cfg['max_age_seconds'])
                    C.write_cursor(self.cursor_path, max(first, self.cursor))
                    self.cursor, self.cursor_initialized = max(first, self.cursor), True
                scan = C.scan_new(self.discovery_db, self.cursor, now=self.clock(), limit=self.scan_limit, cfg=self.screen_cfg)
            except C.DiscoveryUnavailable as error:
                self._error('DISCOVERY_UNAVAILABLE', transient=True, scope='discovery', message=str(error))
                self._m1_feed(None)                                            # M1 #10: an unreadable feed is silent too
                return done
            except (OSError, ValueError, sqlite3.Error) as error:
                self._error('DISCOVERY_INVALID', transient=False, scope='discovery', message=type(error).__name__)
                self._m1_feed(None)
                return done
            self._m1_feed((getattr(scan, 'stats', None) or {}).get('newest_received_at'))        # M1 #10
            handled_to = self.cursor
            for candidate in scan.candidates:
                if self.stop.is_set() or not self.entries_allowed():
                    break
                if self.store.candidate_id(candidate.mint) is not None:      # already handled (e.g. before a restart)
                    self._count('duplicates')
                else:
                    self._isolated(candidate, 1)
                    done += 1
                handled_to = candidate.seq
            else:
                handled_to = scan.next_cursor                                  # the whole batch: skip non-migration frames too
            if handled_to > self.cursor:
                C.write_cursor(self.cursor_path, handled_to)
                self.cursor = handled_to
        except _Halt:
            pass
        done += self._l14_pass()                                              # L14: after the fresh scan
        return done

    # --- L14 hook ---
    def _l14_pass(self):
        """Extra sources, then watchlist re-screens: after the pump scan, bounded, isolated, never raises."""
        from lean import watchlist as _l14
        return _l14.after_scan(self) if self.extra_sources is not None or self.watch is not None else 0
    # --- end L14 ---

    def _isolated(self, candidate, attempt):
        self._beat('candidates')
        try:
            self._handle_candidate(candidate, attempt)
        except _Halt:
            raise
        except AccountingHalt as error:
            self._halt('ACCOUNTING_INVARIANT: %s' % error)
            raise _Halt from None
        except (StoreError, sqlite3.Error) as error:
            self._halt('STORE_FAILURE: %s' % type(error).__name__)
            raise _Halt from None
        except ProviderError as error:
            self._error(error.code, transient=error.transient, mint=candidate.mint, scope=getattr(error, 'kind', 'candidate'),
                        message=getattr(error, 'kind', ''), raw=error.raw, candidate_id=self.store.candidate_id(candidate.mint))
            if error.transient and attempt < self.max_retries:
                self.retry.append((candidate, attempt + 1))
            self._l07r_note(error=error.code)                                  # --- L07R hook (one line) ---
        except Exception as error:                                             # isolation: recorded, never fatal
            self._error('CANDIDATE_FAILED', transient=False, mint=candidate.mint, message=type(error).__name__)
            self._l07r_note(error='CANDIDATE_FAILED')                           # --- L07R hook (one line) ---
        # --- L07R hook ---
        self._l07r_after(candidate)
        # --- end L07R ---
        # --- L12 hook ---
        self._l12_after(candidate)
        # --- end L12 ---

    # --- L12 hook ---
    def _l12_after(self, candidate):
        """Feature recorder: hand a handled candidate (entered or not) to the recorder, which reads what became of it from the
        store. A candidate that will be retried writes no row now (its final attempt does). Never raises, never blocks."""
        screened, self._l12_screened = self._l12_screened, None
        if self.features is None or screened is None or screened[1].error:
            return
        try:
            if any(queued.mint == candidate.mint for queued, _attempt in self.retry):
                return
            self.features.on_candidate(candidate, screened[0], screened[1])
        except Exception:                                                       # the trader never notices
            log.debug('feature recorder hook failed', exc_info=True)
    # --- end L12 ---

    # --- L21 hook ---
    def _l21_submit(self, candidate, cid, result):
        """Hand the screened candidate to the risk_external recorder (it keeps hazard-free ones). Never raises, never blocks."""
        if self.risk_external is None:
            return
        try:
            self.risk_external.submit(candidate, cid, result)
        except Exception:                                                       # the trader never notices
            log.debug('risk_external hook failed', exc_info=True)
    # --- end L21 ---

    # --- L07R hook ---
    def _l07r_note(self, **facts):
        """Remember what happened to the candidate being handled (memory only; the recorder never reads the store)."""
        if self._l07r is not None:
            self._l07r.update(facts)

    def _l07r_after(self, candidate):
        """Path recorder: start the price path of a handled candidate (entered or soft-rejected; the recorder skips
        hazards), decided on its final attempt from runner memory. Writes only the recorder's own paths.sqlite: no
        trader-store access, no provider I/O, no runner lock; never raises."""
        note, self._l07r = self._l07r, None
        if self.paths is None or note is None or note['mint'] != candidate.mint:
            return
        try:
            if any(queued.mint == candidate.mint for queued, _attempt in self.retry):
                return
            self.paths.on_candidate(note)
        except Exception:                                                       # the trader never notices
            log.debug('path recorder hook failed', exc_info=True)

    def l07r_start_paths(self):
        """Build and resume the recorder (``run()`` mode only; ``--once`` / ``--clear-halt`` never call this)."""
        if self.paths is None and self._l07r_build is not None:
            try:
                self.paths = self._l07r_build()
                if self.paths is not None:
                    self.paths.resume()
            except Exception:
                log.exception('path recorder could not start; trading continues without it')
                self.paths = None
        return self.paths
    # --- end L07R ---

    def _handle_candidate(self, candidate, attempt):
        mint = candidate.mint
        if attempt == 1:
            self._count('candidates')
        cid = self.store.add_candidate(mint, pool=candidate.pool, signature=candidate.signature, slot=candidate.slot,
                                       migrated_at=candidate.migrated_at, hint_seq=candidate.seq,
                                       meta={'payload_hash': candidate.payload_hash}, ts=self.clock())
        sol_usd = self._sol_usd_value()
        result = C.screen(candidate, self.providers, self.screen_cfg, now=self.clock(), sol_usd=sol_usd)
        self._count('screened')
        for label, raw in result.raw:
            self.store.add_observation('screen:' + label, raw, mint=mint, candidate_id=cid, ts=self.clock())
        action = 'PASS' if result.passed else 'FAILED' if result.error else 'REJECT'
        self.store.add_decision('screen', action, mint=mint, candidate_id=cid, reasons=result.reasons,
                                features={**result.features, 'unknowns': list(result.unknowns), 'attempt': attempt}, ts=self.clock())
        # --- L07R hook ---
        self._l07r = {'mint': mint, 'cid': cid, 'screen': result, 'entry_reasons': None, 'fill': None, 'error': None}
        # --- end L07R ---
        # --- L12 hook ---
        self._l12_screened = (cid, result)
        # --- end L12 ---
        # --- L21 hook: external risk collection starts right after the screen decision (non-blocking hand-over) ---
        self._l21_submit(candidate, cid, result)
        # --- end L21 ---
        if result.error:
            error = result.error
            self._error(error['code'], transient=error['transient'], mint=mint, scope='screen:%s' % error.get('stage'),
                        candidate_id=cid)
            if error['transient'] and attempt < self.max_retries:
                self.retry.append((candidate, attempt + 1))
            return
        if not result.passed:
            return
        self._count('passed')
        features = A.entry_features(result.features, mint)
        now = self._now()
        gate = S.entry_decision(features, None, self.portfolio(now), self.cfg)
        if gate.action != 'BUY' or not gate.quote_required:
            self.store.add_decision('entry', gate.action, mint=mint, candidate_id=cid, reasons=gate.reasons,
                                    features=_jsonable(gate.detail), ts=self.clock())
            self._l07r_note(entry_reasons=list(gate.reasons))                  # --- L07R hook (one line) ---
            return
        # --- L22 hook: honeypot sell simulation (MAIN lane) before any entry quote; True = rejected, decision recorded ---
        if self.honeypot is not None and self.honeypot.reject(self, candidate, cid, result, gate):
            return
        # --- end L22 ---
        # Quote at the decided size, then the sell leg for exactly the tokens the paper fill will hold.
        size_lamports = A.lamports(gate.size_sol)
        # --- L16 hook ---
        try:
            buy_q, buy_ref, buy_at = self._quote(self.providers, SOL_MINT, mint, size_lamports, 'quote:buy', mint, cid)
        except ProviderError as error:
            self.route_check.on_quote_error('entry', mint, error, candidate_id=cid, trade=cid)
            raise
        # --- end L16 ---
        sell_q, _, sell_at = self._quote(self.providers, mint, SOL_MINT, A.sell_leg_qty(buy_q.out_amount, self.pcfg),
                                         'quote:sell_leg', mint, cid)
        # --- L10 hook: wait exec_delay_s, re-quote, fill at the worse; None = ENTRY_ABORTED_LATENCY (recorded) ---
        xmeta = None
        if self.execution is not None:
            plan = self.execution.entry_requote(self, features, mint, cid, (buy_q, buy_ref, buy_at), (sell_q, sell_at), result.raw)
            if plan is None:
                return
            (buy_q, buy_ref, buy_at), (sell_q, sell_at), xmeta = plan
        # --- end L10 ---
        with self._trade_lock:
            if not self.entries_allowed():                                    # a halt may have started meanwhile
                return
            now = self._now()
            decision = S.entry_decision(features, A.roundtrip(buy_q, buy_at, sell_q, sell_at), self.portfolio(now), self.cfg)
            self.store.add_decision('entry', decision.action, mint=mint, candidate_id=cid, reasons=decision.reasons,
                                    features={**_jsonable(decision.detail), 'size_sol': str(decision.size_sol)}, ts=self.clock())
            self._l07r_note(entry_reasons=list(decision.reasons))              # --- L07R hook (one line) ---
            if decision.action != 'BUY':
                return
            fill = paper.buy(A.paper_quote(buy_q, mint=mint, side='buy', decimals=features['decimals'], ts=self.clock(),
                                           ref=buy_ref), decision.size_sol, self.pcfg)
            # --- L10 hook: ATA rent, transfer-fee haircut, both quotes + latency tax (stored with the fill via the open state) ---
            if xmeta is not None:
                fill, open_extra = self.execution.finish_buy(self, fill, xmeta)
                features = {**features, '_exec_state': open_extra}
            # --- end L10 ---
            # M1 #7: the screen's pool fields, for the marks; M1b #6: and the mark model + vault fee this position is marked with
            # until it closes (a config switch never re-marks an open position)
            from lean import marks as _marks
            features = {**features, '_pool_info': {**(_marks.screen_pool_info(result.features) or {}), **self.mark_model.pin()}}
            self.store.add_fill(fill, candidate_id=cid, state={'event': 'open', 'state': A.open_state(features, fill, self.cfg)})
            self.route_check.on_fill('entry', mint, buy_ref, candidate_id=cid, trade=cid, raw=self._raw_cache.get(buy_ref))   # L16 (+M1 raw)
            self._count('entered')
            self._l07r_note(fill=fill)                                         # --- L07R hook (one line) ---
            self._check()

    # -- position loop --------------------------------------------------------------------------------------------
    def position_pass(self):
        """Mark every open position; sell (paper) when an exit triggers. Runs under the kill switch AND while halted:
        a halt stops new entries only, never exits."""
        self.last['position_loop'] = self.clock()
        self.refresh_halt()
        self._beat('positions')                                               # L15 hook
        try:
            positions = self.store.positions()
            states = self.store.position_states()
            if not positions:
                self.marks.clear()
                return 0
            self._read_marks(positions, states)
            # --- L11 hook --- (no I/O: baselines, trigger detection, valuation of rug positions whose exit is failing)
            if self.held_risk is not None:
                self.held_risk.after_marks_safe(self, positions, states)
            # --- end L11 ---
            self._quote_marks(positions)
            now = self._now()
            liquidate = S.should_liquidate(self.portfolio(now), self.cfg)
            # --- L10 hook: two-phase exits (q0 for every triggered exit, ONE delay, then q1 + fills) ---
            if self.execution is not None:
                exits = self.execution.run_exits(self, positions, states, liquidate)
                # LINT1: L11's probes must run after the two-phase exits too (the early return skipped them)
                if self.held_risk is not None and not self.stop.is_set():
                    self.held_risk.after_exits_safe(self)
                return exits
            # --- end L10 ---
            exits = 0
            for mint, position in positions.items():
                if self.stop.is_set():
                    break
                try:
                    exits += self._isolated_exit(mint, position, states.get(mint), liquidate)
                except AccountingHalt as error:
                    self._halt('ACCOUNTING_INVARIANT: %s' % error)
                except (StoreError, sqlite3.Error) as error:
                    self._halt('STORE_FAILURE: %s' % type(error).__name__)
            # --- L11 hook --- (the only requests of the module: probes and account checks, AFTER every exit of this pass)
            if self.held_risk is not None and not self.stop.is_set():
                self.held_risk.after_exits_safe(self)
            # --- end L11 ---
            return exits
        except _Halt:
            return 0
        except AccountingHalt as error:
            self._halt('ACCOUNTING_INVARIANT: %s' % error)
        except (StoreError, sqlite3.Error) as error:
            self._halt('STORE_FAILURE: %s' % type(error).__name__)
        return 0

    def _read_marks(self, positions, states):
        """ONE getMultipleAccounts per <=50 positions (base + quote vault each) on the exit lane; net marks with read time.
        After a failed read, marks older than ``stale_mark_s`` are evicted."""
        mints = [m for m in positions if m in states]
        # M1 #7: an effective mark also reads the position's pool account (+1 key) and the fee schedule (+1 key per call).
        # M1b #6: each position is marked with the model it was OPENED with (pool_info.model), not the configured one.
        model = self.mark_model
        pins = {m: model.pinned(states[m]['state'].get('pool_info')) for m in mints}
        chunks, chunk, used = [], [], 0
        for mint in mints:
            need = model.keys_for(pins[mint][0])
            reserve = len(model.call_keys([pins[m][0] for m in chunk] + [pins[mint][0]]))
            if chunk and used + need + reserve > MAX_KEYS_PER_CALL:
                chunks.append(chunk)
                chunk, used = [], 0
            chunk.append(mint)
            used += need
        if chunk:
            chunks.append(chunk)
        for chunk in chunks:
            call_keys = model.call_keys([pins[m][0] for m in chunk])
            layouts = {m: model.position_keys(states[m]['state']['base_vault'], states[m]['state']['quote_vault'],
                                              states[m]['state'].get('pool'), model=pins[m][0]) for m in chunk}
            keys = [k for m in chunk for k in layouts[m]] + call_keys
            try:
                result, raw, _meta = self.exit_providers.helius.get_multiple_accounts(keys)
            except ProviderError as error:
                self._error(error.code, transient=error.transient, scope='marks', message='%d positions' % len(chunk), raw=error.raw)
                continue
            mark_at = self._now()
            values = result['value']
            position_keys = sum(len(v) for v in layouts.values())
            if call_keys:
                model.observe_call(values[position_keys:], mark_at)
            stored, offset = {}, 0
            for mint in chunk:
                pinned, vault_fee = pins[mint]
                accounts = values[offset:offset + len(layouts[mint])]
                offset += len(layouts[mint])
                try:
                    if pinned == 'effective':
                        net, info = model.value_accounts(positions[mint].qty_raw, accounts, now=mark_at, model='effective',
                                                         pool_info=states[mint]['state'].get('pool_info'), fee_lamports=self.pcfg.fee_lamports)
                        mark = A.sol(net)
                        base, quote = info['base_raw'], info['quote_raw']
                    else:                                                     # today's mark, at the fee the position opened with
                        base, quote = A.vault_amount(accounts[0]), A.vault_amount(accounts[1])
                        mark = A.mark(positions[mint].qty_raw, base, quote, pool_fee_bps=vault_fee, pcfg=self.pcfg)
                        info = {'model': 'vault', 'base_raw': base, 'quote_raw': quote, 'fee_bps': vault_fee}
                    if pinned != model.model:                                 # M1b #6: opened under the other model
                        info = {**info, 'pinned_from_open': True}
                    self.marks[mint] = (mark, mark_at, 'vaults')
                    self.mark_info[mint] = info
                    stored[mint] = {**info, 'net_lamports': int(mark * paper.LAMPORTS)}
                    # --- L11 hook ---
                    if self.held_risk is not None:
                        self.held_risk.observe(mint, base, quote, mark_at)
                    # --- end L11 ---
                except A.AdapterError as error:
                    # --- L11 hook ---
                    if self.held_risk is not None and None in (accounts[0], accounts[1]):
                        self.held_risk.observe_missing(mint, mark_at)
                    # --- end L11 ---
                    self._error('MARK_INVALID', transient=False, mint=mint, scope='marks', message=str(error))
            # the decoded inputs of every mark ride in the meta, so a mark is reproducible even when the body is not kept (#12)
            kinds = {','.join(('base', 'quote', 'pool')[:len(v)]) for v in layouts.values()}
            self.store.add_observation('marks', raw, meta={'mints': chunk, 'slot': result['context']['slot'], 'model': model.model,
                                                           'layout': kinds.pop() if len(kinds) == 1 else 'mixed',
                                                           'layouts': {m: len(v) for m, v in layouts.items()},
                                                           'call_keys': len(call_keys), 'marks': stored}, ts=self.clock())
        now = self._now()
        for mint in list(self.marks):
            if mint not in positions or now - self.marks[mint][1] > self.stale_mark_s:
                del self.marks[mint]

    def _quote_marks(self, positions):
        """D1: a position without a mark younger than ``stale_mark_s`` is marked by a Jupiter sell quote for its whole
        quantity (exit lane, independent of Helius): net = quoted SOL out (pool fees included) less the fixed fee."""
        now = self._now()
        for mint, position in positions.items():
            if self.stop.is_set():
                break
            held = self.marks.get(mint)
            if held is not None and now - held[1] <= self.stale_mark_s:
                continue
            try:
                quote, _ref, at = self._quote(self.exit_providers, mint, SOL_MINT, position.qty_raw, 'quote:mark', mint)
            except ProviderError as error:
                self._error(error.code, transient=error.transient, mint=mint, scope='quote:mark', raw=error.raw)
                continue
            self.marks[mint] = (A.sol(max(0, quote.out_amount - self.pcfg.fee_lamports)), at, 'quote')
            self._count('quote_marks')

    def _isolated_exit(self, mint, position, state, liquidate):
        self._beat('positions')
        try:
            if state is None:
                raise AccountingHalt('position %s has no state' % mint[:8])
            return self._manage(mint, position, state, liquidate)
        except (_Halt, AccountingHalt, StoreError, sqlite3.Error):
            raise
        except ProviderError as error:                      # the position stays open; retried next tick
            self._error(error.code, transient=error.transient, mint=mint, scope=getattr(error, 'kind', 'position'), raw=error.raw)
        except Exception as error:
            self._error('POSITION_CHECK_FAILED', transient=False, mint=mint, scope='position', message=type(error).__name__)
        return 0

    def _exit_failed(self, mint, open_id, state, why, updated):
        """A wanted exit could not be quoted/filled (no route / 4xx): remember since when (persisted, survives restarts)."""
        if state.get('exit_failing_since') is None:
            state = dict(state, exit_failing_since=self._now(), exit_failing_reason=why)
            self.store.add_position_state(mint, open_id, state, ts=self.clock())
            self._state_written(mint, open_id, state)
        elif updated:
            self._persist_updates(mint, open_id, state, updated)             # M1 #13: a peak-only change may wait
        return state

    # --- M1 #13 hooks: per-pass row spam ------------------------------------------------------------------------------
    def _state_written(self, mint, open_id, state):
        """Any position_state row of ``mint`` was just written (it carries the in-memory state): nothing is pending."""
        self._pending_state.pop(mint, None)
        try:
            peak = Decimal(str(state.get('peak_ratio')))
        except ArithmeticError:
            peak = None
        self._persisted[mint] = (open_id, self.clock(), peak)

    def _persist_updates(self, mint, open_id, state, updates, *, force=False):
        """Persist a strategy state update. A PEAK-ONLY change (``peak_ratio`` the only updated field) is written at most once
        per ``peak_persist_s`` unless the peak rose >= 1% since the last written row; meanwhile the in-memory state is the
        authoritative one (``_pending_state``, read by ``_manage`` and carried by every later row: rung, close, failure,
        update) and is flushed when the runner stops. A restart can lose at most that window of peak drift."""
        keys = set(updates) if isinstance(updates, (dict, set, list, tuple)) else set()
        if not force and self.peak_persist_s > 0 and keys == {'peak_ratio'}:
            last = self._persisted.get(mint)
            now = self.clock()
            try:
                peak = Decimal(str(state['peak_ratio']))
            except (KeyError, ArithmeticError):
                peak = None
            if (last is not None and last[0] == open_id and last[2] is not None and peak is not None
                    and 0 <= now - last[1] < self.peak_persist_s and peak < last[2] * PEAK_PERSIST_STEP):
                self._pending_state[mint] = (open_id, state)
                with self._counts_lock:
                    self.m1_counts['peak_updates_deferred'] += 1
                return False
        self.store.add_position_state(mint, open_id, state, ts=self.clock())
        self._state_written(mint, open_id, state)
        return True

    def flush_pending_states(self):
        """Write every deferred peak-only state (on stop). Never raises for one position."""
        for mint, (open_id, state) in list(self._pending_state.items()):
            try:
                self.store.add_position_state(mint, open_id, state, ts=self.clock())
                self._state_written(mint, open_id, state)
            except Exception:                                                # noqa: BLE001 - the position may have closed
                self._pending_state.pop(mint, None)

    def _forget(self, mint):
        for table in (self._pending_state, self._persisted, self._blocked_log, self._exit_backoff, self.mark_info):
            table.pop(mint, None)

    def _backoff_active(self, mint, open_id, reason, mark):
        """True when this pass must NOT re-quote the wanted exit ``reason``. M1b #1: a STOP / DANGER / LIQUIDATE / MAX_HOLD /
        TIME_STOP / BOT_EXIT exit is never backed off unless its quote proved the position worthless (STUCK_POSITION) or the
        mark is zero: the route may come back on any pass. A different exit reason than the failed one resets the back-off."""
        held = self._exit_backoff.get(mint)
        if self.exit_backoff_max_s <= 0 or held is None or held[0] != open_id:
            return False
        if held[3] != reason:
            self._exit_backoff.pop(mint, None)
            return False
        if reason in PRIORITY_EXITS and not held[4] and not (mark is not None and mark <= 0):
            return False
        return self.clock() < held[2]

    def _exit_attempt_failed(self, mint, open_id, reason, *, stuck):
        """No route / an unexitable quote / a failing quote on a zero mark: the next exit quote waits 10 s, 20 s, 40 s ... capped
        at ``exit_backoff_max_s`` (see ``_backoff_active`` for which exits may wait). Only the re-quoting backs off: the D2
        write-off clock (``exit_failing_since``) is persisted and checked every pass. ``stuck``: the quote said worthless."""
        if self.exit_backoff_max_s <= 0:
            return
        held = self._exit_backoff.get(mint)
        failures = held[1] + 1 if held is not None and held[0] == open_id and held[3] == reason else 1
        delay = min(self.exit_backoff_max_s, EXIT_BACKOFF_BASE_S * 2 ** (failures - 1))
        self._exit_backoff[mint] = [open_id, failures, self.clock() + delay, reason, bool(stuck)]

    def _exit_decision_row(self, mint, open_id, final, features):
        """The exit decision row. An unchanged BLOCKED (same reasons, same lifecycle) is written at most once per ``blocked_log_s``;
        the next row written carries ``suppressed_since_last``. Any other action resets it."""
        if final.action != 'BLOCKED' or self.blocked_log_s <= 0:
            self._blocked_log.pop(mint, None)
            self.store.add_decision('exit', final.action, mint=mint, reasons=final.reasons, ts=self.clock(), features=features)
            return True
        key, now = tuple(final.reasons), self.clock()
        held = self._blocked_log.get(mint)
        if held is not None and held[0] == open_id and held[1] == key and 0 <= now - held[2] < self.blocked_log_s:
            held[3] += 1
            with self._counts_lock:
                self.m1_counts['blocked_suppressed'] += 1
            return False
        suppressed = held[3] if held is not None and held[0] == open_id else 0
        self.store.add_decision('exit', final.action, mint=mint, reasons=final.reasons, ts=self.clock(),
                                features={**features, 'suppressed_since_last': suppressed})
        self._blocked_log[mint] = [open_id, key, now, 0]
        return True
    # --- end M1 ---

    def _write_off(self, mint, position, open_id, state):
        """D2: zero-proceeds paper write-off of a position whose exit stayed impossible for ``unexitable_after_s``."""
        with self._trade_lock:
            now = self._now()
            fill = paper.write_off(position, ts=self.clock())
            # --- L10 hook: the ATA rent of a written-off position is lost: record it so the outstanding rent clears ---
            if self.execution is not None:
                state = {**state, **self.execution.write_off_state(state)}
            # --- end L10 ---
            reason = 'RUG_WRITEOFF' if state.get('rug_trigger') else 'UNEXITABLE'          # L11: chosen by the detector
            closed = dict(state, reason=reason, trade_pnl_lamports=position.realized_lamports + fill.realized_lamports,
                          cooldown_until=S.cooldown_until('STOP' if state.get('rug_trigger') else 'UNEXITABLE', now, self.cfg))
            self.store.add_decision('exit', 'WRITE_OFF', mint=mint, reasons=[reason], ts=self.clock(),
                                    features={'failing_since': state.get('exit_failing_since'), 'why': state.get('exit_failing_reason'),
                                              'qty_raw': position.qty_raw})
            self.store.add_fill(fill, state={'event': 'closed', 'open_fill_id': open_id, 'state': closed})
            self._forget(mint)                                                                      # M1 #13
            self.marks.pop(mint, None)
            self._count('write_offs')
            self._count('closed')
            self._check()
            return 1

    def _manage(self, mint, position, row, liquidate):
        open_id, state = row['open_fill_id'], row['state']
        # --- M1 #13: a deferred peak-only update is the authoritative state of this lifecycle ---
        pending = self._pending_state.get(mint)
        if pending is not None:
            if pending[0] == open_id:
                state = pending[1]
            else:
                self._pending_state.pop(mint, None)
        # --- end M1 ---
        # --- L11 hook --- (a rug trigger makes the exit wanted as DANGER and is stamped on the state, durably once it is saved)
        rug = None if self.held_risk is None else self.held_risk.trigger(mint, open_id)
        if rug and state.get('rug_trigger') != rug:
            state = dict(state, rug_trigger=rug)
        # --- end L11 ---
        bot = None if self.bot_exit is None else self.bot_exit.trigger(mint, open_id)     # --- L23 hook (live mode only) ---
        now = self._now()
        since = state.get('exit_failing_since')
        if since is not None and now - int(since) >= self.unexitable_after_s:
            return self._write_off(mint, position, open_id, state)
        mark, mark_at, _source = self.marks.get(mint, (None, None, None))
        held = A.strategy_position(position, state)
        decision = S.exit_decision(held, mark, None, now, self.cfg, mark_at=mark_at, liquidate=liquidate, danger=bool(rug),
                                   bot_exit=bool(bot))                                    # L23
        if decision.position_updates:
            state = A.apply_updates(state, decision.position_updates)
            held = A.strategy_position(position, state)
        if decision.action == 'HOLD' and since is not None:            # the exit is no longer wanted: forget the failure
            state = {k: v for k, v in state.items() if k not in ('exit_failing_since', 'exit_failing_reason')}
        if decision.action != 'SELL':
            self._exit_backoff.pop(mint, None)                         # M1 #13: the exit is no longer wanted
        if decision.action != 'SELL' or not decision.quote_required:
            if decision.position_updates or (decision.action == 'HOLD' and since is not None):
                self._persist_updates(mint, open_id, state, decision.position_updates,
                                      force=decision.action == 'HOLD' and since is not None)          # M1 #13
            return 0
        # --- M1 #13: a failing exit (no route, unexitable quote, zero mark) is re-quoted with a back-off, not every pass ---
        if self._backoff_active(mint, open_id, decision.reason, mark):
            with self._counts_lock:
                self.m1_counts['exit_backoff_skips'] += 1
            if decision.position_updates:
                self._persist_updates(mint, open_id, state, decision.position_updates)
            return 0
        # --- end M1 ---
        qty = A.sell_qty_raw(decision.qty, position.qty_raw)
        try:
            quote, ref, quote_at = self._quote(self.exit_providers, mint, SOL_MINT, qty, 'quote:exit', mint)
        except ProviderError as error:
            if qty == position.qty_raw:                                             # --- L16 hook --- (full exits only)
                self.route_check.on_quote_error('exit', mint, error, trade=open_id)  # --- end L16 ---
            if not error.transient or (mark is not None and mark <= 0):            # M1 #13: no route, or a zero mark
                self._exit_attempt_failed(mint, open_id, decision.reason, stuck=False)
            if not error.transient:
                self._exit_failed(mint, open_id, state, error.code, bool(decision.position_updates))
            elif decision.position_updates:
                self._persist_updates(mint, open_id, state, decision.position_updates)                # M1 #13
            raise
        # --- L10 hook: phase 1 defers here; phase 2 (and a direct call) gets q1 and the pair of quotes ---
        xmeta = None
        if self.execution is not None:
            quote, ref, quote_at, xmeta = self.execution.after_exit_quote(self, mint, qty, quote, ref, quote_at)
        # --- end L10 ---
        with self._trade_lock:
            now = self._now()
            final = S.exit_decision(held, mark, A.strategy_quote(quote, 'sell', quote_at), now, self.cfg, mark_at=mark_at,
                                    liquidate=liquidate, danger=bool(rug), bot_exit=bool(bot))     # L23
            self._exit_decision_row(mint, open_id, final,                                     # M1 #13: BLOCKED throttled
                                    {**_jsonable(final.detail), 'qty_raw': qty, 'fraction_of_initial': str(final.fraction),
                                     'quote_ref': ref, 'mark_sol': None if mark is None else str(mark),
                                     'mark_source': _source, 'mark_info': _jsonable(self.mark_info.get(mint))})
            if final.action != 'SELL' or A.sell_qty_raw(final.qty, position.qty_raw) != qty:
                if final.action == 'BLOCKED' and final.reason in UNEXITABLE_BLOCKS:
                    self._exit_attempt_failed(mint, open_id, decision.reason, stuck=final.reason == 'STUCK_POSITION')   # M1 #13, M1b #1
                    self._exit_failed(mint, open_id, state, final.reason, bool(decision.position_updates))
                elif decision.position_updates:
                    self._persist_updates(mint, open_id, state, decision.position_updates)  # M1 #13
                return 0
            fill = paper.sell(position, A.paper_quote(quote, mint=mint, side='sell', decimals=position.decimals, ts=self.clock(),
                                                      ref=ref), cfg=self.pcfg, qty_raw=qty)
            # --- L10 hook: transfer-fee haircut, ATA rent refund on a full close, both quotes + latency tax ---
            if xmeta is not None:
                fill, sell_extra = self.execution.finish_sell(fill, position, state, xmeta)
                state = {**state, **sell_extra}
            # --- end L10 ---
            state = {k: v for k, v in state.items() if k not in ('exit_failing_since', 'exit_failing_reason')}
            if fill.qty_raw == position.qty_raw:
                closed = dict(state, reason=final.reason, trade_pnl_lamports=position.realized_lamports + fill.realized_lamports,
                              cooldown_until=S.cooldown_until('STOP' if rug else final.reason, now, self.cfg))     # L11: a rug cools down like a stop
                self.store.add_fill(fill, state={'event': 'closed', 'open_fill_id': open_id, 'state': closed})
                self._forget(mint)                                                                  # M1 #13
                self.route_check.on_fill('exit', mint, ref, trade=open_id, raw=self._raw_cache.get(ref))   # L16 (+M1 raw)
                self.marks.pop(mint, None)
                self._count('closed')
            else:
                rung = dict(A.apply_updates(state, final.after_fill), last_reason=final.reason)
                self.store.add_fill(fill, state={'event': 'rung', 'open_fill_id': open_id, 'state': rung})
                self._state_written(mint, open_id, rung)                                            # M1 #13
                self._exit_backoff.pop(mint, None)
            self._count('exits')
            self._check()
            return 1

    # -- health / lifecycle ---------------------------------------------------------------------------------------
    def health(self):
        try:
            positions, cash = self.store.positions(), self.store.cash()
        except Exception:
            positions, cash = {}, None
        with self._counts_lock:
            counts, errors = dict(self.counts), dict(self.errors_by_code)
        marks = dict(self.marks)
        return {'kind': 'lean_health_v1', 'at': self.clock(), 'code_version': self.code_version,
                'strategy_version': self.strategy_version, 'halted': self.halted, 'entries_allowed': self.entries_allowed(),
                'kill_switch': os.path.exists(self.kill_switch), 'last_loop': dict(self.last), 'heartbeat': dict(self.heartbeat),
                'dead_loops': dict(self.dead), 'counts': counts, 'errors_by_code': errors, 'open_positions': sorted(positions),
                'marks': {m: {'source': v[2], 'at': v[1]} for m, v in marks.items()},
                'cost_basis_mints': list(self.cost_basis_mints), 'cash_lamports': cash, 'cursor': self.cursor,
                'retry_queue': len(self.retry), 'execution_status': 'EXECUTION_UNVERIFIED', 'live_readiness': False,
                # --- L12 hook ---
                'features': self._aux_health('features', self.features),
                # --- end L12 ---
                # --- L13 hook ---
                'wallet_signals': self._aux_health('wallet_signals', self.wallet_signals),
                # --- end L13 ---
                # --- L20 hook ---
                'flow': self._aux_health('flow', self.flow),
                'sell_evidence': self._sell_evidence_health(),
                # --- end L20 ---
                # --- L24 hook ---
                'clusters': self._aux_health('clusters', self.clusters),
                # --- end L24 ---
                # --- L21 hook ---
                'risk_external': self._aux_health('risk_external', self.risk_external),
                # --- end L21 ---
                # --- L07R hook ---
                'paths': self._l07r_health(),
                # --- end L07R ---
                'held_risk': None if self.held_risk is None else self.held_risk.health(),      # L11
                'honeypot_sim': None if self.honeypot is None else self.honeypot.health(),     # L22
                'bot_exit': self._aux_health('bot_exit', self.bot_exit),                        # L23
                # --- M1 hook ---
                'lean_config_hash': self.lean_config_hash, 'strategy_config_hash': self.cfg.config_hash,
                'marks_model': self.mark_model.health(), 'raw_policy': getattr(self.store, 'raw_policy', 'all'),
                'discovery_feed': {**self._feed, 'stale_after_s': self.discovery_stale_s},
                'last_outage': self.last_outage, 'log_throttle': self._m1_health(),
                # --- end M1 ---
                # --- L15 hook ---
                **({} if self.ops is None else self.ops.health())}
                # --- end L15 ---

    # --- M1 hook ---
    def _m1_health(self):
        with self._counts_lock:
            counts = dict(self.m1_counts)
        return {**counts, 'blocked_log_s': self.blocked_log_s, 'exit_backoff_max_s': self.exit_backoff_max_s,
                'peak_persist_s': self.peak_persist_s, 'backing_off': sorted(self._exit_backoff),
                'pending_peak_states': len(self._pending_state)}
    # --- end M1 ---

    # --- L20 hook ---
    def _sell_evidence_health(self):
        prober = getattr(self.execution, 'sell_evidence', None)
        if prober is None:
            return {'enabled': False}
        try:
            return prober.health()
        except Exception as error:
            return {'enabled': True, 'health_error': type(error).__name__}
    # --- end L20 ---

    # --- LINT2b hook ---
    def _aux_health(self, name, worker):
        """A feature thread's own health (counters + queue depth) plus ``alive``. Never raises."""
        thread = self._aux_threads.get(name)
        alive = thread is not None and thread.is_alive()
        if worker is None:
            return {'enabled': False, 'alive': alive}
        try:
            return {**worker.health(), 'alive': alive}
        except Exception as error:
            return {'enabled': True, 'alive': alive, 'health_error': type(error).__name__}
    # --- end LINT2b ---

    # --- L07R hook ---
    def _l07r_health(self):
        """Recorder counters plus ``alive`` (its thread is running). Never raises."""
        alive = self._l07r_thread is not None and self._l07r_thread.is_alive()
        if self.paths is None:
            return {'enabled': self._l07r_build is not None, 'alive': alive}
        try:
            return {**self.paths.health(), 'alive': alive}
        except Exception as error:
            return {'enabled': True, 'alive': alive, 'health_error': type(error).__name__}
    # --- end L07R ---

    def write_health(self):
        """Atomic: a unique temp file in the state dir, fsync, rename; one writer at a time."""
        with self._health_lock:
            fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix='.health.', suffix='.tmp')
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump(self.health(), stream, sort_keys=True, default=str)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, self.health_path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
                raise

    def _loop(self, name, work, interval):
        """One loop thread. A failing pass is recorded and the loop goes on; anything that escapes (a BaseException,
        a bug in the loop itself) marks the loop DEAD and stops the other one, so the process exits non-zero."""
        try:
            while not self.stop.is_set():
                try:
                    work()
                except Exception as error:                   # a pass bug must not kill the loop
                    log.exception('%s pass failed', name)
                    try:
                        self._error('LOOP_FAILED', transient=False, scope=name, message=type(error).__name__)
                    except Exception:
                        pass
                self.heartbeat[name] = self.clock()
                try:
                    self.write_health()
                except Exception:
                    log.exception('health write failed')
                self.stop.wait(interval)
        except BaseException as error:                       # noqa: B902 - a dying loop must be noticed
            self.dead[name] = type(error).__name__
            log.critical('%s loop DIED (%s): stopping the process', name, type(error).__name__)
            self.stop.set()

    # --- LINT4 hook ---
    def _aux_specs(self):
        """``[(name, target, args)]`` of the auxiliary daemon threads ``run`` starts (only the configured ones), each wired to
        ``self.stop``. The path recorder is started separately (it is built and resumed before the candidate loop)."""
        specs = []
        if self.features is not None:                                       # L12: on stop it writes the queued rows unenriched
            specs.append(('features', self.features.run, (self.stop,)))
        for name in ('wallet_signals', 'flow', 'clusters'):                 # L13, L20, L24: run_loop() polls their own .stop
            worker = getattr(self, name)
            if worker is not None:
                worker.stop = self.stop
                specs.append((name, worker.run_loop, ()))
        if self.bot_exit is not None:                                       # L23 (shadow by default)
            self.bot_exit.stop = self.stop
            specs.append(('bot_exit', self.bot_exit.run_loop, (self,)))
        if self.risk_external is not None:                                  # L21 (its own RugCheck bucket)
            specs.append(('risk_external', self.risk_external.run, (self.stop,)))
        return specs
    # --- end LINT4 ---

    def run(self, *, candidate_interval=5.0, position_interval=10.0, install_signals=True):
        """Both loops until SIGTERM/SIGINT (or ``stop``). Returns True on a clean stop, False if a loop died."""
        if install_signals:
            signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
            signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        threads = [threading.Thread(target=self._loop, args=('candidates', self.candidate_pass, candidate_interval), name='candidates'),
                   threading.Thread(target=self._loop, args=('positions', self.position_pass, min(10.0, position_interval)),
                                    name='positions')]
        # --- L07R hook: built and resumed here (run mode only), BEFORE the candidate loop starts; then its own daemon
        # thread, whose death or slowness never stops the trader ---
        recorder = None
        if self.l07r_start_paths() is not None:
            recorder = self._l07r_thread = threading.Thread(target=self.paths.run, args=(self.stop,), name='paths',
                                                            daemon=True)
        # --- end L07R ---
        for t in threads:
            t.start()
        # --- L07R hook ---
        if recorder is not None:
            recorder.start()
        # --- end L07R ---
        # --- L15 hook ---
        threads += [] if self.ops is None else self.ops.start(self)      # Ops.start returns started threads
        # --- end L15 ---
        # --- LINT4 hook: every auxiliary data-collection thread (L12 features, L13 wallet_signals, L20 flow, L23 bot_exit, L24
        # clusters, L21 risk_external) is a daemon started here; none of them runs on the exit or candidate thread ---
        aux = [] if recorder is None else [recorder]
        for name, target, args in self._aux_specs():
            thread = self._aux_threads[name] = threading.Thread(target=target, args=args, name=name, daemon=True)
            thread.start()
            aux.append(thread)
        # --- end LINT4 ---
        for t in threads:
            while t.is_alive():
                t.join(0.5)
        # --- LINT2b/LINT4 hook: the path recorder and EVERY auxiliary thread share ONE deadline of AUX_JOIN_S in total, so a slow one
        # can never push the stop past systemd's TimeoutStopSec (30 s); each checks ``stop`` before every request, so they normally
        # end within one in-flight call. A thread still alive at the deadline is a daemon: the process exits without it ---
        aux_deadline = time.monotonic() + AUX_JOIN_S
        for thread in aux:
            thread.join(max(0.0, aux_deadline - time.monotonic()))
        # --- end LINT2b/LINT4 ---
        self.flush_pending_states()                                            # M1 #13: deferred peak-only states
        try:
            self.write_health()
        except Exception:
            log.exception('final health write failed')
        return not self.dead


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value
