# Explicit birth-inclusive acquisition executor

Base: integration/cloud-wave1 at 5ebecd0. Implements the PR29 acquisition design
using reviewed queue, admission/seal and read-only consumer foundations.

## Dispatch contract

`python -m desk ownership-acquire --db RESEARCH --evidence-db EVIDENCE --mint MINT`
admits one BIRTH_ACQUISITION_V1 job. Resume only with `--scan-id ID`, using the
same canonical databases. Admission shares the rolling ten-investigation/day
limit, three pending slots and mint deduplication with ordinary screen jobs.
Interrupted acquisition jobs retain their pending slot and mint reservation.
Ordinary screen defaults and saved results remain unchanged.

Lock order is canonical research worker lock followed by canonical evidence
invocation lock. Symlinks share lock identity; hardlinks are rejected. Abandoned
RUNNING jobs become INTERRUPTED only under the exclusive research worker lock.
Each acquisition claim increments generation; interruption, archive handoff and
publication require the live token/generation and RUNNING state.

Acquisition reserves every setup attempt before provider I/O, including failures.
It has FOUR TOTAL acquisition attempts across all retries and at most two newly
requested history pages per invocation. These four consume the SAME existing
18-attempt investigation counter used later by ownership continuation. There is
no additional request allowance or reset. Failed setup or history requests can
be retried only within the remaining acquisition allowance. Exhausted setup is
published as explicit blocked research; partial history remains explicitly partial.

Mint account evidence is persisted atomically with its immutable content hash.
Existing mint_policy runs before finalized getSlot. The immutable cutoff response
is likewise persisted atomically. History uses genesis slot zero through that
cutoff, without a claimed birth timestamp or summary. Raw replay and supported
creation/inventory validation determine whether initialization was witnessed.
Unknown programs and Token2022 remain rejected; no launch ancestry is fabricated.

## Exact-source handoff and crash recovery

The exact canonical five-column COMPLETE source is prepared before publication.
The source includes all charged calls, including failures, setup hash pointers,
cutoff, immutable job descriptor hash and original acquisition coverage.
Preparation freezes requests. Publication validates those bindings, archives the
exact acquisition checkpoint, seals that same source, then publishes matching
bytes under the current fenced claim. Recovery of PREPARED/SEALED repeats only
these idempotent local operations and makes no provider calls.

The checkpoint archive preserves its original history ID, scan/budget ID, query,
coverage, status and stage attempt count, bound to the exact source hash (which
includes the immutable descriptor hash). Archive rows are append-only. Moving
the original checkpoint out of active continuation jobs avoids counting its
attempts twice: those attempts are already in the sealed report's calls. The
normal ownership worker subsequently seeds the same coverage with ZERO new
continuation attempts. This zero baseline never means zero acquisition spending;
the shared counter and sealed calls retain every prior attempt. Raw evidence
pages and the archived original checkpoint are preserved without rewriting.
Crash before or after archive, after preparation, or after seal cannot recharge
or extend the four-attempt acquisition allowance. COMPLETE retries verify exact
sealed bytes and never resume acquisition. Missing charged setup and malformed
or rebound setup evidence block recovery without recapture.

## Validation and limitations

All provider responses are synthetic fixtures; no provider, VPS, signer or
broadcast was accessed. Tests cover raw legacy birth older than six hours,
ordinary-screen exclusion, unsupported tokens/anchors, failure charging, partial
history, immutable finality, canonical alias contention, wrong database dispatch,
fenced claims, atomic setup rollback, preparation/archive/seal crashes, CLI
parsing/execution, ownership continuation and the existing sealed consumer.

Acquisition is research-only, always ineligible for trading. Completion is not
ownership acceptance, entry freshness or paper readiness. Coordinator-only live
acceptance remains outstanding. Partial or unsupported seeds retain blockers;
full ownership/account history and finalized bank reconciliation remain separate
bounded continuation work. External tampering with SQLite schema is outside the
lock threat model; loaded binding/hash validation still fails closed.
