# Common-bank original transport outcome preservation

Base: accepted integration `a4e15f34f78d2ca8752d40c6f5d53b1deebc7906`, including
PR98 code integration `691737b03e9571eaecce8f3d22f9ef8a09098802`.
Branch: `codex/cloud-common-bank-transport-outcomes-01`.

This repairs an information-loss boundary: a transport can return a bounded
original body **and** a fixed failure, such as a valid-looking JSON prefix with
`RESPONSE_TRUNCATED`. The old exclusive response-or-failure contract could only
discard one field. Passing the prefix alone could derive diagnostic DONE.
The new API preserves both without treating the body as successful when any
transport failure exists. It performs no transport or producer wiring.

## Explicit compatibility fence

The new `TRANSPORT_VERSION = 0x43425432` is an explicit full-clock journal
completion profile. Application ID remains unchanged. Its exact physical SQL,
indexes, triggers, immutable descriptors and original rows are identical to
the existing full-clock profile. The only installation change is the SQLite
user_version marker, within the existing guarded atomic journal transaction.
There is no new parallel journal, rewritten record or candidate-derived source.

`session.install_transport_outcomes()` first audits the complete existing
journal and requires CLOCK_VERSION or TRANSPORT_VERSION. It never installs
missing stage profiles automatically. It sets the marker and revalidates the
same complete schema, atomically and idempotently, without charging, refunding
or changing source/plan/intent/completion rows. Installation can preserve an
existing same-session in-flight handle, but cannot recover an expired handle or
create a capability for reopened PENDING. Malformed originals refuse the
upgrade; valid FAILED/PENDING originals remain terminal and unchanged. Failure
before commit retains the old marker; lost acknowledgment after
commit leaves the new marker and supports identical explicit installation.

New completion kinds are `common_bank_{genesis,slot,union,clock}_attachment_v2`.
All old v1 completions remain supported and byte/hash-exact under the new marker;
all are audited alongside v2, never filtered by profile, state or freshness.
Existing v1 completion APIs and their exclusive response/failure semantics stay
unchanged. Existing stage-install APIs cannot downgrade the transport marker.

Older accepted journal, inventory and semantic readers reject the unknown marker.
If the marker is maliciously lowered to CLOCK_VERSION, v2 kinds still reject:
they are never silently read as v1, even for a syntactically valid successful
body with no failure. Current readers also require the transport marker for v2
records. Unknown kinds/profiles are refused, not normalized. Existing full-clock
metadata retains its literal v1 bytes because the old schema/descriptor contract
is unchanged; it does not independently provision transport/source authority.

## Completion API

Within an existing guarded session and newly reserved stage capability:

```python
session.attach_genesis_outcome(token, outcome, completed_at=at)
session.attach_slot_outcome(token, outcome, completed_at=at)
session.attach_union_outcome(token, outcome, completed_at=at)
session.attach_clock_outcome(token, outcome, completed_at=at)
```

`outcome` must be exactly the existing immutable SlotByteExchange returned by the
accepted OriginalByteReadTransport (ReadByteExchange is the same type). This is
a local data contract, **not** proof that the transport was actually invoked or
authenticated. The caller must pass the whole actual outcome unchanged; no
runtime caller is wired here. The supplied original request bytes must exactly
equal the capability's generated request, including whitespace/string ID. The
capability is bound to the existing scan/descriptor/plan/fence, stage, session,
PID/thread and original shared investigation counter.

Response bytes are either explicit None or immutable bytes <=64 KiB, including
`b''`. Missing body plus missing failure rejects as invalid input; empty body
without transport failure is retained as locally invalid response, not missing
evidence. Bytes over the cap or nonbytes reject before persistence; the API does
not strip a prefix, discard an over-cap body or claim it was bounded. The actual
accepted transport represents an oversized body as None/RESPONSE_OVERSIZED,
which is retained as FAILED with no invented observed byte count. A caller with
an over-cap object must not reinterpret it as a bounded transport observation.

Supported fixed transport failure codes are exactly CREDENTIAL_UNAVAILABLE,
TRANSPORT_ERROR, HTTP_REJECTED, RESPONSE_HEADERS_INVALID, RESPONSE_OVERSIZED,
RESPONSE_TRUNCATED, RESPONSE_INVALID and RPC_ERROR. No caller/provider messages,
URLs, exception strings or arbitrary new categories are accepted. Original
bounded bodies may themselves contain sensitive provider text and must remain
opaque evidence, not public diagnostic/log content.

A failure is stored as canonical JSON with exactly:

```text
kind: common_bank_transport_failure_v2
stage: the bound stage
method: the original bound request method
category: the fixed original transport failure code
request_sha256: SHA256(original request bytes)
```

No observed_bytes claim is synthesized: accepted transport outcomes do not
supply that count. Existing v1 oversized records retain their actual existing
observed_bytes field and legacy validation unchanged. Fixed v2 failure JSON is
bounded by the unchanged 2-KiB failure limit; union request params are bound by
their original request/hash rather than duplicated into a potentially oversized
failure record.

