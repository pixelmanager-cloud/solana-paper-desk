# Trusted continuation-to-entry evidence adapter

Agent 05 follow-up, 8 October 2026 (Korea time).
Baseline: `integration/cloud-wave1` at `57145088c6b0d213c7ab0b773bc68a0706428c4d`.
Branch: `codex/cloud-wave1-05-continuation`.

## Behavior

Policy `persisted-entry-evidence-v4` adds `history_snapshot`, a separate historical
accounting component. A successful persisted continuation replay verifies this
component and removes only `HISTORY_TO_CURRENT_SNAPSHOT_NOT_RECONCILED` from the
original transfer gate. The transfer gate remains blocked, including an explicit
fallback blocker if that was its last reason. Existing original launch, account
inventory and transfer diagnostics are preserved. Classification, current-holder
bundle exposure, entry/exit route policy and strategy gates remain blocked;
`eligible_for_trading` remains false and every decision remains `REJECT`.

`assess` resolves the canonical continuation itself; an optional caller progress
object supplies only a reference that must match the canonical head. Its copied
history, timestamps, bank, request count, reconciliation and eligibility flags
are ignored. Persisted summary `history` and `snapshot` approvals are also ignored.

The adapter reads the canonical head, source-bound budget, bank/clock pointers
and per-scan history jobs together in one SQLite read transaction. It then loads
content-addressed records with checksum verification. It validates the exact
completed source scan hash, mint, report hash, original request count, original
report identity, progress identity and the unchanged 18-request ceiling. Each
continued query must match a persisted job for this scan or an original query;
job identifiers and query/coverage bindings are independently checked. Budget
usage must cover original calls, persisted history attempts and at least two
bank/clock requests; the replayed pages must fit the charged usage and 18-page
ceiling. Failures/extra attempts remain charged by the existing worker.

Every continued query is independently replayed from its request-bound raw pages.
Production launch/inventory/movement/order/continuity reconstruction supplies the
history endings. The adapter independently validates the canonical finalized
bank, exact frontier, mint policy and bound getBlockTime response, then reconciles
ending balances, supply, account controls and the exclusive slot cutoff against
that bank. It includes all continued request/page hashes plus head/bank/clock
hashes in the component provenance.

Original `observed_at` is taken from the source investigation and never replaced
by progress or snapshot block time. A stale scan can have verified *historical*
accounting while retaining `INVESTIGATION_NOT_FRESH_FOR_ENTRY` and rejection.
The component scope explicitly distinguishes historical cutoff from current
entry freshness. Source timestamp rewrites cannot reuse the original budget.

## Revisions and concurrency

The consumer keys a decision evaluation to the canonical head actually captured
by the adapter, rather than a separate pre-assessment lookup. A later publication
cannot relabel that evaluation as the newer revision. A well-shaped but invalid
head reference still gets a distinct rejected revision; it does not overwrite
an earlier valid evaluation. Original decisions and older policy evaluations
remain intact. Concurrent consumers serialize their journal transaction and
record one row per scan/policy/revision. No worker lock, budget mutation or
classification/pool implementation was changed.

Raw objects remain immutable content-addressed records under the existing trusted
collector/store boundary. This adapter does not authenticate a provider if an
attacker controls the entire evidence database and all canonical metadata. It
rejects missing/tampered bytes and unbound/rehashed assertions within that trust
boundary. Previously journaled results are historical evaluations, not continuous
integrity or freshness attestations.

## Fixture validation

Python 3.12.14, existing dependency-complete `.venv`.

- `.venv/bin/python -m unittest tests.test_continuation_entry_evidence -q`:
  `Ran 17 tests in 0.606s`, `OK`.
- `.venv/bin/python -m unittest discover -q`:
  `Ran 699 tests in 13.599s`, `OK`, exit 0, no skips reported.
- `git diff --check`: clean.

Full suite used approved sandbox escalation for its existing private-loopback
HTTP test. No live provider calls, VPS, credential inspection, signing or
broadcasting. The positive fixture reuses the synthetic raw legacy launch and
bank construction from `tests/test_ownership_integration.py`; production decode,
request persistence, history reconstruction and reconciliation are unmocked.
Only provider I/O is replaced with synthetic replies. Adversarial coverage includes
forged caller/persisted flags, noncanonical/rehashed progress references,
cross-scan/mint/source rebinding, missing/checksum-corrupt/encoding-corrupt pages,
request manifests and bank/clock records, wrong slots/finality, unbound job/query
cutoffs, missing/wrong/undercharged/over-ceiling budgets, stale observation,
partial-to-complete revisions, prior-policy/original-decision preservation,
concurrent consumers and a head publication during replay.

Development validation initially exposed invalid-zlib exceptions escaping the
original entry replay path; entry evaluation now records raw-evidence blockers
for that corruption. That failure is resolved and covered. An old characterization
test now expects rejection of unpersisted caller progress, while preserving all
other original gates and observation time. No unresolved test failure remains.

## Remaining dependencies

This change connects the existing diagnostic snapshot/history output to one
entry-evidence component only. OWN-1 collector live acceptance, OWN-2 trusted
classification and conservative pool admissions, OWN-3 current-holder exposure,
OWN-4 independent integration review, full entry/exit route policy, trusted
strategy inputs and forward paper verification remain outstanding. This is not
issue #4 signoff or readiness approval. No issue closure, merge or deployment
was performed. Coordinator integration and independent review are required.
