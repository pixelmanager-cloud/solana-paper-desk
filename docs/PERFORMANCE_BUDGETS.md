# Gate, accounting and deadline budgets (T24R, T24S)

Paper only. Everything here is cost control for the global terminal gate, monitoring accounting and the cycle read deadline. No
evidence gate was weakened: a missing, extra or disagreeing record still fails closed.

## F7 — gate cost no longer grows with retained history

| before | after |
|---|---|
| every gate call replayed EVERY retained rejection / no-entry receipt in full | a receipt is replayed in full when it has no row in `paper_verified_receipts` (new, or published by older code), plus a bounded deterministic sample (`verified_index.SAMPLE` = 8 per kind and call, a function of the number of passes so it moves with every pass); the others get a cheap binding check (outcome page still loads and hashes to its key, identity fields and index digest still agree) |
| each replay scanned EVERY evidence page (`_attempts`) and raised `Preparation attempt inventory bound` above 4096 pages | the attempts of a preparation are read from the pages written between its intent page and its outcome page (rowid window); no store-wide page-count latch |

* `paper_verified_receipts(id, kind, pass_id, outcome_hash, proof_digest)`: trigger-guarded (no UPDATE/DELETE, contiguous ids, unique per
  kind and pass, at most 16384 rows), schema compared byte for byte, written in the SAME transaction as the receipt after its full
  proof passed (`history_preparation_rejection.publish`, `paper_cycle_no_entry.publish`). An index row without a receipt, or one that
  disagrees with it, fails the gate. Deleting an index row only costs a full replay.
* `DESK_GATE_FULL_REPLAY=1` (or `gate(..., full=True)`) replays everything, as the gate always did. With at most `SAMPLE` receipts of a
  kind (every store in the existing tests) the behaviour is identical to before.
* Documented boundary: an original attempt page that is altered AFTER publication is not re-read for an indexed receipt outside the
  sample until it is sampled (the sample covers every receipt over time) or a full replay is run. The outcome page itself is always
  re-hashed. The window scan relies on insertion order of `pages` rowids; a renumbered store fails closed ("charged attempt missing").
