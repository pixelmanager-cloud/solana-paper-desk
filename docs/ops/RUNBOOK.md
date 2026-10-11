# Fresh-start deployment runbook (paper only)

Coordinator-only. Nothing here signs, broadcasts or spends; no step makes a provider call except the
services themselves once you deliberately start them. Every mutating tool below is a dry run unless you add
its apply flag (`--apply` for `tools.ops.cutover`, `--execute --approved-plan-hash` for `fresh_start`).
Read the dry-run output before running the real thing.

This is ONE sequence. `tests/test_ops_e2e_deploy.py` extracts the `$PY -m tools.ops.*` commands from the fenced blocks
below, runs them in order against a temp filesystem with a fake `systemctl`, and fails when a flag or argument here no
longer matches the tools. Keep the commands literal; do not paraphrase them.

## Variables used below

```
SHA=<release commit>                       VER=2026-10-11.paper-quote-kraken.fresh.1
PY=/opt/solana-desk/.venv/bin/python
OLD=/var/lib/solana-desk                   # archived read-only after step 4
FRESH=/var/lib/solana-desk-fresh           # NEW ROOTS LIVE OUTSIDE $OLD, one directory per experiment version
NEW=$FRESH/$VER                            # must not exist yet
BK=/var/backups/solana-desk/fresh-$VER     # the ONE backup destination (fresh_start default; created by apply)
STATE=/var/lib/solana-desk-health          # health/notify state (0700, solana-desk)
REL=/opt/solana-desk-releases/$SHA         # staged release (step 1)
SRC=<reviewed git checkout of $SHA>        # only used to run `stage`, because $REL does not exist yet
CFG=/etc/solana-paper/paper-kraken-fresh.json   # copy of config/experiments/paper-kraken-fresh.example.json
LEDGER=$NEW/paper-ledger.sqlite
PACING=$OLD/provider-pacing.sqlite         # SHARED, never reset or copied
DISCOVERY=$OLD/discovery/continuous.sqlite # SHARED candidate source
UNITS=/root/units-$SHA                     # rendered unit files (step 7)
INV=/var/backups/solana-desk/systemd-inventory-$SHA   # saved ls -la + systemctl cat of every desk unit (step 2)
SDA=/var/backups/solana-desk/systemd-archive-$SHA     # the Codex-era unit files and drop-in stacks, moved here (step 7a)
SM=/var/backups/solana-desk/seal-manifest-$SHA.json   # what step 4 changed (path, uid, gid, mode, sha256); `unseal` restores it
```

### Working directory rule (exact cwd for every `python -m tools.ops.*`)

| Command | cwd | Why |
|---|---|---|
| `tools.ops.cutover stage` | `$SRC` | The release is not staged yet; run the reviewed checkout of the same commit. |
| every other `tools.ops.*` command | `$REL` | The ledger, evidence store and dispatcher context record the implementation hash of the code that created them. |
| `systemctl`, `install`, `systemd-analyze` | any | |

Read-only tools (`healthcheck`, `status`, `verify_cycle`, `entry_latch`) run as the service user, not as root:
prefix them with `runuser -u solana-desk --`. A root-run reader can leave root-owned `-shm`/`-wal` files in the
store directory that the services then cannot open. Mutating steps (`cutover`, `fresh_start`, `backup`) keep running as root.

