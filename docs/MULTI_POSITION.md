# Concurrent paper positions (`paper_concurrent_entries_version: 1`)

Paper only. No signing, broadcasting or real funds. Fills stay `EXECUTION_UNVERIFIED`.

## What it is

An explicit, versioned experiment config. With the key **absent** every path behaves exactly as before:
any open position, or any mode other than `RUNNING`, refuses new entries (`HELD_POSITION_PRIORITY`), and the
engine's price TTL (10 s) governs everything.

With `"paper_concurrent_entries_version": 1` and `max_positions` an integer in 2..8, entries may start while
positions are open, subject to the gates below. Any other value raises `ValueError` (fail closed). The flag does NOT
require `paper_portfolio_mark_ttl_seconds` (T16H): the coordinator rejected relaxing the held-mark TTL, the 10 s price TTL
applies, and with N positions it is met by the batched portfolio marks (below). Set
`paper_portfolio_mark_source_version` with the flag; without it, any entry while a position is open is refused
before any charge (`PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY`) because the 10 s rule cannot be met by sequential legs. The flag changes the config hash, so it is a **new
ledger/config version** (fresh-start policy): never flip it on an existing ledger.

Code: `desk/paper_concurrency.py` (pure rules), `desk/engine.py::portfolio_ttl` (the one engine change),
`tools/paper_scheduler.py` (held-first tick, bounded lease wait), `tools/paper_entry_dispatcher.py` and
`tools/history_first_paper_entry.py` (gate call sites), `desk/paper_monitor_service.py` (per-position legs),
`desk/paper_cycle.py` (one gate, below).

## Why the 10 s rule needs batched marks (read this before activating)

`engine.transition` rejects an entry when **any held mark is older than the TTL at the entry decision**
(`STALE_PORTFOLIO`), and `DAY_ROLLOVER` is deferred unless every mark is within the TTL. With the price TTL
of 10 s this can never be satisfied with real provider pacing:

* An entry is history preparation (up to 18 s) + a bounded cycle (10 s deadline plus a 2 s sleep) before the
  engine decides (the budgeted `ENTRY_SECONDS` is now 52 s: acquisition 12, intake 10, preparation 18, cycle 12). Even a mark
  refreshed a moment ago is that old at the decision.
* N held positions need N sequential legs of ~7.8 s each (5 requests per leg at 2 s provider pacing), so the
  oldest of N marks is another `(N-1) x 7.8 s` older.
* The candidate's own quote and history must stay fresh for the same decision, so inserting the leg refreshes
  *between* the candidate's reads and the decision (the "refresh everything last" reorder) ages the candidate's
  evidence instead: with two legs the quote is 15.6 s old against its 10 s TTL.

`tests/test_paper_concurrency.py::test_timeline_arithmetic_for_every_book_size` asserts the numbers for every
book size: the oldest mark at an entry decision is 38 s (1 held) to 85 s (7 held), always above 10 s and below 120 s.

REJECTED proposal (T16F, kept for the record; do not use): `paper_portfolio_mark_ttl_seconds` would replace `price_ttl_seconds` in exactly two
engine checks, the entry-time `STALE_PORTFOLIO` gate and the day-rollover "all marks current" test.
Everything else keeps the 10 s price TTL: the candidate's own price/quote/mint evidence, exit decisions,
quote-exit validation and the stale-mark watchdog. Held legs still refresh every mark each tick.

Risk of the relaxed rule (stated, not hidden): at an entry the engine computes equity and drawdown from marks up
to 120 s old. Each position is at most 2 % of equity (`max_position_fraction`) and total exposure is capped at 8 %
(`max_exposure_fraction`), so even if every held token went to zero inside the window equity is overstated by at
most 8 % of equity at that one decision; the daily pause/liquidation then trips on the next fresh mark. Exits are
unaffected. The coordinator REJECTED this relaxation (2026-10-11); the batched marks below replace it and no config,
flag or rollover rule depends on the key.

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
   `STALE_PORTFOLIO` (against the 10 s price TTL, met by the batched marks) still apply inside the ledger transaction. If the pre-I/O
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
  single phase that the pre-phase estimate could not foresee (the cycle phase itself, 18 s preparation + 12 s cycle = 30 s, is
  uninterruptible and already capped in code).
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
2026-10-10 native-clock regression); the earlier planning figure left both out.

| unit (`deploy/fresh`) | budget |
|---|---|
| held (`TimeoutStartSec=120`) | lease wait 40 s + `--wall-seconds 64` (8 legs) + margin |
| entry (`TimeoutStartSec=600`) | held legs for the whole book (62 s) + entry (52 s planned, ~282 s code worst case) |

## Day rollover

There is exactly ONE rollover rule (below). It needs every position's valuation mark to be current (<= 10 s) at the
evaluating event. Pure held legs alone cannot satisfy that for 2+ positions (each leg's own mark is a cadence old when its event
is evaluated); the batched marks do: the refresh at the start of a held pass, and the two refreshes of an entry, make every
mark current at once, and the engine rolls the day over there (under the single versioned flag below), in every mode, with a
full book and with no candidate. Without the flag only the pre-existing top-of-event test applies.

