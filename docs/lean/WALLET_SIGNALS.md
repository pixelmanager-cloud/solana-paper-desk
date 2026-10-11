# Wallet signals (L13): bundle / sniper detection and the smart-wallet table

FEATURES ONLY. Nothing here is read by `lean.strategy`; an entry or exit never depends on it. Paper only: read-only RPC
methods, no signing, no sending. Off by default: `"wallet_signals": null` (or absent) in `lean.json` keeps today's behaviour;
an object (even `{}`) turns it on (`config/lean/wallet_signals.example.json`, defaults in `lean.wallet_signals.DEFAULTS`).

## What is written
One `observations(kind='wallet_signals')` row per screened (PASS or REJECT) candidate: canonical JSON bytes in `raw`, the same
headline figures in `meta`. Fields (`version: wallet_signals_v1`):

| field | meaning |
|---|---|
| `same_slot_buyers` | distinct buyers whose first buy is in the pool-creation (graduation) slot |
| `sniper_count` | distinct buyers whose first buy is within `sniper_slots` (3) slots of it |
| `bundle_score` | share of the examined buyers that are same-slot buyers or members of a funding cluster (0..1) |
| `bundled_supply_pct` | last seen balance of the bundled wallets as a % of the token supply |
| `funding_clusters` | `[{source, wallets}]`: >= `min_cluster` examined buyers first funded by the same wallet (1 hop) |
| `smart_buyers_count`, `smart_wallets` | window buyers the desk's own record rates as smart, as of `smart_asof_event_id` |
| `unavailable` | `{field: reason}`: every null has a reason (`CALL_CAP`, `CREDIT_SHED`, `WINDOW_START_NOT_REACHED`, `SIGNATURES_UNAVAILABLE:<code>`, `FUNDING_HISTORY_TRUNCATED`, `NO_PRIOR_HISTORY`, `NO_FUNDING_TRANSFER`, `SUPPLY_UNAVAILABLE:<code>`, `NO_BUYERS_IN_WINDOW`, `COLLECT_FAILED:<type>`) |
| `calls`, `call_cap` | requests actually sent for this candidate and the hard cap |

**Shared funding is a signal, not proof of common ownership.** The source may be an exchange hot wallet or a funding service;
use `ignore_funders` for known hubs. The caveat is stored in every row. The score is a lower bound (only the earliest
`max_swap_txs` window transactions and the earliest `funding_wallets` buyers are examined).

Append-only `events`: `wallet_buy` (one per examined buyer and candidate) and `wallet_outcome` (one per token once known). The
store schema is untouched (a lean store is never migrated), so `lean_wallets` is the view `WalletSignals.wallet_table()` over those
events: `{wallet: {wins, rugs, flat, unresolved, buys}}`.

## Smart wallets
Built only from this desk's own records. A token's outcome (first hit wins): from the L07 `path_mark` observations
(`meta.price_sol`; WIN = price >= `win_multiple` (2.0) x the first mark, RUG = <= `rug_price_frac` (0.3) x; `path_end` without
either = FLAT), else from this desk's own closed trade (pnl/cost >= +100 % WIN, <= -`rug_loss_frac` (50 %) RUG, else FLAT),
else UNKNOWN after `outcome_max_age_s`. A wallet is smart with >= `min_wins` (2) wins and (wins+1)/(wins+rugs+2) >=
`smart_threshold` (0.7). A candidate's count only uses outcomes recorded before its row was written; its own buys are written
after it.

## Budget
Per candidate at most `max_calls` (16) requests: 2 signature pages + `getTokenSupply` + 6 `getTransaction` + 3 wallets x
(`getSignaturesForAddress` + `getTransaction`) = 15 at worst. A launch with four window transactions and three funding lookups costs 12 (the e2e fixture); one with no swap in the
window costs 2. One attempt per request (no transport retries), so the cap equals the requests sent. Every request goes
through `LowLaneLimiter`: its own bucket (`low_rate_per_s` 1.0, burst 2) AND a token of the shared Helius main-lane bucket, so
the collector can never use more than 1 req/s and always queues behind screening; `shed()` (credit shedding, L15) refuses
before any token is taken. The exit lane is never touched. Collection runs on its own thread (`WalletSignals.run_loop`) and
finds work straight from the store (screened, no row yet), so a restart loses nothing and no queue can overflow. A transient
failure of the first request is retried (`max_retries`, `retry_delay_s`), then recorded as unavailable; an unexpected error is
retried a bounded number of times and then closed out with a `COLLECT_FAILED` row, so one bad candidate never blocks the rest.

`retain_raw`: `signatures` (default) keeps the signature pages as `wallet_signals:raw:*` observations; `all` also keeps every
`getTransaction` body (large); `none` keeps only the feature row.
