# Explicit paper-only ownership history risk policy

Base: accepted `9e43752`. New disconnected callable in
`desk/paper_experimental_policy.py`; no engine or strict-consumer changes.
The user's explicit exception takes precedence over the repository's default
strict evidence gate only for unresolved ownership HISTORY in PAPER_EXPERIMENTAL.

`paper_candidate(research_db, evidence_db, scan_id, source_hash=..., now=...,
mode='PAPER_EXPERIMENTAL', revision_hash=...)` reads the exact existing scans row
and existing persisted raw diagnostics through the accepted guarded Linux read
path. It reuses `assess`, `ReplayView`, `_BoundedStore`, token/holder/pool replay
validators and the live_features field contract. Source length is checked before
loading; existing 128-reference/load and 32 MiB replay caps remain. Both databases
are guarded for the entire read; no provider, mutation, fills or migrations.
The expected scan hash is a local comparison pin, not authentication. An optional
revision pin must match the actual persisted ownership head. Missing history
heads remain unresolved, never invented or retrofitted.

The decision records policy/version/mode, exact source and revision hashes,
evidence hashes, risk flag, original diagnostic gates and UNKNOWN ownership
fields/reasons. It never changes verified/complete/eligible/authenticated flags.
Default PAPER_STRICT preserves rejection. Other modes reject explicitly.

The separate pure `ownership_history_rule` expresses the limited policy waiver,
not an entry/source/control certificate. The adapter derives its observations
from actual persisted raw replay. The explicit risk can be accepted only when
raw token/holder/pool controls and report integrity pass, the investigation is
fresh and identity-bound, and no observed hazard/finding blocks. An allowlist of
explicit incomplete-history reasons prevents observed conflicts, unsupported
failures or generic BLOCKED statuses being relabeled as harmless incompleteness.
Unknowns outside that history set remain blockers. Current bundle/service
classification and numeric ownership fields are not fabricated or waived.
Token-2022 remains excluded. Known control hazards always reject; unavailable
controls remain unknown and reject rather than being called a known hazard.

## Concrete current production dependency

At this base, `live_features.candidate_snapshot` deliberately returns UNKNOWN for
all current strategy fields. `entry_evidence.evaluate` always blocks sellability
and strategy inputs. There is no persisted validated current market event
read path to establish current price/SOL-USD, exact entry quantity and cost,
current per-component observation times or authenticated complete route.
The existing `model.validate_event` requires numeric `top10_pct`, `dev_pct`,
`bundle_pct`, `cluster_pct`, while this policy must preserve their UNKNOWN/null
values. A gross holder concentration is not private ownership and cannot fill
them. Thus the actual adapter returns precise blockers even when the history
risk rule is accepted: it cannot honestly emit an executable event yet.

Minimum next integration decision (coordinator/worker09, not implemented here):
keep UNKNOWN ownership metadata on the paper decision and provide a reviewed
current market/quantity/cost/route producer compatible with the existing event
contract. An event-consumer change is needed to preserve unknown ownership
instead of inventing numerical zero. This exception does not waive any market,
token-control, pool, freshness, route or accounting prerequisite. No engine
approval or fake positive fixture is provided. Synthetic fixture origin is stated
in tests; persisted provenance is never rewritten as SYNTHETIC_TEST_ONLY.

Some existing pool exceptions collapse into POOL_RAW_EVIDENCE_UNAVAILABLE.
This module cannot recover a detailed hazard category from that field; it
retains the reason as a blocker and does not broaden the validator or infer
that a malformed/missing pool is safe. Existing explicit hazard codes remain
separate from these unresolved inputs.

## Validation

Dedicated tests use actual production pool decoding/PDA/LP controls and raw
holder/mint replay in synthetic persisted databases. A fresh, source-bound
fixture accepts only the history risk in explicit paper mode while still
blocking the unavailable market event, cost, freshness and route. Active mint/
freeze, delegated/unknown controls, Token-2022 and withdrawable LP never receive
a waiver. Tests retain unknowns, reject stale/future data and mismatching pins,
refuse caller summary booleans, verify read-only storage and unchanged old assess,
and reject observed/unsupported history conflicts. No network is used.
Exact final results and source identity are recorded in the PR.
