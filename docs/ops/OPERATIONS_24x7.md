# 24/7 unattended operation (fresh-start deployment, paper only)

Nothing here trades, signs or spends. Units live in `deploy/fresh/` as templates (placeholders in
`deploy/fresh/README.md`); the existing `deploy/*.service|*.timer` are not modified. `fresh_start render-units` fills the
placeholders from the applied manifest, and `tools.ops.cutover` pins `WorkingDirectory` per release, writes the store drop-ins and
enables the intended units at boot. There is exactly ONE install/start/enable sequence: `docs/ops/RUNBOOK.md` steps 5-9
(exercised end to end by `tests/test_ops_e2e_deploy.py`). Do not `systemctl enable --now` units by hand: cutover refuses units that
are already running, and starts, verifies and only then enables them.

## What starts at boot

| Unit | Boot | Why |
|---|---|---|
| `desk-continuous-discovery.service` | yes | listens for migrations; `Restart=always`, `RestartSec=10`, start limit 10 per 15 min |
| `desk-dashboard.service` | yes | loopback only (`127.0.0.1:8765`, hard-coded in `desk.dashboard.serve`) |
| `desk-paper-held-cycle.timer` | yes | every 2 min after the previous pass finished; exits positions |
| `desk-decisions.timer` | yes | 1 min; reject-only journal |
| `desk-backup.timer` | yes | every 6 h (`tools.ops.backup`, prunes snapshots older than 7 days only after a successful new one) |
| `desk-healthcheck.timer` | yes | every 5 min; alerts through `tools.ops.notify` |
| `desk-notify-daily.timer` | yes | 08:05 UTC summary |
| `desk-paper-entry-dispatcher.timer` | **NO, coordinator only** | the first live-data BUY is a deliberate step (RUNBOOK step 9) |
| `desk-paper-monitor.timer` | **NO, manual** | stale-mark watchdog; with a slow held cadence it flags every position stale and mode sticks at `EXIT_ONLY` until an operator `RESUME` (T09 F5). `cutover` refuses to start or enable it without `--allow-monitor`; do not pass that before T23 (EXIT_ONLY stickiness) lands |

The two manual timers still carry `[Install]` so they can be enabled with one command; nothing enables them.
`cutover --enable` covers only the units in the boot table marked yes, and never the entry dispatcher.

Discovery: `discovery.continuous listen` is capped at 86400 s by the code and exits 0 afterwards. `Restart=always` re-enters it
immediately; its hourly byte/record budgets live in the shared database and are not reset by a restart.

## Install order

Follow `docs/ops/RUNBOOK.md`; in short:

1. `tools.ops.fresh_start apply ... --enable-held --service-user solana-desk --chown solana-desk` creates `<FRESH_ROOT>`, the scheduler
   lock, the manifest (`scheduler_identity`, per-unit arguments and sections) and the one backup directory `<BACKUP_ROOT>`
   (`/var/backups/solana-desk/fresh-<version>`, the destination of the backup unit and the `--backup-root` of the healthcheck).
2. `tools.ops.fresh_start render-units --root <FRESH_ROOT> --out DIR --release-dir <RELEASE_DIR>` fills every placeholder (it refuses an
   unfilled one); install the result under `/etc/systemd/system/` and `daemon-reload`. Create `<STATE_DIR>` (0700, `solana-desk`).
3. `tools.ops.cutover cutover ... --store-env <FRESH_ROOT>/fresh-start-manifest.json --enable <boot units>` writes
   the drop-ins, starts the listed units with `systemctl start --no-block` (polled until `TimeoutStartUSec` + 60 s), verifies them and
   then runs `systemctl enable` on the boot units. The entry timer and the monitor timer stay off and disabled.
4. Run `tools.ops.healthcheck` once by hand (below). Right after cutover the only legitimate CRITICALs are `discovery_frames` (no frame
   yet) and `backup` (none yet); anything else is a stop. Then the entry timer is a separate, deliberate step (RUNBOOK step 9).

Optional Telegram: install `deploy/fresh/telegram.conf.example` as a drop-in on `desk-healthcheck.service` and
`desk-notify-daily.service` and create `/etc/solana-desk/telegram.json` (`{"token":..., "chat_id":...}`, root 0600). Without it,
alerts go to the journal and `<STATE_DIR>/notify-state.json` only.

