# Lossless raw-debit quantity witness

Source base: `9f6adea66ec1cede061f5318bb2abff675bd8a2c`. The quantity boundary left unresolved by PR73 is implemented as an unsigned diagnostic component, not an engine admission proof. No engine, ledger, monitor, route privilege, shared readiness or queue behavior changes.

## Exact units and source binding

`desk.quantity.quantity_witness` reads the existing strict legacy Mint decoder's original 82-byte initialized account. Token program, state/policy and original account identity are checked before taking byte 44 as the u8 decimals. Zero supply, active mint/freeze authorities, invalid initialization, unsupported program and Token-2022 remain rejected by the existing policy. The quantity witness is restricted to positive raw u64 debit amounts. Raw input and balance metadata accept bounded exact integers or canonical ASCII integer strings; booleans, floats, Decimal raw values, fractional/exponent strings, leading zeros and out-of-range values reject.

The human quantity uses `Decimal((0, digits_of_raw, -mint_decimals))`, then exact fixed-point formatting. There is no float conversion, division, scaleb, quantize or ambient-context rounding. Initialized legacy decimals are the full u8 range 0..255; imposing a made-up 18-decimal limit would discard valid byte states. Output remains bounded (at most 257 characters for the fractional boundary). Tests cover max u64 and decimals 0/6/19/255 with precision 1/9/28/100, tiny Emax/Emin and rounding/inexact/overflow/underflow traps enabled.

The requested debit must equal the raw pre/post balance difference across the exact target mint and wallet. Duplicate/out-of-range indices, missing ownership, missing endpoints on nonzero native accounts, conflicting owner/mint identity, unsupported target program, missing/conflicting/out-of-range decimals, fractional raw balances and inconsistent human strings reject. The float `uiAmount` field is deliberately ignored. Optional declared human quantity and present `uiAmountString` are checked by exact Decimal comparison, never used as the amount source. Target wallet totals cannot exceed raw mint supply. Returned Mint bytes, when present in the simulation's requested accounts, must match original source units/state/program; an absent post-state is not invented.

The output binds mint and wallet, raw u64, decimals, exact human string, original source slot, transaction hash, mint source/account/byte hashes, simulation and resolved-key hashes, and witnessed token indices. `witness_hash` covers the canonical result. The source is either an original getMultipleAccounts request/result envelope or a saved sequence preExecutionAccounts row bound to its transaction identity. Hashes bind local source content; they do not authenticate a remote bank or prove address-to-account truth independently of the original source.

## Wiring and originals

`simulate_sell` computes `quantity_witness` from its existing mint lookup, original atomic simulation, resolved keys and actual compiled unsigned transaction bytes. Capture adds the original `mint_lookup` and witness to the existing evidence record. No additional RPC request is made. `replay_sell` recomputes the component using saved originals and recompiled bytes; a substituted saved witness rejects. Legacy records lacking original mint lookup remain readable and return UNKNOWN quantity instead of acquiring invented evidence.

`simulate_roundtrip` computes `sell_quantity_witness` from the existing preflight mint lookup and the actual sell leg's raw balance metadata. The compiled sell-byte hash must match the sequential diagnostic's transaction hash; a mismatch stays UNKNOWN. Capture retains the original lookup and now also retains each already-available unsigned leg byte sequence/hash, without changing originals or requesting another transaction. Snapshot ordering is mapped from the original shared watchlist into resolved sell keys, as in the existing effects checker.

A witness rejects into `status=UNKNOWN` when required inputs conflict or are absent. It does not change the existing independent effects result into a broader policy verdict. The new component kind is `raw_debit_quantity_witness_v1`, never `sell_simulation`; even a witnessed quantity is rejected as sellability proof by the existing gate. `transaction_policy_ok`, `eligible_for_trading`, signed and submitted flags remain false in diagnostic paths. No existing public capture is edited.

## Fixture evidence and trust limits

The unchanged public roundtrip capture yields `2523452` raw units, original decimals `6`, and exact human quantity `2.523452`. Its existing sell transaction identity is retained. Original SHA256: `32a201190ee146fe11c9897397c18fdfb831af8aedeb48bc9e1201ee9070696b`. The older single-sell capture lacks a preflight lookup and transaction byte/hash identity, so it cannot acquire a complete witness merely from post-state bytes. It remains unchanged at SHA256 `1e833b316620278fada145d60f6ddb7df2d1a48a9cab1bdc9a6e113e79aa2b2d`.

Integration-positive tests use explicit synthetic transport/state, not a provider. The roundtrip transport stub is given matching synthetic sequence hashes in an isolated memory copy; the original public hash/bytes remain unchanged. An unmodified public sequence paired with incompatible stub bytes correctly returns UNKNOWN. The public quantity math test uses original saved raw effects directly; a hash supplied by an old capture is a recorded identity, not newly authenticated execution.

Remaining dependencies: trusted raw route/compiled privilege and source authentication; full authority/CPI policy; current source/freshness validation; exact raw inventory versus hypothetical paper inventory; and native fee/rent/failure cost contracts. This witness does not choose a rounded engine quantity, alter the paper quantity model, manufacture wallet holdings, prove execution success beyond local metadata, establish lifecycle provenance, or authorize a fill. The PR73 engine remains unchanged. Independent review is required before integration; fixture success is not live acceptance.

## Validation

Python 3.12 fixture-only Linux validation commands:

- `python -m unittest tests.test_quantity_witness -q`
- `python -m unittest tests.test_quantity_witness tests.test_simulate tests.test_roundtrip tests.test_cloud_sellability -q`
- `python -m unittest discover -q` on the exact branch and detached latest-integration combination.
- `git diff --check` and unchanged public fixture SHA256 checks.

Final counts, durations, exact source SHAs and combined tree identity are recorded in the PR description after these checks. Tests forbid sockets in diagnostic wiring; full-suite network permission is limited to existing local loopback fixture servers. No provider/VPS/secret/signing access occurs.
