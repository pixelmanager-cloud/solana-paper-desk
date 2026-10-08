# Saved raw launch provenance and inventory

Base integration: `d41fe2933236f972e367dfab8b6101e142511901`. This is a bounded offline correspondence audit, not an instruction syntax or lifecycle implementation. Original fixtures are unchanged; no provider requests were made.

The SHA256 of UTF-8 `json.dumps(record, sort_keys=True, separators=(',', ':'))` for the entire saved raw wrapper is **`2b49cd4c6fe24a121cb59894c8a6fe0e82e4cf51db28e88151612984ccc9a0c7`**, matching provenance. The formatted file's literal-byte SHA256 is `27d481575656d2b254a63bf699a32ce3dd599ce3b55fc8b04da2aa79dd356520`; these are different serialization identities. Original notification byte SHA256 remains `d80cf9876fdb3e9b465e4103fa14bf2baaf09483dad9a27a57ff7339624b3bf2`.

First signature, all declared signatures, slot454321337, transaction index, recent blockhash and create-account mint agree with the notification. Mint is `AoPfwh6vExgSrfzX2ALPBEpWS2wSjhcG2ZxKdN1Vpump`; signature is `4PJardjqDqxr9Sek37eBGvcQahrkCT2GZ1p8aMxB5BLBHk4DRGkQNwobKxmbD7G4Vergu6eu9fSmbBZPoi1KK1NK`.

## Correspondence and representation boundaries

Raw static keys18 + loaded writable5 + loaded readonly11 exactly reproduce the notification's expanded34 keys in that order. Lookup-table identity and writable/readonly lookup indices agree. Raw header declares two writable signers, fourteen writable static keys and four readonly unsigned static keys; notification key signer/writable/source flags agree. This does not independently authenticate lookup-table contents at the execution slot or signer privileges.

Both inventories contain five outer instructions and inner groups2/3/4 with15/4/9 rows, totaling33. There are no missing or extra paths, resolved programs or stack-height declarations. Every compiled account index is in range. The seven notification rows with raw data preserve exact base58 bytes and ordered account addresses: outer0/1/2/4 and inner2.14/4.1/4.8. The other26 notification rows are parsed representations, while the new raw fixture preserves compiled bytes and ordered indices for all33. We do not invent missing bytes from parsed fields or assert parsed semantics equal raw instruction effects; Worker07 owns syntax comparisons.

Notification parsed types include System createAccount/transfer, ATA create/createIdempotent and Token-2022 initialization, metadata, mintTo, authority revocation and transferChecked. Thirteen Token-2022 notification rows remain parsed-only in the original; the raw fixture supplements their representation rather than rewriting the notification. Raw endpoints/intermediate account state are absent from both records.

Raw message header and explicit loadedAddresses are absent from the notification representation; expanded key flags are present there. Shared metadata differs in innerInstructions representation and rewards (`[]` raw versus `null` notification); other shared metadata values agree. Raw record has blockTime and top-level slot, whereas the notification carries slot outside its nested transaction object. Neither representation should be silently promoted to authenticated evidence.

## What provenance cannot prove

The saved wrapper/provenance declares getTransaction, json encoding, finalized requested commitment, version ceiling0, one original provider call and zero replay calls. Canonical hashing proves consistency with the saved digest, not who produced the response, cryptographic signatures, independently verified finality, fork/bank identity, historical program binaries, lookup-table history or completeness of acquisition. Two records from the same provider are corroborating representations, not independent trust anchors.

Declared stackHeight and order do not prove actual direct CPI privileges, signer/PDA authorization, individual caught CPI outcomes or effect order. Transaction success, logs, balances and parsed controls do not prove fresh prestate, all extension/control histories, absent transient authorities, optional wrapper EOF semantics, opaque mayhem/native/quote effects, same-bank endpoints, aggregate holders or sellability. No lifecycle, ownership, paper readiness or entry approval follows. Existing budgets, gates and runtime exclusion remain unchanged.

## Regression validation

Five fixture-only unittest methods pin canonical identity, signature/slot/mint, key segments/header declarations, complete path/program inventory, preserved common raw bytes, explicit representation differences and saved request scope. Adversarial copies change slot/signature/key order/remove a row/redirect a program/change bytes; correspondence rejects them. No production modules or fixtures are modified.

Python3.12.14 validation: dedicated5 tests in0.005s, OK; full Linux unittest discovery1,269 tests in61.029s, OK, no failures/errors/skips. Full discovery included the same five methods; final dedicated rerun also checked both literal-file hash assertions. `git diff --check` clean. No unresolved test failures. Independent review and Worker07 syntax disposition remain separate dependencies.
