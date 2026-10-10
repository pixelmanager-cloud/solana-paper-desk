# solana-paper-desk cloud task queue

The coordinator (the Mac session) maintains this file; workers only read it. Follow `WORKER_PROMPT.md` on this branch.

## DECISION 2026-10-10 (CK): FRESH-START EXPERIMENT — read first

The old production store set (all files under `/var/lib/solana-desk/` today) will be **archived read-only, never modified**, and the next experiment starts on a **fresh, empty store set** with a new config version. Consequences for every task:
- **No retrospective reconciliation.** Retained NULL passes, old receipts and runtime successor/continuation chains for the OLD stores are out of scope. Do not write migrations for old stores.
- **No runtime successor pins.** A fresh ledger records its own implementation hash. Policy from now on: when code under `desk/` changes and the ledger is flat (no open position), the coordinator rotates to a new ledger/store set instead of pinning a successor. Successor machinery is used only if a position is open.
- **Within the new stores, all integrity rules still hold:** append-only records, charged requests stay charged, no counter, budget or reservation resets, and fail-closed evidence gates.
- **Shared across old and new:** `provider-pacing.sqlite` (the two-second Kraken pacing must stay shared and must never be reset), the discovery input `discovery/continuous.sqlite` (read as the candidate source), and the provider keys file.


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

**Do (fresh-start scope — see the DECISION at the top):**
1. **Prospective fix.** A normal, typed, *verified* no-entry rejection must write a terminal, replay-verifiable outcome in the same transaction that records the result. Template: `history_preparation_rejection.publish/verify/gate` (typed outcome kind with a two-way inventory, re-proved on each gate). Classify blockers into:
   - (a) verified normal rejections (no entry, charges retained) → terminal outcome;
   - (b) integrity/recovery-required conditions → stay NULL and fail closed, as today.

   Charged requests stay charged. No retry of the same scan/candidate.
2. **Retrospective fix — NOT REQUIRED (fresh start).** Skip it. Only if it falls out trivially from (1), you may add one *generic* append-only receipt kind that `_gate` accepts for already-retained NULL passes whose recorded result proves a category-(a) rejection. Its proof is parameterised by the pass shape (blocker codes, attempts, journal binding), not a per-incident module. It must be immutable (trigger-guarded, like the existing receipt tables), bounded, and unique per pass. It must reject anything that does not prove a normal rejection.
3. Keep every existing receipt kind and its proofs working unchanged; existing production rows must still verify.
4. **Tests (fail-first):**
   - The installed-lifecycle scenario: an empty-window `MARKET_PRODUCER_BLOCKED` pass on a fresh store must not block the next candidate.
   - Adversarial: a forged receipt, a mismatched intent hash, a category-(b) blocker presented as (a), a duplicate receipt, an updated or deleted original row.
   - The existing modules `test_paper_terminal_reconciliation`, `test_empty_history_no_entry`, `test_history_preparation_phase` and `test_paper_entry_dispatcher` must still pass.
   - Run `test_empty_history_successor_lifecycle` once at the end.
5. In the report, list every `CycleBlocked`/exception code and which category it falls into, and confirm that a fresh empty store set never needs any reconciliation receipt to pass the gate after a normal rejection.

---

