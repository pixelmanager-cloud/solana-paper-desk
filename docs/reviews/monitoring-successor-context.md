# Explicit monitoring experiment successors

Base: `5cbd0da982c71914241265110d9152d5387ef204`. This maintenance change does not implement token profile 2 or authorize deployment, providers or entry.

`desk.monitoring_successor.plan(research, evidence, old_ledger, new_ledger, pacing, new_cfg, old_source=..., at=...)` is read-only and returns the exact pin; caller holds research-worker, evidence-invocation and all affected ledger locks. Review this output separately. `monitoring_handoff.activate_successor(..., new_cfg, pins=pin)` locks and revalidates before one atomic append. CLI: existing `python -m desk.monitoring_handoff` arguments plus `--successor`. No provisioning or automatic adoption.

The empty-by-default `config/monitoring-successors.json` uses `{"version":1,"successors":[pin]}`. A maximum of four additional edges individually binds sequence, original/parent receipt hashes, source/config/origin hashes, five canonical context paths, time, cutoff, unchanged budget and complete reservation/outcome prefixes, retired ledger metadata/checkpoint/history and independent new INIT ledger anchors. Retirement requires zero open positions; capital is a new experiment, never copied continuation. Pending monitoring/pacing refuses append; latches and all rolling/lifetime charges persist.

Original handoff receipt, schema and guards are unchanged. Existing reservation columns/values are preserved; a nullable `successor_context_hash` column and immutable successor journal are added. Original `context_hash` continues binding the first receipt. The new INSERT fence requires the active successor hash and an ordinal above its cutoff, including INSERT OR REPLACE. Readers verify historical ordinal intervals and current tail. Old eight-column readers and inserts fail closed.

Terminal certificates replay original raw proofs using their original config and exactly pinned retired runtime; retired ledger snapshots remain unchanged. Global accounting, pending transports, pacing and latches are checked in the active context. Only exact certified NULL records are exempt; retired candidates remain rejected. No fresh spending in retired contexts.

Tests use synthetic databases/config deltas and fake retained transports, with the existing genuine archived first-handoff predecessor fixture. No production databases or provider calls. Profile-2 semantics and final composed runtime/config pins belong to separately reviewed dependencies and coordinator deployment.
