# Exact dispatcher preparation retirement (proposal)

This separate family permits only explicit reviewed retirement of the observed
sixth-reservation/no-capture preparation shape. It does not classify a network
failure: reservation 12 remains charged with an unknown network outcome. The
original preparation pass remains NULL and the original dispatch intent remains
without a result. No retry, refund, scan reuse, entry or execution authority.

The exact pin binds the preparation intent, SEALED admission 6→12 of18, full
retained attempt inventory (history charges7–11), five replay-valid full50-row
nonterminal pages, the limit50 query and RETRYABLE_ERROR/attempts6 row, unchanged
INIT-only ledger/metadata/native row identities against the pre-attempt backup,
and exact immutable dispatcher context/intent/row identities. Apply requires
empty unrelated pending set, no pending pacing/monitoring reservations or pacing
waiters, and canonical research→evidence→ledger→dispatcher locks. SQL/schema and
content bounds precede materialization. Empty checked-in policy authorizes nothing.

The same evidence-DB receipt explicitly binds original dispatcher context to an
exact successor context, allowing only source/tool/entry-tool hashes to change.
Configuration, paths/device/inode identities, target, size, fee and age bounds
remain equal. Journal context, intents, results, uniqueness/capacity counts and
records are never replaced or reset. Only the certified unresolved intent is
exempted from the missing-result test. Any other unresolved intent fails closed.
Future intents use the exact successor context; the old context and certified
intent remain pinned as a prefix. Global gate validation is still mandatory.

## Operator proof and ordering

External review must establish authenticity of the stopped-invocation witness
and pre-attempt backup, and association of this dispatcher with this preparation.
These are not provider-authenticated facts. Retain a bounded manager artifact
with exactly these fields (actual identities/timestamp required):

```json
{"kind":"dispatcher_stopped_review_v1","dispatch_id":"<32hex>","dispatch_intent_hash":"<64hex>","producer_source_hash":"<64hex>","exit_status":2,"service_active":false,"timer_active":false,"observed_at":0}
```

`observed_at` is the actual integer observation time after dispatch, not a
substitute for missing provider time. Both services and all relevant writers must
remain stopped externally. File locks are not evidence of process termination.

Default CLI is read-only planning; `--apply` requires the exact independent pin
in `config/paper-dispatch-preparation-retirement.json`. Arguments:
research/evidence/ledger/config, pass-id, dispatch-id, pacing-db,
producer-source-hash, ledger-backup, journal-path, stopped-witness-hash.

The actual ledger already has its first runtime edge. First apply the separately
reviewed runtime continuation to the final combined source under quiescence;
then plan/review/install the retirement pin and explicitly apply. Plan/apply
require the live CURRENT-source checkpoint and monitoring validators. The
historical producer is bound to the preserved first receipt, not supplied as
the continued live reader identity. A distinct pre-attempt backup is historical
data only: compare all native rows/types/rowids/schema to validated live originals
and require exact bounded first-receipt equality at its original canonical
context. No canonical-path waiver or active identity is granted to the backup.
Neither another first transition nor overwriting the first receipt is supported. This tool does not write runtime receipts or enable
services. Final source/config/tool pins must be regenerated after composition.

## Observed incident supplied by coordinator (not cloud-accessed)

Scan218c824a072944b2a73dac1503ba3038, pass a5c0ceb687984d06987e8e21210e2ec0,
intent9a335bf58cdb19f71dc6ffc1861c73106d6688276b1fc4397785a6049c025bfc,
history02be07593154bb7a047c4c9ee17afe2a4daf4f7ee312a30b813b78da1099b44c.
Dispatch5d97d8cfdb7743d58ff30254ce38c7e8, intent hash
c647d581c3f46af078edbdb0afb6d118c1dc6946a19780ac298cbb5559a33824.
Coordinator reports service exit2, timer inactive, no committed BUY, cash5 and
zero positions/outcomes. Those statements alone do not authorize the receipt.

Synthetic fixtures use actual acquisition4/intake2/preparation reservation APIs,
five raw50-row responses and a sixth pre-transport exception; they do not claim
the live uncaptured reservation never reached the network. Native timing and
worker03's prospective preparation/rejection repair are separate dependencies.

## Composition seams

Worker03 owns prospective bounded preparation and typed replay-validated
NO_ENTRY dispositions. This retrospective unknown-reservation family does not
authorize or classify those future outcomes. Coordinator must combine03's
normal dispatcher result-validator hook and global gate companion so typed
rejections stay retired through all entry paths; partial/missing rejection
publication must fail closed. These are separate review/test dependencies.
