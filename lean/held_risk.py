"""Held-position rug detection for the lean paper trader (L11, reworked in L11F on top of the D1 quote marks and the D2 write-off).
PAPER ONLY: nothing here signs, builds or sends a transaction.

This module only DETECTS and VALUES. It never sells and never closes a position: a detected rug becomes a forced ``DANGER`` exit
inside ``Runner._manage`` (so the quote, the fee and slippage, the D2 ``exit_failing_since`` clock and the D2 write-off are the
ordinary ones, and exits never go through a second code path). Four triggers per held mint:

  LIQUIDITY_DROP  the pool's quote vault fell by more than ``rug_liq_drop_frac`` (default 0.7) since the screen that opened the
                  position (read from the marks the position loop already makes: no extra request);
  POOL_GONE       the vault accounts were missing from the marks read, or the pool account was closed, or its owner program
                  changed (migrated), in ``pool_gone_confirmations`` (default 2) CONSECUTIVE reads: one empty read is a node
                  hiccup, not a rug;
  NO_ROUTE        no Jupiter sell route for ``unsellable_after_s`` (default 120 s), probed every ``route_probe_s`` (60 s). A probe
                  is skipped when a D1 quote mark younger than that already proves a route. A provider outage (timeout, 5xx,
                  429) is never "no route";
  FREEZE          the mint's freeze authority is set: recorded as a flag; it forces an exit only with ``freeze_forces_exit``.

Order inside one position pass (so detection never queues ahead of a stop exit): marks read -> ``after_marks`` (no I/O: baselines,
trigger detection from the marks just read, valuation of rug positions whose exit is failing) -> D1 quote marks -> the exit loop (a
position with a trigger is wanted out as ``DANGER``) -> ``after_exits`` (the only I/O of this module: route probes and the batched
account check, whose findings take effect on the next pass).

Valuation: a position with a trigger whose exit is failing (D2's persisted ``exit_failing_since``) is worth 0 in the equity unless
an executable sell quote younger than ``unsellable_value_ttl_s`` exists; it is never valued at its last good price or at cost. D2's
write-off then books it after ``unexitable_after_s`` with the reason ``RUG_WRITEOFF`` (``UNEXITABLE`` when no trigger fired).

State is persisted as generic ``events`` rows of kind ``held_risk`` (no schema change: a lean store is never migrated) and the
trigger is also stamped on the position state (``rug_trigger``), so a trigger, the POOL_GONE confirmation count and the no-route
clock all survive a restart. Each lifecycle is keyed by ``(mint, open_fill_id)``: a later re-entry of the same mint starts clean.

A failure in this module is isolated like any other position check (an ``errors`` row, the loop moves on); it never touches the
entry path. Off unless ``held_risk.enabled`` is true.
"""
import json
import sqlite3
from decimal import Decimal, InvalidOperation

from lean import adapters as A, paper, providers
from lean.providers import ProviderError
from lean.store import StoreError

KIND = 'held_risk'
SOL_MINT = A.SOL_MINT
TRIGGERS = ('LIQUIDITY_DROP', 'NO_ROUTE', 'POOL_GONE', 'FREEZE')
DEFAULTS = {
    'enabled': False,
    'rug_liq_drop_frac': '0.7',
    'unsellable_after_s': 120,
    'unsellable_value_ttl_s': 30,
    'route_probe_s': 60,
    'freeze_check_every': 6,
    'freeze_forces_exit': False,
    'pool_gone_confirmations': 2,
    'max_probes_per_pass': 1,          # bounds how long one pass can spend in blocking probe HTTP calls (exit thread)
}
REPLAY_PAGE = 1000


class HeldRiskConfigError(ValueError):
    pass


