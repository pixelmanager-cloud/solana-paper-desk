# W2b: features ready at decision time (#8) and a sample of hazard-reject price paths (#9)

Branch `cloud/W2b` (merged to `integration/r1`; both features OFF in prod). PAPER ONLY: nothing signs or sends, `desk/` is untouched, no network
in any test, no key printed. The review fixes of this work are in `reports/W2b2.md`; this page is the summary of what W2b built and how it
was checked. Where W2b2 changed a default or a number, this page says so.

**Suite (at the W2b tip):** 1346 tests, OK (skipped=1), rc=0, in ONE process. **Mutations:** `tests/lean/w2b_mutate.py` 43/43 killed.

## #8 Features ready at decision time

**Problem.** Every candidate waits `screen.min_age_seconds` (60 s in production) before its screen, and every collector (features L12F, flow L20,
wallet signals L13, clusters L24, RugCheck L21) started only AFTER the screen (TOOL_AUDIT B03). At the decision no collector value existed.

**Design (`lean/prefetch.py`).** One daemon thread, LOW lane only, own read-only scan of the discovery db (the runner's cursor is untouched).
- ~2 s after discovery a **shadow screen** (`candidates.screen` with the real settings, the market bands opened, 3 low-lane calls) tells hazard
  from hazard-free. A hazard (about 58 % of candidates) costs the probe only.
- For a hazard-free candidate each collector's own code runs `lead_s` seconds before the earliest screen (`discovery + min_age_seconds`): features,
  RugCheck, wallet signals, clusters (after wallet signals: it uses its funders), flow. Nothing is written: the result waits in memory.
- After the screen decides, each collector's normal pass finds the finished job (`Prefetch.take`) and writes its row **with no request**, stamped
  with the REAL decision (`decision_ts`), the time the last value was observed (`collected_at`, so `late_s <= 0`) and
  `prefetch = {asof, started_at, finished_at, staleness_s}`.
- **No lookahead.** The decision stays at the screen. Every window a prefetch measures ends at its as-of (`start - settle_s`), never after the
  decision; `take` refuses a result that finished after the decision, whose as-of is after it, that is older than `max_stale_s`, or (W2b2) that
  was used already. A job that cannot finish before the earliest screen is not started (`LATE_START`): under overload the prefetch sheds itself.
  The screen, entry and exits never wait for it (`on_decision` is a dict lookup); a halted trader or the kill switch pauses it.

**Config.** New top-level `prefetch` object (absent / null = off, strict at load): `enabled`, `kinds`, `probe`, `skip_hazards`, `lead_s`,
`probe_at_s`, `min_start_s`, `settle_s`, `max_stale_s`, `pass_interval_s`, `discover_interval_s`, `max_jobs_per_pass`, `max_entries`, `ttl_s`,
`max_attempts`, `retry_delay_s`, `patience_s`, `reserve_tokens`, `scan_limit` (W2b2 adds `flow_calls_per_hour`). `lean_config_hash` leaves the
new keys out while they are off, so the production config keeps the hash it had before.

**Measured** (`tests/lean/w2b_measure.py`, real runner and collectors, only HTTP faked, per-call latencies, 2-5 % failed calls; the OFF world is the
same world without a prefetch). "Ready" = a value observed strictly before the screen decision.
- production-like load (a launch every 30-70 s): 0 % -> 100 % of the hazard-free candidates (flow: PASS only) for every feature;
- stressed (every 6-30 s, 1.5x latencies, 5 % failures, a 30 s 429 shed window): 92-100 %.
- Cost: about +0.9 aux Helius calls per candidate (+3 %), roughly 700 credits/day at 776 candidates/day. Wallet signals and clusters still run
  post-screen on hazard rejects (about 38 % of the low-lane Helius calls in the sim): a separate saving, not done.
- Overload (a launch every 3-9 s, 3x latencies) is beyond the envelope: 4-17 % ready, and any extra user of the saturated lane can cost the
  collectors' own pass a few points. W2b2 added the rollback signals for this (health `prefetch.probe_fp_rate`, `low_lane`).
- **Limit found in review (fixed in W2b2):** the windows end 26-38 s before the decision, so prefetched figures are smaller than decision-time
  ones (medians x0.58-0.63). They are now a different feature for the learner.

## #9 A deterministic sample of hazard-reject price paths

`paths.hazard_path_sample` (a fraction in [0, 1]; absent = 0 = off). A hazard reject is in the sample when `sha256('lean.paths.hazard_sample:' + mint)`
read as a number in [0, 1) is below the fraction (deterministic, monotone in the fraction, pinned by golden values). The runner hook only queues
the screen result in memory; the recorder thread starts the path: a hazard whose screen stopped before its market stage (MAYHEM_POOL,
HOLDER_REWARD_POOL) needs ONE batched `getMultipleAccounts` of its two vaults, one that reached it (HOLDER_CONCENTRATION_ABOVE_MAX) starts from the
screen's figures. Paths are labelled `stage='hazard_reject'`, `reason=['hazard_reject: <reason>']`, **never entered** (no quote, fill or entry
decision). The learner reports them in their own section (per reason: labels, rug/win rate, simulated PnL, Welch test against the entered
candidates) and keeps them out of every table, rule, window and label count; `wallet_signals.read_path` ignores them. Health counts them
(`hazard_seen / sampled / started / unrecordable / expired / queue_full / by_reason / skipped / probe_calls / queued`).

**Cost, corrected.** The first estimate (+16 MB/day at 0.2) was about 7x low. The pre-deploy review measured **about 120 MB/day of `path_marks` at
0.2** (about 60 MB/day at the 0.1 the example now ships; `paths.sqlite` rolls over at `max_db_gb`). Calls: about one more `getMultipleAccounts` per
15 s poll once the active paths pass a multiple of 50 pools (about +5.8k calls/day at 0.2, half at 0.1) plus fewer than one start read per sampled
hazard.

## Files
`lean/prefetch.py` (new); hooks in `lean/{flow,wallet_signals,clusters,features,risk_external,runner,paths,learn,labels,report,__main__}.py`;
tests `tests/lean/{test_w2b,test_e2e_w2b,w2bworld,w2b_measure,w2b_mutate}.py`.