Never run a tool from `$OLD`, from `/root`, or from a checkout whose `desk/` differs from `$REL/desk/`
(`implementation_hash()` hashes every `.py` and `.json` under `desk/`; compare with step 1's `runtime_digest`).

Every tool invocation below runs with cwd = $REL (`cd $REL` first; step 1 uses `$SRC`). That is not cosmetic: Python puts the
current directory first on `sys.path`, so `desk` and `tools` come from `$REL`. A stale `desk` package installed into the venv's
site-packages (an older `pip install -e .` or wheel) would otherwise win from any other directory and the tool would hash and
pin the WRONG code. The services are immune (their `WorkingDirectory` is `$REL`), the tools you type are not. Optionally remove
the stale package once, so a wrong cwd fails loudly instead of silently using old code:

```
$PY -m pip uninstall -y solana-desk
```

## 0. Preconditions

- Release commit reviewed; CI green on that exact commit; archive sha256 recorded.
- Old ledger is flat (no open position), otherwise do not start: see "If a position is open" at the end.
- Decision on record (see DECISION in `TASKS.md`): old stores are archived read-only and never modified.
  `provider-pacing.sqlite`, `discovery/continuous.sqlite` and the provider keys file are shared and never reset.
- `$FRESH` and `$STATE` exist, owned by the service user, canonical (no symlinks):
  `install -d -m 0755 -o solana-desk -g solana-desk $FRESH` and `install -d -m 0700 -o solana-desk -g solana-desk $STATE`.
- `/var/backups/solana-desk` exists and is `root:root 0755` on the VPS; it STAYS root-owned. Never chown or chmod it, and never
  `install` into it: the archive backups, the inventory, the seal manifest and the systemd archive are written there by root, and
  `apply` (run as root with `--chown solana-desk`) creates only `$BK` inside it (0700, owned by the service user), which is the
  one place the service user writes backups.
- `$NEW` itself must NOT exist; `apply` creates it and `$BK`.
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
It prints `runtime_digest`; record it as `DIGEST`. Use `--strip-components 1` for wrapped tarballs.

## 2. Inventory, then stop writers

**Inventory first, before anything is changed.** It saves `ls -la` of every `/etc/systemd/system/desk-*` entry (unit files AND
their `*.d` drop-in directories) and `systemctl cat` of every desk unit, with sha256 per file, to a new `0700` directory:

```
cd $REL
$PY -m tools.ops.cutover inventory --out $INV
$PY -m tools.ops.cutover --apply inventory --out $INV
```

Production units carry a stack of Codex-era drop-ins (`60-`, `90..99-reviewed-*`, `zz-`, `zzz-`, `zzzz-`) that load after any
drop-in written by this flow. They are never layered on: step 7a MOVES them to `$SDA`.

Stop everything that writes the OLD stores, timers first. Stopping is deliberate and separate from `cutover`:

```
systemctl stop desk-paper-entry-dispatcher.timer desk-paper-held-cycle.timer desk-paper-monitor.timer \
  desk-decisions.timer desk-discovery.timer desk-backup.timer
systemctl stop desk-paper-entry-dispatcher.service desk-paper-held-cycle.service desk-paper-monitor.service \
  desk-decisions.service desk-discovery.service desk-recorder.service desk-dashboard.service desk-continuous-discovery.service
systemctl reset-failed 'desk-*'
systemctl is-active desk-paper-entry-dispatcher.service desk-paper-held-cycle.service desk-continuous-discovery.service   # expect inactive
```

`reset-failed` clears the `failed` state of units that died before this deployment (on the VPS `desk-paper-entry-dispatcher.service`
and `desk-decisions.service` are `failed`): a stop leaves a failed unit failed, and the checks below and the health tools should
start from a clean slate. It only resets state; it starts and stops nothing.

Then DISABLE every old unit so a reboot cannot restart anything against the archived stores. The units the fresh set reuses
(`desk-paper-held-cycle.timer`, `desk-decisions.timer`, `desk-backup.timer`, `desk-dashboard.service`,
`desk-continuous-discovery.service`) are enabled again by `cutover --enable` in step 7b once they are healthy; the rest
(`desk-discovery.*`, `desk-recorder.service`, the old monitor and entry timers) stay disabled:

```
systemctl disable desk-paper-entry-dispatcher.timer desk-paper-held-cycle.timer desk-paper-monitor.timer desk-decisions.timer \
  desk-discovery.timer desk-backup.timer desk-discovery.service desk-recorder.service desk-dashboard.service desk-continuous-discovery.service
systemctl is-enabled desk-discovery.timer desk-recorder.service desk-paper-monitor.timer          # expect disabled or static
```

Most of the old units have no `[Install]` section, so `is-enabled` prints `static`, not `disabled` (`systemctl disable` on a
static unit is a harmless no-op and it stays `static`): both mean "no boot persistence", and both are fine. Anything printing `enabled`, `enabled-runtime`, `linked` or `alias` is a stop.

`desk-discovery` (writes `launches.sqlite`) and `desk-recorder` (writes `raw.sqlite`) feed archived stores only; leave them
stopped and out of the new cutover. Continuous discovery is restarted by the cutover in step 7 as a long-running service:
it feeds `discovery/continuous.sqlite`, the shared input the new experiment reads as its candidate source.

## 3. Archive backup of the old stores (T03)

```
cd $REL
$PY -m tools.ops.backup --data $OLD --destination /var/backups/solana-desk/pre-fresh-$SHA \
  --label "pre-fresh-start $SHA" \
  --require-quiesced desk-paper-entry-dispatcher.service desk-paper-held-cycle.service desk-paper-monitor.service \
                     desk-decisions.service desk-discovery.service desk-recorder.service desk-dashboard.service \
                     desk-continuous-discovery.service \
  --expect-count 14
$PY -m tools.ops.verify_backup /var/backups/solana-desk/pre-fresh-$SHA        # must exit 0
```

`--expect-count 14` is the 12 production stores plus `demo.sqlite` and `verified-demo.sqlite`. A different count means the
data directory is not what this runbook assumes: stop and investigate, do not adjust the number to make it pass.
This destination (`pre-fresh-<sha>`) is the archive of the OLD set; the fresh set's backups go to `$BK` only.
Continuous discovery and the dashboard are in `--require-quiesced`, so they stay stopped for the whole backup; discovery is
started again in step 7b against the SHARED `$DISCOVERY` (the DECISION keeps that input shared, so no fresh discovery path exists).

## 4. Seal the old stores (read-only to the service user)

```
cd $REL
$PY -m tools.ops.cutover seal-archive --root $OLD --shared-path $PACING --shared-path $DISCOVERY --manifest $SM
$PY -m tools.ops.cutover --apply seal-archive --root $OLD --shared-path $PACING --shared-path $DISCOVERY --manifest $SM
```

Seal changes ONLY an explicit allow-list: the sqlite family (`*.sqlite`, `-wal`, `-shm`, `-journal`) and the JSON evidence files
(`*.json`, `*.jsonl`: `acquisition-*.json`, `paper-target-*.json`, `*intake*.json`, event logs; the production archived root holds 74, of which 5 are root-only and stay 0600 root: 69 are sealed);
add `--seal-pattern <glob>` for anything else, never a lock. Those files become `root:solana-desk 0440` and their directories `root:solana-desk 0550`, so the
service user can read the old stores but cannot modify, delete or replace them, and nobody else can read them. Files that are
already root-only (`root`, no group/other bits, e.g. a `0600` file) stay exactly as they are.

Never touched, whatever the patterns say: every `*.lock` file (`discovery/continuous.sqlite.discovery.lock` is opened
read-write by continuous discovery, `provider-pacing.sqlite.holder-*.lock` / `*.paper-cycle.lock` /
`*.ownership-invocation.lock` / `paper-scheduler.lock` are flock'ed by running processes), the two SHARED databases, and any file
outside the allow-list (listed as `unlisted` in the output). Sealing a lock file would kill continuous discovery and the
step-7b cutover, so read `untouched_locks` and `unlisted` in the dry run before applying.
The JSON patterns also match any `*.json` that a LIVE process still rewrites inside `$OLD` or `$OLD/discovery`: read the dry run's
`files` count and the manifest's `sealed` list once, and add nothing to the allow-list that a running unit writes.

The shared pacing database uses a rollback journal (`<db>-journal` is created and removed per write), so the service user must
be able to create files in its DIRECTORY (`$OLD`), and the discovery database the same in `$OLD/discovery`. Those two
directories are therefore `root:solana-desk 1770`: group write lets the service create the journal and its lock files, the sticky
bit stops it from deleting or renaming any root-owned (archived) file there, and the mode gives other users nothing. The shared
pacing database is deliberately NOT moved (its migration receipt binds its path, and the pin binds mode 0600 on that exact path).
Never touch `provider-pacing.sqlite` or `discovery/continuous.sqlite` otherwise: the new experiment shares them.

**Before anything is changed** the tool writes `$SM` (new file, `0600`, outside `$OLD`; it refuses an existing one): the path, type,
uid, gid, mode and sha256 of every entry it is about to change. `tools.ops.cutover unseal --manifest $SM` puts exactly those
owners and modes back, and refuses (changing nothing) if a sealed store was modified meanwhile. Keep `$SM` with the backups.

## 5. Bootstrap the new store set (T13)

`apply` must see the shared pacing database in its environment (it refuses otherwise), and runs as the service user or as
root with `--chown`. `--enable-held` is REQUIRED for a desk that exits positions: without it the held unit is emitted with
`--dependency-blocker`, exits BLOCKED (rc 2), never monitors, and `cutover` refuses it. Dry-run plan first and record `plan_hash`:

```
cd $REL
install -m 0644 config/experiments/paper-kraken-fresh.example.json $CFG       # review before use
export DESK_PROVIDER_PACING_DB=$PACING
$PY -m tools.ops.fresh_start plan --root $NEW --config $CFG --pacing-db $PACING --discovery-db $DISCOVERY --enable-held
$PY -m tools.ops.fresh_start apply --root $NEW --config $CFG --pacing-db $PACING --discovery-db $DISCOVERY \
   --enable-held --service-user solana-desk --chown solana-desk --execute --approved-plan-hash <plan_hash>
```

`apply` creates `$NEW` (0700, must not exist) and every store the entry/held path needs, creates `$BK` (0700, service user;
its parent must exist), runs the real dispatcher preflight and terminal gate against the new stores, and writes
`$NEW/fresh-start-manifest.json` (config digest, implementation hash, store list, scheduler lock identity, the per-unit
`units` arguments and `unit_sections`). No reconciliation receipts or successor pins are involved. Its `implementation_hash`
must equal `DIGEST` from step 1.

## 6. Preflight dry run against copies (T05/T05F + T34)

`apply` in step 5 already ran the real dispatcher preflight and the terminal gate against the new stores (its self-check).
This repeats them on COPIES with the real entry-scheduler pre-check, as root (it needs a private mount namespace; the child
sees the copies at the real paths and never touches a live store). The shared pacing and discovery databases live OUTSIDE
`$NEW`, so both must be named: without `--pacing-db $PACING --discovery-db $DISCOVERY` the tool looks for them under `$NEW`,
where they do not exist, and the check is meaningless.

```
cd $REL
$PY -m tools.ops.preflight_dryrun --data $NEW --ledger $LEDGER --config $CFG --release-dir $REL --workdir /tmp/preflight-$SHA --pacing-db $PACING --discovery-db $DISCOVERY
```

The directories `$OLD` and `$OLD/discovery` are in their sealed shape here (`root:solana-desk 1770`, step 4): preflight accepts exactly that
shape (root-owned, the service group, sticky bit, nothing for others) and still blocks every other group-writable or world-accessible
directory (`LIVE_external_*_dir:MODE_...`).

Stop on any result other than a clean gate, plan and pre-check. The shared pacing database is copied, never opened live, and
is never copied into `$NEW`.

## 7. Render, install and cut over (entry timer OFF, monitor OFF)

### 7a. Render and install the units

Do not fill placeholders by hand. `render-units` fills every `deploy/fresh/` placeholder from the applied manifest (and
refuses any it cannot fill); `render-dropins` writes the per-unit store drop-ins that `cutover` also derives from the manifest
(used here only as a second opinion: diff them against the `60-reviewed-release.conf` that cutover writes).

```
cd $REL
$PY -m tools.ops.fresh_start render-units --root $NEW --out $UNITS --release-dir $REL --state-dir $STATE --archived-root $OLD
$PY -m tools.ops.fresh_start render-dropins --root $NEW --out $UNITS/dropins
```

Archive the old configuration, then install the rendered units. `archive-dropins` MOVES every
`/etc/systemd/system/desk-*.d/` directory (the whole Codex-era stack) and the desk unit files that the rendered set replaces into
`$SDA`, with a sha256 manifest (content, modes, owners). The copy is verified against the manifest before any original is
removed. Nothing is layered on top of the old stack. Units that the fresh set does not replace (for example `desk-discovery.*`,
`desk-recorder.service`) are left in place and stay disabled (step 2). The entry unit MUST come from the rendered set: the stock one
has `ReadWritePaths` on the old root only, `ConditionPathExists` on the old journal and `TimeoutStartSec=180`:

```
cd $REL
$PY -m tools.ops.cutover archive-dropins --name systemd-archive-$SHA --fresh-units $UNITS
$PY -m tools.ops.cutover --apply archive-dropins --name systemd-archive-$SHA --fresh-units $UNITS
install -m 0644 $UNITS/*.service $UNITS/*.timer /etc/systemd/system/
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/desk-*.service /etc/systemd/system/desk-*.timer
```

`systemd-analyze verify` must print nothing for the desk units (warnings about the units you intentionally leave disabled are
not errors; any "Failed to parse"/"Unknown key"/quoting message is a stop). The `desk-backup.service` `ExecStart` carries a
`python -c` program: `%` is written `%%` and `$` as `$$` by the renderer; check it with
`systemctl cat desk-backup.service | grep ExecStart` and `systemd-analyze verify` before cutting over.

The entry-latch drop-in in `$UNITS/desk-paper-entry-dispatcher.service.d/` is installed in step 9, not here.

The optional research units (`desk-counterfactual.*`, `desk-held-watcher.service`, `desk-paper-held-cycle.path`) are rendered
into `$UNITS/research/`, outside the `$UNITS/*.service $UNITS/*.timer` install above, so the default flow never installs, starts
or enables them. Step 11 does that explicitly, later.

### 7a2. Pacing policy for the paid Helius / Jupiter plans (T36; optional, reviewed, append-only)

Helius Developer (documented 50 req/s RPC, 10 req/s DAS/Enhanced) and Jupiter Developer (10 req/s over a 60 s window, shared by
Swap/Price/Token) are paid for, but the shared pacing database still runs both providers at the old 2.0 s cadence (0.5 req/s). The
reviewed entry in `config/provider-pacing-policy.json` sets **Helius 0.1 s (10 req/s, 20% of 50) and Jupiter 0.25 s (4 req/s,
40% of 10)** with the 30 s backoff unchanged; **Kraken stays at exactly 2.0 s** (its row, state and migration receipt are never
touched). Skipping this step is safe: everything keeps running at 2.0 s.

**Where it goes, and why exactly here.** Apply it ONLY after the new release is on every unit and BEFORE the first unit starts:

- after 7a: the old stacks and unit files are archived and the rendered units, whose `WorkingDirectory` and `ExecStart` point at
  `$REL`, are installed; nothing that could still start runs older code;
- before 7b: `cutover` starts discovery, the dashboard and the held-cycle timer, all of which open the shared pacing database, and the
  tool refuses while any writer is active (the same `ActiveState` proof as `backup`, checked again immediately before the write, plus
  "no pending grant and no live waiter" in the database).

Never earlier than 7a, and never "later, while things run": the apply needs every writer stopped, so applying after 7b means stopping
the freshly started stack again.

**Run it as the service user, from `$REL`.** The database is owned by `solana-desk` with mode 0600 (the Kraken migration receipt pins
the path and the mode), and a root-run SQLite writer would leave a root-owned `-journal` next to it that the services cannot open.
`--policy` must be this release's file, `$REL/config/provider-pacing-policy.json`; the tool refuses any other path. The first two
commands change nothing: read `changes` (`helius` 2.0 -> 0.1, `jupiter` 2.0 -> 0.25, no `kraken`) and `ready_to_apply: true`, then
run the third. `--require-quiesced` repeats the units of this stack explicitly (the built-in writer list already names them; listing them here keeps the step self-describing):

```
cd $REL
runuser -u solana-desk -- $PY -m tools.ops.pacing_policy plan --db $PACING --require-quiesced desk-continuous-discovery.service desk-decisions.service desk-paper-monitor.service desk-paper-monitor.timer
runuser -u solana-desk -- $PY -m tools.ops.pacing_policy apply --db $PACING --policy $REL/config/provider-pacing-policy.json --require-quiesced desk-continuous-discovery.service desk-decisions.service desk-paper-monitor.service desk-paper-monitor.timer
runuser -u solana-desk -- $PY -m tools.ops.pacing_policy apply --db $PACING --policy $REL/config/provider-pacing-policy.json --execute --require-quiesced desk-continuous-discovery.service desk-decisions.service desk-paper-monitor.service desk-paper-monitor.timer
```

`--execute` appends one `pacing_policy_changes` row per provider (old and new cadence and backoff, the reason, the sha256 of the
policy file, the digest of the reviewed entry, the time). It never edits the original `policy` rows, and re-running it is a no-op
(`ALREADY_APPLIED`). A 429 or a `Retry-After` still embargoes the provider for the larger of the 30 s backoff and the header.

**The policy file is append-only.** `config/provider-pacing-policy.json` is the reviewed record of cadence decisions. Never edit,
reorder or delete an entry that was applied anywhere: every applied row stores the file's sha256 and the entry's digest, and
validation re-checks them forever. A later change is a NEW entry with a new `id`, shipped in a new release and applied by
repeating this step with all writers stopped. Never `UPDATE policy` by hand and never reset or recreate the shared database.

**One-way door for the old stack.** Once applied, code from a release that predates T36 raises `PACING_DATABASE_INVALID` on the extra
table (it fails closed rather than running at the old cadence; the `verify_*_originals` tools also compare the schema strictly).
Do not start a unit of an older release afterwards, and do not apply this step if you may still fall back to the OLD stack in the
rollback below (that fallback would find a pacing database it cannot open, and the shared database must not be reset).

**Budgets are separate from the cadence.** Helius `getMultipleAccounts` counts as ONE RPC call (up to 100 accounts), so batched marks
are cheap; DAS and Enhanced calls have their own 10 req/s cap and are not plain RPC.

### 7a3. Pacing database schema upgrade (T23F orphan recovery; one-way door)

The production pacing database was created before the append-only `pacing_reclaims` table existed, so the T23F recovery of a pacing
slot orphaned by a killed process is INERT until the table is added (without it a reclaim is never a side effect, it just does not
happen). The upgrade is explicit, idempotent and touches no existing row, counter, cadence or the Kraken lane/receipt; it validates the
whole database first and refuses a tampered or unexpected schema. It goes in the same window as 7a2: after 7a (new release on every
unit), before 7b (the first unit start), with every writer stopped. `cd $REL` first: `-m desk.provider_pacing` must import the NEW
release's code, and it runs as the service user (a root-run SQLite writer would leave a root-owned `-journal` next to the 0600
database that the services cannot open).

```
cd $REL
systemctl is-active desk-paper-entry-dispatcher.timer desk-paper-entry-dispatcher.service desk-paper-held-cycle.timer desk-paper-held-cycle.service desk-paper-held-cycle.path desk-paper-monitor.timer desk-paper-monitor.service desk-decisions.timer desk-decisions.service desk-backup.timer desk-backup.service desk-healthcheck.timer desk-healthcheck.service desk-dashboard.service desk-continuous-discovery.service desk-counterfactual.timer desk-counterfactual.service     # every line must be inactive or failed
runuser -u solana-desk -- $PY -m desk.provider_pacing --upgrade $PACING
```

Output `PACING_UPGRADED` (first time) or `PACING_ALREADY_UPGRADED`; `PACING_UPGRADE_FAILED` (rc 2) changed nothing: stop and read
why (a busy database means a writer is still running).

**One-way door.** Once the table exists, code from a release that predates T23F raises `PACING_DATABASE_INVALID` on the extra table
(it fails closed, and the `verify_*_originals` tools compare the schema strictly). Do not start a unit of an older release afterwards,
and do not run this step if you may still fall back to the OLD stack in the rollback below; the shared database is never reset.
The order against 7a2 does not matter; both are append-only schema/record additions.

### 7b. Dry run, then apply

`--store-env` consumes the manifest directly: interpreter prepended, systemd quoting, `%` escaped as `%%` except a leading
`%d/` credentials path, `$` escaped as `$$`, and the manifest's `unit_sections` (ConditionPathExists reset and re-pointed,
`ReadWritePaths`, `TimeoutStartSec`, environment reset) written into the one `60-reviewed-release.conf` per unit.

