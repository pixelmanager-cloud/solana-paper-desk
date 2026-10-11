# LINT2: lean-desk integration of L12F (entry features v2) and L13 (bundle/sniper + smart wallets, the L13F rework)

Branch `cloud/LINT2`, cut from `origin/integration/r1` (423f423: L09/L09b, L17, L07R + L07R2, LINT1). `desk/` untouched.
Paper only: nothing signs or sends. Each branch was reviewed against its merge-base, merged ONE AT A TIME, and the full lean
suite was run in ONE process after each step (`python -m unittest discover -s tests/lean -t .`, `TMPDIR=/private/tmp/`, real rc).

## Verdicts

| ID | Verdict | Why |
|---|---|---|
| L12F (`origin/cloud/L12F` ba29b61) | **MERGED+FIXED** | Every earlier review item is addressed. One integration defect with L14 (a watchlist entry never got an `entered` features row) is fixed in `LINT2: fix L12F ...`. |
| L13 (`origin/cloud/L13` 28e63d4, the L13F rework) | **MERGED+FIXED** | Shared low lane only, private buckets gone, additive hooks, default off. But L13F item 2 was **NOT done**: smart-wallet outcomes still read `observations(kind='path_mark')`. Fixed (one `read_path` reader over `paths.sqlite` + archives), together with a whole-table decisions scan under the store lock (`LINT2: fix L13 ...`). |

Final suite: **803 tests, rc=0** (1 skipped, pre-existing). After the L12F merge it was 720 with 1 failure, fixed in the merge.
After the L13 merge it was 796, rc=0.

## L12F: MERGED+FIXED

