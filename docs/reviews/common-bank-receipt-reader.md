# Disconnected protected mixed-receipt reader

Base `0ce2a333be6c149fc4ac2398815e7ab7f46abc79`, implementing the narrow next
slice from PR89 review comment6061009856. Changes are a new reader module,
dedicated tests and this scoped report. Format89, typed dispatch87, existing v1
ledger, ownership/budgets, entry logic and union journal remain unchanged.

## API, independently supplied pins and honest claims

`read_receipt_chain(CoordinatorBoundary, ChainAnchor)` opens a fresh nonmutating
read and returns the existing frozen TypedChain only after connection/guard exit
checks succeed. It takes no profile/source/time selector, candidate DB path,
certificate-authority flag, policy, arbitrary SQL or default pins. Boundary and
anchor are explicitly supplied by trusted local code; Python types themselves
do not authenticate that caller. No pin is created/refreshed from candidate DB
or certificate. Stale or mismatched independent current head refuses.

Returned scope diagnostics remain REJECT with ALL existing protected-read/source/
finality/raw-conflict/journal/history/ownership/interval/private-control/entry
flags false. Available local metadata is not source authentication, raw conflict
clearance or current admission permission. Output is NOT a v1 policy or loader.
Every later current use requires another fresh read and independent pins; there
is no success/prefix cache. Source/certificate permissions remain syntax and
historical inventory, not current issuer enforcement or cryptographic authority.

## Guards, identities and nonmutation

Linux LP64 capability is checked before pins/path/DB access. No weaker-platform
fallback. Locally supplied canonical absolute paths are required; symlink aliases,
hardlinks, URI identities, missing locks, unsupported local filesystem and overlap
refuse. Root must be owned/private0700; ledger and existing coordinator lock owned
regular single-link0600; evidence owned single-link and not group/world writable.
If evidence is outside the private root, its direct parent must also be owned,
not group/world writable and stable throughout the read.

All four entries are opened O_RDONLY/O_NOFOLLOW/O_CLOEXEC (directories also
O_DIRECTORY), with held fds and externally pinned original descriptor dev/inode
identities; lock additionally pins its original mtime. Root/path/options/registry
must agree with the independent descriptor, not candidate data. Entry/exit checks
compare named and held-fd identities/ownership/modes; regular files also retain
size, mtime and ctime. Atime is not a mutation claim. Parent/root directory content
mtime is not treated as immutable, but identities/modes remain stable.

Lock order: accepted nonmutating evidence OFD guard, existing coordinator LOCK_SH
(nonblocking, no creation), accepted ledger OFD guard. Standard noncooperating
SQLite writers on either file are blocked by the guards even across another
connection close. Existing writers/coordinator contention fail immediately;
there are no sleeps/retries. Accepted guards reject WAL/hot/stale journal/shm
files before SQLite open, including dangling sidecars, and hold rollback-only
byte-range read protection until the ledger connection has closed.

SQLite uses the validated canonical filename with mode=ro; the OS no-follow fds
and accepted guards remain held and path identities are revalidated throughout.
No /proc/fd alias, immutable=1, file copy or weaker VFS bypass is used. Standard
Python SQLite does not expose a separate SQLITE_OPEN_NOFOLLOW flag. Transient
malicious same-UID whole-file rewrite/ABA rollback/omission remains outside the
original coordinator threat model; persistent replacement/rotation is detected
and unsupported. No source authenticity follows from POSIX/hash integrity.

## One coherent preflight-before-load read

The pinned ledger fd supplies exactly100 header bytes and fstat size. Accepted
format89 fixed4KiB/128MiB/page/header profile is checked before SQLite open.
One pristine connection disables extensions, sets query_only=ON/trusted_schema=OFF,
requires exactly the canonical main DB and DELETE journal, and starts one read
transaction. A restrictive authorizer permits only fixed reads, needed scalar
functions, connection pragmas and transaction boundaries; mutation, ATTACH, TEMP
DDL, arbitrary functions and disabling query_only refuse. SQLite's optimized
COUNT(*) read notification uses empty column/databaseNone and is permitted only
for the fixed allowed tables. No candidate SQL/function/extension is registered.

Schema scalar UTF8/type/count bounds run BEFORE fetching SQL TEXT inventory;
exact full17-object inventory including NULL-SQL autoindexes is then checked.
All fixed descriptor/certificate/head/rows/schema scalar preflights run in that
same transaction before ANY full descriptor/certificate/head/payload values are
loaded. SQL BLOB lengths count UTF8 bytes. Exact cardinalities/storage classes,
10,000 rows/8KiB fields and shared32MiB accounting include pinned prefix duplication.
Missing/partial/extra/unknown objects or singleton/head inconsistency refuse.

