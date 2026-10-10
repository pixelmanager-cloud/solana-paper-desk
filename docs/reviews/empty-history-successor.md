Draft exact one-successor compatibility, dependent on PR206

New ledger table: paper_runtime_empty_history_successor (id=1), immutable INSERT
conflict, UPDATE and DELETE guards. Original four SQL extension slots and existing
performance continuation are unchanged. There is exactly one new independently
reviewed policy receipt in config/runtime-empty-history-successor.json; its
repository template authorizes none. No automatic second successor or reset.

API (desk.runtime_empty_history_successor):
plan(ledger_db, *, dispatch_predecessor, dispatch_successor, recovery_pin)
append(ledger_db, *, pin)
read(c), schema(), guards().

Coordinator planning requires stopped writers, original backups and protected
canonical paths. recovery_pin must already be independently reviewed/installed
in the separate reconciliation policy. plan is read-only, checks the existing
performance parent and complete original dispatcher prefix and emits one exact
current source/tool rollover. It does not append anything.

Activation order: independently verify/reconcile the empty-window retirement
first, preserving original NULL and results. Then append the reviewed successor.
append locks journal -> research -> evidence -> ledger, checks reviewed context,
replays the original recovery plus the entire terminal gate using the existing
exact historical source selector BEFORE mutation, checks exact complete journal
prefix, then appends one new receipt. Partial failure keeps original records and
blocks; never restart/retry automatically. Neither API starts services/providers.

Every later runtime use validates all four original extensions, the existing
performance receipt and its prefix, then new source/context/parent/recovery hash
and the entire newly pinned journal prefix. Old source selectors still validate
the new receipt. Dispatcher bindings authorize predecessor records only inside
that immutable prefix, including historical contexts needed by result replay.
Late injected predecessor contexts do not receive a blanket waiver. Historical
native-ledger digest calculation excludes the new table only after validation.

Focused prior to the final full-gate append check: 17 PASS / 68.552s, covering new
scalar/guard bounds, three real four-edge parent regression tests and current
recovery tests including actual BUY/held/full-exit. Full installed new-successor
fixture and final source validation remain pending; this is a draft, not deployment
approval. The coordinator's production retained recovery plan independently passed
read-only at PR206 6b56b89 (digest 915d412be08ee751d1774fe7908615f497b07e5b1a7dd3616cebb9132e567cf8).

Forward repair for review 5478812151 authenticates the complete predecessor
journal at planning and initial append, using the explicit historical source
selector and original reviewed retirement exceptions. Prefix equality alone
does not prove completion. A genuine four-edge/performance fixture adds a valid
unresolved intent after the old prefix: old prefix verification passes, but full
disposition replay rejects it. Five focused tests PASS / 3.465s. The initial full
suite was interrupted after confirmed sandbox socket PermissionError failures;
no full-suite PASS is claimed. Installed lifecycle coverage belongs to worker06.
