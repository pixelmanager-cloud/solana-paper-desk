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

from lean import adapters as A, candidates as C, paper, strategy as S
from lean.paper import AccountingHalt
from lean.providers import PAPER_TAKER, ProviderError
from lean.store import StoreError

log = logging.getLogger('lean.runner')
SOL_MINT = A.SOL_MINT
MAX_KEYS_PER_CALL = 100
UNEXITABLE_BLOCKS = ('NO_ROUTE', 'STUCK_POSITION', 'SELL_QUOTE_INVALID', 'SELL_QUOTE_MISSING')


class _Halt(Exception):
    """Internal: stop this pass, the runner is halted."""


class Runner:
    def __init__(self, *, store, providers, exit_providers, strategy_cfg, discovery_db, state_dir, code_version,
                 screen_overrides=None, pool_fee_bps=25, taker=PAPER_TAKER, kill_switch=None, clock=time.time,
                 scan_limit=500, max_retries=3, sol_usd_ttl_s=30, stale_mark_s=30, unexitable_after_s=7200):
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
        self.cursor_initialized = False
        self.startup()

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
        if self.halted is None:
            self.store.record('start', {'cursor': self.cursor, 'config_hash': self.cfg.config_hash,
                                        'paper': {'fee_lamports': self.pcfg.fee_lamports, 'slippage_bps': self.pcfg.slippage_bps},
                                        'pool_fee_bps': self.pool_fee_bps, 'stale_mark_s': self.stale_mark_s,
                                        'unexitable_after_s': self.unexitable_after_s,
                                        'screen': {k: str(v) for k, v in self.screen_cfg.items()}},
                              code_version=self.code_version, strategy_version=self.strategy_version, ts=self.clock())

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
                                         meta={'amount': amount, 'attempts': meta.get('attempts'), 'route': list(quote.route_labels)})
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
                return done
            except (OSError, ValueError, sqlite3.Error) as error:
                self._error('DISCOVERY_INVALID', transient=False, scope='discovery', message=type(error).__name__)
                return done
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
        return done

    def _isolated(self, candidate, attempt):
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
        except Exception as error:                                             # isolation: recorded, never fatal
            self._error('CANDIDATE_FAILED', transient=False, mint=candidate.mint, message=type(error).__name__)

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
            return
        # Quote at the decided size, then the sell leg for exactly the tokens the paper fill will hold.
        size_lamports = A.lamports(gate.size_sol)
        buy_q, buy_ref, buy_at = self._quote(self.providers, SOL_MINT, mint, size_lamports, 'quote:buy', mint, cid)
        sell_q, _, sell_at = self._quote(self.providers, mint, SOL_MINT, A.sell_leg_qty(buy_q.out_amount, self.pcfg),
                                         'quote:sell_leg', mint, cid)
        with self._trade_lock:
            if not self.entries_allowed():                                    # a halt may have started meanwhile
                return
            now = self._now()
            decision = S.entry_decision(features, A.roundtrip(buy_q, buy_at, sell_q, sell_at), self.portfolio(now), self.cfg)
            self.store.add_decision('entry', decision.action, mint=mint, candidate_id=cid, reasons=decision.reasons,
                                    features={**_jsonable(decision.detail), 'size_sol': str(decision.size_sol)}, ts=self.clock())
            if decision.action != 'BUY':
                return
            fill = paper.buy(A.paper_quote(buy_q, mint=mint, side='buy', decimals=features['decimals'], ts=self.clock(),
                                           ref=buy_ref), decision.size_sol, self.pcfg)
            self.store.add_fill(fill, candidate_id=cid, state={'event': 'open', 'state': A.open_state(features, fill, self.cfg)})
            self._count('entered')
            self._check()

    # -- position loop --------------------------------------------------------------------------------------------
    def position_pass(self):
        """Mark every open position; sell (paper) when an exit triggers. Runs under the kill switch AND while halted:
        a halt stops new entries only, never exits."""
        self.last['position_loop'] = self.clock()
        self.refresh_halt()
        try:
            positions = self.store.positions()
            states = self.store.position_states()
            if not positions:
                self.marks.clear()
                return 0
            self._read_marks(positions, states)
            self._quote_marks(positions)
            now = self._now()
            liquidate = S.should_liquidate(self.portfolio(now), self.cfg)
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
        per_call = MAX_KEYS_PER_CALL // 2
        for start in range(0, len(mints), per_call):
            chunk = mints[start:start + per_call]
            keys = [k for m in chunk for k in (states[m]['state']['base_vault'], states[m]['state']['quote_vault'])]
            try:
                result, raw, _meta = self.exit_providers.helius.get_multiple_accounts(keys)
            except ProviderError as error:
                self._error(error.code, transient=error.transient, scope='marks', message='%d positions' % len(chunk), raw=error.raw)
                continue
            mark_at = self._now()
            self.store.add_observation('marks', raw, meta={'mints': chunk, 'slot': result['context']['slot']}, ts=self.clock())
            for i, mint in enumerate(chunk):
                try:
                    base, quote = A.vault_amount(result['value'][2 * i]), A.vault_amount(result['value'][2 * i + 1])
                    self.marks[mint] = (A.mark(positions[mint].qty_raw, base, quote, pool_fee_bps=self.pool_fee_bps, pcfg=self.pcfg),
                                        mark_at, 'vaults')
                except A.AdapterError as error:
                    self._error('MARK_INVALID', transient=False, mint=mint, scope='marks', message=str(error))
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
            updated = True
        if updated:
            self.store.add_position_state(mint, open_id, state, ts=self.clock())
        return state

    def _write_off(self, mint, position, open_id, state):
        """D2: zero-proceeds paper write-off of a position whose exit stayed impossible for ``unexitable_after_s``."""
        with self._trade_lock:
            now = self._now()
            fill = paper.write_off(position, ts=self.clock())
            closed = dict(state, reason='UNEXITABLE', trade_pnl_lamports=position.realized_lamports + fill.realized_lamports,
                          cooldown_until=S.cooldown_until('UNEXITABLE', now, self.cfg))
            self.store.add_decision('exit', 'WRITE_OFF', mint=mint, reasons=['UNEXITABLE'], ts=self.clock(),
                                    features={'failing_since': state.get('exit_failing_since'), 'why': state.get('exit_failing_reason'),
                                              'qty_raw': position.qty_raw})
            self.store.add_fill(fill, state={'event': 'closed', 'open_fill_id': open_id, 'state': closed})
            self.marks.pop(mint, None)
            self._count('write_offs')
            self._count('closed')
            self._check()
            return 1

    def _manage(self, mint, position, row, liquidate):
        open_id, state = row['open_fill_id'], row['state']
        now = self._now()
        since = state.get('exit_failing_since')
        if since is not None and now - int(since) >= self.unexitable_after_s:
            return self._write_off(mint, position, open_id, state)
        mark, mark_at, _source = self.marks.get(mint, (None, None, None))
        held = A.strategy_position(position, state)
        decision = S.exit_decision(held, mark, None, now, self.cfg, mark_at=mark_at, liquidate=liquidate)
        if decision.position_updates:
            state = A.apply_updates(state, decision.position_updates)
            held = A.strategy_position(position, state)
        if decision.action == 'HOLD' and since is not None:            # the exit is no longer wanted: forget the failure
            state = {k: v for k, v in state.items() if k not in ('exit_failing_since', 'exit_failing_reason')}
        if decision.action != 'SELL' or not decision.quote_required:
            if decision.position_updates or (decision.action == 'HOLD' and since is not None):
                self.store.add_position_state(mint, open_id, state, ts=self.clock())
            return 0
        qty = A.sell_qty_raw(decision.qty, position.qty_raw)
        try:
            quote, ref, quote_at = self._quote(self.exit_providers, mint, SOL_MINT, qty, 'quote:exit', mint)
        except ProviderError as error:
            if not error.transient:
                self._exit_failed(mint, open_id, state, error.code, bool(decision.position_updates))
            elif decision.position_updates:
                self.store.add_position_state(mint, open_id, state, ts=self.clock())
            raise
        with self._trade_lock:
            now = self._now()
            final = S.exit_decision(held, mark, A.strategy_quote(quote, 'sell', quote_at), now, self.cfg, mark_at=mark_at,
                                    liquidate=liquidate)
            self.store.add_decision('exit', final.action, mint=mint, reasons=final.reasons, ts=self.clock(),
                                    features={**_jsonable(final.detail), 'qty_raw': qty, 'fraction_of_initial': str(final.fraction),
                                              'quote_ref': ref, 'mark_sol': None if mark is None else str(mark),
                                              'mark_source': _source})
            if final.action != 'SELL' or A.sell_qty_raw(final.qty, position.qty_raw) != qty:
                if final.action == 'BLOCKED' and final.reason in UNEXITABLE_BLOCKS:
                    self._exit_failed(mint, open_id, state, final.reason, bool(decision.position_updates))
                elif decision.position_updates:
                    self.store.add_position_state(mint, open_id, state, ts=self.clock())
                return 0
            fill = paper.sell(position, A.paper_quote(quote, mint=mint, side='sell', decimals=position.decimals, ts=self.clock(),
                                                      ref=ref), cfg=self.pcfg, qty_raw=qty)
            state = {k: v for k, v in state.items() if k not in ('exit_failing_since', 'exit_failing_reason')}
            if fill.qty_raw == position.qty_raw:
                closed = dict(state, reason=final.reason, trade_pnl_lamports=position.realized_lamports + fill.realized_lamports,
                              cooldown_until=S.cooldown_until(final.reason, now, self.cfg))
                self.store.add_fill(fill, state={'event': 'closed', 'open_fill_id': open_id, 'state': closed})
                self.marks.pop(mint, None)
                self._count('closed')
            else:
                rung = dict(A.apply_updates(state, final.after_fill), last_reason=final.reason)
                self.store.add_fill(fill, state={'event': 'rung', 'open_fill_id': open_id, 'state': rung})
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
                'retry_queue': len(self.retry), 'execution_status': 'EXECUTION_UNVERIFIED', 'live_readiness': False}

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

    def run(self, *, candidate_interval=5.0, position_interval=10.0, install_signals=True):
        """Both loops until SIGTERM/SIGINT (or ``stop``). Returns True on a clean stop, False if a loop died."""
        if install_signals:
            signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
            signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        threads = [threading.Thread(target=self._loop, args=('candidates', self.candidate_pass, candidate_interval), name='candidates'),
                   threading.Thread(target=self._loop, args=('positions', self.position_pass, min(10.0, position_interval)),
                                    name='positions')]
        for t in threads:
            t.start()
        for t in threads:
            while t.is_alive():
                t.join(0.5)
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
