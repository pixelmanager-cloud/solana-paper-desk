Draft checkpoint: deterministic pre-entry measurement rejection

New preparation intents use history_first_paper_preparation_v4. Exact exhausted,
verified retained history is replayed through existing feature calculation. Empty
windows and missing required market measurements publish a typed NO_ENTRY before
the fresh cycle. Older v2/v3 intents retain their previous bound/reason grammar.
Evidence failures and incomplete coverage do not acquire this disposition.

Focused: tests.test_empty_history_no_entry, 5 tests PASS, 8.585 seconds.
Synthetic sell-only, tied latest-slot and stale trade rows exercise actual decoded
history and feature replay. Empty publication is idempotent, retires only its
candidate, preserves charges and permits an unrelated scan. Missing wire capture
and incomplete empty history refuse publication.

Remaining: retained completed empty-window reconciliation proof has been drafted
but has no acceptance fixture yet. Its original NULL observation and original
results are preserved; proposed retirement uses the existing terminal receipt
inventory. Deployment compatibility successor authorization is not implemented.
This checkpoint is for independent review, not integration or activation. Full
suite and historical regression verification are pending. No provider calls.
