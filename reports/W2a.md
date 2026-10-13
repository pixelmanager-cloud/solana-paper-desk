# W2a: shadow paper arms (A/B test of strategies on the same candidates)

BASE `origin/integration/r1` (26e9c52, includes M1). Branch `cloud/W2a`. PAPER ONLY: nothing signs or sends, `desk/` is untouched, no
network in any test, no key printed.

**Suite:** `TMPDIR=/private/tmp/ .venv/bin/python -m unittest discover -s tests/lean -t .` in ONE process: **1243 tests, OK (skipped=1), rc=0**
(r1 baseline 1181 + 13 in `tests/lean/test_e2e_arms.py` + 49 in `tests/lean/test_arms.py`).
**Mutations:** `tests/lean/w2a_mutate.py` kills **48/48** (pricing, exits, the filter's timing, isolation, lanes, storage, reading, config, the runner hooks, the strategy key, the
loader, `/arms`, the report). The older scripts that patch files W2a touched were re-run on the final tree: m1 37/37, l20 38/38, l21 29/29,
l22 35/35, l23 28/28, l24 34/34, lint3 33/33, lint4 13/13.

## What it is

`lean/arms.py` runs several **shadow arms** next to the main arm. An arm is `{name, strategy_config, entry_filter}`. It has its own virtual book
(cash, equity, positions, cooldowns, loss streak, day baseline), decides entry with its own `strategy.entry_decision` and filter, and exits
with the real `strategy.exit_decision` of its own config. It writes ONLY to its own `arms.sqlite` (append-only, schema v1, `arm_name`,
`strategy_version`, `config_hash`, `code_version` on every row). `lean.sqlite` is never written; it is opened read-only for the entry filters.

**Same candidate stream.** The runner hands every candidate that PASSED the main screen to the arms (`Runner._w2a_*` hooks), whatever the
main portfolio then decided. Hazard rejects of the screen never reach them. A candidate the main arm rejected at the L22 honeypot simulation is
skipped (`MAIN_HAZARD:HONEYPOT_SIM_BLOCKED`); one the main gate refused before the simulation ran is recorded `honeypot: unchecked`.

