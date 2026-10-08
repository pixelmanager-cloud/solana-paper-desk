# Disconnected original clock stage

Pinned accepted base: `32417a5965b6d92aeefd05ffbb54a0a20dede891` (accepted PR90). Scope is common_bank_journal.py, dedicated clock tests and this report. The accepted common_bank_view clock contract was read before implementation. No transport, evidence/head/receipt publication, provider calls or approval wiring is added.

## Contract and additive profile

Explicit `install_clock_stage()` requires and audits the union profile before installing three immutable WITHOUT ROWID clock tables and exact audited triggers. user_version advances to `0x43424331`. Original SQL, metadata, rows, descriptors, source records and wire bytes are preserved. Installation is explicit and idempotent; missing/corrupt profiles fail closed without migration or downgrade. Strict old readers reject the new version. Deployment requires the compatible reader and explicit installation together.

`begin_clock(capture_id, fence=..., reserved_at=...)` requires audited UNION_DONE, its exact immutable completion fence and unchanged SEALED source, plan, database and predecessors. The ordinal7 PENDING intent generates original JSON-RPC2.0 `getBlockTime([T])`, where T is the actual completed union context, including T>S. It never uses the older request floor S or accepts a caller slot/options/success flag. Request ID derives from the intent. Charge and immutable intent insert share the existing shared18 counter and one BEGIN IMMEDIATE/FULL transaction. Preflight leaves capacity for clock plus at least one mint and one per-frontier history page; this is not a history completion guarantee.

Only successful reservation acknowledgment mints an opaque same-live-PID/thread/session clock handle. Cross-stage, forged, serialized, reopened and expired handles are rejected. An already charged PENDING observed after reopening is terminal uncertainty: no retry, refund, new capture or rebind. Lost reservation commit acknowledgment cannot mint completion authority.

## Original bytes and conservative clock meaning

Completion stores the unchanged generated request and entire bounded original response in the same transaction as ordinal8 attachment. Strict JSON-RPC framing requires the exact ID, exclusive result/error shape, no duplicate keys/nonfinite values and an actual integer `0 <= timestamp < 2^63`, matching accepted common_bank_view. Null/unknown, boolean, float, string, negative, overflow and malformed bodies remain retained FAILED observations. Oversized responses retain fixed redacted category, count and original-request hash without parsing, truncation or positive-prefix acceptance. Transport failure categories are fixed and contain no caller exception text or URLs.

The attachment binds source/descriptor/plan, actual T, exact union fence, intent, budget, request/response byte hashes and completion time. It retains union reservation time as bank_captured_at; a later clock observation does not refresh the bank's capture age. CLOCK_DONE means a syntactically declared timestamp only. It establishes neither authenticated transport nor finalized time, current freshness, complete history, ownership or authoritative full-capture completion. All approval/eligibility/ownership/chain flags remain false and provider_calls remains zero. Matching synthetic bytes can reach this diagnostic DONE; no fabricated completion flag is trusted.

A known observation is frozen before commit. Identical bytes/category AND completion timestamp may be acknowledged using the same live handle after a persistence error, including commit acknowledgment ambiguity. Conflicting completion is rejected. This is persistence-only retry, never permission to repeat transport. Death before completion commit leaves terminal PENDING; death after commit recovers the immutable original DONE/FAILED observation. Reader audit rederives every saved request and binding; even rehashed floor substitution, predecessor substitution, counter rewind and changed raw bytes fail closed.

## Resource and trust limits

Clock caps are 128 intents and 128 attachments, with one count sentinel before whole-set rejection; request1KiB, original response64KiB, event8KiB and redacted failure2KiB. Length checks precede fetch/parse. The shared framing parser and clock extraction parse the bounded response twice. Four stages now contribute at most32MiB serialized response-body audit work, plus requests/events/failures; scans remain sequential. This is not an RSS or whole-workflow32MiB promise.

Existing discovery replay remains one128-reference/128-load/32MiB pass per audit across runs. create_run has its separate plan pass, so those two passes can total256 loads/references and64MiB. Existing source validation remains separately bounded at2MiB per run (up to256MiB serialized source work per audit). Every public invocation pays its own audit. Registry observations remain stage-bounded and clear on session exit. No ceiling or request budget is widened.

Stable owned Linux paths, accepted research-worker/invocation/request lock ordering, rollback SQLite and a trusted application process/configuration remain assumptions. Hostile same-process private-state introspection or privileged complete file/header/lock rewriting and historical rollback are outside this ordinary-SQL boundary. Existing result-only RPC adapters cannot supply original wire provenance. Independently reviewed byte-preserving trusted transport, subsequent evidence publication and semantic accepted-view validation remain dependencies. Protected receipt reader work is separate; this slice changes no receipt API.

## Validation

Dedicated fixtures exercise actual production SEALED discovery admission, all predecessor stages, shared counter, locks, journal storage and reader audits. Coverage includes T>S, exact original whitespace bytes, timestamp boundaries, wrong slot/framing, stage/fence/source/session mismatches, fixed redaction, profile preservation and corruption, fresh SQLite replace/update/delete/rowid aliases, raw-byte and rehashed predecessor attacks, real spawned process deaths before/after reservation and completion commits, two-process contention and before/after-commit acknowledgment ambiguity. Synthetic positives are not live acceptance or paper readiness.

An initial development regression run exposed copied accessor/key naming errors; these were corrected before the final dedicated and combined runs. The first full-suite command referenced a nonexistent worktree-local venv and did not execute tests; the run below uses the existing Python3.12.14 environment explicitly.

Final Python3.12.14 Linux fixture results:

- Dedicated clock: `python -m unittest tests.test_common_bank_clock_stage -q`: **20 tests in18.048s, OK**.
- All journal stages: `python -m unittest tests.test_common_bank_clock_stage tests.test_common_bank_journal tests.test_common_bank_genesis_attachment tests.test_common_bank_slot_stage tests.test_common_bank_union_stage -q`: **96 tests in69.485s, OK**.
- Full pinned-base combination: `python -m unittest discover -q`: **1783 tests in169.468s, OK; zero skips/errors/failures**.
- `git diff --check`: clean. No known outstanding failures.
- Existing private-loopback fixtures used authorized networking; no providers, VPS, secrets, signing, broadcasting, shared queue/readiness edits, merge or deployment.