* Rotation warnings (80 %): `paper_cycle_no_entry.warn_if_crowded` writes a `rotation_warning_v1` row for `evidence_pages` (advisory
  limit `PAGE_SOFT_LIMIT` = 100,000 pages) and `evidence_bytes` (the store's own write budget, 256 MiB) into the `.publish-refused.jsonl`
  beside the evidence store (`read_refusals`) and the log. The EvidenceStore write budget is unchanged: at 100 % writes still fail closed.

## F14 — monitoring accounting is incremental

`paper_monitor_accounting_chain(seq, reservation_id, chain_hash)` (deliberately not `paper_monitoring_*`: the terminal gate whitelists
that prefix). `retain_outcome` appends the chain link in the same transaction as the outcome, after the full accounting proof of that
row. `_accounting` recomputes the whole chain from SQL scalars (no evidence loads; a changed, deleted or reordered reservation/outcome
or chain row changes the hash), re-proves from blobs only rows without a chain link and `CHAIN_SAMPLE` = 2 chained rows chosen
deterministically from the chain head. Rows from older stores (no chain) are proved from their blobs every time, as before.

## F9 — whole-pass deadline

`paper_cycle_deadline_seconds` (opt-in config key, absent = the historical 10 s, byte-identical) must be an int in `DEADLINE_RANGE`
(10..20) or the config is refused at load. Per-request timeouts and the collector deadline are clamped to 15 s (the sources refuse more).
Worst case of one cycle's reads, `worst_case_seconds` = non-Kraken reads x (max(helius 0.1, jupiter 0.25) + L) + Kraken reads x (2.0 + L),
with L the response time:

| pass | reads (non-Kraken + Kraken) | L = 0.5 s | L = 1.0 s | L = 1.5 s | L = 1.78 s |
|---|---|---|---|---|---|
| entry | 8 + 1 | 8.5 s | 13.0 s | 17.5 s | 20.0 s |
| entry with USD v1 | 6 + 1 | 7.0 s | 10.5 s | 14.0 s | 16.0 s |
| one held leg (estimate) | 5 + 1 | 6.2 s | 9.2 s | 12.2 s | 13.9 s |

So the default 10 s holds for L <= ~0.6 s; 20 s covers L <= ~1.78 s. The read counts come from the preparation reserve (9, or 7 with
USD v1); the held-leg count is an estimate from the call sites, not a measurement. A longer deadline does not relax the 10 s price/quote
freshness rules: reads older than that at the decision are rejected as stale (allow-listed, no latch). Units: held `TimeoutStartSec=120`
must hold `max_positions x 20 + 20`; entry 600 holds 18 + 2 + 20 + margin (tests read the rendered units). An overrun is
`CYCLE_DEADLINE_UNAVAILABLE`, which is on the T22G allow-list: the pass closes FAILED_CHARGED, charges stay charged, nothing latches.

## Benchmark

`python -m tools.research.bench_history_scale --scales 1,10,30` (see the module docstring for the fixture assumptions; `--days 7` is the
7-day profile). Cold, per call, same harness on the base commit and on this branch (1x = 150 unrelated pages + 30 monitoring reads):

| scale | gate loads before -> after | gate s before -> after | snapshot blob loads | next read blob loads |
|---|---|---|---|---|
| 1x | 167 -> 17 | 0.33 -> 0.19 | 30 -> 2 | 92 -> 8 |
| 10x | 1517 -> 17 | 0.72 -> 0.20 | 300 -> 2 | 902 -> 8 |
| 30x (4505 pages, 900 reads) | LATCH `Preparation attempt inventory bound` -> 17 | - -> 0.21 | 900 -> 2 | 2702 -> 8 |

7-day profile on this branch (`--days 7`: 33,605 pages, 4,032 monitoring reads; the base commit latches at 4,096 pages so it cannot run it):
gate 17 loads / 0.19 s, snapshot 2 blob loads / 0.12 s, next read 8 blob loads / 0.42 s. Blob loads are constant; the remaining growth
(next read 0.15 s at 30 reads -> 0.42 s at 4,032) is the chain recomputation over SQL scalars, linear in reads but about 0.1 ms per
read. Planning assumptions, not measurements: 240 candidates/day at ~20 pages, 576 held reads/day.

## T24S — the gate and the monitoring chain are O(new)

T24R left two costs that grew with the number of COMPLETED PASSES (288+ a day): the global gate decoded the outcome page of every
completed pass in two different gates (17 + 2 x passes page loads), and every accounting call recomputed the whole monitoring chain.

* **Pass inventory** (`desk/pass_inventory.py`, table `paper_pass_inventory`): for every completed original pass the scalars a gate
  classifies its outcome page by (`kind`, `scan_id`, `intent_hash`). Written by the two receipt publishers in the SAME transaction as
  the outcome, and lazily (best effort, own connection, no waiting) by the first gate call that meets a completed pass nobody indexed
  (outcomes written by other code or by older code). Append-only (no UPDATE/DELETE, id = count + 1, at most 16384 rows), schema compared
  byte for byte. A gate call loads pages only for passes that are NEW since the last call plus `SAMPLE` = 8 pseudo-random rows and the
  newest 2 (re-classified from the retained page and compared with the row); everything else is SQL scalars: every row must join to a
  pass with the same intent and outcome hash, ids contiguous, the row digest of each listed receipt-kind row. A missing, extra, altered or
  stale row fails closed; `full=True` / `DESK_GATE_FULL_REPLAY=1` re-classifies every pass.
  The first call on an existing store classifies every completed pass once (about 1 ms each: ~6.6 s at 6,500 passes); later calls are flat.
* **Receipts**: of the indexed receipts that are not replayed, `verified_index.QUICK_SAMPLE` = 8 get the page-level binding check per
  call; the rest get a primary-key presence check of their outcome page (nothing decoded). Their binding to pass, scan, intent and outcome
  is the index digest `plan()` already compares.
* **Monitoring chain head** (`paper_monitor_accounting_chain_head`, one row): "every link up to `seq` was verified". It moves forward
  only, and only onto the stored hash of an existing link (triggers). An accounting call recomputes just the links after it, a sample
  of 16 older links from their SQL scalars, and re-proves `CHAIN_SAMPLE` = 2 chained reservations from their blobs; the chain is
  extended and the head moved in the same transaction as the outcome. Reads that no link covers (older stores, abandoned rows) are
  proved from their blobs every time, as before. A stored head without its chain, or beyond it, is refused. `DESK_CHAIN_FULL_VERIFY=1`
  recomputes everything. A window total (`window_used`) is a SQL scalar count over the reservations; `total_used` and `high_water` are
  the stored running totals of `paper_monitoring_budget`.
* **Trade-off, stated plainly**: a row that was verified once and is then altered (a restore, a manual edit) is no longer re-hashed on
  every call; it is found when it is sampled (every row over time), by `DESK_GATE_FULL_REPLAY=1` / `DESK_CHAIN_FULL_VERIFY=1`, or by the
  coordinator's full verification. New rows are always verified.
* **Warnings**: `tools/ops/healthcheck.py` now reports WARN at 80 % of the rejection cap (512), the no-entry table (8192), the closure
  rows and original passes (10,000), evidence pages (100,000) and evidence bytes (256 MiB), for every `publish_refused_v1` /
  `rotation_warning_v1` row of `<evidence>.publish-refused.jsonl`, malformed lines, and a log that is 80 % full.
* **Ceiling**: the original-pass ceiling of 10,000 (`terminal._passes`) is unchanged and is now the binding limit: 288 passes a day
  reaches it in about 34 days. Rotate while flat before then (the healthcheck warns at 8,000). The benchmark refuses more than 9,000
  passes for that reason.

### Worst case of one pass (F9 revisited): the 18-read maximum, pacer contention, gate cost

The 9-read (7 with USD v1) figures of the table above were the entry's RESERVE, not a bound. A scan is admitted with a ceiling of 18
charged requests (`history_progress.admit`), at most one the shared Kraken SOL/USD read, so one pass may spend 17 + 1 reads.
`worst_case_seconds(pass, L, contenders)` = 17 x (c x 0.25 + L) + 1 x (c x 2.0 + L), c = 1 + other units queued on the shared pacer:

| L (response time) | no contention | 1 other unit | 2 other units (`CONTENDING_UNITS`) |
|---|---|---|---|
| 0.0 s | 6.2 s | 12.5 s | 18.8 s |
| 0.5 s | 15.2 s | 21.5 s | 27.8 s |
| 1.0 s | 24.2 s | 30.5 s | 36.8 s |

So a MAXIMUM pass fits the default 10 s only when responses take <= ~0.2 s with no contention, and the 20 s ceiling only when they
take <= ~0.76 s; with a contended pacer even L = 0 exceeds 10 s. A typical pass reads far fewer than 18, and an overrun is the
allow-listed `CYCLE_DEADLINE_UNAVAILABLE` (FAILED_CHARGED, charges kept, nothing latches), so these are bounds, not predictions. Do
not widen the deadline on the strength of this table alone: measure real passes first.

Unit step bound = contended reads + `GATE_CALLS` x `GATE_SECONDS_BUDGET` (entry 5 calls, a held leg 1; budget 1.0 s per call, the
measured warm gate is <= 0.4 s). The held unit (`TimeoutStartSec=120`) must hold `max_positions x (20 + 1) + 20` = 104 s with the
config's 4 positions; the entry unit (600) holds 18 + 2 + 20 + 5 + 60. The decisions unit (60 s) does no provider I/O
(`PrivateNetwork=true`), so it has no pacer contention and no read deadline; its own waits are the journal init (5 s) and the writer
`busy_timeout` (15 s), 20 s in all, and `tests/test_cycle_deadline.py` asserts they fit with margin.

### Benchmark (T24S)

`python -m tools.research.bench_history_scale --scales 1,10,30 --passes-per-scale 200 --receipts-per-scale 16 --pages-per-scale 150
--reads-per-scale 20 --fresh --check` grows PASSES, RECEIPTS, pages and monitoring reads together, runs EVERY SCALE IN A FRESH
INTERPRETER and exits 1 unless the loads are flat (and the seconds bounded). Receipts beyond the first are structurally real clones
(own pass, scan, intent page, outcome page, receipt, verified-digest row, inventory row) whose FULL replay is stubbed (it needs a real
preparation history each); the binding checks the gate does for them are real, and the replay count is reported.

| scale | passes | receipts | pages | cold gate (upgrade) loads / s | warm gate loads / s | replays | one more pass: loads | snapshot blob loads | next read blob loads |
|---|---|---|---|---|---|---|---|---|---|
| 1x | 218 | 17 | 589 | 240 / 0.38 | 42 / 0.16 | 8 | 50 | 2 | 8 |
| 10x | 2,162 | 161 | 5,827 | 2,051 / 2.16 | 42 / 0.19 | 8 | 51 | 2 | 8 |
| 30x | 6,482 | 481 | 17,467 | 6,051 / 6.61 | 42 / 0.20 | 8 | 51 | 2 | 8 |

(The same probe on the T24R code at 1,000 completed passes: 2,017 page loads, 1.65 s, i.e. 17 + 2 x passes; on this branch the warm call is 25 loads.)
