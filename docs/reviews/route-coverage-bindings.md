# Exact checked instruction coverage

Base integration f5eb8541c05e32cc836656e833b73d2841d95d23.
Branch codex/cloud-route-coverage-bindings.

The old aggregator used component-wide passed flags to label unmatched PumpSwap
callbacks, fee-program rows, SPL transfers and system/token setup. It also treated
all outer rows as covered by blanket envelope/router flags. A stale successful
checker result could therefore hide additional instructions at new paths.

Coverage now requires an exact detached diagnostic instruction receipt produced
by the relevant checker: instruction path, program, original data_base64, ordered
account addresses, stack height, parent path and parent program. Each receipt
must match one unique inventory path and all identity fields/types. Repeated
paths are all uncovered, duplicate receipts are ambiguous, and boolean stack
heights cannot compare equal to integer heights. Only the exactly bound fee
query may suppress its inventory UNSUPPORTED_ROUTE_PROGRAM flag. All other
inventory flags stay blocking.

Minimal receipt output fields are added to AMM, sell-event, fee-query, recipient,
setup, envelope and router checkers. Their semantic conditions, amount/profile
checks and passed decisions do not change. Failed checks emit no usable receipt.
Recipient receipts enumerate precisely the transfers checked; setup receipts
enumerate precisely the system/token operations checked. Envelope receipts carry
outer indices; the router receipt has no index because that checker only receives
one instruction. Coverage therefore requires the envelope's exact path plus a
unique outer row matching the router's exact raw/account identity. An identical
second router row does not inherit the first one's receipt.

No checker receipt is a signature, chain authentication, trusted policy or
approval. Reproducing a receipt from caller-asserted data does not create trusted
validation. The normal wrappers still have to run the independent checkers on
one coherent inventory and source. This aggregator checks consistency only:
full_route_policy_passed=False and transaction_policy_ok=False unconditionally.
No fee split, empirical observation or caller passed/verified flag is promoted.

## Explicit unavailable bindings and limits

Legacy component dictionaries containing only passed=True have no row witnesses.
They fail coverage with ROUTE_CHECKER_RECEIPTS_UNAVAILABLE and the component names
in unavailable_bindings; uncovered rows identify the exact path/program gap.
Partial receipts or changed bytes/accounts/context cannot authorize that row.
Empty receipt lists mean the checker checked no rows of that role, never all rows.

Effects, controls, debits, fee totals and fee split remain mandatory component
checks, but do not supply instruction roles. They are not reinterpreted as CPI
approval. Original checker input/report records are not rewritten; offline replay
recomputes receipts from its original inputs.

The normalized inventory contains ordered outer account addresses but does not
carry outer signer/writable flags. Envelope/router checkers independently inspect
those original flags; this coverage layer cannot reauthenticate their privileges
from the inventory alone. Stack/context identity is likewise not proof of CPI
signer authority or runtime execution success. Persisted source admission, full
route control/effect/order policy, verified exact fee economics and fresh runtime
validation remain separate blockers. This change does not invent those witnesses
or make the historical fixtures fresh.

## Adversarial validation

Dedicated tests use the actual offline event, fee-query, recipient, setup,
envelope and router checkers with public/synthetic fixtures. Extra rows at new
paths (including identical bytes with the same program), changed bytes/amounts/
decimals/ordered accounts/context, duplicate paths/receipts, blanket or partial
receipts, malformed identities and boolean stack types remain uncovered. Receipt
accounts are detached; failed checker outputs do not cover rows. Unit component
placeholders are explicitly synthetic and grant no full transaction acceptance.

Scope: route_coverage plus minimal receipt fields in seven existing checkers,
dedicated tests, existing coverage fixture updates and this report. No provider,
VPS, secrets, signer, broadcast, paid service, shared readiness/queue, entry policy,
pool journal, decoder, history or acquisition changes. Coordinator alone reviews,
integrates and deploys; live acceptance and paper readiness remain outstanding.

Final Python 3.12.14/Linux fixture-only validation:
- Dedicated coverage plus related checker suites: 84 tests in 0.135s, OK.
- Full unittest discovery: 1396 tests in 51.681s, OK, zero skips/failures.
- git diff --check: clean. No known test failures.
