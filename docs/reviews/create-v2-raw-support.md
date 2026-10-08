# Offline create_v2 raw launch support

Base: integration/cloud-wave1 `d41fe2933236f972e367dfab8b6101e142511901`. This disconnected research change diagnoses the saved launch. All lifecycle/authentication/ownership/trading approvals remain false. Production consumers, gates, public fixtures and shared readiness/queue files are unchanged. Replay: zero RPC calls.

## Evidence and failure cause

The unchanged `fixtures/mainnet-launch-raw.json` has canonical full-envelope SHA256 `2b49cd4c6fe24a121cb59894c8a6fe0e82e4cf51db28e88151612984ccc9a0c7` and physical-file SHA256 `27d481575656d2b254a63bf699a32ce3dd599ce3b55fc8b04da2aa79dd356520`. Its receipt requests finalized/json at slot 454321337. Saved provenance is not authenticated runtime evidence; independent provenance review belongs to worker06.

Raw trace: 33 instructions (5 outer, 28 inner), 34 resolved static/loaded keys. Raw and parsed records agree on observed programs, direct parents and shared Pump bytes/accounts. The parsed record lacks raw witnesses for 13 Token-2022 rows; the raw record supplies instruction witnesses, not endpoint state.

Controls 2.5/3.0 (GetAccountDataSize), 2.7/3.2 (InitializeImmutableOwner), 2.11 (metadata update-authority removal), and 2.13 (mint-authority revocation) already normalize correctly. Their apparent rejection followed an allocation abort: both System CreateAccount 2.3 and later System Transfer 4.5 use payer/curve accounts and were counted as allocations. The predicate now requires exact CreateAccount syntax. Curve allocation declares 141 bytes/Pump owner; the transfer stays separately unresolved. Duplicate actual allocations reject.

## Exact bounded syntax and primary sources

New auxiliary normalization supports only System CreateAccount (u32 tag0, u64 lamports, u64 space, owner32; 52 bytes/two accounts), Transfer (u32 tag2, u64 lamports; 12 bytes/two accounts), ComputeBudget limit (tag2/u32; 5 bytes/no accounts), price (tag3/u64; 9 bytes/no accounts), and Pump Fees get_fees_with_quote_mint (discriminator8/bool/u128/pubkey; 57 bytes/two accounts). Truncation, suffixes, malformed keys, unsupported tags and noncanonical arguments reject. Syntax grants no authority, rent, cost or CPI-success verdict.

Pinned official layouts:

- [SystemInstruction](https://github.com/solana-labs/solana/blob/d9f20e951a06b61e4505da0955228020b96a8915/sdk/program/src/system_instruction.rs), blob `bb66b4fbce6b67cc40849978b96027357637b609`.
- [ComputeBudgetInstruction](https://github.com/solana-labs/solana/blob/d9f20e951a06b61e4505da0955228020b96a8915/sdk/src/compute_budget.rs), blob `c903be13c214464cfb8ce0776cdc4db4b2a65d49`.
- [Pump Fees IDL](https://github.com/pump-fun/pump-public-docs/blob/cb188ce08b5069196eef1f3e4a0c43b70099793b/idl/pump_fees.json), blob `e740baaa16f1e874403ea8d4a7e81179f7eea3cf`; checksum-verified local schema SHA256 `d87b52305fd6b2ec487d4ba1e08a49990c23fa9b8b76092b2097df0164fa3859`.

The fee query is get_fees_with_quote_mint, not the AMM permission profile. Pump fee-config PDA, config-program argument, bool and SOL sentinel bind explicitly; market-cap inputs, return and schedule remain unverified. Existing pinned Token-2022 syntax and Pump event decoders are unchanged. Official layouts do not authenticate historical deployed program-slot semantics.

## Complete inventory, unresolved effects

All 33 raw rows remain; none is ignored. All 13 Token-2022 rows have complete raw syntax. Real-fixture structural errors are empty, but eight rows remain incomplete: 4.0 volume allocation, 4.1 fee query, 4.3–4.7 native SOL transfers, and 4.8 TradeEvent prefix with eight opaque suffix bytes. Exact witnesses and unresolved effects are retained. Assigned labels alone cannot satisfy inventory agreement.

Volume allocation binds PDA, Pump owner and declared 137-byte size without proving state/rent. Native transfers bind payer and declared buy recipients without authorizing recipients or reconciling costs. Unsupported programs, wrong recipients/parents, adverse controls and malformed forms still reject. TradeEvent retains the existing prefix and exact opaque suffix; schema completeness stays false. Optional create suffix and opaque mayhem semantics are not invented.

Observed inventory agreement and supported sequence agreement remain false. Endpoint bytes are absent (`RAW_STATE_MISSING`); synthetic matching endpoints cannot resolve buy effects. The later confirmed AoPf state at slot454452430 is not consumed or substituted for launch slot454321337. Launch supply remains 10^15 with six decimals; later changes require historical evidence.

Graduation requires independently bound launch-slot mint/curve/holder states and prestate, trusted finalized capture/identity, deployed program-slot semantics, individual CPI outcomes and verified effect order, complete event/optional-argument and mayhem coverage, volume/global/fee state, authorized native recipients and complete native cost accounting. Invocation preorder cannot substitute for effect-order proof.

## Validation

Python 3.12.14, Linux, fixture-only: dedicated lifecycle/raw-support suite **63 tests in 1.055s, OK**; full unittest discovery **1284 tests in 52.944s, OK**, zero skips. Adversarial cases cover truncation/suffixes, invalid tags/keys, allocation/compute/event duplicates, direct-parent confusion, fee swaps, redirected SOL, volume bindings, transient controls, unknown programs, opaque event bytes and endpoint-slot substitution. Synthetic mutations/endpoints are explicit and do not replace historical evidence.
