# Disconnected original union stage

Accepted base `421a9ce1c92a737375b249e8fe83cb11e9295198` (reviewed PR88).
Only `desk/common_bank_journal.py`, dedicated tests and this report change.
Accepted `common_bank_view.py` and common-bank acquisition design were read before
choosing bounds. Worker01 receipt physical format, provider transport, clock,
evidence/bank/head/receipt publication and all approval wiring remain absent.

## Explicit additive profile and immutable transition

`install_union_stage()` requires the installed slot profile and audits every
original source/run/genesis/slot record before installing three new WITHOUT ROWID
tables: common_bank_union_meta, common_bank_union_intents and
common_bank_union_attachments. Their update/delete/conflict/predecessor guards
are audited by exact SQL. SQLite user_version advances explicitly to0x43425531;
original application_id, prior profile metadata, SQL/triggers, descriptors, plans,
raw bytes and rows remain unchanged. Ordinary reads/begin/constructors do not
install. Old strict readers reject the extension; rollout requires this reader
and explicit profile installation together. Missing/unknown/corrupt installed
profiles fail closed without remigration or downgrade.

`begin_union(capture_id, fence=..., reserved_at=...)` requires audited SLOT_DONE,
which itself requires audited GENESIS_DONE and original SEALED source. The fence
is the exact immutable slot completion hash. Failed/ambiguous predecessor, stale
seed/hash/source/plan/database, existing union intent, incompatible fixed bank,
invalid time or insufficient shared capacity blocks. The ordinal5 intent binds
original source/descriptor/plan hashes, frozen U and literal options:

```
getMultipleAccounts(U, {encoding:base64, commitment:finalized, minContextSlot:S})
```

U is independently reconstructed by the accepted plan code as canonical P plus
sorted F minus P, at most100 unique valid addresses; neither candidates nor pool
responses manufacture F. S is the exact original completed getSlot result. There
is no caller key/role/options/success flag parameter. Reservation atomically
increments the SAME existing shared18 counter and inserts this PENDING intent
in one BEGIN IMMEDIATE/FULL rollback-journal transaction. The insert witnesses
current charge and exact DONE predecessor. No new budget/refund/rebind/capture ID
is allowed. Preflight includes union+clock+at least one mint and one per F history
page (`current_used + 3 + len(F) <= 18`); it is not a paging guarantee.

The generated request is exact canonical UTF-8 JSON-RPC2.0 with intent-derived
string ID. It is bounded before charging, retained unchanged on completion, and
returned through an opaque same-live-PID/thread/session union handle minted only
after durable reservation. Cross-stage/forged/serialized/reopened handles cannot
complete it. Request generation and journal transactions perform no I/O. Losing
reservation acknowledgment or session after commit leaves terminal charged
PENDING, not authority to retry or retrofit a later plausible response.

## Original parent bytes, actual T and conservative syntax

Union wire request limit is8KiB:100 address strings plus fixed method/ID/options
fit without reordering, splitting or slicing. Wire response limit is64KiB,
aligned with accepted common_bank_view.MAX_RECORD_BYTES. That validator currently
uses signed-range integer floor/context slots, so union rejects S>=2^63 before
charging while preserving the older slot stage's original u64 results. It does
not rebind or lower that saved floor to make a request feasible.

`attach_union_response(handle, original_bytes, completed_at=...)` retains the
entire original request and entire bounded response atomically with an immutable
ordinal6 completion. It requires strict exclusive JSON-RPC framing, matching ID,
no duplicate/nonfinite fields, result exactly context/value, actual integer
`S <= T < 2^63`, optional bounded string apiVersion, and value cardinality exactly
len(U). Boolean/fractional/overflow/below-floor context and missing/extra values
remain retained FAILED bodies. T>S is valid and retained as actual T; original
request minContextSlot remains S. No exact-slot pinning, request rewriting or
recapture occurs.

The accepted `_raw_account` decoder checks every nonnull entry's expected legacy
owner program, executable/lamport/optional space metadata, strict base64 encoding
and bounded exact layouts: pool243/287/300/301 bytes, mints82, vault/holder165.
Token-2022/unknown owner, unsupported layout/encoding and malformed metadata fail.
The first six required pool-role entries cannot be null. Extra holder nulls remain
explicit in the parent and F; they never become zero balances or proven closure.
The canonical size of the **whole equivalent original-parent envelope**, including
full U/params/result, must also fit the accepted64KiB view bound. This temporary
size calculation saves no normalized/sliced/manufactured RPC record or per-profile
response. Only original wire bytes and journal metadata persist; all pages and
existing source/history/genesis/slot records stay unchanged.

DONE here means raw framing/program/layout syntax only. It does not validate
pool PDA fields, mint/vault authority bytes, supply/balance conservation, holder
initialization-owner agreement, bank-complete frontier/history, closure, clock,
provider provenance or finality. Type-compatible swapped holder states or
semantic pool contradictions can pass syntax and must still fail later accepted
view/policy/ownership gates. No semantic certificate is emitted and no bank pointer
is published. Future clock/evidence wiring must bind original bytes and every
competing malformed/contradictory observation, not turn this diagnostic DONE into
approval or fabricate six-account/mint-plus-holders RPC evidence.

