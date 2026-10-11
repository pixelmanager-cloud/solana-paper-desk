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

## T13F — Fix coordinator review findings in T13 (unit wiring) — CRITICAL PATH
STATUS: OPEN
DEPENDS: none
BASE: integration/r1 (T13 is already merged there)
OWNS: tools/ops/fresh_start.py, tests/test_ops_fresh_start*.py
AVOID: desk/**

The store bootstrap is good and has been merged. The unit wiring it emits is incomplete:
1. **Dashboard.** Emit `DESK_PAPER_SCHEDULER_IDENTITY`; without it `Jobs.once()` raises "Reviewed scheduler inode identity required" and the scanner thread dies. Also emit `DESK_PAPER_LEDGER_DB=<root>/paper-ledger.sqlite`, or name the ledger so `/api/paper` finds it. Add a test that runs `Jobs.once()` against the new root with the emitted env.
2. **Full drop-ins.** Emit full systemd drop-in content per unit, not just env/argv:
   - `ReadWritePaths=<root>`, because units have `ProtectSystem=strict` with only `/var/lib/solana-desk` writable;
   - `ConditionPathExists=` overrides for the new ledger path (held and monitor currently keep `ConditionPathExists=/var/lib/solana-desk/active-paper.sqlite` and would be SILENTLY SKIPPED);
   - reset the ExecStart (`ExecStart=` then the new line) with the full venv interpreter `/opt/solana-desk/.venv/bin/python`, and use proper systemd quoting and `%%` escaping.

   Provide a `render-dropins --out DIR` subcommand that writes one `.conf` per unit, so T11F's cutover tool can install them unchanged. Coordinate with T11F (`origin/cloud/T11F`) and T21 (`deploy/fresh/`) if they exist; the T11F format requirement says to accept T13 output directly.
3. **Service user.** `apply` refuses unless the effective user is the service user (`--service-user solana-desk`, default `solana-desk`), OR it runs as root with `--chown solana-desk` and chowns every created file, directory and lock, verifying ownership afterwards. Without this the scheduler lease owner check and SQLite writes fail.
4. **`verify_flat` / `rotate`.**
   - Take the ledger's paper-cycle lock (non-blocking; refuse if busy) for the whole check and bootstrap.
   - Refuse if the dispatcher journal has an intent without a result, if any position has `exit_blocked` or a pending exit, or if there is an unresolved observation pass.
   - Support roots not created by this tool by passing explicit paths.
5. **Held cycle.** Do NOT silently drop `--dependency-blocker`. Emit the held unit with an explicit `--enable-held` flag that is required to omit it, and document it in the output.
6. **Backup.** Emit backup unit args for the new root (`desk.backup --data <root>` or `tools.ops.backup`).

---

## T05F — Fix coordinator review findings in T05 (preflight dry-run)
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T05
OWNS: tools/ops/preflight_dryrun.py, tests/test_ops_preflight_dryrun.py
AVOID: desk/**, tools/paper_entry_dispatcher.py

This tool runs on the Ubuntu VPS as root (needed for `unshare --mount`), while the stores are owned by `solana-desk`.
1. **Owner check.** It can never PASS in production: the OWNER check compares against euid. Add `--expect-owner USER` (default: the owner of `--data`) and compare against that instead. Test it by running with a different expected owner, simulated via an injectable stat or uid map.
2. **Fresh store set.** A missing `paper-scheduler.lock` or an absent journal must report precisely what is missing. Keep it fail closed, but make the message say which bootstrap step creates it (T13F pre-creates the lock and activates the journal). Do NOT add a bypass.
3. **macOS false positive.** `/proc/self/fd` dir_fd resolution only exists on Linux. Use `fcntl.F_GETPATH` on macOS, or skip that check off Linux with an explicit reason, so the suite is green on macOS. The namespace tests may skip without `unshare`. Also add a GitHub-Actions-friendly note: CI runs on Ubuntu, so the namespace tests will run there. Confirm whether they need root, and mark them clearly.
4. **`_pending` fails open.** On `sqlite3.Error` it returns `[]`. Make that a blocker (`PENDING_UNREADABLE`).
5. **Live-file guards.**
   - Creating WAL sidecars beside a live store is a BLOCKER.
   - `live_sources_unchanged` compares sha256, not only size and mtime.
6. **Minor.** Fix the docstring (paths/journal ARE compared). Run the parent process with no bytecode writes (`sys.dont_write_bytecode=True` at the top).

---

## T32 — Deploy composition: make T13F + T11F + T21 + RUNBOOK one working end-to-end flow — CRITICAL PATH
STATUS: OPEN
DEPENDS: none
BASE: integration/r1 (T13F, T11F and T21 are all merged there)
OWNS: tools/ops/fresh_start.py, tools/ops/cutover.py, tools/ops/healthcheck.py, tools/ops/notify.py, deploy/fresh/**, docs/ops/RUNBOOK.md, docs/ops/OPERATIONS_24x7.md, the related tests/test_ops_*.py
AVOID: desk/**

Each piece passed its own tests (213 OK), but they do not compose. A single owner must make them one flow. The coordinator review found:
1. **Store-env jq.** The RUNBOOK step-7a jq adds `desk-backup.service` while the manifest already has `desk-backup`, so cutover raises "names desk-backup.service twice". It also uses `desk.backup`, which cannot back up the fresh set. Remove the jq and consume the manifest/render output directly.
2. **Entry is dry-run.** The entry argv (fresh_start.py ~:219-224) lacks `--execute --systemd-credentials`, and the 60-/70- drop-ins reset ExecStart over T21's correct template, so entries never happen. Emit `TimeoutStartSec` too (entry 600, held 120).
3. **Drop-in hand-off.** Cutover silently drops `unit_sections` (cutover.py ~:302): no ReadWritePaths, no ConditionPathExists reset, no TimeoutStartSec. It must install T13F `render-dropins` output, or one combined format. Rollback must remove every drop-in the flow wrote (60- and 70-, identified by the marker).
4. **Entry template.** The entry unit must use the fresh template, not the stock one (stock = ReadWritePaths on the old root only, ConditionPathExists on the old journal, TimeoutStartSec 180).
5. **Held cycle.** The plan/apply in the RUNBOOK must pass `--enable-held`; otherwise held exits 2, cutover verification aborts, and positions are never exited.
6. **Service user.** `apply` needs `--service-user solana-desk`/`--chown solana-desk` and must create the backup dir.
7. **Monitor timer.** Do NOT cut over or start the expire/monitor timer until T23 (EXIT_ONLY stickiness) lands. Gate it behind an explicit flag and document why.
8. **Boot and health.** Add boot-enable steps (`systemctl enable`) for the intended units (not the entry timer), plus the healthcheck/notify timers. Reconcile OPERATIONS_24x7's `enable --now` with cutover's refusal of already-running units: one sequence only.
9. **Backup paths.** Pick ONE backup destination for the fresh set (`/var/backups/solana-desk/fresh-<version>/`) and use it everywhere: T13F, the T21 units, the RUNBOOK and the healthcheck `--backup-root`.
10. **README root.** README.md:8 shows the example root inside the archived root. Use `/var/lib/solana-desk-fresh/<version>`.
11. **Cutover timeout.** The runner timeout for oneshot `systemctl start` is 120s, which is ≤ the units' own timeouts. Use `--no-block` plus polling with a deadline above TimeoutStartSec.
12. **Dashboard env.** The T21 dashboard unit lacks `DESK_PAPER_SCHEDULER_IDENTITY` and `DESK_PAPER_LEDGER_DB`, and the invariant test skips the dashboard. Add both and test them.
13. **Restart alarms.** The healthcheck counts discovery's daily clean restart as a failure, giving false WARN/CRITICAL. Count only non-zero-exit restarts, or use a rate over a window.
14. **Stale reports.** notify must reject a stale report (`ts` older than 2× the interval → CRITICAL "healthcheck not running"). Catch `sqlite3.Error` in build_report as a CRITICAL report, not a crash. A malformed telegram.json logs a WARNING.
15. **Rotate.** The rotate procedure must stop the dashboard as well.
16. **Pacing access.** Narrow ReadWritePaths for the shared pacing DB to that single file, or its directory if SQLite needs it, instead of the whole archived root.

**Required end-to-end test.** On a temp filesystem with fake systemctl, run the RUNBOOK sequence literally (parse the commands from RUNBOOK.md, or keep a script that the RUNBOOK mirrors): archive → bootstrap → render drop-ins → cutover (entry OFF and disabled) → healthcheck OK → latch install → entry enable → rollback. Assert:
- every rendered unit satisfies the invariants (T09 F8/F11, paths in the fresh root, loopback dashboard);
- no unit references the archived root except the pacing DB;
- rollback restores everything.

---

## T23F — Fix the pacing-orphan reclaim (F6) from the T23 review — CRITICAL PATH
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T23 (merge origin/integration/r1 first: take T23's side in the two audit test files, where it removes `expectedFailure`)
OWNS: desk/provider_pacing.py, tests/test_provider_pacing.py, tests/test_pacing_reclaim.py, plus MINIMAL call-site additions (one call to `Pacer.reclaim_orphans()` before the pending check) in tools/paper_entry_dispatcher.py (~:333, ~:409), desk/paper_terminal_reconciliation.py (~:135) and tools/history_first_paper_entry.py (~:72)
AVOID: every other line of those call-site files (T22/T22F own them); desk/engine.py (the T23 F5 part is accepted as is)

T23's F5 (EXIT_ONLY auto-recovery) is correct and stays as it is. F6 does not fix the T09 deadlock, and it weakens a deliberate fail-closed guard:
1. **(HIGH) Reachability.** The reclaim only runs inside `Pacer.acquire()`. The dispatcher `_preflight`, the terminal gate and history_first refuse on `pending IS NOT NULL` WITHOUT calling acquire, so on a flat ledger an orphan still blocks entry forever.
   - Fix: add a public `Pacer.reclaim_orphans()` with the same proofs and the same append-only row, and call it right before each of those checks.
   - Add an end-to-end test: SIGKILL a real subprocess holding a slot, then the dispatcher preflight passes after the age threshold. While the owner is alive it must NOT pass.
2. **(HIGH) "Owner gone" must mean the PROCESS is gone.** Today it means the Pacer object was deleted: `__del__` releases the flock, and `paper_read_sources` deliberately keeps tickets (`pacing_release=False`, e.g. a 429 whose throttle write lost to contention). A live process's guard is then reclaimed after 60s with a fixed 30s embargo that ignores longer Retry-After values.
   - Fix: remove `__del__`. Keep the held fds in a module-level registry so only process exit releases them.
   - Honour the stored Retry-After/`blocked_until`: the reclaim never lowers it.
   - Restore the two loosened assertions in `tests/test_provider_pacing.py` (~:329-345) to their original strictness, or justify any change in the test header. A reclaim with the owner alive must be impossible.
3. **(MED) Mixed-version schema.** The new `pacing_reclaims`/`sqlite_sequence` tables make old-code `Pacer()` raise `PACING_DATABASE_INVALID`, and `tools/verify_*_originals.py` compare the schema strictly. Document in RUNBOOK notes (hand to T32 via the report) that every unit using the shared pacing DB must switch atomically. Make the old→new upgrade explicit and idempotent, not a side effect of the first reclaim.
4. **(LOW) Races and validation.**
   - Close the window between the grant commit and `_hold` (take the flock before committing the grant, or record an owner token atomically).
   - Validate `paper_exit_only_recovery_version` at initialize/config load, not at the first `risk()`.
5. **(LOW) Latch interaction.** Add a test combining the T07 entry latch with F5 auto-recovery: after recovery, entries stay latched on EVERY entry path, not only via the dispatcher's ExecStartPre. If they don't, make the latch check part of the scheduler entry pre-check.

---

## T23G — Last pacing/latch fixes (from the T23F review) — CRITICAL PATH
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T23F, then merge origin/integration/r1 (it merges cleanly)
OWNS: desk/provider_pacing.py, desk/model.py or wherever config is loaded (validation only), tools/paper_scheduler.py (entry pre-check only), config/experiments/paper-kraken-fresh.example.json, tests/test_provider_pacing*.py, tests/test_pacing_reclaim*.py, tests/test_latch_exit_only_recovery.py
AVOID: everything else

1. **(HIGH) Release only after a successful commit.** `finish()` and `throttle()` call `_release()` BEFORE `commit()` (~:310, ~:352). In DELETE journal mode with a 0.05s busy timeout, a reader's shared lock makes the commit fail with PACING_DATABASE_BUSY after the flock is gone, so a live owner's slot gets reclaimed (and a `Retry-After: 600` is lost, leaving only a 30s embargo).
   - Release the flock only after the commit succeeds. On commit failure keep holding it and retry the commit with bounded backoff; if it still fails, keep the lock until process exit.
   - Add tests reproducing the coordinator probes: a live owner with a busy commit is never reclaimed, and the throttle's Retry-After is preserved.
2. **(MED) Read before write.** `reclaim_orphans()` always opens a write transaction, which raises PACING_DATABASE_BUSY under contention. At the dispatcher (~:409), which runs after the intent is written, that adds a new unresolved-dispatch path (T09 F3).
   - Read first (`mode=ro` or a deferred read). Take the write lock only when an old pending row exists.
   - If the database is busy, return `[]` (no reclaim this time) and NEVER raise.
   - Test it under writer contention.
3. **(MED) Validate at load.** Validate `paper_exit_only_recovery_version` (and every versioned flag the engine reads lazily) at config load, so a bad value is refused before any cycle.
4. **(MED) Latch on every entry path.** The scheduler's entry pre-check (`tools/paper_scheduler.py`) must also refuse entry when the ledger has ≥1 BUY and the latch policy is configured. Use the T07 entry_latch read-only check, so manual or other entry paths are covered too, not only the dispatcher's ExecStartPre drop-in. Test it.
5. **Example config.** Set `paper_exit_only_recovery_version: 1` in `config/experiments/paper-kraken-fresh.example.json`, so auto-recovery is on in the fresh experiment. Validate it with the real config loader.
6. **(LOW) Docs.** Document in the module docstring that deleting the holder lock file breaks the owner proof (never delete it), and that a forked child inherits the lock.

---

## T32G — Final runbook fixes from the T32F review vs the real VPS — CRITICAL PATH
STATUS: OPEN
DEPENDS: none
BASE: origin/integration/r1 (T32F is merged)
OWNS: tools/ops/cutover.py, tools/ops/fresh_start.py, deploy/fresh/**, docs/ops/RUNBOOK.md, tests/test_ops_e2e_deploy.py, tests/test_ops_cutover.py, tests/test_ops_healthcheck.py
AVOID: desk/**

VPS facts (read-only, 2026-10-11):
- Ubuntu 24.04.4, systemd 255, Python 3.12.3; solana-desk is uid 999 / gid 988.
- `/var/backups/solana-desk` is root:root 0755.
- `/etc/solana-paper` is root:solana-desk 0750.
- `desk-paper-entry-dispatcher.service` and `desk-decisions.service` are currently `failed`.
- Most old units are `static`.
- Both shared DBs are in rollback-journal mode.
- The shared pacing DB MUST stay at `/var/lib/solana-desk/provider-pacing.sqlite`: the Kraken pacing migration receipt and its pin are bound to that exact path and mode 0600, so moving it breaks pacing everywhere. Keep that design.

1. **(BLOCKER) Seal and live lock files.** Seal makes `discovery/continuous.sqlite.discovery.lock` (solana-desk 0600, opened read-write by `discovery/continuous.py:61`) root 0444 with the sticky bit, which kills continuous discovery and the 7b cutover. Never touch live lock files (`*.discovery.lock`, `provider-pacing.sqlite.holder-*.lock`, `*.paper-cycle.lock`, ...); use an explicit allow-list of what seal may change. Seed these lock files in the e2e and assert discovery still opens its lock after seal.
2. **(MED) Seal permissions.** Use `root:solana-desk` with files 0440, dirs 0550, and shared dirs 1770, not world-readable 0444/1775/0555. Root-only 0600 files stay 0600 root. Before changing anything, write a seal manifest (path, uid, gid, mode, sha256), and add `unseal` to restore it exactly. Rollback after step 4 uses unseal.
3. **(11b, not done) Research units.**
   - Render `desk-counterfactual.*` and `desk-held-watcher.service` with the fresh-root layout.
   - Render and install `desk-paper-held-cycle.path` (render-units currently skips `.path`).
   - Make all research units OPTIONAL: not installed or enabled by default; an explicit RUNBOOK step enables them later.
   - Their write paths are their own state dirs plus the shared pacing DB directory requirement, documented.
4. **(Step 0)** `/var/backups/solana-desk` stays root-owned. Never chown it.
5. **(Step 2)** After the stops, run `systemctl reset-failed 'desk-*'`. The expectation is "disabled|static", not only "disabled".
6. **Watchdog grace.** Avoid the false CRITICAL at cutover before `health.json` exists (a grace period after cutover, or the first healthcheck run first).
7. **Archive verification.** `verify_records` also compares uid/gid. A crash part-way through archive removal must be resumable: re-run completes or restores; restore must not refuse a half-removed state that the manifest fully describes.
8. **RUNBOOK note.** Every tool invocation runs with cwd = `$REL` (a stale `desk` package in venv site-packages would otherwise shadow it). Optionally run `pip uninstall -y solana-desk` in the venv as a documented step.

---

## T38 — integration/r1 CI is RED: restore a green full suite on Linux — CRITICAL PATH, DO FIRST
(Coordinator: `cloud/T23H` is DONE but NOT merged. It only stops `reclaim_orphans` from raising. The lifecycle test still fails on integration+T23H with `AssertionError: 5.845` at the fixture line `assert lag<5` (test_empty_history_successor_lifecycle.py ~:54): the shared pacing `high_water` ends up ~6s AHEAD of wall time. Find what advances high_water/next_at into the future since T23G/T14G (reclaim with a fixture clock? the 3-tier priority? a reclaim row timestamp?) and fix that root cause. Merge `cloud/T23H` into your branch. , the narrower lifecycle `PACING_CLOCK_INVALID` fix. If `cloud/T23H` has a DONE commit, merge it in first and build on it.)
STATUS: OPEN
DEPENDS: none
BASE: origin/integration/r1
OWNS: desk/provider_pacing.py, plus the minimal test-fixture fixes needed in tests/ (no assertion loosening without a justification in the test and the commit)
AVOID: desk/paper_cycle.py, desk/paper_pass_closure.py (T22G)

CI (`.github/workflows/tests.yml`, full suite on Ubuntu) on `integration/r1` history:
- green up to the T13 merge;
- 1 failure from the T08 merge (`tests.test_allowance_upgrade.MonitoringUpgradeTests.test_failure_latch_and_clock_highwater_survive`; T08 changed the transient SOURCE_FAILURE latch semantics and this test still expects the old latch);
- 20 failures from the T25F merge (18 errors in `tests.test_monitoring_classification` on Linux, plus `test_audit_first_cycle_b_monitoring`);
- **~480 failures from the T23G merge.**

Each module passes ALONE; the full suite in ONE process fails. That is cross-test state leakage, and the same pattern can hit production processes that open several pacing DBs.

Top errors in run 38074623235 (`gh run view 38074623235 --log-failed`):

| Count | Error |
|---|---|
| 87 | `PacingError: PACING_DATABASE_INVALID` |
| 69 | `PaperReadError: PACING_DATABASE_INVALID` |
| 64 | `PACING_CLOCK_INVALID` |
| 71 | `AssertionError: synthetic sources only` |
| 67 | asyncio `'_UnixSelectorEventLoop' object has no attribute '_ssock'` |
| 49 | `'BLOCKED' != 'COMPLETE'` |
| ~50 | expected-code mismatches where the actual is `PACING_DATABASE_INVALID` |

The main suspect is T23G's module-level fd registry `_HELD` (process-lifetime holder locks): it leaks across tests, i.e. across pacing DBs and providers in one process. That gives a "live owner" to the wrong DB, inode reuse after tempdir cleanup, and validation mismatches.

**Required:**
1. **Fix the registry in production code.** Key it by (canonical db path, st_dev, st_ino, provider, ticket). Release entries when their own commit completes. Never let an entry for one DB affect another DB, and handle inode reuse after a file is deleted (validate the inode is still the same file before trusting a held entry). Add a production-relevant test: one process uses two different pacing DBs in sequence, and a DB is deleted and recreated at the same path.
2. **Fix `reclaim_orphans`/`acquire` clock handling** so a fixture clock never triggers `PACING_CLOCK_INVALID` on the read-first path. `tests.test_empty_history_successor_lifecycle` must pass.
3. **Fix every other failing test** in the CI logs at their root:
   - `test_allowance_upgrade`: update it to T08's semantics, with justification;
   - `test_monitoring_classification` on Linux: the lock-table injection must hold on Linux too;
   - the asyncio `_ssock` errors: most likely an event loop closed or leaked by `test_held_watcher`, so close loops properly;
   - "synthetic sources only".
4. **Acceptance.** Run `python -m unittest discover -q` IN ONE PROCESS on Linux (the cloud VM is Linux) and get exit 0, with the expected-failure count reported. Also run a randomized order: `python -m unittest` with modules shuffled (seed recorded) twice. Push and confirm that the GitHub Actions run on your branch is green before writing DONE. Paste the run URL in the report.

---

## T32I — Final deploy-flow fixes (from the T32H review vs the real VPS), composed with T36 — CRITICAL PATH
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T32H, then merge origin/cloud/T36 (approved by the coordinator)
OWNS: docs/ops/RUNBOOK.md, tools/ops/cutover.py (seal parts), tools/ops/preflight_dryrun.py (live-findings mode rule only), tools/ops/pacing_policy.py (DEFAULT_WRITERS only), tests/test_ops_*.py
AVOID: desk/**

1. **(BLOCKER) Preflight vs the seal.** Step 6 fails after the step-4 seal: `preflight_dryrun._live_findings` flags any external store directory with `mode & 0o022`, and the sealed `$OLD`/`$OLD/discovery` are 1770 `root:solana-desk`, giving `LIVE_external_*_dir:MODE_1770`. Accept the sealed shape (root owner, group = service group, sticky bit, no other-bits) as safe. Add an e2e that runs the REAL `_live_findings` after a real seal; no stubbed preflight `run`.
2. **(HIGH) Pacing schema upgrade.** The production pacing DB has no `pacing_reclaims` table, so T23F orphan recovery is inert. Add a RUNBOOK step next to 7a2 that runs `python -m desk.provider_pacing --upgrade` (check the exact CLI on the merged tree) with the same quiesce rule, `runuser -u solana-desk`, cwd `$REL`, and a one-way-door note. Test it in the e2e against a production-shaped pacing DB that includes the Kraken receipt.
3. **T36 composition.**
   - Keep ONE pacing-policy step: T32H's `### 7a2`. Drop T36's `## 6b` hunk.
   - `DEFAULT_WRITERS` must include `desk-paper-monitor.*`, `desk-decisions.service`/`.timer`, `desk-backup.*` and `desk-healthcheck.*`, plus everything else that sets `DESK_PROVIDER_PACING_DB`. Derive the list from the rendered units, not by hand, and test that.
4. **(MED) Seal the evidence files.** Seal the ~40 non-sqlite evidence files in `$OLD` (`acquisition-*.json`, `paper-target-*.json`, `*intake*.json`, `*.jsonl`) as well: default seal patterns include `*.json` and `*.jsonl`. Still NEVER seal lock files.
5. **(LOW) Rotate manifest.** The rotate step uses a NEW seal manifest path (an existing one is refused).
6. **(LOW) Rotate seal mode.** A rotate seal of `$NEW` gives 0550 (not 1770); `shared_dirs` must not always include `--root`.
6b. **Reports directory.** RUNBOOK: create `<STATE_DIR>/reports` (owner solana-desk) before enabling the daily-report timer (T41/T42).
7. **(LOW) Test portability.** `SealArchiveTests` must compare gid against the parent directory's gid, not `os.getegid()`: on macOS, files under `/private/tmp` get gid 0.

Run all `tests/test_ops_*.py` in ONE process (real exit code), with `TMPDIR` both under `/private/var/folders` and under `/private/tmp`.

---

## T22I — T22H regression in the retirement-receipt modules + small hold gaps — CRITICAL PATH
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T22H, then merge origin/cloud/T23H (removes the unrelated pacing-clock noise)
OWNS: desk/history_progress.py (classification only), the T22H files, tests
AVOID: desk/provider_pacing.py (T38)

Coordinator review of T22H: every safety probe PASSES. Integrity causes HOLD through the real history path and history_first; transients close; the dispatcher holds durably; a dead dispatcher with only transient originals is ABANDONED. Blocking regression: 63 errors in the reviewed retirement-receipt modules:
- `test_dispatch_preparation_retirement` (21)
- `test_paper_preparation_retirement` (28)
- `test_intake_uncaptured_retirement` (10)
- `test_captured_intake_decode_no_entry`
- `test_migration_recovery_lineage`
- `test_pre_entry_abandonment`
- `test_empty_history_successor_lifecycle`

They fail with "Exact sixth unresolved reservation" (`desk/paper_dispatch_preparation_retirement.py:184`) and "Exact initial failed preparation reservation required" (`desk/paper_preparation_retirement.py:186`).

Cause: `HistoryProgress.advance` no longer sets RETRYABLE_ERROR when a failure has no transient original, but `PaperHistorySource` itself raises `PaperReadError('DEADLINE_EXCEEDED')` after the reservation and BEFORE any transport call, so there is no original.

1. **Fix.** Treat a TYPED pre-transport transient raised by the desk's own source (`DEADLINE_EXCEEDED`, and any other code the source raises before transport that is in the transient vocabulary) as RETRYABLE_ERROR. Keep every integrity cause HOLDing; re-run the coordinator probe table (TLS, UNCLASSIFIED, bare OSError, digest conflict, HISTORY_BINDING_INVALID, Cached request mismatch → HOLD; timeout/reset/429/502/pacing busy → FAILED_CHARGED) as committed tests. Do NOT edit the receipt modules' proofs.
2. **(MED) Hold-file write failure.** If `_hold_dispatch` cannot write its file (OSError), the intent must not be abandonable later. Fall back to a second durable mechanism (a journal row in the same DB, or `fsync` of a sibling sentinel), and if both fail, refuse further dispatch (fail closed) with a visible reason.
3. **(MED) Swallowed exceptions.** An exception inside `classify` or `_close_dispatch` in the dispatcher's except handler must write a hold, not be swallowed.
4. **(LOW) Sentinel check.** `recover_abandoned` uses `os.path.lexists` for sentinels, like the dispatcher does.
5. **(LOW) Closure consistency.** The closure `_consistent` checks match publish's for `FEATURE_HISTORY_PAGE_LIMIT`: 8 retained `getTransactionsForAddress` originals, and `==18` where publish uses `==18`.

**Acceptance:** all 7 modules above plus the T22H test set are green in ONE process (real exit code) under both `TMPDIR=/private/var/folders/...` and `/private/tmp`. Also run `test_empty_history_successor_lifecycle`; its only allowed failure is the T38 pacing-clock one, if T38 is not merged yet, and you must say so.

---

## T22H — Close the last two allow-list holes in T22G (history laundering, dispatcher abandonment) — CRITICAL PATH
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T22G (it merges cleanly onto integration/r1)
OWNS: the T22G files, desk/history_progress.py (error classification only), desk/paper_history_preparation.py, tools/paper_entry_dispatcher.py (recovery of unresolved intents only), tests
AVOID: desk/provider_pacing.py (T38)

Coordinator review of T22G: items 1, 3, 4, 6, 7 and 8 PASS, and the T12 R1/R2 tests flipped. Blocking:
1. **(HIGH) History laundering.** `HistoryProgress.advance` turns ANY ValueError/OSError/KeyError/TypeError/IndexError from the history source into RETRYABLE_ERROR, which the cycle then raises as `HISTORY_RECOVERY_REQUIRED`, and that is ALLOW-LISTED. A probe through the real `run_once` showed that TLS_ERROR, UNCLASSIFIED_ERROR, "History page digest conflict", HISTORY_BINDING_INVALID, a bare OSError and "Cached request mismatch" are all closed FAILED_CHARGED with the scan retired. The same happens in history_first `prepare`.
   - Fix: `HISTORY_RECOVERY_REQUIRED` becomes an evidence-bearing cause. It closes only when the retained history attempt original proves a TRANSIENT failure (timeout/reset/429/5xx/pacing contention, the same vocabulary as T25F). Everything else goes to INTEGRITY_HOLD. Also narrow `HistoryProgress.advance`'s catch-all the same way T25F did for reads.
   - Rewrite T22G's "digest conflict" test to raise through the REAL history path, not `source_factory`. Update `test_history_failure_..._closed` (bare OSError must HOLD).
   - Add probes as tests for every cause listed above.
2. **(HIGH) Dispatcher abandonment.** A dispatcher intent has no hold record, so `_recover_unresolved` cannot tell a dead dispatcher from a deliberate refusal. `ACQUISITION_RETRY_OR_EVIDENCE_BLOCKED` and a TLS_ERROR during acquisition both become ABANDONED_CHARGED after 900s and dispatch resumes.
   - Fix: write a durable dispatcher hold record (same mechanism as pass holds: transactional or fsynced sentinel) whenever the dispatcher refuses on purpose or stops on an integrity cause.
   - `_recover_unresolved` abandons ONLY when there is no hold AND the owner proof says the owner is gone AND the intent's own attempt originals (if any) show only transient causes.
   - Test both probes.
3. **(M1) macOS tests.** The two `DispatcherClosureTests` kill/abandon tests fail on macOS. Inject the lock table as T25F does, so they pass on macOS and Linux.
4. **(M2) Lifecycle fixture.** `test_ops_fresh_start_lifecycle` regressed because its fixture uses `SYNTHETIC_PRODUCER_BLOCK`, which R1 now correctly holds. Switch the fixture to `MISSING_WINDOW_MEASUREMENT:net_buy_ratio`.
5. **(LOW)**
   - L1: `paper_history_preparation.py` uses `sqlite3.Error` without importing sqlite3, so a NameError leaves a NULL pass without a hold.
   - L2: move "Captured history changed" inside the held try.
   - L3: `recover_abandoned` must check sentinels over the SAME ordered set as `pending`.
   - L4: write the hold (or sentinel) BEFORE saving the result, or in the same transaction, so a kill between them cannot lead to abandonment.
   - L5: apply publish's `_consistent` checks in the closure path too.

**Acceptance:**
- the T22G test set plus every audit/review module is green in ONE process on macOS (lock table injected) with the real exit code;
- every probe above is a committed test;
- the remaining `expectedFailure` list is unchanged except for any this fixes.

---

## T22G — Fix the coordinator review of T22 (MUST be reconciled with T25F and T22F) — CRITICAL PATH
STATUS: OPEN
DEPENDS: branch `cloud/T22F` has a `DONE T22F:` commit
BASE: origin/cloud/T22F, then merge origin/integration/r1. Expect conflicts in desk/monitoring_budget.py, desk/paper_cycle.py, tools/paper_entry_dispatcher.py and the audit tests. A reference resolution of T22 onto integration is at the coordinator's scratchpad and is described below.
OWNS: the T22/T22F files, desk/monitoring_budget.py (reconciliation only), desk/paper_pass_closure.py
AVOID: tools/ops/**, T37 files

The coordinator review of T22 (with T22F not yet visible) found:
1. **Reconcile with T25F (already on integration).** KEEP T25F's monitoring latch rules: 401/403, RESPONSE_*/malformed, TLS_ERROR and UNCLASSIFIED_ERROR still latch, and only the known network exceptions are transient.
   - Keep T25F's `paper_monitoring_abandoned_v1` schema and `_abandonable`/`_resolve_abandoned` owner proof (`/proc/locks` plus deleted-inode check, injectable lock table).
   - Drop T22's competing abandon schema and expose an adapter if T22 needs one.
   - Rewrite T22's monitoring tests (held_exit_robustness 401/403, monitoring_budget malformed, the two paper_cycle monitoring tests, and the F1 monitoring test using a bare OSError → `TimeoutError`) to match T25F's rules, and inject the lock table so they run on macOS.
