# Disconnected original-byte finalized-slot transport

Base: `fd8601da5017d1b5d227d77daca49e4c97983840`, accepted
`integration/cloud-wave1`, including PR95 integration
`893bf72ed79dee7be224bccf4c7ba3913f36d6b6` and the exclusive transport claim.
Branch: `codex/cloud-original-byte-slot-transport-01`.

PR95 established that the current result-only adapters cannot preserve an
original serialized request/response or string request identity. This change
adds only `desk/original_byte_slot_transport.py`, dedicated tests and this
report. The existing `coordinator_rpc.HeliusMainnetRPC` result-only API, all
acquisition/history/journal code and all readiness/entry controls are unchanged.
No existing production module imports or invokes the new API; there is no CLI.

## Narrow API contract

`HeliusFinalizedSlotTransport()(request_bytes) -> SlotByteExchange` takes one
immutable `bytes` request, 1–1,024 bytes. It has no endpoint, source, opener,
credential, retry, batch, fallback or timeout configuration arguments.

`validate_slot_request` accepts exactly one JSON-RPC object with the four keys
`jsonrpc`, `id`, `method`, `params`, version string `2.0`, method `getSlot`,
nonempty string ID <=128 UTF8 bytes, and exactly
`params=[{"commitment":"finalized"}]`. No missing/extra options, duplicate keys
(including escaped duplicates), numeric/boolean/null IDs, batch, notification,
unknown method, invalid UTF8/surrogate ID, trailing document or nonfinite number
is accepted. Validation and the request byte bound run before any credential
lookup or opener construction. Invalid input raises only fixed
`SlotRequestInvalid("Finalized slot request invalid")` with suppressed context.
The API deliberately requires bytes rather than silently converting mutable
buffers or text into a new asserted original request.

Valid input is passed directly as `urllib.request.Request.data`; it is never
reserialized. Whitespace, key order and escaped string ID spelling are retained.
Response ID comparison uses the exact decoded string value/type; a legitimate
different JSON escape spelling of the same value is accepted while both
original byte strings remain unchanged.

`SlotByteExchange` is immutable and contains:

- `request_bytes`: exactly the validated supplied/sent bytes.
- `response_bytes`: exact observed bounded HTTP body bytes, or explicit `None`
  if no bounded original body was obtained. Invalid JSON/framing/slot syntax,
  valid RPC error bodies and bounded truncated bodies remain original evidence.
- `failure_code`: `None` only for accepted local finalized-slot response syntax;
  otherwise a fixed code below. This is not journal completion or authentication.

Failure codes are `CREDENTIAL_UNAVAILABLE`, `TRANSPORT_ERROR`, `HTTP_REJECTED`,
`RESPONSE_HEADERS_INVALID`, `RESPONSE_OVERSIZED`, `RESPONSE_TRUNCATED`,
`RESPONSE_INVALID`, `RPC_ERROR`. They never include exception text, URL,
credential, headers, provider message/data or caller ID. Raw evidence fields are
excluded from the dataclass representation. `diagnostic()` emits counts/fixed
codes only and always REJECT, with budget/intent/source/finality/entry flags
false. Opaque raw evidence must not be logged merely because it was captured.

An oversized body is not truncated into a fabricated complete response. No
prefix is returned for a cap overflow. `IncompleteRead` retains its original
bounded byte partial explicitly as truncated; a nonbyte/oversized partial is
rejected. A bounded `response_bytes` value with a failure code is **not** a
successful completed slot response. Missing body, empty body, invalid complete
body, partial body and oversized/unread body remain distinguishable. No body
is fabricated when an HTTP status/header is rejected before reading.

## Fixed protections and response grammar

The implementation reuses the accepted `coordinator_rpc` endpoint, credential
selection/validation, `Request`, no-redirect handler, default verified TLS/proxy
opener and fixed 15-second urllib timeout. Endpoint remains exactly
`https://mainnet.helius-rpc.com/` with the locally selected `HELIUS_API_KEY`
query value. Constructor performs no I/O and does not copy/store a credential.
The key is local to the call and never included in exchange records/failures.

