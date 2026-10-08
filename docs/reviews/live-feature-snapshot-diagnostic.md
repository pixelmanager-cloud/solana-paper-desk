# Reject-only historical candidate snapshot diagnostic

Source base: `a8141f153339e97652f9dd9108ff1703717e7697` (`integration/cloud-wave1`). PR59 remains review pending. This implementation does not establish live paper readiness and needs independent review before integration.

`desk.live_features.candidate_snapshot(research_db, evidence_db, scan_id, revision_hash=..., now=...)` reads the persisted five-column scan projection using SQLite read-only mode. No report, feature values, approval flags or RPC clients are accepted from the caller. It uses `decision_runner.assess` and the existing `entry_evidence.evaluate` raw replay, including source checks, legacy/SEALED admission contracts, exact selected ownership head, hashes, budget ceiling and historical bank/history reconciliation. All component statuses are suppressed unless that source/revision reconciliation succeeds. No existing module imports this diagnostic; no runtime gate changes.

A successful component means historical local raw replay only. The output is always REJECT, eligibility false, with `LIVE_FEATURE_ADAPTER_NOT_READY`. It contains no market event. Original `observed_at` is preserved; evaluation time cannot refresh it. Missing, malformed, old and future observation times do not attest entry freshness. Even a ten-second-old observation does not establish component freshness or provider authenticity.

Required strategy input fields have explicit null values, units and unknown reasons. Gross top-ten concentration, owner count and slot are exposed separately only if the existing raw holder gate and source/revision gate both verify. Gross concentration cannot populate private concentration. Mint-control and pool gates can be displayed as historical components but never converted into current approval booleans. No raw reserve conversion, price, flow, route, paper quantity, cost or valuation is invented.

The sorted manifest contains content hashes and a canonical manifest hash. Local hashes establish integrity and binding, not remote authenticity or deployed-slot semantics. Source text is limited to 2 MiB, replay to 128 loads and 32 MiB cumulative decoded canonical payloads (individual pages retain the existing 16 MiB bound), and manifest hashes to 128. These are diagnostic limits, separate from the unchanged 18-RPC investigation ceiling. Exceeding a limit fails closed. Component reason codes are filtered and capped at 32; this compact diagnostic is not the complete evidence report. SQLite read-only connections may wait for existing locks (source timeout two seconds; evidence inherits twenty seconds), but create no schema or journal entries.

## Coverage and limitations

Dedicated tests use synthetic isolated databases and the existing unmocked ownership raw-replay fixture, with sockets forbidden during diagnostics. They cover deterministic output/hash, historical component success without approval, source-summary forgery, revision rebinding, broken seals, a ceiling above 18, deleted raw pages, replay load exhaustion, malformed/oversized sources, explicit input types and byte-equivalent database dumps. Existing fixture setup is reused directly, without inheriting its test class or adding duplicate discovery tests.

This implementation does not acquire evidence, repair history, authorize entries, import the engine, write decisions or ledger records, or implement monitoring and exits. Acquisition and obtainable launch/ownership evidence remain Worker07's separate dependency. The diagnostic conservatively requires historical reconciliation even to display otherwise replayable mint/holder components; a partial source returns blocked components. Concurrent mutation of evidence across existing replay reads can cause rejection; this is not a new whole-store atomic snapshot or external current attestation.

Coordinator decisions still needed: required live component freshness policies; private owner/service/developer classification; exact event provenance and route identity; canonical raw-to-human units; public versus hypothetical inventory separation; fee/rent/slippage accounting; and durable entry/exit/restart handoff. The currently listed units describe inputs expected by the existing model, not approved acquisition contracts. No current attestation has been invented.

## Validation

Python 3.12.14, fixture-only Linux checks on the exact source base above plus these three new files:

- `/workspace/work/agent10-venv/bin/python -m unittest tests.test_live_features -q`: 11 tests, 0.447 seconds, PASS, zero skips.
- `/workspace/work/agent10-venv/bin/python -m unittest discover -q`: 1,347 tests, 59.491 seconds, PASS, zero skips.
- `git diff --cached --check`: PASS.

No known test failures. Full-suite network permission was used only for the suite's existing local loopback fixture servers; no live provider calls occurred. Fixture success is not live acceptance. Independent review and coordinator decisions above remain unresolved.