The entire original request, entire optional bounded body, canonical failure,
completion event, predecessor hash and completion hash persist in one existing
immutable attachment row and transaction. Event response_sha256 uses explicit
None testing, so an empty body has SHA256(empty), not a missing-body hash.
Failure hash binds the exact canonical fixed descriptor. Any non-null transport
failure immediately forces FAILED/reason=category before parsing or extracting
successful body values. Slot/union derived actual slot and clock derived time
are None. The clock's existing slot and bank_captured_at still identify its
previously captured union bank; they are not derived from the failed clock body.
Union request_floor similarly retains the prior requested S. Valid-looking
partial bodies cannot become DONE. With no transport failure, existing strict
stage outcome checks continue to derive only diagnostic DONE/FAILED.

## Persistence, counters and read audits

No new counter exists. Every stage still reserves through its existing atomic
18-attempt shared budget plus PENDING intent before any later caller I/O. Attaching
or repeating a known completion does not reserve another attempt or refund it.
The new API only attaches to a current opaque handle; it cannot begin an attempt
from a serialized outcome, reopen a terminal failed stage or revive ambiguous
PENDING. All reservation/predecessor/fence and same-session ownership checks are
unchanged. No transaction spans I/O.

The existing chosen-observation tuple now includes the v2 event, original request,
optional original body and fixed failure. SQL failure before/after commit still
permits only identical persistence, never recapture or changing/removing the
failure. A changed body, code, completion time or v1/v2 representation refuses.
Crash before attachment commit leaves the charged intent terminal PENDING;
crash after commit preserves the exact terminal FAILED/DONE observation. No
completion acknowledgment establishes permission for another provider request.

Journal replay explicitly dispatches v1/v2 kinds and re-derives hashes and local
outcomes. Completion inventory uses the exact existing typed physical inventory,
accepts the new marker only with the full schema, structurally validates every
v2 request/failure/stage binding and insists that any failure has FAILED state
and matching reason. All legacy rows and all four profiles remain enumerated.
Semantic replay admits either full-clock marker but still requires **every**
stage DONE. It cannot parse a failed prefix into point semantics or return a
partial success. Its guarded read-only snapshot and false authority flags are
unchanged. Inventory remains structural, not a substitute for raw semantic audit.

Request/body/event/failure limits, 128-run/stage caps and shared read resource
bounds remain unchanged. Both body and failure contribute to existing aggregate
preflight accounting before materialization; allowing both does not create a
separate allowance. Guards, private file identity, lock order and stable entry/
exit identities are unchanged. No schema/reader fallback or cached completion
success is added.

## Fixture validation and remaining dependencies

Dedicated real SQLite fixtures cover all stages' valid-looking truncated bodies,
exact request/body/failure retention, missing versus empty bodies, unobserved
oversize and over-cap refusal, supported fixed failure codes, binding/type
attacks, identical-only commit error recovery, actual process death before/after
commit, full-clock marker transition acknowledgment loss, old SQL/rows preserved,
shared18 exhaustion without refunds, terminal-PENDING upgrade refusal, rehashed
DONE forgeries retaining failure witnesses, mixed v1/v2 inventory and semantic replay.
Successful mixed semantic replay remains REJECT/all approval flags false and
does not mutate the database. Failed stages never satisfy semantic completion.

Older reader compatibility is tested using exact accepted base source snapshots
in tests/fixtures/common_bank_transport_outcomes/*.py.txt. These are test-only
source data, never production imports, providers or PR97 code. Tests verify
hardcoded SHA256 before compiling them into isolated test module namespaces;
they work in shallow CI checkouts without Git history/network access. Provenance:

| Original file at a4e15f34f78d2ca8752d40c6f5d53b1deebc7906 | Bytes | SHA256 |
| --- | --- | --- |
| desk/common_bank_journal.py | 83433 | 978c8503073c5acc393f7a5e0bba8743826bb1de31769907164de36952f1de95 |
| desk/common_bank_completion_inventory.py | 42907 | ccfcab3ac6b584307fcc24ed827c01d1842e1c9662c4b2ac0cc864072082baa1 |

No transport/provider/VPS/credential lookup, source sealing/acquisition/runtime
wiring, journal completion authority, issuance, authentication, eligibility,
shared queue/readiness edit, merge or deployment is added. Future cohesive
producer integration remains separately reviewed and must pass actual complete
outcomes under admitted budgets. Token/anchor/history/caller/CPI/interval and
receipt/entry blockers remain unresolved. Diagnostic DONE is not authenticated
common-bank evidence or live acceptance. PR97 is neither imported nor assumed.

Python3.12.14 dedicated validation: 14 tests PASS in17.373s, zero failures/errors/skips.
Combined journal/replay/inventory validation before the final two tests:171 PASS
in89.822s, zero failures/errors/skips. Final full Linux:1,948 tests PASS
in165.557s, zero failures/errors/skips. All five original SQL/schema dictionaries
were also compared byte-for-byte with the pinned accepted base. Exact head/tree
and tracked manifest are recorded on the PR. Logs remain in work/transport-outcomes-focused.log,
work/outcome-combined-focused.log and work/transport-outcomes-full.log. The initial
old inventory compatibility test used the new module's pin object and correctly
hit the older reader's strict pin-type rejection before profile validation; the
fixture now constructs the exact old pin type to reach the intended marker test.
