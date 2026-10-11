"""Lean paper trader runner: one process, two concurrent loops, per-candidate failure isolation.

PAPER ONLY: no signing, no broadcasting, no real funds. Fills are quote-based and EXECUTION_UNVERIFIED.

candidate loop   discovery (lean.candidates.scan_new, cursor persisted) -> screen -> entry decision (pre-quote gate)
                 -> buy quote AT THE DECIDED SIZE + sell-leg quote for exactly the tokens the fill will hold
                 -> entry decision on the round trip -> paper BUY (fill + position state in one transaction).
position loop    every <=10s: ONE batched getMultipleAccounts of all held vaults (exit lane) -> net marks -> exit decision
                 -> (only when an exit triggers) sell quote for the exact raw qty -> exit decision on the quote
                 -> paper SELL (fill + rung/closed state in one transaction). Peak/touched changes are persisted too.

Failure policy (kept simple, no latches): a provider error, malformed response or bad evidence fails THAT candidate or
position check (an ``errors`` row, raw bytes kept as an observation when there are any) and the loop moves on; a
transient screening failure is retried on the next passes (in memory, up to ``max_retries``). Only an accounting
invariant violation (AccountingHalt) or a store failure (StoreError / sqlite3.Error) HALTS: the halt is written to the
store, survives restarts, and stops every new fill until an operator clears it (``python -m lean --clear-halt``). The
kill-switch file stops new entries only; held positions keep being managed.

All unit/type conversions live in lean.adapters.
"""
import json
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

SOL_MINT = A.SOL_MINT
MAX_KEYS_PER_CALL = 100


class _Halt(Exception):
    """Internal: stop this pass, the runner is halted."""


