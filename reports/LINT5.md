# LINT5: Round C integration (C1, C3, C4, C2) onto integration/r1

Branch `cloud/LINT5`, based on `origin/integration/r1` at `d7daabf` (M1/M1b, M2, W2a/W2a2, W2b, `MAX_TX_VERSION = 1`).

The branches were merged one at a time in this order: C1, C3, C4, C2. After each merge I ran the full lean suite in ONE process, with
`TMPDIR=/private/tmp/` and `python -m unittest discover -s tests/lean -t .`, and checked the real rc.

| step | tests | rc |
|---|---|---|
| r1 base | 1395 | 0 |
| + C1 + LINT5 C1 fix | 1457 | 0 |
| + C3 + LINT5 C3 fix (run on the committed `cfaba88`) | 1490 | 0 |
| + C4 + LINT5 C4 fix | 1545 | 0 |
| + C2 + LINT5 C2 fix | 1601 | 0 |
| final (+ C4 lane reserve) | **1602** | **0** |

`desk/` was not touched. Nothing used the network. No keys were printed.

## Verdicts

| ID | verdict | one line |
|---|---|---|
| C1 | **MERGED with fixes** | Sound offline tool. It replayed pre-M1 marks only and could propose on a pooled CI. Both are fixed. |
| C3 | **MERGED with fixes** | Correct log-stack create matching (CPI-safe), bounded and budgeted. It used `maxSupportedTransactionVersion: 0`, and its config block changed the prod hash. Both are fixed. |
| C4 | **MERGED with fixes** | It was blind to CPI migrations, which is UPDATE 2's main finding, and also skipped every signature the feed had stored. It also had tx version 0 and no lane reserve. All are fixed. |
| C2 | **MERGED with fixes** | Layout and discriminator verified. Its defaults could not work on mainnet: v1 lists every curve ever made, and the V2 cursor could never initialise. Fixed. It stays OFF until the V2 call is verified live. |

No branch was skipped. Every defect I found could be fixed within the branch's own module, plus tests.

## Fixes (each is a `LINT5: fix ...` commit with tests in `tests/lean/test_lint5.py`)

### C1: `lean/replay.py`, `lean/tune.py`
1. **Effective marks.** A `path_marks` row that the recorder wrote with `marks.model: effective` is now replayed with that row's own model and fee.
   - Reserves are vault + `virtual_raw`. The fee is the row's own `fee_bps`.
   - Both go through the labels / marks helpers: `labels.mark_lamports`, `labels.sell_quote_lamports` (= `marks.sell_output`) and `marks.buy_output` for the entry.
   - The row's fee applies whatever `costs.fee_model` says.
   - Vault rows (pre-M1, or `model: vault`) keep the vault reserves and the fixture tier. Their recorded `fee_bps` is the configured flat 25, not the pool's fee.
   - The loader reads the M1 columns when the file has them. The acceptance rule matches `labels.iter_paths`.
   - The fix has an E2E test: the real runner + recorder on fakeworld with `marks.model: effective`. On every recorded row, the replay's mark equals the recorder's `net_sol` within 2 lamports.
2. **Proposal gate.** `proposal.json` now also needs the PROPOSED policy's own test-block CI lower bound above 0. Before, the pooled out-of-sample CI could clear 0 while mixing several folds' selections.
3. **Known divergence, not changed.** `ACCOUNT_MISSING` marks are treated as missing data in C1 but as a dead pool in `labels`. The snapshot has none, so the result below is unaffected. The coordinator should decide which is right before a post-M1 rerun.

