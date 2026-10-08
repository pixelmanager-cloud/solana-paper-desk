# Bounded diagnostic raw transaction capture

Base: integration/cloud-wave1 076c9ad87e019bd458a444b59973186565aea5fd.
Scope: desk/raw_transaction_capture.py, dedicated tests and this report only.
No mint policy, unsupported-token acquisition gate, descriptor, ownership worker,
entry, RPC transport, capture ledger, queue or readiness changes.

## API and admission boundary

Construct `RawTransactionCapture(existing_evidence_store,
ReadOnlyRPC(source_id, injected_rpc))`, then call `capture(TransactionBinding(
scan_id, existing_admission_descriptor_hash, mint, signature))`.

This is a trusted local coordinator API, not candidate JSON dispatch. The injected
read-only dependency must execute exactly one request per call, with no hidden
retries, discovery, credential setup or other provider calls. source_id labels
provenance and is bound immutably; it does not authenticate the source or chain.

The store must already contain the admission/budget foundation and an existing
ownership_admission_v1 record matching scan ID, descriptor hash and mint. There
are no calls to admit(), budget(), prepare_source() or seal_source(). No new scan,
budget row, ceiling increase or source-report mutation is performed. Existing
legacy completed-source budgets without admission metadata are unsupported: this
component does not migrate or invent their descriptor bindings. Investigation
admission/day/queue controls remain upstream. This API is not permission to create
arbitrary admissions or investigate unlimited signatures.

Exactly ONE immutable signature/source capture claim is allowed per admitted
scan in its canonical evidence DB. There is no user-supplied capture ID. A fresh
signature or source cannot replace a pending, failed, rejected, refused or completed
claim. Canonical database path is persisted in the claim, so copying an enrolled
DB to another path blocks replay rather than recapturing. Symlink aliases share
locks; hardlinks are rejected. The inherited stable-file contract forbids operators
from replacing/renaming/linking the database during use. Copy-before-enrollment,
privileged schema rewriting or whole-filesystem rollback are not authenticated by
the existing admission descriptor; no such provenance or rollback defense is claimed.

## Request, durable states and the shared counter

The only provider request is:

```
getTransaction(signature, {
  "encoding": "json", "commitment": "finalized",
  "maxSupportedTransactionVersion": 0
})
```

The original request object is content-addressed before the immutable claim commits.
The claim then acts as durable PENDING **before** HistoryProgress.reserve(), which
commits on the SAME existing at-most-18-attempt counter before provider I/O. Lock
order is canonical ownership invocation lock, then ownership request lock. Both
are nonblocking and shared with existing ownership/bank capture paths.

Interruption between PENDING and reservation leaves zero capture attempts spent
and an explicit ambiguous pending claim. Interruption after reservation retains
the charged attempt, regardless of whether I/O occurred or a result was saved.
In both cases restart makes ZERO provider calls; no caller can swap signature/source
to hide the pending attempt. This deliberately sacrifices automatic retry to keep
an unknown request from duplicating or hiding spending. Original request pages
created before a pre-claim crash are harmless idempotent orphan pages; no I/O or
reservation occurs before the claim exists.

After a successful injected call, the exact returned JSON object is saved under
an rpc_response_v1 envelope before validation; no original fields are rewritten.
This preserves raw compiled instruction bytes/account indices using the existing
canonical-JSON EvidenceStore contract, not original HTTP wire whitespace or signed
transaction bytes. Existing 16-MiB page and total evidence-storage caps apply.
If storage/publication fails, the attempt remains PENDING/blocked and charged;
recapture is forbidden. A saved response before terminal publication may remain
an orphan raw page: this component does not guess which response resolves an
ambiguous claim.

Terminal states:

- COMPLETE: saved raw response passed structural identity checks only.
- REJECTED: null/missing/failed/malformed/unbound/unsupported raw response, preserved.
- FAILED: injected RPC raised; charged, terminal sanitized failure record; no raw
  exception body/URL/credential is retained or exposed.
- REFUSED: PREPARED source freeze or exhausted request budget, zero I/O; immutable
  refusal cannot later be retried under the same scan.

Claims and terminal results are append-only with body hashes, exact schema checks,
UPDATE/DELETE rejection and INSERT OR REPLACE guards on primary/unique/row identities.
Rowid, _rowid_ and oid collision attacks are covered. Replay checks original request,
claim/descriptor/database/source identity, response hashes, terminal diagnostics and
reservation floor. Counter regression below recorded usage blocks; no reset or
recharge is performed. Source sealing/preparation bytes remain unchanged even when
a later diagnostic consumes an available SEALED budget attempt.

