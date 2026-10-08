# Initial genesis observation attachment

Base: accepted integration `335e086e18a9e514a4431b7429d69e865bc163e2`.
Scope: disconnected journal completion only, following independent PR84 review
https://github.com/pixelmanager-cloud/solana-paper-desk/pull/84#issuecomment-6059560603.
No provider calls, next-stage reservation, bank/head writes or receipt publication.

## Known in-flight observation versus ambiguous PENDING

A session must explicitly `install_genesis_attachments()` before using
`begin_genesis(capture_id, fence=..., reserved_at=...)`. The latter invokes the
accepted SAME-transaction shared18 counter increment plus immutable v1 PENDING
intent, then mints an opaque object registered only in that live session. Intent
hashes, copied dictionaries, new objects, another session and inherited fork or
thread capabilities cannot attach results. The handle is not an authentication
certificate; process-private registration prevents API callers from manufacturing
recovery authority. Malicious trusted-process introspection, SQL/database/header
rewrites and operator lock/path replacement remain outside this local boundary.

`genesis_request(handle)` supplies the exact generated UTF-8 JSON-RPC request
bytes (`getGenesisHash`, empty params, intent-derived string ID). A future reviewed
transport must send those bytes unchanged. This module does not send anything or
prove a request was sent. The same live handle accepts original response **bytes**
or a fixed redacted failure category; it does not accept result dictionaries,
summary/genesis strings, mutable bytearrays, caller success flags or source flags.
The API preserves byte identity rather than reconstructing an original response
from a parsed summary. Caller-supplied bytes remain a local observation until a
separate trusted transport boundary is implemented; matching GENESIS is not
source, finality or cryptographic chain authentication. The existing result-only
RPC callable cannot supply this raw-wire provenance; constructing JSON-RPC bytes
from its returned result would invent framing and is not supported. A separately
reviewed future transport must provide the original bytes at this boundary.

DONE is derived only from strict bounded JSON-RPC 2.0 response syntax, exact
request ID, exclusive result envelope and exact GENESIS string. Duplicate keys,
nonfinite JSON, malformed/nonobject/deep JSON, extra/conflicting fields, substituted
ID, RPC errors and wrong genesis are retained byte-for-byte as FAILED observations.
A >64KiB response cannot validate a prefix: it becomes a bounded redacted
RESPONSE_OVERSIZED failure containing the original byte count, without hashing,
parsing, retaining or truncating the oversized body. Other failures accept only
TRANSPORT_TIMEOUT, TRANSPORT_ERROR or CANCELLED; no exception text, URL or secret
is admitted. They retain the original request plus canonical redacted provenance.
Timestamps are strictly integral, ordered local caller observation timestamps,
not authenticated clocks.

Attachment reaudits exact SEALED source, admission, plan, database identities,
current counter, fixed-bank absence and exact predecessor intent, under the
accepted research-worker -> invocation -> request lock order and read guard.
The completion hash binds original byte hashes/redacted failure hash, planned
source, descriptor/plan/fence, original reservation time and charge, completion
time and ordinal-1 intent hash. One immutable ordinal-2 attachment is inserted in
one FULL synchronous rollback-journal transaction; no new counter charge occurs.
The response and completion cannot commit separately. No page/source/history
record is rewritten. All completion and status eligibility/ownership/chain
approval flags remain false, and this module's provider_calls remains zero.

Identical completion bytes/category AND timestamp are idempotent in the same live
session. Different bytes, category or timestamp conflict without mutation.
Before attempting commit, the live session freezes the chosen observation. After
an SQL error before or after commit, it may persist/acknowledge only that SAME
already-known observation; this is persistence, never another transport attempt.
An error before commit leaves only charged PENDING; an error after durable commit
leaves the exact completion. Process death/session exit clears the handle. A
reopened PENDING remains TERMINAL_UNCERTAINTY even if somebody later supplies a
plausible response: no retrofit, retry, refund or replacement capture ID is
possible. A reopened durable completion reports GENESIS_DONE/GENESIS_FAILED,
without minting attachment authority or reserving a next stage.

## Explicit additive profile, original v1 preservation

