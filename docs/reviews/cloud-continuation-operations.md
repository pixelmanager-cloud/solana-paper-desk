# Independent continuation consumer operational verification

Agent 09, 8 October 2026 (Asia/Seoul). Scope: PR25 periodic offline consumer cost
using PR24's raw three-account/eight-page fixture. No production files or shared
guidance/readiness/task queue were edited by this verification commit.

## Exact candidate and reproduction

Isolated checkout: `/workspace/agent09-continuation`.
Branch: `codex/cloud-continuation-operations`.
Integration base: `29802548f62d53495597a438a7eb72e0b2ea91d8`.
That integration head did not yet contain the two assigned prerequisites.
They were applied, without conflicts, only in this isolated verification branch:

| Assigned prerequisite | Exact source SHA | Locally applied commit |
| --- | --- | --- |
| PR24 raw multi-account fixture/tests | `b5911a6fece35f83af87b89a14537de2d7c11dec` | `15bc37f` |
| PR25 continuation entry component | `28e04362e426e50fc5cceca232bb227f38a3707c` | `a570b34d0676bb38a77116b761e59f8cd7e8e8be` |

The test/report commit adds only
`tests/test_cloud_continuation_operations.py` and this report. Prerequisite
commits are carried for a reproducible combined candidate, not independent
production edits by this worker. Coordinator integration must retain the assigned
changes and rerun if the candidate changes. Canonical-lock fix `92e9c61` is
already in the integration ancestry. No branch was merged or deployed.

Run from this checkout with Python 3.12 and the declared dependency set:

```sh
PYTHONDONTWRITEBYTECODE=1 /workspace/agent09/.venv/bin/python -m unittest tests.test_cloud_continuation_operations -q
PYTHONDONTWRITEBYTECODE=1 /workspace/agent09/.venv/bin/python -m unittest discover -q
```

The dedicated run passed **6 tests in 12.046s**. The combined full suite passed
**723 tests in 26.154s**, with zero failures, errors or skips. The recorded full
run used `unittest.defaultTestLoader.discover('.')` and TextTestRunner to retain
the module's MEASUREMENTS after execution; it executes the same discovered suite
as the second command. Exact full-run command:
`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /workspace/agent09/.venv/bin/python work/run_operations_validation.py`.
That scratch runner/JSON remain in ignored `work/`; reproduction without them:

```python
import json, unittest
result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover('.'))
from tests.test_cloud_continuation_operations import MEASUREMENTS
print(json.dumps(MEASUREMENTS, indent=2))
assert result.wasSuccessful()
```

No known test failures remain. The default command sandbox was sufficient for
the dedicated suite, which opens no sockets. The full suite needed the existing
private-loopback fixture's permitted socket execution; the new suite explicitly
forbids socket creation, provider fetches and credential lookup throughout its
fixture construction and assertions.

## Fixture and method

Only the committed synthetic
`fixtures/cloud_history_integration/multi_account.json` (31,031 bytes) supplies
raw responses. Its provenance is documented in PR24's fixture README; no live
provider data is fetched. The primary path contains four unique raw transactions,
three token accounts, two pages for the mint and each account, finalized bank
slot 20, clock 110 and exact ending balances 40/25/35 with supply 100.

The test constructs 51 separate completed scan identities by fixture SQL. Each
gets its own source-bound budget, canonical bank, history jobs and published head
through four actual `ownership_worker.advance` invocations capped at three
fixture calls each. Each investigation remains at 13 of 18 charged requests.
All decoder, persistence, worker, raw replay, continuity, bank reconciliation,
consumer and decision projection functions execute without fabricated verdicts.
Only fixture transport supplies synthetic responses; instrumentation wrappers
call the original assessment/load/decode functions and count their work.

