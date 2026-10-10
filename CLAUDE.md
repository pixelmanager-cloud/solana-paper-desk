# Claude Code guide

@AGENTS.md

## Handoff from Codex (2026-10-10)

The Codex handoff documents (`CLAUDE_CODE_HANDOFF.md`, `FINAL_STATUS.md`, `GITHUB_CLEANUP.md`, `support/`) are not in the repository; ask the user for them before production-facing work. Older status in `docs/readiness.md` and `docs/agent-task-queue.json` is historical; the handoff supersedes it.

- `integration/cloud-wave1` (default): deployed baseline `0dcc2117b094bfced0e92feebac9f2df47cfd1d4`.
- `integration/empty-history-ready` (PR #209): reviewed, undeployed candidate `c1e22c289780a0b068190d4938847c0858d0a465`.
- `codex/cloud-06-quote-alternative` (PR #182): deferred PumpSwap estimate fallback; not for the current experiment.
- `main` is stale.

No live-data BUY has occurred yet. The goal is one verified entry → monitoring → exit → accounting → cold restart cycle, then versioned strategy experiments judged on forward paper results.

## Commands

- Setup: `python3.12 -m venv .venv && .venv/bin/python -m pip install -e . -r requirements-live.txt` (the SessionStart hook does this in cloud sessions).
- Full suite: `python -m unittest discover -q` (about 25 minutes, ~2,900 tests; prefer targeted modules such as `python -m unittest -q tests.test_quote_collector_boundary`).
- CLI: `python -m desk`.

## Cloud-session limits

Cloud sessions have no VPS access, provider keys or production databases. Use fixtures only; live verification and deployment are done by the user from their machine.
