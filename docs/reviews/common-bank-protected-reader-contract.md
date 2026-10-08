# Exact physical mixed-receipt format specification

Exclusive MIXED-RECEIPT-PROTECTED-READER prerequisite, extending PR89 append-only.
Base remains `28e59ac3da521c032cec49a26b603088c78e78f1`. PR85, PR87 and review
6060373059 require an exact physical format before protected reader/migration
code. The five previously missing choices are now concrete in
`desk/common_bank_receipt_format.py`. This defines a reviewable format, not an
installation, migration, issuer or production reader. Existing v1 code is unchanged.

## Exact schema and immutable certificate

Format ID: `pool_receipt_physical_format_v2`.
`FORMAT_SQL` is a read-only mapping of exact SQL strings. All v1 schema objects
remain byte-identical except REQUIRED `receipt_no_replace`, defined below.
There is exactly one added table and three added guards:

```sql
CREATE TABLE receipt_transition_certificate(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL,hash TEXT NOT NULL)
CREATE TRIGGER certificate_no_update BEFORE UPDATE ON receipt_transition_certificate BEGIN SELECT RAISE(ABORT,'Immutable format2 certificate'); END
CREATE TRIGGER certificate_no_delete BEFORE DELETE ON receipt_transition_certificate BEGIN SELECT RAISE(ABORT,'Immutable format2 certificate'); END
CREATE TRIGGER certificate_no_replace BEFORE INSERT ON receipt_transition_certificate WHEN EXISTS(SELECT 1 FROM receipt_transition_certificate) BEGIN SELECT RAISE(ABORT,'Immutable format2 certificate'); END
```

Certificate table cardinality is exactly one, id/rowid=1, TEXT body and hash.
The body is the exact canonical PR87 certificate; hash is its canonical SHA256,
externally pinned, not authority derived from candidate storage. Body <=8 KiB
UTF-8; hash exactly 64 bytes and lowercase hexadecimal via typed dispatch.
Unconditional UPDATE rejects every field change and rowid/_rowid_/oid/id alias,
including UPDATE OR REPLACE. DELETE rejects. BEFORE INSERT rejects any existing
row even under INSERT OR REPLACE, avoiding implicit replacement deletion.
The CHECK forbids alternate singleton IDs; the collision guard remains protective
even in the deliberately corrupt fixture with CHECK disabled. No SQL idempotent
replacement path exists. Future migration retry must audit and return the exact
existing result outside mutation, or refuse mismatches.

## Required-object old-binary fence

The exact new `receipt_no_replace` is:

```sql
CREATE TRIGGER receipt_no_replace BEFORE INSERT ON coordinator_receipts WHEN EXISTS(SELECT 1 FROM coordinator_receipts WHERE seq=NEW.seq OR publication_id=NEW.publication_id OR payload_hash=NEW.payload_hash) BEGIN SELECT RAISE(ABORT,'Format2 receipt identity already exists'); END
```

Only the error message differs from v1. All seq/publication/payload collision
predicates and ABORT behavior remain unchanged. INTEGER PRIMARY KEY seq aliases
rowid/_rowid_/oid, so collisions through those aliases are rejected too. Original
receipt/descriptor UPDATE/DELETE/REPLACE guards and head protections are unchanged.
V1 `_audit` compares required SQL byte-exactly, so both already-open reader and
writer reject before returning policy or publishing. Extra metadata alone is not
a fence. Tests prove the actual protected v1 API refuses while original receipt
rows, descriptor, head and every other original SQL object remain unchanged.

## Full object inventory and fingerprint

`EXPECTED_INVENTORY` is exactly 17 `(type,name,tbl_name,sql)` tuples ordered by
SQLite BINARY type then name. It includes all four tables, all eleven triggers,
and BOTH implicit indexes:

* `sqlite_autoindex_coordinator_receipts_1`, table coordinator_receipts, SQL NULL;
* `sqlite_autoindex_coordinator_receipts_2`, table coordinator_receipts, SQL NULL.

INTEGER PRIMARY KEY does not add an autoindex. No other objects are permitted.
Full object names/SQL are literal immutable module constants, not a partial
required-object subset. Views, extra/altered tables/indexes/triggers, missing
entries, nonempty TEMP/attached schemas, virtual tables and unknown formats must
refuse. No SQL whitespace/case normalization or CREATE-prefix inference is allowed.
The future reader uses a pristine connection, no ATTACH/TEMP/extension/arbitrary SQL.

Fingerprint is exactly:

```
digest({'format': FORMAT_ID, 'inventory': EXPECTED_INVENTORY})
```

