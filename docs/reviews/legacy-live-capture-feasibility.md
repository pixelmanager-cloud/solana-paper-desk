# Public legacy launch capture feasibility

Read-only worker 03 review, 8 October 2026 (Korea time), integration base
`29802548f62d53495597a438a7eb72e0b2ea91d8`.
Read AGENTS.md, readiness and task queue; inspected committed public fixtures,
the active schema manifest, decoder/anchor, screen and ownership continuation.
No provider requests, VPS access, current-state inference or fixture changes.

## Result

**No committed public legacy SPL mint establishes a verified creation witness
accepted by this code and a feasible complete ownership replay under 18 requests.**
This is a conclusion about available committed evidence, not a claim that no
suitable legacy Pump mint exists on chain today. Synthetic legacy launches prove
the implementation can exercise the supported path; they cannot supply live
acceptance evidence.

## Exact public candidates and provenance

| Public mint | Committed evidence | Offline conclusion |
| --- | --- | --- |
| `AoPfwh6vExgSrfzX2ALPBEpWS2wSjhcG2ZxKdN1Vpump` | `fixtures/mainnet-launch.json`, provenance `PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE` | Pump `create_v2`, Token-2022 initialization; not legacy. Raw notification is confirmed and lacks blockTime, so its anchor is not verified as captured. |
| `7aN1pJGiMM93gjYgCqn9ReyexzzLLrFVotUcJG62JrbC` | `fixtures/mainnet-distribution.json`, provenance `PUBLIC_MAINNET_FINALIZED_HISTORY_PAGE`; `mainnet-holder-snapshot.json`, `PUBLIC_MAINNET_HOLDER_BANK_SNAPSHOT`; `mainnet-block-order.json`, `PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE` | Genuine public legacy initialization and saved holder/order facts, but no supported Pump creation corroboration. Seventeen positive accounts at the saved holder bank already exceed the full-acquisition 18-request lower bound. |
| `METvsvVRapdj9cFLzq4Tr43xK4tAjQfwX76z3n6mWQL` | `fixtures/mainnet-sell-simulation.json`, `UNSIGNED_UNSUBMITTED_MAINNET_SIMULATION`; `mainnet-roundtrip-simulation.json`, `UNSIGNED_UNSUBMITTED_MAINNET_BUY_PARTIAL_SELL` | Legacy diagnostic/simulation evidence, no committed raw creation witness, exhausted request-bound birth history or historical account inventory. Birth date, account-query costs and replay feasibility unknown. |
| `B4v7fATuSdtNsJximsbLXZ5wMenSvujfED71xe5ZxrPk` | `fixtures/mainnet-pool-snapshot.json` and `mainnet-pool-fee-snapshot.json`, `PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE` | Pool evidence is not a creation witness. The later saved pool result includes `TOKEN_2022_NOT_ALLOWED`; do not use this as a legacy positive candidate. |

File SHA-256 values (original committed bytes):

```text
mainnet-launch.json          d80cf9876fdb3e9b465e4103fa14bf2baaf09483dad9a27a57ff7339624b3bf2
mainnet-distribution.json    b5b6dfb1668a0307f2f3b95e6b1bb35361ebc122d97abe0b1f1cac6d26795803
mainnet-holder-snapshot.json eb60d68e45cc1d4d5b9ce4dcaab9e1be4763c548c0199a37b6579b93ceb25eac
mainnet-block-order.json     3249d223d58da34c08412b77080e4e917c0854806b5802f91365ba34af5e28d2
mainnet-sell-simulation.json 1e833b316620278fada145d60f6ddb7df2d1a48a9cab1bdc9a6e113e79aa2b2d
mainnet-roundtrip-simulation.json 32a201190ee146fe11c9897397c18fdfb831af8aedeb48bc9e1201ee9070696b
```

The legacy distribution transaction is
`3J9zz8cvRobQtd8UQsii7a6h95SkyBSBhceT5ELSnwP6kJa3Hv7BLXj7oMGg2HSdNefRGykmauKhdhz4Lfg3f3Kv`,
slot 454310062, blockTime 1791399537 (8 October 2026, 03:58:57 Korea time).
It initializes the mint using legacy `TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA`.
Its outer creation program is `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN`,
outside the pinned active program schemas. Production decode returns no program
observations for it. Even when evaluated under a finalized-history assumption,
`launch_anchor` reports `LAUNCH_CORROBORATION_MISSING_OR_AMBIGUOUS`. Generic SPL
mint initialization does not prove the supported launch's creation facts.

