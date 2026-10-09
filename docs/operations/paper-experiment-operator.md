# Supervised paper experiment operator handoff

This runbook runs the existing **SYNTHETIC_TEST_ONLY** CLI, not live admission.
The user heartbeat stays paused. No timer, automatic loop, live service, signing
or broadcasting is created. Only the coordinator installs/releases the reviewed
source; these instructions do not attest a VPS deployment or live readiness.

## Runtime source and files

The command verification uses this exact local combination, in listed order:

| Source | Exact head |
| --- | --- |
| Accepted integration | `c0347d836560b365550d077c4f600ac3d140d4b5` |
| PR108 entry checkpoint binding | `90d8c59d1e98c3d09074a6f2fa4a029dbd420862` |
| PR113 performance report repair, including PR105 | `f89029a2669b4a2c4b891c4855a20c821a78cad5` |
| PR115 saved runner visibility repair | `418aa4e7a77b6e9ba43bd82fd9bb7374f62afa6b` |
| PR116 explicit experimental fixture profiles | `d9e4ed6b811b47376ac6ad604847edfcf94aa052` |
| PR117 bounded read sources | `cfb25987e9c5a2b8cddb769da9047ed7262081f5` |

Combined runtime tree: `8b034015d67b21209a76800c470a88878e233861`.
The full `git ls-tree -r` manifest SHA256 is
`558f462787870f986615083bf5db7877f0f9236d5344c1dce56f7b388e0e7491`.
These identities pin tested source, not acceptance of pending components. The
coordinator must release the reviewed combination before using its commands.

Combination limitation: the full 2,075-test suite has three errors in PR116's
experimental-profile tests. PR115's saved-event projection calls strict
`validate_event` without the pinned experimental config, so experimental unknown
fields redact the paper projection (`RUNNER_EVENT_INVALID`). This handoff uses
only the tested default `config/paper.json`; it does not certify either
experimental profile or the entire pending combination for release. The owning
authors/coordinator must resolve that interaction and rerun their required gates.

Retain the complete source checkout at `/opt/solana-desk`, its Python 3.12
`.venv`, `desk/schemas`, `config/paper.json`, and
`fixtures/paper_runner/lifecycle.json`. Repository setup is
`python -m pip install -e . -r requirements-live.txt`; dependency installation is
not a provider call. Config and fixtures are separate checkout files, not wheel
package data. The existing `solana-desk` account needs read access to that frozen
release and ownership/write access to the experiment parent directory.
No credential file or provider environment is required for this synthetic path.

Do not change code or config after initialization: Ledger binds their hashes.
A code/config mismatch, corrupt or partial checkpoint requires diagnosis against
preserved originals, not removal of metadata, automatic migration or adoption.

## Create a NEW isolated experiment

Run as the `solana-desk` account, with the coordinator-prepared parent writable:

```bash
set -eu
umask 077
APP=/opt/solana-desk
PY="$APP/.venv/bin/python"
CFG="$APP/config/paper.json"
FIXTURE="$APP/fixtures/paper_runner/lifecycle.json"
EXP=/var/lib/solana-desk/experiments/synthetic-operator
DB="$EXP/active-paper.sqlite"

test ! -e "$EXP"
mkdir -m 700 "$EXP"
"$PY" -m desk.paper_runner --config "$CFG" init --db "$DB" > "$EXP/init.json"
```

`init` uses exclusive new-file creation. Never point it at production
`/var/lib/solana-desk/active-paper.sqlite`, research/evidence/decision databases,
a backup, or an existing experiment. A repeat initialization must fail; do not
work around it. For another experiment, choose a new directory and, if using the
optional unit, a separately reviewed unit path. Retain originals and SQLite
sidecars; do not copy a running database with plain `cp`.

## Verify entry, refresh, exit and restart

Continue in the same shell. These are the fixture's original **simulated** clock
timestamps; do not replace them with wall-clock time or rewrite observations:

