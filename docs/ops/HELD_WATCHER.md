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
| Own store `<state>/watcher.sqlite` | append-only (triggers reject UPDATE/DELETE): `requests` + `request_results` (its allowance), `vaults` (resolved pool vaults and the pool/mint facts used for the mark), `triggers` (with their class), `trigger_slot_times` (block time of the observed slot), `health` |
| `<state>/health.json` | current status for operators / future healthcheck (0600, atomic) |
| `<state>/trigger/request` | atomically replaced file; a systemd `.path` unit watches it |
| Provider | Helius only: websocket `accountSubscribe`, HTTP `getAccountInfo` (pool and mint, once per new pool), `getMultipleAccounts` (poll / reconcile / settle) and `getBlockTime` (latency bookkeeping). Key from `$CREDENTIALS_DIRECTORY/provider-keys.json`, never logged; errors are classes (`HTTP_429`, `TIMEOUT`, ...), never provider text. |

**Write scope.** The service may write only `<STATE_DIR>/held-watcher` (`ReadWritePaths=` names exactly that directory, not
the shared state directory the healthcheck uses). Everything else, including the ledger, is read-only to it.

### Why it does not use the shared pacing store, and why it cannot starve the desk

`provider-pacing.sqlite` is a single-writer ticket queue: a process killed while holding a ticket leaves it pending and
blocks every provider call of the desk (T09 F6). A side process whose whole point is to react fast must not be able to do
that, so the watcher has its own allowance and spacing, persisted in its own request log. Because it therefore does not
queue behind the desk, the guard against crowding out the desk's Helius calls is its own rate ceiling and backoff:

* **at most 0.5 requests/second by default** (`--min-request-interval 2.0`, enforced from the persisted log so it survives
  restarts) and 1800 requests per rolling hour (`--allowance-per-hour`, charged before each call, failures included);
* **provider backoff:** after HTTP 429/5xx, a timeout or a network error ALL watcher requests are held, with exponential
  jittered backoff (2 s, 4 s, 8 s ... capped by `--http-backoff-cap`, default 300 s) that resets on the first success;
  nothing is sent or charged while held (`PROVIDER_BACKOFF`, status `DEGRADED`);
* streaming needs about 120 requests/hour (the 30 s reconcile poll); polling alone at the 2 s fallback is the 0.5 req/s
  ceiling. It never consumes the entry or monitoring budgets.

## When it nudges

For each open position it computes `ratio = (constant-product sell of qty - slippage haircut - fixed fee) / cost_left`
from the two vault balances (same formula as `desk.strategy.swap_quote`), using thresholds from the frozen config and the
persisted position:

| Reason | Condition (position fields from the ledger) |
|---|---|
| `STOP` | `ratio <= stop_ratio + margin` |
| `TRAILING_STOP` | `stage >= 3` and `ratio <= peak * (1 - trailing_fraction) + margin`, where `peak = max(recorded peak_ratio, ratio)`; the watcher's own higher peak counts only up to `recorded + margin` (an early hint), because the engine trails its RECORDED peak |
| `TAKE_PROFIT` | `stage < 3` and `ratio >= {1.4, 2, 3}[stage] - margin` |
| `MAX_HOLD` | `now - opened_at >= max_hold_seconds` (no reserves needed) |
| `TIME_STOP` | `not touched_15` and `now - opened_at >= time_stop_seconds` (no reserves needed) |
| `LIQUIDATE` | ledger mode is `LIQUIDATING` |

`margin` defaults to 0.02 (ratio units): the nudge goes out slightly early, because the held pass takes seconds. The
ladder, the 1.15 "touched" level and the trailing stage are duplicated from `desk/engine.py` (they are local variables
there); a test parses the engine source and fails if they drift. The engine's `DANGER` reason depends on evidence the
watcher cannot see, so it is not covered: the regular timer remains the safety net for that.

