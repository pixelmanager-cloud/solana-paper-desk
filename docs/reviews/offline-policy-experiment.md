# Offline entry-policy experiment foundation

Baseline: integration/cloud-wave1 aa24a7648b7d39972db17364b68788d913d96458.
Only additive research tooling, dedicated tests and this report; no desk source,
live fingerprint, active policy, ledger, budget or deployment changes.

Run from the repository (Python3.12):

```
python -m research.policy_experiment --training /protected/saved-training.sqlite \
  --holdout /protected/later-holdout.sqlite --trials /offline/trials.json --now EPOCH
```

Trials are an explicit JSON list, for example
`[{"min_liquidity_usd":"9000"},{"min_age_seconds":400}]`.
The coordinator supplies original saved databases; arbitrary exported JSON reports
are not accepted as evidence. Read-only SQLite backup yields a consistent bounded
snapshot, then the existing experiment_report/read_checkpoint/original quote
validators verify it. Originals are never initialized or repaired. CLI output
omits database paths, raw source metadata and provider identifiers.

Versioned proposals bind the whole original config hash plus an entire candidate
config hash, changing only supported min/max age and minimum liquidity fields.
Strategy entry-score/momentum thresholds are literals, not config parameters;
they cannot be tuned here. Market-cap bounds, known hazards, token/evidence checks,
portfolio/capital, fees/slippage, TTLs, exit policy and all budgets remain frozen.
The tool neither calls strategy with a candidate nor runs engine/transport/writers.

Each ordered partition must have disjoint retained event/raw/price/feature/quote
time bounds; holdout starts STRICTLY after the last training bound. Explicit v3
signal-window lookback is included. Shared event IDs, event hashes or raw payload
hashes reject even across copied databases. All samples share exact config and
implementation identities. Hidden source lookback, unrecorded rejected candidates
and population independence cannot be established by the aggregate report; no
leak-free counterfactual claim follows from passing these checks. Caller labels
cannot override saved timestamps, accounting or policy identity.

Closed-trade net realized accounting and recorded fees are reported separately
from open-trade realized values and modeled marks. Fees are already in net results
and are NOT subtracted again. Original execution assumptions and ownership-risk
flags remain visible; EXECUTION_UNVERIFIED always remains. Saved drawdowns are
per-experiment modeled values, not stitched portfolio drawdown. Unrecorded costs,
source authentication and actual fills remain unknown.

Minimum floors are20 training and10 holdout closed trades, no open lifecycles.
These are conservative research screening floors, not statistical sufficiency.
Missing holdout is allowed only as an explicitly insufficient dataset. A verified
zero-trade checkpoint therefore yields INSUFFICIENT_DATA, not a profitable policy.
Even when floors are met, status BLOCKED_COUNTERFACTUAL persists: current reports
lack per-candidate rejected/unobserved outcome paths and alternate portfolio/capital
scheduling. Every proposal has null counterfactual PnL, no ranking, no promotion,
no profitability verdict, and no entry authorization. Batch trial count is visible;
optimization trials performed=0 and external prior searches are explicitly unknown.

Synthetic tests use actual engine/ledger/report validation for closed trades,
rejections, partial/open marks, quote assumptions and history-risk flags; corrupted
journals/config, copied samples, equality/overlap/reversed chronology, stale feature
lookback, future times, forbidden controls, malformed thresholds/duplicate JSON,
and missing files reject without repair. Thirty actual synthetic closed lifecycles
meeting the floors still cannot authorize a counterfactual. No production ledger
was read; the coordinator's reported real zero-trade state remains insufficient.

Dependency: honest comparison/ranking requires a separately reviewed original-bound
candidate observation and outcome/population contract with portfolio-path semantics.
This patch deliberately does not fabricate that contract or accepted evidence.
Packaging is unchanged; run the research module from the repository checkout.
