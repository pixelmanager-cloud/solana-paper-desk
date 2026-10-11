# Candidate feature store (T40)

`python -m tools.research.features` builds `features.sqlite`, an append-only store of the features that were **known at each time**
for every candidate, derived only from evidence the desk already retains. It makes **no provider, RPC or network call**, opens
every input `mode=ro` (`immutable=1` only for a quiet WAL file) and never writes to them. Paper only; everything it describes is
EXECUTION_UNVERIFIED.

## Commands
```
python -m tools.research.features init  --store <ROOT>/features/features.sqlite
python -m tools.research.features ingest --store S --ledger L [--discovery-db D] [--journal J] [--research-db R] \
        [--decisions-db X] [--counterfactual-store C] [--since T] [--until T] [--now T]
python -m tools.research.features verify --store S                      # exit 2 if any row fails its proof
python -m tools.research.features export-jsonl --store S --out rows.jsonl [--since T] [--until T]
python -m tools.research.features shadow-features --store S --out features.json [--until T]   # T27 --features input
```
`ingest` is idempotent. Re-running yields identical rows (no-ops); if a key `(mint, as_of, feature_set_version)` would receive
*different* content it is recorded once in `feature_conflicts` and the stored row is never touched. Triggers refuse every UPDATE and
DELETE. `export-jsonl` and `shadow-features` create their file exclusively (0600) and refuse to overwrite.

## Rows
One row per `(mint, as_of, feature_set_version = candidate-features-v1)`: `features` (name -> value) and `provenance` (name ->
`{source, ref, at, role}`; derived features also carry `derived`). `ref` is the source row (`event_id#payload_hash`, discovery
`seq`, journal scan id, sample `mint/horizon`, decision `scan_id`). `role` is `input` (known before the decision) or `decision`
(what the desk decided/did: reject reasons, `entered`, dispatch outcome, decision label; **labels, never model inputs**).

| source | as_of | features |
|---|---|---|
| ledger market events (`events`) | event `ts` | `market_cap_usd`, `reserve_sol`, `reserve_tokens`, `sol_usd`, `pool_fee_bps` (at `price_at`); `top10_pct`, `dev_pct`, `bundle_pct`, `cluster_pct`, `fresh_wallet_ratio` (at `holder_at`); `flow`, `net_buy_ratio`, `unique_buyers_5m`, `volume_vs_liq`, `wash_score`, `manip_*`, `flow_confirmed` (at `flow_at`); `drawdown_from_high` (at `momentum_at`); safety flags, `dev_launches_7d`; derived `liquidity_usd = 2*reserve_sol*sol_usd`, `age_since_migration_seconds = ts - graduated_at`; `token_2022` (token program of the recorded mint account); `holders_listed`, `early_buys_listed`, `holder_coverage_pct` (bundle evidence) |
| ledger outcomes | event `ts` | `reject_reasons`, `entered` (role decision) |
| discovery frames | `received_at` | `migrated_at`, `migration_slot` |
| dispatcher journal | intent / result `at` | `dispatched`, `dispatch_stage_reached`, `dispatch_death_reason`, `requests_charged` (role decision) |
| counterfactual samples | `sampled_at` | `cf_base_vault_raw`, `cf_quote_vault_raw`, `cf_sample_horizon_seconds` |
| decision journal | the decision's own `ts`/`at` | `decision_label` (skipped if the decision has no time or mint) |

## No look-ahead and unknown-stays-unknown
* `as_of` is the evidence row's own time, never the ingest time; every feature has `at <= as_of`, checked on insert and again by `verify`.
* A measurement stamped later than the event carrying it (for example `holder_at > ts`), a value whose measurement time is missing, and
  evidence dated after `--now` are **dropped and counted** in the ingest report (`skipped`), never kept or repaired.
* A feature the evidence does not carry is absent. There are no neutral defaults in the store. Corrupt values (non-numeric decimals,
  absurd magnitudes, non-boolean flags, bool-as-number) are skipped and counted.

## T27 shadow input
`shadow-features` emits `{mint: {"as_of": epoch, <field>: value}}` for `tools.research.shadow_strategies --features`. Per mint it takes
the **earliest** ledger-event row that carries any shadow field and only the field names the shadow engine accepts (its
`NEUTRAL_FEATURES`), with the values as recorded, and never `decision`-role data. The shadow's own rule then refuses entries before `as_of`.
Later rows are deliberately not merged in: that would be look-ahead for entries made before them.

## Unit
`deploy/fresh/desk-features.service` + `.timer` (inactive templates): offline (`PrivateNetwork=true`), no credentials, only
`<FRESH_ROOT>/features` writable, T39-style limits (`CPUQuota=100%`, `CPUWeight=20`, `IOWeight=20`, `Nice=10`, `IOSchedulingClass=idle`,
`MemoryMax=384M`, `TasksMax=16`). Add `--counterfactual-store ...` to `ExecStart` once that store exists.

## Limits
* Regime metrics (T33) are not read: no T33 evidence store exists in this base.
* The shadow `--features` format holds one as-of per mint, so variants see features from the first recorded event on; time-varying features
  are available in the JSONL export for offline analysis only.
* Decisions carry no time in the stores seen so far, so most `decisions` rows are skipped (counted as `DECISION_NOT_PLACEABLE`).
* Not run under a real systemd; the dispatcher/ledger/discovery fixtures are synthetic or the repo's public test captures.
