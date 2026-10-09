# Minimal supervised synthetic paper runner

Base: `9e43752`. Scope: standalone `desk.paper_runner`, one offline lifecycle
fixture, dedicated lifecycle tests, and this report. Engine thresholds, evidence
modules, `LIVE_FEATURE_ADAPTER_NOT_READY`, existing CLI/dashboard/projection
contracts and shared readiness/queue documents are untouched.

Run from the repository with Python 3.12:

```sh
python -m desk.paper_runner --config config/paper.json init --db work/new-demo.sqlite
python -m desk.paper_runner --config config/paper.json once --db work/new-demo.sqlite --fixture fixtures/paper_runner/lifecycle.json --now 1791417600
python -m desk.paper_runner --config config/paper.json once --db work/new-demo.sqlite --fixture fixtures/paper_runner/lifecycle.json --now 1791417601
python -m desk.paper_runner --config config/paper.json once --db work/new-demo.sqlite --fixture fixtures/paper_runner/lifecycle.json --now 1791417602
python -m desk.paper_runner --config config/paper.json once --db work/new-demo.sqlite --fixture fixtures/paper_runner/lifecycle.json --now 1791417602
```

`init` exclusively creates a new experiment, establishes the existing Ledger
config/code identity and checkpoint, and labels the new experiment synthetic.
It refuses existing files. `once` accepts only an existing runner-initialized
synthetic experiment; it never adopts or migrates a historical ledger. The
fixture is explicit `SYNTHETIC_TEST_ONLY` with at most 16 market events and a
256 KiB file limit. No adapter-module path, safe flags or live-authorization
mode are exposed. Fixture values are invented positive controls, not captured
chain evidence. The fixed fixture clock is for offline demonstration only.

Each run delivers current operator controls first (`--control PAUSE_ENTRY`,
`EXIT_ONLY`, `LIQUIDATE`, or `RESUME`), observes the open-position mints from the
saved checkpoint, calls existing `monitor.tick`, then considers new candidates.
The total control/market delivery bound is 16 by default, adjustable downward
with `--limit`; the watchdog may additionally persist one existing clock event.
Open observations are validated before delivery. Missing/malformed/outage
observations block the candidate phase, even before mark TTL expiry. If there
is no fresh observation, the watchdog expires stale marks without changing
cash/inventory/PnL or inventing a fill. Fresh observations may recover a mark;
the existing engine still requires explicit RESUME after EXIT_ONLY. Clock
regression performs no adapter work or write. Market events must carry this
pass's fixture time; original price/component times are never refreshed by the
runner. Repeated records retain their exact IDs and payloads.

`Ledger.apply` remains the sole transition/checkpoint transaction. Each delivery
is individually atomic; the whole pass is not an atomic batch. A process can die
after one committed delivery, and retry redelivers exact events against the saved
checkpoint. Duplicate events create no new outcome. Config/code changes require
a new experiment. Missing/corrupt checkpoints are not reconstructed. Supervisors
should invoke one pass at a time; no daemon/service scheduling is introduced.
Unexpected programming/crash errors escape rather than being converted to healthy
status. Expected adapter unavailability is explicit and redacted.

Optional decision/risk provenance fields remain unchanged in the original event
journal payload. They are not interpreted as permission, ownership completeness
or a policy decision. Worker02's experimental paper admission policy and its
reviewed engine integration are separate prerequisites; this PR does not wait
for, duplicate or enable them. Real-data positions cannot be managed through this
synthetic boundary. A trusted real-data entry/refresh adapter is still absent.

## Actual executable demonstration

| Pass | Open positions | Mark time | Simulated cash (SOL) | Realized PnL (SOL) |
| --- | --- | --- | --- | --- |
| Entry at 1791417600 | 1 | 1791417600 | 4.916616667 | 0 |
| Refresh at 1791417601 | 1 | 1791417601 | 4.916616667 | 0 |
| Full exit at 1791417602 | 0 | — | 4.996888045856944320960511715 | -0.00311195414305567903948828490 |
| Restart/duplicate exit pass | 0 | — | unchanged | unchanged |

The refresh records an actual fixture observation with no fill. Full sell
quantity `818.1288282126013875010687251` exactly matches the buy quantity. Net
sell proceeds are `0.08027137885694432096051171510` SOL, with an existing modeled
sell fee of `0.00005` SOL. Ending cash equals initial 5 SOL plus realized PnL;
entry and exit fees remain included by the existing engine. This constant-product
synthetic model does not establish real execution costs, rent/failure accounting,
profitability or live-paper readiness. The final retry emits zero new outcomes.

Output includes the bounded existing `paper_view` projection. Its automatic
entry flag remains false and its continuous-runner field stays `NOT_CONNECTED`:
manual run-once supervision is not continuously monitored service readiness.

## Verification

Fifteen new tests cover complete lifecycle/exact accounting, open observations
before candidates, missing observation suppression, outage/day rollover, original
mark timestamps, pause/exit-only/resume, blocked exits, clocks, input limits,
optional risk/decision provenance retention, no legacy adoption/config migration,
checkpoint recovery refusal, rollback before commit, actual subprocess death
immediately after commit, restart/idempotence and executable CLI passes. Python
socket creation is forbidden inside each test; subprocess cases run only the
local synthetic adapter. No provider/VPS/secrets/signing/broadcast or paid service
is used. No deployment/merge or real-data readiness claim.

```text
PYTHONDONTWRITEBYTECODE=1 /workspace/agent09/.venv/bin/python -m unittest tests.test_cloud_paper_runner tests.test_monitor tests.test_paper_restart_integration -q
Ran 37 tests in 8.151s — OK
```

An earlier invocation also requested nonexistent `tests.test_engine` and
`tests.test_ledger`, yielding two unittest loader errors. The above command uses
the actual existing monitor/restart modules and passes. No implementation failure
was observed in that invocation. Python is 3.12.14. `git diff --check` and the
standalone module help/CLI demonstration pass.

Full suite on the same final production/test source:

```text
PYTHONDONTWRITEBYTECODE=1 /workspace/agent09/.venv/bin/python -m unittest discover -q
Ran 1963 tests in 193.883s — OK (zero failures/errors/skips)
```

No known implementation test failures remain. This is fixture-only validation;
the full suite also includes existing temporary loopback endpoint tests.