Malformed/contradictory bounded bodies are retained byte-for-byte as FAILED.
Oversized bodies become fixed redacted category/count observations without
parsing/hash/truncation/positive-prefix acceptance. Fixed transport failure
categories are TRANSPORT_TIMEOUT, TRANSPORT_ERROR, CANCELLED; provenance names
getMultipleAccounts and hashes the full original request instead of repeating U.
Arbitrary exception text/URLs/secrets are rejected; bounded raw RPC-error bodies
remain private original evidence rather than whole-body secret redaction.

Same-observation/category AND timestamp duplicates are idempotent only with the
same live capability. The chosen observation is frozen before commit; SQL error
before/after durability permits identical persistence-only acknowledgment, never
transport retry or conflicting replacement. Death before attachment commit leaves
terminal PENDING; after commit recovers exact UNION_DONE/UNION_FAILED. Reopening
mints no authority and cannot reserve again. Reader audit rederives every
predecessor/request/slot/raw-byte hash and metadata binding; changed/rehashed slot
cannot rebase the union, and counter rewind below its charge fails closed.

## Resource accounting and authority boundary

Each audit still shares one128-ref/128-physical-load/32MiB serialized discovery
replay across <=128 runs. create_run retains a separate bounded plan pass, so
its two passes can total256 loads/refs and64MiB, not one32MiB window. Source
validation repeats per run with existing2MiB input caps (up to256MiB serialized
source work per audit), independently of page replay. Original source/descriptor/
plan/event limits remain. These are serialized work/retention bounds, not Python
RSS or a whole-workflow32MiB claim.

Each new stage has <=128 intents and <=128 attachments, with one oversize count
sentinel before whole-set rejection. Union intent<=16KiB, attachment event<=8KiB,
request<=8KiB, raw response<=64KiB, redacted failure<=2KiB. Row length preflight
precedes fetch/parse. Three response stages together add up to24MiB serialized
raw-body audit work (8MiB per stage), plus bounded requests/metadata, on top of
source/plan/replay work. Their raw row scans are sequential. The union grammar
parses a bounded response twice (shared framing then union shape) and computes a
bounded whole-parent canonical encoding, plus per-account strict base64 decoding;
this is additional bounded processing, not one parse/RSS promise. Known live
observations retain the same per-stage bounds and registry clears on exit.
Each install/begin/attach/resume invocation pays its own audit.

Raw bytes/opaque handles/ApprovedSource descriptors cannot establish sent requests,
original network provenance, authenticated source/genesis/finality or private
control. Matching synthetic bytes can produce syntax DONE, with provider_calls0
and all eligibility/ownership/chain flags false. Existing result-only RPC adapters
cannot fabricate original framing; a future independently reviewed byte-preserving
trusted transport remains required. Stable owned Linux paths, accepted research
worker->invocation->request lock order/read guard, rollback SQLite and trusted
application process/configuration remain assumptions. Hostile same-process private
state introspection or privileged whole-file/header/lock rewrites/historical
rollback are outside this ordinary-SQL boundary.

## Validation

Fixtures invoke actual production discovery/SEALED admission, reservations,
readeraudits and storage against accepted raw synthetic history/pool account
records. Tests cover exact T>S full-parent bytes/U/floor, explicit upgrade and
unchanged prior SQL/rows, null holder semantics, malformed framing/context/
cardinality/program/layout/metadata, Token-2022 exclusion, canonical parent bound,
oversized positive prefixes, source/counter/parent substitution, opaque session/
stage authority, immutable fresh SQLite replace/update/delete/rowid aliases,
missing schema and deterministic duplicate/conflicting results.

Actual spawned exits exercise before/after reservation commit, before/after
completion commit and after FAILED commit. A two-process race charges/completes
exactly once. Before/after completion SQL commit errors permit only identical
known-result persistence. Synthetic positives are not live acceptance, ownership
completion or paper readiness. Initial17-test run had one harness error from
attempting a nested public lock inside the positive test; using the existing
session fixed it without relaxing production locking. Final results follow.

Final Python3.12.14 Linux fixture results:

- `python -m unittest tests.test_common_bank_union_stage -q`: **20 tests in17.246s, OK** on final production/test source.
- Existing v1/genesis/slot regression: **56 tests in32.616s, OK** with the additive implementation.
- `python -m unittest discover -q`: **1732 tests in143.711s, OK; zero skips/errors/failures**, pinned accepted base plus exactly these three files.
- `git diff --check`: clean. No known outstanding failures.
- Existing full-suite private loopback fixtures used authorized networking only;
  no provider/VPS/secrets/signing/broadcasting access, shared readiness/queue edits,
  merge or deployment occurred.
