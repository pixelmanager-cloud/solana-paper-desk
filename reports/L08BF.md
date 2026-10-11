# L08BF — Fix L08B (replay / tune)

BASE: `origin/integration/r1` with `cloud/L07R2` (the recorder now writes its own `paths.sqlite`) and the old `cloud/L08B` merged in. Only
`lean/replay.py`, `lean/tune.py` and their three test files changed; `desk/` and the runner are untouched. Paper only, no network, fixtures only.

## Per review item
1. **Replay marks exactly like live (HIGH).** Input is now the recorder's `paths.sqlite` (`paths` + `path_marks`, one or several files, oldest first). A mark
   is the stored `base_raw` / `quote_raw`; the position is marked with `adapters.mark(qty_raw, base_raw, quote_raw, pool_fee_bps, pcfg)` — the function the live
   position loop uses — with no extra 50 bps haircut, and no reserve is rebuilt from a price or a liquidity figure. A mark that nets nothing after the fees is 0
   (the live `max(0, net)`), which is the unsellable case, not a crash. Buy and sell fills are integer constant-product quotes on the same reserves
   (`quote_buy` / `quote_sell`, pool fee first), then the paper fill applies the strategy's slippage. The old test that locked in the divergence (a
   hand-computed `BASE_PRICE`-based oracle with a double haircut) is replaced by independent integer oracles written in the test.
2. **Fill at the next mark; band at entry time.** `pullback` / `momentum` decide at the trigger mark and fill at the NEXT recorded mark (`ENTRY_MARK_DEAD`
   if that mark is dead, `ENTRY_NO_NEXT_MARK` if the path ends first); `first_mark` fills at the mark. The market-cap / liquidity band is judged on features
   recomputed AT the entry mark (`features_at`: price from the effective quote reserve, liquidity from the spendable one, market cap = supply × price × SOL/USD,
   with the screen's `fee_raw`, virtual reserves, supply and SOL/USD) — exactly the formulas `lean.candidates` uses. A pool that cannot be priced is
   `POOL_STATE_UNPRICEABLE`.
3. **Select in-sample, confirm out-of-sample.** `split_paths` makes train / select / confirm by start time (default 50/25/25, optional embargo on both
   boundaries). Configs are ranked on the SELECT window only; the winner is chosen there. The CONFIRM window can only accept or reject it: a proposal needs
   select improvement over the base AND a profitable select mean, then on confirm ≥ `min_confirm` (30) trades, a positive mean and a total above the base's.
   Statuses: `NO_SUFFICIENT_ALTERNATIVE`, `BASE_INSUFFICIENT`, `NO_SELECTION_IMPROVEMENT`, `NOT_CONFIRMED`, `WOULD_PROPOSE_NO_DIRECTORY_GIVEN`, `WRITTEN`.
   `min_select` default 50. Every entry carries `confirm_used_for_selection: false`. Proposals are still never applied and never overwritten. Also fixed on the
   way: NaN fractions passed the validation.
4. **Calibration on real recorder output.** `tests/lean/test_e2e_replay.py` runs the real runner AND the real L07R2 recorder (15 s marks, own `paths.sqlite`) on
   8 fake tokens, then `calibrate` (live fills/decisions from `lean.sqlite`, paths from `paths.sqlite`) must reproduce all 7 traded tokens' ordered exit reasons.
   The `edge` token sits 0.2% above the stop line (net mark): the live runner holds it to `TIME_STOP`; replay with a 50 bps-haircut mark turns exactly that row
   into a `MISMATCH` (`STOP` vs `TIME_STOP`). Other e2e checks: no `path_*` rows in the trader store, the recorder's first mark equals the pool's vault amounts,
   pullback fills one mark after the dip, momentum one mark after its trigger, the band is judged at the entry mark (75k market cap at the screen, 60k at the
   pullback fill, floor 70k → `MARKET_CAP`), tune is 3-way and both databases are byte-identical afterwards.
5. **`test_database_is_not_modified` tolerates `-wal` / `-shm`.** A plain `mode=ro` open of a WAL database may recreate those sidecars (`lean.report.open_ro`).
   The test compares the sha256 of the data files and allows only optional `-wal` / `-shm` extras (a `-wal` must be empty); nothing else may appear.

## Tests / evidence
```
python -m unittest discover -s tests/lean -t . -q     (ONE process, on top of integration/r1 + L07R2)   -> Ran 501 tests OK
python -m unittest tests.lean.test_replay tests.lean.test_tune tests.lean.test_e2e_replay   -> Ran 132 tests OK
```
(replay 74, tune 46, e2e 12.) Fail-first: the new test files on the old L08B code (same base) → `FAILED (failures=1, errors=111)` of 130 (`/tmp/l08bf/red.txt`;
most are the API change, the behavioural ones are the mark, fill-timing, band and selection tests). Mutations (single replacement, the three test modules): 30
mutants, 30 killed (`/tmp/l08bf/mutate.py`): extra mark haircut, no price impact, spendable / effective reserve mistakes, screen-time band, trigger-mark fill,
dead fill mark, pool fee missing in buy / sell, corrupt reserves / duplicate start / pre-start marks / duplicate timestamps / incomplete features accepted,
dead marks dropped, wrong sell order, ranking or improvement judged on confirm, each confirm gate removed, `min_select` off by one, overlapping windows, no
embargo on confirm, NaN fractions, unsorted split, proposal overwrite. Two survived the first set (confirm mean not required positive; the inclusive
`min_select` boundary) and got tests.

## Limitations
- Replay is a SIMULATION on synthesised constant-product quotes from recorded vault amounts (`EXECUTION_UNVERIFIED`); real routes and priority fees are not
  modelled. The pool fee is an assumption (`ReplayConfig.pool_fee_bps`).
- Marks are sampled every `interval_s` (15 s) while the live loop manages at its own cadence: a stop that live saw between two recorded marks is not
  reproduced. Dead (`ACCOUNT_MISSING` / `EMPTY_POOL`) marks write the position off at its last mark like D2's `RUG_WRITEOFF`.
- With a handful of configs per grid the select winner is optimistic (selection bias); the confirm window is a single look, not a guarantee.

## For the coordinator
- **CLI changed:** `python -m lean.tune --db lean.sqlite --paths-db paths.sqlite [--paths-db archive ...] --grid ... --out-dir ... --now ...`; `--fractions`
  (default `0.5,0.25,0.25`), `--min-select`, `--min-confirm` replace `--split` / `--min-oos`. `ReplayConfig.decimals` is gone (decimals come from the recorded
  target). `load_paths` / `calibrate` take recorder connections.
- **Pool fee default is 30 bps in replay but 25 in `lean.json`.** Pass `--pool-fee-bps 25` (or the config's value) for calibration against the live runner;
  I did not change the default because the existing tests and reports state 30. Say if you want it aligned to the runner config.
- The 50 / 30 trade minimums need a few hundred recorded paths per window to be reachable; with fewer, the tool reports `INSUFFICIENT` and writes no proposal.
- L13 `path_outcome` and any other consumer that read `path_mark` observations from `lean.sqlite` must read `paths.sqlite` now (recorder moved in L07R2).
