# Fresh-start deployment runbook (paper only)

Coordinator-only. Nothing here signs, broadcasts or spends; no step makes a provider call except the
services themselves once you deliberately start them. Every mutating tool below is a dry run unless
you add `--apply`. Read the dry-run output before re-running with `--apply`.

Tool flags for T02/T03/T05/T06/T07/T13 follow the task specs in `TASKS.md` on `cloud-queue`; confirm each
against `--help` of the merged tool before running (those tools are built by other tasks).
`tools.ops.cutover` (T11) is documented here from its code.

Placeholders: `SHA` = release commit, `NEW=/var/lib/solana-desk/exp-2026-10-11` (must not exist),
`OLD=/var/lib/solana-desk`, `REL=/opt/solana-desk-releases/$SHA`, `PY=/opt/solana-desk/.venv/bin/python`.

## 0. Preconditions

- Release commit reviewed; CI green on that exact commit; archive sha256 recorded.
- Old ledger is flat (no open position), or you are following "Rotate when flat" below.
- Decision on record: old stores are archived read-only and never modified (see DECISION in `TASKS.md`).
  `provider-pacing.sqlite`, `discovery/continuous.sqlite` and the provider keys file are shared and never reset.

## 1. Stage the release (no service impact)

```
sha256sum release-$SHA.tar.gz                       # record it
$PY -m tools.ops.cutover stage --tar release-$SHA.tar.gz --sha256 <HASH> --commit $SHA          # dry run
$PY -m tools.ops.cutover --apply stage --tar release-$SHA.tar.gz --sha256 <HASH> --commit $SHA
```

`stage` refuses a sha mismatch, absolute/`..`/backslash member names, any symlink or hardlink, duplicate
members, oversize archives and an already-staged commit, extracts into a temporary directory and renames it
into place only after the runtime digest computes. It prints `runtime_digest`; record it. Pass
`--expect-digest <hex>` to make a different digest a hard failure. Use `--strip-components 1` for wrapped tarballs.

## 2. Stop writers

Stop everything that writes the OLD stores, entry timer first:

```
systemctl stop desk-paper-entry-dispatcher.timer desk-paper-held-cycle.timer desk-paper-monitor.timer \
  desk-decisions.timer desk-discovery.timer desk-backup.timer
systemctl stop desk-paper-entry-dispatcher.service desk-paper-held-cycle.service desk-paper-monitor.service \
  desk-decisions.service desk-discovery.service desk-dashboard.service desk-continuous-discovery.service
systemctl is-active desk-paper-entry-dispatcher.service desk-paper-held-cycle.service   # expect inactive
```

If `desk-continuous-discovery` must keep feeding `discovery/continuous.sqlite`, leave it running and exclude
that file from the archive check (it is a shared input, not an old-experiment store).

## 3. Archive backup of the old stores (T03)

```
$PY -m tools.ops.backup --data $OLD --destination /var/backups/solana-desk/pre-fresh-$SHA \
  --label "pre-fresh-start $SHA" --require-quiesced desk-paper-entry-dispatcher.service desk-paper-held-cycle.service \
  --expect-count <N>
$PY -m tools.ops.verify_backup /var/backups/solana-desk/pre-fresh-$SHA        # must exit 0
```

## 4. Mark the old stores read-only

```
find $OLD -maxdepth 2 -name '*.sqlite' ! -name 'provider-pacing.sqlite' ! -path '*/discovery/*' -exec chmod 0400 {} +
```

Never touch `provider-pacing.sqlite`; the new experiment shares it. This `chmod` is the only intended change
to the old set; re-run `verify_backup` against `$OLD` (excluding the shared files) if you want proof.

## 5. Bootstrap the new store set (T13)

```
$PY -m tools.ops.fresh_start plan  --root $NEW --config config/experiments/paper-kraken-fresh.example.json \
   --pacing-db $OLD/provider-pacing.sqlite --discovery-db $OLD/discovery/continuous.sqlite
$PY -m tools.ops.fresh_start apply --root $NEW --config <same> --pacing-db <same> --discovery-db <same>
```

`apply` creates `$NEW` (0700, must not exist) and every store the entry/held path needs, and writes
`$NEW/fresh-start-manifest.json`. No reconciliation receipts or successor pins are involved.

## 6. Preflight dry run against copies (T05)

```
$PY -m tools.ops.preflight_dryrun --data $NEW --ledger <ledger name> --config <config> \
   --release-dir $REL --workdir /tmp/preflight-$SHA
```

Expect: terminal gate passes, dispatcher plan passes, scheduler pre-check passes, no `fresh != ctx`
context mismatch. Stop here on any other result. The live files are not modified.

## 7. Cutover with the entry timer OFF

Write the per-unit paths for the new set as a store-env file (cutover-store-env v1). If `fresh_start plan`
prints ExecStart/arguments in another shape, convert them into this JSON; the tool accepts nothing else:

```json
{"version": 1, "units": {
  "desk-paper-held-cycle.service": {
    "Environment": ["DESK_PROVIDER_PACING_DB=/var/lib/solana-desk/provider-pacing.sqlite"],
    "ExecStart": "/opt/solana-desk/.venv/bin/python -m tools.paper_scheduler --research-db /var/lib/solana-desk/exp-2026-10-11/research.sqlite --mode held -- ..."},
  "desk-paper-entry-dispatcher.service": {"ExecStart": "/opt/solana-desk/.venv/bin/python -m tools.paper_scheduler ..."}
}}
```