`desk.model.digest` uses SHA256 of canonical UTF-8 JSON: sorted object keys,
compact separators, tuple arrays and SQL NULL as JSON null. Rootpages/file inode/
allocation and query order variants are NOT included. Expected fingerprint:
`186fde57edcf2d53007f6972c66484f64a156d319d4814cec3db062397e687e8`.
This value must match independently supplied ChainAnchor.schema_hash AND the
certificate. It cannot be provisioned by fingerprinting a candidate layout.
A hard-coded hash regression freezes this exact inventory against accidental
future v1/schema changes. Unknown fingerprint is unsupported, not auto-upgraded.

Before fetching inventory TEXT, fixed schema scalar SQL requires exactly 17
objects, correct TEXT/NULL storage classes, <=128 UTF-8 bytes for type/name/table,
<=2,048 bytes per SQL string and <=32 KiB aggregate inventory. Then the complete
ordered inventory must equal the literal expected tuple. All autoindexes remain
visible despite their NULL SQL; no `WHERE sql IS NOT NULL` pruning.

## Atomic discriminator and transition

The exact certificate singleton is the ONLY installed-format discriminator;
no new mutable marker table or application_id/user_version switch is introduced.
Format 2 exists only when its full schema/fence, singleton and exact PR87 transition
are simultaneously present and consistent with externally supplied pins.
Certificate alone, fence alone, missing/multiple singleton, extra/misplaced
transition, inconsistent old/current head or partial DDL is UNAVAILABLE. No v1
fallback, schema initialization, repair or prefix salvage is allowed.

Future migration (not implemented here) must lock the stable evidence then ledger
in established order and audit the exact original v1 identities/descriptor/prefix/
old head, reviewed source capabilities and authoritative offline quiescence.
Within ONE `BEGIN IMMEDIATE`/DELETE-journal/FULL transaction: replace the required
fence, create exact certificate table/guards, insert exact canonical certificate,
append the exact PR87 transition at old count+1 and update head atomically.
Original descriptor body/hash and every old row/byte/hash/sequence/publication
remain unchanged. No copying, rotation, reseeding, refund, VACUUM or normalization.
Crash before COMMIT leaves old v1; after COMMIT leaves complete format 2, or read
refusal. A retry may return only the exactly audited same transition/certificate;
unknown/partial/mismatched state refuses without issuing new IDs or resetting spend.
No downgrade or automatic restore to a less complete backup.

PR87 defines all typed dispatch, sentinel columns, certificate/old-head/current-
head bindings and source version/profile capabilities; this module reuses that
validator unchanged. Expired/disabled-source observations and all unresolved
markers are retained; no type/profile/source/time filtering is introduced.
Signed invalid semantic times remain visible for later rejection. Marker resolution
is unsupported; later successful observations do not erase failed attempts.

## Concrete file/header and UTF-8 SQL preflight limits

Supported physical profile is SQLite format3, EXACT 4,096-byte pages, rollback
read/write version=1, reserved-byte count=0, payload fractions64/32/32, schema
format4, UTF-8 encoding1, user_version/application_id=0 and zero expansion bytes.
This preserves conventional v1 headers; different page size/header profile is
explicitly unsupported, never converted. Header change counter must equal
version-valid-for, and valid header page count must match no-follow fstat size.
Freelist count/trunk range must be internally plausible, never repaired.

Maximum file size is 128 MiB: 32,768 pages, size page-aligned, >=one page.
Header input is exactly 100 bytes. Future protected reader checks owned stable
file identity/size before SQLite/schema scan and reads only this header first.
The physical envelope is four times the accepted 32-MiB serialized bound, allowing
B-tree pages, overflow pages and two indexes while bounding any schema/count scan
and physical I/O. The real 10,000-record fixture fits comfortably inside it.
This is a resource ceiling, not a guarantee that every bloated/freelist-heavy file
fits; otherwise valid but oversized files honestly refuse. No VACUUM/rewrite or
larger limit is used to recover them. Physical size does not enlarge logical spend
or byte budgets. The same logical 10,000 rows/8-KiB/32-MiB limits remain authoritative.

`PREFLIGHT_SQL` contains fixed READ-ONLY scalar SELECTs, never executed by this
module. It inspects complete tables, no source/profile/freshness filters. It uses
`length(CAST(value AS BLOB))`, not character length. `FormatMetrics` captures only
bounded integer tuples from those SELECTs. Checks before full body/row fetch:

* Descriptor/certificate: exactly one id1 INTEGER row, TEXT fields, body1..8,192
  UTF-8 bytes, hash64 bytes, exact aggregate body+hash bytes.
* Head: exactly one id1 INTEGER row/count, TEXT hash64 bytes, count1..10,000
  (format2 includes at least transition), agreeing with rows and external current
  count pin. Actual hash agrees with independently pinned current head in dispatch.