## T13 — Fresh-start store bootstrap + ledger rotation
STATUS: OPEN
DEPENDS: none
BASE: r1-base
OWNS: tools/ops/fresh_start.py, tests/test_ops_fresh_start.py, config/experiments/ (new example config only)
AVOID: desk/** (if a desk change is truly required, describe it for the coordinator and route it via T01's branch owner)

This is the critical path alongside T01. Build `python -m tools.ops.fresh_start`, with `--dry-run` as the default:
- `plan --root /var/lib/solana-desk/exp-<version> --config <new config> --pacing-db <shared provider-pacing.sqlite> --discovery-db <shared continuous.sqlite>`. Prints exactly which stores would be created, with which repo initialisers, and the ExecStart/drop-in arguments every `desk-*` unit needs to point at them (entry dispatcher, held cycle, monitor, decisions, dashboard).
- `apply` (same args). Creates a canonical root (0700, no symlinks, must not exist yet) and initialises EVERY store the entry/held path needs, using the repo's own schema creators and activation APIs only. That includes: research, evidence (with fresh monitoring/ownership budgets set to the approved ceilings: monitoring 3600 requests/rolling hour, investigations 1000 admissions/day, queue 25, lifetime 18 requests/investigation), ledger, decisions journal, dispatcher journal + its activated context, and anything else the gates require. No retained-history successor/extension rows for a fresh ledger. Writes a `fresh-start-manifest.json` (config digest, implementation hash, store list, created UTC).
- `rotate --from <root> --to <new root> ...`: allowed only when the old ledger is flat (no positions, mode RUNNING or ENTRY_PAUSED, no unresolved exit). It leaves the old root untouched and bootstraps a new one.
- Write `config/experiments/paper-kraken-fresh.example.json`: a copy of the production experiment parameters (initial 5 SOL, fee reserve 0.3, max position 0.02, exposure 0.08, max positions 4, stop 0.18, time-stop 2700s, max hold 21600s, trailing 0.30, cooldowns 1800/7200, mcap 50k–2M USD, min liquidity 8000 USD, round-trip cost cap 0.08, fee 0.00005 SOL, slippage 50 bps, TTLs price10/holder120/flow30/momentum15, signal policy v3, quote execution v1, token profile v2, usd valuation v1) with a NEW version string `2026-10-11.paper-quote-kraken.fresh.1`. Read the real config schema in `config/` and `desk/model.py` (and the existing paper-kraken config fields referenced by tests) so the file is valid; do not invent keys.

**Key acceptance test (fail-first where applicable):** on a temp dir, `apply`, then run the real dispatcher `--plan`/`_preflight`, the terminal gate and the scheduler entry pre-check against the new stores (shared pacing DB = a fixture copy). All must PASS with no reconciliation receipts and no successor pins. Then simulate one normal rejection followed by a second candidate using fixtures. If the second candidate is blocked, mark the test `expectedFailure` and state that it depends on T01. Also test: refuse an existing root, refuse a symlinked root, refuse rotation with an open position, and the old root byte-identical after rotation.

---

## T11F — Fix coordinator review findings in T11 (cutover tool) — CRITICAL PATH
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T11
OWNS: tools/ops/cutover.py, tests/test_ops_cutover.py, docs/ops/RUNBOOK.md
AVOID: desk/**, deploy/*.service, deploy/*.timer

The coordinator review found that the cutover/rollback lifecycle is not yet safe. Fix each item with a fail-first test:
1. **A failed or hung start is never stopped.** A unit whose `systemctl start` fails or times out must be added to the started set *before* the start call, so the failure path stops it. Wrap `subprocess.TimeoutExpired` (and every other runner exception) in a `CutoverError`.
2. **Already-running units.** Refuse to cutover any unit in `--units` that is not inactive/failed beforehand. Starting it would be a silent no-op, and the old code would keep running. After the start, verify the *running* process: `ExecMainStartTimestamp` must be newer than the cutover start, `MainPID` must have changed, or for oneshot timers the next trigger must be scheduled.
3. **Rollback with active units.** Rollback must refuse while any managed unit is active, unless `--stop` is given, in which case it stops them first. Never leave timers firing the old release against the archived stores.
4. **Keep-off must also be disabled.** Keep-off units must be `disable`d as well as stopped (check `is-enabled`, report it, and refuse to proceed if one is still enabled), so a reboot cannot start entries early.
5. **Health settle.** `unit_ok` must not accept `activating`/`auto-restart`. Re-check after a settle delay (configurable, default 10s), and require `NRestarts` to be unchanged.
6. **Old-store guard.** Refuse to start any writer unit whose effective ExecStart or Environment references the archived store root (`--archived-root /var/lib/solana-desk`), unless the unit is in an explicit `--allow-archived` list (empty by default).
7. **T13 format.** Accept T13's `unit_arguments` JSON directly. Check `git show origin/cloud/T13:tools/ops/fresh_start.py` for the exact shape: keys without the `.service` suffix, lowercase `environment`/`argv` keys, argv without the interpreter. Render it to systemd correctly: proper systemd quoting, `%` escaped as `%%`, the full interpreter path prepended. No hand conversion.
8. **Partial store-env.** `--store-env` with a subset of units must be allowed (stray-unit refusal only for units that are neither in `--units` nor in the file).
9. **Minor fixes.**
   - Hash and extract from the same open fd.
   - Collision → `CutoverError`.
   - Check all effective ExecStart lines.
   - fsync drop-ins and their directory.
   - The marker must be on the first line.
10. **Rewrite the RUNBOOK** for the fresh start:
    - The new store root lives OUTSIDE the old one: `/var/lib/solana-desk-fresh/<version>/`. Old: `/var/lib/solana-desk/`, archived read-only.
    - The archive backup uses `--expect-count 14` (the 12 stores plus `demo.sqlite` and `verified-demo.sqlite`).
    - Cut over and re-enable EVERY unit that should run: continuous discovery (as a long-running service with `Restart=`; it currently exits after `--seconds 86400`), held cycle, monitor, decisions, dashboard (loopback only) and backup (pointed at the new root).
    - Install the T07 latch drop-in BEFORE enabling the entry timer.
    - Give the exact cwd for every `python -m tools.ops.*` command, including stage, which runs from the staged release.

---

## T02F — Fix coordinator review findings in T02 (status CLI)
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T02
OWNS: tools/ops/status.py, tests/test_ops_status.py
AVOID: desk/**

1. **[Blocker]** The release probe writes `.pyc` files into `--release-dir`. Use `python -I -B`, set `PYTHONDONTWRITEBYTECODE=1` in the subprocess env, and set `sys.dont_write_bytecode=True` before any `desk` import in the tool itself. Add a test that runs the REAL probe subprocess against a copied `desk/` tree and asserts that no file appears.
2. **[High]** `immutable=1` is only allowed when the header byte 18 == 2 (WAL) and there is no `-wal`. Otherwise use plain `mode=ro`. `provider-pacing.sqlite` is in rollback-journal mode and is written every ~2s.
3. **Tests:**
   - exercise the live-WAL `mode=ro` path with an open writer connection;
   - add a quote-execution fill fixture so the `EXECUTION_UNVERIFIED` extraction is actually asserted.
4. **Binding check.** Check the `paper_monitoring_budget.ledger`/`config_hash` binding against `--ledger` and the config; a mismatch is a blocker.
5. **Fail closed:**
   - ownership-exhausted must not be limited to the first 50 rows (use an aggregate query);
   - the `os.walk` onerror raises;
   - symlinked or hardlinked stores go into blockers;
   - NULL ceiling/mode becomes a blocker, not a crash.

   A non-null pacing `pending` becomes a WARNING, not a blocker, unless it stays unchanged across `--samples 2` reads 3s apart.

---

## T10F — Fix coordinator review findings in T10 (forward eval)
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T10
OWNS: tools/research/forward_eval.py, tests/test_forward_eval.py
AVOID: desk/**

1. **MFE/MAE.** Rebuild MFE/MAE from journaled held market events (`events` rows with `reserve_sol`/`reserve_tokens`/`market_cap_usd`, desk/model.py ~:267) between open and close for constant-product ledgers. Report `UNAVAILABLE_QUOTE_MODE` only where the marks truly are not persisted, and detect which case applies.
2. **Pooled ordering.** Sort pooled trades by `closed_at` (tie-break ledger, seq), not by per-ledger seq. Define the pooled equity baseline explicitly. Add a test that argument order does not change any number.
3. **One snapshot.** Validate and read inside one read transaction, or assert that the max outcome seq equals what was validated.
4. **Holdout.** The holdout drawdown starts from equity at the cut. Split on ENTRY time (state this in the output) so train-period decisions do not leak.
5. **Code versions.** Same config hash but different implementation hashes → separate rows by default (`--pool-implementations` to merge, with a warning).
6. **Concurrency test.** Add a known-answer test with concurrent positions: buy A, buy B, sell B, sell A.

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
STATUS: DEFERRED (fresh start makes it non-critical; do not claim)
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
- **Multi-position ready:** do not refuse multiple entries. Attribute each round trip per position (mint + entry fill identity); report open positions and closed round trips separately; and check that portfolio cash equals the initial amount plus the sum of realized PnL minus the cost of open positions. The desk will soon hold several coins at once (see T16).
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

## T09 — Harsh first-cycle readiness audit (fresh-start deployment)
STATUS: OPEN
DEPENDS: none
BASE: r1-base
OWNS: reports/T09.md, tests/test_audit_first_cycle_*.py (new tests only)
AVOID: every non-test source file

Act as a harsh critic. Assume the coordinator deploys `r1-base` plus the T01 fix on a FRESH empty store set (T13) and starts entries. Find every way the next 48 hours fail *without* a code bug in the classic sense: another one-off reconciliation needed, a gate latch, budget exhaustion, a pacing deadlock, a service wall timeout (entry 600s, held 120s, MemoryMax ~384MiB), retained-history replay cost growth, a lock left behind after SIGKILL, a timer cadence overlap, or a checkpoint/config pin mismatch after a config change.

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
- `cutover --release <dir> --units u1 u2 ... --keep-off desk-paper-entry-dispatcher.timer [--store-env <file>]`: write the `60-reviewed-release.conf` drop-ins (WorkingDirectory=<release>, plus, for fresh start, the new store/config paths the units' ExecStart needs — coordinate the format with T13's output), run `daemon-reload`, start only the listed units, and never start `--keep-off` units. Then verify each unit's effective WorkingDirectory and ActiveState. On any failure, stop every unit the tool started and leave drop-ins recorded for rollback.
- `rollback --units ...`: remove the drop-ins the tool wrote (only those, identified by a marker comment), then daemon-reload.
- Refuse any unit whose rendered ExecStart would bind the dashboard to a non-loopback address.

Also write `docs/ops/RUNBOOK.md`: the full reviewed flow for the FRESH-START deployment (stage → stop writers → T03 archive backup of the old stores → mark the old stores read-only → T13 bootstrap of the new store set → T05 preflight dry-run on the new set → cutover with the entry timer OFF and units pointed at the new paths → T02 status → enable entries → T07 latch → T06 snapshot/compare across restart), plus the 'rotate ledger when flat' procedure, with exact example commands.

Tests use a fake systemctl and a temp filesystem. Cover: path traversal in the tar refused, a sha mismatch refused, a keep-off unit never started, rollback removing only its own drop-ins, and a failure mid-cutover stopping the started units.

---

## T12 — Independent harsh review of T01
STATUS: OPEN
DEPENDS: branch `cloud/T01` has a commit whose message starts with `DONE T01:` (`git log origin/cloud/T01 --format=%s | grep '^DONE T01:'`)
BASE: origin/cloud/T01
OWNS: reports/T12.md, tests/test_review_t01_*.py
AVOID: every non-test source file

Review T01 as an adversary. Can a category-(b) integrity condition be mislabelled as a normal rejection? Can a receipt be forged or replayed for a different pass? Do existing production receipt kinds still verify? Is anything ever reset or rewritten? Is a charged request ever refunded? Write RED tests for each real defect (mark them `expectedFailure`) and rank them. Run `test_empty_history_successor_lifecycle` and the T01-touched modules.

---

## T14 — Execution-realism measurement (latency-adjusted paper fills)
STATUS: OPEN
DEPENDS: none (unblocked by the coordinator for speed)
BASE: r1-base
NOTE: T01 is being developed in parallel and may edit desk/paper_cycle.py, desk/paper_terminal_reconciliation.py and the dispatcher's gate handling. Keep your edits to those files minimal and well-separated (new functions or modules, not rewrites), so the coordinator can merge both cleanly.
OWNS: desk/quote_execution.py, a new desk/fill_realism.py, the related tests, and tools/research/fill_realism_report.py
AVOID: desk/paper_terminal_reconciliation.py and T13's files

The developer intends to move to real-money execution later, gated on paper evidence. The paper results must therefore predict live results. A past project died because a ~4s decision→fill latency cost 5–8% per round trip, which paper results never showed.

Implement an explicit, versioned, opt-in measurement (a config flag, off by default, with a new version key):
- For every simulated BUY and SELL fill, record re-quotes of the same route at +2s, +5s and +10s (bounded; each attempt is charged to the existing budgets, and a failure is charged too). Never change the recorded paper fill itself.
- Persist the drift: price, out-amount and implied slippage vs the decision quote, append-only, bound to the fill identity.
- `python -m tools.research.fill_realism_report --ledger ...` (read-only) reports the per-trade and aggregate drift distribution, plus the paper PnL re-computed under the +2s/+5s/+10s fills ("what a live bot with N seconds of latency would have got").

Tests: fixtures with a scripted quote source, budget charging including failures, the default-off path producing byte-identical behaviour, and the report's math checked against known answers.

---

## T15 — Live-execution design document (NO executable trading code)
STATUS: OPEN
DEPENDS: none
BASE: r1-base
OWNS: docs/live/LIVE_EXECUTION_DESIGN.md (documentation only)
AVOID: all code

The developer wants a future path from paper to real money. Write the design only. **Do not add signing, broadcasting, wallet or key-handling code in this task.** The doc must cover:
1. **Executor abstraction.** Define a single `Executor` interface (quote → build → sign → send → confirm → reconcile) with `PaperExecutor` (current behaviour) and a future `LiveExecutor`, and show exactly where it plugs into the current flow (`engine.transition` → `fulfill_quotes` → `ledger.apply`). Live fills must be recorded from on-chain confirmation, not from the quote.
2. **Latency and MEV realities** for pump.fun/PumpSwap via Jupiter: priority fees, Jito bundles, slippage tolerance, failed or dropped txs, partial fills, and RPC choice. How to measure them in shadow mode before any funds move.
3. **Key and fund safety:** a dedicated hot wallet with a hard SOL cap, keys in systemd credentials or a hardware/remote signer, never in Git or logs. A per-trade cap, a daily loss cap, a global kill switch (a file plus a CLI), and an auto-halt on N consecutive failed sends or any reconciliation mismatch.
4. **Go-live gate (proposed, for the developer to approve):**
   - ≥ N forward paper round trips on one frozen config version;
   - positive net PnL after T14's latency-adjusted fills (not just the instantaneous quote);
   - the bootstrap CI lower bound above 0, or an explicit risk acceptance;
   - a shadow-mode period (build and sign-simulate, never send) with zero reconciliation errors;
   - then a tiny-size live phase.
5. **Staged rollout plan, failure handling and reconciliation.**
6. **What in the current codebase would block live use**, ranked.

Be concrete, with file:line references. Be harsh about the risks.

---

## T16 — Concurrent multi-position trading (entries while holding)
STATUS: OPEN
DEPENDS: none (unblocked by the coordinator for speed)
BASE: r1-base
NOTE: T01 is being developed in parallel and may edit desk/paper_cycle.py, desk/paper_terminal_reconciliation.py and the dispatcher's gate handling. Keep your edits to those files minimal and well-separated (new functions or modules, not rewrites), so the coordinator can merge both cleanly.
OWNS: tools/paper_scheduler.py, tools/paper_entry_dispatcher.py, tools/history_first_paper_entry.py, desk/paper_scheduler.py, desk/paper_monitor_service.py, the related tests, and docs/MULTI_POSITION.md
AVOID: desk/paper_terminal_reconciliation.py, T13's and T14's files. Touch desk/paper_cycle.py only for the per-item held-mint gate, and say so in the report.

The developer wants the desk to hold several coins at once. The engine and ledger already support multiple positions (`max_positions`, exposure fraction, a held-monitoring allowance shared across positions). But three entry paths refuse ANY entry while a position is open:
- `tools/paper_scheduler.py` ~:40 (`HELD_POSITION_PRIORITY`);
- the dispatcher `_preflight`/`_held_guard` ~:313/:350;
- `history_first_paper_entry._context` ~:47.

Make concurrency an explicit, versioned config option: `paper_concurrent_entries_version: 1` with `max_positions` > 1. With it absent, behaviour must stay byte-identical to today (one at a time).

Requirements:
1. **Held first, always.** Each scheduler tick runs the held/exit pass for every open position before any entry work. An entry must never delay an exit. Use the existing lease. If held monitoring is degraded (an unresolved exit, EXIT_ONLY, the monitoring budget near its ceiling), block new entries.
2. **Engine gates stay authoritative:** max_positions, max exposure 0.08, max position fraction 0.02, per-mint no-duplicate, cooldowns, daily pause and liquidation. No path may bypass `engine.transition`.
3. **Budget partitioning.** Entry investigations and held monitoring must not starve each other. Reserve monitoring capacity per open position, refuse an entry that would leave too little monitoring allowance for all positions (including the new one), and document the math.
4. **Wall-time.** The entry service has a 600s wall timeout and held has 120s with N positions. Prove that held passes for 4 positions fit inside the timeout using the fixture timings, or split the work.
5. **Accounting and reporting:** per-position attribution in the ledger readers and dashboard view (read-only), with portfolio cash consistency checked after interleaved buy/buy/sell/buy/sell sequences.
6. **Tests (fail-first):**
   - two entries while holding (allowed only with the flag);
   - a 5th entry refused at max 4;
   - an exposure cap refusal;
   - an exit-blocked position freezing entries;
   - a cold restart with 3 open positions resuming monitoring without duplicate fills or charges;
   - default-off byte-identical behaviour.

Write `docs/MULTI_POSITION.md`: the scheduling model, the budget math, the failure modes, and the recommended first setting (e.g. max_positions 2, then 4) for the coordinator to activate after the first single-position cycle is verified.

---

## T17 — Fix review findings in the live-execution design doc
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T15
OWNS: docs/live/LIVE_EXECUTION_DESIGN.md (docs only)
AVOID: all code

Coordinator review of T15. Amend the doc:
1. **Signer policy vs rent.** §3 refuses "any account close", but each round trip then strands about 0.002 SOL of ATA rent, roughly 20% of a 0.01 SOL canary trade. Allow closing the desk's own ATA back to the hot wallet (and only that), or justify a different canary size. Show the math.
2. **Bootstrap CI basis.** §4.3's CI must be computed on T14's +5s latency-adjusted fills including the real fee stack (priority fee, Jito tip, rent), not on the instantaneous quote or `fixed_fee_sol`.
3. **Size mismatch.** Paper trades 0.1 SOL and the canary 0.01 SOL. The gate must require PnL recomputed at canary size, with fixed costs scaled correctly. Alternatively, recommend matching sizes.
4. **Numbers everywhere.** Put a number on every gate threshold: shadow build success rate, fee tolerance, canary realised-vs-model cost tolerance. Define "reconciliation" in simulate-only shadow mode (e.g. simulated balance deltas vs the expected quote).
5. **Risk acceptance.** Cap the "explicit written risk acceptance" escape hatch at no more than the wallet cap, with a time limit.
6. **Reference fix.** `forJitoBundle` is at providers.py:213, not :223.

---

## T20 — Watchlist re-evaluation of "not yet" rejections (APPROVED rule change)
STATUS: OPEN
DEPENDS: branch `cloud/T16` has a `DONE T16:` commit (both edit the dispatcher's candidate selection)
BASE: origin/cloud/T16
OWNS: tools/paper_entry_dispatcher.py (selection only), a new desk/watchlist.py, tests/test_watchlist*.py, docs/WATCHLIST.md
AVOID: T01's reconciliation files

**DECISION (CK, 2026-10-11).** Replaces the old "never retry retired candidates" rule *for the fresh store set only*. The goal is a 24/7 desk that keeps filtering graduated memecoins and trades them when they satisfy selection and entry criteria. Today a candidate is evaluated once and a token that becomes eligible 30 minutes later is lost.

- **Classify** every rejection into:
  - **PERMANENT**: rug/control hazards, dangerous Token-2022 extensions, corrupt or contradictory evidence, the known-hazard list;
  - **NOT_YET**: market cap or liquidity out of range, too young or too old for the window, insufficient history, empty five-minute window/`MARKET_PRODUCER_BLOCKED`, momentum or flow not met.

  Default to PERMANENT for anything unclassified (fail closed). Show the full mapping table in the docs.
- **Schedule NOT_YET mints.** They go to an append-only watchlist with `next_eval_at` (backoff 10m → 30m → 2h, configurable) and a max of N re-evaluations (default 4). They expire when the token leaves the engine age window (21600s).
- **Fresh scans only.** Each re-evaluation is a NEW scan with a new intent id, and its requests are charged to the normal budgets. The original scan and its outcome are never retried, rewritten or un-charged.
- **Selection order.** The dispatcher considers fresh migration hints first, then due watchlist entries, inside the existing budgets. Admission throughput must not starve held monitoring (respect T16's partitioning).
- **Versioning.** A versioned config flag `paper_watchlist_version: 1`; when absent, behaviour stays identical.
- **Tests (fail-first):**
  - a NOT_YET token rejected at t0 and accepted at t0+30m with new fixture data;
  - a PERMANENT token never re-admitted;
  - an unclassified code is treated as PERMANENT;
  - the backoff schedule and max count;
  - budget charging of re-evaluations;
  - the original scan rows unchanged;
  - a cold restart preserving the watchlist.

---

## T21 — 24/7 unattended operations
STATUS: OPEN
DEPENDS: none
BASE: r1-base
OWNS: deploy/fresh/ (NEW unit files and drop-ins for the fresh-start deployment), tools/ops/healthcheck.py, tools/ops/notify.py, tests/test_ops_healthcheck.py, docs/ops/OPERATIONS_24x7.md
AVOID: desk/**, the existing deploy/*.service|*.timer (do not modify them; create new templates under deploy/fresh/)

The desk must run unattended 24/7. Today continuous discovery runs with `--seconds 86400` and exits after a day, it is not enabled at boot, and nothing watches health.
1. **Units under `deploy/fresh/`** for every service the fresh desk needs (discovery, entry dispatcher + timer, held cycle + timer, monitor + timer, decisions + timer, dashboard on loopback, backup + timer pointed at the new root), with:
   - `Restart=on-failure` and sane `RestartSec`/`StartLimit*`;
   - long-running discovery that restarts cleanly instead of exiting after 24h;
   - `WantedBy` so the intended units start at boot, except the entry timer, which is documented as enabled only by the coordinator after verification;
   - the existing hardening (ProtectSystem, credentials via `LoadCredential`, MemoryMax);
   - store paths as `<FRESH_ROOT>` placeholders that match T13's layout.
2. **Health check.** `tools/ops/healthcheck.py` is read-only and runs as a timer every 5 min. It checks:
   - each unit's state and restart counts;
   - discovery freshness (latest discovery row age);
   - entry/held tick recency;
   - an open position's last mark age vs its TTL;
   - unresolved exits / EXIT_ONLY;
   - budget headroom;
   - pacing blocked;
   - disk free;
   - the latest backup age.

   It emits one JSON line and exits non-zero on CRITICAL.
3. **Notify.** `tools/ops/notify.py` uses a pluggable notifier. The default writes to the journal plus a state file. An optional Telegram bot is supported when a token file exists in systemd credentials, with the token never in Git or logs; it is off unless configured. It rate-limits and de-duplicates alerts, and sends a daily summary (positions, realized PnL, trades, blockers).
4. **Tests:** fake systemctl, fixture stores, staleness thresholds, rate limiting, and the notifier never logging secrets.
5. **`docs/ops/OPERATIONS_24x7.md`:** the alert meanings and the operator response for each.
