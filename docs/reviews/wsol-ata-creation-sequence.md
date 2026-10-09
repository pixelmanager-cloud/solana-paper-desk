# Narrow WSOL ATA creation sequence

Base `9e43752daabf19b7e78e08e0b15d16d1c6e2a680`. This closes the reproduced count-only gap directly in the existing `desk.setup_policy.check_sell_setup` consumer; no unused helper, route admission component or gate changes are introduced.

The old checker accepted initialization before creation and a reduced create/initialize pair. It also accepted an existing ATA with no observed children. The checker now requires one exact idempotent outer ATA instruction, the wallet's canonical legacy Token/WSOL ATA derivation and six ordered accounts, plus exactly four direct children by numeric raw inventory path:

1. Legacy Token GetAccountDataSize, target mint WSOL, bytes `15` or the explicit ImmutableOwner request `15 07 00`.
2. System CreateAccount, exactly52 bytes and `[wallet, ATA]`, positive u64 funding, space165, legacy Token owner.
3. Legacy Token InitializeImmutableOwner, exactly `16`, `[ATA]`.
4. Legacy Token InitializeAccount3, exactly `12 || wallet32`, `[ATA, WSOL]`.

Require child paths `<outer>.0` through `<outer>.3`, unique across the supplied inventory; exact direct parent path/program and integer stackHeight2; no deeper/extra children. Sort by numeric indices rather than trusting input array order. Missing, duplicate, gapped, reordered, substituted or extra instructions reject. The System Transfer/Allocate/Assign funded-account branch rejects; the empty existing-ATA/no-op branch explicitly rejects as unsupported. No historical zero-prebalance, account ownership or successful creation is inferred from this pattern.

The existing caller wiring in sell simulation, historical sell replay and diagnostic coverage already invokes this function, so reordered initialization now fails that existing diagnostic. Its successful receipts remain detached exact row identities. Existing outer CloseAccount binding checks remain; transfer recipients, rent exactness, close placement, wallet effects, underlying route policy and runtime privileges remain separate checks. Without an ATA creation call, this narrow setup result rejects instead of approving the no-creation profile. This tightens supported scope; it cannot upgrade route approval.

## Completeness and scope

Require explicit bounded inventory/row error witnesses, consistent inventory summary, verified stack metadata, canonical unique paths, bounded rows/accounts/bytes and exact typed depths. Enumeration-loss errors such as unsupported parsed instructions, omitted encoding or missing inner metadata reject globally, before subtree selection. No errors are cleared in the supplied inventory.

Identified `UNSUPPORTED_ROUTE_PROGRAM` rows outside the ATA subtree remain reported in `unresolved_inventory_reasons`, as in the existing partial setup scope; they do not gain receipts or full-route support. Any such flag inside the selected ATA subtree rejects. All other inventory/row errors reject conservatively. The public capture's unrelated unsupported swap programs therefore remain unsupported while its independently observable ATA sequence can match. Setup agreement does not imply that the full supplied inventory is an approved route.

Bounds:256 rows,64 accounts/row,16-character canonical paths,1644-character base64 instructions,256 bounded error strings. No provider calls, endpoint fabrication, generic parsed-operation support or allocation-branch relaxation.

## Primary pinned semantics

Official SPL repository commit [ad2b81274075c45e6ef428e52479b7d3d8f0dd6a](https://github.com/solana-labs/solana-program-library/tree/ad2b81274075c45e6ef428e52479b7d3d8f0dd6a):

| Source | Git blob | Establishes |
|---|---|---|
| [ATA processor.rs](https://github.com/solana-labs/solana-program-library/blob/ad2b81274075c45e6ef428e52479b7d3d8f0dd6a/associated-token-account/program/src/processor.rs) | `20767a247d15212c031d891d3e098e60e4c904b6` | Derived ATA/accounts; size query, creation, immutable-owner call, initialize-account3 order; existing-account early return |
| [ATA tools/account.rs](https://github.com/solana-labs/solana-program-library/blob/ad2b81274075c45e6ef428e52479b7d3d8f0dd6a/associated-token-account/program/src/tools/account.rs) | `14158f70bf4fe758873ea7f095dd772955bbca89` | Zero-funded CreateAccount versus Transfer/Allocate/Assign branch; return-data-dependent account size |
| [Legacy Token processor.rs](https://github.com/solana-labs/solana-program-library/blob/ad2b81274075c45e6ef428e52479b7d3d8f0dd6a/token/program/src/processor.rs) | `7056f2e707ed93282d19ec56e9711d22a24b7498` | GetAccountDataSize returns Account::LEN; InitializeImmutableOwner checks uninitialized state but only emits compatibility warning for legacy Token |

In particular, observing legacy InitializeImmutableOwner **does not establish immutable ownership**. The `15` form comes from unchanged public raw instruction bytes; `15 07 00` is the narrowly enumerated ImmutableOwner extension request already recognized by the existing legacy inventory. Other suffixes reject. These reference sources do not establish the binary deployed at the sample slot. Invocation order remains observed metadata, not verified effect order, individual CPI success or PDA signer authorization. Source/runtime-success/deployed-program/full-route/entry flags remain false.

## Evidence and tests

Unchanged `fixtures/mainnet-sell-simulation.json`, physical SHA256 `1e833b316620278fada145d60f6ddb7df2d1a48a9cab1bdc9a6e113e79aa2b2d`, matches through the actual production inventory builder: outer1, direct children1.0–1.3. Its unsupported route programs and missing compiled privilege witness remain unresolved. Changing creation/initialization path order rejects. This is positive observed-sequence coverage, not positive live sellability.

Synthetic tests use deterministic public addresses only, no keypairs/signers. They cover the original reduced/reordered reproducer, complete reordered semantics, shuffled input array preserving numeric paths, missing each child, duplicate/extra/gapped paths, wrong bytes/accounts/owner/caller/depth, unsupported no-op/funded allocation, enumeration-loss errors and outer close diversion. Existing consumer-focused simulation/coverage tests verify compatibility. The receipt regression now uses free path1.4 for its extra-row assertion; path1.2 is a real required child, so retaining that old extra path would test a duplicate instead of a distinct uncovered row. Actual public-pipeline parsed approval/unknown-operation additions also reject when normalization loses account identity. No shared queue/readiness, ownership, CLI, providers/VPS/secrets, signing, merge or deployment changes.

Python3.12.14/Linux fixture-only validation:10 dedicated setup tests in0.015s;36 focused setup/simulation/coverage/receipt tests in0.079s; final full suite1951 tests in213.135s. All OK,zero failures/errors/skips. `git diff --check` passes. An initial full run found the old receipt test’s colliding extra path; adapting it to distinct path1.4 preserved its negative coverage assertion and resolved that failure. Setup source SHA256 `149e9fc9b8970e997ec1ee41a3decbf8b3190dde3594d598d1da3a9ca25843d5`. No outstanding test failure or additional RPC request.
