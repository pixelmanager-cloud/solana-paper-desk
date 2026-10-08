# PR53 independent exact-head provenance audit

Exact headff4aadf41b158ca841d2ae9a3d1cc1841e8772bf; combined baseline90e1ece1e744f0317446a095741cc6ff4a8b9fe3. Worker06 originaltask01a118fc-f867-7233-8eec-c20659e70095, branchcodex/cloud-wave1-06-raw-fixture-provenance; independent cloudLinux Python3.12.14, shared GitHub identity COMMENT only, cloud-independent-review-v1.

No scoped blocking defect found. Entire two-file diff is tests/report only; existing production and fixtures unchanged by this PR. Independently recomputed canonical raw wrapper SHA2562b49cd4c6fe24a121cb59894c8a6fe0e82e4cf51db28e88151612984ccc9a0c7, literal raw SHA25627d481575656d2b254a63bf699a32ce3dd599ce3b55fc8b04da2aa79dd356520 and literal notification SHA256d80cf9876fdb3e9b465e4103fa14bf2baaf09483dad9a27a57ff7339624b3bf2. Saved provider call/finalized request declarations were read as data, not independently authenticated.

Independent compiled_keys/compiled_instruction reconstruction confirms18 static+5 loaded writable+11 loaded readonly=34 keys, ordered identity/header-requested signer/writable flags match notification. All33 paths (5outer, inner15/4/9) match resolved program/stack-height declarations.26 rows are parsed in notification,7 preserve common raw bytes/accounts; report explicitly does not claim semantic effects equivalent for parsed-only rows.40 independent per-path mutations (33program redirects+7common raw-byte changes) rejected. Existing tests additionally cover slot/signature/key order/missing row and declared representation differences, rewards [] versus null, absence of raw header/loaded segments in notification. Full digest pins all raw compiled data, not its authenticity.

Report accurately limits provenance: same provider/signature/slot corroborates saved representations, not independent signature verification/finality/lookup-table historical state/program binaries/directCPI privileges/caughtCPI outcomes/effect order/control histories/endpoints/ownership/entry. No acceptance flags, runtime gate changes or lifecycle inference. Worker07 syntax work remains separately active; not reviewed or completed here.

Actually run:
- python -m unittest discover -s tests -v:1269 tests in65.323s, OK, zero failures/Linux skips.
- python -m unittest tests.test_cloud_raw_fixture_provenance -q:5 tests in0.004s, OK.
- python /workspace/work/review/p53-independent.py:independent digest/34key/header/all33path reconstruction and40 negative probes PASS.
- Exact head GitHub Tests run37733681037 completed success.
No resulting scratch-combination GitHub CI/merge commit claimed. Zero provider/VPS/secrets calls; network permissions solely unrelated existing loopback fixtures.

Source identity: prior verifieddf8ac82+repairedPR52 source copied isolated, all8 integrationdf8ac82->90e1ece changed blobs and2 PR53 additions fetched at exact commits, every Git blob SHA1 verified. Complete270-source-file SHA1/SHA256 manifest cloud-pr53-90e1ece-ff4aadf-hashes.json; compact manifest SHA2561e67806b8a910d47d312cfffda3623e776867e8c1b8c185b023655a1f55a707e. Baseline includes acceptedCLI/raw public reference; these were not newly acquired by cloud. No source conflict resolution or production/sharedqueue edits.

Scoped fixture correspondence only; desktop retains integration/exact resulting-commit CI and live acceptance. Paper-only/shared18/private-loopback gates retained. No merge/dispatch/issue closure/providers/VPS/live readiness or dependent completion.
