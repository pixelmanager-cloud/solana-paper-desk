# Issue #5: unsigned route and sellability audit

Audit date: 8 October 2026 (Korea time). Agent 07, branch
`codex/cloud-wave1-07`, isolated checkout `/workspace/solana-paper-desk-agent07`.
Reviewed base: `e95a8dfcd0536efa5479934f81b872c050d7ed5e`.
Issue: https://github.com/pixelmanager-cloud/solana-paper-desk/issues/5.

Status: preparatory audit only. SELL-1 remains dependent on OWN-4 / issue #4.
The queue records OWN-4 and SELL-1 as BACKLOG. No ownership completion,
transaction approval, live sellability, entry permission or paper readiness is
established by this work. Only tests and this review are changed.

## Findings and executable evidence

| Boundary | Observed behavior | Audit reproduction / consequence |
| --- | --- | --- |
| Roundtrip instruction policy | `simulate_roundtrip` calls effect/control checks but not inventory, router, envelope, gross debit, AMM recipient, fee or route coverage checks. | `test_malicious_outer_approval_is_not_checked_by_roundtrip` supplies an added forbidden approval alongside unchanged captured post-state. Independent inventory rejects it; roundtrip still returns `effects_passed=True`. All final approval flags stay false. This is an orchestration gap, not a demonstrated executable malicious chain transaction. |
| Missing CPI evidence | Removing `innerInstructions` from both replayed legs does not change roundtrip effect success. | `test_missing_inner_inventory_does_not_change_roundtrip_effects` demonstrates that net effect success does not attest complete instruction coverage. |
| Compiler vs policy | `compile_unsigned` verifies signer count, null signatures, lookup contents and packet/key budgets. It serializes provider `otherInstructions`; it does not authorize their effects. | `test_compiler_serializes_extra_native_transfer_but_envelope_rejects_it` uses the real compiler to produce two System transfers, verifies null signatures, and observes envelope rejection. Compilation is not route approval. No transaction is executed. |
| Exact input | Buy native wealth debit, excluding fee, must equal spend; sell net token debit must equal requested raw size. Quote sell size must equal the conservative buy minimum. | One-unit input deviations fail for both legs; quote-size mismatch fails before compilation. These checks concern net effects. Gross withdrawal/refund cycles require separate instruction checks, which roundtrip currently omits. Existing `tests/test_debits.py` covers overdebit/refund and unrelated-token cycles for standalone sells. |
| Output bounds | Output is checked against a minimum, not equality to the optimistic quoted output. Rent and WSOL are included in native wealth accounting. | One raw unit above witnessed buy receipt or sell proceeds-before-network-fee fails the respective minimum check. Do not describe this as exact-output verification; the supported intent is exact-input with a minimum output. |
| Partial exit | The captured buy receives 2,548,941 raw units; its sell consumes only 2,523,452. | Replay leaves exactly 25,489 raw units and net native wealth delta -143,010 lamports. Neither full liquidation nor profitability follows. A position holding all received tokens needs separate full-size exit evidence. |
| Recipient policy | Bound sell transfers must reach the approved user/vault/fee roles. Cleanup refunds must reach the wallet. | Synthetic bound-transfer tests reject an external recipient and fee/user role alias; captured cleanup with a substituted refund recipient fails. Recipient success still does not verify complete fee policy. |
| Hidden control effects | Matching net balances cannot excuse a delegate appearing in returned account bytes. | The roundtrip post-sell delegate attack fails despite unchanged amounts. Existing controls also reject freeze, owner/close-authority changes, missing metadata and state/metadata contradictions. |
| Unsupported route/layout | Saved public sell balance effects can pass while its Jupiter multihop route remains outside the direct PumpSwap-sell profile. | Public multihop replay and appended unknown router bytes fail route checks. Legacy mint suffixes and Token-2022 fail token policy; unsupported roundtrip mint stops before quote/compilation. These tests do not approve any extension. |
| Unsigned execution boundary | Sequential simulation accepts only null signatures. | Fabricated nonzero signature bytes fail before the injected transport can be called. No keypair, signer or broadcast is used. |
| Exact-size exit proof | `sellability_gate` binds mint/wallet, decimal token quantity, timestamp, positive proceeds, hash format and asserted flags. | Smaller/larger sizes, full-size proof reused for a partial exit, stale/future timestamps and wrong identities fail. A roundtrip diagnostic itself is rejected as the wrong proof kind. |
| Adapter trust boundary | The low-level gate does not load/reconstruct hash-addressed evidence; formatted hashes and caller-set success flags can satisfy it. `position_sellability` also accepts those assertions for a matching real-position wallet. | `test_formatted_hashes_and_asserted_flags_are_not_reconstructed_by_gate` deliberately records this acceptance using synthetic assertions. It is a downstream trust-boundary risk, not proof that automatic entries are enabled. The engine's `LIVE_FEATURE_ADAPTER_NOT_READY` entry guard and the reject-only decision service remain essential. A trusted exit adapter must reconstruct evidence rather than copy flags from normalized JSON. |

