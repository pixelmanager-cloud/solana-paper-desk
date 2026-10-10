# Held-position watcher (T28)

`python -m tools.ops.held_watcher` shortens the reaction time of the held cycle. It does **not** exit positions and it
does **not** decide anything. It watches the pool vaults of every open paper position and, when the implied mark gets
close to an engine exit threshold, asks systemd to run the normal held pass now instead of at the next timer tick.
The held pass fetches the executable quote and the engine decides, exactly as before.

Paper only. EXECUTION_UNVERIFIED applies unchanged: the watcher's price is an estimate and is never used for a fill.

## What it reads and writes

| | |
|---|---|
| Ledger (`paper-ledger.sqlite`) | **read-only** (`mode=ro`; `immutable=1` only for a WAL file with no `-wal` sidecar, the T02F rule, which is what makes it work under `ReadOnlyPaths=`). Never written. |
| Config | frozen experiment config, read with `desk.model.load_config` |
| Own store `<state>/watcher.sqlite` | append-only (triggers reject UPDATE/DELETE): `requests` + `request_results` (its allowance), `vaults` (resolved pool vaults), `triggers`, `health` |
| `<state>/health.json` | current status for operators / future healthcheck (0600, atomic) |
| `<state>/trigger/request` | atomically replaced file; a systemd `.path` unit watches it |
| Provider | Helius only: websocket `accountSubscribe`, HTTP `getAccountInfo` (once per new pool) and `getMultipleAccounts` (poll / reconcile). Key from `$CREDENTIALS_DIRECTORY/provider-keys.json`, never logged; errors are classes (`HTTP_429`, `TIMEOUT`, ...), never provider text. |

It does **not** use the shared `provider-pacing.sqlite`. A process killed while holding a pacing ticket leaves it pending
and blocks every provider call of the desk (T09 F6); a fast-reacting side process must not be able to do that. It keeps
its own allowance (default 3600 requests per rolling hour, charged *before* each call, failures included) and a minimum
spacing between requests (default 1 s, enforced from the persisted request log, so it survives restarts). Budget math:
polling alone at the 2 s fallback cadence is 1800 requests/hour; streaming with the 30 s reconcile poll is 120/hour; the
engine's monitoring allowance (3600/hour) is not touched. It never consumes the entry or monitoring budgets.

## When it nudges

For each open position it computes `ratio = (constant-product sell of qty - slippage haircut - fixed fee) / cost_left`
from the two vault balances (same formula as `desk.strategy.swap_quote`), using thresholds from the frozen config and the
persisted position:

| Reason | Condition (position fields from the ledger) |
|---|---|
| `STOP` | `ratio <= stop_ratio + margin` |
| `TRAILING_STOP` | `stage >= 3` and `ratio <= max(peak_ratio, watcher's observed peak, ratio) * (1 - trailing_fraction) + margin` |
| `TAKE_PROFIT` | `stage < 3` and `ratio >= {1.4, 2, 3}[stage] - margin` |
| `MAX_HOLD` | `now - opened_at >= max_hold_seconds` (no reserves needed) |
| `TIME_STOP` | `not touched_15` and `now - opened_at >= time_stop_seconds` (no reserves needed) |
| `LIQUIDATE` | ledger mode is `LIQUIDATING` |

`margin` defaults to 0.02 (ratio units): the nudge goes out slightly early, because the held pass takes seconds. The
ladder, the 1.15 "touched" level and the trailing stage are duplicated from `desk/engine.py` (they are local variables
there); a test parses the engine source and fails if they drift. The engine's `DANGER` reason depends on evidence the
watcher cannot see, so it is not covered: the regular timer remains the safety net for that.

Reserves are used only while fresh: a polled snapshot older than 20 s (polling mode) is ignored (and `RESERVES_STALE` is
reported); while the websocket is up a quiet pool's snapshot stays valid as long as the reconcile poll keeps refreshing
it. Time-based reasons never need reserves.

## Not a trading loop: limits on how often it nudges

* Debounce per (position, reason): 30 s, persisted, so a restart does not re-fire immediately.
* At most 12 requests per position per hour and 60 per hour overall; further ones are suppressed and reported
  (`TRIGGER_RATE_LIMITED`).
* At least 5 s between ANY two requests: one held pass handles every position, a second request inside that window is
  redundant. A suppressed request is not remembered, so a position that is still crossing fires after the gap.
* A failed request is retried after min(debounce, 5 s) and reported as `TRIGGER_FAILED`.

## Starting the held pass without sudo or polkit

