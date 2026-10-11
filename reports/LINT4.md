# LINT4: integration and harsh review of L20, L21, L22, L23, L24

Branch `cloud/LINT4`, from `origin/integration/r1` (8c64fc8, live in paper). Paper only: nothing signs or sends, `desk/` untouched, no
network in any test, no key printed. Merge order: L20 (b5fc18f), L22 (3dd7627), L23 (4f7e82a), L24 (273dcbb), L21 (e1053b5, sent
later by the coordinator). Every merge was followed by the full lean suite in ONE process and that branch's mutation script.

| step | suite (one process) | mutation script |
|---|---|---|
| r1 baseline | 927 tests, OK (skipped=1), rc=0 | |
| + L20 | 978, OK, rc=0 | l20 38/38 |
| + L22 | 1010, OK, rc=0 | l22 35/35 |
| + L23 | 1044, OK, rc=0 | l23 28/28 |
| + L24 | 1091, **1 failure** -> fix -> 1091, OK, rc=0 | l24 34/34 |
| + L21 | 1127, **1 error** -> fix -> 1130, OK, rc=0 | l21 28/29 (**BAD MUTANT**) -> fix -> 29/29 |
| + LINT4 integration | **1142, OK (skipped=1), rc=0** | lint4 13/13 |

Final run of every mutation script on the merged tree (after all fixes): **l20 38/38, l21 29/29, l22 35/35, l23 28/28, l24 34/34,
lint3 33/33, lint4 13/13, each rc=0.** (The first final run found two more merge-ambiguous patterns, l24 and lint3: fix 6.)

## Verdicts