## Existing safety boundaries preserved

Standalone `simulate_sell` already runs inventory, gross debits/exact rent,
router/envelope, AMM bindings, recipient, aggregate-fee, fee-call/split,
sell-event, setup and role-coverage checks. `replay_sell` reconstructs version-2
captures and reruns checks without provider calls. Both remain diagnostic;
`transaction_policy_ok` and trading eligibility remain false. Historical replay
is not fresh sellability evidence. The older public sell capture lacks the newer
complete reconstruction inputs and is not relabeled as version-2 verified data.

`fee_split` checks the historically observed 50% buyback profile; its
`fee_amounts_verified` and `full_route_policy_passed` remain false. Role coverage
retains `INDEPENDENT_FULL_ROUTE_VALIDATION_REQUIRED`. Those blockers must not be
removed merely because recipient totals, events or all unit tests agree.

The engine requests full-position evidence before refreshing marks, then checks
the actual partial quantity again when selling a ladder rung. A single exact-size
proof cannot satisfy both full and partial quantities. The future driver needs
separate evidence requests/handling; weakening equality or scaling a captured
quote would conceal this mismatch. This audit tests the equality boundary and
does not implement the dependent driver.

No request-budget, original-record, loopback, evidence-gate, schema, provider,
strategy, ledger, shared AGENTS/readiness/queue or runtime code was changed.
Provider RPC attempts made by this audit: **0** (the 18-RPC ceiling is unchanged).
No VPS, secrets, live quotes, live providers, deployment or issue mutation was used.

## Provenance and reproduction limits

`fixtures/mainnet-roundtrip-simulation.json` and
`fixtures/mainnet-sell-simulation.json` are existing committed public unsigned,
unsubmitted captures. They were read locally and not rewritten. Their historical
accounts/results exercise actual effect and control code, not current mainnet
conditions. Synthetic modifications are created only in memory and explicitly
identified in tests.

The roundtrip harness patches compilation and sequence transport. Its bytes are
an explicit non-transaction stub; it does not test transaction serialization,
lookup-table verification or altered-instruction execution. This isolates the
missing orchestration check. The separate compiler test does serialize a real
unsigned transaction locally, with no lookup tables and a transport that fails
on any call. Signature rejection invokes the real sequence validator before RPC.
The public sell minimum-output comparison uses a synthetic minimum of one
lamport because that historical capture does not include a saved route minimum;
it only demonstrates balance-check success alongside route rejection.

Tests do not refresh historical timestamps as live evidence. The orchestration
harness uses a fixed synthetic clock to exercise its freshness branch; historical
fixtures remain historical and no positive live result is reported.

## Validation and coordinator handoff

Python **3.12.14**, existing dependency versions `solders==0.29.0` and
`websockets==15.0.1`, copied locally into the isolated ignored `.venv`; no package
network access or shared checkout writes were needed.

Commands from the isolated checkout:

```sh
.venv/bin/python -m unittest tests.test_cloud_sellability -q
.venv/bin/python -m unittest discover -q
```

Final targeted result: **Ran 21 tests in 0.065s — OK**.
Final full-suite result: **Ran 534 tests in 2.303s — OK**.
The first sandboxed full-suite attempt ran 531 tests and failed solely because
its existing dashboard test could not create a socket (`PermissionError`). The
final full run used approved sandbox escalation for the existing ephemeral
`127.0.0.1` HTTP test. No external provider calls were required. Initial targeted
iterations exposed missing dependency/fixture-field assumptions and were corrected
before the final runs. Passing characterization tests document gaps, not approval.

Unresolved dependencies and follow-up, for coordinator review after OWN-4:

1. Independent ownership integration review and milestone acceptance (issue #4).
2. Per-leg complete unsigned buy/sell instruction, gross-debit, recipient, rent,
   fee and account-effect policy with exact supported route provenance. Buy policy
   cannot be inferred from the standalone sell profile.
3. Independent fee-split/full-route verification; supported fresh evidence must
   survive reconstruction rather than accepting summary success flags.
4. Wallet/position-specific full and partial exit sizes, raw-unit/decimal binding,
   residual inventory and costs, sequencing/freshness and outage handling in a
   trusted adapter/driver. No dependent integration is implemented here.
5. Coordinator-controlled live verification and forward paper observation only
   after prerequisites. Tests and public-holder diagnostics cannot satisfy these.

Changed paths: `tests/test_cloud_sellability.py` and
`docs/reviews/cloud-sellability.md`. Retrieve the local commit/branch from this
cloud workspace; no push, merge, deploy, issue closure or readiness change occurred.
