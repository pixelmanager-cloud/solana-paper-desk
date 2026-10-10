# Concurrent paper positions (`paper_concurrent_entries_version: 1`)

Paper only. No signing, broadcasting or real funds. Fills stay `EXECUTION_UNVERIFIED`.

## What it is

An explicit, versioned experiment config. With the key **absent** every path behaves exactly as before:
any open position, or any mode other than `RUNNING`, refuses new entries (`HELD_POSITION_PRIORITY`), and the
engine's price TTL (10 s) governs everything.

With `"paper_concurrent_entries_version": 1`, `max_positions` an integer in 2..8 **and**
`"paper_portfolio_mark_ttl_seconds"` (an integer from `price_ttl_seconds` to 120), entries may start while
positions are open, subject to the gates below. Any other value, the flag without the TTL key, or the TTL key
without the flag raises `ValueError` (fail closed). The flag changes the config hash, so it is a **new
ledger/config version** (fresh-start policy): never flip it on an existing ledger.

Code: `desk/paper_concurrency.py` (pure rules), `desk/engine.py::portfolio_ttl` (the one engine change),
`tools/paper_scheduler.py` (held-first tick, bounded lease wait), `tools/paper_entry_dispatcher.py` and
`tools/history_first_paper_entry.py` (gate call sites), `desk/paper_monitor_service.py` (per-position legs),
`desk/paper_cycle.py` (one gate, below).

## Why a portfolio mark TTL exists (read this before activating)

`engine.transition` rejects an entry when **any held mark is older than the TTL at the entry decision**
(`STALE_PORTFOLIO`), and `DAY_ROLLOVER` is deferred unless every mark is within the TTL. With the price TTL
of 10 s this can never be satisfied with real provider pacing:

* An entry is history preparation (up to 18 s) + a bounded cycle (10 s deadline plus a 2 s sleep) before the
  engine decides: `ENTRY_SECONDS = 30`. Even a mark refreshed a moment ago is ~30 s old at the decision.
* N held positions need N sequential legs of ~7.8 s each (5 requests per leg at 2 s provider pacing), so the
  oldest of N marks is another `(N-1) x 7.8 s` older.
* The candidate's own quote and history must stay fresh for the same decision, so inserting the leg refreshes
  *between* the candidate's reads and the decision (the "refresh everything last" reorder) ages the candidate's
  evidence instead: with two legs the quote is 15.6 s old against its 10 s TTL.

`tests/test_paper_concurrency.py::test_timeline_arithmetic_for_every_book_size` asserts the numbers for every
book size: the oldest mark at an entry decision is 38 s (1 held) to 85 s (7 held), always above 10 s and below 120 s.

Fix (versioned and opt-in): `paper_portfolio_mark_ttl_seconds` replaces `price_ttl_seconds` in exactly two
engine checks, the entry-time `STALE_PORTFOLIO` gate and the day-rollover "all marks current" test.
Everything else keeps the 10 s price TTL: the candidate's own price/quote/mint evidence, exit decisions,
quote-exit validation and the stale-mark watchdog. Held legs still refresh every mark each tick.

Risk of the relaxed rule (stated, not hidden): at an entry the engine computes equity and drawdown from marks up
to 120 s old. Each position is at most 2 % of equity (`max_position_fraction`) and total exposure is capped at 8 %
(`max_exposure_fraction`), so even if every held token went to zero inside the window equity is overstated by at
most 8 % of equity at that one decision; the daily pause/liquidation then trips on the next fresh mark. Exits are
unaffected. The coordinator decides whether that is acceptable for the experiment; the default is off.

## Scheduling model

1. **Held first, always.** In entry mode with the flag on, the scheduler tick (under the existing lease)
   runs the held legs for **every** open position before any entry work, oldest mark first. Its stdout is
   captured. If the held pass fails the tick ends as `HELD_MONITORING_DEGRADED` with 0 requests and no entry.
2. **Ledger gates** (`state_blockers`, no I/O): mode must be `RUNNING`; fewer than `max_positions`
   positions; no position with `exit_blocked` or `mark_status == UNVERIFIED_EXIT`. Failing any ends the
   tick as `CONCURRENT_ENTRY_BLOCKED`; the held pass already ran, so an unresolved exit keeps being retried.
3. **Dispatcher preflight, before the irreversible intent only** (no intent, admission or request yet) adds the
   monitoring-reserve and mark-freshness estimate below. Later preflights (after the intent, when charges exist)
   skip the estimate on purpose: a refusal there would leave an unresolved dispatch intent (a permanent latch),
   and the engine remains the authority.
