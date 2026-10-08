# Common raw bank acquisition: proposed coordinated contract

Base: `11bd94462aa24053d95b77b5ba9336b374ea8444` on
`integration/cloud-wave1`. Design and disposable fixture proof only. No production
code, acquisition permission, schema migration, receipt issuance, policy relaxation,
Token-2022 support or live acceptance is implemented by this PR.

## What existing code can and cannot do

`HistoryProgress.capture_bank` captures `[mint] + sorted(holder_frontier)` with
exact finalized/base64 options, reserves before each bank/clock call, and fixes the
bank before slot-bounded history replay. Restart reuses that bank. It neither
accepts extra pool-state accounts nor replaces a completed canonical bank.
`ownership_worker` rejects frontier changes after bank capture.

`PoolCaptureBridge` independently charges genesis, finalized getSlot, exact
six-account getMultipleAccounts with minContextSlot=S, and getBlockTime(T).
Its six keys are `[pool, mint, wrapped_SOL_mint, LP_mint, base_vault, quote_vault]`,
independently derived by `canonical_accounts`. `_records`, receipt publication,
`pool_vault_admission._bound_capture`, and the protected ledger's PROFILE all
bind that exact request. No existing union/subset receipt profile is accepted.

PR81's `project_pool_vault` already joins independently captured banks **if** the
actual slots and block times coincide and original mint/base-vault raw bytes,
owner, executable status and lamports agree. The existing transformed synthetic
history/receipt fixture proves that path. It deliberately arranges two observations
at T20; it is not evidence an operator can ask a later RPC for T20.
minContextSlot=S is only a floor: the next response may be T>S. Even equal slots
need raw/time conflict checks. Repeating reads until they happen to coincide is
not an acquisition contract and spends charged attempts. A saved ownership bank
cannot be refreshed to chase a pool receipt. An unrelated later investigation
cannot be used to relabel the old source or reset its spending.

The dedicated tests prove both exact readers reject a union envelope today.
There is no reliable existing single-response common-bank path. Implementing
one requires coordinated reader/receipt/schema changes, independently reviewed
before any coordinator use. Merely changing getMultipleAccounts keys is unsafe.

## Minimal proposed request and indexed views

Use an existing **SEALED admission** with the same descriptor, completed-source
hash, scan/mint and one shared18 counter; no conversion of legacy source-bound
budgets, admission creation, competing Jobs or changes to10-investigations/day.
Require the canonical ownership bank to be absent. Preserve original completed
source bytes. Source PREPARED blocks reservation; recovery must finish the exact
original seal/publication before this acquisition begins.

Replay the request-bound discovery history to obtain F, the full historical
legacy target-mint holding-account frontier, including closed accounts. Neither
a supplied holder summary nor the pool accounts may manufacture F. Require
verified initialization inventory and base_vault in F for this join; missing base
membership remains a blocker, not a fabricated lifetime. The quote vault holds
wrapped SOL and is not inserted into the target-mint holder frontier. Unknown
programs, Token-2022, malformed/ambiguous frontier or duplicate F reject.

Let P be the exact six canonical pool keys above. Freeze the ordered union:

```
U = P + sorted(F - set(P))
ownership_indices = [index_U(mint)] + [index_U(a) for a in sorted(F)]
pool_indices = [0, 1, 2, 3, 4, 5]
```

Require unique valid32-byte addresses, mint not in F, exactly the independently
derived P prefix, and `len(U) <= 100`. With only the base vault shared with F,
95 holders fit and96 do not. Never split into batches or treat separate contexts
as atomic. Freeze F's discovery revision/query refs, U, role mapping, approved
source identity, descriptor/source hashes, evidence DB identity and capture
identity **before I/O**. F is provisional coverage until postcapture T replay.

Obtain approved-source getGenesisHash, finalized getSlot(S), ONE
`getMultipleAccounts(U, {encoding:base64, commitment:finalized, minContextSlot:S})`,
then getBlockTime(T), where T is the returned exact integer context slot >=S.
Retain original S; do not rewrite it to T. Freshness uses original bank capture
wall time, not recovery/clock publication time. Finalized is provider-declared,
not authenticated chain/finality proof. Request/response size limits remain
bounded: current transport2MiB, storage16MiB and diagnostic128 references/32MiB;
proposed full union point evidence retains the existing64KiB per-record point
bound. Oversized original envelopes reject rather than being stripped/sliced.

