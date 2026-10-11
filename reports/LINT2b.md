# LINT2b: pre-deploy fixes for L12F (entry features) and L13 (wallet signals)

Branch `cloud/LINT2b`, cut from `origin/integration/r1` (4421edf, LINT2 merged). `desk/` is untouched. Paper only.

The full lean suite ran in one process (`python -m unittest discover -s tests/lean -t .`, `TMPDIR=/private/tmp/`) and passed:
**829 tests, rc=0** (1 pre-existing skip).

The fixes were checked two ways:
- **On the r1 code:** 23 of the 24 new LINT2b tests fail there. The 24th, "4 mints is not a hub", is a negative case.
- **Mutations:** 20 of 20 mutants were KILLED.

## Review items

### 1. MED shutdown

**Problem:** L13 checked `stop` only after a LANE_SHED. A running collection finished its ~12 calls, and the three joins ran one
after another, up to 30 s each.

**Fix:**
- `_Calls.check` raises `_Cap('STOPPED')` before every request, including inside a paced wait. A stopped collection writes NO
  row, no `wallet_buy` and no funding events, and it is collected in full on the next start.
- `features.enrich` checks `stop` before every try, and the rest of its fields are recorded as `STOPPED`.
- On stop, the feature thread drains its queue with free fields only, sending no request.
- `Runner.run` joins the path recorder, features and wallet_signals threads with ONE shared deadline, `AUX_JOIN_S` = 10 s in total.

**Tests:**
- a stop during the 3rd request: nothing more is sent and no row is written;
- a stop ends a paced wait;
- two threads that ignore stop are joined in under one deadline;
- the feature drain sends nothing.

### 2. MED lookahead

L13 rows:
- Every row has:
  - `decision_ts`: the FIRST PASS/REJECT screen decision;
  - `collected_at`;
  - `late_s`.
- The swap window ends at `min(graduation + window_s, decision_ts)` by block time. A window signature with no block time is
  skipped and counted.
- The row records `window_cut_by_decision` and `window_s_effective`. `window_after_decision_s` is 0 when the decision is known
  and null when it is not.
- The supply is the one the screen read (`supply_source: screen`). It falls back to a call flagged `rpc_after_decision`.
- `smart_buyers_count` counts only buys and outcomes RECORDED before `decision_ts`. `smart_asof_ts` is set to `decision_ts`.
- The backfill cutoff is `migrated_at`, not `c.ts`.

L12F rows (`features_version` 3):
- `screen_at` is the stored screen decision.
- `decision_ts` is the last entry decision after the screen, else the screen. The row also has `late_s` and `flags`.
- `tx_count_first_10m` ends at the decision: `flags.tx_count_first_10m = CUT_AT_DECISION`, plus `tx_count_window_s`.
- The dev balance and the soft-reject holder list are snapshots read at collection (`AT_COLLECTION`). They are not read at all
  when the row is later than `max_late_s` (30 s) after the decision.

**Tests that fail on post-decision data:**
- `test_LINT2b_no_post_decision_data_in_the_row` (L13): a swap after the decision, inside `window_s`, is never fetched or counted,
  and no supply call is made.
- `test_LINT2b_no_post_decision_transaction_is_counted` (L12F).
- `test_LINT2b_an_outcome_resolved_after_the_decision_is_never_counted`.
- The e2e check that every fetched swap and funding transaction is at or before the row's `decision_ts`.

**The shipped e2e changes accordingly:**
- `ok_1` now counts 2 transactions, not 3. The one at 599 s is after its entry decision, and the row is flagged.
- The smart-wallet unit test now decides the late launch after the outcomes were resolved.

### 3. MED bundle poisoning
- The row stores `funders`, the buyer -> funder map, and `hub_funders`.
- Every found source is persisted as a `wallet_funding` event.
- A funder of buyers of at least `hub_min_mints` (5) distinct mints within `hub_window_s` (24 h) before the decision is a HUB. A
  hub never forms a cluster; the same-slot evidence is kept. A newly detected hub is persisted once as a `wallet_hub` event.
- No exchange address is hard-coded.
- `bundle_score_basis` is `same_slot_only` unless at least one funding source was found.

### 4. Low-lane starvation

**Changes:**
- L13 `pace_s` is 0.5 (default and example).
- L13 now only calls while at least 2 tokens are available, through `reserve_tokens: 1` and `LowLaneLimiter.available()`, a
  read-only peek in `providers.py`.
- An L11 account check that is shed is retried ONCE on the next pass (`account_retries` counter), instead of waiting 6 passes.

**Simulation** (`tests/lean/lint2b_starvation.py`, run with `python -m tests.lean.lint2b_starvation`):
- It uses the REAL `LowLaneLimiter` (2 req/s, burst 2, 30 s shed) on a fake clock, with discrete-event models of the path recorder
  (`_poll`/`_call` logic), L11, L12F and L13.
- Load: 70 candidates/h (Poisson), a 429 about every 20 min, 5 seeds x 24 h each.

Path recorder at 0.6 req/s (9 calls per 15 s):

| scenario | path gap % | L11 sheds | L11 missed cycles | L11 retries | L12F rows shed % | L13 stopped % | low-lane req/s |
|---|---|---|---|---|---|---|---|
| r1 without L13 | 2.626 | 1077 | 1077 | 0 | 66.41 | - | 0.614 |
| r1 with L13 (pace 0.25, no reserve) | **4.842** | 1679 | **1678** | 0 | 72.97 | 3.16 | 0.877 |
| LINT2b without L13 | 2.628 | 1290 | 186 | 1103 | 2.64 | - | 0.627 |
| **LINT2b with L13 (pace 0.5, reserve 1)** | **2.630** | 1309 | **181** | 1127 | 2.68 | 1.03 | 0.914 |

