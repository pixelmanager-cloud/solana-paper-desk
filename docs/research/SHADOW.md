# Shadow strategy variants (T27, reviewed in T27F)

`python -m tools.research.shadow_strategies --store counterfactual.sqlite --grid config/experiments/shadow/exit-grid.json [--holdout-from UTC] [--features f.json] [--min-trades 30] [--no-outcomes] [--include-truncated] [--json]`
`python -m tools.research.shadow_strategies --parity-ledger ledger.sqlite`

Evaluate many parameter sets on the same candidate stream **without a single provider request**. Everything is labelled `SIMULATED_SHADOW`; it is not trading evidence and never writes to a ledger.

## How a variant is evaluated
Each variant is a versioned override of `config/paper.json` (`shadow-grid-1` files: explicit `variants` and/or a cross-product `grid`; unknown keys or changed types are rejected). For every T26 candidate the priced samples (+5m ... +6h) become market events carrying the sampled PumpSwap reserves, and the REAL `desk.engine.transition` decides entries and exits (gates, sizing, stop, ladder, trailing, time-stop, max-hold). Costs are the engine's: pool fee (assumption, default 25 bps), 50 bps adverse slippage and the fixed fee. Each candidate runs in an isolated in-memory state, first entry only, so trades are comparable across variants. Events carry provenance `SYNTHETIC_TEST_ONLY` because the engine refuses unverified provenance outside quote mode; nothing here is written anywhere. Candidate ages are **seconds since the migration hint receipt** (the counterfactual store records the discovery receive time, not the on-chain block time).

## The nominal run and the bracket
The store holds a handful of samples per candidate, so the order of events inside a window is unknown. Two things are computed for every entered candidate:
- **Nominal run** (`simulate`): the engine sees only the sampled instants, strictly causally. It is ONE possible history, not a bound.
- **Bracket** (`bracket_trade`): the minimum and maximum PnL over every history consistent with the samples under one explicit, printed assumption:

> Between two consecutive samples the live engine may observe up to `max_intra_marks` (default 3) extra marks, in any order and at any time, each at a pool price (quote reserve per base unit) inside `[lo, hi]`, with `lo = (1 - excursion_fraction) * min(price_a, price_b)` and `hi = (1 + excursion_fraction) * max(price_a, price_b)` (default 0.30). A gap that contains a POOL_DEAD sample, or that ends at the pool's death, has `lo = 0`. Marks use the pool depth of the earlier sample and fill at the observed price. Nothing else is assumed: no monotone or linear path.

Inside a gap the engine only changes behaviour at its own thresholds, so the enumeration marks exactly the prices where they trigger (plus the extremes `lo`/`hi`): the current stop level, the +15% touch that cancels the time-stop (and the highest price at which a time-stop still fills), the next ladder rung, the trailing level (which depends on the peak), and the time-stop / max-hold deadlines. That covers **a stop crossed and recovered between samples**, **several rungs hit inside one gap** (the engine sells one rung per observed mark, so a coarse gap can hide several; the nominal run sells them one per sample) and **an unknown peak for the trailing level**. A dynamic program over the resulting real-engine states returns the true minimum and maximum (`pnl_lo`, `pnl_hi`) together with the shortest mark sequence that reaches each (`worst_path`, `best_path`). A trade whose bounds differ is **AMBIGUOUS**; the share of ambiguous trades is reported per variant and part. With `max_intra_marks = 0` the bracket collapses to the nominal run. The nominal run must lie inside the bracket (checked on every trade; a violation is an error, not a warning).

How it is verified without sharing code with the DP: known answers for the three review scenarios; an independent sequential engine run that replays each `worst_path`/`best_path` and must reproduce the bounds; and a property test (plus an offline stress run of ~60,000 random schedules over random-walk paths, excursions 0 to 0.5, 1 to 3 marks, five configs, with and without pool death) in which no random intra-gap schedule may leave the bracket. The property test found one real hole during development: a time-stop can only fill while the +15% touch has not happened, so the best time-stop fill is just below that touch (the `TIMECAP` mark).