Rules enforced: unit names must match `desk-*.service|timer`; only `Environment` and `ExecStart` are accepted;
an ExecStart must be one absolute single-line command; duplicate keys, control characters and units outside
the cutover are rejected. `ExecStart=` is emitted as a clear-then-set pair.

Dry run, then apply. The entry service and timer are configured but never started (`--configure-only`,
`--keep-off`); a timer requires its same-named service in the cutover so it cannot run an unpinned release:

```
$PY -m tools.ops.cutover cutover --release $REL --store-env env.json \
   --units desk-dashboard.service desk-paper-held-cycle.timer desk-paper-held-cycle.service desk-decisions.service \
   --configure-only desk-paper-entry-dispatcher.service \
   --keep-off desk-paper-entry-dispatcher.timer \
   --expect-digest <runtime_digest from step 1>
$PY -m tools.ops.cutover --apply cutover <same arguments>
```

What `--apply` does, in order: validate everything (units, release is a canonical direct child of
`/opt/solana-desk-releases` containing `desk/`, runtime digest, loopback-only ExecStart, keep-off units not
active) before any write; write `60-reviewed-release.conf` drop-ins (marker comment, `WorkingDirectory=<release>`,
store-env settings); `daemon-reload`; check each service's effective `WorkingDirectory` and `ExecStart` via
`systemctl show` before starting anything; start only `--units` in order; re-check ActiveState/Result/
WorkingDirectory; confirm keep-off units stayed inactive. Any failure stops every unit the tool started
(reverse order) and leaves the drop-ins in place. Starting an `entry-dispatcher` unit additionally needs
`--allow-entry`; do not pass it until step 9.

An unmanaged `60-reviewed-release.conf` already on a unit makes the tool refuse. `--replace-existing` copies
it to `60-reviewed-release.conf.pre-cutover-<UTC>` first; rollback restores it.

The audit trail is `/var/lib/solana-desk-cutover/cutover-journal.jsonl` (0600, append-only, fsynced); override with `--journal`.

### Rollback

```
$PY -m tools.ops.cutover --apply rollback --units desk-dashboard.service desk-paper-held-cycle.service desk-decisions.service \
   desk-paper-entry-dispatcher.service
systemctl restart desk-dashboard.service       # deliberately, after checking the old release is what you want
```

Removes only drop-ins carrying the marker (restoring a backed-up predecessor if one was recorded), then
`daemon-reload`. It does not stop or start units. Hand-written drop-ins and other `*.conf` files are left alone.
Never restore old backups over the new stores to "roll back code".

## 8. Verify (T02)

```
$PY -m tools.ops.status --data $NEW --ledger $NEW/<ledger>.sqlite --config <config> --release-dir $REL --systemd
```

Check: runtime digest equals step 1, `blockers` empty, no NULL passes, mode RUNNING, dashboard on 127.0.0.1:8765 only,
all `desk-*` units show the new `WorkingDirectory`. Take the pre-entry snapshot now:

```
$PY -m tools.ops.verify_cycle snapshot --data $NEW --ledger <ledger> --config <config> --out /root/snap-before.json
```

## 9. Enable entries and latch (T07)

```
$PY -m tools.ops.cutover --apply cutover --release $REL --store-env env.json --allow-entry \
   --units desk-paper-entry-dispatcher.service ...        # only if the service itself should run once
systemctl enable --now desk-paper-entry-dispatcher.timer
```

For the controlled first cycle, install the latch from `deploy/drop-ins/entry-latch.conf.example` (T07) so new
admissions stop after the first persisted BUY while held monitoring continues to an exit.

## 10. Cold restart check (T06)

After a position has been opened or closed and with no new activity expected:

```
systemctl restart desk-paper-held-cycle.timer desk-dashboard.service
$PY -m tools.ops.verify_cycle snapshot --data $NEW --ledger <ledger> --config <config> --out /root/snap-after.json
$PY -m tools.ops.verify_cycle compare /root/snap-before.json /root/snap-after.json            # exact
$PY -m tools.ops.verify_cycle compare /root/snap-before.json /root/snap-after.json --allow-progress
```

Any differing fill, checkpoint or budget, or a duplicate fill/charge, is a failure. A cycle is verified only
when `snapshot` reports `VALIDATED_LIVE_DATA_PAPER_ROUND_TRIP` and the compare passes.

## Rotate when flat

Code under `desk/` changed and the ledger holds no position: do not pin a successor. Roll to a new store set.

1. `$PY -m tools.ops.status ...` → `blockers` must show no held position and no unresolved exit; mode `RUNNING` or `ENTRY_PAUSED`.
2. Stop the entry timer, then the held/monitor/decision units (step 2).
3. Backup the current set (step 3) and mark it read-only (step 4).
4. `$PY -m tools.ops.fresh_start rotate --from $NEW --to /var/lib/solana-desk/exp-<next> --config <new config> --pacing-db ... --discovery-db ...`
   (refuses an open position; leaves `--from` untouched).
5. Stage the new release (step 1), run preflight on the new root (step 6), cut over (step 7) with the entry timer still off, verify (step 8), then enable entries (step 9).

If a position IS open, do not rotate: the existing successor machinery applies and that is a separate coordinator procedure.

## Things that are deliberately not automated

- Stopping writers, `chmod` of old stores and enabling the entry timer are explicit commands above, not side effects of `cutover`.
- `cutover` never edits `deploy/*.service` templates, never touches stores, and never calls a provider.
- Dashboard exposure: any ExecStart (override, base unit or the live effective value) that binds off loopback aborts the cutover.
