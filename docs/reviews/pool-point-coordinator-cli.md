# Existing-admission pool-point coordinator command

Base: accepted 9e43752daabf19b7e78e08e0b15d16d1c6e2a680.
New composition only: desk/pool_point_capture_cli.py, dedicated fixture tests,
and this report. Bridge, transport, ledger, admission and journal contracts are
unchanged. No presealed work is inspected or used.

Invocation:

```
python -m desk.pool_point_capture_cli --coordinator-diagnostic \
  --evidence-db /private/evidence.sqlite --ledger-db /private/receipts.sqlite \
  --scan-id EXISTING_SCAN --capture-id EXISTING_OR_NEW_CAPTURE
```

The invocation flag records explicit local operator intent, not authentication.
Only already provisioned canonical owned/private Linux files are supported. Mint,
descriptor hash and canonical pool derive from the existing persisted admission.
The command creates no job/admission or budget. It requires exactly the fixed
HeliusMainnetRPC source in the protected ledger's persisted configuration, default
60-second freshness and synthetic-fixture opt-in false. The local ledger ID comes
from that existing protected descriptor, not an argument. No source, endpoint,
policy, freshness, trust, mint or credential parameter is accepted. Live invocation
still requires independent coordinator provisioning/source approval; a protected
file and this command do not supply cryptographic provider/finality proof.

Preflight reuses the accepted canonical/private path checks and rollback-only
Linux OFD guard. WAL/journal/alias/missing/wrong-mode paths reject before SQLite
reads or initialization. Descriptor/admission size preflight precedes body reads.
The existing read-only ledger audits exact configuration, identities, schema and
receipt chain. Only after valid admission/configuration can the existing writer
be reopened; files must already exist and retain their identities. No ledger
provisioning occurs. A read-only bridge audit checks existing capture configuration,
identity and prior failures before capture initialization, including budget
headroom for every remaining stage. PREPARED admission is refused. The exact
existing contracts recheck bindings and charged reservations during capture;
normal workers still obey their locks and stable owned-file assumptions.

At most four existing bridge calls occur: genesis, finalized floor S, canonical
six-account bank T>=S, and blockTime(T). Each is durably charged to the existing
shared18 before transport. Completed replay may use zero calls even at18/18;
failed/PENDING captures cannot be retried or hidden by another capture ID. Missing
headroom rejects without initializing the capture journal. Existing captured
responses/receipts and contradictions remain unchanged. Calls add no admission,
so daily10 and queue3 behavior remains intact. All original freshness/clock and
terminal uncertainty rules remain those of PoolCaptureBridge.

Output is a fixed redacted status/counter/attempt summary only. No raw responses,
paths, mint/IDs, exception strings, provider details or credentials are printed.
CAPTURED_POINT is the existing point status, not permission to exclude historical
holders or trade. Chain authentication, interval exclusion, continuity, private
control, ownership and entry flags remain false. No classifier/exposure adapter,
common-bank producer, scheduler or paper-trader consumer is connected.

Tests execute the actual parser/main/_run composition with real existing admission,
ledger and fixed RPC adapter through locally mocked HTTPS only. Coordinator time
is fixture-controlled. Cases cover four reservations, actual bank107 versus
floor100, zero-call byte-preserving completed replay at exhausted18, invalid
admission/missing or aliased paths/forbidden policy arguments, insufficient budget
with no initialization, changed provisioned configuration, terminal transport
failure and lost-completion PENDING with same/new IDs, WAL refusal without sidecars, credential/opener access
not reached on invalid admission, and redaction/false flags. Initial credential
probe also blocked argparse's terminal-size lookup; narrowing to HELIUS_API_KEY
resolved that test issue. No actual credential or provider access occurred.

Targeted Python3.12.14 results: 15 CLI/integration tests in5.645s, OK. Final full
Linux suite: 1956 tests in375.442s, OK. Existing private-loopback fixture tests are
authorized; this command's provider calls remain mocked. No source contract,
shared queue/readiness, deployment, merging or live paper readiness claim.

Remaining live prerequisites: an independently provisioned fixed-source ledger,
a stable private supported filesystem, existing exact admission and sufficient
original headroom, local credential invocation, and fresh valid legacy canonical
pool state. Missing same-bank ownership revision/interval evidence, fee/liquidity/
route/exit and entry prerequisites remain unresolved. The command cannot rewrite
an older bank to match a newly returned point or select a historical bank.
