# Read-only legacy control obligation inventory

Source: accepted integration `ae3301348a77dd8a6ddfbb93e73a5e78210aa87e`.
Coordinator dispatch authorizes this projection of the obligation-inventory
contract in `token-control-certificate-proposal.md`, not a new certificate or
semantic acceptance contract. Only `desk/control_obligations.py`, minimal
`desk/live_features.py` integration, dedicated tests and this report change.

## Output and evidence boundary

`candidate_snapshot` now includes `control_obligations`. The read-only inventory
independently calls the accepted `continuation_snapshot` raw consumer with the
persisted source row and exact selected head hash. It does not consume the
decision runner's summary or a caller's reconciled/eligible/control flags.
That consumer binds source report hash, scan/mint identity, original request
counter and ceiling18, persisted jobs/coverage, canonical bank/clock and raw
request/page hashes. The output records the source/revision, evidence hashes,
actual bank slot/time, original observed_at, used counter and manifest hash.
It makes zero provider calls and writes no records or counters.

Two explicitly scoped components may be observed:

- `endpoint_controls`: named `legacy-endpoint-controls-v1`, applying existing
  legacy mint/holding policies to persisted discovered frontier bytes at actual
  T. It exposes raw token authorities and amounts, not beneficial ownership.
  Frontier completeness and historical authority invariance are separate
  obligations. Legacy owner-equal explicit close authority remains allowed;
  active delegates even with zero allowance remain rejected. No decoder or
  policy semantics change.
- `historical_accounting`: reconciled only by independent raw history-to-bank
  replay. Provider-declared completeness/finality is the existing conditional
  component contract, never chain authentication or proof of individual CPIs.

Historical controls, initialization final completion, parent noninterference,
individual CPI outcomes/privileges, historical deployment/runtime identity,
source/finality/history authentication and transaction-bound birth bytes always
remain separately unresolved with exact static blocker codes. Seven approval
flags remain false, including common-control, ownership and trading. The outer
candidate remains REJECT with all existing unknown strategy fields preserved.
Actual T is never relabeled as initialization S. Stale observations retain their
original age and historical component scope, with an explicit freshness blocker.

Compatibility source modes are explicit: `SEALED_ADMISSION` consumes the exact
accepted seal; `LEGACY_COMPLETED_SOURCE` is the accepted historical-fixture
fallback, not a claim of a newly provisioned/authenticated admission. A present
but invalid seal cannot fall back. Local content hashes prove record identity,
not provider honesty, network identity, canonical-chain inclusion or finality.
Token-2022 remains excluded; disconnected structural diagnostics cannot promote
these components.

## Read consistency and bounds

Both local databases stay under the accepted Linux LP64 shared OFD rollback
preflight guard during candidate evaluation. This blocks standard SQLite POSIX
writers and journal transitions even when SQLite reader connections close.
WAL mode, any journal/sidecar, contention, unsupported filesystem/kernel/VFS or
invalid owned-file boundary fails closed before SQLite opens. No immutable WAL
read, checkpoint, copy, retry, repair, lock-file creation or journal SQL change
is introduced. The public inventory guards its evidence DB; trusted callers
supply the exact source row, whose hash is independently checked against the
persisted budget/revision. `candidate_snapshot` additionally holds the research
guard while acquiring and consuming that row.

Deployment contract: stable canonical owned regular files, one hardlink, no
group/world write, positive local filesystem classification, standard SQLite
POSIX locking, and no rename/replace/hardlink by cooperating workers while in
use. Supported diagnostic filesystems include tmpfs for volatile local fixtures;
this does not change the receipt writer's durable-filesystem policy or certify
durable tmpfs capture. Active WAL evidence is unavailable rather than silently
ignored. Supporting live WAL would require a separately reviewed nonmutating
consistent-reader contract.

A trusted local immutable-record cache shares physical reads across the two
independent replays, preserving the existing 128-load/32MiB boundary. Each cached
record is hash-checked and copied before use. The inventory itself enforces
128 unique hashes/32MiB and a2MiB source limit, including direct API use. Existing
request/page/order limits and the original18 counter remain authoritative;
there are no new acquisitions or renewed investigations.

## Boundary reproductions

Dedicated tests reuse the accepted three-account/four-transaction/eight-page
legacy fixture. Acquisition uses13 charged requests, including the original3;
projection performs zero further requests. It exposes clean endpoint and
reconciled accounting, exact request/page refs, original age and false approvals.
Repeated projection preserves both DB bytes and filenames, original rows and
counter. A sealed11-request fixture independently verifies seal consumption and
refuses a rebound completed-source hash.

Adversarial projections cover forged positive progress flags/slot/timestamp,
rehashed refreshed source, foreign scan/mint, absent and tampered hash-addressed
pages, fabricated payload hashes, missing account query/frontier, counter reset,
active zero-allowance delegate, Token-2022 owner substitution and resource
exhaustion. Every failed binding or raw read leaves positive components unknown.
An approve/revoke pair restored before T leaves the endpoint observable but
accounting unresolved; historical-control approval remains false throughout.
Substituting T21 while raw histories end at T20 reports actual21, blocks accounting
with `HISTORY_SNAPSHOT_SLOT_BOUNDARY_UNVERIFIED`, and cannot fill birth-state
obligations. Stale evaluation at131 preserves observed120 and adds the freshness
blocker. WAL reads preserve all filenames/bytes without creating sidecars.
A separate-process writer after replay's SQLite connection closes still cannot
reset the counter, verifying revision/accounting stability under the read guard.

## Remaining prerequisites

Independent review and coordinator integration are outstanding. No ownership,
entry or paper-readiness signoff is supplied. Stronger control proof needs a
reviewed, obtainable frontier/completeness and initialization-completion
contract; slot-applicable authenticated deployment/runtime/caller semantics;
completed CPI/authority/privilege evidence; noninterference across System-owned,
token-owned and close/recreate intervals; approved source/genesis/finality trust;
and transaction-bound archived S bytes or explicitly reviewed semantic
equivalence. Current RPC T, parsed events, hashes, same-provider corroboration,
official IDLs or summary flags cannot discharge those obligations. Classification,
current-holder exposure, route, live strategy, monitor/exit/accounting/restart and
forward-observation dependencies remain with their owners and blocked.

## Validation

Python3.12.14, synthetic/mock transports and local SQLite only. No provider,
credential, VPS, signer or broadcast access.

- New projection plus existing live-features tests:28 tests in2.551s, OK.
- Focused projection/live-features/multi-history/control/sealed-continuation/
  continuation/snapshot suite:93 tests in4.512s, OK.
- `.venv/bin/python -m unittest discover -s tests`:1,489 tests in65.147s, OK;
  no failures, errors or skips. Existing unrelated loopback fixtures ran with
  sandbox permission; no external provider requests were made.
- `git diff --check`: clean.

An initial test-file assembly placed seven existing new boundary methods under
the wrong fixture class (seven AttributeErrors); corrected before the above
final runs. No known test failures remain. Tests validate the narrow diagnostic
boundary and accepted conditional components, not stronger semantic proofs or
live provisioning.
