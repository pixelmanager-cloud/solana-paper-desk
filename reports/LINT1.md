# LINT1: lean-desk integration of L16F, L06F, L10F, L11F, L14F, L15, L13

Branch `cloud/LINT1`, cut from `origin/integration/r1` (129f31a: L09 + L09b, L17, entry spacing, L07R) and then updated with the
new `origin/integration/r1` (L07R2: paths in their own `paths.sqlite`). `desk/` untouched. Paper only: nothing signs or sends.
Every branch was reviewed against its merge-base, merged ONE AT A TIME, and the full lean suite run in ONE process after each step
(`python -m unittest discover -s tests/lean -t .`, `TMPDIR` set, real rc). One mutation per branch was re-run and killed.

## Verdicts

| ID | Verdict | Why |
|---|---|---|
| L16F | **MERGED** | Built on r1 (D1/D2). Default off. Re-uses the quote response (no extra provider call, so no lane question). Defects of the earlier review fixed. |
| L06F | **MERGED** | Small, correct, clean merge. |
| L10F | **MERGED** | Two-phase exits with one delay per pass, q1 failures through D2, rent cleared on write-off, cost cap includes fee+tip, additive hooks. Its early `return` from `position_pass` broke L11 (fixed under L11F). |
| L11F | **MERGED+FIXED** | Review items 1-7 fixed, but probes ran on the EXIT lane and stalled the position thread; and with L10 on they never ran. Both fixed (`LINT1: fix L11F ...`). |
| L14F | **MERGED** | Portfolio gate before any re-screen, re-screens after the scan, hourly cap, fill ends the watch, EXPIRED vs HAZARD, paged reads, 30-line hook. New AMM sources honestly left TODO (unverified layouts). |
| L15 | **MERGED+FIXED** | Cut from the OLD L09. Watchdog, regime log and morning report are fine, but the credit tracker never counted the low lane and its CREDIT_SHED gated nothing. Fixed (`LINT1: fix L15 ...`). |
| L13 | **SKIPPED** | Private limiter that takes a token from the MAIN (screening) bucket on every call; path outcomes read an L07 schema that L07R never wrote; old base. Needs an L13F rework (details below). |

## Per ID

### L16F: MERGED
- Review: `route_check.py` reads the build response the quote already fetched (`/swap/v2/build`), so there is no extra call and no
  lane to choose. Once per trade (durable), transient/our-call errors are `INCONCLUSIVE`, exact `UNSUPPORTED_ERROR_CODES` only, a
  non-dict config is refused, the public `store.code_version` is used. The AST walker flags only calls / attributes / imports and its
  self-test uses the same walker; it scans the WHOLE `lean/`, so every later module (execution, held_risk, ops, ...) is covered.
- Minor (not fixed): `on_fill` reads the store (`observation`, up to 10,000 decision rows per mint) under `_trade_lock`. Store only,
  no provider I/O, bounded per mint.
- Conflict: `__main__.py` with L07R; both kept.
- Mutation re-run: "every error is conclusive" -> 14 failures incl. 2 e2e (KILLED).

### L06F: MERGED
- Review: a trade (and all its fills) belongs to the strategy_version of its OPENING buy in `report.py` and in notify `/pnl` and
  the fill events; `mixed_version` flagged per trade. Clean merge.
- Mutation re-run: `COALESCE` preferring the sell's version -> `test_realized_per_version_is_by_opening_buy` fails (KILLED).

### L10F: MERGED
- Review vs the fix-round items: (1) `run_exits`: phase 1 runs the normal exit logic, triggered exits raise `_Deferred` (a
  BaseException, before any lock) with q0; ONE sleep; phase 2 re-runs and fills on q1. Non-exiting positions finish in phase 1 on
  fresh marks. (2) a q1 `ProviderError` goes through `_manage`'s own D2 path. (3) rent is out of the shown prices (report and notify
  via `economic`), a D2 write-off clears the outstanding rent, and fee+tip enter through `fixed_fee_sol` (so the cost cap includes
  them). (4) the hooks are additive (`adapters` wraps the old functions).
- Minor (not fixed): `_restamp` shifts a mark's timestamp forward by the delay (up to `exec_delay_s`) so phase 2 sees the decision
  as it was; after a PARTIAL rung that mark stays ~3 s "fresher" than it is, until the next read. `config_hash` does not change with
  the added fee (the `execution_config` event records it).