## Structural validation and unresolved evidence

The requested signature and returned signature list must be canonical 64-byte
base58 values, match the primary transaction signature and header counts, with no
duplicate signatures. Signature text is not cryptographic authentication.
Slot is exact unsigned uint64; transaction/message/meta must have expected shapes.
Existing compiled_keys resolves explicit legacy or v0 static/lookup segments;
requested mint must be present in that exact resolved key set. Ordered outer/inner
indices and raw instruction data, unique inner parents, bounded instruction count,
optional stack heights and block-time shape are checked. Parsed-only instructions
are rejected as missing raw witnesses; no fields are synthesized. Failed transaction
metadata is preserved and rejected, not converted to successful accounting.

COMPLETE does not authenticate finalized chain state merely because finalized was
requested or err was null. Every returned finality/signature/source/caller/CPI-
success/account-state/lifecycle/ownership/trading approval flag remains false.
No birth time, lifetime, endpoint control, effect order or entry permission is
inferred. Raw state, caller privileges and complete control histories remain separate
unresolved prerequisites. No raw decoder is used to bypass a token policy gate.

The integration fixture admits an unsupported Token-2022 research seed using one
request, captures its diagnostic raw transaction using the same counter's second
attempt, and verifies ownership advance still returns UNSUPPORTED_TOKEN with zero
additional I/O. The original COMPLETE scan report (calls=1), seal and source bytes
remain identical; cumulative requests_used becomes 2. No acceptance is granted.

## Transport dependency and validation

The generic helius_rpc adapter on this exact base does not allow getTransaction.
The coordinator has since independently reviewed/integrated **PR49 at
2b11fbf6c8a5d7933088283a9a7ab9cbd9f25578**: its dedicated HeliusMainnetRPC allows
only the exact finalized/json/version0 getTransaction request. This capture remains
injected and does not import, construct or invoke that transport. Its separate
integration/source verification and coordinator-only live acceptance remain
prerequisites; no runtime path is enabled. Injecting the unsupported generic
adapter would still produce a charged terminal failure; this component neither
bypasses an allowlist nor silently retries.
All tests inject synthetic read-only fixtures; no provider, VPS, secrets, signing,
broadcasting, merge or deployment was performed.

Dedicated tests cover persisted request/response replay, loaded mint resolution,
invalid bindings and missing/legacy budgets, malformed/failed/null raw results,
sanitized charged RPC failure, signature/source rebinding, shared 18-counter limits,
PREPARED/SEALED behavior and original source preservation, actual process death
before reservation/during I/O, publication crashes before/after terminal commit,
storage/SQLite publication failure, canonical symlink contention and both ownership locks, hardlinks,
enrolled database copies, schema/content corruption and row identity attacks.
Live evidence capture and ownership acceptance remain coordinator-only and outstanding.

Final validation on Python 3.12.14 (Linux):
- Dedicated fixture suite: 20 tests, 0.188s, OK.
- Full fixture-only unittest discovery: 1190 tests, 41.566s, OK (zero skips).
- git diff --check: clean. No known test failures.

## PR51 partial-schema repair

Independent review at 5bd4cd0854fa95470489cbbef00a3b8cb620c8db reproduced
recapture after deleting only the claims table following a charged publication
failure. Schema validation now distinguishes an absent journal from an existing
journal: first initialization creates every object in one SQLite transaction;
any existing capture object requires the exact complete table/trigger inventory.
Missing, altered or unexpected capture objects block without schema repair,
reservation or transport I/O. Operator recovery requires separate review.

Linux regressions remove each table and protection trigger after PENDING,
FAILED, COMPLETE and REFUSED outcomes, then retry the original binding, a new
signature and a new source. Every retry blocks and snapshots of surviving schema,
rows, original pages and budgets remain identical. Fresh initialization failure
rolls back all objects and spending, while subsequent pristine initialization
works. Existing process-death and publication-crash cases remain covered.
Complete privileged removal/reconstruction of the whole journal or database
rollback cannot be authenticated by this local journal and remains outside its
protection boundary; stable trusted filesystem/lock identity is still assumed.

Repair validation: Python 3.12.14/Linux dedicated suite 22 tests in 0.403s,
OK; full fixture-only discovery 1192 tests in 41.434s, OK, zero skips.
The partial-damage matrix covers 32 damaged state/object combinations and
96 blocked retry variants. git diff --check clean; no known test failures.
