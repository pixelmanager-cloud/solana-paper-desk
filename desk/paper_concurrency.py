"""Opt-in concurrent paper entries: pure gates, budget and wall-time arithmetic.

`paper_concurrent_entries_version: 1` (with `max_positions` > 1) lets the entry
paths proceed while positions are open. Absent, every caller keeps the previous
rule (any open position or non-RUNNING mode refuses entry). Nothing here reads a
provider, writes a store or replaces `engine.transition`, whose gates (max
positions, exposure, cooldowns, STALE_PORTFOLIO, daily pause) stay authoritative.
"""
import time
from decimal import Decimal

from . import engine
from .model import decimal

VERSION = 1
KEY = 'paper_concurrent_entries_version'
TTL_KEY = engine.PORTFOLIO_TTL_KEY
MAX_CONCURRENT = 8

# Held monitoring cost per position per leg; matches paper_monitor_operator.preflight.
LEG_REQUESTS = 5
# One hour of five-minute held passes stays reserved for every position, new one included.
RESERVE_PASSES = 12
# Native-clock Kraken fixture timings (docs/readiness.md): BUY 7.006 s, SELL 7.807 s.
# An entry is history preparation (desk.paper_history_preparation.PREPARATION_SECONDS, asserted equal in tests)
# followed by the bounded 10 s cycle plus its 2 s sleep, all BEFORE the engine decides.
PREPARATION_SECONDS = 18
CYCLE_SECONDS = 12.0
ENTRY_SECONDS = PREPARATION_SECONDS + CYCLE_SECONDS
LEG_SECONDS = 7.8


def selected(cfg):
    """0 when absent; 1 when explicitly and validly enabled; otherwise fail closed.

    The experiment also fixes the portfolio mark TTL explicitly (see engine.portfolio_ttl): without it
    no concurrent entry could ever fill under real provider pacing, so the flag alone is refused.
    """
    if KEY not in cfg:
        if TTL_KEY in cfg:
            raise ValueError('Portfolio mark TTL requires the concurrent entries experiment')
        return 0
    value = cfg[KEY]
    if type(value) is not int or value != VERSION:
        raise ValueError('Unsupported concurrent entries version')
    cap = cfg.get('max_positions')
    if cfg.get('mode') != 'paper' or type(cap) is not int or not 2 <= cap <= MAX_CONCURRENT:
        raise ValueError('Concurrent entries require paper mode and 2..8 max_positions')
    if TTL_KEY not in cfg:
        raise ValueError('Concurrent entries require paper_portfolio_mark_ttl_seconds')
    engine.portfolio_ttl(cfg)  # type and range validation
    return VERSION


def portfolio_ttl(cfg):
    return engine.portfolio_ttl(cfg)


def entry_blocked(state, cfg):
    """Drop-in for the legacy `state['positions'] or state['mode'] != 'RUNNING'` test."""
    if not selected(cfg):
        return bool(state['positions']) or state['mode'] != 'RUNNING'
    return bool(state_blockers(state, cfg))


def state_blockers(state, cfg):
    """Ledger-only reasons a concurrent entry must not start (no I/O)."""
    if not selected(cfg):
        return ['HELD_POSITION_PRIORITY'] if state['positions'] or state['mode'] != 'RUNNING' else []
    blockers = []
    if state['mode'] != 'RUNNING':
        blockers.append('LEDGER_MODE_NOT_RUNNING')
    if len(state['positions']) >= cfg['max_positions']:
        blockers.append('MAX_POSITIONS_REACHED')
    for position in state['positions'].values():
        if unresolved(position):
            blockers.append('HELD_EXIT_UNRESOLVED')
            break
    return blockers


def monitoring_required(positions_after_entry, reserve_passes=RESERVE_PASSES):
    """Allowance that must remain so every position, the new one included, keeps its passes."""
    return LEG_REQUESTS * positions_after_entry * reserve_passes


def portfolio_age_at_decision(state, now, entry_seconds=ENTRY_SECONDS):
    """Oldest held mark age when an entry started now would be decided (None if flat)."""
    marks = [p['mark_at'] for p in state['positions'].values()]
    return None if not marks else max(0, now - min(marks)) + entry_seconds


