# Captured intake decoder-gap abandonment

Scope: a completed pre-entry finalized-slot intake has an exhausted request range,
retained `HISTORY_DECODE_GAP`, and all one or two charged HTTP200 request/response
pairs. Abandon it permanently without asserting token safety, ownership, semantic
completeness, or entry eligibility. Acquisition remains four canonical records,
not original transport; only intake has original request/response bytes.

The existing `paper_migration_no_entry` table, guards, publication markers,
dispatch result verifier, and global retirement gate are reused. A prospective
`CAPTURED_INTAKE_DECODE_GAP` result takes the already existing dispatcher NO_ENTRY
branch. There is no new table, journal grammar, counter reset, decoder repair,
provider call, screening expansion, or entry authority.

Proof validates the exact admission, source/descriptor, acquisition seed pages,
query inventory, original cursor/page/request manifests, HTTP wire, original
charges, and ledger/config/pacing/monitoring state. The failed page is retained
as unverified; its rows are not semantically reinterpreted to abandon it. Any
candidate preparation or cycle pass forbids this disposition. Uncaptured,
nonexhausted, HTTP-failed, differently queried, additionally charged, partially
published, or conflicting records remain blocked.

Historical review uses the existing coordinator review/apply API with explicit
`captured_decode_gap=True` (`--captured-decode-gap`). It requires an independently
installed exact policy pin, original stopped-owner witness, matching distinct
ledger backup, unchanged original journal prefix/results and first two runtime
extensions, and the existing intake-retirement lineage. Read-only predecessor
planning is allowed only for the exact producer source; application has no source
override. The original unresolved intent/result inventory is not rewritten.
Only the source/tool hashes may change in the reviewed successor context.
Existing legacy-sentinel pins remain required; one additional decoder-gap pin is
allowed. Subsequent runtime extensions still pass the ordinary ledger validator.

Coordinator-supplied public metadata identifies historical 1396: dispatch
`882e62d5bced456882c7d71397c5daf7`, scan
`2e186c3728c045c2a237a51e55eaa546`, retained history
`9c7c4315098a1d71cc115a21ee4be32b62d9858c730454ed74ebd1ab98e24ec3`,
coverage `a20c8ff3d9edd6c4ec7e83b4816934e6b004487aca2263648f17d5fc9342dae1`,
charges five and six at 1791612890/1791612892. This worker has no production DB,
raw provider transaction, or independent source authentication. The coordinator
must generate and independently review the full exact pin against retained data;
these identifiers alone authorize neither installation nor retry.

Synthetic tests exercise real acquisition, capture, history persistence,
retirement, dispatcher verification and runtime-extension lineage. The current
focused/historical and existing regression reruns are pending at draft publication.
An earlier six prospective-test run passed in 41.399s; a historical planning failure
exposed a predecessor-source validation issue and prompted the scoped correction.
No full-suite or production validation claim is made here.
