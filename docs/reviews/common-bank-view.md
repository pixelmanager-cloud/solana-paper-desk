# Disconnected common-bank manifest/index validator

Base `b4cbfaf588412cbd49a94f66b12abfa3f5b1c9a1`; slice1 of the accepted
common-bank acquisition design. Only `desk/common_bank_view.py`, its dedicated
test file and this report change. No capture/journal/receipt/schema/consumer or
entry-policy integration. Worker02's journal API is neither imported nor assumed.

## Narrow callable contract

`validate_common_bank(manifest_hash, view) -> CommonBankView` accepts an existing
trusted read-only `control_obligations.ReplayView`. The caller holds the existing
read guard and supplies a persisted content hash, not an inventory or completeness
flag. Validation loads only through that view; physical reads/reference/byte
limits remain shared with the caller's other consumers. There is no parallel raw
cache, DB writer, provider transport, reservation or receipt issuer.

Success means local original-parent/index/protocol binding, never an approved
source, authenticated bank, complete holder history, final CPI execution or entry.
Refusal raises static `CommonBankError` and returns no partial view. This module
is not called by any existing runtime consumer. Existing v1 readers remain strict.

Stored manifest kind is **`common_bank_manifest_v1`**, with exactly these fields:

```
kind, mint, discovery_hash,
genesis_response_hash, slot_response_hash,
bank_response_hash, bank_request_hash,
clock_response_hash, clock_request_hash,
keys, frontier, ownership_indices, pool_indices
```

This is the disconnected view's manifest, not the proposed journal's
`ownership_common_bank_v1` bank-pointer manifest. Future journal integration must
explicitly bind these references to its scan/descriptor/source/seal/plan/fence;
this validator deliberately does not pretend to verify those mutable bindings.
A new producer must construct and persist the exact view manifest from audited
records; it cannot rename journal records or pass caller manifests as receipts.

`discovery_hash` addresses the whole persisted coverage JSON object, including
its inner evidence_hash; that inner hash still independently binds rebuilt query
coverage. It is not a caller's initialization_inventory_verified summary. The
existing replay_history reconstructs coverage from exact saved history request
manifests/pages with their original method/options/cursors/hashes. account_inventory
then reconstructs F and requires its existing launch/initialization/discovery
checks. Discovery is genesis-slot-bounded, with integer gte=0, lt>0 and cutoff
D=lt-1<=the persisted finalized request floor S. No time-only or fabricated birth
summary replaces this evidence. Incomplete/raw-unbound/contradictory or unsupported
initializations reject; Token-2022 initialization programs reject.

P is independently derived by the existing canonical_accounts(mint), with exact
pool/mint/wrapped_SOL/LP/base_vault/quote_vault order. Base vault must occur in raw
F. Reconstruct U=P+sorted(F-set(P)), mint plus sorted-F ownership indices and six
pool indices. Require unique canonical pool identities, mint not in F, <=100 keys
and exact declared arrays; bool/duplicate/omitted/foreign/reordered indices cannot
satisfy equality. F is discovered history, not proof of complete state at later T.

## Original evidence and raw state

Genesis and slot records have exact rpc_response_v1 kind/method/params/result
shape. Genesis must equal the existing pinned mainnet genesis and slot request is
exact finalized getSlot. S, T and clock are nonnegative exact ints below2^63;
booleans reject. Bank request is exactly getMultipleAccounts(U,base64/finalized/
minContextSlot:S). Returned actual T>=S; the request floor is never rewritten to T.
Clock record is exactly getBlockTime(T). Request manifests have exact
`common_bank_request_v1` shape with pinned network/genesis, original method/params
and exact original response hash. This is a new disconnected manifest shape,
not a v1 pool-vault receipt. Bool/int aliases are rejected by canonical JSON
comparison in nested request/clock binding, as well as explicit integer checks.

The parent preserves every original result/context/value field. No provider-
claimed getMultipleAccounts response is synthesized from a subset. Frozen
CommonBankView holds its original parent JSON and hash, tuples of validated
indices/keys, actual T/time and original S/D. `account(key)` returns a detached
original account or explicit None. Diagnostic output has no method/params/result
RPC-envelope shape. Neither accessor writes a new evidence object. Changing a
returned account cannot mutate the parent or shared cache.