def entry_blockers(state, cfg, *, monitoring_remaining=None, now=None, entry_seconds=ENTRY_SECONDS):
    """All reasons to refuse starting an entry; empty means the engine may be asked.

    The engine rejects any entry whose held marks are older than the portfolio mark TTL
    (STALE_PORTFOLIO). The estimate includes history preparation, so an entry that cannot
    finish inside that window is refused here before any provider I/O instead of burning
    investigation requests and then being rejected.
    """
    blockers = state_blockers(state, cfg)
    if not selected(cfg):
        return blockers
    after = len(state['positions']) + 1
    if monitoring_remaining is not None and monitoring_remaining < monitoring_required(after):
        blockers.append('MONITORING_RESERVE_INSUFFICIENT')
    if now is not None and state['positions']:
        age = portfolio_age_at_decision(state, now, entry_seconds)
        if age > decimal(portfolio_ttl(cfg)):
            blockers.append('PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY')
    return blockers


def unresolved(position):
    return position.get('exit_blocked') is not None or position.get('mark_status') == 'UNVERIFIED_EXIT'


def held_order(positions):
    """Unresolved exits first, then least recently marked; ties by entry time then mint.

    Deterministic from the ledger alone, so a restart resumes the same order.
    """
    return sorted(positions, key=lambda mint: (not unresolved(positions[mint]), positions[mint]['mark_at'],
                                               positions[mint]['opened_at'], mint))


def held_wall_seconds(count, leg_seconds=LEG_SECONDS):
    return count * leg_seconds


def plan_legs(positions, wall_seconds=None, leg_seconds=LEG_SECONDS):
    """(run now, deferred) held legs, oldest mark first.

    `wall_seconds=None` covers every open position. A finite budget runs as many whole legs
    as fit (at least one) and defers the rest to the next pass in the same order.
    """
    order = held_order(positions)
    if wall_seconds is None:
        return order, []
    count = max(1, int(Decimal(str(wall_seconds)) // Decimal(str(leg_seconds))))
    return order[:count], order[count:]


def clock():
    return time.time()


def attribution(connection, cfg):
    """Read-only per-position fill attribution and portfolio cash identity.

    Raises ValueError when fills and the saved checkpoint disagree. Positions are
    keyed by mint and entry fill (a mint may be re-entered after its cooldown).
    """
    import json
    state = json.loads(connection.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
    cash = decimal(cfg['initial_equity_sol'])
    live, trips = {}, []
    for seq, event_id, payload in connection.execute('SELECT seq,event_id,payload FROM outcomes ORDER BY seq'):
        fill = json.loads(payload)
        if fill.get('type') != 'fill':
            continue
        mint = fill['mint']
        if fill['side'] == 'buy':
            if mint in live:
                raise ValueError('second entry while position open')
            cost = decimal(fill['amount_sol']) + decimal(fill['fee_sol'])
            cash -= cost
            live[mint] = {'mint': mint, 'entry_seq': seq, 'entry_event_id': event_id, 'cost': cost,
                          'qty': decimal(fill['quantity']), 'sold': Decimal(0), 'basis': Decimal(0),
                          'pnl': Decimal(0)}
            continue
        position = live.get(mint)
        if position is None:
            raise ValueError('sell without open entry')
        qty, proceeds, pnl = (decimal(fill[k]) for k in ('quantity', 'proceeds_sol', 'realized_pnl_sol'))
        cash += proceeds
        position['sold'] += qty
        position['basis'] += proceeds - pnl
        position['pnl'] += pnl
        if position['sold'] > position['qty'] + Decimal('1e-20'):
            raise ValueError('oversell')
        if position['qty'] - position['sold'] <= Decimal('1e-20'):
            trips.append(live.pop(mint))
    tolerance = Decimal('1e-18')
    if set(live) != set(state['positions']):
        raise ValueError('checkpoint positions differ from fills')
    held = sum((decimal(p['cost_left']) for p in state['positions'].values()), Decimal(0))
    realized = sum((p['pnl'] for p in [*trips, *live.values()]), Decimal(0))
    if abs(decimal(state['cash']) - cash) > tolerance:
        raise ValueError('checkpoint cash differs from fills')
    if abs(decimal(state['cash']) - (decimal(cfg['initial_equity_sol']) + decimal(state['realized_pnl']) - held)) > tolerance:
        raise ValueError('portfolio cash identity fails')
    if abs(decimal(state['realized_pnl']) - realized) > tolerance:
        raise ValueError('checkpoint realized PnL differs from fills')
    view = lambda p: {k: (str(v) if isinstance(v, Decimal) else v) for k, v in p.items()}
    return {'open': [view(p) for p in live.values()], 'closed': [view(p) for p in trips],
            'cash_sol': state['cash']}