```bash
for TS in 1791417600 1791417601 1791417602; do
  "$PY" -m desk.paper_runner --config "$CFG" once --db "$DB" \
    --fixture "$FIXTURE" --now "$TS" --limit 16 > "$EXP/pass-$TS.json"
done

"$PY" -m desk.paper_runner --config "$CFG" once --db "$DB" \
  --fixture "$FIXTURE" --now 1791417602 --limit 16 > "$EXP/restart.json"
"$PY" -m desk.experiment_report "$DB" --now 1791417602 > "$EXP/report.json"
```

Every pass must exit zero with `status: COMPLETE`. Expected persisted results:

| Pass | Open positions | Cash SOL | New fill outcomes |
| --- | ---: | --- | ---: |
| Initialization | 0 | `5` | 0 |
| Entry, 1791417600 | 1 | `4.916616667` | 1 |
| Refresh, 1791417601 | 1 | unchanged | 0 |
| Exit, 1791417602 | 0 | `4.996888045856944320960511715` | 1 |
| Same-clock restart | 0 | unchanged | 0 |

The report must show one closed trade, zero partial sells and zero open inventory;
net realized PnL is approximately `-0.003111954143055679` SOL. Fees are already
included in reconstructed entry cost/net proceeds; do not subtract the separate
recorded fee total again. PnL consistency is not authenticated execution or a
profitability PASS. A report exits nonzero on corrupt/partial accounting.

Verify the saved command outputs without changing the database:

```bash
"$PY" - "$EXP" <<'PY'
import json, sys
from decimal import Decimal as D
from pathlib import Path
root = Path(sys.argv[1])
read = lambda name: json.loads((root / name).read_text())
assert read('init.json')['status'] == 'LEDGER_PRESENT'
for ts, count in ((1791417600, 1), (1791417601, 1), (1791417602, 0)):
    result = read(f'pass-{ts}.json')
    assert result['status'] == 'COMPLETE'
    assert result['provenance'] == 'SYNTHETIC_TEST_ONLY'
    assert result['automatic_entry_enabled'] is False
    assert len(result['paper']['positions']) == count
assert read('restart.json')['outcomes'] == []
report = read('report.json')
assert report['closed_trade_count'] == 1
assert report['partial_sell_fill_count'] == report['open_position_count'] == 0
cash = D(read('restart.json')['paper']['cash_sol'])
assert abs(cash - D('5') - D(report['net_realized_pnl_sol'])) <= D('1e-20')
assert report['profitability_verdict'] == 'NOT_ASSESSED'
print('Synthetic lifecycle/restart accounting verified; no live acceptance')
PY
```

A pass is bounded to 16 control/observation/candidate events. Missing position
observations, a clock behind the ledger or other non-COMPLETE status requires
inspection, not a fabricated price or an entry retry using new IDs. Earlier
commits survive interruption; use the same source/config/fixture IDs on restart.

## Private-loopback status

In a second service-account shell, start the existing dashboard against the
**experiment's** directory, on a separate unused port:

```bash
/opt/solana-desk/.venv/bin/python -m desk serve \
  --db /var/lib/solana-desk/experiments/synthetic-operator/research.sqlite \
  --port 8766
```

This creates separate research/evidence files in that new directory; it does not
reuse production databases. `serve` binds `127.0.0.1`, never an external address.
It also starts its existing research job worker: keep this experiment research
DB new/empty and make only the GET below; do not submit scan jobs. No live
collection, forwarding or provider setup is part of this handoff.

```bash
curl --fail --silent --show-error http://127.0.0.1:8766/api/paper
```

Expected: `LEDGER_PRESENT`, `SYNTHETIC_CHECKPOINT_RECORDED`, explicit
`SYNTHETIC_TEST_ONLY`, `runner_liveness: UNKNOWN`,
`automatic_entry_enabled: false`, saved cash, positions and recent outcomes.
Market age is based on real wall time here, so the old fixture is stale; a saved
row, healthy API or completed oneshot never proves a process is currently alive.
Stop this dashboard with Ctrl-C in its foreground terminal. Restart with the
same `serve` command; GET status/report must preserve the paper checkpoint.
Do not stop or restart the production dashboard, monitor or heartbeat.

