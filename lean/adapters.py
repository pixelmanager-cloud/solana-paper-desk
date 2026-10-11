"""The ONE place where the lean modules' units and types meet. Pure functions, no I/O (the store is only read).

Units, and who uses them:
  * lean.providers   Jupiter ``Quote``: integers (lamports for SOL, raw units for tokens); getMultipleAccounts: the RPC
                     result ``{'context','value':[{data:[b64,'base64'],...}]}``.
  * lean.paper       integers: lamports and raw token units (``paper.Quote``, ``Fill``, ``Position``).
  * lean.strategy    Decimal SOL for money; token quantities are raw units held in a Decimal (whole units only when
                     sold); marks are the NET SOL value of the whole remaining position, with the epoch second read.
  * lean.store       integers in fills; JSON (Decimal as str) in position_state.

Every conversion between them is one function below; the runner never converts by hand.
"""
from decimal import Decimal, ROUND_DOWN

from desk.security import account_bytes
from lean import paper, strategy as S
from lean.candidates import DEFAULTS as SCREEN_DEFAULTS

LAMPORTS = paper.LAMPORTS
SOL_MINT = 'So11111111111111111111111111111111111111112'


class AdapterError(ValueError):
    """Evidence that cannot be converted (a malformed account, a non-integral slippage...). Per-candidate/position."""


# ------------------------------------------------------------------------------------------------- money
def sol(lamports):
    """int lamports -> Decimal SOL (exact)."""
    if type(lamports) is not int:
        raise AdapterError('lamports must be an int')
    return Decimal(lamports) / LAMPORTS


def lamports(sol_amount):
    """Decimal SOL -> int lamports; sub-lamport amounts are refused (strategy sizes are quantized to 1e-9)."""
    return paper.to_lamports(sol_amount)


# ------------------------------------------------------------------------------------------------ configs
def paper_config(cfg):
    """StrategyConfig -> PaperConfig: the strategy's fee and slippage ARE the paper fill's (never two sets of numbers)."""
    bps = cfg.adverse_slippage_bps
    if bps != bps.to_integral_value():
        raise AdapterError('adverse_slippage_bps must be a whole number for paper fills')
    fee = Decimal(cfg.fixed_fee_sol) * LAMPORTS
    if fee != fee.to_integral_value():
        raise AdapterError('fixed_fee_sol must be a whole number of lamports')
    return paper.PaperConfig(fee_lamports=int(fee), slippage_bps=int(bps))


def screen_config(cfg, overrides=None):
    """The candidates.screen config: market-cap/liquidity bands from the StrategyConfig (one source of truth), plus the
    screen-only settings (age window, holder check...) from lean.json ``screen``. Unknown keys are refused."""
    overrides = dict(overrides or {})
    shared = {'min_market_cap_usd', 'max_market_cap_usd', 'min_liquidity_usd'}
    unknown = set(overrides) - (set(SCREEN_DEFAULTS) - shared)
    if unknown:
        raise AdapterError('unknown or strategy-owned screen keys: %s' % sorted(unknown))
    out = dict(SCREEN_DEFAULTS)
    out.update(overrides)
    out.update(min_market_cap_usd=cfg.min_market_cap_usd, max_market_cap_usd=cfg.max_market_cap_usd,
               min_liquidity_usd=cfg.min_liquidity_usd)
    return out


# ------------------------------------------------------------------------------------------------ entry
def entry_features(features, mint):
    """Screen features -> strategy entry features. ``reserve_sol`` is the pool's spendable SOL reserve."""
    out = dict(features)
    out['mint'] = mint
    spendable = features.get('quote_spendable_raw')
    if spendable is not None:
        out['reserve_sol'] = str(Decimal(int(spendable)) / LAMPORTS)
    return out


def sell_leg_qty(buy_out_amount, pcfg):
    """Raw tokens the paper BUY will hold (paper's slippage haircut): the round-trip sell leg is quoted for exactly this,
    and it equals ``strategy.sell_quote_tokens``."""
    return buy_out_amount * (paper.MAX_BPS - pcfg.slippage_bps) // paper.MAX_BPS


def strategy_quote(quote, side, observed_at):
    """provider Quote (ints) -> strategy.Quote (SOL legs in Decimal SOL, token legs in raw units)."""
    if side == 'buy':
        return S.Quote('buy', sol(quote.in_amount), Decimal(quote.out_amount), int(observed_at))
    if side == 'sell':
        return S.Quote('sell', Decimal(quote.in_amount), sol(quote.out_amount), int(observed_at))
    raise AdapterError('side must be buy or sell')


def roundtrip(buy_quote, buy_at, sell_quote, sell_at):
    return S.RoundTrip(strategy_quote(buy_quote, 'buy', buy_at), strategy_quote(sell_quote, 'sell', sell_at))


def paper_quote(quote, *, mint, side, decimals, ts, ref):
    """provider Quote -> paper.Quote (same integers; ``ref`` is the observation id of the raw response)."""
    return paper.Quote(mint, side, quote.in_amount, quote.out_amount, int(decimals), float(ts), 'jupiter',
                       None if ref is None else str(ref))


