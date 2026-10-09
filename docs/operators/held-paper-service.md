# Inactive held-position runtime templates

`python -m desk.paper_monitor_service` performs exactly one positions-only pass.
It calls PR147 `export_targets(..., ledger_db=..., cfg=...)` into a fresh private
temporary file, then invokes the reviewed monitoring CLI. It never supplies
candidate input, creates admissions, provisions budgets, resets/retries a failed
pass or enables entry. Every invocation derives exact residual inventory from the
verified original ledger; the downstream cycle revalidates it. Empty positions
return without credentials or a cycle/provider invocation. Temporary targets are
removed after use. Exporter/cycle locks are reused, not bypassed.

Required arguments are config/research-db/evidence-db/ledger-db/pool-fee-bps;
`--systemd-credentials` reuses existing LoadCredential and
`--dependency-blocker CODE` refuses before export/database/credential access.
The fee is a model hypothesis. Exit2 means blocked/unavailable; exit0 is a
completed pass or no positions, never actual execution/entry/continuous freshness.
No live cloud invocation has been performed.

`deploy/desk-paper-held-cycle.service` and `.timer` are inactive templates with no
Install/WantedBy section, no Restart and no automatic enable/start operation.
The service is unprivileged, uses private temporary files, strict filesystem
protection, existing LoadCredential, no public listener and a20-second hard
systemd timeout enclosing the cycle's10-second cooperative deadline. Interruption
can still latch recovery; subsequent invocations must not clear that state.
The five-minute timer is nonpersistent and cannot provide continuous10-second
mark freshness. Durable fixed60/hour allowance and conservative5reads/position
preflight block partial passes below remaining capacity. Original saved mark age
and stale status remain truthful; no new provider polling request is introduced.
Concurrent manual invocations can race the advisory preflight; source budget
reservations and cycle fail-closed latch remain authoritative. No continuous
availability guarantee follows from these artifacts.

Default ExecStart contains RUNTIME_REVIEW_AND_PATH_BINDINGS_REQUIRED, so even an
accidental manual start is blocked before provider access. All example private
paths, the25bps fee hypothesis and the new paper-quote-reviewed config filename
require coordinator verification and an explicit reviewed ExecStart override.
Do not remove the blocker until repaired142 budget,143 consumer,145 cycle,146
CLI and147 exporter pass independent review and are integrated into one accepted
release. PR142's reentry/replacement findings remain worker03's responsibility;
these templates do not repair or authorize the held dependency.

The existing strict watchdog points at active-paper.sqlite with original
config/paper.json and an older release. Preserve that original config. Before a
new quote ledger uses the active-paper path, the coordinator must review a
matching experiment-config and accepted-release WorkingDirectory/ExecStart
watchdog override, or keep the old watchdog disabled. Dashboard and backup path
bindings require the same review. No VPS access, credential inspection, override,
installation, activation, merge, deployment or readiness claim was performed.

Test dependencies are exact1467e95377 and14738bd533, including their unchanged
pending components. A passing fixture suite does not accept those prerequisites.
