# One reviewed preparation oversize retirement

Proposal only; independent review and exact production pins are required. This
repairs one observed history-first failure, not screening or history completeness.
The current production instance is scan `86b0c16242934b768a0ddc0847d9b09d`,
pass `02b82210a5694a629a99ae54d20466bb`, failed attempt
`f54506e614f264c6c483b2d054848f9d31c4c3fb1d6974a490da3e88f4227bfc`,
and original intent `3ba01098f17e01e61174af87fb2cf49b67af52cc48de6108ff93310dea6616fd`.
No production pin is included or authorized here.

The original intent has no ledger/source anchors. The receipt therefore labels
its association `EXPLICIT_REVIEWED_PREPARATION_OVERSIZE`, never intrinsic proof.
An independent coordinator review must establish the canonical pre-attempt ledger
backup's provenance, exact source/config, invocation ordering and complete evidence
inventory. Manager records can corroborate the failed invocation but do not turn
missing response bytes or timestamps into observed facts.

## Exact scope and original evidence

Only a SEALED admission moving from 6 to 7 requests under the original 18 ceiling
is supported. The target must have public-capture provenance, no known hazards,
and its exact original captured history time. The failed query is the first page
of the exact 301-second pool window: finalized, ascending, full `jsonParsed`,
version ceiling 1, limit **100**, status any, tokenAccounts none, no cursor or slot
filter. Its existing reservation must be `RETRYABLE_ERROR`, attempts 1, coverage
NULL. This deliberately does not accept failures from a new smaller-page policy.

The exact original charged attempt must have HTTP200, `RESPONSE_OVERSIZED`, NULL
response bytes and NULL observed time, with the original canonical request bytes.
All retained `paper_read_attempt_v1` pages for that scan are inventoried in charge
order; duplicate/missing/new/contradictory attempts refuse. A completed preparation
outcome for the same intent is a contradiction. All original history rows and
rowids for that scan are pinned. No original pass, query, page, admission, budget,
ledger or monitoring row is updated. The only write is a new immutable receipt
family in evidence, with unique pass/scan, exact guards and autoindexes.

A read-only comparison to the distinct canonical pre-attempt ledger backup binds
all native ledger rows, rowids, storage types, bytes and schema, in addition to
accepted INIT-only checkpoint/history validation. The backup byte hash is pinned.
At apply the full ledger fingerprint must still match. Subsequent gate reads pin
the immutable metadata/INIT prefix, permit legitimate new events/checkpoints under
accepted runtime validation, and keep the rejected scan permanently excluded.
Unrelated NULL passes continue to block. No retry, refund, reset, recovery rebuild,
entry approval or complete-history assertion is introduced.

## Staged coordinator order

Keep writers stopped and canonical research/evidence/ledger paths unchanged.
Execute the final reviewed code as staged tooling, without asserting it is already
the active ledger runtime. Planning and apply validate the old ledger explicitly
under the independently established producer source (`77de75…` for this instance).
The proposal separately records the staged tool's final implementation hash.

Read-only default/explicit `--plan` command:

```
python -m desk.paper_preparation_retirement \
  --research-db /var/lib/solana-desk/research.sqlite \
  --evidence-db /var/lib/solana-desk/evidence.sqlite \
  --ledger-db /var/lib/solana-desk/paper-kraken-77de75a2.sqlite \
  --config EXACT_REVIEWED_KRAKEN_CONFIG \
  --pass-id 02b82210a5694a629a99ae54d20466bb \
  --failed-attempt-hash f54506e614f264c6c483b2d054848f9d31c4c3fb1d6974a490da3e88f4227bfc \
  --pacing-db /var/lib/solana-desk/provider-pacing.sqlite \
  --producer-source-hash 77de75a248fd087e62bcc2bac4a581c667905dfcb709e28ed8e80a58afcd4d54 \
  --ledger-backup /var/backups/solana-desk/post-kraken-77de75a2/paper-kraken-77de75a2.sqlite \
  --plan
```

The API acquires research worker, evidence invocation, then ledger cycle locks.
Independently review the exact proposal, original inventory and pre-attempt backup;
install that exact entry in `config/paper-preparation-retirement.json`. Its empty
checked-in policy authorizes nothing. Repeat the same invocation with `--apply`
for append-only retirement. Revalidation under an evidence write transaction must
produce precisely the same reviewed pin or no receipt is committed.

Then use the existing independently reviewed first runtime transition from the
original native source to the exact final combined source. No terminal gate waiver
is used by this transition API. Before that transition, the new gate must refuse
an old native runtime rather than pretending compatibility. After transition,
validate accepted checkpoint/monitoring/global gates and require the rejected scan
to return `REJECTED_SCAN_RETIRED`. Preserve all original tables and the original NULL
row/7 charges, perform quiesced post-backups, and apply the separately reviewed
restart procedure. Final integration/source policy and deployment are coordinator
owned; worker03's history request-size change remains a separate dependency.

## Bounds and limitations

Because the existing compressed evidence schema has no kind/scan index, attempt
inventory uses a whole-store scalar preflight followed by bounded streaming page
loads: at most 4096 pages, 256MiB total declared raw bytes and compressed bytes.
Each page is independently hash/decompression checked by the accepted bounded
reader. The coordinator must verify these bounds against the actual retained store.
Inventory cost is not exempt from runtime deadlines; live timing is not established
by synthetic tests. Ledger backup is capped at 32MiB; native ledger row inspection
at 10000 rows/table, 32MiB aggregate and 16MiB/row. Protected path stability and
quiescence remain coordinator prerequisites. No provider/VPS access was performed.