Persist the original full envelope `{kind:rpc_response_v1,method,params,result}`
and exact request manifest with its response hash, network/genesis/source and
bound capture. Keep every returned account, null, context field and annotation.
Use the existing envelope definition, not a claim that JSON-RPC wire framing is
retained by the current result-returning adapter. Original union envelope hash is
the bank identity. A new versioned manifest binds the raw hash plus U/F/indices,
scan, source/descriptor and discovery references. It is locally computed linkage,
not a provider response or source-authentication certificate.

Reader views are index/address lookups into that **same parent record**, not
new RPC responses. Both readers independently reconstruct U and indices from
bound raw discovery/canonical identities and reject reordering, extra/unbound
keys, count mismatch, index aliases, missing indices or changed parent hash.
No six-value or mint-plus-holders object is ever saved under rpc_response_v1,
passed to v1 admission, or claimed to have been an independent getMultipleAccounts.
Explicit null closed-holder values remain covered and are verified by history;
a required live pool account cannot be null. Mint and shared vault are literally
the same parent entries in both views, not just matching normalized quantities.

## Required production coordination, not implemented here

1. Add a versioned common-bank capture/binding journal and reader contract in
   the same evidence DB. Do not change or overwrite existing ownership_banks
   rows. New bank mode must explicitly dispatch indexed replay; old records
   retain strict v1 behavior. Audit partial schema, triggers, rowid aliases,
   canonical path/hardlink identity and monotonic journal/fence on reopening.
2. Add a narrowly scoped original-parent indexed bank validator/reconciliation
   path to ownership snapshot, continuation consumer and worker. Mint index is1
   for this order, not0. Pool/config/mints/quote entries must not be interpreted
   as target-mint holders. Existing complete holder/supply/control/history gates
   remain required. Bind history slots `{gte:0,lt:T+1}` and clock(T).
3. Introduce a separately reviewed union-point receipt/admission profile and
   protected ledger schema/reader support. Preserve exact six-account v1 profile
   and all original receipts byte-for-byte. Existing fixed PROFILE source roster
   and ledger descriptors cannot silently be changed in place; an explicit
   versioned migration/registry decision is required. Unknown profiles reject.
   Union-point admission consumes the original full envelope and validates the
   canonical six-role indices; apply the unchanged legacy raw pool/mint/vault
   policy and actual T/time/state conflict checks to those entries.
4. Extend PR81's join to consume the versioned ownership view and original union
   receipt. Do not issue generic holder exclusions, classification certificates,
   private-control claims or entry approval from it.

Existing v1 readers cannot consume the proposed manifest and must continue to
reject it until these components land together. Changing only a receipt producer
would either fail v1 admission or forge the original request, not solve the gap.

## Concrete proposed schema and API boundary

These are implementation requirements, not APIs shipped by this PR:

- `common_bank_runs(capture_id PRIMARY KEY, budget_id UNIQUE, descriptor_json,
  plan_json, plan_hash)` is one immutable run per existing budget. descriptor_json
  binds exact source/descriptor and protected ledger/source/evidence identities;
  plan_json binds canonical pool, sorted F, U, ownership/pool indices and original
  discovery revision/query hashes. New IDs cannot replace the UNIQUE budget.
- `common_bank_events(capture_id, ordinal, event_json, previous_hash, event_hash)`
  is append-only with PRIMARY KEY(capture_id,ordinal). Events describe stage,
  PENDING/DONE/FAILED/PUBLISHED, used_after, exact request, raw response/failure hash
  and original capture wall time. A journal hash chain starts at the immutable
  descriptor/plan hash. Reject missing/altered tables or triggers on reopen;
  every row identity/field is protected against UPDATE/DELETE/OR REPLACE.
- Existing ownership_banks can bind a new content-addressed manifest in
  snapshot_hash only through an explicit kind-dispatched upgraded reader; old
  consumers reject it. At union DONE, insert that absent pointer atomically with
  its manifest/parent and event. Manifest kind is `ownership_common_bank_v1` and
  contains `budget_id,capture_id,descriptor_hash,completed_source_hash,plan_hash,
  bank_response_hash,bank_request_hash,slot_response_hash,genesis_response_hash,
  discovery_revision_hash,discovery_query_hashes,U,F,ownership_indices,pool_indices`.
  Every array is reconstructed/validated, not accepted as caller authority.
  Existing clock_hash keeps the original full rpc_response_v1 clock; upgraded
  consumers validate method/options/kind accordingly. Bind clock once and retain
  the clock request hash in its DONE event. Never replace old v1 banks.