Consequences to read the output with:
- The bracket is **wide by construction** when gaps are long (minutes to hours): `lo` fills at the lowest allowed price. Many trades will be AMBIGUOUS; that is the honest answer for 5-minute-to-6-hour sampling. Use `excursion_fraction` and `max_intra_marks` as explicit sensitivity knobs; the nominal mean is printed next to the bounds (`nominal-mean`) and is clearly not a bound.
- The bracket is conditioned on BOTH ends of every gap on purpose: it describes histories consistent with the data. Every engine decision inside one history sees only that history's marks. The nominal run never reads a later sample.
- Time exits: the live monitor is assumed to keep marks fresh within `price_ttl_seconds`. The earlier `carry` model fed the last sample at a deadline as if it were fresh and could sidestep that time-to-live; it has been removed. Time exits inside a gap are bracketed at unknown prices instead and make the trade AMBIGUOUS.
- A truncated enumeration (event budget `MAX_ENGINE_EVENTS` per candidate) is reported (`bracket_truncated`) and excluded, never guessed.
- Cost: roughly 0.05 to 1 s per candidate and variant at 3 marks (more with long, volatile paths); lower `max_intra_marks` for big grids.

## Selection effects: nothing is dropped silently
A candidate must have a complete path (all six horizons recorded) and an OK-priced **+5m sample** to enter; otherwise it is excluded and counted. Exclusion stages, in the order applied, with the remaining count after each (`selection.funnel`): `no_sample_rows` -> `truncated_paths` (path not yet complete; `--include-truncated` evaluates them anyway, liquidated at the last sample and counted in `truncated_included`) -> `dead_before_baseline` (POOL_DEAD at +5m) -> `no_priced_sample` -> `baseline_not_priced` (+5m FAILED/MISSED). Evaluated candidates also report `failed_or_missed_gaps`, `transient_death`, `terminal_death`; trades closed by the horizon-end liquidation are counted per variant (`horizon_end_trades`) and entries the engine rejected are counted by reason (`entry_rejects`).

**POOL_DEAD.** A death is terminal only if no priced sample follows it. A closed vault that later reappears is a gap (its gap has `lo = 0`), never a -100% (this follows the counterfactual store's rule: dead = -100% only until a priced sample appears). A terminal death brackets the unknown death time: the position is lost in full at the worst case, while the engine could have stopped out, hit a time-stop deadline or sold a rung before the pool died (the best case, with `lo = 0` over the dying gap).

## Output and ranking
Per variant and per part (train / holdout): trades, ambiguous trades and share, net PnL interval (SOL), win-rate interval, mean-return interval, max-drawdown interval, the nominal mean return, bootstrap CI (`alpha = 0.05 / variants`, Bonferroni; seeded and deterministic) of the lower and upper return series, the engine's entry rejects, truncated brackets, horizon-end trades, and (unless `--no-outcomes`) the same figures split by the T26F classification of each candidate (`BOUGHT`, `REJECTED:<stage>:<primary code>`, `NOT_DISPATCHED`, `UNKNOWN`). The split is by migration time (default: the latest 30% of candidates, or `--holdout-from`).

**Ranking is holdout-only, needs at least 30 holdout trades (`--min-trades`), and orders by the lower bound of the Bonferroni-corrected bootstrap CI of the mean return over the worst-case bounds.** Variants with fewer trades are listed with `UNRANKED_FEWER_THAN_30_HOLDOUT_TRADES`. The bootstrap uses `max(4000, 40/alpha)` resamples (capped at 100,000) and interpolates between order statistics, so many variants cannot collapse the interval to its smallest resample. Train rows are shown but never used to pick.

## Signal features and look-ahead
Price paths carry no flow/holder/wash features, so by default every candidate gets the same **neutral features** (`neutral_features` in the output; also the explicit assumptions `sol_usd` 150, supply 1e9 tokens, 6 decimals, pool fee 25 bps). `--features f.json` takes `{mint: {"as_of": epoch, <event feature overrides>}}`: every entry must say when it became known, must not be observed after its price path ends, and may only contain known feature names. Such a candidate's features are used only from `as_of` on; entry decisions before it are refused (counted as `FEATURES_NOT_YET_KNOWN`), so a feature observed after a decision never influences it.

## Parity with the live engine
`--parity-ledger L` replays the RECORDED events of a (strict-mode) ledger through the shadow's own engine path with the ledger's saved config and compares every decision with the outcomes the live writer recorded (exit 3 on any mismatch). Quote-execution ledgers need their recorded quotes and are refused. The test builds a ledger with the real `Ledger.apply`, so the comparison is not circular.

## What this cannot tell you
- Signal quality (see features above) and portfolio effects (max positions, exposure, cash): each trade is independent.
- The take-profit ladder is fixed in `desk.engine.manage_position`, not a config key, so it is not a grid axis (its trigger ratios are pinned to the engine by a test).
- Market cap uses `token_supply_tokens` x spot price x `sol_usd`; those are explicit assumptions in the grid file.
- T14 latency drift is not applied.
- The bracket is only as good as its stated assumption (excursion and mark count): a path that moves further than `excursion_fraction` between samples, or a live engine that observes more marks than `max_intra_marks`, is outside it.