| ID | verdict | why |
|---|---|---|
| L20 sell_evidence + flow | **ACCEPT** (example policy changed to `flag_only`) | Probe starts on its own daemon thread, the exit waits only inside the L10 delay it pays anyway (bounded by the remaining delay, then the rest is slept), so the exit instant is unchanged; mutants kill "unbounded wait" and "delay slept on top". No lock across I/O (the probe's lock only guards dicts; the trade lock is never held when the probe starts). Low lane only. Flow rows are whole-second cut before the decision (`window_after_decision_s` 0, learner guard). Absent = off. Under `flag_only` the fill is the normal SELL at q1 (flagged): recording only. |
| L21 risk_external | **ACCEPT** (2 integration fixes) | RugCheck has its own 1 req/s bucket (`Transport('rugcheck', own RateLimiter)`); a 429 there tries to shed a `rugcheck` low lane that does not exist (caught, no Helius/Jupiter effect). Credits: `rugcheck` table at 0, compatible with the LINT4 cost table (the example sets it explicitly too). Its thread is now in the single aux list under the shared deadline. Values are AT_COLLECTION snapshots; the learner uses them only within `max_late_s`. Absent = off. |
| L22 honeypot_sim | **ACCEPT** (1 integration fix in its e2e) | The ONLY entry-changing feature, main lane, before the entry quotes; never on the exit thread. `simulateTransaction` is admitted by `Helius.rpc` only unsigned (all-zero signature slots), `sigVerify: false`, `replaceRecentBlockhash: true`, base64, <= 1232 bytes; the AST no-sign guard allows exactly that one call site. It rejects only BLOCKED (hazard) or an unverifiable risky Token-2022 mint; a plain pump SPL mint costs 0 calls. Cap 30 simulated candidates/h, persisted. Absent = off. |
| L23 bot_exit | **ACCEPT** (shadow kept) | Own daemon thread, low lane, `trigger()` is a dict read under a tiny lock (no I/O) and returns something only in `live`; `exit_decision(bot_exit=False)` is a no-op (fills byte-identical in shadow). The extra `tx_rate_ratio < 0.6` in `PUMPERS_OUT` is accepted. Absent = off. Weakness: at 15 positions `max_calls_per_pass` 6 polls each pool only every ~25 s against a 30 s window (gaps -> `None` metrics, never a false signal); see recommendations. |
| L24 clusters | **ACCEPT** | Own daemon thread, low lane, `stop` before every request, pacing with no lock held, `reserve_tokens` peek. Holders from the screen snapshot, window cut at the decision, histories and hub evidence at or before it; learner guards (`holders_source`, `supply_source`, `AT_COLLECTION` within `max_late_s`). Absent = off. Its new `fakeworld.Token.holder_accounts()` method collided with an L22 test double (fixed). |

Common checks, all five: nothing runs on or slows the exit thread except the L20 probe start (non-blocking) and its wait inside the
existing L10 delay; no provider I/O under a lock; the e2e tests use the real runner/store/providers/execution with only HTTP faked;
default OFF when the key is absent (tested against the live production config below); every mutation script kills everything.

## Fixes (each with a test)

1. `adeb5a7` **LINT4: example `sell_evidence.unverified_sell_policy` = `flag_only`** (coordinator decision 1). Test: `test_e2e_l20` asserts the
   shipped policy; `test_lint4.ProductionConfig` asserts it in the enable-list.
2. `b65129d` **LINT4: fix L22 e2e `Token22.holder_accounts`**. L24 added `fakeworld.Token.holder_accounts()` and `World.getMultipleAccounts` calls
   it; L22's `Token22` test double set an attribute of that name (a list), so every getMultipleAccounts raised and nothing was bought
   (`test_e2e_honeypot_sim` B failed after the L24 merge). Renamed the test attribute; that e2e is the regression test.
3. `d55635b` **LINT4: fix L21 learner e2e for the merged example**. It loads the shipped example with `execution` None; L20's
   `sell_evidence` (on in the example) refuses to build without execution. The world switches `sell_evidence` off too (L20's own pattern).
4. `7a525bb` **LINT4: fix `l21_mutate.py`**. After L24, "late risk snapshots accepted" matched two lines (L24's cluster guard is the same
   expression) -> BAD MUTANT, rc=1. The pattern now ends at the newline only the L21 line has: 29/29.
5. `f43a699` **LINT4 integration** (coordinator decisions 2-3):
   * `runner.run`: ONE list of auxiliary daemon threads (`_aux_specs`: features, wallet_signals, flow, clusters, bot_exit, risk_external)
     plus the path recorder, joined under ONE `AUX_JOIN_S` = 10 s deadline in total (TimeoutStopSec=30). Before, L21 joined in a second
     block (same deadline, but a separate code path that the next feature would have to remember).
   * `lanes.helius_rps` (new, optional; absent = 10/s exactly as today; 1..500; burst = rate). Shares still validated (each in (0, 1],
     sum <= 1). Example: 20/s x 0.35 / 0.20 / 0.45 = **main 7, exit 4, low 9 req/s**; Jupiter unchanged (4/s -> 1.4 / 0.8 / 1.8).
   * `ops.DEFAULT_COSTS` / example: `getAsset` 10, `getProgramAccounts` 10, `getTransactionsForAddress` 100 (not called by lean/ today;
     conservative, >= 10), everything else 1, `rugcheck` 0. Example `credit_budget_month` 9,000,000. `LOW_PRIORITY_LANES` += flow,
     sell_evidence, bot_exit, clusters (health lists them while shedding). Shedding still sheds the ONE shared Helius low lane.
   * Tests `tests/lean/test_lint4.py` (12): lane rates; bad `helius_rps` / shares > 1 refused at load and nothing changed; the
     `reserve_tokens` yield at burst 9 still takes 8 and leaves the 9th token to another user (1 at the old burst 2); the cost table;
     **over budget every low-lane collector (paths, features, wallet_signals, flow, clusters, bot_exit, sell_evidence, held_risk) gets
     LANE_SHED with zero HTTP, while a main-lane (screening and honeypot_sim) and an exit-lane call still go out**; `allow()` never refuses
     main / exit / screen / honeypot_sim / risk_external; 7 hanging threads stop within one deadline; the recorder is joined too; the
     production config; the example lists every key and keeps L23 shadow. `tests/lean/lint4_mutate.py`: 13/13 killed.
   * Existing tests updated for the decided values only: `test_lint1` (budget 9,000,000), `test_ops` (cost table), `test_e2e_ops` (shed lanes).
6. `b8056c5` **LINT4: fix `l24_mutate.py` / `lint3_mutate.py`**. Same class as fix 4: L21's guard duplicated L24's "late cluster snapshot"
   expression and L20's flow guard duplicated LINT3's "uncut wallet window" expression (BAD MUTANT, rc=1). Both patterns now include their
   unique trailing comment: 34/34 and 33/33.

Reserve/burst at the higher low-lane rate: L13 wallet_signals, L20 flow/sell_evidence (`LowCalls`), L23 `_call` and L24 clusters all use
the same peek `limiter.available() < 1 + reserve_tokens`; at 9 req/s / burst 9 they simply get more tokens before yielding and still
leave `reserve_tokens` for the others (tested with the real `LowLaneLimiter`). Pacing waits are unchanged (0.25-0.5 s, own threads).

## Call budget (Helius, worst case at ~70 candidates/h and up to 15 positions)

Low lane capacity at `helius_rps` 20 x 0.45 = **9 req/s = 32,400 calls/h**.

| lane | feature | worst-case calls/h | basis |
|---|---|---|---|
| low | paths (L07R) | 2,160 | 70/h x 6 h = 420 live paths, 50 pools per getMultipleAccounts, every 15 s (9 calls / 15 s). At `max_paths` 2000: 9,600 |
| low | held_risk (L11) | 60 | one batched account check every 6th position pass (60 s) |
| low | features (L12F) | 210 | <= 3 per hazard-free candidate |
| low | wallet_signals (L13) | 1,120 | `max_calls` 16 per screened candidate |
| low | flow (L20) | 900 | <= 15 per PASS decision, hard cap `max_calls_per_hour` 900 |
| low | sell_evidence (L20) | 400 | <= 9 per probe, hard cap 400/h (15 positions: cache 60 s per pool) |
| low | bot_exit (L23) | 2,160 | `max_calls_per_pass` 6 every 10 s whenever >= 1-2 positions are held (cap, independent of 15) |
| low | clusters (L24) | 1,400 | `max_calls` 20 per screened candidate |
| low | **total** | **8,410 (2.3 req/s, 26 % of 9/s)** | with paths at `max_paths` 2000: 15,850 (4.4 req/s, 49 %) |
| main | screening | 210 | 2 getMultipleAccounts + 1 getTokenLargestAccounts per candidate (more on transient retries, <= 3) |
| main | watchlist re-screens (L14) | 360 | hard cap |
| main | honeypot_sim (L22) | 150 | 30 simulated candidates/h x <= 5 (plain pump SPL mints: 0) |
| main | **total** | **720 (0.2 req/s of 7/s)** | |
| exit | marks | 360 | one getMultipleAccounts per 10 s pass (<= 50 positions) |
| none | risk_external (L21) | 0 Helius | RugCheck only, own bucket <= 1 req/s (~70-280 calls/h), 0 credits |

Monthly credits (30 days, 1 credit per call except getAsset / getProgramAccounts 10; none of the listed features calls either):

| scenario | credits/h | credits/month | vs 10M plan | vs 9M budget |
|---|---|---|---|---|
| worst case above (paths steady at 420) | 9,490 | **6.83M** | 68 % | under, no shedding |
| paths at `max_paths` 2000 | 16,930 | 12.19M | 122 % | L15 sheds the low lane once the 24 h projection passes 9M; screening, honeypot_sim and exits continue |

Not in the table: the separate notify unit's cached DAS getAsset (10 credits, one per new mint name) and Jupiter (rate limited, not credit billed).

## Production config check

The live config is the r1 example plus `strategy_config` (absolute), `initial_cash_sol` "100", `screen.min_age_seconds` 60,
`ops.credit_budget_month` null and `wallet_signals.window_s` 60. `test_lint4.ProductionConfig` builds exactly that from the r1 example's
byte copy (`tests/lean/lean.r1-live.json`) and proves: it loads unchanged; `sell_evidence`, `flow`, `honeypot_sim`, `risk_external`
disabled, `bot_exit` and `clusters` None; lanes stay 0.55 / 0.25 / 0.20 of 10/s (5.5 / 2.5 / 2); only features and wallet_signals threads
start; health shows every new block disabled. One accounting-only difference: the credit tracker now prices DAS `getAsset` at 10 (from
`DEFAULT_COSTS`) instead of 1; with the budget null nothing is shed.

### Enable-list (add to the live lean.json; values exactly as in `config/lean/lean.example.json`)

1. `"lanes"`: `{"helius_rps": 20, "main": 0.35, "exit": 0.2, "low": 0.45, "low_shed_s": 30}` (replaces the r1 block).
2. `"ops"`: `"credit_budget_month": 9000000` and `"credit_costs": {"helius": {"default": 1, "getProgramAccounts": 10, "getAsset": 10,
   "getTransactionsForAddress": 100}, "rugcheck": {"default": 0}}` (other ops keys unchanged).
3. `"sell_evidence"`: the example block (`enabled` true, `unverified_sell_policy` **"flag_only"**, `max_tx_samples` 8, cap 400/h, ...). Needs `execution` (on in r1).
4. `"flow"`: the example block (`enabled` true, `window_s` 180, `max_txs` 12, cap 900/h, ...).
5. `"honeypot_sim"`: `{"enabled": true, "max_per_hour": 30, "full_size": true}`.
6. `"bot_exit"`: the example block, **`"mode": "shadow"`**, with the four rules (PUMPERS_OUT incl. `tx_rate_ratio < 0.6`).
7. `"clusters"`: the example block (`top_n` 20, `max_calls` 20, `reserve_tokens` 1, ...).
8. `"risk_external"`: the example block (`enabled` true, `rate_per_s` 1.0, `max_late_s` 30, ...).

`test_lint4.ProductionConfig.test_adding_exactly_the_enable_list_turns_everything_on` adds exactly these to the live config: everything
builds, lanes are 7 / 4 / 9, bot_exit is shadow (no trigger), the budget is 9,000,000.

## Recommendations / limitations (not changed here)

* **L22 is not verified live.** Run one `--once` on a known Token-2022 mint and inspect the `honeypot_sim` observation before trusting
  `NO_BUILD`/`INCONCLUSIVE` counts (they only reject risky mints).
* **L23 at 15 positions:** 6 calls per 10 s pass cannot sample 15 pools inside a 30 s window. With the 9/s low lane there is room for
  `max_calls_per_pass` ~18 (6,480 calls/h, still < 50 % of the lane with everything else). Keep shadow until the report shows savings.
* **L20 code default** for `unverified_sell_policy` is still `zero` (only the example says `flag_only`): an operator who writes
  `{"enabled": true}` alone gets zeroing. Always copy the example block.
* **L20 probe threads** are per-pool daemon threads outside the shared join (bounded by held positions, `probe_timeout_s` 20, `stop`
  checked before each call); on stop they are abandoned like any daemon, never joined.
* `getTransactionsForAddress` = 100 is a conservative estimate for a method lean/ does not call; calibrate every cost against the
  Helius dashboard (the tracker says `costs_are_estimates`).
* RugCheck public limits are undocumented; watch `risk_external.http_429` / `backoff` in health.