Re-check of the earlier review defects:
- **holder_count, flows, socials always null**: removed from the v2 row (`REMOVED_FIELDS`) with the reason each one cannot be filled
  within the budget or the read-only allowlist. They are not left as null columns, and a test guards this. Accepted ("fill them or
  remove them").
- **Top-10 excludes burn**: the pool vault and the derived token accounts (ATAs) of the incinerator and the system program are
  excluded, both in the screen's retained holder body and in the soft-reject call. Limitation: a burn account that is not an ATA
  still counts as a holder.
- **Holders on soft rejects**: the 3rd call is `getTokenLargestAccounts`, only when the screen stopped before its holder stage.
  Hazard rejects spend nothing.
- **Dev wallet**: `coin_creator` is parsed from the pool account the screen already retained (`desk.pools.parse_pool`, pure). The
  mint's signatures are never paged. There is ONE `getSignaturesForAddress(pool, 1000)`, and `HISTORY_TRUNCATED` is a reason.
- **Block time, not `received_at`**: `graduation_block_time` is the `blockTime` of the pool's oldest signature, only when that
  signature is the graduation slot.
- **Tie-safe quantile buckets**: `quantile_groups` moves a cut to the end of a run of ties, with a property test.
- **Report**: `features_report` is wired into `report.build` and `render_html`.
- **Hook**: delimited and additive. It runs after `_isolated`'s try/except, the same pattern as L07R. There is no `sys.exc_info()`
  in a `finally`, and a `_Halt` writes no row.
- **Wiring and default**: wired in `build_runner`; absent or null means off.
- **No I/O under locks**:
  - the recorder lock guards only the dedupe set;
  - enrichment runs on its own daemon thread from a bounded queue;
  - `submit` never blocks;
  - nothing touches `_trade_lock` or the exit thread.
- **Features only**: an e2e test shows the trading result is identical with the recorder off.

Defect found and fixed (`58ee966`):
- **L12F x L14 (fixed):** the recorder deduped on `(mint, candidate_id)`. L14 re-screens a soft reject under the SAME candidate
  id, so a watchlist re-screen that ENTERS kept only its first `entered=false` row, and the feature-vs-outcome table (entered and
  closed rows only) never saw watchlist entries. The key is now `(mint, candidate_id, entered)`: at most one not-entered row and
  one entered row per candidate. Fail-first unit test.

Merge notes:
- Conflicts in `__main__` (CONFIG_KEYS, load_config, build_runner), `runner` (init and threads beside L11/L14/L15),
  `report` (held_risk + features sections), `fakeworld` (L11 knobs + `coin_creator_tag`) and the example. Every r1 feature was kept.
- `test_e2e_features.test_every_extra_call_went_through_the_low_lane` failed after the merge: since LINT1, the L11 probes share
  the Helius low lane, so the lane-grant count was no longer the recorder's own. That test's world now disables held_risk.

Minor, not fixed:
- Busy pools often return `HISTORY_TRUNCATED` (more than 1000 signatures by screen time), so `graduation_block_time` and
  `tx_count_first_10m` will often be null on exactly the active tokens. They are recorded as reasons.
- In `--once` mode the recorder is built but its thread never runs, so rows queued there are not written. Production uses run mode.
- `outcome()` reads `fills`/`decisions` by mint (indexed) on the candidate thread after handling. It holds the store lock only per
  query.
- L13 keeps its own `wallet_signals` row instead of writing into the L12 row. This is defensible: it collects about 125 s after
  graduation on another thread. Join on mint or candidate_id.

## L13: MERGED+FIXED

Re-check of the L13F items:
1. **Lanes**:
   - every Helius call goes through `providers.low(keys).helius`;
   - `LowLaneLimiter` and `build_low_lane_helius` are gone, with a test;
   - nothing touches the main or exit buckets.
   - **Deviation, accepted:** a `no_token` LANE_SHED is PACED (`pace_s` 0.25 s sleeps, up to `shed_wait_s` per request) instead of
     "skip, retry next time". A collection of up to 15 calls could never finish on the low lane's 2-token burst otherwise.
   - The sleeps are on its own thread, with no lock held, the same pattern as the L07R2 recorder.
   - A shed WINDOW (429) or lost patience stops the candidate with what it learned. With nothing learned, the candidate is retried
     later.
2. **Outcomes from `paths.sqlite` through ONE reader: NOT DONE in 28e63d4. Fixed.**
   - `path_outcome` read lean.sqlite `observations(kind='path_mark')` with `meta.price_sol`, a schema that never existed. Its
     tests fabricated that schema.
   - In production, every path outcome would have been None, so smart wallets could only come from the desk's own closed trades.
3. **Hook**: delimited and additive (init, health, daemon thread, bounded join); off when the key is absent.
4. **Tests**: e2e on fakeworld, with an identical-trading check with the feature off.

Defects found and fixed (`36bff68`):
- **Outcome source (fixed).** `read_path(paths_db, mint)` is now the single reader:
  - it reads `paths.sqlite` plus the archived `paths-*.sqlite`;
  - each file is opened `mode=ro` with its own short-lived connection: no store lock, nothing created, never lean.sqlite;
  - it returns `(status, net_sol)` oldest first, plus `ended`.

  `path_outcome` uses the `net_sol` of OK marks:
  - WIN at `win_multiple` times the first mark;
  - RUG at `rug_price_frac` times the first mark, or on an EMPTY_POOL mark after the reference;
  - FLAT on `path_ends`.

  `build_runner` passes `paths.db_path(...)` when paths are enabled, else None (trade outcomes only).

  Tests: real PathStore rows, including a rollover archive and an open live writer; read-only proof; the old observation schema is
  ignored; the e2e now resolves WIN/RUG through `<state>/paths.sqlite`.
- **Scan under the store lock (fixed).** `pending()` runs every 5 s holding the STORE lock, which the exit thread needs. Its
  decisions subquery matched `candidate_id` only, so SQLite built an AUTOMATIC INDEX over the whole `decisions` table on every
  pass (shown by EXPLAIN). That is O(all decisions ever) every 5 s, growing forever. `d.mint=c.mint` now uses `decisions_mint`.
  An EXPLAIN-QUERY-PLAN guard test covers it.
- **Mutants re-run, all KILLED:**
  - archives not read;
  - the EMPTY_POOL rule removed;
  - `paths_db` not wired in `build_runner` (e2e);
  - the index join removed (fail-first).

Minor, not fixed (follow-ups):
- `_query` reaches into `store._lock`/`store.db`. `pending()` still scans `candidates` (no `ts` index), which is fine at
  ~1.7k rows/day for months.
- `resolve_outcomes` calls `store.closed_positions()` once per unresolved mint (up to 200 every 300 s). The lock is held per fetch,
  and the JSON parse happens outside it. Cache it per pass if the trade count grows into the thousands.
- `read_path` opens `1 + archives` files per mint per resolve pass. This is fine until archives pile up.
- The in-memory wallet table grows with every buyer seen (small).
- Hazard REJECTs are collected too, by design: bundle evidence on rugs.
- `ignore_funders` is empty: add known exchange hot wallets, or exchange-funded buyers will cluster. The caveat ("signal, not
  proof of common ownership") travels with every row.

## Both: locks, exit thread, entry decisions
- Neither feature takes `_trade_lock`, runs on the position thread, or is read by `lean.strategy`.
- Both run on their own daemon threads in run mode only, with bounded joins.
- `features` adds one `submit` (non-blocking) after a candidate is handled.
- Both e2e suites assert an identical trading result (decisions, fills, closes, cash) with the feature off.

## Extra Helius calls at ~70 candidates/h, and the 2 req/s low lane

| Source | Calls per candidate | Calls/h at 70/h | req/s |
|---|---|---|---|
| L12F features | 0 (hazard reject), 2 (passed: dev balance + pool signatures), 3 (soft reject: + holders); max 3 | <= 210 | <= 0.06 |
| L13 wallet_signals | 1-2 signature pages + supply + <= 6 getTransaction + <= 3 x (history + tx) = <= 15 (hard cap 16) | <= 1,120 (typ. ~850-1,050) | <= 0.31 |
| **New total** | | **<= 1,330** | **<= 0.37** |
| L07R2 path recorder (given) | | ~2,160 | ~0.6 |
| **Low lane demand** | | **~3,500** | **~0.97 of 2.0** |

- **Fits on average**: about 49% of the Helius low lane (10 req/s x 0.20, burst 2), plus the small L11 held-risk probes.
- **Bursts**: each wallet-signal collection wants ~15 calls back to back. At 2 req/s that saturates the lane for ~7.5 s, ~70 times
  an hour (~15% of the time).
- **Contention**: during those seconds the path recorder sees `no_token` and paces within 80% of its 15 s interval. A ~5-call poll
  still completes. A gap is possible only when bursts overlap or a 429 sheds the lane.
- **Catch-up after a restart** is bounded by `backfill_max_age_s` 1800 in the example: at most ~35 candidates, ~560 calls, ~5 min
  of a busy lane. The default of 21600 could be ~6,700 calls, ~1 h, with the path recorder gapping.
- **Credits**: ~1M extra calls/month. That is ~1M credits if Helius bills these methods at 1 credit; `ops.credit_costs` assumes
  1. If the plan bills `getTransaction` / `getSignaturesForAddress` higher, set them in `credit_costs`. Production has
  `credit_budget_month` null, so no budget shedding happens anyway.
- **Disk**: lean.sqlite grows by ~10 KB per candidate (~17 MB/day) with `retain_raw: none`. With the module default
  `signatures` it would be ~12-24 MB/h of 1000-row signature pages, which is why the example sets `none`.
- Jupiter: neither feature calls it.

## Config
`config/lean/lean.example.json`:
- adds `features` (enabled) and `wallet_signals` (an object; its presence means on) at paper-collection defaults;
- sets `retain_raw: none` and `backfill_max_age_s: 1800` in `wallet_signals`;
- updates the comment.

The loader tests (`test_lint1.ShippedExampleConfig`, `test_lint2`) pass through the real `__main__.load_config` and
`python -m lean --once`.

**Production check.** The live config is the r1 example with these edits: an absolute `strategy_config`, `initial_cash_sol`
"100", `screen.min_age_seconds` 60 and `ops.credit_budget_month` null. It loads UNCHANGED on this branch, and both new features
are OFF in it (`test_lint2.ProductionConfig`). This was also checked against the real r1 file from git.

To turn them on, add exactly these two top-level keys (the `_comment` keys are optional). `paths.enabled` must stay true, as it is
live, for path-based smart-wallet outcomes.

```json
"features": {"enabled": true, "enrich": true, "queue_size": 500},
"wallet_signals": {"window_s": 120, "sniper_slots": 3, "max_swap_txs": 6, "sig_pages": 2, "funding_wallets": 3, "max_calls": 16,
                   "pace_s": 0.25, "shed_wait_s": 30.0, "max_per_pass": 2, "backfill_max_age_s": 1800, "retain_raw": "none",
                   "ignore_funders": [], "min_wins": 2, "smart_threshold": 0.7}
```

## Commits
- `77228a2` Merge origin/cloud/L12F (conflicts resolved; the e2e lane-count test isolated from L11)
- `58ee966` LINT2: fix L12F dedupe (watchlist entries get their entered row)
- `66cf49d` Merge origin/cloud/L13 (28e63d4)
- `36bff68` LINT2: fix L13 outcomes from paths.sqlite via one reader; indexed pending query
- `9130c9f` LINT2: example config on; production-config check
