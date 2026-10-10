# Solana paper desk — Claude Code continuation prompt

You are taking over development from Codex at the user's request. Read this entire handoff and the accompanying support files before changing production. The user wants a working live-data paper trader, followed by versioned entry/exit experiments evaluated on actual forward paper results. The ultimate objective is positive net returns, not finding a perfectly safe token. No profitability or successful production paper cycle has been demonstrated.

## Start here

Repository: https://github.com/pixelmanager-cloud/solana-paper-desk (public).
GitHub default branch was changed during cleanup to `integration/cloud-wave1`, the maintained deployed baseline. Legacy `main` remains stale and is preserved for history. Use `integration/empty-history-ready` for the reviewed but UNDEPLOYED candidate. Do not merge historical PRs indiscriminately.

- Deployed baseline: `0dcc2117b094bfced0e92feebac9f2df47cfd1d4`, branch `integration/cloud-wave1`.
- Deployed runtime source digest: `0cf089b1bb4af6acf47b30d5d8443af1b2af7296bb173be7dbdc0101ba67d692`.
- Candidate: PR #209, https://github.com/pixelmanager-cloud/solana-paper-desk/pull/209, branch `integration/empty-history-ready`, commit `c1e22c289780a0b068190d4938847c0858d0a465`.
- Candidate tree: `6b42821ccd2717865c0617c1111057ad96e1350a`; runtime source digest: `bf98b9c7be3d1480bf3bf75f2dcb0a7df452bafcc2504fe3f7c118d19762ffb2`.
- Codex has stopped development, dispatch, integration and deployment. Final exact-head CI runs both passed 2,941 tests. Both Codex recovery/review automations are paused. See FINAL_STATUS.md for exact outcomes and GitHub cleanup totals.
- No candidate migration or release cutover has been performed. Only discovery is running; entries, held-position monitoring and dashboard/runtime services are stopped.

The latest user direction supersedes older autonomous instructions in queue documents: complete the checks, clean up GitHub and hand off now, without waiting for a full paper cycle. Do not resume Codex workers or scheduled development. Claude may continue implementation under the user's new instructions. The previous instruction was to stop on another code bug; do not conceal any failing check or treat fixtures as live success.

## What works, and what has not been proved

Built: continuous migration discovery; durable investigation queue and request accounting; Solana token/pool/history screening; explicit limited Token-2022 paper profile; experimental unknown-ownership risk flags; live-data observation collection; quote-based simulated BUY/SELL sizing; multiple positions; held-position quotes; stops, profit-taking and time exits; fees/slippage-adjusted accounting; checkpoints/replay; local dashboard/reporting; systemd scheduling; backup and explicit runtime migration/recovery tooling.

Observed production results: several candidates were rejected normally (including MAYHEM_POOL and unavailable/empty history measurements). Other runs uncovered defects at decoder, timestamp, dispatch/recovery and historical runtime compatibility boundaries. Those defects and accumulated preservation machinery repeatedly blocked activation. The latest deployed ledger has no simulated BUY and no open position; initial cash remains 5 simulated SOL. Passing thousands of tests did not validate a live production cycle.

Synthetic lifecycle evidence exists for BUY, held mark, STOP exit and post-exit replay. It is explicitly synthetic. Actual live-data entry → monitoring → exit → accounting → cold restart remains unverified. A first BUY alone is not proof of that cycle, and one complete cycle would not prove profitability.

## Current blocker and candidate fixes

Production scan `2606f102383c4153acb5decf7fc8fcf4`, dispatch `fd03e82b393a4a54ab5279cc1066481d`, observation pass `5c3226ceffeb489e922b88fdb516cc32` completed a verified empty five-minute-window rejection (`MARKET_PRODUCER_BLOCKED`) with 12 of 18 requests charged. It incorrectly left the observation outcome NULL. The journal validates, but preflight recovery refuses to proceed. Preserve that NULL and original evidence; no retry or counter reset.

Candidate mint: `AQpnkvhJmbWcCT8japKCRso4EanytZb1hfjFwwNrpump`; pool: `EJ3UexdXRE77L6UknbLMHq6WA7CPUe87XZhN5CtLa9Q7`. This is a rejected historical candidate, not an open trade or a candidate to retry.

