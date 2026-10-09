# Explicit local runtime compatibility transition

This adds one exact reviewed predecessor → successor edge without replacing the
original `metadata.implementation_hash`, config, state, events, outcomes, raw
records or investigation/monitoring budgets. It never treats zero fills as a fresh
ledger. No provider/VPS access or deployment was performed.

All ledger-write, checkpoint-read, cycle-read and dashboard identity checks use
`runtime_compatibility.require_runtime(connection, implementation=code_hash)`.
A native current-code ledger needs no transition. A predecessor ledger requires
an explicit transaction and an exact entry in `config/runtime-compatibility.json`.
That allowlist is outside the hashed `desk/` source domain: hardcoding the successor
inside its own implementation would require a self-referential hash. The policy
is trusted reviewed deployment input, not permission from observation JSON or a
cryptographically authenticated migration certificate. Reader diagnostics retain
the original implementation identity.

The allowlist is deliberately EMPTY until the final worker01 budget + this runtime
source is frozen and independently reviewed. Required production edge:

- predecessor desk hash `15054558d09320369b427a2900028472bf46a870d30e23159b235351df3a72a8`;
- successor: exact final combined `desk/**/*.py,*.json` canonical hash, pending;
- unchanged config digest `1a095e98a3b7a168ffb16573e799179d5a89bf1afc39757020e76a1a6fbb1eb7`.

Worker01 must use the SAME `require_runtime` resolver in monitoring checkpoint
identity checks and bind its allowance-policy transition to the approved effective
runtime without replacing original code/config hashes or resetting counters.
This PR does not edit `job_persistence.py` or `monitoring_budget.py`, and therefore
alone does not activate the budget upgrade. Root owns coordination/final pins.

The explicit coordinator callable/CLI `transition` holds research worker → evidence
invocation → ledger-cycle canonical locks and a ledger `BEGIN IMMEDIATE`. It
requires distinct existing regular singly linked paths, the exact predecessor,
running successor and unchanged canonical config. It validates the saved checkpoint,
quote accounting, bounded complete original event grammar/hashes and outcome links
before any migration DDL. It saves an immutable additive receipt with original
checkpoint/metadata and journal-prefix counts/hashes; original SQL rows remain
untouched. Original journal prefixes must remain identical after later normal
append-only events. One edge only: no implicit chains, adoption, repair or fallback.
Repeated exact invocation is idempotent; malformed/partial/unauthorized records or
failed validation roll back. Budget databases receive no writes.

```sh
python -m desk.runtime_compatibility --research-db EXISTING_RESEARCH.sqlite --evidence-db EXISTING_EVIDENCE.sqlite --ledger-db EXISTING_LEDGER.sqlite --config UNCHANGED_REVIEWED_CONFIG.json --predecessor EXACT_OLD_DESK_HASH --successor EXACT_REVIEWED_NEW_DESK_HASH
```

This is an explicit local coordinator trust contract under cooperative single-host
locks. It does not authenticate economic/provider/ownership/runtime effects or
permit entries. No automatic call is wired into read, init, service or provider
paths. The external reviewed policy must ship with the source checkout; packaged
installations lacking it fail closed for transitioned ledgers.

## Reproducible pinned predecessor fixture

`fixtures/runtime-predecessor-desk.zip` contains only original `desk/` Python/JSON
files from public-to-this-private-repository commit
`b84e20dd3a464be0c5a06f4252b4118a402190e0`, the documented deployed release.
Their canonical hash exactly reproduces the predecessor above. ZIP SHA256:
`e80779bb95e87ff7462709d452e0f1c24e0d3d30c0755d713f483296f55895da`.

Reproduction: `git ls-tree -r --name-only COMMIT desk`; take only `.py`/`.json`,
read exact `git show COMMIT:PATH` bytes; create sorted deflated ZIP entries with
1980-01-01 timestamp and mode 100644. Hash the canonical sorted mapping of paths
relative to `desk/` to decoded UTF-8 source text using the original model digest.
No production DB, private credentials or original live records are included.
Tests execute that actual old checkout in an isolated subprocess to initialize a
SYNTHETIC config/ledger; their test-only allowlist authorizes its synthetic config
and actual current desk hash. They never substitute synthetic config bytes for the
unavailable production config identified only by its digest. Original source is
real; ledger contents, budgets and test authorization are explicit synthetic data.

Validation results and exact frozen successor will be appended after full Linux
suite and independent review. Final runtime acceptance remains pending final source
pin, worker01 interface integration and coordinator explicit transition.

The first full Linux run executed 2,363 tests in289.877s with one regression in
missing-config diagnostic ordering; that ordering is restored before the frozen
rerun. The forward hardening rejects transition-only partial ledgers as nonfresh
and verifies exact immutable trigger bodies, not names alone. Focused restart/
reader/runtime checks passed37tests13.253s; dedicated compatibility checks passed
9tests4.601s including a genuine old-source synthetic partial-position journal
with retained0.689535quantity and cost basis. Final full-suite result follows.

Independent review found two draft defects: later unsupported event grammar could
pass some consumers, and named no-op triggers could undermine a baseline receipt.
Forward repair checks exact table and trigger SQL, validates bounded full retained
suffix event grammar/hash/outcome links before any runtime acknowledgment, and
validates the original checkpoint against copies of its exact original prefix in a
bounded temporary in-memory verifier. It writes no rebuilt state to any ledger.
`Ledger.report` uses the same resolver. Tests reproduce schema99/future suffix,
all three named no-op guards with rehashed empty checkpoint, malformed baseline
with correct guards, and oversized suffix preflight before materialization.
The intermediate full rerun was stopped for these concrete findings; no PASS is
claimed for that abandoned run. Focused repair checks41tests22.280s passed; final
source full-suite validation is pending frozen repair review.