class Runner:
    def __init__(self, *, store, providers, exit_providers, strategy_cfg, discovery_db, state_dir, code_version,
                 screen_overrides=None, pool_fee_bps=25, taker=PAPER_TAKER, kill_switch=None, clock=time.time,
                 scan_limit=500, max_retries=3, sol_usd_ttl_s=30, route_check=None):
        self.store, self.providers, self.exit_providers = store, providers, exit_providers
        self.cfg = strategy_cfg
        self.pcfg = A.paper_config(strategy_cfg)
        self.screen_cfg = A.screen_config(strategy_cfg, screen_overrides)
        self.discovery_db, self.state_dir = discovery_db, os.fspath(state_dir)
        self.code_version, self.strategy_version = code_version, strategy_cfg.strategy_version
        self.pool_fee_bps, self.taker, self.clock = int(pool_fee_bps), taker, clock
        self.kill_switch = kill_switch or os.path.join(self.state_dir, 'KILL')
        self.scan_limit, self.max_retries, self.sol_usd_ttl_s = scan_limit, max_retries, sol_usd_ttl_s
        self.health_path = os.path.join(self.state_dir, 'health.json')
        self.cursor_path = os.path.join(self.state_dir, 'cursor.json')
        self.stop = threading.Event()
        self._trade_lock = threading.RLock()       # final re-check + fill, and the day-start baseline
        self._counts_lock = threading.Lock()       # shared counters
        self._health_lock = threading.Lock()       # one health writer at a time
        self.counts = {'candidates': 0, 'screened': 0, 'passed': 0, 'entered': 0, 'exits': 0, 'closed': 0,
                       'errors': 0, 'retries': 0, 'duplicates': 0}
        self.errors_by_code = {}
        self.last = {'candidate_loop': None, 'position_loop': None}
        self.marks = {}                            # mint -> (Decimal net mark SOL, mark_at)
        self.retry = []                            # [(candidate, attempt)]
        self._sol_usd = None                       # (Decimal, fetched_at)
        self.halted = None
        self.cursor = 0
        # --- L16 hook ---
        from lean.route_check import RouteChecker
        self.route_check = RouteChecker(store, route_check, clock=clock)    # off unless route_check.enabled
        # --- end L16 ---
        self.startup()

    # -- lifecycle ------------------------------------------------------------------------------------------------
    def startup(self):
        """Persisted halt, store invariants and the cursor: checked before any loop runs."""
        self.halted = self.store.halt_reason()
        try:
            self.store.check_invariants()
            self.store.check_position_states()
        except AccountingHalt as error:
            self._halt('ACCOUNTING_INVARIANT: %s' % error)
        except (StoreError, sqlite3.Error) as error:
            self._halt('STORE_FAILURE: %s' % type(error).__name__)
        self.cursor = C.read_cursor(self.cursor_path)
        if self.halted is None:
            self.store.record('start', {'cursor': self.cursor, 'config_hash': self.cfg.config_hash,
                                        'paper': {'fee_lamports': self.pcfg.fee_lamports, 'slippage_bps': self.pcfg.slippage_bps},
                                        'pool_fee_bps': self.pool_fee_bps, 'screen': {k: str(v) for k, v in self.screen_cfg.items()}},
                              code_version=self.code_version, strategy_version=self.strategy_version, ts=self.clock())

    def _now(self):
        return int(self.clock())

    def _halt(self, reason):
        if self.halted is None:
            self.halted = reason
            try:
                self.store.record('halt', {'reason': reason, 'at': self.clock()}, code_version=self.code_version,
                                  strategy_version=self.strategy_version, ts=self.clock())
            except Exception:              # the store itself may be what failed; the in-memory halt still holds
                pass
        self.write_health()

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

    def day_start_equity(self, now):
        """The persisted UTC-day baseline, rolled over at midnight only with fresh marks (strategy.roll_day_start)."""
        with self._trade_lock:
            equity, fresh = A.equity_and_freshness(self.store, now, dict(self.marks), self.cfg.price_ttl_seconds)
            last = self.store.latest_event('day_start')
            previous = (int(last[1]['day']), Decimal(last[1]['equity'])) if last else None
            current = S.roll_day_start(previous, now, equity, marks_fresh=fresh)
            if current != previous:
                self.store.record('day_start', {'day': current[0], 'equity': str(current[1]), 'marks_fresh': fresh},
                                  code_version=self.code_version, strategy_version=self.strategy_version, ts=self.clock())
            return current[1]

    def portfolio(self, now):
        # the position thread updates self.marks: work on a snapshot
        return A.portfolio(self.store, self.cfg, now, marks=dict(self.marks), day_start_equity=self.day_start_equity(now))

    # -- candidate loop -------------------------------------------------------------------------------------------
    def candidate_pass(self):
        """Retries first, then new discovery frames. One candidate failing never affects another."""
        self.last['candidate_loop'] = self.clock()
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
        # --- L16 hook ---
        try:
            buy_q, buy_ref, buy_at = self._quote(self.providers, SOL_MINT, mint, size_lamports, 'quote:buy', mint, cid)
        except ProviderError as error:
            self.route_check.on_quote_error('entry', mint, error, candidate_id=cid)
            raise
        # --- end L16 ---
        sell_q, _, sell_at = self._quote(self.providers, mint, SOL_MINT, A.sell_leg_qty(buy_q.out_amount, self.pcfg),
                                         'quote:sell_leg', mint, cid)
        with self._trade_lock:
            now = self._now()
            decision = S.entry_decision(features, A.roundtrip(buy_q, buy_at, sell_q, sell_at), self.portfolio(now), self.cfg)
            self.store.add_decision('entry', decision.action, mint=mint, candidate_id=cid, reasons=decision.reasons,
                                    features={**_jsonable(decision.detail), 'size_sol': str(decision.size_sol)}, ts=self.clock())
            if decision.action != 'BUY':
                return
            fill = paper.buy(A.paper_quote(buy_q, mint=mint, side='buy', decimals=features['decimals'], ts=self.clock(),
                                           ref=buy_ref), decision.size_sol, self.pcfg)
            self.store.add_fill(fill, candidate_id=cid, state={'event': 'open', 'state': A.open_state(features, fill, self.cfg)})
            self.route_check.on_fill('entry', mint, buy_ref, candidate_id=cid)      # --- L16 hook ---
            self._count('entered')
            self._check()

    # -- position loop --------------------------------------------------------------------------------------------
    def position_pass(self):
        """Mark every open position with batched reads; sell (paper) when an exit triggers. Runs under the kill switch."""
        self.last['position_loop'] = self.clock()
        if self.halted is not None:
            return 0
        try:
            positions = self.store.positions()
            states = self.store.position_states()
            if not positions:
                return 0
            self._read_marks(positions, states)
            now = self._now()
            liquidate = S.should_liquidate(self.portfolio(now), self.cfg)
            exits = 0
            for mint, position in positions.items():
                if self.stop.is_set():
                    break
                exits += self._isolated_exit(mint, position, states.get(mint), liquidate)
            return exits
        except _Halt:
            return 0
        except AccountingHalt as error:
            self._halt('ACCOUNTING_INVARIANT: %s' % error)
        except (StoreError, sqlite3.Error) as error:
            self._halt('STORE_FAILURE: %s' % type(error).__name__)
        return 0

    def _read_marks(self, positions, states):
        """ONE getMultipleAccounts per <=50 positions (base + quote vault each) on the exit lane; net marks with read time."""
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
                                        mark_at)
                except A.AdapterError as error:
                    self._error('MARK_INVALID', transient=False, mint=mint, scope='marks', message=str(error))
        for mint in list(self.marks):
            if mint not in positions:
                del self.marks[mint]

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

    def _manage(self, mint, position, row, liquidate):
        open_id, state = row['open_fill_id'], row['state']
        mark, mark_at = self.marks.get(mint, (None, None))
        now = self._now()
        held = A.strategy_position(position, state)
        decision = S.exit_decision(held, mark, None, now, self.cfg, mark_at=mark_at, liquidate=liquidate)
        if decision.position_updates:
            state = A.apply_updates(state, decision.position_updates)
            held = A.strategy_position(position, state)
            if decision.action != 'SELL':
                self.store.add_position_state(mint, open_id, state, ts=self.clock())
        if decision.action != 'SELL' or not decision.quote_required:
            return 0
        qty = A.sell_qty_raw(decision.qty, position.qty_raw)
        # --- L16 hook ---
        try:
            quote, ref, quote_at = self._quote(self.exit_providers, mint, SOL_MINT, qty, 'quote:exit', mint)
        except ProviderError as error:
            if qty == position.qty_raw:                                             # full exits only
                self.route_check.on_quote_error('exit', mint, error)
            raise
        # --- end L16 ---
        with self._trade_lock:
            now = self._now()
            final = S.exit_decision(held, mark, A.strategy_quote(quote, 'sell', quote_at), now, self.cfg, mark_at=mark_at,
                                    liquidate=liquidate)
            self.store.add_decision('exit', final.action, mint=mint, reasons=final.reasons, ts=self.clock(),
                                    features={**_jsonable(final.detail), 'qty_raw': qty, 'fraction_of_initial': str(final.fraction),
                                              'quote_ref': ref, 'mark_sol': None if mark is None else str(mark)})
            if final.action != 'SELL' or A.sell_qty_raw(final.qty, position.qty_raw) != qty:
                if decision.position_updates:
                    self.store.add_position_state(mint, open_id, state, ts=self.clock())
                return 0
            fill = paper.sell(position, A.paper_quote(quote, mint=mint, side='sell', decimals=position.decimals, ts=self.clock(),
                                                      ref=ref), cfg=self.pcfg, qty_raw=qty)
            if fill.qty_raw == position.qty_raw:
                closed = dict(state, reason=final.reason, trade_pnl_lamports=position.realized_lamports + fill.realized_lamports,
                              cooldown_until=S.cooldown_until(final.reason, now, self.cfg))
                self.store.add_fill(fill, state={'event': 'closed', 'open_fill_id': open_id, 'state': closed})
                self.route_check.on_fill('exit', mint, ref)                          # --- L16 hook ---
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
        return {'kind': 'lean_health_v1', 'at': self.clock(), 'code_version': self.code_version,
                'strategy_version': self.strategy_version, 'halted': self.halted,
                'kill_switch': os.path.exists(self.kill_switch), 'last_loop': dict(self.last), 'counts': counts,
                'errors_by_code': errors, 'open_positions': sorted(positions), 'cash_lamports': cash, 'cursor': self.cursor,
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

    def run(self, *, candidate_interval=5.0, position_interval=10.0, install_signals=True):
        if install_signals:
            signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
            signal.signal(signal.SIGINT, lambda *_: self.stop.set())

        def loop(work, interval):
            while not self.stop.is_set():
                try:
                    work()
                except Exception as error:               # a loop-body bug must not kill the other loop
                    try:
                        self._error('LOOP_FAILED', transient=False, scope='loop', message=type(error).__name__)
                    except _Halt:
                        pass
                try:
                    self.write_health()
                except OSError:
                    pass
                self.stop.wait(interval)
        threads = [threading.Thread(target=loop, args=(self.candidate_pass, candidate_interval), name='candidates'),
                   threading.Thread(target=loop, args=(self.position_pass, min(10.0, position_interval)), name='positions')]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.write_health()


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value
