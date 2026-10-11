# Lean paper trader (`lean/`)

A simple paper-only trader that runs beside the existing desk and never imports its gates, receipts, pins, monitoring
budget or pacing DB. No signing, no broadcasting, no real funds; every fill is quote-based and `EXECUTION_UNVERIFIED`.

## Run
```
cd /opt/solana-desk-lean && /opt/solana-desk/.venv/bin/python -m lean --config /etc/solana-paper/lean.json \
  --state-dir /var/lib/solana-desk-lean --discovery-db /var/lib/solana-desk/discovery/continuous.sqlite \
  [--keys-file KEYS.json] [--once] [--clear-halt]
```
* **Code and interpreter:** the code is a root-owned checkout at `/opt/solana-desk-lean` (the unit's
  `WorkingDirectory`, from which `python -m lean` resolves `lean/`). The interpreter is the desk venv.
* **Config:** copy `config/lean/lean.example.json` next to a copy of `config/lean/strategy-default.json`.
  * The example lists every key the runner reads; unknown keys are refused.
  * `strategy_config` is relative to the lean.json file.
  * `initial_cash_sol` creates the store on first start; an existing store must match it.
* **Keys:** `--keys-file` defaults to the systemd credential `$CREDENTIALS_DIRECTORY/provider-keys.json`. This is the
  same file the desk uses: `{"HELIUS_API_KEY": "...", "JUPITER_API_KEY": "..."}` (mode 0400/0440/0600). Keys are
  never printed.
* **Code version:** `Environment=LEAN_CODE_VERSION=<deployed sha>` is set at install, and every stored row carries it.
  If it is empty, the code version is `h:` + the sha256 of the sorted `lean/*.py` files. It never depends on git and
  is never `unknown`.
* **Unit:** `deploy/lean/desk-lean.service`, with `Restart=always`. It writes only `/var/lib/solana-desk-lean`. The
  discovery DELETE-journal database is opened with plain `mode=ro` from a read-only directory; see the comment in the
  unit.
* `--once` runs one position pass and one candidate pass.
* `--clear-halt` lets an operator record that a persisted halt is cleared; the store invariants must pass first. A
  RUNNING trader re-reads the store's halt state on every pass, so it resumes entries without a restart.
* Report: `python -m lean.report --db /var/lib/solana-desk-lean/lean.sqlite --out-dir DIR`. It opens the store with
  plain `mode=ro`.
* Exit codes:
  * 0: a clean stop (SIGTERM).
  * 2: the key file is invalid.
  * 3: `--once` finished halted.
  * 4: a loop thread died. systemd restarts the process.

## Behaviour
* **Candidate loop:**
  * Discovery uses `lean.candidates.scan_new`. The cursor in `<state>/cursor.json` is an int. On a first start it
    begins at the first frame newer than `now - max_age_seconds`, so it skips old history.
  * Each candidate goes through:
    1. the screen;
    2. an entry gate, with no quote;
    3. a buy quote at the decided size;
    4. a sell-leg quote for exactly the tokens the fill will hold;
    5. the entry decision on the round trip (cost cap);
    6. a paper BUY.
  * The fill and the position's state are written in one transaction. The state holds the pool and vault keys, the
    initial quantity, the stage, the stop and the peak.
* **Position loop (every ≤10 s):**
  * **Marks:** one batched `getMultipleAccounts` reads all held vaults. Net mark = the constant-product value less the
    pool fee and the fixed fee.
  * **Quote-marks:** a mark older than `stale_mark_s` (default 30 s, 3× the interval) is never used. After a failed
    read it is evicted, and the position is marked by a Jupiter sell quote for its whole quantity instead. That quote
    runs on the exit lane, independent of Helius, so stop, time-stop and max-hold still fire while Helius is down.
  * **Exit flow:**
    1. the exit decision;
    2. only when an exit triggers, a sell quote for the exact raw quantity;
    3. the decision on that quote;
    4. a paper SELL. The TP rung or the close is written with the fill.
  * **Persisted state:** peak and touched changes are saved, so rungs never re-fire and trailing stays armed across
    restarts.
  * **Unexitable positions:** an exit can stay wanted while its quote fails with no route or a 4xx (the failure start
    time is persisted). After `unexitable_after_s` (default 2 h), the position is written off at zero proceeds with
    reason `UNEXITABLE`. This frees the slot and books the loss.
* **Equity and daily loss:** these use only marks younger than `stale_mark_s`. A position with no usable mark is
  valued at cost and listed in health `cost_basis_mints`. The UTC-day baseline rolls over only with fresh marks.
