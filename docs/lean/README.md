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

## Held-position rug / unsellable handling (L11, `lean/held_risk.py`)
Configured by the `held_risk` object of `lean.json` (all keys optional; unknown keys are refused; `"enabled": false` gives exactly the
previous behaviour). For every held mint the position loop watches:
* **LIQUIDITY_DROP**: the pool's quote vault fell by MORE than `rug_liq_drop_frac` (0.7) since entry (baseline = the reserve the screen saw);
* **NO_ROUTE**: the Jupiter sell quote has had no route for longer than `unsellable_after_s` (120 s); probed every `route_probe_s` (60 s) with a
  full-size sell quote on the exit lane. A provider outage (timeout, 5xx, 429) is NOT "no route". About one Jupiter call per position per minute;
* **POOL_GONE**: a vault account is missing, the pool account is closed, or its owner program changed (migrated); one batched read of
  `[mint, pool]` per held position every `freeze_check_every` (6) position passes, which also reads the mint's freeze authority;
* **freeze authority** set after entry: recorded as a flag and reported; it forces an exit only with `freeze_forces_exit: true`.

Any of the first three sells the whole position at a FRESH quote (`RUG_EXIT`, cooldown as after a stop) before the normal strategy looks at it.
If no executable route exists (a non-transient quote failure, or a route worth less than the fee) the position is **UNSELLABLE**: valued in the
equity at the best executable quote seen (0 if there never was one), re-tried every pass (a route that comes back is used,
`trigger: ROUTE_RESTORED`) and, after `unsellable_writeoff_s` (6 h), closed in the books at that value with exit reason `RUG_WRITEOFF` and no fee
(nothing is sent). A transient quote failure while exiting is retried, never turned into UNSELLABLE.

State lives in generic `events` rows of kind `held_risk` (no schema change) and is rebuilt at start, so UNSELLABLE, the no-route clock and the
write-off clock survive a restart. `health.json` has a `held_risk` block (trigger / rug-exit / write-off / freeze counts and the UNSELLABLE mints).
The report has a "Rugs and write-offs" section: counts and PnL of `RUG_EXIT` and `RUG_WRITEOFF` apart from the ordinary exit reasons, the triggers
that fired, freeze flags, and the positions that are UNSELLABLE now with their value.
