# Solana scam and rug-pull defense policy

Version: 2026-10-08. Scope: speculative Solana tokens, initially graduated PumpSwap pools. All thresholds below are research hypotheses, not statistically validated safety boundaries. Unknown evidence causes a skip. A pass means the available evidence did not trigger this policy; it does not certify safety.

## What can go wrong, and the planned response

| Threat | Evidence required | Policy / response | Current implementation |
|---|---|---|---|
| Coordinated early purchases and hidden ownership | Original mint/bonding-curve history, funding edges, current owner balances, material transfers | Skip concentrated linked supply; high score cannot override | Normalized-evidence policy and tests; raw graph builder pending |
| Supply split or moved after launch | Trace material token transfers from early buyers to present owners; avoid double counting | Carry cluster exposure forward; do not assume selling means supply is dispersed | Three-hop classified transfer links implemented; live classification and inventory tracing pending |
| Fake links through exchanges or dust | Verified service labels, transfer amounts and timestamps | Do not cluster solely through a common exchange/service; ignore dust-only links | Hub exclusions and minimum material-transfer threshold implemented |
| Mint dilution | Mint program owner and binary mint authority | Reject active mint authority | Binary legacy SPL inspector implemented |
| Frozen or selectively restricted transfers | Freeze authority, token-account state, transfer extension authorities | Reject freeze authority, frozen accounts; initially reject Token-2022 | Inspectors and policy implemented |
| Tokens transferred/burned after purchase by issuer | Permanent delegate or previously approved account delegate | Reject permanent-delegate capabilities and account delegations | Token-2022 blanket rejection and legacy account-delegate check implemented |
| Hooks, taxes, pauses, non-transferability | Program and extension semantics, authority changes, exact transfer behavior | Reject unsupported/custom programs and all Token-2022 initially | Program allowlist implemented; extension-specific support intentionally absent |
| LP removal or thin exit liquidity | Canonical pool provenance, reserve ownership, lock/burn proof, withdrawal authority and maturity, size-specific sell depth | Skip unverified locks or inadequate depth; monitor actual reserve changes | Required safety fields and reserve model; chain-backed LP proof pending |
| Insider dumping despite locked LP | Dev/cluster inventory, sales, holder concentration, net flow | Skip excessive concentration; emergency exit when usable route exists | Bundle thresholds and danger-exit input; live detection pending |
| Honeypot-like sell failure | Real wallet's exact sell transaction, route, account states and simulation | Reject unknown/failed/size-mismatched simulation; periodically re-evaluate | Evidence gate implemented; simulation producer pending |
| Malicious route or wallet drain | Decode all top-level and inner effects, programs, signers, destination accounts, balance deltas | Reject unrelated transfers, approvals/authority changes, unexpected account closure, excess fees | Design only; no signer exists in this build |
| Fake popularity and wash trading | Independent buyers, repeated pairs, shared funding, organic net flow | Discount score; reject excessive wash fraction; do not let social override safety | Momentum formula and wash gate implemented; real feature extraction pending |
| Lookalike token or poisoned metadata | Mint addresses, canonical pool derivation and creation transaction | Identify by mint/program, never ticker; treat names, URLs and descriptions as untrusted | Mint binding and supported-venue gate; canonical pool decoder pending |
| MEV and adverse execution | Transaction simulation, minimum output, observed landing cost, route policy | Bound slippage and tips; private submission where appropriate; no guaranteed protection | Cost model only; live executor pending |
| Provider failure, stale data or lying source | Source/slot timestamps, independent provider comparisons, finalized reconciliation | Pause entries; preserve unresolved positions; no manufactured fills | Input freshness, ledger, raw gap markers; live recovery pending |

## Bundle screening specification

Collect launch history from token creation and the original bonding curve, not only the first slots after PumpSwap migration. Keep initial purchase cohorts distinct from proven common ownership. A Jito bundle ID is not assumed to be present in every transaction stream or observable for every other trader's transaction.

The full planned evidence pipeline records mint, slot, transaction and instruction identity, observation time, owner wallet, quantities, funding source, service labels and confidence. Initial funding tracing is bounded; later expand to 2-3 hops with explicit coverage and cost limits. Cross-token recurrence and deployer identity must be based on creation/funding history, not ticker or a mutable fee-recipient label.

The implemented normalized-evidence screen requires launch and funding completeness attestations, a holder snapshot no older than 120 seconds, and at least 95% holder-supply coverage. Percentages use one consistent denominator: circulating holder supply after verified pool/burn exclusions. Those exclusions and the denominator must be established by the future on-chain evidence builder. Current JSON fixtures attest them only synthetically.

