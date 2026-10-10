# Concurrent paper positions (`paper_concurrent_entries_version: 1`)

Paper only. No signing, broadcasting or real funds. Fills stay `EXECUTION_UNVERIFIED`.

## What it is

An explicit, versioned config option. With the key **absent** every path behaves exactly as before:
any open position, or any mode other than `RUNNING`, refuses new entries (`HELD_POSITION_PRIORITY`).
With `"paper_concurrent_entries_version": 1` and `max_positions` an integer in 2..8, entries may start
while positions are open, subject to the gates below. Any other value, or the key with `max_positions`
outside 2..8, raises `ValueError` (fail closed). It changes the config hash, so it is a **new
ledger/config version** (fresh-start policy); never flip it on an existing ledger.

Code: `desk/paper_concurrency.py` (pure rules), `tools/paper_scheduler.py` (held-first tick),
`tools/paper_entry_dispatcher.py` and `tools/history_first_paper_entry.py` (gate call sites),
`desk/paper_monitor_service.py` (per-position legs), `desk/paper_cycle.py` (one gate, below).

## Scheduling model

1. **Held first, always.** In entry mode with the flag on, the scheduler tick (under the existing
   lease, so nothing can interleave) runs the held pass for every open position *before* any entry
   work. Its stdout is captured, not mixed into the tick output. If the held pass fails the tick
   ends as `HELD_MONITORING_DEGRADED` with 0 requests and no entry.
2. **Ledger gates** (`state_blockers`, no I/O): mode must be `RUNNING`; fewer than `max_positions`
   positions; no position with `exit_blocked` or `mark_status == UNVERIFIED_EXIT`. Failing any of
   them ends the tick as `CONCURRENT_ENTRY_BLOCKED` with the reasons. The held pass already ran, so
   an unresolved exit keeps being retried while it freezes entries.
3. **Dispatcher preflight** (before the irreversible intent latch and before any provider I/O) adds
   the monitoring-reserve and mark-freshness checks below, then the unchanged terminal/observation,
   pacing and budget gates.
4. **Engine gates stay authoritative.** Nothing bypasses `engine.transition`: `max_positions`,
   exposure 0.08, position fraction 0.02, per-mint duplicate, cooldowns, entry throttle, daily
   pause/liquidation and `STALE_PORTFOLIO` still apply inside the ledger transaction.
5. **`paper_cycle` per-item held-mint gate.** The cycle used to require a target for *every* open
   position in every pass (`ALL_OPEN_POSITION_TARGETS_REQUIRED`). With the flag, supplied position
   targets need only be a duplicate-free subset of the open positions (so one leg monitors one
   position); with the flag off the old equality rule is unchanged. An entry candidate whose mint is
   already held is still skipped (`ENTRY_CONTROL_OR_EXISTING_POSITION`). This is the only edit to
   `desk/paper_cycle.py`.
6. **Held legs.** `desk.paper_monitor_service` with the flag runs one ordinary positions-only
   monitoring cycle per position (unresolved exits first, then least recently marked, ties by
   entry time and mint; deterministic from the ledger so a cold restart resumes the same order).
   `--wall-seconds` (default 12) chooses how many legs run (`floor(wall / 7.8)`, at least 1); the
   rest are deferred, never dropped, and a failed leg does not hide the others (exit code is the
   worst leg).

## Budget math

Monitoring allowance (`paper_monitoring_budget`, window 3600 s) is spent only by held legs, 5
requests per position per leg (the figure `paper_monitor_operator.preflight` already uses).
Investigation requests (admission ceiling 18, shared pacing) are separate stores, so entries cannot
drain monitoring and monitoring cannot drain investigations; they only share the pacing clock,
which the lease serialises.

An entry is refused unless the remaining window allowance covers one hour of five-minute held
passes for every position **including the new one**:

    required = LEG_REQUESTS * (open + 1) * RESERVE_PASSES = 5 * (open + 1) * 12

| open before entry | required remaining |
|---|---|
| 1 | 120 |
| 3 | 240 |
| 7 | 480 |

The legacy cap is **60 per hour**, which cannot carry a second position (and one position at 5-minute
passes already consumes 60). The reviewed 3600/hour allowance is a prerequisite. At 4 positions held
every 60 s: 5 x 4 x 60 = 1200/hour, inside 3600 with the reserve intact.

## Wall time (measured fixture timings, not live)

