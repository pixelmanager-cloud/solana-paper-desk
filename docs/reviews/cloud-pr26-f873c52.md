# PR26 independent review at f873c52

Independent exact-head review (cloud-independent-review-v1) at f873c52cfab48ebb203377d0e3548cfc38c477bf; actual diff, raw fixtures, adversarial tests and pinned parser dependencies read. Head checked before review and immediately before submission. No merge/acceptance/live claim.

P2 — quarantine contradictory admitted captures by snapshot scope, not only by identical reference tuple (desk/pool_vault_admission.py:187–190). Receipt selection filters on r.refs == refs first, so two independently trusted receipts for the SAME source/pool/mint/finalized slot/block time but DIFFERENT raw capture hashes do not conflict. Independent reproduction:
- Start with the committed synthetic six-account fixture (base vault amount 6000).
- Produce a second internally valid capture at the identical point with base vault amount 6001, preserving PDA/ATA/mint/owner/control/supply checks; rehash its response/request manifests.
- In the fixture harness, construct two coordinator_capture receipts for those captures and place BOTH in one trusted policy, using the same allowed source_id, pool, mint, slot and snapshot_time. No actual live acquisition is claimed.
- Query either ref tuple. Both return label=POOL_VAULT, production_snapshot_exclusion_allowed=True and reasons=[], with conflicting amount_raw 6000 versus 6001 for the same finalized vault.
The constant interval/entry flags stay false, but production point-in-time admission can select a convenient conflicting capture. The existing conflict test uses the SAME refs with a second receipt and therefore misses this case. Please detect/quarantine conflicting captures for the same immutable snapshot scope before admission (or enforce a unique admitted capture per scope in the protected ledger and have this API verify it). Different metadata alone need not imply a semantic conflict; contradictory raw state/block time must. Add the two-distinct-ref regression and ensure neither can yield a production exclusion while contradiction is unresolved.

Verified scoped controls:
- Canonical Pump pool-authority -> index-0 PumpSwap pool PDA, exact LP PDA and legacy-token-program ATAs are reconstructed; six distinct accounts and request order/commitment/minContextSlot, exact response slot and bound block time are checked.
- Raw owner/executable/layout/mint/control checks reject delegates, orphan allowances, invalid/external close keys, frozen states, unsupported token programs and inconsistent WSOL lamport/reserve accounting. Independent 300-case base+quote authority matrix passed.
- Capture freshness is explicit and exclusive; independent four-boundary probe passed. Each of the four receipt-bound references cannot be substituted without a matching protected receipt.
- Synthetic receipts never authorize production exclusion. Historical interval exclusion, continuity, common/private control, ownership/entry approval and chain_authenticated remain false even when a snapshot label is admitted; no generic interval classification is emitted.

Positive-source authentication limitation: TrustedSourcePolicy/AcquisitionReceipt are caller-supplied protected trust contracts. Python dataclass type checks, source_id membership, genesis strings and hashes do NOT authenticate acquisition. There is no production receipt issuer/ledger call site in this PR; the test's coordinator_capture branch is a harness attestation, not real chain proof. Before production use, the desktop coordinator must create policy from its candidate-independent protected acquisition ledger, verify actual authorized RPC source/cluster and capture identity, bind all four refs and actual capture/current wall times, and resolve conflicting observations. Never deserialize candidate JSON into these trusted objects or relabel synthetic captures as coordinator_capture. Interval exclusions additionally require complete lifecycle/control/history continuity and a separately reviewed adapter. Point labels establish neither liquidity safety nor ready ownership.

Validation: all 36 files changed since the local baseline were retrieved at the exact head and their Git blob hashes matched the isolated review tree, including all three PR files. Python 3.12.14: 33/33 targeted tests; exact-head full suite 703/703, zero skips, using permission only for existing localhost HTTP fixtures. Independent 300-control/time/ref probes passed; conflicting-capture reproduction demonstrated the P2. Exact-head GitHub Tests run 37713023296 completed success. Fixture-only, no live/provider calls or production edits. PR25 left to worker10 as requested.

