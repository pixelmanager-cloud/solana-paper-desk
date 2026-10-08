# Disconnected runtime log witness join

Base: integration/cloud-wave1 c578dbead9931762a35a9a51b45b6aad62e1597c.
New files only: research/runtime_log_witness.py, dedicated tests and this report.
PR58 head 38f456e940c071a0d50d5750021ef900ad79c67d was read as unapproved
research/design input, not an accepted evidence contract or deployment proof.
No runtime or lifecycle consumer imports the new module.

`report_runtime_log_witness(record)` consumes the original raw RPC transaction
object and reuses reconstruct_execution_trace. It joins sequential invoke frames
to exact program, stack depth, invocation order and direct parent, then pairs
success/failed records with the active stack top. No search ahead or heuristic
resynchronization occurs. Repeat invocations of the same program have separate
ordinal/path/index witnesses. Every original record, trace row and log entry is
retained, including uninspected entries after a stop or budget failure. Input is
not mutated. SHA256 identity comes from the existing trace's canonical record.

Parsing examines whole log entries: Program log:/Program data: text is never
split into apparent runtime entries, even when it contains embedded newlines.
Consumed-compute and Program return entries are classified as auxiliary only;
return bytes/fee semantics are not decoded or authenticated here. Unknown log
formats make the join unknown rather than silently admitting runtime extensions.
Limits are 2048 entries, 262144 UTF-8 payload bytes, depth16, plus the existing
trace's64-key/256-instruction profile. These bound inspection, not retention of
caller-supplied originals. No request budget is consumed or enlarged.

Missing/null/malformed/empty logs, encoding/budget failures, truncation markers,
unclosed frames, extra/missing/duplicated/reordered invocations, program/depth/
parent mismatches, incomplete raw trace and failed/missing transaction status
keep the join unknown. Failed/caught branches are explicitly recorded, ancestors
retain failed-descendant markers, and the entire join stays unknown even if a
caller logs success afterward. Candidate pairs remain diagnostic witnesses;
none becomes log_match_complete after any global ambiguity or failure.

A complete result is labeled correspondence_only, not execution verified.
`success_log` and `failed_log` describe supplied program-return records only;
all final_syscall_outcome values remain unknown. Input authentication, finality,
final syscall/CPI success, authority, lifecycle, ownership and entry approvals
remain false in every result. There is no signer privilege or effect-order proof.

## Immutable primary source check

Official solana-labs/solana revision d9f20e951a06b61e4505da0955228020b96a8915:

- [stable_log.rs](https://github.com/solana-labs/solana/blob/d9f20e951a06b61e4505da0955228020b96a8915/program-runtime/src/stable_log.rs), Git blob748c4d7639214a510db193a5254342c2a47d24cb: runtime prefixes.
- [invoke_context.rs](https://github.com/solana-labs/solana/blob/d9f20e951a06b61e4505da0955228020b96a8915/program-runtime/src/invoke_context.rs#L434), Git blob8259c2ed2bcc7ac4b315d1e76feebf53224f5641: success logged at516, then process_instruction combines the executable result with pop at450.
- [cpi.rs](https://github.com/solana-labs/solana/blob/d9f20e951a06b61e4505da0955228020b96a8915/programs/bpf_loader/src/syscalls/cpi.rs#L1107), Git blob13f9cbaf905275cbc07ac96642b2c7667911851f: caller synchronization after process_instruction can still fail.

These three public immutable files were fetched for research and their Git blob
hashes independently matched. Pins describe the source model, not which runtime
revision/features or program executables ran at the saved slot. They establish
why matching program-success logs alone cannot establish final syscall success.

## Saved fixture and validation

The unchanged fixtures/mainnet-launch-raw.json joins all33 raw paths to33 invoke
and33 success frames across111 log entries. This matches the proposed PR58
correspondence finding, without adopting its further semantic/acquisition plan.
No endpoint bytes, privilege arrays, signatures or final CPI status are invented.
Original public fixture/provenance files remain unchanged.

Eleven meaningful targeted tests cover the public replay, nested/repeated program
joins, retained input/source rows, missing/malformed logs, omitted/duplicate/
reordered frames, wrong programs/returns/depths, missing metadata stack heights,
all truncation boundaries, caught failures, post-success transaction failure,
multiline forged user text, unknown runtime extensions and entry/byte budgets.
An AST check confirms no production import. Python3.12.14 dedicated results:
11 tests in0.328s, OK. Full Linux unittest discovery:
1347 tests in80.818s, OK, zero skips/failures/errors.

Remaining dependencies: trusted metadata/finality and deployed runtime interval,
signed-message/loaded-key provenance, full CPI privileges, proof of post-log
completion/error propagation, executable semantics and intermediate effects.
Log matching discharges none of those obligations. Missing logs for precompiles
or runtime variants remain unknown; no inferred success/implicit frame is added.
No lifecycle integration, shared readiness/queue, VPS/provider/secrets/signing,
broadcasting, live funds, paid services, deployment or merging performed.