```
python -m tools.ops.healthcheck --root <FRESH_ROOT> --discovery-db <DISCOVERY_DB> --pacing-db <PACING_DB> \
   --backup-root <BACKUP_ROOT> --config <CONFIG>            # one JSON line; exit 2 on CRITICAL
journalctl -u desk-healthcheck -n 20 --no-pager
```

## How alerting behaves

`desk-healthcheck.service` exits 2 on any CRITICAL (the unit shows as failed in `systemctl --failed`); `ExecStopPost` runs
`notify alert` for both outcomes. Notify keeps `<STATE_DIR>/notify-state.json` (0600): a finding alerts when new or escalated to
CRITICAL, repeats every 1 h (CRITICAL) / 6 h (WARN) while unchanged, and sends one `RESOLVED` when it clears. At most 12 messages per
hour; extra findings wait for the next run rather than being dropped. A failed delivery is not recorded as sent and is retried.
Messages are redacted (bot-token shape, `key=`/`token=`/`secret=` pairs, bearer values); the Telegram token only ever comes from
`$CREDENTIALS_DIRECTORY/telegram.json` and send failures log only the exception type.

The healthcheck opens every database `mode=ro`. SQLite may create `-wal`/`-shm` sidecars when it opens a WAL database that no
process currently holds open; no database content, mtime or the shared pacing/discovery stores are changed.

## Alerts and what to do

Never edit a store by hand, clear a pacing slot, reset a counter, or delete a NULL pass: charged requests stay charged and
original records stay original. When the ledger is flat the sanctioned recovery is a new store set (RUNBOOK "Rotate when flat").

| Check | Severity | Meaning | Response |
|---|---|---|---|
| `unit:<service>` | CRITICAL (WARN for decisions/monitor/backup oneshots) | long-running unit not active, or oneshot last run failed | `systemctl status`/`journalctl -u`; discovery/dashboard restart themselves, repeated failure hits the start limit |
| `restarts:<unit>` | WARN ≥3, CRITICAL ≥10 | `NRestarts` is high AND the last run did not exit cleanly (`Result`/`ExecMainStatus` non-zero, or `auto-restart`/`failed`). Discovery's daily clean exit-0 restart stays OK, however high `NRestarts` gets | read the journal for the crash reason; do not mask it by raising limits |
| `unit:*.timer` | CRITICAL | a required timer is not active (manual timers: only if enabled but inactive) | `systemctl enable --now` / inspect why it stopped |
| `entry_tick` / `held_tick` / `decisions_tick` | WARN/CRITICAL | no trigger for 5/15 min (entry), 10/30 min (held), 3/10 min (decisions) | timer stuck or unit running far past its cadence; check `systemctl list-timers` |
| `discovery_frames` | WARN 2 min, CRITICAL 5 min | listener has not completed a frame | check the discovery unit and provider reachability; the hourly budget may be exhausted (`python -m discovery.continuous status --db <DISCOVERY_DB>`) |
| `discovery_events` | WARN 30 min, CRITICAL 2 h | no migration event seen | usually market quiet or filter issue; confirm frames are fresh first |
| `exits` | CRITICAL when a position has `exit_blocked`; WARN for bare `EXIT_ONLY` | an exit cannot proceed, entries are frozen | the position cannot be sold until a fresh exit quote succeeds; check held-pass journal output. `RESUME` is an explicit operator control (`desk.paper_cycle_cli --control`), not part of this runbook |
| `mode` | WARN `ENTRY_PAUSED` | entries paused (for example by the first-BUY latch) | expected after the latch; otherwise investigate who paused it |
| `mark_age` | WARN 6 min, CRITICAL 15 min | an open position's last mark is old. Price TTL is 10 s, so marks are only fresh during a held pass; this alerts on *missed passes* | check `desk-paper-held-cycle` status and the pacing/monitoring checks below |
| `max_hold` | CRITICAL | position older than `max_hold_seconds` without exit | same as `exits`; time-stop should have fired |
| `ledger` | CRITICAL | checkpoint missing while events exist | stop writers; recovery needs the coordinator, original records are preserved |
| `observation_passes` | CRITICAL | a charged pass has a NULL outcome and the global terminal gate refuses all further work (T09 F1) | do not retry the candidate or hand-edit; if flat, rotate to a fresh store set; if a position is open, hand over to the coordinator (successor machinery) |
| `monitoring_budget` | WARN <20 %, CRITICAL <5 % headroom | requests in the last window vs the effective cap (`max(table cap, monitoring_cap 3600)`) | spending is far below the cap in normal operation (about 150/h); a jump means a loop, find the cause; never reset |
| `monitoring_latch` | CRITICAL | monitoring `blocked` is set (`SOURCE_FAILURE`, clock rollback) and held monitoring stops | coordinator action (reviewed handoff); do not clear by hand |
| `monitoring_pending` | WARN | reservations without an outcome (killed mid-read) | if it persists after a pass, escalate to the coordinator |
| `pacing:<provider>` | CRITICAL | provider blocked for N s, or a slot has been pending >5 min (orphaned by a kill, T09 F6) | blocked: wait it out, never reset the shared pacing store. Orphaned: coordinator only |
| `disk` | WARN <20 %, CRITICAL <10 % or <1 GiB | filesystem under `<FRESH_ROOT>` | free space outside the stores (old backups, logs); never delete store files |
| `backup` | WARN >30 h, CRITICAL >54 h or none | newest snapshot age | `journalctl -u desk-backup`; run `tools.ops.verify_backup` on the latest snapshot |
| `healthcheck` / `notify_input` | CRITICAL | the check itself could not run, its report was unreadable, a database probe raised `sqlite3.Error`, or `notify` found the report older than 2x the interval (`healthcheck not running`) | fix the path/permission named in the detail; if the report is stale, `systemctl status desk-healthcheck.timer`. `notify alert|daily --interval N` must match the timer period (default 300 s) |

