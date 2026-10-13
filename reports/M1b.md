# M1b: fixes from the M1 pre-deploy review (DEPLOY-WITH-FIXES)

Branch `cloud/M1b`, cut from `origin/integration/r1` (26e9c52, which has M1 merged). Paper only: `desk/` untouched, no network in any
test, no key printed.

**Suite:** `TMPDIR=/private/tmp/ .venv/bin/python -m unittest discover -s tests/lean -t .` in ONE process gives **1216 tests, OK
(skipped=1), rc=0**. That count is after merging M2. The 16 new tests are in `tests/lean/test_m1b.py`.

**Fail-first:** all 14 of the original `test_m1b` tests fail or error on 26e9c52. I checked this by running the file against a
`git archive` of that commit. The review's repro failed exactly as reported: the STOP filled at **+250 s instead of +200 s**. Two more
tests were added for mutants (a STOP with a worthless quote; HOLD resets a take-profit back-off).

**Mutations:** `tests/lean/m1_mutate.py` (now runs `test_m1` + `test_m1b`) kills **57/57**. That is the 37 from M1, 4 of which had
patterns moved by these fixes and were updated, plus 20 new `b*` mutants.

## Fixes

### 1. MED-HIGH: the exit back-off delayed real STOP / DANGER exits

**Rule** (`runner._backoff_active` / `_exit_attempt_failed`):

- A **STOP / DANGER / LIQUIDATE / MAX_HOLD / TIME_STOP / BOT_EXIT** exit is never backed off. The only exceptions:
  - the last quote proved the position worthless (`STUCK_POSITION`, recorded as `stuck`);
  - the mark is zero.

  Such an exit is quoted every pass, so a returning route is used at once.
- Non-priority exits (TAKE_PROFIT, TRAILING_STOP) keep the 10, 20, 40… capped back-off.
- **A change of the wanted exit reason resets the back-off.** A HOLD, a rung or a close still reset it too.
- The D2 write-off clock is unchanged.

**Tests:**

| test | result |
|---|---|
| the review's repro (TAKE_PROFIT without a route, then STOP with the route back) | fills at **+200 s** |
| a STOP without a route | quoted every pass, fills when the route returns, 0 skips |
| a STOP whose quote keeps 0.01 % of the output (mark still ~0.5) | backs off: <= 16 quotes in 600 s instead of 55 |
| a take-profit that is held, then wanted again with the route back | quoted at once |
| unit tests on reason / stuck / zero mark / reason reset | pass |
| the M1 dust-pool test (zero mark) | still 24 BLOCKED rows / 122 quotes |

**Trade-off:** a STOP / DANGER exit with no route and a non-zero mark now writes an exit quote attempt (and its error row) every
10 s pass until the route returns or D2 writes it off. That is the price of never delaying the exit.

### 2. LOW-MED: race on the shared MarkModel

- `MarkModel.schedule(now)` accepts a schedule stamped up to `SCHEDULE_SKEW_S` (60 s) after `now`. The paths thread stamps float
  seconds and the runner asks with an int `mark_at`.
- An arbitrary future stamp is still refused.
- Test: a schedule stamped at 100.7 s is fresh for a mark at 100 s and gives `fee_source: dynamic` (the M1 code fell back to 120 bps).

### 3. LOW: a stale or wrong schedule was used silently

**(a) Stale schedule.** A FeeConfig read that is missing or undecodable sets `schedule_bad_since`. The last good schedule is still used
until `schedule_ttl_s`, but every such mark carries **`schedule_stale: true`**, and health shows `schedule_stale_marks` and
`schedule_bad_since`. A good read clears it, whether through the unchanged-bytes path or the decode path (both tested).

**(b) Implausible supply.** No fee tier is picked from an implausible supply. A mark falls back to `pool_fee_bps` with
**`supply_invalid: true`** (and `fee_fallback: true`) when either holds:

- base reserve > supply;
- supply outside 1e6–1e12 whole tokens for the mint's decimals.

`pool_info` now records `decimals` from the screen.

### 4. LOW: risk_external said the body was kept when it was not

- `Store.keeps_raw(kind)` reports whether `add_observation` keeps the body.
- `risk_external` records `raw.kept` from it; it was always `True`.
- Under `errors_only` it is now `false`, and the `raw_kept` counter agrees. The sha256 and size are always recorded.

### 5. MED: learning data mixed models

- **Vault rows are valued at their own recorded fee** (`path_marks.fee_bps`, or 25 for pre-M1 NULL rows). The current `lean.json`
  `pool_fee_bps` is never used for them. This applies to both `labels.mark_lamports` and `sell_quote_lamports`. Points carry `model`.
- **Mixed paths are never labelled.** A path whose marks after the decision mix vault and effective rows is labelled
  `INSUFFICIENT_PATH` with reason **`MIXED_MARK_MODELS`**, so the learner never uses it.
- **The learner defaults to the store's latest run.** Its default filter is the newest `start` event's `lean_config_hash`, which means
  one `lean.json` and so one mark model. `--all-configs` opts out; `--config-hash H` and `--all-configs` exclude each other; a pre-M1
  store with no hash is read whole. The output line includes the hash used.
- The deployed `desk-lean-learn.service` passes neither flag, so it now learns from the current run only.
- Tests: own-fee labelling (lean.json at 120, rows at 25 or NULL), the mixed-path refusal, and `learn.main` defaults / `--all-configs` /
  `--config-hash` on a two-run store.

### 6. OPS: the mark model is pinned per position

**What is written at open.** Every open state now carries `pool_info.model` and `pool_info.pool_fee_bps`: the configured model and
vault fee at the open, plus the screen's pool fields. `pool_info` is now written under both models.

**How marks follow it.**

- `_read_marks` marks each position with its pinned model and fee. Keys are chunked per position: 2 or 3 accounts, plus the FeeConfig
  when any position in the chunk is effective.
- A mark under a model other than the configured one carries `pinned_from_open: true`.
- Positions opened before M1b:
  - no `pool_info`: vault, at the configured fee when the runner is configured vault, else at 25 (`LEGACY_VAULT_FEE_BPS`);
  - M1's effective `pool_info` without `model`: effective.

**Tests:**

| switch | result |
|---|---|
| effective → vault rollback | the open boosted-pool position stays effective (ratio 0.95–1.0, no STOP; the vault mark would read about 0.80) |
| vault → effective | the open position keeps the vault mark at 25 bps, identical to `A.mark(..., 25)` |
| unpinned positions | unit-tested |

## Mutation scripts on the final tree

| script | killed |
|---|---|
| m1 | 57/57 (after the M2 merge) |
| m2 | 25/25 (after the M2 merge) |
| lint3 | 32/32 (after the M2 merge) |
| l23 | 28/28 (before the M2 merge, no file of theirs changed by it) |
| lint4 | 13/13 (before the M2 merge, no file of theirs changed by it) |
| l21 | 29/29 (before the M2 merge, no file of theirs changed by it) |

On lint3, the pattern of its "mark without the pool fee" mutant moved with fix 5. It was updated in `tests/lean/lint3_mutate.py`
and is killed.

## Merge with M2

`origin/integration/r1` at f6ffc71 (M2: learner statistics) is merged. The only conflict was in `lean/learn.py` `main()`; the
resolution keeps all of M2's statistics. `latest_config_hash` / `--all-configs` and the filtering in `build_dataset` sit on top of
M2's code. The vault-row fee and the mixed-model refusal live in `lean/labels.py`, which M2 calls unchanged
(`label_path` / `simulate_exits`).

**Suite after the merge:** 1216 tests, OK (skipped=1), rc=0.