These are independent investigation identities over the SAME synthetic asset
and raw history. Content-addressed pages deduplicate and the filesystem/page
cache is warm. They are not 50 distinct real assets or a realistic corpus of
maximum-size histories. Fixture-building/setup time is excluded from consume
measurements; the full-suite duration includes it. No scans are submitted,
provider functions called, budgets reset, or original scan bytes changed.

For the 50-case workload, initial default-limit consumption takes three batches
(20/20/10) before three unchanged passes. A counted unchanged pass measures actual
candidate assessments, EvidenceStore loads and production raw decode calls.
A 51-case workload demonstrates that the recent window excludes scan-000 and
assesses precisely scan-001 through scan-050 even with `limit=1` after all rows
are journaled. Tests assert identities/bounds/idempotence, not elapsed deadlines
or exact implementation call counts. Timings use perf_counter and process_time.
Counters are unavailable on uninstrumented rows (their stored zero placeholders
must not be interpreted as zero replay work).

## Observed cost

Python 3.12.14, solders 0.29.0, Linux 6.18.44 x86_64/glibc 2.41. CPU model reported
by lscpu: AMD EPYC 9V74 80-Core Processor, KVM guest. Five CPUs are visible and in
affinity; cgroup cpu.max is `400000 100000` (four CPU equivalents). Cgroup memory
limit is 17,179,869,184 bytes (16 GiB). This shared cloud environment is not the
VPS or the service's 128 MiB memory-limited/network-disabled systemd context.
Whole-suite peak RSS was 74,784 KiB; that includes other tests and is not an
isolated consumer memory measurement or a service memory certification.

| Consume workload | Limit | New journal rows | Elapsed seconds | CPU seconds | Assessed scans (instrumented only) |
| --- | --- | --- | --- | --- | --- |
| 50 investigations, initial batch 1 | 20 | 20 | 0.301489 | 0.301325 | 20 |
| 50 investigations, initial batch 2 | 20 | 20 | 0.312326 | 0.312174 | 20 |
| 50 investigations, initial batch 3 | 20 | 10 | 0.789748 | 0.789412 | 50 |
| 50 unchanged, warm pass 1 | 20 | 0 | 0.851007 | 0.850728 | not instrumented |
| 50 unchanged, warm pass 2 | 20 | 0 | 0.828662 | 0.828216 | not instrumented |
| 50 unchanged, warm pass 3 | 20 | 0 | 0.818412 | 0.818083 | not instrumented |
| 50 unchanged, counted | 20 | 0 | 0.801109 | 0.800766 | 50 |
| 51 investigations, initial batch | 50 | 50 | 0.800485 | 0.798100 | not instrumented |
| 51 investigations, finishing pass | 50 | 1 | 0.849488 | 0.849146 | not instrumented |
| 51 journaled, recent window, counted | 1 | 0 | 0.774889 | 0.774429 | 50 |

Each counted 50-scan pass performs **2,000 EvidenceStore loads**, **1,500 raw decode
calls**, and reloads **4,859,100 logical uncompressed bytes**. Byte counts sum
stored raw_bytes per actual load, including rereads; they are not physical disk
I/O measurements. Each load currently opens a separate SQLite read connection.
Instrumented timings include wrapper overhead and should not be used to compare
small differences against uninstrumented passes.

Warm unchanged median was 0.828662 seconds, approximately 2.76% of one CPU at a
30-second cadence on this fixture/host. Source inspection of
`deploy/desk-decisions.timer` and `.service` shows 30-second scheduling,
TimeoutStartSec=25 and MemoryMax=128M. The small fixture stayed below that timeout;
this is NOT evidence of the deadline or memory budget holding with large pages,
99 accounts, varied assets, cold caches, concurrent writers or provider outages.
No service was started or contacted. Three warm observations are not a tail
latency characterization; no wall-clock assertion is imposed in CI.

## Findings and actionable follow-up

