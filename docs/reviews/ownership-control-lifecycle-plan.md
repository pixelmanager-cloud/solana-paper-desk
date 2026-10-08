# Ownership control lifecycle: minimum legacy launch support plan

8 October 2026 (Korea time), Agent 07. Architecture planning only; no production
code, schema, test expectation or evidence gate is changed. This report does not
approve a launch, ownership milestone, SELL-1, paper entry or live readiness.

Context: [PR27](https://github.com/pixelmanager-cloud/solana-paper-desk/pull/27),
reviewed head `dbffb7551ce2925156b1c8a173f4b0c38ad4f6c9`, based on integration
`bcb464eae57aad91e0cf6dd9aa2188e87e2796f5`. Its diagnostic control inventory and
fail-closed rejection must remain until a separately reviewed semantic checker
can resolve individual witnesses. This report proposes that checker; it does
not repeat PR27's fix or give a PR27 approval verdict.

## Recommendation

The first positive profile should support exactly one original Pump **legacy
`create`**, followed within its successful creation instruction by initial supply
issuance and irreversible mint-authority removal, plus ordinary legacy ATA
creation. It need not accept arbitrary owner/delegate/close/freeze transitions.
Decode every such transition and retain its ordering and safety consequences;
accept only the narrow proved birth operations below. A supported revocation
must not act as permission to erase earlier controls.

Separate three conclusions: instruction semantics reconstructed, ownership history
reconciled, and ownership safety/coverage approved. A fully decoded adverse control
can permit diagnostic accounting while still blocking ownership approval. Unknown
controls continue to block both. None of these conclusions authorizes trading.

## Committed schemas and what the fixtures actually prove

The integrity-checked `desk/schemas/manifest.json` pins Pump documentation at
`cb188ce08b5069196eef1f3e4a0c43b70099793b` and two historical layouts.
All three committed Pump schemas declare legacy `create` with discriminator
`18 1e c8 28 05 1c 07 77` (hex), fourteen named accounts and exactly four Borsh
arguments: `name`, `symbol`, `uri`, `creator`.

Account order is: mint, mint_authority, bonding_curve,
associated_bonding_curve, global, mpl_token_metadata, metadata, user,
system_program, token_program, associated_token_program, rent,
event_authority, program. The mint and user are declared signers; token_program
is fixed to legacy `TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA`.

Required identities include:

- Pump program `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`;
  mint authority PDA seeds `[b'mint-authority']`;
  curve PDA `[b'bonding-curve', mint_bytes]`; global PDA `[b'global']`.
- Curve ATA seeds `[curve_bytes, legacy_token_program_bytes, mint_bytes]`
  under `ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL`.
- Metadata program `metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s`;
  metadata PDA `[b'metadata', metadata_program_bytes, mint_bytes]`;
  event authority `[b'__event_authority']` under Pump; fixed System and rent IDs.

The IDL proves declared interface/PDA bindings, **not** the complete creation CPI
order, issuance amount, deployed program behavior or historical control state.
`programs.instruction` recognizes identity without fully decoding creation args;
`launch_anchor` corroborates initialization/event/PDAs and outer scope. Those
existing results are useful inputs, not a lifecycle proof. A future positive
profile must consume the entire creation argument payload, bind the creator
argument to the complete event, reject extra accounts/bytes, and independently
verify every relevant child operation and its actual caller.

| Existing raw fixture | Exact observations | Limitation for this plan |
| --- | --- | --- |
| `fixtures/mainnet-launch.json` | Public notification at slot 454321337, signature `4PJardjqDqxr9Sek37eBGvcQahrkCT2GZ1p8aMxB5BLBHk4DRGkQNwobKxmbD7G4Vergu6eu9fSmbBZPoi1KK1NK`. Outer 2 is Pump `create_v2`. Child 2.2 initializes a Token-2022 mint; 2.12 mints 1000000000000000 raw units; 2.13 sets `mintTokens` authority to explicit null. ATA children 2.7 and 3.2 initialize immutable owner. | **Not a legacy launch.** Metadata pointer/metadata-authority operations and Token-2022 layouts remain unsupported. Decoder sees confirmed provenance and missing blockTime. Existing launch tests set finalized commitment and chain time in memory; those test substitutions do not make the original capture finalized. It cannot be a positive legacy readiness fixture. |
| `fixtures/mainnet-distribution.json` | Public finalized-history-page provenance; slot 454310062, blockTime 1791399537, signature `3J9zz8cvRobQtd8UQsii7a6h95SkyBSBhceT5ELSnwP6kJa3Hv7BLXj7oMGg2HSdNefRGykmauKhdhz4Lfg3f3Kv`. Legacy mint `7aN1pJGiMM93gjYgCqn9ReyexzzLLrFVotUcJG62JrbC` is created as 82 bytes at 0.0, initialized at 0.1, issued 1000000000000000 raw units at 0.12, revoked at 0.13. Legacy ATA sequences have getAccountDataSize / System creation / initializeImmutableOwner / initializeAccount3 at 1.0–1.3 and 2.0–2.3. | Outer creator is `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN`, **not Pump**. These are useful SPL-semantic witnesses, not Pump creation approval. WSOL wrapping/closure also needs separate lamport accounting. A committed provenance label alone is not a request-bound complete history proof. |

Neither fixture supplies complete historical raw account-control snapshots or a
positive finalized legacy Pump creation lifecycle. Do not relabel either to get
an approval. The PR24 multi-account fixture and PR27 mutations are synthetic
integration/attack evidence, not that missing public positive witness.

## Exact minimum semantic surface

These are **future proposed support conditions**, not exceptions to today's gates.
Every accepted instruction retains its original diagnostic record and receives
a separate versioned semantic verdict with reasons and source hashes.

| Operation | Narrow support conditions and transition |
| --- | --- |
| `initializeMint` / `initializeMint2` | Successful original Pump legacy create; exact 82-byte legacy mint allocation, fresh/uninitialized identity, mint signer and payer bound to creation. Decode decimals and authority options from authoritative instruction bytes. Initial mint authority must be the derived Pump PDA; freeze authority explicitly None. The supported profile must pin decimals and supply rules from independent provenance, not silently infer universal constants from one capture. Establish supply zero and initialized=true. Omitted JSON freezeAuthority is not, by itself, proof of an encoded None. |
| Initial `mintTo` (or independently pinned checked equivalent) | After mint initialization and curve token-account initialization, under the verified Pump creation caller. Destination is only the derived curve ATA, mint matches, authority is the mint PDA, amount is exact under the verified creation supply rule. Reconcile issuance, token balances and supply; retain any same-transaction post-creation buys separately. No arbitrary destination, additional issue, delegated issuer or inferred fill. One source-backed issuance profile is enough initially; unproved variants stay blocked. |
| `setAuthority(MintTokens, None)` | Exact target is the new mint; current authority is the proved Pump mint PDA; direct child of that original Pump create, after its authorized issuance. Require explicit None, single expected revocation, no authority redirection beforehand, and no later successful issuance/authority restoration. Canonical legacy bytes are `[6, 0, 0]`: instruction tag, authority role, one-byte instruction-option tag (not the four-byte account-state COption). Successful transition is PDA → None and is irreversible under the verified legacy semantics. Final mint bytes must corroborate None. |
| `getAccountDataSize` in an ATA setup | Read-only query on the bound mint under the bound ATA caller. Legacy semantics return 165-byte Account size, not extension permission. Support only an independently pinned request shape (empty or the standard immutable-owner request), exact relevant mint/program and no unknown extension list. Keep return-data provenance when used to justify allocation. Canonical token bytes `[21]` and any ATA-used suffix must be distinguished and explicitly pinned; legacy parsing ignores remaining bytes, so do not treat arbitrary suffixes as validated. |
| Legacy `initializeImmutableOwner` | Canonical `[22]`, bound fresh 165-byte uninitialized legacy ATA, immediately within the verified ATA creation sequence before account initialization. Under the checked legacy processor this **does not change state or make the owner immutable**; it rejects an already initialized account and returns success otherwise. Record `legacy_noop_on_uninitialized_account`, never `immutable_owner=true`. Token-2022's real extension operation is outside this profile. |
| `initializeAccount` / `initializeAccount2` / `initializeAccount3` | Proved fresh legacy account, expected mint and exact owner; curve ATA owner is the derived curve PDA, holder ATA owner is the bound wallet. Bind variant-specific accounts/encoded owner and rent requirements. Establish amount=0 for non-native mint, delegate=None, delegated_amount=0, close_authority=None, state=initialized, is_native=None. Do not apply these defaults to an existing account or WSOL. |
| ATA create / createIdempotent | Verify ATA derivation using **the actual token program**, mint and owner; payer/System/rent effects and nested operations must match the chosen semantic profile. For idempotent creation of an existing ATA, require independently known matching existing state; do not invent a new lifetime, zero balance or cleared controls. |

The related official SPL source reference is `solana-labs/solana-program-library`
commit `ad2b81274075c45e6ef428e52479b7d3d8f0dd6a` (resolved `token-v4.0.0`).
Its legacy processor makes revoked mint/freeze authority irreversible, and
initializeAccount establishes the defaults listed above. AccountOwner changes
also clear delegate/delegated_amount; for native accounts they clear explicit
close authority. CloseAccount None means fallback to the token owner, **not**
that the account is unclosable. Therefore even an owner change to the same
address is not a general harmless no-op.

The ATA processor at that same source revision invokes the shared Token-2022
instruction builders with the supplied token-program ID for immutable-owner and
initializeAccount3 setup. Builder namespace alone does not establish Token-2022
execution. Verify the instruction's resolved program and account owner instead.
These historical source files establish reviewed semantics, not a demonstrated
mapping to the executable deployed at each fixture's slot. That mapping and
exact ATA request-byte profile remain prerequisites before admission.

## Trust, execution order and state proofs

1. **Evidence origin and completeness.** Reconstruct from immutable,
   content-addressed successful finalized transaction responses and their saved
   request manifests. Bind signature, slot, address/filter/cursor/cutoff and hash;
   quarantine conflicting records. Retain missing pages, zero-balance accounts,
   closed accounts and decoding gaps. Normalized `safe`, completeness or passed
   flags, parser type names, events and logs alone cannot grant a semantic verdict.
   Preserve PR27's raw inventories and the original decisions.
2. **Actual execution trace.** Resolve raw message keys including validated lookup
   tables; decode instruction bytes/account indices and check parsed forms against
   them. Retain the outer index and numeric CPI sequence index. Execute conceptually
   as outer 0 then all its executed children, outer 1 then its children, etc.; the
   decoder's outer-list-then-inner-list traversal is not execution order. Do not
   lexically sort `2.10` before `2.2`. Reconstruct parent calls from valid stack
   heights without inventing skipped levels. A matching outer prefix is insufficient
   to prove direct Pump parentage when metadata/setup calls nest below it. Missing
   raw bytes or indispensable caller context remains unknown, not an exemption.
3. **Authority authentication.** Validate transaction signers/privileges and known
   program invocation semantics. Pump's off-curve PDA is not an outer transaction
   signer. Its CPI authority requires the verified Pump invocation and derivation,
   with independently validated program semantics for the relevant slot; do not
   invent an isSigner field or infer authority from a log string. Extra multisig
   signer accounts, unknown callers or contradictory parser fields stay unsupported.
4. **Ordered state machine.** Key states by address, mint, executing token program
   and lifetime generation, not by wallet address alone. For the mint, track initialized,
   decimals, supply, mint authority, freeze authority and account owner/layout.
   For token accounts, track owner, mint, amount, delegate/delegated_amount, close
   authority/effective closer, initialized/frozen/native flags and live/closed state.
   Apply every supported transition in numeric execution order. Initial defaults
   may come only from a proved birth plus verified processor semantics; existing
   balances cannot establish missing initial controls. Historical intermediate
   snapshots are not routinely returned by getTransaction: derive them only when
   the complete trace and known predecessor state justify the transition.
5. **Persistent adverse witnesses.** Record owner changes, approvals, authority
   transfers, explicit close-authority changes and freeze/thaw at their actual
   positions even when undone before transaction end or followed by burn/closure.
   Never compress these to a net state difference. Delegate→transfer→revoke,
   owner→restore, close-authority→clear and freeze→thaw must still block the initial
   safe profile. A zero amount, identical end owner, empty end delegate or matching
   end balance is not an exception. Mint-authority revocation is permitted only
   through its separate birth rule; it does not clear those adverse witnesses.
6. **Between transactions.** Order finalized slots, and within-slot transactions
   only with persisted finalized-block signature positions. Query order, signature
   lexical order and second-resolution blockTime cannot decide controls before a
   transfer. Keep incomplete same-slot order unknown. Deduplicate exact observations
   across histories; conflicting identities remain quarantined.
7. **Coverage and endpoint proof.** Collect every discovered mint/token-account
   lifetime through the immutable finalized bank cutoff, including control-only
   transactions with no token-balance delta and accounts that close or reach zero.
   Resolve control target → mint from a proved account identity, not from authority
   wallet or only balance metadata. Compare all reconstructed endpoint fields with
   saved single-bank mint/account bytes, exact supply and every missing/closed
   identity. Pin whether the cutoff includes the full snapshot slot and reflect
   that exact choice in query slot bounds; timestamps cannot substitute for it.
   Keep 18-RPC ceiling, existing block/account budgets and progress accounting;
   insufficient evidence under the budget stays blocked.
8. **Creation before distribution.** Prove initialization, issuance and revocation
   occur before any accepted external transfer/buy under the chosen profile, with
   exact destinations and amounts. Supply may change later through separately
   authorized burns; the creation issue amount is not necessarily the current
   supply. All buys/transfers before and after revocation remain in accounting.
   A historical creation proof is not a service/private-controller classification.

## Unsupported cases and scoped resolution

The initial profile leaves Token-2022/create_v2, metadata pointer or token-metadata
controls, delegate/approve/approveChecked/revoke cycles, multisig, mint authority
transfer to another key, freeze-authority activation or revocation from an active
value, freeze/thaw, token AccountOwner and CloseAccount authority changes, unknown
extensions/program versions, unknown layouts/bytes and missing caller/order/state
proofs blocked. A later harmless revoke with a *proved already absent* delegate
could be considered separately, but is not needed for the minimum creation profile.

Creation metadata changes are a different authority domain from SPL mint issuance.
Legacy Pump create declares the Metaplex program; pin and scope its metadata
operations before classifying them as irrelevant to token controls. Do not treat
Token-2022 updateTokenMetadataAuthority as a legacy mint revocation.

Witnessed zero-balance close/recreation can retain accounting support when owner,
authority, refund destination and lifetime ordering are proved. It is not an
exception for transient controls: keep earlier adverse records, start a new
explicit generation after actual recreation, and account for intermediate flows
and lamports. Native wrapping/closure remains a separate WSOL accounting profile.

A supported operation on another mint must not require rejecting every unrelated
history forever. Future resolution can scope it only after its exact target,
program, lifetime, CPI effects and relationship to relevant accounts are proved.
Unknown targets or shared-control effects still block. Never delete an unrelated
operation simply because it did not appear in token balance metadata.

## Future acceptance evidence, without changing PR27

- Add an unchanged, public, finalized **legacy Pump create** fixture with complete
  relevant CPI trace and raw bytes, verified identities/signers/PDAs, exact supply
  rule provenance, request binding and corroborating mint/account controls. Existing
  fixtures above supply negative/domain-distinction coverage, not this witness.
- Implement separate normalization and semantic verdicts retaining raw control
  operations. Consumers may resolve a blocker only through independent replay of
  the exact witness; removing limitation flags or injecting a resolution summary
  must never bypass PR27's direct inventory checks. Version the policy and preserve
  old decisions; do not retroactively upgrade originals.
- Test missing/explicit-null authority fields, unknown/trailing bytes, extra signers,
  role confusion, false PDA/caller, redirected issuance, repeated revocation, mint
  issuance after revocation and contradictory state. Distinguish impossible successful
  authority restoration after None from failed transactions with no committed effects.
- Test legacy immutable-owner no-op vs Token-2022 extension, wrong/preexisting ATA,
  idempotent creation without reset, owner/delegate/close/freeze changes restored
  within a transaction, same-slot order gaps, control-only account queries, closure/
  recreation, foreign-mint scoping and incomplete state/bank reconciliation.
- Independent architecture/security review precedes runtime admission. Continue
  OWN-1/OWN-2 and OWN-4 dependency work. Even a passing lifecycle component leaves
  coverage, funding/service classification, current-holder exposure and forward
  ownership verification separate; entry/readiness gates remain closed.

## Provenance and read-only checks

Manifest SHA-256 checks passed for all three creation schemas:

| Schema | Source revision | SHA-256 |
| --- | --- | --- |
| `pump.json` | `cb188ce08b5069196eef1f3e4a0c43b70099793b` | `ffe966c42f1af41652ee753fe2f1e3f7cd4077d7e6f49faf3138959c8b56064b` |
| `pump_previous1.json` | `3c6721a67c0b206b39130b454c8ba22a83ce972e` | `b90bc471327f671449271d5d1d42354d1fae6f5a06502f5834459a3108138e49` |
| `pump_previous2.json` | `91db6800e55bf341696564bd30a08ed4e3fc7491` | `9c74bb906dbef3890082009e1fab2b80e26099385c697e6a1a87af817efadf7e` |

Public fixture file digests:

- `mainnet-launch.json`: SHA-256 `d80cf9876fdb3e9b465e4103fa14bf2baaf09483dad9a27a57ff7339624b3bf2`;
  Git blob `3bb32a0e8f4050b4f5be301ca2f037d54b2132a7`.
- `mainnet-distribution.json`: SHA-256 `b5b6dfb1668a0307f2f3b95e6b1bb35361ebc122d97abe0b1f1cac6d26795803`;
  Git blob `aa7df1a3af049533d937d167bd18a31c005fe61f`.

Both fixture blobs and manifest blob `d9036d7188717ad921e34e53e0c35c83a6ee0806`
match the files read through the GitHub connector at the reviewed PR27 head.
Official semantic references read through that connector, without executing them:

- [Legacy processor](https://github.com/solana-labs/solana-program-library/blob/ad2b81274075c45e6ef428e52479b7d3d8f0dd6a/token/program/src/processor.rs),
  blob `7056f2e707ed93282d19ec56e9711d22a24b7498`.
- [Legacy instruction layouts](https://github.com/solana-labs/solana-program-library/blob/ad2b81274075c45e6ef428e52479b7d3d8f0dd6a/token/program/src/instruction.rs),
  blob `e798abdea4dc930354b970dd3dc36f098d4bb4d3`.
- [ATA creation processor](https://github.com/solana-labs/solana-program-library/blob/ad2b81274075c45e6ef428e52479b7d3d8f0dd6a/associated-token-account/program/src/processor.rs),
  blob `20767a247d15212c031d891d3e098e60e4c904b6`.

Read-only Python 3.12 inspection verified three schema hashes, fourteen-account
legacy profiles/discriminators and both public fixture identities; output:
`SCHEMA_AND_FIXTURE_ASSERTIONS_OK: 3 manifest digests / creation profiles and 2 raw public fixture identities verified`.
An exploratory attempt to decode every modern create_v2 argument with the generic
event reader encountered unsupported tuple-field handling; no reader or schema was
changed and no complete create_v2 argument proof is claimed. Corrected inspection
assertions passed without that out-of-scope parse. No new unit tests/full-suite run
is claimed for this documentation-only assignment; earlier SELL-1 test results do
not validate the proposed checker. Git whitespace check must pass before publication.

Only this report is published. No providers/RPC, VPS, secrets, signer, broadcast,
production edit, relaxed gate, merge, deployment or readiness change occurred.
