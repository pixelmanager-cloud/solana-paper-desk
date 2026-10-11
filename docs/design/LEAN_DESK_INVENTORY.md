# Lean-desk simplification inventory (T44, analysis only)

Status: documentation only. No code, test, config or unit was changed. Paper only; nothing here weakens a rule of `AGENTS.md`.
Base: `origin/integration/r1` at `b89864c` (merge of T43). Everything below is that tree. Branch-only work is NOT in it: the T22 line
(`paper_pass_closure.py`, FAILED_CHARGED), T24R (verified-digest index, chained monitoring accounting), T35/T16G+ (`portfolio_marks.py`),
T16J. Where a branch already fixes something the inventory calls out, it says so.

## 0. Method and size of the thing

* `desk/` is 144 files / 29,251 lines; `tools/` + `discovery/` add about 14,000 more (43,377 lines in the 169 modules analysed).
  `tests/` is 273 files / 65,251 lines / 4,211 `def test_` (static count; neutralised inherited tests inflate it, the coordinator's
  figure for a real run is about 2,900 tests / about 25 min). Test code is 1.5x the production code.
* Import graph: AST of every `desk/`, `tools/`, `discovery/` module, **including function-local (lazy) imports**, closed over the
  entry points of `deploy/fresh/*.service` (`tools.paper_entry_dispatcher`, `tools.paper_scheduler` -> `desk.paper_monitor_service`,
  `desk` CLI, `discovery.continuous`, `tools.ops.*`, `tools.research.*`). 141 of 169 modules are reachable, 86 by eager imports.
  "Reachable" means "loaded"; the run-time column below says what actually executes on a FRESH store.
* Gate cost was profiled on a fresh store holding one real T01 receipt (`tests.test_cycle_no_entry.fixture`): one `terminal.gate`
  costs about 1.6 ms. The problem is not the constant; it is that cost and failure modes scale with retained history (section 4)
  and that one unresolved record stops everything (section 1).
* Hash coupling: `runtime_compatibility.implementation_hash()` reads and hashes 156 files / 3.6 MB under `desk/` in **37 ms per call**.

What a paper desk really needs for data integrity (everything else in this document is measured against this list):
(a) ledger events append-only, idempotent by `event_id`, one config fingerprint (`desk/ledger.py:150-190`);
(b) provider request accounting that can never exceed the allowance and never resets (a counter, not a proof system);
(c) the shared pacing store, Kraken lane at 2 s, never reset (`desk/provider_pacing.py`);
(d) a charged scan is never retried (dedupe by scan id);
(e) the engine's own exit-safety modes (`desk/engine.py`).

## 1. Global-latch machinery

"Global" = one bad record stops every candidate AND every exit until an operator acts. Columns: where / what stops / what it protects /
needed for PAPER integrity? / per-candidate replacement.

