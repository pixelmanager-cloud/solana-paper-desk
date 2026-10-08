# Admission budget and completed-source seal API

Implemented on a new branch from latest fetched `integration/cloud-wave1` at
`f2009acd09f846a305eca94b4179574920a845fa`, without rebasing an existing branch.
Scope: `history_progress.py`, ownership-worker binding, dedicated fixture tests
and this report. No Jobs/dashboard, decision journal or acquisition CLI changes.

## Successor contract

Use ONE scan ID, ONE evidence database and ONE existing `ownership_budgets`
18-attempt counter for admission, acquisition and ownership continuation. The
new `ownership_admissions` table stores only immutable descriptor/source lifecycle
metadata and recovery bytes. It is not another request ledger. Existing budgets
retain their original table schema, source hash, used count and ceiling.

`HistoryProgress.admit(scan_id, descriptor, ceiling=18)` accepts exactly:

```json
{"kind":"ownership_admission_v1","scan_id":"<same scan ID>","mint":"<public mint>","created":123}
```

`created` is a nonnegative integer admission timestamp, not an asserted birth
time. The versioned kind describes this budget API, not a Jobs dispatch kind.
The queue successor must construct this descriptor from its durable admitted
scan, not accept arbitrary identity changes from a retrying caller. Its own
kind/descriptor record can include additional queue metadata; this budget
projection has exactly the four keys above.

Admission creates usage ZERO, atomically inserting descriptor metadata and the
budget row. `source_hash` in this new budget row is the immutable descriptor
hash and is never replaced by the completed-source hash. Reopening requires the
same descriptor and ceiling; it returns existing usage and lifecycle state.
Existing completed-scan budget IDs cannot become admissions. A lower ceiling can
be used for fixtures; normal worker continuation expects 18.

`reserve(scan_id)` commits one attempt before setup/provider I/O. A failure or
process death after reservation remains charged. False means no request may be
sent; inspect `admission(scan_id)` to distinguish PREPARED from exhaustion.
History and bank capture already reserve their own attempts; never wrap them in
another reservation. `create/advance/seed` retain their existing history rules.

`prepare_source(scan_id, descriptor_hash, source)` takes the exact intended
completed scan projection with exactly these five fields:

```python
source = {"id": scan_id, "mint": mint, "created": admission_time,
          "status": "COMPLETE", "result": exact_report_json_string}
```

Use the original JSON string unchanged, including whitespace. Other future Jobs
columns do not enter this projection because ownership_worker selects only these
five columns. `result` must be at most 2 MiB and contain matching mint, a valid
`report_hash`, explicit `eligible_for_trading=false`, and integer `calls` equal to
ALL currently reserved attempts, including failures. This is source/budget
binding validation, not raw-evidence authentication or entry approval.

Preparation atomically stores the exact projection, its hash and the spent count,
then transitions ADMITTED -> PREPARED. The counter remains unchanged. PREPARED
freezes reservations through setup, history and bank APIs; history/bank attempts
return `INVESTIGATION_SOURCE_PREPARED` without provider calls. A different source,
including only a different result serialization, cannot replace a prepared one.
There is deliberately no unprepare/reset API.

`seal_source(scan_id, descriptor_hash, digest(source))` transitions PREPARED ->
SEALED once. It requires the stored descriptor and exact prepared-source hash.
Repeating the same seal is idempotent, even after continuation spends additional
requests. Mismatched seals fail. The seal does not copy/reset counters or mutate
source bytes.

`admission(scan_id)` returns descriptor/hash, lifecycle state, prepared source
projection/hash, preparation-time request count, current usage and ceiling.
Legacy completed-scan budgets return None. It validates stored metadata against
its hashes/counts and fails closed on inconsistent state. Treat prepared source
bytes as private research records; do not publish provider reports in fixtures.

## Queue and acquisition finalization protocol

