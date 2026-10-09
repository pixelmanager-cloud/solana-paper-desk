# Same-experiment runtime upgrade after monitoring handoff

Base: e4badabe0ce850ceec59e4ae25aa1e7e0c266efb, desk source
0fe08c215d591fb972b59749329abcea5c30fbbf5483ea313695ffd175aa433e.
The coordinator reports the active token2022-paper ledger is native to that source
with no runtime receipt, unchanged config30406d9cb727b3e518687dbb4c20820483bf51384f0edfb14a94f8221e2b595e,
and monitoring bindingfb44fb5f974bf649f029f50fd96a340fe668c0848fb4a67316f60b3d68db3aac.
Those are supplied deployment facts, not cloud inspection or provider evidence.

Previously validate_active required running source == immutable handoff origin;
_ledger also required original ledger metadata == running source. Both rejected
an otherwise valid existing runtime receipt for a reviewed successor.

The only production edit is monitoring_handoff.py (8 added / 4 removed lines).
The binding's new_source remains the original experiment pin. The active reader
passes its actually running source to the existing require_runtime resolver, while
_ledger separately requires metadata.implementation_hash to equal that immutable
origin. Native same-source behavior remains; a different source requires the
existing exact one-hop reviewed receipt with unchanged config and canonical
ledger/research/evidence context. The helper still checks actual implementation
hash, full history/checkpoint, old retired ledger, initial-state identity, grants,
original monitoring prefixes, format3, pacing and all prior handoff constraints.
No source flag or caller-provided boolean replaces the receipt. This is local
reviewed policy/record integrity, not cryptographic provider/deployment authentication.

Coordinator activation after review/source freeze: retain the original legacy
runtime edge and handoff policy/binding, add the exact0fe08c -> FINAL_SUCCESSOR
edge for SAME config and token2022 ledger context to the external runtime policy,
then invoke the existing explicit runtime_compatibility.transition under its
canonical locks. It appends the first immutable runtime receipt only to the active
ledger; metadata/config/checkpoint/events/outcomes remain original. Do not call
handoff.activate, initialize another experiment, change a grant, replace a source
hash, provision allowances, restore counters or reset pacing. Monitoring receipts
continue to carry the SAME binding hash; effective runtime identity is established
by the ledger receipt. Further successors/chains remain unsupported by the
existing resolver and are outside this task.

Adversarial fixture tests compile a real synthetic predecessor: a temporary copy
of the current desk plus one inert comment-only marker. Hashes are recomputed and
that predecessor initializes/activates its own synthetic experiment via actual
APIs. The current code then uses an actual reviewed runtime transition, with no
mocked implementation hash. This is synthetic local contract evidence, not a
claim of replaying the production0fe database or binary. Existing pinned150/8098
fixtures are reused unchanged. Tests cover prior charged monitoring usage,
unchanged old ledger/research/evidence/pacing and binding, normal successor
buy/read/restart, missing/unreviewed/revoked/wrong-context receipts, rehashed
receipt source/config/prefix tampering, source/config metadata tampering, a
native-current identity rewrite without origin proof, and genuine compiled
predecessor refusal after successor publication. All failed readers are nonmutating.

Python3.12.14 Linux: first focused run40 tests / 29.993s / OK / zero skips;
final dedicated7 tests / 8.964s / OK / zero skips. Full unittest discovery2525
tests / 399.592s / OK / zero skips. Tested head
0dd147dac66e4e39e07970670ea13254543c53d7, tree
d5245ed5f9e44c1bee2e05d3bf9b0e88c8c13f6d; desk hash
99ba6f06c92b19438f4d1535b6707630414fcdf4c3f8ba2e6bc70f890d651ef2
is this component, not the final combined deployment pin. Source manifest SHA256
b749b619b66c6ca912ebe9a5126936f8f3673c0fc8b79f8caddaefb11b7bfabd;
full log SHA256 c1151a66bd0217a0b13b024184debca85c7169a3ff6f39bc415a29ee5cf133ea. Artifacts remain under
/workspace/work/agent07-active-runtime-upgrade/{source-manifest.txt,manifest.json,focused.log,dedicated.log,full-linux.log}.
This validation update changes only the report; production code/tests remain
byte-identical to the tested head. No
transport, runtime policy/deployment pin, shared readiness/queue or production
changes. Worker08 owns the separate HTTPchunked fix; final combined successor
hash and external reviewed policy edge belong to the coordinator. Independent09
review and final combined validation remain required; no paper activation claim.