- `reserve_stage(capture_id,stage,method,params) -> intent` validates SEALED source,
  fixed predecessor/plan, next expected stage, current journal fence and capacity;
  increments existing used and commits PENDING in the same transaction.
  `finish_stage(intent,original_response_or_failure)` checks exact intent/fence
  and appends once; union completion also publishes absent bank atomically.
  `resume_capture(budget_id)` returns next unstarted stage or terminal ambiguity;
  it never calls a PENDING stage again. These functions run inside the canonical
  invocation/request locks rather than recursively acquiring them.
- Union receipt extension binds original bank/request/clock/request hashes plus
  the common manifest hash and canonical pool role indices. Protected ledger
  issuer derives all fields from the audited run, not a caller receipt. Use a
  new explicit union profile and ledger format capable of replaying both v1 and
  union rows; migration preserves original v1 payload/hash-chain identities.
  PoolCaptureBridge's v1 _records/_publish_run cannot be reused unchanged.

S is captured once in the slot DONE event; the exact union params are sealed in
its subsequent PENDING intent, not by mutating plan_json. T belongs only to the
original union response. The manifest's hashes and ledger trust establish local
provenance/authorized source scope, not authenticated Solana state. Supporting
these versioned records across consumers and mixed-profile ledgers is the
specific design-review dependency; there is no permissive backwards fallback.

## Durable ordering and recovery

Lock order: existing canonical research worker lock where applicable, canonical
ownership invocation lock, ownership request lock, then protected ledger lock.
Use the accepted stable local evidence/ledger identity and nonblocking behavior;
never call public lock-taking worker methods while holding their locks recursively.
Serialize with ownership and existing pool capture; do not run competing bank
acquisition or head publication. Database transactions must not span provider I/O.

Freeze one capture identity per bound scan/source; a fresh capture ID cannot hide
PENDING, failure, missing schema, changed plan or prior incomplete observations.
For each stage, atomically decrement remaining capacity by incrementing the SAME
ownership_budgets.used and append immutable PENDING intent (method, exact params,
stage, descriptor/source/plan hash, ordinal/fence) in one evidence-DB transaction,
commit durably, then perform at most one I/O. This requires a reviewed admission-
bound reservation+intent primitive: today's generic reserve followed by separate
bridge PENDING commit has a charged-without-intent crash window. No refund is
allowed in that window; current reserve persistence is proven by tests, not a
claim that the future atomic API already exists.

Append success envelope or redacted failure and DONE/FAILED in the evidence DB;
retain malformed/contradictory returned bodies as observations too. A shape,
finality-option, T<S, mint-policy or index-binding failure is terminal/unavailable
for this run and cannot insert an ownership bank. Pool semantic-policy failure
may preserve a structurally valid common bank but issues no positive point label;
atomically bind the successful union envelope to the absent common canonical
bank. Never publish a new bank pointer without its original raw record/binding.
After bank DONE, resume only clock(T) or publication, not genesis/floor/union.
After intent with uncertain outcome, stop unresolved: do not retry I/O or restart
with another ID. Responses lost before commit remain charged ambiguous attempts;
no response or bank is invented. A FAILED stage is terminal for that capture.

Clock(T) uses another durable reservation/intent. Complete original raw record,
clock, manifests and index binding before receipt publication. Publish an
idempotent protected receipt keyed by bound capture + bank/time hashes, then
record publication acknowledgment. Cross-DB crash after receipt append but before
acknowledgment reuses exactly that receipt without I/O or extra spending. Reader
availability waits for both bindings; a half-published artifact grants nothing.
Publication before owned-history replay is not ownership completion. Subsequent
history/head publication stays under the invocation lock and the exact source.

Retain every old capture/raw record, failed or ambiguous attempt, original source,
old canonical bank/head and every scoped receipt from every approved source.
Audit/publish all completed competing observations before admission; incomplete
ones block rather than being dropped. The versioned ledger must compare v1 and
union profiles at the same pool/mint/T using six-role raw bytes/owner/executable/
lamports/time; disagreement, unavailable originals, malformed or oversized scope
rejects. Never partition contradiction checks by profile, freshness or chosen refs.