Review: https://github.com/pixelmanager-cloud/solana-paper-desk/pull/26#pullrequestreview-5450369141

## Reproducible independent fixture checks

Run from the exact-head tree with its test dependencies installed. These scripts use committed synthetic fixtures only; constructing coordinator_capture receipts here demonstrates an API trust-contract contradiction, not authenticated acquisition.

```python
from copy import deepcopy
from dataclasses import replace
from tests.test_pool_vault_admission import PoolVaultAdmissionTests

f=PoolVaultAdmissionTests();f.setUp()
first_refs=f.refs;first_evidence=deepcopy(f.evidence)
first_receipt=replace(f.receipt,source_kind='coordinator_capture')
f.mutate_bytes(4,64,(6001).to_bytes(8,'little'))
second_refs=f.refs;second_receipt=replace(f.receipt,source_kind='coordinator_capture')
f.evidence.update(first_evidence)
policy=replace(f.policy,receipts=frozenset((first_receipt,second_receipt)),allow_synthetic_fixtures=False)
first=f.admit(refs=first_refs,policy=policy)
second=f.admit(refs=second_refs,policy=policy)
assert first['production_snapshot_exclusion_allowed'] and second['production_snapshot_exclusion_allowed']
assert first['snapshot_slot']==second['snapshot_slot'] and first['snapshot_time']==second['snapshot_time']
assert first['account']==second['account'] and (first['amount_raw'],second['amount_raw'])==('6000','6001')
assert not first['reasons'] and not second['reasons']
assert not first['historical_interval_exclusion_allowed'] and not first['eligible_for_trading']
print('REPRODUCED: one trusted policy contains two conflicting captures of same finalized pool/mint/account/slot/time (amount 6000 vs6001); choosing refs selects either and both return production_snapshot_exclusion_allowed=True with no reasons. No live acquisition claimed.')
import base64
from copy import deepcopy
from dataclasses import replace
from solders.pubkey import Pubkey
from tests.test_pool_vault_admission import PoolVaultAdmissionTests

f=PoolVaultAdmissionTests();count=0
for index in (4,5):
 for delegate in (0,1,2,256,2**32-1):
  for amount in (0,1,2**64-1):
   for close in (0,1,2,256,2**32-1):
    for key_matches in (False,True):
     f.setUp();snapshot=deepcopy(f.evidence[f.refs.snapshot]);account=snapshot['result']['value'][index]
     raw=bytearray(base64.b64decode(account['data'][0]));raw[72:76]=delegate.to_bytes(4,'little')
     raw[121:129]=amount.to_bytes(8,'little');raw[129:133]=close.to_bytes(4,'little')
     raw[133:165]=bytes(Pubkey.from_string(f.q['pool'])) if key_matches else bytes([9])*32
     account['data'][0]=base64.b64encode(raw).decode();f.repin(snapshot=snapshot)
     result=f.admit(account=f.q['base_vault'] if index==4 else f.q['quote_vault'])
     allowed=delegate==0 and amount==0 and (close==0 or (close==1 and key_matches))
     assert result['snapshot_label_admitted']==allowed,(index,delegate,amount,close,key_matches,result)
     assert not result['production_snapshot_exclusion_allowed']
     for flag in ('chain_authenticated','historical_interval_exclusion_allowed','continuity_verified','private_control_proven','ownership_approval','eligible_for_trading'):
      assert not result[flag],flag
     count+=1
f.setUp()
for now,allowed in ((f.captured_at-1,False),(f.captured_at,True),(f.captured_at+59,True),(f.captured_at+60,False)):
 assert f.admit(now=now)['snapshot_label_admitted']==allowed
for refs_field in ('snapshot','snapshot_request','block_time','block_time_request'):
 refs=replace(f.refs,**{refs_field:'f'*64})
 result=f.admit(refs=refs)
 assert not result['snapshot_label_admitted'] and 'ACQUISITION_RECEIPT_MISSING_OR_CONFLICTING' in result['reasons']
print(f'PASS: {count} independent base/WSOL authority combinations, four freshness boundaries and each independently receipt-bound reference; every synthetic case keeps production/interval/entry/chain-authentication flags false.')

```
