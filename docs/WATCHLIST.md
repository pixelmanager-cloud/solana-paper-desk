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
  (`journal_schema: 2`, `watchlist_db` path identity, `maximum_age` = engine window), so it is chosen when the
  journal is initialised; schema-1 journals and contexts are byte-identical to before.
* Rows are derived from the journal (`_reconcile_watchlist`) at the start of each executing dispatch, after
  journal validation: a crash between a result and its watchlist row is repaired on the next run; nothing is
  written from an unvalidated journal.

## Candidate supply (T09 F15)

Schema-1 selection reads only the newest 500 `raw_events`; at high volume the eligible window shrinks to
about a minute. With the flag, selection reads **every notification inside the age window**
(`received_at` between `now - maximum_age` and `now - 300`), bounded by 20 000 rows for memory only. The
per-row receipt/integrity checks are unchanged. The fresh-hint window is the engine age window (21600 s)
instead of 7200 s, so a migration seen while the desk was down remains eligible while the engine would still
accept its age.

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
  completed cycle, `history_preparation_no_entry_v1`, `dispatcher_token_rejection_v1` and
  `dispatcher_migration_no_entry_v1`. T01's generic cycle no-entry receipt (`paper_cycle_no_entry_v1`, which
  lives on another branch) is not parsed here; until its blocker is surfaced to `reason_codes` it classifies
  as `PERMANENT`. After merging T01, add its blocker mapping (its `MARKET_PRODUCER_BLOCKED` is already listed).
* A NOT_YET rejection that is a charged-and-latched failure (no terminal outcome) still blocks the store
  until T01/T22 land; the watchlist adds no recovery path.
* Timings and thresholds are not tuned from data; the counterfactual report (T26/T29) should drive the table.
