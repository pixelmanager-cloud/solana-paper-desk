# Manual supervised quote-paper cycle

This is an explicit one-pass interface, not an enabled service, continuous
monitor, current safety certificate or paper readiness claim. Only the coordinator
runs provider-backed invocations after independent dependency acceptance. Cloud
tests use injected synthetic responses. Do not run these commands against an
existing strict experiment or production deployment without coordinator review.

The wrapper calls `desk.paper_cycle.initialize` and `run_once` unchanged. Its
configuration must contain `mode: paper`, integer `paper_signal_policy_version: 3`
and integer `paper_quote_execution_version: 1`, and omit
`experimental_policy_version`. Retain all existing strategy/fee/slippage settings;
this wrapper does not choose thresholds, fees or sizing. The complete config/code
fingerprint belongs to a new experiment; changing it cannot adopt an old ledger.

Create the new experiment separately (refuses every existing ledger path):

```sh
python -m desk.paper_cycle_cli --config REVIEWED_CONFIG.json init --ledger-db NEW_EXPERIMENT.sqlite
```

Targets are a local operator file at most64KiB, exactly this shape:

```json
{
  "position_targets": [],
  "candidates": [],
  "usd_evidence_refs": []
}
```

Each target row has exactly `scan_id`, `mint`, `pool`, `taker`, `amount_raw`,
`provenance`, `pool_fee_bps`, `graduation_refs`, `known_hazards`.
Addresses are public keys; amount_raw is an exact positive integer, not human
units. At most18 targets total. The original admission/scan and all saved open
positions must match the persisted ledger, including exact current raw quantity,
pool and taker. Position reads precede candidates; this is checked by the cycle,
not authorized by target JSON. Known hazards are explicit adverse uppercase code
labels (at most32); an empty list is not a safety certificate. Fee is null or an
explicit decimal string hypothesis, never executable fee proof.

Provenance is `SYNTHETIC_TEST_ONLY` or
`PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE`; neither authenticates a provider.
`graduation_refs` contains at most8 retained `history_request_v1` manifest hashes.
The cycle replays those original successful migration transactions; no operator
numeric graduation or holder timestamp is accepted. Holder history remains null
and visibly unknown under the explicit paper profile. USD refs are empty or the
exact three retained attempt hashes required by the cycle; absence triggers its
bounded genuine acquisition, never a USD/peg default. Unknown source evidence
blocks entries. Never fabricate manifests, admission, quantities or history.

One explicit supervised invocation:

```sh
python -m desk.paper_cycle_cli --config REVIEWED_CONFIG.json once --research-db EXISTING_RESEARCH.sqlite --evidence-db EXISTING_EVIDENCE.sqlite --ledger-db NEW_EXPERIMENT.sqlite --targets REVIEWED_TARGETS.json
```

Runtime credentials remain the accepted transport environment contract. For
systemd, use existing `LoadCredential=provider-keys.json:/etc/solana-desk/provider-keys.json`
and add `--systemd-credentials`; the wrapper reads only
`$CREDENTIALS_DIRECTORY/provider-keys.json`, accepts existing managed0400/0440/0600
modes, and never accepts caller credential paths or prints values. No systemd
unit, timer, listener or install/enable action is supplied by this change.

Supply actual unresolved integration/review facts with repeated
`--dependency-blocker CODE`; any blocker refuses before credentials, database
access or network. Pending dependency review is not cleared by a JSON safe flag.
Existing explicit `--control PAUSE_ENTRY|EXIT_ONLY|LIQUIDATE|RESUME` becomes a current
operator control event; it does not repair evidence or authorize an entry.
No reset/recovery/admission or automatic enable command exists.

Exit0 means initialization or completed pass, including a rejected candidate;
it does not mean entry approval, profitability or live readiness. Exit2 means
blocked/recovery/unavailable. Output keeps EXECUTION_UNVERIFIED, original
historical migration timestamps/hashes and bounded rejection diagnostics, while
omitting raw source payloads and credentials. Retained evidence_hash points to
the cycle's full original result. Inspect unresolved exits and the checkpoint
before any further invocation; charged/interrupted passes remain latched.

