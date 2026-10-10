# Fresh-start unit templates (24/7)

Templates for the fresh-start deployment. They are NOT installed by anything in the repository and never
modify `deploy/*.service|*.timer`. Placeholders (substitute every one before install; the test renders them):

| Placeholder | Meaning |
|---|---|
| `<FRESH_ROOT>` | the root created by `tools.ops.fresh_start apply`, e.g. `/var/lib/solana-desk/exp-2026-10-11` |
| `<RELEASE_DIR>` | staged release, e.g. `/opt/solana-desk-releases/<sha>` (also what `tools.ops.cutover` pins) |
| `<CONFIG>` | experiment config used by `fresh_start` (the file whose inode the dispatcher context pins) |
| `<SCHEDULER_IDENTITY>` | `scheduler_identity` (`dev:ino` of `paper-scheduler.lock`) printed by `fresh_start apply` |
| `<PACING_DB>` / `<PACING_DIR>` | the SHARED `provider-pacing.sqlite` and its directory |
| `<DISCOVERY_DB>` / `<DISCOVERY_DIR>` | the SHARED `discovery/continuous.sqlite` and its directory |
| `<TAKER>` / `<AMOUNT_RAW>` | the same values passed to `fresh_start` (defaults: production taker, `100000000`) |
| `<BACKUP_ROOT>` | e.g. `/var/backups/solana-desk/fresh` |
| `<STATE_DIR>` | health/notify state, e.g. `/var/lib/solana-desk-health` (0700, owned by solana-desk) |

See `docs/ops/OPERATIONS_24x7.md` for install, enablement order and alert handling.
