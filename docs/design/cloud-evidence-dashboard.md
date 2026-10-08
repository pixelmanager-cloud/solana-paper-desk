# Cloud evidence dashboard contract

Prepared for Agent 09, 8 October 2026 (Asia/Seoul), against base `e95a8df`.
This is a contract and fixture-test deliverable for the existing read APIs.
It does not complete [OPS-1, issue #8](https://github.com/pixelmanager-cloud/solana-paper-desk/issues/8), enable entries, or establish paper readiness.

## Scope and sources of truth

The implementation sources are `desk/dashboard.py` (`handler`, `Jobs.list`,
`serve`), `desk/decision_runner.py` (`recent_decisions`, `assess`, `consume`),
`desk/entry_evidence.py` (`evaluate`, `POLICY`), `desk/replay_history.py`,
`desk/ownership_signals.py`, `desk/evidence.py`, `desk/history_progress.py`,
`desk/paper_view.py` (`paper_status`), `desk/ledger.py` and `desk/engine.py`.
API names below are actual fields; presentation rules describe the intended
integration. No new response fields or production UI changes are introduced.

All paths are GET requests on the private loopback service. The database siblings
are resolved from the research database directory, not caller-supplied paths.
`serve` binds `127.0.0.1`; accepted Host values are exactly
`127.0.0.1:<port>` and `localhost:<port>`. Origin must be absent or match one of
those HTTP origins. Responses carry `Cache-Control: no-store`, `nosniff` and a
same-origin CSP with `frame-ancestors 'none'`. Foreign Host/Origin returns 403.
Maintain these checks and binding; no public proxy or access expansion follows
from this design.

| Endpoint | Persisted source and actual projection | Required interpretation |
| --- | --- | --- |
| `/api/status` | Constant `mode=RESEARCH_ONLY`, `live_trading=false`, `automatic_entry_enabled=false`, `max_scans_per_day=10`, `max_rpc_calls_per_scan=18` | Policy limits and disabled entries, not measured worker health or remaining quota. The 18 ceiling is shared with continuation, not a new allowance on refresh. |
| `/api/scans` | Research `scans(id,mint,created,status,result)`; result JSON decoded; newest 50 by created then rowid | Original bounded investigations; `created` is queue time, not chain time. `COMPLETE` means job completion, not complete ownership evidence. Summary approval claims have no authority. |
| `/api/decisions` | `paper-decisions.sqlite`, `decision_evaluations`; latest inserted current-policy revision per scan, newest 20 rows | Historical evidence evaluations, not current approval. Current policy is `persisted-entry-evidence-v3`; revision suffixes are matched by the reader. Older policies and original decisions remain persisted but are not listed here. |
| `/api/paper` | `active-paper.sqlite`; one read transaction binds state, metadata and latest 50 outcome rows | Optional simulated accounting checkpoint. It says `PAPER_ONLY`, entries disabled and runner `NOT_CONNECTED`, even when a ledger exists. |
| `/api/launches` | `launches.sqlite`, latest 30 raw event rows decoded into identified launch instructions | `launches[]` has mint, wallet, slot, signature, received_at; coverage is `Periodic bounded sample; not all launches`. Thirty is a raw-row limit, not necessarily a launch-count limit. No finalized anchor or ownership approval is asserted. |

The endpoints are independently read; there is no atomic cross-database dashboard
snapshot. Join investigations and decisions by `scan_id`/scan `id` and mint, then
retain the decision's source identity. Do not join by array position or replace
an evaluation's source with a later mutable summary. Read limits have no total
counts/pagination guarantee; absence from a recent list does not mean no older
record, no open obligation, or no exposure. GET must not enqueue or run a scan,
advance ownership, reevaluate decisions, create an optional database or consume
provider requests. The test harness constructs `Jobs` once for its temporary
schema, never calls `serve`, `submit`, `once` or `run`.

## Evidence provenance and unresolved ownership

A decision contains `kind`, `policy`, `scan_id`, `mint`, `observed_at`,
`evaluated_at`, `source_hash`, `decision`, `eligible_for_trading`, `reasons`,
`notice` and `entry_evidence`. Its `source_hash` hashes the canonical scan record
including the original serialized result. This differs from `report_hash` (hash
of the report without its own hash) and raw `evidence_hashes`. These hashes are
integrity identities, not claims of chain finality, current freshness or safety.
The original `decisions` table keeps `source_payload` and `source_hash`;
versioned evaluations add rows without replacing originals. The GET endpoint
does not expose either source_payload or an evidence download API. It projects
stored evaluation JSON without rerunning gates or authenticating journal
contents; a successful GET is not an independent evidence replay. Preserve the
trusted evaluator boundary and treat unexpected response shapes as unavailable.

`entry_evidence` contains `policy`, `gates`, `metrics`, `launch_history`,
`eligible_for_trading=false` and aggregated `reasons`. Each gate has `status`,
`reasons[]`, `evidence_hashes[]` and sometimes `scope`. A status is `BLOCKED` or
`VERIFIED_COMPONENT`. Display the exact component and its scope beside the
status; never promote one verified component to token safety or entry approval.
The current consumer always records `decision=REJECT`. Display blocker codes
alongside readable explanations, retain missing-data reasons, and render data
as text (including untrusted findings), not HTML.

| Actual gate/field | Evidence binding and presentation boundary |
| --- | --- |
| `report_integrity` | Report checksum alone cannot replace raw replay. |
| `token_controls` | Saved `getAccountInfo` response is bound to the exact mint and request options. |
| `holder_snapshot` | Saved same-bank `getMultipleAccounts` reconstructs supply/account controls. `metrics.gross_top10_supply_pct`, `holder_owner_count`, `holder_slot` are gross supply; pool/service wallets have not been excluded. This is not private-wallet concentration or bundle exposure. Missing metric is unknown, never zero. |
| `pool_liquidity` | Saved pool discovery and snapshot must replay identity, accounts and liquidity checks; one successful pool check does not approve route policy. |
| `launch_anchor`, `mint_history_coverage`, `historical_account_inventory`, `transfer_history` | Request-bound pages are replayed and hashed. A mint query is distinct from every discovered token account's history, continuity, ordering and current-snapshot boundary. The current transfer gate includes `HISTORY_TO_CURRENT_SNAPSHOT_NOT_RECONCILED`. |
| `bundle_exposure` | Launch/transfer coverage, funding service classification and current-holder exposure remain explicit blockers. No observed links is not zero exposure. |
| `sellability`, `strategy_inputs` | Exact entry/exit route policy and live flow/momentum/cost inputs remain unverified. Public-holder diagnostic simulations are not wallet-specific approval or fills. |

When available, `launch_history` exposes `launch_verified`, `launch_reasons`,
`anchors`, `query_coverage_verified`, `query_reasons`, `history_start`,
`history_end`, `coverage_hash`, `slot_range`, `page_hashes`, `request_hashes`,
`inventory`, `observed_transaction_count`, `funding`, `account_queries`,
`block_ordering`, `account_continuity`, `observed_movements`,
`transfer_history_complete=false`, and `eligible_for_trading=false`.
Show `account_queries.required`, `verified`, `unverified_accounts` and `reasons`
together, not just a reassuring fraction. Missing/null history is unavailable.
Query windows, exact slot cutoff, cursor exhaustion, decoding gaps, conflicts,
unordered same-slot transactions and reconciliation failures are distinct gaps.
A block timestamp is an estimate and cannot substitute for a slot cutoff.

Funding's `shared_source_candidates[].classification` remains
`UNKNOWN_SERVICE_OR_PRIVATE_SOURCE`; `common_control_verified=false` and
`funding_history_complete=false`. Shared funding is a risk candidate, not proof
of common ownership. Preserve ambiguous transfers and missing-query reasons.
There is no evidence-backed service expiry/label or quantified current-holder
bundle percentage in these APIs yet; do not invent these values.

Optional `entry_evidence.continued_ownership_history` is a separately reconstructed
view. Show it beside the original `launch_history`, with
`ownership_progress_hash` and `ownership_requests_used` (spent out of 18).
Continuation does not replace original gates, source hashes or timestamps, and
spent requests include failed/interrupted attempts. The GET projection does not
expose individual resume cursors, a remaining quota guarantee, or snapshot
capture status. These belong to future bounded operational integration.

## Decision freshness

Label `observed_at` as investigation observation time and `evaluated_at` as
historical evaluation time. Show both with timezone and age. Neither queue
creation nor recent evaluation proves fresh market/holder/route data.
`assess` adds `INVESTIGATION_NOT_FRESH_FOR_ENTRY` unless observed_at is an
integer with `0 <= evaluated_at - observed_at <= 10`. Future observations fail
that check. The endpoint does not recompute freshness at GET time, so an old
persisted decision may have no stale reason from its original evaluation.
A client may label it historical/aged but must not infer permission from the
absence of that reason. A new continuation evaluation preserves `observed_at`.

No response contains a dashboard-wide observation time or live worker heartbeat.
Show fetch failures/stale cached data as unavailable with the last successful
fetch time, not as a current successful decision. A current-policy revision is
selected by insertion order, not necessarily the maximum evaluated_at.

## Positions, costs and net outcomes

`/api/paper` states are `NOT_CONFIGURED`, `EMPTY_LEDGER`, `LEDGER_UNAVAILABLE`
and `LEDGER_PRESENT`. The first three show no confirmed balance/outcomes;
null cash or PnL means unavailable, not zero. An unavailable decision projection
likewise differs from `NOT_CONFIGURED`; neither means evidence passed.
`recent_decisions` currently handles malformed schema/JSON but opening a damaged
SQLite file can raise before that handler. The UI must handle transport failure.
The scans/launches readers also lack a uniform malformed-schema error envelope;
this contract does not silently promise one.

For a present ledger, expose `strategy_mode`, `last_event_at`, `config_hash`,
`implementation_hash`, `cash_sol`, `realized_pnl_sol`, `estimated_equity_sol`,
`valuation_status`, `positions` and `recent_outcomes`. Preserve decimal strings;
format for display without float-based accounting or converting null to zero.
`last_event_at` is a checkpoint event time, not a runner heartbeat.

Each position has `mint`, `quantity`, `cost_left_sol`, `last_model_value_sol`,
`unrealized_pnl_sol`, `mark_at`, `mark_age_seconds`, `mark_status`, `exit_blocked`,
`provenance`, `valuation_verified=false`. Show quantity and remaining allocated
cost even if blocked. Show `SYNTHETIC_TEST_ONLY` prominently. A model value is
not executable proceeds or a verified valuation. Freshness requires
`0 <= now - mark_at <= config.price_ttl_seconds`, `MODEL_ESTIMATE`, and no
exit blocker; stale/future/blocked marks withhold unrealized PnL and aggregate
equity, while retaining the last model value and position. Blocked mark status
may be preserved rather than replaced with `STALE`; display age and blocker
together. With no positions, cash can be model equity without implying verified
market prices or a connected runner.

Outcomes wrap `{event_id, ts, outcome}`. Buy fills provide `amount_sol`,
`quantity`, `fee_sol`, `estimated_cost_fraction`; sell fills provide `quantity`,
`proceeds_sol`, `fee_sol`, `realized_pnl_sol`, `reason`. Entry cost includes the
entry fixed fee; sell proceeds deduct the sell fixed fee; realized PnL deducts
the allocated remaining entry cost. Preserve fees and provenance and label all
results simulated. Do not subtract displayed fees twice, sum the recent 50 rows
as lifetime PnL, or count `blocked_exit` as a fill. The current constant-product
model excludes rent; these APIs do not provide a complete realized fee/rent/
failed-attempt cost breakdown or full trade history. Synthetic net outcomes
cannot establish profitability.

## Pause, exit-only and restart presentation

| `strategy_mode` | Presentation |
| --- | --- |
| `RUNNING` | Persisted strategy state; automatic entries still disabled and runner not connected. |
| `ENTRY_PAUSED` | Entry paused; retain positions and exit obligations. |
| `EXIT_ONLY` | Entries blocked; show every unresolved exit and exact reason. |
| `LIQUIDATING` | Liquidation requested; do not imply a verified successful exit. |
| `STOPPED` | Persisted stopped strategy state; not proof of process health. |

Controls are durable engine events (`PAUSE_ENTRY`, `EXIT_ONLY`, `LIQUIDATE`,
`RESUME`), not dashboard endpoints. The sole POST route submits research scans;
there is no pause/resume/exit mutation API. This read-only design adds no buttons
that claim to execute controls. An unverified exit keeps inventory, latches
EXIT_ONLY from RUNNING, emits `blocked_exit` with reasons, and cannot be bypassed
by RESUME while the blocker persists. Verified recovery does not automatically
resume entries. Reopening the ledger must preserve checkpoint mode, positions,
outcomes and identity. GET only projects a checkpoint; it does not establish that
a continuous runner or watchdog restarted successfully.

## Verification and integration dependencies

`tests/test_cloud_dashboard_evidence.py` sends actual HTTP GETs to an ephemeral
`127.0.0.1` listener with sibling temporary databases. It seeds investigations
via fixture SQL, evaluations through the offline consumer, and synthetic paper
states through Ledger/engine. It guards `Jobs.submit` and the scanner against
invocation. Public committed fixtures `fixtures/mainnet-holder-snapshot.json`
and `fixtures/mainnet-launch.json` are replayed locally, never fetched from a
provider; they prove historical component/projection behavior only. A manually
seeded continuation revision tests projection selection, not the continuation
producer or live ownership completion.

Adversarial cases cover hostile Host/Origin, forged summary approval, missing
raw evidence, old/future observation times, obsolete policy selection, a newer
historical revision preserving observation/source, gross-supply scope, limits,
malformed launch JSON, missing/corrupt projections, exact TTL/future marks,
persisted pause, blocked exit plus RESUME/reopen, net cost fields, and unchanged
original database records across GETs. See the coordinator handoff for exact
Python 3.12 full suite results: `.venv/bin/python -m unittest discover -q`
passed 529 tests in 10.481 seconds on Python 3.12.14, with no failures or skips,
including all 16 new contract tests. Fixture tests do not validate visual UI,
live resource consumption, provider outages or forward operation.

Issue #8 remains dependent on [PAPER-2, #7](https://github.com/pixelmanager-cloud/solana-paper-desk/issues/7),
which depends on [SELL-1, #5](https://github.com/pixelmanager-cloud/solana-paper-desk/issues/5)
and [PAPER-1, #6](https://github.com/pixelmanager-cloud/solana-paper-desk/issues/6).
Those depend on the ownership review [OWN-4, #4](https://github.com/pixelmanager-cloud/solana-paper-desk/issues/4),
following OWN-1/2/3. This preparatory contract does not satisfy those dependencies.
The coordinator must integrate validated ownership/classification/exposure,
trusted live strategy inputs, exact route checks and durable automatic paper
entry/monitor/exit/accounting before completing OPS-1. Remaining OPS integration
includes explicit operational health/freshness, full cost/history projections,
verified control authorization and acknowledgements, restart workflow and visual
UI/access/resource validation. End-to-end forward observation belongs to QA-1
(#9) after OPS-1. No production UI, gates, budget, records, task queue or readiness
file is changed here; no issue is closed, no deployment occurs, and entries
remain disabled.
