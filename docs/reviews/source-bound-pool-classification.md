# Source-bound pool-vault point prerequisite

Base: `2ab25cbb8472fd29072da078e2fa7e049e0c65ea`.

The narrow missing contract was the join between two existing components, not
another pool decoder or generic source registry. `admit_pool_vault` already
validates canonical pool/LP/vault PDAs, exact legacy raw program/mint/authority,
same-response six-account state, finalized request provenance, actual response
slot/block time, freshness and all competing source-approved observations.
`continuation_snapshot` already binds a persisted scan/source/seal/budget and
current revision to the canonical ownership bank and raw history replay. Neither
previously established that a protected-ledger-admitted pool point agreed with
the exact mint and vault endpoint used by that ownership revision.

`desk.pool_classification_projection.project_pool_vault` provides that join as a
standalone read-only prerequisite. Trusted application initialization supplies a
read-only `PoolReceiptLedger` from its protected coordinator boundary and the
research database path. Candidate JSON must never configure that dependency.
The query supplies only scan/revision/account/pool identities and evaluation time;
there is no loader, policy, trusted-hash list or caller validation flag parameter.
The ledger's configured evidence-store identity supplies the only raw reader.

The projection holds the source database's existing OFD read guard, then the
evidence read guard, then the ledger's current policy view. It loads the actual
scan, reuses continuation replay with the exact selected head, and reads its
canonical bank/clock refs. It selects the newest scoped receipt deterministically
without removing any other observation: existing admission still checks all
approved observations of the pool/mint/actual finalized slot, including stale,
malformed and contradictory captures. Every read shares the existing 128-reference,
128-load and 32 MiB replay profile. Failures cannot use a cached policy or publish
a positive prefix, including guard teardown failures.

Admission's slot and block time must equal the canonical revision bank. The base
mint and vault must agree on exact decoded raw bytes, chain owner, executable
status and lamports. The vault must appear in the revision's discovered frontier;
quote/WSOL vaults cannot become base-mint holder exclusions. Transport annotations
are not bank state. Existing raw metadata/space checks remain mandatory.
No source records, requests, canonical pointers or historical decisions change.

Success means `point_binding_verified=True`, label POOL_VAULT, exact source hash,
revision/bank/clock/receipt evidence references, slot/time and exclusive receipt
expiry. Synthetic fixture sources visibly retain their source kind and have
`production_point_prerequisite=False`; only an independently configured
coordinator-capture endorsement can satisfy that point prerequisite. Fixture
coordinator configuration in tests is an explicit harness simulation, not live
evidence. Python types/hashes cannot authenticate a source; the existing protected
UID/filesystem/registry boundary remains the authority assumption, with its
documented privileged-writer limitations.

The result always leaves classification_resolved, exclusion_allowed, historical
interval exclusion, private control, ownership approval, chain authentication and
entry eligibility false. It emits no generic classification record, exposure-core
input, top-ten private concentration, service label or market event. Existing
classification/exposure/entry consumers are untouched. Incomplete/unreconciled
history is retained separately in `history_reasons`; a matching point cannot
erase it. Token-2022 remains excluded by existing admission and exact legacy
endpoint comparison.

Dedicated fixtures derive canonical pool addresses from the existing pool fixture
and explicitly transform the existing multi-account raw history fixture's mint,
curve PDA, selected token address/authority and serialized instruction/event bytes.
Actual fixture transport, storage, worker continuation, decoding, replay and
receipt persistence run unmocked. The synthetic positive uses eight history pages,
13 charged total requests and zero projection provider calls; repeated projection
is deterministic and all original database/lock bytes remain unchanged.

Adversarial tests exercise wrong/stale revisions, changed source assertions, wrong
frontier/pool, exact expiry boundary, canonical-bank lamport substitution,
contradictory point captures/block times, Token-2022 evidence, valid self-hashed
records without receipts, candidate JSON/writer dependencies, corrupt ledger heads,
guard teardown failure and retained unreconciled history. Existing validators'
owner/mint/authority/PDA adversarial coverage is reused rather than duplicated.

Remaining exact inputs/decision: deployment must supply its independently approved
read-only ledger and an already captured source-approved pool point at the SAME
actual finalized bank T/block time as the canonical ownership revision, with
matching mint/vault bytes. If that receipt is absent or was captured at a later
bank, this projection rejects. `minContextSlot` cannot retroactively select T;
never rewrite saved requests or transplant later bytes. Historical holder
exclusion still requires independently justified interval continuity/control and
history/source semantics; this point prerequisite does not implement those claims.
Coordinator integration must review this standalone contract before any consumer
wiring. No provider/VPS/secrets/signing, shared queue/readiness edits, merge or
deployment occurred in this task.

Final Python 3.12.14/Linux fixture-only results: 83 focused projection/admission/
classification tests in 4.238s, OK; full unittest discovery 1,576 tests in 83.612s,
OK. Zero failures/errors/skips. Diff check passed. No outstanding test failures.