4. **Engine gates stay authoritative.** Nothing bypasses `engine.transition`: `max_positions`, exposure 0.08,
   position fraction 0.02, per-mint duplicate, cooldowns, entry throttle, daily pause/liquidation and
   `STALE_PORTFOLIO` (now against the portfolio TTL) still apply inside the ledger transaction. If the pre-I/O
   estimate is wrong, the entry becomes an ordinary terminal engine rejection, never a latch (tested).
5. **`paper_cycle` per-item held-mint gate.** With the flag, supplied position targets need only be a
   duplicate-free subset of the open positions; flag off keeps the old equality rule. An entry candidate whose
   mint is already held is still skipped. This is the only edit to `desk/paper_cycle.py`.
6. **Held legs** (`desk.paper_monitor_service`, flag on): one ordinary positions-only monitoring cycle per
   position (unresolved exits first, then least recently marked, ties by entry time and mint; deterministic from
   the ledger, so a cold restart resumes the same order). **By default every open position runs in the pass**;
   `--wall-seconds N` caps the legs to `floor(N / 7.8)` (at least 1) and defers the rest, never drops them.
   The fresh held unit passes `--wall-seconds 64` (8 legs = 62.4 s) inside `TimeoutStartSec=120`.

## Do entries delay exits? (corrected)

They can, by design, and the earlier claim "exits are never starved by entries" was false. An entry holds the
scheduler lease and the research/evidence/ledger locks for its whole duration (typically 35 to 60 s of
preparation and cycle, code worst case ~282 s). A held pass cannot run inside an entry, and splitting the
scheduler lease would not help because `run_once` itself takes the worker, evidence and ledger locks without waiting.

What is guaranteed instead:

* The entry tick always runs the held legs **first**, so entering never starts from older marks than a held
  tick would have produced.
* A held tick that arrives while an entry runs **waits up to 40 s for the lease** (polling every 0.5 s) and runs
  immediately when the entry finishes, instead of being skipped to the next timer tick
  (`HeldLeaseWaitTests`: real flock, real wrapper, latency measured: starts within one poll of the release). The
  wait applies only to a valid concurrent-entries config; flag off is byte-identical (`SCHEDULER_BUSY`, no wait).
* **Held checkpoints between entry phases (T16G).** The entry phases (acquisition, intake, and the preparation +
  cycle that ends in the engine decision) release the research/evidence/ledger locks between them. Before each phase
  the dispatcher asks `paper_concurrency.checkpoint_due(elapsed_since_held, next_phase_seconds, positions)`: when the
  next uninterruptible phase plus the legs themselves (7.8 s per position) would stretch the gap since the last held
  legs past `HELD_MAX_GAP_SECONDS` (120 s, the held cadence), it runs one in-process held pass (every open position,
  oldest mark first) before that phase. Flat books and flag-off runs never checkpoint. The pass never raises: the
  entry intent is already latched, and a degraded pass shows up as stale marks that the engine's `STALE_PORTFOLIO`
  gate rejects. Budgeted phases: acquisition 12 s, intake 10 s, preparation 18 s, cycle 12 s = `ENTRY_SECONDS` 52 s
  (conservative planning figures, not measurements). With those figures no extra leg is needed; a slow provider (pacing
  waits stretching acquisition to 70 s and intake to 40 s) triggers exactly one checkpoint leg before the cycle
  (`tests/test_paper_concurrency_held_latency.py`; the same entry without the checkpoint leaves a 140 s gap).
* So the worst-case gap between two refreshes of a position is now bounded by `HELD_MAX_GAP_SECONDS` plus the longest
  single phase that the pre-phase estimate could not foresee (the cycle phase itself, 30 s, is uninterruptible).
  A held *timer* tick that finds the lease taken still waits up to 40 s and otherwise reports `SCHEDULER_BUSY`; the
  entry's own checkpoints now cover that case. Budget: 40 s wait + 62.4 s legs fits `TimeoutStartSec=120`.

## Budget math

Monitoring allowance (`paper_monitoring_budget`, window 3600 s) is spent only by held legs, 5 requests per
position per leg. Investigation requests (admission ceiling 18, shared pacing) are separate stores, so entries
cannot drain monitoring and monitoring cannot drain investigations; they share the pacing clock, which the lease
serialises.

An entry is refused unless the remaining window allowance covers one hour of five-minute held passes for every
position **including the new one**: `required = 5 * (open + 1) * 12`.

| open before entry | required remaining |
|---|---|
| 1 | 120 |
| 3 | 240 |
| 7 | 480 |

The legacy cap is 60 per hour (cannot carry a second position); the reviewed 3600/hour allowance (which
`tools.ops.fresh_start` provisions) is a prerequisite.

## Wall time (fixture timings, not live)