## Stop, controls and optional Linux oneshot

There is no continuously running paper loop to stop. Ctrl-C interrupts a manual
pass; committed events remain. Paper controls are persistent strategy controls,
not operating-system process controls. On a separate fresh experiment or after
the above completed lifecycle, using a nondecreasing simulated time:

```bash
"$PY" -m desk.paper_runner --config "$CFG" once --db "$DB" \
  --fixture "$FIXTURE" --now 1791417603 --control PAUSE_ENTRY
"$PY" -m desk.paper_runner --config "$CFG" once --db "$DB" \
  --fixture "$FIXTURE" --now 1791417604 --control EXIT_ONLY
```

`PAUSE_ENTRY` does not stop position monitoring; `EXIT_ONLY` blocks new entries.
`LIQUIDATE` requests proof-gated exits, not guaranteed sales. `RESUME` changes only
this experiment's strategy mode and is an explicit operator choice; it does not
resume the user heartbeat or grant live permission. Never add `RESUME` to a
timer. A control with missing open-position observations can persist while the
pass reports unavailable; inspect both the JSON and saved checkpoint.

`deploy/desk-synthetic-experiment.service` is an optional **manual** oneshot for
this exact isolated directory. It has no `[Install]`, timer or restart policy,
no credential load, private networking, and writes only inside the experiment.
The coordinator may install it after review; this handoff does not install it.
Before each manual start, the experiment owner writes the intended simulated
clock. This example is a bounded empty pass after the lifecycle and optional
controls above; never choose a time behind the saved ledger:

```bash
printf 'PAPER_SYNTHETIC_NOW=1791417604\n' > "$EXP/runner.env"
chmod 600 "$EXP/runner.env"
```

Once the coordinator has installed the unit, its existing systemd commands are:

```bash
sudo systemctl start desk-synthetic-experiment.service
sudo systemctl status desk-synthetic-experiment.service
sudo journalctl -u desk-synthetic-experiment.service -n 30 --no-pager
sudo systemctl stop desk-synthetic-experiment.service
sudo systemctl restart desk-synthetic-experiment.service
```

`start`/`restart` runs one bounded pass, not a loop. Update the simulated clock for
the next fixture pass; reuse an identical clock for idempotent retry. A successful
oneshot becomes inactive, not continuously healthy. A failed condition can skip
execution: verify its journal and paper GET/report, not just a systemctl result.
`stop` cannot undo committed trades; restart the same pass to reconcile after
interruption. Never `enable` this unit, add a timer or alter production units.
The monitor service in the repo only expires stale marks; it cannot obtain new
prices or sell inventory and is not required for this fixed-clock verification.
To use the unit instead of the direct lifecycle CLI, initialize a fresh experiment
first, then select 1791417600, 1791417601 and 1791417602 one manual start at a time.
Do not replay these earlier times after the example controls at 1791417603/4.

## The future live command DOES NOT exist

There is no `desk.paper_runner live`, `--live`, live fixture mode or paper-loop
live service. `run_once` still accepts only `FixtureAdapter` and synthetic
provenance. `desk serve`, `consume-scans` and `paper-monitor` are not live entry
commands. Generic `replay` is not a trusted live adapter.

PR117's `PaperReadSources` provides charged bounded reads; accepted collection,
observation parsing and scoring components remain inputs, not a runnable fill
loop. Live activation still needs the separately owned trusted current-feature
and admission adapter, position refresh/full-valuation and exact action-quantity
exit proof adapter, and reviewed runner orchestration with durable IDs/budget
and original timestamp preservation. `LIVE_FEATURE_ADAPTER_NOT_READY` and all
source/control/route/quantity gates remain. A `LIVE_PAPER` database marker or
experimental profile cannot manufacture these contracts. This runbook does not
duplicate transport/collector/features work or decide the pending fill model.
