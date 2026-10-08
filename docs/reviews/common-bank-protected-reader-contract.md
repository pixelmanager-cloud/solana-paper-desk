# Protected mixed-receipt reader: concrete format blocker

Base `28e59ac3da521c032cec49a26b603088c78e78f1`. Exclusive successor:
MIXED-RECEIPT-PROTECTED-READER. PR85 and PR87 were read, including independent
review comment 6060373059. This is the assignment's explicit missing-contract
fallback, not a protected reader implementation or acceptance claim.

## Finding before implementation

The accepted code defines the typed JSON chain/certificate, but does NOT define
an exact protected format-2 SQLite schema. `ChainAnchor.schema_hash` is opaque:
`validate_receipt_chain` compares the certificate against the supplied pin; it
never inspects SQLite. The review explicitly states: "No filesystem/schema/fence
authenticity is established by the schema hash alone." It lists "protected
exact-format snapshot access/schema-fence audit with independent pin provisioning"
as the next slice. PR85 gives an example of changing `receipt_no_replace`, not
reviewed replacement SQL or a certificate table layout. PR87 explicitly leaves
"exact SQL/schema fence" and protected format-2 snapshot access as dependencies.

Inspection of current production sources finds only v1 `PoolReceiptLedger.SCHEMA`.
There is no accepted certificate table/query/cardinality, format-2 object inventory,
fingerprint algorithm, required fence SQL or installed-format marker. A reader
cannot honestly audit an EXACT format by selecting any convenient table name,
trusting a matching candidate hash, accepting caller-selected SQL or normalizing
an arbitrary fence into equivalence. Doing that would define the migration/issuer
format implicitly within a read-only task.

The assignment explicitly says: "If reviewed format/provisioning is not defined
enough, identify concrete missing contract before implementing assumptions."
Therefore no production reader module is added. Seven new fixture proofs and
this scoped report make the blocker reviewable. No existing production file,
journal, schema, shared queue or readiness record changes.

Independent anchor inputs ARE already defined by PR87. Supplying reviewed pins
explicitly, as requested, does not require default trust provisioning. The blocker
is the physical exact-format contract, not routine user approval, missing live
credentials, or a need to access worker02's completion API. The following choices
must be frozen in an independently reviewed format specification before a reader
implements them. Candidate data may never select these choices.

## Smallest concrete contract to freeze

1. **Physical certificate representation.** Specify one table name, exact CREATE
   TABLE SQL, columns/storage classes, singleton key/cardinality and exact immutable
   INSERT/UPDATE/DELETE/REPLACE/rowid protections. Proposed review input: one
   `receipt_transition_certificate(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT
   NOT NULL, hash TEXT NOT NULL)` table with all singleton mutation guards.
   This name/layout is a proposal, NOT an accepted format or installed schema.
   Define explicit refusal on missing/extra rows, wrong id/rowid aliases or hash.
   Certificate JSON/type/bytes remain exactly PR87, never reconstructed from
   transition payload alone or an external unprotected file.
2. **Old-binary fence.** Specify the exact SQL replacing a REQUIRED v1 object,
   preserving every original collision and mutation protection. Additional tables
   cannot fence v1. One reviewed replacement for `receipt_no_replace` is sufficient
   only if it differs byte-exactly from v1 while retaining seq/publication/payload
   collision rejection and all immutable guard semantics. The reader must compare
   this SQL exactly, not infer safety by searching for a token or SQL prefix.
3. **Full schema inventory/fingerprint.** Specify exact `(type,name,tbl_name,sql)`
   inventory including unavoidable autoindexes with NULL SQL. Decide one canonical
   encoding and digest used by migration certificate AND reader. Rootpage numbers
   are physical allocation, not portable schema identity; do not silently include
   them. Reject all unexpected tables/views/triggers/indexes/virtual objects,
   altered required SQL, partial installations and unknown format versions.
   Freeze schema/hash in reviewed code/config, never derive the expected value
   from candidate sqlite_master. The existing opaque schema pin is insufficient
   without knowing precisely what it fingerprints.
4. **Atomic installed-format discriminator.** Specify whether the exact certificate
   singleton itself denotes format 2 or an additional immutable singleton exists.
   All discriminator/certificate/fence/transition/head installation must be one
   migration transaction. Reader must refuse a fence without a certificate, a
   certificate without its fence/transition, duplicate transitions, or old/new
   mixed DDL. No schema creation/repair, partial-state initialization or v1 fallback.
5. **Protected read resource envelope.** Freeze physical-file/header limits and
   bounded SQL scalar preflight in addition to PR87's 10,000 rows/8-KiB fields/
   aggregate 32-MiB limits. SQL UTF-8 byte preflight must use BLOB length, not
   character length. Specify count/cardinality/storage class checks and aggregate
   byte accounting BEFORE fetching full payload/descriptor/certificate values.
   Account for pinned prefix duplication as PR87 does. A reviewed maximum database
   file size/page count prevents a corrupt oversized file from forcing an unbounded
   schema scan before row limits apply. Do not invent a permissive value in code.

These are format decisions shared with the future offline migration/issuer,
not changes to the finalized-slot journal. No dependency on worker02's completion
schema or authority is assumed. Completion remains independently unresolved.

## Reader API and algorithm once those choices are accepted

