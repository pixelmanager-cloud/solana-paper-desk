# Disconnected coordinator Helius single-request transport

Agent 05 prerequisite, 8 October 2026 (Korea time).
Baseline: `integration/cloud-wave1` commit
`586bae86f8858f6c642be184b87c93d0995f7ceb`.
Branch: `codex/cloud-wave1-05-coordinator-rpc`.

## Contract

`desk/coordinator_rpc.py` exposes `HeliusMainnetRPC()`, a callable with no
constructor configuration or constructor I/O. Its immutable `source` is an
`ApprovedSource` with ID `helius-mainnet-single-request-v1`, kind
`coordinator_capture`, network `mainnet-beta`, the existing pinned mainnet genesis
hash and the existing receipt profile. The coordinator must explicitly provision
this exact descriptor in its protected source roster; neither candidate data nor
RPC response fields select the endpoint or identity. The descriptor identifies
the configured transport, not independent verification of the provider's claims.

The only URL root is `https://mainnet.helius-rpc.com/`. `HELIUS_API_KEY` is read
from local invocation context each time and URL-encoded in the transient request.
It is never constructor state, descriptor content, persisted evidence, a log or
an exception message. Tests use explicitly fake keys only. This change neither
reads coordinator secret files nor adds provisioning or command-line wiring.

Only `getGenesisHash`, `getSlot`, `getMultipleAccounts` and `getBlockTime` are
accepted. Requests have JSON-RPC 2.0, integer ID 1 and POST method. Slot/bank
requests require finalized commitment; bank requests require base64 encoding and
1–100 syntactically valid account addresses, with an optional nonnegative integer
minContextSlot. Clock slots must be nonnegative integers. No caller URL, source,
headers or credential arguments are accepted.

After validation there is exactly one opener invocation and at most one HTTP RPC
POST. All five redirect status codes (301/302/303/307/308) close the response and
abort before another request. No retry, discovery, preliminary genesis/slot probe,
provider fallback, signer or broadcasting path exists. HTTP/protocol/read failures
abort, including rate-limit and server failures. Standard HTTPS certificate
verification and inherited urllib proxy handling remain enabled. HTTPS proxy
connection establishment and DNS/TLS mechanics are not additional RPC calls.

The opener uses a fixed 15-second socket timeout. This is the normal urllib
socket-operation timeout, **not a hard wall-clock acquisition deadline** against
a continuously streaming peer. Response reads are capped at 2 MiB plus one byte
for overflow detection; declared oversized/invalid lengths are rejected before
reading. The adapter rejects non-200 status, unsupported Content-Encoding,
length mismatch, malformed UTF-8/JSON, duplicate JSON members and nonfinite or
unrepresentable numeric values. No compressed-response expansion is performed.

Response envelopes must contain exactly jsonrpc/id plus one of result or error,
with version 2.0 and matching integer ID 1. Error objects require integer code and
string message (optional data); every RPC error is rejected without printing its
body. Successful result content is returned unchanged. Method-specific account,
slot, genesis and policy validity remain the capture/replay layer's responsibility;
a JSON-RPC null result is not a successful required-state attestation. All regular
construction, local credential, URL, HTTP/header/body and parsing exceptions become
one static `CoordinatorRPCError` message with the original display chain suppressed.
HTTP error bodies are closed without being logged or parsed.

TLS/RPC evidence is **not cryptographic chain proof**. No holder/ownership,
liquidity, freshness, route, strategy or entry eligibility follows from this
transport. The existing bridge must reserve the shared charged attempt durably
*before* invocation, including attempts that fail or never reach HTTP. This adapter
has no budget state, refunds, reserve bypass or production entry integration.

## Local validation

`tests/test_coordinator_rpc.py` uses real urllib opener/redirect/error dispatch
with local HTTPS/HTTP fixture handlers, plus mocks for construction/read failures.
It opens no sockets and makes no provider requests. Redirect tests span each
status with HTTPS, HTTP and relative Location values; all count exactly one
request. The tests inspect the POST, fixed endpoint, timeout, RPC binding,
invocation-time key rotation and fixed frozen descriptor. Adversarial cases cover
missing/bad keys, forbidden methods/options, provider/URL/header/read errors with
a fake key, malformed envelopes/errors, duplicate fields, invalid bytes and numeric
constants, oversized and truncated bodies, and the exact byte ceiling. Errors are
checked through both string formatting and displayed traceback for key leakage.

Python 3.12.14, existing dependency-complete `.venv`:

- `.venv/bin/python -m unittest tests.test_coordinator_rpc -q`:
  `Ran 14 tests in 0.458s`, `OK`.
- `.venv/bin/python -m unittest discover -q`:
  `Ran 1052 tests in 41.800s`, `OK`, exit 0, no skips reported.
- Whitespace check: `git diff --check`, clean.

Initial test construction had two mock context-manager setup errors; corrected
by using MagicMock for the opener response context. No production defect was
hidden or test skipped. Final full-suite execution uses approved sandbox access
for existing unrelated private-loopback/multiprocess fixtures, not RPC networking.

## Handoff and remaining prerequisites

Changes are confined to this module, its dedicated tests and this report.
`providers.py`, CLI, existing bridge and all entry gates remain unmodified.
Independent review, coordinator source/credential provisioning and explicit bridge
integration are required before use. This supplies transport mechanics only;
trusted raw capture, cross-checking, budget enforcement, replay, ownership exposure,
route/strategy validation and forward paper acceptance remain separate gates.
No live provider access, real keys, VPS, issue closure, merge, deployment, paper
readiness or cryptographic verification claim is made here.
