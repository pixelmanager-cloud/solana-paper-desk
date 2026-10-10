# Fresh-start deployment runbook (paper only)

Coordinator-only. Nothing here signs, broadcasts or spends; no step makes a provider call except the
services themselves once you deliberately start them. Every mutating tool below is a dry run unless you add
its apply flag (`--apply` for `tools.ops.cutover`, `--execute --approved-plan-hash` for `fresh_start`).
Read the dry-run output before running the real thing.

Flags of the other tools are quoted from their branches (`cloud/T02`, `T03`, `T05`, `T06`, `T07`, `T13`) at the
time of writing. Confirm each against `--help` of the merged tool before running it.

## Variables used below

```
SHA=<release commit>                       VER=2026-10-11.paper-quote-kraken.fresh.1
PY=/opt/solana-desk/.venv/bin/python
OLD=/var/lib/solana-desk                   # archived read-only after step 4
FRESH=/var/lib/solana-desk-fresh           # NEW ROOTS LIVE OUTSIDE $OLD, one directory per experiment version
NEW=$FRESH/$VER                            # must not exist yet
REL=/opt/solana-desk-releases/$SHA         # staged release (step 1)
SRC=<reviewed git checkout of $SHA>        # only used to run `stage`, because $REL does not exist yet
CFG=/etc/solana-paper/paper-kraken-fresh.json   # copy of config/experiments/paper-kraken-fresh.example.json
LEDGER=$NEW/paper-ledger.sqlite
```

### Working directory rule (exact cwd for every `python -m tools.ops.*`)

| Command | cwd | Why |
|---|---|---|
| `tools.ops.cutover stage` | `$SRC` | The release is not staged yet; run the reviewed checkout of the same commit. `cutover.py` only needs the standard library. |
| every other `tools.ops.*` command | `$REL` | The ledger, evidence store and dispatcher context record the implementation hash of the code that created them. Running from anywhere else records, or later verifies against, the wrong code. |
| `systemctl`, `chmod`, `jq`, `install` | any | |

