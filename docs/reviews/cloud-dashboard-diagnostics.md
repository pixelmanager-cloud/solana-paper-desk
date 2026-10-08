# On-demand dashboard evidence inspection

Implemented on coordinator-requested base `e23027f` (accepted quantity evidence
PR75 and control-obligation diagnostics PR76). This is operator inspection, not
paper readiness or an entry adapter. Only the coordinator integrates/deploys.

## Endpoint contract and resource boundary

`GET /api/evidence-diagnostics?scan_id=<id>` uses the existing private-loopback
server and Host/Origin checks. The sole argument is an ASCII scan ID of 1–128
characters (`A–Z`, `a–z`, digits, `_`, `.`, `:`, `-`). The entire request target
is limited to 512 characters. Duplicate arguments, additional arguments,
fragments, paths, source payloads, revision overrides and safe flags are rejected.
SQL uses a bound parameter; an ID is never interpreted as a filesystem path.

The handler receives the evidence path from trusted server configuration; the
normal server explicitly passes its configured `EvidenceStore.path`. The adapter
holds the accepted `control_obligations.read_guard` on both research and evidence
files from head lookup through `candidate_snapshot` replay. It opens an existing
`EvidenceStore(read_only=True)` and reads the actual `ownership_heads` row. No
head is synthesized, and no source/admission/progress records are published.
The existing replay load/hash/byte limits remain in force.

A process-wide nonblocking single-flight lock permits one expensive diagnostic
replay at a time. Concurrent requests return HTTP409 `BUSY` without waiting or
replaying. Successful reads return HTTP200 `AVAILABLE`; missing/invalid heads,
unsupported platforms/filesystems/layouts, writer contention, failed reads and
oversized projections return HTTP503 `UNAVAILABLE`. Malformed request arguments
return HTTP400. Responses retain the existing no-store/CSP/nosniff headers.
Errors contain fixed diagnostic codes, never filesystem paths or exception text.

JSON encoding stops at a 128 KiB response ceiling. An oversized result is withheld
in full, including its component/manifest projection; an actually looked-up
revision may remain in the unavailable envelope. There is no partial manifest,
provider call, DB write, retry loop, periodic replay poll, trading action or
eligibility promotion. Busy/unavailable/available envelopes all retain REJECT and
false eligibility. The accepted read guard requires supported Linux LP64 local
rollback SQLite without WAL or journal sidecars; other configurations explicitly
remain unavailable. Operators must not replace/rename/link database files during
reads, as required by that existing guard contract.

## UI and fixture DOM evidence

Each saved scan has an **Inspect saved evidence diagnostics** button. One click
makes one GET. A duplicate click while it is pending makes no second request.
The result survives ordinary scan-list refreshes; visible-scan panels are pruned
when their scan leaves the list. The display names the age **at inspection
display**, since it does not refresh diagnostic evidence automatically. Retry is
manual. Fetch failures/busy/unavailable results clear component claims.

The actual temporary three-account/eight-page ownership fixture produced:

- `Historical evidence inspection · entry remains REJECT`.
- Original observation `120`, observation age at fixture display `880 seconds`,
  distinct diagnostic evaluation time, exact canonical revision/source hash and
  candidate/obligation manifest hashes.
- `Observed endpoint controls · OBSERVED_COMPONENT`, with the actual persisted
  account endpoints and separate scope. Endpoint observations do not establish
  safety throughout history.
- `Reconciled historical accounting · RECONCILED_COMPONENT`, with a separate
  caveat that accounting does not authenticate source/CPI/control semantics.
- `Unresolved historical control and authentication obligations`, including
  `historical_controls · UNRESOLVED` and `source_authentication · UNRESOLVED`.
- `Persisted investigation requests: 13/18 · diagnostic provider calls: 0`.
- `LIVE_FEATURE_ADAPTER_NOT_READY` and other actual rejection/freshness reasons,
  plus exact raw diagnostic fields, evidence hashes and source bindings.

Endpoint controls, accounting and unresolved obligations are separate sections.
Hashes bind local records; the UI retains UNVERIFIED provider authenticity and
states that current evidence health is not continuously certified. Literal DOM
text insertion prevents account/scope/reason markup from executing. A forged
positive decision or eligibility flag is not displayed as approval.

## Verification and limitations

Dedicated tests dispatch real GETs through temporary loopback HTTP servers and
replay real temporary SQLite fixture records. They cover the server-configured
path rather than a default sibling DB, unchanged raw/source bytes and request
accounting, unknown/malformed/duplicate heads, unsupported platforms/layouts,
actual writer contention and exclusion throughout replay, a nonblocking busy
response and release after failure, output ceilings, argument/Host/Origin attacks,
redacted errors, user-triggered DOM behavior, original age/hash preservation and
literal markup handling. Existing renderer tests are reused rather than copied.

Targeted command (Python 3.12.14, Node v24.19.0):

```text
PYTHONDONTWRITEBYTECODE=1 /workspace/agent09/.venv/bin/python -m unittest tests.test_cloud_dashboard_diagnostics tests.test_cloud_decision_renderer tests.test_cloud_paper_renderer -q
Ran 28 tests in 7.242s — OK
```

Three independent adapter reads of the same cached fixture took 33.771, 31.431
and 36.148 milliseconds, returning 18,828 bytes on this Linux x86_64 cloud
container (AMD EPYC 9V74 host model, five CPUs in process affinity). Measurements
exclude fixture construction and browser rendering. They are observations, not
CI time assertions or predictions for maximum-size production stores. Fixture
transport calls and source-file bytes remained unchanged. DOM evidence comes
from the shipped JavaScript executed in the existing Node VM text-DOM harness;
it is not a visual-browser screenshot. Renderer tests require Node. Positive
read-guard cases explicitly require Linux LP64; portable unavailable/security
cases remain independently covered.

Unresolved dependencies remain authenticated complete live ownership/control
history, trusted current strategy inputs/entry adapter and supervised paper
entry/monitor/exit integration. This read-only inspection resolves no historical
obligation and changes no budget, original decision, approval flag or trading
control. No provider, VPS, secret, signer, broadcaster or paid service was used.
No shared readiness/task-queue or simulation files were edited.

Full regression on the same final production/test source:

```text
PYTHONDONTWRITEBYTECODE=1 /workspace/agent09/.venv/bin/python -m unittest discover -q
Ran 1525 tests in 76.415s — OK (zero failures/errors/skips)
```

`node --check desk/static/app.js`,
`node --check tests/cloud_decision_renderer_harness.js`, Python compilation of
the adapter/handler, and `git diff --check` passed. No known failing check.
