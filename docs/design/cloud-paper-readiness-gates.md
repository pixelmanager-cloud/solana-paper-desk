# Cloud paper readiness gates: offline harness design

This is Agent 10's design and regression scope, based on the checked-in
`docs/readiness.md`, `docs/agent-task-queue.json` and implementation on the
wave-1 base. It does not change readiness, authorize entries or satisfy QA-1.
The cloud harness uses Python 3.12 and synthetic, isolated SQLite databases.
There are no provider calls, credentials, signers, broadcasting or VPS access.
Only the coordinator integrates and performs subsequently authorized observation.

## Dependency boundary

The queue remains BACKLOG/PREPARED_NOT_DISPATCHED in this checkout. This design
can describe future acceptance without dispatching those tickets:

`OWN-1 + OWN-2 -> OWN-3 -> OWN-4 -> SELL-1 + PAPER-1 -> PAPER-2 -> OPS-1 -> QA-1`.

OWN-1 still needs finalized snapshot capture/requery integration; OWN-2 needs
provenance-bound service/private classification; OWN-3 needs current-holder
exposure; OWN-4 needs independent ownership review. SELL-1 needs complete
exact-size route policy. PAPER-1 needs trusted live strategy/cost inputs.
PAPER-2 needs durable automatic entry/exit integration; OPS-1 needs its operating
workflow. QA-1 depends on OPS-1 and forward observation. Fixture passes cannot
advance these dependencies. Existing normalized synthetic bundle evidence is a
fixture interface, not a trusted live evidence builder.

## Stage-to-proof map

Each stage must preserve the preceding stage's identity and provenance. A
verified component does not grant downstream acceptance. A future harness should
report BLOCKED, VERIFIED_COMPONENT or NOT_OBSERVED with reasons and artifact
references, rather than collapsing component successes into a readiness boolean.

| Stage and current code | Required proof for future end-to-end acceptance | Failure injection and required response |
| --- | --- | --- |
| Discovery: `providers.py`, `dashboard.py`, `launch.py` | Original source/signature/slot, receive time, bounded sample window, queue identity and uncovered intervals; complete pinned creation anchor for the candidate. Sampling is currently bounded, not complete discovery coverage. | Conflicting raw identity, duplicate notification, interrupted scan, dropped sample. Preserve originals, reject conflict, make coverage gaps visible; never infer absence from silence. |
| Evidence: `evidence.py`, `replay_history.py`, `history_progress.py`, `ownership_snapshot.py`, `funding.py`, `distribution.py` | Content-addressed raw responses plus exact request manifests, address/range/cursor/slot cutoff, exhausted mint and all historical account histories, initialization/closure continuity, finalized block order, same-bank holder/supply and pool controls. Snapshot ending balances must reconcile at the exact finalized cutoff. Service labels require address binding, provenance, scope and expiry; shared funding alone proves no controller. Current-holder distribution exposure must distinguish lower bounds and unresolved paths. | Delete/tamper pages, rehash a rebound request, truncate coverage, reorder pages, duplicate/conflict transactions, cursor cycle, account recreation, unknown labels, slot mismatch, failed or interrupted request. Replay raw evidence, quarantine conflicts, leave gates blocked. Charge attempts before I/O; original scan plus continuation stays at 18 RPC attempts across accounts and restarts. |
| Decision: `entry_evidence.py`, `decision_runner.py` | Original source payload/hash, original observation time, evaluation time, policy/revision, component results and all blockers. Read-only research/evidence attachment; preserve first decision and add versioned evaluations. A continuation cannot refresh an old scan. | Forged eligible/complete flags, wrong mint, stale source, late completion, malformed batch, changed progress. No entry from assertions; atomic journal writes, no watermark skipping late work. Current consumer always REJECT and creates no fills. |
| Entry: `engine.py`, `strategy.py`, `roundtrip.py`, route-policy modules | OWN-4, SELL-1 and PAPER-1 fulfilled; trusted point-in-time features and deterministic sizing; fresh exact-size buy and exit evidence bound to mint/pool/wallet/amount; complete instruction, recipient, fee, rent, debit and account-effect policy; one durable paper entry. | High score plus missing evidence, Token-2022, stale quote, unknown CPI/extensions, alias recipients, excess fees, incompatible configuration. Reject without a fill. Current engine requires SYNTHETIC_TEST_ONLY for entries; representative-holder and sequential diagnostics do not authorize an entry. |
| Monitor: `monitor.py`, `engine.py`, `paper_view.py` | Continuous active-position coverage, mark timestamp/status, full-position matching sellability, source health, last successful update and outage intervals; watchdog must survive lack of market events. | Stop observations, regress clock, cross midnight, attempt RESUME with stale/unverified position. Expire marks and latch EXIT_ONLY while preserving cash, quantity, cost and loss baseline; clock ticks cannot fabricate market data. |
| Exit: `engine.py`, `replay_sell.py`, `simulate.py` | Fresh supported wallet/position-bound sellability for the actual exit size; independent full route policy, complete effects and costs; partial positions retained; each ladder fill needs a new observation. Historical sell replay is diagnostic, fresh=false and ineligible. | Missing route, stale price, wrong wallet/mint/pool, reused synthetic evidence for real-data exit, unsupported fee/layout. Record blocked exit, retain inventory/cost/PnL, keep entries blocked. Verified recovery alone must not resume entries. |
| Accounting: `ledger.py`, `engine.py`, `paper_view.py` | Transactional event/outcomes/checkpoint, explicit fee/rent/failure costs, residual quantity and cost basis, realized versus model-marked PnL, reproducible implementation/config hashes. Dashboard agrees with persisted ledger and shows stale/unresolved inventory. | Abort after transition but before checkpoint; redeliver same ID; change payload/config/implementation; partial sell then restart. Roll back all writes, reject identity/drift, prevent duplicate fill and reconcile cash/quantity/cost. Current constant-product synthetic model excludes rent and is no profitability proof. |
| Restart/restore: `ledger.py`, `storage.py`, `backup.py`, decision/history journals | Verified checksum/integrity manifests, restored original records, decisions/revisions, cursors, charged budget, positions/costs and modes; exact replay outcomes after redelivery. Backup sets are independent DB snapshots, not atomic cross-DB checkpoints. | Kill after request reservation, fail during checkpoint, restore stale-mark inventory, corrupt/missing snapshot, changed implementation. Preserve spent attempts; reject bad restore; never invent fills. Cross-DB reconciliation must detect incomplete linkage before future entry. |

