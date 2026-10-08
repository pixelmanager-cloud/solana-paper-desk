# Local coordinator raw capture command

Baseline: `integration/cloud-wave1` at `3aa4ec1acc2f3d8e9dde8184ebe354b15b6d5402`.
This is a disconnected diagnostic command. Independent review and coordinator
provisioning are required before any live invocation. No live invocation was
performed in this worker.

```sh
python -m desk.coordinator_capture_cli --coordinator-diagnostic \
  --evidence-db /absolute/private-local-directory/evidence.sqlite \
  --scan-id EXISTING_SCAN --descriptor-hash EXISTING_DESCRIPTOR_SHA256 \
  --mint CANONICAL_MINT --signature CANONICAL_TRANSACTION_SIGNATURE
```

The explicit flag expresses operator intent; it is not authentication. Only the
trusted local coordinator should invoke this command. The command requires Linux,
an existing canonical absolute path on the accepted positive local filesystem
allowlist, an owned directory with mode 0700, and an owned regular database with
mode 0600 and one hard link. Existing SQLite and ownership lock sidecars must also
be owned regular single-link files with mode 0600. Symlink aliases are rejected.
Operators must keep the directory, database and sidecars stable: no renaming,
replacement or linking while ownership workers run. Device/inode identity is
checked on each database connection; this is a trusted stable-path contract,
not protection against a hostile process with the coordinator's UID. Connections
use SQLite `mode=ro` for preflight and `mode=rw` afterward, so disappearance does
not recreate a database. New lock files use a private umask.

## P2 repair: nonmutating preflight

The original `mode=ro` preflight was insufficient: opening a WAL-mode database
and selecting its schema can create WAL/SHM sidecars even for a missing scan.
This repair rejects WAL **before opening SQLite**, including closed/checkpointed
WAL databases with absent sidecars and active WAL databases containing committed
newer evidence. It does not use `immutable=1`, copy the main database, checkpoint,
change journal mode, or read a stale snapshot that omits WAL.

The supported preflight is Linux LP64 with kernel OFD lock support, SQLite's
standard POSIX locking VFS, a rollback-format database header (both file format
versions 1), and no existing WAL, SHM or rollback journal. Persistent journal
files, unsupported headers/lock ABIs/kernels and active writers fail closed.
Operators must quiesce a database with unsupported journal state outside this
command; this CLI never repairs or converts it.

Before reading the header, the command opens the existing database read-only and
takes a nonblocking shared Linux OFD byte-range lock across SQLite's pending,
reserved and shared lock bytes. This conflicts with conforming writers and
journal-mode transitions. It persists across SQLite connection closes in the
same process, and is held through all admission preflight connections. Existing
readers may coexist; writer contention returns BLOCKED without retry or SQLite
open. Consequently schema and admission lookup see a consistent committed
rollback snapshot. After binding validation, the guard is released and the
accepted capture component revalidates the admission/budget under its existing
transaction rules. Nonconforming writers, nonstandard VFSs and hostile same-UID
file mutation are outside the explicitly trusted stable-path contract.

Import, help and argument errors do not open a database or transport. Binding
syntax is validated before database access; preflight reads the existing
admission and requires the exact supplied scan, descriptor hash and mint and the
existing 18-call ceiling. A signature must be canonical base58 for 64 bytes.
The first signature is explicitly supplied by the trusted coordinator; it is
not independently authenticated by admission metadata. The accepted capture
component binds that signature immutably with the scan, descriptor, mint, fixed
source and database. The command never creates an admission, resets a budget,
adds a scheduler or wires an entry flow.

The accepted capture component owns journal serialization, the single durable
claim per admission, reservation before invocation, immutable replay, and the
shared budget. A charged failure is not retried; an interrupted uncharged claim
also blocks recapture. Partial journals are rejected rather than repaired. The
transport is always the accepted fixed `HeliusMainnetRPC`, retaining its source
identity and one-request/no-redirect/no-retry policy. No URL, source, credential
or retry option is exposed. Only that adapter reads `HELIUS_API_KEY` at actual
invocation. Replay does not require a credential or contact a provider.

Output is one compact JSON object with capture state, validated hashes, budget
usage, logical RPC attempts, and false approval flags. It excludes paths,
bindings, raw records, diagnostic reasons, exception strings and provider bodies.
Argument errors are deliberately generic to avoid echoing secrets. Exit status
is 0 for diagnostic COMPLETE, 2 for argument errors, and 1 for every other state.
Budget usage is null when a safe bound result is unavailable. COMPLETE does not
authenticate chain provenance, finality, signatures, ownership or trading
eligibility. Token2022 runtime eligibility and all entry gates remain unchanged.

## Fixture verification

Tests execute the real module through subprocesses with a test-only patched
transport and synthetic records. The patched invocation checks that a claim and
budget reservation already exist. Socket creation is guarded; no real key or
provider is used. Coverage includes import/help/invalid arguments, missing or
wrong bindings, missing databases, permissions/aliases/hardlinks, immutable replay
and original rows, signature rebinding, exhausted and final-call budgets, null
results, secret-bearing response and exception text, failed terminal journal
writes, partial journals, and process death before reservation.

New regressions snapshot names, bytes, inodes, modes and modification times for
missing/wrong/valid bindings on closed WAL and active WAL with a committed
budget update. All are blocked with no mutations or transport invocation. Other
tests prove active rollback writers are rejected, a separate process cannot
reserve/write or switch to WAL while the guard survives a SQLite reader close,
capture succeeds after lock release, existing journals are not recovered, and
unsupported OFD locking has no SQLite fallback.

Python 3.12.14: focused suite `python -m unittest
tests.test_coordinator_capture_cli -q`: **20 tests passed in 6.256 seconds**.
The real subprocess writer/journal-mode lock test was additionally repeated
**20 times, all passed in 3.653 seconds**. Full Linux suite `python -m unittest
discover -q`: **1,264 tests passed in 52.761 seconds**, no failures or skips.
Existing loopback tests require the approved sandbox
execution exception; all provider tests remain fixtures.

Dependencies and limits: accepted PR50/51 capture and transport are prerequisites;
worker01 continues to own persistence and budgets. Independent review and trusted
coordinator provisioning remain outstanding. This is evidence collection only,
with no live verification, readiness signoff, integration, merge or deployment.