Suggested disconnected entry point: a context-managed `read_protected_chain`
accepting an explicitly supplied local CoordinatorBoundary, independently supplied
ChainAnchor, and the frozen reviewed format identifier. No defaults from candidate
DB, certificate, evidence JSON, dashboard arguments or environment discovery.
The reviewed implementation owns its SQL statements; callers never supply SQL.
The boundary pins canonical root/ledger/lock/evidence identities and policy options;
the anchor pins byte-exact legacy descriptor/prefix/old head, reviewed certificate/
schema and independently supplied current head. All must agree with protected data.

Fail unsupported Linux/LP64 capability before path/DB access. Reuse accepted guard
semantics without weaker fallbacks: stable allowlisted durable local filesystem,
canonical no-alias paths, no symlink/hardlink/rotation, owned regular single-link
files, private root 0700, ledger/lock 0600 and evidence without group/world writes.
Validate ancestors/parents and no-follow opens before SQLite; guard opened inode
identities and canonical names throughout. Read-only calls MUST NOT create lock
files or databases, recover journals, checkpoint WAL or change permissions.
A same-UID hostile whole-file rewrite/rollback/omission remains outside the original
coordinator contract; do not promote POSIX/hash integrity into remote authenticity.

Lock order is evidence nonmutating read guard, then existing coordinator shared
lock, then protected ledger nonmutating read guard. Match established acquisition
ordering and preserve private loopback. Accepted OFD guard blocks SQLite writers
and rejects WAL/hot/stale journals, contention and unsupported VFS/platform before
SQLite open. Root/evidence/ledger/lock identities must agree with both trusted
boundary and original descriptor, checked on entry/exit and connection close.
Do not use immutable=1, copied files or an fd alias as an unreviewed bypass.

Use mode=ro/query_only with a single bounded transaction, no arbitrary SQL,
extensions or repair. Audit exact reviewed schema/fence and the installed singleton
shape before decoding data. Preflight byte/storage classes/counts before fetching
all rows. Read descriptor/certificate/head and ALL rows in that same transaction
under both guards; no separate per-profile query, freshness/source filter or
caller-supplied row subset. Bound BEGIN/read/lock duration and contention; an
unavailable read returns no usable policy or prior success.

Build one complete immutable ChainSnapshot and pass the independent anchor to
`validate_receipt_chain`. Preserve every original TEXT/hash/index tuple, all
legacy and typed rows and all markers. Revalidate exact identities/schema/head
before yielding and at guard exit; do not cache a successful object as current.
Every new use needs a fresh guarded full read. The output is diagnostic REJECT,
not a v1 TrustedSourcePolicy, issuer token or raw conflict proof. Keep source/
finality/history/interval/private-control/ownership/entry flags false. Raw replay
under one shared128-ref/32-MiB view is a separate consumer slice, not this reader.

## Seven fixture proofs and honest limits

`tests/test_mixed_receipt_reader_contract.py` constructs fresh explicitly SYNTHETIC
in-memory SQL layouts, with metadata fixtures from PR87. It does NOT open an
operator ledger, create a migration, issue receipts, provision trusted anchors,
assert protected storage or fabricate union RPC responses. All diagnostics REJECT.

* A matching candidate schema hash can bind a layout missing receipt_no_update;
  pure dispatch still validates metadata and leaves protected-read approval false.
* A candidate-selected certificate table is not an independently defined/provisioned
  table. Metadata can validate even with NO physical certificate table at all.
* An added certificate table with unchanged required v1 schema has no reviewed
  required-object old-binary fence; metadata cannot certify that missing fence.
* Two different certificate table layouts, separately repinned synthetically, both
  pass pure dispatch. The opaque hash does not choose the approved physical format.
* Multibyte TEXT fits a character limit while violating the 8-KiB byte limit;
  SQL BLOB length sees the violation without fetching the large payload.
* Head and rows from interleaved states cannot be salvaged as one complete snapshot;
  the existing typed validator refuses rather than returning a prefix.

These reproduce CURRENT accepted limitations and preflight requirements; they are
not tests of a nonexistent protected reader. Existing v1 schema/metadata behavior
and all request budgets/original records remain unchanged.

## Remaining authority and completion dependencies

A reviewable exact-format specification covering the five choices above unblocks
the reader. Afterwards independent review must validate no-follow/identity races,
permissions and SQL preflight, actual contention/hot journal handling, no writes,
atomic schema/head/certificate consistency, all rows/markers and corruption after
previous success. Protected format provisioning/offline migration, issuer
permissions, authoritative completion/quiescence and marker publication/resolution,
raw source/seal/revision/budget/common-bank replay and mixed conflict comparison
remain separate dependencies. None is inferred from syntax/hash flags.
No provider/VPS/secrets/signing/shared docs edits, merge/deployment or live acceptance.

Linux/Python 3.12.14 fixture validation:

* `python -m unittest tests.test_mixed_receipt_reader_contract -q`: seven tests,
  0.010 seconds, OK, zero skips/failures/errors.
* `python -m unittest discover -q`: 1,698 tests, 86.962 seconds, OK,
  zero skips/failures/errors. Existing private-loopback fixtures only.
* `git diff --check`: clean. No known failing tests remain.

Reader implementation remains BLOCKED on the exact physical format specification,
not on test failures. This report/test PR is the explicit conditional deliverable;
no protected reader, migration or authority acceptance is claimed.
