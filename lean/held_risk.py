"""Held-position rug / unsellable handling for the lean paper trader (L11). PAPER ONLY: nothing here signs, builds or sends a transaction.

For every held mint the position loop watches four things:

  (a) LIQUIDITY_DROP  the pool's quote vault fell by more than ``rug_liq_drop_frac`` (default 0.7) since entry;
  (b) FREEZE          the mint's freeze authority is set (a cheap account read every ``freeze_check_every`` position passes);
                      recorded and reported; it forces an exit only when ``freeze_forces_exit`` is true (default false);
  (c) NO_ROUTE        the Jupiter sell quote has had no route for longer than ``unsellable_after_s`` (default 120 s), probed
                      every ``route_probe_s`` (default 60 s); a provider OUTAGE (timeout, 5xx, 429) is not "no route";
  (d) POOL_GONE       the pool account is closed (or a vault is gone), or its owner program changed (migrated).

On (a), (c) or (d) the position is sold in full at a fresh quote (q1) with the exit reason ``RUG_EXIT``. If no executable route
exists (a non-transient quote failure, or a route worth less than the fee) the position is marked UNSELLABLE: it is valued at the
best executable quote seen so far, or 0 if there never was one, and written off in the books after ``unsellable_writeoff_s``
(default 6 h) at that value with the exit reason ``RUG_WRITEOFF``. While UNSELLABLE the exit is re-attempted every pass, so a route
that comes back is used.

State is persisted as generic ``events`` rows of kind ``held_risk`` (no schema change: a lean store is never migrated) and rebuilt
from them at start, so UNSELLABLE state, the no-route clock and the entry baseline survive a restart. Each lifecycle is keyed by
``(mint, open_fill_id)``, so a later re-entry of the same mint starts clean.

A failure in this module is isolated like any other position check (an ``errors`` row, the loop moves on); it never touches the
entry path. The only things that halt the trader are still an accounting violation or a store failure.
"""
import json
from decimal import Decimal, InvalidOperation

from lean import adapters as A, paper, strategy as S
from lean.providers import ProviderError

KIND = 'held_risk'
SOL_MINT = A.SOL_MINT
REASON_RUG_EXIT = 'RUG_EXIT'
REASON_WRITEOFF = 'RUG_WRITEOFF'
TRIGGERS = ('LIQUIDITY_DROP', 'NO_ROUTE', 'POOL_GONE', 'FREEZE')
DEFAULTS = {
    'enabled': True,
    'rug_liq_drop_frac': '0.7',
    'unsellable_after_s': 120,
    'unsellable_writeoff_s': 21600,
    'route_probe_s': 60,
    'freeze_check_every': 6,
    'freeze_forces_exit': False,
}


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
        for name in ('unsellable_after_s', 'unsellable_writeoff_s', 'route_probe_s', 'freeze_check_every'):
            if type(merged[name]) is not int or not 1 <= merged[name] <= 10 ** 7:
                raise HeldRiskConfigError('%s must be a positive integer' % name)
        for name in ('enabled', 'freeze_forces_exit'):
            if type(merged[name]) is not bool:
                raise HeldRiskConfigError('%s must be true or false' % name)
        self.enabled, self.rug_liq_drop_frac = merged['enabled'], frac
        self.unsellable_after_s, self.unsellable_writeoff_s = merged['unsellable_after_s'], merged['unsellable_writeoff_s']
        self.route_probe_s, self.freeze_check_every = merged['route_probe_s'], merged['freeze_check_every']
        self.freeze_forces_exit = merged['freeze_forces_exit']
        if self.unsellable_writeoff_s < self.unsellable_after_s:
            raise HeldRiskConfigError('unsellable_writeoff_s must not be below unsellable_after_s')

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
        self.baseline_quote = None          # quote-vault lamports at entry
        self.pool_owner = None
        self.no_route_since = None
        self.last_good_value = None         # lamports: net proceeds of the last executable full-size sell quote
        self.trigger = None                 # (reason, detail) while an exit is wanted
        self.unsellable = None              # {'since', 'value_lamports', 'reason'}
        self.freeze_flagged = False
        self.last_probe = None
        self.closed = False