2. **(HIGH) Allow-list, not deny-list.** `cause_of` (`paper_pass_closure.py` ~:70-80) closes any ValueError as FAILED_CHARGED unless it contains a deny marker, so integrity failures get closed and silently un-latched. Verified examples: `quote envelope binding`, `mint binding`, `pool envelope binding`, `retained evidence unavailable`, `History page digest conflict`, `Captured history changed`, `TLS_ERROR`, `UNCLASSIFIED_ERROR`, `COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID`. Invert it: only an explicit ALLOW-list of transient/normal causes (network timeouts and resets, 429/5xx, pacing contention, typed normal blockers) may close as FAILED_CHARGED. Everything else stays latched (INTEGRITY_HOLD). Add a test for every example above.
3. **(HIGH) Nested blockers.** `_close_unfinished` (`paper_cycle.py` ~:431) checks only top-level blockers, so a `MARKET_PRODUCER_BLOCKED` with integrity codes nested in its diagnostics, whose no-entry publish was refused by the R1 allow-list, is closed FAILED_CHARGED. That undoes R1. Include diagnostics in the check: any non-allow-listed nested code means INTEGRITY_HOLD.
4. **(MED) Owner proof.**
   - `_lock` opens with `'a'` (O_CREAT), so a deleted-and-recreated lock file lets `recover_abandoned` close a live pass. Use T25F's owner proof (`/proc/locks` plus the deleted-inode check); a missing or recreated lock file means owner unknown, so nothing is closed.
   - The dispatcher's `.dispatcher.lock` plus 900s wall clock has the same issue; fix it the same way.
   - Add an `outcome_hash IS NULL` guard to the COMPLETE update (~:756).