### The ONE rollover rule (`paper_rollover_after_mark_version: 1`, T16G/T16H/T35F)

There is exactly one place that rolls the UTC day (`engine._roll_day`: every position's valuation mark must be current,
then `day_start_equity` is re-based on the guarded risk equity and the daily loss counter reset). The versioned flag is
valid only with the concurrent-entries flag (it needs neither the TTL key nor batched marks) and lets two more events
call that rule: a batched `portfolio_marks` event (which previously rolled the day unconditionally) and a held-position
event after its own mark is refreshed. Without the flag only the pre-existing top-of-event test applies. With it, a
held-position event that has just refreshed its own mark (or exited) re-tests the rollover against the
updated book: if every remaining mark is within the 10 s price TTL the UTC day rolls over inside that event
(`DAY_ROLLOVER_AFTER_FRESH_MARKS`, `day_start_equity` from the refreshed book, `day_gross_losses` reset). The held
pass's final leg therefore rolls the day even with a full book, no candidate, or `EXIT_ONLY` mode (`RolloverAfterMarkTests`).
A single failed or skipped leg leaves its stale mark in place and the rollover stays deferred (fail closed). Key
absent: the old behaviour, byte for byte.

## Batched on-chain portfolio marks (T35, recommended: `paper_portfolio_mark_source_version: 2`)

Decision (coordinator, 2026-10-11): do NOT relax the held-mark TTL. With N positions the executable (Jupiter sell quote)
marks cannot all be 10 s old at an entry decision, so the entry path refreshes every position's **portfolio mark** with
ONE `getMultipleAccounts` over the PumpSwap pool and both vaults of every open position (3N accounts, one slot context).

* Config: `paper_portfolio_mark_source_version: 2` (version 1, kept only for comparison, lets a model mark move the peak, the
  day-start equity and the daily liquidation; do not recommend or use it), `paper_portfolio_mark_pool_fee_bps` (decimal string, the pool fee
  hypothesis, config-hash bound), the concurrent-entries flag, quote execution 1, paper mode. `paper_portfolio_mark_ttl_seconds`
  is then NOT required and should stay absent: the 10 s price TTL applies to the marks. Absent flag: byte-identical behaviour.
* Mark: constant-product value of the position's remaining quantity from the same-slot base/quote vault balances, net of
  the pool fee hypothesis, the PumpSwap creator fee and virtual quote reserves (from the pool account), a Token-2022
  transfer fee where the retained mint evidence shows one, the adverse slippage and the fixed fee. Model parity with
  `tools/ops/held_watcher.implied_ratio` is a test. Vault addresses are derived (ATAs of the pool), never taken from the
  response, and verified against the pool account's own fields; a position that fails verification keeps its stale mark.
* Where it runs: (1) in the entry cycle before the engine plans, and again right after the quote reads (so the marks are
  seconds old at the decision); (2) at the start of every held pass, as a positions-less monitoring cycle, before the exit
  legs. Each refresh is one monitoring-allowance request for N positions (charged to the oldest-marked position's entry
  scan), retained as original evidence, and applied as a ledger `portfolio_marks` event. Its own 5 s read bound is excluded
  from the entry cycle's 10 s deadline so a refresh cannot starve the quote reads.
* Use: **valuation only** (equity, exposure/daily checks, `STALE_PORTFOLIO`, day rollover). The marks live in separate
  position keys (`portfolio_mark_value/at/source`); `mark_at`/`mark_value`, stop/trailing/take-profit and every SELL fill
  still come from the executable quote path, and a marks event cannot cure a blocked exit or the stale-mark watchdog.
  A partial sale drops the portfolio mark of the larger remainder.
* Failure: a failed read is charged and leaves marks stale, so the entry ends as an ordinary `STALE_PORTFOLIO` reject, never
  a latch. Caveat (existing monitoring design): a non-transient transport failure code (for example `RESPONSE_INVALID`,
  `UNCLASSIFIED_ERROR`) latches the monitoring allowance for every read; connection resets, timeouts and 408/429/5xx do not.
* Cost: 2 requests per entry with positions, 1 per held pass.

## Batched marks: fixes from the review (T35F)

* **Source version 2** (`paper_portfolio_mark_source_version: 2`, optional `paper_portfolio_mark_max_divergence`, default
  "0.03"): model marks still feed exposure, position limits, sizing and every freshness gate, but `peak_equity`,
  `day_start_equity` and the daily drawdown/pause/liquidation use the executable mark unless the model mark is within
  the bound of it. Version 1 keeps the model mark in every valuation (unchanged).
