# Disconnected native-lamport reconciliation

Base: accepted integration `7568e4622d19b6eabe50b94b0b72b3ca53334017`, including PR54 auxiliary syntax. Scope: new `research/native_lamports.py`, dedicated tests and this report. No lifecycle-report, runtime, fixture, shared readiness/queue or budget changes; replay uses zero RPC calls.

## Prediction and observation

The pure API `report_native_lamports(record)` consumes the raw RPC transaction object, reconstructs resolved static/loaded keys and inventory internally, and models each supported raw System CreateAccount/Transfer path exactly once. Identical instructions at different paths remain separate movements. Legal repeated account references (self-transfer) accumulate both signed legs; duplicate transaction keys, lookup indices, inner group indices and inventory paths cannot silently double-count. Missing/invalid indexes and witnesses block comparison completeness.

Predicted deltas conditionally assume each modeled invocation committed. Observed deltas are supplied postBalances minus preBalances, not authenticated effects. The supplied meta.fee is conditionally debited from message key zero after strict writable-signer header binding; it is not independently established from bank state, signatures or compute settings. Fees are never defaulted to zero. Each account has an exact signed residual (observed minus predicted); aggregate residual and sum of absolute account residuals expose cancellation. Out-of-u64 predicted endpoints are gaps, not proof of intermediate execution order.

Inputs require exact integer u64 balances and fee: no boolean, float, string, negative, overflow or coercion. Signed deltas and sums use Python integers and exact decimal-string output, including aggregates larger than u64. The unchanged RPC fixture contains finite token UI display floats: only pre/postTokenBalances[*].uiTokenAmount.uiAmount may retain those opaque source witnesses; they never enter accounting or become raw amounts. Other floats/non-JSON input reject. Source bounds precede trace copying: 1 MiB JSON, 32 levels, 32,768 nodes and 20,000-character strings; inherited inspection bounds are 64 keys, 64 outer instructions and 256 total instructions.

All instructions remain inventoried. Unsupported System forms reject and remain unmodeled; other programs retain explicit unknown direct-lamport-effect coverage. Token closure refunds, owner-program writes, reallocation, rent and opaque program effects are not inferred from net balances. Every inner path retains individual-CPI uncertainty, including when transaction success or success logs are supplied; logs are not promoted to proof or used to suppress failed invocations. Failed/unknown transaction status cannot produce complete arithmetic agreement. Missing balances/fee produce no invented account comparison.

`predicted_net_agreement` means conditional arithmetic agreement only. `observed_instruction_model_complete` concerns this narrow observed instruction model, not completeness of native effects, fees, rent or historical coverage. Authentication, effect verification, CPI success, fee/rent verification, recipient authorization, lifecycle, ownership and entry flags are permanently false. A recipient earns no permission from a matching net balance.

## Original public sample: actual result

Original files are unchanged. Raw envelope canonical SHA256: `2b49cd4c6fe24a121cb59894c8a6fe0e82e4cf51db28e88151612984ccc9a0c7`; physical fixture SHA256: `27d481575656d2b254a63bf699a32ce3dd599ce3b55fc8b04da2aa79dd356520`. The report hashes the inner transaction result separately as `b5f76f79e44591fd60eae71d3a325aac78d52b3d4f2375c5676b9f6fd0d0690e`. These identify saved records, not trusted finality or independent authenticity. The report consumes no account-state endpoint bytes.

At saved slot454321337, all 33 inventory rows and all 34 balance keys remain. Eleven conditional movements total 509,164,320 lamports; the declared fee is 147,774. Pre-balance sum is 80,929,483,351,662; post-balance sum is 80,929,483,203,888. Observed and predicted total delta are both -147,774. Every per-account residual and the aggregate residual are zero; absolute account residual sum is zero. There are no malformed-input errors or arithmetic residual gaps. This correct arithmetic result does not eliminate the evidence gaps below.

| Path | Raw kind | Resolved account indices | Lamports |
| --- | --- | --- | ---: |
| 2.0 | CreateAccount | 0 → 1 | 1,838,960 |
| 2.3 | CreateAccount | 0 → 2 | 1,366,520 |
| 2.6 | CreateAccount | 0 → 3 | 1,513,840 |
| 2.9 | Transfer | 0 → 1 | 934,720 |
| 3.1 | CreateAccount | 0 → 6 | 1,513,840 |
| 4.0 | CreateAccount | 0 → 12 | 1,346,200 |
| 4.3 | Transfer | 0 → 10 | 650,240 |
| 4.4 | Transfer | 0 → 10 | 1,481,482 |
| 4.5 | Transfer | 0 → 2 | 493,827,159 |
| 4.6 | Transfer | 0 → 20 | 2,345,680 |
| 4.7 | Transfer | 0 → 21 | 2,345,679 |

Twenty unmodeled program-effect rows: `2, 2.1, 2.2, 2.4, 2.5, 2.7, 2.8, 2.10, 2.11, 2.12, 2.13, 2.14, 3, 3.0, 3.2, 3.3, 4, 4.1, 4.2, 4.8`. This conservative module makes no lamport-effect exemption based on their token/event/fee labels. All 28 CPI outcomes remain individually unverified. Observed instruction model completeness stays false. Fee provenance/derivation, rent exemption/effects, deployed program semantics, effect order, intermediate mutations, capture authenticity and recipient permission remain unresolved. Net agreement can conceal transient offsetting effects or caught failures compensated elsewhere.

The later confirmed mint state at slot454452430 is not used, and no launch state is fabricated. Existing lifecycle and runtime rejection remain unchanged. Independent authentication, exact deployed program-slot semantics and individual effect/outcome evidence are prerequisites for any future verification; this module supplies arithmetic diagnostics only.

## Primary provenance and validation

Reuse accepted auxiliary System/ComputeBudget pins and inherited trace format/key-order pins unchanged. Fee-payer position/header semantics were checked against official [Solana legacy message source](https://github.com/solana-labs/solana/blob/d9f20e951a06b61e4505da0955228020b96a8915/sdk/program/src/message/legacy.rs), Git blob `1a6a9239f4e0aaff47f02356ead414f5abc05414`. System layout primary source remains [SystemInstruction](https://github.com/solana-labs/solana/blob/d9f20e951a06b61e4505da0955228020b96a8915/sdk/program/src/system_instruction.rs), blob `bb66b4fbce6b67cc40849978b96027357637b609`; RPC fee/pre/post fields inherit [transaction-status format](https://github.com/solana-labs/solana/blob/d9f20e951a06b61e4505da0955228020b96a8915/transaction-status/src/lib.rs), blob `0eb13d36819c4a1d8cf6dfa11918fc6e70b42f84`. Format pins do not authenticate execution or historical deployment.

Synthetic records are source-built bytes and explicitly supplied invented balances; they are not captures. Positive cases show exact conditional arithmetic, not safety. Negative cases cover canceled account residuals, aggregate discrepancies, caught CPI/failed transactions, token-close/direct-write refunds, unsupported layouts, duplicate paths/groups/keys/lookup indices, malformed indexes/header/balance/fee, loaded key order, missing witnesses, u64 limits, source bounds/cycles, raw/parsed ambiguity and opaque UI floats.

Python 3.12.14 Linux: dedicated **33 tests in 0.206s, OK**; full `python -m unittest discover -q`: **1,322 tests in 54.139s, OK, zero skips**. No known failures. Public replay is byte-for-byte unchanged; no provider, VPS, signer or broadcast access.