PR #209 composes:
1. #206 `23e0e9d98ff45d74349628750fa5782f1fb5baaf`: proof-backed prospective NO_ENTRY classification for missing history measurements, plus exact append-only reconciliation of the retained incomplete pass.
2. #207 `3404beca34dd8fbc87ff76254a15c85bff072a69`: one pinned runtime successor while preserving the original four immutable extension edges and later performance continuation.
3. #208 `1cd55dc31ac9ac229cfd3756bbb3116e63b73e67`: preserve quote acquisition and collector completion as different timestamps; an otherwise fresh quote must not be rejected merely because they cross an integer-second boundary. Successful original attempt binding and ten-second freshness remain required.
4. Installed-lifecycle fixture from `0b7e9010e3d5c3d2ef3df613ba1aefe51cccde67`, plus exact reviewed migration policies.

Read-only proposals against actual retained stores passed. They have NOT been applied:
- Recovery policy pin: `915d412be08ee751d1774fe7908615f497b07e5b1a7dd3616cebb9132e567cf8`.
- Runtime successor pin: `6f13bf2fbd53e54c8c71d188ae89e817bd1d63c4673331b10b4cf52e35a6752a`.

Independent scoped reviews: #206 review 5478785944; #207 review 5478832430; #208 review 5478889969; #209 final composition review 5478929513. These are scoped review evidence, not a claim that every operational path has been proven.

## Verification record

- Deployed baseline: full VPS Linux suite and exact-head CI 38042093506 previously passed 2,920 tests.
- #208 exact-head CI 38050776472 succeeded.
- Final #209 CI runs: 38051749718 (pull request) and 38051736491 (push). Read FINAL_STATUS.md for final results, not older pending queue entries.
- Synthetic installed-successor lifecycle: coordinator Linux PASS, one test in 147.276 seconds; independent worker03 PASS in 158.375 seconds. Includes preserved original records, explicit migration/recovery, synthetic BUY (8 requests), held mark (5), STOP exit (5), and replay after exit.
- That fixture's post-exit replay is in-process. It does not independently establish a cold process restart or fully compare held-monitoring budgets across restart.
- A separate standalone #208 Linux full-suite session ended without a final summary and had no remaining process. Its local log has no recorded failure, but it is interrupted/unverified, NOT PASS and NOT still running. Final #209 Ubuntu CI is the complete combined Linux check.

To test locally: Python 3.12; `python -m pip install -e . -r requirements-live.txt`, then `python -m unittest discover -q`. CI uses Ubuntu and Python 3.12 with a 40-minute timeout. Tests must use fixtures, no provider credentials. On macOS a symlinked temporary directory previously caused fixture-path false failures; use a real canonical TMPDIR. Do not rerun full suites merely to inflate test counts.

## Architecture and code map

Python package `desk`; SQLite stores retain original provider evidence, investigation history, durable budgets, dispatch intent/result journal and paper ledger/checkpoints. One private VPS runs systemd services. CLI: `python -m desk` / `solana-desk` (`desk.cli:main`). requirements-live pins websockets 15.0.1 and solders 0.29.0. No signing or funded wallet is needed for this paper mode.

Flow: migration discovery → durable admission queue → bounded investigation and token/pool/history checks → source-bound market observations and fresh quotes → entry eligibility/sizing → simulated ledger BUY → held-position monitoring with separate shared allowance → strategy-driven simulated SELL → net accounting/checkpoint → replay/restart validation.