# --------------------------------------------------------------------------------------------- positions
def open_state(features, fill, cfg):
    """The position_state row written with the opening BUY: everything the position loop needs after any restart."""
    fields = S.new_position_fields(cfg)
    return {'pool': features['pool'], 'base_vault': features['pool_base_token_account'],
            'quote_vault': features['pool_quote_token_account'], 'decimals': int(fill.decimals),
            'initial_qty_raw': int(fill.qty_raw), 'stage': fields['stage'], 'stop_ratio': str(fields['stop_ratio']),
            'peak_ratio': str(fields['peak_ratio']), 'touched_15': fields['touched_15']}


def apply_updates(state, *updates):
    """New state dict with strategy ``position_updates`` / ``after_fill`` applied (Decimals stored as str)."""
    out = dict(state)
    for update in updates:
        for key, value in dict(update).items():
            if key not in ('stage', 'stop_ratio', 'peak_ratio', 'touched_15'):
                raise AdapterError('unexpected position field %r' % key)
            out[key] = str(value) if isinstance(value, Decimal) else value
    return out


def strategy_position(position, state):
    """store Position (ints) + its position_state -> strategy.Position (cost in Decimal SOL, quantities in raw units)."""
    return S.Position(mint=position.mint, opened_at=int(position.opened_at), qty=Decimal(position.qty_raw),
                      initial_qty=Decimal(int(state['initial_qty_raw'])), cost_left=sol(position.cost_lamports),
                      stop_ratio=Decimal(state['stop_ratio']), stage=int(state['stage']),
                      peak_ratio=Decimal(state['peak_ratio']), touched_15=bool(state['touched_15']))


def sell_qty_raw(decision_qty, held_qty_raw):
    """strategy ``Decision.qty`` (Decimal raw units, possibly fractional) -> int raw qty for ``paper.sell(qty_raw=)``:
    floored, never above what is held. The ladder fraction is of the INITIAL quantity; this is the exact amount."""
    qty = int(Decimal(decision_qty).to_integral_value(rounding=ROUND_DOWN))
    qty = min(qty, held_qty_raw)
    if qty <= 0:
        raise AdapterError('sell quantity rounds to nothing')
    return qty


# ------------------------------------------------------------------------------------------------- marks
def vault_amount(account):
    """Raw amount of an SPL / Token-2022 token account (RPC base64 shape)."""
    if not isinstance(account, dict):
        raise AdapterError('vault account missing')
    try:
        data = account_bytes(account)
    except ValueError:
        raise AdapterError('vault account malformed') from None
    if len(data) < 165:
        raise AdapterError('vault account too short')
    return int.from_bytes(data[64:72], 'little')


def mark(qty_raw, base_reserve_raw, quote_reserve_lamports, *, pool_fee_bps, pcfg):
    """NET SOL value of selling ``qty_raw`` into the pool now: constant-product output, less the pool fee and the fixed
    paper fee (the strategy's mark unit). No slippage haircut: that is the fill's, not the mark's. Never negative."""
    if base_reserve_raw <= 0 or quote_reserve_lamports <= 0 or qty_raw <= 0:
        raise AdapterError('empty pool or position')
    gross = quote_reserve_lamports * qty_raw // (base_reserve_raw + qty_raw)
    net = gross * (paper.MAX_BPS - int(pool_fee_bps)) // paper.MAX_BPS - pcfg.fee_lamports
    return sol(max(0, net))


# ------------------------------------------------------------------------------------------------ portfolio
def portfolio(store, cfg, now, *, marks, day_start_equity):
    """strategy.Portfolio built from the store: cash and exposure from fills, equity at the latest marks (cost basis when
    a position has none yet), cooldowns and the loss streak from closed positions, the entry throttle from the last BUY."""
    positions = store.positions()
    cash = sol(store.cash())
    exposure = sum((sol(p.cost_lamports) for p in positions.values()), Decimal(0))
    equity = cash + sum((marks[m][0] if m in marks else sol(p.cost_lamports) for m, p in positions.items()), Decimal(0))
    cooldowns, streak = {}, 0
    for closed in store.closed_positions():
        state = closed['state']
        cooldowns[closed['mint']] = int(state['cooldown_until'])
        streak = S.next_loss_streak(streak, Decimal(int(state['trade_pnl_lamports'])))
    last_buy = store.last_fill_ts('buy')
    return S.Portfolio(now=int(now), equity=equity, cash=cash, exposure=exposure, day_start_equity=day_start_equity,
                       open_mints=frozenset(positions), cooldowns=cooldowns,
                       last_entry_minute=-1 if last_buy is None else int(last_buy) // 60, loss_streak=streak,
                       last_entry_at=None if last_buy is None else int(last_buy))


def equity_and_freshness(store, now, marks, ttl):
    """(equity, marks_fresh) for the day-start baseline: fresh means every open position has a mark no older than ttl."""
    positions = store.positions()
    equity = sol(store.cash()) + sum((marks[m][0] if m in marks else sol(p.cost_lamports) for m, p in positions.items()),
                                     Decimal(0))
    fresh = all(m in marks and 0 <= int(now) - marks[m][1] <= ttl for m in positions)
    return equity, fresh