## Frontier and budget feasibility

After capture, replay mint and ALL F account histories through actual T using
existing request-bound paging/order controls and the same18 counter. Require the
rediscovered frontier F_T equals planned F, with exact endings and complete supply
reconciliation. A new account/lifetime between discovery and T blocks completion;
do not extend U, recapture, change cutoff, or silently omit the new account. Common
state observation is feasible; complete history under a moving frontier is not
guaranteed. Already persisted canonical banks must instead use the existing
matching-point path, or report no feasible common capture for that investigation.

If A calls have already been spent, four unreused setup attempts plus h_m mint
continuation pages and sum(h_a) account pages must fit:
`A + 4 + h_m + sum(h_a) <= 18`.
Only exact previously persisted approved-source/descriptor-bound genesis/floor
stage records may remove corresponding setup calls; confirmed mint policy or a
summary slot is not a reusable finalized getSlot witness. A newer floor must never
rebind a frozen plan. Every failed/ambiguous/setup call remains in A. Shared raw
transaction diagnostics and prior pool captures consume the same capacity.

Illustrative three-holder fixture: original3 + genesis/floor/union/clock4 + mint2
+ account histories6 =15. Existing ownership-only cost13 becomes15 if common bank
replaces its two bank/clock calls; it is not13 plus a new independent four-call
capture with a guaranteed matching T. At A=5 with four two-page account histories,
5+4+2+8=19: infeasible. A100-key union does not imply enough history budget.

Preflight refuse if even pending required stages plus at least one page per
unfinished query cannot fit, source PREPARED, bank already fixed/incompatible,
base absent, union>100, unsupported token/program/layout or unresolved prior
capture. A lower-bound preflight is not a guarantee of exhaustion; later paging,
failures/new frontier can still consume remaining attempts and return PARTIAL/
BUDGET_EXHAUSTED with original bank/cutoff and no policy approval. No count reset,
new budget or arbitrary additional investigation is an escape hatch.

## Fixture proof and limits

Dedicated tests execute the proposed union/order/index contract only inside the
test module. A synthetic callable returns one ORIGINAL eight-account union at
T21 with request floor20; storage contains that one parent envelope, and both
semantic views reference its hash/entries. No fabricated sliced RPC evidence is
saved. Tests cover100/101-key bounds, missing/duplicate frontier, request/order/
slot/coverage substitution, current reader rejection, existing coincident positive
join and no bank recapture, real shared reserve durability/budget exhaustion and
four charged setup stages fitting a15-attempt envelope. A second, complete raw
history fixture supplies all three holder ending balances and six pool account
states to one synthetic union response at T20: direct indexed mint/holding-policy
checks reconcile full supply and preserve every original pool raw state. It
therefore proves more than matching two byte slices, while keeping all actual
receipt/ownership permissions unchanged. They do not implement
atomic future journal/schema/publication or prove live history coverage/source
trust. Closed extra entries in the union-only fixture are syntax/provenance
examples, not assertions of authenticated closed lifetimes.

All lifecycle, interval exclusion, private/common control, source authentication,
Token-2022 acceptance, entry and paper-readiness permissions remain absent/false.
Coordinator-only design review, coordinated implementation, independent crash/
concurrency/mixed-profile review and real supervised fixture-to-live acceptance
remain prerequisites. No provider/VPS/credentials/signing/broadcasting were used.

## Validation on final proposed source

Python3.12.14, fixture-only SQLite/transport and existing private loopback tests.

- `python -m unittest tests.test_common_bank_acquisition_contract -q`:
  10 tests in0.498s, OK.
- `python -m unittest discover -q`: 1,586 tests in69.379s, OK;
  zero failures/errors/skips.
- Staged diff whitespace check: clean. Exactly this report and dedicated test
  file change; production, shared readiness and queue files are unchanged.

An initial focused run had one test-harness error because it expected ValueError
from pool admission's internal _Reject exception. The assertion was corrected
without changing production. Earlier nine-test/full1,585 runs passed before the
complete-holder union feasibility test was added; the final full result above
includes that test and final fixture-call accounting proof. No known failures
remain. Passing tests prove the stated fixture feasibility and existing refusals,
not the unimplemented future journal/migration/receipt trust or live acceptance.