**Operational work is bounded by the recent window, not the insertion limit.**
`desk/decision_runner.py:63-66` assesses before revision deduplication inside a
BEGIN IMMEDIATE journal transaction. An unchanged call with `limit=1` still
assesses 50 scans, with zero rows returned. `consumed=0` is not evidence of an
idle/no-replay run. The 20/20/10 initial workload's last batch also assesses 50
because it never reaches 20 new rows. This is an actionable operating-contract
and scaling concern, not a demonstrated timeout on this fixture.

Before periodic operational acceptance, expose separate assessed/replayed/new-row
counts and duration, document the limit as an insertion bound, and benchmark
larger distinct/cold histories in the actual service resource envelope. If the
cost becomes material, coordinator-owned follow-up should assess safe per-call
raw-load caching or bounded audit scheduling while preserving source/bank/head
binding, revision capture and fail-closed replay. Do not simply deduplicate
before checking evidence and thereby discard the integrity checks. The journal
writer lock spans the replay pass; concurrency/writer latency at production
volume was not measured here.

**The API supplies scope/observation time, but the current UI does not render
those fields next to a VERIFIED_COMPONENT gate.** The real `/api/decisions`
handler was exercised through an in-memory HTTP connection with production
request parsing and response serialization. It exposes historical
history_snapshot=VERIFIED_COMPONENT together with overall REJECT,
eligible_for_trading=false, blocked transfer/exposure/route/strategy gates, exact
historical scope and original observed_at=120. Evaluation at now=1000 retains the
880-second original age and stale reason; snapshot clock 110 is never substituted.
Exact freshness boundaries 119/120/130/131 fail for future or >10-second age while
historical component verification remains distinct.

Source inspection of `desk/static/app.js:10` finds that the decision renderer
shows gate name/status/reasons and evaluated_at, but ignores gate.scope,
evidence_hashes and observed_at. It already displays overall REJECT, historical
continuation text and 'historical decision, not current approval', which mitigate
misinterpretation. Render the component's historical finalized cutoff/limited
accounting scope and original observation time/age prominently in coordinator
UI follow-up. No browser rendering test or production UI change is included.

**Historical journal rows are not refreshed integrity/freshness assertions.**
The executable age case journals a fresh observation at evaluation time 120,
then consumes unchanged evidence at 1000: zero rows are added and GET still
returns evaluated_at=120 with no stale reason from that old evaluation. A second
adversarial case deletes a persisted raw account page after successful journaling:
assessment now returns BLOCKED with CONTINUATION_RAW_EVIDENCE_UNVERIFIED, but the
same head revision is already journaled, so consume returns zero and the earlier
VERIFIED_COMPONENT historical row remains visible. Original rows/hashes remain
unchanged and every decision stays REJECT. This is characterization of the
historical-journal contract, not an entry bypass or a claim of fresh verification.

A follow-up should expose current evidence health separately from immutable
historical decisions (including a last-replay failure) and make the UI's current
vs historical distinction explicit. Do not overwrite original decisions or
reinterpret an old verified component as evidence presently available. The
bounded recent window also means older investigations are not continuously
rechecked. Changing the revision/audit-health design is outside this assignment.

## Limits and dependencies

No production regression or new entry-permission defect was found in the tested
scope; cost and UI/audit-health concerns above require coordinator consideration.
PR24/PR25 exact prerequisite changes and this report/tests are a candidate, not
live acceptance or integration approval. The known PR24 parsed setAuthority
normalization gap is still characterized in these exact dependencies and is
assigned to worker06; these tests do not prove complete historical control
operation coverage. No independent decoder fix or PR26 pool changes were added.

Ownership classification/exposure, trusted provider acquisition, exact route
policy, live strategy inputs, durable automatic paper execution, OPS-1 (#8) and
forward observation remain unresolved. Historical balance/lifetime reconciliation
is not common ownership, current freshness or safety. No VPS, secrets, live
providers, signers, broadcasts, deployment, merge, issue closure, readiness change
or entry enablement occurred. Only the coordinator may integrate and dispatch
follow-up production work.
