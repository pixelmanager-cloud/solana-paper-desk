# Disconnected pre-SEALED finalized-slot intent journal

Base: `893bf72ed79dee7be224bccf4c7ba3913f36d6b6` (accepted PR95).

## Scope and admission

`desk/presealed_slot_journal.py` adds a separate explicit application ID
`0x50534A31`, user version 1, and three append-only WITHOUT ROWID tables.
It does not modify acquisition dispatch, call a transport, collect history,
publish evidence/head/receipt, migrate a source, or grant any approval.

Trusted coordinator code must first create an ordinary BIRTH_ACQUISITION_V1
job and its existing zero-used ownership admission. The journal requires the
existing live first-generation job claim, exact immutable descriptor, existing
18-call ceiling, and a dedicated evidence database containing only the four
existing empty/admission tables and exactly that one admission/counter.
Any legacy setup (even empty), page, history, spent counter, reopened claim,
unknown profile or publication rejects without installing a journal.
This deliberately restrictive profile is not a retrofit for existing scans.

Locks follow the existing worker -> ownership invocation -> ownership request
order. The caller already holds `JobPersistence.worker()`; the journal validates
that claim and never reacquires the worker lock. Canonical paths, device/inode,
owned single-link files, distinct databases, no-follow private lock files and
nonblocking contention reuse existing guards. Single-host stable-file assumptions
remain; same-UID/privileged whole-database rewrite and rollback are outside the
local integrity contract. Python types/hashes/configuration do not authenticate
an external transport or grant candidate trust.

## Exact future transport agreement

`session.begin_slot()` commits one generated original request BLOB, immutable
PENDING intent and the existing counter's `0 -> 1` charge in ONE FULL-synchronous
rollback-journal transaction, before returning an opaque handle. Intent insertion
precedes the charge inside that transaction; neither half is visible alone.
No handle exists when reservation commit acknowledgment is lost.

`session.request_bytes(handle)` returns precisely the persisted UTF-8 bytes:
`canonical({'jsonrpc':'2.0','id':'presealed-slot:'+digest(binding),
'method':'getSlot','params':[{'commitment':'finalized'}]}).encode()`.
`binding` is the immutable version/pinned databases/planned source/exact job
and admission descriptor/live claim fence. `canonical` uses the repository's
sorted-key, compact JSON serialization. The ID is a STRING, fixed for that
intent, not integer `1`. Future worker01 transport must transmit these bytes
unchanged and return its original bounded response bytes; it must not rebuild
params, replace the ID, unwrap `result`, or assert authentication through a
caller summary. This module assumes no unfinished transport API.

`attach_response(handle, exact_request_bytes, original_response_bytes)` requires
same session/PID/thread, live original fence and exact handle object identity.
It preserves the entire original response, including whitespace and invalid
syntax, within bounds. A DONE response has exactly `jsonrpc`, matching string
`id`, and `result`; strict JSON rejects duplicate keys/nonfinite numbers. Only
actual integer `0 <= C < 2**63-1` produces `cutoff=C, exclusive_cutoff=C+1`.
Boolean/float/null/unknown/error/wrong-ID/overflow/malformed frames are retained
FAILED, not normalized. The signed-63-bit profile is intentionally stricter than
Solana u64 so C+1 fits SQLite and existing bounded history consumers.

A failure API accepts only TRANSPORT_TIMEOUT/TRANSPORT_ERROR/CANCELLED, preserving
bounded canonical redacted category provenance bound by the same predecessor,
request/source/database/job/fence. It accepts no free-text exception or credentials.
This is local failure recording, not authenticated proof of network activity.

## Uncertainty, readers and future dependencies

A reopened PENDING is terminal. Inspection can show the old intent/attachment
under a subsequently valid job claim but cannot recreate its completion handle.
Unknown, expired, forked, wrong-source, stale-fence or conflicting completions
reject. Completion also verifies the exact expected attachment body/hash/original bytes
actually exists after INSERT before acknowledgment; a suppressed insert cannot
report DONE. Handle types are rejected before any user-defined hash/equality
dispatch, retaining exact plain-object identity.