The original three v1 journal tables, trigger SQL and rows remain unchanged.
The explicit installer creates `common_bank_attachment_meta` and
`common_bank_attachments` plus update/delete/insert-conflict guards. Both tables
are WITHOUT ROWID; fresh connections with recursive_triggers=0 cannot use
INSERT OR REPLACE, UPDATE OR REPLACE or rowid/oid/_rowid_ to replace/delete records.
The attachment insert requires the exact existing PENDING hash predecessor;
reader audit independently rederives every attachment and all original raw-byte
bindings. Unexpected table triggers, lost metadata, corrupted bodies, wrong
hashes and unsupported versions fail closed.

Installation transactionally claims an otherwise-zero SQLite user_version
`0x43424731`, in addition to the existing v1 application_id. A retained installed
version with missing tables/metadata cannot silently reinstall. Existing unrelated
nonzero versions reject. No constructor, ordinary v1 create/reserve/resume or
read operation installs this profile. Explicit installation on old PENDING does
not make it completable. Older strict v1 code rejects the additive profile, so
coordinator rollout must use this reviewed reader with its explicit installation;
there is no permissive mixed-version fallback or receipt migration.

## Resource accounting

Existing run/event capacity remains128. Each audit's raw discovery view has its
own128-reference/128-physical-load/32MiB serialized replay cap shared across all
runs. Existing create_run performs that audit PLUS a separate bounded plan pass:
at most256 references/loads and64MiB serialized replay across both passes, not
one shared32MiB window. Completion installation/reservation/attachment/resume
perform one source audit each; begin_genesis additionally has a schema-only
transaction before its reservation. genesis_request does session/database identity
checks without another source replay. Repeated API calls each pay their audit
cost; the whole multi-operation workflow is not advertised as one32MiB window.
Original per-source2MiB and descriptor8KiB checks,128-run bound, descriptor8KiB,
plan64KiB and event8KiB lengths remain. In particular source validation is repeated
per run;128 sources can contribute up to256MiB of serialized source input per
audit, separately from replay-page ceilings. These are serialized input/retention
bounds, not a claim about Python RSS or decoded-object overhead.

Attachments add at most128 rows: each request<=1KiB, original response<=64KiB,
redacted failure<=2KiB and event<=8KiB. Thus original response retention is at
most8MiB per attachment audit, plus bounded request/failure/event metadata, on
top of its replay/source/plan passes. SQL length preflight precedes attachment
row fetch and parsing; no entire oversized body is copied/parsed. Known live
observations have the same per-handle bound and at most one handle per bound run;
exit clears the registry. Exact raw-byte hashes are independent of canonical
parsed JSON hashes. No RPC ceiling is increased.

## Validation and remaining dependencies

Dedicated tests use the accepted actual raw decoding/discovery fixture and
HistoryProgress SEALED admission. They exercise original v1 preservation, opaque
capabilities, fork/thread/session fencing, stale source, exact response identity,
malformed/conflicting envelopes, redacted/oversized failures, shared counter
conservation, immutable fresh-connection attacks, corrupted bytes and lost
extension metadata/version. Real spawned processes terminate after PENDING,
before completion commit, after successful commit and after failed-result commit;
two independent processes race through begin+attachment and exactly one succeeds.
Injected before/after SQL commit errors prove same-observation persistence and
conflicting-observation rejection without I/O. Synthetic DONE proves local
protocol and persistence contracts only, not live acceptance or OWN-2/3 completion.

Full capture transport/source verification, finalized-slot/union/clock stages,
common_bank_view reconciliation, absent canonical bank/head publication and
coordinated protected receipt-kind migration remain separately reviewed work.
All entry/ownership/history/source gates are retained. No dependency on unfinished
APIs or provider secrets is added, and no shared readiness/queue files change.

Exact final targeted/full Linux Python3.12 results are recorded below.

- Python **3.12.14**, Linux, fixture-only.
- `python -m unittest tests.test_common_bank_genesis_attachment tests.test_common_bank_journal tests.test_common_bank_view tests.test_common_bank_acquisition_contract -q`: **70 tests passed in22.583s**, including19 dedicated attachment tests.
- `python -m unittest discover -q`: **1646 tests passed in107.229s, zero skips/errors/failures** on the exact pinned base plus this three-file change.
- `git diff --check`: clean. No outstanding failures. Earlier focused runs of16 unchanged journal tests and17 then18 developing attachment tests also passed; the final results above include all final production changes and the real fork/failed-commit additions.
- Existing private loopback fixture tests used authorized loopback networking; no provider/VPS/secrets/signing/broadcasting access occurred. No shared readiness/queue changes, merge or deployment.
