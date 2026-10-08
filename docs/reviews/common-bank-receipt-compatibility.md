# Protected mixed-profile receipt compatibility contract

Design prerequisite only. Base: `335e086e18a9e514a4431b7429d69e865bc163e2`.
This change contains documentation and offline synthetic fixture proofs, no
production migration, issuer, journal completion, provider calls or approvals.
It builds on common-bank-acquisition.md and the accepted original-parent view.

## Existing contract and refusal boundary

`PoolReceiptLedger` is one canonical protected SQLite file, canonical lock and
pinned evidence store. Its immutable version-1 descriptor includes file/root
identities, source registry, fixed `pumpswap-legacy-vault-point-v1` PROFILE,
fixture permission and freshness. `ApprovedSource.profile` must equal PROFILE.
Changing the registry/profile is not migration: it fails boundary/descriptor
validation. Existing payloads are exactly `{version:1, profile:PROFILE,
receipt:<AcquisitionReceipt fields>}`. Four EvidenceRefs bind original exact-six
snapshot/request and actual-slot block-time/request records. Union RPCs are
not v1 captures; neither replacing refs nor slicing the union creates one.

Every receipt has a contiguous sequence, unique publication ID and payload hash;
row hash covers sequence/publication/payload hash/previous hash. The descriptor
hash seeds the chain. The mutable head pins count and final hash. Full audit
precedes every policy/publication. Policy selects all pool/mint/actual-slot
receipts, capped at 256, without filtering source or freshness. Admission compares
all six raw account tuples `(owner, executable, lamports, decoded bytes)` and
block time; stale, malformed or missing competing evidence is not ignored.
The ledger protects records from ordinary mutations, not a malicious same-UID
issuer, whole-file rollback or omitted captures. Source approval is local issuer
configuration, not RPC authentication or on-chain finality.

Required schema SQL is checked exactly, but EXTRA tables/triggers are presently
not rejected. Merely adding a version table cannot fence old binaries. A separate
union ledger would also partition contradictions and is forbidden.

## Explicit dispatch in one physical ledger

Future format 2 must use this same ledger/evidence/lock identity and one ordered
observation chain. Preserve ALL existing descriptor body/hash, payload TEXT,
payload hashes, previous hashes, row hashes, sequence and publication IDs byte
for byte. Do not reserialize v1, change its PROFILE or seed, UPDATE existing
receipts, replace files, copy databases or recreate a ledger from surviving rows.
The unchanged descriptor remains the legacy trust seed, not an assertion that
all future observations have its profile.

Introduce an immutable, explicitly authenticated local migration certificate
with format/version, exact old descriptor hash/count/head, new dispatch/schema
hash, coordinator-approved versioned source capabilities and its own content
hash. Registry extensions authorize `(source ID, kind, network, genesis,
payload version, profile)` exactly; they do not overwrite the original registry
or retroactively authorize old records. Historical issuer permissions remain
part of audit even if future issuance is disabled; revocation must not remove
that issuer's prior contradictions from scope. Bound each new payload to that certificate
hash. The certificate is integrity evidence under the existing trusted issuer
assumption, not a signature asserting remote authenticity. Append a chain-bound
migration event: its canonical versioned payload and prior head bind certificate
and source permissions. Use the existing sequence/hash algorithm for typed events;
count all events in the 10,000-record ceiling. Legacy rows stay receipts. Define migration-event index columns as exact reserved
sentinels in the new schema (not valid pool/mint/source addresses); strict typed
audit verifies those columns against the event. No candidate can publish that
type, and it is never returned as a bank observation. Dispatch
must distinguish migration events from observations and reject every unknown
version/profile/type combination, never reinterpret unknown payloads as v1.