Default `--trigger path`: the watcher atomically replaces `<state>/trigger/request`; `desk-paper-held-cycle.path`
(`PathChanged=`) makes systemd start `desk-paper-held-cycle.service`. The watcher needs no privilege at all. The held
service keeps its own scheduler lease: a request that arrives while a pass is running is a no-op, and a lost race exits
0 (`SCHEDULER_BUSY`) like any timer tick. `--trigger systemctl` runs `systemctl start --no-block <unit>` and needs
privileges (root, or a polkit rule you write yourself); `--trigger none` / `--dry-run` only records.

The path unit deliberately has no `TriggerLimit*`: reaching a path unit's trigger limit stops the unit and would silently
disable fast exits. The watcher's own limits above are the guard.

### Install (coordinator)

1. Copy `deploy/fresh/desk-held-watcher.service` and `deploy/fresh/desk-paper-held-cycle.path`, substituting
   `<FRESH_ROOT>`, `<RELEASE_DIR>`, `<CONFIG>`, `<STATE_DIR>` (the same values as the other fresh units; `<STATE_DIR>` is the
   0700 `solana-desk` directory used by the healthcheck, the watcher creates `held-watcher/` inside it).
2. Cut over with `tools.ops.cutover` like the other services (the unit has no store-env needs; its paths are in the
   template). It is a `Type=simple` service with `Restart=on-failure`; a cutover health check treats it like the dashboard.
3. Enable the path unit and the watcher **after** the entry timer is enabled (there is nothing to watch before the first
   position): `systemctl enable --now desk-paper-held-cycle.path desk-held-watcher.service`.
4. First run with `--dry-run` (edit `--trigger none` into the ExecStart, or run by hand with `--once --dry-run`) and
   look at `health.json` and the `triggers` table before letting it fire.

Hand check, no unit: `python -m tools.ops.held_watcher run --ledger L --config C --state-dir D --once --dry-run`
(needs `CREDENTIALS_DIRECTORY` with `provider-keys.json` for the real RPC; mode 0400/0440/0600).

## Health and failure behaviour

`health.json`: `status` is `IDLE` (no positions), `STREAM`, `POLLING` (stream down or disabled), or `DEGRADED` (polls
failing or allowance exhausted). `warnings` holds codes such as `STREAM_DOWN`, `POLL_FAILED`, `ALLOWANCE_EXHAUSTED`,
`RESERVES_STALE`, `VAULT_RESOLUTION_FAILED`, `LEDGER_UNREADABLE`, `TRIGGER_FAILED`, `TRIGGER_RATE_LIMITED`. Transitions are
also appended to the `health` table.

* Lost stream: reconnect with exponential backoff (2, 4, 8 ... capped at 60 s, jittered); polling every `--poll-seconds`
  (default 2 s) takes over at once. On reconnect the watcher seeds the reserves with one poll, because
  `accountSubscribe` sends no initial value.
* Allowance exhausted: no further provider requests until the rolling window frees; price-based nudges pause, time-based
  ones still fire; status `DEGRADED`. The timer-driven held pass is unaffected.
* Watcher crash/kill: nothing is left behind (no pacing ticket, no ledger lock); its request log keeps the allowance honest
  across restarts; systemd restarts it.
* A vault whose mint, owner or layout is not exactly what the verified pool says is ignored and reported
  (`VAULT_DECODE_FAILED`), never trusted.

Healthcheck integration (T21's `tools.ops.healthcheck`) is **not** wired in this task: it would need a check for a stale
`health.json` and for `DEGRADED`/`STREAM_DOWN`. See the report.

## Measuring the benefit

`python -m tools.ops.held_watcher latency --ledger L --state-dir D` joins the `triggers` table with the ledger's SELL fills
(read-only) and prints, per trigger, `watch_to_trigger_seconds` (our own processing), `trigger_to_fill_seconds`, the fill
reason, and the median/p90 summary. Ledger times are whole seconds (the quote time of the held pass) and the wall-clock time
of an on-chain slot is not recorded (`slot` is stored, `slot_wall_time: NOT_RECORDED`), so "event slot time -> trigger" is
not reported; a later task could resolve slot times with `getBlockTime`. Use it for T14-style analysis; it says nothing
about execution quality, only about how much sooner the held pass ran.

## Limits

* The mark is a constant-product estimate. The held pass may find a different executable quote (or none) and then not exit.
* PumpSwap pools only (the pool account is decoded and its PDA verified; anything else is not watched and is reported).
* Vault resolution costs one `getAccountInfo` per new pool; a position whose pool cannot be verified is not watched (timer only).
* It speeds up reaction by seconds; it cannot make a thin pool sellable.
