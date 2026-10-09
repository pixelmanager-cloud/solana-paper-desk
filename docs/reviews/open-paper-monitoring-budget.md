# Shared open-paper monitoring allowance

User authorization fixes 60 read-only provider attempts per rolling 3,600 seconds,
shared across all open positions in the coordinator's fixed paper ledger/evidence
store. Investigation18, daily10 and queue3 remain unchanged. This is disconnected
from cycle wiring, automatic entries and actual execution approval.

Trusted coordinator interface (hold research, evidence invocation, then ledger
cycle locks throughout provisioning/read/collection/delivery):

```python
budget = MonitoringBudget(existing_store, fixed_ledger_path, pinned_config)
budget.provision()  # explicit one-time coordinator setup, not a read-path fallback
# On later invocations, construct without calling provision.
before = budget.snapshot()
source = PaperReadSources(progress, original_entry_scan_id,
                          monitoring_budget=budget)
# Existing rpc/rpc_with_evidence/quote/sol_price interfaces remain unchanged.
after = budget.snapshot()
```

`reserve_read(progress,scan_id,method,params)` is transport-internal: commits one
reservation before credentials/network I/O. Each saved `paper_read_attempt_v1`
keeps its original bytes/source/method/time and adds `monitoring_reservation`
with durable `id`, `total_used`, `window_used`, reserved time/mint, fixed cap/window, checkpoint hash
and unchanged investigation usage. Exact-charge comparison uses `id/total_used`,
NOT rolling `window_used+1`: expiration can decrease the rolling count. No
HistoryProgress counter or admission is synthesized, copied, increased or reset.

The actual current checkpoint, saved config/code fingerprints, hashed entry event,
original scan/admission, mint/pool/taker and remaining raw inventory authorize each
reservation. Buy/candidate/history calls are excluded. Sell quantities must be
positive and no larger than held inventory. Pool bank keys must exactly match the
retained original pool snapshot's vault/LP/pool/config/fee/mint roster. Caller
locks prevent a competing ledger writer from closing/changing positions between
validation and I/O; this cooperative single-host contract is not OS authentication.

Duplicate identity insertion (including REPLACE and rowid aliases) is rejected
before SQLite conflict resolution can delete originals. Completed reservation
identities/timestamps are checked against original hash-addressed transport receipts.
The counter is atomic and retains every reservation/outcome; expiry deletes
nothing. An unfinished attempt prevents another monitoring reservation. Persisted
source failures and backward wall clock permanently require reviewed recovery;
there is no clear/reset command. No outcome, including an accepted quote response,
proves source authenticity, final CPI success, actual fills or paid fees. Exhaustion
is explicit `MONITORING_REQUEST_BUDGET_EXHAUSTED`; snapshot reports
`STALE_UNVERIFIED_BLOCKED`. Caller must retain positions and block unsupported
mark/fill projection, never manufacture an exit or replenish through admission.

Worker08 owns a positions-only collector/cycle successor: existing collector's
investigation +1 guard intentionally remains incompatible with monitoring reads.
Candidate/history paths must retain default transport and original18. Worker06
owns held-position event production; worker10 checkpoint authority remains intact.
The cap cannot establish that all positions can be refreshed before strategy TTL,
and an untrusted forward clock jump cannot be authenticated by a local counter.
No provider/VPS/secrets calls, live acceptance or deployment are claimed.