| # | Where | What stops | Protects | Needed? | Replacement |
|---|---|---|---|---|---|
| L1 | `desk/paper_cycle.py:537` inserts `paper_observation_passes(id,intent,NULL)`; `desk/paper_history_preparation.py:70`; `desk/paper_observe_cli.py:160`. Gate: `desk/paper_terminal_reconciliation.py:549-588` returns `OBSERVATION_RECOVERY_REQUIRED`; consumed at `paper_cycle.py:493-495`, `paper_observe_cli.py:149`, `tools/paper_entry_dispatcher.py:340-342,386-389,425,769-770` | EVERY later cycle, entry, held exit and dispatch, after any charged non-COMPLETE pass | a charged attempt with no recorded outcome cannot be forgotten or retried | the *charge accounting*: yes. The *global stop*: no | a per-scan terminal row written with the charge: `scan_outcome(scan_id PK, status, reason, charges)`. A crash leaves an in-flight row; the next owner process closes rows older than N minutes as `ABANDONED_CHARGED` (charges stay). No other scan reads it. T22/T22G (branch) already does the retirement half |
| L2 | receipt kinds that only exist to clear L1: `history_preparation_rejection.py` (`MAX_REJECTIONS=512` at :19), `paper_cycle_no_entry.py` (`MAX_ROWS=8192` at :24), `paper_terminal_reconciliation.py` (`MAX_RECEIPTS=256` at :27,:96,:455) | the table filling up is itself a hard stop | nothing beyond L1 | no | none; they disappear with L1 |
| L3 | `paper_terminal_reconciliation.py:112-118` (`> 10000` passes: "Original pass count bound"), `:549-550` (`> 256` pending: "Pending pass bound") | everything, purely by run count (every held pass adds a row) | defensive bound | no | rows per scan, pruned/archived by store rotation; no bound check on the read path |
| L4 | `tools/paper_entry_dispatcher.py:294-295` "Unresolved dispatch; no retry or automatic recovery": an intent without a result | every dispatch forever (on r1 there is no durable hold or terminal checkpoint result; T16H-T22I branches add some) | no duplicate charge for one hint | the no-duplicate rule yes, the stop no | `intents(hint_id PK)`; result row or auto-close after N minutes; other hints proceed |
| L5 | `paper_entry_dispatcher.py:330-366` `_preflight` (mirrored in `_held_guard` :380-390 and `_rejection` :420-440): `Reviewed context changed`, `Held-position priority`, `Observation recovery`, `Monitoring pending or blocked`, `Provider pacing pending`, `Context file identity changed`, `Source/config context changed` | the whole tick (a raise, not a skip) | context = same stores/config/code as reviewed | identity: no (section 2); the rest are *scheduling* rules | return `SKIP(reason)` for this tick, log, continue; held-before-entry stays a scheduling order, not a latch |
| L6 | `desk/monitoring_budget.py`: `blocked='SOURCE_FAILURE'` set at :662 (`UPDATE ... blocked`), read at :485,:632; `MONITORING_OUTCOME_PENDING` :502,:633; `MONITORING_CLOCK_ROLLBACK` :257,:496,:631; `MONITORING_ACCOUNTING_INVALID` :345-387,:590; `MONITORING_CONTEXT_BINDING_INVALID` :220,:328,:379-381 | held exit quote reads AND entries | the allowance cap; tamper evidence of past receipts | the cap: yes. The proofs: no | a rolling counter (`reads in last hour < cap`) incremented in the same transaction as the read; a failed read is one more charged row; no blocked flag. T25F (merged) narrowed what latches, T24R (branch) makes the proof incremental |
| L7 | `desk/provider_pacing.py`: pending ticket / waiters (`state.pending`, :630,:659), `PACING_DATABASE_INVALID` (:127,:225,:258,:696), `PACING_QUEUE_FULL` :480, `PACING_CLOCK_INVALID` :453-468, `reclaim_orphans` :371; dispatcher raises `Provider pacing pending` at :360 and :437 | entries until the owner process is provably gone | provider rate limits (the Kraken 2 s lane must stay shared and never reset) | pacing itself: yes | keep the pacer; an orphan older than a bound is reclaimed automatically; a busy pacer makes the tick wait/skip, never raise. T23F/G, T36 (merged) did most of this |
| L8 | `desk/ledger.py:159,163,166,169,174,182` and `desk/paper_checkpoint.py:92-112` (`CHECKPOINT_MISSING`, `EVENT_JOURNAL_INCOMPLETE`, `EXPERIMENT_IDENTITY_MISSING/INVALID`, `RUNTIME_IDENTITY_INVALID`) | the ledger: no event can be applied | accounting integrity | yes except the code-version part (`:171-174`, `:112`) | keep checkpoint/journal/config checks; drop the code-hash comparison (section 2) |
| L9 | `desk/paper_scheduler.py:10-30`: `DESK_PAPER_SCHEDULER_IDENTITY` must equal the lock file's `st_dev:st_ino`, mode 0600, nlink 1 (set in 5 units) | all scheduled work if the lock file is ever recreated | one writer at a time | serialisation: yes; inode pin: no | `flock` on a fixed path |
| L10 | `desk/engine.py` exit paths: `exit_blocked` (:222,230,263,296,316,324,342), sticky `EXIT_ONLY` (:139-148,:449-450; opt-in recovery :109-125, T23), `ENTRY_PAUSED`, `LIQUIDATING` | entries (by design) until cleared | position safety | yes | keep; make EXIT_ONLY recovery default |
| L11 | `desk/evidence.py:29` `Evidence storage budget exhausted` (256 MiB default, `:8`), `history_preparation_rejection.py:100-122` (`count>4096` pages: "Preparation attempt inventory bound") | every save / every gate | disk bound | a disk guard: yes. As a hard raise on the gate path: no | rotation warning at 80 % + new store set (T24R branch: warning and windowed scan) |
| L12 | `paper_entry_dispatcher.py:38,750-751` `MAX_DISPATCHES=4096` ("capacity exhausted; no reset"), journal `_bounded` :195 | all dispatch after 4096 intents | journal size | no | rotate the journal with the store set |
| L13 | `tools/ops/entry_latch.py` (first-BUY latch) | new entries after the first BUY | experiment protocol, not integrity | operational choice | keep as an operator control, off the gate path |

