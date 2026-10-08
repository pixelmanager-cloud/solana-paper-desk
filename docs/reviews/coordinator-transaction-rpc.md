# Disconnected coordinator raw transaction transport extension

Agent 05 follow-up to accepted PR46, 8 October 2026 (Korea time).
Baseline: `integration/cloud-wave1` at
`d6c710bcf95a6c61fd0f786489be4b458bf7b6da`.
Branch: `codex/cloud-wave1-05-transaction-rpc`.

## Narrow request contract

The dedicated `HeliusMainnetRPC` transport additionally accepts only:

```python
['canonical-base58-64-byte-signature', {
    'encoding': 'json',
    'commitment': 'finalized',
    'maxSupportedTransactionVersion': 0,
}]
```

for `getTransaction`. The illustrative signature above is a placeholder, not a
valid request. Validation requires an exact list containing an exact string and
an exact dictionary with precisely those three options. The signature must decode
to exactly 64 bytes and round-trip to identical base58 text; its input length is
bounded before decoding. The encoding and commitment must be exact strings with
the shown values; the version must have exact integer type and value zero.
`False`, `True`, floats, strings and integer subclasses cannot substitute for zero.
Missing/extra options, weaker commitment, parsed encoding, wrong byte lengths,
invalid alphabets, whitespace, bytes and string/container subclasses fail before
credential lookup or transport opening. No signature is cryptographically verified.

Existing fixed HTTPS endpoint, credential handling, JSON-RPC envelope binding,
byte ceiling, timeout, redirect rejection, single-open/no-retry behavior and safe
exception messages are unchanged. The transport still makes no discovery probes,
provider fallback or signing/broadcast calls. Invalid requests issue zero network
requests; a valid transport attempt issues at most one RPC POST.

Successful raw result content is returned unchanged, including `null`, failed
transaction metadata, mismatching signatures/mints or unsupported fields. JSON-RPC
errors remain transport failures with sanitized messages. The adapter adds no
caller/mint/signature-success/state/policy approval. The capture component must
separately bind and persist raw results, mark unavailable/null evidence incomplete,
and validate all necessary chain/state/transaction conditions.

## Source identity decision

The frozen descriptor remains
`ApprovedSource('helius-mainnet-single-request-v1', 'coordinator_capture')`, with
its existing mainnet network, genesis and receipt profile. It describes the same
provisioned provider origin, fixed endpoint, credential boundary and single-request
transport behavior; adding one narrowly constrained read-only RPC does not create
a second source or new trust domain. Exact method/params and raw response remain
part of request provenance rather than caller-selected source identity.

Retaining that descriptor does **not** turn PR46's earlier acceptance into approval
of this expanded capability. This exact change requires independent review and
explicit coordinator use/provisioning before capture integration. TLS/RPC evidence
is provider evidence, not cryptographic proof of chain state or transaction finality.

## Fixture verification and handoff

`tests/test_coordinator_transaction_rpc.py` adds nine focused tests using synthetic
64-byte signatures, local urllib handlers and mock failures. Production request
validation, request serialization and response/error dispatch execute directly;
no sockets, real credentials or provider calls are used. Tests verify exact POST
parameters and raw/null/failed results, canonical signature boundaries, strict
configuration/types, all five redirect codes without a second request, sanitized
RPC/connection/timeout errors without retry, byte-limit/JSON rejection and unchanged
source identity. The existing 14 PR46 tests continue exercising the shared transport
boundary.

Python 3.12.14, existing dependency-complete `.venv`:

- `.venv/bin/python -m unittest tests.test_coordinator_rpc tests.test_coordinator_transaction_rpc -q`:
  `Ran 23 tests in 0.552s`, `OK`.
- `.venv/bin/python -m unittest discover -q`:
  `Ran 1179 tests in 45.597s`, `OK`, exit 0, no skips reported.
  Approved sandbox access supports existing unrelated private-loopback and
  multiprocess fixtures, not provider networking.
- `git diff --check`: clean.

An initial test helper mistakenly replaced an explicit None signature with its
valid default, and one appended base58 character still encoded a valid 64-byte
signature. Corrected those fixture assumptions; no production gate was relaxed.
No unresolved focused-test failure remains.

Changes are limited to `desk/coordinator_rpc.py`, the dedicated tests and this
report. Worker01 exclusively owns capture persistence and budgets. This adapter
remains disconnected: no capture, entry/CLI, `providers.py`, shared queue/readiness
or source-roster wiring edits. The bridge must still durably reserve the charged
attempt before invocation, including failures, without refunds or budget expansion.
Independent review, coordinator provisioning and capture/state acceptance remain
required. No VPS/provider/real-secret access, signing, broadcasting, merge,
deployment, issue closure or paper-readiness claim.
