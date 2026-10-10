# solana-paper-desk cloud task queue

The coordinator (the Mac session) maintains this file; workers only read it. Follow `WORKER_PROMPT.md` on this branch.

## Global facts for every task

- **BASE** for every round-1 task is tag `r1-base`, which equals `integration/empty-history-ready` at `c1e22c289780a0b068190d4938847c0858d0a465` (PR #209, the reviewed but undeployed candidate). The deployed production baseline is `integration/cloud-wave1` at `0dcc2117`.
- **Hash domain.** `desk/runtime_compatibility.implementation_hash()` hashes every `.py` and `.json` file under `desk/`. Editing anything in `desk/` changes the runtime identity and forces a coordinator-made runtime successor pin at deploy time.
  - Do **not** create or edit runtime successor/transition pins (`config/runtime-*.json`, `desk/runtime_*successor*.py`) yourself. The coordinator composes one successor per release.
  - Put new operator tooling in `tools/ops/` (outside the hash domain) unless the task says otherwise. `tools/` is a namespace package (no `__init__.py`); `python -m tools.paper_scheduler` works from the repo root. Keep `tools/ops/` and `tools/research/` as namespace packages too: do NOT add `__init__.py` files, so parallel branches never conflict on them.
- **Production layout** (for realistic defaults; never access it):
  - Stores: `/var/lib/solana-desk/` holds `research.sqlite`, `evidence.sqlite`, `active-paper.sqlite`, `token2022-paper.sqlite`, `token2022-boost-00b7c302.sqlite`, `paper-decisions.sqlite`, `launches.sqlite`, `raw.sqlite`, `provider-pacing.sqlite`, `discovery/continuous.sqlite`, `entry-dispatch/dispatch.sqlite` and the active ledger `paper-kraken-77de75a2.sqlite`.
  - Config `/etc/solana-paper/paper-kraken-reviewed.json`; releases `/opt/solana-desk-releases/<sha>`; venv `/opt/solana-desk/.venv`; backups `/var/backups/solana-desk/`.
  - Units are `desk-*` systemd services and timers. Releases are switched with `60-reviewed-release.conf` drop-ins that set WorkingDirectory; see `docs/deployment.md`.
- **Read-only SQLite** means `sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)`. Note that `MonitoringBudget.__init__` can commit, so read-only tools must read its tables directly instead (`paper_monitoring_budget`, `paper_monitoring_reservations`, `paper_monitoring_outcomes` in evidence.sqlite).
- Reference helpers (release-specific and historical, but useful patterns) are in `docs/handoff/support/*.py` on branch `claude/pensive-clarke-75dfwj`: `git show origin/claude/pensive-clarke-75dfwj:docs/handoff/support/<file>`.

---

## T01 — Generic terminal outcome for normal rejections (THE blocker)
STATUS: OPEN
DEPENDS: none
BASE: r1-base
OWNS: desk/paper_cycle.py, desk/paper_terminal_reconciliation.py, desk/history_preparation_rejection.py, desk/paper_history_preparation.py, desk/paper_observe_cli.py, a new desk/paper_<generic>_no_entry.py, a new config/*.json policy if needed, and the related tests
AVOID: tools/ops/**

Note: a separate cloud session may already be working on this from the handoff prompt. If that is you, claim this ID and move your work onto `cloud/T01`.

**Problem.** `paper_observation_passes.outcome_hash` is inserted as NULL and filled only on COMPLETE (`desk/paper_cycle.py` ~:531/:678). Every charged non-COMPLETE pass stays NULL. That covers each `CycleBlocked` (e.g. `MARKET_PRODUCER_BLOCKED` from `fulfill_quotes` when `event_builder` returns None, `OBSERVATIONS_INCOMPLETE`, `USD_ORIGINAL_BINDING_INVALID`, budget exhaustion), plus `ObservationError` and `RecoveryRequired`. The global gate `paper_terminal_reconciliation._gate` (~:544-583) then returns `OBSERVATION_RECOVERY_REQUIRED` forever. `terminal.certify_intrinsic` only fires for mint/pool policy hazards, so hazard-less typed blockers can never self-clear. Each occurrence so far needed a bespoke reconciliation module, policy pin, migration and deploy. The same latch exists in `paper_history_preparation.prepare` (~:71-82) and `paper_observe_cli.observe` (~:266-270).

**Do:**
1. **Prospective fix.** A normal, typed, *verified* no-entry rejection must write a terminal, replay-verifiable outcome in the same transaction that records the result. Template: `history_preparation_rejection.publish/verify/gate` (typed outcome kind with a two-way inventory, re-proved on each gate). Classify blockers into:
   - (a) verified normal rejections (no entry, charges retained) → terminal outcome;
   - (b) integrity/recovery-required conditions → stay NULL and fail closed, as today.

   Charged requests stay charged. No retry of the same scan/candidate.
2. **Retrospective fix.** One *generic* append-only receipt kind that `_gate` accepts for already-retained NULL passes whose recorded result proves a category-(a) rejection. Its proof is parameterised by the pass shape (blocker codes, attempts, journal binding), not a per-incident module. It must be immutable (trigger-guarded, like the existing receipt tables), bounded, and unique per pass. It must reject anything that does not prove a normal rejection.
3. Keep every existing receipt kind and its proofs working unchanged; existing production rows must still verify.
4. **Tests (fail-first):**
   - The installed-lifecycle scenario: an empty-window `MARKET_PRODUCER_BLOCKED` pass on a fresh store must not block the next candidate.
   - Adversarial: a forged receipt, a mismatched intent hash, a category-(b) blocker presented as (a), a duplicate receipt, an updated or deleted original row.
   - The existing modules `test_paper_terminal_reconciliation`, `test_empty_history_no_entry`, `test_history_preparation_phase` and `test_paper_entry_dispatcher` must still pass.
   - Run `test_empty_history_successor_lifecycle` once at the end.
5. In the report, list every `CycleBlocked`/exception code and which category it falls into, and the exact migration step the coordinator must run to append retrospective receipts for production's retained NULL pass(es).

---

## T02 — Read-only production status CLI
STATUS: OPEN
DEPENDS: none
BASE: r1-base
OWNS: tools/ops/status.py, tests/test_ops_status.py
AVOID: desk/**

Build `python -m tools.ops.status --data /var/lib/solana-desk --ledger <path> --config <path> [--release-dir <dir>] [--systemd]`. It prints one JSON report and must **never write**: every DB is opened `mode=ro`, nothing outside stdout is touched, and there is no network.

The report covers:
- **Release identity:** runtime digest of `--release-dir` (call `implementation_hash` in a subprocess with cwd set to that dir, `python -I`).
- **Ledger:** table counts, event/outcome counts, positions (mint, qty, entry), cash, realized PnL, mode, last_ts, the fill list labels (`EXECUTION_UNVERIFIED`), config_hash and implementation_hash from metadata, and whether the file config equals the stored config (parsed comparison; report both the raw file sha256 and the canonical digest).
- **Observation passes:** counts of NULL vs resolved `paper_observation_passes`, with the NULL ids (bounded).
- **Dispatcher journal:** last N intents/results with codes.
- **Budgets:** monitoring budget/reservations (raw table reads), ownership budgets, provider pacing state (`next_at`, `blocked_until`, `high_water`, `pending`, waiters count).
- **Store inventory:** name, size, mtime and integrity of each `*.sqlite`. Use `PRAGMA quick_check` only behind a `--check` flag.
- **With `--systemd`:** for each `desk-*` unit, ActiveState, UnitFileState, Result, WorkingDirectory and drop-ins (via `systemctl show`; injectable runner for tests).
- **Dashboard listener:** if `ss` is available, flag any non-loopback bind of :8765.
- A `blockers` list summarising anything that would stop the next entry: NULL passes, held position, mode≠RUNNING, budget exhausted, pacing blocked.

Tests: build fixture stores with the repo's own schema creators (prefer existing test helpers over hand-written DDL) and a fake systemctl runner. Prove the tool fails closed on a symlinked/aliased DB path and that it does not modify file bytes or mtimes (hash before and after).

---

## T03 — Generic quiesced backup + manifest verifier
STATUS: OPEN
DEPENDS: none
BASE: r1-base
OWNS: tools/ops/backup.py, tools/ops/verify_backup.py, tests/test_ops_backup.py
AVOID: desk/** (`desk/backup.py` may be imported, not edited)

Generalise `docs/handoff/support/backup-dispatch-recovery.py` (see branch `claude/pensive-clarke-75dfwj`) into `python -m tools.ops.backup --data DIR --destination /var/backups/solana-desk/<name> --label <text> [--require-quiesced unit ...] [--expect-count N]`.

- Discover every `*.sqlite` under `--data` (recursive, including `discovery/` and `entry-dispatch/`), skipping `-wal`/`-shm`/locks.
- Validate all sources first: regular file, single link, canonical (no symlink), distinct inode. Refuse if any listed unit's ActiveState is not inactive/failed (injectable systemctl runner).
- Copy with the online `sqlite3` backup API from a `mode=ro` source, set `journal_mode=DELETE` on the copy, run `integrity_check`, chmod 600, directory 700. The destination must not already exist, and its parent must be canonical.
- Write `manifest.json` with: label, created UTC, `implementation_hash` of the running code, per-DB sha256, size, table list with row counts, and schema sha. With `--expect-count`, fail if the count differs.
- `python -m tools.ops.verify_backup <dir>` re-hashes everything and integrity-checks it against the manifest; it exits non-zero on any mismatch.

Tests use temp dirs and fixture DBs. Cover: an alias/symlink/hardlink refused, a non-quiesced unit refused, the destination already existing, a corrupted copy detected by the verifier, a WAL-mode source backed up consistently while a writer holds a transaction, and source bytes unchanged.

---

## T04 — Generic original-preservation verifier
STATUS: OPEN
DEPENDS: none
BASE: r1-base
OWNS: tools/ops/verify_preservation.py, tests/test_ops_preservation.py
AVOID: desk/**

Generalise `verify-empty-successor-originals.py` (handoff support) into `python -m tools.ops.verify_preservation --before <backup dir> --after <live data dir> --allow <spec.json>`.

For every DB in the backup manifest:
- every original table still exists with an identical or superset schema (no column type/order changes, and no dropped constraints or triggers);
- every original row is still present with the same rowid and identical values *and SQLite types*.

New rows or tables are allowed only where the spec permits. The spec format is JSON, for example `{"evidence.sqlite": {"new_tables": ["x"], "append_only": {"paper_observation_passes": {"may_set_null_columns": []}}}}`. Default deny: any changed original value, including NULL→value, fails unless the spec explicitly lists that column/table. Output is a JSON summary with per-DB counts. It is read-only on both sides.

Tests cover: an identical pass, an appended row allowed, an appended row in a non-allowed table failing, a changed value failing, a type change failing (`1` vs `'1'`), a deleted row failing, a rowid shift failing, a dropped trigger failing, a symlinked root refused, and the bounded memory approach (streaming ordered comparison, no full-table loading — test with ~200k rows).

---

## T05 — Dry-run preflight harness against store copies
STATUS: OPEN
DEPENDS: none
BASE: r1-base
OWNS: tools/ops/preflight_dryrun.py, tests/test_ops_preflight_dryrun.py
AVOID: desk/**, tools/paper_entry_dispatcher.py

The coordinator must be able to reproduce the exact production entry gate **without touching live stores or providers**.

`python -m tools.ops.preflight_dryrun --data DIR --ledger NAME --config PATH --release-dir DIR --workdir /tmp/x`:
1. Copy all stores into `--workdir` using the sqlite backup API (read-only sources).
2. Run, against the copies only, in a subprocess with cwd set to `--release-dir`:
   - the terminal gate (`desk.paper_terminal_reconciliation.gate`);
   - the dispatcher `--plan`/`_preflight` path;
   - the scheduler entry pre-check.

   Set `DESK_PROVIDER_PACING_DB` to the *copy*, and make sure the dispatcher's `--pacing-db` points at the same copy (`paper_entry_dispatcher.py` ~:322). Block outbound network in the subprocess: refuse to run if provider key env/credentials paths are set, and monkeypatch socket connect to raise in a small bootstrap.
3. Print JSON: gate result codes, pending NULL pass ids, preflight refusal reason, whether `fresh != ctx` context mismatch occurred, and timing (the handoff flags replay cost).
4. Delete the workdir unless `--keep`.

Tests: a fixture store set where the gate passes; one with a retained NULL pass → `OBSERVATION_RECOVERY_REQUIRED`; one with a held position → refusal; proof that live fixture files are byte-identical afterwards; proof that a socket connection attempt fails the run.

---

## T06 — Paper round-trip + cold-restart verifier
STATUS: OPEN
DEPENDS: none
BASE: r1-base
OWNS: tools/ops/verify_cycle.py, tests/test_ops_verify_cycle.py
AVOID: desk/**

Generalise `read-completed-paper-cycle.py` (handoff support) with all paths as arguments, and add restart comparison:
- `snapshot --data --ledger --config --out snap.json`. Read-only. Record: the checkpoint state digest, `last_ts`, the fill list with identities, event/outcome counts and max seq, monitoring budget used/reserved, ownership budget used, the pacing high_water, and the dispatcher journal counts. Validate the round-trip exactly as the support script does, reporting status `NO_FILLS` | `OPEN_POSITION` | `VALIDATED_LIVE_DATA_PAPER_ROUND_TRIP`. For an open position, report the entry token, time, size, price and the `EXECUTION_UNVERIFIED` label.
- `compare snapA.json snapB.json`, used across a cold service restart (no new activity expected). Fail if any fill, checkpoint or budget differs, or a duplicate fill/charge appears. With `--allow-progress`, allow only append-only growth, and verify the old prefix is identical.

Tests: use the existing synthetic ledger/lifecycle fixtures (see `tests/test_cloud_ledger_restart.py`, `tests/test_paper_restart_integration.py`, `tests/test_empty_history_successor_lifecycle.py`) to build buy → held → exit ledgers. Cover: a duplicate fill detected, a cash mismatch detected, a sell before buy rejected, and a reopened-db cold restart compare passing.

---

## T07 — First-BUY entry latch (stop new entries after the first verified BUY)
STATUS: OPEN
DEPENDS: none
BASE: r1-base
OWNS: tools/ops/entry_latch.py, tests/test_ops_entry_latch.py
AVOID: desk/**

For the controlled first cycle, the coordinator wants new admissions to stop once the first live-data BUY is persisted, while held monitoring continues to a real exit. The engine already has `PAUSE_ENTRY` → mode `ENTRY_PAUSED` via `desk/paper_cycle_cli.py --control` (`engine.py` ~:362). The scheduler and dispatcher already refuse entry while a position is open, but entries would resume after the exit.

Build `python -m tools.ops.entry_latch --ledger --config ... [--apply]`:
- Read-only by default. Report whether the ledger has ≥1 BUY fill and the current mode.
- With `--apply`, if there is a BUY and the mode is RUNNING or EXIT_ONLY-compatible, issue exactly one `PAUSE_ENTRY` control through the existing supported CLI/API (no direct state writes). It must be idempotent: a second run is a no-op.
- It must be suitable as an `ExecStartPost=` of the entry service: exit 0 when there is nothing to do, and never retry provider work.

Tests: no BUY → no-op; a BUY → paused and held monitoring still allowed (prove by running the held path in a fixture); idempotency; a held position with exit_blocked still pausing.

Also write `deploy/drop-ins/entry-latch.conf.example` (an example only; do not change existing units).

---

## T08 — Held-exit robustness tests (and fixes outside the T01 files)
STATUS: OPEN
DEPENDS: none
BASE: r1-base
OWNS: tests/test_held_exit_robustness.py; fixes allowed in desk/engine.py, desk/paper_monitor_service.py, desk/monitoring_budget.py
AVOID: the T01 OWNS files (desk/paper_cycle.py, desk/paper_terminal_reconciliation.py, etc.)

The first real cycle depends on the exit path. Exit-quote failures currently set `exit_blocked='EXACT_FRESH_SELL_QUOTE_REQUIRED'` → mode `EXIT_ONLY` → `run_once` raises `UNRESOLVED_POSITION_EXIT`. Write fixture tests that drive a synthetic open position through:
1. A transient provider error on the exit quote, followed by a good quote on the next held pass. The exit must complete with no duplicated charge and correct fee-adjusted PnL.
2. A stale quote, then a fresh one.
3. A stop/time-stop trigger while the quote source is down for several passes. The budget must be charged per real attempt and stay within the shared allowance.
4. A held pass after a cold restart (reopened stores) mid-blocked-exit.
5. Does the observation pass from a failed exit attempt leave a NULL outcome that blocks *future entries* after the exit? Document the answer.

For each case, decide whether the system recovers on its own or wedges permanently. If it wedges and the fix lies outside the T01 files, fix it (fail-first). If the fix needs the T01 files, leave a RED test marked `expectedFailure` with a precise description in the report, so the coordinator can route it.

---

## T09 — Harsh first-cycle readiness audit of PR #209
STATUS: OPEN
DEPENDS: none
BASE: r1-base
OWNS: reports/T09.md, tests/test_audit_first_cycle_*.py (new tests only)
AVOID: every non-test source file

Act as a harsh critic. Assume the coordinator deploys `r1-base` plus the T01 fix and starts entries. Find every way the next 48 hours fail *without* a code bug in the classic sense: another one-off reconciliation needed, a gate latch, budget exhaustion, a pacing deadlock, a service wall timeout (entry 600s, held 120s, MemoryMax ~384MiB), retained-history replay cost growth, a lock left behind after SIGKILL, a timer cadence overlap, or a checkpoint/config pin mismatch after a config change.

For each finding, give severity, a concrete scenario, the evidence (file:line), and ideally a small test that demonstrates it (RED is fine, mark it `expectedFailure`). Rank them. No source edits. Be specific, not balanced; the coordinator wants the real risks.

---

## T10 — Forward-evaluation report for versioned strategy experiments
STATUS: OPEN
DEPENDS: none
BASE: r1-base
OWNS: tools/research/forward_eval.py, tests/test_forward_eval.py
AVOID: desk/**

This prepares step 4 (strategy tuning on forward paper data). `python -m tools.research.forward_eval --ledger L1 [--ledger L2 ...] [--holdout-from <utc>]` is read-only. Per experiment (config version + config_hash from ledger metadata), report:
- trades, wins and losses, net PnL in SOL after fees/slippage, mean and median return per trade, hold time, exit-reason breakdown (STOP/TRAILING/TIME/TAKE_PROFIT/…), max drawdown;
- MFE/MAE per trade where held marks exist;
- a bootstrap CI for the mean return;
- a train vs holdout split by time.

Never mix experiments with different config hashes in one row. Label everything `EXECUTION_UNVERIFIED` and paper-only. With fewer than 30 trades, print an explicit "insufficient sample" warning instead of hiding it. Reuse `desk/experiment_report.py` readers where possible (import, don't edit).

Tests: synthetic multi-experiment ledgers with known answers, experiment mixing refused, an empty ledger, and only open positions.

---

## T11 — Generic release staging + cutover tool (dry-run first)
STATUS: OPEN
DEPENDS: none
BASE: r1-base
OWNS: tools/ops/cutover.py, tests/test_ops_cutover.py, docs/ops/RUNBOOK.md
AVOID: desk/**, deploy/*.service, deploy/*.timer

Replace the per-release `deploy-*`/`start-*` helpers (handoff support) with one parameterised tool. Every mutation goes through an injectable runner, and the default mode is `--dry-run`, which prints the exact commands.
- `stage --tar X --sha256 H --release-root /opt/solana-desk-releases`: verify the sha, extract to `<root>/<commit>` safely (no absolute paths, no `..`, no symlinks escaping), then compute and print the runtime digest.
- `cutover --release <dir> --units u1 u2 ... --keep-off desk-paper-entry-dispatcher.timer`: write the `60-reviewed-release.conf` drop-ins (WorkingDirectory=<release>), run `daemon-reload`, start only the listed units, and never start `--keep-off` units. Then verify each unit's effective WorkingDirectory and ActiveState. On any failure, stop every unit the tool started and leave drop-ins recorded for rollback.
- `rollback --units ...`: remove the drop-ins the tool wrote (only those, identified by a marker comment), then daemon-reload.
- Refuse any unit whose rendered ExecStart would bind the dashboard to a non-loopback address.

Also write `docs/ops/RUNBOOK.md`: the full reviewed flow (stage → stop writers → T03 backup → migration → T04 preservation verify → T05 preflight dry-run → post-migration backup → cutover with entry timer OFF → T02 status → enable entries → T07 latch → T06 snapshot/compare across restart), with exact example commands.

Tests use a fake systemctl and a temp filesystem. Cover: path traversal in the tar refused, a sha mismatch refused, a keep-off unit never started, rollback removing only its own drop-ins, and a failure mid-cutover stopping the started units.

---

## T12 — Independent harsh review of T01
STATUS: OPEN
DEPENDS: branch `cloud/T01` has a commit whose message starts with `DONE T01:` (`git log origin/cloud/T01 --format=%s | grep '^DONE T01:'`)
BASE: origin/cloud/T01
OWNS: reports/T12.md, tests/test_review_t01_*.py
AVOID: every non-test source file

Review T01 as an adversary. Can a category-(b) integrity condition be mislabelled as a normal rejection? Can a receipt be forged or replayed for a different pass? Do existing production receipt kinds still verify? Is anything ever reset or rewritten? Is a charged request ever refunded? Write RED tests for each real defect (mark them `expectedFailure`) and rank them. Run `test_empty_history_successor_lifecycle` and the T01-touched modules.
