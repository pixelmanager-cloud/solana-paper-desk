"""Lean paper trader runner: one process, two concurrent loops, per-candidate failure isolation.

Paper only: no signing, no broadcasting, no real funds. Fills are quote-based and EXECUTION_UNVERIFIED.

The runner is wired against the L01-L04 interfaces by duck typing so it can be built and tested in parallel:

* store      ``record(kind, payload, *, code_version, strategy_version)``, ``positions()``, ``cash()``,
             ``check_invariants()`` (raises AccountingHalt). Fills are recorded with kind ``fill``.
* providers  ``helius.get_multiple_accounts(pubkeys)``, ``jupiter.quote(in, out, amount, taker)``, ``kraken.sol_usd()``;
             each returns ``(parsed, raw_bytes, meta)`` or raises ProviderError(code, transient).
* candidates ``iter_new_candidates(discovery_db, cursor)`` -> [Candidate] (each exposes a monotonic ``cursor``,
             falling back to ``seq``/``id``); ``screen(candidate, providers, cfg)`` -> Screen(passed, reasons, features).
* strategy   ``entry_decision(features, quote, portfolio, cfg)`` -> Decision(enter, size_sol, reason);
             ``exit_decision(position, mark, quote, now, cfg)`` -> Decision(exit, fraction, reason).
* paper      ``buy(quote, size_sol, cfg)`` -> Fill, ``sell(position, quote, fraction, cfg)`` -> Fill.

Only an accounting-invariant violation, a store write failure, or the kill-switch file stops entries; every other
failure marks that candidate / position check as failed, records it, and the loop moves on.
"""
import dataclasses
import json
import os
import signal
import threading
import time
import traceback

try:                                                   # real types once L01/L02 land
    from lean.store import AccountingHalt
except ImportError:
    class AccountingHalt(Exception):
        pass
try:
    from lean.providers import ProviderError
except ImportError:
    class ProviderError(Exception):
        def __init__(self, code, transient=False):
            super().__init__(code)
            self.code, self.transient = code, transient

SOL_MINT = 'So11111111111111111111111111111111111111112'
LAMPORTS = 10 ** 9


def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _payload(obj):
    if isinstance(obj, dict):
        return dict(obj)
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if hasattr(obj, 'as_dict'):
        return obj.as_dict()
    return dict(vars(obj))


def _read_u64(raw_account, offset=64):
    import base64
    data = base64.b64decode(raw_account['data'][0]) if isinstance(raw_account['data'], list) else raw_account['data']
    return int.from_bytes(data[offset:offset + 8], 'little')


def reserve_mark(position, accounts):
    """Default mark: constant-product spot value of the position from its two pool vault balances (SOL per raw token)."""
    base, quote = accounts
    if base is None or quote is None:
        raise ProviderError('MARK_ACCOUNT_MISSING', False)
    base_raw, quote_raw = _read_u64(base), _read_u64(quote)
    if base_raw <= 0 or quote_raw <= 0:
        raise ProviderError('MARK_EMPTY_POOL', False)
    qty_raw = int(_get(position, 'qty_raw'))
    return qty_raw * quote_raw / base_raw / LAMPORTS