The saved holder bank is slot 454347831 with 17 positive legacy accounts and
exact supply 800017057543498. Mint bytes pass the current mint policy offline.
However, its raw RPC params specify **confirmed**, not finalized (despite the
summary's verified atomic supply coverage). It cannot be relabeled as the
finalized ownership bank. Its public block-order fixture verifies order at
454310062, not a Pump creation anchor, full lifetimes or current balances.

The Pump fixture's signature is
`4PJardjqDqxr9Sek37eBGvcQahrkCT2GZ1p8aMxB5BLBHk4DRGkQNwobKxmbD7G4Vergu6eu9fSmbBZPoi1KK1NK`,
slot 454321337. CreateEvent timestamp 1791402595 is 8 October 2026, 04:49:55 Korea
time; received_at is 1791402596. `tests/test_launch.py` supplies the event's time
and finalized commitment to test corroboration; these substitutions are not
independent raw finality/chain-time evidence and do not change Token-2022 into
legacy. Existing finalized-history acceptance noted in readiness is a separate
capture, not an immutable completed legacy source scan supplied by these files.

## Supported creation path and seed gap

Active instruction schemas cover Pump
`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` and PumpSwap
`pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`.
`launch_anchor` accepts only Pump LAUNCH plus a complete CreateEvent and exactly
one mint initialization in the same outer instruction. It checks curve and
mint-authority PDAs, payer/event identity, token program, chain time and absent
freeze authority. Pump `create` pins legacy Tokenkeg (discriminator
`[24,30,200,40,5,28,7,119]`); `create_v2` pins Token-2022
(`[214,144,76,236,95,139,49,180]`). PumpSwap pool creation is not an original mint
launch witness. Historical schema files assist event layouts but do not broaden
the launch verifier to other creation programs.

`screen` first queries mint history in `[max(0,now-21600),now)`, ascending,
finalized, tokenAccounts=none, at most two 100-record pages. It also spends the
same 18-attempt budget on mint/holders/pools/funding and account/order diagnostics.
Exhausting this six-hour window does not include the birth of an older mint.

`ownership_worker.advance` requires an immutable COMPLETE source scan with a
matching report hash/mint, original integer usage 0..18, persisted initial
getAccountInfo at confirmed/base64, a mint-policy pass and exactly one
request-bound mint discovery query with tokenAccounts=none. It seeds and resumes
that original range; it cannot enlarge the seed's timestamp range. **Before**
capturing its finalized bank it requires exhausted mint discovery with verified
initialization inventory, which itself requires the supported creation anchor.
Only after that stage does it re-query `[0,snapshot_slot+1)` by exact slot.
Consequently the later gte=0 query cannot rescue an old mint whose birth was
missing from the initial six-hour seed: continuation never reaches bank capture.

Public single-transaction captures and supply/simulation summaries are not
completed scans, full history pages or provenance-bound manifests. Do not
manufacture source hashes, completeness, finality, original request costs or
backdate observed_at to make a candidate seedable.

## Request feasibility

For a newly acquired complete history/bank with A historical accounts, the
optimistic lower bound is one mint-policy read, one mint-history page, A account
history pages, one bank and one block-time read: **A+4 attempts**, before retries,
extra pages, funding or ordering. The existing worker normally needs a separate
initial timestamp history and bounded mint re-query, so that becomes at least
**A+5**. At the fixture's 17-account bank those minima are 21 and 22, already over
18. Account closure/zero-balance accounts can increase A. This calculation says
nothing about the mint's holder count today, which was not queried.

For an actual seeded scan use its saved usage C0, then account for unfinished
initial pagination, two bank/time attempts, every slot-bounded mint page and
every required slot-bounded account page. Timestamp account histories cannot be
reused as bounded ones. With C0=7, two bank/time reads, two bounded mint pages and
three accounts with two pages each, total usage is 17 before further failures.
This is a conditional arithmetic example, not evidence of a real candidate.
Block-order proofs must also be acquired and charged: the worker replays saved
proofs and does not independently fetch missing getBlock evidence. Unknown order
must stay blocked. Never reset budgets across invocations or discard accounts.

## Smallest bounded capture proposal (coordinator only)

Prefer a low-activity public **legacy Pump create** whose supported birth is
still inside the real six-hour window. Use the existing bounded recorder
(confirmed stream discovery only, e.g. the established 20-second/5-record/200-KB
sample), locally decode candidates and require the actual legacy create profile.
A sample without such a witness is a failed search, not evidence that the
current Pump deployment no longer permits legacy creates. No new subscription
or provider use is authorized to this cloud worker.

For one selected candidate, the coordinator can run a bounded research capture
using existing authorized access, persist the original mint envelope plus full
finalized request-bound birth-inclusive pages and freeze the completed source
scan with honest elapsed time/request accounting. Stop if token policy fails,
the supported witness is absent, pagination is not exhausted within remaining
allowance or historical account count/pages leave insufficient budget. Aim for
one to three historical accounts and very few transactions, but verify counts
from full initialization inventory instead of assuming low activity.

Use ownership continuation to capture a single finalized bank/time before the
exact-slot re-query; persist all account pages, closures and controls and any
required finalized order proofs. Reconcile raw endings to all bank accounts and
supply. Capture failure, unknown ordering, changed frontier or exhaustion remain
valid reject-only outcomes. Passing this accounting component does not satisfy
classification, common-control, route or paper-readiness gates.

If only **older** supported legacy Pump launches can be sourced, the smallest
architectural addition is an explicit bounded birth-inclusive **new acquisition
seed**, retaining real observed_at, raw query manifests and the same shared
18-attempt budget. It must not edit the existing completed scan or pretend a
recent six-hour range includes birth. This is a proposal, not an existing CLI
option; the current provider allowlist has no getTransaction shortcut. Pinning
and verifying another creation program would be a larger independent schema/
anchor task. Neither generic SPL initializeMint nor Token-2022 relaxation is a
substitute for the missing witness.

## What remains unknown

Existence/availability of a recent low-activity supported public legacy Pump
creation; current mint authorities/layout and account controls; finalized birth
pages, historical account frontier and closure/recreation; required page/order
counts and provider capability/quota; exact current supply/bank; trusted source
scan availability; authority-operation normalization follow-up; service/private
classification and current-holder exposure. None was inferred from old fixtures.

Validation was offline Python 3.12 decoding/anchor/policy inspection and SHA-256
of original fixture bytes. No tests were added and no full-suite run was needed
for this documentation-only review. No claims of live acceptance or readiness.
