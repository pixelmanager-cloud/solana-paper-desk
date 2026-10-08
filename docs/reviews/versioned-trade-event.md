# Versioned official TradeEvent syntax, unresolved economics

The literal official `idl/pump.json` from commit
`8cda1fa30ea658b20909d8aedf002047119388d2` is retained unchanged as
`desk/schemas/pump_events_8cda1fa.json`, with SHA256
`38b8abcc5b279bda85cf473e7c6f67bd15eb89df658cf93434687a43c88ad937`.
Immutable source:
https://github.com/pump-fun/pump-public-docs/blob/8cda1fa30ea658b20909d8aedf002047119388d2/idl/pump.json
The earlier provenance report gives the complete captured field offsets.

Manifest registration is explicitly `events_only: true`. Existing instruction
schemas and historical schema files are unchanged. The existing loader already
verifies all manifest digests and excludes events-only files from instruction
registration, so `desk/programs.py` needs no change. Tests compare the complete
registered Pump instruction map to the original pinned map, and exercise every
new-only instruction discriminator to confirm UNKNOWN_DISCRIMINATOR.

The captured event at path 4.8 now returns EVENT_DECODED, schema_complete true,
using the versioned file. Its last field is creator_fee_unclaimed:u64, value zero.
Historical 34- and 32-field exact events still decode through their earlier files.
Appending unknown bytes remains EVENT_PREFIX_DECODED/schema_complete false; no
suffix is ignored. Existing research malformed/truncation tests remain intact.
A corruption probe verifies digest failure before instruction/event loading.

Disconnected lifecycle research now binds either a complete or partial TradeEvent
to its direct buy parent, event authority, mint, user, buy flag and token amount.
Duplicate and redirected events still fail closed. Complete syntax is recorded as
`complete_trade_event_syntax_economic_effects_unresolved`, with structural role
incomplete and TRADE_EVENT_ECONOMIC_EFFECTS_UNVERIFIED. It cannot establish an
inventory agreement, economic effect, authenticated lifecycle or ownership.
Changing the declared appended fee to the maximum u64 still grants no approval.
Prefix witnesses continue to retain opaque original suffix bytes.

The unchanged public raw launch retains all 33 instruction paths, eight incomplete
buy-side rows and eight unresolved economic witnesses, zero ignored rows, no
invented endpoint states, and false agreement/trust/lifecycle/ownership/eligibility
flags. Tests assert these properties. No acquisition, RPC, VPS, signing, secrets,
entry behavior, worker07 native reconciliation or shared readiness/queue changed.

Validation: Python 3.12.14, targeted suites 31 tests in 0.512s, OK. Full Linux
unittest discovery: 1300 tests in 99.038s, OK (no failures or skips). An initial broader focused run encountered
the sandbox restriction on the existing loopback HTTP test; the full run uses
approved loopback access. No provider access is needed.

Remaining dependencies: native movement/state reconciliation, endpoint provenance,
individual CPI/signature/finality authentication, ownership continuity and economic
accounting still require their independently reviewed evidence. Syntactic source
compatibility does not prove deployment identity or authorize a launch/entry.
