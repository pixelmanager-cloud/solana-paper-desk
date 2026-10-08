# Disconnected finalized-slot journal stage

Pinned accepted base `072f654478dd7a73563cfbc5b18092f1ed92829d`.
Scope: journal plus dedicated fixture tests/report only. This extends accepted
PR86's trusted-local observation persistence contract, not provider authentication
or any bank/receipt approval. Independent PR86 review remains relevant:
https://github.com/pixelmanager-cloud/solana-paper-desk/pull/86#pullrequestreview-5456789245.

## Additive profile and unchanged originals

`install_slot_stage()` explicitly installs three new WITHOUT ROWID tables:
`common_bank_slot_meta`, `common_bank_slot_intents`, `common_bank_slot_attachments`,
with immutable update/delete and insert-conflict/predecessor guards. It requires
the existing genesis attachment profile and audits all source/run/genesis rows
before installation. It atomically advances SQLite user_version from the accepted
genesis marker to `0x43425331`; existing application_id, original v1/genesis table
and trigger SQL, descriptors, plans, raw bytes and rows stay unchanged. No
constructor, ordinary read or begin call implicitly installs either profile.

The reader explicitly dispatches v1, genesis and slot markers. Unknown versions,
missing installed tables/metadata, unexpected triggers or altered profile
bindings fail closed without remigration. The old strict readers reject slot
extension tables; coordinator rollout must include this reviewed reader and
explicit installation together. Installing the profile on failed/ambiguous
genesis records does not grant transition or recovery authority. A still-known
genesis handle can finish after installation under its unchanged original
contract. There is no receipt profile migration or publication.

## Genesis DONE -> one finalized slot intent

`begin_slot(capture_id, fence=..., reserved_at=...)` requires an independently
reaudited genesis DONE attachment, exact immutable source/descriptor/plan/database
binding, current same18 budget, no fixed canonical bank and no prior slot intent.
Failed genesis, absent result and reopened genesis PENDING cannot transition.
The slot fence is the exact durable genesis completion hash, not the original
run seed or a caller-selected/rehashed summary. The ordinal3 slot intent names
that predecessor, fixed `getSlot` method and `[{'commitment':'finalized'}]`, original
charge/time and source/plan hashes. Boolean/negative/backdated/overflow reservation
times reject. Capture identities retain the bounded ASCII ID profile.

The SAME existing shared18 counter increment and immutable slot PENDING insertion
commit together in one BEGIN IMMEDIATE/FULL synchronous rollback-journal
transaction. The insert trigger witnesses current charge and exact DONE parent.
No new budget, refund or capture ID is permitted. Prior unrelated legitimate
shared spending stays included. The lower-bound preflight is now
`current_used + 4 + len(F) <= 18`: slot/union/clock plus at least one mint page and
one per frontier account. This is not a paging feasibility guarantee or an
extension of the18-RPC ceiling. Source PREPARED/changed source, stale fence,
exhaustion, fixed bank or prior slot reservation blocks before charging.

Only after durable intent commit is one opaque slot capability minted in the
current PID/thread/session registry. Its stage is fixed: genesis handles cannot
complete slot and slot handles cannot complete genesis. Request bytes are exact
canonical UTF-8 JSON-RPC2.0 with an intent-derived string ID and finalized options;
`slot_request(handle)` returns them unchanged. No request is sent by this module.
If intent commit succeeds but its result/registering handle is lost, the durable
PENDING is terminal on reopening; no replacement handle, retry, refund, rebinding
or replacement capture ID is possible.

## Exact bounded slot observation attachment

`attach_slot_response(handle, original_bytes, completed_at=...)` derives its
result from strict original JSON-RPC bytes. It requires exact response ID,
exclusive result envelope and **actual Python integer from JSON**, `0 <= S < 2^64`.
Booleans, floating values, strings, negatives, null/objects and u64 overflow are
retained as FAILED, never coerced. Duplicate/nonfinite/malformed/extra/conflicting
JSON or wrong ID follows PR86 strict parsing and remains byte-preserving FAILED.
A DONE event records the exact S, not a caller summary, adjusted slot or inferred
bank. The full response, original generated request and immutable ordinal4
completion insert in one transaction, hashing the original bytes and linking the
ordinal3 intent. Completion time is integral, ordered after reservation and not
refreshed on recovery. It is a local supplied observation time, not a trusted
clock proof. The reader rederives state, S, wire hashes and the complete parent
chain on every audit; changed/rehashed genesis cannot rebase an existing slot.