Verdict: L1-L4, L6 (the proofs), L11-L12 are the ones that turn a single failed or unlucky candidate into a dead desk. Their only legitimate
job (charges stay charged, no duplicate) is a per-scan row plus a counter.

## 2. Identity pinning

| Pin | Defined | Enforced | What breaks on any byte change under `desk/` |
|---|---|---|---|
| `implementation_hash()` | `desk/runtime_compatibility.py:19-22` (every `.py`/`.json` under `desk/`, 156 files, 37 ms) | `ledger.py:151,171-176` (`implementation changed or unversioned: explicit reviewed transition required`), `paper_checkpoint.py:84-112` (`RUNTIME_IDENTITY_INVALID`), `paper_cycle.py:114-118` (`SAVED_IMPLEMENTATION_MISMATCH`), `:532`, `paper_checkpoint.py` (`CONFIG_IMPLEMENTATION_MISMATCH`), `monitoring_budget.py:233-237,332` (`code_hash`), `paper_terminal_reconciliation.py:428,600`, `monitoring_handoff/successor`, `paper_view.py:11,126-161`, dispatcher `tools/paper_entry_dispatcher.py:162,364,380,424` | every cycle refuses until a reviewed transition receipt exists or the ledger is rotated. A comment-only edit counts |
| runtime successor / continuation / extension chain | `desk/runtime_compatibility.py:134-215` dispatching into `runtime_continuation.py`, `runtime_extensions.py`, `runtime_empty_history_successor.py`, `runtime_performance_continuation.py` (about 900 lines) | `require_runtime` (`runtime_compatibility.py:134-143`) on every ledger apply and checkpoint read | only relevant when a position is open across a code change |
| pin files | `config/*.json`: `runtime-*.json`, `monitoring-*.json`, `paper-*-retirement.json`, `paper-migration-recovery.json` (57 KB), `runtime-empty-history-successor.json` (41 KB), ... = **180 KB** of hand-reviewed pins | read by the modules above | a new release needs new pins; the repo rule since the fresh start is "rotate instead" |
| dispatcher context | `tools/paper_entry_dispatcher.py:124-165` `plan()`: per-path `(path, st_dev, st_ino)` (`_identity` :86-88), `config_hash`, `source_hash`, `tool_hash` (hash of the dispatcher file), `entry_tool_hash`, discovery digest; frozen into the journal by `activate` (:325) | `_preflight` :334,:364; `_held_guard` :380; `_rejection` :424 | replacing, restoring or copying any store file (inode), editing the dispatcher or `paper_cycle_cli`, or changing config makes every dispatch raise |
| config fingerprint | `Ledger.apply` `ledger.py:164-169` (`digest(cfg)` + saved canonical config) | every apply | config change needs a new ledger (this one is reasonable: it is part of the experiment's identity) |
| scheduler inode | `paper_scheduler.py:17-25` | each scheduled run | see L9 |

What "record the code version per event" would need: add `code_version` (git sha, or a hash of the *event-producing* modules only) to
each ledger event/outcome and to each scan row; keep ONE `config_hash` in ledger metadata; drop `implementation_hash` from
`Ledger.apply`, `read_checkpoint`, the dispatcher context and the monitoring budget; delete the runtime_* chain and its 180 KB of pins.
Replay stays deterministic because the engine is pure and events carry their inputs; auditability improves (each fill names the code that
made it) instead of "the whole desk stops". Cost: a code change no longer *proves* nothing else changed; that guarantee is exactly what
costs a rotation per release today.

## 3. Legacy reconciliation / retirement / successor modules

All of these exist because the pre-fresh-start production stores had charged-but-unresolved incidents. The decision of 2026-10-10 archives
those stores read-only and starts new ones. On a fresh store every table these modules guard is absent, so they are loaded and called
(`terminal._gate` :514-589 calls each hook every time) and return `None`.

Legend: D = delete (nothing on the fresh path needs it; old stores are archived and read with plain SQLite); R = replace by the per-scan model
of section 1, keep until the replacement exists; K = keep.

| Module | Lines | Own tests (files / defs) | Callers (file:line) | Verdict |
|---|---|---|---|---|
| `desk/common_bank_completion_inventory.py` | 672 | `test_common_bank_completion_inventory` 34 | none outside the family | D |
| `desk/common_bank_journal.py` | 1220 | `test_common_bank_journal` 45 | `common_bank_completion_inventory:17`, `..._replay:13` | D |
| `desk/common_bank_journal_replay.py` | 224 | 29 | `common_bank_journal:1219` | D |
| `desk/common_bank_receipt_chain.py` / `_format.py` / `_reader.py` / `common_bank_view.py` | 360 / 208 / 221 / 223 | 34 / 24 / 34 / 25 | only each other | D (the whole "common bank" family: **3,128 lines + 192 test defs, disconnected, no operational caller**) |
| `desk/paper_http403_retirement.py` | 242 | `test_paper_http403_retirement` 7 | `paper_terminal_reconciliation:536,543,576`, `paper_preparation_retirement:251`, `paper_dispatch_preparation_retirement:293,328` | D |
| `desk/paper_preparation_retirement.py` | 284 | `test_paper_preparation_retirement` 28 | `paper_terminal_reconciliation`, `paper_cycle_no_entry:18`, ... (also supplies `_schema_bounds`, `_ledger_originals` used by T01 modules) | D after moving `_schema_bounds`/`_ledger_originals` (about 40 lines) |
| `desk/paper_dispatch_preparation_retirement.py` | 390 | `test_dispatch_preparation_retirement` | `paper_terminal_reconciliation:538,545,582`, `paper_migration_no_entry:401,434,473,665`, dispatcher | D |
| `desk/paper_intake_uncaptured_retirement.py` | 256 | `test_intake_uncaptured_retirement` | `paper_terminal_reconciliation:516` (gate), `paper_migration_no_entry` | D |
| `desk/paper_empty_history_reconciliation.py` | 214 | `test_empty_history_*` | `paper_terminal_reconciliation:53,106,251`, `runtime_empty_history_successor` | D |
| `desk/runtime_continuation.py` / `runtime_extensions.py` / `runtime_empty_history_successor.py` / `runtime_performance_continuation.py` | 182 / 253 / 211 / 260 | 13 / 8 / `test_empty_history_successor` + `_lifecycle` (not counted in the totals) / 7 | `runtime_compatibility:138-162` | D (with section 2) |
| `desk/monitoring_handoff.py` / `monitoring_successor.py` | 331 / 231 | 16 / 12 | `monitoring_budget:210,323,369,488-490,620-622`, `paper_terminal_reconciliation:369-394,560` | D (budget carried across ledgers; a fresh store starts a fresh budget) |
| `tools/verify_http403_originals.py` / `tools/verify_kraken_successor_originals.py` | 142 / 165 | 16 | none | D |
| `desk/paper_terminal_reconciliation.py` | 644 | `test_paper_terminal_reconciliation` 28, `test_terminal_gate_byte_reuse`; 32 test files mention it | `paper_cycle:37`, `evidence:39`, `paper_market_adapter:214`, `streaming_history:17`, `paper_observe_cli:145`, dispatcher | R (it IS the global gate; helpers `_load`, `_wire`, `_classification` are reused by the T01 modules) |
| `desk/paper_migration_no_entry.py` | 693 | covered by `test_migration_no_entry` | `paper_terminal_reconciliation:519`, dispatcher | R (typed no-entry for an unsupported-quote migration; the "historical journal continuation" half is legacy) |
| `desk/history_preparation_rejection.py` (276), `desk/paper_cycle_no_entry.py` (273) | 549 | `test_history_preparation_phase`, `test_cycle_no_entry` | `paper_cycle:507,543,702` | R (T01: receipts that only clear L1) |
| `desk/runtime_compatibility.py` | 282 | `test_runtime_compatibility` | `ledger:171,221`, `paper_checkpoint:107`, `paper_cycle:116`, `monitoring_budget:233` | R (becomes "write code_version", about 20 lines) |
| `desk/kraken_pacing_migration.py` | 89 | none | `provider_pacing:232,473` | K until the shared pacing DB is rebuilt; it is the proof that the Kraken lane exists. Do not touch while the shared DB is live |
| `desk/_migration_decline_legacy_v1.py` | 126 | via `test_migration_no_entry` | `paper_migration_no_entry:133` | D with the previous row |

Totals. D = 20 modules, **6,289 lines**, 18 own test files, **4,679 test lines, 349 test defs**; 47 further test files (754 defs) mention a deleted
or replaced module and need edits, not deletion. R = 5 modules, 1,834 lines, 921 test lines. Config pins: 180,298 bytes in 15 files. 53 docs mention the
retirement tooling (`docs/ops`, `docs/reviews`, ...).

## 4. Full re-verification on every gate

`terminal.gate` is called at least 8 times per dispatch: `tools/paper_entry_dispatcher.py:340,386` (once per acquisition RPC and intake call through
`_held_guard`), `:425`, `:769`, plus `paper_cycle.py:493`.

| What is re-proved | Where | Cost class | Note |
|---|---|---|---|
| every retained history-preparation rejection, including a scan of ALL evidence pages | `history_preparation_rejection.py:243-` `gate`, `_attempts` :100-122 (decodes every `pages` row, hard raise at `count>4096`) | O(R x P); measured 0.17 s + 0.35 ms per unrelated page; latches at 4097 pages | T24R (branch): index + sample + windowed scan |
| every retained cycle no-entry receipt (intent, result, attempts, ledger anchor, live charge) | `paper_cycle_no_entry.py:239-` `gate`, `_proof` :126, `_index` :67 | O(R x refs), R up to 8192 | T24R (branch): index + sample |
| every legacy receipt (`_rows`, `http_rows`, `preparation_rows`, `dispatch_rows`) with ledger lock + proof | `paper_terminal_reconciliation.py:549-589` | O(receipts) loads, none on a fresh store | D in section 3 |
| every original pass (`_passes`: schema, 10,000-row scan, scalar checks) | `paper_terminal_reconciliation.py:112-118`; `history_preparation_rejection.py:28` | O(passes), up to 10,000 | |
| monitoring accounting: every completed reservation's evidence blob loaded, hashed, receipt-checked | `monitoring_budget.py:345-387` (`_accounting`), called from `snapshot` :629-, `retain_outcome`, `reserve_read`, and `terminal._monitoring` :360- | O(N) blob loads per call, about 5 passes per held read; measured 0.07 s at 50 rows, 0.136 s at 400 | T24R (branch): hash chain |
| the whole desk source | `implementation_hash()` `runtime_compatibility.py:19`, in `_preflight` :364, `_held_guard` :380, `_rejection` :424, `plan` :162 | 37 ms x (4-10 per dispatch) | section 2 |
| dispatcher journal | `tools/paper_entry_dispatcher.py:300-` `_journal` + `_bounded` :195: every intent/result JSON re-parsed and re-hashed | O(intents <= 4096) per dispatch start | |
| ledger anchors, config, live charge, pacing database | `terminal._ledger` :140-, `_pacing` :130-, `_context` :121-, per receipt | O(receipts) opens of the ledger and the pacing DB | |

All of it re-proves content-addressed, append-only data that was already proved when it was written. A lean desk writes the proof result
once (`verified=1`) and re-checks only new rows.

## 5. Minimal core

Closure of the operational flow with the legacy and gate modules blocked (static graph, lazy imports included): **65 modules, 13,493 lines**,
against 86 modules / 19,630 lines for the same entry points with the gates. The edges that drag the legacy machinery into the core are few:

* `desk/ledger.py:171,221`, `desk/paper_checkpoint.py:107`, `desk/paper_cycle.py:116`, `desk/paper_view.py:132`, `desk/monitoring_budget.py:233` -> `runtime_compatibility`
* `desk/monitoring_budget.py:210,323,369,488-490,620-622` -> `monitoring_handoff` / `monitoring_successor`
* `desk/evidence.py:39`, `desk/paper_cycle.py:37`, `desk/paper_market_adapter.py:214`, `desk/paper_observe_cli.py:145`, `desk/streaming_history.py:17` -> `paper_terminal_reconciliation` (mostly for `_load`/`_wire` helpers)
* `desk/paper_cycle.py:507,543,702`, `desk/paper_history_preparation.py:6` -> `history_preparation_rejection`, `paper_cycle_no_entry`
* `desk/provider_pacing.py:232,473` -> `kraken_pacing_migration`

Core, by stage (module lines in parentheses):

* discovery: `discovery/continuous.py` (426)
* screening: `decode` (256), `programs` (163), `pools` (164), `parsed_v1` (164), `graduation_witness` (126), `bundles` (182), `security` (154), `strategy` (213), `live_strategy_features` (305), `live_observation` (317), `paper_market_adapter` (283), `paper_observation_collector` (261), `paper_history_source` (117), `paper_history_preparation` (83), `history_progress` (308), `replay_history`, `streaming_history`, `account_history`, `funding`, `launch`, `ownership_*`
* quotes: `paper_read_sources` (424), `providers` (224), `original_byte_read_transport` (220), `original_byte_slot_transport` (175), `sol_usd_observation` (194), `kraken_usd_observation` (167), `quote_execution` (344), `pool_vault_admission` (371), `pool_receipt_ledger` (415), `coordinator_rpc` (157), `provider_pacing` (724)
* engine and accounting: `engine` (609), `model` (302), `ledger` (238), `paper_checkpoint` (396), `paper_view` (173), `fill_realism` (225), `sell_fees`, `dynamic_fees`, `fee_config`, `regime` (146), `regime_producer` (96)
* cycle, held, exits: `paper_cycle` (713), `paper_cycle_cli` (177), `paper_observe_cli` (292), `paper_monitor_service` (79), `paper_monitor_operator`, `paper_target_export` (209), `paper_exit_adapter`, `paper_concurrency` (171), `monitoring_budget` (822), `job_persistence` (365), `allowance_policy`
* reports and ops: `tools/ops/*`, `tools/research/*` (already outside `desk/`), `tools/paper_entry_dispatcher.py` (875), `tools/paper_scheduler.py` (118)

Second tier (inside the "core" but still over-built; candidates for the next pass, not this one): `monitoring_budget.py` (822: owner proofs from `/proc/locks`,
chained receipts), `paper_checkpoint.py` (396), `pool_receipt_ledger.py` (415), `pool_vault_admission.py` (371), `job_persistence.py` (365),
`history_progress.py` (308), `paper_cycle.py` (713: 20+ blocker codes, intent/outcome journalling).

## 6. Size

| Item | Production lines | Test lines | Test defs |
|---|---|---|---|
| D set (20 modules incl. the 7 `common_bank_*`) | 6,289 | 4,679 | 349 |
| R set (5 modules; replaced by about 200-400 new lines) | 1,834 | 921 | 59 |
| Tests that mention D/R modules and need editing | - | - | 754 (47 files) |
| `config/*.json` pins | 180 KB | | |
| Runtime effect on the gate path | 1.6 ms (fresh) -> O(history) later; 4-10 x 37 ms hashing per dispatch | | |

So about 6.3 k of 29 k `desk/` lines (22 %) and about 4.7 k test lines can be removed with no fresh-path behaviour loss, and about 1.8 k more replaced.
Bypassed (not deleted) in the first step: the gate hooks for L1/L2 (a `paper_lean=1` config switch), which takes the 8 gate calls per dispatch to near zero.

Risks:
1. Losing tamper evidence for the archived stores. Mitigation: they are read-only archives; verification tools (`tools/ops/verify_backup.py`, byte copies) remain.
2. The per-scan model must write the charge and the terminal/in-flight row atomically (one SQLite transaction with the budget counter), or "charged requests stay charged" breaks.
3. Parity: the lean path must produce the same decisions and fills as the current one on the same inputs. The T27 parity harness (`tools/research/shadow_strategies.py` parity modes) and the golden digests (`tests/test_portfolio_risk.py`) are the starting oracles.
4. The shared pacing DB and the discovery DB stay shared (decision 2026-10-10); nothing here may reset them.
5. Deleting 754 test defs' worth of edits at once is a review hazard: do it in waves.

## 7. Migration path (current desk keeps running)

1. **Freeze** the legacy path (bug fixes only). Land the measurement hooks first: `code_version` column proposal (section 2) as an extra, optional field.
2. **Write the per-scan model** (`scan_outcome`, in-flight/auto-close, counter in the same transaction) as a new module beside the old one, behind a config
   key (`paper_lean_version`: 1). It consumes the same discovery DB, the same pacer, the same `engine.transition` and `quote_execution`; it never calls `terminal.gate`.
3. **Shadow run** on a separate store root (the fresh-start layout, T13 `fresh_start` creates it) fed by the same candidate stream for N days; compare scan by scan
   against the legacy decisions (the funnel report T29 already classifies where candidates die; T40/T41 features and daily report give the comparison surface).
4. **Cut over** with the existing cutover tool (T11F): new store set, lean units, legacy units left disabled and the old roots archived read-only.
5. **Delete in waves**, each wave a PR with its tests: (a) `common_bank_*` + `tools/verify_*_originals` + pins they own (no behaviour change); (b) the retirement/reconciliation
   modules and their gate hooks; (c) the `runtime_*` chain and `implementation_hash` coupling; (d) `monitoring_handoff/successor`; (e) the T01 receipt modules once nothing reads them.
6. **Second tier** (section 5) only after a clean forward observation period.

Order of risk: (a) none, (b) low, (c) medium (touches `ledger.py`, the one file every number depends on), (d) low, (e) after L1 is gone.