Relevant modules/tools (inspect current branch; older docs may describe previous APIs):
- Discovery/decoding: migration_slot_intake.py, graduation_witness.py, raw recorder and continuous discovery tools; account_keys and pinned schemas for Pump/PumpSwap/Jupiter/token extensions.
- Investigation: job_persistence.py, ownership_acquisition.py, ownership_worker.py, history.py and history progress/account-history/replay helpers, distribution_exposure.py, classification_exposure_adapter.py. Shared funding alone must not be called common ownership.
- Token/pool observations: token2022_paper.py, live_observation.py, pool/vault/control checks and history-signal adapters. Metadata-only Token-2022 profile is supported explicitly; dangerous/unsupported extensions and known hazards still reject. Do not blanket-label Token-2022 safe or unsupported.
- Entry orchestration: tools/history_first_paper_entry.py, tools/paper_entry_dispatcher.py, tools/paper_scheduler.py, durable intent/result journal and lock ownership.
- Source collection: paper_read_sources.py, paper_observation_collector.py, paper_history_preparation.py, paper_market_adapter.py, history_preparation_rejection.py. Original bytes and request attempts are retained; failed attempts consume budget.
- SOL valuation: kraken_usd_observation.py, kraken_pacing_migration.py, provider_pacing.py. Kraken public SOLUSD uses shared two-second pacing.
- Simulation/accounting: quote_execution.py, paper_cycle.py, paper_runner.py, engine.py, model.py, quantity.py, storage.py, paper_checkpoint.py; exit readers/reporting retain original entry and quantity bindings.
- Held monitoring: paper_monitor_service/operator, monitoring_budget.py, monitoring_handoff and successor support. Monitor existing positions first; shared allowance does not create new investigation quota.
- Runtime identity/migration: runtime_compatibility.py, runtime_continuation.py, runtime_extensions.py, runtime_performance_continuation.py, runtime_empty_history_successor.py. Source/config/path/experiment identity pins prevent silently mixing histories.
- Recovery: paper_terminal_reconciliation.py and specific HTTP403/intake/dispatch/preparation retirement receipts; paper_empty_history_reconciliation.py for this candidate. Append-only exact dispositions preserve historical failures instead of rewriting success.
- UI/ops: dashboard.py, static assets, paper_view/dashboard_diagnostics, experiment reports, backups and restart validators. Dashboard is local read-only, not publicly exposed.

A major engineering problem is accumulated runtime compatibility and retained-history replay cost. PR #203 caching plus #204 successor and #205 bounded retained-history streaming were incorporated into deployed baseline 0dcc. Before changing this machinery, reproduce the exact production preflight path using an isolated copy or synthetic fixture. Avoid expanding screening architecture: prioritize making this existing cycle operable and observable.

## Provider status

Helius supplies Solana discovery/history/RPC data. Jupiter swap quotes previously worked. Jupiter PriceV3 returned HTTP403 from the VPS; support request submitted, resolution unverified. Do NOT retry PriceV3 or retired candidate703. Kraken public SOLUSD valuation replaces the blocked price source, with no paid subscription. Preserve shared two-second Kraken pacing.

PR #182 is a separate unfinished direct PumpSwap quote-estimate fallback. It is retained as deferred work, not activation-ready and not equivalent to executable aggregator quotes. Do not quietly substitute its estimates into the current experiment.

## VPS and operational state

SSH: `root@158.247.196.20` using the user's existing local credentials. Hardware after upgrade: 6 vCPU / 16 GB RAM. Do not publish credentials or databases. Provider secret path is `/etc/solana-desk/provider-keys.json`, loaded using systemd credentials; no secret values are included here. Do not send those secrets to cloud agents or GitHub.

- Python: `/opt/solana-desk/.venv/bin/python`.
- Deployed release directory: `/opt/solana-desk-releases/0dcc2117b094bfced0e92feebac9f2df47cfd1d4`.
- Discovery service active, directory `/opt/solana-desk-tools/discovery-dd09bb1`.
- Entry and held timers inactive; entry service may show retained failed status from the old run. That is not a newly attempted run.
- Dashboard and runtime services intentionally stopped. Dashboard binding must remain `127.0.0.1:8765` when restored.
- Configured cadence: entry 10 seconds and held 15 seconds after completion (not overlapping every fixed interval), but currently stopped.
- Service wall timeouts: entry 600 seconds, held 120 seconds. Provider deadlines unchanged. MemoryMax observed 402653184 bytes. Larger VPS alone does not eliminate these process/code bottlenecks.
- Boot enables may still exist. Do not assume reboot preserves the current intentionally stopped posture.

Data under `/var/lib/solana-desk`: research.sqlite, evidence.sqlite, active-paper.sqlite, token2022-paper.sqlite, token2022-boost-00b7c302.sqlite, paper-decisions.sqlite, launches.sqlite, raw.sqlite, provider-pacing.sqlite, discovery/continuous.sqlite, entry-dispatch/dispatch.sqlite, paper-kraken-77de75a2.sqlite.