At most one `opener.open` is called per valid invocation. There is no retry,
redirect, discovery, user URL, source override, batch or fallback. All standard
redirect codes are rejected by the existing handler before following Location;
HTTP errors are closed and returned as static failures. Non-200 status rejects.
Only identity Content-Encoding is supported; duplicate length/encoding headers,
malformed/non-ASCII/overlong lengths and lengths above the cap reject before
reading. The read is bounded to 65,537 bytes to detect the 65,536-byte ceiling;
declared Content-Length must agree with observed bytes. Responses close on all
paths. The accepted older adapter keeps its existing 2MiB cap unchanged; this
separate primitive intentionally uses the smaller existing slot-journal-sized
64KiB bound. The inherited timeout is urllib's network-operation timeout, not
an assertion of a durable whole-operation deadline or RPC completion.

JSON decoding is strict UTF8, duplicate-key rejecting, nonfinite/overflow-number
rejecting and bounded; integer conversion is limited before parsing huge
decimal integers. A response must have exactly version/id/result or
version/id/error framing and match the supplied string ID. A result must be an
actual nonboolean integer `0 <= slot < 2**64`. Null, floats, negatives, objects,
flags, unknown fields, combined result/error, wrong IDs and overflow reject.
An error must have integer signed-64 code, string message and optional data,
without extra members; its original body is retained with `RPC_ERROR` and
provider text is never promoted into redacted failure metadata.

The transport does **not** derive `C+1`, enforce a later signed-slot/history
range, prove a finalized bank, or authenticate anything merely because the
requested commitment is finalized. In particular max-u64 is valid slot syntax
here but may be infeasible for the future bounded discovery-cutoff consumer.
That consumer must reject overflow/infeasibility, never refresh a cutoff or
manufacture an original response.

## Integration prerequisites and exclusions

The primitive has no admission ID, descriptor, budget, session/fence, SQLite,
source registry or journal API. It cannot reserve/refund/reset capacity or
provide idempotency across invocations. Direct repeated calls are not safe
research admission; only a future independently reviewed integration may call
it after atomic shared-18 reservation and durable original-request intent.
This module does not authorize any real use merely by being importable.

Worker02 exclusively owns the pre-SEALED slot journal. Its unfinished API is
not imported, changed or assumed. Later integration must bind exact original
request bytes/ID to the admitted descriptor/source/live completion capability,
persist the original body/fixed failure without rewriting records, leave lost
acknowledgement/PENDING attempts terminal and charge every failed/setup attempt.
Mapping these fixed transport failure codes into its reviewed durable failure
contract remains integration work; HTTP length is not an authenticated observed
body length, and no invented `observed_bytes` field is supplied here.

No getAccountInfo/history expansion, provider transport migration, operational
quiescence, receipt issuance, source/finality/ownership/lifecycle/entry approval,
Token-2022 support, live acceptance, providers/VPS/secrets/signing/broadcasting,
shared queue/readiness changes, deployment or merge occurs.

## Fixture validation

Tests use a mock opener with in-process local urllib handlers, synthetic
credential lookup and explicit empty proxy handler; no listener/network/provider
or real Helius credential lookup occurs. Verified TLS settings are inspected
without a handshake. Tests assert exact whitespace/field-order/escape identity,
string ID and UTF8 bounds, malformed requests before credential access,
duplicates/bools/nonfinite/overflow/framing/slot checks, all redirect codes,
HTTP errors and one attempt, body/header caps, truncation/partial provenance,
close behavior, fixed redaction, false flags and unchanged older result API.

Python 3.12.14 focused command:
`python -m unittest tests.test_original_byte_slot_transport tests.test_coordinator_rpc -q`:
34 tests PASS in 0.848s (20 new +14 existing), zero failures/errors/skips.
Full Linux command `python -m unittest discover -q`: 1,906 tests PASS in
153.546s, zero failures/errors/skips. Exact head/tree/source manifest are
recorded in the PR. `git diff --cached --check` is clean.
Logs remain in `work/original-byte-slot-focused.log` and
`work/original-byte-slot-full.log`.

The initial compatibility-test harness constructed its mock SSL opener while
all environment `.get` calls were replaced with the synthetic key. SSL therefore
mistook that synthetic string for a key-log filename and raised FileNotFoundError.
The mock opener is now built before the credential lookup patch. No live key,
TLS bypass, transport timeout or production compatibility change was introduced
to correct the harness. No unresolved fixture failure is claimed fixed by merely
running unrelated tests.
