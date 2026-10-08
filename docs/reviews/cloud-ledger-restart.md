# Cloud ledger restart review — Agent 08

Scope: fixture-only regression review for [issue #7 / PAPER-2](https://github.com/pixelmanager-cloud/solana-paper-desk/issues/7), branch `codex/cloud-wave1-08`. Production code and runner activation are outside this change. The checkout was clean before work. Instructions reviewed: `AGENTS.md`, `docs/readiness.md`, and `docs/agent-task-queue.json`; issue acceptance criteria are taken from the local queue, not a fresh remote issue fetch.

## Evidence and coverage

All new inputs come from `tests/helpers.py`: invented mint/pool/wallet identities, synthetic reserves, synthetic account bytes and deterministic timestamps. The two simulated-sell-shaped proofs are invented adversarial test objects, not provider responses or evidence of a validated live route. Temporary SQLite files are deleted on cleanup. No production databases, secrets, VPS, provider requests, signing or broadcasting were used. Package installation is the only network work.

`tests/test_cloud_ledger_restart.py` adds nine tests:

- Abrupt child-process termination with `os._exit(73)` at six boundaries: after transition, event insert, first outcome insert, state insert, before COMMIT, and after COMMIT. Each runs for entry, partial sell and final sell: 18 crash scenarios. Before-commit restart recovers the prior report exactly; post-commit redelivery is empty. Retry produces one durable fill and correct event/fill counts. SQLite integrity is checked. These test process crashes, not power loss, storage corruption or distributed execution.
- Nonadjacent reversed replay after a closed trade is idempotent. Changed payload under the same event ID and changed raw observation under the same source ID fail without changing original metadata/events/outcomes/state/raw/health rows.
- Reopen at every ladder rung preserves stage, remaining quantity, proportional entry cost basis and cash. Partial fills consume 30%, 30%, 20% of initial inventory; final exit closes the remaining 20%. Entry cost includes the fixed fee, each sell records that fee, and final cash less initial equity equals realized net PnL.
- A midnight watchdog outage preserves partial inventory, basis, historical mark value/time, cash, PnL and daily loss baseline, while labeling marks stale. Restart and RESUME do not bypass EXIT_ONLY or fabricate an exit.
- Missing route, missing sellability evidence and fee-dominated proceeds retain the partial position and accounting across restart. Fresh synthetic recovery clears the position blocker without automatically resuming entries.
- A full-position sell proof passes the full mark-size check but cannot authorize the partial take-profit quantity. It yields SELL_PROOF_SIZE_MISMATCH, persists the blocked position, and creates no sell fill.
- Simulated net proceeds cap constant-product model proceeds on a full exit. The reported net amount is credited once, losses are recorded, and redelivery after restart does not charge or credit again.
- Configuration and implementation drift fail closed even for a duplicate event, preserving prior records. Implementation drift is injected only into temporary database metadata.
- Cost-budget rejection persists across restart without charging a fee or creating inventory.

## Concrete issue #7 gaps

1. **Partial exits need separate exact-size evidence.** `manage_position` first validates evidence for the whole remaining position, then `sell` validates the partial rung quantity using the same event proof. One exact-size `sell_simulation` proof cannot satisfy both sizes. The new regression proves safe refusal, not a working real partial exit. The coordinator needs a mark/exit evidence design and separate proof for the selected quantity; weakening the quantity gate would be incorrect.
2. **Live cost accounting is incomplete.** Entry uses constant-product quantities and a configured fixed fee; the engine explicitly excludes rent. It has no explicit durable fields/events for incurred failed-attempt fees, rent locked/refunded, or separate priority/provider costs. Blocking an unattempted synthetic exit correctly incurs no fee, but does not verify accounting for an actually incurred diagnostic/attempt cost. Invented net proceeds exercise the cap only; they do not prove live buy cost basis.
3. **Economic replay identity is an adapter responsibility.** Ledger deduplication is by `event_id` and payload hash. These tests establish that contract, not deduplication of the same source observation assigned different IDs or atomic source-cursor advancement with a future automatic runner. Stable immutable source identity and checkpoint integration remain required.
4. **Restart behavior is local and version-bound.** Implementation/config drift requires a new experiment database; no migration or reconciliation path for open positions across a code upgrade is validated. Retrying a committed event returns no outcomes; a future runner must read persisted outcomes/state if it loses the commit acknowledgment.
5. **Stale marks remain historical model amounts.** The watchdog preserves their numeric value while changing status and blocking entries. Consumers must honor STALE/UNVERIFIED_EXIT; this review does not establish a current executable liquidation valuation.
6. **No trusted live entry-to-exit cycle is established.** LIVE_FEATURE_ADAPTER_NOT_READY remains mandatory. Route/proof provenance, automatic orchestration, provider interruption behavior, and forward observation are not established by synthetic ledger tests. No automatic runner was enabled.

## Dependencies and disposition

PAPER-2 remains BACKLOG in the unchanged queue and depends on SELL-1 (#5) and PAPER-1 (#6), which depend on OWN-4 (#4) and its ownership predecessors. This test-only review does not satisfy or bypass those dependencies, grant ownership/route approval, enable entries, close issue #7, or mark paper readiness. Only the coordinator integrates and performs authorized live validation. The 18-RPC ceiling, original records and private loopback access are unchanged.

## Validation

Python 3.12.14. Targeted initial run: `python3.12 -m unittest tests.test_cloud_ledger_restart -q` — 9 tests, OK (2.205s).

An initial full-suite run without declared dependencies ran 485 tests and failed with 109 errors / 31 skips, including missing `solders`. Dependencies were then installed into isolated `work/venv` with `work/venv/bin/python -m pip install -e . -r requirements-live.txt`. Final results are recorded below.

After dependency installation, the restricted-socket run reached 522 tests with one existing loopback HTTP fixture error (`PermissionError` creating `ThreadingHTTPServer(('127.0.0.1', 0), ...)`). The final full run used socket permission for that fixture test and the same unchanged suite command:

```text
work/venv/bin/python -m unittest discover -q
----------------------------------------------------------------------
Ran 522 tests in 4.672s

OK
```

No tests were skipped in the final run. `git diff --check` passed. Setup and test logs remain in ignored `work/`; no environment artifacts are included in the deliverable. Only the assigned test and review document are tracked changes. New tests add no provider client or request path, and the process-crash children receive a minimal environment without inherited credentials. Existing full-suite HTTP activity is synthetic private loopback fixture traffic.
