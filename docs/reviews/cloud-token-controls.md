# Cloud token controls review — Agent 06

Date: 8 October 2026 (Asia/Seoul). Repository: pixelmanager-cloud/solana-paper-desk.
Branch: `codex/cloud-wave1-06`. Reviewed base: `e95a8dfcd0536efa5479934f81b872c050d7ed5e`.
Scope: offline audit and adversarial tests only. Production modules, shared guidance,
readiness and task queue are unchanged. No provider, VPS, signer or broadcast access.

## Evidence and assumptions

The tests load `fixtures/mainnet-holder-snapshot.json`, provenance
`PUBLIC_MAINNET_HOLDER_BANK_SNAPSHOT`, bank slot 454347831. Its original mint and
17 holder accounts reconcile to 800017057543498 raw units. Each attack is an
in-memory synthetic mutation of a deep copy; none is claimed as an observed
mainnet event. The fixture file is never rewritten. Snapshot RPC callbacks return
only that local response and assert the method and exact mint/account list.

Legacy offsets follow existing `desk/security.py`, `desk/holders.py` and repository
tests: mint option 0, supply 36, initialized byte 45, freeze option 46; holding
mint 0, wallet 32, delegate option 72, state 108, delegated amount 121, close option
129 and close key 133. These are existing repository layout assumptions, not a
new independently verified specification. COption-none ignores the residual key;
wallet-owned close authority is allowed by existing holding policy. Whether these
choices meet a future entry profile remains a coordinator policy decision.

Token-2022 inventory uses `desk/schemas/token2022_extensions.json`, pinned to
solana-program/token-2022 commit `d9ffb9787187b6bc29adda1a6b389b9931377e03`,
source SHA-256 `d273a87617b06f4a813a2c3c3bbab8180a0a4c657d836a95475259eca9e40f5a`.
No source was fetched. Synthetic extension payload sizes describe test inputs,
not verified payload semantics or actual active authorities.

## Results and boundaries

| Check | Audited behavior | New adversarial coverage |
| --- | --- | --- |
| Mint/freeze revocation | Each present authority blocks mint policy and holder snapshot; invalid option tags cannot be interpreted as revoked. | Independent authority activation; tags 2, 256 and UINT32_MAX; uninitialized and zero supply. |
| Delegate | Any nonzero delegate option blocks holding policy, including wallet delegates and zero allowance; invalid tags block snapshot verification. | Self/external delegate with zero allowance; multi-byte invalid option. |
| Close authority | Absent or wallet-owned close authority passes holding policy; external or invalid option blocks. | All four cases; external close authority contrasted with snapshot coverage. |
| Identity | Program owner and embedded mint/wallet bindings are distinct checks. | Wrong mint, wrong wallet, system-program owner; Token-2022 holding exclusion. |
| Layout/state | Exact 82/165-byte non-executable legacy layouts required; malformed encoding raises rather than returns approval. | Empty, truncated, appended, executable, frozen/uninitialized/unknown states and malformed encodings. |
| Token-2022 | Base mint, metadata-only and recognized control capabilities are always excluded. | Mint close, permanent delegate, hook and pausable inventory; unknown/truncated extensions; known ID with empty payload. |
| Supply evidence | Same-bank coverage is a bounded accounting component, not authority/trading approval. | Original 17-account replay, unchanged fixture, exact local request bindings and policy failures despite supply consistency. |

### Findings requiring coordinator follow-up

1. `verify_holder_snapshot` can return `verified=true` with an external close
   authority. It does not inspect the close option/key. `holding_policy` rejects
   the same bytes; `ownership_snapshot.reconcile_snapshot` invokes that stricter
   policy. The entry-evidence holder component uses snapshot verification and
   explicitly checks frozen/delegated accounts, but does not add close-authority
   checks. This component should not be presented as complete holder control
   approval. Current global entry blockers remain present.
2. `holding_policy` accepts nonzero delegated amount when delegate option is zero.
   Snapshot verification catches a change against the fixture's indexed zero,
   but does not establish a general invariant that amount must be zero with no
   delegate. Entry-evidence reconstruction derives the indexed amount from the
   same bytes, so that comparison alone cannot enforce the invariant. The test
   intentionally records present behavior, not a recommended safe policy.
   Canonical SPL semantics for this inconsistent state were not independently
   researched here; classify as uncertain malformed-state handling.
3. A zero-allowance delegate is listed in `delegated_accounts` while coverage can
   remain verified. That result is accounting coverage; entry-evidence separately
   blocks `DELEGATED_HOLDER_ACCOUNT`. Consumers must preserve this distinction.
   Likewise, a matched frozen account can establish coverage without passing
   the holding policy; entry-evidence separately blocks frozen holders.
4. Extension inventory validates TLV structure and known IDs, not payload lengths,
   mint initialization, active authority values or extension-specific semantics.
   A known permanent-delegate ID with an empty payload can report
   `layout_inventory_complete=true`. This is explicitly diagnostic: trading
   eligibility remains false and mint policy rejects Token-2022 before decoding.
5. Malformed base64 can raise `ValueError` from low-level policy/snapshot helpers.
   These helpers do not provide a universal SKIP return contract. The persisted
   entry evaluator catches malformed-evidence exceptions and blocks the component;
   future callers must retain an equivalent fail-closed boundary.

The characterization tests for gaps should be revised together with any separately
approved production correction. Passing those tests does not endorse the gaps.
Other existing controls reviewed include simulation post-state/metadata bindings
in `desk/effects.py` and policy reuse in finalized historical reconciliation.
This scoped suite does not claim complete program, route, supply-history or
ownership validation.

## Validation

Python: 3.12.14, existing checkout `.venv`; dependencies already available.

- `.venv/bin/python -m unittest tests.test_cloud_token_controls -q`:
  **16 tests in 0.015s, OK**, no skips.
- `.venv/bin/python -m unittest discover -q`:
  **529 tests in 2.793s, OK**, no skips, failures or errors. This equals the
  existing 513 tests plus 16 new tests.
- First system-interpreter discovery attempt: 492 tests in 2.151s,
  109 errors and 31 skips because optional full-suite dependencies were absent
  (`solders` among them). No code changed to accommodate that interpreter.
- First `.venv` discovery attempt under the default sandbox: 529 tests in 2.577s,
  one error because socket creation for the existing
  `DashboardHTTPTests.test_rebinding_csrf_and_readonly_status` was denied.
  The approved full-suite rerun allowed its private `127.0.0.1` fixture server;
  it did not add public access or use live providers.

Logs remain in ignored `work/cloud-token-controls-unittest*.log` for local review.

## Dependencies and handoff

This test/documentation assignment requires no queue predecessor implementation.
It does not claim completion of OWN-1 or OWN-2, and cannot discharge OWN-3's
OWN-1/OWN-2 dependencies or OWN-4's OWN-3 dependency. SELL-1 and PAPER-1 remain
behind OWN-4; PAPER-2, OPS-1 and QA-1 remain downstream. Finalized snapshot live
integration, classification, current-holder exposure, full route validation and
forward paper verification are unresolved coordinator work.

Only `tests/test_cloud_token_controls.py` and this review are changed. Production
fixes for the findings are outside this assignment. Original evidence/decisions,
18-RPC ceiling, unsupported-token rejection and private loopback policy are
preserved. No merge, deployment, issue closure, entry enabling or paper-readiness
claim was performed. Changes are left on the isolated branch for coordinator
retrieval and review.
