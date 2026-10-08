# Disconnected original-byte OWN-1 read transport

Base `bc5b8d0b85d72e47f85c08dc74b0e6919dd2bed7` is the accepted
integration dispatch following PR96 integration `78f9ed8381e58798075a508b9d54f3f20f702fb0`
and accepted-review record `6bf9d1d`. Branch:
`codex/cloud-original-byte-read-transport-01`. No PR97 code/API is imported or
required. No producer, acquisition, history, journal, CLI or policy wiring changes.

## API and original evidence

`desk.original_byte_read_transport.HeliusOriginalByteReadTransport()(request_bytes)`
accepts immutable `bytes` only and returns the existing immutable
`SlotByteExchange` (also exported as `ReadByteExchange`). Its constructor has no
configuration or I/O. `validate_read_request(bytes)` returns a parsed local
request for inspection, never a normalized substitute for the supplied bytes.
Invalid requests raise fixed `ReadRequestInvalid('Original-byte read request invalid')`
before credential lookup or opener creation. Malformed response syntax returns
fixed `RESPONSE_INVALID` while retaining its bounded original body. Errors retain
their original bounded body with `RPC_ERROR`; exception text is never copied.

The existing finalized slot class, request validator, result grammar, 1-KiB
request/64-KiB response bounds, exceptions and diagnostics stay compatible.
The older result-only `HeliusMainnetRPC` is unchanged. The accepted one-POST body
exchange was extracted to a private shared helper in the slot module, so TLS,
endpoint, header rejection, truncation and redaction have one implementation.
Its validators/limits are supplied only by the module-owned explicit dispatch;
the new public class accepts no method/configuration/endpoint/credential arguments.

Every request has exactly `jsonrpc`, `id`, `method`, `params`; JSON-RPC `2.0`,
nonempty UTF8 string ID <=128 bytes, exclusive non-batch framing, no duplicate
members/nonfinite numbers, integer conversion <=20 digits. Exact request bytes,
field order, whitespace, Unicode escape spelling and key order are sent once.
Response string ID must equal the decoded request ID and type. Neither request
nor response bytes are rebuilt. Raw fields are excluded from representation;
they must still be handled as opaque possibly sensitive evidence.

## Explicit request and response grammar

These shapes come from current accepted `ownership_acquisition`, `history`,
`HistoryProgress.capture_bank`, `ownership_worker`, `common_bank_journal`,
`common_bank_view` and pool capture contracts. This is not arbitrary RPC dispatch.

| Method | Only accepted request params | Accepted local result syntax |
| --- | --- | --- |
| getSlot | `[{commitment:finalized}]` | Existing unsigned u64 integer grammar |
| getAccountInfo | `[canonical32byteKey,{encoding:base64,commitment:confirmed}]` | Exactly context/value; context integer slot in [0,2^63), optional nonempty UTF8 apiVersion <=128 bytes; value null or account syntax below |
| getTransactionsForAddress | `[canonical32byteKey,{transactionDetails:full,sortOrder:asc,limit:100,commitment:finalized,encoding:jsonParsed,maxSupportedTransactionVersion:1,filters:{slot:{gte:0,lt:L},status:any,tokenAccounts:none}}]`, optional paginationToken | Exactly data plus optional paginationToken; <=100 dictionary rows; cursor null/absent or nonempty UTF8 string <=1024 bytes |
| getGenesisHash | `[]` | Canonical base58 32-byte string; not a trusted genesis/network pin |
| getMultipleAccounts | `[orderedUniqueKeys,{encoding:base64,commitment:finalized}]`, optional minContextSlot | Exactly context/value; context integer slot T in [0,2^63), T>=supplied floor; exactly one account/null per requested key |
| getBlockTime | `[actualSlot]`, integer in [0,2^63) | Integer timestamp in [0,2^63); null is retained RESPONSE_INVALID, not available time |

All object keys are exact as shown, except the specifically listed optional
fields. Account lists contain 1..100 canonical base58 32-byte keys and preserve
caller-supplied order; duplicates reject rather than deduplicate/reorder/split.
Optional minimum context slot is an integer in [0,2^63), never bool. A greater
actual T is legitimate syntax; minContextSlot does not select historical state
at S. S is not rewritten. Signed bank bounds match the reviewed common-bank
validator and union contract; a getSlot u64 value outside those bounds must
remain an honest later infeasibility blocker.

History is only genesis-inclusive slot-bounded, with integer `0<L<=2^64` (the
exclusive upper bound can be max-u64 cutoff+1). Its exact fixed limit/version
cannot be replaced with bools. The opaque cursor is not normalized; <=1024 UTF8
bytes is this deliberately conservative supported wire profile. Nonzero lower
slots, time-window queries, alternate token filters, status/sort/detail/encoding
or transaction version forms are unsupported here, even if another existing
general collector accepts them. This does not change ordinary screen defaults.
An oversized cursor is retained in a failed bounded response rather than used
to send another request or increase a cap.

