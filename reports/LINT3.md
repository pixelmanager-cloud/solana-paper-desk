# LINT3 — integration and harsh review of L19 (selection learner)

BASE: `origin/integration/r1` (852d6bf, live in paper on the VPS) + merge of `origin/cloud/L19` (b2bafba). Branch `cloud/LINT3`.
`desk/` untouched. Paper only, offline, read-only; no network in any test.

## Verdict

**L19 as delivered: BLOCK.** After the LINT3 fixes: **PASS (merge-ready), with the limitations below.**

Why L19 was blocked (it was built before L12F v3 and L13 landed on r1):
1. **Lookahead (the #1 risk).** A feature row was accepted when collected up to `max_lag_s` = 300 s after the "decision", and
   `decision_ts`, `late_s` and `flags` were never read. So `AT_COLLECTION` snapshots (dev balance, soft-reject holders) read minutes after the
   decision were learned from, and v1/v2 rows whose `tx_count_first_10m` runs up to 10 minutes PAST the decision were accepted.
2. **The wrong decision time.** The "decision" was the path's `start_ts`, which is the recorder's clock when it received the candidate, after
   the decision. It was not the trader's decision in `decisions`.
3. **Wrong or missing schemas.** It ignored the L13 `wallet_signals` rows entirely. It never read `path_gaps` or `path_ends`.
4. **Gaps were read as flat prices.** A path with missing marks, or one still being recorded, could be labelled FLAT. A pool drained before the
   first mark was never a liquidity rug (the baseline was the first mark, not the decision-time reserve). For candidates that were not
   entered, the reference ratio left out the paper slippage and fee.
5. **An unreviewed dependency.** It merged L08/L08B/L08BF (`replay.py`, `tune.py`, about 2,700 lines with their tests). None of that is on r1,
   so it would have reached r1 without review. `ops.py` would also have started calling that `tune.main` when `--grid` is set.
6. **The CLI did not fit the VPS.** `--db` and `--now` were required. Proposals went to `config/lean/proposals` inside the code tree, which is
   the root-owned, read-only `/opt/solana-desk-lean` on the VPS, so it would fail there. Archives were ordered by file name, and
   `paths-YYYYMMDD-1` sorts before `paths-YYYYMMDD`. Too little data was an exception, not a status. All marks of all paths were loaded at once,
   so a 2 GB recorder history would blow the memory.

Shadow mode (item 6): L19 did not implement it (no `shadow` code anywhere in `lean/`). So nothing can change trading, and the default is
off by absence. Nothing to test.

## What LINT3 changed

* **Merge:** I resolved the 4 conflicts (`lean.example.json`, `__main__.py`, `runner.py`, `test_e2e_features.py`) by keeping r1 as it is. L19's
  sides were its stale pre-v3 copy of L12F. **I removed L08/L08B/L08BF** from this branch (`lean/replay.py`, `lean/tune.py`, their 3 test modules
  and 3 reports). r1 has no replay, so the learner uses its own minimal replay (below). L08BF stays on its own branch for its own LINT.
* **`lean/labels.py` (rewritten):**
  * It reads the L07R2 schema: `paths`, `path_marks`, `path_gaps` and `path_ends`, from `paths.sqlite` plus the `paths-*.sqlite` archives
    found beside it, all plain `mode=ro`.
  * It reads one path at a time with indexed per-mint queries. Measured: 500 paths × 1,440 marks (48 MB) took 2.6 s with 49 MB RSS. The
    oldest start wins regardless of file order.
  * Labels start at the decision and use `adapters.mark` on `base_raw`/`quote_raw`. The position is the one the live paper trader holds or
    would hold: the real fill, or `paper.buy` of the recorder's reference quote.
  * RUG / WIN / LOSS / FLAT are as specified. The liquidity rug is measured from the decision-time spendable reserve.
  * New outcome: `INSUFFICIENT_PATH`. It covers no mark, `TOO_MANY_GAPS` (more than 25 % of due observations missing up to the deciding
    mark; gap rows and MALFORMED marks count), `HOLE` (more than 180 s without an observation, which catches recorder downtime that leaves no
    gap rows), and `PATH_NOT_ENDED`. FLAT requires an ended and fully observed path.
  * `simulate_exits` is the minimal live-exit replay. It calls `strategy.exit_decision` exactly as `runner._manage` does: mark first, then a
    constant-product sell quote, then `exit_decision` again with the quote, then `paper.sell`, carrying the ladder state. A dead pool at the
    end is a `RUG_WRITEOFF`.
* **`lean/learn.py` (dataset and CLI rewritten; miner, statistics and report kept):**
  * The decision is read from `lean.sqlite` `decisions`, with the same definition as the v3 feature row (see Lookahead guarantees).
  * The L12F v3 rows and the L13 `wallet_signals_v2` rows are joined under the lookahead policy below.
  * The PnL objective is `simulate_exits`.
  * `enterable` means entered, or passed the screen and was refused only by a portfolio limit.
  * The new wallet features are `bundle_score`, `sniper_count`, `same_slot_buyers`, `bundled_supply_pct` and `smart_buyers_count`. A rule on
    them is marked `wait_for_wallet_signals`.
  * Too little data now gives `INSUFFICIENT_DATA` with rc 0, not an exception.
  * Every run writes `<out-dir>/proposals/<ts>-selection.json` (exclusive, mode 0600). Its `status` is `SELECTION_PROPOSAL_NOT_APPLIED` only
    for a confirmed set, else `NO_PROPOSAL` with no rules. The run also writes `<out-dir>/lean-learn-<ts>.html` and `.json`.
  * The page has a new section, "Data used and excluded (no lookahead)", with every exclusion count.
  * CLI: `python -m lean.learn --lean-db … --paths-db … --out-dir … [--config lean.json | --strategy S --pool-fee-bps N]`.
    `--config` takes the LIVE strategy and `pool_fee_bps` from the trader's own `lean.json`.

## Lookahead guarantees (each is a test, and each has a mutant killed)

* **The decision.** It is the trader's last `screen` decision at or before the path start, or the last `entry` decision after that screen.
  Nothing after the path start is used.
* **Feature rows.** A row is used only if `features_version >= 3` and its own `decision_ts` is at or before the decision. The last such row of
  the same candidate wins, and a later row (a re-screen, a late write) can never replace it. Rows after the decision, pre-v3 rows and rows
  without `decision_ts` are counted in `stats.skipped`.
* **Fields flagged `AT_COLLECTION`.** They are used only when the row's `late_s` is at most `--max-late-s` (default 15 s). Otherwise the field is
  excluded and counted (`stats.excluded[field].AT_COLLECTION_LATE`); the row's other fields are kept. Any flag other than `CUT_AT_DECISION`
  excludes the field (fail closed).
* **Wallet rows.** A row is used only if it is version `wallet_signals_v2`, its `decision_ts` is at or before the decision, and its window ends at
  the decision (`window_after_decision_s == 0`). `smart_buyers_count` is used only if `smart_asof_ts` is at or before the decision.
  `bundled_supply_pct` is used only if `supply_source == 'screen'`.
* **Marks.** No mark before the decision is read by a label or by the exit replay, and no mark ever becomes a feature.
* **The leak tests:**
  * `test_learn.WriterIntegrationTests` uses the real `Store.add_decision`, `FeatureRecorder._write`/`build_row`, `WalletSignals.collect` and
    `PathRecorder`. Every post-decision value is a canary (666, or 6 buyers instead of 3). It asserts that no canary reaches a learning row and
    that the exclusion counts are exact. With the guards patched out, the canaries DO appear, so the test is sensitive.
  * `test_e2e_learn` LATE world: the real runner, with the planted signal readable only 60+ s after each decision. Nothing is confirmed and all
    100 values are excluded. With `max_late_s=3600` the leaked rule is confirmed, so the guard is what stops it.

## Evidence

```
TMPDIR=/private/tmp/ .venv/bin/python -m unittest discover -s tests/lean -t .   ->  Ran 927 tests, OK (skipped=1), rc=0  (one process)
TMPDIR=/private/tmp/ .venv/bin/python tests/lean/lint3_mutate.py                   ->  33 mutants, 33 killed, rc=0
```
* **Test count:** r1 had 829 tests. LINT3 adds the learner modules: `test_labels` 25, `test_learn` 62, `test_e2e_learn` 11.
* **Planted vs noise (e2e, real runner + recorders, 100 launches each):**
  * Planted: `dev_holding_pct > 15.6` is mined, selected and CONFIRMED, and a proposal is written.
  * Noise: nothing is confirmed, and the file says `NO_PROPOSAL`.
  * LATE: nothing is confirmed (see the leak tests).
* **Live calibration:** `simulate_exits` reproduces the live runner's ordered sell reasons on all 100 traded tokens of the planted world.
* **Mutants** (`tests/lean/lint3_mutate.py`, one replacement each, all killed):
  * mark without the pool fee;
  * marks before the decision (label and exits);
  * gaps ignored; holes ignored;
  * an unfinished path labelled FLAT;
  * first-mark liquidity baseline;
  * win/stop precedence;
  * reference without fee or without slippage;
  * ladder or peak state not carried;
  * no write-off;
  * rows after the decision accepted; late `AT_COLLECTION` accepted; unknown flags accepted; pre-v3 rows accepted;
  * uncut wallet window; smart table after the decision; post-decision supply; old wallet version; another candidate's row;
  * decision = screen instead of the entry; a decision after the path start;
  * `INSUFFICIENT_PATH` rows learned;
  * PnL not from the replay;
  * everything enterable;
  * selection on the confirm window; confirm gate removed; select significance removed; mine support removed;
  * unknown values matching;
  * unconfirmed rules proposed.
* **Read-only:** both databases are byte-identical after every run. The learner reads while a write transaction is open on `lean.sqlite`.
  A WAL database with read-only `-wal`, `-shm` and directory opens fine with `mode=ro`, which is the systemd `ReadOnlyPaths` case.

## Run it on the VPS (coordinator; nothing here touches the running trader)

Prerequisite: the LINT3 code must be on the VPS. The live trader's tree `/opt/solana-desk-lean` is root-owned and r1 does not contain LINT3
until it is merged. Until then, put a separate root-owned copy beside it. Run from the local repo:
```
git archive <LINT3-sha> | ssh root@158.247.196.20 'install -d -o root -g root -m 0755 /opt/solana-desk-learn && tar -x -C /opt/solana-desk-learn'
ssh root@158.247.196.20 'install -d -o solana-desk -g solana-desk -m 0700 /var/lib/solana-desk-lean/learn'
```
Then, as root on the VPS (a transient unit run as `solana-desk`, the state dir read-only, only `learn/` writable, no network):
```
systemd-run --wait --pipe --collect --unit=lean-learn-$(date +%Y%m%d%H%M%S) \
  -p User=solana-desk -p Group=solana-desk -p WorkingDirectory=/opt/solana-desk-learn \
  -p Environment=PYTHONDONTWRITEBYTECODE=1 -p UMask=0077 \
  -p ProtectSystem=strict -p ProtectHome=true -p PrivateTmp=true -p PrivateDevices=true -p NoNewPrivileges=true \
  -p PrivateNetwork=true -p RestrictAddressFamilies=AF_UNIX \
  -p ReadOnlyPaths=/var/lib/solana-desk-lean -p ReadWritePaths=/var/lib/solana-desk-lean/learn \
  -p MemoryMax=1G -p CPUQuota=50% -p Nice=10 -p IOSchedulingClass=idle \
  /opt/solana-desk/.venv/bin/python -m lean.learn \
    --lean-db /var/lib/solana-desk-lean/lean.sqlite \
    --paths-db /var/lib/solana-desk-lean/paths.sqlite \
    --config /etc/solana-paper/lean.json \
    --out-dir /var/lib/solana-desk-lean/learn \
    --embargo-s 21600
```
* **Output.** It prints one JSON line: `status`, `proposal_status`, `confirmed`, `candidates`, `enterable`, and the paths of the page and the
  proposal file.
* **Reading it.** Copy `/var/lib/solana-desk-lean/learn/lean-learn-*.html` back to read it. `proposals/*-selection.json` is evidence only;
  nothing reads it.
* **Assumptions.** The command assumes `paths.db` is the default (`<state-dir>/paths.sqlite`). If `lean.json` sets `paths.db`, use that file.
  Archives `paths-*.sqlite` beside it are picked up automatically.
* **`--embargo-s`.** Set it to `paths.path_hours × 3600` (21600 for the default 6 h), so that no mine-window outcome is still open when the
  select window starts.
* **First runs.** With little data the first runs will say `INSUFFICIENT_DATA` / `NO_RULE_FOUND`. That is the expected answer. The production
  minimums are 50 rejected per rule in mine and in select, and 30 in confirm.
* **After the merge.** Once LINT3 is merged into r1 and deployed, use `WorkingDirectory=/opt/solana-desk-lean` and drop the separate copy.

## Limitations (stated, not hidden)

* **PnL is a simulation (EXECUTION_UNVERIFIED).** It uses one isolated position per path (no cash or position-cap contention), constant-product
  quotes on the recorded vaults, and the marks are about 15 s apart. Not modelled: the L10 latency fills and the L11 DANGER exits. Live stops
  run every ≤10 s.
* **Not-entered candidates.** A not-entered `enterable` candidate is simulated at the recorder's `ref_size_sol`. An entered one uses its real
  fill size.
* **The current strategy.** Labels and PnL use the current strategy config for all history. A strategy version change mid-history is not
  segmented.
* **Gap thresholds.** The defaults (25 % and 180 s) are judgement calls. Both are CLI knobs, and every exclusion is counted on the page.
* **Late snapshots.** A rule on enriched or wallet features needs the runner to wait for them before it can be applied at the decision
  (`requires_runner_change`). Any such rule is only as good as `max_late_s` (15 s) allows.
* **Selection bias.** The best of up to 25 mined rules is chosen on one window and confirmed on one look. A proposal still needs a forward paper
  run under a new strategy version, coordinator review and CK's OK. Nothing is ever applied automatically.
* **The e2e worlds do not run the L13 collector.** Its rows are covered by the writer-integration tests, which call the real
  `WalletSignals.collect` on a fake chain. The trader's e2e worlds cover L12F and L07R2.