Reuse pinned production raw-policy helpers: legacy pool length/discriminator/
PDA/bump/creator/mints/vaults/padding/profile, initialized82-byte base/native/LP
mints with correct authority/decimals, and165-byte base/native vaults with exact
mint/pool authority/delegate/close/native-rent/amount bindings. Base supply must
be positive and cover vault amount. Present holder entries require legacy owner,
nonexecutable positive integer lamports, exact165-byte base64 layout/space,
non-native state, matching target mint and unique raw-discovered initialization
owner plus strict existing holding policy. Multiple different initialization
owners reject as ambiguous; matching declarations are not a lifetime theorem.
No decoder/normalizer/account_inventory semantics are changed.

Null is forbidden for any of the six live pool roles. A null historical holder
remains in F and its role indices, explicitly
`ABSENT_AT_BANK_UNVERIFIED_LIFETIME`. It is never converted to zero amount,
asserted closure, invented owner or omitted coverage. Full per-account history/
ending/close-recreate proof remains a downstream obligation. Even D=T does not
turn provider discovery into authenticated frontier completeness.

Bounds: view records/parent64KiB each, coverage object2MiB, discovery<=18 saved
pages (existing replay enforces100 rows/page), <=100 union keys, and the inherited
shared128-reference/32MiB ceilings. All referenced content is checked by shared
ReplayView and independently rehashed at the manifest boundary. Missing,
compressed/decode/hash errors and exhausted limits refuse without a partial
view. Existing source/storage/transport budgets are unchanged; no RPC attempts
are made or charged here. Successful validation does not prove budget admission.

## Proof and remaining dependencies

Dedicated tests use the accepted transformed three-holder history and synthetic
pool point. They persist one original union response rather than fabricated
six-account RPC slices. The positive fixture adds explicit positive lamport
metadata only to its NEW synthetic union because the older ownership fixture
omits it on two ordinary holders; no existing source/raw record is rewritten.
Both shared mint/vault entries and the original parent hash survive indexed
lookup; all source files/records remain unchanged during validation.

Tests cover exact original hashes/arrays, copy isolation, T>S, null holder versus
null pool role, forged completeness/frontier, omitted/bool/duplicate indices,
order/count/method/finality/floor/clock/genesis/request mismatch, raw discovery
coverage/page damage, Token-2022, mint/vault/holder authority/layout/padding
attacks, inherited shared cache and per-record bounds, actual persisted raw
initialization discovery yielding100 accepted keys and101 refused keys, and
existing v1 reader rejection/no sliced evidence insertion. Portable tests cover
trusted shared-view requirement and bool/int request-alias refusal.

Every diagnostic has decision REJECT; source/finality authentication, bank-frontier
completeness, history, closed lifetimes, CPI success, interval exclusion, common/
private control, lifecycle, production point prerequisite, ownership and trading
permissions remain false. Existing scheme hashes bind local records, not chain
truth. Source/seal/revision/budget/journal fencing, lock ownership, authoritative
receipt issuance, cross-profile conflicting-observation retention, coordinated
reader wiring, post-T full-history reconciliation and live acceptance remain
independent follow-up work. This module neither drops nor selects receipt
observations because it receives no ledger/policy; it cannot satisfy that audit.
No providers/VPS/secrets/signing/broadcasting or shared readiness/queue edits.

## Validation

Python3.12.14, fixture-only providers and local SQLite; existing private loopback
fixtures only in the full suite.

- Final focused: `python -m unittest tests.test_common_bank_view -q`:
  25 tests in4.830s, OK.
- Final full: `python -m unittest discover -q`:
  1,611 tests in72.648s, OK; zero failures/errors/skips.
- Python compilation/AST and staged whitespace check: passed. No other desk
  module imports or invokes common_bank_view; only the three scoped files change.

Earlier focused tests exposed missing lamport metadata in the reused holder
fixture (five positive errors); the new synthetic bank now supplies it, retaining
strict validation. A resource-limit test exposed a definition-time default bound
that ignored runtime limit substitution; the bound now resolves on each call.
A pool-padding attack test assumed a shorter layout and raised ValueError for a
negative padding count (also one error in preliminary full1,611/72.852s); it now
preserves the actual allocation and alters the reserved tail. Final results above
include these corrections. No known remaining failures. No live provisioning,
receipt/source authentication, journal crash proof or readiness is claimed.
