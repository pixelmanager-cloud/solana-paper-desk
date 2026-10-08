These records are synthetic test evidence. They make no real-world service,
pool, liquidity, private-control or ownership claim. No provider was contacted.
The source hash is a synthetic placeholder, not a captured mainnet response.

`desk.account_classification.classify_account` is an offline policy boundary.
The coordinator must verify a record's underlying source and exact account owner
before admitting its content hash to an independently maintained immutable
`trusted_hashes` policy. Supplying a hash, a source URL, or a `verified` flag alone
is not verification. The loader may be a read-only `EvidenceStore.load`.
Never construct the trusted policy from candidate-controlled investigation data.

Each record binds one account, its observed chain-owner program, one mint and
one purpose. `observed_at <= now < expires_at` and
`observed_slot <= slot < expires_slot` must both hold. No wildcard, wallet-wide
or program-wide propagation is supported. Any invalid supplied candidate blocks
resolution, including a stale attestation alongside a valid one. Replace an
expired policy deliberately; do not silently discard inconvenient evidence.

Consumers must check `classification_resolved` and `exclusion_allowed` at the
actual evaluation scope/time/slot; funding-source labels never grant a holder
exclusion. Unknown/conflicting results remain ownership blockers. Every result
sets `private_control_proven` and `ownership_approval` false. A positive pool or
service label cannot prove liquidity safety, history completeness, common
ownership, token safety or entry eligibility.

Remaining integration belongs to the coordinator and dependent ownership work:
verify/pin real service and pool evidence, derive query bindings from persisted
chain state, supply current evaluation time/slot, and connect unresolved results
to ownership gates. This worker does not modify existing ownership modules or
provide a live registry verifier. OWN-2 has no queue prerequisites; OWN-3 and
OWN-4 remain subsequent integration/review work.
