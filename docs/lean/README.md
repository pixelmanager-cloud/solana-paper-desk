# Lean paper trader (`lean/`)

A simple paper-only trader that runs beside the existing desk and never imports its gates, receipts, pins, monitoring
budget or pacing DB. No signing, no broadcasting, no real funds; every fill is quote-based and `EXECUTION_UNVERIFIED`.

## Run
```
python -m lean --config /etc/solana-paper/lean.json --state-dir /var/lib/solana-desk-lean \
  --discovery-db /var/lib/solana-desk/discovery/continuous.sqlite [--keys-file KEYS.json] [--once] [--clear-halt]
```
* **Config:** copy `config/lean/lean.example.json` (every key the runner reads; unknown keys are refused) next to a copy
  of `config/lean/strategy-default.json` (`strategy_config` is relative to the lean.json file). `initial_cash_sol`
  creates the store on first start; an existing store must match it.
* **Keys:** `--keys-file` defaults to the systemd credential `$CREDENTIALS_DIRECTORY/provider-keys.json`, the same file
  the desk uses: `{"HELIUS_API_KEY": "...", "JUPITER_API_KEY": "..."}` (mode 0400/0440/0600). Keys are never printed.
* **Unit:** `deploy/lean/desk-lean.service`. At install, set `Environment=LEAN_CODE_VERSION=<deployed git sha>` (every
  stored row carries it). It writes only `/var/lib/solana-desk-lean`; the discovery DB is read through its existing
  `-wal`/`-shm` with a read-only directory (see the comment in the unit).
* `--once`: one position pass and one candidate pass. `--clear-halt`: an operator records that a persisted halt is
  cleared (the store invariants must pass first).
* Report: `python -m lean.report --db /var/lib/solana-desk-lean/lean.sqlite --out-dir DIR`.

## Behaviour
* **Candidate loop:** discovery (`lean.candidates.scan_new`, cursor in `<state>/cursor.json`, an int from 0) → screen →
  entry gate (no quote) → buy quote at the decided size → sell-leg quote for exactly the tokens the fill will hold →
  entry decision on the round trip (cost cap) → paper BUY. The fill and the position's state (pool and vault keys,
  initial quantity, stage, stop, peak) are written in one transaction.
* **Position loop (≤10 s):** one batched `getMultipleAccounts` of all held vaults → net marks (constant product less the
  pool fee and the fixed fee) → exit decision → only when an exit triggers, a sell quote for the exact raw quantity →
  decision on the quote → paper SELL (TP rung / close written with the fill). Peak and touched changes are persisted, so
  rungs never re-fire and trailing arms across restarts.
* **Provider budget:** one token bucket per provider and lane for the process. The `exit` lane (marks, exit quotes) is
  a reserved share of the plan budget, so screening and its 429s never starve exits. Waits never exceed a call deadline.
* **Isolation:** a provider error, malformed response or bad evidence fails that candidate / position check (an
  `errors` row, raw bytes kept as an observation) and the loop moves on. Transient screening failures (including
  `HOLDERS_UNAVAILABLE`) are retried on the next passes (in memory, up to `max_candidate_retries`). A missing or locked
  discovery DB is `DISCOVERY_UNAVAILABLE`, retried next pass.
* **Halts:** only an accounting invariant violation or a store failure. The halt is written to the store, survives
  restarts and stops all new fills until `--clear-halt`. Startup runs `check_invariants` and `check_position_states`.
  The kill-switch file `<state-dir>/KILL` stops new entries only; held positions keep being managed.
* **Health:** `<state-dir>/health.json`, written atomically (unique temp file, fsync, rename, one writer at a time).

## Modules
`providers` (HTTP clients, limiters) · `candidates` (discovery + screen) · `strategy` (pure decisions) · `paper`
(integer fills) · `store` (append-only SQLite) · `adapters` (the ONE place units/types are converted) · `runner` ·
`report`. The end-to-end test `tests/lean/test_e2e_real.py` runs the real modules with only HTTP faked.

## Ops (L15): credits, watchdog, regime log, morning report
Enabled by an `ops` block in `lean.json` (see `config/lean/lean.example.json`; unknown keys are refused; remove the block to
turn the credit tracker and regime log off). Nothing here is a gate: no decision reads these rows.
* **Credit tracker** (`lean.ops.CreditTracker`). Every provider call (every HTTP attempt, retries included) is counted per
  provider and method and priced from `ops.credit_costs` (ESTIMATES: calibrate against the provider dashboard; default 1 credit per
  Helius request, 10 for `getProgramAccounts`, Jupiter and Kraken free). `health.json` -> `ops.credits` shows the month's usage and
  the projection (month-to-date plus the last 24 h rate times the time left; needs 10 minutes of data). Usage is saved in the store
  (`events` kind `credit_usage`, at most once a minute: a crash loses at most that minute) and continues after a restart; a new UTC
  month starts at zero. With `credit_budget_month` set and the projection above it, the low-priority lanes `paths`, `features` and
  `wallet_signals` are SHED until the projection falls under 90% of the budget: data-collection code asks `runner.ops.credits.allow(lane)`
  before spending, gets False, and one `CREDIT_SHED` row is written per lane and episode. Screening and exits are never shed or slowed.
* **Watchdog** (`lean.ops.Watchdog`). With `WatchdogSec=` in the unit (300 s) the runner sends `WATCHDOG=1` through `$NOTIFY_SOCKET`
  every half timeout, only while BOTH loops have beaten within `watchdog_stale_s` (default 180 s; the candidate loop also beats after
  every candidate, the position loop after every position). A stalled loop stops the pings, writes one `WATCHDOG_STALL` row and systemd
  restarts the service; positions resume from the store.
* **Regime log** (`lean.regime`). Every `regime_interval_s` (300) one `observations(kind='regime')` row: SOL/USD, its 1h and 24h change
  against the log's own earlier samples (null until a reference sample exists within 10 min / 72 min of the wanted time), and the
  candidates stored in the last hour (and per hour once 10 minutes have been observed).
* **Morning report.** `deploy/lean/desk-lean-report.timer` runs `python -m lean.ops report` at 23:00 UTC (08:00 KST) and writes
  `/var/lib/solana-desk-lean/reports/YYYY-MM-DD.html` (+ `.json`, KST date; an existing page is kept). It is the L06 report; with
  `--grid FILE` and L08 (`lean.tune`) installed it also writes `YYYY-MM-DD-replay.html` and links it. No notifications.
