# Cloud worker 03: ownership history adversarial review

Reviewed 8 October 2026 (Korea time), base commit
`e95a8dfcd0536efa5479934f81b872c050d7ed5e`, branch
`codex/cloud-wave1-03`, isolated checkout `/workspace/cloud-wave1-03`.
Read AGENTS.md, docs/readiness.md and docs/agent-task-queue.json.
Only tests, synthetic fixtures and this report are changed.

## Provenance and trust boundary

`fixtures/cloud_history/synthetic.json` is constructed locally. Addresses are
base58 encodings of repeated bytes, signatures are synthetic identifiers, and
account data uses minimal legacy SPL mint/account byte layouts. No provider
responses, private records, credentials or production databases were collected.
The fixture contains explicit provenance and is not a real-chain capture.
Its history summary is deliberately a unit-level input to `reconcile_snapshot`,
not evidence that a live caller may assert completeness. Separate request-bound
tests persist raw synthetic pages and manifests in temporary EvidenceStore files
and independently replay them. Continuity observations are synthetic normalized
inputs to the lifetime checker, not independently decoded chain evidence.

## Attack results and minimal reproductions

All 17 dedicated tests pass. No new production defect was demonstrated within
these attacks; there is no proposed production patch or suppressed failure.
Reproduce every case with:

```sh
PYTHONDONTWRITEBYTECODE=1 /workspace/solana-paper-desk/.venv/bin/python -m unittest tests.test_cloud_history_adversarial -v
```

- The slot-20 snapshot requires range `[0,21)`. Both slot-20 records are included;
  a slot-21 record blocks coverage. A matching timestamp cannot replace that
  exact boundary. Rebinding the cutoff and recomputing the summary checksum
  still fails the saved request manifest binding.
- Reverse the persisted two-page chain and rehash: replay rejects it. Reverse
  provider record slots: replay retains `HISTORY_ORDER_REGRESSION`.
- Repeat a signature on a later page: identical bytes retain
  `HISTORY_DUPLICATE_RECORD`; changed timestamp bytes retain
  `HISTORY_CONFLICTING_RECORD`. Neither grants verified coverage.
- Delete a saved response: complete summary flags cannot recover missing raw
  evidence. Missing snapshot accounts, absent/duplicate ending states and
  closed-account reappearance block reconciliation.
- Change the historical ending to 99 while snapshot amount/supply remain 100:
  `HISTORY_SNAPSHOT_BALANCE_OR_CONTROL_MISMATCH` blocks despite matching supply.
- Put birth and burn in slot 20 with signatures whose lexical order reverses
  their semantic order: both input orders retain
  `HISTORY_SAME_SLOT_ORDER_UNKNOWN`. Query exhaustion alone does not establish
  within-slot execution order. Make a later burn internally balance from 99 to
  90 after an earlier ending of 100: continuity rejects the missing activity.
- Raise SystemExit inside a fake provider after reservation at usage 16:
  reopening retains usage 17; attempted budget reset to zero cannot refund it;
  retry reaches 18 with the same cutoff. A failed request using the final shared
  allowance prevents another account from making any request after restart.
- Nineteen replay queries are rejected by the replay budget boundary.

The process-death test simulates unwinding after the durable reservation; it is
not an OS kill, power-loss or filesystem durability certification. The tests do
not grant common ownership, complete transfer coverage or trading eligibility.

## Exact validation

Python 3.12.14; preinstalled requirements include solders 0.29.0. The existing
environment interpreter was used read-only, with all repository imports and
test working files in the isolated checkout.

- Dedicated command above: 17 tests, 0.034s, OK.
- Full `PYTHONDONTWRITEBYTECODE=1 /workspace/solana-paper-desk/.venv/bin/python -m unittest discover -q`:
  530 tests, 3.115s, OK; zero failures, errors or skips.
- An earlier default-interpreter run lacked solders (491 tests, 109 errors,
  31 skips). A local dependency installation attempt failed at the restricted
  proxy; no dependency or production file was changed.
- The dependency-complete sandbox run had one PermissionError from the existing
  ephemeral 127.0.0.1 dashboard test socket (530 tests, 2.590s). The final full
  run permitted that loopback socket and passed. No provider calls were made.

## Dependencies and coordinator follow-up

This fixture work is review evidence supporting OWN-1 and later OWN-4; it does
not complete either ticket. OWN-1 still requires persisted finalized snapshot
capture/requery integration, immutable cutoff recovery and the coordinator's
bounded live verification. OWN-2 classification remains a dependency for OWN-3
current-holder exposure. OWN-4 follows OWN-3; downstream SELL/PAPER/OPS/QA gates
remain in queue order. Existing tests passing does not establish live ownership
approval, full route policy or end-to-end forward paper readiness.

The coordinator should replay these tests against integrated OWN-1 changes and
add worker-level snapshot crash/requery tests once its API is available. No
VPS access, signer, broadcast, merge, deployment, issue closure, readiness mark
or entry enablement was performed. Original records, fail-closed gates, budgets
and private loopback policy are unchanged.
