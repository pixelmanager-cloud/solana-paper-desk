# Existing monitoring allowance: separate experiment handoff

This explicitly binds the existing evidence-side monitoring allowance to a
separately named, explicitly initialized paper experiment. It does not continue
old capital or rewrite old config/source/ledger history. Default policy is empty;
no deployment or provider call is authorized by this implementation.

## Operator contract

`monitoring_handoff.activate(research_db, evidence_db, old_ledger_db,
new_ledger_db, pacing_db, new_cfg, *, pins, at=None)` requires a matching reviewed
entry in `config/monitoring-handoff.json`, outside the hashed desk source domain.
Each entry has exactly `context`, `old_source`, `new_source`, `old_config_hash`,
`new_config_hash`, `old_runtime_hash`, `old_grant_hash`, `research_grant_hash`.
Context pins five distinct existing canonical absolute database paths: research,
evidence, old ledger, new ledger, pacing. No caller path becomes its own proof.

The new config digest is opaque to this handoff. Worker08 owns the explicit
`paper_token_profile_version: 1` semantics, requiring paper mode, signal3 and
quote-execution1; omitted profile remains legacy strict. Tests use that field on
synthetic config and retain legacy-token fills. They do not establish Token2022
entry support; that requires reviewed PR156 composition.

Under research-worker -> evidence-invocation -> both ledger-cycle locks (ledgers
sorted canonically), validate existing admitted dispatch/setup bindings, the
unchanged research1000/queue25 grant, shared existing pacing DB/configuration,
and the old ledger using its exact reviewed historical source and immutable
runtime receipt. Validate full bounded history/checkpoint and require zero old
open positions. Old metadata/config/source/checkpoint/history are never changed.
The current-source new ledger must have the explicit cycle initialization marker,
exact INIT-only event journal, no outcomes/positions, and its own configured
initial state/capital. Pending old monitoring attempts must be resolved first;
failed attempts/latches are preserved, not cleared.

An evidence-only BEGIN IMMEDIATE transaction appends one immutable binding,
records original budget and prefix hashes, adds a nullable reservation context-hash
column/fence, and advances only the budget format version 2 -> 3. The original
immutable allowance grant stays version2; ledger/config/code identity fields,
cap3600, window3600, high-water, total and latch remain unchanged. Existing
reservation/outcome values and evidence pages remain original. Exact context
replay is idempotent and does not change the original activation timestamp.
Different context/source/config/grant or partial/malformed publication refuses.
All reads use exact reviewed pins; policy revocation fails closed.

Current MonitoringBudget checkpoint/accounting/snapshot/reservation/completion
paths validate the active binding. Post-handoff reservations retain the original
allowance policy hash and additionally bind their immutable context hash. Old
prefixes remain unchanged; cumulative totals and rolling windows include old
charges. New identity is separate, not an aggregate profitability/capital claim.

## Frozen old binary fence

The genuine 8098 source expects budget version2. Version3 makes its accounting
check reject BEFORE any reservation, clock-latch or exhausted-budget update.
There is no reusable global bypass grant. A separate exact SQL insert fence also
requires the binding hash on each new reservation, and the old seven-column
INSERT fails after the additive column. Version downgrade is guarded. Tests run
the archived old implementation, create a synthetic old-code entry AFTER handoff,
then attempt its actual reserve and PaperReadSources transport path: both refuse,
with the complete evidence SQL dump unchanged and credential/opener mocks never
called. Successor readers additionally refuse changed retired ledger history.
Quiescing services is still operationally necessary; this module does not prevent
an old process writing its old ledger, and such writes cause successor refusal.

## Fixture provenance and bounds

`fixtures/monitoring-handoff-predecessor-desk.zip` contains unchanged desk .py/.json
blobs from b550100f5e760faec0d566c6b389d1e62b145062, byte-identical to 73edf43's
entire desk domain. ZIP SHA256:
7119d5fd7e796d46e301253de4b7de7a778bbed6437fa054cb11b58a036dc037.
Every old subprocess recomputes desk hash
8098b4033adff886006e2f6af5ccb0ce4bfa02e242f5fb1b4c7402c05ddaa8c9.
The earlier original-source archive remains unchanged and constructs genuine
15054558... identity before explicit synthetic 15054558 -> 8098 receipt/grant.
Reproduction: sorted `git ls-tree -r --name-only b550100 -- desk` .py/.json paths,
exact `git show b550100:PATH` bytes; ZIP DEFLATE9, timestamp1980-01-01, mode100644.
All databases, config, charges and transport envelopes are synthetic; no production
config bytes, accounts, secrets or records are included.

Policy <=64KiB / eight explicit edges; receipt uses the existing <=8KiB canonical
immutable policy-record contract. Metadata/checkpoint <=2MiB; historical event
bounds remain existing 10000 / 16MiB preflight. Original monitoring prefix <=100000
rows / 32MiB scalar preflight. Oversized or unavailable contexts reject without
reset. This is one handoff, not an automatic chain or future rollover mechanism.
Shared pacing state may advance through normal reads; handoff never provisions,
resets, refunds or writes it.

## Pending deployment inputs and validation

Coordinator supplied candidate config digest
30406d9cb727b3e518687dbb4c20820483bf51384f0edfb14a94f8221e2b595e
(original1a095e98... plus ONLY paper_token_profile_version1), planned new ledger
/var/lib/solana-desk/token2022-paper.sqlite and config
/etc/solana-paper/paper-token2022-reviewed.json. Existing monitoring grant digest
126f67fbf6c0e2aca2a8ff31c26b1f72928d84533ba67e36450b2517b8220bf8,
research grant346bcc22488e1616b18800d96ee3504262f2cdeeb0dc16563bb11fbfb5176338.
These are handoff inputs, not populated policy or deployment approval. Final exact
combined source hash, canonical pacing path, old runtime-receipt digest, and
independently reviewed context edge remain coordinator inputs. Runtime deployment
allowlist is unedited. Existing research/evidence/pacing must be reused, not copied
or reprovisioned. Root stops old execution and initializes the new experiment
explicitly only after independent review; source/profile activation is separate.

Focused component validation so far: Python3.12 Linux, 71 tests / 46.270s / OK,
zero skips (handoff, monitoring, allowance, runtime). Additional dedicated CLI and
old-held-position regressions and final full Linux suite are running. Exact final
results and source manifest will be appended after completion. No code/gate edits
outside monitoring_budget + the handoff module, dedicated fixtures/tests/report.