Initial skip rules: a linked cluster holds at least 10%; an early-linked cluster holds at least 8%; the first four-slot purchase cohort still holds at least 15%; a known-bad wallet currently holds supply; or observed unknown funding reaches at least 5%. Common private funding counts only when labeled `private_verified` and occurring within one hour before the recipient's recorded buy. Exchanges, bridges, services and unknown-source classifications do not establish a funding cluster. Classified private-owner transfers are traced up to three hops from early buyers. Split transfers aggregate before the 0.5% floor. Known service/pool edges are excluded, unknown material paths skip, and earlier-than-acquisition paths are ignored. This is a bounded heuristic, not full token inventory tracing. These choices intentionally favor avoiding exposure and will produce false positives and false negatives.

Additional production gates must assess unresolved funding, unknown service labels, repeated deployer groups and hidden supply after multi-hop transfers. A supplied `funding_history_complete=true` is not a substitute for those checks. The current detector is not advertised as comprehensive bundle detection.

## Sellability is time-, wallet-, route- and size-specific

1. Identify the mint and its owner program; inspect authorities before pricing it.
2. Verify pool provenance, liquidity withdrawal controls and exit depth.
3. Size the proposed entry, obtain a buy route, estimate actual tokens received, then obtain sell routes for the planned liquidation quantity and ladder sizes.
4. Validate the actual transaction's instructions and wallet effects. Bind every result to mint, wallet, quantity, slot, timestamp, route, configuration and transaction hash. Resolve lookup tables before validation. Program-name labels are insufficient.
5. Simulate the correct wallet state and verify positive net proceeds and expected token debit. A sell simulation against a wallet that does not yet own tokens is not evidence of sellability. Use supported sequential state simulation/forking for a hypothetical buy-then-sell. This requires a provider/local environment that supports state continuity; ordinary independent `simulateTransaction` calls do not preserve hypothetical buy state.
6. Before a future real pilot, a tightly capped buy-and-sell canary may test actual execution, but it can itself lose all its value and does not prove a larger/later sale will work. It is not executed by this prototype.
7. After a real buy, reconcile raw token balances, mint/owner/delegate state, fill cost and fee/rent deltas. Simulate the remaining exit from the actual holding account. Continue monitoring; a previous pass never becomes permanent trust.

An emergency exit uses a finite minimum-proceeds policy and capped escalation. When liquidity disappears or transfers are forbidden, no software can force a successful sale. Record STUCK_POSITION; do not report a fake close. Token supply/balance changes are measured in raw units as well as UI amounts to distinguish real removals from display changes.

## The hook / clawback distinction

Solana transfer hooks execute custom logic during transfers and can restrict them. The original transfer accounts are read-only inside the hook and sender signing privileges are not simply inherited. A hook is not, by itself, a general right to seize all future balances. A Token-2022 permanent delegate has separate authority to transfer or burn holdings across the mint. An ordinary approved account delegate is another way holdings can move later. No conclusion about the user's earlier incident on another chain is possible without its transactions. [S1][S2][S3]

Mint/freeze authorities revoked to `None` on standard SPL tokens cannot simply be restored. Metadata mutability alone does not give minting or confiscation rights. Locked LP can still coexist with insiders selling a large inventory into it. These distinctions keep the screen focused on capabilities instead of misleading labels. [S4]

## Sources checked 2026-10-08

- [S1: Solana transfer hooks](https://solana.com/docs/tokens/extensions/transfer-hook)
- [S2: Solana permanent delegates](https://solana.com/docs/tokens/extensions/permanent-delegate)
- [S3: Token-2022 extension guide](https://www.solana-program.com/docs/token-2022/extensions)
- [S4: Solana authority revocation](https://solana.com/docs/tokens/basics/set-authority)
- [S5: Canonical SPL mint/account layout](https://raw.githubusercontent.com/solana-program/token/main/interface/src/state.rs)
- [S6: Solana simulation RPC](https://solana.com/docs/rpc/http/simulatetransaction)
- [S7: Bubblemaps links and supernodes](https://wiki.bubblemaps.io/bubblemaps-v2/how-does-it-work)
- [S8: Bubblemaps bundles versus clusters](https://blog.bubblemaps.io/whats-the-difference-between-bundle-cluster-2/)
- [S9: Jito submission and limitations](https://docs.jito.wtf/lowlatencytxnsend/)

All screening thresholds, pipeline choices and prototype limitations above are project design decisions, not vendor claims or proven loss-prevention rates.