BUY 7.006 s and SELL 7.807 s on the native-clock Kraken fixture: a held leg is budgeted at 7.8 s, an entry at 52 s
(12 s acquisition + 10 s intake + 18 s preparation, asserted equal to `PREPARATION_SECONDS`, + 12 s cycle). The
acquisition and intake figures are conservative planning numbers (intake showed a 7.451 s local delay in the
2026-10-10 native-clock regression); the older 30 s figure left both out.

| unit (`deploy/fresh`) | budget |
|---|---|
| held (`TimeoutStartSec=120`) | lease wait 40 s + `--wall-seconds 64` (8 legs) + margin |
| entry (`TimeoutStartSec=600`) | held legs for the whole book (62 s) + entry (30 s typical, ~282 s worst case) |

## Day rollover (corrected)

Rollover needs every mark current at the evaluating event. During pure held passes it is deferred for any book
size (each leg's own mark is one cadence old when its event is evaluated; this already held with one position).
It fires at the **first entry decision after midnight** because the held-first legs have just refreshed every mark
and the portfolio TTL covers them; the entry gates run after the rollover, so the daily counters are correct
exactly when they are used (`RolloverTests`, three positions across UTC midnight; without the TTL the same book
stays on yesterday and the entry is rejected `STALE_PORTFOLIO`). If no entry is attempted for days while
positions are held, the counters stay stale, which affects only entry gates, never exits.

## Failure modes (all fail closed)

| Situation | Result |
|---|---|
| held pass nonzero / monitoring blocked | `HELD_MONITORING_DEGRADED`, 0 requests |
| unresolved exit, `EXIT_ONLY`/paused mode, full book | `CONCURRENT_ENTRY_BLOCKED` + reasons, still monitoring |
| monitoring allowance below reserve | preflight `MONITORING_RESERVE_INSUFFICIENT`, before the intent |
| marks too old for the portfolio TTL (incl. 30 s of entry work) | preflight `PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY`, before the intent, 0 requests, no latch |
| estimate wrong, engine sees stale marks | terminal engine reject `STALE_PORTFOLIO` (charges kept, store not latched) |
| exported targets differ from checkpoint positions | held service `UNAVAILABLE`, no leg |
| invalid flag / `max_positions` / TTL | `ValueError` |
| entry still running when the held wait expires | `SCHEDULER_BUSY`; the next tick follows |

Restart: ledger events are idempotent by `event_id`. `tests/test_paper_concurrency_pipeline.py` runs two real
dispatcher entries on `fresh_start` stores and uses `tools.ops.verify_cycle` snapshots: a cold restart between and
after the entries changes no fill, charge, reservation or journal row; the second entry is append-only progress.

### Rollover after the mark (T16G, `paper_rollover_after_mark_version: 1`)

Versioned, opt-in, valid only with the concurrent-entries flag and the portfolio TTL (anything else refuses at load).
With it, a held-position event that has just refreshed its own mark (or exited) re-tests the rollover against the
updated book: if every remaining mark is within the portfolio TTL the UTC day rolls over inside that event
(`DAY_ROLLOVER_AFTER_FRESH_MARKS`, `day_start_equity` from the refreshed book, `day_gross_losses` reset). The held
pass's final leg therefore rolls the day even with a full book, no candidate, or `EXIT_ONLY` mode (`RolloverAfterMarkTests`).
A single failed or skipped leg leaves its stale mark in place and the rollover stays deferred (fail closed). Key
absent: the old behaviour, byte for byte.

## Recommended activation

1. Verify the first single-position cycle (entry, hold, exit, accounting, restart) with the flag off.
2. New ledger/config version with the flag, `"max_positions": 2` and `"paper_portfolio_mark_ttl_seconds": 120`
   (timeline arithmetic admits up to 8, so raise to 4 after one forward observation period).
   Leave `paper_portfolio_mark_ttl_seconds` out of every example config until the coordinator decides on it; add
   `"paper_rollover_after_mark_version": 1` with it if daily counters must stay correct while 2+ positions are held.
3. Monitoring allowance 3600/hour (provisioned by `fresh_start`); install the `deploy/fresh` held unit as shipped.
4. Keep `desk-paper-monitor.timer` off: the stale-mark watchdog uses the 10 s price TTL and would put the book in
   `EXIT_ONLY` (T09 F5/T23).

## Not done / limitations

* No live measurement; all timings are fixture figures and the "elapsed tick" in the pipeline tests is modelled by
  advancing the fixture clock (the stores, dispatcher, pre-I/O estimate, cycles and engine are real).
* The read-only dashboard/ledger-reader per-position view was not changed; `desk.paper_concurrency.attribution`
  provides the read-only per-position fill attribution and the portfolio cash identity (tested after an interleaved
  buy/buy/sell/buy/sell/sell sequence).
* The relaxed portfolio TTL is a risk decision; see the bound above.
* `desk/` changed, so the runtime implementation hash changes; the coordinator rotates the ledger.