class Runner:
    def __init__(self, *, store, helius, jupiter, kraken, candidates, strategy, paper, cfg,
                 discovery_db, health_path, code_version, strategy_version, kill_switch=None,
                 taker='LEAN_PAPER', clock=time.time, mark_fn=None, strategy_cfg=None, path_recorder=None):
        self.store, self.helius, self.jupiter, self.kraken = store, helius, jupiter, kraken
        self.candidates, self.strategy, self.paper, self.cfg = candidates, strategy, paper, cfg
        self.strategy_cfg = strategy_cfg if strategy_cfg is not None else cfg
        self.discovery_db, self.health_path = discovery_db, health_path
        self.code_version, self.strategy_version = code_version, strategy_version
        self.kill_switch, self.taker, self.clock = kill_switch, taker, clock
        self.mark_fn = mark_fn or self._default_marks
        self.path_recorder = path_recorder               # lean.paths.PathRecorder or None (L07)
        self.cursor = None
        self.stop = threading.Event()
        self.lock = threading.RLock()                    # serialises store use across the two loops
        self.halted = None
        self.counts = {'candidates': 0, 'screened_passed': 0, 'entered': 0, 'exited': 0, 'errors': 0}
        self.errors_by_code = {}
        self.last = {'candidate_loop': None, 'position_loop': None}

    # -- plumbing ----------------------------------------------------------
    def _track(self, candidate, screen, entered, reason=None):
        """Hand a screened candidate to the price-path recorder (L07). Never raises: a recorder bug must not touch an entry."""
        if self.path_recorder is None:
            return
        try:
            self.path_recorder.start(candidate, screen, entered=entered, reason=reason)
        except Exception:                                # noqa: BLE001
            pass

    def _record(self, kind, payload):
        with self.lock:
            return self.store.record(kind, payload, code_version=self.code_version, strategy_version=self.strategy_version)

    def _error(self, code, **detail):
        self.counts['errors'] += 1
        self.errors_by_code[code] = self.errors_by_code.get(code, 0) + 1
        try:
            self._record('error', {'code': code, 'at': self.clock(), **detail})
        except AccountingHalt:
            raise
        except Exception:
            self._halt('STORE_WRITE_FAILED')

    def _halt(self, reason):
        if self.halted is None:
            self.halted = reason
        self.write_health()

    def _check(self):
        try:
            with self.lock:
                self.store.check_invariants()
        except AccountingHalt as error:
            self._halt('ACCOUNTING_INVARIANT: %s' % error)
            return False
        return True

    def entries_allowed(self):
        if self.halted is not None:
            return False
        if self.kill_switch and os.path.exists(self.kill_switch):
            return False
        return True

    def providers(self):
        return {'helius': self.helius, 'jupiter': self.jupiter, 'kraken': self.kraken}

    # -- candidate loop ----------------------------------------------------
    def candidate_pass(self):
        """Screen new candidates and open paper positions. One candidate failing never affects another."""
        self.last['candidate_loop'] = self.clock()
        if not self.entries_allowed():
            return 0
        try:
            found = list(self.candidates.iter_new_candidates(self.discovery_db, self.cursor))
        except Exception as error:
            self._error('DISCOVERY_READ_FAILED', error=type(error).__name__)
            return 0
        done = 0
        for candidate in found:
            if self.stop.is_set() or not self.entries_allowed():
                break
            cursor = _get(candidate, 'cursor', _get(candidate, 'seq', _get(candidate, 'id')))
            try:
                self._handle_candidate(candidate)
            except AccountingHalt as error:
                self._halt('ACCOUNTING_INVARIANT: %s' % error)
            except ProviderError as error:
                self._error('PROVIDER_' + str(error.code), transient=bool(getattr(error, 'transient', False)), mint=_get(candidate, 'mint'))
            except Exception as error:               # isolation: a bad candidate is recorded, never fatal
                self._error('CANDIDATE_FAILED', error=type(error).__name__, mint=_get(candidate, 'mint'),
                            trace=traceback.format_exc(limit=3)[-400:])
            if cursor is not None and (self.cursor is None or cursor > self.cursor):
                self.cursor = cursor
            done += 1
        return done

    def _handle_candidate(self, candidate):
        mint = _get(candidate, 'mint')
        self.counts['candidates'] += 1
        screen = self.candidates.screen(candidate, self.providers(), self.strategy_cfg)
        self._record('screen', {'mint': mint, 'passed': bool(_get(screen, 'passed')), 'reasons': list(_get(screen, 'reasons', [])),
                                'features': _get(screen, 'features', {})})
        if not _get(screen, 'passed'):
            self._track(candidate, screen, False)
            return
        self.counts['screened_passed'] += 1
        if any(_get(p, 'mint') == mint for p in self.store.positions()):
            return
        features = _get(screen, 'features', {})
        probe_sol = self.cfg.get('entry_probe_sol', 0.02)
        try:
            quote, raw, meta = self.jupiter.quote(SOL_MINT, mint, int(probe_sol * LAMPORTS), self.taker)
        except Exception:
            self._track(candidate, screen, False, 'QUOTE_FAILED')
            raise
        self._record('observation', {'mint': mint, 'kind': 'entry_quote', 'raw_bytes_len': len(raw or b''),
                                     'raw_base64': _b64(raw)})
        with self.lock:
            portfolio = {'positions': self.store.positions(), 'cash': self.store.cash()}
        decision = self.strategy.entry_decision(features, quote, portfolio, self.strategy_cfg)
        self._record('decision', {'mint': mint, 'side': 'entry', 'enter': bool(_get(decision, 'enter')),
                                  'reason': _get(decision, 'reason'), 'size_sol': _get(decision, 'size_sol')})
        if not _get(decision, 'enter'):
            self._track(candidate, screen, False, str(_get(decision, 'reason')))
            return
        fill = self.paper.buy(quote, _get(decision, 'size_sol'), self.strategy_cfg)
        self._record('fill', {**_payload(fill), 'execution_status': 'EXECUTION_UNVERIFIED'})
        self.counts['entered'] += 1
        self._track(candidate, screen, True)
        self._check()

    # -- position loop -----------------------------------------------------
    def _default_marks(self, positions):
        pubkeys, index = [], {}
        for p in positions:
            vaults = (_get(p, 'vault_base'), _get(p, 'vault_quote'))
            if None in vaults:
                continue
            index[_get(p, 'mint')] = (len(pubkeys), len(pubkeys) + 1)
            pubkeys.extend(vaults)
        if not pubkeys:
            return {}
        accounts, raw, meta = self.helius.get_multiple_accounts(pubkeys)       # ONE batched read for all positions
        self._record('observation', {'kind': 'marks', 'n': len(pubkeys), 'raw_base64': _b64(raw)})
        marks = {}
        for p in positions:
            if _get(p, 'mint') in index:
                i, j = index[_get(p, 'mint')]
                try:
                    marks[_get(p, 'mint')] = reserve_mark(p, (accounts[i], accounts[j]))
                except ProviderError as error:
                    self._error('PROVIDER_' + str(error.code), mint=_get(p, 'mint'))
        return marks

    def position_pass(self):
        """Mark every open position with one batched read; sell (paper) when an exit triggers."""
        self.last['position_loop'] = self.clock()
        with self.lock:
            positions = list(self.store.positions())
        if not positions:
            return 0
        try:
            marks = self.mark_fn(positions)
        except AccountingHalt as error:
            self._halt('ACCOUNTING_INVARIANT: %s' % error)
            return 0
        except Exception as error:
            code = 'PROVIDER_' + str(error.code) if isinstance(error, ProviderError) else 'MARKS_FAILED'
            self._error(code, error=type(error).__name__)
            return 0
        exits = 0
        for position in positions:
            if self.stop.is_set():
                break
            mint = _get(position, 'mint')
            try:
                if mint not in marks:
                    continue
                decision = self.strategy.exit_decision(position, marks[mint], None, self.clock(), self.strategy_cfg)
                if not _get(decision, 'exit'):
                    continue
                fraction = _get(decision, 'fraction', 1.0)
                quote, raw, meta = self.jupiter.quote(mint, SOL_MINT, int(_get(position, 'qty_raw') * fraction), self.taker)
                self._record('observation', {'mint': mint, 'kind': 'exit_quote', 'raw_base64': _b64(raw)})
                self._record('decision', {'mint': mint, 'side': 'exit', 'reason': _get(decision, 'reason'), 'fraction': fraction})
                fill = self.paper.sell(position, quote, fraction, self.strategy_cfg)
                self._record('fill', {**_payload(fill), 'reason': _get(decision, 'reason'), 'execution_status': 'EXECUTION_UNVERIFIED'})
                self.counts['exited'] += 1
                exits += 1
                self._check()
            except AccountingHalt as error:
                self._halt('ACCOUNTING_INVARIANT: %s' % error)
            except ProviderError as error:                       # position stays open; retried next tick
                self._error('PROVIDER_' + str(error.code), transient=bool(getattr(error, 'transient', False)), mint=mint)
            except Exception as error:
                self._error('POSITION_CHECK_FAILED', error=type(error).__name__, mint=mint)
        return exits

    # -- health / lifecycle ------------------------------------------------
    def health(self):
        with self.lock:
            try:
                positions, cash = list(self.store.positions()), self.store.cash()
            except Exception:
                positions, cash = [], None
        return {'kind': 'lean_health_v1', 'at': self.clock(), 'code_version': self.code_version,
                'strategy_version': self.strategy_version, 'halted': self.halted,
                'kill_switch': bool(self.kill_switch and os.path.exists(self.kill_switch)),
                'last_loop': dict(self.last), 'counts': dict(self.counts), 'errors_by_code': dict(self.errors_by_code),
                'open_positions': [{'mint': _get(p, 'mint')} for p in positions], 'cash': cash, 'cursor': self.cursor,
                'execution_status': 'EXECUTION_UNVERIFIED', 'live_readiness': False}

    def write_health(self):
        tmp = self.health_path + '.tmp'
        with open(tmp, 'w') as stream:
            json.dump(self.health(), stream, sort_keys=True, default=str)
        os.replace(tmp, self.health_path)

    def run(self, *, candidate_interval=5.0, position_interval=10.0, install_signals=True):
        if install_signals:
            signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
            signal.signal(signal.SIGINT, lambda *_: self.stop.set())

        def loop(work, interval):
            while not self.stop.is_set():
                try:
                    work()
                except Exception as error:               # a loop body bug must not kill the other loop
                    self._error('LOOP_FAILED', error=type(error).__name__)
                try:
                    self.write_health()
                except OSError:
                    pass
                self.stop.wait(interval)
        threads = [threading.Thread(target=loop, args=(self.candidate_pass, candidate_interval), name='candidates'),
                   threading.Thread(target=loop, args=(self.position_pass, position_interval), name='positions')]
        if self.path_recorder is not None:
            threads.append(threading.Thread(target=self.path_recorder.run, args=(self.stop,), name='paths'))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.write_health()


def _b64(raw):
    import base64
    return base64.b64encode(raw).decode() if isinstance(raw, (bytes, bytearray)) else None
