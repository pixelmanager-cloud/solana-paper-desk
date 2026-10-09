# Solana paper desk development

Read docs/readiness.md and the assigned issue before editing. This is an unfinished paper-only tool. The current priority is a working live-data paper entry, monitoring, exit, accounting and restart loop. Freeze further screening architecture expansion unless a concrete defect blocks that loop. Follow the coordinator claims in docs/agent-task-queue.json to avoid duplicate assignments. The user permits unresolved ownership history for an explicitly configured paper experiment with visible risk flags; retain unknown values and reject known hazards. The user also explicitly approved fresh live quote-based simulated paper fills with recorded fee/slippage assumptions and an EXECUTION_UNVERIFIED label. Implement that as an explicit versioned experiment mode; it does not authorize signing, broadcasting, real funds, fabricated source evidence, or silent changes to existing strict experiments.

Use Python 3.12. Install with `python -m pip install -e . -r requirements-live.txt`; run `python -m unittest discover -q`. Tests use fixtures and must not require provider credentials or live RPC.

Work on one assigned issue in an isolated branch. Keep changes within its scope, identify dependencies and include adversarial tests. Do not mark an issue ready merely because tests pass: satisfy its acceptance criteria and state remaining limitations. Return a reviewable diff or pull request. Only the coordinator integrates and deploys.

Never add signing, transaction broadcasting, real-money execution or subscription purchases. Never weaken an evidence gate to create entries. Required missing, stale or contradictory evidence must fail closed. The explicit experimental ownership-history exception above is permitted; it must never turn corrupt supplied evidence into an unknown-history waiver. Preserve original records, historical decisions, request budgets and private loopback access.

Do not access the production VPS, copy credentials, publish databases, or make provider calls from cloud workers. Use public or synthetic fixtures with provenance. Live verification and deployment belong to the coordinator using existing authorized access. Do not claim common ownership from shared funding alone.

Keep secrets out of Git, logs and artifacts. Report test results, evidence provenance and implementation limitations. End-to-end paper readiness requires a verified entry/monitor/exit/accounting/restart path and forward observation, not a test-count target.