* **Provider budget:** one token bucket per provider and lane for the whole process. The `exit` lane (marks, exit and
  mark quotes) is a reserved share of the plan budget, so screening and its 429s never starve exits. A wait never
  exceeds the call deadline. Data collection uses the non-blocking `low` lane; see "Provider lanes" below.
* **Isolation:** a provider error, malformed response or bad evidence fails that candidate or position check. It is
  recorded as an `errors` row, with the raw bytes kept as an observation, and the loop moves on. Details:
  * Transient screening failures, including `HOLDERS_UNAVAILABLE`, are retried on the next passes. This retry is in
    memory, up to `max_candidate_retries` times.
  * A missing or locked discovery DB is `DISCOVERY_UNAVAILABLE` and is retried on the next pass.
  * An observation larger than the store limit is truncated and flagged (`raw_truncated`, `full_bytes`,
    `full_sha256`), never refused.
* **Halts:** only an accounting invariant violation or a store failure. A transient sqlite lock is retried a few
  times first.
  * A halt stops NEW ENTRIES ONLY; exits keep running. It is logged at ERROR, written to the store and survives
    restarts until `--clear-halt`.
  * Startup runs `check_invariants` and `check_position_states`.
  * The kill-switch file `<state-dir>/KILL` also stops new entries only.
* **Threads:** each loop records failures and continues. If a loop thread dies, the other loop is stopped and the
  process exits with 4.
* **Health:** `<state-dir>/health.json` is written atomically (unique temp file, fsync, rename, one writer at a
  time). It holds the per-loop `heartbeat`, `dead_loops`, mark sources, `cost_basis_mints`, counts, errors by code
  and the halt state.

## Provider lanes (L07R): main / exit / low
Each provider's plan rate is split into three process-wide token buckets (`lean.providers.shared_limiter`). Helius at
10 req/s by default: **main 5.5/s** (candidate screening), **exit 2.5/s** (held-position marks, exit and mark quotes),
**low 2/s** (data collection). Jupiter (4 req/s) splits the same way. Kraken keeps one undivided bucket and has no low lane.
* **Config:** `lean.json` `lanes` = `{"main": 0.55, "exit": 0.25, "low": 0.20, "low_shed_s": 30}`. Absent = these
  defaults. Each share must be in (0, 1], and main + exit + low must be ≤ 1. A bad value is refused at startup
  (`ConfigError`). `providers.configure_lanes(shares, low_shed_s=...)` is the API; call it once, before any client exists.
* **The low lane never waits.** Without a token right now, or inside a shed window, a call fails at once with
  `ProviderError('LANE_SHED', transient=True)`. Nothing is sent in that case. `meta['why']` is `no_token` or `backoff`.
* **429 shedding:** any HTTP 429 seen by ANY lane of a provider sheds its low lane for `low_shed_s`, or longer when
  Retry-After asks for more (capped at 30 s). A 429 on the low lane blocks nobody else. The JSON-RPC throttle code
  `-32005` (an HTTP 200 answer) sheds the low lane the same way (L07R2).
* **No retries:** low-lane calls are never retried inside the call. A failed call is a gap for the caller to count.
* **Exits never wait behind low traffic:** the lanes are separate buckets. `tests/lean/test_low_lane.py` floods the low
  lane (1,000 calls, and 8 threads) and checks that the exit lane is untouched and acquires with zero wait.

**For other workers (entry features, wallet signals, ...):** put every data-collection call on the low lane and treat
`LANE_SHED` as "skip this cycle", never as an error worth retrying at once:
```python
from lean import providers
low = providers.low(keys)                  # Providers(helius, jupiter, kraken=None) on the low lane
try:
    result, raw, meta = low.helius.get_multiple_accounts(keys)
except providers.ProviderError as error:
    if providers.is_shed(error):           # nothing was sent; error.meta['why'] in ('no_token', 'backoff')
        ...                                # count it, try again next cycle (your own thread may pace on 'no_token')
    else:
        ...                                # a real provider failure: count it, never raise into the trader
```
* `providers.build_providers(keys, lane='low')` is the same thing as `providers.low(keys)`.
* `providers.low_lane_stats('helius')` returns the counters `granted`, `shed_no_token`, `shed_backoff` and `sheds`.
* All low-lane users share the one 2/s bucket. Keep your calls batched (`getMultipleAccounts` ≤ 100 accounts).

## Price-path recorder (L07R, `lean/paths.py`)
Every candidate that passes the HAZARD checks gets its pool marked every `interval_s` (15 s) for `path_hours` (6 h),
whether it was entered or not. This is data for strategy tuning (L08 replay).
* **Recorded:**
  * entered candidates;
  * screen rejects for market cap or liquidity out of band only (these stop before the holder check, so they carry
    `holders_checked: false`);
  * passed screens that were not entered: the cost cap, portfolio limits, the entry throttle, a cooldown, a quote that
    failed, or entries stopped.