Native-clock Kraken fixture (`docs/readiness.md`): BUY 7.006 s, SELL 7.807 s, so a held leg is
budgeted at 7.8 s and an entry at 8.0 s.

* Held pass for 4 positions = 31.2 s. Inside the **entry** unit (`TimeoutStartSec=180` in this
  repository's `deploy/desk-paper-entry-dispatcher.service`; the task text said 600 s) with 31.2 + 8
  = 39.2 s: fits (asserted in `test_wall_time_*`).
* The **held** unit has `TimeoutStartSec=20` (task text said 120 s). 4 x 7.8 s does not fit, so the
  work is split into legs; at the default 12 s budget one leg runs per pass. To service N positions per
  pass the coordinator must raise `TimeoutStartSec` and `--wall-seconds` together, e.g.
  `TimeoutStartSec=60`, `--wall-seconds 48` (6 legs) and shorten the timer to 60 s. The unit files are
  not changed here (outside this task's files).

## The binding constraint: `STALE_PORTFOLIO` (read this before activating)

`engine.transition` rejects an entry when **any held mark is older than `price_ttl_seconds` (10 s)**
at the entry event time (`test_engine_rejects_entry_when_a_held_mark_is_stale` shows it). A held leg
takes ~7.8 s and the entry decision ~8 s after it starts, so the marks must be refreshed immediately
before the entry and the whole batch decided within 10 s:

    oldest-mark age at decision ~ (time since its leg) + ENTRY_SECONDS(8) <= 10

* 1 open position, held pass just finished: ~0 + 8 = 8 s, allowed (marginal).
* 2 open positions: the older mark is one leg (7.8 s) older, 15.8 s, refused.
* 3-4 open positions: refused.

`entry_blockers` applies this arithmetic *before* any investigation request, so a doomed entry costs
0 requests (`PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY`) instead of ~9 followed by an engine reject. The engine
gate is not weakened. Consequently, **with today's timings the useful setting is `max_positions: 2`
(one held + one new)**. Reaching 4 needs either legs of ~2 s each (fewer or parallel reads per held
leg, which touches the cycle/transport code), or an explicit developer decision to raise
`price_ttl_seconds` (weakening an evidence-freshness gate, deliberately not done here), or an in-cycle
reorder that collects the candidate first and refreshes all marks last. These are listed for the
coordinator; none was implemented.

## Failure modes (all fail closed)

| Situation | Result |
|---|---|
| held pass nonzero / monitoring blocked | `HELD_MONITORING_DEGRADED`, 0 requests |
| unresolved exit, `EXIT_ONLY`/paused mode, full book | `CONCURRENT_ENTRY_BLOCKED` + reasons, still monitoring |
| monitoring allowance below reserve | preflight `MONITORING_RESERVE_INSUFFICIENT` |
| marks too old for the engine TTL | preflight `PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY` |
| exported targets differ from checkpoint positions | held service `UNAVAILABLE`, no leg |
| invalid flag value / `max_positions` outside 2..8 | `ValueError` |
| held pass overruns the lease | the next tick sees `SCHEDULER_BUSY`; exits are never starved by entries |

Restart: ledger events are idempotent by `event_id`; re-delivering every event after a cold restart
with three open positions yields no new fills (tested). Monitoring reservations are durable and
immutable in their own store; the T06 verifier (`tools.ops.verify_cycle`) compares fills and budgets
across a restart.

## Recommended activation

1. Verify the first single-position cycle (entry, hold, exit, accounting, restart) with the flag off.
2. New ledger/config version with `"paper_concurrent_entries_version": 1` and `"max_positions": 2`.
3. Monitoring allowance 3600/hour provisioned; raise the held unit timeout/timer as above.
4. Observe forward results; only then consider 4, after the freshness constraint is addressed.

## Not done / limitations

* The read-only dashboard/ledger-reader per-position view was not changed (those files were outside
  the task's files). `desk.paper_concurrency.attribution(connection, cfg)` provides the read-only
  per-position fill attribution and the portfolio cash identity (`cash = initial + realized - cost
  held`) and is tested after an interleaved buy/buy/sell/buy/sell/sell sequence.
* All timings are the documented fixture figures; no live measurement was possible (no provider
  access from cloud workers).
* `desk/` changed, so the runtime implementation hash changes; the coordinator rotates the ledger.