Every probe failure (missing or symlinked database, bad schema) is reported as CRITICAL `probe failed`, never skipped.

## Timeouts and wall clocks (T09 F8/F11)

| Unit | TimeoutStartSec | Basis |
|---|---|---|
| entry dispatcher | 600 | code worst case about 282 s plus pacing waits; a kill after the dispatch intent is a permanent latch (F3) |
| held cycle | 120 | 10 s pass deadline plus export/accounting; killing mid-pass leaves a NULL pass |
| monitor / decisions | 60 | local work only |
| backup | 300 | |
| healthcheck | 120 | |

Every scheduler unit sets `DESK_PAPER_SCHEDULER_IDENTITY` and has `ConditionPathExists=` on `paper-scheduler.lock`
(created once by `fresh_start apply`; do not recreate it, the inode is pinned). Entry and held set `DESK_PROVIDER_PACING_DB` to the
shared pacing store. Entry runs `--execute --systemd-credentials`; held, entry and monitor use the same `<CONFIG>` and ledger; no
`--dependency-blocker`. `tests/test_ops_healthcheck.py::UnitTemplateTests` renders every template and checks these.

## Known limitations

- The templates were never loaded by a real systemd; the tests render and parse them. Run `systemd-analyze verify` on the VPS.
- `desk.backup` cannot back up the fresh set (it requires `launches.sqlite`, `raw.sqlite`, `active-paper.sqlite`), so
  `desk-backup.service` uses `tools.ops.backup` from T03. That tool must be present in the release. Shared pacing and discovery
  stores are not part of these snapshots.
- The manifest argv and the rendered templates now agree (entry carries `--execute --systemd-credentials`, `TimeoutStartSec=600/120`);
  `tests/test_ops_fresh_start_wiring.py::RenderUnitsTests` fails if they drift apart. The shared pacing database uses a rollback journal, so the
  entry and held units must be able to write its DIRECTORY (the archived root); a single-file `ReadWritePaths` would break pacing.
  The archived stores in that directory are protected by `chmod 0400` and by the cutover archived-store guard instead.
- A malformed `telegram.json` logs a WARNING (never its contents) and falls back to the journal notifier.
- Investigation daily/lifetime budget headroom is not probed (their tables are not a stable read-only interface); monitoring headroom is.
  The monitoring cap is a threshold (`monitoring_cap`, default 3600), because the stored `cap` column does not reflect the activated allowance.
- Healthcheck cannot tell a quiet market from a broken filter (`discovery_events`); it alerts on frame liveness first.
- Telegram delivery was only tested against a fake opener.
- Retention pruning removes snapshot directories older than 7 days (`find -mtime +7`) after each successful backup; it never touches stores.