**Same-slot reserves only.** A swap changes both vaults in one slot, but their websocket notifications arrive separately; pairing a
new balance of one vault with an old balance of the other fakes a price spike (a liquidity add that doubles both reserves looks
like a 50% crash for a moment). The watcher therefore buffers balances per slot and prices a position only from a COMPLETE
pair of one slot. Polling uses a single `getMultipleAccounts` call that always carries both vaults of a pool (one slot). A lone
update waits `--settle-seconds` (1 s) for its partner and is then completed by one same-slot poll.

Reserves are used only while fresh: a polled snapshot older than 20 s (polling mode) is ignored (and `RESERVES_STALE` is
reported); while the websocket is up a quiet pool's snapshot stays valid as long as the reconcile poll keeps refreshing
it. Time-based reasons never need reserves.

### The mark: fees and remaining assumptions

`ratio = (constant-product sell of qty after fees, minus slippage haircut and fixed fee) / cost_left`. Included where known:
the operator's pool fee (`--pool-fee-bps`, the same hypothesis the held unit uses), the PumpSwap creator fee (`--creator-fee-bps`
plus the pool account's own `creator_fee_bps` override), a Token-2022 `TransferFeeConfig` on the mint (its NEWER schedule; the
per-transfer maximum is ignored, the conservative direction) and virtual quote reserves of boosted pools. Pool and mint facts
are read once per new pool and cached in the store. Remaining assumptions: the global PumpSwap fee tier is not read; the
transfer-fee epoch is not checked; the real executable quote can still differ, so the nudge is early by `margin` and the held
pass stays authoritative. A position without `quote_execution` (decimals) is priced from the mint account and reported as
`POSITION_NO_QUOTE_EXECUTION`; if even the mint cannot be read the position is reported as `POSITION_NO_DECIMALS`
(`DEGRADED`). Price triggers are never dropped silently.

## Not a trading loop: limits on how often it nudges

Reasons fall into two classes with SEPARATE budgets, so a reason that cannot resolve can never starve a real crash:

* **price** (`STOP`, `TRAILING_STOP`, `TAKE_PROFIT`): debounce 30 s per (position, reason), at most 12 per position and 60 per
  hour overall, and **a price exit may always fire once per `--price-guarantee-seconds` (60 s) whatever the caps say**.
* **time / mode** (`MAX_HOLD`, `TIME_STOP`, `LIQUIDATE`): at most 6 per position and 30 per hour overall, counted separately.
  If the ledger shows a held pass ran for the position AFTER our last request and the position is still open and the reason
  still holds, the pass could not act on it: asking again at the same pace changes nothing, so the debounce doubles each time
  (30, 60, 120 ... capped at 1 h).

Both classes share: at least 5 s between ANY two requests (one held pass handles every position; a suppressed request is not
remembered, so a position that is still crossing fires after the gap), persisted debounce (a restart does not re-fire
immediately), and a failed request retried after min(debounce, 5 s) (`TRIGGER_FAILED`). Suppression is reported
(`TRIGGER_RATE_LIMITED`).

**Lost requests.** A request can be lost while a held pass is running or on `SCHEDULER_BUSY`. If no held pass followed
(no new journaled event for that mint after the request) the watcher asks again after 5 s, then 10 s, then 20 s (at most 3
refires, `source = refire`, counted in the budgets above) and then reports `TRIGGER_NOT_ACKNOWLEDGED`. Any held-pass event for
the mint, or the reason disappearing, cancels the refire.

## Starting the held pass without sudo or polkit

Default `--trigger path`: the watcher atomically replaces `<state>/trigger/request`; `desk-paper-held-cycle.path`
(`PathChanged=`) makes systemd start `desk-paper-held-cycle.service`. The watcher needs no privilege at all. The held
service keeps its own scheduler lease: a request that arrives while a pass is running is a no-op, and a lost race exits
0 (`SCHEDULER_BUSY`) like any timer tick. `--trigger systemctl` runs `systemctl start --no-block <unit>` and needs
privileges (root, or a polkit rule you write yourself); `--trigger none` / `--dry-run` only records.

The path unit deliberately has no `TriggerLimit*`: reaching a path unit's trigger limit stops the unit and would silently
disable fast exits. The watcher's own limits above are the guard.

### Install (coordinator)