Last direct paper ledger read: events=1, outcomes=0, tables=11; no BUY/open position. Twelve databases / 92 tables were preserved at the preceding hardware/release validation. Do not confuse one ledger's 11 tables with the full store inventory.

Quiesced historical backup: `/var/backups/solana-desk/pre-empty-window-recovery`; manifest SHA256 `3e9f516ac5a7a5a245291dbc6b2061541e594288b6c0570e2171673d0c37a3ff`. Discovery has continued since it; a new deployment needs a fresh quiesced backup. Never reset data, counters, blocked state, usage, high-water or reservations to get a pass.

## Active paper experiment and strategy

Config `/etc/solana-paper/paper-kraken-reviewed.json`; SHA256 `3dc3108e1c23ebbad97b88338e37f4012d3dd6b259f5a911624d799da1c11dff`; version `2026-10-10.paper-quote-kraken.1`; mode paper.

Initial equity 5 SOL; fee reserve 0.3 SOL; minimum order 0.01 SOL; max position fraction 0.02; max exposure fraction 0.08; max positions 4. Liquidity fraction 0.015; risk/trade fraction 0.005; stress-loss fraction 0.30. Daily pause fraction 0.06; daily liquidation fraction 0.10. Max round-trip cost fraction 0.08. Fixed simulated fee 0.00005 SOL; adverse slippage 50 bps.

TTL seconds: price10, holder120, flow30, momentum15. Engine age range300–21600 seconds; dispatch context separately narrows admission to300–7200. Market cap USD50000–2000000; minimum liquidity USD8000. Stop fraction0.18; time-stop2700 seconds; maximum hold21600; trailing fraction0.30; ordinary cooldown1800; stop cooldown7200. Inspect engine.py for exact profit ladder and condition semantics before tuning.

Version flags: paper_signal_policy_version3, paper_quote_execution_version1, paper_token_profile_version2, paper_usd_valuation_version1. Dispatcher requested quote amount100000000 lamports (0.1 SOL); actual fills remain subject to planner/engine sizing and exact quote binding. Pool fee assumption25 bps. Public taker identifier is a quote input, not a signing wallet.

## User approvals and boundaries

Approved targets: discovery100MB and5000 observations per rolling hour,5GiB storage; investigations1000 admissions/day, queue25, lifetime18 requests/investigation; verified-open-position monitoring3600 requests/rolling hour shared across positions. These are ceilings, not consumption goals. Inspect installed policy/receipts before assuming any target is active. Upgrades must explicitly preserve all existing charged usage, reservations, high-water, blocked state and original records. Honor provider pacing; failures are charged; no automatic increases/resets or purchases.

Unknown ownership history is allowed only in the explicit experimental paper profile with visible risk flags. Supplied corrupt evidence and known hazards still reject. Fresh quote-based simulated fills with fees/slippage are approved and must display EXECUTION_UNVERIFIED. No fabricated fills, hindsight profit, or automatic claim of executable trade performance.

Entry-threshold experimentation is approved with versioned configurations and later held-out/forward evaluation. Optimize using real paper outcomes and realistic costs, not merely the count of entries or fitting one winning sample. Do not use this as authorization to weaken evidence integrity or bypass budgets.

No signing, broadcasting, real funds, paid subscriptions, public service exposure or cloud provider secrets. Original source evidence and private loopback access must remain intact. PR #97 and #110 reviews were platform-blocked; never retry or reroute those reviews. Administrative archival is not review approval.

## Reviewed deployment preparation — NOT executed

The support folder includes coordinator-only helpers, exact pins/proposals, and durable queue/readiness snapshots. They are provided for inspection, not as an instruction to run blindly:
- backup-dispatch-recovery.py
- activate-empty-successor-reviewed.py
- verify-empty-successor-originals.py
- verify-empty-successor-active-context.py
- start-empty-successor-reviewed.py
- deploy-empty-successor-reviewed.py

