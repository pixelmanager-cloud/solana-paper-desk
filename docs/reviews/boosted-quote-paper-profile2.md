# Explicit boosted quote-paper profile2

Base: reviewed integration `5cbd0da982c71914241265110d9152d5387ef204`.
No deployment, providers or retired-scan retries. All vertical fixtures are
SYNTHETIC_TEST_ONLY; authentic public excerpts are historical structural data.

## Selection and ownership

New immutable experiment configuration: `mode=paper`,
`paper_signal_policy_version=3`, `paper_quote_execution_version=1`,
`paper_token_profile_version=2`. Versions0/1 retain their current behavior.
Version2 keeps the exact metadata-only Token2022/ImmutableOwner subset and
canonical pool/vault/LP safety gates. It additionally selects a pricing-aware
paper pool and signal subprofile. Existing ledger config bytes cannot change.
Runtime/monitoring context handoff and deployment remain coordinator work;
no runtime compatibility allowlist or existing ledger is rewritten here.

Owned vertical files: token profile/CLI selection, security/pool ingestion,
dynamic fees, market/exit builders, model/strategy dispatch, quote execution,
checkpoint/report/view validation and dedicated fixture tests. Budget, transport,
service, runtime continuation and reconciliation implementation are unchanged.

## Primary semantics

Pinned official docs revision `2293f9a66c654e9fe82dc5e8f4618538f24bb35f`:
`docs/VIRTUAL_QUOTE_RESERVES_FEE_ADJUSTMENT.md` and
`docs/NEGATIVE_VIRTUAL_QUOTE_RESERVES.md`. Official SDK2.1.0 provenance is already
pinned in `desk/schemas/fee_sdk_manifest.json`; authoritative calculation paths
are `src/sdk/sell.ts` (sellBaseInput/sellAmounts), `fees.ts` and `util/fee.ts`.
Program implementation is not published here; these are the primary published
integration contract, not bytecode verification or a transaction certificate.

Let G be gross quote vault, F protocol+creator buckets, V signed i128 virtual.
Physical spendable S=G-F; effective pricing E=G+V; boost B=V+F=E-S.
Require positive bounded S/E, nonnegative B, original verified same-bank source.
Negative virtual is permitted if these invariants hold; negative derived boost
is invalid evidence. Fees are subtracted exactly once. Unknown appended data,
mayhem/cashback/holder rewards/mutable creator/LP controls remain rejected.

`reserve_sol` stays physical S for liquidity and sizing. Spot/capitalization and
fee tiers use E. Every profile2 execution record retains original pool JSON,
hash, timestamp and source ID. Replay recomputes policy from the original bytes,
binds mint program/decimals/bank and checks event physical reserves.

For an exact sell input q, SDK gross output is floor(E*q/(base+q)); fees round
up per the pinned SDK. The physical obligation is gross output minus LP fee,
before protocol/creator deductions or any adverse haircuts. It must be <=S.
Provider estimated output must not exceed the independently calculated snapshot
user output. Only one complete canonical-pool hop is supported. Quotes do not
prove fill, account execution or future liquidity. Snapshot/quote differences
can conservatively refuse a legitimate quote; there is no optimistic fallback.

Historical events lack aggregate protocol fee buckets, so physical historical
liquidity is not inferred. Version2 selects
`observable-flow-churn-concentration-effective-pricing-v2` and the new measured
`volume_vs_effective_pricing_reserves` = window quote principal /
(2 * latest original event (quote vault + signed virtual)). Original event/page
hashes, captured window and formula are retained. Legacy `volume_vs_liq` remains
UNKNOWN/null. Model/strategy/checkpoint dispatch requires trusted saved profile2;
entry decisions persist the selected feature identity. The arithmetic view used
for scoring is private, never a replacement for persisted physical liquidity.

## Actual candidate242, not entry eligibility

Coordinator original snapshot reference:
`da20be9e1a2d8e5a6754ffc086718158c8de4fbd17458e7f735101582db2e437`.
Independent offline decoding of supplied original pool/vault bytes gives:
base964743019199758; gross/spendable2666323439lamports;
protocol0/creator0; virtual/boost17584505290lamports;
effective20250828729lamports; circulating LP0. Physical2.666323439SOL is not
20.250828729SOL liquid capital. Original candidate242/222 scans remain retired.
The unchanged physical liquidity threshold and sizing apply; no eligibility,
profitability or retry authority is claimed.

## Verification / remaining acceptance

Dedicated fixtures cover profile isolation, signed bounds, fee underflow,
known pool flags, one-unit physical obligation before fees/slippage, original
history pricing identity, actual acquisition->entry->held mark->full/partial
exit->restart, exact-size quotes, cost accounting, record binding and unchanged
budget counters. Full Linux suite evidence is exact-head GitHub CI; no duplicate
long cloud run is launched. Independent source review remains required. No
live lifecycle or deployment acceptance is inferred from fixtures.