**Entry pricing (no extra call when the main arm quoted).** The main arm's fill quote (q0 or q1, the worse, as L10 filled) and its sell leg for the
same mint are scaled to the arm's size (linear; refused outside `max_scale` 2x). A candidate the main arm dropped with ENTRY_ABORTED_LATENCY is
skipped. If the main arm did not quote the candidate (its gate refused, it errored, the size is out of range), the candidate costs **one buy
quote on the shared LOW lane, however many arms want it**: the first arm pays, the others scale it (`SHARED_QUOTE`). The sell leg is then
MODELED (the effective mark model on the screen's reserves, like a sell quote against the pool as it is: `SELL_LEG_MODELED`) and the fill is
flagged `LATENCY_MODEL_ABSENT` (one quote, no q0/q1). On a shed the arm skips and records `ARM_SKIPPED_SHED`.

**Exits.** Marks: the raw accounts of the main marks read are captured and each arm values ITS OWN quantity with `MarkModel.value_accounts`
(so M1's effective marks apply unchanged, zero calls). A mint only an arm holds is read in one batched `getMultipleAccounts` on the LOW lane; a
position that still has no mark younger than `stale_mark_s` is valued by a low-lane sell quote (D1). The arm never calls the exit lane or the
main lane (`test_the_arms_use_only_the_low_lane` counts calls per lane: not one extra). Fills follow L10: an exit decided at t is filled at the quote
taken `exec_delay_s` after (q1): the main arm's own q1 for the same mint when it exits at the same moment (scaled, no call), else the latest
main quote-mark, else ONE low-lane sell quote (shared by the arms that exit the same mint together). With no quote for `exit_wait_s` because of a
shed or a transient error the arm fills at its own mark, flagged `MARK_FILL` + `LATENCY_MODEL_ABSENT`; a missing route never does: it is written
off at zero after `unexitable_after_s`, like the main D2. Fees, ATA rent and the Token-2022 transfer fee are `lean.execution`'s on the arm's
own book. Every fill carries `price_source` and `flags`.

**Entry filter (decision-time features only).** `{feature, op, value, source, wait_s}` or `{all: [...]}`. `source` is `screen` or an
observation kind a collector writes (`clusters`, `features`, `flow`, `wallet_signals`, `risk_external`). A collector row counts only if its
`ts` (when it was WRITTEN) is at or before the arm's decision time and its figure is not flagged `AT_COLLECTION`; else `FEATURE_NOT_READY`
with the reason (`NO_ROW`, `WRITTEN_AFTER_DECISION`, `VALUE_NULL`, `AT_COLLECTION`, `STORE_UNREADABLE`). `wait_s` > 0 holds the candidate and
decides when the row exists, at THAT time on a fresh low-lane quote (the entry is later than main's; `deferred_s` is recorded).

**Strategy.** One optional key was added to `lean.strategy`: `full_take_profit_ratio` (> 1). The whole remaining position is sold (TAKE_PROFIT) when the
net mark reaches it; STOP, TRAILING_STOP, MAX_HOLD, TIME_STOP and forced exits rank above it; absent = nothing changes (the hash of an existing
strategy file is unchanged). It is what `tp110` needs: the ladder cannot express a full exit (its fractions must leave a remainder).

**Isolation.** One daemon arm thread (`Runner.run` starts it; outside `run`, e.g. tests and `--once`, the hooks process inline). The hooks the
runner carries do nothing but append to a bounded queue or a dict under a tiny lock and set an event: no I/O under any lock, nothing on the exit
thread's path. An exception in one arm is caught, counted in `health()` (`failures`, per arm) and reaches neither the runner nor the other arms;
an arm whose book cannot be reconciled halts alone. A broken `arms.sqlite` at start leaves the trader running without arms (`arms_unavailable`
event). A config typo in the `arms` block stops the start (strict at load, like every other block).

**Report / notify.** `lean.report` gains a "Shadow arms" section when `arms.sqlite` is beside the store (or `--arms-db`): per arm trades, EV per
trade with a 95 % CI, win rate, P(<= -50 %), PnL, median hold, an equity-curve SVG (main included), skips, price sources, fill flags, exit
reasons, and the **paired comparison vs main on the candidates both traded** (mean difference with CI, share where the arm was better,
arm-only/main-only counts). Telegram gets one new command, `/arms` (the same summary, main first); no arm ever sends a fill message.

## Config block (`config/lean/lean.example.json`, key `arms`; absent = off)

```json
"arms": {
  "enabled": true, "db": null, "initial_cash_sol": null,
  "queue_size": 200, "max_scale": 2.0, "exit_wait_s": 120, "exit_grace_s": 1.0, "eval_interval_s": 15,
  "quote_marks_per_pass": 3, "peak_persist_s": 60,
  "arms": [
    {"name": "hold2m", "strategy_config": {"base": "strategy-default.json",
        "set": {"strategy_version": "lean-1-hold2m", "max_hold_seconds": 120}}},
    {"name": "tp110", "strategy_config": {"base": "strategy-default.json",
        "set": {"strategy_version": "lean-1-tp110", "stop_fraction": "0.18", "full_take_profit_ratio": "1.10"}}},
    {"name": "nocluster_hold2m", "strategy_config": {"base": "strategy-default.json",
        "set": {"strategy_version": "lean-1-nocluster-hold2m", "max_hold_seconds": 120}},
     "entry_filter": {"feature": "largest_cluster_supply_pct", "op": "<=", "value": 0.71, "source": "clusters", "wait_s": 0}},
    {"name": "live_copy", "strategy_config": "strategy-default.json"}
  ]
}
```

`strategy_config` is a path (relative to lean.json), a full inline object, or `{base, set}`. `initial_cash_sol` null = the main store's cash.
The production config (`tests/lean/lean.r1-prod.json`, `lean.r1-live.json`) loads unchanged with arms off and keeps its `lean_config_hash`
(`ProductionConfig`). `health.json` gains an `arms` key (`{"enabled": false}` when off).

## Call budget (low lane only; main and exit lanes: zero added calls, tested)

Inputs: REPORT.md, 50 h of live paper: about 14 hazard-free candidates/h, about 10 main entries/h; example lanes: Helius low lane 9 req/s
(32,400/h), Jupiter low lane 1.8 req/s (6,480/h); Helius Developer budget 9 M credits/month, 1 credit per `getMultipleAccounts`.

| call | provider | when | bound per hour | simulated 20-candidate world (1.55 h) |
|---|---|---|---|---|
| entry quote | Jupiter | a candidate the main arm did not quote and an arm wants; ONE per candidate for all arms | <= 14 (one per candidate); about 4 typical | 10 (of 15 passed: the world keeps the main arm full of flat positions on purpose) |
| exit quote | Jupiter | an arm exit that no main exit quote prices; shared by the arms exiting a mint together; retries only on failure | <= 1 per arm exit; about 20 typical | 14 |
| marks read | Helius | one batched `getMultipleAccounts` per evaluation (<= every 10 s, 100 keys = 33 pools) while an arm holds a mint the main arm does not | <= 360 (1 credit each, 0.26 M/month at the absolute worst = 2.9 % of the budget); about 120 typical | 181 |
| quote-mark | Jupiter | only a position with no mark younger than `stale_mark_s`; at most `quote_marks_per_pass` (3) per evaluation | 0 normally; <= 3 per evaluation during a Helius low-lane outage | 0 |

Worst case in normal operation: about 40 Jupiter and 360 Helius low-lane calls/h, under 1.2 % of the low-lane capacity of either provider.
The mints main and an arm both hold cost nothing (the main read is reused). `health()['arms']['calls']` counts every call by kind, so the live
figure can be checked against this table; the L15 credit tracker sees them too (they pass the shared low-lane transport), and over its
budget the shared low lane is shed, which only ever makes the arms skip or wait. Storage: about 3 KB per arm per candidate, roughly 4 MB/day
for four arms.

## Judgement calls and limits (read before trusting a number)

1. **`nocluster_hold2m` will mostly record `FEATURE_NOT_READY` today.** The L24 row is written by its own thread AFTER the screen decision
   (`late_s`, typically tens of seconds, up to minutes while it waits for L13). The arm refuses to look ahead, so with `wait_s` 0 it skips
   almost every candidate until the cluster collector runs at discovery time (W2b). `wait_s` (e.g. 120) makes it a real, later-entering strategy
   instead; that is a different test (later entry, own quote) and is recorded as such. The feature itself is a percent of supply, as L24 stores it:
   0.71 means 0.71 %.
2. The pnl of an arm that entered through the **low-lane path** has no q0/q1 pair (flag `LATENCY_MODEL_ABSENT`) and a MODELED sell leg for the cost cap:
   slightly optimistic against the L10 fills. The report lists price sources and flags per arm; compare arms on the same source mix.
3. `live_copy` reproduces the main fills exactly in the simulated world (same quotes, scaled by 1.0). In production its entries differ where the
   main arm did not quote, and its size drifts as its equity drifts from the main's. It is a control for the harness, not a second main.
4. **Not modeled in the arms:** held-risk rug triggers (L11), live bot-exit (L23), sell-evidence zeroing (L20). A rugged position with no mark is
   written off at zero after `unexitable_after_s`, a stale-mark one is valued by a low-lane quote.
5. `tp110` takes profit on the NET mark (fees in), so "1.10x" is about 1.12x gross, as the main marks are defined.
6. The simulated world's chain-slot counter ticks on every Helius call, so the main store's `mint_snapshot_slot` values differ between an
   arms-on and an arms-off run; the byte-identical test normalises exactly that field (a real chain's slot does not depend on who asks).
7. A change of an arm's config under the same name keeps its book and is logged (`arm_start.config_changed`); rows carry the hash they were
   written under. Start a new name for a clean experiment.

## Tests

- `tests/lean/test_e2e_arms.py` (fakeworld, real modules, 4 arms from the shipped example over 20 candidates with the L10 model): the arms
  never see a hazard; every passed candidate is decided once per arm; the main arm's fills, states, decisions and errors are **identical with
  arms on and off**; `live_copy` has the main's fills to the lamport; `hold2m` exits at 120 s plus the delay, MAX_HOLD, the stop stays; `tp110` takes
  profit at 1.10; the nocluster filter respects readiness (no row / written after the decision / null / AT_COLLECTION / boundary 0.71);
  entries reuse the main quote, one low-lane call otherwise, extra calls == counted calls; a provider shed changes only the arms; an arm that
  raises changes nothing else; restart with open positions; the report section and the paired comparison; a broken `arms.sqlite`; thread mode.
- `tests/lean/test_arms.py`: config (forms, errors, the shipped block, a copied config, production configs and hashes), filters, store
  (append-only, schema, atomic fill+state), quote math, statistics, evidence variants (hazard, abort, shed, dedupe, `wait_s`, scale limit,
  main-read marks, lanes), exit timing, idle wake, unexitable write-off, runner hooks, `/arms`, the `full_take_profit_ratio` key.
- Other suites that build the runner from the shipped example now set `cfg['arms'] = None` where they count low-lane tokens
  (`test_e2e_features`, `test_e2e_learn`): the example turns the arms ON, and the arms share that lane by design.