`attach_slot_failure` accepts only fixed redacted TRANSPORT_TIMEOUT,
TRANSPORT_ERROR or CANCELLED categories, retaining finalized request provenance
without arbitrary exception text/URLs/secrets. An oversized response becomes
bounded RESPONSE_OVERSIZED category plus original byte count: no parsing,
truncation, positive-prefix validation or invented original-body hash occurs.
Bounded original raw RPC-error bodies remain private byte evidence, not whole-body
secret redaction. All original rows/pages/source/history are preserved.

Identical raw observation/category AND timestamp may be acknowledged again in
that same still-live session; conflicts reject without mutation. The session
freezes its chosen observation before attempting the transaction. An SQL error
before or after commit permits only identical already-known observation
persistence/acknowledgment, never a transport retry. Actual process death before
completion commit leaves charged terminal PENDING; after commit it leaves exact
SLOT_DONE or SLOT_FAILED. Reopened sessions report durable completion but cannot
mint a completion capability or reserve slot again. Counter rewind below the slot
charge fails the full audit rather than reporting completion.

## Resource and authority limits

Each audit still shares one128-reference/128-physical-load/32MiB serialized
raw-discovery view across up to128 runs. create_run retains its separate second
bounded plan pass: at most256 references/loads and64MiB across both, not one32MiB
whole-operation window. Each source remains bounded2MiB and revalidated per run;
up to256MiB serialized source input across128 runs is separate from page-replay
ceilings. Sources/plans/events retain existing per-record bounds and finite run
count. This is not a Python RSS guarantee.

Slot adds at most128 intents and128 attachments, each event<=8KiB, generated
request<=1KiB, raw response<=64KiB, failure<=2KiB. Preflight length checks precede
fetch/parse; count queries use one extra sentinel row and reject the whole
oversized set before interpreting a prefix. One audit checks both genesis and
slot attachments, so up to16MiB serialized original response work (8MiB per
stage), plus bounded request/failure/event metadata, is additional to replay,
source and plan work. The stage audits are sequential; decoded Python memory is
not advertised as that byte total. install/begin/attach/resume each incur their
own audit; a whole workflow is not a single32MiB budget. Live capabilities and
known observation retention remain bounded by the existing per-run stage count;
exit clears their registry.

DONE means local request-ID/syntax/integer agreement with supplied bytes, not
proof of actual sent requests, original network provenance, authenticated
finality/genesis, route/CPI/source authentication or OWN-2/3 acceptance. Planned
ApprovedSource configuration and opaque handles cannot grant such trust. Existing
result-only RPC adapters cannot supply original wire provenance; future separately
reviewed transport must send the unchanged bytes and return originals. This
module performs no provider I/O. Provider_calls stays0 and all exposed
eligibility/ownership/chain flags stay false. Trusted process/configuration,
stable owned Linux local paths, accepted lock order, rollback SQLite and ordinary
SQL integrity remain assumptions; hostile trusted-process introspection,
whole-file/header rewrites/historical rollback or lock replacement remain outside
the boundary. No provider transport, union/clock reservation or I/O, bank/head or
receipt publication is implemented. Worker01's receipt validator is untouched.

## Validation

Dedicated fixtures use the actual accepted raw discovery/decoding and existing
SEALED source/budget. Tests cover oldschema SQL/byte preservation and explicit
installation, strict integer/u64 boundaries, stage/fence/session authority,
failed/ambiguous genesis rejection, exact raw completion/duplicate/conflict,
oversized positive prefix, redacted failures, shared spending and rewinds,
rehashed parent substitution, stale source, restored-trigger body corruption,
missing schema and fresh SQLite replace/update/delete/rowid aliases.

Actual spawned processes exit before/after reservation commit, before/after DONE
commit and after FAILED commit; a two-process race permits one charge/attachment.
Injected before/after reservation commit errors mint no authority; before/after
completion errors retain only the same known result. Synthetic positive S is not
live source acceptance. Full Linux fixture-suite and exact final targeted results
follow below. No provider/VPS/secrets/signing/broadcasting, shared readiness/queue
edits, merge or deployment.

Final Python3.12.14 Linux fixture results:

- `python -m unittest tests.test_common_bank_slot_stage -q`: **21 tests in13.620s, OK** on final production source.
- Targeted slot/v1/genesis/common-bank-view suite: **81 tests in35.722s, OK** before the final bounded capture-ID ingress check; the dedicated final run and full final run include that check.
- `python -m unittest discover -q`: **1678 tests in124.920s, OK; zero skips/errors/failures** on the pinned base plus these three files.
- `git diff --check`: clean. No known failures. Earlier developing16-test slot and35-test unchanged v1/genesis runs also passed; final results include all21 scoped tests and the final source guard.
- Full fixture suite uses existing authorized private loopback networking only; no provider/VPS calls. No additional existing-module hook, shared documents/queue edits, merge or deployment.
