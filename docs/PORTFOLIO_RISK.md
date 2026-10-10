# Portfolio-level risk for concurrent positions (T31)

Opt-in, versioned, engine-authoritative and replay-deterministic. With `paper_portfolio_risk_version` absent the engine
behaves exactly as before (a scripted multi-position scenario hashes identically to the previous engine, and no extra
state key is ever written). Paper only; nothing here changes `EXECUTION_UNVERIFIED` labelling or any evidence gate.

The existing gates are untouched and keep precedence: mode, `max_positions`, per-mint cooldown, `ENTRY_THROTTLE` (one
entry per minute), `STALE_PORTFOLIO`, `max_exposure_fraction`, `max_position_fraction`, the daily pause and liquidation, the
`loss_streak >= 4` size halving and the `loss_streak >= 6` pause. The portfolio gates only ever make an entry smaller or
refuse it; they never touch exits, marks or existing positions.

## Configuration (frozen in the experiment config, so part of its hash)

```json
"paper_portfolio_risk_version": 1,
"portfolio_max_open_risk_fraction": "0.03",
"portfolio_loss_streak_cooldown_after": 3,
"portfolio_cooldown_minutes": 30,
"portfolio_max_entries_per_10m": 2
```

All four parameters are required with the flag: there are no hidden defaults. Fail closed (`ValueError` on the first
event, including control events): a flag other than int `1`, a non-`paper` mode, a missing or mistyped parameter, a
fraction outside `(0, 1)`, a streak threshold outside `1..100`, cooldown minutes outside `1..1440`, entries per 10 minutes
outside `1..10`, or **parameters present without the flag** (a typo in the flag name can never silently disable a gate).
The entry limit is capped at 10 because `ENTRY_THROTTLE` already allows one entry per minute: a larger limit could never
bind, and a gate that cannot act is refused rather than silently ignored.

## The gates

1. **Aggregate open risk** `<= X` of equity. Open risk of a position is `cost_left * max(0, 1 - stop_ratio)`: the loss
   if its own stop fills at its stop ratio. A position whose stop was raised to breakeven or above (after a take-profit
   rung) contributes 0, and a partially sold position contributes only the cost still at risk. At entry the budget is
   `room = X * equity - open_risk` and the largest allowed entry is `room / stop_fraction - fixed_fee_sol` (rounded down to
   9 decimals), so the new position's stop loss exactly fits. The entry size is `min(size(), that cap)`: it is **clamped,
   not refused**, while the cap stays at or above `min_order_sol`; below that the entry is refused with
   `PORTFOLIO_RISK_CAP` (the record carries `size_sol`, `open_risk`, `limit`). `equity` is the portfolio equity at the entry
   decision, before the new fill's costs, so the book can sit a few ten-thousandths of a SOL over `X * equity_after`; the next
   entry then sees a negative room and is refused.
2. **Loss-streak cooldown.** The engine's existing `loss_streak` counts consecutive *fully closed* losing positions (a win
   resets it; partial take-profit sells do not count). **Liquidation closes count like any other full close** (decision,
   tested): the portfolio cooldown follows the engine's own `loss_streak`, which has always included `LIQUIDATE` exits
   (and already halves size at 4 and pauses at 6). A forced exit at a loss therefore extends the streak and can start the
   cooldown; a forced exit at a profit resets it. This is also the fail-closed choice, since it can only delay entries. After a
   liquidation the mode is `STOPPED` until an operator `RESUME`; the cooldown then still applies from the liquidation time. When an exit leaves `loss_streak >= N`, the state records
   `portfolio_cooldown_until = exit_ts + M minutes` (it only ever moves later). Entries before that time are refused with
   `PORTFOLIO_LOSS_COOLDOWN`; `ts == until` is allowed. Every further losing close at or above `N` renews it from that exit.
   Exits, marks and the monitoring of open positions are not affected.
