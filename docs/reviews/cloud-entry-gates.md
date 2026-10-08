# Cloud entry gates: Agent 05 preliminary adversarial review

Reviewed 8 October 2026 (Korea time). Baseline commit:
`e95a8dfcd0536efa5479934f81b872c050d7ed5e`.
Branch: `codex/cloud-wave1-05`. Scope: current `desk/entry_evidence.py`
and `desk/decision_runner.py`; changes confined to this review and
`tests/test_cloud_entry_gates.py`. No production edits.

## Verdict and limits

No tested attack changed the final reject-only result. `evaluate` returns
`eligible_for_trading=False`; `assess` returns `REJECT`; the consumer leaves
`automatic_entry_enabled=False`. Bundle exposure, full entry/exit route policy,
and strategy inputs remain mandatory blockers. A verified component is not
ownership approval or current entry permission.

This is preliminary review, **not issue #4 / OWN-4 signoff**. OWN-4 depends on
OWN-3, which depends on OWN-1 finalized snapshot integration and OWN-2
classification. The later integrated snapshot/classification/exposure state
requires independent re-review. No ownership milestone or paper readiness is
established here. No live verification was performed.

## Reproduced findings

### F1: report timestamp can refresh the freshness label

Reproduction: `test_rehashing_timestamp_refreshes_freshness_label_but_never_entry`.
Evaluate the unchanged public holder capture with `observed_at=100, now=1000`;
`INVESTIGATION_NOT_FRESH_FOR_ENTRY` is present. Change only `observed_at` to 1000
and recompute `report_hash`: that reason disappears, with exactly the same
component gates and raw evidence hashes. `assess` trusts the report timestamp;
it does not bind freshness to an immutable collection timestamp or the raw
snapshot slot. `test_old_public_snapshot_is_verified_component_without_age_binding`
confirms old stored captures can still be `VERIFIED_COMPONENT`.

Impact: freshness-label bypass in direct assessment, **not entry authorization**.
Before any future entry path, independently bind collection time, snapshot slot
and current-time/slot freshness; a checksum alone cannot authenticate a timestamp.
The 10-second inclusive boundary is tested: 90 is fresh at 100; 89, 101, missing,
boolean and string timestamps are stale/invalid.

Mitigations verified: persisted consumer evaluations are not refreshed by a
source-only timestamp rewrite for the same policy/revision; original source
payload and rejection remain intact. Continuation preserves the original
observation time and original gates even when progress contains a new timestamp.

### F2: content addressing provides integrity, not provider authenticity

Reproduction: `test_new_checksum_does_not_authenticate_synthetic_provider_bytes`.
Change one holder wallet's 32-byte owner field in a copy of the public capture,
retain valid layout and balances, save under its newly computed hash and update
the report reference/checksum. Holder replay returns `VERIFIED_COMPONENT`.
Tampering under the *original* hash is rejected by `EvidenceStore.load`.

Impact: an actor able to inject mutually consistent evidence into the trusted
store can fabricate a component; this is a storage/ingestion trust boundary,
not a demonstrated remote exploit. All final entry blockers remain. Future
integration must define and enforce trusted collection provenance and bind
reports to that provenance; rehashing untrusted JSON is insufficient.

### F3: separate mint response and holder bank are not a unified snapshot

Reproduction: `test_inconsistent_separate_mint_and_snapshot_controls_block_entry`.
Introduce a mint authority in the separate saved mint response, leaving the
holder snapshot's mint unchanged, and rehash both reference and report.
`token_controls` is `BLOCKED` while `holder_snapshot` remains
`VERIFIED_COMPONENT`; the overall decision remains blocked. This demonstrates
component independence rather than a successful bypass. The integration review
must verify a shared snapshot/cutoff and temporal consistency rather than
combining independently verified components as a single point-in-time approval.

## Attack coverage and evidence

All captures are existing committed public fixtures:
`fixtures/mainnet-holder-snapshot.json` and `fixtures/mainnet-launch.json`.
Mutations are synthetic local copies in temporary SQLite stores; no capture is
edited. Tests use real EvidenceStore loading, entry evaluation, history replay,
assessment and consumer persistence. Continuation is supplied directly to
`assess` to test its boundary; this is not a continuation collector integration
or provenance-validation test. The consumer reproduction forbids socket creation.

| Attempt | Observed result |
| --- | --- |
| Assert complete bundle/transfer/simulation/strategy approval | Mandatory final blockers retained |
| Missing, invalid or nonexistent raw references | Raw-evidence gates blocked; holder metrics absent |
| Change compressed payload under original key | Checksum mismatch blocks raw evidence |
| Cross-mint report/request/scan or holder account bytes | Binding/identity gates blocked |
| Holder drift beyond 32 slots or duplicate account keys | Holder component blocked |
| Missing/future/ill-typed timestamp | Freshness reason retained |
| Rehash changed timestamp or forged consistent bytes | F1/F2 component-label limits; final entry blocked |
| Assert history completeness with newly rehashed coverage | Request-bound history replay blocked |
| Rebind query range, remove manifest/page or reorder cursors | Request-bound history replay blocked |
| Replay launch history for another mint | Mint-query coverage blocked |
| Supply 19 replay queries | Replay blocked at the unchanged 18-request ceiling |
| Supply continuation with fresh timestamp | Original observation/gates retained; stale entry rejected |
| Rewrite consumed source timestamp | No duplicate evaluation; original journal preserved |

No VPS, credentials, live providers, signers or broadcasting were accessed.
Original records, budget code and private loopback behavior were unchanged.
No entries, integration, deployment, issue closure or shared coordination-file
changes were performed.

## Exact validation and coordinator handoff

Python: `.venv/bin/python --version` -> `Python 3.12.14`.

- `.venv/bin/python -m unittest tests.test_cloud_entry_gates -q`:
  `Ran 16 tests in 0.177s`, `OK`.
- `.venv/bin/python -m unittest discover -q` with ordinary sandbox:
  `Ran 529 tests in 1.894s`, `FAILED (errors=1)`. Existing
  `tests.test_research.DashboardHTTPTests.test_rebinding_csrf_and_readonly_status`
  could not create its private loopback socket (`PermissionError: Operation not permitted`).
- Same full command with approved sandbox escalation for that loopback test:
  `Ran 529 tests in 2.537s`, `OK` (exit 0; no skips reported).
- Initial use of system `python3.12` lacked `solders` and produced 16 setup errors;
  the preinstalled Python 3.12 virtual environment supplies the required dependencies.

The new tests deliberately assert current limitations so they stay executable;
a future fix to F1/F2 should update those expectations to require the stronger
boundary. They must not be read as approval of those limitations.

Only the coordinator integrates. Re-review the actual integrated OWN-1/OWN-2/
OWN-3 snapshot, classification provenance/expiry, exact address binding,
current-holder exposure and completeness; repeat timestamp/rehash/cross-mint
attacks against that revision. Fixture success does not satisfy live integration
verification, full route policy or end-to-end forward paper readiness.
