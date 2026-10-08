# Ownership acquisition recovery integration regression

Base: integration/cloud-wave1 c578dbead9931762a35a9a51b45b6aad62e1597c
(includes accepted PR57). Branch: codex/cloud-ownership-recovery.

Scope: one dedicated integration test file plus this report. Inspected production
ownership_acquisition, ownership_worker, history_progress and fenced JobPersistence
recovery. No uncovered correctness defect was established by these probes, so no
production modules changed. No raw capture or pool journal changes.

Existing acquisition tests already cover setup/provider errors, mocked
prepare/seal/archive interruption, wrong database identity, immutable setup,
queue fencing, two-page acquisition and the four-attempt acquisition ceiling.
Existing ownership integration tests cover bank interruption and request locks.
The new tests connect these mechanisms through actual Linux process deaths and
one admitted scan, then drive the same durable counter to its 18-attempt ceiling.
They test cross-stage recovery rather than add isolated API/count assertions.

## Fixture and boundaries

Synthetic legacy Pump launch from tests.test_ownership_integration.synthetic_launch;
no private or live records. Production SQLite persistence, mint policy, instruction
replay, inventory, source prepare/seal, archive handoff, Jobs recovery and snapshot
reconciliation execute unchanged. Only read-only RPC replies and explicit death
hooks are fixtures. A separate append-only fixture ledger fsyncs each invocation
and checks its reservation through another SQLite connection before returning.

Each scenario:

1. Starts with an admitted QUEUED acquisition. A child exits immediately after
   atomic cutoff publication (two requests spent, source still RUNNING).
2. A new child invokes public acquire, recovers/fences the RUNNING job, reuses
   the setup hashes and exits after the first history checkpoint commits (three
   requests spent). The saved cursor is `next` and acquisition cutoff remains 20.
3. Public acquire reopens the persisted checkpoint and makes only the cursor
   request; it seals/publishes the exact four-attempt source. The immutable
   archive retains both acquisition page attempts and exact source/query/coverage.
4. Public ownership advance captures finalized bank 20, then the child dies in
   getBlockTime after reservation (six total attempts spent). Restart retains the
   bank hash, charges a clock retry and an account-history failure (eight spent).
5. Account-history outages each consume one further attempt. Two variants either
   establish coverage on attempt 18 or fail that attempt. All earlier failure
   snapshots are unapproved; the missing-coverage variant remains unreconciled.
6. Reopening acquisition and ownership twice at the ceiling makes zero RPCs,
   preserves the source/setup/archive/bank rows and keeps requests_used=18.
   Missing coverage explicitly reports INVESTIGATION_REQUEST_BUDGET_EXHAUSTED.

The fixture would return cutoff 900 or bank 901 if recaptured. Each trace instead
contains exactly one getSlot, one getMultipleAccounts, and reserved counts 1..18.
The original source remains calls=4 after continuation; admission preparation
usage remains 4, while the one shared counter includes all failed/ambiguous I/O.
A zero-baseline continuation row does not erase the archived acquisition spending.
Both variants retain eligible_for_trading=false, including reconciled arithmetic.

## Remaining live blockers

This establishes synthetic durable recovery, not chain authentication or ownership
acceptance. Coordinator-only live acquisition, finalized bank/source provenance,
complete mint/account histories, supported launch/control semantics and ownership
review remain required. Token-2022 and unknown launch/control gates are unchanged.
A charged request interrupted before response publication remains ambiguous and
may consume a charged retry; no budget is refunded. Evidence exceeding the fixed
acquisition four-attempt or shared 18-attempt ceiling remains partial/blocked.
No production records may be retrofitted or rewritten to recover lost budget.
Trusted stable database/sidecar paths and local filesystem remain assumptions.

No providers/VPS/secrets/signing/broadcasting/funds/paid services were accessed.
No entry enablement, readiness/queue edits, extra workers, merge or deployment.

## Validation

Python 3.12.14, Linux, fixture-only:
- New dedicated integration scenarios: 2 tests in 0.484s, OK.
- Targeted acquisition/ownership/history/recovery group: 47 tests in 1.334s, OK.
- Full unittest discovery: 1338 tests in 49.853s, OK, zero skips/failures.
- git diff --check: clean.