* **No latch from a valuation-only read.** The 3N-key marks read is exempt from the monitoring `SOURCE_FAILURE` latch
  whatever its failure class (`RESPONSE_INVALID`, HTTP 4xx, TLS, unclassified): the attempt stays charged with its typed
  `failure_code`, the marks stay stale and the entry ends as an ordinary `STALE_PORTFOLIO` reject, while the held legs'
  own exit-protecting reads (7-key pool snapshots, quotes) keep their latch. (A 429 still arms the provider's pacing backoff.)
* **Deadline.** The read happens inside the 10 s cycle budget, bounded by what is left (at most 5 s; skipped with
  `PORTFOLIO_MARKS_DEADLINE` when under 3 s remain) and never extends the deadline.
* **Closed vault/pool.** A null vault or pool account in one answer is only an OBSERVATION (`VAULT_NULL`/`POOL_NULL`: commitment lag
  and provider gaps happen). After two consecutive distinct observations the engine values the position at zero with the explicit
  reason `VAULT_CLOSED`/`POOL_CLOSED` (stored as `portfolio_mark_reason`); an ordinary answer in between resets the count.
* **Estimates.** `plan_legs` and the checkpoint rule count the pass's refresh (2.5 s) against `--wall-seconds` / the held gap. (The
  unused `entry_seconds(cfg)` helper was removed: with batched marks the refreshes happen inside the cycle, so no pre-I/O age estimate applies.)
* **Reserve math (T16I).** Held passes spend the allowance from EVERY source, so the reserve is derived from their bounds, not
  from the timer alone: timer 29/hour (`OnUnitInactiveSec=120` + a leg: ceil(3600 / 127.8)), the held-watcher `.path`
  trigger 60 price + 30 time triggers/hour (its `--max-triggers-hour` / `--max-time-triggers-hour` defaults, pinned by a test),
  dispatcher checkpoints 12 dispatches/hour (enforced from the journal before the intent) x 3 = 36: `RESERVE_PASSES` = 155.
  Required remaining allowance = 155 x (5 N + 1) + 2: N=4 -> 3257, N=5 -> 4032 > 3600, so with every source at its bound the
  approved 3600/hour carries at most 4 positions. A store still on the legacy 60/hour allowance is refused with the explicit
  reason `MONITORING_ALLOWANCE_LEGACY_60` (and `MONITORING_RESERVE_INSUFFICIENT` otherwise), before any intent.

## Checkpoint results and phase caps (T16H)

* A post-intent checkpoint that comes back non-zero, moves the mode off `RUNNING`, leaves an exit unresolved, or
  leaves monitoring blocked ends the intent as a typed terminal result `dispatcher_checkpoint_no_entry_v1` in the
  dispatcher journal (reasons, the held pass's exit code, the mode, the phase). The investigation charges stay charged,
  nothing is retried, the next preflight validates the journal normally, and held monitoring proceeds on its own.
  The dispatch result surfaces it as `checkpoint_no_entry` (previously the next preflight raised and the intent was
  orphaned: "Unresolved dispatch; no retry").
* Acquisition and intake have enforced wall caps (`PHASE_CAPS`: 30 s and 25 s, 2.5x the planning figures): no request of
  an over-cap phase starts, and an overrun (cut, or finished late) ends the intent as the same typed result with reason
  `PHASE_CAP_EXCEEDED`. Preparation and the cycle are already capped in code (18 s and the 10 s cycle deadline).

## Recommended activation

1. Verify the first single-position cycle (entry, hold, exit, accounting, restart) with the flag off.
2. New ledger/config version with the flag, `"max_positions": 2`, `"paper_portfolio_mark_source_version": 2` and
   `"paper_portfolio_mark_pool_fee_bps"` (4 is the most the reserve math carries; raise only after one forward observation period). Never set
   `paper_portfolio_mark_ttl_seconds`. The batched marks roll the day over at the start of a held pass or checkpoint
   whenever every position's mark is fresh; `"paper_rollover_after_mark_version": 1` additionally lets a single
   fresh leg do it when the other marks are within 10 s.
3. Monitoring allowance 3600/hour (provisioned by `fresh_start`); install the `deploy/fresh` held unit as shipped.
4. Keep `desk-paper-monitor.timer` off: the stale-mark watchdog uses the 10 s price TTL and would put the book in
   `EXIT_ONLY` (T09 F5/T23).

## Not done / limitations

* No live measurement; all timings are fixture figures and the "elapsed tick" in the pipeline tests is modelled by
  advancing the fixture clock (the stores, dispatcher, pre-I/O estimate, cycles and engine are real).
* The read-only dashboard/ledger-reader per-position view was not changed; `desk.paper_concurrency.attribution`
  provides the read-only per-position fill attribution and the portfolio cash identity (tested after an interleaved
  buy/buy/sell/buy/sell/sell sequence).
* The relaxed portfolio TTL was rejected by the coordinator (see the REJECTED note above); nothing depends on it.
* `desk/` changed, so the runtime implementation hash changes; the coordinator rotates the ledger.