Dependencies: this wrapper tests published worker08 cycle5774e254 and its exact
stacked components plus PR135 repaire10b0a8. Independent review/integration is
required. User-approved shared60 rolling requests/hour for open positions is
owned by workers03/08 and is NOT in that initial cycle dependency. Do not claim
continuous operation: the initial callable still has lifetime18 investigation
and cycle18/10s bounds. Preserve daily10/queue3; no budget extension is performed
by this wrapper. Coordinator must wire the accepted hourly-budget implementation
before using that new monitoring allowance.

## Explicit held-position monitoring successor

After the exact repaired budget/cycle dependencies are independently accepted,
the coordinator may provision the fixed existing ledger/config/evidence binding
once, separately from every read/service invocation:

```sh
python -m desk.paper_cycle_cli --config REVIEWED_CONFIG.json provision-monitoring --research-db EXISTING_RESEARCH.sqlite --evidence-db EXISTING_EVIDENCE.sqlite --ledger-db NEW_EXPERIMENT.sqlite
```

This command holds research worker → evidence invocation → cycle ledger locks.
It refuses any existing monitoring schema; it cannot reset, recover, reprovision
or migrate an old allowance. It reads no provider credentials and makes no
provider calls. Neither init nor once invokes provisioning.

`once --monitoring` explicitly selects the existing shared60/hour allowance.
Its target file must contain positions only: candidates and USD refs must be
empty. It performs no autonomous target admission/export. The reviewed exporter
must supply all current held targets and exact residual raw quantities after
partial sells; worker01's callable remains pending for an automated driver.

Before credentials or cycle intent, a locked read-only durable budget/checkpoint
snapshot requires at least five remaining reads per saved open position: mint,
pool discovery, atomic pool, full SELL and a conservative optional partial SELL.
Insufficient allowance returns blocked diagnostics with the original mark time,
age and configured TTL comparison. It does not poll providers, refresh the mark,
reset counters, clear the interruption latch or promise continuous ten-second
coverage. Reinvoking a snapshot cannot create allowance; only real rolling
expiry restores capacity. Output keeps separate investigation/monitoring counts.

The snapshot is advisory, not a budget reservation: run_once reacquires its own
locks. A competing manually invoked cycle may consume allowance between those
steps; source reservations and the cycle's existing fail-closed latch remain
final authority. Do not run concurrent operator drivers. A stronger atomic
preflight belongs to the cycle API; this wrapper does not bypass its locks or
invent a bulk budget reservation. Network failure/interruption can still latch
recovery even when the conservative allowance is sufficient.

Test composition uses PR14587572e53 + unchanged PR142db6f785 + PR143 consumer,
and preserves PR14421d9ed5's duplicate-free immutable config snapshot. PR142's
reservation replacement repair/review is still required; this composition is not
permission to activate monitoring. No deployment units or timers are supplied in
this successor; those await the exporter contract and accepted runtime budget.

The coordinator's read-only deployment preflight reports the existing
`desk-paper-monitor.service` watchdog points to
`/var/lib/solana-desk/active-paper.sqlite` with
`/opt/solana-desk/config/paper.json` and an older release WorkingDirectory.
That original strict config must remain intact. A new quote ledger requires a
reviewed watchdog override selecting the exact matching experiment config and
accepted release, or the old watchdog must remain disabled. A mismatched
code/config fingerprint fails closed; do not reinterpret that as fresh marks.
Dashboard and existing backups use the active-paper path, so the coordinator
must also review that fixed path binding before adoption. No override or service
activation is executed or supplied by this manual-only change. Future inactive
unit artifacts must state these prerequisites; activation waits for the accepted
release and exporter/budget/cycle reviews.