- Integration defect: `position_pass` RETURNS from `run_exits`, so anything after the exit loop (L11's probes) was skipped; fixed in
  the L11F fix commit.
- Conflicts: runner signature/`__init__`, `__main__`, report (with L07R, L16); all kept.
- Mutation re-runs: a delay per deferred exit -> the 6-exit e2e fails; write-off not clearing rent -> the D2 e2e fails (KILLED).

### L11F: MERGED+FIXED
- Review vs the fix-round items: (1) a triggered position whose exit is failing is worth 0 unless an executable quote younger than
  `unsellable_value_ttl_s` exists; (2) marks stay `(value, at, source)`; (3) ONE write-off path: D2's `_write_off` picks
  `RUG_WRITEOFF` vs `UNEXITABLE` from `rug_trigger`; `_manage` is never short-circuited (a trigger only makes the exit `DANGER`);
  (4) default off; (5) probes after the exit pass; (6) baseline = the latest PASS screen at or before the opening fill;
  (7) POOL_GONE needs 2 consecutive reads and the counters replay after a restart.
- **Defect A (fixed):** route probes and account checks used `runner.exit_providers`. Jupiter's exit lane is 1 req/s with burst 1,
  so N held positions cost ~N-1 s of blocking sleep in the position thread on every probe round (every 60 s): 15 positions stall
  the next pass by ~14 s, i.e. exits wait. Now they use the SHARED non-blocking low lane (`providers.low(keys)`, set in
  `build_runner` inside a `LINT1 hook` block); a `LANE_SHED` sends nothing, records no error, and the probe is due again next pass
  (health `held_risk.shed`).
- **Defect B (fixed):** with L10 on, `position_pass` returned from `run_exits` before L11's `after_exits`: NO_ROUTE and the account
  checks never ran. The L10 hook now calls `after_exits_safe` before returning.
- Minor (not fixed): `held_risk` uses `store._lock` / `store.db` directly; the D2 write-off reason line is edited in place (the spec
  required that single path).
- Tests: `tests/lean/test_lint1.py` (no NO_ROUTE detection with L10 on; probes on the low lane; a probe round never sleeps; a shed
  probe sends nothing, records nothing and is retried). All 4 FAIL without the fix.
- Mutation re-run: valuing a failing rug at a stale quote -> unit + e2e fail (KILLED).

### L14F: MERGED
- Review vs the fix-round items: `portfolio_blockers` (no I/O) before any re-screen; re-screens after the fresh scan; hourly cap
  from persisted events (`<= 360 Helius + 240 Jupiter` per hour at the default 120); any fill ends the watch; `watch_hours` checked
  against `max_age_seconds`, TOO_OLD = EXPIRED; paged reads (`Store.rows_after`); the runner hook is 2 blocks <= 30 lines.
- Re-screens are entry screening, so they use the MAIN lane (correct: they can lead to a BUY; they are bounded by the cap).
- **New AMM sources are NOT implemented** (Raydium CPMM / Meteora DAMM v2): no verified captured layout. `build_sources` refuses
  those types. Only the pump graduation source exists.
- Minor (not fixed): `watchlist.install` wraps `runner._isolated` and `store.add_candidate` at instance level (documented).
- Merge: `test_e2e_sources.test_no_read_uses_the_capped_oldest_100000_window` spied on EVERY `Store.rows` call and tripped on the
  L07R recorder's per-mint reads; the spy now counts only L14's own reads (`lean.watchlist` / `lean.sources`), its stated scope.
  (L07R2 has since removed the recorder's store reads entirely.)
- L14F does not read path rows.
- Mutation re-run: no portfolio gate before a re-screen -> 3 tests incl. the e2e fail (KILLED).

### L15: MERGED+FIXED
- Base: the OLD L09 (no L09b, no L07R). Conflicts resolved onto the current runner: `position_pass` keeps L09b's `refresh_halt` and
  exits-while-halted (L15's old `if halted: return 0` dropped); beats added; `ops.health()` merged into the health dict; the ops
  threads join with the others in `run()`.
- **Defect A (fixed):** the credit tracker metered only the main/exit `Providers` bundles. The low lane (path recorder ~2,160
  Helius calls/h, the biggest consumer; held-risk account checks) was never counted, so the projection was far too low. Counting
  now happens at the transport for EVERY lane: an additive `providers.CALL_OBSERVERS` hook (weak set) in `Transport.call`; a call
  that sent nothing (`LANE_SHED`) costs nothing; `Ops.meter` only registers the tracker (no double count).
- **Defect B (fixed):** over budget, `CREDIT_SHED` only answered `allow('paths'|'features'|'wallet_signals')`, which no code
  calls. The housekeeping thread now sheds the SHARED Helius low lane (`providers.shed_low`, re-applied every `housekeeping_s` while
  over budget), with one `CREDIT_SHED` row per episode (message `low`). Screening and exits are never shed.
- Portability (needed for a green suite on macOS; production is Linux): `getattr(socket, 'SOCK_CLOEXEC', 0)`; the abstract-socket
  test is skipped off Linux.
- Minor (not fixed): the unit comment says `Restart=on-failure` covers a watchdog kill; the unit is `Restart=always` (also covers it).
  The regime log calls `runner._sol_usd_value` from the ops thread (Kraken main bucket, cached 30 s; benign).
- Tests: `test_lint1.py` (a low-lane call is counted, a shed one is not; over budget the shared low lane is shed and exits are not):
  both FAIL without the fix. `test_e2e_ops` now also expects the `low` lane and checks the low limiter really raises `LANE_SHED`.
- Mutation re-run: watchdog pings regardless of stale heartbeats -> 5 watchdog tests fail (KILLED).

### L13: SKIPPED (not merged)
1. **Private limiter on the screening budget.** `build_low_lane_helius` builds its own `RateLimiter` AND takes a token from
   `shared_limiter('helius', 'main')` (blocking `acquire`) for every call: up to 16 calls per candidate are drawn from the screening
   bucket, and the calls wait. It must use the shared non-blocking `providers.low(keys)` and treat `LANE_SHED` as "pace / skip".
2. **Path outcomes read a schema that does not exist.** `path_outcome` reads `observations(kind='path_mark')` `meta.price_sol`
   (old L07). L07R wrote `{base_raw, quote_raw, net_sol, ...}`, and L07R2 has moved paths out of `lean.sqlite` into `paths.sqlite`.
   Smart-wallet outcomes from paths would never resolve. The read is already isolated in one function (`path_outcome(store, mint,
   cfg)`), so a rework can repoint it at the L07R2 reader.
3. Old base (L09): `run()` / health conflicts; `store._lock` private access.
These are a module redesign of its budget and its tests, not a <=30-line fix. Suggested L13F: rebase on the current r1, move every
call to `providers.low(keys).helius` (pace on `no_token` in its own thread, stop the candidate on `backoff`), read outcomes through
the L07R2 path reader, keep the per-candidate hard cap.

## Example config (`config/lean/lean.example.json`)
- Every key the runner reads (`set(raw) == CONFIG_KEYS`). ON at paper-collection defaults: `paths` (L07R2), `route_check` (L16),
  `execution` (L10: 3 s, 0.0001 + 0.0001 SOL, ATA rent), `held_risk` (L11 defaults), `watchlist` (L14: 300 s, 2 h, 5 per pass,
  120 per hour), `ops` (L15) with `credit_budget_month: 10000000` = **ASSUMED Helius Developer plan (10M credits/month)**, marked in
  the file. No `wallet_signals` key (L13 skipped). `sources: []` (no verified AMM source).
- Validated by `tests/lean/test_lint1.py`: the REAL `__main__.load_config` accepts it; `entry.main([... '--once'])` on it returns 0
  with no key in any output; the 20-candidate e2e (Helius outage + restart) with EVERY feature on: no halt, invariants every tick,
  route-check rows, execution payloads, credits counted.
- The tests of the DEFAULT behaviour now load `tests/lean/lean.baseline.json` (the pre-LINT1 example, features off) instead of the
  deploy example; two "the example ships X disabled" assertions now assert the deploy example enables X and the fixture does not.

## Provider calls per hour, everything on (~70 candidates/h, up to 15 positions, position pass every 10 s)
Estimates from the code paths; Helius costs 1 credit per request except where noted (calibrate on the dashboard).

| Provider / lane | Users | Calls/h (est.) | Lane capacity/h | Use |
|---|---|---|---|---|
| Helius main (5.5/s) | screen: 2 x getMultipleAccounts + 1 getTokenLargestAccounts per candidate = 210; watchlist re-screens <= 120 x 3 = 360 (cap) | <= ~570 | 19,800 | ~3% |
| Helius exit (2.5/s) | marks: 1 getMultipleAccounts per pass (15 positions <= 50) = 360 | ~360 | 9,000 | ~4% |
| Helius low (2/s) | path recorder ~2,160 (420 live paths, 9 calls/poll, 15 s); held-risk account checks 360/6 = 60; L16/L15 add 0 | ~2,220 | 7,200 | ~31% |
| **Helius total** | | **~3,150/h = ~2.3M credits/month** | | **~23% of the assumed 10M** |
| Jupiter main (2.2/s) | entry: buy + sell-leg <= 140; L10 re-quotes <= 140; watchlist entries <= 240 | <= ~520 | 7,920 | ~7% |
| Jupiter exit (1/s) | exits: ~20 trades/h steady state (15 slots / 45 min time stop) x <= 3 sells x 2 quotes (L10 q0+q1) <= ~120 | ~120 | 3,600 | ~3% |
| Jupiter low (0.8/s, burst 1) | held-risk route probes: 15 positions x 1/60 s = 900 wanted | ~360 effective | 2,880 nominal | see note |
| Kraken (0.5/s) | SOL/USD, 30 s cache (+ regime log reuses it) | <= 120 | 1,800 | ~7% |

Notes:
- **Held-risk probe cadence:** the Jupiter low bucket refills 0.8/s but holds only 1 token, so a 10 s position pass gets ~1 probe:
  ~360 probes/h, i.e. each of 15 positions is probed every ~150 s instead of 60 s (skipped anyway when a D1 quote mark proves a
  route). NO_ROUTE detection therefore takes up to ~120 + 150 s. Raising the low-lane burst is a one-line follow-up if wanted.
- **Helius outage (pre-existing D1 behaviour, not new):** with stale marks, every pass quote-marks all 15 positions on the Jupiter
  EXIT lane: 15 per 10 s = 1.5 req/s > the 1 req/s exit bucket, so the position thread waits ~5 s per pass during an outage.
- L13 (skipped) would add up to 16 Helius calls per candidate (~1,100/h).

## Updated r1 (L07R2) merged last
`origin/integration/r1` 26bfdfd (L07R2: paths in their own `paths.sqlite`, recorder built in run mode only, outcomes from runner
memory) was merged after all the branches. Conflicts: `__main__.py` (L07R2's recorder factory beside every other hook),
`runner.py` health (`_l07r_health()` beside `held_risk` and `ops`), the example config (+ `max_db_gb`, `min_free_gb`).
`providers.py` merged cleanly: L07R2's `Transport.shed_low_lane()` / `-32005` handling and LINT1's `CALL_OBSERVERS` hook are both
present. Nothing in LINT1 reads `paths.py` internals or `path_*` rows (L13, the only reader, was skipped).
- Follow-up (not fixed, small): an entry dropped by L10's latency re-quote (`ENTRY_ABORTED_LATENCY`) returns before the L07R2
  `_l07r_note(entry_reasons=...)` line, so the recorder sees that candidate as passed-but-not-entered without a reason.

## Tests
- Final: `python -m unittest discover -s tests/lean -t .` in ONE process: **647 tests, OK, rc=0** (1 skipped: Linux-only abstract
  socket test on macOS). Per step: r1 361; +L16F 390; +L06F 398; +L10F 435; +L11F 498; +LINT1 L11 fix 502; +L14F 567; +L15 634;
  +LINT1 L15 fix 636; +example config 639; +r1/L07R2 647.
- New test file `tests/lean/test_lint1.py` (9 tests); fixture `tests/lean/lean.baseline.json`.

## Incident
Around 13:07-13:08 another agent's mutation run (through a shared scratchpad helper) replaced the 429 `shed_low(...)` line in
`lean/providers.py` with `pass` in THIS worktree. It was never committed: I saw the low-lane shedding tests fail, found the foreign
edit with `git diff`, restored the file and re-ran the suite green. All later runs use helpers inside this worktree only
(`.lint1/`, never committed).
