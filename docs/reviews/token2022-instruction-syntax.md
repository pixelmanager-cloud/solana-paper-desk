# Disconnected Token-2022 instruction syntax

Base: integration/cloud-wave1 d9c093b0f0be103ebdb11a826c1896be6b8f055b.
Scope: research/token2022_instructions.py, dedicated tests and this report only.
The normalizer has only standard-library imports and is excluded from the
installed desk runtime package. No production, shared queue/readiness, capture,
ledger, state-decoder or gate changes. No worker07 API dependency.

Read the [PR44 compatibility matrix](https://github.com/pixelmanager-cloud/solana-paper-desk/blob/c121119589880ae143fbb2f34d65eec93749a5c2/docs/reviews/create-v2-compatibility.md)
through GitHub at its exact reviewed head. This implements its raw syntax
prerequisite, not its proposed Token-2022 lifecycle/state acceptance.

## API and narrow profile

`normalize_token2022_instruction(program, raw=bytes, accounts=ordered_keys,
parsed=optional_witness, evidence=optional_untrusted_dict)` accepts only the exact
Token-2022 executing program ID. Accounts must already be resolved public keys,
optionally account-meta witnesses; numeric indices and missing keys are rejected.
No index resolver, provider, caller reconstruction or state decoder is invoked.

| Operation | Canonical bytes | Exact ordered account roles |
| --- | --- | --- |
| MetadataPointer initialize | 39,0,authority32,address32 (66 bytes) | mint |
| InitializeMint2 | 20,decimals,mint-authority32,one-byte option (+key when Some) | mint |
| ATA GetAccountDataSize | 21,7,0 only | mint |
| InitializeImmutableOwner | 22 only | account |
| InitializeAccount3 | 18,owner32 (33 bytes) | account,mint |
| Metadata initialize | d2e11ea258b84d8d + three exact Borsh strings | metadata,update authority,mint,mint authority |
| Metadata update authority | d7e4a6e45464567b + optional-key32 (40 bytes) | metadata,current metadata authority |
| MintTo | 7,uint64LE amount (9 bytes) | mint,account,mint authority |
| SetAuthority MintTokens | 6,0,one-byte option (+key when Some) | mint,current mint authority |
| TransferChecked | 12,uint64LE amount,decimals (10 bytes) | source,mint,destination,transfer authority |

Metadata initialization requires metadata=mint, as the Token-2022 processor
requires. Distinct update and mint authority keys are preserved as distinct roles;
shared observed A values never collapse their authority domains. Pointer authority,
metadata update authority and MintTokens authority remain separate fields.

Ordinary SPL options have a one-byte 0/1 tag, including explicit Some(System zero
pubkey). Extension/metadata optional keys use exactly 32 bytes: zero is explicitly
None, nonzero explicitly Key. Missing data never becomes None. Parsed omission
versus explicit null is preserved by parsed_supplied/parsed_field_presence and the
unchanged witness. Parsed fields are not interpreted or compared with raw syntax;
representations_match remains unknown, even when both witnesses are supplied.
Neither parsed fields nor balances reconstruct missing byte/account evidence.

Unknown operations, non-MintTokens roles, other size-extension requests, extra or
multisig accounts, suffixes, truncation, malformed options/keys/meta flags and wrong
program IDs remain UNRESOLVED with no raw operation. Repeated instruction account
roles are permitted where needed (metadata=mint); transaction-level key uniqueness
belongs to the upstream capture resolver, not an instruction-account list.
Metadata strings use exact UTF-8 Borsh byte lengths, at most 4096 bytes each and
16384 total raw bytes. These are inspection bounds, not asserted on-chain limits.
URI text is untrusted and never fetched. Amounts remain exact uint64 decimal
strings; decimals remain byte values. No returned-data account size is invented.

## Evidence and approval boundaries

SYNTAX_ONLY/raw_layout_complete describe supplied byte structure only. Every
authority/evidence/caller/privilege/CPI-success/effect-order/deployed-code/account-
state/lifecycle/ownership/Token-2022/trading approval flag remains false, including
when untrusted metas/evidence claim signers or success. Caller hints are copied
verbatim but never promoted into direct Pump/ATA caller proof or parent/order.
Raw bytes have exact SHA256/hex/length; ordered account, parsed and context witnesses
are copied without mutating input. JSON evidence hash is a binding hash, not trust.

Token-2022 ImmutableOwner reference semantics initialize an extension; legacy
Tokenkeg no-op semantics are not reused and Tokenkeg program binding is rejected.
The normalizer cannot say the extension exists, the CPI succeeded, AccountOwner is
immutable now, or delegate/freeze/close controls are safe. Raw syntax must later be
bound to independently proved code, state, caller privileges and execution.

The saved public mainnet-launch fixture is unchanged (SHA256
 d80cf9876fdb3e9b465e4103fa14bf2baaf09483dad9a27a57ff7339624b3bf2).
All 13 Token-2022 CPIs are parsed-only and remain RAW_INSTRUCTION_UNAVAILABLE with
no normalized raw operation/accounts. Pointer authority and freeze authority are
omitted, not explicit None. Metadata newAuthority=null is retained as a parsed
witness, not recovered zero bytes. Provenance, path and stack height are retained
untrusted; finality, request binding, raw endpoint state and lifecycle gaps persist.
No successful Token-2022 profile or original-byte reconstruction is claimed.

## Official pins verified during implementation

The following complete public files were fetched through the GitHub connector;
their git blob hashes matched the PR44 pins. These are reference formats, not the
program deployed at the fixture slot or its historical metadata dependency.

| Repository / commit | File | Git blob |
| --- | --- | --- |
| solana-program/token-2022 / d9ffb9787187b6bc29adda1a6b389b9931377e03 | interface/src/instruction.rs | 0c80e6d86274846945a881584bcdb02bcc6a0145 |
| same | interface/src/extension/metadata_pointer/instruction.rs | 1f3f542ff499c0757e950e80543df4734ed56d16 |
| same | program/src/processor.rs | f5816ef243c6ea3aa283ef471988f08e2251d361 |
| same | program/src/extension/token_metadata/processor.rs | f488deb80fbcda1481dc15e6dd98cb9873a262a2 |
| solana-program/token-metadata / fb7755d1520af9fb2cda2fbfbcceb0248080121a | interface/src/instruction.rs | 39903167fda0f3ba680bf7c4615b7fd7edf2033e |

Tests independently check metadata discriminator hashes, all supported ordered
fields, every truncated prefix and suffix, None/Some/missing distinctions,
uint64/decimal boundaries, strings/bounds/UTF-8, unsupported roles/extensions,
extra accounts, incorrect executing programs, untrusted Pump/ATA caller hints,
explicit metas, provenance/hash preservation and every saved parsed-only CPI.
All fixtures are synthetic or the existing public reference. No provider, VPS,
secrets, signer or broadcasting. Complete raw capture, independent state decoder,
reviewed lifecycle/caller proofs and coordinator live acceptance remain outstanding.

Final Linux Python 3.12.14 validation:
- `python -m unittest tests.test_token2022_instructions -q`: 21 tests in 0.019s, OK.
- `python -m unittest discover -s tests -q`: 1059 tests in 37.064s, OK.
- `git diff --check`: passed. No known test failures. Existing private-loopback
  test fixtures were permitted; no live provider I/O was performed.