Only after preflight does `_load_snapshot` fetch bounded singleton values and
ALL ordered10-column original rows, no source/profile/freshness pruning. The
accepted `validate_format_snapshot` delegates full `validate_receipt_chain`,
checking original byte-exact legacy prefix/old-head/certificate/current-head pins,
typed dispatch/capabilities/sentinels/hash chain, and loaded numeric metrics
against preflight. All expired/disabled-source observations and every applicable
unresolved marker remain retained. No marker resolution is inferred from a later
observation. No raw objects/union banks/history are loaded or compared here.

Schema and head are checked again before ending the same transaction. ROLLBACK
closes the read transaction without writes; connection closes under both OFD
guards. Named/fd identity/stamp checks run after connection and at all guard exits;
only then does the function return. Exceptions become bounded static
ReaderUnavailable codes; no partial result or stale success is salvaged. The SQL
progress handler and completion check enforce a ten-second read deadline, without
retries. OS syscall scheduling and hostile same-UID mutation are not hard real-time
or stronger authenticity guarantees.

Preflight prevents upstream PYTHON full-value materialization; SQLite itself
must parse schema/read B-trees to answer scalar queries. Fixed physical-file
bounds and progress checks bound that stage, rather than claiming the pure89
header validates B-trees. SQLite/UTF8/corruption failures refuse; no recovery,
checkpoint, journal removal, lock/database creation, schema repair, migration,
issuer writes, budget reset/decrement or provider calls occur.

## Fixture validation

Actual private synthetic format2 DB fixtures preserve a real protected v1 prefix
and use independently prepared test boundary/anchor pins. Fixture setup alone
creates format SQL/certificate/typed observations; product reader has no installer.
Parent/manifest/seal/journal references remain synthetic unavailable placeholders,
not real raw union witnesses or successful completion evidence.

34 dedicated tests verify complete positive reads and file inode/size/mtime/ctime/
SHA256 preservation, ALL profiles/expired/disabled observations/markers, unknown
identity blockers, independent/stale pins, one-transaction scalar-before-load
traces, real10,000 rows,10,001 refusal, real shared32MiB aggregate and UTF8 bounds,
extra/partial/oversized schema and singleton/invalidUTF8 refusal, prior-success
corruption, hot/WAL/shm guards, physical/header overflow, canonical/hardlink/
permission/missing-lock refusal, immediate contention, noncooperating SQLite
writer blocking on both DBs, ledger/evidence/lock/root replacement races, exit
permission changes, authorizer/pristine connection behavior, deadlines and
portable unsupported-platform failure before filesystem/DB access.

Linux/Python3.12.14:

* `python -m unittest tests.test_common_bank_receipt_reader -q`:34 tests,
  1.942 seconds, OK, zero skips/errors/failures.
* `python -m unittest discover -q`:1,777 tests,103.269 seconds, OK,
  zero skips/errors/failures. Existing private-loopback fixtures only.
* `PYTHONPATH=. python work/reader_external_parent_probe.py`:two independent
  actual-reader probes passed (safe external parent; writable parent refused
  before DB open). Script preserved locally under checkout work/.
* `git diff --check`:clean. No known failing tests remain.

An initial focused run had11 errors because the restrictive authorizer denied
SQLite optimized COUNT notifications; that exact read-only event was allowed,
without relaxing mutation/function restrictions. The same run exposed a race-test
cleanup error before its hook ran; cleanup now requires an actual swap and the
test asserts all three replacements executed. Subsequent focused/full runs pass.

## Remaining dependencies

This is disconnected local read-only metadata acquisition. Trusted independent
pin provisioning, atomic offline migration and issuer permission implementation,
authoritative completion/quiescence and protected marker publication/resolution,
source/seal/revision/budget/raw replay and shared all-profile common-bank conflict
comparison remain separately reviewed work. Evidence guard establishes a stable
file window, not raw coverage or existing-record validity. No unfinished slot-
journal completion API is assumed or edited. No provider/VPS/secrets/signing,
shared queue/readiness edits, merges/deployments or live acceptance claims.

Exact-head/tree and full tracked-source path/mode/blob/SHA256 manifest are
published with the PR; manifest and full test log remain in checkout work/ for
coordinator/independent review retrieval.