Path recorder at 0.93 req/s (14 calls per poll, stress):

| scenario | path gap % | L11 missed | L12F shed % | L13 stopped % |
|---|---|---|---|---|
| r1 without L13 | 2.738 | 1542 | 73.96 | - |
| r1 with L13 | **8.000** | 2138 | 79.71 | 3.28 |
| LINT2b without L13 | 2.742 | 338 | 2.64 | - |
| LINT2b with L13 | 2.747 | 394 | 2.68 | 1.37 |

**20-seed check** (mean ± sd per 24 h):

| path load | without L13: gaps | without L13: L11 missed | with L13: gaps | with L13: L11 missed |
|---|---|---|---|---|
| 0.6 req/s | 2.663 ± 0.373 % | 35.4 ± 6.6 | 2.667 ± 0.373 % | 35.9 ± 5.6 |
| 0.93 req/s | 2.770 ± 0.388 % | 71.3 ± 7.4 | 2.775 ± 0.388 % | 77.3 ± 8.4 |

**Reading:**
- **Target met:** with LINT2b, path gaps and L11 misses are the same with and without L13, within noise.
  - At the stated load the difference is ~0.004 points of gaps and ~0.5 missed checks/day.
  - Under the stress load, L11 misses are +6/day, under 1 sd.
- The ~2.6% gap floor is the 429 shed windows themselves (30 s every ~20 min).
- **r1 as merged was worse than the review assumed:**
  - L13 nearly doubled path gaps (2.6 -> 4.8%, and 8% under stress).
  - The L11 account check missed ~75% of its cycles even WITHOUT L13: it collided with the path recorder's bursts and then waited
    6 passes.
  - L12F lost ~66% of its enrichments to a single `no_token` shed.
- The L11 retry (missed 1077 -> 186 per 5 days) and the L12F retry (66% -> 2.6%) fix those. L13 sends ~0.3 req/s and is stopped by
  a shed on ~1% of candidates.
- `test_lint2b.Starvation` asserts these relations on 3 seeds x 12 h.

### 5. L12F retries a LANE_SHED
- A `no_token` shed is retried every 0.5 s for up to `shed_wait_s` (10 s, a config key) per call, in the recorder's own thread,
  holding no lock.
- A shed window (429) is never waited.
- A shed is not counted as a call.
- A stop ends the wait.

### 6. resolve_outcomes
- A hazard reject (no PASS, a REJECT for a non-market reason) that was never bought closes at once as
  `UNKNOWN / NEVER_RESOLVABLE`: L07R2 never records a path for it.
- Unresolved mints are visited ROUND-ROBIN (`resolve_batch` per pass from a rotating cursor), so new tokens are not stuck behind a
  backlog.
- `closed_positions()` is read once per pass, not once per mint.

### 7. Health
- `features` gains `alive` and `queued` (queue depth).
- `wallet_signals` gains `alive`, `pending` (candidates waiting for a row at the last pass) and `unresolved`, along with its
  counters (`stopped`, `yielded`, `never_resolvable`, ...).
- When off, both report `{"enabled": false, "alive": false}`.

## Config
- `config/lean/lean.example.json`:
  - `wallet_signals.pace_s` 0.5;
  - `reserve_tokens` 1;
  - `hub_min_mints` 5;
  - `hub_window_s` 86400;
  - `features.shed_wait_s` 10;
  - `features.max_late_s` 30.
- `window_s` 120 is at most `screen.min_age_seconds` 300; a test guards this.
- **Production (live) config:** `screen.min_age_seconds` is 60. Rows decided before 120 s after graduation will flag
  `window_cut_by_decision` and get a shorter window (`window_s_effective`). That is honest and by design: nothing after the
  decision is used.
- `config/lean/wallet_signals.example.json` and `docs/lean/WALLET_SIGNALS.md` are updated to pace 0.5.
- **The production-config compatibility test still passes** (`test_lint2.ProductionConfig`). The live config loads unchanged with
  both features off. The keys to turn them on are now the example's `features` and `wallet_signals` objects. The new keys are
  optional with these defaults, so the LINT2 objects also still load.

## Mutations (20/20 KILLED)
1. stop not checked
2. a stopped collection still writes its row
3. the window not cut at the decision
4. the smart table not as of the decision
5. a supply call instead of the screen supply
6. the backfill cutoff on the row time
7. hubs ignored
8. a dishonest basis
9. no reserve
10. hazard rejects wait 12 h
11. oldest-first resolution
12. one join deadline per thread
13. health without alive
14. no L11 retry
15. the tx count past the decision
16. L12F gives up on the first shed
17. L12F stop not checked
18. the free drain enriches
19. `screen_at` = handling time
20. a late snapshot read anyway

## Residual
- L12F `dev_holding_pct` and the soft-reject holders are still read a few seconds after the decision. They are labelled
  `AT_COLLECTION`, carry `late_s`, and are dropped past 30 s. There is no cheap historical source for a balance.
- Hub detection needs history: during the first 24 h after deploy few funders reach 5 mints, so exchange-funded clusters can still
  appear. They are visible in `funders`.
- The starvation numbers are a model, not a measurement. Check the live `paths.gaps`, `held_risk.shed` / `account_retries` and
  `wallet_signals.yielded` counters in health after deploy.
