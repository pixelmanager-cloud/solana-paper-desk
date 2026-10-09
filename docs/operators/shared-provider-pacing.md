# Explicit shared provider pacing

This prerequisite does not raise investigation, queue or monitoring limits. The
coordinator and independent reviewer must approve those separate policies. No
provider entitlement, throughput, source authentication or entry approval follows.

Provision once, in a coordinator-controlled stable directory, under the service
user shared by investigation and held-monitor processes:

```
python -m desk.provider_pacing --initialize /protected/provider-pacing.sqlite \
  --helius-seconds 2 --jupiter-seconds 2 --backoff-seconds 30
```

Initialization refuses existing files. Activation is separate: explicitly set
`DESK_PROVIDER_PACING_DB` to that SAME file in every participating process. An
absent variable preserves existing lower-policy behavior; present empty, missing,
aliased, rotated, partial or invalid files refuse requests rather than creating or
repairing them. File mode must be0600, single link, DELETE journal, bounded2MiB.
Do not replace/restore the pacing DB to reset cadence or a provider embargo.
Cadences are configured explicitly at initialization,0.05–60seconds; these ranges
are implementation bounds, not a claim the provider permits any particular rate.
Missing/invalid credentials still fail as before. No credential lives in this DB.

One atomic durable slot per provider spaces permission grants, not physical TCP
packet timestamps. A grant is not refunded on crash or failed transport. Helius
and Jupiter have separate clocks; Jupiter quotes and SOL-price reads share one
provider.429 and error responses carrying Retry-After publish a shared embargo;
delay-seconds and timezone-bearing HTTP dates are honored with at least the
configured fallback. Duplicate/oversized or unrepresentable header delays latch a
conservative long embargo; operator diagnosis is needed, not automatic clearing.
No retry is sent. Host wall-clock correctness is a trust assumption; rollback
refuses. Per-request monotonic deadlines and at most5seconds cooperative waiting
apply. SQLite lock timeout50ms; fsync/OS scheduling are not hard realtime bounds.
32 live waiters/provider maximum; expired/crashed waiters are reclaimed. Held
monitor requests precede waiting investigations; already granted slots cannot be
preempted. An investigation can expire under held load and remain charged.

Actual connectors: legacy providers.helius_rpc/Jupiter probe through fetch_json,
and PaperReadSources (including history adapter, collector and held-monitor
reservations). The caller's existing budget reservation occurs BEFORE waiting;
pacing does not reserve/refund/reset those budgets. Collector pacing failures are
retained as ordinary failed paper_read_attempt_v1, with original HTTP status when
available. Active legacy pacing disables redirects and retains a single open.
The protected original-byte/coordinator research transports and discovery
WebSocket listener are outside this narrowly assigned active-callsite patch; they
must not be treated as paced by these connectors. Provider limits can apply across
those products/accounts, so the coordinator must coordinate any overlapping use.

Seams owned elsewhere: worker01 owns higher daily/queue/monitor budget activation;
worker07 owns existing-ledger runtime fingerprint compatibility. This patch changes
desk source and MUST NOT be installed into an active pinned ledger release without
that reviewed compatibility handoff. No services/env activation, policy updates,
ledger reprovision, deployment, merge or live verification is performed here.