class Config:
    def __init__(self, **values):
        unknown = set(values) - set(DEFAULTS)
        if unknown:
            raise HeldRiskConfigError('unknown held_risk keys: %s' % sorted(unknown))
        merged = {**DEFAULTS, **values}
        try:
            frac = Decimal(str(merged['rug_liq_drop_frac']))
        except InvalidOperation:
            raise HeldRiskConfigError('rug_liq_drop_frac must be a number') from None
        if not frac.is_finite() or not Decimal(0) < frac < Decimal(1):
            raise HeldRiskConfigError('rug_liq_drop_frac must be in (0, 1)')
        for name in ('unsellable_after_s', 'unsellable_value_ttl_s', 'route_probe_s', 'freeze_check_every'):
            if type(merged[name]) is not int or not 1 <= merged[name] <= 10 ** 7:
                raise HeldRiskConfigError('%s must be a positive integer' % name)
        if type(merged['max_probes_per_pass']) is not int or not 1 <= merged['max_probes_per_pass'] <= 50:
            raise HeldRiskConfigError('max_probes_per_pass must be an integer from 1 to 50')
        if type(merged['pool_gone_confirmations']) is not int or not 1 <= merged['pool_gone_confirmations'] <= 20:
            raise HeldRiskConfigError('pool_gone_confirmations must be an integer from 1 to 20')
        for name in ('enabled', 'freeze_forces_exit'):
            if type(merged[name]) is not bool:
                raise HeldRiskConfigError('%s must be true or false' % name)
        self.enabled, self.rug_liq_drop_frac = merged['enabled'], frac
        self.unsellable_after_s, self.unsellable_value_ttl_s = merged['unsellable_after_s'], merged['unsellable_value_ttl_s']
        self.route_probe_s, self.freeze_check_every = merged['route_probe_s'], merged['freeze_check_every']
        self.freeze_forces_exit, self.pool_gone_confirmations = merged['freeze_forces_exit'], merged['pool_gone_confirmations']
        self.max_probes_per_pass = merged['max_probes_per_pass']

    @classmethod
    def from_dict(cls, value):
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise HeldRiskConfigError('held_risk must be an object')
        return cls(**value)

    def as_dict(self):
        return {**{k: getattr(self, k) for k in DEFAULTS if k != 'rug_liq_drop_frac'}, 'rug_liq_drop_frac': str(self.rug_liq_drop_frac)}


class Life:
    """The risk state of one position lifecycle."""

    def __init__(self):
        self.baseline_quote = None          # quote-vault lamports at the screen that opened the position
        self.pool_owner = None
        self.trigger = None                 # (reason, detail) once an exit is wanted; sticky until the position closes
        self.vault_misses = 0               # consecutive marks reads with a missing vault account
        self.last_miss_at = None
        self.account_misses = 0             # consecutive account checks with the pool closed / migrated
        self.no_route_since = None
        self.last_good = None               # (net lamports of an executable full-size sell quote, observed at)
        self.freeze_flagged = False
        self.last_probe = None