* **Never recorded:** hazard rejects (authorities, extensions, pool/vault/LP evidence, holder concentration) and failed
  screens. A candidate queued for a retry is decided on its final attempt.
* **Its own database (L07R2):** `paths.db`, default `<state-dir>/paths.sqlite`, with its own connection and lock.
  * The trader's accounting store `lean.sqlite` gets NO path rows.
  * The recorder never reads or locks `lean.sqlite`: the not-entered reason comes from the runner's memory.
  * The notifier and the report only open `lean.sqlite`, so they never walk path rows.
* **Tables:** plain indexed tables, append-only by convention (the recorder only INSERTs). Query them directly:
  * `paths(mint PK, candidate_id, start_ts, ends_ts, entered, stage, reason JSON, screen_reasons JSON, holders_checked,
    features JSON, target JSON, carried_from, code_version, strategy_version)`.
  * `path_marks(mint, ts, slot, base_raw, quote_raw, net_sol, liquidity_sol, status)`, with an index on `(mint, ts)`.
    `net_sol` is `adapters.mark`, the same function the position loop marks with. It values the filled quantity for an
    entered candidate, otherwise the tokens `ref_size_sol` (0.2 SOL) buys at the path-start reserves.
  * `path_gaps(mint, ts, cause)`, with an index on `(mint, ts)`: a due mark that was skipped. `cause` is `shed`,
    `HTTP_429`, `budget`, another provider code, `stopped`, or `POLL_CALL_FAILED`.
  * `path_ends(mint, ts, why, marks, gaps)`: `COMPLETE` after `path_hours`, or `EXPIRED_WHILE_DOWN` on resume.
  * The live paths are the `paths` rows without a `path_ends` row.
* **Rotation and disk:**
  * When the file passes `max_db_gb` (default 2), it is renamed to `paths-YYYYMMDD[-n].sqlite`, which is logged at
    WARNING. A fresh `paths.sqlite` then carries over the live paths (`carried_from`).
  * Archives are never deleted automatically; delete old ones by hand.
  * Below `min_free_gb` of free disk (default 2), nothing is written and the polls count `disk_low`.
* **Run mode only:** `Runner.run()` builds the recorder, resumes the live paths from the file before the loops start,
  then starts its thread. `--once`, `--clear-halt` and `build_runner` never open `paths.sqlite`.
* **Polling:**
  * One `getMultipleAccounts` per 50 pools (100 accounts, the RPC limit), on the LOW lane. The marks, gaps and ends of
    a poll are written in one transaction.
  * When the low lane has no token, the recorder's thread paces its own calls within 80% of the interval.
  * A shed (a 429 or -32005 on any lane), the exhausted budget, or `stop` ends the poll. The skipped paths get
    `path_gaps` rows.
  * The order ROTATES: the next poll starts at the first skipped path, so the newest paths are never always the ones
    dropped. `stop` is checked between calls.
* **Isolation:** recorder failures are counted in `health.json` under `paths` and never raised. That includes `alive`
  (the thread runs), `errors_by_code`, `gaps`, `shed`, `paced`, `write_failures`, `rollovers`, `disk_low`, `db_bytes`
  and `low_lane`. The recorder never takes a runner lock and does no provider I/O under any lock.
* **Config:** `lean.json` `paths` = `{"enabled": true, "db": null, "path_hours": 6, "interval_s": 15,
  "ref_size_sol": "0.2", "max_paths": 2000, "max_db_gb": 2, "min_free_gb": 2}`. When `paths` is absent, the recorder is
  disabled.
* **Budget at ~70 recorded candidates/h:**
  * About 420 live paths, 9 calls per poll, so about 2,160 Helius calls per hour (0.6 req/s of the 2 req/s low lane).
  * Storage is about 180 B per mark, so about 18 MB/h (about 0.43 GB/day) in `paths.sqlite` (see `reports/L07R2.md`).

## Modules
* `providers`: HTTP clients and limiters (main / exit / low lanes).
* `paths`: the price-path recorder (low lane).
* `candidates`: discovery and the screen.
* `strategy`: pure decisions.
* `paper`: integer fills, including the write-off.
* `store`: append-only SQLite.
* `adapters`: the ONE place where units and types are converted.
* `runner`: the two loops.
* `report`: the read-only report.

The end-to-end test `tests/lean/test_e2e_real.py` runs the real modules with only HTTP faked.