* Rows: count1..10,000 including transition/markers; integer seq1..count,
  all nine remaining fields TEXT, payload1..8,192 bytes, other fields1..128 bytes,
  complete aggregate bytes. PK uniqueness plus exact min/max/count rules out gaps.
* Schema: complete inventory scalar bounds above, followed by exact ordered audit.

Shared serialized aggregate is EXACTLY row-string bytes + externally pinned
legacy-prefix string bytes + actual descriptor body + actual certificate body +
externally pinned descriptor body, <=32 MiB, matching accepted chain accounting.
Hashes outside body and schema metadata have separate small fixed bounds; no
per-profile expansion. Prefix duplication is charged honestly. Preflight invalid
UTF-8/type/shape or bounds fails before JSON/row materialization. Invalid UTF-8
TEXT discovered on later fetching also refuses; scalar length alone is not JSON
or decoding authentication. Full snapshot dispatch rechecks all original bounds.

`validate_inventory`, `validate_header`, `validate_preflight` and
`validate_format_snapshot` are pure specifications/validators: no filesystem,
SQLite open/execute, locks, installation or providers. The combined validator
runs header/preflight/inventory then unchanged full chain dispatch, and verifies
loaded snapshot/inventory numeric metrics equal the preflight image. No cached
success or partial result. All malformed helper inputs become ChainUnavailable.
Supplying valid scalars/header does NOT authenticate physical storage or prove a
guarded read: all existing diagnostic protected-read/source/finality/raw-conflict/
ownership/entry flags remain false, decision REJECT.

## Reader boundary and remaining dependencies

The five choices are concrete; implementation is no longer blocked on undefined
format. Independent review of this exact specification precedes the protected
reader. Reader remains unimplemented. It must accept explicitly external boundary
and anchor pins, never derive trusted pins from candidate DB/certificate. Use
Linux/LP64 capability refusal before access, canonical no-alias/no-follow stable
owned single-link files, private root0700, ledger/lock0600, safe evidence permissions,
accepted durable filesystem and guarded inode identities throughout.

One complete read must hold evidence nonmutating guard then existing coordinator
shared lock then ledger nonmutating guard and one mode=ro/query_only transaction.
Reject WAL/hot/stale journals, contention, unsupported VFS/platform, replacements,
partial schema/heads and SQL/encoding errors without weaker fallback or recovery.
No creation of lock/database, repair/checkpoint, immutable=1, copies or cached
policy. Full bounded schema/scalar/descriptor/certificate/head/row read is one
consistent image; revalidate identities/schema/head before yield and guard exit.
Every use needs fresh revalidation. Same-UID whole rewrite/rollback/omission remains
outside the original coordinator threat model; local integrity is not provenance.

Offline migration/issuer implementation, authoritative journal completion and
quiescence, issuer permission enforcement, protected marker publication/resolution,
source/seal/revision/budget/raw replay and all-profile raw conflict comparison
remain separate dependencies. No unfinished slot-journal API is assumed or edited.
Request budgets remain unchanged, no provider/VPS/secrets/signing, shared queue/
readiness edits, merges, deployment or live acceptance.

## Fixture proofs

`tests/test_common_bank_receipt_format.py` provisions only fresh synthetic scratch
SQL fixtures. It tests exact inventory/fingerprint, original v1 SQL preservation,
certificate mutation/REPLACE/rowid guards, all collision paths, actual v1 API fence
refusal without row/descriptor/head rewrite, every missing/changed inventory entry,
extra object kinds, original/weakened fences, partial singleton/schema, external
pins, header/pages/freelist/file bounds, UTF-8/scalar/cardinality/shared byte limits,
loaded/preflight consistency, corrupted state after earlier success and real
10,000 accepted/10,001 refused records before dispatch. Raw witness references
remain explicit synthetic placeholders; no union RPC, source or completion proof.

The seven original PR89 contract proofs remain as historical regression evidence:
opaque candidate schema hashes alone still cannot select or authenticate a format.
The new spec closes the physical-definition gap without changing that limitation.

Linux/Python 3.12.14 validation of this exact specification:

* `python -m unittest tests.test_common_bank_receipt_format tests.test_mixed_receipt_reader_contract -q`:
  31 tests, 1.516 seconds, OK, zero skips/failures/errors (24 new format proofs and
  seven retained original contract proofs).
* `python -m unittest discover -q`: 1,722 tests, 91.379 seconds, OK,
  zero skips/failures/errors; existing private-loopback fixtures only.
* `git diff --check`: clean. No known failing tests remain.

The exact-format prerequisite is implemented for independent review; protected
reader/migration/issuer/raw replay and live acceptance remain unimplemented.