The caller must hold the shared canonical ownership INVOCATION lock throughout
acquisition/checkpoint/finalization, distinct from the history/bank request lock.
The queue owner must also enforce its research-DB claim/generation fence. These
APIs do not implement Jobs admission, daily limits, dispatch, leases or fencing.
Symlink/relative evidence paths share existing canonical lock identity; hard
links and filesystem replacement while workers run remain unsupported.

1. After the queue transaction admits an immutable scan, idempotently `admit` its
   budget BEFORE any provider I/O. Crash between queue admission and budget
   admission leaves a zero-request recoverable job, not a new admission on retry.
2. Reserve each setup attempt and use existing charged history continuation.
   Persist raw evidence/cursors. No provider error bodies become report records.
3. Finish in-flight acquisition requests under the invocation lock. Construct the
   immutable report once from persisted evidence and current durable usage.
   `prepare_source` persists exact completion bytes before changing the original
   scan to COMPLETE. If preparation commits, perform no more acquisition I/O.
4. Seal the exact prepared source, then publish that SAME projection in the
   research DB under the queue claim fence. If publication was already performed
   with identical bytes, recovery may seal and verify it idempotently. If the
   existing completed row differs, fail closed; never silently rewrite it.
5. On restart: ADMITTED resumes acquisition; PREPARED reads stored completion bytes
   and seals/publishes them; SEALED verifies or publishes the exact prepared row,
   WITHOUT further acquisition calls. Retry does not change created time or admit
   a replacement scan. Ownership continuation starts only after the actual row is
   COMPLETE and the exact budget source is SEALED.

This is a recoverable protocol across two SQLite databases, not a claimed atomic
cross-database transaction. The successor must implement the fenced research-DB
publication step. Source data can be recovered even if a process dies after
prepare/seal and before scan publication. There is no refund/unseal after a
publication conflict; operator/coordinator resolution must preserve records.

## Ownership-worker compatibility

`budget(scan_id, digest(completed_projection), original_report_calls)` now accepts
new admissions ONLY when the exact completed source is sealed and the supplied
original calls equal the preparation-time usage. It validates the immutable
admission descriptor through the persisted side table. The current counter may
be higher after continuation, but is never reset to the original report value.
Legacy budgets retain their existing source-hash checks and initialization.

Ownership_worker performs this check before mint evidence/policy evaluation.
Unsupported tokens still perform no provider calls, and cannot bypass source
binding. Newly encountered legacy unsupported scans can acquire a budget row
bound to their original report; existing rows are not rewritten. No decoder,
Token-2022, history, pool or entry-evidence gate has been relaxed.

## Validation and remaining dependencies

Dedicated tests cover unchanged legacy schema/row/evidence bytes, identity and
ceiling rebinding, missing/forged report hashes, boolean counts/timestamps,
changed JSON serialization, prepared source freeze, seal mismatch/idempotence,
shared ceiling across acquisition/history continuation, concurrent reservations,
competing preparations, prepare-vs-reserve race, concurrent idempotent admission
and sealing, transaction rollback, actual subprocess death after reservation and
after preparation, database-backup recovery, and real ownership-worker sealed,
unsealed and modified-source handling.

Only fixture provider functions run. No live providers, secrets, signers, VPS or
broadcasting are used. Queue foundation belongs to worker08. Acquisition module,
CLI, raw-evidence receipt/cutoff handling, fenced cross-DB publication and bounded
live acceptance remain successor/coordinator work. Passing tests does not create
an acquisition command or establish ownership/paper readiness.

Exact final validation (Python 3.12.14):

- Focused: `python -m unittest tests.test_ownership_budget_admission tests.test_history_progress tests.test_ownership_worker tests.test_ownership_integration tests.test_snapshot_collection -q`;
  53 tests in 0.685s, OK.
- Full: `python -m unittest discover -q`; 737 tests in 13.822s, OK,
  no failures/skips. Execution permission was used for the existing temporary
  private loopback HTTP test. `git diff --check` passed.
