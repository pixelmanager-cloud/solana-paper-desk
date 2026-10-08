# Observed legacy historical control inventory

Base: accepted integration `c119efb063ca1e1a542da53d9d404555491731e3`.
This dispatched unit adds concrete persisted-history evidence to the existing
guarded obligation projection. It does not implement an interval certificate,
change ownership acquisition/decoding, relax an entry gate or claim readiness.

## Investigation and implemented gap

Reviewed `research/legacy_pump_birth.py`, `research/execution_trace.py`,
`desk/legacy_controls.py`, `desk/legacy_token_accounting.py`, the control
obligation proposal/inventory reports and the raw legacy accounting report.
The offline birth analyzer already retains adverse control witnesses, but only
from one supplied transaction. Its execution-trace namespace explicitly forbids
production imports. The generic decoder inventories control type/path and raw
accounting syntax but does not join normalized targets/authorities to the
persisted multi-account ownership revision. The guarded projection's
`historical_controls` obligation previously had only a constant blocker.

New `desk/historical_controls.py` uses accepted production syntax normalizers
and compiled-key resolution; it never imports the research analyzer/trace.
`control_obligations` invokes it only after `continuation_snapshot` independently
replays the exact persisted source, head, jobs/coverage, canonical bank/clock,
legacy fallback or sealed admission, raw request/pages and original shared
counter. The same guarded `ReplayView` remains the sole raw cache. No new
provider/loader, acquisition, admission, database writes or budget exists.

The nested `historical_controls.observed_inventory` now exposes observed
approve/approveChecked/revoke/setAuthority/freeze/thaw, close and initialization
syntax across mint and historical frontier account queries. The reference
13-request fixture gains four concrete parsed initialization witnesses while
preserving its original observation age/counter and reconciled accounting.
Parsed-only controls expose their supplied parsed witness but retain missing-raw
blockers; they cannot become raw-complete. Existing raw control normalization
gives exact target/authority/delegate/amount/role/option witnesses. Close and
initialization use existing raw accounting syntax or parsed decoder witnesses;
dual lifetime representations remain explicitly unproved. Initialization tags
are labeled `UNVERIFIED_INITIALIZATION_OR_REINITIALIZATION`: no fresh lifetime
or completed recreation is invented from an invocation.

## Identity, bounds and rejection behavior

Every operation binds the original transaction hash/signature/slot, original
instruction hash/path, resolved instruction hash, inner group and instruction
ordinals, supplied stack-height witness and request/query/page/record capture
locations. Compiled instruction identity remains the original indexed bytes;
resolved pubkeys are a separate local syntax view, not authenticated privileges.
Same-hash overlap across queries merges only output identity and retains capture
locations. Different raw versions of one signature remain separate witnesses
with an explicit conflict blocker; no first/last-wins selection occurs.
Iteration follows first capture occurrence and original outer/inner arrays,
not lexical signatures, guessed CPI ancestry or a fabricated total chronology.
Transaction outcomes are labeled provider-declared, including failed attempts;
they never establish individual CPI completion or state-effect ordering.

Missing frontier queries, incomplete/ambiguous coverage/order, omitted inner
groups, duplicate parents, malformed rows/bytes, unsupported operations,
raw/parsed contradictions and wrong mint/outside-frontier targets are explicit
static blockers. Malformed instruction/transaction gaps retain capture identity.
Unknown targets are not assumed irrelevant. A foreign/unbound revision, bad seal,
missing raw page or other failed canonical replay exposes UNAVAILABLE with no
fabricated operations. Provider omission undetectable from supplied pages remains
part of the unresolved history/source-authentication contract, never a promise
of complete on-chain control coverage.

Inherited source≤2MiB, shared128-hash/32MiB read limits, original18 request ceiling
and Linux LP64/local rollback/OFD guard stay intact. Additional diagnostic bounds
are≤1,800 raw occurrences (18 pages×100 rows),≤256 instructions per transaction,
≤16KiB per inspected instruction,≤256 operation rows,≤256 gap rows and≤1MiB
final inventory. Global row/output limit refusal leaves the obligation unavailable
with `CONTROL_PROJECTION_RESOURCE_LIMIT`; individual instruction/container
inspection limits retain explicit unresolved gaps instead of guessing omitted
syntax. No silent truncation, hidden retry or new cache is used. Small identity
indexes point to output rows rather than caching
raw transactions. Unsupported desktop reads remain explicitly unavailable;
only actual Linux read-dependent positives are capability-gated in tests.

`historical_controls.status` stays UNRESOLVED regardless of successful syntax.
Its original `HISTORICAL_CONTROL_SEMANTICS_UNVERIFIED` blocker remains. Every
control/CPI/effect-order/source/ownership/trading approval flag remains false.
Accounting continues to reject unsupported controls independently; a clean T
endpoint cannot erase approve/revoke, freeze/thaw or close/reinitialize attempts.
No parent noninterference, historical deployment/runtime trust, authenticated
finality/completeness, initialized predecessor/S state or current eligibility
obligation is discharged. Token-2022 remains excluded/explicitly unresolved.

## Regression and acceptance limits

Fourteen dedicated tests cover the persisted13-request reference/no writes,
all six principal normalized controls, raw/parsed contradiction, failed account
control attempts with duplicate inner paths, raw close/reinitialization syntax,
wrong mint/frontier/unknown tag, malformed parsed type hiding raw bytes,
conflicting account-query versions, indexed compiled identity, missing account
query/current-head mismatch, forged summary/raw omission/resource exhaustion,
sealed-source refusal and two portable syntax boundary tests. Existing normalizer,
birth, history-control, sealed-source and live-features suites are also run.

The old AST regression forbade every runtime import of the control normalizer.
This dispatch explicitly requests its diagnostic use: the test now permits only
`historical_controls.py` importing only `normalize_legacy_control`, and asserts
that exact exception exists. Every other runtime consumer remains prohibited.
Normalizer/acquisition/decoder semantics and approval gates are unchanged.
Initial focused/full runs hit that old assertion (one failure); the narrowed
exception fixed it. No failures are intentionally accepted.

Remaining dependencies: independent exact-head review and coordinator
integration; trusted obtainable complete history/frontier evidence, supported
historical handler/caller/runtime/deployment semantics, CPI completion/privileges,
lifetime/noninterference and initialization-state equivalence or archived S
bytes. The public legacy birth reference lacks accepted complete request-bound
ownership history/bank; it is not fabricated into an admitted positive fixture.
This unit yields useful local evidence only, with no live provisioning/signoff.

## Validation

Python3.12.14; synthetic/local fixture transports and SQLite only. No provider,
credential, VPS, signing/broadcast, shared queue/readiness edit, merge or deployment.

- Projection/control/live-features target:46 tests in4.131s, OK, zero skips.
- Extended normalizer/accounting/birth/history/sealed target:140 tests in5.898s,
  OK, zero failures/errors/skips.
- Unsupported-platform branch simulation on Linux:46 tests in1.267s, OK with25
  Linux-read skips and21 portable executions; not a real macOS run.
- Full final Linux: `.venv/bin/python -m unittest discover -q`,1,555 tests in
  73.036s, OK; zero failures/errors/skips. The first full run before the precise
  diagnostic import exception had1 failure (1,555 tests in72.309s); final
  validation accepts no known failures.
- `git diff --check`: clean.