Roles: `--units` are started (timers start their services; the oneshot services themselves are only
`--configure-only`, so no pass runs at cutover), `--keep-off` are never started and are disabled if enabled, `--enable` makes
the started units persistent across reboots after they have settled healthy. The entry service is configure-only, its timer
keep-off (entries are step 9). `desk-paper-monitor` is configure-only and keep-off because the stale-mark watchdog sticks the
mode at `EXIT_ONLY` between slow held passes until T23 lands; starting or enabling it needs the explicit `--allow-monitor`.

```
cd $REL
$PY -m tools.ops.cutover cutover --release $REL --store-env $NEW/fresh-start-manifest.json \
   --units desk-continuous-discovery.service desk-dashboard.service desk-paper-held-cycle.timer \
           desk-decisions.timer desk-backup.timer desk-healthcheck.timer desk-notify-daily.timer desk-notify-watchdog.timer \
   --configure-only desk-paper-held-cycle.service desk-decisions.service desk-backup.service \
           desk-healthcheck.service desk-notify-daily.service desk-notify-watchdog.service desk-paper-monitor.service \
           desk-paper-entry-dispatcher.service \
   --keep-off desk-paper-entry-dispatcher.timer desk-paper-monitor.timer \
   --enable desk-continuous-discovery.service desk-dashboard.service desk-paper-held-cycle.timer \
           desk-decisions.timer desk-backup.timer desk-healthcheck.timer desk-notify-daily.timer desk-notify-watchdog.timer \
   --archived-root $OLD --expect-digest <DIGEST from step 1>
$PY -m tools.ops.cutover --apply cutover <same arguments>
```

