# Disconnected common-bank completion inventory

Base: `e0c9fbefdba58ebf5b6d2dd7b32cc466d36a96d8`, fetched accepted
`integration/cloud-wave1` (including reviewed PR91 and this exclusive claim).
Branch: `codex/cloud-completion-inventory-01`.

This is a read-only prerequisite for later offline migration. There is no
migration, recovery, initialization, issuer, provider, command, operational
quiescence assertion, refund or runtime admission integration. Production
journal/view/receipt implementations are unchanged. No existing module imports
the new inventory. All diagnostic permission/authentication flags remain false.

## API and independently supplied boundaries

`read_completion_inventory(InventoryPins(...)) -> CompletionInventory` requires
local operator-supplied original ledger descriptor bytes, exact ledger head,
research canonical path/device/inode, journal descriptor bytes and optional
exact legacy capture head. An installed format-2 ledger additionally requires
the independent `ChainAnchor` used by the accepted typed-chain validator.
Candidate metadata cannot initialize any of these arguments. These pins bind
local originals, not issuer authority or Solana truth. There is no default,
selector, cached success, automatic repinning or partial inventory fallback.

`InventoryUnavailable` returns no rows. Unknown journal installation/version,
partial/additional journal objects, missing triggers, invalid ledger format,
unknown stage kinds, invalid counters, missing admission/run bindings, malformed
original chains, stale pins, unsupported file boundaries or resource failures
refuse the whole inventory. An entirely absent common-bank installation is
enumerated honestly with `COMMON_BANK_JOURNAL_NOT_INSTALLED`; a partial
installation is not treated as absent. Absence requires zero installation
markers and no journal-family objects. A legacy capture head must be explicitly
pinned if that family exists and must be absent in the pins otherwise.

## Complete scope and original records

One guarded window enumerates all rows in these selected families, without
pool, mint, slot, source, profile, freshness or successful-state filtering:

- `pool_capture_config`, `pool_capture_head`, every append-only legacy
  `pool_capture_events` row, including original PENDING rows subsequently
  followed by DONE/FAILED. Supported stage order/binding is checked without
  reading a provider, issuing receipts or replaying pool state.
- The descriptor, head and every `coordinator_receipts` row. Exact current v1
  SQL/collision protections and both autoindexes are required; all v1 payloads
  and chain hashes are checked against independent descriptor/head pins.
- Format-2 additionally retains its immutable certificate and every typed row
  and marker, and calls `validate_receipt_chain`. Exact accepted physical schema
  inventory and header are required. Expired/disabled-source observations and
  unknown-identity/slot markers are not dropped. These records are not granted
  raw conflict, source/finality or completion authority.
- All common-bank run/plan/meta rows and all eight possible intent/attachment
  rows across genesis, slot, union and clock additive versions. Exact version
  dispatch and unchanged SQL/triggers are required. Profile metadata binds
  each additive profile to its prior schema and original database identities.
- Every ownership budget and admission, including unrelated budgets. The one
  existing `used`/`ceiling` counter remains unchanged and must satisfy
  `0 <= used <= ceiling <= 18`. The existing read-only admission inspector
  validates descriptor/prepared-source/seal identities. Run source hashes,
  initial counts, charge progression and attachment counts bind to those rows.
- Every saved research scan (the current exact five-column table contract),
  including unfinished/unrelated scans. Missing/different original sealed
  scan rows are explicit blockers rather than caller summary substitutions.
- All evidence page **catalog entries**: original hash, declared decompressed
  length and compressed payload byte length. Compressed page BLOBs are not
  materialized, decompressed or claimed verified. Missing legacy record refs
  are explicit blockers. Page byte/hash/semantic replay remains a separate
  dependency; a matching catalog key is not proof of original raw validity.

Common-bank attachment `request_bytes`/`response_bytes` and failure JSON are
retained byte-for-byte. Stored SHA256/hash fields and local predecessor chains
are checked; this does not replay response semantics or upgrade diagnostic
DONE. No union response is sliced into a fabricated `rpc_response_v1`.
Structural journal checks are independent of worker02's semantic replay.

The returned immutable `tables` image contains `(database_index, table_name,
original_rows)` tuples: 0=evidence, 1=ledger, 2=research. Page rows deliberately
contain the catalog projection above. `schemas` preserves the complete schema
inventory for each database, including autoindexes and unrelated objects.
`file_identities` includes held original device/inode/mode/owner/link count,
size/mtime/ctime boundaries. `snapshot_hash` length/type-delimits original
values and hashes schemas, external descriptor/head pins and held original
file boundaries; bytes are not JSON-normalized. This is a local
inventory fingerprint, not an authenticated completion certificate.

## Read-only protection and resource boundaries

