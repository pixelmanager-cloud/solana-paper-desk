# Jupiter-independent direct PumpSwap quote proposal

Base: `c1c613b696755c53912feb45639191045b831554`. Research checked 10 October
2026. No quote endpoint, RPC/provider, credentials or VPS access occurred.
Only official documentation and public npm SDK artifacts were fetched.

## Coverage decision

Recommend **direct canonical PumpSwap WSOL/base exact-input local estimates**,
using the existing raw Helius account captures. Do not integrate Raydium as a
PumpSwap fallback. The actual pool verifier is PumpSwap-specific; migration
witnesses bind Pump `migrate`/`migrate_v2` to that program. Decoder recognition
of another instruction does not establish supported quote/account semantics.

Raydium's official [Trade API description](https://github.com/raydium-io/raydium-docs-v1/blob/main/sdk-api/trade-api.mdx)
documents read-only `/compute/swap-base-in` and `/compute/swap-base-out` requests.
Its router covers Raydium CPMM/CLMM/AMM v4, with raw integer quantity, output
threshold, fees and impact. This does not establish PumpSwap coverage; a token
would need an independently supported Raydium pool/route. No such pool-family
validator exists here. No transaction POST/build/sign/send endpoint is needed
or authorized. Indexed API state is also not an original common-bank account
snapshot. Thus this endpoint is not a demonstrated weekend unblocker for these
migration pools; no endpoint was queried.

Pump's [official PumpSwap documentation](https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_SWAP_README.md)
identifies its distinct constant-product program and canonical migration pools.
Its [signed virtual reserve guidance](https://github.com/pump-fun/pump-public-docs/blob/main/docs/NEGATIVE_VIRTUAL_QUOTE_RESERVES.md)
requires both directions to price using gross quote vault plus signed virtual
reserves. The vault net of accrued fees is the physical payout capacity. Local
math avoids Jupiter indexing/access altogether, but depends on fresh supported
account state and preserves all existing known-hazard rejection.

## Draft, exact scope

`desk/local_pumpswap_quote.py:quote_exact_input` is disconnected pure replay/math.
It accepts an original `ProviderObservation`, expected RPC source ID and capture
hash, exact mint/pool/taker/direction/input, integer slippage, trusted current time
and explicit token profile1/2. It reuses `ingest_pool` and `verify_pool` against
the original atomic capture; it never trusts normalized caller PASS flags.
No network, writes, transaction construction, signer or production imports.

Both supported directions use raw integers and fee ceilings. Buy ports SDK
`buyQuoteInput`, including fee-budget shaving and the one-raw-unit swap buffer.
Sell ports `sellBaseInput`/`sellAmounts`, rejecting when gross outflow minus LP
fee exceeds physical quote reserves **before** protocol/creator deductions.
Fee tiers use the same effective reserve exactly once; absent coin creator
charges zero creator fee. Slippage produces a conservative integer output floor,
not an execution guarantee. Impact is a rational comparison to pretrade spot
using net swap input/gross output (excludes fee impact); raw vault/effective
capacity and original fee configuration are independently replayed.

Only the existing metadata-only Token2022 profiles are admitted. Transfer-fee,
hook and unknown extensions, non-WSOL quotes, noncanonical pools, mayhem,
cashback/rewards, mutable creator overrides and other existing hazards reject.
Profile1 requires proven nonboost behavior; profile2 permits supported boosted
pricing with physical payout checks. Unsupported states stay unsupported.

The distinct `local_pumpswap_quote_v1` envelope binds request/hash, capture hash,
RPC identity/time, bank slot and math version. It is neither a Jupiter response
nor a `QuoteObservation` accepted by the current runner. All entry/execution
approvals remain false; execution is `EXECUTION_UNVERIFIED`. Buy input is a
conservative spend budget, not proof every lamport will transfer. No runtime
activation, safe metric defaults or chart-price fill is included.

## Official math identity and tests

The official npm latest version observed is `@pump-fun/pump-swap-sdk@2.1.0`,
git `0bc59090bbd0b8d6c27b8df72d52e30bfc069c3b`, matching the existing manifest.
[Official SDK repository](https://github.com/pump-fun/pump-swap-sdk/tree/0bc59090bbd0b8d6c27b8df72d52e30bfc069c3b),
[versioned tarball](https://registry.npmjs.org/@pump-fun/pump-swap-sdk/-/pump-swap-sdk-2.1.0.tgz).
Tarball SHA512 integrity matches the manifest. SHA256 source pins independently
checked: buy.ts `f0da0f9a62fe2eab0c5780205b61abcce8d20686f81b7896a9b00db377bc0f12`;
sell.ts `97bf00f0f7b4d1d694a0143f603b8f103bdcc0943a86cfdc75492eb52ca8d94b`;
fees.ts `45d55d0faca9aec48aca2637f885973602182012ab9623c703f42fec9be164c1`;
util.ts `f1e43eb6d5e3ce1ac4014935208a961bfac331aab5151d15b58c6272f95a6737`.
The SDK fee helper uses ceiling despite an outdated floor comment in buy.ts.

Offline Node invocation of actual pinned `buyQuoteInput`/`sellBaseInput` matched
12 buy/sell vectors (inputs100,1000,10000,99999,100000,500000; reserves1,000,000
each; LP20/protocol5; absent creator). Oracle JSON SHA256
`dc965652d78a8c371bb185925450b1ef991542d129e68a92d9c0ed10a501a3fd`.
Python synthetic original-account tests additionally cover hash/source/time,
exact request binding, stale/future, integer/u64/slippage bounds, rehashed raw
identity/control corruption, LP/delegate hazards, unknown program, missing
accounts, negative virtual reserves, boosted payout failure, metadata-only base
and LP Token2022, and transfer-fee rejection. No provider fixtures fabricated.

## Smallest integration follow-up (not implemented here)

1. `paper_observation_collector.collect_observations` and
   `paper_cycle._collect_held`/`fulfill_quotes`: explicit local-source dispatch
   using already charged atomic account reads, retaining capture + local result
   with a distinct version/source. Never claim the pure calculation charged an
   RPC. Current collector expects every `source.quote` call to increment the
   shared counter; passing this calculator as a fake remote quote would fail
   that contract or falsely charge a nonexistent request.
2. `live_observation.ingest_quote`: distinct replay validation for local source,
   not a forged `unsigned_route_probe`. Bind refreshed exact input/identity/time
   and retained original snapshot. Any refreshed account request must use normal
   caller reservations/pacer/deadline; no retry or enlarged18 investigation cap.
3. `quote_execution`/checkpoint/report readers: explicit versioned compatibility
   and fee accounting. Existing paper costs add modeled pool/network costs;
   avoid charging the same SDK pool fee twice. Preserve explicit modeled
   network/slippage assumptions and immutable saved entry quantity/decimals.

Existing acquisition uses mint/discovery/atomic reads; local math adds zero
network requests if those snapshots are fresh. Held-path four account reads can
serve both directions' math; exact new budget counts require call-site fixtures,
not an assumed subtraction of Jupiter requests. SOL/USD remains separate and
owned by worker07; this proposal does not overlap Kraken. Read/sell after state
changes requires fresh captures, never timestamp-restamping an old bank.

Independent review, integrated raw-bound `QuoteObservation` replay, source/config
version pins, actual accounting/held-cycle/restart fixtures and deployment gates
remain prerequisites. This draft demonstrates a viable quote calculator, not an
activated alternative or live-data entry readiness.
