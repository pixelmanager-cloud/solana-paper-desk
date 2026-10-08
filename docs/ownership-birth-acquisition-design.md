# Explicit birth-inclusive research acquisition: blocked design

Status: design only, not an implemented acquisition command. No production code,
shared readiness file or task queue is changed. No live acceptance is claimed.

This handoff follows the assignment's explicit fallback: if safe acquisition needs
invasive queue/schema/budget changes, return a concrete design and blocker rather
than duplicate worker modules. The inspected baseline is `9b9a277` on main.
PR #18's snapshot/locking work is a separate dependency, not integrated into this
branch. Its reviewed canonical-lock revision is `92e9c61`.

## Existing behavior and precise blockers

Ordinary `screen()` queries mint history from `max(0, now-21600)` to `now`.
This six-hour default must remain unchanged. An older launch can never establish
initialization inventory through this seed, regardless of remaining RPC budget.

The existing production interfaces cannot safely host a separate seed worker:

1. `desk/dashboard.py:Jobs.submit()` admits jobs atomically, enforcing a queue of
   three, ten investigations per rolling day and one pending scan per mint.
   Its rows have no job kind, immutable acquisition descriptor or dispatch key.
   `Jobs.once()` claims any `QUEUED` row and invokes `scanner(mint)` without scan
   identity. A new command cannot guarantee that the ordinary dashboard worker
   will not consume its queued acquisition as a six-hour screen.
2. Constructing `Jobs` unconditionally changes all `RUNNING` rows to
   `INTERRUPTED`. Starting a second acquisition process can therefore interrupt
   another active scan's database state and allow a duplicate submission. There
   is no claim/lease token checked by the terminal update in `Jobs.once()`.
3. `screen()` increments only an in-memory RPC counter. `Jobs.once()` stores a
   generic sanitized failure if the scanner raises; it does not persist attempts.
   Initial provider failure or process death cannot be accounted for durably
   using these APIs. Ordinary screens are not being retroactively changed here.
4. `HistoryProgress.budget()` can reserve durable history attempts, but binds
   them permanently to `source_hash`. `ownership_worker.advance()` computes that
   hash from the immutable completed scan, including its final report. That
   final report is unavailable before mint/cutoff/history acquisition I/O.
   Binding to an acquisition descriptor then passing the final scan hash raises
   `Investigation identity changed`. Changing/removing that guard would weaken
   original-record protection. Adding a parallel acquisition budget and copying
   its counter into continuation would create two competing authorities.

Resolving these requires changes to shared Jobs admission/claim/recovery and the
ownership budget/source-binding contract, outside new-module/CLI-only scope.
The blocker is architectural, not missing credentials or provider access.

## Required shared foundation

One coordinator-owned prerequisite should provide the following behavior:

- Add explicit dispatch kind (`SCREEN` for existing records, a new versioned
  birth-acquisition kind for new records) and a durable immutable descriptor
  associated with the same scan ID. `Jobs.submit()` creates both atomically in
  the research DB. Keep its shared queue/day/mint admission checks; do not build
  another queue or rolling-day counter. All failed/interrupted jobs remain in
  the day count. Active acquisition jobs remain pending for mint deduplication.
- Give claims an owner/generation fence or equivalent exclusive worker lock.
  Only the claimant may checkpoint or finalize that scan. Recovery must prove a
  claim is abandoned; constructing a queue object must not interrupt unrelated
  workers. Filter scanner dispatch by job kind, with unknown kinds blocked.
- Extend the existing durable ownership budget to support an immutable
  admission descriptor and a separately sealed completed-source binding.
  Existing budget rows remain bound to their original completed-scan hash.
  New descriptor-bound rows must never be reset or rebound on retry. A narrow
  one-time finalization method seals the exact immutable completed scan hash;
  continuation validates BOTH descriptor and sealed source rather than replacing
  the original identity. There remains ONE 18-attempt counter per investigation.
  Failed/interrupted setup calls, history requests and continuation all use it.
- Finalization must recover safely across the research/evidence databases.
  Persist a prepared immutable report and its hash before completing the scan;
  idempotently seal that exact source and then publish `COMPLETE` under the claim
  fence. A crash can leave only a recoverable prepared state, never an uncharged
  completed scan. A mismatching prepared report or source is a hard blocker.
  No original completed scan is edited to change its window or request usage.

This foundation needs schema migration tests and updates to dashboard Jobs,
HistoryProgress and ownership-worker binding. It should be reviewed separately
before the narrowly scoped acquisition module and CLI can be implemented.

## Proposed acquisition command after the foundation

Proposed interface (not currently available):

```
python -m desk ownership-acquire --db research.sqlite \
  --evidence-db evidence.sqlite --mint <public-mint>
python -m desk ownership-acquire --db research.sqlite \
  --evidence-db evidence.sqlite --scan-id <existing-acquisition-id>
```

