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
