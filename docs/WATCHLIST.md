# Watchlist: re-evaluating "not yet" candidates (`paper_watchlist_version: 1`)

Paper only. No signing, broadcasting or real funds; every fill stays `EXECUTION_UNVERIFIED`.
**Decision (CK, 2026-10-11):** for the *fresh store set only*, a candidate rejected for a reason that can
still change is re-evaluated later, instead of being lost. Without the flag every path is unchanged.

## What happens

1. A dispatch ends (BUY, typed no-entry, engine reject). Its completed result is read from the dispatcher
   **journal** (the single source of truth) and classified: `ACCEPTED` (a BUY fill), `PERMANENT`, or `NOT_YET`.
2. `NOT_YET` mints are appended to the watchlist with `next_eval_at = at + backoff` (10 min, then 30 min,
   then 2 h, then 2 h; configurable) up to `paper_watchlist_max_reevaluations` (default 4) re-evaluations.
3. Selection order inside the existing dispatcher budgets: **fresh migration hints first**, then due
   watchlist entries (oldest due first). A due entry is only used when the journal, the research `scans`
   table and the watchlist all agree on its history (any ambiguity: skipped).
4. A re-evaluation is a **new scan** with a **new intent id**, evaluation number n, and its own
   18-request investigation budget. Requests of the original scan stay charged to the original scan; the
   original scan, journal rows, outcome, evidence and counters are never retried, rewritten or refunded.
5. A mint expires when it leaves the engine age window (`received_at + max_age_seconds`, 21600 s) and is
   never re-evaluated after any terminal disposition.
6. **Preparation margin (T20F).** Preparation of a dispatch (acquisition, intake, history) can take minutes, and
   the dispatcher refuses `Candidate expired during preparation` AFTER the intent is written, which strands an
   unresolved intent. So every pick in a watchlist journal must still have **900 s** of its age window left:
   fresh hints are selectable while `300 <= age <= 7200 - 900`, due re-evaluations while
   `age <= 21600 - 900`, and a re-evaluation whose `next_eval_at` would fall inside that margin is recorded
   `EXPIRED` instead of `ENROLLED`.

## Classification (full mapping)

Unlisted means `PERMANENT`: a new engine code is permanent until a reviewer lists it. A result is NOT_YET only
when it has at least one code and **every** code is listed; one permanent or unknown code makes the whole
result permanent, and a rejection that carries no recognisable code is permanent (fail closed).

| Code | Class | Meaning |
|---|---|---|
| `AGE` | NOT_YET | engine age window: too young now (too old ends the entry via expiry) |
| `MARKET_CAP` | NOT_YET | market cap outside the configured range |
| `LIQUIDITY` | NOT_YET | pool liquidity below the minimum |
| `MOMENTUM_OR_WASH` | NOT_YET | momentum not met |
| `MOMENTUM_OR_OBSERVED_CHURN` | NOT_YET | observable momentum/churn not met |
| `ENTRY_SCORE` | NOT_YET | entry/flow score below threshold |
| `MARKET_PRODUCER_BLOCKED` | NOT_YET | market event not producible from the captured window |
| `HISTORY_FEATURE_EMPTY_WINDOW` | NOT_YET | empty five-minute trade window |
| `HISTORY_REQUIRED_MEASUREMENTS_UNAVAILABLE` | NOT_YET | insufficient history for required measurements |
| `HISTORY_FEATURE_MOMENTUM_STALE` | NOT_YET | captured momentum too old |
| `MISSING_WINDOW_MEASUREMENT:<name>` | NOT_YET | producer: a required window measurement is empty right now (`<name>` = `[a-z][a-z0-9_]{0,63}`) |
| `STALE_WINDOW_MEASUREMENT:<name>` | NOT_YET | producer: a window measurement is older than 30 s |

### Checkpoint results (`dispatcher_checkpoint_no_entry_v1`, T16I/T16J)

