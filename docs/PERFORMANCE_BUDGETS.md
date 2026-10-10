# Gate, accounting and deadline budgets (T24R)

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