class HeldRisk:
    def __init__(self, store, cfg, *, code_version, strategy_version, clock):
        self.store, self.cfg, self.clock = store, cfg, clock
        self.code_version, self.strategy_version = code_version, strategy_version
        self.lives = {}                     # (mint, open_fill_id) -> Life
        self.reserves = {}                  # mint -> (quote vault lamports, mark_at) from the latest marks read
        self.vault_missing = {}             # mint -> mark_at when a vault account was missing
        self.passes = 0
        self.counts = {'triggers': 0, 'rug_exits': 0, 'unsellable': 0, 'writeoffs': 0, 'freeze_flags': 0}
        self._replay()

    # -- persistence ----------------------------------------------------------------------------------------------
    def _write(self, payload):
        self.store.record(KIND, payload, code_version=self.code_version, strategy_version=self.strategy_version, ts=self.clock())

    def _replay(self):
        for row in self.store.rows('events', kind=KIND, limit=100000):
            try:
                p = json.loads(row['payload'])
                life = self.lives.setdefault((p['mint'], int(p['open_fill_id'])), Life())
            except (ValueError, KeyError, TypeError):
                continue
            ev = p.get('ev')
            if ev == 'baseline':
                life.baseline_quote, life.pool_owner = p.get('quote_reserve_lamports'), p.get('pool_owner')
            elif ev == 'pool_owner':
                life.pool_owner = p.get('pool_owner')
            elif ev == 'no_route':
                life.no_route_since = p.get('since')
                if p.get('last_good_value_lamports') is not None:
                    life.last_good_value = p['last_good_value_lamports']
            elif ev == 'route_ok':
                life.no_route_since = None
            elif ev == 'freeze_flag':
                life.freeze_flagged = True
                self.counts['freeze_flags'] += 1
            elif ev == 'unsellable':
                life.unsellable = {'since': p['since'], 'value_lamports': p['value_lamports'], 'reason': p.get('reason')}
            elif ev == 'trigger':
                self.counts['triggers'] += 1
            elif ev == 'closed':
                life.closed = True
                if p.get('reason') == REASON_WRITEOFF:
                    self.counts['writeoffs'] += 1
                elif p.get('reason') == REASON_RUG_EXIT:
                    self.counts['rug_exits'] += 1
        self.counts['unsellable'] = sum(1 for l in self.lives.values() if l.unsellable)

    def life(self, mint, open_fill_id):
        return self.lives.setdefault((mint, int(open_fill_id)), Life())

    # -- inputs from the marks read (runner hook) -----------------------------------------------------------------
    def observe(self, mint, base_raw, quote_lamports, mark_at):
        self.reserves[mint] = (int(quote_lamports), mark_at)
        self.vault_missing.pop(mint, None)

    def observe_missing(self, mint, mark_at):
        self.vault_missing[mint] = mark_at

    # -- the periodic work (runner hook, once per position pass) -----------------------------------------------------
    def tick(self, runner, positions, states, now):
        """Baselines, the account checks, the route probes, the triggers; and the valuation of UNSELLABLE positions."""
        self.passes += 1
        held = {m: states[m] for m in positions if m in states}
        for mint, row in held.items():
            life = self.life(mint, row['open_fill_id'])
            if life.baseline_quote is None:
                self._baseline(runner, mint, row, life, now)
            if life.unsellable:
                value = life.unsellable['value_lamports']
                runner.marks[mint] = (A.sol(int(value)), now)         # valued at the best executable quote, or 0
                continue
            if life.trigger is None:
                self._detect_reserves(mint, row, life)
        if self.passes % self.cfg.freeze_check_every == 1 or self.cfg.freeze_check_every == 1:
            self._account_checks(runner, held, now)
        for mint, row in held.items():
            life = self.life(mint, row['open_fill_id'])
            if life.unsellable or life.trigger is not None:
                continue
            if life.last_probe is None or now - life.last_probe >= self.cfg.route_probe_s:
                self._probe(runner, mint, positions[mint], row, life, now)

    def _baseline(self, runner, mint, row, life, now):
        quote, source = None, None
        try:
            for decision in reversed(self.store.rows('decisions', mint=mint, kind='screen', limit=20)):
                if decision['action'] == 'PASS':
                    features = json.loads(decision['features'])
                    if features.get('quote_gross_raw') is not None:
                        quote, source = int(features['quote_gross_raw']), 'screen'
                    break
        except (ValueError, TypeError, KeyError):
            quote = None
        if quote is None and mint in self.reserves:
            quote, source = self.reserves[mint][0], 'first_mark'
        if quote is None:
            return                                                    # no evidence yet: try again next pass
        life.baseline_quote = quote
        self._write({'ev': 'baseline', 'mint': mint, 'open_fill_id': row['open_fill_id'], 'quote_reserve_lamports': quote,
                     'pool_owner': life.pool_owner, 'source': source})

    def _detect_reserves(self, mint, row, life):
        if mint in self.vault_missing:
            self._trigger(mint, row, life, 'POOL_GONE', {'why': 'vault account missing'})
            return
        seen = self.reserves.get(mint)
        if seen is None or life.baseline_quote is None or life.baseline_quote <= 0:
            return
        keep = Decimal(1) - self.cfg.rug_liq_drop_frac
        if Decimal(seen[0]) < Decimal(life.baseline_quote) * keep:
            self._trigger(mint, row, life, 'LIQUIDITY_DROP',
                          {'baseline_lamports': life.baseline_quote, 'now_lamports': seen[0], 'drop_frac': str(self.cfg.rug_liq_drop_frac)})

    def _trigger(self, mint, row, life, reason, detail):
        if life.trigger is not None:
            return
        life.trigger = (reason, detail)
        self.counts['triggers'] += 1
        self._write({'ev': 'trigger', 'mint': mint, 'open_fill_id': row['open_fill_id'], 'reason': reason, 'detail': detail})

    def _account_checks(self, runner, held, now):
        """ONE batched read of [mint, pool] per held position on the exit lane: freeze authority, pool closed or migrated."""
        mints = list(held)
        for start in range(0, len(mints), 50):
            chunk = mints[start:start + 50]
            keys = [k for m in chunk for k in (m, held[m]['state']['pool'])]
            try:
                result, raw, _meta = runner.exit_providers.helius.get_multiple_accounts(keys)
            except ProviderError as error:
                runner._error(error.code, transient=error.transient, scope='held_risk', message='%d positions' % len(chunk), raw=error.raw)
                continue
            for i, mint in enumerate(chunk):
                row = held[mint]
                life = self.life(mint, row['open_fill_id'])
                if life.unsellable:
                    continue
                self._read_accounts(mint, row, life, result['value'][2 * i], result['value'][2 * i + 1])

    def _read_accounts(self, mint, row, life, mint_account, pool_account):
        if pool_account is None:
            self._trigger(mint, row, life, 'POOL_GONE', {'why': 'pool account closed'})
        else:
            owner = pool_account.get('owner') if isinstance(pool_account, dict) else None
            if life.pool_owner is None and isinstance(owner, str):
                life.pool_owner = owner
                self._write({'ev': 'pool_owner', 'mint': mint, 'open_fill_id': row['open_fill_id'], 'pool_owner': owner})
            elif isinstance(owner, str) and owner != life.pool_owner:
                self._trigger(mint, row, life, 'POOL_GONE', {'why': 'pool owner changed', 'from': life.pool_owner, 'to': owner})
        authority = freeze_authority_set(mint_account)
        if authority and not life.freeze_flagged:
            life.freeze_flagged = True
            self.counts['freeze_flags'] += 1
            self._write({'ev': 'freeze_flag', 'mint': mint, 'open_fill_id': row['open_fill_id']})
            if self.cfg.freeze_forces_exit:
                self._trigger(mint, row, life, 'FREEZE', {'why': 'freeze authority set'})

    def _probe(self, runner, mint, position, row, life, now):
        """A full-size sell quote on the exit lane. A non-transient failure starts the no-route clock; success clears it.
        A probe is not an exit: its raw bytes are kept only when it FAILS (research evidence), never on success."""
        life.last_probe = now
        try:
            quote, _raw, _meta = runner.exit_providers.jupiter.quote(mint, SOL_MINT, position.qty_raw, runner.taker)
        except ProviderError as error:
            runner._error(error.code, transient=error.transient, mint=mint, scope='route_probe', raw=error.raw)
            if error.transient:
                return                                                # an outage is not "no route"
            if life.no_route_since is None:
                life.no_route_since = now
                self._write({'ev': 'no_route', 'mint': mint, 'open_fill_id': row['open_fill_id'], 'since': now, 'code': error.code,
                             'last_good_value_lamports': life.last_good_value})
            if now - life.no_route_since >= self.cfg.unsellable_after_s:
                self._trigger(mint, row, life, 'NO_ROUTE', {'since': life.no_route_since, 'code': error.code})
            return
        value = _net_lamports(quote.out_amount, runner.pcfg)
        if value > 0:
            life.last_good_value = value
        if life.no_route_since is not None:
            life.no_route_since = None
            self._write({'ev': 'route_ok', 'mint': mint, 'open_fill_id': row['open_fill_id']})

    def tick_safe(self, runner, positions, states, now):
        """``tick`` isolated like any other position check: only an accounting or store failure may escape."""
        import sqlite3
        from lean.store import StoreError
        try:
            self.tick(runner, positions, states, now)
        except (paper.AccountingHalt, StoreError, sqlite3.Error):
            raise
        except Exception as error:
            runner._error('HELD_RISK_FAILED', transient=False, scope='held_risk', message=type(error).__name__)

    # -- the exit (runner hook, per position, before the normal strategy) -----------------------------------------------
    def manage(self, runner, mint, position, row):
        """None = carry on with the normal strategy; otherwise the number of fills made (0 or 1)."""
        life = self.life(mint, row['open_fill_id'])
        now = int(runner.clock())
        if life.unsellable:
            return self._retry_unsellable(runner, mint, position, row, life, now)
        if life.trigger is None:
            return None
        reason, detail = life.trigger
        return self._force_exit(runner, mint, position, row, life, now, reason, detail)

    def _force_exit(self, runner, mint, position, row, life, now, reason, detail):
        qty = position.qty_raw
        try:
            quote, ref, quote_at = runner._quote(runner.exit_providers, mint, SOL_MINT, qty, 'quote:rug_exit', mint)
        except ProviderError as error:
            runner._error(error.code, transient=error.transient, mint=mint, scope='rug_exit', raw=error.raw)
            if error.transient:
                return 0                                              # retried next pass; an outage is not "no route"
            self._mark_unsellable(runner, mint, row, life, now, reason='NO_ROUTE:%s' % error.code)
            return 0
        if _net_lamports(quote.out_amount, runner.pcfg) <= 0:          # a route worth less than the fee: nothing executable
            self._mark_unsellable(runner, mint, row, life, now, reason='WORTHLESS_ROUTE')
            return 0
        return self._sell(runner, mint, position, row, life, quote, ref, now, REASON_RUG_EXIT, reason, detail)

    def _sell(self, runner, mint, position, row, life, quote, ref, now, exit_reason, trigger, detail):
        open_id, state = row['open_fill_id'], row['state']
        with runner._trade_lock:
            fill = paper.sell(position, A.paper_quote(quote, mint=mint, side='sell', decimals=position.decimals, ts=runner.clock(), ref=ref),
                              cfg=runner.pcfg, qty_raw=position.qty_raw)
            runner.store.add_decision('exit', 'SELL', mint=mint, reasons=[exit_reason, trigger], ts=runner.clock(),
                                      features={'trigger': trigger, 'detail': detail, 'qty_raw': position.qty_raw, 'quote_ref': ref})
            closed = dict(state, reason=exit_reason, trigger=trigger, trade_pnl_lamports=position.realized_lamports + fill.realized_lamports,
                          cooldown_until=S.cooldown_until('STOP', now, runner.cfg))
            runner.store.add_fill(fill, state={'event': 'closed', 'open_fill_id': open_id, 'state': closed})
            self._write({'ev': 'closed', 'mint': mint, 'open_fill_id': open_id, 'reason': exit_reason})
            life.closed = True
            runner.marks.pop(mint, None)
            runner._count('closed')
            runner._count('exits')
            self.counts['rug_exits'] += 1
            runner._check()
        return 1

    def _mark_unsellable(self, runner, mint, row, life, now, *, reason):
        value = int(life.last_good_value or 0)
        life.unsellable = {'since': now, 'value_lamports': value, 'reason': reason}
        self.counts['unsellable'] += 1
        runner.marks[mint] = (A.sol(value), now)                       # equity values it there from this very pass
        self._write({'ev': 'unsellable', 'mint': mint, 'open_fill_id': row['open_fill_id'], 'since': now, 'value_lamports': value,
                     'reason': reason, 'trigger': life.trigger[0] if life.trigger else None})
        runner.store.add_decision('exit', 'UNSELLABLE', mint=mint, reasons=[reason], ts=runner.clock(),
                                  features={'value_lamports': value, 'trigger': life.trigger[0] if life.trigger else None})

    def _retry_unsellable(self, runner, mint, position, row, life, now):
        """A route that comes back is used; otherwise, after ``unsellable_writeoff_s``, the position is closed in the books."""
        try:
            quote, ref, _at = runner._quote(runner.exit_providers, mint, SOL_MINT, position.qty_raw, 'quote:rug_exit', mint)
            if _net_lamports(quote.out_amount, runner.pcfg) > 0:
                return self._sell(runner, mint, position, row, life, quote, ref, now, REASON_RUG_EXIT, 'ROUTE_RESTORED',
                                  {'unsellable_since': life.unsellable['since']})
        except ProviderError as error:
            runner._error(error.code, transient=error.transient, mint=mint, scope='rug_exit', raw=error.raw)
        if now - life.unsellable['since'] >= self.cfg.unsellable_writeoff_s:
            return self._writeoff(runner, mint, position, row, life, now)
        return 0

    def _writeoff(self, runner, mint, position, row, life, now):
        """Close the position in the books at the best executable value (0 if there never was one). No fee: nothing was sent."""
        value = int(life.unsellable['value_lamports'])
        open_id, state = row['open_fill_id'], row['state']
        with runner._trade_lock:
            cost = position.cost_lamports
            fill = paper.Fill(ts=runner.clock(), mint=mint, side='sell', qty_raw=position.qty_raw, sol_lamports=value, fee_lamports=0,
                              slippage_bps=0, decimals=position.decimals, cost_sold_lamports=cost, realized_lamports=value - cost,
                              quote_ref=None)
            runner.store.add_decision('exit', 'WRITEOFF', mint=mint, reasons=[REASON_WRITEOFF], ts=runner.clock(),
                                      features={'value_lamports': value, 'unsellable_since': life.unsellable['since'], 'cost_lamports': cost})
            closed = dict(state, reason=REASON_WRITEOFF, trigger=life.unsellable.get('reason'),
                          trade_pnl_lamports=position.realized_lamports + fill.realized_lamports,
                          cooldown_until=S.cooldown_until('STOP', now, runner.cfg))
            runner.store.add_fill(fill, state={'event': 'closed', 'open_fill_id': open_id, 'state': closed})
            self._write({'ev': 'closed', 'mint': mint, 'open_fill_id': open_id, 'reason': REASON_WRITEOFF, 'value_lamports': value})
            life.closed = True
            runner.marks.pop(mint, None)
            runner._count('closed')
            runner._count('exits')
            self.counts['writeoffs'] += 1
            runner._check()
        return 1

    # -- health -------------------------------------------------------------------------------------------------------------
    def health(self):
        return {'enabled': True, **self.counts, 'unsellable_now': sorted(m for (m, _), l in self.lives.items() if l.unsellable and not l.closed)}


def _net_lamports(out_amount, pcfg):
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
