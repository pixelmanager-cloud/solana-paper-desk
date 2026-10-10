"""Opt-in concurrent paper entries: pure gates, budget and wall-time arithmetic.

`paper_concurrent_entries_version: 1` (with `max_positions` > 1) lets the entry
paths proceed while positions are open. Absent, every caller keeps the previous
rule (any open position or non-RUNNING mode refuses entry). Nothing here reads a
provider, writes a store or replaces `engine.transition`, whose gates (max
positions, exposure, cooldowns, STALE_PORTFOLIO, daily pause) stay authoritative.
"""
import time
from decimal import Decimal

from . import engine, portfolio_marks
from .model import decimal

VERSION = 1
KEY = 'paper_concurrent_entries_version'
TTL_KEY = engine.PORTFOLIO_TTL_KEY
MAX_CONCURRENT = 8

# Held monitoring cost per position per leg; matches paper_monitor_operator.preflight.
LEG_REQUESTS = 5
# Held passes that spend the monitoring allowance, per hour, from EVERY source (T16I). The reserve is derived from these
# bounds, not from the timer alone:
#   timer      OnUnitInactiveSec=120, so a pass starts at most every 120 + 7.8 s: ceil(3600 / 127.8) = 29
#   watcher    tools/ops/held_watcher.py `.path` trigger, bounded by its per-reason budgets (--max-triggers-hour 60 price
#              triggers plus --max-time-triggers-hour 30 time triggers); a parity test pins these to the watcher's defaults
#   checkpoint dispatcher checkpoints: at most 3 per entry dispatch, and at most MAX_ENTRY_DISPATCHES_PER_HOUR dispatches
#              per hour in a concurrent experiment (enforced from the dispatcher journal before the intent)
HELD_TIMER_SECONDS = 120.0
LEG_SECONDS = 7.8
TIMER_PASSES_PER_HOUR = -(-3600 // int(HELD_TIMER_SECONDS + LEG_SECONDS + 0.999))
WATCHER_PRICE_TRIGGERS_PER_HOUR = 60
WATCHER_TIME_TRIGGERS_PER_HOUR = 30
MAX_ENTRY_DISPATCHES_PER_HOUR = 12
CHECKPOINTS_PER_DISPATCH = 3
CHECKPOINT_PASSES_PER_HOUR = MAX_ENTRY_DISPATCHES_PER_HOUR * CHECKPOINTS_PER_DISPATCH
RESERVE_PASSES = (TIMER_PASSES_PER_HOUR + WATCHER_PRICE_TRIGGERS_PER_HOUR + WATCHER_TIME_TRIGGERS_PER_HOUR
                  + CHECKPOINT_PASSES_PER_HOUR)
# The allowance this experiment needs: the approved 3600/rolling-hour ceiling. The legacy provisioned 60/hour cannot carry it.
APPROVED_ALLOWANCE = 3600
LEGACY_ALLOWANCE = 60
# Batched portfolio marks (T35): one extra monitoring request per held pass, two per entry that has positions.
MARKS_PASS_REQUESTS = 1
MARKS_ENTRY_REQUESTS = 2
# Wall seconds one batched marks read costs the budget (a paced request plus margin; T36's faster pacing only helps).
MARKS_REFRESH_SECONDS = 2.5
# Native-clock Kraken fixture timings (docs/readiness.md): BUY 7.006 s, SELL 7.807 s.
# An entry is history preparation (desk.paper_history_preparation.PREPARATION_SECONDS, asserted equal in tests)
# followed by the bounded 10 s cycle plus its 2 s sleep, all BEFORE the engine decides.
PREPARATION_SECONDS = 18
CYCLE_SECONDS = 12.0
# Birth acquisition (paced setup RPCs) and migration-slot intake (7.451 s local delay in the 2026-10-10 native-clock
# regression, docs/readiness.md) run BEFORE preparation and the cycle, so they age every held mark as well.
# Conservative planning figures, not measurements of a live tick.
ACQUISITION_SECONDS = 12.0
INTAKE_SECONDS = 10.0
ENTRY_PHASES = (('acquisition', ACQUISITION_SECONDS), ('intake', INTAKE_SECONDS),
                ('preparation', float(PREPARATION_SECONDS)), ('cycle', CYCLE_SECONDS))
ENTRY_SECONDS = sum(seconds for _, seconds in ENTRY_PHASES)
# Wall caps ENFORCED by the dispatcher (T16H): no request of an over-cap phase starts, and an overrun ends the intent as a
# typed terminal NO_ENTRY. Preparation and the cycle are already capped in code (18 s history preparation, 10 s cycle deadline).
PHASE_CAPS = {'acquisition': 2.5 * ACQUISITION_SECONDS, 'intake': 2.5 * INTAKE_SECONDS}
# Longest wall time a held position may go without a monitoring leg while an entry runs: the held cadence.
HELD_MAX_GAP_SECONDS = 120.0


def selected(cfg):
    """0 when absent; 1 when explicitly and validly enabled; otherwise fail closed.

    The 10 s price TTL applies to every held mark. With N positions it is met by the batched portfolio marks
    (`paper_portfolio_mark_source_version`), not by relaxing it; the flag requires neither.
    """
    if KEY not in cfg:
        if engine.ROLLOVER_KEY in cfg:
            raise ValueError('Rollover rule requires the concurrent entries experiment')
        if TTL_KEY in cfg:
            raise ValueError('Portfolio mark TTL requires the concurrent entries experiment')
        return 0
    value = cfg[KEY]
    if type(value) is not int or value != VERSION:
        raise ValueError('Unsupported concurrent entries version')
    cap = cfg.get('max_positions')
    if cfg.get('mode') != 'paper' or type(cap) is not int or not 2 <= cap <= MAX_CONCURRENT:
        raise ValueError('Concurrent entries require paper mode and 2..8 max_positions')
    engine.portfolio_ttl(cfg)  # type and range validation
    engine.rollover_after_mark(cfg)
    portfolio_marks.selected(cfg)   # validated whenever the concurrent experiment is
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


def monitoring_required(positions_after_entry, reserve_passes=None, marks=False):
    """Allowance that must remain so every position, the new one included, keeps its passes.

    Per pass: LEG_REQUESTS per position (+1 batched marks read when ``marks``); plus the entry's own two marks reads.
    Worked (RESERVE_PASSES = 29 timer + 60 + 30 watcher + 36 checkpoint = 155): 4 positions with marks = 155 x (5 x 4 + 1) + 2 = 3257 of
    the 3600/hour allowance; 5 positions = 155 x 26 + 2 = 4032 > 3600, so the bounded worst case carries at most 4."""
    reserve_passes = RESERVE_PASSES if reserve_passes is None else reserve_passes
    per_pass = LEG_REQUESTS * positions_after_entry + (MARKS_PASS_REQUESTS if marks else 0)
    return reserve_passes * per_pass + (MARKS_ENTRY_REQUESTS if marks else 0)


def allowance_refusal(remaining, cap, positions_after_entry, marks=False):
    """None when the allowance can carry the entry, else a CLEAR reason: names the legacy 60/hour store explicitly."""
    need = monitoring_required(positions_after_entry, marks=marks)
    if remaining is None or remaining >= need:
        return None
    if cap is not None and cap <= LEGACY_ALLOWANCE:
        return ('MONITORING_ALLOWANCE_LEGACY_60: the store is on the legacy %d/hour allowance; %d positions need %d of the '
                'approved %d/hour (upgrade it explicitly)' % (cap, positions_after_entry, need, APPROVED_ALLOWANCE))
    return 'MONITORING_RESERVE_INSUFFICIENT: %d of the allowance remain, %d positions need %d' % (remaining, positions_after_entry, need)


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
    if monitoring_remaining is not None and monitoring_remaining < monitoring_required(after, marks=bool(portfolio_marks.selected(cfg))):
        blockers.append('MONITORING_RESERVE_INSUFFICIENT')
    if now is not None and state['positions'] and not portfolio_marks.selected(cfg):
        # With batched portfolio marks the cycle refreshes every mark right before the decision, so the age of the
        # marks at the start of the tick says nothing about their age at the decision.
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


def plan_legs(positions, wall_seconds=None, leg_seconds=LEG_SECONDS, refresh_seconds=0.0):
    """(run now, deferred) held legs, oldest mark first.

    `wall_seconds=None` covers every open position. A finite budget runs as many whole legs
    as fit (at least one) and defers the rest to the next pass in the same order. The pass's batched marks refresh
    (`refresh_seconds`, T35F) is spent first, so it is counted against the same wall budget.
    """
    order = held_order(positions)
    if wall_seconds is None:
        return order, []
    count = max(1, int((Decimal(str(wall_seconds)) - Decimal(str(refresh_seconds))) // Decimal(str(leg_seconds))))
    return order[:count], order[count:]


def checkpoint_due(elapsed_since_held, next_phase_seconds, positions, *, max_gap=HELD_MAX_GAP_SECONDS, refresh_seconds=0.0):
    """True when running the next uninterruptible entry phase would stretch the gap since the last held
    legs past `max_gap` (the legs themselves take `held_wall_seconds(positions)`). Pure; flat books never need it."""
    if not positions:
        return False
    return elapsed_since_held + next_phase_seconds + refresh_seconds + held_wall_seconds(len(positions)) > max_gap


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
