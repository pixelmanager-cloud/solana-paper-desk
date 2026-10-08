# Disconnected typed-chain/certificate dispatch validator

Base `ac439cb54a5d709bbc41acc3986010e7fb522d5d`; implements the narrow next
slice specified by PR85's independent review (issue comment 6059991517).
Only `desk/common_bank_receipt_chain.py`, its dedicated tests and this report
change. Existing PoolReceiptLedger, v1 readers/writers, journals, policies and
entry logic are unchanged. No migration, issuance or provider code is added.

## Read-only API and trust limits

`validate_receipt_chain(ChainSnapshot, ChainAnchor)` is a pure bounded validator.
It opens no path/database, takes no locks, creates no schema, loads no evidence,
reserves no requests and calls no provider. Inputs must be exact frozen dataclass
instances containing tuple rows, not a generator, caller-selected subset or live
cursor. A future protected caller must obtain ALL descriptor/certificate/head/
rows together under canonical locks and a single guarded read transaction, pin
its current head in the anchor, and independently establish reviewed legacy
baseline/certificate/schema pins. Candidate or evidence JSON MUST NOT construct
those authority pins. This slice has no protected reader and authenticates none
of those inputs. Even a successfully validated synthetic chain is diagnostic
REJECT, with every provenance/ownership/history/entry flag false.

The immutable `ChainAnchor` contains byte-exact legacy descriptor body, original
10-column row tuples and old `(count,hash)`, the reviewed certificate/schema
hashes, and an independently pinned CURRENT `(count,hash)`. `ChainSnapshot`
contains descriptor body/hash, certificate body/hash, current head and ALL rows.
A supplied final head must match the independent current pin as well as the
computed complete chain. This prevents a truncated or stale snapshot silently
becoming current by recomputing its own head. Neither pin nor SHA256 authenticates
a privileged whole-file rewrite, omitted observations before protected publication
or arbitrary callers that forge both inputs; this limitation is explicit.

The result is a frozen `TypedChain`, retaining exact original descriptor and
certificate TEXT, transition tuple, all typed observations/markers and final
head. Each `TypedRecord` retains its exact full original row including payload
TEXT, hash/previous/row hashes and publication ID. There is no cache or fallback:
every call audits every row; any tail/schema-dispatch/metadata error raises
`ChainUnavailable` without returning a prefix. There is no v1-only fallback.

## Explicit synthetic format-2 contract

The existing canonical v1 payload is accepted ONLY in the byte-exact legacy
prefix and decoded with the existing pure v1 metadata validator (`_decode`,
not any ledger opener). Its original descriptor seeds the unchanged row-hash
algorithm. The complete original prefix must match anchor tuples exactly, not
semantically equivalent parsed JSON. Descriptor body/hash also match exactly.

The next sequence is exactly one typed transition. Its publication ID is
`transition:<certificate hash>` and ALL four index columns pool/mint/slot/source
are exactly `!transition!`. These invalid-address sentinels are not observations.
Its canonical payload has only version=2, type=`transition`,
profile=`pool-receipt-ledger-format-v2`, and certificate_hash. The computed
previous hash at that sequence MUST be the externally pinned old head.

The certificate is bounded canonical JSON with exact fields: kind
`pool_receipt_transition_certificate_v2`, version=2, legacy_descriptor_hash,
legacy_count, legacy_head_hash, dispatch_hash, schema_hash and capabilities.
Certificate hash must equal its bytes and the independently reviewed anchor pin.
`DISPATCH_HASH` pins this module's field sets, type/profile/sentinel conventions
and no-marker-resolution rule. Schema hash binds a reviewed future schema identity;
this pure validator does NOT inspect installed SQL, establish the old-binary
fence or assert protected SQLite immutability. Those remain future work.

Each exact capability contains source_id, source_kind, network, genesis_hash,
version, profile and boolean issuance_enabled. Allowed pairs are (1, v1 PROFILE),
(2, v1 PROFILE), (2, parent PROFILE). Every original registry source's version-1
capability must remain with identical source attributes. Extensions cannot rebind
source kind/network/genesis across versions. Duplicate tuples, unknown profiles,
versions, networks/genesis, source identities or fixture permission violations
refuse the whole chain. Every observation/marker must match its exact capability.
`issuance_enabled=False` NEVER removes historical observations from audit; this
read-only slice does not enforce issuance or prove historical issuance authority.
A future issuer must enforce current permissions separately.

After transition, only these certificate-bound exact payload types are accepted:

* `legacy_observation`, version 2, v1 PROFILE: wraps exact existing receipt fields
  under `observation`, retaining the four original EvidenceRefs. This explicit
  wrapper permits NEW v1-profile observations to bind the transition certificate;
  unwrapped version-1 payloads after transition refuse. Original prefix stays
  unwrapped and byte-exact. New v1-profile source capabilities use version 2.
* `common_bank_observation`, version 2,
  `pumpswap-common-bank-parent-point-v2`: exact `observation` fields bind source,
  pool/mint/actual slot, signed snapshot/capture times, manifest_hash and capture
  binding. Capture binding is capture_id, scan_id, descriptor_hash, source_hash,
  seal_hash, revision_hash, budget_hash and journal_hash. Hash references are
  retained without claiming the referenced records exist or agree.