Suggested union receipt profile: `pumpswap-common-bank-parent-point-v2` and exact
payload version 2. It binds issuer/publication and journal/capture identity,
scan/admission descriptor/source/seal/revision/budget references, mint/pool,
actual T, block time, ORIGINAL capture wall time, and stored manifest hash.
The manifest binds discovery, parent and original request plus genesis/floor/
clock records. Reconstruct P/F/U and indices with `validate_common_bank`, not
caller completeness summaries. The payload must not embed a sliced RPC response.
A valid view alone proves none of the source/seal/budget/journal permissions.
Those must be separately replayed by the future authorized issuer/consumer.

## Atomic offline migration and recovery

No migration of live data is implemented or authorized by this PR. A future
migration entry point must be coordinator-only and offline/quiescent, with
expected canonical identities and exact old head supplied from trusted review.
First take the established evidence invocation lock, then ledger exclusive lock
(follow the acquisition lock order; never reverse it). Open the ledger without
creation in a transaction; re-audit required schema, descriptor, entire chain,
source permissions and every retained scope's evidence. Establish no in-flight
issuer/publication across either profile. Without an authoritative completion
inventory this prerequisite is unavailable and migration MUST refuse. Worker02's
unfinished completion API is not assumed by this design.

Refuse partial/missing tables/triggers, changed identities, ambiguous heads,
unavailable original records, unresolved pending captures, resource overflows,
unknown legacy source permissions or inability to establish quiescence. Preserve
all bytes, requests and charged counters. Do not salvage, delete, mark complete,
refund, refresh capture times or query a provider to repair migration. Missing
historical observations cannot be reconstructed from an empty new ledger.

Under one SQLite `BEGIN IMMEDIATE`, install certificate/event/format schema and
an explicit old-binary fence, then COMMIT with DELETE journal/FULL sync. The fence
must change a required v1 schema object's exact SQL, e.g. replace
`receipt_no_replace` with the reviewed format-2 collision guard. Consequently
old readers AND writers fail `_audit` before returning policy or appending.
New format-2 readers audit the exact new schema and certificate, enforce every
legacy immutable guard including rowid aliases, and validate both pre-transition
v1 and post-transition typed chain. Do not relax immutability in the new guard.
Additional version metadata alone is insufficient, as the fixture proves.

Crash before commit rolls back ALL DDL/event/head changes to usable original v1;
crash after commit yields complete format 2 or refusal. Retry takes locks,
re-audits and returns the same certificate/head only for an exact migration
identity; mismatches refuse. No partial-state initialization and no file rotation.
Once v2 observations exist, downgrade is unsupported. Operational recovery uses
reviewed version-capable software against the same files; backups may be forensic
inputs, never an automatic rollback to a less complete authoritative history.

## One cross-profile scope audit

For `(pool,mint,T)` gather every retained v1 receipt and v2 parent observation
from every authorized source, plus unresolved capture markers. Audit the FULL
ledger chain before scoped decoding, with one read transaction/guard. Do not
filter by profile, source, selected refs, capture freshness or current acceptance.
Malformed ledger metadata blocks the whole ledger. Known-scope unavailable or
incomplete observations block that scope, rather than being treated as absence.
Unknown actual slot but known pool/mint blocks all that pool/mint's slots until
an authoritative exact completion resolves the marker; unknown identity blocks
the ledger. Markers themselves must be protected chain events from the issuer,
not caller summaries. Finalization appends references to originals, never removes
old failed/partial observations or refunds their charged request attempts.

For each valid v1, replay its ORIGINAL exact-six request and actual-slot clock.
For each v2, validate its ORIGINAL union parent and persisted discovery, then
read pool indices from that parent. Compare six semantic raw tuples in canonical
P order, exact T and clock across BOTH profiles. Indexing a parent into a local
comparison tuple is permitted; saving that tuple as an RPC response is forbidden.
Compare ownership mint/vault joins directly to the original parent too. Holder
nulls remain absent-at-bank/unverified-lifetime, not zero/proven-closed; required
six pool accounts must remain nonnull. No lifecycle, historical interval, caller/
CPI authority, complete frontier-at-T, source authenticity, Token2022 support,
entry or common/private-control approval follows from this comparison.