## Held-position rug handling (L11F, `lean/held_risk.py`)
OFF unless `"held_risk": {"enabled": true}` (the key absent or `enabled: false` gives exactly the previous behaviour; unknown keys are refused).
The module only DETECTS and VALUES; it never sells. A detected rug makes the exit wanted as `DANGER` inside `Runner._manage`, so the quote, fee,
slippage, D2's persisted `exit_failing_since` clock and D2's write-off are the ordinary ones. Triggers per held mint:
* **LIQUIDITY_DROP**: the quote vault fell by MORE than `rug_liq_drop_frac` (0.7) since the screen tied to the BUY (the latest PASS screen decided no
  later than the opening fill); read from the marks the position loop already makes;
* **POOL_GONE**: vault accounts missing from the marks read, or the pool account closed / owned by another program, in `pool_gone_confirmations` (2)
  CONSECUTIVE reads (a good read resets; the count survives a restart);
* **NO_ROUTE**: no Jupiter sell route for `unsellable_after_s` (120 s), probed every `route_probe_s` (60 s) AFTER the exits of the pass (skipped where a
  D1 quote mark proves a route). A provider outage is never "no route";
* **freeze authority**: a flag; it forces an exit only with `freeze_forces_exit`.

Order in one position pass: marks read -> `after_marks` (no I/O: baselines, detection, valuation) -> D1 quote marks -> exits (a trigger is `DANGER`)
-> `after_exits` (route probes and one batched `[mint, pool]` read every `freeze_check_every` (6) passes; findings act on the NEXT pass).
The probes and account checks use the shared non-blocking LOW lane (LINT1), so they never sleep in the position thread; a `LANE_SHED`
probe sends nothing, records no error and is retried on the next pass (health `held_risk.shed`). They also run after L10's two-phase exits.

If the forced exit cannot be quoted, D2's clock runs; meanwhile the position is valued at 0 (unless an executable quote younger than
`unsellable_value_ttl_s` (30 s) exists; never at its last price, never at cost) and after `unexitable_after_s` D2 books the zero-proceeds write-off
with the reason `RUG_WRITEOFF` (`UNEXITABLE` when no trigger fired). A rug exit and a write-off cool down like a stop. State: `held_risk` events
plus `rug_trigger` stamped on the position state; the report has a "Rugs and write-offs" section (reasons `DANGER` and `RUG_WRITEOFF`).

## Ops (L15, `lean/ops.py`): credits, watchdog, regime log, morning report
Enabled by an `ops` block in `lean.json` (unknown keys are refused; remove the block to turn the credit tracker and regime log off).
Nothing here is a gate: no decision reads these rows.
* **Credit tracker.** Every HTTP attempt of EVERY lane (main, exit and the shared low lane) is counted per provider and method at the
  transport (`providers.CALL_OBSERVERS`, LINT1) and priced from `ops.credit_costs` (ESTIMATES: default 1 credit per Helius request,
  10 for `getProgramAccounts`; Jupiter and Kraken free; calibrate against the dashboard). A call that sent nothing (`LANE_SHED`) costs
  nothing. `health.json` -> `ops.credits` shows the month's usage and the projection (month-to-date + last-24 h rate x time left; needs
  10 minutes of data). Usage is saved as `events` kind `credit_usage` (at most once a minute) and continues after a restart.
* **Shedding.** With `credit_budget_month` set and the projection above it, the housekeeping thread sheds the SHARED Helius low lane
  (`providers.shed_low`, re-applied every `housekeeping_s` while over budget) until the projection falls under 90 % of the budget:
  the path recorder, the held-risk probes / account checks and every other low-lane user skip their calls (`LANE_SHED`). One
  `CREDIT_SHED` error row per episode (message `low`). Screening and exits are never shed or slowed.
* **Watchdog.** With `WatchdogSec=` in the unit (300 s) the runner sends `WATCHDOG=1` through `$NOTIFY_SOCKET` every half timeout, only
  while BOTH loops have beaten within `watchdog_stale_s` (180 s). A stalled loop stops the pings (one `WATCHDOG_STALL` row) and systemd
  restarts the service.
* **Regime log.** Every `regime_interval_s` (300) one `observations(kind='regime')` row: SOL/USD (the runner's cached value), its 1h/24h
  change against the log's own earlier samples, and candidates per hour. A feature only, never a gate.
* **Morning report.** `deploy/lean/desk-lean-report.timer` runs `python -m lean.ops report` at 23:00 UTC (08:00 KST) into
  `/var/lib/solana-desk-lean/reports/YYYY-MM-DD.html`. No notifications.