Linux LP64 capability is checked before pins/files/SQLite. Canonical absolute
paths, no symlink/hardlink aliases, original device/inode pins, owned 0700
directories and owned 0600 files/lock are required. Distinct research, evidence,
ledger and existing coordinator lock identities are enforced. External direct
parents are held and checked too. No lock file is created. Existing 0644 files
are deliberately unsupported by this private offline inventory contract; the
reader never changes their permissions.

Guard order is research OFD read guard -> evidence OFD read guard -> existing
coordinator shared nonblocking flock -> ledger OFD read guard. This is compatible
with research-worker/ownership/ledger writer ordering and the protected receipt
reader's evidence/coordinator/ledger order. The inventory does not enter the
journal's writable public session or create its worker/ownership sidecars.
OFD guards block standard SQLite writers even across connection closes.
Contention, hot/stale journal, WAL/SHM and unsupported locking fail immediately.

Three pristine `mode=ro`, `query_only=ON`, `trusted_schema=OFF` connections each
hold one read-only transaction simultaneously inside that complete guard
window. Extensions are disabled and an authorizer denies writes, ATTACH,
unexpected functions and writable PRAGMAs. No `immutable=1`, checkpoint, repair,
snapshot copy, recovery or schema installation occurs. Held/named identities
and file stamps are revalidated before transaction exit, after connection
close and after guard exit. Corruption after a prior success requires a new
complete read and fails, rather than using a prior prefix.

All three schema scalar preflights precede full SQL inventory values: at most
256 objects/database, 128-byte names, 8KiB/object SQL, 256KiB aggregate SQL per
database. Primary files are at most 256MiB each; format-2 additionally enforces
its accepted 128MiB/4096-byte-page header contract. Shared row and UTF8/BLOB
preflight finishes across **all three databases before any body/row loading**:
10,000 selected/catalog rows total, 32MiB total values/catalog allowance,
128 rows per common-bank stage, 2MiB maximum row. Field limits are 2MiB for
saved/prepared sources, 64KiB plans/responses, 16KiB union intent, 8KiB general
JSON/request fields, 2KiB failures and 128 bytes for scalar identities. SQL
checks scalar storage types and UTF8 byte lengths, not character counts;
invalid UTF8 rejects during bounded materialization. The page catalog validates
declared raw lengths <=16MiB without inflating them.

The shared 10,000-row bound includes metadata/catalog/scan rows; consequently a
ledger already at its independent 10,000-publication ceiling cannot fit this
inventory with its metadata. Refusal is intentional, never truncation or
pagination interpreted as completeness. A 10-second SQL/projection deadline
and SQLite progress handler further bound work. SQLite must itself parse its
schema/B-tree to answer scalar queries; this does not claim that no internal
SQLite allocation occurs before Python row materialization. As with accepted
readers, same-UID hostile transient whole-file substitution/ABA and authorized
original-record omission require external operating controls.

## Honest completion and external offline requirements

`diagnostic()` always says REJECT with zero provider calls and false
operational-quiescence, completion-authentication, migration, source and entry
flags. It always reports `OFFLINE_EXCLUSIVE_CONTROL_REQUIRED`,
`RAW_SEMANTIC_REPLAY_REQUIRED` and `COMPLETION_NOT_AUTHENTICATED`.
An original PENDING row is retained even after an attachment; this is distinct
from a currently unresolved intent. PENDING without attachment, unstarted runs,
failed attempts, unfinished legacy captures, unsealed admissions, missing raw
records and changed/missing saved source scans are separately visible. Every
common-bank run still requires an independent completion audit.

This window prevents conforming SQLite writes **during this read only**. It
does not prove all workers/transports are stopped, that no RPC remains in
flight, that a pending attempt never reached a provider, that a delayed
publication cannot arrive, or that independent original files form an
authoritative historical cross-database checkpoint. Queue/Jobs operational
state and unselected worker journals are outside this acquisition inventory;
their full schema is visible but their operational quiescence is not certified.
No pending attempt is resumed, refunded or marked completed.

A future offline migration still needs independently established exclusive
worker/transport shutdown, authoritative complete file/issuer registry and
externally pinned final heads, verified raw semantic replay, explicit handling
of every unavailable/ambiguous record and an independently reviewed atomic
migration/rollback protocol. Synthetic tests establish physical feasibility
only. They do not provision trust or permit real-data migration/receipt issuance.

## Validation

