# Pool evidence integration review

Source: `integration/cloud-wave1` at
`c578dbead9931762a35a9a51b45b6aad62e1597c` (includes accepted PR57).
Scope: accepted `pool_capture_bridge` and `pool_vault_admission` composition.
No demonstrated defect required production changes. This PR adds dedicated
integrated fixtures and this report; no receipt-journal SQL or shared queue or
readiness files change.

## Executed evidence

`tests/test_pool_evidence_integration.py` composes the actual fixed
`HeliusMainnetRPC`, capture bridge, persisted ownership admission/shared budget,
protected receipt writer, newly opened read-only receipt reader, guarded policy
view and raw vault admission. Only urllib's HTTPS response handler is replaced;
RPC argument/envelope validation, hashing, persistence, receipt reconstruction
and admission execute unchanged. Socket creation is guarded and the only key is
a literal fake fixture value. The mock observes the committed counter before
each of the four HTTP requests: 1, 2, 3, 4. There is no live source attestation.

The raw fixture is `fixtures/pool-vault-admission-legacy.json`, whose provenance
is explicitly synthetic, constructed from canonical PDA/legacy SPL layouts and
mint bytes `[7]*32`. Tests simulate the fixed coordinator source branch; its
production point-exclusion flag inside this trusted-local harness does not
authorize a real production exclusion or authenticate a provider.

Seven integrated scenarios establish:

- A finalized request floor S=100 remains unchanged in the response envelope
  and persisted request manifest. Returned bank T=107 is bound to the receipt,
  `getBlockTime(107)`, read-only receipt scope and both reconstructed vault
  labels. No receipt is fabricated at S; querying admission at S rejects the
  acquisition scope. The original 18-call counter and descriptor are preserved.
- At original `captured_at + max_age - 1` both labels remain fresh. At the
  exclusive expiry boundary both become UNKNOWN. Replaying the completed
  bridge performs zero HTTP calls, leaves the receipt/capture/page rows unchanged,
  and cannot refresh acquisition time or regain freshness.
- Wrong raw base-vault mint or quote-vault authority is retained in the raw
  content-addressed response and receipt. Both vault admissions become UNKNOWN
  with `VAULT_MINT_OR_AUTHORITY_MISMATCH`; evidence is not erased or normalized.
- Wrong query mint or vault rejects; changed trusted source roster or configured
  freshness cannot rebind the existing protected ledger. Wrong investigation
  mint causes no extra HTTP calls or record/budget changes.
- A real point label is not a generic interval classification record. Even a
  correctly hashed label cannot pass `classify_account`, and the existing
  classification-to-exposure adapter remains UNRESOLVED/verified=False.
- Self-asserted complete fee configurations at bank 99 in a bank-107 response
  are preserved as raw annotations, never copied into point labels or accepted
  as fee evidence. Passing those point labels to `check_sell_fee_totals` yields
  `SELL_FEE_EVIDENCE_UNVERIFIED`, with fee and full-route approval false.
- Splicing a fee configuration account into the identity batch rejects the
  seven-account response as `ATOMIC_ACCOUNT_SET_INCOMPLETE`; the six-account
  contract is not silently widened.

The broader targeted suite also exercises T<S rejection, same-T contradictions
with distinct floors/sources, shared-budget exhaustion, raw/token/configuration
failures, missing records, durable failures and idempotent replay. The new suite
adds the accepted transport-to-read-only-receipt composition rather than copies
of existing module-unit tests.

## Boundary and exact remaining prerequisites

Point labels establish only narrow legacy canonical vault identity under
explicit RPC-source trust. They never establish cryptographic chain proof,
historical continuity, private/common control, ownership approval, liquidity
control, fee correctness, supported exits or entry eligibility. Token2022 and
unsupported profiles remain blocked. No production scheduler or pool capture
CLI is wired by this work.

Before any coordinator-run live capture:

1. Independently provision and review the fixed
   `helius-mainnet-single-request-v1` coordinator source descriptor and exact
   `HeliusMainnetRPC` callable in the protected source roster. Candidate JSON
   must not choose the source, URL, credential, receipt or policy. Supply the
   existing `HELIUS_API_KEY` only locally at invocation; never copy it to cloud,
   persist it in records or print it. TLS/provider identity is a trust
   prerequisite, not a cryptographic bank/finality proof.
2. Provision the supported single-host Linux local filesystem boundary: owned
   canonical stable private root (0700), protected ledger/lock files (0600),
   owned single-link evidence file, and the standard supported SQLite locking
   contract. No symlink/hardlink aliases, replacement/rotation or hostile same-UID
   writes while workers operate. Open the ledger writer only from trusted
   coordinator code; retain all approved observations, including contradictions.
3. Supply an existing persisted ownership admission with exact scan, descriptor
   hash and supported mint binding. Allow sufficient headroom for the four
   genesis/slot/snapshot/time attempts within the SAME original ceiling of 18
   (at most 14 already spent for a fresh complete four-call capture). Every
   attempted call, including failure, remains charged. A failed/in-flight or
   damaged prior capture is a durable blocker, not permission to retry with a
   new ID, reset the budget or delete evidence.
4. Obtain the finalized canonical six-account response at actual bank T>=S and
   exact `getBlockTime(T)` within the configured wall-clock freshness window.
   Retain original manifests and all four content references. Coordinator time
   must be trustworthy: capture must be no earlier than bank block time, no
   more than 300 seconds later, and evaluated before configured receipt expiry
   (default 60 seconds). Delayed clock lookup/replay does not refresh the bank.
   Missing, malformed, stale or contradictory evidence remains UNKNOWN.

For later live paper entry, these are separate unresolved dependencies:
independently trusted same-bank global/dynamic fee configuration and reserve/mint
state, liquidity/LP control, exact-size unsigned route/exit verification, and
complete lifecycle/history/interval classification/holder exposure. The capture
profile has NO fee configuration accounts; mismatch tests prove refusal to
promote point evidence, not successful fee-state acquisition. No interval or
entry adapter is added and no readiness signoff follows these fixtures. The
coordinator alone owns integration, provisioning and any live verification.

## Validation

Python 3.12.14, fixture/mock transport only:

- Dedicated integrated suite: **7 tests passed in 1.553 seconds**.
- Targeted integrated/bridge/admission/ledger/transport/sell-fee suite:
  **161 tests passed in 15.179 seconds**.
- Full Linux `python -m unittest discover -q`: **1,343 tests passed in
  58.343 seconds**, no failures or skips. Existing unrelated loopback tests
  used the approved sandbox execution exception; provider tests stayed fixtures.
- `git diff --cached --check`: passed.

No provider/VPS/signing/broadcast/real-funds/paid-service
access, merge, deployment or issue closure occurred.