class HeldRisk:
    def __init__(self, store, cfg, *, code_version, strategy_version, clock):
        self.store, self.cfg, self.clock = store, cfg, clock
        self.code_version, self.strategy_version = code_version, strategy_version
        self.lives = {}                     # (mint, open_fill_id) -> Life
        self.passes = 0
        self.counts = {'triggers': 0, 'freeze_flags': 0}
        self._fresh = {}                    # mint -> quote vault lamports of THIS pass's marks read
        self._missing = {}                  # mint -> mark_at of THIS pass's read when a vault account was missing
        self._reserve_seen = {}             # mint -> latest quote vault lamports (baseline fallback)
        # LINT1: probes and account checks go on the SHARED non-blocking low lane (set by build_runner), never on the exit lane:
        # 15 serial probes on the 1 req/s Jupiter exit bucket stalled the position thread ~14 s every minute. A LANE_SHED sends
        # nothing and is skipped silently (retried next pass). None = the exit lane (unit tests that build a bare Runner).
        self.low = None
        self.counts['shed'] = 0
        self._replay()

    def _lane(self, runner):
        return self.low if self.low is not None else runner.exit_providers

    # -- persistence ----------------------------------------------------------------------------------------------
    def _write(self, payload):
        self.store.record(KIND, payload, code_version=self.code_version, strategy_version=self.strategy_version, ts=self.clock())

    def _events(self):
        """Every held_risk event in id order, in pages (no row cap: a long run must not lose its oldest rows)."""
        last = 0
        while True:
            with self.store._lock:
                rows = self.store.db.execute('SELECT id,payload FROM events WHERE kind=? AND id>? ORDER BY id LIMIT ?',
                                             (KIND, last, REPLAY_PAGE)).fetchall()
            if not rows:
                return
            for row_id, payload in rows:
                last = row_id
                yield payload

    def _replay(self):
        for payload in self._events():
            try:
                p = json.loads(payload)
                life = self.life(p['mint'], int(p['open_fill_id']))
            except (ValueError, KeyError, TypeError):
                continue
            ev = p.get('ev')
            if ev == 'baseline':
                life.baseline_quote = p.get('quote_reserve_lamports')
                if p.get('pool_owner') is not None:
                    life.pool_owner = p['pool_owner']
            elif ev == 'pool_owner':
                life.pool_owner = p.get('pool_owner')
            elif ev == 'trigger':
                life.trigger = (p.get('reason'), p.get('detail') or {})
                self.counts['triggers'] += 1
            elif ev == 'vault_miss':
                life.vault_misses, life.last_miss_at = int(p.get('n', 0)), p.get('at')
            elif ev == 'vault_seen':
                life.vault_misses = 0
            elif ev == 'account_miss':
                life.account_misses = int(p.get('n', 0))
            elif ev == 'account_seen':
                life.account_misses = 0
            elif ev == 'no_route':
                life.no_route_since = p.get('since')
            elif ev == 'route_ok':
                life.no_route_since = None
            elif ev == 'freeze_flag':
                life.freeze_flagged = True
                self.counts['freeze_flags'] += 1

    def life(self, mint, open_fill_id):
        return self.lives.setdefault((mint, int(open_fill_id)), Life())

    def trigger(self, mint, open_fill_id):
        """The trigger name that wants this lifecycle out as a DANGER exit, or None."""
        life = self.lives.get((mint, int(open_fill_id)))
        return life.trigger[0] if life is not None and life.trigger is not None else None

    # -- inputs from the marks read (runner hook inside ``_read_marks``) ---------------------------------------------------
    def observe(self, mint, base_raw, quote_lamports, mark_at):
        self._fresh[mint] = int(quote_lamports)
        self._reserve_seen[mint] = int(quote_lamports)
        self._missing.pop(mint, None)

    def observe_missing(self, mint, mark_at):
        self._missing[mint] = mark_at
        self._fresh.pop(mint, None)

    # -- after the marks, before any I/O of this module -------------------------------------------------------------------
    def after_marks(self, runner, positions, states):
        """Baselines, trigger detection from the marks just read, and the valuation of rug positions whose exit is failing.
        No request is made here."""
        now = runner._now()
        for mint in [m for m in positions if m in states]:
            row = states[mint]
            life = self.life(mint, row['open_fill_id'])
            if life.baseline_quote is None:
                self._baseline(runner, mint, row, life)
            if life.trigger is None:
                self._detect(mint, row, life)
            if life.trigger is not None and row['state'].get('exit_failing_since') is not None:
                self._value(runner, mint, life, now)
        self._fresh.clear()
        self._missing.clear()

    def _baseline(self, runner, mint, row, life):
        """The quote reserve at the screen tied to THIS position: the latest PASS screen decided no later than the opening fill."""
        quote, source = None, None
        try:
            fill = runner.store.rows('fills', id=row['open_fill_id'], limit=1)
            if fill:
                with runner.store._lock:
                    found = runner.store.db.execute(
                        "SELECT features FROM decisions WHERE mint=? AND kind='screen' AND action='PASS' AND ts<=? ORDER BY id DESC LIMIT 1",
                        (mint, fill[0]['ts'])).fetchone()
                if found is not None:
                    features = json.loads(found[0])
                    if features.get('quote_gross_raw') is not None:
                        quote, source = int(features['quote_gross_raw']), 'screen'
        except (ValueError, TypeError, KeyError):
            quote = None
        if quote is None and mint in self._reserve_seen:
            quote, source = self._reserve_seen[mint], 'first_mark'
        if quote is None:
            return                                                    # no evidence yet: try again next pass
        life.baseline_quote = quote
        self._write({'ev': 'baseline', 'mint': mint, 'open_fill_id': row['open_fill_id'], 'quote_reserve_lamports': quote,
                     'pool_owner': life.pool_owner, 'source': source})

    def _detect(self, mint, row, life):
        open_id = row['open_fill_id']
        if mint in self._missing:
            at = self._missing[mint]
            if life.last_miss_at != at:
                life.vault_misses += 1
                life.last_miss_at = at
                self._write({'ev': 'vault_miss', 'mint': mint, 'open_fill_id': open_id, 'n': life.vault_misses, 'at': at})
            if life.vault_misses >= self.cfg.pool_gone_confirmations:
                self._trigger(mint, row, life, 'POOL_GONE', {'why': 'vault account missing', 'reads': life.vault_misses})
            return
        if mint not in self._fresh:
            return                                                    # no read this pass (provider failure): nothing to conclude
        if life.vault_misses:
            life.vault_misses = 0
            self._write({'ev': 'vault_seen', 'mint': mint, 'open_fill_id': open_id})
        if life.baseline_quote is None or life.baseline_quote <= 0:
            return
        keep = Decimal(1) - self.cfg.rug_liq_drop_frac
        if Decimal(self._fresh[mint]) < Decimal(life.baseline_quote) * keep:
            self._trigger(mint, row, life, 'LIQUIDITY_DROP', {'baseline_lamports': life.baseline_quote, 'now_lamports': self._fresh[mint],
                                                              'drop_frac': str(self.cfg.rug_liq_drop_frac)})

    def _trigger(self, mint, row, life, reason, detail):
        if life.trigger is not None:
            return
        life.trigger = (reason, detail)
        self.counts['triggers'] += 1
        self._write({'ev': 'trigger', 'mint': mint, 'open_fill_id': row['open_fill_id'], 'reason': reason, 'detail': detail})

    def _value(self, runner, mint, life, now):
        """A rug position whose exit is failing is worth 0 unless an executable quote younger than the ttl exists."""
        value = 0
        if life.last_good is not None and 0 <= now - life.last_good[1] <= self.cfg.unsellable_value_ttl_s:
            value = life.last_good[0]
        runner.marks[mint] = (A.sol(int(value)), now, 'held_risk')

    # -- after the exit loop: the only requests of this module ----------------------------------------------------------------
    def after_exits(self, runner):
        """Route probes (skipped where a fresh D1 quote mark already proves a route) and the batched account check. A finding
        takes effect on the NEXT pass: nothing here ever delays an exit of this one."""
        self.passes += 1
        positions, states = runner.store.positions(), runner.store.position_states()
        held = {m: states[m] for m in positions if m in states}
        now = runner._now()
        if self.passes % self.cfg.freeze_check_every == 1 or self.cfg.freeze_check_every == 1:
            self._account_checks(runner, held)
        probes = 0
        for mint, row in held.items():
            if runner.stop.is_set() or probes >= self.cfg.max_probes_per_pass:
                return
            life = self.life(mint, row['open_fill_id'])
            if life.trigger is not None:
                continue
            if life.last_probe is not None and now - life.last_probe < self.cfg.route_probe_s:
                continue
            mark = runner.marks.get(mint)
            if mark is not None and len(mark) >= 3 and mark[2] == 'quote' and 0 <= now - mark[1] < self.cfg.route_probe_s:
                life.last_probe = now                                  # a D1 quote mark IS a full-size sell quote: reuse it
                self._route_ok(mint, row, life, int(mark[0] * 10 ** 9), mark[1])
                continue
            probes += 1
            self._probe(runner, mint, positions[mint], row, life, now)

    def _route_ok(self, mint, row, life, value_lamports, at):
        if value_lamports > 0:
            life.last_good = (value_lamports, at)
        if life.no_route_since is not None:
            life.no_route_since = None
            self._write({'ev': 'route_ok', 'mint': mint, 'open_fill_id': row['open_fill_id']})

    def _probe(self, runner, mint, position, row, life, now):
        """A full-size sell quote on the low lane (LINT1). A non-transient failure starts the no-route clock; success clears it.
        A probe is not an exit: its raw bytes are kept only when it FAILS (research evidence), never on success."""
        previous, life.last_probe = life.last_probe, now
        try:
            quote, _raw, _meta = self._lane(runner).jupiter.quote(mint, SOL_MINT, position.qty_raw, runner.taker)
        except ProviderError as error:
            if providers.is_shed(error):                              # LINT1: nothing was sent; probe again next pass
                life.last_probe = previous
                self.counts['shed'] += 1
                return
            runner._error(error.code, transient=error.transient, mint=mint, scope='route_probe', raw=error.raw)
            if error.transient:
                return                                                # an outage is not "no route"
            if life.no_route_since is None:
                life.no_route_since = now
                self._write({'ev': 'no_route', 'mint': mint, 'open_fill_id': row['open_fill_id'], 'since': now, 'code': error.code})
            if now - life.no_route_since >= self.cfg.unsellable_after_s:
                self._trigger(mint, row, life, 'NO_ROUTE', {'since': life.no_route_since, 'code': error.code})
            return
        self._route_ok(mint, row, life, net_lamports(quote.out_amount, runner.pcfg), now)

    def _account_checks(self, runner, held):
        """ONE batched read of [mint, pool] per held position on the low lane (LINT1): freeze authority, pool closed or migrated."""
        mints = list(held)
        for start in range(0, len(mints), 50):
            if runner.stop.is_set():
                return
            chunk = mints[start:start + 50]
            keys = [k for m in chunk for k in (m, held[m]['state']['pool'])]
            try:
                result, _raw, _meta = self._lane(runner).helius.get_multiple_accounts(keys)
            except ProviderError as error:
                if providers.is_shed(error):                          # LINT1: nothing was sent; the next check retries
                    self.counts['shed'] += 1
                    continue
                runner._error(error.code, transient=error.transient, scope='held_risk', message='%d positions' % len(chunk), raw=error.raw)
                continue
            for i, mint in enumerate(chunk):
                row = held[mint]
                self._read_accounts(mint, row, self.life(mint, row['open_fill_id']), result['value'][2 * i], result['value'][2 * i + 1])

    def _read_accounts(self, mint, row, life, mint_account, pool_account):
        open_id = row['open_fill_id']
        if life.trigger is None:
            owner = pool_account.get('owner') if isinstance(pool_account, dict) else None
            wrong = None
            if pool_account is None:
                wrong = 'pool account closed'
            elif isinstance(owner, str):
                if life.pool_owner is None:
                    life.pool_owner = owner
                    self._write({'ev': 'pool_owner', 'mint': mint, 'open_fill_id': open_id, 'pool_owner': owner})
                elif owner != life.pool_owner:
                    wrong = 'pool owner changed'
            if wrong is not None:
                life.account_misses += 1
                self._write({'ev': 'account_miss', 'mint': mint, 'open_fill_id': open_id, 'n': life.account_misses, 'why': wrong})
                if life.account_misses >= self.cfg.pool_gone_confirmations:
                    self._trigger(mint, row, life, 'POOL_GONE', {'why': wrong, 'reads': life.account_misses})
            elif life.account_misses:
                life.account_misses = 0
                self._write({'ev': 'account_seen', 'mint': mint, 'open_fill_id': open_id})
        if freeze_authority_set(mint_account) and not life.freeze_flagged:
            life.freeze_flagged = True
            self.counts['freeze_flags'] += 1
            self._write({'ev': 'freeze_flag', 'mint': mint, 'open_fill_id': open_id})
            if self.cfg.freeze_forces_exit:
                self._trigger(mint, row, life, 'FREEZE', {'why': 'freeze authority set'})

    # -- isolation ---------------------------------------------------------------------------------------------------------
    def _isolated(self, runner, work, *args):
        """Isolated like any other position check: only an accounting or store failure may escape."""
        try:
            work(runner, *args)
        except (paper.AccountingHalt, StoreError, sqlite3.Error):
            raise
        except Exception as error:
            runner._error('HELD_RISK_FAILED', transient=False, scope='held_risk', message=type(error).__name__)

    def after_marks_safe(self, runner, positions, states):
        self._isolated(runner, self.after_marks, positions, states)

    def after_exits_safe(self, runner):
        self._isolated(runner, self.after_exits)

    # -- health ------------------------------------------------------------------------------------------------------------------
    def health(self):
        try:
            states = self.store.position_states()
        except Exception:
            states = {}
        failing = sorted(m for m, row in states.items()
                         if self.trigger(m, row['open_fill_id']) and row['state'].get('exit_failing_since') is not None)
        return {'enabled': True, **self.counts, 'failing_now': failing}


def net_lamports(out_amount, pcfg):
    """Net proceeds of a paper sell of ``out_amount`` lamports: the slippage haircut, less the fixed fee. Never negative."""
    return max(0, out_amount * (paper.MAX_BPS - pcfg.slippage_bps) // paper.MAX_BPS - pcfg.fee_lamports)


def freeze_authority_set(account):
    """True when an SPL / Token-2022 mint account carries a freeze authority (COption tag at bytes 46..50). A missing or short
    account is not evidence of a freeze."""
    if not isinstance(account, dict):
        return False
    try:
        from desk.security import account_bytes
        data = account_bytes(account)
    except ValueError:
        return False
    return len(data) >= 82 and int.from_bytes(data[46:50], 'little') == 1


def build(cfg_dict, store, *, code_version, strategy_version, clock):
    """None when disabled (the runner then behaves exactly as before)."""
    cfg = Config.from_dict(cfg_dict)
    if not cfg.enabled:
        return None
    return HeldRisk(store, cfg, code_version=code_version, strategy_version=strategy_version, clock=clock)
