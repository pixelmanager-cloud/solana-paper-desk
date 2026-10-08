# Multi-account ownership replay verification

Worker 03 follow-up, 8 October 2026 (Korea time). Reviewed integration base:
`22e1dae7c8f6a1adaca2690c14b13aabc663e729`.
Only this fixture directory and the new dedicated test file are added; no
production modules, other workers' tests, locks, adapters or shared docs change.

## Provenance

`multi_account.json` is synthetic. The pinned Pump create/CreateEvent bytes were
materialized from the existing synthetic launch template in
`tests/test_ownership_integration.py`; the new tests never import that helper.
Additional raw parsed SPL initialization, checked-transfer, close and recreation
records and legacy account bytes were constructed locally. Addresses and
signatures are base58 encodings of repeated bytes. There are no provider
captures, secrets, private databases or claims that these transactions occurred.

The launch creates three token accounts with balances 100, 0, 0. The subsequent
raw transfer ledger is:

| Slot | Operation | A ending | B ending | C ending |
| --- | --- | --- | --- | --- |
| 10 | launch, initialize A/B/C, mint 100 to A | 100 | 0 | 0 |
| 11 | A to B, 40 | 60 | 40 | 0 |
| 12 | B to C, 15 | 60 | 25 | 15 |
| 13 | A to C, 20 | 40 | 25 | 35 |
| 14 | optional C to A, 35 | 75 | 25 | 0 |
| 15 | optional close C | 75 | 25 | absent |
| 16 | optional recreate C with initialization witness | 75 | 25 | 0 |
| 17 | optional A to C, 5 | 70 | 25 | 5 |

Both captured synthetic banks use finalized slot 20, exact supply 100 and
`getBlockTime(20)=110`. The optional lifetime snapshot is independently encoded
to 70/25/5. Closed-account testing asks for C in the bank but returns null and
uses 75/25 for the remaining accounts.

## Actual integration exercised

`tests/test_ownership_multihistory_integration.py` replaces only provider
transport with a deterministic response function. There are no patches to
decode, inventory, reconciliation, replay, storage or worker functions. The raw
mint history and each of three overlapping account histories have two pages:
eight response pages, all content-addressed with real request manifests.
Production decoding, launch-anchor verification, account inventory, duplicate
merging, movement accounting and lifetime continuity reconstruct four unique
transactions. Reopened read-only storage yields exact 40/25/35 snapshot balances
and owner totals. The eight-transaction close/recreate path yields 70/25/5.

The worker path starts with three synthetic initial requests (mint plus two
timestamp-history pages), then captures bank/time and re-queries all eight
slot-bounded pages. Four invocations, capped at three requests each, reach usage
13 of 18. The bank hash stays fixed, source scan bytes stay unchanged, durable
published progress matches replay and an additional invocation makes zero
requests. Separate invocations reopen the production stores; this is restart
coverage, not a power-loss certification.

## Attacks and findings

All supported attacks fail closed:

- Delete an account's second saved response: raw account replay fails and
  snapshot reconciliation retains `ACCOUNT_QUERIES_INCOMPLETE`, even though
  the complete mint history contains the transactions.
- Change a shared transaction's pre-balance in one account query: cross-query
  raw identities conflict and the transaction is quarantined from merged
  history. Duplicate a transfer: `HISTORY_DUPLICATE_RECORD` blocks coverage.
- Remove a recreated account's initialization instruction: continuity reports
  `HISTORY_ACCOUNT_REOPEN_UNWITNESSED`. Remove its earlier close instruction:
  movement accounting and continuity fail instead of inventing closure.
- Change the owner between otherwise individually balanced transfers:
  `HISTORY_ACCOUNT_CONTROL_CHANGED` blocks snapshot reconciliation.
- Place two transfers in slot 12 without saved block evidence: ordering and
  continuity remain unknown. Persist actual synthetic signature-only finalized
  `getBlock` evidence and reverse the within-slot page order: production block
  replay restores the correct positions and balances. Signature strings are
  not used as transaction ordering evidence.

**Normalization gap (not fixed here):** parsed SPL `setAuthority` is not
normalized by `desk.decode.decode` and does not receive
`UNDECODED_TOKEN_INSTRUCTION`. Minimal reproduction is
`test_unsupported_authority_normalization_is_explicit_test_gap`: append a
parsed legacy-token `setAuthority` of type `CloseAccount`, with exact account,
old authority and new authority, to a synthetic transfer. Decoding returns the
same transfer list, no authority-change inventory and no undecoded-token warning.
This test characterizes unsupported normalization; it does not fabricate a
control event, accept a changed-authority bank or assert full control evidence.
The coordinator should assign a separate decoder/policy follow-up before
claiming historical authority-operation coverage. A normalization fix should
replace these characterization assertions with the new event/blocker behavior.
Current snapshot control checks and false eligibility/common-control outputs
remain separate and unchanged.

## Exact results and reproduction

Python 3.12.14, preinstalled solders 0.29.0. The dependency-complete interpreter
is used read-only; imports and temporary databases belong to this isolated
checkout. Commands run from `/workspace/cloud-history-integration`:

```sh
PYTHONDONTWRITEBYTECODE=1 /workspace/solana-paper-desk/.venv/bin/python -m unittest tests.test_ownership_multihistory_integration -q
PYTHONDONTWRITEBYTECODE=1 /workspace/solana-paper-desk/.venv/bin/python -m unittest discover -q
```

- Dedicated suite: 13 tests in 0.528s, OK.
- Full suite: 700 tests in 15.274s, OK, zero failures/errors/skips.
- Full suite allowed its existing ephemeral private loopback dashboard socket.
  No provider/VPS calls, signing, broadcasting, merge or deployment occurred.

## Remaining dependencies and limits

This prepares OWN-1/OWN-4 verification; it is synthetic accounting evidence and
does not close live acceptance, prove common ownership or enable entries.
Production still reports `transfer_history_complete=false` and
`eligible_for_trading=false`. Legacy live capture/verification belongs to the
coordinator. Worker01 continuation locks and worker05 trusted adapter were not
modified. Service classification, quantified current-holder distribution
exposure, complete route policy and forward paper readiness remain separate
dependencies. Latest integration changes after the recorded base need another
combined run before coordinator integration.