Initial published head `714e1e915ae74b965150b8a234c89d92a03b8590`:
Python 3.12.14 fixture-only targeted suite: 24 tests, zero skips, PASS in 5.290s.
Full Linux suite: 1,841 tests, PASS in 135.092s, zero failures,
errors or skips. Commands: `python -m unittest tests.test_common_bank_completion_inventory -q`
and `python -m unittest discover -q`. Logs are retained under local untracked
`work/inventory-focused.log` and `work/inventory-full-suite.log`.
Tests use actual SQLite/OFD windows: all four stages with original wire bytes,
charged pending/failure persistence, complete legacy events/publications,
all-profile typed observations and markers, corruption after success,
future/partial schemas, wrong/missing pins, preflight-before-load bounds,
writer/coordinator contention, nonmutation hashes/stamps, writer blocking during
materialization, SQL authorizer refusal, alias/mode/WAL/hot-journal rejection,
inode substitution, missing saved originals and portable platform refusal.

Initial harness failures were corrected: the reused journal `reserve` helper
requires its explicit frozen fence; another reused stage helper constructs a
fresh fixture that must be rebound before reading. A contention probe originally
closed a same-process SQLite file through checksum reading after BEGIN,
releasing POSIX locks; checksumming now occurs before the writer transaction.
The mixed-profile fixture initially omitted its original source's v1 capability
and correctly refused; that synthetic capability is now explicitly included.
No production guard or gate was weakened to make those tests pass.

## PR94 independent structural-review repair

Independent06 comments [6062498707](https://github.com/pixelmanager-cloud/solana-paper-desk/pull/94#issuecomment-6062498707)
and [6062553574](https://github.com/pixelmanager-cloud/solana-paper-desk/pull/94#issuecomment-6062553574)
correctly identified two defects despite passing fixture suites. The original
name-keyed family comparison collapsed a genuine table/trigger name collision;
partial local intent validation accepted rehashed malformed timestamps, boolean
ordinals, unsupported parameters and wrong fences. Both required structural
repairs; deferred raw semantic replay was not a reason to accept them.

The forward repair compares complete ordered `(type,name,tbl_name,sql)` tuples
and cardinality against accepted constant inventories, including exact automatic
indexes. Tables and triggers sharing a name cannot overwrite each other. Owned
families include every object attached to their tables and every reserved object
name, including legacy `protect_*` names. Legacy receipt comparison also uses
complete typed tuples; no name-to-SQL candidate map remains. Fixed accepted SQL
constants provide every selected table and column; candidate introspection
confirms that fixed contract rather than selecting arbitrary columns/objects.
Real table/trigger and index/trigger collisions on both common-bank and legacy
families refuse before any `table_info` or row body loading. The ledger collision
case is also covered. Schema scalar preflight now includes type/tbl_name storage
types and UTF8 byte bounds and the full schema string aggregate, not SQL alone.

Local records must have exact supported field sets. Descriptors bind exact
source/seal/plan/database fields and typed identities/counters. Plans validate
their local address arrays/order/indices and reference types while leaving raw
discovery justification unverified. Intents enforce strict nonboolean integer
ordinal/reservation time/charge types, monotonic local times, exact predecessor
fence and supported method/parameters. Union/clock parameters bind stored prior
slot declarations and the exact persisted plan union. Attachments enforce exact
field sets, integer completion times, copied reservation/charge/fence/source
bindings, original canonical request bytes, exclusive response/failure presence,
bounded local failure categories, declared reason/slot/floor/clock field types
and literal false permission flags. These checks do not replay raw response
results or authenticate declared DONE. Legacy descriptors additionally bind
their canonical pool identity and legacy timestamps use the existing strict
integer range. No writable journal session, PR93 API, provider, recovery or
schema installation is invoked by the inventory.

New adversarial tests preserve charged counters and exact hash chains while
mutating local records. They reproduce all four reported genesis-intent attacks,
extra fields and range/type variants; rehash malformed local attachment fields,
stage-specific slot/union/clock fields and unstarted descriptor/plan records;
and rebuild a complete legacy chain around invalid timestamps/pool binding.
Successful originals continue to pass with all diagnostic flags false.

Original CI attempt `37794887536` at head714e1e9 failed on the existing dashboard
Node BUSY harness ten-second timeout; its failure remains recorded. Independent
review reported PR workflow `37794988759` PASS and a requested push rerun pending
at its boundary. No timeout or unrelated harness code is changed, and no rerun
success is inferred. Final corrected-head fixture results and source manifest
are recorded in the PR repair comment; independent rereview remains required.

Forward repair validation (Python 3.12.14, Linux, fixture-only): 34 dedicated
tests PASS in 8.595s; full 1,851-test suite PASS in 135.654s, zero failures,
errors or skips. `git diff --cached --check` passes. Exact logs are retained in
`work/inventory-repair-focused.log` and `work/inventory-repair-full.log`.
The repair appends to the original published commit; no rebase/force push,
shared queue/readiness edits, provider calls, migration or approval occurs.
