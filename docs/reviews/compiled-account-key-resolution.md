# Compiled JSON account-key resolution prerequisite

Base: integration/cloud-wave1 at 4879956. Production code has no research imports.

`desk.account_keys.compiled_keys` accepts explicit legacy or version-zero JSON,
validates static keys and the exact message header, and derives requested OUTER
signer/writable privileges from required/readonly signer and unsigned counts.
Version-zero lookup descriptors must be present. Lookup tables and their index
segments must be well formed, nonduplicated and consistent with loaded-address
segment lengths. Resolution order is static, loaded writable, loaded readonly.
Loaded keys are never transaction signers. Duplicate transaction keys, invalid
addresses, unknown versions, boolean/noninteger counts or indices, overflow and
contradictory lookup metadata fail closed.

The decoder resolves compiled program/account indices into a local instruction
view without modifying source payloads or synthesizing jsonParsed instructions.
Strict checks apply to outer and inner indices, inner parent groups and token
balance indices. Repeated account references within a single instruction remain
valid; duplicate transaction keys and duplicate inner parent groups do not.
The separate `outer_message_privileges` output is requested message privilege,
not runtime demotion state, CPI signer privilege, authority proof or approval.
Existing parsed-key decoding remains unchanged for supported versions, including
strict token control inventory. Raw SPL instructions remain undecoded; neither
initialization, supply, transfer nor authority fields are invented.

The pinned legacy Pump reference is now processable directly from its unchanged
compiled JSON. Its hash/provenance remain unchanged. Existing event schema
mismatches, undecoded token operations, unavailable original request binding and
unverified finality remain blockers. Dedicated reference assertions verify that
account initialization inventory, launch ancestry and trading remain unverified.

All tests use synthetic fixtures or the existing provenance-pinned reference.
No providers, VPS, signing or broadcasting were accessed. This prerequisite
unlocks raw index resolution only: it does not complete raw token decoding,
historical event support, birth evidence, ownership or live acceptance.
