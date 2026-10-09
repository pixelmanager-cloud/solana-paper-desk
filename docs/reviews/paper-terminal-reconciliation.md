# Bounded terminal candidate rejection reconciliation

Scope: accepted integration `490d8960566ad4c0c9e81c0ab474bef18a355a46`.
This fixes one global pass-latch defect, without retries, healthy completion,
provider calls, entry authorization or recovery of ambiguous/crashed passes.

`reconcile(research_db, evidence_db, ledger_db, cfg, *, pass_id, outcome_hash,
attempt_refs, invocation_hash, pacing_db)` explicitly appends a guarded,
content-hashed receipt. `review_plan` takes the same arguments under canonical
research → evidence invocation → ledger locks and returns a **read-only proposed
pin**. The lower-level `plan` requires its caller to hold those locks. Neither
planning helper installs policy or authorizes reconciliation. The CLI exposes
these as `python -m desk.paper_terminal_reconciliation` with explicit paths,
`--pass-id`, `--outcome-hash`, three ordered `--attempt-ref` arguments,
`--invocation-hash`, and `--plan` for read-only planning. Omitting `--plan` requires
an exact external pin in `config/paper-terminal-reconciliation.json`; its default
association list is empty. Config parsing reuses the existing bounded CLI parser;
no credentials are read.

## Historical association

The old `paper_cycle_v1` result contains no intrinsic pass/intent/attempt linkage.
Matching an outcome hash and a budget counter is insufficient. An independently
reviewed pin must bind the exact pass, original intent, returned outcome, ordered
transport records, sanitized invocation page, current reviewed desk source,
configuration, canonical research/evidence/ledger/pacing paths, and verified
INIT-only ledger metadata/checkpoint/event/outcome anchors. The pin is explicit
operator association evidence, **not provider or invocation authentication**.
Review must certify the complete retained-attempt inventory; the validator does
not pretend that three supplied pages prove absence of other historical pages.

The coordinator supplied pass `d8600ce75e014e22b84c3fd8d10bf2c0`, intent
`6f6cfd1efe9aa257638bd750ccd3c4099a3943a5d403132fa9d9d2a7b868fb7e`,
outcome `4352afa8e732c4f03eaf1cb788047aea58379fe3ef24edd05f5535e5680eb4a8`,
and scan `d796b12f49594e8097cc6cbb46299edb` as the concrete historical case.
Three retained attempts are ordinals **8, 9, 10**, after the original 7 charges:

- mint: `5fc5caa16818e63891c9fdea7bb7826c1dd80d149803a69991338272685fa4fc`
- discovery pool: `363b8cf31f410964b20a92cf931ab21a324e5da7832ec159d3d8526b3893cbaa`
- atomic accounts: `0a42e9a83430b2aae7df6ae92659ab328195192e50b7f3c92c8113d352f3ebe3`

The supplied systemd manager records bind invocation
`4d4a463a077e41f98aa646e7f620ebcc`, its command, start, exit status2 and timing.
They do **not** contain application stdout. Original bytes and the coordinator's
inventory are retained outside this PR; no production DB or invented fixture is
published. No real association pin is installed here. Actual locked replay and
independent association/pin review remain required before coordinator application.

## Validation and preservation

The original pass retains `outcome_hash=NULL`; original failure/outcome/raw pages,
ledger tables, immutable runtime receipts, metadata/configuration, grants, counters,
high water and latches are untouched. One new evidence-side table is the only
publication. Exact replay is idempotent, conflicts/partial publication reject and
transaction failure rolls back table and row creation. INSERT/REPLACE, UPDATE and
DELETE cannot overwrite receipts. Exact schema/guards/count/SQL type/byte
preflight precedes receipt payload/hash materialization.

Proof requires successful retained HTTP200 records, strict duplicate-free RPC
request/response JSON and base64, the exact mint/discovery/atomic request sequence,
source/scan/contiguous ordinal binding, consistent timestamps, raw mint and atomic
pool ingestion, and exclusively known negative policy results. Missing/failed or
reordered attempts, changed admission/ledger, malformed account controls,
unknown schema/extension, pending pacing tickets/waiters or monitoring outcomes,
and unresolved monitoring latches refuse. Existing monitoring grant/accounting
and active handoff are checked with their accepted validators, never adopted or
reprovisioned. All three pending-pass consumers (`paper_cycle`, `paper_observe_cli`, and the history-first entry wrapper) validate receipts and continue to block
all other NULL passes. The exact rejected scan is durably retired and cannot be
retried through either consumer.

Readers validate the original ledger prefix and current checkpoint/runtime, so
later legitimate activity by another candidate is not confused with changing the
certified INIT prefix. Retired-scan charges remain exact. Historical reconciliation
itself requires the ledger still be INIT-only and all pinned anchors unchanged.

## Future intrinsic terminal rejection

New cycle results include pass ID, intent hash and actual transport attempt hashes.
Eligible single-candidate intents additionally bind the current source/context and
verified INIT-only ledger anchors before transport. A dedicated `PolicyRejection`
subtype is raised only after successful raw policy evaluation with **all** reasons
in the narrow allowlist: active legacy mint/freeze authority, outstanding LP,
selling disabled, evaluated boost, mayhem/cashback/holder rewards. Accrued/virtual
reserve diagnostics qualify only alongside an evaluated positive boost. Generic
`ObservationError`, matching error text, `SOURCE_CONTENT_REJECTED`, Token-2022
profile rejection, malformed controls, identity contradictions, timeouts, stale
clocks, persistence failure and charge mismatch cannot certify closure.

Only exact default transport records plus independent raw replay can certify an
intrinsic rejection: one mint RPC or three pool RPCs, no quote/history/USD request,
no controls, positions, events or outcomes, and an unchanged INIT-only ledger.
The result stays BLOCKED / EXECUTION_UNVERIFIED; the original NULL row remains,
with only an immutable terminal receipt exempting it. No external historical
association is inferred. Multiple-target/held/exit, unknown or partially persisted
failures remain globally blocked. Bounds:256 receipts,10,000 original passes,
64KiB per receipt/policy,32 legacy pins, one or three attempts,2MiB response,
32KiB request and original lifetime18. Exhaustion refuses rather than pruning.

## Fixture validation

Dedicated tests use genuine persisted admissions, ledger/checkpoint and charged
transport with synthetic HTTP bytes, clocks and manager entries. Tests cover
legacy pin refusal, exact replay, immutable original NULL and charges, all three actual
consumers' zero-I/O retirement, a different admitted candidate proceeding,
intrinsic active mint/freeze and LP negatives, unknown/misleading errors,
transport failure, missing/duplicate/reordered attempts, changed checkpoint or
counter, mismatched invocation, pacing/monitoring pending outcomes, corruption,
scalar bounds before parsing, INSERT/REPLACE refusal and rollback after append.
The unchanged `fixtures/mainnet-pool-fee-snapshot.json` is an offline known-negative
replay, **not** the coordinator's candidate222 launch or a historical certificate.

Exact results are recorded in the PR and CI. Final full Linux validation uses
exact-head GitHub CI as requested; no redundant cloud full-suite run is started.
Source changes invalidate the old deployment pin. Independent09/04 review and
coordinator-only final source pinning, runtime upgrade and actual reconciliation
remain dependencies. No readiness, ownership, safety or profitability claim.
