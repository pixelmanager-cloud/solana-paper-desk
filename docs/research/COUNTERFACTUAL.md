# Counterfactual candidate tracking (T26)

Question answered: **do our filters reject winners?** Every migration candidate is followed for six hours whether or not it was admitted, rejected or never dispatched. This is research data only (`RESEARCH_ONLY_NOT_FILL_EVIDENCE`): these prices never feed the ledger, a fill or a gate.

## Store (`counterfactual.sqlite`, 0600, append-only)
Triggers forbid UPDATE/DELETE on every table. `candidates` (identity from continuous discovery), `outcomes` (classification history, appended only when it changes), `vaults` (pool vault addresses, resolved once), `vault_attempts` (a pool account that was not visible yet, one row per try), `requests` + `request_results` (every provider request, charged even if it failed), `samples` (one row per candidate and horizon), `policy` (the allowance and grace history), `meta`.

## Ingestion (no backfill)
`ingest` reads the discovery database read-only and never backfills history: a new store starts at the first frame young enough to still have a horizon left (`received_at >= now - (6 h + grace)`), and older frames are skipped without being decoded. Frames whose stored hash does not match are counted and never used.

## Sampling
Horizons: +5m, +15m, +30m, +1h, +2h, +6h after the migration notification. One `getMultipleAccounts` request covers up to 50 candidates (two vault accounts each); the first sighting of a pool costs one extra batched pool-account read (up to 100 pools per request). Price = PumpSwap constant-product spot, quote lamports per raw base unit (`quote_vault / base_vault`), both vaults verified for owner program, mint and pool authority. A closed or empty vault is `POOL_DEAD`. A malformed account is `FAILED` and never priced. A slot more than `grace_seconds` (default 120) late is recorded as `MISSED`, never sampled late. A wrong-shape or failed batch is logged and retried on the next run while the allowance lasts. A pool account that is `null` (not visible yet) is retried with backoff (60 s, 120 s ... capped at 15 min, each try charged and recorded in `vault_attempts`) until the 6 h window has passed, then recorded `UNRESOLVED` once (`POOL_ACCOUNT_NULL_WINDOW_PASSED`); a pool account that is present but invalid is `UNRESOLVED` once at once.

## Allowance and pacing
Its own rolling-hour allowance (default 300, recorded in `policy`, changed only by appending with `set-allowance`). The count includes failed and interrupted requests, so a kill mid-request still counts. It uses the shared Helius pacing database (`DESK_PROVIDER_PACING_DB`, investigation priority) and refuses to run without it. It never reads or writes the entry or monitoring budgets, the research or evidence stores, or the ledger.

## Outcome classification (what the report groups by)
`ingest` joins, read-only, the dispatcher journal, the ledger and the decision store and appends one classification per candidate whenever it changes. Priority: **ledger BUY fill > typed journal result > decision-store rejection > NOT_DISPATCHED**.

| class | meaning | codes |
|---|---|---|
| `BOUGHT` | a BUY fill (ledger, or the stored cycle result) | none |
| `REJECTED:<stage>:<code>` | a typed normal rejection; one group per code | `TOKEN` (`token_policy.reasons`, e.g. `ACTIVE_MINT_AUTHORITY`), `MIGRATION`, `HISTORY`, `OBSERVATIONS` (cycle blockers), `ENGINE` (`outcomes[].reason/reasons`, e.g. `COST_BUDGET`), `DECISION` (decision-store reasons) |
| `NOT_DISPATCHED` | no journal intent (and no decision rejection) | |
| `UNKNOWN` | unresolved dispatch, no result row, recovery required, corrupt/unrecognised row, a store that could not be read | breakdown in `unknown_breakdown` |

Every known result shape is parsed (`dispatcher_token_rejection_v1`, `dispatcher_migration_no_entry_v1`, `history_preparation_no_entry_v1`, `paper_cycle_v1`); anything else is `UNKNOWN`, never guessed and never reported as admitted. Typed results and BUYs are final and are not read again; `NOT_DISPATCHED` is re-checked until the dispatch window has closed. One journal/ledger/decision view is opened once per pass. A candidate with several reasons appears in one group per reason (each filter's own view), so read `distinct_candidates` for the real count.

## Metrics and report
Per candidate: max gain, max drawdown, return at each horizon, liquidity (2x quote reserve) and whether the pool died.
- **One fixed baseline: the +5m sample** (the migration-time price is not observed). A candidate with no usable +5m sample is **excluded** from the return statistics and counted as `baseline_missing`; it is never re-baselined to a later horizon.
- **Survivorship**: a pool that is `POOL_DEAD` is **-100%** at that horizon and at every later horizon without a priced sample of its own. It is included in the return statistics (`dead_at_horizon`) and reported (`pool_died`). A pool already dead at +5m counts as -100% at every horizon (`dead_at_baseline`).

`python -m tools.research.counterfactual report --store S [--horizon 3600]` prints, per group, the number of candidates, `with_return`, `baseline_missing`, mean/median/p10/p90 return, share positive, deaths and median max gain. Read it as "this filter rejected N tokens whose median +1h return was X%". Small groups are noise.

## Operating
`init` once on the fresh root, in its own subdirectory (`<root>/counterfactual/counterfactual.sqlite`). `deploy/fresh/desk-counterfactual.timer` runs `deploy/fresh/desk-counterfactual.service` (ingest then sample) every minute after the previous run finished (this is its restart policy). The service may write only that subdirectory and the shared pacing database (whose directory must be writable for SQLite's rollback journal; the experiment root is made read-only again). Read-only inputs use `mode=ro`, or `immutable=1` only for a quiet WAL file. `set-allowance` accepts 1..3600. A cold restart resumes from the store: samples are unique per (candidate, horizon).