Creation and resumption are mutually exclusive; neither accepts asserted birth
time, a caller-selected cutoff, a completeness flag or a summary launch anchor.
Submitting the same mint while a screen/acquisition is pending must reuse the
explicit acquisition ID or fail without creating a competing investigation.
Retrying by scan ID does not create another daily admission or reset attempts.

The immutable descriptor records scan ID, mint, admission time, source version,
research/evidence database identity and budget ceiling. Canonicalize both database
paths for locks and SQLite access. Use PR #18's stable-path rules: symlink/relative
aliases share locks; hard-linked databases are unsupported; filesystem mutation
while workers run is unsupported. Use a distinct whole-invocation lock plus the
existing per-request lock, never a second worker implementation.

Each invocation performs at most TWO NEW history-page attempts, including failed
page attempts. Setup RPCs are separately identified and still share the same
18-attempt ceiling; initial setup plus two pages costs at most four RPC attempts.
Every attempt commits its reservation before I/O; no automatic uncharged retries.

1. Reserve and persist the raw `getAccountInfo` mint request/response using the
   existing exact confirmed/base64 binding expected by ownership continuation.
   Apply unchanged `mint_policy`. Token-2022, unknown programs, authorities,
   unsupported layouts or null data stop with explicit unsupported reasons;
   they do not spend cutoff/history requests or become legacy-token assertions.
2. For a supported mint, reserve `getSlot([{'commitment':'finalized'}])`. Persist
   the complete request/response and immutable cutoff once before history I/O.
   Resume always reuses it. Missing, malformed or unpersistable cutoff blocks
   history. Never substitute wall-clock estimates for the slot boundary.
3. Use existing `HistoryProgress.create/advance` and request-bound
   `collect_history` unchanged, with `tokenAccounts=none`, ascending finalized
   full transactions and `slot_range={'gte':0,'lt':cutoff+1}`. Metadata start/end
   can be `0` and admission-time-plus-one; the actual provider request uses only
   slot bounds, NOT the six-hour timestamp filter. Genesis-to-cutoff includes
   every possible birth slot without asserting when birth occurred.
4. Persist each page and cursor with the existing replay controls. A partial
   range remains partial. Once at least one valid bound page is available,
   finalize the original immutable seed report exactly once, recording all
   charged attempts, raw mint/cutoff hashes and actual coverage. An exhausted
   range with unknown/missing launch evidence remains unsupported/incomplete.
   The report stays `RESEARCH_ONLY`, `decision=SKIP`, `eligible_for_trading=false`.
5. Continue using the existing ownership worker, under the same sealed budget,
   for remaining pages, historical accounts and eventual bank reconciliation.
   PR #18 may select a later bank and requery genesis through THAT bank's cutoff;
   these are distinct stages and neither cutoff is silently moved. Preserve the
   acquisition descriptor, original raw pages and report. One bank match is not
   ownership/common-control proof or fresh entry permission.

Expose explicit blockers such as setup retry required, acquisition history
partial, missing launch anchor, incomplete initialization decoding, cursor cycle,
unsupported token and request budget exhausted. Existing decoder, launch-anchor,
history controls, entry evidence and pool policy must not be weakened.

## Required fixture acceptance tests for implementation

- A supported synthetic legacy birth older than six hours is excluded by ordinary
  screen but discovered by persisted genesis-to-cutoff acquisition. Production
  decoder, anchor, inventory and replay run unmocked; only provider I/O is a
  fixture. Unsupported/unknown launch bytes and Token-2022 cannot pass.
- Two pages per invocation, correct cursor resume, immutable cutoff after crash,
  failures at mint/cutoff/page I/O, crash after reservation and failure to persist
  raw evidence all retain charged attempts. Exhaustion at 18 makes zero more
  provider calls, including after handoff to ownership continuation.
- Screens and acquisitions together exhaust the same ten-per-rolling-day limit;
  failures/interruption retain the admission. Duplicate mint submissions,
  ordinary-worker dispatch, concurrent processes, symlink aliases and abandoned
  claims never cause competing jobs or stale terminal publication.
- Crash at every finalization boundary recovers the same prepared report/source
  and budget. A changed source, cutoff or descriptor is rejected. Existing scan
  and decision bytes remain unchanged. No new entry permissions or positions.

## Validation performed for this design-only handoff

Offline temporary-database probes reproduced the four interface blockers above:
failed scan result has no RPC count; second Jobs initialization interrupts an
active row and permits a same-mint replacement; any queued row is handled by its
injected scanner without a dispatch identity; descriptor-to-report budget
rebinding is rejected. These are feasibility probes, not acquisition acceptance.

Python 3.12.14 baseline focused suite:
`python -m unittest tests.test_research.JobTests tests.test_history_progress tests.test_ownership_worker -q`
Result: 15 tests in 0.103s, OK.

Baseline full suite: `python -m unittest discover -q`; 513 tests in 2.244s, OK,
with execution permission for the existing temporary loopback HTTP fixture.
No acquisition module, CLI or new production tests were added. Passing baseline
tests establishes neither the proposed implementation nor live acceptance.
