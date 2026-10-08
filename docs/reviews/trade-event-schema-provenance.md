# TradeEvent 4.8: authoritative trailing-field provenance

The eight opaque bytes have an authoritative exact schema match: official
`pump-fun/pump-public-docs` revision `8cda1fa30ea658b20909d8aedf002047119388d2` adds
`creator_fee_unclaimed: u64` after `holder_rewards` in `TradeEvent`.
This is source-derived field naming, not a meaning inferred from byte counts.
The complete 35-field layout consumes all 390 original instruction bytes
(16-byte CPI/event headers plus 374-byte Borsh payload), without a remainder.
No fixture, production decoder, schema manifest, or evidence gate was changed.

## Immutable primary source

[Official IDL](https://github.com/pump-fun/pump-public-docs/blob/8cda1fa30ea658b20909d8aedf002047119388d2/idl/pump.json)

Full IDL bytes SHA256: `38b8abcc5b279bda85cf473e7c6f67bd15eb89df658cf93434687a43c88ad937`.
Commit timestamp: `2026-10-08T00:11:31+04:00`.
The predecessor IDL revision `e0687ae9b7e064a0f54efc7297c65eecfbba3a8f`
has SHA256 `ffe966c42f1af41652ee753fe2f1e3f7cd4077d7e6f49faf3138959c8b56064b`,
identical to the production IDL pinned at `cb188ce08b5069196eef1f3e4a0c43b70099793b`.
Its 34 TradeEvent fields are identical to the new layout's first 34 fields;
its referenced Shareholder definition and TradeEvent discriminator also match.
The official diff explicitly appends the named u64 field. Seventeen official
`idl/pump.json` path revisions were inspected offline; only this latest revision
consumes the whole event. Relevant preceding shapes: 34 fields leave 8 bytes;
32 fields (revision `3c6721a67c0b206b39130b454c8ba22a83ce972e`) leave 24;
25 fields (`82dacacf15ca93dc0444ab38714f2226210a0a3d`) leave 100.
No older legacy CreateEvent investigation was repeated.

Reproduce source verification with `git show 8cda1fa30ea658b20909d8aedf002047119388d2:idl/pump.json`
in an official repository checkout, then SHA256 the literal output bytes.
The dedicated test embeds only the exact TradeEvent and referenced Shareholder
source excerpts; it performs no network requests or production registration.

## Fit against unchanged captured bytes

Fixture: `fixtures/mainnet-launch-raw.json`; literal SHA256
`27d481575656d2b254a63bf699a32ce3dd599ce3b55fc8b04da2aa79dd356520`.
Instruction locator: outer instruction 4, inner instruction index 8 (zero-based).
Original decoded instruction SHA256:
`2b0a30b072ba6ea8d24e7ef60ab16affb307f37ffe16d825ec0a7c95784d2ca8`.
CPI discriminator `[0:8]`: `e445a52e51cb9a1d`;
TradeEvent discriminator `[8:16]`: `bddb7fd34ee661ee`.
Offsets below are zero-based, end-exclusive, relative to the whole instruction.
String/vector lengths are included; these offsets describe this fixture,
not fixed offsets for events with other strings or nonempty shareholders.

| Field | Official Borsh type | Bytes | Captured decoded value |
| --- | --- | --- | --- |
| `mint` | `"pubkey"` | 16:48 | `AoPfwh6vExgSrfzX2ALPBEpWS2wSjhcG2ZxKdN1Vpump` |
| `sol_amount` | `"u64"` | 48:56 | `493827159` |
| `token_amount` | `"u64"` | 56:64 | `17376518166910` |
| `is_buy` | `"bool"` | 64:65 | `True` |
| `user` | `"pubkey"` | 65:97 | `BVVwAst9gf32KFxNueebENzLwEnu2fiDeuVg1cT5nrbs` |
| `timestamp` | `"i64"` | 97:105 | `1791402595` |
| `virtual_sol_reserves` | `"u64"` | 105:113 | `30493827159` |
| `virtual_token_reserves` | `"u64"` | 113:121 | `1055623481833090` |
| `real_sol_reserves` | `"u64"` | 121:129 | `493827159` |
| `real_token_reserves` | `"u64"` | 129:137 | `775723481833090` |
| `fee_recipient` | `"pubkey"` | 137:169 | `62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV` |
| `fee_basis_points` | `"u64"` | 169:177 | `95` |
| `fee` | `"u64"` | 177:185 | `4691359` |
| `creator` | `"pubkey"` | 185:217 | `BVVwAst9gf32KFxNueebENzLwEnu2fiDeuVg1cT5nrbs` |
| `creator_fee_basis_points` | `"u64"` | 217:225 | `30` |
| `creator_fee` | `"u64"` | 225:233 | `1481482` |
| `track_volume` | `"bool"` | 233:234 | `False` |
| `total_unclaimed_tokens` | `"u64"` | 234:242 | `0` |
| `total_claimed_tokens` | `"u64"` | 242:250 | `0` |
| `current_sol_volume` | `"u64"` | 250:258 | `0` |
| `last_update_timestamp` | `"i64"` | 258:266 | `0` |
| `ix_name` | `"string"` | 266:273 | `buy` |
| `mayhem_mode` | `"bool"` | 273:274 | `False` |
| `cashback_fee_basis_points` | `"u64"` | 274:282 | `0` |
| `cashback` | `"u64"` | 282:290 | `0` |
| `buyback_fee_basis_points` | `"u64"` | 290:298 | `5000` |
| `buyback_fee` | `"u64"` | 298:306 | `2345679` |
| `shareholders` | `{"vec":{"defined":{"name":"Shareholder"}}}` | 306:310 | `[]` |
| `quote_mint` | `"pubkey"` | 310:342 | `11111111111111111111111111111111` |
| `quote_amount` | `"u64"` | 342:350 | `493827159` |
| `virtual_quote_reserves` | `"u64"` | 350:358 | `30493827159` |
| `real_quote_reserves` | `"u64"` | 358:366 | `493827159` |
| `holder_rewards_bps` | `"u64"` | 366:374 | `0` |
| `holder_rewards` | `"u64"` | 374:382 | `0` |
| `creator_fee_unclaimed` | `"u64"` | 382:390 | `0` |

## Limits and validation

The source establishes the field's schema name and type, not independent runtime
accounting semantics, program deployment identity, signature verification,
finality, complete ownership history, request binding, or launch eligibility.
Existing provenance records a finalized request, not independent finality proof.
Production continues to return `EVENT_PREFIX_DECODED`, `schema_complete=False`,
and `unknown_trailing_bytes=8`; updating trusted schemas remains coordinator work.

Tests assert unchanged artifact/event hashes, equality with the pinned prefix,
exact EOF, appended-field offset/value, unchanged production partial rejection,
every truncation, extra suffix bytes, both wrong discriminators, invalid Borsh
boolean, and excessive vector count. Full suite results are recorded in the PR.