* `unresolved_marker`, version 2, parent PROFILE: exact `marker` fields contain
  the same source/capture binding, explicit pool/mint/slot (each nullable),
  PENDING/FAILED/INCOMPLETE/UNAVAILABLE status and unique evidence_hashes.
  Unknown pool/mint/slot columns must be exactly `!unknown-pool!`,
  `!unknown-mint!`, `!unknown-slot!`; known columns exactly match payload fields.

All types use the unchanged sequence/publication/payload hash/previous-hash row
algorithm. Repeated transition events, unknown types, resolution events, boolean
slots, noncanonical/duplicate-key JSON, unbound certificate, index substitutions,
unknown metadata fields, duplicate publication IDs or payload hashes and wrong
heads refuse. No completion API is assumed. Adding a successful observation for
the same capture does not remove a marker, resolve failures or hide charged
attempts. Binding syntax does not replay budgets/seals/revisions or establish
complete request accounting. Original records are never rewritten/refunded.

## Complete all-profile scope enumeration

`TypedChain.enumerate_scope(pool=...,mint=...,slot=...)` returns a frozen
`ScopeEnumeration` with ALL matching observations, applicable markers, aggregate
references, reasons and the same complete head. No source/profile/freshness/
current-permission argument exists. Time is not a grouping key. Expired,
semantically invalid signed times and disabled-source observations remain in
original order and bytes. Different actual slots remain different scopes;
request-floor/historical-bank relabeling is not performed.

Known-identity/known-slot markers block their exact pool/mint/slot. A known
pool/mint marker with unknown actual slot blocks every slot for that pair.
Either unknown pool OR unknown mint blocks every scope, even if another identity
or slot is present; this conservative rule cannot silently hide unknown identity.
Every applicable marker remains in the result with explicit blocker reasons.
No observations are dropped because blockers exist. Raw state/conflict checking
has NOT run: raw conflicts, unavailable refs lacking protected markers, source
provenance, finality, complete frontier and request accounting remain unresolved.
A future consumer must replay EVERY enumerated original through one shared
ReplayView and compare original v1 and indexed original-parent union state/time.
The validator never fabricates/saves a sliced rpc_response_v1 or issues receipts.

## Shared resources and refusal

Before JSON parsing/hashing/copying prefix rows, require exact tuples with at most
10,000 rows including transition/markers, ten typed columns and bounded strings.
Each payload, descriptor and certificate is <=8 KiB UTF-8, with a character
precheck before encoding; non-payload columns <=128 characters. Aggregate serialized
row fields from snapshot PLUS pinned prefix, descriptor/certificate/anchor body
are <=32 MiB. Prefix duplication is intentionally charged, so some otherwise
well-formed large legacy snapshots honestly refuse. These are input-validation
bounds, not a claim that unavailable raw evidence has been loaded under budget.
JSON rejects duplicates/nonfinite/float metadata and exceeds depth 12 or 1,024
nodes. Source capabilities are <=256 TOTAL across versions/profiles and include
the old registry; byte bounds may refuse before this count. Exact v1 descriptors
larger than the metadata limit are unsupported here; v1 runtime behavior stays
unchanged and no alternate baseline is substituted.

Scoped observations PLUS applicable markers are <=256 total across profiles;
aggregate distinct metadata evidence references are <=128, with no per-profile
quota, silent truncation or eviction. Marker reference and capture-binding hashes
share this same set. Missing raw objects are not read; the future shared ReplayView
must enforce its existing 128-object/32-MiB aggregate replay bound, 64-KiB parent,
2-MiB discovery, <=18 pages and <=100 union keys while using common_bank_view.
This validator does not alter any 18-attempt durable counter or acquisition cap.

## Fixture evidence and remaining dependencies

Dedicated tests construct explicitly SYNTHETIC format-2 metadata snapshots. Their
manifest/journal/seal references are placeholders, NOT real union receipts or
successful acquisition evidence. One test loads actual persisted v1 fixture rows
from the current protected ledger, proves exact prefix preservation and unchanged
file bytes, and leaves its existing v1 reader usable. Other proofs cover mixed
profiles, expired/disabled issuers, all marker states, unknown scope identities,
current/old head and certificate pins, capability rebind/duplicate/refusal,
sentinels, unknown dispatch, tail damage/no prior-success fallback, exact 256/257
scope bounds and shared reference/UTF-8/serialized/JSON/capability resources.

Protected format-2 snapshot access, exact SQL/schema fence, atomic offline migration,
authoritative journal completion/quiescence, issuer permission enforcement,
protected marker publication/resolution, source/seal/revision/budget/raw replay
and mixed conflict consumers remain independent reviewed dependencies. No live
migration, provider call, provenance authentication, ownership/interval/entry
approval, Token2022 support or live acceptance is claimed.

Validation on Linux/Python 3.12.14:

* `python -m unittest tests.test_common_bank_receipt_chain -q`: 34 tests,
  0.105 seconds, OK, zero skips/failures/errors.
* `python -m unittest discover -q`: 1,672 tests, 81.083 seconds, OK,
  zero skips/failures/errors. Existing private-loopback fixture tests only.
* `git diff --check`: clean. No known failing tests remain.

Returned snapshots/diagnostics are not reusable current-policy caches. A future
protected consumer must acquire a fresh complete read/pin and revalidate under
its guard for every use; this module neither maintains that guard nor certifies
a returned object's freshness. The local test log remains in checkout work/.