3. **Entry spacing.** At most `K` new entries per rolling 600 seconds (`0 <= ts - entry_ts < 600` counts), refused with
   `PORTFOLIO_ENTRY_SPACING`. The state keeps the last `K` entry timestamps in `portfolio_entries`; refused entries
   consume nothing.
4. **Daily caps** (pause at `daily_pause_fraction`, liquidation at `daily_liquidate_fraction`) are unchanged and apply to the
   whole portfolio as before.

Reject records list every failing reason in `reasons`; `reason` is the first one, and the portfolio reasons are appended
after the existing ones, so an existing gate always wins the label. The two size-based rejects that follow the gates carry
the same audit fields as a gate reject (`reasons`, `scores`, `bundle_audit`, and `entry_policy` when a policy is selected):
`PORTFOLIO_RISK_CAP` additionally has `size_sol`, `open_risk`, `limit`; `BELOW_MINIMUM` has `size_sol`, `limits`. The
`BELOW_MINIMUM` audit fields are added **only with the flag on**: that record exists in every historical journal and the
checkpoint reader replays journals, so the default-off record is unchanged byte for byte.

## State and determinism

Two optional keys are added to the engine state, only when the flag is on and only when needed:
`portfolio_entries` (strictly increasing entry timestamps, at most `K`) and `portfolio_cooldown_until` (an integer). Both
are derived from event timestamps alone: the engine never reads a wall clock, so replay from the journal gives the same
state, and a cold restart (state loaded from the ledger) keeps the streak, the cooldown and the spacing window (tested).

### Checkpoint validation (`Ledger._portfolio_state`)

Every checkpoint read (each `Ledger.apply`, `Ledger.report`) validates the two keys against the ledger's saved config and the
rest of the checkpoint; a violation is the standard `ledger checkpoint invalid: recovery required` error, never a reset, a
rewrite or a bare `TypeError`:

* flag off: neither key may exist;
* `portfolio_entries`, when present, is a non-empty list of at most `K` non-negative ints, strictly increasing, none after
  `last_ts`, and its last element is the engine's last entry (`last // 60 == last_entry_minute`);
* `portfolio_cooldown_until`, when present, is a positive int not beyond `last_ts + M minutes`;
* a **missing** key is an error whenever the checkpoint proves it must exist: `portfolio_entries` once any entry was made
  (`last_entry_minute >= 0`), `portfolio_cooldown_until` once `loss_streak >= N`.

Limitation: the engine writes the keys lazily, so before the first entry (or after a win cleared the streak while a cooldown
was running) an absent key is indistinguishable from a deleted one. Making the guarantee total needs the engine to seed both
keys in `initial_state` when the flag is on, a state-shape change left to a coordinator decision.

## Pre-check without spending requests

`engine.portfolio_blockers(state, cfg, ts)` is pure (no I/O, no mutation) and returns the portfolio reasons a new entry at
`ts` would hit (`PORTFOLIO_LOSS_COOLDOWN`, `PORTFOLIO_ENTRY_SPACING`, `PORTFOLIO_RISK_CAP`). The entry paths
(`desk.paper_concurrency.entry_blockers`, the dispatcher preflight) could call it to skip a doomed investigation before any
charged request. That wiring touches T16's files and is **not done here**; the engine remains the only authority.

## Choosing values

The first-cycle config (4 positions, 2% position size, 8% exposure, 18% stop) risks about 0.015 SOL per entry on 5 SOL, i.e.
0.3% of equity per position. `X = 0.012` (1.2%) then allows about four full-size positions; a tighter `X = 0.006` allows two
full positions plus a clamped third. `N = 3` / `M = 30` pauses entries for half an hour after three losers in a row (the
engine already halves size at 4 and pauses at 6); `K = 2` per 10 minutes stops a pump burst from filling the book in one
minute. These are suggestions, not measured values: choose them for the experiment and freeze them in the config version.
