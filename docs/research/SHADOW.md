# Shadow strategy variants (T27)

`python -m tools.research.shadow_strategies --store counterfactual.sqlite --grid config/experiments/shadow/exit-grid.json [--holdout-from UTC] [--features f.json] [--json]`

Evaluate many parameter sets on the same candidate stream **without a single provider request**. Everything is labelled `SIMULATED_SHADOW`; it is not trading evidence and never touches a ledger.

## How a variant is evaluated
Each variant is a versioned override of `config/paper.json` (`shadow-grid-1` files: explicit `variants` and/or a cross-product `grid`; unknown keys or changed types are rejected). For every T26 candidate the priced samples (+5m ... +6h) become market events carrying the sampled PumpSwap reserves, and the REAL `desk.engine.transition` decides entries and exits (gates, sizing, stop, ladder, trailing, time-stop, max-hold). Costs are the engine's: pool fee (assumption, default 25 bps), 50 bps adverse slippage and the fixed fee. Each candidate runs in an isolated in-memory state, first entry only, so trades are comparable across variants. Events carry provenance `SYNTHETIC_TEST_ONLY` because the engine refuses unverified provenance outside quote mode; nothing here is written anywhere.

## Coarse paths, honestly
Only a few samples exist per candidate. The simulation is strictly causal (a decision never reads a later sample) and brackets what it cannot know:
- `samples`: the engine sees only the sampled instants; an exit fills at the first sampled price after the trigger (late, gap risk) — the worst case.
- `carry`: the engine is also called at the time-stop / max-hold deadline when it falls inside a window, using the last sample before it.
- STOP / TRAILING_STOP: the crossing may have happened earlier in the window; the best case fills exactly at the level the engine tested (analytic bound), the worst case at the sampled price.
- A pool that died while the position was open loses the remaining cost.
Every statistic is an interval `[worst, best]`; a trade whose bounds differ is **AMBIGUOUS** and counted.

## Output
Per variant and per part (train / holdout): trades, ambiguous trades, net PnL interval (SOL), win-rate interval, mean-return interval, max-drawdown interval, bootstrap CI of the mean return (`alpha = 0.05 / variants`, Bonferroni; seeded and deterministic) and the engine's entry-reject counts. The split is by migration time (default: the latest 30% of candidates, or `--holdout-from`). **Ranking is holdout-only, by the lower bound of the mean-return interval**; the train rows are shown but never used to pick, and the number of variants tried is printed with the correction.

## What this cannot tell you
- Signal quality: the price paths carry no flow/holder/wash features, so by default every candidate gets the same neutral features and variants differ only in structure (windows, stop, trailing, time-stop, max-hold, sizing). `--features` accepts real decision evidence per mint (read-only) to override fields.
- The take-profit ladder is fixed in `desk.engine.manage_position`, not a config key, so it is not a grid axis.
- market cap uses `token_supply_tokens` x spot price x `sol_usd`; `sol_usd`, decimals and pool fee are explicit assumptions in the grid file.
- No portfolio effects (max positions, exposure, cash): each trade is independent.
- T14 latency drift is not applied (no data on this branch).