5. **(MED) Hold write.** If the INTEGRITY_HOLD write fails, the pass must not later be closed ABANDONED. Persist the hold first, in the same transaction as any charge record, or treat "no hold record + integrity cause unknowable" as a hold.
6. **(MED) No new hard caps.** The new 8192-row hard stop (`paper_pass_closure.py:36`) must become a rotation warning at 80%, like R4.
7. **History-first handler.** An exception inside the `PreparationRejected` handler (`paper_history_preparation.py` ~:78-81) must not fall through to ABANDONED; treat it as a hold.
8. **T22F items.** If they are still not done after T22F, finish them: R1 allow-list, R2 per-scan page index (no global 4096/256MB bound), R3 visible `publish_refused`, R4 rotation warning, R5, and the addendum page cap.

**Acceptance:**
- All T09/T12 audit modules, every module listed in T22, `test_empty_history_successor_lifecycle`, and all monitoring modules are green with the real exit code on macOS AND Linux semantics (inject lock tables).
- These `expectedFailure` flips hold: F1 dispatcher, history_first, held monitoring, dust/zero quote, T12 R1/R2.
- Report precisely which audit tests remain expected failures and why.

---

## T22 — Extend T01: no store-wide latch from ANY charged failure (from T09 audit F1, F3) — CRITICAL PATH
STATUS: OPEN
DEPENDS: branch `cloud/T01` has a `DONE T01:` commit
BASE: origin/cloud/T01
OWNS: the same files as T01, plus tools/paper_entry_dispatcher.py (post-intent failure handling only)
AVOID: tools/ops/**

First read `reports/T09.md` on branch `origin/cloud/T09`, and its tests `tests/test_audit_first_cycle_*.py`, which are already merged to `integration/r1`. T01 covers typed `CycleBlocked` entry rejections only. The T09 audit proved the store still latches globally (`OBSERVATION_RECOVERY_REQUIRED`) after:
- **(F1)** `ObservationError` or `PaperReadError` (HTTP 429/5xx) escaping `run_once` after a charge; a timeout or SIGKILL mid-pass; history-preparation transient errors;
- **(F1-held, worst)** held passes that charged requests and then failed (dust or zero sell quote, transient read). The open position can then NEVER be sold;
- **(F3)** any dispatcher failure after `_write(intents)` (PROVIDER_RETRY_REQUIRED, SEED_HISTORY_UNAVAILABLE, BUSY, limits, kill). This leaves "Unresolved dispatch; no retry" forever.

**Required design.** A failed pass must leave the store usable:
- A pass that ends without a verified result gets a terminal `FAILED_CHARGED` outcome (typed, replay-verifiable, append-only, charges retained). This applies to a crash or kill too: on the next start, a stale NULL pass older than its deadline whose owning process/lease is gone is closed as `ABANDONED_CHARGED` by a deterministic, bounded recovery step. It must NOT be closed by a reconciliation receipt pinned per incident.
- Only true integrity violations (corrupt evidence, contradictory records, identity mismatch) stay latched (category b).
- The latch must be per-scan/per-position where possible, not global. A failed entry scan must not block held monitoring of open positions, and a failed held pass must not block the next held pass for the same position.
- Dispatcher: an unresolved intent older than its deadline is closed as `FAILED_CHARGED`/`ABANDONED`. The SAME candidate is never retried, but new candidates proceed.

Remove the `expectedFailure` decorators from the T09 tests that this fixes; they must pass. Keep every other T09 test as it is.

**Tests:** each F1/F3 scenario in T09, plus SIGKILL at each stage (before the first charge, after a charge, after the result write), a cold restart, and no double charge. Run `test_empty_history_successor_lifecycle` and the T01 modules.

---

**Coordinator addendum (T01 review, 2026-10-11) — also in T22's scope:**
- **(HIGH) Store-wide page cap latch.** `desk/paper_cycle_no_entry.py` `_index` (~:112-116) counts ALL evidence `pages` and raises above 4096 pages or 256 MB. `run_once` maps that to `OBSERVATION_RECOVERY_REQUIRED`, which latches the whole store after roughly 200 rejections (about a day). Index attempts per scan, never scan the whole store, and never latch on a count bound. Coordinate with T24 F7, which has the same pattern in `history_preparation_rejection`.
- **(MED) Producer diagnostics allow-list.** `MARKET_PRODUCER_BLOCKED` accepts any non-empty producer diagnostics (`_check_result` ~:152-155). Binding/integrity producer codes (`COORDINATOR_TARGET_BINDING_MISMATCH`, `COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID`, `SOL_USD_TRUSTED_INPUT_INVALID`, ...) must stay category (b). Add an explicit allow-list of normal producer codes; anything else stays latched.
- **(MED) `history_first` path.** `run_once` calls `paper_history_preparation.prepare` (`paper_cycle.py` ~:493-498). A `CycleBlocked` from `_history` (FEATURE_HISTORY_PAGE_LIMIT, budget exhaustion) escapes and leaves prepare's NULL pass. Cover it.

---

## T23 — EXIT_ONLY stickiness + orphaned pacing slot (from T09 audit F5, F6)
STATUS: OPEN
DEPENDS: branch `cloud/T08` has a `DONE T08:` commit
BASE: origin/cloud/T08
OWNS: desk/engine.py (the mode transition only), desk/provider_pacing.py, related tests
AVOID: T01/T22 files

Read `reports/T09.md` (branch `origin/cloud/T09`) first.
- **F5.** After a stale-mark `exit_blocked`, the mode goes to `EXIT_ONLY` and never returns to `RUNNING`, so entries stop silently forever after the first trade. Make the mode return to `RUNNING` automatically once no position has an unresolved `exit_blocked`. This must be versioned if it changes engine semantics, and every risk/daily pause still holds. If T08 already fixed this, verify it and only add tests.
- **F6.** A pacing slot `pending` orphaned by a killed process blocks every provider call forever. Add a bounded, deterministic reclaim: a pending ticket older than N×interval, whose owner PID/lease is gone, is released with a recorded, append-only reclaim row. Keep the shared two-second Kraken pacing exact, never reset the pacing state, and never let two holders hold a slot concurrently.

Fail-first tests, including the T09 RED tests for F5 and F6. Remove their `expectedFailure` once they are fixed.

---

## T24 — Throughput/latency headroom (from T09 audit F7, F9, F14)
STATUS: OPEN
DEPENDS: branch `cloud/T22` has a `DONE T22:` commit
BASE: origin/cloud/T22
OWNS: desk/history_preparation_rejection.py, desk/monitoring_budget.py (accounting only), desk/paper_cycle.py (deadline only), related tests
AVOID: tools/ops/**

- **F7.** Gate/replay cost grows O(R×P) and hard-latches past 4096 pages. Make the gate incremental (verify each retained item once and cache the verified digest in an append-only table, or bound the work per call) so the per-call cost stays roughly constant over a month of 24/7 operation. Replace hard latches on counts with rotation guidance (alert at 80%).
- **F9.** The whole-pass deadline of 10s vs real latency: measure what the budget actually needs (pacing 2s × requests + RTT). Make the deadline a versioned config value with a safe default, keeping the price TTL semantics intact. A deadline overrun must NOT latch (T22).
- **F14.** Monitoring accounting cost grows with history. Make it incremental (running totals per window) so it stays constant over time.

Benchmark before and after with fixtures sized for 7 days of 24/7 operation, and include the numbers in the report.

---

## T06F — Fix coordinator review findings in T06 (cycle verifier)
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T06
OWNS: tools/ops/verify_cycle.py, tests/test_ops_verify_cycle.py
AVOID: desk/**

1. **Immutable opens.** `immutable=1` is only allowed when the DB header bytes 18/19 == 2 (WAL) and there are no sidecars. Otherwise use plain `mode=ro`. This matters because `provider-pacing.sqlite` and the dispatcher journals are rollback-journal stores and are live.
2. **Monitoring budget store.** Read the monitoring budget from evidence.sqlite (`paper_monitoring_*` live in the EvidenceStore; see desk/monitoring_budget.py:44-47,155), not research.sqlite. Build test fixtures with the REAL EvidenceStore/MonitoringBudget APIs, not hand-written tables.
3. **`--allow-progress` prefix.** It must verify the exact old prefix: record the digest at the exact old row count (or the old final), so a rewrite of any old row is detected beyond 20k rows. Add a test with more than 20k rows that rewrites the last old row.
4. **Concurrency tests.** Add known-answer tests for concurrent positions (buy A, buy B, sell B, sell A) and for a take-profit ladder of partial sells.
5. **Per-sell PnL.** Check each sell's realized PnL against a proportional cost basis (`cost_left*qty/qty_held`), not only the aggregate.
6. **Volatile pacing fields.** Exclude them (`next_at`, `blocked_until`, `pending`, waiters) from strict compare. Report them separately; only `high_water` must not decrease.
7. **Duplicate-fill key.** Make it a tuple or JSON (event_id, side, mint), not a concatenated string.

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
- **Candidate supply (T09 F15).** `_select` reads only the newest 500 `raw_events`; at high discovery volume the eligible window shrinks to about 60s. Make the selection window-based (by age), not count-based.
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
3b. **Fold in the T09 audit F8/F11** (read `reports/T09.md` on `origin/cloud/T09`):
   - Wall timeouts must cover the code's worst-case budgets (entry ≥ 600s, held ≥ 120s, or the measured budget plus a margin).
   - Every scheduler unit sets `DESK_PAPER_SCHEDULER_IDENTITY` and `DESK_PROVIDER_PACING_DB`.
   - `paper-scheduler.lock` is pre-created (tmpfiles.d or ExecStartPre).
   - The entry unit has `--execute` and systemd credentials.
   - Held/entry/expire all use the SAME fresh config and ledger, with no `--dependency-blocker`.
   - Every timer has an `[Install]` section.
   - Add a test that renders every unit and checks these invariants.
4. **Tests:** fake systemctl, fixture stores, staleness thresholds, rate limiting, and the notifier never logging secrets.
5. **`docs/ops/OPERATIONS_24x7.md`:** the alert meanings and the operator response for each.

---

## T25 — Monitoring latch classification follow-up (from the T08 review)
STATUS: OPEN
DEPENDS: none
BASE: integration/r1 (T08 is merged there)
OWNS: desk/monitoring_budget.py, desk/paper_read_sources.py (error classification only), related tests
AVOID: T01/T22 files, desk/engine.py

T08 stopped `SOURCE_FAILURE` latching for some transient errors. Remaining work:
1. **Common transient held-read failures still latch.** Treat these as transient (non-latching, still charged):
   - JSON-RPC errors from Helius (`RPC_ERROR`, e.g. "block not available", internal errors);
   - pacing contention (`PACING_DEADLINE_EXCEEDED`, `PACING_DATABASE_BUSY`, `PACING_QUEUE_FULL`);
   - `HTTP_REJECTED` with a None status from `CoordinatorRPCError`;
   - `KRAKEN_PROVIDER_ERROR`.

   Keep 401/403, `RESPONSE_*` integrity codes and persistence failures latching.
2. **Catch-all is now silent.** T08 put the `except Exception` catch-all (paper_read_sources.py ~:234/:267/:330/:95) into the non-latching bucket. Narrow it again: only known network exceptions are transient (timeouts, connection reset/refused, DNS, 5xx, 429). TLS certificate failures, ValueError, AttributeError and other programming errors must LATCH, so a bug or a MITM symptom cannot silently burn the allowance.
3. **`MONITORING_OUTCOME_PENDING` after a kill.** It stays forever. Add a deterministic, bounded, append-only resolution: a pending reservation older than its deadline whose owner lease/PID is gone is resolved as `ABANDONED_CHARGED`. It stays charged and is never refunded.

Fail-first tests for every code path. Un-mark the T09 test `test_process_killed_after_reservation_must_not_block_forever`.

---

## T26 — Counterfactual candidate tracking (learn selection without trading)
STATUS: OPEN
DEPENDS: none
BASE: integration/r1
OWNS: tools/research/counterfactual.py, tests/test_counterfactual.py, deploy/fresh/desk-counterfactual.service (template), docs/research/COUNTERFACTUAL.md
AVOID: desk/** (import only)

Goal: know whether the filters pick winners. Record the forward price path of EVERY candidate (admitted, rejected, not dispatched) for learning, not trading.
- **Its own store**, `counterfactual.sqlite` (append-only), under the fresh root. For each migration hint in `discovery/continuous.sqlite` (read-only), record the candidate identity (mint, pool, migration slot/time) and, when available, its dispatcher/decision outcome and rejection codes (read-only joins on the journal and decision stores).
- **Price path.** Sample the pool's reserves (PumpSwap constant product, so the price comes from the vault balances) at +5m, +15m, +30m, +1h, +2h, +6h after migration. Use batched `getMultipleAccounts` against the vault accounts (one request covers many candidates).
  - Give it its OWN request allowance (default 300 requests/hour, configurable, recorded) and honour the shared provider pacing.
  - It must NEVER consume the entry or monitoring budgets. Failures are recorded and charged.
- **Derived metrics:** max gain, max drawdown, return at each horizon, liquidity at each horizon, and whether the pool died.
- **Report** (read-only CLI): for each rejection code, the forward-return distribution of the tokens rejected for that reason vs the admitted ones. In plain terms: "does this filter reject winners?"
- **Constraints:** paper/research only, no keys in the repo (systemd credentials like the others), fail closed on a malformed provider response, and no use of these prices for ledger fills.
- **Tests:** fixtures for the sampling schedule, batching, allowance enforcement, the PumpSwap price math against known reserves, the report's known answers, and a cold restart resuming the schedule without duplicate samples.

---

## T27 — Shadow strategy variants (parallel virtual A/B)
STATUS: OPEN
DEPENDS: branch `cloud/T26` has a `DONE T26:` commit
BASE: origin/cloud/T26
OWNS: tools/research/shadow_strategies.py, tests/test_shadow_strategies.py, config/experiments/shadow/*.json, docs/research/SHADOW.md
AVOID: desk/** (import only)

Evaluate many entry/exit parameter sets at once on the same candidate stream without extra requests.
- **Inputs:** T26 counterfactual price paths and candidate features, plus the real decision/observation evidence where available (read-only).
- **Simulation:** for each variant config (a versioned JSON grid: stop fraction, trailing, time-stop, max hold, take-profit ladder, mcap/liquidity/age windows), simulate entries and exits by calling the REAL `desk.engine` decision functions (`transition`/`manage_position`) on synthesized events. Do not reimplement the logic. Apply realistic costs: the fee, 50 bps slippage, and optionally T14 latency drift if present.
- **Coarse paths.** The price path is coarse (horizon samples), so state clearly which exits can be resolved (path-dependent stops are approximated with min/max between samples; mark the uncertainty) and report intervals, not point estimates, when ordering inside a window is ambiguous.
- **Output:** a leaderboard per variant (trades, net PnL after costs, win rate, max drawdown, bootstrap CI), a train/holdout split by time, and multiple-comparison caution: show how many variants were tried and apply a simple correction or holdout-only ranking. Label everything SIMULATED_SHADOW, never trading evidence.
- **Tests:** known-answer paths, an engine-call parity check (a variant equal to the live config reproduces the live engine decisions on the same events), and no look-ahead (no variant decision uses samples after its decision time).

---

## T28 — Event-driven held-position watcher (fast exits)
STATUS: OPEN
DEPENDS: none
BASE: integration/r1
OWNS: tools/ops/held_watcher.py, tests/test_held_watcher.py, deploy/fresh/desk-held-watcher.service (template), docs/ops/HELD_WATCHER.md
AVOID: desk/** (import only), the engine's exit logic

Memecoins move -30% in seconds; timer-polled held passes react too late. Build a watcher that keeps the engine authoritative:
- **Subscription.** For each open position in the fresh ledger (read-only), subscribe to its pool vault accounts over the Helius websocket (`accountSubscribe`), with an HTTP polling fallback of 1–2s via `getMultipleAccounts` within its own allowance.
- **Trigger.** Compute the implied mark from the vault reserves. When it crosses any engine exit threshold (read thresholds from the frozen config and the position state: stop, trailing, take-profit rung, time-stop/max-hold timers), immediately trigger the normal held pass: `systemctl start desk-paper-held-cycle.service`, via an injectable runner and a polkit/sudo-free method — document the unit setup. The watcher NEVER writes the ledger and NEVER fills. The held pass fetches the executable quote and decides.
- **Rate limits.** Debounce, a max trigger rate per position, and its own request allowance and pacing. Reconnect with backoff. On losing the stream, fall back to polling and raise a health warning.
- **Measurement.** Record trigger latency (event slot time → trigger → held-pass fill time) for T14-style analysis.
- **Tests:** a fake websocket feed; threshold crossings for stop, trailing and take-profit; debounce; reconnect; the fallback; and no ledger writes (byte-identical ledger).

---

## T29 — Funnel report (where candidates die)
STATUS: OPEN
DEPENDS: none
BASE: integration/r1
OWNS: tools/research/funnel_report.py, tests/test_funnel_report.py
AVOID: desk/**

A read-only CLI plus a static HTML output (a single file, no external assets). Per hour and day, show the counts at each stage: migrations discovered → selectable (age window) → dispatched → investigation admitted → history ok → observations ok → engine eligible → BUY. Break down the rejection codes at each stage, with budget usage per stage and median time per stage. Read from the discovery, journal, research, evidence, decision and ledger stores (`mode=ro`; immutable only for WAL with no sidecars, per the T02F rules). If T26 data exists, add each rejection code's forward-return summary. Tests use known-count fixtures.

---

## T30 — Market regime filter
STATUS: OPEN
DEPENDS: branch `cloud/T23` has a `DONE T23:` commit
BASE: origin/cloud/T23
OWNS: a new desk/regime.py, desk/engine.py (one additional entry gate only), tests/test_regime*.py
AVOID: T01/T22 files

Memecoins move together. Add a versioned, opt-in entry gate `paper_regime_version: 1` that reduces or blocks NEW entries in bad regimes, never exits.
- **Metrics from data already collected:**
  - graduation rate per hour (discovery store);
  - the median forward return of recent graduations, if T26 data exists;
  - the SOL/USD trend from the existing Kraken observations (no new provider).
- **States:** NORMAL / CAUTION (halve the size, or require a stricter score) / OFF (no entries), with hysteresis.
- **Determinism.** It must be computed from evidence recorded in the event (replay-deterministic), never from wall-clock reads inside the engine. When absent, behaviour stays identical.
- **Tests:** state transitions, hysteresis, a replay-determinism check, and absent-flag parity.

---

## T31 — Portfolio-level risk for concurrent positions
STATUS: OPEN
DEPENDS: branch `cloud/T16` has a `DONE T16:` commit AND branch `cloud/T23` has a `DONE T23:` commit
BASE: origin/cloud/T16 (merge in origin/cloud/T23 first; resolve conflicts carefully)
OWNS: desk/engine.py (portfolio gates only), tests/test_portfolio_risk.py, docs/PORTFOLIO_RISK.md
AVOID: T01/T22 files

Versioned and opt-in (`paper_portfolio_risk_version: 1`). With several positions open:
- **Aggregate open risk:** the sum of (position size × stop distance) ≤ X% of equity.
- **Loss streak:** after N consecutive losing exits, cool down for M minutes.
- **Entry spacing:** at most K new entries per rolling 10 minutes, so correlated pump bursts don't fill the book at once.
- **Daily caps:** the existing daily pause/liquidation still applies at the portfolio level.

Engine-authoritative and replay-deterministic. When absent, behaviour stays identical. Tests cover each gate, their interaction with max_positions/exposure, and a cold restart preserving streak and spacing state.

---

## T14F — Fill-realism redesign: measure OUTSIDE the trading pass
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T14
OWNS: desk/fill_realism.py, tools/research/fill_realism_report.py, a new tools/ops/fill_realism_worker.py, tests/test_fill_realism*.py, and the minimal hook in desk/paper_cycle.py
AVOID: every other line of desk/paper_cycle.py (T01/T22 are editing it); desk/paper_terminal_reconciliation.py

The coordinator review found the T14 design unsafe: it re-quotes with real sleeps inside the pass's `finally`, after the NULL pass row and before the outcome is saved.
- A kill in that window of 10s or more latches the store (T09 F1).
- Uncaught `sqlite3.Error`/`OSError` escape.
- The 10s pass deadline is bypassed by a fresh `_Budget` per sample.
- Locks are held for 20s or more.
- Sequential scheduling drops the +2s and +5s samples of later fills.
- Stale or cached quotes can be stored as "+10s" samples with ~0 drift, biasing toward "latency is free".

**Redesign:**
1. **The pass only enqueues.** The trading pass only ENQUEUES measurement jobs (append-only rows: fill identity, route, decision quote, `decision_at` with sub-second precision), *after* the outcome is persisted, in the same place `store.save` succeeds. It must never sleep, re-quote or run anything that can raise inside the pass. The hook into paper_cycle.py is at most a few lines; list them exactly.
2. **A separate worker runs the jobs.** `tools/ops/fill_realism_worker.py` runs as its own short-lived timer or long-running service with its own request allowance and the shared pacing. It schedules all samples by absolute due time across jobs (a priority queue), so several fills don't starve each other. It records LATE explicitly with the actual lag.
3. **Freshness.** A sample is valid only if the provider quote's own `observed_at >= decision_at + delay` and it is within a small tolerance. Otherwise record `REALISM_STALE_QUOTE` (excluded from stats, counted).
4. **Error handling.** The worker catches everything per sample (recorded, charged), and its failures can never touch the trading stores' NULL/outcome state. A worker crash leaves jobs pending; a restart resumes, without double-sampling and without double-charging.
5. **Byte-identical when off.** Prove it with a byte-for-byte ledger/events comparison against an r1-base run.
6. **Coverage.** If the allowance is missing or exhausted, report coverage explicitly per side (entry/exit) so latency-adjusted PnL is never silently computed from partial data.

Tests:
- a kill/exception during measurement leaves the trading store unaffected;
- a stale-quote rejection;
- concurrent fills keep their +2s and +5s samples;
- restart idempotency;
- the off-path byte identity;
- the report's known answers.

---

## T16F — Make concurrent multi-position actually work (from the T16 review)
STATUS: OPEN
DEPENDS: none
BASE: integration/r1 (T16 is merged there, with the flag off and behaviour identical)
OWNS: the T16 files (tools/paper_scheduler.py, tools/paper_entry_dispatcher.py, desk/paper_concurrency.py, desk/paper_monitor_service.py, the held-mint gate in desk/paper_cycle.py), tests/test_paper_concurrency.py, docs/MULTI_POSITION.md, deploy/fresh/desk-paper-held-cycle.service (the `--wall-seconds` arg only)
AVOID: T22's reconciliation work; if you must touch the same paper_cycle.py lines as T22 (`origin/cloud/T22`), keep the change minimal and isolated

With the flag ON, T16 does not work. Fix each item with a test that runs the REAL pipeline timing (no manual mark refresh in the test):
1. **No concurrent entry can ever fill.**
   - Cause: the dispatcher writes `position_targets: []` for entry cycles (`paper_entry_dispatcher.py` ~:139/:703), so held marks are never refreshed. The engine then rejects with STALE_PORTFOLIO (marks older than 10s, `engine.py` ~:406) after history prep (≤18s), a 2s sleep and the quote (~8s), with the investigation requests already charged.
   - Fix: run the investigation first, then refresh ALL held marks in the same cycle immediately before the entry decision (charged to monitoring, within the 10s freshness).
   - The pre-I/O estimate in `paper_concurrency.py` (`ENTRY_SECONDS=8.0`) must include preparation, so doomed entries are refused BEFORE any charge.
2. **Entries delay exits.** The dispatcher holds the scheduler lease for up to 600s, so held ticks print SCHEDULER_BUSY. Held passes must always get through:
   - split the lease (a separate held lease), or
   - cap the entry work and release the lease between phases, or
   - run the held pass inside the entry tick at bounded intervals.

   Prove with a timing test that held-pass latency stays ≤ its cadence plus a small margin while an entry runs.
3. **Held-first covers only one position.** The `--wall-seconds 12` default gives floor(12/7.8)=1 leg. Size the legs to cover ALL open positions within the held unit's TimeoutStartSec (120s): 4 × 7.8s ≈ 31s. Pass it through the scheduler and the fresh unit.
4. **Day rollover.** With subset legs the engine defers rollover forever (`engine.py` ~:352-359), so `day_start_equity` and the daily loss counters go stale while 2+ positions are held. Fix by guaranteeing all-fresh marks at least once per rollover window (through item 3), or by a versioned engine rule. Test it.
5. **Restart.** The cold-restart test must cover monitoring charges and reservations across the restart, not only ledger replay.
6. **Docs.** Correct docs/MULTI_POSITION.md, including the false claims "exits are never starved" and "max_positions 2 is the useful setting" until they are true.

---

## T26F — Fix coordinator review findings in T26 (counterfactual)
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T26
OWNS: tools/research/counterfactual.py, tests/test_counterfactual.py, deploy/fresh/desk-counterfactual.service (+ a .timer if used), docs/research/COUNTERFACTUAL.md
AVOID: desk/**

Note: T27 (shadow strategies) is built on T26. After this lands, post the fixed classification and return semantics in the report so T27 can rebase onto them.

1. **Outcome classification is wrong**, so the "does the filter reject winners?" report is invalid. The code only reads `result.blockers`/`result.reason`, so:
   - dispatcher token rejections (`dispatcher_token_rejection_v1`, codes in `token_policy.reasons`) show as `ADMITTED:NO_RESULT`;
   - strategy-filter rejections from `entry.execute` (status COMPLETE, reasons in `outcomes[].reason`) show as `ADMITTED:COMPLETE`, the same as a real BUY.

   Parse every result shape, join the decision store (`paper-decisions.sqlite`, read-only) and the ledger fills, and classify as BOUGHT / REJECTED:<stage>:<code> / NOT_DISPATCHED / UNKNOWN. Add known-answer tests for each shape, built with the real writers where possible.
2. **Survivorship bias.** A pool that is POOL_DEAD at a horizon counts as -100% (or is reported in a separate "died" bucket that is included in the totals), never dropped.
3. **Fixed baseline.** Use one baseline for every candidate: the price at migration, or the +5m sample. If the baseline sample is missing, the candidate is excluded from the return stats and counted as missing; it never re-baselines to a later horizon.
4. **Service template.**
   - Its own store goes in its own subdirectory, and `ReadWritePaths` lists only that subdirectory plus the shared pacing DB (and its directory if SQLite needs it).
   - The discovery DB is read-only and works even when its `-shm` is missing: open with an immutable fallback only for a quiet WAL file (per the T02F rule), or document the needed ReadWritePaths.
   - Add a restart policy or a timer.
5. **Backfill.** On first start, begin at the current max discovery seq, or skip candidates older than 6h plus grace. Never backfill the whole history.
6. **Minor.**
   - A null pool account is retried with backoff until the horizon window passes.
   - `refresh_outcomes` reuses one journal connection and only processes candidates without a final outcome.
   - `set-allowance` range-checks its input (1..3600).

---

## T27F — Fix coordinator review findings in T27 (shadow strategies)
STATUS: OPEN
DEPENDS: branch `cloud/T26F` has a `DONE T26F:` commit
BASE: origin/cloud/T27, then merge origin/cloud/T26F into it first
OWNS: tools/research/shadow_strategies.py, tests/test_shadow_strategies.py, docs/research/SHADOW.md, config/experiments/shadow/*.json
AVOID: desk/**, tools/research/counterfactual.py (T26F owns it)

1. **True bracket.** The [worst, best] interval is not a true bracket. Between samples, assume nothing about the path except what is stated, and make the bracket cover:
   - a stop crossed and recovered between samples;
   - ladder rungs hit between samples (including several rungs at once on a gap);
   - an unknown peak for the trailing level.

   Use explicit min/max reasoning per gap. Mark every trade whose outcome depends on intra-gap ordering as AMBIGUOUS, and report the share of ambiguous trades per variant. Never present a "worst" that is not a worst case.
2. **Parity against real events.** The parity test must replay RECORDED journaled engine events (from the fixture ledgers the existing tests use, or real-shaped events built by the real writers) through the shadow path with the live config, and compare against the decisions the engine actually recorded. The current test is circular.
3. **Selection effects.**
   - Count, never silently drop: candidates with no priced sample, dead before +5m, FAILED/MISSED gaps, horizon_end, and truncated paths.
   - Require the +5m sample to enter; otherwise the candidate is excluded and counted.
   - Report the totals at each exclusion stage.
4. **POOL_DEAD.** Bracket the unknown death time (time-stop exits between the last OK sample and death are ambiguous). Adopt T26F's null-account retry semantics, so a transient VAULT_CLOSED is not counted as -100%.
5. **Ranking.**
   - Require a minimum trade count (default 30) to be ranked.
   - Rank by the Bonferroni-corrected bootstrap CI lower bound, holdout only.
   - Fix the CI index collapse with more variants: scale BOOTSTRAP with the variant count, or use a percentile with interpolation.
6. **Features.**
   - Join the T26F classification (BOUGHT vs REJECTED:<stage>:<code>) and split results by it.
   - `--features` must carry an `as_of` per candidate and refuse features observed after the decision time (no look-ahead); test it.
   - Make the neutral defaults (sol_usd 150, supply 1e9) explicit in the output.
7. **Carry timers.** Carry-timer events must not sidestep `price_ttl_seconds`. Document the behaviour, or mark those exits ambiguous.
8. **Opening stores.** `mode=ro` on a WAL store with a missing `-shm`: follow the T02F rule (immutable only for a quiet WAL).
9. **`migrated_at`.** Use T26F's on-chain migration time if it provides one. Otherwise label the age windows as "since hint receipt".

---

## T22F — Verify T22 covers the T01/T12 review findings; fix any gaps
STATUS: OPEN
DEPENDS: branch `cloud/T22` has a `DONE T22:` commit
BASE: origin/cloud/T22 (merge origin/integration/r1 in first; it contains T01, T12's RED tests, T08 and T16)
OWNS: the same files as T22
AVOID: tools/ops/**

T22 was claimed before the coordinator addendum was written. Read the "Coordinator addendum (T01 review)" under T22 in this file and `reports/T12.md` (on `origin/cloud/T12`), then make sure ALL of the following hold. Fix whatever T22 did not do.
- **R1 / addendum MED.** Allow-list the inner producer vocabulary of `MARKET_PRODUCER_BLOCKED` (`MISSING_WINDOW_MEASUREMENT:*`, `STALE_WINDOW_MEASUREMENT:*`, declared known hazards). Every retained diagnostic blocker must be in the allow-list, or the pass stays NULL. The T12 `expectedFailure` tests in `tests/test_review_t01_integrity.py` must pass with the decorator removed.
- **R2 / addendum HIGH.** Per-scan page indexing, with no global 4096-page or 256 MB bound in the no-entry gate or in publish. The `tests/test_review_t01_bounds.py` `expectedFailure` tests must pass with the decorator removed. Gate cost must stay roughly flat with store size; add a measurement assertion with generous bounds.
- **R3.** `publish` refusals swallowed in `run_once` must record a typed, visible `publish_refused` reason (log plus a queryable row or state file) that the healthcheck can read. Fail closed as before.
- **R4.** Replace the `MAX_ROWS=8192` hard stop with a rotation warning at 80%, plus a hard stop that leaves the store flat-rotatable, not mid-position-latched. Document this in the RUNBOOK rotation section.
- **R5.** Remove `RETAINED_MIGRATION_WITNESS_REQUIRED` from the allow-list, or constrain it to `MIGRATION_WITNESS_ABSENT` only.
- **Addendum.** The `history_first` path through `paper_history_preparation.prepare` must not leave a NULL pass for normal blockers (FEATURE_HISTORY_PAGE_LIMIT, budget exhaustion).

Run every T09/T12 audit module and report which `expectedFailure` tests remain, and why.

---

## T29F — Fix coordinator review findings in T29 (funnel report)
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T29, then merge origin/integration/r1 (it contains T01's `paper_cycle_no_entry`)
OWNS: tools/research/funnel_report.py, tests/test_funnel_report.py
AVOID: desk/**

1. **(HIGH) Latches counted as normal deaths.** Join `paper_observation_passes` (outcome_hash NULL vs set) and T01's `paper_cycle_no_entry` per scan_id. A BLOCKED result whose pass is still NULL and has no no-entry row is `UNRESOLVED:<code>` (a latch), never `OBSERVATIONS:<code>`. Show UNRESOLVED prominently at the top of the report. Add a fixture with a category-(b) blocker (e.g. `USD_ORIGINAL_BINDING_INVALID`) built through the real `run_once`/publish path where feasible.
2. **(MED) Selection filter.** Apply the dispatcher's own selection filter (`migration._hints('selection-only', …)`, `tools/paper_entry_dispatcher.py` ~:567; import it, don't copy it) so `EXPIRED_NOT_DISPATCHED` is not inflated.
3. **(LOW) Mint filter.** Filter fills and rejects by the candidate's mint, so held-position outcomes are never attributed to a candidate.
4. **(LOW) Docstring.** Fix the dead-pool docstring. Once T26F lands, align the dead-pool semantics with it (dead = -100% or a separate bucket counted in the totals).
5. **(LOW) Real-store fixture.** Add at least one dispatched-rejection fixture generated from real stores.

---

## T28F — Fix coordinator review findings in T28 (held watcher)
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T28
OWNS: tools/ops/held_watcher.py, tests/test_held_watcher.py, deploy/fresh/desk-held-watcher.*, docs/ops/HELD_WATCHER.md
AVOID: desk/**

1. **(HIGH) Trigger budgets.** Triggers that never resolve exhaust the rate caps, so a real STOP crash gets `TRIGGER_RATE_LIMITED` (:644-652).
   - Price reasons (STOP, TRAILING, TAKE_PROFIT) get their own budget, separate from time/mode reasons (MAX_HOLD, TIME_STOP, LIQUIDATE).
   - Do not refire a time/mode reason that the held pass already handled (detected from the ledger/state, e.g. a `blocked_exit` or `exit_blocked` recorded after the trigger). Back it off exponentially instead.
   - A price STOP must always be able to fire at least once per N seconds, regardless of other reasons.
2. **(HIGH) Same-slot reserves.** Evaluate only when the base and quote vault balances come from the SAME slot (:549-570). Otherwise a swap between the two notifications creates a fake price spike. Buffer per slot, and use `getMultipleAccounts` (one slot) for polling.
3. **(MED) Provider backoff.** After HTTP 429/5xx, back off exponentially with jitter. Document why the watcher does not use the shared pacing store (T09 F6), and make sure its request rate cannot starve the desk's own Helius calls (configurable cap, default ≤ 0.5 req/s, plus the backoff).
4. **(MED) Lost triggers.** A trigger can be lost while a held pass is running or on SCHEDULER_BUSY. Re-fire after a short delay (e.g. 5s) unless the ledger shows that a held pass completed after the trigger time.
5. **(MED) Latency attribution.**
   - Pair a trigger only with the first held-pass result or fill strictly after the trigger, within a bounded window.
   - Record the slot of the reserve observation and its block time (or a slot→time estimate) for the "event → trigger" latency.
6. **(LOW) Stream health.**
   - Detect a stalled stream (no notifications for N seconds while connected) and fall back to polling.
   - Keep the reconnect backoff across connects; reset it only after a stable period.
7. **(LOW) Write scope.** `ReadWritePaths` covers only `<STATE_DIR>/held-watcher`.
8. **(LOW) Mark accuracy.**
   - Include the PumpSwap creator fee and Token-2022 transfer fees in the mark where known, and state any remaining assumptions.
   - A position without `quote_execution` raises a health WARNING; it does not silently drop price triggers.
9. **(LOW) Trailing peak.** TRAILING uses the engine's recorded peak (from the ledger state), with the watcher's peak only as an early hint inside the margin.

---

## T33 — Regime evidence producer + T30 hardening
STATUS: OPEN
DEPENDS: branch `cloud/T22F` has a `DONE T22F:` commit AND branch `cloud/T23F` has a `DONE T23F:` commit
BASE: origin/integration/r1 (by then it contains T22F, T23F and T30; if T30 is not merged yet, merge origin/cloud/T30 first)
OWNS: desk/regime.py, a new desk/regime_producer.py, the minimal hook in the entry event builder (list it exactly), tests/test_regime*.py
AVOID: desk/paper_terminal_reconciliation.py; held/exit paths

T30's regime gate (opt-in, `paper_regime_version: 1`) has no producer: nothing fills `event["regime"]`, so turning the flag on rejects every entry (fail closed). Build the producer and fix the T30 review items:
1. **Producer.** Attach regime evidence to ENTRY events only, computed from data already collected:
   - the graduations/hour from the discovery store (bounded read, `mode=ro`, immutable only for a quiet WAL);
   - the SOL/USD change from the existing Kraken observations, with no new provider calls;
   - optionally the T26F counterfactual forward return.

   Requirements:
   - It must not create a new NULL-latch path: a producer failure yields missing evidence, which leads to a REGIME_EVIDENCE_REQUIRED reject (a normal, terminal no-entry), never an exception escaping the pass.
   - It charges no requests.
   - The evidence carries its `as_of`, and the engine's TTL check stays.
2. **(T30 issue 1)** `score()` can raise `decimal.Overflow` on huge or tiny values. Bound magnitudes in `validate_record` and catch ArithmeticError, mapping it to INVALID.
3. **(nit)** Validate the flag and policy config at load or initialize time, not at the first entry candidate.
4. **Tests:** the producer fills evidence deterministically from fixture stores; a producer failure becomes a terminal no-entry; the flag stays off by default; replay determinism holds.

---

## T26G — Two remaining classification bugs in the counterfactual (from the T26F review)
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T26F, then merge origin/integration/r1 (it contains T01's `paper_cycle_no_entry`)
OWNS: tools/research/counterfactual.py, tests/test_counterfactual.py
AVOID: desk/**

1. **(HIGH) Latches counted as filter rejections** (~:240). A BLOCKED `paper_cycle_v1` result becomes REJECTED:OBSERVATIONS:<code> only if its blocker is in T01's `desk/paper_cycle_no_entry.NORMAL` set (import it, don't copy it) or a `paper_cycle_no_entry` row exists for the scan. Everything else is `UNRESOLVED:<code>` and is shown separately. Add a category-(b) fixture (`USD_ORIGINAL_BINDING_INVALID`). Same rule as T29F item 1.
2. **(MED) Decision-store results finalised too early** (`_is_final` ~:342). A result from `source=DECISIONS` only becomes final after the dispatch window closes (same rule as NOT_DISPATCHED). A later journal or ledger BUY must override it (priority ledger > journal > decision). Add a test: a decision REJECT first, then a ledger BUY, gives BOUGHT.
3. **(LOW) Null-pool backoff.** Cap the retry backoff so it cannot overshoot the +5m window (300–420s). For example, retry at 30s intervals while inside a sample window.

After this lands, T27F (if it has already merged T26F) must merge T26G as well. Note this in the report.

---

## T14G — Final fill-realism fixes (from the T14F review)
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T14F, then merge origin/integration/r1. Resolve the import conflict at desk/paper_cycle.py:21 as `from . import engine, quote_execution as qe, paper_concurrency as concurrency, fill_realism`.
OWNS: desk/fill_realism.py, desk/quote_execution.py (config validation only), tools/ops/fill_realism_worker.py, tests/test_fill_realism*.py, and the minimal hook lines in desk/paper_cycle.py
AVOID: every other line of desk/paper_cycle.py

1. **(BLOCKER) The hook can raise inside the pass.** `fill_realism.selected(cfg)` raises FILL_REALISM_CONFIG_INVALID after `deliver`, which leaves the pass NULL and latches the store. `qe.config` skips the realism validation when `paper_quote_execution_version` is absent.
   - Validate the realism key unconditionally at config load, so a bad config is refused before any cycle.
   - Make the in-pass hook strictly non-raising.
   - Add a test: a bad realism config with no quote-execution key is refused at load, and the pass never latches.
2. **Freshness tolerance.** Our own receive clock is not provider time. Add an upper bound: a sample is valid only if its request started within a small tolerance of its due time (default ≤ 1.0s late) and finished within ≤ 2.0s. Otherwise it is LATE (excluded from latency-adjusted PnL, counted). Use the provider timestamp or `Age` when present. Document that Jupiter has no provider timestamp.
3. **Pacing priority.** Add a lower pacing priority (e.g. `research`) below `investigation`, used by the worker and the T26/T28 research services, so measurement never delays trading quotes. Minimal change to `desk/provider_pacing.py`; coordinate with T23F, which owns that file. If T23F is not merged yet, put the priority change in a separate small commit and say so.
4. **Early returns.** Place `enqueue` before the terminal-receipt and no-entry early returns (merged ~:688, :698), so fills in those passes are measured.
5. **Single worker.** Hold a single-instance flock for the worker. Move the attempt-row INSERT inside the try, so a duplicate becomes a recorded no-op, not a BLOCKED exit.
6. **Off-path identity.** Add a committed test that runs the same fixture pass on code with the flag absent and compares ledger, events and outcomes bytes. Mask only random IDs, and assert the mask list is exactly those fields.

---

## T32F — RUNBOOK must work against the REAL production systemd state — CRITICAL PATH
STATUS: OPEN
DEPENDS: none
BASE: integration/r1 (T32 is merged there)
OWNS: tools/ops/cutover.py, tools/ops/fresh_start.py, tools/ops/healthcheck.py, tools/ops/notify.py, deploy/fresh/**, docs/ops/RUNBOOK.md, docs/ops/OPERATIONS_24x7.md, tests/test_ops_e2e_deploy.py, tests/test_ops_cutover.py
AVOID: desk/**, tools/ops/status.py, tools/ops/preflight_dryrun.py, tools/ops/verify_cycle.py (T34 owns those)

**Ground truth from the production VPS (read-only inventory by the coordinator, 2026-10-11).** Each existing desk unit has a STACK of Codex-era drop-ins that load AFTER any 60-/70- drop-in and override it:
- `desk-backup.service.d`: 60-reviewed-release, 90-reviewed-73edf43, 95-reviewed-e4badab, 96-reviewed-b414dbd, 97-reviewed-continuation, 98-reviewed-extension, 99-profile2-reviewed
- `desk-dashboard.service.d`: the same as backup, plus zz-paper-scheduler-reviewed
- `desk-decisions.service.d`: the same as dashboard
- `desk-discovery.service.d`: 60-reviewed-release, 70-migration-sampling
- `desk-paper-entry-dispatcher.service.d`: zz-paper-scheduler-reviewed, zzz-local-validation-deadline
- `desk-paper-entry-dispatcher.timer.d`: 90-reviewed-cadence
- `desk-paper-held-cycle.service.d`: 70-reviewed-runtime, 90..99 (as above), zz-paper-scheduler-reviewed, zzz-local-validation-deadline, zzzz-full-cycle-wall-deadline
- `desk-paper-held-cycle.timer.d`: 70-reviewed-enable ([Install]), 90-reviewed-cadence
- `desk-paper-monitor.service.d`: 60-reviewed-release, 90..99, zz-paper-scheduler-reviewed

All timers and the dashboard are currently `disabled` (the coordinator disabled them). Only `desk-continuous-discovery.service` runs (static, not enabled).

Required:
1. **Drop-in archive.** Never layer on top of this. Add a `cutover archive-dropins` step that MOVES every existing `/etc/systemd/system/desk-*.d/` directory (and any `desk-*` unit files that will be replaced) into `/var/backups/solana-desk/systemd-archive-<UTC>/`, preserving content, modes and a manifest with sha256. Then install the fresh unit set as clean full unit files plus our marked drop-ins only. `rollback` restores the archive exactly (verify with the manifest). The e2e test MUST seed this exact production drop-in tree (names above, plus realistic ExecStart/Environment/WorkingDirectory overrides) and prove that after cutover `systemctl cat` (the fake) shows ONLY our configuration, and that rollback restores the original tree byte-for-byte.
2. **Inventory and backup.** Add an inventory step before anything else (`ls -la /etc/systemd/system/desk-*`, `systemctl cat` for each unit, saved to the backup dir). Fix the `cp -a` backup so it includes the `*.d` dirs.
3. **Old units.** `desk-discovery.timer/.service`, `desk-recorder.service`, the old monitor and held timers, and every old unit not in the fresh set must be `disable`d (and stopped) in step 2, so a reboot cannot restart anything against the archived stores.
4. **Archive quiesce.** The archive backup's `--require-quiesced` must include `desk-dashboard.service` and `desk-continuous-discovery.service`, i.e. stop discovery during the archive backup and restart it afterwards against the fresh discovery path or the shared discovery DB, whichever the DECISION says.
5. **Rotate.** The rotate procedure disables the entry timer, so the "reboot fails closed" claim becomes true.
6. **Dashboard pacing.** Give the dashboard unit `DESK_PROVIDER_PACING_DB`, so dashboard scans use the shared 2s pacing (T09 F11).
7. **Run as the service user.** Read-only tools in the RUNBOOK run as `runuser -u solana-desk --`, not as root.
8. **Archived store permissions.** The archived stores become `chown root:root`, files 0444 and dirs 0555 (except the shared `provider-pacing.sqlite` and its directory requirements; document exactly how pacing stays writable, e.g. by moving the shared pacing DB to its own directory `/var/lib/solana-desk-shared/provider-pacing.sqlite` with a symlink-free path, updating every unit, and recording the move). The healthcheck and notify units get `ReadOnlyPaths` for the old and fresh roots.
9. **Healthcheck watchdog.** A dead healthcheck timer must be detected within 15 minutes (a separate watchdog timer or a systemd `OnFailure=`), not only by the daily summary.
10. **Rollback docs.** Document that rendered unit files stay installed after rollback, or remove them.
11. **Unit file check.** The RUNBOOK includes `systemd-analyze verify` for every rendered unit, and the e2e test checks the systemd quoting of the backup unit's `python -c` line.
11b. **Research units.** The research templates merged later (`deploy/fresh/desk-counterfactual.*`, `desk-held-watcher.*`) still use `/var/lib/solana-desk/exp-FRESH` (inside the archived root). Move them to the fresh-root layout, and include them in the render/cutover flow as OPTIONAL units: off by default, and enabled only by an explicit RUNBOOK step.
12. **E2E realism.** The e2e must not turn `mkdir`/`cp`/`ls`/`is-active` into no-ops where avoidable. Run the real filesystem commands inside the sandbox root, cover stage and rotate, and include `--chown`, which is simulated by asserting the intended owner in a fake chown layer.

---

## T34 — Make status / preflight / verify_cycle work with the fresh layout
STATUS: OPEN
DEPENDS: none
BASE: integration/r1
OWNS: tools/ops/status.py, tools/ops/preflight_dryrun.py, tools/ops/verify_cycle.py, tests/test_ops_status.py, tests/test_ops_preflight_dryrun.py, tests/test_ops_verify_cycle.py
AVOID: desk/**, the T32F files

In the fresh layout, the shared stores live OUTSIDE the experiment root. The shared pacing DB is in the old root (or `/var/lib/solana-desk-shared/` if T32F moves it), and the discovery DB is shared too. Today:
- `preflight_dryrun` refuses absolute `--pacing-db`/`--discovery-db` and requires every store inside `--data`;
- `status` hard-codes `data/provider-pacing.sqlite`, so it always reports a pacing error;
- `verify_cycle snapshot --ledger <abs path>` fails because it only accepts plain names inside `--data`.

Add explicit `--pacing-db PATH`, `--discovery-db PATH` and `--ledger PATH` (absolute) options to all three, with the same canonical/no-symlink/owner checks as for in-root stores:
- preflight copies the external stores into its workdir too, and binds them in the namespace;
- status reads them `mode=ro` (immutable only for a quiet WAL);
- verify_cycle snapshots them.

The defaults stay backward compatible. Tests use a fixture layout that mirrors production (fresh root plus an external shared pacing/discovery DB). Show `status` reporting empty blockers on a healthy fresh layout.

---

## T25F — Fix the T25 review findings (monitoring classification)
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T25 (it merges cleanly onto integration/r1)
OWNS: desk/monitoring_budget.py, desk/paper_read_sources.py (classification only), tests/test_monitoring_classification.py
AVOID: other desk files

1. **Catch-all.** The `_read_chunked` catch-all (paper_read_sources.py ~:130-132) still maps any Exception to TRANSPORT_ERROR (transient). Route it through `classify_exception`, so programming errors (ValueError, AttributeError, ...) LATCH. Add a test.
2. **Missing lock file means owner unknown.** `_lock_holders` (monitoring_budget.py ~:74) treats FileNotFoundError as "owner gone". That is the same bug class as T23 F6: a live process can hold an unlinked inode. A missing or recreated lock file must fail closed (owner unknown, so no abandonment). Make the T09 test exercise the real owner check by creating the lock files in its fixture.
3. **macOS tests.** The two `/proc/locks` tests fail on macOS (test_monitoring_classification.py ~:318, :338). Make the lock table injectable or mockable so they run on both platforms, or `skipUnless` Linux with an explicit reason (and keep them running in Linux CI).
4. **None-status scope.** Narrow the None-status transient rule to `CoordinatorRPCError` specifically, or document why a broader rule is safe.
5. **Handoff after a kill.** Handoff context (allowance version 3) is never abandonable after a kill, so it blocks forever. Either support abandonment there with the same owner-gone proof, or make it a clear health CRITICAL with operator guidance.

---

## T20F — Fix the T20 review findings (watchlist)
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T20, then merge origin/integration/r1 (T01 and T16 are there)
OWNS: desk/watchlist.py, tools/paper_entry_dispatcher.py (selection only), tests/test_watchlist*.py, docs/WATCHLIST.md
AVOID: T22/T22F reconciliation work; T16F's `scan is None` guard (keep it)

1. **(HIGH) Empty-window rejections are never NOT_YET.** `reason_codes` folds in `diagnostics[*].blockers` such as `MISSING_WINDOW_MEASUREMENT:*` (from paper_market_adapter ~:88, and required by T01's no-entry proof), so the case always classifies PERMANENT. Map the inner producer codes explicitly:
   - `MISSING_WINDOW_MEASUREMENT:*` and `STALE_WINDOW_MEASUREMENT:*` → NOT_YET;
   - integrity/binding producer codes → PERMANENT, the same allow-list as T22F R1.

   Add an end-to-end test using the real T01 result shape: an empty window at t0 is re-evaluated at t0+10m and admitted with new fixture data.
2. **(HIGH) Full-table scans every tick.** The age-window query scans the whole shared discovery table (`received_at` has no index; `ORDER BY seq DESC LIMIT 20000`). The benchmark showed 7s per tick at 600k rows, and it grows forever. Walk seq DESC and stop at the first `received_at < now - maximum_age`, or bound by a seq range kept in a cursor. The 20000 cap must not truncate a 6h window at 5000/h. Add a benchmark assertion with generous bounds on ≥500k rows.
3. **(MED-HIGH) New latch path.** A near-expiry pick trips `Candidate expired during preparation` after the intent is written, which leaves an unresolved intent and stalls the dispatcher. Require a preparation margin (e.g. ≥ 900s before `expires_at`) both when scheduling and when selecting. Test the expiry-during-preparation case.
4. **(MED) Filter by candidate.** `reason_codes` must filter outcomes by the candidate's mint and diagnostics by its scan_id, so held-position rejects (T16F) do not taint the classification.
5. **(MED) Fresh selection age.** In v2, fresh selection admits hints up to 21600s old instead of 7200. Revert fresh selection to the configured dispatch window (7200). Only the watchlist uses the longer engine window. Document this.
6. **(LOW) Bounded tables.**
   - Account for re-evaluations consuming the bounded tables (512 preparation rejections, 8192 no-entry rows). Stop scheduling re-evaluations when a table is ≥80% full, and raise a health warning.
   - Add a test that a fresh hint is chosen over a due entry.

---

## T31F — Portfolio-risk follow-ups (from the T31 review)
STATUS: OPEN
DEPENDS: branch `cloud/T23F` has a `DONE T23F:` commit
BASE: origin/integration/r1 (by then it contains T23F; then merge origin/cloud/T31. Resolve the two audit-test add/add conflicts by taking the version with `expectedFailure` removed for tests that pass)
OWNS: desk/ledger.py (checkpoint validator only), desk/engine.py (T31 gate reject records only), tests/test_portfolio_risk.py, docs/PORTFOLIO_RISK.md
AVOID: everything else

1. **Checkpoint validation.** The validator (`desk/ledger.py` ~:49-110) must validate `portfolio_entries` and `portfolio_cooldown_until` when the flag is on. A missing or malformed key gives the standard recovery-required error, never a silent reset or a bare TypeError.
2. **Reject records.** `PORTFOLIO_RISK_CAP` (and `BELOW_MINIMUM`) reject records carry `reasons`, scores and policy fields, as the docs claim. Otherwise fix the docs.
3. **Liquidation and the loss streak.** Decide explicitly whether liquidation closes count toward the loss streak, document it, and test it.
4. **Config validation.** Validate `max_entries_per_10m` ≤ 10 (ENTRY_THROTTLE already allows only one entry per minute).

Leave the pre-I/O `portfolio_blockers` call to T16F.

---

## T27G — Shadow strategies: last fixes (from the T27F review)
STATUS: OPEN
DEPENDS: none
BASE: origin/integration/r1 (T27F is merged)
OWNS: tools/research/shadow_strategies.py, tests/test_shadow_strategies.py
AVOID: desk/**, tools/research/counterfactual.py

1. **Keep receipt classes separate.** `shadow_strategies.py` ~:172 drops `detail`. When the detail is `TERMINAL_RECEIPT_UNVERIFIED`, group the result as `REJECTED:...:UNVERIFIED`, and keep UNRESOLVED as its own group. Test both classes.
2. **Non-circular parity.** Rebuild a candidate from RECORDED reserves and timestamps (a fixture ledger with real held/entry events). Run the shadow's own `simulate` with `max_intra_marks=0` and `build_config(saved_cfg, {})`. Compare its entry and exit decisions with the recorded engine decisions, and report the expected gap from neutral features explicitly. The test `test_a_different_config_changes_the_replayed_decisions` must exercise a real difference.
3. **Provisional results.** Flag a provisional NOT_DISPATCHED (dispatch window still open) separately, or exclude it until it is final.

---

## T16G — Concurrency: bounded held latency, rollover, wiring (from the T16F review)
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T16F, then merge origin/integration/r1. Resolve `deploy/fresh/desk-paper-held-cycle.service` to `<POOL_FEE_BPS>` plus `--wall-seconds 64`.
OWNS: the T16/T16F files, plus tools/ops/fresh_start.py (held argv only)
AVOID: T22/T22F files, T23G files

**Coordinator decision (2026-10-11): REJECTED** — do not relax the 10s held-mark freshness. T35 will make all held marks fresh with one batched on-chain read. Leave T16F's `paper_portfolio_mark_ttl_seconds` code behind its flag, but never set it in any config or doc recommendation. The original note was about T16F's `paper_portfolio_mark_ttl_seconds` (a relaxation of held-mark freshness for entry decisions). Keep it behind its flag, and default it to ABSENT in example configs until the coordinator decides. Do not change its semantics.

1. **Wiring.** Add `--wall-seconds 64` to the held manifest argv (`fresh_start.py` ~:215) so `test_ops_fresh_start_wiring` passes after the merge. Fix the stale "held 10 s pass" comment (~:86).
2. **Bounded held latency during an entry.** The worst case today is ~5 min between monitoring passes, against a 120s cadence. Bound it:
   - cap each entry phase so it releases the lease between phases, or
   - run held legs inside the entry at a bounded interval (≤ cadence).

   Prove it with a test that uses REALISTIC phase durations (simulated clock advancing by the code's own budgets: preparation 18s, cycle 12s, acquisition and intake as measured), not a 1s entry.
3. **Rollover.** Guarantee a day rollover at least once per rollover window even when the book is full, no candidate exists, or the mode is EXIT_ONLY. For example, let the held pass's final leg roll over using fresh marks for all positions, under a versioned rule. Test all three cases.
4. **Entry estimate.** `ENTRY_SECONDS` must include acquisition and intake time (dispatcher ~:646-667). The pipeline test must not freeze `time.time` to 0 for those phases.
5. **Restart test.** Use 3 positions, a crash mid-leg (a reservation without an outcome) and an interrupted-entry restart. Prove there are no duplicate charges or fills.

---

## T35 — Batched on-chain portfolio marks (keep the 10s freshness rule with N positions)
STATUS: OPEN
DEPENDS: branch `cloud/T16G` has a `DONE T16G:` commit
BASE: origin/cloud/T16G (merge origin/integration/r1 first)
OWNS: a new desk/portfolio_marks.py, the minimal hooks in tools/paper_entry_dispatcher.py and desk/paper_monitor_service.py, desk/engine.py (only how a versioned reserve-implied mark is accepted for PORTFOLIO VALUATION), tests/test_portfolio_marks*.py, docs/MULTI_POSITION.md
AVOID: exit logic (exits keep using executable Jupiter sell quotes), T22/T23 files

**DECISION (CK, 2026-10-11).** Do not relax the held-mark TTL. Instead, refresh ALL open positions' marks with ONE batched `getMultipleAccounts` over their PumpSwap pool vaults right before an entry decision, and at the start of every held pass.
- **Versioned flag:** `paper_portfolio_mark_source_version: 1`. When absent, behaviour stays identical.
- **The mark:** a reserve-implied price from same-slot base/quote vault balances. Reuse and verify the math from T26G (`tools/research/counterfactual.py`) and T28F (`tools/ops/held_watcher.py`), and move the shared pricing into `desk/portfolio_marks.py` with tests: orientation, decimals, PumpSwap creator fee and Token-2022 transfer fee where known, virtual reserves.
- **Evidence:** the bytes of the accountInfo response (slot, data) are retained like other evidence. The request is charged to the monitoring allowance (one request for N positions). A failure is charged and leaves marks stale, and stale marks give a normal STALE_PORTFOLIO reject, never a latch.
- **Use:** the engine uses these marks ONLY for portfolio valuation, exposure and the daily equity checks. Stop, trailing and take-profit decisions and every SELL fill still require the executable Jupiter quote path, as today.
- **Tests:**
  - 4 open positions, with all marks within 10s using one request;
  - a cross-slot vault pair rejected;
  - flag-absent byte identity;
  - a failure leading to a normal stale reject;
  - realistic pipeline timing, with no frozen clock for real phases.

---

## T36 — Pacing upgrade for the paid Helius/Jupiter Developer plans (Kraken untouched)
STATUS: OPEN
DEPENDS: none
BASE: origin/integration/r1 (it contains T23G's pacing changes and T14G's research priority)
OWNS: desk/provider_pacing.py (the policy upgrade path only), a new config/provider-pacing-policy.json (reviewed policy), tools/ops/pacing_policy.py (plan/apply CLI, dry-run default), tests/test_pacing_policy_upgrade.py, docs/ops/RUNBOOK.md (one new step; coordinate with T32G if it is still open)
AVOID: the Kraken lane: its row, its migration receipt and `config/kraken-pacing-migration.json` must stay byte-identical

**DECISION (CK, 2026-10-11).** CK has paid **Helius Developer** (documented RPC limit 50 req/s, DAS/Enhanced 10 req/s, `sendTransaction` 5/s) and **Jupiter Developer** (documented 10 req/s over a 60s sliding window, shared across Swap/Price/Token; a firewall 429 is possible below the quota) plans. The production shared pacing DB has `policy` rows `('helius',2,2.0,30.0)`, `('jupiter',2,2.0,30.0)`, `('kraken',2,2.0,30.0)`, so Helius and Jupiter run at 0.5 req/s and waste most of the paid capacity.
- **New targets** (with headroom; other consumers such as the held watcher, the counterfactual sampler and the fill-realism worker share the same keys):
  - Helius cadence **0.1s** (10 req/s, 20% of 50);
  - Jupiter cadence **0.25s** (4 req/s, 40% of 10).

  Keep the backoff at 30s, and honour Retry-After as today. Kraken stays at 2.0s, exactly.
- **Explicit, reviewed upgrade.** The upgrade must be explicit and reviewed: an append-only `pacing_policy_changes` row recording the old and new values, the reason, and the config sha256. Never an in-place silent edit. Validation (`_validate`) must accept the reviewed policy for helius/jupiter and still pin Kraken exactly. Mixed-version note: every process using the shared DB must run code that accepts the new policy before `apply` (document this in the RUNBOOK).
- **Dry-run first.** `python -m tools.ops.pacing_policy plan|apply --db <shared pacing db> --policy config/provider-pacing-policy.json` defaults to dry-run. `apply` requires all desk writers stopped (same quiesce check as backup), and is idempotent.
- **Tests:**
  - an upgrade on a copy of a production-shaped DB (including a Kraken migration receipt fixture) leaves the Kraken row and receipt byte-identical;
  - new cadences take effect;
  - an old-code reader's behaviour on the upgraded DB is documented (fails closed);
  - re-apply is a no-op;
  - a forged change row is rejected;
  - a 429 still triggers the 30s backoff.

Add the separate per-provider budget note to docs: Helius `getMultipleAccounts` counts as one RPC call; DAS calls have their own 10/s cap.

---

## T37 — SOL/USD: Jupiter PriceV3 primary, Kraken fallback, divergence guard
STATUS: OPEN
DEPENDS: none
BASE: origin/integration/r1
OWNS: desk/sol_usd_observation.py, desk/kraken_usd_observation.py (only to expose a shared interface), a new desk/usd_valuation.py (source selection and cross-check), the minimal hook where the entry/held pipeline obtains SOL/USD, config/experiments/paper-kraken-fresh.example.json (or a new example file), tests/test_usd_valuation*.py
AVOID: desk/provider_pacing.py (use the existing jupiter and kraken lanes as they are), T22F/T32G files

**DECISION (CK, 2026-10-11).** Jupiter PriceV3 (`https://api.jup.ag/price/v3?ids=<SOL mint>`, header `x-api-key`) was verified working from the VPS on the paid Developer plan (HTTP 200, `usdPrice` field present, rate-limit headers present). Make Jupiter the PRIMARY SOL/USD source and Kraken (public Trades SOLUSD, shared 2s pacing, unchanged) the FALLBACK and cross-check.

Behind a new versioned flag, `paper_usd_valuation_version: 2` (absent or 1 means today's Kraken-only behaviour, byte-identical):
1. **Primary.** Fetch the Jupiter PriceV3 observation through the existing jupiter pacing lane. Retain the original bytes as evidence like the other sources. Validate the shape strictly: `usdPrice` is a finite positive number, `blockId` is present and the `decimals` are sane. Fail closed on any malformed field. Every failure is charged.
2. **Fallback.** If Jupiter fails, is stale (beyond the price TTL), or its shape is invalid, use the Kraken observation when it is fresh. Record which source was used in the event evidence.
3. **Divergence guard.** When both are fresh and differ by more than `usd_divergence_max_fraction` (default 0.01), there are no new ENTRIES: give a normal terminal no-entry (`USD_SOURCE_DIVERGENCE`), never a latch. Exits are never blocked by divergence; they use the primary, or the fallback if the primary is unavailable.
4. **Cost.** Fetch Kraken at most once per cycle, only when needed (fallback) or on a cross-check schedule (default every 5 min). This keeps Kraken traffic low.
5. **Tests:**
   - a fixture Jupiter body (real shape, as observed) parses;
   - each malformed shape is refused;
   - fallback selection;
   - the divergence no-entry, with exits unaffected;
   - flag-absent byte identity;
   - replay determinism;
   - a failure becomes a normal terminal outcome, with no NULL pass.

Add an example config with the flag on, validated by the real loader. The coordinator will decide whether the fresh experiment ships with it.

---

## T16H — Concurrency: post-intent checkpoints must not orphan the intent; TTL-independent rollover (from the T16G review)
STATUS: OPEN
DEPENDS: branch `cloud/T35` has a `DONE T35:` commit (T35's batched marks make all held marks fresh within 10s, which this task relies on)
BASE: origin/cloud/T35 (it contains T16G)
OWNS: the T16/T16F/T16G files, docs/MULTI_POSITION.md
AVOID: T22G files (desk/paper_cycle.py closure logic, desk/paper_pass_closure.py)

1. **(HIGH) Orphaned intents.** T16G's held checkpoints run AFTER the entry intent is written. A checkpoint leg that comes back BLOCKED, or that moves the mode to EXIT_ONLY/LIQUIDATING or sets `exit_blocked`, makes the next `_preflight` raise, leaving an intent without a result ("Unresolved dispatch; no retry"). That is a new dispatcher latch.
   - Fix: when a checkpoint changes the held state or gate after the intent, publish a typed terminal NO_ENTRY result for that intent (charges retained), and let held monitoring proceed normally.
   - Add a test that runs the REAL `_held_pass` (`paper_monitor_service.main` in-process, not patched out) with a BLOCKED leg. Surface the held pass's failure code in the entry result instead of swallowing it.
2. **Rollover.** The rollover rule must not depend on the rejected `paper_portfolio_mark_ttl_seconds`. With T35's fresh batched marks, roll over when every position has a fresh mark (≤10s), on a held pass or a checkpoint. Test the book-full, no-candidate and EXIT_ONLY cases with 3 positions and the TTL at the price TTL (10s).
3. **Flag dependency.** The concurrent flag must NOT require the TTL key. Remove that dependency.
4. **Docs and reports.** Remove every recommendation of `paper_portfolio_mark_ttl_seconds` (docs/MULTI_POSITION.md ~:177; correct reports/T16F.md's suggestion with a note). Fix the stale `ENTRY_SECONDS = 30` line (it is 52).
5. **Phase caps.** Cap each entry phase so the held-latency bound is enforced, not just estimated. A phase that overruns is cut, with a terminal no-entry result, never an orphaned intent.

---

## T39 — Resource isolation for research/optional units
STATUS: OPEN
DEPENDS: none
BASE: origin/integration/r1
OWNS: deploy/fresh/desk-counterfactual.*, deploy/fresh/desk-held-watcher.service, the fill-realism worker unit template if present, tests/test_ops_healthcheck.py (unit invariants only)
AVOID: desk/**, the trading unit templates (entry/held/monitor/decisions/dashboard/discovery)

Research and optional services must never degrade trading:
- Add `CPUQuota=100%` (one core), `Nice=10`, `IOSchedulingClass=idle` (or best-effort with priority 7), `CPUWeight=20`, `IOWeight=20`, and a sane `MemoryMax`/`TasksMax` to `desk-counterfactual.service`, the fill-realism worker, and any other research unit. The held watcher is trading-relevant (it triggers exits fast), so give it NORMAL priority but a `MemoryMax`/`TasksMax`.
- Extend the unit-invariant test so every research unit has these limits and no trading unit gets throttled.
- Document in docs/ops/OPERATIONS_24x7.md how to read per-unit CPU and memory (`systemd-cgtop`, `systemctl show -p CPUUsageNSec,MemoryCurrent`), and add a healthcheck WARN when a research unit's CPU over the last interval exceeds its quota for 3 consecutive checks.

---

## T37F — USD valuation v2 follow-ups (from the T37 review)
STATUS: OPEN
DEPENDS: branch `cloud/T22G` has a `DONE T22G:` commit (its FAILED_CHARGED closure is part of the fix)
BASE: origin/cloud/T37, then merge origin/cloud/T22G
OWNS: desk/usd_valuation.py, desk/sol_usd_observation.py, desk/regime_producer.py (source selection only), desk/model.py (valuation rebuild only), tests/test_usd_valuation*.py
AVOID: desk/paper_pass_closure.py (T22G owns it; only consume it)

1. **(HIGH) A provider failure must not latch.** Today a failed Jupiter or Kraken USD attempt inside an otherwise-normal no-entry pass leaves a NULL pass, because T01's proof refuses failed charged attempts (`paper_cycle_no_entry.py` ~:154-157). The pass must end terminal instead: either the T22G FAILED_CHARGED closure accepts it (an allow-listed transport/429/5xx cause), or the no-entry proof accepts failed USD attempts that the saved v2 valuation cites. Flip the two committed tests that assert the latch (`tests/test_usd_valuation_cycle.py` ~:163, ~:240) to assert a terminal, non-latching outcome, with charges retained.
2. **(MED) Regime producer source.** The regime producer reads only Kraken records (`regime_producer.py` ~:68-80). Make it read the SOL/USD observation the v2 valuation actually used (Jupiter or Kraken, recorded in the evidence). Then implement the spec's Kraken schedule (fallback, or a 5-min cross-check) without starving regime evidence. If a cross-check fails, regime uses the primary.
3. **(MED) Byte identity.** Add a committed flag-absent byte-identity test (ledger, events and outcomes vs a v1 run).
4. **(LOW) Divergence limit.** `model.py` ~:220-224 must rebuild using the CONFIGURED `divergence_max_fraction` and refuse an event whose stored value differs.
5. **(LOW) Persistent auth failures.** A persistent Jupiter 401/403 under v2 must raise a health WARNING (it must not stay silent while falling back). The parser refuses responses with extra mints. Move `ExampleConfigTests` above the `unittest.main()` guard.

---

## T24R — Rebuild T24 on top of T22G with durable, verifiable incremental state
STATUS: OPEN
DEPENDS: branch `cloud/T22G` has a `DONE T22G:` commit
BASE: origin/cloud/T22G (merge origin/integration/r1 first). Build FRESH; do not rebase or cherry-pick the T24 commits (`origin/cloud/T24`, review: FIX). Reuse ideas and tests only.
OWNS: desk/history_preparation_rejection.py, desk/paper_cycle_no_entry.py (index only), desk/monitoring_budget.py (accounting only), desk/paper_cycle.py (deadline config only), new tests and a committed benchmark harness under tools/research/bench_*.py
AVOID: desk/paper_pass_closure.py, dispatcher closure logic (T22G)

The T24 review found that every unit is `Type=oneshot`, so an in-process cache gives cold cost on every pass.
1. **F7 durable index.** Use an append-only, trigger-guarded verified-digest table in the store, written in the same transaction as each retained item, so each gate call verifies only NEW items plus a bounded deterministic sample. Remove the 100k-page/256MiB hard latch: rotation warnings at 80%, visible to the healthcheck (a queryable row or state file, not only the log). The F7 audit test must measure COLD cost, constant within generous bounds across 1×/10×/30× history.
2. **F14 incremental accounting.** Keep running totals per window in an append-only, hash-chained table that is updated in the same transaction as each reservation/outcome. Verification covers new rows plus a chained digest; no unauthenticated sidecar file. Add a property test that the incremental result equals a full recompute after random sequences, including forced corruption of an already-verified row being detected by the chain. No flaky tests: everything deterministic.
3. **F9 deadline.** The default stays 10s. With T36's paced cadences (helius 0.1s, jupiter 0.25s), compute the real worst case per pass type from the code and choose a range accordingly. Validate at config load. Keep it consistent with the unit `TimeoutStartSec` values (held 120, entry 600, decisions as rendered); add a test that checks the rendered units. An overrun yields T22G's FAILED_CHARGED (allow-listed), never a latch.
4. **Benchmarks.** Commit the 7-day fixture generator and benchmark harness, with before/after numbers in the report, reproducible by `python -m tools.research.bench_...`.

---

## T40 — Candidate feature store (as-of features for selection/entry research)
STATUS: OPEN
DEPENDS: none
BASE: origin/integration/r1
OWNS: tools/research/features.py, tests/test_features.py, docs/research/FEATURES.md, deploy/fresh/desk-features.service + .timer (research unit with T39-style limits)
AVOID: desk/** (import only)

GOAL (CK): collect as much paper data as possible to build selection, entry and exit strategies fast. The shadow strategies (T27) currently use neutral features. Build an append-only `features.sqlite` (its own store) that records, for EVERY candidate, the features as they were KNOWN AT EACH TIME (`as_of`), derived ONLY from evidence the desk already retains, so it costs no extra provider requests:
- **Sources (read-only):** discovery hints, investigation/history pages, observation evidence, decision journal, dispatcher results, ledger, counterfactual samples.
- **Features:**
  - age since migration;
  - market cap and liquidity at hint/decision time;
  - holder count and top-N concentration if measured;
  - net buy ratio, flow and momentum windows (the ones the engine computes);
  - Token-2022 profile flags;
  - bundle/sniper flags if present;
  - dev/creator facts if present;
  - the rejection reasons;
  - regime metrics (if T33 evidence exists).
- **Layout:** one row per (mint, as_of, feature_set_version), with provenance (the source row ids or hashes) per feature. A strict no-look-ahead guarantee: a feature's as_of is never later than the source evidence's time.
- **Interfaces:**
  - an export to CSV/Parquet-free JSONL for offline analysis (stdlib only);
  - the T27 `--features` input format, so shadow strategies use REAL features.
- **Tests:** known-answer extraction from fixture stores built with the real writers; no look-ahead (the as_of property); idempotent re-runs; bounded reads (`mode=ro`; immutable only for a quiet WAL).

---

## T41 — Daily strategy research report (one HTML for CK each morning)
STATUS: OPEN
DEPENDS: none
BASE: origin/integration/r1
OWNS: tools/research/daily_report.py, tests/test_daily_report.py, deploy/fresh/desk-daily-report.service + .timer (research limits)
AVOID: desk/**, the other research tools (import only)

One read-only command builds a single self-contained HTML file (no external assets, everything escaped) and a JSON summary, combining:
- the T29 funnel (where candidates die, including the UNRESOLVED latch banner);
- the T26 counterfactual (does each filter reject winners);
- the T27 shadow leaderboard (holdout, CI, ambiguous share), using T40 features when present;
- T10 forward eval of real paper trades;
- T14 fill realism, if present;
- health and budget usage.

It starts with a 10-line "what changed since yesterday / what to try next" section computed from the data (no LLM). Label everything paper-only and EXECUTION_UNVERIFIED, and show sample sizes, with explicit "insufficient sample" flags.

The timer runs daily at 23:30 UTC (08:30 KST), writing to `<STATE_DIR>/reports/YYYY-MM-DD.html`. Tests use fixture stores with known numbers, cover HTML escaping, and cover empty/young data.

---

## T35F — Portfolio-marks fixes (from the T35 review); fold together with T16H's output
STATUS: OPEN
DEPENDS: branch `cloud/T16H` has a `DONE T16H:` commit
BASE: origin/cloud/T16H (it is built on T35)
OWNS: desk/portfolio_marks.py, desk/engine.py (valuation and mark precedence only), desk/monitoring_budget.py (classification of the valuation-only read only), desk/paper_cycle.py (marks deadline only), desk/paper_concurrency.py (estimates only), docs/MULTI_POSITION.md, tests/test_portfolio_marks*.py
AVOID: desk/paper_pass_closure.py (T22G)

1. **(HIGH) Real timing in tests.** `tests/test_portfolio_marks_pipeline.py` (~:100, ~:186) freezes monotonic and wall time, so "≤10s" is only true at zero elapsed time, and ~:258 compares the marks event with itself. Use a simulated clock that advances by realistic per-request durations (pacing per T36: helius 0.1s; the quote and sleep budgets), and assert freshness at the ENTRY DECISION time for 4 positions.
2. **(HIGH) No latch from a valuation-only read.** A non-transient marks-read failure (RESPONSE_INVALID, HTTP 4xx) sets monitoring `SOURCE_FAILURE` (`monitoring_budget.py` ~:634) and blocks held exit quotes. That is a latch. The valuation-only read must NEVER latch monitoring: record a typed failure (charged), leave the marks stale, and let a normal STALE_PORTFOLIO terminal reject follow. Test every failure class.
3. **(MED) Deadline accounting.**
   - Do not extend the 10s cycle deadline by the marks-read time (`paper_cycle.py` ~:353 `budget.start +=`). Do the read inside the budget, or account for it explicitly.
   - Update `ENTRY_SECONDS`/`CYCLE_SECONDS` in `paper_concurrency.py`, and count the held pass's extra refresh in `plan_legs` against `--wall-seconds`.
4. **(MED) Reserve.** Retune `RESERVE_PASSES` (entries now cost +2 requests, held passes +1). Show the math.
5. **(MED) Model marks vs liquidation.** Model (reserve-implied) marks may feed exposure and position limits, but `peak_equity`, `day_start_equity` and DAILY_LIQUIDATE must use executable marks, or a model mark only when it is within `portfolio_mark_max_divergence` (default 3%) of the latest executable mark. Make this a versioned rule and test it.
6. **(MED) TTL doc.** Remove the TTL-120 recommendation (`docs/MULTI_POSITION.md` ~:205).
7. **(LOW) One rollover rule.** T35 added a rollover inside the marks event (`engine.py` ~:469) without a flag. Unify it with T16H's rollover rule under one versioned flag; there must be exactly one rollover rule.
8. **(LOW) Closed vault.** A closed vault sets the mark to zero, with an explicit reason, rather than leaving it stale forever. Add a golden byte-identity test with the flag absent.

---

## T42 — Research-tool follow-ups (T40 caps/checkpoint, T41 fixes, read-only open rule)
STATUS: OPEN
DEPENDS: none
BASE: origin/integration/r1, then merge origin/cloud/T40 and origin/cloud/T41. Resolve the trivial conflict in tests/test_ops_healthcheck.py by keeping both units.
OWNS: tools/research/features.py, tools/research/daily_report.py, tools/research/forward_eval.py (open rule only), tools/research/fill_realism_report.py (open rule only), tools/ops/healthcheck.py (open rule only), deploy/fresh/desk-features.*, deploy/fresh/desk-daily-report.*, their tests
AVOID: desk/**

1. **(MED) T40 incremental ingestion.** Replace the capped full rescans (`features.py` ~:289-293, ~:423-425) with incremental ingestion from a persisted per-source cursor (seq/rowid). Process in bounded pages, iterating rows rather than calling `fetchall`, within `MemoryMax`. Record a counter when a page bound is hit, and NEVER store an outcome without its `reject_reasons` because of a cap. Test 2M fixture events with bounded memory.
2. **(LOW) T40 optional stores.** Only the ledger is required. A missing decisions, research, journal or discovery store skips those features with a recorded reason, not exit 2.
3. **(MED) T41 sample sizes.** "Filters look non-harmful" must never be printed when every group has n<30. Print INSUFFICIENT_SAMPLE instead, and test it.
4. **(MED) T41 uses T40 features.** Run `features shadow-features` when the store exists and pass `--features` to the shadow section, labelling which features were real vs neutral.
5. **(LOW) Unit hardening.**
   - The daily-report unit gets `ReadWritePaths=<STATE_DIR>/reports`, `PrivateNetwork=true`, `CPUWeight`/`IOWeight`, the same as T39/T40.
   - The `daily_report.py` error text drops `str(error)`; report the class name only.
6. **(LOW) Read-only open rule.** `healthcheck.py` ~:71, `forward_eval.py` ~:61 and `fill_realism_report.py` ~:118 use the shared read-only open rule (`mode=ro`, falling back to `immutable` only for a quiet WAL), so sections don't become ERROR under `ReadOnlyPaths`.

---

## T32H — Finish T32G (stalled worker; continue its branch) — CRITICAL PATH
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T32G (its last commit: "report wording (limitations)"). Continue from there, then merge origin/integration/r1.
OWNS: the T32G files
AVOID: desk/**

The worker on T32G went silent for more than 2 hours just before finishing. Read `reports/T32G.md` on `origin/cloud/T32G`, verify each T32G item against the spec in this file (items 1-8), finish anything missing, and make the RUNBOOK commands match the merged tools. T34 added `--pacing-db`/`--discovery-db`/`--ledger` to status, preflight and verify_cycle: steps 6 and 8 and the verify_cycle snapshots must pass them. Also add the T36 pacing-policy step with the correct ordering: apply ONLY after the new release is on every unit, run as solana-desk, and treat the policy JSON as append-only.

Run every `tests/test_ops_*.py` module plus the e2e test with the real exit code, then commit `DONE T32H:`. If `cloud/T32G` gets a DONE commit meanwhile, stop and say so in your report.

---

## T43 — Features: discovery cursor + small fixes (from the T42 review)
STATUS: OPEN
DEPENDS: none
BASE: origin/cloud/T42
OWNS: tools/research/features.py, tests/test_features.py
AVOID: desk/**, tools/research/funnel_report.py (import only)

1. **(MED) Discovery.** Discovery is still a capped full rescan from `since=0` (oldest 50k frames first; `features.py` ~:553-556, ~:707), and `stats['truncated']` is ignored, so once there are more than 50k frames, newer hints are silently never ingested. Use a persisted discovery cursor (seq/received_at), the same as the other sources, with an anchor check. If any bound is hit, record `DISCOVERY_TRUNCATED`. Test with more than 60k frames, where the newest hints must be ingested.
2. **(LOW) False page bound.** `exhausted()` must not count `PAGE_BOUND_HIT` when the last full page drained the source exactly.
3. **(LOW) Dispatch journal.** Read the dispatch journal incrementally with a cursor, not whole on every run.

---

## NOTE (coordinator, 2026-10-11): T24R and T37F were claimed on top of T22G before its review found H1/H2. When they finish, they must merge `origin/cloud/T22H` (once it is DONE) before the coordinator integrates them. Workers on T24R/T37F: if T22H is DONE before you finish, merge it now.

---

## T16I — Concurrency leftovers (from the T16H review) + merge with the T22 line
STATUS: OPEN
DEPENDS: branch `cloud/T35F` has a `DONE T35F:` commit AND branch `cloud/T22H` has a `DONE T22H:` commit
BASE: origin/cloud/T35F, then merge origin/cloud/T22H (expect conflicts in desk/paper_cycle.py ~150 lines and tools/paper_entry_dispatcher.py `_dispatch` ~250 lines)
OWNS: the T16/T35 files, tools/paper_entry_dispatcher.py, desk/watchlist.py (classifier only), tools/research/counterfactual.py and tools/research/funnel_report.py (classifier only), docs/MULTI_POSITION.md
AVOID: desk/provider_pacing.py

1. **Load-bearing merge resolution.** Keep T16H's `phase.finish()` check AHEAD of T22G's post-acquire raise, and keep `phase.check()` OUTSIDE T22G/T22H's `rpc_state` capture. Otherwise a `_PhaseCut` is classified as non-transient and becomes an unresolved intent or an INTEGRITY_HOLD. Add a test that proves a phase cut stays a terminal no-entry after the merge.
2. **Remaining post-intent raises.** Each of these must end with a typed terminal no-entry for the intent (charges kept), never an orphaned intent:
   - `_HeldCadence.gate` (`monitor._context` non-blocking locks: "Research worker busy", "Evidence invocation busy", "Cycle ledger busy");
   - `_preflight(expected, scan)` after a mode change made by another process (~:936, ~:962);
   - `overrun()` (~:911);
   - the no_entry size bound.

   Add a test for each.
3. **Classifiers.** Teach the three classifiers the new kind `dispatcher_checkpoint_no_entry_v1`:
   - watchlist → NOT_YET (a held-side cut, so retry is fine);
   - counterfactual → REJECTED:CHECKPOINT;
   - funnel → its own stage (not UNRESOLVED).

   Test each.
4. **Phase caps.** Enforce the caps, not just detect them: bound each in-flight request's timeout by the remaining phase budget, and treat a pacing backoff longer than the remaining budget as a cut before waiting.
6. **T35F review fold-ins.**
   - (a) The `RESERVE_PASSES` math (`paper_concurrency.py` ~:22-26) must count held passes started by the held-watcher `.path` trigger and by dispatcher checkpoints, not only the held timer. Bound the trigger rate (it is shared with T28F's per-reason budgets), and derive the reserve from that bound. A store on the legacy 60/hour allowance must give a clear refusal reason; do not hide it by patching `RESERVE_PASSES` in tests.
   - (b) Either wire `entry_seconds(cfg)` (~:114) into the production estimate or remove it.
   - (c) Remove the stale TTL docstring (~:56).
   - (d) A transient null account (`portfolio_marks.py` ~:270) must not be treated as closed. Require N consecutive confirmations or a closed-account proof first.
   - (e) Example and docs recommend `paper_portfolio_mark_source_version: 2` only, never 1.
5. **Docs.** Remove the stale TTL and 30s statements from docs/MULTI_POSITION.md (~:146-148, :158, :170, :172, :237). The rollover rule is T35F's unified rule; document exactly one.

---

## T37G — USD failures: transient-only closure (from the T37F review)
STATUS: OPEN
DEPENDS: branch `cloud/T22H` has a `DONE T22H:` commit
BASE: origin/cloud/T37F, then merge origin/cloud/T22H
OWNS: desk/paper_cycle_no_entry.py (`_cited_usd_failure`, `producer_blocker_is_normal` USD handling only), desk/regime_producer.py, tests/test_usd_valuation*.py
AVOID: desk/paper_pass_closure.py (consume its allow-list; don't change it)

T37F made every failed SOL/USD attempt end terminal, including integrity-class causes. That is a new laundering path, the same class as T22H H1. Coordinator probes through `run_once` showed these causes ending NO_ENTRY or FAILED_CHARGED when they should be INTEGRITY_HOLD:
- Jupiter TLS_ERROR, UNCLASSIFIED_ERROR, 401, 403, malformed responses and extra mints;
- Kraken TLS and 403;
- in held passes, Jupiter TLS/401 and Kraken TLS/UNCLASSIFIED.

1. **Transient only.** Accept a failed USD attempt ONLY when its cause is transient (timeout/reset/408/429/5xx/pacing contention, reusing T22G/T22H's transient vocabulary, e.g. `failed_reads_transient`/`_latching_failure`). Everything else HOLDs.
   - Exception: a Jupiter 401/403 may end terminal ONLY IF the Kraken fallback was MEASURED (fresh and valid) in the same pass, since the T37F health WARNING covers it.
   - In held passes, `SOL_USD_UNAVAILABLE` must go through the same evidence check. Do not mark it "normal" without checking `failed_reads_transient`.
   - Commit the coordinator probe table as tests: each cause × {entry, held} × {Kraken up, Kraken down}.
2. **(LOW) Regime source.** When the valuation used the Kraken fallback, the regime must use THAT observation, not a Jupiter price up to 900s old (`regime_producer.py` ~:94-101).
3. **Out of OWNS.** T37F edited paper_cycle.py, engine.py and tools/ops/healthcheck.py. List those edits in the report and keep them minimal.

---

## T16J — Concurrency final fixes (from the T16I review)
STATUS: OPEN
DEPENDS: branch `cloud/T22I` has a `DONE T22I:` commit
BASE: origin/cloud/T16I, then merge origin/integration/r1 and origin/cloud/T22I. Resolve the T22H-origin conflicts in desk/monitoring_budget.py and three tests by keeping integration's T38/T25F versions plus T22H/T22I's additions.
OWNS: the T16/T35 files, tools/paper_entry_dispatcher.py, desk/watchlist.py (docs table only), docs/MULTI_POSITION.md, docs/WATCHLIST.md, tests
AVOID: desk/provider_pacing.py

1. **Crash-restart test.** `tests/test_paper_concurrency_held_latency.py` ~:338 fails on macOS: it uses the wrong key (`monitoring_attempted_requests` instead of `attempted_requests`), and the orphan is never abandoned without an injected lock table. Fix the key and inject the lock table (T25F style), so the test passes on macOS AND Linux.
2. **Watchlist docs.** Add the 11 new NOT_YET codes (`desk/watchlist.py` ~:60-72) to docs/WATCHLIST.md, so `test_watchlist.DocumentationTests` passes. Scope those codes to the checkpoint result kind only, not the global NOT_YET set.
3. **(MED) Post-intent typed terminals.**
   - `Provider pacing pending` (`state_gate` → `_preflight` ~:386) is pacing contention (transient) and must end as a typed terminal no-entry, not EXCEPTION_VALUEERROR → hold.
   - The same applies to the `_held_guard` refusals mid-phase ("Held-position priority before I/O", "Held cycle or ledger busy").

   Add a test for each.
4. **Alarm during intake.** Add a test for the SIGALRM `_Deadline` firing after a charged `HistoryProgress.advance` reservation inside `migration.intake`. The next preflight/dispatch must be clean, with no ambiguous-reservation latch. Fix the code if it is not.
5. **One rollover rule in the docs.** Reconcile docs/MULTI_POSITION.md (~:150 vs ~:235-236) to describe the single flag-gated rule the code has.
6. **(LOW) Closed-vault confirmations.** The N=2 "closed" confirmation needs a minimum spacing (e.g. ≥5s between observations), so back-to-back refreshes cannot confirm a transient null.

Run every listed module plus test_watchlist in ONE process on macOS, with the real exit code.
