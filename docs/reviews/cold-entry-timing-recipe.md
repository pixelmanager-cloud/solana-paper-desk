# History-first cold-entry recipe (fixture proof, not live readiness)

Source: PR156 repaired head `d02d9068f29321ae78771d10f13a4dacc5cf33ef`, tree `ad324bbb83c832626d166b7be19f58f28dd30ebe`.
No desk sources, TTLs, caps, pacing, ledger identities or target grammar are changed.

Run the disconnected probe with Python 3.12:

```sh
python tools/cold_entry_timing_fixture.py > timing.jsonl
```

It uses existing vertical protocol fixtures, original-byte mocked HTTP responses, the real durable SQLite provider pacer (two seconds per provider), reservation/storage/history/parser/engine/ledger code, and a monotonically advancing virtual clock. Each HTTP attempt consumes 0.05 seconds. These are reproducible simulated timings, not measured provider latency. Credentials and opener are mocked; no network or secrets are accessed. Seven cases assert that only history-first/USD-late produces a BUY. Original admission fields, prior evidence hashes and retained USD bytes are checked unchanged; the lifetime counter never resets or exceeds 18.

## Existing trusted Python API recipe

This is coordinator-only execution against explicitly reviewed paths/config/target, with genuine admission and migration references. Retain the actual capture time before issuing history, never replace it with entry time. The existing CLI cannot carry `history_as_of`; use the Python API rather than editing target JSON or runtime code.

```python
import time
from dataclasses import replace
from desk import paper_cycle as cycle
from desk.paper_cycle import canonical_job_path, canonical_ownership_path, _lock
from desk.paper_observe_cli import _worker_lock, _existing_context
from desk.paper_history_source import PaperHistorySource

# research_db, evidence_db, ledger_db, cfg and item come from the existing
# reviewed operator loaders; item is a validated CycleTarget with genuine refs.
research = canonical_job_path(research_db)
evidence = canonical_ownership_path(evidence_db)
captured_as_of = int(time.time())
item = replace(item, history_as_of=captured_as_of)
with _worker_lock(research) as worker:
    if worker is None:
        raise RuntimeError('RESEARCH_WORKER_BUSY')
    with _lock(str(evidence) + '.ownership-invocation.lock') as acquired:
        if not acquired:
            raise RuntimeError('EVIDENCE_INVOCATION_BUSY')
        jobs, store, progress = _existing_context(research, evidence, (item.target,))
        phase = cycle._Budget(progress, time.time, time.monotonic)
        actual_as_of, rows, pairs = cycle._history(progress, item, phase, PaperHistorySource)
        assert actual_as_of == captured_as_of
# Release preparation locks before run_once reacquires them in canonical order.
# Actual elapsed wait, not a pacing/database/time rewrite; pacer also enforces waits.
time.sleep(2.0)
result = cycle.run_once(research_db, evidence_db, ledger_db, cfg,
                       candidates=(item,), dependency_blockers=(),
                       usd_evidence_refs=())
```

The private preparation functions above are existing APIs, not a new supported public command. `_history` binds scan/pool/window `[captured_as_of-300,captured_as_of+1)`, reserves only through `HistoryProgress.advance`, and persists original manifests/responses. A completed identical query is replayed without I/O. Retryable/partial/busy/budget errors must stop preparation; do not reset or silently retry an ambiguous attempt. `run_once` reacquires locks, revalidates admission/ledger, uses the same scan and configured durable pacer, and captures USD late. An empty dependency blocker tuple is appropriate only after coordinator review, not an operator waiver. No new helper is necessary to prove this existing API path.

## Results and request ordering

The successful case spends one history request during preparation, then eight reads inside entry:

1. finalized full pool history window (Helius; preparation)
2. confirmed mint account (Helius)
3. confirmed pool discovery account (Helius)
4. confirmed account union (Helius)
5. initial quote (Jupiter)
6. SOL price (Jupiter)
7. finalized slot (Helius)
8. exact price block time (Helius)
9. exact BUY sizing quote (Jupiter)

Preparation plus real simulated wait: 2.05 seconds. Entry: 8.20 seconds, COMPLETE with BUY, nine total requests. Four prior charged RPCs remain retained in the restart/spent-budget case: same BUY, 13 total of 18. No monitoring allowance is touched. History remains the originally captured window; its age and the actual USD block time are evaluated by unchanged gates.

Cold all-in-one entry blocks at the ten-second deadline. Omitting captured `history_as_of` creates a different window and also blocks. History followed by a separately captured USD triple then entry finishes but rejects `STALE_PRICE`; COMPLETE alone is not proof of entry. Thus use history-first and USD-late inside run_once.

Feasibility depends on a complete bounded history result, genuine supported migration/admission, enough remaining lifetime budget, actual fresh trades and provider latency. Extra pages, slower HTTP, older actual price blocks, pacing contention or known hazards can block. The fixture does not establish live readiness, execution verification or permission to change any gate. Deployment onto a different combined implementation must independently confirm its source/ledger compatibility; this probe is bound to the exact PR156 head above.