A known result is frozen in the live handle before any persistence
attempt: only identical byte/result persistence acknowledgment may be retried.
There is no request retry, refund, reset, rebind, caller ID selection or new-run
API. The job remains pending and existing queue mint deduplication prevents
ordinary new-ID escape; a different evidence path cannot match its descriptor.

The only existing-module hook is a four-line `HistoryProgress.__init__` check
that rejects this profile before generic writes or adoption. Older readers are
also fenced by immutable admission/page/history triggers and a budget trigger
allowing only the single initial charge backed by its intent. Once charged,
generic reserve cannot make another call. The production path for databases
without this profile is unchanged. Existing common-bank readers reject this
separate application/schema profile; no old schema/record is rewritten.

Future history stages, provider authentication, bounded raw transport, source
sealing and common-bank/receipt publication require separate review/wiring.
`source_authenticated`, `ownership_complete`, and `eligible_for_trading` are
always false, even for a syntactically valid synthetic DONE result.

## Resource accounting

One job, one intent, one attachment; no run enumeration or page decompression.
Request 1 KiB, response 64 KiB, redacted failure 2 KiB, each metadata/body 8 KiB.
Each fresh admission and installed audit first preflights the COMPLETE typed
SQLite catalog using scalar count and serialized name/type/table/SQL byte
statistics in the same guarded transaction. At most 64 objects, 128-byte names,
16-byte types, 4 KiB SQL per object and 64 KiB total catalog bytes are allowed.
The entire exact typed list (including known automatic indexes and all original
base table SQL) must match; no name-keyed dictionary, prefix filter, unknown
trigger/view/index or altered base table is allowed. SQL/name bodies are fetched
only after reservation; no invalid prefix or contradictory suffix is salvaged.
These limits bound application materialization, not SQLite engine internal
schema parsing/allocation when opening a corrupt file.

Every audit then queries all three tables' cardinalities and serialized lengths and
reserves the whole 83 KiB aggregate limit BEFORE any journal body/BLOB fetch.
The conservative aggregate allows metadata + intent/event bodies + originals;
actual three metadata bodies are individually bounded, and the aggregate can
reject their combined maximum. Catalog plus journal serialized materialization is at most 147 KiB per audit
pass (64 + 83), with up to two such passes per attachment operation.
Existing admission descriptor is separately
preflighted at 8 KiB with no prepared source. An attachment operation performs
one pre-write and one post-write audit (up to two complete bounded passes),
plus its incoming response and frozen known-result reference; it does not claim
these are one load or free memory. Prior validated wire syntax parsing is repeated
for audit integrity. No selected late-load pass or unbounded reference list exists.

## Validation

Python 3.12.14, Linux, synthetic fixtures only. Dedicated tests include actual
spawned process deaths before reserve, between insert/charge, after charge before
commit, after reserve, and before completion commit; separate actual-process
worker-lock contention; reservation lost acknowledgment; completion commit-before/
after ambiguity; identical-only persistence; restart terminal behavior; exact
original bytes; strict integer/overflow; framing attacks; source/fence/request
substitution; caps before body fetch; unknown schema; hardlink/replace pins;
default-recursive-triggers SQL REPLACE/rowid/deletion attacks; complete typed catalog/capacity attacks and exact persisted-insert postconditions; exhausted shared18;
legacy setup and admission refusal. No provider was called.

Exact test results are recorded in the PR description after the final run.

Forward repair: the initial catalog reader checked only table names and
presealed-prefixed SQL; independent worker05 confirmed unknown schema acceptance,
a hidden IGNORE trigger falsely acknowledging DONE, oversized SQL fetched before
refusal, and an equal/hash-alias handle discrepancy. This forward reader repair
preserves every original schema/row/version while rejecting these cases. The
separate test portability commit resolves canonical paths in injection hooks;
all real process-death/commit-failure checks remain active without added skips.
Future transport MUST record a transport failure even when returned prefix bytes
would parse as a valid slot reply; syntax-DONE alone is never transport success
or source authentication. No transport wiring is included here.
