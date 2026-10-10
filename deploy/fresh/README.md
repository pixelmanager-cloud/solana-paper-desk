# Fresh-start unit templates (24/7)

Templates for the fresh-start deployment. They are NOT installed by anything in the repository and never
modify `deploy/*.service|*.timer`. Placeholders (substitute every one before install; the test renders them):

| Placeholder | Meaning |
|---|---|
| `<FRESH_ROOT>` | the root created by `tools.ops.fresh_start apply`, e.g. `/var/lib/solana-desk-fresh/<version>` (OUTSIDE the archived `/var/lib/solana-desk`) |
| `<RELEASE_DIR>` | staged release, e.g. `/opt/solana-desk-releases/<sha>` (also what `tools.ops.cutover` pins) |
| `<CONFIG>` | experiment config used by `fresh_start` (the file whose inode the dispatcher context pins) |
| `<SCHEDULER_IDENTITY>` | `scheduler_identity` (`dev:ino` of `paper-scheduler.lock`) printed by `fresh_start apply` |
| `<PACING_DB>` / `<PACING_DIR>` | the SHARED `provider-pacing.sqlite` and its directory |
| `<DISCOVERY_DB>` / `<DISCOVERY_DIR>` | the SHARED `discovery/continuous.sqlite` and its directory |
| `<TAKER>` / `<AMOUNT_RAW>` | the same values passed to `fresh_start` (defaults: production taker, `100000000`) |
| `<BACKUP_ROOT>` | `/var/backups/solana-desk/fresh-<version>`: the ONE backup destination (fresh_start's `--backup-dir` default, created by `apply`) |
| `<POOL_FEE_BPS>` | the fee hypothesis string passed to `fresh_start` (default `25`) |
| `<STATE_DIR>` | health/notify state, e.g. `/var/lib/solana-desk-health` (0700, owned by solana-desk) |

`dropins/70-entry-latch.conf` is the first-BUY latch drop-in (marker first line).

Do not substitute by hand: `python -m tools.ops.fresh_start render-units --root <FRESH_ROOT> --out DIR --release-dir <RELEASE_DIR>`
fills every placeholder from the applied manifest (and refuses any it cannot fill).

See `docs/ops/OPERATIONS_24x7.md` for install, enablement order and alert handling.

## Optional research units (T32G)

`desk-counterfactual.service|.timer`, `desk-held-watcher.service` and `desk-paper-held-cycle.path` are NOT part of the first
verified cycle. `render-units` writes them to `<out>/research/` (never to the top level), so the RUNBOOK's default install
(`install $UNITS/*.service $UNITS/*.timer /etc/systemd/system/`) cannot pick them up; `docs/ops/RUNBOOK.md` step 11 installs and
enables them explicitly. They use the same placeholders as the other templates. Their write scope: the counterfactual store
`<FRESH_ROOT>/counterfactual` plus the shared pacing database DIRECTORY (`<PACING_DIR>`, for SQLite's rollback journal), and
for the watcher only `<STATE_DIR>/held-watcher`.

`desk-notify-watchdog.timer` starts with `OnActiveSec=600` (a grace period counted from cutover) instead of `OnBootSec`, so it cannot
fire before the first healthcheck report exists.