Different original request floors S are allowed if each T>=S and both actual T
and state/time match. Later T is a different scope, NEVER historical state at S.
Contradiction is retained even if the conflicting receipt is expired; freshness
only gates use of a selected observation. Duplicate publication is idempotent
only with exact canonical bytes/certificate; a new ID cannot hide prior attempts
or conflicts. Unavailable, incomplete and stale inputs must be surfaced distinctly
and cannot be silently dropped. A scoped union lacking authoritative completion
cannot produce a usable mixed policy even when its local view validates.

## Bounds and concrete implementation boundaries

Retain 10,000 total chain records, 8 KiB per receipt/event, 256 total scoped
observations/markers across BOTH profiles. Bound registry permissions to 256,
with version capabilities inside the same limit; do not get 256 per profile.
Original parent <=64 KiB, discovery <=2 MiB, <=18 discovery pages, U<=100 keys,
and shared ReplayView <=128 referenced objects/32 MiB across all competing
observations. Abort on reaching a limit; no truncation, oldest-first eviction or
separate per-profile cache. These tighter shared bounds may honestly refuse a
256-observation scope before all evidence can be replayed. Migration certificates
also fit 8 KiB or refuse; do not exempt registries from a byte bound. No RPC budget
changes: same durable original 18-attempt counter, reservation before every I/O.

Proposed follow-up API boundaries (not functions introduced by this PR):

* `migrate_receipt_format(boundary, expected_legacy_head, reviewed_capabilities,
  quiescence_evidence)` validates and atomically installs one exact transition.
* `publish_observation(publication_id, typed_receipt_or_marker, certificate_hash)`
  is issuer-only, immutable/idempotent, never accepts candidate authority flags.
* `mixed_policy_view(pool,mint,T)` holds canonical guards/read transaction and
  returns typed originals, certificate and all unresolved scope markers. It must
  NOT coerce a union into `TrustedSourcePolicy`/v1 `EvidenceRefs`.
* `audit_mixed_point(view, shared_replay)` dispatches v1/v2 strictly, returns a
  bounded diagnostic proof/refusal containing all examined refs/head, and keeps
  all eligibility/interval/private-control flags false. Source-bound ownership
  joins remain a separate explicitly reviewed consumer boundary.

Receipt issuance, completion-inventory authority, migration implementation,
version-capable consumers and protected unresolved-marker publication remain
implementation dependencies. Current code must continue to refuse mixed receipts.

## Fixture evidence and honest limits

`tests/test_common_bank_receipt_compatibility.py` reuses saved fixture setup,
constructs one explicitly synthetic original UNION response, validates its
persisted discovery/request/manifest and compares its six account states with
an existing exact-six original at T=20/time=110. It checks conflicting raw state,
clock and unavailable discovery refusal; expired original v1 conflicts and
missing competing records still block fresh selected evidence; original v1 ledger bytes/hashes remain
unchanged on reads and refused registry changes. Current v1 request binding rejects the
original union; extra format metadata does NOT fence v1; changing a required
trigger does fence reads/writes, while rollback restores the exact original
schema and chain. Tests demonstrate these existing boundaries, not an implemented
migration or mixed policy. There is no fabricated six-account RPC response,
provider, journal completion or receipt issued for the synthetic union.

Validation on Linux/Python 3.12.14:

* Focused: `python -m unittest tests.test_common_bank_receipt_compatibility -q`:
  11 tests, 2.078 seconds, OK, zero skips.
* Full: `python -m unittest discover -q`: 1,638 tests, 79.653 seconds,
  OK, zero failures/errors/skips. Includes existing private-loopback fixtures.
* `git diff --check`: clean. Only this report and its dedicated test module change.

Preliminary focused runs exposed test-harness mistakes: incorrect trigger name,
fixture amount above supply, frozenset comparison and PolicyView loader attribute.
Those were corrected in tests/documentation; no production behavior changed.
No known failing tests remain. Migration/issuer/completion/consumer dependencies
above remain unresolved by design; no live acceptance or receipt issuance claimed.
