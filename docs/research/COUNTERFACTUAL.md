# Counterfactual candidate tracking (T26)

Question answered: **do our filters reject winners?** Every migration candidate is followed for six hours whether or not it was admitted, rejected or never dispatched. This is research data only (`RESEARCH_ONLY_NOT_FILL_EVIDENCE`): these prices never feed the ledger, a fill or a gate.

## Store (`counterfactual.sqlite`, 0600, append-only)
Triggers forbid UPDATE/DELETE on every table. `candidates` (identity from continuous discovery), `outcomes` (dispatcher result history, appended only when it changes), `vaults` (pool vault addresses, resolved once), `requests` + `request_results` (every provider request, charged even if it failed), `samples` (one row per candidate and horizon), `policy` (the allowance and grace history), `meta`.

## Sampling
Horizons: +5m, +15m, +30m, +1h, +2h, +6h after the migration notification. One `getMultipleAccounts` request covers up to 50 candidates (two vault accounts each); the first sighting of a pool costs one extra batched pool-account read (up to 100 pools per request). Price = PumpSwap constant-product spot, quote lamports per raw base unit (`quote_vault / base_vault`), both vaults verified for owner program, mint and pool authority. A closed or empty vault is `POOL_DEAD`. A malformed account is `FAILED` and never priced. A slot more than `grace_seconds` (default 120) late is recorded as `MISSED`, never sampled late. A wrong-shape or failed batch is logged and retried on the next run while the allowance lasts; a pool account that is invalid is recorded `UNRESOLVED` once.

## Allowance and pacing
Its own rolling-hour allowance (default 300, recorded in `policy`, changed only by appending with `set-allowance`). The count includes failed and interrupted requests, so a kill mid-request still counts. It uses the shared Helius pacing database (`DESK_PROVIDER_PACING_DB`, investigation priority) and refuses to run without it. It never reads or writes the entry or monitoring budgets, the research or evidence stores, or the ledger.

## Metrics and report
Per candidate: max gain, max drawdown, return at each horizon, liquidity (2x quote reserve) and whether the pool died. **Baseline is the first priced sample (+5m): the migration-time price is not observed**, so returns are "after the first five minutes". `python -m tools.research.counterfactual report --store S [--horizon 3600]` prints, per rejection code (`ADMITTED:<status>`, `NOT_DISPATCHED`, `UNKNOWN_OUTCOME` for the rest), the number of candidates, mean/median/p10/p90 return, share positive, deaths and median max gain. Read it as "this filter rejected N tokens whose median +1h return was X%". Small groups are noise.

## Operating
`init` once on the fresh root, a timer runs `deploy/fresh/desk-counterfactual.service` (ingest then sample). Reports use `mode=ro`. A cold restart resumes from the store: samples are unique per (candidate, horizon).
