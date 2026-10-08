# Exact partial-action evidence in the paper engine

Source base: `5d3a306d21c773747b2f28ac43c2561403be02b9` (`integration/cloud-wave1`). This addresses the quantity conflict identified in reviewed PR59. It does not enable live paper admission or establish trusted route evidence.

## Contract

The existing market event `sellability` is the full-current-position valuation proof. A partial take-profit requires a separate `action_sellability` for exactly `min(current_qty, initial_qty * rung_fraction)`. Neither a full-size result nor its proceeds are scaled into an action proof. Whole-position stop, trailing stop, time stop, max hold, danger and operator liquidation continue to use the exact full-position proof, regardless of an irrelevant partial proof. Each subsequent rung needs a fresh event and proofs for its new current inventory/action quantities.

Explicit paired events add `exit_source_hash` and `exit_revision_hash` (64 lowercase hexadecimal characters). Both simulation proofs carry matching `source_hash`, `revision_hash`, and `provenance`. The action also carries `valuation_proof_hash = digest(event['sellability'])`. Distinct quantities require distinct transaction hashes; the same full transaction cannot be relabeled as partial evidence. These bindings are checked in addition to the existing sellability mint/taker, exact Decimal quantity, slot, ten-second freshness, transaction/route hashes, simulation, transaction policy, wallet-account and positive net-proceeds checks. Stored position taker must match the event when using the new contract, including synthetic fixtures.

`manage_position` validates the whole-position proof before marking or triggering a sale. `sell` independently validates both the whole-position valuation and exact action for a partial sale, so direct calls cannot skip the full proof. The full net-proceeds bound caps the valuation; the action's own bound caps actual modeled sale proceeds. Both remain bounded by the constant-product estimate minus the configured fee. No net proceeds are scaled or derived from the other proof.

Example field structure (all hashes and flags below must be produced by a future reviewed trusted adapter, not inferred by a mapper):

```python
observation['sellability'] = full_current_quantity_simulation
observation['action_sellability'] = exact_rung_quantity_simulation
observation['exit_source_hash'] = source_hash
observation['exit_revision_hash'] = revision_hash
# Each proof: source_hash, revision_hash, provenance, existing simulation fields.
# Action only: valuation_proof_hash = digest(full_current_quantity_simulation).
```

A completed partial simulation invalidates the remaining position's exact-size valuation: its modeled remaining mark is retained as an estimate, with `mark_status=UNVERIFIED_EXIT` and `exit_blocked=REMAINING_POSITION_VALUATION_REQUIRED`. No remaining-position proof is synthesized. The next event must bring exact remaining-size valuation evidence. Existing risk logic latches EXIT_ONLY on an unresolved exit and does not automatically resume entry.

Compatibility: legacy explicitly synthetic-model events retain fixture-only partial behavior when both event and position are synthetic. Legacy full-size simulation fixtures remain valid for whole-position exits. A legacy full-size simulation alone continues to block partial fills (now also reporting `EXIT_ACTION_PROOF_MISSING`); the exact-size error remains. Source-bearing full events retain source checks even without an action record. Presence of a malformed/empty action field opts into the paired contract; it cannot silently fall back to a synthetic action.

## Trust limits and preserved gates

The existing `sellability_gate` schema delegates authentic proof production to a future adapter. This change checks local consistency, not raw route authenticity. Self-asserted hash-shaped strings and true simulation/policy flags are not provider acceptance; existing unsigned diagnostic results cannot be promoted into these proofs. Paired positive tests are explicitly synthetic mechanism tests. A future builder must independently replay/bind the actual full route, transaction bytes, authorities, wallet inventory, costs, current source revision and raw-to-human quantity units. That builder and live acquisition remain unresolved dependencies.

`LIVE_FEATURE_ADAPTER_NOT_READY`, entry token and bundle gates, exact-size matching, existing transaction/route/policy checks, price freshness and unavailable-route blocking are preserved. No network, signing, ledger implementation, monitor, view, route coverage, shared queue or readiness edits are made. Applying changed engine code to an old experiment is still rejected by the unchanged ledger implementation hash; use a new fixture experiment, preserving originals.

## Tests

Focused synthetic tests use two genuinely distinct records and quantities without mocking the sellability checker. Coverage includes three paired take-profit rungs and final exit; action-specific proceeds; missing action; full-proof/transaction substitution; one token raw-unit quantity change; position/event/proof taker, mint, source, revision and provenance substitutions; valuation digest rebinding; stale/future/malformed proof times and quantities; the exact ten-second boundary; existing policy/control/hash failures; valuation net cap; full stop/time/max-hold/manual/danger exits; cash, cost-basis, inventory and realized-PnL conservation; restart and duplicate redelivery; immutable collision rejection; and unchanged live admission rejection. Existing ledger crash/restart fixtures are also rerun, without editing ledger code.

Linux Python 3.12.14 results on the exact base above plus this scoped change:

- `python -m unittest tests.test_partial_action_proof -q`: 18 PASS, zero skips, 0.127s.
- `python -m unittest tests.test_partial_action_proof tests.test_cloud_ledger_restart tests.test_security -q`: 35 PASS, zero skips, 2.416s.
- `python -m unittest discover -q`: 1,415 PASS, zero skips, 62.638s.
- `git diff --check`: PASS.
- `python -m desk replay --input fixtures/demo.jsonl --config config/paper.json --db <new isolated fixture database> --report <scratch report>`: three entries, three take-profit fills, two stops, one time stop, zero open positions; legacy synthetic behavior preserved.

No known test failures. Full-suite network permission served only existing local loopback fixture tests; no provider calls. Independent review is required before integration; no fixture result implies live safety or readiness.