When the HELD side cuts an entry attempt after its dispatch intent (a held pass failed, the book moved, monitoring or a lock was
busy, the provider is pacing, or a phase overran its wall cap) the dispatcher publishes a typed terminal no-entry. Nothing is wrong
with the candidate, so these reasons are `NOT_YET`, but ONLY as reasons of that result kind: `watchlist.reason_codes` hands them
out as `CHECKPOINT:<reason>`, and the same words anywhere else (an engine reject, a cycle blocker, a string that merely starts with
`CHECKPOINT:` in another result kind) are not in the table above and stay `PERMANENT`.

| Code (as `CHECKPOINT:<reason>`) | Class | Meaning |
|---|---|---|
| `HELD_PASS_NONZERO` | NOT_YET | a checkpoint held pass exited non-zero |
| `LEDGER_MODE_NOT_RUNNING` | NOT_YET | a checkpoint left the ledger not RUNNING |
| `HELD_EXIT_UNRESOLVED` | NOT_YET | a checkpoint left a held exit unresolved |
| `MAX_POSITIONS_REACHED` | NOT_YET | the book filled during the attempt |
| `MONITORING_BLOCKED` | NOT_YET | monitoring was blocked at a checkpoint |
| `PHASE_CAP_EXCEEDED` | NOT_YET | an entry phase overran its wall cap |
| `CHECKPOINT_STATE_UNAVAILABLE` | NOT_YET | a non-blocking lock was busy (also the held guard's "ledger busy" refusal) |
| `MONITORING_RESERVE_INSUFFICIENT` | NOT_YET | the monitoring allowance cannot carry another position yet |
| `PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY` | NOT_YET | the held marks cannot be refreshed in time |
| `HELD_POSITION_PRIORITY` | NOT_YET | a held position has priority (also the held guard's mid-phase refusal) |
| `PACING_BACKOFF_EXCEEDS_PHASE_BUDGET` | NOT_YET | the provider is backing off longer than the phase may wait |
| `PROVIDER_PACING_PENDING` | NOT_YET | another process holds or awaits the shared pacer after the intent |
| every other producer blocker (`COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID`, `CAPTURED_HISTORY_WINDOW_STALE_OR_FUTURE`, `BOUNDED_RAW_TRADE_SEQUENCE_REQUIRED`, `SOL_USD_SOURCE_OR_EXACT_BLOCK_TIME_MISSING`, `USD_ORIGINAL_BINDING_INVALID`, ...) | PERMANENT | integrity/binding/identity: not a market condition |
| `DANGER` | PERMANENT | rug/control hazard |
| `UNVERIFIED_SAFETY` | PERMANENT | safety flags not verified |
| `UNSUPPORTED_POOL` | PERMANENT | unsupported venue or not graduated |
| `KNOWN_OWNERSHIP_HAZARD` | PERMANENT | measured ownership hazard |
| `SAFETY` | PERMANENT | safety score below threshold |
| `REPEAT_DEPLOYER` | PERMANENT | repeat-deployer hazard |
| `DATA_UNHEALTHY` | PERMANENT | data unhealthy |
| `NO_ROUTE` | PERMANENT | no route |
| `ACTIVE_MINT_AUTHORITY` | PERMANENT | token policy: active mint authority |
| `ACTIVE_FREEZE_AUTHORITY` | PERMANENT | token policy: active freeze authority |
| `MAYHEM_POOL` | PERMANENT | known hazardous pool class |
| `LIVE_FEATURE_ADAPTER_NOT_READY` | PERMANENT | adapter not ready |
| `HISTORY_FEATURE_RECORD_BYTES_EXCEEDED` | PERMANENT | resource bound exceeded |
| `HISTORY_FEATURE_RECORD_COUNT_EXCEEDED` | PERMANENT | resource bound exceeded |
| `HISTORY_FEATURE_AGGREGATE_BYTES_EXCEEDED` | PERMANENT | resource bound exceeded |
| `HISTORY_FRESH_ENTRY_REQUESTS_UNAVAILABLE` | PERMANENT | request budget cannot cover the work |
| `EXACT_FRESH_ROUNDTRIP_QUOTES_REQUIRED` | PERMANENT | quote evidence missing |
| anything else (corrupt/contradictory evidence, Token-2022 extension hazards, known-hazard list) | PERMANENT | default |

**Whose codes count (T20F).** A cycle result carries the engine outcomes and diagnostics of every item in the pass.
`reason_codes(result, mint=..., scan_id=...)` keeps only this candidate's evidence: an outcome naming another
mint (a held position's reject or fill) and a diagnostic of another scan are ignored, so a held position can
neither taint a candidate with `DANGER` nor make it look `ACCEPTED`. An outcome or diagnostic that names no
mint/scan cannot be attributed and stays in; its code is then unclassified and the result is `PERMANENT`.
T01's empty-window pass (`MARKET_PRODUCER_BLOCKED` plus `MISSING_WINDOW_MEASUREMENT:*` diagnostics) is
therefore `NOT_YET`; before T20F its inner producer codes made it `PERMANENT` and the case could never retry.

Portfolio-state rejections (`MAX_POSITIONS`, `COOLDOWN`, `ENTRY_THROTTLE`, `STALE_PORTFOLIO`,
`BELOW_MINIMUM`) are deliberately **not** NOT_YET: they say nothing about the token and are outside the
approved list. Add them to `desk.watchlist.NOT_YET` only by review.

## Configuration (scalars only; `load_config` requires positive numbers)

    paper_watchlist_version: 1
    paper_watchlist_backoff_first_seconds:  600     (60..86400, non-decreasing)
    paper_watchlist_backoff_second_seconds: 1800
    paper_watchlist_backoff_third_seconds:  7200
    paper_watchlist_max_reevaluations:      4       (1..16)

Invalid values raise (fail closed). The key changes the config hash: it belongs to a **new ledger/config
version** in the fresh store set; never enable it on an existing ledger or journal.

## Stores and integrity

* `watchlist.sqlite` (`python -m desk.watchlist init PATH`, mode 600, rollback journal): one append-only
  table `watchlist_events`. A trigger chain admits only: evaluation 1 for an unseen mint, or evaluation n after
  an `ENROLLED` row n-1 with the identical hint and a non-decreasing time; `EXPIRED` closes an `ENROLLED`
  row. After `PERMANENT`, `ACCEPTED`, `EXHAUSTED` or `EXPIRED` the chain trigger rejects any further row for
  that mint. UPDATE and DELETE are aborted by triggers. `python -m desk.watchlist show PATH` is read-only.
* The dispatcher journal for a watchlist experiment uses **journal schema 2**: `intents` carries an
  `evaluation` column with `UNIQUE(mint, evaluation)` and `UNIQUE(signature, evaluation)` (schema 1 had one
  intent per mint and per signature, which makes any re-evaluation impossible). Everything else, including
  the immutable-record triggers, is unchanged. The schema is part of the reviewed context
  (`journal_schema: 2`, `watchlist_db` path identity, `maximum_age` 7200, `watchlist_maximum_age` = engine window, `preparation_margin`), so it is chosen when the
  journal is initialised; schema-1 journals and contexts are byte-identical to before.
* Rows are derived from the journal (`_reconcile_watchlist`) at the start of each executing dispatch, after
  journal validation: a crash between a result and its watchlist row is repaired on the next run; nothing is
  written from an unvalidated journal.

## Candidate supply (T09 F15, T20F)

Schema-1 selection reads only the newest 500 `raw_events`; at high volume the eligible window shrinks to
about a minute. With the flag, selection considers **every notification inside the age window** by walking
`raw_events` newest first (`seq DESC`, 500-row pages) and stopping at the first row older than the window plus a
600 s receipt-time skew grace. `received_at` has no index and the shared discovery table grows forever, so the
earlier `WHERE received_at BETWEEN ... LIMIT 20000` read every page (7 s per tick at 600k rows) and its cap truncated a
6 h window at 5000 rows/hour. The walk costs the window, not the table (`tests/test_watchlist_selection.py`:
500k-row table, milliseconds), is bounded by 400 000 rows, and relies on `raw_events` being appended in receipt
order (skew beyond 600 s ends the walk early, which only ever shrinks the candidate set). The per-row
receipt/integrity checks are unchanged.

**Windows.** Fresh selection keeps the configured dispatch window, **7200 s** (minus the 900 s margin). Only a
watchlist re-evaluation may use the engine window, **21600 s** (minus the margin): the context carries
`maximum_age: 7200`, `watchlist_maximum_age: 21600` and `preparation_margin: 900`, and the journal accepts an
evaluation-n intent aged up to 21600 s but an evaluation-1 intent only up to 7200 s. (An earlier draft let fresh
hints be as old as 21600 s, which this reverts.)

## Bounded tables

Every re-evaluation is a new scan and, when it ends in a no-entry, a row in a hard-capped append-only table:
`paper_history_preparation_rejections` (512) and `paper_cycle_no_entry` (8192). When either holds at least 80% of its
cap, the dispatcher stops selecting re-evaluations (fresh hints keep running) and reports
`watchlist_paused_table_capacity: [{table, rows, cap}]` in its `NO_CANDIDATE`/`DRY_RUN` result. Nothing is
deleted or reset; the pressure is also available read-only as `desk.watchlist.capacity_pressure(evidence_db)`
for the health check. Remedy: rotate to a new store set while flat (RUNBOOK).

## Budgets and held monitoring

Re-evaluations draw on the normal budgets: 18 investigation requests per scan, shared provider pacing, the
daily scan allowance. The dispatcher `_preflight` (T16 monitoring reserve, mark freshness, held-exit
priority) runs before every dispatch exactly as for fresh candidates, so a re-evaluation never starts when
held monitoring needs the allowance.

## Activation (coordinator)

1. New ledger/config with `paper_watchlist_version: 1`.
2. `python -m desk.watchlist init /var/lib/solana-desk/watchlist.sqlite` (private directory).
3. `python -m tools.paper_entry_dispatcher ... --watchlist-db <path> --plan`, review, `--initialize` with the
   approved context hash (creates the schema-2 journal). Pass `--watchlist-db` in the entry unit as well.
4. The watchlist and the journal must start together: the first journal result is evaluation 1.

## Failure modes (all fail closed)

| Situation | Result |
|---|---|
| flag on without `--watchlist-db`, or path given without flag | `plan` raises |
| watchlist schema/guards tampered, symlink, hard link, mode other than 600 | store refused |
| unclassified or mixed code, no code at all | `PERMANENT`, never scheduled again |
| journal, scans and watchlist disagree on the evaluation count | entry skipped (never re-evaluated) |
| previous result still unresolved | journal validation stops the dispatcher (unchanged rule) |
| age window closed | `EXPIRED`, no scan |

## Limitations

* The result shapes understood are those the dispatcher publishes today: engine rejects and blockers of a
  completed cycle (including T01's `MARKET_PRODUCER_BLOCKED` pass, whose diagnostics are classified as above),
  `history_preparation_no_entry_v1`, `dispatcher_token_rejection_v1` and `dispatcher_migration_no_entry_v1`.
  Other T01 blockers in `desk.paper_cycle_no_entry.NORMAL` (budget exhaustion, history page limit, retained
  witness required) are not listed in the NOT_YET table and are `PERMANENT` until a reviewer lists them.
* The dispatcher reports table pressure in its own result; the health check does not read it yet (it is not
  part of this task's files). `capacity_pressure()` is the read-only hook.
* The dispatcher-level end-to-end test cannot exercise a real re-evaluation older than two hours: the fixture's
  provider data fails with `HISTORY_RECOVERY_REQUIRED` across such a gap, so that test shrinks the fresh
  window instead (same logic).
* A NOT_YET rejection that is a charged-and-latched failure (no terminal outcome) still blocks the store
  until T01/T22 land; the watchlist adds no recovery path.
* Timings and thresholds are not tuned from data; the counterfactual report (T26/T29) should drive the table.