Never run a tool from `$OLD`, from `/root`, or from a checkout whose `desk/` differs from `$REL/desk/`
(`implementation_hash()` hashes every `.py` and `.json` under `desk/`; compare with step 1's `runtime_digest`).

## 0. Preconditions

- Release commit reviewed; CI green on that exact commit; archive sha256 recorded.
- Old ledger is flat (no open position), otherwise do not start: see "If a position is open" at the end.
- Decision on record (see DECISION in `TASKS.md`): old stores are archived read-only and never modified.
  `provider-pacing.sqlite`, `discovery/continuous.sqlite` and the provider keys file are shared and never reset.
- `$FRESH` exists once, owned by the service user, canonical (no symlinks):
  `install -d -m 0755 -o solana-desk -g solana-desk $FRESH`. `$NEW` itself must NOT exist.
- Freeze `desk/` and `tools/` now. Any later byte change under `desk/` changes the runtime identity pinned in the new
  ledger, the monitoring allowance and the dispatcher context; the answer is a new rotation (below), not a pin.

## 1. Stage the release (no service impact)

```
cd $SRC
sha256sum release-$SHA.tar.gz                                   # record it
$PY -m tools.ops.cutover stage --tar release-$SHA.tar.gz --sha256 <HASH> --commit $SHA          # dry run
$PY -m tools.ops.cutover --apply stage --tar release-$SHA.tar.gz --sha256 <HASH> --commit $SHA
```

`stage` hashes and extracts from the same open file handle; it refuses a sha mismatch, absolute/`..`/backslash
member names, any symlink or hardlink, colliding or duplicate members, oversize archives and an already-staged commit.
It extracts into a temporary directory and renames it into place only after the runtime digest computes. It prints
`runtime_digest`; record it as `DIGEST`. Pass `--expect-digest <hex>` to make a different digest a hard failure.
Use `--strip-components 1` for wrapped tarballs.

## 2. Stop writers

Stop everything that writes the OLD stores, entry timer first. Stopping is deliberate and separate from `cutover`:

```
systemctl stop desk-paper-entry-dispatcher.timer desk-paper-held-cycle.timer desk-paper-monitor.timer \
  desk-decisions.timer desk-discovery.timer desk-backup.timer
systemctl stop desk-paper-entry-dispatcher.service desk-paper-held-cycle.service desk-paper-monitor.service \
  desk-decisions.service desk-discovery.service desk-recorder.service desk-dashboard.service desk-continuous-discovery.service
systemctl is-active desk-paper-entry-dispatcher.service desk-paper-held-cycle.service desk-continuous-discovery.service   # expect inactive
```

`desk-discovery` (writes `launches.sqlite`) and `desk-recorder` (writes `raw.sqlite`) feed archived stores only; leave them
stopped and out of the new cutover (the archived-store guard refuses them anyway). Continuous discovery is restarted by the
cutover in step 7 as a long-running service: it feeds `discovery/continuous.sqlite`, a shared input that the new
experiment reads as its candidate source.

## 3. Archive backup of the old stores (T03)

```
cd $REL
$PY -m tools.ops.backup --data $OLD --destination /var/backups/solana-desk/pre-fresh-$SHA \
  --label "pre-fresh-start $SHA" \
  --require-quiesced desk-paper-entry-dispatcher.service desk-paper-held-cycle.service desk-paper-monitor.service \
                     desk-decisions.service desk-discovery.service desk-recorder.service desk-continuous-discovery.service \
  --expect-count 14
$PY -m tools.ops.verify_backup /var/backups/solana-desk/pre-fresh-$SHA        # must exit 0
```

`--expect-count 14` is the 12 production stores plus `demo.sqlite` and `verified-demo.sqlite`. A different count means the
data directory is not what this runbook assumes: stop and investigate, do not adjust the number to make it pass.

## 4. Mark the old stores read-only

```
find $OLD -maxdepth 2 -name '*.sqlite' ! -name 'provider-pacing.sqlite' ! -path '*/discovery/*' -exec chmod 0400 {} +
```

Never touch `provider-pacing.sqlite` or `discovery/continuous.sqlite`: the new experiment shares them. This `chmod` is the
only intended change to the old set.

## 5. Bootstrap the new store set (T13)

`apply` must see the shared pacing database in its environment (it refuses otherwise). Dry-run plan first and record
`plan_hash`:

```
cd $REL
install -m 0644 config/experiments/paper-kraken-fresh.example.json $CFG       # review before use
export DESK_PROVIDER_PACING_DB=$OLD/provider-pacing.sqlite
$PY -m tools.ops.fresh_start plan --root $NEW --config $CFG \
   --pacing-db $OLD/provider-pacing.sqlite --discovery-db $OLD/discovery/continuous.sqlite
$PY -m tools.ops.fresh_start apply --root $NEW --config $CFG \
   --pacing-db $OLD/provider-pacing.sqlite --discovery-db $OLD/discovery/continuous.sqlite \
   --execute --approved-plan-hash <plan_hash>
```

`apply` creates `$NEW` (0700, must not exist) and every store the entry/held path needs, runs the real dispatcher preflight
and terminal gate against them, and writes `$NEW/fresh-start-manifest.json` (config digest, implementation hash, store list,
scheduler lock identity, and the per-unit `units` arguments consumed in step 7). No reconciliation receipts or successor
pins are involved. Its `implementation_hash` must equal `DIGEST` from step 1.

## 6. Preflight dry run against copies (T05)

```
cd $REL
$PY -m tools.ops.preflight_dryrun --data $NEW --ledger paper-ledger.sqlite --config $CFG \
   --release-dir $REL --workdir /tmp/preflight-$SHA
```

Expect: terminal gate passes, dispatcher plan passes, scheduler pre-check passes, no `fresh != ctx` context mismatch.
Stop here on any other result. The live files are not modified.

**Known gap (as of the T05 branch):** T05 resolves `--pacing-db` and `--discovery-db` relative to `--data` and copies every
`*.sqlite` under `--data`. In the fresh layout those two shared files live under `$OLD`, outside `$NEW`, so T05 cannot see them
and will fail or report an incomplete check. Do NOT copy the shared pacing database into `$NEW` (a copy diverges from the shared
two-second pacing state and must never be reset or forked). Until T05 accepts absolute shared paths, `fresh_start apply` in
step 5 has already run the real dispatcher preflight and the terminal gate against the new stores (its self-check), and step 8
re-reads the state; treat this step as not available rather than working around it.

## 7. Cutover with the entry timer OFF

### 7a. Build the store-env file

The T13 manifest already contains the exact arguments of the five units it knows (entry dispatcher, held cycle, monitor,
decisions, dashboard) in the shape `{"desk-x": {"environment": [...], "argv": [...]}}`. `cutover` consumes that shape directly
and renders it to systemd correctly (interpreter prepended, systemd quoting, `%` escaped as `%%` except a leading `%d/`
credentials path, `$` escaped as `$$`). Units T13 does not know need a hand-written entry in the same file. The backup unit
must be pointed at the new root, otherwise the archived-store guard refuses it:

```
jq --arg new "$NEW" '{version: 1, units: (.units + {
     "desk-backup.service": {"ExecStart":
       ("/opt/solana-desk/.venv/bin/python -m desk.backup --data " + $new + " --root /var/backups/solana-desk/fresh-daily --keep 7")}})}' \
   $NEW/fresh-start-manifest.json > /root/store-env-$SHA.json
```

A store-env that is the manifest itself, the bare `units` map, or `{"version":1,"units":{...}}` is accepted. A `fresh_start plan`
output is refused (its scheduler identity is a placeholder). Units named in the file but not in the cutover lists are
ignored and reported under `store_env_ignored`.

For the continuous-discovery, held, monitor, decisions, dashboard and backup units, install the unit files from `deploy/fresh/`
(task T21) or your reviewed copies under `/etc/systemd/system/` first. The stock `deploy/desk-continuous-discovery.service`
exits after `--seconds 86400` and is not suitable for unattended running.

### 7b. Dry run, then apply

Continuous discovery, held cycle, monitor, decisions, dashboard and backup all run in the new set. The entry service is only
configured, and the entry timer is kept off. `--archived-root` is repeatable; list `$OLD` (and, on a later rotation, the
previous fresh root). Shared pacing and discovery inputs under `$OLD` are always permitted:

```
cd $REL
UNITS_ON="desk-continuous-discovery.service desk-dashboard.service \
  desk-paper-held-cycle.service desk-paper-held-cycle.timer \
  desk-paper-monitor.service desk-paper-monitor.timer \
  desk-decisions.service desk-decisions.timer \
  desk-backup.service desk-backup.timer"
$PY -m tools.ops.cutover cutover --release $REL --store-env /root/store-env-$SHA.json \
   --units $UNITS_ON \
   --configure-only desk-paper-entry-dispatcher.service \
   --keep-off desk-paper-entry-dispatcher.timer \
   --archived-root $OLD --expect-digest <DIGEST from step 1>
$PY -m tools.ops.cutover --apply cutover <same arguments>
```

What `--apply` does, in order:

1. Validate everything before any write: unit names; the release is a canonical direct child of `/opt/solana-desk-releases`
   containing `desk/`; runtime digest; loopback-only ExecStart (the unit file, drop-ins and store-env); no unit references an
   archived store root (the old stores must never be written by the new experiment); every unit in `--units` is
   `inactive` or `failed`; keep-off units are inactive.
2. Disable every keep-off unit that is enabled (`systemctl disable`), re-check `is-enabled`, and refuse if it is still enabled, so a
   reboot cannot start entries early. Reported under `keep_off`.
3. Write `60-reviewed-release.conf` drop-ins (marker comment on the first line, `WorkingDirectory=<release>`, store-env settings;
   files and directory are fsynced), `daemon-reload`, then check each service's effective `WorkingDirectory`, every `Exec*` line
   and `Environment` via `systemctl show` (no off-loopback bind, no archived store).
4. Start only `--units`, recording each unit as started before its `start` call so that a failed or hung start is also stopped.
5. Verify the new process, not just a name: for services, `ExecMainStartTimestampMonotonic` is newer than the cutover start and
   `MainPID` changed (oneshot: it ran after the start and succeeded); for timers, a next trigger is scheduled.
   `activating` and `auto-restart` are failures.
6. Wait `--settle-seconds` (default 10), re-check health, and require `NRestarts` and `MainPID` unchanged.
7. Confirm keep-off units are still inactive and disabled.

Any failure stops every unit the tool started (reverse order) and leaves the drop-ins in place for rollback. Starting an
`entry-dispatcher` unit additionally needs `--allow-entry`; do not pass it until step 9. An unmanaged
`60-reviewed-release.conf` already on a unit makes the tool refuse; `--replace-existing` copies it to
`60-reviewed-release.conf.pre-cutover-<UTC>` first and rollback restores it. The audit trail is
`/var/lib/solana-desk-cutover/cutover-journal.jsonl` (0600, append-only, fsynced); override with `--journal`.

A `--units` member that is already running makes the tool refuse (a `start` would be a silent no-op and the old code would
keep running). Stop it deliberately (step 2) and re-run.

### Rollback

Rollback refuses while any managed unit, or the timer/service partner of one, is active. Pass `--stop` to have it stop them
first (timers before services); it verifies they stopped before it removes a single drop-in. It never starts anything.

```
cd $REL
$PY -m tools.ops.cutover --apply rollback --stop --units desk-continuous-discovery.service desk-dashboard.service \
   desk-paper-held-cycle.service desk-paper-monitor.service desk-decisions.service desk-backup.service \
   desk-paper-entry-dispatcher.service
```

Rollback removes only drop-ins whose FIRST line is the marker (restoring a backed-up predecessor if one was recorded), then
`daemon-reload`. Hand-written drop-ins and other `*.conf` files are left alone. After a rollback the units point at their
original configuration, which is the archived store set: do not restart them against the old stores unless that is
what you intend. Never restore old backups over the new stores to "roll back code".

## 8. Verify (T02)

```
cd $REL
$PY -m tools.ops.status --data $NEW --ledger $LEDGER --config $CFG --release-dir $REL --systemd
ss -ltn 'sport = :8765'            # the dashboard listens on 127.0.0.1 only
```

Check: runtime digest equals `DIGEST`, `blockers` empty, no NULL passes, mode RUNNING, every `desk-*` unit shows the new
`WorkingDirectory`, discovery rows are fresh. Take the pre-entry snapshot now:

```
$PY -m tools.ops.verify_cycle snapshot --data $NEW --ledger $LEDGER --config $CFG --out /root/snap-before.json
```

## 9. Latch, then enable entries (T07)

Install the latch drop-in BEFORE enabling the entry timer, so new admissions stop after the first persisted BUY while held
monitoring continues to an exit. Start from `deploy/drop-ins/entry-latch.conf.example` (T07) and replace its example paths
with the new ones:

```
install -d /etc/systemd/system/desk-paper-entry-dispatcher.service.d
sed -e "s#--config [^ ]*#--config $CFG#g" -e "s#--ledger [^ ]*#--ledger $LEDGER#g" \
  deploy/drop-ins/entry-latch.conf.example > /etc/systemd/system/desk-paper-entry-dispatcher.service.d/70-entry-latch.conf
grep -n 'ExecStart' /etc/systemd/system/desk-paper-entry-dispatcher.service.d/70-entry-latch.conf    # verify the paths by eye
systemctl daemon-reload
```

(`70-entry-latch.conf` is not a `60-reviewed-release.conf`; `cutover` and `rollback` never touch it.) Run the latch tool
read-only once (`$PY -m tools.ops.entry_latch --config $CFG --ledger $LEDGER` from `$REL`: no BUY, nothing to do), then enable
entries by the coordinator's explicit decision only:

```
systemctl enable --now desk-paper-entry-dispatcher.timer
```

The entry service was configured but not started by step 7. If the service itself must run once, re-run `cutover`
with `--allow-entry --units desk-paper-entry-dispatcher.service` (all other arguments unchanged).

## 10. Cold restart check (T06)

After a position has been opened or closed and with no new activity expected:

```
cd $REL
systemctl restart desk-paper-held-cycle.timer desk-dashboard.service
$PY -m tools.ops.verify_cycle snapshot --data $NEW --ledger $LEDGER --config $CFG --out /root/snap-after.json
$PY -m tools.ops.verify_cycle compare /root/snap-before.json /root/snap-after.json            # exact
$PY -m tools.ops.verify_cycle compare /root/snap-before.json /root/snap-after.json --allow-progress
```

Any differing fill, checkpoint or budget, or a duplicate fill/charge, is a failure. A cycle is verified only when `snapshot`
reports `VALIDATED_LIVE_DATA_PAPER_ROUND_TRIP` and the compare passes.

## Rotate when flat

Code under `desk/` changed (or a new experiment version starts) and the ledger holds no position: do not pin a successor. Roll
to a new store set. The previous fresh root becomes an archived root too.

1. `cd $REL && $PY -m tools.ops.status --data $NEW ...` → no held position, no unresolved exit; mode `RUNNING` or `ENTRY_PAUSED`.
2. Stop the entry timer, then the held/monitor/decisions units (step 2, with the current unit set).
3. Backup the current root (step 3, `--data $NEW`, with its own `--expect-count`) and mark its stores read-only (step 4, using
   `chmod` on `$NEW`).
4. Stage the new release (step 1), then from the NEW release directory:
   `$PY -m tools.ops.fresh_start rotate --from $NEW --to $FRESH/<next-version> --config <new config> --pacing-db $OLD/provider-pacing.sqlite --discovery-db $OLD/discovery/continuous.sqlite --execute --approved-plan-hash <hash>`
   (it refuses an open position and leaves `--from` untouched; run `plan`/dry run first).
5. Preflight (step 6), then cut over (step 7) with BOTH archived roots: `--archived-root $OLD --archived-root $NEW`, the new
   store-env built from the new manifest, and the entry timer still off. Verify (step 8), install the latch with the new
   ledger path (step 9), then enable entries.

## If a position is open

Do not rotate and do not change `desk/`: the existing successor machinery applies and that is a separate coordinator
procedure. Held monitoring must keep running; do not stop the held cycle or the monitor to deploy anything else.

## Things that are deliberately not automated

- Stopping writers, `chmod` of old stores and enabling the entry timer are explicit commands above, not side effects of `cutover`.
- `cutover` never edits `deploy/*.service` templates, never touches stores, and never calls a provider.
- Dashboard exposure: any ExecStart (override, base unit or the live effective value) that binds off loopback aborts the cutover.
- Writing to the archived stores: any unit that references an archived root (other than the shared pacing and discovery inputs)
  aborts the cutover before anything is written; `--allow-archived <unit>` is an explicit, per-unit exception.
