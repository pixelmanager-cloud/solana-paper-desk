# Classification evaluation resource bounds

Accepted base: `e130c8c478eb4c349e1dc47bddb53077b1ffbccd`.

The public `classify_account` API previously resolved SERVICE after loading and
deep-copying 129 distinct, individually valid synthetic fixture records. No
caller-side aggregate bound protected the public API. Its per-record limits did
not bound reference traversal or the accumulated result.

The API now requires plain candidate lists and independently admitted plain
immutable policy frozensets, each with at most 128 references. Candidate counts
include duplicates and malformed entries before traversal/deduplication. Policy
size is checked before hash validation; a larger policy produces an unresolved
`CLASSIFICATION_POLICY_REFERENCE_LIMIT`, with no automatic policy selection.
Invalid policy types or hashes within the bounded profile still raise ValueError.
Plain string hashes prevent Python subclasses from overriding validation loops.

Retained evidence has a 4 MiB aggregate ceiling measured as canonical UTF-8 record
bytes plus its 64-byte hash, charged once per distinct retained record. This is a
deliberately stricter profile than the existing 32 MiB diagnostic replay ceiling:
128 records at the existing 64 KiB per-record ceiling cannot all be copied into
one evaluation. Wrapper/container overhead is separately bounded by the 128
reference and existing per-record node/depth limits; the ceiling is not a Python
heap/RSS measurement. The loader remains an offline reader and its own allocation
and I/O contract remain its responsibility. At most 128 candidate loads occur.

Oversized candidates/policies return UNKNOWN with explicit resource reasons and
no loader calls. Aggregate overflow stops before copying the overflowing record,
clears all previously retained copies, returns UNKNOWN with
`CLASSIFICATION_RETAINED_EVIDENCE_LIMIT`, and never classifies a prefix. The input
references and original records are unchanged. At or below the ceilings all
supplied distinct candidates retain the existing provenance, exact binding,
scope, expiry, integrity and conflict checks. No labels are admitted from hashes,
shared funding or caller flags. Private control and ownership approval stay false.

Dedicated public-API tests cover 128/129 reference and policy boundaries,
duplicate/malformed oversized lists without loading, exact aggregate byte boundary
and one byte over, duplicate byte accounting, contradictory final candidates,
hostile list/hash subclasses and real aggregate overflow without source mutation.
The real overflow fixture supplies 4,244,787 canonical-record/hash bytes across
70 individually valid records, with a conflicting final label: 70 loads, 69
copies, zero retained evidence, unresolved. These are synthetic attestations, not
authenticated live classification.

Scope is only `desk/account_classification.py`, dedicated tests and this report.
No entry integration, source authentication, provider calls, private-control
inference, shared readiness/queue changes, merge or deployment. Callers needing
larger evaluations must receive an independently reviewed resource contract;
splitting into separately accepted prefixes or dropping suffixes is unsafe.

Python 3.12.14/Linux fixture-only validation: 32 targeted classification tests in
0.084s, OK; full unittest discovery 1,563 tests in 79.965s, OK. Zero failures,
errors or skips. `git diff --check` passed. No known test failures. The resource
profile does not establish independent evidence admission or live readiness.