### C3: `lean/census.py`
1. `getTransaction` now uses `providers.MAX_TX_VERSION`. Before, it used the banned literal 0, and `test_every_get_transaction_accepts_version_1` failed after the merge.
2. **Prod config hash.** `load_config` fills a full `census` dict (`enabled: false` + defaults), and `lean_config_hash` covers the whole loaded config. So merging C3 changed the prod hash.
   - Fix: `__main__.NEUTRAL_WHEN_OFF = ('census', 'backfill', 'curves')`. `_strip_neutral` drops each of these while it is absent or disabled.
   - `tests/lean/test_arms.py` and `test_w2b.py` rebuild the "before" hash, so they now drop the same keys.
   - Verified: the hashes of `tests/lean/lean.r1-prod.json`, `lean.r1-live.json` and `lean.baseline.json` are byte-identical before and after LINT5 (`2321f7ef…`, `f916b0b4…`, `8fe6f1ab…`). Turning a block on changes the hash, which is a new run by design.
3. **Test hygiene.** `test_e2e_census` and `test_e2e_backfill` load the example config, which sets the process-global lane shares. They now reset them on cleanup; before, `test_providers` failed when run after them in some orders.

### C4: `lean/backfill.py`
1. **CPI migrations (main defect).** `lean.candidates._event_hint` accepts only a TOP-LEVEL `migrate`. C4 reused it, so every migration that runs as an inner instruction was rejected twice:
   - by the discovery intake, although the feed STORED the transaction;
   - by C4, which also skipped every signature the feed had stored, without looking at it.

   The fix has three parts:
   - `event_hint_any_depth` / `frame_candidate_any_depth`. A top-level migrate is judged by `lean.candidates`' own rule, unchanged; a test proves the result is identical. An inner migrate (path `i.j`) needs exactly one `CompletePumpAmmMigrationEvent` with the same program, mint and pool, emitted INSIDE that migrate. That means the same inner group, `stackHeight` = migrate's + 1, and before any instruction at the migrate's own depth. The rule reads `meta.innerInstructions`.
   - **Feed-stored signatures.** The stored frame is decoded locally, with no RPC and no credit. If `lean.candidates` rejects it and the any-depth rule accepts it, it is fed as source `backfill` and counted as the new `intake_rejected_migrations`. A top-level migration the feed has stays the discovery path's own, so it is never double-fed. A mutant that feeds intake-accepted frames too is killed.
   - Fetched transactions use the same rule.
2. `getTransaction` now uses `providers.MAX_TX_VERSION` (was 0).
3. **Lane reserve.** New key `reserve_tokens`, default 2.
   - C4 runs up to 3 + 40 low-lane calls in one burst. A call made while fewer than 1 + reserve tokens are left is a `LANE_SHED`: nothing is sent, the call is retried within 15 s, and the shed is counted.
   - This keeps the path recorder's marks (C1's input) from being starved.