Account syntax requires exactly owner/executable/data and permits lamports,
rentEpoch, space. This permits the existing reduced synthetic fixtures; absent
optional metadata is not filled in or claimed established. Owner is a canonical
32-byte public key, executable is bool, data is exactly `[string,base64]` with
strict canonical base64; numeric metadata is unsigned u64, optional space must
match decoded byte length. Null entries remain null, neither zero balance nor
proved closure. No program/layout/authority policy is evaluated: Token-2022,
unknown programs and arbitrary raw layouts remain original captured syntax,
and existing token/bank policy still rejects unsupported states. No legacy
mint success or supported launch anchor is invented by transport success.

History rows deliberately remain opaque dictionaries. Missing transaction
fields, failed transactions, unsupported versions, parsed-only CPIs, duplicate
or conflicting operations, missing birth witnesses and incorrect chronology
remain downstream decode/replay gaps. A locally accepted page is not complete
history. Unknown page-level summary/completeness fields reject, not elevate.
Existing request/history inventory, cursor-cycle, slot-range and exhaustive
frontier checks remain mandatory.

## Bounds and fixed transport protections

Genesis, clock, mint and slot requests are <=1 KiB. Account-bank and history
requests are <=8 KiB. Account-bank, mint, genesis, clock and slot original HTTP
body caps are 64 KiB; history is <=2 MiB. The latter matches existing accepted
diagnostic discovery bounds and older adapter ceiling, below the evidence
store's 16-MiB storage ceiling; it does not widen any history page or request
budget. New response JSON traversal is iterative with <=100,000 value nodes and
depth<=64; UTF8 strings/keys reject lone surrogates. Child count is checked
before expanding a traversal frontier. Byte caps precede parsing; these are
serialized/traversal bounds, not a claim of bounded total Python RSS.

One `read(cap+1)` detects overflow; no oversized prefix becomes evidence. Bounded
IncompleteRead partials remain original truncated bytes, never completed bodies.
Content-Length is duplicate-aware, ASCII decimal <=20 digits and <=method cap;
mismatch truncates. All Transfer-Encoding occurrences, even empty/identity or
well-formed chunked, reject before body reading. Content-Encoding must be absent
or identity. Header/status rejection leaves response bytes explicitly absent.
Strict JSON-RPC framing/result-vs-error checks and matching string ID run after
bounded body acquisition. Unknown fields/framing/results retain the original
body and fixed rejection, not a synthetic response.

Endpoint is fixed `https://mainnet.helius-rpc.com/`, credential lookup is the
accepted `HELIUS_API_KEY` only, same bounded printable key grammar, urlencode,
standard verified TLS/proxy behavior, no redirect handler, 15-second network
operation timeout, no retry/batch/fallback. The inherited timeout is not a
whole-operation wall-clock deadline. No caller URL, injected public opener,
source credentials or source-approval property is added. Tests replace the
opener only; they never read actual credentials or contact a provider.

## Integration dependencies and limits

This API is disconnected. Later independently reviewed cohesive producer
integration must durably reserve/fence every attempted request under the same
18-attempt investigation budget, enforce the admitted descriptor/scan binding,
four acquisition attempts and <=2 acquisition history pages/invocation, save
original request/response bytes atomically, and refuse ambiguous crash replay.
The class itself has no persistence, retry suppression, budget management or
authority to investigate signatures/mints. Repeated direct calls remain repeated
I/O; calling it directly is not admission. Exact producer request IDs and stages
must be independently bound before I/O, not established by matching wire strings.

The original union parent/equivalent record must also fit its existing 64-KiB
record bound; a <=64-KiB wire body alone cannot establish that. Complete holder
frontier, actual slot/time/mint/vault state and source pins still require their
existing replay checks. Capturing these shapes does not solve unknown history,
unsupported tokens/anchors, raw caller/CPI/controls, interval safety or entry
policy. No candidate or provider flag becomes source/finality authentication.

`diagnostic()` always returns REJECT, with budget_reserved, intent_authenticated,
source_authenticated, finality_authenticated and entry_allowed false. No receipt
issuance, journal import, PR97 dependency, default trust provisioning, runtime
wiring, approvals, migration, provider/VPS access, signing, broadcasting,
shared queue/readiness change, merge or deployment occurs.

## Fixture validation

Dedicated tests use real HTTPResponse over BytesIO-backed fake sockets, mocked
openers and synthetic credential lookup. Existing synthetic history records are
read unchanged; the pure accepted collect_history function generates exact
initial/continuation request shapes. Tests exercise original whitespace/escapes,
UTF8 IDs/cursors, invalid requests before credential access, account order/100
bound, bank advancement/regression/coverage, unsupported owner retention,
genesis/time/account/page syntax, raw errors, JSON resources, 64-KiB/2-MiB
boundaries, all transfer encodings, truncation, one POST and fixed redaction.
Existing finalized slot and older result API tests run together. No network
listener, provider call, real key lookup or changed existing fixture is used.

Focused Python 3.12.14: 62 tests PASS in 0.985s, zero failures/errors/skips.
An initial dedicated fixture test looked for a top-level data page in the saved
synthetic fixture; its existing raw transactions are under records. That test
lookup was corrected, with no fixture or production change. Full Linux:
1,934 tests PASS in 152.152s, zero failures/errors/skips. An AST comparison
confirmed that the extracted shared exchange preserves accepted slot I/O logic
except module-owned bound/validator parameter names. Exact head/tree and tracked
source manifest are recorded on the PR. Logs remain
in work/read-transport-focused.log and work/read-transport-full.log.