## Runnable scope and provenance

Run from the isolated repository checkout:

```sh
python3.12 -m unittest tests.test_cloud_readiness_invariants -v
python3.12 -m unittest discover -q
```

`tests/test_cloud_readiness_invariants.py` uses only inline synthetic records,
empty synthetic history responses and `tests.helpers` events/configuration. No
production or public-provider database is opened. Every test owns temporary
files; a socket-connect guard rejects accidental network use in this module.
The suite verifies already-supported boundaries:

- A raw conflict preserves its original observation; real-data assertions cannot
  enter the synthetic ledger.
- Recomputing a summary hash cannot rebind a persisted history request.
- Interruption after reserving attempt 18 survives snapshot restore, cannot reset
  budget and prevents any nineteenth call by another account job.
- Reject-only journal restore retains source and decision bytes without creating
  paper state or fills; rerunning it does not refresh acceptance.
- A SQLite write failure after transition rolls back event, outcomes, metadata
  and checkpoint; restart and redelivery produce exactly one synthetic entry.
- Outage, snapshot restore, RESUME and unverified exit cannot change inventory or
  money; fresh synthetic recovery still leaves EXIT_ONLY latched.
- Partial exit, restart/redelivery and final exit conserve quantity, entry cost,
  net proceeds and realized PnL, including the model's explicit fixed fees.

Other attack cases in the table are future integration obligations or covered by
existing component tests, not claims that this seven-test module implements an
end-to-end live harness. No production code or shared checklist is changed.

## Forward-observation gate (coordinator-owned, not executed here)

Before observation, the coordinator must independently verify upstream queue
acceptance and freeze the reviewed implementation/configuration/policy hashes.
The observation remains paper-only with private loopback access, unchanged
request budgets and no signer/broadcast capability. A future reviewable run
record must include:

1. Explicit start/end timestamps and timezone, planned duration, actual elapsed
   time, healthy coverage duration, outage/gap durations and stop/restart events.
   The repository sets no minimum duration or success-count threshold: the
   coordinator must define and review them before starting, not invent them after
   seeing outcomes. An unagreed duration keeps this gate unresolved.
2. Candidate identities from actual bounded discovery through raw evidence,
   policy decisions, accepted paper entries, position monitoring, verified exits,
   cost accounting and dashboard projection. Record rejected candidates and every
   unresolved dependency as well as successes. No accepted candidate or no
   verified completed cycle leaves that path NOT_OBSERVED; synthetic substitutes
   cannot fill the gap.
3. At least a demonstrated restart and verified backup restore, plus controlled
   provider-failure/outage handling without fabricated fills, duplicate entries,
   refunded request budgets or concealed unsellable positions. Restored journals
   and ledger must reconcile original hashes and linkage before resuming.
4. Redacted evidence/manifest references, exact commands and test results,
   per-investigation request usage, policy reasons, costs, partial/residual
   inventory, stale marks and operator controls. Preserve original artifacts;
   never publish databases or credential-bearing reports.
5. Independent review of the whole chain and an explicit account of unsupported
   token/route profiles, incomplete discovery sampling, observation gaps,
   snapshot non-atomicity and remaining operational limitations. Synthetic or
   model PnL is never reported as realized real-data performance or profitability.

A future QA-1 result remains BLOCKED until all these proofs exist. This worker
neither starts that observation nor enables entries, edits readiness, closes
issues, merges or deploys. Test success establishes regressions on the checked-in
implementation only.

## Worker validation result

Python 3.12.14 with the declared `requirements-live.txt` dependencies installed
in `/workspace/work/agent10-venv`:

- `python3.12 -m unittest tests.test_cloud_readiness_invariants -v`: 7 tests,
  0.174 seconds, OK.
- `/workspace/work/agent10-venv/bin/python -m unittest discover -q`: 520 tests,
  2.378 seconds, OK, no skips. The existing dashboard HTTP fixture required
  sandbox network permission to bind its ephemeral `127.0.0.1` server. No live
  provider verification was performed.
- `git diff --cached --check`: no whitespace errors.

Initial validation before installing dependencies failed because `solders` was
missing (483 tests, 109 errors, 31 skips). With dependencies installed, the
restricted-socket run had one loopback permission error (520 tests); the complete
fixture suite above passed after permitting its local HTTP test. These setup
failures were not repaired by modifying production code or weakening assertions.
