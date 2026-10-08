# Unsigned outer-message privilege diagnostics

Base integration 3f6b9c224e5cefbf809b360448650fc400884df3, accepted PR71.
Branch codex/cloud-outer-message-privileges.

## Inspected data flow

compile_unsigned validates provider route account metas, reads request-bound ALT
accounts, and uses solders MessageV0.try_compile plus a null signature. Previously
it returned raw bytes, resolved keys and provider outer instructions, while
inventory received only the keys/outer/simulation. Outer requested flags were
silently omitted; the inventory could not distinguish them from the message's
union of privileges across all instructions. RPC CPI data does not contain the
runtime signer/writable authority needed to fill that gap for inner calls.

The compiler now derives declared_message_privileges from its serialized unsigned
transaction. inventory gains an optional compiled keyword and receives that
witness through two minimal call-site changes in simulation and saved sell replay.
Existing positional inventory calls remain supported, but explicitly report
OUTER_MESSAGE_PRIVILEGES_UNAVAILABLE and null declarations. Basic operation
inventory checks retain their previous semantics; they do not claim privilege
completeness. Outer route coverage requires the new consistent declaration witness.

## Exact checks and output

The diagnostic resolver parses the exact bounded serialized transaction, checks
canonical byte round-trip, v0/one-payer/null-signature profile and sanitization,
then validates explicit header counts and static keys with the existing production
compiled resolver. Lookup account responses must match the original request's
method, encoding/commitment, ordered table identities/counts and layout/owner;
message lookup indices must be present in those table contents. Loaded writable
then loaded readonly segments retain exact membership/order; duplicate or missing
keys and wrong supplied key ordering reject. No network call is made by this
resolver or by inventory/coverage revalidation.

Every compiled outer instruction must match its indexed path, program, original
bytes and ordered accounts. Recompiling the original ordered route metas with the
same blockhash and request-bound tables must reproduce the exact message bytes:
header/static/loaded privilege claims cannot contradict the request/meta union.

Output preserves separate requested_account_metas and declared_account_privileges
for outer accounts (including message index/segment), with a message hash and full
header/static/loaded declarations. A local readonly request may have a writable
message declaration because another instruction uses the same account; those
fields are deliberately not equated. Original provider/evidence records are not
rewritten. Returned checker receipts now detach the original requested bool flags
as well as path/program/bytes/ordered account identities. Coverage independently
rederives the compiled declarations and compares every reported per-row/global
field before granting an outer diagnostic role; arbitrary supplied compiler
privilege summaries, inventory flags or message hashes cannot replace that step.
Legacy/incomplete receipts and missing/contradictory evidence remain uncovered.

If the simulation includes loadedAddresses, contradictory membership/order rejects
its inventory declarations. A matching value reports MATCHED_UNAUTHENTICATED;
absence reports UNAVAILABLE. This comparison does not authenticate RPC execution.

CPI rows retain requested_account_metas=None and declared_account_privileges=None.
They never inherit message signer/writable declarations as runtime CPI authority.
source_authenticated, finality_authenticated and runtime_cpi_privileges_authenticated
remain false, including consistent messages. ALT contents remain unauthenticated;
owner/layout/request binding proves no chain provenance or current table state.
Both full_route_policy_passed and transaction_policy_ok remain false. Runtime
privilege demotion/reserved accounts and CPI signer elevation remain unproved.

## Fixtures, compatibility and limits

The checked-in public mainnet-sell-simulation fixture contains original route
metas, resolved keys and simulation data, but no serialized unsigned transaction,
message header or compiler ALT account snapshot. Tests preserve it unchanged and
report missing declaration evidence; they do not synthesize a captured message
from its saved completeness/loaded-address flags.

New cases explicitly reconstruct a synthetic narrow direct-route variant from
those public route metas, with a synthetic ALT account deserialized by solders.
They exercise actual compilation and static/loaded privileges; they are not the
original captured transaction or proof of successful chain execution. Existing
saved-record simulation/replay fixtures also validate offline reconstruction from
persisted unsigned bytes, preserve source records and perform zero replay RPCs.

Dedicated attacks include altered header/sanitization, instruction order/bytes,
account/key order, missing/bad lookup request or membership bytes, reordered RPC
loaded addresses, invalid requested flag types, untrusted compiler summaries,
modified per-row/global declarations/hash and CPI inheritance. Receipt flags are
detached. Existing coverage fixtures without message witnesses now explicitly
remain uncovered instead of asserting complete privilege coverage.

The supported diagnostic profile is the compiler's unsigned v0 transaction with
one null payer signature and existing 1232-byte/64-key/64-outer/eight-table limits.
Unknown/legacy/signed profiles do not gain support through this change. ALT account
allocation/provenance and stable source inputs are inherited caller/storage
assumptions; no additional RPC, retry, authentication or policy admission is added.
Complete effect/control/order/fee policy and independent source/finality/live
acceptance remain coordinator-only dependencies. No service/private-control,
ownership, lifecycle, entry or paper-readiness approval is established.

Scope: compile/instructions/route receipt diagnostics, two required inventory
call sites, dedicated tests, backward-compatibility assertions and this report.
No engine/ledger, shared readiness/queue, provider transport, decoder, acquisition,
pool journal, signer, broadcast, VPS, secrets, paid services, merge or deployment.

Final validation: Python 3.12.14/Linux fixture-only targeted compile/inventory/
receipt/replay group, 56 tests in 0.129s, OK. Full unittest discovery, 1437 tests
in 52.536s, OK, zero skips/failures. git diff --check clean; no known failures.