The intended reviewed flow: exact source/archive verification → stop writers → fresh 12-store backup → append recovery receipt and exact runtime successor → original row/schema/type/rowid preservation checks → active-context/journal/budget validation → post-migration backup → service cutover. Start helper keeps entry timer OFF, restores only specified other services, and stops known units on failure. Re-read every helper and pinned identity before future execution. Some helpers are tied to this one historical release; they are not generic migration tools.

Archive `empty-successor-c1e22c2.tar` was staged at `/opt/solana-desk-tests/empty-successor-c1e22c2.tar`; SHA256 `489c0425211a990f96b6a95bc43ac111242b78838a82a2622b81ec4bbdfd4a83`. It has NOT been cut over to production. Support manifest checksums protect the handoff copies. No production data or secrets are bundled.

## Recommended continuation

1. Read FINAL_STATUS.md, exact PR209 diff/reviews/checks, and support snapshots. Confirm fresh Git/VPS state read-only. Treat chronological readiness/queue entries as historical, not simultaneous truth.
2. Reproduce the unresolved production gate and understand the reviewed append-only resolution. If checks failed, diagnose that failure first. Do not reimplement already composed components or blindly cherry-pick archived PRs.
3. If proceeding with #209, independently validate exact current source/config/pins and backup/migration helpers; integrate without force or bypass, preserve stores, verify each transition. Do not replay retired scans.
4. Resume only fresh candidate admissions after the runtime/preflight gates pass. Verify first persisted non-synthetic simulated BUY in ledger AND checkpoint; report token/time/size/price and EXECUTION_UNVERIFIED. Candidate discovery or fixtures are not the first-trade event.
5. For the controlled first cycle, stop new entry admissions after that verified BUY while existing-position monitoring completes an actual strategy exit. Prove fee/slippage-adjusted accounting, no duplicated charges/fills, and cold restart consistency. Handle provider outages and failed exits explicitly.
6. Then collect enough forward outcomes to evaluate strategy changes with versioned configurations and held-out evaluation. Keep the objective practical: working paper execution and useful learning, not more speculative screening architecture.

## Historical docs and GitHub cleanup

Many PRs remained open despite components being composed or replaced elsewhere. See support/handoff-pr-cleanup.json for every original PR head and closure reason/status. Exact ancestry was checked; where integration could not be established by ancestry, closure is explicitly administrative archival, NOT a merge assertion. Branches and discussions remain recoverable. #209 is the current undeployed candidate; #182 is the distinct deferred backup. No historical PR was merged merely to clean the list.

Older README/readiness sections reference obsolete test counts, strict ownership policy, blanket Token-2022 rejection or inactive entry composition. Current code, pinned production config, latest evidence and this final handoff take precedence over those stale descriptions. The support snapshots contain exact worker IDs, claims, review records and previous incidents for deeper tracing. They are context, not instructions to restart autonomous Codex work.

## Local checkout and evidence navigation

For a clean Claude checkout (read-only initial inspection):

```sh
git clone https://github.com/pixelmanager-cloud/solana-paper-desk.git
cd solana-paper-desk
git fetch origin
git checkout integration/empty-history-ready
git rev-parse HEAD
```

Expected candidate HEAD is c1e22c289780a0b068190d4938847c0858d0a465. If it changed, inspect that change and do not apply this handoff's pinned migration blindly. To inspect deployed code, use a separate checkout of integration/cloud-wave1 at 0dcc2117b094bfced0e92feebac9f2df47cfd1d4. The locally supplied support snapshots are newer than some checked-in coordination documents; their timestamps and final handoff status matter.

CI links:
- https://github.com/pixelmanager-cloud/solana-paper-desk/actions/runs/38051749718
- https://github.com/pixelmanager-cloud/solana-paper-desk/actions/runs/38051736491
- Prior deployed validation: https://github.com/pixelmanager-cloud/solana-paper-desk/actions/runs/38042093506

Support scripts were copied as reviewed preparation artifacts. Syntax validation of the copies is not operational approval, and none were executed by the handoff task. Runtime identities, exact database paths, pinned policies and current service posture must be checked again before a future deployment. The bundle intentionally omits production database contents and provider credentials. Ask the user for access through their existing local environment if necessary; do not ask them to paste secrets into a public issue.