What `--apply` does, in order:

1. Validate everything before any write: unit names; the release is a canonical direct child of `/opt/solana-desk-releases`
   containing `desk/`; runtime digest; loopback-only ExecStart (unit file, drop-ins, store-env); no unit references an
   archived store root other than the shared pacing/discovery inputs; a held-cycle ExecStart with `--dependency-blocker` is
   refused; every unit in `--units` is `inactive` or `failed`; keep-off units are inactive.
2. Disable every keep-off unit that is enabled and refuse if it is still enabled.
3. Write the drop-ins (marker comment on the first line; files and directory fsynced), `daemon-reload`, then check each service's
   effective `WorkingDirectory`, every `Exec*` line and `Environment` via `systemctl show`.
4. Start only `--units` with `systemctl start --no-block`, recording each unit as started BEFORE its start call, and poll until
   it leaves `activating` (deadline = the unit's `TimeoutStartUSec` + 60 s, so a 600 s entry pass is not cut off by a runner timeout).
5. Verify the new process, not just a name (`ExecMainStartTimestampMonotonic`, `MainPID`, or a scheduled timer trigger).
6. Wait `--settle-seconds` (default 10), re-check health, require `NRestarts` and `MainPID` unchanged.
7. Confirm keep-off units are still inactive and disabled; then `systemctl enable` each `--enable` unit and verify `is-enabled`.

Any failure disables what this run enabled, stops every unit it started (reverse order) and leaves the drop-ins for rollback.
`--replace-existing` backs up an unmanaged `60-reviewed-release.conf` first. The audit trail is
`/var/lib/solana-desk-cutover/cutover-journal.jsonl` (0600, append-only); override with `--journal`.

A `--units` member that is already running makes the tool refuse (a `start` would be a silent no-op). Stop it (step 2) and re-run.
After a reboot the enabled units start by themselves; the entry timer and the monitor timer never do.

### Rollback

Rollback removes every drop-in this flow wrote (`60-reviewed-release.conf`, `70-fresh-store.conf`, `70-entry-latch.conf`),
identified by their marker on the FIRST line; hand-written drop-ins and other `*.conf` files are left alone. It refuses while
any managed unit, or the timer/service partner of one, is active; `--stop` stops them first (timers before services) and
verifies, `--disable` also undoes boot persistence. It never starts anything.

```
cd $REL
$PY -m tools.ops.cutover --apply rollback --stop --disable --restore-archive $SDA --units desk-continuous-discovery.service desk-dashboard.service \
   desk-paper-held-cycle.service desk-decisions.service desk-backup.service desk-healthcheck.service \
   desk-notify-daily.service desk-notify-watchdog.service desk-paper-monitor.service desk-paper-entry-dispatcher.service
```

`--restore-archive $SDA` is validated BEFORE anything is stopped or removed (the archive is re-verified against its manifest;
a hand-written file in a drop-in directory, a rendered unit that was edited after installation, or a tampered archive aborts
the rollback with nothing changed). Then the managed drop-ins are removed, the rendered units this flow installed are removed,
the archived unit files and the whole `desk-*.d` stack are copied back with their recorded modes, and the tree is verified
byte-for-byte against the manifest. Without `--restore-archive` the rendered unit files STAY installed (and the archive stays
in `$SDA`); restore later with the same command. Rollback never restarts anything, and never restores old backups over the new
stores to "roll back code". After a restore the old units are disabled (step 2); re-enable what you intend to run.

If step 7a2 or 7a3 was applied, the OLD stack cannot be started again (its code fails closed on the policy / reclaim table, see 7a2 and 7a3); this path then
ends at "everything stopped". Otherwise, after a rollback that goes back to the OLD stack, also make the old stores writable again (only if step 4 ran, and only after
the new stack is stopped: the rollback above stops it) and BEFORE you start any old unit:

```
cd $REL
$PY -m tools.ops.cutover unseal --manifest $SM
$PY -m tools.ops.cutover --apply unseal --manifest $SM
```

`unseal` restores the exact numeric owner, group and mode of every entry recorded in `$SM` and refuses if a sealed store changed.
Lock files and the shared databases were never touched, so there is nothing to restore for them.

## 8. Verify

```
cd $REL
runuser -u solana-desk -- $PY -m tools.ops.healthcheck --root $NEW --discovery-db $DISCOVERY --pacing-db $PACING --backup-root $BK --config $CFG
ss -ltn 'sport = :8765'            # the dashboard listens on 127.0.0.1 only
```

`desk-notify-watchdog.timer` (every 5 min, own state file) alerts when `health.json` is older than 600 s, so a dead
healthcheck timer is reported within 15 minutes even though the healthcheck itself only notifies from its own `ExecStopPost`.
Watchdog grace: its first check is `OnActiveSec=600`, counted from the moment the timer is started at cutover (or activated at
boot), so it cannot run before the first healthcheck wrote `health.json` and raise a false CRITICAL during the deployment.

Right after cutover expect exit 2 with exactly two CRITICAL checks, both legitimate: `discovery_frames` (no listener frame has
completed yet) and `backup` (none exists until `desk-backup.timer` fires; `systemctl start desk-backup.service` clears it). It must
clear `discovery_frames` within minutes; any other CRITICAL is a stop. `mode`, `monitoring_budget` and `pacing:*` must be OK. Also check every `desk-*` unit's effective
`WorkingDirectory` (`systemctl show -p WorkingDirectory`), discovery rows are fresh and the mode is `RUNNING`. With the
read-only status tool (T02/T02F/T34) run the block below as the service user and expect empty `blockers`. The shared pacing and
discovery databases are outside `$NEW`, so `--pacing-db $PACING --discovery-db $DISCOVERY` are required: without them the report
reads (and blames) `$NEW/provider-pacing.sqlite`, which does not exist. Then take the pre-entry snapshot (T06/T34) with the same
two flags:

```
cd $REL
runuser -u solana-desk -- $PY -m tools.ops.status --data $NEW --ledger $LEDGER --config $CFG --release-dir $REL --pacing-db $PACING --discovery-db $DISCOVERY --systemd
runuser -u solana-desk -- $PY -m tools.ops.verify_cycle snapshot --data $NEW --ledger $LEDGER --config $CFG --pacing-db $PACING --discovery-db $DISCOVERY --out /var/lib/solana-desk-health/snap-before.json
```

## 9. Latch, then enable entries (T07)

Install the latch drop-in BEFORE enabling the entry timer, so new admissions stop after the first persisted BUY while held
monitoring continues to an exit. It was rendered with the right paths in step 7a (first line is its marker, so `rollback`
removes it):

```
install -D -m 0644 $UNITS/desk-paper-entry-dispatcher.service.d/70-entry-latch.conf /etc/systemd/system/desk-paper-entry-dispatcher.service.d/70-entry-latch.conf
systemctl daemon-reload
cd $REL
runuser -u solana-desk -- $PY -m tools.ops.entry_latch --config $CFG --ledger $LEDGER
```

The last command is read-only (no BUY yet, nothing to do). Enable entries by the coordinator's explicit decision only:

```
systemctl enable --now desk-paper-entry-dispatcher.timer
```

## 10. Cold restart check (T06/T34)

After a position has been opened or closed and with no new activity expected:

```
cd $REL
systemctl restart desk-paper-held-cycle.timer desk-dashboard.service
runuser -u solana-desk -- $PY -m tools.ops.verify_cycle snapshot --data $NEW --ledger $LEDGER --config $CFG --pacing-db $PACING --discovery-db $DISCOVERY --out /var/lib/solana-desk-health/snap-after.json
runuser -u solana-desk -- $PY -m tools.ops.verify_cycle compare /var/lib/solana-desk-health/snap-before.json /var/lib/solana-desk-health/snap-after.json            # exact
runuser -u solana-desk -- $PY -m tools.ops.verify_cycle compare /var/lib/solana-desk-health/snap-before.json /var/lib/solana-desk-health/snap-after.json --allow-progress
```

Any differing fill, checkpoint or budget, or a duplicate fill/charge, is a failure. A cycle is verified only when `snapshot`
reports `VALIDATED_LIVE_DATA_PAPER_ROUND_TRIP` and the compare passes.

## 11. Optional research units (counterfactual tracking, event-driven held watcher)

Not part of the first verified cycle: nothing above installs, starts or enables them. Do this only after step 9, as a deliberate,
separate decision. Their write scope is small and explicit: `desk-counterfactual.service` writes only `$NEW/counterfactual`
and the shared pacing database DIRECTORY (`$OLD`, because SQLite creates the rollback journal beside the shared database; the
experiment root and the discovery directory are made read-only again inside it); `desk-held-watcher.service` writes only
`$STATE/held-watcher` and deliberately does not use the shared pacing store (see `docs/ops/HELD_WATCHER.md`).

```
cd $REL
install -d -m 0700 -o solana-desk -g solana-desk $NEW/counterfactual
runuser -u solana-desk -- $PY -m tools.research.counterfactual init --store $NEW/counterfactual/counterfactual.sqlite --allowance-per-hour 300
install -d -m 0700 -o solana-desk -g solana-desk $STATE/held-watcher
install -m 0644 $UNITS/research/desk-counterfactual.service $UNITS/research/desk-counterfactual.timer $UNITS/research/desk-held-watcher.service $UNITS/research/desk-paper-held-cycle.path /etc/systemd/system/
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/desk-counterfactual.service /etc/systemd/system/desk-counterfactual.timer /etc/systemd/system/desk-held-watcher.service /etc/systemd/system/desk-paper-held-cycle.path
systemctl enable --now desk-counterfactual.timer
```

Start the held watcher and its path unit only together with the entry timer (a watcher without a position has nothing to watch):

```
systemctl enable --now desk-paper-held-cycle.path
systemctl enable --now desk-held-watcher.service
```

To undo: `systemctl disable --now desk-held-watcher.service desk-paper-held-cycle.path desk-counterfactual.timer`, then remove
the four installed files from `/etc/systemd/system/` and `daemon-reload`. `cutover rollback` does not know about these units
(they are not in the archive manifest), so undo them first. The stores under `$NEW/counterfactual` and `$STATE/held-watcher`
are append-only and are kept.

## Rotate when flat

**When to rotate on capacity.** The retired-rejection table (`paper_cycle_no_entry`) holds at most 8192 rows. At 80% (6553 rows)
every new publication logs `rotation_warning` and appends a `rotation_warning_v1` line to `<evidence db>.publish-refused.jsonl`;
read it with `desk.paper_cycle_no_entry.read_refusals(<evidence db>)` (the healthcheck should alert on any line of that kind).
Rotate while flat when it appears. At 100% only the NEXT no-entry publication is refused (`publish_refused_v1`, reason
`RotationRequired`): that pass is closed `FAILED_CHARGED` instead of being left NULL, every retained row still replays, and no
position or pass is latched, so the store stays flat-rotatable. Every other refused publication (`publish_refused_v1`, with the
exception class and a bounded detail) is recorded in the same file, so a silent refusal cannot hide.

Code under `desk/` changed (or a new experiment version starts) and the ledger holds no position: do not pin a successor. Roll
to a new store set. The previous fresh root becomes an archived root too.

1. `cd $REL && runuser -u solana-desk -- $PY -m tools.ops.healthcheck ...` and the status tool: no held position, no unresolved exit; mode `RUNNING` or `ENTRY_PAUSED`.
2. DISABLE the entry timer first, so a reboot in the rotation window cannot start entries (this is what makes the "fails closed"
   claim below true), then stop EVERYTHING that reads or writes the current root, timers first, including the dashboard and
   the health/notify/watchdog timers:
   ```
   systemctl disable desk-paper-entry-dispatcher.timer
   systemctl stop desk-paper-entry-dispatcher.timer desk-paper-held-cycle.timer desk-paper-monitor.timer desk-decisions.timer \
     desk-backup.timer desk-healthcheck.timer desk-notify-daily.timer desk-notify-watchdog.timer
   systemctl stop desk-paper-entry-dispatcher.service desk-paper-held-cycle.service desk-paper-monitor.service desk-decisions.service \
     desk-backup.service desk-healthcheck.service desk-notify-daily.service desk-notify-watchdog.service desk-dashboard.service desk-continuous-discovery.service
   ```
   The stopped units stay enabled; until the new cutover re-points their drop-ins they still name the old root, whose stores are
   about to be read-only, so a reboot in this window fails closed (nothing can write) rather than trading.
3. Backup the current root (step 3 with `--data $NEW` and its own `--expect-count`, destination `/var/backups/solana-desk/pre-rotate-<next version>`)
   and seal its stores. The seal uses a NEW manifest path (an existing one is refused, so never reuse `$SM`) and NO `--shared-path`: the
   shared inputs stay in `$OLD` and are not part of `$NEW`, so no directory of `$NEW` is made group-writable (`$NEW` becomes `0550`, not `1770`):
   ```
   $PY -m tools.ops.cutover seal-archive --root $NEW --manifest /var/backups/solana-desk/seal-manifest-rotate-<next version>.json
   $PY -m tools.ops.cutover --apply seal-archive --root $NEW --manifest /var/backups/solana-desk/seal-manifest-rotate-<next version>.json
   ```
4. Stage the new release (step 1), then from the NEW release directory run `fresh_start rotate` (dry run first; it refuses an
   open position and leaves `--from` untouched; `<hash>` is the `plan_hash` of the dry run):
   ```
   $PY -m tools.ops.fresh_start rotate --from $NEW --to $FRESH/<next-version> --config <new config> --pacing-db $PACING --discovery-db $DISCOVERY --enable-held --service-user solana-desk --chown solana-desk --execute --approved-plan-hash <hash>
   ```
5. Render and install units for the new root (step 7a), then cut over (step 7b) with BOTH archived roots:
   `--archived-root $OLD --archived-root $NEW`, `--store-env <new root>/fresh-start-manifest.json`, the entry timer still off. Verify
   (step 8), install the latch from the new render (step 9), then enable entries.

## If a position is open

Do not rotate and do not change `desk/`: the existing successor machinery applies and that is a separate coordinator
procedure. Held monitoring must keep running; do not stop the held cycle or the monitor to deploy anything else.

## Things that are deliberately not automated

- Stopping writers, sealing the old stores, installing unit files and enabling the entry timer are explicit commands above.
- `cutover` never edits `deploy/*.service` templates, never touches stores, and never calls a provider.
- Dashboard exposure: any ExecStart (override, base unit or the live effective value) that binds off loopback aborts the cutover.
- Writing to the archived stores: any unit that references an archived root (other than the shared pacing and discovery inputs)
  aborts the cutover before anything is written; `--allow-archived <unit>` is an explicit, per-unit exception.


### Reports directory (before enabling the daily report timer)

```bash
install -d -m 0700 -o solana-desk -g solana-desk "$STATE/reports"
```

Create it before `systemctl enable --now desk-daily-report.timer` (T41/T42 write `<STATE_DIR>/reports/YYYY-MM-DD.html` there).

Note: the VPS has `fs.protected_regular = 2`. Once `$OLD` and `$OLD/discovery` are root-owned 1770, root must never open an existing solana-desk-owned file there with O_CREAT; run such tools via `runuser -u solana-desk`.
