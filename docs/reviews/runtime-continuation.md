# Bounded existing-experiment runtime continuation

Base: `b414dbd175042e20eb17a50fd65e396d495820fd`. Maintenance only: no token/event semantics, provider calls, ownership/entry approval or deployment.

## Contract

The existing resolver accepts native identity or one immutable v1 transition. This change adds exactly one immutable `paper_runtime_continuation` receipt; it never replaces original ledger metadata/config, state, events/outcomes, first receipt, monitoring handoff/grants, research usage, provider pacing, pending attempts or latches. Existing monitoring readers continue to bind the ledger's original source to the immutable handoff origin.

`config/runtime-continuation.json` is outside the desk hash domain and ships with **zero approved continuations**. The coordinator must separately review/populate the final composed successor pin. Each pin binds exact first-receipt hash, predecessor, successor, unchanged configuration hash and canonical research/evidence/ledger paths. The production target is first receipt `104329395b99a60b1da22f8085c8fd2832510570b3c4e875937dc0aa6ba6df46`, predecessor `31b249e1dfbb16c12ec89235a7137afab4de989fd05ded52a0231ebdfb9667ae`; final successor is deliberately unpinned here. The original first-edge allowlist must remain available and independently valid.

Explicit `python -m desk.runtime_continuation` accepts `--research-db`, `--evidence-db`, `--ledger-db`, `--config`, `--first-receipt-hash`, `--predecessor`, `--successor`. It holds existing research-worker -> evidence-invocation -> ledger-cycle canonical locks; validates actual admitted/sealed research/evidence binding; then uses one ledger `BEGIN IMMEDIATE` transaction. Only the new table, three exact immutable guards, and one receipt are added. It validates the original receipt/schema/guards/pins/history and current checkpoint before appending. Post-DDL validation failure rolls back the whole addition. Exact replay validates again and performs no SQL data mutation. Config changes, missing/ambiguous pins, conflicting receipt, partial schema, extra runtime tables and third hops reject.

The shared writer/read resolver validates the unchanged first receipt at its declared reviewed predecessor and validates the continuation at the actual requested running source. Both receipt anchors are checked independently against bounded contiguous journal prefixes; original snapshot reconstruction and full suffix grammar remain mandatory. The second receipt additionally binds the full original metadata, current checkpoint and current event/outcome prefixes. This is **local coordinator authorization**, not cryptographic authentication of deployed source, provider observations, CPI success or ownership.

## Old-binary fence and preserved evidence

The actual archived deployed `31b249` resolver expects exactly the first transition table. The added `paper_runtime_continuation` table causes that unmodified resolver to refuse. Executable tests use its compiled source, not a mock or guessed namespace behavior: verified-checkpoint read, duplicate ledger apply, full monitoring reservation and PaperReadSources RPC entry all fail without mutation; patched credential lookup/opener prove no credential/provider IO. A legitimate pacing-path environment lookup is permitted and retained.

Synthetic databases first initialize through genuine `0fe08c` compiled source, activate the existing separate-experiment handoff, then record the first transition through genuine `31b249` compiled source. Tests preserve the first receipt bytes and all other databases' SQL dumps. An additional test retains an open paper position and charged unresolved monitoring reservation before continuation, verifies preservation and exact replay, and independently corrupts the later continuation-only prefix. Pending outcomes are neither completed nor retried by this module. Old unmodified code cannot safely service the transitioned ledger; coordinator deployment must quiesce old workers and use the reviewed successor.

## Fixture provenance

Two new deterministic archives contain only tracked `desk/**/*.py` and `desk/**/*.json` Git blobs from the listed commits. No public original fixture changes or private/production databases. To reproduce, enumerate `git ls-tree -r --name-only <commit> desk/`, filter `.py/.json`, retrieve `git show <commit>:<path>`, and write lexically ordered ZIP_DEFLATED level9 members with timestamp1980-01-01 and external mode0100644.

| Archive | Source commit | SHA256 | Bytes |
|---|---|---|---:|
| `runtime-continuation-origin-desk.zip` | `e4badabe0ce850ceec59e4ae25aa1e7e0c266efb` | `486a29d66470a340f5171d262cdeef14fe9a1384a45a33a14a29ce898aa3474f` | 530278 |
| `runtime-continuation-predecessor-desk.zip` | `b414dbd175042e20eb17a50fd65e396d495820fd` | `4d45de80bf269f973c83926899658e294e581af5bca6e5a6d4a2b89f227df4e1` | 531174 |

Every subprocess recomputes/asserts its desk implementation hash. All configuration/ledger/history/receipt hashes in executable tests are explicitly synthetic; these are not a replay of production receipt104329 or its private database.

## Validation and limitations

Python3.12.14: `python -m unittest tests.test_runtime_continuation -q`: **11 tests, 18.141s, PASS**. `git diff --check`: PASS. Full Linux suite will be reported in PR comments after completion; no documentation-only follow-up commits solely for test results.

Await independent worker09 review, worker08's separate migrate_v2 semantics repair, exact final combined-source/policy review and coordinator activation. No integration/deployment/production writes. Chains longer than two edges, configuration migrations, reset/reprovision/new experiment and automatic adoption are intentionally unsupported. As with the pre-existing SQLite contract, local administrator control of files is not cryptographic tamper resistance; exact schema/hash/policy checks detect unauthorized modifications within the reviewed local trust contract.

## Independent bounded-read repair

Worker09/root held the initial head because `_first` fetched an oversized first payload before its parser bound. The forward repair validates exact first table SQL/columns/guards, runtime namespace, single-row/id identity, SQL storage types, payload byte length and hash byte length using scalar queries **before** fetching any payload/hash contents. Existing JSON grammar, original receipt and approval checks remain unchanged. An instrumented reader/parser regression covers oversized payload/hash, BLOB payload/hash, invalid id, empty receipt, malformed schema and no-op guards; rejected cases perform no payload fetch, no parser call and no SQL dump mutation. Repaired focused result: **12 tests, 20.302s, PASS**. The incomplete old-head full run was stopped on review request; repaired-head full results will be PR comments only.
