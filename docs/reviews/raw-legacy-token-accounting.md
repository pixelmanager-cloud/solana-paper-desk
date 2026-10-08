# Raw legacy SPL accounting syntax prerequisite

Base: integration/cloud-wave1 at 51b754d. Production code is isolated in
legacy_token_accounting.py and decoder integration; no research imports, queue,
readiness, entry, capture or ledger edits.

## Source and exact supported profiles

Official solana-labs/solana-program-library commit
ad2b81274075c45e6ef428e52479b7d3d8f0dd6a, token/program/src/instruction.rs,
git blob e798abdea4dc930354b970dd3dc36f098d4bb4d3. The complete public source
was fetched from GitHub and its git blob hash independently matched during this
implementation. Processor semantics share the existing reviewed source pin
7056f2e707ed93282d19ec56e9711d22a24b7498. These reference pins do not attest
to the program deployed at the captured slot.

Canonical instruction profiles:

| Tags | Accounting | Bytes | Accounts |
| --- | --- | --- | --- |
| 0 / 20 | InitializeMint / InitializeMint2 | 35 None, 67 Some | mint,rent / mint |
| 1 | InitializeAccount | 1 | account,mint,owner,rent |
| 16 / 18 | InitializeAccount2 / InitializeAccount3 | 33 | account,mint,rent / account,mint |
| 3 / 12 | Transfer / TransferChecked | 9 / 10 | source,destination,authority / source,mint,destination,authority |
| 7 / 14 | MintTo / MintToChecked | 9 / 10 | mint,account,authority |
| 8 / 15 | Burn / BurnChecked | 9 / 10 | account,mint,authority |
| 9 | CloseAccount | 1 | account,destination,authority |

Mint authority options use ONE byte, not account-state COption encoding.
Amounts are exact little-endian uint64 strings, checked decimals are uint8.
Declared account and mint roles resolve only from raw bytes/account indices;
available token balance identity/program/checked-decimal contradictions reject.
Unchecked transfer mint remains None: indexed metadata never fabricates its raw
instruction mint. Rent sysvar positions are exact. Truncation, suffix bytes,
extra/multisig accounts and malformed addresses do not yield accounting records.
CloseAccount's third account is declared authority, NOT established owner:
owner remains None. No signer/CPI authorization or execution result is inferred.

## Conservative consumer contract

Normalized rows have raw_accounting_only=true and raw_status=ACCOUNTING_SYNTAX.
The decoder retains UNDECODED_TOKEN_INSTRUCTION and adds
RAW_TOKEN_EXECUTION_UNVERIFIED for all supported raw syntax. Transaction success
alone does not prove each CPI succeeded or establish birth/lifetime semantics.
Retaining the existing blocker ensures current inventory, movement and continuity
consumers cannot promote these fields alone into verified lifetimes. This is an
intentional limitation, not a claim of complete raw instruction decoding.

Raw controls (including SetAuthority, approve/revoke/freeze, multisig and ATA
control/size operations), unknown tags, malformed supported operations and all
Token2022 remain explicit unresolved token_control_operations with raw tag,
syntax status and instruction path. Existing PR27 blockers are preserved; even
canonical authority-control bytes never clear them. Parsed behavior is unchanged.

The unchanged public legacy Pump reference now establishes accounting syntax for
mint initialization, two account initializations, the exact mint amount and a
transfer. Controls remain unresolved, historical event schemas mismatch, original
request binding is absent, and finality remains unverified. Launch ancestry,
ownership, lifecycle and entry readiness remain blocked. Source payload hashes,
original fixture bytes and request/evidence persistence are unchanged.

## Validation and outstanding dependencies

Fixture-only Linux Python 3.12.14. Dedicated tests cover all supported profiles,
option formats, full truncation/suffix rejection, exact account counts, rent,
uint64 boundaries, checked decimal/identity/program contradictions, malformed and
unknown instructions, Token2022, failed transactions and transient init/close
without lifecycle approval. Existing compiled/parsed and PR27 suites are included.
No provider, VPS, signer or broadcast was accessed.

Remaining dependencies: per-invocation execution evidence, raw authority/lifecycle
validation, exact historical event support, request-bound finalized complete
history and ownership acceptance. No live acceptance, merge or deployment.
