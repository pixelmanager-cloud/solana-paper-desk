# Supervised history-first paper entry

Default is a no-provider dry run. Explicit existing config, research, evidence,
ledger and target paths are mandatory. Uses the existing strict cycle target
loader: one candidate, no positions and no retained USD triple. Live execution
requires PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE provenance and the existing
systemd provider-keys.json credential contract. No initialization/recovery CLI.

```sh
DESK_PROVIDER_PACING_DB=/explicit/existing/private/pacing.sqlite \
python tools/history_first_paper_entry.py \
  --config /explicit/config.json --research-db /explicit/research.sqlite \
  --evidence-db /explicit/evidence.sqlite --ledger-db /explicit/paper.sqlite \
  --targets /explicit/targets.json
```

Only after coordinator review, add `--execute --systemd-credentials` in the
credential-managed runtime. The same environment/pacer must be used by every
investigation and held-monitoring process. This tool never provisions a pacer.

Preflight canonical research → evidence invocation → paper ledger locks;
existing admission/schema, checkpoint/config/runtime, empty positions, running
mode, retained migration, pending-pass refusal, sufficient remaining18 budget
and configured private durable pacer precede credentials/history requests.
Preparation records intent in the existing pass latch before history spending;
crash or charged failure retains pending recovery state. Successful original
history persists its real capture time/window and resolves only this tool's
intent. No unknown or pre-existing pending pass is cleared/retried.

After releasing preparation locks and a normal two-second wait, `run_once`
reacquires its existing locks and repeats runtime/admission/checkpoint checks.
The same target carries the original `history_as_of`; USD is captured late in
run_once. No timestamp, budget, allowance or pacing reset occurs. Fixture tests
use normal host clocks and mocked HTTP; synthetic bytes carrying the public
operator grammar label do not establish real mainnet provenance.

Nine requests is the demonstrated complete single-page fixture, not a guaranteed
live cost. Slow responses, pagination, contention, stale actual blocks/trades,
known hazards or less remaining budget can block. The preparation timeout is its
own existing ten-second bound; entry retains its own ten-second cycle deadline.
No desk .py/.json file changes: external tooling does not alter the desk runtime
fingerprint. Runtime release/ledger compatibility remains coordinator-owned.