1. `tools.ops.fresh_start render-units` fills `deploy/fresh/desk-held-watcher.service` and
   `deploy/fresh/desk-paper-held-cycle.path` from the applied manifest (`<FRESH_ROOT>`, `<RELEASE_DIR>`, `<CONFIG>`,
   `<STATE_DIR>`) into `<units>/research/`, outside the default install set. `docs/ops/RUNBOOK.md` step 11 is the exact
   procedure (create the watcher's directory first, install, verify, enable). `<STATE_DIR>/held-watcher`
   (`install -d -m 0700 -o solana-desk -g solana-desk <STATE_DIR>/held-watcher`) is the only path the service may write.
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
`RESERVES_STALE`, `VAULT_RESOLUTION_FAILED`, `LEDGER_UNREADABLE`, `TRIGGER_FAILED`, `TRIGGER_RATE_LIMITED`,
`TRIGGER_NOT_ACKNOWLEDGED`, `PROVIDER_BACKOFF`, `STREAM_STALLED`, `POSITION_NO_QUOTE_EXECUTION`, `POSITION_NO_DECIMALS`,
`MINT_FACTS_UNKNOWN`. Transitions are
also appended to the `health` table.

* Lost stream: reconnect with exponential backoff (2, 4, 8 ... capped at 60 s, jittered); polling every `--poll-seconds`
  (default 2 s) takes over at once. The backoff is KEPT across reconnects and reset only after the stream stayed up for
  `--stable-seconds` (60 s), so a connection that flaps every few seconds cannot hammer the provider at the minimum delay. On
  reconnect the watcher seeds the reserves with one poll, because `accountSubscribe` sends no initial value.
* Stalled stream: connected but silent for `--stall-seconds` (60 s) is ambiguous (a quiet pool, or lost notifications), so a
  poll checks. Unchanged reserves mean a quiet pool; changed reserves mean notifications were lost: `STREAM_STALLED`, a fresh
  connection and polling until it is acknowledged.
* Allowance exhausted: no further provider requests until the rolling window frees; price-based nudges pause, time-based
  ones still fire; status `DEGRADED`. The timer-driven held pass is unaffected.
* Watcher crash/kill: nothing is left behind (no pacing ticket, no ledger lock); its request log keeps the allowance honest
  across restarts; systemd restarts it.
* A vault whose mint, owner or layout is not exactly what the verified pool says is ignored and reported
  (`VAULT_DECODE_FAILED`), never trusted.

Healthcheck integration (T21's `tools.ops.healthcheck`) is **not** wired in this task: it would need a check for a stale
`health.json` and for `DEGRADED`/`STREAM_DOWN`. See the report.

## Measuring the benefit

`python -m tools.ops.held_watcher latency --ledger L --state-dir D` joins the `triggers` table with the ledger (read-only):

* a trigger is paired only with the FIRST held-pass event (market / quote_exit for its mint) and the first sell fill whose
  whole second is strictly after the trigger and within a bounded window (300 s); a fill before or in the same second is never
  paired, and an unrelated later pass outside the window is not attributed to a lost request;
* when several requests (refires) lead to the same held-pass event only the LAST one carries the latency; the earlier ones
  are marked `superseded`, so a fill is counted once;
* `watch_to_trigger_seconds` is our own processing (local receive time to trigger); `slot_to_trigger_seconds` is the real
  "event -> trigger" latency when `getBlockTime(slot)` could be recorded (a background request after each trigger, retried
  twice, recorded in `trigger_slot_times`; `slot_wall_time` says `BLOCK_TIME` or `NOT_RECORDED`).

Ledger times are whole seconds (the decision time of the held pass). Use it for T14-style analysis; it says nothing about
execution quality, only about how much sooner the held pass ran.

## Limits

* The mark is a constant-product estimate. The held pass may find a different executable quote (or none) and then not exit.
* PumpSwap pools only (the pool account is decoded and its PDA verified; anything else is not watched and is reported).
* Vault resolution costs one `getAccountInfo` per new pool; a position whose pool cannot be verified is not watched (timer only).
* It speeds up reaction by seconds; it cannot make a thin pool sellable.