4. **Checks.**
   - Main fills are identical with C4 on and off: E2E test, f1's fills.
   - `candidates.mint` is never doubled: 6 candidates for 6 mints, and a restart neither re-fetches nor duplicates (C4's own test, still green).
   - A candidate older than `max_age` is recorded and rejected `TOO_OLD`, never entered (C4's own test). `migrated_at` is the on-chain `blockTime`.

### C2: `lean/curves.py`
1. **Default method.** `getProgramAccounts` with only the discriminator filter lists EVERY pump.fun curve ever created (millions of accounts). It cannot work on mainnet, so the default `method` is now `getProgramAccountsV2`. v1 stays selectable.
2. **V2 cursor could never initialise.** The cursor was set only after a COMPLETE poll. The first, cursor-less, poll would have to list all of mainnet within `max_pages` (3 × 1000), which is impossible, so every poll re-listed the same first pages forever. Now:
   - an overflow is a `max_pages` gap AND still advances the cursor;
   - the cursor is the LOWEST slot any page of the poll reported (before, it was the last page's slot, which could skip changes made while later pages were read);
   - after a V2 response with no slot, each later poll reads `getSlot` first (1 credit) so the cursor keeps moving.
3. **Credits.** The default `max_credits_month` is now 450,000, down from 500,000. The cap is now under 0.5M, with room for transport retries that are billed after the pre-call check.
4. **M1 adaptation.** The on/off E2E compared whole `lean.sqlite` files. After M1, the start event carries `lean_config_hash`, which differs when the recorder is on, by design. That one field is masked; everything else must still match.
5. **Delimiting.** The C2 hooks were moved out of the L24 block into their own `# --- C2 ---` blocks in `CONFIG_KEYS` and the runner health.

### Mutations (all killed, re-run in this session)

| target | mutant | owner |
|---|---|---|
| C1 | effective rows ignored | LINT5 |
| C1 | effective mark replaced by the vault mark | LINT5 |
| C1 | row fee replaced by 25 | LINT5 |
| C1 | proposal on the pooled CI only | LINT5 |
| C1 | effective entry uses the vault buy | LINT5 |
| C1 | lag forced to 0 | C1's own, re-run |
| C1 | no embargo purge | C1's own, re-run |
| C3 | invoke stack ignored | |
| C3 | tx version 0 | |
| C3 | no sampling cap | |
| C4 | top-level-only rule | |
| C4 | feed-stored CPI not re-examined | |
| C4 | migrate frame end ignored | |
| C4 | frame / depth not checked | |
| C4 | intake-accepted frames fed too | |
| C4 | tx version 0 | |
| C4 | reserve check removed | |
| C4 | feed check removed | C4's own, re-run |
| C2 | overflow does not advance the cursor | |
| C2 | no `getSlot` after a slotless listing | |
| C2 | cursor = last page slot | |
| C2 | default method v1 | |
| C2 | no hourly budget check | C2's own, re-run |

## C1 result on the analysis snapshot (read-only, `mode=ro`, no copy)

Command:
```
python -m lean.tune --lean-db .../leansnap/lean.sqlite --paths-db .../leansnap/paths.sqlite --grid grid.json --out-dir ...
```

Data and settings:
- 700 paths, 992,020 marks, 2,215 gaps, 403 entered live.
- The rows are pre-M1 (no effective columns), so the replay uses vault reserves + the fixture tier (125 bps below 420 SOL).
- Costs: 50 bps slippage per leg and 0.00025 SOL per fill.
- Strategy: `strategy-default.json`. Its exit rules are identical to `lean-1-collect-c`; only portfolio and risk keys differ.

| exit (all 700 paths) | EV lag1 (default) | EV lag0 | 1st / 2nd half (lag1) | P(≤ −50%) lag1 | REPORT.md Q4 (ENTERABLE n=412) |
|---|---|---|---|---|---|
| hold 2 min | **−2.7%** CI [−5.6, +0.1] | −2.5% | +0.7 / −6.2 | .12 | −1.2% |
| hold 1 min | −3.1% | −4.3% | −0.4 / −5.9 | .07 | −2.1% |
| TP 1.10 + 2 min cap | −4.0% | −1.9% | −1.4 / −6.5 | .11 | – |
| TP 1.10, stop .82 | **−6.3%** | −4.8% | −5.8 / −6.7 | .13 | −4.5% |
| TP 1.10, stop .70 | −6.3% | −3.9% | −5.6 / −6.9 | .15 | −4.0% |
| TP 1.20, stop .82 | −8.3% | −7.1% | −9.5 / −7.1 | .19 | −8.4% |
| live ladder + 10 min cap | −13.1% | −12.8% | | .27 | −17.7% |
| live ladder (replayed) | **−22.1%** | −21.9% | −24.9 / −19.3 | .31 | live `-c` −30.9% |
| trailing 10% from entry | −37.1% | −37.7% | | .42 | −56.7% |

- **Verdict:** `NO_PROPOSAL: NO_FOLD_SELECTED_A_POLICY`.
  - Purged walk-forward: 5 blocks, 6 h embargo.
  - Folds 1–4 trained on 84 / 196 / 348 / 427 paths, with 56 / 84 / 72 / 133 paths purged.
  - In every fold, 0 of 15 policies survived BH at q ≤ 0.1. Every in-sample q is 1.000.
- **Agreement with REPORT.md Q4: rough.** The ranking is the same: short holds best, then a tight TP, the live ladder far worse, trailing worst. The magnitudes are within about 1.5 points for hold and TP. Three things explain the gaps:
  - C1 replays ALL 700 paths, including screen rejects; REPORT used the ENTERABLE 412.
  - C1 uses the fixture's 125 bps tier on vault reserves; REPORT used effective reserves with a Jupiter-calibrated 110 bps.
  - C1 fills at the next 15 s mark by default.
- **The live −30.9% is not reproduced (−22%), as expected.**
  - The replay buys and sells on the same vault reserves. It therefore lacks the live mark bug: a Jupiter-filled entry marked on the vault, where the −18% stop fired near −9%.
  - It also lacks the L11 DANGER exits.
  - Calibration: 311 of 486 live positions reproduce their exit reasons exactly. Of the mismatches, 64 are live `DANGER` exits, which cannot be replayed. Most of the remaining 93 + 16 + 2 come from the vault-mark bug and from the replay entering at the path's first mark instead of the live fill. The count is the same with live-equivalent costs (flat 25 bps, lag 0).
- **Rerun after M1.** Once `marks.model: effective` paths exist, rerun C1 on them; the fix above makes it use those rows.
- **Limitation to fix later.** Add a population filter (screen PASS only) to `lean.tune`. C1 has none today, so its EVs mix in candidates the trader never enters.

## C2: live verification the coordinator must run (VPS, low lane, before `enabled: true`)

1. **One V2 call by hand.**
   - Request: `getProgramAccountsV2` with `["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", {"encoding":"base64","commitment":"confirmed","filters":[{"memcmp":{"offset":0,"bytes":"4y6pru6YvC7"}}],"dataSlice":{"offset":0,"length":125},"limit":10}]`.
   - The memcmp bytes are the base58 of the discriminator `17b7f83760d8ac60` = sha256(`account:BondingCurve`)[:8].
   - Record the response shape: `accounts`, `paginationKey`, and whether there is a slot (`context.slot` or `slot`). If there is no slot, the recorder pays +1 `getSlot` per poll. Check that `curves.CurveRecorder.normalize` accepts the response.
2. **The same call with `changedSinceSlot` = that slot − 150.** Confirm it returns only recently traded curves, and record how many curves change per minute; that sets `page_limit` / `max_pages`. Check the Helius dashboard credit cost per page; it is assumed to be 10.
3. **Decode.** Decode 5 returned accounts with `lean.curves.decode_curve`. For one live mint, compare `getAccountInfo(curve_address(mint))` and the price / progress against pump.fun. Check the account sizes seen (49 / 81 / 82 / 83 / 115 / 125 or larger).
4. **Canary for 1 h.** Run with `"curves": {"enabled": true}`, then check `health.curves`:
   - `effective_poll_s` ≈ 58;
   - `calls_last_hour` ≤ 62 and `credits_last_hour` ≤ 625;
   - `avg_pages_per_poll`;
   - `curve_poll_gaps` by cause (`max_pages` should be rare);
   - `python -m lean.curves report --db .../curves.sqlite`.
5. **Join.** For one curve that graduated during the canary, `join_by_mint(curves.sqlite, paths.sqlite, mint)` must continue into the PumpSwap path.

## C3 / C4 live checks

- **C3.** Run `getTransaction` (maxSupportedTransactionVersion 1) on one known Raydium LaunchLab create and one Meteora DBC create, then `census.match_creates(tx, program, patterns)`. Each must return 1. The patterns are from IDL memory and unverified.
  - Pump graduations in C3 are counted from the discovery DB, so they inherit the intake's ~25% miss (C4 rows are not counted there).
- **C4.**
  - **Composition of the miss.** Take ~50 recent `getSignaturesForAddress(39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg)` signatures and classify each one by:
    - whether the feed stored it;
    - whether lean got a candidate from it;
    - whether `backfill.frame_candidate_any_depth` accepts it, using the stored payload or a v1 `getTransaction`.

    This gives the real composition of the 25% miss: CPI vs WebSocket gaps vs bot/no-op transactions.
  - **After enabling.** Watch `health.sources.backfill.today`: `missed_migrations`, `intake_rejected_migrations`, `beyond_max_age`, `gap_rate`, `sheds`.

**Expected C4 recovery rate.** Up to ~100% of the ~25% intake miss is recoverable, but this is unverified live.
- **CPI migrations the feed stored:** recovered at the next poll (≤ 120 s), at zero credits. Their event is emitted by the migrate itself (Anchor `emit_cpi`, one frame deeper), which is the shape the rule requires.
- **WebSocket-gap migrations:** recovered within one poll while RPC works, unless more than 300 authority signatures arrive per 120 s (`max_pages × limit`; counted as `overflow_polls`).
- **Entry window.** In prod (`min_age 60`, `max_age 7200`), recovered candidates are screened at ~60–180 s after the block. This is later than discovery's ~60 s, but far inside `max_age`.
- **Not recovered:** migrations whose event is not a direct child of the migrate, ambiguous transactions with two migrations, and RPC outages longer than `max_age`. The last are recorded, never entered.
- **Coordinator decision.** In prod a backfilled candidate may be ENTERED up to 2 h after graduation (`max_age_seconds` 7200). REPORT.md shows that universe is much worse than +60 s. Consider a lower `screen.max_age_seconds` before enabling C4. C4 is not recording-only: it adds main-arm entries.

## Credit budget (Helius credits per month, defaults)

| feature | calls | credits/month | hard cap |
|---|---|---|---|
| C1 | offline | 0 | – |
| C3 census | 2 pads × (1 sig page + ≤ 4 tx) per 600 s | ≤ 43,200 | config refused above `credit_budget_month` 50,000; plus an hourly call cap |
| C4 backfill | 1–3 sig pages per 120 s + 1 `getTransaction` per feed-MISSED signature (feed-stored ones are free) | ~21,600 sig calls + ~1–10k tx ≈ **25–35k** at 1 credit/call (edit `credits_per_*` if Helius bills these higher) | per poll only (3 + 40 calls); the low lane is shed by the LINT4 ops budget |
| C2 curves | ≤ 62 V2 pages/h × 10 (+1 `getSlot` only if slotless) | ≤ **450,000** | rolling-hour budget from `max_credits_month`, restart-proof |
| **total** | | **≈ 520k** | inside `ops.credit_budget_month` 9,000,000 |

## Enable list (all are OFF until a block is added with `"enabled": true`)

Absent or disabled blocks do not change `lean_config_hash`. Enabling one is a new run.

1. **C1** has no switch: `python -m lean.tune --lean-db … --paths-db … --grid grid.json --out-dir …`. It is read-only and can be used now. The grid used here is in this report's C1 section.
2. **C3** `"census": {"enabled": true}`: after the C3 live check. Cheap: ≤ 43k credits/month.
3. **C4** `"backfill": {"enabled": true}`: after the C4 composition check and the `max_age` decision. It changes main entries.
4. **C2** `"curves": {"enabled": true}` (V2 is the default method): after the 5 C2 live-verification steps. ≤ 450k credits/month.

The example config (`config/lean/lean.example.json`) carries all three blocks with every key listed and `"enabled": false`.

## Files
- New modules: `lean/replay.py`, `lean/tune.py`, `lean/census.py`, `lean/backfill.py`, `lean/curves.py`.
- Hooks: delimited blocks in `lean/__main__.py` and `lean/runner.py`, plus `getProgramAccountsV2` in `lean/providers.py`.
- LINT5 tests: `tests/lean/test_lint5.py`, 28 tests.
