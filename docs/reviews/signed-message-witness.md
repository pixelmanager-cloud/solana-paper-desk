# Offline signed-message witness

Standalone `research/signed_message_witness.py` reconstructs canonical compiled legacy/v0 message bytes using existing solders 0.29.0 and verifies every declared Ed25519 signature against the corresponding required static signer key. It never creates a signer, signs, requests data, broadcasts, or feeds a production consumer. A verified signature establishes only that the declared public key signed those message bytes. It does not establish transaction execution, signer authority for a program operation, recipient permission, ownership, finality, or entry eligibility.

## Pinned primary format and verification sources

Solana source commit [d9f20e951a06b61e4505da0955228020b96a8915](https://github.com/solana-labs/solana/tree/d9f20e951a06b61e4505da0955228020b96a8915):

| Source | Blob SHA | Used requirement |
|---|---|---|
| [legacy.rs](https://github.com/solana-labs/solana/blob/d9f20e951a06b61e4505da0955228020b96a8915/sdk/program/src/message/legacy.rs) | `1a6a9239f4e0aaff47f02356ead414f5abc05414` | Header, static keys, blockhash, compiled instruction serialization and sanitization |
| [v0/mod.rs](https://github.com/solana-labs/solana/blob/d9f20e951a06b61e4505da0955228020b96a8915/sdk/program/src/message/versions/v0/mod.rs) | `df001bb19ce0bcb8b3d4d60a070b1cea64346020` | Lookup descriptor serialization; program IDs must be static; signed keys must be static |
| [versions/mod.rs](https://github.com/solana-labs/solana/blob/d9f20e951a06b61e4505da0955228020b96a8915/sdk/program/src/message/versions/mod.rs) | `301490a2aa7e7d2a8ecd00c5e34d378de26b74d7` | v0 prefix `0x80`, legacy without a version prefix |
| [signature.rs](https://github.com/solana-labs/solana/blob/d9f20e951a06b61e4505da0955228020b96a8915/sdk/src/signature.rs) | `e3cc900e49efc1e47505bf698b4b7dcbf8ab7931` | Ed25519 strict verification over serialized message bytes |

These pin the wire format and verification algorithm, not the historical deployment/runtime at the sample slot.

## Preserved public evidence

No fixture bytes are changed. Inputs can be raw RPC results, saved `rpc_response_v1/getTransaction` envelopes, or their JSON bytes. Every accepted input retains SHA256 of its full canonical JSON (sorted, compact, UTF-8, finite numbers); byte inputs additionally retain the SHA256 of the exact original file bytes. Neither hash authenticates the record's provenance.

| Existing fixture | Canonical complete-record SHA256 | Message SHA256 | Message / transaction wire bytes | Result |
|---|---|---|---|---|
| `fixtures/mainnet-launch-raw.json` | `2b49cd4c6fe24a121cb59894c8a6fe0e82e4cf51db28e88151612984ccc9a0c7` | `e6643b57707da44b5e9ba56ea16025d26ffc0bf11b05deccd3223fd25aad4443` | 931 / 1060 | Both declared signatures verify |
| `fixtures/legacy_pump_reference/create_buy.json` | `89dcc14ecad1432598e4937da2dfa9955af8637a5ae373418e9631c174991749` | `05b53d27a69f58d54620c13b68cd8b9a96d1160cd39a31bc56872b2b2b945560` | 816 / 945 | Both declared signatures verify |

The mainnet raw file's exact physical SHA256 is `27d481575656d2b254a63bf699a32ce3dd599ce3b55fc8b04da2aa79dd356520`; the historical reference's physical hash equals its canonical hash. Both transactions use **v0 wire messages**; the legacy Pump reference is not a positive legacy-wire signature fixture. No committed positive legacy-wire signature sample was found. Legacy serialization is independently checked against a manual short-vector/wire encoder and solders round-trip; its synthetic message reuses an existing public signature and correctly fails verification. No synthetic signature or private key is created. A positive public legacy-wire fixture remains a separate coverage dependency.

## Strict boundary and unresolved evidence

Exact signed header fields, explicit version, canonical 32-byte keys/blockhash, unique static keys, exact signature count/64-byte signatures, raw compiled instruction shapes and integer indices are required. Missing fields, parsed expansions, duplicate signatures/lookup descriptors/indices, malformed encoding, unsupported versions, and out-of-range indices fail closed. Account references may repeat within an instruction; lookup indices may repeat across different tables. Program IDs cannot come from loaded lookup slots or use the fee payer. The verifier preserves lookup table identifiers and indices in signed bytes, without consulting `meta.loadedAddresses`.

Bounds are deliberate research-profile restrictions: 1 MiB source, depth 32, 32768 JSON nodes, 20000-character strings, 64 total static/declared lookup slots, 64 outer instructions, 64 account references per instruction, and 1232 transaction wire bytes. Some otherwise valid larger runtime key/instruction profiles are rejected. Signed integers require exact integers, never bool/float coercion; finite display floats are permitted only in the hashed JSON source. Duplicate JSON object fields, nonfinite numbers, invalid UTF-8, cycles and non-JSON objects reject.

Successful verification leaves `metadata_authenticated`, `finality_verified`, `alt_resolved_values_authenticated`, `execution_success_verified`, `signer_authorization_verified`, `authenticated_lifecycle_accepted`, `lifecycle_verified`, `ownership_approved`, and `eligible_for_trading` permanently false. RPC request parameters, commitment labels, slot/time/index, outer `stackHeight`, all metadata, loaded addresses, balances, logs, CPI outcomes and token states remain unauthenticated. An attacker can replace those fields while retaining valid signatures. ALT contents require independently trusted bank/lookup evidence; signatures authenticate table IDs and index sequences only. Blockchain inclusion/finality and program-specific effect/authority witnesses remain separate dependencies. The module neither verifies these nor supplies lifecycle acceptance.

## Validation

Dedicated tests exercise unchanged public references, manual legacy encoding, each signature independently, signer ordering, signed instruction/header/key/blockhash/lookup mutations, metadata substitution/removal, malformed/duplicate JSON, strict lengths/types/indices/version, packet/source bounds, and absence of signing/network/production imports. Python 3.12.14 Linux: dedicated suite 22 tests in 0.381s, full suite 1358 tests in 54.057s; both OK, zero skips. `git diff --check` passes. Zero provider requests; no readiness or runtime gate changes.
