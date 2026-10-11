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
  exceeds the call deadline.
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

## Modules
* `providers`: HTTP clients and limiters.
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

If the forced exit cannot be quoted, D2's clock runs; meanwhile the position is valued at 0 (unless an executable quote younger than
`unsellable_value_ttl_s` (30 s) exists; never at its last price, never at cost) and after `unexitable_after_s` D2 books the zero-proceeds write-off
with the reason `RUG_WRITEOFF` (`UNEXITABLE` when no trigger fired). A rug exit and a write-off cool down like a stop. State: `held_risk` events
plus `rug_trigger` stamped on the position state; the report has a "Rugs and write-offs" section (reasons `DANGER` and `RUG_WRITEOFF`).
